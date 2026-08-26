"""The ARTEMIS deck and deliverable: a swell at the mouth, an agitation field in.

One serialization hook and one publisher for ARTEMIS, the phase-RESOLVING elliptic
mild-slope (Berkhoff) solver: diffraction behind a breakwater, harbour resonance,
refraction over a shoal. Staging, dispatching and reading the run are the shared
open-water front (``steps/open_water.py``); what lives here is only what is
AGITATION about an agitation run.

THE STRUCTURE IS A SLOT. The sheltering question is meaningless without the thing
that shelters, but WHICH thing is not something this step gets to decide. The
template declares a producer-less ``structure`` slot; whatever fills it - a layer
from ``fetch_osm_breakwaters``, a line the user drew, a barrier they are proposing
- is meshed as a thin solid barrier. Nothing fills it and the run solves OPEN
WATER and says so. This module must never reach out to Overpass itself when the
caller names no structure: that chooses a default the question never asked for,
and does it outside the fetcher router's cache, ladders and provenance.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import (
    TELEMAC_AGITATION_STYLE_PRESET,
    ArtemisAgitationLayerURI,
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
    solved_domain_bbox,
    solves_on_real_bed,
    staged_bed_inputs,
)

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.agitation")

__all__ = ["Agitation", "publish_agitation_products", "write_agitation_deck"]

_STEPS = "trid3nt_server.workflows.telemac.steps"

_SECTION = "agitation"
_PREFIX = "artemis"
_RESULT = "agit_field.slf"
_OUTPUTS = [
    "agit_field.slf", "res_agitation.slf", "geo_agit.slf", "bc_agit.cli",
    "art_agit.cas", "full_listing.log", "artemis_agit.log",
    "telemac_metrics.json",
]


#: Only the DIFFRACTION class has a real harbour to mesh. Resonance and shoal are
#: the ANALYTIC verification domains (a seiche ladder, the Berkhoff-Booij-Radder
#: elliptic shoal), so a real-bathymetry request for those falls back to the
#: idealized domain, labeled, rather than fabricating a harbour outline.
_REAL_BATHY_MODES = ("diffraction",)


async def write_agitation_deck(
    *,
    aoi: dict[str, Any],
    wave_mode: str = "diffraction",
    wave_period_s: float = 8.0,
    wave_direction_deg: float = 90.0,
    wave_height_m: float = 1.0,
    reflection_coef: float = 1.0,
    structure: Any = None,
    mesh_resolution_m: float | None = None,
    bathy_source: str = "auto",
    bed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize the approved sheet into the worker's agitation config + run meta.

    ``structure`` is whatever satisfied the template's context slot - a fetched
    breakwater layer, a drawn line, a proposed barrier - normalized to lon/lat
    polylines by the one shared reader. ``None`` is a legal answer and means the
    domain is solved as OPEN WATER, which the honesty note says out loud.
    """
    from trid3nt_server.workflows.shared.supplied_geometry import supplied_polylines
    from trid3nt_server.workflows.telemac.run_telemac import ARTEMIS_SOLVER_NAME
    # Lazily: the template package imports this module, so the labeled
    # defaults are read where they are used rather than at import time.
    from trid3nt_server.workflows.telemac.agitation.declarations import (
        DEFAULT_IDEALIZED_RES_M,
        DEFAULT_REAL_RES_M,
    )

    lake = great_lake_for(float(aoi["lon"]), float(aoi["lat"]))
    real = solves_on_real_bed(bathy_source, domain_kind="lake",
                              lon=aoi["lon"], lat=aoi["lat"],
                              mode=wave_mode, real_bed_modes=_REAL_BATHY_MODES)
    resolution = (float(mesh_resolution_m) if mesh_resolution_m is not None
                  else (DEFAULT_REAL_RES_M if real else DEFAULT_IDEALIZED_RES_M))
    # Reading a supplied vector is file I/O; it must not run on the loop.
    polylines = await asyncio.to_thread(
        supplied_polylines, structure, label="structure",
        code="ARTEMIS_STRUCTURE_INVALID") or None

    config: dict[str, Any] = {
        "name": aoi["slug"],
        "wave_mode": str(wave_mode),
        "bathy_source": "noaa_greatlakes" if real else "idealized",
        "wave_period_s": float(wave_period_s),
        "wave_dir_deg": float(wave_direction_deg),
        "wave_height_m": float(wave_height_m),
        "reflection_coef": float(reflection_coef),
        "target_resolution_m": float(resolution),
    }
    if real:
        config["bbox"] = [round(float(v), 4) for v in aoi["bbox"]]
    # The barrier is meshed WHEN the slot was filled, and only then. There is no
    # branch here that goes looking for one.
    if polylines:
        config["breakwater_polylines"] = [
            [[round(lon, 6), round(lat, 6)] for lon, lat in line]
            for line in polylines]
    return {
        "config": config,
        "inputs": staged_bed_inputs(bed, real=real, section=_SECTION),
        "run_tag": new_ulid(),
        "section": _SECTION,
        "prefix": _PREFIX,
        "solver": ARTEMIS_SOLVER_NAME,
        "result_basename": _RESULT,
        "outputs": list(_OUTPUTS),
        "run_failed_code": "ARTEMIS_RUN_FAILED",
        "output_missing_code": "ARTEMIS_OUTPUT_MISSING",
        # Only the REAL-bathymetry harbour is georeferenced. The analytic seiche
        # ladder and the Berkhoff shoal are geography-free by construction and
        # report no zone; their reader rasterizes the local metres directly.
        "requires_utm": real,
        "domain_name": aoi["name"],
        "domain_slug": aoi["slug"],
        "mesh_size_m": float(resolution),
        # What the CALLER asked for, kept beside what was built, so a
        # lever the worker moved leaves a row instead of a silence.
        "mesh_resolution_asked_m": (mesh_resolution_m if mesh_resolution_m is not None
                                    else None),
        "wave_mode": str(wave_mode),
        "wave_period_s": float(wave_period_s),
        "wave_height_m": float(wave_height_m),
        "real_bathymetry": real,
        "breakwater_polylines": polylines,
        "bathy_label": _bathy_label(real, str(wave_mode), lake),
    }


