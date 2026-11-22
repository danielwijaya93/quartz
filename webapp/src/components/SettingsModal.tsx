import * as React from "react";
import { Button, Input, Modal, useOverlayState } from "@heroui/react";
import { RotateCcw } from "lucide-react";

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

export function SettingsModal({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}): React.ReactElement {
  const state = useOverlayState({ isOpen: open, onOpenChange });
  const [data, setData] = React.useState<SettingsPayload | null>(null);
  const [drafts, setDrafts] = React.useState<Record<string, string>>({});
  const [loading, setLoading] = React.useState(false);
  const [savingKey, setSavingKey] = React.useState<string | null>(null);
  const [status, setStatus] = React.useState<{ ok: boolean; text: string } | null>(null);

  const load = React.useCallback(async () => {
    setLoading(true);
    setStatus(null);
    try {
      const resp = await fetch("/api/settings");
      if (!resp.ok) throw new Error("Failed to load settings");
      const payload = (await resp.json()) as SettingsPayload;
      setData(payload);
      setDrafts({
        reserve: String(payload.values.reserve),
        keep: String(payload.values.keep),
        progress: String(payload.values.progress),
        bg: String(payload.values.bg),
      });
    } catch {
      setStatus({ ok: false, text: "Could not load settings." });
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    if (open) void load();
  }, [open, load]);

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

  return (
    <Modal state={state}>
      <Modal.Backdrop variant="blur">
        <Modal.Container size="lg" scroll="inside">
          <Modal.Dialog className="p-0">
            <Modal.Header className="border-b border-separator px-4 py-3">
              <Modal.Heading className="text-base font-semibold">Settings</Modal.Heading>
            </Modal.Header>

            <Modal.Body className="space-y-3 p-4">
              {loading && !data ? (
                <p className="text-sm text-muted">Loading settings…</p>
              ) : data ? (
                <div className="divide-y divide-separator rounded-lg border border-border">
                  {SETTING_FIELDS.map((field) => {
                    const current = String(data.values[field.key]);
                    const val = drafts[field.key] ?? current;
                    const dirty = val.trim() !== current;
                    return (
                      <div
                        key={field.key}
                        className="flex flex-col gap-2 px-3 py-2.5 sm:flex-row sm:items-center sm:justify-between"
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
                <p className={status.ok ? "text-xs text-success" : "text-xs text-danger"}>
                  {status.text}
                </p>
              ) : null}
              {data ? <p className="text-xs text-muted">Stored in {data.configPath}</p> : null}
            </Modal.Body>

            <Modal.Footer className="flex items-center justify-between border-t border-separator px-4 py-2.5">
              <Button
                size="sm"
                variant="outline"
                isDisabled={savingKey !== null}
                onPress={() => void saveKey("reset")}
              >
                <RotateCcw aria-hidden="true" className="size-3.5" />
                Reset to defaults
              </Button>
              <Button size="sm" variant="primary" onPress={() => onOpenChange(false)}>
                Done
              </Button>
            </Modal.Footer>
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>
    </Modal>
  );
}
