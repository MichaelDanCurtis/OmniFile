/**
 * The OmniFile mark in dots: four rows that thin out as files sink through the
 * tiers (hot, cool, cold, archive), with one signal dot at the bottom.
 */
export function Brandmark({ large = false }: { large?: boolean }) {
  const rows = [4, 3, 2, 1];
  return (
    <span className={large ? "brandmark brandmark-lg" : "brandmark"} aria-hidden="true">
      {rows.flatMap((n, r) =>
        [0, 1, 2, 3].map((c) => (
          <i
            key={`${r}-${c}`}
            data-on={c < n ? "" : undefined}
            data-signal={r === 3 && c === 0 ? "" : undefined}
          />
        )),
      )}
    </span>
  );
}
