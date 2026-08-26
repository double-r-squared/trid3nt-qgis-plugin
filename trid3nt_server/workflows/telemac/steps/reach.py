"""The reach front of every TELEMAC river plan: geocode, flowline, seed, mesh size.

Three declared steps and one declared Data producer:

* ``Geocode.reach`` - place/AOI to a reach centre, and it REBINDS THE DOMAIN, so
  every spatial producer after it reads the reach AOI implicitly.
* ``fetch_reach_flowline`` - the ``Data("rivers")`` producer (reference data:
  fetched fresh for the domain, never supplied).
* ``ReachSeed`` - the mid-reach point on the largest fetched flowline, which is
  what the worker NLDI-snaps the centerline from and what the carrier-discharge
  lookup queries at.

The mesh autoscaler lives here too: resolution is a USER lever, never a
hardcoded constant, so ``h`` is always derived from the reach geometry and the
chosen preset under a node budget.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from typing import Any, NamedTuple

from trid3nt_server.workflows.lib import Step, user_input

from .errors import TelemacDyeScenarioError

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.reach")

__all__ = [
    "DEFAULT_RIVER_AOI_HALF_DEG",
    "Geocode",
    "MESH_H_FLOOR_M",
    "MESH_NODE_CAP",
    "MeshSizing",
    "ReachSeed",
    "SOLVE_TIME_BUDGET_S",
    "coerce_lonlat_point",
    "estimate_telemac_solve_seconds",
    "fetch_reach_flowline",
    "geocode_reach",
    "named_watercourse",
    "reach_seed",
    "slug",
    "suggest_mesh_size_m",
    "suggest_time_step_s",
]

_STEPS = "trid3nt_server.workflows.telemac.steps"

#: Half-width (deg) of the bbox fetched around the geocoded centroid to locate a
#: river reach + pick the seed. ~0.06 deg (~6 km) reliably catches the main stem
#: even when the geocoded city centroid sits a few km off the channel.
DEFAULT_RIVER_AOI_HALF_DEG: float = 0.06

#: Legacy demo default the mesh preset parity is anchored on (60 m channel at
#: ~4.3 cells across).
_DEFAULT_MESH_SIZE_M: float = 14.0

# --------------------------------------------------------------------------- #
# Mesh granularity autoscaler. The worker meshes a channel ribbon of length L x
# width W with a single uniform gmsh target edge length ``h``. Two constraints
# bound it:
#   (1) ACROSS-CHANNEL RESOLUTION: the plume must be resolved across the channel,
#       so h <= W / N cells. The dominant constraint for a narrow reach.
#   (2) NODE BUDGET: a triangulated ribbon of area A has ~A/(k*h^2) nodes; cap it
#       so a long reach cannot explode the solve -> h >= sqrt(A/(k*NODE_CAP)).
# Final h = max(across-channel target, budget floor), clamped to
# [MESH_H_FLOOR_M, W/2] so there are always >= 2 cells across the channel. An
# explicit override wins outright but is still budget-clamped.
# --------------------------------------------------------------------------- #
MESH_CELLS_ACROSS_BY_PRESET: dict[str, float] = {
    "fine": 6.0,
    "medium": 60.0 / _DEFAULT_MESH_SIZE_M,
    "auto": 60.0 / _DEFAULT_MESH_SIZE_M,
    "coarse": 3.0,
}
#: Node ceiling for a single local-docker TELEMAC reach (keeps a solve to minutes).
MESH_NODE_CAP: int = 60000
#: Triangulated-ribbon node density (nodes ~= area / (k * h^2)). Calibrated on two
#: live meshes of the 8 km x 60 m Snake reach (h=20 -> 3011 nodes; h=10 -> 10230),
#: so the node estimate the approve-mesh gate shows tracks reality within ~15%.
_MESH_NODE_K: float = 0.43
#: Absolute gmsh edge-length floor (below it quality + solve cost degrade).
MESH_H_FLOOR_M: float = 3.0
#: The timestep MUST be coupled to the edge length or the solve diverges (CFL).
#: dt = TIMESTEP_REF_S * min(1, h / MESH_TIMESTEP_REF_M), anchored at h=20 -> 1 s
#: so the law passes through both live-proven-stable points (20, 1.0) and
#: (10, 0.5) and lands at or below the stable dt at every tested size.
TIMESTEP_REF_S: float = 1.0
MESH_TIMESTEP_REF_M: float = 20.0
#: Floor on the coupled timestep (a runaway-fine mesh cannot drive dt to zero).
TIMESTEP_FLOOR_S: float = 0.2
#: Wall-clock target for the SUGGESTED mesh's solve; finer rungs stay on the
#: ladder with their own honest estimates.
SOLVE_TIME_BUDGET_S: float = 2700.0

#: Conservative throughput in node-steps/second, calibrated on two live runs
#: (rates 0.377M and 0.618M/s; the SLOWER is taken so estimates err HIGH).
_TELEMAC_NODE_STEPS_PER_S: float = 377_000.0
#: Fixed overhead outside the node-step model (container start + fetches).
_TELEMAC_SOLVE_OVERHEAD_S: float = 45.0


def suggest_time_step_s(mesh_size_m: float) -> float:
    """CFL-safe TELEMAC timestep for a given mesh edge length."""
    h = max(float(mesh_size_m), MESH_H_FLOOR_M)
    dt = TIMESTEP_REF_S * min(1.0, h / MESH_TIMESTEP_REF_M)
    return round(max(dt, TIMESTEP_FLOOR_S), 3)


def estimate_telemac_solve_seconds(
    npoin: int, sim_duration_s: float, time_step_s: float
) -> float:
    """Conservative wall-clock estimate for a full TELEMAC solve. Errs high by design."""
    steps = max(float(sim_duration_s), 0.0) / max(float(time_step_s), 1e-6)
    est = max(int(npoin), 0) * steps / _TELEMAC_NODE_STEPS_PER_S
    return round(est + _TELEMAC_SOLVE_OVERHEAD_S, 1)


def _estimate_mesh_nodes(reach_length_km: float, channel_width_m: float, h: float) -> int:
    area = max(reach_length_km, 0.0) * 1000.0 * max(channel_width_m, 0.0)
    if h <= 0.0 or area <= 0.0:
        return 0
    return int(round(area / (_MESH_NODE_K * h * h)))


class MeshSizing(NamedTuple):
    """The chosen edge length, its node estimate, its label, and any OVERRIDE note.

    ``cap_note`` is populated only when the user's own explicit ``mesh_resolution_m``
    was moved: a lever the run silently ignored is the dishonest case, so the note
    is what the provenance row carries. ``None`` means the ask stood as asked.
    """

    mesh_size_m: float
    node_estimate: int
    label: str
    cap_note: str | None = None


def suggest_mesh_size_m(
    reach_length_km: float,
    channel_width_m: float,
    resolution: str = "auto",
    override_m: float | None = None,
) -> MeshSizing:
    """Pick the mesh target edge length ``h``, and say so when a lever was moved.

    An EXPLICIT ``override_m`` is still bounded on both sides - raised by the node
    budget, lowered by the >= 2-cells-across-the-channel rule - and either move
    contradicts what the user asked for. Both are narrated: the label carries it for
    the layer, ``cap_note`` for the provenance row.
    """
    L = max(float(reach_length_km), 0.0)
    W = max(float(channel_width_m), 1.0)
    preset = str(resolution or "auto").strip().lower()

    area = L * 1000.0 * W
    budget_floor = ((area / (_MESH_NODE_K * MESH_NODE_CAP)) ** 0.5
                    if area > 0 else MESH_H_FLOOR_M)

    asked = float(override_m) if override_m is not None else None
    if asked is not None and asked > 0.0:
        h = asked
        label = f"custom {h:.3g} m"
    else:
        cells = MESH_CELLS_ACROSS_BY_PRESET.get(
            preset, MESH_CELLS_ACROSS_BY_PRESET["auto"])
        h = W / cells
        label = f"auto ({preset})" if preset in ("auto",) else preset

    h = max(h, MESH_H_FLOOR_M, budget_floor)
    h = min(h, W / 2.0)

    cap_note = None
    if asked is not None and h > asked:
        label += f" -> {h:.3g} m (budget-clamped)"
        cap_note = (f"mesh_resolution_m {asked:g} RAISED to {round(h, 3):g} m by the "
                    f"node budget ({MESH_NODE_CAP} nodes over {L:g} km x {W:g} m)")
    elif asked is not None and h < asked:
        label += f" -> {h:.3g} m (width-capped)"
        cap_note = (f"mesh_resolution_m {asked:g} CAPPED to {round(h, 3):g} m by the "
                    f"channel-width rule (width {W:g} m / 2)")

    return MeshSizing(round(h, 3), _estimate_mesh_nodes(L, W, h), label, cap_note)


# --------------------------------------------------------------------------- #
# Geometry + registry helpers
# --------------------------------------------------------------------------- #
def slug(name: str) -> str:
    """A safe reach slug for the worker ReachConfig ``name`` (ASCII, underscores)."""
    keep = [c.lower() if c.isalnum() else "_" for c in str(name)]
    out = "".join(keep).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return (out or "river_dye")[:48]


def coerce_lonlat_point(value: Any, *,
                        label: str = "release point") -> tuple[float, float] | None:
    """``(lon, lat)`` from a wire value, in TELEMAC's own error family.

    The SHAPE rules are the user-input species' (one normalizer, whether the point
    was clicked or typed); what this adds is the engine's error TYPE, because
    callers whose contract is fail-open - the pre-dispatch gate builder, the mesh
    preview - catch ``TelemacDyeScenarioError`` at the call site.
    """
    try:
        return user_input.lonlat_point(value, label=label,
                                       code="TELEMAC_PARAMS_INVALID")
    except user_input.UserInputError as exc:
        raise TelemacDyeScenarioError("TELEMAC_PARAMS_INVALID", str(exc)) from None


def bbox_center(bbox: Any) -> tuple[float, float]:
    return (0.5 * (float(bbox[0]) + float(bbox[2])),
            0.5 * (float(bbox[1]) + float(bbox[3])))


def bbox_around(lon: float, lat: float,
                half_deg: float = DEFAULT_RIVER_AOI_HALF_DEG
                ) -> tuple[float, float, float, float]:
    return (lon - half_deg, lat - half_deg, lon + half_deg, lat + half_deg)


def layer_field(result: Any, field: str) -> Any:
    if result is None:
        return None
    if hasattr(result, field):
        return getattr(result, field)
    if isinstance(result, dict):
        return result.get(field)
    return None


def registry_fn(name: str) -> Any:
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            f"required atomic tool {name!r} is not registered.",
        )
    return entry.fn


async def call_registry_tool(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Invoke a registry tool fn that may be sync or async - normalize both."""
    import inspect

    out = fn(*args, **kwargs)
    if inspect.isawaitable(out):
        out = await out
    return out


