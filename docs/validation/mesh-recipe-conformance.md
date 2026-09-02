# The mesh-recipe spec, walked clause by clause

Fresh-eyes conformance walk of `docs/specs/mesh-recipe.html` rev 2 (FROZEN) at
the rung-3 wave's close, 2026-09-01. Every row of the spec's section 10
acceptance table was MEASURED LIVE at this close on the live substrate (MinIO
:9000, local docker, `venvs/agent`), not carried over from a stage report.
Evidence lives in the session scratchpad; the numbers are reproduced here so the
table reads without it.

Deviations are REPORTED, never fixed. They are listed after the table.

**VERDICT: BROKEN.** Nine of the ten acceptance rows CONFORM. The end-to-end
template row does not: the reach family cannot author a deck, because the
consumer of the chopped `fit_downstream_bed` survived its producer (D-1).

## The two folds ruled before this walk

`docs/IDEAS.md` 2026-09-01 "FRAGILITY-STAGE JUDGMENTS RULED", landed here:

| fold | landed | measured |
|---|---|---|
| (a) `set_rim_size` joins om2d's VISIBLE DEFAULT ops list | `meshers/om2d.py::_DEFAULT_OPS` now opens with `mesh_op("set_rim_size")`; declared recipes still replace wholesale and are unchanged | an UNDECLARED om2d ask (Point Judith, 60 m) came back with the rim at 61.14-61.28 m against a 60 m ask - `over_ask_max` 1.02. Before the fold the same undeclared ask had no rim op at all |
| (b) the rim tolerance becomes `set_rim_size(tolerance=2.0)` | a visible kwarg with a labeled default on the driver's own `def`; `_RIM_TOLERANCE` deleted; `_Build.rim_tolerance` carries what the op declared | the probe now reads `"tolerance": 2.0` from the ASK rather than from a module constant, on every build above |

