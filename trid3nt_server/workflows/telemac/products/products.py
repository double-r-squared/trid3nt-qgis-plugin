"""The PRODUCTS step: a solved reach -> the map layers, the scalars, the chart spec.

The peak concentration COG is the narration carrier; the native result SELAFIN
beside it is the TEMPORAL artifact the client animates.

EACH SUBSTANCE CLASS LEADS WITH ITS OWN PRODUCT, and the concentration raster is
named for the field it actually carries. A sediment run leads with the signed
bed-evolution map beside a SUSPENDED-SEDIMENT raster; an oil run leads with the
floating-slick track, because the slick and the drogues beneath it are the only
products carrying oil physics while the transported field is the same passive
tracer a dye run advects; the dye class leads with the dye. A dye-named product
outside the dye class asserts more than the run computed.

The class scalars are read HERE, off the run's own uploaded evidence - GAIA's
closure out of the solver listing, the slick out of the drogues track - because
the worker is the engine room and derives nothing.

Everything past the primary layer is best-effort by contract: failure retracts
nothing, so a missing deposition COG or slick never voids the concentration
layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import (
    TELEMAC_SUBSTANCE_PRODUCTS,
    SubstanceProduct,
    TelemacDyeLayerURI,
)

from trid3nt_server.workflows.runtime import Step
from trid3nt_server.emission.publish import PublishLayerError, publish_layer

from ..helpers.errors import TelemacDyeScenarioError
from ..solving.solve import download_result_selafin

logger = logging.getLogger("trid3nt_server.workflows.telemac.products.products")

__all__ = ["Products", "publish_do_products", "publish_dye_products"]

_PRODUCTS = "trid3nt_server.workflows.telemac.products"

def _release_provenance(run: dict[str, Any]) -> SyntheticInput:
    """Where the source entered the water, as the layer's own record.

    The downstream distance is measured from here, so the row states the point
    the run was authored with: the supplied one, which the pre-flight
    containment test already accepted into the domain and put on the flowline,
    or the ``spill_fraction`` walk along the modeled centerline. A point outside
    the domain never reaches this step - the pre-flight refuses it - so the row
    can never read "user" over a relocated release.
    """
    lon, lat = (run.get("release_lon"), run.get("release_lat")) \
        if run.get("release_user_supplied") else (None, None)
    if lon is None or lat is None:
        return SyntheticInput(
            param="release_point",
            value=f"spill_fraction {run.get('spill_fraction')}",
            basis="derived", consequence="scenario",
            real_source_if_any="NHDPlus flowline centerline",
            note="no release point was supplied; the source sits at spill_fraction "
                 "along the modeled reach")
    return SyntheticInput(
        param="release_point", value=f"({lon}, {lat})", basis="user",
        consequence="scenario", note=run.get("release_note"))


def _rain_provenance(run: dict[str, Any]) -> list[SyntheticInput]:
    """The on-mesh rain/evaporation forcing, with its DECLARED temporal transform.

    Empty when no forcing was asked for - a run with no rain has no rain row.
    The note carries the cadence/units stamp the ``Data("rain")`` declaration
    produced, so a reader can tell an as-reported rate from a moved one.
    """
    value = run.get("rain_mm_per_day")
    if value is None:
        return []
    rung = run.get("rain_rung")
    fetched = rung == "gridmet_domain_mean"
    return [SyntheticInput(
        param="rain_or_evap_mm_per_day", value=round(float(value), 2),
        units="mm/day", basis="fetched" if fetched else "user",
        consequence="physics",
        real_source_if_any=("fetch_gridmet (University of Idaho gridMET daily "
                            "precipitation)" if fetched else None),
        note=run.get("rain_note"))]


def _bed_provenance(run: dict[str, Any]) -> SyntheticInput:
    """WHICH bed the reach's nodes carry, as the layer's own record.

    The mesher's label travels on the sheet the worker was handed, so the row
    names the dataset the solve actually read rather than the class of run it
    was: a GLO-30 bed and the 3DEP one the ladder fell to are different physics
    and the layer has to be able to say which it got.
    """
    source = str(run.get("bed_source") or "staged")
    return SyntheticInput(
        param="mesh_bed", value=source, basis="fetched", consequence="physics",
        real_source_if_any=source,
        note="the elevation every node of the reach carries; the solver reads it "
             "as the channel's bathymetry")


def _provenance(solve: dict[str, Any], discharge: dict[str, Any],
                run: dict[str, Any]) -> list[SyntheticInput]:
    """The physically dominant inputs, as rows the layer carries.

    The carrier discharge that governs dilution (real NWM streamflow or
    user-supplied), the on-mesh rain/evaporation forcing when one was asked for,
    the bed the mesh was painted from, the bank geometry the reach was cut from,
    and the release point the run was authored with.
    """
    return [
        _release_provenance(run),
        *_rain_provenance(run),
        _bed_provenance(run),
        SyntheticInput(
            param="discharge_m3s", value=round(float(discharge["m3s"]), 1),
            units="m3/s", basis=discharge.get("basis") or "fetched",
            real_source_if_any=discharge.get("real_source"),
            note=discharge.get("note") or "carrier discharge governs dilution/transport"),
        SyntheticInput(
            param="bank_geometry", value="nhd_area", basis="fetched",
            consequence="physics",
            real_source_if_any="USGS NHDArea water polygons"),
    ]


def _substance_product(substance_class: str) -> SubstanceProduct:
    """The declared class's transported-field product; the dye row when unnamed."""
    return TELEMAC_SUBSTANCE_PRODUCTS.get(
        str(substance_class or "tracer").lower(),
        TELEMAC_SUBSTANCE_PRODUCTS["tracer"])


