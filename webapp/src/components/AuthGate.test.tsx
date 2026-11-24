import { render, screen, waitFor } from "@testing-library/react";
import { userEvent } from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearWebToken, getWebToken } from "@/lib/authToken";
import { AuthGate } from "./AuthGate";

beforeEach(() => {
  clearWebToken();
  vi.restoreAllMocks();
});

afterEach(() => {
  clearWebToken();
  vi.restoreAllMocks();
});

describe("AuthGate", () => {
  it("renders the token login form", () => {
    render(<AuthGate onAuthenticated={() => {}} />);
    expect(screen.getByRole("heading", { name: "Sign in to Quartz" })).toBeInTheDocument();
    expect(screen.getByLabelText("Web auth token")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Unlock" })).toBeInTheDocument();
  });

  it("shows an error when the server rejects an invalid token", async () => {
    const user = userEvent.setup();
    const onAuthenticated = vi.fn();
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ error: "Invalid token" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(<AuthGate onAuthenticated={onAuthenticated} />);
    await user.type(screen.getByLabelText("Web auth token"), "wrong-token");
    await user.click(screen.getByRole("button", { name: "Unlock" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid token");
    expect(onAuthenticated).not.toHaveBeenCalled();
    expect(getWebToken()).toBeNull();
  });

  it("stores the token and calls onAuthenticated on valid login", async () => {
    const user = userEvent.setup();
    const onAuthenticated = vi.fn();
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(<AuthGate onAuthenticated={onAuthenticated} />);
    await user.type(screen.getByLabelText("Web auth token"), "valid-secret-token");
    await user.click(screen.getByRole("button", { name: "Unlock" }));

    await waitFor(() => expect(onAuthenticated).toHaveBeenCalledTimes(1));
    expect(getWebToken()).toBe("valid-secret-token");
  });
});
