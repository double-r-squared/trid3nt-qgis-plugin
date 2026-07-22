"""Tests for ``compute_cross_section`` (the cross-section / profile tool).

All tests use synthetic in-memory/temp-file rasters -- no network, no LLM calls.

Coverage:
- Synthetic ramp DEM -> a known, monotonic linear profile sampled along a line.
- The result is a structurally-valid ChartEmissionPayload (the contract's own
  validator runs on construction, so a broken spec would raise) and rides the
  chart-emission chat-card path (``is_chart_emission_result`` True).
- Line input parsing: GeoJSON LineString, a Feature, a FeatureCollection (the
  drawn ``barriers`` FC round-trip), and a bare ``[lon,lat]`` list (agent-
  derived inline) all resolve; degenerate / malformed lines -> LINE_INVALID.
- Multi-layer overlay (DESIGN CALL B = YES): two synthetic rasters on one line ->
  a two-line chart (``color`` encoding); matching units -> single shared y-axis,
  differing units -> dual independent y scales.
- Honesty floor: nodata stations surface as null (not dropped); a line entirely
  off every raster -> typed LINE_OUTSIDE_RASTER (never a fabricated profile).
- CRS mismatch: a UTM raster is sampled correctly from an EPSG:4326 line.
- Geodesic distance: the x-axis is metres on the ground, not degrees.
- Registration in TOOL_REGISTRY + category membership.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from grace2_agent.tools.chart_tools import is_chart_emission_result
from grace2_agent.tools.compute_cross_section import (
    CrossSectionError,
    _resolve_line_coords,
    compute_cross_section,
)
from grace2_contracts.chart_contracts import (
    ChartEmissionPayload,
    is_structurally_valid_vega_lite_spec,
)


# ---------------------------------------------------------------------------
# Synthetic-fixture helpers
# ---------------------------------------------------------------------------


def _make_raster(
    tmp_path: Path,
    values: np.ndarray,
    *,
    bounds: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    crs: str = "EPSG:4326",
    nodata: float | None = None,
    units: str | None = None,
    name: str = "ramp.tif",
) -> str:
    """Write a single-band GeoTIFF and return its local path.

    ``values`` is a (h, w) array. ``bounds`` is (minx, miny, maxx, maxy) in the
    given ``crs``. Optional ``nodata`` + band-1 ``units`` tag.
    """
    import rasterio
    from rasterio.transform import from_bounds

    height, width = values.shape
    minx, miny, maxx, maxy = bounds
    transform = from_bounds(minx, miny, maxx, maxy, width, height)
    profile = {
        "driver": "GTiff",
        "dtype": values.dtype,
        "width": width,
        "height": height,
        "count": 1,
        "crs": crs,
        "transform": transform,
    }
    if nodata is not None:
        profile["nodata"] = nodata
    path = str(tmp_path / name)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values, 1)
        if units is not None:
            dst.update_tags(1, UNITS=units)
            try:
                dst.units = (units,)
            except Exception:  # noqa: BLE001 -- some drivers reject; tag suffices
                pass
    return path


def _x_ramp(height: int = 64, width: int = 64, *, scale: float = 100.0) -> np.ndarray:
    """A west->east ramp: value == scale * (column-fraction). Row-invariant.

    With bounds (0,0,1,1) the value at longitude ``lon`` is ~ scale*lon, so a
    horizontal line at fixed latitude has a KNOWN linear profile in longitude.
    """
    col = np.linspace(0.0, scale, width, dtype=np.float32)
    return np.tile(col, (height, 1))


def _assert_valid_chart_payload(payload: dict) -> dict:
    """Re-validate a returned chart payload against the contract; return the spec."""
    assert isinstance(payload, dict)
    assert payload["envelope_type"] == "chart-emission"
    assert is_chart_emission_result(payload)
    assert isinstance(payload["chart_id"], str) and payload["chart_id"]
    spec = payload["vega_lite_spec"]
    assert is_structurally_valid_vega_lite_spec(spec), spec
    # Round-trips through the pydantic contract (raises if structurally broken).
    model = ChartEmissionPayload.model_validate(payload)
    assert model.envelope_type == "chart-emission"
    return spec


def _all_rows(spec: dict) -> list[dict]:
    """Collect the inline data rows from a spec (top-level or layered)."""
    rows: list[dict] = []
    data = spec.get("data")
    if isinstance(data, dict) and isinstance(data.get("values"), list):
        rows.extend(data["values"])
    for layer in spec.get("layer", []) or []:
        ldata = layer.get("data")
        if isinstance(ldata, dict) and isinstance(ldata.get("values"), list):
            rows.extend(ldata["values"])
    return rows


# ---------------------------------------------------------------------------
# Line resolution
# ---------------------------------------------------------------------------


class TestResolveLine:
    def test_geojson_linestring(self):
        geom = {"type": "LineString", "coordinates": [[0.0, 0.5], [1.0, 0.5]]}
        coords = _resolve_line_coords(geom)
        assert coords == [[0.0, 0.5], [1.0, 0.5]]

    def test_feature_wrapping_linestring(self):
        feat = {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[0.0, 0.5], [1.0, 0.5]]},
            "properties": {"role": "barrier"},
        }
        assert _resolve_line_coords(feat) == [[0.0, 0.5], [1.0, 0.5]]

    def test_feature_collection_first_linestring(self):
        # Mirrors the drawn ``barriers`` FeatureCollection round-trip.
        fc = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [0.1, 0.1]},
                    "properties": {"role": "point"},
                },
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[0.0, 0.5], [0.5, 0.5], [1.0, 0.5]],
                    },
                    "properties": {"role": "barrier", "barrier_type": "wall"},
                },
            ],
        }
        assert _resolve_line_coords(fc) == [[0.0, 0.5], [0.5, 0.5], [1.0, 0.5]]

    def test_bare_vertex_list(self):
        assert _resolve_line_coords([[0.0, 0.5], [1.0, 0.5]]) == [[0.0, 0.5], [1.0, 0.5]]

    def test_consecutive_duplicates_dropped(self):
        coords = _resolve_line_coords([[0.0, 0.5], [0.0, 0.5], [1.0, 0.5]])
        assert coords == [[0.0, 0.5], [1.0, 0.5]]

    def test_single_vertex_rejected(self):
        with pytest.raises(CrossSectionError) as ei:
            _resolve_line_coords([[0.0, 0.5]])
        assert ei.value.error_code == "LINE_INVALID"

    def test_non_linestring_geometry_rejected(self):
        with pytest.raises(CrossSectionError) as ei:
            _resolve_line_coords({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1]]]})
        assert ei.value.error_code == "LINE_INVALID"

    def test_non_numeric_vertex_rejected(self):
        with pytest.raises(CrossSectionError) as ei:
            _resolve_line_coords([[0.0, 0.5], ["x", 0.5]])
        assert ei.value.error_code == "LINE_INVALID"

    def test_empty_feature_collection_rejected(self):
        with pytest.raises(CrossSectionError) as ei:
            _resolve_line_coords({"type": "FeatureCollection", "features": []})
        assert ei.value.error_code == "LINE_INVALID"


# ---------------------------------------------------------------------------
# Single-layer happy path: a known linear ramp profile
# ---------------------------------------------------------------------------


class TestSingleLayerProfile:
    def test_ramp_profile_is_monotonic_known(self, tmp_path):
        path = _make_raster(tmp_path, _x_ramp(scale=100.0), units="m", name="dem.tif")
        # A horizontal line across the ramp at mid-latitude. Endpoints sit just
        # INSIDE the raster extent (0..1): a coordinate landing on the far edge
        # (lon == maxx) samples out-of-bounds in rasterio, a raster-edge fact, so
        # we inset slightly to read real ramp cells end to end.
        line = {"type": "LineString", "coordinates": [[0.01, 0.5], [0.99, 0.5]]}

        payload = compute_cross_section(layer_uri=path, line=line, n_stations=11)
        spec = _assert_valid_chart_payload(payload)

        assert spec["mark"]["type"] == "line"
        rows = _all_rows(spec)
        assert len(rows) == 11
        # All one layer; the layer field carries the basename.
        assert {r["layer"] for r in rows} == {"dem"}

        # Profile values rise monotonically west->east (the ramp), ~1 -> ~99.
        vals = [r["value"] for r in rows]
        assert all(v is not None for v in vals)
        assert vals[0] < vals[-1]
        assert vals == sorted(vals)
        assert vals[0] == pytest.approx(1.0, abs=3.0)
        assert vals[-1] == pytest.approx(99.0, abs=3.0)

        # x-axis (distance) is metres-on-the-ground, monotonically increasing
        # from 0; a ~1-degree line at 0.5 lat is on the order of 1e5 m.
        dists = [r["distance_m"] for r in rows]
        assert dists[0] == 0.0
        assert dists == sorted(dists)
        assert dists[-1] > 1.0e5  # ~111 km for a degree of longitude near equator

        # Units flow into the y-title; caption carries the computed range.
        assert spec["encoding"]["y"]["title"] == "m"
        assert "m line" in payload["caption"]
        assert payload["source_layer_uri"] == path

    def test_inline_vertex_list_path(self, tmp_path):
        # The agent-derived inline line path (no GeoJSON wrapper).
        path = _make_raster(tmp_path, _x_ramp(), name="dem2.tif")
        payload = compute_cross_section(
            layer_uri=path, line=[[0.0, 0.5], [1.0, 0.5]], n_stations=5
        )
        _assert_valid_chart_payload(payload)

    def test_n_stations_clamped(self, tmp_path):
        path = _make_raster(tmp_path, _x_ramp(), name="dem3.tif")
        # n_stations below the floor clamps to 2 (still a valid 2-point profile).
        payload = compute_cross_section(
            layer_uri=path, line=[[0.0, 0.5], [1.0, 0.5]], n_stations=1
        )
        spec = _assert_valid_chart_payload(payload)
        assert len(_all_rows(spec)) == 2


# ---------------------------------------------------------------------------
# Multi-layer overlay (DESIGN CALL B = YES)
# ---------------------------------------------------------------------------


class TestMultiLayerOverlay:
    def test_two_layers_matching_units_single_y(self, tmp_path):
        ground = _make_raster(tmp_path, _x_ramp(scale=100.0), units="m", name="ground.tif")
        # A flat "water surface" at 50 m everywhere (same units).
        water = _make_raster(
            tmp_path, np.full((64, 64), 50.0, dtype=np.float32), units="m", name="water.tif"
        )
        line = [[0.0, 0.5], [1.0, 0.5]]

        payload = compute_cross_section(
            layer_uri=ground, line=line, n_stations=11, extra_layer_uris=[water]
        )
        spec = _assert_valid_chart_payload(payload)

        # Matching units -> ONE shared y-axis, a color encoding on `layer`.
        assert "layer" not in spec  # not the dual-axis layered form
        assert spec["encoding"]["color"]["field"] == "layer"
        rows = _all_rows(spec)
        labels = {r["layer"] for r in rows}
        assert labels == {"ground", "water"}
        assert len(rows) == 22  # 11 stations x 2 layers

        # The two layers share identical station distances (same line).
        ground_d = sorted(r["distance_m"] for r in rows if r["layer"] == "ground")
        water_d = sorted(r["distance_m"] for r in rows if r["layer"] == "water")
        assert ground_d == water_d

        # Caption names both layers + their ranges.
        assert "ground:" in payload["caption"] and "water:" in payload["caption"]

    def test_two_layers_differing_units_dual_axis(self, tmp_path):
        elev = _make_raster(tmp_path, _x_ramp(scale=100.0), units="m", name="elev.tif")
        # A depth layer in a different unit (feet).
        depth = _make_raster(
            tmp_path, np.full((64, 64), 3.0, dtype=np.float32), units="ft", name="depth.tif"
        )
        payload = compute_cross_section(
            layer_uri=elev, line=[[0.0, 0.5], [1.0, 0.5]], n_stations=8,
            extra_layer_uris=[depth],
        )
        spec = _assert_valid_chart_payload(payload)

        # Differing units -> dual-axis layered spec with independent y scales.
        assert isinstance(spec.get("layer"), list) and len(spec["layer"]) == 2
        assert spec["resolve"]["scale"]["y"] == "independent"
        # Each sub-layer titles its own y-axis with its own unit.
        y_titles = [lyr["encoding"]["y"]["title"] for lyr in spec["layer"]]
        assert any("m" in t for t in y_titles)
        assert any("ft" in t for t in y_titles)

    def test_too_many_layers_rejected(self, tmp_path):
        base = _make_raster(tmp_path, _x_ramp(), name="b.tif")
        extras = [
            _make_raster(tmp_path, _x_ramp(), name=f"e{i}.tif") for i in range(4)
        ]
        with pytest.raises(CrossSectionError) as ei:
            compute_cross_section(
                layer_uri=base, line=[[0.0, 0.5], [1.0, 0.5]], extra_layer_uris=extras
            )
        assert ei.value.error_code == "TOO_MANY_LAYERS"

    def test_duplicate_basename_disambiguated(self, tmp_path):
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        p1 = _make_raster(d1, _x_ramp(), units="m", name="dem.tif")
        p2 = _make_raster(d2, _x_ramp(), units="m", name="dem.tif")
        payload = compute_cross_section(
            layer_uri=p1, line=[[0.0, 0.5], [1.0, 0.5]], n_stations=4,
            extra_layer_uris=[p2],
        )
        spec = _assert_valid_chart_payload(payload)
        labels = {r["layer"] for r in _all_rows(spec)}
        # Two distinct legend labels even though both files are "dem.tif".
        assert len(labels) == 2


# ---------------------------------------------------------------------------
# Honesty floor: nodata -> null, line outside -> typed error
# ---------------------------------------------------------------------------


class TestHonestyFloor:
    def test_nodata_stations_become_null(self, tmp_path):
        # Left half of the ramp is nodata (-9999); the right half is valid.
        arr = _x_ramp(scale=100.0)
        arr[:, : arr.shape[1] // 2] = -9999.0
        path = _make_raster(tmp_path, arr, nodata=-9999.0, units="m", name="halfnd.tif")

        payload = compute_cross_section(
            layer_uri=path, line=[[0.0, 0.5], [1.0, 0.5]], n_stations=20
        )
        spec = _assert_valid_chart_payload(payload)
        vals = [r["value"] for r in _all_rows(spec)]
        # Some stations null (west half), some valid (east half) -- not dropped.
        assert any(v is None for v in vals)
        assert any(v is not None for v in vals)
        # Row count is preserved (null stations kept, not filtered out).
        assert len(vals) == 20

    def test_line_entirely_outside_raster_typed_error(self, tmp_path):
        # Raster covers (0,0)-(1,1); the line is far away at lon ~100.
        path = _make_raster(tmp_path, _x_ramp(), nodata=-9999.0, name="small.tif")
        with pytest.raises(CrossSectionError) as ei:
            compute_cross_section(
                layer_uri=path, line=[[100.0, 50.0], [101.0, 50.0]], n_stations=10
            )
        assert ei.value.error_code == "LINE_OUTSIDE_RASTER"
        assert ei.value.retryable is False


# ---------------------------------------------------------------------------
# CRS handling: a UTM raster sampled from an EPSG:4326 line
# ---------------------------------------------------------------------------


class TestCrsMismatch:
    def test_utm_raster_sampled_from_wgs84_line(self, tmp_path):
        # A small UTM zone 16N raster around a known lon/lat. Build it by
        # reprojecting a known geographic box into UTM bounds.
        from pyproj import Transformer

        # Geographic box near (lon=-85.5, lat=30.0) -- Mexico Beach-ish.
        west, south, east, north = -85.52, 29.99, -85.48, 30.01
        tr = Transformer.from_crs("EPSG:4326", "EPSG:32616", always_xy=True)
        minx, miny = tr.transform(west, south)
        maxx, maxy = tr.transform(east, north)

        arr = _x_ramp(scale=10.0)  # 0..10 m ramp west->east in UTM x
        path = _make_raster(
            tmp_path,
            arr,
            bounds=(minx, miny, maxx, maxy),
            crs="EPSG:32616",
            units="m",
            name="utm.tif",
        )
        # The line is given in EPSG:4326 (lon/lat) -- the tool must reproject it
        # into the raster CRS to sample the right cells.
        line = [[west + 0.001, 30.0], [east - 0.001, 30.0]]
        payload = compute_cross_section(layer_uri=path, line=line, n_stations=9)
        spec = _assert_valid_chart_payload(payload)
        vals = [r["value"] for r in _all_rows(spec)]
        assert all(v is not None for v in vals)
        # West->east ramp -> increasing values; sane physical range.
        assert vals[0] < vals[-1]
        assert 0.0 <= min(vals) <= 10.0 and 0.0 <= max(vals) <= 10.0


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_empty_layer_uri_rejected(self):
        with pytest.raises(CrossSectionError) as ei:
            compute_cross_section(layer_uri="", line=[[0, 0], [1, 1]])
        assert ei.value.error_code == "NO_LAYERS"

    def test_missing_local_file_typed_error(self, tmp_path):
        missing = str(tmp_path / "does_not_exist.tif")
        with pytest.raises(CrossSectionError) as ei:
            compute_cross_section(layer_uri=missing, line=[[0.0, 0.5], [1.0, 0.5]])
        assert ei.value.error_code == "LAYER_OPEN_FAILED"

    def test_extra_kwargs_absorbed(self, tmp_path):
        # job-0164: LLM-invented kwargs must not break the call.
        path = _make_raster(tmp_path, _x_ramp(), name="ek.tif")
        payload = compute_cross_section(
            layer_uri=path,
            line=[[0.0, 0.5], [1.0, 0.5]],
            n_stations=4,
            some_invented_kwarg="ignored",
        )
        _assert_valid_chart_payload(payload)


# ---------------------------------------------------------------------------
# Registration + discoverability
# ---------------------------------------------------------------------------


def test_registered_via_package_import_path():
    """FIX 1 (fused-import regression guard): importing the tools PACKAGE alone
    (NOT the compute_cross_section module directly) must register the tool.

    The other tests in this file import ``compute_cross_section`` directly (which
    self-registers as a side effect), so they pass even when ``tools/__init__.py``
    fails to import the module. This test instead asserts registration in a FRESH
    interpreter that imports ONLY the package -- the path the live agent / catalog
    / LLM-declaration build actually take. A fused ``from . import ...`` line in
    ``tools/__init__.py`` that swallows the ``compute_cross_section`` import would
    FAIL here (a subprocess so it can't be masked by another test's direct
    import already populating the in-process registry).
    """
    import subprocess
    import sys

    code = (
        "import grace2_agent.tools as t; "
        "import sys; "
        "sys.exit(0 if 'compute_cross_section' in t.TOOL_REGISTRY else 1)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True)
    assert proc.returncode == 0, (
        "compute_cross_section must register via the tools package import "
        "(tools/__init__.py must `from . import compute_cross_section`); "
        f"stderr={proc.stderr.decode()[-2000:]}"
    )


def test_registered_in_tool_registry():
    from grace2_agent.tools import TOOL_REGISTRY

    assert "compute_cross_section" in TOOL_REGISTRY
    md = TOOL_REGISTRY["compute_cross_section"].metadata
    assert md.name == "compute_cross_section"
    assert md.cacheable is False
    assert md.ttl_class == "live-no-cache"
    assert md.read_only_hint is True
    assert md.open_world_hint is False
    assert md.idempotent_hint is True


def test_in_geographic_primitives_category():
    from grace2_agent.categories import PRIMARY_CATEGORY, tools_for_category

    assert PRIMARY_CATEGORY.get("compute_cross_section") == "geographic_primitives"
    assert "compute_cross_section" in tools_for_category("geographic_primitives")
