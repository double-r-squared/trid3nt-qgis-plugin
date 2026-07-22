"""Landcover fetcher (``fetch_landcover``): MRLC NLCD WCS primary, ESA WorldCover
fallback -> paletted COG (incl. the state-scale path).

Carved out of the original multi-tool ``data_fetch`` module (job-0033) in the
tools/ reorg; behavior and the registered tool surface are unchanged. The
shared typed-error hierarchy + bbox helpers live in
``trid3nt_server.tools.fetchers._fetch_common``.
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
    _DEFAULT_USER_AGENT,
    _validate_bbox,
    round_bbox_to_resolution,
    _bbox_area_km2,
)

__all__ = [
    "fetch_landcover",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.terrain.fetch_landcover")


# ---------------------------------------------------------------------------
# fetch_landcover — NLCD (MRLC) / ESA WorldCover (sprint-07 Stage B, job-0039;
# job-0044 hotfix: WMS → WCS 1.0.0 to fix palette encoding).
# ---------------------------------------------------------------------------
#
# Access pattern tier — LIVE-VERIFIED THROUGH TWO ROUNDS:
#
# Round 1 (job-0039, 2026-06-07):
#
#   * The MRLC direct file mirror (``s3-us-west-2.amazonaws.com/mrlc/
#     Annual_NLCD_LndCov_<YEAR>_CU_C1V0.tif``) returned an HTTP 200 with a
#     **42-byte placeholder TIFF** (a 1×1 IFD with two ``0xFFFFFFFF`` strip
#     offsets — not a real raster). 2019 and 2021 file URLs at the same path
#     return HTTP 403. The "direct HTTPS + Range" path the kickoff inferred is
#     NOT a real surface for NLCD bytes.
#   * The MRLC WCS endpoint (`/geoserver/mrlc_display/wcs`) timed out on
#     GetCapabilities in the first probe.
#   * MRLC's **WMS** GeoServer at ``www.mrlc.gov/geoserver/mrlc_display/wms``
#     serves NLCD year layers (``NLCD_2021_Land_Cover_L48`` etc.) and supports
#     ``GetMap?format=image/geotiff`` — Tier 2 (OGC service) byte materialized.
#     Substrate landed against WMS GetMap.
#
# Round 2 (job-0044, 2026-06-07 — THE PALETTE-ENCODING HOTFIX):
#
#   * Job-0042's NLCD validation gate (Invariant 7 mitigation) fired on a real
#     Fort Myers smoke run: the WMS GetMap GeoTIFF returns raster bytes that
#     are **palette indices** (1, 3, 4, 5, ..., 21) NOT canonical NLCD class
#     integers (11, 21, 22, 23, ..., 95) — surfaced as
#     OQ-42-NLCD-WMS-PALETTE-ENCODING. The Manning's mapping CSV is keyed by
#     canonical integers; SFINCS dispatch was blocked end-to-end.
#   * Live-probed both candidate fix paths per §F.1.1 live-verification discipline:
#
#     - **Path A (palette decode):** the WMS GeoTIFF carries a 256-entry
#       ColorTable in its IFD; the index→RGB→canonical NLCD mapping is fixed
#       (idx 1 = open-water = (71,107,160) = NLCD 11; idx 3 = developed-open
#       = (221,201,201) = NLCD 21; …). Decoding via the embedded ColorTable
#       and an inverse RGB→class table is feasible but adds a fragile
#       client-side translation step (one MRLC palette reorder breaks us).
#     - **Path B (WCS 1.0.0 GetCoverage):** ``mrlc_display:NLCD_2021_Land_
#       Cover_L48`` coverage served by the WCS 1.0.0 endpoint with
#       ``REQUEST=GetCoverage&CRS=EPSG:4326&BBOX=...&WIDTH=...&HEIGHT=...&FORMAT=GeoTIFF``
#       returns canonical NLCD class integers DIRECTLY (verified: unique band1
#       values for Fort Myers bbox = [11, 21, 22, 23, 24, 31, 41, 42, 43, 52,
#       71, 81, 82, 90, 95, 255-nodata] — every value cleanly mapped to
#       manning_mapping.csv v1.0.0). The DescribeCoverage XML calls the band
#       "PALETTE_INDEX" but the integers ARE the canonical NLCD codes — WCS
#       1.0.0 emits the source dataset's raw byte values whereas WMS GetMap
#       emits the rendered (re-indexed) palette indices.
#     - **WCS 2.0.1 / 1.1.1:** also tried; both fail in different ways. WCS
#       2.0.1 hits a GeoServer "Unable to map projection Popular Visualisation
#       Pseudo Mercator" exception (GeoServer projection-mapping bug on its
#       own native CRS). WCS 1.1.1 rejects bbox-only requests as "less than a
#       pixel would be read." WCS 1.0.0 with explicit WIDTH/HEIGHT is the
#       reliable byte surface.
#
#   * **Path B chosen.** Canonical bytes from the server is a clean win over
#     client-side palette decoding: no RGB→class lookup to maintain, no
#     fragility to MRLC palette reorders, no Round-3 silent-wrong-answer risk.
#     Both paths are §F.1.1 Tier 2 (OGC service) — substrate stays Tier 2,
#     vendor sub-protocol switches from WMS GetMap to WCS GetCoverage.
#
# Job-0044 cache-migration policy: cache key now includes ``source: "mrlc-wcs"``
# (the palette-encoded ``mrlc-wms`` entries from job-0039's evidence land
# under a different cache prefix and naturally evict on the 30-day TTL — no
# explicit invalidation needed). Job-0039's evidence COGs at
# ``cache/static-30d/landcover/56bad09bfa8a71d502ed61badc785a00.tif`` will
# remain until TTL eviction; the new canonical-bytes COGs land at a new key.
#
# Round 1 deviation (job-0039) is still recorded as OQ-39-NLCD-TIER-DEVIATION
# (kickoff inferred Tier 3 → live Tier 2). Round 2 hotfix (job-0044) closes
# OQ-42-NLCD-WMS-PALETTE-ENCODING.
#
# Vintage discipline: NLCD vintages 2019, 2021 (default), and 2023 are most-
# relevant. The Annual NLCD Collection 1.0 (2023 release) is published as the
# ``Annual_NLCD_LndCov_<YEAR>_CU_C1V0`` family; the WMS GeoServer lists
# discrete-year layers up through **NLCD_2021_Land_Cover_L48**. 2023 is the
# newest release but its WMS layer name was not present in the MRLC
# GetCapabilities at probe time (2026-06-07); the substrate defaults to 2021
# and the dataset string parameter supports ``"nlcd_2019"`` and (forward-
# looking) ``"nlcd_2023"`` once it lands. ESA WorldCover (Planetary Computer
# ``esa-worldcover``) opt-in via ``dataset="esa_worldcover_2021"``.
#
# Manning's mapping validation gate (per docs/decisions/oq-4-hydromt-depth.md
# §4 "Immediate (job-0039)"): the NLCD vintage year is returned as sidecar
# metadata alongside the LayerURI so job-0042 ``build_sfincs_model`` can
# verify the Manning's mapping CSV covers the vintage's class encoding. This
# is the Invariant 7 (no silent wrong answers) mitigation OQ-4 demanded.
#
# Sidecar shape — return-value design: ``LayerURI`` (in
# ``trid3nt_contracts.execution``) is a FROZEN contract with
# ``extra="forbid"`` — we cannot add a ``metadata`` field. The kickoff's
# example syntax ``LayerURI.metadata["nlcd_vintage_year"] = 2021`` was
# illustrative; the actual seam is a structured ``dict`` return shape:
#
#     {
#       "layer": LayerURI(...),
#       "nlcd_vintage_year": 2021,
#       "dataset": "nlcd_2021",
#       "source": "mrlc-wms",
#     }
#
# This is the same dict-return pattern as ``geocode_location`` (also no
# contract for its shape) and ``lookup_precip_return_period`` below — see
# OQ-39-LANDCOVER-RETURN-SHAPE-CONTRACT-PROMOTION.


_FETCH_LANDCOVER_METADATA = AtomicToolMetadata(
    name="fetch_landcover",
    ttl_class="static-30d",
    source_class="landcover",
    cacheable=True,
)

# Landcover-ONLY cache-version salt (job-0324 follow-up — STALE-CACHE fix).
# -------------------------------------------------------------------------
# The "bake NLCD land cover into hillshade" demo rendered grey because the
# read-through cache (static-30d, 30-day TTL) was serving NLCD COGs written
# BEFORE deploy #3's palette-preservation fix (job-0324). Those stale COGs
# dropped their embedded GDAL color table, so blending them produced a flat
# grayscale base instead of the NLCD class colors.
#
# Bumping this salt changes the canonicalized ``params`` dict that drives the
# landcover cache key (``compute_cache_key`` hashes ``source_id || params ||
# vintage``), so a post-fix fetch for the SAME bbox now computes a DIFFERENT
# key than the pre-fix entry — i.e. it MISSES the stale palette-less COG and
# regenerates a colored (palette-preserving) COG. This is scoped to
# fetch_landcover ONLY: it is folded into the landcover ``params`` dict, never
# into the shared ``compute_cache_key`` salt, so no other tool's cache key
# changes (a recursive cache wipe was deliberately avoided). Bump the integer
# whenever a landcover-COG-generation fix must force a clean regenerate.
_LANDCOVER_CACHE_VERSION = 3  # v3 = F26 background(0)->nodata transparency; v2 = post-job-0324 palette-preserving COGs

# MRLC WCS 1.0.0 GeoServer endpoint (Tier 2 OGC service, live-verified
# 2026-06-07 in job-0044). WCS 1.0.0 GetCoverage returns canonical NLCD class
# integers in the raster band — the WMS GetMap path job-0039 landed against
# returned palette-encoded indices (the OQ-42-NLCD-WMS-PALETTE-ENCODING
# blocker job-0042's validation gate caught). WCS 1.0.0 was chosen over
# WCS 1.1.1 / 2.0.1: 2.0.1 hits a GeoServer projection-mapping bug ("Unable
# to map projection Popular Visualisation Pseudo Mercator") on its own
# native EPSG:3857; 1.1.1 rejects bbox-only requests; 1.0.0 with explicit
# CRS=EPSG:4326 + WIDTH/HEIGHT + FORMAT=GeoTIFF is the reliable surface.
_MRLC_WCS_URL = "https://www.mrlc.gov/geoserver/mrlc_display/wcs"

# NLCD year → WCS coverage ID in the MRLC GeoServer catalog. WCS uses the
# qualified workspace:coverage form ``mrlc_display:NLCD_<YEAR>_Land_Cover_L48``
# (the underlying GeoServer layer); live-verified 2026-06-07.
_NLCD_WCS_COVERAGE_BY_YEAR: dict[int, str] = {
    2001: "mrlc_display:NLCD_2001_Land_Cover_L48",
    2004: "mrlc_display:NLCD_2004_Land_Cover_L48",
    2006: "mrlc_display:NLCD_2006_Land_Cover_L48",
    2008: "mrlc_display:NLCD_2008_Land_Cover_L48",
    2011: "mrlc_display:NLCD_2011_Land_Cover_L48",
    2013: "mrlc_display:NLCD_2013_Land_Cover_L48",
    2016: "mrlc_display:NLCD_2016_Land_Cover_L48",
    2019: "mrlc_display:NLCD_2019_Land_Cover_L48",
    2021: "mrlc_display:NLCD_2021_Land_Cover_L48",
}

def _read_band1_colormap(src) -> dict | None:
    """Return the band-1 palette color table (``{idx: (r,g,b,a)}``) or ``None``.

    NLCD land cover ships a single-band palette-index COG with an EMBEDDED GDAL
    color table; TiTiler colorizes from it. Every COG re-write (clip, COG
    translate, overview enforcement) must carry that table forward or the layer
    renders solid grey (job-0324 regression). rasterio raises ``ValueError``
    when band 1 has no color table — that is the normal, expected case for
    continuous rasters (DEM, hillshade, flood depth), and we return ``None`` so
    the caller does NOT fabricate one.
    """
    try:
        return src.colormap(1)
    except ValueError:
        # rasterio raises ValueError when band 1 has no color table — the
        # normal case for continuous rasters (DEM/hillshade/flood depth).
        return None
    except Exception as exc:  # noqa: BLE001 — any other read failure: no-op
        logger.debug("colormap read skipped (%s: %s)", type(exc).__name__, exc)
        return None

def _apply_band1_colormap(dst, cmap: dict | None, colorinterp=None) -> None:
    """Write a preserved band-1 color table + palette colorinterp onto ``dst``.

    No-op when ``cmap`` is ``None`` (non-paletted raster — we never fabricate a
    color table). When a table is present, stamp it on band 1 and set band 1's
    color interpretation to ``palette`` so downstream readers/TiTiler treat the
    integer pixels as indices into the table.
    """
    if cmap is None:
        return
    try:
        dst.write_colormap(1, cmap)
        try:
            from rasterio.enums import ColorInterp

            interp = list(dst.colorinterp)
            interp[0] = ColorInterp.palette
            dst.colorinterp = tuple(interp)
        except Exception:  # noqa: BLE001 — colorinterp set is best-effort
            pass
    except Exception as exc:  # noqa: BLE001 — colormap copy is best-effort
        logger.warning(
            "colormap preservation failed (%s: %s); output may render grey",
            type(exc).__name__,
            exc,
        )

def _clip_raster_bytes_to_bbox(
    tif_bytes: bytes, bbox: tuple[float, float, float, float]
) -> bytes:
    """Crop a GeoTIFF (bytes) to the EXACT requested bbox via rasterio windowing.

    The MRLC WCS GetCoverage already returns the requested BBOX server-side,
    but pixel snapping can leave a fringe row/column outside the AOI. This
    reprojects the bbox into the raster's CRS, computes the pixel window, and
    writes the cropped raster — guaranteeing the output extent matches the
    requested bbox to within one pixel. Best-effort: returns the input bytes
    unchanged on any failure (never raises — clipping is a precision nicety,
    not a correctness gate).
    """
    in_tmp: str | None = None
    out_tmp: str | None = None
    try:
        import rasterio  # type: ignore[import-not-found]
        from rasterio.warp import transform_bounds  # type: ignore[import-not-found]
        from rasterio.windows import from_bounds as window_from_bounds  # type: ignore[import-not-found]

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
            in_tmp = f.name
            f.write(tif_bytes)

        with rasterio.open(in_tmp) as src:
            dst_crs = src.crs
            # Reproject the WGS84 bbox into the raster CRS (no-op when already 4326).
            if dst_crs is not None and dst_crs.to_epsg() != 4326:
                left, bottom, right, top = transform_bounds(
                    "EPSG:4326", dst_crs, *bbox, densify_pts=21
                )
            else:
                left, bottom, right, top = bbox
            window = window_from_bounds(
                left, bottom, right, top, transform=src.transform
            )
            # Intersect with the raster's full window so we never read outside it.
            full = rasterio.windows.Window(0, 0, src.width, src.height)
            window = window.intersection(full).round_offsets().round_lengths()
            if window.width < 1 or window.height < 1:
                # Degenerate intersection — keep the original (don't blank it out).
                return tif_bytes
            data = src.read(window=window)
            transform = src.window_transform(window)
            profile = src.profile.copy()
            profile.update(
                height=int(window.height),
                width=int(window.width),
                transform=transform,
            )
            # Preserve a band-1 palette color table (e.g. NLCD land cover) so
            # the cropped output still colorizes. None when the source has no
            # color table (DEM/hillshade/flood depth) — a pure no-op there.
            cmap = _read_band1_colormap(src)
            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as of:
                out_tmp = of.name
            with rasterio.open(out_tmp, "w", **profile) as dst:
                dst.write(data)
                _apply_band1_colormap(dst, cmap)
        with open(out_tmp, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001 — clip is best-effort precision
        logger.warning(
            "fetch_landcover: bbox clip failed (%s: %s); returning unclipped raster",
            type(exc).__name__,
            exc,
        )
        return tif_bytes
    finally:
        for path in (in_tmp, out_tmp):
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass

def _rasterio_translate_to_cog(tif_bytes: bytes) -> bytes:
    """Translate GeoTIFF bytes to a tiled COG WITH overviews via the rasterio COG driver.

    Used as the fallback when the GDAL CLI binaries that ``_translate_to_cog``
    (compute_hillshade) shells out to are not on PATH (e.g. the agent .venv
    without gdal-bin). The rasterio ``COG`` driver builds internal overviews
    and 512x512 tiling automatically — the exact properties TiTiler needs to
    avoid the zoomed-out 404s that made NLCD render spotty. Best-effort:
    returns the input bytes unchanged on any failure.
    """
    in_tmp: str | None = None
    out_tmp: str | None = None
    try:
        import rasterio  # type: ignore[import-not-found]

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
            in_tmp = f.name
            f.write(tif_bytes)
        with rasterio.open(in_tmp) as src:
            profile = {
                "driver": "COG",
                "width": src.width,
                "height": src.height,
                "count": src.count,
                "dtype": src.dtypes[0],
                "crs": src.crs,
                "transform": src.transform,
                "compress": "DEFLATE",
            }
            if src.nodata is not None:
                profile["nodata"] = src.nodata
            data = src.read()
            # Preserve a band-1 palette color table (NLCD land cover) across the
            # COG translate — TiTiler colorizes from this embedded table. None
            # for non-paletted rasters (DEM/hillshade/flood depth): a no-op.
            cmap = _read_band1_colormap(src)
            colorinterp = src.colorinterp
            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as of:
                out_tmp = of.name
            with rasterio.open(
                out_tmp, "w", OVERVIEW_RESAMPLING="NEAREST", **profile
            ) as dst:
                dst.write(data)
                _apply_band1_colormap(dst, cmap, colorinterp)
        with open(out_tmp, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001 — COG translate is best-effort
        logger.warning(
            "fetch_landcover: rasterio COG translate failed (%s: %s); returning "
            "flat GeoTIFF bytes",
            type(exc).__name__,
            exc,
        )
        return tif_bytes
    finally:
        for path in (in_tmp, out_tmp):
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass

def _landcover_bytes_to_cog(
    tif_bytes: bytes, bbox: tuple[float, float, float, float]
) -> bytes:
    """Clip NLCD bytes to the exact bbox and emit a tiled COG WITH overviews.

    job-0271-class fix for fetch_landcover: the MRLC WCS GetCoverage returns a
    flat strip-organized GeoTIFF with NO overviews, so TiTiler 404s the
    zoomed-out tiles and the layer renders spotty / never paints when panned
    out. This routes the raster through ``_translate_to_cog`` (the
    compute_hillshade COG translator that writes a tiled COG with overviews)
    when the GDAL CLI is available, and falls back to the pure-rasterio COG
    driver otherwise — so overviews are present in BOTH environments.

    Also clips to the EXACT requested bbox first (precision nicety; the WCS
    already honors BBOX server-side but pixel snapping can leave a fringe).
    """
    clipped = _clip_raster_bytes_to_bbox(tif_bytes, bbox)

    # Prefer the assigned compute_hillshade COG translator (GDAL CLI path) so
    # the COG profile matches every other raster product. Fall back to the
    # pure-rasterio COG driver when the gdal binaries are not on PATH.
    try:
        from trid3nt_server.tools.processing.compute_hillshade import _get_gdaldem_bin, _translate_to_cog

        in_tmp: str | None = None
        try:
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
                in_tmp = f.name
                f.write(clipped)
            gdaldem_bin = _get_gdaldem_bin()  # raises if gdal CLI absent
            cog = _translate_to_cog(in_tmp, gdaldem_bin)
            # _translate_to_cog returns flat bytes when gdal_translate is missing
            # even though gdaldem resolved; verify overviews landed, else fall
            # through to the rasterio path below.
            if _has_overviews(cog):
                return cog
        finally:
            if in_tmp is not None:
                try:
                    os.unlink(in_tmp)
                except OSError:
                    pass
    except Exception as exc:  # noqa: BLE001 — GDAL CLI not available / failed
        logger.info(
            "fetch_landcover: GDAL-CLI COG translate unavailable (%s); using "
            "rasterio COG driver fallback",
            exc,
        )

    return _rasterio_translate_to_cog(clipped)

def _has_overviews(tif_bytes: bytes) -> bool:
    """Return True iff the GeoTIFF bytes carry internal overviews on band 1."""
    in_tmp: str | None = None
    try:
        import rasterio  # type: ignore[import-not-found]

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
            in_tmp = f.name
            f.write(tif_bytes)
        with rasterio.open(in_tmp) as src:
            return len(src.overviews(1)) > 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        if in_tmp is not None:
            try:
                os.unlink(in_tmp)
            except OSError:
                pass

# NLCD "Background" class code -- MRLC's official legend reserves index 0 for
# pixels outside the classified CONUS extent (open ocean, international
# waters, etc). It is NEVER a legitimate NLCD land-cover class (real classes
# are 11-95); the MRLC WCS 1.0.0 GetCoverage's embedded color table maps it
# to OPAQUE BLACK ((0, 0, 0, 255)) rather than transparent -- confirmed via a
# live probe of the real endpoint (bbox off the Washington coast, 2026-07-09).
# The raster's DECLARED ``nodata`` tag is 255 (a separate sentinel that DOES
# render transparent), so 0 slips through as an undeclared second nodata
# value. City/county-scale fetches (always fully on land) never hit index 0
# and never surfaced this; the state-scale auto-coarsen resolution-gate path
# (commit 21cd123) is what first requested a bbox large enough to include
# real open ocean, live-exposing an opaque black rectangle over the nodata
# region. See _fix_nlcd_background_transparency.
_NLCD_BACKGROUND_CLASS = 0

def _fix_nlcd_background_transparency(tif_bytes: bytes) -> bytes:
    """Fold NLCD's ``0`` (Background/no-coverage) pixels into the declared nodata.

    Root cause (live-verified against the real MRLC WCS endpoint 2026-07-09,
    and against GDAL's actual GTiff behavior -- NOT just the embedded table):
    GDAL's GTiff driver forces alpha=0 ONLY for the color-table entry whose
    index equals the band's DECLARED ``nodata`` value; every other entry's
    alpha is silently forced back to 255 (opaque) when the color table is
    flushed to disk, regardless of what alpha ``write_colormap`` was given.
    (Confirmed empirically: writing ``cmap[0] = (0, 0, 0, 0)`` while
    ``nodata`` stays 255 round-trips back as ``(0, 0, 0, 255)`` -- opaque --
    every time; rewriting the colormap alone can never fix this.) So the only
    reliable fix is at the PIXEL level: remap every ``0``-valued pixel to the
    raster's existing declared ``nodata`` (255 for MRLC WCS NLCD), which
    already renders transparent correctly. Class 0 is never a legitimate NLCD
    code (real codes are 11-95), so this remap can never destroy real data.
    If the raster has no declared nodata at all, ``0`` is promoted to be the
    declared nodata directly (no remap needed; GDAL's forcing behavior then
    makes index 0 transparent on its own).

    Best-effort: returns ``tif_bytes`` unchanged (never raises) if the raster
    has no embedded colormap, has no ``0``-valued pixels, or the rewrite
    fails for any reason -- this is strictly a visualization fix, not a
    correctness gate, and must never corrupt or block a real fetch.
    """
    try:
        import numpy as np  # type: ignore[import-not-found]
        import rasterio  # type: ignore[import-not-found]
        from rasterio.io import MemoryFile  # type: ignore[import-not-found]

        with MemoryFile(tif_bytes) as mem, mem.open() as src:
            cmap = _read_band1_colormap(src)
            if cmap is None:
                return tif_bytes  # not a paletted raster -- nothing to fix

            data = src.read()
            band1 = data[0]
            if not bool(np.any(band1 == _NLCD_BACKGROUND_CLASS)):
                return tif_bytes  # no background pixels present -- no-op

            nodata = src.nodata
            target_nodata = (
                float(_NLCD_BACKGROUND_CLASS) if nodata is None else float(nodata)
            )

            if int(target_nodata) == _NLCD_BACKGROUND_CLASS:
                # No declared nodata (or it's already 0) -- promote 0 itself
                # to the declared nodata; GDAL forces its alpha transparent.
                out_data = data
            else:
                # Fold background (0) into the existing nodata sentinel so
                # there is a single, already-transparent, sentinel value.
                out_data = data.copy()
                out_data[0][band1 == _NLCD_BACKGROUND_CLASS] = target_nodata

            profile = src.profile.copy()
            profile["nodata"] = target_nodata
            colorinterp = src.colorinterp
            with MemoryFile() as out_mem:
                with out_mem.open(**profile) as dst:
                    dst.write(out_data)
                    _apply_band1_colormap(dst, cmap, colorinterp)
                return out_mem.read()
    except Exception as exc:  # noqa: BLE001 -- transparency fix is best-effort
        logger.warning(
            "fetch_landcover: NLCD background-transparency fix failed (%s: %s); "
            "value-0 (ocean/no-coverage) pixels may render opaque black",
            type(exc).__name__,
            exc,
        )
        return tif_bytes

def _fetch_nlcd_landcover_bytes(
    bbox: tuple[float, float, float, float], vintage_year: int, resolution_m: int = 30
) -> bytes:
    """Fetch NLCD landcover for ``bbox`` at the given vintage year via MRLC WCS 1.0.0.

    Tier 2 access pattern (per §F.1.1) — MRLC WCS 1.0.0 ``GetCoverage`` with
    ``FORMAT=GeoTIFF`` returns the canonical NLCD class integers (11, 21, 22,
    23, 24, 31, 41, 42, 43, 51, 52, 71, 72, 73, 74, 81, 82, 90, 95) in the
    raster band — NOT palette indices. This is the job-0044 hotfix that
    unblocks job-0042's NLCD validation gate. The returned GeoTIFF carries a
    proper geo-header (EPSG:4326 in this request shape) so HydroMT's
    ``setup_manning_roughness`` consumes the bytes directly without a
    client-side palette decode.

    ``resolution_m`` controls the WCS pixel grid: at 30 m (native) each pixel is
    one NLCD cell; at coarser values (e.g. 300 m for a state-scale bbox) the grid
    shrinks to stay under the MRLC WCS server's ~4000 px-per-axis limit. Because
    NLCD is a categorical raster, nearest-neighbor resampling is implicit in the
    WCS server's pixel-addressed GetCoverage (no bilinear corruption of class codes).

    Path-comparison summary (live-verified 2026-06-07):
    - WMS GetMap: returned palette indices [1, 3, 4, 5, 6, 7, 9, 10, 11, 13,
      14, 18, 19, 20, 21] for Fort Myers -- BROKEN (Manning's mapping keyed by
      canonical integers).
    - WCS 1.0.0 GetCoverage: returned canonical integers [11, 21, 22, 23, 24,
      31, 41, 42, 43, 52, 71, 81, 82, 90, 95, 255-nodata] -- CORRECT.
    """
    _validate_bbox(bbox)
    coverage = _NLCD_WCS_COVERAGE_BY_YEAR.get(vintage_year)
    if coverage is None:
        available = sorted(_NLCD_WCS_COVERAGE_BY_YEAR.keys())
        raise UpstreamAPIError(
            f"NLCD vintage year {vintage_year} not in MRLC WCS catalog "
            f"(available: {available}); add 2023 once MRLC publishes "
            f"``mrlc_display:NLCD_2023_Land_Cover_L48`` (see OQ-39-NLCD-VINTAGE-DEFAULT)."
        )

    # Pixel grid: sized to the bbox at the requested resolution in EPSG:4326.
    # WCS 1.0.0 requires explicit WIDTH/HEIGHT (no resolution shorthand at this
    # version). At the native 30 m, clamp to 4000 px per axis (MRLC server
    # limit; beyond that the server times out or returns an exception). For
    # coarsened fetches (state-scale AOI at 300+ m) the pixel count is low and
    # the clamp is never hit.
    _res = max(1, int(resolution_m))
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mid_lat))
    width_m = (max_lon - min_lon) * m_per_deg_lon
    height_m = (max_lat - min_lat) * 111_320.0
    # MRLC pixel cap: 4000 px per axis keeps the GetCoverage inside the server's
    # stated limit. At native 30 m this caps at ~122 km/axis; at 300 m it covers
    # ~1200 km/axis, enough for any CONUS state.
    _MRLC_MAX_PX = 4000
    width_px = max(16, min(_MRLC_MAX_PX, int(round(width_m / _res))))
    height_px = max(16, min(_MRLC_MAX_PX, int(round(height_m / _res))))

    # WCS 1.0.0 GetCoverage via the shared generic OGC adapter (job-0047
    # refactor — single source of truth for §F.1.1 Tier 2 retrieval). The
    # adapter handles the WCS request shape (Coverage, CRS, BBOX, WIDTH,
    # HEIGHT, FORMAT), surfaces OGC exception XMLs as typed errors, and
    # validates the GeoTIFF content-type so a misconfigured GeoServer
    # response (HTML error page, ExceptionReport XML) doesn't poison the
    # cache. The MRLC WCS sub-protocol (1.0.0 over 1.1.1/2.0.1) was
    # established in job-0044's live-verification rounds and is preserved.
    from trid3nt_server.tools.discovery.ogc_adapter import OGCAdapterError, fetch_ogc_layer

    try:
        ogc_resp = fetch_ogc_layer(
            url=_MRLC_WCS_URL,
            layer_name=coverage,
            bbox=bbox,
            crs="EPSG:4326",
            service_type="WCS",
            image_format="GeoTIFF",
            version="1.0.0",
            width_px=width_px,
            height_px=height_px,
            timeout_s=120.0,
            user_agent=_DEFAULT_USER_AGENT,
        )
    except OGCAdapterError as exc:
        raise UpstreamAPIError(
            f"MRLC WCS GetCoverage failed for coverage={coverage} bbox={bbox}: {exc}"
        ) from exc

    # Extra defensive check: the adapter already validates content-type and
    # body length, but we re-check the TIFF content-type because the cache
    # write extension is fixed at ``.tif``.
    ct = ogc_resp.content_type
    if "tiff" not in ct.lower() and "geotiff" not in ct.lower():
        raise UpstreamAPIError(
            f"MRLC WCS returned unexpected content-type={ct!r} for coverage={coverage} "
            f"bbox={bbox}; body preview: {ogc_resp.content[:300]!r}"
        )

    # NLCD Background-class transparency fix (2026-07-09): the WCS embedded
    # color table maps class 0 to opaque black instead of transparent -- see
    # _fix_nlcd_background_transparency. Applied BEFORE the COG re-write
    # pipeline so the fixed table is what gets clipped/tiled/cached.
    fixed = _fix_nlcd_background_transparency(ogc_resp.content)

    # job-0271-class fix (F33/F39): the MRLC WCS GetCoverage GeoTIFF is a flat
    # strip-organized raster with NO overviews, so TiTiler 404s the zoomed-out
    # tiles and NLCD renders spotty / vanishes when panned out. Clip to the
    # exact bbox and re-emit a tiled COG WITH overviews before caching.
    return _landcover_bytes_to_cog(fixed, bbox)

def _fetch_esa_worldcover_bytes(
    bbox: tuple[float, float, float, float], vintage_year: int
) -> bytes:
    """Fetch ESA WorldCover landcover for ``bbox`` at the given vintage year.

    ESA WorldCover is hosted by Microsoft Planetary Computer as STAC + COG
    (Tier 1 per §F.1.1). The implementation is reserved as a forward-looking
    branch; the v0.1 substrate raises ``UpstreamAPIError`` so the agent's
    FR-AS-11 surface can decide whether to fall back to NLCD or surface to
    the user. Surface as OQ-39-ESA-WORLDCOVER-SUBSTRATE.
    """
    raise UpstreamAPIError(
        "ESA WorldCover branch is not implemented in the v0.1 substrate "
        "(reserved for a follow-up job; opt into NLCD by passing "
        "dataset='nlcd_2021' / 'nlcd_2019')."
    )

# Default NLCD vintage used both as the ``fetch_landcover`` ``dataset``
# parameter default and as the resolved value for the bare 'nlcd' / 'nlcd_'
# aliases (job-fix: model kept retrying 'nlcd' -> 'nlcd_' before landing on
# a valid 'nlcd_YYYY', re-triggering the resolution-confirm gate each time).
_DEFAULT_NLCD_DATASET = "nlcd_2021"

def _round_bbox_to_30m_nlcd(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Quantize a WGS84 bbox to the NLCD 30 m native grid.

    Per the per-source bbox quantization rule (acceptance criterion 3 of
    the kickoff): NLCD's native cell is 30 m. We reuse
    ``round_bbox_to_resolution(bbox, 30)`` — same semantics as ``fetch_dem``
    at 30 m, so dedup-via-quantization works the same way.
    """
    return round_bbox_to_resolution(bbox, 30)

@register_tool(
    _FETCH_LANDCOVER_METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (NLCD WMS + USGS 3DEP),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_landcover(
    bbox: tuple[float, float, float, float],
    dataset: str = _DEFAULT_NLCD_DATASET,
    resolution_m: int = 30,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Fetch landcover classification raster (NLCD or ESA WorldCover) for a bbox.

    Access pattern: Tier 2 (OGC service — MRLC WCS/WMS endpoint per §F.1.1; live
    verification 2026-06-07 found NLCD is Tier 2, see OQ-39-NLCD-TIER-DEVIATION).

    **What it does:** Downloads an NLCD or ESA WorldCover landcover GeoTIFF
    clipped to the requested bbox via the MRLC WCS 1.0.0 GeoServer endpoint.
    Returns a dict containing a ``LayerURI`` plus a ``nlcd_vintage_year``
    sidecar field that downstream SFINCS setup uses to validate Manning's
    roughness mappings before HydroMT invocation (Invariant 7 — no silent
    wrong answers).

    **When to use:**
    - ``build_sfincs_model`` requires landcover for Manning's roughness
      assignment — this is the canonical supply tool.
    - User asks "what land cover exists in this area?" for a CONUS location.
    - Exposure analysis: intersect a hazard footprint with impervious-surface
      or developed-land classes.
    - Visualization using the ``categorical_landcover`` QML style preset.

    **When NOT to use:**
    - Coverage outside CONUS L48 -- NLCD covers only the 48 contiguous US
      states; Alaska, Hawaii, and Puerto Rico have separate MRLC layers not
      in the v0.1 substrate.
    - Global landcover -- pass ``dataset="esa_worldcover_2021"`` to opt into
      the ESA WorldCover branch, but that branch currently raises
      ``UpstreamAPIError`` (forward-looking, OQ-39-ESA-WORLDCOVER-SUBSTRATE).
    - Single-point landcover classification -- this tool returns a raster;
      use ``extract_landcover_class`` for point lookups once it lands.
    - Continent-scale bboxes (> 5,000,000 km^2) -- the tool raises
      ``BboxInvalidError`` at that hard ceiling. State-scale and
      multi-state-scale bboxes are served by auto-coarsening the resolution
      (the fetch-resolution gate asks the user to confirm the coarsened rung
      before the MRLC WCS GetCoverage is issued).

    **Parameters:**
    - ``bbox`` (tuple[float,float,float,float]): ``(min_lon, min_lat, max_lon,
      max_lat)`` in EPSG:4326. Continent-scale bboxes (> 5e6 km^2) are
      rejected; all other sizes are served at auto-coarsened resolution.
    - ``dataset`` (str, default ``"nlcd_2021"``): ``"nlcd"`` (default vintage)
      or ``"nlcd_YYYY"`` (e.g. ``"nlcd_2021"``, ``"nlcd_2019"``,
      ``"nlcd_2016"``) or ``"esa_worldcover_2021"`` (forward-looking). Bare
      ``"nlcd"`` and ``"nlcd_"`` are accepted as aliases for the default
      vintage. Valid NLCD years: 2001, 2004, 2006, 2008, 2011, 2013, 2016,
      2019, 2021.
    - ``resolution_m`` (int, default 30): pixel grid spacing in meters.
      The fetch-resolution gate auto-coarsens this for large bboxes and
      asks the user to confirm before downloading. The native NLCD grid
      is 30 m; coarser values (60, 120, 300, 600 m) are used for
      state-scale or multi-state-scale AOIs.

    **Returns:**
    A dict with keys:
    - ``layer`` (LayerURI): COG at
      ``s3://trid3nt-cache/cache/static-30d/landcover/<key>.tif``;
      ``style_preset="categorical_landcover"``, ``units="nlcd_class_code"``.
    - ``nlcd_vintage_year`` (int): vintage year consumed by
      ``build_sfincs_model`` to validate the Manning's mapping CSV.
    - ``dataset`` (str): echo of the input dataset string for provenance.
    - ``source`` (str): ``"mrlc-wcs"`` for NLCD.
    - ``effective_resolution_m`` (int): actual pixel spacing used (equals
      ``resolution_m`` when at native 30 m; coarser when the bbox was large).
    - ``native_resolution_m`` (int): NLCD native resolution (30 m).
    - ``downsampled`` (bool): True when ``effective_resolution_m > native_resolution_m``.

    **Cross-tool dependencies:**
    - Upstream: ``geocode_location`` for bbox derivation.
    - Downstream: ``build_sfincs_model`` (Manning's roughness), QGIS Server
      WMS rendering, ``extract_landcover_class``, ``compute_impervious_surface``.
    """
    if not isinstance(dataset, str) or not dataset:
        raise BboxInvalidError(
            f"fetch_landcover requires a non-empty dataset string; got {dataset!r}"
        )

    # Alias resolution: models frequently call this with bare 'nlcd' (no
    # vintage) or a stray trailing-underscore 'nlcd_' before landing on a
    # valid 'nlcd_YYYY' -- each of those was a typed error that forced a
    # retry, and every retry re-triggered the resolution-confirm gate on
    # the same bbox (see turn-memory fix in server.py). Treat both as
    # aliases for the default vintage; an explicit 'nlcd_YYYY' still wins.
    normalized_dataset = dataset.strip().lower()
    if normalized_dataset in ("nlcd", "nlcd_"):
        dataset = _DEFAULT_NLCD_DATASET

    # Pixel-budget constants for MRLC WCS auto-coarsening.
    # PIXEL_BUDGET: max pixels per side we request from the MRLC WCS server
    # (4000 keeps a margin under the ~4096 cap the server enforces).
    _PIXEL_BUDGET = 4000
    _NATIVE_RES_M = 30

    if dataset.startswith("nlcd_"):
        try:
            vintage_year = int(dataset.split("_", 1)[1])
        except (IndexError, ValueError) as exc:
            raise BboxInvalidError(
                f"could not parse NLCD vintage year from dataset={dataset!r}; "
                "expected 'nlcd_YYYY' (e.g. 'nlcd_2021')."
            ) from exc

        # Hard ceiling: continent-scale bboxes (> 5e6 km^2) are refused.
        # Everything below that is served at auto-coarsened resolution.
        rough_area = _bbox_area_km2(bbox)
        if rough_area > 5_000_000.0:
            raise BboxInvalidError(
                f"bbox area {rough_area:.1f} km^2 exceeds the 5,000,000 km^2 hard "
                "ceiling for fetch_landcover (continent-scale; split into sub-regions)."
            )

        # Compute the effective resolution from the gate-supplied resolution_m.
        # The gate (server.py FETCH_CONFIRM_TOOLS) auto-coarsens for large bboxes
        # and injects a confirmed resolution_m; we honour it here. If the gate
        # was bypassed (e.g. a small bbox or a direct call), use the supplied
        # resolution_m as-is, but floor it at 30 m (never finer than native).
        effective_res = max(_NATIVE_RES_M, int(resolution_m))

        # Enforce the MRLC pixel budget on the RESOLUTION (not just the px
        # clamp inside _fetch_nlcd_landcover_bytes): if the bbox at
        # effective_res would exceed _PIXEL_BUDGET px on the long axis, coarsen
        # to fit. This keeps effective_resolution_m HONEST -- it always
        # describes the grid actually delivered, even when the gate was
        # bypassed (direct call, tests, small-model shortcut) with a rung too
        # fine for the AOI. Nearest-neighbor semantics hold: the WCS pixel-
        # addressed GetCoverage samples class codes, never interpolates.
        min_lon, min_lat, max_lon, max_lat = bbox
        mid_lat = 0.5 * (min_lat + max_lat)
        m_per_deg_lon = 111_320.0 * max(0.05, math.cos(math.radians(mid_lat)))
        long_axis_m = max(
            (max_lon - min_lon) * m_per_deg_lon,
            (max_lat - min_lat) * 111_320.0,
        )
        budget_res = int(math.ceil(long_axis_m / _PIXEL_BUDGET))
        effective_res = max(effective_res, budget_res)
        downsampled = effective_res > _NATIVE_RES_M

        # Quantize to the effective resolution grid for cache-key stability.
        quantized = round_bbox_to_resolution(bbox, effective_res)

        # Cache-key source tag is ``mrlc-wcs`` after job-0044's hotfix; the
        # palette-encoded ``mrlc-wms`` entries from job-0039 land under a
        # different key and naturally evict on the 30-day TTL -- no explicit
        # invalidation needed (cached COG migration is a no-op).
        # STALE-CACHE fix (job-0324 follow-up): the ``cache_version`` salt makes
        # the post-fix key differ from the pre-fix (palette-less) entry, so this
        # fetch MISSES the stale COG and regenerates a colored, palette-
        # preserving one. Landcover-only -- see _LANDCOVER_CACHE_VERSION.
        params = {
            "bbox": list(quantized),
            "dataset": dataset,
            "source": "mrlc-wcs",
            "resolution_m": effective_res,
            "cache_version": _LANDCOVER_CACHE_VERSION,
        }
        result = read_through(
            metadata=_FETCH_LANDCOVER_METADATA,
            params=params,
            ext="tif",
            fetch_fn=lambda: _fetch_nlcd_landcover_bytes(quantized, vintage_year, effective_res),
        )
        assert result.uri is not None
        res_suffix = f"-{effective_res}m" if downsampled else ""
        layer = LayerURI(
            layer_id=f"landcover-{quantized[0]:.4f}-{quantized[1]:.4f}-{dataset}{res_suffix}",
            name=f"NLCD Land Cover ({vintage_year})" + (f" at {effective_res} m" if downsampled else ""),
            layer_type="raster",
            uri=result.uri,
            style_preset="categorical_landcover",
            role="input",
            units="nlcd_class_code",
        )
        out: dict[str, Any] = {
            "layer": layer,
            "nlcd_vintage_year": vintage_year,
            "dataset": dataset,
            "source": "mrlc-wcs",
            "effective_resolution_m": effective_res,
            "native_resolution_m": _NATIVE_RES_M,
            "downsampled": downsampled,
        }
        if downsampled:
            out["downsampling_note"] = (
                f"Landcover fetched at {effective_res} m (coarsened from {_NATIVE_RES_M} m native). "
                "NLCD class codes are preserved (nearest-neighbor resampling via WCS pixel grid). "
                "Category boundaries are approximate at this scale."
            )
        return out

    if dataset.startswith("esa_worldcover_"):
        try:
            vintage_year = int(dataset.rsplit("_", 1)[1])
        except (IndexError, ValueError) as exc:
            raise BboxInvalidError(
                f"could not parse ESA WorldCover vintage year from dataset={dataset!r}; "
                "expected 'esa_worldcover_YYYY' (e.g. 'esa_worldcover_2021')."
            ) from exc
        quantized = round_bbox_to_resolution(bbox, 10)  # ESA WorldCover is 10 m native
        params = {"bbox": list(quantized), "dataset": dataset, "source": "esa-worldcover-stac"}
        result = read_through(
            metadata=_FETCH_LANDCOVER_METADATA,
            params=params,
            ext="tif",
            fetch_fn=lambda: _fetch_esa_worldcover_bytes(quantized, vintage_year),
        )
        assert result.uri is not None
        layer = LayerURI(
            layer_id=f"landcover-{quantized[0]:.4f}-{quantized[1]:.4f}-{dataset}",
            name=f"ESA WorldCover ({vintage_year})",
            layer_type="raster",
            uri=result.uri,
            style_preset="categorical_landcover",
            role="input",
            units="esa_worldcover_class_code",
        )
        return {
            "layer": layer,
            "nlcd_vintage_year": None,  # ESA WorldCover is not NLCD
            "esa_worldcover_vintage_year": vintage_year,
            "dataset": dataset,
            "source": "esa-worldcover-stac",
        }

    raise BboxInvalidError(
        f"unsupported dataset={dataset!r}; allowed: 'nlcd' (default vintage, "
        f"currently {_DEFAULT_NLCD_DATASET!r}) or 'nlcd_YYYY' (Tier-1 CONUS), "
        "'esa_worldcover_' (opt-in, forward-looking - not implemented)."
    )
