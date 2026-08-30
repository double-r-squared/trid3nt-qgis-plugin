# 0321 - The suite re-baseline after the purge and the chained domain

SUPERSEDED IN PART by ADR 0322: the two failures this note records are
fixed and the standing baseline is now zero failures in every slice.
The denominator analysis below still stands.

The offline suite's denominator moved twice in one campaign, so the old "zero
failures at these counts" line no longer describes anything. This note fixes the
new counts, says which tests left and why, and records the two that are still red
along with the question that has to be answered before they can go green.

Measured 2026-08-30, repo root, `venvs/agent/bin/python`, from a clean checkout
plus the one test edit this verification pass made.

## The command

    df -h /tmp                                        # tmpfs 7.8G, 2.7G avail
    env -u TRID3NT_CACHE_BUCKET venvs/agent/bin/python -m pytest \
      tests/test_[a-e]*.py -p no:cacheprovider --timeout=300 -q   # + [f-o] [p-r] [s-z]
    env -u TRID3NT_CACHE_BUCKET venvs/agent/bin/python -m pytest \
      contracts/tests -p no:cacheprovider --timeout=300 -q

The globs must reach the shell. Quoting them (`"tests/test_[a-e]*.py"`) hands the
brackets to pytest, which reads them as a parametrization id and exits with
`ERROR: path cannot contain [] parametrization` and **no tests run** - an exit
that scrolls past as though the slice were merely quiet. Any transcript reporting
a slice result without a `N passed` line ran nothing.

## The new baseline

| slice | passed | skipped | failed | wall |
|---|---|---|---|---|
| `test_[a-e]*` (91 files) | 1725 | 5 | 0 | 206.32s |
| `test_[f-o]*` (47 files) | 4150 | 0 (1 xfailed) | 0 | 43.41s |
| `test_[p-r]*` (103 files) | 1917 | 1 | 0 | 99.80s |
| `test_[s-z]*` (78 files) | 1334 | 6 | **2** | 324.06s |
| `contracts/tests` | 789 | 0 | 0 | 5.30s |

Total 9915 passed, **2 failed**. The baseline is NOT zero, and this note does not
pretend it is.

## The denominator change

The previous recorded figure (ADR 0319) was 1748 / 6736 / 2161 / 1752 passed,
zero failures, contracts 789. Against that:

| slice | was | now | delta |
|---|---|---|---|
| `[a-e]` | 1748 | 1725 | -23 |
| `[f-o]` | 6736 | 4150 | -2586 |
| `[p-r]` | 2161 | 1917 | -244 |
| `[s-z]` | 1752 | 1334 | -418 |
| contracts | 789 | 789 | 0 |

-3271 tests, and none of them failed on their way out. Two movements account for
all of it.

**The engine purge.** Every non-telemac engine workflow left the repo for the
attic, and its tests left with it - which is where the `[f-o]` and `[s-z]` losses
come from (the fetch-and-onward engine families, then sfincs / swan / swmm /
schism). The kept tree is telemac plus mesh, lib, shared and solver; the other
engines return one at a time through the new architecture rather than as ports.

**The second mesh front.** `workflows/mesh/watershed.py`, `precondition_gate.py`
and `telemac_build.py` were deleted, and the tests whose subject went with them
(`test_mesh_watershed.py`, the precondition-gate decisions in
`test_mesh_meshers.py`, the `corridor_box` sections of
`test_mesh_declaration_travel.py`) went too. `contracts/` is untouched at 789,
which is the check that the loss is engine-tree and not contract erosion.

## The one edit this pass made

`tests/test_door_dissolution.py::EXPECTED_TEMPLATES` drops
`telemac_rain_on_grid`. The set is a deliberate pin - "a template that stops
registering is a capability that silently left" - and it did its job: it caught
the template being parked out of the tree's import list. Removing the name
records the parking rather than overriding the pin, and the note beside it says
why the name is absent. This also fixes
`test_template_hygiene.py::test_hygiene_gate_covers_all_templates`, which reads
the same set - when that test is run in a session that has not imported the
parked module.

## The two that are still red, and why they cannot be fixed here

    FAILED tests/test_telemac_rain_on_grid_template.py::test_registered_as_telemac_template
    FAILED tests/test_template_hygiene.py::test_hygiene_gate_covers_all_templates

One root cause. The template is parked by COMMENTING OUT one import line in
`trid3nt_server/tools/__init__.py`, but `register_workflow` registers at import
time, so importing the module is what puts it back:

    >>> from trid3nt_server.tools import TOOL_REGISTRY
    >>> 'telemac_rain_on_grid' in TOOL_REGISTRY
    False
    >>> import trid3nt_server.workflows.telemac.rain_on_grid.rain_on_grid
    >>> 'telemac_rain_on_grid' in TOOL_REGISTRY
    True

