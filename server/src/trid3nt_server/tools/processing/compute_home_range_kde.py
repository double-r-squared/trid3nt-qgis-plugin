"""Atomic tool ``compute_home_range_kde`` — kernel-density home range from tracks.

Takes the POINT FlatGeobuf that ``fetch_movebank_tracks(geometry_type="point")``
emits (one feature per telemetry fix) and computes a **utilization distribution
(UD)** via a 2-D Gaussian kernel-density estimate over the fix locations, then
contours the UD at one or more isopleth percentiles (default 50% core area +
95% home range) into **polygon** features:

    ``compute_home_range_kde(points_uri, isopleths=[50, 95]) -> LayerURI(vector)``

This is the canonical "where does this animal live?" home-range product used in
movement ecology. The 95% isopleth is the conventional home-range boundary; the
50% isopleth is the core-use area (den / nest / foraging core). Each polygon
carries the planimetric ``area_km2`` of that isopleth.

**Why a projected CRS.** Kernel bandwidth and area are meaningless in degrees,
so the points are reprojected to the local UTM zone (metres) before the KDE; the
isopleth polygons are reprojected back to EPSG:4326 for the inline-GeoJSON vector
render path (one ``Polygon``/``MultiPolygon`` per isopleth, ``style_preset=
"home_range_kde"``). The artifact is a FlatGeobuf stored under the FR-DC-3 cache
shim:

    ``s3://<cache-bucket>/cache/static-30d/home_range_kde/<key>.fgb``

**Method (per cache miss):**

1. Read the input FlatGeobuf; extract point geometries (LineString / MultiPoint
   inputs are exploded to their vertices so a track-line layer also works).
2. Optionally filter to a single ``individual_id`` (per-animal home range).
3. Reproject to the local UTM zone (metres).
4. ``scipy.stats.gaussian_kde`` over the (x, y) fixes — Scott's-rule bandwidth by
   default, or a metre-scalar ``bandwidth_m`` override.
5. Evaluate the KDE on a padded ``grid_size`` x ``grid_size`` grid; convert to a
   per-cell probability mass (the UD), normalised to sum 1.
6. For each isopleth percentile P: find the density threshold whose super-level
   set holds P% of the UD volume, contour the UD at that threshold
   (``skimage.measure.find_contours``), and union the rings into the isopleth
   polygon. Area is computed in the projected CRS (km^2).
7. Reproject the isopleth polygons to EPSG:4326 and serialise to FlatGeobuf.

**Honest-empty / typed errors (NFR-R-1 / FR-AS-11).** Too-few points (below the
KDE minimum, or a degenerate co-linear / single-location set that yields a
singular covariance) surface as a typed ``HomeRangeKDEError(TOO_FEW_POINTS)`` —
never a fabricated polygon. Bad isopleth percentiles, an empty input layer, and
unreadable inputs each get their own typed code.

**Cross-cutting invariants:**

- **Invariant 2 (Deterministic workflows): preserves.** Zero LLM calls; the KDE
  is seeded only by the input geometry (Scott's rule is deterministic).
- **FR-DC-6 (cacheable): honors.** ``cacheable=True``, ``ttl_class="static-30d"``,
  ``source_class="home_range_kde"`` — derived from immutable historic tracks.
- **NFR-R-1 (resilience): preserves.** Every failure path raises a typed
  ``HomeRangeKDEError`` with a SCREAMING_SNAKE_CASE ``error_code``.

Pairs with ``fetch_movebank_tracks`` (supplies ``points_uri``) and
``compute_zonal_statistics`` (overlay the home range on a hazard raster).
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import CACHE_BUCKET, read_through, read_object_bytes_s3

__all__ = [
    "compute_home_range_kde",
    "HomeRangeKDEError",
    "estimate_payload_mb",
]

logger = logging.getLogger("trid3nt_server.tools.processing.compute_home_range_kde")


# ---------------------------------------------------------------------------
# Typed error (NFR-R-1 / FR-AS-11)
# ---------------------------------------------------------------------------


class HomeRangeKDEError(RuntimeError):
    """Raised when the KDE home-range computation cannot produce a result.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the pipeline
    strip / function_response envelope.

    Codes:
    - ``NO_POINTS_INPUT``   — neither a readable layer nor any point geometry.
    - ``DOWNLOAD_FAILED``   — the input FlatGeobuf could not be fetched/read.
    - ``EMPTY_LAYER``       — the input layer has zero features.
    - ``TOO_FEW_POINTS``    — fewer than the KDE minimum usable fixes, or a
                              degenerate (co-linear / single-location) set that
                              yields a singular covariance. HONEST empty.
    - ``BAD_ISOPLETH``      — an isopleth percentile is outside (0, 100].
    - ``BAD_BANDWIDTH``     — bandwidth_m is non-positive / non-finite.
    - ``NO_ISOPLETHS``      — no isopleth produced any polygon (all degenerate).
    - ``KDE_FAILED``        — an unexpected failure inside the KDE / contouring.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="compute_home_range_kde",
    ttl_class="static-30d",
    source_class="home_range_kde",
    cacheable=True,
    payload_mb_estimator_name="estimate_payload_mb",
)

