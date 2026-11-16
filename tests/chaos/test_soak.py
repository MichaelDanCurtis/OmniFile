"""Tests of the chaos harness's own judgement: tests/chaos/soak.py and soak_io.py, and the
checks in tests/e2e/nfs_grace_test.sh and tests/chaos/storage.sh. They need no VM:

    cd service && uv run pytest ../tests/chaos/test_soak.py -q

(soak.py imports the service package and the e2e helpers, hence the service venv.)
"""
from __future__ import annotations

import contextlib
import errno
import glob
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

import soak  # noqa: E402
import soak_io  # noqa: E402

MiB = soak_io.MiB


def run_io(fn, *args, **kw) -> dict:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(*args, **kw)
    return json.loads(out.getvalue().strip().splitlines()[-1])


def pattern_file(path: Path, size: int, seed: int) -> None:
    with open(path, "wb") as f:
        for p in soak_io.pieces(size, seed):
            f.write(p)


# --- review finding 1: a failed read keeps what it saw before the error -----------------


def test_a_read_that_sees_zeros_then_fails_reports_the_zeros(tmp_path, monkeypatch):
    size, seed = 3 * MiB, 7
    f = tmp_path / "stub"
    pattern_file(f, size, seed)
    with open(f, "r+b") as fh:  # the first MiB reads as zeros (an unfilled region)
        fh.write(bytes(MiB))
    real = os.preadv

    def eio_after_first_mib(fd, bufs, off):
        if off >= MiB:
            raise OSError(errno.EIO, "Input/output error")
        return real(fd, bufs, off)

    monkeypatch.setattr(soak_io.os, "preadv", eio_after_first_mib)
    r = run_io(soak_io.read, str(f), size, seed, direct=False)
    assert r["ok"] is False and r["name"] == "EIO"
    assert r["zero_blocks"] == MiB // soak_io.BLOCK
    # and the soak judges it as zeros, not as an allowed EIO
    assert soak.judge_read(r, changed=False) == "zeros"


def test_a_partial_download_is_checked_over_its_length(tmp_path):
    size, seed = 4 * MiB, 3
    good = tmp_path / "good"
    pattern_file(good, size, seed)
    part = tmp_path / "part"
    part.write_bytes(good.read_bytes()[: MiB + 100])  # smbclient stopped part way
    r = run_io(soak_io.main, ["prefix", str(part), str(size), str(seed)])
    assert r["ok"] is True and r["bytes"] == MiB + 100 and r["zero_blocks"] == 0
    zeroed = tmp_path / "zeroed"
    zeroed.write_bytes(good.read_bytes()[:MiB] + bytes(8192))
    r = run_io(soak_io.main, ["prefix", str(zeroed), str(size), str(seed)])
    assert r["zero_blocks"] == 2


def smb_read_that_fails_part_way(tmp_path, monkeypatch, kept: bytes):
    """read_one over SMB with smbclient stubbed: it leaves KEPT in the destination and
    reports NT_STATUS_IO_DEVICE_ERROR. soak_io runs in-process instead of under setpriv."""
    s = make_soak(tmp_path)
    monkeypatch.setattr(soak, "SMBGET", str(tmp_path / "smbget"))
    monkeypatch.setattr(soak.e2e, "marker_of", lambda path: None)
    calls = []

    def smb(user, command, timeout=420):
        _, _, dst = command.split()
        Path(dst).write_bytes(kept)
        return 1, "getting file \\d\\f.bin: NT_STATUS_IO_DEVICE_ERROR"

    def io_(uid, *args, timeout=420):
        calls.append(args[0])
        return run_io(soak_io.main, [str(x) for x in args])

    monkeypatch.setattr(s, "smb", smb)
    monkeypatch.setattr(s, "io", io_)
    rec = soak.Rec(share="smbwait", rel="d/f.bin", size=4 * MiB, seed=5, state="written")
    r = s.read_one(rec, "smb")
    return s, r, calls


def test_a_failed_smb_read_has_its_partial_file_checked(tmp_path, monkeypatch):
    good = tmp_path / "good"
    pattern_file(good, 4 * MiB, 5)
    kept = good.read_bytes()[:MiB] + bytes(MiB)  # the second MiB came back as zeros
    s, r, calls = smb_read_that_fails_part_way(tmp_path, monkeypatch, kept)
    assert calls == ["prefix"]
    assert r["name"] == "NT_STATUS_IO_DEVICE_ERROR" and r["zero_blocks"] == MiB // soak_io.BLOCK
    assert [v["kind"] for v in s.violations] == ["zeros"]
    assert not list((tmp_path / "smbget").iterdir())  # the download is removed


