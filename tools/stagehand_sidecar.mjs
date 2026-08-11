#!/usr/bin/env node
/**
 * Persistent Stagehand v4 worker for Hermes browser_* tools.
 *
 * Protocol: one JSON request per stdin line, one JSON response per stdout line.
 * Stagehand/browser diagnostics are redirected to stderr so stdout remains a
 * machine-readable channel. The Python edge owns URL policy and secret
 * redaction; this worker receives only browser-scoped credentials.
 */
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";
import {
  Stagehand,
  browserbase,
  localBrowser,
} from "@browserbasehq/stagehand";

for (const level of ["log", "info", "warn", "error", "debug"]) {
  console[level] = (...args) => {
    process.stderr.write(`[stagehand:${level}] ${args.map(String).join(" ")}\n`);
  };
}

const INTERACTIVE_ROLES = new Set([
  "button", "link", "textbox", "searchbox", "combobox", "checkbox",
  "radio", "switch", "slider", "spinbutton", "menuitem", "option",
  "tab", "treeitem", "input", "textarea", "select",
]);

let browser;
let stagehand;
let page;
let refs = new Map();
let consoleMessages = [];
let pageErrors = [];
let selfHealAvailable = false;
let closing = false;

function reply(id, payload) {
  process.stdout.write(`${JSON.stringify({ id, ...payload })}\n`);
}

function errorText(error) {
  return error instanceof Error ? error.message : String(error);
}

function requireOpenPage() {
  if (!page) throw new Error("No browser session. Call browser_navigate first.");
  return page;
}

async function attachPageEvents(target) {
  try {
    await target.on("console", (event) => {
      consoleMessages.push({ type: "console", ...event });
      if (consoleMessages.length > 500) consoleMessages.shift();
    });
  } catch (error) {
    console.warn(`console subscription unavailable: ${errorText(error)}`);
  }
  try {
    await target.on("pageerror", (event) => {
      pageErrors.push({ type: "pageerror", ...event });
      if (pageErrors.length > 500) pageErrors.shift();
    });
  } catch (error) {
    console.warn(`pageerror subscription unavailable: ${errorText(error)}`);
  }
}

function modelConfig(config) {
  const modelName = String(config.model || "openai/gpt-5-mini");
  if (!modelName.startsWith("openai/")) {
    throw new Error(`Stagehand model must use OpenAI/GPT (received ${modelName})`);
  }
  // Use a browser-scoped credential rather than exposing Hermes' general
  // provider keyring to an npm subprocess.
  const apiKey = process.env.STAGEHAND_OPENAI_API_KEY || "";
  selfHealAvailable = Boolean(apiKey && config.selfHeal !== false);
  return apiKey ? { modelName, apiKey } : undefined;
}

async function initialize(config) {
  if (stagehand) return;
  const backend = config.backend || "local";
  if (backend === "browserbase") {
    const apiKey = process.env.BROWSERBASE_API_KEY;
    const projectId = process.env.BROWSERBASE_PROJECT_ID;
    if (!apiKey || !projectId) {
      throw new Error("Browserbase requires BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID");
    }
    const launchOptions = {
      apiKey,
      projectId,
      proxies: config.proxies !== false,
      keepAlive: true,
    };
    if (config.browserbaseBaseUrl) launchOptions.baseUrl = config.browserbaseBaseUrl;
    browser = await browserbase.launch(launchOptions);
  } else if (backend === "cdp") {
    if (!config.cdpUrl) throw new Error("Stagehand CDP mode requires cdpUrl");
    browser = await localBrowser.connect({ cdpUrl: config.cdpUrl });
  } else if (backend === "local") {
    const launchOptions = {
      headless: config.headed !== true,
      chromiumSandbox: config.chromiumSandbox !== false,
      keepAlive: false,
    };
    if (config.executablePath) launchOptions.executablePath = config.executablePath;
    browser = await localBrowser.launch(launchOptions);
  } else {
    throw new Error(`Unsupported Stagehand backend: ${backend}`);
  }

  const model = modelConfig(config);
  const createOptions = {
    browser,
    selfHeal: config.selfHeal !== false,
    domSettleTimeoutMs: Number(config.domSettleTimeoutMs || 3000),
  };
  if (model) createOptions.model = model;
  stagehand = await Stagehand.create(createOptions);
  const pages = await browser.context.pages();
  page = pages[0] || await browser.context.newPage();
  await browser.context.setActivePage(page);
  await attachPageEvents(page);
}

function parseSnapshot(snapshot) {
  const formattedTree = String(snapshot?.formattedTree || "");
  const xpathMap = snapshot?.xpathMap || {};
  const urlMap = snapshot?.urlMap || {};
  refs = new Map();
  let index = 0;
  const output = [];
  for (const rawLine of formattedTree.split("\n")) {
    const match = rawLine.match(/^(\s*)\[([^\]]+)]\s*([^:]+)(?::\s*(.*))?$/);
    if (!match) {
      if (rawLine.trim()) output.push(rawLine);
      continue;
    }
    const [, indent, nodeId, rawRole, rawLabel = ""] = match;
    const role = rawRole.trim().toLowerCase();
    const xpath = xpathMap[nodeId];
    const label = rawLabel.trim();
    if (xpath && INTERACTIVE_ROLES.has(role)) {
      index += 1;
      const ref = `e${index}`;
      refs.set(ref, { xpath, role, label, url: urlMap[nodeId] });
      const escaped = label.replaceAll('"', '\\"');
      output.push(`${indent}- ${role}${escaped ? ` "${escaped}"` : ""} [ref=${ref}]`);
    } else {
      output.push(`${indent}${role}${label ? `: ${label}` : ""}`);
    }
  }
  return { text: output.join("\n"), refs: Object.fromEntries(refs) };
}

