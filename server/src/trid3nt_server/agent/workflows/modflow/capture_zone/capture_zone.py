"""the composer - MODFLOW PRT capture-zone composer.

The end-to-end higher-order workflow for the MODFLOW
``capture_zone`` and ``wellhead_protection`` archetypes: it turns a place (or
AOI point) + a pumping well location into a rendered capture-zone polygon  -
the zone of contribution delineated by backward particle tracking (MF6 PRT).

Canonical real-world pipeline mirrored here (a wellhead protection area /
zone-of-contribution delineation, the MODFLOW analogue of the EPA WHPA /
ZONEBUDGET approach):

    resolve the AOI point (geocode a place, or take an explicit lat/lon)
        -> the user supplies the well location (NEVER fabricated  -  a missing
           well is a typed USER_INPUT_REQUIRED failure, Invariant 9)
        -> assemble MODFLOWRunArgs(archetype='capture_zone', well, tiers, ...)
        -> run_modflow_archetype_job:
             GWF steady flow solve -> mf6
             -> gwt_adapter.build_and_run_prt_from_gwf (PRT backward tracking)
             -> postprocess_capture_zone (convex-hull isochrones + FlatGeobuf)
        -> CaptureZoneLayerURI (vector polygon + per-tier isochrone areas)

The difference between the two archetypes is framing and default travel-time
tiers only:

    ``capture_zone``       - general zone-of-contribution; defaults [1, 5, 10] yr
    ``wellhead_protection`` - EPA-style fixed-travel-time; defaults [2, 5, 10] yr
                             (EPA WHPA fixed-travel-time approach; SDWA Section
                             1428 / EPA 440/6-87-010 delineation guidance)

Both produce a ``CaptureZoneLayerURI`` (layer_type='vector'), which renders
client-side via the inline-GeoJSON path and the ``presetColorFor('capture_zone')``
violet branch in ``vector_rendering.ts``.

Invariants:
- **1 / 2 / 8: preserve** (typed numbers, deterministic composition, cancellable).
- **9. No fabricated model inputs.** A capture-zone run with no well location
  returns a typed ``USER_INPUT_REQUIRED`` failure -- the CONVEX HULL of
  backtracked pathlines is a physical delineation, not a guess; a missing well
  is never invented.
- **10. Minimal parameter surface: preserves.** The signature exposes intent (the
  place + the well + optional tiers / particle count); the grid, demo aquifer
  K / Sy, and PRT parameters are derived defaults, not user-supplied.

PRECISION CAVEAT (Invariant 1): the polygon is the CONVEX HULL of discrete
backtracked pathlines on a structured 100 m rectilinear grid with DEMO aquifer
parameters, NOT a calibrated regulatory wellhead protection area. The agent must
narrate this caveat when presenting the layer (FR-AS-7).
"""

from __future__ import annotations

import logging
import math
from typing import Any

from pydantic import Field

from trid3nt_contracts.common import GraceModel
from trid3nt_contracts.modflow_contracts import (
    CaptureZoneLayerURI,
    DEFAULT_AQUIFER_K_MS,
    DEFAULT_POROSITY,
    MODFLOWRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    substep,
)
from trid3nt_server.agent.tools import TOOL_REGISTRY, register_tool
from trid3nt_server.agent.workflows.modflow._template_card import TemplateCard
# Reuse the shared archetype-run + AOI-resolve helpers from the sustainable_yield
# composer (one implementation, all archetypes).
from trid3nt_server.agent.workflows.modflow.sustainable_yield.sustainable_yield import (
    _aquifer_overrides,
    _coerce_optional_latlon,
    _resolve_aoi_point,
    _run_archetype,
)

logger = logging.getLogger("trid3nt_server.agent.workflows.modflow.capture_zone.capture_zone")

__all__ = [
    "CaptureZoneResult",
    "model_capture_zone_scenario",
    "modflow_capture_zone",
    "CaptureZoneScenarioError",
    "CaptureZoneInputError",
    "CAPTURE_ZONE_DEFAULT_TIERS",
    "WELLHEAD_PROTECTION_DEFAULT_TIERS",
    "TEMPLATE_CARD",
]

#: Default travel-time isochrone tiers (years) for ``capture_zone``.
#: One, five, and ten years is the common municipal-well zone-of-contribution
#: analysis period (e.g. USEPA Source Water Protection guidance).
CAPTURE_ZONE_DEFAULT_TIERS: list[float] = [1.0, 5.0, 10.0]

