"""Engine template ``openquake_secondary_perils`` - earthquake-triggered
liquefaction + landslide screening from a scenario ground-motion field.

Rides on the scenario GMF machinery (``openquake_scenario_gmf.run_scenario_gmf``):
one rupture, a correlated ground-motion field, then the published
``openquake.sep`` geospatial models applied per site over the fetched terrain
covariates:

- Liquefaction: Zhu et al. (2015) global geospatial logistic model - probability
  from PGA (GMF) + magnitude + Vs30 (slope-derived, Wald and Allen 2007) +
  compound topographic index (a soil-wetness proxy from the DEM).
- Landslide: Newmark sliding-block screen - a factor of safety from the DEM slope
  + labelled shallow-soil geotechnical parameters gives the yield acceleration;
  Jibson (2007) maps PGA + magnitude + yield acceleration to a co-seismic Newmark
  displacement, and Jibson et al. (2000) maps that displacement to a landslide
  probability.

Both perils publish a per-site probability COG (liquefaction primary, landslide
context) plus a probability-distribution chart, and return a
``SecondaryPerilLayerURI``. Site-data provenance is stated honestly: PGA/PGV and
magnitude are real engine output; Vs30 and the wetness index are DEM-derived;
the geotechnical strength parameters are labelled screening defaults.

Compute lane: the scenario GMF runs the installed ``oq`` engine on this machine
(no image, no Batch); the ``openquake.sep`` models are pure-numpy playground math.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.openquake_contracts import SecondaryPerilLayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata, GateSpec

from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.data import register_tool
from trid3nt_server.workflows.openquake._template_card import TemplateCard
from trid3nt_server.workflows.openquake.scenario_gmf.scenario_gmf import (
    DEFAULT_NUM_GMFS,
    DEFAULT_SCENARIO_GRID_KM,
    DEFAULT_SCENARIO_MAGNITUDE,
    ScenarioGmfError,
    resolve_scenario_rupture,
    run_scenario_gmf,
)
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.openquake.secondary_perils.secondary_perils"
)

__all__ = [
    "openquake_secondary_perils",
    "model_openquake_secondary_perils",
    "compute_site_covariates",
    "wald_allen_vs30_active",
    "LIQUEFACTION_STYLE_PRESET",
    "LANDSLIDE_STYLE_PRESET",
    "SecondaryPerilsError",
]

#: Screening probability floor: per-site probabilities at/below this are masked to
#: NaN so a COG frames only the meaningfully-triggered footprint.
_PROB_FLOOR: float = 0.02

#: Style presets (registered in publish_layer).
LIQUEFACTION_STYLE_PRESET: str = "continuous_liquefaction_probability"
LANDSLIDE_STYLE_PRESET: str = "continuous_landslide_susceptibility"

#: Labelled shallow-soil geotechnical defaults for the infinite-slope factor of
#: safety (a regional screening choice, NOT a site geotechnical investigation).
_GEO_FRICTION_DEG: float = 30.0
_GEO_COHESION_PA: float = 3000.0
_GEO_SATURATION: float = 0.10
_GEO_SLAB_THICKNESS_M: float = 2.5
_GEO_DRY_DENSITY: float = 1600.0
#: Labelled fallback compound topographic index when the terrain flow computation
#: is unavailable (a moderate-wetness screening constant, stated loudly).
_DEFAULT_CTI: float = 8.0


class SecondaryPerilsError(RuntimeError):
    """Raised when the secondary-perils chain fails fatally before a layer.

    Codes: ``SEP_PARAMS_INVALID`` (bad bbox / magnitude), ``SEP_DEP_MISSING``
    (numpy / rasterio / openquake.sep unavailable), ``SEP_DEM_FAILED`` (the DEM
    fetch produced no usable terrain), plus the propagated scenario-GMF codes."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "earthquake-triggered ground-failure screening - a liquefaction "
        "probability map AND a landslide (Newmark) probability map for a scenario "
        "rupture, from the ground-motion field plus DEM-derived Vs30 / slope / "
        "wetness covariates"
    ),
    required_inputs=["bbox"],
    knobs=(
        "magnitude, gsim, num_ground_motion_fields, site_grid_spacing_km, "
        "rupture_trace, soil_saturation"
    ),
)


