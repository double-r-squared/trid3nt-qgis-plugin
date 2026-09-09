# tests/ - the structure and the cull

READ-ONLY evaluation of `/home/nate/Documents/trid3nt-local` at HEAD (`b91ee76d` in
the GRACE-2 orchestrator repo; trid3nt-local working tree unmodified). Nothing was
edited, committed or run live. Every number below is measured, not estimated:
pure LOC by `scripts/loc_report.py`'s own method (total minus blank minus comment
minus docstring lines, tokenizer-classified), collect counts by
`pytest --collect-only -q` under `venvs/agent` with `env -u TRID3NT_CACHE_BUCKET`.

---

## 1. THE MEASURED HEADLINE

| | pure LOC | files | collected |
|---|---:|---:|---:|
| **Product** (`trid3nt_server` + `plugin` + `contracts` + `scripts` + `workers`) | **98,065** | - | - |
| **Tests now** (`tests/` + `plugin/tests/` + `contracts/tests/`) | **79,680** | 382 | 9,892 |
| &nbsp;&nbsp;`tests/` | 65,377 | 316 | 9,079 |
| &nbsp;&nbsp;`plugin/tests/` | 10,106 | 44 | 388 |
| &nbsp;&nbsp;`contracts/tests/` | 3,929 | 21 | 425 |
| **Tests after the cull** | **78,053** | 372 | 6,646 |
| **Ratio now** | **0.813** test LOC per product LOC | | |
| **Ratio after the cull** | **0.796** | | |

Two honest readings of that table, and the second one is the point.

**The cull is small: 1,627 pure LOC, 2.0% of the test tree.** I hunted orphans by
subject death (does the code under test still exist), duplicates by identical AST
bodies and identical function names across files, and harness-shaped tests by what
the assertion actually pins. The suite came back mostly *live*. There is no pile of
dead tests here, because `tests die with their subjects` has evidently been enforced
along the way. Reporting a bigger cull would mean inventing one.

**The disproportion is large, and it is one file.**
`tests/test_gemini_kwargs_fuzz.py` is 205 pure LOC that collects **3,263 cases -
36% of the entire suite** - by taking the cross product of every tool in
TOOL_REGISTRY with 20 invented kwarg patterns (`tests/test_gemini_kwargs_fuzz.py:334`).
It pins ONE invariant (a tool tolerates a kwarg it does not declare). Collapsing the
cross product to a per-pattern sweep keeps the invariant, keeps the per-pattern
failure message, and returns 3,243 collected cases. That single change is 49% of
every test the suite runs.

So: **the structure is the deliverable, the cull is a rounding error, and the
parametrization is where the suite's weight actually sits.** After the collapse the
suite is 6,646 cases across six named slices instead of 9,892 across five alphabet
ranges.

---

## 2. THE STRUCTURE

### 2.1 What is wrong with flat + alphabet

Measured, not asserted:

1. **The slice boundaries are meaningless.** `tests/test_[f-o]*.py` is 4,250 cases
   because `test_gemini_kwargs_fuzz` starts with `g`. A slice that fails tells you
   nothing about what broke. Landing a file named `test_g*` re-balances the suite by
   accident.
2. **`contracts/tests` physically cannot be collected with `tests/`.** Measured:
   ```
   ImportError while loading conftest '.../contracts/tests/conftest.py'.
   _pytest.pathlib.ImportPathMismatchError:
     ('tests.conftest', '.../tests/conftest.py', PosixPath('.../contracts/tests/conftest.py'))
   ```
   `contracts/` has no `__init__.py`, so pytest's prepend import mode resolves
   `contracts/tests/conftest.py` rootward to the module name `tests.conftest`, which
   `tests/conftest.py` already owns. The "slice 5 is its own invocation" convention is
   a workaround for a config bug, not a design.
3. **The root `Makefile` has no test target at all.** `grep -n "^[a-z_-]*:" Makefile`
   returns `help setup env up down plugin-zip plugin binaries minio plugin-repo agent
   venv status stop`. The five slices exist only as prose repeated across
   `docs/validation/*.md`. There is nothing to run and nothing to change when the
   shape changes.
4. **`plugin/tests` is a different test runner.** `plugin/Makefile:45` is
   `$(PYTHON) -m unittest discover -s tests -v`. It happens to also collect under
   pytest (388 cases), so the two runners disagree about what the plugin suite is.
5. **Shared fakes live inside test modules.** Ten files import fixtures out of two
   other test files:
   - `MockMCPClient` / `_fresh_case_summary` from `tests/test_persistence.py` -
     imported by `test_aoi_pin_lane_c.py:52`, `test_active_aoi_repair_job2.py:57`,
     `test_aoi_pin_fetch_bbox_durability.py:70`, `test_case_history_rehydrate_f17.py:47`,
     `test_auto_create_case_job0262.py:58`, `test_layer_delete_and_reuse_job0325.py:45`,
     `test_server_case_handlers.py:68`, `test_uri_registry.py:833,882`
   - `MockWebSocket` from `tests/test_server_case_handlers.py` - imported by
     `test_case_history_rehydrate_f17.py:48`, `test_auto_create_case_job0262.py:59`

   This is the one hard mechanical dependency in the move: those two modules must
   surrender their fakes to a shared home before anything is relocated, or the
   relative imports break the moment the files land in different directories.

### 2.2 The directory law

Two rules, and every placement below follows from them:

> **A test file lives in the directory named for the product package it asserts
> against.** Where a file touches several, it lives with the one whose *behavior* it
> pins - the imports it merely stands up are scaffolding, not subject.
>
> **A test file whose subject is an instrument (a driver, a proof renderer, a
> checker, an extractor) lives in `tests/scripts/`, never beside the product it
> happens to drive.**

### 2.3 The tree

```
tests/
  conftest.py            # only the truly global autouse fixtures (see 2.4)
  _fakes/                # the shared doubles, extracted from test modules
    __init__.py          #   (currently: MockMCPClient, _fresh_case_summary, MockWebSocket)
    card_client.py       #   -> CULL, orphan (see 3)
    reach_chain.py       #   the two-fetch stand-in for the reach domain chain
  fixtures/              # unchanged; three subdirectories die (see 3)
  telemac/     conftest.py   25 files   4,211 LOC     428 cases
  mesh/                        7 files   2,220 LOC     227 cases
  fetchers/    conftest.py   65 files  13,254 LOC   1,562 cases
  processing/                 33 files   8,193 LOC     460 cases
  search/                     20 files   3,877 LOC     313 cases
  emission/    conftest.py   39 files   8,397 LOC     502 cases
  runtime/                    12 files   3,541 LOC     354 cases
  solver/                      6 files   1,183 LOC      59 cases
  server/      conftest.py   27 files   4,941 LOC     559 cases
  gates/       conftest.py   21 files   5,024 LOC     312 cases
  adapters/    conftest.py   23 files   4,675 LOC     468 cases
  credentials/                 7 files   1,433 LOC      95 cases
  sandbox/                     2 files     399 LOC      34 cases
  tools/                      14 files   2,213 LOC   3,642 cases -> 399 after the collapse
  model/                       1 file       77 LOC      19 cases
  scripts/                     4 files     516 LOC      39 cases
  plugin/                      1 file      102 LOC       3 cases
```

