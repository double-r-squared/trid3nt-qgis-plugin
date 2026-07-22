"""Tests for the ``spatial_query`` tool (DuckDB spatial-query fold, Phase B).

Replaces ``test_analytical_qa.py``: the three fixed-shape analytical Q&A tools
(``summarize_layer_statistics`` / ``count_features_above_threshold`` /
``aggregate_property_within_zone``) folded into ONE read-only SQL surface.
The behavioral coverage of the old suite is rewritten here as spatial_query
SQL against small LOCAL geojson fixtures - fully OFFLINE (no MinIO, no
network; the DuckDB spatial extension is loaded from the local
~/.duckdb/extensions cache).

Coverage:
- Registration + metadata + category + hot-set floor slot.
- Registry fold proof: the three folded tools are GONE; count is 190.
- SQL happy paths: summary stats / count-above-threshold / aggregate-within-
  zone / per-zone spatial join (the old trio's behaviors, as SQL).
- Read-only guard: writes / multi-statement / INSTALL / COPY / ATTACH / SET
  rejected; string-literal keyword smuggling NOT falsely rejected.
- Bad SQL -> typed SQL_ERROR carrying the DuckDB message verbatim (retryable).
- Raster ref -> typed RASTER_UNSUPPORTED naming the playground alternative.
- Bad alias / bad ref -> typed BAD_LAYER_REF.
- Row cap + truncation flag.
- Result materialization ("show me all X in Y" paints): geometry-bearing
  results return a SpatialQueryLayerURI (FlatGeobuf written + vector-tool
  envelope + compact row summary); tabular/empty results unchanged;
  result_name honored; runs-bucket upload key; geopandas fallback; honest
  degrade-to-tabular on materialization failure.
- ADR-0014 handle resolution: SessionUriRegistry.resolve_params resolves
  layer_refs dict VALUES (handle -> uri) for tool_name="spatial_query".
- Retrieval (hard rule - corpus before acceptance): discover_dataset returns
  spatial_query top-5 for folded-tool phrasings; retrieve_visible_tools
  always carries it (hot-set floor).

ASCII only.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from trid3nt_server.tools.processing.spatial_query import (
    SpatialQueryError,
    SpatialQueryLayerURI,
    spatial_query,
)
from trid3nt_server.tools.processing import spatial_query as sq_module


# ---------------------------------------------------------------------------
# Local geojson fixtures (offline; no MinIO needed)
# ---------------------------------------------------------------------------


def _write_geojson_points(tmp_path: Path, records: list[dict]) -> str:
    """Point FeatureCollection; each record has x, y + attribute keys."""
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
    path = str(tmp_path / "test_points.geojson")
    with open(path, "w") as f:
        json.dump(fc, f)
    return path


def _write_geojson_polygons(
    tmp_path: Path, polys: list[list[list[float]]], name_suffix: str = ""
) -> str:
    """Polygon FeatureCollection; polys is a list of open coordinate rings."""
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring + [ring[0]]]},
            "properties": {"zone_id": i},
        }
        for i, ring in enumerate(polys)
    ]
    fc = {"type": "FeatureCollection", "features": features}
    path = str(tmp_path / f"test_polys{name_suffix}.geojson")
    with open(path, "w") as f:
        json.dump(fc, f)
    return path


@pytest.fixture
def points_path(tmp_path: Path) -> str:
    """4 points: 3 inside the unit square + 1 far outside; numeric 'cost'."""
    return _write_geojson_points(
        tmp_path,
        [
            {"x": 0.2, "y": 0.2, "cost": 10.0, "label": "a"},
            {"x": 0.5, "y": 0.5, "cost": 20.0, "label": "b"},
            {"x": 0.8, "y": 0.8, "cost": 30.0, "label": "c"},
            {"x": 2.0, "y": 2.0, "cost": 999.0, "label": "d"},
        ],
    )


@pytest.fixture
def zone_path(tmp_path: Path) -> str:
    """One zone polygon covering the unit square [0,1]x[0,1]."""
    return _write_geojson_polygons(
        tmp_path, [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]]
    )


# ---------------------------------------------------------------------------
# Registration, metadata, category, fold proof
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_registered_with_metadata(self):
        from trid3nt_server.tools import TOOL_REGISTRY

        assert "spatial_query" in TOOL_REGISTRY
        entry = TOOL_REGISTRY["spatial_query"]
        assert entry.metadata.ttl_class == "live-no-cache"
        assert entry.metadata.cacheable is False
        assert entry.metadata.read_only_hint is True
        assert entry.metadata.destructive_hint is False

    def test_folded_tools_are_gone(self):
        """The fold removes the three analytical Q&A registrations."""
        from trid3nt_server.tools import TOOL_REGISTRY

        for name in (
            "summarize_layer_statistics",
            "count_features_above_threshold",
            "aggregate_property_within_zone",
        ):
            assert name not in TOOL_REGISTRY, f"{name} should be folded away"

    def test_registry_count_is_190(self):
        """192 (pre-fold plain-import surface) - 3 folded + 1 spatial_query."""
        from trid3nt_server.tools import TOOL_REGISTRY

        assert len(TOOL_REGISTRY) == 190

    def test_primary_category(self):
        from trid3nt_server.categories import PRIMARY_CATEGORY

        assert PRIMARY_CATEGORY["spatial_query"] == "geographic_primitives"

    def test_hot_set_floor_slot(self):
        """spatial_query inherits the layer-analysis floor slot the folded
        summarize_layer_statistics held."""
        from trid3nt_server.categories import HOT_SET_TOOLS

        assert "spatial_query" in HOT_SET_TOOLS
        assert "summarize_layer_statistics" not in HOT_SET_TOOLS


# ---------------------------------------------------------------------------
# SQL happy paths - the folded trio's behavioral coverage, as SQL
# ---------------------------------------------------------------------------


class TestSqlHappyPaths:
    def test_summary_stats(self, points_path):
        """summarize_layer_statistics equivalent: count/min/max/mean/sum."""
        result = spatial_query(
            sql=(
                "SELECT count(*) AS n, min(cost) AS mn, max(cost) AS mx, "
                "avg(cost) AS mean, sum(cost) AS total FROM pts"
            ),
            layer_refs={"pts": points_path},
        )
        assert result["columns"] == ["n", "mn", "mx", "mean", "total"]
        assert result["row_count"] == 1
        n, mn, mx, mean, total = result["rows"][0]
        assert n == 4
        assert mn == pytest.approx(10.0)
        assert mx == pytest.approx(999.0)
        assert mean == pytest.approx(1059.0 / 4)
        assert total == pytest.approx(1059.0)
        assert result["truncated"] is False
        assert result["layer_views"] == {"pts": points_path}
        assert "computed_at" in result
        assert "1 row" in result["summary"]

    def test_count_above_threshold(self, points_path):
        """count_features_above_threshold equivalent (inclusive >=)."""
        result = spatial_query(
            sql="SELECT count(*) AS n FROM pts WHERE cost >= 20.0",
            layer_refs={"pts": points_path},
        )
        assert result["rows"][0][0] == 3  # 20, 30, 999

    def test_count_zero_is_not_an_error(self, points_path):
        result = spatial_query(
            sql="SELECT count(*) AS n FROM pts WHERE cost >= 1e9",
            layer_refs={"pts": points_path},
        )
        assert result["rows"][0][0] == 0

    def test_aggregate_within_zone(self, points_path, zone_path):
        """aggregate_property_within_zone equivalent: centroid-in-zone sum/
        mean/max in ONE query (the fold's expressiveness win)."""
        result = spatial_query(
            sql=(
                "SELECT sum(p.cost) AS s, avg(p.cost) AS m, max(p.cost) AS mx, "
                "count(*) AS n FROM pts p, zones z "
                "WHERE ST_Within(ST_Centroid(p.geom), z.geom)"
            ),
            layer_refs={"pts": points_path, "zones": zone_path},
        )
        s, m, mx, n = result["rows"][0]
        assert n == 3  # the far-away point is excluded
        assert s == pytest.approx(60.0)
        assert m == pytest.approx(20.0)
        assert mx == pytest.approx(30.0)

    def test_spatial_join_group_by(self, points_path, tmp_path):
        """Per-zone group-by spatial join - NOT expressible by the old trio."""
        zones = _write_geojson_polygons(
            tmp_path,
            [
                [[0.0, 0.0], [0.6, 0.0], [0.6, 1.0], [0.0, 1.0]],  # zone 0: 2 pts
                [[0.6, 0.0], [1.0, 0.0], [1.0, 1.0], [0.6, 1.0]],  # zone 1: 1 pt
            ],
            name_suffix="_two",
        )
        result = spatial_query(
            sql=(
                "SELECT z.zone_id, count(*) AS n FROM pts p JOIN zones z "
                "ON ST_Within(p.geom, z.geom) GROUP BY z.zone_id ORDER BY z.zone_id"
            ),
            layer_refs={"pts": points_path, "zones": zones},
        )
        assert result["rows"] == [[0, 2], [1, 1]]

    def test_geometry_column_as_wkt(self, points_path):
        result = spatial_query(
            sql="SELECT ST_AsText(geom) AS wkt FROM pts ORDER BY cost LIMIT 1",
            layer_refs={"pts": points_path},
        )
        assert result["rows"][0][0].startswith("POINT")

    def test_no_layer_refs_scalar_select(self):
        """A pure scalar SELECT needs no layers (layer_refs optional)."""
        result = spatial_query(sql="SELECT 1 + 1 AS two")
        assert result["rows"] == [[2]]
        assert result["layer_views"] == {}

    def test_row_cap_truncation(self, points_path, monkeypatch):
        monkeypatch.setattr(sq_module, "_ROW_CAP", 2)
        result = spatial_query(
            sql="SELECT cost FROM pts ORDER BY cost",
            layer_refs={"pts": points_path},
        )
        assert result["row_count"] == 2
        assert result["truncated"] is True
        assert result["row_cap"] == 2
        assert "TRUNCATED" in result["summary"]


# ---------------------------------------------------------------------------
# Read-only guard
# ---------------------------------------------------------------------------


class TestReadOnlyGuard:
    @pytest.mark.parametrize(
        "bad_sql",
        [
            "DROP TABLE pts",
            "INSERT INTO pts VALUES (1)",
            "UPDATE pts SET cost = 0",
            "DELETE FROM pts",
            "CREATE TABLE t AS SELECT 1",
            "COPY pts TO 'out.csv'",
            "ATTACH 'other.db'",
            "INSTALL httpfs",
            "LOAD httpfs",
            "SET s3_endpoint='evil'",
            "PRAGMA database_list",
            "SELECT 1; SELECT 2",  # multi-statement
            "SELECT 1; DROP TABLE pts;",
            "WITH x AS (SELECT 1) INSERT INTO pts SELECT * FROM x",
            "",
            "   ",
        ],
    )
    def test_rejects_non_select(self, bad_sql, points_path):
        with pytest.raises(SpatialQueryError) as exc:
            spatial_query(sql=bad_sql, layer_refs={"pts": points_path})
        assert exc.value.error_code == "SQL_NOT_ALLOWED"

    def test_guard_runs_before_any_layer_io(self):
        """The guard rejects BEFORE touching layer refs (no fixture needed)."""
        with pytest.raises(SpatialQueryError) as exc:
            spatial_query(
                sql="DROP TABLE x", layer_refs={"x": "s3://nonexistent/x.fgb"}
            )
        assert exc.value.error_code == "SQL_NOT_ALLOWED"

    def test_keyword_in_string_literal_is_allowed(self, points_path):
        result = spatial_query(
            sql="SELECT 'drop table; create copy' AS s, count(*) AS n FROM pts",
            layer_refs={"pts": points_path},
        )
        assert result["rows"][0][1] == 4

    def test_keyword_in_comment_is_ignored(self, points_path):
        result = spatial_query(
            sql="SELECT count(*) AS n FROM pts -- drop table pts",
            layer_refs={"pts": points_path},
        )
        assert result["rows"][0][0] == 4

    def test_with_cte_select_is_allowed(self, points_path):
        result = spatial_query(
            sql=(
                "WITH expensive AS (SELECT * FROM pts WHERE cost >= 20) "
                "SELECT count(*) AS n FROM expensive"
            ),
            layer_refs={"pts": points_path},
        )
        assert result["rows"][0][0] == 3

    def test_trailing_semicolon_is_allowed(self, points_path):
        result = spatial_query(
            sql="SELECT count(*) AS n FROM pts;",
            layer_refs={"pts": points_path},
        )
        assert result["rows"][0][0] == 4

    def test_offset_is_not_confused_with_set(self, points_path):
        result = spatial_query(
            sql="SELECT cost FROM pts ORDER BY cost LIMIT 1 OFFSET 1",
            layer_refs={"pts": points_path},
        )
        assert result["rows"][0][0] == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class TestTypedErrors:
    def test_bad_sql_carries_duckdb_message_verbatim(self, points_path):
        with pytest.raises(SpatialQueryError) as exc:
            spatial_query(
                sql="SELECT nonexistent_col FROM pts",
                layer_refs={"pts": points_path},
            )
        assert exc.value.error_code == "SQL_ERROR"
        assert exc.value.retryable is True
        # The DuckDB binder message names the missing column - the retry
        # loop's self-correction signal.
        assert "nonexistent_col" in str(exc.value)

    def test_unknown_view_is_sql_error(self, points_path):
        with pytest.raises(SpatialQueryError) as exc:
            spatial_query(
                sql="SELECT * FROM not_a_view",
                layer_refs={"pts": points_path},
            )
        assert exc.value.error_code == "SQL_ERROR"
        assert "not_a_view" in str(exc.value)

    def test_raster_ref_rejected_with_playground_pointer(self):
        with pytest.raises(SpatialQueryError) as exc:
            spatial_query(
                sql="SELECT 1",
                layer_refs={"flood": "s3://bucket/depth_peak.tif"},
            )
        assert exc.value.error_code == "RASTER_UNSUPPORTED"
        msg = str(exc.value)
        assert "code_exec_request" in msg
        assert "compute_zonal_statistics" in msg

    def test_bad_alias_rejected(self, points_path):
        with pytest.raises(SpatialQueryError) as exc:
            spatial_query(
                sql="SELECT 1",
                layer_refs={"bad-alias!": points_path},
            )
        assert exc.value.error_code == "BAD_LAYER_REF"

    def test_non_string_ref_rejected(self):
        with pytest.raises(SpatialQueryError) as exc:
            spatial_query(sql="SELECT 1", layer_refs={"x": 42})
        assert exc.value.error_code == "BAD_LAYER_REF"

    def test_unsupported_scheme_rejected(self):
        with pytest.raises(SpatialQueryError) as exc:
            spatial_query(sql="SELECT 1", layer_refs={"x": "gs://bucket/x.fgb"})
        assert exc.value.error_code == "BAD_LAYER_REF"

    def test_missing_local_file_is_layer_open_failed(self, tmp_path):
        with pytest.raises(SpatialQueryError) as exc:
            spatial_query(
                sql="SELECT 1",
                layer_refs={"x": str(tmp_path / "does_not_exist.geojson")},
            )
        assert exc.value.error_code == "LAYER_OPEN_FAILED"


# ---------------------------------------------------------------------------
# s3 staging fallback (offline: fake the shared boto3 reader, force httpfs off)
# ---------------------------------------------------------------------------


class TestS3StagingFallback:
    def test_s3_ref_stages_via_shared_reader(self, points_path, monkeypatch):
        """With httpfs unavailable, an s3:// ref stages through the shared
        boto3 reader (cache.read_object_bytes_s3) and still queries."""
        from trid3nt_server.tools import cache as cache_module

        payload = Path(points_path).read_bytes()
        calls: list[str] = []

        def _fake_reader(uri: str) -> bytes:
            calls.append(uri)
            return payload

        monkeypatch.setattr(cache_module, "read_object_bytes_s3", _fake_reader)
        monkeypatch.setattr(sq_module, "_try_configure_httpfs", lambda con: False)

        result = spatial_query(
            sql="SELECT count(*) AS n FROM pts",
            layer_refs={"pts": "s3://bkt/points.geojson"},
        )
        assert calls == ["s3://bkt/points.geojson"]
        assert result["rows"][0][0] == 4
        # Provenance carries the ORIGINAL s3 uri, not the staged temp path.
        assert result["layer_views"] == {"pts": "s3://bkt/points.geojson"}

    def test_s3_reader_failure_is_download_failed(self, monkeypatch):
        from trid3nt_server.tools import cache as cache_module

        def _boom(uri: str) -> bytes:
            raise RuntimeError("AccessDenied")

        monkeypatch.setattr(cache_module, "read_object_bytes_s3", _boom)
        monkeypatch.setattr(sq_module, "_try_configure_httpfs", lambda con: False)

        with pytest.raises(SpatialQueryError) as exc:
            spatial_query(
                sql="SELECT 1", layer_refs={"pts": "s3://bkt/points.geojson"}
            )
        assert exc.value.error_code == "DOWNLOAD_FAILED"
        assert exc.value.retryable is True


# ---------------------------------------------------------------------------
# Result materialization ("show me all X in Y" paints, not just tabulates)
# ---------------------------------------------------------------------------


class TestResultMaterialization:
    def test_geometry_select_materializes_layer(self, points_path, tmp_path):
        """A SELECT carrying the geometry column returns a painted-layer
        envelope: FlatGeobuf written, vector-tool LayerURI conventions,
        compact row summary carried alongside."""
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        result = spatial_query(
            sql="SELECT * FROM pts WHERE cost <= 30",
            layer_refs={"pts": points_path},
            _output_dir=str(out_dir),
        )
        from trid3nt_contracts.execution import LayerURI

        assert isinstance(result, SpatialQueryLayerURI)
        # The emit-seam gate is isinstance(result, LayerURI) - must hold.
        assert isinstance(result, LayerURI)
        # Vector-tool envelope conventions (clip_vector_to_polygon shape).
        assert result.layer_type == "vector"
        assert result.role == "primary"
        assert result.style_preset == sq_module._RESULT_STYLE_PRESET
        assert result.layer_id.startswith("spatial-query-")
        assert result.uri.endswith(".fgb")
        assert Path(result.uri).is_file()
        # The FGB is a readable FlatGeobuf carrying the full result set.
        gpd = pytest.importorskip("geopandas")
        gdf = gpd.read_file(result.uri, engine="pyogrio")
        assert len(gdf) == 3
        assert str(gdf.crs) == "EPSG:4326"
        assert "cost" in gdf.columns
        # Compact tabular carry-over (row summary still returned).
        assert result.feature_count == 3
        assert result.row_count == 3
        assert result.truncated is False
        assert "cost" in result.columns
        assert len(result.preview_rows) == 3
        assert "vector layer" in result.summary
        assert result.layer_views == {"pts": points_path}
        # Default display name + bbox of the three in-square points.
        assert result.name == "Query result (3 features)"
        assert result.bbox == pytest.approx((0.2, 0.2, 0.8, 0.8))

    def test_result_name_honored(self, points_path, tmp_path):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        result = spatial_query(
            sql="SELECT * FROM pts WHERE cost >= 20",
            layer_refs={"pts": points_path},
            result_name="Buildings above 1 m",
            _output_dir=str(out_dir),
        )
        assert isinstance(result, SpatialQueryLayerURI)
        assert result.name == "Buildings above 1 m"
        assert "'Buildings above 1 m'" in result.summary

    def test_tabular_path_unchanged(self, points_path, tmp_path):
        """Geometry-less results keep the exact v1 dict contract."""
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        result = spatial_query(
            sql="SELECT count(*) AS n, sum(cost) AS total FROM pts",
            layer_refs={"pts": points_path},
            _output_dir=str(out_dir),
        )
        assert isinstance(result, dict)
        assert set(result.keys()) == {
            "columns",
            "rows",
            "row_count",
            "truncated",
            "row_cap",
            "summary",
            "layer_views",
            "computed_at",
        }
        assert result["rows"] == [[4, pytest.approx(1059.0)]]
        # No stray FGB was written for a tabular result.
        assert list(out_dir.iterdir()) == []

    def test_geometryless_rows_stay_tabular(self, points_path, tmp_path):
        """Multi-row results WITHOUT a geometry column stay tabular."""
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        result = spatial_query(
            sql="SELECT cost, label FROM pts ORDER BY cost",
            layer_refs={"pts": points_path},
            _output_dir=str(out_dir),
        )
        assert isinstance(result, dict)
        assert result["row_count"] == 4
        assert list(out_dir.iterdir()) == []

    def test_empty_result_no_materialize(self, points_path, tmp_path):
        """A geometry SELECT with zero rows returns the tabular dict and
        writes nothing."""
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        result = spatial_query(
            sql="SELECT * FROM pts WHERE cost > 1e9",
            layer_refs={"pts": points_path},
            _output_dir=str(out_dir),
        )
        assert isinstance(result, dict)
        assert result["row_count"] == 0
        assert "0 rows" in result["summary"]
        assert list(out_dir.iterdir()) == []

    def test_runs_bucket_upload_key(self, fake_s3, monkeypatch, points_path):
        """Without _output_dir the FGB uploads to
        s3://trid3nt-runs/spatial_query/<ulid>.fgb via the solver's shared
        boto3 seam (in-memory S3 double - no network)."""
        from trid3nt_server.tools.simulation import solver

        monkeypatch.setattr(solver, "_S3_CLIENT", None)
        monkeypatch.setattr(solver, "_RUNS_BUCKET", None)
        monkeypatch.delenv("TRID3NT_RUNS_BUCKET", raising=False)

        result = spatial_query(
            sql="SELECT * FROM pts WHERE cost <= 30",
            layer_refs={"pts": points_path},
        )
        assert isinstance(result, SpatialQueryLayerURI)
        assert fake_s3.last_put is not None
        key = fake_s3.last_put["Key"]
        assert re.fullmatch(r"spatial_query/[0-9A-HJKMNP-TV-Z]{26}\.fgb", key)
        assert fake_s3.last_put["Bucket"] == "trid3nt-runs"
        assert result.uri == f"s3://trid3nt-runs/{key}"
        assert len(fake_s3.store[key]) > 0

    def test_gdal_copy_failure_falls_back_to_geopandas(
        self, monkeypatch, points_path, tmp_path
    ):
        """When the duckdb-gdal COPY is unavailable the geopandas/pyogrio
        export ships the same FlatGeobuf."""
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        def _boom(con, body, out_path):
            raise RuntimeError("gdal COPY unavailable in this duckdb build")

        monkeypatch.setattr(sq_module, "_copy_via_duckdb_gdal", _boom)
        result = spatial_query(
            sql="SELECT * FROM pts WHERE cost <= 30",
            layer_refs={"pts": points_path},
            _output_dir=str(out_dir),
        )
        assert isinstance(result, SpatialQueryLayerURI)
        gpd = pytest.importorskip("geopandas")
        gdf = gpd.read_file(result.uri, engine="pyogrio")
        assert len(gdf) == 3
        assert str(gdf.crs) == "EPSG:4326"
        assert result.feature_count == 3

    def test_materialize_failure_degrades_to_tabular(
        self, monkeypatch, points_path, tmp_path
    ):
        """A materialization failure never loses the query result: the
        tabular dict comes back with an honest note in the summary."""

        def _boom(con, body, geom_cols, out_path):
            raise RuntimeError("disk full")

        monkeypatch.setattr(sq_module, "_export_fgb", _boom)
        result = spatial_query(
            sql="SELECT * FROM pts WHERE cost <= 30",
            layer_refs={"pts": points_path},
            _output_dir=str(tmp_path),
        )
        assert isinstance(result, dict)
        assert result["row_count"] == 3
        assert "materialization FAILED" in result["summary"]
        assert "disk full" in result["summary"]
        assert "no layer was painted" in result["summary"]

    def test_truncated_result_layer_carries_full_set(
        self, monkeypatch, points_path, tmp_path
    ):
        """The wire rows cap at _ROW_CAP but the LAYER carries the FULL
        result set (the cap is a wire-size rail, not a data cut)."""
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.setattr(sq_module, "_ROW_CAP", 2)
        result = spatial_query(
            sql="SELECT * FROM pts",
            layer_refs={"pts": points_path},
            _output_dir=str(out_dir),
        )
        assert isinstance(result, SpatialQueryLayerURI)
        assert result.truncated is True
        assert result.row_count == 2
        assert result.row_cap == 2
        assert result.feature_count == 4
        gpd = pytest.importorskip("geopandas")
        gdf = gpd.read_file(result.uri, engine="pyogrio")
        assert len(gdf) == 4

    def test_result_registers_layer_handle(self, points_path, tmp_path):
        """ADR-0014: register_tool_result on the returned model mints the
        layer_id <-> uri pair so the NEXT spatial_query can reference the
        result layer by handle."""
        from trid3nt_server.uri_registry import SessionUriRegistry

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        result = spatial_query(
            sql="SELECT * FROM pts WHERE cost <= 30",
            layer_refs={"pts": points_path},
            _output_dir=str(out_dir),
        )
        reg = SessionUriRegistry(session_id="test-sq-materialize")
        reg.register_tool_result("spatial_query", result)
        resolved = reg.resolve_params(
            "spatial_query",
            {"sql": "SELECT 1", "layer_refs": {"r": result.layer_id}},
        )
        assert resolved["layer_refs"] == {"r": result.uri}


# ---------------------------------------------------------------------------
# ADR-0014 handle resolution (the dispatch seam the param name inherits)
# ---------------------------------------------------------------------------


class TestHandleResolution:
    def test_layer_refs_values_resolve_via_registry(self, points_path):
        """server.py dispatch calls uri_registry.resolve_params(tool, params)
        before the tool body; layer_refs is in NESTED_REF_PARAMS so every
        string VALUE resolves handle -> uri. Prove the seam end-to-end with a
        real SessionUriRegistry + the real tool."""
        from trid3nt_server.uri_registry import SessionUriRegistry

        reg = SessionUriRegistry(session_id="test-spatial-query")
        reg.record("nsi-buildings-layer", uri=points_path, tool_name="fetch_usace_nsi")

        params = {
            "sql": "SELECT count(*) AS n FROM pts",
            "layer_refs": {"pts": "nsi-buildings-layer"},
        }
        resolved = reg.resolve_params("spatial_query", params)
        assert resolved["layer_refs"] == {"pts": points_path}
        # sql passes through untouched.
        assert resolved["sql"] == params["sql"]

        result = spatial_query(**resolved)
        assert result["rows"][0][0] == 4

    def test_short_handle_resolves(self, points_path):
        """The ADR-0014 L<n> short handle (what the LLM actually sees)."""
        from trid3nt_server.uri_registry import SessionUriRegistry

        reg = SessionUriRegistry(session_id="test-spatial-query-short")
        reg.record("layer-a", uri=points_path)
        short = reg.short_for_uri(points_path)
        assert short == "L1"

        resolved = reg.resolve_params(
            "spatial_query",
            {"sql": "SELECT 1", "layer_refs": {"pts": short}},
        )
        assert resolved["layer_refs"] == {"pts": points_path}

    def test_unknown_handle_raises_typed_resolution_error(self):
        from trid3nt_server.uri_registry import (
            SessionUriRegistry,
            UriResolutionError,
        )

        reg = SessionUriRegistry(session_id="test-spatial-query-unknown")
        with pytest.raises(UriResolutionError):
            reg.resolve_params(
                "spatial_query",
                {"sql": "SELECT 1", "layer_refs": {"pts": "s3://never/registered.fgb"}},
            )


# ---------------------------------------------------------------------------
# Retrieval (hard rule: corpus + model-free retrieval check before acceptance)
# ---------------------------------------------------------------------------


#: One folded-tool phrasing per ask family; each must rank spatial_query
#: top-5 in discover_dataset (proven live at fold time, hashed backend).
_RETRIEVAL_PHRASINGS = [
    # summary stats of a layer (summarize_layer_statistics lineage)
    "give me summary statistics for this layer, min max mean and sum",
    # count features above a threshold (count_features_above_threshold lineage)
    "count how many parcels exceed a replacement value of 500000",
    # aggregate within zones (aggregate_property_within_zone lineage)
    "sum the structure replacement value for everything within the county boundary",
    # spatial join (new expressiveness)
    "spatial join the points to the zones and count the features per zone",
]


@pytest.fixture(scope="module")
def fresh_index():
    import trid3nt_server.tools.discovery.discover_dataset as dd

    dd._reset_index_for_tests()
    dd._get_index()
    yield
    dd._reset_index_for_tests()


class TestRetrieval:
    @pytest.mark.parametrize("phrase", _RETRIEVAL_PHRASINGS)
    def test_discover_dataset_top5(self, fresh_index, phrase):
        import trid3nt_server.tools.discovery.discover_dataset as dd

        res = asyncio.run(dd.discover_dataset(query=phrase, top_k=5))
        names = [r["tool_name"] for r in res["results"]]
        assert "spatial_query" in names, f"{phrase!r} -> {names}"

    def test_retrieve_visible_tools_always_carries_spatial_query(self, fresh_index):
        """Hot-set floor membership: the model-free visibility check."""
        from trid3nt_server.tools.discovery.tool_retrieval import (
            retrieve_visible_tools,
        )

        for phrase in _RETRIEVAL_PHRASINGS[:3]:
            assert "spatial_query" in retrieve_visible_tools(phrase, None, 8)

    def test_corpus_has_spatial_query_and_not_folded_tools(self):
        import trid3nt_server.tools.discovery.discover_dataset as dd

        corpus = dd._load_corpus()
        assert "spatial_query" in corpus
        assert len(corpus["spatial_query"]) >= 8
        for name in (
            "summarize_layer_statistics",
            "count_features_above_threshold",
            "aggregate_property_within_zone",
        ):
            assert name not in corpus
