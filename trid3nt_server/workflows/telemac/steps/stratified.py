"""The TELEMAC-3D deck and deliverable: a water column in, its structure out.

One serialization hook and one publisher for TELEMAC-3D, the three-dimensional
(hydrostatic or non-hydrostatic) Navier-Stokes solver with active-tracer
baroclinic density coupling over sigma layers. Staging, dispatching and reading
the run are the shared open-water front (``steps/open_water.py``); what lives here
is only what is 3D about a 3D run.

TWO LAYERS, ONE ANSWER. The deliverable is a PAIR - the surface field and the
bottom field - because the whole point of going 3D is the contrast between them,
and a single depth-averaged map is exactly what this template exists to refuse.
The bottom companion is published beside the surface layer the tool returns.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import (
    TELEMAC3D_STRATIFICATION_STYLE_PRESET,
    Telemac3dLayerURI,
)

from trid3nt_server.workflows.lib import Step
from trid3nt_server.workflows.shared.publish_product_layer import (
    publish_product_layer,
)

from .open_water import (
    OpenWaterError,
    download_open_water_result,
    great_lake_for,
    mesh_resolution_label,
    mesh_sizing_provenance,
    real_lake_bathy_label,
    solves_on_real_bed,
    staged_bed_inputs,
)

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.stratified")

__all__ = ["Stratified", "publish_stratified_products", "write_stratified_deck"]

_STEPS = "trid3nt_server.workflows.telemac.steps"

_SECTION = "stratified"
_PREFIX = "telemac3d"
#: The two single-frame layer SELAFINs the worker re-emits from the 3D result's
#: top and bed sigma planes. ``res3d_t3d.slf`` beside them is the time-stepped 3D
#: field, which nothing publishes today (it is what the proof animation reads).
_SURFACE = "t3d_surface.slf"
_BOTTOM = "t3d_bottom.slf"
_OUTPUTS = [
    "t3d_surface.slf", "t3d_bottom.slf", "res3d_t3d.slf", "geo_t3d.slf",
    "bc_t3d.cli", "full_listing.log", "telemac_metrics.json",
]


#: The 3D questions that HAVE real geography. A salt wedge is the ANALYTIC
#: lock-exchange V&V (a real estuary would need a tidal liquid boundary), so it
#: is absent here and never takes the real-bathymetry path.
_REAL_BED_MODES = ("stratification", "wind_circulation")


async def write_stratified_deck(
    *,
    aoi: dict[str, Any],
    flow_mode: str = "stratification",
    wind_speed_mps: float = 0.0,
    wind_direction_deg: float = 270.0,
    warm_temp_c: float = 25.0,
    cold_temp_c: float = 15.0,
    thermocline_depth_m: float = 8.0,
    non_hydrostatic: bool = False,
    nplan: int = 13,
    mesh_resolution_m: float | None = None,
    sim_duration_hours: float = 5.0,
    bathy_source: str = "auto",
    bed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize the approved sheet into the worker's 3D config + the run meta."""
    from trid3nt_server.workflows.telemac.run_telemac import TELEMAC3D_SOLVER_NAME
    # Lazily: the template package imports this module, so the labeled
    # defaults are read where they are used rather than at import time.
    from trid3nt_server.workflows.telemac.stratified_flow.declarations import (
        DEFAULT_IDEALIZED_RES_M,
        DEFAULT_REAL_RES_M,
    )

    lake = great_lake_for(float(aoi["lon"]), float(aoi["lat"]))
    real = solves_on_real_bed(bathy_source, domain_kind="lake",
                              lon=aoi["lon"], lat=aoi["lat"],
                              mode=flow_mode, real_bed_modes=_REAL_BED_MODES)
    resolution = (float(mesh_resolution_m) if mesh_resolution_m is not None
                  else (DEFAULT_REAL_RES_M if real else DEFAULT_IDEALIZED_RES_M))

    config: dict[str, Any] = {
        "name": aoi["slug"],
        "flow_mode": str(flow_mode),
        "bathy_source": "noaa_greatlakes" if real else "idealized",
        "wind_speed_mps": float(wind_speed_mps),
        "wind_dir_from_deg": float(wind_direction_deg),
        "warm_temp_c": float(warm_temp_c),
        "cold_temp_c": float(cold_temp_c),
        "thermocline_depth_m": float(thermocline_depth_m),
        "non_hydrostatic": bool(non_hydrostatic),
        "nplan": int(nplan),
        "target_resolution_m": float(resolution),
        "duration_hours": float(sim_duration_hours),
    }
    if real:
        config["bbox"] = [round(float(v), 4) for v in aoi["bbox"]]
    return {
        "config": config,
        "inputs": staged_bed_inputs(bed, real=real, section=_SECTION),
        "run_tag": new_ulid(),
        "section": _SECTION,
        "prefix": _PREFIX,
        "solver": TELEMAC3D_SOLVER_NAME,
        "result_basename": _SURFACE,
        "bottom_basename": _BOTTOM,
        "outputs": list(_OUTPUTS),
        "run_failed_code": "TELEMAC3D_RUN_FAILED",
        "output_missing_code": "TELEMAC3D_OUTPUT_MISSING",
        # Only the REAL-bathymetry lake column is georeferenced. The lock-exchange
        # channel and the idealized closed basin are geography-free by
        # construction and report no zone; the reader keeps the local metres.
        "requires_utm": real,
        "domain_name": aoi["name"],
        "domain_slug": aoi["slug"],
        "mesh_size_m": float(resolution),
        # What the CALLER asked for, kept beside what was built, so a
        # lever the worker moved leaves a row instead of a silence.
        "mesh_resolution_asked_m": (mesh_resolution_m if mesh_resolution_m is not None
                                    else None),
        "flow_mode": str(flow_mode),
        "real_bathymetry": real,
        "wind_speed_mps": float(wind_speed_mps),
        "warm_temp_c": float(warm_temp_c),
        "cold_temp_c": float(cold_temp_c),
        "thermocline_depth_m": float(thermocline_depth_m),
        "nplan": int(nplan),
        "bathy_label": _bathy_label(real, str(flow_mode), lake),
    }


