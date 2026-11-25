import { describe, expect, it } from "vitest";
import {
  formatNotificationForFrame,
  readNotificationsEnabled,
  setDocumentUnreadBadge,
  writeNotificationsEnabled,
} from "./notifications";

describe("formatNotificationForFrame", () => {
  it("formats turn_end ok and error, and ignores aborted/interrupted", () => {
    expect(
      formatNotificationForFrame({
        type: "turn_end",
        status: "ok",
        tokens: 100,
        elapsedMs: 2450,
      }),
    ).toEqual({
      title: "Quartz: Turn finished",
      body: "Completed in 2.5s.",
      tag: "quartz-turn",
    });

    expect(
      formatNotificationForFrame({
        type: "turn_end",
        status: "error",
        tokens: 100,
        elapsedMs: 500,
        error: "Rate limited",
      }),
    ).toEqual({
      title: "Quartz: Turn failed",
      body: "Rate limited",
      tag: "quartz-turn",
    });

    expect(
      formatNotificationForFrame({
        type: "turn_end",
        status: "aborted",
        tokens: 0,
        elapsedMs: 100,
      }),
    ).toBeNull();

    expect(
      formatNotificationForFrame({
        type: "turn_end",
        status: "waiting_permission",
        tokens: 0,
        elapsedMs: 0,
      }),
    ).toBeNull();
  });

  it("formats permission_request, bash_notice, and error", () => {
    expect(
      formatNotificationForFrame({
        type: "permission_request",
        requestId: "req-1",
        tool: "bash",
        command: "git push",
        cwd: "/repo",
        scope: "remote_git",
      }),
    ).toEqual({
      title: "Quartz: Approval needed",
      body: "Allow git push?",
      tag: "quartz-permission-req-1",
    });

    expect(
      formatNotificationForFrame({
        type: "bash_notice",
        text: "Build finished (exit 0)",
      }),
    ).toEqual({
      title: "Quartz: Background job finished",
      body: "Build finished (exit 0)",
      tag: "quartz-bash",
    });

    expect(
      formatNotificationForFrame({
        type: "error",
        message: "Connection lost",
        fatal: false,
      }),
    ).toEqual({
      title: "Quartz: Error",
      body: "Connection lost",
      tag: "quartz-error",
    });
  });

  it("collapses newlines and truncates long notification bodies to 120 chars", () => {
    const longCommand =
      "python3 -c 'import sys\nprint(123)\n' && " + "echo hello ".repeat(20);
    const perm = formatNotificationForFrame({
      type: "permission_request",
      requestId: "req-long",
      tool: "bash",
      command: longCommand,
      cwd: "/repo",
      scope: "bash",
    });
    expect(perm?.body).not.toContain("\n");
    expect(perm?.body.length).toBeLessThanOrEqual(120);
    expect(perm?.body.endsWith("...")).toBe(true);

    const bash = formatNotificationForFrame({
      type: "bash_notice",
      text: "line 1\n\n  line 2\n" + "x".repeat(150),
    });
    expect(bash?.body.startsWith("line 1 line 2 ")).toBe(true);
    expect(bash?.body.length).toBeLessThanOrEqual(120);
    expect(bash?.body.endsWith("...")).toBe(true);

    const err = formatNotificationForFrame({
      type: "error",
      message: "Traceback:\n  File a.py\n" + "detail ".repeat(30),
      fatal: false,
    });
    expect(err?.body).not.toContain("\n");
    expect(err?.body.length).toBeLessThanOrEqual(120);
    expect(err?.body.endsWith("...")).toBe(true);
  });

  it("ignores streaming deltas", () => {
    expect(
      formatNotificationForFrame({ type: "text_delta", text: "hello" }),
    ).toBeNull();
  });
});

describe("notification preferences and badge", () => {
  it("persists enabled state in localStorage", () => {
    window.localStorage.clear();
    expect(readNotificationsEnabled()).toBe(true);
    writeNotificationsEnabled(false);
    expect(readNotificationsEnabled()).toBe(false);
    writeNotificationsEnabled(true);
    expect(readNotificationsEnabled()).toBe(true);
  });

  it("updates and clears document title unread badge", () => {
    setDocumentUnreadBadge(2);
    expect(document.title).toBe("(2) Quartz");
    setDocumentUnreadBadge(0);
    expect(document.title).toBe("Quartz");
  });
});
