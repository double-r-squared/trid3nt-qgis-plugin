"""USGS 3DEP DEM delegate hooks: the ``fetch_dem`` fold.

``fetch_dem`` folds onto the router as a ``library_delegate`` raster source: the
maintained ``py3dep`` library owns 3DEP discovery + the socket, so the router
DELEGATES the one network step to :func:`read_dem` and keeps params / gates /
cache / stamps / typed-errors. The bespoke DEM behaviour the declarative surface
cannot express lives here as four hooks:

  * ``dem_3dep.validate`` (delegate_validate) -- the continent-ceiling hard cap +
    the auto-path US out-of-coverage pre-flight (both twin-identical), raised
    pre-cache / pre-network.
  * ``dem_3dep.coarsen`` (pre_resolve) -- the pixel-budget auto-coarsen: recompute
    the effective resolution + re-quantize the bbox to that coarser grid BEFORE
    read_through so the cache key keys on the delivered grid (twin behaviour). The
    original requested resolution rides ``requested_res_m`` ONLY when coarsening
    happened, so a non-coarsened request keeps the twin's exact ``{bbox,
    resolution_m}`` cache key byte-for-byte.
  * ``dem_3dep.read`` (delegate) -- ``py3dep.get_dem`` under a hard wall-clock
    watchdog + the reproject-bounds partial-coverage gate + the SOURCE-CONDITIONAL
    error gating (auto -> DemAutoFallbackGateError; pinned 3dep -> a plain suggesting
    UpstreamAPIError). Returns ``(array, transform, crs)`` for the shared COG writer.
  * ``dem_3dep.envelope`` -- the ``dem-{lon}-{lat}-{Nm}`` layer_id + ``USGS 3DEP DEM
    (Nm)`` name with the coarsen stamp (the router's build_layer_uri hardcodes
    ``{source_class}-{variable}``; this is the only naming override seam).

The ``source="copernicus"`` leg is NOT here -- it is the spec's cross-sibling
``dispatch`` block, served verbatim from ``fetch_copernicus_dem``
before this pipeline runs.

The ``Dem*Error`` twins live HERE (their stable importable home now that the
coded ``fetch_dem`` module is deleted): they are ``UpstreamAPIError`` subclasses
carrying PINNED ``error_code``s, and ``library_delegate.invoke`` passes any
``FetchError`` through unchanged so those codes survive the delegate
wrapper verbatim.
"""

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
from . import register_hook

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.hooks.dem_3dep"
)

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


# --------------------------------------------------------------------------- #
# Typed DEM errors (the stable home after the coded twin's deletion).
# --------------------------------------------------------------------------- #


class DemPartialCoverageError(UpstreamAPIError):
    """3DEP returned a DEM that materially under-covers the requested bbox.

    3DEP coverage gaps / edge clipping leave the returned raster smaller than the
    requested extent (the live south-edge clip -> 79% height hillshade); without a
    check we would silently mesh / hillshade a partial DEM (the honesty floor
    forbids that). A TYPED, RETRYABLE upstream signal: it subclasses
    ``UpstreamAPIError`` so the urban workflow's ``except Exception`` 1m->10m
    fallback still fires, and the standalone tool surfaces the distinct
    ``error_code`` so the agent narrates the partial coverage. This is a DATA
    signal, not a service-health one -- it PROPAGATES, it does NOT drive the
    cross-dataset fallback gate.
    """

    error_code = "DEM_PARTIAL_COVERAGE"
    retryable = True


class DemPrimaryTimeoutError(UpstreamAPIError):
    """The 3DEP DEM attempt exceeded its hard wall-clock budget.

    ``py3dep.get_dem`` exposes no timeout arg and grinds inside its own WMS retry
    loop with no per-fetch cap; on a 3DEP outage it eats the whole turn budget.
    This is raised when the bounded watchdog blows ``TRID3NT_DEM_PRIMARY_TIMEOUT_S``
    (default 90 s) and is treated EXACTLY like a 3DEP service failure (drives the
    auto gate; the pinned ``source="3dep"`` path surfaces it suggesting Copernicus).
    """

    error_code = "DEM_PRIMARY_TIMEOUT"
    retryable = True


