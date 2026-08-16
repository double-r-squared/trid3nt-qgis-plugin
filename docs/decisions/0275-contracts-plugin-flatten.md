# ADR 0275 - flatten the last two single-child nestings: contracts/src -> contracts, qgis-plugin/trid3nt -> plugin

Status: LANDED (2026-08-16). NATE order: collapse the two remaining single-child
nestings the 0272/0274 flat-layout campaign left standing.
`contracts/src/trid3nt_contracts/` becomes `contracts/trid3nt_contracts/` (the
`src/` wrapper held exactly one package), and the plugin at
`qgis-plugin/trid3nt/` becomes repo-root `plugin/` (the `qgis-plugin/` wrapper
held one package plus its tests/Makefile). The contracts import name
`trid3nt_contracts` is UNCHANGED. The plugin's INSTALLED name stays `trid3nt`:
the repo dir is `plugin/`, but `install_plugin.sh` rsyncs it into the profile as
`trid3nt/` and every zip path re-roots it under `trid3nt/` (arcname mapping).
Follows the 0274 reference-class method. Date: 2026-08-16

## Context

Two single-child wrappers earned nothing. `contracts/src/` contained exactly one
package (`trid3nt_contracts`); `qgis-plugin/` contained one package (`trid3nt`)
plus its co-located `tests/` + `Makefile`. NATE ruled both away.

- **Contracts** is the 0274 class exactly: the package import name
  (`trid3nt_contracts`) is unchanged, the `src/` prefix is removed, and the
  editable pyproject gains the same explicit flat-layout `find` filter 0274 used
  for the server package. The load-bearing risk is the editable reinstall (the
  stale-`.pth` lesson), not the imports.
- **Plugin** is a NEW class: the on-disk dir renames `trid3nt` -> `plugin`, but
  the package must still LOAD as `trid3nt` inside QGIS. The plugin source uses
  ONLY relative imports (37, zero absolute `trid3nt.` imports), so the source is
  untouched -- when QGIS loads the profile's `trid3nt/` dir, relative imports
  resolve regardless of the repo dir name. What changes is (a) the packaging
  seams that re-root `plugin/` under the shipped name `trid3nt/` and must now
  EXCLUDE the co-located non-shipping siblings, and (b) the TEST harness, which
  imported the package by name.

## Moves

1. `git mv contracts/src/trid3nt_contracts contracts/trid3nt_contracts`;
   `contracts/src/` (and the stale `trid3nt_contracts.egg-info` under it)
   removed. `contracts/tests/` stays put (not single-child).
2. `git mv qgis-plugin plugin`, then `git mv plugin/trid3nt/<members> plugin/`
   (case/ net/ render/ ui/ + icon.svg __init__.py install_dependencies.py
   metadata.txt plugin.py plugin_settings.py) up to `plugin/` root; the empty
   `plugin/trid3nt/` removed. `qgis-plugin/tests`, `qgis-plugin/Makefile`,
   `qgis-plugin/README.md`, `qgis-plugin/LICENSE`, `qgis-plugin/docs`,
   `qgis-plugin/.gitignore` rode the dir rename to `plugin/`. 117 files
   rename-detected. All bytecode caches under `plugin/` + `contracts/` cleared
   after the moves (0272 stale-`co_filename` lesson).

## pyproject change (contracts flat-layout find)

`[tool.setuptools.packages.find]` `where = ["src"]` -> `where = ["."]` +
`include = ["trid3nt_contracts*"]` -- the explicit include filter is REQUIRED at
repo root (bare discovery would error on `schemas/`, `tests/`). `package-data`
keyed by package name -- unchanged. Reinstall was mandatory (0274 lesson):

    ~/.local/bin/uv pip install --python venvs/agent/bin/python \
      --find-links wheels -e contracts
    venvs/agent/bin/python -c "import trid3nt_contracts; print(trid3nt_contracts.__file__)"
    /home/nate/Documents/trid3nt-local/contracts/trid3nt_contracts/__init__.py   # was contracts/src/...

## Plugin packaging: re-root + non-shipping exclude (the heart of this wave)

`plugin/` now co-locates the shipped package (case/ net/ render/ ui/ + the
top-level .py + icon.svg + metadata.txt + LICENSE) WITH the non-shipping
`tests/`, `docs/`, `Makefile`, `README.md`, and build output. Three packaging
seams re-root `plugin/` -> `trid3nt/` and must all drop the non-shipping set:

