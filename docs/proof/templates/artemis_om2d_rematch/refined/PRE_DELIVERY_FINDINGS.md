# Pre-delivery interrogation - artemis om2d rematch (refined)

Every render in this folder was read against the run before the packet was
called finished. Six checks, each with what was looked at and what it said.
Findings are numbered; the ones marked CARRY are limitations the delivered
packet ships with and a reader has to be told, not defects that were fixed.

Run `01M130X4GS6BTXPV0N0PPVDTHZ` - `artemis_harbor_agitation` over the Point
Judith Harbor of Refuge, solving on the authored mesh
`s3://trid3nt-cache/mesh/01M12YC08NNH4TDZPXEVDDF3AV/mesh.2dm`
(13110 nodes / 25424 elements, EPSG:32619).

---

## 1. Cross-panel coherence - PASS

The bed panel, the Kd panel, the peak frame and the two mesh figures describe
one domain and agree about where everything in it is.

* The bed panel (panel 01, NCEI DEM_all over the AOI) shows the breakwaters
  independently, as the bright shallow ridges its own elevations put them at -
  a V opening north, a detached arm to the north-west, and a third structure
  leaving the AOI to the east.
* The mesh figures punch holes at exactly those three places. The bed was fetched
  by one subsystem and the cut was constrained by another, from OpenStreetMap
  geometry; they were never compared to each other before this render, and they
  land on top of each other.
* The Kd panel's shadow is the V's interior. Its two bright fringe fans start at
  the two tips the mesh figure puts the tips at.
* The canvas view stacks the two published layers in emission order and the bed
  is visible only where the Kd raster does not cover it, which is what a
  two-layer stack should look like.

## 2. Picture vs scalar - PASS with one CARRY

| number | narrated | measured on the delivered picture |
|---|---|---|
| `kd_max` | 3.484 | raster max 3.351 |
| `hs_max_m` | 6.969 | peak frame stamp "max 6.97 m" |
| `npoin` / `nelem` | 13110 / 25424 | mesh figure title, from the artifact |
| `mesh_edge_median_m` | 29.5 | mesh figure colour scale centres there |
| `wavelength_m` | 114.5 | fringe spacing in the peak frame, ~1 wavelength |

**Finding 1 (CARRY).** The Kd panel's legend runs 0 to 2.356 while the run
narrates `kd_max` 3.484. The legend is the style seam's percentile stretch and
the raster genuinely reaches 3.351; the 4% gap between 3.484 and 3.351 is the
node field being rasterized onto a grid. Nothing is wrong, but a reader who
reads the maximum OFF the colour bar will be 32% low. The peak frame, whose
scale note says "scaled to this run (p2-p98)" out loud, is the render that
states this; the Kd panel does not.

**Finding 2 (CARRY).** `kd_sheltered` = 0.585 is a mean over the entire downwave
half-plane, not over the visible shadow. Measured off the delivered raster the
shadow band immediately behind the barrier means 0.564 and the exposed approach
1.074, so the two agree in size and sign - but the deep purple a reader sees in
the lee is 0.1 to 0.3, and 0.585 is not a number they can find in the picture.

## 3. Discrimination - PASS

The run answers its own question with a signal, not a flat field.

* Worker: `kd_sheltered` 0.585 vs `kd_exposed` 0.966, `sheltering_ratio` 0.606.
* Independently, off the published COG with a geometric lee/exposed split:
  0.564 vs 1.074, ratio 0.525. Same sign, same order, different masks.
* The chart is the sharper statement: Kd runs 1.0-1.75 along the exposed
  approach, collapses through the barrier over ~50 m, and settles at 0.3-0.6 in
  the lee. A run with no sheltering could not draw that.
* The mesh figures discriminate too: the cut zoom shows element edges at 8-15 m
  along the barrier against 25-50 m in the basin, which is the adaptive sizing
  the whole exercise is about.

## 4. Declaration conformance - PASS with two CARRIES

* The animation is EXEMPT and the packet says why in the physics' own words:
  ARTEMIS solves a boundary-value problem for one monochromatic sea state and
  has no simulation clock. The SELAFIN was measured at 1 frame, so the exemption
  is measured rather than assumed.
* The still is the declared `WAVE HEIGHT` field at the declared `peak` step.
* Every deliverable carries the run id, both burned into its caption and stamped
  into its PNG text chunk.