def _bathy_label(real: bool, flow_mode: str, lake: str | None) -> str:
    if real:
        return real_lake_bathy_label(lake)
    if flow_mode == "salt_wedge":
        return ("idealized lock-exchange channel (analytic Benjamin gravity-current "
                "V&V; no real estuary bathymetry)")
    return ("idealized closed basin (no real bathymetry fetched for this AOI; "
            "geography-free verification physics)")


def _provenance(deck: dict[str, Any], metrics: dict[str, Any]) -> list[SyntheticInput]:
    """The physically dominant inputs, as rows the layer carries.

    The wind is what decides the STRATIFICATION answer - calm keeps the
    thermocline, wind mixes it away - so a run that reports a surviving
    temperature difference has to say which of the two it was forced with.
    """
    real = bool(deck["real_bathymetry"])
    calm = float(deck["wind_speed_mps"]) <= 0.0
    rows = [
        SyntheticInput(
            param="wind_speed_mps", value=round(deck["wind_speed_mps"], 1),
            units="m/s", basis="default_demo", consequence="physics",
            note=("calm - the thermocline persists and no wind circulation is driven"
                  if calm else "prescribed steady wind, which mixes the column and "
                               "drives the gyre")),
        SyntheticInput(
            param="bathy_source", value=deck["config"]["bathy_source"],
            basis="fetched" if real else "default_demo", consequence="physics",
            real_source_if_any=("NOAA NGDC Great Lakes lake-datum bathymetry"
                                if real else None),
            note=deck["bathy_label"]),
        SyntheticInput(
            param="nplan", value=int(deck["nplan"]), basis="default_demo",
            consequence="numerical",
            note="vertical sigma levels - the 3D degree of freedom a 2D model has none of"),
    ]
    if deck["flow_mode"] == "stratification":
        rows.append(SyntheticInput(
            param="thermocline",
            value=f"{deck['warm_temp_c']:g}C/{deck['cold_temp_c']:g}C", units="C",
            basis="default_demo", consequence="physics",
            note=(f"prescribed warm epilimnion over cold hypolimnion, thermocline at "
                  f"{deck['thermocline_depth_m']:g} m (no met-forcing fetcher exists). "
                  "No heat exchange: the column can only REDISTRIBUTE its heat, "
                  "never lose it")))
    return rows + mesh_sizing_provenance(deck.get("mesh_resolution_asked_m"), metrics)


