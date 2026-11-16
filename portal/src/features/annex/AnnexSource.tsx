import { useId, useState } from "react";
import { Link } from "react-router";
import { PROTOCOL_LABEL, label, useFilers } from "./api";

/**
 * The New migration wizard's "A NAS share (Annex)" source (annex.md 7): pick
 * the filer and the folder here, then carry on in Create an Annex, which has
 * the annex's own questions.
 */
export function AnnexSource() {
  const filers = useFilers();
  const ids = { filer: useId(), path: useId() };
  const [filer, setFiler] = useState("");
  const [path, setPath] = useState("");
  const q = new URLSearchParams();
  if (filer) q.set("filer", filer);
  if (path) q.set("path", path);
  return (
    <div className="stack">
      <p>
        OmniFile reads a folder on the NAS and copies its files to Azure. People keep using the NAS
        as they do today; nothing on it is changed.
      </p>
      <div className="grid-2">
        <div className="field">
          <label className="field-label" htmlFor={ids.filer}>
            NAS filer
          </label>
          <select id={ids.filer} value={filer} onChange={(e) => setFiler(e.target.value)}>
            <option value="">Choose a filer, or add one on the next page</option>
            {(filers.data?.items ?? []).map((f) => (
              <option key={f.filer_id} value={f.filer_id}>
                {f.name} ({label(PROTOCOL_LABEL, f.protocol)} {f.host})
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label className="field-label" htmlFor={ids.path}>
            Folder on the share
          </label>
          <input id={ids.path} value={path} onChange={(e) => setPath(e.target.value)} />
        </div>
      </div>
      <p>
        <Link className="btn btn-primary" to={`/annexes/new${q.size ? `?${q.toString()}` : ""}`}>
          Continue in Create an Annex
        </Link>
      </p>
    </div>
  );
}
