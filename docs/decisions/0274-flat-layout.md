# ADR 0274 - flat layout: src/trid3nt_server -> trid3nt_server, services/workers -> workers

Status: LANDED (2026-08-16). NATE order: collapse the two remaining
single-child nestings. `src/trid3nt_server/` becomes repo-root
`trid3nt_server/` (standard flat layout -- `src/` held only the one package),
and `services/workers/` becomes repo-root `workers/` (the `services/` dir held
only `workers/` and dies). Import name `trid3nt_server` is UNCHANGED; the worker
module namespace `services.workers.*` becomes `workers.*`. One atomic wave.
Supersedes ADR 0272 (which had moved `server/src` -> `src`) in DIRECTORY PREFIX
only. Date: 2026-08-16

## Context

After ADR 0272 the package sat at `src/trid3nt_server` and the workers at
`services/workers`. Both were single-child wrappers earning nothing: `src/`
contained exactly one package, and `services/` contained exactly one member
(`workers`). NATE ruled both away. As in 0272 the load-bearing risk was NOT the
imports (the package import name is unchanged; the worker namespace is a uniform
rename) but every **depth-dependent path computation** (`parents[N]`) that walks
up to a repo-root resource -- each package file moved ONE directory shallower
again (off-by-one, the 0272 class), and each worker file moved ONE directory
shallower (the `services` level above `workers` was removed, so only parents
reaching `services`/repo-root shift; parents landing at `workers` or below are
unchanged).

Worker name ruling: `workers` (NATE offered rename latitude; the orchestrator
recommended keeping `workers` -- accurate for the qgis/mesh/postprocess members
-- and NATE did not veto). No strong reason against was found, so the name
stands.

## Moves

1. `git mv src/trid3nt_server trid3nt_server`; `src/` (and the stale
   `trid3nt_server.egg-info`) removed. 1483 files rename-detected clean across
   both trees.
2. `git mv services/workers workers`; `services/` removed. Untracked generated
   fixture run-dirs (`workers/modflow/fixtures/{csub_smoke/csub_run,
   sfr_smoke/sfr_run}`) rode the filesystem rename with the tracked tree.

## pyproject change (flat-layout find)

`[tool.setuptools.packages.find]` `where = ["src"]` ->
`where = ["."]` + `include = ["trid3nt_server*"]`. The explicit include filter
is REQUIRED for flat layout: bare auto-discovery at repo root would error on the
sibling top-level dirs (`workers/`, `tests/`, `scripts/`, `contracts/`), and
`include` scopes discovery to the one package. `package-data` is keyed by
package name -- unchanged. `readme` still resolves to root `README.md`.

Editable install now materializes as a strict MetaPathFinder
(`__editable__.trid3nt_server-0.1.0.pth` -> `..._finder.install()`) rather than
a plain path `.pth` -- setuptools' editable mode for a filtered flat layout.
`top_level.txt` = `trid3nt_server` only (the sibling dirs are NOT swept into the
wheel). Reinstall was mandatory (the 0272 stale-editable-path lesson):

    ~/.local/bin/uv pip install --python venvs/agent/bin/python \
      --find-links wheels -e contracts -e .
    venvs/agent/bin/python -c "import trid3nt_server; print(trid3nt_server.__file__)"
    /home/nate/Documents/trid3nt-local/trid3nt_server/__init__.py   # was src/trid3nt_server/...

## Reference fixes, per class

- **`services/workers` (slash literal, code)** -> `workers`: 173 code files
  swept (src package, workers tree, tests, scripts, contracts/src comment
  pointers, pyproject, README, Dockerfiles' in-image COPY/WORKDIR paths, the
  root `.dockerignore` whitelist). Residual 0.
- **`services.workers` (dotted module namespace)** -> `workers`: same sweep
  (namespace packages -- no `__init__` at the old `services/`/`workers` level,
  so the import path is a pure rename); Dockerfile `-m services.workers.X` ->
  `-m workers.X`, `from services.workers... import` smokes -> `from workers...`.
- **Separated-component idiom `"services" / "workers"`** (single line) ->
  `"workers"`: collapsed across src workflows (modflow/elmfire/openquake/schism),
  scripts, and tests.
