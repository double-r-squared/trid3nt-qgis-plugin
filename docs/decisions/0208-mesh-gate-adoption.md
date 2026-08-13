# ADR 0208 -- Mesh precondition gate: SCHISM adoption, SWAN honest-decline, full-results publishing

Status: accepted (build landed; live proof: coastal generate_mesh + SCHISM gate)
Date: 2026-08-09
Extends: ADR 0200 (mesh as an optional user-supplied precondition + the gate),
0206 (telemac rain-on-grid hyetograph), 0189 (schism baroclinic circulation).

## Context

ADR 0200 landed the standalone `generate_mesh` tool, the `MeshArtifact` record, and
the engine-generic precondition gate (`workflows/mesh/precondition_gate.py`), with
`telemac_rain_on_grid` as the FIRST consumer. Two follow-on gaps remained:

1. `generate_mesh` only ever emitted a TELEMAC SELAFIN (`.slf`); `gr3_uri` /
   `fort14_uri` were always `None`, so SCHISM and SWAN could NEVER be compatible --
   the gate's SCHISM/SWAN branches were dead. The ADR 0200 promise ("SCHISM/SWAN
   adopt the same gate by passing their own engine") was un-exercised.
2. NATE's scrubbing ask: the TELEMAC rain-on-grid run store keeps `r2d_rog.slf`
   (all frames, all variables -- QGIS/MDAL reads it natively with the temporal
   controller), but the template only published the peak-depth COG. The full
   time-series was staged in the runs bucket and never surfaced to the Case.

## Decision

### Part 1 -- SCHISM adoption (honest, where the mesh is separable)

- **Compat facts extended** (`workflows/mesh/artifact.py`): SCHISM compatibility now
  requires a `.gr3` AND sampled bathymetry AND a designated OPEN (seaward) boundary
  (`open_boundary_info.open_node_count > 0`). Bare bathymetry is not enough -- a
  SCHISM solve needs a boundary to force tides / T-S at. An inland `generate_mesh`
  WATERSHED mesh (fully closed, `open_boundary_info == {}`) is therefore honestly
  DECLINED with that reason; a COASTAL mesh built with an `open_boundary_side`
  carries the boundary and is accepted.
- **`generate_mesh` emits the missing facts when it can**: a COASTAL mesh built with
  the new `open_boundary_side` lever ("south"/"north"/"east"/"west", granularity
  norm) also emits `hgrid.gr3` via the SCHISM worker's proven pure-numpy
  `tin_to_hgrid` bridge (SCHISM depth is positive-down, so the mesh bed is negated),
  records `open_boundary_info` (side + open-node count) and `gr3_uri`, and adds
  `"schism"` to `engine_compat`. Best-effort: any bridge failure leaves the mesh
  TELEMAC-only (never fails the build). A watershed mesh (or a coastal mesh with no
  `open_boundary_side`) stays TELEMAC-only.
- **Consumer: `schism_baroclinic_circulation`** (the natural first -- it builds a
  shoreline-clipped estuary mesh internally). The gate runs at solve start; an
  accepted case mesh's `hgrid.gr3` is parsed into `(points_lonlat, tris,
  depths_down)` and passed to `author_baroclinic_estuary_deck` via a new ADDITIVE
  `supplied_mesh` param: the user mesh (real shoreline + real sampled bathymetry)
  REPLACES the idealized lattice, while the salinity IC gradient, freshwater river
  source, and tidal open boundary are still authored keyed to `ocean_side` (taken
  from the mesh's designated open boundary). The forcing stays idealized; only the
  DOMAIN GEOMETRY becomes real -- surfaced honestly in the result `synthetic_inputs`.
  Declined / absent / incompatible -> the idealized channel, unchanged.

  SKIPPED with reason:
  - `schism_coupled_waves` -- its mesh is the bundled SCHISM Test_WWM_Duck FRF
    VALIDATION deck (33586 elements + bundled field observations); the mesh IS the
    validation case, not a user-swappable domain. Adopting the gate would let a
    user mesh silently displace the published validation geometry -- dishonest.
  - `schism_tidal_hydro` -- structurally adoptable (its `coastal_tin` path already
    runs `tin_to_hgrid` on a real US AOI) and registered as a same-helper candidate,
    but NOT wired this landing to keep the change bounded to one live-verified
    consumer; queued behind the baroclinic proof.

### Part 2 -- SWAN honest-decline (regular-grid worker)

The SWAN worker is REGULAR-GRID ONLY (`CGRID REGULAR` + `INPGRID BOTTOM` +
`bottom.bot` sampled from a DEM); it has no unstructured (`fort.14`) path. So
`mesh_compatible_with_engine(art, "swan")` returns `False` UNCONDITIONALLY with the
reason "the SWAN worker is REGULAR-GRID ... it cannot consume a user-supplied mesh"
(an `unstructured_unsupported` flag on the SWAN requirements row), and NO gate is
wired into `swan_wave_field`. Documented, not forced -- the honest outcome per the
task's own instruction.

### Part 3 -- Full-results publishing (`telemac_rain_on_grid`)

After the peak-depth COG, the template publishes the full-results SELAFIN
(`r2d_rog.slf` -- all frames, all variables) as a `layer_type="mesh"` Case layer
("Model results (time series): <reach>") via the ADR 0200 mesh-layer seam
(`publish_input_layer`, role="context"), stamped in the mesh's own UTM CRS. It rides
the runs-bucket object the depth COG was rasterized from (no re-upload). QGIS/MDAL
opens the SELAFIN natively and its time steps drive the temporal controller -- no
per-frame COGs, no plugin change (the plugin already loads mesh layers via MDAL per
0200).

## Consequences

- No new WS event, no new envelope, no plugin change (the gate + the mesh-results
  layer both ride existing seams).
- `author_baroclinic_estuary_deck`'s `supplied_mesh` is purely additive (default
  `None` = unchanged idealized-lattice behavior + all prior tests green).
- Payload: `r2d_rog.slf` is ~4 MB for a 6 h / 37-frame run and grows ~linearly with
  frames x nodes; a multi-day / high-graphic-period run can reach tens of MB (still
  MDAL-streamable via /vsicurl/). Stated in the template docstring.
- SWAN unstructured meshing remains a real gap; if a `fort.14` SWAN path is ever
  added the compat row flips from `unstructured_unsupported` to a `fort14_uri`
  requirement and the gate lights up with no other change.
