import * as React from "react";
import { TextArea } from "@heroui/react";
import { PROMPT_HANDOFF_EVENT, readPromptHandoff } from "@/lib/commandText";
import { parseCommandInput } from "./CommandPalette";
import { AttachmentStrip } from "./composer/AttachmentStrip";
import { ComposerAlerts } from "./composer/ComposerAlerts";
import { ComposerToolbar } from "./composer/ComposerToolbar";
import {
  MAX_FRAME_BYTES,
  MAX_TEXTAREA_PX,
  formatEffortLabel,
  isCoarsePointer,
  readAsAttachment,
  readAsTextFile,
  type AttachedFile,
} from "./composer/composerHelpers";
import { FileMentionMenu } from "./composer/FileMentionMenu";
import { SlashMenu } from "./composer/SlashMenu";
import { useComposerKeyboard } from "./composer/useComposerKeyboard";
import { useComposerMenus } from "./composer/useComposerMenus";
import type { ConnectionStatus } from "@/lib/connection";
import type { Command, ImageAttachment } from "@/protocol";

export { MAX_FRAME_BYTES, formatEffortLabel, type AttachedFile };

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
  cacheHitPct?: number;
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
  /** Text shared into the app from another app; dropped into the draft once. */
  sharedText?: string | null;
  /** Called after `sharedText` has been placed, so the parent stops re-sending it. */
  onSharedTextConsumed?: () => void;
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
  cacheHitPct,
  bgCount = 0,
  bgFailed = 0,
  onOpenJobs,
  onOpenModelPicker,
  onSubmit,
  onSteer,
  onCancel,
  onRunCommand,
  onOpenPalette,
  sharedText = null,
  onSharedTextConsumed,
  textareaRef: forwardedRef,
}: ComposerProps): React.ReactElement {
  const [text, setText] = React.useState("");
  const [images, setImages] = React.useState<ImageAttachment[]>([]);
  const [attachedFiles, setAttachedFiles] = React.useState<AttachedFile[]>([]);
  const [history, setHistory] = React.useState<string[]>([]);
  const [historyIndex, setHistoryIndex] = React.useState<number | null>(null);
  const [caretAtEnd, setCaretAtEnd] = React.useState(false);
  const [caretPos, setCaretPos] = React.useState(0);
  const [payloadError, setPayloadError] = React.useState<string | null>(null);
  const localRef = React.useRef<HTMLTextAreaElement | null>(null);
  const textareaRef = forwardedRef ?? localRef;
  const fileRef = React.useRef<HTMLInputElement | null>(null);

  const consumedRef = React.useRef(onSharedTextConsumed);
  consumedRef.current = onSharedTextConsumed;

  // A share arrives as "here is text", not "send this": it lands in the draft so the
  // person can add a question to it. Appending rather than replacing is what keeps a
  // half-typed prompt when the share comes in mid-sentence.
  React.useEffect(() => {
    if (sharedText === null) return;
    const incoming = sharedText.trim();
    if (!incoming) {
      consumedRef.current?.();
      return;
    }
    setText((prev) => (prev.trim() ? `${prev.trimEnd()}\n\n${incoming}` : incoming));
    consumedRef.current?.();
    const node = textareaRef.current;
    if (node) {
      node.focus();
      const end = node.value.length;
      try {
        node.setSelectionRange(end, end);
      } catch {
        /* Not focusable right now. */
      }
    }
  }, [sharedText, textareaRef]);

  const offline = connection !== "connected";
  const blocked = fatalError !== null;
  const disabled = offline || blocked;
  const canSend = !disabled && (text.trim() !== "" || images.length > 0 || attachedFiles.length > 0);

  const {
    slashQuery,
    inlineCommands,
    slashOpen,
    activeCmd,
    setActiveCmd,
    setSlashDismissed,
    atMention,
    atOpen,
    workspaceFiles,
    mentionFiles,
    activeFile,
    setActiveFile,
    setAtDismissed,
    filesLoading,
    cmdListRef,
    fileListRef,
    keyboardNavRef,
    selectMention,
  } = useComposerMenus({
    text,
    caretPos,
    cwd,
    disabled,
    commands,
    textareaRef,
    setText,
    setCaretPos,
  });

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

  const runInlineCommand = React.useCallback(
    (name: string, arg?: string) => {
      onRunCommand?.(name, arg);
      setText("");
      setPayloadError(null);
      setSlashDismissed(false);
      setHistoryIndex(null);
      textareaRef.current?.focus();
    },
    [onRunCommand, setSlashDismissed, textareaRef],
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

  const onKeyDown = useComposerKeyboard({
    slashOpen,
    atOpen,
    inlineCommands,
    mentionFiles,
    activeCmd,
    activeFile,
    commandsCount: commands.length,
    text,
    turnRunning,
    history,
    historyIndex,
    keyboardNavRef,
    setActiveCmd,
    setActiveFile,
    setSlashDismissed,
    setAtDismissed,
    setText,
    setPayloadError,
    setHistoryIndex,
    selectMention,
    submit,
    onCancel,
    onOpenPalette,
  });

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
          <SlashMenu
            slashQuery={slashQuery}
            inlineCommands={inlineCommands}
            activeCmd={activeCmd}
            cmdListRef={cmdListRef}
            onHoverIndex={(index) => {
              keyboardNavRef.current = false;
              setActiveCmd(index);
            }}
            onRunCommand={(name) => runInlineCommand(name)}
          />
        ) : null}

        {atOpen ? (
          <FileMentionMenu
            query={atMention?.query}
            filesLoading={filesLoading}
            workspaceFilesCount={workspaceFiles.length}
            mentionFiles={mentionFiles}
            activeFile={activeFile}
            fileListRef={fileListRef}
            onHoverIndex={(index) => {
              keyboardNavRef.current = false;
              setActiveFile(index);
            }}
            onSelectMention={selectMention}
          />
        ) : null}

        <ComposerAlerts fatalError={fatalError} payloadError={payloadError} />

        <div className="rounded-2xl border border-border/70 bg-surface/70 px-3 pt-2 pb-1.5 shadow-xs transition-all duration-150 focus-within:border-foreground/25 focus-within:ring-1 focus-within:ring-foreground/10">
          <AttachmentStrip
            images={images}
            attachedFiles={attachedFiles}
            onRemoveImage={(index) => {
              setImages((prev) => prev.filter((_, i) => i !== index));
              setPayloadError(null);
            }}
            onRemoveFile={(index) => {
              setAttachedFiles((prev) => prev.filter((_, i) => i !== index));
              setPayloadError(null);
            }}
          />

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
            enterKeyHint={isCoarsePointer() ? "enter" : "send"}
            spellCheck={false}
            autoCapitalize="off"
            autoComplete="off"
            className="quartz-bare-input max-h-[200px] min-h-[34px] w-full resize-none border-0 bg-transparent px-1 py-1 text-[0.9375rem] shadow-none"
          />

          <ComposerToolbar
            fileRef={fileRef}
            disabled={disabled}
            canSend={canSend}
            turnRunning={turnRunning}
            bgCount={bgCount}
            bgFailed={bgFailed}
            model={model}
            effort={effort}
            contextPct={contextPct}
            maxContextTokens={maxContextTokens}
            cacheHitPct={cacheHitPct}
            onAddFiles={addFiles}
            onOpenJobs={onOpenJobs}
            onOpenModelPicker={onOpenModelPicker}
            onCancel={onCancel}
            onSubmit={submit}
          />
        </div>
      </div>
    </div>
  );
}
