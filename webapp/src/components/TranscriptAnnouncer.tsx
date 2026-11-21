import * as React from "react";
import { nextAnnouncement } from "@/lib/announcements";
import type { Row } from "@/lib/transcript";

/**
 * The one live region in the transcript area.
 *
 * It is a small, separate, visually hidden node that receives coarse events only
 * - turn started, tool call finished, turn finished, error. The streaming text
 * itself is never live, because a per-token live region floods a screen reader
 * with hundreds of announcements per turn.
 */
export function TranscriptAnnouncer({
  rows,
  turnRunning,
}: {
  rows: readonly Row[];
  turnRunning: boolean;
}): React.ReactElement {
  const previousRows = React.useRef<readonly Row[]>(rows);
  const previousTurnRunning = React.useRef(turnRunning);
  const [message, setMessage] = React.useState<string>("");
  // Alternating zero-width suffix so a repeated message ("A tool call finished."
  // twice in one turn) still changes the node's text and is announced again. It
  // is invisible and not spoken.
  const [repeat, setRepeat] = React.useState(0);

  React.useEffect(() => {
    const started = !previousTurnRunning.current && turnRunning ? "Turn started." : null;
    const event = nextAnnouncement(previousRows.current, rows);
    previousRows.current = rows;
    previousTurnRunning.current = turnRunning;

    const announcement = [started, event].filter((part) => part !== null).join(" ");
    if (announcement === "") return;
    setMessage(announcement);
    setRepeat((count) => count + 1);
  }, [rows, turnRunning]);

  return (
    <p
      aria-live="polite"
      aria-atomic="true"
      role="status"
      data-testid="transcript-announcer"
      className="sr-only"
    >
      {message}
      {repeat % 2 === 1 ? "\u200b" : ""}
    </p>
  );
}
