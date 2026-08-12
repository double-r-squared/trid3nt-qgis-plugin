"""Engine template ``schism_pahm_surge`` -- parametric hurricane STORM SURGE (ADR 0217).

SCHISM's barotropic storm-surge archetype: given a hurricane BEST TRACK (central
pressure, max wind, radius of maximum wind along the track), what peak storm-surge
water-level response does a US coast see? The forcing is a STANDALONE parametric
Holland-1980 wind/pressure field authored as SCHISM ``sflux`` atmospheric inputs
(``nws=2``) and solved on the CLEAN hydro-core binary (``pschism_TVD-VL``) -- the
honest, no-rebuild PaHM route for OUR image (the baked full-monty ``USE_PAHM``
binary demands every module namelist, ADR 0115; ``sflux`` is a SCHISM CORE feature).
See ``holland_sflux`` for the physics + the PaHM-vs-standalone decision.

Deliverable: a PEAK storm-surge elevation surface clipped to the AOI + COG (ADR
0116), the best track overlaid as a labeled vector, and a coastal gauge-point surge
HYDROGRAPH. Every surge number the agent narrates comes from the typed
``SchismElevationLayerURI`` fields the postprocess computed (invariant 1).

Fidelity honesty: a SCREENING surge -- symmetric Holland vortex (no forward-motion
GAHM asymmetry), Coriolis-off (wind-setup + inverse-barometer are the dominant
drivers), a still-water open boundary (no tide co-forcing), an internal graded
coastal TIN unless a case mesh is supplied. It answers "what surge response does
this track drive", not a calibrated operational STOFS nowcast. Published-first: the
default track is the published Hurricane Ike (2008, bal092008) best track.

ASCII only.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.schism_contracts import (
    SCHISM_BATHYMETRY_UNAVAILABLE,
    SCHISM_INPUT_INVALID,
    SCHISM_SOLVE_FAILED,
    SchismElevationLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.gates.input_review import gate_input_review
from trid3nt_server.agent.workflows.schism._template_card import TemplateCard

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.schism.pahm_surge.pahm_surge"
)

__all__ = [
    "schism_pahm_surge",
    "model_schism_pahm_surge",
    "SchismSurgeError",
    "TEMPLATE_CARD",
    "PUBLISHED_IKE_TRACK",
]

#: Knots -> m/s, millibar -> Pa, nautical-mile -> m.
_KT_TO_MS = 0.514444
_MB_TO_PA = 100.0
_NMI_TO_M = 1852.0
#: Metres per degree for the deck's local equirectangular inverse (must match
#: deck_authoring's projection).
_M_PER_DEG_LON = 111_320.0
_M_PER_DEG_LAT = 110_540.0

#: The published Hurricane Ike (2008, bal092008) best track approaching the Texas
#: coast, HURDAT2 fixes (UTC) relative to base_date 2008-09-12 06Z. (lon, lat,
#: pres_mb, wind_kt, rmw_nmi) -- Ike was a large storm (RMW ~50 nmi); landfall near
#: Galveston ~09/13 07Z. Published-first anchor (the same storm the geoclaw
#: surge template uses, ADR 0168).
PUBLISHED_IKE_TRACK: tuple[tuple[float, float, float, float, float], ...] = (
    # time_hr, lon, lat, pres_mb, wind_kt, rmw_nmi
    (0.0, -91.5, 26.6, 948.0, 95.0, 50.0),
    (12.0, -93.6, 28.1, 950.0, 80.0, 55.0),
    (18.0, -94.4, 28.8, 951.0, 80.0, 55.0),
    (24.0, -94.7, 29.3, 952.0, 80.0, 50.0),   # ~landfall Galveston
    (30.0, -95.4, 30.5, 964.0, 60.0, 60.0),
)
_IKE_BASE_DATE = (2008, 9, 12, 6)
#: The Ike showcase AOI: GREATER GALVESTON -- the bay, Bolivar Peninsula, Galveston
#: Island, and the open Gulf shelf seaward (south). Generous on purpose so the whole
#: surge footprint (right-of-track lobe onto Bolivar + the bay) is visible; the
#: barotropic screening solve is fast at coarse resolution.
_IKE_BBOX = (-95.4, 28.6, -94.2, 29.95)
_PN_MB = 1008.0  # environmental pressure fallback (POCI-class)

#: Bathymetry-fetch resolution bounds (m); mirrors fetch_topobathy's own
#: resolution_m param bounds (source.yaml: min 1, max 1000) so the autoscaler
#: never proposes something the fetcher would reject.
_SURGE_RES_MIN_M = 25.0
_SURGE_RES_MAX_M = 1000.0
#: Target long-side pixel count for the screening bathymetry COG (the granularity
#: doctrine's "bounded grid dimension" -- 500-800 px is enough detail for a
#: barotropic screening surge without an oversized composite).
_SURGE_BATHY_TARGET_PX = 750.0
#: Target AOI kilometres per internal-TIN node (per axis) -- the NATIVE-default
#: "sane node budget": denser than this wastes solve time on a screening barotropic
#: surge, coarser loses the coastal surge structure (right-of-track lobe, bay
#: funneling). This drives the TIN only on the NATIVE (resolution_m=None) path.
_SURGE_TIN_KM_PER_NODE = 6.0
_SURGE_TIN_DIM_MIN = 8
_SURGE_TIN_DIM_MAX = 40
#: The finest internal-TIN cell a screening barotropic surge will build regardless
#: of an even-finer ask (m): mirrors the fetch resolution_m floor -- below this the
#: graded coarse-TIN screening solve gains no physical fidelity, so the TIN target
#: cell is max(resolution_m, this).
_SURGE_TIN_CELL_FLOOR_M = _SURGE_RES_MIN_M  # 25 m
#: DECLARED node-budget ceiling (per axis) for an EXPLICIT resolution_m ask -- the
#: fidelity-first rule keys TIN density to the requested cell, bounded here. NOT a
#: silent floor/ceiling: the envelope SAYS SO when this binds (declared-clamps
#: ruling). Justification (evidence-based, not a guess): the barotropic screening
#: solve's wall scales with node count; the 0217-0219 runs at ~440 nodes solved in
#: ~30 s, so 80x80 = 6400 nodes (~15x the node count, a few minutes on the same
#: local image) is a comfortably affordable ceiling that keeps a fine ask a
#: screening-scale solve rather than an unbounded refinement.
_SURGE_TIN_DIM_MAX_FINE = 80


def _resolution_driven_tin_dims(
    bbox: tuple[float, float, float, float], resolution_m: float
) -> tuple[int, int, str]:
    """Internal-TIN node grid for an EXPLICIT resolution_m ask: the requested cell
    size drives MESH density (fidelity-first -- a fine ask buys a fine mesh, not just
    a fine bathymetry fetch), bounded by the declared ``_SURGE_TIN_DIM_MAX_FINE``
    node budget.

    Target cell = ``max(resolution_m, _SURGE_TIN_CELL_FLOOR_M)``; nodes per axis =
    AOI extent / cell, clamped to ``[_SURGE_TIN_DIM_MIN, _SURGE_TIN_DIM_MAX_FINE]``.
    Returns ``(nx, ny, budget_note)`` where ``budget_note`` is non-empty ONLY when
    the ceiling binds -- the clamp labels itself (no silent ceiling), quoting the
    requested cell, the unclamped node grid, and the resulting effective cell.
    """
    west, south, east, north = bbox
    mid_lat_rad = math.radians(0.5 * (south + north))
    width_km = max(abs(east - west) * 111.320 * math.cos(mid_lat_rad), 1e-6)
    height_km = max(abs(north - south) * 110.540, 1e-6)
    cell_m = max(float(resolution_m), _SURGE_TIN_CELL_FLOOR_M)

    want_nx = max(round(width_km * 1000.0 / cell_m), _SURGE_TIN_DIM_MIN)
    want_ny = max(round(height_km * 1000.0 / cell_m), _SURGE_TIN_DIM_MIN)
    nx = min(want_nx, _SURGE_TIN_DIM_MAX_FINE)
    ny = min(want_ny, _SURGE_TIN_DIM_MAX_FINE)

    note = ""
    if nx < want_nx or ny < want_ny:
        eff_cell_m = max(width_km * 1000.0 / nx, height_km * 1000.0 / ny)
        note = (
            f"requested ~{float(resolution_m):.0f} m cell wanted a {want_nx}x{want_ny} "
            f"TIN; clamped to {nx}x{ny} by the {_SURGE_TIN_DIM_MAX_FINE}x"
            f"{_SURGE_TIN_DIM_MAX_FINE}-node screening solver budget (effective mesh "
            f"cell ~{eff_cell_m:.0f} m; the bathymetry fetch is still read at the "
            f"requested resolution)"
        )
    return int(nx), int(ny), note


def _autoscale_surge_domain(
    bbox: tuple[float, float, float, float],
    *,
    target_bathy_px: float = _SURGE_BATHY_TARGET_PX,
    min_res_m: float = _SURGE_RES_MIN_M,
    max_res_m: float = _SURGE_RES_MAX_M,
    km_per_tin_node: float = _SURGE_TIN_KM_PER_NODE,
) -> dict[str, float | int]:
    """Autoscale the surge domain's bathymetry-fetch resolution + TIN node grid
    from the AOI size alone (no DEM read -- safe to call before any fetch).

    Two independent targets, both driven by the AOI's approximate WGS84 extent
    in kilometres (equirectangular approx at the AOI's mid-latitude, consistent
    with the rest of this module):

    - ``resolution_m`` (the bathymetry-fetch grid): sized so the AOI's LONGER
      side is ``target_bathy_px`` pixels (default 750, within the 500-800 px
      granularity-doctrine band) -- enough real seabed/shelf detail for a
      screening surge without compositing an oversized COG. Clamped to
      ``[min_res_m, max_res_m]`` (matches ``fetch_topobathy``'s own
      ``resolution_m`` bounds).
    - ``tin_nx``/``tin_ny`` (the internal graded coastal TIN): one node per
      ``km_per_tin_node`` kilometres on each axis, clamped to
      ``[_SURGE_TIN_DIM_MIN, _SURGE_TIN_DIM_MAX]`` per axis so a tiny AOI still
      gets a usable mesh and a huge one stays a fast barotropic screening solve.

    Returns a dict: ``resolution_m``, ``tin_nx``, ``tin_ny``, ``width_km``,
    ``height_km`` (the last two carried for provenance narration).
    """
    west, south, east, north = bbox
    mid_lat_rad = math.radians(0.5 * (south + north))
    width_km = max(abs(east - west) * 111.320 * math.cos(mid_lat_rad), 1e-6)
    height_km = max(abs(north - south) * 110.540, 1e-6)
    long_dim_km = max(width_km, height_km)

    resolution_m = (long_dim_km * 1000.0) / max(target_bathy_px, 1.0)
    resolution_m = min(max(resolution_m, min_res_m), max_res_m)

    tin_nx = min(max(round(width_km / km_per_tin_node), _SURGE_TIN_DIM_MIN), _SURGE_TIN_DIM_MAX)
    tin_ny = min(max(round(height_km / km_per_tin_node), _SURGE_TIN_DIM_MIN), _SURGE_TIN_DIM_MAX)

    return {
        "resolution_m": resolution_m,
        "tin_nx": int(tin_nx),
        "tin_ny": int(tin_ny),
        "width_km": width_km,
        "height_km": height_km,
    }


class SchismSurgeError(RuntimeError):
    """Raised when the SCHISM surge chain fails fatally before producing a layer."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