So whether the parked template is in the registry depends on which test file
imported first, and both of these tests read the live registry. Within `[s-z]`,
`test_telemac_rain_on_grid_template.py` sorts before `test_template_hygiene.py`,
asks the registry before anything has imported the module (fails), then imports it
in its next test - after which the hygiene roster sees a template the pinned set
does not (fails). Neither test can be made order-independent without first
deciding what parking IS: a registration flag the module reads, a guard that
refuses to register while the mesh step is unfinished, or moving the module out of
the import graph entirely. The metadata cannot even be READ without registering -
`telemac_rain_on_grid` is a plain function and its declaration lives only in the
registry entry - so "check the declaration without offering the tool" has no
expression today.

That is a design question, and it is recorded as a DESIGN-STOP rather than
patched. Until it is answered the honest baseline for `[s-z]` is 2 failed / 1334
passed, and these two names are the whole of it. A third failure in that slice is
a regression.

## Standing

Zero failures in `[a-e]`, `[f-o]`, `[p-r]` and `contracts`. Exactly the two named
failures in `[s-z]`. Any other failure, in any slice, is a regression and gets
investigated rather than absorbed into the baseline.

---

## AMENDMENT 2026-08-30 - adversarial re-measure of the lego tail

Re-measured from the working tree at the head of the lego tail (096c7704 plus
that tail's uncommitted edits), commands verbatim from this note, globs reaching
the shell. AS FOUND, one slice was red:

| slice | passed | skipped | failed | wall |
|---|---|---|---|---|
| `test_[a-e]*` | 1730 | 5 | 0 | 210.10s |
| `test_[f-o]*` | 4150 | 0 (1 xfailed) | 0 | 42.81s |
| `test_[p-r]*` | 1917 | 1 | **1** | 94.20s |
| `test_[s-z]*` | 1337 | 6 | 0 | 290.66s |
| `contracts/tests` | 789 | 0 | 0 | 5.30s |

The one failure:

    FAILED tests/test_run_river_dye_scenario.py::test_an_unmapped_reach_refuses_terminally_naming_the_three_supply_paths

It is a STALE PIN, not a defect in the subject. The tail rewrote
`ReachBanksUnmapped`'s message to the wording the banks-coverage ruling asks for
- "Draw or supply the reach polygon ... pick a reach with mapped water coverage",
plus the honesty sentence about 2D's useful range - and left the test pinning the
two phrases the rewrite replaced. The pin was updated to the shipped wording; the
subject was not touched. AFTER that one-line test fix:

| slice | passed | skipped | failed | wall |
|---|---|---|---|---|
| `test_[a-e]*` | 1730 | 5 | 0 | 210.10s |
| `test_[f-o]*` | 4150 | 0 (1 xfailed) | 0 | 42.81s |
| `test_[p-r]*` | 1918 | 1 | 0 | 96.64s |
| `test_[s-z]*` | 1337 | 6 | 0 | 290.66s |
| `contracts/tests` | 789 | 0 | 0 | 5.30s |

9924 passed, **0 failed** - the counts ADR 0322 records, reproduced.

### What the green does NOT cover

Recorded here because a zero-failure line invites the reading that the tail
landed. The suite is green and the two reach templates do not run: driven live,
`telemac_do_sag --coarse` and `telemac_river_dye --coarse` both refuse at
`REF_FIELD_MISSING: Ref('centerline.bbox')`. The navigate fetcher's result
carries no `bbox`, so that ref was always empty; ADR 0322's bind-time refusal did
not cause the break, it EXPOSED one that previously surfaced a step later as
`NHD_AREA_WATER_BBOX_INVALID`.

The parts that were supposed to close it are all built and all work in isolation
- `compute_layer_bounds(pad_m=)` returns a padded window from a chained
`LayerURI`, `measure_bank_coverage` journals "100.0% ... covered" on the Eel and
raises `REACH_BANKS_UNMAPPED` on a Ball Creek headwater - and none of them is
wired into a `DATA` body. `measure_bank_coverage`, `pad_m` and the `journal_note`
channel have zero consumers and zero tests between them, which is exactly why a
full-suite pass says nothing about them.

The suite is therefore a REGRESSION guard here, not evidence of the tail's
landing. Its denominator does not reach unwired code.

---

## AMENDMENT 2026-08-30b - the banks chain landed and wired

Re-measured after the banks window, the measured coverage and the sizing fix
landed. Commands verbatim from this note, globs reaching the shell.

| slice | passed | skipped | failed | wall |
|---|---|---|---|---|
| `test_[a-e]*` | 1735 | 5 | 0 | 208.19s |
| `test_[f-o]*` | 4156 | 0 (1 xfailed) | 0 | 44.55s |
| `test_[p-r]*` | 1924 | 1 | 0 | 97.12s |
| `test_[s-z]*` | 1337 | 6 | 0 | 291.46s |
| `contracts/tests` | 789 | 0 | 0 | 5.42s |

9941 passed, **0 failed**. +17 against the previous amendment, all of them tests
for code that had none: the metre pad and the chained layer handle on
`compute_layer_bounds`, the note channel at both the journal and the interpreter,
the banks window and the two coverage verdicts driven through the whole reach
chain, and the declared resolution reaching the sizing function at 120 / 400 /
1000 m.

### What the green now DOES cover, and what still fails live

The three names the previous amendment recorded as zero-coverage - `pad_m`,
`measure_bank_coverage`, `journal_note` - are wired into both reach templates'
`DATA` bodies and measured by tests that walk the real chain. Driven live
(`drive_do_sag_cards.py --coarse`, Eel River near Scotia):

    data centerline -> data window -> fetch_nhd_area_water(bbox=window.bbox)
    run note: reach banks: 100.0% of the modelled centreline is covered ...
    endpoints: 1 part(s) -> ... over 965.6 m
    section: 0.1535 km^2 of 16.2196 km^2, 1 part(s) kept, 8 dropped
    build_mesh: om2d mesh ... -> 7 nodes 6 elements EPSG:32610 (bed=True)
    mesh accepted: ... min edge 40.49829510320719 m

and an unmapped reach (Ball Creek, Macon County NC) refuses on its own cause:

    REACH_BANKS_UNMAPPED: step 'data:mapped_banks' failed: No mapped water
    polygon covers this reach ...

The run still does not reach `status=ok`. It now fails two steps further on, at
`TELEMAC_MESH_NOT_ACCEPTED: the corridor mesh for this run carries no
topology_uri`. Nothing in the tree assigns `MeshArtifact.topology_uri` - the
corridor mesher that produced the topology bundle was deleted with
`telemac_build.py` - so the reach deck writer demands a bundle no mesher builds.
That is the worker-unification port's seam, not this tail's, and it is recorded
as a DESIGN-STOP rather than patched.

---

## AMENDMENT 2026-08-30c - adversarial re-baseline, second pass

Re-measured from scratch at `9e474353` by a reviewer who ran every command
itself rather than reading the previous amendment. Commands verbatim from this
note, globs reaching the shell, `env -u TRID3NT_CACHE_BUCKET
venvs/agent/bin/python -m pytest ... -p no:cacheprovider --timeout=300 -q`.

| slice | passed | skipped | failed | wall |
|---|---|---|---|---|
| `test_[a-e]*` | 1735 | 5 | 0 | 209.78s |
| `test_[f-o]*` | 4156 | 0 (1 xfailed) | 0 | 42.75s |
| `test_[p-r]*` | 1924 | 1 | 0 | 95.86s |
| `test_[s-z]*` | 1337 | 6 | 0 | 287.42s |
| `contracts/tests` | 789 | 0 | 0 | 5.29s |

9941 passed, **0 failed**. Amendment 30b's counts reproduce exactly, slice for
slice. The standing baseline is unchanged.

### What the suite still does not reach

Two defects live entirely outside this denominator, both found by driving rather
than by testing.

**The ledger replay of a mesh-bearing plan is broken.** `build_declared_mesh`
returns `{"artifact": <MeshArtifact>, ...}`. `_serialize` classifies that dict as
`json`, so the dataclass is flattened into the ledger document and `_rehydrate`
hands the next attempt a plain dict. Both of the deck step's consumers read it by
attribute: `measured_min_edge_m` does `art.probes` and fails
`STEP_FAILED: 'dict' object has no attribute 'probes'`, and `domain_polygon_of`
does `getattr(artifact, "provenance", None) or {}`, which degrades silently to a
typed refusal blaming an extent the run never declared. Any second attempt at a
reach template after an incomplete first one dies here; only `restart_clean=True`
gets past it. `_serialize` has no dataclass arm even though `MeshArtifact`
carries both `to_json` and `from_json`.

**`workers/` carries five uncommitted deletions of modules it still imports.**
`rog_build.py`, `telemac_coastal_build.py`, `telemac_river_dye_build.py`,
`tomawac_build.py` and `rainfall_forcing_compare.py` are gone from the working
tree while `workers/telemac/entrypoint.py` imports four of them at eight call
sites, `Dockerfile` copies them, and `trid3nt_server/workflows/telemac/
run_telemac.py` names one. The offline suite does not walk `workers/`, so five
green slices say nothing about it. Four have DELETION_LEDGER lines from earlier
waves; `rainfall_forcing_compare` has none.

Neither is this tail's subject and neither is patched here.
