"""USGS 3DEP DEM fetcher (``fetch_dem``): 3DEP primary with a bounded timeout, Copernicus GLO-30 fallback ladder -> COG via the FR-DC-3 cache shim.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Callable
from typing import Any

import requests

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through
from trid3nt_server.tools.fetchers._fetch_common import (
    FetchError,
    UpstreamAPIError,
    BboxInvalidError,
    _validate_bbox,
    round_bbox_to_resolution,
    _bbox_area_km2,
)

__all__ = [
    "fetch_dem",
    "DemPartialCoverageError",
    "DemPrimaryTimeoutError",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.terrain.fetch_dem")


class DemPartialCoverageError(UpstreamAPIError):
    """3DEP returned a DEM that materially under-covers the requested bbox.

    LANE-C (#159 follow-up #4): the live case fetched a DEM SHORT on the south
    edge, so a correctly-bboxed hillshade re-fetch still under-covered (79% of the
    requested height). 3DEP coverage gaps / edge clipping leave the returned raster
    smaller than the requested extent; without a check we silently mesh / hillshade
    a partial DEM (the honesty floor forbids that).

    Per the data-source-fallback norm this is a TYPED, RETRYABLE upstream signal —
    it subclasses ``UpstreamAPIError`` so the urban workflow's
    ``except Exception`` 1m->10m fallback still fires (the 10m seamless layer
    usually covers where a 1m tile is missing), and the standalone ``fetch_dem``
    tool surfaces the distinct ``error_code`` so the agent narrates the partial
    coverage rather than presenting a silently-clipped terrain layer.
    """

    error_code = "DEM_PARTIAL_COVERAGE"
    retryable = True

class DemPrimaryTimeoutError(UpstreamAPIError):
    """The 3DEP DEM attempt exceeded its hard wall-clock budget.

    2026-07-13 live incident (session 01KXF54K...): USGS 3DEP was down
    ("Service is currently not available") and ``py3dep.get_dem`` ground away
    inside its internal WMS retry loop with NO per-fetch time cap, eating the
    remaining AGENT_TURN_TIMEOUT budget before failing. This typed error is
    raised by ``_fetch_3dep_dem_bytes_bounded`` when the attempt blows the
    ``TRID3NT_DEM_PRIMARY_TIMEOUT_S`` budget (default 90 s) and is treated
    EXACTLY like a 3DEP service failure: in ``source="auto"`` mode it feeds
    the Copernicus GLO-30 fallback ladder; pinned ``source="3dep"`` surfaces
    it with a suggestion to retry on Copernicus.
    """

    error_code = "DEM_PRIMARY_TIMEOUT"
    retryable = True

# ---------------------------------------------------------------------------
# fetch_dem — USGS 3DEP via py3dep
# ---------------------------------------------------------------------------

#: Coverage shortfall (in degrees) tolerated before a DEM is flagged partial.
#: ~0.0008 deg ~= 90 m at the equator — generous enough to absorb a one-tile /
#: half-cell edge snap (3DEP cells are 1-30 m) without flagging a good DEM, but
#: tight enough to catch the live south-edge clip (~21% of the requested height).
_DEM_COVERAGE_TOL_DEG = 0.0008

def _dem_wgs84_bounds(dem: Any) -> tuple[float, float, float, float] | None:
    """Return a rioxarray DEM's bounds reprojected to WGS84, else ``None``.

    ``py3dep.get_dem`` returns an EPSG:5070 (Albers) DataArray; we reproject just
    the bounding box corners to EPSG:4326 so the coverage check compares like with
    like. Returns ``None`` when the bounds / CRS cannot be read (the caller then
    skips the coverage gate rather than blocking a usable DEM).
    """
    rio = getattr(dem, "rio", None)
    if rio is None:
        return None
    left, bottom, right, top = (float(v) for v in rio.bounds())
    crs = rio.crs
    if crs is None:
        return None
    try:
        from pyproj import CRS as _CRS  # type: ignore[import-not-found]

        if _CRS.from_user_input(crs).to_epsg() == 4326:
            return (left, bottom, right, top)
        from pyproj import Transformer  # type: ignore[import-not-found]

        tf = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        xs, ys = tf.transform([left, right, left, right], [bottom, top, top, bottom])
        return (min(xs), min(ys), max(xs), max(ys))
    except Exception:  # noqa: BLE001 — pyproj/CRS slip -> skip the gate
        return None

def _bbox_covers(
    coverage: tuple[float, float, float, float],
    requested: tuple[float, float, float, float],
    tol: float = _DEM_COVERAGE_TOL_DEG,
) -> bool:
    """True iff ``coverage`` spans ``requested`` on all four edges within ``tol``.

    Both are ``(min_lon, min_lat, max_lon, max_lat)`` WGS84. A material shortfall
    on ANY edge (the live south-edge clip) returns False so the caller surfaces a
    typed partial-coverage signal.
    """
    return (
        coverage[0] <= requested[0] + tol
        and coverage[1] <= requested[1] + tol
        and coverage[2] >= requested[2] - tol
        and coverage[3] >= requested[3] - tol
    )

_FETCH_DEM_METADATA = AtomicToolMetadata(
    name="fetch_dem",
    ttl_class="static-30d",
    source_class="dem",
    cacheable=True,
    # Deterministic auto-publish opt-OUT (NATE 2026-06-26): the raw DEM is a
    # pure INTERMEDIATE input that feeds compute_hillshade / compute_slope /
    # compute_aspect / SFINCS setup. The user normally wants the DERIVED terrain
    # product painted, not the bare elevation grid, so do NOT auto-render it.
    # The LLM can still explicitly publish_layer the raw DEM when the user asks
    # to see elevation directly.
    auto_publish=False,
)

# ---------------------------------------------------------------------------
# F16 pattern extended to fetch_dem (2026-07-10): state-scale auto-coarsen.
# ---------------------------------------------------------------------------
# Live failure this fixes: "show me the hillshade in the bounding box" over
# Washington state -> fetch_dem(bbox=<WA state, ~230,638 km^2>, source="3dep",
# resolution_m=30) -> hard "bbox area ... exceeds 10000 km^2 guardrail" dead
# end. A state-scale DEM at a coarsened resolution is a perfectly reasonable
# request (fine for a hillshade/overview render); commit 21cd123 already gave
# fetch_landcover this exact treatment (hard cap raised to a continent
# ceiling + pixel-budget auto-coarsen instead of a flat area cutoff). This
# block mirrors that pattern for fetch_dem's default source="3dep" path.
#
# Acquisition-path note (why the coarsen is just "pass a bigger resolution_m"
# here, unlike fetch_landcover's WCS WIDTH/HEIGHT dance): fetch_dem's 3DEP
# path calls ``py3dep.get_dem(bbox, resolution=resolution_m)`` directly --
# py3dep's own ``resolution`` argument already controls the delivered grid
# spacing (it picks the fast ``static_3dep_dem`` tile-tree path at 10/30/60 m
# and falls back to a WMS ``get_map`` mosaic + reproject/resample at any other
# value). Coarsening is therefore just requesting a bigger ``resolution_m``;
# py3dep/GDAL's own reprojection resampling applies (area-weighted / bilinear
# for a continuous field), which is CORRECT for elevation -- UNLIKE NLCD land
# cover, which must use nearest-neighbor to keep discrete class codes intact.
# We never touch pixel values ourselves; we only choose what resolution to
# request.
#
# Continent ceiling mirrors fetch_landcover's 5,000,000 km^2 hard cap exactly
# (still hard-fails: no auto-coarsen rescues a whole-continent request).
_DEM_CONTINENT_CEILING_KM2 = 5_000_000.0

# Pixel-budget constant for the auto-coarsen (same long-axis/4000px budget
# fetch_landcover uses against the MRLC WCS 4096 px/axis server cap). py3dep
# has no equivalent hard server-side px cap, but an unbounded grid at fine
# resolution over a huge AOI is still an enormous mosaic/COG to materialize
# and hold in memory -- 4000 px/axis keeps it tractable. Kept in step with
# server.py's ``_FETCH_MAX_PX_BY_TOOL["fetch_dem"]`` so the resolution-gate's
# suggested rung matches what this tool will actually deliver (server.py
# point 5 of the F16-for-DEM extension).
_DEM_PIXEL_BUDGET_PX = 4000

# Absolute floor on the coarsen math: 3DEP's finest tiles (lidar-derived) run
# down to ~1 m. UNLIKE fetch_landcover's fixed 30 m NLCD native grid, 3DEP has
# no single "native" resolution -- coverage varies by tile (1-3 m lidar
# patches in many areas, a 10 m national baseline, 30 m fallback elsewhere)
# -- so this is a sanity floor guarding a degenerate resolution_m < 1 request,
# NOT a native-resolution constant to clamp UP to (that would break the
# tool's existing fine 1 m / 3 m site-scale requests on small AOIs).
_DEM_FINEST_RES_FLOOR_M = 1

def _fetch_3dep_dem_bytes(
    bbox: tuple[float, float, float, float], resolution_m: int
) -> bytes:
    """Call ``py3dep.get_dem`` and serialize the result as a Cloud-Optimized GeoTIFF.

    Raises ``UpstreamAPIError`` on any failure from the 3DEP service so the
    cache shim's "no sentinel on failure" contract surfaces a typed error.
    """
    # py3dep + rasterio import lazily so test environments without these
    # heavy geo deps installed can still load the registry.
    try:
        import py3dep  # type: ignore[import-not-found]
        import rioxarray  # noqa: F401 — registers .rio accessor on xr.DataArray
    except Exception as exc:  # noqa: BLE001
        raise UpstreamAPIError(f"py3dep / rioxarray unavailable: {exc}") from exc

    # job-0306: py3dep reads the USGS 3DEP seamless DEM from the PUBLIC bucket
    # ``prd-tnm.s3.amazonaws.com`` via GDAL ``/vsicurl/``. On the AWS box the
    # instance-role AWS creds are in the environment, so GDAL tried to SIGN the
    # request (and to readdir-list the bucket) — both fail on a public,
    # no-ListBucket bucket, surfacing as "…USGS_Seamless_DEM_1.vrt does not
    # exist in the file system" even though the VRT is reachable (curl 200).
    # Cold DEM fetches for EVERY novel bbox failed (live Case 3, 2026-06-16);
    # only previously-cached DEMs worked. Scope AWS_NO_SIGN_REQUEST +
    # readdir/extension hints to THIS read via ``rasterio.Env`` so the agent's
    # PRIVATE-bucket access (signed instance-role boto3/GDAL) is unaffected.
    try:
        import rasterio  # type: ignore[import-not-found]
        _dem_env = rasterio.Env(
            AWS_NO_SIGN_REQUEST="YES",
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".vrt,.tif,.tiff",
            VSI_CACHE=True,
        )
    except Exception:  # noqa: BLE001 — rasterio always present where py3dep is
        import contextlib
        _dem_env = contextlib.nullcontext()

    try:
        with _dem_env:
            dem = py3dep.get_dem(bbox, resolution=resolution_m)
    except Exception as exc:  # noqa: BLE001 — re-raise as typed error
        raise UpstreamAPIError(
            f"py3dep.get_dem failed for bbox={bbox} resolution={resolution_m}: {exc}"
        ) from exc

    # LANE-C (#159 follow-up #4): coverage gate. 3DEP can return a DEM SHORT on an
    # edge (the live south-edge clip -> 79% height hillshade). Reproject the
    # returned raster's bounds back to WGS84 and assert they span the requested
    # bbox within a small tolerance; a material shortfall raises the typed
    # DemPartialCoverageError so we never silently mesh / hillshade a clipped DEM
    # (the urban workflow's 1m->10m fallback + the agent's honest narration act on
    # it). Best-effort on the bounds read — a bounds-introspection failure leaves
    # the prior (no-check) behavior unchanged rather than blocking a good DEM.
    try:
        cov = _dem_wgs84_bounds(dem)
    except Exception:  # noqa: BLE001 — never block a DEM on an introspection slip
        cov = None
    if cov is not None and not _bbox_covers(cov, bbox):
        raise DemPartialCoverageError(
            f"3DEP DEM for bbox={bbox} resolution={resolution_m}m under-covers the "
            f"requested extent (got coverage {cov}); the returned raster is "
            "materially short on at least one edge."
        )

    # Serialize to a COG via rioxarray's to_raster. We round-trip through a
    # temp file because rasterio's MemoryFile lacks COG driver options on
    # some platforms; the temp file is small for a small bbox.
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        # COG driver + LZW compression. tiled=True is COG-required.
        dem.rio.to_raster(
            tmp_path,
            driver="COG",
            compress="LZW",
            BIGTIFF="IF_SAFER",
        )
        with open(tmp_path, "rb") as f:
            data = f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return data

# ---------------------------------------------------------------------------
# 3DEP fail-fast budget + Copernicus fallback ladder (2026-07-13 live incident).
# ---------------------------------------------------------------------------
# Live failure this fixes (session 01KXF54K..., 3DEP down with "Service is
# currently not available"): (A) py3dep.get_dem has internal retries and no
# per-fetch time cap, so a dead 3DEP service grinds until the turn dies on
# AGENT_TURN_TIMEOUT; (B) fetch_dem had NO fallback despite the norm
# (primary -> fallback -> honest typed error) and Copernicus GLO-30 being a
# keyless global 30 m alternative already implemented in this codebase.

#: Env override for the hard wall-clock budget (seconds) on the 3DEP attempt.
_DEM_PRIMARY_TIMEOUT_ENV = "TRID3NT_DEM_PRIMARY_TIMEOUT_S"

_DEM_PRIMARY_TIMEOUT_DEFAULT_S = 90.0

#: ``source`` spellings that PIN Copernicus GLO-30 (no 3DEP attempt).
_DEM_SOURCE_COPERNICUS_ALIASES = frozenset(
    {"copernicus", "cop-dem-glo-30", "glo-30", "glo30", "copernicus_glo30"}
)

#: ``source`` spellings that PIN USGS 3DEP (explicit request: NO silent
#: cross-source fallback; a service failure surfaces with a suggestion to
#: retry on Copernicus instead).
_DEM_SOURCE_3DEP_PIN_ALIASES = frozenset(
    {"3dep", "usgs", "usgs-3dep", "usgs_3dep", "usgs3dep", "3dep_seamless"}
)

def _dem_primary_timeout_s() -> float:
    """Wall-clock budget (s) for the 3DEP attempt; env-overridable, default 90."""
    raw = os.environ.get(_DEM_PRIMARY_TIMEOUT_ENV, "")
    try:
        val = float(raw)
        if val > 0:
            return val
    except (TypeError, ValueError):
        pass
    return _DEM_PRIMARY_TIMEOUT_DEFAULT_S

def _fetch_3dep_dem_bytes_bounded(
    bbox: tuple[float, float, float, float],
    resolution_m: int,
    timeout_s: float,
) -> bytes:
    """Run ``_fetch_3dep_dem_bytes`` under a hard wall-clock budget.

    ``fetch_dem`` is one of the ``_ALWAYS_OFFLOAD_SYNC_TOOLS`` -- the whole tool
    body already runs on an ``asyncio.to_thread`` worker, so the budget is
    enforced here with a nested DAEMON thread + ``join(timeout)`` rather than
    ``asyncio.wait_for`` (there is no loop in this thread to await on).

    Timeout semantics (dangling-thread safety): on expiry the worker thread is
    ABANDONED and its eventual result/exception is written only into the
    thread-local ``box`` dict that nothing else reads -- it is discarded, never
    surfaced, and never reaches shared state. In particular the cache write
    inside ``read_through`` cannot happen for a timed-out attempt, because
    ``read_through`` only writes when its ``fetch_fn`` RETURNS and this
    ``fetch_fn`` RAISES ``DemPrimaryTimeoutError`` instead. The daemon flag
    keeps an in-flight py3dep grind from blocking interpreter shutdown.
    """
    import threading

    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            # Late-bound module-global lookup so test monkeypatching of
            # ``_fetch_3dep_dem_bytes`` keeps working through the wrapper.
            box["data"] = _fetch_3dep_dem_bytes(bbox, resolution_m)
        except BaseException as exc:  # noqa: BLE001 -- carried to the caller
            box["exc"] = exc

    worker = threading.Thread(
        target=_runner, name="fetch-dem-3dep-bounded", daemon=True
    )
    worker.start()
    worker.join(timeout=timeout_s)
    if worker.is_alive():
        raise DemPrimaryTimeoutError(
            f"USGS 3DEP attempt exceeded the {timeout_s:.0f}s wall-clock budget "
            f"(env {_DEM_PRIMARY_TIMEOUT_ENV}) for bbox={bbox} "
            f"resolution={resolution_m}m; treating as a 3DEP service failure. "
            "The in-flight attempt was abandoned and its result discarded."
        )
    if "exc" in box:
        raise box["exc"]
    return box["data"]

def _short_exc(exc: BaseException, limit: int = 220) -> str:
    """One-line, length-clipped exception text for honest fallback notes."""
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."

@register_tool(
    _FETCH_DEM_METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (USGS 3DEP py3dep),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_dem(
    bbox: tuple[float, float, float, float],
    resolution_m: int = 10,
    source: str = "auto",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch a digital elevation model (DEM) / terrain elevation for a bounding box (USGS 3DEP first with automatic Copernicus GLO-30 fallback; either source pinnable).

    Use this (not ``fetch_topobathy``, which is coastal land+seafloor) for a plain
    ground-elevation DEM. The default ``source="auto"`` tries USGS 3DEP (US, 10 m)
    first and, if the 3DEP SERVICE is unavailable / times out, automatically falls
    back to Copernicus GLO-30 (keyless GLOBAL 30 m) for the same bbox -- the
    returned layer name and ``fallback_note`` say so honestly (GLO-30 data is
    never presented as 3DEP). Prefer omitting ``source`` (or passing "auto");
    pass ``source="copernicus"`` for terrain OUTSIDE the US (the Alps, Andes,
    Himalaya, Africa, ...) -- that ABSORBS the former ``fetch_copernicus_dem``
    (keyless global GLO-30 30 m). Pass ``source="3dep"`` ONLY to pin 3DEP: a
    pinned source never silently switches (a 3DEP outage then surfaces a typed
    error suggesting Copernicus).

    **What it does:** Downloads a Cloud-Optimized GeoTIFF of ground elevation
    from the USGS 3D Elevation Program (3DEP) via the ``py3dep`` library and
    writes it to the 30-day cache. Returns a ``LayerURI`` pointing at the
    cached COG so downstream SFINCS/HydroMT setup and terrain analysis tools
    can consume it without re-fetching. With ``source="copernicus"`` it instead
    mosaics the global Copernicus GLO-30 30 m DEM (same ``continuous_dem`` ramp,
    same ``LayerURI`` raster contract).

    **When to use:**
    - Any flood workflow step that needs terrain elevation: SFINCS model
      domain setup, watershed delineation, slope/hillshade computation.
    - User asks "show me the terrain elevation for [area]" or "what does the
      ground look like here?" — render with the ``continuous_dem`` QML preset.
    - ``build_sfincs_model`` requires a DEM for the SFINCS grid; this tool
      supplies it.
    - Pre-processing step before ``compute_slope``, ``compute_hillshade``,
      ``compute_aspect``, or ``compute_zonal_statistics``.

    **When NOT to use:**
    - Coverage outside the continental US with the DEFAULT source — 3DEP is
      CONUS-only; pass ``source="copernicus"`` for a global GLO-30 30 m DEM.
    - Bathymetry (below-water elevation) — 3DEP/GLO-30 are land/surface models;
      use ``fetch_topobathy`` for coastal seafloor depth.
    - Single-point elevation lookups — the tool fetches a raster window;
      for a point query use a future ``point_elevation`` tool.
    - Continent-scale bboxes (> 5,000,000 km²) — rejected with
      ``BboxInvalidError``. State/multi-state bboxes auto-coarsen instead of
      failing (not a dead end).

    **Parameters:**
    - ``bbox`` (tuple[float,float,float,float]): ``(min_lon, min_lat, max_lon,
      max_lat)`` in EPSG:4326. Max area 5,000,000 km²; large bboxes auto-coarsen.
    - ``resolution_m`` (int, default 10): DEM grid spacing in meters (3DEP only).
      10 m or 30 m are fastest on 3DEP's tile tree. A large bbox may deliver a
      coarser grid than requested (a pixel-budget auto-coarsen); the delivered
      spacing is stamped into the result.
    - ``source`` (str, default ``"auto"``): ``"auto"`` (USGS 3DEP first,
      automatic Copernicus GLO-30 fallback on a 3DEP service failure/timeout --
      honestly labeled); ``"3dep"`` (PIN USGS 3DEP, US-only, honors
      ``resolution_m``, no cross-source fallback); or ``"copernicus"`` (PIN
      Copernicus GLO-30, global 30 m, keyless -- delegates to the folded-in
      ``fetch_copernicus_dem``).

    **Returns:**
    A ``LayerURI`` pointing at a Cloud-Optimized GeoTIFF in the cache bucket
    (``s3://trid3nt-cache/cache/static-30d/dem/<key>.tif``).
    CRS: EPSG:5070 (py3dep default); units: meters above NAVD88.
    Fields consumed downstream: ``uri`` → by ``build_sfincs_model`` and QGIS
    Server WMS; ``style_preset="continuous_dem"`` → map rendering. When the
    bbox forced a coarser grid than requested, ``name`` carries an honest
    coarsening note (approximate terrain -- fine for a hillshade/overview,
    not site-scale analysis).

    **Cross-tool dependencies:**
    - Downstream: ``build_sfincs_model``, ``compute_slope``,
      ``compute_hillshade``, ``compute_aspect``, ``compute_colored_relief``,
      ``compute_zonal_statistics``.
    - Typically called after: ``geocode_location`` supplies the bbox.
    - Sibling source: ``source="copernicus"`` for non-US / global terrain.
    """
    # SOURCE consolidation: the global Copernicus GLO-30 path is folded in as a
    # source mode (the former fetch_copernicus_dem). Same LayerURI raster
    # contract + continuous_dem ramp; the impl lives in the copernicus module.
    src = source.strip().lower() if isinstance(source, str) else "auto"
    if src in _DEM_SOURCE_COPERNICUS_ALIASES:
        from trid3nt_server.tools.fetchers.terrain.fetch_copernicus_dem import _copernicus_dem_impl

        return _copernicus_dem_impl(bbox)

    # Pin semantics (2026-07-13 fallback ladder): an EXPLICIT source="3dep" is
    # honored with no silent cross-source fallback; anything else ("auto", the
    # default, or an unrecognized spelling) is the 3DEP-primary auto-ladder.
    pinned_3dep = src in _DEM_SOURCE_3DEP_PIN_ALIASES

    # Continent ceiling (F16-for-DEM, 2026-07-10): mirrors fetch_landcover's
    # 5,000,000 km^2 hard cap. Below this, auto-coarsen instead of hard-fail
    # -- see the module-level comment above _DEM_CONTINENT_CEILING_KM2.
    requested_res = int(resolution_m)
    rough_area = _bbox_area_km2(bbox)
    if rough_area > _DEM_CONTINENT_CEILING_KM2:
        raise BboxInvalidError(
            f"bbox area {rough_area:.1f} km^2 exceeds the "
            f"{_DEM_CONTINENT_CEILING_KM2:,.0f} km^2 hard ceiling for fetch_dem "
            "(continent-scale; split into sub-regions)."
        )

    # Pixel-budget auto-coarsen: if the requested resolution would put more
    # than _DEM_PIXEL_BUDGET_PX pixels on the bbox's long axis, coarsen to
    # fit. effective_res is the coarser of (a) what the caller/gate asked
    # for and (b) what the pixel budget allows -- it is NEVER finer than
    # requested_res, so a small-bbox site-scale request (e.g. 1 m) is
    # honored exactly as before (byte-identical for the common case).
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lon = 111_320.0 * max(0.05, math.cos(math.radians(mid_lat)))
    long_axis_m = max(
        (max_lon - min_lon) * m_per_deg_lon,
        (max_lat - min_lat) * 111_320.0,
    )
    budget_res = int(math.ceil(long_axis_m / _DEM_PIXEL_BUDGET_PX))
    effective_res = max(_DEM_FINEST_RES_FLOOR_M, requested_res, budget_res)
    downsampled = effective_res > requested_res

    # Quantize to the EFFECTIVE resolution grid -- a coarsened fetch snaps to
    # a coarser grid than a native-resolution fetch of the same bbox would,
    # so the two never collide on the same cache key (matches fetch_landcover).
    quantized = round_bbox_to_resolution(bbox, effective_res)
    params = {"bbox": list(quantized), "resolution_m": effective_res}
    timeout_s = _dem_primary_timeout_s()
    try:
        result = read_through(
            metadata=_FETCH_DEM_METADATA,
            params=params,
            ext="tif",
            fetch_fn=lambda: _fetch_3dep_dem_bytes_bounded(
                quantized, effective_res, timeout_s
            ),
        )
    except DemPartialCoverageError:
        # DATA-coverage signal, not service health: 3DEP responded but the
        # raster under-covers the bbox. Existing typed consumers (the urban
        # workflow's 1m->10m ladder, the agent's honest narration) act on it;
        # propagate unchanged rather than folding into the service ladder.
        raise
    except UpstreamAPIError as primary_exc:
        # SERVICE failure (unavailable / 5xx / DemPrimaryTimeoutError budget
        # blow). User errors (BboxInvalidError) raised above never reach here.
        if pinned_3dep:
            pinned_err = UpstreamAPIError(
                f"USGS 3DEP DEM fetch failed for bbox={quantized} "
                f"resolution={effective_res}m: {_short_exc(primary_exc)} -- "
                "source='3dep' was explicitly requested, so no cross-source "
                "fallback was attempted. If 3DEP is down, retry with "
                "source='copernicus' (global Copernicus GLO-30, 30 m) or "
                "source='auto' (3DEP first, GLO-30 fallback)."
            )
            # Structured recovery options: summarize_tool_result surfaces a
            # ``suggestions`` list so the model relays real options verbatim.
            pinned_err.suggestions = [  # type: ignore[attr-defined]
                "Retry with source='copernicus' (global GLO-30, 30 m).",
                "Retry with source='auto' to allow the automatic fallback.",
            ]
            raise pinned_err from primary_exc
        logger.warning(
            "fetch_dem: 3DEP primary failed (%s); attempting Copernicus "
            "GLO-30 fallback for bbox=%s",
            _short_exc(primary_exc),
            bbox,
        )
        from trid3nt_server.tools.fetchers.terrain.fetch_copernicus_dem import _copernicus_dem_impl

        try:
            cop_layer = _copernicus_dem_impl(bbox)
        except Exception as cop_exc:  # noqa: BLE001 -- typed both-failed error
            both_err = UpstreamAPIError(
                f"DEM fetch failed on BOTH sources for bbox={bbox} -- "
                f"USGS 3DEP (primary): {_short_exc(primary_exc)}; "
                f"Copernicus GLO-30 (fallback): {_short_exc(cop_exc)}. "
                "No elevation data was fetched."
            )
            raise both_err from cop_exc
        # HONESTY FLOOR: never present GLO-30 data as 3DEP. The name suffix
        # reaches the layer list + the LLM's repr summary; fallback_note is the
        # structured field (LayerURI additive contract, 2026-07-13).
        res_note = (
            ""
            if requested_res == 30
            else (
                f" GLO-30 is fixed 30 m, so the requested {requested_res} m "
                "3DEP resolution does not apply."
            )
        )
        return cop_layer.model_copy(
            update={
                "name": cop_layer.name + " (Copernicus GLO-30 -- 3DEP unavailable)",
                "fallback_note": (
                    "Automatic source fallback: USGS 3DEP (primary) was "
                    f"unavailable ({_short_exc(primary_exc)}), so this DEM is "
                    "Copernicus GLO-30 30 m data for the same bbox."
                    + res_note
                    + " Do not present this layer as 3DEP."
                ),
            }
        )
    assert result.uri is not None, "fetch_dem is cacheable; uri must be set"
    name = f"USGS 3DEP DEM ({effective_res}m)"
    if downsampled:
        # Honest coarsening note (Invariant 7 -- no silent wrong answers).
        # LayerURI is a FROZEN contract (extra="forbid", see fetch_landcover's
        # "Sidecar shape" comment above) with ~7 internal Python call sites
        # (fetch_topobathy, compute_contours, model_flood_scenario,
        # run_elmfire, model_landslide_scenario, model_dambreak_geoclaw_
        # scenario, model_urban_flood_swmm) plus 50+ tests depending on
        # fetch_dem returning a bare LayerURI -- unlike fetch_landcover (few
        # callers, already dict-shaped pre-F16), converting fetch_dem to a
        # dict-sidecar return here would be a wide, high-risk blast radius for
        # no functional gain: LayerURI IS fully JSON-serialized back to the
        # LLM (see server.py result handling), so folding the effective /
        # native resolution + the honesty note into ``name`` (and the
        # resolution into ``layer_id`` below) reaches the LLM/user exactly the
        # same as a separate dict field would.
        name += (
            f", coarsened from {requested_res}m -- large-AOI pixel budget. "
            "Terrain detail is approximate at this scale: fine for a "
            "hillshade/overview render, not for site-scale analysis."
        )
    return LayerURI(
        layer_id=f"dem-{quantized[0]:.4f}-{quantized[1]:.4f}-{effective_res}m",
        name=name,
        layer_type="raster",
        uri=result.uri,
        style_preset="continuous_dem",
        role="input",
        units="meters",
        # LANE-C (#159 follow-up #4): declare the requested extent on the layer.
        # The coverage gate in ``_fetch_3dep_dem_bytes`` guarantees the raster
        # spans this bbox (or raised), so stamping it lets the AOI-pin reuse
        # short-circuit + the post-result zoom-to know the DEM's intended extent.
        bbox=quantized,
    )