#: Default travel-time isochrone tiers (years) for ``wellhead_protection``.
#: Two, five, and ten years align with the EPA WHPA fixed-travel-time approach
#: (SDWA Section 1428 wellhead protection program; delineation methods per EPA
#: 440/6-87-010; the 2-year tier is the IMMEDIATE zone).
WELLHEAD_PROTECTION_DEFAULT_TIERS: list[float] = [2.0, 5.0, 10.0]

#: Plausible shallow-aquifer hydraulic-gradient bounds (m/m). The DEM-derived
#: topographic slope is clamped into this range: a near-flat AOI below the floor
#: makes the water-table proxy unreliable (fall back to demo); a cliff above the
#: ceiling would drive an unphysical regional gradient. 5e-4..5e-2 spans typical
#: valley-fill to steep-terrain water-table gradients.
GRADIENT_MIN_MM: float = 5.0e-4
GRADIENT_MAX_MM: float = 5.0e-2

#: Half-width (deg) of the DEM footprint fetched around the AOI to estimate the
#: regional gradient. ~0.025 deg ~= 2.7 km covers the 4.1 km PRT domain so the
#: planar fit reflects the regional slope the capture zone sits in.
DEM_GRADIENT_HALF_DEG: float = 0.025


# --------------------------------------------------------------------------- #
# DEM-derived regional water-table gradient (georeferenced-mode helpers)
# --------------------------------------------------------------------------- #


def _fit_plane(
    xs: list[float], ys: list[float], zs: list[float]
) -> tuple[float, float, float]:
    """Least-squares fit ``z = a*x + b*y + c``; return ``(a, b, c)``.

    ``(a, b)`` is the planar gradient of ``z`` in the ``x`` / ``y`` units. Pure
    (numpy lstsq); raises ValueError on < 3 points or a degenerate system.
    """
    import numpy as np

    if len(xs) < 3:
        raise ValueError("_fit_plane needs >= 3 points")
    A = np.column_stack([np.asarray(xs, float), np.asarray(ys, float), np.ones(len(xs))])
    z = np.asarray(zs, float)
    coeffs, *_ = np.linalg.lstsq(A, z, rcond=None)
    return float(coeffs[0]), float(coeffs[1]), float(coeffs[2])


