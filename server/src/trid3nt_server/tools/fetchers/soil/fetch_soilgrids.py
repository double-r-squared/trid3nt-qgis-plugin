"""``fetch_soilgrids`` atomic tool -- ISRIC SoilGrids 2.0 global soil properties.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any, Literal

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_soilgrids",
    "estimate_payload_mb",
    "SoilGridsError",
    "SoilGridsBboxRequiredError",
    "SoilGridsInputError",
    "SoilGridsUpstreamError",
    "SoilGridsEmptyError",
    "_fetch_soilgrids_bytes",
    "_PROPERTIES",
    "_DEPTHS",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.soil.fetch_soilgrids")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 / NFR-R-1 typed-error surface).
# ---------------------------------------------------------------------------


class SoilGridsError(RuntimeError):
    """Base class for fetch_soilgrids failures.

    ``error_code`` maps to the WebSocket A.6 error frame; ``retryable`` guides
    FR-AS-11 retry logic.
    """

    error_code: str = "SOILGRIDS_ERROR"
    retryable: bool = True


class SoilGridsBboxRequiredError(SoilGridsError):
    """``bbox`` is None.

    Required because each global SoilGrids property/depth mosaic is ~5 GB;
    allowing ``bbox=None`` would be a foot-gun. Matches ``supports_global_query
    =False``.
    """

    error_code = "SOILGRIDS_BBOX_REQUIRED"
    retryable = False


class SoilGridsInputError(SoilGridsError):
    """Invalid input (malformed/too-large bbox, unknown property or depth)."""

    error_code = "SOILGRIDS_INPUT_INVALID"
    retryable = False


class SoilGridsUpstreamError(SoilGridsError):
    """Upstream (ISRIC files.isric.org / vsicurl / rasterio) failed."""

    error_code = "SOILGRIDS_UPSTREAM_ERROR"
    retryable = True


class SoilGridsEmptyError(SoilGridsError):
    """Bbox produced zero valid pixels (off the soil land surface / over ocean).

    Honest no-coverage signal (data-source fallback norm) -- never fabricate.
    """

    error_code = "SOILGRIDS_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_BASE_URL = "https://files.isric.org/soilgrids/latest/data"

#: property -> (scale divisor recovering physical units, physical unit label,
#: human label, style_preset). The stored Int16 is fixed-point; divide to get
#: physical units (ISRIC SoilGrids FAQ "Conversion factors").
_PROPERTIES: dict[str, tuple[float, str, str, str]] = {
    # clay/sand/silt content: stored g/kg x10 -> /10 -> percent.
    "clay":  (10.0,  "percent",  "Clay content",          "soil_clay_pct"),
    "sand":  (10.0,  "percent",  "Sand content",          "soil_sand_pct"),
    "silt":  (10.0,  "percent",  "Silt content",          "soil_silt_pct"),
    # soil organic carbon: stored dg/kg x10 -> /10 -> g/kg.
    "soc":   (10.0,  "g/kg",     "Soil organic carbon",   "soil_soc"),
    # bulk density of the fine earth fraction: stored cg/cm3 x100 -> /100 -> kg/dm3.
    "bdod":  (100.0, "kg/dm3",   "Bulk density",          "soil_bdod"),
    # pH in H2O: stored pH x10 -> /10 -> pH.
    "phh2o": (10.0,  "pH",       "pH (H2O)",              "soil_phh2o"),
}

#: GlobalSoilMap IUSS standard depth intervals (the only depths SoilGrids 2.0
#: publishes). Order matters only for the error message.
_DEPTHS: tuple[str, ...] = (
    "0-5cm", "5-15cm", "15-30cm", "30-60cm", "60-100cm", "100-200cm",
)

#: Source NoData (Int16) on the SoilGrids VRTs.
_SRC_NODATA = -32768

#: Output NoData (float32) on the emitted COG.
_OUT_NODATA = -9999.0

#: SoilGrids native cell size in metres (Homolosine). Used to pick the EPSG:4326
#: target resolution (~250 m -> ~0.00225 deg; we use 0.0025 deg for a clean grid).
_TARGET_RES_DEG = 0.0025

#: Bbox area guardrail (deg^2). SoilGrids is 250 m; an AOI-scoped surface. A huge
#: bbox would window a large native array + many tiles. ~0.5 deg^2 ~ county-ish.
_MAX_BBOX_DEG2 = 0.5

#: 6-dp bbox quantization (~0.1 m) for cache-key stability (matches siblings).
_BBOX_QUANTIZE_DP = 6

#: SoilGrids global land coverage envelope (excludes Antarctica + far Arctic;
#: Homolosine grid spans roughly these lat/lon). Conservative fast-reject box.
_COVERAGE_BBOX = (-180.0, -62.0, 180.0, 84.0)

#: GDAL HTTP timeout for the /vsicurl/ open + window read.
_GDAL_TIMEOUT_S = 300

#: Sanity cap on the native window (refuse pathological reads).
_MAX_WINDOW_PIXELS = 20_000 * 20_000

_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    common: dict[str, Any] = dict(
        name="fetch_soilgrids",
        ttl_class="static-30d",
        source_class="soilgrids",
        cacheable=True,
        payload_mb_estimator_name="estimate_payload_mb",
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:  # pydantic ValidationError if field absent (extra="forbid")
        logger.debug(
            "AtomicToolMetadata lacks supports_global_query; registering "
            "fetch_soilgrids without it."
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload estimator (Wave 1.5 chat-warning gate).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    **_kw: Any,
) -> float:
    """Estimate emitted single-band float32 COG size in MB.

    A 1-band float32 DEFLATE-COG at ~250 m: a 0.5 deg^2 AOI is ~200x200 px
    ~ 160 KB raw, compresses to well under 1 MB. Scale linearly with bbox area,
    floored.
    """
    if bbox is None:
        return 0.5
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 0.5
    return max(0.1, sq_deg * 1.5)


# ---------------------------------------------------------------------------
# Input helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float] | None) -> None:
    if bbox is None:
        raise SoilGridsBboxRequiredError(
            "bbox is required for fetch_soilgrids -- each global SoilGrids "
            "property/depth mosaic is ~5 GB; pass a (min_lon, min_lat, "
            "max_lon, max_lat) in EPSG:4326."
        )
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise SoilGridsInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise SoilGridsInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise SoilGridsInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise SoilGridsInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise SoilGridsInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    area = (max_lon - min_lon) * (max_lat - min_lat)
    if area > _MAX_BBOX_DEG2:
        raise SoilGridsInputError(
            f"bbox area {area:.3f} deg^2 exceeds the {_MAX_BBOX_DEG2} deg^2 "
            "guardrail for fetch_soilgrids (250 m global soil is AOI-scoped; "
            "narrow the bbox)."
        )


def _normalize_property(soil_property: str) -> str:
    if not isinstance(soil_property, str):
        raise SoilGridsInputError(
            f"property must be a string; got {soil_property!r}"
        )
    p = soil_property.strip().lower()
    # Friendly aliases for LLM-invented synonyms.
    aliases = {
        "ph": "phh2o",
        "soil_ph": "phh2o",
        "organic_carbon": "soc",
        "soil_organic_carbon": "soc",
        "bulk_density": "bdod",
        "clay_content": "clay",
        "sand_content": "sand",
        "silt_content": "silt",
    }
    p = aliases.get(p, p)
    if p not in _PROPERTIES:
        raise SoilGridsInputError(
            f"unknown property={soil_property!r}; allowed: "
            f"{sorted(_PROPERTIES)}"
        )
    return p


def _normalize_depth(depth: str) -> str:
    if not isinstance(depth, str):
        raise SoilGridsInputError(f"depth must be a string; got {depth!r}")
    d = depth.strip().lower().replace(" ", "")
    # Accept "0-5", "0-5 cm", "0_5cm" variants -> canonical "0-5cm".
    d = d.replace("_", "-")
    if d and not d.endswith("cm"):
        d = d + "cm"
    if d not in _DEPTHS:
        raise SoilGridsInputError(
            f"unknown depth={depth!r}; allowed: {list(_DEPTHS)}"
        )
    return d


def _bbox_intersects_coverage(bbox: tuple[float, float, float, float]) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    c0, c1, c2, c3 = _COVERAGE_BBOX
    return min_lon <= c2 and max_lon >= c0 and min_lat <= c3 and max_lat >= c1


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_QUANTIZE_DP) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Core fetch: open VRT -> native window read -> reproject -> scale -> COG.
# ---------------------------------------------------------------------------


def _fetch_soilgrids_bytes(
    bbox: tuple[float, float, float, float],
    soil_property: str,
    depth: str,
) -> bytes:
    """Window-read the SoilGrids VRT for ``bbox`` and return scaled float32 COG bytes.

    Raises:
        ``SoilGridsInputError``: unknown property/depth.
        ``SoilGridsEmptyError``: bbox outside coverage / all-NoData window.
        ``SoilGridsUpstreamError``: vsicurl / rasterio I/O failure.
    """
    prop = _normalize_property(soil_property)
    dep = _normalize_depth(depth)
    scale_div, unit, _label, _preset = _PROPERTIES[prop]

    if not _bbox_intersects_coverage(bbox):
        raise SoilGridsEmptyError(
            f"bbox={bbox} falls outside SoilGrids global coverage "
            f"{_COVERAGE_BBOX} (excludes Antarctica / far Arctic)."
        )

    try:
        import numpy as np
        import rasterio
        from rasterio.warp import transform_bounds, reproject, Resampling
        from rasterio.windows import from_bounds as win_from_bounds, Window
    except ImportError as exc:
        raise SoilGridsUpstreamError(
            f"rasterio / numpy not available: {exc}"
        ) from exc

    src_url = f"{_BASE_URL}/{prop}/{prop}_{dep}_mean.vrt"
    vsi_url = "/vsicurl/" + src_url

    gdal_env: dict[str, str] = {
        "GDAL_HTTP_TIMEOUT": str(_GDAL_TIMEOUT_S),
        "GDAL_HTTP_USERAGENT": _USER_AGENT,
        "CPL_VSIL_CURL_USE_HEAD": "NO",  # some hosts 405 on HEAD
        # Only follow .vrt + .tif so GDAL does not probe sidecar files.
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".vrt,.tif",
        # The VRT references a tile FOLDER; avoid an expensive directory listing.
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "VSI_CACHE": "TRUE",
    }

    try:
        with rasterio.Env(**gdal_env):
            try:
                src = rasterio.open(vsi_url)
            except Exception as exc:  # noqa: BLE001
                raise SoilGridsUpstreamError(
                    f"rasterio could not open SoilGrids VRT via /vsicurl/ "
                    f"(property={prop}, depth={dep}, {src_url}): {exc}"
                ) from exc
            try:
                if src.crs is None:
                    raise SoilGridsUpstreamError(
                        "SoilGrids VRT carries no CRS; refusing to read."
                    )
                src_nodata = src.nodata if src.nodata is not None else _SRC_NODATA

                # 4326 bbox -> source (Homolosine) bounds; densify so the curved
                # projection edges are captured, then native-window read.
                l, b, r, t = transform_bounds(
                    "EPSG:4326", src.crs, *bbox, densify_pts=21
                )
                win = win_from_bounds(
                    l, b, r, t, transform=src.transform
                ).round_offsets(op="floor").round_lengths(op="ceil")
                pad = 2
                win = Window(
                    win.col_off - pad,
                    win.row_off - pad,
                    win.width + 2 * pad,
                    win.height + 2 * pad,
                ).intersection(Window(0, 0, src.width, src.height))

                if win.width <= 0 or win.height <= 0:
                    raise SoilGridsEmptyError(
                        f"bbox={bbox} produces a zero-size SoilGrids window "
                        f"(property={prop}, depth={dep}); outside coverage."
                    )
                if int(win.width) * int(win.height) > _MAX_WINDOW_PIXELS:
                    raise SoilGridsInputError(
                        f"bbox={bbox} would request "
                        f"{int(win.width) * int(win.height):,} native pixels -- "
                        f"refuse > {_MAX_WINDOW_PIXELS:,}; narrow the bbox."
                    )

                try:
                    native = src.read(1, window=win)
                except Exception as exc:  # noqa: BLE001
                    raise SoilGridsUpstreamError(
                        f"SoilGrids native window read failed "
                        f"(property={prop}, depth={dep}): {exc}"
                    ) from exc
                native_transform = src.window_transform(win)
                native_crs = src.crs
            finally:
                try:
                    src.close()
                except Exception:  # noqa: BLE001
                    pass

        # Reproject the small native window -> EPSG:4326 target grid (~250 m).
        out_w = max(1, int(round((bbox[2] - bbox[0]) / _TARGET_RES_DEG)))
        out_h = max(1, int(round((bbox[3] - bbox[1]) / _TARGET_RES_DEG)))
        out_transform = rasterio.transform.from_bounds(
            bbox[0], bbox[1], bbox[2], bbox[3], out_w, out_h
        )
        reproj_i16 = np.full((out_h, out_w), src_nodata, dtype="int16")
        reproject(
            native,
            reproj_i16,
            src_transform=native_transform,
            src_crs=native_crs,
            dst_transform=out_transform,
            dst_crs="EPSG:4326",
            src_nodata=src_nodata,
            dst_nodata=src_nodata,
            resampling=Resampling.bilinear,
        )

        valid = reproj_i16 != src_nodata
        if not bool(valid.any()):
            raise SoilGridsEmptyError(
                f"bbox={bbox} produced no valid SoilGrids pixels "
                f"(property={prop}, depth={dep}) -- all-NoData window "
                "(likely over open water or outside the soil land surface)."
            )

        # Scale fixed-point Int16 -> physical units float32; NoData -> _OUT_NODATA.
        out = np.full((out_h, out_w), _OUT_NODATA, dtype="float32")
        out[valid] = reproj_i16[valid].astype("float32") / scale_div

        # Write a single-band float32 COG.
        out_fd, out_path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_soilgrids_")
        os.close(out_fd)
        try:
            profile = dict(
                driver="COG",
                dtype="float32",
                count=1,
                height=out_h,
                width=out_w,
                crs="EPSG:4326",
                transform=out_transform,
                nodata=_OUT_NODATA,
                compress="DEFLATE",
            )
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(out, 1)
                dst.update_tags(
                    units=unit,
                    soil_property=prop,
                    depth=dep,
                    scale_divisor=str(scale_div),
                    source="ISRIC_SoilGrids_2.0",
                    source_url=src_url,
                    tool="fetch_soilgrids",
                )
            with open(out_path, "rb") as f:
                cog_bytes = f.read()
        except Exception as exc:  # noqa: BLE001
            raise SoilGridsUpstreamError(
                f"SoilGrids COG write failed (property={prop}, depth={dep}): {exc}"
            ) from exc
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass

        vv = out[valid]
        logger.info(
            "fetch_soilgrids: property=%s depth=%s bbox=%s -> %d-byte float32 COG "
            "(%dx%d, %s mean=%.2f min=%.2f max=%.2f n_valid=%d)",
            prop, dep, bbox, len(cog_bytes), out_w, out_h, unit,
            float(vv.mean()), float(vv.min()), float(vv.max()), int(valid.sum()),
        )
        return cog_bytes

    except SoilGridsError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SoilGridsUpstreamError(
            f"unexpected error fetching SoilGrids for bbox={bbox} "
            f"property={soil_property!r} depth={depth!r}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (public ISRIC HTTP),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_soilgrids(
    bbox: tuple[float, float, float, float],
    soil_property: Literal["clay", "sand", "silt", "soc", "bdod", "phh2o"] = "clay",
    depth: str = "0-5cm",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """ISRIC SoilGrids 2.0 global soil-property raster (clay/sand/silt/soc/bdod/pH).

    **What it does:** Fetches the ISRIC SoilGrids 2.0 global ~250 m prediction of
    a soil property at a standard depth for the requested bbox. Opens the global
    SoilGrids VRT mosaic via GDAL ``/vsicurl/`` byte-range HTTP, window-reads only
    the overlapping native (Interrupted Goode Homolosine) pixels, reprojects them
    to EPSG:4326 at ~250 m, applies the property scaling to recover physical
    units, and writes a single-band float32 COG to the 30-day cache.

    This is GLOBAL -- the worldwide complement to the US-only STATSGO/SSURGO
    chain (``fetch_statsgo_soils``). Use it for soil texture, organic carbon,
    bulk density, or pH anywhere on Earth at a consistent 250 m schema.

    **Properties (``soil_property``):**
    - ``"clay"`` / ``"sand"`` / ``"silt"`` -- texture fraction, percent.
    - ``"soc"`` -- soil organic carbon, g/kg.
    - ``"bdod"`` -- bulk density of the fine earth fraction, kg/dm3.
    - ``"phh2o"`` -- pH measured in water.

    **Depths (``depth``):** GlobalSoilMap standard intervals
    ``"0-5cm"``, ``"5-15cm"``, ``"15-30cm"``, ``"30-60cm"``, ``"60-100cm"``,
    ``"100-200cm"`` (default ``"0-5cm"`` -- the surface layer that drives
    infiltration and ag rooting).

    **When to use:**
    - Soil texture / organic carbon / pH / bulk density for any NON-US (or
      cross-border) area -- agriculture, hydrology, infiltration substrate.
    - A consistent 250 m global soil layer where US STATSGO/SSURGO does not
      reach (Africa, Asia, South America, Europe).
    - Pair with ``fetch_gcn250_curve_numbers`` (infiltration CN) and
      ``fetch_dem`` (terrain) for a global hydrologic forcing stack.

    **When NOT to use:**
    - US work needing the NRCS map-unit soil survey (SSURGO ~30 m or STATSGO) --
      use ``fetch_statsgo_soils``; SoilGrids is a 250 m machine-learned
      prediction, not a surveyed map unit.
    - SCS curve numbers for runoff -- use ``fetch_gcn250_curve_numbers``.
    - Antarctica / far Arctic (SoilGrids excludes them).

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326.
      Required (``supports_global_query=False`` -- each global mosaic is ~5 GB).
      AOI-scoped (<= 0.5 deg^2).
    - ``soil_property`` (str, default ``"clay"``): one of clay/sand/silt/soc/
      bdod/phh2o (synonyms like ``"ph"``, ``"organic_carbon"``,
      ``"bulk_density"`` are accepted).
    - ``depth`` (str, default ``"0-5cm"``): one of the six standard depths.

    **Returns:** A ``LayerURI`` (``layer_type="raster"``, ``role="input"``)
    pointing at a single-band float32 COG in the ``static-30d`` / ``soilgrids``
    cache prefix. ``units`` is the physical unit (percent / g/kg / kg/dm3 / pH),
    NoData ``-9999``, ~250 m, EPSG:4326. ``style_preset`` is a per-property
    continuous token (e.g. ``"soil_clay_pct"``).

    **Data source:** ISRIC SoilGrids 2.0 (CC-BY 4.0, no API key) --
    ``https://files.isric.org/soilgrids/latest/data``.

    Honesty: an all-NoData window (ocean / off the soil surface) raises a typed
    ``SoilGridsEmptyError``; an unknown property/depth raises
    ``SoilGridsInputError`` -- never a fabricated layer.

    FR-CE-8: routed through ``read_through`` so identical
    ``(bbox, property, depth)`` calls reuse the cached COG.
    """
    _validate_bbox(bbox)
    assert bbox is not None
    prop = _normalize_property(soil_property)
    dep = _normalize_depth(depth)
    _scale, unit, label, preset = _PROPERTIES[prop]

    q_bbox = _round_bbox(bbox)

    params = {
        "bbox": list(q_bbox),
        "property": prop,
        "depth": dep,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_soilgrids_bytes(q_bbox, prop, dep),
    )
    assert result.uri is not None, (
        "fetch_soilgrids is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=(
            f"soilgrids-{prop}-{dep}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=f"SoilGrids {label} ({dep})",
        layer_type="raster",
        uri=result.uri,
        style_preset=preset,
        role="input",
        units=unit,
        bbox=tuple(q_bbox),
    )
