"""The PRODUCTS step: a solved reach -> the map layers, the scalars, the chart spec.

The peak concentration COG is the map anchor and the narration carrier; the
native result SELAFIN beside it is the TEMPORAL artifact the client animates. A
sediment run adds the signed bed-evolution map, an oil run the floating-slick
track, and every run surfaces the bed bathymetry the worker actually solved on.

Everything past the primary layer is best-effort by contract: failure retracts
nothing, so a missing deposition COG or slick never voids the concentration
layer.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import (
    TELEMAC_DYE_STYLE_PRESET,
    TelemacDyeLayerURI,
)

from trid3nt_server.declarative import Step
from trid3nt_server.data.publish_layer.publish_layer import PublishLayerError, publish_layer

from .errors import TelemacDyeScenarioError
from .solve import download_result_selafin

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.products")

__all__ = ["Products", "build_dye_chart", "publish_do_products", "publish_dye_products"]

_STEPS = "trid3nt_server.workflows.telemac.steps"

#: DEM-source label for the bed-COG provenance name (the worker records which DEM
#: rung actually sampled the bed).
_BED_DEM_SOURCE_LABELS: dict[str, str] = {
    "cop-dem-glo-30": "Copernicus GLO-30",
    "usgs-3dep": "USGS 3DEP",
}


def _s3_object_exists(s3: Any, bucket: str, key: str) -> bool:
    """True when the object physically exists.

    The upload-before-register guard: a fabricated URI is only safe to register
    once the object is confirmed present, so any error reads as absent.
    """
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001 -- absent / unreachable == do not register
        return False


def _provenance(solve: dict[str, Any],
                discharge: dict[str, Any]) -> list[SyntheticInput]:
    """The two physically dominant inputs, as rows the layer carries.

    The carrier discharge that governs dilution (real NWM streamflow or
    user-supplied) and the bank geometry the worker actually sampled (real
    NHDArea polygons vs an assumed constant-width ribbon).
    """
    banks = solve.get("bank_provenance") or "constant_ribbon"
    return [
        SyntheticInput(
            param="discharge_m3s", value=round(float(discharge["m3s"]), 1),
            units="m3/s", basis=discharge.get("basis") or "fetched",
            real_source_if_any=discharge.get("real_source"),
            note="carrier discharge governs dilution/transport"),
        SyntheticInput(
            param="bank_geometry", value=banks,
            basis="fetched" if banks == "nhd_area" else "default_demo",
            consequence="physics",
            real_source_if_any=("USGS NHDArea water polygons"
                                if banks == "nhd_area" else None),
            note=(None if banks == "nhd_area"
                  else "assumed constant-width ribbon, not surveyed banks")),
    ]


def _honesty_note(location_name: str, substance: str, bank_source: str) -> str:
    surrogate = ""
    if substance and substance != "dye":
        surrogate = (
            f" NOTE: {substance} is modeled as a passively advected dissolved "
            "tracer (transport + dilution only) - NOT slick physics "
            "(no spreading/evaporation/weathering/beaching).")
    banks_note = (
        " Banks: real USGS NHDArea water-polygon geometry (per-station sampled "
        "widths)." if bank_source == "nhd_area"
        else " Banks: an ASSUMED constant channel-width ribbon (bank_source="
             "constant_ribbon), not real surveyed banks.")
    return (
        f"Idealized demo: a FINITE mid-reach point-source {substance or 'dye'} "
        f"pulse released on the real {location_name} river reach (NLDI/NHDPlus "
        "geometry) over a planar idealized channel bed with prescribed tracer "
        "dispersion. The raster is the PEAK concentration envelope over the run; "
        "the animation plays from the native SELAFIN mesh. Not a calibrated site "
        "study." + banks_note + surrogate)


def _publish_peak_layer(raw_peak: TelemacDyeLayerURI, run_id: str,
                        location_name: str, mesh_meta: dict[str, Any],
                        substance: str, bank_source: str,
                        synthetic_inputs: list[SyntheticInput]) -> TelemacDyeLayerURI:
    """Publish the peak COG through the one styling chokepoint and enrich narration.

    On publish failure the RAW peak is returned unchanged - its s3 COG still lets
    the case discover the SELAFIN sibling, and the dispatch-level guardrail owns
    the map honesty.
    """
    honesty = _honesty_note(location_name, substance, bank_source)
    update = {**mesh_meta, "synthetic_inputs": list(synthetic_inputs)}
    if raw_peak.layer_type != "raster" or not raw_peak.uri.startswith(("gs://", "s3://")):
        return raw_peak.model_copy(update={"fallback_note": honesty, **update})
    layer_id = f"telemac-dye-peak-{run_id}"
    try:
        published_uri = publish_layer(
            layer_uri=raw_peak.uri, layer_id=layer_id,
            style_preset=raw_peak.style_preset or TELEMAC_DYE_STYLE_PRESET)
    except PublishLayerError as exc:
        logger.warning("telemac: publish_layer FAILED layer_id=%s error_code=%s (%s) "
                       "- returning the unpublished peak.", layer_id, exc.error_code, exc)
        return raw_peak.model_copy(update={"fallback_note": honesty, **update})
    return raw_peak.model_copy(update={
        "layer_id": layer_id, "uri": published_uri,
        "style_preset": raw_peak.style_preset or TELEMAC_DYE_STYLE_PRESET,
        "fallback_note": honesty, **update})


async def _surface_bed_bathymetry_input(emitter: Any, metrics: dict[str, Any],
                                  run_id: str, reach_name: str) -> bool:
    """Surface the bed bathymetry the worker sampled, as a context input layer.

    The bed is fetched + fitted INSIDE the container, so the honest surfacing path
    is the worker-envelope seam: the worker writes the bed it solved on next to
    the result and records the key. NEVER raises - a missing bed COG must not void
    a solve.
    """
    bed_cog = metrics.get("bed_cog")
    if emitter is None or not bed_cog:
        return False
    try:
        from trid3nt_server.data.simulation.solver.solver import _get_runs_bucket
        from trid3nt_server.emission.layer_uri_emit import publish_raster_input_cog

        source_label = _BED_DEM_SOURCE_LABELS.get(
            str(metrics.get("bed_cog_source") or ""), "3DEP/Copernicus")
        return await publish_raster_input_cog(
            emitter, cog_uri=f"s3://{_get_runs_bucket()}/{run_id}/{bed_cog}",
            layer_id=f"input-river-bed-{new_ulid()}",
            name=(f"Input: river bed bathymetry ({reach_name}, "
                  f"{source_label}-sampled, in-worker)"),
            style_preset="continuous_dem", role="context")
    except Exception as exc:  # noqa: BLE001 - input surfacing is NEVER fatal
        logger.warning("telemac bed input absent (the solve is unaffected): %s", exc)
        return False


def _download_gaia(run_id: str) -> str | None:
    """Download ``gaia_river.slf``; ``None`` when the run wrote none (fail-open)."""
    from trid3nt_server.data.simulation.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
    )

    gaia_path = str(Path(tempfile.mkdtemp(prefix=f"telemac-gaia-{run_id}-"))
                    / "gaia_river.slf")
    try:
        resp = _get_s3_client().get_object(Bucket=_get_runs_bucket(),
                                           Key=f"{run_id}/gaia_river.slf")
        with open(gaia_path, "wb") as fh:
            fh.write(resp["Body"].read())
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemac sediment: gaia_river.slf missing for %s (%s) - "
                       "deposition COG skipped", run_id, exc)
        return None
    return gaia_path


async def _fold_sediment_products(peak: TelemacDyeLayerURI, *, run_id: str,
                                  utm_epsg: int, reach_name: str,
                                  worker_metrics: dict[str, Any],
                                  erodible: bool, emitter: Any) -> TelemacDyeLayerURI:
    """Fold GAIA's own mass-balance scalars onto the peak + emit the deposition map.

    ``deposited_mass_kg`` is the NET bed mass, clamped at zero - the SAME net
    quantity the deposition map and the deposit fraction integrate. Never the
    GROSS deposition: in a supply-limited run gross deposition can equal gross
    erosion with net ~0, so the map is correctly empty and the narrated mass must
    match it.
    """
    from trid3nt_server.workflows.telemac.postprocess_telemac import (
        PostprocessTelemacError,
        postprocess_telemac_deposition,
    )

    net = worker_metrics.get("sediment_net_bed_mass_kg")
    peak = peak.model_copy(update={
        "deposited_mass_kg": max(float(net), 0.0) if net is not None else None,
        "deposit_fraction": worker_metrics.get("sediment_deposit_fraction"),
        "max_deposition_mm": worker_metrics.get("sediment_max_deposition_mm"),
        "sediment_n_classes": worker_metrics.get("sediment_n_classes"),
        "sediment_surface_d50_min_um": worker_metrics.get("sediment_surface_d50_min_um"),
        "sediment_surface_d50_max_um": worker_metrics.get("sediment_surface_d50_max_um"),
        "sediment_surface_d50_range_um":
            worker_metrics.get("sediment_surface_d50_range_um"),
    })
    gaia_path = await asyncio.to_thread(_download_gaia, run_id)
    if not gaia_path:
        return peak
    try:
        dep_layers, dep_metrics = await asyncio.to_thread(
            postprocess_telemac_deposition, gaia_path, run_id=run_id,
            utm_epsg=utm_epsg, reach_name=reach_name,
            worker_sed_metrics=worker_metrics, erodible=bool(erodible))
    except (PostprocessTelemacError, TelemacDyeScenarioError) as exc:
        logger.warning("sediment deposition postprocess failed (%s) - the "
                       "concentration COG still stands", exc)
        return peak
    finally:
        Path(gaia_path).unlink(missing_ok=True)

    if erodible and dep_metrics.get("max_scour_mm") is not None:
        peak = peak.model_copy(update={"max_scour_mm": dep_metrics["max_scour_mm"]})
    if not dep_layers or emitter is None:
        return peak
    dep_raw = dep_layers[0]
    try:
        pub_uri = await asyncio.to_thread(
            publish_layer, layer_uri=dep_raw.uri, layer_id=dep_raw.layer_id,
            style_preset=dep_raw.style_preset)
        dep_pub = dep_raw.model_copy(update={"uri": pub_uri})
    except PublishLayerError as exc:
        logger.warning("sediment deposition publish failed (%s) - emitting the "
                       "unpublished COG", exc)
        dep_pub = dep_raw
    from trid3nt_server.emission.layer_uri_emit import publish_input_layer

    emitted = await publish_input_layer(emitter, dep_pub)
    logger.info("sediment deposition layer emitted=%s id=%s max_dep_mm=%s "
                "deposited_kg=%s", emitted, dep_pub.layer_id,
                dep_pub.max_deposition_mm, dep_pub.deposited_mass_kg)
    return peak


async def _emit_oil_slick(peak: TelemacDyeLayerURI, *, run_id: str, reach_name: str,
                          oil_preset: Any, emitter: Any) -> None:
    """Emit the floating-slick track, but only once the object is confirmed present.

    The worker's fail-open drogues parse can leave the slick unwritten; registering
    the URI regardless once produced a dangling layer handle, so a missing slick
    reads as an honest skip instead.
    """
    if emitter is None:
        return
    try:
        from trid3nt_contracts.execution import LayerURI

        from trid3nt_server.data.simulation.solver.solver import (
            _get_runs_bucket,
            _get_s3_client,
        )
        from trid3nt_server.emission.layer_uri_emit import publish_input_layer

        bucket, key = _get_runs_bucket(), f"{run_id}/slick.geojson"
        if not await asyncio.to_thread(_s3_object_exists, _get_s3_client(), bucket, key):
            logger.warning("oil slick object absent (s3://%s/%s not written by the "
                           "worker) - slick layer skipped, no dangling handle emitted",
                           bucket, key)
            return
        layer = LayerURI(
            layer_id=f"telemac-oil-slick-{run_id}",
            name=f"Oil slick track ({oil_preset}, {reach_name})",
            layer_type="vector", uri=f"s3://{bucket}/{key}",
            style_preset="nhdplus_flowlines", role="primary", bbox=peak.bbox)
        logger.info("oil slick layer emitted=%s id=%s",
                    await publish_input_layer(emitter, layer), layer.layer_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("oil slick layer skipped: %s", exc)


async def publish_dye_products(*, deck: dict[str, Any], solve: dict[str, Any],
                               carrier_discharge: dict[str, Any]) -> TelemacDyeLayerURI:
    """Postprocess the solved reach into its published layers + narration scalars."""
    from trid3nt_server.emission.pipeline_emitter import current_emitter
    from trid3nt_server.workflows.telemac.postprocess_telemac import postprocess_telemac
    from trid3nt_server.workflows.telemac.results_mesh_seam import (
        publish_results_mesh_via_seam,
    )

    emitter = current_emitter()
    run_id, utm_epsg = solve["run_id"], int(solve["utm_epsg"])
    reach_name, substance = deck["reach_name"], deck["substance"]
    slf_path, _ = await asyncio.to_thread(download_result_selafin, run_id)

    await _surface_bed_bathymetry_input(emitter, solve.get("metrics") or {},
                                  run_id, reach_name)
    try:
        layers, _metrics = await asyncio.to_thread(
            postprocess_telemac, slf_path, run_id=run_id, utm_epsg=utm_epsg,
            reach_name=reach_name, substance=substance,
            substance_class=deck["substance_class"])
    finally:
        Path(slf_path).unlink(missing_ok=True)

    if not layers:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_NO_LAYERS",
            "postprocess_telemac produced no dye layer (empty tracer field?).")
    raw_peak = layers[0]

    mesh_meta = {
        "mesh_size_m": deck["mesh_size_m"],
        "mesh_node_estimate": deck["mesh_node_estimate"],
        "mesh_resolution_label": deck["mesh_resolution_label"],
    }
    peak = await asyncio.to_thread(
        _publish_peak_layer, raw_peak, run_id, deck["location_name"], mesh_meta,
        substance, solve.get("bank_provenance") or "constant_ribbon",
        _provenance(solve, carrier_discharge))

    # EMIT-ON-SOLVE: outputs.json carries the peak entry (the whole-run record)
    # plus the SELAFIN mesh entry, and the seam owns publication of the temporal
    # artifact. The typed peak above stays this step's own.
    await publish_results_mesh_via_seam(
        emitter, run_id=run_id, engine="telemac", peak_layer=raw_peak,
        peak_quantity="dye_concentration", mesh_basename="r2d_river.slf",
        mesh_epsg=utm_epsg, reach_name=reach_name)

    logger.info("telemac reach complete run_id=%s reach=%s dye_cmax_mgl=%.4g "
                "plume_reach_m=%s active_frames=%s peak_uri=%s", run_id, reach_name,
                peak.dye_cmax_mgl, peak.plume_reach_m, peak.active_frames, peak.uri)

    if deck["substance_class"] == "sediment":
        try:
            peak = await _fold_sediment_products(
                peak, run_id=run_id, utm_epsg=utm_epsg, reach_name=reach_name,
                worker_metrics=solve.get("metrics") or {},
                erodible=bool(deck.get("erodible_bed")), emitter=emitter)
        except Exception as exc:  # noqa: BLE001 - a bonus map never voids the run
            logger.warning("sediment deposition unexpected failure (%s)", exc)
    elif deck["substance_class"] == "oil":
        from .substance import classify_substance

        await _emit_oil_slick(peak, run_id=run_id, reach_name=reach_name,
                              oil_preset=classify_substance(substance)[1],
                              emitter=emitter)

    if emitter is not None and peak.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(peak.bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("telemac zoom-to failed: %s", exc)
    return peak


async def publish_do_products(*, deck: dict[str, Any], solve: dict[str, Any],
                              do_sag_config: dict[str, Any]) -> Any:
    """Postprocess a WAQTEL O2 solve into the DISSOLVED-O2 field COG + the sag curve.

    The along-reach distance uses the principal-flow-axis proxy (no centerline is
    threaded to the postprocess); the layer's honesty label states it.
    """
    from trid3nt_contracts.telemac_contracts import TELEMAC_DO_STYLE_PRESET
    from trid3nt_server.emission.pipeline_emitter import current_emitter
    from trid3nt_server.workflows.telemac.postprocess_telemac import (
        postprocess_telemac_do,
    )

    emitter = current_emitter()
    run_id, utm_epsg = solve["run_id"], int(solve["utm_epsg"])
    reach_name = deck["reach_name"]
    slf_path, _ = await asyncio.to_thread(download_result_selafin, run_id)
    await _surface_bed_bathymetry_input(emitter, solve.get("metrics") or {},
                                  run_id, reach_name)
    try:
        layers, _metrics = await asyncio.to_thread(
            postprocess_telemac_do, slf_path, run_id=run_id, utm_epsg=utm_epsg,
            reach_name=reach_name,
            saturation_mgl=float(do_sag_config.get("saturation_mgl", 9.0)),
            upstream_do_mgl=float(do_sag_config.get("upstream_do_mgl", 9.0)),
            bod_upstream_mgl=float(do_sag_config.get("bod_mgl", 20.0)),
            standard_mgl=float(do_sag_config.get("standard_mgl", 5.0)))
    finally:
        Path(slf_path).unlink(missing_ok=True)

    raw = layers[0]
    mesh_meta = {
        "mesh_size_m": deck["mesh_size_m"],
        "mesh_node_estimate": deck["mesh_node_estimate"],
        "mesh_resolution_label": deck["mesh_resolution_label"],
        # The run prefix travels WITH the layer: the caller writes this run's own
        # chart spec + metrics there once the chart has been built.
        "run_id": run_id,
    }
    published = raw.model_copy(update=mesh_meta)
    if raw.uri.startswith(("s3://", "gs://")):
        try:
            pub_uri = await asyncio.to_thread(
                publish_layer, layer_uri=raw.uri, layer_id=raw.layer_id,
                style_preset=raw.style_preset or TELEMAC_DO_STYLE_PRESET)
            published = raw.model_copy(update={"uri": pub_uri, **mesh_meta})
        except PublishLayerError as exc:
            logger.warning("do_sag publish_layer failed (%s) - unpublished COG", exc)

    if emitter is not None:
        try:
            from trid3nt_server.emission.layer_uri_emit import publish_input_layer

            await publish_input_layer(emitter, published)
        except Exception as exc:  # noqa: BLE001
            logger.warning("do_sag layer emit failed: %s", exc)
        if published.bbox:
            try:
                await emitter.emit_map_command("zoom-to",
                                               {"bbox": list(published.bbox)})
            except Exception as exc:  # noqa: BLE001
                logger.warning("do_sag zoom-to failed: %s", exc)

    logger.info("telemac do_sag complete run_id=%s reach=%s do_min=%.3g mg/L at %.0fm "
                "violates=%s uri=%s", run_id, reach_name, published.do_min_mgl,
                published.do_min_distance_m or 0.0, published.do_violates_standard,
                published.uri)
    return published


def build_dye_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The plume's rise-to-peak chart SPEC: honest tracer scalars, never a fitted curve.

    Two points, both measured off the postprocessed field - zero concentration at
    release, then the peak at its arrival time. ``None`` when the run measured no
    peak, which is the honest "there was no curve to draw".
    """
    cmax = getattr(result, "dye_cmax_mgl", None)
    peak_t = getattr(result, "dye_peak_time_s", None)
    if cmax is None or peak_t is None:
        return None
    from trid3nt_server.data.processing.charts_common import build_chart_payload

    where = params.get("location") or getattr(result, "name", None) or "the reach"
    substance = params.get("substance") or "dye"
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": True},
            "data": {"values": [{"t_s": 0.0, "dye_mgl": 0.0},
                                {"t_s": float(peak_t), "dye_mgl": float(cmax)}]},
            "encoding": {
                "x": {"field": "t_s", "type": "quantitative", "title": "Time (s)"},
                "y": {"field": "dye_mgl", "type": "quantitative",
                      "title": f"{str(substance).capitalize()} concentration (mg/L)"},
            },
        },
        title=f"Peak {substance} concentration - {where}",
        caption=(f"Reach peak {substance} concentration {float(cmax):.3g} mg/L, "
                 f"arriving {float(peak_t):.0f} s after release (idealized-bed demo)."),
    )


class Products:
    """Postprocess + publish steps, one constructor per deliverable family."""

    @staticmethod
    def dye(*, deck: Any, solve: Any, carrier_discharge: Any) -> Step:
        """The dye/oil/sediment deliverables: peak COG, results mesh, class extras."""
        return Step(runner=f"{_STEPS}.products.publish_dye_products",
                    kwargs={"deck": deck, "solve": solve,
                            "carrier_discharge": carrier_discharge})
