"""``fetch_ghsl_population`` atomic tool — JRC GHSL GHS-POP global population grid.

Wraps the European Commission Joint Research Centre (JRC) Global Human
Settlement Layer GHS-POP product (residential population, persons-per-cell)
served KEYLESS from the JRC open-data archive at ``jeodpp.jrc.ec.europa.eu``.
Returns a single-band float32 persons-per-cell COG clipped to the requested
bbox. This is the GLOBAL complement to ``fetch_hrsl_population`` (Meta HRSL,
also global but excludes far Arctic/Antarctica) and to the WorldPop branch of
``fetch_population`` — GHS-POP uses an independent built-up-area disaggregation
methodology, so it is a useful cross-check / alternative source.

Data source (Tier-1 free, no auth required):

    https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/
    GHS_POP_GLOBE_R2023A/GHS_POP_E2020_GLOBE_R2023A_4326_3ss/V1-0/tiles/
    └── GHS_POP_E2020_GLOBE_R2023A_4326_3ss_V1_0_R<r>_C<c>.zip
        └── ...R<r>_C<c>.tif   (10° x 10° tile, 12000 x 12000 px, EPSG:4326)

Resolution: 3 arcsecond (~100 m at the equator), epoch 2020, release R2023A.
The product is a global EPSG:4326 grid sliced into 10-degree tiles. Each tile
is published as a ZIP containing a single GeoTIFF. The JRC HTTP server honors
HTTP Range requests (returns 206 Partial Content), so GDAL's
``/vsizip//vsicurl/`` driver reads the ZIP central directory and then windows
into the inner GeoTIFF over byte ranges — we never download the multi-GB global
mosaic, only the bytes covering the requested bbox.

Strategy:

1. Validate bbox (required — ``supports_global_query=False``; the global
   mosaic is ~12 GB).
2. Map the bbox to its covering set of 10-degree GHSL tiles (R/C grid).
3. For each tile, open ``/vsizip//vsicurl/<zip>/<tif>`` and window-read the
   bbox sub-extent. Reject windows fully outside the tile.
4. Mosaic the per-tile windows (when the bbox straddles a tile boundary).
5. Replace the GHSL nodata sentinel (negative fill / -200) with NaN; cast
   float64 -> float32 to halve COG size.
6. Reject all-empty results (over open ocean / no coverage) with an honest
   typed error.
7. Write a CRS-tagged COG into the FR-DC cache (``static-30d``,
   ``source_class="ghsl_population"``).

Geographic-correctness gate (job-0086 / job-0112 lesson, codified):
- The live test asserts ``np.nansum`` of the output COG falls in a sensible
  range for a known non-US city (Lagos, Nigeria: this bbox observed
  ~12.5 million persons during development). A reprojection sign-flip /
  axis-swap would put the dense pixels on the wrong side of the bbox; a CRS
  lie would collapse the sum to ~0 or blow it up by an OOM.
- The output bounds must lie strictly inside the requested bbox.

FR-TA-2: atomic tool, returns ``LayerURI``.
FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical
``(bbox, epoch)`` calls reuse the cached COG.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_ghsl_population",
    "estimate_payload_mb",
    "GHSLPopError",
    "GHSLPopBboxRequiredError",
    "GHSLPopInputError",
    "GHSLPopUpstreamError",
    "GHSLPopEmptyError",
    "_fetch_ghsl_pop_bytes",
    "_tiles_for_bbox",
    "_GHSL_COVERAGE_BBOX",
]

logger = logging.getLogger("trid3nt_server.tools.fetch_ghsl_population")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 / NFR-R-1 typed-error surface).
# ---------------------------------------------------------------------------


class GHSLPopError(RuntimeError):
    """Base class for fetch_ghsl_population failures."""

    error_code: str = "GHSL_POPULATION_ERROR"
    retryable: bool = True


class GHSLPopBboxRequiredError(GHSLPopError):
    """``bbox`` is None / missing. The global GHS-POP mosaic is ~12 GB."""

    error_code = "BBOX_REQUIRED"
    retryable = False


class GHSLPopInputError(GHSLPopError):
    """Invalid input (malformed bbox, oversized window)."""

    error_code = "GHSL_POPULATION_INPUT_INVALID"
    retryable = False


class GHSLPopUpstreamError(GHSLPopError):
    """Upstream (JRC archive / vsizip-vsicurl open / rasterio read) failed."""

    error_code = "GHSL_POPULATION_UPSTREAM_ERROR"
    retryable = True


class GHSLPopEmptyError(GHSLPopError):
    """Bbox produced zero finite pixels (open ocean / outside coverage)."""

    error_code = "GHSL_POPULATION_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# GHSL 4326 3ss (R2023A, epoch 2020) tile grid.
# ---------------------------------------------------------------------------
#
# The product is a global EPSG:4326 raster cut into 10-degree tiles named
# ``R<row>_C<col>``. The tile origin is offset from the integer-degree grid by
# a fixed half-pixel-ish amount (the global raster does not start exactly at
# -180/+90). Both offsets were measured against five tiles read live during
# development (R7_C9, R5_C19, R8_C26, R4_C12, R6_C19) and verified exact to
# < 0.001 deg:
#
#   tile left (W) = -180 + (C - 1) * 10 + LON_OFFSET
#   tile top  (N) =   90 - (R - 1) * 10 + TOP_OFFSET
#
# Each tile is 10 deg square, 12000 x 12000 px (~100 m), float64 in source.

_TILE_DEG = 10.0
_LON_OFFSET = -0.00791625
_TOP_OFFSET = -0.900417

#: Global coverage envelope for GHS-POP (full lon span; lat roughly the GHSL
#: production extent). Used for a fast off-disk rejection before any network.
_GHSL_COVERAGE_BBOX = (-180.0, -60.0, 180.0, 84.0)

#: URL template for one tile (vsizip over vsicurl of the per-tile ZIP). The
#: inner GeoTIFF mirrors the ZIP basename.
_TILE_URL_TEMPLATE = (
    "/vsizip//vsicurl/https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/"
    "GHS_POP_GLOBE_R2023A/GHS_POP_E2020_GLOBE_R2023A_4326_3ss/V1-0/tiles/"
    "GHS_POP_E2020_GLOBE_R2023A_4326_3ss_V1_0_R{r}_C{c}.zip/"
    "GHS_POP_E2020_GLOBE_R2023A_4326_3ss_V1_0_R{r}_C{c}.tif"
)

#: Source URL (human-readable, for tags) of the global product page.
_GHSL_SOURCE_URL = (
    "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/"
    "GHS_POP_GLOBE_R2023A/GHS_POP_E2020_GLOBE_R2023A_4326_3ss/V1-0/"
)

#: The only epoch wired in v0.1. The R2023A archive publishes 1975..2030 in
#: 5-year steps; epoch 2020 is the most recent observed (vs 2025/2030
#: projections). Accepted as a kwarg for forward-compat, currently fixed.
_DEFAULT_EPOCH = 2020
_VALID_EPOCHS = frozenset({2020})

#: Bbox quantization (6 dp) for cache-key stability. Matches sibling fetchers.
_BBOX_QUANTIZE_DP = 6

#: GDAL HTTP timeout (s) for the vsicurl driver.
_GDAL_TIMEOUT_S = 120

#: Per-tile window pixel cap. ~5 deg square @ 100 m = 6000 x 6000 = 36M px;
#: a full 10-deg tile is 12000 x 12000 = 144M px (~576 MB float32). Cap below
#: that so we never materialize a pathological full-tile read.
_MAX_PIXELS = 60_000_000

_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------


_METADATA = AtomicToolMetadata(
    name="fetch_ghsl_population",
    ttl_class="static-30d",
    source_class="ghsl_population",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# Payload estimator (Wave 1.5 chat-warning gate).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    **_kw: Any,
) -> float:
    """Estimate emitted COG size in MB.

    Single-band float32 persons/cell at ~100 m DEFLATE-compresses well
    (population is mostly nodata/zero away from settlements). Lagos 0.6x0.4
    deg observed ~1.0 MB. Scale linearly with bbox area; floor at 0.2 MB.
    """
    if bbox is None:
        return 4.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 4.0
    # ~4 MB / sq-deg is a conservative upper bound for dense regions.
    return max(0.2, sq_deg * 4.0)


# ---------------------------------------------------------------------------
# bbox + tile-grid helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float] | None) -> None:
    """Raise ``GHSLPopBboxRequiredError`` / ``GHSLPopInputError`` if invalid."""
    if bbox is None:
        raise GHSLPopBboxRequiredError(
            "bbox is required for fetch_ghsl_population — the global GHS-POP "
            "mosaic is ~12 GB; pass a (min_lon, min_lat, max_lon, max_lat) in "
            "EPSG:4326."
        )
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise GHSLPopInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise GHSLPopInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise GHSLPopInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise GHSLPopInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise GHSLPopInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _bbox_intersects_coverage(bbox: tuple[float, float, float, float]) -> bool:
    """True iff ``bbox`` intersects the GHS-POP global coverage envelope."""
    min_lon, min_lat, max_lon, max_lat = bbox
    cmnlon, cmnlat, cmxlon, cmxlat = _GHSL_COVERAGE_BBOX
    return (
        min_lon <= cmxlon
        and max_lon >= cmnlon
        and min_lat <= cmxlat
        and max_lat >= cmnlat
    )


def _tile_bounds(r: int, c: int) -> tuple[float, float, float, float]:
    """Return (W, S, E, N) of GHSL tile R<r>_C<c>."""
    left = -180.0 + (c - 1) * _TILE_DEG + _LON_OFFSET
    top = 90.0 - (r - 1) * _TILE_DEG + _TOP_OFFSET
    return (left, top - _TILE_DEG, left + _TILE_DEG, top)


def _tiles_for_bbox(
    bbox: tuple[float, float, float, float],
) -> list[tuple[int, int]]:
    """Map a bbox to the set of (row, col) GHSL tiles that cover it."""
    min_lon, min_lat, max_lon, max_lat = bbox
    c0 = math.floor((min_lon - _LON_OFFSET + 180.0) / _TILE_DEG) + 1
    c1 = math.floor((max_lon - _LON_OFFSET + 180.0) / _TILE_DEG) + 1
    r0 = math.floor((90.0 + _TOP_OFFSET - max_lat) / _TILE_DEG) + 1
    r1 = math.floor((90.0 + _TOP_OFFSET - min_lat) / _TILE_DEG) + 1
    tiles: list[tuple[int, int]] = []
    for r in range(min(r0, r1), max(r0, r1) + 1):
        for c in range(min(c0, c1), max(c0, c1) + 1):
            if r >= 1 and c >= 1:
                tiles.append((r, c))
    return tiles


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox to 6 dp for cache-key stability."""
    return tuple(round(v, _BBOX_QUANTIZE_DP) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Core fetch function.
# ---------------------------------------------------------------------------


def _fetch_ghsl_pop_bytes(
    bbox: tuple[float, float, float, float],
    epoch: int = _DEFAULT_EPOCH,
) -> bytes:
    """Window-read GHS-POP for ``bbox``, mosaic tiles, write a COG.

    Returns COG bytes (persons/cell, float32, EPSG:4326, NaN nodata).

    Raises:
        ``GHSLPopInputError``: unsupported epoch / oversized window.
        ``GHSLPopEmptyError``: bbox outside coverage / all-nodata.
        ``GHSLPopUpstreamError``: archive open / read / COG write failure.
    """
    if epoch not in _VALID_EPOCHS:
        raise GHSLPopInputError(
            f"unsupported epoch={epoch!r}; allowed: {sorted(_VALID_EPOCHS)}"
        )

    if not _bbox_intersects_coverage(bbox):
        raise GHSLPopEmptyError(
            f"bbox={bbox} falls outside GHS-POP coverage {_GHSL_COVERAGE_BBOX}"
        )

    try:
        import numpy as np
        import rasterio
        import rasterio.io
        from rasterio.merge import merge
        from rasterio.windows import Window, from_bounds
    except ImportError as exc:
        raise GHSLPopUpstreamError(
            f"rasterio / numpy not available: {exc}"
        ) from exc

    tiles = _tiles_for_bbox(bbox)
    if not tiles:
        raise GHSLPopEmptyError(
            f"bbox={bbox} maps to no GHS-POP tiles (outside coverage)"
        )

    gdal_env = {
        "GDAL_HTTP_TIMEOUT": str(_GDAL_TIMEOUT_S),
        "GDAL_HTTP_USERAGENT": _USER_AGENT,
        # vsizip needs to see the zip central directory; do not list dirs.
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_CHUNK_SIZE": "1048576",  # 1 MiB
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.zip",
    }

    datasets: list[Any] = []
    try:
        with rasterio.Env(**gdal_env):
            for (r, c) in tiles:
                url = _TILE_URL_TEMPLATE.format(r=r, c=c)
                try:
                    src = rasterio.open(url)
                except Exception as exc:  # noqa: BLE001
                    # A missing tile (ocean-only R/C the archive omits) is a
                    # coverage gap, not a hard failure when other tiles exist.
                    logger.info(
                        "fetch_ghsl_population: tile R%d_C%d open failed (%s); "
                        "treating as no-coverage for this tile",
                        r,
                        c,
                        exc,
                    )
                    continue
                try:
                    win = from_bounds(*bbox, transform=src.transform)
                    win = win.round_offsets(op="floor").round_lengths(op="ceil")
                    win = win.intersection(Window(0, 0, src.width, src.height))
                    if win.width <= 0 or win.height <= 0:
                        src.close()
                        continue
                    if int(win.width) * int(win.height) > _MAX_PIXELS:
                        raise GHSLPopInputError(
                            f"bbox={bbox} would request "
                            f"{int(win.width) * int(win.height):,} pixels in tile "
                            f"R{r}_C{c} — refuse to materialize > "
                            f"{_MAX_PIXELS:,}; narrow the bbox."
                        )
                    arr = src.read(1, window=win).astype(np.float32)
                    # GHSL source uses a negative fill (e.g. -200) for nodata.
                    # Treat any negative as nodata -> NaN.
                    arr[arr < 0] = np.nan
                    out_transform = src.window_transform(win)
                    mem = rasterio.io.MemoryFile()
                    dst = mem.open(
                        driver="GTiff",
                        height=int(win.height),
                        width=int(win.width),
                        count=1,
                        dtype="float32",
                        crs="EPSG:4326",
                        transform=out_transform,
                        nodata=float("nan"),
                    )
                    dst.write(arr, 1)
                    datasets.append(dst)
                except GHSLPopError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise GHSLPopUpstreamError(
                        f"GHS-POP window read failed for tile R{r}_C{c}: {exc}"
                    ) from exc
                finally:
                    try:
                        src.close()
                    except Exception:  # noqa: BLE001
                        pass

            if not datasets:
                raise GHSLPopEmptyError(
                    f"bbox={bbox} produced no GHS-POP pixels "
                    "(over open water or outside coverage)"
                )

            if len(datasets) == 1:
                mosaic = datasets[0].read(1)
                mtransform = datasets[0].transform
            else:
                merged, mtransform = merge(datasets, nodata=float("nan"))
                mosaic = merged[0]

            if not np.isfinite(mosaic).any():
                raise GHSLPopEmptyError(
                    f"bbox={bbox} produced no valid GHS-POP pixels "
                    "(all-NaN window — likely over open water)"
                )

            height, width = mosaic.shape
            out_fd, out_path = tempfile.mkstemp(
                suffix=".tif", prefix="trid3nt_ghsl_pop_"
            )
            os.close(out_fd)
            try:
                profile = {
                    "driver": "COG",
                    "dtype": "float32",
                    "count": 1,
                    "height": int(height),
                    "width": int(width),
                    "crs": "EPSG:4326",
                    "transform": mtransform,
                    "nodata": float("nan"),
                    "compress": "DEFLATE",
                    "PREDICTOR": "2",
                    "BIGTIFF": "IF_SAFER",
                }
                with rasterio.open(out_path, "w", **profile) as dst:
                    dst.write(mosaic.astype(np.float32), 1)
                    dst.update_tags(
                        units="persons_per_cell",
                        source="JRC_GHS_POP_E2020_GLOBE_R2023A_4326_3ss",
                        source_url=_GHSL_SOURCE_URL,
                        epoch=str(epoch),
                        tool="fetch_ghsl_population",
                    )
                with open(out_path, "rb") as f:
                    cog_bytes = f.read()
            finally:
                try:
                    os.unlink(out_path)
                except OSError:
                    pass

            logger.info(
                "fetch_ghsl_population: wrote %d-byte COG "
                "(tiles=%d, persons sum=%.1f, max=%.2f)",
                len(cog_bytes),
                len(datasets),
                float(np.nansum(mosaic)),
                float(np.nanmax(mosaic)),
            )
            return cog_bytes
    except GHSLPopError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GHSLPopUpstreamError(
            f"unexpected error fetching GHS-POP for bbox={bbox}: {exc}"
        ) from exc
    finally:
        for d in datasets:
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    open_world_hint=True,
)
def fetch_ghsl_population(
    bbox: tuple[float, float, float, float],
    epoch: int = _DEFAULT_EPOCH,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch JRC GHSL GHS-POP gridded population (persons/cell) for a bbox.

    **What it does:** Opens the European Commission JRC Global Human Settlement
    Layer GHS-POP product (residential population, release R2023A, epoch 2020,
    ~100 m / 3 arcsecond, EPSG:4326) from the keyless JRC open-data archive.
    The global grid is sliced into 10-degree ZIP-wrapped tiles; this tool maps
    the bbox to its covering tiles and uses GDAL ``/vsizip//vsicurl/`` HTTP
    byte-range reads to fetch only the window covering the bbox, mosaicking
    across tile boundaries when needed. Writes a single-band float32
    persons-per-cell Cloud-Optimized GeoTIFF (NaN nodata). Cached ``static-30d``.
    No API key or login required.

    **When to use:**
    - GLOBAL population exposure modeling outside the US, or as an independent
      cross-check against ``fetch_hrsl_population`` (Meta HRSL) and the WorldPop
      branch of ``fetch_population`` — GHS-POP uses a different built-up-area
      disaggregation methodology.
    - "How many people live inside this flood / surge / wildfire footprint?" for
      any non-US (or US) AOI, especially European, African, Asian, and South
      American cities.
    - Population-density input for Pelicun-style exposure terms anywhere on Earth.

    **When NOT to use:**
    - Per-building occupancy (GHS-POP is gridded; combine ``fetch_buildings``
      footprints with this raster for building-level occupancy).
    - Authoritative US census tabulation for reporting (use ``fetch_census_acs``).
    - Sub-100 m / parcel-scale precision (HRSL ~30 m is finer; prefer it when
      coverage and resolution both matter and the AOI is small).
    - Real-time counts (GHS-POP is a modeled epoch grid, not a live measurement).
    - Bboxes over open ocean / outside coverage (raises ``GHSLPopEmptyError``).

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
      **Required** (``supports_global_query=False`` — the global mosaic is
      ~12 GB). Example: ``(3.10, 6.35, 3.70, 6.75)`` for Lagos, Nigeria.
    - ``epoch`` (int): GHS-POP epoch year; default 2020 (the only wired epoch
      in v0.1). Accepted for forward compatibility.

    **Returns:**
    ``LayerURI(layer_type="raster", role="primary", units="persons_per_cell")``
    pointing at a COG (.tif). EPSG:4326, float32, NaN nodata, ~100 m pixels.
    Tagged ``units=persons_per_cell`` and
    ``source=JRC_GHS_POP_E2020_GLOBE_R2023A_4326_3ss``. Reuses the
    ``population_density`` style preset (magma persons/cell ramp).

    **Cross-tool dependencies:**
    - Downstream of: ``geocode_location`` (provides bbox), ``fetch_dem``
      (co-registered for elevation-weighted exposure).
    - Upstream of: ``compute_zonal_statistics`` (sum population within a polygon),
      Pelicun impact post-processor, any population-at-risk workflow step.
    - Complements: ``fetch_hrsl_population`` (Meta HRSL ~30 m global) and
      ``fetch_population`` (WorldPop / US Census) — different methodologies,
      same persons/cell semantics.
    """
    _validate_bbox(bbox)
    assert bbox is not None
    if epoch not in _VALID_EPOCHS:
        raise GHSLPopInputError(
            f"unsupported epoch={epoch!r}; allowed: {sorted(_VALID_EPOCHS)}"
        )

    q_bbox = _round_bbox(bbox)
    params = {"bbox": list(q_bbox), "epoch": epoch}

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_ghsl_pop_bytes(q_bbox, epoch=epoch),
    )
    assert result.uri is not None, (
        "fetch_ghsl_population is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=(
            f"ghsl-pop-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
            f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name="Population (JRC GHS-POP E2020 — persons/cell)",
        layer_type="raster",
        uri=result.uri,
        style_preset="population_density",
        role="primary",
        units="persons_per_cell",
        bbox=tuple(q_bbox),
    )