def _bathy_label(real: bool, wave_mode: str, lake: str | None) -> str:
    if real:
        return real_lake_bathy_label(lake)
    if wave_mode == "resonance":
        return ("idealized narrow-mouth harbour basin (analytic seiche ladder; no "
                "real harbour outline fetched)")
    if wave_mode == "shoal":
        return ("EXACT Berkhoff-Booij-Radder (1982) elliptic-shoal bathymetry "
                "(analytic refraction-focusing V&V)")
    return ("idealized flat bed (analytic Sommerfeld semi-infinite breakwater; no "
            "real bathymetry for this AOI)")


def _structure_row(deck: dict[str, Any], metrics: dict[str, Any]) -> SyntheticInput:
    """WHAT was meshed as the barrier, read off the SOLVE and not off the deck.

    The deck says what was ASKED for; only the worker knows what it meshed, and
    it echoes that as ``bw_label`` / ``structure_present``. This row must report
    the echo, because a run whose domain carries a barrier the caller never
    supplied is exactly the answer a sheltering question must never quietly
    give, and reading the request back would report the absence rather than the
    barrier.

    A solve that echoes nothing gets a row that says so. An unmeasured structure
    is not the same fact as no structure, and the two must not read alike.
    """
    lines = deck.get("breakwater_polylines")
    echoed = str(metrics.get("bw_label") or "").strip()
    present = metrics.get("structure_present")

    if lines:
        return SyntheticInput(
            param="structure", value=f"supplied_{len(lines)}_lines",
            basis="user", consequence="scenario",
            note=(f"the structure supplied for this run ({len(lines)} line"
                  f"{'s' if len(lines) != 1 else ''}), meshed as a thin solid "
                  f"barrier{'; the solve reports: ' + echoed if echoed else ''}"))

    if present and echoed:
        return SyntheticInput(
            param="structure", value="not_supplied_but_meshed",
            basis="derived", consequence="scenario",
            note=("NO structure was supplied, but the solve did NOT run open "
                  f"water: it reports {echoed}. Every Kd here is sheltered by a "
                  "barrier this run never asked for, so it is not a free-field "
                  "response and not a measurement of anything supplied. Hand the "
                  "slot a breakwater layer (fetch_osm_breakwaters) or a drawn "
                  "line to model a structure you chose."))

    if present is None:
        return SyntheticInput(
            param="structure", value=None, basis="derived", consequence="scenario",
            note=("NO structure was supplied, and the solve reported nothing "
                  "about what it meshed, so whether this domain carries a "
                  "barrier is UNMEASURED. Do not read these Kd values as a "
                  "free-field response."))

    return SyntheticInput(
        param="structure", value=None, basis="derived", consequence="scenario",
        note="NO structure was supplied, and the solve confirms it meshed none: "
             "the domain was solved as OPEN WATER and every Kd here is the "
             "unsheltered response. Hand the slot a breakwater layer "
             "(fetch_osm_breakwaters) or a drawn line to model one.")


