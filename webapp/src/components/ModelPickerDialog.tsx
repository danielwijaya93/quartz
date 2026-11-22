import * as React from "react";
import { Alert, Button, Chip, Input, Modal, Spinner, cn, useOverlayState } from "@heroui/react";
import { Check, Cpu, RefreshCw, Search, Sliders, Zap } from "lucide-react";
import { fetchModels, selectModel, type ModelProvider, type ModelsPayload } from "@/lib/modelsApi";

export interface ModelPickerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  currentModel?: string;
  currentEffort?: string;
  onSuccess?: (provider: string, model: string, effort: string) => void;
}

export function ModelPickerDialog({
  open,
  onOpenChange,
  currentModel,
  currentEffort,
  onSuccess,
}: ModelPickerDialogProps): React.ReactElement {
  const state = useOverlayState({ isOpen: open, onOpenChange });
  const [data, setData] = React.useState<ModelsPayload | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [actionError, setActionError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  const [selectedProviderId, setSelectedProviderId] = React.useState<string>("");
  const [selectedModel, setSelectedModel] = React.useState<string>("");
  const [selectedEffort, setSelectedEffort] = React.useState<string>("medium");
  const [search, setSearch] = React.useState<string>("");

  const load = React.useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const payload = await fetchModels();
      setData(payload);
      if (payload.current) {
        setSelectedProviderId(payload.current.provider || "");
        setSelectedModel(currentModel || payload.current.model || "");
        setSelectedEffort(currentEffort || payload.current.effort || "medium");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load models");
    } finally {
      setLoading(false);
    }
  }, [currentModel, currentEffort]);

  React.useEffect(() => {
    if (open) {
      void load();
      setActionError(null);
      setSearch("");
    }
  }, [open, load]);

  const activeProvider: ModelProvider | undefined = React.useMemo(() => {
    if (!data || data.providers.length === 0) return undefined;
    return (
      data.providers.find((p) => p.id === selectedProviderId) ??
      data.providers.find((p) => p.ready) ??
      data.providers[0]
    );
  }, [data, selectedProviderId]);

  React.useEffect(() => {
    if (activeProvider && !selectedProviderId) {
      setSelectedProviderId(activeProvider.id);
    }
  }, [activeProvider, selectedProviderId]);

  const availableEfforts = React.useMemo(() => {
    if (!activeProvider) return [];
    const perModel = selectedModel ? activeProvider.modelEfforts?.[selectedModel] : undefined;
    return (perModel && perModel.length > 0 ? perModel : activeProvider.efforts) || [];
  }, [activeProvider, selectedModel]);

  React.useEffect(() => {
    if (availableEfforts.length === 0) return;
    if (!availableEfforts.includes(selectedEffort)) {
      const preferred =
        activeProvider?.defaultEffort && availableEfforts.includes(activeProvider.defaultEffort)
          ? activeProvider.defaultEffort
          : availableEfforts[0];
      setSelectedEffort(preferred || "medium");
    }
  }, [availableEfforts, activeProvider, selectedEffort]);

  const filteredModels = React.useMemo(() => {
    if (!activeProvider) return [];
    const query = search.trim().toLowerCase();
    if (!query) return activeProvider.models;
    return activeProvider.models.filter((m) => m.toLowerCase().includes(query));
  }, [activeProvider, search]);

  const handleSwitch = async () => {
    if (!activeProvider || !selectedModel || submitting) return;
    setSubmitting(true);
    setActionError(null);
    try {
      const res = await selectModel(activeProvider.id, selectedModel, selectedEffort);
      if (!res.ok) {
        setActionError(res.error || "Failed to switch model");
        return;
      }
      onSuccess?.(activeProvider.id, selectedModel, selectedEffort);
      onOpenChange(false);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal state={state}>
      <Modal.Backdrop variant="blur">
        <Modal.Container size="md" scroll="inside">
          <Modal.Dialog className="p-0">
            <Modal.Header className="flex items-center justify-between border-b border-separator px-4 py-3">
              <div className="flex items-center gap-2">
                <Cpu aria-hidden="true" className="size-4 text-accent" />
                <Modal.Heading className="text-base font-semibold">Choose Model</Modal.Heading>
              </div>
              <Button
                size="sm"
                variant="ghost"
                isIconOnly
                isDisabled={loading}
                onPress={() => void load()}
                aria-label="Reload model list"
              >
                <RefreshCw aria-hidden="true" className={cn("size-3.5", loading && "animate-spin")} />
              </Button>
            </Modal.Header>

            <Modal.Body className="flex flex-col gap-3 overflow-hidden px-4 py-3">
              {error !== null ? (
                <Alert status="danger" className="rounded-lg p-3">
                  <Alert.Indicator />
                  <Alert.Content>
                    <Alert.Title>Failed to load models</Alert.Title>
                    <Alert.Description>{error}</Alert.Description>
                  </Alert.Content>
                </Alert>
              ) : null}

              {actionError !== null ? (
                <Alert status="danger" className="rounded-lg p-3">
                  <Alert.Indicator />
                  <Alert.Content>
                    <Alert.Title>Switch failed</Alert.Title>
                    <Alert.Description>{actionError}</Alert.Description>
                  </Alert.Content>
                </Alert>
              ) : null}

              {/* Providers Selection */}
              <div className="shrink-0">
                <label className="mb-1 block text-xs font-semibold text-muted uppercase">Provider</label>
                <div className="flex flex-wrap gap-1.5" role="tablist" aria-label="Model providers">
                  {(data?.providers ?? []).map((provider) => {
                    const isSelected = provider.id === activeProvider?.id;
                    return (
                      <button
                        key={provider.id}
                        type="button"
                        role="tab"
                        aria-selected={isSelected}
                        onClick={() => {
                          setSelectedProviderId(provider.id);
                          if (!provider.models.includes(selectedModel)) {
                            setSelectedModel(provider.defaultModel || provider.models[0] || "");
                          }
                        }}
                        className={cn(
                          "flex cursor-pointer items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
                          isSelected
                            ? "border-accent bg-accent/10 text-accent-foreground font-semibold"
                            : "border-border bg-surface-secondary/50 text-muted hover:bg-surface-secondary hover:text-foreground",
                          !provider.ready && "opacity-60",
                        )}
                      >
                        <span className="truncate">{provider.label}</span>
                        {provider.ready ? (
                          <span className="size-1.5 rounded-full bg-success" title="Configured" />
                        ) : null}
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* Model Search & Selection */}
              <div className="flex min-h-0 flex-1 flex-col gap-1.5">
                <div className="flex shrink-0 items-center justify-between">
                  <label className="text-xs font-semibold text-muted uppercase">Model</label>
                  <span className="text-[11px] text-muted">{filteredModels.length} available</span>
                </div>
                <div className="flex shrink-0 items-center gap-2 rounded-lg border border-border bg-surface-secondary/40 px-2.5 py-1 focus-within:border-accent/40 focus-within:ring-2 focus-within:ring-accent/15">
                  <Search aria-hidden="true" className="size-3.5 text-muted" />
                  <Input
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    placeholder="Search models…"
                    aria-label="Filter models"
                    className="quartz-bare-input h-7 w-full border-0 bg-transparent text-xs shadow-none"
                  />
                </div>

                <div className="min-h-28 max-h-52 flex-1 overflow-y-auto rounded-lg border border-border bg-surface/40 p-1">
                  {loading && !data ? (
                    <div className="flex h-24 items-center justify-center">
                      <Spinner size="sm" />
                    </div>
                  ) : filteredModels.length === 0 ? (
                    <div className="px-3 py-6 text-center text-xs text-muted">
                      {search ? `No models match "${search}"` : "No models found for this provider."}
                    </div>
                  ) : (
                    <ul className="space-y-0.5" role="listbox" aria-label="Available models">
                      {filteredModels.map((model) => {
                        const isSelected = model === selectedModel;
                        const isCurrent = model === currentModel;
                        return (
                          <li
                            key={model}
                            role="option"
                            aria-selected={isSelected}
                            onClick={() => setSelectedModel(model)}
                            className={cn(
                              "flex cursor-pointer items-center justify-between rounded-md px-2.5 py-1.5 text-xs transition-colors",
                              isSelected
                                ? "bg-accent text-accent-foreground font-medium"
                                : "text-foreground hover:bg-surface-secondary",
                            )}
                          >
                            <span className="truncate font-mono">{model}</span>
                            <div className="flex items-center gap-1.5">
                              {isCurrent ? (
                                <Chip size="sm" variant="soft" color="default" className="h-5 text-[10px]">
                                  current
                                </Chip>
                              ) : null}
                              {isSelected ? <Check aria-hidden="true" className="size-3.5 shrink-0" /> : null}
                            </div>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>
              </div>

              {/* Reasoning Effort Selection (depends on selected model; shrink-0 keeps it always visible) */}
              {availableEfforts.length > 0 ? (
                <div className="shrink-0">
                  <div className="mb-1 flex items-center gap-1">
                    <Sliders aria-hidden="true" className="size-3 text-muted" />
                    <label className="text-xs font-semibold text-muted uppercase">Reasoning Effort</label>
                  </div>
                  <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Reasoning effort">
                    {availableEfforts.map((effort) => {
                      const isSelected = effort === selectedEffort;
                      return (
                        <button
                          key={effort}
                          type="button"
                          role="radio"
                          aria-checked={isSelected}
                          onClick={() => setSelectedEffort(effort)}
                          className={cn(
                            "cursor-pointer rounded-md border px-2.5 py-1 text-xs font-medium capitalize transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
                            isSelected
                              ? "border-accent bg-accent/15 text-accent-foreground font-semibold"
                              : "border-border bg-surface-secondary/50 text-muted hover:bg-surface-secondary hover:text-foreground",
                          )}
                        >
                          {effort}
                        </button>
                      );
                    })}
                  </div>
                </div>
              ) : null}
            </Modal.Body>

            <Modal.Footer className="flex items-center justify-between border-t border-separator px-4 py-3">
              <span className="truncate text-xs text-muted font-mono">
                {selectedModel ? `${selectedModel} (${selectedEffort})` : "Select a model"}
              </span>
              <div className="flex items-center gap-2">
                <Button size="sm" variant="ghost" onPress={() => onOpenChange(false)}>
                  Cancel
                </Button>
                <Button
                  size="sm"
                  variant="primary"
                  isDisabled={!selectedModel || submitting || loading}
                  onPress={handleSwitch}
                >
                  {submitting ? <Spinner size="sm" /> : <Zap aria-hidden="true" className="size-3.5" />}
                  Switch Model
                </Button>
              </div>
            </Modal.Footer>
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>
    </Modal>
  );
}
