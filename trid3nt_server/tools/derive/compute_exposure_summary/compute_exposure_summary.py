"""``compute_exposure_summary`` - who and what is inside a hazard footprint.

Exposure degrades PER COMPONENT - a failed one is None with its reason, never a
number - and an empty footprint is a typed refusal, not a row of zeros.
"""
from __future__ import annotations

import logging
import math
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

import numpy as np

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

__all__ = [
    "compute_exposure_summary",
    "get_session_exposure",
    "ExposureSummaryError",
    "ExposureInputError",
    "ExposureEmptyFootprintError",
    "ExposureUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.derive.compute_exposure_summary.compute_exposure_summary")




class ExposureSummaryError(RuntimeError):
    """Base class for compute_exposure_summary failures."""

    error_code: str = "EXPOSURE_SUMMARY_ERROR"
    retryable: bool = True


class ExposureInputError(ExposureSummaryError):
    """Bad inputs (missing/unreadable hazard uri, non-finite threshold)."""

    error_code = "EXPOSURE_INPUT_INVALID"
    retryable = False


class ExposureEmptyFootprintError(ExposureSummaryError):
    """No hazard cell crosses the threshold: an honest empty footprint, narrated
    as nothing exposed rather than returned as a row of zeros.
    """

    error_code = "EXPOSURE_EMPTY_FOOTPRINT"
    retryable = False


class ExposureUpstreamError(ExposureSummaryError):
    """Hazard-raster staging or read failed."""

    error_code = "EXPOSURE_UPSTREAM_ERROR"
    retryable = True



_GLOBAL_KEY = "__global__"
_MAX_SESSION_ENTRIES = 32

#: Last exposure result per Case (insertion-order bounded). In-memory only --
#: honest session scope; a fresh process has no exposure history.
_SESSION_EXPOSURE: dict[str, dict[str, Any]] = {}


def _record_session_exposure(result: dict[str, Any]) -> None:
    try:
        from trid3nt_server.emission.pipeline_emitter import current_turn_case

        key = current_turn_case() or _GLOBAL_KEY
    except Exception:  # noqa: BLE001 -- store is best-effort, never a gate
        key = _GLOBAL_KEY
    _SESSION_EXPOSURE.pop(key, None)
    _SESSION_EXPOSURE[key] = result
    while len(_SESSION_EXPOSURE) > _MAX_SESSION_ENTRIES:
        _SESSION_EXPOSURE.pop(next(iter(_SESSION_EXPOSURE)))


def get_session_exposure(case_id: str | None) -> dict[str, Any] | None:
    """The exposure summary computed this session for ``case_id``, falling back to
    the unbound slot; None when nothing has been computed this session.
    """
    if case_id and case_id in _SESSION_EXPOSURE:
        return _SESSION_EXPOSURE[case_id]
    return _SESSION_EXPOSURE.get(_GLOBAL_KEY)



_METADATA = AtomicToolMetadata(
    name="compute_exposure_summary",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)




def _stage_uri_local(uri: str, tmpdir: str, label: str) -> str:
    """Return a local file path for ``uri`` (s3:// download or local path)."""
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        name = uri.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
        local = os.path.join(tmpdir, f"{label}_{name}")
        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise ExposureUpstreamError(
                f"S3 download failed for {label} uri {uri!r}: {exc}"
            ) from exc
        with open(local, "wb") as f:
            f.write(data)
        return local
    if uri.startswith(("gs://", "http://", "https://")):
        raise ExposureInputError(
            f"{label} uri scheme not supported: {uri!r} (use s3:// or a local path)"
        )
    local_probe = uri.split("?", 1)[0]
    if not os.path.exists(local_probe):
        raise ExposureInputError(
            f"{label} uri points at a missing local file: {uri!r}"
        )
    return local_probe


def _footprint_area_km2(
    wet: np.ndarray, transform: Any, crs: Any
) -> float:
    """Footprint area in km^2 off the hazard grid: the affine determinant on a
    projected CRS, per-row geodesic cell area on a geographic one.
    """
    is_geographic = bool(getattr(crs, "is_geographic", False)) if crs else True
    if not is_geographic:
        cell_m2 = abs(transform.a * transform.e - transform.b * transform.d)
        return float(wet.sum()) * cell_m2 / 1e6

    from pyproj import Geod

    dx_deg = abs(transform.a)
    dy_deg = abs(transform.e)
    rows = np.arange(wet.shape[0], dtype=np.float64) + 0.5
    row_lats = transform.f + rows * transform.e
    geod = Geod(ellps="WGS84")
    lon0 = np.full(row_lats.shape, transform.c)
    row_cell_w_m = geod.inv(lon0, row_lats, lon0 + dx_deg, row_lats)[2]
    row_cell_h_m = geod.inv(
        lon0, row_lats - 0.5 * dy_deg, lon0, row_lats + 0.5 * dy_deg
    )[2]
    row_cell_m2 = row_cell_w_m * row_cell_h_m
    wet_per_row = wet.sum(axis=1).astype(np.float64)
    return float((wet_per_row * row_cell_m2).sum() / 1e6)


