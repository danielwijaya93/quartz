import * as React from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { Check, Copy } from "lucide-react";
import { cn } from "@heroui/react";

/**
 * Assistant markdown.
 *
 * Streaming-aware: a half-written fenced block would otherwise be re-parsed on
 * every delta and flicker between "code block" and "paragraph of backticks". We
 * close an unterminated fence before rendering, so a partial block renders as a
 * code block from its first character and never changes shape mid-stream.
 *
 * Syntax colours come from the `--code-*` variables in index.css, so one
 * structure serves both themes.
 */
export function closeUnterminatedFence(markdown: string): string {
  const fences = markdown.match(/^```/gm);
  if (!fences || fences.length % 2 === 0) return markdown;
  const separator = markdown.endsWith("\n") ? "" : "\n";
  return `${markdown}${separator}\`\`\`\n`;
}

/** How long "Copied" stays on the button before it offers to copy again. */
const COPIED_MS = 1600;

/**
 * The code as written.
 *
 * Read from the rendered block rather than from the markdown source: highlight.js
 * only adds `<span>`s, so a pre's `textContent` is the original code exactly - and
 * it is still the original on the day a plugin adds line numbers, because those
 * would be nodes, not text.
 */
export function codeText(pre: HTMLElement | null): string {
  // A fence contributes a trailing newline that nothing asked to copy.
  return (pre?.textContent ?? "").replace(/\n$/, "");
}

/**
 * A fenced block, plus the one thing a reader always wants from one.
 *
 * Copying reads the block's text, never its markup, so what lands on the clipboard
 * is runnable code and not a pile of `<span class="hljs-…">`.
 *
 * Success is confirmed twice over: the button says "Copied" for a moment, and a
 * polite live region says it for a screen reader - a changed name on the button
 * under the focus ring is announced by almost nothing. That region is this block's
 * own confirmation, empty until it is used; it is not a second voice for the
 * transcript, whose announcements stay single-sourced in `TranscriptAnnouncer`.
 *
 * A missing or refused Clipboard API (an insecure context, a permission policy)
 * does nothing at all: the block still reads fine, and an error toast over a code
 * block costs more than the copy was worth.
 */
function CodeBlock({ children }: { children?: React.ReactNode }): React.ReactElement {
  const preRef = React.useRef<HTMLPreElement | null>(null);
  const [copied, setCopied] = React.useState(false);
  const timerRef = React.useRef<number | null>(null);

  React.useEffect(
    () => () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    },
    [],
  );

  const handleCopy = React.useCallback(async (): Promise<void> => {
    const clipboard = typeof navigator === "undefined" ? undefined : navigator.clipboard;
    if (clipboard === undefined || typeof clipboard.writeText !== "function") return;
    try {
      await clipboard.writeText(codeText(preRef.current));
    } catch {
      // Denied, or not a secure context. Quietly: the reader still has the code.
      return;
    }
    setCopied(true);
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => setCopied(false), COPIED_MS);
  }, []);

  return (
    <div className="relative">
      <pre ref={preRef}>{children}</pre>
      <button
        type="button"
        onClick={handleCopy}
        data-testid="copy-code"
        className="absolute right-1.5 top-1.5 flex items-center gap-1 rounded-md border border-border bg-background/90 px-1.5 py-1 text-[0.6875rem] font-medium text-muted transition-colors duration-[var(--duration-feedback)] hover:text-foreground"
      >
        {copied ? (
          <Check aria-hidden="true" className="size-3 shrink-0" />
        ) : (
          <Copy aria-hidden="true" className="size-3 shrink-0" />
        )}
        <span>{copied ? "Copied" : "Copy"}</span>
        {/* Part of the accessible name, not an aria-label: the visible word stays
            the start of it, so a speech-input user can still say "click Copy". */}
        <span className="sr-only"> code</span>
      </button>
      <span role="status" aria-live="polite" className="sr-only">
        {copied ? "Copied to clipboard." : ""}
      </span>
    </div>
  );
}

