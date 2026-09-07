# The module-surface wave, measured

Python line counts (`wc -l`, `__pycache__` excluded, generated catalog JSON not
counted - it is data the image publishes), BEFORE at `09eea8b2` (the commit that
resolved the design stops, immediately ahead of Stage 1) and AFTER at the close
of Stage 5. Every number here was measured off the tree at those two points, not
estimated, and the areas are the seven the spec's section 7 named.

## The seven areas

| area | before | after | delta | what moved |
| --- | ---: | ---: | ---: | --- |
| `telemac/authoring/` | 4,597 | 1,500 | **-3,097** | `author.py` (1,859) and the three JSON-config hops `{agitation,open_water,stratified}.py` (1,363) die; `assembler.py` keeps settling and staging; `serializer.py` (80) is the whole writer. |
| `workflows/runtime/` | 6,742 | 5,658 | **-1,084** | the plan-step half of `interpreter.py` (1,189 -> 975), `plan.py`'s gates (461 -> 353), `validate.py`, `workflow.py`'s `EngineOps`; `slots.py`, `form.py` and the `_setter_envelope.py` orphan (440) delete outright. The rerun machinery survives, which is why this is not the spec's ~2,600. |
| `telemac/workflow.py` | 382 | 349 | **-33** | the four `ops.*` realizations, `_PROCESSES`, `_MESH_SHEET_PARAMS` and `_refuse_uncovered_fields` die; the `Door` a template hands over, the two acts and the card view are what stand. |
| `telemac/modules/` | 0 | 2,172 | **+2,172** | NEW: `module.py` (424) is what a slot and a wrapper are, `sheet.py` (340) the two acts, `describe.py` (184) the read over the catalog, and five wrappers (1,182) holding catalog, composites and outputs. |
| `telemac/templates/` | 3,038 | 4,451 | **+1,413** | the five plan-writing declarations at the telemac root become eight template packages plus `shared/river.py` (401). Every keyword the author hardcoded is a visible assertion here. |
| `workers/telemac/` | 3,839 | 954 | **-2,885** | `artemis_build.py` (1,176), `telemac3d_build.py` (1,002) and `_supplied_mesh.py` (197) die: the worker authors nothing. What is left is the entrypoint and its test. |
| `shared/physics_registry.py` | 753 | 0 | **-753** | the hand-transcribed keyword table; the catalog is the keyword table now. |
| **total** | **19,351** | **15,084** | **-4,267** | |

## The honest net against the promise

The spec's section 7 expected "of order -7,000 to -8,500 Python across server +
worker". Measured over the seven areas it named, the wave delivers **-4,267**.
The promise is NOT met, and the two places it was over-counted are measurable:

- `runtime/` gave up 1,084 of the ~2,600 the spec assigned it. The Stage 1
  inventory ruled the rerun machinery survives under the sheet, so the ledger,
  the snapshot, the journal and the derive/reuse half of `rerun/` never became
  deletable. This was ruled, not missed.
- the surface itself costs 3,585 that the spec's table did not carry as a line
  (`modules/` +2,172 and the templates' +1,413). The catalog moved the keyword
  table out of Python into generated data, but the wrapper, the sheet and the
  eight STEERING bodies are new Python and the spec's arithmetic did not add
  them back.

Whole-tree, at the same two commits: `trid3nt_server/` 140,375 -> 141,081
(+706), `workers/` 3,873 -> 988 (-2,885), so server + worker is **-2,179**. That
range carries other landed work (the bed-datum split, the harbour meshing, the
open-set rulings), so the seven-area number above is the wave's own.

Tests, stated rather than folded into the promise, which was server + worker
Python: `tests/` 107,335 -> 105,994 (**-1,341**) - the author, physics-registry,
open-water, agitation, stratified, supplied-mesh and declarative-card suites die
with their subjects while `test_telemac_module_surface.py` (1,088) and the
catalog-drift, describe-keywords and door-dissolution suites arrive.