function resolveRef(value) {
  const key = String(value || "").replace(/^@/, "");
  const entry = refs.get(key);
  if (!entry) throw new Error(`Unknown or stale element ref @${key}; call browser_snapshot again`);
  return { key, ...entry };
}

async function selfHealClick(entry, originalError) {
  if (!selfHealAvailable) throw originalError;
  const safeLabel = entry.label.replace(/[\r\n]+/g, " ").slice(0, 200);
  const instruction = safeLabel
    ? `Click the ${entry.role} named "${safeLabel}"`
    : `Click the ${entry.role} at selector ${entry.xpath}`;
  const result = await stagehand.act(instruction, { page });
  return { self_healed: true, stagehand: result };
}

async function runCommand(command, args = []) {
  const target = requireOpenPage();
  switch (command) {
    case "open": {
      await target.goto(String(args[0]));
      return { url: await target.url(), title: await target.title() };
    }
    case "snapshot": {
      const parsed = parseSnapshot(await target.snapshot());
      return { snapshot: parsed.text, refs: parsed.refs };
    }
    case "click": {
      const entry = resolveRef(args[0]);
      try {
        await target.locator(entry.xpath).click();
        return { clicked: `@${entry.key}`, self_healed: false };
      } catch (error) {
        return await selfHealClick(entry, error);
      }
    }
    case "fill": {
      const entry = resolveRef(args[0]);
      await target.locator(entry.xpath).fill(String(args[1] ?? ""));
      return { filled: `@${entry.key}` };
    }
    case "scroll": {
      const direction = String(args[0]);
      const pixels = Number(args[1] || 500);
      await target.evaluate(({ direction: dir, pixels: px }) => {
        window.scrollBy(0, dir === "up" ? -px : px);
      }, { direction, pixels });
      return { direction, pixels };
    }
    case "back": {
      await target.goBack();
      return { url: await target.url(), title: await target.title() };
    }
    case "press": {
      await target.keyPress(String(args[0]));
      return { key: String(args[0]) };
    }
    case "eval": {
      return { result: await target.evaluate(String(args[0])) };
    }
    case "console": {
      const clear = args.includes("--clear");
      const messages = [...consoleMessages];
      if (clear) consoleMessages = [];
      return { messages };
    }
    case "errors": {
      const clear = args.includes("--clear");
      const errors = [...pageErrors];
      if (clear) pageErrors = [];
      return { errors };
    }
    case "screenshot": {
      const bytes = await target.screenshot({ fullPage: args.includes("--full") || args.includes("--full-page") });
      const directory = process.env.HERMES_STAGEHAND_SCREENSHOT_DIR || os.tmpdir();
      await fs.mkdir(directory, { recursive: true, mode: 0o700 });
      const requestedPath = [...args].reverse().find((arg) => !String(arg).startsWith("--"));
      const outputPath = requestedPath
        ? path.resolve(String(requestedPath))
        : path.join(directory, `stagehand-${process.pid}-${Date.now()}.png`);
      await fs.mkdir(path.dirname(outputPath), { recursive: true, mode: 0o700 });
      await fs.writeFile(outputPath, bytes, { mode: 0o600 });
      return { path: outputPath, annotated: false };
    }
    case "record":
      return { warning: "Stagehand v4 recording is not exposed by this driver" };
    default:
      throw new Error(`Unsupported Stagehand command: ${command}`);
  }
}

async function shutdown() {
  if (closing) return;
  closing = true;
  const deadline = (promise, milliseconds, label) => Promise.race([
    promise,
    new Promise((resolve) => setTimeout(() => {
      console.warn(`${label} exceeded ${milliseconds}ms; forcing sidecar shutdown`);
      resolve();
    }, milliseconds)),
  ]);
  try {
    if (stagehand) await deadline(stagehand.close(), 4000, "stagehand.close");
  } finally {
    try {
      if (browser && !browser.closed) await deadline(browser.close(), 2000, "browser.close");
    } catch (error) {
      console.warn(`browser close failed: ${errorText(error)}`);
    }
    page = undefined;
    stagehand = undefined;
    browser = undefined;
  }
}

function replyAndExit(id, payload, code = 0) {
  // A referenced watchdog guarantees process termination even when Stagehand,
  // Chrome, or OpenTelemetry leave handles behind. The stdout callback ensures
  // the close acknowledgement is flushed before the normal exit path.
  const watchdog = setTimeout(() => process.exit(code), 250);
  process.stdout.write(`${JSON.stringify({ id, ...payload })}\n`, () => {
    clearTimeout(watchdog);
    process.exit(code);
  });
}

for (const signal of ["SIGTERM", "SIGINT"]) {
  process.once(signal, async () => {
    await shutdown().catch((error) => console.warn(errorText(error)));
    process.exit(0);
  });
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
  if (!line.trim()) continue;
  let request;
  try {
    request = JSON.parse(line);
    const { id, command, args = [], config = {} } = request;
    if (command === "close") {
      await shutdown();
      replyAndExit(id, { success: true, data: {} });
      await new Promise(() => {});
    }
    await initialize(config);
    const data = await runCommand(command, args);
    reply(id, { success: true, data });
  } catch (error) {
    reply(request?.id ?? null, { success: false, error: errorText(error) });
  }
}

if (!closing) await shutdown();
process.exit(0);