**Finding 3 (CARRY).** Panel 01 is titled "Input: TELEMAC open-water bed
elevation at mesh nodes". On this run the solve did NOT read that raster: the
bed it read is the one the supplied mesh carries at its own nodes, sampled from
`fetch_topobathy` (CUDEM 1/9") when the mesh was authored, while the panel is
`fetch_ncei_dem_mosaic` fetched over the AOI for the canvas. The two are the
same NCEI family at different sampling, they agree about where the structures
are, and the run's own provenance rows and honesty note both say the solve read
the mesh's bed - but the LAYER NAME still describes the old path. The template
fetches that layer unconditionally; making it say which of the two the solve
read is a template change this slice did not make.

**Finding 4 (CARRY).** The structure the run was handed does not appear as a
published layer. `fetch_osm_breakwaters` emits its layer when the FETCHER runs
inside a run; this driver fetches it once, up front, to build the mesh obstacle
and then hands the run the resulting uri, so nothing re-emits it. The packet
therefore has 2 layers where the uniform-grid canary has 3. The structure is
visible in the mesh figures instead, drawn in cyan from the same geometry.

## 5. Georeference and framing - PASS with two CARRIES

* The peak frame and both mesh figures place the domain on ESRI World Imagery
  with the Point Judith shoreline entering at the north-east corner, which is
  where GSHHG cuts the mesh. The land in the imagery and the missing corner of
  the mesh are the same corner.
* The frames were drawn over the run's own bbox; the packet's own extent check
  passed (no "drawn at the UTM false origin" entry in `missing`).
* The bed panel is the strongest georeference evidence in the folder: a raster
  fetched independently of the mesh puts the breakwaters exactly where the mesh
  cut them.

**Finding 5 (CARRY).** The AOI CLIPS the harbour. The east breakwater of the
Harbor of Refuge lies entirely outside the domain and the west breakwater
continues north past the mesh's north edge - both visible in the wireframe
figure, where the cyan footprint runs off the mesh on two sides. The modelled
lee is therefore an open-ended basin rather than the real enclosed harbour, and
`kd_sheltered` is the shelter THIS domain provides, not the harbour's.

**Finding 6 (CARRY).** The domain's north, east and west edges are classified
solid ABSORBING (LIHBOR 2, RP 0) because the mesh designates one open boundary
and this run designated the south. Those three edges are open water, so the
model has absorbing walls where the sea continues. Absorbing is the mild choice -
it radiates rather than reflects - but it is not transparent, and a reader
should not read the field within a wavelength of those edges.

## 6. Run vs code freshness - PASS with one CARRY

* `packet.json` records `code_sha be1e9d3d...`, which is the commit this slice
  landed as, so the run and the tree describe the same engine.
* The worker image was rebuilt after the `_supplied_mesh` and `artemis_build`
  edits and the solve ran THROUGH the rebuilt image; the run's own metrics echo
  `mesh_source: supplied`, which only the new code path writes.
* All renders are newer than the evidence JSON they were assembled from (the
  packet's staleness check passed on every row).

**Finding 7 (CARRY).** The staleness line reads `working_tree_dirty`: the tree
is at the run's commit and carries uncommitted changes, which at assembly time
were this folder's own renders. The code is committed; the proof is not, yet.

---

## What could NOT be shown

**The uniform-grid comparison did not run.** `uniform_grid_comparison.json`
records the attempt and its failure. The worker's own grid builder cannot
discretize this AOI at either 25 m or 40 m: ARTEMIS stops with "the number of
lines in the boundary conditions file is greater than the number taken in the
geometry file", which is the grid path's single-ring boundary walk meeting a
domain whose land mask has islands in it. So there is no A/B pair of numbers.

That absence is itself the sharpest thing the flagship measured: on this harbour
the authored route SOLVES and the generated route REFUSES, so the comparison is
not "does adaptive fidelity sharpen the fringes" but "one of these two produces
an answer at all".

## Measured acceptance numbers

| what | measured |
|---|---|
| conformal offset, constrained outline to nearest node | max **21.7 m**, median **0.0 m**, over 931 constrained points |
| open boundary | 126 of 796 boundary nodes, one contiguous section, mean bed -15.2 m, centroid (-71.5130, 41.3430) = the SOUTH edge |
| structure faces | 547 boundary nodes on the cut; 0 contested between structure and liquid |
| bed clamp | 45 of 13110 nodes (0.34%) pinned to -1.0 m: the structure crests and the shoreline fringe |
| element edges | 4.2 / 29.5 / 202.1 m (min / median / max), min angle 10.2 deg |
| degenerate elements repaired at build | 2 |

**The conformal number needs reading carefully.** The MEDIAN offset is 0.0 m -
most constrained vertices are nodes the mesh actually has, which is what the cut
zoom shows. The 21.7 m MAXIMUM comes from where the fetched breakwater ways
overlap: two mapped centrelines a few metres apart buffer into a sliver the
8 m finest edge cannot hold, and DistMesh drops those constrained points rather
than laying a zero-length edge. The acceptance the charter asks for - "nodes
placed ON the polyline, element edges coincident with it" - holds along the
barrier and fails on the sliver, and this is the number that says so.
