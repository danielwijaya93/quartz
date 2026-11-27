import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Transcript } from "./Transcript";
import type { Row } from "@/lib/transcript";

const userRow: Row = { kind: "user", id: "u1", text: "hello", images: [] };
const assistantRow: Row = {
  kind: "assistant",
  id: "a1",
  text: "**hi** there",
  formState: null,
};
const thinkingRow: Row = { kind: "thinking", id: "k1", text: "a\nb\nc" };
const toolRow: Row = {
  kind: "tool",
  id: "t1",
  code: "print(1)",
  status: "ok",
  output: "1",
  durationMs: 12,
  images: [],
  actions: null,
};
const noticeRow: Row = { kind: "bash_notice", id: "n1", text: "job 3 finished" };
const systemNoticeRow: Row = {
  kind: "system_notice",
  id: "sn1",
  tag: "python-state-restored",
  text: "Restored 4 Python variables/functions from the previous session: df, model.",
};
const steeringRow: Row = { kind: "steering", id: "s1", text: "also x" };
const compactionRow: Row = {
  kind: "compaction",
  id: "c1",
  summary: "summarised",
  messagesBefore: 30,
  messagesAfter: 8,
  tokensBefore: 1000,
  tokensAfter: 250,
};
const errorRow: Row = { kind: "error", id: "e1", message: "boom", fatal: false };
const turnEndRow: Row = { kind: "turn_end", id: "te1", status: "ok", tokens: 12, elapsedMs: 340 };
const commandRow: Row = {
  kind: "command",
  id: "cmd1",
  name: "model",
  ok: true,
  result: { kind: "needs_terminal", message: "needs a terminal" },
};

/**
 * Transcript needs the Rich UI preference and the two callbacks a surface uses.
 * Markdown mode plus no-op callbacks is the default shape; the tests that care
 * about OpenUI pass their own.
 */
function renderRows(rows: Row[], richUi = false, cwd = "/home/x/gkyz-experiment"): void {
  render(
    <Transcript
      rows={rows}
      turnRunning={false}
      richUi={richUi}
      cwd={cwd}
      onPrompt={() => {}}
      onFormState={() => {}}
    />,
  );
}

