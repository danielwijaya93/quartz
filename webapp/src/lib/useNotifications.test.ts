import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";

const isNative = { value: false };
vi.mock("./serverUrl", () => ({
  apiUrl: (p: string) => p,
  isNativeCapacitor: () => isNative.value,
}));

const showNativeMock = vi.fn(async () => true);
vi.mock("./nativeNotifications", () => ({
  checkNativePermission: vi.fn(async () => "granted"),
  clearNativeNotifications: vi.fn(async () => undefined),
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
});
