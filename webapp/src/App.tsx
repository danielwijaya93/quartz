import * as React from "react";
import { HeaderBar } from "./components/HeaderBar";
import { Transcript } from "./components/Transcript";
import { StatusLine } from "./components/StatusLine";
import { Composer } from "./components/Composer";
import { CommandPalette } from "./components/CommandPalette";
import { useQuartz } from "./useQuartz";
import { useCommandPalette } from "./lib/useCommandPalette";
import { useOpenUISystemPrompt, useRichUiMode } from "./lib/useRichUi";
import { useOpenUIThemeMirror } from "./lib/theme";
import { wantsRichUi } from "./lib/richUi";
import type { ImageAttachment } from "./protocol";

export function App(): React.ReactElement {
  // Rich UI is a preference with a consequence: it decides both how a reply is
  // drawn *and* whether the session is told to produce OpenUI Lang at all.
  const richUi = useRichUiMode();
  const systemPromptAppend = useOpenUISystemPrompt(richUi.mode);
  const quartz = useQuartz(undefined, systemPromptAppend);

  const composerRef = React.useRef<HTMLTextAreaElement | null>(null);
  const palette = useCommandPalette(composerRef);

  // OpenUI reads `data-openui-theme`; mirror whatever HeroUI applied to `<html>`
  // so a surface is never drawn in the other theme's palette.
  useOpenUIThemeMirror();

  const onSubmit = React.useCallback(
    (text: string, images: ImageAttachment[]) => {
      quartz.prompt(text, images);
    },
    [quartz],
  );

  // An OpenUI form submit (`@ToAssistant`) is just the next user turn: it goes
  // through the same prompt frame, so the model continues this session.
  const onPrompt = React.useCallback(
    (text: string) => {
      quartz.prompt(text, []);
    },
    [quartz],
  );

  return (
    <div className="flex h-full flex-col bg-background text-foreground">
      <HeaderBar
        session={quartz.state.session}
        status={quartz.status}
        richUiMode={richUi.mode}
        onRichUiModeChange={richUi.setMode}
        onNewSession={quartz.newSession}
      />
      <Transcript
        rows={quartz.state.rows}
        turnRunning={quartz.state.turnRunning}
        richUi={wantsRichUi(richUi.mode)}
        onPrompt={onPrompt}
        onFormState={quartz.setFormState}
      />
      <StatusLine
        label={quartz.state.status}
        status={quartz.status}
        turnRunning={quartz.state.turnRunning}
      />
      <Composer
        connection={quartz.status}
        turnRunning={quartz.state.turnRunning}
        fatalError={quartz.state.fatalError}
        onSubmit={onSubmit}
        onSteer={quartz.steer}
        onCancel={quartz.cancel}
        onOpenPalette={palette.openPalette}
        textareaRef={composerRef}
      />
      <CommandPalette
        open={palette.open}
        commands={quartz.state.session.commands}
        onOpenChange={palette.onOpenChange}
        onRun={quartz.runCommand}
      />
    </div>
  );
}