_SEP_METADATA = AtomicToolMetadata(
    name="openquake_secondary_perils",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="openquake",
    gate_spec=GateSpec(
        kind="solver",
        estimate_provider="trid3nt_server.gates.cards.solver_confirm:estimate_scenario",
        title="OpenQuake scenario",
        rationale="A consequential OpenQuake solve: confirm before the run.",
    ),
    tier="template",
)


@register_tool(
    _SEP_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def openquake_secondary_perils(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    magnitude: float = DEFAULT_SCENARIO_MAGNITUDE,
    gsim: str = "BooreAtkinson2008",
    num_ground_motion_fields: int = DEFAULT_NUM_GMFS,
    vs30: float | None = None,
    site_grid_spacing_km: float = DEFAULT_SCENARIO_GRID_KM,
    max_distance_km: float = 200.0,
    rupture_trace: list[list[float]] | None = None,
    soil_saturation: float = _GEO_SATURATION,
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> SecondaryPerilLayerURI | dict[str, Any]:
    """Screen earthquake-triggered liquefaction + landslide for a scenario rupture.

    Fidelity: SCREENING, not a site geotechnical study. Runs the OpenQuake
    scenario ground-motion field (one rupture, correlated realizations), then
    applies the published ``openquake.sep`` geospatial models - Zhu et al. (2015)
    for liquefaction probability and a Newmark sliding-block screen (Jibson 2007
    displacement + Jibson et al. 2000 probability) for landslide - over the
    fetched terrain: Vs30 from DEM slope (Wald and Allen 2007), a DEM compound
    topographic index for soil wetness, and DEM slope for the infinite-slope yield
    acceleration. Shallow-soil strength parameters are labelled screening
    defaults; the returned ``site_data_note`` states every data source honestly.

    Use this when: the user asks whether an earthquake will trigger LIQUEFACTION
    or LANDSLIDES / ground failure, for a co-seismic liquefaction or landslide
    probability / susceptibility map, or a multi-hazard earthquake CASCADE (shaking
    -> ground failure). Do NOT use for: the ground-motion / shaking map itself
    (``openquake_scenario_gmf``); probabilistic return-period hazard
    (``openquake_psha``); RAINFALL-driven landslide susceptibility
    (``landlab_susceptibility``); building damage (``pelicun_damage_assessment``).

    Params:
        bbox: AOI, EPSG:4326.
        magnitude: scenario rupture moment magnitude (default 6.7, labelled demo).
        gsim: ground-motion model, default "BooreAtkinson2008".
        num_ground_motion_fields: correlated GMF realizations (default 100).
        vs30: optional uniform reference Vs30 (m/s) override; unset -> the
            slope-derived (Wald-Allen) Vs30 field is used for liquefaction.
        site_grid_spacing_km: default 4.
        max_distance_km: rupture-to-site integration distance, default 200.
        rupture_trace: optional ``[[lon,lat], ...]`` fault trace to rupture;
            unset -> real-fault-or-synthetic default.
        soil_saturation: shallow-soil saturation fraction for the infinite-slope
            factor of safety, 0..1 (default 0.10).
        input_mode: run-mode lever. ``"user_gated"`` reviews the rupture geometry +
            magnitude before the solve; ``"auto"`` (default) proceeds labelled.

    Returns:
        On success: ``SecondaryPerilLayerURI`` for LIQUEFACTION (primary) with a
        LANDSLIDE context layer also surfaced; carries ``max_probability`` /
        ``mean_probability`` / ``exceedance_area_km2`` / ``model_name`` /
        ``site_data_note``.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached.
    """
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "SEP_PARAMS_INVALID",
            "error_message": (
                "openquake_secondary_perils requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    try:
        mag = float(magnitude)
        if not (4.0 <= mag <= 9.5):
            raise ValueError(f"magnitude {mag} out of the plausible 4.0-9.5 range")
        n_gmf = int(num_ground_motion_fields)
        sat = float(soil_saturation)
        if not (0.0 <= sat <= 1.0):
            raise ValueError("soil_saturation must be in [0, 1]")
    except (TypeError, ValueError) as exc:
        return {
            "status": "error",
            "error_code": "SEP_PARAMS_INVALID",
            "error_message": f"invalid secondary-perils arguments: {exc}",
        }

    rupture = await asyncio.to_thread(
        resolve_scenario_rupture, list(coerced), mag, rupture_trace, 0.0, 90.0
    )

    _entries = [
        SyntheticInput(
            param="magnitude", value=round(mag, 2), units="Mw",
            basis="user" if magnitude != DEFAULT_SCENARIO_MAGNITUDE else "default_demo", consequence="scenario",
            note=(None if magnitude != DEFAULT_SCENARIO_MAGNITUDE
                  else "labelled scenario magnitude demo default (not source-calibrated)"),
        ),
        SyntheticInput(
            param="rupture_geometry", value=rupture.kind, units=None,
            basis=("prompt_interpreted" if rupture_trace is not None
                   else ("fetched" if rupture.kind == "real-fault" else "default_demo")), consequence="scenario",
            note=rupture.note,
        ),
    ]
    _review = await gate_input_review(
        tool_name="openquake_secondary_perils", mode=input_mode,
        entries=_entries, params={"magnitude": mag},
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"openquake_secondary_perils {_review.cancel_reason}",
        }
    _rv_mag = _review.params.get("magnitude")
    if _rv_mag is not None and float(_rv_mag) != mag:
        mag = float(_rv_mag)
        rupture = rupture.with_magnitude(mag)

    logger.info(
        "openquake_secondary_perils bbox=%s M=%.2f gsim=%s n_gmf=%d kind=%s",
        list(coerced), mag, gsim, n_gmf, rupture.kind,
    )

    try:
        layer = await model_openquake_secondary_perils(
            bbox=tuple(coerced), magnitude=mag, gsim=str(gsim), num_gmfs=n_gmf,
            reference_vs30=(float(vs30) if vs30 is not None else None),
            site_grid_spacing_km=float(site_grid_spacing_km),
            max_distance_km=float(max_distance_km), rupture=rupture,
            soil_saturation=sat,
        )
        layer = layer.model_copy(update={"synthetic_inputs": _review.entries})
        return layer
    except asyncio.CancelledError:
        raise
    except (SecondaryPerilsError, ScenarioGmfError) as exc:
        logger.warning("openquake_secondary_perils failed: %s (%s)", exc.error_code, exc)
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("openquake_secondary_perils unexpected failure")
        return {
            "status": "error",
            "error_code": "SEP_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# Terrain covariates: DEM -> slope + Wald-Allen Vs30 + compound topographic index.
# --------------------------------------------------------------------------- #
#: Wald and Allen (2007) active-tectonic Vs30-from-slope bins: (slope_gradient
#: lower bound in m/m, representative Vs30 m/s). A steeper local gradient implies
#: stiffer, higher-Vs30 ground; flat valley floors map to soft, low-Vs30 sediment.
_WALD_ALLEN_ACTIVE: tuple[tuple[float, float], ...] = (
    (0.0, 180.0),
    (3.0e-4, 240.0),
    (3.5e-3, 300.0),
    (0.010, 360.0),
    (0.018, 490.0),
    (0.050, 620.0),
    (0.10, 760.0),
    (0.138, 900.0),
)


def wald_allen_vs30_active(slope_gradient: Any) -> Any:
    """Map topographic slope gradient (m/m) to Vs30 (m/s), active-tectonic table."""
    import numpy as np

    grad = np.asarray(slope_gradient, dtype="float64")
    vs30 = np.full(grad.shape, _WALD_ALLEN_ACTIVE[0][1], dtype="float64")
    for lower, value in _WALD_ALLEN_ACTIVE:
        vs30 = np.where(grad >= lower, value, vs30)
    return vs30


@dataclass
class SiteCovariates:
    slope_rad: Any     # per-site slope, radians
    vs30: Any          # per-site Vs30, m/s (Wald-Allen)
    cti: Any           # per-site compound topographic index
    cti_source: str    # honest provenance of the CTI field


def compute_site_covariates(
    dem_path: str,
    lons: Any,
    lats: Any,
) -> SiteCovariates:
    """Compute per-site slope + Wald-Allen Vs30 + compound topographic index.

    Reads the fetched DEM (EPSG:4326 COG), derives a slope field (metric gradient,
    latitude-corrected pixel size), a Wald-Allen Vs30 field, and a compound
    topographic index (richdem D-infinity accumulation over slope). Because a
    scenario site cell is coarse relative to the DEM, each site samples the DEM
    within a half-site-spacing WINDOW and takes the governing sub-cell condition:
    the 85th-percentile slope (landslide governs on the steepest sub-slope), the
    Vs30 of the 15th-percentile slope gradient (liquefaction governs on the
    softest/flattest sub-cell) and the 85th-percentile CTI (wettest sub-cell). A
    fine grid (window <= 1 px) falls back to the single nearest cell. When the
    terrain flow computation is unavailable the CTI degrades LOUDLY to a labelled
    constant (``cti_source`` records which path was taken).
    """
    import numpy as np
    import rasterio

    with rasterio.open(dem_path) as ds:
        dem = ds.read(1).astype("float64")
        transform = ds.transform
        nodata = ds.nodata
        height, width = dem.shape
        # Cell size in metres (latitude-corrected at the DEM centre).
        lat0 = transform.f + transform.e * (height / 2.0)
        dx_m = abs(transform.a) * 111320.0 * max(math.cos(math.radians(lat0)), 0.05)
        dy_m = abs(transform.e) * 110540.0
    if nodata is not None:
        dem = np.where(dem == nodata, np.nan, dem)
    # Fill NaNs with the finite mean so gradients / flow stay finite.
    finite = np.isfinite(dem)
    fill = float(np.nanmean(dem)) if finite.any() else 0.0
    dem_filled = np.where(finite, dem, fill)

    gy, gx = np.gradient(dem_filled, dy_m, dx_m)
    slope_rad = np.arctan(np.sqrt(gx * gx + gy * gy))
    slope_grad_grid = np.tan(slope_rad)

    cti_grid, cti_source = _compute_cti(dem_filled, slope_rad, (dx_m + dy_m) / 2.0)

    # Windowed sub-cell screening: a coarse scenario site cell contains a
    # DISTRIBUTION of terrain, so sample the DEM within a half-site-spacing window
    # around each site and take the governing sub-cell condition per peril:
    # landslide governs on the STEEPEST sub-slope (85th pct slope), liquefaction on
    # the SOFTEST / WETTEST sub-cell (15th pct slope gradient -> Vs30, 85th pct
    # CTI). Falls back to the single nearest cell for a fine grid (window <= 1 px).
    lons = np.asarray(lons, dtype="float64")
    lats = np.asarray(lats, dtype="float64")
    inv = ~transform
    # Window half-size in pixels from the median inter-site spacing.
    half_deg = _median_site_spacing_deg(lons, lats) / 2.0
    half_px = max(int(round(half_deg / max(abs(transform.a), 1e-9))), 0)

    n = lons.size
    slope_out = np.empty(n, dtype="float64")
    vs30_out = np.empty(n, dtype="float64")
    cti_out = np.empty(n, dtype="float64")
    for i in range(n):
        c, r = inv * (float(lons[i]), float(lats[i]))
        ci = min(max(int(round(c)), 0), width - 1)
        ri = min(max(int(round(r)), 0), height - 1)
        r0, r1 = max(ri - half_px, 0), min(ri + half_px + 1, height)
        c0, c1 = max(ci - half_px, 0), min(ci + half_px + 1, width)
        win_slope = slope_rad[r0:r1, c0:c1]
        win_grad = slope_grad_grid[r0:r1, c0:c1]
        win_cti = cti_grid[r0:r1, c0:c1]
        if win_slope.size <= 1:
            slope_out[i] = slope_rad[ri, ci]
            vs30_out[i] = wald_allen_vs30_active(slope_grad_grid[ri, ci])
            cti_out[i] = cti_grid[ri, ci]
        else:
            slope_out[i] = float(np.nanpercentile(win_slope, 85.0))
            vs30_out[i] = float(wald_allen_vs30_active(
                np.nanpercentile(win_grad, 15.0)))
            cti_out[i] = float(np.nanpercentile(win_cti, 85.0))

    return SiteCovariates(
        slope_rad=slope_out, vs30=vs30_out, cti=cti_out, cti_source=cti_source,
    )


def _median_site_spacing_deg(lons: Any, lats: Any) -> float:
    """Median nearest-neighbour spacing (deg) of the scenario site grid."""
    import numpy as np

    lon_u = np.unique(np.round(np.asarray(lons, dtype="float64"), 4))
    lat_u = np.unique(np.round(np.asarray(lats, dtype="float64"), 4))
    steps = []
    if lon_u.size >= 2:
        steps.append(float(np.median(np.diff(lon_u))))
    if lat_u.size >= 2:
        steps.append(float(np.median(np.diff(lat_u))))
    return max(steps) if steps else 0.03


def _compute_cti(dem_filled: Any, slope_rad: Any, cell_m: float) -> tuple[Any, str]:
    """Compound topographic index ln(specific catchment area / tan slope).

    Primary path: richdem D-infinity flow accumulation. LOUD typed fallback: a
    labelled moderate-wetness constant when richdem is unavailable / fails.
    """
    import numpy as np

    try:
        import richdem as rd

        rda = rd.rdarray(dem_filled.astype("float64"), no_data=-9999.0)
        rd.fill_depressions(rda, in_place=True)
        acc = np.asarray(rd.flow_accumulation(rda, method="Dinf"), dtype="float64")
        sca = (acc + 1.0) * float(cell_m)
        tanb = np.tan(np.asarray(slope_rad, dtype="float64"))
        tanb = np.where(tanb < 1e-4, 1e-4, tanb)
        cti = np.log(sca / tanb)
        cti = np.where(np.isfinite(cti), cti, _DEFAULT_CTI)
        return cti, "compound topographic index from the fetched DEM (richdem D-infinity)"
    except Exception as exc:  # noqa: BLE001 - loud typed fallback
        logger.warning(
            "secondary_perils: CTI terrain computation failed (%s); using the "
            "labelled default CTI=%.1f", exc, _DEFAULT_CTI,
        )
        return (
            np.full(np.asarray(dem_filled).shape, _DEFAULT_CTI, dtype="float64"),
            f"labelled default compound topographic index ({_DEFAULT_CTI:g}) "
            "(DEM flow computation unavailable)",
        )


def _fetch_dem_local(
    bbox: tuple[float, float, float, float], tmpdir: str
) -> str:
    """Fetch the AOI DEM and stage it locally; return the local COG path.

    The fetched DEM (the vs30/slope/CTI covariate substrate) is auto-surfaced as
    a role=context input by the emit-on-fetch router seam; the
    ``purpose="terrain"`` fetch carries the name.
    """
    from trid3nt_server.data import TOOL_REGISTRY

    layer = TOOL_REGISTRY["fetch_copernicus_dem"].fn(
        bbox=list(bbox), purpose="terrain")
    uri = layer.uri
    if uri.startswith("s3://"):
        from trid3nt_server.data.cache import read_object_bytes_s3

        local = os.path.join(tmpdir, "dem.tif")
        Path(local).write_bytes(read_object_bytes_s3(uri))
        return local
    if os.path.exists(uri):
        return uri
    raise SecondaryPerilsError(
        "SEP_DEM_FAILED", f"DEM uri not stageable locally: {uri!r}"
    )


# --------------------------------------------------------------------------- #
# Secondary-peril models (openquake.sep, pure numpy over the GMF field).
# --------------------------------------------------------------------------- #
def _liquefaction_probability(pga: Any, mag: float, cti: Any, vs30: Any) -> Any:
    """Zhu et al. (2015) global liquefaction probability (returns prob only)."""
    from openquake.sep.liquefaction.liquefaction import zhu_etal_2015_general

    prob, _cls = zhu_etal_2015_general(pga, mag, cti, vs30)
    import numpy as np

    return np.clip(np.asarray(prob, dtype="float64"), 0.0, 1.0)


def _landslide_probability(
    pga: Any, mag: float, slope_rad: Any, saturation: float
) -> Any:
    """Newmark landslide probability: infinite-slope FS -> yield accel ->
    Jibson (2007) displacement -> Jibson et al. (2000) probability."""
    import numpy as np

    from openquake.sep.landslide.displacement import (
        critical_accel,
        jibson_2007_model_b,
    )
    from openquake.sep.landslide.probability import jibson_etal_2000_probability
    from openquake.sep.landslide.static_safety_factor import infinite_slope_fs

    fs = infinite_slope_fs(
        slope=slope_rad, cohesion=_GEO_COHESION_PA,
        friction_angle=_GEO_FRICTION_DEG, saturation_coeff=saturation,
        slab_thickness=_GEO_SLAB_THICKNESS_M, soil_dry_density=_GEO_DRY_DENSITY,
    )
    ca = critical_accel(np.asarray(fs, dtype="float64"), np.asarray(slope_rad, dtype="float64"))
    disp = jibson_2007_model_b(pga=np.asarray(pga, dtype="float64"), crit_accel=ca, mag=mag)
    prob = jibson_etal_2000_probability(np.asarray(disp, dtype="float64"))
    return np.clip(np.asarray(prob, dtype="float64"), 0.0, 1.0)


# --------------------------------------------------------------------------- #
# Composer.
# --------------------------------------------------------------------------- #
async def model_openquake_secondary_perils(
    *,
    bbox: tuple[float, float, float, float],
    magnitude: float,
    gsim: str,
    num_gmfs: int,
    reference_vs30: float | None,
    site_grid_spacing_km: float,
    max_distance_km: float,
    rupture: Any,
    soil_saturation: float,
) -> SecondaryPerilLayerURI:
    """Run the scenario GMF, apply the sep models, publish liquefaction +
    landslide probability COGs, and return the liquefaction layer (primary)."""
    import numpy as np

    from trid3nt_server.workflows.openquake.postprocess_openquake import (
        rasterize_hazard_sites,
    )
    from trid3nt_server.workflows.openquake.scenario_gmf.scenario_gmf import (
        _write_publish_cog,
    )
    from trid3nt_server.emission.layer_uri_emit import publish_input_layer

    run_id = new_ulid()
    begin_substeps(current_emitter(), 4)

    # 1) scenario GMF (in-process oq off the loop).
    async with substep(current_emitter(), "run_scenario_gmf"):
        result = await asyncio.to_thread(
            run_scenario_gmf,
            bbox=bbox, magnitude=magnitude, imt="PGA", num_gmfs=num_gmfs,
            gsim=gsim, reference_vs30=(reference_vs30 or 760.0),
            site_grid_spacing_km=site_grid_spacing_km,
            max_distance_km=max_distance_km, rupture=rupture,
        )

    lons = np.array([s["lon"] for s in result.sites], dtype="float64")
    lats = np.array([s["lat"] for s in result.sites], dtype="float64")
    pga = np.array([s.get("gmv_PGA", float("nan")) for s in result.sites], dtype="float64")

    # 2) terrain covariates (DEM fetch + slope/Vs30/CTI) off the loop. The fetched
    # DEM auto-surfaces as a role=context input via the emit-on-fetch seam (0244).
    async with substep(current_emitter(), "site_covariates"):
        cov = await asyncio.to_thread(
            _covariates_for_sites, bbox, lons, lats)

    vs30 = (
        np.full(lons.shape, float(reference_vs30), dtype="float64")
        if reference_vs30 is not None else cov.vs30
    )
    vs30_note = (
        f"uniform user Vs30 {reference_vs30:g} m/s"
        if reference_vs30 is not None
        else "Vs30 from DEM slope (Wald and Allen 2007, active-tectonic)"
    )

    # 3) apply the sep models (pure numpy).
    liq = _liquefaction_probability(pga, float(magnitude), cov.cti, vs30)
    lsl = _landslide_probability(pga, float(magnitude), cov.slope_rad, soil_saturation)

    site_data_note = (
        f"PGA + magnitude are OpenQuake scenario output; {vs30_note}; "
        f"{cov.cti_source}; slope from the fetched DEM (governing sub-cell "
        f"percentile per peril); shallow-soil strength (cohesion "
        f"{_GEO_COHESION_PA:g} Pa, friction {_GEO_FRICTION_DEG:g} deg, saturation "
        f"{soil_saturation:g}) are labelled screening defaults."
    )

    # 4) rasterize + publish both probability COGs.
    async with substep(current_emitter(), "rasterize_and_publish"):
        liq_grid, bbox_grid, cell_deg = rasterize_hazard_sites(
            list(zip(lons.tolist(), lats.tolist(), liq.tolist()))
        )
        lsl_grid, _b2, _c2 = rasterize_hazard_sites(
            list(zip(lons.tolist(), lats.tolist(), lsl.tolist()))
        )
        liq_uri, liq_bbox = await asyncio.to_thread(
            _write_publish_cog, liq_grid, bbox_grid, run_id, "liquefaction",
            LIQUEFACTION_STYLE_PRESET, floor=_PROB_FLOOR,
        )
        lsl_uri, _lb = await asyncio.to_thread(
            _write_publish_cog, lsl_grid, bbox_grid, run_id, "landslide",
            LANDSLIDE_STYLE_PRESET, floor=_PROB_FLOOR,
        )

    liq_max, liq_mean, liq_area, liq_n = _prob_metrics(liq_grid, cell_deg, liq_bbox)
    lsl_max, lsl_mean, lsl_area, lsl_n = _prob_metrics(lsl_grid, cell_deg, liq_bbox)

    liq_layer = SecondaryPerilLayerURI(
        layer_id=f"eq-liquefaction-{run_id}",
        name=f"Earthquake liquefaction probability (M{magnitude:g})",
        layer_type="raster", uri=liq_uri, style_preset=LIQUEFACTION_STYLE_PRESET,
        role="primary", units="probability", bbox=liq_bbox,
        peril="liquefaction", model_name="zhu_etal_2015_general",
        max_probability=liq_max, mean_probability=liq_mean,
        exceedance_area_km2=liq_area, n_sites=liq_n, magnitude=float(magnitude),
        site_data_note=site_data_note,
    )
    lsl_layer = SecondaryPerilLayerURI(
        layer_id=f"eq-landslide-{run_id}",
        name=f"Earthquake landslide probability (Newmark, M{magnitude:g})",
        layer_type="raster", uri=lsl_uri, style_preset=LANDSLIDE_STYLE_PRESET,
        role="context", units="probability", bbox=None,
        peril="landslide", model_name="jibson_2007_newmark",
        max_probability=lsl_max, mean_probability=lsl_mean,
        exceedance_area_km2=lsl_area, n_sites=lsl_n, magnitude=float(magnitude),
        site_data_note=site_data_note,
    )
    await publish_input_layer(current_emitter(), lsl_layer, role="context")

    await _emit_peril_distribution_chart(
        liq, lsl, magnitude, source_layer_uri=liq_layer.uri
    )

    logger.info(
        "model_openquake_secondary_perils complete run_id=%s M=%.2f "
        "liq_max=%.3f lsl_max=%.3f n_sites=%d kind=%s",
        run_id, magnitude, liq_max, lsl_max, liq_n, result.rupture_kind,
    )
    return liq_layer


def _covariates_for_sites(
    bbox: tuple[float, float, float, float], lons: Any, lats: Any,
) -> SiteCovariates:
    """Fetch the DEM + compute site covariates (sync; run off the loop)."""
    tmpdir = tempfile.mkdtemp(prefix="trid3nt_sep_dem_")
    dem_path = _fetch_dem_local(bbox, tmpdir)
    return compute_site_covariates(dem_path, lons, lats)


def _prob_metrics(
    grid: Any, cell_deg: float, bbox: tuple[float, float, float, float]
) -> tuple[float, float, float, int]:
    """Return (max, mean, exceedance_area_km2_above_floor, n_finite_sites)."""
    import numpy as np

    arr = np.asarray(grid, dtype="float64")
    finite = np.isfinite(arr)
    n = int(np.count_nonzero(finite))
    if n == 0:
        return 0.0, 0.0, 0.0, 0
    mean_lat = (bbox[1] + bbox[3]) / 2.0
    km = 111.32
    cell_area = (cell_deg * km) * (cell_deg * km * abs(math.cos(math.radians(mean_lat))))
    area = float(np.count_nonzero(finite & (arr > _PROB_FLOOR))) * cell_area
    return float(np.nanmax(arr)), float(np.nanmean(arr)), area, n


async def _emit_peril_distribution_chart(
    liq: Any, lsl: Any, magnitude: float, *, source_layer_uri: str | None
) -> None:
    """Side-emit a per-site probability-distribution chart for both perils."""
    try:
        import numpy as np

        def _sorted_rows(arr: Any, label: str) -> list[dict[str, Any]]:
            a = np.asarray(arr, dtype="float64")
            a = np.sort(a[np.isfinite(a)])
            if a.size < 3:
                return []
            return [
                {"site_percentile": round(100.0 * i / (a.size - 1), 2),
                 "probability": round(float(v), 5), "peril": label}
                for i, v in enumerate(a)
            ]

        rows = _sorted_rows(liq, "liquefaction") + _sorted_rows(lsl, "landslide")
        if not rows:
            return
        from trid3nt_server.data.processing.charts_common import build_chart_payload

        spec = {
            "data": {"values": rows},
            "mark": {"type": "line"},
            "encoding": {
                "x": {"field": "site_percentile", "type": "quantitative",
                      "title": "AOI site percentile (%)"},
                "y": {"field": "probability", "type": "quantitative",
                      "title": "triggering probability"},
                "color": {"field": "peril", "type": "nominal", "title": "peril"},
            },
        }
        payload = build_chart_payload(
            vega_lite_spec=spec,
            title=f"Earthquake-triggered ground-failure probability (M{magnitude:g})",
            caption=(
                "Per-site liquefaction (Zhu 2015) and landslide (Newmark/Jibson) "
                "probabilities sorted low-to-high across AOI sites."
            ),
            source_layer_uri=source_layer_uri,
        )
        await emit_chart_payloads(payload)
    except Exception as exc:  # noqa: BLE001 - chart is best-effort
        logger.warning("peril distribution chart emit failed (non-fatal): %s", exc)