def test_a_failed_smb_read_with_good_bytes_is_only_an_error(tmp_path, monkeypatch):
    good = tmp_path / "good"
    pattern_file(good, 4 * MiB, 5)
    s, r, calls = smb_read_that_fails_part_way(tmp_path, monkeypatch, good.read_bytes()[:MiB])
    assert calls == ["prefix"] and r["zero_blocks"] == 0 and r["bytes"] == MiB
    assert s.violations == []
    assert s.counts["read_err:smb:NT_STATUS_IO_DEVICE_ERROR"] == 1


def test_judge_read_puts_zeros_and_wrong_bytes_before_errors():
    assert soak.judge_read({"ok": False, "name": "EIO", "zero_blocks": 1}, False) == "zeros"
    assert soak.judge_read({"ok": False, "name": "EIO", "zero_blocks": 1}, True) == "zeros"
    assert soak.judge_read({"ok": False, "name": "NT_STATUS_IO_DEVICE_ERROR", "bad_blocks": 3}, False) == "wrong_bytes"
    assert soak.judge_read({"ok": False, "name": "EIO", "zero_blocks": 0, "bad_blocks": 0}, False) == "error"
    assert soak.judge_read({"ok": False, "name": "ENOENT"}, False) == "enoent"
    assert soak.judge_read({"ok": False, "zero_blocks": 0, "bad_blocks": 0, "short_at": 5}, False) == "wrong_bytes"
    assert soak.judge_read({"ok": True, "zero_blocks": 0}, False) == "ok"


# --- review finding 3: deleting a written file that is not there is ENOENT ---------------


def test_enoent_on_deleting_a_written_file_is_a_violation():
    assert soak.delete_outcome("written", False, "ENOENT") == "enoent"
    assert soak.delete_outcome("failed", False, "ENOENT") == "deleted"
    assert soak.delete_outcome("written", False, "EIO") == "failed"
    assert soak.delete_outcome("written", True, None) == "deleted"


# --- review finding 2: a run that exercises nothing is not GREEN ------------------------


BUSY = {"write_ok:nfs-v3": 30, "write_ok:nfs-v41": 30, "write_ok:smb": 25,
        "read_ok:v3:filled": 60, "read_ok:v42:unfilled": 50, "read_ok:v41:archived": 12,
        "read_ok:smb:filled": 40, "read_ok:smb:unfilled": 15, "read_ok:smb:archived": 12,
        "recalls_seen": 9, "listings": 40, "listings:nfs": 30, "listings:smb": 10}


def test_a_run_that_exercised_nothing_is_red():
    mins = soak.parse_minimums(soak.DEFAULT_MINIMUMS)
    short = soak.shortfalls({}, mins)
    assert short and not soak.green([], False, True, short)
    assert soak.shortfalls(BUSY, mins) == []
    assert soak.green([], False, True, [])


def test_the_minimums_are_per_protocol():
    """Every SMB read failed, NFS was busy: not GREEN on NFS alone."""
    mins = soak.parse_minimums(soak.DEFAULT_MINIMUMS)
    no_smb_reads = {k: v for k, v in BUSY.items() if not k.startswith("read_ok:smb:")}
    no_smb_reads["read_err:smb:NT_STATUS_IO_DEVICE_ERROR"] = 70
    short = soak.shortfalls(no_smb_reads, mins)
    assert {s.split()[0] for s in short} == {"smb.reads", "smb.unfilled", "smb.archived"}
    assert not soak.green([], False, True, short)
    no_smb_writes = {k: v for k, v in BUSY.items() if k != "write_ok:smb"}
    assert [s.split()[0] for s in soak.shortfalls(no_smb_writes, mins)] == ["smb.write_ok"]
    no_smb_listings = {k: v for k, v in BUSY.items() if k != "listings:smb"}
    assert [s.split()[0] for s in soak.shortfalls(no_smb_listings, mins)] == ["smb.listings"]