def _planar_gradient_from_dem(
    dem_uri: str, lat0: float, lon0: float
) -> tuple[float, float, float, float] | None:
    """Estimate the regional water-table gradient from a DEM (screening proxy).

    Reads the fetched DEM, samples a decimated pixel grid, converts each pixel to
    local east/north metres about ``(lat0, lon0)``, and fits a plane. The returned
    ``(gx, gy)`` is the topographic slope vector (m/m, x=east y=north): under the
    shallow-unconfined subdued-replica assumption the water table mimics surface
    topography, so this slope is a SCREENING proxy for the hydraulic gradient (NOT
    a measured potentiometric surface). Magnitude is clamped to
    ``[GRADIENT_MIN_MM, GRADIENT_MAX_MM]`` (direction preserved); a below-floor
    (near-flat) AOI returns ``None`` so the caller falls back to the demo gradient.

    Returns ``(gx, gy, magnitude, azimuth_deg)`` where azimuth is the compass
    bearing (deg CW from north) groundwater FLOWS toward (down-gradient), or
    ``None`` on any read failure / degenerate/flat DEM. NEVER raises.
    """
    try:
        import numpy as np
        import rasterio
        from pyproj import Transformer

        from trid3nt_server.agent.tools.processing._gdal_runner import read_raster_bytes

        # read_raster_bytes accepts s3:// or a bare local path; normalise file://.
        read_uri = dem_uri[len("file://"):] if dem_uri.startswith("file://") else dem_uri
        dem_bytes = read_raster_bytes(read_uri, on_error=lambda msg: RuntimeError(msg))
        with rasterio.MemoryFile(dem_bytes) as mf:
            with mf.open() as src:
                arr = src.read(1, masked=True)
                transform = src.transform
                src_crs = src.crs
                H, W = src.height, src.width
        step = max(1, max(H, W) // 80)
        data = np.ma.filled(arr.astype("float64"), np.nan)
        rr, cc = np.mgrid[0:H:step, 0:W:step]
        vals = data[rr, cc]
        # Pixel-centre coordinates in the dataset CRS.
        xs_ds, ys_ds = rasterio.transform.xy(transform, rr, cc)
        xs_ds = np.asarray(xs_ds, float).ravel()
        ys_ds = np.asarray(ys_ds, float).ravel()
        vals = np.asarray(vals, float).ravel()
        good = np.isfinite(vals)
        if good.sum() < 8:
            return None
        xs_ds, ys_ds, vals = xs_ds[good], ys_ds[good], vals[good]
        # Convert dataset-CRS coords -> lon/lat (identity when already 4326).
        if src_crs is not None and src_crs.to_epsg() != 4326:
            to_4326 = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True)
            lons, lats = to_4326.transform(xs_ds, ys_ds)
        else:
            lons, lats = xs_ds, ys_ds
        # Local east/north metres about the AOI centre (equirectangular).
        m_per_deg_lat = 110_540.0
        m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))
        east = (np.asarray(lons, float) - lon0) * m_per_deg_lon
        north = (np.asarray(lats, float) - lat0) * m_per_deg_lat
        a, b, _c = _fit_plane(list(east), list(north), list(vals))
        mag = math.hypot(a, b)
        if not math.isfinite(mag) or mag < GRADIENT_MIN_MM:
            return None  # too flat: DEM proxy unreliable -> caller uses demo
        clamped = min(mag, GRADIENT_MAX_MM)
        scale = clamped / mag
        gx, gy = a * scale, b * scale
        # Down-gradient (flow) azimuth = bearing of -(gx, gy).
        az = math.degrees(math.atan2(-gx, -gy)) % 360.0
        return gx, gy, clamped, az
    except Exception as exc:  # noqa: BLE001 -- DEM gradient is best-effort
        logger.warning("capture_zone DEM-gradient estimate failed (non-fatal): %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #


class CaptureZoneResult(GraceModel):
    """Return type for the composer.

    Bundles the capture-zone vector layer + the derived args + a narration
    summary dict. Invariant 1: every narrated number is a typed field  -
    ``capture_zone_layer`` carries ``capture_zone_area_km2``,
    ``travel_time_years``, ``isochrone_areas_km2``, and ``particle_count``.
    """

    schema_version: str = "v1"

    capture_zone_layer: CaptureZoneLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class CaptureZoneScenarioError(RuntimeError):
    """Base class for the composer failures."""

    error_code: str = "CAPTURE_ZONE_SCENARIO_ERROR"
    retryable: bool = False


class CaptureZoneInputError(CaptureZoneScenarioError):
    """Caller supplied invalid / missing well or AOI input (honesty gate).

    Invariant 9: the well location is NEVER fabricated. A ``capture_zone`` run
    with no well location raises this error so the agent asks the user for the
    real well coordinates.
    """

    error_code = "CAPTURE_ZONE_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #


async def model_capture_zone_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | None = None,
    *,
    well_location_latlon: tuple[float, float] | None = None,
    travel_time_years: list[float] | None = None,
    n_particles: int = 16,
    archetype: str = "capture_zone",
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    use_dem_gradient: bool = True,
    compute_class: str = "standard",
    pipeline_emitter: Any | None = None,
) -> CaptureZoneResult:
    """Compose place/AOI + a pumping well -> MODFLOW PRT -> CaptureZoneLayerURI.

    Args:
        location: a place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: an explicit ``(lat, lon)`` AOI point.
        well_location_latlon: the pumping-well ``(lat, lon)``. REQUIRED  -  a
            missing well is a typed USER_INPUT_REQUIRED failure (never invented).
            Invariant 9: the CONVEX HULL of backtracked pathlines is a physical
            delineation computed by MF6 PRT; no coordinate is fabricated.
        travel_time_years: list of travel-time isochrone cutoffs, years. Each
            value defines one nested isochrone tier of the capture zone (particles
            that reach the well within this time bound define that zone). When None
            the archetype-specific default is used:
                ``capture_zone``        -> [1.0, 5.0, 10.0]
                ``wellhead_protection`` -> [2.0, 5.0, 10.0] (EPA WHPA tiers)
        n_particles: number of particles released around the pumping-well screen
            per PRT solve (default 16; range 4..256). More particles improve
            capture-zone shape fidelity at the cost of slightly longer runtime.
        archetype: ``'capture_zone'`` (zone-of-contribution) or
            ``'wellhead_protection'`` (EPA fixed-travel-time framing). The
            difference is framing and default tiers only; both produce the same
            carrier.
        aquifer_k_ms / porosity: optional demo-aquifer overrides (narrated as
            demo defaults, not site-specific hydrogeology).
        compute_class: FR-CE-3 compute class. NOTE: PRT archetypes are
            LOCAL-ONLY (fast; the Batch path is never used).
        pipeline_emitter: optional PipelineEmitter for live progress cards.

    Returns:
        ``CaptureZoneResult`` with the ``CaptureZoneLayerURI`` (a vector polygon
        carrying per-tier isochrone areas) + derived args + a narration summary.

    Raises:
        CaptureZoneInputError: missing/invalid AOI or well (Invariant 9 gate).
        CaptureZoneScenarioError: a required step (geocode / solver) failed.
        Propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    if archetype not in ("capture_zone", "wellhead_protection"):
        raise CaptureZoneInputError(
            f"model_capture_zone_scenario: archetype must be 'capture_zone' or "
            f"'wellhead_protection'; got {archetype!r}."
        )

    # --- Honesty gate (Invariant 9): never fabricate the well -----------------
    if well_location_latlon is None:
        raise CaptureZoneInputError(
            f"{archetype} requires a pumping-well location (well_location_latlon). "
            "The well coordinates are a user input and are NEVER invented; ask the "
            "user to supply the pumping-well lat/lon. The capture-zone polygon is "
            "computed by MF6 backward particle tracking from the real well cell."
        )

    # Apply archetype-specific default tiers when the caller did not supply them.
    if travel_time_years is None:
        if archetype == "wellhead_protection":
            tiers = list(WELLHEAD_PROTECTION_DEFAULT_TIERS)
        else:
            tiers = list(CAPTURE_ZONE_DEFAULT_TIERS)
    else:
        tiers = [float(t) for t in travel_time_years if t > 0]
        if not tiers:
            raise CaptureZoneInputError(
                "travel_time_years must contain at least one positive value; "
                f"got {travel_time_years!r}."
            )

    # declare the planned internal-tool count up front: geocode (only
    # when a place string was supplied) + fetch_dem (georeferenced-gradient mode)
    # + run_modflow_archetype_job (always).
    _planned = 1
    has_loc = bool(location and location.strip())
    if has_loc:
        _planned += 1
    if use_dem_gradient:
        _planned += 1
    begin_substeps(current_emitter(), _planned)

    lat, lon, location_name = await _resolve_aoi_point(
        location, aoi_latlon, pipeline_emitter=pipeline_emitter
    )

    try:
        wlat = float(well_location_latlon[0])
        wlon = float(well_location_latlon[1])
    except Exception as exc:  # noqa: BLE001
        raise CaptureZoneInputError(
            f"invalid well_location_latlon (expected (lat, lon)): {exc}"
        ) from exc

    # --- Georeferenced-gradient mode (DEM water-table proxy) ------------------ #
    # Fetch a DEM over the well footprint and estimate the regional gradient from
    # its planar slope. The CHD boundary is then oriented to this vector so the
    # capture zone extends up-gradient toward recharge -- the "what land does my
    # well draw from" answer. A DEM fetch failure or a near-flat AOI is a LOUD
    # fallback to the demo west->east gradient (gradient_source narrated), never a
    # silent wrong-direction zone.
    grad_x: float | None = None
    grad_y: float | None = None
    gradient_source = "demo_west_east"
    gradient_magnitude: float | None = None
    gradient_azimuth_deg: float | None = None
    if use_dem_gradient:
        try:
            import asyncio

            fetch_dem_entry = TOOL_REGISTRY.get("fetch_dem")
            if fetch_dem_entry is None:
                raise CaptureZoneScenarioError("fetch_dem tool is not registered")
            d = DEM_GRADIENT_HALF_DEG
            dem_bbox = [wlon - d, wlat - d, wlon + d, wlat + d]
            async with substep(current_emitter(), "fetch_dem"):
                dem_layer = await asyncio.to_thread(
                    lambda: fetch_dem_entry.fn(bbox=dem_bbox)
                )
            dem_uri = (
                dem_layer.get("uri")
                if isinstance(dem_layer, dict)
                else getattr(dem_layer, "uri", None)
            )
            if dem_uri:
                grad = await asyncio.to_thread(
                    _planar_gradient_from_dem, dem_uri, wlat, wlon
                )
                if grad is not None:
                    grad_x, grad_y, gradient_magnitude, gradient_azimuth_deg = grad
                    gradient_source = "dem"
        except Exception as exc:  # noqa: BLE001 -- DEM gradient is best-effort
            logger.warning(
                "capture_zone DEM-gradient step failed (non-fatal, using demo "
                "west->east gradient): %s",
                exc,
            )

    try:
        run_args = MODFLOWRunArgs(
            spill_location_latlon=(lat, lon),
            contaminant="n/a",       # GWF-only archetype: no solute (placeholder)
            release_rate_kg_s=1.0,   # ignored when archetype is set
            duration_days=1.0,       # ignored when archetype is set
            archetype=archetype,
            well_location_latlon=(wlat, wlon),
            capture_zone_travel_time_years=tiers,
            n_particles=int(n_particles),
            regional_gradient_x=grad_x,
            regional_gradient_y=grad_y,
            **_aquifer_overrides(aquifer_k_ms, porosity, None, None),
        )
    except Exception as exc:  # noqa: BLE001  -  pydantic ValidationError
        raise CaptureZoneInputError(
            f"invalid {archetype} run arguments: {exc}"
        ) from exc

    label = (
        f"Model {'wellhead protection area' if archetype == 'wellhead_protection' else 'capture zone'} "
        f"[{len(tiers)} tier(s), {n_particles} particles]"
    )
    layer = await _run_archetype(
        run_args,
        compute_class=compute_class,
        pipeline_emitter=pipeline_emitter,
        tool_label=label,
        expected_type=CaptureZoneLayerURI,
        error_code=f"{archetype.upper()}_RUN_FAILED",
        scenario_error=CaptureZoneScenarioError,
    )

    layer_grad_source = getattr(layer, "gradient_source", gradient_source)
    layer_grad_mag = getattr(layer, "gradient_magnitude", gradient_magnitude)
    layer_grad_az = getattr(layer, "gradient_azimuth_deg", gradient_azimuth_deg)
    derived = {
        "location_name": location_name,
        "aoi_latlon": [lat, lon],
        "well_location_latlon": [wlat, wlon],
        "archetype": archetype,
        "travel_time_years": tiers,
        "n_particles": n_particles,
        "gradient_source": layer_grad_source,
        "regional_gradient_x": grad_x,
        "regional_gradient_y": grad_y,
        "gradient_magnitude": layer_grad_mag,
        "gradient_azimuth_deg": layer_grad_az,
    }
    iso_areas = getattr(layer, "isochrone_areas_km2", {})
    if layer_grad_source == "dem":
        gradient_caveat = (
            f"Regional gradient DEM-derived: magnitude {layer_grad_mag:.2g} m/m, "
            f"groundwater flows toward azimuth {layer_grad_az:.0f} deg (the capture "
            "zone extends the opposite, up-gradient way). This is a SCREENING proxy "
            "-- the shallow water table taken as a subdued replica of surface "
            "topography (DEM slope), NOT a measured potentiometric surface."
        )
    else:
        gradient_caveat = (
            "Regional gradient is the DEMO west->east placeholder (no usable DEM "
            "slope: fetch failed or the AOI is near-flat). The zone ORIENTATION is "
            "a placeholder, not the site's true flow direction -- narrate this."
        )
    summary = {
        "location_name": location_name,
        "archetype": archetype,
        "well_location_latlon": [wlat, wlon],
        "capture_zone_area_km2": layer.capture_zone_area_km2,
        "travel_time_years": layer.travel_time_years,
        "isochrone_areas_km2": iso_areas,
        "particle_count": layer.particle_count,
        "pathline_count": getattr(layer, "pathline_count", 0),
        "gradient_source": layer_grad_source,
        "gradient_magnitude_m_per_m": layer_grad_mag,
        "gradient_azimuth_deg": layer_grad_az,
        "stagnation_distance_m": getattr(layer, "stagnation_distance_m", None),
        "capture_width_m": getattr(layer, "capture_width_m", None),
        "gradient_caveat": gradient_caveat,
        "demo_aquifer_caveat": (
            f"Aquifer K={DEFAULT_AQUIFER_K_MS:g} m/s and porosity={DEFAULT_POROSITY:g} "
            "are demo defaults, not site-specific hydrogeology. The zone is the fan "
            "of backtracked PRT pathlines (+ their convex-hull isochrones) on a "
            "structured 100 m grid -- a screening-tier wellhead delineation, not a "
            "legally defensible wellhead protection area."
        ),
    }
    logger.info(
        "%s scenario complete location=%r capture_zone_area_km2=%.6g tiers=%s",
        archetype,
        location_name,
        layer.capture_zone_area_km2,
        layer.travel_time_years,
    )
    return CaptureZoneResult(
        capture_zone_layer=layer, derived_params=derived, summary=summary
    )


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrappers (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


TEMPLATE_CARD = TemplateCard(
    question=(
        "the capture zone / zone of contribution for a pumping well "
        "(backward particle tracking isochrones)"
    ),
    required_inputs=["location (or aoi_latlon)", "well_location_latlon"],
    knobs="travel_time_years, n_particles, aquifer_k_ms, porosity",
)


_CAPTURE_ZONE_METADATA = AtomicToolMetadata(
    name="modflow_capture_zone",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="modflow",
    tier="template",
)


@register_tool(
    _CAPTURE_ZONE_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def modflow_capture_zone(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    well_location_latlon: tuple[float, float] | list[float] | None = None,
    travel_time_years: list[float] | None = None,
    n_particles: int = 16,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Delineate the capture zone (zone of contribution) for a pumping well.

    Fidelity: MODFLOW 6 local planning-grade groundwater envelope (aquifer
    K/porosity default to narrated demo values unless supplied), not a
    calibrated regulatory delineation. Off-scope: surface-water inundation
    flooding -> sfincs_flood; urban storm-sewer / pipe-network flooding ->
    swmm_urban_flood.

    Builds a MODFLOW 6 steady groundwater-flow model, then runs an MF6 PRT
    (Particle Tracking) backward-tracking solve that releases particles around
    the pumping-well screen and tracks them up-gradient to their capture origin.
    The convex hull of all backtracked pathlines at each requested travel-time
    threshold is the capture-zone isochrone for that tier. Produces a VECTOR
    polygon layer on the map (violet protection-zone colour).

    Use this when:
        - The user asks for the capture zone, zone of contribution, zone of
          influence, or zone of transport for a pumping well.
        - The user asks how far back in time the water in a well came from.

    Do NOT use this for:
        - A wellhead PROTECTION area with EPA WHPA framing (use
          ``modflow_wellhead_protection``).
        - A pumping-well DRAWDOWN cone (use ``modflow_sustainable_yield``).
        - A contaminant spill plume (use ``modflow_contaminant_plume``).

    PRECISION CAVEAT: the polygon is the CONVEX HULL of discrete backtracked
    pathlines on a structured 100 m rectilinear grid with DEMO aquifer parameters
    (K=1e-4 m/s, porosity=0.3), NOT a calibrated regulatory wellhead protection
    area. Always narrate this caveat.

    Params:
        location: place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: explicit ``(lat, lon)`` AOI point.
        well_location_latlon: the pumping-well ``(lat, lon)``. REQUIRED -- never
            invented; ask the user if absent (Invariant 9).
        travel_time_years: list of isochrone cutoffs in years. Default [1, 5, 10].
        n_particles: particles released around the well screen (default 16; range
            4..256). More = denser pathline fan = more representative shape.
        aquifer_k_ms / porosity: optional demo-aquifer overrides.
        compute_class: FR-CE-3 compute class. Default ``'standard'``. PRT
            archetypes run LOCAL-ONLY (fast; Batch is not used).

    Returns:
        On success: a ``CaptureZoneResult`` JSON dict with the
        ``capture_zone_layer`` (a ``CaptureZoneLayerURI`` carrying
        ``capture_zone_area_km2`` + ``travel_time_years`` + per-tier
        ``isochrone_areas_km2`` + ``particle_count``). On a recoverable failure
        (incl. a missing well) the tool returns a typed error the agent narrates
        honestly -- it never fabricates a well.

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
    """
    aoi = _coerce_optional_latlon(aoi_latlon)
    well = _coerce_optional_latlon(well_location_latlon)
    try:
        result = await model_capture_zone_scenario(
            location=location,
            aoi_latlon=aoi,
            well_location_latlon=well,
            travel_time_years=(
                [float(t) for t in travel_time_years] if travel_time_years else None
            ),
            n_particles=int(n_particles),
            archetype="capture_zone",
            aquifer_k_ms=aquifer_k_ms,
            porosity=porosity,
            compute_class=compute_class,
            pipeline_emitter=None,
        )
    except CaptureZoneInputError as exc:
        return {
            "status": "error",
            "error_code": "USER_INPUT_REQUIRED",
            "error_message": str(exc),
        }
    except CaptureZoneScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "CAPTURE_ZONE_SCENARIO_ERROR"),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")

