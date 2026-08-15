# ADR 0267 - server-refactor wave 7: severed-consumer chop, round 2

Status: LANDED (2026-08-15). Wave 7 of the server refactor. Round 1 (ADR 0266,
c258e139) chopped the case-view snapshot + coldview machinery and produced the
PART-2 severed-consumer inventory (11 suspects, verdict per row). NATE ruled:
chop ALL EIGHT remaining PROPOSE-CHOP / UNCLEAR items. Behavior-preserving for
every LIVE local path.
Date: 2026-08-15

## Context

TRID3NT is a local, single-user product (QGIS plugin + local daemon + MinIO +
local docker solvers). `_core` + `persistence` + `credentials` carried cloud /
multi-user residue: live code paths whose CONSUMERS do not exist locally. ADR
0266's PART-2 inventory traced each; this wave deletes the eight NATE confirmed.

Delete-don't-disable. Every deleted symbol greps to zero (src + tests) before
done; tests pinning deleted machinery were deleted or reduced to absence
guards; source-inspection tests re-anchored to the ABSENCE. Two persisted-shape
tolerance proofs were added (chops 5 + 8) because existing user/case records on
disk still carry the retired keys.

## The eight chops

### 1. SIGNED-URL seam (`emission/layer_uri_emit.py`, -38 LOC)

`SIGNED_URLS` / `signed_urls_enabled()` and the WARNING branch in
`emit_layer_uri` were a dormant browser-fetch signer scaffold; passthrough
today. Deleted the env const, the predicate, the `__all__` entry, and the
warning path. The guardrail (drop a renderable raster with an un-renderable
uri) and every PASS/DROP outcome are byte-identical to before. Natural
consumer = a browser direct-fetch signer that does not exist on the QGIS-only
stack.

Evidence: grep `SIGNED_URLS` / `signed_urls_enabled` across `server/src`,
`server/tests`, `qgis-plugin`, `scripts` = ZERO. Tests: dropped the 6
`SIGNED_URLS` tests in `test_layer_uri_emit.py` + the `SIGNED_URLS=true`
byte-identity test in `test_pipeline_emitter.py`; kept the drop-warning + the
seam-is-a-no-op passthrough tests.

### 2. `persistence.get_user_by_firebase_uid` (-22 LOC)

Zero callers in src (grep = def + docstring only). The `firebase_uid` IdP-sub
slot is a dormant multi-user carrier. Deleted the method; retargeted the
`test_persistence.py` user round-trip to `get_user_by_id`.

### 3. `append_audit` + the `audit_log` collection surface (-49 LOC persistence, -26 LOC _core)

Writers existed (`mode2_classifier` doc + `_core` `p_audit.append_audit`),
READERS zero (no `find` on the collection anywhere). Deleted
`Persistence.append_audit`, the `AUDIT_COLLECTION` constant + `__all__` entry,
the now-unused `new_ulid` import, and the `_core` mode2-candidate audit block
(candidates are still logged in-process). Updated the `mode2_classifier`
docstrings to state the no-sink constraint.

The in-memory `state.payload_warning_audit_log` is a SEPARATE, LIVE structure
(read locally by the payload-warning flow) and was left untouched -- verified
distinct.

Evidence: grep `append_audit` / `AUDIT_COLLECTION` / `"audit_log"` collection
in src = only the constraint comments + the absence-guard test. Tests: deleted
`test_mode2_audit_mcp.py` (whole file was the collection round-trip); reduced
`test_mode2_classifier.py`'s writer-removed test to an absence guard that also
asserts `not hasattr(Persistence, "append_audit")`; deleted the
`test_append_audit_writes_log_entry` persistence test.

### 4. Anonymous-user provisioning (`credentials/auth_handshake.py` + `persistence`, net -234 LOC auth_handshake)

