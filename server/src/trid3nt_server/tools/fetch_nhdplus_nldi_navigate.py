"""``fetch_nhdplus_nldi_navigate`` atomic tool — NHDPlus NLDI navigation (job A11).

Wraps the USGS NLDI (Network Linked Data Index) navigation endpoint —
the official REST surface that walks the NHDPlus v2.1 stream network
from any indexed seed feature (a snapped point COMID, an NWIS gauge
site, a Hydrologic Linked Data Index point, etc.) in one of four
directions:

    UM  upstream main stem      — single trunk reaches above the seed
    UT  upstream tributaries    — full tributary network above the seed
    DM  downstream main stem    — single trunk reaches below the seed
    DD  downstream + diversions — main stem plus any anastomosing splits

The output is a FlatGeobuf of NHDPlus flowline LineStrings carrying the
``nhdplus_comid`` join key, suitable for downstream routing analyses,
hazard-cascade overlays (post-fire debris flow downstream impact zones,
pollution plumes, dam-break inundation paths), and for the agent's
"trace water from here to there" narrative class.

API surface (verified live 2026-06-09):

    base:       https://api.water.usgs.gov/nldi/linked-data
    snap:       /comid/position?coords=POINT(<lon> <lat>)
                  → returns the NHDPlus COMID nearest a point
    navigate:   /comid/<comid>/navigation/<mode>/flowlines?distance=<km>
                  → returns a GeoJSON FeatureCollection of flowline
                    LineStrings, each tagged with nhdplus_comid

We expose two seed modes:

    (1) ``comid=<int>``     — start from an already-known NHDPlus COMID
    (2) ``seed_point=(lon, lat)`` — snap a point to the nearest COMID
                                    via /comid/position first

…then call navigate with the requested direction + distance. Output is
written as a FlatGeobuf with the inline-GeoJSON conversion path Wave 4.9
job-0175 codified, so the agent surface can stream it to the map.

FR-CE-8 / FR-DC-3: routed through ``read_through`` with
``ttl_class="static-30d"`` (NHDPlus topology is static on multi-year
horizons), ``source_class="nhdplus_nldi"``. Cache key on
``(seed-comid OR rounded-seed-point, direction, distance_km)``.

Tier-1 free. ``supports_global_query=False`` — CONUS-only.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Literal

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_nhdplus_nldi_navigate",
    "NHDPlusNLDIError",
    "NHDPlusNLDIInputError",
    "NHDPlusNLDIUpstreamError",
    "NHDPlusNLDIEmptyError",
    "estimate_payload_mb",
]

logger = logging.getLogger("trid3nt_server.tools.fetch_nhdplus_nldi_navigate")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class NHDPlusNLDIError(RuntimeError):
    """Base class for fetch_nhdplus_nldi_navigate failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "NHDPLUS_NLDI_ERROR"
    retryable: bool = True


class NHDPlusNLDIInputError(NHDPlusNLDIError):
    """Bad inputs (missing seed, unknown direction, bad distance, etc.)."""

    error_code = "NHDPLUS_NLDI_INPUT_INVALID"
    retryable = False


class NHDPlusNLDIUpstreamError(NHDPlusNLDIError):
    """NLDI HTTP / parse error (5xx, timeout, malformed response)."""

    error_code = "NHDPLUS_NLDI_UPSTREAM_ERROR"
    retryable = True


class NHDPlusNLDIEmptyError(NHDPlusNLDIError):
    """Seed valid but the navigate query returned zero flowlines.

    Typical causes: the seed COMID is at a network terminus (no upstream
    when direction=UM, no downstream when direction=DM), the seed point
    snapped to a stub reach with no traversable network, or the requested
    distance is shorter than the next NHDPlus reach.
    """

    error_code = "NHDPLUS_NLDI_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: USGS NLDI base. Verified live 2026-06-09.
_NLDI_BASE = "https://api.water.usgs.gov/nldi/linked-data"

#: Supported navigation directions per the NLDI API.
_VALID_DIRECTIONS: frozenset[str] = frozenset({"UM", "UT", "DM", "DD"})

