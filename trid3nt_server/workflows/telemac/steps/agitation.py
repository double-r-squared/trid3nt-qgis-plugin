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

THE MESH IS A SLOT TOO. A mesh supplied on the invocation is taken as it stands:
its geometry, its bed and its designated liquid boundary are staged into the run
directory and the worker solves on them rather than laying its own grid over the
AOI. Nothing here has an opinion about a supplied mesh beyond refusing one an
ARTEMIS solve cannot read. Unfilled, the deck asks for the uniform grid it always
did, which is a labeled fallback and not a stance.
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

#: Where a supplied mesh's two halves land in the run directory. The worker READS
#: these names; the pair travels together because a boundary file is only valid
#: against the geometry whose numbering it was written from.
_STAGED_MESH_SLF = "supplied_mesh.slf"
_STAGED_MESH_CLI = "supplied_mesh.cli"
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
    supplied_mesh: Any = None,
) -> dict[str, Any]:
    """Serialize the approved sheet into the worker's agitation config + run meta.

    ``structure`` is whatever satisfied the template's context slot - a fetched
    breakwater layer, a drawn line, a proposed barrier - normalized to lon/lat
    polylines by the one shared reader. ``None`` is a legal answer and means the
    domain is solved as OPEN WATER, which the honesty note says out loud.

    ``supplied_mesh`` is the mesh the run was handed, if it was handed one. It
    REPLACES the grid ask end to end: the geometry pair is staged, the resolution
    lever stops describing anything the solve did, and the bed the nodes carry is
    the bed the solve reads - so all three say so rather than reporting the
    request back.
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
    mesh = await asyncio.to_thread(resolve_supplied_mesh, supplied_mesh, real=real)

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
    inputs = staged_bed_inputs(bed, real=real, section=_SECTION)
    if mesh is not None:
        # The supplied mesh IS the domain, so the deck stops asking for a grid and
        # the bed raster stops being staged: the worker reads the bed off the mesh
        # it was handed, and an input nothing opens is not staged at all.
        config.pop("target_resolution_m")
        config["bathy_source"] = "supplied_mesh"
        config["supplied_mesh_slf"] = _STAGED_MESH_SLF
        config["supplied_mesh_cli"] = _STAGED_MESH_CLI
        inputs = [{"gs_uri": mesh.slf_uri, "dest": _STAGED_MESH_SLF},
                  {"gs_uri": mesh.cli_uri, "dest": _STAGED_MESH_CLI}]
        resolution = _mesh_edge_m(mesh) or resolution
    # The deck travels through the ledger as JSON, so what it carries about the
    # mesh is a record and not the artifact object: a resumed run rehydrates a
    # dict, and a dict is what every reader below reads.
    mesh_record = _mesh_record(mesh)
    return {
        "config": config,
        "inputs": inputs,
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
        "supplied_mesh": mesh_record,
        "bathy_label": (_supplied_bed_label(mesh_record) if mesh_record
                        else _bathy_label(real, str(wave_mode), lake)),
    }


def _mesh_record(art: Any) -> dict[str, Any] | None:
    """The facts about a supplied mesh the deck carries, as plain JSON."""
    if art is None:
        return None
    return {
        "name": art.name, "mesher": art.mode,
        "node_count": int(art.node_count), "element_count": int(art.element_count),
        "crs_authid": art.crs_authid,
        "display_uri": art.display_uri, "slf_uri": art.slf_uri,
        "cli_uri": art.cli_uri, "recipe_uri": art.recipe_uri,
        "dem_source": str((art.provenance or {}).get("dem_source") or ""),
        "open_boundary_info": dict(art.open_boundary_info or {}),
        "edge_length_m": _mesh_edge_m(art),
    }


def resolve_supplied_mesh(supplied: Any, *, real: bool) -> Any:
    """The mesh handed to this run, checked against what ARTEMIS can read.

    Refuses by name rather than falling through: a run told to solve on a mesh and
    quietly given a grid instead answers a different question than the one asked.
    An ARTEMIS domain needs BOTH halves of the pair - the SELAFIN geometry with a
    bed at its nodes and the boundary file numbered from that geometry's own walk -
    because the liquid stretch the incident wave enters through is the boundary
    file's to name.
    """
    from trid3nt_server.workflows.mesh.tool import supplied_mesh_artifact

    if supplied is None or not str(supplied).strip():
        return None
    if not real:
        raise OpenWaterError(
            "an ANALYTIC agitation domain is authored by its own physics - a "
            "seiche ladder, a Berkhoff shoal, a flat-bed Sommerfeld half-plane - "
            "so a supplied mesh has nothing to be. Ask for the real-bathymetry "
            "class, or drop the mesh.",
            error_code="ARTEMIS_SUPPLIED_MESH_UNSUPPORTED_MODE")
    art = supplied_mesh_artifact(supplied, engine="telemac")
    if not art.cli_uri:
        raise OpenWaterError(
            f"the mesh supplied for this run ({art.name!r}) carries no boundary "
            "file, so no stretch of its boundary is designated liquid and a "
            "prescribed incident wave has no edge to enter through; open a "
            "seaward boundary on the mesh before solving on it.",
            error_code="ARTEMIS_SUPPLIED_MESH_CLOSED")
    logger.info("telemac artemis: solving on the supplied mesh %r "
                "(%d nodes / %d elements, %s)", art.name, art.node_count,
                art.element_count, art.crs_authid)
    return art


def _mesh_edge_m(art: Any) -> float | None:
    """The MEAN element edge the supplied mesh measured, in metres."""
    edges = (getattr(art, "probes", None) or {}).get("edge_length_m")
    try:
        return float(dict(edges)["mean"])
    except (KeyError, TypeError, ValueError):
        return None


def _supplied_bed_label(mesh: dict[str, Any]) -> str:
    """What painted the bed the SOLVE reads - the supplied mesh's own provenance."""
    source = str(mesh.get("dem_source") or "source UNRECORDED by the mesh").strip()
    return (f"the bed the supplied mesh {mesh['name']!r} carries at its own nodes "
            f"({source})")


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
    mesh = deck.get("supplied_mesh")
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
            basis="user" if mesh
            else ("fetched" if real else "default_demo"), consequence="physics",
            real_source_if_any=(
                str(mesh.get("dem_source") or "") or None if mesh
                else ("NOAA NGDC Great Lakes lake-datum bathymetry" if real
                      else None)),
            note=deck["bathy_label"]),
    ]
    if deck["wave_mode"] == "diffraction":
        rows.append(_structure_row(deck, metrics))
    if mesh:
        # A supplied mesh moved the whole discretization, so the resolution ask is
        # not what was solved on and the ask-vs-built row would compare two
        # different things. What was solved on is the mesh, and it says so.
        return rows + [_supplied_mesh_row(mesh, metrics)]
    return rows + mesh_sizing_provenance(deck.get("mesh_resolution_asked_m"), metrics)


