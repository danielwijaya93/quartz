import { beforeEach, describe, expect, it, vi } from "vitest";

const isNative = { value: false };
vi.mock("../lib/serverUrl", () => ({ isNativeCapacitor: () => isNative.value }));

const authMock = vi.fn(async () => ({ ok: true }));
vi.mock("../lib/nativeBridge", () => ({
  NativePlugin: { authenticate: () => authMock() },
}));

const { render, screen, waitFor } = await import("@testing-library/react");
const { userEvent } = await import("@testing-library/user-event");
const { BiometricGate, readBiometricLockEnabled, writeBiometricLockEnabled } = await import(
  "./BiometricGate"
);

beforeEach(() => {
  isNative.value = false;
  localStorage.clear();
  vi.clearAllMocks();
});

describe("BiometricGate", () => {
  it("renders children unchanged in a browser", () => {
    render(<BiometricGate><p>secret</p></BiometricGate>);
    expect(screen.getByText("secret")).toBeInTheDocument();
  });

  it("does not lock when the switch is off", () => {
    isNative.value = true;
    writeBiometricLockEnabled(false);
    render(<BiometricGate><p>secret</p></BiometricGate>);
    expect(screen.getByText("secret")).toBeInTheDocument();
    expect(authMock).not.toHaveBeenCalled();
  });

  it("locks on launch and reveals the app after a successful prompt", async () => {
    isNative.value = true;
    writeBiometricLockEnabled(true);
    render(<BiometricGate><p>secret</p></BiometricGate>);
    expect(screen.getByText("Quartz is locked")).toBeInTheDocument();
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("secret")).toBeInTheDocument());
  });

  it("stays locked and offers a retry when the prompt is dismissed", async () => {
    isNative.value = true;
    authMock.mockResolvedValue({ ok: false });
    writeBiometricLockEnabled(true);
    render(<BiometricGate><p>secret</p></BiometricGate>);
    await waitFor(() => expect(screen.getByText(/Not unlocked/)).toBeInTheDocument());
    expect(screen.queryByText("secret")).not.toBeInTheDocument();

    authMock.mockResolvedValue({ ok: true });
    await userEvent.click(screen.getByRole("button", { name: "Unlock" }));
    await waitFor(() => expect(screen.getByText("secret")).toBeInTheDocument());
  });

  it("keeps the preference in storage", () => {
    expect(readBiometricLockEnabled()).toBe(false);
    writeBiometricLockEnabled(true);
    expect(readBiometricLockEnabled()).toBe(true);
  });
});