def test_bad_minimums_are_refused_before_the_run(tmp_path, monkeypatch, capsys):
    def never(a):
        raise AssertionError("a run started with bad --minimums")

    monkeypatch.setattr(soak, "Soak", never)
    base = ["--out", str(tmp_path / "o"), "--report", str(tmp_path / "r.md"), "--seed", "1"]
    for bad in ("write_ok=50", "nfs.reads=lots", "nfs.reads=-1", "smb.reads", "recalls=5,nope=1"):
        with pytest.raises(SystemExit) as e:
            soak.main(base + ["--minimums", bad])
        assert e.value.code == 2, bad
        assert "--minimums" in capsys.readouterr().err
    for bad in ("write_ok=50", "nfs.reads=x"):
        with pytest.raises(ValueError):
            soak.parse_minimums(bad)
    assert soak.parse_minimums("nfs.reads=0, recalls=3") == {"nfs.reads": 0, "recalls": 3}


# --- review finding 9: the replay line and cancelled recalls ----------------------------


def test_the_replay_line_names_everything_the_schedule_depends_on():
    a = SimpleNamespace(duration=100, each=2, warmup=10, cooldown=20, files=30, big=3)
    cmd = soak.replay_cmd(a, 42)
    for part in ("--seed 42", "--duration 100", "--each 2", "--warmup 10", "--cooldown 20",
                 "--files 30", "--big 3"):
        assert part in cmd


def test_a_cancelled_recall_of_a_file_still_there_is_flagged():
    assert soak.judge_recall("cancelled", file_live=True)
    assert soak.judge_recall("cancelled", file_live=False) is None
    assert soak.judge_recall("downloading", file_live=True) == "still downloading"
    assert soak.judge_recall("failed", file_live=False) == "failed"
    assert soak.judge_recall("available", file_live=True) is None


def test_a_superseded_recall_is_accepted_only_when_what_it_says_happened():
    """The 2026-11-08 soak's one violation: a recall of a file overwritten meanwhile
    ended `failed`. It now ends `superseded`, which the soak accepts, but not on the
    recall's word alone: the file must hold another version, or be gone."""
    j = soak.judge_recall
    assert j("superseded", True, "replaced", still_current=False) is None
    assert j("superseded", False, "replaced") is None  # replaced, then deleted
    assert j("superseded", False, "deleted") is None
    assert j("superseded", True, "replaced", still_current=True) == (
        "superseded as replaced, the file still holds that version")
    assert j("superseded", True, "deleted") == "superseded as deleted, the file still there"
    assert j("superseded", True, None).startswith("superseded for no known reason")
    # Everything else is judged as before.
    assert j("failed", True, "replaced") == "failed"
    assert j("downloading", True, None) == "still downloading"


# --- review finding 7: recovery timed from the kill; no new pid is a violation ----------


def make_soak(tmp_path) -> soak.Soak:
    a = SimpleNamespace(seed=1, out=str(tmp_path / "out"), duration=60, each=1, warmup=1,
                        cooldown=1, settle=1, files=10, big=1,
                        minimums=soak.DEFAULT_MINIMUMS,
                        label="t", report=str(tmp_path / "r.md"))
    return soak.Soak(a)


def test_a_kill_that_never_gets_a_new_pid_is_a_violation(tmp_path, monkeypatch):
    s = make_soak(tmp_path)
    s.restart_budget = 0.5
    monkeypatch.setattr(s, "sh", lambda *cmd, **kw: (0, "4242"))  # MainPID never changes
    monkeypatch.setattr(s, "active", lambda unit: True)
    d = s.kill_and_wait("omnifile-edge")
    assert d["restarted_s"] is None and d["kill_t"]
    assert [v["kind"] for v in s.violations] == ["recovery"]


def test_recovery_is_timed_from_the_kill(tmp_path, monkeypatch):
    s = make_soak(tmp_path)
    monkeypatch.setattr(s, "active", lambda unit: True)
    assert s.recover("kill_edge", since=time.time() - 5) >= 5


# --- review findings 4, 5, 6 and 9: the grace script's own checks -----------------------


BASHES = sorted({os.path.realpath(b) for b in ("/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash",
                                                "/opt/homebrew/bin/bash") if os.path.exists(b)})


