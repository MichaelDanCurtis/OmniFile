import { useId, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { ApiError } from "../../api/client";
import { useChildren, useShare } from "../../api/queries";
import type { AsOfEntry, DirHeader, Entry, Share } from "../../api/types";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { Callout, Empty, ErrorCallout, Loading } from "../../components/Feedback";
import {
  ClockIcon,
  FileIcon,
  FolderIcon,
  LockIcon,
  PinIcon,
  RecallIcon,
  RewindIcon,
  ShieldIcon,
} from "../../components/icons";
import { TierChip } from "../../components/TierChip";
import {
  addDays,
  formatBytes,
  formatDateTime,
  fromLocalInput,
  pluralize,
  toLocalInput,
} from "../../lib/format";
import { canEditOpenPolicy, canKeepOnline, canPin, isArchiveAdmin } from "../../lib/permissions";
import { EntryTags, NextStep, StateLabel, needsRecall } from "./EntryBits";
import { FileDrawer } from "./FileDrawer";
import { KeepOnlineDialog, LegalHoldDialog } from "./HoldDialogs";
import { PinDialog } from "./PinDialog";
import { RecallDialog } from "./RecallDialog";

type Dialog = "recall" | "pin" | "keep" | "legal" | null;

function isLive(item: Entry | AsOfEntry): item is Entry {
  return "state" in item;
}

export function FileBrowserPage() {
  const { shareId = "", dirId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const asOf = params.get("as_of");
  const openFile = params.get("file");
  const share = useShare(shareId);
  const children = useChildren(dirId, asOf);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [rewinding, setRewinding] = useState(!!asOf);
  const [dialog, setDialog] = useState<Dialog>(null);
  const { me } = useSession();

  const pages = children.data?.pages;
  const dir = pages?.[0]?.dir;
  const items = useMemo(() => (pages ?? []).flatMap((p) => p.items), [pages]);
  usePageTitle(dir?.name ?? "Files");

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value === null) next.delete(key);
    else next.set(key, value);
    setParams(next);
  };

  if (share.isPending) return <Loading />;
  if (share.isError) {
    return (
      <div className="page">
        <ErrorCallout error={share.error} />
      </div>
    );
  }

  const selectedEntries = items.filter((i): i is Entry => isLive(i) && selected.has(i.file_id));
  const targets = selectedEntries.map((e) => ({ file_id: e.file_id, name: e.name }));
  const one = selectedEntries.length === 1 ? selectedEntries[0] : undefined;

  return (
    <div className="page">
      <Crumbs share={share.data} dir={dir} asOf={asOf} />
      <div className="page-head">
        <div>
          <h1>{dir?.name ?? share.data.name}</h1>
          <p className="page-question">Where is each file, and is it ready to open?</p>
          <p className="lede">
            {share.data.dataset_name} ·{" "}
            {asOf
              ? `as it was on ${formatDateTime(asOf)}`
              : "live view, filtered to what your AD groups can read"}
          </p>
        </div>
        <div className="actions">
          <button
            type="button"
            className="btn"
            aria-pressed={rewinding}
            onClick={() => {
              if (rewinding) {
                setRewinding(false);
                setParam("as_of", null);
              } else {
                setRewinding(true);
                setSelected(new Set());
              }
            }}
          >
            <RewindIcon /> {rewinding ? "Back to now" : "Rewind"}
          </button>
          {canEditOpenPolicy(me, share.data) ? (
            <Link className="btn" to={`/shares/${share.data.share_id}`}>
              Share settings
            </Link>
          ) : null}
        </div>
      </div>

      {rewinding ? (
        <RewindBar
          share={share.data}
          asOf={asOf}
          onChange={(value) => {
            setSelected(new Set());
            setParam("as_of", value);
          }}
        />
      ) : null}

      <div className="browser" data-drawer={openFile ? "open" : "closed"}>
        <div className="stack" style={{ gap: 0 }}>
          {children.isPending ? (
            <Loading label="Reading the folder…" />
          ) : children.isError ? (
            children.error instanceof ApiError && children.error.code === "beyond_rewind_depth" ? (
              <Callout tone="warn" title="That's further back than this share keeps" role="alert">
                {children.error.problem?.detail} Choose a later date.
              </Callout>
            ) : (
              <ErrorCallout error={children.error} />
            )
          ) : items.length === 0 ? (
            <div className="table-wrap">
              <Empty title={asOf ? "This folder was empty then" : "This folder is empty"}>
                <p>Or nothing in it is readable by your AD groups.</p>
              </Empty>
            </div>
          ) : asOf ? (
            <AsOfTable
              items={items.filter((i): i is AsOfEntry => !isLive(i))}
              shareId={shareId}
              onOpen={(id) => setParam("file", id)}
            />
          ) : (
            <LiveTable
              items={items.filter(isLive)}
              shareId={shareId}
              selected={selected}
              setSelected={setSelected}
              onOpen={(id) => setParam("file", id)}
              openFile={openFile}
            />
          )}
          {children.hasNextPage ? (
            <div className="table-foot">
              <button
                type="button"
                className="btn btn-sm"
                disabled={children.isFetchingNextPage}
                onClick={() => void children.fetchNextPage()}
              >
                {children.isFetchingNextPage ? "Loading…" : "Load more"}
              </button>
            </div>
          ) : null}
          {selectedEntries.length > 0 ? (
            <SelectionBar
              count={selectedEntries.length}
              share={share.data}
              canRecall={!!one && needsRecall(one) && !one.active_recall}
              onAction={setDialog}
              onClear={() => setSelected(new Set())}
            />
          ) : null}
        </div>
        {openFile ? (
          <FileDrawer
            key={openFile}
            fileId={openFile}
            share={share.data}
            onClose={() => {
              setParam("file", null);
              // Give focus back to the row the drawer was opened from.
              const id = openFile;
              setTimeout(() => {
                document
                  .querySelector<HTMLElement>(`[data-open-file="${CSS.escape(id)}"]`)
                  ?.focus();
              }, 0);
            }}
          />
        ) : null}
      </div>

      {dialog === "recall" && one ? (
        <RecallDialog
          open
          onClose={() => setDialog(null)}
          fileId={one.file_id}
          fileName={one.name}
        />
      ) : null}
      {dialog === "pin" ? (
        <PinDialog open onClose={() => setDialog(null)} items={targets} share={share.data} />
      ) : null}
      {dialog === "keep" ? (
        <KeepOnlineDialog open onClose={() => setDialog(null)} items={targets} />
      ) : null}
      {dialog === "legal" ? (
        <LegalHoldDialog open onClose={() => setDialog(null)} items={targets} />
      ) : null}
    </div>
  );
}

