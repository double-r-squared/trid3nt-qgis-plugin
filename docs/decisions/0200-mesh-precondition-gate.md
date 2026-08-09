# ADR 0200 -- Mesh as an optional user-supplied precondition (standalone builder + gate)

Status: accepted (build landed; live proof: watershed path)
Date: 2026-08-08
Supersedes/extends: ADR 0192 (oceanmesh sandbox), 0193 (watershed-first), 0194
(coastal water-edge), 0196 (telemac rain-on-grid mesh acquisition seam).

## Context

Mesh creation was, until now, a PRIVATE step inside each model template
(`acquire_watershed_mesh` delineated + meshed inline). NATE's design
(docs/IDEAS.md 2026-08-08) inverts this: mesh creation is an EXPLICIT user act via
a standalone tool, and model templates gain PRECONDITION POLYMORPHISM -- if a mesh
artifact already exists in the case, the template ASKS "use this mesh?" before
building its own. Rationale: delineating inside a bbox reproduces the cut-off
problem a bbox-first design already has; a user (or a prior explicit mesh step)
supplying the domain is strictly better. Basis ranking: user mesh > drawn box >
geocoded AOI (the same seam family as DrawnGeometry + the input-review gate).

## Decision

1. **Standalone tool `generate_mesh`** (question-class name; never place-based),
   `tier="general"`, `cacheable=False`. It promotes the PROVEN sandbox meshers
   behind ONE tool with the mode INFERRED from inputs:
   - a `pour_point` (or `mesh_mode="watershed"`) -> the ADR 0193 watershed-first
     mesher (catchment domain, distance-to-river refinement);
   - a coastal AOI (or `mesh_mode="coastal"`) -> the ADR 0194 water-edge mesher
     (OSM+NHD water polygon, distance-to-shore + wavelength refinement).
   Sizing is exposed as user levers (`min_edge_length_m`, `max_edge_length_m`,
   `grade`) per the granularity norm. The GPL OceanMesh2D engine stays isolated in
   `trid3nt-local/mesh:latest` (shelled, never imported).

   It EMITS INTO THE CASE, reusing existing seams (no parallel store):
   - a **display layer** -- an MDAL-loadable `.2dm` (`layer_type="mesh"`, explicit
     UTM `crs_authid`) auto-emitted as an ordinary `LayerURI` -> `loaded_layers`;
   - a **mesh artifact record** (`MeshArtifact`: format URIs, CRS, node/element
     counts, `has_bathymetry`, open-boundary info, `engine_compat`) persisted in a
     same-process **case-keyed stash** (mirrors `publish_layer._LAST_LEGEND_BY_URI`)
     AND a durable **`mesh_artifact.json` sidecar** written beside the mesh objects
     (its key is the mesh key with the basename swapped, so any mesh row's `uri`
     resolves its facts in a later session);
   - the real `.slf` (+ best-effort `.gr3`/`fort.14`) solver geometries as case
     artifacts, so acceptance is the common case across engines.

2. **The precondition gate** (`workflows/mesh/precondition_gate.py`,
   engine-generic): at a consuming template's run start, discover the case's mesh
   artifacts, keep only those the target engine can actually solve on
   (`mesh_compatible_with_engine` -- TELEMAC needs a bathymetric SELAFIN; SCHISM an
   hgrid; SWAN a fort.14), and:
   - compatible mesh exists -> fire a yes/no gate on the EXISTING input-review /
     `tool-payload-warning` spine ("use this mesh (<name>, <n> elements)?", labeled
     default = USE, per the basis ranking). Accepted -> the supplied-mesh path;
     declined -> unchanged AOI/pour-point authoring. The gate's decline means
     "build fresh" and NEVER cancels the run (its one semantic difference from
     input-review's cancel).
   - incompatible mesh -> do NOT gate; proceed with the fallback + ONE loud
     narration line saying why the mesh was skipped (never a silent force-fit).
   - AUTO mode / no live session (headless seeding) -> apply the labeled default
     (use the compatible mesh) without pausing.

   First consumer: `telemac_rain_on_grid`. The accepted path reads the case `.2dm`
   nodes (reprojected UTM->lon/lat so NLCD node-field sampling is identical to the
   built path) and points the solve at the case `.slf` -- a real end-to-end
   consumption, no fresh delineation. SCHISM/SWAN templates adopt the same helper
   by passing their own `engine`.

## Consequences

- No new WS event, no new envelope, no plugin change: the gate rides the same
  `tool-payload-warning` / pending-confirmation card the input-review gate uses.
- No parallel artifact store: facts ride the layer-emission + object-store seams
  the case already has (same-process stash + durable sidecar).
- The coastal water-edge path shares the exact container seam as the watershed
  path; the ADR live proof is the watershed (Coweeta) case, coastal is
  live-verified separately.
- `use_supplied_mesh` (the ADR 0196 stub) is complemented by `use_supplied_mesh_2dm`
  (full node population) so the seam is real end-to-end, not a retrofit.