def _honesty_note(location_name: str, substance: str) -> str:
    surrogate = ""
    if substance and substance != "dye":
        surrogate = (
            f" NOTE: {substance} is modeled as a passively advected dissolved "
            "tracer (transport + dilution only) - NOT slick physics "
            "(no spreading/evaporation/weathering/beaching).")
    banks_note = (" Banks: real USGS NHDArea water-polygon geometry "
                  "(per-station sampled widths).")
    return (
        f"Idealized demo: a FINITE mid-reach point-source {substance or 'dye'} "
        f"pulse released on the real {location_name} river reach (NLDI/NHDPlus "
        "geometry) over a planar idealized channel bed with prescribed tracer "
        "dispersion. The raster is the PEAK concentration envelope over the run; "
        "the animation plays from the native SELAFIN mesh. Not a calibrated site "
        "study." + banks_note + surrogate)


def _publish_peak_layer(raw_peak: TelemacDyeLayerURI, run_id: str,
                        location_name: str, mesh_meta: dict[str, Any],
                        substance: str, substance_class: str,
                        synthetic_inputs: list[SyntheticInput]) -> TelemacDyeLayerURI:
    """Publish the peak COG through the one styling chokepoint and enrich narration.

    On publish failure the RAW peak is returned unchanged - its s3 COG still lets
    the case discover the SELAFIN sibling, and the dispatch-level guardrail owns
    the map honesty.
    """
    from trid3nt_server.workflows.telemac.products.postprocess_telemac import peak_layer_id

    honesty = _honesty_note(location_name, substance)
    update = {**mesh_meta, "synthetic_inputs": list(synthetic_inputs)}
    if raw_peak.layer_type != "raster" or not raw_peak.uri.startswith(("gs://", "s3://")):
        return raw_peak.model_copy(update={"fallback_note": honesty, **update})
    layer_id = peak_layer_id(run_id, substance_class)
    style = raw_peak.style or _substance_product(substance_class).style
    try:
        published_uri = publish_layer(
            layer_uri=raw_peak.uri, layer_id=layer_id, style=style)
    except PublishLayerError as exc:
        logger.warning("telemac: publish_layer FAILED layer_id=%s error_code=%s (%s) "
                       "- returning the unpublished peak.", layer_id, exc.error_code, exc)
        return raw_peak.model_copy(update={"fallback_note": honesty, **update})
    return raw_peak.model_copy(update={
        "layer_id": layer_id, "uri": published_uri, "style": style,
        "fallback_note": honesty, **update})


