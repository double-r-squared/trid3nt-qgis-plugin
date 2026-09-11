"""``model_debris_flow`` - USGS post-fire debris-flow hazard over a burned AOI.

These are POST-FIRE models, so unburned terrain refuses. The MTBS fallback ships
PERIMETERS, so an interior is assumed moderate and every run notes it.
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

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

__all__ = [
    "model_debris_flow",
    "DebrisFlowLayerURI",
    "DebrisFlowError",
    "DebrisFlowInputError",
    "AoiTooLargeError",
    "NoBurnDataError",
    "DebrisFlowDependencyError",
    "DebrisFlowUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.derive.model_debris_flow.model_debris_flow")




class DebrisFlowError(RuntimeError):
    """Base class for model_debris_flow failures; ``error_code`` is the code the
    agent surface reports and ``retryable`` guides the retry decision.
    """

    error_code: str = "DEBRIS_FLOW_ERROR"
    retryable: bool = True


class DebrisFlowInputError(DebrisFlowError):
    """Bad inputs (malformed bbox, bad rainfall intensity, unreadable URI)."""

    error_code = "DEBRIS_FLOW_INPUT_INVALID"
    retryable = False


class AoiTooLargeError(DebrisFlowInputError):
    """The AOI exceeds the CPU-bound clamp (> 0.15 degrees per side)."""

    error_code = "DEBRIS_FLOW_AOI_TOO_LARGE"
    retryable = False


class NoBurnDataError(DebrisFlowError):
    """The AOI has no burn-severity data. Running a post-fire model on unburned
    terrain would be fabrication, so it refuses and asks for a burned area.
    """

    error_code = "DEBRIS_FLOW_NO_BURN_DATA"
    retryable = False


class DebrisFlowDependencyError(DebrisFlowError):
    """A required library (pfdf / rasterio / geopandas) is unavailable."""

    error_code = "DEBRIS_FLOW_DEPENDENCY_MISSING"
    retryable = False


class DebrisFlowUpstreamError(DebrisFlowError):
    """Input staging, upstream fetch, or artifact write failed."""

    error_code = "DEBRIS_FLOW_UPSTREAM_ERROR"
    retryable = True




class DebrisFlowLayerURI(LayerURI):
    """The segment-network ``LayerURI`` plus per-class counts, the maximum
    likelihood and volume, the design storm used and the burned fraction.
    """

    segment_count: int = 0
    high_hazard_count: int = 0
    moderate_hazard_count: int = 0
    low_hazard_count: int = 0
    likelihood_max: float | None = None
    volume_max_m3: float | None = None
    rainfall_intensity_mm_h: float = 24.0
    burned_fraction: float = 0.0
    notes: list[str] = []



#: CPU-bound AOI clamp (degrees per side). At 30 m cells, 0.15 deg is roughly
#: a 550 x 550 grid -- comfortably CPU-bounded for the pysheds-backed
#: watershed analysis on the agent box.
_MAX_AOI_DEG: float = 0.15

#: Minimum upslope CATCHMENT area (km^2) for a pixel to seed a stream segment.
#: 0.025 km^2 is the pfdf-tutorial / USGS-assessment convention.
_MIN_SEGMENT_AREA_KM2: float = 0.025

#: Maximum catchment area (km^2) retained in the assessment. The USGS
#: emergency assessments target upland catchments; larger drainages respond
#: as floods rather than debris flows (Gartner 2014 calibration domain).
_MAX_CATCHMENT_AREA_KM2: float = 8.0

#: Maximum stream-segment length (meters) -- pfdf/USGS convention.
_MAX_SEGMENT_LENGTH_M: float = 500.0

#: Documented constant KF-factor fallback when neither ``kf_uri`` nor
#: STATSGO KFFACT is available. 0.2 is a mid-range fine-soil erodibility.
_KF_CONSTANT_FALLBACK: float = 0.2

#: dNBR values assigned per BARC4 class when only a CLASS raster (or the MTBS
#: perimeter fallback) is available. Midpoints of pfdf's default
#: ``severity.estimate`` thresholds [125, 250, 500].
_DNBR_BY_BARC4: dict[int, float] = {1: 60.0, 2: 187.0, 3: 375.0, 4: 600.0}

#: Combined-hazard class labels (cannon2010.hazard with default thresholds
#: returns classes 1..3).
_HAZARD_LABELS: dict[int, str] = {1: "Low", 2: "Moderate", 3: "High"}

#: Rainfall-intensity sanity range (mm/h). 24 mm/h is a common design storm;
#: >250 mm/h exceeds any recorded 15-minute intensity.
_MIN_INTENSITY_MM_H = 1.0
_MAX_INTENSITY_MM_H = 250.0


_METADATA = AtomicToolMetadata(
    name="model_debris_flow",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)




def _validate_bbox(bbox: Any) -> tuple[float, float, float, float]:
    """Validate + normalize the bbox; enforce the CPU-bound AOI clamp."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise DebrisFlowInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    try:
        west, south, east, north = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise DebrisFlowInputError(f"bbox contains non-numeric values: {bbox!r}") from exc
    if not all(math.isfinite(v) for v in (west, south, east, north)):
        raise DebrisFlowInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise DebrisFlowInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise DebrisFlowInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if west >= east or south >= north:
        raise DebrisFlowInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    if (east - west) > _MAX_AOI_DEG or (north - south) > _MAX_AOI_DEG:
        raise AoiTooLargeError(
            f"AOI {bbox!r} exceeds the model_debris_flow clamp of "
            f"{_MAX_AOI_DEG} degrees per side "
            f"(got {east - west:.3f} x {north - south:.3f} deg). The 30 m "
            "watershed analysis is CPU-bounded; pick a single fire / small "
            "watershed AOI."
        )
    return (west, south, east, north)