def _supplied_mesh_row(mesh: dict[str, Any],
                       metrics: dict[str, Any]) -> SyntheticInput:
    """WHICH mesh the solve ran on, read off the SOLVE's own echo of it.

    The artifact says what was handed over; only the worker knows what it read, so
    the counts here are the run's. A solve that echoes no mesh source gets a row
    that says so: an unconfirmed mesh is not the same fact as a confirmed one.
    """
    source = str(metrics.get("mesh_source") or "").strip()
    if source != "supplied":
        return SyntheticInput(
            param="mesh_domain", value=mesh["name"], basis="user",
            consequence="numerical",
            real_source_if_any=f"build_mesh (mesher={mesh['mesher']})",
            note=(f"the mesh {mesh['name']!r} was supplied and staged, but the "
                  "solve reported no mesh source, so whether it ran on that mesh "
                  "or on a grid of its own is UNMEASURED."))
    clamped = metrics.get("bed_clamped_nodes")
    edges = (f"edges {metrics.get('mesh_edge_min_m')}-"
             f"{metrics.get('mesh_edge_max_m')} m (median "
             f"{metrics.get('mesh_edge_median_m')} m)")
    return SyntheticInput(
        param="mesh_domain",
        value=f"{mesh['name']} ({metrics.get('npoin')} nodes / "
              f"{metrics.get('nelem')} elements)",
        basis="user", consequence="numerical",
        real_source_if_any=f"build_mesh (mesher={mesh['mesher']})",
        note=(f"solved on the mesh supplied for this invocation: {edges}, "
              f"{metrics.get('mesh_open_boundary_nodes')} of "
              f"{metrics.get('mesh_boundary_nodes')} boundary nodes designated "
              f"liquid (the incident edge), "
              f"{metrics.get('mesh_structure_face_nodes')} on the structure cut. "
              + (f"{clamped} node(s) whose bed sat above "
                 f"{metrics.get('bed_clamp_depth_m')} m depth were CLAMPED to it "
                 "- the structure crests and the shoreline fringe the mesh's own "
                 "outline left in - so the bed near them is the clamp, not the "
                 "survey." if clamped else "No node needed a bed clamp.")))


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
    mesh = deck.get("supplied_mesh")
    domain = (f"the SUPPLIED unstructured mesh {mesh['name']!r} (mean element "
              f"edge {deck['mesh_size_m']:g} m) carrying {deck['bathy_label']}"
              if mesh
              else f"a {deck['mesh_size_m']:g} m grid of {deck['bathy_label']}")
    return (
        "Phase-RESOLVING agitation SCREENING: ARTEMIS elliptic mild-slope "
        f"(Berkhoff) over {domain}, "
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
            "mesh_resolution_label": (
                f"supplied {(deck.get('supplied_mesh') or {}).get('mesher')} mesh, "
                f"{metrics.get('npoin')} nodes at a "
                f"{metrics.get('mesh_edge_median_m')} m median edge "
                f"({metrics.get('mesh_edge_min_m')}-"
                f"{metrics.get('mesh_edge_max_m')} m)"
                if deck.get("supplied_mesh")
                else mesh_resolution_label(
                    "real NOAA lake bathy" if deck["real_bathymetry"]
                    else "idealized analytic", deck, metrics)),
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
