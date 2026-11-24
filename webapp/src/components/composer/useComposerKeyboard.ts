import * as React from "react";
import { isCoarsePointer } from "./composerHelpers";
import type { Command } from "@/protocol";

export function useComposerKeyboard({
  slashOpen,
  atOpen,
  inlineCommands,
  mentionFiles,
  activeCmd,
  activeFile,
  commandsCount,
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
}: {
  slashOpen: boolean;
  atOpen: boolean;
  inlineCommands: readonly Command[];
  mentionFiles: readonly string[];
  activeCmd: number;
  activeFile: number;
  commandsCount: number;
  text: string;
  turnRunning: boolean;
  history: readonly string[];
  historyIndex: number | null;
  keyboardNavRef: React.MutableRefObject<boolean>;
  setActiveCmd: React.Dispatch<React.SetStateAction<number>>;
  setActiveFile: React.Dispatch<React.SetStateAction<number>>;
  setSlashDismissed: React.Dispatch<React.SetStateAction<boolean>>;
  setAtDismissed: React.Dispatch<React.SetStateAction<boolean>>;
  setText: React.Dispatch<React.SetStateAction<string>>;
  setPayloadError: React.Dispatch<React.SetStateAction<string | null>>;
  setHistoryIndex: React.Dispatch<React.SetStateAction<number | null>>;
  selectMention: (filePath: string) => void;
  submit: () => void;
  onCancel: () => void;
  onOpenPalette: () => void;
}) {
  return React.useCallback(
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
        if (isCoarsePointer()) return;
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
      if (event.key === "/" && text === "" && commandsCount === 0) {
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
      commandsCount,
      history,
      historyIndex,
      inlineCommands,
      keyboardNavRef,
      mentionFiles,
      onCancel,
      onOpenPalette,
      selectMention,
      setActiveCmd,
      setActiveFile,
      setAtDismissed,
      setHistoryIndex,
      setPayloadError,
      setSlashDismissed,
      setText,
      slashOpen,
      submit,
      text,
      turnRunning,
    ],
  );
}
