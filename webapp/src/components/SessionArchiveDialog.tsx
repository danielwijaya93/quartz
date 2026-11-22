import * as React from "react";
import { Button, Input, Modal, Spinner, useOverlayState } from "@heroui/react";
import { ArchiveRestore, FolderOpen, Import, RefreshCw, Search } from "lucide-react";
import {
  ARCHIVE_REASONS,
  archiveEntries,
  archiveReasonCounts,
  archiveReasonLabel,
  displayTitle,
  filterArchiveEntries,
  formatAge,
  groupArchive,
  isArchiveFilterActive,
  NO_ARCHIVE_FILTER,
  type ArchiveFilter,
  type ArchiveReason,
  type SidebarPayload,
} from "@/lib/sidebar";

export interface SessionArchiveDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The last good payload, or null before the first load succeeds. */
  data: SidebarPayload | null;
  loading: boolean;
  /** A fetch failure: shown instead of the list, with a retry. */
  error: string | null;
  /** A failed import/restore, from the same store the sidebar reads. */
  actionError: string | null;
  onReload: () => void;
  onImport: (sessionId: string) => Promise<boolean>;
  onRestore: (sessionId: string) => Promise<boolean>;
  /**
   * The control that opened the dialog.
   *
   * `App` records it when the open is requested, because by the time the overlay
   * mounts, its own focus scope has already moved the keyboard inside the dialog.
   */
  returnFocusTo?: React.RefObject<HTMLElement | null>;
}

/**
 * "Not in sidebar": the other half of archiving, and the only view of a dead folder.
 *
 * Hiding a session is not deleting it, so there has to be somewhere the hidden ones
 * can be found again -- and the same list is where terminal sessions are imported
 * from, because "the sidebar does not show it" and "it was never imported" look
 * identical to a person. One row per session, one button that does what is needed
 * for that row: Restore for a hidden one, Import for one that was never listed.
 *
 * A session whose folder has been removed joins the same list. The sidebar drops
 * those folders outright (they cannot start or hold a session), so without this
 * place a transcript in a deleted worktree would be unreachable from the browser.
 *
 * At ~150 rows that list stops being a list and becomes a wall, so it is filtered
 * and grouped rather than merely rendered: text matches the title and the folder,
 * the reason a row is here is a filter chip with a count instead of a badge
 * repeated on every row, and rows arrive under their folder so the eye has
 * something to scan.
 */
