# 0323 - The suite re-baseline after the test cull

The test cull (four scopes, `a18ddf18..cbf233c4`) removed tests whose subjects
left the tree. The denominator moved, so the standing "zero failures at these
counts" line needs new counts. This note fixes them, attributes every test that
left to the scope that took it, and records the verification that the removals
are removals rather than breakage.

Measured 2026-08-31 at `cbf233c4`, repo root, `venvs/agent/bin/python`, clean
working tree.

## The command

    df -h /tmp
    env -u TRID3NT_CACHE_BUCKET venvs/agent/bin/python -m pytest \
      tests/test_[a-e]*.py -p no:cacheprovider --timeout=300 -q   # + [f-o] [p-r] [s-z]
    env -u TRID3NT_CACHE_BUCKET venvs/agent/bin/python -m pytest \
      contracts/tests -p no:cacheprovider --timeout=300 -q

ADR 0321's warning still governs: the globs must reach the shell UNQUOTED, and a
transcript reporting a slice without an `N passed` line ran nothing.

A second trap surfaced on this pass and is worth the same standing note: a
scratchpad reused across sessions can hold a PRIOR run's slice files. Four of the
five "results" available at the start of this verification were dated the day
before and matched no run in flight. Any re-baseline must confirm slice files are
newer than the commit under test before reading them.

## The new baseline

| slice | passed | skipped | failed | wall |
|---|---|---|---|---|
| `test_[a-e]*` | 1736 | 5 | 0 | 207.62s |
| `test_[f-o]*` | 4133 | 0 (1 xfailed) | 0 | 42.12s |
| `test_[p-r]*` | 1879 | 1 | 0 | 349.04s |
| `test_[s-z]*` | 1400 | 6 | 0 | 325.74s |
| `contracts/tests` | 521 | 0 | 0 | 5.07s |

**9,669 passed, 0 failed, five slices at ZERO.** Skips are unchanged from the
entering baseline (5 / 0 / 1 / 6 / 0) and the one xfail is the same one: nothing
went quiet on its way out.

## The denominator change, attributed

The entering figure was 10,010 (worker-unification conformance walk). That walk
predates two landings that added tests, so the honest pre-cull reference is the
cull's own parent commit, `89819e5b`. Attribution is a collected-test-ID diff
between `89819e5b` and `cbf233c4` - not an arithmetic guess - taken with
`--collect-only -q` under identical commands in both trees:

| tree | collected | = passed | + skipped | + xfailed |
|---|---|---|---|---|
| `89819e5b` (pre-cull) | 10,030 | 10,017 | 12 | 1 |
| `cbf233c4` (post-cull) | 9,682 | 9,669 | 12 | 1 |

**354 test IDs removed, 6 added, net -348.** The 6 additions are renames landing
under a new id, not new coverage. Every removed id maps to a scope:

| scope | net | where |
|---|---|---|
| 1 - the cloud-deployed era | -14 | `test_case.py` 6 (CaseManifest), `test_publish_layer_titiler_base_sprint14aws.py` 2, `test_anon_identity_convergence.py` 2, `test_case_list_http_route.py` 1, `test_gate_timeout_local.py` 1, `test_case_history_rehydrate_f17.py` 1, `test_publish_manifest_register_only_phase4.py` 1 |
| 2 - the attic'd engines' wire contracts | -263 | `test_modflow_contracts.py` 159, `test_swmm_contracts.py` 58, `test_geoclaw_contracts.py` 38, `test_openquake_contracts.py` 6, `test_publish_manifest_register_only_phase4.py` 1 (`register_swan_wave_layers`), `test_ws.py` 1 |
| 3 - the declared-resolution enforcer | -18 | `test_resolution_declared_0225.py` 18 |
| 4 - the QGIS passthrough and discovery pair | -53 | `test_qgis_discovery.py` 20, `test_gemini_kwargs_fuzz.py` 20, `test_qgis_process_run_job0308.py` 6, `test_main_startup.py` 5, `test_tool_annotations.py` 1, `test_gemini_schema_compliance.py` 1 |

