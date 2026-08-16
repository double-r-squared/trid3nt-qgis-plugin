# ADR 0269 - server-refactor wave 10: smell-to-code audit

Status: LANDED (audit + corrections). Date: 2026-08-15.

## Context

NATE's directive: "if the comments smell there is code that needs to be cut
most likely." Waves 8-9 (ADR 0268) swept notation (ADR/FR/job-id/Decision
citations, milestone tags). NATE then named the deeper move: a comment that
narrates a *deleted system* usually annotates *dead code*. This wave is
read-and-judge over comments AND the code they mark, across the server package
EXCLUDING `agent/tools/` + `agent/workflows/`.

The method, per lead: (1) read the file, (2) classify each smell (milestone
tags; tombstones "X is GONE"; dead-system nouns Atlas/Mongo-MCP/Gemini/Vertex/
DynamoDB/TiTiler; attributions/dates; spec-sign refs; stale future-tense), (3)
JUDGE THE CODE by tracing callers/states, not the comment's claim, (4) ACT:
dead code -> DELETE; live code with a lying comment -> fix to present-tense
truth; wire/persisted-shape-touching or ambiguous -> FLAG.

## The proof example (NATE's starting point): Persistence None-fallback

NATE's hypothesis: `init_persistence_from_env` "now does literally nothing";
its docstring narrated a deleted Atlas/MCP bootstrap + an "M1 in-memory
fallback" contract ("callers MUST handle the None case"); if local startup
ALWAYS binds a Persistence, the None-fallback contract is dead and the
None-handling branches guard an impossible state.

TRACE (verified, not trusted):

- `main.run()` calls `_maybe_bind_dev_persistence()` (binds the file backend)
  BEFORE `run_server()`, which then calls `init_persistence_from_env()`
  (preserve-or-None, no bind). So on the DEFAULT path a Persistence is bound
  before serving.
- BUT `is_dev_persistence_enabled()` honors `TRID3NT_DEV_PERSISTENCE=0` -> the
  singleton stays `None`. This is a LIVE, TESTED escape hatch: 30 `get_persistence()`
  consumers across `_core.py` / `tool_catalog_http.py` / `cases/ingest_user_layer.py`
  handle `None`; the case-command dispatch emits a typed INTERNAL_ERROR naming
  `TRID3NT_DEV_PERSISTENCE=0`; two test files exercise the `=0` path directly.