describe("Transcript", () => {
  it("exposes an accessible log region", () => {
    renderRows([userRow]);
    expect(screen.getByRole("log", { name: "Session transcript" })).toBeInTheDocument();
  });

  it("is not a live region - streaming text must not be announced per token", () => {
    renderRows([assistantRow, { ...assistantRow, id: "a2", text: "second" } as Row]);

    const log = screen.getByRole("log", { name: "Session transcript" });
    expect(log).not.toHaveAttribute("aria-live");
    expect(log).not.toHaveAttribute("aria-atomic");
    // The only live region in the transcript area is the coarse announcer, and it
    // is not the node the deltas land in.
    const liveRegions = Array.from(document.querySelectorAll("[aria-live]"));
    expect(liveRegions).toHaveLength(1);
    const [liveRegion] = liveRegions;
    expect(liveRegion).not.toBe(log);
    expect(liveRegion === undefined ? true : log.contains(liveRegion)).toBe(false);
  });

  it("names the folder in an empty session, so starting one is acknowledged without starter templates", () => {
    renderRows([]);
    expect(screen.getByText("New session in gkyz-experiment")).toBeInTheDocument();
    expect(screen.getByText(/Send a prompt to start/)).toBeInTheDocument();
    expect(screen.getByText(/for commands/)).toBeInTheDocument();
    expect(screen.queryByText("Summarize repository")).not.toBeInTheDocument();
    expect(screen.queryByText("Git status & commits")).not.toBeInTheDocument();
    expect(screen.queryByText("Tests & checks")).not.toBeInTheDocument();
  });

  it("falls back to a plain empty state when the folder is not known yet", () => {
    renderRows([], false, "");
    expect(screen.getByText("No turns yet")).toBeInTheDocument();
  });

  it("renders a user row without a redundant You label", () => {
    renderRows([userRow]);
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.queryByText("You")).toBeNull();
  });

  it("renders assistant markdown as html", () => {
    renderRows([assistantRow]);
    expect(screen.getByText("hi").tagName).toBe("STRONG");
  });

  it("hides thinking rows once the turn settles", () => {
    renderRows([thinkingRow]);
    expect(screen.queryByRole("button", { name: /Thought/ })).toBeNull();
    expect(screen.queryByText("a\nb\nc")).toBeNull();
  });

  it("hides settled tool calls and thinking rows once the turn completes", () => {
    renderRows([toolRow, assistantRow, turnEndRow]);

    expect(screen.queryByRole("button", { name: /tool/i })).toBeNull();
    expect(screen.queryByTestId("living-waveform")).toBeNull();
    expect(screen.getByText("hi").tagName).toBe("STRONG");
  });

  it("keeps intermediate agent progress updates visible alongside the final reply", () => {
    renderRows([
      thinkingRow,
      toolRow,
      { kind: "assistant", id: "a-interim", text: "Found cause #1, checking #2...", formState: null } as Row,
      { ...toolRow, id: "t2" } as Row,
      assistantRow,
      turnEndRow,
    ]);

    expect(screen.getByText("Found cause #1, checking #2...")).toBeInTheDocument();
    expect(screen.getByText("hi").tagName).toBe("STRONG");
    expect(screen.queryByRole("button", { name: /tool/i })).toBeNull();
  });

  it("renders a bash notice, a steering row and a compaction marker", () => {
    renderRows([noticeRow, steeringRow, compactionRow]);
    expect(screen.getByText("job 3 finished")).toBeInTheDocument();
    expect(screen.getByText(/You steered the turn/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Context compacted" })).toBeInTheDocument();
    expect(screen.getByText(/30→8 messages/)).toBeInTheDocument();
  });

  it("draws a harness notice collapsed by default and expands on click", async () => {
    const user = userEvent.setup();
    renderRows([systemNoticeRow]);

    const notice = screen.getByRole("region", { name: "System notice" });
    expect(notice).toHaveTextContent("python-state-restored · 4 vars");
    expect(screen.queryByText("Restored 4 Python variables/functions")).toBeNull();

    await user.click(screen.getByRole("button", { name: /python-state-restored/ }));
    expect(notice).toHaveTextContent("Restored 4 Python variables/functions");
  });

  it("hides internal progress and tool-images notices from the stream", () => {
    renderRows([
      { kind: "system_notice", id: "p1", tag: "progress", text: "Update the user now on the progress." },
      { kind: "system_notice", id: "ti1", tag: "tool-images", text: "Attached image(s) from tool output:" },
    ]);
    expect(screen.queryByRole("region", { name: "System notice" })).toBeNull();
  });

  it("renders an error row", () => {
    renderRows([errorRow]);
    expect(screen.getByText("boom")).toBeInTheDocument();
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
  });

  it("marks a fatal error as an alert", () => {
    renderRows([{ ...errorRow, fatal: true } as Row]);
    expect(screen.getByRole("alert")).toHaveTextContent("boom");
  });

  it("renders the turn footer", () => {
    renderRows([turnEndRow]);
    expect(screen.getByText("12 tokens")).toBeInTheDocument();
    expect(screen.getByText("340ms")).toBeInTheDocument();
  });

  it("renders a needs_terminal command as a runnable-in-terminal card", () => {
    renderRows([commandRow]);
    expect(screen.getByText("Run this in the terminal")).toBeInTheDocument();
    expect(screen.getByText("needs a terminal")).toBeInTheDocument();
    // The name appears in the header and in the copyable command, so match all.
    expect(screen.getAllByText(/\/model/).length).toBeGreaterThan(0);
  });

  it("renders every row kind in one stream without throwing", () => {
    renderRows([
      userRow,
      thinkingRow,
      assistantRow,
      toolRow,
      noticeRow,
      systemNoticeRow,
      steeringRow,
      compactionRow,
      errorRow,
      turnEndRow,
      commandRow,
    ]);
    const log = screen.getByRole("log");
    // One node per standalone row, one per turn (which holds its tray and its
    // answer), plus the scroll sentinel: eleven rows become nine.
    expect(log.children).toHaveLength(10);
  });

  it("hides thinking and tools once settled while keeping the answer at full width", () => {
    renderRows([
      userRow,
      { kind: "thinking", id: "k1", text: "look" } as Row,
      toolRow,
      { ...toolRow, id: "t2" } as Row,
      { ...toolRow, id: "t3" } as Row,
      assistantRow,
      turnEndRow,
    ]);

    expect(screen.queryByText("Succeeded")).toBeNull();
    expect(screen.queryByRole("button", { name: /Thought, 3 tools/ })).toBeNull();
    expect(screen.getByText("hi").tagName).toBe("STRONG");
    expect(screen.queryByText("Quartz")).toBeNull();
  });

  it("renders a live turn with no answer as the living waveform progress indicator", () => {
    render(transcript([{ kind: "thinking", id: "k1", text: "thinking only" } as Row], true));
    expect(screen.getByTestId("living-waveform")).toBeInTheDocument();
    expect(screen.getByTestId("turn-tray-status")).toHaveTextContent("Thinking");
    expect(screen.queryByText("Quartz")).toBeNull();
  });

  it("keeps standalone rows between two turns in their original position", () => {
    renderRows([
      { kind: "assistant", id: "a1", text: "first", formState: null } as Row,
      noticeRow,
      { kind: "assistant", id: "a2", text: "second", formState: null } as Row,
    ]);

    const log = screen.getByRole("log");
    const texts = Array.from(log.children)
      .map((node) => node.textContent ?? "")
      .slice(0, 3);

    // Three rows in, three nodes out - the notice keeps its place between the
    // turns instead of being absorbed by one of them.
    expect(texts[0]).toContain("first");
    expect(texts[1]).toContain("job 3 finished");
    expect(texts[2]).toContain("second");
    // An answer-only turn has no machinery, so it gets no tray.
    expect(screen.queryAllByRole("button", { name: /Thought|tools/ })).toHaveLength(0);
  });

  it("interleaves settled work summaries, progress messages, and live trailing work in order", () => {
    const { rerender } = render(
      transcript(
        [
          { kind: "thinking", id: "k1", text: "step 1" } as Row,
          {
            kind: "tool",
            id: "t1",
            code: 'run("git status")',
            status: "ok",
            output: "",
            durationMs: 10,
            images: [],
            actions: null,
          } as Row,
          { kind: "assistant", id: "a1", text: "Checking files now", formState: null } as Row,
          {
            kind: "tool",
            id: "t2",
            code: 'edit("src/app.ts", "a", "b")',
            status: "running",
            output: "",
            durationMs: null,
            images: [],
            actions: null,
          } as Row,
        ],
        true,
      ),
    );

    const summaries = screen.getAllByTestId("work-summary");
    expect(summaries).toHaveLength(1);
    expect(summaries[0]).toHaveTextContent("Ran 1 command");
    expect(screen.getByText("Checking files now")).toBeInTheDocument();
    expect(screen.getByTestId("living-waveform")).toBeInTheDocument();

    // Verify DOM order: summary 1 -> progress message -> live waveform
    const logText = screen.getByRole("log").textContent ?? "";
    expect(logText.indexOf("Ran 1 command")).toBeLessThan(logText.indexOf("Checking files now"));
    expect(logText.indexOf("Checking files now")).toBeLessThan(logText.indexOf("Running edit"));

    // Now finish tool 2 and emit final report
    rerender(
      transcript(
        [
          { kind: "thinking", id: "k1", text: "step 1" } as Row,
          {
            kind: "tool",
            id: "t1",
            code: 'run("git status")',
            status: "ok",
            output: "",
            durationMs: 10,
            images: [],
            actions: null,
          } as Row,
          { kind: "assistant", id: "a1", text: "Checking files now", formState: null } as Row,
          {
            kind: "tool",
            id: "t2",
            code: 'edit("src/app.ts", "a", "b")',
            status: "ok",
            output: "",
            durationMs: 20,
            images: [],
            actions: null,
          } as Row,
          { kind: "assistant", id: "a2", text: "All done!", formState: null } as Row,
        ],
        false,
      ),
    );

    expect(screen.queryByTestId("living-waveform")).toBeNull();
    const settled = screen.getAllByTestId("work-summary");
    expect(settled.map((el) => el.textContent)).toEqual(["Ran 1 command · 10ms", "Edited 1 file · 20ms"]);
    const finalLogText = screen.getByRole("log").textContent ?? "";
    expect(finalLogText.indexOf("Ran 1 command")).toBeLessThan(
      finalLogText.indexOf("Checking files now"),
    );
    expect(finalLogText.indexOf("Checking files now")).toBeLessThan(
      finalLogText.indexOf("Edited 1 file"),
    );
    expect(finalLogText.indexOf("Edited 1 file")).toBeLessThan(finalLogText.indexOf("All done!"));
  });
});

/** The same props every time; only the rows change in these tests. */
function transcript(rows: Row[], turnRunning: boolean): React.ReactElement {
  return (
    <Transcript
      rows={rows}
      turnRunning={turnRunning}
      richUi={false}
      cwd=""
      onPrompt={() => {}}
      onFormState={() => {}}
    />
  );
}

describe("TranscriptAnnouncer", () => {
  it("announces a turn starting and finishing, not each delta", () => {
    const { rerender } = render(transcript([], false));
    const announcer = screen.getByTestId("transcript-announcer");

    rerender(transcript([], true));
    expect(announcer).toHaveTextContent("Turn started.");

    // A streamed delta grows the assistant row but must not re-announce.
    const streaming: Row = { kind: "assistant", id: "a1", text: "hel", formState: null };
    rerender(transcript([streaming], true));
    expect(announcer).toHaveTextContent("Turn started.");
    rerender(transcript([{ ...streaming, text: "hello world" } as Row], true));
    expect(announcer.textContent).not.toContain("hello world");

    rerender(transcript([streaming, turnEndRow], false));
    expect(announcer).toHaveTextContent("Turn finished in 0.3 seconds.");
  });

  it("leaves tool progress to the turn tray instead of announcing every call", () => {
    const running: Row = { ...toolRow, status: "running" } as Row;
    const { rerender } = render(transcript([running], true));
    const announcer = screen.getByTestId("transcript-announcer");
    expect(announcer.textContent).toBe("");

    // A finished call is not an interruption; the tray header is the live line
    // that carries "Running print(1)" and then the next step.
    rerender(transcript([toolRow], true));
    expect(announcer.textContent).toBe("");
    expect(screen.getAllByRole("status").map((node) => node.textContent)).toContain(
      "Used 1 tool",
    );
  });
});

/**
 * Tail following is the one behaviour in this component with a real user cost when
 * it breaks: the transcript silently drifts out from under a reader who never
 * scrolled. jsdom has no layout, so these tests drive the two inputs the component
 * actually uses - the scroll listener's metrics, and the size observer's callback.
 */
describe("Transcript tail following", () => {
  interface ObserverRecord {
    callback: ResizeObserverCallback;
    targets: Element[];
    disconnected: boolean;
  }

  const realResizeObserver = globalThis.ResizeObserver;

  function stubResizeObserver(): ObserverRecord[] {
    const records: ObserverRecord[] = [];
    class RecordingResizeObserver implements ResizeObserver {
      private readonly record: ObserverRecord;
      constructor(callback: ResizeObserverCallback) {
        this.record = { callback, targets: [], disconnected: false };
        records.push(this.record);
      }
      observe(target: Element): void {
        this.record.targets.push(target);
      }
      unobserve(): void {}
      disconnect(): void {
        this.record.disconnected = true;
      }
    }
    // vitest.setup.ts installs its stub with defineProperty, so it is writable but
    // not configurable: assign directly rather than using vi.stubGlobal.
    globalThis.ResizeObserver = RecordingResizeObserver;
    return records;
  }

  function logNode(): HTMLElement {
    return screen.getByRole("log");
  }

  /**
   * HeroUI's ScrollShadow runs its own observer on the scroll container for its
   * fade edges, so pick ours by what it watches: the reading column.
   */
  function contentObserver(records: ObserverRecord[]): ObserverRecord {
    const record = records.find((entry) => entry.targets.includes(logNode()));
    if (!record) throw new Error("no observer on the transcript content column");
    return record;
  }

  /** The scroll container the component measures; the log element is its child. */
  function scrollContainer(): HTMLElement {
    const container = logNode().parentElement;
    if (!container) throw new Error("transcript has no scroll container");
    return container;
  }

  /** jsdom lays nothing out, so the metrics the scroll listener reads are injected. */
  function setScrollMetrics(node: HTMLElement, scrollTop: number): void {
    Object.defineProperty(node, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(node, "clientHeight", { configurable: true, value: 100 });
    Object.defineProperty(node, "scrollTop", { configurable: true, value: scrollTop });
  }

  async function flushFrames(): Promise<void> {
    await act(async () => {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    });
  }

  afterEach(() => {
    globalThis.ResizeObserver = realResizeObserver;
    vi.restoreAllMocks();
  });

  it("observes the content column rather than the scroll container", () => {
    const records = stubResizeObserver();
    renderRows([userRow, assistantRow]);

    // The observer must sit on the node that grows (the reading column), because
    // the scroll container's own box does not change when rows reflow.
    expect(contentObserver(records).targets).toEqual([logNode()]);
  });

  it("re-pins to the tail when a row grows while the reader is at the bottom", async () => {
    const records = stubResizeObserver();
    renderRows([userRow, assistantRow]);

    const container = scrollContainer();
    setScrollMetrics(container, 900); // distance 0 -> pinned
    fireEvent.scroll(container);

    const sentinel = logNode().lastElementChild as HTMLElement;
    const scrollIntoView = vi.spyOn(sentinel, "scrollIntoView").mockImplementation(() => {});

    const observer = contentObserver(records);
    act(() => observer.callback([], observer as unknown as ResizeObserver));
    await flushFrames();

    // Growing content below the fold is exactly the reflow (markdown, webfont,
    // image decode, collapsible) that row-count following misses.
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "end" });
  });

  it("leaves a reader who scrolled up alone when a row grows", async () => {
    const records = stubResizeObserver();
    renderRows([userRow, assistantRow]);

    const container = scrollContainer();
    setScrollMetrics(container, 0); // distance 900 -> unpinned
    fireEvent.scroll(container);

    const sentinel = logNode().lastElementChild as HTMLElement;
    const scrollIntoView = vi.spyOn(sentinel, "scrollIntoView").mockImplementation(() => {});

    const observer = contentObserver(records);
    act(() => observer.callback([], observer as unknown as ResizeObserver));
    await flushFrames();

    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("coalesces a burst of reflow callbacks into a single scroll", async () => {
    const records = stubResizeObserver();
    renderRows([userRow, assistantRow]);

    const container = scrollContainer();
    setScrollMetrics(container, 900);
    fireEvent.scroll(container);

    const sentinel = logNode().lastElementChild as HTMLElement;
    const scrollIntoView = vi.spyOn(sentinel, "scrollIntoView").mockImplementation(() => {});

    const observer = contentObserver(records);
    const asObserver = observer as unknown as ResizeObserver;
    act(() => {
      observer.callback([], asObserver);
      observer.callback([], asObserver);
      observer.callback([], asObserver);
    });
    await flushFrames();

    expect(scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("offers a way back to the newest message only when the reader is off the tail", () => {
    renderRows([userRow, assistantRow]);
    expect(screen.queryByRole("button", { name: /Jump to the newest message/ })).toBeNull();

    const container = scrollContainer();
    setScrollMetrics(container, 0); // 900px above the tail
    fireEvent.scroll(container);

    // A real name, in words: an icon-only circle above a transcript is a mystery.
    expect(screen.getByRole("button", { name: /Jump to the newest message/ })).toBeInTheDocument();

    setScrollMetrics(container, 900);
    fireEvent.scroll(container);
    expect(screen.queryByRole("button", { name: /Jump to the newest message/ })).toBeNull();
  });

  it("says when output arrived while the reader was away, without a live region", () => {
    const first: Row[] = [userRow, assistantRow];
    const { rerender } = render(transcript(first, true));
    const container = scrollContainer();
    setScrollMetrics(container, 0);
    fireEvent.scroll(container);

    // Nothing new yet: it is a scroll button and says nothing more.
    const before = screen.getByRole("button", { name: /Jump to the newest message/ });
    expect(before).not.toHaveTextContent("New output");

    rerender(transcript([...first, { ...assistantRow, id: "a2", text: "and more" }], true));
    const after = screen.getByRole("button", { name: /Jump to the newest message/ });
    expect(after).toHaveTextContent("New output arrived below.");
    // Still not a live region: the transcript's announcements stay single-sourced.
    expect(after).not.toHaveAttribute("aria-live");
  });

  it("pins the tail again when the way back is pressed", () => {
    renderRows([userRow, assistantRow]);
    const container = scrollContainer();
    setScrollMetrics(container, 0);
    fireEvent.scroll(container);

    const sentinel = logNode().lastElementChild as HTMLElement;
    const scrollIntoView = vi.spyOn(sentinel, "scrollIntoView").mockImplementation(() => {});

    fireEvent.click(screen.getByRole("button", { name: /Jump to the newest message/ }));

    expect(scrollIntoView).toHaveBeenCalledWith({ block: "end" });
    // Back at the tail, the control is gone - there is nothing left to jump to.
    expect(screen.queryByRole("button", { name: /Jump to the newest message/ })).toBeNull();
  });

  it("keeps the way back on screen while a keyboard user holds it", async () => {
    const user = userEvent.setup();
    renderRows([userRow, assistantRow]);
    const container = scrollContainer();
    setScrollMetrics(container, 0);
    fireEvent.scroll(container);

    // Reached and pressed from the keyboard. Removing the node under the focus ring
    // would drop focus on <body> and send the next Tab to the top of the page.
    await user.tab();
    const button = screen.getByRole("button", { name: /Jump to the newest message/ });
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("jump-to-newest")).toHaveFocus();

    act(() => button.blur());
    expect(screen.queryByTestId("jump-to-newest")).toBeNull();
  });

  it("scrolls to a row the reader just sent, even from the middle of history", () => {
    const history: Row[] = [userRow, assistantRow];
    const { rerender } = render(transcript(history, true));
    const container = scrollContainer();
    setScrollMetrics(container, 0); // reading, far above the tail
    fireEvent.scroll(container);

    const sentinel = logNode().lastElementChild as HTMLElement;
    const scrollIntoView = vi.spyOn(sentinel, "scrollIntoView").mockImplementation(() => {});

    // The prompt is echoed straight into the row stream - the same shape every send
    // path produces - and sending is an explicit act, not the stream talking.
    const sent: Row = { kind: "user", id: "local-user-1", text: "what is 6*7?", images: [] };
    rerender(transcript([...history, sent], true));

    expect(scrollIntoView).toHaveBeenCalledWith({ block: "end" });
    // ...and the reader is now following the reply again.
    expect(screen.queryByTestId("jump-to-newest")).toBeNull();
  });

  it("lands at the tail when the session is switched", () => {
    // History rows are numbered by position, so the two sessions share every id:
    // the words are what tell the transcript it is looking at another conversation.
    const sessionOne: Row[] = [
      { kind: "user", id: "history-user-0", text: "about the data", images: [] },
      { kind: "assistant", id: "history-assistant-1", text: "here it is", formState: null },
    ];
    const sessionTwo: Row[] = [
      { kind: "user", id: "history-user-0", text: "about the plots", images: [] },
      { kind: "assistant", id: "history-assistant-1", text: "here they are", formState: null },
    ];

    const { rerender } = render(transcript(sessionOne, false));
    const container = scrollContainer();
    setScrollMetrics(container, 0);
    fireEvent.scroll(container);
    expect(screen.getByTestId("jump-to-newest")).toBeInTheDocument();

    const sentinel = logNode().lastElementChild as HTMLElement;
    const scrollIntoView = vi.spyOn(sentinel, "scrollIntoView").mockImplementation(() => {});

    rerender(transcript(sessionTwo, false));

    // A different conversation opens at its newest message, never mid-history.
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "end" });
  });

  it("stops observing the content column on unmount", () => {
    const records = stubResizeObserver();

    const { unmount } = render(<Transcript rows={[userRow]} turnRunning={false} richUi={false} cwd="" onPrompt={() => {}} onFormState={() => {}} />);
    const observer = contentObserver(records);
    expect(observer.disconnected).toBe(false);
    unmount();
    expect(observer.disconnected).toBe(true);
  });

  it("windows long transcripts to recent items first and loads earlier messages on demand", () => {
    const manyRows: Row[] = [];
    for (let i = 0; i < 60; i += 1) {
      manyRows.push({ kind: "user", id: `history-user-${i * 2}`, text: `Question ${i}`, images: [] });
      manyRows.push({
        kind: "assistant",
        id: `history-assistant-${i * 2 + 1}`,
        text: `Answer ${i}`,
        formState: null,
      });
    }

    renderRows(manyRows);
    // Newest messages are visible right away; oldest are hidden behind the window trigger.
    expect(screen.getByText("Answer 59")).toBeInTheDocument();
    expect(screen.queryByText("Question 0")).toBeNull();
    const loadEarlier = screen.getByRole("button", { name: /Show earlier messages/i });
    expect(loadEarlier).toBeInTheDocument();

    // Clicking loads older batches until all items are shown.
    fireEvent.click(loadEarlier);
    fireEvent.click(screen.getByRole("button", { name: /Show earlier messages/i }));
    expect(screen.getByText("Question 0")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Show earlier messages/i })).toBeNull();
  });
});