def _is_state_snap_geocode(geo: Any) -> bool:
    """True when the geocode fell back to a WHOLE-STATE bbox.

    A state-snap centroid is the middle of the state - as a river-reach seed that
    is 100+ km of drift, so it is never seeded from.
    """
    return isinstance(geo, dict) and (
        geo.get("source") == "state-bbox-fallback"
        or geo.get("fallback_reason") is not None
    )


def _locality_tail(location: str) -> str | None:
    """'Snake River near Twin Falls, Idaho' -> 'Twin Falls, Idaho'.

    Nominatim often has no feature for the compound query but pins the locality
    fine; the worker NLDI-snaps the locality seed to the nearest flowline anyway.
    """
    for sep in ("near", "at", "by", "outside", "in"):
        m = re.search(rf"\b{sep}\b(.+)$", location, flags=re.IGNORECASE)
        if m:
            tail = m.group(1).strip(" ,")
            if tail and tail.lower() != location.strip().lower():
                return tail
    return None


_WATERCOURSE_TYPES = ("river", "creek", "slough", "fork", "bayou")
_NAME_STOPWORDS = frozenset({"the", "a", "an", "on", "in", "into", "near", "at", "by"})


def named_watercourse(location: str) -> str | None:
    """The GNIS-style watercourse name in a location phrase, or None.

    The worker re-seeds onto the NAMED mainstem before the position snap, so a
    geocode near a confluence stops landing the mesh on the tributary.
    """
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