_SURGE_NOTE_TMPL: str = (
    "SCREENING STORM SURGE (SCHISM barotropic + parametric Holland-1980 sflux "
    "winds): {storm} best track -> a SYMMETRIC vortex wind/pressure field (peak "
    "10-m wind {vmax:.0f} m/s, min pressure {pmin:.0f} hPa) on an internal graded "
    "coastal TIN, still-water open boundary so the surge is PURELY wind/pressure "
    "driven. NOT GAHM asymmetry, NOT tide co-forcing, NOT Coriolis-resolved, NOT a "
    "calibrated STOFS nowcast -- it answers the surge RESPONSE to this track. "
    "Bathymetry: {bathy}."
)


TEMPLATE_CARD = TemplateCard(
    question=(
        "parametric hurricane STORM SURGE (SCHISM barotropic + Holland-1980 sflux "
        "winds): given a hurricane best track, the peak storm-surge water-level a "
        "US coast sees + a coastal gauge surge hydrograph -- the published "
        "Hurricane Ike (2008) case by default, or a named storm via fetch_storm_tracks"
    ),
    required_inputs=[],  # published Ike best track is the self-contained default
    knobs="storm_name, year, location_query/bbox, sim_days, open_boundary_side, input_mode",
)


def _fmt_mb(mb: float) -> str:
    """Human size: MB below 1 GB, else GB (the gate quotes real numbers)."""
    return f"{mb / 1024.0:.2f} GB" if mb >= 1024.0 else f"{mb:.1f} MB"


