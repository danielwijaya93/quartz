const TOKEN_STORAGE_KEY = "quartz:webToken";

export function getWebToken(): string | null {
  try {
    const raw = window.localStorage.getItem(TOKEN_STORAGE_KEY);
    return raw && raw.trim() !== "" ? raw.trim() : null;
  } catch {
    return null;
  }
}

export function setWebToken(token: string): void {
  const cleaned = token.trim();
  if (!cleaned) return;
  try {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, cleaned);
  } catch {
    // ignore storage errors
  }
}

export function clearWebToken(): void {
  try {
    window.localStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    // ignore storage errors
  }
}

export function authHeaders(): Record<string, string> {
  const token = getWebToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function authFetch(input: string, init?: RequestInit): Promise<Response> {
  const token = getWebToken();
  if (!token) return fetch(input, init);
  const headers = new Headers(init?.headers);
  if (!headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return fetch(input, {
    ...init,
    headers,
    credentials: init?.credentials ?? "include",
  });
}

export function withAuthToken(url: string): string {
  const token = getWebToken();
  if (!token) return url;
  const hashIndex = url.indexOf("#");
  const base = hashIndex >= 0 ? url.slice(0, hashIndex) : url;
  const hash = hashIndex >= 0 ? url.slice(hashIndex) : "";
  const sep = base.includes("?") ? "&" : "?";
  return `${base}${sep}token=${encodeURIComponent(token)}${hash}`;
}
