# Worker-unification wave - spec-conformance gate

Fresh-eyes clause walk of `docs/specs/square-two.html` (rev 1, 2026-08-28) -
section 4 as the wave's own charter, sections 1/2/3/6 as the standing bars -
against the landed tree, plus the plan at `~/.claude/plans/mossy-rolling-fairy.md`
and the `docs/IDEAS.md` rulings of 2026-08-30 and 2026-08-31. Deviations are
REPORTED, never fixed.

Wave span `cb2234b4..49542f14`. Live proof runs are on `c5591c2c`; HEAD is one
docs-only commit ahead of them.

Suite at the time of this walk, five slices, commands verbatim from ADR 0321:

| slice | passed | skipped | failed | wall |
|---|---|---|---|---|
| `test_[a-e]*` | 1737 | 5 | 0 | 205.68s |
| `test_[f-o]*` | 4160 | 0 (1 xfailed) | 0 | 43.44s |
| `test_[p-r]*` | 1927 | 1 | 0 | 336.30s |
| `test_[s-z]*` | 1397 | 6 | 0 | 306.76s |
| `contracts/tests` | 789 | 0 | 0 | 5.27s |

**10,010 passed, 0 failed, five slices at ZERO.** Against the last recorded
baseline (ADR 0321 amendment 30d: 1736 / 4155 / 1924 / 1337 / 789 = 9,941) the
denominator grows by 69 - the wave's own offline tests for the author, the
topology bundle and the run readers - and nothing left silently. `workers/` is walked by no slice: the worker's own
`test_entrypoint.py` (36 tests) passes when pointed at directly, and the rest of
its evidence is the image's build-time smoke blocks plus the live runs.

---

## 1. Spec section 4 - the worker unification

| clause | implementation | verdict |
|---|---|---|
| "The worker stops knowing processes." | `workers/telemac/entrypoint.py:385-389` is a three-row dispatch `{case, agitation, stratified}`; the section a manifest carries IS the routing decision. Five per-process pipelines, five strict-config gates and six run-wrappers are gone. | CONFORMS, with the ruled carve-out below |
| "The deck/manifest declares the telapy module, the staged inputs, and the result files" | `case` section = `{module, steering, user_fortran, results, family, echo, coupling, continue_from}` (`entrypoint.py:89-91`), written by ONE server writer `steps/open_water.py::case_section` (`:227`) which `deck.py:92` and `rain_on_grid.py` both delegate to. Live: `data/runs/01M1CGTRE1MV0QQBWX7MH1XYFQ/manifest.json` carries `module=telemac2d, results=['r2d_river.slf','restart_river.slf']`. | CONFORMS |
| "the entrypoint is read manifest -> stage -> telapy runs the case -> collect -> out" | `main()` (`:399`) reads, `_solve_case` (`:284`) gates and dispatches, `_run_child` (`:248`) tees, `_solve_in_process` (`:190`) drives telapy's own `run_one_time_step` loop. | CONFORMS, with the launcher deviation below |
| "NATE pre-deleted the five per-process build scripts" | committed at `9102ec61` with ledger lines (`rog_build`, `telemac_coastal_build`, `telemac_river_dye_build`, `tomawac_build`, `rainfall_forcing_compare`). | CONFORMS |
| "residual build logic moves server-side into deck authoring (the sole-authored-record law) or dies" | `steps/author.py` (1,321) holds every `.cas` writer for the case families; `mesh/topology.py`, `mesh/shared/nodes.py::fit_downstream_bed`, `steps/run_reads.py` hold the rest. `grep` over `workers/telemac/*.py` finds NO `.cas` on the case path, NO `gmsh`, NO `requests`/`urllib`/`httpx`/`boto3`, NO `fetch_`. | CONFORMS on the case path; DEVIATION D-1 on the two legacy builders |
| "Ends with an image rebuild + one coarse smoke per process family THROUGH the image." | `trid3nt-local/telemac:latest` built 2026-08-31T09:58:43. Provenance checked: the in-image `md5(/opt/trid3nt/workers/telemac/entrypoint.py)` = `353756fbc4b08fac2d5813b4f80ba909` = the working-tree file exactly. `.dockerignore` excludes `tests/`, and the image carries no `test_*.py`. Seven families solved through it (table in section 5). | CONFORMS |
| "Box contract unchanged; `--network none` unchanged." | `run_telemac.py:148` passes `network="none"`; `mesh/shared/selafin_cli.py:72` and `mesh/meshers/om2d.py:646` do the same for the mesh drivers. | CONFORMS |