function Crumbs({
  share,
  dir,
  asOf,
}: {
  share: Share;
  dir: DirHeader | undefined;
  asOf: string | null;
}) {
  const suffix = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
  const path = dir?.path ?? [];
  return (
    <nav aria-label="Breadcrumb">
      <ol className="crumbs">
        <li>
          <Link to="/files">Files</Link>
        </li>
        {path.map((seg, i) => {
          const last = i === path.length - 1;
          if (!seg.file_id) {
            return (
              <li key={`hidden-${i}`}>
                <span className="muted" title="A folder you can't read">
                  <LockIcon size={12} /> …
                </span>
              </li>
            );
          }
          return (
            <li key={seg.file_id}>
              {last ? (
                <span aria-current="page">{seg.name}</span>
              ) : (
                <Link to={`/files/${share.share_id}/${seg.file_id}${suffix}`}>{seg.name}</Link>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

function RewindBar({
  share,
  asOf,
  onChange,
}: {
  share: Share;
  asOf: string | null;
  onChange: (value: string | null) => void;
}) {
  const id = useId();
  const now = new Date();
  const earliest = addDays(now, -share.rewind.depth_days);
  const [value, setValue] = useState(
    asOf ? toLocalInput(new Date(asOf)) : toLocalInput(addDays(now, -1)),
  );
  const iso = fromLocalInput(value);
  const outOfRange =
    !!iso &&
    (new Date(iso).getTime() < earliest.getTime() || new Date(iso).getTime() > now.getTime());
  return (
    <form
      className="rewind-bar"
      onSubmit={(e) => {
        e.preventDefault();
        if (iso && !outOfRange) onChange(iso);
      }}
    >
      <RewindIcon />
      <label htmlFor={id}>
        <strong>Show this folder as it was on</strong>
      </label>
      <input
        id={id}
        type="datetime-local"
        value={value}
        min={toLocalInput(earliest)}
        max={toLocalInput(now)}
        aria-invalid={outOfRange}
        aria-describedby={`${id}-hint`}
        onChange={(e) => setValue(e.target.value)}
      />
      <button type="submit" className="btn btn-sm btn-primary" disabled={!iso || outOfRange}>
        Show
      </button>
      <span id={`${id}-hint`} className={outOfRange ? "field-error" : "field-hint"}>
        This share keeps {pluralize(share.rewind.depth_days, "day")} of history (back to{" "}
        {formatDateTime(earliest.toISOString())}).
      </span>
    </form>
  );
}

function LiveTable({
  items,
  shareId,
  selected,
  setSelected,
  onOpen,
  openFile,
}: {
  items: Entry[];
  shareId: string;
  selected: Set<string>;
  setSelected: (s: Set<string>) => void;
  onOpen: (id: string) => void;
  openFile: string | null;
}) {
  const all = items.length > 0 && items.every((i) => selected.has(i.file_id));
  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  };
  return (
    <div className="table-wrap">
      <table className="table" aria-label="Folder contents">
        <thead>
          <tr>
            <th className="col-check">
              <input
                type="checkbox"
                aria-label="Select all"
                checked={all}
                onChange={() => setSelected(all ? new Set() : new Set(items.map((i) => i.file_id)))}
              />
            </th>
            <th scope="col" className="col-name">
              Name
            </th>
            <th scope="col" className="col-num">
              Size
            </th>
            <th scope="col">Tier</th>
            <th scope="col">Where</th>
            <th scope="col" className="hide-narrow hide-with-drawer">
              Next
            </th>
            <th scope="col">Pins and holds</th>
          </tr>
        </thead>
        <tbody>
          {items.map((entry) => (
            <tr
              key={entry.file_id}
              aria-selected={selected.has(entry.file_id) || openFile === entry.file_id}
            >
              <td className="col-check">
                <input
                  type="checkbox"
                  aria-label={`Select ${entry.name}`}
                  checked={selected.has(entry.file_id)}
                  onChange={() => toggle(entry.file_id)}
                />
              </td>
              <td>
                <span className="cell-name">
                  {entry.kind === "dir" ? <FolderIcon /> : <FileIcon />}
                  {entry.kind === "dir" ? (
                    <Link to={`/files/${shareId}/${entry.file_id}`}>{entry.name}</Link>
                  ) : (
                    <button
                      type="button"
                      data-open-file={entry.file_id}
                      onClick={() => onOpen(entry.file_id)}
                    >
                      {entry.name}
                    </button>
                  )}
                </span>
              </td>
              <td className="col-num num">{entry.kind === "dir" ? "" : formatBytes(entry.size)}</td>
              <td>{entry.kind === "dir" ? null : <TierChip tier={entry.tier} />}</td>
              <td>
                <StateLabel entry={entry} />
              </td>
              <td className="hide-narrow hide-with-drawer">
                <NextStep entry={entry} />
              </td>
              <td>
                <EntryTags entry={entry} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AsOfTable({
  items,
  shareId,
  onOpen,
}: {
  items: AsOfEntry[];
  shareId: string;
  onOpen: (id: string) => void;
}) {
  return (
    <div className="table-wrap">
      <table className="table" aria-label="Folder contents as of the chosen time">
        <thead>
          <tr>
            <th scope="col">Name</th>
            <th scope="col" className="col-num">
              Size then
            </th>
            <th scope="col">Tier of that version</th>
            <th scope="col">Version from</th>
            <th scope="col">Since then</th>
          </tr>
        </thead>
        <tbody>
          {items.map((row) => (
            <tr key={row.file_id}>
              <td>
                <span className="cell-name">
                  {row.kind === "dir" ? <FolderIcon /> : <FileIcon />}
                  {row.kind === "dir" ? (
                    <Link to={`/files/${shareId}/${row.file_id}`}>{row.name}</Link>
                  ) : (
                    <button
                      type="button"
                      data-open-file={row.file_id}
                      onClick={() => onOpen(row.file_id)}
                    >
                      {row.name}
                    </button>
                  )}
                </span>
              </td>
              <td className="col-num num">{row.kind === "dir" ? "" : formatBytes(row.size)}</td>
              <td>{row.tier ? <TierChip tier={row.tier} /> : <span className="muted">—</span>}</td>
              <td className="num">{formatDateTime(row.committed_at)}</td>
              <td>
                {!row.live ? (
                  <span className="badge badge-danger">Deleted since</span>
                ) : row.current ? (
                  <span className="badge">Unchanged</span>
                ) : (
                  <span className="badge badge-warn">Changed since</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SelectionBar({
  count,
  share,
  canRecall,
  onAction,
  onClear,
}: {
  count: number;
  share: Share;
  canRecall: boolean;
  onAction: (d: Dialog) => void;
  onClear: () => void;
}) {
  const { me } = useSession();
  return (
    <div className="selection-bar" role="region" aria-label="Selection actions">
      <strong>{pluralize(count, "item")} selected</strong>
      <span className="spacer" />
      {count === 1 ? (
        <button
          type="button"
          className="btn btn-sm"
          disabled={!canRecall}
          onClick={() => onAction("recall")}
        >
          <RecallIcon /> Recall…
        </button>
      ) : null}
      {canPin(me, share) ? (
        <button type="button" className="btn btn-sm" onClick={() => onAction("pin")}>
          <PinIcon /> Pin to a tier…
        </button>
      ) : null}
      {canKeepOnline(me, share) ? (
        <button type="button" className="btn btn-sm" onClick={() => onAction("keep")}>
          <ClockIcon /> Keep online…
        </button>
      ) : null}
      {isArchiveAdmin(me) ? (
        <button type="button" className="btn btn-sm" onClick={() => onAction("legal")}>
          <ShieldIcon /> Legal hold…
        </button>
      ) : null}
      <button type="button" className="btn btn-sm" onClick={onClear}>
        Clear
      </button>
    </div>
  );
}
