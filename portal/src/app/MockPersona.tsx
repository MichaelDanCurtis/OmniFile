import { useQueryClient } from "@tanstack/react-query";
import { useId } from "react";
import { useSession } from "./session";

const KEY = "omnifile.mockPersona";

/** Dev mock only: switch between an ArchiveAdmin, a DataSteward who owns a share, and a Reader. */
export function MockPersonaSwitcher() {
  const client = useQueryClient();
  const { me } = useSession();
  const id = useId();
  const current = me.roles.includes("ArchiveAdmin")
    ? "admin"
    : me.roles.includes("DataSteward")
      ? "steward"
      : "reader";
  return (
    <div className="mock-banner">
      <label htmlFor={id}>Mock as</label>
      <select
        id={id}
        value={current}
        onChange={async (event) => {
          const persona = event.target.value;
          try {
            localStorage.setItem(KEY, persona);
          } catch {
            // ignore
          }
          const { setPersona } = await import("../mocks/handlers");
          setPersona(persona as "admin" | "steward" | "reader");
          client.clear();
          window.location.assign("/files");
        }}
      >
        <option value="admin">ArchiveAdmin</option>
        <option value="steward">DataSteward (Owner)</option>
        <option value="reader">Reader</option>
      </select>
    </div>
  );
}
