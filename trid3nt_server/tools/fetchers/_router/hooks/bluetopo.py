"""NOAA BlueTopo delegate hooks: the ``fetch_bluetopo`` AOI -> tiles -> merge.

BlueTopo is NOAA's National Bathymetric Source compiled surface for
navigationally significant US waters. It is BATHYMETRY ONLY -- there is no land
in it -- and it is published on NAVD88, an orthometric datum rather than a
navigational or tidal one, which is what lets it merge with a NAVD88 land DEM
with no vertical transformation.

The bespoke step this delegate owns is DISCOVERY: the bucket's tile scheme is a
GeoPackage of tile polygons carrying each delivered tile's GeoTIFF link, its
resolution tier and its UTM zone. That file is the coverage truth -- a tile row
with no link is a tile the programme defined and has not delivered -- so the AOI
is intersected against it rather than against any assumption about the grid. The
warp-merge onto one target grid is NOT bespoke and is not reimplemented here:
the coastal composite's per-source reprojecting compositor is called directly.

  * ``bluetopo.validate`` -- bbox finiteness + the US envelope, raised pre-cache
    / pre-network as a ``BlueTopoInputError``.
  * ``bluetopo.read`` -- tile-scheme select + per-tile NAVD88 gate + merge ->
    ``(array, transform, crs)`` for the shared COG writer, recording the
    fetch-time provenance (datum, tiles, tiers, measured coverage) on the
    provenance channel so it survives a cache hit.
  * ``bluetopo.envelope`` -- the layer_id / name and the provenance fields read
    back from the channel into a ``BlueTopoResult``.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import time
from typing import Any

from trid3nt_server.tools.cache import record_provenance
from trid3nt_server.tools.fetchers._public_s3 import public_s3_client

from ..._fetch_common import FetchError
from . import register_hook

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.hooks.bluetopo"
)

__all__ = [
    "BlueTopoError",
    "BlueTopoInputError",
    "BlueTopoUpstreamError",
    "BlueTopoDatumError",
    "BlueTopoCoverageGapError",
    "BLUETOPO_BUCKET",
    "BLUETOPO_TILE_SCHEME_PREFIX",
    "TARGET_CRS",
    "latest_tile_scheme_key",
    "select_bluetopo_tiles",
    "assert_navd88_tile",
    "validate_bluetopo",
    "read_bluetopo",
    "envelope_bluetopo",
]


# ---------------------------------------------------------------------------
# Typed errors (the A.6 codes the router preserves through the delegate wrapper).
# ---------------------------------------------------------------------------


class BlueTopoError(FetchError):
    """Base class for fetch_bluetopo failures."""

    error_code: str = "BLUETOPO_ERROR"
    retryable: bool = True


class BlueTopoInputError(BlueTopoError):
    """Bad inputs (bbox shape, non-finite numbers, outside the US envelope)."""

    error_code = "BLUETOPO_INPUT_INVALID"
    retryable = False


class BlueTopoUpstreamError(BlueTopoError):
    """Tile-scheme listing / download / tile read / merge failure."""

    error_code = "BLUETOPO_UPSTREAM_ERROR"
    retryable = True


class BlueTopoDatumError(BlueTopoError):
    """A selected tile does not state NAVD88.

    BlueTopo publishes NAVD88 and says so in the tile's own vertical CRS and in
    its ``VERTICALDATUMWKT`` tag. A tile that says otherwise is refused rather
    than merged onto a NAVD88 bed, because a silent cross-datum merge is exactly
    the substitution the correct-data-class law forbids.
    """

    error_code = "BLUETOPO_DATUM_MISMATCH"
    retryable = False


class BlueTopoCoverageGapError(BlueTopoError):
    """No delivered BlueTopo tile intersects the AOI.

    BlueTopo concentrates on navigationally significant water, so an AOI over a
    minor creek or an undeveloped bay legitimately has none. The terminal answer
    is this refusal naming the gap, never a surface built from something else.
    """

    error_code = "BLUETOPO_COVERAGE_GAP"
    retryable = False


# ---------------------------------------------------------------------------
# Constants -- each one a MEASURED fact about the live bucket, not a guess.
# ---------------------------------------------------------------------------

#: The AWS Open Data bucket, anonymous read, us-east-1.
BLUETOPO_BUCKET = "noaa-ocs-nationalbathymetry-pds"

#: Where the tile scheme GeoPackage lives. The programme republishes it under a
#: timestamped name, so the key is DISCOVERED by listing rather than pinned.
BLUETOPO_TILE_SCHEME_PREFIX = "BlueTopo/_BlueTopo_Tile_Scheme/"

#: Public https base for the objects the tile scheme's links point at.
BLUETOPO_HTTPS_ROOT = f"https://{BLUETOPO_BUCKET}.s3.amazonaws.com/"

#: Band 1 of a BlueTopo tile. The three bands are Elevation, Uncertainty and
#: Contributor; only the first is a bed.
_ELEVATION_BAND = 1

#: Output CRS. BlueTopo tiles are per-UTM-zone NAD83; the merge normalises onto
#: one grid the way every other topo-bathy consumer in the tree expects.
TARGET_CRS = "EPSG:32616"

#: US envelope (incl. AK, HI and the territories BlueTopo covers) -- a coarse
#: pre-screen so a foreign bbox fails before the tile scheme is downloaded.
_US_BBOX: tuple[float, float, float, float] = (-180.0, -15.0, -64.0, 72.0)

#: How long a downloaded tile scheme is reused within one process. The file is
#: ~6.5 MB and is republished on the programme's own cadence, not per request.
_SCHEME_MEMO_TTL_S = 3600.0

#: Ceiling on the tiles one AOI may merge. A request that wants more is asking
#: for a regional mosaic, and the honest answer is to say so rather than to
#: spend an unbounded number of reads discovering it.
_MAX_TILES = 64

_scheme_memo: tuple[float, str] | None = None


# ---------------------------------------------------------------------------
# Discovery: the tile scheme is the coverage truth.
# ---------------------------------------------------------------------------


def latest_tile_scheme_key(*, timeout_s: float = 60.0) -> str:
    """The newest tile-scheme GeoPackage key under the scheme prefix.

    Listed rather than pinned: the key carries a publish timestamp, so a pinned
    one goes stale silently and a stale scheme reports coverage that has moved.
    """
    client = public_s3_client()
    try:
        resp = client.list_objects_v2(
            Bucket=BLUETOPO_BUCKET, Prefix=BLUETOPO_TILE_SCHEME_PREFIX
        )
    except Exception as exc:  # noqa: BLE001 -- listing is the upstream
        raise BlueTopoUpstreamError(
            f"could not list the BlueTopo tile scheme prefix "
            f"s3://{BLUETOPO_BUCKET}/{BLUETOPO_TILE_SCHEME_PREFIX}: {exc}"
        ) from exc
    keys = [
        str(o["Key"]) for o in (resp.get("Contents") or [])
        if str(o.get("Key", "")).lower().endswith(".gpkg")
    ]
    if not keys:
        raise BlueTopoUpstreamError(
            f"no tile-scheme GeoPackage under s3://{BLUETOPO_BUCKET}/"
            f"{BLUETOPO_TILE_SCHEME_PREFIX} -- BlueTopo coverage cannot be resolved"
        )
    return sorted(keys)[-1]


def _tile_scheme_path(*, timeout_s: float) -> str:
    """A local copy of the newest tile scheme, memoized for this process."""
    global _scheme_memo
    now = time.monotonic()
    if _scheme_memo is not None and now - _scheme_memo[0] < _SCHEME_MEMO_TTL_S:
        if os.path.exists(_scheme_memo[1]):
            return _scheme_memo[1]

    key = latest_tile_scheme_key(timeout_s=timeout_s)
    client = public_s3_client()
    with tempfile.NamedTemporaryFile(
        suffix=".gpkg", delete=False, prefix="trid3nt_bluetopo_scheme_"
    ) as fh:
        path = fh.name
    try:
        client.download_file(BLUETOPO_BUCKET, key, path)
    except Exception as exc:  # noqa: BLE001
        raise BlueTopoUpstreamError(
            f"could not download the BlueTopo tile scheme {key}: {exc}"
        ) from exc
    _scheme_memo = (now, path)
    logger.info("fetch_bluetopo: tile scheme %s staged at %s", key, path)
    return path


def select_bluetopo_tiles(
    bbox: tuple[float, float, float, float], *, timeout_s: float = 120.0
) -> tuple[list[dict[str, Any]], float]:
    """Tiles whose footprint intersects the AOI, plus the MEASURED covered share.

    Returns ``(rows, coverage_fraction)`` where each row carries the tile id, its
    https link, its resolution tier and its UTM zone. Rows the scheme defines but
    has NOT delivered (no GeoTIFF link) are dropped: a defined tile is not data.

    The share is measured against the tile scheme's OWN polygons, so it is what
    the programme says it covers rather than what a read happened to paint.
    """
    import geopandas as gpd
    from shapely.geometry import box

    path = _tile_scheme_path(timeout_s=timeout_s)
    aoi = box(*bbox)
    try:
        frame = gpd.read_file(path, bbox=bbox)
    except Exception as exc:  # noqa: BLE001
        raise BlueTopoUpstreamError(
            f"could not read the BlueTopo tile scheme at {path}: {exc}"
        ) from exc

    rows: list[dict[str, Any]] = []
    covered = []
    for _, row in frame.iterrows():
        link = row.get("GeoTIFF_Link")
        geom = row.get("geometry")
        if not link or geom is None or geom.is_empty:
            continue
        if not geom.intersects(aoi):
            continue
        rows.append({
            "tile": str(row.get("tile") or ""),
            "url": str(link),
            "resolution": str(row.get("Resolution") or ""),
            "utm": str(row.get("UTM") or ""),
            "delivered": str(row.get("Delivered_Date") or ""),
        })
        covered.append(geom)

    if not covered:
        return [], 0.0

    from shapely.ops import unary_union

    union = unary_union(covered).intersection(aoi)
    fraction = float(union.area / aoi.area) if aoi.area > 0 else 0.0
    # Coarsest tier FIRST so the finest tier paints LAST: the compositor is
    # last-wins, and where two tiers overlap the finer cell is the better bed.
    rows.sort(key=lambda r: -_tier_metres(r["resolution"]))
    return rows, max(0.0, min(1.0, fraction))


def _tier_metres(label: str) -> float:
    """Metres from a tile-scheme ``Resolution`` label (``4m``), or 0 when unread."""
    try:
        return float(str(label).strip().lower().rstrip("m"))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# The datum gate. BlueTopo states NAVD88 machine-readably; this reads it.
# ---------------------------------------------------------------------------


def assert_navd88_tile(vsicurl_path: str) -> str:
    """Return the tile's stated vertical datum, or refuse.

    BlueTopo carries its vertical datum twice -- in the compound CRS's vertical
    component and in the private ``VERTICALDATUMWKT`` tag the product
    documentation points at. Both are read; a tile that states neither is
    refused rather than assumed, because the whole reason this source is on a
    bed ladder is that its datum is known.
    """
    import rasterio

    from .topobathy import _VSICURL_ENV_KW

    text = ""
    try:
        with rasterio.Env(**_VSICURL_ENV_KW):
            with rasterio.open(vsicurl_path) as ds:
                crs = ds.crs
                if crs is not None:
                    text += " " + (crs.to_wkt() or "")
                text += " " + str((ds.tags() or {}).get("VERTICALDATUMWKT", ""))
    except Exception as exc:  # noqa: BLE001
        raise BlueTopoUpstreamError(
            f"could not read the BlueTopo tile header for the datum check "
            f"({vsicurl_path}): {exc}"
        ) from exc

    lowered = text.lower()
    if "navd88" in lowered or "navd 88" in lowered or "navd_88" in lowered:
        return "NAVD88"
    raise BlueTopoDatumError(
        f"BlueTopo tile {vsicurl_path} does not state NAVD88 in its vertical CRS "
        f"or its VERTICALDATUMWKT tag (read: {text.strip()[:200]!r}); refusing to "
        "merge a bed whose datum is unknown"
    )


# ---------------------------------------------------------------------------
# HOOK: delegate_validate -- pre-cache, pre-network input gate.
# ---------------------------------------------------------------------------


@register_hook("bluetopo.validate")
def validate_bluetopo(spec: Any, params: dict[str, Any]) -> None:
    """Bbox finiteness + ordering + the US envelope, raised before any fetch."""
    raw = params.get("bbox")
    try:
        bbox = tuple(float(v) for v in raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise BlueTopoInputError(f"bbox must be four numbers; got {raw!r}") from exc
    if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
        raise BlueTopoInputError(f"bbox must be four finite numbers; got {raw!r}")
    west, south, east, north = bbox
    if east <= west or north <= south:
        raise BlueTopoInputError(
            f"degenerate bbox (min must be strictly less than max); got {bbox!r}"
        )
    uw, us, ue, un = _US_BBOX
    if east < uw or west > ue or north < us or south > un:
        raise BlueTopoInputError(
            f"bbox {bbox!r} is outside the US envelope {_US_BBOX!r} -- NOAA "
            "BlueTopo covers US waters only"
        )
    t_s = params.get("timeout_s")
    if t_s is not None and (not math.isfinite(float(t_s)) or float(t_s) <= 0):
        raise BlueTopoInputError(f"timeout_s must be > 0 and finite; got {t_s!r}")
    mpx = params.get("min_pixel_m")
    if mpx is not None and (not math.isfinite(float(mpx)) or float(mpx) <= 0):
        raise BlueTopoInputError(f"min_pixel_m must be > 0 and finite; got {mpx!r}")


# ---------------------------------------------------------------------------
# HOOK: delegate -- select, gate, merge, record provenance.
# ---------------------------------------------------------------------------


@register_hook("bluetopo.read")
def read_bluetopo(
    spec: Any, params: dict[str, Any], *, timeout_s: float
) -> tuple[Any, Any, Any]:
    """AOI -> tile-scheme select -> per-tile NAVD88 gate -> merged bed."""
    from .topobathy import (
        TopobathyEmptyError,
        TopobathyUpstreamError,
        _composite_sources_to_array,
    )

    bbox = tuple(float(v) for v in params["bbox"])
    target_crs = (str(params.get("target_crs") or TARGET_CRS)).strip()
    fetch_timeout = float(params.get("timeout_s") or 120.0)
    mpx = params.get("min_pixel_m")
    min_pixel_m = float(mpx) if mpx is not None else None

    rows, coverage = select_bluetopo_tiles(bbox, timeout_s=fetch_timeout)
    if not rows:
        raise BlueTopoCoverageGapError(
            f"no delivered NOAA BlueTopo tile intersects {bbox!r}. BlueTopo covers "
            "navigationally significant US waters; this AOI is outside what the "
            "programme has delivered. Supply your own bed (dem_uri on the "
            "topobathy row) or permit a declared alternative rung."
        )
    if len(rows) > _MAX_TILES:
        raise BlueTopoInputError(
            f"AOI {bbox!r} intersects {len(rows)} BlueTopo tiles, over the "
            f"{_MAX_TILES}-tile ceiling; narrow the bbox"
        )

    sources: list[str] = []
    for row in rows:
        path = f"/vsicurl/{row['url']}"
        assert_navd88_tile(path)
        sources.append(path)

    try:
        array, transform, crs, painted, _footprints = _composite_sources_to_array(
            sources, target_crs, bbox, min_pixel_m=min_pixel_m
        )
    except TopobathyEmptyError as exc:
        raise BlueTopoCoverageGapError(
            f"the BlueTopo tiles selected for {bbox!r} produced no bed: {exc}"
        ) from exc
    except TopobathyUpstreamError as exc:
        raise BlueTopoUpstreamError(
            f"BlueTopo tile read / merge failed for {bbox!r}: {exc}"
        ) from exc

    painted_rows = [row for row, ok in zip(rows, painted) if ok]
    record_provenance({
        "vertical_datum": "NAVD88",
        "tile_count": len(painted_rows),
        "resolution_tiers": sorted({r["resolution"] for r in painted_rows if r["resolution"]}),
        "coverage_fraction": coverage,
        "rung_coverage": {"bluetopo": coverage},
    })
    logger.info(
        "fetch_bluetopo: %d/%d tiles painted, tiers=%s, tile-scheme coverage %.3f",
        len(painted_rows), len(rows),
        sorted({r["resolution"] for r in painted_rows}), coverage,
    )
    return array, transform, crs


# ---------------------------------------------------------------------------
# HOOK: envelope -- the layer identity + the provenance replayed from the channel.
# ---------------------------------------------------------------------------


@register_hook("bluetopo.envelope")
def envelope_bluetopo(
    spec: Any,
    params: dict[str, Any],
    layer: Any,
    data: bytes | None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``BlueTopoResult`` fields from the recorded fetch provenance."""
    b = tuple(float(v) for v in params["bbox"])
    prov = provenance or {}
    coverage = prov.get("rung_coverage")
    tiers = prov.get("resolution_tiers") or []
    return {
        "layer_id": f"bluetopo-{b[0]:.4f}-{b[1]:.4f}-{b[2]:.4f}-{b[3]:.4f}",
        "name": (
            "NOAA BlueTopo bathymetry (NAVD88 m, positive-up) -- bbox "
            f"({b[0]:.2f},{b[1]:.2f},{b[2]:.2f},{b[3]:.2f})"
        ),
        "vertical_datum": str(prov.get("vertical_datum", "NAVD88")),
        "tile_count": int(prov.get("tile_count", 0)),
        "resolution_tiers": [str(t) for t in tiers],
        "coverage_fraction": float(prov.get("coverage_fraction", 0.0)),
        "rung_coverage": (
            {str(k): float(v) for k, v in coverage.items()}
            if isinstance(coverage, dict) and coverage
            else None
        ),
    }
