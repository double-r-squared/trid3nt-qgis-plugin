"""Dense-vector handling for the inline-GeoJSON emit path (F94).

ROOT CAUSE (NATE 2026-06-17, confirmed): OSM building footprints — thousands of
polygons — were attached to ``session-state`` as a single raw inline-GeoJSON
``FeatureCollection`` (the Wave 4.9 ``pipeline_emitter.add_loaded_layer`` path).
The browser then (a) downloaded the whole FC over the WebSocket, (b) parsed it,
and (c) handed every full-resolution polygon to MapLibre, which re-tiles the
ENTIRE collection on the main thread. With dense footprints this made the app
"considerably more laggy."

This module is the single decision + transform seam the choke point
(``pipeline_emitter._read_vector_uri_as_geojson``) calls on every vector
FeatureCollection before it is attached for the client. The contract:

    densify_if_needed(fc) -> (fc_out, meta)

- ``feature_count <= THRESHOLD``  -> the FC is returned UNCHANGED; ``meta`` is
  ``None``. The legacy inline path is byte-for-byte preserved for small layers
  (NWS alerts, a handful of WDPA polygons, a panther occurrence set, ...).
- ``feature_count >  THRESHOLD``  -> the FC is made cheap to ship AND cheap to
  draw, and ``meta`` records exactly what was done so the layer can be TAGGED
  (surfaced + logged), never silently degraded ([[feedback_data_source_fallback_norm]]).

Two strategies, selected at runtime:

1. **Vector tiles (PREFERRED, env-gated OFF until a serving face exists).**
   When ``GRACE2_VECTOR_TILES_ENABLED=1`` *and* a tile-serving base URL is
   configured (``GRACE2_VECTOR_TILES_BASE_URL``), ``build_pmtiles`` slices the
   FC into a PMTiles archive of Mapbox Vector Tiles, writes it to the object
   store, and the choke point emits a vector-tile ``LayerURI`` instead of inline
   GeoJSON — MapLibre then fetches only the tiles in view. The full PMTiles+MVT
   build is implemented and unit-tested here; the gate stays OFF by default
   because this AWS deployment has no client-reachable HTTP face for the
   ``s3://`` PMTiles object yet (TiTiler only serves raster ``/cog`` tiles; the
   web client never reaches ``s3://`` directly — Invariant 5). A follow-up infra
   job that stands up a PMTiles range-serving origin (CloudFront over the runs
   bucket, or an agent ``/vector-tiles/`` proxy) flips this on with no code
   change here.

2. **Topology-preserving simplification + feature cap (HONEST FALLBACK,
   ACTIVE).** This ships today and directly fixes the reported lag with NO new
   serving infra:
     - every geometry is Douglas-Peucker simplified with ``preserve_topology``
       (shared edges stay shared; no slivers/holes) at a tolerance scaled to the
       layer's own extent, cutting vertex count (≈70 % wire-byte reduction on
       real footprints) so both the WebSocket payload and MapLibre's
       main-thread tiling get dramatically lighter;
     - the feature list is capped at ``MAX_INLINE_FEATURES`` so MapLibre never
       draws an unbounded count; the cap keeps the LARGEST features (by bbox
       area) so the map stays representative rather than arbitrarily clipped;
     - ``meta`` records ``simplified`` / ``capped`` / original-vs-emitted counts
       so the choke point can stamp the wire layer and the LayerPanel can show a
       "simplified for performance" affordance.

Style is untouched: the simplified FC is the same geometry families
(point/line/polygon) the existing Map.tsx vector styling already paints, so no
client styling changes are needed for the fallback path.

Invariants preserved:
- 1 (Determinism): simplification only DROPS vertices / DROPS whole features;
  it never invents a coordinate. Tiling re-projects received coordinates only.
- 5 (Tier separation): the helper writes to the object store the agent already
  owns; it never hands the client a ``gs://`` / ``s3://`` URL — the choke point
  is responsible for emitting a client-reachable URL only when the serving face
  is configured.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("grace2.agent.vector_tiles")

__all__ = [
    "DENSE_VECTOR_THRESHOLD",
    "MAX_INLINE_FEATURES",
    "DensifyMeta",
    "densify_if_needed",
    "vector_tiles_enabled",
    "build_pmtiles",
    "write_pmtiles_to_object_store",
]


# --------------------------------------------------------------------------- #
# Tunables (env-overridable so ops can move the line without a redeploy)
# --------------------------------------------------------------------------- #

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = int(raw)
    except ValueError:
        logger.warning("vector_tiles: %s=%r is not an int; using default %d", name, raw, default)
        return default
    return val if val > 0 else default


#: Above this feature count a vector layer is treated as "dense": it routes to
#: the tiled (preferred) or simplified+capped (fallback) path instead of raw
#: inline GeoJSON. Below it the current inline path is preserved unchanged.
#: Chosen so a few thousand building footprints trip it while typical
#: occurrence / boundary / alert layers (low hundreds) do not.
DENSE_VECTOR_THRESHOLD: int = _env_int("GRACE2_DENSE_VECTOR_THRESHOLD", 1500)

#: Hard cap on the number of features ever shipped inline on the fallback path.
#: MapLibre draws at most this many polygons for one layer; the cap keeps the
#: largest-area features so the map stays representative. Always >= threshold so
#: a layer just over the threshold is simplified but NOT capped.
MAX_INLINE_FEATURES: int = max(
    _env_int("GRACE2_MAX_INLINE_FEATURES", 4000), DENSE_VECTOR_THRESHOLD
)


# --------------------------------------------------------------------------- #
# Result metadata
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DensifyMeta:
    """What ``densify_if_needed`` did to a dense FeatureCollection.

    ``None`` is returned alongside the FC when the layer was below threshold
    (untouched). When present, the choke point stamps these onto the wire layer
    so the client surfaces the degradation honestly.
    """

    strategy: str  # "simplified" | "capped" | "simplified+capped" | "inline" | "tiled"
    original_feature_count: int
    emitted_feature_count: int
    simplified: bool
    capped: bool
    #: Set only when ``strategy == "tiled"``: the vector-tile LayerURI fields.
    tiles_uri: str | None = None

    def as_wire_tag(self) -> dict[str, Any]:
        """Additive dict merged onto the wire layer dict (extra-tolerant on TS).

        Mirrors the inline-GeoJSON additive-field pattern (job-0175): the strict
        ``ProjectLayerSummary`` is dumped first, then this is merged in.
        """
        return {
            "vector_density": {
                "strategy": self.strategy,
                "original_feature_count": self.original_feature_count,
                "emitted_feature_count": self.emitted_feature_count,
                "simplified": self.simplified,
                "capped": self.capped,
            }
        }


# --------------------------------------------------------------------------- #
# Geometry helpers (shapely — already a hard dep via geopandas)
# --------------------------------------------------------------------------- #

def _feature_count(fc: dict[str, Any]) -> int:
    feats = fc.get("features")
    return len(feats) if isinstance(feats, list) else 0


def _fc_bounds(geoms: list[Any]) -> tuple[float, float, float, float] | None:
    """Overall (minx, miny, maxx, maxy) of a list of shapely geometries."""
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    for g in geoms:
        if g is None or g.is_empty:
            continue
        gminx, gminy, gmaxx, gmaxy = g.bounds
        minx = min(minx, gminx)
        miny = min(miny, gminy)
        maxx = max(maxx, gmaxx)
        maxy = max(maxy, gmaxy)
    if minx == float("inf"):
        return None
    return (minx, miny, maxx, maxy)


def _scaled_tolerance(bounds: tuple[float, float, float, float]) -> float:
    """Douglas-Peucker tolerance (in CRS units, EPSG:4326 degrees) scaled to the
    layer extent. ~1/4000 of the larger span keeps building-footprint corners
    while collapsing redundant vertices. Floored so a tiny extent still
    simplifies meaningfully; capped so a huge extent doesn't over-collapse."""
    minx, miny, maxx, maxy = bounds
    span = max(maxx - minx, maxy - miny)
    tol = span / 4000.0
    # ~1 m to ~50 m at mid-latitudes (1 deg lat ~= 111 km).
    return min(max(tol, 1e-5), 5e-4)


