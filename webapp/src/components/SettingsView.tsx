import * as React from "react";
import { Button, Chip, Input, cn } from "@heroui/react";
import { ArrowLeft, Plus, RotateCcw, Trash2 } from "lucide-react";

interface SettingsPayload {
  values: {
    reserve: number;
    keep: number;
    progress: number;
    bg: number;
  };
  defaults: {
    reserve: number;
    keep: number;
    progress: number;
    bg: number;
  };
  configPath: string;
}

interface McpServerItem {
  name: string;
  enabled: boolean;
  transport: "http" | "stdio";
  url: string;
  command: string;
  args: string[];
  cwd: string;
  headerKeys: string[];
  envKeys: string[];
}

interface McpPayload {
  configPath: string;
  servers: McpServerItem[];
}

function parseKvLines(raw: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const eq = trimmed.indexOf("=");
    if (eq > 0) {
      const key = trimmed.slice(0, eq).trim();
      const val = trimmed.slice(eq + 1).trim();
      if (key) result[key] = val;
    }
  }
  return result;
}

const SETTING_FIELDS: readonly {
  key: "reserve" | "keep" | "progress" | "bg";
  label: string;
  hint: (def: number) => string;
}[] = [
  {
    key: "reserve",
    label: "Reserve tokens",
    hint: (def) => `Default ${def.toLocaleString()}`,
  },
  {
    key: "keep",
    label: "Keep recent tokens",
    hint: (def) => `Default ${def.toLocaleString()}`,
  },
  {
    key: "progress",
    label: "Progress update interval (turns)",
    hint: (def) => `Default ${def}, 0 disables`,
  },
  {
    key: "bg",
    label: "Background check interval (seconds)",
    hint: (def) => `Default ${def}s, 0 disables`,
  },
];

/**
 * In-canvas settings view: replaces the transcript column with a calm, native
 * page and a `Back to chat` button—no full-screen dimmed backdrop.
 */
