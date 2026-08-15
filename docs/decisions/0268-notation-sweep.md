# ADR 0268 - server-refactor waves 8-9: notation sweep (docstrings/comments)

Status: LANDED (both waves complete). Wave 8: 2026-08-15. Wave 9: 2026-08-15.

## Context

NATE's hard rule: docstrings/comments should be focused on the constraint
itself, not carry ADR/FR/NFR/Appendix/job-id/Decision-letter citations or
wave/date archaeology. Those notations belong in commit messages, not in
source. A repo-wide measurement found ~1,404 lines across ~296 Python files
under `server/src/trid3nt_server/` matching the notation regex:

```
ADR[- ]?0[0-9]|FR-[A-Z]+-[0-9]|NFR-|Appendix [A-Z]|job[- ]?0[0-9]{3}|Decision [A-Z]\b
```

(Actual measured baseline at sweep start: 1421 matching lines across 296 `.py`
files under `server/src/trid3nt_server/`, plus 125 lines in `.yaml`/`.md`
sidecars that are out of scope per the kickoff -- Python source
docstrings/comments only.)

## What landed this wave

Swept every subsystem under `server/src/trid3nt_server/` EXCEPT the two
largest trees, `agent/tools/` (141 files, ~700 matching lines) and
`agent/workflows/` (106 files, ~400 matching lines), which are scoped to a
continuation wave (see below).

Before/after match counts, by subsystem (Python files only):

| Subsystem | Before | After |
|---|---:|---:|
| `server/` (incl. `_core.py`) | 121 | 0 |
| `agent/mesh/` | 27 | 0 |
| `agent/gates/` (incl. `cards/`) | 15 | 0 |
| `emission/` | 24 | 1 (see exception below) |
| `credentials/` | 5 | 0 |
| `cases/` | 5 | 0 |
| `sandbox/` | 3 | 0 |
| `agent/adapters/adapter.py` | 6 | 0 |
| `agent/categories.py` | 17 | 0 |
| `agent/tool_arg_normalizer.py` | 6 | 0 |
| `main.py` | ~35 | 0 |
| `persistence.py` | 11 | 0 |
| `case_lifecycle.py` | 1 | 0 |
| `telemetry.py` | 1 | 0 |
| `tool_catalog_http.py` | 3 | 0 |
| `agent/tools/cache.py` (touched; rest of `agent/tools/` deferred) | 19 | 0 |
| **Deferred: `agent/tools/` (rest)** | ~141 files / ~700 lines | unchanged |
| **Deferred: `agent/workflows/`** | 106 files / ~400 lines | unchanged |

One line is a deliberate exception: `emission/layer_uri_emit.py:92` embeds
`(job-0254 guardrail; see Decision 11.)` inside a `logger.warning(...)`
message string -- a runtime log payload, excluded per the kickoff's "string
literals that are runtime behavior" carve-out. Left as-is.

**48 files touched, ~120 individual reword/delete edits.** Split:

- **Reword** (majority, ~85 edits): citation stripped, constraint kept --
  e.g. `"ADR 0014: persist the session registry's short-handle map"` ->
  `"Persist the session registry's short-handle map"`; `"FR-DC-6
  short-circuit"` -> `"Uncacheable-tools short-circuit"`; `"Appendix A.7
  replace-not-reconcile"` -> `"replace-not-reconcile"` (the mechanism, stated
  plainly, without the citation).
- **Delete** (~35 edits): pure provenance/history with no surviving
  constraint -- `"server-refactor wave 4, ADR 0264"` module-docstring
  parentheticals, `"Door dissolution (ADR 0094)"` clauses once the sentence
  already states what's true now, `"V&V wave (ADR 0021, lane C)"` labels,
  `job-0277`/`job-0203` inline tags on log/bind calls.
- One larger reword: `agent/mesh/hecras_geometry.py`'s ~30-line module
  docstring narrating the HEC-RAS 2D-geometry-write unblock kept every
  engineering fact (dWSE deltas, correlation coefficients, the
  Windows-DLL-vs-Linux-headless finding) but dropped every `ADR 01xx` label
  and the `2026-08-03 M3 finding` date-archaeology framing.

No pinned tests reference the exact citation strings changed (checked via
`grep -rn "ADR 0014"` etc. against `server/tests/` for direct string
assertions and `inspect.getsource` source-pinning patterns -- none found).
Zero code changes; docstrings/comments only.

## Deferred (at wave-8 close) -- landed in wave 9 below: `agent/tools/` and `agent/workflows/`

