#!/usr/bin/env node
// TRID3NT -- front-page multi-layer screenshot capture against the LIVE site.
// Drives the code-gate, selects Haiku (cost), sends one prompt, waits for the
// agent to fetch/render layers, and saves periodic + final screenshots.
//
// Env:
//   CASE_ID   short slug for filenames (e.g. "tampa")
//   PROMPT    the chat prompt to send (geocode path, no coords)
//   OUT_DIR   where PNGs land (default scratchpad)
//   MAX_MS    total wait budget after sending prompt (default 360000 = 6min)
//   URL       site (default https://trid3nt.vercel.app/app)
//   CODE      access code (default trident-demo-4db31803)
//   HEADLESS  "0" to watch (default headless)
//
// Exit 0 = at least one screenshot written; layer-legend presence is logged.

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const CASE_ID = process.env.CASE_ID ?? "case";
const PROMPT = process.env.PROMPT ?? "Show building footprints, elevation, and rivers for downtown Tampa, Florida";
const OUT_DIR = process.env.OUT_DIR ?? "/tmp/claude-1000/-home-nate-Documents-GRACE-2/fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/shots";
const MAX_MS = Number(process.env.MAX_MS ?? 360000);
const URL = process.env.URL ?? "https://trid3nt.vercel.app/app";
const CODE = process.env.CODE ?? "trident-demo-4db31803";
const HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0";

const log = (...a) => console.log(`[${CASE_ID}]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function shot(page, tag) {
  const p = `${OUT_DIR}/${CASE_ID}_${tag}.png`;
  await page.screenshot({ path: p, fullPage: false }).catch((e) => log("shot fail", tag, e.message));
  return p;
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const browser = await chromium.launch({ headless: process.env.HEADLESS !== "0" });
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 });
  if (process.env.DARK === "1") {
    await ctx.addInitScript(() => { try { localStorage.setItem("grace2.theme", "dark"); } catch {} });
    log("dark theme enabled (grace2.theme=dark)");
  }
  const page = await ctx.newPage();
  page.on("console", (m) => { const t = m.text(); if (/error|fail|1005|1006/i.test(t)) log("page-console:", t.slice(0, 160)); });

  log("goto", URL);
  await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 60000 });

  // --- code gate ---
  const codeInput = page.getByTestId("grace2-code-input");
  try {
    await codeInput.waitFor({ state: "visible", timeout: 30000 });
    log("code gate present -> entering code");
    await codeInput.fill(CODE);
    await page.getByTestId("grace2-code-submit").click();
  } catch {
    log("no code gate (already authed or different shell)");
  }

  // --- wait for authed app shell (chat input); WAKE the box if asleep ---
  const chat = page.getByTestId("chat-input");
  const deadline = Date.now() + 220000; // box wake can take ~90s+
  let woke = false;
  while (Date.now() < deadline) {
    if (await chat.isVisible().catch(() => false)) break;
    const wake = page.getByTestId("wake-overlay-rect");
    if ((await wake.count().catch(() => 0)) > 0 && (await wake.first().isVisible().catch(() => false))) {
      if (!woke) { log("wake overlay present -> clicking to wake box"); woke = true; }
      await wake.first().click({ timeout: 5000 }).catch(() => {});
    }
    await sleep(3000);
  }
  await chat.waitFor({ state: "visible", timeout: 8000 });
  log("authed; chat input visible. waiting for WS/box-wake settle...");
  await sleep(4000);
  await shot(page, "00_authed");

  // --- select Haiku to save cost ---
  try {
    const modelBtn = page.getByTestId("model-selector-button").or(page.getByTestId("chat-input-model")).first();
    await modelBtn.click({ timeout: 8000 });
    await page.getByTestId(`model-option-${HAIKU}`).click({ timeout: 8000 });
    log("selected Haiku");
  } catch (e) {
    log("model select skipped:", e.message.slice(0, 80));
    await page.keyboard.press("Escape").catch(() => {});
  }

  // --- send the prompt ---
  await chat.click();
  await chat.fill(PROMPT);
  await sleep(300);
  await page.keyboard.press("Enter");
  log("prompt sent:", PROMPT);
  const t0 = Date.now();

  // --- poll: periodic shots, detect layer legend ---
  let sawLegend = false;
  let tick = 0;
  while (Date.now() - t0 < MAX_MS) {
    await sleep(20000);
    tick += 1;
    // auto-accept the #154 resolution/granularity gate so DEM/terrain cases proceed
    try {
      const conf = page.getByTestId("resolution-picker-confirm");
      if (await conf.count() > 0 && await conf.first().isVisible().catch(() => false)) {
        await conf.first().click({ timeout: 4000 });
        log("auto-confirmed resolution gate");
      }
    } catch { /* none up */ }
    const legend = page.getByTestId("grace2-layer-legend");
    const legendCount = await legend.count().catch(() => 0);
    const legendVisible = legendCount > 0 ? await legend.first().isVisible().catch(() => false) : false;
    if (legendVisible) sawLegend = true;
    await shot(page, `t${String(tick).padStart(2, "0")}`);
    log(`+${Math.round((Date.now() - t0) / 1000)}s legendVisible=${legendVisible}`);
    // heuristic: once we've seen the legend and held it for 2 ticks, we likely have rendered layers
    if (sawLegend && tick >= 3) break;
  }

  // settle, then produce a CLEAN map shot: dismiss the resolution gate if any,
  // close the Layers panel so it does not occlude the data, and zoom OUT a step
  // so all rendered layers sit inside the viewport with context.
  await sleep(3000);
  await shot(page, "FINAL_panel"); // keep one with the legend visible for reference

  // close the Layers legend panel (aria-label "Hide legend")
  try {
    await page.getByRole("button", { name: "Hide legend" }).click({ timeout: 4000 });
    log("closed layers panel");
  } catch { log("no hide-legend control (continuing)"); }
  await sleep(800);

  // zoom out a couple of steps on the map canvas for context
  try {
    const canvas = page.locator("canvas").first();
    const box = await canvas.boundingBox();
    if (box) {
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
      await page.keyboard.press("Minus"); await sleep(700);
      log("zoomed out 1 step");
    }
  } catch (e) { log("zoom-out skipped:", e.message.slice(0, 60)); }
  await sleep(2500);

  const finalDefault = await shot(page, "FINAL");
  log("DONE", { sawLegend, final: finalDefault });
  await browser.close();
  // signal layer presence for the supervising agent
  console.log(`RESULT ${CASE_ID} sawLegend=${sawLegend}`);
}

main().catch((e) => { console.error("FATAL", e); process.exit(2); });
