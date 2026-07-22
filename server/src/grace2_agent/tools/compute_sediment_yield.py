"""``compute_sediment_yield`` composer tool -- RUSLE annual soil loss (v1).

Computes the Revised Universal Soil Loss Equation (RUSLE; Renard et al. 1997,
USDA Agriculture Handbook 703) over an AOI:

    A = R * K * LS * C * P        [t/ha/yr]

where:

    R  -- rainfall-runoff erosivity (MJ mm ha^-1 h^-1 yr^-1). Caller-supplied
          ``rainfall_erosivity``; when omitted a documented CONSTANT default of
          300 is used with an HONEST note (no fake per-site precision -- 300 is
          a mid-range CONUS value; humid Southeast is ~4000+, arid West ~10-50).
    K  -- soil erodibility (t ha h ha^-1 MJ^-1 mm^-1). ``k_uri`` override, else
          ``fetch_statsgo_soils`` KFFACT (30 m CONUS), else a documented
          constant fallback (0.2) with a note.
    LS -- slope length x steepness factor, derived from the DEM (``dem_uri``
          override, else ``fetch_copernicus_dem`` GLO-30). Slope comes from the
          numpy gradient of the DEM; the classic Wischmeier & Smith (1978)
          unit-plot form is used:

              L = (lambda / 22.13) ** m
              S = 65.41*sin(theta)^2 + 4.56*sin(theta) + 0.065

          with theta the local slope angle and m the standard slope-percent
          exponent bands (>=5% -> 0.5, 3.5-5% -> 0.4, 1-3.5% -> 0.3,
          <1% -> 0.2). HONEST SIMPLIFICATION (noted in ``notes``): the slope
          length lambda is fixed at the DEM cell size (no flow-accumulation
          routing), i.e. a per-cell unit-slope-length estimate -- adequate for
          relative hot-spot mapping, not for engineering design.
    C  -- cover-management factor mapped from ``fetch_esri_landcover_10m``
          (Impact Observatory io-lulc-annual-v02 classes; ``landcover_uri``
          override) via the literature-standard table ``C_BY_IO_LULC_CLASS``
          below (Wischmeier & Smith 1978; Panagos et al. 2015 European-mean
          C-factors).
    P  -- support-practice factor, fixed at 1.0 (no terracing/contouring data).

Output: a single-band float32 COG of A in t/ha/yr (raw values -- NOT
color-baked, so downstream zonal stats read real numbers), written to the runs
bucket (or ``_output_dir`` for offline tests) and returned as a
``SedimentYieldLayerURI`` -- a ``LayerURI`` subclass (the ``FaultSourcesResult``
house pattern) carrying summary scalars + honest ``notes``. The raster renders
through the SAME publish path as every other compute_* raster tool: the
wrap-site auto-publishes the s3 COG via ``publish_layer``, whose styling seam
(``_registry_style_params``) resolves ``style_preset="sediment_yield_t_ha_yr"``
to a LOG-SCALED interval colormap (class breaks 1/5/10/50/100/500 t/ha/yr --
half-decade steps -- because soil loss spans orders of magnitude; a linear
rescale would render everything but the worst gullies as one flat color).

AOI clamp: <= 0.2 degrees per side (``SedimentYieldAoiTooLargeError`` above) --
the 10-30 m analysis is CPU/memory bounded on the agent box.

``cacheable=False`` (``ttl_class="live-no-cache"``): this is a modeling
composer, not a fetcher -- the artifact goes to the runs bucket, not the cache.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import uuid
from typing import Any

import numpy as np

from grace2_contracts.execution import LayerURI, LegendClass, LegendKey
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

__all__ = [
    "compute_sediment_yield",
    "SedimentYieldLayerURI",
    "SedimentYieldError",
    "SedimentYieldInputError",
    "SedimentYieldAoiTooLargeError",
    "SedimentYieldDependencyError",
    "SedimentYieldUpstreamError",
    "C_BY_IO_LULC_CLASS",
    "SEDIMENT_YIELD_LOG_CLASSES",
    "hex_to_rgba",
]

logger = logging.getLogger("grace2_agent.tools.compute_sediment_yield")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class SedimentYieldError(RuntimeError):
    """Base class for compute_sediment_yield failures."""

    error_code: str = "SEDIMENT_YIELD_ERROR"
    retryable: bool = True


class SedimentYieldInputError(SedimentYieldError):
    """Bad inputs (malformed bbox, bad erosivity, unreadable URI)."""

    error_code = "SEDIMENT_YIELD_INPUT_INVALID"
    retryable = False


class SedimentYieldAoiTooLargeError(SedimentYieldInputError):
    """The AOI exceeds the CPU-bound clamp (> 0.2 degrees per side)."""

    error_code = "SEDIMENT_YIELD_AOI_TOO_LARGE"
    retryable = False


class SedimentYieldDependencyError(SedimentYieldError):
    """A required library (rasterio/numpy) is unavailable."""

    error_code = "SEDIMENT_YIELD_DEPENDENCY_MISSING"
    retryable = False


class SedimentYieldUpstreamError(SedimentYieldError):
    """Input staging, upstream fetch, or artifact write failed."""

    error_code = "SEDIMENT_YIELD_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Result type -- LayerURI subclass carrying the summary (house side-channel).
# ---------------------------------------------------------------------------


class SedimentYieldLayerURI(LayerURI):
    """The RUSLE soil-loss ``LayerURI`` plus assessment summary.

    Extra fields beyond ``LayerURI``:

    - ``mean_soil_loss_t_ha_yr`` / ``max_soil_loss_t_ha_yr`` /
      ``p95_soil_loss_t_ha_yr`` -- headline statistics over valid cells.
    - ``rainfall_erosivity`` -- the R-factor actually used.
    - ``notes`` -- honest provenance + every fallback/simplification used.
    """

    mean_soil_loss_t_ha_yr: float | None = None
    max_soil_loss_t_ha_yr: float | None = None
    p95_soil_loss_t_ha_yr: float | None = None
    rainfall_erosivity: float = 300.0
    notes: list[str] = []


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: CPU/memory-bound AOI clamp (degrees per side).
_MAX_AOI_DEG: float = 0.2

#: Documented constant R-factor default (MJ mm ha^-1 h^-1 yr^-1) when the
#: caller supplies no ``rainfall_erosivity``. 300 is a mid-range CONUS value;
#: real R spans ~10 (arid West) to ~6000+ (Gulf Coast). We do NOT fake per-site
#: precision -- the honest note tells the user to pass a local value.
_DEFAULT_R: float = 300.0

#: R-factor sanity range.
_MIN_R, _MAX_R = 1.0, 20000.0

#: Documented constant K-factor fallback (t ha h ha^-1 MJ^-1 mm^-1) when
#: neither ``k_uri`` nor STATSGO KFFACT is available. 0.2 is a mid-range value
#: for medium-textured soils.
_K_CONSTANT_FALLBACK: float = 0.2

#: RUSLE unit-plot slope length (m) -- the Wischmeier & Smith standard plot.
_UNIT_PLOT_LENGTH_M: float = 22.13

#: Support-practice factor. No terracing/contouring data source -> 1.0.
_P_FACTOR: float = 1.0

#: C-factor (cover management, dimensionless 0..1) per Impact Observatory
#: io-lulc-annual-v02 class code (the ``fetch_esri_landcover_10m`` classes;
#: codes 3 and 6 are absent from the v02 schema). Literature-standard values:
#: Wischmeier & Smith 1978 (AH-537) crop/rangeland tables and Panagos et al.
#: 2015 ("Estimating the soil erosion cover-management factor at the European
#: scale", Land Use Policy 48) land-cover means:
#:
#:   1  Water              0.0    (no soil loss on open water)
#:   2  Trees              0.003  (forest with litter cover, WS78 0.001-0.009;
#:                                 Panagos forest mean ~0.001-0.003)
#:   4  Flooded vegetation 0.01   (wetland vegetation, near-full ground cover)
#:   5  Crops              0.20   (Panagos arable mean 0.233; WS78 row crops
#:                                 0.1-0.5 -> generic cropland 0.20)
#:   7  Built area         0.01   (largely impervious; pervious fraction
#:                                 lawn-like, Panagos artificial ~0.0-0.01)
#:   8  Bare ground        0.45   (bare/fallow, WS78 fallow ~0.36-0.6)
#:   9  Snow/Ice           0.0    (permanent snow/ice -> no annual soil loss)
#:  11  Rangeland          0.08   (grass/shrub, WS78 pasture/range 0.02-0.15;
#:                                 Panagos grassland mean 0.05-0.10)
#:
#: Class 0 (No Data) and 10 (Clouds) carry NO cover information -> NaN
#: (nodata in the output, never a fabricated C).
C_BY_IO_LULC_CLASS: dict[int, float] = {
    1: 0.0,
    2: 0.003,
    4: 0.01,
    5: 0.20,
    7: 0.01,
    8: 0.45,
    9: 0.0,
    11: 0.08,
}

#: LOG-SCALED render classes for the published layer: (min, max, "#rrggbb",
#: label) in t/ha/yr. Breaks at 1/5/10/50/100/500 are half-decade (log10) steps
#: -- soil loss spans orders of magnitude, so equal-color-per-decade classing
#: is the standard erosion-map convention (a linear ramp would flatten
#: everything below the worst gullies into one color). Colors are the ylorrd
#: family. ``publish_layer._registry_style_params`` turns this table into a
#: TiTiler interval ``&colormap=`` for ``style_preset="sediment_yield_t_ha_yr"``,
#: and the returned LayerURI's ``legend`` is built from the SAME table so the
#: key always matches the paint.
SEDIMENT_YIELD_LOG_CLASSES: tuple[tuple[float, float, str, str], ...] = (
    (0.0, 1.0, "#ffffcc", "< 1 (very low)"),
    (1.0, 5.0, "#ffeda0", "1-5 (low)"),
    (5.0, 10.0, "#fed976", "5-10 (moderate)"),
    (10.0, 50.0, "#feb24c", "10-50 (high)"),
    (50.0, 100.0, "#fd8d3c", "50-100 (very high)"),
    (100.0, 500.0, "#f03b20", "100-500 (severe)"),
    (500.0, 1.0e9, "#bd0026", ">= 500 (extreme)"),
)

_NODATA: float = -9999.0

_METADATA = AtomicToolMetadata(
    name="compute_sediment_yield",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


def hex_to_rgba(color: str) -> list[int]:
    """``"#rrggbb"`` -> ``[r, g, b, 255]`` (TiTiler colormap entry)."""
    c = color.lstrip("#")
    return [int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255]


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: Any) -> tuple[float, float, float, float]:
    """Validate + normalize the bbox; enforce the CPU-bound AOI clamp."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise SedimentYieldInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    try:
        west, south, east, north = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise SedimentYieldInputError(
            f"bbox contains non-numeric values: {bbox!r}"
        ) from exc
    if not all(math.isfinite(v) for v in (west, south, east, north)):
        raise SedimentYieldInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise SedimentYieldInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise SedimentYieldInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if west >= east or south >= north:
        raise SedimentYieldInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    if (east - west) > _MAX_AOI_DEG or (north - south) > _MAX_AOI_DEG:
        raise SedimentYieldAoiTooLargeError(
            f"AOI {bbox!r} exceeds the compute_sediment_yield clamp of "
            f"{_MAX_AOI_DEG} degrees per side "
            f"(got {east - west:.3f} x {north - south:.3f} deg). The 10-30 m "
            "RUSLE analysis is CPU-bounded; pick a field / small-watershed AOI."
        )
    return (west, south, east, north)


