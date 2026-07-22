"""``compute_flood_depth_damage`` composer tool -- HAZUS-curve flood damage screening.

Lightweight depth-damage estimator: the quick screening cousin of the Pelicun
chain (``run_pelicun_damage_assessment`` / ``compute_impact_envelope``). Takes
ANY flood-depth raster (a SFINCS / GeoClaw / SWMM depth COG, s3:// or local),
samples the depth at each structure point, applies the FEMA HAZUS-style
residential depth-damage curve below, multiplies by the structure replacement
value when the asset carries one (USACE NSI ``val_struct`` does), and returns
a point vector layer styled by damage fraction plus headline totals.

Structures default to the USACE National Structure Inventory fetched over the
depth raster's bounds (``fetch_usace_nsi``); pass ``assets_uri`` (FlatGeobuf /
GeoJSON points with optional ``val_struct`` / ``found_ht`` / ``occtype``
properties) to override -- the offline-test seam and the
bring-your-own-buildings path.

Depth-damage curve (documented source)
======================================

``DEPTH_DAMAGE_CURVE_FT`` is the generic ONE-STORY, NO-BASEMENT residential
STRUCTURE curve from USACE Economic Guidance Memorandum (EGM) 04-01,
"Generic Depth-Damage Relationships for Residential Structures with
Basements" companion tables (the no-basement relationship, derived from the
FIA/National Flood Insurance Program claims-based curves), which is also the
FEMA HAZUS-MH Flood Model default residential (RES1) structure curve family
(HAZUS Flood Model Technical Manual, FEMA 2013, Ch. 5). Damage fraction of
structure replacement value vs water depth ABOVE THE FIRST FLOOR in feet;
linear interpolation between the published 1-ft rows; 0 below 0 ft; capped at
the 16-ft table maximum (0.807).

When the asset carries the NSI ``found_ht`` (foundation height, ft), the
sampled ground-referenced depth is reduced by it before entering the curve
(depth above first floor); otherwise the ground depth is used directly
(noted).

HONESTY: this is a SCREENING estimate -- one aggregate claims-based curve
applied to every occupancy class, structure value only (no contents, no
inventory, no downtime), no uncertainty treatment. It is NOT a Pelicun
component-level assessment; use ``run_pelicun_damage_assessment`` /
``compute_impact_envelope`` for defensible per-asset loss work. Every result
carries that note.

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

from grace2_contracts.execution import LayerURI, LegendClass, LegendKey
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

__all__ = [
    "compute_flood_depth_damage",
    "FloodDepthDamageLayerURI",
    "FloodDamageError",
    "FloodDamageInputError",
    "FloodDamageNoStructuresError",
    "FloodDamageUpstreamError",
    "DEPTH_DAMAGE_CURVE_FT",
    "DAMAGE_FRACTION_CLASSES",
    "damage_fraction_at_depth",
]

logger = logging.getLogger("grace2_agent.tools.compute_flood_depth_damage")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class FloodDamageError(RuntimeError):
    """Base class for compute_flood_depth_damage failures."""

    error_code: str = "FLOOD_DAMAGE_ERROR"
    retryable: bool = True


class FloodDamageInputError(FloodDamageError):
    """Bad inputs (unreadable raster/assets, bad units)."""

    error_code = "FLOOD_DAMAGE_INPUT_INVALID"
    retryable = False


class FloodDamageNoStructuresError(FloodDamageError):
    """No structure points over the depth raster's footprint (honest empty)."""

    error_code = "FLOOD_DAMAGE_NO_STRUCTURES"
    retryable = False


class FloodDamageUpstreamError(FloodDamageError):
    """Input staging, the NSI fetch, or the artifact write failed."""

    error_code = "FLOOD_DAMAGE_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Result type -- LayerURI subclass carrying the summary (house side-channel).
# ---------------------------------------------------------------------------


