# The documentation census - and the standard the doc wave enforces

Read-only census of this repo. Baseline commit `c2a29a1f` (2026-09-09); the tree moved to
`850e7bed`, and to `44e63624` by the time this was written, while the census ran (a concurrent wave added `docs/validation/{scripts-eval,tests-eval,worker-unification-proof-interrogation}.md`
and `corpus-additions.yaml`), so every count below is stated against `c2a29a1f` and the four
new files are out of scope. Nothing was edited, committed or run to produce this. Every
number is measured; LOC is pure code via `scripts/loc_report.py`, markdown is raw `wc -l`,
docstring lines are the physical source span of the docstring token - the same span
`loc_report.py` already excludes from pure LOC.

Source censuses: `docstrings.md`, `decisions.md`, `docs-tree.md` (scratchpad, not committed).

---

## 1. Headline numbers

### 1a. Docstrings by audience

4,299 docstrings / 36,204 lines across 660 non-test Python files
(`trid3nt_server/ plugin/ contracts/ scripts/ workers/`).

| audience | docstrings | lines | share |
|---|---|---|---|
| LLM-facing (hand-written `@register_tool` body) | 54 | 2,234 | 6.2% |
| Internal (everything else) | 4,245 | 33,970 | 93.8% |

Three of the four registration paths carry **no source docstring at all**: `source.yaml`
fetchers synthesize one (`tools/fetchers/_router/registration.py:308`), `register_workflow`
templates render one from a `doc=` dict (`workflows/runtime/workflow.py:521`), and five
symbols assign `__doc__` programmatically. The routing surface has already mostly left the
docstring; what remains in docstrings is 94% internal prose.

The 1000-char front budget is enforced on the rendered path
(`workflows/runtime/docstring.py:62` raises) and **on nothing else**: 19 of the 54
hand-written tool docstrings exceed it, worst `tools/fetchers/socioeconomic/geocode_location/geocode_location.py`
at 6,236 chars before `Returns:`. The inverse also exists - `tools/search/ogc_adapter.py:295`
carries a 63-line "Use this when / Do NOT use this for" routing block on a symbol no model
ever sees, because it is not registered.

### 1b. Docstring lines by content class

Paragraph-level, two labels max, over 26,971 non-blank internal content lines.

| content class | internal lines | LLM-facing lines | verdict under the standard |
|---|---|---|---|
| refusals-constraints | 6,762 | 464 | ALLOWED (upper bound - the scorer counts any must/never/raises paragraph) |
| usage-narrative | 5,391 | 622 | DISALLOWED |
| what-it-is | 4,451 | 55 | ALLOWED, one line |
| contract-inputs-outputs | 3,856 | 474 | ALLOWED |
| architecture-neighbours | 3,532 | 178 | DISALLOWED -> map / model |
| why-rationale | 1,576 | 21 | DISALLOWED -> `docs/decisions/` |
| history | 1,322 | 24 | DISALLOWED -> deleted (git is the archive) |
| examples | 81 | 51 | DISALLOWED |

The four unambiguously-not-a-contract classes (usage-narrative, architecture-neighbours,
why-rationale, history, examples) carry **11,902 lines, 44% of internal docstring content**.
Only 736 of 26,971 internal content lines sit inside a structured `Args:`/`Returns:`/`Raises:`
section - **26,235 lines are free prose**. The bloat is not Sphinx boilerplate; it is essays.

Marker prevalence (whole-docstring hit): spec notation (`sprint-`/`job-`/`FR-`/`§`/`Wave N`/
`Invariant N`) in 168 docstrings totalling 4,221 lines; architecture-neighbour references in
151 / 2,719 lines; history prose in 82 / 1,661; person attribution or `feedback_*` memory
references in 73 / 1,659; an explicit `WHY THIS EXISTS` essay header in 14 / 545.

### 1c. Against the proposed limits, and the projected reduction

| kind | count | lines | meet limit | excess |
|---|---|---|---|---|
| module (<= 5) | 501 | 10,243 | 64 (13%) | 7,906 |
| class (<= 3) | 672 | 5,132 | 322 (48%) | 3,733 |
| function (<= 3) | 3,072 | 18,595 | 1,211 (39%) | 11,527 |
| **all internal** | **4,245** | **33,970** | **1,597 (37.6%)** | **23,166** |

Median 6 lines, p90 18, max 156. Where the mass sits: `trid3nt_server/tools` 9,849 lines,
`workflows` 7,197, `contracts/trid3nt_contracts` 3,328 (**15.2 lines average on a Pydantic
model whose fields are already typed**), `server` 2,030, `emission` 1,817, `scripts` 1,670,
`gates` 1,531, `plugin/ui` 1,422, `adapters` 1,190.

**Projected reduction - pure LOC moves by 0.** Docstrings and comments are already excluded
from pure LOC by `loc_report.py`, so the entire cut is file-line reduction with no code change.

| tier | what | lines |
|---|---|---|
| T1 mechanical | non-summary paragraphs matching a spec-notation / history / attribution / example / `WHY THIS EXISTS` marker (255 docstrings) | -2,098 |
| T1 mechanical | narration-only comment blocks > 6 lines (47 blocks), incl. 146 lines of deleted-twin changelog in `trid3nt_server/tools/__init__.py` | -611 |
| T2 relocate | remaining excess over 3/5 across 2,648 over-limit internal docstrings | ~-21,068 |
| T2 relocate | narrative half of constraint+narration blocks (49) and method-not-rule explainers (143) | ~-1,200 |
| | **docstring lines** | **-23,166 (33,970 -> ~10,804, a 68% cut)** |
| | **comment lines** | **~-1,800 (17,274 -> ~15,474)** |
| | **product file lines** | **~-24,966 (175,270 -> ~150,304, -14%)** |

Destination split of the 21,068 relocated docstring lines: ~40% deleted outright (~8,400),
~25% to `docs/` as method notes (~5,300), ~20% to the SysML model (~4,200), ~10% to
directory-map READMEs (~2,100), ~5% survives as real call-site constraint (~1,000).

Highest-leverage single pass: **module docstrings only** - 501 headers, 10,243 lines, 87%
over limit, recovers 7,906 lines and touches no function contract.

### 1d. Comments - the comments-are-constraints law

Full-line comment blocks longer than 6 lines, product code only: **476 blocks in 121 files,
5,184 lines** (30% of the 17,274 product comment lines).

| tag | blocks | lines | fate |
|---|---|---|---|
| constraint (states a must/never/invariant/refusal) | 237 | 2,645 | KEEP |
| neutral explainer (mechanism, no rule) | 143 | 1,359 | method -> `docs/` |
| constraint + narration | 49 | 569 | SPLIT - keep the rule, cut the story |
| narration only (history / wave / person / deleted twin) | 47 | 611 | DELETE |

### 1e. Decision records by verdict

326 numbered records (0001-0327, gaps at 0221 and 0234, collision at 0307), 45,614 md lines
in `docs/decisions/`, written in 45 days.

| verdict | records | files | md lines |
|---|---|---|---|
| BINDING | 106 | 106 | 13,626 |
| SUPERSEDED | 66 | - | - |
| DEAD | 154 | - | - |
| **chop set (SUPERSEDED + DEAD)** | **220** | **220** | **31,850 (70%)** |

