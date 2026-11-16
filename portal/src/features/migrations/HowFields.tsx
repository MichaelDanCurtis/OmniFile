import { useId } from "react";
import { Callout } from "../../components/Feedback";

export interface How {
  /** `auto`: Azure Storage Mover; `external`: a copier that works through the API. */
  copier: "auto" | "external";
  cutover: "auto" | "manual";
}

export const DEFAULT_HOW: How = { copier: "auto", cutover: "auto" };

const COPIERS: { value: How["copier"]; title: string; body: string }[] = [
  {
    value: "auto",
    title: "Azure Storage Mover (recommended)",
    body: "OmniFile runs Storage Mover jobs over the prefix and watches them. Archived objects are restored first, in waves.",
  },
  {
    value: "external",
    title: "An external copier, through the API",
    body: "Another tool copies each object to the target path and reports its runs through the API. OmniFile only verifies and switches over.",
  },
];

const CUTOVERS: { value: How["cutover"]; title: string; body: string }[] = [
  {
    value: "auto",
    title: "Automatically",
    body: "Files are served from Azure as soon as every one of them is verified.",
  },
  {
    value: "manual",
    title: "When I say",
    body: "It waits at Verified until an ArchiveAdmin presses Cut over on the migration's page.",
  },
];

/** The "how" questions: who copies, and when it switches over. */
export function HowFields({ how, onChange }: { how: How; onChange: (how: How) => void }) {
  const copierName = useId();
  const cutoverName = useId();
  return (
    <div className="stack">
      <fieldset className="options">
        <legend className="field-label">Who copies the files?</legend>
        {COPIERS.map((o) => (
          <label key={o.value} className="option">
            <input
              type="radio"
              name={copierName}
              checked={how.copier === o.value}
              onChange={() => onChange({ ...how, copier: o.value })}
            />
            <span className="option-title">{o.title}</span>
            <span className="option-body">{o.body}</span>
          </label>
        ))}
      </fieldset>
      <fieldset className="options">
        <legend className="field-label">When does it switch over?</legend>
        {CUTOVERS.map((o) => (
          <label key={o.value} className="option">
            <input
              type="radio"
              name={cutoverName}
              checked={how.cutover === o.value}
              onChange={() => onChange({ ...how, cutover: o.value })}
            />
            <span className="option-title">{o.title}</span>
            <span className="option-body">{o.body}</span>
          </label>
        ))}
      </fieldset>
      <Callout tone="info" title="Nothing switches until every file is verified">
        OmniFile reads each copy inside Azure and checks its CRC64-NVME against the checksum S3
        holds for the object. Until every file under the prefix matches, users keep reading from S3,
        whichever switch-over you choose.
      </Callout>
      <Callout tone="info" title="New writes go to Azure from the start">
        While the dataset migrates, every new or changed file is written to Azure. Deletes are never
        applied to the S3 bucket: the migration&apos;s cutover report lists them for whoever cleans
        the bucket up.
      </Callout>
    </div>
  );
}
