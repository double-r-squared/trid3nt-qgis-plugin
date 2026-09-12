"""``compute_flood_depth_damage`` - HAZUS-curve flood damage screening.

A SCREENING estimate: one aggregate claims-based curve over every occupancy
class, structure value only, no contents, downtime or uncertainty treatment.
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

logger = logging.getLogger("trid3nt_server.tools.derive.compute_flood_depth_damage.compute_flood_depth_damage")




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




class FloodDepthDamageLayerURI(LayerURI):
    """The depth-damage point ``LayerURI`` plus the assessment summary: counts,
    total damage in USD over valued points, fraction statistics, honest notes.
    """

    n_structures: int = 0
    n_flooded: int = 0
    n_with_value: int = 0
    total_damage_usd: float = 0.0
    mean_damage_fraction: float = 0.0
    max_damage_fraction: float = 0.0
    notes: list[str] = []



#: Generic one-story no-basement residential STRUCTURE depth-damage curve:
#: (depth above first floor, ft) -> damage fraction of structure replacement
#: value. USACE EGM 04-01 generic depth-damage relationships, the FEMA HAZUS-MH
#: flood default RES1 family. Linear interpolation between the published 1-ft
#: rows; 0.0 below 0 ft; capped at the 16-ft maximum.
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

#: Damage-fraction render classes: (min, max, "#rrggbb", label) - the breaks
#: the declared style row classifies on, and so the breaks the .qml paints.
DAMAGE_FRACTION_CLASSES: tuple[tuple[float, float, str, str], ...] = (
    (0.0, 0.001, "#bdbdbd", "No damage"),
    (0.001, 0.1, "#ffffb2", "< 10% (minor)"),
    (0.1, 0.25, "#fecc5c", "10-25% (moderate)"),
    (0.25, 0.5, "#fd8d3c", "25-50% (major)"),
    (0.5, 0.75, "#f03b20", "50-75% (severe)"),
    (0.75, 1.01, "#bd0026", ">= 75% (destroyed)"),
)

_M_TO_FT = 3.280839895

#: Damage is read as a class, not a ramp: the breaks the legend shows are the
#: breaks the paint uses.
_STYLE: dict = {
    "kind": "classed",
    "geometry": "point",
    "attribute": "damage_fraction",
    "units": "fraction of structure value",
    "label": "Flood damage (HAZUS-style screening)",
    "classes": [list(c) for c in DAMAGE_FRACTION_CLASSES],
}

def _build_legend() -> Any:
    """The declared style row, resolved - the same table the paint uses."""
    from trid3nt_server.render import presets

    return presets.legend_key(_STYLE)


_METADATA = AtomicToolMetadata(
    name="compute_flood_depth_damage",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)




def damage_fraction_at_depth(depth_ft: float) -> float:
    """Damage fraction for a depth above first floor in feet: linear between the
    table rows, 0.0 below its 0-ft entry, capped at its 16-ft maximum.
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
    return 0.0




def _stage_uri_local(uri: str, tmpdir: str, label: str) -> str:
    """Return a local file path for ``uri`` (s3:// download or local path)."""
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

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


def _load_assets(assets_uri: Any, tmpdir: str, notes: list[str]) -> Any:
    """Structure points from the layer the caller handed in."""
    import geopandas as gpd

    if not isinstance(assets_uri, str) or not assets_uri.strip():
        raise FloodDamageInputError(
            "assets_uri is required: fetch the structure inventory first "
            "(fetch_usace_nsi over the depth raster's bounds, or fetch_buildings) "
            "and pass its uri."
        )
    local = _stage_uri_local(assets_uri, tmpdir, "assets")
    try:
        gdf = gpd.read_file(local)
    except Exception as exc:  # noqa: BLE001
        raise FloodDamageInputError(
            f"could not open assets_uri {assets_uri!r}: {exc}"
        ) from exc
    notes.append(f"Structures from assets_uri ({assets_uri}).")

    if len(gdf) == 0:
        raise FloodDamageNoStructuresError(
            "no structure points over the depth raster footprint (the asset "
            "layer is empty). Nothing to assess."
        )
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
        notes.append("Assets carried no CRS; assumed EPSG:4326.")
    # A non-point geometry degrades to its centroid at screening resolution.
    geom_types = set(gdf.geometry.geom_type.unique())
    if not geom_types.issubset({"Point"}):
        gdf = gdf.copy()
        gdf["geometry"] = gdf.geometry.centroid
        notes.append(
            f"Non-point asset geometries ({sorted(geom_types - {'Point'})}) "
            "reduced to centroids for depth sampling."
        )
    return gdf




def _write_output(payload: bytes, seed: str, output_dir: str | None) -> str:
    """Persist the FGB and return its URI: a local path when ``output_dir`` is
    given, else an ``s3://`` key in the runs bucket.
    """
    filename = f"flood_depth_damage_{seed}.fgb"
    if output_dir is not None:
        path = os.path.join(output_dir, filename)
        with open(path, "wb") as f:
            f.write(payload)
        return path
    try:
        from trid3nt_server import storage

        bucket = storage.runs_bucket()
        key = f"flood-depth-damage-{seed}/{filename}"
        storage.client().put_object(
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




@register_tool(
    _METADATA,
    # Reads two layers it was handed; nothing external.
    open_world_hint=False,
)
def compute_flood_depth_damage(
    depth_raster_uri: str,
    assets_uri: str,
    depth_units: str = "m",
    *,
    _output_dir: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> FloodDepthDamageLayerURI:
    """Screen flood damage per structure from a depth raster (HAZUS-style curve).

    Use when a flood DEPTH raster ALREADY exists and you want a per-structure
    damage screen, or a scenario ranking. It reads a depth raster; it does NOT
    simulate the flood. HONEST SCOPE: one aggregate curve, structure value
    only, no contents or uncertainty - not a component-level assessment. Not
    for non-flood hazards. Fetch the structures first (``fetch_usace_nsi`` over
    the depth raster's bounds) and pass their uri.

    Params:
        depth_raster_uri: depth COG; positive is depth above ground, nodata
            or <= 0 is dry.
        assets_uri: structure points with optional ``val_struct``,
            ``found_ht``, ``occtype`` (the NSI columns).
        depth_units: "m" (default) or "ft".

    Returns FlatGeobuf points carrying per-structure depth, damage fraction and
    damage in USD, plus headline totals and honest notes. No assets over the
    footprint is a typed error, never an empty layer.
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
        "NOT a component-level assessment, and none is modeled here, so "
        "defensible per-asset loss work belongs outside this product."
    ]

    with tempfile.TemporaryDirectory(prefix="trid3nt_depth_damage_") as tmpdir:
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

            gdf = _load_assets(assets_uri, tmpdir, notes)

            # src.sample takes coordinates in the raster's own CRS.
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

        # Replacement value: NSI val_struct, or its replacement_value duplicate.
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
        style=_STYLE,
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