### D-1 - the two legacy builders still author in-worker (RULED, not a drift)

`artemis_build.py` (1,138) and `telemac3d_build.py` (1,001) still write their own
`.cas` in-container (`artemis_build.py:835,957,1038`; `telemac3d_build.py:734,817,882`)
and still shell `subprocess.run` on their own launchers. That is 2,389 of the
worker's remaining 2,873 product lines - 83 percent.

This is NATE's own fork ruling (IDEAS 2026-08-30, WORKER-UNIFICATION WAVE
PLANNED): "artemis_build + telemac3d_build STAY LIVE in-worker behind the unified
dispatch, never extended, awaiting rung 4." Both now run BEHIND the one dispatch
(`entrypoint.py:_solve_agitation` / `_solve_stratified`) through the one strict gate, which is what the ruling
asked. Recorded here because a reader grepping the spec's "no `.cas` in the
worker" bar will hit these two files and should meet the ruling rather than a
surprise.

### D-2 - the WAQTEL/GAIA launcher deviation (RULED, ledgered, with a die-date)

`_LAUNCHER_COUPLINGS = {"waqtel", "gaia"}` (`entrypoint.py:101`): a coupled case
runs `telemac2d.py <cas>` in the child instead of telapy. Ruled at IDEAS
2026-08-31 (PROOF STOPS, ruling 1), ledgered with both measured telapy failures
(`OS (BIEF): OBJECT TYPE NOT IMPLEMENTED: 0` for WAQTEL; `HERMES_FILE_NOT_OPENED_ERR`
at finalize for GAIA) and a stated condition to die. It sits behind the SAME
runner seam, manifest, tee, timeout and success convention. Live: the do_sag and
sediment manifests carry `coupling=waqtel` / `coupling=gaia` and both reached
CORRECT END.

**The reentrant caveat, stated here beside the ruling because the ruling's own
words are "reentrant by default":** coupled (launcher) classes author NO
`RESTART FILE` and cannot be continued. `steps/deck.py` authors
`restart=None if coupled_with else _RESTART`; the worker refuses
`continue_from` on a coupled case at `entrypoint.py:319` (`TELEMAC_CASE_NOT_CONTINUABLE`);
and the live manifests show it - the dye and oil runs declare
`restart_river.slf` among their results, the do_sag and sediment runs declare
none. Reentrant-by-default holds for the pure-telemac2d classes ONLY. This is
carried finding 12.

---

## 2. Spec section 1 - the principle (library-first)