VERDICT: the None-fallback contract is **LIVE**, not dead. NATE's "does startup
ALWAYS bind?" resolves to NO -- the `=0` no-persistence path is a real, tested
consumer. Per the kickoff ("if some path genuinely runs unbound (tests?), say so
and scope the cut honestly"), the None-handling branches are NOT cut.

The Atlas/MCP-narrating docstring NATE remembered was ALREADY corrected in the
working tree (ADR 0262 cloud-seam chop rewrote the persistence-singleton comment
to "file-backed document store"; the current docstrings are present-tense
truthful). `init_persistence_from_env` is thin (preserve-check + a startup
diagnostic log line "singleton already bound; retained" vs "remains unbound")
but LIVE (called at startup, pinned by the preserve-contract tests). It is a
startup diagnostic, not vestigial -> FLAGGED for NATE, not cut.

## Lead -> verdict -> action table

| Comment smell | Code trace | Verdict | Action / LOC |
| --- | --- | --- | --- |
| `persistence.py` docstrings narrating "cloud MCP client" / `mongodb-mcp-server` / Atlas / D.2 / D.6 / NATE attributions | `Persistence` + `FileMCPClient` are LIVE; only the file backend + test mocks implement `MCPClientProtocol` | LIVE, lying comments | Comment fixes (in-tree, ADR 0262 body extended): dead-noun swaps, spec-ref/attribution removal |
| `init_persistence_from_env` "does nothing" (proof example) | preserve-or-None + startup diagnostic log; called in `run_server`; `=0` None path tested; preserve-contract pinned by 2 test files | LIVE (thin startup diagnostic) | FLAG (keep; do not cut) |
| None-fallback contract "callers MUST handle None" | 30 `get_persistence()` None-handling consumers + tested `TRID3NT_DEV_PERSISTENCE=0` escape hatch | LIVE | No cut (scoped honestly per kickoff) |
| `session.py` SessionState "M1 keeps everything in-process; Mongo-backed session restore lands when the LLM-facing DB seam is wired" | restore IS wired: `Persistence.get_session_state` called at 7 sites (case-open/select/create) | LIVE code, FALSE future-tense + dead noun | Comment fixed to present-tense truth |
| `telemetry.py` `get_persistence` "unbound (M1 path)" | the unbound path is the `TRID3NT_DEV_PERSISTENCE=0` escape hatch | LIVE, archaeology tag | Comment fixed |
| `test_persistence.py` `test_live_mcp_write_then_read` importing `trid3nt_server.mcp` (`MCPClient`, `fetch_srv_from_secret_manager`) | `trid3nt_server.mcp` module was removed with GCP decommission; import is dead | DEAD (deleted-module import) | DELETED (in-tree): the env-guarded live test + its dead import (~35 LOC) |
| `test_file_persistence.py` MCP-stdio "defers to real MCP" tests | `TRID3NT_MONGO_MCP_STDIO` read nowhere in code | DEAD | DELETED / rewritten (in-tree): 2 stdio tests folded into `=1` enable test |
| `test_file_persistence.py` module docstring "MongoDB Atlas MCP server is the production LLM-facing DB seam (FR-AS-4)" | Atlas decommissioned; file backend is the only seam | LIVE tests, FALSE premise | Docstring fixed to present-tense truth |
| `test_mongo_mcp_wiring.py` docstring lists 6 tests incl 2 deleted Atlas/MCP-stdio tests; tombstone narrating GCP/Atlas/`trid3nt_server.mcp`/AWS/DynamoDB | only 4 tests exist; the 2 stdio tests are already gone; the named systems are all decommissioned | LIVE tests, FALSE inventory + dead-system tombstone | Docstring/tombstone rewritten to the 4 real tests; dead `TRID3NT_MONGO_MCP_*` env pops removed |
| `uri_registry.py` `_titiler_cog_uri` + Branch-3 TiTiler unwrap ("LEGACY GUARD, TiTiler exit") | unwraps OLD PERSISTED case docs / layer_handles maps that still carry TiTiler tile-template URLs | LIVE guard over PERSISTED data | FLAG (persisted-shape; do not cut) -- matches ADR 0262's own residual-TiTiler flag |
| `_core.py` `confirm-response`/`disambiguation-response`/`clarification-response` "Scaffolding only -- no triggers yet. noop M1" | these are LIVE contract message types (`ws.py` `ConfirmResponsePayload` etc.) with real script senders; the real confirm flow uses typed per-gate messages (`tool-payload-confirmation`, `credential-provided`, ...) | LIVE wire types, tolerant no-op ack (deliberate) | FLAG (wire-shape; the ack is intentional, not dead) |

## Falsehoods corrected (verbatim before -> after)

1. `server/src/trid3nt_server/server/session.py` (SessionState docstring):
   - BEFORE: "Per-session in-memory state. M1 keeps everything in-process;
     Mongo-backed session restore lands when the LLM-facing DB seam is wired."
   - AFTER: "Per-session in-memory state, held in-process for the life of the
     session. Durable restore is a separate path: `Persistence.get_session_state`
     rehydrates chat history, loaded layers, and charts on `case-open` /
     `case-select`. This dataclass is the live in-process mirror, not the
     durable store."
   - (Also: "stay as the M1 mirror" -> "mirror the pipeline"; removed a
     duplicated "replace-not-reconcile".)

2. `server/src/trid3nt_server/telemetry.py` (`get_persistence` docstring):
   - BEFORE: "or if the Persistence singleton is unbound (M1 path)."
   - AFTER: "or if the Persistence singleton is unbound (the
     `TRID3NT_DEV_PERSISTENCE=0` no-persistence path)."

3. `server/tests/test_file_persistence.py` (module docstring):
   - BEFORE: "The MongoDB Atlas MCP server is the production LLM-facing DB seam
     (FR-AS-4); for LOCAL DEV without Atlas/MCP, `FileMCPClient` satisfies the
     same `MCPClientProtocol` against per-collection JSON files. These tests
     exercise that substrate through the unmodified `Persistence` wrapper to
     prove the file-backed shim is interchangeable with the live MCP path."
   - AFTER: "`FileMCPClient` is the persistence substrate: it satisfies
     `MCPClientProtocol` against per-collection JSON files. These tests exercise
     that substrate through the unmodified `Persistence` wrapper, proving the
     file-backed shim round-trips every typed contract the wrapper serializes."

4. `server/tests/test_mongo_mcp_wiring.py` (module docstring): rewritten from a
   6-test inventory (2 of which -- `test_mcp_stdio_1_attempts_connection`,
   `test_mcp_stdio_1_start_failure_does_not_crash_server` -- no longer exist) to
   the 4 real tests; dropped the "job-0200 Wave 4.11 M1" archaeology and all
   Atlas/`MCPClient.start` narration.