def _validate_intensity(value: Any) -> float:
    try:
        intensity = float(value)
    except (TypeError, ValueError) as exc:
        raise DebrisFlowInputError(
            f"rainfall_intensity_mm_h must be a number; got {value!r}"
        ) from exc
    if not math.isfinite(intensity) or not (
        _MIN_INTENSITY_MM_H <= intensity <= _MAX_INTENSITY_MM_H
    ):
        raise DebrisFlowInputError(
            f"rainfall_intensity_mm_h must be in "
            f"[{_MIN_INTENSITY_MM_H}, {_MAX_INTENSITY_MM_H}] mm/h; got {value!r}"
        )
    return intensity




def _stage_uri_local(uri: str, tmpdir: str, label: str) -> str:
    """Return a local file path for ``uri`` (s3:// download or local path)."""
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        name = uri.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
        local = os.path.join(tmpdir, f"{label}_{name}")
        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise DebrisFlowUpstreamError(
                f"S3 download failed for {label} uri {uri!r}: {exc}"
            ) from exc
        with open(local, "wb") as f:
            f.write(data)
        return local
    if uri.startswith("gs://") or uri.startswith("http://") or uri.startswith("https://"):
        raise DebrisFlowInputError(
            f"{label} uri scheme not supported: {uri!r} (use s3:// or a local path)"
        )
    if not os.path.exists(uri):
        raise DebrisFlowInputError(f"{label} uri points at a missing local file: {uri!r}")
    return uri


def _load_dem(
    bbox: tuple[float, float, float, float],
    dem_uri: str | None,
    tmpdir: str,
    notes: list[str],
) -> Any:
    """Load the DEM (override URI or fetch_copernicus_dem), projected to UTM."""
    from pfdf.raster import Raster

    if dem_uri is not None:
        local = _stage_uri_local(dem_uri, tmpdir, "dem")
        source_note = f"DEM from caller-supplied dem_uri ({dem_uri})."
    else:
        try:
            from trid3nt_server.tools import TOOL_REGISTRY

            layer = TOOL_REGISTRY["fetch_copernicus_dem"].fn(bbox=bbox)
        except DebrisFlowError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DebrisFlowUpstreamError(
                f"fetch_copernicus_dem failed for bbox={bbox}: {exc}"
            ) from exc
        local = _stage_uri_local(layer.uri, tmpdir, "dem")
        source_note = "DEM: Copernicus GLO-30 (30 m) via fetch_copernicus_dem."
    try:
        dem = Raster.from_file(local)
    except Exception as exc:  # noqa: BLE001
        raise DebrisFlowInputError(f"could not open DEM raster {local!r}: {exc}") from exc

    # Watershed slope and relief math needs a projected (metre) grid, so a
    # geographic DEM goes to its UTM zone and a projected one stays as it is.
    try:
        crs_is_geographic = bool(getattr(dem.crs, "is_geographic", False))
    except Exception:  # noqa: BLE001
        crs_is_geographic = False
    if crs_is_geographic:
        utm = dem.utm_zone
        if utm is None:
            raise DebrisFlowInputError(
                "DEM has a geographic CRS and no resolvable UTM zone; supply a "
                "projected dem_uri."
            )
        dem.reproject(crs=utm)
    notes.append(source_note)
    return dem


