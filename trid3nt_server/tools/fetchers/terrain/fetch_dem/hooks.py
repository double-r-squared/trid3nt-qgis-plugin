"""USGS 3DEP DEM delegate hooks.

The maintained library owns 3DEP discovery and the socket, so the router delegates that
one network step and keeps params, gates, cache, stamps and typed errors. Four hooks
carry the DEM behaviour the declarative surface cannot express."""

# ``validate`` is the continent-ceiling hard cap plus the auto-path out-of-coverage
# pre-flight, raised pre-cache and pre-network.
#
# ``coarsen`` is the pixel-budget auto-coarsen: it recomputes the effective resolution
# and re-quantizes the bbox to that coarser grid BEFORE read_through, so the cache key
# keys on the DELIVERED grid. The original requested resolution rides
# ``requested_res_m`` ONLY when coarsening happened, so a non-coarsened request keeps
# the plain ``{bbox, resolution_m}`` key.
#
# ``read`` runs the library call under a hard wall-clock watchdog, gates partial
# coverage on the reprojected bounds, and gates its errors on the SOURCE: an automatic
# request raises the fallback gate, while a pinned one raises a plain suggesting
# upstream error.
#
# ``envelope`` is the only naming override seam, because the router's own layer builder
# hardcodes ``{source_class}-{variable}``.
#
# The ``source="copernicus"`` leg is NOT here: it is the spec's cross-sibling dispatch,
# served verbatim from its sibling before this pipeline runs. The ``Dem*Error`` classes
# live HERE, carrying PINNED codes that survive the delegate wrapper's passthrough.

from __future__ import annotations

import logging
import math
import os
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._fetch_common import (
    BboxInvalidError,
    UpstreamAPIError,
    _bbox_area_km2,
    round_bbox_to_resolution,
)
from ..._router.hooks import register_hook

logger = logging.getLogger(__name__)

__all__ = [
    "DemPartialCoverageError",
    "DemPrimaryTimeoutError",
    "DemAutoFallbackGateError",
    "DemOutOfCoverageError",
    "validate_dem",
    "coarsen_dem",
    "read_dem",
    "envelope_dem",
]




class DemPartialCoverageError(UpstreamAPIError):
    """3DEP returned a DEM that materially UNDER-COVERS the requested bbox. A DATA
    signal, not a service-health one: it PROPAGATES and does NOT drive the
    cross-dataset fallback gate."""

    # Coverage gaps and edge clipping leave the returned raster smaller than the
    # requested extent, and without this check a partial DEM would silently be meshed or
    # hillshaded. It stays an ``UpstreamAPIError`` subclass so a caller's coarse-retry
    # fallback still fires, while the distinct code lets the surface narrate the gap.

    error_code = "DEM_PARTIAL_COVERAGE"
    retryable = True


class DemPrimaryTimeoutError(UpstreamAPIError):
    """The DEM attempt exceeded its hard wall-clock budget, and is treated EXACTLY like a
    service failure. The library exposes no timeout and grinds inside its own retry loop
    with no per-fetch cap, so on an outage it would eat the whole turn budget."""

    error_code = "DEM_PRIMARY_TIMEOUT"
    retryable = True


class DemAutoFallbackGateError(UpstreamAPIError):
    """3DEP failed on the AUTO path, and the coarser global substitute needs USER
    approval. The error names what failed, names the substitute as an explicit retry,
    and states the tradeoff, so the swap is approved conversationally."""

    # A LIDAR product at 1-10 m and a RADAR one at 30 m are a DIFFERENT measurement
    # method at a coarser resolution, so swapping them silently degrades map integrity
    # while looking like success. Only a SERVICE failure -- an outage, a 5xx, a blown
    # timeout budget -- reaches here.

    error_code = "DEM_FALLBACK_GATE"
    retryable = True


