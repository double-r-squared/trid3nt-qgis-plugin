// Verify the FULL mandatory-auth sign-in round-trip (no Bedrock): wall -> Cognito
// Hosted UI -> email/password -> redirect back with ?code= -> token exchange ->
// agent verifies the ID token over WS -> app loads signed-in. Creds via env
// (GRACE2_DEMO_EMAIL / GRACE2_DEMO_PASSWORD) so nothing is hardcoded in-repo.
import { chromium } from "playwright";

const SITE = "https://d125yfbyjrpbre.cloudfront.net/app";
const CF = "d125yfbyjrpbre.cloudfront.net";
const EMAIL = process.env.GRACE2_DEMO_EMAIL;
const PW = process.env.GRACE2_DEMO_PASSWORD;
const OUT = "/tmp/aws_signin";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const errors = [];
let wsToCF = false;
page.on("pageerror", (e) => errors.push(String(e)));
page.on("websocket", (ws) => { if (ws.url().includes(CF)) wsToCF = true; });

await page.goto(SITE, { waitUntil: "networkidle", timeout: 60000 });
await page.waitForTimeout(2000);
const wallBefore = /Sign in to continue/i.test(await page.evaluate(() => document.body.innerText));
await page.screenshot({ path: `${OUT}_1_wall.png` });

console.log("[1] click Sign in -> Hosted UI");
await page.getByRole("button", { name: /Sign in|Sign up/i }).first().click().catch(() => {});
await page.waitForTimeout(5000);
const onHostedUI = /amazoncognito\.com/.test(page.url());
console.log(`[hostedUI] reached=${onHostedUI} url=${page.url().slice(0, 80)}`);
await page.screenshot({ path: `${OUT}_2_hostedui.png` });

console.log("[2] fill credentials");
// Classic Hosted UI (ManagedLoginVersion 1): name=username / name=password
const userField = page.locator('input[name="username"]:visible, input[type="email"]:visible').first();
const pwField = page.locator('input[name="password"]:visible, input[type="password"]:visible').first();
await userField.waitFor({ timeout: 12000 });
await userField.fill(EMAIL);
await pwField.fill(PW);
await page.screenshot({ path: `${OUT}_3_filled.png` });
await page.locator('input[name="signInSubmitButton"]:visible, input[type="submit"]:visible, button[type="submit"]:visible').first().click().catch(() => {});

console.log("[3] await redirect back to app + signed-in render");
for (let i = 0; i < 20; i++) {
  await page.waitForTimeout(2000);
  if (page.url().includes(CF) && !/amazoncognito/.test(page.url())) break;
}
await page.waitForTimeout(6000);
await page.screenshot({ path: `${OUT}_4_signedin.png` });
const body = await page.evaluate(() => document.body.innerText);
const backOnApp = page.url().includes(CF) && !/amazoncognito/.test(page.url());
const chatInput = await page.locator('[data-testid="chat-input"]').count();
const wallAfter = /Sign in to continue/i.test(body);

console.log(`[result] wallBefore=${wallBefore} backOnApp=${backOnApp} chatInput=${chatInput > 0} wallGone=${!wallAfter} wssToCF=${wsToCF} errors=${errors.length}`);
const pass = onHostedUI && backOnApp && chatInput > 0 && !wallAfter && errors.length === 0;
console.log(pass ? "[PASS] full mandatory-auth sign-in round-trip works: Hosted UI -> token -> agent-verified -> app loads"
               : "[REVIEW] see signals + screenshots");
await browser.close();
process.exit(pass ? 0 : 1);
