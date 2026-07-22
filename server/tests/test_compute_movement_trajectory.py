"""Unit tests for ``compute_movement_trajectory`` atomic tool (FR-TA-2, FR-CE-8, FR-DC).

The tool turns a layer of timestamped track POINTS (one feature per telemetry
fix, e.g. ``fetch_movebank_tracks`` with ``geometry_type="point"``) into an
annotated LineString-segment trajectory + per-individual movement summary,
written to a FlatGeobuf and routed through the FR-DC-3 cache shim.

Coverage:
 1. ``test_registered`` — tool in TOOL_REGISTRY with the expected metadata
    (cacheable, static-30d, source_class="movement_trajectory").
 2. ``test_normalize_turn_angle`` — bearing-difference normalization to (-180, 180].
 3. ``test_segments_geodesic_correctness`` — on a known L-shaped synthetic track:
    step lengths, speeds, bearings, the 90-deg turn, path length, net
    displacement, and straightness all match hand-computed expectations.
 4. ``test_multi_individual_grouping`` — two individuals -> two independent
    trajectories; no segment spans two animals.
 5. ``test_layer_uri_shape`` — vector LayerURI (layer_type vector, style_preset,
    units m, bbox set) and the segment FlatGeobuf round-trips through the cache.
 6. ``test_honest_empty_single_point`` — < 2 points -> INSUFFICIENT_POINTS typed
    error (the honest-empty path; never an empty-success layer).
 7. ``test_no_timestamp_field_raises`` — a point layer with no timestamp column
    -> NO_TIMESTAMP_FIELD typed error.
 8. ``test_not_point_geometry_raises`` — a LineString layer -> NOT_POINT_GEOMETRY.
 9. ``test_input_validation`` — empty/non-string points_uri -> typed error.
10. ``test_cache_hit_skips_fetch`` — a second identical call hits the cache.
"""

from __future__ import annotations

import os
import tempfile

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, Point

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.compute_movement_trajectory import (
    MovementTrajectoryError,
    _normalize_turn_angle,
    compute_movement_trajectory,
    estimate_payload_mb,
)
from trid3nt_contracts.execution import LayerURI


# ---------------------------------------------------------------------------
# Synthetic point-FGB builders (shaped like fetch_movebank_tracks point output)
# ---------------------------------------------------------------------------


def _write_point_fgb(path: str, rows: list[dict], geoms: list) -> None:
    gdf = gpd.GeoDataFrame(pd.DataFrame(rows), geometry=geoms, crs="EPSG:4326")
    gdf.to_file(path, driver="FlatGeobuf", engine="pyogrio")


def _l_shaped_track_fgb(path: str) -> None:
    """An L-shaped track: ~850 m east x2, then ~1000 m north x2 (a 90-deg turn).

    Fixes are 10 minutes apart. Mirrors the geometry_type="point" schema of
    fetch_movebank_tracks (individual_id, timestamp, sensor_type_id, study_id).
    """
    coords = [
        (-105.0000, 40.0000),  # start
        (-104.9900, 40.0000),  # ~850 m east
        (-104.9800, 40.0000),  # ~850 m east (straight)
        (-104.9800, 40.0090),  # ~1000 m north (90-deg left turn)
        (-104.9800, 40.0180),  # ~1000 m north (straight)
    ]
    rows = []
    geoms = []
    for i, (lon, lat) in enumerate(coords):
        ts = f"2020-01-01 12:{10 * i:02d}:00.000"
        rows.append(
            {
                "individual_id": "synthetic-1",
                "timestamp": ts,
                "sensor_type_id": 653,
                "study_id": 99999,
            }
        )
        geoms.append(Point(lon, lat))
    _write_point_fgb(path, rows, geoms)


# ---------------------------------------------------------------------------
# In-memory S3 double (cache shim's only object store) — mirrors the
# test_compute_contours fixture so the tool's real read_through reads/writes the
# same store the test inspects.
# ---------------------------------------------------------------------------


class _S3Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeStorageClient:
    _active: "FakeStorageClient | None" = None

    def __new__(cls) -> "FakeStorageClient":
        if cls._active is not None:
            return cls._active
        return super().__new__(cls)

    def __init__(self) -> None:
        if getattr(self, "_init", False):
            return
        self._init = True
        self.store: dict[str, bytes] = {}
        self.put_count = 0

    def get_object(self, *, Bucket, Key):
        from botocore.exceptions import ClientError

        try:
            data = self.store[Key]
        except KeyError:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            )
        return {"Body": _S3Body(data)}

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        data = Body.read() if hasattr(Body, "read") else Body
        self.store[Key] = data
        self.put_count += 1
        return {}


