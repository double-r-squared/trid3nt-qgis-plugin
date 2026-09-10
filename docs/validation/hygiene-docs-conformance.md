# Hygiene and docs wave - conformance

Written 2026-09-10, at `664ce883`. The wave's verify came back BROKEN on the
record rather than on the work: no conformance table had been written, and four
deviations had nowhere to be reported. This is that table. Every ruling in the
eleven charter entries is read against the tree and marked CONFORMS or DEVIATES
with the evidence that decided it; the four deviations sit below with the
resolutions that bind; the LOC table is re-measured on the corrected instrument
against a named ref.

Scope note: this is a DATED RECORD. It states what was true at `664ce883`, and
a later change that falsifies a line here is corrected in the deletion ledger
rather than by rewriting the line.

## The eleven charter entries

### 1. FULL COVERAGE LAW FOR THE HYGIENE AND DOCS WAVE (2026-09-09)

| ruling | verdict | evidence |
| --- | --- | --- |
| a COVERAGE MANIFEST: every product file, test, script, README and doc in scope with the agent that READ it and a per-file verdict row | CONFORMS | `docs/validation/hygiene-manifest/` - sixteen lens files plus `COVERAGE.md`, 1,958 census rows over 1,907 files |
| a file with no row FAILS the gate | CONFORMS | `COVERAGE.md` opened three gaps (`scenario_reuse.py`, `telemetry.py`, `workflows/__init__.py`), read all three end to end and rowed them; the leg deltas rowed every file the guards, the template docs and this remedy added |
| fan-out by DIRECTORY, one agent per directory, every file end to end | CONFORMS | each lens is named for its directory and carries its own scope sentence; the 49 double-rowed files are disclosed overlaps, not gaps |
| greps are sweep GUARDS after the fact, never the census | CONFORMS | `docs/CONVENTIONS.md` states it in the Enforcement section, and every guard's docstring repeats it |
| a sweep guard per class in the suite, each proven on a seeded break | CONFORMS | six guards in `tests/hygiene/` (docstring standard, history markers, dead references, map READMEs, template docs, proof coverage), 83 collected; each fired on a seeded break in its own commit, and the dead-reference guard fired twice more in this remedy |
| a COMPLETENESS CRITIC runs before verify until TWO rounds come back dry | **DEVIATES, remedied** | the original critic ran four rounds and never came back dry (the charter's own text capped it at four). Deviation 1 below; the remedy ran five more, rounds 4 and 5 dry |

### 2. TEMPLATE DOCS, THE FLOPY NOTES (2026-09-09)

| ruling | verdict | evidence |
| --- | --- | --- |
| a GALLERY INDEX with a doc-sized composite thumbnail per template, its question and the module it wraps | CONFORMS | `docs/templates/index.md`, eight cards |
| a page per template GENERATED FROM THE DECLARATION | CONFORMS | `scripts/instruments/gen_template_docs.py`; eight pages regenerate byte-identical |
| the images from the proving run under the doc-image freshness rule | CONFORMS | 33 stamped figures under `docs/templates/<template>/`; `tests/hygiene/test_template_docs.py` fails on a stale one |
| a MODULES page listing the wrappers with catalog size, composites, outputs | CONFORMS | `docs/modules.md`, five wrappers |
| one generator over the template registry, run at every acceptance | CONFORMS | the instrument is one script beside the packet renderer |

### 3. TEMPLATE DOCS, THE MODFLOW 6 EXAMPLES NOTE (2026-09-09)

| ruling | verdict | evidence |
| --- | --- | --- |
| each page carries THE SHEET OF THE PROVING RUN as its parameter table | CONFORMS | every page's sheet table is read out of its `run.json` |
| a REPRODUCE block with the exact invocation, run id and commit | CONFORMS | present on all eight pages |
| the figures with captions under the freshness rule | CONFORMS | as entry 2 |
| the proving runs double as the regression set | CONFORMS | `run.json` is committed beside each page; `tests/hygiene/test_proof_coverage.py` holds the converse for `docs/proof/` |

### 4. SCRIPTS RULED (2026-09-09)

| ruling | verdict | evidence |
| --- | --- | --- |
| the FIVE-DIRECTORY structure - `scripts/` entry points, `instruments/`, `packet/`, `drivers/`, `staging/`, and `local/` GITIGNORED | CONFORMS | all five directories present; `scripts/local/` is in `.gitignore` |
| the two stranded modules under `sandbox/` move into the product tree FIRST, and the `sys.path` hack dies | CONFORMS | `workflows/mesh/shared/formats/`; no `sys.path` insert survives in `nodes.py` |
| THE CULL, all ten recommendations | CONFORMS | six deleted, nine atticked, six to `local/`, the two tracked, `replay_canary_evidence` an instrument, the breakwater proof a driver, the two site pages rewritten, the four docs corrected, `loc_report --help` fixed |
| executes inside the wave under the full-coverage law | CONFORMS | the scripts rows are in `scripts-workers.md` with the leg deltas |

### 5. DOCS CENSUS RULED (2026-09-09)

| ruling | verdict | evidence |
| --- | --- | --- |
| (a) THE FORTY LONGEST cut with the sweep; NO `docs/method/` | CONFORMS | no such directory; docstrings 36,243 -> 11,802 |
| a derivation that governs a line becomes a COMMENT BLOCK at that line; what constrains nothing GOES | CONFORMS in outcome, and the outcome is not the one the ruling expected | comments moved +18 net against 24,441 docstring lines removed - "goes" won overwhelmingly. Stated plainly below |
| the 220 superseded records DELETED and the folder's convention replaced | CONFORMS | 218 deleted in the ruled order, 106 binding kept; `docs/decisions/README.md` states the new convention, and its index matches the folder exactly (0 present-but-unindexed, 0 indexed-but-absent) |
| the seven binding records that contradict later rulings AMENDED in place | CONFORMS | seven amended; an eighth (`0320-spec-format.md`) was amended by this remedy when its first instance was deleted |
| the six "STATUS: THINKING" notes folded or deleted | CONFORMS | `f15dcf24` |
| IDEAS re-sectioned - one heading per ruling and an index, nothing deleted | CONFORMS | `186abd63`, zero body lines touched |
| `system-uml.html` DEMOTED to a rendering of the model | **DEVIATES, stands** | it was DELETED. Deviation 2 below |
| the two content migrations run BEFORE any deletion | CONFORMS | `eb545e93` precedes `6d76ad71` |
| (c) the guards, the exemption ledger, no glob or directory exemptions | CONFORMS | `test_docstring_standard.py`; 8 exemption rows, each a per-symbol marker, ledger byte-equal to the tree |
| the same test checks every map README and covers every top-level module, landing LAST | CONFORMS | `test_map_readmes.py` lands after the twelve maps |
| frozen evidence: the SWAN and TOMAWAC proof trees DELETED, `coastal_tidal_surge` kept, a proof-coverage assertion, the README's variant law stated as governing renders | CONFORMS | `0eff0225`; the assertion is `xfail(strict=True)` naming the three templates with no packet |
| superseded material is DELETED OUTRIGHT because git is the archive | CONFORMS, and applied twice more here | the two landed plans and one staging file, this remedy's fourth commit |

### 6. THE DOCSTRING LIMIT RULED (2026-09-09)

| ruling | verdict | evidence |
| --- | --- | --- |
| 3 content lines for a function or class, 5 for a module | CONFORMS | `test_internal_docstrings_stay_within_the_content_limit`, 0 over |
| a longer genuine contract carries `# docstring-exempt: <reason>` on a regenerated ledger | CONFORMS | 8 rows; `test_the_exemption_ledger_is_the_tree` compares byte for byte |
| past roughly ten entries the limit is re-argued, never routed around | CONFORMS | `test_the_ledger_stays_short_enough_to_argue_about` asserts <= 10 |
| full-line COMMENT BLOCKS are UNCAPPED | CONFORMS | no length assertion touches a comment block |
| LLM-facing tool docstrings keep the 1,000-char front budget, enforced on the 19 over it | CONFORMS | `test_llm_facing_docstrings_stay_within_the_front_budget`, 0 over |
| `docs/CONVENTIONS.md` rewritten to state exactly this | CONFORMS | and extended twice by this remedy, for the guard's widened scope |

### 7. COMMENTS, THE CLEAN CODE CH. 4 DISCUSSION (2026-09-09)

| ruling | verdict | evidence |
| --- | --- | --- |
| the wave is DOCUMENTATION ONLY: never touch a name, a signature or a body | **DEVIATES, accepted as documentation** | eleven function bodies changed - all inside STRING LITERALS. Deviation 3 below |
| it deletes the disallowed classes, trims docstrings to the contract, moves a derivation into a comment block or drops it | CONFORMS | `test_no_disallowed_prose_classes` is green on both origins, and this remedy re-asked it over trailing comments too, which no guard reads: 0 |
| naming-driven comment elimination belongs to whichever wave owns the module | CONFORMS | nothing in `docs/READABILITY_LEDGER.md` was applied |

### 8. TESTS RULED (2026-09-09)

| ruling | verdict | evidence |
| --- | --- | --- |
| the MIRROR TREE by subsystem | CONFORMS | 308 files moved, one commit per destination |
| SIX SLICES by directory; the standing line becomes "all six slices, zero failures" | CONFORMS | six `Makefile` targets; `AGENTS.md` law 1 states them and no longer names five alphabet slices |
| the import-mode change lands FIRST as its own green checkpoint | CONFORMS | `757d1b40` precedes every move |
| `plugin/tests` and `contracts/tests` stay in their distributions, joining as the sixth slice | CONFORMS | `test-packages` |
| the three straddling files get HOMES not splits; the two milestone-named plugin files SPLIT by subject | CONFORMS | recorded in `tests-eval.md` and applied |
| the fuzz cross product COLLAPSES to a per-pattern sweep | CONFORMS | `tests/tools/test_gemini_kwargs_fuzz.py` |
| THE NINE Qt-shim tests NAMED AS STANDING EXCEPTIONS | CONFORMS, with the landing's own correction | `tests/README.md` names them; eight carry the marker, and the ninth is not one - stated in place |
| the cull is 1,627 pure LOC | CONFORMS | tests pure 80,896 -> 80,085 net, which is the cull less 594 of new guards and the mirror's own additions |

### 9. READABILITY LEDGER (2026-09-09)

| ruling | verdict | evidence |
| --- | --- | --- |
| every comment or docstring a better name or small extraction would remove becomes a row (file, line, comment, change, risk) | CONFORMS | `docs/READABILITY_LEDGER.md`, 257 rows in five columns (244 product + 13 tests) |
| NOTHING is applied by the wave | CONFORMS | no row's change appears in any commit |
| the row's LINE is usable | **DEVIATED at verify, fixed here** | 74 rows named a line the trims had moved; all 241 original rows were re-resolved by symbol at HEAD and 139 now name a new line |
| tests/ carries rows of its own | **DEVIATED at verify, fixed here** | zero rows from `tests/` at verify; 13 added, twelve of them one class the product tree does not have (a helper copied verbatim into up to nine files) |

### 10. DATED RECORDS STAY VERBATIM (2026-09-09)

| ruling | verdict | evidence |
| --- | --- | --- |
| a dated validation or conformance record is the record of what was true at that check | CONFORMS | `mesh-wave-conformance.md:97` left verbatim with its note |
| a later change that falsifies its wording does NOT rewrite the finding; the correction lives in the ledger row | CONFORMS, and load-bearing in this remedy | the manifest rows for the three files deleted here (two REWRITE, one KEEP) are left verbatim; the correction is `DELETION_LEDGER.md`'s "The two landed plans" section |
| same class as `docs/proof`: evidence is frozen, method and maps are live | CONFORMS | the dead-reference guard scans the maps and the manual and skips `docs/design/`, `docs/validation/`, `docs/reports/`, `docs/decisions/` |

### 11. THE DOCSTRING COUNT CLARIFIED (2026-09-09)

| ruling | verdict | evidence |
| --- | --- | --- |
| "3 lines" and "5 lines" count CONTENT lines - the non-blank lines between the delimiters | CONFORMS | `Docstring.content_lines` in `tests/hygiene/_source.py` |
| the LOC instrument double-subtracted blank lines inside docstrings; fixed in the guards leg | CONFORMS | `de2544ef`; the comment at `loc_report.classify` states the partition |
| the baselines are restated from the corrected measure | CONFORMS | the table below, re-measured with that instrument |

## The four deviations, and their resolutions

| # | deviation | resolution | state at `664ce883` |
| --- | --- | --- | --- |
| 1 | the completeness critic ran four rounds and never came back dry; the charter's own text capped it at four | RUN IT UNTIL DRY | five more rounds ran. Round 1 found and fixed one dead reference in a live README; round 2 dry; round 3 widened Q4 to every live document, found two more dead references plus one the widened guard found itself, and closed the guard hole; rounds 4 and 5 dry - the two consecutive the law asks for. Reports in the remedy's round files |
| 2 | `system-uml.html` was DELETED, not demoted | the deletion STANDS | the model views under `docs/model/` are the live rendering, checked by `scripts/instruments/model_check.py` and `tests/model/`; a second hand-drawn copy is drift with nothing to catch it, which is the MRE rule. `0320-spec-format.md`'s 2026-09-09 amendment states it as law |
| 3 | eleven function bodies changed - pydantic `Field` descriptions in `tool_registry.py` and `ws.py`, attribute docstrings, log and error text - and `atomic_tool_metadata.json` regenerated | ACCEPTED as documentation | a `Field(description=...)` is prose the model and the user read; no name, signature or logic moved, and the regenerated schema is the honest consequence of editing prose pydantic exports. The wave's own round 3 recorded why no guard saw them: `_source.py` reads docstrings and comment tokens, so a string literal is outside every sweep, and extending the sweep to literals would red on three populations the wave has ruled it will not clean |
| 4 | `AGENTS.md` and `docs/CONVENTIONS.md` sat outside the dead-reference guard | the guard's scope GAINS both | `LAW_DOCS` in `test_dead_references.py`; seeded break fired on a fabricated instrument path in one and a fabricated module path in the other, and both were restored. `docs/CONVENTIONS.md` states the scope |

Three further corrections the verify named, all discharged: `tests/README`'s
test-server count (1067 -> 1143, with two directory rows corrected in the same
pass), the 74 moved ledger lines (139 re-resolved), and the absent `tests/` rows
(13 added).

## Reported, not fixed

- **A fifth line in the string-literal class.** The wave's round 3 measured the
  prose-inside-a-function-body remainder at FOUR lines. `scripts/drivers/
  seed_showcase_cases.py:98` names `scripts/proof_elmfire_river_barrier.py`,
  deleted with the ELMFIRE engine, inside a case-description string. The
  measurement is five. Same class, same ruling: outside a documentation-only
  wave.
- **One conditional delete.** `fetch_goes_blend_animation/corpus.yaml` carries
  "DELETE candidate -- follows the spec's fate". Its tool still registers, and
  `test_every_registered_tool_has_corpus_queries` requires a registered tool to
  have queries, so removing the file now would red the suite. The condition is
  the deprecated spec's own fate, undecided.

## LOC, on the corrected instrument

Baseline is `d41822c0`, the commit that landed the full-coverage law and opened
the wave; "now" is `664ce883`. Both measured with `scripts/instruments/
loc_report.py`'s `classify` AFTER the blank-in-docstring fix, so the two sides
are the same instrument. Product is `trid3nt_server` + `plugin` + `contracts` +
`scripts` + `workers`; `docs/` and non-Python never count.

| measure | baseline `d41822c0` | now `664ce883` | delta |
| --- | ---: | ---: | ---: |
| PRODUCT pure | 102,887 | 100,411 | **-2,476** |
| PRODUCT docstring | 36,243 | 11,802 | **-24,441** |
| PRODUCT comment | 17,274 | 17,292 | **+18** |
| PRODUCT total | 175,275 | 147,997 | -27,278 |
| TESTS pure | 80,896 | 80,085 | -811 |
| TESTS docstring | 12,912 | 7,591 | -5,321 |
| TESTS comment | 9,137 | 8,994 | -143 |

The verify recorded -2,445 product pure and -24,416 docstring against its own
baseline; this table's baseline is 31 and 25 lines higher because it is taken at
the charter commit rather than at whatever ref the verify used, which it did not
name. The difference is two small commits of ordinary work, not a measurement
disagreement, and naming the ref is the point of restating it.

Tests lose 811 pure lines NET while gaining 594 of new guards in
`tests/hygiene/`, so the cull itself is larger than the net says.

## What the numbers say about the ruling that produced them

The census ruled that a derivation governing a line of code BECOMES a full-line
comment block at that line, and that what constrains nothing GOES. Both halves
were available; only one was used. Against 24,441 docstring lines removed from
the product tree, comments moved by EIGHTEEN. Almost nothing was converted.

That is not a failure of the sweep and it is not a silent choice: it is the
measured answer to a question the ruling left open. The overwhelming majority of
what the forty longest docstrings and their thousands of smaller siblings
carried was not a constraint on the line below - it was history, rationale,
roll-calls of arguments the signature already lists, usage examples, and
architecture cross-references. None of that governs a line, so none of it earned
a comment block. "Goes" won, and git is the archive.
