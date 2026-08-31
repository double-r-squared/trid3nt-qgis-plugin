# Elegance review: fewer concepts, doing more

rev 1 - 2026-08-29 - PROPOSALS ONLY. This document changes nothing. It is judged
against the recorded rulings (`docs/IDEAS.md` 2026-08-27..29, `docs/specs/square-two.html`,
`docs/specs/gmsh-mesher.html` rev 19), not against taste.

## Thesis

The settled principles have already collapsed most of the vocabulary; what is
left in the tree is not missing abstraction but leftover PARALLEL machinery -
second homes for facts that now have a first one. Four survive: a second
invocation substrate (the DATA door resolves dotted module paths, so "tools all
the way down" stops at the template's own world-reads, and ten bespoke `resolve_*`
shims exist only to reach registered tools the door cannot name); a second mesh
front (`mesh/watershed.py` plus `precondition_gate.py`, complete with its own
delineation, its own DEM/landcover/river resolvers and its own discovery gate,
still carrying `rain_on_grid` while three of seven templates declare meshers the
purge removed); a second membership vocabulary (`ENGINE_MESH_REQUIREMENTS`, now
one row, answering the question `Accepts` was just ruled to answer); and a second
format writer (88 lines of hand-packed SELAFIN bytes beside the telapy pair
writer, which the library-first gate exists to reject). Every proposal below is a
deletion that makes an existing declaration real. The one addition anywhere in
this list is roughly six lines in `interpreter._load`, and it is what unblocks the
LEGO chain without introducing a single new concept - the chain the rulings ask
for is already spelled by `Data` + `Ref` + the plan list, and needs only to be
allowed to say a tool's name. Where the tree contradicts a landed ruling
(`bank_source="constant_ribbon"` is still a declared param whose typed refusal
ADVERTISES the ribbon as a retry) the proposal is execution, not invention.

---

## P1. One runner namespace: let the DATA door name a registered tool

**Asymmetry removed.** `Fetch.tool(name)` / `Build.tool(name)` are spelled `tool`,
but `interpreter._load` requires a dotted import path and refuses a bare name
("runner ... is not a dotted import path"). All 13 producers in the tree are
therefore dotted paths into workflow modules, and every world-read a template
wants needs a bespoke function whose whole body is `TOOL_REGISTRY["fetch_dem"].fn(...)`
plus prose. Two invocation substrates wearing one word - and the LEGO ruling's
chain (`delineate -> build_mesh(polygon)`) cannot be declared at all today without
authoring an eleventh shim.

**Sketch.**

```python
DATA = (
    Data("bed_dem", tool("fetch_dem", source="3dep",
                         resolution_m=P.bed_dem_resolution_m)
         .ladder("usgs_3dep_bare_earth", "copernicus_glo30")),
    Data("basin", tool("delineate_watershed", dem=D.bed_dem,
                       pour_point=P.pour_point)),
)
MESH = tool.build_mesh(mesher="om2d", kind="unstructured_tri", extent=Ref("basin"))
```

**Deletes.** `_load` gains a registry lookup before the dotted fallback and a
refusal that names both namespaces on a miss (collision refuses rather than
picks). As each shim is replaced by the registry call it was hiding, the shim
goes: `watershed.resolve_bed_dem` / `resolve_landcover` / `resolve_river_network`
(~110 LOC) first, the remaining seven as their templates rebuild. Shims that
genuinely COMPOSE (multi-fetch plus a merge) stay - they are steps, not aliases.

**Enhanced behavior.** The chained-tools rung becomes expressible in the
declaration language that already exists; `!run delineate_watershed` and
`Data("basin", tool("delineate_watershed"))` become the same call, which is
what "tools all the way down" claims. Existing producer tests cover the door.

**Cost.** ~6 LOC added, ~110 removed on the first pass. One real risk: a name that
is both a registry key and a module attribute - refuse loudly instead of choosing.

---

## P2. Rebuild reach and catchment as chains; retire the second mesh front

**Asymmetry removed.** `tool.build_mesh` validates its mesher at declaration time
(`validate_spec` -> `get_mesher`), and the roster is `om2d` + `reg_grid`. Yet
`rain_on_grid` declares `mesher="watershed"` and `river_dye` / `do_sag` declare
`mesher="corridor_tin"`: three MESH blocks naming producers that no longer exist,
because in those three templates the block never reaches the tool - it is read by
`Catchment.mesh` / `ReachMesh.corridor`, which call the old front directly. That
is exactly the deck-keyword carrier D-1 ruled out. Behind them sit
`mesh/watershed.py` (783 LOC: its own delineation, its own generate/adopt pair,
its own DEM/landcover/river resolvers, retained by three other modules for a
ten-line `utm_epsg_for`) and `precondition_gate.py` (222 LOC, one live consumer -
`steps/rain_on_grid.py`), a second discovery gate whose documented AUTO behavior
adopts a discovered mesh silently, which D-9 forbids.

