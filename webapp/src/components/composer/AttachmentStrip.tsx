import * as React from "react";
import { FileText, X } from "lucide-react";
import type { ImageAttachment } from "@/protocol";
import type { AttachedFile } from "./composerHelpers";

export function AttachmentStrip({
  images,
  attachedFiles,
  onRemoveImage,
  onRemoveFile,
}: {
  images: readonly ImageAttachment[];
  attachedFiles: readonly AttachedFile[];
  onRemoveImage: (index: number) => void;
  onRemoveFile: (index: number) => void;
}): React.ReactElement | null {
  if (images.length === 0 && attachedFiles.length === 0) return null;

  return (
    <ul className="mb-2 flex flex-wrap gap-2" aria-label="Pending attachments">
      {images.map((image, index) => (
        <li key={`pending-img-${index}`} className="relative">
          <img
            src={`data:${image.mimeType};base64,${image.data}`}
            alt={`Pending attachment ${index + 1}`}
            className="size-14 rounded-lg border border-border object-cover"
          />
          <button
            type="button"
            onClick={() => onRemoveImage(index)}
            aria-label={`Remove attachment ${index + 1}`}
            className="absolute -top-1.5 -right-1.5 inline-flex size-5 cursor-pointer items-center justify-center rounded-full border border-border bg-surface text-muted hover:text-foreground"
          >
            <X aria-hidden="true" className="size-3" />
          </button>
        </li>
      ))}
      {attachedFiles.map((file, index) => (
        <li
          key={`pending-file-${file.name}-${index}`}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-surface-secondary/70 px-2.5 py-1.5 text-xs text-foreground"
        >
          <FileText aria-hidden="true" className="size-3.5 shrink-0 text-muted" />
          <span className="max-w-[12rem] truncate font-mono">{file.name}</span>
          <button
            type="button"
            onClick={() => onRemoveFile(index)}
            aria-label={`Remove file ${file.name}`}
            className="ml-0.5 inline-flex size-4 cursor-pointer items-center justify-center rounded-full text-muted hover:text-foreground"
          >
            <X aria-hidden="true" className="size-3" />
          </button>
        </li>
      ))}
    </ul>
  );
}