def _download_artifact(run_id: str, basename: str) -> str | None:
    """Download one file the run uploaded; ``None`` when it wrote none (fail-open).

    Every class extra past the primary layer is read off an artifact that may not
    be there - a run that coupled no GAIA writes no GAIA result - so absence is
    an answer rather than a failure.
    """
    from trid3nt_server.workflows.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
    )

    local = str(Path(tempfile.mkdtemp(prefix=f"telemac-{run_id}-")) / basename)
    try:
        resp = _get_s3_client().get_object(Bucket=_get_runs_bucket(),
                                           Key=f"{run_id}/{basename}")
        with open(local, "wb") as fh:
            fh.write(resp["Body"].read())
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemac: %s absent for run %s (%s)", basename, run_id, exc)
        return None
    return local


def _listing_text(run_id: str) -> str:
    """The solver listing this run wrote, or the empty string."""
    local = _download_artifact(run_id, "full_listing.log")
    if not local:
        return ""
    try:
        return Path(local).read_text(errors="replace")
    finally:
        Path(local).unlink(missing_ok=True)


async def _fold_sediment_products(peak: TelemacDyeLayerURI, *, run_id: str,
                                  utm_epsg: int, reach_name: str,
                                  run: dict[str, Any],
                                  erodible: bool, emitter: Any) -> TelemacDyeLayerURI:
    """Fold GAIA's own mass-balance scalars onto the peak + emit the deposition map.

    The scalars are READ HERE, off the listing GAIA printed its closure into and
    the result it wrote its graded surface into. ``deposited_mass_kg`` is the NET
    bed mass, clamped at zero - the SAME net quantity the deposition map and the
    deposit fraction integrate. Never the GROSS deposition: in a supply-limited
    run gross deposition can equal gross erosion with net ~0, so the map is
    correctly empty and the narrated mass must match it.
    """
    from trid3nt_server.workflows.telemac.products.postprocess_telemac import (
        PostprocessTelemacError,
        postprocess_telemac_deposition,
    )

    from .run_reads import sediment_scalars

    gaia_path = await asyncio.to_thread(_download_artifact, run_id, "gaia_river.slf")
    listing = await asyncio.to_thread(_listing_text, run_id)
    stats = await asyncio.to_thread(
        sediment_scalars, listing_text=listing,
        injected_kg=float(run["sediment_injected_kg"]),
        n_classes=int(run.get("sediment_n_classes") or 1), gaia_slf=gaia_path)
    net = stats.get("sediment_net_bed_mass_kg")
    peak = peak.model_copy(update={
        "deposited_mass_kg": max(float(net), 0.0) if net is not None else None,
        "deposit_fraction": stats.get("sediment_deposit_fraction"),
        "sediment_n_classes": stats.get("sediment_n_classes"),
        "sediment_surface_d50_min_um": stats.get("sediment_surface_d50_min_um"),
        "sediment_surface_d50_max_um": stats.get("sediment_surface_d50_max_um"),
        "sediment_surface_d50_range_um": stats.get("sediment_surface_d50_range_um"),
    })
    if not gaia_path:
        return peak
    try:
        dep_layers, dep_metrics = await asyncio.to_thread(
            postprocess_telemac_deposition, gaia_path, run_id=run_id,
            utm_epsg=utm_epsg, reach_name=reach_name,
            worker_sed_metrics=stats, erodible=bool(erodible))
    except (PostprocessTelemacError, TelemacDyeScenarioError) as exc:
        logger.warning("sediment deposition postprocess failed (%s) - the "
                       "concentration COG still stands", exc)
        return peak
    finally:
        Path(gaia_path).unlink(missing_ok=True)

    peak = peak.model_copy(update={
        "max_deposition_mm": dep_metrics.get("max_deposition_mm"),
        **({"max_scour_mm": dep_metrics["max_scour_mm"]}
           if erodible and dep_metrics.get("max_scour_mm") is not None else {})})
    if not dep_layers or emitter is None:
        return peak
    dep_raw = dep_layers[0]
    try:
        pub_uri = await asyncio.to_thread(
            publish_layer, layer_uri=dep_raw.uri, layer_id=dep_raw.layer_id,
            style=dep_raw.style)
        dep_pub = dep_raw.model_copy(update={"uri": pub_uri})
    except PublishLayerError as exc:
        logger.warning("sediment deposition publish failed (%s) - emitting the "
                       "unpublished COG", exc)
        dep_pub = dep_raw
    from trid3nt_server.emission.layer_uri_emit import publish_input_layer

    # A sediment run LEADS with the bed: this is a RESULT the solve produced, not
    # an input it consumed, and it surfaces before the concentration raster.
    emitted = await publish_input_layer(emitter, dep_pub, role="primary")
    logger.info("sediment bed-evolution layer emitted=%s id=%s max_dep_mm=%s "
                "deposited_kg=%s", emitted, dep_pub.layer_id,
                dep_pub.max_deposition_mm, dep_pub.deposited_mass_kg)
    return peak