@pytest.fixture(autouse=True)
def _route_cache_to_inmemory_s3(monkeypatch):
    import boto3

    FakeStorageClient._active = None
    client = FakeStorageClient()
    FakeStorageClient._active = client

    def _factory(service_name, *a, **k):
        assert service_name == "s3"
        return client

    monkeypatch.setattr(boto3, "client", _factory)
    try:
        yield client
    finally:
        FakeStorageClient._active = None


def _read_layer_segments(layer: LayerURI, store_client: FakeStorageClient) -> gpd.GeoDataFrame:
    """Read the segment FlatGeobuf back from the in-memory S3 store."""
    assert layer.uri is not None and layer.uri.startswith("s3://")
    key = layer.uri.split("/", 3)[3]  # s3://<bucket>/<key>
    data = store_client.store[key]
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(data)
        p = f.name
    try:
        return gpd.read_file(p)
    finally:
        try:
            os.unlink(p)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Test 1 — registration
# ---------------------------------------------------------------------------


def test_registered():
    assert "compute_movement_trajectory" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["compute_movement_trajectory"]
    assert entry.metadata.cacheable is True
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "movement_trajectory"
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


# ---------------------------------------------------------------------------
# Test 2 — turn-angle normalization
# ---------------------------------------------------------------------------


def test_normalize_turn_angle():
    assert _normalize_turn_angle(0.0) == 0.0
    assert _normalize_turn_angle(90.0) == 90.0
    assert _normalize_turn_angle(-90.0) == -90.0
    # A change of +270 is really a -90 turn.
    assert _normalize_turn_angle(270.0) == -90.0
    # A change of -270 is really a +90 turn.
    assert _normalize_turn_angle(-270.0) == 90.0
    # +/-180 both map to 180 (a complete reversal).
    assert _normalize_turn_angle(180.0) == 180.0
    assert _normalize_turn_angle(-180.0) == 180.0


# ---------------------------------------------------------------------------
# Test 3 — geodesic metric correctness on a known L-shaped track
# ---------------------------------------------------------------------------


def test_segments_geodesic_correctness():
    with tempfile.TemporaryDirectory() as td:
        pts = os.path.join(td, "track.fgb")
        _l_shaped_track_fgb(pts)
        layer = compute_movement_trajectory(pts, _bucket="test-bucket")

    store = FakeStorageClient()
    gdf = _read_layer_segments(layer, store)

    # 5 fixes -> 4 segments.
    assert len(gdf) == 4
    assert set(gdf.geom_type) == {"LineString"}

    by_idx = {int(r["seg_index"]): r for _, r in gdf.iterrows()}

    # East segments (~850 m); north segments (~1000 m). Tolerances are loose
    # because exact ellipsoidal distance differs slightly from the nominal.
    assert by_idx[0]["step_length_m"] == pytest.approx(854.0, abs=5.0)
    assert by_idx[2]["step_length_m"] == pytest.approx(999.0, abs=5.0)

    # Each fix is 600 s apart; speed = length / 600.
    assert by_idx[0]["duration_s"] == pytest.approx(600.0, abs=0.1)
    assert by_idx[0]["speed_mps"] == pytest.approx(by_idx[0]["step_length_m"] / 600.0, rel=1e-4)

    # Bearings: east ~ 90 deg, north ~ 0 deg.
    assert by_idx[0]["bearing_deg"] == pytest.approx(90.0, abs=0.5)
    assert by_idx[2]["bearing_deg"] == pytest.approx(0.0, abs=0.5)

    # The first segment of the individual has no prior bearing -> turn is null.
    assert pd.isna(by_idx[0]["turn_angle_deg"])
    # The east->north transition (seg 2) is a ~90-deg turn (-90 = left/CCW).
    assert by_idx[2]["turn_angle_deg"] == pytest.approx(-90.0, abs=0.5)
    # Two straight segments have ~0 turn.
    assert by_idx[1]["turn_angle_deg"] == pytest.approx(0.0, abs=0.5)
    assert by_idx[3]["turn_angle_deg"] == pytest.approx(0.0, abs=0.5)

    # Whole-track summary stamped on every segment.
    path_len = by_idx[0]["path_length_m"]
    net_disp = by_idx[0]["net_displacement_m"]
    straightness = by_idx[0]["straightness"]
    # path length ~ 2*854 + 2*999 ~= 3706 m.
    assert path_len == pytest.approx(3706.0, abs=10.0)
    # net displacement (start->end) < path length for an L-shape.
    assert net_disp < path_len
    # straightness = net/path, in (0, 1); ~0.71 for this L.
    assert straightness == pytest.approx(net_disp / path_len, rel=1e-4)
    assert 0.0 < straightness < 1.0


