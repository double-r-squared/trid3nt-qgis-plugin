#!/usr/bin/env node
// GRACE-2 — job-0253 AuthGuard live-verify evidence.
//
// Two captures, both against a real Vite-served build with a stub WebSocket
// (no live agent, no live Firebase — per the kickoff):
//
//   A. DISABLED MODE (the load-bearing constraint) — against the ALREADY
//      RUNNING dev server (no VITE_FIREBASE_PROJECT_ID). The AuthGuard must be
//      a transparent pass-through: the app shell (or the pre-existing anonymous
//      AuthGate) renders EXACTLY as before; NO job-0253 sign-in surface, NO
//      sign-out affordance appear. This proves the dev/tailnet demo is
//      untouched.
//
//   B. ENABLED MODE — against a SEPARATE ephemeral Vite dev server started by
//      this script with dummy VITE_FIREBASE_* env (so isFirebaseConfigured()
//      is true) on a DIFFERENT port. The running dev server is never touched.
//      Signed-out → the minimal Google sign-in surface renders; the app shell
//      is hidden. We never click "Sign in with Google" (no live Firebase).
//
// The ephemeral server is torn down at the end regardless of outcome.

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";
import { spawn } from "child_process";

const OUT_DIR =
  process.argv[2] ??
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0253-web-20260611/evidence";
const RUNNING_URL = process.env.GRACE2_DEV_URL ?? "http://localhost:5173";
const ENABLED_PORT = Number(process.env.GRACE2_ENABLED_PORT ?? 5191);
const ENABLED_URL = `http://localhost:${ENABLED_PORT}`;

const STUB_WS = () => {
  class StubWS {
    constructor(url) {
      this._url = url;
      this._listeners = {};
      this._ready = 1;
      setTimeout(() => {
        (this._listeners["open"] ?? []).forEach((cb) => cb({}));
      }, 0);
    }
    get readyState() {
      return this._ready;
    }
    addEventListener(type, cb) {
      (this._listeners[type] ??= []).push(cb);
    }
    send() {}
    close() {
      this._ready = 3;
      (this._listeners["close"] ?? []).forEach((cb) => cb({ code: 1000 }));
    }
  }
  StubWS.OPEN = 1;
  StubWS.CONNECTING = 0;
  StubWS.CLOSED = 3;
  window.WebSocket = StubWS;
};

function waitForHttp(url, timeoutMs) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const r = await fetch(url);
        if (r.ok || r.status === 200) return resolve(true);
      } catch (_e) {
        /* not up yet */
      }
      if (Date.now() - start > timeoutMs) return reject(new Error(`timeout waiting for ${url}`));
      setTimeout(tick, 400);
    };
    tick();
  });
}

async function captureDisabled(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  await page.addInitScript(STUB_WS);
  // Seed the anonymous-accepted flag so the pre-existing AuthGate passes
  // straight through to the app shell — this is exactly the live tailnet path.
  await page.addInitScript(() => {
    try {
      localStorage.setItem("grace2_anonymous_accepted", "true");
    } catch (_e) {
      /* noop */
    }
  });
  console.log(`[A/disabled] loading running dev server ${RUNNING_URL}`);
  await page.goto(`${RUNNING_URL}/app`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="grace2-app-shell"]', { timeout: 15000 });

  // The job-0253 guard surfaces must be ABSENT in disabled mode.
  for (const sel of [
    "grace2-auth-guard-signin",
    "grace2-auth-guard-signout",
    "grace2-auth-guard-pending",
  ]) {
    const n = await page.locator(`[data-testid="${sel}"]`).count();
    if (n !== 0) {
      console.error(`[FAIL] disabled mode rendered ${sel} (count=${n})`);
      process.exit(1);
    }
  }
  console.log("[VERIFY] disabled mode: app shell present; NO job-0253 guard chrome");
  await page.waitForTimeout(300);
  await page.screenshot({ path: `${OUT_DIR}/A_disabled_passthrough.png` });
  console.log("[A/disabled] A_disabled_passthrough.png saved");
  await ctx.close();
}

async function captureEnabled(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  await page.addInitScript(STUB_WS);
  console.log(`[B/enabled] loading ephemeral configured server ${ENABLED_URL}`);
  await page.goto(`${ENABLED_URL}/app`, { waitUntil: "domcontentloaded" });

  // Signed-out + configured → the job-0253 sign-in surface.
  await page.waitForSelector('[data-testid="grace2-auth-guard-signin"]', { timeout: 15000 });
  const shellCount = await page.locator('[data-testid="grace2-app-shell"]').count();
  if (shellCount !== 0) {
    console.error("[FAIL] enabled+signed-out rendered the app shell");
    process.exit(1);
  }
  const wordmark = await page.locator('[data-testid="grace2-auth-guard-wordmark"]').innerText();
  if (!/GRACE-2/.test(wordmark)) {
    console.error(`[FAIL] sign-in wordmark wrong: ${wordmark}`);
    process.exit(1);
  }
  await page.waitForSelector('[data-testid="grace2-auth-guard-google"]');
  await page.waitForSelector('[data-testid="grace2-auth-guard-privacy"]');
  // No anonymous CTA on the prod surface (Decision 6).
  const anonHits = await page.getByText(/anonymous|without saving/i).count();
  if (anonHits !== 0) {
    console.error(`[FAIL] prod sign-in surface offers an anonymous option (${anonHits})`);
    process.exit(1);
  }
  console.log("[VERIFY] enabled+signed-out: Google-only sign-in surface; shell hidden; no anon");
  await page.waitForTimeout(300);
  await page.screenshot({ path: `${OUT_DIR}/B_enabled_signin.png` });
  console.log("[B/enabled] B_enabled_signin.png saved");
  await ctx.close();
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });

  // Boot the ephemeral CONFIGURED dev server (dummy Firebase env → configured).
  console.log(`[setup] starting ephemeral configured vite on :${ENABLED_PORT}`);
  const child = spawn(
    "npx",
    ["vite", "--port", String(ENABLED_PORT), "--strictPort", "--host", "127.0.0.1"],
    {
      cwd: "/home/nate/Documents/GRACE-2/web",
      env: {
        ...process.env,
        VITE_FIREBASE_PROJECT_ID: "grace2-job0253-dummy",
        VITE_FIREBASE_API_KEY: "AIza-job0253-dummy-key",
        VITE_FIREBASE_AUTH_DOMAIN: "grace2-job0253-dummy.firebaseapp.com",
        VITE_FIREBASE_APP_ID: "1:000:web:dummy",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout.on("data", (d) => process.stdout.write(`[vite] ${d}`));
  child.stderr.on("data", (d) => process.stderr.write(`[vite!] ${d}`));

  const browser = await chromium.launch({ headless: true });
  try {
    await waitForHttp(ENABLED_URL, 30000);
    await captureDisabled(browser);
    await captureEnabled(browser);
    console.log("[OK] job-0253 AuthGuard live-verify passed (disabled + enabled)");
  } finally {
    await browser.close();
    try {
      child.kill("SIGTERM");
    } catch (_e) {
      /* noop */
    }
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
