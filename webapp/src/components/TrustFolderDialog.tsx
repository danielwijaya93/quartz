import * as React from "react";
import { ShieldCheck } from "lucide-react";

export interface TrustFolderDialogProps {
  open: boolean;
  cwd: string;
  files: string[];
  onTrust: () => void;
  onDismiss: () => void;
}

/**
 * Native modal `<dialog>` asking once before loading `.quartz/helpers.py` or
 * `.quartz/mcp.json` in an unfamiliar folder.
 */
export function TrustFolderDialog({
  open,
  cwd,
  files,
  onTrust,
  onDismiss,
}: TrustFolderDialogProps): React.ReactElement {
  const headingId = React.useId();
  const dialogRef = React.useRef<HTMLDialogElement | null>(null);
  const dismissBtnRef = React.useRef<HTMLButtonElement | null>(null);
  const trustBtnRef = React.useRef<HTMLButtonElement | null>(null);

  React.useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    if (open) {
      if (!dialog.open) {
        if (typeof dialog.showModal === "function") {
          dialog.showModal();
        } else {
          dialog.setAttribute("open", "");
        }
      }
    } else if (dialog.open || dialog.hasAttribute("open")) {
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    }
  }, [open]);

  React.useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    const handleClose = () => {
      const choice = dialog.returnValue;
      dialog.returnValue = "";
      if (choice === "trust") {
        onTrust();
      } else {
        onDismiss();
      }
    };

    dialog.addEventListener("close", handleClose);
    return () => dialog.removeEventListener("close", handleClose);
  }, [onDismiss, onTrust]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLFormElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (document.activeElement === dismissBtnRef.current) {
        trustBtnRef.current?.focus();
      } else {
        dismissBtnRef.current?.focus();
      }
    }
  };

  const handleJsdomClick = (choice: "trust" | "dismiss") => {
    const dialog = dialogRef.current;
    if (!dialog || typeof dialog.showModal === "function") return;
    dialog.removeAttribute("open");
    if (choice === "trust") onTrust();
    else onDismiss();
  };

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={headingId}
      className="m-auto w-[min(32rem,calc(100vw-2rem))] rounded-2xl border border-border bg-surface p-6 text-foreground shadow-2xl backdrop:bg-black/50 backdrop:backdrop-blur-xs"
    >
      <div className="flex items-start gap-3">
        <div className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent/15 text-accent">
          <ShieldCheck aria-hidden="true" className="size-5" />
        </div>
        <div className="min-w-0 flex-1">
          <h2 id={headingId} className="text-base font-semibold">
            Trust this folder?
          </h2>
          <p className="mt-1 text-sm text-meta">
            This folder contains project configuration that runs code or starts MCP servers on your
            machine. Your choice will be saved for next time.
          </p>
        </div>
      </div>

      <div className="mt-4 rounded-xl border border-separator bg-surface-secondary/50 p-3">
        <p className="break-all font-mono text-xs text-foreground">{cwd}</p>
        {files.length > 0 ? (
          <ul className="mt-2 list-disc space-y-1 pl-4 font-mono text-xs text-meta">
            {files.map((file) => (
              <li key={file}>{file}</li>
            ))}
          </ul>
        ) : null}
      </div>

      <form method="dialog" onKeyDown={handleKeyDown} className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <button
          ref={dismissBtnRef}
          type="submit"
          value="dismiss"
          autoFocus
          onClick={() => handleJsdomClick("dismiss")}
          className="rounded-lg border border-border bg-surface-secondary px-4 py-2 text-sm font-medium text-foreground transition hover:bg-surface-secondary/80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
        >
          Keep untrusted for now
        </button>
        <button
          ref={trustBtnRef}
          type="submit"
          value="trust"
          onClick={() => handleJsdomClick("trust")}
          className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-foreground transition hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
        >
          Yes, trust this folder
        </button>
      </form>
    </dialog>
  );
}
