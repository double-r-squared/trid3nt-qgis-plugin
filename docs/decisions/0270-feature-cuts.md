# ADR 0270 - server-refactor wave 11: five feature-level cuts

Status: LANDED (2026-08-15). Wave 11 of the server refactor. Waves 1-10 landed
at 4e254e17 (ADR 0261-0269). NATE ruled five FEATURE-level cuts -- mechanics
that predate the local shift and no longer earn their keep on the QGIS-only,
single-user, one-local-compute-environment product. Behavior-preserving for
every LIVE local path.
Date: 2026-08-15

## Context

Waves 6-7 (ADR 0266/0267) chopped severed CONSUMERS (live code whose callers do
not exist locally). Wave 11 goes one level up: whole FEATURES whose premise
(multi-user identity, cloud instance-class sizing, a browser research modal,
a second-pipeline research toggle) is moot on the local product. Each cut was
traced before removal; every deleted symbol greps to zero across src + tests +
`qgis-plugin` + `scripts`; comments were rewritten to present-tense truth;
delete-don't-disable throughout. Contract/wire changes are coordinated (server
contract + plugin ship together, synced by `scripts/install_plugin.sh`).

## The five cuts

### 1. `research_mode` dead-wire parameter

`UserMessagePayload.research_mode` (`ResearchMode` Literal) was a "pinned but
never branched" toggle: logged + forwarded, never read for a second pipeline.

TRACE: grep `qgis-plugin/` for a SENDER = ZERO -- the plugin's `user-message`
payload builder (`trid3nt_client.py`) never sets it, so the server always saw
the default `"research"`. The `default_research_mode` "User pref" was never a
contract field: `User.prefs` is an open dict; the name appeared only as a
docstring example + a test sample value.

CUT: removed the contract field + `ResearchMode` Literal + `__all__` entry +
module-docstring bullet (`ws.py`); the param off `_stream_model_reply` /
`_dispatch_model_turn_and_persist`, the dispatch log field, the `um.research_mode`
call-site arg, and the module-docstring "pass-through pinned" block (`_core.py`);
the prefs-docstring mention (`user.py`).

WIRE PROOF: `UserMessagePayload` is `extra="forbid"`; the new
`test_user_message_research_mode_field_removed` asserts a `research_mode=` kwarg
is now rejected. PERSISTED-SHAPE: `User.prefs` is an open dict, so a legacy row
carrying `default_research_mode` loads unchanged (no per-key validation) --
documented in the `user.py` prefs docstring; the `test_user` sample pref was
repointed to a neutral key. Server stream-mock test signatures dropped the
positional `research_mode`. Net ~34 LOC.

### 2. Firebase H.4 capability-tier claim

`TierClaim` (`auth.py`) + `AuthAckEnvelope.tier` + `AuthResult.tier` +
`SessionState.tier` + the `_bind_auth_result` assignment + the auth-ack log
field.

TRACE: plugin grep for a tier READER = ZERO -- `trid3nt_client._handshake`
reads only `user_id` / `is_anonymous` / endpoint bases off the ack.
`SessionState.tier` was written (`state.tier = result.tier`) but had ZERO
readers (no tier-gating anywhere). The `tier=template` retrieval-pool vocabulary
and OpenRouter `:free` model handling are UNRELATED (share only the word) --
untouched.

CUT: the `TierClaim` type + `__all__` entry + ack field + tier docstrings
(`auth.py`); the `TierClaim` import + `AuthResult.tier` + the `tier="free"`
return + `build_auth_ack`'s `tier=` + docstrings (`auth_handshake.py`);
`SessionState.tier` (`session.py`); the `state.tier` bind + the log field +
docstring (`_core.py`). `Literal` import in `auth.py` dropped (now unused).

CONTRACT PROOF: `contracts/` run at the end (wave-8 lesson). New
`test_auth_ack_tier_field_removed` asserts `extra="forbid"` rejects a `tier`
key; `test_auth_ack_envelope_anonymous` asserts `not hasattr(ack, "tier")`.
Net ~40 LOC.

### 3. Anonymous-identity plumbing (TRACE-FIRST)

VERDICT: the local single-user identity is CONSTANT-keyed, NOT anon-id-keyed.
`authenticate_token` is hardwired to `_resolve_local_single_user`, which returns
the fixed `LOCAL_SINGLE_USER_ID` regardless of any hint; case ownership keys off
`state.authenticated_user_id` == that constant (verified at both `upsert_case`
call sites + `list_cases_for_user`). So the sticky `anonymous_user_id` hint and
the session-scoped anon-id mirror (which existed to converge dual browser
sockets on one minted anon id) are 100% vestigial. FULL removal warranted.

CUT (server): `_SESSION_ANON_ID` + `_SESSION_ANON_ID_CAP` +
`_get_session_anon_id` / `_set_session_anon_id` / `_apply_session_anon_hint`
(`session.py`, + its now-unused `AuthTokenEnvelope` import); the imports + the
two `_apply_session_anon_hint`/`_set_session_anon_id` call sites in the
auth-token + implicit-anonymous handlers (`_core.py`); `AuthTokenEnvelope.anonymous_user_id`
(contract `auth.py`) + the Part-C docstrings (`auth_handshake.py`, `persistence.py`).

