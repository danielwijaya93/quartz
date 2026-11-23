import * as React from "react";
import { Alert, Button, TextArea, cn } from "@heroui/react";
import { ArrowUp, ChevronDown, CornerDownLeft, CornerDownRight, FileText, Plus, Square, X } from "lucide-react";
import { PROMPT_HANDOFF_EVENT, isProseQuery, readPromptHandoff } from "@/lib/commandText";
import { detectAtMention, filterMentionFiles, insertAtMention } from "@/lib/fileMentions";
import { filterCommands, isTerminalOnly, orderCommands, parseCommandInput } from "./CommandPalette";
import type { ConnectionStatus } from "@/lib/connection";
import type { Command, ImageAttachment } from "@/protocol";

const MAX_TEXTAREA_PX = 200;
export const MAX_FRAME_BYTES = 1024 * 1024;

export interface AttachedFile {
  name: string;
  content: string;
}

const EFFORT_LABELS: Record<string, string> = {
  none: "Off",
  minimal: "Min",
  low: "Low",
  medium: "Med",
  high: "High",
  max: "Max",
};

export function formatEffortLabel(effort: string): string {
  const key = effort.trim().toLowerCase();
  if (!key) return "";
  return EFFORT_LABELS[key] ?? `${key.charAt(0).toUpperCase()}${key.slice(1)}`;
}