export function SessionArchiveDialog({
  open,
  onOpenChange,
  data,
  loading,
  error,
  actionError,
  onReload,
  onImport,
  onRestore,
  returnFocusTo,
}: SessionArchiveDialogProps): React.ReactElement {
  const state = useOverlayState({ isOpen: open, onOpenChange });
  const [busyId, setBusyId] = React.useState<string | null>(null);
  const [filter, setFilter] = React.useState<ArchiveFilter>(NO_ARCHIVE_FILTER);
  const headingId = React.useId();

  const entries = React.useMemo(() => (data ? archiveEntries(data) : []), [data]);
  const counts = React.useMemo(() => archiveReasonCounts(entries), [entries]);
  const shown = React.useMemo(
    () => (data ? filterArchiveEntries(entries, filter, data.sidebar) : []),
    [entries, filter, data],
  );
  const groups = React.useMemo(() => groupArchive(shown), [shown]);
  const failure = actionError ?? error;

  // Opening always starts unfiltered. The row a person wants is usually the one
  // they just lost, and a filter remembered from last time is exactly what would
  // hide it.
  React.useEffect(() => {
    if (open) setFilter(NO_ARCHIVE_FILTER);
  }, [open]);

  // Give focus back to the control that opened the dialog. It has to wait for the
  // overlay to be gone: focusing while the dialog still owns the keyboard is undone
  // when its portal is removed, which leaves focus on `<body>` and the next Tab
  // starting from the top of the page.
  React.useEffect(() => {
    if (open) return;
    const opener = returnFocusTo?.current ?? null;
    if (opener === null || !opener.isConnected) return;
    const timer = window.setTimeout(() => opener.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [open, returnFocusTo]);

  const act = async (sessionId: string, hidden: boolean, needsImport: boolean): Promise<void> => {
    setBusyId(sessionId);
    try {
      // Both halves can be needed: restoring a hidden terminal session without
      // importing it would leave it just as invisible.
      if (needsImport) await onImport(sessionId);
      if (hidden) await onRestore(sessionId);
    } finally {
      setBusyId(null);
    }
  };

  const toggleReason = (reason: ArchiveReason): void => {
    setFilter((previous) => ({
      ...previous,
      reasons: previous.reasons.includes(reason)
        ? previous.reasons.filter((item) => item !== reason)
        : [...previous.reasons, reason],
    }));
  };

  const noMatchCopy =
    filter.query.trim() === ""
      ? "No session matches this filter."
      : `No session matches “${filter.query.trim()}”.`;

  return (
    <Modal state={state}>
      <Modal.Backdrop variant="blur">
        <Modal.Container size="md" scroll="inside">
          <Modal.Dialog className="p-0">
            <Modal.Header className="border-b border-separator px-4 py-3">
              <Modal.Heading className="text-base font-semibold">Not in sidebar</Modal.Heading>
            </Modal.Header>

            <p className="px-4 pt-3 text-xs text-muted">
              Hidden sessions, sessions started elsewhere, and sessions whose folder has been
              removed. Hiding one never deletes anything: the transcript stays on disk and can come
              back from here.
            </p>

            {failure !== null ? (
              <p
                role="alert"
                className="mx-4 mt-2 rounded-lg bg-danger-soft px-2 py-1.5 text-xs text-danger-soft-foreground"
              >
                {failure}
              </p>
            ) : null}

            {data === null ? null : (
              <>
                {/* Same interaction as the folder picker's: one text field over the
                    list, focused on open, and a way back to the full list. */}
                <div className="flex items-center gap-2 px-4 py-2">
                  <div className="flex flex-1 items-center gap-2 rounded-lg border border-border bg-surface-secondary/50 px-3 py-1.5 focus-within:border-focus focus-within:ring-2 focus-within:ring-focus/25">
                    <Search aria-hidden="true" className="size-4 shrink-0 text-muted" />
                    <Input
                      value={filter.query}
                      onChange={(event) =>
                        setFilter((previous) => ({ ...previous, query: event.target.value }))
                      }
                      placeholder="Filter sessions…"
                      aria-label="Filter sessions"
                      spellCheck={false}
                      autoComplete="off"
                      autoFocus
                      className="quartz-bare-input w-full border-0 bg-transparent px-0 shadow-none"
                    />
                  </div>
                  {isArchiveFilterActive(filter) ? (
                    <Button size="sm" variant="ghost" onPress={() => setFilter(NO_ARCHIVE_FILTER)}>
                      Clear
                    </Button>
                  ) : null}
                </div>

                <div className="flex flex-wrap items-center gap-1.5 px-4 pb-2">
                  {ARCHIVE_REASONS.map((reason) => {
                    const active = filter.reasons.includes(reason);
                    const count = counts[reason];
                    return (
                      <Button
                        key={reason}
                        size="sm"
                        variant={active ? "primary" : "outline"}
                        aria-pressed={active}
                        // A reason nobody has is not a filter, it is a dead end.
                        isDisabled={count === 0 && !active}
                        onPress={() => toggleReason(reason)}
                        className="text-[11px]"
                      >
                        {archiveReasonLabel(reason)}
                        <span className="tabular-nums opacity-70">{count}</span>
                      </Button>
                    );
                  })}
                  <p aria-live="polite" className="ml-auto shrink-0 text-[11px] text-meta tabular-nums">
                    {shown.length === entries.length
                      ? `${entries.length} ${entries.length === 1 ? "session" : "sessions"}`
                      : `${shown.length} of ${entries.length}`}
                  </p>
                </div>
              </>
            )}

            <Modal.Body className="max-h-[50vh] min-h-[6rem] overflow-y-auto px-2 py-2">
              {data === null && loading ? (
                <p role="status" className="flex items-center gap-2 px-3 py-6 text-sm text-muted">
                  <Spinner size="sm" aria-label="Loading sessions" />
                  Loading sessions…
                </p>
              ) : data === null ? (
                <div className="px-3 py-6 text-center">
                  <Button size="sm" variant="outline" onPress={onReload}>
                    <RefreshCw aria-hidden="true" className="size-3.5" />
                    Retry
                  </Button>
                </div>
              ) : entries.length === 0 ? (
                <p className="px-3 py-6 text-center text-sm text-muted">
                  Every session is already in the sidebar.
                </p>
              ) : shown.length === 0 ? (
                <p className="px-3 py-6 text-center text-sm text-muted">{noMatchCopy}</p>
              ) : (
                <div role="group" aria-label="Sessions not in the sidebar" className="space-y-3">
                  {groups.map((group, index) => {
                    const id = `${headingId}-${index}`;
                    return (
                      <section key={group.cwd} aria-labelledby={id}>
                        <h3
                          id={id}
                          className="flex items-baseline gap-1.5 px-2 pb-1 text-[11px] font-semibold tracking-wider text-meta uppercase"
                        >
                          <FolderOpen aria-hidden="true" className="size-3 shrink-0 self-center" />
                          <span className="truncate">{group.label}</span>
                          <span className="tabular-nums">{group.entries.length}</span>
                          {group.folderMissing ? (
                            // Said once per folder rather than on every row in it.
                            <span className="ml-auto shrink-0 tracking-normal normal-case">
                              folder removed
                            </span>
                          ) : null}
                        </h3>
                        <ul className="space-y-1">
                          {group.entries.map(({ session, hidden, needsImport, missingFolder }) => {
                            const title = displayTitle(session, data.sidebar);
                            const age = formatAge(session.updatedAt);
                            return (
                              <li
                                key={session.sessionId}
                                className="flex items-center justify-between gap-3 rounded-xl border border-border px-3 py-2"
                              >
                                <div className="min-w-0">
                                  <div className="truncate text-sm font-medium text-foreground">
                                    {title}
                                  </div>
                                  <div className="truncate text-[11px] text-meta">
                                    {session.origin === "web" ? "web" : "terminal"}
                                    {age ? ` · ${age}` : ""}
                                  </div>
                                </div>
                                <div className="flex shrink-0 items-center gap-1.5">
                                  {/* No Import/Restore for a gone folder: both change a
                                      list that still cannot draw the session, so the
                                      button would be a click with nothing to show for
                                      it. */}
                                  {!missingFolder && (needsImport || hidden) ? (
                                    <Button
                                      size="sm"
                                      variant="outline"
                                      isDisabled={busyId !== null}
                                      onPress={() => void act(session.sessionId, hidden, needsImport)}
                                      aria-label={
                                        hidden
                                          ? `Restore session ${title}`
                                          : `Import session ${title}`
                                      }
                                    >
                                      {hidden ? (
                                        <ArchiveRestore aria-hidden="true" className="size-3.5" />
                                      ) : (
                                        <Import aria-hidden="true" className="size-3.5" />
                                      )}
                                      {hidden ? "Restore" : "Import"}
                                    </Button>
                                  ) : null}
                                </div>
                              </li>
                            );
                          })}
                        </ul>
                      </section>
                    );
                  })}
                </div>
              )}
            </Modal.Body>

            <Modal.Footer className="flex items-center justify-end gap-2 border-t border-separator px-4 py-3">
              <Button size="sm" variant="ghost" onPress={onReload}>
                <RefreshCw aria-hidden="true" className="size-3.5" />
                Refresh
              </Button>
              <Button size="sm" variant="primary" onPress={() => onOpenChange(false)}>
                Done
              </Button>
            </Modal.Footer>
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>
    </Modal>
  );
}
