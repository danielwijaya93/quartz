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
         * Named chunks instead of one bundle: the highlight.js language pack is
         * by far the heaviest dependency and changes least often, so splitting it
         * keeps the app chunk small and cacheable.
         */
        manualChunks: (id: string) => {
          /**
           * A2UI is *excluded* from manual chunking rather than given a chunk of
           * its own. Naming a chunk does not make it lazy: the generic vendor rule
           * below would pull the renderer and its ~470 kB of lit/zod/markdown-it
           * into the eager vendor bundle, so everyone would download a renderer
           * most users never open. Returning nothing here lets Rollup place them
           * in the chunk the `React.lazy` import creates, which is not preloaded.
           */
          if (
            id.includes("node_modules/@a2ui/") ||
            id.includes("node_modules/lit/") ||
            id.includes("node_modules/lit-html/") ||
            id.includes("node_modules/@lit/") ||
            id.includes("node_modules/@preact/signals-core/") ||
            id.includes("node_modules/markdown-it/") ||
            id.includes("node_modules/dompurify/") ||
            id.includes("node_modules/redos-detector/") ||
            id.includes("node_modules/validate-color/") ||
            id.includes("node_modules/zod")
          ) {
            // No manual chunk: Rollup's own splitting then places these in the
            // chunk the `React.lazy` import creates and nowhere else.
            return undefined;
          }
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