@pytest.mark.parametrize("bash", BASHES)  # macOS's 3.2 and a current one, where both exist
def test_the_grace_scripts_self_test_passes_and_leaves_no_copy(bash):
    before = set(glob.glob("/tmp/omnifile-grace-*"))
    # The script re-runs itself with the bash on PATH: put this one first.
    env = {**os.environ, "PATH": f"{os.path.dirname(bash)}:{os.environ['PATH']}"}
    p = subprocess.run([bash, str(REPO / "tests/e2e/nfs_grace_test.sh"), "--self-test"],
                       capture_output=True, text=True, timeout=60, env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "FAIL" not in p.stdout
    for name in ("a rider that never finished fails",
                 "a lock granted early is seen before the other attempts end",
                 "the contender writes each attempt's answer as att.ingrace.N.json",
                 "granted_so_far counts the contender's own answers",
                 "ns_down leaves a pid in this shell's own namespace alone",
                 "ns_down forgets that pid",
                 "the contender exits when told"):
        assert f"ok: {name}" in p.stdout
    assert set(glob.glob("/tmp/omnifile-grace-*")) == before


# --- #48 review minor: the self-test's wait for the contender is bounded ----------------


@pytest.mark.parametrize("bash", BASHES)
def test_a_contender_that_never_exits_fails_the_self_test_instead_of_hanging_it(bash, tmp_path):
    # python3 on PATH runs the real contender (so its answers are written), but the process the
    # self-test waits on never exits: before the bound, `wait "$cpid"` hung until the timeout.
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "python3").write_text(
        "#!/bin/sh\n"
        f'if [ "$2" = contender ]; then "{sys.executable}" "$@" & exec sleep 300; fi\n'
        f'exec "{sys.executable}" "$@"\n')
    (shim / "python3").chmod(0o755)
    env = {**os.environ, "PATH": f"{shim}:{os.path.dirname(bash)}:{os.environ['PATH']}"}
    p = subprocess.Popen([bash, str(REPO / "tests/e2e/nfs_grace_test.sh"), "--self-test"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                         start_new_session=True)
    try:
        out, _ = p.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, 9)
        p.communicate()
        pytest.fail("the self-test hung on a contender that never exits")
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, 9)
    assert p.returncode == 1, out
    assert "FAIL: the contender exits when told" in out
    assert "ok: the contender writes each attempt's answer as att.ingrace.N.json" in out


# --- review finding 9: storage.sh off honours a custom BIGDIR ---------------------------


def test_storage_off_honours_a_custom_bigdir():
    p = subprocess.run(["bash", str(HERE / "storage.sh"), "off", "/srv/big2", "--delete"],
                       capture_output=True, text=True, env={**os.environ, "STORAGE_PLAN": "1"})
    assert p.stdout.strip() == "action=off bigdir=/srv/big2 delete=1", p.stdout + p.stderr
    p = subprocess.run(["bash", str(HERE / "storage.sh"), "off", "--delete", "/srv/big3"],
                       capture_output=True, text=True, env={**os.environ, "STORAGE_PLAN": "1"})
    assert p.stdout.strip() == "action=off bigdir=/srv/big3 delete=1"


# --- 2026-11-08 regression: a lane's verify-only drop-in mustn't outlive it ------------------


