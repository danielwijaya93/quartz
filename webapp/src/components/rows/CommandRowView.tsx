import * as React from "react";
import { Alert, Button, Card, Chip, Separator } from "@heroui/react";
import { Check, Copy, Terminal } from "lucide-react";
import { Markdown } from "../Markdown";
import { parseCommandText, type CommandTextRow } from "@/lib/commandText";
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
    <div className="flex items-center gap-2">
      <code className="min-w-0 flex-1 truncate rounded-md border border-border bg-background px-2.5 py-1.5 font-mono text-xs">
        {command}
      </code>
      <Button
        size="sm"
        variant="outline"
        onPress={copy}
        aria-label={copied ? "Copied" : `Copy ${command}`}
      >
        {copied ? <Check aria-hidden="true" className="size-3.5" /> : <Copy aria-hidden="true" className="size-3.5" />}
        {copied ? "Copied" : "Copy"}
      </Button>
    </div>
  );
}

/** A two-column block: label left, description right, one row per item. */
function ColumnList({ rows }: { rows: readonly CommandTextRow[] }): React.ReactElement {
  return (
    <ul className="divide-y divide-separator rounded-md border border-border">
      {rows.map((row, index) => (
        <li
          key={`${row.label}-${index}`}
          className="flex flex-col gap-0.5 px-3 py-1.5 sm:flex-row sm:items-baseline sm:gap-3"
        >
          <span className="shrink-0 font-mono text-xs font-medium text-foreground">{row.label}</span>
          {row.detail !== "" ? (
            <span className="text-xs text-muted sm:ml-auto sm:text-right">{row.detail}</span>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

/**
 * `kind: "text"` bodies.
 *
 * Some of them (notably `/help`) are really tables, and markdown would flatten
 * them into one run-on paragraph. `parseCommandText` recognises that shape and
 * this renders it as a list; everything else is ordinary markdown.
 */
function TextBody({ body }: { body: string }): React.ReactElement {
  const parsed = React.useMemo(() => parseCommandText(body), [body]);

  if (parsed === null) return <Markdown>{body}</Markdown>;

  return (
    <div className="space-y-3">
      {parsed.intro !== null ? <Markdown>{parsed.intro}</Markdown> : null}
      {parsed.sections.map((section, index) => (
        <div key={`${section.heading ?? "section"}-${index}`} className="space-y-1.5">
          {section.heading !== null ? (
            <p className="text-xs font-semibold tracking-wide text-muted uppercase">
              {section.heading}
            </p>
          ) : null}
          <ColumnList rows={section.rows} />
        </div>
      ))}
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
        <div className="space-y-2">
          {result.title ? (
            <p className="text-xs font-semibold tracking-wide text-muted uppercase">
              {result.title}
            </p>
          ) : null}
          {result.body ? <TextBody body={result.body} /> : null}
        </div>
      );

    case "list": {
      const items = (result.items ?? []).map((item, index) => ({
        label: item.label,
        detail: item.detail ?? "",
        key: `${item.label}-${index}`,
      }));
      return (
        <div className="space-y-2">
          {result.title ? (
            <p className="text-xs font-semibold tracking-wide text-muted uppercase">
              {result.title}
            </p>
          ) : null}
          {items.length === 0 ? (
            <p className="text-sm text-muted">Nothing to show.</p>
          ) : (
            <ColumnList rows={items} />
          )}
        </div>
      );
    }

    case "error":
      return (
        <Alert status="danger" className="rounded-lg p-3">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>/{name} failed</Alert.Title>
            <Alert.Description>
              <span className="text-sm">{result.message || "The command did not complete."}</span>
            </Alert.Description>
          </Alert.Content>
        </Alert>
      );

    case "needs_terminal":
      // Deliberately not an error: this command works, it just needs a TTY.
      return (
        <Card variant="secondary" className="gap-2 rounded-lg px-3.5 py-3">
          <div className="flex items-center gap-2">
            <Terminal aria-hidden="true" className="size-4 shrink-0 text-warning" />
            <p className="text-sm font-medium">Run this in the terminal</p>
          </div>
          <p className="text-xs text-muted">
            {result.message || `/${name} needs an interactive terminal.`}
          </p>
          {/* Copy what the user must actually run. `quartz` is already running -
              the browser is talking to it - so the useful clipboard payload is
              the slash command itself, and the label says exactly that. */}
          <CopyableCommand command={`/${name}`} />
        </Card>
      );

    default:
      return <p className="text-sm text-muted">This command returned a result this client cannot show.</p>;
  }
}

export function CommandRowView({ row }: { row: CommandRow }): React.ReactElement {
  return (
    <Card className="gap-2 rounded-xl px-4 py-3">
      <header className="flex items-center gap-2">
        <span className="font-mono text-sm font-medium text-foreground">/{row.name}</span>
        <Chip size="sm" variant="soft" color={row.ok ? "success" : "danger"} className="ml-auto">
          {row.ok ? "Done" : "Failed"}
        </Chip>
      </header>
      <Separator />
      <CommandBody name={row.name} result={row.result} />
    </Card>
  );
}
