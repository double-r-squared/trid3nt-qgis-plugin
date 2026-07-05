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

LOG = logging.getLogger("grace2.worker.landlab.chain")

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
    raise ValueError(
        f"unknown Landlab analysis {analysis!r} "
        f"(expected 'landslide_probability' or 'overland_flow')"
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
    # ALREADY-VALIDATED resolved value (or the default). FlowDirectorD8/Dinf/MFD
    # are the Landlab director names; map the short registry token onto them.
    _DIRECTOR_MAP = {
        "D8": "FlowDirectorD8",
        "Dinf": "FlowDirectorDINF",
        "MFD": "FlowDirectorMFD",
    }
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