@pytest.mark.parametrize("bash", BASHES)
def test_worker_env_removes_a_verify_only_drop_in_left_by_a_lane(bash, tmp_path):
    """The soak sets OMNIFILE_STUB_AFTER_MINUTES=0 with Environment=, which a lane's leftover
    EnvironmentFile= (stub after a year) overrides: nothing was stubbed for 6 hours."""
    run = tmp_path / "run"
    dropins = run / "omnifile-dev-worker.service.d"
    dropins.mkdir(parents=True)
    verify = dropins / "90-e2e-verify-only.conf"
    verify.write_text("[Service]\nEnvironmentFile=/run/omnifile-e2e-worker.env\n")
    envfile = run / "omnifile-e2e-worker.env"
    envfile.write_text("OMNIFILE_STUB_AFTER_MINUTES=525600\n")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "systemctl").write_text("#!/bin/sh\necho \"systemctl $*\" >>\"$SYSTEMCTL_LOG\"\n")
    (fakebin / "systemctl").chmod(0o755)
    env = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}", "REPO": str(REPO),
           "WORK": str(tmp_path), "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
           "WORKER_DROPIN": str(dropins / "90-omnifile-chaos.conf"),
           "E2E_VERIFY_DROPIN": str(verify), "E2E_WORKER_ENV": str(envfile)}
    p = subprocess.run([bash, "-c", '. "$1"; worker_env OMNIFILE_STUB_AFTER_MINUTES=0', "bash",
                        str(REPO / "tests/chaos/lib/devstack.sh")],
                       capture_output=True, text=True, timeout=30, env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert not verify.exists() and not envfile.exists(), "the lane's leftover still overrides"
    assert "Environment=OMNIFILE_STUB_AFTER_MINUTES=0" in (dropins / "90-omnifile-chaos.conf").read_text()
    assert "systemctl restart omnifile-dev-worker" in (tmp_path / "systemctl.log").read_text()


def _exit_handler_text(text: str) -> str:
    """The EXIT trap's function and the functions it calls, one level down."""
    trap = [line for line in text.splitlines() if line.startswith("trap ") and line.endswith(" EXIT")]
    assert trap, "no EXIT trap"
    handler = trap[-1].split()[1]
    body = text.split(f"{handler}() {{", 1)[1].split("\n}", 1)[0]
    called = [w for w in body.split() if w.isidentifier()]
    return body + "".join(text.split(f"{c}() {{", 1)[1].split("\n}", 1)[0]
                          for c in called if f"{c}() {{" in text)


@pytest.mark.parametrize("script, dropin", [
    ("exit_test.sh", "90-e2e-verify-only.conf"),
    ("m2_exit_test.sh", "90-e2e-verify-only.conf"),
    ("large_copy_test.sh", "90-e2e-verify-only.conf"),
    ("m3_exit_test.sh", "90-e2e-m3.conf"),
    ("m5_exit_test.sh", "90-e2e-m5.conf"),
])
def test_a_lane_removes_its_worker_drop_in_on_any_exit(script, dropin):
    """A worker drop-in a lane writes in /run lasts until a reboot: the lane's EXIT handler
    removes it, and HUP, INT and TERM go through that handler, however the lane ends."""
    text = (REPO / "tests/e2e" / script).read_text()
    assert dropin in text
    assert f"rm -f /run/systemd/system/omnifile-dev-worker.service.d/{dropin}" in _exit_handler_text(text)
    for sig, code in (("HUP", 129), ("INT", 130), ("TERM", 143)):
        assert f"trap 'exit {code}' {sig}" in text, f"{sig} doesn't go through cleanup"


def test_the_soak_refuses_a_worker_that_wont_stub_at_once():
    """The soak says the worker's effective OMNIFILE_STUB_AFTER_MINUTES at the start, and stops
    when it isn't 0, rather than reporting RED after 6 hours."""
    text = (REPO / "tests/chaos/soak.sh").read_text()
    assert "worker_effective OMNIFILE_STUB_AFTER_MINUTES" in text
    assert '[ "$stub_after" = 0 ] ||' in text


@pytest.mark.parametrize("bash", BASHES)
def test_worker_effective_reads_the_running_worker_s_environment(bash, tmp_path):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    # A worker process with its environment: this test's own python, with K set.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            env={**os.environ, "OMNIFILE_STUB_AFTER_MINUTES": "525600"})
    try:
        (fakebin / "systemctl").write_text(f"#!/bin/sh\necho {proc.pid}\n")
        (fakebin / "systemctl").chmod(0o755)
        if not Path(f"/proc/{proc.pid}/environ").exists():
            pytest.skip("no /proc on this OS: run on Linux")
        env = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}", "REPO": str(REPO),
               "WORK": str(tmp_path)}
        p = subprocess.run([bash, "-c", '. "$1"; worker_effective OMNIFILE_STUB_AFTER_MINUTES', "bash",
                            str(REPO / "tests/chaos/lib/devstack.sh")],
                           capture_output=True, text=True, timeout=30, env=env)
        assert p.stdout.strip() == "525600", p.stdout + p.stderr
    finally:
        proc.kill()


