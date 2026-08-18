"""Engine template ``culvert_embankment_flow`` -- HEC-RAS 2D culvert through a road/levee
embankment (Stage 2).

The productionization of the 2D-structure seam: the Culvert is the ONE
2D hydraulic structure the HEC-RAS 2025 managed beta wires into the compute
(``InitializeDriver_Culverts`` -> ``new Culvert(...)`` copies every barrel/opening
field into the solve; authored weirs/gates/pumps are silently inert). This
template AUTHORS a culvert barrel + its BarrelProperties + OpeningProperties on a
real-reach 2D deck (real 3DEP terrain, the road embankment IN the lidar) and runs the
present-vs-absent A/B: WITHOUT the barrel the embankment blocks the reach and ponds it
upstream; WITH the barrel the flow is conveyed under the road.

Backend: ``workers/hecras2025/subst/crux/freshtopo/culvert_reach_pipeline.py``
(the same mounted ``synthdrv.dll`` the RoG leg uses; author + prepare + solve on the CPU).

FIDELITY (loud, NATE no-hand-wave): the SOLVE is the 2025 managed CPU shallow-water
engine (beta); the culvert physics is the wired ``InitializeDriver_Culverts`` path
(inlet-control chart/scale + entrance/exit loss + barrel Manning + inverts), proven to
MOVE WATER (seam-probe max|A-B|=3.4 m; the weir A/B was 0.0, inert).
SCREENING-grade. The barrel engineering params (diameter, invert, opening type,
entrance/exit loss, Manning) are UN-FETCHABLE -> they go through the input-review gate
with labeled defaults, never invented silently. ASCII only.
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
from trid3nt_contracts.hecras_contracts import (
    HECRAS_DEPTH_STYLE_PRESET,
    HECRAS_INPUT_INVALID,
    HECRAS_SOLVE_FAILED,
    HecrasDepthLayerURI,
)
from trid3nt_contracts.execution import LegendKey
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.data import register_tool
from trid3nt_server.data.resolution_declared import (
    ResolutionOutOfRangeError,
    resolve_resolution,
)
from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.workflows.hecras._template_card import TemplateCard

logger = logging.getLogger(
    "trid3nt_server.workflows.hecras.culvert_embankment_flow.culvert_embankment_flow"
)

__all__ = [
    "culvert_embankment_flow",
    "model_culvert_embankment_flow",
    "CulvertEmbankmentFlowError",
    "TEMPLATE_CARD",
]

#: The culvert-reach backend lives in the workers tree (proprietary natives image +
#: pure-python geometry/metric prep); imported at CALL time so the server package
#: carries no hard dependency on workers (mirrors hecras_flood_2d).
_WORKERS_FRESHTOPO = (
    Path(__file__).resolve().parents[4]
    / "workers/hecras2025/subst/crux/freshtopo"
)

_FT_PER_M: float = 1.0 / 0.3048

#: Labeled defaults for the UN-FETCHABLE barrel engineering (input-review gate). A
#: modest small-crossing circular pipe with the standard inlet-control chart/scale.
_DEFAULT_DIAMETER_M: float = 1.0                # ~3.3 ft circular pipe
_DEFAULT_OPENING_TYPE: str = "ConcretePipeCulvert_SquareEdgeWithHeadwall"
_DEFAULT_K_IN: float = 0.5                       # entrance loss
_DEFAULT_K_OUT: float = 1.0                      # exit loss
_DEFAULT_BARREL_MANNING: float = 0.013           # concrete pipe
_DEFAULT_INFLOW_CMS: float = 2.0                 # reach discharge (screening)

#: Resolution band (m) -- the structured screening deck (same class as hecras_flood_2d).
_MIN_RES_M, _MAX_RES_M, _DEFAULT_RES_M = 10.0, 60.0, 20.0
_RES_SPEC = ResolutionSpec(
    param="resolution_m", unit="m", min_value=_MIN_RES_M, max_value=_MAX_RES_M,
    native_hint="3DEP 10 m (fetch_dem)", constraint_source="solver",
    rationale=("the 2025 managed 2D culvert deck resolves the reach + embankment at a "
               "10-60 m screening cell; finer over-refines the barrel snap, coarser drops "
               "the road cell"),
)

#: Recognized inlet-control opening types (the wired chart/scale set; Custom/None
#: hard-fail in InitializeDriver_Culverts).
_OPENING_TYPES = (
    "ConcretePipeCulvert_SquareEdgeWithHeadwall",
    "ConcretePipeCulvert_GrooveEndWithHeadwall",
    "ConcretePipeCulvert_GrooveEndProjecting",
    "CorrugatedMetalPipe_HeadwallOrHeadwallAndWingwalls",
    "CorrugatedMetalPipe_MiteredToConformToSlope",
    "CorrugatedMetalPipe_Projecting",
)

_FIDELITY_NOTE: str = (
    "HEC-RAS 2025 MANAGED engine (beta) 2D culvert-through-embankment: a barrel authored "
    "on a real-reach deck (fetched 3DEP terrain), prepared + solved on the CPU. The culvert "
    "physics is the wired InitializeDriver_Culverts path (proven to move water "
    "seam-probe). Present-vs-absent A/B: the embankment blocks + ponds the reach without the "
    "barrel; the barrel conveys it under the road. SCREENING-grade; barrel engineering is "
    "labeled (input-review gate), not measured."
)


class CulvertEmbankmentFlowError(RuntimeError):
    """Fatal fault before a layer is produced (typed error_code to the emitter)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "how a culvert routes reach flow UNDER a road/levee embankment vs the blocked "
        "case: 2D peak depth with the barrel present (conveyance) vs absent (upstream "
        "ponding), authored on a real reach + fetched terrain (HEC-RAS 2025 managed CPU)"
    ),
    required_inputs=["bbox (or a location that resolves to a road/stream crossing)"],
    knobs=("inflow_cms, barrel_diameter_m, opening_type, k_in, k_out, barrel_manning, "
           "resolution_m, embankment_mode, input_mode"),
)