class FloodDepthDamageLayerURI(LayerURI):
    """The depth-damage point ``LayerURI`` plus assessment summary.

    Extra fields beyond ``LayerURI``:

    - ``n_structures`` -- structure points assessed.
    - ``n_flooded`` -- points with positive sampled depth.
    - ``n_with_value`` -- points carrying a replacement value (USD).
    - ``total_damage_usd`` -- sum of fraction x value over valued points.
    - ``mean_damage_fraction`` / ``max_damage_fraction`` -- over all points.
    - ``notes`` -- honest provenance incl. the screening-estimate caveat.
    """

    n_structures: int = 0
    n_flooded: int = 0
    n_with_value: int = 0
    total_damage_usd: float = 0.0
    mean_damage_fraction: float = 0.0
    max_damage_fraction: float = 0.0
    notes: list[str] = []


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Generic one-story no-basement residential STRUCTURE depth-damage curve:
#: (depth above first floor, ft) -> damage fraction of structure replacement
#: value. Source: USACE EGM 04-01 generic depth-damage relationships
#: (FIA/NFIP claims-based, no-basement structure table), the FEMA HAZUS-MH
#: Flood Model default RES1 curve family (HAZUS FTM, FEMA 2013, Ch. 5).
#: Linear interpolation between rows; 0.0 below 0 ft; capped above 16 ft.
DEPTH_DAMAGE_CURVE_FT: tuple[tuple[float, float], ...] = (
    (0.0, 0.134),
    (1.0, 0.233),
    (2.0, 0.321),
    (3.0, 0.401),
    (4.0, 0.471),
    (5.0, 0.532),
    (6.0, 0.586),
    (7.0, 0.632),
    (8.0, 0.672),
    (9.0, 0.705),
    (10.0, 0.732),
    (11.0, 0.754),
    (12.0, 0.772),
    (13.0, 0.785),
    (14.0, 0.795),
    (15.0, 0.802),
    (16.0, 0.807),
)

#: Damage-fraction render classes: (min, max, "#rrggbb", label). The
#: categorical ``LegendKey`` (value_field="damage_fraction") is built from
#: this SAME table so the key always matches the paint.
DAMAGE_FRACTION_CLASSES: tuple[tuple[float, float, str, str], ...] = (
    (0.0, 0.001, "#bdbdbd", "No damage"),
    (0.001, 0.1, "#ffffb2", "< 10% (minor)"),
    (0.1, 0.25, "#fecc5c", "10-25% (moderate)"),
    (0.25, 0.5, "#fd8d3c", "25-50% (major)"),
    (0.5, 0.75, "#f03b20", "50-75% (severe)"),
    (0.75, 1.01, "#bd0026", ">= 75% (destroyed)"),
)

_M_TO_FT = 3.280839895

_STYLE_PRESET = "flood_depth_damage"