| domain job | the spec's library | what the tree does | verdict |
|---|---|---|---|
| data acquisition | the fetcher substrate (FROZEN) | untouched, measured: `git diff --stat cb2234b4 HEAD -- trid3nt_server/tools/fetchers/` is EMPTY, as are the same diffs over `trid3nt_server/emission/`, `contracts/` and `docs/proof/` | CONFORMS |
| basin delineation | pysheds | `delineate_watershed` wraps it; the wave's only change was the CRS conformance repair (`2be76449`) | CONFORMS |
| corridor construction | shapely/pyproj | `combine`, `endpoints`, `section` are registered tools over shapely | CONFORMS |
| mesh generation | oceanmesh | the om2d driver in `trid3nt-local/mesh:latest` | CONFORMS |
| TELEMAC geometry + boundary files | telapy (HermesFile, Conlim numliq, get_ipobo) | `mesh/shared/selafin_cli.py` (84) shells `trid3nt-local/telemac:latest` and `meshers/drivers/selafin_cli_driver.py:152` calls `telapy.api.hermes`. The numliq order is MEASURED, not assumed. | CONFORMS |
| deck authoring | **telapy `TelemacCas` + the dico, library-validated** | `steps/author.py` hand-writes keyword text and `Path(...).write_text("\n".join(lines))` (`:208, :495, :1103, :1318`). `TelemacCas` appears NOWHERE in `trid3nt_server/`. | **DEVIATION - see finding N-1** |
| the solve | telapy runners | `_solve_in_process` over `telapy.api.{t2d,t3d,wac,art}` | CONFORMS (with D-2) |
| results reading | telapy `TelemacFile` / `data_manip` | `telemac/result_reader.py` (110) shells `trid3nt-local/telemac:latest` and `meshers/drivers/telemac_result_driver.py` calls `data_manip.extraction.telemac_file.TelemacFile`. `postprocess_telemac.py`, `steps/run_reads.py` and `steps/deck.py` read through it and import no `struct`. | CONFORMS (N-2 CLOSED) |
| display + styling | MDAL/QGIS + the emission seam (FROZEN) | untouched | CONFORMS |

Library-first grep over every module the wave ADDED - `steps/author.py`,
`mesh/topology.py`, `mesh/shared/nodes.py`, `steps/run_reads.py`,
`mesh/shared/selafin_cli.py` - finds numpy, shapely, pyproj and rasterio doing the
math and the IO, and no hand-rolled numerics. `fit_downstream_bed` fits its plane
with `np.linalg.lstsq` (`nodes.py:211`), clips with `np.clip`, and reports
`measured_slope` beside `enforced_slope`. The two deviations above are both
PRE-EXISTING surfaces the wave inherited and built on, not code it wrote.

## 3. Spec section 2 - the tree after focus

`trid3nt_server/workflows/` holds exactly `telemac`, `mesh`, `lib`, `shared`,
`solver`. `workers/` holds exactly `telemac`, `mesh`, `qgis`, `conftest.py`,
`README.md`. Eighteen non-telemac worker directories are mirrored at
`~/Documents/trid3nt-attic/workers/` (19 entries - the pre-deleted telemac
payloads are there too), each with a DELETION_LEDGER line. **CONFORMS.**

## 4. Spec section 6 - standing acceptance, every rung

| gate | evidence | verdict |
|---|---|---|
| library-first | section 2 above | CONFORMS on new code; two inherited deviations reported |
| LEGO law - no mesher/stage carries domain-prep | `grep -niE "fetch_\|delineate\|navigate\|nldi"` over `workflows/mesh/meshers/*.py` returns only the declared bed-row NAME `"fetch_topobathy"` and the two `fetch_activation_rows` / `fetch_fallback_note` provenance readers. No acquisition. | CONFORMS |
| suite - re-baselined slices at ZERO, scripted lane only | section 0; no UI work in the wave | CONFORMS |
| records - recipe/journal honest, ledgers per landing and per move | every one of the 22 non-attic deletions/renames in the span has a DELETION_LEDGER mention (checked by name, one by one). Live journal row `01M1CHZ6RK5JEFN5GD2PA9B2DS` carries the measured banks line and the resolution-sensitivity note; `provenance` names basis per param. | CONFORMS |
| close-out - fresh-eyes clause walk + live walkthroughs, deviations reported | this document | CONFORMS |

---

## 5. Live transcripts

Read off the run directories, not off a report. Every row is `status=ok`,
`correct_end=true`, `code_sha c5591c2c`.

