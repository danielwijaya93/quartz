import type { ImageAttachment } from "@/protocol";

export const MAX_TEXTAREA_PX = 200;
export const MAX_FRAME_BYTES = 1024 * 1024;

export interface AttachedFile {
  name: string;
  content: string;
}

const EFFORT_LABELS: Record<string, string> = {
  none: "Off",
  minimal: "Min",
  low: "Low",
  medium: "Med",
  high: "High",
  max: "Max",
};

export function formatEffortLabel(effort: string): string {
  const key = effort.trim().toLowerCase();
  if (!key) return "";
  return EFFORT_LABELS[key] ?? `${key.charAt(0).toUpperCase()}${key.slice(1)}`;
}

export function formatTokenWindow(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}k`;
  return String(tokens);
}

export function formatCostUsd(costUsd: number): string {
  if (costUsd <= 0) return "$0.00";
  if (costUsd < 0.01) return `$${costUsd.toFixed(4)}`;
  if (costUsd < 1) return `$${costUsd.toFixed(3)}`;
  return `$${costUsd.toFixed(2)}`;
}

/** Read a File as base64 without the `data:` prefix the protocol expects. */
export async function readAsAttachment(file: File): Promise<ImageAttachment> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsDataURL(file);
  });
  const comma = dataUrl.indexOf(",");
  return {
    mimeType: file.type || "image/png",
    data: comma === -1 ? dataUrl : dataUrl.slice(comma + 1),
  };
}

export function isCoarsePointer(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(pointer: coarse)").matches
  );
}

/** Read a non-image File as UTF-8 text. */
export async function readAsTextFile(file: File): Promise<AttachedFile> {
  const content = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsText(file);
  });
  return { name: file.name, content };
}