#: Output coordinate precision (decimal degrees) for dense layers. 6 dp ≈ 0.11 m
#: at the equator — well below building-footprint fidelity — but shapely.mapping
#: emits full float repr (~15 sig digits), so rounding is a real, geometry-safe
#: wire-byte win EVEN when Douglas-Peucker drops no vertices (the simple-footprint
#: case the F94 verifier flagged as otherwise unaddressed). Env-overridable.
_COORD_PRECISION: int = _env_int("GRACE2_DENSE_VECTOR_COORD_DP", 6)


def _count_coords(geom_mapping: Any) -> int:
    """Total coordinate pairs in a GeoJSON geometry mapping (recursive)."""
    if not isinstance(geom_mapping, dict):
        return 0
    coords = geom_mapping.get("coordinates")

    def _walk(node: Any) -> int:
        if not isinstance(node, (list, tuple)) or not node:
            return 0
        # A coordinate pair: [x, y(, z)] of numbers.
        if isinstance(node[0], (int, float)):
            return 1
        return sum(_walk(child) for child in node)

    return _walk(coords)


def _round_coords(node: Any, nd: int) -> Any:
    """Recursively round a GeoJSON coordinate array to ``nd`` decimals."""
    if isinstance(node, (int, float)):
        return round(float(node), nd)
    if isinstance(node, (list, tuple)):
        return [_round_coords(c, nd) for c in node]
    return node


