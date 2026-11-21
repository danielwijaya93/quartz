import * as React from "react";
import { HeaderBar } from "./components/HeaderBar";
import { Transcript } from "./components/Transcript";
import { StatusLine } from "./components/StatusLine";
import { Composer } from "./components/Composer";
import { CommandPalette } from "./components/CommandPalette";
import { useQuartz } from "./useQuartz";
import type { ImageAttachment } from "./protocol";

export function App(): React.ReactElement {
  const quartz = useQuartz();
  const [paletteOpen, setPaletteOpen] = React.useState(false);

  // `/` opens the palette from anywhere, except while typing in a field. The
  // composer handles its own `/`, so this is the global fallback.
  React.useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || target?.isContentEditable) return;
      event.preventDefault();
      setPaletteOpen(true);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const onSubmit = React.useCallback(
    (text: string, images: ImageAttachment[]) => {
      quartz.prompt(text, images);
    },
    [quartz],
  );

  return (
    <div className="flex h-full flex-col bg-base">
      <HeaderBar
        session={quartz.state.session}
        status={quartz.status}
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
        onOpenPalette={() => setPaletteOpen(true)}
      />
      <CommandPalette
        open={paletteOpen}
        commands={quartz.state.session.commands}
        onOpenChange={setPaletteOpen}
        onRun={quartz.runCommand}
      />
    </div>
  );
}