@pytest.mark.parametrize("bash", BASHES)
def test_worker_effective_waits_for_a_worker_that_has_only_just_been_restarted(bash, tmp_path):
    """The short soak of 2026-11-11 refused to start: read right after `systemctl restart`,
    the unit's main process was still systemd's child, about to exec the worker, and K was
    not in its environment yet. The same pid gets it a moment later, at the exec."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    proc = subprocess.Popen(
        ["sh", "-c", "sleep 1; OMNIFILE_STUB_AFTER_MINUTES=0 exec sleep 30"],
        env={k: v for k, v in os.environ.items() if k != "OMNIFILE_STUB_AFTER_MINUTES"})
    try:
        (fakebin / "systemctl").write_text(f"#!/bin/sh\necho {proc.pid}\n")
        (fakebin / "systemctl").chmod(0o755)
        if not Path(f"/proc/{proc.pid}/environ").exists():
            pytest.skip("no /proc on this OS: run on Linux")
        env = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}", "REPO": str(REPO),
               "WORK": str(tmp_path)}
        p = subprocess.run([bash, "-c", '. "$1"; worker_effective OMNIFILE_STUB_AFTER_MINUTES', "bash",
                            str(REPO / "tests/chaos/lib/devstack.sh")],
                           capture_output=True, text=True, timeout=30, env=env)
        assert p.stdout.strip() == "0", p.stdout + p.stderr
    finally:
        proc.kill()


def test_worker_effective_gives_up_on_a_worker_that_never_has_the_setting(tmp_path):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "systemctl").write_text("#!/bin/sh\necho 0\n")
    (fakebin / "systemctl").chmod(0o755)
    env = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}", "REPO": str(REPO),
           "WORK": str(tmp_path), "WORKER_EFFECTIVE_WAIT_S": "1"}
    p = subprocess.run([BASHES[0], "-c", '. "$1"; worker_effective OMNIFILE_STUB_AFTER_MINUTES; echo "rc=$?"',
                        "bash", str(REPO / "tests/chaos/lib/devstack.sh")],
                       capture_output=True, text=True, timeout=30, env=env)
    assert p.stdout.strip() == "rc=0", p.stdout + p.stderr


# --- the run's evidence outlives the VM (2026-11-08 regression) ------------------------------


def test_the_journal_is_saved_gzipped_for_the_run_s_units(tmp_path):
    seen = {}

    class FakeProc:
        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
            self.stdout = io.BytesIO(b"edge: recall fill interrupted\nworker: verify ok\n")

        def wait(self):
            return 0

    path = soak.save_journal(tmp_path, 1794186007.9, popen=FakeProc)
    assert path == tmp_path / "journal.log.gz"
    import gzip
    assert gzip.open(path).read() == b"edge: recall fill interrupted\nworker: verify ok\n"
    cmd = seen["cmd"]
    assert cmd[:1] == ["journalctl"] and "--since" in cmd and "@1794186007" in cmd
    for unit in ("omnifile-edge", "omnifile-dev-worker", "omnifile-dev-api", "nfs-ganesha-omni",
                 "smbd", "postgresql@17-main"):
        assert unit in cmd, unit


def test_a_violation_names_its_recall_and_its_file_even_after_the_file_is_deleted():
    recalls = {209: {"file_id": 515, "state": "failed"}, 7: {"file_id": 3, "state": "available"}}
    cat = {"wait/soak/d2/f1.bin": {"file_id": 88}}  # 515's file was deleted since: not listed
    violations = [{"kind": "recall", "recall_id": 209, "why": "failed"},
                  {"kind": "lost_commit", "key": "wait/soak/d2/f1.bin"},
                  {"kind": "enoent", "key": "wait/soak/gone.bin"}]
    assert soak.violation_ids(violations, recalls, cat) == ([209], [88, 515])
    assert soak.violation_ids([], recalls, cat) == ([], [])


def test_the_catalog_dump_keeps_going_past_a_query_that_fails():
    class Cur:
        description = [SimpleNamespace(name="recall_id"), SimpleNamespace(name="state")]

        def fetchall(self):
            return [(209, "failed")]

    class Conn:
        def __init__(self):
            self.asked = []

        def execute(self, sql, params):
            self.asked.append((sql, params))
            if "catalog.location" in sql:
                raise RuntimeError("column l.version_id does not exist")
            return Cur()

    conn = Conn()
    dump = soak.dump_catalog(conn, [209], [515])
    assert dump["recall"] == [{"recall_id": 209, "state": "failed"}]
    assert "does not exist" in dump["location"]["error"]
    assert set(soak.EVIDENCE_SQL) <= set(dump) and dump["file_ids"] == [515]
    assert all(p == {"recalls": [209], "files": [515]} for _, p in conn.asked)


def test_the_evidence_is_saved_before_the_report_and_never_changes_the_verdict():
    text = (HERE / "soak.py").read_text()
    run = text.split("    def run(self) -> int:", 1)[1].split("    def save_evidence", 1)[0]
    assert run.index("self.verify_end()") < run.index("self.save_evidence()") < run.index("self.report(")
    body = text.split("    def save_evidence(self) -> None:", 1)[1].split("    def enough", 1)[0]
    assert "self.violation(" not in body and "return 1" not in body
