import * as React from "react";
import { Alert, Button, TextArea } from "@heroui/react";
import { ArrowUp, CornerDownRight, ImagePlus, Square, X } from "lucide-react";
import { PROMPT_HANDOFF_EVENT, readPromptHandoff } from "@/lib/commandText";
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
  /** Exposed so the app can return focus here after an overlay closes. */
  textareaRef?: React.RefObject<HTMLTextAreaElement | null>;
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
 * One field, one primary button, and a rule: while a turn is running the same
 * Enter key means "steer" instead of "prompt", mirroring the TUI. That is the
 * whole interaction model - no mode switch to learn.
 */
export function Composer({
  connection,
  turnRunning,
  fatalError,
  onSubmit,
  onSteer,
  onCancel,
  onOpenPalette,
  textareaRef: forwardedRef,
}: ComposerProps): React.ReactElement {
  const [text, setText] = React.useState("");
  const [images, setImages] = React.useState<ImageAttachment[]>([]);
  const [history, setHistory] = React.useState<string[]>([]);
  const [historyIndex, setHistoryIndex] = React.useState<number | null>(null);
  const [caretAtEnd, setCaretAtEnd] = React.useState(false);
  const localRef = React.useRef<HTMLTextAreaElement | null>(null);
  const textareaRef = forwardedRef ?? localRef;
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
  }, [textareaRef]);

  React.useEffect(() => {
    resize();
  }, [text, resize]);

  /**
   * A draft that only looked like a command comes back here.
   *
   * `/` opens the palette, and from then on the palette's filter is what receives
   * the keystrokes: a question about a path ("`/etc/hosts` says what?") turns into
   * a command nobody can run. The palette notices and hands the text back; it
   * arrives as composer text, with the caret after it, so the sentence can simply
   * be finished.
   */
  React.useEffect(() => {
    const onHandoff = (event: Event): void => {
      const handed = readPromptHandoff(event);
      if (handed === null) return;
      setText((prev) => prev + handed);
      setHistoryIndex(null);
      setCaretAtEnd(true);
    };
    window.addEventListener(PROMPT_HANDOFF_EVENT, onHandoff);
    return () => window.removeEventListener(PROMPT_HANDOFF_EVENT, onHandoff);
  }, []);

  // The caret can only be moved once the new value is in the DOM, which is the
  // render after the handoff lands in `text`.
  React.useEffect(() => {
    if (!caretAtEnd) return;
    setCaretAtEnd(false);
    const node = textareaRef.current;
    if (!node) return;
    node.focus();
    const end = node.value.length;
    node.setSelectionRange(end, end);
  }, [caretAtEnd, text, textareaRef]);

  // The composer owns focus: this is a keyboard tool, and losing focus after a
  // submit is the single most common papercut in chat UIs.
  React.useEffect(() => {
    if (!disabled) textareaRef.current?.focus();
  }, [disabled, turnRunning, textareaRef]);

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
        // A `/` that turns out to start a sentence rather than a command comes
        // back through `PROMPT_HANDOFF_EVENT` instead of being run as one.
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

  const placeholder = disabled
    ? offline
      ? "Disconnected — waiting for the server…"
      : "Blocked after an error — start a new session to continue"
    : turnRunning
      ? "Steer the running turn…"
      : "Ask Quartz anything — press / for commands";

  return (
    <div className="shrink-0 border-t border-separator bg-background px-4 pt-3 pb-4">
      <div className="mx-auto w-full max-w-3xl space-y-2">
        {blocked ? (
          <Alert status="danger" role="alert">
            <Alert.Indicator />
            <Alert.Content>
              <Alert.Title>Input is blocked</Alert.Title>
              <Alert.Description>
                <span className="text-sm">
                  {fatalError} — start a new session to continue.
                </span>
              </Alert.Description>
            </Alert.Content>
          </Alert>
        ) : null}

        {images.length > 0 ? (
          <ul className="flex flex-wrap gap-2" aria-label="Pending attachments">
            {images.map((image, index) => (
              <li key={`pending-${index}`} className="relative">
                <img
                  src={`data:${image.mimeType};base64,${image.data}`}
                  alt={`Pending attachment ${index + 1}`}
                  className="size-14 rounded-lg border border-border object-cover"
                />
                <button
                  type="button"
                  onClick={() => setImages((prev) => prev.filter((_, i) => i !== index))}
                  aria-label={`Remove attachment ${index + 1}`}
                  className="absolute -top-1.5 -right-1.5 inline-flex size-5 cursor-pointer items-center justify-center rounded-full border border-border bg-surface text-muted hover:text-foreground"
                >
                  <X aria-hidden="true" className="size-3" />
                </button>
              </li>
            ))}
          </ul>
        ) : null}

        <TextArea
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
          placeholder={placeholder}
          aria-label={turnRunning ? "Steer the running turn" : "Prompt quartz"}
          spellCheck={false}
          autoCapitalize="off"
          autoComplete="off"
          className="max-h-[200px] min-h-[52px] w-full resize-none py-3 text-[0.9375rem]"
        />

        <div className="flex items-center gap-2">
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
            size="sm"
            variant="ghost"
            isIconOnly
            isDisabled={disabled}
            onPress={() => fileRef.current?.click()}
            aria-label="Attach an image"
          >
            <ImagePlus aria-hidden="true" className="size-4" />
          </Button>

          <p className="hidden text-xs text-muted sm:block">
            <kbd className="font-mono">Enter</kbd> {turnRunning ? "to steer" : "to send"}
            <span aria-hidden="true"> · </span>
            <kbd className="font-mono">Shift+Enter</kbd> for a new line
          </p>

          <div className="ml-auto flex items-center gap-2">
            {turnRunning ? (
              <Button
                size="sm"
                variant="outline"
                onPress={onCancel}
                aria-label="Cancel the running turn"
              >
                <Square aria-hidden="true" className="size-3.5" />
                Stop
              </Button>
            ) : null}
            <Button
              size="sm"
              variant={canSend ? "primary" : "outline"}
              isDisabled={!canSend}
              onPress={submit}
              aria-label={turnRunning ? "Steer the running turn" : "Send prompt"}
            >
              {turnRunning ? (
                <>
                  <CornerDownRight aria-hidden="true" className="size-3.5" />
                  Steer
                </>
              ) : (
                <>
                  <ArrowUp aria-hidden="true" className="size-3.5" />
                  Send
                </>
              )}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
