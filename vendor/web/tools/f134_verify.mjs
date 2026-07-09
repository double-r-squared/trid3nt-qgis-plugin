// F1/F3/F4 live verification: drive the LOCAL web app like NATE did.
// Sends "show me landcover over washington", samples the UI DURING the turn
// (mid-turn cards/thinking = F1), then checks layer loading state (F3) and
// visible raster (F4). Screenshots at T+15s, T+60s, end.
import { chromium } from "playwright";
const OUT = "/tmp/claude-1000/-home-nate-Documents-GRACE-2/fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
page.on("console", (m) => { if (m.type() === "error") console.log("[console.error]", m.text().slice(0, 160)); });
await page.goto("http://127.0.0.1:5173/app", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(6000);
// F5 check: should be straight in the app, no sign-in gate
const gate = await page.locator("text=/sign in/i").count();
console.log("auth-gate visible:", gate > 0);
// find the chat input
const input = page.locator("textarea, input[placeholder*='Ask'], input[placeholder*='ask']").first();
await input.waitFor({ timeout: 20000 });
await input.fill("show me landcover over washington state");
await input.press("Enter");
console.log("prompt sent", new Date().toISOString());
// Sample mid-turn state
for (const t of [15, 40, 70]) {
  await page.waitForTimeout(t === 15 ? 15000 : t === 40 ? 25000 : 30000);
  const thinking = await page.locator('[data-testid*="thinking"]').count() + await page.getByText(/thinking/i).count();
  const cards = await page.locator('[data-testid*="card"], [class*="pipeline"], [class*="ToolCard"]').count();
  const loading = await page.locator("text=/loading/i").count();
  for (const label of ["Proceed anyway", "Proceed", "Agree size"]) {
    const btn = page.getByRole("button", { name: label }).first();
    if (await btn.count() && await btn.isEnabled().catch(() => false)) {
      await btn.click().catch(() => {});
      console.log("clicked gate:", label);
      break;
    }
  }
  console.log(`T+${t}s thinking-els=${thinking} cards=${cards} loading-els=${loading}`);
  await page.screenshot({ path: `${OUT}/f1-t${t}.png` });
}
// Wait for turn to settle then final state
await page.waitForTimeout(90000);
const loadingFinal = await page.locator("text=/loading/i").count();
const layerRows = await page.locator('[data-testid*="layer"]').count();
console.log(`FINAL loading-els=${loadingFinal} layer-rows=${layerRows}`);
await page.screenshot({ path: `${OUT}/f1-final.png`, fullPage: false });
await browser.close();
console.log("done");