function formatTokenWindow(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}k`;
  return String(tokens);
}

function ContextRing({
  pct,
  maxTokens,
}: {
  pct: number;
  maxTokens?: number;
}): React.ReactElement {
  const clamped = Math.max(0, Math.min(100, pct));
  const visualPct = clamped > 0 && clamped < 3 ? 3 : clamped;
  const radius = 7;
  const circumference = 2 * Math.PI * radius;
  const dashOffset = circumference * (1 - visualPct / 100);
  const label = maxTokens && maxTokens > 0
    ? `Context: ${pct.toFixed(1)}% of ${formatTokenWindow(maxTokens)}`
    : `Context: ${pct.toFixed(1)}%`;
  const toneClass =
    clamped >= 90 ? "text-danger" : clamped >= 75 ? "text-warning" : "text-foreground/75";

  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className={cn("inline-flex size-6 shrink-0 items-center justify-center", toneClass)}
    >
      <svg viewBox="0 0 18 18" className="size-4 -rotate-90" aria-hidden="true">
        <circle
          cx="9"
          cy="9"
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeOpacity={0.18}
        />
        <circle
          cx="9"
          cy="9"
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={dashOffset}
        />
      </svg>
    </span>
  );
}

export interface ComposerProps {
  connection: ConnectionStatus;
  cwd?: string;
  turnRunning: boolean;
  /** Latched fatal error blocks submission, matching the server's stance. */
  fatalError: string | null;
  commands?: readonly Command[];
  model?: string;
  effort?: string;
  contextPct?: number;
  maxContextTokens?: number;
  bgCount?: number;
  bgFailed?: number;
  onOpenJobs?: () => void;
  onOpenModelPicker?: () => void;
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

/** Read a non-image File as UTF-8 text. */
async function readAsTextFile(file: File): Promise<AttachedFile> {
  const content = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsText(file);
  });
  return { name: file.name, content };
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
  cwd = "",
  turnRunning,
  fatalError,
  commands = [],
  model,
  effort,
  contextPct,
  maxContextTokens,
  bgCount = 0,
  bgFailed = 0,
  onOpenJobs,
  onOpenModelPicker,
  onSubmit,
  onSteer,
  onCancel,
  onRunCommand,
  onOpenPalette,
  textareaRef: forwardedRef,
}: ComposerProps): React.ReactElement {
  const [text, setText] = React.useState("");
  const [images, setImages] = React.useState<ImageAttachment[]>([]);
  const [attachedFiles, setAttachedFiles] = React.useState<AttachedFile[]>([]);
  const [history, setHistory] = React.useState<string[]>([]);
  const [historyIndex, setHistoryIndex] = React.useState<number | null>(null);
  const [caretAtEnd, setCaretAtEnd] = React.useState(false);
  const [slashDismissed, setSlashDismissed] = React.useState(false);
  const [activeCmd, setActiveCmd] = React.useState(0);
  const [caretPos, setCaretPos] = React.useState(0);
  const [atDismissed, setAtDismissed] = React.useState(false);
  const [activeFile, setActiveFile] = React.useState(0);
  const [filesByCwd, setFilesByCwd] = React.useState<Readonly<Record<string, string[]>>>({});
  const [filesLoading, setFilesLoading] = React.useState(false);
  const [payloadError, setPayloadError] = React.useState<string | null>(null);
  const localRef = React.useRef<HTMLTextAreaElement | null>(null);
  const textareaRef = forwardedRef ?? localRef;
  const fileRef = React.useRef<HTMLInputElement | null>(null);
  const cmdListRef = React.useRef<HTMLUListElement | null>(null);
  const fileListRef = React.useRef<HTMLUListElement | null>(null);
  const keyboardNavRef = React.useRef(false);

  const offline = connection !== "connected";
  const blocked = fatalError !== null;
  const disabled = offline || blocked;
  const canSend = !disabled && (text.trim() !== "" || images.length > 0 || attachedFiles.length > 0);

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

  const atMention = React.useMemo(
    () => (slashOpen ? null : detectAtMention(text, caretPos)),
    [slashOpen, text, caretPos],
  );
  const atOpen = !disabled && !slashOpen && !atDismissed && atMention !== null;
  const workspaceFiles = filesByCwd[cwd] ?? [];
  const mentionFiles = React.useMemo(
    () => (atMention ? filterMentionFiles(workspaceFiles, atMention.query) : []),
    [atMention, workspaceFiles],
  );

  React.useEffect(() => {
    setActiveFile(0);
  }, [atMention?.query]);

  React.useEffect(() => {
    if (!slashOpen || !keyboardNavRef.current) return;
    keyboardNavRef.current = false;
    const selected = cmdListRef.current?.querySelector<HTMLElement>('[aria-selected="true"]');
    selected?.scrollIntoView?.({ block: "nearest" });
  }, [slashOpen, activeCmd]);

  React.useEffect(() => {
    if (!atOpen || !keyboardNavRef.current) return;
    keyboardNavRef.current = false;
    const selected = fileListRef.current?.querySelector<HTMLElement>('[aria-selected="true"]');
    selected?.scrollIntoView?.({ block: "nearest" });
  }, [atOpen, activeFile]);

  React.useEffect(() => {
    if (!atOpen || !cwd || filesByCwd[cwd] !== undefined) return;
    let cancelled = false;
    setFilesLoading(true);
    void fetch(`/api/workspace/files?cwd=${encodeURIComponent(cwd)}`)
      .then((resp) => (resp.ok ? resp.json() : Promise.reject(new Error("failed"))))
      .then((data: { files?: unknown }) => {
        if (cancelled) return;
        const list = Array.isArray(data.files)
          ? data.files.filter((item): item is string => typeof item === "string")
          : [];
        setFilesByCwd((prev) => ({ ...prev, [cwd]: list }));
      })
      .catch(() => {
        if (!cancelled) setFilesByCwd((prev) => ({ ...prev, [cwd]: [] }));
      })
      .finally(() => {
        if (!cancelled) setFilesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [atOpen, cwd, filesByCwd]);

  const selectMention = React.useCallback(
    (filePath: string) => {
      if (!atMention) return;
      const { nextText, nextCaret } = insertAtMention(text, atMention, filePath);
      setText(nextText);
      setCaretPos(nextCaret);
      setAtDismissed(false);
      window.requestAnimationFrame(() => {
        const node = textareaRef.current;
        if (!node) return;
        node.focus();
        node.setSelectionRange(nextCaret, nextCaret);
      });
    },
    [atMention, text, textareaRef],
  );

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
    if (value === "" && images.length === 0 && attachedFiles.length === 0) return;
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
      const fileBlocks = attachedFiles.map(
        (file) => `[Attached file: ${file.name}]\n\`\`\`\n${file.content}\n\`\`\``,
      );
      const finalText = [value, ...fileBlocks].filter(Boolean).join("\n\n");
      const frameBytes = new TextEncoder().encode(
        JSON.stringify({ type: "prompt", text: finalText, images }),
      ).length;
      if (frameBytes > MAX_FRAME_BYTES) {
        setPayloadError(
          "Message and attachments exceed the 1 MiB limit — remove an attachment or shorten the text.",
        );
        return;
      }
      setPayloadError(null);
      onSubmit(finalText, images);
      setHistory((prev) => [...prev, value].filter((entry) => entry !== ""));
    }
    setText("");
    setImages([]);
    setAttachedFiles([]);
    setHistoryIndex(null);
  }, [
    activeCmd,
    attachedFiles,
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
          keyboardNavRef.current = true;
          setActiveCmd((prev) =>
            inlineCommands.length === 0 ? 0 : (prev + 1) % inlineCommands.length,
          );
          return;
        }
        if (event.key === "ArrowUp") {
          event.preventDefault();
          keyboardNavRef.current = true;
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
      if (atOpen) {
        if (event.key === "ArrowDown") {
          event.preventDefault();
          keyboardNavRef.current = true;
          setActiveFile((prev) => (mentionFiles.length === 0 ? 0 : (prev + 1) % mentionFiles.length));
          return;
        }
        if (event.key === "ArrowUp") {
          event.preventDefault();
          keyboardNavRef.current = true;
          setActiveFile((prev) =>
            mentionFiles.length === 0 ? 0 : (prev - 1 + mentionFiles.length) % mentionFiles.length,
          );
          return;
        }
        if (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey)) {
          const selected = mentionFiles[activeFile];
          if (selected) {
            event.preventDefault();
            selectMention(selected);
            return;
          }
        }
        if (event.key === "Escape") {
          event.preventDefault();
          setAtDismissed(true);
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
      activeFile,
      atOpen,
      commands.length,
      history,
      historyIndex,
      inlineCommands,
      mentionFiles,
      onCancel,
      onOpenPalette,
      selectMention,
      slashOpen,
      submit,
      text,
      turnRunning,
    ],
  );

  const addFiles = React.useCallback((files: FileList | null) => {
    if (!files || files.length === 0) return;
    const list = Array.from(files);
    const imageFiles = list.filter((file) => file.type.startsWith("image/"));
    const otherFiles = list.filter((file) => !file.type.startsWith("image/"));
    setPayloadError(null);
    if (imageFiles.length > 0) {
      void Promise.all(imageFiles.map(readAsAttachment)).then((attachments) => {
        setImages((prev) => [...prev, ...attachments]);
      });
    }
    if (otherFiles.length > 0) {
      void Promise.all(otherFiles.map(readAsTextFile)).then((parsed) => {
        setAttachedFiles((prev) => [...prev, ...parsed]);
      });
    }
  }, []);

  const placeholder = disabled
    ? offline
      ? "Disconnected — waiting for the server…"
      : "Blocked after an error — start a new session to continue"
    : turnRunning
      ? "Steer the running turn…"
      : "Ask Quartz anything";

  return (
    <div className="shrink-0 bg-background px-4 pt-2 pb-5">
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
                ref={cmdListRef}
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
                      onMouseEnter={() => {
                        keyboardNavRef.current = false;
                        setActiveCmd(index);
                      }}
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

        {atOpen ? (
          <div
            role="region"
            aria-label="Workspace files"
            className="absolute right-0 bottom-full left-0 z-20 mb-2 overflow-hidden rounded-xl border border-border bg-surface shadow-lg"
          >
            <div className="flex items-center justify-between border-b border-separator px-3 py-2 text-xs text-muted">
              <span className="font-medium text-foreground">Files</span>
              <span>↑↓ navigate · Tab/Enter insert · Esc close</span>
            </div>
            {filesLoading && workspaceFiles.length === 0 ? (
              <p className="px-3 py-4 text-center text-sm text-muted">Loading workspace files…</p>
            ) : mentionFiles.length === 0 ? (
              <p className="px-3 py-4 text-center text-sm text-muted">
                No file matches “@{atMention?.query}”.
              </p>
            ) : (
              <ul
                ref={fileListRef}
                id="inline-file-list"
                role="listbox"
                aria-label="Matching workspace files"
                className="max-h-64 space-y-0.5 overflow-y-auto p-1.5"
              >
                {mentionFiles.map((filePath, index) => {
                  const selected = index === activeFile;
                  return (
                    <li
                      key={filePath}
                      role="option"
                      aria-selected={selected}
                      onMouseEnter={() => {
                        keyboardNavRef.current = false;
                        setActiveFile(index);
                      }}
                      onMouseDown={(event) => {
                        event.preventDefault();
                        selectMention(filePath);
                      }}
                      className={cn(
                        "flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs transition-colors",
                        selected
                          ? "bg-accent-soft text-accent-soft-foreground"
                          : "text-foreground hover:bg-surface-secondary/70",
                      )}
                    >
                      <FileText aria-hidden="true" className="size-3.5 shrink-0 text-muted" />
                      <span className="min-w-0 flex-1 truncate font-mono">{filePath}</span>
                      {selected ? (
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

        <div className="rounded-2xl border border-border/70 bg-surface/70 px-3 pt-2 pb-1.5 shadow-xs transition-all duration-150 focus-within:border-foreground/25 focus-within:ring-1 focus-within:ring-foreground/10">
          {images.length > 0 || attachedFiles.length > 0 ? (
            <ul className="mb-2 flex flex-wrap gap-2" aria-label="Pending attachments">
              {images.map((image, index) => (
                <li key={`pending-img-${index}`} className="relative">
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
              {attachedFiles.map((file, index) => (
                <li
                  key={`pending-file-${file.name}-${index}`}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-surface-secondary/70 px-2.5 py-1.5 text-xs text-foreground"
                >
                  <FileText aria-hidden="true" className="size-3.5 shrink-0 text-muted" />
                  <span className="max-w-[12rem] truncate font-mono">{file.name}</span>
                  <button
                    type="button"
                    onClick={() => {
                      setAttachedFiles((prev) => prev.filter((_, i) => i !== index));
                      setPayloadError(null);
                    }}
                    aria-label={`Remove file ${file.name}`}
                    className="ml-0.5 inline-flex size-4 cursor-pointer items-center justify-center rounded-full text-muted hover:text-foreground"
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
              const nextValue = event.target.value;
              setText(nextValue);
              setCaretPos(event.target.selectionStart ?? nextValue.length);
              setPayloadError(null);
              setSlashDismissed(false);
              setAtDismissed(false);
            }}
            onSelect={(event) => {
              const target = event.currentTarget;
              setCaretPos(target.selectionStart ?? target.value.length);
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
            className="quartz-bare-input max-h-[200px] min-h-[34px] w-full resize-none border-0 bg-transparent px-1 py-1 text-[0.9375rem] shadow-none"
          />

          <div className="mt-1 flex items-center gap-1.5">
            <input
              ref={fileRef}
              type="file"
              multiple
              className="sr-only"
              aria-label="File upload"
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
              aria-label="Attach files"
              className="size-7 rounded-lg text-muted hover:bg-surface-secondary hover:text-foreground"
            >
              <Plus aria-hidden="true" className="size-4" />
            </Button>

            <div className="ml-auto flex items-center gap-1.5">
              {bgCount > 0 || bgFailed > 0 ? (
                <button
                  type="button"
                  onClick={onOpenJobs}
                  aria-label={
                    bgFailed > 0
                      ? `Background jobs: ${bgCount} running, ${bgFailed} failed. Click to inspect.`
                      : `Background jobs running: ${bgCount}. Click to inspect.`
                  }
                  title="Click to open /jobs"
                  className={cn(
                    "inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-lg border px-2 font-mono text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
                    bgFailed > 0
                      ? "border-danger/40 bg-danger-soft text-danger-soft-foreground hover:bg-danger-soft/80"
                      : "border-success/40 bg-success-soft text-success hover:bg-success-soft/80",
                  )}
                >
                  <span
                    className={cn(
                      "size-1.5 rounded-full",
                      bgFailed > 0 ? "bg-danger" : "animate-pulse bg-success",
                    )}
                    aria-hidden="true"
                  />
                  <span>
                    {bgFailed > 0
                      ? bgCount > 0
                        ? `bg × ${bgCount} · ${bgFailed} failed`
                        : `bg × ${bgFailed} failed`
                      : `bg × ${bgCount}`}
                  </span>
                </button>
              ) : null}
              {onOpenModelPicker ? (
                <button
                  type="button"
                  onClick={onOpenModelPicker}
                  aria-label={
                    model
                      ? `Switch model (current: ${model}${effort ? ` ${formatEffortLabel(effort)}` : ""})`
                      : "Choose a model"
                  }
                  className="inline-flex h-7 max-w-[16rem] cursor-pointer items-center gap-1 rounded-lg px-2 text-xs text-muted transition-colors hover:bg-surface-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                >
                  <span className="truncate font-medium">{model || "Model"}</span>
                  {effort ? (
                    <span className="shrink-0 text-muted/80">{formatEffortLabel(effort)}</span>
                  ) : null}
                  <ChevronDown aria-hidden="true" className="size-3 shrink-0 text-muted/70" />
                </button>
              ) : null}
              {contextPct !== undefined ? (
                <ContextRing pct={contextPct} maxTokens={maxContextTokens} />
              ) : null}
              {turnRunning ? (
                <Button
                  size="sm"
                  variant="outline"
                  onPress={onCancel}
                  aria-label="Cancel the running turn"
                  className="h-8 gap-1 rounded-full px-2.5 text-xs font-medium"
                >
                  <Square aria-hidden="true" className="size-3" />
                  Stop
                </Button>
              ) : null}
              <Button
                size="sm"
                isIconOnly
                variant={canSend ? "secondary" : "ghost"}
                isDisabled={!canSend}
                onPress={submit}
                aria-label={turnRunning ? "Steer the running turn" : "Send prompt"}
                className={cn(
                  "size-8 rounded-full transition-all duration-150",
                  canSend
                    ? "bg-foreground/85 text-background shadow-xs hover:bg-foreground"
                    : "border border-border/50 bg-surface-secondary/40 text-muted opacity-40",
                )}
              >
                {turnRunning ? (
                  <CornerDownRight aria-hidden="true" className="size-4" />
                ) : (
                  <ArrowUp aria-hidden="true" className="size-4" />
                )}
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