These two trees hold the bulk of the remaining ~1,100 matching lines and are
the LLM-facing tool-docstring surface (routing/constraint content the
Bedrock 1000-char truncation makes precious). They deserve their own
focused wave with per-tool docstring review rather than a rushed pass
appended to this one. Queued as the wave-9 kickoff. Exact remaining counts
(measured at this wave's close):

- `agent/tools/`: 141 files, ~700 matching lines (of which `cache.py`, the
  one file touched this wave, is now clean).
- `agent/workflows/`: 106 files, ~400 matching lines.

## Gates run

- `pytest server/tests -k fetch_resolution`: 4 failed / 19 passed (matches
  the documented offline baseline exactly).
- `pytest server/tests -k river_dye`: 2 failed / 52 passed (the `[p-r]`
  slice named in the kickoff; matches baseline).
- `pytest contracts/tests`: 8 failed / 702 passed -- pre-existing, unrelated
  to this sweep (contracts/ package untouched; confirmed via `git status`).
- Tool-registry import (`main._import_tools_registry()`): 256 tools
  registered, zero import errors.
- `retrieve_visible_tools(prompt, None, 8)` spot-check on 5 diverse prompts:
  no crash, 255/256 tools visible each time (expected fail-open behavior
  with `allowed_set=None`; unaffected by this wave since no tool docstrings
  were touched).
- Daemon restart (`make stop && make up`): clean boot, no import/registration
  errors in `logs/agent.log`.
- `scripts/ws_smoke.py`: `all_passed=True`.

Flood canary not run (comments-only change, explicitly optional per kickoff;
skipped this pass in favor of closing out the gate list above).


## Wave 9: `agent/tools/` + `agent/workflows/` (the deferred LLM-facing surface)

Status: LANDED. Date: 2026-08-15.

Swept the two trees wave 8 deferred: `agent/tools/` (141 files) and
`agent/workflows/` (106 files) -- the LLM-facing tool-docstring surface
(Bedrock truncates a tool docstring to 1000 chars, so routing content is
precious) plus composer docstrings feeding gates/cards.

### Method

Mechanical-then-manual: a regex sweep script normalized the common shapes
(`(ADR 0153, description)` -> `(description)`, `(ADR 0225)` -> deleted,
`, ADR 0225` -> deleted, `ADR 0225:` -> deleted, bare `ADR 0225 text` ->
`text`, `(ADR 0089 - description)` -> `(description)`, slash-joined
`(X / ADR 0225)` -> `(X)`) across every matched file, per the wave-8
reword-vs-delete style: citation stripped, constraint kept whenever the
citation gestured at a real constraint; pure provenance/history clauses
(fold lineage, wave/date archaeology, standalone "NEW capability (ADR
NNNN)" framings) deleted outright. Every mechanical edit was then diffed
against a pre-sweep backup and hand-reviewed for grammar breaks (dangling
`per.`, doubled periods, orphaned parens, line-wrapped citations the
per-line regex couldn't see -- e.g. `(ADR\n0068)` split across a wrapped
comment) and hand-fixed. One test-pinned runtime string
(`agent/tools/__init__.py`'s `ToolRegistrationError` message, asserted
verbatim by `server/tests/test_tools_registry.py::test_register_tool_...
duplicate`) was caught by the mechanical pass and reverted -- runtime
error-message strings are out of scope per the kickoff carve-out.

### Before/after counts (Python files only, matching the wave-8 regex)

| Subsystem | Files touched | Before (lines) | After |
|---|---:|---:|---:|
| `agent/tools/` top-level (`__init__.py`, `resolution_declared.py`) | 2 | ~107 | 0 |
| `agent/tools/publish_layer/` | 1 | 5 | 0 |
| `agent/tools/display/` | 0 | 1 | 0 |
| `agent/tools/meta/` | 5 | 18 | 0 |
| `agent/tools/simulation/` | 10 | 47 | 0 |
| `agent/tools/search/` | 9 | 35 | 0 |
| `agent/tools/processing/` | 34 | 117 | 0 |
| `agent/tools/fetchers/` | 79 | 293 | 1 (runtime string, see below) |
| **`agent/tools/` total** | **~139** | **~623** | **1** |
| `agent/workflows/__init__.py` | 1 | -- | 0 |
| `agent/workflows/openquake/` | 4 | 13 | 0 |
| `agent/workflows/sfincs/` | 7 | 61 | 0 |
| `agent/workflows/hecras/` | 6 | 29 | 0 |
| `agent/workflows/elmfire/` | 7 | 10 | 0 |
| `agent/workflows/geoclaw/` | 7 | 37 | 0 |
| `agent/workflows/schism/` | 10 | 65 | 0 |
| `agent/workflows/telemac/` | 17 | 91 | 0 |
| `agent/workflows/swan/` | 2 | 5 | 0 |
| `agent/workflows/mesh/` | 6 | 15 | 0 |
| `agent/workflows/modflow/` | 17 | 92 | 0 |
| `agent/workflows/pelicun/` | 1 | 7 | 0 |
| `agent/workflows/swmm/` | 16 | 35 | 0 |
| `agent/workflows/landlab/` | 5 | 12 | 0 |
| **`agent/workflows/` total** | **106** | **478** | **0** |

**245 files touched, ~1,101 matching lines resolved.** Two deliberate
runtime-string exceptions remain repo-wide (the same carve-out wave 8
used for `emission/layer_uri_emit.py`): `agent/tools/__init__.py`'s
`ToolRegistrationError` message (`"...rejected at import time per
FR-CE-8."`, pinned by `test_tools_registry.py`) and
`agent/tools/fetchers/_router/hooks/overpass.py`'s river-source
`ToolInputError` message (`"...removed in ADR 0074)."`) -- both are
error text a caller/LLM sees at runtime, not docstrings/comments, so they
stay per the kickoff's string-literal carve-out.

**Repo-wide notation regex over `server/src` at close: 3 matches, all
three the runtime-string exceptions above (the wave-8
`layer_uri_emit.py` one plus these two).** Zero outside runtime strings.

### Retrieval gate (THE gate for this wave)

`retrieve_visible_tools`'s discover-index top-8 (`_discover_topk(prompt,
8)`, floor-set excluded so the comparison isolates the ranking) on 12
diverse natural prompts spanning flood, fire, seismic, groundwater,
waves, dredging, culvert, capture-zone, lifelines, surge, LID, and
terrain -- run BEFORE the sweep and AFTER, byte-for-byte identical
top-8 sets in all 12 slices (no reordering measured; set equality
checked). E.g.:

- flood (before == after): `geoclaw_storm_surge, schism_pahm_surge,
  fetch_storm_tracks, fetch_gtsm_tide_surge, swmm_urban_flood,
  coastal_tidal_surge, fetch_storm_events_db, sfincs_flood`
- groundwater (before == after): `fetch_usgs_groundwater_levels,
  modflow_wellhead_protection, modflow_asr, modflow_managed_recharge,
  modflow_regional_water_budget, modflow_mine_dewatering,
  modflow_sustainable_yield, modflow_capture_zone`
- terrain (before == after): `hecras_riverine_flood, generate_mesh,
  landlab_dem_conditioning, hecras_flood_2d,
  landlab_green_ampt_overland_flow, delineate_watershed,
  landlab_flow_accumulation, fetch_gcn250_curve_numbers`

All 12 slices matched with zero missing tools (full before/after JSON
captured during the run). No tool-description content that feeds the
discover index's BM25/dense/name-substring corpus was thinned -- routing
blocks, Literal enums, and arg semantics were preserved word-for-word;
only citation tokens were stripped.

### Pinned-test updates

- `server/tests/test_tools_registry.py::test_register_tool_rejects_...`
  asserts `"FR-CE-8" in str(exc.value)` against the
  `ToolRegistrationError` message raised by
  `agent/tools/__init__.py::register_tool`'s duplicate-registration
  guard. The mechanical sweep initially rewrote this runtime string;
  reverted to keep the citation (it's a runtime error message, out of
  scope per the kickoff carve-out) -- no test file edit needed once
  reverted.
- No other pinned test asserted exact docstring/comment text containing
  the removed notation (checked via grep across `server/tests/` for
  `__doc__`/`.description` assertions against ADR/FR/NFR/Appendix/job
  strings; the `test_catalog_surfacing.py` hits are the test's own
  inline comments annotating registry-size assertions, not pins on
  source docstring content).

### Gates run (foreground, sequential)

- Retrieval gate: 12/12 prompts, top-8 set identical before/after (see
  above).
- `pytest server/tests -k fetch_resolution`: 4 failed / 19 passed --
  matches the documented offline baseline exactly (same 4 tests: 2
  `fetch_dem`/`fetch_topobathy` granularity-block + 2
  compute-label-deployment-aware).
- `pytest server/tests -k river_dye`: 2 failed / 52 passed -- matches
  baseline exactly (`test_tool_rejects_invalid_bbox`,
  `test_tool_rejects_both_location_and_bbox`).
- `pytest contracts/tests`: 710 passed, 0 failed (the wave-8 report's "8
  failed / 702 passed" baseline was resolved by intervening chops; this
  wave's sweep touched zero files under `contracts/`, confirmed via `git
  status`).
- Tool-registry import (`main._import_tools_registry()` +
  `TOOL_REGISTRY`): 256 tools registered, zero import errors.
- Daemon restart (`make stop && make up`): clean boot, `logs/agent.log`
  shows `tool registry loaded: 256 tool(s)` with zero import/registration
  errors, no `logs/agent_boot.log` crash entry.
- `scripts/ws_smoke.py`: `all_passed=True`.
- `python -m py_compile` over every file under `server/src/trid3nt_server`
  (agent + emission + rest): zero syntax errors.

Flood canary not run (docstrings/comments only, no runtime code paths
touched; explicitly optional per the wave-8 precedent for a
comments-only change).