**Sketch.**

```python
# catchment
Data("basin", tool("delineate_watershed", dem=D.bed_dem, pour_point=P.pour_point)),
MESH = tool.build_mesh(mesher="om2d", kind="unstructured_tri", extent=Ref("basin"), ...)

# reach
Data("water", tool("fetch_nhd_area_water")),
Data("reach", tool("section", source=D.water, between=[P.upstream, P.downstream])),
MESH = tool.build_mesh(mesher="om2d", kind="unstructured_tri", extent=Ref("reach"), ...)
```

**Deletes.** `mesh/watershed.py` (783, `utm_epsg_for` moves to the one place a
UTM zone is computed), `precondition_gate.py` (222), `ReachMesh` +
`build_corridor_mesh` and the corridor half of `steps/reach.py`, the watershed
Data shims, the `Catchment.mesh` step. Order of 1200 LOC, two whole concepts (a
parallel mesh front, a parallel discovery gate).

**Enhanced behavior.** The three templates gain what the mesh wave built and they
never got: sessions, editability, the gate, discovery by membership, recipe-as-
record, measured `mesh_size_m` for DS-3. `om2d` already accepts a polygon extent
and builds its SDF from it (`_domain`), so the mesher side is done.

**Cost.** This IS ladder rung 1 and is in flight; the proposal is its SHAPE - the
chain declared through P1 rather than through new step code, and no new
`domain=`-style vocabulary. Depends on P3 for an honest reach polygon.

---

## P3. Execute the ribbon ruling: delete `bank_source`

**Asymmetry removed.** The 2026-08-29 ribbon ruling is absolute - a buffered
ribbon is never a mesh domain, no fallback rung, ever - and the tree still carries
the rung as a first-class declared concept: `Param("bank_source", default="nhd_area")`
in two templates with a `constant_ribbon` member, `normalize_bank_source`
coercing a two-word vocabulary, an alias row (`banks -> bank_source`), a review-
sheet entry, two manifest fields, and `TelemacBanksUnavailableError`, whose
message and `suggestions` ADVERTISE `bank_source="constant_ribbon"` as the retry.
A refusal that offers the banned thing is worse than no refusal.

**Sketch.**

```python
raise ReachBanksUnmapped(          # terminal, not retryable
    "No mapped water polygon covers this reach. Draw the reach polygon, "
    "name a case layer that holds it, or pick a reach with NHDArea coverage.")
```

**Deletes.** 2 Params, `normalize_bank_source` + its vocabulary table, the alias
row, the review entry, the two manifest fields, the retry suggestions - about 90
LOC and one concept. `channel_width_m` survives only while the node ESTIMATE
needs it, and DS-3 already prefers the measured artifact, so it goes with the
estimate.

**Enhanced behavior.** The reach refusal names the three real supply paths the
ruling named, and bank provenance (real vs user-supplied) is the only bank fact
left travelling.

**Cost.** Pure removal. Reach tests that assert the ribbon retry are deleted with
their subject.

---

## P4. One membership test at the supply door

**Asymmetry removed.** After the ACCEPTS ruling two vocabularies answer "can this
mesh be used here": the template's `Accepts(mesh=("unstructured_tri",))`, read off
the registry by `accepts_for`, and `ENGINE_MESH_REQUIREMENTS` - now a single
`telemac` row whose body reduces to "the artifact carries `slf_uri` and a bed".
That is not a contract, it is a readiness property of the artifact, and it drags
an `engine=` argument through `resolve_mesh`, `supplied_mesh_artifact`, the gate,
the session and the precondition gate, plus an `engine_compat` field on
`MeshArtifact` that is written and read only by itself.

**Sketch.**

```python
art = resolve_mesh(explicit, tool_name=ops.name, ...)   # kind membership via accepts_for
missing = art.unsolvable_reason()                        # "carries no SELAFIN geometry"
```

**Deletes.** `ENGINE_MESH_REQUIREMENTS`, `mesh_compatible_with_engine`,
`MeshArtifact.engine_compat` and its plumbing, the `engine=` thread - about 80
LOC and one vocabulary; the readiness check is ~10 lines on the artifact that
owns the facts.

**Enhanced behavior.** Refusals speak the template's own words ("river_dye accepts
unstructured_tri; got structured_grid") instead of an engine table's, which is
what the declared-input-contracts ruling asked for. Best landed WITH the
door-wiring stop the ACCEPTS ruling already opened, so the door is written once.

**Cost.** Sequencing only - do not land it before the `accepts_for` door is wired,
or the tree is briefly without any check.

---

## P5. Declared ladders execute, or `.ladder()` goes

