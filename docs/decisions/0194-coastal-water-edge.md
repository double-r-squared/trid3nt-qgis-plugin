# ADR 0194 - coastal water-edge re-mesh (OSM coastline; CUSP verdict)

Status: Accepted (Delaware Bay + Tampa Bay re-meshed and verified as standalone
sandbox cases; pipeline placement stays NATE's per ADR 0192)
Date: 2026-08-08
Extends: ADR 0192 (OceanMesh2D standalone), ADR 0193 (custom-SDF meshing) - this
lands the estuary water-edge re-mesh ADR 0193 Section 7.2 left as the remaining
live step.

## Context

NATE flagged the v1 coastal meshes (Delaware Bay, Tampa Bay): the GSHHG-
intermediate shoreline is too coarse -- the meshed water edge is "close but not
really" aligned to the real shoreline/river in the AOI. ADR 0193 proved the
watershed-first custom-SDF method inland (Coweeta) but honestly deferred the
estuary re-mesh with a real finding: NHDPlus HR *waterbody* polygons do NOT
contain the open bay (open estuary water is NHDArea/SeaOcean, not NHDWaterbody),
so the open-water edge needs a coastline source. The directive: build a high-res
coastal water-edge (NOAA CUSP if it exposes a queryable vector layer, else OSM
`natural=coastline`), union in NHDArea/estuary features where they add river
arms, and re-mesh both bays full-domain so the mesh is not cut off at the AOI box.

## Decision

Land a high-res coastal WATER polygon builder and re-mesh Delaware Bay + Tampa
Bay against it, all in the standalone sandbox (nothing registered/wired).

**Water-edge source.** OSM `natural=coastline` (Overpass, 3-mirror etiquette
reused from the repo's fetchers) is the landed high-res source, UNIONed with
connected NHDPlus HR NHDArea (open-water areal features) + NHDWaterbody polygons
that add river arms, clipped to the domain. NOAA CUSP was evaluated live and is
**download-only**: the canonical NGS Continually Updated Shoreline Product has no
public queryable ArcGIS vector layer (`gis.charttools.noaa.gov/.../MCS/CUSP/
MapServer` returns a service-absent error; CUSP ships as shapefiles via the NOAA
Shoreline Data Explorer). The reachable NOAA ENC-derived shoreline is S-57
nautical geometry with no clear fidelity gain over OSM. The CUSP probe result is
recorded in each run's provenance.

**Coastline -> water polygon.** `polygonize(domain box + coastline ways)` splits
the domain into faces; each face is classified LAND/WATER by the OSM direction
convention (water lies to the RIGHT of a way), located via an STRtree of the
face set; islands survive as holes. WATER = union of the water faces.

**Meshing.** A custom signed-distance function over the exact water polygon
(negative inside; island holes and multi-part water native), with feature
(distance-to-shore) + wavelength (depth) sizing, handed to OceanMesh2D
`generate_mesh` in the GPL-isolated `mesh:latest` image via a mounted (not baked)
`_mesh_water_edge_incontainer.py`. The coastal `om.Shoreline` path is deliberately
bypassed: it models polygons as exterior-ring-only coord arrays (drops island
holes; a "box minus water" land polygon collapses to no mesh) AND Chaikin-smooths
the shoreline, both of which move the meshed edge OFF the imagery -- the opposite
of the alignment goal. This is the ADR 0193 custom-SDF pattern with coastal sizing.

**Domain.** The whole bay (land inland, a straight offshore open boundary
seaward) -- NOT the tight v1 AOI box, which is drawn only as a residual overlay
and demonstrably does not truncate the mesh.

## Consequence

- New sandbox files only: `scripts/sandbox/oceanmesh/build_coastal_water_edge_mesh.py`,
  `_mesh_water_edge_incontainer.py`; `water_edge.py` gains the OSM-coastline +
  NHD water-polygon builder + the CUSP probe (its dead `write_land_from_water`
  land-shapefile scaffold was removed -- the custom-SDF path supersedes it).
- The v1 coastal path (`_mesh_incontainer.py`, `build_coastal_mesh.py`) is
  UNCHANGED; duck_nc + puget_sound stay GSHHG. No image rebuild (in-container
  script mounted, not baked). No workflow / tool / category / board / worker touched.
- Re-meshed, overwriting the v1 files in `docs/proof/templates/oceanmesh_meshes/`:
  - Delaware Bay: 5308 nodes / 9422 elems; 107-4292 m (median 420 m); min qE 0.52
    median 0.95; 0 inverted; closed (6 loops); water 3953 km^2.
  - Tampa Bay: 10797 nodes / 18726 elems; 84-3043 m (median 273 m); min qE 0.43
    median 0.96; 0 inverted; closed (18 loops); water 2262 km^2 (incl. NHD arms).
  Both MDAL- (QgsMeshLayer) and SERAFIN- (telemac TelemacFile) verified; four
  formats (.2dm/.slf/_hgrid.gr3/.fort.14) emitted.
- Alignment proof: `oceanmesh_standalone_{delaware_bay,tampa_bay}.png` overwritten
  + `_closeup` companions showing the mesh edge tracking the imagery shoreline
  (Delaware NJ tidal creeks / bay mouth; Tampa Gulf barrier islands + passes +
  river arms). Bulk data stays in the gitignored `_runs/` + `shoreline/` dirs.

## Open questions

- NHD NHDArea (hydro.nationalmap.gov) intermittently 500s; the builder degrades
  to OSM-coastline-only (Delaware was coastline-only on one run). A retry/backoff
  around the NHD leg would firm up the river-arm union.
- Local UTM meshing (vs the current EPSG:4326 degree frame) remains ADR 0192 Q1.
- Placement (TELEMAC geometry / SCHISM hgrid / registered tool) stays NATE's.
