"""TELEMAC-2D river-dye release composer (river-dye North Star, PHASE 4).

The TELEMAC analogue of ``model_wave_scenario`` (SWAN) /
``model_dambreak_geoclaw_scenario`` (GeoClaw): a deterministic orchestrator-style
workflow (Invariant 2 - no LLM in the chain) that turns a PLACE (or an AOI bbox)
into a rendered, ANIMATED river-dye plume:

    geocode the place -> centroid + AOI bbox (F46: the model NEVER hand-types
        coords -- a natural prompt geocodes)
      -> fetch_river_geometry(bbox) to confirm a real reach + pick a mid-reach
        SEED point on the largest flowline (the worker NLDI-snaps it to the
        COMID and navigates downstream)
      -> stage the ``telemac_river_dye`` worker manifest (ReachConfig overrides)
        to the cache bucket
      -> run_solver('telemac_river_dye', ...) -> wait_for_completion (the SAME
        generic solve seam SFINCS/SWAN/GeoClaw use; local-docker here)
      -> download the result SELAFIN (r2d_river.slf) + telemac_metrics.json
      -> postprocess_telemac (rasterize the PEAK dye concentration -> ONE COG +
        the SELAFIN mesh sibling the plugin animates)
      -> publish the peak COG through publish_layer (render chokepoint)
      -> return the TelemacDyeLayerURI (a LayerURI subtype so the emit_tool_call
        add_loaded_layer gate fires + export_case_to_qgis discovers the mesh).

The DELIBERATE difference from the flood engines: the primary deliverable is the
engine's NATIVE time-stepped SELAFIN mesh (MDAL opens .slf directly and animates
its DYE dataset group with zero new render infra). So this composer emits ONE
peak-concentration COG as the map anchor + narration carrier and lets the mesh
sibling (discovered by ``export_case_to_qgis`` next to the COG in the runs
bucket) carry the animation -- NO per-frame COGs.

Determinism boundary (Invariant 1): every dye number the agent narrates comes
from the typed ``TelemacDyeLayerURI`` fields the postprocess computed with plain
arithmetic over the SELAFIN tracer field - never free-generated. Honesty floor
(FR-AS-7): the layer's ``fallback_note`` labels the run an idealized-bed demo so
a release is never read as a calibrated site study.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from grace2_contracts import new_ulid
from grace2_contracts.telemac_contracts import (
    TELEMAC_DYE_STYLE_PRESET,
    TelemacDyeLayerURI,
)

from ..pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)
from ..tools import TOOL_REGISTRY
from ..tools.publish_layer import PublishLayerError, publish_layer
from .postprocess_telemac import PostprocessTelemacError, postprocess_telemac
from .run_telemac import TELEMAC_SOLVER_NAME
from .solve_progress import drive_live_solve_progress

logger = logging.getLogger("grace2_agent.workflows.model_river_dye_release_scenario")

__all__ = [
    "model_river_dye_release_scenario",
    "TelemacDyeScenarioError",
    "TelemacDyeScenarioInputError",
    "DEFAULT_RIVER_AOI_HALF_DEG",
]

#: Half-width (deg) of the bbox fetched around the geocoded centroid to locate a
#: river reach + pick the seed. ~0.06 deg (~6 km) reliably catches the main stem
#: even when the geocoded city centroid sits a few km off the channel.
DEFAULT_RIVER_AOI_HALF_DEG: float = 0.06

#: Demo defaults so a bare "dye spill in the river near X" runs end-to-end. These
#: mirror the worker ReachConfig demo defaults (Snake River near Twin Falls
#: tuning); the composer only overrides intent-bearing fields.
DEFAULT_REACH_LENGTH_KM: float = 6.0
DEFAULT_CHANNEL_WIDTH_M: float = 60.0
DEFAULT_MESH_SIZE_M: float = 14.0
DEFAULT_SPILL_FRACTION: float = 0.25
DEFAULT_PULSE_WINDOW_S: float = 300.0
DEFAULT_SOURCE_Q_M3S: float = 8.0
DEFAULT_DYE_CONC_MGL: float = 100.0
DEFAULT_SIM_DURATION_S: float = 3600.0

# --------------------------------------------------------------------------- #
# Mesh granularity autoscaler (BK-3c) - resolution is a USER/LLM lever, NEVER a
# hardcoded constant. The worker meshes a channel ribbon of length L (the reach)
# x width W with a single uniform gmsh target edge length ``h`` (mesh_size_m).
# Two physics/cost constraints bound ``h``:
#   (1) ACROSS-CHANNEL RESOLUTION: the plume must be resolved across the channel,
#       so we need >= N cells spanning the width -> h <= W / N. N is set by the
#       chosen resolution preset (fine = more cells across, coarse = fewer). This
#       is the dominant constraint for a narrow reach.
#   (2) NODE BUDGET: a triangulated ribbon of area A = L*W has ~A/(k*h^2) nodes
#       (k ~ 0.87 for good-quality equilateral triangles). Cap it at NODE_CAP so
#       a long reach can't explode the solve -> h >= sqrt(A / (k*NODE_CAP)).
# The final h is max(across-channel target, budget floor), then clamped to an
# absolute [MESH_H_FLOOR_M, W/2] sanity range (>= 2 cells across no matter what).
# An explicit override_m (LLM/user "use 8 m edges") wins outright but is still
# budget-clamped so a reckless value can't wedge the solver.
# --------------------------------------------------------------------------- #
#: cells-across-the-channel target per resolution preset. "medium"/"auto" ~= the
#: legacy DEFAULT_MESH_SIZE_M (14 m on a 60 m channel = ~4.3 cells across).
MESH_CELLS_ACROSS_BY_PRESET: dict[str, float] = {
    "fine": 6.0,
    "medium": 60.0 / DEFAULT_MESH_SIZE_M,  # ~4.3, parity with the old default
    "auto": 60.0 / DEFAULT_MESH_SIZE_M,
    "coarse": 3.0,
}
#: node-count ceiling for a single local-docker TELEMAC reach (keeps the solve to
#: minutes, not hours). The autoscaler coarsens ``h`` to stay under this.
MESH_NODE_CAP: int = 60000
#: triangulated-ribbon node-density constant (nodes ~= area / (k * h^2)).
#: CALIBRATED against two live TELEMAC meshes of the 8 km x 60 m Snake reach
#: (h=20 -> 3011 nodes -> k=0.40; h=10 -> 10230 nodes -> k=0.47), so the node
#: estimate the approve-mesh gate shows tracks reality within ~15%.
_MESH_NODE_K: float = 0.43
#: absolute gmsh edge-length floor (below this gmsh quality + solve cost degrade).
MESH_H_FLOOR_M: float = 3.0
#: TELEMAC-2D timestep MUST be coupled to the mesh edge length or the solve
#: DIVERGES (CFL). Proven live 2026-07-17 on the 8 km Snake reach:
#:   (h=20, dt=1.0)   -> OK      (h=14, dt=1.0) -> OK (historical default)
#:   (h=10, dt=1.0)   -> CRASH   (h=10, dt=0.714) -> CRASH   (h=10, dt=0.5) -> OK
#: The stable dt scales with the edge length (constant Courant):
#: dt = TIMESTEP_REF_S * min(1, h / MESH_TIMESTEP_REF_M). Anchored at h=20 m ->
#: 1 s so the law passes THROUGH both live-proven-stable points - (20, 1.0) and
#: (10, 0.5) - and lands at or below the stable dt at every tested size (h=14 ->
#: 0.7 s, safely under its proven-stable 1.0 s; a smaller dt at a fixed mesh is
#: strictly more stable). An earlier /14 anchor shipped h=10 -> 0.714 s, which
#: the live solve REJECTED - hence the conservative /20. This makes "fine" usable.
TIMESTEP_REF_S: float = 1.0
MESH_TIMESTEP_REF_M: float = 20.0
#: floor on the coupled timestep (a runaway-fine mesh can't drive dt to zero).
TIMESTEP_FLOOR_S: float = 0.2


def suggest_time_step_s(mesh_size_m: float) -> float:
    """CFL-safe TELEMAC timestep for a given mesh edge length (BK-3c / OPEN-27).

    dt scales with the edge length (constant Courant), capped at the proven-stable
    1 s for meshes >= 14 m so the default is unchanged, floored so a very fine mesh
    still terminates. Threaded into the worker manifest as ``time_step_s`` (an
    existing ReachConfig field) - no worker rebuild needed.
    """
    h = max(float(mesh_size_m), MESH_H_FLOOR_M)
    dt = TIMESTEP_REF_S * min(1.0, h / MESH_TIMESTEP_REF_M)
    return round(max(dt, TIMESTEP_FLOOR_S), 3)


#: Conservative TELEMAC throughput in node-steps/second, calibrated on the two
#: live 2026-07-17 runs (coarse 3011 nodes x 10800 steps = 86 s; fine 10230 x
#: 21600 = 358 s -> rates 0.377M and 0.618M/s; take the SLOWER so estimates err
#: HIGH - never promise fast then run slow). Covers the worker's full wall
#: (NLDI + DEM + probe + final solve) for a typical reach.
_TELEMAC_NODE_STEPS_PER_S: float = 377_000.0
#: Fixed overhead outside the node-step model (container start + fetches).
_TELEMAC_SOLVE_OVERHEAD_S: float = 45.0


def estimate_telemac_solve_seconds(
    npoin: int, sim_duration_s: float, time_step_s: float
) -> float:
    """Conservative wall-clock estimate for a full TELEMAC dye solve.

    ``wall ~= npoin * (sim_duration / dt) / RATE + overhead`` - the gate card's
    ``estimated_solve_seconds``. Errs high by design (the calibrated rate is the
    slower of the two live datapoints)."""
    steps = max(float(sim_duration_s), 0.0) / max(float(time_step_s), 1e-6)
    est = max(int(npoin), 0) * steps / _TELEMAC_NODE_STEPS_PER_S
    return round(est + _TELEMAC_SOLVE_OVERHEAD_S, 1)


def _estimate_mesh_nodes(reach_length_km: float, channel_width_m: float, h: float) -> int:
    """Estimated node count for a length x width channel ribbon meshed at edge ``h``."""
    area = max(reach_length_km, 0.0) * 1000.0 * max(channel_width_m, 0.0)
    if h <= 0.0 or area <= 0.0:
        return 0
    return int(round(area / (_MESH_NODE_K * h * h)))


def suggest_mesh_size_m(
    reach_length_km: float,
    channel_width_m: float,
    resolution: str = "auto",
    override_m: float | None = None,
) -> tuple[float, int, str]:
    """Pick the mesh target edge length ``h`` (BK-3c). Returns ``(h, est_nodes, label)``.

    ``resolution`` is a preset ("auto"/"medium"/"fine"/"coarse"); ``override_m`` is
    an explicit edge length that wins outright (still budget-clamped). Never
    returns the hardcoded default blindly - it is always derived from the reach
    geometry + the chosen lever, so a small AOI gets a fine mesh and a long reach
    gets coarsened under the node budget.
    """
    L = max(float(reach_length_km), 0.0)
    W = max(float(channel_width_m), 1.0)
    preset = str(resolution or "auto").strip().lower()

    # budget floor: coarsest h that keeps node count <= MESH_NODE_CAP.
    area = L * 1000.0 * W
    budget_floor = (area / (_MESH_NODE_K * MESH_NODE_CAP)) ** 0.5 if area > 0 else MESH_H_FLOOR_M

    if override_m is not None and float(override_m) > 0.0:
        h = float(override_m)
        label = f"custom {h:.3g} m"
    else:
        cells = MESH_CELLS_ACROSS_BY_PRESET.get(preset, MESH_CELLS_ACROSS_BY_PRESET["auto"])
        h = W / cells
        label = f"auto ({preset})" if preset in ("auto",) else preset

    # apply constraints: never finer than the absolute floor / budget floor, never
    # coarser than 2 cells across the channel.
    h = max(h, MESH_H_FLOOR_M, budget_floor)
    h = min(h, W / 2.0)
    if override_m is not None and h > float(override_m):
        label += f" -> {h:.3g} m (budget-clamped)"

    est_nodes = _estimate_mesh_nodes(L, W, h)
    return round(h, 3), est_nodes, label


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #
class TelemacDyeScenarioError(RuntimeError):
    """Base class for ``model_river_dye_release_scenario`` failures.

    Carries an open-set ``error_code`` propagated to the agent emitter so the
    failure renders a typed error frame (never a silent dead-end)."""

    error_code: str = "TELEMAC_DYE_SCENARIO_ERROR"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class TelemacDyeScenarioInputError(TelemacDyeScenarioError):
    """Caller supplied neither a location string nor a bbox (or both)."""

    def __init__(self, message: str) -> None:
        super().__init__("TELEMAC_DYE_SCENARIO_INPUT_INVALID", message)


# --------------------------------------------------------------------------- #
# Registry / geometry helpers
# --------------------------------------------------------------------------- #
async def _call_registry_tool(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Invoke a registry tool fn that may be sync (returns the value) or async
    (returns an awaitable) - normalize both (what _maybe_emit does internally)."""
    import inspect

    out = fn(*args, **kwargs)
    if inspect.isawaitable(out):
        out = await out
    return out