# ---------------------------------------------------------------------------
# Test 4 — multi-individual grouping
# ---------------------------------------------------------------------------


def test_multi_individual_grouping():
    rows = []
    geoms = []
    # Individual A: 3 fixes moving east.
    for i, lon in enumerate((-100.00, -99.99, -99.98)):
        rows.append({"individual_id": "A", "timestamp": f"2020-01-01 00:{i:02d}:00.000"})
        geoms.append(Point(lon, 40.0))
    # Individual B: 3 fixes moving north, far away.
    for i, lat in enumerate((10.00, 10.01, 10.02)):
        rows.append({"individual_id": "B", "timestamp": f"2020-01-01 00:{i:02d}:00.000"})
        geoms.append(Point(20.0, lat))

    with tempfile.TemporaryDirectory() as td:
        pts = os.path.join(td, "two.fgb")
        _write_point_fgb(pts, rows, geoms)
        layer = compute_movement_trajectory(pts, _bucket="test-bucket")

    gdf = _read_layer_segments(layer, FakeStorageClient())
    # 2 individuals x 2 segments each = 4 segments.
    assert len(gdf) == 4
    assert set(gdf["individual_id"]) == {"A", "B"}
    # A's segments live near (-100, 40); B's near (20, 10): no cross-animal segment.
    for _, r in gdf.iterrows():
        xs = [c[0] for c in r.geometry.coords]
        if r["individual_id"] == "A":
            assert all(x < -99 for x in xs)
        else:
            assert all(x > 19 for x in xs)


# ---------------------------------------------------------------------------
# Test 5 — LayerURI shape + cache round-trip
# ---------------------------------------------------------------------------


def test_layer_uri_shape():
    with tempfile.TemporaryDirectory() as td:
        pts = os.path.join(td, "track.fgb")
        _l_shaped_track_fgb(pts)
        layer = compute_movement_trajectory(pts, _bucket="test-bucket")

    assert isinstance(layer, LayerURI)
    assert layer.layer_type == "vector"
    assert layer.style_preset == "movement_trajectory"
    assert layer.role == "context"
    assert layer.units == "m"
    assert layer.uri is not None and layer.uri.endswith(".fgb")
    assert layer.bbox is not None and len(layer.bbox) == 4
    # bbox spans the synthetic track extent.
    minx, miny, maxx, maxy = layer.bbox
    assert minx <= -104.99 <= maxx
    assert miny <= 40.0 <= maxy


# ---------------------------------------------------------------------------
# Test 6 — honest empty (< 2 points)
# ---------------------------------------------------------------------------


def test_honest_empty_single_point():
    with tempfile.TemporaryDirectory() as td:
        pts = os.path.join(td, "one.fgb")
        _write_point_fgb(
            pts,
            [{"individual_id": "x", "timestamp": "2020-01-01 00:00:00.000"}],
            [Point(-100.0, 40.0)],
        )
        with pytest.raises(MovementTrajectoryError) as exc:
            compute_movement_trajectory(pts, _bucket="test-bucket")
    assert exc.value.error_code == "INSUFFICIENT_POINTS"


def test_honest_empty_all_individuals_short():
    # Two individuals, each with a single fix -> no segment for either.
    rows = [
        {"individual_id": "A", "timestamp": "2020-01-01 00:00:00.000"},
        {"individual_id": "B", "timestamp": "2020-01-01 00:00:00.000"},
    ]
    geoms = [Point(-100.0, 40.0), Point(-90.0, 30.0)]
    with tempfile.TemporaryDirectory() as td:
        pts = os.path.join(td, "shorts.fgb")
        _write_point_fgb(pts, rows, geoms)
        with pytest.raises(MovementTrajectoryError) as exc:
            compute_movement_trajectory(pts, _bucket="test-bucket")
    assert exc.value.error_code == "INSUFFICIENT_POINTS"