async def _geocode_seed_center(geocode_fn: Any, location: str,
                               geo: Any) -> tuple[float, float, str]:
    """(lon, lat, name) for the reach seed, REJECTING state-snaps.

    On a state-snap, retry once with the locality tail; if that also snaps (or no
    tail exists), refuse typed rather than simulating the wrong river.
    """
    if _is_state_snap_geocode(geo):
        tail = _locality_tail(location)
        retry = None
        if tail:
            logger.info("telemac seed geocode: %r snapped to a whole state; retrying "
                        "with locality tail %r", location, tail)
            try:
                retry = await call_registry_tool(geocode_fn, tail)
            except Exception as exc:  # noqa: BLE001 - fall through to the typed error
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


def river_seed_from_geometry(river_uri: str) -> tuple[float, float] | None:
    """Mid-reach ``(lon, lat)`` on the LONGEST flowline in the fetched FlatGeobuf.

    So the worker's NLDI snap lands on the main stem, not a stray ditch. Returns
    None on ANY failure - the caller then falls back to the geocoded centroid,
    which the worker snaps regardless.
    """
    try:
        from trid3nt_server.workflows.solver.solver import (
            _get_s3_client,
            _split_object_uri,
        )

        if river_uri.startswith(("s3://", "gs://")):
            _scheme, bucket, key = _split_object_uri(river_uri)
            tmp = tempfile.NamedTemporaryFile(
                suffix=".fgb", delete=False, prefix="telemac_river_seed_")
            tmp.close()
            resp = _get_s3_client().get_object(Bucket=bucket, Key=key)
            with open(tmp.name, "wb") as fh:
                fh.write(resp["Body"].read())
            local_fgb = tmp.name
        else:
            local_fgb = river_uri  # a local path (test seam)

        import geopandas as gpd

        gdf = gpd.read_file(local_fgb)
        if gdf.empty:
            return None
        if gdf.crs is not None and str(gdf.crs).upper() not in ("EPSG:4326", "WGS84"):
            try:
                gdf = gdf.to_crs(4326)
            except Exception:  # noqa: BLE001
                pass
        lines = gdf[gdf.geometry.type.isin(["LineString", "MultiLineString"])]
        if lines.empty:
            return None
        longest = max(lines.geometry, key=lambda g: g.length)
        if longest.geom_type == "MultiLineString":
            longest = max(longest.geoms, key=lambda g: g.length)
        mid = longest.interpolate(0.5, normalized=True)
        return (float(mid.x), float(mid.y))
    except Exception as exc:  # noqa: BLE001 -- seed extraction is best-effort
        logger.warning("telemac dye: river-seed extraction failed (non-fatal): %s", exc)
        return None