def _is_state_snap_geocode(geo: Any) -> bool:
    """True when geocode_location fell back to a WHOLE-STATE bbox.

    A state-snap centroid is the middle of the state - as a river-reach seed it
    is ~100+ km of drift (THE root cause of OPEN-25a: 'Snake River near Twin
    Falls' geocoding to central Idaho). Never seed a reach from one."""
    return isinstance(geo, dict) and (
        geo.get("source") == "state-bbox-fallback"
        or geo.get("fallback_reason") is not None
    )


def _locality_tail(location: str) -> str | None:
    """Extract the locality phrase from a river+locality compound query.

    'Snake River near Twin Falls, Idaho' -> 'Twin Falls, Idaho'. Nominatim
    often has no feature for the compound but pins the locality fine; the
    worker NLDI-snaps the locality seed to the nearest flowline anyway."""
    import re

    for sep in ("near", "at", "by", "outside", "in"):
        m = re.search(rf"\b{sep}\b(.+)$", location, flags=re.IGNORECASE)
        if m:
            tail = m.group(1).strip(" ,")
            if tail and tail.lower() != location.strip().lower():
                return tail
    return None


_WATERCOURSE_TYPES = ("river", "creek", "slough", "fork", "bayou")
_NAME_STOPWORDS = frozenset({"the", "a", "an", "on", "in", "into", "near", "at", "by"})


