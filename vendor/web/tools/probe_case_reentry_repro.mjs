#!/usr/bin/env node
// job-0275 probe: reproduce "can't get back into the Case" WITHOUT Gemini.
// Creates Cases via the real UI (+ New Case → case-command create), then
// stress-cycles: navigate out, re-enter, rapid deselect/select, delete one,
// re-enter survivors. Every outbound case-command and inbound case-open is
// logged; a click that emits NO select = the user's symptom reproduced.
import { chromium } from "@playwright/test";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const sent = [];
const received = [];
const errors = [];
const t0 = Date.now();
const rel = () => Date.now() - t0;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
page.on("console", (m) => {
  if (m.type() === "error") errors.push({ t: rel(), e: m.text().slice(0, 250) });
});
page.on("pageerror", (e) => errors.push({ t: rel(), e: "PAGEERROR " + String(e).slice(0, 250) }));
page.on("websocket", (ws) => {
  ws.on("framesent", (d) => {
    try {
      const p = JSON.parse(typeof d.payload === "string" ? d.payload : d.payload.toString());
      if (p?.type === "case-command")
        sent.push({ t: rel(), cmd: p.payload?.command, case: p.payload?.case_id?.slice(-6) ?? null });
    } catch {}
  });
  ws.on("framereceived", (d) => {
    try {
      const p = JSON.parse(typeof d.payload === "string" ? d.payload : d.payload.toString());
      if (p?.type === "case-open")
        received.push({ t: rel(), open: p.payload?.session_state?.case?.case_id?.slice(-6) ?? "NULL" });
      if (p?.type === "case-list")
        received.push({ t: rel(), list: (p.payload?.cases ?? []).length });
    } catch {}
  });
});

await page.goto("http://localhost:5173", { waitUntil: "domcontentloaded" });
const anon = page.locator('[data-testid="grace2-auth-gate-anonymous"]');
if (await anon.isVisible({ timeout: 4000 }).catch(() => false)) await anon.click();
await sleep(2500);

// Helper: find the New Case button + back-to-Cases breadcrumb + rows.
const newCaseBtn = page.getByRole("button", { name: /new case/i }).first();
const backCrumb = () =>
  page.locator('[data-testid="back-to-cases"], [data-testid="grace2-breadcrumb-back"]').first();
const rows = () =>
  page.locator('[data-testid="grace2-case-row"]');

async function goRoot(tag) {
  const bc = backCrumb();
  if (await bc.isVisible({ timeout: 1500 }).catch(() => false)) {
    await bc.click();
  } else {
    // fall back: any element whose text is exactly "Cases"
    await page.getByText("Cases", { exact: true }).first().click().catch(() => {});
  }
  await sleep(800);
  console.log(`[${tag}] at root; rows visible=${await rows().count().catch(() => -1)}`);
}

// Save-gate dismissal: anonymous create/delete arms the modal; "Continue
// anyway" matches the user's real flow.
async function passSaveGate() {
  const cont = page.locator('[data-testid="grace2-save-gate-modal-continue"]');
  if (await cont.isVisible({ timeout: 1200 }).catch(() => false)) {
    await cont.click();
    await sleep(400);
  }
}

// 1. Create two Cases through the real UI.
for (const n of [1, 2]) {
  if (await newCaseBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
    await newCaseBtn.click();
    await passSaveGate();
    await sleep(1500);
    console.log(`[create ${n}] done`);
    await goRoot(`after-create-${n}`);
  } else {
    console.log("NO New Case button found — dumping testids");
    const ids = await page.evaluate(() =>
      Array.from(document.querySelectorAll("[data-testid]")).map((e) =>
        e.getAttribute("data-testid")
      )
    );
    console.log(JSON.stringify([...new Set(ids)]));
    break;
  }
}

// 2. Stress cycle: enter row 1, out, enter row 2, out, rapid double-clicks.
for (let cycle = 1; cycle <= 4; cycle++) {
  const count = await rows().count();
  if (count === 0) {
    console.log(`[cycle ${cycle}] NO ROWS VISIBLE — symptom candidate`);
    await page.screenshot({ path: `/tmp/reentry_cycle${cycle}.png` });
    break;
  }
  const idx = cycle % count;
  await rows().nth(idx).click();
  await sleep(1200);
  const opened = received.filter((r) => r.open).length;
  console.log(`[cycle ${cycle}] clicked row ${idx}; selects=${sent.filter((s) => s.cmd === "select").length} opens=${opened}`);
  await goRoot(`cycle-${cycle}`);
}

// 3. Delete the first Case, then try re-entering the survivor.
const delBtn = page
  .locator('[data-testid="grace2-case-row-delete"]')
  .first();
if (await delBtn.isVisible({ timeout: 1500 }).catch(() => false)) {
  await delBtn.click();
  await passSaveGate();
  await sleep(600);
  const confirm = page.getByRole("button", { name: /delete|confirm/i }).last();
  if (await confirm.isVisible({ timeout: 1500 }).catch(() => false)) await confirm.click();
  await sleep(1500);
  console.log("[delete] done");
}
const survivors = await rows().count();
if (survivors > 0) {
  await rows().first().click();
  await sleep(1500);
}
await page.screenshot({ path: "/tmp/reentry_final.png" });

console.log("SENT case-commands:", JSON.stringify(sent));
console.log("RECEIVED:", JSON.stringify(received.slice(-15)));
console.log("ERRORS:", JSON.stringify(errors));
const selects = sent.filter((s) => s.cmd === "select").length;
const opens = received.filter((r) => r.open).length;
console.log(`[verdict] selects_sent=${selects} case_opens=${opens} errors=${errors.length}`);
await browser.close();
