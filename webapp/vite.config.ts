import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

/**
 * Static SPA build. `quartz web` serves `dist/` from the Python server, so there
 * is no SSR, no server runtime and no Node process in production.
 *
 * The dev server proxies `/ws` to the mock so `npm run dev` exercises the same
 * URL the shipped app uses (same-origin WebSocket, no CORS).
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      "/ws": { target: "ws://127.0.0.1:8765", ws: true, rewriteWsOrigin: true },
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
         */
        manualChunks: (id: string) => {
          if (id.includes("node_modules/highlight.js") || id.includes("node_modules/lowlight")) {
            return "highlight";
          }
          if (id.includes("node_modules/react-markdown") || id.includes("node_modules/remark") ||
              id.includes("node_modules/rehype") || id.includes("node_modules/micromark") ||
              id.includes("node_modules/mdast") || id.includes("node_modules/hast") ||
              id.includes("node_modules/unified")) {
            return "markdown";
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