#: KDE in 2-D needs strictly more than (ndim + 1) = 3 points for a non-singular
#: covariance; we require a slightly higher floor so the UD is meaningful.
_MIN_POINTS = 5

#: Default isopleth percentiles (core area + home-range boundary).
_DEFAULT_ISOPLETHS: tuple[float, ...] = (50.0, 95.0)

#: Default UD evaluation grid resolution per axis.
_DEFAULT_GRID = 200

#: Hard caps (defensive — a huge grid is O(n_points * grid^2) for the KDE eval).
_MAX_GRID = 600
_MIN_GRID = 32


# ---------------------------------------------------------------------------
# Payload estimator (FR-DC-9 / Wave-1.5 chat-warning gate)
# ---------------------------------------------------------------------------


def estimate_payload_mb(**args: Any) -> float:
    """Estimate the output FlatGeobuf size (MB).

    Output is at most a handful of isopleth polygons (one per requested
    percentile), each a closed ring of a few hundred vertices. That is tiny
    (well under 1 MB) regardless of the input track size, since the KDE
    summarises the fixes into smooth contours. Returns a small constant scaled
    by the isopleth count.
    """
    isopleths = args.get("isopleths")
    try:
        n_iso = len(isopleths) if isopleths is not None else len(_DEFAULT_ISOPLETHS)
    except TypeError:
        n_iso = len(_DEFAULT_ISOPLETHS)
    n_iso = max(1, int(n_iso))
    # ~30 KB per isopleth polygon (generous: a few hundred vertices in FGB).
    return round(0.03 * n_iso, 4)


# ---------------------------------------------------------------------------
# Input download / read
# ---------------------------------------------------------------------------


def _download_points_to_local(points_uri: str) -> str:
    """Return a local file path for the input FlatGeobuf.

    Accepts an ``s3://`` URI (downloaded via the shared boto3 reader) or a
    local path (used directly). Raises ``HomeRangeKDEError(DOWNLOAD_FAILED)``.
    """
    if points_uri.startswith("s3://"):
        try:
            data = read_object_bytes_s3(points_uri)
        except Exception as exc:  # noqa: BLE001
            raise HomeRangeKDEError(
                "DOWNLOAD_FAILED",
                f"S3 download failed for {points_uri!r}: {exc}",
            ) from exc
        suffix = "." + (points_uri.rsplit(".", 1)[-1] if "." in points_uri else "fgb")
        with tempfile.NamedTemporaryFile(
            suffix=suffix, delete=False, prefix="trid3nt_homerange_"
        ) as f:
            f.write(data)
            return f.name
    if not os.path.isfile(points_uri):
        raise HomeRangeKDEError(
            "DOWNLOAD_FAILED",
            f"input points path does not exist: {points_uri!r}",
        )
    return points_uri