def _named_watercourse(location: str) -> str | None:
    """The GNIS-style watercourse name in a location phrase, or None.

    'Columbia River near Longview, Washington' -> 'Columbia River'. OPEN-26:
    the worker re-seeds onto the NAMED mainstem (gnis_name flowline query)
    before the NLDI position-snap, so a geocode near a confluence stops
    landing the mesh on the tributary/slough."""
    import re

    m = re.search(
        rf"\b((?:[\w'.-]+\s+){{1,3}}(?:{'|'.join(_WATERCOURSE_TYPES)}))\b",
        str(location or ""), flags=re.IGNORECASE,
    )
    if not m:
        return None
    words = m.group(1).split()
    while words and words[0].lower() in _NAME_STOPWORDS:
        words = words[1:]
    if len(words) < 2:  # need at least '<Name> River'
        return None
    return " ".join(w.title() for w in words)


async def _geocode_seed_center(
    geocode_fn: Any, location: str, geo: Any
) -> tuple[float, float, str]:
    """Resolve (lon, lat, name) for the reach seed from a geocode result,
    REJECTING state-snaps (OPEN-25a hardening).

    ``geo`` is the first-attempt result (already fetched by the caller so the
    emit-wrapped card shows the user's own phrase). On a state-snap, retry
    ONCE with the locality tail; if that also snaps (or no tail exists), raise
    the typed ambiguity error instead of simulating the wrong river."""
    if _is_state_snap_geocode(geo):
        tail = _locality_tail(location)
        retry = None
        if tail:
            logger.info(
                "telemac seed geocode: %r snapped to a whole state; retrying "
                "with locality tail %r", location, tail,
            )
            try:
                retry = await _call_registry_tool(geocode_fn, tail)
            except Exception as exc:  # noqa: BLE001 -- fall through to the typed error
                logger.warning("telemac seed geocode retry failed: %s", exc)
        if retry is not None and not _is_state_snap_geocode(retry):
            geo = retry
        else:
            raise TelemacDyeScenarioError(
                "TELEMAC_DYE_GEOCODE_AMBIGUOUS",
                f"geocode_location({location!r}) only matched a whole US state "
                "- too coarse to place a river reach (the centroid would be "
                "~100 km off). Give a more specific place (a city/town near "
                "the reach) or an explicit bbox AOI.",
            )
    glat = geo.get("latitude") if isinstance(geo, dict) else None
    glon = geo.get("longitude") if isinstance(geo, dict) else None
    if glat is None or glon is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_GEOCODE_FAILED",
            f"geocode_location({location!r}) returned no centroid lat/lon.",
        )
    return float(glon), float(glat), str(geo.get("name") or location)


def _registry_fn(name: str) -> Any:
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            f"required atomic tool {name!r} is not registered.",
        )
    return entry.fn


def _bbox_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    return (0.5 * (bbox[0] + bbox[2]), 0.5 * (bbox[1] + bbox[3]))


def _bbox_around(lon: float, lat: float, half_deg: float) -> tuple[float, float, float, float]:
    return (lon - half_deg, lat - half_deg, lon + half_deg, lat + half_deg)


def _layer_field(result: Any, field: str) -> Any:
    if result is None:
        return None
    if hasattr(result, field):
        return getattr(result, field)
    if isinstance(result, dict):
        return result.get(field)
    return None