- **Separated-component idiom, MULTI-LINE** (`/ "services"` on its own line
  above `/ "workers"`): the single-line perl missed these; 4 deleted
  (test_land_subsidence, test_stream_depletion, run_landlab `_LANDLAB_RUN_CHAIN`,
  run_swmm `_SWMM_WORKER_RUN_INP`). Caught by the [f-o] slice (6 land_subsidence
  failures, empty fixture path) and fixed.
- **`src/trid3nt_server` (slash literal)** -> `trid3nt_server`: code + LIVE docs
  (authoring guides, install, troubleshooting, metrics recipe, contracts comment
  pointers, proof template). Residual 0 in non-history.
- **Separated-component `"src" / "trid3nt_server"`** -> `"trid3nt_server"`: the
  src analog of the class above; 5 test files (emit_on_fetch, input_layer,
  no_markdown, resolution_declared, schism_coupled_waves). Caught by [a-e] (4
  emit failures, empty `_WORKFLOWS`) and fixed.
- **`PYTHONPATH=src:` / bare-`src` sys.path** (script run-doc headers +
  runtime): `PYTHONPATH=src:` -> `PYTHONPATH=.:`; `REPO / "src"` / `REPO +
  "/src"` / `.parent / "src"` / `"..", "src"` / `insert(0, "src")` -> repo-root
  form. Swept across scripts/ + tests/. Residual 0.

## Depth fixes (the off-by-one)

Rule (unchanged from 0272): a `parents[N]` targeting a repo-root resource
decrements by one; package/worker-INTERNAL `parents[N]` are untouched.

- **Package (src flatten, one shallower)**: 18 lines across 14 files decremented
  -- main.py `[2]->[1]`, plugin_repo.py `[2]->[1]`, modflow_package_validation
  `[4]->[3]` (bin/mf6), catalog_common `[5]->[4]`, living_atlas_common
  `[5]->[4]`, elmfire run_elmfire `[5]->[4]` (+comment), hecras
  culvert/flood_2d `[6]->[5]`, landlab run_landlab `[5]->[4]` x2, mesh
  generate_mesh/hecras_build `[6]->[5]`, modflow run_modflow `[5]->[4]`,
  openquake psha `[6]->[5]` x2, swmm run_swmm `[5]->[4]` x2. The
  `_WORKERS_FRESHTOPO.parents[2]` (relative to the constructed worker path, not
  `__file__`) and the package-internal search_tools/search_spatial_functions
  `parents[3..4]` (target the package root, moved WITH the file) are unchanged.
- **Workers (services removed above workers, one shallower)**: 18 lines --
  every `_*_postprocess` / `_sfincs_build` / engine worker test `parents[3]->[2]`
  (repo root, incl. the two modflow `bin/mf6` tests), telemac
  test_classify_substance `parents[4]->[3]` (+comment), build_sayers_connection
  `_HERE.parents[5]->[4]`, and `workers/conftest.py`
  `.parent.parent.parent -> .parent.parent`. UNCHANGED: hecras2025 freshtopo
  `_HECRAS2025 = _HERE.parents[2..3]` and the two `.parent.parent /
  "hecras{,2025}"` sibling-fixture references and validate_authormesh
  `parents[3]` -- all land at `workers` or an internal subtree, below the
  removed `services` level.

Two false-positive classes deliberately LEFT: `_ROOT / "services" / "agent" /
"src"` (a dead legacy-layout fallback candidate in telemac
test_classify_substance -- inert, the real import is the editable install), and
literal `"src"`/`"services"` filename/extension/category tokens
(sfincs_forcing `_unique(...,"src",...)`, modflow `.src` package extension,
LEHD `"services"` job category).

## Image-law (Docker) survival

The worker Dockerfiles' repo-root-context COPY paths, `/opt/trid3nt/workers/...`
in-image layout, `PYTHONPATH=/opt/trid3nt`, `-m workers.<engine>.entrypoint`
ENTRYPOINT, and in-image import smokes all rode the uniform `services/workers ->
workers` rename. The root `.dockerignore` whitelist became `* / !workers/`. The
build commands in `docs/site/install.md` (`-f workers/<engine>/Dockerfile .`),
`configuration.md`, and `scripts/build_telemac_image.sh` (context =
`$REPO_ROOT/workers/telemac`) updated.

