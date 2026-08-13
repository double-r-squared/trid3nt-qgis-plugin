# ADR 0212 -- Mesh precondition gate: schism_tidal_hydro (coastal_tin) adoption

Status: accepted (build landed; live proof: coastal Galveston generate_mesh + SCHISM
tidal_hydro gate accepted + solve green)
Date: 2026-08-09
Extends: ADR 0208 (mesh gate SCHISM adoption -- baroclinic first consumer),
0200 (mesh as an optional user-supplied precondition + the gate),
0118 (schism_tidal_hydro barotropic tidal core + the coastal_tin path).

## Context

ADR 0208 landed the engine-generic precondition gate's SCHISM branch and wired the
FIRST consumer (`schism_baroclinic_circulation`) via an additive `supplied_mesh` deck
param, but bounded the change to one live-verified consumer.
`schism_tidal_hydro` was registered there as "structurally adoptable via the same
helper" (its `coastal_tin` path already runs `tin_to_hgrid` on a real US AOI) and
queued. This ADR wires it.

## Decision

The `coastal_tin` path of `schism_tidal_hydro` adopts the gate exactly per the
baroclinic pattern:

- **Additive deck param** (`deck_authoring.author_coastal_tin_deck`): a default-None
  `supplied_mesh=(points_lonlat (N,2), tris (M,3) 0-based, depths_positive_down (N,))`.
  `points`/`cells`/`depths` become optional (default None) -- the author needs EITHER
  the internal-TIN arrays OR a supplied mesh (a missing-both call is a typed
  `SCHISM_MESH_INVALID` error). When `supplied_mesh` is given it REPLACES the TIN
  geometry; the deck (bctides open-boundary block, drag, station, param) is authored
  on those nodes and the open boundary is re-keyed via `tin_to_hgrid(open_boundary_side)`.
  Purely additive: existing callers pass points/cells/depths and are unchanged.

- **Composer** (`tidal_hydro._build_coastal_tin_deck`): a new `_schism_mesh_precondition_gate`
  (the baroclinic helper, adapted -- `engine="schism"`, `tool_name="schism_tidal_hydro"`)
  runs at the TOP, before any TIN is built. It discovers the case mesh, keeps only
  SCHISM-compatible ones (a designated open boundary), and:
  - **accepted** -> materialize the mesh's `hgrid.gr3`, parse it with the composer's
    `_parse_hgrid_nodes_cells` into `(points, tris, depths_down)`, author the deck via
    `author_coastal_tin_deck(supplied_mesh=...)` with `open_boundary_side` taken from
    the mesh's designated open side. The internal oceanmesh TIN worker + bathymetry
    fetch are SKIPPED entirely (no GSHHG needed on this path). Honesty: real domain
    geometry + the template's EXISTING screening tidal forcing (a spatially-uniform
    constituent amplitude), surfaced in the result `synthetic_inputs` (basis=user).
  - **declined / absent** -> `None`; the internal coastal TIN is built as before,
    unchanged.
  - **incompatible** (a case mesh with no open boundary) -> not gated; the gate's loud
    one-line skip reason is folded into the fallback provenance note (the gate already
    logged one WARNING line).

The `bundled_quarterannulus` verification path never calls `_build_coastal_tin_deck`,
so it never reaches the gate -- the analytical RMSE/amplitude verification is
untouched.

## Consequences

- No new WS event, no new envelope, no plugin change (rides the ADR 0200/0208 gate
  spine). No `artifact.py` change -- the SCHISM compat facts (gr3 + bathymetry +
  open boundary) from ADR 0208 already serve `tidal_hydro` (same `engine="schism"`).
- `author_coastal_tin_deck`'s `supplied_mesh` is additive (default None = the internal
  TIN, all prior tests green); making points/cells/depths optional keeps every
  existing keyword call valid.
- Gate activates ONLY when a compatible case mesh is present, so the tidal_hydro
  showcase default (bundled_quarterannulus) is unaffected -- no re-seed.
- Live proof (same-process direct drive through the real `trid3nt-local/schism:latest`
  solver, no daemon): generate_mesh coastal Galveston -> stashed mesh 'Galveston Bay'
  (30 elements, open side=south) -> gate AUTO-accepted -> "consuming case mesh ...
  instead of the internal coastal TIN" -> solve green (24 nodes, elev_max 0.395 m,
  tidal_range 0.895 m, run 01KZMCKZMVTXZ08KA140A1GVPJ). Control QuarterAnnulus (no
  mesh) green + verification unchanged (RMSE 0.0155 m, corr 0.99882).
- Remaining SCHISM gate consumer: `schism_coupled_waves` stays SKIPPED (its mesh IS
  the bundled Test_WWM_Duck validation deck -- a user mesh would displace the
  published validation geometry; ADR 0208).