def _align_to_dem(raster: Any, dem: Any) -> Any:
    """Reproject + clip ``raster`` onto the DEM grid (in place; returns it)."""
    raster.reproject(template=dem)
    raster.clip(dem.bounds)
    return raster


def _severity_from_uri(
    severity_uri: str, dem: Any, tmpdir: str, notes: list[str]
) -> tuple[Any, Any]:
    """``(BARC4 severity, dNBR)`` from an override URI; values entirely within
    [0, 4] are read as BARC4 classes, anything else as continuous dNBR.
    """
    from pfdf import severity as pfdf_severity
    from pfdf.raster import Raster

    local = _stage_uri_local(severity_uri, tmpdir, "severity")
    try:
        sev_in = Raster.from_file(local)
    except Exception as exc:  # noqa: BLE001
        raise DebrisFlowInputError(
            f"could not open severity raster {local!r}: {exc}"
        ) from exc
    _align_to_dem(sev_in, dem)

    values = np.asarray(sev_in.values, dtype=np.float64)
    nodata = sev_in.nodata
    valid = np.isfinite(values)
    if nodata is not None and math.isfinite(float(nodata)):
        valid &= values != float(nodata)
    finite = values[valid]
    if finite.size == 0:
        raise NoBurnDataError(
            f"severity raster {severity_uri!r} has no valid pixels over the AOI."
        )

    if float(finite.max()) <= 4.0 and float(finite.min()) >= 0.0:
        classes = np.zeros(values.shape, dtype=np.int16)
        classes[valid] = np.clip(np.rint(finite), 0, 4).astype(np.int16)
        barc4 = Raster.from_array(classes, spatial=dem, nodata=0)
        dnbr_arr = np.zeros(values.shape, dtype=np.float32)
        for cls, dnbr_val in _DNBR_BY_BARC4.items():
            dnbr_arr[classes == cls] = dnbr_val
        dnbr = Raster.from_array(dnbr_arr, spatial=dem, nodata=-32768.0)
        notes.append(
            "Burn severity from caller-supplied BARC4 class raster; dNBR "
            "approximated from class midpoints "
            f"{_DNBR_BY_BARC4} (no continuous dNBR supplied)."
        )
    else:
        # Continuous dNBR raster -> classify with pfdf's default thresholds.
        dnbr_arr = np.where(valid, values, np.nan).astype(np.float32)
        # casting="unsafe": a float64 NaN NoData into a float32 raster is
        # value-preserving but fails pfdf's default "safe" cast check.
        dnbr = Raster.from_array(
            dnbr_arr, spatial=dem, nodata=np.nan, casting="unsafe"
        )
        try:
            barc4 = pfdf_severity.estimate(dnbr)
        except Exception as exc:  # noqa: BLE001
            raise DebrisFlowInputError(
                f"pfdf.severity.estimate failed on dNBR raster {severity_uri!r}: {exc}"
            ) from exc
        notes.append(
            "Burn severity classified from caller-supplied continuous dNBR "
            "raster via pfdf.severity.estimate (thresholds [125, 250, 500])."
        )
    return barc4, dnbr


