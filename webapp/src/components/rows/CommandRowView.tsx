import * as React from "react";
import { AlertTriangle, Check, Copy, List, MonitorSmartphone, X } from "lucide-react";
import { Markdown } from "../Markdown";
import { RowFrame } from "./RowFrame";
import type { CommandResult } from "@/protocol";
import type { CommandRow } from "@/lib/transcript";

function CopyableCommand({ command }: { command: string }): React.ReactElement {
  const [copied, setCopied] = React.useState(false);

  const copy = React.useCallback(() => {
    void navigator.clipboard?.writeText(command).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      },
      () => setCopied(false),
    );
  }, [command]);

  return (
    <div className="mt-1 flex items-center gap-2">
      <code className="min-w-0 flex-1 truncate rounded-[3px] border border-line bg-surface px-2 py-1 font-mono text-xs text-ink">
        {command}
      </code>
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? "Copied" : `Copy ${command}`}
        className="inline-flex size-6 shrink-0 items-center justify-center rounded-[var(--radius-cell)] text-ink-muted transition-colors duration-[var(--duration-feedback)] hover:bg-elevated hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent"
      >
        {copied ? (
          <Check aria-hidden="true" className="size-3 text-ok" />
        ) : (
          <Copy aria-hidden="true" className="size-3" />
        )}
      </button>
    </div>
  );
}

function CommandBody({
  name,
  result,
}: {
  name: string;
  result: CommandResult;
}): React.ReactElement {
  switch (result.kind) {
    case "text":
      return (
        <div>
          {result.title ? (
            <p className="mb-0.5 font-mono text-2xs uppercase tracking-[0.08em] text-ink-muted">
              {result.title}
            </p>
          ) : null}
          {result.body ? <Markdown>{result.body}</Markdown> : null}
        </div>
      );

    case "list": {
      const items = result.items ?? [];
      if (items.length === 0) {
        return <p className="font-mono text-xs text-ink-dim">(empty)</p>;
      }
      return (
        <div>
          {result.title ? (
            <p className="mb-0.5 font-mono text-2xs uppercase tracking-[0.08em] text-ink-muted">
              {result.title}
            </p>
          ) : null}
          <ul className="divide-y divide-line border border-line">
            {items.map((item, index) => (
              <li
                key={`${item.label}-${index}`}
                className="flex items-baseline gap-2 px-2 py-1 text-xs"
              >
                <List aria-hidden="true" className="size-3 shrink-0 text-ink-dim" />
                <span className="font-mono text-ink">{item.label}</span>
                {item.detail ? (
                  <span className="ml-auto shrink-0 font-mono text-2xs text-ink-dim">
                    {item.detail}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      );
    }

    case "error":
      return (
        <div className="flex items-start gap-2">
          <AlertTriangle aria-hidden="true" className="mt-0.5 size-3 shrink-0 text-danger" />
          <p className="whitespace-pre-wrap font-mono text-xs text-ink">
            {result.message || "Command failed."}
          </p>
        </div>
      );

    case "needs_terminal":
      // Deliberately not an error: this command works, it just needs a TTY.
      return (
        <div className="border border-line bg-surface px-2 py-1.5">
          <div className="flex items-center gap-1.5">
            <MonitorSmartphone aria-hidden="true" className="size-3 shrink-0 text-warn" />
            <p className="font-mono text-xs text-ink">Run this in the terminal</p>
          </div>
          <p className="mt-0.5 text-2xs text-ink-muted">
            {result.message || `/${name} needs an interactive terminal.`}
          </p>
          {/* Copy what the user must actually run. `quartz` is already running —
              the browser is talking to it — so the useful clipboard payload is the
              slash command itself, and the label says exactly that. */}
          <CopyableCommand command={`/${name}`} />
        </div>
      );

    default:
      return <p className="font-mono text-xs text-ink-dim">(unrecognised result)</p>;
  }
}

export function CommandRowView({ row }: { row: CommandRow }): React.ReactElement {
  return (
    <RowFrame
      label="command"
      tone={row.ok ? "plain" : "danger"}
      meta={
        <span className="inline-flex items-center gap-1.5">
          <span>/{row.name}</span>
          {row.ok ? (
            <Check aria-hidden="true" className="size-3 text-ok" />
          ) : (
            <X aria-hidden="true" className="size-3 text-danger" />
          )}
        </span>
      }
    >
      <div className="space-y-1">
        <CommandBody name={row.name} result={row.result} />
      </div>
    </RowFrame>
  );
}