def _population_in_footprint(
    population_layer_uri: str,
    wet: np.ndarray,
    hazard_transform: Any,
    hazard_crs: Any,
    tmpdir: str,
    notes: list[str],
) -> int:
    """Sum the people of a population raster over footprint cells, the mask
    transferred nearest onto the population grid; any failure raises for the
    caller to record per-component.
    """
    import rasterio
    from rasterio.warp import Resampling, reproject

    pop_local = _stage_uri_local(population_layer_uri, tmpdir, "population")
    with rasterio.open(pop_local) as pop_src:
        pop = pop_src.read(1).astype(np.float64)
        pop_nodata = pop_src.nodata
        mask_on_pop = np.zeros(pop_src.shape, dtype=np.uint8)
        reproject(
            source=wet.astype(np.uint8),
            destination=mask_on_pop,
            src_transform=hazard_transform,
            src_crs=hazard_crs or "EPSG:4326",
            dst_transform=pop_src.transform,
            dst_crs=pop_src.crs or "EPSG:4326",
            resampling=Resampling.nearest,
        )
    valid = np.isfinite(pop)
    if pop_nodata is not None and math.isfinite(float(pop_nodata)):
        valid &= pop != float(pop_nodata)
    selected = (mask_on_pop == 1) & valid
    population = int(round(float(pop[selected].sum())))
    notes.append(
        f"Population: cells of {population_layer_uri} whose center falls on the "
        "hazard footprint (nearest-neighbor mask transfer). Population cells "
        "are coarser than most hazard grids, so edge cells are counted "
        "whole-cell -- a screening estimate, not a parcel census."
    )
    return population


def _buildings_in_footprint(
    buildings_layer_uri: str,
    hazard_local: str,
    wet_test: Any,
    tmpdir: str,
    notes: list[str],
) -> int:
    """Count building footprints whose representative point is on a wet cell."""
    import geopandas as gpd
    import rasterio

    bld_local = _stage_uri_local(buildings_layer_uri, tmpdir, "buildings")
    gdf = gpd.read_file(bld_local)
    gdf = gdf[gdf.geometry.notna()]
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    total = int(len(gdf))
    if total == 0:
        notes.append(
            "Buildings: the footprint layer holds zero buildings; the "
            "exposed-building count is an honest 0."
        )
        return 0

    with rasterio.open(hazard_local) as src:
        pts = gdf.to_crs(src.crs) if src.crs is not None else gdf
        coords = [
            (geom.representative_point().x, geom.representative_point().y)
            for geom in pts.geometry
        ]
        sampled = np.array(
            [float(v[0]) for v in src.sample(coords)], dtype=np.float64
        )
        nodata = src.nodata
    if nodata is not None and math.isfinite(float(nodata)):
        sampled[sampled == float(nodata)] = np.nan
    exposed = int(np.count_nonzero(wet_test(sampled)))
    notes.append(
        f"Buildings: {exposed} of {total} fetched footprints (OSM/MS via "
        "fetch_buildings) have a representative point on a footprint cell."
    )
    return exposed




