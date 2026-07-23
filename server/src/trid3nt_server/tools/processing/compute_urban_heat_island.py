"""``compute_urban_heat_island`` composer tool -- MODIS LST x land cover UHI analysis.

Quantifies the urban heat island (UHI) over a bbox by crossing MODIS
land-surface temperature with the Esri / Impact Observatory 10 m annual land
cover:

1. LST: ``fetch_modis_lst`` (MODIS 8-day 1 km composite, single-band float32
   COG in DEG C; ``lst_uri`` override for offline tests / precomputed grids).
2. Land cover: ``fetch_esri_landcover_10m`` (io-lulc-annual-v02 9-class
   categorical COG; ``landcover_uri`` override).
3. Resample the LST onto the LAND-COVER grid -- COARSE -> FINE, bilinear.
   Documented simplification: interpolating 1 km LST to the 10 m class grid
   adds NO thermal information below the native 1 km scale; it only aligns
   the two grids so every 10 m class pixel reads the local (smoothly
   interpolated) 1 km surface temperature. Class means therefore compare
   the KILOMETER-SCALE thermal environment of each land-cover class, which
   is exactly the standard surface-UHI (SUHI) formulation.
4. Mean LST per land-cover class + the UHI delta:

       uhi_delta_c = mean LST(Built area, class 7)
                   - mean LST(vegetation classes: Trees 2, Flooded veg 4,
                              Crops 5, Rangeland 11)

   (the canonical built-up-minus-vegetated SUHI intensity). When either side
   has no pixels over the AOI the delta is ``None`` with an honest note --
   never a fabricated number.

Returns the resampled DEG-C LST COG styled with the existing
``land_surface_temp_c`` preset (the same paint as ``fetch_modis_lst``) and
the per-class table + delta on a ``UrbanHeatIslandLayerURI`` (the
``SedimentYieldLayerURI`` house side-channel pattern).

``cacheable=False`` (``live-no-cache``): modeling composer; the artifact goes
to the runs bucket (or ``_output_dir`` for offline tests).
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import uuid
from typing import Any

import numpy as np

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

__all__ = [
    "compute_urban_heat_island",
    "UrbanHeatIslandLayerURI",
    "UhiError",
    "UhiInputError",
    "UhiAoiTooLargeError",
    "UhiUpstreamError",
    "IO_LULC_LABELS",
    "BUILT_CLASS",
    "VEGETATION_CLASSES",
]

logger = logging.getLogger("trid3nt_server.tools.processing.compute_urban_heat_island")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class UhiError(RuntimeError):
    """Base class for compute_urban_heat_island failures."""

    error_code: str = "UHI_ERROR"
    retryable: bool = True


class UhiInputError(UhiError):
    """Bad inputs (malformed bbox, unreadable URI, no overlapping data)."""

    error_code = "UHI_INPUT_INVALID"
    retryable = False


class UhiAoiTooLargeError(UhiInputError):
    """The AOI exceeds the in-process resample clamp (> 1.0 degree per side)."""

    error_code = "UHI_AOI_TOO_LARGE"
    retryable = False


class UhiUpstreamError(UhiError):
    """An input fetch, resample, or artifact write failed."""

    error_code = "UHI_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Result type -- LayerURI subclass carrying the summary (house side-channel).
# ---------------------------------------------------------------------------


class UrbanHeatIslandLayerURI(LayerURI):
    """The UHI LST ``LayerURI`` plus the per-class analysis.

    Extra fields beyond ``LayerURI``:

    - ``uhi_delta_c`` -- built-up mean LST minus vegetation mean LST (deg C);
      ``None`` when either side has no pixels over the AOI (noted).
    - ``built_mean_lst_c`` / ``vegetation_mean_lst_c`` -- the two sides of
      the delta (deg C; ``None`` when absent).
    - ``per_class_lst_c`` -- one row per land-cover class present:
      ``{class_code, label, mean_lst_c, min_lst_c, max_lst_c, pixel_count,
      share}``.
    - ``daynight`` -- which MODIS LST band was analyzed.
    - ``notes`` -- honest provenance + the coarse->fine resample note.
    """

    uhi_delta_c: float | None = None
    built_mean_lst_c: float | None = None
    vegetation_mean_lst_c: float | None = None
    per_class_lst_c: list[dict] = []
    daynight: str = "day"
    notes: list[str] = []


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Esri / Impact Observatory io-lulc-annual-v02 class labels (codes 3 and 6
#: are absent from the v02 schema). Kept local (the compute_sediment_yield
#: convention) so this module documents its own class semantics.
IO_LULC_LABELS: dict[int, str] = {
    0: "No Data",
    1: "Water",
    2: "Trees",
    4: "Flooded vegetation",
    5: "Crops",
    7: "Built area",
    8: "Bare ground",
    9: "Snow/Ice",
    10: "Clouds",
    11: "Rangeland",
}

#: Classes carrying NO surface information -- excluded from class stats.
_NO_INFO_CLASSES = frozenset({0, 10})

#: The built-up side of the UHI delta.
BUILT_CLASS = 7

#: The vegetated reference side of the UHI delta (union): Trees, Flooded
#: vegetation, Crops, Rangeland -- the standard vegetated/rural reference for
#: surface-UHI intensity.
VEGETATION_CLASSES = frozenset({2, 4, 5, 11})

#: In-process resample clamp (degrees per side): the LST is interpolated onto
#: the 10 m class grid, so the working array is the LAND-COVER grid (already
#: px-clamped by the fetcher, but a metro-scale AOI keeps memory + runtime
#: comfortably bounded).
_MAX_AOI_DEG = 1.0

#: Hard defensive ceiling on the reference grid (cells per axis).
_MAX_GRID_PX = 4096

_NODATA = -9999.0

_STYLE_PRESET = "land_surface_temp_c"  # the fetch_modis_lst paint

_METADATA = AtomicToolMetadata(
    name="compute_urban_heat_island",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


# ---------------------------------------------------------------------------
# Validation + staging helpers (mirror compute_sediment_yield).
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: Any) -> tuple[float, float, float, float]:
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise UhiInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    try:
        west, south, east, north = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise UhiInputError(f"bbox contains non-numeric values: {bbox!r}") from exc
    if not all(math.isfinite(v) for v in (west, south, east, north)):
        raise UhiInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise UhiInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise UhiInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if west >= east or south >= north:
        raise UhiInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    if (east - west) > _MAX_AOI_DEG or (north - south) > _MAX_AOI_DEG:
        raise UhiAoiTooLargeError(
            f"AOI {bbox!r} exceeds the compute_urban_heat_island clamp of "
            f"{_MAX_AOI_DEG} degree per side "
            f"(got {east - west:.3f} x {north - south:.3f} deg). Pick a "
            "metro-scale AOI (the LST is resampled onto the 10 m class grid "
            "in-process)."
        )
    return (west, south, east, north)


def _stage_uri_local(uri: str, tmpdir: str, label: str) -> str:
    """Return a local file path for ``uri`` (s3:// download or local path)."""
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        name = uri.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
        local = os.path.join(tmpdir, f"{label}_{name}")
        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise UhiUpstreamError(
                f"S3 download failed for {label} uri {uri!r}: {exc}"
            ) from exc
        with open(local, "wb") as f:
            f.write(data)
        return local
    if uri.startswith(("gs://", "http://", "https://")):
        raise UhiInputError(
            f"{label} uri scheme not supported: {uri!r} (use s3:// or a local path)"
        )
    if not os.path.exists(uri):
        raise UhiInputError(f"{label} uri points at a missing local file: {uri!r}")
    return uri


def _open_band(path: str, label: str) -> tuple[np.ndarray, Any]:
    """Open band 1 as float64 with nodata -> NaN; return (array, open dataset)."""
    try:
        import rasterio
    except ImportError as exc:
        raise UhiUpstreamError(f"rasterio unavailable: {exc}") from exc
    try:
        src = rasterio.open(path)
    except Exception as exc:  # noqa: BLE001
        raise UhiInputError(f"could not open {label} raster {path!r}: {exc}") from exc
    band = src.read(1).astype(np.float64)
    nodata = src.nodata
    if nodata is not None and math.isfinite(float(nodata)):
        band[band == float(nodata)] = np.nan
    return band, src


def _resample_lst_to_grid(
    lst_path: str, ref_src: Any, notes: list[str]
) -> np.ndarray:
    """Reproject/resample the LST band onto the land-cover grid.

    COARSE -> FINE (1 km MODIS onto the 10 m class grid), BILINEAR: smooth
    interpolation adds no sub-kilometer thermal information (documented note)
    -- it aligns grids so per-class means read the local 1 km thermal
    environment. An LST already on the exact reference grid passes through
    value-identical (the compute_sediment_yield same-grid shortcut).
    """
    import rasterio  # noqa: F401
    from rasterio.warp import Resampling, reproject

    band, src = _open_band(lst_path, "lst")
    try:
        same_grid = (
            src.crs == ref_src.crs
            and src.transform == ref_src.transform
            and src.shape == ref_src.shape
        )
        if same_grid:
            notes.append(
                "LST already on the land-cover grid; used value-identical."
            )
            return band
        out = np.full(ref_src.shape, np.nan, dtype=np.float64)
        reproject(
            source=band,
            destination=out,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=ref_src.transform,
            dst_crs=ref_src.crs,
            src_nodata=np.nan,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
        notes.append(
            "LST resampled COARSE->FINE onto the land-cover grid (bilinear): "
            "interpolating ~1 km MODIS LST to the 10 m class grid adds NO "
            "thermal detail below the native 1 km scale; per-class means "
            "compare each class's kilometer-scale thermal environment (the "
            "standard surface-UHI formulation)."
        )
        return out
    except UhiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise UhiUpstreamError(
            f"resampling the LST onto the land-cover grid failed: {exc}"
        ) from exc
    finally:
        src.close()


# ---------------------------------------------------------------------------
# Output helpers.
# ---------------------------------------------------------------------------


def _write_cog_bytes(lst: np.ndarray, ref_src: Any, tmpdir: str) -> bytes:
    """Encode the aligned LST grid as COG bytes on the land-cover grid."""
    import rasterio

    data = np.where(np.isfinite(lst), lst, _NODATA).astype(np.float32)
    path = os.path.join(tmpdir, "uhi_lst.tif")
    profile = {
        "driver": "COG",
        "count": 1,
        "dtype": "float32",
        "crs": ref_src.crs,
        "transform": ref_src.transform,
        "width": ref_src.width,
        "height": ref_src.height,
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
    filename = f"urban_heat_island_{seed}.tif"
    if output_dir is not None:
        path = os.path.join(output_dir, filename)
        with open(path, "wb") as f:
            f.write(payload)
        return path
    try:
        from trid3nt_server.tools.simulation.solver import _get_runs_bucket, _get_s3_client

        bucket = _get_runs_bucket()
        key = f"urban-heat-island-{seed}/{filename}"
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="image/tiff",
        )
        return f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001
        raise UhiUpstreamError(
            f"failed to upload the UHI LST COG to the runs bucket: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Registered tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: fetches its own MODIS LST + Esri/IO land-cover inputs
    # (external PC STAC API) unless both override URIs are passed -- an
    # input-fetching composer like compute_sediment_yield, so
    # open_world_hint=True is honest (listed in
    # test_tool_annotations._OPEN_WORLD_COMPUTE_EXCEPTIONS).
    open_world_hint=True,
)
def compute_urban_heat_island(
    bbox: tuple[float, float, float, float],
    start_date: str | None = None,
    end_date: str | None = None,
    daynight: str = "day",
    lst_uri: str | None = None,
    landcover_uri: str | None = None,
    *,
    _output_dir: str | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> UrbanHeatIslandLayerURI:
    """Quantify the urban heat island: MODIS LST stratified by land-cover class.

    Use this (do not stop at ``geocode_location``) once you have the AOI --
    it is the tool that computes the UHI analysis: "how strong is the
    urban heat island here?", "how much hotter is built-up vs parks?"
    Fetches MODIS 8-day LST + Esri/IO land cover, resamples LST onto the
    land-cover grid, and reports ``uhi_delta_c`` = mean(Built) -
    mean(vegetation classes). Do NOT use for: sub-kilometer thermal
    contrast (MODIS is 1km; use ``fetch_landsat_imagery(band_combo=
    "thermal")`` for 30m); air-temperature (2m canopy) UHI -- this is
    SURFACE UHI; a plain LST map (``fetch_modis_lst``).

    Params:
        bbox: EPSG:4326, clamped to <= 1.0 deg per side.
        start_date/end_date: "YYYY-MM-DD" LST window; default trailing
            ~120 days.
        daynight: ``"day"`` (default, peak surface heat) or ``"night"``
            (overnight retention, public-health-relevant).
        lst_uri: optional precomputed deg-C LST raster override.
        landcover_uri: optional land-cover class raster override.

    Returns:
        ``UrbanHeatIslandLayerURI`` -- raster ``LayerURI`` (single-band
        float32 deg-C, ``style_preset="land_surface_temp_c"``) with
        ``uhi_delta_c``, ``built_mean_lst_c``, ``vegetation_mean_lst_c``,
        ``per_class_lst_c`` (per-class mean/min/max/count/share), honest
        ``notes``. ``uhi_delta_c`` is ``None`` when no built or vegetated
        pixels exist -- never fabricated.

    Raises:
        UhiAoiTooLargeError / UhiInputError: bad bbox, unreadable URIs,
            no overlapping valid cells.
        UhiUpstreamError: fetch/resample/write failure.
    """
    q_bbox = _validate_bbox(bbox)
    dn = str(daynight or "day").strip().lower()
    if dn not in ("day", "night"):
        raise UhiInputError(f"daynight must be 'day' or 'night'; got {daynight!r}")

    notes: list[str] = []

    with tempfile.TemporaryDirectory(prefix="trid3nt_uhi_") as tmpdir:
        # ---- 1. Land cover (the reference grid). --------------------------
        if landcover_uri is not None:
            lc_local = _stage_uri_local(landcover_uri, tmpdir, "landcover")
            notes.append(
                f"Land cover from caller-supplied landcover_uri ({landcover_uri})."
            )
        else:
            try:
                from trid3nt_server.tools.fetchers.terrain.fetch_esri_landcover_10m import fetch_esri_landcover_10m

                layer = fetch_esri_landcover_10m(bbox=q_bbox)
            except UhiError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise UhiUpstreamError(
                    f"fetch_esri_landcover_10m failed for bbox={q_bbox}: {exc}"
                ) from exc
            lc_local = _stage_uri_local(layer.uri, tmpdir, "landcover")
            notes.append(
                "Land cover: Esri/IO 10 m annual LULC (io-lulc-annual-v02) via "
                "fetch_esri_landcover_10m."
            )
        lc_band, lc_src = _open_band(lc_local, "landcover")
        try:
            if max(lc_src.width, lc_src.height) > _MAX_GRID_PX:
                raise UhiInputError(
                    f"land-cover grid {lc_src.width}x{lc_src.height} exceeds the "
                    f"{_MAX_GRID_PX}px/axis in-process ceiling; narrow the bbox."
                )

            # ---- 2. LST (override or fetch), aligned onto the grid. -------
            if lst_uri is not None:
                lst_local = _stage_uri_local(lst_uri, tmpdir, "lst")
                notes.append(
                    f"LST from caller-supplied lst_uri ({lst_uri}); values "
                    "assumed deg C."
                )
            else:
                try:
                    from trid3nt_server.tools.fetchers.climate.fetch_modis_lst import fetch_modis_lst

                    lst_layer = fetch_modis_lst(
                        bbox=q_bbox,
                        start_date=start_date,
                        end_date=end_date,
                        daynight=dn,
                    )
                except UhiError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise UhiUpstreamError(
                        f"fetch_modis_lst failed for bbox={q_bbox}: {exc}"
                    ) from exc
                lst_local = _stage_uri_local(lst_layer.uri, tmpdir, "lst")
                notes.append(
                    f"LST: MODIS 8-day 1 km composite ({dn}) in deg C via "
                    "fetch_modis_lst."
                )
            lst = _resample_lst_to_grid(lst_local, lc_src, notes)

            # ---- 3. Per-class stats over the joint-valid cells. -----------
            lc_valid = np.isfinite(lc_band)
            codes = np.zeros(lc_src.shape, dtype=np.int32)
            codes[lc_valid] = np.rint(lc_band[lc_valid]).astype(np.int32)
            joint = lc_valid & np.isfinite(lst)
            if not joint.any():
                raise UhiInputError(
                    "the LST and land-cover rasters share no valid overlapping "
                    f"cells over bbox={q_bbox} (all-cloud LST window or "
                    "non-overlapping grids)."
                )
            n_joint = int(joint.sum())

            per_class: list[dict[str, Any]] = []
            class_means: dict[int, float] = {}
            for code in sorted(IO_LULC_LABELS):
                if code in _NO_INFO_CLASSES:
                    continue
                sel = joint & (codes == code)
                count = int(sel.sum())
                if count == 0:
                    continue
                vals = lst[sel]
                mean_c = float(vals.mean())
                class_means[code] = mean_c
                per_class.append(
                    {
                        "class_code": code,
                        "label": IO_LULC_LABELS[code],
                        "mean_lst_c": round(mean_c, 2),
                        "min_lst_c": round(float(vals.min()), 2),
                        "max_lst_c": round(float(vals.max()), 2),
                        "pixel_count": count,
                        "share": round(count / n_joint, 4),
                    }
                )
            if not per_class:
                raise UhiInputError(
                    "no land-cover class with valid LST cells over the AOI "
                    "(only No-Data/Clouds classes present)."
                )

            # ---- 4. UHI delta: built minus the vegetation union. ----------
            built_mean = class_means.get(BUILT_CLASS)
            veg_sel = joint & np.isin(codes, list(VEGETATION_CLASSES))
            veg_count = int(veg_sel.sum())
            veg_mean = float(lst[veg_sel].mean()) if veg_count else None

            if built_mean is None:
                uhi_delta = None
                notes.append(
                    "UHI delta not computed: the AOI contains no Built-area "
                    "(class 7) pixels with valid LST -- an honest None, not 0."
                )
            elif veg_mean is None:
                uhi_delta = None
                notes.append(
                    "UHI delta not computed: the AOI contains no vegetated "
                    "pixels (Trees/Flooded veg/Crops/Rangeland) with valid LST "
                    "-- an honest None, not 0."
                )
            else:
                uhi_delta = built_mean - veg_mean
                notes.append(
                    f"uhi_delta_c = mean LST(Built area) {built_mean:.2f} - "
                    f"mean LST(vegetation union: Trees/Flooded veg/Crops/"
                    f"Rangeland) {veg_mean:.2f} = {uhi_delta:.2f} deg C "
                    "(surface-UHI intensity; positive = built-up hotter)."
                )

            # ---- 5. Write the aligned LST COG. -----------------------------
            payload = _write_cog_bytes(lst, lc_src, tmpdir)
        finally:
            lc_src.close()

    seed = uuid.uuid4().hex[:8]
    uri = _write_output(payload, seed, _output_dir)

    logger.info(
        "compute_urban_heat_island: bbox=%s daynight=%s -> %d classes, "
        "uhi_delta_c=%s",
        q_bbox,
        dn,
        len(per_class),
        f"{uhi_delta:.2f}" if uhi_delta is not None else "None",
    )
    return UrbanHeatIslandLayerURI(
        layer_id=f"urban-heat-island-{seed}",
        name=(
            f"Urban heat island LST ({dn}) -- bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="raster",
        uri=uri,
        style_preset=_STYLE_PRESET,
        role="primary",
        units="Land-surface temperature (deg C)",
        bbox=q_bbox,
        uhi_delta_c=round(uhi_delta, 3) if uhi_delta is not None else None,
        built_mean_lst_c=round(built_mean, 3) if built_mean is not None else None,
        vegetation_mean_lst_c=round(veg_mean, 3) if veg_mean is not None else None,
        per_class_lst_c=per_class,
        daynight=dn,
        notes=notes,
    )
