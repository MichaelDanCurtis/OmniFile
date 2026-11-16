import { useId, useState } from "react";
import { useGroupSearch } from "../api/queries";
import type { GroupRef } from "../api/types";
import { useDebounced } from "../lib/useDebounced";

export function groupName(group: GroupRef | null | undefined): string {
  if (!group) return "Nobody";
  return group.display_name ?? group.sam_account_name ?? group.sid;
}

/** Search the AD groups the catalog knows (GET /v1/directory/groups). */
export function GroupPicker({
  label,
  onPick,
  disabled,
}: {
  label: string;
  onPick: (group: GroupRef) => void;
  disabled?: boolean;
}) {
  const [q, setQ] = useState("");
  const debounced = useDebounced(q, 250);
  const results = useGroupSearch(debounced);
  const id = useId();
  const listId = useId();
  const items = debounced ? (results.data?.items ?? []) : [];
  return (
    <div className="group-pick">
      <label className="field-label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        type="search"
        value={q}
        disabled={disabled}
        placeholder="Group name, sAMAccountName or SID"
        aria-controls={debounced ? listId : undefined}
        autoComplete="off"
        onChange={(e) => setQ(e.target.value)}
      />
      {debounced ? (
        <ul id={listId} className="group-results" aria-label={`Groups matching ${debounced}`}>
          {items.length === 0 ? (
            <li className="muted" style={{ padding: "6px 10px" }}>
              {results.isFetching ? "Searching…" : "No groups found."}
            </li>
          ) : (
            items.map((g) => (
              <li key={g.sid}>
                <button
                  type="button"
                  onClick={() => {
                    onPick(g);
                    setQ("");
                  }}
                >
                  <span>{groupName(g)}</span>
                  <span className="mono muted">{g.sid}</span>
                </button>
              </li>
            ))
          )}
        </ul>
      ) : null}
    </div>
  );
}
