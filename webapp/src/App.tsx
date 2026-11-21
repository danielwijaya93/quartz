import * as React from "react";
import { HeaderBar } from "./components/HeaderBar";
import { Transcript } from "./components/Transcript";
import { StatusLine } from "./components/StatusLine";
import { Composer } from "./components/Composer";
import { CommandPalette } from "./components/CommandPalette";
import { useQuartz } from "./useQuartz";
import { useCommandPalette } from "./lib/useCommandPalette";
import { useRichUiMode } from "./lib/richUi";
import type { ImageAttachment } from "./protocol";

export function App(): React.ReactElement {
  const quartz = useQuartz();
  const richUi = useRichUiMode();
  const composerRef = React.useRef<HTMLTextAreaElement | null>(null);
  const palette = useCommandPalette(composerRef);

  // Read at send time, not at mount: flipping the toggle changes the *next*
  // prompt, which is exactly what the control promises.
  const dashboard = richUi.dashboard;
  const onSubmit = React.useCallback(
    (text: string, images: ImageAttachment[]) => {
      quartz.prompt(text, images, dashboard);
    },
    [quartz, dashboard],
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
      <Transcript rows={quartz.state.rows} turnRunning={quartz.state.turnRunning} />
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
