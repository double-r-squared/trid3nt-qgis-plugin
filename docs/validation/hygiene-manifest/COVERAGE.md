# Hygiene manifest - coverage check

FULL COVERAGE LAW audit of `docs/validation/hygiene-manifest/*.md`: every file in
the wave's scope must carry exactly one row, written by the agent that READ it.

## Scope enumeration (reproducible)

    git ls-files trid3nt_server plugin contracts scripts workers tests docs
    git ls-files | grep -iE '(^|/)README(\.[a-z]+)?$'
    git ls-files | grep -E '^(AGENTS\.md|CONVENTIONS\.md|README\.md)$'
      union, minus '^docs/proof/'                                    -> 1,907

`docs/CONVENTIONS.md` is a member of the first set; there is no root
`CONVENTIONS.md`. `docs/proof/` is excluded by the charter, as are untracked
files (`__pycache__`, `.pytest_cache`, the four untracked GSHHS shapefile parts
under `scripts/sandbox/oceanmesh/shoreline/`, and the thirteen untracked docs
siblings the `docs-and-readmes` lens names).

Scope by top-level path: `trid3nt_server` 836, `docs` 498, `tests` 331,
`contracts` 92, `plugin` 79, `scripts` 61, `workers` 8, `AGENTS.md` 1,
`README.md` 1. The mesh-format move took two files from `scripts` into
`trid3nt_server` and added the package `__init__.py` that came with them; the
three rows are in `workflows-mesh.md`. The import-mode checkpoint then deleted
`tests/__init__.py`, `tests/workflows/__init__.py` and
`contracts/tests/__init__.py` and added `tests/_fakes/__init__.py`; those four
rows are in `tests-plugin-contracts.md`, struck where the file is gone.

GAP OPENED HERE, CLOSED BY THE DOCS STAGE: `git ls-files docs | grep -v
'^docs/proof/'` counted 514 when this audit ran, sixteen above the 498 the
census enumerated - the sixteen `hygiene-manifest/` siblings that became
tracked. Two more landed afterwards (`docs/READABILITY_LEDGER.md` and
`docs/validation/docstring-exemptions.md`), making the count 516 and the gap
EIGHTEEN. All eighteen were read end to end and rowed in
`docs-and-readmes.md`, whose scope is now 209. The 100% below remains the
figure for the tree this audit walked; the docs stage's own closing line is the
figure for the tree with those eighteen in it.

## Result

| measure | count |
| --- | --- |
| files in scope | 1,907 |
| files with a row | **1,907 (100%)** |
| files with NO row | **0** (three at the start of this pass; see below) |
| census rows across all lenses | 1,958 |
| files with TWO rows | 49 (34 self-declared restatements + 15 cross-lens overlaps) |