`tests/workflows/` currently exists and contains nothing but an empty
`__init__.py`. It is a leftover; it dies.

### 2.4 conftest.py placement

`tests/conftest.py` today is 123 pure LOC holding four fixtures and three autouse
fixtures. Measured usage across `tests/*.py`:

| fixture | line | users | placement |
|---|---:|---:|---|
| `_default_scripted_provider` (autouse) | `tests/conftest.py:130` | all | **stays root** - every test that touches a turn needs it |
| `_reset_fake_llm_harness` (autouse) | `tests/conftest.py:146` | all | **stays root** - paired with `fake_llm` |
| `_offline_cas_parse` (autouse) | `tests/conftest.py:223` | all (telemac-shaped) | **moves to `tests/telemac/conftest.py`** - it monkeypatches CAS parsing; nothing outside telemac reads it |
| `fake_s3` | `tests/conftest.py:109` | 13 | **stays root** - crosses fetchers, emission, solver, processing |
| `fake_llm` | `tests/conftest.py:160` | 18 | **stays root** - crosses adapters, server, gates |
| `empty_registry` | `tests/conftest.py:206` | 4 | **moves to `tests/tools/conftest.py`** |
| `telemac_result` | `tests/conftest.py:249` | 5 | **moves to `tests/telemac/conftest.py`** |
| `make_read_through_s3_injector` (helper fn) | `tests/conftest.py:71` | - | **moves to `tests/_fakes/`** - it is a function, not a fixture; conftest is not a module |

Per-directory `conftest.py` files are listed in 2.3 only where a fixture actually
moves there. **A directory gets a conftest only when a fixture lands in it** - an
empty conftest is a file that has to be read to learn it says nothing.

`contracts/tests/conftest.py` (9 pure LOC) stays where it is.
`plugin/tests/` has no conftest and needs none.

### 2.5 The import-mode fix (a precondition, not an option)

The mirror puts basenames in more than one directory. Under pytest's default
`prepend` import mode that is a collection error unless every directory carries an
`__init__.py`. Two ways out; take the second:

- add `__init__.py` to 17 new directories, keep `tests/__init__.py`, keep the
  `contracts/tests` collision; or
- **set `--import-mode=importlib` once, in the root `pyproject.toml`, and delete
  `tests/__init__.py` and `tests/workflows/__init__.py`.**

`importlib` mode drops the directory-to-package-name inference entirely. It fixes the
`ImportPathMismatchError` above (so `contracts/tests` can join a run), it lets
duplicate basenames coexist, and it turns the `from .test_persistence import ...`
relative imports into hard errors - which is correct, because those imports are the
defect named in 2.1(5) and must be fixed before the move regardless.

```toml
[tool.pytest.ini_options]
addopts = "--import-mode=importlib -p no:cacheprovider"
testpaths = ["tests", "contracts/tests", "plugin/tests"]
```

The root `pyproject.toml` has no `[tool.pytest.ini_options]` section today
(`grep -n pytest pyproject.toml` returns only the `dev` extra at line 301).

---

## 3. THE CULL

Ledger row shape - one line per chop, appended to `docs/DELETION_LEDGER.md`:

```
| <path> | <pure LOC> | ORPHAN | DUPLICATE | HARNESS | <the evidence, verbatim, with file:line> | <CONDITION that must hold before the chop> |
```

### 3.1 ORPHAN - the subject is gone

| file | pure LOC | collected | evidence | condition |
|---|---:|---:|---|---|
| `tests/eval_routing_live.py` | 528 | 0 | Live routing eval against "the agent (Gemini-2.5-pro through the FunctionTool registry)" (`tests/eval_routing_live.py:1-9`). Gemini is not a selectable provider: `model_provider()` defaults to `openai` (`trid3nt_server/adapters/model_selection.py:18`) and the shipped adapters are `openai`, `anthropic`, `scripted`. Zero code references - only `docs/validation/scope-census.md` and the code graph. It is also a LIVE driver sitting in the offline suite directory. | none |
| `tests/audit_gemini_schema_compliance.py` | 162 | 0 | A standalone `#!/usr/bin/env python` audit script (`:1`) that re-implements `tests/test_gemini_schema_compliance.py`'s four invariants. Zero code references. Also a DUPLICATE - see 3.2. | none |
| `tests/live_evidence_job_0169.py` | 155 | 0 | "Live-evidence harness for job-0169 (multi-turn function_call loop)" against "a mocked Gemini" (`:1-5`). job-0169 is closed. Zero code references. | none |
| `tests/card_client.py` | 60 | 0 | Docstring claims "Shared by every test that drives a declared gate without a daemon" (`:1-2`). Measured: `grep -rn "card_client" --include=*.py tests/ plugin/ scripts/ trid3nt_server/` returns **nothing** outside the file itself. Shared by zero tests. | none |
| `tests/test_pandas_pin_regression.py` | 25 | 3 | Guards a pandas pin for `hydromt-sfincs 1.2.2 sfincs.py:1858 / :2456` (`:1-21`). `git ls-files '*.py' \| xargs grep -ln "hydromt_sfincs\|SfincsModel"` returns **zero files**. Double violation: the subject is gone AND the two assertions call `pd.RangeIndex.is_integer()` and `pd.date_range(freq="10T")` directly - it tests pandas, not a product behavior. | the `hydromt-sfincs` / `pandas` pins in `pyproject.toml:52-70` go with it |
| `tests/workflows/__init__.py` | 0 | 0 | The directory contains this file and nothing else. | none |
| `tests/__init__.py` | 0 | 0 | Only needed by prepend import mode. | dies with the `--import-mode=importlib` switch (2.5) and the `.test_persistence` import fixes |
| `plugin/tests/headless_telemac_p4_acceptance.py` | 277 | 0 | One-shot acceptance driver; zero inbound references in `plugin/ tests/ scripts/ docs/ Makefile`. | confirm the P4 acceptance is re-provable from a live template drive |
| `plugin/tests/headless_dye_redrive_proof.py` | 193 | 0 | Zero inbound references. | the dye redrive is the flagship canary's own packet |
| `plugin/tests/headless_thinking_proof.py` | 139 | 0 | Zero inbound references (it references `headless_first_run`, nothing references it). | thinking persistence is covered offline by `tests/test_thinking_persistence.py` (222 LOC, 10 cases) |
| `tests/fixtures/sfincs_aoi/` | - (68K) | - | `grep -rl "sfincs_aoi" --include=*.py` over `tests/ plugin/ scripts/ trid3nt_server/ workers/` returns nothing; SFINCS product code is gone. | with `test_pandas_pin_regression.py` |
| `tests/fixtures/finite_fault/` | - (8K) | - | `grep -rl "finite_fault"` returns nothing. | none |
| `tests/fixtures/case2_news_article.txt` | - (4K) | - | `grep -rl "case2_news_article"` returns nothing. | none |

