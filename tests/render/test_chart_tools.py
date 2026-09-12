"""The generic ``generate_chart`` primitive and the agent chart-emission loop.

Synthetic in-memory data, no network and no model. The SHAPE is the caller's
Vega-Lite spec plus inline records, so every mark is forced interactive, an image
mark is rejected as the anti-PNG honesty floor and a mark-less spec raises. The
inline row cap holds, the summarizer strips the spec, and the emit persists."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.render.charts import (
    ChartToolError,
    _MAX_ROWS,
    build_chart_payload,
    is_chart_emission_result,
)
from trid3nt_server.tools.derive.charts.generate_chart.generate_chart import (
    generate_chart,
)
from trid3nt_contracts.chart_contracts import (
    ChartEmissionPayload,
    is_structurally_valid_vega_lite_spec,
)




def _make_raster(tmp_path: Path, values: np.ndarray, name: str = "r.tif") -> str:
    import rasterio
    from rasterio.transform import from_bounds

    if values.ndim == 2:
        values = values[np.newaxis, :, :]
    count, height, width = values.shape
    transform = from_bounds(0.0, 0.0, 1.0, 1.0, width, height)
    path = str(tmp_path / name)
    with rasterio.open(
        path, "w", driver="GTiff", dtype=values.dtype, width=width, height=height,
        count=count, crs="EPSG:4326", transform=transform,
    ) as dst:
        for b in range(count):
            dst.write(values[b], b + 1)
    return path


def _make_geojson_points(tmp_path: Path, records: list[dict], name: str = "pts.geojson") -> str:
    features = []
    for r in records:
        r = dict(r)
        x = r.pop("x")
        y = r.pop("y")
        features.append(
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [x, y]},
             "properties": r}
        )
    path = str(tmp_path / name)
    with open(path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    return path


def _bar_spec(x_field: str = "label", y_field: str = "count") -> dict:
    return {
        "mark": "bar",
        "encoding": {
            "x": {"field": x_field, "type": "ordinal"},
            "y": {"field": y_field, "type": "quantitative"},
        },
    }


def _assert_valid_interactive_chart(payload: dict, *, expect_source: str | None = None) -> str:
    """Re-validate a returned chart payload against the contract; return mark type."""
    assert isinstance(payload, dict)
    assert payload["envelope_type"] == "chart-emission"
    assert isinstance(payload["chart_id"], str) and payload["chart_id"]
    assert isinstance(payload["title"], str) and payload["title"]
    spec = payload["vega_lite_spec"]
    assert is_structurally_valid_vega_lite_spec(spec), spec
    ChartEmissionPayload.model_validate(payload)  # raises if structurally broken
    mark = spec.get("mark")
    if mark is None and "layer" in spec:
        mark = spec["layer"][0].get("mark")
    assert isinstance(mark, dict), f"mark not normalized to dict: {mark}"
    assert str(mark.get("type")).lower() != "image", "image mark leaked through"
    assert mark.get("tooltip") is True, "tooltip not forced by construction"
    if expect_source is not None:
        assert payload["source_layer_uri"] == expect_source
    return str(mark.get("type"))




class TestInteractivityByConstruction:
    def test_string_mark_becomes_interactive_dict(self):
        payload = generate_chart(
            vega_lite_spec=_bar_spec(),
            title="Bars",
            records=[{"label": "a", "count": 3}, {"label": "b", "count": 5}],
        )
        assert _assert_valid_interactive_chart(payload) == "bar"

    def test_existing_dict_mark_gets_tooltip(self):
        spec = {"mark": {"type": "line"}, "encoding": {
            "x": {"field": "t", "type": "ordinal"}, "y": {"field": "v", "type": "quantitative"}}}
        payload = generate_chart(vega_lite_spec=spec, title="Line",
                                 records=[{"t": "1", "v": 1.0}, {"t": "2", "v": 2.0}])
        assert _assert_valid_interactive_chart(payload) == "line"

    def test_image_mark_rejected(self):
        with pytest.raises(ChartToolError) as exc:
            generate_chart(vega_lite_spec={"mark": {"type": "image", "url": "x"}, "encoding": {}},
                           title="png", records=[{"a": 1}])
        assert exc.value.error_code == "IMAGE_MARK_REJECTED"

    def test_markless_spec_raises(self):
        with pytest.raises(ChartToolError) as exc:
            generate_chart(vega_lite_spec={"encoding": {}}, title="nada", records=[{"a": 1}])
        assert exc.value.error_code == "NO_MARK"

    def test_layered_spec_all_marks_interactive(self):
        spec = {"layer": [
            {"mark": "line", "encoding": {"x": {"field": "t"}, "y": {"field": "v"}}},
            {"mark": {"type": "rule"}, "encoding": {"y": {"field": "thr"}}},
        ]}
        payload = generate_chart(vega_lite_spec=spec, title="Layered",
                                 records=[{"t": "1", "v": 1.0, "thr": 2.0}])
        spec_out = payload["vega_lite_spec"]
        assert all(layer["mark"]["tooltip"] is True for layer in spec_out["layer"])




class TestDataInjection:
    def test_inline_records(self):
        rows = [{"label": str(i), "count": i} for i in range(6)]
        payload = generate_chart(vega_lite_spec=_bar_spec(), title="Inline", records=rows)
        assert payload["vega_lite_spec"]["data"]["values"] == rows

    def test_records_precedence_over_layer(self, tmp_path):
        path = _make_geojson_points(tmp_path, [{"x": 0.1, "y": 0.1, "v": 9.0}])
        rows = [{"label": "a", "count": 1}]
        payload = generate_chart(vega_lite_spec=_bar_spec(), title="Prec",
                                 records=rows, layer_uri=path)
        # records win; source_layer_uri NOT set (no layer read happened).
        assert payload["vega_lite_spec"]["data"]["values"] == rows
        assert payload["source_layer_uri"] is None

    def test_vector_layer_attribute_rows(self, tmp_path):
        recs = [{"x": 0.1 * i, "y": 0.1 * i, "depth": float(i)} for i in range(8)]
        path = _make_geojson_points(tmp_path, recs)
        spec = {"mark": "point", "encoding": {
            "x": {"field": "depth", "type": "quantitative"},
            "y": {"field": "depth", "type": "quantitative"}}}
        payload = generate_chart(vega_lite_spec=spec, title="Scatter", layer_uri=path)
        _assert_valid_interactive_chart(payload, expect_source=path)
        rows = payload["vega_lite_spec"]["data"]["values"]
        assert len(rows) == 8
        assert all("depth" in r and "geometry" not in r for r in rows)

    def test_raster_layer_sampled_values(self, tmp_path):
        arr = np.linspace(0, 50, 100, dtype=np.float32).reshape(10, 10)
        path = _make_raster(tmp_path, arr)
        spec = {"mark": "point", "encoding": {"x": {"field": "value", "type": "quantitative"}}}
        payload = generate_chart(vega_lite_spec=spec, title="Raster rows", layer_uri=path)
        rows = payload["vega_lite_spec"]["data"]["values"]
        assert rows and all("value" in r for r in rows)

    def test_empty_layer_raises(self, tmp_path):
        path = str(tmp_path / "empty.geojson")
        with open(path, "w") as f:
            json.dump({"type": "FeatureCollection", "features": []}, f)
        with pytest.raises(ChartToolError) as exc:
            generate_chart(vega_lite_spec=_bar_spec(), title="Empty", layer_uri=path)
        assert exc.value.error_code == "NO_DATA"




class TestCulledShapeReplication:
    def test_histogram_bars(self):
        counts, edges = np.histogram(np.arange(100, dtype=float), bins=10)
        rows = [{"bin": f"{edges[i]:.3g}-{edges[i+1]:.3g}", "count": int(counts[i])}
                for i in range(10)]
        payload = generate_chart(vega_lite_spec=_bar_spec("bin"), title="Histogram - value",
                                 records=rows)
        assert _assert_valid_interactive_chart(payload) == "bar"
        assert len(payload["vega_lite_spec"]["data"]["values"]) == 10

    def test_time_series_line(self):
        rows = [{"time": f"2020-0{t}", "value": float(t)} for t in range(1, 5)]
        spec = {"mark": "line", "encoding": {
            "x": {"field": "time", "type": "ordinal"},
            "y": {"field": "value", "type": "quantitative"}}}
        payload = generate_chart(vega_lite_spec=spec, title="Time series", records=rows)
        assert _assert_valid_interactive_chart(payload) == "line"

    def test_damage_distribution_bars(self):
        ds = ("DS0 None", "DS1 Slight", "DS2 Moderate", "DS3 Extensive", "DS4 Complete")
        rows = [{"damage_state": ds[i], "count": c} for i, c in enumerate([2, 1, 1, 1, 2])]
        spec = {"mark": "bar", "encoding": {
            "x": {"field": "damage_state", "type": "ordinal", "sort": list(ds)},
            "y": {"field": "count", "type": "quantitative"},
            "color": {"field": "count", "type": "ordinal", "scale": {"scheme": "yellorred"}}}}
        payload = generate_chart(vega_lite_spec=spec, title="Damage-state distribution", records=rows)
        assert _assert_valid_interactive_chart(payload) == "bar"
        assert len(payload["vega_lite_spec"]["data"]["values"]) == 5

    def test_choropleth_legend_bars(self):
        rows = [{"class_label": f"c{i}", "count": 10} for i in range(5)]
        payload = generate_chart(vega_lite_spec=_bar_spec("class_label"),
                                 title="Choropleth legend", records=rows)
        assert _assert_valid_interactive_chart(payload) == "bar"




class TestRowCap:
    def test_inline_rows_capped(self):
        big = [{"label": str(i), "count": i} for i in range(_MAX_ROWS + 500)]
        payload = generate_chart(vega_lite_spec=_bar_spec(), title="big", records=big)
        assert len(payload["vega_lite_spec"]["data"]["values"]) == _MAX_ROWS

    def test_schema_injected(self):
        payload = generate_chart(vega_lite_spec=_bar_spec(), title="t",
                                 records=[{"label": "a", "count": 1}])
        assert "$schema" in payload["vega_lite_spec"]

    def test_build_chart_payload_still_caps_direct(self):
        big = [{"x": i, "count": i} for i in range(_MAX_ROWS + 10)]
        spec = {"data": {"values": big}, "mark": "bar",
                "encoding": {"x": {"field": "x", "type": "ordinal"},
                             "y": {"field": "count", "type": "quantitative"}}}
        payload = build_chart_payload(vega_lite_spec=spec, title="big")
        assert len(payload["vega_lite_spec"]["data"]["values"]) == _MAX_ROWS




class TestChartEmissionDiscriminator:
    def test_true_on_generic_chart(self):
        payload = generate_chart(vega_lite_spec=_bar_spec(), title="t",
                                 records=[{"label": "a", "count": 1}])
        assert is_chart_emission_result(payload) is True

    def test_false_on_ordinary_results(self):
        assert is_chart_emission_result({"layer_type": "raster", "count": 9}) is False
        assert is_chart_emission_result({"envelope_type": "impact-envelope"}) is False
        assert is_chart_emission_result(None) is False
        assert is_chart_emission_result("a string") is False
        assert is_chart_emission_result([1, 2, 3]) is False
        assert is_chart_emission_result(
            {"envelope_type": "chart-emission", "chart_id": "x"}
        ) is False




class TestSummarizeChartEmission:
    def test_spec_stripped_for_chart(self):
        from trid3nt_server.adapters.adapter import summarize_tool_result

        payload = generate_chart(vega_lite_spec=_bar_spec(), title="t", caption="cap",
                                 records=[{"label": "a", "count": 1}, {"label": "b", "count": 2}])
        summary = summarize_tool_result("generate_chart", payload)

        assert summary["status"] == "ok"
        res = summary["result"]
        assert res["chart_emitted"] is True
        assert res["chart_id"] == payload["chart_id"]
        assert res["title"] == payload["title"]
        assert res["caption"] == payload["caption"]
        assert "vega_lite_spec" not in res
        assert "vega_lite_spec" not in json.dumps(summary)
        assert res["n_data_rows"] == 2
        assert res["chart_type"] == "bar"

    def test_ordinary_dict_preserved(self):
        from trid3nt_server.adapters.adapter import summarize_tool_result

        ordinary = {"columns": ["count"], "rows": [[9]], "row_count": 1, "count": 9}
        summary = summarize_tool_result("compute_layer_bounds", ordinary)
        assert summary["status"] == "ok"
        assert summary["result"]["count"] == 9




class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, msg: str) -> None:
        self.sent.append(msg)


class _FakeMCP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        return {"matchedCount": 1, "modifiedCount": 1}


@pytest.mark.asyncio
class TestEmitChart:
    async def _make_state(self, session_id=None, case_id=None):
        from trid3nt_server.server import SessionState
        from trid3nt_contracts import new_ulid

        state = SessionState(session_id=session_id or new_ulid())
        state.active_case_id = case_id
        return state

    async def test_emits_envelope_and_persists(self, monkeypatch):
        import trid3nt_server.server as server
        from trid3nt_server.persistence import Persistence
        from trid3nt_contracts import new_ulid

        fake_mcp = _FakeMCP()
        persistence = Persistence(fake_mcp)
        monkeypatch.setattr(server, "get_persistence", lambda: persistence)

        case_id = new_ulid()
        turn_id = new_ulid()
        state = await self._make_state(case_id=case_id)
        state.current_turn_pipeline_id = turn_id

        payload = generate_chart(vega_lite_spec=_bar_spec(), title="t",
                                 records=[{"label": "a", "count": 1}])

        ws = _FakeWS()
        await server._maybe_emit_chart(ws, state, payload)

        assert len(ws.sent) == 1
        env = json.loads(ws.sent[0])
        assert env["type"] == "chart-emission"
        assert env["session_id"] == state.session_id
        assert env["payload"]["envelope_type"] == "chart-emission"
        assert "vega_lite_spec" in env["payload"]
        assert env["payload"]["created_turn_id"] == turn_id

        assert len(fake_mcp.calls) == 1
        name, args = fake_mcp.calls[0]
        assert name == "update-one"
        assert args["collection"] == "sessions"
        assert args["filter"]["_id"] == case_id
        assert "$push" in args["update"] and "charts" in args["update"]["$push"]
        pushed = args["update"]["$push"]["charts"]
        assert pushed["payload"]["chart_id"] == payload["chart_id"]
        assert pushed["schema_version"] == "v1"

    async def test_persist_keyed_by_session_when_no_case(self, monkeypatch):
        import trid3nt_server.server as server
        from trid3nt_server.persistence import Persistence

        fake_mcp = _FakeMCP()
        persistence = Persistence(fake_mcp)
        monkeypatch.setattr(server, "get_persistence", lambda: persistence)

        state = await self._make_state(case_id=None)
        payload = generate_chart(vega_lite_spec=_bar_spec(), title="t",
                                 records=[{"label": "a", "count": 1}])
        ws = _FakeWS()
        await server._maybe_emit_chart(ws, state, payload)
        name, args = fake_mcp.calls[0]
        assert args["filter"]["_id"] == state.session_id

    async def test_no_persistence_singleton_is_safe(self, monkeypatch):
        import trid3nt_server.server as server

        monkeypatch.setattr(server, "get_persistence", lambda: None)
        state = await self._make_state()
        payload = generate_chart(vega_lite_spec=_bar_spec(), title="t",
                                 records=[{"label": "a", "count": 1}])
        ws = _FakeWS()
        await server._maybe_emit_chart(ws, state, payload)
        assert len(ws.sent) == 1




def test_dispatch_detection_signal(tmp_path):
    from trid3nt_server.tools.derive.compute_layer_bounds.compute_layer_bounds import (
        compute_layer_bounds,
    )

    records = [{"x": 0.1 * i, "y": 0.1 * i, "v": float(i)} for i in range(4)]
    vec_path = _make_geojson_points(tmp_path, records)

    chart = generate_chart(vega_lite_spec=_bar_spec(), title="t",
                           records=[{"label": "a", "count": 1}])
    bounds = asyncio.run(compute_layer_bounds(layer_uri=vec_path, fit_map=False))

    assert is_chart_emission_result(chart) is True
    assert is_chart_emission_result(bounds) is False




class TestRegistration:
    def test_in_tool_registry(self):
        from trid3nt_server.tools import TOOL_REGISTRY

        assert "generate_chart" in TOOL_REGISTRY

    def test_metadata(self):
        from trid3nt_server.tools import TOOL_REGISTRY

        m = TOOL_REGISTRY["generate_chart"].metadata
        assert m.ttl_class == "dynamic-1h"
        assert m.source_class == "chart_tools"
        assert m.read_only_hint is True

    def test_culled_chart_tools_absent(self):
        from trid3nt_server.tools import TOOL_REGISTRY

        for dead in ("generate_histogram", "generate_choropleth_legend",
                     "generate_time_series", "generate_damage_distribution"):
            assert dead not in TOOL_REGISTRY




def _summarizers():
    from trid3nt_server.render.charts import _summarize_raster, _summarize_vector

    return _summarize_raster, _summarize_vector


def _write_masked_raster(path: Path, data: np.ndarray, mask: np.ndarray) -> Path:
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_bounds

    h, w = data.shape
    with rasterio.open(
        path, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
        crs="EPSG:4326", transform=from_bounds(-100.0, 40.0, -99.0, 41.0, w, h),
    ) as ds:
        ds.write(data.astype("float32"), 1)
        ds.write_mask(mask)
    return path


def test_summarize_raster_excludes_mask_band_pixels(tmp_path: Path) -> None:
    # GDAL's validity, not a nodata comparison, decides what counts: a raster
    # whose mask band clears four pixels summarizes over the other 60.
    summarize_raster, _ = _summarizers()
    data = np.arange(64, dtype="float32").reshape(8, 8)
    mask = np.full((8, 8), 255, dtype="uint8")
    mask[0, :4] = 0
    path = _write_masked_raster(tmp_path / "masked.tif", data, mask)

    s = summarize_raster(str(path))
    assert s["count"] == 60
    assert s["min"] == 4.0
    assert s["sum"] == 2010.0
    assert sum(b["count"] for b in s["distribution"]) == 60


def test_summarize_raster_excludes_nan_the_file_never_tagged(tmp_path: Path) -> None:
    # NaN fill in an untagged raster is masked on top of GDAL's own validity.
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_bounds

    summarize_raster, _ = _summarizers()
    data = np.arange(16, dtype="float32").reshape(4, 4)
    data[0, :2] = np.nan
    path = tmp_path / "nan.tif"
    with rasterio.open(
        path, "w", driver="GTiff", height=4, width=4, count=1, dtype="float32",
        crs="EPSG:4326", transform=from_bounds(-100.0, 40.0, -99.0, 41.0, 4, 4),
    ) as ds:
        ds.write(data, 1)

    s = summarize_raster(str(path))
    assert s["count"] == 14
    assert s["min"] == 2.0


def test_summarize_vector_reports_zeros_for_an_all_null_column(tmp_path: Path) -> None:
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    _, summarize_vector = _summarizers()
    gdf = gpd.GeoDataFrame(
        {"depth": [1.0, np.nan, 3.5], "empty": [np.nan, np.nan, np.nan]},
        geometry=[Point(-99.5, 40.5), Point(-99.4, 40.6), Point(-99.3, 40.7)],
        crs="EPSG:4326",
    )
    path = tmp_path / "pts.gpkg"
    gdf.to_file(path, driver="GPKG")

    s = summarize_vector(str(path))
    assert s["feature_count"] == 3
    assert s["attribute_summary"]["depth"] == {
        "count": 2, "min": 1.0, "max": 3.5, "mean": 2.25, "sum": 4.5,
    }
    assert s["attribute_summary"]["empty"] == {
        "count": 0, "min": None, "max": None, "mean": None, "sum": None,
    }


def test_summarize_vector_skips_non_numeric_columns(tmp_path: Path) -> None:
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    _, summarize_vector = _summarizers()
    gdf = gpd.GeoDataFrame(
        {"depth": [1.0, 2.0], "label": ["a", "b"]},
        geometry=[Point(-99.5, 40.5), Point(-99.4, 40.6)],
        crs="EPSG:4326",
    )
    path = tmp_path / "typed.gpkg"
    gdf.to_file(path, driver="GPKG")

    assert set(summarize_vector(str(path))["attribute_summary"]) == {"depth"}