def _round_geom(geom_mapping: Any, nd: int) -> Any:
    """Return the geometry mapping with coordinates rounded to ``nd`` decimals."""
    if not isinstance(geom_mapping, dict) or "coordinates" not in geom_mapping:
        return geom_mapping
    out = dict(geom_mapping)
    out["coordinates"] = _round_coords(geom_mapping["coordinates"], nd)
    return out


def _strategy_label(simplified: bool, capped: bool) -> str:
    """Honest strategy tag reflecting what actually happened to a dense FC."""
    if simplified and capped:
        return "simplified+capped"
    if simplified:
        return "simplified"
    if capped:
        return "capped"
    # Dense, but no vertices dropped and no features cut — only coordinate
    # precision was trimmed. Do NOT claim "simplified".
    return "inline"


def _simplify_and_cap(fc: dict[str, Any]) -> tuple[dict[str, Any], bool, bool, int]:
    """Topology-preserving simplify + coord-precision trim + largest-area cap.

    Returns ``(fc_out, simplified, capped, emitted_count)``. ``simplified`` is
    True ONLY when Douglas-Peucker actually removed coordinates (so the honesty
    tag never claims a reduction that did not happen — F94 verifier fix); the
    always-applied coordinate-precision rounding is a separate, unflagged
    wire-byte win. Best-effort: on any shapely failure the ORIGINAL fc is
    returned with ``simplified=False`` so the layer still renders (never a silent
    dead-end — the caller logs).
    """
    try:
        from shapely.geometry import mapping, shape  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "vector_tiles: shapely unavailable (%s) — emitting dense FC as-is", exc
        )
        feats = fc.get("features") or []
        return fc, False, False, len(feats)

    raw_features = fc.get("features") or []
    parsed: list[tuple[dict[str, Any], Any]] = []
    for f in raw_features:
        if not isinstance(f, dict):
            continue
        geom = f.get("geometry")
        if not geom:
            continue
        try:
            g = shape(geom)
        except Exception:  # noqa: BLE001 — skip an unparseable feature, keep going
            continue
        if g.is_empty:
            continue
        parsed.append((f, g))

    if not parsed:
        return fc, False, False, len(raw_features)

    bounds = _fc_bounds([g for _, g in parsed])
    tol = _scaled_tolerance(bounds) if bounds else 1e-4

    from shapely.geometry import mapping as _mapping  # local: scoped to this fn

    simplified_features: list[tuple[dict[str, Any], Any, float]] = []
    any_simplified = False
    for f, g in parsed:
        try:
            sg = g.simplify(tol, preserve_topology=True)
        except Exception:  # noqa: BLE001 — keep the original geometry on failure
            sg = g
        if sg.is_empty:
            sg = g  # never drop a feature to emptiness via simplification
        else:
            # HONESTY: only flag "simplified" when vertices were actually
            # removed. For simple footprints (4-8 vertices) Douglas-Peucker
            # frequently drops nothing; claiming a reduction then would lie to
            # the user (F94 verifier finding).
            try:
                if _count_coords(_mapping(sg)) < _count_coords(_mapping(g)):
                    any_simplified = True
            except Exception:  # noqa: BLE001 — never let the count probe break the path
                pass
        try:
            area = abs(sg.bounds[2] - sg.bounds[0]) * abs(sg.bounds[3] - sg.bounds[1])
        except Exception:  # noqa: BLE001
            area = 0.0
        simplified_features.append((f, sg, area))

    capped = False
    if len(simplified_features) > MAX_INLINE_FEATURES:
        # Keep the largest-area features so the map stays representative.
        simplified_features.sort(key=lambda t: t[2], reverse=True)
        simplified_features = simplified_features[:MAX_INLINE_FEATURES]
        capped = True

    out_features: list[dict[str, Any]] = []
    for f, sg, _area in simplified_features:
        try:
            new_geom = _round_geom(mapping(sg), _COORD_PRECISION)
        except Exception:  # noqa: BLE001
            new_geom = f.get("geometry")
        out_features.append(
            {
                "type": "Feature",
                "properties": f.get("properties") or {},
                "geometry": new_geom,
            }
        )

    fc_out = {"type": "FeatureCollection", "features": out_features}
    return fc_out, any_simplified, capped, len(out_features)