def _surge_payload_estimate(
    bbox: tuple[float, float, float, float], resolution_m: float | None
):
    """Measured surge-bathymetry payload estimate for the AOI at ``resolution_m``
    (``None`` = native). Shares fetch_topobathy's sampled-density cache + acquisition
    profile (the surge fetch IS a fetch_topobathy call), so this is never a parallel
    threshold check -- it is the SAME measurement the fetch will emit."""
    from trid3nt_server.agent.tools.fetchers._router.hooks.topobathy import (
        _analytic_payload_mb,
        _sample_topobathy_density,
    )
    from trid3nt_server.agent.tools.payload_sampling import estimate_mb

    return estimate_mb(
        "topobathy", bbox, analytic_mb=_analytic_payload_mb(bbox),
        sample_fn=_sample_topobathy_density, resolution_m=resolution_m,
    )


def estimate_payload_mb(
    bbox: list[float] | tuple[float, float, float, float] | None = None,
    location_query: str | None = None,
    resolution_m: float | None = None,
    **_kw: Any,
) -> float:
    """Pre-dispatch payload estimate for the tool-payload-warning gate. Resolution
    doctrine R-A (2026-08-11): DEFAULT = NATIVE, so ``resolution_m=None`` quotes the
    NATIVE bathymetry fetch (the gate fires + offers coarsening); an explicit value
    quotes that coarsened grid. Reuses fetch_topobathy's MEASURED sampled estimator
    (R-B) -- the surge bathymetry fetch is a fetch_topobathy call, so this is the same
    number it will emit, not a parallel guess. A bare ``location_query`` (not yet
    geocoded) falls back to the Ike showcase AOI so the estimate stays conservative."""
    resolved_bbox = tuple(float(v) for v in bbox) if bbox and len(bbox) == 4 else _IKE_BBOX
    return _surge_payload_estimate(
        resolved_bbox, float(resolution_m) if resolution_m else None
    ).mb


def estimate_payload_mb_detail(
    bbox: list[float] | tuple[float, float, float, float] | None = None,
    location_query: str | None = None,
    resolution_m: float | None = None,
    **_kw: Any,
) -> str | None:
    """Gate-text detail: the MEASURED estimate + estimator KIND + the autoscale
    COARSENING suggestion (resolution doctrine R-B). For the native default the card
    reads e.g. "native ~2.4 GB measured; suggested coarsening 199 m ~0.4 MB; proceed
    native / coarsen (resolution_m=199) / cancel"; an explicit resolution names that
    grid alone. The gate resolves this as ``<estimator>_detail`` and appends it to the
    payload-warning recommendation (no new envelope field)."""
    resolved_bbox = tuple(float(v) for v in bbox) if bbox and len(bbox) == 4 else _IKE_BBOX
    if resolution_m:
        est = _surge_payload_estimate(resolved_bbox, float(resolution_m))
        return (f"surge bathymetry {float(resolution_m):.0f} m grid "
                f"~{_fmt_mb(est.mb)} ({est.kind})")
    native = _surge_payload_estimate(resolved_bbox, None)
    coarse_res = float(_autoscale_surge_domain(resolved_bbox)["resolution_m"])
    coarse = _surge_payload_estimate(resolved_bbox, coarse_res)
    return (
        f"native bathymetry ~{_fmt_mb(native.mb)} ({native.kind}); suggested "
        f"coarsening {coarse_res:.0f} m ~{_fmt_mb(coarse.mb)}; proceed native / "
        f"coarsen (resolution_m={coarse_res:.0f}) / cancel"
    )


_SURGE_METADATA = AtomicToolMetadata(
    name="schism_pahm_surge",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="schism",
    tier="template",
    payload_mb_estimator_name="estimate_payload_mb",
)