def _validate_erosivity(value: Any, notes: list[str]) -> float:
    """Resolve the R-factor: caller value or the honest constant default."""
    if value is None:
        notes.append(
            f"R-factor DEFAULT: no rainfall_erosivity supplied; using the "
            f"documented constant {_DEFAULT_R:g} MJ mm/(ha h yr) -- a coarse "
            "mid-range CONUS value, NOT a site-specific estimate (real R spans "
            "~10 in the arid West to ~6000+ on the Gulf Coast). Pass "
            "rainfall_erosivity for a locally meaningful result."
        )
        return _DEFAULT_R
    try:
        r = float(value)
    except (TypeError, ValueError) as exc:
        raise SedimentYieldInputError(
            f"rainfall_erosivity must be a number; got {value!r}"
        ) from exc
    if not math.isfinite(r) or not (_MIN_R <= r <= _MAX_R):
        raise SedimentYieldInputError(
            f"rainfall_erosivity must be in [{_MIN_R:g}, {_MAX_R:g}] "
            f"MJ mm/(ha h yr); got {value!r}"
        )
    notes.append(f"R-factor: caller-supplied rainfall_erosivity {r:g}.")
    return r


# ---------------------------------------------------------------------------
# Input staging.
# ---------------------------------------------------------------------------


def _stage_uri_local(uri: str, tmpdir: str, label: str) -> str:
    """Return a local file path for ``uri`` (s3:// download or local path)."""
    if uri.startswith("s3://"):
        from .cache import read_object_bytes_s3

        name = uri.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
        local = os.path.join(tmpdir, f"{label}_{name}")
        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise SedimentYieldUpstreamError(
                f"S3 download failed for {label} uri {uri!r}: {exc}"
            ) from exc
        with open(local, "wb") as f:
            f.write(data)
        return local
    if uri.startswith(("gs://", "http://", "https://")):
        raise SedimentYieldInputError(
            f"{label} uri scheme not supported: {uri!r} (use s3:// or a local path)"
        )
    if not os.path.exists(uri):
        raise SedimentYieldInputError(
            f"{label} uri points at a missing local file: {uri!r}"
        )
    return uri


