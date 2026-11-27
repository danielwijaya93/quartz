import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";

const pkg = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf8")) as { version?: string };
const appVersion = pkg.version ?? "0.1.0";
let appBuild = "dev";
try {
  appBuild = execSync("git rev-list --count HEAD", { stdio: ["ignore", "pipe", "ignore"] }).toString().trim() || "dev";
} catch {
  /* Not a git checkout. */
}

/**
 * Static SPA build. `quartz web` serves `dist/` from the Python server, so there
 * is no SSR, no server runtime and no Node process in production.
 *
 * The dev server proxies `/ws` to the mock so `npm run dev` exercises the same
 * URL the shipped app uses (same-origin WebSocket, no CORS).
 */
export default defineConfig({
  define: {
    "import.meta.env.VITE_APP_VERSION": JSON.stringify(appVersion),
    "import.meta.env.VITE_APP_BUILD": JSON.stringify(appBuild),
  },
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      "/ws": { target: "ws://127.0.0.1:8765", ws: true, rewriteWsOrigin: true },
      "/api": { target: "http://127.0.0.1:8765" },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: process.env.VITE_SOURCEMAP === "true",
    rollupOptions: {
      output: {
        /**
         * Split `highlight.js` into its own cacheable chunk and let Rollup's
         * natural graph-based splitting keep OpenUI's heavy transitive
         * dependencies (`refractor`, `katex`, `zod`, `react-syntax-highlighter`,
         * `react-day-picker`, `date-fns`, and dynamic `lucide-react` icons)
         * inside the lazy `OpenUISurface` chunk instead of hoisting them into
         * an eager catch-all `vendor` bundle.
         */
        manualChunks: (id: string) => {
          if (id.includes("commonjsHelpers")) {
            return "commonjs";
          }
          if (id.includes("node_modules/highlight.js")) {
            return "highlight";
          }
          if (
            id.includes("node_modules/react/") ||
            id.includes("node_modules/react-dom/") ||
            id.includes("node_modules/scheduler/")
          ) {
            return "react-vendor";
          }
          return undefined;
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    css: false,
    include: ["src/**/*.test.{ts,tsx}", "mock/**/*.test.{ts,tsx}"],
  },
});