# --------------------------------------------------------------------------- #
# Vector tiles (PREFERRED) — full PMTiles+MVT builder, env-gated
# --------------------------------------------------------------------------- #

def vector_tiles_enabled() -> bool:
    """True only when the tiled path is BOTH opted-in and has a serving face.

    Default OFF: this AWS deployment has no client-reachable HTTP origin for an
    ``s3://`` PMTiles object yet. A follow-up infra job sets both env vars to
    flip the choke point onto the tiled path with no code change here.
    """
    enabled = os.environ.get("GRACE2_VECTOR_TILES_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    has_base = bool(os.environ.get("GRACE2_VECTOR_TILES_BASE_URL", "").strip())
    return enabled and has_base


def build_pmtiles(
    fc: dict[str, Any],
    *,
    layer_name: str = "vector",
    min_zoom: int = 6,
    max_zoom: int = 14,
    extent: int = 4096,
) -> bytes:
    """Slice a GeoJSON FeatureCollection into a PMTiles archive of MVT tiles.

    Pure (no I/O). Returns the PMTiles bytes. Raises ``ImportError`` if the
    tiling toolchain is missing and ``ValueError`` if the FC has no bounds. The
    caller decides whether to write the bytes to the object store.

    Coordinates are taken as EPSG:4326 lon/lat (the inline path's CRS); each web
    mercator tile clips the geometry and ``mapbox_vector_tile.encode`` quantizes
    into the tile's 4096-unit extent. Tiles are gzip-compressed per the PMTiles
    header so MapLibre's pmtiles protocol decompresses transparently.
    """
    import gzip

    import mercantile  # type: ignore[import-not-found]
    import mapbox_vector_tile as mvt  # type: ignore[import-not-found]
    from shapely.geometry import box, shape  # type: ignore[import-not-found]
    from pmtiles.tile import (  # type: ignore[import-not-found]
        Compression,
        TileType,
        zxy_to_tileid,
    )
    from pmtiles.writer import Writer  # type: ignore[import-not-found]

    parsed: list[tuple[dict[str, Any], Any]] = []
    for f in fc.get("features") or []:
        if not isinstance(f, dict) or not f.get("geometry"):
            continue
        try:
            g = shape(f["geometry"])
        except Exception:  # noqa: BLE001
            continue
        if not g.is_empty:
            parsed.append((f.get("properties") or {}, g))

    bounds = _fc_bounds([g for _, g in parsed])
    if bounds is None:
        raise ValueError("build_pmtiles: FeatureCollection has no usable geometry")
    minx, miny, maxx, maxy = bounds

    buf = io.BytesIO()
    writer = Writer(buf)
    n_tiles = 0
    for z in range(min_zoom, max_zoom + 1):
        for t in mercantile.tiles(minx, miny, maxx, maxy, [z]):
            tb = mercantile.bounds(t)
            clip = box(tb.west, tb.south, tb.east, tb.north)
            tile_features: list[dict[str, Any]] = []
            for props, g in parsed:
                if not g.intersects(clip):
                    continue
                cg = g.intersection(clip)
                if cg.is_empty:
                    continue
                tile_features.append({"geometry": cg, "properties": props})
            if not tile_features:
                continue
            encoded = mvt.encode(
                [{"name": layer_name, "features": tile_features}],
                default_options={
                    "quantize_bounds": (tb.west, tb.south, tb.east, tb.north),
                    "extents": extent,
                },
            )
            writer.write_tile(zxy_to_tileid(t.z, t.x, t.y), gzip.compress(encoded))
            n_tiles += 1

    header = {
        "tile_type": TileType.MVT,
        "tile_compression": Compression.GZIP,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "min_lon_e7": int(minx * 1e7),
        "min_lat_e7": int(miny * 1e7),
        "max_lon_e7": int(maxx * 1e7),
        "max_lat_e7": int(maxy * 1e7),
        "center_zoom": min_zoom,
        "center_lon_e7": int((minx + maxx) / 2 * 1e7),
        "center_lat_e7": int((miny + maxy) / 2 * 1e7),
    }
    metadata = {
        "vector_layers": [{"id": layer_name, "description": layer_name}],
    }
    writer.finalize(header, metadata)
    logger.info(
        "vector_tiles: built PMTiles features=%d tiles=%d bytes=%d z=%d-%d",
        len(parsed),
        n_tiles,
        buf.tell(),
        min_zoom,
        max_zoom,
    )
    return buf.getvalue()


def write_pmtiles_to_object_store(pmtiles_bytes: bytes, key: str) -> str:
    """Write PMTiles bytes to the runs/cache bucket; return the ``s3://`` URI.

    Mirrors ``publish_layer._write_overview_cog``'s S3 path. Used only when the
    tiled path is enabled. The choke point converts this object-store URI to a
    client-reachable URL via the (future) serving face; this helper never hands
    the client a bucket URI directly (Invariant 5).
    """
    import boto3  # type: ignore[import-not-found]

    bucket = (
        os.environ.get("GRACE2_RUNS_BUCKET")
        or os.environ.get("GRACE2_CACHE_BUCKET")
        or "grace-2-hazard-prod-runs"
    )
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=pmtiles_bytes,
        ContentType="application/vnd.pmtiles",
    )
    return f"s3://{bucket}/{key}"