`solver_backend()` is hardwired to `local-docker`, so `_is_local_single_user_mode()`
is ALWAYS True and the entire non-local anonymous branch of `authenticate_token`
was dead. Collapsed `authenticate_token` to: local mode -> `_resolve_local_single_user`;
otherwise raise the new typed `NonLocalAuthUnsupported` (fail LOUD, never
silently mint an unauthenticated identity). Deleted `_provision_anonymous_user`,
`_try_reuse_anonymous_user`, `_anonymous_id_is_claimable`, the once-per-process
`_local_case_adoption_done` guard, and `Persistence.adopt_cases_to_user`.
Inlined `_resolve_local_single_user` to a direct `get_user_by_id` +
`upsert_user` on the fixed `LOCAL_SINGLE_USER_ID` (the is_anonymous/is_active
reuse gate was an anti-hijack defense for arbitrary client hints -- moot for a
fixed constant id). The stray-case adoption sweep (a one-time historical
migration for pre-fix per-client anon owners) is retired -- every Case is now
created under and listed for the one local user.

The `FileMCPClient` `update-many` handler was removed too -- its only callers
(`adopt_cases_to_user` + `migrate_preauth_cases`, chop 6) are gone, so no
Persistence surface issues `update-many` anymore.

Behavior on the live local path is byte-identical: `_resolve_local_single_user`
still reuses the persisted local-user record (stable `created_at`) and provisions
it with `is_anonymous=True` on first connect. Tests: reworked
`test_auth_handshake.py` (local-user resolution + a `NonLocalAuthUnsupported`
raise test); kept `test_sticky_anonymous_user.py` + `test_anon_identity_convergence.py`
local-convergence tests (they already asserted the fixed local user); deleted
the adoption test, reduced it to an absence guard
(`not hasattr(Persistence, "adopt_cases_to_user")`).

### 5. `firebase_uid` field threading (-contract fields, wire + persisted shape)

Always `None` locally; a provider-agnostic IdP-sub carrier with no local IdP.
Removed `AuthResult.firebase_uid`, the `build_auth_ack` mirror,
`AuthAckEnvelope.firebase_uid` (contract), `SessionState.firebase_uid`,
`_core._bind_auth_result`'s `state.firebase_uid = ...`, and `User.firebase_uid`
(contract).

WIRE PROOF (plugin auth-ack read): `qgis-plugin/trid3nt/net/trid3nt_client.py`
`_handshake` reads ONLY `payload["user_id"]`, `payload["is_anonymous"]`, and the
`endpoints` / `http_base` / `data_base` bases from the auth-ack -- grep of the
whole `qgis-plugin` tree for `firebase` = ZERO. The plugin does NOT read the
field, so it was removed cleanly from the envelope (no null-emitting shim
needed).

PERSISTED-SHAPE PROOF (old user rows): `User` is `extra="forbid"`, but
`Persistence.get_user_by_id` already filters the stored doc to
`User.model_fields` BEFORE `model_validate`, so a legacy row still carrying a
`firebase_uid` key loads without crashing (the key is dropped). Proven by the
new `test_get_user_by_id_tolerates_stale_firebase_uid_key` (seeds a legacy
user doc with `firebase_uid` on disk, asserts it loads and the attr is absent).

### 6. `migrate_preauth_cases` / `_run_preauth_case_migration` (-51 LOC persistence, -37 LOC _core)

The multi-user pre-Auth case-leak governance concern is moot for a single-user
product where every Case has one owner. Deleted `Persistence.migrate_preauth_cases`,
`_core._run_preauth_case_migration` + `MIGRATION_ANON_UID`, and the `run_server`
startup call. Cleaned the `upsert_case` / `list_cases_for_user` docstrings that
referenced the sweep. Deleted `test_preauth_case_migration.py`.

### 7. `CONFIRMATION_TRIGGERS` empty scaffold (`_core`, -5 LOC + docstrings)

