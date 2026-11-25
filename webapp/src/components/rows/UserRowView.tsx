import * as React from "react";
import { Card } from "@heroui/react";
import { FileSpreadsheet, FileText } from "lucide-react";
import { extractOpenUIUserText } from "@/lib/openui/lang-helpers";
import type { UserRow } from "@/lib/transcript";

interface ParsedAttachment {
  name: string;
  lineCount: number;
}

const ATTACHED_FILE_RE =
  /\[Attached file: ([^\n\]]+)\]\n```\n([\s\S]*?)\n```(?=\n\n\[Attached file: |\s*$)/g;

function countLines(content: string): number {
  if (content === "") return 0;
  return content.replace(/\r?\n$/, "").split(/\r?\n/).length;
}

function parseUserMessage(text: string): {
  body: string;
  attachments: ParsedAttachment[];
} {
  const attachments: ParsedAttachment[] = [];
  const plainText = extractOpenUIUserText(text);
  const body = plainText
    .replace(ATTACHED_FILE_RE, (_match, name: string, content: string) => {
      attachments.push({ name: name.trim(), lineCount: countLines(content) });
      return "";
    })
    .trim();

  return { body, attachments };
}

function isTabularFile(filename: string): boolean {
  return /\.(csv|tsv)$/i.test(filename);
}

/**
 * The user's side of the conversation.
 *
 * A distinct card, right-aligned against a full-width assistant column: the
 * speaker is legible at a glance without a gutter label doing the work.
 */
export function UserRowView({ row }: { row: UserRow }): React.ReactElement {
  const { body, attachments } = React.useMemo(
    () => parseUserMessage(row.text),
    [row.text],
  );

  return (
    <article className="my-2 flex flex-col items-end pl-6">
      <Card variant="secondary" className="max-w-full px-4 py-2.5">
        {body !== "" ? (
          <p className="whitespace-pre-wrap text-[0.9375rem] leading-relaxed text-surface-secondary-foreground">
            {body}
          </p>
        ) : null}
        {attachments.length > 0 ? (
          <ul
            aria-label="Attached files"
            className={body !== "" ? "mt-2 flex flex-wrap gap-2" : "flex flex-wrap gap-2"}
          >
            {attachments.map((file, index) => {
              const Icon = isTabularFile(file.name) ? FileSpreadsheet : FileText;
              const lineLabel = `${file.lineCount} ${file.lineCount === 1 ? "line" : "lines"}`;
              return (
                <li
                  key={`${row.id}-file-${file.name}-${index}`}
                  className="inline-flex max-w-full items-center gap-2 rounded-lg border border-border bg-surface/70 px-2.5 py-1.5 text-xs text-foreground"
                >
                  <Icon aria-hidden="true" className="size-3.5 shrink-0 text-muted" />
                  <span className="max-w-[16rem] truncate font-mono">{file.name}</span>
                  <span className="shrink-0 text-muted">{lineLabel}</span>
                </li>
              );
            })}
          </ul>
        ) : null}
        {row.images.length > 0 ? (
          <ul className="mt-2 flex flex-wrap gap-2">
            {row.images.map((image, index) => (
              <li key={`${row.id}-img-${index}`}>
                <img
                  src={`data:${image.mimeType};base64,${image.data}`}
                  alt={`Attached image ${index + 1}`}
                  className="max-h-40 rounded-md border border-border"
                />
              </li>
            ))}
          </ul>
        ) : null}
      </Card>
    </article>
  );
}