def _severity_from_mtbs(
    bbox: tuple[float, float, float, float], dem: Any, tmpdir: str, notes: list[str]
) -> tuple[Any, Any]:
    """Rasterize MTBS fire perimeters as a severity substrate; they are boundary
    POLYGONS, so the interior is assumed uniformly MODERATE severity.
    """
    from pfdf.raster import Raster

    try:
        from trid3nt_server.tools import TOOL_REGISTRY
        from trid3nt_server.tools.fetchers._fetch_common import FetchError
        _fetch_mtbs = TOOL_REGISTRY["fetch_mtbs_burn_severity"].fn
    except Exception as exc:  # noqa: BLE001
        raise DebrisFlowDependencyError(
            f"fetch_mtbs_burn_severity unavailable: {exc}"
        ) from exc

    try:
        layer = _fetch_mtbs(bbox=bbox)
    except FetchError as exc:
        raise DebrisFlowUpstreamError(
            f"MTBS burned-area query failed for bbox={bbox}: {exc}"
        ) from exc
    local = _stage_uri_local(layer.uri, tmpdir, "mtbs")

    try:
        import geopandas as gpd
    except ImportError as exc:
        raise DebrisFlowDependencyError(f"geopandas unavailable: {exc}") from exc
    try:
        gdf = gpd.read_file(local)
    except Exception as exc:  # noqa: BLE001
        raise DebrisFlowUpstreamError(
            f"could not read MTBS FlatGeobuf {local!r}: {exc}"
        ) from exc
    if len(gdf) == 0:
        raise NoBurnDataError(
            f"No MTBS-mapped fire intersects the AOI {bbox!r}. The post-fire "
            "debris-flow models (Staley 2017 M1 + Gartner 2014) only apply to "
            "BURNED terrain -- pick an AOI over a mapped fire, or pass "
            "severity_uri with a BARC4/dNBR burn-severity raster."
        )

    try:
        from rasterio.features import rasterize
    except ImportError as exc:
        raise DebrisFlowDependencyError(f"rasterio unavailable: {exc}") from exc

    try:
        gdf = gdf.set_crs(4326, allow_override=False) if gdf.crs is None else gdf
        gdf = gdf.to_crs(dem.crs)
        shapes = [(geom, 3) for geom in gdf.geometry if geom is not None and not geom.is_empty]
        burned = rasterize(
            shapes,
            out_shape=dem.shape,
            transform=dem.affine,
            fill=1,  # unburned
            dtype=np.int16,
        )
    except Exception as exc:  # noqa: BLE001
        raise DebrisFlowUpstreamError(
            f"rasterizing MTBS perimeters onto the DEM grid failed: {exc}"
        ) from exc

    barc4 = Raster.from_array(burned.astype(np.int16), spatial=dem, nodata=0)
    dnbr_arr = np.where(burned == 3, _DNBR_BY_BARC4[3], 0.0).astype(np.float32)
    dnbr = Raster.from_array(dnbr_arr, spatial=dem, nodata=-32768.0)
    fire_names = [
        str(n) for n in gdf.get("FIRE_NAME", []) if isinstance(n, str) and n.strip()
    ][:5]
    notes.append(
        "Burn severity FALLBACK: MTBS supplies fire-perimeter polygons, not the "
        "per-pixel BARC4 raster -- assumed uniform MODERATE severity (BARC4 "
        f"class 3, dNBR {_DNBR_BY_BARC4[3]:.0f}) inside the mapped perimeter(s)"
        + (f" ({', '.join(fire_names)})" if fire_names else "")
        + ". Pass severity_uri with a real BARC4/dNBR raster for a calibrated run."
    )
    return barc4, dnbr


