# ADR 0277 -- fragmentation: the agent/ namespace dies

Status: accepted (2026-08-16). Extends the 0261-0276 layout chain.

## Context

`agent/` nested the whole agent-loop subsystem one level below
`trid3nt_server/`, so NATE's Big-3 (data / mesh / workflows) and the gates and
adapters seams read as second-class. The 0261-0265 series had already extracted
the server monolith's flat siblings around a facade; this wave promotes the
agent subsystem to top-level peers and groups the persistence modules.

## Decision

Dissolve `trid3nt_server/agent/` -- its children become top-level packages:

| New folder | Was | Files | LOC |
|---|---|---|---|
| `data/` | `agent/tools/` (+ `agent/data/` assets, `agent/tool_arg_normalizer.py`) | 364 | 75,924 |
| `workflows/` | `agent/workflows/` | 281 | 104,086 |
| `mesh/` | `agent/mesh/` | 14 | 8,476 |
| `gates/` | `agent/gates/` | 16 | 4,705 |
| `adapters/` | `agent/adapters/` | 5 | 5,573 |
| `persistence/` | `persistence.py` + `case_lifecycle.py` (grouped) | 3 | 1,358 |
| `emission/` | unchanged | 4 | 4,302 |
| `server/` | unchanged (see deferral) | 12 | 12,021 |

`agent/cases/malpasset_obs.py` -> `cases/` (joins the existing platform
`cases/` package). `agent/__init__.py` deleted; the `agent/` tree is gone
(`git ls-files trid3nt_server/agent | wc -l` == 0).

## How it executed

- Escaping relative imports (115, in `tools/__init__.py`'s workflow-registration
  block + `gates/cards/*` deferred cross-package imports) were converted to
  absolute FIRST -- after the move these are peer-top-level imports, so absolute
  is the correct form.
- A single ordered rewrite pass over every `.py` (+ two experiment JSON inputs)
  mapped dotted AND slash forms: `trid3nt_server.agent.tools -> .data`,
  `.workflows/.adapters/.gates/.mesh` un-nested, `.tool_arg_normalizer -> .data.tool_arg_normalizer`,
  `.cases -> .cases`; relative `.agent.X` / `..agent.X` forms handled per depth.
- `main.py`'s two no-alias `from .agent import tools` sites kept the body name
  via `from . import data as tools`.

## Reference-sweep (the parts a module-path grep misses)

- `parents[N]` depths: every moved file lost exactly one ancestor (`agent`), so
  27 `Path(__file__).resolve().parents[N]` / literal-`"agent"`/`"tools"` path
  lookups that resolved to `agent` or above were decremented by one (repo-root
  and package-root anchors in `workflows/*/run_*.py`, `mesh/modflow_package_validation`,
  `data/search/*`, `tool_catalog_http`). Within-subtree `parents[N]` (e.g.
  `data/fetchers/_router/spec.py:44`) were left unchanged.
- Test-side source-inspection anchors (`_WORKFLOWS`, `_FLOOD`, the
  `no_markdown` ALLOWLIST + SCAN_DIRS) re-anchored off `"agent"/"workflows"` and
  `"agent/tools"`.
- The deleted `agent/data/` assets (`tool_query_corpus.yaml`,
  `duckdb_spatial_functions.json`) were restored into `data/`.
- Dead `agent.lessons` import (removed paradigm) stripped from the telemac probe.
- `grep 'trid3nt_server.agent' == 0` repo-wide.

## Cycles / splits

- No new import cycle: `case_lifecycle -> persistence` is one-directional;
  `data`/`mesh`/`workflows`/`gates` are peers with `gates/cards` reaching the
  others via deferred (function-local) imports, and composers reaching `data`/`mesh`.
- No per-module split needed: no moved region crossed the 1,500-line budget as a
  NEW file (the pre-existing `_core.py` and large composers were moved verbatim,
  not created).

## Design pages born

`docs/design/{data,mesh,workflows,gates,adapters,emission,persistence,server}.md`.

## Deferred (honest partial; ADR 0265 blockers stand)

The `server/` internal target -- `session/ turn/ dispatch/ protocol/` subfolders,
the extraction of `_core`'s ~10k-line turn-engine + WS-loop bodies into them,
the GateSpec engine -> `gates/`, `tool_catalog_http` -> `server/protocol/`, and
the adapter model-discovery routes -> `adapters/` -- was NOT executed. `_core`
still carries the two coupled regions ADR 0265 documented (the shared
`_gate_wait_timeout` source-inspection seam + the driver<->helper import cycle);
external code reaches server internals only through the facade, so the internal
structure blocks no one. Cracking `_core` under live-daemon risk at the tail of
this wave was judged worse than an honest partial -- it warrants its own gated
wave (ws_smoke + flood canary through a restarted daemon).

## Verification

Offline four-slice at documented baseline (4 fetch_resolution + 2 river_dye,
all else green). Registry 254, contracts 721, retrieval discriminates,
catalog page renders. Live: daemon restart (254 tools), ws_smoke all_passed,
SFINCS flood canary status=ok (depth COG + envelope). Plugin suite untouched
(90 passed; 1 pre-existing qgis-interpreter-absent harness skip-that-ran).
