# 0062 - credentials collapse onto QgsAuthManager + resolver session cache

Context: the custom credential machinery (a local file vault with per-user
per-provider 0600 files, Persistence secrets CRUD, and server.py vault
add/revoke/list handlers, ~935 LOC) duplicated a home QGIS already provides.
`credentials-chop-plan.md` found the redundancy: QgsAuthManager is the natural
credential HOME; the plugin can broker key VALUES over the existing `secret-add`
WS seam; env vars stay the headless/dev floor.

## Decision

1. **New `credentials/resolver.py` (~140 LOC).** The single runtime credential
   source. Resolution order: in-memory SESSION CACHE (`session_id ->
   provider_id -> value`, populated by the plugin push) then ENV fallback (the
   same env var each fetcher's `_resolve_*_key` reads). It returns a raw `str`,
   which the server injects as `params["secret_ref"]`; every keyed fetcher's
   `_materialize_secret` accepts a `str` secret_ref verbatim, so no vault and no
   Persistence read sit on the path. `MovebankUSER/_PASSWORD` is a composite
   handled by the fetcher's own env fallback, so the resolver's env map omits it
   (session-cache path still serves a pushed Movebank blob).

2. **Deleted the file vault.** `secrets_handler.py` shrinks from 577 to ~45 LOC
   (the typed `SecretError` / `SecretNotFoundError` / `SecretRevokedError`
   family only). Persistence secrets CRUD (`list_secrets_refs` /
   `upsert_secret_ref` / `revoke_secret` / `get_secret_value`, ~155 LOC, plus
   `SECRETS_COLLECTION`, the `SecretRecord` import) deleted. server.py vault
   surface deleted: `_emit_secrets_list`, `_handle_secret_revoke`,
   `_resolve_active_secret_ref`, `_bind_secret_seams` / `_SECRET_SEAM_TOOL_MODULES`
   and the `secret-revoke` / `secrets-list-request` dispatch cases. Legacy vault
   schemes (aws-ssm/gcp-sm/local-file) and GCP Secret Manager docstrings die
   with the vault.

3. **Reshaped the mid-turn missing-credential flow (~320 -> ~135 LOC).** The
   product feature SURVIVES: a keyed-tool credential error still pauses the
   tool, emits a `credential-request` card, blocks on a session-scoped future,
   and retries once on `credential-provided`. What changed is the middle: the
   plugin prompts, stores to QgsAuthManager, and pushes the value over the
   EXISTING `secret-add` seam; the reshaped `_handle_secret_add` writes it to
   the resolver session cache (no file, no Persistence); the retry re-resolves
   from the session cache. `credential_registry.py` (the need-catalog + shape
   detectors + `_build_credential_request_payload`) is UNCHANGED. `secret-add`
   is retained as the push seam.

4. **Plugin QgsAuthManager broker (`net/auth_broker.py`).** `QgsAuthManagerStore`
   reads/writes trid3nt-cred entries keyed by provider_id, degrading to a no-op
   (returns `{}` / `False`, never raises) when no master password is set or QGIS
   is absent -- connect-time push must never block the connect. `AuthBroker`
   does connect-time PUSH (one `secret-add` per stored key, no new bulk contract)
   and prompt STORE. `AgentClient` gained `push_secret` (secret-add only) and a
   `credential_broker` hook; `submit_credential` also stores the answered key
   back for the next connect.

5. **source.yaml `auth:` blocks LEFT INERT.** The `auth.user_agent` sub-field IS
   read by the `_router` hooks/executors (User-Agent header), so the block is
   not dead; only its api-key sub-fields are unwired to the resolver
   (`TOOL_PROVIDER` is the real declaration surface). Deleting the field is a
   separate router-scoped change, not this wave.

## Item 0 (separate change): /api/case-layers route deleted

`build_case_layers_manifest` + the `POST /api/case-layers` route (handler,
`_case_layers_route_enabled`, `_case_layers_fn`, `_handle_case_layers_post`,
`_CaseLayersBadRequest`) are plugin-unreached since decision A (the WS case-open
replay already restores layers; `case_export.py` records the plugin client was
removed). Deleted server + tests; `_layers_from_case` stays (still used by the
REMOTE-mode `hydrate_case_layers`).

## Consequences

- Headless parity preserved: env fallback keeps every keyed fetcher working with
  no plugin session (proven by the offline suite's keyed-fetcher tests).
- Localhost-transit unchanged: the key still rides WS plaintext between
  localhost-bound plugin and daemon (same as the old vault write).
- Master-password UX deferred: with no master password the broker's push is a
  graceful no-op and env covers it; Option B (headless authcfg reader) is a
  demand-gated v2, not built here.
- Wire isolation intact: the raw key rides only `secret-add`; the session cache
  holds it in process memory for the session's lifetime, never persisted,
  logged, or echoed.