Rows per lens (census tables only; guard, findings, summary and roll-up tables
below a lens's census are not rows and are not counted):

| lens | rows |
| --- | --- |
| `trid3nt_server-tools-fetchers.md` | 405 |
| `tests-plugin-contracts.md` | 395 |
| `docs-decisions.md` | 328 |
| `docs-and-readmes.md` | 188 (+3 for `docs/proof` READMEs, outside this scope) |
| `tools-processing.md` | 141 |
| `workflows-telemac.md` | 98 |
| `contracts.md` | 72 |
| `scripts-workers.md` | 69 |
| `trid3nt_server-tools-root-search-meta-display.md` | 57 |
| `workflows-runtime-solver-shared.md` | 43 |
| `plugin.md` | 35 |
| `trid3nt_server-server-main-persistence-plugin_repo.md` | 35 |
| `trid3nt_server-gates-cases-sandbox-testing.md` | 32 |
| `workflows-mesh.md` | 31 |
| `trid3nt_server-adapters-credentials-fallbacks.md` | 16 |
| `emission.md` | 11 |

## Files that had NO row, and the lens that missed each

Three. All three were READ end to end by this critic and rowed into the lens
whose scope sentence should have carried them; each row says so in its notes.

| file | lens that missed it | why it fell through |
| --- | --- | --- |
| `trid3nt_server/scenario_reuse.py` | `trid3nt_server-server-main-persistence-plugin_repo.md` | The lens claims "+ package root" and its table is headed "Python files (33)", but the package root holds SEVEN modules: it rowed `__init__.py`, `__main__.py`, `main.py`, `persistence.py`, `plugin_repo.py` and stopped at the four the filename names. |
| `trid3nt_server/telemetry.py` | `trid3nt_server-server-main-persistence-plugin_repo.md` | Same gap, same lens. |
| `trid3nt_server/workflows/__init__.py` | `workflows-runtime-solver-shared.md` | No lens claimed the `workflows/` PACKAGE ROOT. `workflows-mesh` takes `mesh/`, `workflows-telemac` takes `telemac/`, and this lens takes `runtime`/`solver`/`shared`; the `__init__.py` one level above all three belongs to none of their scope sentences. |

The three rows are now in place (the two package-root modules in the
`server + main + persistence + plugin_repo` lens, the package marker in the
`runtime/solver/shared` lens), with the lens headers and file counts corrected
to 35 and 41. Neither addition touches any other lens's rows.

## Files with TWO rows

None of the 49 is a contradiction: each pair either self-declares as a
restatement or is a deliberate scope intersection two lenses each disclose.

**34 - `tools-processing.md`, self-declared restatements.** Its
"Empty package markers (0 bytes each)" subsection re-lists 34 zero-byte
`__init__.py` files that its main "Code files" table already rows, and every one
of the 34 carries the literal annotation `(listed above)`. One census row plus
one index entry, not two censuses.

**15 - README files two lenses each claim.** The `docs-and-readmes` lens's scope
sentence includes "the README of every directory", which necessarily overlaps
every subtree lens that rows its own README. Both sides disclose the overlap in
their scope prose:

| README | lenses |
| --- | --- |
| `contracts/README.md` | `contracts.md`, `docs-and-readmes.md` |
| `docs/decisions/README.md` | `docs-decisions.md`, `docs-and-readmes.md` |
| `plugin/README.md` | `plugin.md`, `docs-and-readmes.md` |
| `workers/README.md` | `scripts-workers.md`, `docs-and-readmes.md` |
| `trid3nt_server/tools/README.md` | `trid3nt_server-tools-root-search-meta-display.md`, `docs-and-readmes.md` |
| `trid3nt_server/workflows/mesh/README.md` | `workflows-mesh.md`, `docs-and-readmes.md` |
| `trid3nt_server/workflows/telemac/README.md` and its 8 subtree READMEs (`authoring`, `catalog`, `helpers`, `modules`, `products`, `solving`, `templates`, `templates/shared`) | `workflows-telemac.md`, `docs-and-readmes.md` |

A double row is a duplicated READ, not a gap, and the wave can act on either
copy; where the two disagree the subtree lens is the closer reader. No
de-duplication is applied here - removing a row would delete a read that
happened.

## Method

The scope list was enumerated from `git ls-files` BEFORE any manifest was
parsed. Rows were extracted from every census table in every lens and resolved
against the scope by section-aware path prefixing (a lens whose table rows are
relative - `runtime/__init__.py`, `catalog/README.md`, `0307` - resolves under
its own scope root and the nearest preceding heading). Thirty-one table rows
resolve to no file and are correctly not rows: verdict-rollup totals, guard
rows keyed `file:line`, and the three `docs/proof` READMEs the
`docs-and-readmes` lens rows outside this scope.

## Guards leg delta

`tests/hygiene/` adds five files (344 pure LOC) to the `tests` scope; their rows
are at the foot of `tests-plugin-contracts.md`, written by the agent that wrote
and read them. No other file enters scope in this leg - the sweep edited files
that already carry rows.

## Template-docs leg delta

Three script files (442 pure LOC) enter the `scripts` scope and two guard files
(162 pure LOC) enter the `tests` scope; their rows are at the foot of
`scripts-workers.md` and `tests-plugin-contracts.md`. The `docs` scope gains ten
generated markdown pages, eight run records and thirty-three doc figures, and
the tracked tree gains twelve package maps outside `docs/`; all of them are
rowed at the foot of `docs-and-readmes.md`, which is where the map-README rule
lives. Seven existing maps were corrected against the tree and carry a row
naming the defect; each already had a row from the first pass, so no file enters
scope unrowed. The remaining edits in the leg - the four `scripts/packet/`
renderers that take `--doc` - are to files that already carry rows.