def _open_band(path: str, label: str) -> tuple[np.ndarray, Any]:
    """Open band 1 as float64 with nodata -> NaN; return (array, dataset profile src)."""
    try:
        import rasterio
    except ImportError as exc:
        raise SedimentYieldDependencyError(f"rasterio unavailable: {exc}") from exc
    try:
        src = rasterio.open(path)
    except Exception as exc:  # noqa: BLE001
        raise SedimentYieldInputError(
            f"could not open {label} raster {path!r}: {exc}"
        ) from exc
    band = src.read(1).astype(np.float64)
    nodata = src.nodata
    if nodata is not None and math.isfinite(float(nodata)):
        band[band == float(nodata)] = np.nan
    return band, src


def _resample_to_grid(
    path: str, label: str, dem_src: Any, *, categorical: bool
) -> np.ndarray:
    """Reproject/resample ``path`` band 1 onto the DEM grid (float64, NaN nodata).

    ``categorical=True`` -> nearest neighbour (class codes must not blend);
    otherwise bilinear. An input already on the exact DEM grid passes through
    value-identical.
    """
    try:
        import rasterio
        from rasterio.warp import Resampling, reproject
    except ImportError as exc:
        raise SedimentYieldDependencyError(f"rasterio unavailable: {exc}") from exc
    band, src = _open_band(path, label)
    try:
        same_grid = (
            src.crs == dem_src.crs
            and src.transform == dem_src.transform
            and src.shape == dem_src.shape
        )
        if same_grid:
            return band
        out = np.full(dem_src.shape, np.nan, dtype=np.float64)
        reproject(
            source=band,
            destination=out,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dem_src.transform,
            dst_crs=dem_src.crs,
            src_nodata=np.nan,
            dst_nodata=np.nan,
            resampling=Resampling.nearest if categorical else Resampling.bilinear,
        )
        return out
    except SedimentYieldError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SedimentYieldUpstreamError(
            f"resampling {label} raster onto the DEM grid failed: {exc}"
        ) from exc
    finally:
        src.close()


