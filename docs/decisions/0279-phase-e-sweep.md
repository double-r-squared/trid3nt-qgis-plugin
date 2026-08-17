# ADR 0279 -- phase E: standards sweep of workers/plugin/contracts/scripts + the two 0278 relocations

Status: LANDED. Date: 2026-08-16.

## Context

ADRs 0268/0269 swept the server package (notation + smell-to-code). Phase E
applies the same standards to everything else -- `workers/`, `plugin/`,
`contracts/`, `scripts/` -- and executes the two relocations ADR 0278 deferred
for budget (no import cycle blocked them):

- `tool_catalog_http.py` (top-level) into `server/protocol/`.
- the provider model-discovery routes into `adapters/` (the charter's law:
  provider nouns live only in `adapters/`).

## Part 1 -- the two relocations

### 1a. `tool_catalog_http.py` -> `server/protocol/catalog_http.py`

`git mv` + verbatim body; the module's ~20 function-local relative imports
(`from .data`, `.telemetry`, `.adapters`, `. import plugin_repo`, ...) were
rewritten to absolute (the file dropped two levels, so `..`-depth would have
crashed at runtime -- the same move gotcha ADR 0278 named). The one importer
(`server/protocol/loop.py`) now reads `from .catalog_http import
serve_catalog_http`. Prose references in `plugin_repo.py`, `telemetry.py`,
`cases/probe_point.py`, `cases/ingest_user_layer.py`, `server/turn/stream.py`
re-pointed. The logger name + self-referential log prefixes updated to the new
dotted path. Test reference sites (13 files) moved WITH it (law 3): imports
re-pointed to `trid3nt_server.server.protocol.catalog_http` (aliased
`as tool_catalog_http` to keep test bodies verbatim); the string-form
`monkeypatch.setattr("trid3nt_server.tool_catalog_http.X", ...)` targets
rewritten to the new path. `grep trid3nt_server.tool_catalog_http` == 0
(source + tests).

MOVE BUG caught by the live gate (not the unit tests): the corpus resolver used
`Path(__file__).parent / "data"`, which after the two-level descent pointed at
the nonexistent `server/protocol/data` -- the catalog booted with **0** query
entries (was 157). Fixed by anchoring on the package root
(`Path(trid3nt_server.__file__).parent / "data"`, `_package_data_dir()`),
robust to future moves. Post-fix: 157 corpus entries, 157/254 tools carry
`sample_queries`.

### 1b. Model-discovery routes -> `adapters/model_discovery.py`

Moved out of `catalog_http` (the named `_ollama_tags_url`,
`_filter_openrouter_models`, `_fetch_openrouter_models`) plus their tight
provider-touching cluster (`_fetch_local_models`, `_base_url_host`,
`_local_models_route_enabled`, `_LocalModelsUpstreamError`, the
`_OPENROUTER_MODELS_*` cache) and `_ollama_root` (out of
`gates/context_budget`). The catalog route handler now calls
`model_discovery._fetch_local_models` / `._local_models_route_enabled` /
`._base_url_host` via a single `from trid3nt_server.adapters import
model_discovery` import; `context_budget` imports `_ollama_root` from the same
home. Provider nouns (`openrouter.ai`, Ollama, `model_provider() == "openai"`)
now appear only under `adapters/`. Test reference sites moved: the direct
provider-function tests import from `adapters.model_discovery`; the route-dispatch
monkeypatch target moved from `tool_catalog_http` to `model_discovery`.

### Relocation proofs

- Import: all three modules import clean; `context_budget._ollama_root(...)` and
  `catalog_http.model_discovery is adapters.model_discovery` verified.
- Unit: the 12 relocation-affected test files (164 tests incl.
  provider-config / local-models / case-list / plugin-repo / probe / ingest /
  building-detail / telemetry / model-selector / recall shadow) all pass;
  `test_context_budget` 43 pass.
- LIVE (restarted daemon, provider=openai on this box):
  `GET /catalog` -> HTTP 200, 895 KB, `<title>TRID3NT tool catalog</title>`,
  254 tools; `GET /api/tool-catalog` -> 254 tools, 157 with sample_queries;
  `GET /api/local-models` -> HTTP 200 served through the relocated
  `model_discovery._fetch_local_models` (OpenRouter free/tool-capable list).
  Boot log: `trid3nt_server.server.protocol.catalog_http tool-catalog HTTP
  server listening ... port=8766`, `loaded 157 tool query entries`, no import
  errors.

## Part 2 -- the standards sweep

Method: the ADR 0268/0269 waves-8-10 method, per area. The mechanical notation
sweep strips the UNAMBIGUOUS citation tokens (`ADR NNNN`, `FR-XX-N`, `NFR-XX-N`,
`job-NNNN`, `Appendix X`, `Decision X`, incl. their slash-joined list forms) from
comments+docstrings, preserving the constraint text. Runtime strings are exempt
via a guard that skips lines raising/logging OR carrying an f-string / `print(` /
plot `title=`/`label=` (proof-driver output in `workers/`+`scripts/` routinely
embeds citations in those strings). Every mechanical edit was diffed and
grammar-cleaned (dangling dash/slash/colon separators, emptied inline `#`
markers); byte-compile + the area's suite gated each area.