/**
 * Links always leave the app; this is a local tool, not a browser.
 *
 * Hoisted with the rest of `COMPONENTS` so the renderer is not handed a new
 * component type on every delta - a new type would remount the link, and the code
 * block's "Copied" state with it.
 */
function ExternalLink({
  children,
  href,
}: {
  children?: React.ReactNode;
  href?: string;
}): React.ReactElement {
  return (
    <a href={href} target="_blank" rel="noreferrer noopener">
      {children}
    </a>
  );
}

const COMPONENTS: Components = {
  a: ExternalLink,
  pre: CodeBlock,
};

const REMARK_PLUGINS = [remarkGfm];
// Highlighting with language auto-detection runs all bundled grammars on every
// untagged code block. Keep auto-detect for settled markdown, but skip it while
// tokens are actively streaming (explicitly tagged fences still highlight).
const REHYPE_PLUGINS_SETTLED: [typeof rehypeHighlight, { detect: boolean; ignoreMissing: boolean }][] = [
  [rehypeHighlight, { detect: true, ignoreMissing: true }],
];
const REHYPE_PLUGINS_STREAMING: [typeof rehypeHighlight, { detect: boolean; ignoreMissing: boolean }][] = [
  [rehypeHighlight, { detect: false, ignoreMissing: true }],
];

/**
 * Split streaming markdown into top-level blocks separated by blank lines while
 * never splitting inside a fenced code block (``` or ~~~). Completed earlier
 * blocks stay referentially stable so React.memo skips re-parsing them on every
 * incoming token delta.
 */
export function splitMarkdownBlocks(markdown: string): string[] {
  if (!markdown) return [];
  const lines = markdown.split("\n");
  const blocks: string[] = [];
  let current: string[] = [];
  let fenceMarker: string | null = null;

  for (const line of lines) {
    const trimmed = line.trimStart();
    if (fenceMarker === null) {
      if (trimmed.startsWith("```")) {
        fenceMarker = "```";
      } else if (trimmed.startsWith("~~~")) {
        fenceMarker = "~~~";
      }
    } else if (trimmed.startsWith(fenceMarker)) {
      fenceMarker = null;
    }

    if (fenceMarker === null && line.trim() === "") {
      if (current.length > 0) {
        blocks.push(current.join("\n"));
        current = [];
      }
    } else {
      current.push(line);
    }
  }
  if (current.length > 0) {
    blocks.push(current.join("\n"));
  }
  return blocks;
}

const MarkdownBlock = React.memo(function MarkdownBlock({
  source,
  streaming,
}: {
  source: string;
  streaming: boolean;
}): React.ReactElement {
  return (
    <ReactMarkdown
      remarkPlugins={REMARK_PLUGINS}
      rehypePlugins={streaming ? REHYPE_PLUGINS_STREAMING : REHYPE_PLUGINS_SETTLED}
      components={COMPONENTS}
    >
      {source}
    </ReactMarkdown>
  );
});

export interface MarkdownProps {
  children: string;
  /** True while deltas are still arriving; enables fence closure. */
  streaming?: boolean;
  className?: string;
}

export const Markdown = React.memo(function Markdown({
  children,
  streaming = false,
  className,
}: MarkdownProps): React.ReactElement {
  const blocks = React.useMemo(
    () => (streaming ? splitMarkdownBlocks(closeUnterminatedFence(children)) : null),
    [children, streaming],
  );

  if (blocks !== null) {
    return (
      <div className={cn("transcript-prose", className)}>
        {blocks.map((block, index) => (
          <MarkdownBlock key={index} source={block} streaming={true} />
        ))}
      </div>
    );
  }

  return (
    <div className={cn("transcript-prose", className)}>
      <MarkdownBlock source={children} streaming={false} />
    </div>
  );
});