- **`trid3nt_server/plugin_repo.py`** (the daemon-served zip, single source of
  truth). `_plugin_src_dir` -> `repo_root / "plugin"`. New
  `_NON_SHIPPING_TOPLEVEL = {tests, Makefile, README.md, LICENSE, docs, dist}`
  matched at the plugin-source ROOT only; `_iter_packaged_files` (fresh-zip +
  tree-sha + signature) skips it, and `_build_zip` (deploy PACKAGE copytree)
  uses a new top-level-aware `_staging_ignore` (base cache/hidden/marker
  patterns everywhere + the non-shipping siblings at the root). LICENSE is still
  re-added INSIDE the zip as `trid3nt/LICENSE`, now sourced from
  `plugin/LICENSE`. arcname prefix `f"{PLUGIN_NAME}/..."` unchanged -> the zip
  internal root stays `trid3nt/`.
- **top-level `make plugin-zip`** + **`plugin/Makefile` `zip`/`install`**: rsync
  `plugin/` -> a `trid3nt/`-rooted staging dir with anchored `--exclude '/tests'`
  etc, then `zip -r ... trid3nt`. The plugin/Makefile zip target was reworked
  from `zip -r trid3nt.zip trid3nt` (which assumed a `trid3nt/` subdir) to a
  staging copy (there is no subdir anymore -- the code is at the Makefile's own
  dir). `PYTHON ?= ../venvs/agent/bin/python` UNCHANGED (`plugin/` is the same
  depth under repo root as `qgis-plugin/` was).
- **`scripts/install_plugin.sh`**: `SRC=plugin/`, `DST=.../plugins/trid3nt/`
  (installed name unchanged), rsync `-a --delete` with an explicit
  `SHIP_EXCLUDES` array (`/tests /docs /Makefile /README.md /dist` + caches +
  dotfiles; LICENSE ships). `--check` uses the same exclude set.

## Reference fixes, per class

- **`contracts/src` (PYTHONPATH / sys.path / COPY literal)** -> `contracts`: 44
  files swept (scripts run-doc headers `PYTHONPATH=.:contracts/src`, sandbox +
  smoke + run_*_direct sys.path inserts, tests, `_sfincs_build`, the two
  contracts-package self-comments, docs/authoring, docs/metrics live rows) plus
  `deploy/agent/Dockerfile` `COPY contracts/src` ->
  `COPY contracts/trid3nt_contracts`. Residual 0 (non-history). LEFT as history:
  docs/decisions, docs/specs, DELETION_LEDGER; LEFT as a dead legacy-layout
  fallback (0274 class): `workers/telemac/tests/test_classify_substance.py`
  `_ROOT / "packages" / "contracts" / "src"` (inert; the real import is the
  editable install).
- **`qgis-plugin/trid3nt` + `qgis-plugin/` (slash literal, code + LIVE docs)**
  -> `plugin`: plugin_repo.py docstrings, tool_catalog_http.py + extract_
  timeseries comment pointers, install_dependencies.py comment pointers,
  proof_geoclaw_particles/proof_swan_charts `spec_from_file_location` charts.py
  paths, README, docs/site/install.md, CLAUDE.md map line, plugin/tests run-doc
  comments (`cd qgis-plugin` -> `cd plugin`, etc). LEFT: `trid3nt-qgis-plugin`
  (the external GitHub repo URL in fetcher user_agents -- NOT a path), the
  `40-qgis-plugin-firstrun` proof screenshot filenames, docs/metrics.md line 8
  frozen "older recipe" (0274 left it retaining `server/src`+`services/workers`;
  kept consistent), docs/specs + DELETION_LEDGER (history).
- **Separated-component `"qgis-plugin" / "trid3nt"` (Path idioms)** -> `"plugin"`:
  test_plugin_repo_http_route fake-tree builder (single- + the ws_bridge
  multi-line idiom), test_ws_bridge_signal_signatures `_WS_BRIDGE` path.
