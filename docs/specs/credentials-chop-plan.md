# Credentials-chop plan (FOR NATE REVIEW)

Mission: find the design that makes the custom credential-handler machinery
redundant. Accepted direction: QgsAuthManager = credential home, plugin
brokers secrets over the existing WS seam at connect, env vars stay the
headless/dev fallback (cannot die). Plan only -- no code changed.

## 1. Inventory (server/src/trid3nt_server/credentials/)

| module | LOC | role | consumers | verdict |
|---|---|---|---|---|
| auth_handshake.py | 574 | WS connect handshake -- resolves EVERY connection to a User (local-single-user / sticky-anonymous) + endpoint advertisement. Session identity, NOT API-key creds. | server.py, persistence.py, tool_catalog_http.py, cases/, 5 test files | KEEP -- out of scope, misfiled (identity, not secrets). |
| credential_registry.py | 513 | Data-only provider catalog (5 providers) + tool->provider map + `is_credential_error`/`is_credential_shaped_error` detectors + generic-name fallback | server.py, gates/actionability.py, gates/cards/credential.py, adapters/adapter.py | KEEP (~500/513) -- the need-catalog is backend-agnostic; a connect-time broker reads it too. `derive_generic_credential_name` (~70 LOC) is a UX call, flagged not chopped. |
| secrets_handler.py | 577 | File-vault CRUD (write/read/revoke/list, path-safety) + legacy-scheme rejection + 3 WS-envelope handlers | persistence.py, server.py | DELETE-when-QgsAuthManager-is-home (~490/577). This IS the redundant machinery. Keeps only a typed missing-credential error family (~90 LOC). |
| __init__.py | 1 | empty | -- | KEEP |

**Directly-coupled consumers, same redundancy:** persistence.py secrets CRUD
(L1612-1764, ~155 LOC) DELETE; server.py `_PENDING_CREDENTIALS` registry
(L1553-1603, ~50) RESHAPE (~40 kept); server.py `_emit_secrets_list`/
`_handle_secret_add`/`_handle_secret_revoke` + dispatch (L11700-11865,
12436-12456, ~160) DELETE; server.py `_resolve_active_secret_ref`/
`_inject_secret_ref`/`_maybe_handle_credential_error`/
`_emit_credential_request_and_wait` (L8637-8905, ~320) RESHAPE to ~135;
gates/cards/credential.py (57) KEEP unchanged; gates/actionability.py +
adapters/adapter.py (~35) KEEP, reuse the detector.

## 2. Live credentials (key names only)

Registered (5): FIRMS (`TRID3NT_FIRMS_MAP_KEY`), eBird
(`TRID3NT_EBIRD_API_KEY`), Copernicus CDS -- shared ERA5+GTSM
(`TRID3NT_COPERNICUS_CDS_API_KEY`), Movebank
(`TRID3NT_MOVEBANK_USER`/`_PASSWORD`), IUCN Red List
(`TRID3NT_IUCN_RED_LIST_API_KEY`). Keyed but unregistered (env-only + generic
card): AirNow, OpenAQ, USACE NID/dams. `.env.local` here carries only infra
vars, no provider keys; `server/.env` carries one FIRMS key under a stale
`GRACE2_...` name that would not resolve (live resolver reads
`TRID3NT_FIRMS_MAP_KEY`). Every bespoke fetcher hand-rolls the same 3-step
priority (kwarg -> `secret_ref`/vault -> env) independently -- that pattern
lives in the fetchers, not the credentials/ package.

## 3. Dead-era findings

- `AWS_SSM_VAULT_SCHEME`/`GCP_SM_VAULT_SCHEME`/`_LEGACY_LOCAL_FILE_SCHEME`:
  recognized only to reject unresolvable refs; no writer produces them, no
  live secret uses them. Confirmed cloud-era residue -- dies with the vault.
- source.yaml `auth:` block (27 fetchers declare it): NOT wired to any
  resolver code (grepped the router, zero consumers). Only 1 fetcher
  declares `mode: api_key_env` and it's keyless-fallback, not in
  `TOOL_PROVIDER`. The mission brief's "AuthMode already does" declaration
  assumption only holds for this unused surface -- the real declaration
  point in production is `TOOL_PROVIDER`, keep that, not `auth:`.
- `_handle_secret_add`/`_handle_secret_revoke` docstrings still say
  "GCP Secret Manager" -- stale from the pre-file-vault era.

## 4. Runtime-request flow verdict

Yes -- a real mid-turn ask exists: a credential-shaped dispatch failure
pauses the tool, emits `credential-request` (real per-provider card, or a
NAME-ONLY generic card, never a fabricated URL), blocks on a session-scoped
`Future` keyed by `request_id`, resumes on `credential-provided` and retries
once. One-prompt-per-tool-per-turn guarded. This is a genuine product
feature and its UX SURVIVES, reshaped: server raises a typed
missing-credential error -> plugin prompts -> stores in QgsAuthManager ->
pushes over the seam -> retry. KEPT: the pause/emit/wait/retry skeleton,
the one-prompt guard, gates/cards/credential.py. DELETED: everything
between "user replied" and "retry" that touches the file vault or
Persistence (DB query, file write, `secrets-list` refresh) -- replaced by a
session-cache write/read.

## 5. End-state design