@register_tool(
    _SURGE_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def schism_pahm_surge(
    storm_name: str | None = None,
    year: int | None = None,
    location_query: str | None = None,
    bbox: list[float] | tuple[float, float, float, float] | None = None,
    sim_days: float = 1.5,
    open_boundary_side: str = "south",
    input_mode: str | None = None,
    allow_synthetic_domain: bool = False,
    resolution_m: float | None = None,
    **_extra_ignored: Any,
) -> SchismElevationLayerURI | dict[str, Any]:
    """PARAMETRIC HURRICANE STORM SURGE on a coastal mesh (SCHISM + Holland-1980 winds).

    Fidelity: SCHISM (the semi-implicit cross-scale unstructured-grid model behind
    NOAA STOFS) barotropic surge, forced by a STANDALONE parametric Holland-1980
    wind/pressure field authored as sflux inputs (nws=2). Returns the PEAK
    storm-surge water-level surface + a coastal gauge surge hydrograph.

    THE tool for "storm surge from a hurricane", "hurricane surge simulation",
    "SCHISM storm surge", "parametric hurricane wind surge", "best-track surge
    response", "how much surge from hurricane <X>". Given a best track (central
    pressure + max wind + radius of maximum wind along the track), it drives a
    symmetric Holland vortex over an internal graded coastal TIN with a still-water
    boundary so the surge is PURELY the storm's wind/pressure response.

    Do NOT use this for:
        - TIDAL circulation (no storm) -- use ``schism_tidal_hydro``.
        - Wind WAVES / nearshore Hs -- use ``schism_coupled_waves``.
        - FAST arbitrary-AOI flood screening -- use ``sfincs_flood``.
        - Riverine (``hecras_riverine_flood``) or urban drainage (``swmm_urban_flood``).

    Params:
        storm_name: a historical Atlantic hurricane name (e.g. ``"IKE"``). When set
            (with ``year``) the best track is fetched via ``fetch_storm_tracks``
            (IBTrACS); on an unavailable fetch it falls back to the published Ike
            track with a LOUD note. Default ``None`` -> the published Hurricane Ike
            (2008) best track (published-first).
        year: the storm season for ``fetch_storm_tracks`` (required with
            ``storm_name``).
        location_query / bbox: the coastal AOI (a place name geocoded to a bbox, or
            an explicit EPSG:4326 ``[min_lon,min_lat,max_lon,max_lat]``). Default:
            the storm's landfall region (Ike -> the Galveston / NW Gulf AOI).
        sim_days: run length in days (default 1.5; covers pre-landfall to landfall).
        open_boundary_side: the seaward (open) mesh side (``south|north|east|west``;
            default ``south`` for a Gulf coast).
        input_mode: ``"user_gated"`` reviews the resolved storm + forcing + mesh
            basis (and previews the mesh) before solving.
        allow_synthetic_domain: MECHANISM-DEMO MODE ONLY, default ``False``. When
            real topo-bathymetry cannot be fetched for the AOI, the default
            behaviour is an honest ``SCHISM_BATHYMETRY_UNAVAILABLE`` typed error --
            fabricated bathymetry is never a silent fallback (NATE ruling,
            2026-08-11). Setting this ``True`` opts into an IDEALIZED sloping-shelf
            substitute so the Holland-vortex/sflux/solve PATHWAY can still be
            exercised without real geography; the resulting envelope is marked
            ``synthetic_inputs`` and the surge PATTERN is explicitly non-physical.
        resolution_m: the bathymetry-fetch grid resolution (metres), a USER LEVER
            (the granularity-gate doctrine -- resolution is the user's right, never
            a silent cap). Default ``None`` -> AUTOSCALED from the resolved AOI
            (``_autoscale_surge_domain``: the AOI's longer side sized to ~750 px,
            clamped to fetch_topobathy's own [25, 1000] m bounds); an explicit
            value always wins and is honored even when it implies a large fetch --
            oversized requests trip the payload-warning gate (this tool declares
            ``estimate_payload_mb``), never a silent resolution ceiling. Whether
            the resolution used was auto or user-supplied is recorded in
            ``synthetic_inputs`` (``resolution_m`` entry, ``basis`` ``derived`` vs
            ``user``).

    Returns:
        On success: ``SchismElevationLayerURI`` -- the peak-surge COG beside the
        mesh preview + the track overlay + the gauge hydrograph. ``elev_max_m`` is
        the PEAK surge (metres); narrate the typed fields only (invariant 1).
        On failure: dict ``status="error"`` + ``error_code`` + ``error_message``
        (``SCHISM_BATHYMETRY_UNAVAILABLE`` when real bathymetry could not be
        fetched and ``allow_synthetic_domain`` was not set).

    FR-DC-6: ``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="workflow_dispatch"``.
    """
    if open_boundary_side not in ("south", "north", "east", "west"):
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": "open_boundary_side must be south|north|east|west"}
    try:
        sim_days = float(sim_days)
    except (TypeError, ValueError):
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": "sim_days must be a number"}
    if not (0.5 <= sim_days <= 5.0):
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": "sim_days in [0.5, 5.0]"}
    bbox_t = tuple(float(v) for v in bbox) if bbox and len(bbox) == 4 else None
    logger.info("schism_pahm_surge storm=%s year=%s location=%s sim_days=%.3g mode=%s",
                storm_name, year, location_query, sim_days, input_mode)
    try:
        result = await model_schism_pahm_surge(
            storm_name=storm_name, year=year, location_query=location_query,
            bbox=bbox_t, sim_days=sim_days, open_boundary_side=open_boundary_side,
            input_mode=input_mode, allow_synthetic_domain=bool(allow_synthetic_domain),
            resolution_m=float(resolution_m) if resolution_m is not None else None,
        )
        if isinstance(result, dict):
            return result
        logger.info("schism_pahm_surge complete layer_id=%s peak_surge=%.3g uri=%s",
                    result.layer_id, result.elev_max_m, result.uri)
        return result
    except asyncio.CancelledError:
        raise
    except SchismSurgeError as exc:
        logger.warning("schism_pahm_surge failed: %s (%s)", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("schism_pahm_surge unexpected failure")
        return {"status": "error", "error_code": "SCHISM_INTERNAL_ERROR", "error_message": str(exc)}


# --------------------------------------------------------------------------- #
# Composer.
# --------------------------------------------------------------------------- #
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)
from trid3nt_server.emission.layer_uri_emit import publish_input_layer
from trid3nt_server.agent.workflows.schism import deck_authoring
from trid3nt_server.agent.workflows.schism import holland_sflux as _H
from trid3nt_server.agent.workflows.schism import postprocess_schism as pp
from trid3nt_server.agent.workflows.schism.run_schism import SCHISM_SURGE_SOLVER_NAME
from trid3nt_server.agent.workflows.schism.tidal_hydro.tidal_hydro import (
    _download_run_output,
    _fetch_bathymetry_cog,
    _maybe_emit_station_chart,
    _publish_elev_layer,
    _runs_uri,
)


def _cache_bucket() -> str:
    b = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not b:
        raise SchismSurgeError(SCHISM_SOLVE_FAILED,
                               "TRID3NT_CACHE_BUCKET must be set to stage the SCHISM manifest.")
    return b


def _stage_surge_manifest(deck_files: list[Path], case_dir: Path, run_tag: str) -> str:
    """Upload the deck as manifest inputs[]; preserve the sflux/ subdir in ``dest``.

    Unlike the tidal stager (basename dest), a surge deck carries nested
    ``sflux/*`` files -- the ``dest`` MUST be the path RELATIVE to the case dir so
    the launcher stages ``sflux/sflux_air_1.0001.nc`` under the rundir (the launcher
    creates dest parents + guards against rundir escape)."""
    import json

    from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client

    cache_bucket = _cache_bucket()
    s3 = _get_s3_client()
    inputs = []
    for f in deck_files:
        rel = f.relative_to(case_dir).as_posix()
        key = f"schism/{run_tag}/{rel}"
        with open(f, "rb") as fh:
            s3.put_object(Bucket=cache_bucket, Key=key, Body=fh.read())
        inputs.append({"gs_uri": f"s3://{cache_bucket}/{key}", "dest": rel})
    manifest = {
        "variant": "hydro", "ncompute": 3, "nscribe": 2, "run_id": run_tag,
        "inputs": inputs, "schism_args": [],
        "outputs": ["outputs/*.nc", "outputs/staout_*", "schism_metrics.json"],
    }
    key = f"schism/{run_tag}/manifest.json"
    s3.put_object(Bucket=cache_bucket, Key=key,
                  Body=json.dumps(manifest, indent=2).encode("utf-8"),
                  ContentType="application/json")
    return f"s3://{cache_bucket}/{key}"


def _track_to_fixes(
    raw: list[dict[str, Any]], base_date: tuple[int, int, int, int]
) -> list[_H.TrackFix]:
    """Convert raw best-track fixes (IBTrACS units) to ``TrackFix`` (SI, hours-since-base).

    ``raw`` rows carry ``iso_time`` (ISO8601), ``lon``/``lat`` (deg), ``pres_mb``,
    ``wind_kt``, and optionally ``rmw_nmi`` / ``poci_mb``. Fixes with a missing
    center or pressure are dropped; RMW/Pn fall back to Holland defaults.
    """
    import datetime as _dt

    base = _dt.datetime(base_date[0], base_date[1], base_date[2], base_date[3])
    fixes: list[_H.TrackFix] = []
    for r in raw:
        lon, lat = r.get("lon"), r.get("lat")
        pres = r.get("pres_mb")
        iso = r.get("iso_time")
        if lon is None or lat is None or pres is None or not iso:
            continue
        try:
            t = _dt.datetime.fromisoformat(str(iso).replace("Z", ""))
        except ValueError:
            continue
        th = (t - base).total_seconds() / 3600.0
        wind = r.get("wind_kt") or 35.0
        rmw = (r.get("rmw_nmi") or 40.0) * _NMI_TO_M
        pn = (r.get("poci_mb") or _PN_MB) * _MB_TO_PA
        fixes.append(_H.TrackFix(
            time_hr=float(th), lon=float(lon), lat=float(lat),
            pc_pa=float(pres) * _MB_TO_PA, vmax_ms=float(wind) * _KT_TO_MS,
            rmw_m=float(rmw), pn_pa=pn,
        ))
    return sorted(fixes, key=lambda f: f.time_hr)


def _published_ike_fixes() -> tuple[list[_H.TrackFix], tuple[int, int, int, int], str]:
    """The published Hurricane Ike best track as ``TrackFix`` (published-first default)."""
    fixes = [
        _H.TrackFix(time_hr=t, lon=lon, lat=lat, pc_pa=pmb * _MB_TO_PA,
                    vmax_ms=wkt * _KT_TO_MS, rmw_m=rnmi * _NMI_TO_M, pn_pa=_PN_MB * _MB_TO_PA)
        for (t, lon, lat, pmb, wkt, rnmi) in PUBLISHED_IKE_TRACK
    ]
    return fixes, _IKE_BASE_DATE, "Hurricane Ike (2008, bal092008)"


async def _resolve_track(
    storm_name: str | None, year: int | None, bbox: tuple | None
) -> tuple[list[_H.TrackFix], tuple[int, int, int, int], str, str]:
    """Resolve the best track -> (fixes, base_date, storm_label, provenance_basis).

    Named storm + year -> fetch_storm_tracks (IBTrACS points); the raw fixes are
    parsed off the vector features. Anything unavailable falls back to the published
    Ike track (LOUD basis). No storm named -> the published Ike track (published-first).
    """
    if not storm_name:
        fixes, base, label = _published_ike_fixes()
        return fixes, base, label, "published_default"
    # Named-storm fetch (best-effort; a slow/absent IBTrACS falls back honestly).
    try:
        from trid3nt_server.agent.tools.fetchers._router.hooks import storm_tracks as _st

        fb = bbox or _IKE_BBOX
        raw_storms = await asyncio.wait_for(
            asyncio.to_thread(_fetch_ibtracs_fixes, _st, fb, year, storm_name),
            timeout=90.0,
        )
        if raw_storms:
            # earliest fix -> base_date at the top of that hour.
            import datetime as _dt

            first = min((f for f in raw_storms if f.get("iso_time")),
                        key=lambda f: f["iso_time"])
            t0 = _dt.datetime.fromisoformat(str(first["iso_time"]).replace("Z", ""))
            base = (t0.year, t0.month, t0.day, t0.hour)
            fixes = _track_to_fixes(raw_storms, base)
            if len(fixes) >= 2:
                return fixes, base, f"{storm_name.title()} ({year})", "fetch_storm_tracks"
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_storm_tracks(%s,%s) unavailable (%s); using published Ike",
                       storm_name, year, exc)
    fixes, base, label = _published_ike_fixes()
    return fixes, base, label, "published_fallback"


