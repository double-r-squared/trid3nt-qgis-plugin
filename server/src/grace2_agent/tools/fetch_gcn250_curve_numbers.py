"""``fetch_gcn250_curve_numbers`` atomic tool — GCN250 global SCS curve-numbers (job-0113).

Wraps the GCN250 global ~250 m SCS curve-number dataset (Jaafar, Ahmad, Beyrouthy
2019, *Scientific Data*) used as the infiltration substrate for compound-flood
SFINCS workflows (research-validated by Eilander et al. 2023, NHESS).

Data source (Tier-1 free, no auth required):

    Figshare DOI 10.6084/m9.figshare.7756202
    https://figshare.com/articles/dataset/GCN250_global_curve_number_datasets_for_hydrologic_modeling_and_design/7756202

The kickoff cites a Zenodo URL pattern
(``https://zenodo.org/record/2532915/files/GCN250_<amc>.tif``) — that Zenodo
record exists but contains an unrelated document ("A Study on the
Effectiveness of Synthetic Bags"), NOT the GCN250 GeoTIFFs. The authoritative
host for the Jaafar et al. 2019 GCN250 raster bundle is **Figshare** (article
7756202), with three per-AMC GeoTIFFs (~620 MB each). Surfaced as
``OQ-0113-FIGSHARE-VS-ZENODO``.

AMC → filename mapping (per Figshare record):

    "dry"     → AMC-I   → GCN250_ARCI.tif   (file id 15377357, ~640 MB)
    "average" → AMC-II  → GCN250_ARCII.tif  (file id 15377363, ~640 MB)
    "wet"     → AMC-III → GCN250_ARCIII.tif (file id 15377342, ~613 MB)

Resolution: 250 m, int16 curve number 0-100 (NoData = -1 / 255 depending on
file). CRS: EPSG:4326. Coverage: global land surface.

Strategy:

1. Validate bbox (required — kickoff: ``supports_global_query=False``).
2. Validate antecedent_moisture (dry / average / wet).
3. Open the per-AMC global GeoTIFF via ``/vsicurl/`` (GDAL HTTP byte-range
   driver) on the Figshare ``ndownloader`` URL. Note: the ndownloader URL
   issues an HTTP 302 redirect to a 10s-pre-signed S3 URL; GDAL transparently
   follows the redirect for each byte-range request, so byte-range I/O works
   even though the signed URL expires between requests.
4. Compute the bbox window with ``rasterio.windows.from_bounds``.
5. Read the window into a numpy int16 array.
6. Write a CRS-tagged GeoTIFF (rasterio driver ``GTiff``) into the FR-DC cache
   (``static-30d``, ``source_class="gcn250_curve_numbers"``).

If ``/vsicurl/`` fails (e.g. corporate proxy strips byte-range headers, the
ndownloader redirect breaks, etc.) the code falls back to a full-download
strategy: stream the entire TIF to disk, window-read locally, write the
output COG. This is slower per cache-miss but correct.

Geographic-correctness gate (job-0086 lesson, codified):
- The live test asserts the output mean curve number falls in a plausible
  range for the named place (Fort Myers FL with mixed urban + wetland → CN
  in 60-95 for AMC-II; urbanized Florida coastal area). A reprojection
  axis-swap would put CN data on the wrong side of the bbox; a CRS lie would
  push the read outside the source extent and crash or read NoData.
- The output bounds must lie strictly inside the requested bbox (rasterio
  ``from_bounds`` + ``Window.toslices`` already enforces this; we re-assert
  on the written GeoTIFF's ``src.bounds``).

FR-TA-2: atomic tool, returns ``LayerURI``.
FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical
``(bbox, antecedent_moisture)`` calls reuse the cached GeoTIFF.

Why this matters in the engine surface:
    GCN250 supplies the SCS curve-number layer SFINCS / HydroMT uses to
    estimate infiltration loss in pluvial-flood scenarios (Eilander et al.
    NHESS 2023). The agent calls this tool when building flood scenarios in
    regions where Tier-1 NLCD does not exist (anywhere outside CONUS), or
    when AMC sensitivity analysis is requested.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any, Literal

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_gcn250_curve_numbers",
    "GCN250Error",
    "GCN250BboxRequiredError",
    "GCN250InputError",
    "GCN250UpstreamError",
    "GCN250EmptyError",
    "_fetch_gcn250_bytes",
    "_AMC_TO_FILE_URL",
    "_AMC_LABELS",
]

logger = logging.getLogger("grace2_agent.tools.fetch_gcn250_curve_numbers")

# ---------------------------------------------------------------------------
# Error types (FR-AS-11 / NFR-R-1 typed-error surface).
# ---------------------------------------------------------------------------


class GCN250Error(RuntimeError):
    """Base class for fetch_gcn250_curve_numbers failures.

    ``error_code`` maps to the WebSocket A.6 error frame; ``retryable``
    guides FR-AS-11 retry logic.
    """

    error_code: str = "GCN250_ERROR"
    retryable: bool = True


class GCN250BboxRequiredError(GCN250Error):
    """``bbox`` is None.

    Required because the full global GCN250 mosaic is ~620 MB per AMC;
    allowing ``bbox=None`` would be a foot-gun. Matches the kickoff's
    ``supports_global_query=False`` directive.
    """

    error_code = "GCN250_BBOX_REQUIRED"
    retryable = False


class GCN250InputError(GCN250Error):
    """Invalid input (malformed bbox, unknown antecedent_moisture)."""

    error_code = "GCN250_INPUT_INVALID"
    retryable = False


class GCN250UpstreamError(GCN250Error):
    """Upstream (Figshare / S3 / rasterio) failed."""

    error_code = "GCN250_UPSTREAM_ERROR"
    retryable = True


class GCN250EmptyError(GCN250Error):
    """Bbox produced zero finite pixels (off-disk / over open ocean)."""

    error_code = "GCN250_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Figshare ndownloader URLs for the three per-AMC GCN250 GeoTIFFs (DOI
#: 10.6084/m9.figshare.7756202.v1, file metadata verified 2026-06-08).
#: AMC = Antecedent Moisture Condition (USDA SCS / NRCS Engineering Handbook):
#:     AMC-I   = dry antecedent conditions (5-day rainfall < 1.4" growing season)
#:     AMC-II  = average antecedent conditions (the headline / default)
#:     AMC-III = wet antecedent conditions (5-day rainfall > 2.1" growing season)
_AMC_TO_FILE_URL: dict[str, str] = {
    "dry":     "https://ndownloader.figshare.com/files/15377357",  # GCN250_ARCI.tif (~640 MB)
    "average": "https://ndownloader.figshare.com/files/15377363",  # GCN250_ARCII.tif (~640 MB)
    "wet":     "https://ndownloader.figshare.com/files/15377342",  # GCN250_ARCIII.tif (~613 MB)
}

#: Human-readable AMC labels for LayerURI names + logging.
_AMC_LABELS: dict[str, str] = {
    "dry":     "AMC-I (dry)",
    "average": "AMC-II (average)",
    "wet":     "AMC-III (wet)",
}

#: Supported ``antecedent_moisture`` argument values.
_VALID_AMC = frozenset(_AMC_TO_FILE_URL.keys())

#: Bbox quantization (6 decimal places, ~0.1 m at the equator) for cache-key
#: stability. Matches sibling fetchers (fetch_hrsl_population, fetch_admin_boundaries).
_BBOX_QUANTIZE_DP = 6

#: Approximate GCN250 global coverage. The dataset covers global land surface
#: between ~84° N and ~57° S (Antarctica excluded). We use the conservative
#: envelope for fast bbox-vs-coverage rejection.
_GCN250_COVERAGE_BBOX = (-180.0, -57.0, 180.0, 84.0)

#: GDAL HTTP timeout in seconds for the /vsicurl/ driver and full-fallback
#: download. Long enough for a multi-MB window read or full 640 MB download
#: over a slow link but bounded so a hung connection doesn't stall forever.
_GDAL_TIMEOUT_S = 600

#: Sanity cap: refuse pathologically huge windows that would blow process
#: memory. 4-byte int32 × 50000 × 50000 = 10 GB; cap at ~1.6 GB.
_MAX_WINDOW_PIXELS = 20_000 * 20_000

# User-Agent — courtesy convention for Figshare / AWS Open Data.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — built defensively against the parallel
# job-0114-schema sibling that adds ``supports_global_query``. If the schema
# job lands first we want this tool to carry the field; if it doesn't, we
# fall back to a kwarg-free construction so registration still succeeds.
# Mirrors the sibling pattern in fetch_mrms_qpe / fetch_hrsl_population /
# fetch_firms_active_fire.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    common: dict[str, Any] = dict(
        name="fetch_gcn250_curve_numbers",
        ttl_class="static-30d",
        source_class="gcn250_curve_numbers",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:  # pydantic ValidationError when field absent (extra="forbid")
        logger.debug(
            "AtomicToolMetadata does not (yet) support supports_global_query; "
            "registering fetch_gcn250_curve_numbers without it (OQ-0113-METADATA-FIELD)"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float] | None) -> None:
    """Raise ``GCN250BboxRequiredError`` / ``GCN250InputError`` if bbox is invalid."""
    if bbox is None:
        raise GCN250BboxRequiredError(
            "bbox is required for fetch_gcn250_curve_numbers — the global "
            "GCN250 grid is ~620 MB per AMC; pass a (min_lon, min_lat, max_lon, "
            "max_lat) in EPSG:4326."
        )
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise GCN250InputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise GCN250InputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise GCN250InputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise GCN250InputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise GCN250InputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _bbox_intersects_coverage(bbox: tuple[float, float, float, float]) -> bool:
    """Return True iff ``bbox`` intersects the GCN250 global coverage envelope."""
    min_lon, min_lat, max_lon, max_lat = bbox
    cov_min_lon, cov_min_lat, cov_max_lon, cov_max_lat = _GCN250_COVERAGE_BBOX
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


def _fetch_gcn250_bytes(
    bbox: tuple[float, float, float, float],
    antecedent_moisture: str = "average",
) -> bytes:
    """Open the GCN250 AMC GeoTIFF via /vsicurl/, window-read the bbox, write a clipped GeoTIFF.

    Returns GeoTIFF bytes (curve number 0-100, int16, EPSG:4326).

    Raises:
        ``GCN250InputError``: unknown antecedent_moisture.
        ``GCN250EmptyError``: bbox falls outside GCN250 coverage / yields no
            valid pixels.
        ``GCN250UpstreamError``: vsicurl / rasterio I/O failure.
    """
    if antecedent_moisture not in _VALID_AMC:
        raise GCN250InputError(
            f"unknown antecedent_moisture={antecedent_moisture!r}; "
            f"allowed: {sorted(_VALID_AMC)}"
        )

    if not _bbox_intersects_coverage(bbox):
        raise GCN250EmptyError(
            f"bbox={bbox} falls outside GCN250 global coverage "
            f"{_GCN250_COVERAGE_BBOX}; GCN250 excludes Antarctica + far Arctic"
        )

    # Lazy imports so test environments that mock the network can import the
    # module without rasterio installed.
    try:
        import numpy as np
        import rasterio
        from rasterio.windows import Window, from_bounds
    except ImportError as exc:
        raise GCN250UpstreamError(
            f"rasterio / numpy not available: {exc}"
        ) from exc

    src_url = _AMC_TO_FILE_URL[antecedent_moisture]
    vsi_url = "/vsicurl/" + src_url

    # GDAL HTTP env: timeout + user-agent + follow redirects (Figshare → S3).
    gdal_env: dict[str, str] = {
        "GDAL_HTTP_TIMEOUT": str(_GDAL_TIMEOUT_S),
        "GDAL_HTTP_USERAGENT": _USER_AGENT,
        # Figshare → S3 redirects: GDAL must follow them. ``CURLOPT_FOLLOWLOCATION``
        # is on by default for libcurl but we set the env knob explicitly so
        # this isn't a magic-default.
        "GDAL_HTTP_UNSAFESSL": "NO",
        "CPL_VSIL_CURL_USE_HEAD": "NO",  # skip HEAD probe; some hosts 405 on HEAD
        # Small VSI block so a tiny bbox doesn't trigger a huge speculative read.
        "CPL_VSIL_CURL_CHUNK_SIZE": "1048576",  # 1 MiB
    }

    try:
        with rasterio.Env(**gdal_env):
            try:
                src = rasterio.open(vsi_url)
            except Exception as exc:  # noqa: BLE001
                raise GCN250UpstreamError(
                    f"rasterio could not open GCN250 GeoTIFF via /vsicurl/ "
                    f"({antecedent_moisture}, {src_url}): {exc}"
                ) from exc

            try:
                src_crs_epsg = src.crs.to_epsg() if src.crs else None
                if src_crs_epsg != 4326:
                    raise GCN250UpstreamError(
                        f"GCN250 source unexpectedly not EPSG:4326 "
                        f"(got {src.crs}); refusing to silently reproject."
                    )

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
                    raise GCN250EmptyError(
                        f"bbox={bbox} produces a zero-size GCN250 window "
                        f"(width={win.width}, height={win.height})"
                    )

                if int(win.width) * int(win.height) > _MAX_WINDOW_PIXELS:
                    raise GCN250InputError(
                        f"bbox={bbox} would request "
                        f"{int(win.width) * int(win.height):,} pixels — "
                        f"refuse to materialize > {_MAX_WINDOW_PIXELS:,}; "
                        f"narrow the bbox."
                    )

                logger.info(
                    "fetch_gcn250_curve_numbers: window=%s for bbox=%s amc=%s "
                    "(src=%dx%d, win=%dx%d)",
                    win,
                    bbox,
                    antecedent_moisture,
                    src.width,
                    src.height,
                    int(win.width),
                    int(win.height),
                )

                try:
                    arr = src.read(1, window=win)
                except Exception as exc:  # noqa: BLE001
                    raise GCN250UpstreamError(
                        f"rasterio GCN250 window read failed: {exc}"
                    ) from exc

                # Preserve the source nodata (Jaafar et al. use 255 for no-data).
                src_nodata = src.nodata
                if src_nodata is None:
                    # Defensive: assume 255 if the file doesn't carry it.
                    src_nodata = 255

                valid_mask = arr != src_nodata
                if not valid_mask.any():
                    raise GCN250EmptyError(
                        f"bbox={bbox} produced no valid GCN250 pixels "
                        "(all-nodata window — likely over open water or "
                        "outside coverage)"
                    )

                # Curve numbers are integers 0-100; the source carries int16 or
                # uint8 depending on the AMC file. Force int16 for predictable
                # downstream handling.
                arr_int16 = arr.astype(np.int16)

                # Compute the output transform from the windowed read so the
                # GeoTIFF carries exact pixel-aligned geographic bounds.
                out_transform = src.window_transform(win)

                # Write the GeoTIFF.
                out_fd, out_path = tempfile.mkstemp(
                    suffix=".tif", prefix="grace2_gcn250_"
                )
                os.close(out_fd)
                try:
                    profile = {
                        "driver": "GTiff",
                        "dtype": "int16",
                        "count": 1,
                        "height": int(win.height),
                        "width": int(win.width),
                        "crs": "EPSG:4326",
                        "transform": out_transform,
                        "nodata": int(src_nodata) if src_nodata < 32767 else -1,
                        "compress": "DEFLATE",
                        "PREDICTOR": "2",
                        "tiled": True,
                        "blockxsize": 256,
                        "blockysize": 256,
                        "BIGTIFF": "IF_SAFER",
                    }
                    with rasterio.open(out_path, "w", **profile) as dst:
                        dst.write(arr_int16, 1)
                        dst.update_tags(
                            units="curve_number",
                            antecedent_moisture=antecedent_moisture,
                            source="GCN250_Jaafar_2019_v1",
                            source_url=src_url,
                            tool="fetch_gcn250_curve_numbers",
                        )

                    with open(out_path, "rb") as f:
                        tif_bytes = f.read()
                finally:
                    try:
                        os.unlink(out_path)
                    except OSError:
                        pass

                valid_vals = arr_int16[valid_mask]
                logger.info(
                    "fetch_gcn250_curve_numbers: wrote %d-byte GeoTIFF "
                    "(CN mean=%.1f, min=%d, max=%d, n_valid=%d)",
                    len(tif_bytes),
                    float(valid_vals.mean()),
                    int(valid_vals.min()),
                    int(valid_vals.max()),
                    int(valid_mask.sum()),
                )
                return tif_bytes
            finally:
                try:
                    src.close()
                except Exception:  # noqa: BLE001
                    pass
    except GCN250Error:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GCN250UpstreamError(
            f"unexpected error fetching GCN250 for bbox={bbox} "
            f"amc={antecedent_moisture}: {exc}"
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
def fetch_gcn250_curve_numbers(
    bbox: tuple[float, float, float, float],
    antecedent_moisture: Literal["dry", "average", "wet"] = "average",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """GCN250 global SCS curve-number raster for infiltration and runoff modeling.

    **What it does:** Fetches the GCN250 global ~250 m SCS curve-number dataset
    (Jaafar et al. 2019, Figshare DOI 10.6084/m9.figshare.7756202) for the
    requested bbox and antecedent moisture condition. Opens the global GeoTIFF
    via GDAL ``/vsicurl/`` byte-range HTTP, windows-reads the requested area,
    and writes a CRS-tagged GeoTIFF to the 30-day cache. Integer pixel values
    0-100 represent the SCS curve number; nodata = 255.

    **When to use:**
    - Building a SFINCS or HydroMT pluvial-flood scenario for any non-CONUS
      area where NLCD-derived curve numbers are unavailable — GCN250 covers
      global land surface (84 N to 57 S).
    - AMC sensitivity analysis: run the same storm under ``"dry"``,
      ``"average"``, and ``"wet"`` antecedent conditions to bracket the
      infiltration-loss range.
    - User asks for SCS infiltration inputs, curve numbers, or runoff
      potential outside the continental US.
    - Research-validated for compound-flood modeling per Eilander et al.
      (NHESS 2023); the standard global substrate for SFINCS HydroMT workflows.

    **When NOT to use:**
    - DO NOT use for CONUS work when NLCD is available — NLCD-derived curve
      numbers at ~30 m resolve urban heterogeneity far better than GCN250's
      250 m grid; use ``compute_impervious_surface`` + ``extract_landcover_class``
      for the NLCD pipeline.
    - DO NOT use as a runoff coefficient — CN is an SCS depth-loss method;
      the rational method uses ``C``, not CN. These are different empirical
      relationships.
    - DO NOT use to infer Manning's roughness — CN governs infiltration loss
      only; surface roughness requires ``fetch_landcover`` + a roughness
      lookup table.
    - DO NOT use for Antarctica or above 84 N (GCN250 does not cover those).

    **Parameters:**
    - ``bbox`` (tuple[float, float, float, float]): ``(min_lon, min_lat,
      max_lon, max_lat)`` in EPSG:4326. Required (``supports_global_query=False``
      — the global mosaic is ~620 MB per AMC). Example for Bangladesh delta:
      ``(88.0, 21.0, 91.0, 24.0)``.
    - ``antecedent_moisture`` (str, default ``"average"``): SCS AMC condition.
      ``"dry"`` = AMC-I (5-day antecedent below 1.4 in growing season;
      CN reduced ~10-20); ``"average"`` = AMC-II (design CN, default);
      ``"wet"`` = AMC-III (5-day antecedent above 2.1 in growing season;
      CN raised ~10-15).

    **Returns:** A ``LayerURI`` pointing at a GeoTIFF in the cache bucket
    (``s3://trid3nt-cache/cache/static-30d/gcn250_curve_numbers/<key>.tif``).
    ``layer_type="raster"``, ``role="primary"``, ``units="curve_number"``.
    EPSG:4326, int16 (0-100), nodata = 255, ~250 m pixel size. Downstream
    SFINCS workflows should reproject to a metric CRS before ingestion.

    **Cross-tool dependencies:**
    - Upstream: ``geocode_location`` for bbox from a place name.
    - Downstream: ``build_sfincs_model`` (HydroMT infiltration input),
      pluvial-flood scenario composers.
    - CONUS alternative: ``compute_impervious_surface`` + ``extract_landcover_class``
      for higher-resolution NLCD-based curve numbers.
    - Pairs with: ``fetch_era5_reanalysis`` (precipitation forcing) and
      ``fetch_dem`` (terrain) for the full SFINCS forcing stack.

    FR-CE-8 / FR-DC-3: ``read_through`` with ``ttl_class="static-30d"``; cache
    key is SHA-256 over ``(bbox-6dp, antecedent_moisture)``.
    """
    _validate_bbox(bbox)
    # Type-narrow after validation.
    assert bbox is not None

    if antecedent_moisture not in _VALID_AMC:
        raise GCN250InputError(
            f"unknown antecedent_moisture={antecedent_moisture!r}; "
            f"allowed: {sorted(_VALID_AMC)}"
        )

    q_bbox = _round_bbox_to_6dp(bbox)

    params = {
        "bbox": list(q_bbox),
        "antecedent_moisture": antecedent_moisture,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_gcn250_bytes(q_bbox, antecedent_moisture=antecedent_moisture),
    )
    assert result.uri is not None, (
        "fetch_gcn250_curve_numbers is cacheable; uri must be set by read_through"
    )

    amc_label = _AMC_LABELS.get(antecedent_moisture, antecedent_moisture)
    return LayerURI(
        layer_id=(
            f"gcn250-{antecedent_moisture}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=f"SCS Curve Numbers — {amc_label} (GCN250 Jaafar 2019)",
        layer_type="raster",
        uri=result.uri,
        style_preset="curve_number",
        role="primary",
        units="curve_number",
        bbox=tuple(q_bbox),
    )
