"""The COASTAL deck and the coastal deliverable: a stage series in, inundation out.

One serialization hook and one publisher for the TELEMAC-2D coastal domain: a
regular grid over the AOI with real NOAA DEM_all topobathy at the nodes, ONE
seaward liquid boundary driven in time by a water-level series through the LIQUID
BOUNDARIES FILE (SL(1)), and SAINT-VENANT + TIDAL FLATS wetting/drying flooding
the low coast as the boundary stage rises.

Everything staging, dispatching and reading the run is the shared open-water
front (``steps/open_water.py``); what lives here is only what is COASTAL about a
coastal run.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import (
    TELEMAC_COASTAL_DEPTH_STYLE_PRESET,
    TelemacCoastalLayerURI,
)

from trid3nt_server.workflows.lib import Step

from .open_water import (
    OpenWaterError,
    download_open_water_result,
    publish_peak_layer,
    surface_in_worker_bed_input,
)

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.coastal")

__all__ = ["publish_coastal_products", "write_coastal_deck"]

_STEPS = "trid3nt_server.workflows.telemac.steps"

#: The worker's manifest section and the artifacts the supervisor uploads for it.
_SECTION = "coastal"
_RESULT = "res_coastal.slf"
_OUTPUTS = [
    "res_coastal.slf", "geo_coastal.slf", "bc_coastal.cli", "t2d_coastal.cas",
    "coastal_liquid_bnd.txt", "full_listing.log", "bed_bathymetry.tif",
    "telemac_metrics.json",
]

#: The default simulated window when neither a duration nor a series span says
#: otherwise: 30 hours, about two full tidal cycles.
_FALLBACK_DURATION_S = 108000.0


async def write_coastal_deck(
    *,
    aoi: dict[str, Any],
    water_level: dict[str, Any] | None = None,
    mesh_resolution_m: float = 180.0,
    datum_offset_m: float = 0.0,
    ocean_edge: str = "auto",
    duration_hours: float | None = None,
    time_step_s: float = 20.0,
    bathy_source: str = "noaa_demall",
    output_interval_min: float | None = None,
) -> dict[str, Any]:
    """Serialize the approved sheet into the worker's coastal config + the run meta.

    The returned value carries what SOLVES it - solver name, manifest section,
    result file, outputs - so the shared open-water dispatch needs to know nothing
    coastal. A ``synthetic`` bathymetry run drives an analytic plane beach and
    carries no series at all: the deterministic offline path.
    """
    from trid3nt_server.workflows.telemac.run_telemac import TELEMAC_COASTAL_SOLVER_NAME

    synthetic = str(bathy_source).strip().lower() == "synthetic"
    series = list((water_level or {}).get("series") or [])
    if not synthetic and not series:
        raise OpenWaterError(
            "the coastal domain has no water-level series to drive its seaward "
            "boundary; supply a station and window, or ask for the synthetic "
            "plane-beach bed.", error_code="COASTAL_TIDE_EMPTY")
    duration_s = (float(duration_hours) * 3600.0 if duration_hours
                  else (float(series[-1][0]) if series else _FALLBACK_DURATION_S))
    datum = str((water_level or {}).get("series_datum") or "MLLW")

    config: dict[str, Any] = {
        "name": aoi["slug"],
        "bbox": [round(float(v), 4) for v in aoi["bbox"]],
        "bathy_source": "synthetic" if synthetic else "noaa_demall",
        "target_resolution_m": float(mesh_resolution_m),
        "ocean_edge": str(ocean_edge or "auto"),
        "series_datum": datum,
        "datum_offset_m": float(datum_offset_m),
        "duration_s": float(duration_s),
        "time_step_s": float(time_step_s),
    }
    # The output cadence rides ONLY when it was asked for, so an unasked run
    # stays byte-identical to the worker's computed ~40-frame default.
    if output_interval_min is not None:
        config["output_interval_min"] = float(output_interval_min)
    if series:
        config["water_level_series"] = series
    return {
        "config": config,
        "run_tag": new_ulid(),
        "section": _SECTION,
        "solver": TELEMAC_COASTAL_SOLVER_NAME,
        "result_basename": _RESULT,
        "outputs": list(_OUTPUTS),
        "run_failed_code": "COASTAL_RUN_FAILED",
        "output_missing_code": "COASTAL_OUTPUT_MISSING",
        "domain_name": aoi["name"],
        "domain_slug": aoi["slug"],
        "domain_bbox": tuple(float(v) for v in aoi["bbox"]),
        "mesh_size_m": float(mesh_resolution_m),
        "series_datum": datum,
        "datum_offset_m": float(datum_offset_m),
        "series_type": str((water_level or {}).get("series_type") or "observed"),
        "station_id": (water_level or {}).get("station_id"),
        "station_name": (water_level or {}).get("station_name"),
        "series_window": (water_level or {}).get("window"),
        "series_points": len(series),
    }


def _provenance(deck: dict[str, Any], metrics: dict[str, Any]) -> list[SyntheticInput]:
    """The physically dominant inputs, as rows the layer carries.

    Which SERIES drove the boundary and which gauge it came from (the answer is
    meaningless without them), and the datum offset that reconciled the tide datum
    with the DEM's - a labeled physics knob, so it is stated rather than assumed.
    """
    observed = deck["series_type"] == "observed"
    rows = [
        SyntheticInput(
            param="series_type", value=deck["series_type"], basis="user",
            consequence="scenario",
            note=("OBSERVED storm-surge record" if observed
                  else "astronomical PREDICTION (the calm-tide control)")),
        SyntheticInput(
            param="datum_offset_m", value=round(float(deck["datum_offset_m"]), 3),
            units="m", basis="user", consequence="physics",
            note=(f"reconciles the {deck['series_datum']} tide datum with the "
                  "DEM_all (~MSL) datum")),
    ]
    if deck.get("station_id"):
        rows.append(SyntheticInput(
            param="station", value=str(deck["station_id"]), basis="fetched",
            consequence="physics",
            real_source_if_any="NOAA CO-OPS water-level gauge",
            note=(f"{deck.get('station_name') or 'CO-OPS gauge'}, "
                  f"{deck.get('series_points')} points over "
                  f"{deck.get('series_window')}")))
    if metrics.get("ocean_edge"):
        rows.append(SyntheticInput(
            param="ocean_edge", value=str(metrics["ocean_edge"]), basis="derived",
            consequence="numerical",
            note="the bbox edge the seaward liquid boundary was placed on"))
    return rows


def _honesty_note(deck: dict[str, Any]) -> str:
    observed = deck["series_type"] == "observed"
    return (
        "Planning-grade coastal inundation SCREENING: TELEMAC-2D shallow water "
        "with TIDAL FLATS wetting/drying over a regular "
        f"{deck['mesh_size_m']:g} m grid of real NOAA DEM_all topobathy, driven at "
        "ONE seaward liquid boundary by the "
        + ("OBSERVED CO-OPS water-level record (tide + surge)" if observed
           else "astronomical CO-OPS PREDICTION (calm tide, no surge)")
        + f" through the LIQUID BOUNDARIES FILE, {deck['series_datum']} datum with a "
        f"{deck['datum_offset_m']:g} m offset applied. The raster is the peak "
        "inundation DEPTH envelope over the run; the animation plays from the "
        "native coastal SELAFIN. Not a calibrated hindcast.")


async def publish_coastal_products(*, deck: dict[str, Any],
                                   solve: dict[str, Any]) -> TelemacCoastalLayerURI:
    """Postprocess the solved coastal domain into its published layers + scalars."""
    from trid3nt_server.emission.pipeline_emitter import current_emitter
    from trid3nt_server.workflows.telemac.postprocess_telemac import postprocess_coastal
    from trid3nt_server.workflows.telemac.results_mesh_seam import (
        publish_results_mesh_via_seam,
    )

    emitter = current_emitter()
    run_id, utm_epsg = solve["run_id"], int(solve["utm_epsg"])
    metrics = dict(solve.get("metrics") or {})
    reach = deck["domain_slug"]

    # The worker knows the grid and the boundary; the template knows the question.
    # Both end up on the layer, so the reader sees which series drove which bed.
    metrics["series_type"] = deck["series_type"]
    metrics.setdefault("series_datum", deck["series_datum"])
    metrics.setdefault("datum_offset_m", float(deck["datum_offset_m"]))
    if deck.get("station_id"):
        metrics.setdefault("station_id", deck["station_id"])
        metrics.setdefault("station_name", deck["station_name"])

    slf_path = await asyncio.to_thread(
        download_open_water_result, run_id, deck["result_basename"],
        error_code=deck["output_missing_code"])
    try:
        layers, _pmetrics = await asyncio.to_thread(
            postprocess_coastal, slf_path, run_id=run_id, utm_epsg=utm_epsg,
            domain_bbox=deck["domain_bbox"], reach_name=reach,
            worker_metrics=metrics)
    finally:
        Path(slf_path).unlink(missing_ok=True)
    if not layers:
        raise OpenWaterError("postprocess_coastal produced no layer.",
                             error_code="COASTAL_NO_LAYERS")
    raw = layers[0]

    published = await publish_peak_layer(
        raw, style_preset=TELEMAC_COASTAL_DEPTH_STYLE_PRESET,
        update={
            "mesh_resolution_label": (
                f"real NOAA DEM_all topobathy grid "
                f"{metrics.get('dx_m', deck['mesh_size_m']):g} m"
                + (" (coarsened under node budget)" if metrics.get("coarsened") else "")),
            "fallback_note": _honesty_note(deck),
            "synthetic_inputs": _provenance(deck, metrics),
            # The run prefix travels WITH the layer so the skeleton writes this
            # run's own chart spec + answer metrics under it.
            "run_id": run_id,
        })

    # EMIT-ON-SOLVE: outputs.json carries the peak entry plus the SELAFIN mesh
    # entry, and the seam owns publication of the rising-tide temporal artifact.
    # The typed peak above stays this step's own. ``raw`` (the unpublished s3 COG)
    # is what the whole-run record points at, as it does on the reach family.
    await publish_results_mesh_via_seam(
        emitter, run_id=run_id, engine="telemac", peak_layer=raw,
        peak_quantity="flood_depth", mesh_basename=deck["result_basename"],
        mesh_epsg=utm_epsg, reach_name=reach)

    await surface_in_worker_bed_input(
        emitter, run_metrics=metrics, run_id=run_id,
        name=(f"Input: coastal bed bathymetry ({reach}, NOAA DEM_all topobathy, "
              "in-worker)"),
        layer_id_prefix="input-coastal-bed")

    # The peak layer is RETURNED, and the dispatch seam materializes what a tool
    # returns - so there is no emit call here. One seam, one emission.
    logger.info("telemac coastal complete run_id=%s domain=%s peak_depth=%.4g "
                "flooded_land=%.4g km2 sl_peak=%s uri=%s", run_id, reach,
                published.peak_depth_m, published.flooded_land_km2,
                published.sl_peak_m, published.uri)
    return published


class Coastal:
    """The coastal author + read steps, as the facade binds them."""

    @staticmethod
    def deck(**kwargs: Any) -> Step:
        return Step(runner=f"{_STEPS}.coastal.write_coastal_deck", stage="author",
                    kwargs=kwargs)

    @staticmethod
    def products(*, deck: Any, solve: Any) -> Step:
        return Step(runner=f"{_STEPS}.coastal.publish_coastal_products",
                    stage="publish", kwargs={"deck": deck, "solve": solve})
