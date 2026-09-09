"""The solved harbour -> the agitation field, the sheltering pair and the curve.

Everything is MEASURED off the result file the solve wrote, including the bed the
wavelength is computed over, so the depth the dispersion relation is solved at is
the depth the solver ran on rather than a second sample of the bathymetry."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import (
    TELEMAC_AGITATION_STYLE,
    ArtemisAgitationLayerURI,
)

from trid3nt_server.workflows.runtime import Step
from trid3nt_server.workflows.shared.publish_product_layer import publish_product_layer

from ..helpers.errors import OpenWaterError
from ..solving.solve import download_result

logger = logging.getLogger("trid3nt_server.workflows.telemac.products.agitation")

__all__ = ["AgitationProducts", "dispersion_k", "publish_agitation_products",
           "structure_shadow"]

_PRODUCTS = "trid3nt_server.workflows.telemac.products"

#: Standard gravity, as the dispersion relation reads it.
_G = 9.81

#: How many points the published transect carries. A curve a reader scrubs does
#: not gain from one point per node, and a spec that carried thousands would be
#: the field again rather than a profile through it.
_TRANSECT_POINTS = 60


def dispersion_k(period_s: float, depth_m: float) -> float:
    """The wavenumber ``k`` solving ``omega^2 = g k tanh(k h)`` - Newton, from deep water.

    Its wavelength is the distance the two strips are held off the structure by."""
    import numpy as np

    omega = 2.0 * np.pi / float(period_s)
    k = omega * omega / _G
    for _ in range(200):
        th = np.tanh(k * depth_m)
        step = (_G * k * th - omega * omega) / (
            _G * th + _G * k * depth_m * (1.0 - th * th))
        k -= step
        if abs(step) < 1e-12:
            break
    return float(k)


def structure_shadow(segments: Any, ux: float, uy: float,
                     fallback_mid: tuple[float, float]
                     ) -> tuple[tuple[float, float], float | None]:
    """The structure's midpoint and the HALF-WIDTH of the strip it blocks.

    Measured on the lateral axis; with no structure there is no strip at all."""
    import numpy as np

    segs = np.asarray(segments, dtype=float).reshape(-1, 4)
    if segs.shape[0] == 0:
        return fallback_mid, None
    allx = np.concatenate([segs[:, 0], segs[:, 2]])
    ally = np.concatenate([segs[:, 1], segs[:, 3]])
    mid = (float(allx.mean()), float(ally.mean()))
    lateral = (allx - mid[0]) * (-uy) + (ally - mid[1]) * ux
    return mid, float(np.abs(lateral).max())


def _sheltering(x: Any, y: Any, kd: Any, *, mid: tuple[float, float],
                wave_uv: tuple[float, float], wavelength_m: float,
                shadow_half_m: float | None) -> dict[str, Any]:
    """The lee and the exposed approach, as the two means and what they averaged.

    THE SHADOW, not the half-plane: both are held inside the lateral extent."""
    import numpy as np

    ux, uy = wave_uv
    along = (x - mid[0]) * ux + (y - mid[1]) * uy
    lateral = (x - mid[0]) * (-uy) + (y - mid[1]) * ux
    in_shadow = np.abs(lateral) <= max(shadow_half_m or 0.0, wavelength_m)
    sheltered = (along > 0.5 * wavelength_m) & in_shadow
    exposed = (along < -0.5 * wavelength_m) & in_shadow
    kd_sheltered = (float(np.nanmean(kd[sheltered])) if sheltered.any() else None)
    kd_exposed = (float(np.nanmean(kd[exposed])) if exposed.any() else None)
    return {
        "kd_sheltered": (round(kd_sheltered, 3) if kd_sheltered is not None
                         else None),
        "kd_exposed": round(kd_exposed, 3) if kd_exposed is not None else None,
        "sheltering_ratio": (round(kd_sheltered / kd_exposed, 3)
                             if kd_sheltered and kd_exposed else None),
        "n_sheltered_nodes": int(sheltered.sum()),
        "n_exposed_nodes": int(exposed.sum()),
        "shadow_half_width_m": (round(float(shadow_half_m), 1)
                                if shadow_half_m else None),
    }


def _transect(x: Any, y: Any, kd: Any, *, mid: tuple[float, float],
              wave_uv: tuple[float, float], wavelength_m: float,
              shadow_half_m: float | None) -> dict[str, Any]:
    """A 1-D Kd profile along the incident direction through the shelter zone.

    The band is the same shadow strip the sheltered and exposed pair is read from."""
    import numpy as np

    ux, uy = wave_uv
    along = (x - mid[0]) * ux + (y - mid[1]) * uy
    band = (np.abs((x - mid[0]) * (-uy) + (y - mid[1]) * ux)
            <= max(shadow_half_m or 0.0, wavelength_m, 1.0))
    if band.sum() < 3:
        band = np.ones_like(along, dtype=bool)
    order = np.argsort(along[band])
    s = along[band][order]
    v = kd[band][order]
    if s.size > _TRANSECT_POINTS:
        pick = np.linspace(0, s.size - 1, _TRANSECT_POINTS).astype(int)
        s, v = s[pick], v[pick]
    return {"agitation_curve_m": np.round(s, 1).tolist(),
            "agitation_curve_kd": np.round(v, 3).tolist(),
            "agitation_curve_kind": "diffraction_transect"}


def _field(result: dict[str, Any]) -> tuple[Any, Any]:
    """``(WAVE HEIGHT, BOTTOM)`` at the mesh nodes, from the solved result.

    The last record is the answer: an elliptic solve is steady state."""
    import numpy as np

    def _named(*words: str) -> Any:
        for name in result["varnames"]:
            upper = name.strip().upper()
            if any(word in upper for word in words):
                data = result["data"].get(name)
                if data is not None and data.size:
                    return np.asarray(data)[-1]
        return None

    hs = _named("WAVE HEIGHT", "HAUTEUR HOULE")
    if hs is None:
        raise OpenWaterError(
            f"the solved agitation file carries {result['varnames']} and no wave "
            "height, so there is no agitation field to publish.",
            error_code="ARTEMIS_NO_LAYERS")
    return hs, _named("BOTTOM", "FOND")


def _provenance(run: dict[str, Any], measured: dict[str, Any]
                ) -> list[SyntheticInput]:
    """The physically dominant inputs, as rows the layer carries."""
    segments = run.get("structure_segments") or []
    rows = [
        SyntheticInput(
            param="wave_period_s", value=round(float(run["wave_period_s"]), 1),
            units="s", basis="default_demo", consequence="physics",
            note="prescribed monochromatic incident wave period"),
        SyntheticInput(
            param="wave_height_m", value=round(float(run["wave_height_m"]), 2),
            units="m", basis="default_demo", consequence="physics",
            note="prescribed incident wave height H0 on the designated liquid "
                 "boundary; Kd is measured against it"),
        SyntheticInput(
            param="mesh_bed", value=str(run.get("bed_source") or "staged"),
            basis="fetched", consequence="physics",
            real_source_if_any=str(run.get("bed_source") or None) or None,
            note="the elevation every node of the harbour carries; the solve "
                 "reads it as the bathymetry the wave refracts over"),
    ]
    if segments:
        rows.append(SyntheticInput(
            param="structure", value=f"supplied_{len(segments)}_segments",
            basis="user", consequence="scenario",
            note=(f"the structure supplied for this run, {len(segments)} segment"
                  f"{'s' if len(segments) != 1 else ''} of it, meshed as a "
                  f"conformal cut and forced as a solid face reflecting "
                  f"{float(run['reflection_coef']):g} of the incident energy; "
                  f"{int(run['structure_boundary_nodes'])} boundary nodes took "
                  "that face")))
    else:
        rows.append(SyntheticInput(
            param="structure", value=None, basis="derived",
            consequence="scenario",
            note="NO structure was declared, so the domain was solved as OPEN "
                 "WATER and every Kd here is the unsheltered response. Hand the "
                 "slot a breakwater layer (fetch_osm_breakwaters) or a drawn "
                 "line to model one."))
    rows.append(SyntheticInput(
        param="mesh_domain",
        value=f"{run['mesh_name']} ({run['mesh_node_count']} nodes / "
              f"{run['mesh_element_count']} elements)",
        basis="user", consequence="numerical",
        real_source_if_any="build_mesh (mesher=om2d)",
        note=(f"solved on the accepted mesh: edges "
              f"{run['mesh_edge_min_m']:g}-{run['mesh_edge_max_m']:g} m "
              f"(median {run['mesh_size_m']:g} m), "
              f"{run['open_boundary_nodes']} of {run['boundary_nodes']} boundary "
              f"nodes designated liquid - the edge the "
              f"{measured['wavelength_m']:g} m incident wave enters through")))
    return rows


def _honesty_note(run: dict[str, Any], measured: dict[str, Any]) -> str:
    bed = str(run.get("bed_source") or "the bed the mesh carries")
    return (
        "Phase-RESOLVING agitation SCREENING: ARTEMIS elliptic mild-slope "
        f"(Berkhoff) over the authored mesh {run['mesh_name']!r} (median element "
        f"edge {run['mesh_size_m']:g} m) carrying {bed}, driven by a PRESCRIBED "
        f"monochromatic {float(run['wave_period_s']):g} s / "
        f"{float(run['wave_height_m']):g} m incident wave - a labeled demo "
        "forcing, not an observed sea state. The raster is the steady-state "
        f"agitation coefficient Kd = Hs/H0 over a {measured['mean_depth_m']:g} m "
        "mean depth. Not a calibrated hindcast.")


async def publish_agitation_products(*, run: dict[str, Any],
                                     solve: dict[str, Any]
                                     ) -> ArtemisAgitationLayerURI:
    """The solved harbour -> its published Kd layer, its scalars and its curve."""
    import numpy as np

    from .result_reader import read_selafin
    from .postprocess_telemac import postprocess_artemis

    run_id = solve["run_id"]
    utm_epsg = int(solve["utm_epsg"])
    local = await asyncio.to_thread(
        download_result, run_id, run["result_basename"],
        error_code="ARTEMIS_OUTPUT_MISSING")
    try:
        result = await asyncio.to_thread(read_selafin, local)
    finally:
        Path(local).unlink(missing_ok=True)

    hs, bed = _field(result)
    x = np.asarray(result["x"], dtype=float)
    y = np.asarray(result["y"], dtype=float)
    h0 = float(run["wave_height_m"])
    kd = hs / max(h0, 1e-9)
    mean_depth = float(-np.nanmean(bed)) if bed is not None else 0.0
    wavelength = (2.0 * np.pi
                  / dispersion_k(float(run["wave_period_s"]), max(mean_depth, 1.0)))

    direction = np.radians(float(run["wave_direction_deg"]))
    wave_uv = (float(np.cos(direction)), float(np.sin(direction)))
    mid, shadow_half = structure_shadow(
        run.get("structure_segments") or [], wave_uv[0], wave_uv[1],
        (float(x.mean()), float(y.mean())))
    measured: dict[str, Any] = {
        "wavelength_m": round(float(wavelength), 1),
        "mean_depth_m": round(mean_depth, 1),
        "hs_max_m": round(float(np.nanmax(hs)), 3),
        **_sheltering(x, y, kd, mid=mid, wave_uv=wave_uv,
                      wavelength_m=float(wavelength), shadow_half_m=shadow_half),
        **_transect(x, y, kd, mid=mid, wave_uv=wave_uv,
                    wavelength_m=float(wavelength), shadow_half_m=shadow_half),
    }

    layers, raster = await asyncio.to_thread(
        postprocess_artemis, run_id=run_id, utm_epsg=utm_epsg, x=x, y=y,
        ikle=result["ikle2"], hs=hs, incident_hs_m=h0,
        reach_name=run["domain_slug"])
    if not layers:
        raise OpenWaterError("the agitation postprocess produced no layer.",
                             error_code="ARTEMIS_NO_LAYERS")

    published = await publish_product_layer(
        layers[0], style=TELEMAC_AGITATION_STYLE,
        update={
            "kd_sheltered": measured["kd_sheltered"],
            "kd_exposed": measured["kd_exposed"],
            "wave_period_s": float(run["wave_period_s"]),
            "mesh_size_m": float(run["mesh_size_m"]),
            "mesh_resolution_label": (
                f"authored om2d mesh, {run['mesh_node_count']} nodes at a "
                f"{run['mesh_size_m']:g} m median edge "
                f"({run['mesh_edge_min_m']:g}-{run['mesh_edge_max_m']:g} m)"),
            "fallback_note": _honesty_note(run, measured),
            "synthetic_inputs": _provenance(run, measured),
            "run_id": run_id,
            "boundary_states": run["boundary_states"],
            "agitation_curve_m": measured["agitation_curve_m"],
            "agitation_curve_kd": measured["agitation_curve_kd"],
            "agitation_curve_kind": measured["agitation_curve_kind"],
        })

    logger.info("telemac artemis complete run_id=%s domain=%s kd_max=%.3g "
                "sheltered=%s exposed=%s over %d/%d nodes uri=%s", run_id,
                run["domain_slug"], published.kd_max, measured["kd_sheltered"],
                measured["kd_exposed"], measured["n_sheltered_nodes"],
                measured["n_exposed_nodes"], published.uri)
    _journal_sheltering(measured, raster)
    return published


def _journal_sheltering(measured: dict[str, Any],
                        raster: dict[str, Any]) -> None:
    """What the two narrated means actually averaged, as the run's own note."""
    from trid3nt_server.workflows.runtime import journal_note

    if measured["kd_sheltered"] is None or measured["kd_exposed"] is None:
        journal_note(
            "sheltering: the incident direction and the declared structure leave "
            "no strip with nodes on both sides of the barrier, so no "
            "sheltered/exposed pair was measured. The Kd field stands on its own.")
        return
    journal_note(
        f"sheltering: Kd {measured['kd_exposed']:.3g} on the exposed approach "
        f"against {measured['kd_sheltered']:.3g} in the lee, each a mean over the "
        f"structure's own {measured['shadow_half_width_m'] or 0:g} m half-width "
        f"shadow strip held half a {measured['wavelength_m']:g} m wavelength clear "
        f"of the barrier ({measured['n_exposed_nodes']} and "
        f"{measured['n_sheltered_nodes']} nodes). The raster carries a value on "
        f"{100.0 * raster['valid_pixel_fraction']:.0f}% of its pixels.")


class AgitationProducts:
    """The solved harbour's deliverable, as the door binds it."""

    @staticmethod
    def agitation(*, run: Any, solve: Any) -> Step:
        """The Kd field, the sheltering pair and the transect through the shadow."""
        return Step(runner=f"{_PRODUCTS}.agitation.publish_agitation_products",
                    stage="publish", kwargs={"run": run, "solve": solve})