# --------------------------------------------------------------------------- #
# The declared runners
# --------------------------------------------------------------------------- #
async def geocode_reach(*, location: str | None,
                        bbox: tuple[float, float, float, float] | None) -> dict[str, Any]:
    """Resolve the reach AOI: a geocoded place, or the centre of an explicit bbox.

    The returned ``bbox`` is what REBINDS THE DOMAIN, so every spatial producer
    after this step fetches over the reach AOI without being handed one.
    """
    if bool(location and str(location).strip()):
        geocode_fn = registry_fn("geocode_location")
        geo = await call_registry_tool(geocode_fn, location)
        lon, lat, name = await _geocode_seed_center(geocode_fn, str(location), geo)
    elif bbox is not None:
        lon, lat = bbox_center(bbox)
        name = f"AOI ({lat:.4f}, {lon:.4f})"
    else:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_INPUT_INVALID",
            "the reach needs a place `location` (geocoded) or an explicit `bbox` AOI.",
        )
    return {
        "lon": lon, "lat": lat, "name": name, "slug": slug(name),
        "river_name": named_watercourse(location or name) or "",
        "bbox": bbox_around(lon, lat),
    }


async def fetch_reach_flowline(*, prefetched: str | None = None,
                               fallback: tuple[str, ...] = ()) -> str | None:
    """The reach flowline FlatGeobuf over the CURRENT DOMAIN. Reference data.

    ``prefetched`` reuses a flowline the caller already fetched for this reach -
    the same dataset, not a substituted one, so no cross-dataset gate applies.
    A non-object URI is a model-invented pseudo-call and is ignored.
    """
    if prefetched and str(prefetched).startswith(("s3://", "gs://")):
        return str(prefetched)
    if prefetched:
        logger.warning("telemac: river_geometry_uri %r is not an object URI - ignoring",
                       prefetched)
    from trid3nt_server.workflows.lib import current_domain

    domain = current_domain()
    if domain is None or domain.bbox is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            "the reach flowline cannot be fetched: no domain is bound.",
        )
    layer = await call_registry_tool(registry_fn("fetch_river_geometry"),
                                     bbox=tuple(domain.bbox))
    return layer_field(layer, "uri")


async def reach_seed(*, reach: dict[str, Any], rivers: str | None) -> dict[str, Any]:
    """The mid-reach seed the worker resolves the centerline from.

    The largest fetched flowline's midpoint when there is one, else the geocoded
    centroid - which the worker NLDI-snaps to the nearest flowline COMID anyway,
    so the degrade is honest rather than a dead end.
    """
    seed = None
    if rivers:
        seed = await asyncio.to_thread(river_seed_from_geometry, str(rivers))
    if seed is None:
        return {"lon": float(reach["lon"]), "lat": float(reach["lat"]),
                "source": "geocoded-centroid (NLDI will snap to the nearest flowline)"}
    return {"lon": seed[0], "lat": seed[1],
            "source": "mid-reach point on the largest fetched flowline"}


class Geocode:
    """Reach-AOI resolution steps."""

    @staticmethod
    def reach(location: Any, bbox: Any) -> Step:
        """Place/AOI -> the reach centre. Refines the domain for everything after it."""
        return Step(runner=f"{_STEPS}.reach.geocode_reach", stage="acquire",
                    kwargs={"location": location, "bbox": bbox}).overrides_domain()


def ReachSeed(*, reach: Any, rivers: Any) -> Step:  # noqa: N802 - a value constructor
    """The mid-reach seed on the fetched flowline."""
    return Step(runner=f"{_STEPS}.reach.reach_seed", stage="acquire",
                kwargs={"reach": reach, "rivers": rivers})
