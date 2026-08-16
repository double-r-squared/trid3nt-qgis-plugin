# ADR 0272 - repo unnesting: server/src -> src (standard src-layout)

Status: LANDED (2026-08-16). NATE order: unnest the server tree one layer so
`server/src/trid3nt_server/` becomes repo-root `src/trid3nt_server/`, a standard
Python src-layout. LAYOUT-ONLY: the package name `trid3nt_server` and every
import string are IDENTICAL; only the on-disk directory prefix changed. One
atomic wave. Waves 1-11 landed at ADR 0261-0270; provider-neutral seam at 0271.
Date: 2026-08-16

## Context

`server/` was a nested subtree (`server/src/trid3nt_server`, `server/tests`,
`server/pyproject.toml`, `server/wheels`, `server/Dockerfile`) inheriting the
cloud-era assumption that the agent was one deployable among many. On the
QGIS-only, single-user local product the extra directory layer earns nothing and
forces `../` gymnastics in every script and doc. This wave collapses it to the
conventional root layout and moves the tests to the root `tests/` at the same
time (the reference count was tractable).

Because BOTH the package (`server/src/trid3nt_server` -> `src/trid3nt_server`)
and the tests (`server/tests` -> `tests`) moved one directory SHALLOWER, the
load-bearing risk was not the imports (unchanged) but every **depth-dependent
path computation** (`Path(__file__).resolve().parents[N]`) that walks up to a
repo-root resource -- each was off by one after the move.

## Blast-radius inventory (taken BEFORE touching anything)

**Packaging / editable install**
- `server/pyproject.toml` declared the package via `[tool.setuptools.packages.find]
  where = ["src"]` (relative to the pyproject location) -- moving the pyproject
  to the repo root keeps `where=["src"]` correct because `src/` is now at root.
- Editable install mechanism: a plain path `.pth`
  (`venvs/agent/.../__editable__.trid3nt_server-0.1.0.pth`) pointing at
  `server/src`. HARD LESSON (worker-image-staleness / stale-editable-path):
  imports resolve from the OLD path until the package is REINSTALLED. Reinstall
  was mandatory, not optional. (`contracts` editable install at `contracts/src`
  is unaffected.)

**Build / run tooling**
- `Makefile` `venv` target: `--find-links $(REPO_ROOT)/server/wheels` and
  `-e $(REPO_ROOT)/server`.
- `server/Dockerfile` (agent image, DORMANT -- cloud decommissioned, ADR
  `project_aws_cloud_decommissioned`): `COPY server/{pyproject.toml,README.md,src,
  wheels}` + `pip install /build/server`. NO worker image copies server paths
  (verified: `services/workers/*` Dockerfiles COPY `services/workers` only), so
  NO image rebuild was required by this move.
- `scripts/*.py` (~40): `PYTHONPATH=server/src:contracts/src` in run-doc headers +
  `sys.path.insert(0, "server/src")`, plus a SEPARATED-COMPONENT variant
  (`str(REPO / "server" / "src")`, `os.path.join(..., "..", "server", "src")`) in
  ~17 sandbox/proof scripts that a `server/src` substring grep does NOT catch.

**Tests**
- `server/tests/` (411 test files) -> `tests/`. Alphabetical four-slice command
  runs over this dir. `server/tests/conftest.py` imports `trid3nt_server.*` by
  name only (no path literal) -- moved unchanged.
- Depth idiom: 18 test files used `parents[2]` for repo root (correct at
  `server/tests/`, overshoots to `/home/nate/Documents` at `tests/`) and one
  (`test_schism_coupled_waves.py`) built `parent / "server" / "src" /
  "trid3nt_server" / ...` from separate components.

**Source (the subtler half)** -- `src/trid3nt_server/**` modules that compute a
repo-root resource (`bin/mf6`, `data/`, `services/workers/`,
`public_data_source_catalog.yaml`) via `parents[N]`: `plugin_repo.py`,
`main.py`, `catalog_common.py`, `living_atlas_common.py`,
`modflow_package_validation.py`, and the engine workflows (openquake, hecras
flood_2d + culvert, elmfire, modflow, swmm, landlab, mesh generate_mesh +
hecras_build). Package-INTERNAL `parents[N]` (e.g. `search_tools` ->
`trid3nt_server/agent/data/...`) are UNCHANGED by the move and were left alone.

**Docs** -- ADRs / DELETION_LEDGER / metrics table / specs / validation reports /
frozen experiment fixtures reference the old paths as HISTORY and were LEFT per
the "do not rewrite history" rule. Only docs giving RUNNABLE current commands
were updated (see below).

## Decisions

1. **Tests move with the package** (standard layout): `server/tests` -> `tests/`.
   The four-slice commands change from `server/tests/test_[X]*.py` to
   `tests/test_[X]*.py`, run from the repo root (they already ran from root).
2. **pyproject.toml -> repo root**, `where=["src"]` unchanged, `readme` now
   resolves to the root `README.md`.
3. **wheels/ -> repo root** (the `pfdf` vendored find-links source).
4. **server/README.md -> docs/design/server-package.md** (preserved; the root
   `README.md` is the package/product readme).
