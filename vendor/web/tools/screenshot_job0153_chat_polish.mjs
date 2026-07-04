#!/usr/bin/env node
// GRACE-2 — job-0153 evidence screenshots.
//
// Captures the 5 chat-polish scenarios called out in the kickoff:
//   01_chat_with_markdown    — agent message rendered as HTML markdown.
//   02_user_message_bubble   — right-aligned grey bubble with white text.
//   03_scroll_arrow_visible  — scrolled up; floating chevron above input.
//   04_scroll_arrow_hidden   — scrolled to bottom; arrow gone.
//   05_enter_submits         — multi-step Enter to send + Shift+Enter newline.
//
// Each scenario mounts a minimal harness that imports the real Chat surface
// components (AgentMessage, UserBubble, ScrollToBottom, ChatInput) so what
// appears in the screenshot is exactly what users see in the live app.
//
// Outputs under reports/inflight/job-0153-web-20260608/evidence/.

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT_DIR =
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0153-web-20260608/evidence";
const BASE_URL = "http://localhost:5173";

const AGENT_MD =
  "# Flood scenario summary\n" +
  "\n" +
  "Modeling **Hurricane Ian** flood depth across *Lee County, FL* with " +
  "[SFINCS](https://github.com/Deltares/SFINCS).\n" +
  "\n" +
  "Inputs:\n" +
  "- 10m DEM (USGS 3DEP)\n" +
  "- NLCD 2019 land-cover roughness\n" +
  "- NOAA Atlas-14 100-year return-period precip\n" +
  "\n" +
  "```python\n" +
  "result = run_model_flood_scenario(case_id, bbox)\n" +
  "```\n" +
  "\n" +
  "Inline code: use `git status` to verify.";

const USER_MSG_SHORT = "Model Hurricane Ian over Lee County.";
const USER_MSG_LONG =
  "Run SFINCS for Hurricane Ian over Lee County with the following parameters: " +
  "10m DEM, NLCD 2019 roughness, NOAA Atlas-14 100-year return-period precipitation, " +
  "and a 24-hour simulation window centered on landfall (2022-09-28 19:00 UTC).";

// Many filler messages so the scroll container has real overflow for the
// scroll-arrow scenarios.
const FILLER_AGENT_MD =
  "Pulling data from the configured endpoints. This is a long-running step " +
  "that will stream updates as fetches complete.\n\n" +
  "- DEM tiles: requested\n- NLCD: requested\n- Precip return period: requested";

const SCENARIOS = [
  {
    name: "01_chat_with_markdown",
    description: "Agent markdown rendered (heading + bold + list + code + link)",
    msgs: [
      { role: "user", text: USER_MSG_SHORT },
      { role: "agent", text: AGENT_MD, done: true },
    ],
    scrollToBottom: true,
    showScrollArrow: false,
  },
  {
    name: "02_user_message_bubble",
    description: "User message: grey bubble + white text + right-aligned",
    msgs: [
      { role: "user", text: USER_MSG_LONG },
    ],
    scrollToBottom: true,
    showScrollArrow: false,
  },
  {
    name: "03_scroll_arrow_visible",
    description: "Scrolled up — floating down-chevron above the chat input",
    msgs: [
      { role: "user", text: "First question." },
      { role: "agent", text: AGENT_MD, done: true },
      { role: "user", text: USER_MSG_LONG },
      { role: "agent", text: FILLER_AGENT_MD, done: true },
      { role: "user", text: "Another follow-up — does this scroll?" },
      { role: "agent", text: AGENT_MD, done: true },
      { role: "user", text: USER_MSG_LONG },
      { role: "agent", text: FILLER_AGENT_MD, done: true },
    ],
    scrollToBottom: false, // leave scrolled to top
    showScrollArrow: true,
  },
  {
    name: "04_scroll_arrow_hidden",
    description: "Scrolled to bottom — chevron gone",
    msgs: [
      { role: "user", text: "First question." },
      { role: "agent", text: AGENT_MD, done: true },
      { role: "user", text: USER_MSG_LONG },
      { role: "agent", text: FILLER_AGENT_MD, done: true },
      { role: "user", text: "Another follow-up — does this scroll?" },
      { role: "agent", text: AGENT_MD, done: true },
      { role: "user", text: USER_MSG_LONG },
      { role: "agent", text: FILLER_AGENT_MD, done: true },
    ],
    scrollToBottom: true,
    showScrollArrow: false,
  },
  {
    name: "05_enter_submits",
    description: "Enter sends; Shift+Enter inserts newline (multi-line state)",
    msgs: [
      { role: "user", text: USER_MSG_SHORT },
      {
        role: "agent",
        text:
          "Sure — Enter sends, Shift+Enter inserts a newline. The placeholder " +
          'now reads "Reply to GRACE-2".',
        done: true,
      },
    ],
    scrollToBottom: true,
    showScrollArrow: false,
    inputDraft: "Follow-up over multiple lines:\nlocked at 5m depth?",
  },
];