| run id | family | module | wall_s | npoin | manifest case |
|---|---|---|---|---|---|
| `01M1CGTRE1MV0QQBWX7MH1XYFQ` | reach (dye tracer) | telemac2d | 5.07 | 907 | telapy, results `r2d_river.slf` + `restart_river.slf` |
| `01M1CH3NFCXA8Y3NVF52ZGG7GE` | reach (do_sag) | telemac2d | 6.54 | 658 | `coupling=waqtel` - launcher, NO restart |
| `01M1CH6J14DWTYHGV8KHY4Y7XW` | reach (oil) | telemac2d | 5.43 | 907 | `user_fortran=user_fortran`, results + `drogues.txt` |
| `01M1CHA7HV1EZV9ZGMZNE2HXX3` | reach (sediment) | telemac2d | 6.76 | 907 | `coupling=gaia` - launcher, NO restart |
| `01M1CHEWX0BF2V01SRYMCKWZR9` | rain_on_grid | telemac2d | 11.94 | 4998 | telapy, `r2d_rog.slf` |
| `01M1CHJPTNH0036D21QRW6HR3V` | agitation | artemis | 5.02 | 2738 | in-worker builder behind the dispatch |
| `01M1CHMKD12BEGBFRM976DZ0GB` | stratified | telemac3d | 9.80 | 494 | in-worker builder behind the dispatch |
| `01M1CHVSR933PSNHAXS8BF6GM4` | split A straight | telemac2d | 5.68 | 907 | telapy |
| `01M1CHRC9FJR5JXNF26GGGMY8T` | split B1 | telemac2d | 5.02 | 907 | telapy |
| `01M1CHZ6RK5JEFN5GD2PA9B2DS` | split B2 continued | telemac2d | 5.04 | 907 | `continue_from=previous.slf` |

**The measured-numliq deck, checked on the deck the run actually solved.**
`data/runs/01M1CGTRE1MV0QQBWX7MH1XYFQ/t2d_river.cas` line 6 records
`Measured liquid-boundary order: ['outflow', 'inflow']`, and lines 25-26 read
`PRESCRIBED FLOWRATES = 0.0;2.2` / `PRESCRIBED ELEVATIONS = 15.116;0.0`. The
discharge is on slot 2 = the inflow. The two-pass probe-solve is retired and the
deck is right the first time.

**The echo, verbatim in the metrics.** `telemac_metrics.json` for that run carries
`utm_epsg 32610`, one `bbox`, `npoin 907`, `nelem 1615`, `mesh_size_m 10.415`,
`bed_source` naming the staged raster - all server measurements copied through,
none re-derived in the container.

**The E2E witness the FLIP ruling asked for** (journal + solve), run
`01M1CHZ6RK5JEFN5GD2PA9B2DS`:

> reach banks: 100.0% of the modelled centreline is covered by mapped water
> polygons; any stretch NHD maps only as a flowline carries no surveyed width and
> is not in the domain this run solved over.

**Staleness, asserted rather than assumed.** `code_provenance.staleness` for a
PRE-WAVE run (`cb2234b4`) returns
`{'kind': 'engine_code_moved', 'engine': 'telemac', 'commit_count': 16, ...}` -
"STALE vs CODE: 16 commit(s) have touched the telemac engine's paths since this
run was dispatched". The same call for `c5591c2c` and for HEAD returns `None`.
The warning on pre-wave runs is expected, and it fires.

---

## 6. The plan's own verification list

| plan line | verdict |
|---|---|
| "Image builds from the worktree (it cannot today)" | MET - built, and the in-image entrypoint is byte-identical to HEAD |
| "no `.cas` authoring, no mesh/fetch/gmsh code, no `subprocess.*telemac2d.py` on the generic path" in `workers/telemac/` | MET on the generic path; the two legacy builders keep theirs by ruling (D-1), and the one `subprocess` on the case path is the ruled coupling launcher (D-2) |
| "Offline suite at the new zero; the 3 rain_on_grid awaiting-port failures green" | MET - `telemac_rain_on_grid` unparked (`test_door_dissolution.EXPECTED_TEMPLATES`), slices green |
| "Five+ coarse live solves CORRECT END with products published" | MET - ten runs, seven distinct families |
| "completion.json consumer keys unchanged (`solve.py`, diagnostics, products readers all satisfied)" | **NOT MET for one consumer** - the packet assembler. Carried finding 1. |
| "Every deletion/move has a ledger line" | MET - checked name by name |
| `entrypoint.py` ~220 LOC | **477** - deviation D-3 |
| `run_telemac.py` 569 -> ~220 | **188** - beat the target |
| `steps/author.py` ~700 LOC | **1,321** - deviation D-3 |
| net ~ -7,500 beyond the attic moves | **-3,823** tree-wide; **-7,499** on the worker column. See the LOC ledger. |