CUT (plugin, coordinated wire change): `AgentClient.__init__`'s
`anonymous_user_id` param + `self.anonymous_user_id` + the `{token,
anonymous_user_id}` handshake send (now token-only) + the sticky-replay block
(`trid3nt_client.py`); the `anonymous_user_id` QSettings property/setter + the
`_ULID_RE` guard + `import re` (`plugin_settings.py`); the `anonymous_user_id`
params through `AgentWorker` / `AgentBridge.start` (`ws_bridge.py`); the
`connect_agent` wiring + the `_on_connected` sticky store (`dock.py`).

WIRE PROOF: `AuthTokenEnvelope` is `extra="forbid"`; the contract
`test_auth_token_rejects_extra_fields` now lists `anonymous_user_id` among the
rejected extras, and `test_auth_token_envelope_rejects_anonymous_user_id`
(server) proves the same. CONVERGENCE PROOF: `test_anon_identity_convergence`
rewritten to the token-only shape -- multiple connects (and a `None` envelope)
all resolve to `LOCAL_SINGLE_USER_ID`, one user record, case-list stable across
reconnect; `test_session_anon_registry_removed` is the absence guard. Plugin
`test_milestone2` rewritten to assert a token-only auth-token on the SAME
session_id across reconnect; the `TestAnonymousIdGuard` class (tested the
removed property) deleted from `test_milestone3`.

NO PERSISTED-SHAPE concern: `anonymous_user_id` was a TRANSIENT wire hint on
`auth-token`, never persisted server-side (the sticky id lived only in the
plugin's QSettings). The coordinated wire change means a STALE plugin (pre-0.3.16)
sending `anonymous_user_id` would now hit `extra="forbid"`; on the local product
plugin + server ship + reload together, so this is an intended coordinated
upgrade (see 0.3.16 note). Net ~250 LOC (server + plugin).

### 4. Compute-class / vCPU instance-class sizing

Locally there is ONE compute environment (`local-docker` on the host CPUs);
the auto-vertical-scaling + instance-sizing layer under the #154 granularity
gate is dead vocabulary.

TRACE: `solve_progress_vcpus` already returned `os.cpu_count()` unconditionally
(local-docker only); `COMPUTE_CLASS_SIZING`'s vcpus/mem/omp map was never
applied to the `docker run` argv (`launch_local_solver` uses it only to stamp
the telemetry `ExecutionHandle.compute_class`); `select_compute_class`'s
element-count ladder picked an instance tier that the single local box ignores.

CUT: `COMPUTE_CLASS_SIZING`, `select_compute_class` (+ the
`COMPUTE_CLASS_*_MAX_ELEMENTS` thresholds + `COMPUTE_CLASS_FALLBACK` +
`_env_int`), `solve_progress_vcpus` + their `__all__` entries (`solver.py`); the
dead Batch else-branch in the SWMM solver-confirm card (collapsed to the LOCAL
lane: `vcpus=os.cpu_count()`, no Spot label) (`solver_confirm.py`); the
`effective_compute_class = select_compute_class(...)` + `solve_progress_vcpus(...)`
blocks in six composers (sfincs flood, elmfire fire_spread, geoclaw inundation,
swan wave_field, swmm urban_flood, compute_canopy_height) -- now the caller's
`compute_class` flows through unchanged and the live solve-progress card reads
`vcpus=os.cpu_count()`.

KEPT (the live #154 granularity gate): `GranularitySuggestion` (cells / payload
MB / runtime / cell_cap / coarsened) is UNCHANGED -- it still carries `vcpus`
(now `os.cpu_count()`) and `compute_class` (`"local"`), so the gate's estimate
tests stay green. Also kept: the `compute_class` param + `_COMPUTE_CLASS_ALIAS`
+ `ExecutionHandle.compute_class` telemetry field (pervasive, harmless
single-environment default); `sfincs_builder.resolve_solve_vcpus` +
`test_sfincs_autoscale` (the SEPARATE perf-model cell-cap sizing, granularity-
adjacent, not named for the cut).

TESTS: `test_select_compute_class` (459 LOC) + `test_solve_progress_vcpus`
(88 LOC) deleted; `test_compute_canopy_height`'s auto-select test reduced to a
default-class assertion; `test_granularity_gate` / `test_solver_confirm_gate` /
`test_sfincs_autoscale` stay green (93 passed) + the six composer chains
re-verified (212 passed). Net ~360 LOC src.

### 5. Mode-2 offer-to-add catalog flow

NATE: moot -- endpoint additions are easily authored + discovery already covers
the need. A whole feature: the deterministic `.gov`/`.edu` classifier, the
pending catalog-offer registry + TTL prune + endpoint probe + draft-entry
completion + user-overlay append, the wire envelopes, and the plugin offer card.

CUT (server): the `mode2_classifier.py` module (411 LOC:
`classify_for_mode2` / `Mode2Candidate` / `Mode2CandidateEnvelope`); the
catalog-offer section of `interactions.py` (`_PENDING_CATALOG_OFFERS`,
`_CATALOG_OFFER_MAX`, `_prune_catalog_offers`, `_register_pending_catalog_offer`,
`_pop_pending_catalog_offer`, `_probe_catalog_endpoint[_sync]`,
`_complete_catalog_entry`, `_handle_catalog_addition_response`, `_slug`) +
its now-unused `re`/`new_ulid`/`now_utc`/`_catalog_offer_ttl_s` imports;
`_catalog_offer_ttl_s` (`config.py`); `append_user_catalog_entry`
(`catalog_common.py`, KEEPING the `_merge_user_overlay` READ path -- hand-
authored `user_catalog.yaml` entries still surface); the `_maybe_emit_mode2_candidate`
emit + the web_fetch call site + the `catalog-addition-response` dispatch + all
imports (`_core.py`); the `ProbeFindings` / `SuggestedCatalogEntry` /
`OfferCatalogAdditionPayload` / `CatalogAdditionDecision` /
`CatalogAdditionResponsePayload` envelopes + `__all__` + message-type maps
(`ws.py`).

CUT (plugin): the offer-to-add card `Mode2CandidateCard` + chrome (`cards.py`);
the `parse_mode2_candidate` / `parse_offer_catalog_addition` /
`Mode2CandidateRequest` / `mode2_reason_lines` / `resolve_mode2_decision` /
`mode2_decision_chip` section + `__all__` + the now-unused `urlparse` import
(`gate.py`); the `_show_mode2_candidate_card` / `_on_mode2_decision` methods +
the two `elif kind ==` handlers + the import (`dock.py`); `respond_catalog_addition`
+ the two `mode2-candidate` / `offer-catalog-addition` event handlers
(`trid3nt_client.py`).

TESTS: server `test_mode2_classifier` (219) + `test_catalog_offer_loop` (239)
deleted; `test_catalog_user_overlay` rewritten to seed the overlay YAML directly
(keeps the merge-READ coverage, drops the `append_user_catalog_entry` writer);
contract `test_catalog` / `test_ws` de-mode2'd (the payload-registry test now
asserts the envelopes are ABSENT); plugin `test_mode2_offer` (364) +
`qt_mode2_offer_harness` (221) deleted, `stub_server` de-mode2'd. Net ~1400 LOC
(server + plugin).

