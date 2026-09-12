"""A dense vector layer routes to the simplified path, not raw inline.

A sub-threshold collection is returned UNCHANGED, preserving the inline path,
while an over-threshold one is topology-preserving-simplified and capped and
TAGGED so the degradation is surfaced honestly rather than dropped silently. The
emitter's choke point emits the simplified geometry with its density tag."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from trid3nt_contracts import new_ulid
from trid3nt_contracts.execution import LayerURI

from trid3nt_server.render.pipeline_emitter import PipelineEmitter
from trid3nt_server.tools import vector_tiles
from trid3nt_server.tools.vector_tiles import (
    DENSE_VECTOR_THRESHOLD,
    DensifyMeta,
    densify_if_needed,
)




def _polygon_fc(n: int, *, verts: int = 24) -> dict[str, Any]:
    """A dense FeatureCollection of small blobby polygons (footprint-like)."""
    feats: list[dict[str, Any]] = []
    for i in range(n):
        cx = -122.0 + (i % 80) * 0.001
        cy = 37.0 + (i // 80) * 0.001
        ring: list[list[float]] = []
        for k in range(verts):
            a = 2 * math.pi * k / verts
            r = 0.0003 + 0.00008 * math.sin(4 * a)
            ring.append([cx + r * math.cos(a), cy + r * math.sin(a)])
        ring.append(ring[0])
        feats.append(
            {
                "type": "Feature",
                "properties": {"id": i},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
    return {"type": "FeatureCollection", "features": feats}


def _point_fc(n: int) -> dict[str, Any]:
    feats = [
        {
            "type": "Feature",
            "properties": {"id": i},
            "geometry": {"type": "Point", "coordinates": [-122.0 + i * 0.0001, 37.0]},
        }
        for i in range(n)
    ]
    return {"type": "FeatureCollection", "features": feats}


class _CapturingSink:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def __call__(self, text: str) -> None:
        self.frames.append(json.loads(text))


def _session_frames(sink: _CapturingSink) -> list[dict[str, Any]]:
    return [f for f in sink.frames if f["type"] == "session-state"]


def _make_vector_layer(uri: str, layer_id: str) -> LayerURI:
    return LayerURI(
        layer_id=layer_id,
        name="OSM buildings",
        layer_type="vector",
        uri=uri,
    )




def test_below_threshold_returns_unchanged() -> None:
    fc = _point_fc(DENSE_VECTOR_THRESHOLD)  # exactly at threshold = NOT dense
    out, meta = densify_if_needed(fc, layer_id="small")
    assert out is fc
    assert meta is None


def test_above_threshold_simplifies_and_tags() -> None:
    n = DENSE_VECTOR_THRESHOLD + 1200
    fc = _polygon_fc(n)
    raw_bytes = len(json.dumps(fc))

    out, meta = densify_if_needed(fc, layer_id="osm-buildings")

    assert meta is not None
    assert isinstance(meta, DensifyMeta)
    assert meta.strategy == "simplified"
    assert meta.original_feature_count == n
    assert meta.simplified is True
    # Topology-preserving simplification must shrink the wire payload.
    assert len(json.dumps(out)) < raw_bytes
    # The emitted FC is still a valid polygon FeatureCollection (same family).
    assert out["type"] == "FeatureCollection"
    assert out["features"]
    assert out["features"][0]["geometry"]["type"] in {"Polygon", "MultiPolygon"}
    # Honest tag exposed for the wire.
    tag = meta.as_wire_tag()
    assert tag["vector_density"]["strategy"] == "simplified"
    assert tag["vector_density"]["original_feature_count"] == n
    assert tag["vector_density"]["simplified"] is True


def test_above_cap_is_capped_to_max_and_tagged(monkeypatch: pytest.MonkeyPatch) -> None:
    # Drive the cap with small explicit limits so the test stays fast.
    monkeypatch.setattr(vector_tiles, "DENSE_VECTOR_THRESHOLD", 100)
    monkeypatch.setattr(vector_tiles, "MAX_INLINE_FEATURES", 150)

    fc = _polygon_fc(400)
    out, meta = densify_if_needed(fc, layer_id="huge")

    assert meta is not None
    assert meta.capped is True
    assert meta.original_feature_count == 400
    assert meta.emitted_feature_count == 150
    assert len(out["features"]) == 150
    assert meta.as_wire_tag()["vector_density"]["capped"] is True


def _simple_rect_fc(n: int, *, dp: int = 12) -> dict[str, Any]:
    """A dense collection of SIMPLE five-vertex rectangles with high-precision coords.

    Douglas-Peucker drops no vertex from a rectangle, so the only honest wire win is
    coordinate-precision rounding."""
    feats: list[dict[str, Any]] = []
    for i in range(n):
        cx = -122.0 + (i % 80) * 0.001
        cy = 37.0 + (i // 80) * 0.001
        # 12-dp coords so 6-dp rounding has something to trim.
        d = 0.0003
        ring = [
            [round(cx - d, dp), round(cy - d, dp)],
            [round(cx + d, dp), round(cy - d, dp)],
            [round(cx + d, dp), round(cy + d, dp)],
            [round(cx - d, dp), round(cy + d, dp)],
            [round(cx - d, dp), round(cy - d, dp)],
        ]
        feats.append(
            {
                "type": "Feature",
                "properties": {"id": i},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
    return {"type": "FeatureCollection", "features": feats}


def test_simple_rectangles_honest_tag_and_coord_trim() -> None:
    """F94 honesty: simple footprints that DP can't simplify must NOT be tagged
    'simplified', but coordinate-precision rounding still shrinks the wire."""
    n = DENSE_VECTOR_THRESHOLD + 800  # dense, but below MAX_INLINE_FEATURES
    fc = _simple_rect_fc(n)

    out, meta = densify_if_needed(fc, layer_id="osm-rects")

    assert meta is not None
    # Douglas-Peucker removed NO vertices from rectangles -> honest flag is False.
    assert meta.simplified is False
    assert meta.capped is False
    assert meta.strategy == "inline"  # not "simplified" — would be a lie
    # Coordinate-precision rounding still ran (the geometry-safe wire win that
    # matters for simple footprints, where shapely.mapping would otherwise emit
    # full ~15-digit floats): every emitted coordinate is <= 6 decimal places.
    ring = out["features"][0]["geometry"]["coordinates"][0]
    assert len(ring) == 5  # rectangle ring preserved (no vertices dropped)
    for x, y in ring:
        assert len(str(x).split(".")[-1]) <= 6
        assert len(str(y).split(".")[-1]) <= 6


def test_non_dict_input_is_passthrough() -> None:
    out, meta = densify_if_needed(None)
    assert out is None
    assert meta is None




@pytest.mark.asyncio
async def test_dense_vector_emits_simplified_inline_plus_density_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dense vector LayerURI added to the emitter must reach the wire as a
    SIMPLIFIED inline FeatureCollection carrying the ``vector_density`` tag."""
    n = DENSE_VECTOR_THRESHOLD + 1000
    dense = _polygon_fc(n)
    raw_bytes = len(json.dumps(dense))

    # Stub the object-store read so the choke point gets our dense FC. The
    # densify transform runs AFTER this stub inside _read_vector_uri_as_geojson.
    import trid3nt_server.render.pipeline_emitter as pe

    async def _fake_read(uri: str) -> dict[str, Any]:
        # Mirror the real function: read raw, then densify at the choke point.
        from trid3nt_server.tools.vector_tiles import densify_if_needed as _dn

        obj, meta = _dn(dense, layer_id=uri)
        if meta is not None:
            pe._LAST_DENSITY_META_BY_URI[uri] = meta
        return obj

    monkeypatch.setattr(pe, "_read_vector_uri_as_geojson", _fake_read)

    sink = _CapturingSink()
    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)
    uri = "s3://b/osm_buildings.geojson"
    await emitter.add_loaded_layer(_make_vector_layer(uri, "osm-1"))

    frames = _session_frames(sink)
    assert frames, "expected a session-state emission"
    layers = frames[-1]["payload"]["loaded_layers"]
    assert len(layers) == 1
    layer = layers[0]

    # Inline FC present but SIMPLIFIED (lighter than the raw input).
    assert "inline_geojson" in layer
    assert layer["inline_geojson"]["type"] == "FeatureCollection"
    assert len(json.dumps(layer["inline_geojson"])) < raw_bytes

    # Honest density tag stamped on the wire layer.
    assert "vector_density" in layer
    assert layer["vector_density"]["strategy"] == "simplified"
    assert layer["vector_density"]["original_feature_count"] == n
    assert layer["vector_density"]["simplified"] is True


@pytest.mark.asyncio
async def test_small_vector_stays_inline_with_no_density_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sub-threshold vector keeps the current inline path: full FC, no tag."""
    small = _point_fc(50)

    import trid3nt_server.render.pipeline_emitter as pe

    async def _fake_read(uri: str) -> dict[str, Any]:
        from trid3nt_server.tools.vector_tiles import densify_if_needed as _dn

        obj, meta = _dn(small, layer_id=uri)
        if meta is not None:
            pe._LAST_DENSITY_META_BY_URI[uri] = meta
        return obj

    monkeypatch.setattr(pe, "_read_vector_uri_as_geojson", _fake_read)

    sink = _CapturingSink()
    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)
    uri = "s3://b/gbif_points.geojson"
    await emitter.add_loaded_layer(_make_vector_layer(uri, "gbif-1"))

    layer = _session_frames(sink)[-1]["payload"]["loaded_layers"][0]
    assert "inline_geojson" in layer
    assert len(layer["inline_geojson"]["features"]) == 50  # unchanged
    assert "vector_density" not in layer
