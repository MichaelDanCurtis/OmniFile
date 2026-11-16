/**
 * Decision 34: the names a restored version gets. The server decides (and may
 * shorten a long stem to fit 255 bytes); these are for showing the choice.
 *   restored              <stem>-restored<ext>
 *   restored_timestamped  <stem>-restored-<YYYY-MM-DD-HHMMSS>Z<ext>, UTC, no colons
 * The extension starts at the last dot; a name whose only dot leads it
 * (".bashrc") has none.
 */
export function splitName(name: string): { stem: string; ext: string } {
  const dot = name.lastIndexOf(".");
  if (dot <= 0) return { stem: name, ext: "" };
  return { stem: name.slice(0, dot), ext: name.slice(dot) };
}

export function utcStamp(at: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${at.getUTCFullYear()}-${pad(at.getUTCMonth() + 1)}-${pad(at.getUTCDate())}-` +
    `${pad(at.getUTCHours())}${pad(at.getUTCMinutes())}${pad(at.getUTCSeconds())}Z`
  );
}

export function restoredName(name: string): string {
  const { stem, ext } = splitName(name);
  return `${stem}-restored${ext}`;
}

export function restoredTimestampedName(name: string, at: Date): string {
  const { stem, ext } = splitName(name);
  return `${stem}-restored-${utcStamp(at)}${ext}`;
}

export type RestoreTarget = "overwrite" | "restored" | "restored_timestamped";