### D-3 - the two size targets missed, and why

`entrypoint.py` is 477 against a planned ~220. The 257 lines are four things the
plan did not have when it was written and three of them are NATE rulings landed
mid-wave: the solve timeout and its typed metrics write (~45), the stepped
per-step loop plus `_step_count` and the `_on_step` seam (~50), the continuation
staging and its two refusals (~35), and the launcher-coupling arm (~25); the rest
is docstring - the file is roughly one third prose. `steps/author.py` is 1,321
against ~700 because it absorbed seven deck writers plus NESTOR's three files,
the oil steering and the rain-on-grid hyetograph, i.e. what the five deleted
build scripts carried (4,983 lines) reduced by 73 percent. Both are reported, not
argued away: the numbers in the plan were estimates, and these are what landed.

---

## 7. The 2026-08-30 / 2026-08-31 rulings, walked

| ruling | landed as | verdict |
|---|---|---|
| DATA is a CLASS BODY, the role prefix dies; one word `tool(...)` | `class DATA: centerline = tool("fetch_nhdplus_nldi_navigate", ...)` in both reach templates; refs are attribute access | CONFORMS |
| AUTO EDGE DIES - `mesh_resolution_m` required, labeled default | the reach canaries declare 12 m plainly (`6754c5ff`); no derivation code owns a granularity judgment | CONFORMS |
| telapy-in-the-worker; crash isolation kept | `_solve_in_process` in a CHILD; a Fortran STOP cannot take the metrics write down | CONFORMS |
| `.cas` AUTHORING MIGRATES SERVER-SIDE; the two-pass probe-solve is OBSOLETE | section 1 and the live deck above | CONFORMS |
| ONE manifest writer with `case` + echo; ONE strict gate; ONE dispatch table | `case_section` / `stage_telemac_manifest`; `_strict_section` + `UnknownManifestFieldError`; `_DISPATCH` | CONFORMS |
| `mesh_only` dies; worker reach modules die; `_supplied_mesh` stays | `mesh_only` gone; `_staged_mesh` / `_staged_reach` gone; `_supplied_mesh` (197) stays for artemis BYO; **`_staged_bed` (53) also stays** - read by both legacy builders, ledgered with a die-date | CONFORMS (the `_staged_bed` survival is ledgered) |
| PHYSICS shim + `channel_width_m` + `bank_source` die (P3) | `grep` finds none of the three in `trid3nt_server/` | CONFORMS |
| wave_field + coastal_tidal_surge go DARK | both `register_workflow(parked=...)`; `PARKED_TEMPLATES` pins them; registry 170 -> 169 | CONFORMS |
| rain_on_grid UNPARKS on the declared-outlet mechanism | `boundaries={"outflow": Point(snapped pour point)}`; outlet hydrograph read off the listing's own FLUX series; live run 4,998 nodes CORRECT END | CONFORMS |
| BOUNDARY ROLES ARE CONTIGUOUS BY CONSTRUCTION | `topology.py::match_boundary_roles` takes the contour in walk order and constructs the run between the nodes nearest a face's ends; ledgered with the measured `.III...OO.OO...IIII` scatter that forced it | CONFORMS |
| STEPPABLE + REENTRANT | the child loops `run_one_time_step`; `_on_step` is the ONE structural hook and does nothing; continuation is the engine's own `RESTART FILE`; split run closes BIT-IDENTICALLY | CONFORMS, with the coupled-class caveat in D-2 |
| RESOLUTION LADDER REJECTED - mesh-coverage heuristic instead | NOT BUILT this wave. The PARAMS ruling of the same day places it in the post-wave mechanical fold; `grep` finds no `mesh_coverage`. | DEFERRED BY RULING, not a deviation |
| PARAMS class body | explicitly "FOLDS AFTER the worker wave" | DEFERRED BY RULING |
| DIRECTORY MAPS - a README map per major package dir | `workers/README.md` roster updated; `trid3nt_server/workflows/`, `workflows/mesh/` have none. The ruling says the first authoring pass "rides the post-wave fold once the worker wave stops moving the tree". | DEFERRED BY RULING - with one stale line, N-5 |
| TEST CULL SCOPE - a surviving test of a dead function is an anchor | the ten worker test modules went WITH their subjects in the same landing. One counter-example survives in the server tree: N-4. | CONFORMS in the worker; one exception |

