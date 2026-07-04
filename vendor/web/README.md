# web/ — React + MapLibre web client

**Owner:** `web` specialist.

The browser application (SRS v0.3 Decision A, FR-WC-*). A React single-page app
with a MapLibre GL JS map, chat panel, layer panel, time scrubber, identify
popover, pipeline strip + cancel UI, and the spatial-input / disambiguation
pick-modes. It talks to the agent service over the Appendix-A WebSocket protocol
and renders Tier B map data exclusively through QGIS Server (WMS/WMTS/WFS) or
agent-served GeoJSON — never by reading GCS directly (Invariant 5).

Tier A basemap is a swappable public provider (OSM direct in v0.1; documented
MapTiler / Protomaps swap path — FR-DT-1, FR-DT-5).

Empty scaffold until `job-0016` lands the CONUS map + chat round-trip stub.

## Playwright (job-0027 — closes job-0016 OQ-W-3)

`@playwright/test` is a `devDependency` here for two purposes:

1. **AFK iteration loop** (`feedback_playwright_afk_iteration_loop.md`). The
   orchestrator runs `make screenshot` or `make ui-tour` from the repo root,
   then ships the resulting PNGs to the user's phone with
   `SendUserFile(status='proactive')`. The user reviews on the phone,
   replies with guidance, and the loop repeats.
2. **M3 acceptance suite** (`tests/m3/` — job-0028). Playwright drives a
   headless browser against the deployed QGIS Server WMS substrate, hits
   `tests/m3/playwright/` test files, and emits canonical reference captures
   to `tests/m3/artifacts/` (visual baselines committed; per-run captures
   under `tests/m3/artifacts/screenshots-*/` are gitignored).

### Install browsers on a fresh dev box

```
make playwright-install   # downloads Chromium + Firefox to ~/.cache/ms-playwright/
```

This closes **OQ-W-3** from job-0016: a fresh Debian box no longer needs
`apt install chromium` or `npx playwright install` typed by hand.

### One-shot capture

```
make run-web                              # in another terminal
make screenshot SCREENSHOT_ARGS='--state=initial --browser=chromium \
                                 --out=/tmp/grace2-shots/initial.png'
```

The CLI flags (`--url`, `--route`, `--state`, `--browser`, `--wait`,
`--viewport`, `--full-page`) live in `tools/screenshot.mjs`. Output PNG is
1440x900 by default.

### Full UI tour (six states x two browsers = twelve PNGs)

```
make ui-tour              # outputs /tmp/grace2-shots/<state>-<browser>.png
```

States: `initial`, `after-message`, `layer-panel-open`, `pipeline-running`,
`cancelled`, `disconnected`. States whose driving selectors (LayerPanel
toggle, PipelineStrip steps, cancel button) land later in M3 fall back
gracefully to the initial frame — by design, so the tooling shipped in
job-0027 is usable immediately.

### SendUserFile loop pattern

The orchestrator's pattern (per `feedback_playwright_afk_iteration_loop.md`):

```
# 1. orchestrator edits a React component
# 2. orchestrator runs `make screenshot ...` or `make ui-tour`
# 3. orchestrator calls SendUserFile(files=[...], status='proactive', caption='<route/state notes>')
# 4. user sees the screenshot on phone, replies with guidance
# 5. orchestrator edits, repeats
```

`status='proactive'` routes to the user's phone push, not just the
current desktop session. One-line captions per shot (route + state +
notable elements) so the user can navigate phone-side without zooming.

## Firebase Auth setup (job-0123, sprint-12-mega Wave 2)

The client integrates Firebase Authentication per SRS Appendix H (Decision P).
Authenticated mode (Google sign-in) requires a Firebase project; anonymous
mode works without any provisioning.

### Anonymous-only (no Firebase project)

The web client boots and runs against a local agent with no Firebase config.
The AuthPanel shows "Sign in with Google" (disabled, tooltip explains why)
and "Continue as anonymous" (functional). The `ws.ts` connect handler skips
the `auth-token` envelope; the agent's anonymous fallback handles the session.

### Authenticated mode (Firebase project provisioned)

1. Create a Firebase project at https://console.firebase.google.com/.
2. Enable Email/Password, Google, and Anonymous sign-in providers under
   Authentication → Sign-in method.
3. Add a web app (`</> Web`) to the project and copy the config.
4. Copy `.env.example` to `.env.local` and fill in the five `VITE_FIREBASE_*`
   vars from the config snippet.
5. Restart the dev server. The "Sign in with Google" button is now active.

The Firebase API key is **safe to ship to clients** — Firebase enforces
security via Auth rules + Identity Platform IAM, not by hiding the key.
See Firebase docs § "Is it safe to expose Firebase API key to the public?".

### Wire protocol (Appendix H.5)

On WebSocket connect, `ws.ts` fetches the current Firebase ID token via
`getIdToken()` and emits an `auth-token` envelope:

```json
{
  "type": "auth-token",
  "id": "<ulid>",
  "ts": "2026-06-08T...Z",
  "session_id": "<session-ulid>",
  "payload": {
    "id_token": "<firebase JWT>",
    "provider": "firebase"
  }
}
```

If no token is available (signed out, Firebase disabled, fetch failed) the
envelope is skipped — the agent's anonymous fallback handles the session.
