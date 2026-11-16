"""Two counts an alert can keep firing on, read by the metrics collector and Settings.

**Overdue archives.** A file past the end of its archive date and not in Archive. A file
whose next step is Archive stays due until the move lands (its `next_action_at` is the last
sweep that looked), whatever holds it up. So this reads `file_archive_due` (migration 0049),
the files whose next step is Archive, from the oldest due, for at most `SCAN` rows, and costs
the same at 10^8 files. Files due for any other step aren't in that index, so however many go
stale (`next_action_at` isn't cleared when the scheduler stops taking a file), they can't push
an overdue file out of the scan. A file whose dataset no longer has a tiering policy isn't
counted, since nothing will archive it, and is left out inside the scan, so it can't fill it
either. A new policy with no tiering clears the steps the old one left
(`scheduler.reschedule_dataset`), so such rows don't pile up in the index: the scan reads
past them, and only rows from before that fix can make it read further. **The limit:** past
`SCAN` archive steps due at once, the count stops at what those rows hold, which is still above
zero when any of them is overdue. A pin that overrode the date has another next step, so it
isn't counted.

**Own copies that are stuck.** Blobs with a report of a file waiting for its own copy, from
the last retry's day (`policy.engine.HELD_BACK`). Each retry reports again while it stays
stuck, so this stays above zero until the copy can be made. By the audit log's time index,
as the live feed reads it.
"""

from __future__ import annotations

from .engine import HELD_BACK

#: Index entries one count reads.
SCAN = 10_000
#: An archive is late from this long after the end of its date (`tiering._archive_late`).
GRACE = "1 hour"

ARCHIVE_OVERDUE_SQL = f"""
SELECT count(*) AS n
  FROM (SELECT f.kind, f.archive_at, f.remote_tier
          FROM catalog.file f
         WHERE f.next_action = 'archive' AND f.deleted_at IS NULL
           AND f.next_action_at < %(now)s
           AND (%(datasets)s::bigint[] IS NULL OR f.dataset_id = ANY(%(datasets)s::bigint[]))
           AND EXISTS (SELECT 1 FROM catalog.policy p WHERE p.dataset_id = f.dataset_id
                          AND p.state = 'active' AND p.body ? 'archive_day')
         ORDER BY f.next_action_at
         LIMIT {SCAN}) due
 WHERE due.kind = 'file'
   AND due.archive_at < ((%(now)s::timestamptz AT TIME ZONE 'UTC') - interval '{GRACE}')::date
   AND coalesce(due.remote_tier::text, 'hot') <> 'archive'
"""

OWN_COPY_STUCK_SQL = f"""
SELECT count(*) AS n
  FROM catalog.audit_log a
 WHERE a.at > %(since)s AND a.action = 'tier.move.refused' AND a.details->>'why' = '{HELD_BACK}'
"""