5. **Agent Dockerfile + its .dockerignore -> deploy/agent/** (DORMANT cloud
   blueprint kept as code/docs; COPY paths rewritten to the unnested layout; NOT
   rebuilt -- decommissioned, and the ACTIVE root `.dockerignore` is worker-build
   scoped so the dormant agent build must not share it).
6. **server/.env** (gitignored FIRMS test key, read by NO live path -- the local
   flow loads root `.env.local`) moved to root `.env` with a plain `mv`.
7. **Depth fix rule**: any `parents[N]` targeting a repo-root resource is
   decremented by one; package-internal `parents[N]` are untouched.

## Editable-install reinstall (proof)

    ~/.local/bin/uv pip install --python venvs/agent/bin/python --find-links wheels -e .
      - trid3nt-server==0.1.0 (from file:///home/nate/Documents/trid3nt-local/server)
      + trid3nt-server==0.1.0 (from file:///home/nate/Documents/trid3nt-local)

    venvs/agent/bin/python -c "import trid3nt_server; print(trid3nt_server.__file__)"
    /home/nate/Documents/trid3nt-local/src/trid3nt_server/__init__.py   # was server/src/...

## Reference sweep

- `server/src` / `server/tests` substrings: swept to ZERO across scripts, src,
  tests, services, contracts/src, and the LIVE docs/plugin comment-pointers
  (`qgis-plugin/trid3nt/render/*`, authoring guides, troubleshooting). History
  docs retain them by design.
- Separated-component idiom (`"server" / "src"`, `"server", "src"`): swept to
  zero in scripts + the one schism test.
- Depth decrements: 17 repo-root `parents[N]` lines in `src/` and 18 `parents[2]`
  in `tests/` decremented; internal `parents[N]` left intact.

Two regressions were CAUGHT by the slices and fixed before close:
`test_elmfire_sensitivity` / `test_always_offload_heavy_tools` (elmfire
`parents[6]->[5]` + a stale `git mv`-carried `__pycache__` whose `.pyc`
`co_filename` embedded the old `server/tests` path -- all bytecode caches
cleared), and `test_schism_coupled_waves` (the separated-component corpus path).

## Command-doc updates (the four-slice + install commands are LAW)

- `docs/site/install.md`: `make venv` comment `-e contracts -e server` ->
  `-e contracts -e .`; `server/wheels/` -> `wheels/`.
- `docs/validation/build-contract.md` sec 6 TEST COMMANDS: run from the repo root
  (`cd .../trid3nt-local`), `venvs/agent/bin/python` (no `../`), `server/tests` ->
  `tests`; sec 5.4 `-e server` -> `-e .`.
- `docs/metrics.md` LOC recipe: `cd server/src/trid3nt_server` -> `cd
  src/trid3nt_server`.
- `pyproject.toml` / `deploy/agent/Dockerfile` / `deploy/agent/.dockerignore`
  comments corrected to the unnested paths.

New canonical four-slice (repo root):

    env -u TRID3NT_CACHE_BUCKET venvs/agent/bin/python -m pytest \
      tests/test_[a-e]*.py -p no:cacheprovider --timeout=300 -q      # + [f-o] [p-r] [s-z]

## Gates (all green)

- Four slices (repo root, new paths): [a-e] 1499 passed / 0 failed; [f-o] 6377
  passed / **4 failed** (`test_fetch_resolution_gate` x4); [p-r] 2020 passed /
  **2 failed** (`test_run_river_dye_scenario::test_tool_rejects_{invalid_bbox,
  both_location_and_bbox}`); [s-z] 1414 passed / 0 failed (re-verified after the
  coupled_waves fix). Baseline EXACTLY 4 fetch_resolution + 2 river_dye -- no
  regressions.
- contracts/: 708 passed.
- registry import (offline in-process): 252. Daemon boot: 256 tools; sync-tool
  off-load ARMED, 151 candidates verified emit-free (the source-inspection guard
  runs clean -- proof the stale-pyc path is gone).
- daemon restart (`make stop` + `make up`) + `scripts/ws_smoke.py`:
  all_passed=True.
- flood canary `scripts/run_sfincs_direct.py`: status=ok, real local-docker
  SFINCS solve (22890 active cells, 30 m), depth COG published, envelope
  complete (`model_flood_scenario complete ... layers=1`).
- case-lifecycle: `tests/test_case_lifecycle.py` 4 passed (plus the case-persistence
  suite across [p-r]/[s-z]).
- PLUGIN suite (`cd qgis-plugin && make test`): Ran 392 tests, 2 failures --
  the SAME pre-existing offscreen-Qt harness failures as HEAD (`test_case_bbox`,
  `test_tool_picker`); zero regressions. Its Makefile `PYTHON ?=
  ../venvs/agent/bin/python` was UNCHANGED (the plugin did not move; relative
  depth to `venvs/` is identical). The "matplotlib MISSING (boom)" console line
  is mock test-data printed by `test_install_dependencies.py`, not a failure.

## Consequences

- `git mv` preserved history on all 1524 moved files (rename detection clean);
  114 files carry content edits (175/175 line deltas), server/ dir gone.
- The dormant agent image (`deploy/agent/Dockerfile`) is not built by any live
  path; if the cloud blueprint is ever resumed, its COPY paths are already
  correct for the unnested layout.
- Supersedes ADR 0016 (src-layout) only in DIRECTORY PREFIX; the src-layout
  decision itself stands.
