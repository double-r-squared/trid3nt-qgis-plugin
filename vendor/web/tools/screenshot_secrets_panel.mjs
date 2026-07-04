#!/usr/bin/env node
// GRACE-2 — job-0125 evidence screenshot.
// Boots the dev server, opens the SecretsPanel via the toggle button,
// injects a mock secrets-list with 1 fake record via the
// window.__grace2InjectSecretsList dev seam, then captures:
//   1. The panel rendering the existing key + revoke button
//   2. The panel after submitting an "Add key" form so the captured
//      WS envelope shape can be verified

import { chromium } from "@playwright/test";
import { mkdir } from "fs/promises";
import { dirname } from "path";

const OUT_DIR =
  process.argv[2] ?? "reports/inflight/job-0125-web-20260608/evidence";
const BASE_URL = process.env.GRACE2_DEV_URL ?? "http://localhost:5174";

async function main() {
  await mkdir(OUT_DIR, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();

  // Intercept WebSocket messages so we can verify the secret-add envelope
  // shape after the user clicks "Add key". The mock WS is installed in
  // page-init to capture every outbound frame.
  await page.addInitScript(() => {
    const captured = [];
    const realWS = window.WebSocket;
    class CapturingWS {
      constructor(url, protocols) {
        this._url = url;
        this._listeners = {};
        this._ready = 1;
        // Don't actually connect — just track sends.
        setTimeout(() => {
          (this._listeners["open"] ?? []).forEach((cb) => cb({}));
        }, 0);
      }
      get readyState() { return this._ready; }
      addEventListener(type, cb) {
        (this._listeners[type] ??= []).push(cb);
      }
      send(data) {
        captured.push(data);
      }
      close() {
        this._ready = 3;
        (this._listeners["close"] ?? []).forEach((cb) => cb({}));
      }
    }
    CapturingWS.OPEN = 1;
    CapturingWS.CONNECTING = 0;
    CapturingWS.CLOSED = 3;
    window.WebSocket = CapturingWS;
    window.__grace2CapturedFrames = captured;
    window.__grace2RealWS = realWS;
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

  // Wait for the secrets toggle button to be ready, then click it open.
  await page.waitForSelector('[data-testid="grace2-secrets-toggle"]', {
    timeout: 5000,
  });
  await page.click('[data-testid="grace2-secrets-toggle"]');
  console.log("[screenshot] secrets toggle clicked");

  // Wait for the panel itself to mount.
  await page.waitForSelector('[data-testid="grace2-secrets-panel"]', {
    timeout: 5000,
  });

  // Empty state baseline — capture before injecting a record.
  await page.waitForSelector('[data-testid="grace2-secrets-empty-state"]', {
    timeout: 3000,
  });
  await page.screenshot({
    path: `${OUT_DIR}/secrets_panel_empty.png`,
    fullPage: false,
  });
  console.log("[screenshot] empty-state screenshot saved");

  // Inject a fake secrets-list via the dev seam.
  await page.evaluate(() => {
    window.__grace2InjectSecretsList({
      envelope_type: "secrets-list",
      secrets: [
        {
          schema_version: "v1",
          secret_id: "01ABCDEFGHJKMNPQRSTVWX0001",
          provider: "ebird",
          case_id: "01ABCDEFGHJKMNPQRSTVWX0050",
          vault_ref: "gcp-sm://projects/grace2-dev/secrets/ebird-1/versions/latest",
          label: "personal-eBird-key",
          added_at: "2026-06-08T12:00:00.000Z",
          last_used_at: "2026-06-08T13:00:00.000Z",
          is_active: true,
        },
      ],
    });
  });
  console.log("[screenshot] secrets-list injected");

  await page.waitForSelector(
    '[data-testid="grace2-secret-row-01ABCDEFGHJKMNPQRSTVWX0001"]',
    { timeout: 5000 },
  );
  await page.screenshot({
    path: `${OUT_DIR}/secrets_panel_with_record.png`,
    fullPage: false,
  });
  console.log("[screenshot] with-record screenshot saved");

  // Add a key via the form.
  await page.selectOption(
    '[data-testid="grace2-secret-provider"]',
    "movebank",
  );
  await page.fill(
    '[data-testid="grace2-secret-label"]',
    "movebank-academic",
  );
  await page.fill(
    '[data-testid="grace2-secret-key"]',
    "FAKE-MOVEBANK-KEY-XYZ",
  );
  await page.click('[data-testid="grace2-secret-submit"]');
  console.log("[screenshot] submit clicked");

  // Verify key field cleared (Decision F)
  const keyVal = await page.inputValue('[data-testid="grace2-secret-key"]');
  if (keyVal !== "") {
    console.error(
      `[FAIL] key field NOT cleared after submit (value: ${JSON.stringify(keyVal)})`,
    );
    process.exit(1);
  }
  console.log("[VERIFY] key field cleared after submit");

  await page.screenshot({
    path: `${OUT_DIR}/secrets_panel_after_submit.png`,
    fullPage: false,
  });

  // Verify captured WS frames contain a secret-add with the right shape.
  const frames = await page.evaluate(() => window.__grace2CapturedFrames);
  const parsed = frames
    .map((s) => {
      try {
        return JSON.parse(s);
      } catch {
        return null;
      }
    })
    .filter((x) => x !== null);

  const secretAdd = parsed.find((e) => e.type === "secret-add");
  if (!secretAdd) {
    console.error("[FAIL] no secret-add envelope captured");
    console.error(`captured types: ${parsed.map((e) => e.type).join(", ")}`);
    process.exit(1);
  }
  console.log(
    `[VERIFY] secret-add envelope captured: provider=${secretAdd.payload.provider}, label=${secretAdd.payload.label}, key_value_length=${(secretAdd.payload.key_value ?? "").length}`,
  );
  if (secretAdd.payload.provider !== "movebank") {
    console.error(
      `[FAIL] expected provider=movebank, got ${secretAdd.payload.provider}`,
    );
    process.exit(1);
  }
  if (secretAdd.payload.key_value !== "FAKE-MOVEBANK-KEY-XYZ") {
    console.error(
      `[FAIL] secret-add envelope did not carry expected key_value`,
    );
    process.exit(1);
  }

  console.log("[OK] all secrets-panel verifications passed");

  await browser.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
