# ADR 0266 - server-refactor wave 6: severed-consumer chop (case-view snapshot + coldview)

Status: LANDED (2026-08-15). Wave 6 of the server refactor (ADR 0261 wave-1
package skeleton; 0262 wave-2 cloud-seam chop; 0263 wave-3
interactions/styles/spatial; 0264 wave-4 reuse/dispatch/protocol; 0265 finale
state/turn split, package end-state at cb72bab6). Behavior-preserving for every
LIVE local path.
Date: 2026-08-15

## Context

The codebase was cloud-first and retrofitted local. `_core` + `persistence`
carry BEHAVIORAL residue: live code paths whose CONSUMERS no longer exist in the
local product (QGIS plugin + local daemon + MinIO + local docker solvers). There
is no browser viewer, no Lambda signer, no presigned-GET cold path, no sleeping
agent box, no multi-user.

NATE confirmed severed and ordered chopped: the **case-view snapshot machinery**
and the **coldview backfill**. Both existed to serve the retired
**view-without-agent** feature -- a browser cold view that fetched a presigned
`case-views/{id}.json` snapshot (and a thin `case-manifests/{id}.json` index)
from S3 while the agent box slept. The QGIS-only local product rebuilds a
reopened Case from the persisted store + WS replay (`_emit_case_open` ->
`get_session_state` -> `rehydrate_history_from_case` + emitter re-inline).
Snapshots were NEVER the reopen path.

### Consumer-absence evidence (PART 1 premise verified)

Grep of `server/` + `qgis-plugin/` + `scripts/` for readers of the
`case-views/` and `case-manifests/` objects: **ZERO consumers**.

- No `get_object` / `s3_get` / download of a `case_view_snapshot_key` /
  `case_manifest_key` object anywhere.
- The only non-writer references to the prefix strings were EXCLUSION filters in
  8 `scripts/run_*_direct.py` (`if key.startswith("case-manifests/") or
  key.startswith("case-views/"): continue`) -- they skip those prefixes when
  diffing run prefixes; they never READ the objects. Harmless once writes stop;
  left in place (test-driver residue, not product).
- The two emitter properties feeding the snapshot (`inline_geojson_by_layer_id`,
  `density_meta_by_layer_id`) had their SOLE consumer in
  `_persist_case_view_snapshot`; grep of `.inline_geojson_by_layer_id` /
  `.density_meta_by_layer_id` reads across src+tests = ZERO after the chop.
- `list_all_active_case_ids` had its SOLE caller in `_run_coldview_backfill`.

## Decision (PART 1 deletions)

Delete-don't-disable. Every deleted symbol greps to zero (src + tests) before
done; source-inspection tests re-anchored to the ABSENCE.

### persistence.py (-612 LOC)

- Constants: `CASE_VIEWS_BUCKET`, `CASE_VIEWS_PREFIX`, `CASE_MANIFESTS_PREFIX`,
  `CASE_VIEW_INLINE_GEOJSON_MAX_BYTES`, and the key seams
  `case_view_snapshot_key` / `case_manifest_key`.
- The whole materialized-snapshot machinery block: `build_case_view_snapshot`,
  `_resolve_cross_case_vector_inline`, `_resolve_case_owner`,
  `write_case_view_snapshot`, `_default_s3_put_case_view`,
  `_manifest_layer_from_summary`, `build_case_manifest`, `write_case_manifest`,
  `_default_s3_put_case_manifest`.
- `list_all_active_case_ids` (owner-agnostic enumerator; only the backfill used
  it).
- Now-unused imports `CaseManifest`, `CaseManifestLayer`, `CaseOpenEnvelopePayload`
  from `trid3nt_contracts.case`; `__all__` entries `CASE_VIEWS_BUCKET`,
  `CASE_VIEWS_PREFIX`, `case_view_snapshot_key`.

### server/_core.py (net -353 LOC)

- `_run_coldview_backfill` + `_COLDVIEW_BACKFILL_ENABLED` /
  `_COLDVIEW_BACKFILL_CONCURRENCY` (daemon-restart re-materialize sweep) + its
  startup wiring (`_coldview_task = create_task(...)`).
