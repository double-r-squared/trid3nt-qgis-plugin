"""Landlab component-chain runner — the documented snap-together engine core.

Sprint-17 — NEW engine. This is the worker-side numerical core: given a DEM
``(H, W)`` array + its georegistration + a build_spec dict, build a Landlab
``RasterModelGrid`` and run the documented component chain, returning the output
field ``(H, W)`` array + a typed result dict.

Two documented chains (mirroring the canonical Landlab tutorials, NOT a bespoke
pipeline):

  * ``landslide_probability`` — the infinite-slope landslide stability chain:
      RasterModelGrid(DEM as ``topographic__elevation``)
        -> FlowAccumulator (computes ``topographic__steepest_slope`` +
           ``drainage_area``; the steepest-descent slope is mapped into the
           ``topographic__slope`` field + drainage_area into
           ``topographic__specific_contributing_area`` — the EXACT input fields
           the LandslideProbability API reads)
        -> LandslideProbability (Monte-Carlo relative-wetness + probability of
           failure, driven by ``topographic__slope`` +
           ``topographic__specific_contributing_area`` + the soil__ fields:
           transmissivity, saturated_hydraulic_conductivity, density,
           internal_friction_angle, thickness, and the triangular cohesion
           triple {mode,minimum,maximum}_total_cohesion).
    Output field = ``landslide__probability_of_failure`` (probability in [0, 1])
    with a co-computed factor-of-safety field for the narration scalar.

  * ``overland_flow`` — the de Almeida (2012) shallow-water rainfall chain:
      RasterModelGrid(DEM as ``topographic__elevation``)
        -> set boundary outlet
        -> OverlandFlow, stepped over the storm duration with a rainfall pulse
    Output field = peak ``surface_water__depth`` (m) over the storm.

This module isolates the Landlab-dependent numerics so:
  - the worker ``entrypoint.py`` stays a thin S3-IN -> RUN -> S3-OUT shim, and
  - the chain is independently unit-testable on a tiny in-memory DEM (when
    landlab is installed) or mockable (when it is not).

Landlab is a LAZY import (only when a chain actually runs) so importing this
module for arg-assembly / mocking never requires landlab in the environment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

LOG = logging.getLogger("trid3nt.worker.landlab.chain")

#: A cell whose probability-of-failure is at/above this is flagged "unstable"
#: for the ``unstable_area_fraction`` narration scalar (Landlab tutorial uses
#: 0.75 as a high-susceptibility cutoff).
UNSTABLE_PROBABILITY_THRESHOLD: float = 0.75

#: A factor-of-safety at/below this is "at failure" (the infinite-slope FoS=1
#: stability boundary; <= 1 means the driving stress meets/exceeds resistance).
FOS_FAILURE_THRESHOLD: float = 1.0

#: A surface-water depth (m) at/above this is "wet" for the overland-flow
#: unstable/inundated fraction (matches the flood NODATA_DEPTH_M wet floor).
OVERLAND_WET_DEPTH_M: float = 0.05

#: Green-Ampt demo soil parameters (labeled demo values, not SSURGO-calibrated).
#: Saturated hydraulic conductivity (m/s) driving the infiltration rate; a
#: sandy-loam K. Initial soil moisture content (volumetric fraction) sets the
#: moisture deficit. soil_type selects Landlab's tabulated capillary-head +
#: porosity for the class.
DEFAULT_GREEN_AMPT_K_M_S: float = 1.0e-5
DEFAULT_INITIAL_SOIL_MOISTURE: float = 0.15
DEFAULT_GREEN_AMPT_SOIL_TYPE: str = "sandy loam"

#: The Green-Ampt chain caps the OverlandFlow timestep to storm_duration /
#: this many steps so the infiltration dynamics are resolved (the CFL step for
#: a thin sheet is huge and would infiltrate the whole storm in one leap).
GREEN_AMPT_MIN_STEPS: int = 40

#: Default HAND lowland cutoff (m): cells within this height above the nearest
#: drainage are the near-channel / flood-prone lowland fraction narration scalar.
DEFAULT_HAND_LOWLAND_THRESHOLD_M: float = 5.0

#: A fill depth (m) at/above this counts a cell as "filled" for the pit-fill
#: conditioning fraction (below it is numerical noise from the fill incline).
FILL_DEPTH_EPS_M: float = 1.0e-3

#: Lake-mapping discrimination floors: a mapped depression is a REAL lake only if
#: its deepest point is at least this many metres deep AND its surface area is at
#: least this many m^2. Below either floor the depression is DEM noise (a
#: stair-step pit in a coarse DEM, an upland saddle micro-pit) and is dropped from
#: every lake output. 10000 m^2 is ~11 cells at 30 m -- below a mappable lake.
DEFAULT_MIN_LAKE_DEPTH_M: float = 1.0
DEFAULT_MIN_LAKE_AREA_M2: float = 10_000.0

#: Default channel-head drainage-area threshold, expressed as a MULTIPLE of the
#: grid CELL AREA (a channel head is conventionally reached once the contributing
#: area exceeds a few hundred cells). Cells whose drainage_area meets this are the
#: extracted channel network. A pure multiple of cell area so the threshold scales
#: with resolution (matches the Landlab FlowAccumulator tutorial's
#: drainage-area-threshold channel extraction).
DEFAULT_CHANNEL_THRESHOLD_CELLS: int = 100

#: The flow-routing directors compared in the routing-comparison output (the
#: FlowAccumulator tutorial's central question: how much does the routing choice
#: change where concentrated flow ends up). Each is run through the
#: PriorityFloodFlowRouter (uniform depression handling) with its mapped
#: flow_metric so the comparison is fair across directors.
_ROUTING_COMPARISON_DIRECTORS: tuple[str, ...] = ("D8", "Dinf", "MFD")

#: Short registry director token -> Landlab FlowDirector class name (D8 default).
_DIRECTOR_MAP: dict[str, str] = {
    "D8": "FlowDirectorD8",
    "Dinf": "FlowDirectorDINF",
    "MFD": "FlowDirectorMFD",
}

#: Short registry director token -> PriorityFloodFlowRouter ``flow_metric`` name
#: (the priority-flood router speaks flow_metric, not director class names; MFD
#: maps onto its multiple-flow "Quinn" metric).
_PF_METRIC_MAP: dict[str, str] = {
    "D8": "D8",
    "Dinf": "Dinf",
    "MFD": "Quinn",
}


@dataclass
class ChainResult:
    """The output of a Landlab component chain.

    ``field`` is the ``(H, W)`` output raster (probability of failure for the
    landslide chain; peak surface-water depth for the overland chain) with
    inactive/closed-boundary cells set to NaN. The scalars are the typed
    narration numbers the agent cites (computed with plain numpy arithmetic — no
    LLM, invariant 1).
    """

    field: Any  # numpy (H, W) float array, NaN where inactive
    analysis: str
    unstable_area_fraction: float
    min_factor_of_safety: float
    mean_probability_of_failure: float
    # which output field name the chain produced (for the COG band metadata).
    output_field_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    #: levers STEP 3 -- the additional grids the chain ALREADY computes but the
    #: pre-STEP-3 worker discarded (drainage_area / slope / relative_wetness /
    #: discharge / factor_of_safety). Each is a (H, W) NaN-masked float array
    #: keyed by a STABLE token the agent maps onto an OutputQuantitySpec:
    #: {"drainage_area", "slope", "relative_wetness", "discharge",
    #: "factor_of_safety"}. Empty for a chain that does not compute a given
    #: field (e.g. overland_flow has discharge but no factor_of_safety). The
    #: entrypoint writes each as its own COG so the agent can publish it.
    secondary_fields: dict[str, Any] = field(default_factory=dict)


def run_component_chain(
    dem: Any,
    *,
    resolution_m: float,
    build_spec: dict[str, Any],
) -> ChainResult:
    """Build a RasterModelGrid from ``dem`` and run the build_spec's chain.

    Args:
        dem: a numpy ``(H, W)`` float array of elevations (metres); NaN / the
            ``nodata`` sentinel marks no-data cells (set as closed boundaries).
        resolution_m: the grid cell size in metres (the DEM's projected-metres
            resolution after resampling).
        build_spec: the run parameters. Keys consumed:
            ``analysis`` ("landslide_probability" | "overland_flow"),
            and the per-chain parameters (see ``LandlabRunArgs``).

    Returns:
        A :class:`ChainResult` with the output field + narration scalars.

    Raises:
        ValueError: an unknown ``analysis`` (honest typed error, never a silent
            wrong field).
        ImportError: landlab is not installed in the runtime (the worker image
            pip-installs it; surfaced honestly).
    """
    analysis = str(build_spec.get("analysis", "landslide_probability"))
    if analysis == "landslide_probability":
        return _run_landslide_probability(dem, resolution_m, build_spec)
    if analysis == "overland_flow":
        return _run_overland_flow(dem, resolution_m, build_spec)
    if analysis == "flow_accumulation":
        return _run_flow_accumulation(dem, resolution_m, build_spec)
    if analysis == "green_ampt_overland_flow":
        return _run_green_ampt_overland_flow(dem, resolution_m, build_spec)
    if analysis == "landslide_storm_ensemble":
        return _run_landslide_storm_ensemble(dem, resolution_m, build_spec)
    if analysis == "overland_flow_timeseries":
        return _run_overland_flow_timeseries(dem, resolution_m, build_spec)
    if analysis == "dem_pit_fill":
        return _run_dem_pit_fill(dem, resolution_m, build_spec)
    if analysis == "lake_mapping":
        return _run_lake_mapping(dem, resolution_m, build_spec)
    if analysis == "hacks_law":
        return _run_hacks_law(dem, resolution_m, build_spec)
    if analysis == "hand":
        return _run_hand(dem, resolution_m, build_spec)
    raise ValueError(
        f"unknown Landlab analysis {analysis!r} (expected one of "
        "'landslide_probability', 'overland_flow', 'flow_accumulation', "
        "'green_ampt_overland_flow', 'landslide_storm_ensemble', "
        "'overland_flow_timeseries', 'dem_pit_fill', 'lake_mapping', "
        "'hacks_law', 'hand')"
    )


def _build_grid(dem: Any, resolution_m: float) -> tuple[Any, Any, Any]:
    """Build a Landlab ``RasterModelGrid`` carrying ``topographic__elevation``.

    No-data cells (NaN) are set as CLOSED boundaries so they are excluded from
    the active set (and end up NaN in the output field). Returns ``(grid,
    nodata_mask, z)`` where ``nodata_mask`` is the ``(H, W)`` boolean of no-data
    cells and ``z`` is the elevation node-field handle. Lazy-imports landlab.
    """
    import numpy as np
    from landlab import RasterModelGrid  # type: ignore

    arr = np.asarray(dem, dtype="float64")
    nrows, ncols = arr.shape
    nodata_mask = ~np.isfinite(arr)

    grid = RasterModelGrid((nrows, ncols), xy_spacing=float(resolution_m))
    # Fill no-data with the finite minimum so the component math never sees NaN;
    # those cells are closed boundaries and are re-masked to NaN on output.
    filled = arr.copy()
    if nodata_mask.any():
        finite = arr[~nodata_mask]
        fill_val = float(finite.min()) if finite.size else 0.0
        filled[nodata_mask] = fill_val
    z = grid.add_field("topographic__elevation", filled.ravel(), at="node")
    # Close no-data nodes so the active mesh excludes them.
    if nodata_mask.any():
        grid.status_at_node[nodata_mask.ravel()] = grid.BC_NODE_IS_CLOSED
    LOG.info(
        "landlab grid built: %dx%d cells @ %.2f m (%d no-data closed)",
        nrows,
        ncols,
        resolution_m,
        int(nodata_mask.sum()),
    )
    return grid, nodata_mask, z


def _run_landslide_probability(
    dem: Any, resolution_m: float, spec: dict[str, Any]
) -> ChainResult:
    """The infinite-slope landslide-stability chain.

    FlowAccumulator (slope + drainage area) -> LandslideProbability (Monte-Carlo
    relative wetness + probability of failure + factor of safety). Mirrors the
    canonical Landlab LandslideProbability tutorial.
    """
    import numpy as np
    from landlab.components import FlowAccumulator, LandslideProbability  # type: ignore

    grid, nodata_mask, _z = _build_grid(dem, resolution_m)
    nrows, ncols = np.asarray(dem).shape

    # FlowAccumulator computes ``topographic__steepest_slope`` + ``drainage_area``
    # (the D8 steepest descent slope + contributing area). LandslideProbability,
    # however, reads its slope from the grid field ``topographic__slope`` (tan
    # theta) — NOT ``topographic__steepest_slope``. We therefore map the
    # FlowAccumulator-computed steepest slope into the ``topographic__slope``
    # field the component actually consumes (per the Landlab LandslideProbability
    # API: required input fields are ``topographic__slope`` +
    # ``topographic__specific_contributing_area`` + the soil__ fields).
    # levers STEP 3: advanced_physics["flow_director"] selects the flow-routing
    # director (D8 default; Dinf / MFD per the registry). build_spec carries the
    # ALREADY-VALIDATED resolved value (or the default). ``_DIRECTOR_MAP``
    # (module-level) maps the short registry token onto the Landlab director name.
    director = _DIRECTOR_MAP.get(
        str(spec.get("flow_director", "D8")), "FlowDirectorD8"
    )
    fa = FlowAccumulator(
        grid,
        flow_director=director,
        depression_finder="DepressionFinderAndRouter",
    )
    fa.run_one_step()

    # Map steepest-descent slope (tan theta) -> the ``topographic__slope`` field
    # LandslideProbability reads. (FlowDirectorD8 writes
    # ``topographic__steepest_slope`` as the gradient/tan of the steepest link.)
    grid.add_field(
        "topographic__slope",
        np.asarray(grid.at_node["topographic__steepest_slope"], dtype="float64"),
        at="node",
        clobber=True,
    )

    # Specific contributing area = drainage_area / cell_width — the per-unit
    # -contour-length area the infinite-slope wetness term needs. Landlab's
    # LandslideProbability reads grid field ``topographic__specific_contributing_area``.
    cell_width = float(resolution_m)
    spec_area = grid.at_node["drainage_area"] / max(cell_width, 1e-9)
    grid.add_field(
        "topographic__specific_contributing_area",
        spec_area,
        at="node",
        clobber=True,
    )

    # Soil parameter fields (uniform demo values broadcast to every node). These
    # are the EXACT input fields the LandslideProbability API documents:
    # soil__transmissivity, soil__saturated_hydraulic_conductivity, soil__density,
    # soil__internal_friction_angle, soil__thickness, and the triangular-cohesion
    # triple soil__{mode,minimum,maximum}_total_cohesion (the component draws
    # cohesion per Monte-Carlo iteration from a triangular dist between min/max
    # about the mode, so all three must be present).
    n_nodes = grid.number_of_nodes
    transmissivity = float(spec.get("soil_transmissivity_m2_day", 20.0))
    sat_hyd_cond = float(spec.get("soil_saturated_hydraulic_conductivity_m_day", 10.0))
    cohesion_pa = float(spec.get("soil_cohesion_pa", 10_000.0))
    # Half-width of the triangular cohesion distribution about the mode (Pa). The
    # component requires distinct min/max-total-cohesion fields; default to +/-25%
    # of the mode (clamped >= 0) so the min is never negative.
    cohesion_scatter_pa = float(
        spec.get("soil_cohesion_scatter_pa", 0.25 * cohesion_pa)
    )
    cohesion_min_pa = max(cohesion_pa - cohesion_scatter_pa, 0.0)
    cohesion_max_pa = cohesion_pa + cohesion_scatter_pa
    friction_deg = float(spec.get("soil_internal_friction_deg", 35.0))
    density = float(spec.get("soil_density_kg_m3", 2000.0))
    thickness = float(spec.get("soil_thickness_m", 1.0))
    recharge_mm_day = float(spec.get("recharge_mm_day", 30.0))
    n_mc = int(spec.get("n_monte_carlo", 250))

    grid.add_field(
        "soil__transmissivity",
        np.full(n_nodes, transmissivity),
        at="node",
        clobber=True,
    )
    grid.add_field(
        "soil__saturated_hydraulic_conductivity",
        np.full(n_nodes, sat_hyd_cond),
        at="node",
        clobber=True,
    )
    grid.add_field(
        "soil__mode_total_cohesion",
        np.full(n_nodes, cohesion_pa),
        at="node",
        clobber=True,
    )
    grid.add_field(
        "soil__minimum_total_cohesion",
        np.full(n_nodes, cohesion_min_pa),
        at="node",
        clobber=True,
    )
    grid.add_field(
        "soil__maximum_total_cohesion",
        np.full(n_nodes, cohesion_max_pa),
        at="node",
        clobber=True,
    )
    grid.add_field(
        "soil__internal_friction_angle",
        np.full(n_nodes, friction_deg),
        at="node",
        clobber=True,
    )
    grid.add_field(
        "soil__density",
        np.full(n_nodes, density),
        at="node",
        clobber=True,
    )
    grid.add_field(
        "soil__thickness",
        np.full(n_nodes, thickness),
        at="node",
        clobber=True,
    )

    # Uniform recharge distribution for the Monte-Carlo wetness draws (mm/day).
    ls = LandslideProbability(
        grid,
        number_of_iterations=n_mc,
        groundwater__recharge_distribution="uniform",
        groundwater__recharge_min_value=max(recharge_mm_day * 0.5, 0.0),
        groundwater__recharge_max_value=recharge_mm_day * 1.5,
    )
    ls.calculate_landslide_probability()

    prob = np.asarray(
        grid.at_node["landslide__probability_of_failure"], dtype="float64"
    ).reshape(nrows, ncols)
    # Factor of safety: Landlab exposes the mean relative wetness + FS via the
    # component; the single-value FS field is ``soil__mean_relative_wetness`` and
    # ``landslide__probability_of_failure``. Derive a representative FoS proxy
    # from the infinite-slope relation on the steepest slope where the component
    # does not expose a direct FS grid. We use the component's exposed
    # probability for the unstable fraction and compute a deterministic FoS field
    # from slope + cohesion for the min-FoS narration scalar.
    fos = _infinite_slope_factor_of_safety(
        grid,
        nrows,
        ncols,
        cohesion_pa=cohesion_pa,
        friction_deg=friction_deg,
        density=density,
        thickness=thickness,
    )

    # Re-mask closed / no-data cells to NaN on every output field.
    prob[nodata_mask] = np.nan
    fos[nodata_mask] = np.nan

    # levers STEP 3: collect the additional grids the chain already computed
    # (the pre-STEP-3 worker discarded these). Each is reshaped to (H, W) and
    # NaN-masked on the closed/no-data cells, ready for its own COG.
    def _grid2d(node_field: str) -> Any:
        try:
            a = np.asarray(grid.at_node[node_field], dtype="float64").reshape(
                nrows, ncols
            )
        except Exception:  # noqa: BLE001 - a field the run did not populate
            return None
        a[nodata_mask] = np.nan
        return a

    secondary: dict[str, Any] = {}
    da = _grid2d("drainage_area")
    if da is not None:
        secondary["drainage_area"] = da
    sl = _grid2d("topographic__slope")
    if sl is not None:
        secondary["slope"] = sl
    rw = _grid2d("soil__mean_relative_wetness")
    if rw is not None:
        secondary["relative_wetness"] = rw
    secondary["factor_of_safety"] = fos

    active = np.isfinite(prob)
    n_active = int(active.sum())
    if n_active == 0:
        unstable_frac = 0.0
        mean_pof = 0.0
        min_fos = 0.0
    else:
        unstable_frac = float(
            np.count_nonzero(prob[active] >= UNSTABLE_PROBABILITY_THRESHOLD)
            / n_active
        )
        mean_pof = float(np.nanmean(prob[active]))
        finite_fos = fos[np.isfinite(fos)]
        min_fos = float(np.min(finite_fos)) if finite_fos.size else 0.0

    LOG.info(
        "landlab landslide chain: n_active=%d unstable_frac=%.4f mean_pof=%.4f "
        "min_fos=%.4f",
        n_active,
        unstable_frac,
        mean_pof,
        min_fos,
    )
    return ChainResult(
        field=prob,
        analysis="landslide_probability",
        unstable_area_fraction=unstable_frac,
        min_factor_of_safety=min_fos,
        mean_probability_of_failure=mean_pof,
        output_field_name="landslide__probability_of_failure",
        extra={"factor_of_safety_field": fos},
        secondary_fields=secondary,
    )


def _infinite_slope_factor_of_safety(
    grid: Any,
    nrows: int,
    ncols: int,
    *,
    cohesion_pa: float,
    friction_deg: float,
    density: float,
    thickness: float,
) -> Any:
    """Deterministic dry infinite-slope factor of safety per node.

    FoS = (C' + (rho_s - m*rho_w) g z cos^2(theta) tan(phi)) /
          (rho_s g z sin(theta) cos(theta))
    with m=0 (dry) for the deterministic min-FoS narration scalar (the
    probabilistic wetness is handled by the Monte-Carlo component above). This is
    the textbook infinite-slope relation; with the demo wetness set to dry it is
    a conservative-upper-bound FoS field whose MIN over the AOI is the narration
    scalar. Slope comes from ``topographic__slope`` (tan of the slope angle) —
    the same field LandslideProbability consumes (mapped from the FlowAccumulator
    steepest-descent slope by the caller), so the deterministic FoS and the
    Monte-Carlo probability act on identical slopes.
    """
    import numpy as np

    g = 9.81
    slope_tan = np.asarray(
        grid.at_node["topographic__slope"], dtype="float64"
    ).reshape(nrows, ncols)
    # Clamp tiny/zero slopes so flat cells do not produce a divide-by-zero
    # (a flat cell is trivially stable -> a large FoS).
    slope_tan = np.where(slope_tan > 1e-4, slope_tan, np.nan)
    theta = np.arctan(slope_tan)
    sin_t = np.sin(theta)
    cos_t = np.cos(theta)
    tan_phi = np.tan(np.radians(friction_deg))

    resisting = cohesion_pa + (density * g * thickness * cos_t * cos_t * tan_phi)
    driving = density * g * thickness * sin_t * cos_t
    with np.errstate(divide="ignore", invalid="ignore"):
        fos = resisting / driving
    # Flat cells (slope NaN) -> trivially stable; leave NaN so they don't drag
    # the min, then those are also nodata-masked by the caller.
    return fos


def _run_overland_flow(
    dem: Any, resolution_m: float, spec: dict[str, Any]
) -> ChainResult:
    """The de Almeida (2012) shallow-water overland-flow rainfall chain.

    Steps OverlandFlow over the storm duration with a uniform rainfall pulse and
    reports the PEAK ``surface_water__depth`` (m) per cell. Mirrors the canonical
    Landlab OverlandFlow tutorial.
    """
    import numpy as np
    from landlab.components import OverlandFlow  # type: ignore

    grid, nodata_mask, _z = _build_grid(dem, resolution_m)
    nrows, ncols = np.asarray(dem).shape

    grid.add_zeros("surface_water__depth", at="node", clobber=True)

    intensity_mm_hr = float(spec.get("rainfall_intensity_mm_hr", 50.0))
    duration_hr = float(spec.get("storm_duration_hr", 2.0))
    rain_ms = intensity_mm_hr / 1000.0 / 3600.0  # mm/hr -> m/s
    duration_s = duration_hr * 3600.0

    # levers STEP 3: advanced_physics overland_alpha / mannings_n override the
    # OverlandFlow stability coefficient + roughness (build_spec carries the
    # validated resolved values; absent => Landlab defaults, byte-identical).
    of_kwargs: dict[str, Any] = {"steep_slopes": True}
    if spec.get("mannings_n") is not None:
        of_kwargs["mannings_n"] = float(spec["mannings_n"])
    if spec.get("overland_alpha") is not None:
        of_kwargs["alpha"] = float(spec["overland_alpha"])
    of = OverlandFlow(grid, **of_kwargs)

    peak = np.zeros(grid.number_of_nodes, dtype="float64")
    peak_q = np.zeros(grid.number_of_nodes, dtype="float64")
    elapsed = 0.0
    # Bounded step budget so a pathological AOI cannot loop forever.
    max_steps = int(spec.get("max_overland_steps", 2000))
    steps = 0
    while elapsed < duration_s and steps < max_steps:
        of.dt = min(of.calc_time_step(), max(duration_s - elapsed, 1e-3))
        grid.at_node["surface_water__depth"][grid.core_nodes] += (
            rain_ms * of.dt
        )
        of.overland_flow()
        depth = np.asarray(grid.at_node["surface_water__depth"], dtype="float64")
        peak = np.maximum(peak, depth)
        # levers STEP 3: track the peak per-NODE discharge magnitude. OverlandFlow
        # carries ``surface_water__discharge`` on LINKS (m^2/s per unit width);
        # map the max-incident-link magnitude to each node for a per-cell raster.
        try:
            q_link = np.abs(
                np.asarray(grid.at_link["surface_water__discharge"], dtype="float64")
            )
            q_node = grid.map_max_of_node_links_to_node(q_link)
            peak_q = np.maximum(peak_q, np.asarray(q_node, dtype="float64"))
        except Exception:  # noqa: BLE001 - discharge field/mapping unavailable
            pass
        elapsed += of.dt
        steps += 1

    peak_grid = peak.reshape(nrows, ncols)
    peak_grid[nodata_mask] = np.nan

    secondary: dict[str, Any] = {}
    if np.any(peak_q > 0.0):
        q_grid = peak_q.reshape(nrows, ncols)
        q_grid[nodata_mask] = np.nan
        secondary["discharge"] = q_grid

    active = np.isfinite(peak_grid)
    n_active = int(active.sum())
    if n_active == 0:
        wet_frac = 0.0
        max_depth = 0.0
    else:
        wet_frac = float(
            np.count_nonzero(peak_grid[active] >= OVERLAND_WET_DEPTH_M) / n_active
        )
        max_depth = float(np.nanmax(peak_grid[active]))

    LOG.info(
        "landlab overland chain: steps=%d wet_frac=%.4f max_depth=%.4f m",
        steps,
        wet_frac,
        max_depth,
    )
    # min_factor_of_safety carries the peak depth (the layer units disambiguate);
    # mean_probability_of_failure is 0 for the overland chain (no PoF).
    return ChainResult(
        field=peak_grid,
        analysis="overland_flow",
        unstable_area_fraction=wet_frac,
        min_factor_of_safety=max_depth,
        mean_probability_of_failure=0.0,
        output_field_name="surface_water__depth",
        extra={"max_depth_m": max_depth, "n_steps": steps},
        secondary_fields=secondary,
    )


def _accumulate_drainage_area(
    dem: Any,
    resolution_m: float,
    *,
    director_token: str,
    depression_handler: str,
) -> tuple[Any, Any, Any, dict[str, Any]]:
    """Run flow accumulation on a FRESH grid and return the drainage-area field.

    Builds its own ``RasterModelGrid`` (so repeated calls for the routing
    comparison never share accumulated state), routes flow with the requested
    director + depression handling, and returns
    ``(drainage_area_2d, slope_2d, nodata_mask, notes)``:

      * ``depression_handler="priority_flood"`` (the folded row-9 component): the
        Landlab ``PriorityFloodFlowRouter`` fills/breaches depressions and
        accumulates in one pass, with ``flow_metric`` mapped from the director
        token (D8 / Dinf / MFD->Quinn). The whole domain routes to the edge.
      * ``depression_handler="fill"`` (default): ``FlowAccumulator`` with the
        requested ``FlowDirector``; ``DepressionFinderAndRouter`` is added ONLY
        for the D8 director (Landlab's depression router is single-flow-only),
        and the note records when a multi-flow director ran without it.

    All grids are ``(H, W)`` NaN-masked on the closed / no-data cells.
    """
    import numpy as np

    grid, nodata_mask, _z = _build_grid(dem, resolution_m)
    nrows, ncols = np.asarray(dem).shape
    notes: dict[str, Any] = {}

    if depression_handler == "priority_flood":
        from landlab.components import PriorityFloodFlowRouter  # type: ignore

        metric = _PF_METRIC_MAP.get(director_token, "D8")
        pf = PriorityFloodFlowRouter(grid, flow_metric=metric)
        pf.run_one_step()
        notes["depression_handler"] = "priority_flood"
        notes["flow_metric"] = metric
    else:
        from landlab.components import FlowAccumulator  # type: ignore

        director = _DIRECTOR_MAP.get(director_token, "FlowDirectorD8")
        fa_kwargs: dict[str, Any] = {"flow_director": director}
        if director == "FlowDirectorD8":
            fa_kwargs["depression_finder"] = "DepressionFinderAndRouter"
            notes["depression_handler"] = "fill (DepressionFinderAndRouter)"
        else:
            # Landlab's DepressionFinderAndRouter is single-flow-only; a multi-flow
            # director (Dinf / MFD) cannot use it. Run without it and say so.
            notes["depression_handler"] = (
                f"none ({director_token} is multi-flow; fill is D8-only)"
            )
        fa = FlowAccumulator(grid, **fa_kwargs)
        fa.run_one_step()

    def _grid2d(node_field: str) -> Any:
        try:
            a = np.asarray(grid.at_node[node_field], dtype="float64").reshape(
                nrows, ncols
            )
        except Exception:  # noqa: BLE001 - a field the run did not populate
            return None
        a[nodata_mask] = np.nan
        return a

    drainage = _grid2d("drainage_area")
    slope = _grid2d("topographic__steepest_slope")
    return drainage, slope, nodata_mask, notes


def _run_flow_accumulation(
    dem: Any, resolution_m: float, spec: dict[str, Any]
) -> ChainResult:
    """The FlowAccumulator drainage-area + channel-network chain.

    Mirrors the canonical Landlab ``the_FlowAccumulator`` tutorial: route flow
    over the DEM, accumulate contributing drainage area, and extract the channel
    network by a drainage-area threshold. The PRIMARY field is ``drainage_area``
    (m^2, log-styled downstream); the channel network is a secondary boolean
    mask (vectorized by the postprocess); and a routing-comparison summary
    (D8 vs Dinf vs MFD, all through the priority-flood router) answers the
    tutorial's central question -- how much the routing choice moves the
    concentrated flow paths.

    build_spec keys consumed: ``flow_director`` (D8 / Dinf / MFD),
    ``depression_handler`` (fill / priority_flood), ``channel_threshold_cells``.
    """
    import numpy as np

    cell_area = float(resolution_m) ** 2
    director_token = str(spec.get("flow_director", "D8"))
    if director_token not in _DIRECTOR_MAP:
        director_token = "D8"
    depression_handler = str(spec.get("depression_handler", "fill")).lower()
    if depression_handler not in ("fill", "priority_flood"):
        depression_handler = "fill"
    threshold_cells = int(
        spec.get("channel_threshold_cells", DEFAULT_CHANNEL_THRESHOLD_CELLS)
    )
    threshold_cells = max(threshold_cells, 1)
    threshold_m2 = threshold_cells * cell_area

    # --- Primary run (the user's director + depression handling) ---
    drainage, slope, nodata_mask, notes = _accumulate_drainage_area(
        dem,
        resolution_m,
        director_token=director_token,
        depression_handler=depression_handler,
    )
    if drainage is None:
        raise ValueError(
            "flow_accumulation produced no drainage_area field "
            f"(director={director_token}, depression_handler={depression_handler})"
        )

    # Channel network: cells whose contributing area meets the threshold.
    channel_mask = np.where(drainage >= threshold_m2, 1.0, np.nan)
    channel_mask[nodata_mask] = np.nan

    secondary: dict[str, Any] = {"channel_network": channel_mask}
    if slope is not None:
        secondary["slope"] = slope

    active = np.isfinite(drainage)
    n_active = int(active.sum())
    if n_active == 0:
        max_da_km2 = 0.0
        mean_da_km2 = 0.0
        channelized_frac = 0.0
    else:
        da_active = drainage[active]
        max_da_km2 = float(np.max(da_active)) / 1e6
        mean_da_km2 = float(np.mean(da_active)) / 1e6
        channelized_frac = float(
            np.count_nonzero(da_active >= threshold_m2) / n_active
        )

    # --- Routing comparison: D8 vs Dinf vs MFD (all priority-flood routed) ---
    # Cheap (flow accumulation is fast); each director run through the
    # priority-flood router so depression handling is uniform and the ONLY
    # difference is the routing metric -- exactly the tutorial's comparison.
    routing_comparison: list[dict[str, Any]] = []
    for tok in _ROUTING_COMPARISON_DIRECTORS:
        try:
            da_c, _slope_c, mask_c, _notes_c = _accumulate_drainage_area(
                dem,
                resolution_m,
                director_token=tok,
                depression_handler="priority_flood",
            )
        except Exception as exc:  # noqa: BLE001 - a director the venv cannot route
            LOG.warning("routing-comparison director %s failed: %s", tok, exc)
            continue
        if da_c is None:
            continue
        act_c = np.isfinite(da_c)
        na_c = int(act_c.sum())
        if na_c == 0:
            continue
        dav = da_c[act_c]
        routing_comparison.append(
            {
                "flow_director": tok,
                "max_drainage_area_km2": float(np.max(dav)) / 1e6,
                "channelized_area_fraction": float(
                    np.count_nonzero(dav >= threshold_m2) / na_c
                ),
            }
        )

    LOG.info(
        "landlab flow_accumulation chain: director=%s depression=%s "
        "n_active=%d max_da=%.4g km2 channelized_frac=%.4f (%d comparison rows)",
        director_token,
        depression_handler,
        n_active,
        max_da_km2,
        channelized_frac,
        len(routing_comparison),
    )
    # Contract carrier reuse: unstable_area_fraction := channelized fraction;
    # min_factor_of_safety := max drainage area (km2, units disambiguate);
    # mean_probability_of_failure is unused for this chain (0.0). The typed
    # flow-accumulation scalars travel in ``extra`` (folded into the worker
    # result block) so the postprocess emits the drainage-area layer metrics.
    return ChainResult(
        field=drainage,
        analysis="flow_accumulation",
        unstable_area_fraction=channelized_frac,
        min_factor_of_safety=max_da_km2,
        mean_probability_of_failure=0.0,
        output_field_name="drainage_area",
        extra={
            "max_drainage_area_km2": max_da_km2,
            "mean_drainage_area_km2": mean_da_km2,
            "channelized_area_fraction": channelized_frac,
            "channel_threshold_cells": threshold_cells,
            "channel_threshold_m2": threshold_m2,
            "flow_director": director_token,
            "depression_handler_note": notes.get("depression_handler", ""),
            "routing_comparison": routing_comparison,
        },
        secondary_fields=secondary,
    )


def _run_green_ampt_overland_flow(
    dem: Any, resolution_m: float, spec: dict[str, Any]
) -> ChainResult:
    """The Green-Ampt infiltration + de Almeida overland-flow partition chain.

    Mirrors the canonical Landlab
    ``infilt_green_ampt_with_overland_flow`` tutorial: step ``OverlandFlow``
    over a design storm while ``SoilInfiltrationGreenAmpt`` removes infiltrated
    water from the surface sheet each step. Reports where the storm PARTITIONS
    into infiltration vs runoff:

      * PRIMARY field = ``soil_water_infiltration__depth`` (m): cumulative
        infiltrated depth per cell.
      * SECONDARY field ``runoff_depth`` (m): the rainfall EXCESS per cell
        (total rainfall depth - infiltration depth, clamped >= 0) -- the depth
        that became runoff (where runoff initiates).

    The narration scalars (domain infiltrated/runoff fraction + means) travel in
    ``extra`` (folded into the worker result block ``green_ampt``).

    build_spec keys consumed: ``rainfall_intensity_mm_hr``, ``storm_duration_hr``,
    ``soil_hydraulic_conductivity_m_s``, ``initial_soil_moisture_content``,
    ``green_ampt_soil_type``.
    """
    import numpy as np
    from landlab.components import OverlandFlow, SoilInfiltrationGreenAmpt  # type: ignore

    grid, nodata_mask, _z = _build_grid(dem, resolution_m)
    nrows, ncols = np.asarray(dem).shape

    # Green-Ampt requires strictly-positive surface + infiltration depth fields
    # to seed the wetting-front math (a zero sheet has no water to infiltrate);
    # a 1e-8 m floor is the tutorial's convention.
    grid.add_zeros("surface_water__depth", at="node", clobber=True)
    grid.at_node["surface_water__depth"] += 1e-8
    grid.add_zeros("soil_water_infiltration__depth", at="node", clobber=True)
    grid.at_node["soil_water_infiltration__depth"] += 1e-8

    intensity_mm_hr = float(spec.get("rainfall_intensity_mm_hr", 90.0))
    duration_hr = float(spec.get("storm_duration_hr", 0.5))
    k_m_s = float(spec.get("soil_hydraulic_conductivity_m_s", DEFAULT_GREEN_AMPT_K_M_S))
    theta_i = float(
        spec.get("initial_soil_moisture_content", DEFAULT_INITIAL_SOIL_MOISTURE)
    )
    soil_type = str(spec.get("green_ampt_soil_type", DEFAULT_GREEN_AMPT_SOIL_TYPE))

    rain_ms = intensity_mm_hr / 1000.0 / 3600.0  # mm/hr -> m/s
    duration_s = duration_hr * 3600.0

    of = OverlandFlow(grid, steep_slopes=True)
    si = SoilInfiltrationGreenAmpt(
        grid,
        hydraulic_conductivity=k_m_s,
        initial_soil_moisture_content=theta_i,
        soil_type=soil_type,
    )

    # Cap the OverlandFlow step so the storm is resolved over >= GREEN_AMPT_MIN_STEPS
    # substeps (the thin-sheet CFL step is otherwise huge).
    dt_cap = duration_s / float(GREEN_AMPT_MIN_STEPS)
    elapsed = 0.0
    steps = 0
    max_steps = int(spec.get("max_overland_steps", 2000))
    while elapsed < duration_s and steps < max_steps:
        dt = min(of.calc_time_step(), dt_cap, max(duration_s - elapsed, 1e-3))
        of.dt = dt
        grid.at_node["surface_water__depth"][grid.core_nodes] += rain_ms * dt
        of.overland_flow()
        si.run_one_step(dt)
        elapsed += dt
        steps += 1

    infil = np.asarray(
        grid.at_node["soil_water_infiltration__depth"], dtype="float64"
    ).reshape(nrows, ncols)
    infil[nodata_mask] = np.nan

    rain_total_m = rain_ms * duration_s
    # Rainfall excess = the depth that ran off (total rainfall - infiltration),
    # clamped at 0 (a cell that infiltrated more than it received got run-on).
    runoff = np.clip(rain_total_m - infil, 0.0, None)
    runoff[nodata_mask] = np.nan

    secondary: dict[str, Any] = {"runoff_depth": runoff}

    active = np.isfinite(infil)
    n_active = int(active.sum())
    if n_active == 0 or rain_total_m <= 0.0:
        mean_infil_m = 0.0
        mean_runoff_m = 0.0
        infiltrated_frac = 0.0
        runoff_frac = 0.0
    else:
        mean_infil_m = float(np.mean(infil[active]))
        mean_runoff_m = float(np.mean(runoff[active]))
        infiltrated_frac = min(1.0, mean_infil_m / rain_total_m)
        runoff_frac = min(1.0, mean_runoff_m / rain_total_m)

    LOG.info(
        "landlab green_ampt chain: steps=%d rain=%.1f mm infil_frac=%.3f "
        "runoff_frac=%.3f mean_infil=%.4f m",
        steps,
        rain_total_m * 1000.0,
        infiltrated_frac,
        runoff_frac,
        mean_infil_m,
    )
    # Contract carrier reuse: unstable_area_fraction := runoff fraction (the
    # runoff-generating share of the storm); min_factor_of_safety := mean
    # infiltration depth (m, units disambiguate); mean_probability_of_failure
    # unused (0.0). The typed partition scalars travel in ``extra``.
    return ChainResult(
        field=infil,
        analysis="green_ampt_overland_flow",
        unstable_area_fraction=runoff_frac,
        min_factor_of_safety=mean_infil_m,
        mean_probability_of_failure=0.0,
        output_field_name="soil_water_infiltration__depth",
        extra={
            "total_rainfall_mm": rain_total_m * 1000.0,
            "mean_infiltration_mm": mean_infil_m * 1000.0,
            "mean_runoff_mm": mean_runoff_m * 1000.0,
            "infiltrated_fraction": infiltrated_frac,
            "runoff_fraction": runoff_frac,
            "rainfall_intensity_mm_hr": intensity_mm_hr,
            "storm_duration_hr": duration_hr,
            "soil_hydraulic_conductivity_m_s": k_m_s,
            "green_ampt_soil_type": soil_type,
            "n_steps": steps,
        },
        secondary_fields=secondary,
    )


# --------------------------------------------------------------------------- #
# landslide_storm_ensemble: the infinite-slope LandslideProbability chain swept
# across storm/recharge scenarios (PrecipitationDistribution draws).
# --------------------------------------------------------------------------- #
def _set_landslide_soil_fields(grid: Any, spec: dict[str, Any]) -> None:
    """Broadcast the LandslideProbability soil input fields onto every node.

    Sets the EXACT fields the component reads (transmissivity, saturated hydraulic
    conductivity, density, internal-friction angle, thickness, and the triangular
    cohesion triple), from the build_spec demo values.
    """
    import numpy as np

    n_nodes = grid.number_of_nodes
    transmissivity = float(spec.get("soil_transmissivity_m2_day", 20.0))
    sat_hyd_cond = float(spec.get("soil_saturated_hydraulic_conductivity_m_day", 10.0))
    cohesion_pa = float(spec.get("soil_cohesion_pa", 10_000.0))
    cohesion_scatter_pa = float(spec.get("soil_cohesion_scatter_pa", 0.25 * cohesion_pa))
    cohesion_min_pa = max(cohesion_pa - cohesion_scatter_pa, 0.0)
    cohesion_max_pa = cohesion_pa + cohesion_scatter_pa
    friction_deg = float(spec.get("soil_internal_friction_deg", 35.0))
    density = float(spec.get("soil_density_kg_m3", 2000.0))
    thickness = float(spec.get("soil_thickness_m", 1.0))
    for name, values in (
        ("soil__transmissivity", np.full(n_nodes, transmissivity)),
        ("soil__saturated_hydraulic_conductivity", np.full(n_nodes, sat_hyd_cond)),
        ("soil__mode_total_cohesion", np.full(n_nodes, cohesion_pa)),
        ("soil__minimum_total_cohesion", np.full(n_nodes, cohesion_min_pa)),
        ("soil__maximum_total_cohesion", np.full(n_nodes, cohesion_max_pa)),
        ("soil__internal_friction_angle", np.full(n_nodes, friction_deg)),
        ("soil__density", np.full(n_nodes, density)),
        ("soil__thickness", np.full(n_nodes, thickness)),
    ):
        grid.add_field(name, values, at="node", clobber=True)


def _draw_recharge_scenarios(spec: dict[str, Any]) -> list[float]:
    """Draw ``n_recharge_scenarios`` positive storm depths (mm) from a Poisson
    ``PrecipitationDistribution``; each is one triggering-recharge scenario
    (mm/day pulse). Deterministic (fixed seed). Returns the sorted scenarios."""
    from landlab.components import PrecipitationDistribution  # type: ignore

    n = max(int(spec.get("n_recharge_scenarios", 8)), 2)
    msd = float(spec.get("mean_storm_duration_hr", 2.0))
    mid = float(spec.get("mean_interstorm_duration_hr", 48.0))
    mdd = float(spec.get("mean_storm_depth_mm", 15.0))
    seed = int(spec.get("random_seed", 1234))
    pd = PrecipitationDistribution(
        mean_storm_duration=msd,
        mean_interstorm_duration=mid,
        mean_storm_depth=mdd,
        total_t=max(n, 2) * (msd + mid) * 8.0,
        delta_t=1.0,
        random_seed=seed,
    )
    # A physically meaningful triggering storm: drop degenerate near-zero draws
    # (a Poisson depth ~ 0 mm/day recharge is not a scenario worth sweeping).
    depth_floor = max(1.0, 0.05 * mdd)
    depths: list[float] = []
    guard = 0
    while len(depths) < n and guard < n * 200:
        pd.update()
        d = float(pd.storm_depth)
        if d >= depth_floor:
            depths.append(d)
        guard += 1
    if not depths:
        depths = [mdd]
    return sorted(depths)


def _run_landslide_storm_ensemble(
    dem: Any, resolution_m: float, spec: dict[str, Any]
) -> ChainResult:
    """The infinite-slope landslide chain swept across a storm/recharge ensemble.

    Builds the grid + FlowAccumulator slope/area + soil fields ONCE, then runs the
    Monte-Carlo LandslideProbability component once per recharge scenario (drawn
    from a PrecipitationDistribution). The primary field is the ENSEMBLE-MEAN
    probability of failure; the per-scenario unstable-area fraction table drives
    the susceptibility-vs-recharge sensitivity chart.
    """
    import numpy as np
    from landlab.components import FlowAccumulator, LandslideProbability  # type: ignore

    grid, nodata_mask, _z = _build_grid(dem, resolution_m)
    nrows, ncols = np.asarray(dem).shape

    director = _DIRECTOR_MAP.get(str(spec.get("flow_director", "D8")), "FlowDirectorD8")
    fa = FlowAccumulator(
        grid, flow_director=director, depression_finder="DepressionFinderAndRouter"
    )
    fa.run_one_step()
    grid.add_field(
        "topographic__slope",
        np.asarray(grid.at_node["topographic__steepest_slope"], dtype="float64"),
        at="node",
        clobber=True,
    )
    cell_width = float(resolution_m)
    grid.add_field(
        "topographic__specific_contributing_area",
        grid.at_node["drainage_area"] / max(cell_width, 1e-9),
        at="node",
        clobber=True,
    )
    _set_landslide_soil_fields(grid, spec)

    n_mc = int(spec.get("n_monte_carlo", 250))
    recharges = _draw_recharge_scenarios(spec)

    prob_sum = np.zeros((nrows, ncols), dtype="float64")
    table: list[dict[str, Any]] = []
    for rech in recharges:
        ls = LandslideProbability(
            grid,
            number_of_iterations=n_mc,
            groundwater__recharge_distribution="uniform",
            groundwater__recharge_min_value=max(rech * 0.5, 0.0),
            groundwater__recharge_max_value=rech * 1.5,
        )
        ls.calculate_landslide_probability()
        prob = np.asarray(
            grid.at_node["landslide__probability_of_failure"], dtype="float64"
        ).reshape(nrows, ncols)
        prob_masked = prob.copy()
        prob_masked[nodata_mask] = np.nan
        active = np.isfinite(prob_masked)
        va = prob_masked[active]
        uf = (
            float(np.count_nonzero(va >= UNSTABLE_PROBABILITY_THRESHOLD) / va.size)
            if va.size
            else 0.0
        )
        mp = float(np.mean(va)) if va.size else 0.0
        table.append(
            {
                "recharge_mm_day": round(float(rech), 3),
                "unstable_area_fraction": uf,
                "mean_probability_of_failure": mp,
            }
        )
        prob_sum += np.where(np.isfinite(prob_masked), prob_masked, 0.0)

    mean_field = prob_sum / float(max(len(recharges), 1))
    mean_field[nodata_mask] = np.nan
    active = np.isfinite(mean_field)
    va = mean_field[active]
    unstable_frac = (
        float(np.count_nonzero(va >= UNSTABLE_PROBABILITY_THRESHOLD) / va.size)
        if va.size
        else 0.0
    )
    mean_pof = float(np.mean(va)) if va.size else 0.0

    rs = np.array([t["recharge_mm_day"] for t in table], dtype="float64")
    us = np.array([t["unstable_area_fraction"] for t in table], dtype="float64")
    slope = (
        float(np.polyfit(rs, us, 1)[0])
        if rs.size >= 2 and float(rs.max() - rs.min()) > 0.0
        else 0.0
    )

    LOG.info(
        "landlab storm-ensemble chain: n_scenarios=%d recharge=[%.1f,%.1f] mm/day "
        "unstable_frac=%.4f slope=%.5f",
        len(recharges),
        float(min(recharges)),
        float(max(recharges)),
        unstable_frac,
        slope,
    )
    return ChainResult(
        field=mean_field,
        analysis="landslide_storm_ensemble",
        unstable_area_fraction=unstable_frac,
        min_factor_of_safety=0.0,
        mean_probability_of_failure=mean_pof,
        output_field_name="landslide__probability_of_failure",
        extra={
            "recharge_scenarios": table,
            "min_recharge_mm_day": float(min(recharges)),
            "max_recharge_mm_day": float(max(recharges)),
            "n_recharge_scenarios": len(recharges),
            "sensitivity_slope": slope,
        },
    )


# --------------------------------------------------------------------------- #
# overland_flow_timeseries: the de Almeida OverlandFlow chain sampled at N
# intervals so depth is written frame by frame (time-stepped animation output).
# --------------------------------------------------------------------------- #
#: Upper bound on time-step depth snapshots written by the worker (the composer
#: subsamples again to the animation frame cap; this bounds worker COG count).
_MAX_TIMESERIES_SNAPSHOTS: int = 48


def _run_overland_flow_timeseries(
    dem: Any, resolution_m: float, spec: dict[str, Any]
) -> ChainResult:
    """The de Almeida OverlandFlow chain writing depth at N intervals.

    Steps OverlandFlow over the storm while snapshotting ``surface_water__depth``
    every ``output_interval_s`` (subsampled to ``_MAX_TIMESERIES_SNAPSHOTS``). The
    primary field is the PEAK depth; each snapshot is a secondary field
    ``depth_step_NN`` the composer publishes as an animation frame; the
    depth-vs-time series at the max-depth cell drives the hydrograph chart.

    ``condition_dem`` (OPT-IN, default False) depression-fills the DEM before
    routing. The default routes the RAW DEM (the validated default response); when
    enabled, the storm routes over a pit-filled surface so flow traces connected
    valleys instead of ponding in the DEM's closed depressions. The conditioning
    facts ride the result ``extra`` when enabled.
    """
    import numpy as np
    from landlab.components import OverlandFlow  # type: ignore

    condition_dem = bool(spec.get("condition_dem", False))
    n_depressions_filled = 0
    max_fill_depth_m = 0.0
    if condition_dem:
        dem, n_depressions_filled, max_fill_depth_m = _condition_dem_fill(
            dem, resolution_m, spec
        )

    grid, nodata_mask, _z = _build_grid(dem, resolution_m)
    nrows, ncols = np.asarray(dem).shape
    grid.add_zeros("surface_water__depth", at="node", clobber=True)

    intensity_mm_hr = float(spec.get("rainfall_intensity_mm_hr", 50.0))
    duration_hr = float(spec.get("storm_duration_hr", 2.0))
    rain_ms = intensity_mm_hr / 1000.0 / 3600.0
    duration_s = duration_hr * 3600.0
    interval_s = float(spec.get("output_interval_s", 300.0))
    # Bound the snapshot count: never finer than duration/_MAX so a long storm
    # cannot balloon the frame count.
    interval_s = max(interval_s, duration_s / float(_MAX_TIMESERIES_SNAPSHOTS))

    of = OverlandFlow(grid, steep_slopes=True)

    peak = np.zeros(grid.number_of_nodes, dtype="float64")
    snapshots: list[tuple[float, Any]] = []
    next_snap = interval_s
    elapsed = 0.0
    max_steps = int(spec.get("max_overland_steps", 4000))
    steps = 0
    while elapsed < duration_s and steps < max_steps:
        # Cap the step to the snapshot interval so the animation resolves frame by
        # frame (a near-dry sheet's stable CFL step is otherwise huge and would
        # leap the whole storm in one step, collapsing the time series).
        of.dt = min(
            of.calc_time_step(), interval_s, max(duration_s - elapsed, 1e-3)
        )
        grid.at_node["surface_water__depth"][grid.core_nodes] += rain_ms * of.dt
        of.overland_flow()
        depth = np.asarray(grid.at_node["surface_water__depth"], dtype="float64")
        peak = np.maximum(peak, depth)
        elapsed += of.dt
        steps += 1
        if elapsed >= next_snap:
            snap = depth.reshape(nrows, ncols).copy()
            snap[nodata_mask] = np.nan
            snapshots.append((elapsed, snap))
            next_snap += interval_s
    # Always keep a final snapshot at the storm end.
    final = (
        np.asarray(grid.at_node["surface_water__depth"], dtype="float64")
        .reshape(nrows, ncols)
        .copy()
    )
    final[nodata_mask] = np.nan
    if not snapshots or snapshots[-1][0] < elapsed - 1e-6:
        snapshots.append((elapsed, final))

    peak_grid = peak.reshape(nrows, ncols)
    peak_grid[nodata_mask] = np.nan

    active = np.isfinite(peak_grid)
    n_active = int(active.sum())
    if n_active == 0:
        wet_frac = 0.0
        max_depth = 0.0
        max_node_flat = 0
    else:
        wet_frac = float(
            np.count_nonzero(peak_grid[active] >= OVERLAND_WET_DEPTH_M) / n_active
        )
        max_depth = float(np.nanmax(peak_grid))
        max_node_flat = int(np.nanargmax(np.where(active, peak_grid, np.nan)))

    # Depth at the max-depth cell over time + the frame time index.
    series: list[dict[str, float]] = []
    time_to_peak_s = 0.0
    prev_v = -1.0
    for t, snap in snapshots:
        v = float(snap.flat[max_node_flat])
        series.append({"time_s": round(float(t), 2), "depth_m": v})
        if v > prev_v:
            time_to_peak_s = float(t)
            prev_v = v

    secondary: dict[str, Any] = {}
    for i, (_t, snap) in enumerate(snapshots, start=1):
        secondary[f"depth_step_{i:02d}"] = snap

    LOG.info(
        "landlab overland-timeseries chain: steps=%d frames=%d max_depth=%.4f m "
        "interval=%.1fs condition_dem=%s n_filled=%d max_fill=%.3f m",
        steps,
        len(snapshots),
        max_depth,
        interval_s,
        condition_dem,
        n_depressions_filled,
        max_fill_depth_m,
    )
    return ChainResult(
        field=peak_grid,
        analysis="overland_flow_timeseries",
        unstable_area_fraction=wet_frac,
        min_factor_of_safety=max_depth,
        mean_probability_of_failure=0.0,
        output_field_name="surface_water__depth",
        extra={
            "frame_times_s": [round(float(t), 2) for t, _s in snapshots],
            "max_cell_series": series,
            "max_depth_m": max_depth,
            "time_to_peak_s": time_to_peak_s,
            "n_frames": len(snapshots),
            "output_interval_s": interval_s,
            "condition_dem": condition_dem,
            "n_depressions_filled": n_depressions_filled,
            "max_fill_depth_m": max_fill_depth_m,
        },
        secondary_fields=secondary,
    )


# --------------------------------------------------------------------------- #
# dem_pit_fill + lake_mapping: shared LakeMapperBarnes plumbing.
# --------------------------------------------------------------------------- #
def _run_lakemapper(
    dem: Any, resolution_m: float, spec: dict[str, Any]
) -> tuple[Any, Any, Any, Any]:
    """Run FlowAccumulator + LakeMapperBarnes on a fresh grid.

    Returns ``(grid, nodata_mask, lmb, fill_depth_2d)`` where ``fill_depth_2d`` is
    the ``(H, W)`` per-cell fill depth (fill_surface - topographic elevation),
    NaN-masked on closed cells. The LakeMapperBarnes carries the lake properties
    (``lake_map`` / ``lake_depths`` / ``lake_areas`` / ``lake_volumes`` /
    ``number_of_lakes``).
    """
    import numpy as np
    from landlab.components import FlowAccumulator, LakeMapperBarnes  # type: ignore

    grid, nodata_mask, _z = _build_grid(dem, resolution_m)
    nrows, ncols = np.asarray(dem).shape
    grid.add_zeros("water__fill_surface", at="node", clobber=True)
    grid.at_node["water__fill_surface"][:] = grid.at_node["topographic__elevation"]
    fa = FlowAccumulator(grid, flow_director="D8")
    fa.run_one_step()
    fill_flat = bool(spec.get("fill_flat", True))
    lmb = LakeMapperBarnes(
        grid,
        method="D8",
        fill_flat=fill_flat,
        surface="topographic__elevation",
        fill_surface="water__fill_surface",
        redirect_flow_steepest_descent=True,
        reaccumulate_flow=True,
        track_lakes=True,
    )
    lmb.run_one_step()
    fill_depth = (
        grid.at_node["water__fill_surface"] - grid.at_node["topographic__elevation"]
    ).reshape(nrows, ncols)
    fill_depth = np.where(fill_depth > 0.0, fill_depth, 0.0)
    fill_depth[nodata_mask] = np.nan
    return grid, nodata_mask, lmb, fill_depth


def _condition_dem_fill(
    dem: Any, resolution_m: float, spec: dict[str, Any]
) -> tuple[Any, int, float]:
    """Depression-fill a DEM to the LakeMapperBarnes fill surface.

    Returns ``(filled_dem_2d, n_depressions_filled, max_fill_depth_m)``. The
    filled DEM raises every closed depression to a routable surface (the same
    fill machinery ``dem_pit_fill`` / ``lake_mapping`` use) so a rainfall/routing
    chain traces connected flow paths down valleys instead of ponding in the
    DEM's sink pits. No-data cells stay no-data (NaN) so the caller re-closes
    them. Reuses ``_run_lakemapper`` -- one fill implementation, not two.
    """
    import numpy as np

    _grid, _nodata_mask, lmb, fill_depth = _run_lakemapper(dem, resolution_m, spec)
    arr = np.asarray(dem, dtype="float64")
    add = np.where(np.isfinite(fill_depth), fill_depth, 0.0)
    filled = arr + add  # no-data cells are NaN + 0 -> stay NaN
    finite_fill = fill_depth[np.isfinite(fill_depth)]
    max_fill = float(np.max(finite_fill)) if finite_fill.size else 0.0
    n_dep = int(lmb.number_of_lakes)
    return filled, n_dep, max_fill


def _run_dem_pit_fill(
    dem: Any, resolution_m: float, spec: dict[str, Any]
) -> ChainResult:
    """The DEM pit-fill conditioning chain (LakeMapperBarnes).

    The primary field is the per-cell FILL DEPTH (metres the DEM had to rise to
    become routable). Answers where a DEM needed filling before it can route flow.
    """
    import numpy as np

    _grid, _nodata_mask, lmb, fill_depth = _run_lakemapper(dem, resolution_m, spec)

    active = np.isfinite(fill_depth)
    va = fill_depth[active]
    max_fill = float(np.max(va)) if va.size else 0.0
    filled_frac = (
        float(np.count_nonzero(va >= FILL_DEPTH_EPS_M) / va.size) if va.size else 0.0
    )
    n_dep = int(lmb.number_of_lakes)

    LOG.info(
        "landlab dem-pit-fill chain: max_fill=%.3f m filled_frac=%.4f n_depressions=%d",
        max_fill,
        filled_frac,
        n_dep,
    )
    return ChainResult(
        field=fill_depth,
        analysis="dem_pit_fill",
        unstable_area_fraction=filled_frac,
        min_factor_of_safety=max_fill,
        mean_probability_of_failure=0.0,
        output_field_name="dem_fill_depth",
        extra={
            "max_fill_depth_m": max_fill,
            "filled_area_fraction": filled_frac,
            "n_depressions": n_dep,
        },
    )


def _run_lake_mapping(
    dem: Any, resolution_m: float, spec: dict[str, Any]
) -> ChainResult:
    """The lake extent + depth mapping chain (LakeMapperBarnes, lake tracking on).

    The primary field is the per-cell LAKE DEPTH within mapped lakes; the lake
    extent mask is a secondary field the composer vectorizes.

    LakeMapperBarnes maps EVERY closed depression, so on a real DEM most mapped
    "lakes" are noise pits (coarse-DEM stair-step artifacts, upland saddle
    micro-pits) rather than real waterbodies. Two discrimination floors separate
    real lakes from noise: a lake is kept only if its deepest point is at least
    ``min_lake_depth_m`` deep AND its surface area is at least ``min_lake_area_m2``
    -- a depression failing EITHER floor is dropped from the depth field, the
    extent mask, the vector, and the counts. ``n_lakes_raw`` (mapped) vs
    ``n_lakes_kept`` (after filtering) ride the result ``extra`` so the filtering
    is loud.
    """
    import numpy as np

    grid, nodata_mask, lmb, _fill = _run_lakemapper(dem, resolution_m, spec)
    nrows, ncols = np.asarray(dem).shape

    min_lake_depth_m = float(spec.get("min_lake_depth_m", DEFAULT_MIN_LAKE_DEPTH_M))
    min_lake_area_m2 = float(spec.get("min_lake_area_m2", DEFAULT_MIN_LAKE_AREA_M2))
    cell_area = float(resolution_m) ** 2

    lake_map_flat = np.asarray(lmb.lake_map).ravel()  # outlet id per node, -1 if none
    lake_depths_flat = np.asarray(lmb.lake_depths, dtype="float64").ravel()
    outlets = list(lmb.lake_outlets)
    areas = np.asarray(lmb.lake_areas, dtype="float64")
    vols = np.asarray(lmb.lake_volumes, dtype="float64")
    n_lakes_raw = int(lmb.number_of_lakes)

    # Per-lake discrimination: keep a depression only if it clears BOTH floors.
    kept_node_mask = np.zeros(lake_map_flat.shape[0], dtype=bool)
    kept_area_m2 = 0.0
    kept_vol_m3 = 0.0
    n_lakes_kept = 0
    for i, outlet in enumerate(outlets):
        nodes = lake_map_flat == outlet
        if not nodes.any():
            continue
        depths_i = lake_depths_flat[nodes]
        max_depth_i = float(np.max(depths_i)) if depths_i.size else 0.0
        area_i = (
            float(areas[i]) if i < areas.size else float(int(nodes.sum())) * cell_area
        )
        if max_depth_i >= min_lake_depth_m and area_i >= min_lake_area_m2:
            kept_node_mask |= nodes
            n_lakes_kept += 1
            kept_area_m2 += area_i
            kept_vol_m3 += float(vols[i]) if i < vols.size else 0.0

    in_lake = kept_node_mask.reshape(nrows, ncols)
    lake_depth = lake_depths_flat.reshape(nrows, ncols)
    depth = np.where(in_lake, lake_depth, np.nan)
    depth[nodata_mask] = np.nan
    extent = np.where(in_lake, 1.0, np.nan)
    extent[nodata_mask] = np.nan

    total_area_km2 = kept_area_m2 / 1e6
    total_vol_m3 = kept_vol_m3
    fin = depth[np.isfinite(depth)]
    max_lake_depth = float(np.max(fin)) if fin.size else 0.0

    n_active = int(np.count_nonzero(~nodata_mask))
    lake_frac = (
        float(np.count_nonzero(in_lake & ~nodata_mask) / n_active)
        if n_active
        else 0.0
    )

    LOG.info(
        "landlab lake-mapping chain: n_lakes_raw=%d n_lakes_kept=%d "
        "(min_depth=%.2f m min_area=%.0f m2) area=%.4g km2 vol=%.4g m3 "
        "max_depth=%.3f m",
        n_lakes_raw,
        n_lakes_kept,
        min_lake_depth_m,
        min_lake_area_m2,
        total_area_km2,
        total_vol_m3,
        max_lake_depth,
    )
    return ChainResult(
        field=depth,
        analysis="lake_mapping",
        unstable_area_fraction=lake_frac,
        min_factor_of_safety=max_lake_depth,
        mean_probability_of_failure=0.0,
        output_field_name="lake_depth",
        extra={
            "n_lakes": n_lakes_kept,
            "n_lakes_raw": n_lakes_raw,
            "n_lakes_kept": n_lakes_kept,
            "min_lake_depth_m": min_lake_depth_m,
            "min_lake_area_m2": min_lake_area_m2,
            "total_lake_area_km2": total_area_km2,
            "total_lake_volume_m3": total_vol_m3,
            "max_lake_depth_m": max_lake_depth,
        },
        secondary_fields={"lake_extent": extent},
    )


# --------------------------------------------------------------------------- #
# hacks_law: HackCalculator basin length-area scaling diagnostic.
# --------------------------------------------------------------------------- #
def _run_hacks_law(
    dem: Any, resolution_m: float, spec: dict[str, Any]
) -> ChainResult:
    """The Hack's-law basin-scaling diagnostic (HackCalculator).

    Runs FlowAccumulator, fits ``L = C * A**h`` per basin via a ChannelProfiler
    (HackCalculator), and reports the exponent ``h`` for the largest basin. The
    primary field is the log-styled drainage-area backdrop; the largest basin's
    footprint is a secondary mask; the length-vs-area scatter drives the log-log
    chart.
    """
    import numpy as np
    from landlab.components import FlowAccumulator, HackCalculator  # type: ignore

    grid, nodata_mask, _z = _build_grid(dem, resolution_m)
    nrows, ncols = np.asarray(dem).shape
    fa = FlowAccumulator(
        grid, flow_director="D8", depression_finder="DepressionFinderAndRouter"
    )
    fa.run_one_step()
    drainage = np.asarray(grid.at_node["drainage_area"], dtype="float64").reshape(
        nrows, ncols
    )
    drainage[nodata_mask] = np.nan

    exponent = 0.0
    coefficient = 0.0
    largest_area_km2 = 0.0
    n_basins = 0
    scatter: list[dict[str, float]] = []
    basin_outlet: int | None = None
    try:
        hc = HackCalculator(grid, save_full_df=True)
        hc.calculate_hack_parameters()
        df = hc.hack_coefficient_dataframe
        n_basins = int(len(df))
        if n_basins > 0:
            ordered = df.sort_values("A_max")
            row = ordered.iloc[-1]
            exponent = float(row["h"])
            coefficient = float(row["C"])
            largest_area_km2 = float(row["A_max"]) / 1e6
            basin_outlet = int(ordered.index[-1])
            full = hc.full_hack_dataframe
            if full is not None:
                sub = full[full["basin_outlet_id"] == basin_outlet]
                aa = sub["A"].to_numpy(dtype="float64")
                ll = sub["L_obs"].to_numpy(dtype="float64")
                keep = (aa > 0.0) & (ll > 0.0)
                aa = aa[keep]
                ll = ll[keep]
                if aa.size > 400:
                    idx = np.linspace(0, aa.size - 1, 400).round().astype(int)
                    idx = np.unique(idx)
                    aa = aa[idx]
                    ll = ll[idx]
                scatter = [
                    {"area_m2": float(a), "length_m": float(l)}
                    for a, l in zip(aa, ll)
                ]
    except Exception as exc:  # noqa: BLE001 - fit can fail on a channel-poor AOI
        LOG.warning("landlab hacks_law fit failed: %s", exc)

    secondary: dict[str, Any] = {}
    if basin_outlet is not None:
        try:
            from landlab.utils.watershed import get_watershed_mask  # type: ignore

            wmask = get_watershed_mask(grid, basin_outlet).reshape(nrows, ncols)
            basin = np.where(wmask, 1.0, np.nan)
            basin[nodata_mask] = np.nan
            if np.any(np.isfinite(basin)):
                secondary["basin"] = basin
        except Exception as exc:  # noqa: BLE001 - watershed mask unavailable
            LOG.warning("landlab hacks_law basin mask failed: %s", exc)

    LOG.info(
        "landlab hacks_law chain: n_basins=%d exponent=%.4f coefficient=%.4g "
        "largest_area=%.4g km2 scatter_pts=%d",
        n_basins,
        exponent,
        coefficient,
        largest_area_km2,
        len(scatter),
    )
    return ChainResult(
        field=drainage,
        analysis="hacks_law",
        unstable_area_fraction=0.0,
        min_factor_of_safety=largest_area_km2,
        mean_probability_of_failure=0.0,
        output_field_name="drainage_area",
        extra={
            "hack_exponent": exponent,
            "hack_coefficient": coefficient,
            "largest_basin_area_km2": largest_area_km2,
            "n_basins": n_basins,
            "scatter": scatter,
        },
        secondary_fields=secondary,
    )


# --------------------------------------------------------------------------- #
# hand: Height Above Nearest Drainage (Nobre et al. 2011).
# --------------------------------------------------------------------------- #
def _run_hand(dem: Any, resolution_m: float, spec: dict[str, Any]) -> ChainResult:
    """The Height Above Nearest Drainage chain (HeightAboveDrainageCalculator).

    Routes flow (D8), extracts a channel mask by a drainage-area threshold, and
    computes each cell's height above its nearest drainage channel (Nobre et al.
    2011). The primary field is HAND; the channel mask is a secondary the composer
    vectorizes.
    """
    import numpy as np
    from landlab.components import (  # type: ignore
        FlowAccumulator,
        HeightAboveDrainageCalculator,
    )

    grid, nodata_mask, _z = _build_grid(dem, resolution_m)
    nrows, ncols = np.asarray(dem).shape
    fa = FlowAccumulator(
        grid, flow_director="D8", depression_finder="DepressionFinderAndRouter"
    )
    fa.run_one_step()

    cell_area = float(resolution_m) ** 2
    thr_cells = max(
        int(spec.get("channel_threshold_cells", DEFAULT_CHANNEL_THRESHOLD_CELLS)), 1
    )
    thr_m2 = thr_cells * cell_area
    da = np.asarray(grid.at_node["drainage_area"], dtype="float64")
    # HeightAboveDrainageCalculator requires the channel mask as uint8.
    channel_mask = np.zeros(grid.number_of_nodes, dtype="uint8")
    channel_mask[da >= thr_m2] = 1
    if channel_mask.sum() == 0:
        # No cell meets the threshold on a small/low-relief AOI: seed the single
        # highest-accumulation node as the drainage anchor so HAND is defined.
        channel_mask[int(np.argmax(da))] = 1
    grid.add_field("channel__mask", channel_mask, at="node", clobber=True)

    hand = HeightAboveDrainageCalculator(grid, channel_mask="channel__mask")
    hand.run_one_step()
    h = np.asarray(
        grid.at_node["height_above_drainage__elevation"], dtype="float64"
    ).reshape(nrows, ncols)
    h[nodata_mask] = np.nan

    active = np.isfinite(h)
    va = h[active]
    mean_h = float(np.mean(va)) if va.size else 0.0
    max_h = float(np.max(va)) if va.size else 0.0
    lowland_thr = float(
        spec.get("hand_lowland_threshold_m", DEFAULT_HAND_LOWLAND_THRESHOLD_M)
    )
    lowland_frac = (
        float(np.count_nonzero(va <= lowland_thr) / va.size) if va.size else 0.0
    )
    cm2 = channel_mask.reshape(nrows, ncols)
    chan_frac = (
        float(np.count_nonzero((cm2 > 0.0) & active) / int(active.sum()))
        if active.sum()
        else 0.0
    )
    channel_layer = np.where(cm2 > 0.0, 1.0, np.nan)
    channel_layer[nodata_mask] = np.nan

    LOG.info(
        "landlab hand chain: thr_cells=%d mean_hand=%.3f m max_hand=%.3f m "
        "channel_frac=%.4f lowland_frac=%.4f",
        thr_cells,
        mean_h,
        max_h,
        chan_frac,
        lowland_frac,
    )
    return ChainResult(
        field=h,
        analysis="hand",
        unstable_area_fraction=lowland_frac,
        min_factor_of_safety=max_h,
        mean_probability_of_failure=0.0,
        output_field_name="height_above_drainage__elevation",
        extra={
            "mean_hand_m": mean_h,
            "max_hand_m": max_h,
            "channel_area_fraction": chan_frac,
            "lowland_area_fraction": lowland_frac,
            "channel_threshold_cells": thr_cells,
            "hand_lowland_threshold_m": lowland_thr,
        },
        secondary_fields={"channel_network": channel_layer},
    )