# ---------------------------------------------------------------------------
# RUSLE factor computation.
# ---------------------------------------------------------------------------


def _cell_size_m(dem_src: Any) -> tuple[float, float]:
    """(dx_m, dy_m) cell size in METERS, handling geographic-CRS DEMs.

    Projected CRS: the transform's pixel sizes are already meters. Geographic
    CRS: degrees are converted at the raster's center latitude (dx scales with
    cos(lat)); adequate over a <=0.2-degree AOI.
    """
    t = dem_src.transform
    res_x, res_y = abs(t.a), abs(t.e)
    try:
        geographic = bool(getattr(dem_src.crs, "is_geographic", False))
    except Exception:  # noqa: BLE001
        geographic = False
    if not geographic:
        return res_x, res_y
    bounds = dem_src.bounds
    lat_c = 0.5 * (bounds.bottom + bounds.top)
    dx = res_x * 111_320.0 * max(math.cos(math.radians(lat_c)), 0.01)
    dy = res_y * 110_540.0
    return dx, dy


def _ls_factor(dem: np.ndarray, dx_m: float, dy_m: float, notes: list[str]) -> np.ndarray:
    """Wischmeier & Smith (1978) LS from the DEM gradient.

    L = (lambda/22.13)^m with lambda fixed at the cell size (documented
    no-flow-routing simplification); S = 65.41 sin^2(theta) + 4.56 sin(theta)
    + 0.065; m banded on slope percent (>=5 -> 0.5, 3.5-5 -> 0.4,
    1-3.5 -> 0.3, <1 -> 0.2).
    """
    gy, gx = np.gradient(dem, dy_m, dx_m)
    grad = np.hypot(gx, gy)  # rise/run
    theta = np.arctan(grad)
    slope_pct = grad * 100.0

    m = np.full(dem.shape, 0.2, dtype=np.float64)
    m[slope_pct >= 1.0] = 0.3
    m[slope_pct >= 3.5] = 0.4
    m[slope_pct >= 5.0] = 0.5

    lam = 0.5 * (dx_m + dy_m)
    length = (lam / _UNIT_PLOT_LENGTH_M) ** m
    sin_t = np.sin(theta)
    steep = 65.41 * sin_t * sin_t + 4.56 * sin_t + 0.065
    notes.append(
        "LS-factor: Wischmeier & Smith (1978) unit-plot form from the DEM "
        f"gradient; slope length fixed at the cell size ({lam:g} m) -- NO "
        "flow-accumulation routing (a per-cell simplification suitable for "
        "relative hot-spot mapping, not engineering design)."
    )
    return length * steep


