"""Unit tests for ``compute_layer_bounds`` (NATE 2026-06-17).

The live bug this tool fixes: the agent reached for the Python sandbox to
compute ``gdf.total_bounds`` for "resize the box to encompass all the
<features>" — slow, gated, orphaned, and never applied to the map. This tool
computes the EPSG:4326 extent deterministically AND emits a ``zoom-to``
map-command so the viewport actually fits all features.

Tests:

1. ``test_returns_correct_bbox_for_known_vector`` — a known GeoJSON
   FeatureCollection yields the expected min/max lon/lat.
2. ``test_emits_zoom_to_map_command_with_those_bounds`` — driven inside an
   ``emit_tool_call`` bracket, the tool fires ``map-command(zoom-to)`` carrying
   exactly the computed bbox.
3. ``test_reprojects_non_4326_vector_to_wgs84`` — a Web-Mercator vector is
   reprojected so the returned bbox is in lon/lat degrees, not meters.
4. ``test_returns_correct_bbox_for_known_raster`` — a small synthetic GeoTIFF
   yields its corner extent.
5. ``test_pad_fraction_expands_bbox`` — padding widens the box symmetrically.
6. ``test_unknown_uri_raises_typed_error`` — FR-AS-11 typed error on a bad URI.
7. ``test_no_emitter_does_not_crash`` — direct call (no emitter) skips the emit.
8. ``test_registered_and_in_hot_set`` — registry + category + hot-set wiring.
9. ``test_adapter_steer_present`` — the SYSTEM_PROMPT carries the fit/zoom steer.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from grace2_agent.pipeline_emitter import PipelineEmitter
from grace2_agent.tools.compute_layer_bounds import (
    ComputeLayerBoundsError,
    compute_layer_bounds,
)
from grace2_contracts import new_ulid


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _write_geojson(features: list[dict], crs_name: str | None = None) -> str:
    fc: dict = {"type": "FeatureCollection", "features": features}
    if crs_name is not None:
        fc["crs"] = {"type": "name", "properties": {"name": crs_name}}
    fd, path = tempfile.mkstemp(suffix=".geojson", prefix="grace2_test_bounds_")
    with os.fdopen(fd, "w") as f:
        json.dump(fc, f)
    return path


def _point_feature(lon: float, lat: float) -> dict:
    return {
        "type": "Feature",
        "properties": {},
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
    }


def _make_emitter(captured: list[tuple[str, dict]]) -> PipelineEmitter:
    async def _sink(raw: str) -> None:
        env = json.loads(raw)
        captured.append((env["type"], env["payload"]))

    return PipelineEmitter(session_id=new_ulid(), sink=_sink)


# --------------------------------------------------------------------------- #
# Test 1 — correct bbox for a known vector
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_returns_correct_bbox_for_known_vector() -> None:
    """Three points across Boulder, CO → extent spans their min/max lon/lat."""
    pts = [
        _point_feature(-105.30, 39.95),  # SW-ish
        _point_feature(-105.20, 40.05),  # NE-ish
        _point_feature(-105.27, 40.01),  # interior
    ]
    path = _write_geojson(pts)
    try:
        result = await compute_layer_bounds(path)
    finally:
        os.unlink(path)

    assert result["layer_type"] == "vector"
    assert result["crs"] == "EPSG:4326"
    assert result["min_lon"] == pytest.approx(-105.30)
    assert result["min_lat"] == pytest.approx(39.95)
    assert result["max_lon"] == pytest.approx(-105.20)
    assert result["max_lat"] == pytest.approx(40.05)
    assert result["bbox"] == [
        result["min_lon"],
        result["min_lat"],
        result["max_lon"],
        result["max_lat"],
    ]


# --------------------------------------------------------------------------- #
# Test 2 — emits zoom-to map-command with the computed bounds
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_emits_zoom_to_map_command_with_those_bounds() -> None:
    """Inside emit_tool_call, the tool fires ``map-command(zoom-to)`` carrying
    EXACTLY the computed bbox — the seam that actually fits the view."""
    pts = [
        _point_feature(-81.92, 26.55),
        _point_feature(-81.80, 26.68),
    ]
    path = _write_geojson(pts)
    captured: list[tuple[str, dict]] = []
    emitter = _make_emitter(captured)

    try:

        async def _invoke():
            return await compute_layer_bounds(path)

        result = await emitter.emit_tool_call(
            name="compute_layer_bounds",
            tool_name="compute_layer_bounds",
            invoke=_invoke,
        )
    finally:
        os.unlink(path)

    assert result["map_fitted"] is True

    map_cmds = [(t, p) for t, p in captured if t == "map-command"]
    assert len(map_cmds) == 1, f"expected exactly one map-command; got {map_cmds!r}"
    _, payload = map_cmds[0]
    assert payload["command"] == "zoom-to"
    emitted_bbox = payload["args"]["bbox"]
    assert emitted_bbox == result["bbox"]
    assert emitted_bbox[0] == pytest.approx(-81.92)
    assert emitted_bbox[1] == pytest.approx(26.55)
    assert emitted_bbox[2] == pytest.approx(-81.80)
    assert emitted_bbox[3] == pytest.approx(26.68)


# --------------------------------------------------------------------------- #
# Test 3 — reprojects a non-4326 vector to WGS84
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reprojects_non_4326_vector_to_wgs84() -> None:
    """A Web-Mercator (EPSG:3857) vector is reprojected — the returned bbox is
    in lon/lat degrees, not meters."""
    import geopandas as gpd
    from shapely.geometry import Point

    # Two points near Boulder in EPSG:3857 meters.
    gdf = gpd.GeoDataFrame(
        {"id": [1, 2]},
        geometry=[Point(-11722000.0, 4862000.0), Point(-11710000.0, 4875000.0)],
        crs="EPSG:3857",
    )
    fd, path = tempfile.mkstemp(suffix=".fgb", prefix="grace2_test_bounds_3857_")
    os.close(fd)
    gdf.to_file(path, driver="FlatGeobuf")
    try:
        result = await compute_layer_bounds(path, fit_map=False)
    finally:
        os.unlink(path)

    # Degrees, not meters: lon ~ -105, lat ~ 40.
    assert -106.0 < result["min_lon"] < -104.0
    assert -106.0 < result["max_lon"] < -104.0
    assert 39.0 < result["min_lat"] < 41.0
    assert 39.0 < result["max_lat"] < 41.0
    assert result["min_lon"] < result["max_lon"]
    assert result["min_lat"] < result["max_lat"]


# --------------------------------------------------------------------------- #
# Test 4 — correct bbox for a known raster
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_returns_correct_bbox_for_known_raster() -> None:
    """A small synthetic EPSG:4326 GeoTIFF yields its corner extent."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    minx, miny, maxx, maxy = -105.3, 39.9, -105.1, 40.1
    width, height = 8, 8
    transform = from_bounds(minx, miny, maxx, maxy, width, height)
    fd, path = tempfile.mkstemp(suffix=".tif", prefix="grace2_test_bounds_ras_")
    os.close(fd)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as ds:
        ds.write(np.ones((height, width), dtype="float32"), 1)

    try:
        result = await compute_layer_bounds(path, fit_map=False)
    finally:
        os.unlink(path)

    assert result["layer_type"] == "raster"
    assert result["min_lon"] == pytest.approx(minx)
    assert result["min_lat"] == pytest.approx(miny)
    assert result["max_lon"] == pytest.approx(maxx)
    assert result["max_lat"] == pytest.approx(maxy)