#: Allowed distance window in kilometres. NLDI's server-side cap is 9999 km;
#: we cap lower so a single call returns a tractable payload.
_MIN_DISTANCE_KM = 0.0
_MAX_DISTANCE_KM = 1000.0
_DEFAULT_DISTANCE_KM = 50.0

#: CONUS envelope for input-validation early-out (NLDI is CONUS-only).
_CONUS_BBOX: tuple[float, float, float, float] = (-130.0, 20.0, -60.0, 55.0)

#: HTTP timeout (seconds). NLDI navigate is normally <2 s, generous
#: ceiling.
_HTTP_TIMEOUT_S = 60.0

#: User-Agent per USGS usage guidance.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: Hard cap on number of flowlines returned to keep the FlatGeobuf
#: tractable. NLDI will happily emit thousands of reaches for a UT
#: traversal in a big watershed; downstream renderers prefer ≤ a few
#: thousand features.
_MAX_FLOWLINES = 5000


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------


_METADATA = AtomicToolMetadata(
    name="fetch_nhdplus_nldi_navigate",
    ttl_class="static-30d",
    source_class="nhdplus_nldi",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# Payload estimator (Wave 1.5 chat-warning gate).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    direction: str = "DM",
    distance_km: float = _DEFAULT_DISTANCE_KM,
    **_kw: Any,
) -> float:
    """Estimate emitted FlatGeobuf size in MB.

    Empirically each NHDPlus reach LineString serializes to ~300-500
    bytes of FlatGeobuf. A typical DM (main-stem only) trace returns
    1-5 reaches per km of distance; UT (full tributary network) returns
    ~50× more features per km of distance.
    """
    try:
        d = max(0.0, float(distance_km))
    except (TypeError, ValueError):
        d = _DEFAULT_DISTANCE_KM
    per_km = 50 if str(direction).upper() == "UT" else 5
    n = min(_MAX_FLOWLINES, max(1, int(d * per_km)))
    return max(0.01, n * 400 / 1_000_000.0)