def _load_k(
    bbox: tuple[float, float, float, float],
    k_uri: str | None,
    dem_src: Any,
    tmpdir: str,
    notes: list[str],
) -> np.ndarray:
    """K-factor grid (override / STATSGO KFFACT / constant fallback)."""
    if k_uri is not None:
        local = _stage_uri_local(k_uri, tmpdir, "k")
        k = _resample_to_grid(local, "k", dem_src, categorical=False)
        notes.append(f"K-factor from caller-supplied k_uri ({k_uri}).")
        return k
    try:
        from .fetch_statsgo_soils import fetch_statsgo_soils

        layer = fetch_statsgo_soils(bbox=bbox, field="KFFACT")
        local = _stage_uri_local(layer.uri, tmpdir, "k")
        k = _resample_to_grid(local, "k", dem_src, categorical=False)
        notes.append("K-factor: USGS STATSGO KFFACT (30 m) via fetch_statsgo_soils.")
        return k
    except Exception as exc:  # noqa: BLE001 -- documented constant fallback
        notes.append(
            f"K-factor FALLBACK: STATSGO KFFACT unavailable ({exc}); using a "
            f"constant K-factor of {_K_CONSTANT_FALLBACK} across the AOI. "
            "Pass k_uri for soil-resolved results."
        )
        return np.full(dem_src.shape, _K_CONSTANT_FALLBACK, dtype=np.float64)