---

## 8. Carried findings - the thirteen, with a severity read

Filed at `docs/validation/worker-unification-proof-interrogation.md` and fixed by
NOBODY this wave, per the wave's own charter. What this gate adds is the column
NATE needs to batch-rule them: **does this block the wave's claims, or is it
downstream template/canary debt?**

| # | finding | blocks a WAVE claim? | read |
|---|---|---|---|
| 1 | unified metrics dropped `result_slf` / `ntimestep`; the packet assembler went blind | **YES** | The plan's verification line "completion.json consumer keys unchanged" is not true - one consumer broke and was carried by a scratchpad-only shim. Narrowing worth having: `result_slf` IS still in `run_telemac.py::_COMPLETION_METRIC_KEYS` (`:48-52`), so the server-side channel survives and only the worker's producer is gone. The fix is plausibly one echo key, not a new channel - but which channel is still NATE's call. |
| 2 | `output_interval_min` accepted and silently ignored on the reach path | **YES** | An accepted-and-ignored knob is the exact failure the strict manifest gate exists to prevent, one layer up. Two refined canaries have been silently getting 6 frames for 30. |
| 3 | do_sag: the BOD never enters; the "sag" is the inflow boundary | no - template physics | The wave's claim is plumbing (the launcher-deviation path ran, WAQTEL tracers present) and that IS proven. The DO-sag PHYSICS is not, and the packet says so. Downstream debt, but the largest of it. |
| 4 | `spill_fraction` runs backwards along the reach | no - template semantics | Journal-honesty adjunct: the provenance row states "0=upstream..1=downstream" on a run where it was not. |
| 5 | the reach mesh is not on the water | no - substrate judgment | Affects every reach run in the wave equally; a canary-siting call. |
| 6 | canvas view basin-scale, answer is a dot | no - layer-extent policy | Render policy, not this wave's surface. |
| 7 | substance is a label, not a physics fork, on two of three legs | no - declaration surface | The oil leg's distinct PRODUCTS are real; its field is the dye field. |
| 8 | two published results declared `role="input"` | no - declaration | Two-line declaration fix when ruled. |
| 9 | sediment: deposition scalar contradicts its own field | no - reader semantics | `deposited_mass_kg = -0.0` beside 78 mm of measured bed evolution. |
| 10 | rain_on_grid: peak is the truncation; headline depth is a DEM pit | no - physics/declaration | The MECHANISM (delineate -> mesh -> infiltrate -> solve -> hydrograph) is what the wave claims and it is proven; the window, the bed conditioning and the mask floor are three separate calls. |
| 11 | ARTEMIS canary models no harbour | no - canary naming | The run's own metrics say so honestly (`structure_note`). |
| 12 | coupled runs are not reentrant, by construction | **PARTLY** | "Reentrant by default" is a wave claim and it holds for pure-telemac2d only. Stated in D-2 beside the ruling, which is where this gate was asked to put it. Deliberate and visible in code, not a regression. |
| 13 | TELEMAC3D: no basemap, and the domain includes land | no - canary/framing | Framing unverifiable; land cells solved as water is a legacy-builder domain, awaiting rung 4. |

Three block a claim (1, 2, and 12 partly). Ten are downstream template/canary
debt that the wave's own packets already state honestly.

---

## 9. New findings from this gate

Deviations found by this walk that are not among the thirteen. Reported, not
fixed.