# ---------------------------------------------------------------------------
# HTTP helpers.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float) -> bytes:
    """Plain HTTP GET. Raises ``NHDPlusNLDIUpstreamError`` on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise NHDPlusNLDIUpstreamError(
            f"NLDI HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise NHDPlusNLDIUpstreamError(
            f"network error for {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise NHDPlusNLDIUpstreamError(
            f"timed out after {timeout}s for {url}"
        ) from exc


# ---------------------------------------------------------------------------
# Seed resolution.
# ---------------------------------------------------------------------------


def _validate_seed_point(seed: tuple[float, float]) -> None:
    if len(seed) != 2:
        raise NHDPlusNLDIInputError(
            f"seed_point must be (lon, lat); got {seed!r}"
        )
    lon, lat = seed
    if not (math.isfinite(lon) and math.isfinite(lat)):
        raise NHDPlusNLDIInputError(f"seed_point has non-finite values: {seed!r}")
    if not (-180.0 <= lon <= 180.0):
        raise NHDPlusNLDIInputError(f"seed_point lon out of [-180,180]: {lon!r}")
    if not (-90.0 <= lat <= 90.0):
        raise NHDPlusNLDIInputError(f"seed_point lat out of [-90,90]: {lat!r}")
    west, south, east, north = _CONUS_BBOX
    if not (west <= lon <= east and south <= lat <= north):
        raise NHDPlusNLDIInputError(
            f"seed_point {seed} is outside NLDI's CONUS coverage {_CONUS_BBOX}"
        )


def _snap_point_to_comid(seed: tuple[float, float]) -> int:
    """Use NLDI ``/comid/position`` to snap a (lon, lat) to nearest COMID."""
    lon, lat = seed
    url = (
        f"{_NLDI_BASE}/comid/position"
        f"?coords=POINT({lon}%20{lat})"
    )
    body = _http_get(url, timeout=_HTTP_TIMEOUT_S).decode("utf-8", errors="replace")
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise NHDPlusNLDIUpstreamError(
            f"NLDI /comid/position returned non-JSON for {seed}: {exc}"
        ) from exc
    feats = obj.get("features", []) if isinstance(obj, dict) else []
    if not feats:
        raise NHDPlusNLDIEmptyError(
            f"NLDI could not snap {seed} to any NHDPlus reach "
            "(likely offshore or outside the NHDPlus network)"
        )
    props = feats[0].get("properties", {}) if isinstance(feats[0], dict) else {}
    comid = props.get("comid")
    if comid is None:
        # Some NLDI payloads carry the COMID as a feature ``id`` instead.
        comid = feats[0].get("id")
    if comid is None:
        raise NHDPlusNLDIUpstreamError(
            f"NLDI /comid/position response missing comid for {seed}: {feats[0]!r}"
        )
    try:
        return int(comid)
    except (TypeError, ValueError) as exc:
        raise NHDPlusNLDIUpstreamError(
            f"NLDI /comid/position returned non-integer COMID {comid!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Navigation fetch.
# ---------------------------------------------------------------------------


def _navigate_flowlines(
    comid: int, direction: str, distance_km: float,
) -> list[dict[str, Any]]:
    """Call NLDI navigate and return the GeoJSON feature list."""
    url = (
        f"{_NLDI_BASE}/comid/{comid}/navigation/{direction}/flowlines"
        f"?distance={distance_km}"
    )
    body = _http_get(url, timeout=_HTTP_TIMEOUT_S).decode("utf-8", errors="replace")
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise NHDPlusNLDIUpstreamError(
            f"NLDI navigate returned non-JSON for comid={comid} dir={direction}: {exc}"
        ) from exc
    if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
        raise NHDPlusNLDIUpstreamError(
            f"NLDI navigate returned non-FeatureCollection for comid={comid}: "
            f"{type(obj).__name__}"
        )
    feats = obj.get("features", []) or []
    if len(feats) > _MAX_FLOWLINES:
        logger.warning(
            "fetch_nhdplus_nldi_navigate: NLDI returned %d features > cap %d; truncating",
            len(feats), _MAX_FLOWLINES,
        )
        feats = feats[:_MAX_FLOWLINES]
    return feats


# ---------------------------------------------------------------------------
# GeoJSON → FlatGeobuf.
# ---------------------------------------------------------------------------


def _flowlines_to_fgb(features: list[dict[str, Any]]) -> bytes:
    """Convert NLDI flowline LineStrings to a FlatGeobuf byte string."""
    try:
        import geopandas as gpd
    except ImportError as exc:
        raise NHDPlusNLDIUpstreamError(
            f"geopandas unavailable for FlatGeobuf conversion: {exc}"
        ) from exc

    cleaned: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if not isinstance(geom, dict) or geom.get("type") != "LineString":
            continue
        props = feat.get("properties") or {}
        # NLDI carries the COMID as ``nhdplus_comid`` in properties and
        # as ``id`` at the feature level. Promote whichever is available.
        comid = props.get("nhdplus_comid") or props.get("comid") or feat.get("id")
        try:
            comid_int = int(comid) if comid is not None else None
        except (TypeError, ValueError):
            comid_int = None
        cleaned.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "nhdplus_comid": comid_int,
            },
        })

    if not cleaned:
        gdf = gpd.GeoDataFrame(
            {"nhdplus_comid": []},
            geometry=gpd.GeoSeries([], crs="EPSG:4326"),
            crs="EPSG:4326",
        )
    else:
        gdf = gpd.GeoDataFrame.from_features(cleaned, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_nldi_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise NHDPlusNLDIUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} flowlines: {exc}"
            ) from exc
        with open(tmp_fgb, "rb") as f:
            return f.read()
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# End-to-end fetcher.
# ---------------------------------------------------------------------------


def _fetch_navigate_bytes(
    seed_comid: int,
    direction: str,
    distance_km: float,
) -> bytes:
    """Call NLDI navigate + serialize to FlatGeobuf."""
    flowlines = _navigate_flowlines(seed_comid, direction, distance_km)
    if not flowlines:
        raise NHDPlusNLDIEmptyError(
            f"NLDI navigate returned zero flowlines for seed COMID={seed_comid} "
            f"direction={direction} distance_km={distance_km} (network "
            "terminus, stub reach, or distance shorter than the next reach)"
        )
    return _flowlines_to_fgb(flowlines)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_nhdplus_nldi_navigate(
    seed_point: tuple[float, float] | None = None,
    comid: int | None = None,
    direction: Literal["UM", "UT", "DM", "DD"] = "DM",
    distance_km: float = _DEFAULT_DISTANCE_KM,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Walk the NHDPlus stream network from a seed in the requested direction.

    What it does:
        Snaps the seed (a point lon/lat OR a known NHDPlus COMID) to the
        NHDPlus v2.1 channel network, then asks USGS NLDI to enumerate
        the connected flowlines in one of four directions — upstream
        main stem, upstream tributaries, downstream main stem, or
        downstream with diversions — out to the requested distance.
        Returns the connected flowlines as a FlatGeobuf of LineString
        geometries tagged with ``nhdplus_comid``.

    When to use:
        - User asks to "trace water downstream from here" / "find every
          tributary above this gauge" / "show me the river network from
          this confluence to the sea".
        - Post-fire debris-flow / contaminant-plume / dam-break workflow
          needs the downstream reach catalog from a seed point so it can
          score each reach for impact.
        - Watershed scoping: pair this tool's downstream-main-stem (DM)
          output with ``fetch_administrative_boundaries`` /
          ``fetch_noaa_nwm_streamflow`` to bound a reach-level routing
          model.
        - Visualization-only: emit the upstream tributary network (UT)
          as a context layer for narrative answers about a watershed.

    When NOT to use:
        - DO NOT use for raw, unfiltered NHDPlus reach geometry by
          HUC4 — use ``fetch_river_geometry`` (the bulk NHDPlus HR
          fetcher) instead; NLDI navigate is a *traversal* primitive,
          not a bbox-scoped reach dump.
        - DO NOT use for streamflow / discharge values — pair with
          ``fetch_noaa_nwm_streamflow`` (NWM model output keyed on the
          same COMID) or ``fetch_streamflow`` (NWIS gauge observations).
        - DO NOT use for global / non-CONUS hydrography — NLDI / NHDPlus
          v2.1 covers CONUS only. For OUS use a future
          ``fetch_hydrosheds_rivers`` or similar.
        - DO NOT use for catchment / basin polygons — that is the
          NLDI ``/basin`` endpoint, surfaced separately when needed.

    Parameters:
        seed_point: optional ``(lon, lat)`` in EPSG:4326 to snap to the
            nearest NHDPlus COMID via NLDI's ``/comid/position`` endpoint.
            Mutually exclusive with ``comid``. Must fall inside the CONUS
            envelope ``(-130, 20, -60, 55)``. Example for Caloosahatchee
            at Fort Myers: ``(-81.85, 26.55)``.
        comid: optional NHDPlus v2.1 COMID (positive integer) to start
            navigation from directly, skipping the snap step. Use this
            when chaining off another tool that already produced a
            ``nhdplus_comid`` (``fetch_noaa_nwm_streamflow``, NWIS gauge
            resolution, etc.). Example: ``15334434``.
        direction: One of:
            - ``"UM"`` — upstream main stem (trunk only, single branch)
            - ``"UT"`` — upstream tributaries (full upstream subnetwork)
            - ``"DM"`` (default) — downstream main stem (trunk only)
            - ``"DD"`` — downstream including diversions (anastomosing
              splits, distributaries).
        distance_km: traversal cutoff in km along the network from the
            seed. Range ``[0, 1000]``; default ``50.0``. Larger values
            risk hitting the ``_MAX_FLOWLINES=5000`` cap on the response
            (especially with UT) — the payload estimator gates this via
            the Wave-1.5 chat warning.

    Returns:
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket
        ``s3://trid3nt-cache/cache/static-30d/nhdplus_nldi/<key>.fgb``.
        ``layer_type="vector"``, ``role="context"``,
        ``style_preset="nhdplus_flowlines"``, ``units=None``. Geometry
        is LineString in EPSG:4326. Single property per feature:
        ``nhdplus_comid`` (int) — the join key downstream tools (NWM,
        NWIS, NHDPlus VAA tables) consume.

    Rendering: the returned flowlines are a VECTOR layer that renders
    INLINE on the map automatically (the agent surface streams the FlatGeobuf
    as GeoJSON). Do NOT call ``publish_layer`` on this layer — ``publish_layer``
    is raster-only and publishing a vector trips the vector guard. Simply
    return / surface the ``LayerURI`` and it paints on its own.

    Cross-tool dependencies (FR-TA-3):
        - Composes WITH: ``fetch_noaa_nwm_streamflow`` (join NWM discharge
          values to navigated reaches by COMID); ``fetch_streamflow``
          (point NWIS gauges along the traversal).
        - Composes ALONGSIDE: ``fetch_river_geometry`` (bulk NHDPlus
          HR fetch when bbox scope is wanted rather than network
          traversal); ``fetch_fema_nfhl_zones`` (intersect downstream
          flowlines with regulatory flood zones for impact analysis).
        - Upstream data source: USGS NLDI
          (https://api.water.usgs.gov/nldi).

    Cache: ``ttl_class="static-30d"``, ``source_class="nhdplus_nldi"``.
    The NHDPlus topology is multi-year-stable, so a static-30d bucket is
    well amortized.

    Errors (FR-AS-11 typed-error surface):
        - ``NHDPlusNLDIInputError``: missing/both seeds, unknown direction,
          out-of-range distance, seed outside CONUS (retryable=False).
        - ``NHDPlusNLDIUpstreamError``: NLDI 5xx / network error / malformed
          response (retryable=True).
        - ``NHDPlusNLDIEmptyError``: seed valid but no flowlines returned
          (network terminus, stub reach, distance too short) (retryable=False).

    Tier-1 free. No API key. ``supports_global_query=False``.
    """
    if (seed_point is None) == (comid is None):
        raise NHDPlusNLDIInputError(
            "exactly one of seed_point or comid must be provided; got "
            f"seed_point={seed_point!r}, comid={comid!r}"
        )

    direction_up = str(direction).upper().strip()
    if direction_up not in _VALID_DIRECTIONS:
        raise NHDPlusNLDIInputError(
            f"unknown direction={direction!r}; allowed: {sorted(_VALID_DIRECTIONS)}"
        )

    try:
        d_km = float(distance_km)
    except (TypeError, ValueError) as exc:
        raise NHDPlusNLDIInputError(
            f"distance_km must be a finite number; got {distance_km!r}"
        ) from exc
    if not math.isfinite(d_km) or not (_MIN_DISTANCE_KM <= d_km <= _MAX_DISTANCE_KM):
        raise NHDPlusNLDIInputError(
            f"distance_km must be in [{_MIN_DISTANCE_KM}, {_MAX_DISTANCE_KM}]; "
            f"got {d_km!r}"
        )

    # Resolve seed COMID.
    if seed_point is not None:
        if not isinstance(seed_point, tuple):
            try:
                seed_point = tuple(seed_point)  # type: ignore[arg-type]
            except TypeError as exc:
                raise NHDPlusNLDIInputError(
                    f"seed_point must be (lon, lat); got {type(seed_point).__name__}"
                ) from exc
        _validate_seed_point(seed_point)  # type: ignore[arg-type]
        seed_comid = _snap_point_to_comid(seed_point)  # type: ignore[arg-type]
        # Round seed for cache stability.
        cache_seed: dict[str, Any] = {
            "seed_point": [round(seed_point[0], 6), round(seed_point[1], 6)],  # type: ignore[index]
            "comid": None,
        }
    else:
        if not isinstance(comid, int) or comid <= 0:
            raise NHDPlusNLDIInputError(
                f"comid must be a positive integer; got {comid!r}"
            )
        seed_comid = int(comid)
        cache_seed = {"seed_point": None, "comid": seed_comid}

    params: dict[str, Any] = {
        "direction": direction_up,
        "distance_km": round(d_km, 3),
        **cache_seed,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_navigate_bytes(seed_comid, direction_up, d_km),
    )
    assert result.uri is not None, (
        "fetch_nhdplus_nldi_navigate is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=f"nhdplus-nldi-{seed_comid}-{direction_up}-{int(d_km)}km",
        name=(
            f"NHDPlus NLDI navigate — seed COMID {seed_comid}, {direction_up}, "
            f"{d_km:g} km"
        ),
        layer_type="vector",
        uri=result.uri,
        style_preset="nhdplus_flowlines",
        role="context",
        units=None,
    )