@register_tool(
    _METADATA,
    # An analysis over three layers the caller names; nothing external.
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def compute_exposure_summary(
    hazard_layer_uri: str,
    population_layer_uri: str | None = None,
    buildings_layer_uri: str | None = None,
    threshold: float | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Summarize exposure (population, buildings, area) inside a hazard footprint.

    Use for "how many people or buildings are in the flood zone?" right after
    a flood, surge or plume solve produces a depth or intensity raster, and for
    situation-report headline numbers. Not for dollar losses
    (``compute_flood_depth_damage``) or generic raster-in-zone stats. Fetch the
    population raster (``fetch_population``) and the footprints
    (``fetch_buildings``) over the hazard's bbox first and pass their uris.

    Params:
        hazard_layer_uri: hazard raster (band 1 defines the footprint).
        population_layer_uri: a population-count raster; absent, the
            population component is None with its reason.
        buildings_layer_uri: a building-footprint vector layer; absent, the
            buildings component is None with its reason.
        threshold: footprint cutoff in raster units (e.g. 0.5 for >=0.5m
            depth). ``None`` (default) = any positive/wet cell.

    Returns population, buildings, area_km2, the threshold, bbox, per-component
    errors and notes. A failed component is None with its reason, never
    fabricated; no cell over the threshold is a typed "nothing exposed".
    """
    if not isinstance(hazard_layer_uri, str) or not hazard_layer_uri.strip():
        raise ExposureInputError(
            f"hazard_layer_uri must be a non-empty URI string; got "
            f"{hazard_layer_uri!r}"
        )
    thr: float | None
    if threshold is None:
        thr = None
    else:
        try:
            thr = float(threshold)
        except (TypeError, ValueError) as exc:
            raise ExposureInputError(
                f"threshold must be numeric or None; got {threshold!r}"
            ) from exc
        if not math.isfinite(thr):
            raise ExposureInputError(
                f"threshold must be finite; got {threshold!r}"
            )

    try:
        import rasterio
        from rasterio.warp import transform_bounds
    except ImportError as exc:  # pragma: no cover -- rasterio is a base dep
        raise ExposureUpstreamError(f"rasterio unavailable: {exc}") from exc

    notes: list[str] = []
    errors: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="trid3nt_exposure_") as tmpdir:
        hazard_local = _stage_uri_local(hazard_layer_uri, tmpdir, "hazard")
        try:
            with rasterio.open(hazard_local) as src:
                data = src.read(1).astype(np.float64)
                nodata = src.nodata
                transform = src.transform
                crs = src.crs
                bounds = src.bounds
        except ExposureSummaryError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ExposureInputError(
                f"could not open hazard raster {hazard_layer_uri!r}: {exc}"
            ) from exc

        valid = np.isfinite(data)
        if nodata is not None and math.isfinite(float(nodata)):
            valid &= data != float(nodata)

        if thr is None:
            wet = valid & (data > 0.0)
            notes.append(
                "Footprint = any positive (wet) cell (threshold not supplied)."
            )

            def _wet_test(v: np.ndarray) -> np.ndarray:
                return np.isfinite(v) & (v > 0.0)

        else:
            wet = valid & (data > thr)
            notes.append(f"Footprint = cells with value > {thr:g}.")

            def _wet_test(v: np.ndarray) -> np.ndarray:
                return np.isfinite(v) & (v > thr)

        wet_count = int(wet.sum())
        if wet_count == 0:
            raise ExposureEmptyFootprintError(
                f"no cell of {hazard_layer_uri!r} crosses the footprint "
                f"threshold ({'value > 0' if thr is None else f'value > {thr:g}'}); "
                f"valid_cells={int(valid.sum())}. Nothing is exposed at this "
                "threshold -- lower it to test a wider footprint."
            )

        area_km2 = _footprint_area_km2(wet, transform, crs)

        try:
            if crs is not None and str(crs).upper() != "EPSG:4326":
                bbox_4326 = tuple(
                    float(v)
                    for v in transform_bounds(crs, "EPSG:4326", *bounds)
                )
            else:
                bbox_4326 = (
                    float(bounds.left),
                    float(bounds.bottom),
                    float(bounds.right),
                    float(bounds.top),
                )
        except Exception as exc:  # noqa: BLE001
            raise ExposureInputError(
                f"could not derive an EPSG:4326 bbox from the hazard raster: {exc}"
            ) from exc

        # ---- Population (per-component degrade). ---------------------------
        population: int | None = None
        try:
            if not population_layer_uri:
                raise ExposureInputError(
                    "no population_layer_uri given: fetch_population over the "
                    "hazard bbox and pass its uri"
                )
            population = _population_in_footprint(
                population_layer_uri, wet, transform, crs, tmpdir, notes
            )
        except Exception as exc:  # noqa: BLE001 -- honest per-component degrade
            errors["population"] = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "compute_exposure_summary: population component failed: %s", exc
            )

        # ---- Buildings (per-component degrade). ----------------------------
        buildings: int | None = None
        try:
            if not buildings_layer_uri:
                raise ExposureInputError(
                    "no buildings_layer_uri given: fetch_buildings over the "
                    "hazard bbox and pass its uri"
                )
            buildings = _buildings_in_footprint(
                buildings_layer_uri, hazard_local, _wet_test, tmpdir, notes
            )
        except Exception as exc:  # noqa: BLE001 -- honest per-component degrade
            errors["buildings"] = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "compute_exposure_summary: buildings component failed: %s", exc
            )

    result: dict[str, Any] = {
        "population": population,
        "buildings": buildings,
        "area_km2": round(area_km2, 4),
        "threshold": thr,
        "footprint_cell_count": wet_count,
        "bbox": [round(v, 6) for v in bbox_4326],
        "hazard_layer_uri": hazard_layer_uri,
        "errors": errors,
        "notes": notes,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    _record_session_exposure(result)
    logger.info(
        "compute_exposure_summary: uri=%s thr=%s -> population=%s buildings=%s "
        "area_km2=%.3f errors=%s",
        hazard_layer_uri,
        thr,
        population,
        buildings,
        area_km2,
        sorted(errors) or "none",
    )
    return result