**ORPHAN subtotal: 1,539 pure LOC, 80K of fixture data.**

Not culled, and why: `tests/fixtures/swmm_wq/` is still read by
`tests/test_catalog_surfacing.py` even though SWMM is gone from the tree - it keeps.
`tests/test_ws_bridge_signal_signatures.py` (102 LOC) reads
`plugin/net/ws_bridge.py`, which exists - it is not an orphan, it is misplaced
(-> `tests/plugin/`). `tests/test_door_dissolution.py` (85 LOC) still asserts a live
registry fact even though the doors are dissolved: the guarantee is "no `tier=door`
tool survives", which is exactly a test that must outlive its subject's death.
`tests/test_gemini_schema_compliance.py` (184 LOC) is NOT an orphan - it targets
`build_tool_declarations`, which every provider goes through
(`trid3nt_server/server/turn/stream.py:377,550,1341`).

### 3.2 DUPLICATE - two files, one subject

| kept | dropped | pure LOC | evidence |
|---|---|---:|---|
| `tests/test_gemini_schema_compliance.py` | `tests/audit_gemini_schema_compliance.py` | 162 | Same four invariants (typeless properties, `anyOf/oneOf/allOf/$ref`, underscore params, fallback activation) - compare `:1-18` of each. **Keep the test**: it runs in the suite; the audit is a script that no one calls. Already counted in 3.1. |
| `tests/test_mesh_meshers.py` | `tests/test_mesh_om2d.py::test_the_roster_is_the_two_meshers_and_nothing_else` | 3 | Byte-identical AST body: `assert registered_meshers() == ("om2d", "reg_grid")` at `test_mesh_meshers.py:58-59` and `test_mesh_om2d.py:52-53`. **Keep the one in `test_mesh_meshers.py`**: the roster is the meshers module's own subject; `test_mesh_om2d.py` is about the OM2D mesher specifically, and a roster assertion there is a name that does not describe its file. Function-level chop, not a file. |

**DUPLICATE subtotal: 3 pure LOC (net; 162 already counted).**

Three near-duplicate families were examined and **cleared**:

- `test_router_groundwater_recharge.py` / `test_router_zell_sanford_groundwater.py`
  share 8 function names and 2 byte-identical bodies, but cover ADR 0297 and ADR 0298
  - four different specs. Not a duplicate: a repeated *staged-dataset conformance
  shape* that wants a shared parametrized helper in `tests/fetchers/conftest.py`. That
  is a consolidation, and it is a design change, so it is question Q4, not a cull.
- `test_scenario_reuse_job0326.py` (15 unit cases) /
  `test_scenario_reuse_dispatch_job0326.py` (4 async dispatch cases), and
  `test_scenario_reuse_fetch_f96.py` (16) / `test_fetch_reuse_dispatch_f96.py` (4):
  complements, unit vs dispatch. Both halves keep; both land in `tests/runtime/`.
- `test_telemac_rain_on_grid_cn.py` (15 cases, SCS-CN physics) /
  `test_rain_on_grid_cn_and_nodes.py` (8 cases, forcing selection + node fields):
  they overlap only on node CN. Merge candidate, and if merged **keep
  `test_telemac_rain_on_grid_cn.py`** - the other is an `_and_` name, which is two
  subjects admitting they are two subjects. Q5.

### 3.3 HARNESS - the assertion is about the scaffolding, not the product

Nine files in `plugin/tests/` are subprocess shims: the pytest-visible test asserts
that a `qt_*_harness.py` subprocess exited 0 and printed a magic marker. The product
assertions live inside the harness, where the suite cannot see them.

| shim | marker | harness |
|---|---|---|
| `plugin/tests/test_tool_picker.py:318` `test_harness_green` | `TOOL-PICKER-OK` | `qt_tool_picker_harness.py` |
| `plugin/tests/test_charts.py:268` `test_charts_harness` | `CHARTS-OK` | `qt_charts_harness.py` |
| `plugin/tests/test_dock_ui.py` | `DOCK-UI-OK` | `qt_dock_ui_harness.py` (958 LOC) |
| `plugin/tests/test_case_bbox.py` | `CASE-BBOX-OK` | `qt_case_bbox_harness.py` |
| `plugin/tests/test_mesh_temporal.py` | `QT-MESH-TEMPORAL-OK` | `qt_mesh_temporal_harness.py` |
| `plugin/tests/test_milestone3.py` | `QT-BRIDGE-OK` | `qt_bridge_harness.py` |
| `plugin/tests/test_remote_endpoints.py` | `REMOTE-ENDPOINTS-OK` | `qt_remote_endpoints_harness.py` |
| `plugin/tests/test_provider_config.py` | (rc only) | `qt_provider_config_harness.py` |
| `plugin/tests/test_install_dependencies.py` | (rc only) | 9 subprocess calls |

`test_harness_green` is the literal name of a test that tests the harness. The
failure mode is real and silent: a harness that stops asserting still exits 0 and
still prints its marker, and the shim stays green forever.

**This is NOT a cull.** Qt cannot be imported in-process alongside the server suite,
so the subprocess bridge is the honest mechanism, and deleting the shims deletes the
plugin's only UI coverage. The correct move is a reshape, and a reshape is a design
decision:

> Each `qt_*_harness.py` prints one `RESULT <name> PASS|FAIL` line per assertion; the
> shim parses those lines and reports one pytest case per assertion, and **fails when
> the expected assertion names are absent**. That converts 8 opaque green lights into
> named product-behavior cases and closes the silent-stop hole.

Q6 puts that to you. Until it is answered, the shims stay and are named in the
structure as what they are.

**HARNESS subtotal: 0 pure LOC culled.**

### 3.4 DISPROPORTION - keep the invariant, drop the cross product

Not orphan, not duplicate, not harness - but the largest single fact about this
suite, so it belongs in the same ledger:

| file | pure LOC | collected | measure |
|---|---:|---:|---|
| `tests/test_gemini_kwargs_fuzz.py` | 205 | **3,263** | `_FUZZ_CASES` is the cross product of every TOOL_REGISTRY entry with 20 invented kwarg patterns (`:334-337`). 36% of the suite for one invariant: a tool tolerates an undeclared kwarg. Collapse to 20 cases (one per pattern, sweeping the registry inside each and naming every tool that fails). **-3,243 collected, -85 pure LOC**, invariant and failure legibility both preserved. |

Four other registry sweeps were checked and **kept as they are** - a per-tool case
that names the failing tool is worth its count: `test_tool_description_surface.py`
(56 LOC / 260 cases), `test_arg_normalizer_wave_4_10.py` (401 / 237),
`test_gemini_schema_compliance.py` (184 / 181), `test_hook_colocation.py` (82 / 160).

### 3.5 The cull totalled

| | pure LOC | collected |
|---|---:|---:|
| ORPHAN | 1,539 | 3 |
| DUPLICATE (net) | 3 | 0 |
| HARNESS | 0 | 0 |
| DISPROPORTION (collapse) | 85 | 3,243 |
| **TOTAL** | **1,627** | **3,246** |
| tests before | 79,680 | 9,892 |
| **tests after** | **78,053** | **6,646** |

