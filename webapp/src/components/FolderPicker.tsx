import * as React from "react";
import { Button, Chip, Input, Modal, Spinner, cn, useOverlayState } from "@heroui/react";
import { ArrowUp, ChevronRight, Folder, FolderPlus, RefreshCw } from "lucide-react";
import {
  currentName,
  filterDirectories,
  parentPath,
  type DirectoryListing,
} from "@/lib/folderPicker";
import { createDirectory, listDirectories } from "@/lib/sidebarApi";

export interface FolderPickerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the directory on screen. The caller decides what to do with it. */
  onChoose: (path: string) => void;
  /** Heading, so the same dialog can add a folder or start a session in one. */
  title: string;
  /** Label for the primary button, e.g. "Start here". */
  chooseLabel: string;
}

/**
 * Tap-through folder picker: breadcrumbs, a list of directories, and nothing else.
 *
 * There is no path text field, by design (`docs/web-protocol.md`): the only text a
 * person types here filters *this* directory's rows, and every path the dialog sends
 * was handed to it by the server. That is also why "up" is a breadcrumb and a
 * button rather than an editable prefix -- on a phone, the breadcrumb is the
 * explorer.
 */
export function FolderPicker({
  open,
  onOpenChange,
  onChoose,
  title,
  chooseLabel,
}: FolderPickerProps): React.ReactElement {
  const state = useOverlayState({ isOpen: open, onOpenChange });
  const [listing, setListing] = React.useState<DirectoryListing | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [query, setQuery] = React.useState("");
  const [creating, setCreating] = React.useState(false);
  const [folderName, setFolderName] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const load = React.useCallback(async (path?: string) => {
    setLoading(true);
    setError(null);
    try {
      const next = await listDirectories(path);
      setListing(next);
      setQuery("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not read that folder");
    } finally {
      setLoading(false);
    }
  }, []);

  // Opening always starts from the server's directory: a picker that remembered the
  // last folder would be one more piece of state to be wrong about, and the folder
  // you want is usually near where you started.
  React.useEffect(() => {
    if (!open) return;
    setListing(null);
    setQuery("");
    setCreating(false);
    setFolderName("");
    void load();
  }, [open, load]);

  const visible = React.useMemo(
    () => (listing ? filterDirectories(listing.entries, query) : []),
    [listing, query],
  );

  const up = listing ? parentPath(listing) : null;

  const submitNewFolder = async (event: React.FormEvent): Promise<void> => {
    event.preventDefault();
    const name = folderName.trim();
    if (!name || !listing) return;
    setBusy(true);
    setError(null);
    try {
      const next = await createDirectory(listing.path, name);
      if (next.error) {
        setError(next.error);
        return;
      }
      setListing(next);
      setQuery("");
      setFolderName("");
      setCreating(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not create that folder");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal state={state}>
      <Modal.Backdrop variant="blur">
        <Modal.Container size="md" scroll="inside">
          <Modal.Dialog className="p-0">
            <Modal.Header className="border-b border-separator px-4 py-3">
              <Modal.Heading className="text-base font-semibold">{title}</Modal.Heading>
            </Modal.Header>

            <div className="flex items-center gap-1 overflow-x-auto border-b border-separator px-3 py-2">
              <Button
                isIconOnly
                size="sm"
                variant="ghost"
                aria-label="Go up one folder"
                isDisabled={up === null || loading}
                onPress={() => {
                  if (up !== null) void load(up);
                }}
              >
                <ArrowUp aria-hidden="true" className="size-4" />
              </Button>
              <ol className="flex min-w-0 items-center gap-0.5" aria-label="Folder path">
                {(listing?.parents ?? []).map((crumb, index, all) => {
                  const current = index === all.length - 1;
                  return (
                    <li key={crumb.path} className="flex shrink-0 items-center gap-0.5">
                      {index > 0 ? (
                        <ChevronRight aria-hidden="true" className="size-3 shrink-0 text-muted" />
                      ) : null}
                      <button
                        type="button"
                        onClick={() => {
                          if (!current) void load(crumb.path);
                        }}
                        aria-current={current ? "page" : undefined}
                        aria-label={`Go to ${crumb.path}`}
                        className={cn(
                          "max-w-[8rem] cursor-pointer truncate rounded px-1 py-0.5 text-xs focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none",
                          current ? "font-semibold text-foreground" : "text-muted hover:text-foreground",
                        )}
                      >
                        {crumb.name}
                      </button>
                    </li>
                  );
                })}
              </ol>
            </div>

            <div className="flex items-center gap-2 px-4 py-2">
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Filter folders…"
                aria-label="Filter folders"
                spellCheck={false}
                autoComplete="off"
                className="border-0 bg-transparent px-0 shadow-none"
              />
            </div>

            {error !== null ? (
              <p role="alert" className="mx-4 mb-2 rounded-lg bg-danger-soft px-2 py-1.5 text-xs text-danger-soft-foreground">
                {error}
              </p>
            ) : null}

            {listing?.error ? (
              <p role="alert" className="mx-4 mb-2 rounded-lg bg-danger-soft px-2 py-1.5 text-xs text-danger-soft-foreground">
                {listing.error}
              </p>
            ) : null}

            <Modal.Body className="max-h-[45vh] min-h-[8rem] overflow-y-auto px-2 pb-2">
              {loading && listing === null ? (
                <p role="status" className="flex items-center gap-2 px-3 py-6 text-sm text-muted">
                  <Spinner size="sm" aria-label="Loading folders" />
                  Reading folders…
                </p>
              ) : null}

              {!loading && listing !== null && listing.error === null && visible.length === 0 ? (
                <p className="px-3 py-6 text-center text-sm text-muted">
                  {query.trim() === ""
                    ? "No folders here. Create one below."
                    : `No folder matches “${query.trim()}”.`}
                </p>
              ) : null}

              <ul className="space-y-0.5" aria-label="Folders">
                {visible.map((entry) => (
                  <li key={entry.name}>
                    <button
                      type="button"
                      onClick={() => void load(entry.path)}
                      className="flex min-h-10 w-full cursor-pointer items-center gap-2 rounded-lg px-3 text-left text-sm hover:bg-surface-secondary focus-visible:ring-2 focus-visible:ring-focus focus-visible:outline-none"
                    >
                      <Folder aria-hidden="true" className="size-4 shrink-0 text-muted" />
                      <span className="truncate">{entry.name}</span>
                    </button>
                  </li>
                ))}
              </ul>

              {error !== null && listing === null ? (
                <div className="px-3 py-4 text-center">
                  <Button size="sm" variant="outline" onPress={() => void load()}>
                    <RefreshCw aria-hidden="true" className="size-3.5" />
                    Retry
                  </Button>
                </div>
              ) : null}
            </Modal.Body>

            <Modal.Footer className="flex flex-wrap items-center gap-2 border-t border-separator px-4 py-3">
              {creating ? (
                <form className="flex min-w-0 flex-1 items-center gap-1" onSubmit={(event) => void submitNewFolder(event)}>
                  <Input
                    value={folderName}
                    onChange={(event) => setFolderName(event.target.value)}
                    aria-label="New folder name"
                    placeholder="New folder name"
                    spellCheck={false}
                    autoComplete="off"
                    autoFocus
                  />
                  <Button size="sm" variant="primary" type="submit" isDisabled={busy || folderName.trim() === ""}>
                    Create
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onPress={() => {
                      setCreating(false);
                      setFolderName("");
                    }}
                  >
                    Cancel
                  </Button>
                </form>
              ) : (
                <>
                  <Button
                    size="sm"
                    variant="ghost"
                    onPress={() => setCreating(true)}
                    isDisabled={listing === null || listing.error !== null}
                  >
                    <FolderPlus aria-hidden="true" className="size-3.5" />
                    New folder
                  </Button>
                  <div className="ml-auto flex items-center gap-2">
                    <Chip size="sm" variant="soft" color="default" className="hidden sm:inline-flex">
                      <span className="max-w-[12rem] truncate">{listing ? currentName(listing) : "…"}</span>
                    </Chip>
                    <Button size="sm" variant="ghost" onPress={() => onOpenChange(false)}>
                      Cancel
                    </Button>
                    <Button
                      size="sm"
                      variant="primary"
                      isDisabled={listing === null || listing.error !== null || busy}
                      onPress={() => {
                        if (listing) {
                          onChoose(listing.path);
                          onOpenChange(false);
                        }
                      }}
                    >
                      {chooseLabel}
                    </Button>
                  </div>
                </>
              )}
            </Modal.Footer>
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>
    </Modal>
  );
}