class DemAutoFallbackGateError(UpstreamAPIError):
    """3DEP failed on the auto path; a Copernicus swap needs USER approval.

    NORM (IDEAS.md "Loud, user-gated cross-dataset fallbacks"): 3DEP (US, 1-10 m
    LIDAR) -> Copernicus GLO-30 (global, 30 m RADAR) is a DIFFERENT measurement
    method at a COARSER resolution; swapping it silently degrades map integrity
    while looking like success. On a 3DEP SERVICE failure (outage / 5xx / timeout
    budget blow) the ``source="auto"`` path raises THIS typed retryable error,
    which (a) states 3DEP failed and why, (b) NAMES the substitute as an explicit
    retry ``source="copernicus"``, (c) states the tradeoff plainly -- and rides the
    tool-retry loop (``summarize_tool_result`` surfaces ``.suggestions``) so the
    USER approves the swap conversationally.
    """

    error_code = "DEM_FALLBACK_GATE"
    retryable = True


class DemOutOfCoverageError(UpstreamAPIError):
    """The requested bbox lies outside USGS 3DEP's coverage (US) entirely.

    A pre-flight envelope check catches a clearly NON-US AOI BEFORE the 3DEP
    attempt, so the user gets an immediate DISTINCT "3DEP has no coverage there"
    error naming ``source="copernicus"`` rather than waiting out a guaranteed-miss
    attempt or reading the outage gate. Kept distinct from
    ``DemAutoFallbackGateError`` (3DEP covers the AOI but the SERVICE failed). The
    envelope is deliberately GENEROUS so a border-straddling bbox falls through to
    a real 3DEP attempt, never a false out-of-coverage error.
    """

    error_code = "DEM_OUT_OF_COVERAGE"
    retryable = True


# --------------------------------------------------------------------------- #
# Constants (twin-identical).
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Pure geometry helpers (twin-identical).
# --------------------------------------------------------------------------- #


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
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


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


# --------------------------------------------------------------------------- #
# The 3DEP array fetch (the monkeypatchable network seam) + bounded watchdog.
# --------------------------------------------------------------------------- #


def _fetch_3dep_dem_array(
    bbox: tuple[float, float, float, float], resolution_m: int
) -> tuple[Any, Any, Any]:
    """Call ``py3dep.get_dem``, run the coverage gate, return ``(array, transform, crs)``.

    Raises ``UpstreamAPIError`` on any 3DEP service failure and
    ``DemPartialCoverageError`` when the returned raster materially under-covers the
    requested bbox. The array is nodata-masked to NaN (the pfdf_3dep pattern) so the
    shared COG writer serializes NaN-nodata; the EPSG:5070 array / transform / CRS
    are re-encoded by ``array_to_cog_bytes`` (the accepted divergence class,
    same array / CRS / nodata as the twin's ``rio.to_raster`` COG).
    """
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
    """Run :func:`_fetch_3dep_dem_array` under a hard wall-clock budget.

    ``py3dep`` exposes no timeout, so the budget is enforced with a DAEMON thread +
    ``join(timeout)``. On expiry the worker is ABANDONED (its eventual result/exc is
    written only to the local ``box`` and discarded -- never reaches the cache, since
    this ``fetch_fn`` RAISES instead of returning). The daemon flag keeps an in-flight
    grind from blocking interpreter shutdown.
    """
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


# --------------------------------------------------------------------------- #
# HOOK: delegate_validate -- continent ceiling + auto-path out-of-coverage.
# --------------------------------------------------------------------------- #