By cohort: A founding doctrine 0001-0035 (22 BINDING / 12 SUP / 1 DEAD); B fetcher fold
0036-0112 (22/25/30); C engine+template campaign 0113-0260 (20/19/**107**); D server refactor
0261-0279 (10/5/4); E emit-on-solve/ladders 0280-0300 (13/0/8); F declarative era 0301-0327
(19/5/4).

Two structural facts decide most verdicts: the fetcher fold closed 2026-09-09 (`b2daea6e`;
99 `source.yaml` specs, 0 coded fetchers) and the fresh-start purge of 2026-08-28 took eleven
of twelve engines out of the tree (`grep -rli` over `trid3nt_server workers`: swan 0,
elmfire 0, hecras 0, schism 0, telemac 101). Cohort A is the argument for writing fewer,
shorter records: 22 of 35 founding records are still BINDING 49 days later and exactly one
is DEAD, while 107 of 146 campaign records govern a subsystem that left the tree.

The other three ledgers: `IDEAS.md` 4,313 lines (LIVE, the rulings record, delete nothing -
re-section it), `DELETION_LEDGER.md` 3,405 lines / 439 rows (353 DELETED rows = ~2,400 lines
are rollup-able; keep the 36 QUEUED + 1 CONDITION-MET, 4 SCOPE-ATTIC, 9 REJECTED),
`REANALYZE_LEDGER.md` 248 lines / 8 entries (**the healthiest document of the four** - every
entry carries DECIDED / evidence / REVISIT TRIGGER, nothing to roll up).

### 1f. docs/ files by fate

`docs/` is 664 MB, 1,010 tracked files, 494 markdown files, 87,296 markdown lines - of which
654 MB (98% of the bytes) is `docs/proof/templates/`, frozen evidence that stays.

| fate | files | md lines |
|---|---|---|
| KEEP - live ledgers, method, frozen evidence, suite-gated model | 421 md | ~44,700 |
| DELETE - decision records SUPERSEDED or DEAD | 220 md | 31,850 |
| DELETE - docs describing gone subsystems | 53 md + 1 json | 13,301 |
| MOVE - package maps out of `docs/design/` into their package | 4 md | 497 |
| RULING NEEDED - the V&V "STATUS: THINKING" stratum | 6 md | 358 |
| **DELETE total** | **273 md + 1 json** | **45,151 md lines** |

That is 52% of all markdown under `docs/` and 55% of its line count.

### 1g. READMEs by state

22 tracked READMEs (20 `.md`, 2 `.txt`).

| state | count | which |
|---|---|---|
| ACCURATE MAP - the reference standard | 11 | `trid3nt_server/tools`, `trid3nt_server/workflows`, `workers`, and the eight `workflows/telemac/{authoring,catalog,helpers,modules,products,solving,templates,templates/shared}` |
| ACCURATE, keep as-is | 1 | `docs/model/README.md` (93 lines - the checker's contract, not prose) |
| SPLIT - map stays, method leaves | 3 | `workflows/mesh` (the GSHHG ladder + env var -> `docs/site/configuration.md`), `workflows/telemac` (the TOMAWAC-has-no-wrapper paragraph -> an ADR), `docs/proof/templates` (254 lines; the variant law -> `docs/`) |
| REWRITE | 5 | root `README.md` (125), `contracts/README.md` (90), `plugin/README.md` (245), `docs/decisions/README.md` (51), `docs/validation/README.md` (33) |
| FROZEN evidence notes | 2 | `docs/proof/templates/{oceanmesh_meshes,rog_run_products}/README.txt` |
| MISSING, map to move in from `docs/design/` | 4 | `adapters/`, `emission/`, `gates/`, `server/` |
| MISSING, no map anywhere | 8 | `cases/`, `credentials/`, `fallbacks/`, `sandbox/`, `testing/`, `scripts/`, `tests/`, `contracts/trid3nt_contracts/` |

`contracts/README.md` is the most stale file in the repo: it describes MongoDB collection
schemas, cites "Appendix D, FR-MP-5, Decision F/L" and "SRS v0.3 Appendices A-D", describes
`ExecutionHandle` as a "Cloud Workflows execution-id cancellation seam", names
`public_hazard_catalog.yaml` (the file is `public_data_source_catalog.yaml`), closes with an
"Amendments to SRS Appendices A-D" section pointing at a report that is not in this repo, and
omits 13 of the 25 live contract modules.

`AGENTS.md` (117 lines) is not a README but is the charter every agent reads, and two of its
laws name missing commands: Law 3 requires the flood canary `scripts/run_sfincs_direct.py`
(**the file does not exist**; the flood direct-call canary is retired) and Law 7 requires
`EXPECTED_TEMPLATES`, which lives only in `tests/test_door_dissolution.py:31`.

---

## 2. The docstring standard

A ruleset a lint test enforces. It lives as `tests/test_docstring_standard.py` beside
`tests/test_model_conformance.py`, and `docs/CONVENTIONS.md` is rewritten to state it (that
file currently states the OLD budget - "LLM-facing RICH, module <= 3 lines, private helpers
NONE" - and is the file the lint test should cite).

### 2.1 Definitions the checker uses

1. **LLM-facing** - a `FunctionDef`/`AsyncFunctionDef` whose `decorator_list` contains a call
   resolving to `register_tool`, OR any symbol whose `__doc__` is assigned by a module-level
   `<name>.__doc__ = ...` statement. The second clause closes a real detector gap:
   `tools/search/fetch_from_catalog/fetch_from_catalog.py:464` copies a private helper's
   docstring onto the registered tool, so `_fetch_from_catalog_entry` (54 lines) scores
   internal and is not.
2. Symbols registered through `register_workflow(doc={...})` or a `source.yaml` spec are
   **not** LLM-facing under this rule - they carry no source docstring, and their rendered
   prose is already budget-checked at `workflows/runtime/docstring.py:62`.
3. **Everything else is internal.** A nested function inherits nothing.
4. **Line** means a physical source line of the docstring token span, matching
   `loc_report.classify`, so the measure is the same one the LOC report already uses.

### 2.2 The limits

| target | limit |
|---|---|
| internal function / async function / class docstring | **<= 3 physical lines** |
| internal module docstring | **<= 5 physical lines** |
| LLM-facing docstring | no line limit; the text before the first `Args:` / `Params:` / `Returns:` must be **<= 1000 characters** - the same `_FRONT_BUDGET` the workflow renderer enforces. 19 of 54 fail today |
| full-line comment block | no length cap (a threshold-derivation table is legitimately long) |

### 2.3 Allowed content

The docstring is **the caller's contract at the call site**, and nothing else:

- **what it is** - one line, in the caller's terms, where the name and signature do not
  already say it;
- **what it refuses** - the raise, the empty return, the precondition it will not fix;
- **a constraint the signature cannot carry** - a unit, a datum, a CRS, an ordering
  guarantee, a "must be called after X", a "does not close the handle";
- **the input/output contract** where types under-specify it - what a `dict[str, Any]` key
  set actually is, what a returned path points at.

For LLM-facing docstrings only, the routing surface is additionally allowed and expected:
what question the tool answers, when to reach for it, what NOT to use it for - inside the
1000-char front.

### 2.4 Disallowed content, and where it goes instead

| disallowed class | detector | destination |
|---|---|---|
| history / changelog ("used to", "now deleted", "superseded", "retired", "the former X was cut") | regex | deleted - git is the archive |
| spec notation - `sprint-\d`, `job-\d`, `FR-[A-Z]`, `§`, `[Ww]ave \d`, `[Mm]ilestone \d`, `Invariant \d` | regex | deleted |
| person attribution, and `feedback_*` / `project_*` memory filenames | regex | deleted |
| why / rationale essays, incl. any `WHY THIS EXISTS` header | regex + length | `docs/decisions/` |
| architecture and neighbours (`:class:` cross-refs, "sibling of", "the counterpart of", "lives next to") | regex | the directory-map README, or the SysML model |
| subsystem structure, seams, handshakes, protocol walkthroughs | length | the SysML model (`docs/model/*.sysml`) |
| method and derivation (equations, factor ladders, citations, thresholds) | length | `docs/` method note |
| usage narrative, "typical use cases", `>>>` and `::` example blocks | regex | deleted, or `docs/authoring/` |
| a per-field roll-call of an already-typed Pydantic model | length | deleted - the type is the truth |

Truth order, for adjudicating any single line: **code** (names, types, signatures) ->
**inline constraint comment** -> **the docstring contract** -> **the SysML model** (structure
and requirements, suite-checked) -> **docs/** (method, rulings, maps) -> **tests**. A line
belongs at the FIRST level that can carry it. If a level above already carries it, the line
is a duplicate and is deleted.

### 2.5 The comment rule

Comments state constraints. A full-line comment block longer than 6 lines that matches **no**
constraint token (`MUST`, `NEVER`, `must not`, `cannot`, `invariant`, `refuse`, `require`,
`ALWAYS`, `DO NOT`, `guarantee`) and **does** match a history token fails the test. That is
47 blocks / 611 lines today, and it is the formulation that produces no false positive on a
legitimate long explainer such as the Matson & Dozier threshold derivation at
`tools/fetchers/imagery/_goes_archive_core.py:155`. The banned-token regex list of 2.4
applies to comments identically.

### 2.6 The exemption mechanism

One mechanism, deliberately awkward: a per-symbol pragma on the docstring's closing line,
`# docstring-exempt: <reason>`, collected by the same AST pass into
`docs/validation/docstring-exemptions.md`, which the test regenerates and diffs - the pattern
`docs/validation/worker-loc-ledger.md` already uses. **No directory-level or glob exemptions**:
a glob exemption is how `contracts/` reached 15.2 docstring lines per symbol. The expected
legitimate population is a handful (the `_ALWAYS_OFFLOAD_SYNC_TOOLS` justification table at
`server/dispatch/emitter.py:174`, one or two physics-constant derivations), and both of those
are better served by moving the table to `docs/`.

### 2.7 Seeded-break proof

Per the MBSE convention the wave ends with a seeded break: add a 6-line docstring to an
internal function, a `sprint-99` token to a comment, and a 1,100-char front to a
`@register_tool` docstring. All three must fail the suite; reverting all three must restore
green.

---

## 3. The delete list

273 markdown files and 1 json, 45,151 markdown lines, ordered by directory. Nothing here is
deleted by this document; this is the list the wave's delete commits execute.

**Two migrations run BEFORE any deletion, or content is lost:**

1. **Lift the surviving revisit triggers** out of ADRs `0049` (the experiment's
   "supersede with the re-run's verdict"), `0050` (NO_ADVANCE), `0091` (the gated fallback
   condition) and `0321` into `REANALYZE_LEDGER.md`. They are the only forward-looking
   content in the chop set; every other trigger written before 2026-09-03 lives in an ADR
   body rather than the ledger.
2. **Lift the surviving clause** out of the eight split records before deleting them.
   "In part" means a live constraint dies with the file unless its successor restates it:
   `0005` (the no-tile-server half holds; `titiler` survives only in 2 test/comment strings),
   `0022` (the fidelity ladder holds, the Malpasset half is dead), `0055` (the VRT fan-out
   survives as `_router/transforms/fan_out.py`), `0075` (the `mb_per_sq_deg_by_param`
   payload-estimate field survives), `0263` (`server/{interactions,spatial}.py` live),
   `0295` (**DEAD SWMM half, BINDING metrics half** - "the supervisor stops eating the
   metrics pointer" is a live honesty rule with no other home), `0303` (the declarative
   library itself is live at `workflows/runtime/`), `0314` (the static plan is BINDING).

### 3.1 `docs/` root - 1 file, 105 lines

| file | lines | evidence |
|---|---|---|
| `docs/metrics.md` | 105 | Its regeneration command reads `server/src/trid3nt_server`, `services/workers` and `qgis-plugin/trid3nt` - three paths that do not exist - and its measure ("LOC = python lines incl. comments/blank") is exactly the one `scripts/loc_report.py` replaced. |

### 3.2 `docs/design/` - 15 files, 2,564 lines

Stale package maps, superseded by the `trid3nt_server/**/README.md` family:

| file | lines | evidence |
|---|---|---|
| `mesh.md` | 31 | Describes `trid3nt_server/mesh/`, which does not exist; every file it names (`raster_cell_mesh.py`, `coastal_tin.py`, `hecras_geometry.py`, `swmm_network.py`, `modflow_package_validation.py`, `preview_gate.py`) is gone. |
| `workflows.md` | 37 | "the largest folder (104k LOC)" - measured 19,705 pure LOC; names twelve engine folders, one survives; superseded by `trid3nt_server/workflows/README.md` (48 lines, accurate). |
| `data.md` | 43 | Titled `data/` for a folder that is `tools/`; claims "255 tools" (161); names `simulation/` and `publish_layer/` families, both gone (`publish_layer` died in ADR 0313). |

Spent one-shots - a plan, a recon or a kickoff whose wave closed:

| file | lines | evidence |
|---|---|---|
| `offline-architecture.md` | 123 | The 2026-07-04 v1 design of the GCP->local port, "grounded in a full seam inventory of the GRACE-2 codebase"; 21 retired-era term hits; the port landed two months ago. |
| `local-roadmap-2026-07-06.md` | 137 | A five-track roadmap from 2026-07-06; every track landed or was superseded. |
| `local-model-upgrade-2026-07.md` | 226 | A model shortlist for a `qwen3:8b` era; the default is now the OpenAI-compatible / Anthropic seam. |
| `qgis-plugin-product-analysis-2026-07.md` | 386 | Analyses "a local agent with ~176 tools"; the registry is 161 tools / 97 specs and the product it analyses shipped. |
| `server-refactor-recon-2026-08-14.md` | 112 | A pre-refactor map of `server/src/trid3nt_server/server.py`; the refactor landed (ADR 0261-0265) and both the file and the path are gone; 15 retired-era hits. |
| `pipeline-library-assessment-2026-08-12.md` | 89 | Assesses "the CURRENT composer code"; composers dissolved at ADR 0105 and the GeoClaw chain it walks left the tree. |
| `emission-campaign-cadence-recon.md` | 159 | A per-engine fact table for twelve engines; one remains, and its consumer `outputs-manifest-schema.md` is FROZEN and stands alone. |
| `mesh-wave-kickoff.md` | 94 | "STATUS: READY - launches when..."; the wave closed. |
| `model-wave-kickoff.md` | 75 | "STATUS: LAUNCHED 2026-08-28"; the wave closed. |
| `worker-unification-port.md` | 442 | "Awaiting the worker-unification port" - a hand-off note between two waves, both closed. |
| `elegance-review.md` | 290 | "PROPOSALS ONLY. This document changes nothing", judged against rulings since taken. **VERIFY FIRST**: each proposal dispositioned in `IDEAS.md` before deleting. |
| `temporal-endpoint-inventory.md` | 320 | Recon feeding the temporal doctrine; ADR 0310 landed temporal transforms v1. **VERIFY FIRST** against 0310. |

### 3.3 `docs/playbooks/` - 2 files, 283 lines

| file | lines | evidence |
|---|---|---|
| `modflow-affected-fields-recipe.md` | 76 | Drives MODFLOW-GWT; no MODFLOW in the tree and no `run_modflow_*` tool registered. |
| `frame-animation-recipe.md` | 207 | Built on `fetch_goes_imagery` / GOES fire animation, not registered. **VERIFY FIRST**: the frame-animation half may survive against the live emit-on-solve frame seam - rewrite rather than delete if it does. |

### 3.4 `docs/specs/` - 19 files + 1 json, 4,422 lines

Every file in this folder opens with a status line, and the status is the verdict: all 19 are
"FOR NATE REVIEW" / "PROPOSAL ONLY" / "Status: PLAN" one-shots whose wave closed, and 18 of
them are written over `server/src/` or `agent/tools/` paths that no longer exist.

| file | lines | evidence |
|---|---|---|
| `cull-proposal.md` | 283 | Scoped over "212 tools"; the registry is 161. |
| `engine-door-refactor.md` | 134 | Doors dissolved at ADR 0094; `tests/test_door_dissolution.py:73` pins that no engine door survives. |
| `engine-rollout-contract.md` | 425 | The rollout contract for the nine engines that left the tree on 2026-08-28. |
| `modflow-pilot-contract.md` | 645 | MODFLOW is gone from the tree. |
| `hygiene-sweep-plan.md` | 409 | Written over a 15,439-line `server.py` that no longer exists. |
| `credentials-chop-plan.md` | 160 | Landed at ADR 0062. |
| `gdal-leverage-audit.md` | 173 | Its verdicts were re-decided by the 2026-09-09 fold closure. |
| `gdal-native-collapse-verdict.md` | 194 | Same closure; the live authority is `router-pilot-contract.md`. |
| `ingest-framework-adoption.md` | 152 | Settled and shipped. |
| `ingest-transport-decision.md` | 196 | Settled on httpx and shipped. |
| `hydrology-tools-analysis.md` | 391 | Superseded by the hydrology picks in memory plus the fold's HYDRO stage. |
| `processing-decloud-refactor.md` | 85 | Landed at ADR 0048. |
| `processing-redundancy-report.md` | 141 | Self-declares "the '48 tools' scope count and the '33 of 48 KEEP' summary are therefore both stale". |
| `processing-redundancy-cull-proposal.md` | 209 | Outcome table folded into ADR 0043 plus the deletion ledger. |
| `processing-redundancy-candidates.json` | 66 | A generated sidecar of the two deleted reports above. |
| `review-findings.md` | 26 | A July branch-review scratchpad. |
| `server-modularization-plan.md` | 104 | Landed (ADR 0261-0265). |
| `sfincs-workflow-audit.md` | 289 | SFINCS left the tree. |
| `shared-workflows-cull-proposal.md` | 217 | Executed. |
| `mesh-layer-extraction.md` | 189 | The mesh wave superseded it. |

KEEP in this folder: `router-pilot-contract.md` (468, still the fold's authority),
`data-router-fold.md` (155), `fetcher-fold-audit.md` (487, cited as authority by the router
contract), and the 8 `.html` specs - a separate surface not covered by the markdown rulings,
cited as live authority by the kickoffs. One of the eight, `system-uml.html`, overlaps
`docs/model/` and is the only one with a live replacement (see question 4f).

### 3.5 `docs/validation/` - 16 files, 5,927 lines

| file | lines | evidence |
|---|---|---|
| `module-coverage-board.md` | 2,209 | The engine coverage board across twelve engines; eleven are gone; 23 retired-era hits. **The single largest dead doc in the tree.** |
| `engine-coverage-inventory.md` | 732 | The same board one month older; 20 `server/src/` references. |
| `build-contract.md` | 562 | 12 `server/src/` references; superseded by the five-slice law in `AGENTS.md` plus ADR 0323. |
| `template-input-provenance-audit.md` | 486 | Audits templates that left the tree. |
| `build-report.md` | 370 | 17 `server/src/` references; a July build state. |
| `telemac-family-migration-inventory.md` | 266 | Inventory of a migration that closed. |
| `replication-candidates.md` | 228 | Superseded by the fetchable-observations refinement of 2026-08-26. |
| `ml-signoff-shortlist.md` | 226 | Shortlist executed at ADR 0182-0190. |
| `provenance-audit-2026-08-11.md` | 165 | Superseded by ADR 0106 / 0222. |
| `tool-list.md` | 155 | A July tool list, against a registry that has since changed twice. |
| `telemac-family-deck-parity.md` | 148 | Parity record of a migration that closed. |
| `template-velocity.md` | 123 | A velocity metric for a template fleet that is now eight. |
| `composer-cull-characterization.md` | 84 | Composers dissolved at ADR 0105. |
| `e2e-harness.md` | 66 | Superseded by `trid3nt_server/testing/`, the live-run harness (ADR 0305). |
| `l2-harvey-findings.md` | 61 | A July SFINCS-era finding; SFINCS left the tree. |
| `sfincs-nws-forcing-characterization.md` | 46 | Same. |

### 3.6 `docs/decisions/` - 220 files, 31,850 lines

Every record whose census verdict is SUPERSEDED or DEAD, per the 2026-09-09 ruling that
superseded or dead records are deleted outright and git is the archive. 106 BINDING records
(13,626 lines) survive. Both `0307` files are DEAD, which resolves the numbering collision by
deletion. `afk-ledger-2026-08-24.md` (87 lines) is not a decision record and MOVES out of the
folder rather than being deleted.

| file | title | verdict | evidence |
|---|---|---|---|
| `0005-qgis-native-rendering.md` | QGIS reads COGs natively; no tile server | SUPERSEDED | the no-TiTiler half holds (`titiler` survives only in 2 test/comment strings); the `/vsicurl/` + anonymous-bucket + legacy-unwrap half is deleted -... |
| `0007-file-vault-secrets.md` | secrets in a local file vault | SUPERSEDED | `file-vault://` survives ONLY in `plugin/tests/stub_server.py:1021` and `plugin/tests/test_envelope_gaps.py:212`; the server side is `credentials/{... |
| `0016-src-layout.md` | server uses the src/ layout | SUPERSEDED | the package is `trid3nt_server/` at the repo root; `server/src/` and `src/` are both gone |
| `0017-harness-absorbs-prompt.md` | the harness absorbs the prompt (PROPOSAL) | SUPERSEDED | (1)=0014, (2)=0030, (3)=dispatch guards in `server/dispatch/`, (4)=turn-loop invariants in `server/turn/`, (5)=0019, (6)=adapter thinking-strip. No... |
| `0018-auto-ask-modes.md` | auto and ask modes | SUPERSEDED | the picker it queued never shipped as described; the live two-mode gate is `gates/input_review.py` (INPUT_REQUIRED, ADR 0107) plus `gates/fallback.... |
| `0021-vv-wave-lock.md` | V&V wave lock: folds and the 9-tool scope | SUPERSEDED | group D (`set_*_parameters`) is DELETED - `tests/test_rerun_with_overrides.py:572` asserts `"set_telemac_parameters" not in TOOL_REGISTRY`; group A... |
| `0022-fidelity-ladder-canonical-vv.md` | fidelity ladder + canonical-case V&V (Malpasset) | SUPERSEDED | the ladder doctrine is BINDING (memory norm "model fidelity ladder"); the Malpasset case is a registered chop candidate - `DELETION_LEDGER.md:897`... |
| `0024-full-engine-control-surface.md` | full engine control, not scenario keyholes | SUPERSEDED | 0025 supersedes the playground-as-runtime framing in its own header; layer-1 primitives are TELEMAC-only after 2026-08-28 |
| `0026-cut-affected-fields-composer.md` | cut the affected-fields composer | DEAD | its subject (`run_model_contamination_affected_fields`) was cut in 2026-07; `analyze_affected_fields` is also gone from `tools/processing/` at HEAD |
| `0029-deterministic-rendering.md` | rendering and camera do not depend on the LLM | SUPERSEDED | the auto-publish call site this ADR created was deleted; 0313 moved the mechanism to `emission/` and killed the tool. The INVARIANT survives, state... |
| `0032-context-budget-compaction.md` | measured prompt-fit context compaction | SUPERSEDED | 0311 replaced hardcoded windows with runtime discovery + one client-side trim seam (`gates/context_budget.py`); the measured-fit rule is carried fo... |
| `0033-sandbox-containment-single-ec2.md` | sandbox containment posture on a single host | SUPERSEDED | its premise ("the live stack runs the agent on a single EC2 host") is dead (AWS decommissioned 2026-08-06) and its in-process hardening is deleted:... |
| `0034-engine-door-template-registration.md` | engine-door template registration | SUPERSEDED | 0094 header says so; `tests/test_door_dissolution.py:73 test_no_engine_door_survives()` is the live pin, and `engine_door` appears nowhere else in... |
| `0037-fetcher-fold-replication-parity-closure.md` | replication-parity closure (5/5 pilots) | SUPERSEDED | its one durable artifact, `SourceSpec.error_prefix`, survives in the contract; the parity-panel verdicts it pins were superseded twice |
| `0038-fetcher-fold-pilot-promotion.md` | pilot promotion (the first real cut) | DEAD | the pilots' twins were deleted in 2026-07; 3 of the 5 (`fetch_cdc_svi`, `fetch_census_acs`, plus `fetch_lehd_jobs`) then left the tree entirely to... |
| `0039-fetcher-fold-wave2-arcgis-family.md` | wave-2 ArcGIS FeatureServer/MapServer family | DEAD | re-adjudicated by the 2026-09-09 G1 ruling: "12 of 15 G1 (ESRI on the GDAL driver)"; the family now rides `_router/executors/vector_ogr.py`, not th... |
| `0040-fetcher-fold-wave3-usgs-dataretrieval.md` | wave-3 USGS water-data, dataretrieval-delegated | SUPERSEDED | `IDEAS.md:4228` - HWM and NFHL moved to pygeohydro, `pynhd` dropped as a direct dependency; only the NLDI halves keep the dataretrieval path. `_rou... |
| `0041-shared-workflows-cull-phase-a.md` | shared-workflows cull phase A | DEAD | its subjects were deleted in 2026-07 and the whole composer class died at 0105 |
| `0042-glm-cull-p2-dropped.md` | glm-lightning-animation cull; P2 dropped | DEAD | same; `run_model_glm_lightning_animation` has not existed since 2026-07-29 |
| `0045-fetcher-fold-wave4-stations.md` | wave-4 station family; CO-OPS currents | SUPERSEDED | 0065's own header: "the wave-4 deferred station family folds onto the EXISTING phases". `_router/executors/station_timeseries.py` serves 3 specs |
| `0046-satellite-preemptive-cull.md` | satellite fire-animation composer preemptive cull | DEAD | subject deleted 2026-07-29 |
| `0047-fetcher-fold-wave5-raster.md` | wave-5 raster: STAC-tile transport migration | SUPERSEDED | "FOLDED F1 (8 STAC specs on the stac-raster executor)" - `_router/executors/stac_raster.py`, 875 lines, replaced this wave's per-source work |
| `0049-catalog-surfacing-experiment.md` | catalog-surfacing experiment (INCONCLUSIVE) | DEAD | its own text says "Supersede this ADR with the re-run's verdict"; the re-run never happened and the registry-size question was settled instead by 0... |
| `0050-design3-stratified-data-pool.md` | Design 3: stratified data pool - NO_ADVANCE | DEAD | the stratified-pool architecture is recorded in memory as HISTORY ("pools/doors dissolved"); 0094 dissolved the doors, and no pool machinery exists... |
| `0052-fetcher-fold-wave6-vector-zip.md` | wave-6 VECTOR/ZIP: 3 folds + ZIP defer | SUPERSEDED | 0067 header: "the zip-member range-read transport ADR 0052 deferred is REJECTED-superseded"; then `IDEAS.md` G10 puts TIGER on `/vsizip//vsicurl/`... |
| `0053-fetcher-fold-wave7-raster-enablers.md` | wave-7 raster enablers: imageserver_export + STAC | SUPERSEDED | the continuous-float STAC path is now the shared `stac_raster` executor |
| `0054-fetcher-fold-wave8.md` | wave-8: copernicus re-point + gcn250 | DEAD | `fetch_copernicus_dem` is a spec on the STAC executor at HEAD (and `IDEAS.md:4247` flags it as having no corpus) - the wave's redirect/skip-HEAD ma... |
| `0055-fetcher-fold-wave9.md` | wave-9: multi_url VRT fan-out + gzip_object | SUPERSEDED | the fan-out survives as `_router/transforms/fan_out.py`; the `gzip_object` whole-object mode has no spec consumer at HEAD |
| `0057-docstring-refresh.md` | mass docstring refresh + verbatim-carry supersession | SUPERSEDED | the new law splits LLM-FACING (1000-char front budget) from the caller's contract and caps 3 lines / 5 lines with a lint test - a strictly tighter... |
| `0059-fetcher-fold-wave11.md` | wave-11: copernicus absorption + tier-2-able sweep | DEAD | a zero-landing sweep whose DEFER verdicts were superseded by 0066, 0068 and 0074 in turn, then by the 2026-09-09 closure |
| `0061-hook-wave.md` | tier-3 hook wave: auth-free JSON/REST point-obs | DEAD | a per-source wave under the 0056 contract; the contract is the survivor |
| `0064-remaining-ratchets.md` | remaining ratchets | DEAD | ratchet bookkeeping for a campaign that has since closed twice |
| `0065-station-siblings.md` | station-siblings | DEAD | supersedes 0045; itself a per-source wave. `station_timeseries` executor is the survivor and belongs to 0045's mechanism, not this record |
| `0066-arcgis-odd.md` | arcgis-odd | SUPERSEDED | its own header already carries "PARTLY SUPERSEDED (2026-09-09, `12c8471e`)"; the family moved to the GDAL ESRIJSON driver and the fold found a real... |
| `0067-zip-multifile.md` | zip/multi-file via WHOLE-OBJECT extract | SUPERSEDED | `zip_vector` deleted 2026-09-09; `_router/transport/zip_object.py` is what remains |
| `0068-raster-stragglers.md` | raster stragglers: SLR pair folds, rest STOP | DEAD | its 4 named "Superseded prior: ADR 0047/0055/0059" rows show the pattern; its own STOPs were then re-decided in 2026-09 |
| `0069-weather-grib.md` | weather/GRIB: MRMS via grib_object | DEAD | no `grib_object` shape is declared by any of the 99 specs at HEAD |
| `0070-overpass-family.md` | Overpass-family: OSM QL via endpoint_fallback | SUPERSEDED | "G3 all six Overpass rows on OSMnx" (`IDEAS.md:4200`); `_router/hooks/osm.py` + `executors/overpass_sidecar.py` replaced the hand-rolled assembler,... |
| `0071-keyed-misc.md` | keyed + misc leftovers: 5 folds, 9 STOPs | DEAD | per-source verdicts, all re-decided |
| `0072-model-bench.md` | model bench: nemotron hypothesis REFUTED | DEAD | its finding ("local 8-9B ollama is not good enough") is carried live by 0271/0301/0311, which are the standing model-dispatch decisions |
| `0075-delegate-ext-output-shapes.md` | delegate fast-follows + output shapes | SUPERSEDED | 0313's header: "Supersedes: the `auto_publish` opt-out (ADR 0075's intermediate-raster suppression)". The `mb_per_sq_deg_by_param` payload-estimate... |
| `0077-finishers.md` | per-source finishers: movebank folded | DEAD | `fetch_movebank_tracks` left the tree entirely - `DELETION_LEDGER.md` biodiversity row, **SCOPE-ATTIC(f0e87b3d)**, 7 fetch specs + 3 credential pro... |
| `0078-satellite-family.md` | satellite family: slider_timestamps folded | SUPERSEDED | the animation cluster it STOPPED was folded by the animation waves; `shape: animation_frames` serves 6 specs |
| `0079-quick-folds.md` | quick folds: firms / noaa_sst / sentinel1_sar | DEAD | per-source campaign log |
| `0080-stac-composite.md` | STAC multi-asset RGB composite (imagery trio) | SUPERSEDED | the 8 STAC specs now share one `stac_raster` executor |
| `0081-finisher-mechanisms.md` | finisher mechanisms: fault_sources | DEAD | the constant-cache two-tier and emptiness output-switch have no spec consumer at HEAD |
| `0082-landcover-flood-extent.md` | landcover + flood-extent finishers | DEAD | `IDEAS.md:4243` queued finding (2): "the land-cover categorical legend is GONE from the cached object that carried 175 classes on 09-03 (the emissi... |
| `0083-endgame.md` | endgame sweep: HRRR-Zarr + FTW GeoParquet | DEAD | `_router/hooks/hrrr.py` is the residue; the STOPs were re-decided |
| `0084-trigger-wave.md` | trigger wave: lehd_jobs + buildings | DEAD | `fetch_lehd_jobs` is in the scope attic (**SCOPE-ATTIC(f40984a4)**) and the `join` transform that served it was deleted with it (`DELETION_LEDGER.m... |
| `0085-post-merge.md` | post-merge: CDS pair + NWIS | DEAD | `_router/hooks/cds.py` is the residue |
| `0086-raster-modes.md` | raster-modes: JRC + SoilGrids | SUPERSEDED | JRC is now one of the 8 STAC-executor specs |
| `0088-animation-wave2.md` | animation wave 2: netcdf_cf_object per-frame mode | DEAD | `_router/hooks/{goes_animation,goes_archive}.py` are the residue; the per-source STOPs were re-decided by 0111 |
| `0089-topobathy-fold.md` | topobathy fold: STOP re-affirmed and SHARPENED | SUPERSEDED | 0110's title: "The fetch-time provenance channel + the fetch_topobathy fold" - the STOP this record sharpened was cleared |
| `0090-dem-storm-tracks-wave.md` | DEM + STORM_TRACKS wave: both STOP | SUPERSEDED | both STOPs cleared by name in those records |
| `0091-gated-dem-fallback.md` | gated cross-dataset DEM fallback | SUPERSEDED | the fallback-ladder waves generalized this one gated fallback into `trid3nt_server/fallbacks/{ladder,walker,persist}.py` and NATE's migrate-all rul... |
| `0092-approved-folds-population-glm.md` | approved folds: population + glm | DEAD | `fetch_population` survives as a spec + hook, but as the fold's product, not this record's constraint |
| `0093-llm-surface-dedupe.md` | LLM fed-surface dedupe: door envelope boilerplate | DEAD | its subject is the engine-door envelope, dissolved by 0094 the same day |
| `0096-dem-fold.md` | fetch_dem fold: STOP | SUPERSEDED | next-day record clears it |
| `0097-dispatch-seam-dem-fold.md` | cross-sibling dispatch seam + the fetch_dem fold | SUPERSEDED | `IDEAS.md` STALE DRIVER note: "`scripts/sandbox/oceanmesh/build_coastal_mesh.py:91` still has the pre-F2 `fetch_dem`-shaped bed helper the product... |
| `0098-mesh-m1.md` | mesh layer, wave M1 (EXTRACT) | SUPERSEDED | the mesh subsystem was rebuilt at `workflows/mesh/` with a 2-mesher roster (`om2d` + `reg_grid`); the M1 extraction targets are atticked |
| `0099-mesh-m2.md` | mesh layer, wave M2 (GENERALIZE) | SUPERSEDED | ditto |
| `0100-mesh-m3-hecras.md` | mesh layer, wave M3 (HECRAS WRITER + worker) | DEAD | `workflows/mesh/meshers/hecras.py` moved to attic in the fresh-start purge; `DELETION_LEDGER.md:1363` "`hecras_rog` mesher left with the fresh-star... |
| `0101-oceanmesh-wave.md` | oceanmesh wave (bank fallback, coastal_tin, RiverMapper) | SUPERSEDED | `coastal_edge.py` and `corridor_tin.py` are atticked (`DELETION_LEDGER.md:1228-1229`); `om2d` survives as one of two meshers |
| `0102-template-fetcher-wiring.md` | template input-provenance: wire the fetchers | SUPERSEDED | 0106 is "provenance-chain WAVE 2"; 0231 is input-layer parity |
| `0104-live-drive-fixes.md` | live-drive bug-fix wave (six defects) | DEAD | none of the six defects names a rule; the code they touched has been rewritten by 0261-0279 |
| `0109-hecras-landing.md` | HEC-RAS engine landing (engine #11) | DEAD | fresh-start purge: `workflows/hecras/` atticked; `grep -rli hecras trid3nt_server workers` = 0 |
| `0111-storm-tracks-goes-satellite.md` | storm_tracks + goes_satellite folds | DEAD | `_router/hooks/{goes_animation,goes_archive}.py`; the storm-tracks zip leg was re-folded 2026-09-09 |
| `0112-nwm-fold-endgame.md` | fetch_noaa_nwm_streamflow fold - the last coded fetcher | SUPERSEDED | this record declared the endgame once; the library fold re-opened and re-closed it with a measured `-638` product LOC and 19 families placed |
| `0113-m4-quadtree.md` | M4 QUADTREE: the real SFINCS quadtree leg (cht_sfincs) | DEAD | `workflows/sfincs/` atticked |
| `0115-schism-spike.md` | SCHISM feasibility spike | DEAD | `workflows/schism/` atticked |
| `0118-schism-landing.md` | SCHISM engine landing (engine #12) | DEAD | atticked |
| `0120-stier-wave1.md` | S-tier template wave 1 (flood/hydraulics) | DEAD | all subjects atticked |
| `0121-stier-wave2.md` | S-tier template wave 2 (hazard) - triage | DEAD | same; and its "hazard cluster" framing is itself banned by 0177 |
| `0122-hazard-easy-four.md` | hazard easy-four build wave | DEAD | same |
| `0123-easy-four-continuation.md` | hazard easy-four continuation | DEAD | same |
| `0124-swmm-network-family.md` | SWMM real-network family | DEAD | `workflows/swmm/` + `mesh/swmm_network.py` atticked |
| `0125-hecras-archetypes.md` | HEC-RAS archetypes: levee-breach landing + triage | DEAD | atticked |
| `0126-schism-candidates.md` | SCHISM candidates wave - TRIAGE | DEAD | atticked |
| `0127-hecras-2025-spike.md` | HEC-RAS 2025 Beta headless spike - NO-GO-YET | DEAD | atticked. Its one durable finding (the 2025 managed engine prepares+solves all-Linux) survives as a memory fact, not as a repo constraint |
| `0128-published-deck-runner.md` | published-deck runner: cited-SWMM-example family | DEAD | `mesh/swmm_deck_runner.py` atticked |
| `0129-hecras2025-substitution.md` | HEC-RAS 2025: Linux native-SUBSTITUTION experiment | DEAD | atticked |
| `0130-hecras2025-prepare-crux.md` | HEC-RAS 2025: THE PREPARE CRUX | DEAD | atticked |
| `0131-schism-coupled-waves-landing.md` | schism_coupled_waves LANDS (SCHISM+WWM on Duck FRF) | DEAD | atticked |
| `0132-hecras-muncie-transplant.md` | HEC-RAS: THE MUNCIE TRANSPLANT | DEAD | atticked |
| `0133-hecras-geometry-writer.md` | HEC-RAS: the 2D geometry WRITER lands | DEAD | `mesh/hecras_geometry.py` atticked |
| `0134-hecras-pure2d-forcing-reference.md` | HEC-RAS: pure-2D forcing reference obtained | DEAD | atticked |
| `0135-hecras-bc-lines-and-forcing-discharge.md` | HEC-RAS: BC Lines writer lands | DEAD | atticked |
| `0136-hecras-fresh-topology-solve.md` | HEC-RAS: FRESH-TOPOLOGY solve probe | DEAD | atticked |
| `0137-hecras-pure2d-fakereach-solve.md` | HEC-RAS: version-correct pure-2D reference found | DEAD | atticked |
| `0138-hecras-2dbc-event-conditions-wets.md` | HEC-RAS: 2D-BC-line Event Conditions schema decoded | DEAD | atticked |
| `0139-hecras-flood2d-authoring-worker-and-both-acceptances.md` | HEC-RAS flood_2d: C# AuthorMesh worker + deck composer | DEAD | atticked |
| `0140-hecras-flood2d-template-promotion.md` | HEC-RAS flood_2d PROMOTED | DEAD | atticked; the template does not register (`EXPECTED_TEMPLATES` is 8 TELEMAC names) |
| `0141-landlab-six-diagnostic-templates.md` | Landlab six-row diagnostic/knob template wave | DEAD | `workflows/landlab/` atticked |
| `0142-elmfire-sensitivity-wave.md` | ELMFIRE sensitivity wave | DEAD | atticked |
| `0143-geoclaw-swe-amr-knob-templates.md` | GeoClaw SWE+AMR knob templates | DEAD | atticked |
| `0144-geoclaw-depth-cog-revision.md` | GeoClaw depth-COG revision | DEAD | atticked |
| `0145-landlab-lake-discrimination-fix.md` | Landlab lake discrimination fix | DEAD | atticked |
| `0146-pelicun-validation-wave.md` | Pelicun validation wave | DEAD | atticked |
| `0147-swan-physics-knobs-templates.md` | SWAN physics-scheme knobs + 2 templates | DEAD | atticked |
| `0148-geoclaw-knob-activation-fix.md` | GeoClaw knob activation - stale-image rebuild | DEAD | the stale-worker-image rule is a live standing norm ("worker edits INERT until rebuild; every worker wave ends w/ rebuild + smoke THROUGH the image... |
| `0149-openquake-logic-tree-uhs-fold.md` | OpenQuake logic-tree + UHS/multi-PoE fold | DEAD | atticked |
| `0150-geoclaw-mesh-emission.md` | GeoClaw AMR mesh as a first-class per-run product | DEAD | atticked |
| `0151-swmm-mechanism-comparison-templates.md` | SWMM mechanism-comparison templates | DEAD | `mesh/swmm_mechanism_compare.py` atticked |
| `0152-sfincs-cand-s-knob-folds.md` | SFINCS CAND-S rows: knob folds | DEAD | atticked |
| `0153-modflow-package-validation.md` | MODFLOW package-validation template + PRT STOP | DEAD | `mesh/modflow_package_validation.py` atticked |
| `0154-telemac-cand-s-wind-fold-and-overlap-doctrine.md` | TELEMAC CAND-S wind fold + cross-engine overlap doctrine | SUPERSEDED | the overlap doctrine (one template per question class, never per engine-pair) is now carried by the declaration substrate and the preset family; th... |
| `0155-geoclaw-tail-particle-fgmax-folds.md` | GeoClaw CAND-S tail: particle + fgmax folds | DEAD | atticked |
| `0156-schism-tail-transport-validation.md` | SCHISM CAND-S tail: transport-validation template | DEAD | atticked |
| `0157-hecras-tail-equation-set-knob.md` | HEC-RAS CAND-S tail: diffusion-wave knob | DEAD | atticked |
| `0160-pelicun-dl-calculation-harness.md` | Pelicun DL_calculation CLI harness | DEAD | atticked |
| `0161-elmfire-transient-weather-and-crown-fronts.md` | ELMFIRE transient-weather + crown-fire fronts | DEAD | atticked |
| `0162-sfincs-wind-knobs.md` | SFINCS wind timeseries + drag-curve knobs | DEAD | atticked |
| `0163-modflow-prt-buy-front.md` | MODFLOW PRT capture-zone + BUY/Henry | DEAD | atticked |
| `0164-openquake-scenario-gmf-secondary-perils.md` | OpenQuake scenario GMF + secondary perils | DEAD | atticked |
| `0165-modflow-real-aoi-capture-zone.md` | MODFLOW real-AOI georeferenced capture zone | DEAD | atticked |
| `0166-capture-zone-measured-heads.md` | capture-zone regional gradient from MEASURED heads | DEAD | its DATA seam survives: `fetch_water_table_depth`, `fetch_aquifer_transmissivity` are live specs (0297/0298) |
| `0167-modflow-mvr-sfr-front.md` | MODFLOW MVR/SFR front | DEAD | atticked |
| `0168-geoclaw-storm-surge-front.md` | GeoClaw parametric-Holland storm-surge front | DEAD | atticked |
| `0169-telemac-waqtel-do-sag-front.md` | TELEMAC WAQTEL O2 dissolved-oxygen sag (`telemac_do_sag`) | SUPERSEDED | the template survives (`workflows/telemac/templates/do_sag/`) but was rewritten by the declarative migration; 0303's title is "Declarative library... |
| `0170-hecras-1d-steady-front.md` | HEC-RAS 1D steady front - STOP | DEAD | atticked |
| `0171-hecras-structures-front.md` | HEC-RAS 2D structure-authoring front - STOP | DEAD | superseded first by 0249, then atticked |
| `0172-hecras-fixture-seed.md` | HEC-RAS reference-fixture seed | DEAD | atticked |
| `0173-hecras-plan-hdf-skeleton.md` | HEC-RAS plan-HDF skeleton | DEAD | atticked |
| `0174-hecras-connection-gate.md` | HEC-RAS SA/2D connection gate CRACKED | DEAD | atticked |
| `0176-sfincs-real-quadtree-run.md` | SFINCS real quadtree run, off the fixture | DEAD | atticked |
| `0178-sfincs-quadtree-dispatch-and-crs.md` | SFINCS quadtree: composer dispatch + native-mesh CRS | DEAD | atticked |
| `0181-reload-flake-sweep.md` | order-dependent SFINCSSetupError reload flake | DEAD | the victim and the root fix both left with the SFINCS attic move |
| `0182-openquake-shortlist-batch.md` | OpenQuake shortlist batch | DEAD | atticked |
| `0183-modflow-georef-hardening.md` | MODFLOW postprocess georef hardening | DEAD | "identity-affine fallback removed" is the general honesty rule now stated by 0180 and the fallback ladders |
| `0184-landlab-shortlist-batch.md` | Landlab shortlist grind batch 2 | DEAD | atticked |
| `0185-geoclaw-shortlist-batch.md` | GeoClaw shortlist grind batch 3 | DEAD | atticked |
| `0186-geoclaw-thacker-fgout.md` | GeoClaw fgout knob + Thacker V&V (deferred) | DEAD | atticked |
| `0187-geoclaw-completion.md` | GeoClaw completion: fgout PROMOTION + Thacker V&V | DEAD | atticked |
| `0188-hecras-shortlist-batch.md` | HEC-RAS shortlist batch 4 | DEAD | atticked |
| `0189-schism-shortlist-batch.md` | SCHISM shortlist batch 5 | DEAD | atticked |
| `0190-final-shortlist-singles.md` | final shortlist singles (TELEMAC / ELMFIRE / SWAN / SWMM) | DEAD | 3 of 4 atticked; the TELEMAC rainfall row was superseded by 0195/0196/0206 then 0316 |
| `0191-spot-check-fixes.md` | spot-check fixes (TELEMAC mesh render, SWAN, SCHISM) | DEAD | 2 of 3 atticked; the TELEMAC mesh render is now `emission/mesh_display.py` |
| `0192-oceanmesh-standalone.md` | OceanMesh2D coastal meshing: standalone-first | SUPERSEDED | `DELETION_LEDGER.md:1125` "The standalone mesh builder - DISSOLVED into the one mesh router (2026-08-27)"; `om2d` survives as one of two meshers |
| `0193-pysheds-watershed-mesh.md` | pysheds watershed coverage + watershed-first meshing | SUPERSEDED | `meshers/watershed.py` atticked (`DELETION_LEDGER.md:1227`); the catchment STRATEGY stayed and was re-decided by 0316 |
| `0194-coastal-water-edge.md` | coastal water-edge re-mesh (OSM coastline; CUSP verdict) | SUPERSEDED | `meshers/coastal_edge.py` atticked, "its water-edge prep folds into om2d in a later wave" (`DELETION_LEDGER.md:1228`) |
| `0195-telemac-rain-on-grid.md` | TELEMAC-2D rain-on-grid foundation | SUPERSEDED | 0316 is "the rain-on-grid migration"; the mesh-acquisition step this record created was DELETED (`DELETION_LEDGER.md:717`) |
| `0196-telemac-rog-build.md` | TELEMAC-2D rain-on-grid build wave | SUPERSEDED | `rain_on_grid/mesh_acquisition.py` DELETED 2026-08-26; `observed_gauge_id`, `mesh_uri`, `mrms_window` DELETED (`DELETION_LEDGER.md:736,751`) |
| `0197-mesh-alignment-fix.md` | coastal/watershed mesh proof-render misalignment fix | DEAD | both meshers it fixed are atticked |
| `0199-hecras-rog-cross-engine.md` | HEC-RAS 2D RoG + TELEMAC-vs-HEC-RAS comparison | DEAD | HEC-RAS atticked; no cross-engine comparison surface exists |
| `0200-mesh-precondition-gate.md` | mesh as an optional user-supplied precondition | SUPERSEDED | `workflows/mesh/precondition_gate.py` was DELETED (ADR 0321 names it); the successor is `Data.supplied()` on the DATA class body |
| `0201-mesh-sandbox-polish.md` | coastal water-edge narrow-pass + NHD retry | DEAD | subject atticked |
| `0202-rog-replication.md` | RoG replication vs Coweeta: STOPPED on a coverage gap | SUPERSEDED | the gap was the unblock target of 0203 and the STOP was cleared by 0204 |
| `0204-rog-replication-ballcreek.md` | RoG replication on the Ball Creek fork (Coweeta) | SUPERSEDED | the replication stands as validation history; the RoG template it validated was re-authored by the migration, and the outlet it used was re-decided... |
| `0205-hecras-rog-headless.md` | HEC-RAS 2D RoG: headless precip-interpolation decode | DEAD | atticked |
| `0207-hecras-preproc-shims.md` | HEC-RAS preprocessing-shim hunt | DEAD | atticked |
| `0208-mesh-gate-adoption.md` | mesh precondition gate: SCHISM adoption, SWAN decline | DEAD | both adopters atticked; the gate itself deleted (see 0200) |
| `0209-hecras2025-rog.md` | HEC-RAS 2025 RoG productionized | DEAD | atticked |
| `0210-hecras-refined-mesh.md` | HEC-RAS 2025 RoG: paper-style channel-refined mesh | DEAD | atticked |
| `0211-generate-mesh-hecras.md` | `generate_mesh mode=hecras` | DEAD | `DELETION_LEDGER.md:1170` `tests/test_generate_mesh.py` DELETED 2026-08-27; `:1363` the `hecras_rog` mesher left with the purge |
| `0212-tidal-hydro-gate.md` | mesh precondition gate: schism_tidal_hydro adoption | DEAD | SCHISM atticked; gate deleted |
| `0213-rog-fidelity-ladder-2.md` | RoG fidelity ladder II: soil-moisture store + channel mesh | SUPERSEDED | the mesh front the composer was hiding is 0316's subject; the ladder rungs were re-decided there |
| `0214-landlab-groundwater.md` | Landlab groundwater front | DEAD | atticked |
| `0215-modflow-wellhead-reeval.md` | MODFLOW wellhead reeval: soil-derived K + kriged water table | DEAD | `fetch_water_table_depth`, `fetch_aquifer_transmissivity`, `fetch_aquifer_thickness` are live specs (0297/0298) |
| `0216-telemac-gaia-sediment.md` | TELEMAC GAIA v2 erodible-bed scour | SUPERSEDED | `telemac_river_scour` registers and `workflows/telemac/modules/gaia.py` is the live module |
| `0217-schism-pahm-surge.md` | SCHISM parametric-hurricane surge (synthetic shelf) | DEAD | superseded by 0219 the same week, then atticked |
| `0218-swmm-snowmelt-groundwater.md` | SWMM snowmelt + two-zone aquifer baseflow | DEAD | atticked |
| `0219-schism-pahm-surge-real-geography.md` | SCHISM PaHM surge on REAL Galveston geography | DEAD | atticked |
| `0220-openquake-site-model.md` | OpenQuake site-model batch (NEHRP amplification) | DEAD | atticked |
| `0222-provenance-audit.md` | provenance-transparency audit of the tool surface | SUPERSEDED | 0223's title: "audit 0222 remediation" |
| `0226-geoclaw-okada.md` | GeoClaw Okada seafloor-deformation product | DEAD | `grep -ri okada trid3nt_server workers` = zero |
| `0227-bathymetry-input-layer.md` | bathymetry-consuming templates surface topobathy as input | SUPERSEDED | 0231 generalizes it to every fetched input that shapes a run |
| `0228-modflow-uzt-csub.md` | MODFLOW UZT + CSUB | DEAD | atticked |
| `0229-deepwater-runup.md` | deep-water rung: ETOPO full column vs 3DEP ocean-fill | SUPERSEDED | the rung became a fallback-ladder row under `fallbacks/ladder.py` and NATE's migrate-all ruling |
| `0230-slab2-scenario.md` | Slab2 scenario source (earthquake-source ladder) | DEAD | `grep -ri slab2 trid3nt_server workers` = zero |
| `0235-modflow-gwe.md` | MODFLOW GWE (heat transport) archetype family | DEAD | atticked |
| `0236-tomawac.md` | TOMAWAC spectral-wave engine leg | DEAD | no `modules/tomawac.py` exists; TOMAWAC survives only as a name in `modules/describe.py` and `products/postprocess_telemac.py`. No registered template |
| `0237-artemis.md` | ARTEMIS phase-resolving harbour-agitation engine | SUPERSEDED | `artemis_harbor_agitation` registers and `modules/artemis.py` is live, but the physics-proof recipe was replaced by the declarative template; the A... |
| `0238-snapwave-nesting.md` | SFINCS SnapWave + nesting substrate gate | DEAD | atticked |
| `0239-elmfire-spotting.md` | ELMFIRE ember spotting | DEAD | atticked |
| `0240-gaia-sediment.md` | GAIA v3 multi-class graded sediment | SUPERSEDED | `modules/gaia.py` + `telemac_river_scour` / `telemac_river_sediment_plume` are the live surface |
| `0241-telemac3d.md` | TELEMAC-3D stratified/3D hydrodynamics leg | SUPERSEDED | `telemac3d_stratified_flow` registers via the workflow skeleton; `modules/telemac3d.py` is live |
| `0242-schism-icm-sed3d.md` | SCHISM ICM + SED3D substrate gate | DEAD | atticked |
| `0243-swan-nesting.md` | SWAN computational grid & nesting | DEAD | atticked |
| `0245-telemac-structures-misc-triage.md` | TELEMAC-2D Structures + misc triage (all STOP-RECIPE) | DEAD | a STOP-recipe sheet; none of its rows landed, and the module surface is now the `sheet.py` / dico path (0322 REANALYZE entry 2026-09-04) |
| `0246-triage-sweep-2-bouss-prt-lifelines.md` | triage sweep 2: GeoClaw boussinesq + MODFLOW PRT + HAZUS | DEAD | all atticked |
| `0247-hazus-lifelines.md` | HAZUS earthquake lifeline-network DL template | DEAD | atticked |
| `0248-tomawac-t3d-coverage.md` | TOMAWAC + TELEMAC-3D coverage close-out | DEAD | a board-reconciliation record for a board (`docs/validation/module-coverage-board.md`) the purge invalidated |
| `0249-hecras-coverage.md` | HEC-RAS coverage wave (2025 engine is 2D-only) | DEAD | superseded by 0250 within the day, then atticked |
| `0250-hecras-2d-structure-authoring-seam.md` | HEC-RAS 2025 2D structure-authoring seam | DEAD | atticked |
| `0251-hecras-2d-culvert-embankment-seam.md` | HEC-RAS 2025 2D culvert-through-embankment seam | DEAD | atticked |
| `0252-landlab-clusters.md` | Landlab coverage clusters + SFINCS Infiltration | DEAD | both atticked |
| `0253-mixed-clusters-sweep.md` | mixed-clusters triage sweep (0 landings) | DEAD | zero landings, all four families atticked |
| `0254-nestor-dredging.md` | NESTOR channel-maintenance dredging | DEAD | NESTOR survives only as keyword support inside `modules/gaia.py` and `authoring/assembler.py`; no NESTOR template registers |
| `0255-longtail-sweep.md` | long-tail triage sweep (12 rows, 6 families) | DEAD | all subjects atticked |
| `0256-knob-eligible-trio.md` | knob-eligible trio from the 0255 sweep | DEAD | both atticked |
| `0257-geoclaw-boussinesq.md` | GeoClaw Boussinesq (SGN) dispersive solver image | DEAD | atticked |
| `0258-modflow-gridgen-prt.md` | MODFLOW gridgen in the worker image; DISV PRT | DEAD | atticked; `workers/` holds only `mesh` and `telemac` |
| `0259-telemac-coastal.md` | TELEMAC-2D coastal tidal/surge substrate | SUPERSEDED | 0315 is "the coastal split"; the liquid-boundaries path was re-decided there |
| `0260-schism-icm-sed3d.md` | SCHISM ICM + SED3D targeted binaries BAKED | DEAD | atticked; "template registration is the remaining cheap step" never happened |
| `0261-server-refactor-wave1.md` | server.py refactor wave 1: package skeleton | SUPERSEDED | the skeleton it created was re-cut four more times; `server/` now holds `{config,errors,interactions,spatial}.py` + `dispatch/ protocol/ session/ t... |
| `0263-server-refactor-wave3-interactions-styles-spatial.md` | wave 3: interactions / styles / spatial extraction | SUPERSEDED | `server/{interactions,spatial}.py` live; the styles half moved again to `emission/` (0313) and then to `presets.py` (0326) |
| `0266-server-severed-consumers.md` | wave 6: severed-consumer chop (case-view snapshot + coldview) | DEAD | the subjects are gone; no standing constraint |
| `0267-severed-consumers-round-2.md` | wave 7: severed-consumer chop, round 2 | DEAD | same |
| `0268-notation-sweep.md` | waves 8-9: notation sweep (docstrings/comments) | SUPERSEDED | live norm: "comments = constraints ONLY; no ADR/job refs, no history, NO PERSON ATTRIBUTION" plus the new 3/5-line caps and the LLM-facing split |
| `0269-smell-to-code-audit.md` | wave 10: smell-to-code audit | SUPERSEDED | 0327 closes its last standing condition by name: *"This closes the condition ADR 0269 attached to the registry's TiTiler unwrap"* |
| `0270-feature-cuts.md` | wave 11: five feature-level cuts | DEAD | subjects gone |
| `0272-repo-unnesting.md` | repo unnesting: `server/src` -> `src` | SUPERSEDED | same-day successor moved it again |
| `0279-phase-e-sweep.md` | phase E: standards sweep + two relocations | DEAD | no standing constraint of its own |
| `0281-sclass-legs.md` | the GeoClaw + SWAN S-class legs | DEAD | both atticked |
| `0282-mclass-legs.md` | the M-class legs (SWMM + Landlab overland) | DEAD | both atticked |
| `0284-modflow-leg.md` | the MODFLOW transport leg | DEAD | atticked |
| `0286-schism-leg.md` | the L-class SCHISM native-mesh leg | DEAD | atticked |
| `0287-hecras-leg.md` | the HEC-RAS leg (two agent-side producers) | DEAD | atticked |
| `0288-elmfire-leg.md` | the ELMFIRE leg (derived burned-extent frames) | DEAD | atticked |
| `0295-swmm-out-of-process-lane-deletion-and-metrics-honesty.md` | the out-of-process SWMM lane dies; metrics honesty | DEAD | `mesh/_swmm_solve_subprocess.py` atticked; "the supervisor stops eating the metrics pointer" is a live honesty rule |
| `0296-geoclaw-manning-domain-split.md` | GeoClaw Manning siblings: split-by-domain NLCD wiring | DEAD | `IDEAS.md:4210` (2026-09-09) "THE MANNING TABLE STAYS OURS (Godara 2024) ... the comparison is recorded in `docs/validation/nlcd-manning-tables.md`" |
| `0302-mcp-server-v1.md` | the tool registry as an MCP surface (v1, stdio) | DEAD | `DELETION_LEDGER.md:1211-1214`: `trid3nt_server/mcp_server.py`, `tests/test_mcp_server.py`, `docs/design/mcp-server.md` and `.mcp.json.example` all... |
| `0303-declarative-library-v1-and-do-sag-migration.md` | declarative library v1 + the do_sag migration | SUPERSEDED | 0322 killed `Fetch.tool` / `Build.tool` with "no alias, no shim" and replaced the role prefix with the DATA class body. The library itself is live... |
| `0306-generalization-checkpoint-swmm-and-modflow.md` | the generalization checkpoint: one SWMM and one MODFLOW | DEAD | written 4 days before the purge took both engines; neither template registers |
| `0307-swmm-campaign-wave-a.md/0307-swmm-engine-campaign-wave-a.md` | *(untitled fragment)* SWMM wave-A post-review corrections | DEAD | 50 lines, **no `#` title**, and it duplicates the number of `0307-swmm-engine-campaign-wave-a.md`. It is a post-review addendum that should have be... |
| `0307-swmm-campaign-wave-a.md/0307-swmm-engine-campaign-wave-a.md` | SWMM engine campaign, wave A: standalone solve templates | DEAD | `workflows/swmm/` atticked 4 days later |
| `0308-telemac-bed-cog-honesty.md` | TELEMAC bed-COG manifest gap + existence check | SUPERSEDED | the in-worker bed COG was DELETED (`DELETION_LEDGER.md:987` "`workers/telemac/_bed_cog.py` + the in-worker bed COG - DELETED"); the bed now leaves... |
| `0314-the-static-plan-and-the-style-contract.md` | the static plan, and the style contract | SUPERSEDED | the static plan is BINDING (`workflows/runtime/` plan machinery); the `styles.yaml` style contract is gone - **no `styles.yaml` exists in the tree*... |
| `0321-suite-re-baseline-after-the-purge-and-the-chained-domain.md` | the suite re-baseline after the purge and the chained domain | SUPERSEDED | its own first line: "SUPERSEDED IN PART by ADR 0322: the two failures this note records are fixed and the standing baseline is now zero failures in... |
| `0323-suite-re-baseline-after-the-test-cull.md` | the suite re-baseline after the test cull | SUPERSEDED | a measurement, superseded by the next measurement; the live baseline fact is the memory norm "EXACTLY ZERO failures ... 5 slices incl contracts (789)" |

---

## 4. The docs layout

After the deletions, `docs/` holds six kinds of thing and nothing else. The organising rule
is the truth order of section 2.4: **`docs/` is where method, rulings and maps live - the
things code cannot carry and the model does not model.**

| directory | holds | rule |
|---|---|---|
| `docs/` root | the four live ledgers - `IDEAS.md` (rulings), `DELETION_LEDGER.md` (chop queue), `REANALYZE_LEDGER.md` (revisit triggers) - plus `CONVENTIONS.md` | append-only or queue-shaped; no new root files |
| `docs/decisions/` | ADR-lite records: one decision, its constraint, its evidence | a record is BINDING or it is DELETED; there is no "superseded" state that keeps a file |
| `docs/method/` (**new**) | the derivations, equations, citations and call chains that come out of module docstrings | one file per method, named for the method, not the caller |
| `docs/authoring/` | how to add a tool, a source spec, an engine, a template | grows with the surfaces; the "how" for a contributor |
| `docs/site/` | the user-facing manual: install, configuration, engines, models | the only prose written for a user rather than an author |
| `docs/model/` | the SysML model plus its generated views | suite-gated; never hand-edited |
| `docs/validation/` | conformance tables, censuses, ledgers - dated evidence of a wave | a file here is a measurement; when the thing measured dies, the file dies |
| `docs/research/` | primary-source recon per front | kept while the front is open |
| `docs/specs/` | contracts pinned before a wave builds | a spec is deleted when its wave closes, unless another doc cites it as authority |
| `docs/playbooks/` | code-exec recipes replacing culled tools | one per demoted tool; dies with the tool |
| `docs/proof/templates/` | frozen render proofs | never pruned without an explicit say-so |
| `docs/reports/` | the July bench evidence | frozen |

### Where embedded docs go

| what is embedded today | where it goes |
|---|---|
| a module docstring that is a subsystem essay (31 of the 40 longest) | the SysML model for structure and requirements; `docs/method/` for derivation; a 5-line header stays |
| "sibling of X", "the analogue of Y", "lives next to Z" | the package's map README |
| a per-field roll-call of a typed Pydantic model | deleted; the type is the truth |
| `WHY THIS EXISTS`, "we used to", "the twin was deleted" | deleted; git is the archive |
| an operator walkthrough (`plugin_repo.py`, `install_dependencies.py`, `assemble_proof_packet.py`) | `docs/site/` for the user half, `docs/authoring/` for the author half |
| a method derivation (RUSLE ladder, NDWI, Matson-Dozier thresholds, CIRA passes, the pfdf call chain, Zell & Sanford provenance) | `docs/method/`, one file each; the constants keep a 2-line constraint comment at the call site |
| the four package maps in `docs/design/` (`adapters.md` 71, `gates.md` 56, `server.md` 58, `persistence.md` 32) | `trid3nt_server/{adapters,gates,server}/README.md`; `persistence.md` folds into the server map |
| `docs/design/emission.md` (315) | split - a short map to `trid3nt_server/emission/README.md`, the method to `docs/method/` |
| `docs/design/server-package.md` (111) | folds into the server map; its four relative links are broken and its opening cites an "Appendix-A WebSocket protocol" that is not in this repo |
| the GSHHG shoreline ladder in `workflows/mesh/README.md` | `docs/site/configuration.md` |
| the macOS matplotlib wheel recipe in `plugin/README.md` (60 lines) | `docs/site/install.md` |
| the variant law in `docs/proof/templates/README.md` (254) | `docs/method/proof-variants.md`; a map stays |

`docs/design/` empties to seven standing design documents (`calibration-methodology.md`
1,022, `declarative-workflows.md` 848, `external-fetch-audit.md` 702, `outputs-manifest-schema.md`
480 FROZEN, `demo-physics-defaults-audit.md` 454, `fallback-audit.md` 338,
`fallback-ladders.md` 252). **Recommendation: rename it `docs/design/` -> keep, but move the
three audits to `docs/validation/`**, which is where dated measurements belong, leaving four
standing designs.

### The map-README rule

- **Every package directory that a contributor navigates gets a README, and it is a MAP.**
  One paragraph of what the package is for, then a table of files/subfolders with one line
  each. Target 20-50 lines; the eight `workflows/telemac/*/README.md` files (19-50 lines each,
  218 total, all accurate) are the reference standard.
- **A README states no method, no rationale, no history, no person.** The moment a README
  explains *how* to do something or *why* a choice was made, that half moves to `docs/`.
- **A README never duplicates a `docs/design/` page.** Where both exist today the docs page
  loses and its content moves into the package.
- **A README is not generated but it is checked**: the lint test asserts that every file and
  subfolder named in a map README exists, and that every top-level `.py` in the package is
  named. That is what makes the four stale maps of section 3.2 impossible to write again.
- The twelve packages currently without a map (four with a `docs/design/` page to move in,
  eight with nothing) get one.

### `.gitignore`

One line: `docs/validation/code-graph/graph.json` (1.0 MB, machine-only, regenerated wholesale
by `scripts/code_graph.py`, read by nothing, and it dirties the index on every run - observed
live during this census). The three markdown reports beside it stay tracked. Do NOT ignore
`docs/model/*-view.md` - the suite fails while one is stale, and untracking them removes the
gate. `docs/specs/processing-redundancy-candidates.json` is deleted with its parent report,
not ignored.

---

## 5. The README outline

### 5.1 The root `README.md`

Today: 125 lines, last touched 2026-09-04. It names `server/` four times for a package that
is `trid3nt_server/`; it calls the product "an AI workbench for multi-hazard geospatial
modeling" against the standing geospatial-intelligence identity; it promises "Real solvers
run locally: MODFLOW 6, TELEMAC, SFINCS, SWMM, and more" when TELEMAC is the only engine in
the tree; it lists `data/` in the layout block (deleted and gitignored); and the module
surface, the mesh recipe, the run journal, `contracts/`, `docs/` and the offline suite are
absent. What it gets right and keeps: the three install paths with their real make targets
(`setup`, `up`, `plugin`, `plugin-zip`, `status`), the service URL table, the tailnet
paragraph, the `.env.local` LLM block, the three deploy seams.

Target ~90 lines. A map plus the three install paths; everything else is a link.

| # | section | one line of content |
|---|---|---|
| 1 | What TRID3NT is | a QGIS plugin plus a local daemon: ask in plain language, get real layers on your own canvas, on your own machine |
| 2 | What it can do today | fetch measured data from 97 declared sources through one router; compose analyses in the code-exec playground; author, mesh, run and read back a TELEMAC hydrodynamic run |
| 3 | The one loop | ask -> the router fetches -> the mesh front builds a recipe and holds it at a gate -> a template fills the module sheet -> the box solves -> layers, charts and a packet land in QGIS |
| 4 | The module surface | a template declares raw engine keywords over a wrapper built from the engine's own dictionary; fill and run are the two acts; nothing is hidden |
| 5 | The engines | TELEMAC (2D/3D, ARTEMIS, WAQTEL, GAIA) in a container; the eight registered templates in one table, each with the question it answers |
| 6 | Install | the three paths (daemon-only / client-only / both), the make targets, the prerequisites; full walkthrough linked to `docs/site/install.md` |
| 7 | The LLM | pluggable through the OpenAI-compatible seam or Anthropic; set in `.env.local`, switched live from Settings |
| 8 | Service URLs | the four-row table (agent WS, agent HTTP, MinIO, optional Ollama) |
| 9 | Repo layout | `plugin/ trid3nt_server/ contracts/ workers/ scripts/ tests/ docs/`, one line each, each pointing at its own README |
| 10 | Deploy seams | the three - server = restart, plugin = `make plugin` + reload, worker = rebuild the image - each with its one command |
| 11 | Where the documentation lives | `docs/site/` the manual, `docs/authoring/` how to extend, `docs/method/` how it is derived, `docs/decisions/` why, `docs/model/` the checked model, `AGENTS.md` the charter |

Removed outright: the `server/` layout row, the multi-hazard framing, the five-engine
promise, `TRID3NT_MODFLOW_LOCAL=1` and the `mf6` binary in `make setup` (documented as
first-class with no engine behind them), the `data/` row.

### 5.2 The map-README skeleton, for the twelve packages that lack one

Four with a `docs/design/` page to move in (`adapters/`, `emission/`, `gates/`, `server/`)
and eight with nothing (`cases/`, `credentials/`, `fallbacks/`, `sandbox/`, `testing/`,
`scripts/`, `tests/`, `contracts/trid3nt_contracts/`). The eight
`workflows/telemac/*/README.md` files (19-50 lines each, 218 total, all accurate against the
tree) are the reference standard; the skeleton is theirs:

```
# <package>

<One paragraph: what this package is for, in the caller's terms, and the one
constraint that holds across every file in it.>

| file | what it is |
|---|---|
| `x.py` | one line |

| subfolder | what lives there |
|---|---|
| `y/` | one line, pointing at its own README |
```

20-50 lines. No method, no rationale, no history, no person. Every top-level `.py` is named
or the package declares a subfolder table; every name in the tables exists. Those last two
are the assertions the lint test makes, and they are what makes the four stale maps of
section 3.2 impossible to write again.

---

## 6. The wave's stages and verification

Nine stages. Every stage ends green on the five-slice offline suite (contracts included,
789) and with `scripts/loc_report.py` re-run: **pure LOC must not move**, because docstrings
and comments are already excluded from it. Commits are path-scoped (`git commit -- <paths>`)
while other waves are in flight, and moves use `git mv` so history follows the file.

| stage | what lands | verification |
|---|---|---|
| **S0 lift** | the surviving revisit triggers out of ADRs 0049, 0050, 0091, 0321 into `REANALYZE_LEDGER.md`; the surviving clause out of the eight split records (0005, 0022, 0055, 0075, 0263, 0295, 0303, 0314) into their successor or the ledger | a diff review: every lifted trigger has a DECIDED / evidence / REVISIT TRIGGER triple; `grep` proves no other file states it |
| **S1 the lint test** | `tests/test_docstring_standard.py` in the offline suite, beside `tests/test_model_conformance.py`; `docs/CONVENTIONS.md` rewritten to the ruleset of section 2 and cited by the test | **seeded break, three seeds**: a 6-line docstring on an internal function, a `sprint-99` token in a comment, an 1,100-char front on a `@register_tool` docstring. All three fail the suite; reverting all three restores green. The test is RED at this point against 2,648 real violations, so it lands `xfail`-free only at S3 - land it skipped-by-marker and flip the marker at S3, or land it last; either way the seeded break runs at S1 |
| **S2 mechanical cut** | T1 only: non-summary paragraphs matching a spec-notation / history / attribution / example / `WHY THIS EXISTS` marker (255 docstrings, -2,098 lines) and narration-only comment blocks over 6 lines (47 blocks, -611 lines, including the 146-line deleted-twin changelog in `trid3nt_server/tools/__init__.py`) | regex-driven, one commit per top-level directory; suite green; `loc_report` pure LOC delta = 0; the banned-token regex of section 2.4 returns zero hits over product code |
| **S3 the 40 longest** | the hand leg: 40 docstrings, 2,536 lines, 2,358 of them excess. Each line goes to its destination - the SysML model, `docs/method/`, a map README, or deleted. This leg seeds `docs/method/` | one commit per destination class; `scripts/model_check.py` green on every new model element (per-usage evidence, pass-through ends); each new `docs/method/` file is reachable from a README or a model view |
| **S4 the sweep** | the remaining T2 excess across the other 2,608 over-limit internal docstrings (~21,000 lines) and the narrative half of the 49 constraint+narration comment blocks plus the 143 method-not-rule explainers (~1,200 lines) | the lint test flips to enforcing; suite green; `loc_report` shows docstrings 33,970 -> ~10,804 and product file lines 175,270 -> ~150,304 with pure LOC unchanged |
| **S5 the doc deletes** | the delete list of section 3: 273 markdown files and 1 json, 45,151 markdown lines. One commit per directory, its message naming the count and the evidence class | before each: `git grep -l "<filename>"` returns only the file itself and this census (three files carry a VERIFY FIRST rider - `elegance-review.md`, `temporal-endpoint-inventory.md`, `frame-animation-recipe.md`); after all: `docs/decisions/` holds 106 records and the index lists all 106 |
| **S6 the moves** | the four package maps out of `docs/design/` into their packages; the three audits out of `docs/design/` into `docs/validation/`; the method halves out of `workflows/mesh/README.md`, `workflows/telemac/README.md`, `plugin/README.md` and `docs/proof/templates/README.md`; `afk-ledger-2026-08-24.md` out of `docs/decisions/` | `git mv` for every one; no orphan links (`grep -rn "docs/design/\(mesh\|workflows\|data\|adapters\|gates\|server\)" .` returns zero) |
| **S7 the READMEs** | five rewrites (root, `contracts/`, `plugin/`, `docs/decisions/`, `docs/validation/`), twelve new package maps, and the two `AGENTS.md` laws that name missing commands | the README lint assertion of section 5.2 passes on all 34 maps; Law 3 names a command that exists and runs green |
| **S8 close** | one `IDEAS.md` ruling line per stage with its measured delta; `DELETION_LEDGER.md` rows for the deleted docs classes; the `.gitignore` line for `docs/validation/code-graph/graph.json` | the spec-conformance gate: a fresh-eyes clause-by-clause table of this document's section 2 against the landed lint test, plus a live walkthrough of one `@register_tool` docstring, one internal module and one map README |

**The sweep guard**, per the paradigm-waves law: this document is the inventory and the
verdict table. The guard is the lint test itself for docstrings and comments, plus one
assertion for docs - a test that fails if a new file appears in `docs/specs/` or
`docs/validation/` whose first 20 lines carry a `STATUS: THINKING` / `PROPOSAL ONLY` /
`FOR NATE REVIEW` marker and whose git-add date is older than 30 days. That is the exact
shape of every file on the delete list, and it is the only rule that stops the folder
refilling.

**Not in this wave, and why**: the `DELETION_LEDGER.md` rollup (question 6) touches the live
work queue; the `IDEAS.md` re-sectioning (question 7) is a 3,620-line mechanical diff that
would drown every other stage's review. Both are their own commits, sequenced after.

---

## 7. Design questions for NATE

### Q1. The exact line limits

Measured sensitivity, over the 4,245 internal docstrings (excess = lines that must move):

| module limit | meet | excess | | function/class limit | meet | excess |
|---|---|---|---|---|---|---|
| 3 | 44 / 501 | 8,814 | | 2 | 1,436 / 3,744 | 17,568 |
| 4 | 50 | 8,357 | | 3 | 1,533 | 15,260 |
| **5** | **64** | **7,906** | | **3** | **1,533** | **15,260** |
| 6 | 82 | 7,469 | | 4 | 1,621 | 13,049 |

`docs/CONVENTIONS.md:14` already says module docstrings are "<= 3 lines: what lives here" -
**tighter than the proposed 5** - and `:15` says private helpers get NONE, and `:11` says
LLM-facing docstrings are "RICH". So the proposed ruleset is looser in one place and
stricter in two, and the existing convention file contradicts it either way.

**Recommendation: 3 for functions and classes, 5 for modules, and rewrite `CONVENTIONS.md`
to match.** The module limit buys 908 lines by going from 5 to 3, which is 1.8 lines per
file, and costs the one thing a module header is for: a name line plus a module-wide refusal
that every symbol in the file inherits (a datum, a thread rule, an import-time side effect).
The function limit is where the mass is - 15,260 lines - and 3 is where a contract stops
being a paragraph. Drop the CONVENTIONS "private helpers: NONE" row: a flat 3 covers it, and
a rule that forbids a docstring is a rule people work around by writing a comment instead.

### Q2. Do module docstrings state constraints, or point at the map?

**Recommendation: state constraints; never point.** The five lines are one line of what
lives here plus up to four lines of the constraint that holds across every symbol in the
file - the thing a reader of any one function in it would otherwise miss. A pointer
("see the package README", "sibling of X") is a link that rots, and the census found 151
docstrings / 2,719 lines already doing exactly that. The map is discovered by directory, not
by docstring; the neighbours belong to the map README (section 5.2) and the structure to the
SysML model. If a module has no module-wide constraint, the header is one line, and that is
the correct outcome for most of the 501.

### Q3. What to do with the 40 longest

Measured: 2,536 docstring lines across 40 symbols, 2,358 of them excess, **29 of the 40 are
module headers**, and they sit in `trid3nt_server` (23), `contracts` (11), `plugin` (4),
`scripts` (2). Not one is an `Args:`/`Returns:` block; the length is prose. They are the only
population in the wave where the destination is not mechanical - the same file splits across
delete, `docs/method/`, the model and a map.

**Recommendation: a dedicated hand leg (S3) before the mechanical sweep, one commit per
destination class.** They seed `docs/method/` with real content, and they are the population
that proves the destination taxonomy of section 2.4 works before it is applied 2,600 more
times. The alternative - cutting them to 5 lines with everything else - discards ~2,000 lines
of RUSLE ladders, NDWI derivations, the pfdf call chain, the CIRA passes and the Zell &
Sanford provenance, which is method worth keeping and currently invisible because it is
buried in 30 module headers.

### Q4. BINDING records and live files that contradict a later ruling

Seven, each with a recommendation:

| # | the contradiction | recommendation |
|---|---|---|
| a | `docs/decisions/README.md:5` - "never rewrite history - supersede with a new note that links back" - versus the 2026-09-09 delete-outright ruling | rewrite the convention with the ruling. This sentence is why the folder reached 45,614 lines in 45 days |
| b | `docs/CONVENTIONS.md:11,14,15` - the old budget ("LLM-facing RICH", "module <= 3 lines", "private helpers NONE") versus the new split and the 3/5 caps. `:40` also sets a "well under 20%" documentation share; the tree measures 20.7% | rewrite the whole table to section 2, keep the 20% share line as the standing target, and make the lint test cite this file |
| c | ADR `0023-us-only-paper-first-replication.md` BINDING, but its US-only clause was refined 2026-08-26 to "cases wherever gauges our substrate can FETCH" | amend in place - the record survives the chop, so the title and the clause should say what is actually true. Paper-first is unchanged |
| d | ADR `0004-zero-legacy-naming.md` BINDING but unmet in its own Layer B: `GraceModel` is the base class of every contract (`contracts/trid3nt_contracts/auth.py:49,65`, `catalog.py:47,86`) and `persistence.py:866,893,896` still migrates `~/.grace2` | keep BINDING; move the identifier rename into `DELETION_LEDGER.md` as a QUEUED row with its CONDITION, so an unmet clause has a queue entry rather than living as a permanently-false ADR |
| e | ADR `0015-vendored-wheel.md` BINDING but names `server/wheels/`; the path is `wheels/` after 0272/0274 | fix the path line in place |
| f | ADR `0320-spec-format.md` BINDING ("one self-contained HTML page per spec, mermaid for all diagrams") versus `docs/model/` being the suite-checked structure surface; `docs/specs/system-uml.html` is the overlap and the only one of the eight HTML specs with a live replacement | 0320 keeps HTML for a wave contract, but a diagram of LIVE structure belongs to the model. Delete `system-uml.html`, or demote it to a rendering generated from `docs/model/` |
| g | `AGENTS.md` Law 3 requires the flood canary `scripts/run_sfincs_direct.py` (**the file does not exist**; the flood direct-call canary is retired) and Law 7 pins `EXPECTED_TEMPLATES`, which lives only in `tests/test_door_dissolution.py:31` | re-point Law 3 at the one flagship canary (authored mesh -> `.supplied()` -> solve -> full packet) and give Law 7 the test path. A law naming a missing command is a law nobody can satisfy |

### Q5. The V&V "STATUS: THINKING" stratum

Six files, 358 lines, under `docs/validation/`: `research.md` (194), `open-questions.md` (42),
`agentic-loop.md` (40), `responsibility-cut.md` (34), `activation-boundary.md` (31),
`roadmap-proposal.md` (17). Their own README declares the whole folder "STATUS: THINKING -
nothing here is implemented or approved", which is now wrong for 30+ of its 45 files. The
live successor is `docs/design/calibration-methodology.md` (1,022 lines, awaiting sign-off).

**Recommendation: fold `research.md` into the calibration methodology as its background
section and delete the other five (164 lines).** They are open questions the methodology doc
either answers or supersedes, and the README's status line goes with them.

### Q6. The `DELETION_LEDGER.md` rollup

439 rows: 353 DELETED, 36 QUEUED, 9 REJECTED, 4 SCOPE-ATTIC, 1 CONDITION-MET. The 353
terminal rows are ~2,400 of the file's 3,405 lines and their whole content is "this used to
exist, here is the commit", which is what `git log` answers.

**Recommendation: roll the 353 up into one dated line per wave, keep all 50 non-terminal
rows, and promote the three structural narrative sections (`:1218` the fresh-start purge,
`:1125` the mesh-builder dissolution, `:653` `trid3nt_server/data/`) to `docs/method/` notes -
they are the only prose record of those moves and ADRs 0321 and 0327 cite them.** Sequence it
AFTER the doc wave: it touches the live work queue, and a queue edit inside a 45,000-line
deletion diff will not get read.

### Q7. `IDEAS.md` re-sectioning

Lines 668-4287 - 3,620 lines, 84% of the file - sit under a single `## 2026-08-24` heading,
carrying 213 date-stamped rulings addressable only by line number, which the file's own
convention at `:659` warns against. Five ADRs and two ledger rows cite it with no anchor.

**Recommendation: restore one `## <date> - <title>` heading per ruling and generate a heading
index at the top. Delete nothing** - it is the rulings record. It is a mechanical
re-sectioning with zero content change, and it belongs in its own commit for exactly that
reason.

### Q8. Does `docs/method/` exist as a directory?

The wave produces ~5,300 lines of relocated method (derivations, equations, citations, call
chains) with no home today; `docs/design/` currently mixes standing designs, stale package
maps and dated audits.

**Recommendation: yes - create `docs/method/`, one file per method named for the method and
not for its caller.** `docs/design/` then holds four standing designs
(`calibration-methodology.md`, `declarative-workflows.md`, `fallback-ladders.md`,
`outputs-manifest-schema.md` FROZEN) after the three audits (`external-fetch-audit.md` 702,
`demo-physics-defaults-audit.md` 454, `fallback-audit.md` 338) move to `docs/validation/`,
which is where dated measurements belong.

### Q9. Does the lint test check map READMEs too?

**Recommendation: yes, same test module, two assertions - every file and subfolder named in a
map README exists, and every top-level `.py` in the package is either named or covered by a
declared subfolder table.** It is the only mechanism that makes the four stale maps of
section 3.2 impossible to write again, and the escape hatch (the subfolder table) keeps it
from punishing a package with forty modules. This is also the one new rule with real
false-positive risk, so it lands last, after the twelve new maps are written.

### Q10. The exemption mechanism

**Recommendation: ship `# docstring-exempt: <reason>` with an empty ledger at
`docs/validation/docstring-exemptions.md`, regenerated and diffed by the test - the pattern
`worker-loc-ledger.md` already uses. No directory or glob exemptions**: a glob is how
`contracts/` reached 15.2 docstring lines per symbol. If the ledger passes ~10 entries, the
limit is wrong and should be re-argued, not routed around. The two candidates today - the
`_ALWAYS_OFFLOAD_SYNC_TOOLS` justification table (`server/dispatch/emitter.py:174`) and one
or two physics-constant derivations - are both better served by moving to `docs/method/`.

### Q11. Frozen evidence: three rulings the census surfaced but will not take

`docs/proof/templates/` is 654 MB, 98% of `docs/` by bytes, and its README says it is never
pruned without an explicit say-so. Three facts want a ruling rather than an action:

1. **Three proof directories have no live template**: `swan_nested_grid/` (92 KB),
   `coastal_tidal_surge/` (42 MB), `tomawac_wave_field/` (4.8 MB). **Recommendation: delete
   the SWAN and TOMAWAC directories** (both engines are gone and TOMAWAC has no wrapper);
   keep `coastal_tidal_surge/` until the coastal split's own proofs land.
2. **Three live templates have no proof directory**: `river_oil_spill`, `river_scour`,
   `river_sediment_plume`. **Recommendation: a proof-coverage assertion in the same lint
   test** - a registered template without a proof directory is a gap, not a silence.
3. **Three evidence JSONs exceed 1 MB** (`telemac_river_dye_refined_canary_evidence.json` is
   7.5 MB). They are machine output, not renders. **Recommendation: keep, but say so in the
   README's own law** - the "FOUR variants and no more" rule governs renders and is silent
   about evidence blobs, which is why they grew unnoticed.