An empty `set()` (FR-AS-8 scaffold) that no code path membership-tested (grep =
def + docstrings only). Deleted the set + its module-docstring mention + the
`_persist_chat_message` docstring reference + the persistence-module docstring
mention. The LIVE solver-confirm gate (`SOLVER_CONFIRM_TOOLS` /
`FETCH_CONFIRM_TOOLS` / the `_PENDING_CONFIRMATIONS` block-and-wait) is a
SEPARATE mechanism and was untouched.

### 8. `expires_at` ephemeral-Case TTL stamping (`persistence` + `_core`)

Persisted DATA field with no local reader/reaper (the file backend does not
auto-reap). Stripped the STAMPING at every write site: removed the `ephemeral`
kwarg + the numeric-`expires_at` branch from `upsert_case`, deleted the
`touch_case` method, removed the `touch_case` call in `_touch_session_record`,
and dropped `ephemeral=state.is_anonymous` at both `upsert_case` call sites
(case-create + auto-create). Cases are now durable, which is a no-op change
(nothing reaped them before).

SCOPE NOTE: this is the **ephemeral-CASE** numeric TTL (the ADR 0266 inventory
item #9). The D.6 **session-record** TTL (`touch_session`'s ISO `expires_at`,
`SESSIONS_TTL`, the REQUIRED `SessionDocument.expires_at` contract field) is a
SEPARATE mechanism with its own tests and was left intact -- stripping a
required contract field is a larger change outside this inventory item. Flagged
here for a NATE call if the session TTL should also go.

PERSISTED-SHAPE PROOF (old case rows): `_doc_to_case_summary` already drops any
key not in `CaseSummary.model_fields`, so a legacy case doc still carrying a
numeric `expires_at` reads back as a clean `CaseSummary`. Proven by
`test_get_case_tolerates_legacy_expires_at` (seeds an old-shape case doc with
`expires_at` on disk, reads it back, asserts no crash + key absent from the
wire) and `test_doc_to_case_summary_drops_stale_expires_at`.

## Orphaned-contract flags (out of server-refactor scope)

- `trid3nt_contracts.collections.CASES_ANON_TTL_SECONDS` is now orphaned in src
  (its only consumers were the deleted ephemeral-Case TTL path). FLAGGED for the
  schema owner as a contract-package chop candidate (mirrors ADR 0266's
  `CaseManifest` flag). Left in place this wave.

## Consequences (remaining LOC)

- `server/_core.py`: 10721 -> 10610 (-111)
- `persistence.py`: 1448 -> 1210 (-238)
- `credentials/auth_handshake.py`: 574 -> 340 (-234)
- `emission/layer_uri_emit.py`: 296 -> 258 (-38)
- Total src: -621 LOC. Diffstat (server + contracts, incl. tests):
  312 insertions / 1781 deletions across 21 files (3 test files deleted).

The severed-consumer inventory from ADR 0266 PART 2 is now fully discharged
(items 1-9 chopped or scope-flagged; items 7/10/11 were KEEP).

## Gates (all green)

- Four pytest slices (`env -u TRID3NT_CACHE_BUCKET python -m pytest <slice>
  -p no:cacheprovider --timeout=300 -q`): [a-e] GATE_AE; [f-o] GATE_FO;
  [p-r] GATE_PR; [s-z] GATE_SZ. Baseline = EXACTLY 4 fetch_resolution_gate
  ([f-o]) + 2 river_dye ([p-r]) failures -- no regressions.
- workflows import + registry: GATE_WF.
- daemon restart (`make agent`) + `scripts/ws_smoke.py`: GATE_SMOKE (the smoke
  exercises the auth handshake -- it MUST still connect after chops 4/5).
- flood canary `scripts/run_sfincs_direct.py`: GATE_CANARY.
- case-lifecycle reopen check (create/publish/close/reopen via WS replay):
  GATE_REOPEN.
- old-shape tolerance: `test_get_user_by_id_tolerates_stale_firebase_uid_key`
  (chop 5) + `test_get_case_tolerates_legacy_expires_at` (chop 8): GATE_OLDSHAPE.