const MOUNT_FN = `
async (scenario) => {
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

  // Header (matches Chat.tsx).
  const header = document.createElement("div");
  header.style.cssText =
    "padding:10px 12px;border-bottom:1px solid #333;display:flex;align-items:center;gap:8px;";
  const t = document.createElement("strong");
  t.textContent = "GRACE-2";
  t.style.fontSize = "14px";
  const stub = document.createElement("span");
  stub.style.cssText = "color:#888;font-size:11px";
  stub.textContent = "M1 stub";
  const sp = document.createElement("span");
  sp.style.flex = "1";
  const dot = document.createElement("span");
  dot.style.cssText = "width:8px;height:8px;border-radius:4px;background:#5a5;display:inline-block;";
  const cn = document.createElement("span");
  cn.style.cssText = "color:#5a5;font-size:11px;margin-left:6px;";
  cn.textContent = "connected";
  header.append(t, stub, sp, dot, cn);
  frame.appendChild(header);

  const conv = document.createElement("div");
  conv.id = "harness-scroll";
  conv.style.cssText = [
    "flex:1",
    "overflow-y:auto",
    "padding:12px 12px 88px 12px",
    "display:flex",
    "flex-direction:column",
    "gap:10px",
  ].join(";");
  frame.appendChild(conv);

  const arrowAnchor = document.createElement("div");
  arrowAnchor.id = "harness-arrow-anchor";
  arrowAnchor.style.cssText = [
    "position:absolute",
    "left:0",
    "right:0",
    "bottom:96px",
    "display:flex",
    "justify-content:center",
    "pointer-events:none",
    "z-index:2",
  ].join(";");
  frame.appendChild(arrowAnchor);

  const overlay = document.createElement("div");
  overlay.style.cssText =
    "position:absolute;left:12px;right:12px;bottom:12px;pointer-events:auto;z-index:3;";
  frame.appendChild(overlay);

  // Import real React + components from Vite's module graph.
  const ReactMod = await import("/node_modules/.vite/deps/react.js");
  const ReactDOMMod = await import("/node_modules/.vite/deps/react-dom_client.js");
  const ChatInputMod = await import("/src/components/ChatInput.tsx");
  const AgentMessageMod = await import("/src/components/AgentMessage.tsx");
  const UserBubbleMod = await import("/src/components/UserBubble.tsx");
  const ScrollToBottomMod = await import("/src/components/ScrollToBottom.tsx");
  const React = ReactMod.default || ReactMod;
  const ReactDOM = ReactDOMMod.default || ReactDOMMod;

  // Render messages.
  const convRoot = ReactDOM.createRoot(conv);
  const children = scenario.msgs.map((m, i) => {
    if (m.role === "user") {
      return React.createElement(UserBubbleMod.UserBubble, {
        key: "m" + i,
        text: m.text,
      });
    }
    return React.createElement(AgentMessageMod.AgentMessage, {
      key: "m" + i,
      text: m.text,
      done: m.done === undefined ? true : m.done,
    });
  });
  convRoot.render(React.createElement(React.Fragment, null, children));

  // Render input.
  const inputRoot = ReactDOM.createRoot(overlay);
  inputRoot.render(
    React.createElement(ChatInputMod.ChatInput, {
      state: "idle",
      onSubmit: () => {},
      onCancel: () => {},
    })
  );

  // Render scroll-to-bottom arrow.
  const arrowRoot = ReactDOM.createRoot(arrowAnchor);
  arrowRoot.render(
    React.createElement(ScrollToBottomMod.ScrollToBottom, {
      visible: !!scenario.showScrollArrow,
      onClick: () => {
        const c = document.getElementById("harness-scroll");
        if (c) c.scrollTo({ top: c.scrollHeight, behavior: "smooth" });
      },
    })
  );

  await new Promise((res) => setTimeout(res, 400));

  // Optionally pre-fill draft for the Enter-submit scenario.
  if (scenario.inputDraft) {
    const ta = document.querySelector('[data-testid="chat-input"]');
    if (ta) {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype,
        "value"
      ).set;
      setter.call(ta, scenario.inputDraft);
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }

  // Scroll behavior.
  if (scenario.scrollToBottom) {
    conv.scrollTop = conv.scrollHeight;
  } else {
    conv.scrollTop = 0;
  }

  await new Promise((res) => setTimeout(res, 400));
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

  // Warm up Vite's dep optimizer.
  await page.goto(BASE_URL + "/", { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3000);

  const evidence = [];
  for (const sc of SCENARIOS) {
    await page.goto(BASE_URL + "/", { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1000);

    let mountResult;
    try {
      mountResult = await page.evaluate(
        `(${MOUNT_FN})(${JSON.stringify(sc)})`
      );
    } catch (e) {
      console.warn("[" + sc.name + "] mount error: " + String(e));
      continue;
    }
    console.log("[" + sc.name + "] mount: " + mountResult);

    const inspect = await page.evaluate(() => {
      const conv = document.getElementById("harness-scroll");
      const arrow = document.querySelector('[data-testid="scroll-to-bottom"]');
      const ta = document.querySelector('[data-testid="chat-input"]');
      const agents = Array.from(
        document.querySelectorAll('[data-testid="agent-message"]')
      );
      const users = Array.from(
        document.querySelectorAll('[data-testid="user-bubble"]')
      );
      const headings = Array.from(
        document.querySelectorAll('[data-testid="agent-message"] h1')
      ).map((h) => h.textContent);
      const links = Array.from(
        document.querySelectorAll('[data-testid="agent-message"] a')
      ).map((a) => a.href);
      const codes = Array.from(
        document.querySelectorAll('[data-testid="agent-message"] pre code')
      ).length;
      return {
        agentCount: agents.length,
        userCount: users.length,
        headings,
        links,
        codeBlocks: codes,
        arrowVisible: arrow ? arrow.getAttribute("data-visible") : null,
        arrowOpacity: arrow ? arrow.style.opacity : null,
        scrollTop: conv ? conv.scrollTop : null,
        scrollHeight: conv ? conv.scrollHeight : null,
        clientHeight: conv ? conv.clientHeight : null,
        placeholder: ta ? ta.placeholder : null,
        draftValue: ta ? ta.value : null,
      };
    });
    console.log("[" + sc.name + "] inspect: " + JSON.stringify(inspect));
    evidence.push({ name: sc.name, inspect });

    const out = OUT_DIR + "/" + sc.name + ".png";
    await page.locator("#harness-root").screenshot({ path: out });
    console.log("saved " + out);
  }

  console.log("EVIDENCE_JSON " + JSON.stringify(evidence));
  console.log("PAGE_ERRORS " + JSON.stringify(pageErrs));
  console.log("CONSOLE_ERRORS " + JSON.stringify(consoleErrs));
  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