- `_persist_case_view_snapshot` + `_persist_case_manifest` defs and all 7 call
  sites + their trigger wiring: case-open (fire-and-forget create_task pair),
  case-create, case-rename, case-set-bbox, the turn-close finally
  snapshot/manifest block, the publish_layer-last-tool wrap-site
  snapshot/manifest, the auto-publish snapshot/manifest, and the turn-close
  create_task pair. The sibling `_persist_case_loaded_layers` calls at those
  sites -- the REAL reopen-persistence path -- are KEPT untouched.
- `__all__` entries `_persist_case_view_snapshot`, `_persist_case_manifest`.

GENERALIZED (not deleted -- live non-snapshot consumer): the background-task
registry `_BG_SNAPSHOT_TASKS` -> `_BG_TASKS`, drain
`_drain_bg_snapshot_tasks` -> `_drain_bg_tasks`, budget
`_BG_SNAPSHOT_DRAIN_TIMEOUT_S` -> `_BG_DRAIN_TIMEOUT_S` (env
`TRID3NT_BG_SNAPSHOT_DRAIN_TIMEOUT_S` -> `TRID3NT_BG_DRAIN_TIMEOUT_S`). The
startup tool-retrieval discover-index warm task registers here for GC-safety +
graceful drain; that consumer is untouched, so the registry survives with its
snapshot-specific naming/semantics chopped.

### emission/pipeline_emitter.py (-29 LOC)

- The two public property accessors `inline_geojson_by_layer_id` /
  `density_meta_by_layer_id` (defensive copies that ONLY the deleted snapshot
  sourced). The private `_inline_geojson_by_layer_id` /
  `_density_meta_by_layer_id` attrs are KEPT -- the live `emit_session_state`
  wire path reads them directly.

### tests

- Deleted wholesale (entirely snapshot/manifest/coldview machinery):
  `test_case_view_snapshot.py` (15 tests), `test_case_manifest_job165.py` (10),
  `test_coldview_backfill_box_wake.py` (7), `test_coldview_snapshot_durability_j1.py` (7).
- Reduced (kept the surviving-path tests, dropped the machinery pins):
  `test_case_history_rehydrate_f17.py` -- the two snapshot-trigger tests replaced
  by ONE absence guard (`test_case_open_writes_no_case_view_snapshot`: asserts
  the persister symbols are gone AND a real case-open still rehydrates chat);
  `test_nested_substep_persistence_job168.py` -- dropped
  `test_cold_view_snapshot_carries_children` + its docstring bullet + orphaned
  `CaseChatMessage` import (warm-reopen `get_session_state` coverage stays);
  `test_server_case_handlers.py` -- dropped the manifest dual-write test + the
  two `_persist_case_manifest` no-op tests (case create/rename/open handler
  coverage stays).

## Consequences

Remaining LOC: `_core.py` 11074 -> 10721; `persistence.py` 2060 -> 1448;
`pipeline_emitter.py` 2877 -> 2848.

`CaseManifest` / `CaseManifestLayer` (in `contracts/src/trid3nt_contracts/case.py`)
are now FULLY orphaned (zero consumers in src+tests). FLAGGED for the schema
owner as a contract-package chop candidate -- out of this server-refactor scope.

## PART 2 - severed-consumer inventory (read + classify; NOT chopped here)

Sweep of `_core` + `persistence` + `main` + `session` + `credentials` +
`telemetry` for write-for-an-absent-reader / serve-an-absent-caller shapes.
Verdict per suspect; PROPOSE-CHOP items await NATE's next ruling.