**Asymmetry removed.** `.ladder("usgs_3dep_bare_earth", "copernicus_glo30")` is a
declaration that implies a mechanism it does not have: the interpreter only does
`kwargs.setdefault("fallback", rungs)`, and the real ladder is a hand-written
try/except inside `resolve_bed_dem` that echoes the rung names back into prose.
Two homes for one fact, and the declared home is the inert one - so `landcover`
and `rivers`, which declare no ladder and implement none, are indistinguishable
from a producer whose ladder simply failed to fire.

**Sketch.**

```python
Data("bed_dem", tool("fetch_dem", source="3dep")
     .ladder(tool("fetch_copernicus_dem")))           # rungs are producers
```

The producer machinery walks the rungs, records which one answered, and generates
the loud cross-dataset note ONCE, in the one place that knows a rung changed
dataset.

**Deletes.** The hand-rolled ladder in `resolve_bed_dem` and the `fallback=`
kwarg convention every shim must honor. If the mechanism is not wanted,
`.ladder()` should be deleted instead - an inert declaration is the worse of the
two outcomes.

**Enhanced behavior.** The fallback norm (primary -> fallback -> typed error,
cross-dataset LOUD) becomes structural rather than per-shim discipline; it applies
to producers that today degrade silently.

**Cost.** Two declared consumers exist today (`bed_dem`, `rain`). Land it with P1,
where the rungs become nameable registry calls.

---

## P6. One SELAFIN writer

**Asymmetry removed.** `mesh/telemac_build.write_bottom_selafin` packs SELAFIN
bytes by hand with `struct` (88 LOC, host-side, geometry only) while
`mesh/shared/selafin_cli.write_telemac_pair` writes the geometry AND its `.cli`
through telapy/pretel inside the image. Square Two's standing gate is "reject
reimplemented library math/IO"; this is the clearest instance of it in the tree,
and the hand writer produces a geometry whose boundary numbering is asserted
elsewhere rather than measured.

**Sketch.** `MeshSession._selafin` and the rain-on-grid staging step both call
`write_telemac_pair(rundir, x=..., y=..., cells=..., bed=...)`.

**Deletes.** `mesh/telemac_build.py` (88) and the half of `tests/test_mesh_watershed.py`
that covers it.

**Enhanced behavior.** Every mesh that reaches a solve carries a `.cli` numbered
from its own IPOBO, with the driver's measured stats, instead of two paths where
one measures and one asserts.

**Cost.** Honest one: the two host-side call sites gain a container hop (seconds).
If a host-side fast path is ever needed it should come from telapy on the host,
not from a byte layout we maintain.

---

## P7. One spatial word on `build_mesh`

**Asymmetry removed.** `extent` and `domain` are two words for "what the mesh is
cut from". After P2 nothing declares `domain` (its only declarers were the
corridor templates), and `tool.py` still branches on both.

**Sketch.** `extent` accepts a bbox, a polygon layer uri, or inline GeoJSON -
which `om2d._domain` already implements.

**Deletes.** The word, the `"extent" not in declared and "domain" not in declared`
branch, and one entry from the vocabulary a template author must learn.

**Cost.** Trivial; strictly a follow-on to P2.

---

## DO NOT

- **Factor the repeated five-line `reg_grid` MESH block into a shared constant.**
  Four templates repeat `mesher="reg_grid", kind=..., extent=Ref("aoi"), resolution_m=...`.
  Ruled (2026-08-28, AOI shape): `extent=Ref("aoi")` written in every MESH
  declaration IS the visibility. Deduping the words costs the reading; the
  duplication is the feature.
- **Fold `Data(...).supplied(geometry="mesh")` into the `Accepts` row.** They
  answer different questions - what shape a user may DRAW or hand in, versus which
  kinds the pipeline was TESTED against - and folding them needs a kind ->
  geometry-class map, a new concept bought with ~20 LOC of deletion. Net loss.
- **Fold `Accepts` back into `Param` metadata.** Contradicts the 2026-08-29
  ruling: the accept-set is its own standalone declaration beside PARAMS, and its
  rows are supply ROLES, not params.
- **Introduce a `Chain` / pipeline object for tool chaining.** Speculative
  generality: the plan list plus `Data` + `Ref` already sequences and names every
  hop, and P1 is what makes tools nameable there. A chain type would be a fifth
  spelling of the plan with no consumer.
- **Give meshers a domain-prep hook or plugin base class.** The LEGO ruling is
  explicit: meshers never grow domain preps; a capability gap is answered by a
  composable tool in the chain.
- **Infer the mesher from `kind`.** `structured_grid -> reg_grid` is tempting and
  ruled out: the mesher is an EXPLICIT, deliberate choice (spec section 4).
- **Revive `corridor_of` / an `approximate_reach` fallback rung for release-point
  snapping.** Atticked before birth (2026-08-29): release validity is containment
  in the real domain polygon, snap is the nearest point on the real flowline.
- **Generalize `Accepts` to arbitrary roles with per-role validators now.** Only
  `mesh` and `release` have tested pipelines; the ruling says rows land WITH their
  tests. `banks` arrives with the geometry-by-name seam, not before.