def _honesty_note(deck: dict[str, Any]) -> str:
    return (
        "3D structure SCREENING: TELEMAC-3D "
        f"({'non-hydrostatic' if deck['config']['non_hydrostatic'] else 'hydrostatic'}) "
        f"over {deck['nplan']} sigma planes on a {deck['mesh_size_m']:g} m horizontal "
        f"grid of {deck['bathy_label']}, driven by a PRESCRIBED column and a "
        f"{deck['wind_speed_mps']:g} m/s wind - labeled demo forcing, not observed "
        "conditions. The pair of rasters is the SURFACE and BOTTOM field; their "
        "contrast is what a depth-averaged model cannot show. Not a calibrated study."
        + (" The deck exchanges NO heat with the atmosphere: heat is CONSERVED, so a "
           "falling surface temperature is downward MIXING, not the lake cooling."
           if deck["flow_mode"] == "stratification" else ""))


async def publish_stratified_products(*, deck: dict[str, Any],
                                      solve: dict[str, Any]) -> Telemac3dLayerURI:
    """Postprocess the solved column into its surface + bottom layers and scalars.

    The BOTTOM companion is published and emitted; the SURFACE layer is returned,
    and the dispatch seam puts that one on the canvas.
    """
    from trid3nt_server.emission.pipeline_emitter import current_emitter
    from trid3nt_server.workflows.telemac.postprocess_telemac import (
        postprocess_telemac3d,
    )

    emitter = current_emitter()
    run_id = solve["run_id"]
    metrics = dict(solve.get("metrics") or {})
    reach = deck["domain_slug"]
    utm_epsg = solve["utm_epsg"]

    surface = await asyncio.to_thread(
        download_open_water_result, run_id, deck["result_basename"],
        error_code=deck["output_missing_code"])
    bottom = await asyncio.to_thread(
        download_open_water_result, run_id, deck["bottom_basename"],
        error_code=deck["output_missing_code"])
    try:
        layers, _pmetrics = await asyncio.to_thread(
            postprocess_telemac3d, surface, bottom, run_id=run_id,
            utm_epsg=utm_epsg, worker_metrics=metrics, reach_name=reach,
            flow_mode=deck["flow_mode"])
    finally:
        for path in (surface, bottom):
            Path(path).unlink(missing_ok=True)
    if not layers:
        raise OpenWaterError("postprocess_telemac3d produced no layer.",
                             error_code="TELEMAC3D_NO_LAYERS")

    update = {
        "mesh_resolution_label": mesh_resolution_label(
            "real NOAA lake bathy" if deck["real_bathymetry"] else "idealized",
            deck, metrics, suffix=f", {deck['nplan']} sigma planes"),
        "fallback_note": _honesty_note(deck),
        "synthetic_inputs": _provenance(deck, metrics),
        "run_id": run_id,
        # The vertical profile the WORKER measured, initial and final, so the chart
        # shows what the column DID rather than a resampling of the surface map.
        "profile_sigma": list(metrics.get("chart_sigma") or []) or None,
        "profile_values": list(metrics.get("chart_profile") or []) or None,
        "profile_values_initial": list(metrics.get("chart_profile_init") or []) or None,
    }

    # The bottom companion first: it is published and EMITTED here, because only
    # the returned surface layer rides the dispatch seam onto the canvas.
    if len(layers) > 1 and emitter is not None:
        bottom_pub = await publish_product_layer(
            layers[1], style_preset=TELEMAC3D_STRATIFICATION_STYLE_PRESET,
            update={"fallback_note": _honesty_note(deck)})
        try:
            from trid3nt_server.emission.layer_uri_emit import publish_input_layer

            logger.info("telemac3d bottom layer emitted=%s id=%s",
                        await publish_input_layer(emitter, bottom_pub),
                        bottom_pub.layer_id)
        except Exception as exc:  # noqa: BLE001 - a missing companion never voids the pair
            logger.warning("telemac3d bottom emit failed: %s", exc)

    published = await publish_product_layer(
        layers[0], style_preset=TELEMAC3D_STRATIFICATION_STYLE_PRESET, update=update)
    logger.info("telemac3d complete run_id=%s domain=%s mode=%s metric=%.4g "
                "surface_mean=%s bottom_mean=%s uri=%s", run_id, reach,
                deck["flow_mode"], published.stratification_metric,
                metrics.get("surface_value_mean"), metrics.get("bottom_value_mean"),
                published.uri)
    return published


class Stratified:
    """The TELEMAC-3D author + read steps, as the facade binds them."""

    @staticmethod
    def deck(**kwargs: Any) -> Step:
        return Step(runner=f"{_STEPS}.stratified.write_stratified_deck",
                    stage="author", kwargs=kwargs)

    @staticmethod
    def products(*, deck: Any, solve: Any) -> Step:
        return Step(runner=f"{_STEPS}.stratified.publish_stratified_products",
                    stage="publish", kwargs={"deck": deck, "solve": solve})
