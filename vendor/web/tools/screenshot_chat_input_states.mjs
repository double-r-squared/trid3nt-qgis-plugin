#!/usr/bin/env node
// GRACE-2 — job-0144 evidence screenshots.
//
// Captures the merged send/stop chat input across four states + a multi-line
// growth scenario. Playwright mounts a hand-rolled frame containing a fresh
// React tree that imports the actual ChatInput.tsx via Vite's module graph
// (so what's screenshot'd is the same component shipped to users).
//
// Outputs under reports/inflight/job-0144-web-20260608/evidence/.

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT_DIR =
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0144-web-20260608/evidence";
const BASE_URL = "http://localhost:5173";

const SCENARIOS = [
  { name: "01_idle_empty", state: "idle", draft: "", msgs: [] },
  {
    name: "02_idle_with_text",
    state: "idle",
    draft: "Model the Hurricane Ian flood across Lee County, FL.",
    msgs: [{ role: "agent", text: "Hi — what would you like to model?" }],
  },
  {
    name: "03_in_flight_stop",
    state: "in-flight",
    draft: "",
    msgs: [
      { role: "user", text: "Model the Hurricane Ian flood across Lee County, FL." },
      { role: "agent", text: "Pulling DEM, NLCD, and precip return-period tiles…" },
    ],
  },
  {
    name: "04_post_cancel_idle",
    state: "idle",
    draft: "",
    msgs: [
      { role: "user", text: "Model the Hurricane Ian flood across Lee County, FL." },
      { role: "agent", text: "Cancelled. Loaded layers remain visible." },
    ],
  },
  {
    name: "05_multiline_growth",
    state: "idle",
    draft:
      "Run SFINCS for Hurricane Ian over Lee County with the following parameters: " +
      "10m DEM resolution, NLCD 2019 roughness, NOAA Atlas 14 100-year return period " +
      "precipitation, and a 24-hour simulation window centered on landfall " +
      "(2022-09-28 19:00 UTC). Output flood depth, max velocity, and a damage envelope " +
      "from Pelicun for buildings within 5km of the Caloosahatchee River. " +
      "Save the run under the active Case.",
    msgs: [],
  },
];

const MOUNT_FN = `
async (state, draft, msgs) => {
  document.body.innerHTML = "";
  document.body.style.cssText =
    "margin:0;padding:0;background:#0d0d11;font-family:system-ui,sans-serif;font-size:13px;";
  const root = document.createElement("div");
  root.id = "harness-root";
  document.body.appendChild(root);

  const frame = document.createElement("div");
  frame.style.cssText = [
    "position:relative",
    "width:380px",
    "height:600px",
    "margin:32px auto",
    "background:rgba(20,20,25,0.92)",
    "color:#eee",
    "border-radius:8px",
    "box-shadow:0 4px 24px rgba(0,0,0,0.4)",
    "overflow:hidden",
    "display:flex",
    "flex-direction:column",
  ].join(";");
  root.appendChild(frame);

  const header = document.createElement("div");
  header.style.cssText =
    "padding:10px 12px;border-bottom:1px solid #333;display:flex;align-items:center;gap:8px;";
  const t = document.createElement("strong");
  t.textContent = "GRACE-2";
  t.style.fontSize = "14px";
  const s = document.createElement("span");
  s.style.cssText = "color:#888;font-size:11px";
  s.textContent = "M1 stub";
  const sp = document.createElement("span");
  sp.style.flex = "1";
  const dot = document.createElement("span");
  dot.style.cssText = "width:8px;height:8px;border-radius:4px;background:#5a5;display:inline-block;";
  const cn = document.createElement("span");
  cn.style.cssText = "color:#5a5;font-size:11px;margin-left:6px;";
  cn.textContent = "connected";
  header.append(t, s, sp, dot, cn);
  frame.appendChild(header);

  const conv = document.createElement("div");
  conv.style.cssText = [
    "flex:1",
    "overflow-y:auto",
    "padding:12px 12px 88px 12px",
    "display:flex",
    "flex-direction:column",
    "gap:10px",
  ].join(";");
  frame.appendChild(conv);

  for (const m of msgs) {
    const b = document.createElement("div");
    b.style.cssText = [
      m.role === "user" ? "align-self:flex-end" : "align-self:flex-start",
      "background:" + (m.role === "user" ? "#264" : "#222"),
      "padding:8px 10px",
      "border-radius:6px",
      "max-width:85%",
      "white-space:pre-wrap",
      "word-break:break-word",
    ].join(";");
    b.textContent = m.text;
    conv.appendChild(b);
  }

  const overlay = document.createElement("div");
  overlay.style.cssText =
    "position:absolute;left:12px;right:12px;bottom:12px;pointer-events:auto;";
  frame.appendChild(overlay);

  const ReactMod = await import("/node_modules/.vite/deps/react.js");
  const ReactDOMMod = await import("/node_modules/.vite/deps/react-dom_client.js");
  const ChatInputMod = await import("/src/components/ChatInput.tsx");
  const React = ReactMod.default || ReactMod;
  const ReactDOM = ReactDOMMod.default || ReactDOMMod;

  const r = ReactDOM.createRoot(overlay);
  r.render(
    React.createElement(ChatInputMod.ChatInput, {
      state,
      onSubmit: (text) => { window.__lastSubmit = text; },
      onCancel: () => { window.__lastCancel = (window.__lastCancel || 0) + 1; },
    })
  );

  await new Promise((res) => setTimeout(res, 150));
  if (draft) {
    const ta = document.querySelector('[data-testid="chat-input"]');
    if (ta) {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype,
        "value"
      ).set;
      setter.call(ta, draft);
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }
  await new Promise((res) => setTimeout(res, 300));
  return "ok";
}
`;

