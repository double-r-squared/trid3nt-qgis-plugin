"""``compute_exposure_summary`` composer tool -- who/what is inside a hazard footprint.

Given a hazard raster (flood depth COG, landslide probability, plume
concentration, ...) this tool derives the hazard FOOTPRINT (cells whose value
crosses ``threshold``; default: any wet/positive cell) and computes three
exposure numbers inside it:

    population  -- sum of WorldPop gridded population (people) over footprint
                   cells, via the SAME ``fetch_population`` machinery the rest
                   of the surface uses (WorldPop Global_2000_2020, keyless).
    buildings   -- count of building footprints whose representative point
                   falls on a footprint cell, via the SAME ``fetch_buildings``
                   machinery (OSM Overpass primary, MS fallback).
    area_km2    -- footprint area in km^2 straight off the hazard grid.

Honesty (data-source fallback norm)
===================================

Exposure degrades PER COMPONENT, never silently and never fabricated:

- If the WorldPop fetch/read fails, ``population`` is ``None`` and
  ``errors["population"]`` carries the real reason; buildings + area still
  compute (and vice versa for buildings).
- ``area_km2`` comes from the hazard raster itself -- if THAT cannot be read
  the whole call raises a typed error (there is no exposure without a
  footprint).
- A footprint with ZERO cells over the threshold raises the typed
  ``ExposureEmptyFootprintError`` -- "nothing is exposed" is narrated as an
  explicit no-footprint signal, not a fabricated row of zeros that reads like
  a computed exposure.

Session side-channel
====================

The most recent result is recorded in a small module-level store keyed by the
turn's Case (``pipeline_emitter.current_turn_case``) so ``compose_case_report``
can fold the exposure numbers into the case situation report without
re-fetching. The store is in-memory and session-scoped by construction.

``cacheable=False`` (``ttl_class="live-no-cache"``): this is an analysis
composer over a caller-named artifact; the underlying WorldPop / buildings
fetches are themselves cached by their own tools.
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

from . import register_tool

__all__ = [
    "compute_exposure_summary",
    "get_session_exposure",
    "ExposureSummaryError",
    "ExposureInputError",
    "ExposureEmptyFootprintError",
    "ExposureUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.compute_exposure_summary")


# ---------------------------------------------------------------------------
# Typed errors (FR-AS-11).
# ---------------------------------------------------------------------------


class ExposureSummaryError(RuntimeError):
    """Base class for compute_exposure_summary failures."""

    error_code: str = "EXPOSURE_SUMMARY_ERROR"
    retryable: bool = True


class ExposureInputError(ExposureSummaryError):
    """Bad inputs (missing/unreadable hazard uri, non-finite threshold)."""

    error_code = "EXPOSURE_INPUT_INVALID"
    retryable = False


class ExposureEmptyFootprintError(ExposureSummaryError):
    """No hazard cell crosses the threshold -- an honest empty footprint.

    "Nothing exposed" is a typed signal the agent narrates explicitly; it is
    never returned as a zeros row that reads like a computed exposure.
    """

    error_code = "EXPOSURE_EMPTY_FOOTPRINT"
    retryable = False


class ExposureUpstreamError(ExposureSummaryError):
    """Hazard-raster staging or read failed."""

    error_code = "EXPOSURE_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Session store (read by compose_case_report).
# ---------------------------------------------------------------------------

_GLOBAL_KEY = "__global__"
_MAX_SESSION_ENTRIES = 32

#: Last exposure result per Case (insertion-order bounded). In-memory only --
#: honest session scope; a fresh process has no exposure history.
_SESSION_EXPOSURE: dict[str, dict[str, Any]] = {}


def _record_session_exposure(result: dict[str, Any]) -> None:
    try:
        from ..pipeline_emitter import current_turn_case

        key = current_turn_case() or _GLOBAL_KEY
    except Exception:  # noqa: BLE001 -- store is best-effort, never a gate
        key = _GLOBAL_KEY
    _SESSION_EXPOSURE.pop(key, None)
    _SESSION_EXPOSURE[key] = result
    while len(_SESSION_EXPOSURE) > _MAX_SESSION_ENTRIES:
        _SESSION_EXPOSURE.pop(next(iter(_SESSION_EXPOSURE)))


def get_session_exposure(case_id: str | None) -> dict[str, Any] | None:
    """Return the exposure summary computed this session for ``case_id``.

    Falls back to the global (no-Case-bound) slot; returns ``None`` when no
    exposure summary has been computed this session. Consumed by
    ``compose_case_report``.
    """
    if case_id and case_id in _SESSION_EXPOSURE:
        return _SESSION_EXPOSURE[case_id]
    return _SESSION_EXPOSURE.get(_GLOBAL_KEY)


# ---------------------------------------------------------------------------
# Metadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="compute_exposure_summary",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)


# ---------------------------------------------------------------------------
# Fetcher indirections (monkeypatch seams for offline tests).
# ---------------------------------------------------------------------------


def _fetch_population_layer(
    bbox: tuple[float, float, float, float], dataset: str
) -> Any:
    """WorldPop raster ``LayerURI`` for ``bbox`` (the fetch_population seam)."""
    from .data_fetch import fetch_population

    return fetch_population(bbox=bbox, dataset=dataset)


def _fetch_buildings_layer(bbox: tuple[float, float, float, float]) -> Any:
    """Building-footprint vector ``LayerURI`` (the fetch_buildings seam)."""
    from .data_fetch import fetch_buildings

    return fetch_buildings(bbox=bbox)


# ---------------------------------------------------------------------------
# Staging + footprint helpers.
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
    """Footprint area (km^2) off the hazard grid.

    Projected CRS: |det| of the affine cell vectors (meters assumed).
    Geographic CRS: per-row cell area with a cos(latitude) correction --
    adequate at AOI scale (the digitize_water_body Web-Mercator-class
    approximation).
    """
    is_geographic = bool(getattr(crs, "is_geographic", False)) if crs else True
    if not is_geographic:
        cell_m2 = abs(transform.a * transform.e - transform.b * transform.d)
        return float(wet.sum()) * cell_m2 / 1e6

    dx_deg = abs(transform.a)
    dy_deg = abs(transform.e)
    rows = np.arange(wet.shape[0], dtype=np.float64) + 0.5
    row_lats = transform.f + rows * transform.e  # row-center latitudes
    row_cell_m2 = (
        dx_deg * 111_320.0 * np.cos(np.radians(row_lats)) * (dy_deg * 110_540.0)
    )
    wet_per_row = wet.sum(axis=1).astype(np.float64)
    return float((wet_per_row * row_cell_m2).sum() / 1e6)


def _population_in_footprint(
    bbox_4326: tuple[float, float, float, float],
    wet: np.ndarray,
    hazard_transform: Any,
    hazard_crs: Any,
    dataset: str,
    tmpdir: str,
    notes: list[str],
) -> int:
    """Sum WorldPop people over footprint cells.

    The hazard footprint mask is reprojected (nearest) onto the population
    grid; population is summed where the mask lands. Raises on any failure --
    the caller converts to the per-component honest error.
    """
    import rasterio
    from rasterio.warp import Resampling, reproject

    layer = _fetch_population_layer(bbox_4326, dataset)
    pop_local = _stage_uri_local(str(layer.uri), tmpdir, "population")
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
        f"Population: WorldPop ({dataset}) cells whose center falls on the "
        "hazard footprint (nearest-neighbor mask transfer). WorldPop cells "
        "are coarser than most hazard grids, so edge cells are counted "
        "whole-cell -- a screening estimate, not a parcel census."
    )
    return population


def _buildings_in_footprint(
    bbox_4326: tuple[float, float, float, float],
    hazard_local: str,
    wet_test: Any,
    tmpdir: str,
    notes: list[str],
) -> int:
    """Count building footprints whose representative point is on a wet cell."""
    import geopandas as gpd
    import rasterio

    layer = _fetch_buildings_layer(bbox_4326)
    bld_local = _stage_uri_local(str(layer.uri), tmpdir, "buildings")
    gdf = gpd.read_file(bld_local)
    gdf = gdf[gdf.geometry.notna()]
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    total = int(len(gdf))
    if total == 0:
        notes.append(
            "Buildings: the footprint fetch returned zero buildings in the "
            "hazard bbox; exposed-building count is an honest 0."
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


# ---------------------------------------------------------------------------
# Registered tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Fetches WorldPop + Overpass buildings (external APIs) => open world.
    read_only_hint=True,
    open_world_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
)
def compute_exposure_summary(
    hazard_layer_uri: str,
    threshold: float | None = None,
    population_dataset: str = "worldpop_2020",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Summarize exposure (population, buildings, area) inside a hazard footprint.

    **What it does:** Derives the hazard footprint from a hazard raster (cells
    with ``value > threshold``; default threshold: any wet/positive cell,
    i.e. ``value > 0``) and computes: total WorldPop population on footprint
    cells (via ``fetch_population``), the count of building footprints whose
    representative point falls on a footprint cell (via ``fetch_buildings``),
    and the footprint area in km^2.

    **When to use:**
    - "How many people / buildings are in the flood zone?" right after a
      flood/surge/plume solve produced a depth or intensity raster.
    - Headline exposure numbers for a situation report
      (``compose_case_report`` picks this result up automatically).

    **When NOT to use:**
    - Dollar losses or per-structure damage states -- use
      ``compute_flood_depth_damage`` (screening) or
      ``run_pelicun_damage_assessment`` (defensible).
    - Generic raster-in-zone statistics -- use ``compute_zonal_statistics``.

    **Parameters:**
    - ``hazard_layer_uri``: the hazard raster (s3:// or local GeoTIFF/COG,
      e.g. a flood-depth layer's uri). Single band; the footprint comes from
      band 1.
    - ``threshold``: footprint cutoff in the raster's own units (e.g. ``0.5``
      for >= 0.5 m depth). Default ``None`` = any positive (wet) cell.
    - ``population_dataset``: WorldPop vintage token (default
      ``"worldpop_2020"``; see ``fetch_population``).

    **Returns:** dict with
    ``population`` (int or None), ``buildings`` (int or None),
    ``area_km2`` (float), ``threshold`` (the cutoff actually used; None means
    any-positive), ``bbox`` (footprint raster bounds, EPSG:4326),
    ``errors`` (dict: per-component honest failure reasons, {} when clean),
    ``notes`` (provenance + approximations), ``hazard_layer_uri``,
    ``computed_at``. A failed component is ``None`` WITH its error recorded --
    components degrade independently, values are never fabricated.

    **Errors (FR-AS-11):** ``ExposureInputError`` (bad uri/threshold),
    ``ExposureEmptyFootprintError`` (no cell crosses the threshold -- an
    honest "nothing exposed" signal), ``ExposureUpstreamError`` (hazard
    staging/read failed).
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

        # Raster bounds in EPSG:4326 drive the population/buildings fetches.
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
            population = _population_in_footprint(
                bbox_4326, wet, transform, crs, population_dataset, tmpdir, notes
            )
        except Exception as exc:  # noqa: BLE001 -- honest per-component degrade
            errors["population"] = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "compute_exposure_summary: population component failed: %s", exc
            )

        # ---- Buildings (per-component degrade). ----------------------------
        buildings: int | None = None
        try:
            buildings = _buildings_in_footprint(
                bbox_4326, hazard_local, _wet_test, tmpdir, notes
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