async def _emit_oil_slick(peak: TelemacDyeLayerURI, *, run_id: str,
                          reach_name: str, oil_preset: Any, utm_epsg: int,
                          emitter: Any) -> None:
    """Build the floating-slick track off the drogues the run wrote, and emit it.

    The engine writes the raw TecPlot track and nothing else; the renderable
    snapshots and the exit accounting are read HERE and uploaded beside it, so
    the layer's bytes exist before its handle does - a URI registered ahead of
    its object was the dangling-handle class.

    An oil run LEADS with this: the slick and the drogues beneath it are the only
    products that carry oil physics, while the transported field beside them is
    the same passive tracer a dye run advects. A run that wrote no track is an
    honest skip: the tracer COG stands.

    The slick carries NO style preset. It is a snapshot point cloud, the style
    contract has no row for it, and a preset the contract never declared resolves
    to whatever the renderer guesses - which is how a river-line preset came to
    style a slick.
    """
    from trid3nt_contracts.execution import LayerURI

    from trid3nt_server.emission.layer_uri_emit import publish_input_layer
    from trid3nt_server.workflows.solver.solver import _get_runs_bucket, _get_s3_client

    from .run_reads import oil_slick_features

    if emitter is None:
        return
    try:
        drogues = await asyncio.to_thread(_download_artifact, run_id, "drogues.txt")
        if not drogues:
            logger.warning("oil: run %s wrote no drogues track - slick skipped", run_id)
            return
        try:
            particles, slick, stats = await asyncio.to_thread(
                oil_slick_features, drogues, utm_epsg=utm_epsg)
        finally:
            Path(drogues).unlink(missing_ok=True)
        if not slick["features"]:
            logger.warning("oil: the drogues track for %s holds no floats at any "
                           "written instant - slick skipped", run_id)
            return
        bucket, s3 = _get_runs_bucket(), _get_s3_client()
        for basename, body in (("particles.json", particles), ("slick.geojson", slick)):
            await asyncio.to_thread(
                lambda k=basename, b=body: s3.put_object(
                    Bucket=bucket, Key=f"{run_id}/{k}",
                    Body=json.dumps(b).encode("utf-8"),
                    ContentType="application/json"))
        layer = LayerURI(
            layer_id=f"telemac-oil-slick-{run_id}",
            name=f"Oil slick track ({oil_preset}, {reach_name})",
            layer_type="vector", uri=f"s3://{bucket}/{run_id}/slick.geojson",
            role="primary", bbox=peak.bbox)
        logger.info("oil slick layer emitted=%s id=%s stats=%s",
                    await publish_input_layer(emitter, layer, role="primary"),
                    layer.layer_id, stats)
    except Exception as exc:  # noqa: BLE001 -- a bonus layer never voids the run
        logger.warning("oil slick layer skipped: %s", exc)


