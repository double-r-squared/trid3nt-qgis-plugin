"""A solved catchment, answered: the peak depth envelope and the outlet hydrograph.

The hydrograph IS the rainfall-runoff answer - a depth raster says where the
water stood, and the series says how much left the basin and when - and the
ENGINE measured it: the flux across the declared outlet is part of the solver's
own water-volume balance, so the run is narrated from the number it printed
rather than from a second integral computed over its output fields.

Everything past the primary layer is best-effort by contract: a missing
hydrograph or an unpublished results mesh never voids a solve.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Mapping

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import (
    TELEMAC_WSE_STYLE,
    TelemacRainOnGridLayerURI,
)

from trid3nt_server.workflows.runtime import Step
from trid3nt_server.workflows.shared.publish_product_layer import publish_product_layer

logger = logging.getLogger("trid3nt_server.workflows.telemac.products.rain_on_grid")

__all__ = ["RainOnGridProducts", "publish_rain_on_grid_products"]

_PRODUCTS = "trid3nt_server.workflows.telemac.products"

#: Seconds in an hour, spelled once so no expression below spells it again.
_HOUR_S = 3600.0


def _read_listing(run_id: str) -> str:
    """The solver listing the supervisor uploaded; ``""`` on any miss.

    Best-effort by the products contract: the closure the engine printed is a
    scalar the answer carries, never the reason a solved run has no layer.
    """
    from trid3nt_server.workflows.solver.solver import _get_runs_bucket, _get_s3_client

    try:
        body = _get_s3_client().get_object(
            Bucket=_get_runs_bucket(), Key=f"{run_id}/full_listing.log")["Body"].read()
        return body.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - a missing listing costs one scalar
        logger.info("rog: listing unreadable for %s: %s", run_id, exc)
        return ""


def _rain_applied(run: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """What FELL on the catchment: ``(depth_mm, volume_m3)``, either ``None`` when
    unmeasured.

    Gross depth times the meshed area - the same area the runoff left through -
    so the coefficient is a ratio of two figures measured on one domain. The DEPTH
    travels beside the volume because a dry run is narrated in both: millimetres
    are what a reader checks a storm against, cubic metres what the outflow is
    compared to.
    """
    rain, area_km2 = run["rain"], float(run.get("area_km2") or 0.0)
    total_mm = (run.get("hyetograph_total_mm") if rain["kind"] == "hyetograph"
                else float(rain["intensity_mm_per_hr"])
                * float(rain.get("rain_duration_s") or rain["duration_s"]) / _HOUR_S)
    if not area_km2 or total_mm is None:
        return (None if total_mm is None else round(float(total_mm), 4)), None
    return (round(float(total_mm), 4),
            round(float(total_mm) * 1.0e-3 * area_km2 * 1.0e6, 3))


def _provenance(run: Mapping[str, Any]) -> list[SyntheticInput]:
    """The physically dominant inputs, as rows the layer carries.

    Which STORM drove it, which infiltration path ran, where the bed came from and
    whether the mesh was generated or handed in - every one of them a fact the
    answer is meaningless without, so each is stated rather than assumed.
    """
    infiltration, rain = run["infiltration"], run["rain"]
    rows = [
        SyntheticInput(
            param="rain_event", value=str(rain["kind"]), basis=(
                "fetched" if rain["kind"] == "hyetograph" else "default_demo"),
            consequence="scenario",
            real_source_if_any=("NOAA AORC hourly analysis"
                                if rain["kind"] == "hyetograph" else None),
            note=str(rain["note"])),
        SyntheticInput(
            param="sim_duration_hr",
            value=round(float(rain["duration_s"]) / _HOUR_S, 3), units="h",
            basis="user" if rain["duration_basis"] == "user" else "derived",
            consequence="numerical",
            note=("the window you asked for" if rain["duration_basis"] == "user"
                  else "the fetched hyetograph's own span"
                  if rain["duration_basis"] == "hyetograph"
                  else "the design storm's own duration")),
        SyntheticInput(
            param="antecedent_moisture", value=int(infiltration["amc_condition"]),
            basis="user", consequence="physics",
            note=("the SCS antecedent-moisture condition the curve numbers are "
                  "converted under - the dominant infiltration lever")),
        SyntheticInput(
            param="runoff_path", value=str(run["runoff_path"]), basis="derived",
            consequence="physics", note=str(run["runoff_reason"])),
        SyntheticInput(
            param="mesh_domain",
            value=f"{run['catchment'].get('element_count') or 0} elements over "
                  f"{float(run.get('area_km2') or 0.0):.3g} km2",
            basis="derived", consequence="numerical",
            real_source_if_any=str(run.get("domain_source") or "") or None,
            note=("the catchment was delineated at the pour point and meshed for "
                  "this run")),
    ]
    if infiltration["curve_number"] is not None:
        rows.append(SyntheticInput(
            param="curve_number", value=float(infiltration["curve_number"]),
            basis="user", consequence="physics",
            note=("a UNIFORM curve number overriding the land-cover-distributed "
                  "field; roughness is still per-node")))
    rows.append(SyntheticInput(
        param="mesh_bed", value=str(run.get("bed_source") or "staged"),
        basis="fetched", consequence="physics",
        real_source_if_any="USGS 3DEP",
        note=str(run.get("bed_note") or
                 "the bare-earth bed the mesher painted every node from")))
    if run.get("sizing_source"):
        rows.append(SyntheticInput(
            param="mesh_sizing_source", value=str(run["sizing_source"]),
            basis="fetched", consequence="numerical",
            note="the channel network the mesh refinement was sized by distance to"))
    return rows


def _dryness_note(scalars: Mapping[str, Any], *, rain_mm: float | None) -> str:
    """The measured DRYNESS in the run's own numbers, or ``""`` when it ran off.

    A correct solve over a catchment that shed no water is a FINDING - the storm
    infiltrated - and stating it takes the three figures a reader would otherwise
    go looking for: how deep the water got, how much rain the domain took, how
    much left the outlet. So the sentence carries them rather than announcing an
    absence.

    The DEPTH FIELD is what decides, on the same wet floor the raster is masked
    at: a catchment that never held a centimetre anywhere is the dry answer even
    when the solver's own balance passed a trace of water across the outlet, and
    hanging the finding on an exactly-zero outflow instead would hide it behind
    a rounding.
    """
    from trid3nt_server.workflows.telemac.products.postprocess_telemac import (
        TELEMAC_WSE_WET_DEPTH_M,
    )

    depth = float(scalars.get("max_depth_peak_m") or 0.0)
    if depth > TELEMAC_WSE_WET_DEPTH_M:
        return ""
    runoff, rain_m3 = scalars.get("runoff_volume_m3"), scalars.get(
        "rainfall_volume_m3")
    applied = (f"{float(rain_mm):.4g} mm of rain" if rain_mm is not None
               else "the storm")
    over = (f" ({float(rain_m3):.4g} m3 over the meshed catchment)"
            if rain_m3 is not None else "")
    outflow = (f"{float(runoff):.4g} m3" if runoff is not None
               else "a volume the solver listing could not be read for")
    return (
        f" MEASURED DRY: the peak water depth was {depth:.4g} m - nowhere did the "
        f"water clear the {TELEMAC_WSE_WET_DEPTH_M} m wet floor - and {applied}"
        f"{over} left {outflow} through the outlet. The storm infiltrated, and "
        "that is this run's answer rather than a missing result: a wetter "
        "antecedent condition, a larger storm or a lower curve number is what "
        "moves it.")


def _honesty_note(run: Mapping[str, Any], metrics: Mapping[str, Any],
                  product_note: str | None, truncated: bool = False,
                  dryness: str = "") -> str:
    """What the RUN was, prefixed by what the LAYER is.

    The applicability envelope is part of the sentence, not a footnote: rain-on-
    grid reproduces single-storm flash floods in small steep catchments and does
    NOT carry baseflow, because infiltrated water is permanently lost.

    A hydrograph still rising at the last sample gets its own sentence, because
    every number the run reports about the storm is then a floor rather than a
    measurement, and that is not a caveat a reader should have to derive from a
    time series. A run that stayed dry gets one for the same reason.
    """
    rain = run["rain"]
    spacing = metrics.get("mesh_size_m") or run["mesh_size_m"]
    truncation = (
        " WINDOW-TRUNCATED: the outlet discharge was still RISING when the "
        "simulated window closed, so the peak, the runoff volume and the runoff "
        "coefficient are LOWER BOUNDS - simulate past the storm to close them."
        if truncated else "")
    return (
        (f"{product_note} " if product_note else "")
        + "Planning-grade rainfall-runoff SCREENING: TELEMAC-2D shallow water over a "
        f"{float(run.get('area_km2') or 0.0):.3g} km2 catchment delineated at the "
        f"pour point and triangulated at {float(spacing):g} m minimum edge "
        f"({run['catchment'].get('element_count') or 0} elements), infiltrating by "
        "the SCS curve-number method with per-node curve numbers from land cover. "
        "Driven by "
        + str(rain["note"]).rstrip(".").split(" - ")[0]
        + ". The raster is the peak water DEPTH envelope over the run; the animation "
        "plays from the native rain-on-grid SELAFIN. Single-storm events only: "
        "infiltrated water is permanently lost, so there is no subsurface return "
        "flow and no inter-peak baseflow. Not a calibrated rainfall-runoff model."
        + truncation + dryness)


async def publish_rain_on_grid_products(*, run: dict[str, Any],
                                        solve: dict[str, Any],
                                        ) -> TelemacRainOnGridLayerURI:
    """Postprocess the solved catchment into its published layers + scalars."""
    from trid3nt_server.emission.pipeline_emitter import current_emitter
    from trid3nt_server.workflows.telemac.authoring.open_water import (
        download_open_water_result,
    )
    from trid3nt_server.workflows.telemac.products.postprocess_telemac import (
        postprocess_telemac_wse,
    )
    from trid3nt_server.workflows.telemac.products.run_reads import (
        continuity_rel_error,
        outlet_hydrograph,
    )
    from trid3nt_server.workflows.telemac.results_mesh_seam import (
        publish_results_mesh_via_seam,
    )

    emitter = current_emitter()
    run_id, utm_epsg = solve["run_id"], int(solve["utm_epsg"])
    metrics = dict(solve.get("metrics") or {})
    catchment = run["catchment"]
    name = str(run["domain_name"])

    slf_path = await asyncio.to_thread(
        download_open_water_result, run_id, run["result_basename"],
        error_code="TELEMAC_ROG_OUTPUT_MISSING")
    try:
        layers, pmetrics = await asyncio.to_thread(
            postprocess_telemac_wse, slf_path, run_id=run_id,
            mesh_epsg=utm_epsg, reach_name=name, quantity="depth",
            mesh_frame_note="rain-on-grid peak water depth (UTM mesh frame)")
    finally:
        Path(slf_path).unlink(missing_ok=True)
    raw = layers[0]

    listing = await asyncio.to_thread(_read_listing, run_id)
    hydrograph = await asyncio.to_thread(
        outlet_hydrograph, listing, boundary=run["outlet_boundary"])
    rain_mm, rainfall = _rain_applied(run)
    runoff = hydrograph.get("runoff_volume_m3")
    scalars: dict[str, Any] = {
        "catchment_area_km2": round(float(run.get("area_km2") or 0.0), 4),
        "peak_discharge_m3s": hydrograph.get("peak_discharge_m3s"),
        "peak_discharge_time_s": hydrograph.get("peak_discharge_time_s"),
        "peak_is_window_truncated": hydrograph.get("peak_is_window_truncated"),
        "rainfall_volume_m3": rainfall,
        "runoff_volume_m3": runoff,
        # A ratio, not a percentage, and only when there was rain to divide by:
        # a runoff coefficient over zero rainfall is a number with no meaning.
        "runoff_coefficient": (round(float(runoff) / float(rainfall), 6)
                               if rainfall and runoff is not None
                               and float(rainfall) > 0.0 else None),
        "max_depth_peak_m": pmetrics.get("wse_max_m"),
        "max_depth_p99_m": pmetrics.get("wse_p99_m"),
        "continuity_rel_error": continuity_rel_error(listing),
        "runoff_path": run["runoff_path"],
        "amc_condition": int(run["infiltration"]["amc_condition"]),
        "rain_intensity_mm_per_hr": float(run["rain"]["intensity_mm_per_hr"]),
        "outlet_hydrograph_t_s": list(hydrograph.get("t_s") or ()) or None,
        "outlet_hydrograph_q_m3s": list(hydrograph.get("q_m3s") or ()) or None,
        "mesh_node_count": int(catchment.get("node_count") or 0) or None,
        "mesh_element_count": int(catchment.get("element_count") or 0) or None,
        "mesh_size_m": float(run["mesh_size_m"]),
        "mesh_resolution_label": (
            f"catchment TIN, {float(run['mesh_size_m']):g} m minimum edge to "
            f"{float(run['mesh_max_edge_m']):g} m, refined toward the channel "
            f"network ({catchment.get('element_count') or 0} elements)"),
        "catchment_provenance": str(run.get("domain_source") or ""),
        "catchment_name": name,
        "domain_bbox": [float(v) for v in run["lonlat_bounds"]],
    }
    typed = TelemacRainOnGridLayerURI(**raw.model_dump(), **scalars)
    published = await publish_product_layer(
        typed, style=TELEMAC_WSE_STYLE,
        update={
            # The published raster is in the mesh's UTM metres, so the postprocess
            # leaves it without a zoom-to extent; the DOMAIN's own 4326 bounds are
            # known here and the camera follows the domain.
            "bbox": tuple(run["lonlat_bounds"]),
            "fallback_note": _honesty_note(
                run, metrics, raw.fallback_note,
                truncated=bool(scalars["peak_is_window_truncated"]),
                dryness=_dryness_note(scalars, rain_mm=rain_mm)),
            "synthetic_inputs": _provenance(run),
            # The run prefix travels WITH the layer so the skeleton writes this
            # run's own chart spec and answer metrics under it.
            "run_id": run_id,
        })

    # EMIT-ON-SOLVE: outputs.json carries the peak entry plus the SELAFIN mesh
    # entry, and the seam owns publication of the temporal artifact. ``raw`` (the
    # unpublished s3 COG) is what the whole-run record points at, as on the other
    # two families.
    await publish_results_mesh_via_seam(
        emitter, run_id=run_id, engine="telemac", peak_layer=raw,
        peak_quantity="flood_depth", mesh_basename=run["result_basename"],
        mesh_epsg=utm_epsg, reach_name=name)

    # The NOTE rides the log line: it is where a dry run states its finding, and
    # the layer envelope the wire carries has no field for it.
    logger.info("rog complete run_id=%s catchment=%s area=%.4g km2 outlet_boundary=%d "
                "peak_q=%s peak_depth=%s continuity=%s uri=%s note=%s", run_id, name,
                float(run.get("area_km2") or 0.0), run["outlet_boundary"],
                published.peak_discharge_m3s, published.max_depth_peak_m,
                published.continuity_rel_error, published.uri,
                published.fallback_note)
    return published


class RainOnGridProducts:
    """The solved catchment's deliverable, as the facade binds it."""

    @staticmethod
    def flood_depth(*, run: Any, solve: Any) -> Step:
        """The peak depth envelope and the outlet hydrograph it is narrated by."""
        return Step(runner=f"{_PRODUCTS}.rain_on_grid.publish_rain_on_grid_products",
                    stage="publish", kwargs={"run": run, "solve": solve})
