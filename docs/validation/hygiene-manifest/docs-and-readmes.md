# Hygiene manifest - docs (except decisions and proof) + every directory README + AGENTS.md + CONVENTIONS.md + the repo README

Scope listing method, stated so the count is reproducible:

    git ls-files docs | grep -v '^docs/decisions/' | grep -v '^docs/proof/'   -> 172
    git ls-files | grep -iE '(^|/)README(\.[a-z]+)?$' | grep -v '^docs/'      ->  18
    AGENTS.md                                                                  ->   1
    the four READMEs under docs/decisions/ and docs/proof/                     ->   4
                                                                       union  -> 191

`docs/CONVENTIONS.md` and the repo `README.md` are members of the first two
sets, so the task's explicit naming of them adds no row. The scope sentence
excludes `docs/decisions` and `docs/proof`, and the same sentence's next clause
asks for "the READMEs of every directory": the four README files inside those
two trees are the intersection, and they are ROWED here rather than dropped, so
no file in either clause goes unread. The decision RECORDS themselves
(`docs/decisions/0*.md`) are the sibling `docs-decisions.md` lens's subject and
are deliberately absent from this table.

EXCLUDED AT THE FIRST PASS, NOW ROWED: thirteen sibling files under
`docs/validation/hygiene-manifest/` were untracked when the first pass ran
(contracts.md, docs-decisions.md, emission.md, plugin.md, scripts-workers.md,
tools-processing.md, the four `trid3nt_server-*.md`, workflows-mesh.md,
workflows-runtime-solver-shared.md, workflows-telemac.md) - in-flight products
of concurrent lenses, each owned by the agent writing it, which `git ls-files`
did not list. They are committed now, and so are COVERAGE.md,
tests-plugin-contracts.md, this file, `docs/READABILITY_LEDGER.md` and
`docs/validation/docstring-exemptions.md`. All EIGHTEEN were read end to end by
the agent adding their rows below, alongside the one sibling the first pass did
row, `hygiene-manifest/trid3nt_server-tools-fetchers.md`. The scope is therefore
209 rows: the 191 the first pass enumerated plus these eighteen.

Column shape is the task's docs/README shape: path | lines | what it is |
stale claims | fate. "Lines" is physical lines (`wc -l`), not pure LOC - none of
these files is code.

Method for the stale-claims column: for every path, symbol, tool name, count or
subsystem a file names, the claim was checked against the live tree at HEAD
(`main`), not against the file's own assertion. The recurring finding has one
shape: eleven engine families (SFINCS, SWMM, MODFLOW, GeoClaw, SWAN, Landlab,
OpenQuake, ELMFIRE, Pelicun, SCHISM, HEC-RAS) left at the 2026-08-28
fresh-start purge and only TELEMAC survives, so a doc that names one of them as
a live subsystem is stale by that fact alone. The second recurring shape is the
decommissioned substrate: AWS/GCP, AWS Batch, TiTiler, QGIS-Server WMS,
DynamoDB, Cognito, Bedrock, the web client.

## A. Charter and root

