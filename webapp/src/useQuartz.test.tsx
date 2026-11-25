import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { ServerMessage } from "./protocol";

type Listener = (payload: unknown) => void;

class FakeConnection {
  private listeners = new Map<string, Set<Listener>>();
  on(event: string, listener: Listener): () => void {
    let set = this.listeners.get(event);
    if (!set) {
      set = new Set();
      this.listeners.set(event, set);
    }
    set.add(listener);
    return () => set?.delete(listener);
  }
  emit(event: string, payload: unknown): void {
    for (const listener of this.listeners.get(event) ?? []) listener(payload);
  }
  setSystemPromptAppend(): void {}
  connect(): void {}
  close(): void {}
}

let latestConnection: FakeConnection | null = null;

vi.mock("./lib/connection", () => ({
  Connection: class {
    constructor() {
      latestConnection = new FakeConnection();
      return latestConnection;
    }
  },
  resolveSocketUrl: () => "ws://localhost/ws",
}));

import { useQuartz } from "./useQuartz";

describe("useQuartz streaming delta coalescing", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    latestConnection = null;
  });

  it("coalesces multiple text_delta frames into one render per animation frame and flushes before turn_end", () => {
    const rafCallbacks: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb) => {
      rafCallbacks.push(cb);
      return rafCallbacks.length;
    });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});

    let renders = 0;
    const { result } = renderHook(() => {
      renders += 1;
      return useQuartz();
    });

    const emit = (msg: ServerMessage) => latestConnection!.emit("message", msg);

    act(() => {
      emit({ type: "turn_start", turnId: "t1" });
    });
    const rendersAfterStart = renders;

    // Fire 10 rapid text_delta packets in the same animation frame.
    act(() => {
      for (let i = 0; i < 10; i += 1) {
        emit({ type: "text_delta", text: `tok${i} ` });
      }
    });

    // Should not have re-rendered 10 times before rAF fires.
    expect(renders).toBe(rendersAfterStart);
    expect(rafCallbacks).toHaveLength(1);

    // Flushing the animation frame applies all 10 tokens in one render.
    act(() => {
      rafCallbacks.shift()!(16);
    });
    expect(renders).toBe(rendersAfterStart + 1);
    expect(result.current.state.rows[0]).toMatchObject({
      kind: "assistant",
      text: "tok0 tok1 tok2 tok3 tok4 tok5 tok6 tok7 tok8 tok9 ",
    });

    // Queued deltas arriving right before turn_end must flush BEFORE turn_end so ordering stays intact.
    act(() => {
      emit({ type: "text_delta", text: "final." });
      emit({ type: "turn_end", status: "ok", tokens: 42, elapsedMs: 120 });
    });
    expect(result.current.state.rows[0]).toMatchObject({
      kind: "assistant",
      text: "tok0 tok1 tok2 tok3 tok4 tok5 tok6 tok7 tok8 tok9 final.",
    });
    expect(result.current.state.rows[1]).toMatchObject({ kind: "turn_end", status: "ok" });
    expect(result.current.state.turnRunning).toBe(false);
  });
});