async function main() {
  await mkdir(OUT_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 480, height: 720 },
    deviceScaleFactor: 2,
  });
  const page = await context.newPage();

  const pageErrs = [];
  const consoleErrs = [];
  page.on("pageerror", (e) => {
    pageErrs.push(e.message);
    console.warn("[harness] pageerror:", e.message);
  });
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      consoleErrs.push(msg.text());
      console.warn("[harness] console.error:", msg.text());
    }
  });

  // Warm up Vite's dep optimizer with a brief navigate.
  await page.goto(BASE_URL + "/", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2000);

  const evidence = [];
  for (const sc of SCENARIOS) {
    await page.goto(BASE_URL + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(800);

    let mountResult;
    try {
      mountResult = await page.evaluate(
        `(${MOUNT_FN})(${JSON.stringify(sc.state)}, ${JSON.stringify(sc.draft)}, ${JSON.stringify(sc.msgs)})`
      );
    } catch (e) {
      console.warn(`[${sc.name}] mount error: ${String(e)}`);
      continue;
    }
    console.log(`[${sc.name}] mount: ${mountResult}`);

    const inspect = await page.evaluate(() => {
      const el = document.querySelector('[data-testid="chat-input-wrapper"]');
      if (!el) return { error: "no wrapper" };
      const cs = window.getComputedStyle(el);
      const btn = document.querySelector('[data-testid="chat-input-action"]');
      const glyph = document.querySelector('[data-testid="chat-input-glyph"]');
      const ta = document.querySelector('[data-testid="chat-input"]');
      return {
        boxShadow: cs.boxShadow,
        borderRadius: cs.borderRadius,
        background: cs.backgroundColor,
        wrapperState: el.getAttribute("data-state"),
        glyph: glyph ? glyph.getAttribute("data-glyph") : null,
        actionAria: btn ? btn.getAttribute("aria-label") : null,
        actionDisabled: btn ? btn.hasAttribute("disabled") : null,
        textareaHeight: ta ? ta.style.height : null,
      };
    });
    console.log(`[${sc.name}]`, JSON.stringify(inspect));
    evidence.push({ name: sc.name, inspect });

    if (!inspect.error) {
      const out = OUT_DIR + "/" + sc.name + ".png";
      await page.locator("#harness-root").screenshot({ path: out });
      console.log(`saved ${out}`);
    }
  }

  console.log("EVIDENCE_JSON " + JSON.stringify(evidence));
  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