---

## 4. THE SLICES

### 4.1 Six slices, by directory, balanced on measured collect count

The alphabet ranges go. Each slice is a nameable set of subsystems, so a red slice
tells you where to look before you open the log.

| # | target | directories | files | collected (post-cull) |
|---:|---|---|---:|---:|
| 1 | `test-fetchers` | `tests/fetchers` | 65 | **1,562** |
| 2 | `test-spatial` | `tests/processing tests/emission tests/mesh` | 79 | **1,189** |
| 3 | `test-engines` | `tests/telemac tests/runtime tests/solver tests/search` | 63 | **1,154** |
| 4 | `test-server` | `tests/server tests/gates tests/credentials tests/sandbox tests/model tests/scripts` | 61 | **1,058** |
| 5 | `test-model-surface` | `tests/adapters tests/tools` | 37 | **867** |
| 6 | `test-packages` | `contracts/tests plugin/tests tests/plugin` | 66 | **816** |
| | | | **371** | **6,646** |

Spread 816-1,562. Slice 1 is the outlier and stays whole on purpose: the fetcher
router is one subject, and splitting it would put `test_router_dem` and
`test_router_hrrr` in different slices for no reason but arithmetic.

Two notes on balance, both honest:

- **These are case counts, not wall time.** The historical slices ran 49s to 511s at
  similar counts, so the slow work is not where the cases are - it is in
  `tests/telemac`, `tests/mesh` and `tests/solver`, which touch images and stage runs.
  Re-balance on measured wall time after one full run; the counts above are the
  starting point, not the answer.
- Slice 5 is `adapters + tools` at 867 because `tests/tools` drops from 3,642 to 399
  when the fuzz collapses. **If the collapse is not taken, slice 5 is 4,110 and the
  balance is gone** - the slice design and the disproportion fix are one decision.

### 4.2 The Makefile targets

There are none today. These are the whole thing, added to the root `Makefile`:

```makefile
# ---- suite -------------------------------------------------------------
# Six slices by subsystem. Run from the repo root. Globs are unquoted so the
# shell expands them; TRID3NT_CACHE_BUCKET is unset so no test can reach a
# live cache bucket; cacheprovider is off so a slice leaves nothing behind.
PYTEST = env -u TRID3NT_CACHE_BUCKET venvs/agent/bin/python -m pytest \
         -p no:cacheprovider --timeout=300 -q

.PHONY: test test-fetchers test-spatial test-engines test-server test-model-surface test-packages

test-fetchers:       ; $(PYTEST) tests/fetchers
test-spatial:        ; $(PYTEST) tests/processing tests/emission tests/mesh
test-engines:        ; $(PYTEST) tests/telemac tests/runtime tests/solver tests/search
test-server:         ; $(PYTEST) tests/server tests/gates tests/credentials tests/sandbox tests/model tests/scripts
test-model-surface:  ; $(PYTEST) tests/adapters tests/tools
test-packages:       ; $(PYTEST) contracts/tests plugin/tests tests/plugin

test: test-fetchers test-spatial test-engines test-server test-model-surface test-packages
```

`plugin/Makefile:45`'s `unittest discover` target goes with this: one runner.

### 4.3 The standing invocation, rewritten

The line that has been copied into every `docs/validation/*.md` close-out becomes:

> From the repo root with `venvs/agent`, globs unquoted, `env -u TRID3NT_CACHE_BUCKET`,
> `-p no:cacheprovider --timeout=300 -q`, each slice its own foreground invocation:
>
> ```
> make test-fetchers
> make test-spatial
> make test-engines
> make test-server
> make test-model-surface
> make test-packages
> ```
>
> or, unrolled, the six commands the targets expand to:
>
> ```
> env -u TRID3NT_CACHE_BUCKET venvs/agent/bin/python -m pytest tests/fetchers -p no:cacheprovider --timeout=300 -q
> env -u TRID3NT_CACHE_BUCKET venvs/agent/bin/python -m pytest tests/processing tests/emission tests/mesh -p no:cacheprovider --timeout=300 -q
> env -u TRID3NT_CACHE_BUCKET venvs/agent/bin/python -m pytest tests/telemac tests/runtime tests/solver tests/search -p no:cacheprovider --timeout=300 -q
> env -u TRID3NT_CACHE_BUCKET venvs/agent/bin/python -m pytest tests/server tests/gates tests/credentials tests/sandbox tests/model tests/scripts -p no:cacheprovider --timeout=300 -q
> env -u TRID3NT_CACHE_BUCKET venvs/agent/bin/python -m pytest tests/adapters tests/tools -p no:cacheprovider --timeout=300 -q
> env -u TRID3NT_CACHE_BUCKET venvs/agent/bin/python -m pytest contracts/tests plugin/tests tests/plugin -p no:cacheprovider --timeout=300 -q
> ```
>
> Baseline: EXACTLY ZERO failures across all six. Any failure is investigated.

The globs are gone entirely - a directory is a better glob than a letter range, and
it does not silently absorb the next file someone names `test_g*`.

---

## 5. EVERY FILE'S DESTINATION

316 rows, every file in `tests/` today, sorted by pure LOC within each destination.
`plugin/tests/` and `contracts/tests/` are unchanged in place unless Q2 says
otherwise.

### `tests/telemac/`  (25 files, 4211 pure LOC, 428 collected)

| pure | collected | file |
|---:|---:|---|
| 864 | 79 | `tests/test_telemac_module_surface.py` |
| 484 | 35 | `tests/test_run_river_dye_scenario.py` |
| 335 | 25 | `tests/test_telemac_rain_on_grid_template.py` |
| 305 | 29 | `tests/test_telemac_do_sag.py` |
| 185 | 25 | `tests/test_telemac_run_reads.py` |
| 182 | 11 | `tests/test_telemac_reach_mesh_session.py` |
| 168 | 10 | `tests/test_postprocess_telemac_wse.py` |
| 166 | 31 | `tests/test_telemac_outflow_stage.py` |
| 136 | 10 | `tests/test_postprocess_telemac.py` |
| 135 | 14 | `tests/test_telemac_reach_staged_inputs.py` |
| 127 | 16 | `tests/test_release_containment.py` |
| 124 | 10 | `tests/test_open_water_domains.py` |
| 102 | 12 | `tests/test_run_telemac_chain.py` |
| 102 | 11 | `tests/test_telemac_boundary_contract.py` |
| 99 | 13 | `tests/test_telemac_event_time.py` |
| 94 | 8 | `tests/test_telemac_result_reader.py` |
| 89 | 16 | `tests/test_telemac_rain_forcing.py` |
| 86 | 6 | `tests/test_telemac_cas_validate.py` |
| 81 | 15 | `tests/test_telemac_rain_on_grid_cn.py` |
| 69 | 4 | `tests/test_telemac_mesh_coverage.py` |
| 67 | 9 | `tests/test_telemac3d_vertical_grid.py` |
| 56 | 13 | `tests/test_mesh_declaration_travel.py` |
| 55 | 14 | `tests/test_telemac_input_provenance.py` |
| 50 | 8 | `tests/test_rain_on_grid_cn_and_nodes.py` |
| 50 | 4 | `tests/test_spill_fraction_chainage.py` |