5. `server/tests/test_mongo_mcp_wiring.py` (tombstone block):
   - BEFORE: "GCP decommissioned: the live MongoDB-MCP (Atlas) stdio bootstrap
     was removed ... prod persistence on AWS is the file / DynamoDB backend ...
     `MCPClientProtocol` seam stays as the abstract surface DynamoDB and the
     file backend implement."
   - AFTER: "`MCPClientProtocol` is the abstract document-store surface: the
     file backend implements it in production and an in-memory mock implements
     it in tests. The compatibility test below pins that structural contract."
   - (Also: two test docstrings de-archaeologied -- "M1 in-memory path" ->
     "no-persistence escape hatch"; "zero Atlas configuration" -> "zero
     configuration"; dead `TRID3NT_MONGO_MCP_STDIO`/`_URL` env pops removed.)

## FLAGGED for NATE (do not cut without a call)

| Item | Location | Why flagged | Recommendation |
| --- | --- | --- | --- |
| `init_persistence_from_env` (thin preserve-or-None + startup diagnostic) | `server/_core.py` ~727 | Startup-wire; pinned by the preserve-contract tests; its only work is a boot diagnostic log | KEEP. The vestigial-looking Atlas docstring NATE remembered was already fixed. Optionally inline into `run_server`, but the diagnostic log has value. |
| TiTiler tile-template unwrap (`_titiler_cog_uri` + Branch-3 in `_normalize_layer_uri`) | `emission/uri_registry.py` ~314, ~843 | Guards OLD PERSISTED case docs / `layer_handles` maps carrying legacy TiTiler URLs | KEEP until a persisted-data migration proves no live Case carries a TiTiler template. Same class ADR 0262 already flagged (~88 residual TiTiler prose refs). |
| `confirm-response` / `disambiguation-response` / `clarification-response` tolerant no-op ack | `server/_core.py` ~10306 | LIVE `ws.py` contract types with real script senders; deliberate graceful ack (removing it turns those sends into INTERNAL_ERROR) | KEEP as the tolerant ack; optionally drop the "noop M1" log-string archaeology to "noop (no trigger bound)". |
| `test_mongo_mcp_wiring.py` filename | `server/tests/` | Named for a dead system (Mongo-MCP); actually tests the Persistence-singleton startup wiring | Rename to `test_persistence_wiring.py` in a test-hygiene pass (touches the offline-suite lastfailed cache; out of this wave's cut scope). |
| `~88 TiTiler + ~40 Gemini residual prose refs` | `emission/`, `postprocess/`, `adapter.py` | ADR 0262's own carried-forward flag; largely accurate legacy-IR / legacy-unwrap descriptions | Whole-codebase hygiene pass, not this scope. `adapter.py` Gemini/Vertex is the intentional dormant seam (CLAUDE.md) -- describing it is truthful. |

## Per-file coverage (priority order)

Read end-to-end + judged: `persistence.py`, `main.py`, `server/_core.py`
(persistence-singleton block + confirm-dispatch + case-command + run_server
persistence init; the 10.5k-line file was audited at the persistence/session/
dispatch seams NATE named, NOT line-by-line end-to-end), `server/session.py`,
`telemetry.py`, `server/config.py` (CODE_EXEC_CONFIRM_TIMEOUT retention comment
verified truthful -- constant still borrowed by credential/region/solver gates),
`emission/uri_registry.py` (TiTiler branch), `emission/layer_uri_emit.py`
(s3/TiTiler prose verified accurate to the QGIS-native swap). Test files
re-anchored: `test_persistence.py`, `test_file_persistence.py`,
`test_mongo_mcp_wiring.py`.

NOT exhaustively read this wave (honest scope): the full 10.5k lines of
`_core.py` beyond the named seams; `emission/pipeline_emitter.py`,
`credentials/`, `cases/`, `agent/gates/`, `agent/mesh/`, `agent/adapters/`
(the Gemini prose there is the intentional dormant seam), `tool_catalog_http.py`,
`case_lifecycle.py`, `contracts/src`. The bulk of these carry milestone-tag
archaeology (M1-M4, Part A/B/C, Wave 4.x) annotating LIVE code -- comment-fix
candidates for a continuation notation pass, not dead-code leads.

## Consequence

The smell-to-code hypothesis, applied to the highest-priority persistence/
session/dispatch seams, resolves overwhelmingly to LIVE code: waves 0262/0266/
0267 already cut the dead cloud seams and severed consumers, so the surviving
smells annotate live behavior (comment fixes) or wire/persisted contract state
(flags). The one genuine dead-code find in scope -- the `test_live_mcp_write_then_read`
test + its `trid3nt_server.mcp` import + the MCP-stdio enable tests -- was
already removed in the working tree. Net new dead code cut THIS session: 0 LOC
(the None-fallback NATE pointed at is live); comment falsehoods corrected: 5;
dead env-var references removed from tests: 4.
