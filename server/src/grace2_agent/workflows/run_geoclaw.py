"""GeoClaw (Clawpack) deck-build + staging + Batch-dispatch orchestration
(sprint-17 — the GeoClaw analogue of ``run_swmm.py`` / ``run_modflow.py``).

One module owns the GeoClaw engine's solver-dispatch surface. Unlike SWMM (whose
pyswmm runs IN-PROCESS in the agent venv) GeoClaw is a Fortran solver that lives
ONLY in the worker container image (Clawpack compiles its Fortran at install) —
there is NO in-process agent lane. So GeoClaw is BATCH-PRIMARY: the agent stages
a ``build_spec`` (the typed run args) + a topo DEM to S3 and dispatches through
the SAME generic ``run_solver`` / ``wait_for_completion`` seam SFINCS uses, then
downloads the GeoClaw ``fort.q`` frames and postprocesses them.

  1. **build_spec assembly + staging** (``stage_geoclaw_manifest``). Builds the
     worker-contract manifest (``inputs[]`` = the topo DEM + optional dtopo/surge
     forcing; ``build_spec`` = the setrun_builder field dict; ``outputs`` = the
     fort.q globs) and uploads it + the DEM to the cache bucket, returning the
     ``manifest.json`` URI to feed ``run_solver(solver='geoclaw', ...)``.

  2. **GeoClaw solver registration** (``register_geoclaw_solver``). Adds
     ``'geoclaw'`` to ``SOLVER_WORKFLOW_REGISTRY`` (idempotent ``setdefault``,
     mirroring ``register_swmm_solver``) so ``run_solver(solver='geoclaw')``
     dispatches. The orchestrator ALSO pins the registry entry in code (the
     shared-append line this lane returns) so the dispatch works even when this
     module is not imported first.

Determinism boundary (Invariant 1 / 2): no LLM call anywhere in this module. The
deck is authored deterministically (in the worker, via setrun_builder); every
number the agent narrates comes from the typed ``GeoClawDepthLayerURI`` fields the
postprocess computed — never free-generated.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from grace2_contracts import new_ulid
from grace2_contracts.geoclaw_contracts import GeoClawRunArgs

logger = logging.getLogger("grace2_agent.workflows.run_geoclaw")

__all__ = [
    "GeoClawWorkflowError",
    "GeoClawStaging",
    "build_geoclaw_build_spec",
    "stage_geoclaw_manifest",
    "register_geoclaw_solver",
    "plan_geoclaw_domain",
    "plan_geoclaw_grid",
    "resolve_offshore_source",
    "finalize_geoclaw_domain",
    "reproject_dem_to_4326",
    "GEOCLAW_SOLVER_NAME",
    "GEOCLAW_OFFSHORE_SCENARIOS",
]

#: Scenarios whose driver source is OFFSHORE (a seafloor Okada deformation) and so
#: REQUIRE the computational domain to extend off the AOI coast into deep water.
#: ``dam_break`` (an onshore impoundment release) + ``surge`` (a uniform sea-level
#: offset, no point source) keep ``domain == AOI``.
GEOCLAW_OFFSHORE_SCENARIOS: frozenset[str] = frozenset({"tsunami"})

#: Elevation (m, positive-up) at/above which a DEM cell is treated as LAND when
#: validating / relocating the Okada source. A source must sit strictly below this
#: (i.e. under water) so the seafloor deformation displaces a real water column.
_SOURCE_WET_ELEV_M: float = 0.0

#: Deep-water FLOOR (m, positive-up) for the resolved Okada source (P1.1). A source
#: planted over a shallow shelf puddle (e.g. -0.7 m) displaces a negligible water
#: column; the canonical megathrust source sits over genuinely deep water (tens to
#: thousands of metres). So we PREFER the deepest cell below this floor, falling
#: back to any below-waterline cell only when the domain has no genuinely-deep
#: water (never regressing to the onshore centroid).
_SOURCE_DEEP_ELEV_M: float = -50.0

#: Minimum inset (degrees, ~5.5 km) the resolved offshore source is held off the
#: fetched-DEM edge (issue #9) so the final computational domain can be grown to
#: ENCLOSE the source with a margin WITHOUT reaching past the topo coverage (GeoClaw
#: aborts when the domain is not fully covered by topo). Also keeps the Okada source
#: off the absorbing domain boundary.
_SOURCE_DEM_EDGE_INSET_DEG: float = 0.05


#: The registry key + handle ``solver`` tag for the GeoClaw engine.
GEOCLAW_SOLVER_NAME: str = "geoclaw"

#: GeoClaw fort.q output globs the postprocess reads (the AMR ASCII frames +
#: their headers + the echoed deck manifest). Kept BYTE-IDENTICAL to the worker
#: entrypoint's output list so the agent + worker agree on the harvested set; the
#: fgmax monitor (fgmax{NNNN}.txt + fgmax_grids.data) + gauge time series
#: (gauge{NNNNN}.txt) ride along for the GAP1 fgmax reader.
GEOCLAW_OUTPUT_GLOBS: list[str] = [
    "_output/fort.q*",
    "_output/fort.t*",
    "_output/fort.h*",
    "_output/fort.b*",
    "_output/fgmax*.txt",
    "_output/fgmax_grids.data",
    "_output/gauge*.txt",
    "deck_manifest.json",
]


# --------------------------------------------------------------------------- #
# Errors (mirrors SWMMWorkflowError shape).
# --------------------------------------------------------------------------- #
class GeoClawWorkflowError(RuntimeError):
    """Raised on any deck-spec / staging / dispatch failure.

    Carries an open-set A.6 ``error_code`` so the agent emitter renders a typed
    error frame. Codes:

    - ``GEOCLAW_PARAMS_INVALID`` — the run args could not be coerced.
    - ``GEOCLAW_STAGING_FAILED`` — the build_spec / DEM upload failed.
    - ``GEOCLAW_RUN_FAILED`` — the Batch solve did not complete.
    - ``GEOCLAW_BATCH_OUTPUT_MISSING`` — a 'complete' solve produced no fort.q.
    """

    error_code: str = "GEOCLAW_WORKFLOW_FAILED"

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# --------------------------------------------------------------------------- #
# Staging result — the Batch-lane handoff (mirrors SWMMStaging).
# --------------------------------------------------------------------------- #
@dataclass
class GeoClawStaging:
    """The result of assembling + staging a GeoClaw build_spec + DEM.

    Fields:
        run_id: the run identifier the output COGs are keyed under.
        manifest_uri: the ``s3://`` URI of the staged ``manifest.json``.
        build_spec: the setrun_builder field dict that was staged.
        run_args: the validated ``GeoClawRunArgs`` (echoed for provenance).
        bbox: the AOI the postprocess rasterizes onto.
    """

    run_id: str
    manifest_uri: str
    build_spec: dict[str, Any]
    run_args: GeoClawRunArgs
    bbox: tuple[float, float, float, float]
    n_active_cells: int = 0
    resolution_m: float = 0.0
    staged_inputs: list[dict[str, str]] = field(default_factory=list)
    # The COMPUTATIONAL DOMAIN actually authored into the deck (offshore-extended
    # for a tsunami; == ``bbox`` otherwise). Echoed for provenance / narration.
    domain_bbox: tuple[float, float, float, float] | None = None


# --------------------------------------------------------------------------- #
# build_spec assembly.
# --------------------------------------------------------------------------- #
def build_geoclaw_build_spec(
    run_args: GeoClawRunArgs,
    *,
    topo_dest: str = "topo.asc",
    dtopo_dest: str | None = None,
    surge_dest: str | None = None,
    extra_topo_files: list[str] | None = None,
    base_num_cells: tuple[int, int] = (40, 40),
    domain_bbox: tuple[float, float, float, float] | None = None,
    source_lonlat_override: tuple[float, float] | None = None,
    amr_levels_override: int | None = None,
) -> dict[str, Any]:
    """Assemble the setrun_builder ``build_spec`` dict from the validated run args.

    The single source of truth for the worker-side deck author's input. Maps the
    typed ``GeoClawRunArgs`` onto the flat build_spec the worker's
    ``setrun_builder.parse_build_spec`` consumes. The staged DEM is referenced by
    its in-deck destination filename (``topo_dest``); a staged dtopo / surge file
    is referenced by ``dtopo_dest`` / ``surge_dest`` when present.

    ``extra_topo_files`` are the staged-destination names of additional topo/bathy
    tiles (ordered coarse -> fine, appended AFTER the primary ``topo_dest`` so the
    worker layers them finest-last). ``fgmax_arrival_tol_m`` always rides along
    (it backs the fgmax wave-arrival monitor); ``coastal_gauge_lonlat`` and the
    four USER-GATED Okada ``fault_*`` keys are threaded ONLY when supplied (the
    engine substitutes scenario defaults otherwise and MUST surface that, never
    silently fabricate them).

    Pure dict assembly — unit-testable with no network.
    """
    spec: dict[str, Any] = {
        "scenario": run_args.scenario,
        "bbox": list(run_args.bbox),
        "topo_file": topo_dest,
        "sim_duration_s": float(run_args.sim_duration_s),
        "output_frames": int(run_args.output_frames),
        # The COMPOSER's cost-bounded AMR-level plan (plan_geoclaw_grid) overrides
        # the raw run_args.amr_levels so the AOI mesh stays within the runtime
        # budget; absent -> honor the requested amr_levels verbatim (back-compat).
        "amr_levels": int(
            amr_levels_override
            if amr_levels_override is not None
            else run_args.amr_levels
        ),
        "manning_n": float(run_args.manning_n),
        "sea_level_m": float(run_args.sea_level_m),
        "base_num_cells": [int(base_num_cells[0]), int(base_num_cells[1])],
        "source_magnitude": float(run_args.source_magnitude),
        "dam_break_depth_m": float(run_args.dam_break_depth_m),
        "fgmax_arrival_tol_m": float(run_args.fgmax_arrival_tol_m),
    }
    # Source point: a composer-RESOLVED offshore override (tsunami, placed over
    # deep water + spanned by domain_bbox) wins over the user's raw source_lonlat;
    # else the raw source_lonlat; else the worker falls back to the AOI centroid.
    _src = source_lonlat_override or run_args.source_lonlat
    if _src is not None:
        spec["source_lonlat"] = [float(_src[0]), float(_src[1])]
    # The offshore-extended COMPUTATIONAL DOMAIN (clawdata bounds). Only threaded
    # when it differs from the AOI (the worker defaults domain -> AOI otherwise).
    if domain_bbox is not None and tuple(domain_bbox) != tuple(run_args.bbox):
        spec["domain_bbox"] = [float(v) for v in domain_bbox]
    if extra_topo_files:
        spec["extra_topo_files"] = list(extra_topo_files)
    if run_args.coastal_gauge_lonlat is not None:
        spec["coastal_gauge_lonlat"] = [
            float(run_args.coastal_gauge_lonlat[0]),
            float(run_args.coastal_gauge_lonlat[1]),
        ]
    # USER-GATED Okada fault overrides: thread ONLY the ones the user supplied.
    if run_args.fault_strike_deg is not None:
        spec["fault_strike_deg"] = float(run_args.fault_strike_deg)
    if run_args.fault_dip_deg is not None:
        spec["fault_dip_deg"] = float(run_args.fault_dip_deg)
    if run_args.fault_rake_deg is not None:
        spec["fault_rake_deg"] = float(run_args.fault_rake_deg)
    if run_args.fault_depth_km is not None:
        spec["fault_depth_km"] = float(run_args.fault_depth_km)
    if run_args.scenario == "tsunami" and dtopo_dest is not None:
        spec["dtopo_file"] = dtopo_dest
    if run_args.scenario == "surge" and surge_dest is not None:
        spec["surge_forcing_file"] = surge_dest
    return spec


# --------------------------------------------------------------------------- #
# Offshore-domain planning + bathymetry-aware Okada source placement.
#
# The two physics-setup fixes that turn a zero-inundation coastal tsunami run into
# a real run-up (sprint-17 follow-up):
#
#   1. DOMAIN EXTENT. ``plan_geoclaw_domain`` extends the computational domain
#      offshore so it SPANS the Okada source -> the AOI coast. A source-at-center,
#      land-only micro-AOI can never inundate regardless of bathymetry -- the
#      domain MUST reach offshore into the deep-water column the source displaces.
#
#   2. SOURCE PLACEMENT. ``resolve_offshore_source`` honors a user/composer
#      offshore source when it sits over below-waterline bathymetry, else projects
#      the source onto the DEEPEST deep-water cell seaward of the AOI (reading the
#      fetched DEM with rasterio) -- never the onshore AOI centroid.
#
# General (no Crescent-City hard-coding): the domain is sized from the AOI span +
# the requested source, and the source is chosen from the bathymetry of whatever
# coastal AOI was asked for.
# --------------------------------------------------------------------------- #
def plan_geoclaw_domain(
    bbox: tuple[float, float, float, float],
    scenario: str,
    source_lonlat: tuple[float, float] | None,
) -> tuple[float, float, float, float]:
    """Compute the COMPUTATIONAL DOMAIN bbox for a GeoClaw run.

    For an OFFSHORE-source scenario (tsunami) the domain extends off the AOI on
    all sides by at least one AOI span (floored so a small AOI still reaches the
    shelf), and is grown further to enclose an explicit ``source_lonlat`` with a
    buffer so the Okada deformation + a deep-water column sit INSIDE the domain.
    For dam_break / surge the domain is the AOI unchanged.

    Direction-agnostic (pads all sides equally) so it works for any coastal AOI
    regardless of which side the ocean is on; the seaward side is resolved later
    from the bathymetry by ``resolve_offshore_source``. Returns a lon/lat-clamped
    ``(min_lon, min_lat, max_lon, max_lat)``.
    """
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    if str(scenario) not in GEOCLAW_OFFSHORE_SCENARIOS:
        return (min_lon, min_lat, max_lon, max_lat)

    span_x = max_lon - min_lon
    span_y = max_lat - min_lat
    # Offshore pad: at least one AOI span on each side, floored to ~0.1 deg
    # (~11 km, comfortably past the surf zone onto the shelf for ETOPO bathy).
    pad = max(span_x, span_y, 0.1)
    d_min_lon = min_lon - pad
    d_min_lat = min_lat - pad
    d_max_lon = max_lon + pad
    d_max_lat = max_lat + pad

    if source_lonlat is not None:
        slon, slat = float(source_lonlat[0]), float(source_lonlat[1])
        buf = max(0.05, 0.25 * pad)
        d_min_lon = min(d_min_lon, slon - buf)
        d_min_lat = min(d_min_lat, slat - buf)
        d_max_lon = max(d_max_lon, slon + buf)
        d_max_lat = max(d_max_lat, slat + buf)

    # Clamp to valid lon/lat (a coastal AOI near the antimeridian/poles still
    # yields a well-formed, in-range domain).
    d_min_lon = max(d_min_lon, -180.0)
    d_max_lon = min(d_max_lon, 180.0)
    d_min_lat = max(d_min_lat, -90.0)
    d_max_lat = min(d_max_lat, 90.0)
    return (d_min_lon, d_min_lat, d_max_lon, d_max_lat)


# --------------------------------------------------------------------------- #
# Cost-bounded grid + AMR planning (the SOLVER_TIMEOUT fix).
#
# GeoClaw cost is driven by the COMPUTATIONAL GRID, not the topo pixel count. A
# wet coastal solve over the offshore-extended domain TIMES OUT if the base grid
# is sized one-cell-per-topo-pixel and the finest AMR level is created over the
# whole AOI at metres resolution. The canonical tsunami pattern instead is:
#   - a COARSE base (level-1) grid over the full propagation domain (~1 arcmin),
#     so the open ocean is only a few thousand cells; and
#   - NESTED AMR refined ONLY at the coastal AOI (gated by the setrun region), to
#     a run-up resolution of tens of metres, with the finest mesh BOUNDED by a
#     cell budget so a wet domain finishes in minutes (not hours).
# plan_geoclaw_grid sizes both deterministically from the domain + AOI geometry.
# --------------------------------------------------------------------------- #

#: Target base (level-1) cell size in degrees (~1 arcmin ~= 1.8 km): coarse enough
#: that the open-ocean propagation grid is only a few thousand cells, fine enough
#: to carry the long tsunami wave. AMR refines from here toward the AOI.
_GEOCLAW_BASE_TARGET_DEG: float = 1.0 / 60.0
#: Base-grid cell-count clamp per axis: a tiny AOI still gets >= _MIN cells; a huge
#: offshore domain is capped so the base grid stays a few thousand cells total.
_GEOCLAW_BASE_CELLS_MIN: int = 30
_GEOCLAW_BASE_CELLS_MAX: int = 90
#: Run-up resolution target (m): AMR grows until the AOI finest cell is at or below
#: this -- the "dense coastal inundation" floor. Lowered 40 -> 20 m (P2) so a
#: town-scale AOI refines to a ~20 m run-up mesh (a DENSE inundation sheet) instead
#: of stopping at ~38 m -- the finer nearshore nested topo (P2) is wasted under a
#: 38 m cell. The per-step cost is still bounded by ``_GEOCLAW_FINEST_CELL_BUDGET``
#: (UNCHANGED) so the coarse-base + AOI-AMR perf design holds; only the timestep
#: count grows with the finer dx.
_GEOCLAW_TARGET_FINEST_M: float = 20.0
#: Do NOT refine finer than this (m): a guard against an unbounded finest mesh.
#: Lowered 25 -> 15 m so the level-5 nest (~20 m, gentle final 2x ratio) is allowed
#: but a runaway sub-10 m mesh is not.
_GEOCLAW_MIN_FINEST_M: float = 15.0
#: Budget: max finest-level cells permitted over the AOI. Bounds the per-step AMR
#: cost (the finest level is pinned over the AOI for the whole run) so the wet
#: solve stays minutes, not hours. UNCHANGED at 400k (P2): the per-step ceiling is
#: the proven perf guardrail -- a large AOI still budget-clamps to the SAME coarser
#: run-up it did before; only small/town AOIs (cell headroom) spend the extra level.
_GEOCLAW_FINEST_CELL_BUDGET: int = 400_000
#: Hard cap on AMR levels regardless of the request (keeps ratios + cost bounded).
#: Raised 4 -> 5 (P2): the 5th level (gentle final 2x ratio, cumulative 64x) takes a
#: town AOI run-up from ~38 m to ~20 m for a dense inundation footprint, while the
#: cell budget keeps a large AOI from ever creating that finest level.
_GEOCLAW_MAX_AMR_LEVELS: int = 5
#: Approx metres per degree of latitude (spherical mean) for the cost estimate.
_GEOCLAW_M_PER_DEG: float = 111_320.0


def _geoclaw_refinement_product(levels: int) -> int:
    """Cumulative AMR refinement (base -> finest) for ``levels`` levels.

    MIRRORS ``setrun_builder._refinement_ratios`` EXACTLY (first transition 2x,
    middle transitions 4x, and -- for a deep >= 5-level nest -- the FINAL
    transition steps back to 2x) so the agent-side cost estimate matches the deck
    the worker authors from it. ``levels <= 1`` -> ``1`` (a uniform base grid).
    """
    n = max(int(levels) - 1, 0)
    product = 1
    for i in range(n):
        if i == 0:
            product *= 2
        elif i == n - 1 and n >= 4:
            product *= 2  # gentle final step for a deep (>= 5-level) nest
        else:
            product *= 4
    return product


#: Levels above the coarse base the INTERMEDIATE offshore PROPAGATION tier sits at
#: (== ``setrun_builder._PROPAGATION_LEVELS_ABOVE_BASE``). 2 levels above base ==
#: level 3 (~230 m for the ~1.8 km base): the ~200-500 m mid-resolution grid the
#: canonical tsunami nesting forces over the source->coast corridor + shelf.
_GEOCLAW_PROPAGATION_LEVELS_ABOVE_BASE: int = 2


def _geoclaw_propagation_level(amr_levels: int) -> int:
    """The INTERMEDIATE offshore propagation/shelf refinement level.

    MIRRORS ``setrun_builder._propagation_level`` EXACTLY (a pure function of
    ``amr_levels``: ``_PROPAGATION_LEVELS_ABOVE_BASE`` above the base, capped at
    one-below-finest, floored at the base) so the agent's propagation-tier cell /
    cost estimate matches the region the worker emits (the agent <-> worker
    cross-check). ``amr_levels <= 2`` -> ``1`` (no separate propagation tier).
    """
    base_plus = 1 + _GEOCLAW_PROPAGATION_LEVELS_ABOVE_BASE
    return min(base_plus, max(int(amr_levels) - 1, 1))


def plan_geoclaw_grid(
    domain_bbox: tuple[float, float, float, float],
    aoi_bbox: tuple[float, float, float, float],
    requested_amr_levels: int,
) -> tuple[tuple[int, int], int, int, int, int]:
    """Plan a tractable (base grid, AMR levels) for a GeoClaw run.

    Returns ``((base_nx, base_ny), amr_levels, est_finest_aoi_cells,
    propagation_level, est_propagation_domain_cells)``:

      - ``base_num_cells``: a COARSE level-1 grid over ``domain_bbox`` sized to
        ~``_GEOCLAW_BASE_TARGET_DEG`` per cell, clamped to
        ``[_GEOCLAW_BASE_CELLS_MIN, _GEOCLAW_BASE_CELLS_MAX]`` per axis (a few
        thousand cells total -- NOT one cell per topo pixel).
      - ``amr_levels``: grown level-by-level (using the same refinement schedule
        the worker authors) until the AOI finest cell reaches
        ``_GEOCLAW_TARGET_FINEST_M`` (tens of metres) AND the user's request is
        satisfied, then STOPPED before the finest mesh would exceed
        ``_GEOCLAW_FINEST_CELL_BUDGET`` cells / go finer than
        ``_GEOCLAW_MIN_FINEST_M``. Capped at ``_GEOCLAW_MAX_AMR_LEVELS``.
      - ``est_finest_aoi_cells``: the estimated finest-level cell count over the
        AOI (used for compute-class sizing -- a far better work proxy than the
        base-grid cell count, since the finest mesh is pinned over the AOI).
      - ``propagation_level``: the INTERMEDIATE offshore propagation/shelf tier
        the worker FORCES over the whole offshore-extended domain (the source ->
        coast corridor + continental shelf) so the wave is resolved as it shoals,
        not damped on the base grid. ``_geoclaw_propagation_level(amr_levels)``
        (mirrors the worker). 1 == no separate tier (a shallow nest / domain==AOI).
      - ``est_propagation_domain_cells``: the estimated cell count of that
        intermediate tier over the WHOLE domain (``base_cells * product^2``) --
        the offshore cost the propagation tier adds. It is bounded well under the
        finest-AOI per-step work (the propagation tier steps fewer substeps and
        its per-step cells are comparable to the finest AOI), so the existing
        ``_GEOCLAW_FINEST_CELL_BUDGET`` stays the binding runtime guard.

    Deterministic geometry only (no I/O); general for ANY coastal AOI. A large
    AOI is bounded to a coarser run-up resolution by the budget (still non-zero
    inundation); a small AOI reaches the tens-of-metres target.
    """
    import math

    d0, d1, d2, d3 = (float(v) for v in domain_bbox)
    a0, a1, a2, a3 = (float(v) for v in aoi_bbox)
    dom_w = max(d2 - d0, 1e-9)
    dom_h = max(d3 - d1, 1e-9)
    aoi_w = max(a2 - a0, 1e-9)
    aoi_h = max(a3 - a1, 1e-9)
    coslat = max(math.cos(math.radians(0.5 * (d1 + d3))), 0.1)

    # (1) COARSE base grid: ~_GEOCLAW_BASE_TARGET_DEG per cell, clamped per axis.
    nx = max(
        _GEOCLAW_BASE_CELLS_MIN,
        min(int(round(dom_w / _GEOCLAW_BASE_TARGET_DEG)), _GEOCLAW_BASE_CELLS_MAX),
    )
    ny = max(
        _GEOCLAW_BASE_CELLS_MIN,
        min(int(round(dom_h / _GEOCLAW_BASE_TARGET_DEG)), _GEOCLAW_BASE_CELLS_MAX),
    )

    # Base cell size + AOI extent in metres (lon scaled by cos(lat)).
    base_dx_m = (dom_w / nx) * _GEOCLAW_M_PER_DEG * coslat
    base_dy_m = (dom_h / ny) * _GEOCLAW_M_PER_DEG
    aoi_w_m = aoi_w * _GEOCLAW_M_PER_DEG * coslat
    aoi_h_m = aoi_h * _GEOCLAW_M_PER_DEG

    # (2) NESTED AMR: grow levels toward the run-up target, stop at the budget /
    #     min-resolution / max-levels guard.
    req = max(1, int(requested_amr_levels or 1))
    levels = 1
    est_finest_cells = (aoi_w_m / base_dx_m) * (aoi_h_m / base_dy_m)
    for level in range(1, _GEOCLAW_MAX_AMR_LEVELS + 1):
        product = _geoclaw_refinement_product(level)
        fx_m = base_dx_m / product
        fy_m = base_dy_m / product
        finest_m = min(fx_m, fy_m)
        finest_cells = (aoi_w_m / fx_m) * (aoi_h_m / fy_m)
        if finest_m < _GEOCLAW_MIN_FINEST_M or finest_cells > _GEOCLAW_FINEST_CELL_BUDGET:
            # The NEXT level would be too fine / too costly -- keep the current.
            break
        levels = level
        est_finest_cells = finest_cells
        if finest_m <= _GEOCLAW_TARGET_FINEST_M and level >= min(
            req, _GEOCLAW_MAX_AMR_LEVELS
        ):
            # Reached the run-up target AND satisfied the request -- stop.
            break

    # (3) INTERMEDIATE propagation tier: the worker FORCES the whole offshore
    #     domain to ``propagation_level`` so the shoaling wave is resolved over the
    #     corridor + shelf, not damped on the base grid. Estimate its whole-domain
    #     cell count (base_cells * product^2) for telemetry + the budget story.
    propagation_level = _geoclaw_propagation_level(levels)
    prop_product = _geoclaw_refinement_product(propagation_level)
    est_prop_domain_cells = int(round((nx * prop_product) * (ny * prop_product)))

    return (
        (nx, ny),
        levels,
        int(round(est_finest_cells)),
        propagation_level,
        est_prop_domain_cells,
    )


def _dem_uri_to_local(dem_uri: str) -> tuple[str, bool]:
    """Resolve a DEM URI to a local readable path for rasterio sampling.

    ``file://`` / bare local paths are returned as-is; ``s3://`` is downloaded to
    a temp file via the SAME boto3 client the solver dispatch uses (no new
    client). Returns ``(path, is_temp)`` so the caller can clean up a temp copy.
    Raises on an unreachable / unsupported URI (the caller degrades to a geometric
    fallback).
    """
    import tempfile as _tf

    if dem_uri.startswith("file://"):
        return dem_uri[len("file://"):], False
    if "://" not in dem_uri:
        return dem_uri, False
    if dem_uri.startswith("s3://"):
        from ..tools.solver import _get_s3_client, _split_object_uri

        _scheme, bucket, key = _split_object_uri(dem_uri)
        s3 = _get_s3_client()
        fd, path = _tf.mkstemp(suffix=".tif", prefix="grace2_geoclaw_bathy_")
        os.close(fd)
        resp = s3.get_object(Bucket=bucket, Key=key)
        import shutil as _shutil

        with open(path, "wb") as fh:
            _shutil.copyfileobj(resp["Body"], fh)
        return path, True
    raise GeoClawWorkflowError(
        "GEOCLAW_STAGING_FAILED",
        message=f"cannot sample bathymetry from unsupported DEM URI scheme: {dem_uri!r}",
    )


def resolve_offshore_source(
    dem_uri: str,
    domain_bbox: tuple[float, float, float, float],
    aoi_bbox: tuple[float, float, float, float],
    requested_source: tuple[float, float] | None,
) -> tuple[float, float] | None:
    """Resolve the Okada source to an OFFSHORE, over-deep-water point.

    Reads the fetched topo/bathy DEM (rasterio) over ``domain_bbox`` and:

      1. Honors ``requested_source`` when it falls inside the domain AND over
         GENUINELY-DEEP water (elevation < ``_SOURCE_DEEP_ELEV_M``); a requested
         source over a shallow shelf puddle is NOT honoured (P1.1) -- it would
         seed the wave over a negligible water column.
      2. Else projects the source onto the DEEPEST cell, PREFERRING genuinely-deep
         water (elevation < ``_SOURCE_DEEP_ELEV_M``) SEAWARD of the AOI and inset
         off the domain boundary, then any deep water, then (only if the domain
         has no genuinely-deep water at all) the deepest below-waterline cell.

    Returns the resolved ``(lon, lat)``, or ``None`` when the DEM has no
    below-waterline cell in the domain (a fully-dry/inland domain -- the caller
    then keeps the requested source / honest fallback and logs it). Best-effort:
    any read error returns ``None`` rather than raising (the run still proceeds
    with the requested source).
    """
    path = None
    is_temp = False
    try:
        import numpy as np  # noqa: WPS433 - agent venv
        import rasterio  # noqa: WPS433

        path, is_temp = _dem_uri_to_local(dem_uri)
        with rasterio.open(path) as ds:
            band = ds.read(1, masked=True).astype("float64")
            transform = ds.transform
        height, width = band.shape
        if height < 2 or width < 2:
            return None

        cols = np.arange(width)
        rows = np.arange(height)
        lons = transform.c + transform.a * (cols + 0.5)
        lats = transform.f + transform.e * (rows + 0.5)  # transform.e < 0
        lon_grid, lat_grid = np.meshgrid(lons, lats)

        valid = ~np.ma.getmaskarray(band)
        elev = band.filled(1.0e9)
        wet = valid & (elev < _SOURCE_WET_ELEV_M)
        # Genuinely-deep water (P1.1): the preferred substrate for the source.
        deep = valid & (elev < _SOURCE_DEEP_ELEV_M)

        # DEM-EDGE inset (issue #9): the resolved source must sit safely INSIDE the
        # fetched DEM, not at its very rim -- the final computational domain is then
        # grown to ENCLOSE the source with a margin (finalize_geoclaw_domain), and
        # GeoClaw aborts if the domain reaches past the topo coverage. So the
        # whole-DEM fallback masks below pick the deepest cell at least
        # ``_SOURCE_DEM_EDGE_INSET_DEG`` in from the raster edge, leaving room for
        # that margin (and keeping the source off the absorbing domain boundary).
        dem_w = float(lon_grid.min())
        dem_e = float(lon_grid.max())
        dem_s = float(lat_grid.min())
        dem_n = float(lat_grid.max())
        mx = max(_SOURCE_DEM_EDGE_INSET_DEG, 0.05 * (dem_e - dem_w))
        my = max(_SOURCE_DEM_EDGE_INSET_DEG, 0.05 * (dem_n - dem_s))
        dem_inset = (
            (lon_grid > dem_w + mx)
            & (lon_grid < dem_e - mx)
            & (lat_grid > dem_s + my)
            & (lat_grid < dem_n - my)
        )

        # (1) Honor a requested source ONLY when it sits over genuinely-deep water
        #     (a requested shallow-shelf source is relocated to deep water below).
        if requested_source is not None:
            rlon, rlat = float(requested_source[0]), float(requested_source[1])
            col = int((rlon - transform.c) / transform.a)
            row = int((rlat - transform.f) / transform.e)
            if 0 <= row < height and 0 <= col < width and deep[row, col]:
                return (rlon, rlat)

        if not wet.any():
            return None

        # (2) Inset off the domain boundary (avoid the absorbing edge).
        d_min_lon, d_min_lat, d_max_lon, d_max_lat = (float(v) for v in domain_bbox)
        ix = 0.08 * (d_max_lon - d_min_lon)
        iy = 0.08 * (d_max_lat - d_min_lat)
        inset = (
            (lon_grid > d_min_lon + ix)
            & (lon_grid < d_max_lon - ix)
            & (lat_grid > d_min_lat + iy)
            & (lat_grid < d_max_lat - iy)
        )
        a_min_lon, a_min_lat, a_max_lon, a_max_lat = (float(v) for v in aoi_bbox)
        outside_aoi = ~(
            (lon_grid >= a_min_lon)
            & (lon_grid <= a_max_lon)
            & (lat_grid >= a_min_lat)
            & (lat_grid <= a_max_lat)
        )

        def _deepest(mask: "np.ndarray") -> tuple[float, float] | None:
            if not mask.any():
                return None
            masked_elev = np.where(mask, elev, 1.0e9)
            idx = np.unravel_index(int(np.argmin(masked_elev)), masked_elev.shape)
            return (float(lon_grid[idx]), float(lat_grid[idx]))

        # Prefer GENUINELY-DEEP water (< _SOURCE_DEEP_ELEV_M) seaward of the AOI +
        # inset off the boundary, then any inset deep water, then any DEM-inset deep
        # water; only if the domain has NO genuinely-deep water fall back to the
        # deepest below-waterline cell (seaward+inset -> inset -> DEM-inset) --
        # never the onshore centroid (P1.1). The whole-DEM fallbacks are
        # ``dem_inset``-bounded (issue #9) so the source stays inside the fetched
        # topo coverage with room for the domain-enclosing margin.
        for mask in (
            deep & inset & outside_aoi,
            deep & inset,
            deep & dem_inset,
            wet & inset & outside_aoi,
            wet & inset,
            wet & dem_inset,
        ):
            pt = _deepest(mask)
            if pt is not None:
                return pt
        return None
    except Exception as exc:  # noqa: BLE001 - best-effort; degrade to fallback
        logger.warning(
            "resolve_offshore_source: bathymetry sampling failed (%s); keeping "
            "the requested source",
            exc,
        )
        return None
    finally:
        if is_temp and path:
            try:
                os.unlink(path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Domain <-> source coordination (issue #9: source-outside-domain inundation
# blocker).
#
# ``plan_geoclaw_domain`` sizes the domain from the AOI (+ any USER source), and
# ``resolve_offshore_source`` then finds the deepest cell in the fetched bathymetry
# -- which spans FURTHER offshore than that AOI-sized domain. So the resolved deep
# source can land OUTSIDE the computational domain (live Crescent City: source
# -124.527 vs domain west -124.34, ~15 km out). GeoClaw's Okada dtopo only deforms
# cells INSIDE the domain, so a source outside it injects ~no water -> zero
# inundation (Total mass byte-identical to a no-source run).
#
# ``finalize_geoclaw_domain`` closes the loop: AFTER the source is resolved over
# deep in-DEM water, it RE-SIZES the domain to ENCLOSE that source (reusing
# ``plan_geoclaw_domain``'s source-enclosing path), CLAMPS the domain to the fetched
# DEM's coverage (so the topo still covers the whole domain), and then ASSERTS the
# invariant -- source strictly inside the final domain AND the domain still spans
# the AOI coast -- failing loudly (GEOCLAW_SOURCE_OUTSIDE_DOMAIN) if a future
# domain/source drift breaks it. Same guardrail philosophy as the flat-ocean gate.
# --------------------------------------------------------------------------- #
def _dem_bounds_and_sample(
    dem_uri: str, lon: float, lat: float
) -> tuple[tuple[float, float, float, float] | None, float | None]:
    """Read the DEM's (west, south, east, north) lon/lat bounds + the elevation at
    ``(lon, lat)`` in a single rasterio open. Best-effort: any failure -> (None,
    None) so the caller degrades (the run proceeds, failing loudly downstream)."""
    path = None
    is_temp = False
    try:
        import numpy as np  # noqa: WPS433 - agent venv
        import rasterio  # noqa: WPS433

        path, is_temp = _dem_uri_to_local(dem_uri)
        with rasterio.open(path) as ds:
            b = ds.bounds
            bounds = (float(b.left), float(b.bottom), float(b.right), float(b.top))
            elev: float | None = None
            try:
                row, col = ds.index(lon, lat)
                if 0 <= row < ds.height and 0 <= col < ds.width:
                    val = ds.read(
                        1,
                        window=((row, row + 1), (col, col + 1)),
                        masked=True,
                    )
                    if not bool(np.ma.getmaskarray(val).all()):
                        elev = float(val.filled(np.nan).ravel()[0])
            except Exception:  # noqa: BLE001 - sampling is best-effort
                elev = None
        return bounds, elev
    except Exception as exc:  # noqa: BLE001 - best-effort; degrade to no clamp
        logger.warning(
            "_dem_bounds_and_sample: could not read DEM bounds for %s (%s)",
            dem_uri,
            exc,
        )
        return None, None
    finally:
        if is_temp and path:
            try:
                os.unlink(path)
            except OSError:
                pass


def finalize_geoclaw_domain(
    aoi_bbox: tuple[float, float, float, float],
    scenario: str,
    source_lonlat: tuple[float, float] | None,
    dem_uri: str,
) -> tuple[float, float, float, float]:
    """Re-size the computational domain to ENCLOSE the resolved offshore source.

    Issue #9 fix. Called AFTER ``resolve_offshore_source`` placed the Okada source
    over deep in-DEM water. Returns the FINAL ``(min_lon, min_lat, max_lon,
    max_lat)`` computational domain such that the invariant holds:

      - ``source_lon`` in ``(domain_xlower, domain_xupper)`` and ``source_lat`` in
        ``(domain_ylower, domain_yupper)`` -- the Okada deformation now happens
        INSIDE the box the solver integrates; and
      - the domain still spans the AOI coast (``domain`` covers ``aoi_bbox``); and
      - the domain stays within the fetched DEM's coverage (GeoClaw aborts if the
        topo does not cover the domain).

    For ``dam_break`` / ``surge`` (domain == AOI, internal/uniform source) or when
    no source was resolved, the AOI-sized ``plan_geoclaw_domain`` result is returned
    unchanged (no regression).

    Raises ``GeoClawWorkflowError('GEOCLAW_SOURCE_OUTSIDE_DOMAIN')`` if the
    invariant cannot be satisfied (e.g. a drift that plants the source outside the
    DEM coverage) -- a loud, named failure rather than a silent zero-inundation run.
    """
    if str(scenario) not in GEOCLAW_OFFSHORE_SCENARIOS or source_lonlat is None:
        return plan_geoclaw_domain(aoi_bbox, scenario, source_lonlat)

    slon, slat = float(source_lonlat[0]), float(source_lonlat[1])
    # Grow the domain to enclose [AOI, source] with a margin (plan_geoclaw_domain's
    # source-enclosing path: domain west <= source_lon - buf, etc.).
    d0, d1, d2, d3 = plan_geoclaw_domain(aoi_bbox, scenario, source_lonlat)

    # Clamp to the fetched DEM's coverage so the topo still covers the domain.
    bounds, src_elev = _dem_bounds_and_sample(dem_uri, slon, slat)
    if bounds is not None:
        bw, bs, be, bn = bounds
        d0 = max(d0, bw)
        d1 = max(d1, bs)
        d2 = min(d2, be)
        d3 = min(d3, bn)

    # --- Invariant assertion (the guardrail) -------------------------------
    a0, a1, a2, a3 = (float(v) for v in aoi_bbox)
    eps = 1.0e-9
    in_domain = (d0 < slon < d2) and (d1 < slat < d3)
    covers_aoi = (
        d0 <= a0 + eps and d1 <= a1 + eps and d2 >= a2 - eps and d3 >= a3 - eps
    )
    if not (in_domain and covers_aoi):
        raise GeoClawWorkflowError(
            "GEOCLAW_SOURCE_OUTSIDE_DOMAIN",
            message=(
                "GeoClaw domain/source inconsistency: resolved Okada source "
                f"({slon:.4f}, {slat:.4f}) not strictly inside the final domain "
                f"({d0:.4f}, {d1:.4f}, {d2:.4f}, {d3:.4f}) covering AOI "
                f"{tuple(round(v, 4) for v in aoi_bbox)} -- the seafloor "
                "deformation would fall outside the integrated box (no wave)."
            ),
            details={
                "source_lonlat": [slon, slat],
                "domain_bbox": [d0, d1, d2, d3],
                "aoi_bbox": list(aoi_bbox),
                "source_in_domain": in_domain,
                "domain_covers_aoi": covers_aoi,
                "dem_bounds": list(bounds) if bounds else None,
            },
        )

    # Depth sanity (warn, not fatal): the source SHOULD sit over genuinely-deep
    # water; resolve_offshore_source already prefers it, but surface a drift.
    if src_elev is not None and src_elev >= _SOURCE_DEEP_ELEV_M:
        logger.warning(
            "finalize_geoclaw_domain: source (%.4f, %.4f) sits over %.1f m "
            "(shallower than the %.0f m deep-water floor); wave may be weak",
            slon,
            slat,
            src_elev,
            _SOURCE_DEEP_ELEV_M,
        )

    logger.info(
        "finalize_geoclaw_domain: domain=(%.4f, %.4f, %.4f, %.4f) encloses source "
        "(%.4f, %.4f) depth=%s + spans AOI %s",
        d0,
        d1,
        d2,
        d3,
        slon,
        slat,
        (f"{src_elev:.1f} m" if src_elev is not None else "n/a"),
        tuple(round(v, 4) for v in aoi_bbox),
    )
    return (d0, d1, d2, d3)


# --------------------------------------------------------------------------- #
# CRS alignment — reproject the topo/bathy DEM to EPSG:4326 (lon/lat) for GeoClaw.
#
# GeoClaw's tsunami solve runs in SPHERICAL lat/lon (``geo_data.coordinate_system
# = 2``); the computational domain (clawdata lower/upper) is authored in lon/lat
# DEGREES. But the coastal topo/bathy merge (``fetch_topobathy``) emits a
# PROJECTED-METRES COG (a fixed UTM zone, e.g. EPSG:32616 with bounds in millions
# of metres) -- and that zone can even be the WRONG one for the AOI's longitude.
# A projected-metres topo extent has ZERO overlap with a lon/lat domain, so GeoClaw
# aborts before any timestep with "topo arrays do not cover domain (area of overlap
# = 0.0)" -> zero fort.q frames -> GEOCLAW_BATCH_OUTPUT_MISSING -> zero inundation.
#
# We reproject the staged DEM from WHATEVER source CRS to EPSG:4326 here (so even
# the wrong-UTM-zone artifact cannot reintroduce a mismatch -- the reprojection is
# driven by the COG's own georeferencing, which round-trips back to correct lon/lat
# regardless of which zone was chosen). This ALSO fixes ``resolve_offshore_source``,
# which samples the DEM treating its affine as lon/lat -- garbage on a UTM grid.
#
# GeoClaw-SPECIFIC: the SFINCS / SWAN paths keep their projected CRS; only this
# lane reprojects.
# --------------------------------------------------------------------------- #
def reproject_dem_to_4326(dem_uri: str, *, run_id: str | None = None) -> str:
    """Reproject a topo/bathy DEM to EPSG:4326 and re-stage it; return the new URI.

    Reads the DEM (``file://`` / local / ``s3://``) with rasterio:

      - If it has no CRS, or is ALREADY EPSG:4326, returns ``dem_uri`` unchanged
        (nothing to do -- the topo is lon/lat already).
      - Else reprojects every band to EPSG:4326 (bilinear; nodata preserved) into
        a fresh GeoTIFF and re-stages it: an ``s3://`` source is re-uploaded to the
        cache bucket (returning the new ``s3://`` URI); a local/``file://`` source
        (tests) is written next to a temp file and returned as ``file://``.

    Best-effort: ANY failure (unreachable URI, synthetic test URI, rasterio error)
    returns ``dem_uri`` unchanged + logs, so the run still proceeds (it will then
    fail loudly downstream with the honest GeoClaw overlap message rather than here).
    """
    src_local: str | None = None
    src_is_temp = False
    out_path: str | None = None
    try:
        import tempfile as _tf

        import rasterio  # noqa: WPS433 - agent venv
        from rasterio.warp import (  # noqa: WPS433
            Resampling,
            calculate_default_transform,
            reproject,
        )

        src_local, src_is_temp = _dem_uri_to_local(dem_uri)
        with rasterio.open(src_local) as src:
            src_crs = src.crs
            if src_crs is None:
                logger.warning(
                    "reproject_dem_to_4326: DEM %s has no CRS; assuming lon/lat, "
                    "leaving it unchanged",
                    dem_uri,
                )
                return dem_uri
            if src_crs.to_epsg() == 4326:
                return dem_uri

            dst_crs = "EPSG:4326"
            transform, width, height = calculate_default_transform(
                src_crs, dst_crs, src.width, src.height, *src.bounds
            )
            kwargs = src.meta.copy()
            kwargs.update(
                driver="GTiff",
                crs=dst_crs,
                transform=transform,
                width=width,
                height=height,
            )
            fd, out_path = _tf.mkstemp(
                suffix=".tif", prefix="grace2_geoclaw_topo4326_"
            )
            os.close(fd)
            with rasterio.open(out_path, "w", **kwargs) as dst:
                for i in range(1, src.count + 1):
                    reproject(
                        source=rasterio.band(src, i),
                        destination=rasterio.band(dst, i),
                        src_transform=src.transform,
                        src_crs=src_crs,
                        dst_transform=transform,
                        dst_crs=dst_crs,
                        resampling=Resampling.bilinear,
                    )
            new_bounds = (transform.c, transform.f + transform.e * height,
                          transform.c + transform.a * width, transform.f)

        # Re-stage the reprojected raster by the SAME scheme as the source.
        if dem_uri.startswith("s3://"):
            from ..tools.cache import CACHE_BUCKET, storage_scheme
            from ..tools.solver import _get_s3_client

            scheme = storage_scheme()
            cache_bucket = os.environ.get("GRACE2_CACHE_BUCKET") or CACHE_BUCKET
            rid = run_id or new_ulid()
            key = f"cache/static-30d/geoclaw_setup/{rid}/topo_4326.tif"
            new_uri = f"{scheme}://{cache_bucket}/{key}"
            s3 = _get_s3_client()
            with open(out_path, "rb") as fh:
                s3.put_object(Bucket=cache_bucket, Key=key, Body=fh)
            logger.info(
                "reproject_dem_to_4326: %s (%s) -> %s bounds=%s",
                dem_uri,
                src_crs,
                new_uri,
                tuple(round(v, 4) for v in new_bounds),
            )
            return new_uri

        # Local / file:// source (tests): keep the reprojected file, return it.
        logger.info(
            "reproject_dem_to_4326: %s (%s) -> file://%s bounds=%s",
            dem_uri,
            src_crs,
            out_path,
            tuple(round(v, 4) for v in new_bounds),
        )
        kept = out_path
        out_path = None  # do not unlink in finally; the caller reads it
        return f"file://{kept}"
    except Exception as exc:  # noqa: BLE001 - best-effort; keep the original DEM
        logger.warning(
            "reproject_dem_to_4326: could not reproject %s to EPSG:4326 (%s); "
            "keeping the original DEM",
            dem_uri,
            exc,
        )
        return dem_uri
    finally:
        if src_is_temp and src_local:
            try:
                os.unlink(src_local)
            except OSError:
                pass
        if out_path:
            try:
                os.unlink(out_path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Staging — upload the build_spec manifest + the topo DEM to S3.
# --------------------------------------------------------------------------- #
def stage_geoclaw_manifest(
    run_args: GeoClawRunArgs,
    *,
    dem_uri: str,
    run_id: str | None = None,
    dtopo_uri: str | None = None,
    surge_uri: str | None = None,
    extra_dem_uris: list[str] | None = None,
    base_num_cells: tuple[int, int] = (40, 40),
    domain_bbox: tuple[float, float, float, float] | None = None,
    source_lonlat_override: tuple[float, float] | None = None,
    amr_levels_override: int | None = None,
) -> GeoClawStaging:
    """Stage the GeoClaw ``manifest.json`` (build_spec + input refs) to S3.

    The GeoClaw analogue of ``stage_swmm_manifest``. Mirrors that path EXACTLY
    (no new client): the same ``cache.storage_scheme()`` scheme + the same
    ``tools.solver._get_s3_client()`` boto3 client + the same
    ``GRACE2_CACHE_BUCKET`` staging bucket the SWMM/SFINCS decks upload to.

    The worker downloads the topo DEM (and optional dtopo / surge) listed in
    ``inputs[]`` BY SCHEME and authors the deck from ``build_spec``. ``dem_uri``
    is a cache/runs ``s3://`` URI produced by ``fetch_topobathy`` / ``fetch_dem``
    upstream (it is staged BY REFERENCE — the worker downloads it directly — so we
    do not re-upload the DEM bytes here, only point at them).

    Args:
        run_args: the validated ``GeoClawRunArgs``.
        dem_uri: the ``s3://`` URI of the topo/bathy DEM (ESRI-ASCII topotype-3
            preferred; the worker references it as ``topo.asc``).
        run_id: optional ULID; minted if absent.
        dtopo_uri: optional ``s3://`` URI of a staged dtopo (tsunami scenario).
        surge_uri: optional ``s3://`` URI of a staged surge hydrograph CSV.
        extra_dem_uris: optional ordered (coarse -> fine) list of additional
            topo/bathy DEM ``s3://`` URIs; each is staged BY REFERENCE as
            ``topo_extra_{i}.asc`` and threaded into the build_spec after the
            primary topo so the worker layers them finest-last.
        base_num_cells: the GeoClaw base computational-grid resolution.

    Returns:
        ``GeoClawStaging`` carrying the manifest URI + the build_spec + bbox.

    Raises:
        GeoClawWorkflowError("GEOCLAW_STAGING_FAILED"): the upload could not
            complete (the Batch lane cannot dispatch without a reachable
            manifest — fail loudly, never a silent dead-end).
    """
    from ..tools.cache import CACHE_BUCKET, storage_scheme
    from ..tools.solver import _get_s3_client

    rid = run_id or new_ulid()
    bbox = tuple(run_args.bbox)

    # Stage the DEM BY REFERENCE; the worker downloads it as topo.asc.
    inputs: list[dict[str, str]] = [{"gs_uri": dem_uri, "dest": "topo.asc"}]
    dtopo_dest: str | None = None
    surge_dest: str | None = None
    # Additional topo/bathy tiles (ordered coarse -> fine) staged BY REFERENCE.
    extra_topo_files: list[str] = []
    for i, uri in enumerate(extra_dem_uris or []):
        if not uri:
            continue
        dest = f"topo_extra_{i}.asc"
        inputs.append({"gs_uri": str(uri), "dest": dest})
        extra_topo_files.append(dest)
    if run_args.scenario == "tsunami" and dtopo_uri:
        dtopo_dest = "dtopo.tt3"
        inputs.append({"gs_uri": dtopo_uri, "dest": dtopo_dest})
    if run_args.scenario == "surge" and surge_uri:
        surge_dest = "surge.csv"
        inputs.append({"gs_uri": surge_uri, "dest": surge_dest})

    build_spec = build_geoclaw_build_spec(
        run_args,
        topo_dest="topo.asc",
        dtopo_dest=dtopo_dest,
        surge_dest=surge_dest,
        extra_topo_files=extra_topo_files,
        base_num_cells=base_num_cells,
        domain_bbox=domain_bbox,
        source_lonlat_override=source_lonlat_override,
        amr_levels_override=amr_levels_override,
    )

    scheme = storage_scheme()  # "s3" on AWS (GCP decommissioned)
    cache_bucket = os.environ.get("GRACE2_CACHE_BUCKET") or CACHE_BUCKET
    prefix = f"cache/static-30d/geoclaw_setup/{rid}/"
    manifest_key = f"{prefix}manifest.json"
    manifest_uri = f"{scheme}://{cache_bucket}/{manifest_key}"

    manifest_dict: dict[str, Any] = {
        "inputs": inputs,
        "build_spec": build_spec,
        "outputs": list(GEOCLAW_OUTPUT_GLOBS),
        "geoclaw_args": ["--run-id", rid, "--manifest-uri", manifest_uri],
    }

    try:
        s3 = _get_s3_client()
        s3.put_object(
            Bucket=cache_bucket,
            Key=manifest_key,
            Body=json.dumps(manifest_dict, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        raise GeoClawWorkflowError(
            "GEOCLAW_STAGING_FAILED",
            message=f"failed to stage GeoClaw manifest to {manifest_uri}: {exc}",
            details={"run_id": rid, "manifest_uri": manifest_uri},
        ) from exc

    logger.info(
        "stage_geoclaw_manifest run_id=%s scenario=%s dem=%s -> manifest=%s",
        rid,
        run_args.scenario,
        dem_uri,
        manifest_uri,
    )
    # n_active_cells used only for telemetry + compute-class sizing; the base grid
    # cell count is a coarse proxy (AMR refines it dynamically downstream).
    n_active = int(base_num_cells[0]) * int(base_num_cells[1])
    _dom = build_spec.get("domain_bbox")
    return GeoClawStaging(
        run_id=rid,
        manifest_uri=manifest_uri,
        build_spec=build_spec,
        run_args=run_args,
        bbox=bbox,  # type: ignore[arg-type]
        n_active_cells=n_active,
        staged_inputs=inputs,
        domain_bbox=(tuple(_dom) if _dom else None),  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# GeoClaw solver registration (mirrors register_swmm_solver).
# --------------------------------------------------------------------------- #
def register_geoclaw_solver() -> None:
    """Register ``'geoclaw'`` in ``tools.solver.SOLVER_WORKFLOW_REGISTRY``.

    Mirrors ``register_swmm_solver``. ``run_solver`` only requires the KEY to be
    present to dispatch (the local-docker backend seam routes to
    ``_run_solver_local_docker``). Idempotent ``setdefault`` — safe to call at
    import. The orchestrator ALSO pins this in code via the shared-append line so
    dispatch works regardless of import order. (The registry value is a
    presence-gate only; the local sentinel is used since the AWS Batch arm was
    removed.)
    """
    from ..tools.solver import LOCAL_DOCKER_WORKFLOW_NAME, SOLVER_WORKFLOW_REGISTRY

    SOLVER_WORKFLOW_REGISTRY.setdefault(GEOCLAW_SOLVER_NAME, LOCAL_DOCKER_WORKFLOW_NAME)


# Register at import so ``run_solver(solver='geoclaw')`` is wired wherever this
# module is imported (the composer + the tool wrapper both import it).
register_geoclaw_solver()


# --------------------------------------------------------------------------- #
# GeoClaw LocalSolverSpec -- docker runner for the local-docker backend.
#
# exec_kind="docker": GeoClaw is a Fortran solver that compiles xgeoclaw at
# run time; the trid3nt-local/geoclaw:latest image carries gfortran + the
# full Clawpack 5.14 source tree. The entrypoint takes --run-id + --manifest-uri
# (pointing to the MinIO/S3 staged manifest) and handles all S3 I/O internally.
#
# The MinIO endpoint is injected via -e flags in the docker run command so the
# container's boto3 reaches the local MinIO at 127.0.0.1:9000.
# --------------------------------------------------------------------------- #

#: Default GeoClaw image under local-docker (env GRACE2_GEOCLAW_IMAGE).
DEFAULT_GEOCLAW_IMAGE: str = "trid3nt-local/geoclaw:latest"


def geoclaw_local_spec() -> "Any":
    """Build the GeoClaw LocalSolverSpec for the local-docker backend."""
    import os
    from pathlib import Path
    from ..tools.solver import LOCAL_DOCKER_WORKFLOW_NAME, LocalSolverSpec

    image = os.environ.get("GRACE2_GEOCLAW_IMAGE") or DEFAULT_GEOCLAW_IMAGE
    aws_endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    aws_access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    aws_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    aws_region = os.environ.get("AWS_REGION", "us-east-1")
    runs_bucket = os.environ.get("GRACE2_RUNS_BUCKET", "trid3nt-runs")

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        # args comes from manifest["geoclaw_args"] = ["--run-id", rid, "--manifest-uri", uri]
        # Replace any staging --run-id with the launcher's run_id so container
        # outputs land under the same S3 prefix that the supervisor polls.
        fixed_args = list(args)
        if "--run-id" in fixed_args:
            idx = fixed_args.index("--run-id")
            fixed_args[idx + 1] = run_id
        else:
            fixed_args = ["--run-id", run_id] + fixed_args
        cmd = [
            "docker", "run", "--rm",
            "--name", run_id,
            "--network", "host",
        ]
        # Inject MinIO / S3 credentials so the container reaches the local MinIO
        env_pairs = [
            ("GRACE2_RUNS_BUCKET", runs_bucket),
            ("GRACE2_OBJECT_STORE", "s3"),
            ("GRACE2_GEOCLAW_SCRATCH", "/opt/grace2/work"),
            ("AWS_REGION", aws_region),
            ("PYTHONUNBUFFERED", "1"),
        ]
        if aws_endpoint:
            env_pairs.append(("AWS_ENDPOINT_URL", aws_endpoint))
        if aws_access_key:
            env_pairs.append(("AWS_ACCESS_KEY_ID", aws_access_key))
        if aws_secret_key:
            env_pairs.append(("AWS_SECRET_ACCESS_KEY", aws_secret_key))
        for k, v in env_pairs:
            cmd += ["-e", f"{k}={v}"]
        cmd.append(image)
        cmd.extend(fixed_args)
        return cmd

    return LocalSolverSpec(
        solver=GEOCLAW_SOLVER_NAME,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="geoclaw_args",
        build_argv=build_argv,
        stdout_name="geoclaw.stdout",
        stderr_name="geoclaw.stderr",
        stdout_uri_field="geoclaw_stdout_uri",
        stderr_uri_field="geoclaw_stderr_uri",
        exec_kind="docker",
        classify_exit=None,
    )


def register_geoclaw_local_spec() -> None:
    """Register the GeoClaw LocalSolverSpec factory for the local-docker backend."""
    from ..tools.solver import register_local_solver_spec
    register_local_solver_spec(GEOCLAW_SOLVER_NAME, geoclaw_local_spec)


# Register at import so run_solver(solver='geoclaw') with
# GRACE2_SOLVER_BACKEND=local-docker dispatches to the docker spec.
register_geoclaw_local_spec()
