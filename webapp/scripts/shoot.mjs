#!/usr/bin/env node
/**
 * Screenshot both themes of the A2UI surface with the DOM as the source of truth.
 * Dev-only helper: uses the Chrome DevTools Protocol over `ws`, launched against
 * whatever URL is given. Not part of the shipped app or the test suite.
 *
 *   node scripts/shoot.mjs <url> <out-prefix> [width] [height]
 */
import { spawn } from "node:child_process";
import { existsSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { WebSocket } from "ws";

const [url, prefix = "shot", width = "1600", height = "1000"] = process.argv.slice(2);

/**
 * Find a headless Chromium. `CHROME` wins; otherwise the newest Playwright
 * download is used, so a machine that has run Playwright once just works.
 */
function findChrome() {
  if (process.env.CHROME) return process.env.CHROME;
  const root = join(process.env.HOME ?? "", ".cache", "ms-playwright");
  if (!existsSync(root)) return null;
  const candidates = readdirSync(root)
    .filter((entry) => entry.startsWith("chromium"))
    .sort()
    .reverse()
    .map((entry) =>
      join(root, entry, "chrome-headless-shell-linux64", "chrome-headless-shell"),
    );
  return candidates.find((candidate) => existsSync(candidate)) ?? null;
}

const CHROME = findChrome();
if (CHROME === null) {
  console.error("No headless Chromium found. Set CHROME=/path/to/chrome-headless-shell.");
  process.exit(1);
}

const HOST = "127.0.0.1";
const port = 9200 + Math.floor(Math.random() * 400);
const child = spawn(
  CHROME,
  [
    `--remote-debugging-port=${port}`,
    "--headless=new",
    "--disable-gpu",
    "--no-sandbox",
    `--window-size=${width},${height}`,
    "about:blank",
  ],
  { stdio: ["ignore", "ignore", "ignore"] },
);

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function targets() {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch(`http://${HOST}:${port}/json/list`);
      const list = await response.json();
      const page = list.find((entry) => entry.type === "page");
      if (page) return page;
    } catch {}
    await wait(150);
  }
  throw new Error("chrome never came up");
}

const page = await targets();
const socket = new WebSocket(page.webSocketDebuggerUrl, { maxPayload: 256 * 1024 * 1024 });
await new Promise((resolve, reject) => {
  socket.once("open", resolve);
  socket.once("error", reject);
});

let nextId = 0;
const pending = new Map();
socket.on("message", (raw) => {
  const message = JSON.parse(String(raw));
  if (message.id !== undefined && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(JSON.stringify(message.error)));
    else resolve(message.result ?? {});
  }
});
const call = (method, params = {}) =>
  new Promise((resolve, reject) => {
    nextId += 1;
    pending.set(nextId, { resolve, reject });
    socket.send(JSON.stringify({ id: nextId, method, params }));
  });

await call("Page.enable");
await call("Runtime.enable");
await call("Emulation.setDeviceMetricsOverride", {
  width: Number(width),
  height: Number(height),
  deviceScaleFactor: 1,
  mobile: false,
});
await call("Page.navigate", { url });
await wait(2500);

const evaluate = async (expression) => {
  const result = await call("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  return result.result?.value;
};

const shoot = async (name) => {
  const shot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(`${prefix}-${name}.png`, Buffer.from(shot.data, "base64"));
  console.log(`wrote ${prefix}-${name}.png`);
};

const theme = process.env.THEME ?? "light";
const prompt = process.env.PROMPT;

const click = async (selector) => {
  const ok = await evaluate(`(() => { const node = document.querySelector(${JSON.stringify(selector)}); if (!node) return false; node.click(); return true; })()`);
  if (!ok) throw new Error(`no element for ${selector} at ${url}`);
};

if (theme === "dark" || theme === "light") {
  await click(`[aria-label='${theme === "dark" ? "Dark" : "Light"} theme']`);
  await wait(300);
}
await click("[aria-label='Dashboard response']");
if (prompt) {
  await evaluate(`
    (() => {
      const box = document.querySelector("[aria-label='Prompt quartz']");
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
      setter.call(box, ${JSON.stringify(prompt)});
      box.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    })()
  `);
  await evaluate(`document.querySelector("[aria-label='Prompt quartz']").dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }))`);
  await wait(Number(process.env.WAIT ?? 6000));
}
await evaluate(`document.querySelector(".a2ui-scope")?.scrollIntoView({ block: "center" }); true`);
await wait(400);
await shoot(theme);

const info = await evaluate(`
  JSON.stringify({
    scopes: document.querySelectorAll(".a2ui-scope").length,
    text: document.querySelector(".a2ui-scope")?.innerText ?? null,
  })
`);
console.log(info);
socket.close();
child.kill();
process.exit(0);