def _journal_wetted_fraction(metrics: dict[str, Any]) -> None:
    """Say out loud how much of the solved domain the run actually wet.

    The reach is meshed from the mapped ACTIVE CHANNEL, which is a bankfull
    polygon: at low flow the solve correctly leaves part of it dry, and the
    conveyance width the answer rests on is narrower than the picture. The number
    rides the journal because a reader needs it beside the map; it decides
    nothing, and a run whose result cannot be measured says nothing rather than
    losing its products over a heuristic.

    The postprocess measured it off the read it already made, so this narrates
    rather than reopening the result.
    """
    from trid3nt_server.workflows.runtime import journal_note

    measured = {k: metrics[k] for k in
                ("wetted_fraction", "mesh_area_m2", "wet_area_m2", "wet_tol_m")
                if metrics.get(k) is not None}
    if len(measured) < 4:
        return
    journal_note(
        f"wetted fraction: {measured['wetted_fraction']:.0%} of the "
        f"{measured['mesh_area_m2'] / 1e6:.3g} km2 solved domain still held more "
        f"than {measured['wet_tol_m']:g} m of water at the final frame "
        f"({measured['wet_area_m2'] / 1e6:.3g} km2). The domain is the mapped "
        "active channel, so the dry remainder is bar and bank the flow did not "
        "reach at this discharge - a measured heuristic, not a verdict.")


