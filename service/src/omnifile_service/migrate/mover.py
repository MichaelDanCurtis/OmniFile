"""The copier port (docs/design/migration-engine.md section 4).

Azure Storage Mover copies; OmniFile never does (decision 20). A job
definition takes one source subpath and one target subpath, in Additive mode
(copy what the target doesn't have; never Mirror, which deletes at the
target), and a run reports counters, not objects [V].

- `Mover`: the port the engine drives.
- `SimMover`: Storage Mover's semantics over the service's own backends, for
  tests and e2e on sim and fakes. It is not offered for real backends: a
  production worker gets the Azure Storage Mover client (design note
  section 10, part 2), or none, and `copy_by = omnifile` prefixes wait.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from ..backends.base import ObjectNotFound, ObjectRef, RestoreState, StorageBackend

#: Run counters, as Storage Mover names them (snake_case).
COUNTERS = ("scanned", "transferred", "failed", "unsupported", "no_transfer_needed")


@dataclass(frozen=True)
class MoverJob:
    """One job definition: a source subpath copied to a target subpath."""

    name: str
    source_backend_id: int
    source_container: str
    source_subpath: str
    target_backend_id: int
    target_container: str
    target_subpath: str


@dataclass(frozen=True)
class MoverRun:
    run_id: str
    state: str  # running | succeeded | failed
    counters: Mapping[str, int] = field(default_factory=dict)
    error: str | None = None


class Mover(Protocol):
    async def ensure_job(self, job: MoverJob) -> str:
        """Create or update the job definition; returns its name."""
        ...

    async def start_run(self, name: str) -> str:
        """Start a run of the job; returns the run id."""
        ...

    async def run_status(self, name: str, run_id: str) -> MoverRun: ...

    async def running_run(self, name: str) -> str | None:
        """The job's run that is still going, if any: one a crashed step started and
        never recorded is found here, and not started again."""
        ...


class SimMover:
    """Additive copies between the registry's backends, finished at once.

    Archived objects with no live restored copy are `unsupported`, as
    Storage Mover reports unrestored Glacier objects [V]. A target with an
    object of the same size, not older than the source, is
    `no_transfer_needed`; a newer source overwrites it, as Additive (merge)
    updates matching names (VERIFY on real Azure, real-azure-checks.md).
    """

    def __init__(self, backends: Mapping[int, StorageBackend] | object) -> None:
        self._backends = backends
        self._jobs: dict[str, MoverJob] = {}
        self._runs: dict[str, MoverRun] = {}
        self._seq = itertools.count(1)
        #: Corrupt the next copy of a key (tests: a bad copy must be caught).
        self.corrupt: set[str] = set()
        #: Fail this many of the next runs of a job (tests).
        self.fail_runs: dict[str, int] = {}
        #: Jobs whose runs copy but report `running` until taken off (tests).
        self.hold_runs: set[str] = set()
        #: Every run started (tests).
        self.started: list[str] = []

    def _backend(self, backend_id: int) -> StorageBackend:
        get = getattr(self._backends, "get", None)
        if get is None:
            raise TypeError("SimMover needs a mapping or a BackendRegistry")
        return get(backend_id)  # type: ignore[no-any-return]

    async def ensure_job(self, job: MoverJob) -> str:
        self._jobs[job.name] = job
        return job.name

    async def start_run(self, name: str) -> str:
        self.started.append(name)
        job = self._jobs[name]
        if self.fail_runs.get(name):
            self.fail_runs[name] -= 1
            run_id = f"{name}-run-{next(self._seq)}"
            self._runs[run_id] = MoverRun(run_id, "failed", {}, "the run failed")
            return run_id
        src = self._backend(job.source_backend_id)
        dst = self._backend(job.target_backend_id)
        counts = dict.fromkeys(COUNTERS, 0)
        cursor: str | None = None
        while True:
            page = await src.list(job.source_container, job.source_subpath, cursor)
            for head in page.items:
                counts["scanned"] += 1
                if head.restore_state in (RestoreState.ARCHIVED, RestoreState.IN_PROGRESS,
                                          RestoreState.EXPIRED):  # fmt: skip
                    counts["unsupported"] += 1
                    continue
                rel = head.ref.key[len(job.source_subpath) :]
                target = job.target_subpath + rel
                tref = ObjectRef(job.target_backend_id, job.target_container, target)
                try:
                    have = await dst.head(tref)
                    newer = (
                        head.last_modified is not None
                        and have.last_modified is not None
                        and head.last_modified > have.last_modified
                    )
                    if have.size == head.size and not newer:
                        counts["no_transfer_needed"] += 1
                        continue
                except ObjectNotFound:
                    pass
                try:
                    body = src.get_range(head.ref, 0, head.size + 1)
                    if head.ref.key in self.corrupt:
                        self.corrupt.discard(head.ref.key)
                        body = _flip_first_byte(body)
                    await dst.put(job.target_container, target, body, None, {})
                    counts["transferred"] += 1
                except Exception:  # noqa: BLE001 - counted, as a run reports it
                    counts["failed"] += 1
            if page.cursor is None:
                break
            cursor = page.cursor
        run_id = f"{name}-run-{next(self._seq)}"
        self._runs[run_id] = MoverRun(run_id, "succeeded", counts)
        return run_id

    async def run_status(self, name: str, run_id: str) -> MoverRun:
        run = self._runs.get(run_id) or MoverRun(run_id, "failed", {}, "no such run")
        if name in self.hold_runs and run.state == "succeeded":
            return MoverRun(run_id, "running", run.counters)
        return run

    async def running_run(self, name: str) -> str | None:
        if name not in self.hold_runs:
            return None
        ours = [r for r in self._runs if r.startswith(f"{name}-run-")]
        return ours[-1] if ours else None


async def _flip_first_byte(body: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    first = True
    async for chunk in body:
        if first and chunk:
            chunk = bytes([chunk[0] ^ 0xFF]) + chunk[1:]
            first = False
        yield chunk


__all__ = ["COUNTERS", "Mover", "MoverJob", "MoverRun", "SimMover"]
