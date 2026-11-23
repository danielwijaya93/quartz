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