_METADATA = AtomicToolMetadata(
    name="compute_flood_depth_damage",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


# ---------------------------------------------------------------------------
# Curve evaluation.
# ---------------------------------------------------------------------------


def damage_fraction_at_depth(depth_ft: float) -> float:
    """Damage fraction for a depth above first floor (ft); linear interp.

    0.0 below the 0-ft table entry (water below the first floor of a
    no-basement structure); capped at the 16-ft table maximum.
    """
    if not math.isfinite(depth_ft) or depth_ft < DEPTH_DAMAGE_CURVE_FT[0][0]:
        return 0.0
    if depth_ft >= DEPTH_DAMAGE_CURVE_FT[-1][0]:
        return DEPTH_DAMAGE_CURVE_FT[-1][1]
    for (d0, f0), (d1, f1) in zip(
        DEPTH_DAMAGE_CURVE_FT, DEPTH_DAMAGE_CURVE_FT[1:]
    ):
        if d0 <= depth_ft <= d1:
            if d1 == d0:
                return f1
            t = (depth_ft - d0) / (d1 - d0)
            return f0 + t * (f1 - f0)
    return 0.0  # unreachable; defensive


# ---------------------------------------------------------------------------
# Staging helpers (mirror compute_sediment_yield / chart_tools).
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
            raise FloodDamageUpstreamError(
                f"S3 download failed for {label} uri {uri!r}: {exc}"
            ) from exc
        with open(local, "wb") as f:
            f.write(data)
        return local
    if uri.startswith(("gs://", "http://", "https://")):
        raise FloodDamageInputError(
            f"{label} uri scheme not supported: {uri!r} (use s3:// or a local path)"
        )
    if not os.path.exists(uri):
        raise FloodDamageInputError(
            f"{label} uri points at a missing local file: {uri!r}"
        )
    return uri


def _load_assets(
    assets_uri: str | None,
    raster_bbox_4326: tuple[float, float, float, float],
    tmpdir: str,
    notes: list[str],
) -> Any:
    """Structure points: caller-supplied ``assets_uri`` or NSI over the bounds."""
    import geopandas as gpd

    if assets_uri is not None:
        local = _stage_uri_local(assets_uri, tmpdir, "assets")
        try:
            gdf = gpd.read_file(local)
        except Exception as exc:  # noqa: BLE001
            raise FloodDamageInputError(
                f"could not open assets_uri {assets_uri!r}: {exc}"
            ) from exc
        notes.append(f"Structures from caller-supplied assets_uri ({assets_uri}).")
    else:
        west, south, east, north = raster_bbox_4326
        span = max(east - west, north - south)
        if span > 1.0:
            raise FloodDamageInputError(
                f"the depth raster spans {span:.2f} deg -- larger than the "
                "USACE NSI ~1-degree query limit. Pass assets_uri, or clip the "
                "depth raster (clip_raster_to_bbox) and re-run per tile."
            )
        try:
            from .fetch_usace_nsi import fetch_usace_nsi

            layer = fetch_usace_nsi(bbox=raster_bbox_4326)
        except FloodDamageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise FloodDamageUpstreamError(
                f"fetch_usace_nsi failed over the depth raster bounds "
                f"{raster_bbox_4326}: {exc}"
            ) from exc
        local = _stage_uri_local(layer.uri, tmpdir, "assets")
        try:
            gdf = gpd.read_file(local)
        except Exception as exc:  # noqa: BLE001
            raise FloodDamageUpstreamError(
                f"could not open the fetched NSI FlatGeobuf: {exc}"
            ) from exc
        notes.append(
            "Structures: USACE National Structure Inventory via fetch_usace_nsi "
            f"over the depth raster bounds {tuple(round(v, 4) for v in raster_bbox_4326)}."
        )

    if len(gdf) == 0:
        raise FloodDamageNoStructuresError(
            "no structure points over the depth raster footprint (the asset "
            "layer is empty). Nothing to assess."
        )
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
        notes.append("Assets carried no CRS; assumed EPSG:4326.")
    # Non-point geometries degrade to centroids (screening resolution).
    geom_types = set(gdf.geometry.geom_type.unique())
    if not geom_types.issubset({"Point"}):
        gdf = gdf.copy()
        gdf["geometry"] = gdf.geometry.centroid
        notes.append(
            f"Non-point asset geometries ({sorted(geom_types - {'Point'})}) "
            "reduced to centroids for depth sampling."
        )
    return gdf


# ---------------------------------------------------------------------------
# Output helpers.
# ---------------------------------------------------------------------------


def _write_output(payload: bytes, seed: str, output_dir: str | None) -> str:
    """Persist the FGB; return its URI (local for tests, runs bucket live)."""
    filename = f"flood_depth_damage_{seed}.fgb"
    if output_dir is not None:
        path = os.path.join(output_dir, filename)
        with open(path, "wb") as f:
            f.write(payload)
        return path
    try:
        from .solver import _get_runs_bucket, _get_s3_client

        bucket = _get_runs_bucket()
        key = f"flood-depth-damage-{seed}/{filename}"
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="application/octet-stream",
        )
        return f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001
        raise FloodDamageUpstreamError(
            f"failed to upload the depth-damage FGB to the runs bucket: {exc}"
        ) from exc