PROOF -- modflow image rebuilt through its build command with absolute paths
(repo-root context, absolute `-f`):

    docker build -t trid3nt-local/modflow:adr0274 \
      -f /home/nate/Documents/trid3nt-local/workers/modflow/Dockerfile \
      /home/nate/Documents/trid3nt-local

The build-time smokes ALL passed in-image -- `entrypoint import OK`,
`modflow_build (offload) substrate import OK`, `modflow_postprocess substrate
import OK` (`from workers._modflow_postprocess` + `from workers._raster_postprocess`),
`gwt_adapter import OK`, mf6 6.7.0 + gridgen 1.0.02 provenance. Boot provenance
(entrypoint override): `from workers.modflow.entrypoint import main; from
workers._raster_postprocess import manifest` resolve, `mf6: 6.7.0` -- proving the
`.dockerignore` whitelist assembled the context, the `COPY workers/...` layers
landed, and the `workers.*` module path resolves under `PYTHONPATH=/opt/trid3nt`.
Image 1.16 GB (unchanged from the pre-move cached layers).

## Command-doc updates (LAW)

- `docs/site/install.md`: three worker build commands `services/workers/... ->
  workers/...`.
- `docs/site/configuration.md`: `TRID3NT_{GEOCLAW,SWAN}_IMAGE` source pointers.
- `docs/metrics.md`: the LIVE folder-view recipe `cd src/trid3nt_server -> cd
  trid3nt_server` (the older top recipe + the dated table rows are history and
  retain `server/src`/`services/workers` by design).
- `docs/authoring/{adding-an-engine,writing-a-tool}.md`,
  `docs/site/troubleshooting.md`: current file-location pointers.
- ADRs / DELETION_LEDGER / validation inventories / specs / frozen experiment
  fixtures / metrics table rows / `docs/reports/*.jsonl` retain the old paths as
  HISTORY (left per the do-not-rewrite-history rule).

## Gates (all green)

- Four slices (repo root, `env -u TRID3NT_CACHE_BUCKET`): [a-e] 1495 passed
  (the 4 `test_emit_on_fetch_equivalence` failures were the `"src" /
  "trid3nt_server"` class -- fixed + re-verified 50 passed); [f-o] 6394 passed /
  **4 failed** (`test_fetch_resolution_gate` x4) after the 6 `test_land_subsidence`
  multi-line-idiom failures were fixed; [p-r] 2020 passed / **2 failed**
  (`test_run_river_dye_scenario` x2); [s-z] 1414 passed. Baseline EXACTLY 4
  fetch_resolution + 2 river_dye -- no regressions.
- contracts/: 721 passed.
- registry import (in-process): 252. Daemon boot: 256 tools; sync-tool off-load
  ARMED, 151 candidates verified emit-free.
- daemon restart (`make stop` + `make up`) + `scripts/ws_smoke.py`:
  all_passed=True (tool call fired + text reply).
- flood canary `scripts/run_sfincs_direct.py`: status=ok, real local-docker
  SFINCS solve, depth COG published, 7 depth frames + peak.
- case-lifecycle: `tests/test_case_lifecycle.py` 4 passed.
- PLUGIN suite (`cd qgis-plugin`, `python -m unittest discover -s tests`): Ran
  392 tests, 2 failures -- the SAME pre-existing offscreen-Qt harness failures as
  0272 (`test_case_bbox`, `test_tool_picker`); zero regressions. `PYTHON ?=
  ../venvs/agent/bin/python` UNCHANGED (the plugin did not move; relative depth
  to `venvs/` identical). The "matplotlib MISSING (boom)" line is mock test-data.
- Worker suites, both conventions from the new `workers/` path: telemac
  (repo root) 104 passed / 1 skipped; geoclaw (repo root) 92 passed / 1 skipped;
  modflow (own dir, `cd workers/modflow`) 218 passed / 15 skipped.
- Worker IMAGE build smoke: modflow rebuilt + boots (above).

## Consequences

- `git mv` preserved history on 1483 moved files; 256 files carry content edits;
  `src/` and `services/` dirs gone.
- All bytecode caches cleared after the moves (the 0272 stale `co_filename`
  lesson -- the `__pycache__` a `git mv` carries embeds the old path).
- Supersedes ADR 0272 in DIRECTORY PREFIX only; the src-/flat-layout progression
  and the depth-fix rule stand.
