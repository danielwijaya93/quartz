import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

const isNative = { value: false };
vi.mock("./serverUrl", () => ({
  apiUrl: (p: string) => p,
  isNativeCapacitor: () => isNative.value,
}));

const showNativeMock = vi.fn(async () => true);
const clearNativeMock = vi.fn(async () => undefined);
vi.mock("./nativeNotifications", () => ({
  checkNativePermission: vi.fn(async () => "granted"),
  clearNativeNotifications: clearNativeMock,
  ensureNativeChannel: vi.fn(async () => undefined),
  requestNativePermission: vi.fn(async () => "granted"),
  showNativeNotification: showNativeMock,
}));

const { useNotifications } = await import("./useNotifications");

afterEach(() => {
  isNative.value = false;
  vi.clearAllMocks();
});

describe("useNotifications on native Android", () => {
  it("notifies even when the app is in the foreground/active session", async () => {
    isNative.value = true;
    vi.spyOn(document, "hasFocus").mockReturnValue(true);

    const { result } = renderHook(() => useNotifications());
    result.current.notifyFromFrame({
      type: "turn_end",
      status: "ok",
      tokens: 100,
      elapsedMs: 2500,
    });

    await vi.waitFor(() => expect(showNativeMock).toHaveBeenCalledTimes(1));
    expect(showNativeMock).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Quartz: Turn finished" }),
    );
  });

  it("clears native notifications on notifications_cleared frame", async () => {
    isNative.value = true;
    const { result } = renderHook(() => useNotifications());
    result.current.notifyFromFrame({ type: "notifications_cleared" });
    await vi.waitFor(() => expect(clearNativeMock).toHaveBeenCalled());
  });

  it("notifies on unread active_sessions_state when backgrounded", async () => {
    isNative.value = true;
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    vi.spyOn(document, "hasFocus").mockReturnValue(false);

    const { result } = renderHook(() => useNotifications());
    result.current.notifyFromFrame({
      type: "active_sessions_state",
      items: [
        {
          sessionId: "s-1",
          cwd: "/tmp/proj",
          title: "proj",
          kind: "done",
          read: false,
          updatedAt: "2026-01-01T00:00:00Z",
          preview: "Finished refactoring",
        },
      ],
    });

    await vi.waitFor(() => expect(showNativeMock).toHaveBeenCalledTimes(1));
    expect(showNativeMock).toHaveBeenCalledWith(
      expect.objectContaining({ title: "proj", body: "Finished refactoring" }),
    );
  });

  it("syncs enabled state from server via applySyncedEnabled", async () => {
    isNative.value = true;
    const { result } = renderHook(() => useNotifications());
    await act(async () => {
      await result.current.applySyncedEnabled(false);
    });
    expect(result.current.enabled).toBe(false);
    result.current.notifyFromFrame({
      type: "turn_end",
      status: "ok",
      tokens: 100,
      elapsedMs: 1000,
    });
    expect(showNativeMock).not.toHaveBeenCalled();
  });
});
