"""The solved column -> the surface and bottom planes, and what survived between.

A 3D field arrives flat over NPOIN3 as NPLAN planes stacked over the 2D mesh,
bottom plane first. The run exchanges NO heat with the atmosphere, so the
depth-weighted mean's drift is the numerical error bar rather than a signal."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import (
    TELEMAC3D_STRATIFICATION_STYLE,
    Telemac3dLayerURI,
)

from trid3nt_server.workflows.runtime import Step
from trid3nt_server.workflows.shared.publish_product_layer import publish_product_layer

from ..helpers.errors import OpenWaterError
from ..solving.solve import download_result

logger = logging.getLogger("trid3nt_server.workflows.telemac.products.stratified")

__all__ = ["StratifiedProducts", "planes", "publish_stratified_products",
           "vertical_profile"]

_PRODUCTS = "trid3nt_server.workflows.telemac.products"


def planes(result: dict[str, Any], variable: str, record: int) -> Any:
    """One 3D field at ``record``, reshaped ``(nplan, npoin2)``, bottom plane first.

    Plane-major is the file's own layout; a variable never written is ``None``."""
    import numpy as np

    nplan = int(result["nplan"])
    for name in result["varnames"]:
        if variable in name.strip().upper():
            data = result["data"].get(name)
            if data is None or not data.size:
                continue
            flat = np.asarray(data)[record]
            return flat.reshape(nplan, flat.shape[0] // nplan)
    return None


def vertical_profile(field: Any, column: int) -> Any:
    """One column of a reshaped 3D field, bed -> surface."""
    import numpy as np

    return np.asarray(field)[:, int(column)]


def _deepest_column(bed: Any) -> int:
    """The node the column is read at: the deepest one the mesh carries.

    The deepest column is where vertical structure can exist at all."""
    import numpy as np

    return int(np.nanargmin(np.asarray(bed, dtype=float)))


def _sigma(elevation: Any, column: int) -> Any:
    """The plane positions the run ACTUALLY used, 0 = bed, 1 = free surface.

    Measured off the solved elevations: a zoomed sigma is not evenly spaced."""
    import numpy as np

    z = np.asarray(vertical_profile(elevation, column), dtype=float)
    span = float(z[-1] - z[0])
    if abs(span) < 1e-9:
        return np.linspace(0.0, 1.0, z.shape[0])
    return (z - z[0]) / span


def _measure(result: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """What the solved column says, as the numbers a reader has to be able to check."""
    import numpy as np

    frames = int(np.asarray(result["times"]).shape[0])
    if frames < 1:
        raise OpenWaterError(
            "the solved 3D file carries no record, so there is no column to read.",
            error_code="TELEMAC3D_OUTPUT_MISSING")
    temperature_final = planes(result, "TEMPERATURE", frames - 1)
    temperature_init = planes(result, "TEMPERATURE", 0)
    elevation = planes(result, "ELEVATION", frames - 1)
    velocity_u = planes(result, "VELOCITY U", frames - 1)
    if temperature_final is None or elevation is None:
        raise OpenWaterError(
            f"the solved 3D file carries {result['varnames']} and needs both an "
            "elevation and a temperature to read a column from.",
            error_code="TELEMAC3D_OUTPUT_MISSING")

    column = _deepest_column(np.asarray(elevation)[0])
    sigma = _sigma(elevation, column)
    final = vertical_profile(temperature_final, column)
    initial = (vertical_profile(temperature_init, column)
               if temperature_init is not None else final)
    # SURFACE minus BED at the column the profile is read from, off the solved
    # elevations: the free surface is the level the basin opened at, not the
    # datum, so a depth counted from zero would be the wrong column.
    column_depth = float(np.asarray(elevation)[-1][column]
                         - np.asarray(elevation)[0][column])
    dt_final = float(final[-1] - final[0])
    heat_initial = float(np.trapezoid(initial, sigma))
    heat_final = float(np.trapezoid(final, sigma))

    measured: dict[str, Any] = {
        # Not "Surface temperature": this label rides BOTH rasters' legend caption
        # and the bed-to-surface profile chart title, so it may not name one
        # plane, and it stays a single word so a split on the first space leaves
        # the whole label as the noun.
        "variable_label": "Water temperature", "variable_units": "degC",
        "stratification_metric": round(abs(dt_final), 4),
        "stratification_dt": round(dt_final, 4),
        "stratification_dt_init": round(float(initial[-1] - initial[0]), 4),
        "column_heat_mean_init_c": round(heat_initial, 4),
        "column_heat_mean_final_c": round(heat_final, 4),
        "column_heat_drift_frac": round(
            (heat_final - heat_initial) / max(abs(heat_initial), 1e-9), 6),
        "column_depth_m": round(column_depth, 2),
        "nplan": int(result["nplan"]),
        "profile_sigma": np.round(sigma, 4).tolist(),
        "profile_values": np.round(final, 3).tolist(),
        "profile_values_initial": np.round(initial, 3).tolist(),
        "wind_speed_mps": float(run["wind_speed_mps"]),
        "mesh_size_m": float(run["mesh_size_m"]),
        "mesh_resolution_label": (
            f"authored om2d mesh, {run['mesh_node_count']} nodes at a "
            f"{run['mesh_size_m']:g} m median edge, "
            f"{result['nplan']} sigma planes"),
        # MEASURED off the solved elevations rather than replanned: the label
        # says what the run's grid actually achieved at the column the profile is
        # read from.
        "vertical_resolution_label": (
            f"{int(result['nplan'])} sigma planes, near-surface layer "
            f"{float(sigma[-1] - sigma[-2]) * column_depth:.2f} m over the "
            f"{column_depth:.1f} m deep column"),
        "surface_field": np.asarray(temperature_final)[-1],
        "bottom_field": np.asarray(temperature_final)[0],
    }
    if velocity_u is not None:
        # The WIND-CIRCULATION half of the same run: a steady wind drives surface
        # water downwind and a return flow at depth, and the depth average of the
        # two is near zero - which is exactly why a 2D model reports nothing.
        u = vertical_profile(velocity_u, column)
        measured.update({
            "u_surface": round(float(u[-1]), 5),
            "u_bottom": round(float(u[0]), 5),
            "depth_avg_u": round(float(np.trapezoid(u, sigma)), 5),
        })
    return measured


def _provenance(run: dict[str, Any], measured: dict[str, Any]
                ) -> list[SyntheticInput]:
    """The physically dominant inputs, as rows the layer carries."""
    calm = float(run["wind_speed_mps"]) <= 0.0
    vertical = str(measured.get("vertical_resolution_label")
                   or "vertical sigma levels")
    return [
        SyntheticInput(
            param="wind_speed_mps", value=round(float(run["wind_speed_mps"]), 1),
            units="m/s", basis="default_demo", consequence="physics",
            note=("calm - the thermocline persists and no wind circulation is "
                  "driven" if calm else
                  "prescribed steady wind, which mixes the column and drives the "
                  "surface-downwind / return-flow-at-depth pair reported beside "
                  "the temperature")),
        SyntheticInput(
            param="thermocline",
            value=f"{float(run['warm_temp_c']):g}C/{float(run['cold_temp_c']):g}C",
            units="C", basis="default_demo", consequence="physics",
            note=(f"prescribed warm epilimnion over cold hypolimnion, thermocline "
                  f"at {float(run['thermocline_depth_m']):g} m (no met-forcing "
                  "fetcher exists). No heat exchange: the column can only "
                  "REDISTRIBUTE its heat, never lose it")),
        SyntheticInput(
            param="nplan", value=int(measured["nplan"]), basis="default_demo",
            consequence="numerical",
            note=(f"{vertical} - the 3D degree of freedom a 2D model has "
                  "none of")),
        SyntheticInput(
            param="lake_level_m", value=run.get("surface_m"), units="m",
            basis="fetched", consequence="physics",
            real_source_if_any="fetch_greatlakes_water_level (NOAA CO-OPS)",
            note=str(run.get("level_note")
                     or "the observed level the basin's free surface opened at")),
        SyntheticInput(
            param="mesh_bed", value=str(run.get("bed_source") or "staged"),
            basis="fetched", consequence="physics",
            real_source_if_any=str(run.get("bed_source") or None) or None,
            note="the elevation every node of the basin carries; the solve reads "
                 "it as the bathymetry the column stands over, on the same datum "
                 "the level above is counted from"),
        SyntheticInput(
            param="mesh_domain",
            value=f"{run['mesh_name']} ({run['mesh_node_count']} nodes / "
                  f"{run['mesh_element_count']} elements)",
            basis="user", consequence="numerical",
            real_source_if_any="build_mesh (mesher=om2d)",
            note=(f"solved on the accepted mesh: {run['boundary_states']}, so the "
                  f"water in it is conserved. The column is read at the deepest "
                  f"node, {measured['column_depth_m']:g} m down")),
    ]


def _honesty_note(run: dict[str, Any], measured: dict[str, Any]) -> str:
    return (
        "3D structure SCREENING: TELEMAC-3D (hydrostatic) over "
        f"{measured['nplan']} sigma planes on the authored mesh "
        f"{run['mesh_name']!r}, driven by a PRESCRIBED column and a "
        f"{float(run['wind_speed_mps']):g} m/s wind - labeled demo forcing, not "
        "observed conditions. The pair of rasters is the SURFACE and BOTTOM field; "
        "their contrast is what a depth-averaged model cannot show. The run "
        "exchanges NO heat with the atmosphere: heat is CONSERVED, so a falling "
        "surface temperature is downward MIXING, not the lake cooling. Not a "
        "calibrated study.")


async def publish_stratified_products(*, run: dict[str, Any],
                                      solve: dict[str, Any]) -> Telemac3dLayerURI:
    """The solved column -> its surface and bottom layers, and the profile.

    The BOTTOM is published and emitted; the SURFACE is returned for the seam."""
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    from .result_reader import read_selafin
    from .postprocess_telemac import postprocess_telemac3d

    emitter = current_emitter()
    run_id = solve["run_id"]
    utm_epsg = int(solve["utm_epsg"])
    local = await asyncio.to_thread(
        download_result, run_id, run["result_basename"],
        error_code="TELEMAC3D_OUTPUT_MISSING")
    try:
        result = await asyncio.to_thread(read_selafin, local)
    finally:
        Path(local).unlink(missing_ok=True)

    measured = _measure(result, run)
    layers, _metrics = await asyncio.to_thread(
        postprocess_telemac3d, run_id=run_id, utm_epsg=utm_epsg,
        x=result["x"], y=result["y"], ikle=result["ikle2"],
        surface=measured["surface_field"], bottom=measured["bottom_field"],
        measured=measured, reach_name=run["domain_slug"])
    if not layers:
        raise OpenWaterError("the 3D postprocess produced no layer.",
                             error_code="TELEMAC3D_NO_LAYERS")

    update = {
        "fallback_note": _honesty_note(run, measured),
        "synthetic_inputs": _provenance(run, measured),
        "run_id": run_id,
        "profile_sigma": measured["profile_sigma"],
        "profile_values": measured["profile_values"],
        "profile_values_initial": measured["profile_values_initial"],
        "column_heat_drift_frac": measured["column_heat_drift_frac"],
        "stratification_dt_init": measured["stratification_dt_init"],
        "column_heat_mean_init_c": measured["column_heat_mean_init_c"],
        "column_heat_mean_final_c": measured["column_heat_mean_final_c"],
        "column_depth_m": measured["column_depth_m"],
    }

    # The bottom companion first: it is published and EMITTED here, because only
    # the returned surface layer rides the dispatch seam onto the canvas.
    if len(layers) > 1 and emitter is not None:
        companion = await publish_product_layer(
            layers[1], style=TELEMAC3D_STRATIFICATION_STYLE,
            update={"fallback_note": update["fallback_note"]})
        try:
            from trid3nt_server.emission.layer_uri_emit import publish_input_layer

            logger.info("telemac3d bottom layer emitted=%s id=%s",
                        await publish_input_layer(emitter, companion),
                        companion.layer_id)
        except Exception as exc:  # noqa: BLE001 - a missing companion never voids the pair
            logger.warning("telemac3d bottom emit failed: %s", exc)

    published = await publish_product_layer(
        layers[0], style=TELEMAC3D_STRATIFICATION_STYLE, update=update)
    _journal_column(measured)
    logger.info("telemac3d complete run_id=%s domain=%s dT=%.4g heat drift=%.3g "
                "u_surface=%s u_bottom=%s uri=%s", run_id, run["domain_slug"],
                measured["stratification_dt"], measured["column_heat_drift_frac"],
                measured.get("u_surface"), measured.get("u_bottom"),
                published.uri)
    return published


def _journal_column(measured: dict[str, Any]) -> None:
    """What the column did, and the error bar the conservation check puts on it."""
    from trid3nt_server.workflows.runtime import journal_note

    journal_note(
        f"the column at the mesh's deepest node ({measured['column_depth_m']:g} m) "
        f"opened {measured['stratification_dt_init']:.3g} degC top to bottom and "
        f"ended {measured['stratification_dt']:.3g} degC over "
        f"{measured['nplan']} sigma planes. The depth-weighted column mean moved "
        f"{100.0 * measured['column_heat_drift_frac']:.3g}% - the run exchanges no "
        "heat, so that drift is the numerical error bar on the mixing and not a "
        "loss."
        + ("" if measured.get("u_surface") is None else
           f" The same run drove {measured['u_surface']:.4g} m/s at the surface "
           f"against {measured['u_bottom']:.4g} m/s at the bed, depth-averaging to "
           f"{measured['depth_avg_u']:.4g} m/s - the return flow a 2D model "
           "reports as nothing."))


class StratifiedProducts:
    """The solved column's deliverable, as the door binds it."""

    @staticmethod
    def column(*, run: Any, solve: Any) -> Step:
        """The surface and bottom planes, and the profile between them."""
        return Step(runner=f"{_PRODUCTS}.stratified.publish_stratified_products",
                    stage="publish", kwargs={"run": run, "solve": solve})