class DemOutOfCoverageError(UpstreamAPIError):
    """The requested bbox lies outside 3DEP's US coverage entirely, caught pre-flight so
    a guaranteed miss is not waited out. Kept DISTINCT from the service-failure gate,
    and the envelope is GENEROUS so a border-straddling bbox still gets a real try."""

    error_code = "DEM_OUT_OF_COVERAGE"
    retryable = True



#: Coverage shortfall (deg) tolerated before a DEM is flagged partial (~90 m).
_DEM_COVERAGE_TOL_DEG = 0.0008

#: Continent ceiling (mirrors fetch_landcover); above this a bbox hard-fails.
_DEM_CONTINENT_CEILING_KM2 = 5_000_000.0

#: Pixel-budget long-axis cap for the auto-coarsen (matches fetch_landcover).
_DEM_PIXEL_BUDGET_PX = 4000

#: Absolute floor on the coarsen math (3DEP's finest lidar tiles ~1 m).
_DEM_FINEST_RES_FLOOR_M = 1

#: Env override + default for the hard wall-clock budget on the 3DEP attempt.
_DEM_PRIMARY_TIMEOUT_ENV = "TRID3NT_DEM_PRIMARY_TIMEOUT_S"
_DEM_PRIMARY_TIMEOUT_DEFAULT_S = 90.0

#: ``source`` spellings that PIN USGS 3DEP (no cross-source fallback). Copernicus
#: spellings are dispatched away pre-flight by the spec's ``dispatch`` block, so
#: they never reach these hooks; anything NOT in this set (incl. "auto", the
#: default, and any unrecognized spelling) is the 3DEP-primary AUTO path.
_DEM_SOURCE_3DEP_PIN_ALIASES = frozenset(
    {"3dep", "usgs", "usgs-3dep", "usgs_3dep", "usgs3dep", "3dep_seamless"}
)

#: The 3DEP -> Copernicus tradeoff, stated plainly (shared by both gate errors).
_DEM_COPERNICUS_TRADEOFF = (
    "The substitute would be Copernicus GLO-30: a keyless GLOBAL 30 m DEM derived "
    "from RADAR (TanDEM-X). That is a DIFFERENT measurement method at a COARSER "
    "resolution than 3DEP's 1-10 m LIDAR-derived terrain -- coarser detail and a "
    "radar-vs-lidar surface, fine for a hillshade / overview but not for site-scale "
    "terrain analysis."
)

#: Generous US super-envelopes 3DEP plausibly covers (CONUS / AK incl. the
#: antimeridian Aleutian tail / HI / PR-USVI). A bbox intersecting ANY box is
#: treated in-coverage; only a bbox intersecting NONE is out-of-coverage -- so a
#: border-straddler can only DOWNGRADE to the outage gate, never a false success.
_US_3DEP_COVERAGE_ENVELOPES: tuple[tuple[float, float, float, float], ...] = (
    (-125.0, 24.0, -66.5, 49.5),   # CONUS
    (-170.0, 51.0, -129.0, 72.0),  # mainland + southeast Alaska
    (172.0, 51.0, 180.0, 54.0),    # Aleutian tail across the antimeridian
    (-161.0, 18.0, -154.0, 23.0),  # Hawaii
    (-68.0, 17.0, -64.0, 19.0),    # Puerto Rico / USVI
)




def _src(params: dict[str, Any]) -> str:
    raw = params.get("source", "auto")
    return raw.strip().lower() if isinstance(raw, str) else "auto"


def _pinned_3dep(params: dict[str, Any]) -> bool:
    return _src(params) in _DEM_SOURCE_3DEP_PIN_ALIASES


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