**N-1. Decks are hand-written text, not `TelemacCas`-validated.** Spec section 1
names "telapy `TelemacCas` + the dico" as the library that runs deck authoring,
"library-validated". `steps/author.py` writes keyword lines and joins them. The
consequence is already in the ledger as a measured incident: a bare absolute path
in the rain-on-grid `FORTRAN FILE` line opened a damocles comment, erased the
keyword AND swallowed the line after it, and the hyetograph run died in the image
as "missing file for FORTRAN FILE: EQUATIONS". That was FOUND by probing with
`TelemacCas` inside the image - the library was used as the post-mortem
instrument for a defect it would have caught at authoring time. The structural
cause is real and not laziness: telapy is image-only on this machine
(`import telapy` fails in `venvs/agent`), and the wave already built the
driver-in-image pattern for the geometry pair (`mesh/shared/selafin_cli.py`, 84
lines) without extending it to the deck.

**N-2. The results reader is a hand-rolled `struct` SELAFIN parser. CLOSED
2026-09-02.** Spec section 1 names telapy `TelemacFile` / `data_manip` for
results reading. `postprocess_telemac.py` carried 113 lines of big-endian Fortran
record parsing around `struct.unpack(">i", ...)`, and `steps/run_reads.py` was a
new consumer of it. Elegance-review P6 killed the 88-line struct WRITER on
exactly this argument; the reader was not in scope then and survived.

It is now `result_reader.read_selafin`, an in-image driver on the pattern the
wave built for the geometry pair: `TelemacFile` inside
`trid3nt-local/telemac:latest`, `--network none`, the result directory mounted
read-only. Equivalence was measured against the parser it replaced over 156 real
result files in the tree (2D, 3D, GAIA, TOMAWAC, ARTEMIS, coastal, rain-on-grid):
155 agree bit-for-bit on coordinates, element table, instants and every field,
and the 156th is a truncated `gaia_river.slf` the struct parser refuses with
`EOFError` and the engine reads without complaint. Two behaviour deltas, both
corrections: variable names arrive without the record's unit glued on
(`"WATER DEPTH"`, not `"WATER DEPTH     M"`), and `title`, which no reader in the
tree consumed, is gone. Cost: ~1.2 s of container startup per read, ~2.0 s on a
97 MB / 78-frame result. Nothing loops a read - each postprocess reads its result
once and works in memory - so no batching was needed.

**N-3. The two dark fronts are 2,078 lines nothing can reach.**
`wave_field/` (413) + `coastal_tidal_surge/` (529) + `steps/wave.py` (271) +
`steps/coastal.py` (357) product, plus `test_tomawac_wave_field.py` (178) +
`test_coastal_tidal_surge.py` (330) test. Parked is the right STATE per the
ruling. The observation is different: the fork ruling also says rung 4 rebuilds
them as fresh expressions and "the attic is never a restoration source" - so no
future landing will read these lines, and they are refactor cost every mesh /
declaration / emission change pays. Their solver rows are live too:
`run_telemac.py:34,37` still register `tomawac_wave` and `telemac_coastal` in
`SOLVER_WORKFLOW_REGISTRY` for templates that no longer reach the model surface.
`steps/open_water.py` (536) is now consumed by these two parked modules and by
`deck.py`'s delegation only.

**N-4. `streeter_phelps.py` (92) has no production consumer.** Its own docstring
says it is "the deterministic analytical reference the DO-sag template overlays
against its computed profile", and no module in `trid3nt_server/` or `scripts/`
imports it - only `tests/test_telemac_do_sag.py` does. The overlay consumer died
in an earlier wave and the test is now the anchor the 2026-08-31 TEST CULL ruling
names. Sharper than an MRE line: this is the exact instrument that would have
caught carried finding 3, sitting unwired beside the template whose physics is
unproven.

**N-5. `workers/README.md` cites a path that does not exist.** The roster line
names `tools/simulation/solver.py` for the `run_solver` / `wait_for_completion`
seam; the file is `trid3nt_server/workflows/solver/solver.py`. Mechanical, and
corrected in this close-out under the maps maintenance law; recorded because the
Stage-0 landing that rewrote the roster is the landing that should have caught
it.