### `tests/mesh/`  (7 files, 2220 pure LOC, 227 collected)

| pure | collected | file |
|---:|---:|---|
| 528 | 49 | `tests/test_mesh_om2d.py` |
| 445 | 56 | `tests/test_build_mesh_tool.py` |
| 395 | 30 | `tests/test_mesh_gate_loop.py` |
| 368 | 41 | `tests/test_mesh_topology_and_bed.py` |
| 247 | 24 | `tests/test_mesh_polygon_domain.py` |
| 142 | 18 | `tests/test_mesh_meshers.py` |
| 95 | 9 | `tests/test_mesh_shoreline_ladder.py` |

### `tests/fetchers/`  (65 files, 13254 pure LOC, 1562 collected)

| pure | collected | file |
|---:|---:|---|
| 970 | 79 | `tests/test_fallback_ladder.py` |
| 854 | 98 | `tests/test_data_fetch.py` |
| 697 | 46 | `tests/test_router_executors.py` |
| 452 | 48 | `tests/test_router_storm_tracks.py` |
| 361 | 23 | `tests/test_router_stac_raster.py` |
| 318 | 32 | `tests/test_router_overpass.py` |
| 309 | 37 | `tests/test_router_hooks.py` |
| 307 | 24 | `tests/test_router_chained.py` |
| 288 | 31 | `tests/test_router_topobathy.py` |
| 286 | 45 | `tests/test_router_goes_satellite.py` |
| 279 | 19 | `tests/test_router_transport.py` |
| 256 | 68 | `tests/test_router_zell_sanford_groundwater.py` |
| 255 | 35 | `tests/test_router_goes_animation.py` |
| 251 | 25 | `tests/test_router_nwm_streamflow.py` |
| 247 | 28 | `tests/test_bathymetry_data_seam.py` |
| 244 | 22 | `tests/test_router_fanout_routing.py` |
| 240 | 26 | `tests/test_router_dem.py` |
| 220 | 69 | `tests/test_router_promotion.py` |
| 214 | 22 | `tests/test_router_glm.py` |
| 213 | 9 | `tests/test_emit_on_fetch_seam.py` |
| 212 | 6 | `tests/test_aoi_pin_lane_c.py` |
| 209 | 16 | `tests/test_router_engine.py` |
| 208 | 21 | `tests/test_router_wfigs_incident.py` |
| 199 | 17 | `tests/test_router_stations.py` |
| 199 | 14 | `tests/test_router_vector_ogr.py` |
| 197 | 18 | `tests/test_router_envelope.py` |
| 196 | 19 | `tests/test_router_arcgis_odd.py` |
| 196 | 25 | `tests/test_router_groundwater_recharge.py` |
| 190 | 14 | `tests/test_router_river.py` |
| 183 | 21 | `tests/test_router_stac_composite.py` |
| 179 | 23 | `tests/test_router_goes_archive.py` |
| 171 | 11 | `tests/test_router_lter_records.py` |
| 168 | 14 | `tests/test_router_zip_multifile.py` |
| 166 | 17 | `tests/test_router_spec_loader.py` |
| 158 | 21 | `tests/test_router_cds.py` |
| 156 | 25 | `tests/test_router_population.py` |
| 151 | 24 | `tests/test_router_grib.py` |
| 137 | 12 | `tests/test_router_jrc.py` |
| 134 | 10 | `tests/test_router_aorc_precip.py` |
| 133 | 22 | `tests/test_router_soilgrids.py` |
| 132 | 9 | `tests/test_router_fault_sources.py` |
| 131 | 16 | `tests/test_router_hrrr.py` |
| 130 | 21 | `tests/test_router_nwis.py` |
| 129 | 12 | `tests/test_fallback_sweep_guard.py` |
| 129 | 7 | `tests/test_router_landcover.py` |
| 126 | 9 | `tests/test_router_noaa_sst.py` |
| 124 | 16 | `tests/test_router_mapserver_export.py` |
| 119 | 15 | `tests/test_router_firms.py` |
| 119 | 6 | `tests/test_router_flood_extent_observation.py` |
| 117 | 15 | `tests/test_router_viirs_day_fire.py` |
| 116 | 11 | `tests/test_router_slider_timestamps.py` |
| 113 | 11 | `tests/test_router_buildings.py` |
| 96 | 13 | `tests/test_router_3dep_extra.py` |
| 89 | 11 | `tests/test_router_sentinel1.py` |
| 88 | 7 | `tests/test_router_delegate_resolve.py` |
| 85 | 10 | `tests/test_router_statsgo.py` |
| 83 | 10 | `tests/test_router_hyriver.py` |
| 82 | 160 | `tests/test_hook_colocation.py` |
| 74 | 6 | `tests/test_router_nldas2.py` |
| 72 | 5 | `tests/test_router_keyed_misc.py` |
| 66 | 6 | `tests/test_router_opera_dswx.py` |
| 65 | 31 | `tests/test_us_states.py` |
| 62 | 3 | `tests/test_emit_on_fetch_equivalence.py` |
| 56 | 8 | `tests/test_satellite_slider.py` |
| 48 | 8 | `tests/test_router_field_boundaries.py` |

### `tests/processing/`  (33 files, 8193 pure LOC, 460 collected)

| pure | collected | file |
|---:|---:|---|
| 521 | 18 | `tests/test_compute_hillshade.py` |
| 476 | 22 | `tests/test_extract_model_at_observations.py` |
| 457 | 13 | `tests/test_clip_raster_to_polygon.py` |
| 418 | 27 | `tests/test_compute_building_density.py` |
| 402 | 11 | `tests/test_extract_landcover_class.py` |
| 392 | 14 | `tests/test_compute_impervious_surface.py` |
| 344 | 11 | `tests/test_compute_aspect.py` |
| 318 | 11 | `tests/test_compute_blended_composite.py` |
| 318 | 17 | `tests/test_compute_model_residuals.py` |
| 304 | 24 | `tests/test_compute_cross_section.py` |
| 298 | 19 | `tests/test_compute_colored_relief.py` |
| 298 | 12 | `tests/test_compute_contours.py` |
| 253 | 11 | `tests/test_hydrology_primitives.py` |
| 246 | 10 | `tests/test_compute_slope.py` |
| 242 | 16 | `tests/test_enhance_satellite_image.py` |
| 217 | 10 | `tests/test_compute_flood_depth_damage.py` |
| 217 | 16 | `tests/test_extract_timeseries_at_point.py` |
| 214 | 11 | `tests/test_compute_layer_bounds.py` |
| 212 | 14 | `tests/test_query_point_hazard.py` |
| 210 | 15 | `tests/test_digitize_water_body.py` |
| 208 | 10 | `tests/test_compute_sediment_yield.py` |
| 188 | 16 | `tests/test_compute_skill_metrics.py` |
| 176 | 9 | `tests/test_compute_flood_extent_skill.py` |
| 170 | 12 | `tests/test_compute_change_detection.py` |
| 166 | 15 | `tests/test_compute_ndvi.py` |
| 155 | 13 | `tests/test_compute_exposure_summary.py` |
| 153 | 7 | `tests/test_model_debris_flow.py` |
| 146 | 9 | `tests/test_compute_idf_curve.py` |
| 140 | 26 | `tests/test_section_tool.py` |
| 115 | 15 | `tests/test_show_nexrad_radar.py` |
| 96 | 12 | `tests/test_hydro_validation_metrics.py` |
| 90 | 12 | `tests/test_geometry_composition_tools.py` |
| 33 | 2 | `tests/test_job0305_memoryfile_lifetime.py` |