- **Plugin test import name `trid3nt` -> `plugin`** (the repo dir name; the
  INSTALLED name is still `trid3nt`, handled by the packaging seams above): the
  plugin package uses relative imports so the SOURCE is untouched; the tests
  imported it by name. Swept across plugin/tests: `from trid3nt.X` ->
  `from plugin.X`, `from trid3nt import` -> `from plugin import`,
  `import_module("trid3nt.X")` + mock-patch targets `"trid3nt.X"` ->
  `"plugin.X"`, sys.modules pop filters `[0] == "trid3nt"` -> `"plugin"`. LEFT
  UNTOUCHED (NOT imports): QSettings-key strings `"trid3nt/show_thinking"` etc
  (namespaced under the plugin's runtime `GROUP="trid3nt"`, unchanged) --
  distinguished by the slash vs the import dot. The bk3b offline drivers'
  hardcoded `.../qgis-plugin` absolute paths repointed (validate ->
  `.../plugin`, headless -> repo root for `import plugin`).

## Depth + package-root fixes (plugin tests)

`plugin/tests/` sits at the SAME depth under repo root as `qgis-plugin/tests/`
did (both 2 levels), so every `..`,`..` repo-root reach (the docs/proof
screenshot dirs) is UNCHANGED. What broke is the single-`..` "parent of the
package" reach and the package-root computations:

- The recurring bootstrap `sys.path.insert(0, join(dirname(__file__), ".."))`
  added `qgis-plugin/` (parent of the `trid3nt` package). Now `..` is `plugin/`
  which IS the package; to import it as `plugin` the tests need repo root ->
  `join(dirname(__file__), "..", "..")` (26 files, incl. PLUGIN_PATH env
  defaults + the two `plugin_root` in test_raster_render + the milestone2
  loader + headless_run_invocation).
- `test_metadata_parses` `parents[1] / "trid3nt" / "metadata.txt"` -> `parents[1]
  / "metadata.txt"` (metadata is at plugin/ root now).
- `test_qt_conformance` `_PACKAGE_ROOT = join(_HERE, "..", "trid3nt")` ->
  `join(_HERE, "..")` (= plugin/), AND `_iter_py_files` now prunes
  `{tests, docs, __pycache__}` -- else the Qt6 scoped-enum scan would flag the
  dev-only Qt5 tests/ harnesses (they legitimately use unscoped enums). Two NEW
  failures caught + fixed by the plugin suite.
- `test_install_dependencies` `PLUGIN_ROOT = ....parent / "trid3nt"` -> `.parent`
  (= plugin/); `scan_third_party_imports` in install_dependencies.py now skips
  `{tests, docs, __pycache__}` relative to root (a no-op in an installed profile
  which has neither -- there the sweep saw an EMPTY tree with the stale
  `/trid3nt` and reported source-imports = [], a NEW failure caught + fixed).
- `headless_case_switch_proof` `join(PLUGIN_PATH, "..", "docs", "proof")` ->
  `join(PLUGIN_PATH, "docs", "proof")` (PLUGIN_PATH default is now repo root).

## Gates (all green)

- Four slices (repo root, `env -u TRID3NT_CACHE_BUCKET`): [a-e] 1499 passed /
  5 skipped; [f-o] 6400 passed / **4 failed** (`test_fetch_resolution_gate` x4) /
  1 xfailed; [p-r] 2020 passed / **2 failed** (`test_run_river_dye_scenario` x2);
  [s-z] 1414 passed / 6 skipped. Baseline EXACTLY 4 fetch_resolution + 2
  river_dye -- no regressions. (The `test_emit_on_fetch` class that 0274's src
  move disturbed stayed green -- the contracts flatten touched no
  `"src" / "trid3nt_server"` idiom.)
- contracts/: 721 passed (AFTER the editable reinstall; import proof above).
- registry import (in-process): 252 tools. plugin_repo + contracts import clean.
- daemon restart + `scripts/ws_smoke.py`: all_passed=True. Flood canary
  `scripts/run_sfincs_direct.py`: status=ok (required -- plugin_repo.py +
  tool_catalog_http comment + extract_timeseries comment are trid3nt_server
  files touched this wave).
- PLUGIN suite (`cd plugin`, `python -m unittest discover -s tests`): Ran 392
  tests, 2 failures -- the SAME pre-existing offscreen-Qt harness failures
  (`test_case_bbox`, `test_tool_picker`); zero regressions after the two
  package-root scanners were fixed. The "matplotlib MISSING (boom)" line is mock
  test-data.
- **Packaging proofs**: `make plugin-zip` -> `dist/trid3nt-plugin-0.3.16.zip`,
  `unzip -l` first entry `trid3nt/`, single top-level component `trid3nt`, has
  `trid3nt/metadata.txt` + `trid3nt/LICENSE`, NO tests/Makefile/dist/README.
  `plugin_repo.build_fresh_zip` in-process: 32 entries all `trid3nt/`-rooted,
  tests/Makefile/docs excluded. `install_plugin.sh --check` (scratch HOME): syncs
  shipped code + LICENSE + metadata.txt only, no tests/docs/Makefile/README.
  `test_metadata_parses` green. New test_plugin_repo_http_route fake tree carries
  tests/ + Makefile bait with asserts they never ship.

## Consequences

- `git mv` preserved history on the moved files; `contracts/src/` +
  `qgis-plugin/` dirs gone.
- The plugin's repo dir (`plugin`) and its LOADED name (`trid3nt`) now differ ON
  PURPOSE: tests import `plugin`, production QGIS loads `trid3nt`, relative
  imports bridge the two with zero source change. The name divergence is
  invisible at runtime (the `trid3nt/*` custom-property + logger + QSettings-
  group strings are literals, not `__name__`-derived).
- ADRs / DELETION_LEDGER / specs / frozen metrics recipe retain the old paths as
  HISTORY (do-not-rewrite-history rule).
- Supersedes nothing; completes the flat-layout campaign (0272 -> 0274 -> 0275):
  no single-child nesting remains.