def _load_kf(
    bbox: tuple[float, float, float, float],
    kf_uri: str | None,
    dem: Any,
    tmpdir: str,
    notes: list[str],
) -> Any:
    """Load the KF-factor raster (override / STATSGO KFFACT / constant fallback)."""
    from pfdf.raster import Raster

    if kf_uri is not None:
        local = _stage_uri_local(kf_uri, tmpdir, "kf")
        try:
            kf = Raster.from_file(local)
        except Exception as exc:  # noqa: BLE001
            raise DebrisFlowInputError(
                f"could not open KF-factor raster {local!r}: {exc}"
            ) from exc
        _align_to_dem(kf, dem)
        notes.append(f"KF-factor from caller-supplied kf_uri ({kf_uri}).")
        return kf

    try:

        from trid3nt_server.tools import TOOL_REGISTRY
        fetch_statsgo_soils = TOOL_REGISTRY["fetch_statsgo_soils"].fn

        layer = fetch_statsgo_soils(bbox=bbox, field="KFFACT")
        local = _stage_uri_local(layer.uri, tmpdir, "kf")
        kf = Raster.from_file(local)
        _align_to_dem(kf, dem)
        notes.append("KF-factor: USGS STATSGO KFFACT (30 m) via fetch_statsgo_soils.")
        return kf
    except Exception as exc:  # noqa: BLE001 -- documented constant fallback
        kf = Raster.from_array(
            np.full(dem.shape, _KF_CONSTANT_FALLBACK, dtype=np.float32),
            spatial=dem,
            nodata=-1.0,
        )
        notes.append(
            f"KF-factor FALLBACK: STATSGO KFFACT unavailable ({exc}); using a "
            f"constant KF-factor of {_KF_CONSTANT_FALLBACK} across the AOI. "
            "Pass kf_uri for soil-resolved results."
        )
        return kf




def _write_segments_geojson(
    fc: dict[str, Any], seed: str, output_dir: str | None
) -> str:
    """Persist the segment FeatureCollection and return its URI: a local path when
    ``output_dir`` is given, else an ``s3://`` key in the runs bucket.
    """
    payload = json.dumps(fc).encode("utf-8")
    filename = f"debris_flow_segments_{seed}.geojson"
    if output_dir is not None:
        path = os.path.join(output_dir, filename)
        with open(path, "wb") as f:
            f.write(payload)
        return path
    try:
        from trid3nt_server.workflows.solver.solver import _get_runs_bucket, _get_s3_client

        bucket = _get_runs_bucket()
        key = f"debris-flow-{seed}/{filename}"
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="application/geo+json",
        )
        return f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001
        raise DebrisFlowUpstreamError(
            f"failed to upload debris-flow segments GeoJSON to the runs bucket: {exc}"
        ) from exc