def _short_exc(exc: BaseException, limit: int = 220) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _bbox_intersects(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    """True iff the two WGS84 bboxes overlap; a touching edge counts."""
    from shapely.geometry import box

    return box(*a).intersects(box(*b))


def _bbox_in_us_coverage(bbox: tuple[float, float, float, float]) -> bool:
    return any(_bbox_intersects(bbox, env) for env in _US_3DEP_COVERAGE_ENVELOPES)


def _dem_wgs84_bounds(dem: Any) -> tuple[float, float, float, float] | None:
    """A rioxarray DEM's bounds reprojected to WGS84, else ``None`` (skip the gate)."""
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
    except Exception:  # noqa: BLE001 -- pyproj/CRS slip -> skip the gate
        return None


def _bbox_covers(
    coverage: tuple[float, float, float, float],
    requested: tuple[float, float, float, float],
    tol: float = _DEM_COVERAGE_TOL_DEG,
) -> bool:
    """True iff ``coverage`` spans ``requested`` on all four edges within ``tol``."""
    return (
        coverage[0] <= requested[0] + tol
        and coverage[1] <= requested[1] + tol
        and coverage[2] >= requested[2] - tol
        and coverage[3] >= requested[3] - tol
    )




def _fetch_3dep_dem_array(
    bbox: tuple[float, float, float, float], resolution_m: int
) -> tuple[Any, Any, Any]:
    """Call the library, run the coverage gate, and return ``(array, transform, crs)``
    with nodata masked to NaN for the shared COG writer. A service failure raises
    upstream; a raster that materially under-covers the bbox raises partial coverage."""
    try:
        import py3dep  # type: ignore[import-not-found]
        import rioxarray  # noqa: F401 -- registers the .rio accessor
    except Exception as exc:  # noqa: BLE001
        raise UpstreamAPIError(f"py3dep / rioxarray unavailable: {exc}") from exc

    # AWS_NO_SIGN_REQUEST + readdir/extension hints scoped to THIS public-bucket
    # read (prd-tnm.s3.amazonaws.com); the agent's private-bucket boto3/GDAL is
    # unaffected. On the AWS box the instance-role creds would else sign a
    # public no-ListBucket read and fail ("...vrt does not exist").
    try:
        import rasterio  # type: ignore[import-not-found]
        _dem_env = rasterio.Env(
            AWS_NO_SIGN_REQUEST="YES",
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".vrt,.tif,.tiff",
            VSI_CACHE=True,
        )
    except Exception:  # noqa: BLE001 -- rasterio always present where py3dep is
        import contextlib
        _dem_env = contextlib.nullcontext()

    try:
        with _dem_env:
            dem = py3dep.get_dem(bbox, resolution=resolution_m)
    except Exception as exc:  # noqa: BLE001 -- re-raise as typed service error
        raise UpstreamAPIError(
            f"py3dep.get_dem failed for bbox={bbox} resolution={resolution_m}: {exc}"
        ) from exc

    # Coverage gate: 3DEP can return a DEM SHORT on an edge. Reproject the returned
    # bounds back to WGS84 and assert they span the request; a material shortfall
    # raises the typed partial-coverage signal (never silently mesh a clipped DEM).
    try:
        cov = _dem_wgs84_bounds(dem)
    except Exception:  # noqa: BLE001 -- never block a DEM on an introspection slip
        cov = None
    if cov is not None and not _bbox_covers(cov, bbox):
        raise DemPartialCoverageError(
            f"3DEP DEM for bbox={bbox} resolution={resolution_m}m under-covers the "
            f"requested extent (got coverage {cov}); the returned raster is "
            "materially short on at least one edge."
        )

    import numpy as np

    arr = np.asarray(dem.values, dtype="float32")
    if arr.ndim == 3 and arr.shape[0] == 1:
        arr = arr[0]
    rio = dem.rio
    transform = rio.transform()
    crs = rio.crs
    nod = getattr(rio, "nodata", None)
    if nod is not None and not (isinstance(nod, float) and math.isnan(nod)):
        try:
            arr = np.where(arr == float(nod), np.nan, arr).astype("float32")
        except (TypeError, ValueError):
            pass
    return arr, transform, crs


def _fetch_3dep_dem_array_bounded(
    bbox: tuple[float, float, float, float],
    resolution_m: int,
    timeout_s: float,
) -> tuple[Any, Any, Any]:
    """Run the read under a hard wall-clock budget, enforced with a DAEMON thread and a
    join timeout because the library exposes none. On expiry the worker is ABANDONED:
    its eventual result is discarded and never reaches the cache, since this raises."""
    import threading

    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            # Late-bound module-global lookup so test monkeypatching of
            # ``_fetch_3dep_dem_array`` keeps working through the wrapper.
            box["data"] = _fetch_3dep_dem_array(bbox, resolution_m)
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




@register_hook("dem_3dep.validate")
def validate_dem(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Pre-cache DEM input gate: the continent-scale hard cap, then the out-of-coverage
    check on the AUTO path only. Both run AFTER type validation and BEFORE
    read_through, so both are pre-network and testable offline."""
    bbox = tuple(float(v) for v in params["bbox"])
    rough_area = _bbox_area_km2(bbox)
    if rough_area > _DEM_CONTINENT_CEILING_KM2:
        raise BboxInvalidError(
            f"bbox area {rough_area:.1f} km^2 exceeds the "
            f"{_DEM_CONTINENT_CEILING_KM2:,.0f} km^2 hard ceiling for fetch_dem "
            "(continent-scale; split into sub-regions)."
        )

    # OUT-OF-COVERAGE: a clearly non-US AOI cannot be served by 3DEP; on the AUTO
    # path fail FAST with a DISTINCT typed error naming copernicus rather than
    # burning the 90 s budget on a guaranteed miss. Off the pinned source="3dep"
    # path (the user chose 3DEP; its own outage error already suggests copernicus).
    if not _pinned_3dep(params) and not _bbox_in_us_coverage(bbox):
        oob_err = DemOutOfCoverageError(
            f"USGS 3DEP has no coverage for bbox={bbox}: 3DEP is US-only (CONUS, "
            "Alaska, Hawaii, PR/USVI) and this AOI falls outside it. No 3DEP "
            'attempt was made. Retry with source="copernicus" for the global '
            f"GLO-30 30 m DEM. {_DEM_COPERNICUS_TRADEOFF}"
        )
        oob_err.suggestions = [  # type: ignore[attr-defined]
            'Retry with source="copernicus" (global GLO-30 30 m) for this '
            "non-US AOI.",
        ]
        raise oob_err




@register_hook("dem_3dep.coarsen")
def coarsen_dem(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Pixel-budget auto-coarsen, returning the coarsened bbox and effective resolution.
    The effective resolution is NEVER finer than requested, and the bbox is re-quantized
    to the DELIVERED grid so a coarsened fetch cannot collide with a native one."""

    # ``requested_res_m`` is returned ONLY when coarsening actually happened, so a
    # non-coarsened request keeps the plain ``{bbox, resolution_m}`` cache key, and the
    # envelope hook reads it back to stamp the honest coarsening note.
    requested_res = int(params["resolution_m"])
    min_lon, min_lat, max_lon, max_lat = tuple(float(v) for v in params["bbox"])
    mid_lat = 0.5 * (min_lat + max_lat)
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    long_axis_m = max(
        geod.inv(min_lon, mid_lat, max_lon, mid_lat)[2],
        geod.inv(min_lon, min_lat, min_lon, max_lat)[2],
    )
    budget_res = int(math.ceil(long_axis_m / _DEM_PIXEL_BUDGET_PX))
    effective_res = max(_DEM_FINEST_RES_FLOOR_M, requested_res, budget_res)
    quantized = round_bbox_to_resolution((min_lon, min_lat, max_lon, max_lat), effective_res)
    out: dict[str, Any] = {"bbox": list(quantized), "resolution_m": effective_res}
    if effective_res > requested_res:
        out["requested_res_m"] = requested_res
    return out




@register_hook("dem_3dep.read")
def read_dem(spec: SourceSpec, params: dict[str, Any], *, timeout_s: float) -> tuple[Any, Any, Any]:
    """Read a 3DEP DEM and return ``(array, transform, crs)``. The DEM watchdog owns its
    own env-tunable budget; the spec's delegate timeout is a nominal outer bound."""

    # Gating on a SERVICE failure is SOURCE-CONDITIONAL: partial coverage propagates, a
    # data signal rather than a fallback trigger; a pinned source raises a plain
    # suggesting upstream error with no fallback; the auto path raises the loud
    # user-gated cross-dataset swap. Every raise is a FetchError, so the delegate
    # wrapper passes its pinned code through unchanged.
    bbox = tuple(float(v) for v in params["bbox"])
    resolution_m = int(params["resolution_m"])
    pinned = _pinned_3dep(params)
    budget = _dem_primary_timeout_s()
    try:
        return _fetch_3dep_dem_array_bounded(bbox, resolution_m, budget)
    except DemPartialCoverageError:
        # DATA-coverage signal (3DEP responded but under-covers) -- propagate
        # unchanged; existing typed consumers act on it, never the service ladder.
        raise
    except UpstreamAPIError as primary_exc:
        # SERVICE failure (unavailable / 5xx / DemPrimaryTimeoutError budget blow).
        if pinned:
            pinned_err = UpstreamAPIError(
                f"USGS 3DEP DEM fetch failed for bbox={bbox} "
                f"resolution={resolution_m}m: {_short_exc(primary_exc)} -- "
                "source='3dep' was explicitly requested, so no cross-source "
                "fallback was attempted. If 3DEP is down, retry with "
                "source='copernicus' (global Copernicus GLO-30, 30 m) or "
                "source='auto' (3DEP first, GLO-30 fallback)."
            )
            pinned_err.suggestions = [  # type: ignore[attr-defined]
                "Retry with source='copernicus' (global GLO-30, 30 m).",
                "Retry with source='auto' to allow the automatic fallback.",
            ]
            raise pinned_err from primary_exc
        # GATED cross-dataset fallback: the auto path NEVER silently swaps.
        logger.warning(
            "fetch_dem: 3DEP primary failed (%s) for bbox=%s -- raising the "
            "user-gated Copernicus-fallback error (no silent cross-dataset swap)",
            _short_exc(primary_exc),
            bbox,
        )
        gate_err = DemAutoFallbackGateError(
            f"USGS 3DEP DEM fetch failed for bbox={bbox} "
            f"resolution={resolution_m}m: {_short_exc(primary_exc)}. No dataset "
            "was substituted automatically -- switching to a different dataset is a "
            'user decision. Retry with source="copernicus" to use the global '
            f"GLO-30 30 m DEM instead, or retry with source=\"3dep\" once USGS 3DEP "
            f"is back. {_DEM_COPERNICUS_TRADEOFF}"
        )
        gate_err.suggestions = [  # type: ignore[attr-defined]
            'Retry with source="copernicus" to substitute the global GLO-30 30 m '
            "DEM (coarser 30 m radar, not 1-10 m lidar).",
            'Retry with source="3dep" once USGS 3DEP recovers to keep 1-10 m '
            "lidar terrain.",
        ]
        raise gate_err from primary_exc




@register_hook("dem_3dep.envelope")
def envelope_dem(
    spec: SourceSpec, params: dict[str, Any], layer: Any, data: bytes | None
) -> dict[str, Any]:
    """Build the emitted ``layer_id`` and ``name``, plus the honest coarsen stamp when
    ``requested_res_m`` shows the delivered grid is coarser than asked. Pure over the
    resolved params, and the router strips the identity keys, so it can only enrich."""
    bbox = tuple(float(v) for v in params["bbox"])
    effective_res = int(params["resolution_m"])
    requested_res = params.get("requested_res_m")
    name = f"USGS 3DEP DEM ({effective_res}m)"
    if requested_res is not None and int(requested_res) != effective_res:
        name += (
            f", coarsened from {int(requested_res)}m -- large-AOI pixel budget. "
            "Terrain detail is approximate at this scale: fine for a "
            "hillshade/overview render, not for site-scale analysis."
        )
    layer_id = f"dem-{bbox[0]:.4f}-{bbox[1]:.4f}-{effective_res}m"
    return {"layer_id": layer_id, "name": name}
