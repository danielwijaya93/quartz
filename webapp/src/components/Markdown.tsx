import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { cn } from "@/lib/utils";

/**
 * Assistant markdown.
 *
 * Streaming-aware: a half-written fenced block would otherwise be re-parsed on
 * every delta and flicker between "code block" and "paragraph of backticks". We
 * close an unterminated fence before rendering, so a partial block renders as a
 * code block from its first character and never changes shape mid-stream.
 */
export function closeUnterminatedFence(markdown: string): string {
  const fences = markdown.match(/^```/gm);
  if (!fences || fences.length % 2 === 0) return markdown;
  const separator = markdown.endsWith("\n") ? "" : "\n";
  return `${markdown}${separator}\`\`\`\n`;
}

export interface MarkdownProps {
  children: string;
  /** True while deltas are still arriving; enables fence closure. */
  streaming?: boolean;
  className?: string;
}

export function Markdown({
  children,
  streaming = false,
  className,
}: MarkdownProps): React.ReactElement {
  const source = streaming ? closeUnterminatedFence(children) : children;
  return (
    <div className={cn("prose-quartz", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]}
        components={{
          // Links always leave the app; this is a local tool, not a browser.
          a: ({ children: linkChildren, href }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {linkChildren}
            </a>
          ),
        }}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}