# ---------------------------------------------------------------------------
# Test 7 — no timestamp column
# ---------------------------------------------------------------------------


def test_no_timestamp_field_raises():
    rows = [{"individual_id": "x", "foo": 1}, {"individual_id": "x", "foo": 2}]
    geoms = [Point(-100.0, 40.0), Point(-99.99, 40.0)]
    with tempfile.TemporaryDirectory() as td:
        pts = os.path.join(td, "nots.fgb")
        _write_point_fgb(pts, rows, geoms)
        with pytest.raises(MovementTrajectoryError) as exc:
            compute_movement_trajectory(pts, _bucket="test-bucket")
    assert exc.value.error_code == "NO_TIMESTAMP_FIELD"


def test_explicit_timestamp_field_missing_raises():
    with tempfile.TemporaryDirectory() as td:
        pts = os.path.join(td, "track.fgb")
        _l_shaped_track_fgb(pts)
        with pytest.raises(MovementTrajectoryError) as exc:
            compute_movement_trajectory(
                pts, timestamp_field="not_a_column", _bucket="test-bucket"
            )
    assert exc.value.error_code == "NO_TIMESTAMP_FIELD"


# ---------------------------------------------------------------------------
# Test 8 — not point geometry
# ---------------------------------------------------------------------------


def test_not_point_geometry_raises():
    gdf = gpd.GeoDataFrame(
        {"individual_id": ["x"], "timestamp": ["2020-01-01 00:00:00.000"]},
        geometry=[LineString([(-100.0, 40.0), (-99.0, 41.0)])],
        crs="EPSG:4326",
    )
    with tempfile.TemporaryDirectory() as td:
        pts = os.path.join(td, "lines.fgb")
        gdf.to_file(pts, driver="FlatGeobuf", engine="pyogrio")
        with pytest.raises(MovementTrajectoryError) as exc:
            compute_movement_trajectory(pts, _bucket="test-bucket")
    assert exc.value.error_code == "NOT_POINT_GEOMETRY"


# ---------------------------------------------------------------------------
# Test 9 — input validation
# ---------------------------------------------------------------------------


def test_input_validation_empty_uri():
    with pytest.raises(MovementTrajectoryError) as exc:
        compute_movement_trajectory("", _bucket="test-bucket")
    assert exc.value.error_code == "VECTOR_OPEN_FAILED"


def test_input_validation_non_string_uri():
    with pytest.raises(MovementTrajectoryError):
        compute_movement_trajectory(12345, _bucket="test-bucket")  # type: ignore[arg-type]


def test_input_validation_bad_timestamp_field_type():
    with pytest.raises(MovementTrajectoryError) as exc:
        compute_movement_trajectory(
            "/tmp/x.fgb", timestamp_field=123, _bucket="test-bucket"  # type: ignore[arg-type]
        )
    assert exc.value.error_code == "NO_TIMESTAMP_FIELD"


def test_missing_local_file_raises():
    with pytest.raises(MovementTrajectoryError) as exc:
        compute_movement_trajectory(
            "/tmp/definitely-not-here-12345.fgb", _bucket="test-bucket"
        )
    assert exc.value.error_code == "DOWNLOAD_FAILED"


# ---------------------------------------------------------------------------
# Test 10 — cache hit skips the fetch
# ---------------------------------------------------------------------------


def test_cache_hit_skips_fetch():
    with tempfile.TemporaryDirectory() as td:
        pts = os.path.join(td, "track.fgb")
        _l_shaped_track_fgb(pts)

        store = FakeStorageClient()
        layer1 = compute_movement_trajectory(pts, _bucket="test-bucket")
        puts_after_first = store.put_count
        assert puts_after_first >= 1

        layer2 = compute_movement_trajectory(pts, _bucket="test-bucket")
        # Same cache key -> same uri; no second write.
        assert layer1.uri == layer2.uri
        assert store.put_count == puts_after_first


# ---------------------------------------------------------------------------
# Test 11 — payload estimator is advisory + bounded
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_is_small_and_positive():
    mb = estimate_payload_mb(points_uri="s3://bucket/x.fgb")
    assert isinstance(mb, float)
    assert 0.0 < mb < 100.0
