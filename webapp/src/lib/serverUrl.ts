import { Capacitor } from "@capacitor/core";

const STORAGE_KEY = "quartz:serverUrl";

/** True when running inside the Capacitor native shell (Android / iOS). */
export function isNativeCapacitor(): boolean {
  try {
    return Capacitor.isNativePlatform();
  } catch {
    return false;
  }
}

/** Normalize a user-entered server URL (e.g. `100.64.0.1:8765` -> `http://100.64.0.1:8765`). */
export function normalizeServerUrl(raw: string): string {
  const trimmed = raw.trim().replace(/\/+$/, "");
  if (!trimmed) return "";
  const hasScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed);
  if (hasScheme && !/^https?:\/\//i.test(trimmed)) {
    throw new Error("Server URL must use http:// or https://");
  }
  const withScheme = hasScheme ? trimmed : `http://${trimmed}`;
  const parsed = new URL(withScheme);
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error("Server URL must use http:// or https://");
  }
  return `${parsed.protocol}//${parsed.host}`;
}

export function getServerUrl(): string | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw && raw.trim() !== "" ? raw.trim() : null;
  } catch {
    return null;
  }
}

export function setServerUrl(url: string): string {
  const normalized = normalizeServerUrl(url);
  if (!normalized) throw new Error("Server URL cannot be empty");
  window.localStorage.setItem(STORAGE_KEY, normalized);
  return normalized;
}

export function clearServerUrl(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore storage errors
  }
}

/** Prefix `/api/...` paths with the configured server URL when running natively. */
export function apiUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  const base = getServerUrl();
  if (!base) return path;
  const cleanPath = path.startsWith("/") ? path : `/${path}`;
  return `${base}${cleanPath}`;
}

/** Derive the `/ws` WebSocket URL from the configured server URL, if any. */
export function configuredWsUrl(): string | null {
  const base = getServerUrl();
  if (!base) return null;
  const parsed = new URL(base);
  const wsScheme = parsed.protocol === "https:" ? "wss:" : "ws:";
  return `${wsScheme}//${parsed.host}/ws`;
}