`docs/specs/gmsh-mesher.html` (FROZEN rev 19) got a DATED AMENDMENT NOTE
appended under the primer list - the "oceanmesh - iterative, measured
nondeterministic" bullet at line 44 stands as written, and the note beneath it
names the root cause (`medial_axis`'s per-process generator) and the fix
(`om2d_driver._seed_library_randomness`). Nothing in the frozen spec was
rewritten.

## Section 10, the acceptance table

### 1. The four fragilities - each fixed and RE-MEASURED

| fragility | measured live | verdict |
|---|---|---|
| deterministic contour selection on holed domains | Point Judith Harbor of Refuge (`-71.525 41.338 -71.492 41.368`), a GSHHG shoreline-cut (holed) domain, 60 m, recipe `feature_sizing_function -> set_rim_size -> enforce_mesh_gradation(0.2) -> delete_boundary_faces -> delete_faces_connected_to_one_face -> laplacian2 -> make_mesh_boundaries_traversable -> fix_mesh -> set_bed -> identify_ocean_boundary_sections`. Two builds from one recipe: 711 nodes / 1,199 elements BOTH times, `mesh.2dm` sha256 `c5d744062bcd794d...` IDENTICAL. The op that used to drift (`feature_sizing_function`) is IN this recipe | CONFORMS |
| multi-section open boundaries | the same build: `open_boundary_sections` = 2, `role_sections` = `{"open": 2}`, 133 open nodes across two sections, section centroids `(-71.49591, 41.339032)` and `(-71.519111, 41.343778)` - the harbour's two mouths, each its own section, neither dropped | CONFORMS |
| rim sizing honored within declared tolerance | shoreline domain, DEFAULT recipe, ask 60 m: min 61.14 / median 61.14 / max 61.28 m, `over_ask_max` 1.02, `tolerance` 2.0, `within_tolerance` true. Polygon domain (Marquette), ask 60 m: min 60.04 / median 60.41 / max 78.02 m, `over_ask_max` 1.3, `within_tolerance` true. Both rims MEASURED on "the boundary edges on the outline the rim op sized" | CONFORMS |
| a lake-capable domain (Marquette BUILDS) | `scripts/drive_lake_domain_mesh.py`, Marquette Lower Harbor, Lake Superior, 60 m. The BBOX domain refuses first, typed and honestly - `MESH_SHORELINE_DOES_NOT_DESCRIBE_EXTENT`, escalation `fetch_nhd_waterbodies` with the bbox - and the LEGO chain then builds from the fetched water body's own polygon: 827 nodes / 1,502 elements, EPSG:32616, bed painted, 2 boundary loops. No lake logic in the adapter | CONFORMS |

### 2. Recipe determinism - proven twice

Same recipe + same staged inputs -> identical mesh, run back to back through
`TOOL_REGISTRY["build_mesh"].fn`: mesh ids `01M1GCZ2BSDYHFY9QSZG1K6S3N` and
`01M1GCZ97D0NHC8M7YA0WPCKYN`, 711 nodes / 1,199 elements each, identical
`mesh.2dm` bytes. `om2d` registers `deterministic=True` and the journal's first
line carries NO determinism caveat, which is the honest reading. **CONFORMS.**

### 3. mesh_op live

A gate session opened over an om2d recipe (Point Judith, 200 m, ops
`set_rim_size, delete_boundary_faces, fix_mesh`), then edited at RUNTIME through
`TOOL_REGISTRY["mesh_op"].fn`:

| clause | measured | verdict |
|---|---|---|
| appends + regens + re-presents | each call returned the presentation payload (layer, probes, RENUMBERED ops) and the mesh went 225 nodes / 384 elements -> 876 / 1,669 -> 1,159 / 2,225 | CONFORMS |
| unknown fn refuses with nearest names | `mesh_op(fn="feature_sizing_funtcion")` -> `MESH_OP_UNKNOWN`, "did you mean 'feature_sizing_function', 'distance_sizing_function', 'wavelength_sizing_function'?", then the whole 24-name combined namespace | CONFORMS |
| TWO distance-sizing entries on two lines measurably refine BOTH corridors | two `distance_sizing_from_line_function` entries, corridor A and corridor B, `rate=0.05`, `min_edge_length=50`. Mean element edge inside a 300 m band: corridor A **248.16 -> 72.71 m**, corridor B **249.75 -> 71.78 m**. After only A had been appended, B still stood at 144.22 m - so the second entry did the second corridor's work and the first entry did not | CONFORMS |
| order + duplicates work | both entries survive in the numbered recipe as entries 3 and 4, same function name twice, each with its own `line_file` | CONFORMS |

Render interrogated before reporting (`mesh_op_two_corridors.png`, wireframe on,
ONE shared colour scale across both panels): domain outline, extent and CRS
identical panel to panel; the pale bands run through both drawn corridors in the
right panel and through neither in the left; EPSG:32619 eastings 289-291.5 km
place it on the Rhode Island coast. HONEST CAVEAT, stated rather than cropped
out: at `rate=0.05` the library's own growth law reaches ~200 m only after ~3 km
and the domain is ~2.5 km across, so the refinement in the picture spreads well
past the two corridors. The picture shows THAT the corridors were refined; the
banded measurement above is what isolates them.

### 4. Correct data class

| clause | measured | verdict |
|---|---|---|
| `set_bed` paints from topobathy on a covered domain | Point Judith, `set_bed(source="fetch_topobathy")` -> bed painted, `bed_source` = `"fetch_topobathy: cudem_nearshore 100%"` - the row, the rung that served, and its measured coverage | CONFORMS |
| an uncovered domain's topobathy row refuses honestly | `fetch_topobathy` over the Marquette bbox -> `TOPOBATHY_COVERAGE_GAP`: "NO NOAA NCEI CUDEM 1/9\" nearshore tile intersects AOI ... 0% of the AOI has a nearshore bathymetry source. Filling it from the 3DEP land DEM would paint flat 0 m ocean -- a fake landmass a wave/surge solver excludes as dry ground, so this fetch refuses instead", naming the `etopo_bathy_base` rung the caller may PERMIT. No silent DEM proxy, no silent coarser relief | CONFORMS |
| the explicit DEM substitution runs AND the journal names the source row | `set_bed(source="fetch_dem")` on the covered domain builds and accepts. `mesh_recipe.jsonl` carries `{"ops": [{"op": "set_bed", "source": "fetch_dem"}]}`; the accepted artifact's provenance carries `bed_source` = `"fetch_dem (source UNMEASURED: the fetch reported no activation rows)"`. The substitution is the author's visible declared choice and the record names the row | CONFORMS (see D-4 on WHERE the measured rung lands) |
| the op carries no class-policing branch | `shared/primitives.py::_bed_raster` permits NO ladder rung of its own ("which substitutions a bed tolerates is the DATA row's declaration") | CONFORMS |

Companion NO-NAME-DRIFT law: `banks` -> `water` landed (`water = tool("fetch_nhd_area_water", ...)` in `river_dye` and `do_sag`; zero `banks`-named data rows anywhere in the product - the remaining prose hits describe a river's two banks, which is what the word means); `measure_bank_coverage` -> `measure_water_coverage` landed, zero live spellings of the old name.

### 5. Generality

| clause | measured | verdict |
|---|---|---|
| reg_grid conforms to the same surface | `build_recipe(mesher="reg_grid", kind="structured_grid", extent=..., resolution_m=250.0, ops=[mesh_op("set_bed", source=...)])` builds, beds and accepts through the SAME `MeshSession` - three agnostic params and an ops list, no reg_grid-shaped branch anywhere on the path. `reg_grid` registers `default_ops=()`, the near-empty default recipe the spec calls for | CONFORMS |
| ONE card path, zero per-mesher card code | `grep -n "om2d\|reg_grid\|oceanmesh" trid3nt_server/workflows/mesh/gate.py` -> **ZERO hits**. `_mesh_param_sheet` built live for both meshers returns the same row shape: `resolution_m` (user lever) + one read-only `op[i]` row per recipe entry + `reset`, differing only in the recipe's own content | CONFORMS |

### 6. Dissolution

| dies | live spellings | verdict |
|---|---|---|
| `MeshField` | 0 | CONFORMS |
| per-mesher `_FIELDS` | 0 under `workflows/mesh/` | CONFORMS |
| `refine={}` vocabulary | 0 (`grep "refine="` over the product, plugin, tests, scripts) | CONFORMS |
| `bed=` / `boundaries=` as build_mesh params | 0 (the one `bed=` hit is the `Mesh` dataclass field, not a param of the generalization) | CONFORMS |
| `DeclaredEdit` | 0 | CONFORMS |
| `fit_downstream_bed` | 0 in product code; one guard test asserting its ABSENCE from the namespace; ledger row present with its condition | CONFORMS as a grep - **but see D-1: the CONSUMER survived** |
| the snapshot cache | 0 live spellings. MEASURED then decided, in the ledger: wholesale regen of an already-staged recipe, 5 repeats - coarse reach canary (om2d, 12 m, 6 ops, 1,933 nodes) median 3.5 s; basin canary (om2d, 40 m, 8 ops incl. distance sizing + gradation, 6,079 nodes) median 7.4 s. Cheap as ruled; closed as a CANDIDATE that was never built | CONFORMS |
| recipe-vs-declaration duality | one object; the declaration is a recipe literal (`tool.build_mesh(...)` in the five templates) | CONFORMS |
| renames landed | `water`, `measure_water_coverage` - see section 4 | CONFORMS |

### 7. Library-first grep

| clause | measured | verdict |
|---|---|---|
| every op call reaches its real callable verbatim | `om2d_driver._resolve`: our primitives by their real `def` names out of `_PRIMITIVES`, everything else `getattr(om, name)` with a refusal naming the installed version. No alias table, no translation layer | CONFORMS |
| the signature is the schema | `inspect.signature(...)` binds both origins - `meshers/__init__.py:364` for host-side primitives, `om2d_driver.py:539` in the container against the library's real signature; a stated kwarg is used as written, an unstated one comes from the staged domain, a required-and-absent one is refused BY NAME | CONFORMS |
| no reimplemented sizing/clean math | no `def` in `workflows/mesh/` names a triangulator, a gradation or a sizing function. Our three om2d primitives WRITE ONTO the library's own `Grid` (`grid.create_grid()`, `grid.build_interpolant()`) rather than computing a lattice of their own; `_clean_once` is the file-forced fusions only (single-precision SELAFIN coincidence, zero-area elements), each one reported rather than absorbed | CONFORMS |

### 8. Suite + conformance

Five slices from the repo root with `venvs/agent`, globs unquoted, `env -u
TRID3NT_CACHE_BUCKET`:

| slice | passed | skipped | failed | vs the 0323 baseline table |
|---|---|---|---|---|
| `test_[a-e]*` | 1698 | 5 | **0** | -17 |
| `test_[f-o]*` | 4181 | 0 (1 xfailed) | **0** | +46 |
| `test_[p-r]*` | 1880 | 1 | **0** | +1 |
| `test_[s-z]*` | 1662 | 6 | **0** | +267 |
| `contracts/tests` | 521 | 0 | **0** | 0 |

**9,421 + 521 passed, ZERO failed in every slice** - the standing bar, met. The
denominator moved with the wave (tests died with their subjects and new ones
landed across the seven rung-3 commits); this walk adds +2 to `[f-o]` for fold
(b)'s visible-kwarg guard and fold (a)'s default-rim guard.

### 9. ONE coarse end-to-end template run

`telemac_river_dye`, Eel River near Scotia CA, discharge pinned at 60 m3/s,
through the declared recipe surface (`tool.build_mesh(mesher="om2d", ...)` with
`set_bed(source=DATA.dem)` and `set_boundary_roles`).

**status=error.** Run `01M1GDBBERYZ3ZCWRKHATMZ7TT`. `reach`, `seed`,
`carrier_discharge`, `mesh` and `measure_mesh_coverage` all completed - the MESH
BUILT through the new surface - and the run then failed at `deck` with
`TELEMAC_MESH_BED_UNFITTED`. See D-1. **DOES NOT CONFORM.**

No packet was assembled: `docs/proof/` is frozen for this wave and there is no
successful run to assemble one from. The two mesh renders that WERE produced
(the two-corridor pair above, the lake domain) are in the scratchpad, wireframe
on, and were interrogated before this document was written.

## Deviations - REPORTED, NOT FIXED

> **RESOLVED 2026-09-02** - `docs/IDEAS.md` "RUNG-3 CLOSE RESOLUTIONS". D-1, D-3,
> D-4 and D-6 are ruled and landed: the deck's outflow stage is the MEASURED bed
> at the accepted mesh's declared boundary roles, the stale prose carries a dated
> correction, the measured substitution rung joins the JSONL journal beside the
> artifact provenance, and the rim-note conditioning is ratified. D-2's errata
> (`line` -> `line_file`) is applied to the spec; D-5 stands as written. The
> findings below are the record as MEASURED at the 2026-09-01 close and are not
> rewritten.

### D-1 (BLOCKING) - the chopped `fit_downstream_bed` left its consumer standing

`trid3nt_server/workflows/telemac/steps/deck.py:187 _fitted_bed` reads
`probes["bed_fit"]` and refuses `TELEMAC_MESH_BED_UNFITTED` when it is absent.
`fit_downstream_bed` was the ONLY writer of `bed_fit` and it was DELETED this
wave. `grep -rn "bed_fit"` over the product returns exactly ONE hit - that
reader. Every other hit is a test that FABRICATES the key in its mesh record
(`tests/test_run_river_dye_scenario.py:374`,
`tests/test_telemac_reach_mesh_session.py:59,63,294`), which is why the offline
suite is green while the live template is dead.

Blast radius: `deck.py:703` passes `bed=_fitted_bed(mesh)` into
`author.author_reach_deck`, the shared reach deck author - so `river_dye`,
`do_sag` and every reach-family template that authors a deck refuse before the
solver is ever reached. Measured live for `river_dye`; established by code path
for the rest.

The `DELETION_LEDGER` row for `fit_downstream_bed` states a PHYSICS consequence
("the reach canaries now carry the raw sampled surface, which runs uphill
between adjacent nodes and ponds"). It does not state this one: the deck's
outflow stage has no ground to be measured from at all. The spec's section 8
claim - "its condition met by design" - is not met while the consumer stands.

Not fixed here: what the outflow stage should read once the bed is honest
topobathy is a DESIGN question, and the chartered bathymetry item is where it
belongs.

### D-2 - the spec's own example names a kwarg the library does not have

Spec sections 2 and 3 write
`mesh_op("distance_sizing_from_line_function", line=DATA.centerline, rate=0.05)`.
oceanmesh's real signature is
`distance_sizing_from_line_function(line_file, bbox, min_edge_length, rate=0.15, max_edge_length=None, coarsen=1, crs='EPSG:4326')`.
Under the spec's own section 4 ("the kwargs bind to the real callable's
signature") the spec's example would be REFUSED. The code is right and the
example is wrong; the live measurement in section 3 above used `line_file`.

### D-3 - stale prose still describes `fit_downstream_bed` as live

`docs/design/worker-unification-port.md:158,268` describes the fitted bed as
present. (`docs/validation/worker-unification-conformance.md` also names it, but
that is a DATED conformance record and correctly stands as written.) A doc-wave
item, not a code defect.

### D-4 - where the MEASURED bed source lands

Spec section 6's lifecycle diagram lists "bed source row" among what the journal
carries. Measured: the `mesh_recipe.jsonl` journal names the source ROW (the
recipe's `set_bed(source=...)`), and the MEASURED rung that actually served
(`"fetch_topobathy: cudem_nearshore 100%"`) rides on the accepted artifact's
`provenance.bed_source` instead. Conformant on a literal read of "names the
source row"; reported so the placement is NATE's to confirm rather than mine to
assume.

### D-5 - consequence of fold (b), stated

With `_RIM_TOLERANCE` gone, the band is a property of the ASK. A build whose
recipe declares no rim op therefore reports the rim's measured spread with NO
`tolerance` and NO `within_tolerance` verdict - an unsized rim declared no band
to be held to. Every om2d build that takes the default recipe now HAS a rim op,
so this only surfaces on a declared recipe that leaves `set_rim_size` out.

### D-6 - one judgment made inside fold (a), flagged for a ruling

`set_rim_size` appends a note when it locks a rim with no sizing lattice built
yet ("the elements behind the rim are not graded into it"). With the op now in
the DEFAULT list and no sizing op beside it, that note would fire on every
undeclared om2d ask - where it is misleading, because with no lattice the whole
domain is uniform at exactly the rim's own edge and there is no step to grade.
The note is now conditioned on the rim ask DIFFERING from the size word. This
was not ruled; it is surfaced here rather than buried.

## LOC, product `.py` only, rolling

| date | wave | surface | before | after | delta | running |
|---|---|---|---|---|---|---|
Filed in full in `docs/validation/skeleton-loc-ledger.md`.

| date | wave | surface | before | after | delta | running |
|---|---|---|---|---|---|---|
| 2026-09-01 | rung-3 stage commits (7) | the whole product tree | 141,031 | 141,519 | +488 | +10,159 |
| 2026-09-01 | close, fold (a) | `mesh/meshers/om2d.py` | 706 | 711 | +5 | +10,164 |
| 2026-09-01 | close, fold (b) | `mesh/meshers/drivers/om2d_driver.py` | 998 | 1,005 | +7 | +10,171 |

The generalization GREW the tree, +500 net over the wave: it absorbed a
per-mesher field table, a named-action edit chain, a spec-vs-declaration duality
and a bed-fitting shim, and paid for them with recipe machinery, a
container-side op interpreter binding against real library signatures, and
two-origin namespaces. What it bought is not lines - a new mesher is three
registrations and the gate grows no card code.

NOT COUNTED by the rule at the head of the ledger: `tests/test_mesh_om2d.py`
+22 (2 tests: the default rim op carries no invented number, and the band is a
visible kwarg with `_RIM_TOLERANCE` grepped to zero; plus the default-recipe
test following its subject).
