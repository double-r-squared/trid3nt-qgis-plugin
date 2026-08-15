# ADR 0262 - server-refactor wave 2: cloud-shaped-seam chop (aws-batch / TiTiler / Vertex / DynamoDB)

Status: LANDED (2026-08-14). Wave 2 of the server-refactor series (ADR 0261 =
wave 1). NATE approved all four chops registered on DELETION_LEDGER row 232.
Strictly behavior-preserving for every LIVE path (local-docker solves, QGIS
styling, bedrock/openai/scripted LLM, file/mongo persistence). Delete-don't-
disable; comments = constraints, never history (no tombstone comments).
Date: 2026-08-14
Supersedes-nothing (continues ADR 0261; recon map at
`docs/design/server-refactor-recon-2026-08-14.md`).

## Context

The wave-1 recon flagged four "live-but-cloud-shaped" seams deferred out of the
pure package-skeleton wave. Verification this wave found all four were already
substantially removed by the prior local-only slim (2026-07); the residue was
predominantly stale VOCABULARY (misleading identifiers + history-comments) plus
a handful of genuinely-dead branches and two live seams carrying cloud-era
names. Each chop below leads with its verification evidence.

## Decision + per-chop evidence

### Chop 1 - AWS-batch backend switch

VERIFY: nothing selects aws-batch anywhere. `solver_backend()`
(`agent/tools/simulation/solver/solver.py`) is hardwired to `local-docker`;
`TRID3NT_SOLVER_BACKEND` is read NOWHERE for dispatch (grep across server/src).
The env-reading switch was already gone.

- `solve_progress_vcpus`: the post-`local-docker` cloud tail was UNREACHABLE
  (the leading `if solver_backend() == SOLVER_BACKEND_LOCAL_DOCKER` always
  returns). Collapsed to `return os.cpu_count()`, signature kept for call-site
  stability. Byte-identical: `test_solve_progress_vcpus` already asserts host
  CPU for every input (incl `TRID3NT_SOLVER_BACKEND=aws-batch`).
- `AWS_BATCH_COMPUTE_CLASS_SIZING` -> `COMPUTE_CLASS_SIZING` (RENAME, not
  delete): the table is LIVE-consumed by the solver-confirm card sizing +
  `solve_progress_vcpus`; only the cloud-era name was stale. Updated all sites
  (solver.py, gates/cards/solver_confirm.py, test_select_compute_class.py,
  test_solve_progress_vcpus.py).
- `solver_backend()` + `SOLVER_BACKEND_LOCAL_DOCKER` KEPT: a live predicate seam
  read by `credentials/auth_handshake._is_local_single_user_mode` (a
  load-bearing auth path) and solver_confirm; deleting it is not a minimal
  restructure. Swept the batch/aws-batch/Cloud-Workflows history vocabulary from
  solver.py docstrings/comments + the honest unsupported-backend error message.
- LOC: ~20 net removed in solver.py (dead-branch collapse + comment trims).

### Chop 2 - TiTiler style fallbacks

VERIFY: the QGIS plugin never dials/constructs a TiTiler tile URL - it only
UNWRAPS legacy tile-templates from OLD persisted cases for backward-compat
(`qgis-plugin/trid3nt/render/layers.py`, `net/trid3nt_client.py`). The server no
longer serves TiTiler URLs (`publish_layer` emits the raw `s3://` COG the plugin
reads via `/vsicurl/`). No dead TiTiler-specific fallback branch exists: the
empty-preset flood-depth default and the p2/p98 percentile fallback are LIVE
QGIS styling.

- RENAMED the live styling seam (vocabulary, values byte-identical):
  `_resolve_titiler_style_params` -> `_resolve_qgis_style_params`,
  `_TITILER_STYLE_REGISTRY` -> `_QGIS_STYLE_REGISTRY`,
  `_TITILER_SAFE_DEFAULT` -> `_QGIS_STYLE_SAFE_DEFAULT` - 63 sites across 15
  files (publish_layer.py, compute_ndvi, 7 workflow postprocess/urban modules, 6
  tests).