@register_tool(
    _METADATA,
    # Writes only its own run artifact; open-world when fetching its inputs.
    open_world_hint=True,
)
def model_debris_flow(
    bbox: tuple[float, float, float, float],
    rainfall_intensity_mm_h: float = 24.0,
    dem_uri: str | None = None,
    severity_uri: str | None = None,
    kf_uri: str | None = None,
    min_burned_fraction: float = 0.01,
    *,
    _output_dir: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> DebrisFlowLayerURI:
    """USGS post-fire debris-flow hazard assessment over a burned AOI (pfdf: Staley/Gartner/Cannon models).

    Use for debris-flow risk below a fire, or what happens when it rains on a
    burn scar. The chain is DEM -> watershed -> stream segments -> Staley 2017
    LIKELIHOOD -> Gartner 2014 VOLUME -> Cannon 2010 HAZARD class. Not for
    unburned terrain, nor for rainfall flooding (``telemac_rain_on_grid``).

    Params:
        bbox: EPSG:4326, clamped to 0.15 deg per side.
        rainfall_intensity_mm_h: peak 15-min design storm, default 24, driving
            both models.
        dem_uri: override DEM; default Copernicus GLO-30.
        severity_uri: override BARC4 or dNBR raster; default MTBS perimeters
            at uniform moderate severity.
        kf_uri: override soil KF raster; default STATSGO, then a noted 0.2.
        min_burned_fraction: burned fraction below which it refuses.

    Returns GeoJSON LineStrings carrying ``likelihood``, ``volume_m3`` and
    ``hazard_class`` per segment, with per-class counts and honest notes.
    """
    q_bbox = _validate_bbox(bbox)
    intensity = _validate_intensity(rainfall_intensity_mm_h)
    try:
        min_burned = float(min_burned_fraction)
    except (TypeError, ValueError) as exc:
        raise DebrisFlowInputError(
            f"min_burned_fraction must be a number; got {min_burned_fraction!r}"
        ) from exc
    if not (0.0 <= min_burned <= 1.0):
        raise DebrisFlowInputError(
            f"min_burned_fraction must be in [0, 1]; got {min_burned_fraction!r}"
        )

    try:
        from pfdf import severity as pfdf_severity
        from pfdf import watershed
        from pfdf.models import cannon2010 as c10
        from pfdf.models import gartner2014 as g14
        from pfdf.models import staley2017 as s17
        from pfdf.segments import Segments
    except ImportError as exc:
        raise DebrisFlowDependencyError(f"pfdf not importable: {exc}") from exc

    notes: list[str] = []

    with tempfile.TemporaryDirectory(prefix="trid3nt_debris_flow_") as tmpdir:
        dem = _load_dem(q_bbox, dem_uri, tmpdir, notes)
        if severity_uri is not None:
            barc4, dnbr = _severity_from_uri(severity_uri, dem, tmpdir, notes)
        else:
            barc4, dnbr = _severity_from_mtbs(q_bbox, dem, tmpdir, notes)
        kf = _load_kf(q_bbox, kf_uri, dem, tmpdir, notes)

        try:
            isburned = pfdf_severity.mask(barc4, ["low", "moderate", "high"])
            moderate_high = pfdf_severity.mask(barc4, ["moderate", "high"])
        except Exception as exc:  # noqa: BLE001
            raise DebrisFlowInputError(
                f"pfdf.severity.mask failed on the severity raster: {exc}"
            ) from exc
        burned_fraction = float(np.asarray(isburned.values, dtype=bool).mean())
        if burned_fraction < min_burned:
            raise NoBurnDataError(
                f"Burned fraction of the AOI is {burned_fraction:.4f} "
                f"(< min_burned_fraction={min_burned}). The post-fire "
                "debris-flow models only apply to BURNED terrain -- pick an "
                "AOI over a mapped fire, or pass severity_uri with a "
                "BARC4/dNBR burn-severity raster."
            )

        try:
            conditioned = watershed.condition(dem)
            flow = watershed.flow(conditioned)
            slopes = watershed.slopes(conditioned, flow)
            relief = watershed.relief(conditioned, flow)
            accumulation = watershed.accumulation(flow)
        except Exception as exc:  # noqa: BLE001
            raise DebrisFlowUpstreamError(
                f"pfdf watershed analysis failed: {exc}"
            ) from exc

        pixel_km2 = float(dem.pixel_area(units="kilometers"))
        area_km2 = np.asarray(accumulation.values, dtype=np.float64) * pixel_km2
        network_mask = area_km2 >= _MIN_SEGMENT_AREA_KM2
        if not bool(network_mask.any()):
            raise DebrisFlowUpstreamError(
                "no stream network could be delineated: no pixel reaches the "
                f"{_MIN_SEGMENT_AREA_KM2} km^2 upslope-area threshold. The AOI "
                "may be too small or too flat for a debris-flow assessment."
            )

        from pfdf.raster import Raster

        try:
            mask_raster = Raster.from_array(network_mask, spatial=flow, isbool=True)
            segments = Segments(flow, mask_raster, max_length=_MAX_SEGMENT_LENGTH_M)
        except Exception as exc:  # noqa: BLE001
            raise DebrisFlowUpstreamError(
                f"pfdf stream-segment delineation failed: {exc}"
            ) from exc
        if segments.size == 0:
            raise DebrisFlowUpstreamError(
                "stream-segment delineation produced zero segments."
            )

        # The USGS assessment domain is upland catchments that intersect the
        # burn; a larger drainage responds as a flood, not a debris flow.
        catch_km2 = np.asarray(segments.area(units="kilometers"), dtype=np.float64)
        burn_ratio = np.asarray(segments.burn_ratio(isburned), dtype=np.float64)
        keep = (catch_km2 <= _MAX_CATCHMENT_AREA_KM2) & (burn_ratio > 0.0)
        if not bool(keep.any()):
            raise NoBurnDataError(
                "the delineated stream network does not intersect the burned "
                "area (no segment has a burned catchment <= "
                f"{_MAX_CATCHMENT_AREA_KM2} km^2). Pick an AOI centered on the "
                "burn scar."
            )
        n_removed = int((~keep).sum())
        segments.keep(keep)
        if n_removed:
            notes.append(
                f"Filtered {n_removed} segment(s) outside the assessment domain "
                f"(catchment > {_MAX_CATCHMENT_AREA_KM2} km^2 or unburned "
                "catchment); "
                f"{segments.size} segment(s) retained."
            )

        try:
            B, Ct, Cf, Cs = s17.M1.parameters(durations=[15])
            T, F, S = s17.M1.variables(
                segments, moderate_high, slopes, dnbr, kf, omitnan=True
            )
            # R is the 15-minute rainfall ACCUMULATION (mm) equivalent to the
            # requested mm/h intensity.
            accumulation_mm = np.array([intensity / 4.0])
            likelihoods = np.asarray(
                s17.likelihood(accumulation_mm, B, Ct, T, Cf, F, Cs, S),
                dtype=np.float64,
            ).reshape(-1)
        except Exception as exc:  # noqa: BLE001
            raise DebrisFlowUpstreamError(
                f"Staley 2017 M1 likelihood model failed: {exc}"
            ) from exc

        try:
            bmh_km2 = np.asarray(
                segments.burned_area(moderate_high, units="kilometers"),
                dtype=np.float64,
            )
            relief_m = np.asarray(segments.relief(relief), dtype=np.float64)
            with np.errstate(divide="ignore", invalid="ignore"):
                volumes, _v_min, _v_max = g14.emergency(
                    np.array([intensity]), bmh_km2, relief_m
                )
            volumes = np.asarray(volumes, dtype=np.float64).reshape(-1)
        except Exception as exc:  # noqa: BLE001
            raise DebrisFlowUpstreamError(
                f"Gartner 2014 emergency volume model failed: {exc}"
            ) from exc

        try:
            hazard = np.asarray(
                c10.hazard(likelihoods, volumes), dtype=np.float64
            ).reshape(-1)
        except Exception as exc:  # noqa: BLE001
            raise DebrisFlowUpstreamError(
                f"Cannon 2010 combined hazard classification failed: {exc}"
            ) from exc
        labels = np.array(
            [
                _HAZARD_LABELS.get(int(h), "Unknown") if math.isfinite(h) else "Unknown"
                for h in hazard
            ],
            dtype="U8",
        )
        n_unknown = int((labels == "Unknown").sum())
        if n_unknown:
            notes.append(
                f"{n_unknown} segment(s) classified 'Unknown' (insufficient "
                "severity/soil data in the catchment)."
            )

        try:
            fc = segments.geojson(
                properties={
                    "likelihood": np.round(likelihoods, 4),
                    "volume_m3": np.round(volumes, 1),
                    "hazard_class": labels,
                },
                crs=4326,
            )
        except Exception as exc:  # noqa: BLE001
            raise DebrisFlowUpstreamError(
                f"segment GeoJSON export failed: {exc}"
            ) from exc

    seed = uuid.uuid4().hex[:8]
    uri = _write_segments_geojson(dict(fc), seed, _output_dir)

    segment_count = int(segments.size)
    high_hazard_count = int((labels == "High").sum())
    finite_lik = likelihoods[np.isfinite(likelihoods)]
    finite_vol = volumes[np.isfinite(volumes)]
    logger.info(
        "model_debris_flow: bbox=%s intensity=%.1f mm/h -> %d segment(s), "
        "%d High-hazard, burned_fraction=%.3f",
        q_bbox,
        intensity,
        segment_count,
        high_hazard_count,
        burned_fraction,
    )
    # Only an isinstance(result, LayerURI) return persists to the case record,
    # so a LayerURI-SHAPED dict here renders live and vanishes from export.
    return DebrisFlowLayerURI(
        layer_id=f"debris-flow-{seed}",
        name=(
            f"Debris-flow hazard segments ({intensity:g} mm/h design storm) -- "
            f"bbox ({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="vector",
        uri=uri,
        style={"kind": "reference", "geometry": "line"},
        role="primary",
        units="likelihood (0-1) / m^3",
        bbox=q_bbox,
        segment_count=segment_count,
        high_hazard_count=high_hazard_count,
        moderate_hazard_count=int((labels == "Moderate").sum()),
        low_hazard_count=int((labels == "Low").sum()),
        likelihood_max=float(finite_lik.max()) if finite_lik.size else None,
        volume_max_m3=float(finite_vol.max()) if finite_vol.size else None,
        rainfall_intensity_mm_h=intensity,
        burned_fraction=round(burned_fraction, 4),
        notes=notes,
    )
