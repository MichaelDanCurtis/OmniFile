# Kernel canary

The stub guard is fail-closed only because of a handful of kernel behaviours. The canary checks
them. Its workflow is manual (the repo has no GitHub runners), so run it after a kernel update and
after changing the guard. Risk R3 in
`docs/BUILD-PLAN.md` covers kernel drift: fanotify pre-content events are new in Linux 6.14–7.0,
and an Ubuntu or Azure kernel update could change them. Each behaviour is **asserted**: if it
changes, the job fails and someone must review the guard design against the new kernel.

**The canary tests the kernel, not the product.** It drives the throwaway filer-spike binaries
from `spikes/filer/guard/`:
- `omnifile-guard`, the guard daemon;
- `omnitool`, which makes stubs, reads them and checks the bytes, and runs floods.

It also installs the spike's systemd unit as is. Every fact it asserts, and the kernel source
line behind it, is recorded in `docs/pilots/filer-spike-results-A.md`. When the product's edge
daemon replaces the spike, these checks still describe the same kernel contract.

## What each check asserts

| Check | Spike test | Asserts |
|---|---|---|
| **K1** `k1_precontent.sh` | A1–A4 | On an xfs loop volume, with a `FAN_CLASS_PRE_CONTENT` group, `FAN_MARK_FILESYSTEM` and `FAN_PRE_ACCESS`: <ul><li>with no group, a stub's hole reads as zeros (the baseline);</li><li>with a group, a read of the hole **blocks in state D until the event is answered**;</li><li>ALLOW after a fill returns the correct pattern bytes, and a 3 s fill makes the reader wait at least 3 s;</li><li>`FAN_DENY_ERRNO(EIO)` reaches the reader as `EIO`;</li><li>the resident range reads correctly and causes no fill.</li></ul> |
| **K2** `k2_failopen.sh` | A7, A8, A9 | The **known fail-open facts**, asserted as current behaviour: <ul><li>**K2a:** releasing the group with an event still queued allows it, and the reader sees zeros;</li><li>**K2b:** the same with an event read but not answered;</li><li>**K2c:** without `FAN_UNLIMITED_QUEUE`, exactly *N − limit* readers get zeros at once and `FAN_Q_OVERFLOW` is queued (`max_queued_events` is lowered to 1,024 here, with 1,500 readers);</li><li>**K2d:** an fd opened before any group existed stays unguarded and reads zeros, while a new open is guarded.</li></ul> |
| **K3** `k3_fdstore.sh` | A6 | The fail-closed design, using the spike's unit: the group is kept in the fd store (`FileDescriptorStorePreserve=yes`, unit enabled), with `Restart=always` and `RestartMode=direct`. <ul><li>`kill -9` of the guard while a reader is blocked in a fill: the reader gets **correct bytes** after the restart;</li><li>a kill while idle, followed at once by a read: the read is correct;</li><li>four kills in a row during a stream of 30 reads: no zero, no mismatch, no hung reader;</li><li>the group is created once, every restart reuses it, and the store still holds it at the end.</li></ul> |
| **K4** `k4_flood.sh` | A8 | With `FAN_UNLIMITED_QUEUE`, 17,000 readers, which is more than the default limit of 16,384, wait on a guard that reads nothing. All of them stay blocked and **none gets zeros**. The guard then drains the queue and every byte must be correct. The drain time is recorded for risk R6 (the O(N) wake-up). The reader threads run in their own systemd scope (`TasksMax=infinity`): the runner service's task limit is far below 17,000, and exhausting it starves the runner itself. |
| **K5** `k5_ignore_mark.sh` | A13 | <ul><li>**K5a:** a plain `FAN_MARK_IGNORE` added through the O_RDWR event fd returns 0 but adds nothing, and later reads still raise events;</li><li>**K5b:** the same mark added through a read-only fd, opened by handle on the guard's private fill mount, appears in the group's fdinfo, and a second read raises no events.</li></ul> |

Every check prints its evidence, such as reader state and wchan, omnitool's verdict lines, flood
counters, guard log lines and unit properties. `run.sh` gathers the evidence into the job
summary, along with `uname -r` and the kernel config lines.

The workflow runs **one job per check** (a matrix of `k1` to `k5`), so each check gets a fresh
runner and volume, runs in parallel with the others, and cannot hide the others' results if it
wedges its runner. Each run uses its own concurrency group (the commit SHA), so a later push
never cancels a canary midway.

## Safety

- **The root filesystem is never marked.** `check-mount.sh` runs before every plain-guard start
  and as the unit's `ExecStartPre`, and the unit also gets `RequiresMountsFor=`. It requires all
  of the following, and the UUID is generated at `mkfs` time:
  - the mark path is a mount point;
  - it is xfs on a loop device;
  - it is not the device of `/`;
  - its filesystem UUID matches.
- **Nothing can hang the runner:**
  - Every blocking reader runs under `timeout -s KILL`. A stub read blocked in fanotify sleeps
    `TASK_KILLABLE` (state D), so only a fatal signal frees it.
  - Every check has a time budget in `run.sh`.
  - The workflow step has a timeout.
- **Cleanup runs after every check, and again in an `always()` step.** It kills the readers first,
  because SIGKILL cancels their events. Only then does it release the group: it stops and
  disables the unit and runs `systemctl clean --what=fdstore`. It also restores the sysctl and
  unmounts the volume.

## Running it

The canary needs a disposable Linux machine with kernel 6.14 or later,
`CONFIG_FANOTIFY_ACCESS_PERMISSIONS=y`, systemd and root. GitHub's `ubuntu-26.04` runner
qualifies: kernel 7.0 Azure. It has been seen with 2 vCPU and 7 GB, and every check fits that size.

```
cd spikes/filer/guard && cargo build --release && cd -
sudo apt-get install -y xfsprogs attr
sudo bash ci/kernel-canary/run.sh --bin-dir spikes/filer/guard/target/release          # all
sudo bash ci/kernel-canary/run.sh --bin-dir spikes/filer/guard/target/release --only k1,k5
sudo bash ci/kernel-canary/run.sh --cleanup-only
```

The canary uses `/srv/omni-a`, the spike's layout. **Do not run it on the spike VM while the
spike's own volume is mounted there.** Logs go to `/srv/omni-a/logs` and are uploaded as the
`kernel-canary-logs` artifact. The summary goes to `/srv/omni-a/results/summary.md`.

## When it fails

1. Read the failing assertion in the job summary. A K2 or K5 assertion fails with "kernel
   behaviour CHANGED" when a known fact no longer holds, even if the change is an improvement.
2. Compare the runner's `uname -r` with the last green run.
3. Re-check the cited kernel source in `docs/pilots/filer-spike-results-A.md` against the new
   kernel, and decide whether the guard design, or only the canary's expectation, must change.

**A kernel update must not reach appliances until the canary is green on it** (M6 policy).