### `tests/search/`  (20 files, 3877 pure LOC, 313 collected)

| pure | collected | file |
|---:|---:|---|
| 561 | 62 | `tests/test_spatial_query.py` |
| 501 | 27 | `tests/test_catalog_tools.py` |
| 347 | 23 | `tests/test_web_fetch.py` |
| 272 | 11 | `tests/test_living_atlas.py` |
| 247 | 34 | `tests/test_search_tools.py` |
| 233 | 9 | `tests/test_tool_retrieval_shadow.py` |
| 202 | 15 | `tests/test_tool_gating_stage3.py` |
| 189 | 8 | `tests/test_tool_candidates_stage3.py` |
| 178 | 7 | `tests/test_search_tools_mongo_backend.py` |
| 171 | 13 | `tests/test_tool_annotations.py` |
| 138 | 39 | `tests/test_tool_retrieval.py` |
| 136 | 9 | `tests/test_catalog_surfacing.py` |
| 134 | 5 | `tests/test_discovery_expands_gate_lane_a.py` |
| 101 | 6 | `tests/test_catalog_user_overlay.py` |
| 99 | 5 | `tests/test_tool_candidates_waves.py` |
| 85 | 4 | `tests/test_door_dissolution.py` |
| 79 | 10 | `tests/test_describe_keywords.py` |
| 78 | 6 | `tests/test_agent_routing.py` |
| 75 | 9 | `tests/test_poor_fit_widen_lane_a.py` |
| 51 | 11 | `tests/test_search_spatial_functions.py` |

### `tests/emission/`  (39 files, 8397 pure LOC, 502 collected)

| pure | collected | file |
|---:|---:|---|
| 1219 | 68 | `tests/test_pipeline_emitter.py` |
| 656 | 44 | `tests/test_uri_registry.py` |
| 389 | 32 | `tests/test_chart_tools.py` |
| 323 | 12 | `tests/test_session_durability_jobs_bc.py` |
| 316 | 25 | `tests/test_layer_handles_adr0014.py` |
| 306 | 12 | `tests/test_layer_delete_and_reuse_job0325.py` |
| 295 | 12 | `tests/test_register_case_layer.py` |
| 295 | 12 | `tests/test_sim_card_persistence_task208.py` |
| 282 | 9 | `tests/test_solve_survive_disconnect.py` |
| 267 | 17 | `tests/test_dispatch_guards_stage3.py` |
| 236 | 11 | `tests/test_input_layer_surfacing.py` |
| 229 | 30 | `tests/test_publish_layer_vector_and_overviews.py` |
| 206 | 6 | `tests/test_resume_replays_case_layers.py` |
| 204 | 14 | `tests/test_duplicate_flood_layer_fix.py` |
| 198 | 12 | `tests/test_publish_layer_legend.py` |
| 192 | 12 | `tests/test_publish_manifest_register_only_phase4.py` |
| 191 | 4 | `tests/test_nested_substep_persistence_job168.py` |
| 178 | 7 | `tests/test_vector_tiles_f94.py` |
| 174 | 7 | `tests/test_compaction_card_persistence.py` |
| 171 | 15 | `tests/test_bench_block_hook_lane_a.py` |
| 165 | 7 | `tests/test_pipeline_emitter_substeps.py` |
| 152 | 18 | `tests/test_aoi_residual_floored_zoom_to.py` |
| 150 | 3 | `tests/test_case_layer_persistence.py` |
| 141 | 25 | `tests/test_presets.py` |
| 134 | 12 | `tests/test_auto_publish_droppable_raster.py` |
| 129 | 4 | `tests/test_scenario_reuse_dispatch_job0326.py` |
| 125 | 3 | `tests/test_layer_persist_survives_cancel.py` |
| 121 | 8 | `tests/test_outputs_seam.py` |
| 119 | 4 | `tests/test_fetch_reuse_dispatch_f96.py` |
| 115 | 2 | `tests/test_publish_discipline_job0270.py` |
| 113 | 7 | `tests/test_ephemeral_cases.py` |
| 105 | 9 | `tests/test_outputs_manifest_schema.py` |
| 105 | 7 | `tests/test_restyle_surface.py` |
| 89 | 3 | `tests/test_unique_layer_id_mint_f97.py` |
| 80 | 5 | `tests/test_envelope_case_tagging_job0277.py` |
| 66 | 7 | `tests/test_publish_layer.py` |
| 60 | 1 | `tests/test_server.py` |
| 54 | 12 | `tests/test_layer_uri_emit.py` |
| 47 | 4 | `tests/test_publish_layer_envelope.py` |

### `tests/runtime/`  (12 files, 3541 pure LOC, 354 collected)

| pure | collected | file |
|---:|---:|---|
| 1361 | 127 | `tests/test_declarative_library.py` |
| 385 | 28 | `tests/test_rerun_with_overrides.py` |
| 273 | 30 | `tests/test_workflow_skeleton.py` |
| 255 | 18 | `tests/test_cog_io.py` |
| 220 | 15 | `tests/test_scenario_reuse_job0326.py` |
| 206 | 6 | `tests/test_aoi_pin_fetch_bbox_durability.py` |
| 200 | 10 | `tests/test_active_aoi_repair_job2.py` |
| 190 | 38 | `tests/test_declarative_temporal.py` |
| 143 | 16 | `tests/test_scenario_reuse_fetch_f96.py` |
| 117 | 18 | `tests/test_run_journal.py` |
| 109 | 39 | `tests/test_user_input_species.py` |
| 82 | 9 | `tests/test_resolution_sensitivity.py` |

### `tests/solver/`  (6 files, 1183 pure LOC, 59 collected)

| pure | collected | file |
|---:|---:|---|
| 449 | 13 | `tests/test_solver_local_docker.py` |
| 238 | 4 | `tests/test_local_subprocess_runner.py` |
| 178 | 12 | `tests/test_list_run_frames.py` |
| 169 | 16 | `tests/test_read_run_diagnostics.py` |
| 97 | 11 | `tests/test_engine_room_posture.py` |
| 52 | 3 | `tests/test_solver.py` |

### `tests/server/`  (27 files, 4941 pure LOC, 559 collected)

