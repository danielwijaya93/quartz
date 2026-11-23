import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { FileAttachments } from "./FileAttachments";
import { Transcript } from "./Transcript";
import type { Row } from "@/lib/transcript";

describe("FileAttachments", () => {
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