## Coordinated wire / plugin-sync note (for NATE)

Plugin bumped to **0.3.16** (`metadata.txt`, ASCII changelog, configparser
continuation-line indentation preserved -- `test_metadata_parses` green). Cuts
3 + 5 are coordinated wire changes (`auth-token` drops `anonymous_user_id`;
`catalog-addition-response` / `mode2-candidate` gone), so 0.3.16 must be synced
to NATE's box via `scripts/install_plugin.sh` + a QGIS reload before the changes
take effect there (QGIS runs the INSTALLED profile copy, not the repo).

## Consequences (remaining LOC)

- `server/_core.py`: 10610 -> 10452 (-158)
- `persistence.py`: 1210 -> 1206 (-4)
- `interactions.py`: 465 -> 141 (-324)
- `credentials/auth_handshake.py`: 340 -> 333 (-7)
- `mode2_classifier.py`: 411 -> 0 (deleted)
- Diffstat (server + contracts + plugin, incl. tests): 287 insertions /
  4377 deletions across 50 files (6 files deleted: mode2_classifier + 5 test
  files; `test_mode2_offer` + `qt_mode2_offer_harness` on the plugin side).

## Gates (all green)

- Four pytest slices (`env -u TRID3NT_CACHE_BUCKET python -m pytest <slice>
  -p no:cacheprovider --timeout=300 -q`): baseline EXACTLY 4 fetch_resolution
  ([f-o]) + 2 river_dye ([p-r]) failures -- no regressions.
- contracts/: 708 passed (was 710; net -2 tests after the research_mode/mode2
  removals + reworks).
- registry import: 252 (stable; no tool registered/removed -- mode2_classifier
  was a gate module, never a TOOL_REGISTRY entry).
- daemon restart + `scripts/ws_smoke.py` (exercises the auth handshake after
  cuts 2/3): all_passed.
- flood canary `scripts/run_sfincs_direct.py`: status=ok + depth COG + envelope
  (exercises cut 4's solve-progress path).
- case-lifecycle reopen check.
- old-shape / wire proofs: cut 1 (`User.prefs` open-dict tolerance +
  `test_user_message_research_mode_field_removed`); cut 3
  (`test_auth_token_envelope_rejects_anonymous_user_id` +
  `test_anon_identity_convergence` token-only rewrite).
- PLUGIN suite (`cd qgis-plugin && make test`): 392 tests, the SAME 2
  pre-existing offscreen-Qt harness failures as HEAD (`test_case_bbox`,
  `test_tool_picker`, both verified failing on unmodified HEAD ui files) --
  ZERO regressions from wave 11.
