#!/usr/bin/env node
// GRACE-2 — job-0151 evidence screenshot.
// Verifies the SecretsPopup is a single card surface (no nested card).
// Captures:
//   01_secrets_popup_flat.png       — popup open, empty state, flat layout.
//   02_secrets_popup_with_record.png — popup with one injected eBird record.

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";

const OUT_DIR =
  process.argv[2] ??
  "/home/nate/Documents/GRACE-2/reports/inflight/job-0151-web-20260608/evidence";
const BASE_URL = process.env.GRACE2_DEV_URL ?? "http://localhost:5177";

async function main() {
  await mkdir(OUT_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();

  // Mock WebSocket + pre-accept anonymous auth so the AuthGate doesn't block.
  await page.addInitScript(() => {
    const captured = [];
    class CapturingWS {
      constructor(_url) {
        this._listeners = {};
        this._ready = 1;
        setTimeout(() => {
          (this._listeners["open"] ?? []).forEach((cb) => cb({}));
        }, 0);
      }
      get readyState() { return this._ready; }
      addEventListener(type, cb) {
        (this._listeners[type] ??= []).push(cb);
      }
      send(data) { captured.push(data); }
      close() { this._ready = 3; }
    }
    CapturingWS.OPEN = 1;
    CapturingWS.CONNECTING = 0;
    CapturingWS.CLOSED = 3;
    window.WebSocket = CapturingWS;
    window.__grace2CapturedFrames = captured;
    // Bypass AuthGate (same pattern as screenshot_job0143_layout.mjs).
    try { localStorage.setItem("grace2_anonymous_accepted", "true"); } catch {}
  });

  page.on("pageerror", (err) =>
    console.warn(`[screenshot] pageerror: ${err.message}`),
  );
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      console.warn(`[screenshot] console.error: ${msg.text()}`);
    }
  });

  console.log(`[screenshot] loading ${BASE_URL}`);
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });

  await page.waitForSelector('[data-testid="grace2-app-shell"]', {
    timeout: 15000,
  });
  console.log("[screenshot] app shell mounted");

  // Open the SecretsPopup via the bottom-row Secrets button.
  await page.waitForSelector('[data-testid="grace2-bottom-row-secrets"]', {
    timeout: 5000,
  });
  await page.click('[data-testid="grace2-bottom-row-secrets"]');
  await page.waitForSelector('[data-testid="grace2-secrets-popup"]', {
    timeout: 5000,
  });
  await page.waitForSelector('[data-testid="grace2-secrets-panel"]', {
    timeout: 5000,
  });
  console.log("[screenshot] SecretsPopup open");

  // (1) Empty-state flat layout screenshot.
  await page.screenshot({
    path: `${OUT_DIR}/01_secrets_popup_flat.png`,
    fullPage: false,
  });
  console.log("[screenshot] 01_secrets_popup_flat.png saved");

  // Verify single-card depth: grace2-secrets-panel must NOT contain a -card child.
  const nestedCard = await page.$('[data-testid="grace2-secrets-panel"] [data-testid$="-card"]');
  if (nestedCard) {
    console.error("[FAIL] nested card element found inside grace2-secrets-panel");
    process.exit(1);
  }
  console.log("[VERIFY] single card depth confirmed — no nested -card inside panel");

  // Verify header reads "API Keys".
  const headerText = await page.$eval(
    '[data-testid="grace2-secrets-popup-card"] h2',
    (el) => el.textContent,
  );
  if (headerText !== "API Keys") {
    console.error(`[FAIL] expected h2="API Keys", got "${headerText}"`);
    process.exit(1);
  }
  console.log(`[VERIFY] popup h2 text = "${headerText}"`);

  // Inject a fake eBird record to show the flat list layout.
  await page.evaluate(() => {
    window.__grace2InjectSecretsList?.({
      envelope_type: "secrets-list",
      secrets: [
        {
          schema_version: "v1",
          secret_id: "01ABCDEFGHJKMNPQRSTVWX0001",
          provider: "ebird",
          case_id: null,
          vault_ref: "gcp-sm://projects/grace2-dev/secrets/ebird-1/versions/latest",
          label: "personal-eBird-key",
          added_at: "2026-06-08T12:00:00.000Z",
          last_used_at: "2026-06-08T13:00:00.000Z",
          is_active: true,
        },
      ],
    });
  });

  // Give React a tick to re-render if injection worked.
  await page.waitForTimeout(300);

  // (2) With-record screenshot.
  await page.screenshot({
    path: `${OUT_DIR}/02_secrets_popup_with_record.png`,
    fullPage: false,
  });
  console.log("[screenshot] 02_secrets_popup_with_record.png saved");

  console.log("[OK] all job-0151 verifications passed");
  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
