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
