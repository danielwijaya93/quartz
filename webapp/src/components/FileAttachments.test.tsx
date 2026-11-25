import { describe, expect, it, vi, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { downloadFilename, FileAttachments } from "./FileAttachments";
import { Transcript } from "./Transcript";
import type { Row } from "@/lib/transcript";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("FileAttachments", () => {
  it("sanitizes download filenames and preserves extension from mimeType", () => {
    expect(downloadFilename("Mockup 1: Clean Sidebar + Bot Run View", "image/png")).toBe(
      "Mockup 1_ Clean Sidebar + Bot Run View.png",
    );
    expect(downloadFilename("quartz-v0.4.0.apk", "application/vnd.android.package-archive")).toBe(
      "quartz-v0.4.0.apk",
    );
  });

  it("renders file cards with download links and image previews", () => {
    render(
      <FileAttachments
        files={[
          {
            id: "apk1",
            name: "quartz-v0.4.0.apk",
            mimeType: "application/vnd.android.package-archive",
            size: 12_582_912,
            url: "/api/files/apk1",
          },
          {
            id: "img1",
            name: "screenshot.png",
            mimeType: "image/png",
            size: 2048,
            url: "/api/files/img1",
          },
        ]}
      />,
    );

    expect(screen.getByText("quartz-v0.4.0.apk")).toBeInTheDocument();
    expect(screen.getByText(/12\.0 MB/)).toBeInTheDocument();
    const apkLink = screen.getByRole("link", { name: "Download quartz-v0.4.0.apk" });
    expect(apkLink).toHaveAttribute("href", "/api/files/apk1");
    expect(apkLink).toHaveAttribute("download", "quartz-v0.4.0.apk");

    const preview = screen.getByAltText("screenshot.png");
    expect(preview).toHaveAttribute("src", "/api/files/img1?inline=1");
  });

  it("opens and closes an in-app image preview lightbox modal", async () => {
    const user = userEvent.setup();
    render(
      <FileAttachments
        files={[
          {
            id: "img1",
            name: "Mockup 1: Clean Sidebar",
            mimeType: "image/png",
            size: 2048,
            url: "/api/files/img1",
          },
        ]}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Preview Mockup 1: Clean Sidebar" }));
    expect(
      screen.getByRole("dialog", { name: "Image preview: Mockup 1: Clean Sidebar" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close preview" }));
    expect(
      screen.queryByRole("dialog", { name: "Image preview: Mockup 1: Clean Sidebar" }),
    ).not.toBeInTheDocument();
  });

  it("hides broken image and marks file unavailable when image fails to load", () => {
    render(
      <FileAttachments
        files={[
          {
            id: "missing1",
            name: "gone.png",
            mimeType: "image/png",
            size: 1024,
            url: "/api/files/missing1",
          },
        ]}
      />,
    );

    const img = screen.getByAltText("gone.png");
    fireEvent.error(img);

    expect(screen.queryByAltText("gone.png")).not.toBeInTheDocument();
    expect(screen.getByText("File no longer available")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Download gone.png" })).not.toBeInTheDocument();
  });

  it("marks file unavailable when download returns 404 instead of downloading json", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ error: "File not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(
      <FileAttachments
        files={[
          {
            id: "missing2",
            name: "report.pdf",
            mimeType: "application/pdf",
            size: 4096,
            url: "/api/files/missing2",
          },
        ]}
      />,
    );

    await user.click(screen.getByRole("link", { name: "Download report.pdf" }));
    await waitFor(() => {
      expect(screen.getByText("File no longer available")).toBeInTheDocument();
    });
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
  });

  it("renders attached files from a turn below the assistant reply in Transcript", () => {
    const rows: Row[] = [
      { kind: "user", id: "u1", text: "give me the excel", images: [] },
      {
        kind: "tool",
        id: "t1",
        code: "attach_file('report.xlsx')",
        status: "ok",
        output: "Attached report.xlsx",
        durationMs: 15,
        images: [],
        files: [
          {
            id: "xlsx1",
            name: "report.xlsx",
            mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size: 4096,
            url: "/api/files/xlsx1",
          },
        ],
        actions: null,
      },
      { kind: "assistant", id: "a1", text: "Here is your spreadsheet.", formState: null },
    ];

    render(
      <Transcript
        rows={rows}
        turnRunning={false}
        richUi={false}
        cwd="/tmp"
        onPrompt={() => {}}
        onFormState={() => {}}
      />,
    );

    expect(screen.getByText("Here is your spreadsheet.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download report.xlsx" })).toBeInTheDocument();
  });
});
