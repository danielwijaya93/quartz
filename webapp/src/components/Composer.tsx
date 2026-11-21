import * as React from "react";
import { ArrowUp, CornerDownRight, ImagePlus, Square, X } from "lucide-react";
import { Button } from "./ui/button";
import { Textarea } from "./ui/textarea";
import { cn } from "@/lib/utils";
import type { ConnectionStatus } from "@/lib/connection";
import type { ImageAttachment } from "@/protocol";

const MAX_TEXTAREA_PX = 200;

export interface ComposerProps {
  connection: ConnectionStatus;
  turnRunning: boolean;
  /** Latched fatal error blocks submission, matching the server's stance. */
  fatalError: string | null;
  onSubmit: (text: string, images: ImageAttachment[]) => void;
  onSteer: (text: string) => void;
  onCancel: () => void;
  onOpenPalette: () => void;
}

/** Read a File as base64 without the `data:` prefix the protocol expects. */
async function readAsAttachment(file: File): Promise<ImageAttachment> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsDataURL(file);
  });
  const comma = dataUrl.indexOf(",");
  return {
    mimeType: file.type || "image/png",
    data: comma === -1 ? dataUrl : dataUrl.slice(comma + 1),
  };
}

/**
 * The input.
 *
 * One textarea, one button, and a rule: while a turn is running the same Enter key
 * means "steer" instead of "prompt", mirroring the TUI. That is the whole
 * interaction model — no mode switch to learn.
 */