def _load_c(
    bbox: tuple[float, float, float, float],
    landcover_uri: str | None,
    dem_src: Any,
    tmpdir: str,
    notes: list[str],
) -> np.ndarray:
    """C-factor grid mapped from IO LULC classes (override or fetched)."""
    if landcover_uri is not None:
        local = _stage_uri_local(landcover_uri, tmpdir, "landcover")
        source_note = f"C-factor land cover from caller-supplied landcover_uri ({landcover_uri})"
    else:
        try:
            from .fetch_esri_landcover_10m import fetch_esri_landcover_10m

            layer = fetch_esri_landcover_10m(bbox=bbox)
        except SedimentYieldError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SedimentYieldUpstreamError(
                f"fetch_esri_landcover_10m failed for bbox={bbox}: {exc}"
            ) from exc
        local = _stage_uri_local(layer.uri, tmpdir, "landcover")
        source_note = (
            "C-factor land cover: Esri/IO 10 m annual LULC via fetch_esri_landcover_10m"
        )
    classes = _resample_to_grid(local, "landcover", dem_src, categorical=True)

    c = np.full(dem_src.shape, np.nan, dtype=np.float64)
    valid = np.isfinite(classes)
    codes = np.zeros(dem_src.shape, dtype=np.int32)
    codes[valid] = np.rint(classes[valid]).astype(np.int32)
    for code, c_val in C_BY_IO_LULC_CLASS.items():
        c[valid & (codes == code)] = c_val
    if not np.isfinite(c).any():
        raise SedimentYieldUpstreamError(
            "no land-cover cell over the AOI maps to a known C-factor class "
            f"(known IO LULC codes: {sorted(C_BY_IO_LULC_CLASS)})."
        )
    notes.append(
        f"{source_note}; classes mapped to literature-standard C-factors "
        "(Wischmeier & Smith 1978; Panagos et al. 2015): "
        + ", ".join(f"{k}->{v:g}" for k, v in sorted(C_BY_IO_LULC_CLASS.items()))
        + ". No-data/cloud cells carry no C and are nodata in the output."
    )
    return c


# ---------------------------------------------------------------------------
# Output writing.
# ---------------------------------------------------------------------------


def _write_cog_bytes(
    a: np.ndarray, dem_src: Any, tmpdir: str
) -> bytes:
    """Encode the soil-loss grid as COG bytes on the DEM grid (float32)."""
    try:
        import rasterio
    except ImportError as exc:
        raise SedimentYieldDependencyError(f"rasterio unavailable: {exc}") from exc
    data = np.where(np.isfinite(a), a, _NODATA).astype(np.float32)
    path = os.path.join(tmpdir, "rusle_soil_loss.tif")
    profile = {
        "driver": "COG",
        "count": 1,
        "dtype": "float32",
        "crs": dem_src.crs,
        "transform": dem_src.transform,
        "width": dem_src.width,
        "height": dem_src.height,
        "nodata": _NODATA,
        "compress": "DEFLATE",
    }
    try:
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(data, 1)
    except Exception:  # noqa: BLE001 -- COG driver absent on old GDAL: GTiff
        profile.update({"driver": "GTiff", "tiled": True})
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(data, 1)
    with open(path, "rb") as f:
        return f.read()