def _fetch_ibtracs_fixes(st_mod, bbox, year, storm_name) -> list[dict[str, Any]]:
    """Pull the raw IBTrACS fixes for one named storm via the storm_tracks hook helpers."""
    y = int(year) if year else None
    files = st_mod._resolve_basin_files(tuple(bbox))  # type: ignore[attr-defined]
    storms: dict[str, list[dict[str, Any]]] = {}
    for fname in files:
        raw = st_mod._fetch_ibtracs_csv(fname)  # type: ignore[attr-defined]
        parsed = st_mod._parse_ibtracs(raw)  # type: ignore[attr-defined]
        for sid, fixes in parsed.items():
            storms.setdefault(sid, []).extend(fixes)
    want = storm_name.strip().upper()
    for sid, fixes in storms.items():
        if any((f.get("name") or "").upper() == want for f in fixes):
            if y and not any(str(f.get("season")) == str(y) for f in fixes):
                continue
            return sorted(fixes, key=lambda f: f.get("iso_time") or "")
    return []


def _build_internal_tin(bbox: tuple[float, float, float, float], nx: int = 22, ny: int = 20):
    """A graded coastal TIN over the AOI: a jittered lon/lat point grid, Delaunay."""
    import numpy as np
    from scipy.spatial import Delaunay

    w, s, e, n = bbox
    gx, gy = np.meshgrid(np.linspace(w, e, nx), np.linspace(s, n, ny))
    pts = np.column_stack([gx.ravel(), gy.ravel()]).astype(float)
    rng = np.random.default_rng(7)
    jitter = (np.array([e - w, n - s]) / np.array([nx, ny])) * 0.18
    interior = ~(
        (np.isclose(pts[:, 0], w)) | (np.isclose(pts[:, 0], e))
        | (np.isclose(pts[:, 1], s)) | (np.isclose(pts[:, 1], n))
    )
    pts[interior] += rng.uniform(-1, 1, size=(interior.sum(), 2)) * jitter
    cells = Delaunay(pts).simplices.astype(np.int64)
    return pts, cells