export function Composer({
  connection,
  turnRunning,
  fatalError,
  onSubmit,
  onSteer,
  onCancel,
  onOpenPalette,
}: ComposerProps): React.ReactElement {
  const [text, setText] = React.useState("");
  const [images, setImages] = React.useState<ImageAttachment[]>([]);
  const [history, setHistory] = React.useState<string[]>([]);
  const [historyIndex, setHistoryIndex] = React.useState<number | null>(null);
  const textareaRef = React.useRef<HTMLTextAreaElement | null>(null);
  const fileRef = React.useRef<HTMLInputElement | null>(null);

  const offline = connection !== "connected";
  const blocked = fatalError !== null;
  const disabled = offline || blocked;
  const canSend = !disabled && (text.trim() !== "" || images.length > 0);

  const resize = React.useCallback(() => {
    const node = textareaRef.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, MAX_TEXTAREA_PX)}px`;
  }, []);

  React.useEffect(() => {
    resize();
  }, [text, resize]);

  // The composer owns focus: this is a keyboard tool, and losing focus after a
  // submit is the single most common papercut in chat UIs.
  React.useEffect(() => {
    if (!disabled) textareaRef.current?.focus();
  }, [disabled, turnRunning]);

  const submit = React.useCallback(() => {
    const value = text.trim();
    if (value === "" && images.length === 0) return;
    if (turnRunning) {
      if (value === "") return;
      onSteer(value);
    } else {
      onSubmit(value, images);
      setHistory((prev) => [...prev, value].filter((entry) => entry !== ""));
    }
    setText("");
    setImages([]);
    setHistoryIndex(null);
  }, [images, onSteer, onSubmit, text, turnRunning]);

  const onKeyDown = React.useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        submit();
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        if (turnRunning) onCancel();
        else if (text !== "") setText("");
        return;
      }
      if (event.key === "/" && text === "") {
        // Let `/` seed the palette: it is the command affordance, and typing it
        // into the prompt first would make the palette feel like a second mode.
        event.preventDefault();
        onOpenPalette();
        return;
      }
      if (event.key === "ArrowUp" && text === "" && history.length > 0) {
        event.preventDefault();
        const nextIndex =
          historyIndex === null ? history.length - 1 : Math.max(0, historyIndex - 1);
        setHistoryIndex(nextIndex);
        setText(history[nextIndex] ?? "");
        return;
      }
      if (event.key === "ArrowDown" && historyIndex !== null) {
        event.preventDefault();
        const nextIndex = historyIndex + 1;
        if (nextIndex >= history.length) {
          setHistoryIndex(null);
          setText("");
        } else {
          setHistoryIndex(nextIndex);
          setText(history[nextIndex] ?? "");
        }
      }
    },
    [history, historyIndex, onCancel, onOpenPalette, submit, text, turnRunning],
  );

  const addFiles = React.useCallback((files: FileList | null) => {
    if (!files || files.length === 0) return;
    const imageFiles = Array.from(files).filter((file) => file.type.startsWith("image/"));
    if (imageFiles.length === 0) return;
    void Promise.all(imageFiles.map(readAsAttachment)).then((attachments) => {
      setImages((prev) => [...prev, ...attachments]);
    });
  }, []);

  return (
    <div className="shrink-0 border-t border-line bg-base px-4 pb-3 pt-2">
      <div className="mx-auto w-full max-w-5xl">
        {blocked ? (
          <p className="mb-1.5 font-mono text-2xs text-danger" role="alert">
            Input blocked after a fatal error: {fatalError} — start a new session to continue.
          </p>
        ) : null}

        {images.length > 0 ? (
          <ul className="mb-1.5 flex flex-wrap gap-1.5" aria-label="Pending attachments">
            {images.map((image, index) => (
              <li key={`pending-${index}`} className="relative">
                <img
                  src={`data:${image.mimeType};base64,${image.data}`}
                  alt={`Pending attachment ${index + 1}`}
                  className="h-12 w-12 rounded-[var(--radius-cell)] border border-line object-cover"
                />
                <button
                  type="button"
                  onClick={() => setImages((prev) => prev.filter((_, i) => i !== index))}
                  aria-label={`Remove attachment ${index + 1}`}
                  className="absolute -right-1 -top-1 inline-flex size-4 items-center justify-center rounded-full border border-line bg-elevated text-ink-muted hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent"
                >
                  <X aria-hidden="true" className="size-2.5" />
                </button>
              </li>
            ))}
          </ul>
        ) : null}

        <div
          className={cn(
            "flex items-end gap-2 rounded-[var(--radius-cell)] border bg-surface px-2 py-1.5 transition-colors duration-[var(--duration-feedback)]",
            disabled ? "border-line opacity-60" : "border-line-strong focus-within:border-accent",
          )}
        >
          <Textarea
            ref={textareaRef}
            rows={1}
            value={text}
            disabled={disabled}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={onKeyDown}
            onPaste={(event) => {
              const files = event.clipboardData?.files;
              if (files && files.length > 0) addFiles(files);
            }}
            placeholder={
              disabled
                ? offline
                  ? "Disconnected — waiting for the server…"
                  : "Blocked by an error — start a new session, or fix it and reconnect"
                : turnRunning
                  ? "Steer the running turn…"
                  : "Prompt quartz — / for commands"
            }
            aria-label={turnRunning ? "Steer the running turn" : "Prompt quartz"}
            spellCheck={false}
            autoCapitalize="off"
            autoComplete="off"
            className="max-h-[200px] min-h-6 py-0.5"
          />

          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            multiple
            className="sr-only"
            aria-label="Attach images"
            onChange={(event) => {
              addFiles(event.target.files);
              event.target.value = "";
            }}
          />
          <Button
            variant="ghost"
            size="iconSm"
            disabled={disabled}
            onClick={() => fileRef.current?.click()}
            aria-label="Attach an image"
          >
            <ImagePlus aria-hidden="true" />
          </Button>

          {turnRunning ? (
            <Button
              variant="outline"
              size="iconSm"
              onClick={onCancel}
              aria-label="Cancel the running turn"
            >
              <Square aria-hidden="true" />
            </Button>
          ) : null}

          <Button
            variant={canSend ? "accent" : "outline"}
            size="sm"
            disabled={!canSend}
            onClick={submit}
            aria-label={turnRunning ? "Steer the running turn" : "Send prompt"}
            className="h-6"
          >
            {turnRunning ? (
              <>
                <CornerDownRight aria-hidden="true" />
                Steer
              </>
            ) : (
              <>
                <ArrowUp aria-hidden="true" />
                Send
              </>
            )}
          </Button>
        </div>

        <p className="mt-1 flex items-center gap-2 font-mono text-2xs text-ink-dim">
          <span>enter send</span>
          <span aria-hidden="true">·</span>
          <span>shift+enter newline</span>
          <span aria-hidden="true">·</span>
          <span>esc {turnRunning ? "cancel" : "clear"}</span>
          <span aria-hidden="true">·</span>
          <span>/ commands</span>
        </p>
      </div>
    </div>
  );
}