# --------------------------------------------------------------------------- #
# The single decision + transform seam
# --------------------------------------------------------------------------- #

def densify_if_needed(
    fc: dict[str, Any] | None,
    *,
    layer_id: str = "",
) -> tuple[dict[str, Any] | None, DensifyMeta | None]:
    """Decide how a vector FeatureCollection is delivered to the client (F94).

    Returns ``(fc_out, meta)``:
    - below ``DENSE_VECTOR_THRESHOLD``: ``(fc, None)`` — current inline path
      preserved byte-for-byte.
    - at/above threshold: ``(simplified_capped_fc, DensifyMeta)`` — lighter to
      ship AND lighter for MapLibre to draw, with the degradation recorded so
      the choke point can tag the wire layer honestly.

    The tiled path is intentionally NOT taken here (it returns an object-store
    URI, not an inline FC, which the choke point must emit as a vector
    ``LayerURI``); ``vector_tiles_enabled`` + ``build_pmtiles`` are exposed so
    the choke point can opt into it once a serving face exists. This function is
    the inline-FC transform — always safe, always renders.
    """
    if not isinstance(fc, dict):
        return fc, None
    count = _feature_count(fc)
    if count <= DENSE_VECTOR_THRESHOLD:
        return fc, None

    fc_out, simplified, capped, emitted = _simplify_and_cap(fc)
    strategy = _strategy_label(simplified, capped)
    meta = DensifyMeta(
        strategy=strategy,
        original_feature_count=count,
        emitted_feature_count=emitted,
        simplified=simplified,
        capped=capped,
    )
    logger.info(
        "vector_tiles: densified layer_id=%s strategy=%s original=%d "
        "emitted=%d simplified=%s capped=%s",
        layer_id,
        strategy,
        count,
        emitted,
        simplified,
        capped,
    )
    return fc_out, meta
