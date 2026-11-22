import * as React from "react";
import { Button, Chip, Modal, Spinner, useOverlayState } from "@heroui/react";
import { ArchiveRestore, Import, RefreshCw } from "lucide-react";
import { archiveEntries, displayTitle, formatAge, type SidebarPayload } from "@/lib/sidebar";
import { directoryLabel } from "@/lib/sidebar";

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
}

/**
 * "Not in sidebar": the other half of archiving.
 *
 * Hiding a session is not deleting it, so there has to be somewhere the hidden ones
 * can be found again -- and the same list is where terminal sessions are imported
 * from, because "the sidebar does not show it" and "it was never imported" look
 * identical to a person. One row per session, one button that does what is needed
 * for that row: Restore for a hidden one, Import for one that was never listed.
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
}: SessionArchiveDialogProps): React.ReactElement {
  const state = useOverlayState({ isOpen: open, onOpenChange });
  const [busyId, setBusyId] = React.useState<string | null>(null);

  const entries = React.useMemo(() => (data ? archiveEntries(data) : []), [data]);
  const failure = actionError ?? error;

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

  return (
    <Modal state={state}>
      <Modal.Backdrop variant="blur">
        <Modal.Container size="md" scroll="inside">
          <Modal.Dialog className="p-0">
            <Modal.Header className="border-b border-separator px-4 py-3">
              <Modal.Heading className="text-base font-semibold">Not in sidebar</Modal.Heading>
            </Modal.Header>

            <p className="px-4 pt-3 text-xs text-muted">
              Hidden sessions and sessions started elsewhere. Hiding one never deletes anything: the
              transcript stays on disk and can come back from here.
            </p>

            {failure !== null ? (
              <p
                role="alert"
                className="mx-4 mt-2 rounded-lg bg-danger-soft px-2 py-1.5 text-xs text-danger-soft-foreground"
              >
                {failure}
              </p>
            ) : null}

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
              ) : (
                <ul className="space-y-1.5" aria-label="Sessions not in the sidebar">
                  {entries.map(({ session, hidden, needsImport }) => (
                    <li
                      key={session.sessionId}
                      className="flex items-center justify-between gap-3 rounded-xl border border-border px-3 py-2"
                    >
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium text-foreground">
                          {displayTitle(session, data.sidebar)}
                        </div>
                        <div className="truncate text-[11px] text-muted">
                          {directoryLabel(session.cwd)} · {session.origin === "web" ? "web" : "terminal"}
                          {formatAge(session.updatedAt) ? ` · ${formatAge(session.updatedAt)}` : ""}
                        </div>
                      </div>
                      <div className="flex shrink-0 items-center gap-1.5">
                        {hidden ? (
                          <Chip size="sm" variant="soft" color="default">
                            hidden
                          </Chip>
                        ) : null}
                        <Button
                          size="sm"
                          variant="outline"
                          isDisabled={busyId !== null}
                          onPress={() => void act(session.sessionId, hidden, needsImport)}
                          aria-label={
                            hidden
                              ? `Restore session ${displayTitle(session, data.sidebar)}`
                              : `Import session ${displayTitle(session, data.sidebar)}`
                          }
                        >
                          {hidden ? (
                            <ArchiveRestore aria-hidden="true" className="size-3.5" />
                          ) : (
                            <Import aria-hidden="true" className="size-3.5" />
                          )}
                          {hidden ? "Restore" : "Import"}
                        </Button>
                      </div>
                    </li>
                  ))}
                </ul>
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
