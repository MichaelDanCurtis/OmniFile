import { useId, useState } from "react";
import type { GroupRef } from "../../api/types";
import { GroupPicker, groupName } from "../../components/GroupPicker";

/** One AD group: chosen, or a search to choose one. */
export function GroupField({
  title,
  hint,
  group,
  onChange,
}: {
  title: string;
  hint: string;
  group: GroupRef | null;
  onChange: (group: GroupRef | null) => void;
}) {
  const [changing, setChanging] = useState(false);
  const headId = useId();
  return (
    <div className="field" role="group" aria-labelledby={headId}>
      <span id={headId} className="field-label">
        {title}
      </span>
      <span className="field-hint">{hint}</span>
      {group && !changing ? (
        <div className="row" style={{ gap: 8 }}>
          <strong>{groupName(group)}</strong>
          <span className="mono muted">{group.sam_account_name ?? group.sid}</span>
          <button
            type="button"
            className="btn btn-sm"
            aria-label={`Change the ${title} group`}
            onClick={() => setChanging(true)}
          >
            Change
          </button>
        </div>
      ) : (
        <GroupPicker
          label={`${title} group`}
          onPick={(g) => {
            onChange(g);
            setChanging(false);
          }}
        />
      )}
    </div>
  );
}
