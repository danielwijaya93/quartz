import * as React from "react";
import { Alert, Button, TextArea, cn } from "@heroui/react";
import { ArrowUp, CornerDownLeft, CornerDownRight, ImagePlus, Slash, Square, X } from "lucide-react";
import { PROMPT_HANDOFF_EVENT, isProseQuery, readPromptHandoff } from "@/lib/commandText";
import { filterCommands, isTerminalOnly, orderCommands, parseCommandInput } from "./CommandPalette";
import type { ConnectionStatus } from "@/lib/connection";
import type { Command, ImageAttachment } from "@/protocol";

const MAX_TEXTAREA_PX = 200;
export const MAX_FRAME_BYTES = 1024 * 1024;

export interface ComposerProps {
  connection: ConnectionStatus;
  turnRunning: boolean;
  /** Latched fatal error blocks submission, matching the server's stance. */
  fatalError: string | null;
  commands?: readonly Command[];
  onSubmit: (text: string, images: ImageAttachment[]) => void;
  onSteer: (text: string) => void;
  onCancel: () => void;
  onRunCommand?: (name: string, arg?: string) => void;
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
  commands = [],
  onSubmit,
  onSteer,
  onCancel,
  onRunCommand,
  onOpenPalette,
  textareaRef: forwardedRef,
}: ComposerProps): React.ReactElement {
  const [text, setText] = React.useState("");
  const [images, setImages] = React.useState<ImageAttachment[]>([]);
  const [history, setHistory] = React.useState<string[]>([]);
  const [historyIndex, setHistoryIndex] = React.useState<number | null>(null);
  const [caretAtEnd, setCaretAtEnd] = React.useState(false);
  const [slashDismissed, setSlashDismissed] = React.useState(false);
  const [activeCmd, setActiveCmd] = React.useState(0);
  const [payloadError, setPayloadError] = React.useState<string | null>(null);
  const localRef = React.useRef<HTMLTextAreaElement | null>(null);
  const textareaRef = forwardedRef ?? localRef;
  const fileRef = React.useRef<HTMLInputElement | null>(null);

  const offline = connection !== "connected";
  const blocked = fatalError !== null;
  const disabled = offline || blocked;
  const canSend = !disabled && (text.trim() !== "" || images.length > 0);

  const commandNames = React.useMemo(() => commands.map((c) => c.name), [commands]);
  const slashQuery = React.useMemo(() => {
    if (!text.startsWith("/") || text.includes("\n")) return null;
    const raw = text.slice(1);
    if (isProseQuery(raw, commandNames)) return null;
    return raw;
  }, [commandNames, text]);

  const inlineCommands = React.useMemo(() => {
    if (slashQuery === null || commands.length === 0) return [];
    return orderCommands(filterCommands(commands, slashQuery));
  }, [commands, slashQuery]);

  const slashOpen = !disabled && !slashDismissed && slashQuery !== null && commands.length > 0;

  React.useEffect(() => {
    setActiveCmd(0);
  }, [slashQuery]);

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

  const runInlineCommand = React.useCallback(
    (name: string, arg?: string) => {
      onRunCommand?.(name, arg);
      setText("");
      setPayloadError(null);
      setSlashDismissed(false);
      setHistoryIndex(null);
      textareaRef.current?.focus();
    },
    [onRunCommand, textareaRef],
  );

  const submit = React.useCallback(() => {
    const value = text.trim();
    if (value === "" && images.length === 0) return;
    if (slashOpen && onRunCommand) {
      const parsed = parseCommandInput(value);
      const exact = commands.find(
        (command) => command.name.toLowerCase() === parsed.name.toLowerCase(),
      );
      if (parsed.arg !== "") {
        if (exact) {
          runInlineCommand(exact.name, parsed.arg);
          return;
        }
      } else {
        const selected = inlineCommands[activeCmd];
        if (selected) {
          runInlineCommand(selected.name);
          return;
        }
        if (exact) {
          runInlineCommand(exact.name);
          return;
        }
      }
    }
    if (turnRunning) {
      if (value === "") return;
      onSteer(value);
    } else {
      const frameBytes = new TextEncoder().encode(
        JSON.stringify({ type: "prompt", text: value, images }),
      ).length;
      if (frameBytes > MAX_FRAME_BYTES) {
        setPayloadError(
          "Message and attachments exceed the 1 MiB limit — remove an attachment or shorten the text.",
        );
        return;
      }
      setPayloadError(null);
      onSubmit(value, images);
      setHistory((prev) => [...prev, value].filter((entry) => entry !== ""));
    }
    setText("");
    setImages([]);
    setHistoryIndex(null);
  }, [
    activeCmd,
    commands,
    images,
    inlineCommands,
    onRunCommand,
    onSteer,
    onSubmit,
    runInlineCommand,
    slashOpen,
    text,
    turnRunning,
  ]);

  const onKeyDown = React.useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (slashOpen) {
        if (event.key === "ArrowDown") {
          event.preventDefault();
          setActiveCmd((prev) =>
            inlineCommands.length === 0 ? 0 : (prev + 1) % inlineCommands.length,
          );
          return;
        }
        if (event.key === "ArrowUp") {
          event.preventDefault();
          setActiveCmd((prev) =>
            inlineCommands.length === 0
              ? 0
              : (prev - 1 + inlineCommands.length) % inlineCommands.length,
          );
          return;
        }
        if (event.key === "Tab") {
          const selected = inlineCommands[activeCmd];
          if (selected) {
            event.preventDefault();
            setText(`/${selected.name} `);
            return;
          }
        }
        if (event.key === "Escape") {
          event.preventDefault();
          setSlashDismissed(true);
          return;
        }
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        submit();
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        if (turnRunning) onCancel();
        else if (text !== "") {
          setText("");
          setPayloadError(null);
        }
        return;
      }
      if (event.key === "/" && text === "" && commands.length === 0) {
        // Fallback when inline commands aren't wired (e.g. standalone tests).
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
    [
      activeCmd,
      commands.length,
      history,
      historyIndex,
      inlineCommands,
      onCancel,
      onOpenPalette,
      slashOpen,
      submit,
      text,
      turnRunning,
    ],
  );

  const addFiles = React.useCallback((files: FileList | null) => {
    if (!files || files.length === 0) return;
    const imageFiles = Array.from(files).filter((file) => file.type.startsWith("image/"));
    if (imageFiles.length === 0) return;
    setPayloadError(null);
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
    <div className="shrink-0 bg-background px-4 pt-2 pb-4">
      <div className="relative mx-auto w-full max-w-3xl space-y-2">
        {slashOpen ? (
          <div
            role="region"
            aria-label="Slash commands"
            className="absolute right-0 bottom-full left-0 z-20 mb-2 overflow-hidden rounded-xl border border-border bg-surface shadow-lg"
          >
            <div className="flex items-center justify-between border-b border-separator px-3 py-2 text-xs text-muted">
              <span className="font-medium text-foreground">Commands</span>
              <span>↑↓ navigate · Tab complete · Enter run · Esc close</span>
            </div>
            {inlineCommands.length === 0 ? (
              <p className="px-3 py-4 text-center text-sm text-muted">
                No command matches “/{slashQuery}”. Press Enter to send as text or Esc to close.
              </p>
            ) : (
              <ul
                id="inline-command-list"
                role="listbox"
                aria-label="Available commands"
                className="max-h-64 space-y-0.5 overflow-y-auto p-1.5"
              >
                {inlineCommands.map((command, index) => {
                  const selected = index === activeCmd;
                  const terminalOnly = isTerminalOnly(command);
                  return (
                    <li
                      key={command.name}
                      id={`inline-command-${command.name}`}
                      role="option"
                      aria-selected={selected}
                      data-selected={selected}
                      onMouseEnter={() => setActiveCmd(index)}
                      onMouseDown={(event) => {
                        event.preventDefault();
                        runInlineCommand(command.name);
                      }}
                      className={cn(
                        "flex min-h-9 cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm",
                        selected
                          ? "bg-accent-soft text-accent-soft-foreground"
                          : "text-foreground hover:bg-surface-secondary",
                        terminalOnly && "opacity-60",
                      )}
                    >
                      <span className="shrink-0 font-mono text-xs font-semibold">
                        /{command.name}
                      </span>
                      {command.usage ? (
                        <span className="shrink-0 font-mono text-xs text-muted">
                          {command.usage}
                        </span>
                      ) : null}
                      <span className="min-w-0 flex-1 truncate text-xs text-muted">
                        {command.description}
                      </span>
                      {terminalOnly ? (
                        <span className="shrink-0 rounded bg-surface-secondary px-1.5 py-0.5 text-[10px] font-medium text-muted">
                          Terminal
                        </span>
                      ) : selected ? (
                        <CornerDownLeft aria-hidden="true" className="size-3.5 shrink-0 text-muted" />
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        ) : null}

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
        ) : payloadError !== null ? (
          <Alert status="danger" role="alert">
            <Alert.Indicator />
            <Alert.Content>
              <Alert.Title>Message too large</Alert.Title>
              <Alert.Description>
                <span className="text-sm">{payloadError}</span>
              </Alert.Description>
            </Alert.Content>
          </Alert>
        ) : null}

        <div className="rounded-2xl border border-border bg-surface/70 px-3 pt-2 pb-2 shadow-sm transition-colors focus-within:border-focus focus-within:ring-2 focus-within:ring-focus/20">
          {images.length > 0 ? (
            <ul className="mb-2 flex flex-wrap gap-2" aria-label="Pending attachments">
              {images.map((image, index) => (
                <li key={`pending-${index}`} className="relative">
                  <img
                    src={`data:${image.mimeType};base64,${image.data}`}
                    alt={`Pending attachment ${index + 1}`}
                    className="size-14 rounded-lg border border-border object-cover"
                  />
                  <button
                    type="button"
                    onClick={() => {
                      setImages((prev) => prev.filter((_, i) => i !== index));
                      setPayloadError(null);
                    }}
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
            onChange={(event) => {
              setText(event.target.value);
              setPayloadError(null);
              setSlashDismissed(false);
            }}
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
            className="quartz-bare-input max-h-[200px] min-h-[40px] w-full resize-none border-0 bg-transparent px-1 py-1.5 text-[0.9375rem] shadow-none"
          />

          <div className="mt-1 flex items-center gap-1.5">
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

            <Button
              size="sm"
              variant="ghost"
              isIconOnly
              isDisabled={disabled}
              onPress={() => {
                if (commands.length > 0) {
                  setText("/");
                  setSlashDismissed(false);
                  textareaRef.current?.focus();
                } else {
                  onOpenPalette();
                }
              }}
              aria-label="Slash commands"
            >
              <Slash aria-hidden="true" className="size-3.5" />
            </Button>

            <p className="ml-1 hidden text-xs text-muted sm:block">
              <kbd className="font-mono">Enter</kbd> {turnRunning ? "to steer" : "to send"}
              <span aria-hidden="true"> · </span>
              <kbd className="font-mono">Shift+Enter</kbd> new line
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
    </div>
  );
}
