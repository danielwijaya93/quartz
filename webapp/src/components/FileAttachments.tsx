import * as React from "react";
import {
  Download,
  File as FileIcon,
  FileArchive,
  FileImage,
  FileSpreadsheet,
  FileText,
  Smartphone,
} from "lucide-react";
import { apiUrl, isNativeCapacitor } from "@/lib/serverUrl";
import type { FileAttachment } from "@/protocol";

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function FileGlyph({ name, mimeType }: { name: string; mimeType: string }): React.ReactElement {
  const lower = name.toLowerCase();
  if (mimeType.startsWith("image/")) {
    return <FileImage aria-hidden="true" className="size-4 text-accent" />;
  }
  if (lower.endsWith(".apk") || mimeType === "application/vnd.android.package-archive") {
    return <Smartphone aria-hidden="true" className="size-4 text-success" />;
  }
  if (
    lower.endsWith(".xlsx") ||
    lower.endsWith(".xls") ||
    lower.endsWith(".csv") ||
    mimeType.includes("spreadsheet")
  ) {
    return <FileSpreadsheet aria-hidden="true" className="size-4 text-success" />;
  }
  if (
    lower.endsWith(".zip") ||
    lower.endsWith(".tar") ||
    lower.endsWith(".gz") ||
    lower.endsWith(".7z")
  ) {
    return <FileArchive aria-hidden="true" className="size-4 text-warning" />;
  }
  if (
    lower.endsWith(".pdf") ||
    lower.endsWith(".doc") ||
    lower.endsWith(".docx") ||
    lower.endsWith(".ppt") ||
    lower.endsWith(".pptx") ||
    lower.endsWith(".txt") ||
    lower.endsWith(".md")
  ) {
    return <FileText aria-hidden="true" className="size-4 text-accent" />;
  }
  return <FileIcon aria-hidden="true" className="size-4 text-muted" />;
}

export function FileAttachments({
  files,
  compact = false,
}: {
  files: readonly FileAttachment[];
  compact?: boolean;
}): React.ReactElement | null {
  if (files.length === 0) return null;
  const native = isNativeCapacitor();

  return (
    <ul
      aria-label="Attached files"
      className={compact ? "flex flex-wrap gap-2" : "mt-2 flex flex-wrap gap-2"}
    >
      {files.map((file) => {
        const downloadHref = apiUrl(file.url);
        const isImage = file.mimeType.startsWith("image/");
        const inlineHref = apiUrl(
          file.url.includes("?") ? `${file.url}&inline=1` : `${file.url}?inline=1`,
        );
        const sizeLabel = formatBytes(file.size);

        return (
          <li
            key={file.id}
            className="flex max-w-full flex-col gap-2 rounded-xl border border-border bg-surface-secondary/70 p-2.5"
          >
            {isImage && !compact ? (
              <a
                href={inlineHref}
                target="_blank"
                rel="noopener noreferrer"
                className="block overflow-hidden rounded-lg border border-border bg-background"
              >
                <img
                  src={inlineHref}
                  alt={file.name}
                  className="max-h-56 w-auto object-contain"
                  loading="lazy"
                />
              </a>
            ) : null}
            <div className="flex items-center gap-2.5">
              <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-border bg-background">
                <FileGlyph name={file.name} mimeType={file.mimeType} />
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-xs font-medium text-foreground" title={file.name}>
                  {file.name}
                </p>
                <p className="truncate text-[11px] text-muted">
                  {sizeLabel ? `${sizeLabel} · ` : ""}
                  {file.mimeType}
                </p>
              </div>
              <a
                href={downloadHref}
                download={native ? undefined : file.name}
                target={native ? "_blank" : undefined}
                rel={native ? "noopener noreferrer" : undefined}
                onClick={
                  native
                    ? (event) => {
                        event.preventDefault();
                        window.open(downloadHref, "_blank", "noopener,noreferrer");
                      }
                    : undefined
                }
                aria-label={`Download ${file.name}`}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border bg-background px-2.5 py-1.5 text-xs font-medium text-foreground transition hover:bg-surface-secondary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
              >
                <Download aria-hidden="true" className="size-3.5" />
                <span>Download</span>
              </a>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
