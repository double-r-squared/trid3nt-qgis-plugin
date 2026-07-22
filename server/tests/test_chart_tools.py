"""Tests for chart-generation tools + agent chart-emission loop (job-0230).

All tests use synthetic in-memory/temp-file data — no network, no Gemini calls.

Coverage:
- Each of the 4 chart tools produces a structurally-valid ChartEmissionPayload
  on synthetic rasters / GeoJSON (the contract's own validator runs on
  construction, so a structurally-broken spec would raise).
- Inline row cap (_MAX_ROWS) enforced.
- generate_time_series: clean NO_TIME_DIMENSION error envelope on a non-temporal
  layer; happy path on a temporal raster (band descriptions) + temporal vector
  (time column).
- generate_damage_distribution: DS0..DS4 bins on a synthetic Pelicun FGB;
  MISSING_DAMAGE_COLUMN on a layer without ds_mean.
- is_chart_emission_result: triggers on chart payloads, NOT on ordinary results.
- adapter.summarize_tool_result: strips vega_lite_spec for chart payloads,
  keeps the full result for ordinary tool dicts.
- server._maybe_emit_chart: emits the chart-emission WS envelope AND calls the
  persistence append; the emission helper does NOT fire for an ordinary result.
- Registration + category membership.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.tools.chart_tools import (
    ChartToolError,
    _MAX_ROWS,
    build_chart_payload,
    generate_choropleth_legend,
    generate_damage_distribution,
    generate_histogram,
    generate_time_series,
    is_chart_emission_result,
)
from trid3nt_contracts.chart_contracts import (
    ChartEmissionPayload,
    is_structurally_valid_vega_lite_spec,
)


# ---------------------------------------------------------------------------
# Synthetic-fixture helpers
# ---------------------------------------------------------------------------


def _make_raster(
    tmp_path: Path,
    values: np.ndarray,
    nodata: float | None = None,
    descriptions: list[str] | None = None,
    name: str = "test_raster.tif",
) -> str:
    """Write a (possibly multiband) GeoTIFF and return its local path.

    ``values`` is (bands, h, w) for multiband or (h, w) for single-band.
    ``descriptions`` (one per band) marks the raster as temporal for the
    time-series tool.
    """
    import rasterio
    from rasterio.transform import from_bounds

    if values.ndim == 2:
        values = values[np.newaxis, :, :]
    count, height, width = values.shape
    transform = from_bounds(0.0, 0.0, 1.0, 1.0, width, height)
    profile = {
        "driver": "GTiff",
        "dtype": values.dtype,
        "width": width,
        "height": height,
        "count": count,
        "crs": "EPSG:4326",
        "transform": transform,
    }
    if nodata is not None:
        profile["nodata"] = nodata
    path = str(tmp_path / name)
    with rasterio.open(path, "w", **profile) as dst:
        for b in range(count):
            dst.write(values[b], b + 1)
        if descriptions:
            for b, desc in enumerate(descriptions):
                dst.set_band_description(b + 1, desc)
    return path


def _make_geojson_points(tmp_path: Path, records: list[dict], name: str = "pts.geojson") -> str:
    features = []
    for r in records:
        r = dict(r)
        x = r.pop("x")
        y = r.pop("y")
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [x, y]},
                "properties": r,
            }
        )
    fc = {"type": "FeatureCollection", "features": features}
    path = str(tmp_path / name)
    with open(path, "w") as f:
        json.dump(fc, f)
    return path


def _make_damage_fgb(tmp_path: Path, ds_means: list[float], name: str = "damage.fgb") -> str:
    """Write a synthetic per-asset Pelicun damage FlatGeobuf with ds_mean."""
    import geopandas as gpd
    from shapely.geometry import Point

    gdf = gpd.GeoDataFrame(
        {
            "ds_mean": ds_means,
            "repair_cost_mean": [v * 1000.0 for v in ds_means],
            "geometry": [Point(0.1 * i, 0.1 * i) for i in range(len(ds_means))],
        },
        crs="EPSG:4326",
    )
    path = str(tmp_path / name)
    gdf.to_file(path, driver="FlatGeobuf")
    return path


def _assert_valid_chart_payload(payload: dict, *, expect_source: str | None = None) -> None:
    """Re-validate a returned chart payload against the contract."""
    assert isinstance(payload, dict)
    assert payload["envelope_type"] == "chart-emission"
    assert isinstance(payload["chart_id"], str) and payload["chart_id"]
    assert isinstance(payload["title"], str) and payload["title"]
    spec = payload["vega_lite_spec"]
    assert is_structurally_valid_vega_lite_spec(spec), spec
    # Round-trips through the pydantic contract (raises if structurally broken).
    model = ChartEmissionPayload.model_validate(payload)
    assert model.envelope_type == "chart-emission"
    if expect_source is not None:
        assert payload["source_layer_uri"] == expect_source


# ---------------------------------------------------------------------------
# generate_histogram
# ---------------------------------------------------------------------------


class TestGenerateHistogram:
    def test_raster_histogram(self, tmp_path):
        arr = np.linspace(0, 100, 100, dtype=np.float32).reshape(10, 10)
        path = _make_raster(tmp_path, arr)

        payload = generate_histogram(layer_uri=path)

        _assert_valid_chart_payload(payload, expect_source=path)
        spec = payload["vega_lite_spec"]
        assert spec["mark"]["type"] == "bar"
        # 10 histogram bins inline.
        assert len(spec["data"]["values"]) == 10
        assert all("count" in row for row in spec["data"]["values"])
        # Caption carries computed numbers (determinism boundary).
        assert "min" in payload["caption"] and "max" in payload["caption"]

    def test_vector_histogram_default_property(self, tmp_path):
        records = [{"x": 0.1 * i, "y": 0.1 * i, "depth": float(i)} for i in range(20)]
        path = _make_geojson_points(tmp_path, records)

        payload = generate_histogram(layer_uri=path)

        _assert_valid_chart_payload(payload)
        # Default property is the first numeric column ("depth").
        assert "depth" in payload["title"]

    def test_vector_histogram_named_property(self, tmp_path):
        records = [
            {"x": 0.1 * i, "y": 0.1 * i, "depth": float(i), "value": float(i * 2)}
            for i in range(10)
        ]
        path = _make_geojson_points(tmp_path, records)

        payload = generate_histogram(layer_uri=path, property="value")

        _assert_valid_chart_payload(payload)
        assert "value" in payload["title"]

    def test_property_not_found_raises(self, tmp_path):
        records = [{"x": 0.1, "y": 0.1, "depth": 1.0}]
        path = _make_geojson_points(tmp_path, records)

        with pytest.raises(ChartToolError) as exc:
            generate_histogram(layer_uri=path, property="nope")
        assert exc.value.error_code == "PROPERTY_NOT_FOUND"

    def test_no_numeric_property_raises(self, tmp_path):
        records = [{"x": 0.1, "y": 0.1, "label": "a"}]
        path = _make_geojson_points(tmp_path, records)

        with pytest.raises(ChartToolError) as exc:
            generate_histogram(layer_uri=path)
        assert exc.value.error_code == "NO_NUMERIC_PROPERTY"

    def test_missing_uri_raises(self):
        with pytest.raises(ChartToolError) as exc:
            generate_histogram(layer_uri="")
        assert exc.value.error_code == "DOWNLOAD_FAILED"

    def test_raster_sampling_cap_path(self, tmp_path, monkeypatch):
        """Large raster path: cap the sample, still produce 10 bins."""
        import trid3nt_server.tools.chart_tools as ct

        # Shrink the cap so a small raster exercises the sampling branch.
        monkeypatch.setattr(ct, "_RASTER_SAMPLE_CAP", 50)
        arr = np.arange(100, dtype=np.float32).reshape(10, 10)
        path = _make_raster(tmp_path, arr)

        payload = generate_histogram(layer_uri=path)
        _assert_valid_chart_payload(payload)
        assert len(payload["vega_lite_spec"]["data"]["values"]) == 10


# ---------------------------------------------------------------------------
# generate_choropleth_legend
# ---------------------------------------------------------------------------


class TestGenerateChoroplethLegend:
    def test_vector_class_breaks(self, tmp_path):
        records = [{"x": 0.05 * i, "y": 0.05 * i, "pop": float(i)} for i in range(50)]
        path = _make_geojson_points(tmp_path, records)

        payload = generate_choropleth_legend(layer_uri=path, property="pop")

        _assert_valid_chart_payload(payload, expect_source=path)
        spec = payload["vega_lite_spec"]
        assert spec["mark"]["type"] == "bar"
        rows = spec["data"]["values"]
        # 5 quantile classes for well-distributed data.
        assert 1 <= len(rows) <= 5
        # Class counts sum to the feature total.
        assert sum(r["count"] for r in rows) == 50
        assert "class" in payload["caption"] or "classes" in payload["caption"]

    def test_degenerate_all_equal(self, tmp_path):
        records = [{"x": 0.1 * i, "y": 0.1 * i, "pop": 5.0} for i in range(10)]
        path = _make_geojson_points(tmp_path, records)

        payload = generate_choropleth_legend(layer_uri=path, property="pop")
        _assert_valid_chart_payload(payload)
        rows = payload["vega_lite_spec"]["data"]["values"]
        assert sum(r["count"] for r in rows) == 10

    def test_raster_class_breaks(self, tmp_path):
        arr = np.linspace(0, 50, 100, dtype=np.float32).reshape(10, 10)
        path = _make_raster(tmp_path, arr)

        payload = generate_choropleth_legend(layer_uri=path)
        _assert_valid_chart_payload(payload)


# ---------------------------------------------------------------------------
# generate_time_series
# ---------------------------------------------------------------------------


class TestGenerateTimeSeries:
    def test_temporal_raster(self, tmp_path):
        # 4-band raster with band descriptions = temporal.
        bands = np.stack(
            [np.full((4, 4), float(t), dtype=np.float32) for t in range(4)]
        )
        path = _make_raster(
            tmp_path,
            bands,
            descriptions=["2020-01", "2020-02", "2020-03", "2020-04"],
            name="temporal.tif",
        )

        payload = generate_time_series(layer_uri=path)

        _assert_valid_chart_payload(payload, expect_source=path)
        spec = payload["vega_lite_spec"]
        assert spec["mark"]["type"] == "line"
        rows = spec["data"]["values"]
        assert len(rows) == 4
        assert [r["time"] for r in rows] == ["2020-01", "2020-02", "2020-03", "2020-04"]
        # Per-band means: 0, 1, 2, 3.
        assert [r["value"] for r in rows] == [0.0, 1.0, 2.0, 3.0]

    def test_temporal_vector(self, tmp_path):
        records = [
            {"x": 0.1, "y": 0.1, "time": "2021-01-01", "discharge": 10.0},
            {"x": 0.2, "y": 0.2, "time": "2021-01-02", "discharge": 20.0},
            {"x": 0.3, "y": 0.3, "time": "2021-01-03", "discharge": 15.0},
        ]
        path = _make_geojson_points(tmp_path, records)

        payload = generate_time_series(layer_uri=path)

        _assert_valid_chart_payload(payload)
        rows = payload["vega_lite_spec"]["data"]["values"]
        assert len(rows) == 3
        # Sorted by time (geopandas may parse the time column to datetime, so
        # the stringified label can carry a 00:00:00 suffix — check the prefix).
        times = [r["time"] for r in rows]
        assert times[0].startswith("2021-01-01")
        assert times[1].startswith("2021-01-02")
        assert times[2].startswith("2021-01-03")
        # Values follow the (sorted-by-time) discharge sequence.
        assert [r["value"] for r in rows] == [10.0, 20.0, 15.0]

    def test_non_temporal_raster_error_envelope(self, tmp_path):
        """Single-band raster -> NO_TIME_DIMENSION (clean error envelope)."""
        arr = np.ones((4, 4), dtype=np.float32)
        path = _make_raster(tmp_path, arr)

        with pytest.raises(ChartToolError) as exc:
            generate_time_series(layer_uri=path)
        assert exc.value.error_code == "NO_TIME_DIMENSION"

    def test_multiband_no_descriptions_not_temporal(self, tmp_path):
        """An RGB-like 3-band raster with no descriptions is NOT temporal."""
        bands = np.stack([np.ones((4, 4), dtype=np.float32) for _ in range(3)])
        path = _make_raster(tmp_path, bands, name="rgb.tif")

        with pytest.raises(ChartToolError) as exc:
            generate_time_series(layer_uri=path)
        assert exc.value.error_code == "NO_TIME_DIMENSION"

    def test_non_temporal_vector_error_envelope(self, tmp_path):
        records = [{"x": 0.1, "y": 0.1, "depth": 1.0}]
        path = _make_geojson_points(tmp_path, records)

        with pytest.raises(ChartToolError) as exc:
            generate_time_series(layer_uri=path)
        assert exc.value.error_code == "NO_TIME_DIMENSION"


# ---------------------------------------------------------------------------
# generate_damage_distribution
# ---------------------------------------------------------------------------


class TestGenerateDamageDistribution:
    def test_damage_bins(self, tmp_path):
        # ds_means spanning DS0..DS4: round() -> 0,0,1,2,3,4,4.
        ds_means = [0.1, 0.4, 1.2, 2.0, 3.4, 3.6, 4.0]
        path = _make_damage_fgb(tmp_path, ds_means)

        payload = generate_damage_distribution(damage_layer_uri=path)

        _assert_valid_chart_payload(payload, expect_source=path)
        spec = payload["vega_lite_spec"]
        assert spec["mark"]["type"] == "bar"
        rows = spec["data"]["values"]
        # Always 5 DS bins (DS0..DS4), zeros included.
        assert len(rows) == 5
        by_key = {r["ds_key"]: r["count"] for r in rows}
        # round(0.1)=0, round(0.4)=0 -> DS0=2
        assert by_key["DS0_none"] == 2
        assert by_key["DS1_slight"] == 1  # round(1.2)=1
        assert by_key["DS2_moderate"] == 1  # round(2.0)=2
        assert by_key["DS3_extensive"] == 1  # round(3.4)=3
        assert by_key["DS4_complete"] == 2  # round(3.6)=4, round(4.0)=4
        # Total across bins == feature count.
        assert sum(by_key.values()) == 7
        # Caption carries damaged + destroyed counts.
        assert "structures" in payload["caption"]

    def test_missing_ds_mean_column(self, tmp_path):
        records = [{"x": 0.1, "y": 0.1, "depth": 1.0}]
        path = _make_geojson_points(tmp_path, records, name="nods.geojson")

        with pytest.raises(ChartToolError) as exc:
            generate_damage_distribution(damage_layer_uri=path)
        assert exc.value.error_code == "MISSING_DAMAGE_COLUMN"

    def test_empty_layer(self, tmp_path):
        fc = {"type": "FeatureCollection", "features": []}
        path = str(tmp_path / "empty.geojson")
        with open(path, "w") as f:
            json.dump(fc, f)

        with pytest.raises(ChartToolError) as exc:
            generate_damage_distribution(damage_layer_uri=path)
        assert exc.value.error_code == "NO_DATA"


# ---------------------------------------------------------------------------
# Row-cap enforcement (build_chart_payload)
# ---------------------------------------------------------------------------


class TestRowCap:
    def test_inline_rows_capped(self):
        big = [{"x": i, "count": i} for i in range(_MAX_ROWS + 500)]
        spec = {
            "data": {"values": big},
            "mark": "bar",
            "encoding": {
                "x": {"field": "x", "type": "ordinal"},
                "y": {"field": "count", "type": "quantitative"},
            },
        }
        payload = build_chart_payload(vega_lite_spec=spec, title="big")
        assert len(payload["vega_lite_spec"]["data"]["values"]) == _MAX_ROWS

    def test_schema_injected(self):
        spec = {
            "data": {"values": [{"a": 1}]},
            "mark": "bar",
            "encoding": {"x": {"field": "a", "type": "quantitative"}},
        }
        payload = build_chart_payload(vega_lite_spec=spec, title="t")
        assert "$schema" in payload["vega_lite_spec"]


# ---------------------------------------------------------------------------
# is_chart_emission_result discriminator
# ---------------------------------------------------------------------------


class TestChartEmissionDiscriminator:
    def test_true_on_chart_payload(self, tmp_path):
        arr = np.arange(16, dtype=np.float32).reshape(4, 4)
        path = _make_raster(tmp_path, arr)
        payload = generate_histogram(layer_uri=path)
        assert is_chart_emission_result(payload) is True

    def test_false_on_ordinary_results(self):
        # Ordinary tool dict (e.g. a LayerURI / stats result).
        assert is_chart_emission_result({"layer_type": "raster", "count": 9}) is False
        assert is_chart_emission_result({"envelope_type": "impact-envelope"}) is False
        assert is_chart_emission_result(None) is False
        assert is_chart_emission_result("a string") is False
        assert is_chart_emission_result([1, 2, 3]) is False
        # chart-emission discriminator but no vega spec -> still False.
        assert is_chart_emission_result(
            {"envelope_type": "chart-emission", "chart_id": "x"}
        ) is False


# ---------------------------------------------------------------------------
# adapter.summarize_tool_result strips the spec for charts
# ---------------------------------------------------------------------------


class TestSummarizeChartEmission:
    def test_spec_stripped_for_chart(self, tmp_path):
        from trid3nt_server.adapter import summarize_tool_result

        records = [{"x": 0.1 * i, "y": 0.1 * i, "v": float(i)} for i in range(30)]
        path = _make_geojson_points(tmp_path, records)
        payload = generate_histogram(layer_uri=path, property="v")

        summary = summarize_tool_result("generate_histogram", payload)

        assert summary["status"] == "ok"
        res = summary["result"]
        assert res["chart_emitted"] is True
        assert res["chart_id"] == payload["chart_id"]
        assert res["title"] == payload["title"]
        assert res["caption"] == payload["caption"]
        # The full inline spec must NOT appear in the function_response.
        assert "vega_lite_spec" not in res
        assert "vega_lite_spec" not in json.dumps(summary)
        # But the row-count IS surfaced (compact metadata).
        assert res["n_data_rows"] == 10
        assert res["chart_type"] == "bar"

    def test_ordinary_dict_preserved(self):
        from trid3nt_server.adapter import summarize_tool_result

        ordinary = {"layer_type": "raster", "count": 9, "mean": 5.0}
        summary = summarize_tool_result("summarize_layer_statistics", ordinary)
        assert summary["status"] == "ok"
        # Ordinary results keep their content (coerced summary).
        assert summary["result"]["count"] == 9


# ---------------------------------------------------------------------------
# server._maybe_emit_chart — emission + persistence
# ---------------------------------------------------------------------------


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

        # job-0259: active_case_id is now a session-scoped property (not a
        # dataclass field) — set it after construction.
        state = SessionState(session_id=session_id or new_ulid())
        state.active_case_id = case_id
        return state

    async def test_emits_envelope_and_persists(self, tmp_path, monkeypatch):
        import trid3nt_server.server as server
        from trid3nt_server.persistence import Persistence
        from trid3nt_contracts import new_ulid

        fake_mcp = _FakeMCP()
        persistence = Persistence(fake_mcp)
        monkeypatch.setattr(server, "get_persistence", lambda: persistence)

        case_id = new_ulid()
        turn_id = new_ulid()
        state = await self._make_state(case_id=case_id)
        # current turn pipeline id used as the stack-grouping key.
        state.current_turn_pipeline_id = turn_id

        arr = np.arange(16, dtype=np.float32).reshape(4, 4)
        path = _make_raster(tmp_path, arr)
        payload = generate_histogram(layer_uri=path)

        ws = _FakeWS()
        await server._maybe_emit_chart(ws, state, payload)

        # 1. WS envelope emitted with type=chart-emission and the full payload.
        assert len(ws.sent) == 1
        env = json.loads(ws.sent[0])
        assert env["type"] == "chart-emission"
        assert env["session_id"] == state.session_id
        assert env["payload"]["envelope_type"] == "chart-emission"
        assert "vega_lite_spec" in env["payload"]
        # created_turn_id stamped from the current turn.
        assert env["payload"]["created_turn_id"] == turn_id

        # 2. Persistence append called: update-one $push to sessions, keyed by
        #    the active case id.
        assert len(fake_mcp.calls) == 1
        name, args = fake_mcp.calls[0]
        assert name == "update-one"
        assert args["collection"] == "sessions"
        assert args["filter"]["_id"] == case_id
        assert "$push" in args["update"]
        assert "charts" in args["update"]["$push"]
        pushed = args["update"]["$push"]["charts"]
        assert pushed["payload"]["chart_id"] == payload["chart_id"]
        assert pushed["schema_version"] == "v1"

    async def test_persist_keyed_by_session_when_no_case(self, tmp_path, monkeypatch):
        import trid3nt_server.server as server
        from trid3nt_server.persistence import Persistence

        fake_mcp = _FakeMCP()
        persistence = Persistence(fake_mcp)
        monkeypatch.setattr(server, "get_persistence", lambda: persistence)

        state = await self._make_state(case_id=None)
        arr = np.arange(16, dtype=np.float32).reshape(4, 4)
        path = _make_raster(tmp_path, arr)
        payload = generate_histogram(layer_uri=path)

        ws = _FakeWS()
        await server._maybe_emit_chart(ws, state, payload)

        name, args = fake_mcp.calls[0]
        assert args["filter"]["_id"] == state.session_id

    async def test_no_persistence_singleton_is_safe(self, tmp_path, monkeypatch):
        """When Persistence is unbound, emit still works, persistence skipped."""
        import trid3nt_server.server as server

        monkeypatch.setattr(server, "get_persistence", lambda: None)
        state = await self._make_state()
        arr = np.arange(16, dtype=np.float32).reshape(4, 4)
        path = _make_raster(tmp_path, arr)
        payload = generate_histogram(layer_uri=path)

        ws = _FakeWS()
        # Must not raise.
        await server._maybe_emit_chart(ws, state, payload)
        assert len(ws.sent) == 1

    async def test_persistence_failure_does_not_raise(self, tmp_path, monkeypatch):
        import trid3nt_server.server as server
        from trid3nt_server.persistence import Persistence

        class _BrokenMCP:
            async def call_tool(self, name, arguments=None):
                raise RuntimeError("mongo down")

        persistence = Persistence(_BrokenMCP())
        monkeypatch.setattr(server, "get_persistence", lambda: persistence)

        state = await self._make_state()
        arr = np.arange(16, dtype=np.float32).reshape(4, 4)
        path = _make_raster(tmp_path, arr)
        payload = generate_histogram(layer_uri=path)

        ws = _FakeWS()
        # Persistence failure is swallowed — emission still happened.
        await server._maybe_emit_chart(ws, state, payload)
        assert len(ws.sent) == 1


# ---------------------------------------------------------------------------
# Dispatch-site detection: chart payload triggers emission, ordinary does not
# ---------------------------------------------------------------------------


def test_dispatch_detection_signal(tmp_path):
    """The server dispatch loop uses is_chart_emission_result as its trigger.

    Confirm a chart tool's result trips it while an ordinary tool result (the
    analytical_qa summary dict) does not — this is the exact branch condition
    in _stream_gemini_reply.
    """
    from trid3nt_server.tools.analytical_qa import summarize_layer_statistics
    from trid3nt_server.tools import cache as cache_module

    # Bypass GCS cache for the analytical tool.
    class _FakeResult:
        def __init__(self, data):
            self.data = data
            self.cache_hit = False

    orig = cache_module.read_through
    cache_module.read_through = lambda *, metadata, params, ext, fetch_fn, bucket, storage_client, source_id, **_kw: _FakeResult(fetch_fn())
    try:
        arr = np.arange(16, dtype=np.float32).reshape(4, 4)
        path = _make_raster(tmp_path, arr)

        chart = generate_histogram(layer_uri=path)
        stats = summarize_layer_statistics(layer_uri=path)

        assert is_chart_emission_result(chart) is True
        assert is_chart_emission_result(stats) is False
    finally:
        cache_module.read_through = orig


# ---------------------------------------------------------------------------
# Registration + categories
# ---------------------------------------------------------------------------


class TestRegistration:
    _TOOLS = (
        "generate_histogram",
        "generate_choropleth_legend",
        "generate_time_series",
        "generate_damage_distribution",
    )

    def test_all_in_tool_registry(self):
        from trid3nt_server.tools import TOOL_REGISTRY

        for name in self._TOOLS:
            assert name in TOOL_REGISTRY, name

    def test_metadata(self):
        from trid3nt_server.tools import TOOL_REGISTRY

        for name in self._TOOLS:
            m = TOOL_REGISTRY[name].metadata
            assert m.ttl_class == "dynamic-1h"
            assert m.source_class == "chart_tools"
            assert m.read_only_hint is True

    def test_category_membership(self):
        from trid3nt_server.categories import PRIMARY_CATEGORY

        for name in self._TOOLS:
            assert PRIMARY_CATEGORY[name] == "geographic_primitives"