def _provenance(deck: dict[str, Any], metrics: dict[str, Any]) -> list[SyntheticInput]:
    """The physically dominant inputs, as rows the layer carries."""
    real = bool(deck["real_bathymetry"])
    rows = [
        SyntheticInput(
            param="wave_period_s", value=round(deck["wave_period_s"], 1), units="s",
            basis="default_demo", consequence="physics",
            note="prescribed monochromatic incident wave period"),
        SyntheticInput(
            param="wave_height_m", value=round(deck["wave_height_m"], 2), units="m",
            basis="default_demo", consequence="physics",
            note="prescribed incident wave height H0 at the open boundary"),
        SyntheticInput(
            param="bathy_source", value=deck["config"]["bathy_source"],
            basis="fetched" if real else "default_demo", consequence="physics",
            real_source_if_any=("NOAA NGDC Great Lakes lake-datum bathymetry"
                                if real else None),
            note=deck["bathy_label"]),
    ]
    if deck["wave_mode"] == "diffraction":
        rows.append(_structure_row(deck, metrics))
    return rows + mesh_sizing_provenance(deck.get("mesh_resolution_asked_m"), metrics)


#: The x/y metric pair each question class MEASURES, by the worker's own chart
#: kind. Each mode sweeps a different independent variable - distance along a
#: transect, incident period, distance along the shoal axis - and writes it under
#: its own key. Reading only the diffraction pair left resonance and shoal runs
#: publishing a raster with no curve at all, and a chart builder that correctly
#: refused to invent one, so two of the three question classes were silently
#: chartless.
_CURVE_KEYS: dict[str, tuple[str, str]] = {
    "diffraction_transect": ("chart_s_m", "chart_kd"),
    "resonance_sweep": ("chart_period_s", "chart_response"),
    "shoal_axis_transect": ("chart_axis_y_m", "chart_kd"),
}


def _curve_rows(metrics: dict[str, Any]) -> dict[str, Any]:
    """The measured curve as the layer's own fields, whichever mode measured it."""
    kind = str(metrics.get("chart_kind") or "")
    x_key, y_key = _CURVE_KEYS.get(kind, ("chart_s_m", "chart_kd"))
    return {
        "agitation_curve_m": list(metrics.get(x_key) or []) or None,
        "agitation_curve_kd": list(metrics.get(y_key) or []) or None,
        "agitation_curve_kind": metrics.get("chart_kind"),
    }


