import * as React from "react";
import { Button, Input, Modal, Spinner, cn, useOverlayState } from "@heroui/react";
import { ArrowUp, ChevronRight, CornerDownLeft, Folder, FolderPlus, RefreshCw, Search } from "lucide-react";
import {
  choiceForCreated,
  choiceForListing,
  filterDirectories,
  highlightedEntry,
  indexOfEntry,
  parentPath,
  stepHighlight,
  type DirectoryListing,
  type PickerChoice,
} from "@/lib/folderPicker";
import { createDirectory, listDirectories } from "@/lib/sidebarApi";

export interface FolderPickerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the folder that was chosen. The caller decides what to do next. */
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
 *
 * Two things make it usable without a mouse. The filter keeps focus and the rows
 * are a real listbox underneath it: arrows move an `aria-activedescendant`
 * highlight, Enter opens the highlighted folder, Backspace on an empty filter goes
 * up. And the primary button stays off until something has been chosen, so the
 * dialog cannot start a session in whatever folder it happened to open on.
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
  const [choice, setChoice] = React.useState<PickerChoice | null>(null);
  const [active, setActive] = React.useState(-1);

  const visible = React.useMemo(
    () => (listing ? filterDirectories(listing.entries, query) : []),
    [listing, query],
  );

  const up = listing ? parentPath(listing) : null;
  const highlighted = highlightedEntry(visible, active);

  const filterRef = React.useRef<HTMLInputElement | null>(null);

  /**
   * The control that opened the dialog, so closing it puts the keyboard back
   * where it was. Captured while rendering, because by the time effects run the
   * overlay has already moved focus into itself -- and the overlay does not hand
   * focus back on its own.
   */
  const openerRef = React.useRef<HTMLElement | null>(null);
  const wasOpen = React.useRef(false);
  if (open && !wasOpen.current) {
    const focused = document.activeElement;
    openerRef.current =
      focused instanceof HTMLElement && focused !== document.body ? focused : null;
  }
  wasOpen.current = open;

  React.useEffect(() => {
    if (open) return;
    const opener = openerRef.current;
    openerRef.current = null;
    // A dialog that closes onto <body> loses a keyboard user's place in the page.
    if (opener !== null && opener.isConnected) opener.focus();
  }, [open]);

  const load = React.useCallback(async (path?: string) => {
    setLoading(true);
    setError(null);
    try {
      const next = await listDirectories(path);
      setListing(next);
      setQuery("");
      setActive(next.entries.length === 0 ? -1 : 0);
      // Opening a folder, or walking up out of one, *is* pointing at it. The
      // initial load is not: it is where the picker happened to land, and
      // treating that as a choice is the accident this dialog exists to avoid.
      setChoice(path === undefined ? null : choiceForListing(next));
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
    // Parking the keyboard in the filter is what makes the dialog usable without
    // a mouse: opening it is followed by typing a folder's name or arrowing into
    // the list, never by reaching for the pointer. Closing puts focus back on the
    // control that opened the picker -- see `openerRef` above.
    const frame = window.requestAnimationFrame(() => filterRef.current?.focus());
    setListing(null);
    setQuery("");
    setCreating(false);
    setFolderName("");
    setChoice(null);
    setActive(-1);
    void load();
    return () => window.cancelAnimationFrame(frame);
  }, [open, load]);

  const onFilterChange = (value: string): void => {
    setQuery(value);
    // Typing narrows the list under the highlight, so put it on the first row:
    // pressing Enter straight after typing a folder's name should open it.
    setActive(filterDirectories(listing?.entries ?? [], value).length === 0 ? -1 : 0);
  };

  const goUp = (): void => {
    if (up !== null && !loading) void load(up);
  };

  const chooseThisFolder = (): void => {
    if (listing !== null) setChoice(choiceForListing(listing));
  };

  const onFilterKeyDown = (event: React.KeyboardEvent<HTMLInputElement>): void => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((prev) => stepHighlight(prev, visible.length, 1));
      return;
    }
    if (event.key === "ArrowUp" && event.altKey) {
      event.preventDefault();
      goUp();
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((prev) => stepHighlight(prev, visible.length, -1));
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      if (highlighted !== null) void load(highlighted.path);
      return;
    }
    if (event.key === "Backspace" && query === "") {
      // An empty filter has nothing to delete, so Backspace means "leave this
      // folder" -- the same gesture as walking up a tree on the way in.
      event.preventDefault();
      goUp();
    }
  };

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
      setActive(Math.max(0, indexOfEntry(next.entries, name)));
      // The folder was made on purpose, so it is the destination -- not a row the
      // person then has to find and open.
      setChoice(choiceForCreated(next, name));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not create that folder");
    } finally {
      setBusy(false);
    }
  };

  const canChoose = choice !== null && listing !== null && listing.error === null && !busy;

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
                onPress={goUp}
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

            <div className="px-4 py-2">
              <div className="flex items-center gap-2 rounded-lg border border-border bg-surface-secondary/50 px-3 py-1.5 focus-within:border-focus focus-within:ring-2 focus-within:ring-focus/25">
                <Search aria-hidden="true" className="size-4 shrink-0 text-muted" />
                <Input
                  ref={filterRef}
                  value={query}
                  onChange={(event) => onFilterChange(event.target.value)}
                  onKeyDown={onFilterKeyDown}
                  placeholder="Filter folders…"
                  aria-label="Filter folders"
                  // A filter that drives a listbox is a combobox: it owns focus, and
                  // `aria-activedescendant` is what makes the highlighted row audible.
                  role="combobox"
                  aria-expanded="true"
                  aria-controls="folder-options"
                  aria-autocomplete="list"
                  aria-activedescendant={
                    highlighted === null || active < 0 ? undefined : `folder-option-${active}`
                  }
                  spellCheck={false}
                  autoComplete="off"
                  className="w-full border-0 bg-transparent px-0 shadow-none outline-none! ring-0! focus-visible:outline-none!"
                />
              </div>
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

              <ul id="folder-options" role="listbox" aria-label="Folders" className="space-y-0.5">
                {visible.map((entry, index) => {
                  const selected = index === active;
                  return (
                    <li
                      key={entry.name}
                      id={`folder-option-${index}`}
                      role="option"
                      aria-selected={selected}
                      data-selected={selected}
                      onMouseEnter={() => setActive(index)}
                      onClick={() => void load(entry.path)}
                      className={cn(
                        "flex min-h-10 cursor-pointer items-center gap-2 rounded-lg px-3 text-sm",
                        selected
                          ? "bg-accent-soft text-accent-soft-foreground"
                          : "text-foreground hover:bg-surface-secondary",
                      )}
                    >
                      <Folder aria-hidden="true" className="size-4 shrink-0 text-muted" />
                      <span className="truncate">{entry.name}</span>
                      {selected ? (
                        <CornerDownLeft
                          aria-hidden="true"
                          className="ml-auto size-3.5 shrink-0 self-center text-muted"
                        />
                      ) : null}
                    </li>
                  );
                })}
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
                  <p aria-live="polite" className="w-full text-xs">
                    <span className="text-muted">Destination </span>
                    {choice !== null ? (
                      <span className="font-mono break-all text-foreground">{choice}</span>
                    ) : (
                      <>
                        <span className="text-foreground">not chosen yet</span>
                        <span className="text-muted"> — this folder is </span>
                        <span className="font-mono break-all text-foreground">
                          {listing?.path || "…"}
                        </span>
                      </>
                    )}
                  </p>
                  <div className="flex w-full items-center justify-between gap-2">
                    <Button
                      size="sm"
                      variant="ghost"
                      onPress={() => setCreating(true)}
                      isDisabled={listing === null || listing.error !== null}
                      className="shrink-0"
                    >
                      <FolderPlus aria-hidden="true" className="size-3.5" />
                      New folder
                    </Button>
                    <div className="flex items-center gap-2">
                      {choice === null ? (
                        // The folder the dialog opened on is a real destination, so
                        // there is always a way to pick it without navigating away
                        // and back just to arm the primary action.
                        <Button
                          size="sm"
                          variant="outline"
                          isDisabled={listing === null || listing.error !== null || busy}
                          onPress={chooseThisFolder}
                        >
                          Use this folder
                        </Button>
                      ) : null}
                      <Button size="sm" variant="ghost" onPress={() => onOpenChange(false)}>
                        Cancel
                      </Button>
                      <Button
                        size="sm"
                        variant="primary"
                        isDisabled={!canChoose}
                        onPress={() => {
                          if (choice !== null) {
                            onChoose(choice);
                            onOpenChange(false);
                          }
                        }}
                      >
                        {chooseLabel}
                      </Button>
                    </div>
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