"""The TOMAWAC deck and deliverable: a storm over a fetch, a wave field out.

One serialization hook and one publisher for TOMAWAC, the third-generation
spectral (phase-averaged) wave-action solver: wind-wave generation over a fetch,
swell shoaling and depth-breaking, wave-current interaction, bottom-friction
dissipation. Staging, dispatching and reading the run are the shared open-water
front (``steps/open_water.py``); what lives here is only what is WAVE about a
wave run.

TWO BED PATHS, and which one runs is decided HERE rather than by the template,
because it depends on where the acquired AOI actually IS: a Great Lakes AOI gets
the real NOAA lake-datum bathymetry, and anywhere else gets the geography-free
idealized basin that reproduces the official TOMAWAC verification physics -
labeled as such, never passed off as a survey.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import (
    TELEMAC_WAVE_STYLE_PRESET,
    TelemacWaveLayerURI,
)

from trid3nt_server.workflows.lib import Step
from trid3nt_server.workflows.shared.publish_product_layer import (
    publish_product_layer,
)

from .open_water import (
    download_open_water_result,
    mesh_sizing_provenance,
    solved_domain_bbox,
    surface_in_worker_bed_input,
)

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.wave")

__all__ = ["GREAT_LAKES", "Wave", "great_lake_for", "publish_wave_products",
           "write_wave_deck"]

_STEPS = "trid3nt_server.workflows.telemac.steps"

#: The key the worker entrypoint reads, and the cache path the manifest is
#: staged under. NOT the same word: the module answers to "wave" but its
#: manifests have always lived under "tomawac/".
_SECTION = "wave"
_PREFIX = "tomawac"
_RESULT = "res_wave.slf"
_OUTPUTS = [
    "res_wave.slf", "geo_wave.slf", "bc_wave.cli", "tom_wave.cas",
    "full_listing.log", "tomawac_wave.log", "bed_bathymetry.tif",
    "telemac_metrics.json",
]

#: Rough lon/lat extents of the five Great Lakes' open water. The gate on the
#: REAL-bathymetry path: the NOAA lake-datum grids cover these and nothing else,
#: so an AOI outside them has no real bed to sample and says so.
GREAT_LAKES: dict[str, tuple[float, float, float, float]] = {
    "superior": (-92.2, 46.4, -84.3, 49.1),
    "michigan": (-88.1, 41.6, -84.7, 46.1),
    "huron": (-84.8, 43.0, -79.7, 46.3),
    "erie": (-83.5, 41.3, -78.8, 42.9),
    "ontario": (-79.9, 43.2, -76.0, 44.3),
}

#: Labeled grid spacings when the caller names none. The real path is coarser
#: because a lake is large; both are self-labeled on the layer.
_DEFAULT_REAL_RES_M = 2000.0
_DEFAULT_IDEALIZED_RES_M = 1500.0


def great_lake_for(lon: float, lat: float) -> str | None:
    """Which Great Lake this point sits in, or ``None`` for anywhere else."""
    for name, (x0, y0, x1, y1) in GREAT_LAKES.items():
        if x0 <= lon <= x1 and y0 <= lat <= y1:
            return name
    return None


async def write_wave_deck(
    *,
    aoi: dict[str, Any],
    wave_mode: str = "fetch_growth",
    wind_speed_mps: float = 20.0,
    wind_direction_deg: float = 270.0,
    boundary_hs_m: float = 1.5,
    boundary_period_s: float = 10.0,
    current_speed_mps: float = -2.5,
    bottom_friction: bool | None = None,
    mesh_resolution_m: float | None = None,
    sim_duration_hours: float = 4.0,
    bathy_source: str = "auto",
) -> dict[str, Any]:
    """Serialize the approved sheet into the worker's wave config + the run meta.

    ``bottom_friction`` unset ARMS ITSELF for the mode that is about friction and
    stays off otherwise: a run whose question is dissipation and whose deck had no
    dissipation in it would answer a different question than the one asked.
    """
    from trid3nt_server.workflows.telemac.run_telemac import TOMAWAC_SOLVER_NAME

    asked = str(bathy_source or "auto").strip().lower()
    lake = great_lake_for(float(aoi["lon"]), float(aoi["lat"]))
    real = asked == "noaa_greatlakes" or (asked == "auto" and lake is not None)
    resolution = (float(mesh_resolution_m) if mesh_resolution_m is not None
                  else (_DEFAULT_REAL_RES_M if real else _DEFAULT_IDEALIZED_RES_M))
    friction = (str(wave_mode) == "bottom_friction" if bottom_friction is None
                else bool(bottom_friction))

    config: dict[str, Any] = {
        "name": aoi["slug"],
        "wave_mode": str(wave_mode),
        "bathy_source": "noaa_greatlakes" if real else "idealized",
        "wind_speed_mps": float(wind_speed_mps),
        "wind_dir_from_deg": float(wind_direction_deg),
        "boundary_hs_m": float(boundary_hs_m),
        "boundary_fp_hz": round(1.0 / max(float(boundary_period_s), 1e-3), 5),
        "current_uc_mps": float(current_speed_mps),
        "bottom_friction": friction,
        "target_resolution_m": float(resolution),
        "duration_hours": float(sim_duration_hours),
    }
    # The IDEALIZED basin is geography-free: it has no AOI to sample, so the
    # manifest carries no bbox and the worker builds its own rectangle.
    if real:
        config["bbox"] = [round(float(v), 4) for v in aoi["bbox"]]
    return {
        "config": config,
        "run_tag": new_ulid(),
        "section": _SECTION,
        "prefix": _PREFIX,
        "solver": TOMAWAC_SOLVER_NAME,
        "result_basename": _RESULT,
        "outputs": list(_OUTPUTS),
        "run_failed_code": "TOMAWAC_RUN_FAILED",
        "output_missing_code": "TOMAWAC_OUTPUT_MISSING",
        # Both bed paths report a zone: the real lake its own, the idealized basin
        # the placeholder one its geography-free grid is laid in.
        "requires_utm": True,
        "domain_name": aoi["name"],
        "domain_slug": aoi["slug"],
        "mesh_size_m": float(resolution),
        # What the CALLER asked for, kept beside what was built, so a
        # lever the worker moved leaves a row instead of a silence.
        "mesh_resolution_asked_m": (mesh_resolution_m if mesh_resolution_m is not None
                                    else None),
        "wave_mode": str(wave_mode),
        "real_bathymetry": real,
        "lake": lake,
        "bathy_label": (
            f"real NOAA Great Lakes lake-datum bathymetry ({lake or 'AOI'})" if real
            else "idealized basin (no real bathymetry fetched for this AOI; "
                 "geography-free verification physics)"),
        "bottom_friction": friction,
        "wind_speed_mps": float(wind_speed_mps),
        "wind_direction_deg": float(wind_direction_deg),
    }


def _provenance(deck: dict[str, Any], metrics: dict[str, Any]) -> list[SyntheticInput]:
    """The physically dominant inputs, as rows the layer carries.

    The storm wind is a PRESCRIBED demo value - no wave-forcing fetcher exists -
    so it is labeled as one rather than left to read as an observation, and the
    bed says out loud whether it was surveyed or invented.
    """
    real = bool(deck["real_bathymetry"])
    return [
        SyntheticInput(
            param="wind_speed_mps",
            value=round(float(metrics.get("wind_speed_mps",
                                          deck["wind_speed_mps"])), 1),
            units="m/s", basis="default_demo", consequence="physics",
            note="prescribed steady storm wind (no wave-forcing fetcher exists)"),
        SyntheticInput(
            param="wind_direction_deg", value=round(deck["wind_direction_deg"], 1),
            units="deg", basis="default_demo", consequence="physics",
            note="compass bearing the wind blows FROM; the fetch runs downwind of it"),
        SyntheticInput(
            param="bathy_source", value=deck["config"]["bathy_source"],
            basis="fetched" if real else "default_demo", consequence="physics",
            real_source_if_any=("NOAA NGDC Great Lakes lake-datum bathymetry"
                                if real else None),
            note=deck["bathy_label"]),
        SyntheticInput(
            param="bottom_friction", value=str(bool(deck["bottom_friction"])),
            basis="derived", consequence="physics",
            note=("bottom-friction dissipation armed for this question class"
                  if deck["bottom_friction"] else "no bottom-friction dissipation")),
    ] + mesh_sizing_provenance(deck.get("mesh_resolution_asked_m"), metrics)


def _honesty_note(deck: dict[str, Any]) -> str:
    return (
        "Spectral wave SCREENING: TOMAWAC third-generation wave-action solver "
        f"({deck['wave_mode']}) over a {deck['mesh_size_m']:g} m grid of "
        f"{deck['bathy_label']}, driven by a PRESCRIBED steady storm wind of "
        f"{deck['wind_speed_mps']:g} m/s from {deck['wind_direction_deg']:g} deg - "
        "a labeled demo forcing, not an observed sea state. The raster is the "
        "FINAL-frame significant wave height (the steady sea); the time evolution "
        "plays from the native SELAFIN. Not a calibrated hindcast.")


async def publish_wave_products(*, deck: dict[str, Any],
                                solve: dict[str, Any]) -> TelemacWaveLayerURI:
    """Postprocess the solved wave field into its published layer + scalars."""
    from trid3nt_server.emission.pipeline_emitter import current_emitter
    from trid3nt_server.workflows.telemac.postprocess_telemac import postprocess_tomawac

    emitter = current_emitter()
    run_id, utm_epsg = solve["run_id"], int(solve["utm_epsg"])
    metrics = dict(solve.get("metrics") or {})
    reach = deck["domain_slug"]

    slf_path = await asyncio.to_thread(
        download_open_water_result, run_id, deck["result_basename"],
        error_code=deck["output_missing_code"])
    try:
        layers, _pmetrics = await asyncio.to_thread(
            postprocess_tomawac, slf_path, run_id=run_id, utm_epsg=utm_epsg,
            reach_name=reach, wave_mode=deck["wave_mode"],
            # ONLY the real-lake grid has a geographic corner to add back; the
            # idealized basin has none, and claiming one would put a made-up
            # domain on a real map.
            domain_bbox=(solved_domain_bbox(deck, metrics)
                         if deck["real_bathymetry"] else None))
    finally:
        Path(slf_path).unlink(missing_ok=True)
    if not layers:
        from .open_water import OpenWaterError

        raise OpenWaterError("postprocess_tomawac produced no wave layer.",
                             error_code="TOMAWAC_NO_LAYERS")
    raw = layers[0]

    # The worker's own discriminating shore pair: Hs at the upwind shore against
    # Hs at the downwind shore under the SAME storm, which is what makes a
    # fetch-growth answer checkable rather than a single number.
    published = await publish_product_layer(
        raw, style_preset=TELEMAC_WAVE_STYLE_PRESET,
        update={
            "hs_upwind_m": metrics.get("hs_upwind_m"),
            "hs_downwind_m": metrics.get("hs_downwind_m"),
            "peak_period_max_s": metrics.get("peak_period_max_s"),
            "wind_speed_mps": metrics.get("wind_speed_mps"),
            "mesh_size_m": metrics.get("dx_m"),
            "mesh_resolution_label": (
                f"{'real NOAA lake bathy' if deck['real_bathymetry'] else 'idealized'} "
                f"grid {metrics.get('dx_m', deck['mesh_size_m']):g} m"
                + (" (coarsened under node budget)" if metrics.get("coarsened") else "")),
            "fallback_note": _honesty_note(deck),
            "synthetic_inputs": _provenance(deck, metrics),
            "run_id": run_id,
            # The along-fetch growth curve the WORKER measured. Carried on the
            # layer so the chart plots the run's own numbers instead of
            # resampling the raster into a second, slightly different answer.
            "fetch_curve_km": list(metrics.get("chart_x_km") or []) or None,
            "fetch_curve_hs_m": list(metrics.get("chart_hs_m") or []) or None,
        })

    await surface_in_worker_bed_input(
        emitter, run_metrics=metrics, run_id=run_id,
        name=(f"Input: lake bed bathymetry ({reach}, NOAA Great Lakes lake-datum, "
              "in-worker)"),
        layer_id_prefix="input-lake-bed")

    logger.info("telemac tomawac complete run_id=%s domain=%s mode=%s hs_max=%.4g "
                "upwind=%s downwind=%s uri=%s", run_id, reach, deck["wave_mode"],
                published.hs_max_m, published.hs_upwind_m, published.hs_downwind_m,
                published.uri)
    return published


class Wave:
    """The TOMAWAC author + read steps, as the facade binds them."""

    @staticmethod
    def deck(**kwargs: Any) -> Step:
        return Step(runner=f"{_STEPS}.wave.write_wave_deck", stage="author",
                    kwargs=kwargs)

    @staticmethod
    def products(*, deck: Any, solve: Any) -> Step:
        return Step(runner=f"{_STEPS}.wave.publish_wave_products", stage="publish",
                    kwargs={"deck": deck, "solve": solve})