`contracts/tests` carries the bulk of it: 789 -> 521, entirely scope 2 plus the
six CaseManifest cases. That slice was the untouched control through the engine
purge (ADR 0321 read it as "the check that the loss is engine-tree and not
contract erosion"); this wave is the pass that finally swept it, so the control
is spent and the new 521 is the floor.

Four id-level movements are worth naming because the ledger's prose does not
predict their arithmetic:

- `test_gemini_kwargs_fuzz.py` -20 and `test_gemini_schema_compliance.py` -1 are
  DERIVED, not edited: both parametrize over the live tool roster, so
  `[qgis_process__patN]` and `[qgis_process]` ids vanished when the tool left the
  registry. Nobody deleted a test; the roster shrank.
- `test_main_startup.py` lost 6 and gained 1. The ledger says "the three
  readiness-probe tests"; the tree says five `_bind_worker_submitter` cases plus
  `test_import_tools_registry_populates_passthroughs`, which returns as
  `..._populates_startup_only_tools`. The subjects are exactly the ledgered ones -
  the count in the prose is low.
- `test_ingest_layer_http_route.py` and `test_probe_point_http_route.py` show
  removals AND additions netting zero: the `aws-batch` arm left and the surviving
  arm re-parametrized under a new id.

## What was verified beyond the counts

- Every path the ledger calls deleted is absent from the tree; every path it
  calls atticked is present in `trid3nt-attic` at the mirrored location. Tests
  were deleted, not atticked, per the standing rule.
- Zero imports of any cut module remain in `trid3nt_server/ tests/ plugin/
  contracts/ workers/ scripts/ experiments/`. Surviving mentions are docstrings
  and comments in `contracts/ws.py`, `common.py`, `telemac_contracts.py` and
  `gates/spatial_roles.py` - the residue class the ledger already carved out for
  its own pass.
- `contracts/__init__.py` `__all__` is 59 with no member naming a moved engine.
  `trid3nt_contracts.export_schemas` regenerates `contracts/schemas/` with ZERO
  drift, confirming none of the culled shapes was ever exported.
- The agent boots on the post-cull tree (the running daemon predated the cull by
  four hours and was restarted for this check): registry 166, search index 165
  tools, no traceback, and no readiness probe - the boot thread that was the only
  thing keeping the QGIS seam warm is gone with it.
- The model surface carries no QGIS: zero qgis-named tools in `TOOL_REGISTRY`,
  zero occurrences of the culled tool names in any tool description or parameter
  schema, and `retrieve_visible_tools` returns 166 tools with none qgis-named
  even for "run a qgis algorithm". Retrieval discriminates rather than merely
  failing empty - that query returns `compute_hillshade` / `compute_slope` /
  `run_solver`.
- The directory maps in the cut directories verify against the tree:
  `workers/README.md` rosters exactly `telemac/` and `mesh/`, and every path
  named in `trid3nt_server/tools/README.md` exists.

## Residue this pass found and did not fix

`contracts/trid3nt_contracts/tool_registry.py` was edited by scope 4 to strip
cloud-era nouns from `read_only_hint` and `idempotent_hint` - and those two are
clean. Two neighbours in the same file were not swept, and both ride into
`contracts/schemas/atomic_tool_metadata.json`:

- `open_world_hint.description` still reads "outside the GCP project boundary"
  and "compute, clip, and intra-GCP tools opt out".
- the `AtomicToolMetadata` class docstring still names "MongoDB writes" twice.

Choosing the replacement noun for a decommissioned substrate on a model-facing
description is a wording decision with behavioral reach, so it is reported here
rather than patched. `experiments/bench/routing_sweep/run.py` carries the same
class of residue in a docstring listing the culled tool names.

---

## Amendment 2026-09-01 - the fronts-and-orphans wave moved it again

The wave that followed this note (`bcf2a2cd..e1a052e5`) atticked two dark TELEMAC
fronts, nine `workflows/shared` orphans and the barrier spatial role, and added
tests for the deck validator, the chainage walk, the release-containment gate and
the proof basemap credit. The denominator moved again, so the counts above are
superseded by these. Measured at `e1a052e5`, same commands, same interpreter.

### The five slice lines, verbatim

    1715 passed, 5 skipped, 42 warnings in 261.72s (0:04:21)
    4135 passed, 1 xfailed, 40 warnings in 45.48s
    1879 passed, 1 skipped in 341.55s (0:05:41)
    1395 passed, 6 skipped in 316.89s (0:05:16)
    521 passed in 5.15s

| slice | passed | skipped | failed | delta vs the table above |
|---|---|---|---|---|
| `test_[a-e]*` | 1715 | 5 | 0 | -21 |
| `test_[f-o]*` | 4135 | 0 (1 xfailed) | 0 | +2 |
| `test_[p-r]*` | 1879 | 1 | 0 | 0 |
| `test_[s-z]*` | 1395 | 6 | 0 | -5 |
| `contracts/tests` | 521 | 0 | 0 | 0 |

**9,645 passed, 0 failed, five slices at ZERO.** Skips are unchanged (5 / 0 / 1 /
6 / 0) and the xfail is the same one. `contracts/tests` held at the 521 floor this
note set: the wave's cuts were engine-tree and workflow-tree, and the contract
surface did not move with them.

### The denominator change, attributed

The note above attributed id-by-id across two trees. That method is not available
here: both packages are installed into `venvs/agent` through setuptools'
`__editable__` META-PATH finder, which resolves `trid3nt_server` to THIS checkout
from any working directory - so collecting an older tree's tests imports the
NEWER server, and the counts it yields are not that tree's. Attribution is
therefore whole-file movements (exact, and every deleted file was parametrize-free
so its `def test_` count IS its id count) plus a per-slice residual for the files
that were edited rather than added or removed.

| slice | whole-file movements | net | residual (edited files) |
|---|---|---|---|
| `[a-e]` | `test_coastal_tidal_surge.py` -17 (`2f07043d`), `test_discharge_resolve.py` -3 (`a70e542c`) | -20 | -1 |
| `[f-o]` | none | 0 | +2 |
| `[p-r]` | `test_publish_quantities.py` -12 (`a70e542c`), `test_proof_basemap_credit.py` +4 (`79c8caf5`) | -8 | +8 |
| `[s-z]` | `test_shared_soil_hydraulics.py` -6, `test_shared_water_table_interp.py` -6 (`a70e542c`), `test_tomawac_wave_field.py` -9 (`2f07043d`), `test_spatial_input_barriers.py` -28 -> `test_spatial_input_gate.py` +19 (`3b2499a6`), `test_spill_fraction_chainage.py` +5 (`7fcfd244`), `test_telemac_cas_validate.py` +7 (`b0f1f541`) | -18 | +13 |

-46 from whole files, +22 from edited ones, net -24: 9,669 -> 9,645. The `[f-o]`
residual is the same DERIVED class this note already named -
`test_gemini_kwargs_fuzz.py` parametrizes over the live tool roster, so its id
count tracks the registry without anyone editing a test.

### What is superseded

The standing "zero failures at these counts" law in `AGENTS.md` and `CLAUDE.md`
hardcoded the contracts slice at 789 - a figure this note had already retired to
521. Both now point at this file's LAST amendment instead of carrying a count in
prose, so the next denominator move rots nothing but this table.
