import * as React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { initialState, type TranscriptState } from "@/lib/transcript";
import type { QuartzApi } from "@/useQuartz";

let currentState: TranscriptState = {
  ...initialState,
  turnRunning: true,
  session: {
    ...initialState.session,
    sessionId: "sess-1",
    cwd: "/repo",
  },
};

const stableActions = {
  prompt: vi.fn(),
  steer: vi.fn(),
  cancel: vi.fn(),
  runCommand: vi.fn(),
  newSession: vi.fn(),
  loadSession: vi.fn(),
  trustProject: vi.fn(),
  setSandbox: vi.fn(),
  respondPermission: vi.fn(),
  respondIdleSession: vi.fn(),
  setFormState: vi.fn(),
  clearBgFailed: vi.fn(),
  dismissActiveSession: vi.fn(),
  clearReadActiveSessions: vi.fn(),
  markNotificationsRead: vi.fn(),
  configureWatcher: vi.fn(),
  watcherAction: vi.fn(),
};

vi.mock("@/useQuartz", () => ({
  useQuartz: (): QuartzApi => ({
    state: currentState,
    status: "connected",
    ...stableActions,
  }),
}));

import { SessionTile } from "./SessionTile";

function baseProps(overrides: Partial<React.ComponentProps<typeof SessionTile>> = {}) {
  return {
    tileId: "tile-1",
    tileNumber: 1,
    multiTile: true,
    focused: false,
    maximized: false,
    canClose: true,
    isPrimary: true,
    requestedSessionId: "sess-1",
    createFreshOnConnect: false,
    systemPromptAppend: "",
    richUi: false,
    bots: [],
    botRuns: [],
    runningBotIds: new Set<string>(),
    sharedText: null,
    onSharedTextConsumed: vi.fn(),
    onAuthError: vi.fn(),
    onSessionBound: vi.fn(),
    onHandleUpdate: vi.fn(),
    onHandleUnmount: vi.fn(),
    onReloadSidebar: vi.fn(),
    onFocus: vi.fn(),
    onClose: vi.fn(),
    onToggleMaximize: vi.fn(),
    onPromote: vi.fn(),
    onOpenSettings: vi.fn(),
    onOpenMcp: vi.fn(),
    onOpenPalette: vi.fn(),
    onOpenBotHistory: vi.fn(),
    onEditBot: vi.fn(),
    onRerunBot: vi.fn(),
    ...overrides,
  };
}

describe("SessionTile performance & focus", () => {
  it("does not emit onHandleUpdate when streaming text rows change without session metadata changes", () => {
    const onHandleUpdate = vi.fn();
    const props = baseProps({ onHandleUpdate });
    const { rerender } = render(<SessionTile {...props} />);
    expect(onHandleUpdate).toHaveBeenCalledTimes(1);

    // Simulate a streaming text_delta: rows array changes, session metadata stays identical.
    currentState = {
      ...currentState,
      rows: [{ kind: "assistant", id: "a1", text: "Hello", formState: null }],
    };
    rerender(<SessionTile {...props} />);

    currentState = {
      ...currentState,
      rows: [{ kind: "assistant", id: "a1", text: "Hello world!", formState: null }],
    };
    rerender(<SessionTile {...props} />);

    expect(onHandleUpdate).toHaveBeenCalledTimes(1);
  });

  it("does not steal focus to composer when focused flips via mouse click", () => {
    const props = baseProps({ focused: false, multiTile: true });
    const { rerender } = render(<SessionTile {...props} />);

    const externalButton = document.createElement("button");
    document.body.appendChild(externalButton);
    externalButton.focus();
    expect(document.activeElement).toBe(externalButton);

    rerender(<SessionTile {...props} focused={true} />);
    expect(document.activeElement).toBe(externalButton);

    externalButton.remove();
  });

  it("only invokes onFocus on mousedown when the tile is not already focused", () => {
    const onFocus = vi.fn();
    const props = baseProps({ focused: true, onFocus });
    render(<SessionTile {...props} />);

    fireEvent.mouseDown(screen.getByRole("region", { name: /Tile 1/i }));
    expect(onFocus).not.toHaveBeenCalled();
  });
});