def _extract_xy(
    points_uri: str,
    individual_id: str | None,
) -> tuple["Any", str]:
    """Read the input layer and return (Nx2 lon/lat array, label).

    Point geometries are used directly; LineString / MultiPoint / MultiLineString
    inputs are exploded to their vertices (so a track-LINE layer also works).
    Optionally filters to a single ``individual_id`` if that column exists.

    Raises ``HomeRangeKDEError`` (EMPTY_LAYER / NO_POINTS_INPUT) on no usable
    geometry.
    """
    import geopandas as gpd
    import numpy as np

    local = _download_points_to_local(points_uri)
    try:
        gdf = gpd.read_file(local)
    except Exception as exc:  # noqa: BLE001
        raise HomeRangeKDEError(
            "DOWNLOAD_FAILED",
            f"could not read input FlatGeobuf {points_uri!r}: {exc}",
        ) from exc

    if gdf is None or len(gdf) == 0:
        raise HomeRangeKDEError(
            "EMPTY_LAYER",
            f"input layer {points_uri!r} has zero features — no fixes to model.",
        )

    # Filter to a single individual if requested and the column exists.
    label = "all"
    if individual_id is not None:
        if "individual_id" in gdf.columns:
            gdf = gdf[gdf["individual_id"].astype(str) == str(individual_id)]
            label = str(individual_id)
            if len(gdf) == 0:
                raise HomeRangeKDEError(
                    "EMPTY_LAYER",
                    f"no features for individual_id={individual_id!r} in "
                    f"{points_uri!r}.",
                )
        else:
            logger.warning(
                "compute_home_range_kde: input has no individual_id column; "
                "ignoring individual_id=%r filter",
                individual_id,
            )

    # Ensure EPSG:4326 lon/lat for the extracted coordinates.
    if gdf.crs is not None and str(gdf.crs).upper() not in {"EPSG:4326", "WGS84"}:
        gdf = gdf.to_crs("EPSG:4326")

    coords: list[tuple[float, float]] = []
    for geom in gdf.geometry:
        if geom is None or geom.is_empty:
            continue
        gtype = geom.geom_type
        if gtype == "Point":
            coords.append((geom.x, geom.y))
        elif gtype == "MultiPoint":
            for p in geom.geoms:
                coords.append((p.x, p.y))
        elif gtype in ("LineString", "LinearRing"):
            coords.extend((x, y) for x, y in geom.coords)
        elif gtype == "MultiLineString":
            for ls in geom.geoms:
                coords.extend((x, y) for x, y in ls.coords)
        elif gtype in ("Polygon", "MultiPolygon"):
            # Use polygon vertices as a last resort (unusual input).
            polys = [geom] if gtype == "Polygon" else list(geom.geoms)
            for poly in polys:
                coords.extend((x, y) for x, y in poly.exterior.coords)
        # else: GeometryCollection / other -> skip

    if not coords:
        raise HomeRangeKDEError(
            "NO_POINTS_INPUT",
            f"input layer {points_uri!r} yielded no usable point coordinates.",
        )

    arr = np.asarray(coords, dtype=np.float64)
    # Drop non-finite rows.
    finite = np.isfinite(arr).all(axis=1)
    arr = arr[finite]
    if arr.shape[0] == 0:
        raise HomeRangeKDEError(
            "NO_POINTS_INPUT",
            f"input layer {points_uri!r} had only non-finite coordinates.",
        )
    return arr, label


# ---------------------------------------------------------------------------
# Local UTM CRS selection
# ---------------------------------------------------------------------------


def _local_utm_epsg(mean_lon: float, mean_lat: float) -> int:
    """Return the EPSG code of the UTM zone containing (mean_lon, mean_lat)."""
    zone = int((mean_lon + 180.0) / 6.0) + 1
    zone = max(1, min(60, zone))
    base = 32600 if mean_lat >= 0 else 32700
    return base + zone