export function SettingsView({
  onBack,
}: {
  onBack: () => void;
}): React.ReactElement {
  const [data, setData] = React.useState<SettingsPayload | null>(null);
  const [drafts, setDrafts] = React.useState<Record<string, string>>({});
  const [loading, setLoading] = React.useState(false);
  const [savingKey, setSavingKey] = React.useState<string | null>(null);
  const [status, setStatus] = React.useState<{ ok: boolean; text: string } | null>(null);

  const [mcpData, setMcpData] = React.useState<McpPayload | null>(null);
  const [mcpBusy, setMcpBusy] = React.useState<string | null>(null);
  const [mcpStatus, setMcpStatus] = React.useState<{ ok: boolean; text: string } | null>(null);
  const [addingMcp, setAddingMcp] = React.useState(false);
  const [newName, setNewName] = React.useState("");
  const [newTransport, setNewTransport] = React.useState<"stdio" | "http">("stdio");
  const [newUrl, setNewUrl] = React.useState("");
  const [newCmd, setNewCmd] = React.useState("");
  const [newKv, setNewKv] = React.useState("");

  const load = React.useCallback(async () => {
    setLoading(true);
    setStatus(null);
    try {
      const [settingsResp, mcpResp] = await Promise.all([
        fetch("/api/settings"),
        fetch("/api/mcp").catch(() => null),
      ]);
      if (!settingsResp.ok) throw new Error("Failed to load settings");
      const payload = (await settingsResp.json()) as SettingsPayload;
      setData(payload);
      setDrafts({
        reserve: String(payload.values.reserve),
        keep: String(payload.values.keep),
        progress: String(payload.values.progress),
        bg: String(payload.values.bg),
      });
      if (mcpResp && mcpResp.ok) {
        const mcpPayload = (await mcpResp.json()) as McpPayload;
        if (Array.isArray(mcpPayload.servers)) setMcpData(mcpPayload);
      }
    } catch {
      setStatus({ ok: false, text: "Could not load settings." });
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load]);

  const saveKey = async (key: string, value?: string): Promise<void> => {
    setSavingKey(key);
    setStatus(null);
    try {
      const resp = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(key === "reset" ? { key: "reset" } : { key, value }),
      });
      const body = (await resp.json()) as {
        ok?: boolean;
        error?: string;
        message?: string;
        values?: SettingsPayload["values"];
      };
      if (!resp.ok || !body.ok) {
        setStatus({ ok: false, text: body.error || "Failed to save setting." });
        return;
      }
      if (body.values) {
        setData((prev) => (prev ? { ...prev, values: body.values! } : prev));
        setDrafts({
          reserve: String(body.values.reserve),
          keep: String(body.values.keep),
          progress: String(body.values.progress),
          bg: String(body.values.bg),
        });
      }
      setStatus({ ok: true, text: body.message || "Saved." });
    } catch {
      setStatus({ ok: false, text: "Request failed." });
    } finally {
      setSavingKey(null);
    }
  };

  const toggleMcp = async (name: string, enabled: boolean): Promise<void> => {
    setMcpBusy(name);
    setMcpStatus(null);
    try {
      const resp = await fetch("/api/mcp/toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, enabled }),
      });
      const body = (await resp.json()) as { ok?: boolean; error?: string } & Partial<McpPayload>;
      if (!resp.ok || !body.ok) {
        setMcpStatus({ ok: false, text: body.error || "Failed to update MCP server." });
        return;
      }
      if (Array.isArray(body.servers)) {
        setMcpData({ configPath: body.configPath ?? mcpData?.configPath ?? "", servers: body.servers });
      }
      setMcpStatus({ ok: true, text: `${enabled ? "Enabled" : "Disabled"} ${name}.` });
    } catch {
      setMcpStatus({ ok: false, text: "Request failed." });
    } finally {
      setMcpBusy(null);
    }
  };

  const removeMcp = async (name: string): Promise<void> => {
    setMcpBusy(name);
    setMcpStatus(null);
    try {
      const resp = await fetch("/api/mcp/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      const body = (await resp.json()) as { ok?: boolean; error?: string } & Partial<McpPayload>;
      if (!resp.ok || !body.ok) {
        setMcpStatus({ ok: false, text: body.error || "Failed to remove MCP server." });
        return;
      }
      if (Array.isArray(body.servers)) {
        setMcpData({ configPath: body.configPath ?? mcpData?.configPath ?? "", servers: body.servers });
      }
      setMcpStatus({ ok: true, text: `Removed ${name}.` });
    } catch {
      setMcpStatus({ ok: false, text: "Request failed." });
    } finally {
      setMcpBusy(null);
    }
  };

  const addMcp = async (): Promise<void> => {
    const cleanName = newName.trim();
    if (!cleanName) {
      setMcpStatus({ ok: false, text: "Server name is required." });
      return;
    }
    const kv = parseKvLines(newKv);
    const payload: Record<string, unknown> = {
      name: cleanName,
      transport: newTransport,
    };
    if (newTransport === "http") {
      payload.url = newUrl.trim();
      if (Object.keys(kv).length > 0) payload.headers = kv;
    } else {
      const parts = newCmd.trim().split(/\s+/).filter(Boolean);
      payload.command = parts[0] ?? "";
      payload.args = parts.slice(1);
      if (Object.keys(kv).length > 0) payload.env = kv;
    }
    setMcpBusy("__add__");
    setMcpStatus(null);
    try {
      const resp = await fetch("/api/mcp/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = (await resp.json()) as { ok?: boolean; error?: string } & Partial<McpPayload>;
      if (!resp.ok || !body.ok) {
        setMcpStatus({ ok: false, text: body.error || "Failed to save MCP server." });
        return;
      }
      if (Array.isArray(body.servers)) {
        setMcpData({ configPath: body.configPath ?? mcpData?.configPath ?? "", servers: body.servers });
      }
      setNewName("");
      setNewUrl("");
      setNewCmd("");
      setNewKv("");
      setAddingMcp(false);
      setMcpStatus({ ok: true, text: `Saved ${cleanName}.` });
    } catch {
      setMcpStatus({ ok: false, text: "Request failed." });
    } finally {
      setMcpBusy(null);
    }
  };

  return (
    <main
      aria-label="Settings"
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          onBack();
        }
      }}
      className="min-h-0 flex-1 overflow-y-auto px-4 py-6 sm:px-5"
    >
      <div className="mx-auto w-full max-w-3xl space-y-5">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <Button
              size="sm"
              variant="ghost"
              onPress={onBack}
              className="h-8 gap-1.5 px-2 text-muted hover:text-foreground"
            >
              <ArrowLeft aria-hidden="true" className="size-4" />
              Back to chat
            </Button>
          </div>
          <Button
            size="sm"
            variant="outline"
            isDisabled={savingKey !== null}
            onPress={() => void saveKey("reset")}
          >
            <RotateCcw aria-hidden="true" className="size-3.5" />
            Reset to defaults
          </Button>
        </div>

        <div>
          <h1 className="text-lg font-semibold tracking-tight text-foreground">Settings</h1>
          <p className="mt-0.5 text-xs text-muted">
            Session compaction and background check defaults.
          </p>
        </div>

        {loading && !data ? (
          <p className="text-sm text-muted">Loading settings…</p>
        ) : data ? (
          <div className="divide-y divide-separator rounded-xl border border-border bg-surface/50">
            {SETTING_FIELDS.map((field) => {
              const current = String(data.values[field.key]);
              const val = drafts[field.key] ?? current;
              const dirty = val.trim() !== current;
              return (
                <div
                  key={field.key}
                  className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
                >
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-foreground">{field.label}</p>
                    <p className="text-xs text-muted">{field.hint(data.defaults[field.key])}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <Input
                      aria-label={field.label}
                      value={val}
                      onChange={(e) =>
                        setDrafts((prev) => ({ ...prev, [field.key]: e.target.value }))
                      }
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          void saveKey(field.key, val.trim());
                        }
                      }}
                      className="w-28 font-mono text-xs"
                    />
                    <Button
                      size="sm"
                      variant={dirty ? "primary" : "outline"}
                      isDisabled={savingKey !== null || val.trim() === ""}
                      onPress={() => void saveKey(field.key, val.trim())}
                    >
                      Save
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
        ) : null}

        {status ? (
          <p role="status" className={status.ok ? "text-xs text-success" : "text-xs text-danger"}>
            {status.text}
          </p>
        ) : null}
        {data ? <p className="text-xs text-muted">Stored in {data.configPath}</p> : null}

        <section aria-label="MCP Servers" className="space-y-3 pt-4 border-t border-separator/70">
          <div className="flex items-center justify-between gap-2">
            <div>
              <h2 className="text-base font-semibold tracking-tight text-foreground">MCP Servers</h2>
              <p className="mt-0.5 text-xs text-muted">
                Manage global MCP servers; changes hot-reload in your active session.
              </p>
            </div>
            <Button
              size="sm"
              variant="outline"
              onPress={() => {
                setAddingMcp((prev) => !prev);
                setMcpStatus(null);
              }}
            >
              <Plus aria-hidden="true" className="size-3.5" />
              Add MCP Server
            </Button>
          </div>

          {addingMcp ? (
            <div className="space-y-3 rounded-xl border border-border bg-surface/60 p-4">
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1">
                  <label htmlFor="mcp-name" className="text-xs font-medium text-foreground">
                    Server name
                  </label>
                  <Input
                    id="mcp-name"
                    aria-label="MCP server name"
                    placeholder="e.g. exa"
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    className="w-full text-xs"
                  />
                </div>
                <div className="space-y-1">
                  <span className="block text-xs font-medium text-foreground">Transport</span>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant={newTransport === "stdio" ? "primary" : "outline"}
                      onPress={() => setNewTransport("stdio")}
                    >
                      stdio
                    </Button>
                    <Button
                      size="sm"
                      variant={newTransport === "http" ? "primary" : "outline"}
                      onPress={() => setNewTransport("http")}
                    >
                      http
                    </Button>
                  </div>
                </div>
              </div>

              {newTransport === "http" ? (
                <div className="space-y-1">
                  <label htmlFor="mcp-url" className="text-xs font-medium text-foreground">
                    Streamable HTTP URL
                  </label>
                  <Input
                    id="mcp-url"
                    aria-label="MCP server URL"
                    placeholder="https://example.com/mcp"
                    value={newUrl}
                    onChange={(e) => setNewUrl(e.target.value)}
                    className="w-full font-mono text-xs"
                  />
                </div>
              ) : (
                <div className="space-y-1">
                  <label htmlFor="mcp-cmd" className="text-xs font-medium text-foreground">
                    Command & arguments
                  </label>
                  <Input
                    id="mcp-cmd"
                    aria-label="MCP server command"
                    placeholder="npx -y @modelcontextprotocol/server-filesystem ."
                    value={newCmd}
                    onChange={(e) => setNewCmd(e.target.value)}
                    className="w-full font-mono text-xs"
                  />
                </div>
              )}

              <div className="space-y-1">
                <label htmlFor="mcp-kv" className="text-xs font-medium text-foreground">
                  {newTransport === "http" ? "Headers (KEY=VALUE per line, optional)" : "Environment variables (KEY=VALUE per line, optional)"}
                </label>
                <textarea
                  id="mcp-kv"
                  rows={2}
                  value={newKv}
                  onChange={(e) => setNewKv(e.target.value)}
                  placeholder="API_KEY=..."
                  className="w-full rounded-lg border border-border bg-background px-2.5 py-1.5 font-mono text-xs text-foreground focus:border-focus focus:outline-none"
                />
              </div>

              <div className="flex items-center justify-end gap-2">
                <Button size="sm" variant="ghost" onPress={() => setAddingMcp(false)}>
                  Cancel
                </Button>
                <Button
                  size="sm"
                  variant="primary"
                  isDisabled={mcpBusy !== null}
                  onPress={() => void addMcp()}
                >
                  Save Server
                </Button>
              </div>
            </div>
          ) : null}

          {mcpData && mcpData.servers.length > 0 ? (
            <div className="divide-y divide-separator rounded-xl border border-border bg-surface/50">
              {mcpData.servers.map((srv) => {
                const target =
                  srv.transport === "http"
                    ? srv.url
                    : [srv.command, ...srv.args].filter(Boolean).join(" ");
                return (
                  <div
                    key={srv.name}
                    className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
                  >
                    <div className="min-w-0 flex-1 space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium text-foreground">{srv.name}</span>
                        <Chip size="sm" variant="soft" color="default" className="h-5 text-[10px]">
                          {srv.transport}
                        </Chip>
                        {srv.headerKeys.length > 0 ? (
                          <span className="text-[11px] text-meta">
                            headers: {srv.headerKeys.join(", ")}
                          </span>
                        ) : null}
                        {srv.envKeys.length > 0 ? (
                          <span className="text-[11px] text-meta">
                            env: {srv.envKeys.join(", ")}
                          </span>
                        ) : null}
                      </div>
                      <p className="truncate font-mono text-xs text-muted" title={target}>
                        {target}
                      </p>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <button
                        type="button"
                        role="switch"
                        aria-checked={srv.enabled}
                        aria-label={`Toggle MCP server ${srv.name}`}
                        disabled={mcpBusy === srv.name}
                        onClick={() => void toggleMcp(srv.name, !srv.enabled)}
                        className="inline-flex h-7 cursor-pointer items-center rounded-md px-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus disabled:opacity-50"
                      >
                        <span
                          aria-hidden="true"
                          className={cn(
                            "relative inline-flex h-4 w-7 shrink-0 items-center rounded-full border transition-colors duration-150",
                            srv.enabled
                              ? "border-success/50 bg-success/80"
                              : "border-border bg-surface-secondary",
                          )}
                        >
                          <span
                            className={cn(
                              "inline-block size-3 rounded-full transition-transform duration-150",
                              srv.enabled
                                ? "translate-x-3.5 bg-background"
                                : "translate-x-0.5 bg-muted",
                            )}
                          />
                        </span>
                      </button>
                      <Button
                        isIconOnly
                        size="sm"
                        variant="ghost"
                        aria-label={`Remove MCP server ${srv.name}`}
                        isDisabled={mcpBusy === srv.name}
                        onPress={() => void removeMcp(srv.name)}
                        className="size-7 min-w-7 text-muted hover:text-danger"
                      >
                        <Trash2 aria-hidden="true" className="size-3.5" />
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : mcpData ? (
            <p className="text-xs text-muted">No MCP servers configured yet.</p>
          ) : null}

          {mcpStatus ? (
            <p role="status" className={mcpStatus.ok ? "text-xs text-success" : "text-xs text-danger"}>
              {mcpStatus.text}
            </p>
          ) : null}
          {mcpData ? <p className="text-xs text-muted">Stored in {mcpData.configPath}</p> : null}
        </section>
      </div>
    </main>
  );
}
