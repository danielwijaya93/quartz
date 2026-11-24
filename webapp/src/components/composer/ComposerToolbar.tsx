import * as React from "react";
import { Button, cn } from "@heroui/react";
import { ArrowUp, ChevronDown, CornerDownRight, Plus, Square } from "lucide-react";
import { ContextRing } from "./ContextRing";
import { formatEffortLabel } from "./composerHelpers";

export function ComposerToolbar({
  fileRef,
  disabled,
  canSend,
  turnRunning,
  bgCount,
  bgFailed,
  model,
  effort,
  contextPct,
  maxContextTokens,
  cacheHitPct,
  onAddFiles,
  onOpenJobs,
  onOpenModelPicker,
  onCancel,
  onSubmit,
}: {
  fileRef: React.RefObject<HTMLInputElement | null>;
  disabled: boolean;
  canSend: boolean;
  turnRunning: boolean;
  bgCount: number;
  bgFailed: number;
  model?: string;
  effort?: string;
  contextPct?: number;
  maxContextTokens?: number;
  cacheHitPct?: number;
  onAddFiles: (files: FileList | null) => void;
  onOpenJobs?: () => void;
  onOpenModelPicker?: () => void;
  onCancel: () => void;
  onSubmit: () => void;
}): React.ReactElement {
  return (
    <div className="mt-1 flex items-center gap-1.5">
      <input
        ref={fileRef}
        type="file"
        multiple
        className="sr-only"
        aria-label="File upload"
        onChange={(event) => {
          onAddFiles(event.target.files);
          event.target.value = "";
        }}
      />
      <Button
        size="sm"
        variant="ghost"
        isIconOnly
        isDisabled={disabled}
        onPress={() => fileRef.current?.click()}
        aria-label="Attach files"
        className="size-7 shrink-0 rounded-lg text-muted hover:bg-surface-secondary hover:text-foreground"
      >
        <Plus aria-hidden="true" className="size-4" />
      </Button>

      <div className="ml-auto flex min-w-0 flex-1 items-center justify-end gap-1.5">
        {bgCount > 0 || bgFailed > 0 ? (
          <button
            type="button"
            onClick={onOpenJobs}
            aria-label={
              bgFailed > 0
                ? `Background jobs: ${bgCount} running, ${bgFailed} failed. Click to inspect.`
                : `Background jobs running: ${bgCount}. Click to inspect.`
            }
            title="Click to open /jobs"
            className={cn(
              "inline-flex h-7 shrink-0 cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-lg border px-2 font-mono text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
              bgFailed > 0
                ? "border-danger/40 bg-danger-soft text-danger-soft-foreground hover:bg-danger-soft/80"
                : "border-success/40 bg-success-soft text-success hover:bg-success-soft/80",
            )}
          >
            <span
              className={cn(
                "size-1.5 shrink-0 rounded-full",
                bgFailed > 0 ? "bg-danger" : "animate-pulse bg-success",
              )}
              aria-hidden="true"
            />
            <span className="sm:hidden">
              {bgFailed > 0 ? `${bgFailed} failed` : `bg × ${bgCount}`}
            </span>
            <span className="hidden sm:inline">
              {bgFailed > 0
                ? bgCount > 0
                  ? `bg × ${bgCount} · ${bgFailed} failed`
                  : `bg × ${bgFailed} failed`
                : `bg × ${bgCount}`}
            </span>
          </button>
        ) : null}
        {onOpenModelPicker ? (
          <button
            type="button"
            onClick={onOpenModelPicker}
            aria-label={
              model
                ? `Switch model (current: ${model}${effort ? ` ${formatEffortLabel(effort)}` : ""})`
                : "Choose a model"
            }
            className="inline-flex h-7 min-w-0 max-w-[16rem] cursor-pointer items-center gap-1 rounded-lg px-2 text-xs text-muted transition-colors hover:bg-surface-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            <span className="truncate font-medium">{model || "Model"}</span>
            {effort ? (
              <span className="shrink-0 text-muted/80">{formatEffortLabel(effort)}</span>
            ) : null}
            <ChevronDown aria-hidden="true" className="size-3 shrink-0 text-muted/70" />
          </button>
        ) : null}
        {contextPct !== undefined ? (
          <ContextRing pct={contextPct} maxTokens={maxContextTokens} cacheHitPct={cacheHitPct} />
        ) : null}
        {turnRunning ? (
          <Button
            size="sm"
            isIconOnly
            variant="outline"
            onPress={onCancel}
            aria-label="Cancel the running turn"
            className="size-8 shrink-0 rounded-full"
          >
            <Square aria-hidden="true" className="size-3.5" />
          </Button>
        ) : null}
        <Button
          size="sm"
          isIconOnly
          variant={canSend ? "secondary" : "ghost"}
          isDisabled={!canSend}
          onPress={onSubmit}
          aria-label={turnRunning ? "Steer the running turn" : "Send prompt"}
          className={cn(
            "size-8 shrink-0 rounded-full transition-all duration-150",
            canSend
              ? "bg-foreground/85 text-background shadow-xs hover:bg-foreground"
              : "border border-border/50 bg-surface-secondary/40 text-muted opacity-40",
          )}
        >
          {turnRunning ? (
            <CornerDownRight aria-hidden="true" className="size-4" />
          ) : (
            <ArrowUp aria-hidden="true" className="size-4" />
          )}
        </Button>
      </div>
    </div>
  );
}