| pure | collected | file |
|---:|---:|---|
| 420 | 22 | `tests/test_server_case_handlers.py` |
| 390 | 13 | `tests/test_payload_warning_flow.py` |
| 363 | 35 | `tests/test_plugin_repo_http_route.py` |
| 287 | 16 | `tests/test_auto_create_case_job0262.py` |
| 285 | 19 | `tests/test_telemetry_accuracy_panel.py` |
| 257 | 16 | `tests/test_file_persistence.py` |
| 230 | 16 | `tests/test_ingest_layer_http_route.py` |
| 226 | 10 | `tests/test_persistence.py` |
| 223 | 16 | `tests/test_persistence_sessions.py` |
| 221 | 12 | `tests/test_telemetry.py` |
| 210 | 27 | `tests/test_aoi_autofill_adr0017.py` |
| 205 | 5 | `tests/test_case_binding_job0268.py` |
| 198 | 7 | `tests/test_case_layer_write_path_job0259.py` |
| 196 | 13 | `tests/test_probe_point.py` |
| 187 | 6 | `tests/test_telemetry_summary_http.py` |
| 175 | 6 | `tests/test_stream_scoped_turns_job0269.py` |
| 163 | 9 | `tests/test_case_authority_resume.py` |
| 142 | 9 | `tests/test_probe_point_http_route.py` |
| 104 | 6 | `tests/test_building_detail_http_route.py` |
| 98 | 6 | `tests/test_telemetry_cache_emission.py` |
| 92 | 11 | `tests/test_max_turns_cap.py` |
| 67 | 3 | `tests/test_ws_heartbeat.py` |
| 65 | 4 | `tests/test_persistence_singleton_wiring.py` |
| 56 | 260 | `tests/test_tool_description_surface.py` |
| 37 | 3 | `tests/test_case_context_reset.py` |
| 29 | 7 | `tests/test_aoi_snap_independent_of_geolocate.py` |
| 15 | 2 | `tests/test_main_startup.py` |

### `tests/gates/`  (21 files, 5024 pure LOC, 312 collected)

| pure | collected | file |
|---:|---:|---|
| 806 | 45 | `tests/test_openai_adapter.py` |
| 593 | 18 | `tests/test_multi_turn_loop.py` |
| 427 | 42 | `tests/test_context_budget.py` |
| 422 | 20 | `tests/test_provider_config_http_route.py` |
| 334 | 10 | `tests/test_region_choice_picker.py` |
| 270 | 19 | `tests/test_spatial_input_gate.py` |
| 243 | 19 | `tests/test_fetch_resolution_gate.py` |
| 205 | 7 | `tests/test_circuit_breaker_integration.py` |
| 193 | 14 | `tests/test_tool_retry_on_failure.py` |
| 188 | 10 | `tests/test_input_review_gate.py` |
| 182 | 27 | `tests/test_circuit_breaker.py` |
| 180 | 6 | `tests/test_spatial_input_invalid_resolve.py` |
| 169 | 4 | `tests/test_context_window_abort_persistence.py` |
| 169 | 12 | `tests/test_spatial_input_neutral_line.py` |
| 158 | 8 | `tests/test_runaway_guard.py` |
| 127 | 8 | `tests/test_solver_confirm_gate.py` |
| 104 | 15 | `tests/test_law9_consequence_guard.py` |
| 100 | 13 | `tests/test_spatial_roles.py` |
| 92 | 3 | `tests/test_no_markdown_in_tool_results.py` |
| 37 | 8 | `tests/test_gate_collapse_specs.py` |
| 25 | 4 | `tests/test_gate_timeout_local.py` |

### `tests/adapters/`  (23 files, 4675 pure LOC, 468 collected)

| pure | collected | file |
|---:|---:|---|
| 580 | 20 | `tests/test_full_stream_persistence_job0267.py` |
| 347 | 40 | `tests/test_context_window_discovery.py` |
| 304 | 21 | `tests/test_anthropic_adapter.py` |
| 285 | 11 | `tests/test_turn_telemetry.py` |
| 269 | 18 | `tests/test_case_history_rehydrate_f17.py` |
| 222 | 10 | `tests/test_thinking_persistence.py` |
| 206 | 10 | `tests/test_provider_discipline.py` |
| 203 | 5 | `tests/test_parallel_call_bundling.py` |
| 202 | 6 | `tests/test_terminal_narration_and_failure_card.py` |
| 189 | 14 | `tests/test_tool_not_found_exception.py` |
| 184 | 181 | `tests/test_gemini_schema_compliance.py` |
| 183 | 9 | `tests/test_turn_invariants_stage3.py` |
| 177 | 6 | `tests/test_invariant_logging_p10.py` |
| 177 | 13 | `tests/test_thought_signature.py` |
| 164 | 14 | `tests/test_model_selector.py` |
| 162 | 31 | `tests/test_system_prompt.py` |
| 135 | 10 | `tests/test_loop_exhausted_envelope.py` |
| 134 | 8 | `tests/test_local_models_http_route.py` |
| 128 | 3 | `tests/test_turn_timeout_hardening.py` |
| 124 | 6 | `tests/test_crisp_end_after_deliverable.py` |
| 120 | 4 | `tests/test_empty_completion_retry.py` |
| 109 | 23 | `tests/test_scripted_adapter.py` |
| 71 | 5 | `tests/test_adapter_strip_private_params.py` |

### `tests/credentials/`  (7 files, 1433 pure LOC, 95 collected)

| pure | collected | file |
|---:|---:|---|
| 588 | 35 | `tests/test_credential_pipeline.py` |
| 257 | 25 | `tests/test_remote_daemon_access.py` |
| 198 | 9 | `tests/test_auth_handshake.py` |
| 161 | 7 | `tests/test_anon_identity_convergence.py` |
| 101 | 5 | `tests/test_case_list_http_route.py` |
| 75 | 4 | `tests/test_sticky_anonymous_user.py` |
| 53 | 10 | `tests/test_credential_resolver.py` |

### `tests/sandbox/`  (2 files, 399 pure LOC, 34 collected)

| pure | collected | file |
|---:|---:|---|
| 248 | 16 | `tests/test_code_exec_tool.py` |
| 151 | 18 | `tests/test_sandbox_box.py` |

### `tests/tools/`  (14 files, 2213 pure LOC, 3642 collected)

| pure | collected | file |
|---:|---:|---|
| 401 | 237 | `tests/test_arg_normalizer_wave_4_10.py` |
| 289 | 18 | `tests/test_tools_cache.py` |
| 260 | 59 | `tests/test_tool_arg_normalizer.py` |
| 218 | 9 | `tests/test_compose_case_report.py` |
| 217 | 10 | `tests/test_dev_tool_invoke_handler.py` |
| 205 | 3263 | `tests/test_gemini_kwargs_fuzz.py` |
| 119 | 7 | `tests/test_tools_registry.py` |
| 102 | 6 | `tests/test_register_tool_wave15_kwargs.py` |
| 90 | 7 | `tests/test_always_offload_heavy_tools.py` |
| 75 | 12 | `tests/test_resolution_doctrine_0224.py` |
| 68 | 3 | `tests/test_sync_tool_offload_dispatch.py` |
| 66 | 4 | `tests/test_provenance_channel.py` |
| 60 | 2 | `tests/test_template_hygiene.py` |
| 43 | 5 | `tests/test_sync_tool_offload_stage0.py` |