def _build_legend() -> LegendKey:
    """Categorical damage-fraction legend built from the SAME class table."""
    return LegendKey(
        kind="categorical",
        classes=[
            LegendClass(value_min=lo, value_max=hi, color=color, label=label)
            for lo, hi, color, label in DAMAGE_FRACTION_CLASSES
        ],
        value_field="damage_fraction",
        units="fraction of structure value",
        label="Flood damage (HAZUS-style screening)",
    )


# ---------------------------------------------------------------------------
# Registered tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: fetches its own NSI structure inventory (external USACE
    # API) unless assets_uri is passed, and stages s3 rasters -- an
    # input-fetching composer like compute_sediment_yield, so
    # open_world_hint=True is honest (listed in
    # test_tool_annotations._OPEN_WORLD_COMPUTE_EXCEPTIONS).
    open_world_hint=True,
)
def compute_flood_depth_damage(
    depth_raster_uri: str,
    assets_uri: str | None = None,
    depth_units: str = "m",
    *,
    _output_dir: str | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> FloodDepthDamageLayerURI:
    """Screen flood damage per structure from a depth raster (HAZUS-style curve).

    Use this (not run_model_flood_scenario, which SIMULATES the flood) when a flood DEPTH raster already exists and you want per-structure HAZUS damage.

    **What it does:** Samples the flood depth at every structure point (USACE
    NSI fetched over the raster bounds by default, or caller-supplied
    ``assets_uri``), converts to depth above the first floor (subtracting the
    NSI ``found_ht`` foundation height when present), applies the documented
    FEMA HAZUS-style / USACE EGM 04-01 residential one-story no-basement
    depth-damage curve, multiplies by the per-structure replacement value
    (NSI ``val_struct``) when available, and returns the points styled by
    damage fraction plus totals (structures flooded, total estimated USD).

    HONEST SCOPE: a SCREENING estimate -- one aggregate claims-based curve for
    every occupancy class, structure value only (no contents / downtime), no
    uncertainty. It is NOT a Pelicun component-level assessment; for
    defensible per-asset loss use ``run_pelicun_damage_assessment`` /
    ``compute_impact_envelope``. The result ``notes`` repeat this caveat.

    **When to use:**
    - "Roughly how much building damage does this flood cause?" right after a
      SFINCS / GeoClaw / SWMM run produces a depth COG.
    - Ranking neighborhoods / scenarios by exposed structure loss quickly.
    - A first-pass exposure screen before deciding whether to spend a Pelicun
      run on the scenario.

    **When NOT to use:**
    - Defensible per-asset loss estimates (component fragilities, contents,
      uncertainty) -- use the Pelicun chain.
    - Non-flood hazards (the curve is flood-depth-based only).
    - Areas outside NSI coverage (CONUS + AK/HI/territories) without a
      caller-supplied ``assets_uri``.

    **Parameters:**
    - ``depth_raster_uri``: flood-depth COG/GeoTIFF (s3:// or local). Positive
      values = water depth above ground; nodata / <= 0 = dry.
    - ``assets_uri``: optional structure points (FlatGeobuf / GeoJSON).
      Optional properties honored: ``val_struct`` or ``replacement_value``
      (USD), ``found_ht`` (ft), ``occtype``. Default: ``fetch_usace_nsi`` over
      the raster bounds (must span <= ~1 degree, the NSI query limit).
    - ``depth_units``: ``"m"`` (default; house depth COGs are meters) or
      ``"ft"`` if the raster is already in feet.

    **Returns:** ``FloodDepthDamageLayerURI`` -- a point vector ``LayerURI``
    (FlatGeobuf, EPSG:4326; per-feature ``depth_ft``, ``depth_above_ffe_ft``,
    ``damage_fraction``, ``damage_usd``, ``val_struct``, ``occtype``) with a
    categorical damage-fraction legend (``value_field="damage_fraction"``)
    plus ``n_structures`` / ``n_flooded`` / ``n_with_value`` /
    ``total_damage_usd`` / ``mean_damage_fraction`` / ``max_damage_fraction``
    and honest ``notes``.

    **Errors (FR-AS-11):** ``FloodDamageNoStructuresError`` (no assets over
    the footprint), ``FloodDamageInputError`` (unreadable inputs / bad units /
    raster too large for NSI), ``FloodDamageUpstreamError`` (staging / NSI /
    write failures).
    """
    units = str(depth_units or "m").strip().lower()
    if units not in ("m", "ft"):
        raise FloodDamageInputError(
            f"depth_units must be 'm' or 'ft'; got {depth_units!r}"
        )
    if not isinstance(depth_raster_uri, str) or not depth_raster_uri.strip():
        raise FloodDamageInputError(
            f"depth_raster_uri must be a non-empty URI string; got "
            f"{depth_raster_uri!r}"
        )

    try:
        import rasterio
        from rasterio.warp import transform_bounds
    except ImportError as exc:
        raise FloodDamageUpstreamError(f"rasterio unavailable: {exc}") from exc

    notes: list[str] = [
        "SCREENING ESTIMATE: one aggregate HAZUS-style residential curve "
        "(USACE EGM 04-01 one-story no-basement structure relationship, the "
        "FEMA HAZUS-MH flood default RES1 family) applied to every structure; "
        "structure value only, no contents/inventory/downtime, no uncertainty. "
        "NOT a Pelicun component-level assessment -- use "
        "run_pelicun_damage_assessment / compute_impact_envelope for "
        "defensible per-asset loss."
    ]

    with tempfile.TemporaryDirectory(prefix="grace2_depth_damage_") as tmpdir:
        depth_local = _stage_uri_local(depth_raster_uri, tmpdir, "depth")
        try:
            src = rasterio.open(depth_local)
        except Exception as exc:  # noqa: BLE001
            raise FloodDamageInputError(
                f"could not open depth raster {depth_raster_uri!r}: {exc}"
            ) from exc
        try:
            if src.crs is None:
                raise FloodDamageInputError(
                    f"depth raster {depth_raster_uri!r} carries no CRS."
                )
            bbox_4326 = tuple(
                float(v)
                for v in transform_bounds(src.crs, "EPSG:4326", *src.bounds)
            )

            gdf = _load_assets(assets_uri, bbox_4326, tmpdir, notes)

            # Sample the raster at each point (in the raster's own CRS).
            pts = gdf.to_crs(src.crs)
            coords = [(geom.x, geom.y) for geom in pts.geometry]
            nodata = src.nodata
            raw = np.array(
                [float(v[0]) for v in src.sample(coords)], dtype=np.float64
            )
            if nodata is not None and math.isfinite(float(nodata)):
                raw[raw == float(nodata)] = np.nan
        finally:
            src.close()

        # Depth in feet above ground; nodata / negative -> dry (0).
        to_ft = 1.0 if units == "ft" else _M_TO_FT
        depth_ft = np.where(np.isfinite(raw), raw * to_ft, 0.0)
        depth_ft = np.clip(depth_ft, 0.0, None)
        n_nodata = int((~np.isfinite(raw)).sum())
        if n_nodata:
            notes.append(
                f"{n_nodata} structure(s) fell on raster nodata (outside the "
                "modeled wet footprint) and were treated as dry (0 damage)."
            )

        # Depth above first floor: subtract the NSI foundation height (ft).
        if "found_ht" in gdf.columns:
            found_ht = np.array(
                [
                    float(v) if v is not None and math.isfinite(float(v)) else 0.0
                    for v in gdf["found_ht"].tolist()
                ],
                dtype=np.float64,
            )
            notes.append(
                "Depth above first floor = sampled depth - NSI found_ht "
                "(foundation height, ft) where present."
            )
        else:
            found_ht = np.zeros(len(gdf), dtype=np.float64)
            notes.append(
                "Assets carry no found_ht attribute; ground depth used as "
                "first-floor depth (conservative for raised structures)."
            )
        depth_ffe_ft = depth_ft - found_ht

        # The curve's 0-ft row (0.134) means water AT the first floor of a
        # WET structure; a dry structure (no sampled water at ground) is 0.
        wet = depth_ft > 0.0
        fractions = np.array(
            [
                damage_fraction_at_depth(d) if is_wet else 0.0
                for d, is_wet in zip(depth_ffe_ft, wet)
            ],
            dtype=np.float64,
        )

        # Replacement value: NSI val_struct (or the Pelicun duplicate column).
        value_col = None
        for cand in ("val_struct", "replacement_value"):
            if cand in gdf.columns:
                value_col = cand
                break
        if value_col is not None:
            values = np.array(
                [
                    float(v)
                    if v is not None
                    and isinstance(v, (int, float))
                    and math.isfinite(float(v))
                    else np.nan
                    for v in gdf[value_col].tolist()
                ],
                dtype=np.float64,
            )
            notes.append(f"Structure replacement value from the {value_col} attribute.")
        else:
            values = np.full(len(gdf), np.nan, dtype=np.float64)
            notes.append(
                "Assets carry no val_struct/replacement_value attribute; "
                "damage fractions are reported but USD totals cover 0 structures."
            )
        damage_usd = fractions * values  # NaN where no value

        # ---- Output vector (EPSG:4326 points). ----------------------------
        out = gdf.copy()
        out["depth_ft"] = np.round(depth_ft, 3)
        out["depth_above_ffe_ft"] = np.round(depth_ffe_ft, 3)
        out["damage_fraction"] = np.round(fractions, 4)
        out["damage_usd"] = [
            round(float(v), 2) if math.isfinite(v) else None for v in damage_usd
        ]
        if out.crs is None or out.crs.to_epsg() != 4326:
            out = out.to_crs(4326)

        fgb_path = os.path.join(tmpdir, "depth_damage.fgb")
        try:
            out.to_file(fgb_path, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise FloodDamageUpstreamError(
                f"depth-damage FlatGeobuf write failed: {exc}"
            ) from exc
        with open(fgb_path, "rb") as f:
            payload = f.read()

    n_structures = int(len(out))
    n_flooded = int((depth_ft > 0.0).sum())
    valued = np.isfinite(damage_usd)
    n_with_value = int(valued.sum())
    total_usd = float(np.nansum(damage_usd)) if n_with_value else 0.0

    seed = uuid.uuid4().hex[:8]
    uri = _write_output(payload, seed, _output_dir)

    logger.info(
        "compute_flood_depth_damage: raster=%s -> %d structures (%d flooded, "
        "%d valued) total=%.0f USD",
        depth_raster_uri,
        n_structures,
        n_flooded,
        n_with_value,
        total_usd,
    )
    return FloodDepthDamageLayerURI(
        layer_id=f"flood-depth-damage-{seed}",
        name=f"Flood depth-damage screening ({n_structures} structures)",
        layer_type="vector",
        uri=uri,
        style_preset=_STYLE_PRESET,
        role="primary",
        units="fraction of structure value",
        bbox=tuple(round(float(v), 6) for v in bbox_4326),
        legend=_build_legend(),
        n_structures=n_structures,
        n_flooded=n_flooded,
        n_with_value=n_with_value,
        total_damage_usd=round(total_usd, 2),
        mean_damage_fraction=round(float(fractions.mean()), 4) if n_structures else 0.0,
        max_damage_fraction=round(float(fractions.max()), 4) if n_structures else 0.0,
        notes=notes,
    )