(a) Declare: `TOOL_PROVIDER` stays the closed map (unchanged), the broker's
lookup key. (b) Resolver: ONE new module -- in-memory session cache
(`dict[session_id, dict[provider_id, str]]`) from plugin push -> env
fallback. Est. ~60-80 LOC, replaces `_resolve_active_secret_ref` +
`get_secret_value` + the file-vault path. (c) WS seam: reuse existing
envelopes -- `secret-add` for on-demand retry (stop writing to a file,
write to the session cache), `credential-request`/`-provided` unchanged.
Connect-time bulk push is OPEN: N `secret-add` calls (no contract change,
recommended for wave 1) vs. a new bulk envelope. (d) QGIS side: authcfg
entries keyed by the same `provider_id`s + connect-time broker (plugin's
own in-process `QgsAuthManager`) + the existing credential card reshaped to
write-then-push instead of relay-raw-string.

**Option B -- headless auth broker (NATE addition).** For headless clients
with QGIS installed but no live plugin GUI session (canaries/drivers/
suite): a ~50-line subprocess under SYSTEM QGIS python (never imported into
the daemon -- Qt+asyncio mixing, dep weight, server-runs-without-QGIS all
forbid that) reads authcfg headlessly and hands values to the resolver's
cache. Verified read-only on this box: `python3-qgis` 3.40.6 installed,
`import qgis.core` succeeds from system python
(`/usr/lib/python3/dist-packages/qgis`); headless `QgsApplication([],
False)` under `QT_QPA_PLATFORM=offscreen` inits, `authManager()` reachable
(`isDisabled=False`). Profile auth db:
`~/.local/share/QGIS/QGIS3/profiles/default/qgis-auth.db` (real SQLite,
`auth_configs`/`auth_pass`/`auth_settings` tables) but on this box
`masterPasswordIsSet=False`, `masterPasswordHashInDatabase=False`, 0
configIds -- never provisioned here. `strings` on `libqgis_core.so.3.40.6`
confirms `QGIS_AUTH_PASSWORD_FILE`/`QGIS_AUTH_DB_DIR_PATH` are real linked
env hooks (the documented QGIS Server headless-unlock mechanism), not
fabricated. Honest trade: needs the master password established first
(one-time GUI step or scripted `qgis_process` init) AND a plaintext
password file on disk for the broker to read -- same secret-custody problem
as today's vault, collapsed from N files to one gating file. Ranking:
real single-home win for headless-with-QGIS boxes, but an EXTRA moving part
(subprocess boot, second Qt init, password-file provisioning) vs. plain
env-fallback which has zero extra parts and is already the mandatory floor.
Recommend: wave 1 = env-fallback only; defer Option B to a demand-gated v2
(a canary that needs a real key headlessly with no env set -- rare today).

## 6. Chop delta (pre-implementation estimates)

| surface | today | end-state | delta |
|---|---|---|---|
| credentials/ package (cred-scope, excl. auth_handshake) | 1091 | ~660 | -430 (-39%) |
| server.py (secrets/credential-pipeline LOC) | ~530 | ~180 | -350 (-66%) |
| persistence.py (secrets collection CRUD) | ~155 | 0 | -155 (-100%) |
| **total (server-side, excl. tests/plugin)** | **~1776** | **~840** | **~-935 (-53%)** |

Not sized above: tests (`test_secrets_handler.py` 533 LOC nearly fully
vault-specific; `test_credential_pipeline.py` 947 LOC partially kept) and
the plugin side (`cards.py`, `gate.py`, client relay -- reshaped).

## 7. Execution plan (one scoped wave)

1. Server: add `credentials/resolver.py` (session cache + env fallback +
   typed `MissingCredentialError`); gut `secrets_handler.py` to the typed
   errors only; delete Persistence secrets CRUD + the add/revoke/list
   handlers + dispatch cases; slim the mid-turn ask to a cache write.
2. Plugin: authcfg provisioning UI (one entry per registry provider),
   connect-time push over `TOOL_PROVIDER`'s provider set, reshape the
   credential card to write-then-push.
3. Headless plugin tests: `test_credential.py` (220 LOC) + `stub_server.py`
   swap from vault-file stub to session-cache stub.
4. NATE reload acceptance: live WS drive vs. a real daemon + authcfg-
   provisioned profile -- keyed fetch resolves with zero env set; a
   genuinely missing key still produces the card and a successful retry.

## 8. Risks

- Localhost-transit (accepted, restated): key still rides WS plaintext
  between plugin and daemon, both localhost-bound -- unchanged by this
  chop (today's vault write already receives the same plaintext).
- Master-password UX at connect: no master password yet -> `QgsAuthManager`
  calls fail; connect-time push must skip gracefully and fall to env, not
  block connect.
- Headless parity: env fallback must keep working for CI/canaries/drivers
  post-chop (hard rule) -- Option B is additive, never a replacement.
- Migration: `.env.local`/`server/.env` users keep working unchanged, env
  never removed. (`server/.env`'s stale `GRACE2_FIRMS_MAP_KEY` name is a
  pre-existing unrelated issue.)

## 9. Open questions

- Bulk connect-time push: N `secret-add` calls vs. a new bulk envelope.
- Register AirNow/OpenAQ/USACE-NID into `TOOL_PROVIDER` this wave (real
  cards) or leave them on the generic fallback?
- Keep `derive_generic_credential_name`/`generic_provider_for_tool` once
  every keyed fetcher is registered, or is the generic path dead weight?
- Option B timing: wave 1 or demand-gated v2 (recommend v2).
