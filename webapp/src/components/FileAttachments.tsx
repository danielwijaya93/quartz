import * as React from "react";
import {
  AlertCircle,
  Download,
  File as FileIcon,
  FileArchive,
  FileImage,
  FileSpreadsheet,
  FileText,
  Maximize2,
  Smartphone,
  X,
} from "lucide-react";
import { authFetch, withAuthToken } from "@/lib/authToken";
import { apiUrl, isNativeCapacitor } from "@/lib/serverUrl";
import type { FileAttachment } from "@/protocol";

const MIME_EXTENSION: Record<string, string> = {
  "image/png": ".png",
  "image/jpeg": ".jpg",
  "image/webp": ".webp",
  "image/gif": ".gif",
  "application/pdf": ".pdf",
  "text/plain": ".txt",
  "text/csv": ".csv",
  "application/json": ".json",
  "application/zip": ".zip",
  "application/vnd.android.package-archive": ".apk",
};

export function downloadFilename(name: string, mimeType: string): string {
  const cleaned = name
    .trim()
    .replace(/[:/\\*?"<>|\x00-\x1f]+/g, "_")
    .replace(/^[. ]+|[. ]+$/g, "");
  const base = cleaned || "download";
  if (/\.[a-z0-9]{1,10}$/i.test(base)) return base;
  const essence = mimeType.split(";", 1)[0]?.trim().toLowerCase() ?? "";
  return `${base}${MIME_EXTENSION[essence] ?? ""}`;
}

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

function ImagePreviewModal({
  file,
  inlineHref,
  downloadHref,
  downloadName,
  native,
  onDownload,
  onClose,
}: {
  file: FileAttachment;
  inlineHref: string;
  downloadHref: string;
  downloadName: string;
  native: boolean;
  onDownload: (event: React.MouseEvent<HTMLAnchorElement>) => void;
  onClose: () => void;
}): React.ReactElement {
  React.useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const sizeLabel = formatBytes(file.size);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Image preview: ${file.name}`}
      className="fixed inset-0 z-50 flex flex-col bg-black/85 p-3 backdrop-blur-xs sm:p-6"
      onClick={onClose}
    >
      <div
        className="mx-auto flex w-full max-w-5xl items-center justify-between gap-3 pb-3 text-white"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="min-w-0">
          <p className="truncate text-sm font-medium">{file.name}</p>
          {sizeLabel ? <p className="text-xs text-white/70">{sizeLabel}</p> : null}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <a
            href={downloadHref}
            download={native ? undefined : downloadName}
            target={native ? "_blank" : undefined}
            rel={native ? "noopener noreferrer" : undefined}
            onClick={onDownload}
            aria-label={`Download ${file.name} from preview`}
            className="inline-flex items-center gap-1.5 rounded-lg border border-white/20 bg-white/10 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-white/20"
          >
            <Download aria-hidden="true" className="size-3.5" />
            <span>Download</span>
          </a>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close preview"
            className="inline-flex size-8 items-center justify-center rounded-lg border border-white/20 bg-white/10 text-white transition hover:bg-white/20"
          >
            <X aria-hidden="true" className="size-4" />
          </button>
        </div>
      </div>
      <div
        className="flex min-h-0 flex-1 items-center justify-center"
        onClick={onClose}
      >
        <img
          src={inlineHref}
          alt={file.name}
          onClick={(event) => event.stopPropagation()}
          className="max-h-full max-w-full rounded-lg object-contain shadow-2xl"
        />
      </div>
    </div>
  );
}

function AttachmentCard({
  file,
  compact,
  native,
}: {
  file: FileAttachment;
  compact: boolean;
  native: boolean;
}): React.ReactElement {
  const [unavailable, setUnavailable] = React.useState(false);
  const [downloading, setDownloading] = React.useState(false);
  const [previewOpen, setPreviewOpen] = React.useState(false);

  const downloadHref = withAuthToken(apiUrl(file.url));
  const isImage = file.mimeType.startsWith("image/");
  const inlineHref = withAuthToken(
    apiUrl(file.url.includes("?") ? `${file.url}&inline=1` : `${file.url}?inline=1`),
  );
  const dlName = downloadFilename(file.name, file.mimeType);
  const sizeLabel = formatBytes(file.size);

  const handleDownload = React.useCallback(
    (event: React.MouseEvent<HTMLAnchorElement>) => {
      if (native) {
        event.preventDefault();
        window.open(downloadHref, "_blank", "noopener,noreferrer");
        return;
      }
      event.preventDefault();
      if (downloading || unavailable) return;
      setDownloading(true);
      void authFetch(downloadHref)
        .then(async (res) => {
          if (!res.ok) {
            setUnavailable(true);
            setPreviewOpen(false);
            return;
          }
          const blob = await res.blob();
          const objectUrl = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = objectUrl;
          link.download = dlName;
          document.body.appendChild(link);
          link.click();
          link.remove();
          window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
        })
        .catch(() => {
          setUnavailable(true);
          setPreviewOpen(false);
        })
        .finally(() => {
          setDownloading(false);
        });
    },
    [dlName, downloadHref, downloading, native, unavailable],
  );

  return (
    <li className="flex max-w-full flex-col gap-2 rounded-xl border border-border bg-surface-secondary/70 p-2.5">
      {isImage && !compact && !unavailable ? (
        <button
          type="button"
          onClick={() => setPreviewOpen(true)}
          aria-label={`Preview ${file.name}`}
          className="group relative block overflow-hidden rounded-lg border border-border bg-background text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          <img
            src={inlineHref}
            alt={file.name}
            onError={() => setUnavailable(true)}
            className="max-h-56 w-auto object-contain transition group-hover:opacity-95"
            loading="lazy"
          />
          <span className="pointer-events-none absolute right-2 bottom-2 inline-flex items-center gap-1 rounded-md bg-black/65 px-2 py-1 text-[11px] font-medium text-white opacity-0 transition group-hover:opacity-100 group-focus-visible:opacity-100">
            <Maximize2 aria-hidden="true" className="size-3" />
            <span>Preview</span>
          </span>
        </button>
      ) : null}

      <div className="flex items-center gap-2.5">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-border bg-background">
          {unavailable ? (
            <AlertCircle aria-hidden="true" className="size-4 text-danger" />
          ) : (
            <FileGlyph name={file.name} mimeType={file.mimeType} />
          )}
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-xs font-medium text-foreground" title={file.name}>
            {file.name}
          </p>
          <p className="truncate text-[11px] text-muted">
            {unavailable
              ? "File no longer available"
              : `${sizeLabel ? `${sizeLabel} · ` : ""}${file.mimeType}`}
          </p>
        </div>
        {unavailable ? (
          <span className="inline-flex shrink-0 items-center rounded-lg border border-border bg-background/60 px-2.5 py-1.5 text-xs font-medium text-muted">
            Unavailable
          </span>
        ) : (
          <a
            href={downloadHref}
            download={native ? undefined : dlName}
            target={native ? "_blank" : undefined}
            rel={native ? "noopener noreferrer" : undefined}
            onClick={handleDownload}
            aria-label={`Download ${file.name}`}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border bg-background px-2.5 py-1.5 text-xs font-medium text-foreground transition hover:bg-surface-secondary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            <Download aria-hidden="true" className="size-3.5" />
            <span>{downloading ? "Downloading..." : "Download"}</span>
          </a>
        )}
      </div>

      {previewOpen && !unavailable ? (
        <ImagePreviewModal
          file={file}
          inlineHref={inlineHref}
          downloadHref={downloadHref}
          downloadName={dlName}
          native={native}
          onDownload={handleDownload}
          onClose={() => setPreviewOpen(false)}
        />
      ) : null}
    </li>
  );
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
      {files.map((file) => (
        <AttachmentCard key={file.id} file={file} compact={compact} native={native} />
      ))}
    </ul>
  );
}