**N-6. The universal manifest writer lives in a module named `open_water.py`.**
`case_section` and `stage_telemac_manifest` - THE writers for reach, do_sag, oil,
sediment and rain-on-grid - are in `steps/open_water.py`, which is otherwise the
parked coastal/wave front's module. The plan named this destination explicitly so
it is conforming; the NAME is now wrong about what it holds, and it will be wrong
in a direction that misleads (a reader looking for the reach manifest writer will
not look in `open_water.py`).

---

## 10. MRE - what can still be removed

Ordered by lines, all reported rather than taken:

| candidate | LOC | why it is removable, and what makes it a judgment |
|---|---|---|
| ~~`postprocess_telemac.py`'s struct parser (N-2)~~ | 113 | TAKEN 2026-09-02: `TelemacFile` behind an in-image driver, the pattern the wave already built for geometry. The 2,681 was the module, not the parser - the rasterizers and the eight per-deliverable postprocessors are arithmetic over the fields and stay. |
| the two dark fronts + their tests + their solver rows (N-3) | 2,078 | rung 4 rebuilds them fresh and never reads them; the attic is not a restoration source either way |
| `artemis_build.py` + `telemac3d_build.py` + `_staged_bed.py` (D-1) | 2,192 | dies at rung 4 by ruling; the wave correctly did not touch it |
| `steps/author.py`'s hand-written keyword emission (N-1) | part of 1,321 | a `TelemacCas` writer is a sheet-to-keywords mapping plus the library |
| `streeter_phelps.py` + its test (N-4) | 92 + test | either wire it back as the do_sag overlay or delete it with its test; a surviving test of a dead function is an anchor |
| `_on_step` (`entrypoint.py:165`) | 8 | a deliberate empty seam. NOT a removal candidate - it is the ruled attachment point for emit-on-solve, live progress and BMI, and the ruling says so. Listed so a future MRE pass meets the reason instead of the emptiness. |

The wave's own additions survive the MRE question: `mesh/shared/selafin_cli.py`
(84), `mesh/topology.py` (152), `mesh/shared/nodes.py` (309),
`steps/run_reads.py` (311) and `run_telemac.py` (188, from 569) are each the one
expression of their job with no second front beside them.

---

## 11. Ledger and LOC

Rows appended to `docs/validation/worker-loc-ledger.md` (Wave D). Headline:

* attic sweep: **-37,324** product, **-11,424** test, 18 directories, mirrored
  with a ledger line each - a MOVE, not a dissolution.
* `workers/telemac/`: **9,172 -> 2,873** product, **1,998 -> 798** test
  (**-6,299** / **-1,200**); `entrypoint.py` 1,594 -> 477.
* whole tree beyond the attic, `.py` only: **-3,823**
  (`workers/telemac` -7,499, `trid3nt_server` +2,047, tests +1,286,
  scripts +329, plugin +14). The plan's -7,500 is met on the worker column and
  missed tree-wide; the difference is the server-side authoring substrate the
  sole-authored-record law requires, not slippage.
* docs: +1,166, of which DELETION_LEDGER +546.

**CODED tools** (hand-written registered tools, never registry totals):
**65 -> 64**, a rolling **-1**. The registry pin moves 170 -> 169
(`tests/test_catalog_surfacing.py:126`) and declarative fetcher specs are
unchanged at 105, so the move is entirely coded: `tomawac_wave_field` and
`coastal_tidal_surge` left the model surface (-2) and `telemac_rain_on_grid`
returned to it (+1). Rolling coded-tool LOC over the wave:
`trid3nt_server/tools/` **+94** (+118 / -24), which is the two new geometry links
and the registration edits, not the templates - the template work is in
`workflows/` (+2,047).

Stale ledger noted, not corrected: `docs/validation/skeleton-loc-ledger.md`
stops at a running `+9,671` on 2026-08-28 and carries no row for the LEGO,
elegance or worker waves, which reported their deltas in `docs/IDEAS.md` instead.
Whether that ledger is retired or resumed is a call for its owner.