### `tests/model/`  (1 files, 77 pure LOC, 19 collected)

| pure | collected | file |
|---:|---:|---|
| 77 | 19 | `tests/test_model_conformance.py` |

### `tests/scripts/`  (4 files, 516 pure LOC, 39 collected)

| pure | collected | file |
|---:|---:|---|
| 270 | 23 | `tests/test_live_run_harness.py` |
| 169 | 9 | `tests/test_animation_legend_stability.py` |
| 41 | 3 | `tests/test_telemac_catalog_drift.py` |
| 36 | 4 | `tests/test_proof_basemap_credit.py` |

### `tests/plugin/`  (1 files, 102 pure LOC, 3 collected)

| pure | collected | file |
|---:|---:|---|
| 102 | 3 | `tests/test_ws_bridge_signal_signatures.py` |

### `tests/_helper/`  (7 files, 1096 pure LOC, 0 collected)

| pure | collected | file |
|---:|---:|---|
| 528 | - | `tests/eval_routing_live.py` |
| 162 | - | `tests/audit_gemini_schema_compliance.py` |
| 155 | - | `tests/live_evidence_job_0169.py` |
| 123 | - | `tests/conftest.py` |
| 68 | - | `tests/reach_chain.py` |
| 60 | - | `tests/card_client.py` |
| 0 | - | `tests/__init__.py` |

### `tests/CULL-ORPHAN/`  (1 files, 25 pure LOC, 3 collected)

| pure | collected | file |
|---:|---:|---|
| 25 | 3 | `tests/test_pandas_pin_regression.py` |

### `tests/UNASSIGNED/`  (1 files, 0 pure LOC, 0 collected)

| pure | collected | file |
|---:|---:|---|
| 0 | - | `tests/workflows/__init__.py` |
---

## 6. QUESTIONS FOR NATE

Each carries a recommendation. None of these is mechanical; all six are design.

**Q1. Six slices or five?**
Six is what the subsystems actually are, and it is what the collect counts balance
into (816-1,562). Five requires either folding `contracts/tests` + `plugin/tests`
into a subsystem slice they have nothing to do with, or splitting `tests/fetchers`
on an arithmetic boundary. The "five slices" phrase is load-bearing in a dozen
close-out docs, so changing the count means editing the standing conformance line.
**RECOMMENDATION: six.** The count was never the point - zero failures across all of
them was. Rewrite the standing line to "all six slices, zero failures."

**Q2. Do `plugin/tests/` and `contracts/tests/` join the mirror under `tests/`, or
stay where they are?**
They are separate distributions: `contracts/` and `plugin/` each have their own
`pyproject.toml` / `Makefile`, and `plugin/Makefile:22` explicitly excludes `/tests`
from the shipped zip. Moving them to `tests/contracts/` and `tests/plugin/` breaks
that co-location and makes the plugin zip build reach outside its own tree.
**RECOMMENDATION: they stay.** They join the *run* (slice 6) but not the *tree*.
The one exception is `tests/test_ws_bridge_signal_signatures.py`, which asserts
against `plugin/net/ws_bridge.py` from the server suite - it moves to `tests/plugin/`
(a one-file directory naming the seam) or into `plugin/tests/`. I lean
`tests/plugin/` because it must run offline in the server suite, which is the whole
reason it parses with `ast` instead of importing Qt (`:11-13`).

**Q3. Does `--import-mode=importlib` land, with `tests/__init__.py` deleted?**
This is a precondition for the mirror (2.5), it fixes the `contracts/tests` collision
that forces a separate invocation today, and it turns the ten
`from .test_persistence import ...` imports into hard errors that must be fixed by
extracting the fakes into `tests/_fakes/`. That last part is the real work: ~10 files
touched, two modules lose their fakes.
**RECOMMENDATION: yes, and land it FIRST, as its own change, before a single file
moves.** A green suite under `importlib` with the fakes extracted and the tree still
flat is the checkpoint that proves the move is safe. Moving files and switching
import modes in one wave means a red suite with two candidate causes.

**Q4. Splits vs homes - the three files that span subsystems.**
Three files genuinely straddle, and I recommend a home for each rather than a split,
because a split doubles the file count for one or two cases:
- `tests/test_cog_io.py` (7 imports from `tools.cache.storage_scheme`, 3 from
  `workflows.solver.solver`, 1 from `workflows.shared.cog_io`) -> **`tests/runtime/`**
  (`workflows/shared/cog_io.py` is the subject; cache and solver are scaffolding).
- `tests/test_engine_room_posture.py` (`workflows.solver.code_provenance` x6,
  `telemac.solving`, `fetchers._router`) -> **`tests/solver/`** (the worker-doctrine
  posture is a solver fact).
- `plugin/tests/test_milestone2.py` (581 LOC) and `test_milestone3.py` (1,000 LOC,
  75 cases) are named for milestones, not subjects, and each covers 5-6 unrelated
  seams (gate cards, canvas AOI, reconnect, case list, layer grouping, token expiry).
  These are the one place I recommend **a split**, into subject-named files under
  `plugin/tests/`. 1,581 LOC under two names that say nothing is the largest naming
  debt in the tree.
Separately: `test_router_groundwater_recharge.py` / `test_router_zell_sanford_groundwater.py`
share 8 assertion shapes across 4 staged-dataset specs (3.2). Consolidating into a
parametrized `staged_dataset_spec` conformance helper in `tests/fetchers/conftest.py`
would pay off across every future staged fetcher.
**RECOMMENDATION: homes for the three, split the two milestones, and take the
staged-dataset helper as a follow-on rather than part of the move.**

**Q5. The fuzz collapse - 3,263 cases to 20?**
This is 49% of the post-cull suite and it is the single highest-leverage change here.
The invariant survives; what dies is 3,243 pytest ids that all prove the same thing.
The cost is that a failure names a pattern and then lists the tools that failed it,
instead of one red id per (tool, pattern) pair.
**RECOMMENDATION: yes, collapse it.** Also merge `test_rain_on_grid_cn_and_nodes.py`
into `test_telemac_rain_on_grid_cn.py` while in there - `_and_` in a test filename is
two subjects wearing one name.

**Q6. The Qt subprocess shims - reshape, or leave them?**
Nine `plugin/tests/test_*.py` files assert only "the harness exited 0 and printed
`X-OK`" (3.3). A harness that quietly stops asserting stays green forever, and one of
them is literally named `test_harness_green`. The reshape is: harnesses print
`RESULT <name> PASS|FAIL` per assertion, shims parse and re-report one pytest case
each, and a shim fails when an expected assertion name is missing. That is real work
across 8 harness files (~2,500 LOC of harness), and it changes what those tests are.
**RECOMMENDATION: reshape, but not in this wave.** Land the structure and the cull
first; take the shim reshape as its own change with the plugin's UI coverage as the
acceptance. Until then, `tests die with their subjects; a test tests a product
behavior, never the harness` has nine standing exceptions, and they should be named
as exceptions rather than quietly tolerated.
