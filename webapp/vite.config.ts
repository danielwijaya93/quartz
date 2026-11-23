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
    sourcemap: true,
    rollupOptions: {
      output: {
        /**
         * Three chunks instead of one 680 kB bundle: the highlight.js language
         * pack is by far the heaviest dependency and changes least often, so
         * splitting it keeps the app chunk small and cacheable.
         *
         * There is no `markdown` chunk either, though one existed. Splitting the
         * markdown pipeline out created a `markdown <-> vendor` cycle, which was
         * harmless until the OpenUI renderer -- itself a `react-markdown` consumer
         * -- joined the graph, at which point the production bundle threw
         * `Cannot access '<sym>' before initialization` on load: a chunk needed
         * bindings from a chunk that had not finished initialising. One fewer
         * hand-cut boundary is worth more than the cache granularity it bought.
         *
         * The OpenUI renderer is likewise left out of `vendor`, so Rollup places it
         * in the lazy chunk that imports it rather than in the eager one.
         *
         * What is deferred, concretely: the renderer module (`React.lazy`), its
         * 356 kB stylesheet (imported by that module rather than by `index.css`),
         * its chart/table dependencies, and the generated spec + prompt (86 kB,
         * imported only when Rich UI is on).
         */
        manualChunks: (id: string) => {
          if (
            id.includes("node_modules/@openuidev") ||
            id.includes("node_modules/recharts") ||
            id.includes("node_modules/d3-") ||
            id.includes("node_modules/lodash-es") ||
            id.includes("node_modules/goober") ||
            id.includes("node_modules/@radix-ui") ||
            id.includes("node_modules/@tanstack")
          ) {
            return undefined;
          }
          if (id.includes("commonjsHelpers")) {
            return "commonjs";
          }
          if (id.includes("node_modules/highlight.js")) {
            return "highlight";
          }
          if (id.includes("node_modules")) return "vendor";
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
    include: ["src/**/*.test.{ts,tsx}"],
  },
});