async def publish_dye_products(*, run: dict[str, Any], solve: dict[str, Any],
                               carrier_discharge: dict[str, Any]) -> TelemacDyeLayerURI:
    """Postprocess the solved reach into its published layers + narration scalars."""
    from trid3nt_server.emission.pipeline_emitter import current_emitter
    from trid3nt_server.workflows.telemac.products.postprocess_telemac import (
        postprocess_telemac,
    )
    from trid3nt_server.workflows.telemac.results_mesh_seam import (
        publish_results_mesh_via_seam,
    )

    emitter = current_emitter()
    run_id, utm_epsg = solve["run_id"], int(solve["utm_epsg"])
    reach_name, substance = run["reach_name"], run["substance"]
    substance_class = run["substance_class"]
    product = _substance_product(substance_class)
    slf_path = await asyncio.to_thread(download_result_selafin, run_id)

    try:
        layers, metrics = await asyncio.to_thread(
            postprocess_telemac, slf_path, run_id=run_id, utm_epsg=utm_epsg,
            reach_name=reach_name, substance_class=substance_class)
        _journal_wetted_fraction(metrics)
    finally:
        Path(slf_path).unlink(missing_ok=True)

    if not layers:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_NO_LAYERS",
            f"postprocess_telemac produced no {product.noun} layer (empty tracer "
            "field?).")
    raw_peak = layers[0]

    mesh_meta = {
        "mesh_size_m": run["mesh_size_m"],
        "mesh_resolution_label": run["mesh_resolution_label"],
    }
    peak = await asyncio.to_thread(
        _publish_peak_layer, raw_peak, run_id, run["location_name"], mesh_meta,
        substance, substance_class, _provenance(solve, carrier_discharge, run))

    # EMIT-ON-SOLVE: outputs.json carries the peak entry (the whole-run record)
    # plus the SELAFIN mesh entry, and the seam owns publication of the temporal
    # artifact. The typed peak above stays this step's own.
    await publish_results_mesh_via_seam(
        emitter, run_id=run_id, engine="telemac", peak_layer=raw_peak,
        peak_quantity=product.quantity, mesh_group=product.mesh_group,
        mesh_basename="r2d_river.slf",
        mesh_epsg=utm_epsg, reach_name=reach_name,
        reference_time=solve.get("started_at"))

    logger.info("telemac reach complete run_id=%s reach=%s class=%s cmax_mgl=%.4g "
                "plume_reach_m=%s active_frames=%s peak_uri=%s", run_id, reach_name,
                substance_class, peak.dye_cmax_mgl, peak.plume_reach_m,
                peak.active_frames, peak.uri)

    if substance_class == "sediment":
        try:
            peak = await _fold_sediment_products(
                peak, run_id=run_id, utm_epsg=utm_epsg, reach_name=reach_name,
                run=run, erodible=bool(run.get("erodible_bed")), emitter=emitter)
        except Exception as exc:  # noqa: BLE001 - a bonus map never voids the run
            logger.warning("sediment deposition unexpected failure (%s)", exc)
    elif substance_class == "oil":
        from ..helpers.substance import classify_substance

        await _emit_oil_slick(peak, run_id=run_id, reach_name=reach_name,
                              oil_preset=classify_substance(substance)[1],
                              utm_epsg=utm_epsg, emitter=emitter)

    if emitter is not None and peak.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(peak.bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("telemac zoom-to failed: %s", exc)
    return peak


def _do_sag_provenance(carrier_discharge: dict[str, Any] | None) -> list[SyntheticInput]:
    """The carrier discharge governing dilution, as the DO-sag layer's own record.

    Mirrors ``_provenance``'s dye row: the layer must carry which cycle it read,
    never leave the reader to trust an unrecorded "latest".
    """
    if not carrier_discharge:
        return []
    return [SyntheticInput(
        param="discharge_m3s", value=round(float(carrier_discharge["m3s"]), 1),
        units="m3/s", basis=carrier_discharge.get("basis") or "fetched",
        real_source_if_any=carrier_discharge.get("real_source"),
        note=carrier_discharge.get("note") or "carrier discharge governs dilution")]


async def publish_do_products(*, run: dict[str, Any], solve: dict[str, Any],
                              do_sag_config: dict[str, Any],
                              carrier_discharge: dict[str, Any] | None = None) -> Any:
    """Postprocess a WAQTEL O2 solve into the DISSOLVED-O2 field COG + the sag curve.

    The along-reach distance uses the principal-flow-axis proxy (no centerline is
    threaded to the postprocess); the layer's honesty label states it.
    """
    from trid3nt_contracts.telemac_contracts import TELEMAC_DO_STYLE
    from trid3nt_server.emission.pipeline_emitter import current_emitter
    from trid3nt_server.workflows.telemac.products.postprocess_telemac import (
        postprocess_telemac_do,
    )

    emitter = current_emitter()
    run_id, utm_epsg = solve["run_id"], int(solve["utm_epsg"])
    reach_name = run["reach_name"]
    slf_path = await asyncio.to_thread(download_result_selafin, run_id)
    try:
        layers, metrics = await asyncio.to_thread(
            postprocess_telemac_do, slf_path, run_id=run_id, utm_epsg=utm_epsg,
            reach_name=reach_name,
            # Read, never re-defaulted: these were the THIRD copy of four
            # declared do_sag defaults (declarations.py, the assembler, here).
            saturation_mgl=float(do_sag_config["saturation_mgl"]),
            upstream_do_mgl=float(do_sag_config["upstream_do_mgl"]),
            standard_mgl=float(do_sag_config["standard_mgl"]),
            k1_per_day=float(do_sag_config["k1_per_day"]),
            k2_per_day=float(do_sag_config["k2_per_day"]))
        _journal_wetted_fraction(metrics)
    finally:
        Path(slf_path).unlink(missing_ok=True)

    raw = layers[0]
    mesh_meta = {
        "mesh_size_m": run["mesh_size_m"],
        "mesh_resolution_label": run["mesh_resolution_label"],
        # The run prefix travels WITH the layer: the caller writes this run's own
        # chart spec + metrics there once the chart has been built.
        "run_id": run_id,
        "synthetic_inputs": _do_sag_provenance(carrier_discharge),
    }
    published = raw.model_copy(update=mesh_meta)
    if raw.uri.startswith(("s3://", "gs://")):
        try:
            pub_uri = await asyncio.to_thread(
                publish_layer, layer_uri=raw.uri, layer_id=raw.layer_id,
                style=raw.style or TELEMAC_DO_STYLE)
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


class Products:
    """Postprocess + publish steps, one constructor per deliverable family."""

    @staticmethod
    def dye(*, run: Any, solve: Any, carrier_discharge: Any) -> Step:
        """The dye/oil/sediment deliverables: peak COG, results mesh, class extras."""
        return Step(runner=f"{_PRODUCTS}.products.publish_dye_products", stage="publish",
                    kwargs={"run": run, "solve": solve,
                            "carrier_discharge": carrier_discharge})

    @staticmethod
    def dissolved_oxygen(*, run: Any, solve: Any, process: Any,
                         carrier_discharge: Any) -> Step:
        """The WAQTEL O2 deliverables: dissolved-O2 field COG + the along-reach sag."""
        return Step(runner=f"{_PRODUCTS}.products.publish_do_products", stage="publish",
                    kwargs={"run": run, "solve": solve, "do_sag_config": process,
                            "carrier_discharge": carrier_discharge})
