import { afterEach, describe, expect, it, vi } from "vitest";

type ScheduleArgs = { notifications: { id: number; title: string; body: string }[] };
const scheduleMock = vi.fn(async (_args: ScheduleArgs) => ({ notifications: [] as unknown[] }));
const removeAllMock = vi.fn(async () => undefined);
const checkMock = vi.fn(async () => ({ display: "granted" as string }));
const requestMock = vi.fn(async () => ({ display: "granted" as string }));

vi.mock("@capacitor/local-notifications", () => ({
  LocalNotifications: {
    schedule: scheduleMock,
    removeAllDeliveredNotifications: removeAllMock,
    checkPermissions: checkMock,
    requestPermissions: requestMock,
  },
}));

const isNative = { value: false };
vi.mock("./serverUrl", () => ({
  isNativeCapacitor: () => isNative.value,
}));

const { showNativeNotification, requestNativePermission, clearNativeNotifications } = await import(
  "./nativeNotifications"
);

afterEach(() => {
  isNative.value = false;
  vi.clearAllMocks();
});

describe("native notifications", () => {
  it("does nothing in a browser", async () => {
    const shown = await showNativeNotification({ title: "t", body: "b", tag: "x" });
    expect(shown).toBe(false);
    expect(scheduleMock).not.toHaveBeenCalled();
  });

  it("schedules one local notification with a stable id per tag", async () => {
    isNative.value = true;
    const shown = await showNativeNotification({
      title: "Quartz: Turn finished",
      body: "Completed in 12.0s.",
      tag: "quartz-turn",
    });
    expect(shown).toBe(true);
    const args = scheduleMock.mock.calls[0]![0];
    expect(args.notifications[0]?.title).toBe("Quartz: Turn finished");

    // The same tag must map to the same id, or a turn would stack notifications.
    await showNativeNotification({ title: "again", body: "again", tag: "quartz-turn" });
    const second = scheduleMock.mock.calls[1]![0];
    expect(second.notifications[0]?.id).toBe(args.notifications[0]?.id);
  });

  it("reports a failed schedule instead of throwing", async () => {
    isNative.value = true;
    scheduleMock.mockRejectedValueOnce(new Error("no permission"));
    await expect(
      showNativeNotification({ title: "t", body: "b", tag: "t" }),
    ).resolves.toBe(false);
  });

  it("returns granted when permission is already held", async () => {
    isNative.value = true;
    await expect(requestNativePermission()).resolves.toBe("granted");
    expect(requestMock).not.toHaveBeenCalled();
  });

  it("asks for permission when it is not held yet", async () => {
    isNative.value = true;
    checkMock.mockResolvedValueOnce({ display: "prompt" });
    await expect(requestNativePermission()).resolves.toBe("granted");
    expect(requestMock).toHaveBeenCalled();
  });

  it("reports unsupported outside a native shell", async () => {
    await expect(requestNativePermission()).resolves.toBe("unsupported");
  });

  it("clears delivered notifications on return to the app", async () => {
    isNative.value = true;
    await clearNativeNotifications();
    expect(removeAllMock).toHaveBeenCalled();
  });

  it("swallows a failing clear", async () => {
    isNative.value = true;
    removeAllMock.mockRejectedValueOnce(new Error("nope"));
    await expect(clearNativeNotifications()).resolves.toBeUndefined();
  });
});