| # | Suspect | Consumer trace (grep evidence) | Verdict |
|---|---------|-------------------------------|---------|
| 1 | `SIGNED_URLS` / `signed_urls_enabled()` (layer_uri_emit.py) | Dormant scaffold; `emit_layer_uri` calls it only to log a WARNING then pass through byte-identically. Natural consumer = a browser direct-fetch signer that does not exist locally. | PROPOSE-CHOP |
| 2 | `persistence.get_user_by_firebase_uid` | ZERO callers in src (grep = def + docstring only). The `firebase_uid` IdP-sub slot is a dormant multi-user carrier. | PROPOSE-CHOP |
| 3 | `persistence.append_audit` (D.15 `audit_log` collection) | Writers: `mode2_classifier` + `_core` (`p_audit.append_audit`). READERS of `audit_log`: ZERO (no `find` on the collection anywhere). Fire-and-forget write to a sink nothing reads. (Distinct from in-memory `state.payload_warning_audit_log`, which IS read locally -- KEEP that.) | PROPOSE-CHOP |
| 4 | Anonymous-user provisioning: `_provision_anonymous_user`, `_try_reuse_anonymous_user`, `_anonymous_id_is_claimable`, `adopt_cases_to_user` (auth_handshake + persistence) | Live ONLY on the non-local-single-user branch of `authenticate_token`; local mode short-circuits to `_resolve_local_single_user` (`_is_local_single_user_mode()` True). Dormant multi-user/cloud auth. | PROPOSE-CHOP |
| 5 | `firebase_uid` field threading (SessionState.firebase_uid, build_auth_ack, User.firebase_uid) | Always `None` in local single-user mode; a provider-agnostic IdP-sub carrier with no local IdP. | UNCLEAR (contract-shaped; couples to #2/#4; NATE + schema owner) |
| 6 | `migrate_preauth_cases` / `_run_preauth_case_migration` (MIGRATION_ANON_UID stamp) | Live: called once at every startup from `run_server`. Stamps pre-Auth Cases so they do not leak to every signed-in user -- a multi-user governance concern. In a single-user local product every Case has one owner. | UNCLEAR (runs live, but the invariant it protects is multi-user-only) |
| 7 | `users` collection generally (`upsert_user`, `get_user_by_id`, USERS_COLLECTION) | Live local consumers: `auth_handshake` (`_resolve_local_single_user` upserts + reads the single local user; anonymous-reuse reads `get_user_by_id`). The single-user row is real. The broader multi-user surface is not. | KEEP (single local user row) / trim couples to #2/#4 |
| 8 | `CONFIRMATION_TRIGGERS` (empty `set()` in _core) + the write-carveout gate complexity | `CONFIRMATION_TRIGGERS` is an EMPTY set (FR-AS-8 scaffold); the confirm-gate code sized for cloud governance is dead-weight-adjacent. Solver-confirm (`SOLVER_CONFIRM_TOOLS`) is a SEPARATE live gate. | UNCLEAR (empty-set scaffold; confirm which gate code is reachable) |
| 9 | `expires_at` / ephemeral-Case TTL marker (persistence) | Persisted DATA field (ADR 0262 kept it under the byte-identical rule). File backend does not auto-reap; no local reader acts on it. | UNCLEAR (persisted data; NATE call on whether local honors TTL) |
| 10 | `tool_catalog_http` HTTP endpoints | The `_is_local_single_user_mode()`-gated routes (ingest-layer, probe-point) ARE the local plugin surface -- live. No endpoint found serving nothing local. | KEEP |
| 11 | telemetry emitters (`emit_tool_call_event`, `emit_turn_telemetry`, ...) | Sink is the local telemetry store + the `/api/telemetry-summary` HTTP route the plugin reads. Live local consumer. | KEEP |

## Gates (all green)

- Four pytest slices (`env -u TRID3NT_CACHE_BUCKET python -m pytest <slice>
  -p no:cacheprovider --timeout=300 -q`): [a-e] 1528 passed / 0 failed (incl the
  new absence guard); [f-o] 6408 passed / 4 failed; [p-r] 2026 passed / 2 failed;
  [s-z] 1446 passed / 0 failed. The 6 failures are EXACTLY the baseline (4
  fetch_resolution_gate + 2 river_dye) -- no regressions.
- workflows import + registry: 252 tools registered, 280 workflow modules import
  clean.
- daemon restart (`make agent`): clean boot, no boot errors; the log line
  `tool_retrieval: discover index warmed at startup` confirms the GENERALIZED
  `_BG_TASKS` registry preserved the warm-task consumer.
- `scripts/ws_smoke.py`: all_passed=True (TEST A chat + TEST B geocode tool call
  + reply).
- flood canary `scripts/run_sfincs_direct.py`: status=ok, depth COG published
  (`s3://trid3nt-runs/.../overviews/*.tif`), 7 depth frames + peak + sfincs_map.nc
  in MinIO.
- case-lifecycle reopen check (seed a Case with a persisted published layer ->
  live WS `case-command select` == REOPEN -> assert the case-open envelope's
  `session_state.loaded_layers` carries the layer): PASS -- "layer rebuilt from
  persisted store via WS replay". Proves snapshots were never the reopen path and
  it stays true.
