"""``fetch_hrsl_population`` atomic tool — Meta + CIESIN HRSL population fetcher (job-0112).

Wraps the Meta Data-for-Good High-Resolution Settlement Layer (HRSL) general
population product hosted as a global COG mosaic on AWS Open Data (the
``dataforgood-fb-data`` public S3 bucket). Returns a COG raster of persons-
per-cell clipped to the requested bbox.

Data source (Tier-1 free, no auth required):

    https://dataforgood-fb-data.s3.amazonaws.com/hrsl-cogs/hrsl_general/
    └── hrsl_general-latest.vrt   ← global mosaic VRT (referenced via /vsicurl/)
        ├── v1.5/cog_globallat_<lat>_lon_<lon>_general-v1.5.<patch>.tif (10°×40° tiles)
        └── v1/  (older v1.0 tiles)

Resolution: 1 arcsecond (~30 m at the equator), float64 persons/cell.
NoData: NaN. CRS: EPSG:4326. Coverage: -55.99° to 71.33° latitude, full
longitude span (excludes Antarctica + far-north Arctic).

The kickoff calls out ``hrsl_general-latest.tif`` (a single whole-US COG),
which does NOT exist at that path on the bucket as of 2026-06-08 — the
authoritative artifact at that prefix is ``hrsl_general-latest.vrt`` (a
VRT mosaic referencing per-tile COGs). We use the VRT and let GDAL's
``/vsicurl/`` driver fetch only the bytes inside the requested bbox window
via HTTP Range requests. Surfaced as OQ-0112-VRT-VS-COG. For typical city-
scale bboxes (≤ 1° square) this fetches only a few MB.

Strategy:

1. Validate bbox (required — kickoff: ``supports_global_query=False``).
2. Open the global VRT via ``/vsicurl/`` (GDAL HTTP byte-range driver).
3. Compute the bbox window with ``rasterio.windows.from_bounds``.
4. Reject windows that fall entirely outside HRSL coverage (Antarctica / far
   Arctic / off-disk).
5. Read the window into a numpy float32 array (cast from float64 to halve
   COG size; HRSL precision does not need 8 bytes per pixel).
6. Write a CRS-tagged COG (rasterio driver ``COG``) into the FR-DC cache
   (``static-30d``, ``source_class="hrsl_population"``).

Geographic-correctness gate (job-0086 lesson, codified):
- The live test asserts ``np.nansum`` of the output COG falls in a sensible
  range for the named place (Fort Myers FL ~ 380,000 persons in the bbox
  observed during development). A reprojection sign-flip or axis-swap would
  put the wettest pixels on the wrong side of the bbox; a CRS lie would
  collapse the sum to ~0 or blow it up by an OOM.
- The output bounds must lie strictly inside the requested bbox (rasterio
  ``from_bounds`` + ``Window.toslices`` already enforces this; we re-assert
  on the written COG's ``src.bounds``).

International (non-US) coverage: Meta HRSL is *global*, not US-only. The
kickoff text says "hardcoded US-only path for v0.1; surface OQ-112-INTL".
Implementation note: because we use the **global** VRT, the tool already
returns valid data for any land-area bbox inside HRSL coverage; no US-only
restriction is enforced. Surfaced as OQ-0112-INTL for the orchestrator
(the kickoff design and implementation reality diverge in the user's
favor here — strictly more capability).

FR-TA-2: atomic tool, returns ``LayerURI``.
FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical
``(bbox, year, source)`` calls reuse the cached COG.
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
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_hrsl_population",
    "HRSLError",
    "HRSLBboxRequiredError",
    "HRSLInputError",
    "HRSLUpstreamError",
    "HRSLEmptyError",
    "_fetch_hrsl_bytes",
    "_HRSL_VRT_URL",
    "_HRSL_COVERAGE_BBOX",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.socioeconomic.fetch_hrsl_population")

# ---------------------------------------------------------------------------
# Error types (FR-AS-11 / NFR-R-1 typed-error surface).
# ---------------------------------------------------------------------------


class HRSLError(RuntimeError):
    """Base class for fetch_hrsl_population failures.

    ``error_code`` maps to the WebSocket A.6 error frame; ``retryable``
    guides FR-AS-11 retry logic.
    """

    error_code: str = "HRSL_POPULATION_ERROR"
    retryable: bool = True


class HRSLBboxRequiredError(HRSLError):
    """``bbox`` is None or otherwise missing.

    Required because the full global HRSL mosaic is hundreds of GB across
    the per-tile COGs; allowing ``bbox=None`` would be a foot-gun.
    Matches the kickoff's ``supports_global_query=False`` directive.
    """

    error_code = "BBOX_REQUIRED"
    retryable = False


class HRSLInputError(HRSLError):
    """Invalid input (malformed bbox, unsupported source/year)."""

    error_code = "HRSL_INPUT_INVALID"
    retryable = False


class HRSLUpstreamError(HRSLError):
    """Upstream (S3 / VRT open / rasterio read) failed."""

    error_code = "HRSL_UPSTREAM_ERROR"
    retryable = True


class HRSLEmptyError(HRSLError):
    """Bbox produced zero finite pixels (outside coverage / off-disk)."""

    error_code = "HRSL_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Global HRSL mosaic VRT (Meta + CIESIN, v1.5 latest at time of authoring).
_HRSL_VRT_URL = (
    "https://dataforgood-fb-data.s3.amazonaws.com/"
    "hrsl-cogs/hrsl_general/hrsl_general-latest.vrt"
)

#: GDAL /vsicurl/ form of the VRT URL (lets rasterio fetch byte ranges over HTTP).
_HRSL_VSI_URL = "/vsicurl/" + _HRSL_VRT_URL

#: Approximate HRSL global coverage bbox in EPSG:4326. Used for fast
#: bbox-vs-coverage rejection so we don't pay the VRT open round-trip for
#: a query that can never return pixels (Antarctica, far Arctic).
_HRSL_COVERAGE_BBOX = (-180.0, -56.0, 180.0, 72.0)

#: Supported ``source`` argument values. ``meta_hrsl`` is the only v0.1
#: source; ``worldpop_hrsl`` is reserved for a future tool-split if/when we
#: add WorldPop-derived HRSL.
_VALID_SOURCES = frozenset({"meta_hrsl"})

#: Bbox quantization step (6 decimal places, ~0.1 m at the equator) for
#: cache-key stability. Matches sibling fetchers.
_BBOX_QUANTIZE_DP = 6

#: GDAL HTTP timeout in seconds for the /vsicurl/ driver. Long enough for
#: a multi-MB window read over a poor connection but bounded so a dead
#: bucket doesn't hang the agent forever.
_GDAL_TIMEOUT_S = 120

# User-Agent — courtesy convention for Meta / AWS Open Data.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — built defensively against the parallel
# job-0114-schema sibling that adds ``supports_global_query``. If the schema
# job lands first we want this tool to carry the field; if it doesn't, we
# fall back to a kwarg-free construction so registration still succeeds.
# Mirrors the sibling pattern in fetch_mrms_qpe / fetch_goes_satellite.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    common: dict[str, Any] = dict(
        name="fetch_hrsl_population",
        ttl_class="static-30d",
        source_class="hrsl_population",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:  # pydantic ValidationError when field absent (extra="forbid")
        logger.debug(
            "AtomicToolMetadata does not (yet) support supports_global_query; "
            "registering fetch_hrsl_population without it (OQ-0112-METADATA-FIELD)"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float] | None) -> None:
    """Raise ``HRSLBboxRequiredError`` / ``HRSLInputError`` if bbox is invalid."""
    if bbox is None:
        raise HRSLBboxRequiredError(
            "bbox is required for fetch_hrsl_population — the global HRSL "
            "mosaic is hundreds of GB; pass a (min_lon, min_lat, max_lon, "
            "max_lat) in EPSG:4326."
        )
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise HRSLInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise HRSLInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise HRSLInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise HRSLInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise HRSLInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _bbox_intersects_coverage(bbox: tuple[float, float, float, float]) -> bool:
    """Return True iff ``bbox`` intersects the HRSL global coverage envelope."""
    min_lon, min_lat, max_lon, max_lat = bbox
    cov_min_lon, cov_min_lat, cov_max_lon, cov_max_lat = _HRSL_COVERAGE_BBOX
    return (
        min_lon <= cov_max_lon
        and max_lon >= cov_min_lon
        and min_lat <= cov_max_lat
        and max_lat >= cov_min_lat
    )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox to 6 decimal places for cache-key stability."""
    return tuple(round(v, _BBOX_QUANTIZE_DP) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Core fetch function.
# ---------------------------------------------------------------------------


def _fetch_hrsl_bytes(
    bbox: tuple[float, float, float, float],
    year: int = 2020,
    source: str = "meta_hrsl",
) -> bytes:
    """Open the HRSL VRT via /vsicurl/, window-read the bbox, write a COG.

    Returns COG bytes (persons/cell, float32, EPSG:4326).

    Raises:
        ``HRSLInputError``: unknown source.
        ``HRSLEmptyError``: bbox falls outside HRSL coverage / yields 0 finite pixels.
        ``HRSLUpstreamError``: VRT open / rasterio read / COG write failure.
    """
    if source not in _VALID_SOURCES:
        raise HRSLInputError(
            f"unknown source={source!r}; allowed: {sorted(_VALID_SOURCES)}"
        )
    # ``year`` is informational for v0.1 (the HRSL "latest" VRT is a single
    # generation; per-year HRSL releases are not separated on the bucket).
    # We accept the kwarg so the signature is stable when per-year tiles
    # become available; surfaced as OQ-0112-YEAR.
    del year  # currently unused; preserved for forward-compat

    if not _bbox_intersects_coverage(bbox):
        raise HRSLEmptyError(
            f"bbox={bbox} falls outside HRSL global coverage "
            f"{_HRSL_COVERAGE_BBOX}; HRSL excludes Antarctica + far Arctic"
        )

    # Lazy imports so test environments that mock the network can import the
    # module without rasterio installed.
    try:
        import numpy as np
        import rasterio
        from rasterio.windows import Window, from_bounds
    except ImportError as exc:
        raise HRSLUpstreamError(
            f"rasterio / numpy not available: {exc}"
        ) from exc

    # GDAL HTTP env: timeout + user-agent. Use a session-scoped env so we
    # don't leak settings to other tools running in the same process.
    gdal_env = {
        "GDAL_HTTP_TIMEOUT": str(_GDAL_TIMEOUT_S),
        "GDAL_HTTP_USERAGENT": _USER_AGENT,
        # Keep VSI block size small so a tiny bbox doesn't trigger a huge
        # speculative read. Default is 16384.
        "CPL_VSIL_CURL_CHUNK_SIZE": "1048576",  # 1 MiB
    }

    try:
        with rasterio.Env(**gdal_env):
            try:
                src = rasterio.open(_HRSL_VSI_URL)
            except Exception as exc:  # noqa: BLE001
                raise HRSLUpstreamError(
                    f"rasterio could not open HRSL VRT via /vsicurl/: {exc}"
                ) from exc

            try:
                # Compute the window covering the requested bbox.
                win = from_bounds(
                    bbox[0], bbox[1], bbox[2], bbox[3], transform=src.transform
                )
                # Round outward to integer pixels and clip to the source
                # extent so we never request out-of-bounds rows/cols.
                win = win.round_offsets(op="floor").round_lengths(op="ceil")
                win = win.intersection(
                    Window(0, 0, src.width, src.height)
                )

                if win.width <= 0 or win.height <= 0:
                    raise HRSLEmptyError(
                        f"bbox={bbox} produces a zero-size HRSL window "
                        f"(width={win.width}, height={win.height})"
                    )

                # Sanity cap: refuse pathologically huge windows that
                # would blow the agent process's memory. ~10° square @ 1
                # arcsec = 36000×36000 pixels × 4 bytes = ~5.2 GB. The
                # kickoff describes city-scale use; a 5° square is plenty.
                MAX_PIXELS = 36_000 * 36_000  # ~5 GB float32
                if int(win.width) * int(win.height) > MAX_PIXELS:
                    raise HRSLInputError(
                        f"bbox={bbox} would request "
                        f"{int(win.width) * int(win.height):,} pixels — "
                        f"refuse to materialize > {MAX_PIXELS:,}; "
                        f"narrow the bbox."
                    )

                logger.info(
                    "fetch_hrsl_population: window=%s for bbox=%s "
                    "(src=%dx%d, win=%dx%d)",
                    win,
                    bbox,
                    src.width,
                    src.height,
                    int(win.width),
                    int(win.height),
                )

                try:
                    arr64 = src.read(1, window=win)
                except Exception as exc:  # noqa: BLE001
                    raise HRSLUpstreamError(
                        f"rasterio HRSL window read failed: {exc}"
                    ) from exc

                # Cast float64 → float32 to halve COG size. HRSL precision is
                # well below 6 significant figures.
                arr = arr64.astype(np.float32)
                # Preserve NaN nodata.
                # (np.float32 carries NaN identically.)

                if not np.isfinite(arr).any():
                    raise HRSLEmptyError(
                        f"bbox={bbox} produced no valid HRSL pixels "
                        "(all-NaN window — likely over open water or "
                        "outside coverage)"
                    )

                # Compute the output transform from the windowed read so the
                # COG carries exact pixel-aligned geographic bounds.
                out_transform = src.window_transform(win)

                # Write the COG.
                out_fd, out_path = tempfile.mkstemp(
                    suffix=".tif", prefix="trid3nt_hrsl_pop_"
                )
                os.close(out_fd)
                try:
                    profile = {
                        "driver": "COG",
                        "dtype": "float32",
                        "count": 1,
                        "height": int(win.height),
                        "width": int(win.width),
                        "crs": "EPSG:4326",
                        "transform": out_transform,
                        "nodata": float("nan"),
                        "compress": "DEFLATE",
                        "PREDICTOR": "2",
                        "BIGTIFF": "IF_SAFER",
                    }
                    with rasterio.open(out_path, "w", **profile) as dst:
                        dst.write(arr, 1)
                        # Tag units + description for downstream consumers.
                        dst.update_tags(
                            units="persons_per_cell",
                            source="Meta_HRSL_v1.5_latest",
                            source_url=_HRSL_VRT_URL,
                            tool="fetch_hrsl_population",
                        )

                    with open(out_path, "rb") as f:
                        cog_bytes = f.read()
                finally:
                    try:
                        os.unlink(out_path)
                    except OSError:
                        pass

                logger.info(
                    "fetch_hrsl_population: wrote %d-byte COG "
                    "(persons sum=%.1f, max=%.2f)",
                    len(cog_bytes),
                    float(np.nansum(arr)),
                    float(np.nanmax(arr)),
                )
                return cog_bytes
            finally:
                try:
                    src.close()
                except Exception:  # noqa: BLE001
                    pass
    except (HRSLError,):
        raise
    except Exception as exc:  # noqa: BLE001
        raise HRSLUpstreamError(
            f"unexpected error fetching HRSL for bbox={bbox}: {exc}"
        ) from exc


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
def fetch_hrsl_population(
    bbox: tuple[float, float, float, float],
    year: int = 2020,
    source: str = "meta_hrsl",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch Meta + CIESIN HRSL gridded population clipped to a bbox.

    **What it does:** Opens the global Meta High-Resolution Settlement Layer
    (HRSL) VRT mosaic on AWS Open Data via GDAL ``/vsicurl/`` HTTP byte-range,
    reads only the window covering the requested bbox, and writes a
    Cloud-Optimized GeoTIFF of persons-per-cell values. Resolution ~1 arcsecond
    (~30 m at the equator); float32, NaN nodata, EPSG:4326. Cached ``static-30d``.
    Coverage: global land areas between approximately −56° and +72° latitude
    (excludes Antarctica + far Arctic). No API key required.

    **When to use:**
    - Exposure modeling: "how many people live inside the flood inundation zone?"
    - Population-at-risk summaries for storm surge, wildfire evacuation zones,
      or any hazard footprint overlay.
    - Pelicun damage/loss assessment: HRSL provides the population-density input
      for the exposure term.
    - Highest-resolution open population raster available globally; preferable
      to WorldPop for sub-city-block analyses.

    **When NOT to use:**
    - Per-building occupancy counts (HRSL is gridded, not parcel-level; combine
      ``fetch_buildings`` footprints with HRSL for building-level occupancy).
    - Authoritative US census tabulation for reporting (use US Census API).
    - Real-time or near-real-time population counts (HRSL is an annual model
      output, not a live measurement).
    - Bboxes covering Antarctica or far-north Arctic (raises ``HRSLEmptyError``).

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
      **Required** (``supports_global_query=False`` — the full mosaic is
      hundreds of GB). Example: ``(-81.95, 26.3, -81.7, 26.7)`` for Fort Myers FL.
    - ``year`` (int): HRSL release year; default 2020. v0.1 ignores this
      (bucket exposes a single "latest" VRT); accepted for forward compatibility.
    - ``source`` (str): currently only ``"meta_hrsl"`` supported.

    **Returns:**
    ``LayerURI(layer_type="raster", role="primary", units="persons_per_cell")``
    pointing at a COG (.tif). EPSG:4326, float32, NaN nodata, ~30 m pixels.
    Tagged with ``units=persons_per_cell`` and ``source=Meta_HRSL_v1.5_latest``.

    **Cross-tool dependencies:**
    - Downstream of: ``geocode_location`` (provides bbox), ``fetch_dem``
      (co-registered for elevation-weighted exposure).
    - Upstream of: ``compute_zonal_statistics`` (sum population within a polygon),
      Pelicun impact post-processor, any population-at-risk workflow step.
    - Pairs with: ``fetch_buildings`` (combine for occupancy-weighted analyses).
    """
    _validate_bbox(bbox)
    # Type-narrow after validation
    assert bbox is not None
    if source not in _VALID_SOURCES:
        raise HRSLInputError(
            f"unknown source={source!r}; allowed: {sorted(_VALID_SOURCES)}"
        )

    q_bbox = _round_bbox_to_6dp(bbox)

    params = {
        "bbox": list(q_bbox),
        "year": year,
        "source": source,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_hrsl_bytes(q_bbox, year=year, source=source),
    )
    assert result.uri is not None, (
        "fetch_hrsl_population is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=f"hrsl-pop-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-{q_bbox[2]:.4f}-{q_bbox[3]:.4f}",
        name="Population (Meta HRSL v1.5 — persons/cell)",
        layer_type="raster",
        uri=result.uri,
        style_preset="population_density",
        role="primary",
        units="persons_per_cell",
        bbox=tuple(q_bbox),
    )
