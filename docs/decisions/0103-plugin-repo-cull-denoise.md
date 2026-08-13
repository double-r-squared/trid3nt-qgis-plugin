# ADR 0103 -- Daemon-hosted plugin repository, remote-mode cull, chat de-noise

Status: accepted (2026-08-04, NATE live remote session)
Supersedes: the `/plugins/` on-demand plugin repository landed in 7739cce
(2026-07-28).

## Context

NATE runs QGIS on macOS over the tailnet, pointed at a STOPGAP static plugin
repository (a `python -m http.server` on this box serving a hardcoded-IP
`plugins.xml` at `http://100.92.163.46:8767/`). The daemon needs to host the
plugin repository itself, on a stable path, with a host-correct `download_url`
so any tailnet client's "Add repository" URL round-trips to a reachable zip.

Three unrelated NATE decisions ride this wave.

## Decision

### 1. Daemon-hosted plugin repository at `/plugin-repo/` (metadata-driven)

`scripts/package_plugin.sh` (wired into `make agent` via a `plugin-repo`
prerequisite) builds `qgis-plugin/trid3nt` into `run/plugin-repo/`
(gitignored, server-owned; override `TRID3NT_PLUGIN_REPO_DIR`): a versioned
zip `trid3nt-<version>.zip`, a regenerated `plugins.xml`, and a
`manifest.json`. The logic lives in `trid3nt_server.plugin_repo`
(`package_plugin_repo`) so the serve routes reuse the same code.

- **Version is metadata.txt-driven, never auto-bumped.** `<version>` in
  `plugins.xml` and the zip's `metadata.txt` are the SAME `version=` line.
  `package_plugin_repo` hashes the packaged tree and WARNS (never fails, never
  auto-bumps) when the tree changed but the version did not -- a forgotten bump
  is caught at deploy time. (This is the deliberate reversal of 7739cce's
  `<version>+<git-describe>` auto-suffix, which bumped the version on every
  commit.)
- **Host derivation at serve time.** The packaged `plugins.xml` carries a
  `HOST_SENTINEL` in place of the `download_url` host; the daemon route
  `GET /plugin-repo/plugins.xml` substitutes the request's own `Host` header
  (`plugin_repo.render_plugins_xml`). A tailnet client's index URL therefore
  yields a zip URL on the SAME host it dialed -- the exact defect the
  hardcoded-IP stopgap had. `GET /plugin-repo/<zip>` serves the packaged bytes
  statically (path-traversal guarded).
- `/api/version` (daemon git sha + provider) is unrelated and unchanged.

Why replace `/plugins/` rather than add a second route: two competing plugin
repositories violate clean-as-you-go; the `/plugins/` route was never adopted
(NATE stayed on the stopgap), and its git-describe versioning contradicts this
decision. The old route + its on-demand `ensure_plugin_zip` build are removed.

### 2. Remote (cloud/Cognito) mode dies -- the tailnet IS the remote story

The plugin had a LOCAL/REMOTE mode switch (cloud endpoint + Cognito bearer
token). The VPN/tailnet is the only remote story now.

- Removed: `MODE_REMOTE`, `DEFAULT_REMOTE_URL`, `PluginSettings.remote_url`,
  the settings-dialog Mode combo + "Remote agent URL" row + mode-visibility
  toggle, and every `MODE_REMOTE` / `mode != MODE_LOCAL` branch in `dock.py`
  and `render/layers.py`. Docstrings across `ws_bridge.py` / `trid3nt_client.py`
  reworded from token-EXPIRY (Cognito) to token-REJECTION (a static shared
  token is accepted or rejected, never "expires").
- **Kept, re-scoped:** the optional shared token field. Its sole meaning is now
  the LOCAL tailnet shared token (OFF/empty by default; feeds the daemon's
  `TRID3NT_ACCESS_TOKEN` gate when enabled).
- **Settings migration:** `PluginSettings.mode` is a read-only property that
  always returns `MODE_LOCAL`. A config persisted with `mode=remote` by an
  older build loads without a crash and degrades to local; the stored key is
  otherwise inert.
- **Server side (characterized, not changed):** `credentials/auth_handshake.py`
  has NO Cognito verifier (the local build never had one). Its only token path
  is the shared `TRID3NT_ACCESS_TOKEN` gate (`verify_access_token`) which serves
  the tailnet token -- KEPT. `User.firebase_uid` is a dormant, provider-agnostic
  IdP-sub carrier retained so the file-backed user store needs no migration --
  out of scope, KEPT.

### 3. Chat de-noise

Removed two dock-side status notes (client render side; no server event was
involved, nothing else consumed them):

- `"Case '<title>' active"` on every case open (the "blank case is active"
  line).
- `"Zoomed to case area"` (three call sites in `_zoom_after_case_open`). The
  canvas STILL auto-focuses on case open; only the chat subtext is gone. The
  honest bbox-less fallback (`"Case has no stored map area ..."`) stays.

`"config applied"` (provider-config note) is KEPT.

### 4. Bridge signal-signature regression guard

New offline test `server/tests/test_ws_bridge_signal_signatures.py` parses
`ws_bridge.py` with `ast` (no Qt import) and asserts every forwarded
worker->bridge signal pair has identical argument-type lists, and that
`connected` is the 4-arg `(str, bool, str, str)`. This locks out the 650e575
class of bug (a narrower bridge signal silently dropping emitted args).

## Consequences

- The daemon route serves the repo only after `make agent` (or
  `scripts/package_plugin.sh`) has packaged it; a serve before packaging is an
  honest 503.
- Live verification of the new route awaits the orchestrator's daemon restart
  (NATE's session is live; the specialist does not restart). Verified offline:
  the package + serve helpers, host substitution, traversal guard, and the HTTP
  dispatch (`test_plugin_repo_http_route.py`, 27 tests).
- `case_export.ws_url_to_http_base` is orphaned by the remote-branch removal
  (registered in DELETION_LEDGER).