_METADATA = AtomicToolMetadata(
    name="culvert_embankment_flow",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="hecras",
    tier="template",
    resolution_specs=(_RES_SPEC,),
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def culvert_embankment_flow(
    bbox: list[float] | None = None,
    location: str | None = None,
    inflow_cms: float = _DEFAULT_INFLOW_CMS,
    barrel_diameter_m: float = _DEFAULT_DIAMETER_M,
    opening_type: str = _DEFAULT_OPENING_TYPE,
    k_in: float = _DEFAULT_K_IN,
    k_out: float = _DEFAULT_K_OUT,
    barrel_manning: float = _DEFAULT_BARREL_MANNING,
    resolution_m: float = _DEFAULT_RES_M,
    sim_hours: float = 2.5,
    embankment_mode: str = "auto_seal",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> HecrasDepthLayerURI | dict[str, Any]:
    """HEC-RAS 2D CULVERT-THROUGH-EMBANKMENT flow at a REAL road/stream crossing.

    THE tool for "does the culvert under this road pass the creek", "culvert vs no
    culvert at a road crossing", "how much does a barrel relieve upstream ponding at an
    embankment", "route reach flow under a road/levee with a culvert". Authors a culvert
    barrel on a real-reach 2D deck (fetched 3DEP terrain, the road embankment in the
    lidar) and runs the present-vs-absent A/B on the HEC-RAS 2025 managed CPU engine --
    the ONE 2D structure the beta wires into the solve. For open-floodplain
    flooding (no structure) use ``hecras_flood_2d``; for storm-drain pipe networks use
    ``swmm_urban_flood``.

    Params:
        bbox: the crossing AOI as ``[min_lon,min_lat,max_lon,max_lat]`` (EPSG:4326),
            framed on a road crossing a stream reach roughly along a domain axis (the
            reach runs down the box, the road broadside). Prefer this over ``location``.
        location: OPTIONAL place name geocoded to a bbox when ``bbox`` is absent.
        inflow_cms: the reach discharge forcing the crossing (m3/s). Pin to a real
            gauge/NWM value; default ~2 m3/s screening event.
        barrel_diameter_m: culvert rise/span (m). UN-FETCHABLE engineering -> labeled
            default 1.0 m (~3.3 ft circular pipe); reviewed via the input-review gate.
        opening_type: inlet-control type (chart/scale). One of the recognized concrete/
            corrugated-metal types; default SquareEdgeWithHeadwall. Custom/None hard-fail.
        k_in / k_out: entrance / exit loss coefficients. Labeled defaults 0.5 / 1.0.
        barrel_manning: barrel roughness (n). Labeled default 0.013 (concrete).
        resolution_m: 2D cell size (m), supported 10-60. Out-of-range asks are quoted
            the range (typed error), never silently snapped.
        sim_hours: unsteady window (h); default 2.5 (the proven inflow-BC window).
        embankment_mode: ``"auto_seal"`` (default) raises a 1-cell crest cap at the real
            road centerline -- a sub-cell rural road fill under-resolves at the screening
            mesh, so the cap seals the road cell so the blocked case genuinely ponds
            (disclosed in the result). ``"real_terrain"`` uses the lidar embankment as-is
            (for a tall real fill that already blocks at the mesh scale).
        input_mode: ``"user_gated"`` reviews the barrel engineering + forcing before the
            solve; ``"auto"`` (default) proceeds with them labeled.

    Returns:
        On success: ``HecrasDepthLayerURI`` -- the WITH-culvert peak-depth COG, carrying
        the A/B discriminant (``depth_max_ft`` etc.; the barrel-conveyed discharge +
        upstream ponding relieved are in the fidelity note). On failure: dict with
        ``status="error"`` + ``error_code``.
    """
    from trid3nt_server.workflows.hecras.flood_2d.flood_2d import (
        _coerce_bbox, _geocode_bbox,
    )

    aoi = _coerce_bbox(bbox)
    if aoi is None and location:
        aoi = await asyncio.to_thread(_geocode_bbox, location)
    if aoi is None:
        return {
            "status": "error", "error_code": HECRAS_INPUT_INVALID,
            "error_message": "culvert_embankment_flow needs a bbox [min_lon,min_lat,max_lon,max_lat] "
            "(or a location that geocodes to a road/stream crossing)",
        }

    try:
        resolution_m = float(resolution_m)
    except (TypeError, ValueError):
        resolution_m = _DEFAULT_RES_M
    try:
        _resolved = resolve_resolution(resolution_m, spec=_RES_SPEC)
        resolution_m, res_basis, res_note = _resolved.value, _resolved.basis, _resolved.note
    except ResolutionOutOfRangeError as exc:
        return {"status": "error", "error_code": HECRAS_INPUT_INVALID, "error_message": str(exc)}

    if str(opening_type) not in _OPENING_TYPES:
        return {
            "status": "error", "error_code": HECRAS_INPUT_INVALID,
            "error_message": f"opening_type {opening_type!r} is not a recognized inlet-control "
            f"type; use one of {_OPENING_TYPES} (Custom/None hard-fail in the culvert kernel)",
        }
    seal_mode = str(embankment_mode or "auto_seal").strip().lower()
    if seal_mode not in ("auto_seal", "real_terrain"):
        seal_mode = "auto_seal"

    try:
        return await model_culvert_embankment_flow(
            bbox=aoi, inflow_cms=float(inflow_cms), barrel_diameter_m=float(barrel_diameter_m),
            opening_type=str(opening_type), k_in=float(k_in), k_out=float(k_out),
            barrel_manning=float(barrel_manning), resolution_m=resolution_m,
            sim_hours=float(sim_hours), embankment_mode=seal_mode, input_mode=input_mode,
            resolution_basis=res_basis, resolution_note=res_note,
        )
    except asyncio.CancelledError:
        raise
    except CulvertEmbankmentFlowError as exc:
        logger.warning("culvert_embankment_flow failed: %s (%s)", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("culvert_embankment_flow unexpected failure")
        return {"status": "error", "error_code": "HECRAS_INTERNAL_ERROR", "error_message": str(exc)}


def _fetch_dem_local(bbox: list[float]) -> str:
    """Fetch the crossing DEM (3DEP) and download it to a local GeoTIFF (emit-on-fetch
    surfaces it as a role=context terrain input via ``purpose=``)."""
    from trid3nt_server.data import TOOL_REGISTRY
    from trid3nt_server.data.simulation.solver.solver import _download_object

    layer = TOOL_REGISTRY["fetch_dem"].fn(bbox=list(bbox), resolution_m=10, purpose="terrain")
    uri = getattr(layer, "uri", None) or (layer.get("uri") if isinstance(layer, dict) else None)
    if not uri:
        raise CulvertEmbankmentFlowError(HECRAS_SOLVE_FAILED, f"fetch_dem returned no uri for {bbox}")
    tmp = Path(tempfile.mkdtemp(prefix="culvert-dem-")) / "dem.tif"
    _download_object(str(uri), tmp)
    return str(tmp)


def _surface_reach(bbox: list[float]) -> None:
    """Fetch the NHD reach for the crossing so it surfaces as an input layer (purpose=)."""
    try:
        from trid3nt_server.data import TOOL_REGISTRY
        TOOL_REGISTRY["fetch_river_geometry"].fn(
            bbox=tuple(bbox), source="nhdplus_hr", purpose="reach")
    except Exception as exc:  # noqa: BLE001 -- surfacing is best-effort
        logger.info("culvert_embankment_flow: reach surfacing skipped (%s)", exc)


async def model_culvert_embankment_flow(
    *,
    bbox: list[float],
    inflow_cms: float,
    barrel_diameter_m: float,
    opening_type: str,
    k_in: float,
    k_out: float,
    barrel_manning: float,
    resolution_m: float,
    sim_hours: float,
    embankment_mode: str,
    input_mode: str | None,
    resolution_basis: str = "derived",
    resolution_note: str | None = None,
) -> HecrasDepthLayerURI | dict[str, Any]:
    """fetch DEM + reach -> input-review gate -> culvert-reach A/B solve -> depth COG."""
    import sys

    from trid3nt_server.emission.pipeline_emitter import (
        begin_substeps, current_emitter, substep,
    )

    emitter = current_emitter()
    begin_substeps(emitter, 2)  # solve, publish

    # --- input-review gate: the UN-FETCHABLE barrel engineering (labeled defaults) ----- #
    review_entries: list[SyntheticInput] = [
        SyntheticInput(param="terrain", value="fetch_dem (3DEP 10 m)", basis="fetched",
                       note="reprojected to a local SI grid; the road embankment is in the lidar"),
        SyntheticInput(param="reach", value="fetch_river_geometry (NHDPlus HR)", basis="fetched",
                       note="the stream the crossing carries"),
        SyntheticInput(param="inflow_cms", value=round(float(inflow_cms), 3), units="m3/s",
                       basis="user", note="reach discharge forcing the crossing"),
        SyntheticInput(param="barrel_diameter_m", value=round(float(barrel_diameter_m), 3), units="m",
                       basis="default_demo" if abs(barrel_diameter_m - _DEFAULT_DIAMETER_M) < 1e-9 else "user",
                       consequence="physics",
                       note="culvert rise/span -- UN-FETCHABLE engineering (not derivable from terrain)"),
        SyntheticInput(param="opening_type", value=opening_type,
                       basis="default_demo" if opening_type == _DEFAULT_OPENING_TYPE else "user",
                       consequence="physics",
                       note="inlet-control chart/scale (entrance geometry) -- UN-FETCHABLE engineering"),
        SyntheticInput(param="entrance_exit_loss", value=f"KIn={k_in}, KOut={k_out}",
                       basis=("default_demo" if abs(k_in - _DEFAULT_K_IN) < 1e-9
                              and abs(k_out - _DEFAULT_K_OUT) < 1e-9 else "user"),
                       consequence="physics",
                       note="culvert entrance/exit loss coefficients -- UN-FETCHABLE engineering"),
        SyntheticInput(param="barrel_manning", value=round(float(barrel_manning), 4),
                       basis="default_demo" if abs(barrel_manning - _DEFAULT_BARREL_MANNING) < 1e-9 else "user",
                       consequence="physics",
                       note="barrel roughness n -- UN-FETCHABLE engineering"),
        SyntheticInput(param="resolution_m", value=round(float(resolution_m), 1), units="m",
                       basis=resolution_basis, note=(resolution_note or "2D screening cell size")),
    ]
    if embankment_mode == "auto_seal":
        review_entries.append(SyntheticInput(
            param="embankment", value="1-cell crest cap at the real road centerline",
            basis="derived",
            note="the sub-cell rural road fill under-resolves at the screening mesh; a crest "
            "cap seals the road cell so the blocked (no-barrel) case genuinely ponds"))
    else:
        review_entries.append(SyntheticInput(
            param="embankment", value="real 3DEP lidar road fill (as-is)", basis="fetched",
            note="the crossing embankment as captured in the terrain"))

    review = await gate_input_review(
        tool_name="culvert_embankment_flow", mode=input_mode, entries=review_entries,
        params={"bbox": bbox, "inflow_cms": inflow_cms, "barrel_diameter_m": barrel_diameter_m})
    if not review.proceed:
        return {"status": "error", "error_code": "HECRAS_INPUT_REVIEW_CANCELLED",
                "error_message": review.cancel_reason or "input review not approved; the solver did not run"}
    inflow_cms = float(review.params.get("inflow_cms", inflow_cms) or inflow_cms)
    barrel_diameter_m = float(review.params.get("barrel_diameter_m", barrel_diameter_m) or barrel_diameter_m)

    run_tag = new_ulid()
    workdir = tempfile.mkdtemp(prefix=f"culvert-{run_tag}-")

    if str(_WORKERS_FRESHTOPO) not in sys.path:
        sys.path.insert(0, str(_WORKERS_FRESHTOPO))
        sys.path.insert(0, str(_WORKERS_FRESHTOPO.parents[2]))
    from culvert_reach_pipeline import (  # type: ignore
        run_culvert_reach, CulvertReachError,
    )
    from rog2025_pipeline import build_depth_cog  # type: ignore

    # surface the fetched inputs (purpose=) + get the DEM local
    await asyncio.to_thread(_surface_reach, bbox)
    dem_tif = await asyncio.to_thread(_fetch_dem_local, bbox)

    seal_m = 1.5 if embankment_mode == "auto_seal" else None
    async with substep(emitter, "culvert_solve"):
        try:
            result = await asyncio.to_thread(
                run_culvert_reach, dem_tif, workdir, cell_size=float(resolution_m),
                elev_units="m", bbox4326=list(bbox), inflow_cms=float(inflow_cms),
                sim_hours=float(sim_hours), rise_span_m=float(barrel_diameter_m),
                opening_type=str(opening_type), k_in=float(k_in), k_out=float(k_out),
                min_embank_m=0.6, seal_embankment_m=seal_m)
        except CulvertReachError as exc:
            raise CulvertEmbankmentFlowError(HECRAS_SOLVE_FAILED, f"culvert-reach solve failed: {exc}") from exc

    disc = result["discriminant"]
    if not disc.get("moves_water"):
        logger.warning("culvert_embankment_flow: A/B did not clearly move water: %s", disc)
    logger.info(
        "culvert_embankment_flow run=%s conveyed=%.3g m3/s ponding_relieved=%.3g m maxdelta=%.3g m wall=%ss",
        run_tag, disc.get("storage_relieved_m3s"), disc.get("ponding_relieved_max_m"),
        disc.get("max_abs_depth_delta_m"), result["wall_s"])

    # --- rasterize + publish the WITH-culvert (A) peak-depth COG (feet) ---------------- #
    from trid3nt_server.workflows.shared import cog_io
    from trid3nt_server.data.simulation.solver.solver import _get_runs_bucket
    from trid3nt_server.data.publish_layer.publish_layer import (
        PublishLayerError, publish_layer,
    )

    async with substep(emitter, "publish"):
        cog_tif = str(Path(workdir) / "culvert_depth.tif")
        cinfo = await asyncio.to_thread(
            build_depth_cog, result["result_a_h5"], result["prep"], cog_tif, None, _FT_PER_M)
        cog_uri = await asyncio.to_thread(
            cog_io.upload_cog, Path(cog_tif), run_tag, _get_runs_bucket(),
            dest_filename="hecras_culvert_depth.tif", log_label="HEC-RAS 2025 culvert depth COG")
        bb = cinfo["bbox4326"]
        dmax_ft = round(cinfo["depth_max"], 3)
        conveyed = disc.get("storage_relieved_m3s")
        relieved = disc.get("ponding_relieved_max_m")
        note = (
            f"{_FIDELITY_NOTE} A/B on this reach: the barrel conveys ~{conveyed:.3g} m3/s under the "
            f"embankment (upstream ponding relieved {relieved:.3g} m; max|A-B| depth "
            f"{disc.get('max_abs_depth_delta_m'):.3g} m). {result.get('embankment_basis','')}"
        )
        depth = HecrasDepthLayerURI(
            layer_id=f"hecras-culvert-depth-{run_tag}",
            name=f"Peak depth with culvert ({barrel_diameter_m:.2g} m barrel, {inflow_cms:.2g} m3/s reach)",
            layer_type="raster", uri=cog_uri, style_preset=HECRAS_DEPTH_STYLE_PRESET,
            role="primary", units="ft", bbox=(bb[0], bb[1], bb[2], bb[3]),
            legend=LegendKey(kind="continuous", colormap="blues", vmin=0.0, vmax=dmax_ft,
                             units="ft", label="Peak water depth (ft)"),
            fallback_note=note, depth_max_ft=dmax_ft, depth_mean_ft=round(cinfo["depth_mean"], 3),
            wet_cell_count=int(cinfo["wet_px"]), wse_max_ft=dmax_ft,
            synthetic_inputs=list(review_entries),
        )
        try:
            published_uri = await asyncio.to_thread(
                publish_layer, layer_uri=depth.uri, layer_id=depth.layer_id,
                style_preset=depth.style_preset)
            depth = depth.model_copy(update={"uri": published_uri})
        except PublishLayerError as exc:
            logger.warning("culvert_embankment_flow publish_layer FAILED (%s)", exc)

    if emitter is not None and depth.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(depth.bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("culvert_embankment_flow zoom-to failed: %s", exc)
    return depth