# --------------------------------------------------------------------------- #
# Test 5 — pad_fraction expands the bbox symmetrically
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pad_fraction_expands_bbox() -> None:
    pts = [_point_feature(-100.0, 40.0), _point_feature(-90.0, 45.0)]
    path = _write_geojson(pts)
    try:
        result = await compute_layer_bounds(path, pad_fraction=0.10, fit_map=False)
    finally:
        os.unlink(path)

    # width=10, height=5 → pad 1.0 lon, 0.5 lat on each side.
    assert result["min_lon"] == pytest.approx(-101.0)
    assert result["max_lon"] == pytest.approx(-89.0)
    assert result["min_lat"] == pytest.approx(39.5)
    assert result["max_lat"] == pytest.approx(45.5)
    assert result["pad_fraction"] == pytest.approx(0.10)


# --------------------------------------------------------------------------- #
# Test 6 — typed error on an unknown URI (FR-AS-11)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unknown_uri_raises_typed_error() -> None:
    with pytest.raises(ComputeLayerBoundsError) as ei:
        await compute_layer_bounds("/nonexistent/path/to/nothing.fgb")
    assert ei.value.error_code == "UNKNOWN_LAYER_URI"


# --------------------------------------------------------------------------- #
# Test 6b — D1 Fix B: a TiTiler tile-template / display URL is tolerated.
# The defense-in-depth fallback in _resolve_layer_to_local_path recovers the
# COG from the template's url= param so an LLM that passed the display URL
# (the live SWMM UNKNOWN_LAYER_URI incident) still gets a deterministic extent.
# --------------------------------------------------------------------------- #


