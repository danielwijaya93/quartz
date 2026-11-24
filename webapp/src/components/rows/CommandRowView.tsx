import * as React from "react";
import { authFetch } from "../../lib/authToken";
import { apiUrl } from "../../lib/serverUrl";
import { Alert, Button, Card, Chip, Input, Separator } from "@heroui/react";
import { Check, Copy, Eye, RotateCcw, Square, Terminal } from "lucide-react";
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

interface ParsedSetting {
  key: string;
  argName: string;
  label: string;
  value: string;
  hint: string;
}

const SETTING_META: Record<string, { argName: string; label: string }> = {
  reserve_tokens: { argName: "reserve", label: "Reserve tokens" },
  keep_recent_tokens: { argName: "keep", label: "Keep recent tokens" },
  progress_every: { argName: "progress", label: "Progress update interval (turns)" },
  background_check_every: { argName: "bg", label: "Background check interval (seconds)" },
};

function parseSettingsBody(body: string): { settings: ParsedSetting[]; storedIn: string | null } | null {
  const settings: ParsedSetting[] = [];
  let storedIn: string | null = null;
  for (const rawLine of body.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    if (line.startsWith("Stored in ")) {
      storedIn = line;
      continue;
    }
    const match = /^([a-z_]+):\s*([0-9,]+)s?\s*\(([^)]+)\)$/.exec(line);
    if (!match) continue;
    const key = match[1];
    const rawVal = match[2];
    const hint = match[3];
    if (!key || !rawVal || !hint) continue;
    const meta = SETTING_META[key];
    if (!meta) continue;
    settings.push({
      key,
      argName: meta.argName,
      label: meta.label,
      value: rawVal.replaceAll(",", ""),
      hint,
    });
  }
  return settings.length > 0 ? { settings, storedIn } : null;
}