def _honesty_note(deck: dict[str, Any]) -> str:
    return (
        "Phase-RESOLVING agitation SCREENING: ARTEMIS elliptic mild-slope "
        f"(Berkhoff) over a {deck['mesh_size_m']:g} m grid of {deck['bathy_label']}, "
        f"driven by a PRESCRIBED monochromatic {deck['wave_period_s']:g} s / "
        f"{deck['wave_height_m']:g} m incident wave - a labeled demo forcing, not an "
        "observed sea state. The raster is the steady-state agitation coefficient "
        "Kd = Hs/H0. Not a calibrated hindcast.")


async def publish_agitation_products(*, deck: dict[str, Any],
                                     solve: dict[str, Any]) -> ArtemisAgitationLayerURI:
    """Postprocess the solved harbour into its published layer + scalars."""
    from trid3nt_server.emission.pipeline_emitter import current_emitter
    from trid3nt_server.workflows.telemac.postprocess_telemac import postprocess_artemis

    emitter = current_emitter()
    run_id = solve["run_id"]
    metrics = dict(solve.get("metrics") or {})
    reach = deck["domain_slug"]
    utm_epsg = solve["utm_epsg"]

    slf_path = await asyncio.to_thread(
        download_open_water_result, run_id, deck["result_basename"],
        error_code=deck["output_missing_code"])
    try:
        layers, _pmetrics = await asyncio.to_thread(
            postprocess_artemis, slf_path, run_id=run_id, utm_epsg=utm_epsg,
            # The worker meshes in a LOCAL UTM frame, so the postprocess needs the
            # SW corner to add the origin back before UTM -> 4326. The idealized
            # analytic domain records no bbox and has none to add.
            request_bbox=solved_domain_bbox(deck, metrics),
            incident_hs_m=float(deck["wave_height_m"]), reach_name=reach,
            wave_mode=deck["wave_mode"])
    finally:
        Path(slf_path).unlink(missing_ok=True)
    if not layers:
        raise OpenWaterError("postprocess_artemis produced no agitation layer.",
                             error_code="ARTEMIS_NO_LAYERS")
    raw = layers[0]

    published = await publish_product_layer(
        raw, style_preset=TELEMAC_AGITATION_STYLE_PRESET,
        update={
            "kd_sheltered": metrics.get("kd_sheltered"),
            "kd_exposed": metrics.get("kd_exposed"),
            "resonant_period_s": metrics.get("resonant_period_s"),
            "response_at_resonance": metrics.get("response_at_resonance"),
            "response_off_resonance": metrics.get("response_off_resonance"),
            "wave_period_s": metrics.get("wave_period_s") or deck["wave_period_s"],
            "mesh_size_m": metrics.get("dx_m") or deck["mesh_size_m"],
            "mesh_resolution_label": mesh_resolution_label(
                "real NOAA lake bathy" if deck["real_bathymetry"]
                else "idealized analytic", deck, metrics),
            "fallback_note": _honesty_note(deck),
            "synthetic_inputs": _provenance(deck, metrics),
            "run_id": run_id,
            # The curve the WORKER measured across the field. The chart plots
            # this, so the chart and the narrated scalars are one measurement.
            **_curve_rows(metrics),
        })

    # No structure re-upload here. A supplied layer is ALREADY on the canvas - the
    # fetcher that produced it emitted it, or the user drew it - and staging a
    # second copy of somebody else's layer is the double-emission the input
    # guard exists to catch.
    logger.info("telemac artemis complete run_id=%s domain=%s mode=%s kd_max=%.3g "
                "sheltered=%s exposed=%s uri=%s", run_id, reach, deck["wave_mode"],
                published.kd_max, published.kd_sheltered, published.kd_exposed,
                published.uri)
    return published


class Agitation:
    """The ARTEMIS author + read steps, as the facade binds them."""

    @staticmethod
    def deck(**kwargs: Any) -> Step:
        return Step(runner=f"{_STEPS}.agitation.write_agitation_deck", stage="author",
                    kwargs=kwargs)

    @staticmethod
    def products(*, deck: Any, solve: Any) -> Step:
        return Step(runner=f"{_STEPS}.agitation.publish_agitation_products",
                    stage="publish", kwargs={"deck": deck, "solve": solve})
