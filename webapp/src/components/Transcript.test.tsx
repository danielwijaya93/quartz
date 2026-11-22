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

  it("names the folder in an empty session, so starting one is acknowledged", () => {
    renderRows([]);
    expect(screen.getByText("New session in gkyz-experiment")).toBeInTheDocument();
    expect(screen.getByText(/Send a prompt to start/)).toBeInTheDocument();
    expect(screen.getByText(/for commands/)).toBeInTheDocument();
  });

  it("falls back to a plain empty state when the folder is not known yet", () => {
    renderRows([], false, "");
    expect(screen.getByText("No turns yet")).toBeInTheDocument();
  });

  it("renders a user row", () => {
    renderRows([userRow]);
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.getByText("You")).toBeInTheDocument();
  });

  it("renders assistant markdown as html", () => {
    renderRows([assistantRow]);
    expect(screen.getByText("hi").tagName).toBe("STRONG");
  });

  it("folds thinking into the tray and keeps it collapsed when the tray opens", async () => {
    const user = userEvent.setup();
    renderRows([thinkingRow]);

    // Nothing the turn did is on the page until the tray is opened.
    expect(screen.queryByRole("button", { name: /Thinking, 3 lines/ })).toBeNull();

    await user.click(screen.getByRole("button", { name: /1 step/ }));
    const trigger = screen.getByRole("button", { name: /Thinking, 3 lines/ });
    const body = (): Element | null => document.querySelector("pre");
    expect(body()).not.toBeVisible();
    await user.click(trigger);
    expect(body()).toBeVisible();
    expect(body()?.textContent).toBe("a\nb\nc");
  });

  it("renders a tool row, folded, with its status and duration", async () => {
    const user = userEvent.setup();
    renderRows([toolRow]);

    // The run, not the call, is the tray's row: one line for one kind of work.
    expect(screen.queryByText("Succeeded")).toBeNull();
    await user.click(screen.getByRole("button", { name: /1 step/ }));
    await user.click(screen.getByRole("button", { name: /Used 1 tool/ }));

    expect(screen.getByText("Succeeded")).toBeInTheDocument();
    expect(screen.getByText("12ms")).toBeInTheDocument();
  });

  it("renders a failed tool row with the error output visible", async () => {
    const user = userEvent.setup();
    renderRows([{ ...toolRow, status: "failed", output: "NameError: x" } as Row]);

    // The failure is visible before anything is opened: the header counts it.
    expect(screen.getByText("1 failed")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /1 step/ }));
    await user.click(screen.getByRole("button", { name: /Used 1 tool/ }));
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("NameError: x")).toBeVisible();
  });

  it("expands a tool row that carries an image, since the image is the output", async () => {
    const user = userEvent.setup();
    renderRows([
      {
        ...toolRow,
        images: [{ mimeType: "image/png", data: "abc" }],
      } as Row,
    ]);
    await user.click(screen.getByRole("button", { name: /1 step/ }));
    await user.click(screen.getByRole("button", { name: /Used 1 tool/ }));

    expect(screen.getByAltText("Tool output image 1")).toHaveAttribute(
      "src",
      "data:image/png;base64,abc",
    );
  });

  it("renders action chips as text, not colour", async () => {
    const user = userEvent.setup();
    renderRows([{ ...toolRow, actions: { wrote: "plot.png", cells: 4 } } as Row]);
    await user.click(screen.getByRole("button", { name: /1 step/ }));
    await user.click(screen.getByRole("button", { name: /Used 1 tool/ }));
    await user.click(screen.getByRole("button", { name: /Tool call, Succeeded/ }));

    expect(screen.getByRole("list", { name: "Side effects" })).toBeInTheDocument();
    expect(screen.getByText("wrote: plot.png")).toBeInTheDocument();
    expect(screen.getByText("cells: 4")).toBeInTheDocument();
  });

  it("renders a bash notice, a steering row and a compaction marker", () => {
    renderRows([noticeRow, steeringRow, compactionRow]);
    expect(screen.getByText("job 3 finished")).toBeInTheDocument();
    expect(screen.getByText(/You steered the turn/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Context compacted" })).toBeInTheDocument();
    expect(screen.getByText(/30→8 messages/)).toBeInTheDocument();
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
      steeringRow,
      compactionRow,
      errorRow,
      turnEndRow,
      commandRow,
    ]);
    const log = screen.getByRole("log");
    // One node per standalone row, one per turn (which holds its tray and its
    // answer), plus the scroll sentinel: ten rows become eight.
    expect(log.children).toHaveLength(9);
  });

  it("folds a turn's work into one closed tray above the answer", () => {
    renderRows([
      userRow,
      { kind: "thinking", id: "k1", text: "look" } as Row,
      toolRow,
      { ...toolRow, id: "t2" } as Row,
      { ...toolRow, id: "t3" } as Row,
      assistantRow,
      turnEndRow,
    ]);

    // The old shape was one card per thinking block and per tool call.
    expect(screen.queryByText("Succeeded")).toBeNull();

    // Thinking plus one folded run of three calls: two steps, not four cards.
    const tray = screen.getByRole("button", { name: /2 steps/ });
    expect(tray).toHaveAttribute("aria-expanded", "false");
    // The answer is on the page, at full width, in the reading column.
    expect(screen.getByText("hi").tagName).toBe("STRONG");
    // One speaker label per turn, not one per assistant row.
    expect(screen.getAllByText("Quartz")).toHaveLength(1);
  });

  it("renders a turn with no answer as just its tray", () => {
    renderRows([{ kind: "thinking", id: "k1", text: "thinking only" } as Row]);
    expect(screen.getByRole("button", { name: /1 step/ })).toBeInTheDocument();
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
    expect(screen.queryAllByRole("button", { name: /step/ })).toHaveLength(0);
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

  it("stops observing the content column on unmount", () => {
    const records = stubResizeObserver();

    const { unmount } = render(<Transcript rows={[userRow]} turnRunning={false} richUi={false} cwd="" onPrompt={() => {}} onFormState={() => {}} />);
    const observer = contentObserver(records);
    expect(observer.disconnected).toBe(false);
    unmount();
    expect(observer.disconnected).toBe(true);
  });
});