function InteractiveSettingsBody({
  body,
  onRunCommand,
}: {
  body: string;
  onRunCommand?: (name: string, arg?: string) => void;
}): React.ReactElement {
  const parsed = React.useMemo(() => parseSettingsBody(body), [body]);
  const [drafts, setDrafts] = React.useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    if (parsed) {
      for (const s of parsed.settings) init[s.key] = s.value;
    }
    return init;
  });

  React.useEffect(() => {
    if (!parsed) return;
    const next: Record<string, string> = {};
    for (const s of parsed.settings) next[s.key] = s.value;
    setDrafts(next);
  }, [parsed]);

  if (!parsed) return <TextBody body={body} />;

  const saveOne = (s: ParsedSetting): void => {
    const val = (drafts[s.key] ?? "").trim();
    if (val === "" || !onRunCommand) return;
    onRunCommand("settings", `${s.argName} ${val}`);
  };

  return (
    <div className="space-y-3">
      <div className="divide-y divide-separator rounded-md border border-border">
        {parsed.settings.map((s) => {
          const val = drafts[s.key] ?? s.value;
          const dirty = val.trim() !== s.value;
          return (
            <div
              key={s.key}
              className="flex flex-col gap-2 px-3 py-2.5 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="min-w-0">
                <p className="text-sm font-medium text-foreground">{s.label}</p>
                <p className="text-xs text-muted">{s.hint}</p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Input
                  aria-label={s.label}
                  value={val}
                  onChange={(e) => setDrafts((prev) => ({ ...prev, [s.key]: e.target.value }))}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      saveOne(s);
                    }
                  }}
                  className="w-28 font-mono text-xs"
                />
                <Button
                  size="sm"
                  variant={dirty ? "primary" : "outline"}
                  isDisabled={!onRunCommand || val.trim() === ""}
                  onPress={() => saveOne(s)}
                >
                  Save
                </Button>
              </div>
            </div>
          );
        })}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        {parsed.storedIn ? <p className="text-xs text-muted">{parsed.storedIn}</p> : <span />}
        {onRunCommand ? (
          <Button
            size="sm"
            variant="outline"
            onPress={() => onRunCommand("settings", "reset")}
          >
            <RotateCcw aria-hidden="true" className="size-3.5" />
            Reset to defaults
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function InteractiveJobsBody({
  items,
  onRunCommand,
}: {
  items: readonly { label: string; detail: string; key: string }[];
  onRunCommand?: (name: string, arg?: string) => void;
}): React.ReactElement {
  const [tails, setTails] = React.useState<Record<string, string | null>>({});
  const [loadingPid, setLoadingPid] = React.useState<string | null>(null);
  const [killingPid, setKillingPid] = React.useState<string | null>(null);

  const toggleTail = async (pid: string): Promise<void> => {
    if (tails[pid] !== undefined) {
      setTails((prev) => {
        const next = { ...prev };
        delete next[pid];
        return next;
      });
      return;
    }
    setLoadingPid(pid);
    try {
      const resp = await authFetch(apiUrl(`/api/jobs/${encodeURIComponent(pid)}/tail`));
      if (!resp.ok) {
        setTails((prev) => ({ ...prev, [pid]: "(output tail unavailable)" }));
      } else {
        const data = (await resp.json()) as { tail?: string };
        const text = (data.tail ?? "").trim();
        setTails((prev) => ({ ...prev, [pid]: text || "(no output yet)" }));
      }
    } catch {
      setTails((prev) => ({ ...prev, [pid]: "(failed to load output tail)" }));
    } finally {
      setLoadingPid(null);
    }
  };

  const killJob = async (pid: string): Promise<void> => {
    setKillingPid(pid);
    try {
      await authFetch(apiUrl(`/api/jobs/${encodeURIComponent(pid)}/kill`), { method: "POST" });
      onRunCommand?.("jobs");
    } finally {
      setKillingPid(null);
    }
  };

  return (
    <ul className="divide-y divide-separator rounded-md border border-border">
      {items.map((item) => {
        const pid = item.label;
        const tail = tails[pid];
        const showingTail = tail !== undefined;
        return (
          <li key={item.key} className="space-y-2 px-3 py-2.5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0 flex-1">
                <span className="font-mono text-xs font-medium text-foreground">pid {pid}</span>
                {item.detail ? (
                  <p className="mt-0.5 truncate text-xs text-muted">{item.detail}</p>
                ) : null}
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  isDisabled={loadingPid === pid}
                  onPress={() => void toggleTail(pid)}
                >
                  <Eye aria-hidden="true" className="size-3.5" />
                  {showingTail ? "Hide Tail" : "View Tail"}
                </Button>
                <Button
                  size="sm"
                  variant="danger-soft"
                  isDisabled={killingPid === pid}
                  onPress={() => void killJob(pid)}
                >
                  <Square aria-hidden="true" className="size-3" />
                  Kill
                </Button>
              </div>
            </div>
            {showingTail ? (
              <pre className="max-h-48 overflow-auto rounded-md border border-border bg-background p-2.5 font-mono text-xs whitespace-pre-wrap text-foreground">
                {tail}
              </pre>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

function CommandBody({
  name,
  result,
  onRunCommand,
}: {
  name: string;
  result: CommandResult;
  onRunCommand?: (name: string, arg?: string) => void;
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
          {result.body ? (
            name === "settings" && result.title === "Settings" ? (
              <InteractiveSettingsBody body={result.body} onRunCommand={onRunCommand} />
            ) : (
              <TextBody body={result.body} />
            )
          ) : null}
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
          ) : name === "jobs" ? (
            <InteractiveJobsBody items={items} onRunCommand={onRunCommand} />
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

export function CommandRowView({
  row,
  onRunCommand,
}: {
  row: CommandRow;
  onRunCommand?: (name: string, arg?: string) => void;
}): React.ReactElement {
  return (
    <Card className="gap-2 rounded-xl px-4 py-3">
      <header className="flex items-center gap-2">
        <span className="font-mono text-sm font-medium text-foreground">/{row.name}</span>
        <Chip size="sm" variant="soft" color={row.ok ? "success" : "danger"} className="ml-auto">
          {row.ok ? "Done" : "Failed"}
        </Chip>
      </header>
      <Separator />
      <CommandBody name={row.name} result={row.result} onRunCommand={onRunCommand} />
    </Card>
  );
}