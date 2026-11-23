import { describe, expect, it, vi, beforeEach } from "vitest";

const listeners: Record<string, (arg: unknown) => void> = {};
const removeMocks: ReturnType<typeof vi.fn>[] = [];

vi.mock("@capacitor/app", () => ({
  App: {
    addListener: vi.fn(async (event: string, handler: (arg: unknown) => void) => {
      listeners[event] = handler;
      const remove = vi.fn();
      removeMocks.push(remove);
      return { remove };
    }),
    minimizeApp: vi.fn(async () => undefined),
    exitApp: vi.fn(async () => undefined),
  },
}));

vi.mock("@capacitor/status-bar", () => ({
  StatusBar: { setOverlaysWebView: vi.fn(async () => undefined) },
}));

const isNative = { value: false };
vi.mock("./serverUrl", () => ({ isNativeCapacitor: () => isNative.value }));

const setKeepAliveMock = vi.fn();
vi.mock("./useAndroidKeepAlive", () => ({
  setKeepAlive: (active: boolean) => setKeepAliveMock(active),
}));

const consumeMock = vi.fn(async () => ({ text: "", kind: "" }));
vi.mock("./nativeBridge", () => ({
  NativePlugin: { consumeSharedText: () => consumeMock() },
}));

const { renderHook } = await import("@testing-library/react");
const { useNativeShell } = await import("./useNativeShell");

beforeEach(() => {
  isNative.value = false;
  vi.clearAllMocks();
  for (const key of Object.keys(listeners)) delete listeners[key];
});

describe("useNativeShell", () => {
  it("registers nothing in a browser", async () => {
    renderHook(() => useNativeShell({ turnRunning: false, onBack: () => false, onSharedText: () => {} }));
    expect(setKeepAliveMock).not.toHaveBeenCalled();
    expect(Object.keys(listeners)).toHaveLength(0);
  });

  it("starts the keep-alive only while a turn runs", async () => {
    isNative.value = true;
    const { rerender } = renderHook(
      ({ running }: { running: boolean }) =>
        useNativeShell({ turnRunning: running, onBack: () => false, onSharedText: () => {} }),
      { initialProps: { running: false } },
    );
    expect(setKeepAliveMock).toHaveBeenLastCalledWith(false);
    rerender({ running: true });
    expect(setKeepAliveMock).toHaveBeenLastCalledWith(true);
    rerender({ running: false });
    expect(setKeepAliveMock).toHaveBeenLastCalledWith(false);
  });

  it("lets Back close a dialog before leaving the app", async () => {
    isNative.value = true;
    const onBack = vi.fn(() => true);
    renderHook(() => useNativeShell({ turnRunning: false, onBack, onSharedText: () => {} }));
    listeners.backButton?.({});
    expect(onBack).toHaveBeenCalled();
  });

  it("hands text shared in from another app to the caller", async () => {
    isNative.value = true;
    consumeMock.mockResolvedValueOnce({ text: "https://example.test", kind: "text" });
    const onSharedText = vi.fn();
    renderHook(() =>
      useNativeShell({ turnRunning: false, onBack: () => false, onSharedText }),
    );
    await vi.waitFor(() => expect(onSharedText).toHaveBeenCalledWith("https://example.test"));
  });

  it("ignores an empty share", async () => {
    isNative.value = true;
    consumeMock.mockResolvedValue({ text: "   ", kind: "text" });
    const onSharedText = vi.fn();
    renderHook(() =>
      useNativeShell({ turnRunning: false, onBack: () => false, onSharedText }),
    );
    await Promise.resolve();
    expect(onSharedText).not.toHaveBeenCalled();
  });
});