@register_hook("dem_3dep.validate")
def validate_dem(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Pre-cache DEM input gate (twin-identical): continent ceiling then out-of-coverage.

    Runs in ``route()`` AFTER type/gate validation and BEFORE read_through. Raises
    ``BboxInvalidError`` (the continent-scale hard cap, the twin's exact message) or
    ``DemOutOfCoverageError`` (a clearly non-US AOI on the AUTO path only) -- both
    pre-network, offline-testable, and both propagate through ``pre_validate``
    unwrapped.
    """
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


# --------------------------------------------------------------------------- #
# HOOK: pre_resolve -- pixel-budget auto-coarsen (pre-cache-key).
# --------------------------------------------------------------------------- #


@register_hook("dem_3dep.coarsen")
def coarsen_dem(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Pixel-budget auto-coarsen: return the coarsened ``bbox`` + effective ``resolution_m``.

    If the requested resolution would put > ``_DEM_PIXEL_BUDGET_PX`` pixels on the
    bbox's long axis, coarsen to fit. ``effective_res`` is NEVER finer than
    requested, so a small-bbox site-scale request is byte-identical. The bbox is
    re-quantized to the EFFECTIVE grid so a coarsened fetch never collides on the
    cache key with a native fetch of the same bbox (twin behaviour). ``requested_res_m``
    is returned ONLY when coarsening actually happened -- so a non-coarsened request
    keeps the twin's exact ``{bbox, resolution_m}`` cache key byte-for-byte, and the
    envelope hook reads it back to stamp the honest coarsening note.
    """
    requested_res = int(params["resolution_m"])
    min_lon, min_lat, max_lon, max_lat = tuple(float(v) for v in params["bbox"])
    mid_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lon = 111_320.0 * max(0.05, math.cos(math.radians(mid_lat)))
    long_axis_m = max(
        (max_lon - min_lon) * m_per_deg_lon,
        (max_lat - min_lat) * 111_320.0,
    )
    budget_res = int(math.ceil(long_axis_m / _DEM_PIXEL_BUDGET_PX))
    effective_res = max(_DEM_FINEST_RES_FLOOR_M, requested_res, budget_res)
    quantized = round_bbox_to_resolution((min_lon, min_lat, max_lon, max_lat), effective_res)
    out: dict[str, Any] = {"bbox": list(quantized), "resolution_m": effective_res}
    if effective_res > requested_res:
        out["requested_res_m"] = requested_res
    return out


# --------------------------------------------------------------------------- #
# HOOK: delegate -- py3dep read + bounded watchdog + source-conditional gating.
# --------------------------------------------------------------------------- #


@register_hook("dem_3dep.read")
def read_dem(spec: SourceSpec, params: dict[str, Any], *, timeout_s: float) -> tuple[Any, Any, Any]:
    """Read a 3DEP DEM via py3dep and return ``(array, transform, crs)``.

    The wall-clock budget is the twin's env-tunable ``TRID3NT_DEM_PRIMARY_TIMEOUT_S``
    (default 90 s); the spec's ``ingest.delegate.timeout_s`` is a nominal outer bound
    the delegate wrapper passes but the DEM watchdog owns its own budget (twin-faithful,
    honoring the test env override). SOURCE-CONDITIONAL gating on a SERVICE failure:
      * partial coverage -> propagates (DATA signal, not a fallback trigger);
      * pinned source="3dep" -> a plain suggesting UpstreamAPIError (no fallback);
      * auto -> DemAutoFallbackGateError (loud, user-gated cross-dataset swap).
    Every raised error is a ``FetchError`` so ``library_delegate.invoke`` passes its
    pinned code through unchanged.
    """
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


# --------------------------------------------------------------------------- #
# HOOK: envelope -- the layer_id / name naming override (+ coarsen stamp).
# --------------------------------------------------------------------------- #


@register_hook("dem_3dep.envelope")
def envelope_dem(
    spec: SourceSpec, params: dict[str, Any], layer: Any, data: bytes | None
) -> dict[str, Any]:
    """Override the emitted ``layer_id`` + ``name`` to the twin's exact forms.

    ``dem-{lon:.4f}-{lat:.4f}-{Nm}`` and ``USGS 3DEP DEM (Nm)``, plus the honest
    pixel-budget coarsen stamp when ``requested_res_m`` shows the delivered grid is
    coarser than requested. Pure (reads only the already-resolved params); the
    router strips uri/layer_type so this can only enrich, never re-point the layer.
    """
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