async def model_schism_pahm_surge(
    *,
    storm_name: str | None,
    year: int | None,
    location_query: str | None,
    bbox: tuple[float, float, float, float] | None,
    sim_days: float,
    open_boundary_side: str,
    input_mode: str | None,
    allow_synthetic_domain: bool = False,
    resolution_m: float | None = None,
) -> SchismElevationLayerURI | dict[str, Any]:
    """Resolve track + AOI + mesh + bathymetry -> author surge deck -> solve -> publish."""
    import numpy as np

    emitter = current_emitter()
    begin_substeps(emitter, 3)  # run_solver + postprocess + publish

    # --- Stage 1: best track ------------------------------------------------- #
    fixes, base_date, storm_label, track_basis = await _resolve_track(storm_name, year, bbox)
    if len(fixes) < 2:
        raise SchismSurgeError(SCHISM_INPUT_INVALID,
                               f"best track for {storm_label} has <2 usable fixes")

    # --- Stage 2: AOI bbox --------------------------------------------------- #
    if bbox is None and location_query:
        from trid3nt_server.agent.tools.fetchers.socioeconomic.geocode_location.geocode_location import (
            geocode_location,
        )
        geo = geocode_location(location_query)
        bb = geo.get("bbox")
        if bb and len(bb) == 4:
            bbox = tuple(float(v) for v in bb)
    if bbox is None:
        bbox = _IKE_BBOX if not storm_name else _track_landfall_bbox(fixes)

    # --- Stage 2b: resolution (the user lever). Resolution doctrine (2026-08-11):
    # DEFAULT = NATIVE bathymetry; coarsening is an EXPLICIT user declaration. When
    # resolution_m is None the bathymetry fetch runs NATIVE (fine CUDEM 1/9"
    # composite, 12000 px guard); the autoscaled cell is only the COARSENING HINT the
    # payload gate quotes, never a silent coarsen. An explicit value is honored as-is.
    autoscale = _autoscale_surge_domain(bbox)
    if resolution_m is not None:
        resolved_res_m: float | None = float(resolution_m)
        fetch_res_m: float | None = float(resolution_m)
        res_basis: str = "user"
        # Fidelity-first: an explicit resolution drives the MESH density too, not
        # only the bathymetry fetch -- the requested cell keys the TIN, bounded by
        # the declared node budget (which labels itself in the envelope if it binds).
        tin_nx, tin_ny, tin_budget_note = _resolution_driven_tin_dims(bbox, resolved_res_m)
    else:
        resolved_res_m = None  # native
        fetch_res_m = None
        res_basis = "derived"  # the tool DERIVED native as the default (SyntheticInput basis)
        # Native path: mesh density keys to the AOI-driven autoscale suggestion.
        tin_nx, tin_ny = int(autoscale["tin_nx"]), int(autoscale["tin_ny"])
        tin_budget_note = ""

    # --- Stage 3: mesh (case mesh via the gate, else internal TIN) ----------- #
    supplied_mesh, gate_open_side, mesh_gate_note = await _surge_mesh_gate(input_mode)
    workdir = Path(tempfile.mkdtemp(prefix="schism-surge-"))
    case_dir = workdir / "case"
    synthetic_bathy = False

    if supplied_mesh is not None:
        open_side = gate_open_side or open_boundary_side
        bathy_source = "user-supplied mesh (node bathymetry)"
        deck = await asyncio.to_thread(
            deck_authoring.author_pahm_surge_deck, case_dir,
            track=fixes, mesh_bbox=bbox, base_date=base_date, supplied_mesh=supplied_mesh,
            sim_days=sim_days, open_boundary_side=open_side,
        )
    else:
        open_side = open_boundary_side
        points, cells = _build_internal_tin(bbox, nx=tin_nx, ny=tin_ny)
        # Bathymetry: fetch a topobathy/DEM COG; else a synthetic sloping shelf.
        bathy_override = os.environ.get("TRID3NT_SCHISM_BATHY_PATH")
        depths = None
        if bathy_override and Path(bathy_override).exists():
            depths = deck_authoring.sample_bathymetry_on_nodes(points, bathy_override)
            bathy_source = "local topobathy COG"
        else:
            try:
                # REAL bathymetry: the fine CUDEM 1/9" nearshore composite (native
                # by default) + ETOPO shelf base beyond it, land leg dropped so the
                # 0 m ocean fill never clobbers the offshore bathy. An explicit
                # resolution_m coarsens the fetch grid (and skips CUDEM only when the
                # cell is coarser than ETOPO's own -- see _CUDEM_SKIP_RES_M).
                dem_path, bathy_source = await _fetch_bathymetry_cog(
                    bbox, resolution_m=fetch_res_m, force_bathy_base=True,
                    skip_land=True)
                depths = deck_authoring.sample_bathymetry_on_nodes(points, dem_path)
                res_label = ("native CUDEM 1/9\"" if fetch_res_m is None
                             else f"~{fetch_res_m:.0f} m [{res_basis}]")
                bathy_source = (
                    f"{bathy_source} COG ({res_label}, ETOPO shelf base)"
                )
            except Exception as exc:  # noqa: BLE001
                if not allow_synthetic_domain:
                    raise SchismSurgeError(
                        SCHISM_BATHYMETRY_UNAVAILABLE,
                        f"no real topo-bathymetry could be fetched for this AOI "
                        f"(fetch_topobathy + fetch_dem both failed/unavailable: {exc}); "
                        "the surge run stopped rather than substitute fabricated "
                        "bathymetry. Pass allow_synthetic_domain=True to run the "
                        "declared mechanism-demo mode on an idealized shelf instead.",
                    ) from exc
                logger.warning(
                    "surge bathymetry fetch failed (%s); allow_synthetic_domain=True "
                    "-> idealized sloping shelf (declared mechanism-demo mode)", exc)
                depths = _synthetic_shelf_depths(points, bbox, open_side)
                bathy_source = "SYNTHETIC sloping shelf (no bathymetry fetched; mechanism-demo mode)"
                synthetic_bathy = True
        deck = await asyncio.to_thread(
            deck_authoring.author_pahm_surge_deck, case_dir,
            track=fixes, mesh_bbox=bbox, base_date=base_date, points=points,
            cells=cells, depths=depths, sim_days=sim_days, open_boundary_side=open_side,
        )

    field = deck["holland_field"]
    note = _SURGE_NOTE_TMPL.format(
        storm=storm_label, vmax=field.peak_wind_ms, pmin=field.min_pressure_pa / 100.0,
        bathy=bathy_source,
    )
    if synthetic_bathy:
        note = (
            "WARNING -- SYNTHETIC BATHYMETRY: no real topo-bathy could be fetched for "
            "this AOI, so the surge ran on an IDEALIZED sloping shelf. The peak piles "
            "against the domain's open edge, NOT real coastal geography -- treat the "
            "surge PATTERN as non-physical (magnitude only, screening). "
        ) + note
    if track_basis == "published_fallback":
        note += " [fetch_storm_tracks unavailable -- published Ike track substituted]"
    if mesh_gate_note:
        note += f" [case mesh: {mesh_gate_note}]"

    # --- Stage 4: input-review gate ------------------------------------------ #
    review_entries = [
        SyntheticInput(param="storm", value=storm_label,
                       basis="user" if track_basis == "fetch_storm_tracks" else "default_demo",
                       real_source_if_any="IBTrACS best track" if track_basis == "fetch_storm_tracks" else "published HURDAT2 Ike track",
                       note=f"{len(fixes)} best-track fixes; peak 10-m wind {field.peak_wind_ms:.0f} m/s, min pressure {field.min_pressure_pa/100.0:.0f} hPa (Holland B~{field.holland_b_mean})"),
        SyntheticInput(param="forcing", value="parametric Holland-1980 sflux (nws=2)",
                       basis="derived", note="symmetric vortex wind/pressure on a "
                       f"{field.n_lon}x{field.n_lat} lon/lat grid, {field.n_times} time steps"),
        SyntheticInput(param="mesh", value=("user-supplied case mesh" if supplied_mesh is not None
                                            else "internal graded coastal TIN"),
                       basis="user" if supplied_mesh is not None else "derived",
                       note=f"{deck['n_nodes']} nodes / {deck['n_elements']} elements, open ({open_side}) boundary"),
        SyntheticInput(param="bathymetry", value=bathy_source,
                       basis="fetched" if "COG" in bathy_source else "default_demo",
                       real_source_if_any=bathy_source if "COG" in bathy_source else None),
        SyntheticInput(
            param="domain_provenance",
            value="SYNTHETIC (idealized sloping shelf)" if synthetic_bathy else "REAL",
            basis="default_demo" if synthetic_bathy else "fetched",
            real_source_if_any=None if synthetic_bathy else bathy_source,
            note=("mechanism-demo mode (allow_synthetic_domain=True): the surge PATTERN "
                  "is non-physical, magnitude-only" if synthetic_bathy else
                  "bathymetry traced to a real fetched source; the surge geometry reflects "
                  "actual coastal bathymetry, not an idealized shelf"),
        ),
        SyntheticInput(param="sim_days", value=round(sim_days, 3), units="d", basis="default_demo"),
        SyntheticInput(
            param="resolution_m",
            value=(round(resolved_res_m, 1) if resolved_res_m is not None
                   else "native (CUDEM 1/9\")"),
            units="m", basis=res_basis,
            note=(
                (f"user-supplied (overrides the {autoscale['resolution_m']:.0f} m autoscale "
                 f"coarsening suggestion); drives the mesh too -- TIN {tin_nx}x{tin_ny} nodes"
                 + (f". {tin_budget_note}" if tin_budget_note else "")) if res_basis == "user" else
                "NATIVE bathymetry (default): the fine CUDEM 1/9\" nearshore composite, "
                f"bounded by the 12000 px guard; the ~{autoscale['resolution_m']:.0f} m "
                "autoscale cell is offered as the coarsening hint on the payload gate. "
                f"TIN {tin_nx}x{tin_ny} nodes"
            ),
        ),
    ]
    review = await gate_input_review(
        tool_name="schism_pahm_surge", mode=input_mode, entries=review_entries,
        params={"storm": storm_label, "sim_days": sim_days, "open_boundary_side": open_side},
    )
    if not review.proceed:
        return {"status": "error", "error_code": "SCHISM_INPUT_REVIEW_CANCELLED",
                "error_message": review.cancel_reason or "input review not approved; the solver did not run"}

    # --- Stage 5: stage manifest + dispatch ---------------------------------- #
    run_tag = new_ulid()
    manifest_uri = await asyncio.to_thread(_stage_surge_manifest, deck["files"], case_dir, run_tag)
    logger.info("model_schism_pahm_surge staged manifest run_tag=%s files=%d storm=%s",
                run_tag, len(deck["files"]), storm_label)

    from trid3nt_server.agent.tools.simulation.solver.solver import (
        run_solver, wait_for_completion,
    )
    handle = run_solver(solver=SCHISM_SURGE_SOLVER_NAME, model_setup_uri=manifest_uri,
                        compute_class="medium")
    sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=SCHISM_SURGE_SOLVER_NAME, handle=handle, compute_class="medium",
    )
    run_result = None
    try:
        async with substep(emitter, "run_solver"):
            run_result = await wait_for_completion(handle)
    except asyncio.CancelledError:
        await route_sim_terminal(emitter, sim_step_id, run_result=None)
        raise
    await route_sim_terminal(emitter, sim_step_id, run_result=run_result)

    if run_result is None or run_result.status != "complete":
        raise SchismSurgeError(
            SCHISM_SOLVE_FAILED,
            "SCHISM surge solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or getattr(run_result, 'cancellation_reason', '') or ''}",
        )
    batch_run_id = getattr(run_result, "run_id", None) or run_tag

    # --- Stage 6: postprocess (peak-surge COG = elev_max) -------------------- #
    out2d_local = await asyncio.to_thread(_download_run_output, batch_run_id, "outputs/out2d_1.nc")
    if out2d_local is None:
        raise SchismSurgeError(SCHISM_SOLVE_FAILED,
                               "SCHISM surge completed but outputs/out2d_1.nc was not downloadable")
    out2d_uri = _runs_uri(batch_run_id, "outputs/out2d_1.nc")
    # The surge deck solves in a local-metres projection (ics=1); the inverse
    # equirectangular maps out2d node metres back to lon/lat so the COG + mesh
    # georeference to the real AOI (the projection centre rode out of the deck).
    lon_c, lat_c, coslat = deck["proj_center"]

    def _inv_project(nx, ny):
        return (lon_c + nx / (_M_PER_DEG_LON * coslat), lat_c + ny / _M_PER_DEG_LAT)

    try:
        async with substep(emitter, "postprocess_schism"):
            layers, metrics = await asyncio.to_thread(
                pp.postprocess_schism, out2d_local, out2d_uri, run_id=batch_run_id,
                mesh_source="coastal_tin", sim_days=sim_days, constituents=[],
                n_nodes_grid=deck["n_nodes"], n_elements_grid=deck["n_elements"],
                fallback_note=note, reproject_xy=_inv_project,
            )
    except pp.PostprocessSchismError as exc:
        raise SchismSurgeError(exc.error_code, str(exc)) from exc
    finally:
        try:
            Path(out2d_local).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    elev = layers[0]
    assert isinstance(elev, SchismElevationLayerURI)
    mesh_layer = layers[1] if len(layers) > 1 else None
    elev = elev.model_copy(update={"mesh_source": "pahm_surge"})

    # --- Stage 7: station surge hydrograph ----------------------------------- #
    staout_local = await asyncio.to_thread(_download_run_output, batch_run_id, "outputs/staout_1")
    station_series = pp.read_station_series(staout_local) if staout_local else []
    if staout_local:
        try:
            Path(staout_local).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    # --- Stage 8: publish the peak-surge COG --------------------------------- #
    async with substep(emitter, "publish_layer"):
        elev = await asyncio.to_thread(_publish_elev_layer, elev, review.entries)

    if mesh_layer is not None:
        try:
            await publish_input_layer(emitter, mesh_layer, role="context")
        except Exception as exc:  # noqa: BLE001
            logger.warning("schism surge mesh preview emit skipped: %s", exc)

    # --- Best-effort: the best-track overlay --------------------------------- #
    if emitter is not None:
        try:
            await _emit_track_overlay(emitter, fixes, storm_label)
        except Exception as exc:  # noqa: BLE001
            logger.warning("schism surge track overlay skipped: %s", exc)

    # --- Best-effort: the coastal gauge surge hydrograph --------------------- #
    if emitter is not None and station_series:
        try:
            await _maybe_emit_station_chart(emitter, station_series, "pahm_surge", False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("schism surge hydrograph skipped: %s", exc)

    if emitter is not None and elev.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(elev.bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("schism surge zoom-to failed: %s", exc)

    return elev


def _track_landfall_bbox(fixes: list[_H.TrackFix]) -> tuple[float, float, float, float]:
    """A ~1.6x1.4 deg AOI around the track's minimum-pressure (peak) fix."""
    peak = min(fixes, key=lambda f: f.pc_pa)
    return (peak.lon - 0.8, peak.lat - 0.7, peak.lon + 0.8, peak.lat + 0.7)


def _synthetic_shelf_depths(points, bbox, open_side: str):
    """A sloping continental shelf: deeper toward the open (seaward) side (2..40 m)."""
    import numpy as np

    w, s, e, n = bbox
    x, y = points[:, 0], points[:, 1]
    if open_side == "south":
        frac = (n - y) / max(1e-9, n - s)
    elif open_side == "north":
        frac = (y - s) / max(1e-9, n - s)
    elif open_side == "west":
        frac = (e - x) / max(1e-9, e - w)
    else:
        frac = (x - w) / max(1e-9, e - w)
    return (2.0 + 38.0 * np.clip(frac, 0.0, 1.0)).astype(float)


async def _surge_mesh_gate(input_mode):
    """Offer a case mesh to the surge solve (ADR 0212); None -> the internal TIN."""
    try:
        from trid3nt_server.agent.workflows.schism.tidal_hydro.tidal_hydro import (
            _schism_mesh_precondition_gate,
        )
        return await _schism_mesh_precondition_gate(input_mode)
    except Exception as exc:  # noqa: BLE001
        logger.warning("surge mesh gate failed (%s); internal TIN", exc)
        return None, None, None


async def _emit_track_overlay(emitter, fixes: list[_H.TrackFix], storm_label: str) -> None:
    """Emit the best track as a labeled LineString+point vector context layer.

    Uploads the track FeatureCollection to the runs bucket and emits a
    ``layer_type="vector"``, ``role="context"`` LayerURI (the labeled track must not
    fight the surge-result camera) -- mirrors the geoclaw track-overlay seam.
    """
    import json as _json

    from trid3nt_contracts.execution import LayerURI
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        _get_runs_bucket, _get_s3_client,
    )

    coords = [[round(f.lon, 4), round(f.lat, 4)] for f in fixes]
    features: list[dict[str, Any]] = [{
        "type": "Feature",
        "properties": {"name": f"{storm_label} best track", "kind": "storm_track"},
        "geometry": {"type": "LineString", "coordinates": coords},
    }]
    for f in fixes:
        features.append({
            "type": "Feature",
            "properties": {"pres_mb": round(f.pc_pa / 100.0, 1),
                           "wind_ms": round(f.vmax_ms, 1), "kind": "fix"},
            "geometry": {"type": "Point", "coordinates": [round(f.lon, 4), round(f.lat, 4)]},
        })
    fc = {"type": "FeatureCollection", "features": features}
    run_id = new_ulid()

    def _upload() -> str:
        bucket = _get_runs_bucket()
        key = f"{run_id}/storm_track.geojson"
        _get_s3_client().put_object(
            Bucket=bucket, Key=key,
            Body=_json.dumps(fc, separators=(",", ":")).encode("utf-8"),
            ContentType="application/geo+json",
        )
        return f"s3://{bucket}/{key}"

    s3_uri = await asyncio.to_thread(_upload)
    layer = LayerURI(
        layer_id=f"surge-track-{run_id}",
        name=f"{storm_label} best track",
        layer_type="vector",
        uri=s3_uri,
        style_preset="storm_track",
        role="context",
        bbox=None,
        crs_authid="EPSG:4326",
    )
    await publish_input_layer(emitter, layer, role="context")