Milestone tags (`M1`-`M4`) were EXCLUDED from mechanical handling and left to the
semantic pass: `M2` in the SCHISM tidal fixtures is the principal-lunar tidal
CONSTITUENT, not a milestone, and `M8.2`/`M9.0` in scripts are earthquake
magnitudes -- a blind strip corrupts them.

### Before/after notation counts (Python source, comments+docstrings)

| Area | Before (matching lines) | After -- outside runtime strings | Lines swept |
|---|---:|---:|---:|
| `contracts/` | 489 | 0 | 477 + 3 hand |
| `plugin/` | 91 | 0 | 90 + 2 hand |
| `workers/` | 445 | ~11 (all runtime strings) | 395 + 3 hand |
| `scripts/` | 260 | ~14 (all runtime strings) | 241 + hand |

The `workers/`+`scripts/` residual is entirely inside runtime f-strings /
`print` / plot labels (proof-driver output), exempt per the kickoff carve-out.

### Semantic pass (worst files, budget-bound)

grep-led (`gemini|vertex|atlas|dynamo|batch|GONE|deleted|will be|lands when`) over
the four areas. Result: the swept areas are overwhelmingly free of live
dead-system tombstones -- most `atlas`/`vertex`/`batch` hits are DOMAIN terms
(NOAA Atlas 14 rainfall, mesh/graph `vertex`, a `batch` of cells), not the dead
cloud nouns. Falsehoods corrected in-sweep:

- `contracts/collections.py`: `"""MongoDB collection schemas (SRS, Decision F/L)`
  -> `"""Document-store collection schemas.` (Atlas/Mongo decommissioned; the
  file backend is the only substrate -- matches ADR 0269's `MCPClientProtocol`
  finding).
- `workers/modflow/{__init__,entrypoint}.py`: `Sprint-13 / MOD-1 / FR-CE-1/2/3.`
  sprint-archaeology sentence-leads deleted.

### Cut / flag table

| Item | Area | Verdict | Action |
|---|---|---|---|
| `_ollama_root` duplicate in `gates/context_budget` | gates | LIVE, wrong home | MOVED to `adapters/model_discovery` (dead def removed from context_budget) |
| Worker entrypoint object-store docstrings (`Cloud Run Job`, `AWS Batch`, `GCS`, `google-cloud-storage`) | workers | LIVE code (scheme-aware `s3://`+`gs://`), STALE orchestration nouns | FLAG (see punch list) -- the dual-scheme code is real; the "Cloud Run / AWS Batch" framing is stale under local-docker-only. Not rewritten this pass (risk of mischaracterizing traced-but-not-fully-audited code). |
| `plugin/ui/charts.py` `# (NATE chart-chrome feedback).` lone attribution | plugin | pre-existing attribution, no constraint | FLAG (low value; not a break I introduced) |

No dead CODE was cut in the swept areas (the smells annotate live behavior or
already-scheme-aware workers); the one relocation-adjacent dead def
(`context_budget._ollama_root`) was removed as part of 1b.

## Gates run (foreground/background, sequential)

- Registry import: `TOOL_REGISTRY` == 254, zero import errors.
- Contracts suite: 721 passed.
- Relocation-affected units: 164 passed; `test_context_budget` 43; catalog
  corpus (`test_catalog_tools` + `test_catalog_surfacing`) 41 passed (locks the
  corpus-path fix).
- Daemon restart: clean boot, 254 tools, catalog listening on 8766 from
  `server.protocol.catalog_http`, 157 corpus entries, no import errors.
- `scripts/ws_smoke.py`: `all_passed=True` (chat + geocode tool dispatch).
- Catalog curl from the NEW protocol/ home: `/catalog` 200, `/api/tool-catalog`
  254 tools, `/api/local-models` 200 (relocated model-discovery).
- Offline four-slice: `[a-e]` 1468 passed / 5 skipped / 0 failed. (`[f-o]`,
  `[p-r]`, `[s-z]` completing; documented baseline is 4 fetch_resolution in
  `[f-o]` + 2 river_dye in `[p-r]`.)
- Byte-compile: every `.py` under all four swept areas compiles.

## Consequence

The server-package standards now hold across the repo's other four Python trees:
notation is zero outside runtime strings in `contracts/`+`plugin/` and
runtime-exempt-only in `workers/`+`scripts/`. The `_core` deferrals are closed --
`catalog_http` lives in `protocol/`, provider discovery lives in `adapters/`, and
the corpus-path move bug (invisible to the offline suite, caught only by the live
boot) is fixed and pinned.
