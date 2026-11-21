/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Dev-only override so the SPA can point at a mock on another port. */
  readonly VITE_WS_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
