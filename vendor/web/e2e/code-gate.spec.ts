// GRACE-2 web — code-gate E2E (Playwright).
//
// WHY THIS EXISTS (the #310 class vitest CANNOT catch): vitest mounts <App/>
// PRE-authed via the __setAuthForTesting seam, so it never exercises the
// UNAUTHED -> code-entry -> AUTHED transition where the real App.tsx auth
// early-return runs. A hook landing below that early-return blanks the authed
// app with React #310, and only a real browser navigating the full transition
// surfaces it. This spec drives exactly that: load unauthed, see the code form,
// enter a code whose demo-token fetch is MOCKED to return a Cognito token set,
// and assert the app body renders (no blank root, no #310).
//
// RUNNER NOTE: playwright.config.ts currently sets testDir: "tests/m3/playwright"
// (the historical screenshot-suite location), so `npx playwright test` will not
// pick this file up as-is. To run it, point the runner at this dir, e.g.:
//   npx playwright test --config playwright.config.ts e2e/code-gate.spec.ts \
//     --project chromium
// after temporarily setting testDir (or pass the file path explicitly). The
// spec is intentionally self-contained — it builds a fake unsigned Cognito-shaped
// JWT and intercepts both the demo-token POST and the agent WS so the authed
// render is reached without any live backend. The default `baseURL`
// (PLAYWRIGHT_BASE_URL ?? http://localhost:5173) must serve a build with the
// VITE_COGNITO_* + VITE_GRACE2_DEMO_TOKEN_URL env set so AuthGuard is in MODE 2.

import { test, expect } from "@playwright/test";

/** Build an UNSIGNED JWT (header.payload.) — auth.ts decodes claims only; the
 *  agent verifies the real signature against JWKS (irrelevant in this client
 *  E2E). Cognito-shaped: sub / email / exp. */
function makeJwt(claims: Record<string, unknown>): string {
  const b64url = (o: unknown) =>
    Buffer.from(JSON.stringify(o))
      .toString("base64")
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  return `${b64url({ alg: "none", typ: "JWT" })}.${b64url(claims)}.`;
}

const FUTURE_EXP = Math.floor(Date.now() / 1000) + 3600;
const FAKE_ID_TOKEN = makeJwt({
  sub: "e2e-demo-judge",
  email: "demo@grace2-dev.test",
  name: "Demo Judge",
  exp: FUTURE_EXP,
});

test.describe("code-gate: unauthed -> code entry -> authed render", () => {
  test("entering a valid access code mounts the app (guards React #310)", async ({
    page,
  }) => {
    // Mock the demo-token endpoint: any POST to a *…/demo-token URL returns a
    // Cognito-shaped token set. This stands in for the live Lambda so the spec
    // needs no backend.
    await page.route(/\/demo-token(\?.*)?$/, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id_token: FAKE_ID_TOKEN,
          access_token: "e2e-access",
          refresh_token: "e2e-refresh",
        }),
      });
    });

    // Neutralize the agent WebSocket so reaching the authed app does not hang on
    // a live box (the render-transition is what we assert, not WS traffic).
    await page.addInitScript(() => {
      class NoopWS {
        public onopen: (() => void) | null = null;
        public onclose: (() => void) | null = null;
        public onerror: (() => void) | null = null;
        public onmessage: (() => void) | null = null;
        public readyState = 0;
        constructor() {
          /* never opens; the app tolerates a connecting socket */
        }
        send() {}
        close() {}
        addEventListener() {}
        removeEventListener() {}
      }
      // @ts-expect-error override for the test
      window.WebSocket = NoopWS;
    });

    await page.goto("/app");

    // UNAUTHED: the code-entry surface is shown (MODE 2 default), NOT the app.
    const codeInput = page.getByTestId("grace2-code-input");
    await expect(codeInput).toBeVisible();
    await expect(page.getByTestId("grace2-auth-guard-signin")).toBeVisible();

    // Enter the code and submit.
    await codeInput.fill("THE-ACCESS-CODE");
    await page.getByTestId("grace2-code-submit").click();

    // AUTHED: the sign-in surface disappears and the app body renders. The
    // critical assertion is that the authed app is NOT a blank root (React #310
    // would leave nothing mounted). We assert the gate is gone AND the document
    // body has real content.
    await expect(page.getByTestId("grace2-auth-guard-signin")).toHaveCount(0, {
      timeout: 15_000,
    });
    const bodyText = await page.evaluate(
      () => document.body.innerText.trim().length,
    );
    expect(bodyText).toBeGreaterThan(0);
    // The React root mounted children (any app chrome node present).
    const rootChildren = await page.evaluate(() => {
      const root = document.getElementById("root");
      return root ? root.childElementCount : 0;
    });
    expect(rootChildren).toBeGreaterThan(0);
  });

  test("?admin renders the Hosted-UI sign-in button instead of the code form", async ({
    page,
  }) => {
    await page.goto("/app?admin=1");
    await expect(page.getByTestId("grace2-auth-guard-signin-btn")).toBeVisible();
    await expect(page.getByTestId("grace2-code-input")).toHaveCount(0);
  });
});