def _river_seed_from_geometry(river_uri: str) -> tuple[float, float] | None:
    """Pick a mid-reach seed ``(lon, lat)`` on the LONGEST flowline in the fetched
    river FlatGeobuf, so the worker's NLDI snap lands on the main stem (not a
    stray ditch). Pure geopandas/shapely; downloads the FGB via the SAME boto3
    client the solver uses (MinIO-aware via AWS_ENDPOINT_URL). Returns ``None`` on
    ANY failure (the composer then falls back to the geocoded centroid, which the
    worker NLDI-snaps regardless)."""
    try:
        from ..tools.solver import _get_s3_client, _split_object_uri

        local_fgb: str | None = None
        if river_uri.startswith("s3://") or river_uri.startswith("gs://"):
            _scheme, bucket, key = _split_object_uri(river_uri)
            s3 = _get_s3_client()
            tmp = tempfile.NamedTemporaryFile(
                suffix=".fgb", delete=False, prefix="telemac_river_seed_"
            )
            tmp.close()
            resp = s3.get_object(Bucket=bucket, Key=key)
            with open(tmp.name, "wb") as fh:
                fh.write(resp["Body"].read())
            local_fgb = tmp.name
        else:
            local_fgb = river_uri  # a local path (test seam)

        import geopandas as gpd

        gdf = gpd.read_file(local_fgb)
        if gdf.empty:
            return None
        # Reproject to EPSG:4326 for consistent lon/lat + length ranking in a
        # metric-ish sense (geographic length is a fine proxy for "longest").
        if gdf.crs is not None and str(gdf.crs).upper() not in ("EPSG:4326", "WGS84"):
            try:
                gdf = gdf.to_crs(4326)
            except Exception:  # noqa: BLE001
                pass
        lines = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])]
        if lines.empty:
            return None
        longest = max(lines.geometry, key=lambda g: g.length)
        # Explode a MultiLineString to its longest part, then take the midpoint.
        if longest.geom_type == "MultiLineString":
            longest = max(longest.geoms, key=lambda g: g.length)
        mid = longest.interpolate(0.5, normalized=True)
        return (float(mid.x), float(mid.y))
    except Exception as exc:  # noqa: BLE001 -- seed extraction is best-effort
        logger.warning("telemac dye: river-seed extraction failed (non-fatal): %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Manifest staging (cache bucket)
# --------------------------------------------------------------------------- #
def _stage_manifest(
    reach: dict[str, Any], run_tag: str, *, mesh_only: bool = False
) -> str:
    """Write the ``telemac_river_dye`` worker manifest to the cache bucket and
    return its ``s3://`` URI (``run_solver`` downloads it to the rundir).

    ``mesh_only=True`` (BK-3b approve-mesh gate) flags the worker's fast
    mesh-preview mode: build the mesh, write ``river.slf`` + the EPSG:4326
    ``mesh_preview.geojson`` wireframe + gate-stat metrics, skip the solve."""
    from ..tools.solver import _get_s3_client

    cache_bucket = (os.environ.get("GRACE2_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_STAGING_FAILED",
            "GRACE2_CACHE_BUCKET must be set to stage the TELEMAC manifest.",
        )
    outputs = [
        "r2d_river.slf",
        "river.slf",
        "river.cli",
        "t2d_river.cas",
        "full_listing.log",
        "telemac_metrics.json",
    ]
    if mesh_only:
        outputs = ["river.slf", "river.cli", "mesh_preview.geojson",
                   "telemac_metrics.json"]
    manifest = {
        "reach": reach,
        "run_id": run_tag,
        "inputs": [],  # the pipeline self-fetches NHDPlus + the DEM
        "telemac_args": [],  # the image CMD drives the entrypoint
        "outputs": outputs,
    }
    if mesh_only:
        manifest["mesh_only"] = True
    key = f"telemac/{run_tag}/manifest.json"
    s3 = _get_s3_client()
    s3.put_object(
        Bucket=cache_bucket,
        Key=key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{cache_bucket}/{key}"


def _download_telemac_result(run_id: str) -> tuple[str, int]:
    """Download ``r2d_river.slf`` + read ``utm_epsg`` from ``telemac_metrics.json``
    for a completed run. Returns ``(local_slf_path, utm_epsg)``. Raises
    ``TelemacDyeScenarioError`` when the SELAFIN result is missing."""
    from ..tools.solver import _get_runs_bucket, _get_s3_client

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()

    # utm_epsg from telemac_metrics.json (the SELAFIN carries no CRS).
    utm_epsg: int | None = None
    try:
        obj = s3.get_object(Bucket=runs_bucket, Key=f"{run_id}/telemac_metrics.json")
        metrics = json.loads(obj["Body"].read().decode("utf-8"))
        if isinstance(metrics, dict) and metrics.get("utm_epsg") is not None:
            utm_epsg = int(metrics["utm_epsg"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemac dye: metrics read failed for run %s: %s", run_id, exc)

    slf_key = f"{run_id}/r2d_river.slf"
    tmp_dir = tempfile.mkdtemp(prefix=f"telemac-dye-{run_id}-")
    slf_path = str(Path(tmp_dir) / "r2d_river.slf")
    try:
        resp = s3.get_object(Bucket=runs_bucket, Key=slf_key)
        with open(slf_path, "wb") as fh:
            fh.write(resp["Body"].read())
    except Exception as exc:  # noqa: BLE001
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_OUTPUT_MISSING",
            f"TELEMAC run {run_id} completed but s3://{runs_bucket}/{slf_key} "
            f"was not downloadable: {exc}",
        ) from exc

    if utm_epsg is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_OUTPUT_MISSING",
            f"TELEMAC run {run_id} produced no utm_epsg in telemac_metrics.json; "
            "cannot georeference the SELAFIN mesh.",
        )
    return slf_path, utm_epsg


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #
async def model_river_dye_release_scenario(
    location: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    spill_fraction: float = DEFAULT_SPILL_FRACTION,
    spill_duration_s: float = DEFAULT_PULSE_WINDOW_S,
    dye_concentration_mgl: float = DEFAULT_DYE_CONC_MGL,
    reach_length_km: float = DEFAULT_REACH_LENGTH_KM,
    sim_duration_s: float = DEFAULT_SIM_DURATION_S,
    source_q_m3s: float = DEFAULT_SOURCE_Q_M3S,
    channel_width_m: float = DEFAULT_CHANNEL_WIDTH_M,
    river_geometry_uri: str | None = None,
    mesh_resolution: str = "auto",
    mesh_resolution_m: float | None = None,
    release_lon: float | None = None,
    release_lat: float | None = None,
    substance: str = "dye",
    *,
    compute_class: str = "medium",
    pipeline_emitter: Any | None = None,
) -> TelemacDyeLayerURI:
    """Compose place/AOI -> river reach -> TELEMAC-2D dye pulse -> animated layer.

    Supply exactly one of ``location`` (a place name, geocoded - the natural-prompt
    path) or ``bbox`` (an explicit AOI, e.g. a drawn canvas AOI). Optionally pass a
    ``river_geometry_uri`` (an already-fetched ``fetch_river_geometry`` flowline) to
    reuse it for the seed instead of re-fetching. Returns the published
    ``TelemacDyeLayerURI`` (a ``LayerURI`` subtype) so the emit_tool_call
    ``add_loaded_layer`` gate fires and ``export_case_to_qgis`` discovers the
    SELAFIN mesh sibling for animation.

    Raises ``TelemacDyeScenarioError`` (typed error_code) on any fatal step and
    propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    has_loc = bool(location and str(location).strip())
    has_bbox = bbox is not None
    if has_loc == has_bbox:  # both or neither
        raise TelemacDyeScenarioInputError(
            "supply exactly one of location or bbox "
            f"(got location={has_loc}, bbox={has_bbox})."
        )

    emitter = pipeline_emitter or current_emitter()
    prefetched_river = bool(river_geometry_uri and str(river_geometry_uri).strip())

    # Plan the user-meaningful atomic-tool count for the breadcrumb: geocode
    # (place path only) + fetch_river_geometry (only when NOT pre-fetched) +
    # run_solver + postprocess + publish_layer. Each substep is a no-op when no
    # emitter is bound.
    _planned = 3  # run_solver + postprocess + publish
    if has_loc:
        _planned += 1  # geocode_location
    if not prefetched_river:
        _planned += 1  # fetch_river_geometry
    begin_substeps(current_emitter(), _planned)

    # --- Stage 1: resolve the AOI + centroid (F46: geocode, never hand-type) -- #
    if has_loc:
        geocode_fn = _registry_fn("geocode_location")
        async with substep(current_emitter(), "geocode_location"):
            geo = await _maybe_emit(
                pipeline_emitter,
                name=f"Geocode: {location}",
                tool_name="geocode_location",
                invoke=lambda: geocode_fn(location),
            )
        # OPEN-25a hardening: reject whole-state snaps (retry with the locality
        # tail, else typed ambiguity error - never seed from a state centroid).
        center_lon, center_lat, location_name = await _geocode_seed_center(
            geocode_fn, str(location), geo
        )
    else:
        assert bbox is not None
        center_lon, center_lat = _bbox_center(bbox)
        location_name = f"AOI ({center_lat:.4f}, {center_lon:.4f})"

    river_bbox = _bbox_around(center_lon, center_lat, DEFAULT_RIVER_AOI_HALF_DEG)

    # --- Stage 2: obtain the river flowline (reuse a provided one, else fetch)
    #     + pick a mid-reach seed. When the caller already fetched the reach
    #     (fetch_river_geometry -> river_geometry_uri), reuse it -- no re-fetch. -- #
    if prefetched_river:
        river_uri: str | None = str(river_geometry_uri)
    else:
        fetch_river_fn = _registry_fn("fetch_river_geometry")
        async with substep(current_emitter(), "fetch_river_geometry"):
            river_layer = await _maybe_emit(
                pipeline_emitter,
                name="Fetch river geometry",
                tool_name="fetch_river_geometry",
                invoke=lambda: fetch_river_fn(bbox=river_bbox),
            )
        river_uri = _layer_field(river_layer, "uri")
    seed: tuple[float, float] | None = None
    if river_uri:
        seed = await asyncio.to_thread(_river_seed_from_geometry, str(river_uri))
    if seed is None:
        # Fall back to the geocoded centroid; the worker NLDI-snaps it to the
        # nearest flowline COMID regardless (honest degrade, never a dead-end).
        seed = (center_lon, center_lat)
        seed_source = "geocoded-centroid (NLDI will snap to the nearest flowline)"
    else:
        seed_source = "mid-reach point on the largest fetched flowline"
    seed_lon, seed_lat = seed

    # --- Stage 3: stage the worker manifest (ReachConfig overrides) ----------- #
    # BK-3c: mesh resolution is derived from the reach geometry + the chosen lever
    # (auto/preset/explicit override), NEVER the hardcoded default. Surfaced on the
    # returned layer so the agent narrates it and the approve-mesh gate can show it.
    mesh_size_m, mesh_node_estimate, mesh_resolution_label = suggest_mesh_size_m(
        reach_length_km=reach_length_km,
        channel_width_m=channel_width_m,
        resolution=mesh_resolution,
        override_m=mesh_resolution_m,
    )
    # OPEN-27: couple the timestep to the mesh so a finer mesh does not diverge
    # (CFL). Proven live: fine h=10 crashed at fixed dt=1 s, ran clean at dt<=0.5.
    time_step_s = suggest_time_step_s(mesh_size_m)
    logger.info(
        "model_river_dye_release_scenario mesh granularity: %s -> h=%.3g m "
        "(~%d nodes, dt=%.3g s, reach=%.3g km x %.3g m)",
        mesh_resolution_label, mesh_size_m, mesh_node_estimate, time_step_s,
        reach_length_km, channel_width_m,
    )
    reach_name = _slug(location_name)
    # OPEN-26: hand the worker the NAMED watercourse so it re-seeds onto the
    # gnis_name mainstem (confluence disambiguation, Columbia-proven).
    river_name = _named_watercourse(location or location_name) or ""
    reach: dict[str, Any] = {
        "name": reach_name,
        "seed_lon": round(seed_lon, 6),
        "seed_lat": round(seed_lat, 6),
        **({"river_name": river_name} if river_name else {}),
        "nav_direction": "DM",
        "distance_km": float(reach_length_km),
        "channel_width_m": float(channel_width_m),
        "mesh_size_m": mesh_size_m,
        "time_step_s": time_step_s,
        "dye_conc_mgl": float(dye_concentration_mgl),
        # BK-6: user-picked release point overrides spill_frac (worker snaps to
        # the nearest interior mesh node, validated within 2 channel widths).
        **({"release_lon": round(float(release_lon), 6),
            "release_lat": round(float(release_lat), 6)}
           if release_lon is not None and release_lat is not None else {}),
        "spill_frac": float(min(max(spill_fraction, 0.0), 1.0)),
        "pulse_window_s": float(spill_duration_s),
        "source_q_m3s": float(source_q_m3s),
        "duration_s": float(sim_duration_s),
    }
    run_tag = new_ulid()
    manifest_uri = await asyncio.to_thread(_stage_manifest, reach, run_tag)
    logger.info(
        "model_river_dye_release_scenario staged manifest run_tag=%s seed=(%.5f,%.5f) "
        "seed_source=%s reach=%s -> %s",
        run_tag, seed_lon, seed_lat, seed_source, reach_name, manifest_uri,
    )

    # --- Stage 4: dispatch to the solver (generic run_solver seam) ------------ #
    from ..tools.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    handle = run_solver(
        solver=TELEMAC_SOLVER_NAME,
        model_setup_uri=manifest_uri,
        compute_class=compute_class,
    )
    run_id = handle.run_id

    _sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter,
        solver=TELEMAC_SOLVER_NAME,
        handle=handle,
        compute_class=compute_class,
    )
    if emitter is not None and _sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))

    _progress_task = asyncio.ensure_future(
        drive_live_solve_progress(
            emitter=current_emitter(),
            run_id=run_id,
            solver=TELEMAC_SOLVER_NAME,
            grid_resolution_m=None,
            active_cell_count=None,
            vcpus=None,
            eta_seconds=None,
        )
    )
    run_result = None

    class _SolveReturnedFailed(RuntimeError):
        pass

    # OPEN-29 companion: the default 1800 s wait outran a cap-sized solve live
    # (74k nodes x 14400 steps ~ 38 min -> publish leg lost to the timeout).
    # Bound by the WORST honest mesh (the node cap; the preview re-clamp keeps
    # any approved h at or under it) with 1.5x headroom, floored at 1800.
    _wait_s = max(
        1800.0,
        estimate_telemac_solve_seconds(
            MESH_NODE_CAP, float(reach["duration_s"]), float(reach["time_step_s"])
        ) * 1.5,
    )
    try:
        async with substep(emitter, "run_solver"):
            try:
                run_result = await wait_for_completion(handle, timeout_s=_wait_s)
            except asyncio.CancelledError:
                logger.info("model_river_dye_release_scenario cancelled awaiting solver")
                await route_sim_terminal(emitter, _sim_step_id, run_result=None)
                raise
            finally:
                _progress_task.cancel()
                try:
                    await _progress_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                set_emitter_binding(None)
            if run_result.status != "complete":
                raise _SolveReturnedFailed
    except _SolveReturnedFailed:
        pass

    await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

    if run_result is None or run_result.status != "complete":
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_RUN_FAILED",
            "TELEMAC dye solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or getattr(run_result, 'cancellation_reason', '') or ''}",
        )

    # --- Stage 5: download the SELAFIN result + postprocess to the dye COG ---- #
    batch_run_id = getattr(run_result, "run_id", None) or run_id
    slf_path, utm_epsg = await asyncio.to_thread(_download_telemac_result, batch_run_id)

    try:
        async with substep(emitter, "postprocess_telemac"):
            layers, metrics = await asyncio.to_thread(
                postprocess_telemac,
                slf_path,
                run_id=batch_run_id,
                utm_epsg=utm_epsg,
                reach_name=reach_name,
                substance=substance,
            )
    finally:
        try:
            Path(slf_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    if not layers:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_NO_LAYERS",
            "postprocess_telemac produced no dye layer (empty tracer field?).",
        )
    raw_peak = layers[0]

    # --- Stage 6: publish the peak COG (render chokepoint) + honest narration - #
    async with substep(emitter, "publish_layer"):
        peak = await asyncio.to_thread(
            _publish_peak_layer, raw_peak, batch_run_id, location_name, reach_name,
            mesh_size_m, mesh_node_estimate, mesh_resolution_label, substance,
        )

    logger.info(
        "model_river_dye_release_scenario complete run_id=%s reach=%s "
        "dye_cmax_mgl=%.4g plume_reach_m=%s active_frames=%s peak_uri=%s",
        batch_run_id, reach_name, peak.dye_cmax_mgl, peak.plume_reach_m,
        peak.active_frames, peak.uri,
    )

    # --- Best-effort downstream concentration chart (never blocks) ----------- #
    if emitter is not None:
        try:
            await _maybe_emit_chart(emitter, metrics, location_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("telemac dye: concentration chart skipped: %s", exc)

    # --- AUTHORITATIVE LAST zoom-to ----------------------------------------- #
    if emitter is not None and peak.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(peak.bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("model_river_dye_release_scenario: zoom-to failed: %s", exc)

    return peak


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _slug(name: str) -> str:
    """A safe reach slug for the ReachConfig ``name`` (ASCII, underscores)."""
    keep = [c.lower() if (c.isalnum()) else "_" for c in str(name)]
    slug = "".join(keep).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return (slug or "river_dye")[:48]


def _publish_peak_layer(
    raw_peak: TelemacDyeLayerURI, run_id: str, location_name: str, reach_name: str,
    mesh_size_m: float | None = None,
    mesh_node_estimate: int | None = None,
    mesh_resolution_label: str | None = None,
    substance: str = "dye",
) -> TelemacDyeLayerURI:
    """Publish the peak dye COG through publish_layer (render chokepoint) and
    enrich the narration. On publish failure the raw peak is returned UNCHANGED
    (the raw s3:// COG still lets export_case_to_qgis discover the mesh sibling;
    the dispatch-level emit_layer_uri guardrail handles the map honesty).

    The three mesh_* params are the composer's chosen granularity (BK-3c),
    threaded explicitly - referencing composer locals here was a NameError that
    crashed every publish (caught by the BK-3b seam audit)."""
    surrogate = ""
    if substance and substance != "dye":
        surrogate = (
            f" NOTE: {substance} is modeled as a passively advected dissolved "
            f"tracer (transport + dilution only) - NOT slick physics "
            f"(no spreading/evaporation/weathering/beaching)."
        )
    honesty = (
        f"Idealized demo: a FINITE mid-reach point-source {substance or 'dye'} "
        f"pulse released on "
        f"the real {location_name} river reach (NLDI/NHDPlus geometry) over a "
        f"planar idealized channel bed with prescribed tracer dispersion. The "
        f"raster is the PEAK concentration envelope over the run; the animation "
        f"plays from the native SELAFIN mesh. Not a calibrated site study."
        + surrogate
    )
    # BK-3c: the chosen mesh granularity travels on every return branch so the
    # agent can narrate it and the approve-mesh gate can display it.
    mesh_meta = {
        "mesh_size_m": mesh_size_m,
        "mesh_node_estimate": mesh_node_estimate,
        "mesh_resolution_label": mesh_resolution_label,
    }
    if raw_peak.layer_type != "raster" or not (
        raw_peak.uri.startswith("gs://") or raw_peak.uri.startswith("s3://")
    ):
        return raw_peak.model_copy(update={"fallback_note": honesty, **mesh_meta})
    layer_id_for_pub = f"telemac-dye-peak-{run_id}"
    try:
        published_uri = publish_layer(
            layer_uri=raw_peak.uri,
            layer_id=layer_id_for_pub,
            style_preset=raw_peak.style_preset or TELEMAC_DYE_STYLE_PRESET,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_river_dye_release_scenario: publish_layer FAILED layer_id=%s "
            "error_code=%s (%s) - returning the unpublished peak.",
            layer_id_for_pub, exc.error_code, exc,
        )
        return raw_peak.model_copy(update={"fallback_note": honesty, **mesh_meta})
    return TelemacDyeLayerURI(
        layer_id=layer_id_for_pub,
        name=raw_peak.name,
        layer_type=raw_peak.layer_type,
        uri=published_uri,
        style_preset=raw_peak.style_preset or TELEMAC_DYE_STYLE_PRESET,
        role=raw_peak.role,
        units=raw_peak.units,
        bbox=raw_peak.bbox,
        legend=raw_peak.legend,
        fallback_note=honesty,
        dye_cmax_mgl=raw_peak.dye_cmax_mgl,
        dye_peak_time_s=raw_peak.dye_peak_time_s,
        plume_reach_m=raw_peak.plume_reach_m,
        active_frames=raw_peak.active_frames,
        mesh_size_m=mesh_size_m,
        mesh_node_estimate=mesh_node_estimate,
        mesh_resolution_label=mesh_resolution_label,
    )


# --------------------------------------------------------------------------- #
# BK-3b: fast mesh-only preview for the approve-mesh gate
# --------------------------------------------------------------------------- #
async def preview_telemac_mesh(
    params: dict[str, Any], *, emitter: Any = None
) -> dict[str, Any]:
    """Build (only) the TELEMAC mesh for the approve-mesh gate - no solve.

    Called by the server's ``_build_telemac_mesh_envelope`` (the ``run_telemac``
    solver-confirm gate builder, mirror of the SWMM #154 builder) BEFORE the tool
    dispatches: resolves the same seed the composer will, stages a ``mesh_only``
    worker manifest, runs the fast mesh-only container (~10-25 s: gmsh, no DEM,
    no solve), emits the resulting triangle-wireframe GeoJSON as a role="input"
    map layer + a zoom-to, and returns the REAL gate stats::

        {run_id, mesh_size_m, time_step_s, npoin, nelem, edge_mean_m,
         est_solve_seconds, resolution_label, location_name, bbox}

    MUST-MATCH NOTE: the seed derivation below (geocode -> river fetch -> mid-
    reach seed, centroid fallback) intentionally mirrors Stages 1-2 of
    ``model_river_dye_release_scenario`` - both are cache-backed tool calls, so
    the approved solve re-derives the SAME seed and reproduces the previewed
    mesh. If you change the seed logic THERE, change it HERE.

    Raises on any failure - the gate caller fails OPEN (card skipped, tool runs
    with its own typed errors), matching the SWMM builder convention.
    """
    from ..layer_uri_emit import publish_input_layer
    from ..tool_arg_normalizer import coerce_bbox_value
    from ..tools.solver import (
        _get_runs_bucket,
        _get_s3_client,
        run_solver,
        wait_for_completion,
    )
    from grace2_contracts.execution import LayerURI

    location = params.get("location")
    coerced_bbox = None
    raw_bbox = params.get("bbox")
    if raw_bbox is not None:
        cb = coerce_bbox_value(raw_bbox)
        if cb is not None:
            coerced_bbox = tuple(cb)
        elif isinstance(raw_bbox, str) and any(c.isalpha() for c in raw_bbox) \
                and not (location and str(location).strip()):
            location = raw_bbox  # LLM put a place name in the bbox field
    has_loc = bool(location and str(location).strip())
    if has_loc and coerced_bbox is not None:
        coerced_bbox = None  # LOCATION wins (mirror of run_telemac, 2026-07-18)
    if not has_loc and coerced_bbox is None:
        raise ValueError("preview_telemac_mesh: no location/bbox in params")

    # Mirror of the tool's LLM-arg hardening (the gate builder sees RAW params):
    # a 50 km reach live-hung gmsh on the meandering centerline - clamp.
    try:
        reach_length_km = float(params.get("reach_length_km") or DEFAULT_REACH_LENGTH_KM)
    except (TypeError, ValueError):
        reach_length_km = DEFAULT_REACH_LENGTH_KM
    reach_length_km = min(max(reach_length_km, 0.5), 15.0)
    try:
        channel_width_m = float(params.get("channel_width_m") or DEFAULT_CHANNEL_WIDTH_M)
    except (TypeError, ValueError):
        channel_width_m = DEFAULT_CHANNEL_WIDTH_M
    channel_width_m = min(max(channel_width_m, 10.0), 1500.0)
    try:
        sim_duration_s = float(params.get("sim_duration_s") or DEFAULT_SIM_DURATION_S)
    except (TypeError, ValueError):
        sim_duration_s = DEFAULT_SIM_DURATION_S
    sim_duration_s = min(max(sim_duration_s, 600.0), 14400.0)
    mesh_resolution = str(params.get("mesh_resolution") or "auto")
    mesh_resolution_m = params.get("mesh_resolution_m")
    river_geometry_uri = params.get("river_geometry_uri")
    if river_geometry_uri and not str(river_geometry_uri).startswith(("s3://", "gs://")):
        river_geometry_uri = None  # pseudo-call string, not a real URI

    # --- Stage 1-2 mirror (QUIET: no substep/tool cards pre-gate) ------------ #
    if has_loc:
        geocode_fn = _registry_fn("geocode_location")
        geo = await _call_registry_tool(geocode_fn, location)
        # OPEN-25a hardening (same as the main composer): reject state-snaps.
        center_lon, center_lat, location_name = await _geocode_seed_center(
            geocode_fn, str(location), geo
        )
    else:
        assert coerced_bbox is not None
        center_lon, center_lat = _bbox_center(coerced_bbox)  # type: ignore[arg-type]
        location_name = f"AOI ({center_lat:.4f}, {center_lon:.4f})"

    river_bbox = _bbox_around(center_lon, center_lat, DEFAULT_RIVER_AOI_HALF_DEG)
    if river_geometry_uri and str(river_geometry_uri).strip():
        river_uri: str | None = str(river_geometry_uri)
    else:
        fetch_river_fn = _registry_fn("fetch_river_geometry")
        river_layer = await _call_registry_tool(fetch_river_fn, bbox=river_bbox)
        river_uri = _layer_field(river_layer, "uri")
    seed: tuple[float, float] | None = None
    if river_uri:
        seed = await asyncio.to_thread(_river_seed_from_geometry, str(river_uri))
    if seed is None:
        seed = (center_lon, center_lat)
    seed_lon, seed_lat = seed

    # --- Granularity (BK-3c) + reach dict (mirror of Stage 3) ---------------- #
    mesh_size_m, mesh_node_estimate, mesh_resolution_label = suggest_mesh_size_m(
        reach_length_km=reach_length_km,
        channel_width_m=channel_width_m,
        resolution=mesh_resolution,
        override_m=(float(mesh_resolution_m) if mesh_resolution_m else None),
    )
    time_step_s = suggest_time_step_s(mesh_size_m)
    preview_river_name = _named_watercourse(location or location_name) or ""
    reach: dict[str, Any] = {
        "name": _slug(location_name),
        "seed_lon": round(seed_lon, 6),
        "seed_lat": round(seed_lat, 6),
        **({"river_name": preview_river_name} if preview_river_name else {}),
        "nav_direction": "DM",
        "distance_km": reach_length_km,
        "channel_width_m": channel_width_m,
        "mesh_size_m": mesh_size_m,
        "time_step_s": time_step_s,
    }
    # OPEN-29: the suggest_mesh_size_m budget floor estimates nodes from the
    # STATED channel width, but real-bank meshing follows the MEASURED river -
    # live 2026-07-18 the Columbia (stated 150-500 m, real ~1400 m) previewed
    # 295k nodes at h=10 against the 60k cap, cascading into a coarsest-rung
    # solve that outran the wait budget. After the first mesh-only build, if
    # the MEASURED npoin blows the cap, re-derive h from the measured node
    # density (nodes scale ~1/h^2) and rebuild ONCE at the honest edge length.
    for attempt in (1, 2):
        run_tag = new_ulid()
        manifest_uri = await asyncio.to_thread(
            _stage_manifest, reach, run_tag, mesh_only=True
        )
        logger.info(
            "preview_telemac_mesh dispatch run_tag=%s seed=(%.5f,%.5f) h=%.3g dt=%.3g",
            run_tag, seed_lon, seed_lat, mesh_size_m, time_step_s,
        )

        # Fast mesh-only worker run (no sim cards; the gate IS the surface).
        handle = run_solver(
            solver=TELEMAC_SOLVER_NAME,
            model_setup_uri=manifest_uri,
            compute_class="small",
        )
        # A healthy mesh-only run is ~10-40 s; 240 s bounds a hung gmsh so a
        # broken preview cannot park the turn before the gate falls open.
        run_result = await wait_for_completion(handle, poll_interval_s=3, timeout_s=240)
        if run_result is None or run_result.status != "complete":
            raise TelemacDyeScenarioError(
                "TELEMAC_MESH_BUILD_FAILED",
                "mesh-only preview run did not complete "
                f"(status={getattr(run_result, 'status', None)}).",
            )
        mesh_run_id = getattr(run_result, "run_id", None) or handle.run_id

        def _read_mesh_metrics() -> dict[str, Any]:
            s3 = _get_s3_client()
            obj = s3.get_object(
                Bucket=_get_runs_bucket(), Key=f"{mesh_run_id}/telemac_metrics.json"
            )
            loaded = json.loads(obj["Body"].read().decode("utf-8"))
            return loaded if isinstance(loaded, dict) else {}

        m = await asyncio.to_thread(_read_mesh_metrics)
        npoin = int(m.get("npoin") or 0)
        nelem = int(m.get("nelem") or 0)
        bbox4326 = m.get("bbox4326")
        if npoin <= 0:
            raise TelemacDyeScenarioError(
                "TELEMAC_MESH_BUILD_FAILED",
                f"mesh-only preview metrics carry no node count (run {mesh_run_id}).",
            )
        if attempt == 1 and npoin > MESH_NODE_CAP * 1.15:
            h_honest = mesh_size_m * (npoin / MESH_NODE_CAP) ** 0.5
            logger.warning(
                "preview_telemac_mesh: measured %d nodes at h=%.3g blows the "
                "%d cap (stated width %.0f m vs real banks) - rebuilding once "
                "at h=%.3g",
                npoin, mesh_size_m, MESH_NODE_CAP, channel_width_m, h_honest,
            )
            mesh_size_m = round(h_honest, 1)
            time_step_s = suggest_time_step_s(mesh_size_m)
            reach["mesh_size_m"] = mesh_size_m
            reach["time_step_s"] = time_step_s
            continue
        break

    # --- Emit the wireframe as a role='input' vector layer + zoom-to --------- #
    # current_emitter() is NOT bound in the pre-dispatch gate context (live
    # finding 2026-07-17: emitter=NONE) - the server passes state.emitter in.
    if emitter is None:
        emitter = current_emitter()
    preview_layer = LayerURI(
        layer_id=f"telemac-mesh-preview-{mesh_run_id}",
        name=f"Mesh preview ({mesh_size_m:g} m edges, {npoin:,} nodes)",
        layer_type="vector",
        uri=f"s3://{_get_runs_bucket()}/{mesh_run_id}/mesh_preview.geojson",
        style_preset="nhdplus_flowlines",  # known line preset -> sane wireframe styling
        role="input",
        bbox=tuple(bbox4326) if bbox4326 else None,
    )
    emitted = await publish_input_layer(emitter, preview_layer)
    logger.info(
        "preview_telemac_mesh wireframe emit: emitter=%s emitted=%s layer=%s",
        "bound" if emitter is not None else "NONE", emitted,
        preview_layer.layer_id,
    )
    if emitter is not None and bbox4326:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox4326)})
        except Exception as exc:  # noqa: BLE001 -- preview zoom is best-effort
            logger.warning("preview_telemac_mesh zoom-to failed: %s", exc)

    return {
        "run_id": mesh_run_id,
        "mesh_size_m": float(mesh_size_m),
        "time_step_s": float(time_step_s),
        "npoin": npoin,
        "nelem": nelem,
        "edge_mean_m": m.get("edge_mean_m"),
        "est_solve_seconds": estimate_telemac_solve_seconds(
            npoin, sim_duration_s, time_step_s
        ),
        "resolution_label": mesh_resolution_label,
        "node_estimate": mesh_node_estimate,
        "location_name": location_name,
        "bbox": bbox4326,
        "wireframe_capped": bool(m.get("wireframe_capped")),
    }


async def _maybe_emit_chart(emitter: Any, metrics: dict[str, Any], location_name: str) -> None:
    """Best-effort dye-concentration summary chart (rise-to-peak). Non-blocking:
    swallows any failure so the map deliverable never depends on a chart. The two
    points are HONEST tracer-field scalars (t0=0 concentration -> the peak
    concentration at its arrival time), not a fabricated curve."""
    if not hasattr(emitter, "emit_chart"):
        return
    cmax = metrics.get("dye_cmax_mgl")
    peak_t = metrics.get("dye_peak_time_s")
    if cmax is None or peak_t is None:
        return
    from ..tools.chart_tools import build_chart_payload  # type: ignore

    vega_lite_spec = {
        "mark": {"type": "line", "point": True},
        "data": {
            "values": [
                {"t_s": 0.0, "dye_mgl": 0.0},
                {"t_s": float(peak_t), "dye_mgl": float(cmax)},
            ]
        },
        "encoding": {
            "x": {"field": "t_s", "type": "quantitative", "title": "Time (s)"},
            "y": {
                "field": "dye_mgl",
                "type": "quantitative",
                "title": "Dye concentration (mg/L)",
            },
        },
    }
    payload = build_chart_payload(
        vega_lite_spec=vega_lite_spec,
        title=f"Peak dye concentration - {location_name}",
        caption=(
            "Reach peak dye concentration and its arrival time (idealized-bed demo)."
        ),
    )
    await emitter.emit_chart(payload)


async def _maybe_emit(
    emitter: Any | None, *, name: str, tool_name: str, invoke: Any
) -> Any:
    """Run ``invoke()`` through ``emitter.emit_tool_call`` if given, else direct."""
    if emitter is not None:
        return await emitter.emit_tool_call(name=name, tool_name=tool_name, invoke=invoke)
    result = invoke()
    if asyncio.iscoroutine(result):
        result = await result
    return result
