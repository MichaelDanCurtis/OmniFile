"""How a migration step calls a provider (docs/design/migration-engine.md section 5).

A transaction id held during a call stops the edge feed for every edge, so a step makes
each call `outside` its transaction, and its writes wait. The pass that asks commits
nothing: the worker's savepoint rolls it back. The consumer makes the call with no
transaction open, then runs the step again from the top with the result in hand. The
last pass makes no call: it locks the prefix, reads everything afresh, and does all the
step's writes in one transaction, as a step always has.

So a call's result is as old as the call, and everything else the step looks at is new.

A call that makes something a retry could make again, or pay for again (a Batch job, a
staging copy, a staging run), can't wait for the last pass, since a later call's error would
roll its record back. So the pass after such a call records it and does nothing else, whatever
state the prefix is in by then, and the step ends there (`engine.migrate_step`). It records
what was asked for, whatever the catalog says now: the call returns that with its answer.

Two things a row lock used to give, and what gives them now:

- **One step per prefix at a time.** The worker holds the prefix's lease, a session
  advisory lock, from the first pass to the end of the message. It needs no transaction,
  and it dies with the connection, so a crash frees it (`worker.migrate_jobs`).
- **Nobody else takes the message.** Each call hides it for longer than the call's bound.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime

from ..backends.base import BackendError
from ..worker.queue import Message, outside
from .mover import Mover, MoverJob

#: A call that should answer in moments: a status, a page, a CreateJob, a few deletes.
CALL_SECONDS = 240.0
#: A step's message stays hidden at least this long, as when it was read.
STEP_VT_S = 15 * 60
HIDE_MARGIN_S = 60.0


def called(msg: Message | None, tag: str) -> bool:
    """This message already made the call `tag`: its answer is in hand."""
    return msg is not None and tag in msg.called


async def bounded[T](call: Callable[[], Awaitable[T]], seconds: float) -> T:
    """`call`, or a `BackendError` once it has taken `seconds`, which the step retries
    like any provider error."""
    try:
        async with asyncio.timeout(seconds):
            return await call()
    except TimeoutError:
        raise BackendError(f"no answer from the provider in {seconds:.0f} s") from None


async def provider[T](
    msg: Message | None,
    tag: str,
    call: Callable[[], Awaitable[T]],
    *,
    seconds: float | None = None,
) -> T:
    """The answer to `call`, made outside the transaction with a bound on it (by default
    `CALL_SECONDS`). `tag` must name what is asked: a call is made once per tag for a
    message."""
    bound = CALL_SECONDS if seconds is None else seconds
    return await outside(
        msg,
        tag,
        lambda: bounded(call, bound),
        hide_for=max(bound + HIDE_MARGIN_S, STEP_VT_S),
    )


async def start_run(
    msg: Message | None, tag: str, mover: Mover, job: MoverJob, now: datetime
) -> tuple[str, str, datetime]:
    """(job name, run id, when it was asked for) for a run of `job`. If the job already
    has a run going, that is the one to follow: a step that crashed after starting it
    never recorded it, and a second run is a second scan of the whole prefix. It is looked
    for before the job is touched: Azure may refuse a change to a job with a run going, and
    a refusal would be retried until the run ended, and then a second one started."""

    async def go() -> tuple[str, str, datetime]:
        run_id = await mover.running_run(job.name)
        if run_id is not None:
            return job.name, run_id, now
        name = await mover.ensure_job(job)
        return name, await mover.start_run(name), now

    return await provider(msg, tag, go)


__all__ = ["CALL_SECONDS", "STEP_VT_S", "bounded", "called", "provider", "start_run"]