| path | lines | what it is | stale claims | fate |
|---|---|---|---|---|
| AGENTS.md | 117 | The canonical charter: the ten standing laws, the suite law, the commit and push rules, the deletion-ledger norm, the wave close-out gates. CLAUDE.md was de-duplicated onto it 2026-09-03. | Names the five alphabet slices as the suite shape; TESTS RULED replaces those with six named Makefile targets. Its engine vocabulary is TELEMAC-only and current. | REWRITE (suite-law paragraph only; the charter itself is live and binding) |
| README.md | 125 | The repo's front door: what TRID3NT is, the plugin-plus-daemon shape, install, the make targets, the layout map. | Layout map and capability prose predate the fresh-start purge and the module-surface wave; DOCS CENSUS RULED §5.1 specifies a ~90-line, 11-section replacement. | REWRITE (per DOCS CENSUS RULED 5.1) |
| docs/CONVENTIONS.md | 44 | Naming, layout and comment conventions for the repo. | Thin against the standard the charter now carries (the docstring limit, the comment-block rule, the census's map-README skeleton) - it under-states rather than mis-states. | REWRITE |

## B. Directory READMEs (the product tree)

| path | lines | what it is | stale claims | fate |
|---|---|---|---|---|
| contracts/README.md | 90 | Map of the `trid3nt_contracts` package: the wire models, the schema export, the regeneration command. | Names contract modules the test-cull scope-2 sweep atticked (the per-engine `*_contracts.py` family) and describes an engine roster that is gone. | REWRITE |
| plugin/README.md | 245 | Map of the QGIS plugin: dock, gate cards, render seam, net client, the stdlib-only connection-layer clause, the install script, the Qt-harness tier. | The stdlib-only clause at :204-225 is LIVE and load-bearing (lean sweep L8 defers against it). Roster and version prose drift with each plugin bump. | REWRITE (trim to the census's map skeleton; keep the stdlib clause verbatim) |
| workers/README.md | 26 | Map of `workers/`: the one surviving TELEMAC engine room, the `--network none` posture, the image-rebuild rule. | Cites `docs/site/engines.md`, which is itself RE-CUT below; otherwise current - the seventeen purged worker dirs were already scrubbed from it. | KEEP |
| trid3nt_server/tools/README.md | 48 | Map of `tools/`: fetchers (the `_router` engine, its executors and transforms including `join.py`), processing, search, meta, display. | `_router/transforms/join.py` is named as present; the ledger records it DELETED/attic-supplied with the demographic scope move, so the transforms list over-states by one module. | REWRITE (one line) |
| trid3nt_server/workflows/README.md | 48 | Map of `workflows/`: `mesh/`, `runtime/`, `shared/`, `solver/`, `telemac/`. | None found - it was repointed when the `shared/` orphans left. | KEEP |
| trid3nt_server/workflows/mesh/README.md | 56 | Map of the mesh front: the recipe, `mesh_op`, the two meshers (`om2d`, `reg_grid`), the drivers, the GPL-isolated image. | None found. | KEEP |
| trid3nt_server/workflows/telemac/README.md | 45 | Map of the TELEMAC tree: the eight subdirectories, the eight templates, the stated absence of a TOMAWAC wrapper with its measured reason. | None found; the TOMAWAC absence is stated honestly and matches the tree. | KEEP |
| trid3nt_server/workflows/telemac/authoring/README.md | 24 | Map of `authoring/`: the assembler (settle + stage) and the one serializer over telapy `TelemacCas`. | None found. | KEEP |
| trid3nt_server/workflows/telemac/catalog/README.md | 24 | Map of `catalog/`: the six dico-extracted keyword JSONs and the extraction/drift-audit rule. | None found; the six JSONs and their keyword counts are present. | KEEP |
| trid3nt_server/workflows/telemac/helpers/README.md | 29 | Map of `helpers/`: uniform flow, dredging, oil, forcing, errors, catchment, infiltration, release point/layer. | None found. | KEEP |
| trid3nt_server/workflows/telemac/modules/README.md | 50 | Map of `modules/`: the per-module wrappers (telemac2d, telemac3d, artemis, gaia, waqtel, tomawac) and the composite/keyword rule. | None found. | KEEP |
| trid3nt_server/workflows/telemac/products/README.md | 25 | Map of `products/`: the publishers, the result reader, the results-mesh seam, Streeter-Phelps. | None found. | KEEP |
| trid3nt_server/workflows/telemac/solving/README.md | 20 | Map of `solving/`: dispatch, wait, download, `run_telemac.py`. | None found. | KEEP |
| trid3nt_server/workflows/telemac/templates/README.md | 27 | Map of `templates/`: the eight question-class templates and the door they hand over. | None found; the eight named templates match the tree. | KEEP |
| trid3nt_server/workflows/telemac/templates/shared/README.md | 19 | Map of `templates/shared/`: what two or more templates hold in common. | None found. | KEEP |

## C. docs/authoring

| path | lines | what it is | stale claims | fate |
|---|---|---|---|---|
| docs/authoring/adding-an-engine.md | 118 | How to land a new engine: the template, the facade process table, the worker. Rewritten off its MODFLOW-era body in the 2026-08-31 dead-name scrub. | None found - this is the file the scrub already fixed, and README links it as the authoring entry point. | KEEP |
| docs/authoring/writing-a-tool.md | 374 | How to author a registered tool: the decorator, the metadata, the corpus, the docstring front budget, the declarative `source.yaml` path. | Examples reference processing tools and fetchers that survive; the router vocabulary predates the vector/hydro fold stages (names `zip_vector`, the `vector_fgb` fetch half and the shared `overpass.py` hook, all three DELETED). | REWRITE |

## D. docs/design

| path | lines | what it is | stale claims | fate |
|---|---|---|---|---|
| docs/design/adapters.md | 71 | The LLM adapter seam: provider selection, the genai IR, the per-provider adapters. | Names the Bedrock adapter as a live path; `adapters/bedrock_adapter.py` was deleted whole in the lean sweep (Q1) and the default provider is `openai`. | REWRITE |
| docs/design/calibration-methodology.md | 1022 | The calibration track's methodology: canonical cases, observation pairing, skill metrics, the paper-first rule, the NATE-signs-methodology-first rule. | Its worked examples are SFINCS/Harvey and Malpasset, both chopped; the METHOD is the live commitment and is unaffected. | KEEP |
| docs/design/data.md | 43 | A stub map of the data layer. | Named in DOCS CENSUS RULED §3.1 as a delete: superseded by the fetcher-fold census and the tools README. | DELETE (census 3.1) |
| docs/design/declarative-workflows.md | 848 | The design of the declarative plan language: `plan(ops)`, gates, `When`, `Ref`, the interpreter, the ledger. | The plan LANGUAGE died at module-surface stage 3 (`FormGate`, `DrawGate`, `When`, `STAGES`, `slots.py`, `form.py` all grepped to zero). Its illustrative constructors do not exist. The rerun/ledger half survives. | REWRITE (down to the surviving runtime: `Plan`/`Step`/`ChartSpec`, the ledger, rerun) |
| docs/design/demo-physics-defaults-audit.md | 454 | The ADR 0285 law-9 audit: every baked demo physics constant, its consequence class and its derive-or-refuse disposition. | Carries a stray `</content>` artifact at the tail (a real defect in the file). Rows 8-11, 16, 19, 23, 27 and the P6-P8 tail all name Landlab/GeoClaw/SWMM/SCHISM/HEC-RAS workflows that left the tree. | DELETE |
| docs/design/elegance-review.md | 290 | The elegance-review panel's findings (P1-P7) against the mesh and workflow fronts. | Every P-item it raises is executed and ledgered (the second mesh front, `precondition_gate`, `MeshPolicy`, `min_spacing`, the ladder). A findings list whose findings are all closed. | DELETE |
| docs/design/emission-campaign-cadence-recon.md | 159 | Recon for the emission campaign: where frames and cadence were decided across the engines. | Its subject engines (SFINCS/GeoClaw/SWAN/SWMM/Landlab frame emitters) are all in the attic. | DELETE |
| docs/design/emission.md | 315 | The emission seam's design: the one publish chokepoint, the presets, the legend, the store. | Predates the emission fold: names `styles.yaml`, the quantity-keyed preset zoo, `LegendKey.classes` and the WMS/TiTiler display faces, all deleted (ADR 0326/0327). The live design is four preset KINDS plus `restyle_layer`. | REWRITE |
| docs/design/external-fetch-audit.md | 702 | The audit that found every network call outside the router - the basis for the no-double-middleware law and the fetch migration. | Its site list is mostly engine workers that left; the LAW it established is live and stated in AGENTS.md. | DELETE |
| docs/design/fallback-audit.md | 338 | The audit of every silent substitution site, the parked register, and the loudness gate it produced. | Names `_router/stratified.py` as live; it was atticked in the lean sweep (Q2). The parked register it describes now has one row ("11"). | DELETE |
| docs/design/fallback-ladders.md | 252 | The declared-ladder design: rungs, activation rows, the cross-dataset gate. | Live design - `.ladder()` executes and the loudness gate is in the suite. Its example rungs name a couple of purged fetchers. | KEEP |
| docs/design/gates.md | 56 | The gate design: form gates, draw gates, the confirm lane, the spatial-input spine. | `FormGate` and `DrawGate` are gone from the runtime; what survives is the card the sheet renders and the spatial-input gate. | REWRITE |
| docs/design/local-model-upgrade-2026-07.md | 226 | The July local-model upgrade record: the qwen3 8b/9b comparison and the routing-harness procedure. | Dated record; it is the file `docs/site/models.md` cites as the rerunnable procedure, so it is load-bearing history rather than a claim about now. | KEEP |
| docs/design/local-roadmap-2026-07-06.md | 137 | A dated roadmap for the offline build. | Every item is either landed or superseded by the campaign rulings in IDEAS.md; a roadmap whose successor is the rulings record. | DELETE |
| docs/design/mesh.md | 31 | A stub map of the mesh layer. | Named in DOCS CENSUS RULED §3.1 as a delete: superseded by `workflows/mesh/README.md` and the mesh-recipe spec. | DELETE (census 3.1) |
| docs/design/mesh-wave-kickoff.md | 94 | The mesh wave's kickoff brief. | The wave closed; `docs/validation/mesh-wave-conformance.md` is its verdict. | DELETE |
| docs/design/model-wave-kickoff.md | 75 | The MBSE model wave's kickoff brief. | The wave closed; the model is suite-gated and `docs/model/` is its product. | DELETE |
| docs/design/offline-architecture.md | 123 | The offline build's architecture: local sims, the pluggable LLM, the store. | Its migration table still names Bedrock/AWS as the from-side; flagged in the lean sweep Q1 row as prose residue. | REWRITE |
| docs/design/outputs-manifest-schema.md | 480 | The `outputs.json` emit-on-solve contract: entry kinds, `t`, `crs_authid`, the frames rule. | The SCHEMA is live (the seam and the results-mesh publisher both read it); its worked producers are the purged raster workers. | KEEP |
| docs/design/persistence.md | 32 | A stub map of the persistence layer. | Describes the package that flattened to `persistence.py` 2026-09-03; names Mongo/MCP framing the flatten scrubbed. | DELETE |
| docs/design/pipeline-library-assessment-2026-08-12.md | 89 | A dated assessment of pipeline libraries. | Decision-support for a decision taken; nothing in the tree reads it. | DELETE |
| docs/design/qgis-plugin-product-analysis-2026-07.md | 386 | The July analysis that produced the QGIS-only product ruling. | Historical; its conclusion is now the standing identity in AGENTS.md and PROJECT memory. | DELETE |
| docs/design/server.md | 58 | A stub map of the server layer. | Superseded by `server-package.md` below and by the package's own structure. | DELETE |
| docs/design/server-package.md | 111 | Map of `trid3nt_server/server/`: the protocol loop, dispatch, turn, session, persistence ref. | Its tree map still carried `credentials/secrets_handler.py` rows until the lean sweep D12 scrubbed them; re-verified clean at HEAD. | KEEP |
| docs/design/server-refactor-recon-2026-08-14.md | 112 | Recon for the server refactor waves (ADR 0261-0262). | The refactor landed; its cloud-seam findings are all ledgered as deleted. | DELETE |
| docs/design/temporal-endpoint-inventory.md | 320 | Inventory of every temporal endpoint and animation-capable source. | Its consumer was the frame/temporal machinery the emission fold deleted (`temporal.py`, `TemporalConfig`, the frame-token parsers). | DELETE |
| docs/design/worker-unification-port.md | 442 | The worker-unification wave's port design, stage by stage, including the open DESIGN-STOP on the two reach templates naming `corridor_tin`. | The `corridor_tin` DESIGN-STOP it records was closed by AUTO EDGE DIES (both templates declare `om2d`), so its one open question is answered. | DELETE |
| docs/design/workflows.md | 37 | A stub map of the workflows layer. | Named in DOCS CENSUS RULED §3.1 as a delete: superseded by `workflows/README.md`. | DELETE (census 3.1) |

## E. docs/model (the MBSE model and its generated views)

Every file in this section is SUITE-GATED: `scripts/model_check.py` plus
`tests/test_model_conformance.py` run in the offline suite, the views are
regenerated from the `.sysml` sources in the same commit, and a stale row fails
the suite rather than sitting unnoticed. Verified at HEAD: model_check reports
zero findings. There is therefore no stale-claim finding to report for any of
them, and the fate is KEEP for all thirteen.

| path | lines | what it is | stale claims | fate |
|---|---|---|---|---|
| docs/model/README.md | 93 | Map of the model: the six seams, the checker, the per-usage evidence rule, the unmodeled-author rule, the regeneration command. | None (suite-gated). | KEEP |
| docs/model/data-seam.sysml | 777 | The data seam: source rows, the one-source-one-datum rule, the ladder, provenance. | None (suite-gated). | KEEP |
| docs/model/data-seam-view.md | 169 | Generated view of `data-seam.sysml`. | None (regenerated in-commit). | KEEP |
| docs/model/emission-seam.sysml | 739 | The emission seam: the one publish chokepoint, presets, legend, store. | None (suite-gated). | KEEP |
| docs/model/emission-seam-view.md | 171 | Generated view of `emission-seam.sysml`. | None (regenerated in-commit). | KEEP |
| docs/model/mesh-seam.sysml | 863 | The mesh seam: recipe, ops, meshers, the accepted artifact, boundary roles. | None (suite-gated). | KEEP |
| docs/model/mesh-seam-view.md | 242 | Generated view of `mesh-seam.sysml`. | None (regenerated in-commit). | KEEP |
| docs/model/solve-seam.sysml | 1005 | The solve seam: dispatch, the engine room, the success convention, diagnostics. | None (suite-gated). | KEEP |
| docs/model/solve-seam-view.md | 202 | Generated view of `solve-seam.sysml`. | None (regenerated in-commit). | KEEP |
| docs/model/steering-surface.sysml | 899 | The steering surface: the catalog, the wrappers, the one serializer, `TheSteeringFormatHasOneWriter`. | None (suite-gated). | KEEP |
| docs/model/steering-surface-view.md | 171 | Generated view of `steering-surface.sysml`. | None (regenerated in-commit). | KEEP |
| docs/model/tool-plane.sysml | 344 | The tool plane: registration, retrieval, the atomic-tool rule. | None (suite-gated). | KEEP |
| docs/model/tool-plane-view.md | 83 | Generated view of `tool-plane.sysml`. | None (regenerated in-commit). | KEEP |

## F. docs/playbooks

| path | lines | what it is | stale claims | fate |
|---|---|---|---|---|
| docs/playbooks/frame-animation-recipe.md | 207 | The recipe for a frame animation through the fetchers' `animation_frames` shape (GOES/VIIRS), with the scrubber grouping. | Names `fetch_wfigs_incident` by name for the fire framing (registered and live) and the GOES family (live). Its emission half predates the declared `valid_from`/`valid_to` window. | REWRITE |
| docs/playbooks/modflow-affected-fields-recipe.md | 76 | The MODFLOW-GWT "which ag field and how much" contamination recipe. | Its whole subject is a purged engine: no `workflows/modflow/`, no `modflow_*` registered tool, no worker. Every step is unexecutable. | DELETE |
| docs/playbooks/urban-heat-island-recipe.md | 100 | The zonal recipe that replaced `compute_urban_heat_island` when it was demoted to the playground, with its three stated honesty losses. | Its inputs (`fetch_modis_lst`, `fetch_landcover`, `code_exec_request`) are all registered and live. | KEEP |
| docs/playbooks/zonal-statistics-recipe.md | 108 | The zonal-statistics recipe that replaced `compute_zonal_statistics` (ADR 0043 precedent). | Live; the playground and its layer handles are the current surface. | KEEP |

## G. docs/reports (the routing and usability benches)

Cross-file finding recorded once here rather than repeated per row: FIVE pairs
of these files are byte-identical twins (md5 verified at HEAD) -
`ab-2026-07-07/tool-usability-report.md` = `tool-usability-report.md`;
`ab-2026-07-07/usability-8b-final.jsonl` = `tool-usability-results.jsonl`;
`ab-2026-07-07/8b-lessons-gated.jsonl` = `tool-routing-results.jsonl`;
`ab-2026-07-07/8b-lessons-gated-split.md` = `tool-routing-failure-split.md`;
`ab-2026-07-07/baseline-dark-qwen3-8b.jsonl` = `consumed-baseline.jsonl`. A
second cross-file finding: `tool-routing-failure-split.md` reports 183 total /
44 / 138 where `docs/site/models.md` reports 174 / 45 / 127 for the same bench -
two numbers for one measurement, which is exactly the shape one of them goes
unread.

| path | lines | what it is | stale claims | fate |
|---|---|---|---|---|
| docs/reports/ab-2026-07-07/STATE | 1 | A one-line state marker for the July A/B run. | Marks a run that closed. | DELETE |
| docs/reports/ab-2026-07-07/FINAL-REPORT.md | 86 | The July A/B's final report: lessons-gated vs baseline on qwen3-8b/9b. | Dated measurement of models and a registry that no longer exists (256-era tool counts against 162 today). | KEEP (dated record; it is the only narrative of that experiment) |
| docs/reports/ab-2026-07-07/8b-lessons-gated.jsonl | 183 | Raw per-prompt records, lessons-gated arm. | Byte-identical twin of `tool-routing-results.jsonl`. | DELETE (duplicate) |
| docs/reports/ab-2026-07-07/8b-lessons-gated-split.md | 147 | The failure split for that arm. | Byte-identical twin of `tool-routing-failure-split.md`. | DELETE (duplicate) |
| docs/reports/ab-2026-07-07/baseline-dark-qwen3-8b.jsonl | 178 | Raw per-prompt records, dark baseline arm. | Byte-identical twin of `consumed-baseline.jsonl`. | DELETE (duplicate) |
| docs/reports/ab-2026-07-07/baseline-split.md | 136 | The failure split for the baseline arm. | Names tools absent from the registry. | KEEP (dated arm evidence for FINAL-REPORT) |
| docs/reports/ab-2026-07-07/consumed-baseline.jsonl | 178 | The consumed copy of the baseline records. | Byte-identical twin of `baseline-dark-qwen3-8b.jsonl`; one of the two is the copy. | KEEP (retain exactly one of the pair) |
| docs/reports/ab-2026-07-07/lessons-on-qwen3-8b.jsonl | 176 | Raw records, lessons-on arm. | Dated. | KEEP |
| docs/reports/ab-2026-07-07/lessons-on-split.md | 132 | The failure split for the lessons-on arm. | Dated. | KEEP |
| docs/reports/ab-2026-07-07/lessons-store-after-9b.jsonl | 17 | The lessons store as it stood after the 9b run. | Dated. | KEEP |
| docs/reports/ab-2026-07-07/model-swap-9b-lessons.jsonl | 183 | Raw records, 9b model-swap arm. | Dated. | KEEP |
| docs/reports/ab-2026-07-07/model-swap-9b-split.md | 150 | The failure split for the 9b arm. | Dated. | KEEP |
| docs/reports/ab-2026-07-07/tool-usability-report.md | 189 | The usability sweep's report inside the A/B folder. | Byte-identical twin of `docs/reports/tool-usability-report.md`. | DELETE (duplicate) |
| docs/reports/ab-2026-07-07/usability-8b-final.jsonl | 180 | Raw usability records. | Byte-identical twin of `tool-usability-results.jsonl`. | DELETE (duplicate) |
| docs/reports/tool-routing-bench-qwen3-8b.md | 155 | The routing bench's headline report for qwen3-8b. | Names purged tools in its per-prompt targets; the HARNESS (`scripts/tool_routing_bench.py`) is live and `docs/site/models.md` cites this as the rerunnable procedure's output. | KEEP |
| docs/reports/tool-routing-bench-qwen3-8b-BEFORE-coldindex.md | 140 | The same bench with a cold retrieval index. | Dated pair member. | KEEP |
| docs/reports/tool-routing-bench-qwen3-8b-AFTER-warmindex.md | 152 | The same bench with a warm index - the pair that measured index warm-up. | Dated pair member. | KEEP |
| docs/reports/tool-routing-failure-split.md | 147 | The routing bench's failure taxonomy. | Duplicate of the A/B copy; ALSO contradicts `docs/site/models.md` on the totals (183/44/138 vs 174/45/127). | REWRITE (reconcile the totals, then it is the one copy) |
| docs/reports/tool-routing-report.md | 190 | The routing sweep's narrative report. | Registry counts and tool names predate the purge. | KEEP (dated) |
| docs/reports/tool-routing-results.jsonl | 183 | Raw routing records. | Duplicate of the A/B copy. | KEEP (retain exactly one of the pair) |
| docs/reports/tool-sweep-checklist.md | 185 | The per-tool sweep checklist `scripts/tool_sweep.py` drives. | Rows for tools that left the registry (the engine doors, the purged composers, `publish_layer`, `compute_urban_heat_island`). | REWRITE (regenerate from the live registry) |
| docs/reports/tool-sweep-results.jsonl | 367 | Raw sweep results. | Same roster drift. | KEEP (dated; regenerate rather than edit) |
| docs/reports/tool-usability-report.md | 189 | The usability sweep's report. | Duplicate of the A/B copy. | KEEP (retain exactly one of the pair) |
| docs/reports/tool-usability-results.jsonl | 180 | Raw usability records. | Duplicate of the A/B copy. | KEEP (retain exactly one of the pair) |

## H. docs/research

| path | lines | what it is | stale claims | fate |
|---|---|---|---|---|
| docs/research/bathymetry-sources.md | 963 | The bathymetry survey: topobathy coverage by water-body class, vertical datums, CUDEM/BlueTopo/NCEI/ETOPO, the surveyed-cross-section supply path. | Live and load-bearing: the bathymetry charter and the one-source-one-datum ruling both read from it, and the NCEI-mosaic finding it carries is the reason `fetch_greatlakes_bathymetry` replaced `fetch_ncei_dem_mosaic`. | KEEP |
| docs/research/coastal-mesh-edit-landscape.md | 383 | Survey of coastal mesh-editing tools and what an editable mesh layer must offer. | Its recommendations landed as the mesh session's edit surface (`mesh_op`, `mesh_reset`); a survey whose conclusion is implemented. | DELETE |
| docs/research/oceanmesh-front-proposal.md | 335 | The proposal that became the om2d front. | Superseded by `docs/specs/mesh-recipe.html` and the shipped mesher. | DELETE |
| docs/research/om2d-telapy-mesh-recon.md | 459 | Recon of OceanMesh2D against telapy/pretel for the geometry-writing seam. | Its verdict is the shipped `mesh/shared/selafin_cli.py` plus the GPL-isolated image; the recon's alternatives are closed. | DELETE |
| docs/research/rog-replication-methodology.md | 166 | The Ball Creek / Coweeta rain-on-grid replication methodology, graded against EDI weir observations. | The DRIVERS it names were deleted in the stale-scripts sweep; the METHODOLOGY is the live commitment for the rain-on-grid template's V&V. | KEEP |

## I. docs/site (the user-facing pages)

| path | lines | what it is | stale claims | fate |
|---|---|---|---|---|
| docs/site/configuration.md | 169 | Configuration reference: the env block, the store, the provider selection, the tailnet token. | Documents `TRID3NT_TOOL_RETRIEVAL` (deleted, ADR 0276), `MODEL_PROVIDER=bedrock` (adapter deleted), and store vars that moved to `/vsis3`. | REWRITE |
| docs/site/engines.md | 93 | The engines page: what TRID3NT can simulate. | Lists eleven engines; ONE is in the tree. This is the single most misleading page in the docs set - it advertises capability that does not exist. | REWRITE |
| docs/site/install.md | 243 | Install: the plugin, the daemon, the images, MinIO, the make targets. | Names images for purged engines and the AWS-era prerequisites. | REWRITE |
| docs/site/models.md | 123 | The models page: which LLMs are supported, the routing-bench numbers, the harness procedure. | Bench totals contradict `docs/reports/tool-routing-failure-split.md` (174/45/127 vs 183/44/138); names Bedrock as a selectable provider. | REWRITE |
| docs/site/overview.md | 111 | The overview page: what the product is and the shape of a session. | Engine roster and web-client framing predate the purge and the QGIS-only ruling. | REWRITE |
| docs/site/tool-support.md | 228 | The generated tool-support table (credentials, coverage, caveats), written by `scripts/gen_tool_support_page.py`. | A generated snapshot taken before the purge: rows for tools that no longer register, and a credential map naming providers that left. | RE-CUT (regenerate from the live registry, do not hand-edit) |
| docs/site/troubleshooting.md | 131 | Troubleshooting: the common failures and what they mean. | Several entries diagnose AWS Batch, TiTiler and the web client. | REWRITE |

## J. docs/specs

| path | lines | what it is | stale claims | fate |
|---|---|---|---|---|
| docs/specs/calibration-seam.html | 163 | Spec for the calibration seam. | The seam is unbuilt and the calibration track is chartered fresh (paper-first, no code inheritance). | KEEP (unexecuted charter, not a claim about the tree) |
| docs/specs/credentials-chop-plan.md | 160 | The credentials vault chop plan (ADR 0062). | Executed: the vault, the legacy schemes and the file-vault CRUD are all ledgered DELETED. | DELETE |
| docs/specs/cull-proposal.md | 283 | An early cull proposal across the tool surface. | Superseded by the lean-sweep inventory, which measured rather than proposed. | DELETE |
| docs/specs/data-router-fold.md | 155 | The spec for folding coded fetchers onto the declarative router. | Executed to completion - zero coded fetchers remain; the census is `fetcher-fold-census.md`. | DELETE |
| docs/specs/emission-fold.html | 95 | The emission fold's spec (sections 5-6: the frame collapse, the .qml as the style document). | Executed; its conformance walk is `emission-fold-*-conformance.md` and its honest net is in the ledger. | KEEP (the spec a conformance walk is read against) |
| docs/specs/engine-door-refactor.md | 134 | The spec for the engine-door concierge tools. | The doors were DISSOLVED (ADR 0094); templates rejoined the pool. | DELETE |
| docs/specs/engine-rollout-contract.md | 425 | The contract a new engine must satisfy to roll out. | Written against the eleven-engine era; its per-engine rows are gone, though the CONTRACT shape is what `adding-an-engine.md` now carries. | DELETE |
| docs/specs/fetcher-fold-audit.md | 487 | The audit that scoped the fetcher fold. | Superseded by the executed census. | DELETE |
| docs/specs/gdal-leverage-audit.md | 173 | Audit of where GDAL could replace hand-rolled IO. | Its findings landed (the `/vsizip//vsicurl/` read, the OGR driver rows, `/vsis3`). | DELETE |
| docs/specs/gdal-native-collapse-verdict.md | 194 | The verdict refuting the gzip/vsizip native collapse (36MB for an 8x5px window). | A REFUTATION with measurement - the ledger cites it as the reason a candidate was rejected. | KEEP |
| docs/specs/gmsh-mesher.html | 140 | Spec for a gmsh-based mesher. | gmsh left the worker image entirely (worker-unification stage 4); no gmsh mesher exists or is planned. | DELETE |
| docs/specs/hydrology-tools-analysis.md | 391 | Analysis of the Python hydrology library landscape (dataretrieval, pywatershed, pysheds, the dep-trio). | Live feedstock: the HyRiver fold stage reads it, and `section-vs-hyriver.md` is its continuation. | KEEP |
| docs/specs/hygiene-sweep-plan.md | 409 | The plan for a hygiene sweep across the tree. | Superseded by THIS wave's charter and its manifest lenses. | DELETE |
| docs/specs/ingest-framework-adoption.md | 152 | Spec for adopting an ingest framework in the router. | The `ingest:` block shipped and is the router's live vocabulary. | DELETE |
| docs/specs/ingest-transport-decision.md | 196 | The transport decision behind the ingest block (whole-object vs ranged). | Its ZIP verdict was later REVERSED by measurement (the `/vsizip//vsicurl/` read at 61s vs 138s), so it records a decision the tree no longer follows. | DELETE |
| docs/specs/mesh-layer-extraction.md | 189 | Spec for extracting the mesh as a first-class layer (the big-3 extraction). | Landed via the mesh wave: `emission/mesh_display.py` and the MDAL binding. | DELETE |
| docs/specs/mesh-recipe.html | 131 | The mesh-recipe spec rev 2 - the three agnostic params plus ordered ops. | LIVE: it is the spec `mesh-recipe-conformance.md` walks clause by clause, and the ledger cites its sections by number. | KEEP |
| docs/specs/modflow-pilot-contract.md | 645 | The MODFLOW pilot's contract. | Whole subject purged. | DELETE |
| docs/specs/module-surface.html | 236 | The module-surface spec (the dico catalogs, the wrappers, the sheet, the one serializer, the LLM surface, section 12's dissolution gate). | LIVE: `module-surface-conformance.md` walks it and `module-surface-loc.md` measures against its section 7; its LOC promise is honestly recorded as unmet in both. | KEEP |
| docs/specs/processing-decloud-refactor.md | 85 | Spec for de-clouding the processing tools. | Executed; the cloud seams are ledgered deleted. | DELETE |
| docs/specs/processing-redundancy-candidates.json | 66 | Machine-readable candidate list for the processing-redundancy cull. | Its candidates were decided (one demoted, five kept with reasons). | DELETE |
| docs/specs/processing-redundancy-cull-proposal.md | 209 | The cull proposal with the per-module reasoning the ledger cites for the five KEPT verbs. | The ledger's cleanup-wave-phase-2 row names this file as where the per-module reasoning lives, so deleting it orphans a live citation. | KEEP |
| docs/specs/processing-redundancy-report.md | 141 | The report behind that proposal. | Superseded by the proposal it fed. | DELETE |
| docs/specs/review-findings.md | 26 | A short findings list from a review pass. | All items closed. | DELETE |
| docs/specs/router-pilot-contract.md | 468 | The router pilot's contract - what a declarative source must satisfy. | The pilot is the whole product now; the contract shape is `source_spec.py` and the executors. | DELETE |
| docs/specs/server-modularization-plan.md | 104 | The plan for breaking up the server monolith. | Executed across ADR 0261-0262 and the package skeleton. | DELETE |
| docs/specs/sfincs-workflow-audit.md | 289 | Audit of the SFINCS workflow. | Whole subject purged. | DELETE |
| docs/specs/shared-workflows-cull-proposal.md | 217 | Proposal to cull `workflows/shared/`. | Executed - the nine orphans went to the attic 2026-08-31. | DELETE |
| docs/specs/square-two.html | 95 | The Square Two standing gate: no reimplemented library IO, wrap the library where it lives. | LIVE standing gate - the ledger cites it as the reason the hand-packed SELAFIN writer and the struct parser died. | KEEP |
| docs/specs/system-uml.html | 167 | A hand-drawn system UML. | Superseded by `docs/model/` (suite-gated, so it cannot be stale); a second, ungated drawing of the same system is the drift hazard the MBSE model exists to close. | DELETE |
| docs/specs/workflow-blueprint.html | 293 | The workflow blueprint: the skeleton, the declaration, the plan. | Its plan-language half died at module-surface stage 3; the skeleton half is the live `Workflow` base and door. | REWRITE |

## K. docs/validation

| path | lines | what it is | stale claims | fate |
|---|---|---|---|---|
| docs/validation/README.md | 33 | Map of `docs/validation/`: what a conformance walk is, what a census is, what a ledger is. | Its roster of files predates this wave; the census rules sixteen of them deleted. | REWRITE |
| docs/validation/activation-boundary.md | 31 | A "STATUS: THINKING" note on where activation rows begin and end. | An unfinished thinking note; DOCS CENSUS RULED §3.5 names the THINKING notes as deletes. | DELETE (census 3.5) |
| docs/validation/agentic-loop.md | 40 | A "STATUS: THINKING" note on the agent loop. | Same class. | DELETE (census 3.5) |
| docs/validation/build-contract.md | 562 | The build contract for the worker images (the eleven-engine era). | Ten of its eleven image contracts describe Dockerfiles in the attic. | DELETE (census 3.5) |
| docs/validation/build-report.md | 370 | The report of a build run against that contract. | Same. | DELETE (census 3.5) |
| docs/validation/code-graph/SUMMARY.md | 134 | The code-graph instrument's summary: node and edge counts, the roots, the orphan and dead-symbol counts. | Verified current at HEAD (1042 nodes / 3471 edges / 0 missing paths). | KEEP |
| docs/validation/code-graph/graph.json | 37856 | The code atlas itself - the committed import graph `scripts/code_graph.py` writes. | Verified current: 0 missing paths, and `model_check.py` no longer reads it (it parses imports itself), so it is an instrument product with its own readers rather than a live input. | KEEP |
| docs/validation/code-graph/orphans.md | 197 | The orphan list the atlas derives. | Verified: `secrets_handler.py`, the one true product orphan it named, is deleted (lean sweep D12), so the list is one row ahead of its own regeneration. | KEEP (regenerate rather than edit) |
| docs/validation/code-graph/dead_symbols.md | 73 | The dead-symbol list the atlas derives. | Still names the flood run-settings provider trio, which the ledger records as HELD pending NATE's read rather than dead-by-mistake. | KEEP |
| docs/validation/composer-cull-characterization.md | 84 | The characterization behind the ADR 0095/0105 composer cull. | Every composer it characterizes is deleted. | DELETE |
| docs/validation/corpus-additions.yaml | 118 | Corpus phrasings staged for addition to registered tools' `corpus.yaml`. | Live feedstock: the new-tool-needs-corpus-first norm reads it. | KEEP |
| docs/validation/docs-census.md | 952 | THE CENSUS this wave's DOCS CENSUS RULED entry rules on: the delete list (273 md + 1 json, 45,151 md lines), the target layout, the root-README and map-README skeletons, the nine wave stages, the eleven design questions. | Its §6 Q8 (a `docs/method/` directory) is OVERRULED by the ruling itself ("NO docs/method/ directory"); every other row was spot-verified against the tree during this lens and held. | KEEP |
| docs/validation/docstring-exemptions.md | 18 | The regenerated `# docstring-exempt:` ledger THE DOCSTRING LIMIT RULED requires: one row per marker in the tree (symbol, `file:line`, reason) under a preamble stating the 3/5 content-line limits and the ~10-entry ceiling past which the limit is re-argued rather than routed around. | Eight rows against a ceiling of about ten, so the ledger already sits at the pressure point the ruling names - a standing condition, not a false claim. Every `file:line` resolves and all eight symbols exist. One reason field reads as truncated at its leading `**roles` (`shared/primitives.py:74`); that is the marker's own text carried verbatim, which is what "regenerated from the markers themselves" means. | KEEP (regenerated by the guard test, never hand-edited) |
| docs/validation/e2e-harness.md | 66 | Notes on an end-to-end harness. | Superseded by `trid3nt_server/testing/{ws_client,live_run,canaries}.py`, which IS the harness. | DELETE (census 3.5) |
| docs/validation/emission-fold-presets-conformance.md | 115 | Clause-by-clause conformance of the emission fold's preset half. | Verified against the tree: `emission/presets.py` holds the four kinds and the .qml writer as claimed. | KEEP |
| docs/validation/emission-fold-store-conformance.md | 174 | Conformance of the emission fold's store half (one store, one scheme, `/vsis3`). | Verified: `s3_to_vsis3` is the whole translation, `configure_store_access` sets the endpoint once. | KEEP |
| docs/validation/engine-coverage-inventory.md | 732 | Per-engine coverage inventory across the eleven-engine era. | Ten of eleven sections describe absent engines; the ledger already cites it as "the dated engine-coverage-inventory" when scrubbing references. | DELETE (census 3.5) |
| docs/validation/fetcher-fold-census.md | 772 | The fetcher fold's census: every source row, its executor, its fate, the G-numbered groups the fold stages execute against. | ONE defect found: §6 verification item 5 still names `scripts/run_sfincs_direct.py`, which is not in the tree (it went with the SFINCS purge). Everything else re-verified. | REWRITE (one item) |
| docs/validation/fetcher-fold-conformance.md | 249 | Conformance walk of the fetcher fold. | Verified against the 99 specs and the executor roster. | KEEP |
| docs/validation/fetcher-fold-hydro-stage.md | 172 | The HyRiver/hydro stage's record: what folded, what was refuted, what is queued on the `pynhd` cap. | Current (2026-09-09); its refutations are measured and its queued row states the re-measure trigger. | KEEP |
| docs/validation/fetcher-fold-raster-half.md | 262 | The raster half of the fold: the access modes, the delegates, the provenance channel. | Verified. | KEEP |
| docs/validation/fetcher-fold-stage0.md | 476 | Stage 0's probe: which libraries are constructible at which versions, and the rulings that came out of it. | Current; it is why `pynhd` is not a dependency. | KEEP |
| docs/validation/hygiene-manifest/COVERAGE.md | 129 | The FULL COVERAGE LAW audit over the sibling lenses: the reproducible scope enumeration (1,907 files), the rows-per-lens table, the three files that had no row and the lens that missed each, the 49 files carrying two rows, and the guards-leg delta. | Its own OPEN GAP paragraph is the live finding and this pass closes it: `git ls-files docs | grep -v '^docs/proof/'` now counts 516, EIGHTEEN above the 498 the census walked - the sixteen the gap names plus `docs/READABILITY_LEDGER.md` and `docs/validation/docstring-exemptions.md`, which landed after it was written. Second finding: it records two lens headers being corrected (to 35 and to 41) but neither lens's closing `files listed / read / rows written` line moved with its header. | REWRITE (the gap paragraph) |
| docs/validation/hygiene-manifest/contracts.md | 180 | The lens over `contracts/` minus its tests: 72 rows (24 Python modules, `py.typed`, the package README, `pyproject.toml`, 45 generated JSON Schemas), over a preamble that includes a fifteen-row table of sentences a previous mechanical spec-notation strip left broken mid-clause. | None found against the tree. Its load-bearing cross-cutting claim - every `schemas/*.json` `description` is a verbatim mirror of the docstring above it, so any docstring trim in that scope REQUIRES regenerating the directory in the same commit - was re-checked and holds (`contracts/tests/test_schema_drift.py` walks the directory rather than a hand-listed set). Its `tool_metadata.py` DELETE candidate is correctly recorded as outside a documentation wave's authority. | KEEP |
| docs/validation/hygiene-manifest/docs-and-readmes.md | 399 | THIS FILE: the lens over `docs/` except `decisions/` and `proof/`, plus every directory README, `AGENTS.md`, `docs/CONVENTIONS.md` and the repo README - 191 rows in thirteen sections, twelve scope-wide findings and a fate tally. | Its EXCLUDED paragraph is stale in exactly the direction this pass corrects: the thirteen sibling lenses it declined to row because `git ls-files` did not list them ARE tracked at HEAD, and they are rowed here. The fate tally in finding 12 counted 191 rows and is restated below. | REWRITE (this pass) |
| docs/validation/hygiene-manifest/docs-decisions.md | 380 | The lens over `docs/decisions/`: 328 rows (326 numbered records with the gaps at 0221/0234 and the 0307 collision, the folder README, the AFK ledger), each classified BINDING / SUPERSEDED-by / DEAD-because with a fate, over a preamble of the tree evidence the verdicts rest on, closing on the verdict rollup (BINDING 106, SUPERSEDED 66, DEAD 154, chop set 220). | One divergence from the ruling it executes, and it is an ADDITION rather than a contradiction: DOCS CENSUS RULED names seven binding records to amend, this lens names five by number (0004, 0015, 0023, 0315, 0320) because two of the census's seven are `AGENTS.md`'s laws and `docs/CONVENTIONS.md` - the sibling lens's rows - and it adds 0315, whose coastal-split half died with the coastal template and which the census did not carry. Its preamble evidence was re-checked and holds. | KEEP |
| docs/validation/hygiene-manifest/emission.md | 48 | The lens over `trid3nt_server/emission`: 11 code rows plus six sweep guards, the corrected pure-LOC measure (244 lines understated over the scope), and two documented mechanisms proven absent from the code that documents them. | None found. Its `presets.py` row states its own condition rather than a wrong claim (`scripts/qml_preset_smoke.py` "goes stale the moment SCRIPTS RULED lands the move"). | KEEP |
| docs/validation/hygiene-manifest/plugin.md | 88 | The lens over `plugin/` minus `plugin/tests/`: 34 rows (24 code files, the README, the Makefile, `metadata.txt`, `.gitignore`, LICENSE, `icon.svg`, two binary screenshots read for identity not pixels), the sweep guards, and one DESIGN question raised without action. | None found against the tree. The DESIGN question is still open and correctly unacted: `metadata.txt`'s `changelog` field carries `ADR 0105`, `F9`, `BUG 3b` and repeated NATE attributions in a QGIS distribution manifest field the Plugin Manager renders to the user, which no ruling covers. The two screenshots are named as DOC IMAGES freshness candidates rather than as stale-claim findings. | KEEP |
| docs/validation/hygiene-manifest/scripts-workers.md | 194 | The lens over `scripts/` and `workers/`: 69 rows arranged by the five-directory structure SCRIPTS RULED adopts (entry points, instruments, packet, drivers, staging, local, attic, delete) plus `workers/` and the data/README table, with six sweep guards. | Two rows carry findings the scripts evaluation did not, both reported without relitigating the ruled fate: `seed_showcase_cases.py` seeds 48 distinct tool names of which only SEVEN resolve anywhere in the tree, and `use_openrouter.sh`'s header documents a default model its own body contradicts. Its section 6 correctly records the two mesh-format modules as already moved out of this scope. | KEEP |
| docs/validation/hygiene-manifest/tests-plugin-contracts.md | 514 | The lens over `tests/`, `plugin/tests/` and `contracts/tests/`: 395 MAP rows (`file -> destination`) rather than prose rows, a totals table, a LANDED section recording what the mirror move executed and the five stated fates measurement corrected, and the guards leg's five new `tests/hygiene/` rows. | Its totals paragraph states the one divergence honestly (79,450 pure LOC here against the evaluation's 79,680, which is tree drift and not method). The struck rows for the four `__init__.py` files the import-mode checkpoint deleted or added are kept struck rather than removed, which is the dated-record convention. | KEEP |
| docs/validation/hygiene-manifest/tools-processing.md | 202 | The lens over `trid3nt_server/tools/processing`: 107 rows (73 Python files including 34 empty package markers, 34 `corpus.yaml` files), the empty-marker index, and eight sweep guards including the LLM front-budget census (33 of 34 registered tools over 1,000 chars). | None found. Its nine-name dead-reference list was spot-checked and holds; the `# tools-backlog:` TODO-class comments and the two `downtown-Tampa` strings it flags are correctly recorded as live product text outside a documentation wave's reach. | KEEP |
| docs/validation/hygiene-manifest/trid3nt_server-adapters-credentials-fallbacks.md | 72 | The lens over `trid3nt_server/{adapters,credentials,fallbacks}`: 16 rows, a seven-row sweep-guard table, a fate roll-up and three readability-ledger rows. | None found. It carries the repo's one memory-filename hit in a comment (`adapter.py:2474`) and the `SYSTEM_PROMPT` finding - 511 lines of LLM-facing product text carrying spec labels, job ids and two incident narratives on the wire to the model - correctly REPORTED rather than touched, because editing prompt text changes model behaviour. | KEEP |
| docs/validation/hygiene-manifest/trid3nt_server-gates-cases-sandbox-testing.md | 89 | The lens over `trid3nt_server/{gates,cases,sandbox,testing}`: 32 rows across four code tables plus eight sweep guards. | One finding beyond documentation, correctly labelled as such: `proof_animations.PACKET_NOTES[("telemac3d_stratified_flow","coarse")]` states 32.94 m horizontal, a five-hour window and a 6 m/s wind while the canary declares 3000 m, 1.0 h and 0.0 m/s - a stale claim in SHIPPED data that reaches a reader on the packet, not a comment. Its `canaries.py` row also names the runtime-fragile absolute-path load of `scripts/assemble_proof_packet.py` under the scripts move. | KEEP |
| docs/validation/hygiene-manifest/trid3nt_server-server-main-persistence-plugin_repo.md | 117 | The lens over `trid3nt_server/server` plus `main`, `persistence`, `plugin_repo` and the package root: one table under a nine-point summary of what the scope carries (the `_core` residue, the deleted web client as the documented renderer, six named symbols that do not exist). | ONE count defect, of the class this pass corrects: the table header reads "Python files (35)" and the table holds 35 rows after the completeness critic added `scenario_reuse.py` and `telemetry.py`, but the closing line still reads `files listed: 33 / files read: 33 / rows written: 33`. The scope claims in the summary were re-checked and hold. | REWRITE (the trailer count) |
| docs/validation/hygiene-manifest/trid3nt_server-tools-fetchers.md | 503 | The sibling manifest lens over `trid3nt_server/tools/fetchers/` - 405 rows across four sections plus twelve scope-wide findings and a ten-row docstring-exempt candidate table. | None; it is this wave's own product and its closing line (405/405/405) is internally consistent. | KEEP |
| docs/validation/hygiene-manifest/trid3nt_server-tools-root-search-meta-display.md | 160 | The lens over `trid3nt_server/tools` at top level plus `search/`, `meta/` and `display/`: 57 rows, cross-cutting findings, and - alone among the siblings - a "Prose-lens pass applied" section recording what the trim actually executed against those rows. | Its census table is a BEFORE picture: the rows record the pre-trim state and the pass section records what changed, which is the dated-record convention rather than an internal contradiction. Its registry-count correction ("the live registry is 161, verified") is the measurement three other files in that scope got wrong. | KEEP |
| docs/validation/hygiene-manifest/workflows-mesh.md | 64 | The lens over `trid3nt_server/workflows/mesh`: 31 rows (29 code files, the README, `corpus.yaml`) plus four sweep guards, and the first record of the `loc_report` double-subtraction defect every sibling lens then carries. | None found. Its `shared/nodes.py` and `shared/formats/*` rows already describe the POST-move tree (the `sys.path` hack gone, `tin_formats()` importing the package), so the mesh-format move is recorded here as landed rather than pending. | KEEP |
| docs/validation/hygiene-manifest/workflows-runtime-solver-shared.md | 98 | The lens over `trid3nt_server/workflows/{runtime,solver,shared}` and the `workflows/` package root: 41 Python rows plus 2 non-code rows, and five cross-cutting findings (the four LLM-facing docstrings all over budget, the dead-engine residue concentrated in four files, the non-ASCII survivors, the exempt shortlist). | ONE count defect of the same class as its server sibling: the header reads "Python files (41)" and the file carries 41 Python rows plus 2 non-code rows, but the closing line reads `files listed: 42 / files read: 42 / rows written: 42` - moved by one when the critic added the `workflows/__init__.py` row, and reconciling with neither 41 nor 43. | REWRITE (the trailer count) |
| docs/validation/hygiene-manifest/workflows-telemac.md | 144 | The lens over `trid3nt_server/workflows/telemac`: 98 rows (73 Python files, one vendored Fortran template, the READMEs and corpora, six machine-extracted catalog JSONs read record by record), seven sweep guards and a duplication census. | None found: the six catalog keyword counts, the twenty registry tool names and the thirteen mesh ops it names were all re-verified against the tree. Its one recommendation carrying a standing condition is the vendored `oil_flot_template.f` - the history-marker guard needs an explicit exclusion for that path or it fires on the ENGINE's own file header on every run. | KEEP |
| docs/validation/l2-harvey-findings.md | 61 | Findings from the L2 Harvey V&V runs. | Its harness (`scripts/run_l2_validation_harness.py`) went to the attic with SFINCS; the findings describe an engine that is gone. | DELETE (census 3.5) |
| docs/validation/lean-sweep-inventory.md | 427 | The lean sweep's inventory: sections 2 (D1-D12), 3 (L-rows), 5 (stale rows), 6 (the questions Q1-Q8). | LIVE and heavily cited - the ledger's newest rows cite it by section and row number, and several of its rows are still QUEUED or re-scoped rather than closed. | KEEP |
| docs/validation/mesh-recipe-conformance.md | 355 | Clause-by-clause conformance of the mesh recipe spec rev 2. | Verified against `workflows/mesh/`; its deviations are reported, not fixed, per the gate. | KEEP |
| docs/validation/mesh-wave-conformance.md | 532 | The mesh wave's conformance walk. | Dated: several of its "current" statements were superseded by the recipe wave and the elegance review (the mesher roster, `MeshPolicy`, the second mesh front). It is a record of the wave's close, not of now. | KEEP (dated conformance record) |
| docs/validation/ml-signoff-shortlist.md | 226 | A shortlist of ML sign-off candidates. | No live consumer; the ML track is not chartered. | DELETE (census 3.5) |
| docs/validation/module-coverage-board.md | 2209 | The engine module-coverage board: per-module fidelity-ladder entries and what each solve proved. | Mostly purged engines, BUT the ledger explicitly KEEPS its 2026-08-10 fidelity-ladder entry as "the measured history of a run that happened, not a map of the live tree", and cites it that way when repointing. | KEEP (dated measurement record) |
| docs/validation/module-surface-conformance.md | 408 | Clause-by-clause conformance of the module-surface spec, including the honestly-unmet LOC promise. | Verified; the unmet promise is stated in the walk itself rather than claimed met. | KEEP |
| docs/validation/module-surface-loc.md | 85 | The measured LOC table behind that walk, with both reasons the spec's number was not met. | Verified against `scripts/loc_report.py`. | KEEP |
| docs/validation/nlcd-manning-tables.md | 110 | The NLCD land-cover to Manning's n mapping tables, with their source. | The CODE that read them went to the attic with `roughness_resolve.py`, but the TABLE is data with a stated provenance and the ledger's fetcher rows still cite it. | KEEP |
| docs/validation/open-questions.md | 42 | A "STATUS: THINKING" open-questions note. | Superseded by IDEAS.md, which is the rulings record and the place a question goes. | DELETE (census 3.5) |
| docs/validation/provenance-audit-2026-08-11.md | 165 | A dated provenance audit across the fetchers. | Its findings landed as the provenance channel (ADR 0110) and the activation rows. | DELETE (census 3.5) |
| docs/validation/replication-candidates.md | 228 | Candidate published studies for replication. | Its candidate list is engine-keyed to the purged families; the paper-first NORM lives in memory and AGENTS.md. | DELETE (census 3.5) |
| docs/validation/research.md | 194 | A "STATUS: THINKING" research note. | Same class. | DELETE (census 3.5) |
| docs/validation/responsibility-cut.md | 34 | A "STATUS: THINKING" note on the server/worker responsibility cut. | The cut is made and is the worker-doctrine line in AGENTS.md and the worker README. | DELETE (census 3.5) |
| docs/validation/roadmap-proposal.md | 17 | A 17-line roadmap proposal. | Superseded by the campaign rulings in IDEAS.md. | DELETE (census 3.5) |
| docs/validation/scope-census.md | 425 | The scope census that ruled the demographic, biodiversity and movement-ecology extensions out of scope and into the attic. | Verified against the ledger's SCOPE-ATTIC rows (registry 172 -> 161, specs 108 -> 97); its section 3 rows 13-16 and section 4a L6 are cited by name in the ledger. | KEEP |
| docs/validation/scripts-eval.md | 432 | THE EVALUATION that SCRIPTS RULED rules on: every file under `scripts/`, what it drives, whether its subject exists, and its fate. | Verified in this lens's spot checks; it is a charter input and must survive its own wave. | KEEP |
| docs/validation/section-vs-hyriver.md | 84 | The measured comparison that REFUTED folding `section`'s `between` cut onto `pynhd.flowline_xsection`. | Current (2026-09-09); the ledger's hydro-stage rows cite it. | KEEP |
| docs/validation/sfincs-nws-forcing-characterization.md | 46 | Characterization of the SFINCS NWS forcing path. | Whole subject purged. | DELETE (census 3.5) |
| docs/validation/skeleton-loc-ledger.md | 955 | The skeleton wave's LOC ledger: per-file before/after across the declarative migration. | A measured record with dated refs; several of its "after" numbers were superseded by later waves, which is what a dated ledger is. | KEEP |
| docs/validation/telemac-family-deck-parity.md | 148 | Deck-parity evidence across the TELEMAC family migration (byte-identical deck asks). | Superseded twice over: the f-string author it compared against is deleted and the serializer is the only writer, pinned by a standing test. | DELETE (census 3.5) |
| docs/validation/telemac-family-migration-inventory.md | 266 | The inventory that scoped the TELEMAC family migration. | The migration landed; the inventory's rows are all executed. | DELETE (census 3.5) |
| docs/validation/template-input-provenance-audit.md | 486 | The audit of every template's input provenance rows. | Written across the eleven-engine template fleet; eight TELEMAC templates remain and their provenance is the sheet's. | DELETE (census 3.5) |
| docs/validation/template-velocity.md | 123 | A measurement of template-authoring velocity across the fleet. | Measures a fleet that no longer exists. | DELETE (census 3.5) |
| docs/validation/tests-eval.md | 911 | THE EVALUATION that TESTS RULED rules on: every test file, its subject, whether the subject exists, and the six-slice proposal. | Charter input; verified in spot checks. | KEEP |
| docs/validation/tool-list.md | 155 | A hand-maintained list of registered tools. | A second, ungated copy of `TOOL_REGISTRY` - the registry is the answer and this list drifts by construction (it names purged tools). | DELETE (census 3.5) |
| docs/validation/worker-loc-ledger.md | 199 | The worker LOC ledger: row 0's baseline at a named git ref and the dissolution across the fetch-migration and mesh waves. | Its row-0 table measures a git ref, which the ledger explicitly protects as "a record of what was true when it was taken". | KEEP |
| docs/validation/worker-unification-conformance.md | 376 | Clause-by-clause conformance of the worker-unification port. | Verified: one gate, one dispatch, one metrics envelope, and the WAQTEL/GAIA launcher deviation stated as a scoped ledgered deviation rather than hidden. | KEEP |
| docs/validation/worker-unification-proof-interrogation.md | 301 | The adversarial pre-delivery interrogation of that wave's proofs. | Current; it is the record the MECHANICAL-packets norm asks for. | KEEP |

## L. Ledgers and metrics

| path | lines | what it is | stale claims | fate |
|---|---|---|---|---|
| docs/IDEAS.md | 4518 | THE RULINGS RECORD - the standing charter source. Every wave's rulings in date order, ending at the 2026-09-09 FOLD RESIDUE HYGIENE CLOSED entry with its two live reds (`TRID3NT_GSHHG_SHP` unset; `telemac_do_sag_refined`'s `TELEMAC_RELEASE_POINT_OUTSIDE_DOMAIN`). | Append-only by design; superseded rulings are corrected in place with the correction stated (e.g. the malpasset fence that "does not exist"). No stale CLAIM about the tree survives uncorrected. | KEEP |
| docs/DELETION_LEDGER.md | 3405 | THE DELETION LEDGER - every candidate at decision time with its CONDITION, through QUEUED -> CONDITION-MET -> DELETED(commit) or SCOPE-ATTIC(commit), plus the rejected candidates with their reasons. Read end to end for this row. | Two structural defects, both reportable rather than fixable by a docs lens: (1) MASSIVE DUPLICATION - the fourteen wave-A rows (`.byo()`, the p-view read-recording, `_check_revisable_branches`, `Plan.flat()`, `DataRefs`, the eager Data batch, `.render`, `quantity_styles.py`, `_QGIS_STYLE_REGISTRY`, `OutputQuantitySpec.style_preset`, `case_lifecycle.py`, `translate_to_cog`, `SEDIMENT_YIELD_LOG_CLASSES`) are repeated VERBATIM in four separate tables (the main table and the "Cleanup wave phase 1", "Wave 2c" and "Cleanup wave phase 2" headings), so the same deletion is recorded up to four times; (2) several QUEUED rows are marked by the lean sweep as "PLAUSIBLY MET, needs a live run" (the gridgen binary, the HEC-RAS 2025 Linux payload which CONTRADICTS the standing project memory note, the in-worker bed input). | KEEP (it is a standing norm's instrument; the duplication is a defect to collapse, not a reason to delete) |
| docs/READABILITY_LEDGER.md | 253 | THE READABILITY LEDGER: every comment or docstring a better NAME or a small EXTRACTION would make unnecessary, as file / line / the comment as it stands / the change that would remove it / risk class (rename, extract, restructure). Written by this wave, applied by NOTHING. | None - it records proposals, so it carries no claim about the tree beyond its line references, which were spot-checked. Its standing condition is the ruling's own: after the docs wave NATE reads it and either takes a batch as its own reviewed change per module, where behaviour is verified as code, or moves on. | KEEP |
| docs/REANALYZE_LEDGER.md | 248 | The re-analysis ledger: guards and heuristics deleted by ruling, each with the measured evidence and the trigger that would bring a real check back. | Current; the ledger's newest rows cite it by date (the two-populations bed guard). | KEEP |
| docs/metrics.md | 105 | A metrics page: 105 lines of very wide rows (95 KB) of per-wave counts. | Named in DOCS CENSUS RULED §3.1 as a delete. Its counts are registry- and engine-keyed to the pre-purge tree and no instrument regenerates it, so it cannot be brought current - it can only be re-derived, which `loc_report.py` and `code_graph.py` already do. | DELETE (census 3.1) |

## M. The README clause inside docs/decisions and docs/proof

Rowed here because the scope's "READMEs of every directory" clause reaches them.
The decision RECORDS and the proof ARTIFACTS themselves are out of scope; the
proof tree is additionally FROZEN except for what DOCS CENSUS RULED names.

| path | lines | what it is | stale claims | fate |
|---|---|---|---|---|
| docs/decisions/README.md | 51 | Map of the ADR-lite decision records: the numbering, the one-decision-per-file rule, how a record is superseded. | Its stated range and its "what a live ADR looks like" example predate the purge; the sibling `docs-decisions.md` lens owns the per-record BINDING/SUPERSEDED/DEAD verdicts. | REWRITE (map skeleton only) |
| docs/proof/templates/README.md | 254 | The proof-packet contract: what a template's pinned evidence must contain, the coarse-vs-refined variants, the assembler's rules, the re-pin rule. | Verified current: the ledger's 2026-09-03 row records this file being corrected so that a coarse pin owes its packet, and the assembler now enforces it. | KEEP |
| docs/proof/templates/oceanmesh_meshes/README.txt | 87 | The persisted oceanmesh proof meshes: what each of the five is, its AOI and how it was built. | Names the three sandbox builders (`build_coastal_mesh.py`, `build_coastal_water_edge_mesh.py`, `build_watershed_mesh.py`) as the regeneration path; all three were DELETED in the stale-scripts sweep, so the meshes are now unregenerable by the route this file names. | REWRITE (state the recipe path, or state honestly that these are frozen artifacts with no live regeneration route) |
| docs/proof/templates/rog_run_products/README.txt | 10 | A ten-line note on the pinned rain-on-grid run products. | None found. | KEEP |

## Scope-wide findings

1. **The engine roster is the dominant staleness axis.** The guard grep after
   the read (a case-insensitive search for the eleven purged family names)
   matches 114 of the 191 files. That number is an UPPER BOUND, not the census:
   the ledgers, the ADR-citing rows and the dated conformance walks name those
   engines as HISTORY, which is what a dated record is for, and the per-file
   rows above are where each mention was actually judged. What the rows find is
   that the mentions divide cleanly - a doc that DESCRIBES a purged engine as a
   live subsystem is a DELETE, a doc that RECORDS one is a KEEP. The user-facing
   `docs/site/engines.md` is the sharpest case on the wrong side of that line:
   it advertises eleven engines where one exists, on the page a user reads to
   decide whether the product answers their question.

2. **The census's own delete list holds up.** Every row of DOCS CENSUS RULED's
   §3.1 and §3.5 name-lists was independently checked against the tree in this
   lens, and each names a file whose subject is measurably absent. Two rows are
   sharpened rather than accepted: `docs/metrics.md` deletes because it cannot
   be regenerated, not merely because it is stale; `tool-list.md` deletes because
   it is an ungated second copy of `TOOL_REGISTRY`, which is a drift MECHANISM
   rather than a drift instance.

3. **Five byte-identical duplicate pairs in `docs/reports/`** (md5-verified,
   listed in section G). Deleting one of each pair removes 947 lines and no
   information.

4. **One numeric contradiction between two live pages**:
   `docs/reports/tool-routing-failure-split.md` says 183/44/138 where
   `docs/site/models.md` says 174/45/127 for the same routing bench. Two numbers
   for one measurement is how one of them goes unread; neither is marked
   superseded.

5. **`docs/design/demo-physics-defaults-audit.md` carries a stray `</content>`
   tag** at its tail - a literal authoring artifact left in a committed file.

6. **`docs/validation/fetcher-fold-census.md` §6 verification item 5 names
   `scripts/run_sfincs_direct.py`**, which is not in the tree. This is the only
   defect found in an otherwise-current census, and it is a one-line fix.

7. **`docs/proof/templates/oceanmesh_meshes/README.txt` names three deleted
   builders as the regeneration route.** The proof tree is frozen, so this is
   reported rather than acted on - but a reader who follows it will find nothing,
   and the honest replacement is either the recipe path or an explicit statement
   that these five meshes are frozen artifacts.

8. **The DELETION_LEDGER's fourteen wave-A rows appear up to four times each,
   verbatim.** Roughly 60 duplicated table rows across four headings. The ledger
   is a standing norm's instrument and stays, but the duplication is a real
   defect: a reader counting deletions counts some of them four times, and a
   future collapse must keep exactly one copy per candidate.

9. **Three QUEUED ledger rows are marked "PLAUSIBLY MET, needs a live run - do
   not cut blind"**, and one of them (HEC-RAS 2025 native-Linux) actively
   CONTRADICTS the standing project memory note. That contradiction is named in
   the ledger itself and is carried forward here so the next wave does not
   resolve it by picking the record it happens to read first.

10. **`docs/model/` cannot be stale and is the only part of this scope with that
    property.** `scripts/model_check.py` plus `tests/test_model_conformance.py`
    run in the suite and the views regenerate in-commit, so all thirteen files
    are KEEP without further audit. `docs/specs/system-uml.html` is a second,
    ungated drawing of the same system and is deleted for exactly that reason.

11. **`docs/validation/code-graph/graph.json` is current** (1042 nodes / 3471
    edges / 0 missing paths, verified at HEAD), and `model_check.py` no longer
    reads it - it parses its own import edges at check time - so the atlas is an
    instrument product with its own readers rather than a live input that could
    silently pass a violation.

12. **Fate tally across the 191 rows of the first pass, counted from the table
    itself**: KEEP 92, DELETE 72, REWRITE 26, RE-CUT 1. No row is MOVE - nothing in this scope belongs in another tree
    (the census's target layout keeps `docs/{authoring,model,playbooks,site,
    validation}` and folds `design`/`specs`/`research`/`reports` down rather
    than relocating them), and no row is an EXEMPT candidate - the docstring
    limit and its exemption ledger govern code, and this scope holds no code.

13. **The eighteen rows added when the sibling lenses became tracked**: KEEP 14,
    REWRITE 4 (this file, COVERAGE.md's gap paragraph, and the two lens trailer
    counts that did not move when their headers were corrected). Scope total
    209: KEEP 106, DELETE 72, REWRITE 30, RE-CUT 1. The only defects the read
    found in the wave's own products are counting defects in three closing
    lines and one now-closed coverage gap; no lens carries a wrong claim about
    the tree.

files listed: 209 / files read: 209 / rows written: 209 (191 at the first pass,
plus the eighteen docs that landed after the census: the sixteen
`hygiene-manifest/` siblings that became tracked, `docs/READABILITY_LEDGER.md`
and `docs/validation/docstring-exemptions.md`)

## Added by the template-docs leg

Written and read end to end by the agent that landed them. Two classes: pages
this leg GENERATES (the generator is the reader of record - a page cannot drift
from the declaration it is written from, and the byte-currency guard proves it),
and maps this leg WROTE or CORRECTED after reading the package they describe.

### Generated pages and the run records beside them

| path | lines | fate | note |
| --- | ---: | --- | --- |
| `docs/templates/index.md` | 70 | KEEP - generated | the gallery: eight cards, each a doc composite, the question and the module |
| `docs/templates/artemis_harbor_agitation.md` | 109 | KEEP - generated | one page per registered template: the question, the module and parts, the DATA rows with producer and datum, the sheet, what it answers, the figures, the proving run's filled sheet with provenance, the reproduce block |
| `docs/templates/telemac3d_stratified_flow.md` | 140 | KEEP - generated | as above |
| `docs/templates/telemac_do_sag.md` | 158 | KEEP - generated | as above |
| `docs/templates/telemac_rain_on_grid.md` | 152 | KEEP - generated | as above; two declared animations, so two of each figure |
| `docs/templates/telemac_river_dye.md` | 160 | KEEP - generated | as above |
| `docs/templates/telemac_river_oil_spill.md` | 162 | KEEP - generated | as above |
| `docs/templates/telemac_river_scour.md` | 178 | KEEP - generated | as above |
| `docs/templates/telemac_river_sediment_plume.md` | 157 | KEEP - generated | as above |
| `docs/modules.md` | 22 | KEEP - generated | the five module wrappers: keyword count, composites, outputs, and the `describe_keywords` read over them |
| `docs/templates/<template>/run.json` (8) | - | KEEP - evidence | the committed record of the proving run a page is generated from: the invocation, the sheet, its provenance, the answer, the published layers. Written by the doc renderer; the pages read it, and nothing reads the untracked run journal |
| `docs/templates/<template>/*.png`, `*.gif` (33) | - | KEEP - evidence | the doc-sized figures, 9.0 MB total, each stamped with its run id and the commit that drew it. Frozen like `docs/proof/` in kind, but REPLACED rather than kept when the declaration moves - that is what the freshness guard forces |

### Package maps written by this leg

| path | lines | fate | note |
| --- | ---: | --- | --- |
| `trid3nt_server/README.md` | 35 | KEEP | the package root: seven modules, eleven subfolders, one line each |
| `trid3nt_server/adapters/README.md` | 20 | KEEP | the shared IR and one adapter per provider |
| `trid3nt_server/cases/README.md` | 14 | KEEP | the two seams that move a layer the other way |
| `trid3nt_server/credentials/README.md` | 14 | KEEP | the handshake, the registry, the resolver |
| `trid3nt_server/emission/README.md` | 23 | KEEP | eleven modules; the one styling seam is named as one |
| `trid3nt_server/fallbacks/README.md` | 15 | KEEP | ladders as data, one walker, the persisted activations |
| `trid3nt_server/gates/README.md` | 30 | KEEP | thirteen modules and the `cards/` table |
| `trid3nt_server/sandbox/README.md` | 14 | KEEP | the box, and what runs inside it |
| `trid3nt_server/server/README.md` | 25 | KEEP | five modules and the four subpackages |
| `trid3nt_server/testing/README.md` | 18 | KEEP | the scripted client and the declared canaries |
| `scripts/README.md` | 29 | KEEP | the eight entry points, and the four lanes below them |
| `contracts/trid3nt_contracts/README.md` | 36 | KEEP | one row per contract |

### Existing maps corrected against the tree

| path | what was wrong | fate |
| --- | --- | --- |
| `README.md` | named `server/` for `trid3nt_server/`, the multi-hazard framing, five engines where one ships, a deleted `data/` row; no module surface, gallery, contracts, tests or suite | REWRITE - done |
| `plugin/README.md` | named none of its own files or subfolders | REWRITE - a Layout section added under the user-facing page |
| `workers/README.md` | named neither `conftest.py` nor its two subfolders; the roster was prose | REWRITE - done |
| `tests/README.md` | did not name `conftest.py`; the `hygiene/` row counted three files | REWRITE - done |
| `trid3nt_server/tools/README.md` | the `fetchers/` section's rows were relative to `fetchers/`, so thirteen of them resolved to nothing from the map's own directory | REWRITE - rows re-rooted |
| `trid3nt_server/workflows/README.md` | the same defect over the `runtime/` section, nineteen rows | REWRITE - rows re-rooted |
| `trid3nt_server/workflows/telemac/templates/README.md` | did not name its own `__init__.py` | REWRITE - done |

Scope delta: `git ls-files docs | grep -v '^docs/proof/'` gains ten markdown
pages, eight run records and thirty-three figures; the tracked tree gains twelve
package maps outside `docs/`, rowed here because the map-README rule is this
lens's subject.
