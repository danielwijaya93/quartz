import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Transcript } from "./Transcript";
import type { Row } from "@/lib/transcript";

const userRow: Row = { kind: "user", id: "u1", text: "hello", images: [] };
const assistantRow: Row = { kind: "assistant", id: "a1", text: "**hi** there" };
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

function renderRows(rows: Row[]): void {
  render(<Transcript rows={rows} turnRunning={false} />);
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

  it("shows an empty state with the palette hint", () => {
    renderRows([]);
    expect(screen.getByText(/No turns yet/)).toBeInTheDocument();
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

  it("collapses thinking by default and expands on click", async () => {
    const user = userEvent.setup();
    renderRows([thinkingRow]);
    const trigger = screen.getByRole("button", { name: /Thinking, 3 lines/ });
    const body = (): Element | null => document.querySelector("pre");
    expect(body()).not.toBeVisible();
    await user.click(trigger);
    expect(body()).toBeVisible();
    expect(body()?.textContent).toBe("a\nb\nc");
  });

  it("renders a tool row with its status and duration", () => {
    renderRows([toolRow]);
    expect(screen.getByText("Succeeded")).toBeInTheDocument();
    expect(screen.getByText("12ms")).toBeInTheDocument();
  });

  it("renders a failed tool row with the error output visible", () => {
    renderRows([{ ...toolRow, status: "failed", output: "NameError: x" } as Row]);
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("NameError: x")).toBeVisible();
  });

  it("expands a tool row that carries an image, since the image is the output", () => {
    renderRows([
      {
        ...toolRow,
        images: [{ mimeType: "image/png", data: "abc" }],
      } as Row,
    ]);
    expect(screen.getByAltText("Tool output image 1")).toHaveAttribute(
      "src",
      "data:image/png;base64,abc",
    );
  });

  it("renders action chips as text, not colour", async () => {
    const user = userEvent.setup();
    renderRows([{ ...toolRow, actions: { wrote: "plot.png", cells: 4 } } as Row]);
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
    // One element per row, plus the scroll sentinel.
    expect(log.children).toHaveLength(11);
  });
});

describe("TranscriptAnnouncer", () => {
  it("announces a turn starting and finishing, not each delta", () => {
    const { rerender } = render(<Transcript rows={[]} turnRunning={false} />);
    const announcer = screen.getByTestId("transcript-announcer");

    rerender(<Transcript rows={[]} turnRunning />);
    expect(announcer).toHaveTextContent("Turn started.");

    // A streamed delta grows the assistant row but must not re-announce.
    const streaming: Row = { kind: "assistant", id: "a1", text: "hel" };
    rerender(<Transcript rows={[streaming]} turnRunning />);
    expect(announcer).toHaveTextContent("Turn started.");
    rerender(<Transcript rows={[{ ...streaming, text: "hello world" } as Row]} turnRunning />);
    expect(announcer.textContent).not.toContain("hello world");

    rerender(<Transcript rows={[streaming, turnEndRow]} turnRunning={false} />);
    expect(announcer).toHaveTextContent("Turn finished in 0.3 seconds.");
  });

  it("announces a tool call finishing once, not while it runs", () => {
    const running: Row = { ...toolRow, status: "running" } as Row;
    const { rerender } = render(<Transcript rows={[running]} turnRunning />);
    const announcer = screen.getByTestId("transcript-announcer");
    expect(announcer.textContent).toBe("");

    rerender(<Transcript rows={[toolRow]} turnRunning />);
    expect(announcer).toHaveTextContent("A tool call finished.");
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

    const { unmount } = render(<Transcript rows={[userRow]} turnRunning={false} />);
    const observer = contentObserver(records);
    expect(observer.disconnected).toBe(false);
    unmount();
    expect(observer.disconnected).toBe(true);
  });
});