- Swept the TiTiler prose in `_core.py` (7 comment sites) and `publish_layer.py`
  (renderer/consumer refs -> QGIS; kept `rio-tiler` as the style-params string
  FORMAT the plugin parses; reframed the "TiTiler exit" history notes to the
  QGIS-native constraint).
- FLAGGED (out of scope, follow-up): ~88 residual `TiTiler` prose refs in
  `emission/` + solver `postprocess_*` - mostly accurate legacy-tile-template-
  unwrap descriptions; a separate whole-codebase hygiene pass, not this chop.

### Chop 3 - dormant adapter.py Vertex/Gemini path

VERIFY: the raw google-genai / Vertex `generate_content_stream` client path is
ALREADY removed. The provider dispatch in `adapter.stream_events_with_contents`
delegates to scripted/bedrock/openai and raises `UnsupportedModelProviderError`
for anything else - selecting `vertex`/`gemini` yields a clear typed error
naming the supported providers.

- Removed the dead env-reads in `load_settings` (`GOOGLE_CLOUD_PROJECT` /
  `GOOGLE_CLOUD_LOCATION` / `GOOGLE_GENAI_USE_VERTEXAI`); `ModelSettings.model`
  is the only live field (display/telemetry label). `project`/`location`/
  `use_vertex` DEFAULTED (not deleted) - ~8 test files construct them; only
  `.model` is read in production. Reframed the module docstring off "Gemini-only
  containment layer".
- DEPENDENCY: `google-genai` is LOAD-BEARING and KEPT. `google.genai.types` is
  the shared IR (`Content`/`Part`/`FunctionCall`/`FunctionDeclaration`) imported
  by bedrock_adapter, openai_adapter, adapter, fetchers/_router/stratified, and
  gates/context_budget; `server/pyproject.toml` already documents the carve-out.
  No dependency removed.
- FLAGGED: ~40 residual `Gemini` prose refs in adapter.py describe the genai
  IR/schema constraints (largely accurate) + the `_normalize_callable_for_gemini`
  identifier - a follow-up vocabulary pass.

### Chop 4 - DynamoDB residue

VERIFY: no `dynamo_backend.py` module exists (removed in the local-only slim).
`persistence.make_persistence_for_backend` / `resolve_persistence_backend`
already return file unconditionally. `TRID3NT_PERSISTENCE_BACKEND` is read
nowhere for selection and no test sets it.

- Swept every DynamoDB comment in `_core.py` (12 sites) and `persistence.py` to
  file-backend constraints (comments = constraints, not history).
- Added `UnsupportedPersistenceBackendError`: `resolve_persistence_backend`
  now RAISES on any non-`file` `TRID3NT_PERSISTENCE_BACKEND` (was silently
  ignored), and `make_persistence_for_backend` validates before building.
  Byte-identical for the default/unset/`file` path; `main._maybe_bind_dev_
  persistence`'s try/except degrades a stale selection to the M1 None-persistence
  path with a loud log.
- KEPT + FLAGGED: the `expires_at` / ephemeral-Case TTL machinery is LIVE
  persisted data. The file backend does not auto-reap; the numeric marker is
  retained in the persisted record. NOT deleted - the byte-identical-persistence
  hard rule forbids changing persisted output (wire/persisted -> FLAG). A future
  persistence wave may excise it under an explicit persistence-contract decision.
- file + mongo persistence paths unchanged.

## Consequence

- `server/_core.py` = 12,717 lines (was 12,718; -1 net - the chop was comment
  reframes + one persistence-docstring trim, no code moved).
- Registration-neutral: no `@register_tool` / `tools/__init__` / spec change.
  Workflows import = 280 modules; TOOL_REGISTRY = 252 (unchanged from HEAD; the
  "256" in the kickoff was an approximate/stale figure).
- Grace*/GRACE identifiers: none encountered in the touched regions.
- Overall diff: +258 / -291 (net -33 LOC) across 22 files (14 src, 8 test).
- The four cloud-shaped seams are off the ledger (row 232 -> DELETED); the
  remaining TiTiler/Gemini prose sweeps are registered as follow-up hygiene.