def test_resolve_titiler_template_recovers_s3_cog(monkeypatch) -> None:
    """_resolve_layer_to_local_path extracts the url= COG from a TiTiler
    template and routes it through the s3 branch."""
    import grace2_agent.tools.cache as cache_mod
    import grace2_agent.tools.compute_layer_bounds as clb

    real_cog = "s3://trid3nt-runs/01ABC/swmm_depth_frame_01.tif"
    captured: dict[str, str] = {}

    def _fake_read(uri: str) -> bytes:  # stands in for read_object_bytes_s3
        captured["uri"] = uri
        return b"\x00\x01\x02"  # bytes unused — we only assert the routing

    # The tool does `from .cache import read_object_bytes_s3` inside the s3
    # branch, so patch the name on the cache module it imports from.
    monkeypatch.setattr(cache_mod, "read_object_bytes_s3", _fake_read)

    template = (
        "https://d123abc.cloudfront.net/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
        "?url=s3%3A%2F%2Ftrid3nt-runs%2F01ABC%2Fswmm_depth_frame_01.tif"
        "&rescale=0%2C2&colormap_name=blues"
    )
    path, is_temp = clb._resolve_layer_to_local_path(template)
    try:
        assert captured["uri"] == real_cog  # the COG was recovered + downloaded
        assert is_temp is True
        assert path.endswith(".tif")
    finally:
        if is_temp and os.path.isfile(path):
            os.unlink(path)


def test_resolve_titiler_template_without_url_param_raises_typed_error() -> None:
    """A display URL with no recoverable s3 url= param still raises the typed
    UNKNOWN_LAYER_URI (honest, retryable) — it must not fail open silently."""
    import grace2_agent.tools.compute_layer_bounds as clb

    bad = "https://d123abc.cloudfront.net/cog/tiles/WebMercatorQuad/3/2/1.png"
    with pytest.raises(ComputeLayerBoundsError) as ei:
        clb._resolve_layer_to_local_path(bad)
    assert ei.value.error_code == "UNKNOWN_LAYER_URI"


# --------------------------------------------------------------------------- #
# Test 7 — no emitter bound → no emit, no crash (UX action, not a gate)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_emitter_does_not_crash() -> None:
    from grace2_agent.pipeline_emitter import current_emitter

    assert current_emitter() is None  # precondition
    pts = [_point_feature(-1.0, 1.0), _point_feature(1.0, 2.0)]
    path = _write_geojson(pts)
    try:
        result = await compute_layer_bounds(path)  # fit_map defaults True
    finally:
        os.unlink(path)
    # No emitter bound → emission skipped, but the bbox is still returned.
    assert result["map_fitted"] is False
    assert result["bbox"] == [-1.0, 1.0, 1.0, 2.0]


# --------------------------------------------------------------------------- #
# Test 8 — registry + category + hot-set wiring
# --------------------------------------------------------------------------- #


def test_registered_and_in_hot_set() -> None:
    import grace2_agent.categories as categories
    import grace2_agent.tools as tools

    rt = tools.TOOL_REGISTRY.get("compute_layer_bounds")
    assert rt is not None, "compute_layer_bounds not in TOOL_REGISTRY"
    assert rt.metadata.cacheable is False
    assert rt.metadata.ttl_class == "live-no-cache"
    assert categories.PRIMARY_CATEGORY["compute_layer_bounds"] == "geographic_primitives"
    assert "compute_layer_bounds" in categories.HOT_SET_TOOLS
    # And it surfaces in the geographic_primitives member list.
    assert "compute_layer_bounds" in categories.tools_for_category("geographic_primitives")


# --------------------------------------------------------------------------- #
# Test 9 — adapter SYSTEM_PROMPT carries the fit/zoom steer
# --------------------------------------------------------------------------- #


def test_adapter_steer_present() -> None:
    from grace2_agent.adapter import SYSTEM_PROMPT

    prompt = SYSTEM_PROMPT.lower()
    assert "compute_layer_bounds" in SYSTEM_PROMPT
    # Steers the agent away from the sandbox for bbox math.
    assert "do not use the python sandbox" in prompt
    # Asserts the agent CAN drive the map view.
    assert "never claim you cannot" in prompt
    # Names the fit/zoom/resize trigger.
    assert "encompass all" in prompt