def _write_output(payload: bytes, seed: str, output_dir: str | None) -> str:
    """Persist the COG; return its URI (local for tests, runs bucket live)."""
    filename = f"sediment_yield_{seed}.tif"
    if output_dir is not None:
        path = os.path.join(output_dir, filename)
        with open(path, "wb") as f:
            f.write(payload)
        return path
    try:
        from .solver import _get_runs_bucket, _get_s3_client

        bucket = _get_runs_bucket()
        key = f"sediment-yield-{seed}/{filename}"
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="image/tiff",
        )
        return f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001
        raise SedimentYieldUpstreamError(
            f"failed to upload the RUSLE soil-loss COG to the runs bucket: {exc}"
        ) from exc


def _build_legend() -> LegendKey:
    """Categorical legend built from the SAME log-class table the paint uses."""
    return LegendKey(
        kind="categorical",
        classes=[
            LegendClass(value_min=lo, value_max=hi, color=color, label=label)
            for lo, hi, color, label in SEDIMENT_YIELD_LOG_CLASSES
        ],
        units="t/ha/yr",
        label="Annual soil loss (RUSLE)",
    )


# ---------------------------------------------------------------------------
# Registered tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: writes only its own run artifact; open-world when fetching
    # DEM / STATSGO / land-cover inputs.
    open_world_hint=True,
)
def compute_sediment_yield(
    bbox: tuple[float, float, float, float],
    rainfall_erosivity: float | None = None,
    dem_uri: str | None = None,
    k_uri: str | None = None,
    landcover_uri: str | None = None,
    *,
    _output_dir: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> SedimentYieldLayerURI:
    """Map annual soil loss over an AOI with the RUSLE erosion model.

    Computes A = R * K * LS * C * P (t/ha/yr) per cell: slope (LS) from a
    fetched Copernicus GLO-30 DEM, soil erodibility (K) from STATSGO KFFACT,
    cover management (C) from Esri/IO 10 m land cover via a documented
    literature C-factor table, support practice P = 1. Returns a styled
    soil-loss raster (log-scaled color classes at 1/5/10/50/100/500 t/ha/yr).

    When to use:
        - "Where is erosion worst in this watershed", "soil loss / sediment
          yield map for these fields", "erosion risk after this land-use
          change", sediment-source screening upstream of a reservoir.
        - Pair with ``compute_zonal_statistics`` to rank fields/parcels by
          mean soil loss.

    When NOT to use:
        - In-channel sediment TRANSPORT / deposition (RUSLE is hillslope sheet
          + rill erosion only; no gully/channel/mass-wasting processes).
        - Post-fire debris flows (use ``model_debris_flow``).
        - Single-storm event loss -- RUSLE is a long-term ANNUAL average.

    Parameters:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326, clamped to
            <= 0.2 degrees per side (a field / small-watershed AOI).
        rainfall_erosivity: R-factor in MJ mm/(ha h yr). Default: a constant
            300 with an HONEST note (a coarse mid-range value -- pass the
            local R for meaningful absolute numbers; relative patterns within
            the AOI are unaffected).
        dem_uri: optional override DEM raster (s3:// or local GeoTIFF).
            Default: Copernicus GLO-30 via ``fetch_copernicus_dem``.
        k_uri: optional override K-factor raster. Default: STATSGO KFFACT via
            ``fetch_statsgo_soils``, then a constant 0.2 fallback (noted).
        landcover_uri: optional override land-cover class raster (IO LULC
            codes). Default: ``fetch_esri_landcover_10m``.

    Returns:
        ``SedimentYieldLayerURI`` -- a raster ``LayerURI`` (single-band float32
        COG, values = A in t/ha/yr; ``style_preset="sediment_yield_t_ha_yr"``,
        a log-scaled interval colormap) carrying ``mean_soil_loss_t_ha_yr`` /
        ``max_soil_loss_t_ha_yr`` / ``p95_soil_loss_t_ha_yr``,
        ``rainfall_erosivity`` (the R actually used), and honest ``notes``
        (every default/fallback/simplification).

    Errors (FR-AS-11):
        - ``SedimentYieldAoiTooLargeError`` (AOI over the 0.2-deg clamp),
        - ``SedimentYieldInputError`` (bad bbox / erosivity / unreadable URI),
        - ``SedimentYieldDependencyError`` (rasterio missing),
        - ``SedimentYieldUpstreamError`` (input fetch or artifact write failed).
    """
    q_bbox = _validate_bbox(bbox)
    notes: list[str] = []
    r_factor = _validate_erosivity(rainfall_erosivity, notes)

    try:
        import rasterio  # noqa: F401 -- fail fast with the typed error
    except ImportError as exc:
        raise SedimentYieldDependencyError(f"rasterio not importable: {exc}") from exc

    with tempfile.TemporaryDirectory(prefix="grace2_sediment_yield_") as tmpdir:
        # ---- 1. DEM (override or fetch). ---------------------------------
        if dem_uri is not None:
            dem_local = _stage_uri_local(dem_uri, tmpdir, "dem")
            notes.append(f"DEM from caller-supplied dem_uri ({dem_uri}).")
        else:
            try:
                from .fetch_copernicus_dem import fetch_copernicus_dem

                layer = fetch_copernicus_dem(bbox=q_bbox)
            except SedimentYieldError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise SedimentYieldUpstreamError(
                    f"fetch_copernicus_dem failed for bbox={q_bbox}: {exc}"
                ) from exc
            dem_local = _stage_uri_local(layer.uri, tmpdir, "dem")
            notes.append("DEM: Copernicus GLO-30 (30 m) via fetch_copernicus_dem.")
        dem, dem_src = _open_band(dem_local, "dem")
        try:
            if not np.isfinite(dem).any():
                raise SedimentYieldInputError(
                    f"DEM raster {dem_local!r} has no valid cells over the AOI."
                )

            # ---- 2. RUSLE factors. ---------------------------------------
            dx_m, dy_m = _cell_size_m(dem_src)
            ls = _ls_factor(dem, dx_m, dy_m, notes)
            k = _load_k(q_bbox, k_uri, dem_src, tmpdir, notes)
            c = _load_c(q_bbox, landcover_uri, dem_src, tmpdir, notes)

            # ---- 3. A = R * K * LS * C * P. ------------------------------
            a = r_factor * k * ls * c * _P_FACTOR
            a = np.where(np.isfinite(dem), a, np.nan)
            finite = a[np.isfinite(a)]
            if finite.size == 0:
                raise SedimentYieldUpstreamError(
                    "RUSLE produced no valid cells (inputs do not overlap the "
                    "DEM grid, or every cell is nodata/cloud)."
                )

            # ---- 4. Write the styled COG. --------------------------------
            payload = _write_cog_bytes(a, dem_src, tmpdir)
        finally:
            dem_src.close()

    seed = uuid.uuid4().hex[:8]
    uri = _write_output(payload, seed, _output_dir)

    mean_a = float(finite.mean())
    max_a = float(finite.max())
    p95_a = float(np.percentile(finite, 95))
    logger.info(
        "compute_sediment_yield: bbox=%s R=%g -> mean=%.2f max=%.2f p95=%.2f "
        "t/ha/yr (%d valid cells) notes=%s",
        q_bbox,
        r_factor,
        mean_a,
        max_a,
        p95_a,
        finite.size,
        json.dumps(notes)[:400],
    )
    return SedimentYieldLayerURI(
        layer_id=f"sediment-yield-{seed}",
        name=(
            f"RUSLE annual soil loss (R={r_factor:g}) -- "
            f"bbox ({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="raster",
        uri=uri,
        style_preset="sediment_yield_t_ha_yr",
        role="primary",
        units="t/ha/yr",
        bbox=q_bbox,
        legend=_build_legend(),
        mean_soil_loss_t_ha_yr=round(mean_a, 3),
        max_soil_loss_t_ha_yr=round(max_a, 3),
        p95_soil_loss_t_ha_yr=round(p95_a, 3),
        rainfall_erosivity=r_factor,
        notes=notes,
    )
