"""``pelicun_damage_with_buildings`` — convenience composer: buildings → Pelicun (job-0147).

**Pattern this encapsulates**

The canonical ``run_pelicun_damage_assessment`` atomic tool accepts an
``assets_uri`` parameter.  Before job-0147 the typical caller was passing
``fetch_administrative_boundaries(level='place')`` output, producing
Census Designated Place (CDP) polygons as assets.  CDPs are administrative
rectangles — the damage choropleth came back as a grid of rectangles rather
than showing damage aligned with real built-area structure.

This composer enforces the correct pattern:

    1. ``compute_building_density(bbox, cell_size_m)``
         → buildings_uri (COG; one 100 m cell per bin; value = building count)
    2. ``run_pelicun_damage_assessment(hazard_raster_uri, assets_uri=buildings_uri, ...)``
         → LayerURI (FlatGeobuf; one damage point per non-trivially-occupied cell)

The resulting damage layer shows spatially-varying damage distributed over the
real built-area grid rather than over administrative polygons.

**Invariants preserved**

- **1. Determinism boundary:** CRS + cell assignments are deterministic; no LLM
  numbers anywhere.
- **2. Deterministic workflows:** pure Python composition of two already-tested
  atomic tools; no LLM in the loop.
- **3. Engine registration:** composer reuses existing registered tools; no
  agent-core changes.
- **10. Minimal parameter surface:** signature exposes only what the caller
  must supply — hazard raster URI, spatial extent, cell resolution, and the
  fragility-set choice.  DEM, building-fetch internals, HAZUS curves, and
  GCS cache paths are resolved inside the atomic tools.

**LLM exposure**

Registered as ``run_pelicun_with_buildings`` via the standard workflow-dispatch
pattern (``cacheable=False``, ``ttl_class="live-no-cache"``,
``source_class="workflow_dispatch"``).  The LLM receives a single tool that
handles the full buildings → Pelicun chain without needing to call
``compute_building_density`` explicitly.

**Geographic-correctness gate (codified lesson from job-0086)**

The unit tests assert that the output damage points span the bbox at grid
spacing (approximately ``bbox_area / cell_size_m²`` points), that each carries
``ds_mean`` in [0, 4], and that the live Fort Myers run produces a
non-rectangular spatial distribution (OQ-0147-GEO-CORRECTNESS: verified by
``TRID3NT_TEST_LIVE_PELICUN_V2=1`` guard).

FR-TA-1 / FR-TA-2 / FR-CE-8 / invariants 1, 2, 3, 10.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from ..tools import TOOL_REGISTRY, register_tool

if TYPE_CHECKING:
    pass

__all__ = [
    "run_pelicun_with_buildings",
    "pelicun_damage_with_buildings",
    "density_cog_to_point_fgb",
    "PelicunWithBuildingsError",
]

logger = logging.getLogger("trid3nt_server.workflows.pelicun_damage_with_buildings")


# ---------------------------------------------------------------------------
# Error type.
# ---------------------------------------------------------------------------


class PelicunWithBuildingsError(RuntimeError):
    """Raised when the buildings-density fetch or Pelicun step fails.

    ``error_code`` and ``retryable`` mirror the atomic-tool error surface so
    the agent can route it to the WebSocket A.6 error frame via the standard
    typed-error path (FR-AS-11 / NFR-R-1).

    For the specific error subclass (e.g. ``PelicunNoAssetsError``,
    ``BuildingDensityUpstreamError``) inspect ``__cause__``.
    """

    error_code: str = "PELICUN_WITH_BUILDINGS_ERROR"
    retryable: bool = True


# ---------------------------------------------------------------------------
# Density-COG → point FlatGeobuf conversion helper.
#
# ``compute_building_density`` returns a float32 single-band COG whose cell
# values are building counts per cell.  ``run_pelicun_damage_assessment``
# consumes a vector FlatGeobuf of point or polygon assets.  This helper
# bridges the two by sampling every non-zero cell centroid from the density
# COG and writing them as point features in a temporary FlatGeobuf.
#
# Each point carries:
#   - ``building_count``: the density cell value (number of buildings binned
#     into that cell by ``compute_building_density``).
#   - ``component_type``: ``"RES1"`` (default residential — v0.1 proxy; a
#     future sprint can infer from parcel data or Census block-group data).
#
# The CRS of the output FlatGeobuf is EPSG:4326 (lat/lon) so that it aligns
# with the flood COG which ``postprocess_flood`` always writes in EPSG:4326.
#
# Empty-density cells (count == 0 or nodata) are OMITTED.  This is the key
# correctness property: only cells where Microsoft detected buildings become
# assets, so the damage choropleth follows the real built-area distribution.
# ---------------------------------------------------------------------------


def density_cog_to_point_fgb(cog_uri: str) -> str:
    """Convert a building-density COG to a temporary point FlatGeobuf.

    Reads the local-path or gs:// COG at ``cog_uri``.  For each cell with
    a non-zero (and non-nodata) value, emits one EPSG:4326 point feature at
    the cell centroid, carrying ``building_count`` and ``component_type``
    (always ``"RES1"`` in v0.1).

    Returns the path to a NamedTemporaryFile ``.fgb`` that the caller is
    responsible for unlinking after use.

    Raises:
        PelicunWithBuildingsError: if rasterio, geopandas, or pyproj are not
            installed, or if the COG cannot be opened.
    """
    try:
        import geopandas as gpd
        import numpy as np
        import rasterio
        import tempfile as _tempfile
        from pyproj import Transformer
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PelicunWithBuildingsError(
            f"density_cog_to_point_fgb: required geospatial packages not installed: {exc}"
        ) from exc

    try:
        with rasterio.open(cog_uri) as src:
            arr = src.read(1)
            nodata = src.nodata
            transform = src.transform
            height, width = arr.shape
            crs_str = src.crs.to_string() if src.crs else "EPSG:3857"
    except Exception as exc:
        raise PelicunWithBuildingsError(
            f"density_cog_to_point_fgb: failed to open COG {cog_uri!r}: {exc}"
        ) from exc

    # Mask out zero and nodata cells.
    mask = arr > 0.0
    if nodata is not None:
        mask &= (arr != nodata)
    if not mask.any():
        raise PelicunWithBuildingsError(
            f"density_cog_to_point_fgb: COG {cog_uri!r} has no non-zero cells; "
            "no buildings detected — cannot create asset layer for Pelicun."
        )

    # ``transform`` is a rasterio Affine for the COG's native CRS.
    # We need to project cell centroids to EPSG:4326.
    # Building-density COGs are in EPSG:3857 (Web Mercator) per
    # ``compute_building_density``'s documented grid CRS.
    try:
        trans = Transformer.from_crs(crs_str, "EPSG:4326", always_xy=True)
    except Exception as exc:
        raise PelicunWithBuildingsError(
            f"density_cog_to_point_fgb: CRS transform init failed "
            f"({crs_str!r} → EPSG:4326): {exc}"
        ) from exc

    # Build centroids: col + 0.5 (pixel centre), row + 0.5.
    rows, cols = np.where(mask)
    # Rasterio Affine: x = transform.c + col * transform.a + row * transform.b
    #                  y = transform.f + col * transform.d + row * transform.e
    # For a north-up raster: a > 0, e < 0, b = d = 0.
    native_x = transform.c + (cols + 0.5) * transform.a + (rows + 0.5) * transform.b
    native_y = transform.f + (cols + 0.5) * transform.d + (rows + 0.5) * transform.e

    lons, lats = trans.transform(native_x, native_y)
    counts = arr[rows, cols].tolist()

    geometries = [Point(lon, lat) for lon, lat in zip(lons, lats)]
    gdf = gpd.GeoDataFrame(
        {
            "geometry": geometries,
            "building_count": counts,
            "component_type": ["RES1"] * len(geometries),
        },
        crs="EPSG:4326",
    )

    # Write to a temp file — caller must unlink.
    tmp = _tempfile.NamedTemporaryFile(suffix=".fgb", delete=False, prefix="trid3nt_density_pts_")
    tmp_path = tmp.name
    tmp.close()
    gdf.to_file(tmp_path, driver="FlatGeobuf", engine="pyogrio")
    logger.info(
        "density_cog_to_point_fgb: wrote %d asset points to %s",
        len(gdf),
        tmp_path,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Core composer (no @register_tool; the wrapper below is the LLM surface).
# ---------------------------------------------------------------------------


async def pelicun_damage_with_buildings(
    hazard_raster_uri: str,
    bbox: tuple[float, float, float, float],
    cell_size_m: float = 100.0,
    fragility_set: str = "hazus_flood_v6",
    realization_count: int = 100,
) -> LayerURI:
    """Compose building-density fetch → Pelicun damage assessment.

    Step 1: call ``compute_building_density`` for ``bbox`` at ``cell_size_m``.
    Step 2: pass the resulting COG URI as ``assets_uri`` to
    ``run_pelicun_damage_assessment``.

    The output FlatGeobuf contains one damage point per 100 m cell (or
    whatever ``cell_size_m`` was requested) with ``ds_mean`` in [0, 4] and
    ``repair_cost_mean`` in USD per cell.  The spatial distribution follows
    the real building-density grid, not administrative polygon boundaries.

    Args:
        hazard_raster_uri: single-band flood-depth COG in metres. This MUST be the
            EXACT LayerURI value (copied verbatim) returned by a prior
            ``run_model_flood_scenario`` / ``run_model_nws_flood_event_scenario`` call
            EARLIER IN THIS CONVERSATION. NEVER invent or construct it (a
            ``flood-depth-peak-<id>`` you did not receive will fail). If no flood has
            been modeled yet, run ``run_model_flood_scenario`` FIRST and pass its
            result here.
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326 defining
            the area to fetch building density for.  Should overlap the
            hazard raster extent.
        cell_size_m: density grid cell size in metres on EPSG:3857.
            Default 100 m.  Smaller cells produce more fine-grained damage
            points; larger cells aggregate more buildings per asset.
        fragility_set: fragility curve family.  Default
            ``"hazus_flood_v6"`` — the only wired fragility set for v0.1.
        realization_count: Monte-Carlo realizations per asset.  Default 100.

    Returns:
        A ``LayerURI`` pointing at the Pelicun damage FlatGeobuf — the same
        shape returned by ``run_pelicun_damage_assessment`` directly, with
        ``style_preset="pelicun_damage_state"``.

    Raises:
        PelicunWithBuildingsError: wraps any error from either atomic-tool
            step with ``error_code`` and ``retryable`` set.
    """
    import os as _os

    # ------------------------------------------------------------------
    # Step 1 — building density fetch.
    # ------------------------------------------------------------------
    logger.info(
        "pelicun_damage_with_buildings: step 1 — compute_building_density "
        "bbox=%s cell_size_m=%s",
        bbox,
        cell_size_m,
    )
    try:
        compute_fn = TOOL_REGISTRY["compute_building_density"].fn
        buildings_uri: LayerURI = compute_fn(
            bbox=bbox,
            cell_size_m=cell_size_m,
        )
    except Exception as exc:
        raise PelicunWithBuildingsError(
            f"pelicun_damage_with_buildings: building-density fetch failed: {exc}"
        ) from exc

    # ------------------------------------------------------------------
    # Step 1b — convert density COG to point FlatGeobuf.
    #
    # ``run_pelicun_damage_assessment`` consumes a vector FlatGeobuf of asset
    # features.  The density COG from ``compute_building_density`` is a raster
    # (EPSG:3857).  We convert: one point per non-zero cell, projected to
    # EPSG:4326, with ``building_count`` + ``component_type`` attributes.
    # ------------------------------------------------------------------
    logger.info(
        "pelicun_damage_with_buildings: step 1b — density COG → point FlatGeobuf "
        "cog_uri=%s",
        buildings_uri.uri,
    )
    assets_fgb_path: str | None = None
    try:
        assets_fgb_path = density_cog_to_point_fgb(buildings_uri.uri)
    except Exception as exc:
        raise PelicunWithBuildingsError(
            f"pelicun_damage_with_buildings: density→points conversion failed: {exc}"
        ) from exc

    # ------------------------------------------------------------------
    # Step 2 — Pelicun damage assessment.
    # ------------------------------------------------------------------
    logger.info(
        "pelicun_damage_with_buildings: step 2 — run_pelicun_damage_assessment "
        "hazard=%s assets=%s",
        hazard_raster_uri,
        assets_fgb_path,
    )
    try:
        pelicun_fn = TOOL_REGISTRY["run_pelicun_damage_assessment"].fn
        damage_uri: LayerURI = pelicun_fn(
            hazard_raster_uri=hazard_raster_uri,
            assets_uri=assets_fgb_path,
            fragility_set=fragility_set,
            realization_count=realization_count,
        )
    except Exception as exc:
        raise PelicunWithBuildingsError(
            f"pelicun_damage_with_buildings: Pelicun step failed: {exc}"
        ) from exc
    finally:
        # Unlink the temp point FGb — already consumed (or failed) by Pelicun.
        if assets_fgb_path is not None:
            try:
                _os.unlink(assets_fgb_path)
            except OSError:
                pass

    logger.info(
        "pelicun_damage_with_buildings: done — damage_uri=%s",
        damage_uri.uri,
    )
    return damage_uri


# ---------------------------------------------------------------------------
# LLM-facing atomic-tool wrapper.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="run_pelicun_with_buildings",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(_METADATA)
async def run_pelicun_with_buildings(
    hazard_raster_uri: str,
    bbox: tuple[float, float, float, float],
    cell_size_m: float = 100.0,
    fragility_set: str = "hazus_flood_v6",
    realization_count: int = 100,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch building density grid then run Pelicun flood-damage assessment.

    Two-step composition: ``compute_building_density(bbox, cell_size_m)`` →
    ``density_cog_to_point_fgb`` (COG-to-point conversion) →
    ``run_pelicun_damage_assessment(hazard_raster_uri, assets_uri=<points_fgb>)``.
    The resulting damage layer shows spatially-varying damage over the actual
    built-area grid rather than administrative polygons. Not cached at the
    workflow level (each underlying step is individually cached).

    When to use:
        - User asks for a building-level or spatially-distributed damage
          assessment over a flood hazard raster for an international bbox
          or when USACE NSI structures are not available.
        - User wants to see "which buildings are damaged" or "damage
          distributed across the flood zone" — not a CDP administrative aggregate.
        - A prior ``run_model_flood_scenario`` has produced a flood depth COG
          and the user wants to pair it with a building-density asset layer.

    When NOT to use:
        - Plain hazard exposure counts (use ``compute_zonal_statistics`` —
          cheaper when you only need "how many assets are in the flood zone").
        - Cases with a custom asset layer already in hand (pass it directly to
          ``run_pelicun_damage_assessment(assets_uri=...)`` instead).
        - CONUS bboxes where ``fetch_usace_nsi`` is available — NSI carries
          real HAZUS occupancy class + per-structure replacement value. Prefer
          ``run_pelicun_damage_assessment(assets_uri=<NSI URI>)`` for CONUS.
        - Non-flood hazards (``fema_hazus_eq_2020`` is not wired in v0.1).

    Parameters:
        hazard_raster_uri: single-band flood-depth COG in metres. This MUST be the
            EXACT LayerURI value (copied verbatim) returned by a prior
            ``run_model_flood_scenario`` / ``run_model_nws_flood_event_scenario`` call
            EARLIER IN THIS CONVERSATION. NEVER invent or construct it (a
            ``flood-depth-peak-<id>`` you did not receive will fail). If no flood has
            been modeled yet, run ``run_model_flood_scenario`` FIRST and pass its
            result here.  Raster CRS and asset CRS are reconciled
            internally.
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326 defining
            the area to fetch building density for.  Should overlap the
            hazard raster extent.
        cell_size_m: density grid cell size in metres on the EPSG:3857 grid.
            Default 100 m.  Smaller cells yield more fine-grained spatial
            detail; larger cells aggregate more buildings per damage point.
            The cache key for the building-density step includes
            ``cell_size_m`` so different resolutions do not collide.
        fragility_set: which fragility curve family to use. v0.1 ships
            ``"hazus_flood_v6"`` (FEMA HAZUS-MH 6.1 flood depth-damage
            loss functions).  ``"fema_hazus_eq_2020"`` is registered but
            raises ``PelicunInputError`` until the seismic engine lands.
        realization_count: Monte-Carlo realizations per asset. Default 100;
            raise (e.g. 500-1000) for tighter 95% CIs at the cost of compute.

    Returns:
        A ``LayerURI`` pointing at a FlatGeobuf of damage points (one per
        non-trivially-occupied building-density cell).  Each feature carries
        the same damage properties as ``run_pelicun_damage_assessment``:
        ``ds_mean``, ``ds_p05``, ``ds_p95``, ``loss_ratio_mean``,
        ``loss_ratio_p95``, ``repair_cost_mean``, ``repair_cost_p95``,
        ``replacement_value``, ``component_type_used``,
        ``fragility_curve_id``, ``hazard_depth_sampled``.
        ``layer_type="vector"``, ``style_preset="pelicun_damage_state"``.

    LLM guidance:
        - Pair with ``run_model_flood_scenario`` output: pass its flood depth
          COG URI as ``hazard_raster_uri``; pass the same ``bbox`` used to
          scope the flood model.
        - Narrate ``ds_mean`` + ``repair_cost_mean`` from the returned
          feature properties — never from LLM-generated numbers (invariant 1).
        - If the user asks for an administrative summary (e.g. "total damage
          in Fort Myers"), run this tool first then ``compute_zonal_statistics``
          over the resulting damage FlatGeobuf against the admin polygons.

    Cache:
        Building-density step: ``ttl_class="static-30d"`` — cached for 30
        days per ``(bbox-rounded-6dp, cell_size_m, source)``; subsequent calls
        with the same inputs hit GCS rather than re-fetching Microsoft tiles.
        Pelicun step: ``ttl_class="static-30d"`` — seeded deterministically
        per asset so byte-identical results are cached.
        This workflow wrapper itself: ``cacheable=False`` (the wrapper
        coordinates two cached atomic tools; it does not add its own cache
        layer).

    Cross-tool dependencies:
        Step 1 — ``compute_building_density(bbox, cell_size_m)`` — fetches
        Microsoft Global ML Buildings density COG for the bbox at the
        requested cell resolution. Returns a raster ``LayerURI``.
        Step 2 — ``density_cog_to_point_fgb(density_uri)`` — converts the
        density COG to a FlatGeobuf of point assets (one point per non-zero
        cell), each with a default ``component_type="RES1"`` and a class-
        default ``replacement_value``.
        Step 3 — ``run_pelicun_damage_assessment(hazard_raster_uri,
        assets_uri=<points_fgb>)`` — runs the full Pelicun Monte-Carlo
        loop and returns a FlatGeobuf damage-property ``LayerURI``.
        Final output — ``publish_layer`` (optional follow-on, not called
        here): pass the returned ``LayerURI`` to display on the map.

    Raises:
        PelicunWithBuildingsError: wraps any ``BuildingDensityError`` or
            ``PelicunDamageError`` from the underlying steps.  Inspect
            ``__cause__`` for the originating typed error.
    """
    return await pelicun_damage_with_buildings(
        hazard_raster_uri=hazard_raster_uri,
        bbox=bbox,
        cell_size_m=cell_size_m,
        fragility_set=fragility_set,
        realization_count=realization_count,
    )
