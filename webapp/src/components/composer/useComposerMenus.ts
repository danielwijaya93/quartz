import * as React from "react";
import { apiUrl } from "@/lib/serverUrl";
import { isProseQuery } from "@/lib/commandText";
import { detectAtMention, filterMentionFiles, insertAtMention } from "@/lib/fileMentions";
import { filterCommands, orderCommands } from "../CommandPalette";
import type { Command } from "@/protocol";

export function useComposerMenus({
  text,
  caretPos,
  cwd,
  disabled,
  commands,
  textareaRef,
  setText,
  setCaretPos,
}: {
  text: string;
  caretPos: number;
  cwd: string;
  disabled: boolean;
  commands: readonly Command[];
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  setText: React.Dispatch<React.SetStateAction<string>>;
  setCaretPos: React.Dispatch<React.SetStateAction<number>>;
}) {
  const [slashDismissed, setSlashDismissed] = React.useState(false);
  const [activeCmd, setActiveCmd] = React.useState(0);
  const [atDismissed, setAtDismissed] = React.useState(false);
  const [activeFile, setActiveFile] = React.useState(0);
  const [filesByCwd, setFilesByCwd] = React.useState<Readonly<Record<string, string[]>>>({});
  const [filesLoading, setFilesLoading] = React.useState(false);
  const cmdListRef = React.useRef<HTMLUListElement | null>(null);
  const fileListRef = React.useRef<HTMLUListElement | null>(null);
  const keyboardNavRef = React.useRef(false);

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
    void fetch(apiUrl(`/api/workspace/files?cwd=${encodeURIComponent(cwd)}`))
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
    [atMention, setCaretPos, setText, text, textareaRef],
  );

  return {
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
  };
}