# ---------------------------------------------------------------------------
# Isopleth thresholding + contouring
# ---------------------------------------------------------------------------


def _isopleth_threshold(ud_grid: "Any", percent: float) -> float:
    """Density threshold whose super-level set holds ``percent``% of the UD mass.

    Standard home-range UD: sort cells descending by mass, accumulate, find the
    cutoff at the cumulative ``percent`` quantile.
    """
    import numpy as np

    flat = ud_grid.ravel()
    order = np.argsort(flat)[::-1]
    cum = np.cumsum(flat[order])
    cutoff_idx = int(np.searchsorted(cum, percent / 100.0))
    cutoff_idx = min(cutoff_idx, len(order) - 1)
    return float(flat[order][cutoff_idx])


def _compute_isopleths(
    xy_proj: "Any",
    isopleths: tuple[float, ...],
    bandwidth_m: float | None,
    grid_size: int,
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Build the UD via gaussian_kde and contour it at each isopleth percentile.

    ``xy_proj`` is an Nx2 array of projected (metre) coordinates. Returns
    ``(records, geoms_proj)`` — one record + one (Multi)Polygon per isopleth that
    produced a polygon, in the projected CRS. Raises
    ``HomeRangeKDEError(TOO_FEW_POINTS)`` on a singular covariance.
    """
    import numpy as np
    from scipy.stats import gaussian_kde
    from skimage import measure
    from shapely.geometry import Polygon, MultiPolygon
    from shapely.ops import unary_union

    xs = xy_proj[:, 0]
    ys = xy_proj[:, 1]
    n = xy_proj.shape[0]

    # Build the KDE. A degenerate (co-linear / coincident) set yields a singular
    # covariance -> honest TOO_FEW_POINTS rather than a fabricated polygon.
    bw_method: Any = None
    if bandwidth_m is not None:
        # Convert an absolute metre bandwidth to gaussian_kde's covariance-factor
        # convention: kde uses factor^2 * cov(data). For an isotropic target
        # std of bandwidth_m we approximate factor = bandwidth_m / std(data).
        data_std = float(np.sqrt(0.5 * (xs.std() ** 2 + ys.std() ** 2)))
        if data_std <= 0.0:
            raise HomeRangeKDEError(
                "TOO_FEW_POINTS",
                "all fixes are at a single location — cannot estimate a kernel "
                "density (zero spatial spread).",
            )
        bw_method = max(1e-6, bandwidth_m / data_std)

    try:
        kde = gaussian_kde(np.vstack([xs, ys]), bw_method=bw_method)
    except np.linalg.LinAlgError as exc:
        raise HomeRangeKDEError(
            "TOO_FEW_POINTS",
            f"the {n} fix(es) lie in a lower-dimensional subspace (co-linear or "
            f"coincident) — a kernel density home range is undefined. {exc}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HomeRangeKDEError(
            "KDE_FAILED", f"gaussian_kde failed: {exc}"
        ) from exc

    # Evaluate on a padded grid.
    span_x = xs.max() - xs.min()
    span_y = ys.max() - ys.min()
    pad_x = 0.20 * span_x if span_x > 0 else 1.0
    pad_y = 0.20 * span_y if span_y > 0 else 1.0
    gx = np.linspace(xs.min() - pad_x, xs.max() + pad_x, grid_size)
    gy = np.linspace(ys.min() - pad_y, ys.max() + pad_y, grid_size)
    mesh_x, mesh_y = np.meshgrid(gx, gy)
    try:
        dens = kde(np.vstack([mesh_x.ravel(), mesh_y.ravel()])).reshape(
            grid_size, grid_size
        )
    except Exception as exc:  # noqa: BLE001
        raise HomeRangeKDEError(
            "KDE_FAILED", f"KDE grid evaluation failed: {exc}"
        ) from exc

    cell_area = float((gx[1] - gx[0]) * (gy[1] - gy[0]))
    ud = dens * cell_area
    total = ud.sum()
    if not np.isfinite(total) or total <= 0:
        raise HomeRangeKDEError(
            "KDE_FAILED", "UD integrates to zero — degenerate density."
        )
    ud = ud / total

    def grid_to_world(row: float, col: float) -> tuple[float, float]:
        x = gx[0] + (col / (grid_size - 1)) * (gx[-1] - gx[0])
        y = gy[0] + (row / (grid_size - 1)) * (gy[-1] - gy[0])
        return x, y

    records: list[dict[str, Any]] = []
    geoms_proj: list[Any] = []
    for pct in isopleths:
        thr = _isopleth_threshold(ud, pct)
        contours = measure.find_contours(ud, level=thr)
        rings: list[Any] = []
        for c in contours:
            if len(c) < 4:
                continue
            world = [grid_to_world(r, col) for r, col in c]
            poly = Polygon(world)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.area <= 0:
                continue
            rings.append(poly)
        if not rings:
            logger.info(
                "compute_home_range_kde: isopleth %.1f%% produced no polygon "
                "(threshold %.3e) — skipping",
                pct,
                thr,
            )
            continue
        merged = unary_union(rings)
        if merged.geom_type == "Polygon":
            merged = MultiPolygon([merged])
        area_km2 = float(merged.area) / 1e6
        records.append(
            {
                "isopleth_pct": float(pct),
                "area_km2": round(area_km2, 4),
                "n_points": int(n),
            }
        )
        geoms_proj.append(merged)

    return records, geoms_proj


# ---------------------------------------------------------------------------
# Core compute (used directly by tests; returns FGB bytes + a summary)
# ---------------------------------------------------------------------------


def _compute_home_range_bytes(
    points_uri: str,
    isopleths: tuple[float, ...],
    bandwidth_m: float | None,
    grid_size: int,
    individual_id: str | None,
) -> tuple[bytes, dict[str, Any]]:
    """Full KDE home-range pipeline -> (FlatGeobuf bytes, summary dict).

    Reads points, reprojects to local UTM, fits the KDE, contours the UD at each
    isopleth, reprojects polygons back to EPSG:4326, and serialises to FGB.
    """
    import geopandas as gpd
    import numpy as np

    lonlat, label = _extract_xy(points_uri, individual_id)
    n = lonlat.shape[0]
    if n < _MIN_POINTS:
        raise HomeRangeKDEError(
            "TOO_FEW_POINTS",
            f"need at least {_MIN_POINTS} telemetry fixes to estimate a kernel "
            f"density home range; got {n}.",
        )

    mean_lon = float(np.mean(lonlat[:, 0]))
    mean_lat = float(np.mean(lonlat[:, 1]))
    epsg_utm = _local_utm_epsg(mean_lon, mean_lat)

    # Project lon/lat -> UTM metres via geopandas (one transform call).
    from shapely.geometry import Point

    pts_gdf = gpd.GeoDataFrame(
        geometry=[Point(lon, lat) for lon, lat in lonlat], crs="EPSG:4326"
    )
    proj = pts_gdf.to_crs(epsg=epsg_utm)
    xy_proj = np.column_stack([proj.geometry.x.values, proj.geometry.y.values])

    records, geoms_proj = _compute_isopleths(
        xy_proj, isopleths, bandwidth_m, grid_size
    )
    if not records:
        raise HomeRangeKDEError(
            "NO_ISOPLETHS",
            "no isopleth produced a polygon — the kernel density was too diffuse "
            "or the grid too coarse to contour.",
        )

    # Annotate records with shared metadata.
    for rec in records:
        rec["individual_id"] = label
        rec["bandwidth_m"] = (
            round(float(bandwidth_m), 2) if bandwidth_m is not None else None
        )

    iso_gdf = gpd.GeoDataFrame(records, geometry=geoms_proj, crs=f"EPSG:{epsg_utm}")
    iso_4326 = iso_gdf.to_crs("EPSG:4326")

    # Compute the 4326 bbox for camera zoom-to.
    minx, miny, maxx, maxy = iso_4326.total_bounds
    bbox_4326 = (float(minx), float(miny), float(maxx), float(maxy))

    out_tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_homerange_out_"
        ) as f:
            out_tmp = f.name
        iso_4326.to_file(out_tmp, driver="FlatGeobuf", engine="pyogrio")
        with open(out_tmp, "rb") as f:
            fgb_bytes = f.read()
    except Exception as exc:  # noqa: BLE001
        raise HomeRangeKDEError(
            "KDE_FAILED", f"FlatGeobuf serialisation failed: {exc}"
        ) from exc
    finally:
        if out_tmp is not None:
            try:
                os.unlink(out_tmp)
            except OSError:
                pass

    summary = {
        "n_points": int(n),
        "individual_id": label,
        "epsg_utm": epsg_utm,
        "isopleths": [
            {"pct": r["isopleth_pct"], "area_km2": r["area_km2"]} for r in records
        ],
        "bbox_4326": list(bbox_4326),
    }
    logger.info(
        "compute_home_range_kde: %d fixes -> %d isopleth(s) %s, %d bytes",
        n,
        len(records),
        summary["isopleths"],
        len(fgb_bytes),
    )
    return fgb_bytes, summary


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_isopleths(isopleths: Any) -> tuple[float, ...]:
    if isopleths is None:
        return _DEFAULT_ISOPLETHS
    if isinstance(isopleths, (int, float)):
        isopleths = [isopleths]
    try:
        vals = [float(v) for v in isopleths]
    except (TypeError, ValueError) as exc:
        raise HomeRangeKDEError(
            "BAD_ISOPLETH",
            f"isopleths must be a list of percentiles in (0, 100]; got "
            f"{isopleths!r}: {exc}",
        ) from exc
    if not vals:
        return _DEFAULT_ISOPLETHS
    for v in vals:
        if not math.isfinite(v) or not (0.0 < v <= 100.0):
            raise HomeRangeKDEError(
                "BAD_ISOPLETH",
                f"isopleth percentile must be in (0, 100]; got {v}.",
            )
    # Sort ascending so the smaller (core) isopleths render under the larger.
    return tuple(sorted(set(vals)))


def _validate_grid(grid_size: Any) -> int:
    try:
        g = int(grid_size)
    except (TypeError, ValueError) as exc:
        raise HomeRangeKDEError(
            "KDE_FAILED", f"grid_size must be an int; got {grid_size!r}: {exc}"
        ) from exc
    return max(_MIN_GRID, min(_MAX_GRID, g))


def _validate_bandwidth(bandwidth_m: Any) -> float | None:
    if bandwidth_m is None:
        return None
    try:
        bw = float(bandwidth_m)
    except (TypeError, ValueError) as exc:
        raise HomeRangeKDEError(
            "BAD_BANDWIDTH",
            f"bandwidth_m must be a positive number of metres or None; got "
            f"{bandwidth_m!r}: {exc}",
        ) from exc
    if not math.isfinite(bw) or bw <= 0.0:
        raise HomeRangeKDEError(
            "BAD_BANDWIDTH",
            f"bandwidth_m must be a positive finite number of metres; got {bw}.",
        )
    return bw


# ---------------------------------------------------------------------------
# Registered atomic tool
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    payload_mb_estimator_name="estimate_payload_mb",
    # Annotations: readOnlyHint=True (reads the input vector; writes only the
    # cache artifact via read_through), openWorldHint=False (pure local compute —
    # no external network call of its own; the upstream fetch is its own tool),
    # destructiveHint=False, idempotentHint=True (deterministic transform — the
    # same points + params always yield the same isopleths).
)
def compute_home_range_kde(
    points_uri: str,
    isopleths: list[float] | float | None = None,
    bandwidth_m: float | None = None,
    grid_size: int = _DEFAULT_GRID,
    individual_id: str | None = None,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Compute a kernel-density home range (utilization-distribution isopleths) from tracking fixes.

    Use this (not ``compute_movement_trajectory``) when the user wants an
    animal's "home range"/"core area"/"utilization distribution"/"50%/95%
    isopleth" from GPS fixes -- fits a 2-D Gaussian KDE over fix locations
    and contours it into home-range polygons (95%=conventional boundary,
    50%=core-use area). Do NOT use for: a minimum-convex-polygon range
    (this is kernel-density, not convex hull); the raw track line
    (``fetch_movebank_tracks(geometry_type="linestring")``).

    Params:
        points_uri: FlatGeobuf of telemetry fixes, typically
            ``fetch_movebank_tracks(..., geometry_type="point").uri``.
        isopleths: percentile(s) to contour, e.g. ``[50, 95]`` (default);
            each in (0, 100].
        bandwidth_m: kernel bandwidth, metres. ``None`` (default) uses
            Scott's rule.
        grid_size: UD grid resolution per axis (default 200, [32, 600]).
        individual_id: restrict to one animal's ``individual_id``; ``None``
            pools all fixes.

    Returns:
        ``LayerURI`` (vector, ``style_preset="home_range_kde"``,
        ``units="km2"``) for isopleth polygons in EPSG:4326, each carrying
        ``isopleth_pct``, ``area_km2``, ``n_points``, ``individual_id``,
        ``bandwidth_m``. Cache bucket, TTL 30d.

    Raises:
        HomeRangeKDEError: NO_POINTS_INPUT/DOWNLOAD_FAILED/EMPTY_LAYER
            (input problems); TOO_FEW_POINTS (below KDE minimum or
            degenerate set -- honest empty, never fabricated);
            BAD_ISOPLETH/BAD_BANDWIDTH (invalid params);
            NO_ISOPLETHS/KDE_FAILED (UD could not be contoured).
    """
    if not isinstance(points_uri, str) or not points_uri.strip():
        raise HomeRangeKDEError(
            "NO_POINTS_INPUT",
            f"points_uri must be a non-empty string; got {points_uri!r}",
        )

    iso = _validate_isopleths(isopleths)
    bw = _validate_bandwidth(bandwidth_m)
    grid = _validate_grid(grid_size)
    ind_id = str(individual_id) if individual_id is not None else None

    effective_bucket = _bucket or CACHE_BUCKET

    # Cache key on the inputs that materially change the output.
    params: dict[str, Any] = {
        "points_uri": points_uri,
        "isopleths": list(iso),
        "grid_size": grid,
    }
    if bw is not None:
        params["bandwidth_m"] = round(bw, 4)
    if ind_id is not None:
        params["individual_id"] = ind_id

    captured: dict[str, Any] = {"summary": None}

    def _fetch() -> bytes:
        fgb_bytes, summary = _compute_home_range_bytes(
            points_uri=points_uri,
            isopleths=iso,
            bandwidth_m=bw,
            grid_size=grid,
            individual_id=ind_id,
        )
        captured["summary"] = summary
        return fgb_bytes

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch,
        bucket=effective_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, (
        "compute_home_range_kde is cacheable; uri must be set by read_through"
    )

    summary = captured["summary"]
    bbox_4326 = tuple(summary["bbox_4326"]) if summary else None
    label = summary["individual_id"] if summary else (ind_id or "all")

    iso_label = "/".join(
        f"{int(p)}" if float(p).is_integer() else f"{p:g}" for p in iso
    )
    name = f"Home range (KDE {iso_label}% UD)"
    if label and label != "all":
        name = f"Home range — {label} (KDE {iso_label}% UD)"

    return LayerURI(
        layer_id=f"home-range-kde-{label}-{iso_label.replace('/', '-')}",
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="home_range_kde",
        role="context",
        units="km2",
        bbox=bbox_4326,
    )
