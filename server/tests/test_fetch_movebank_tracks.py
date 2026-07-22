"""Unit tests for the ``fetch_movebank_tracks`` atomic tool (job-0130).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Validation: bad bbox / bad study_id / bad max_records / bad geometry_type
  raise typed errors.
- Credential resolution: missing creds → MovebankInputError; env-var fallback;
  explicit user+pass overrides env.
- Mocked happy path (linestring): a 2-individual CSV → 2 LineString features
  with vertices ordered by timestamp.
- Mocked point geometry: same CSV with geometry_type="point" → one Point per
  fix; bbox filter drops out-of-bbox rows.
- Empty study: header-only CSV → 0-feature FlatGeobuf (no error).
- HTTP 401 → MovebankAuthError (retryable=False).
- HTTP 403 → MovebankLicenseError (retryable=False).
- Cache hit: identical params skip the fetch.
- Geographic-correctness gate (job-0086): linestring with any out-of-bbox vertex
  is dropped; points outside bbox are filtered.

Live test (env-gated TRID3NT_TEST_LIVE_MOVEBANK=1 + TRID3NT_MOVEBANK_USER +
TRID3NT_MOVEBANK_PASSWORD):
- Public study 1259686571 (Sandhill Crane Bismarck-Hettinger-Mandan) → real
  tracks; evidence/movebank_live.txt.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_movebank_tracks import (
    MovebankAuthError,
    MovebankInputError,
    MovebankLicenseError,
    MovebankUpstreamError,
    _parse_movebank_csv,
    _records_to_flatgeobuf_bytes,
    _resolve_credentials,
    _round_bbox_to_6dp,
    _validate_bbox,
    fetch_movebank_tracks,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# A bbox covering north-central North Dakota (sandhill crane breeding habitat
# around Bismarck — overlaps the live-test public study).
_ND_BBOX = (-101.5, 46.5, -99.5, 47.5)

# The live test's public study — sandhill crane Bismarck-Hettinger-Mandan.
_LIVE_STUDY_ID = 1259686571

# Live-test gates.
_LIVE_MOVEBANK = os.environ.get("TRID3NT_TEST_LIVE_MOVEBANK") == "1"
_LIVE_USER = os.environ.get("TRID3NT_MOVEBANK_USER")
_LIVE_PASS = os.environ.get("TRID3NT_MOVEBANK_PASSWORD")
_HAS_LIVE_CREDS = bool(_LIVE_MOVEBANK and _LIVE_USER and _LIVE_PASS)


# ---------------------------------------------------------------------------
# Fake GCS plumbing.
# ---------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, store: dict[str, bytes], path: str) -> None:
        self._store = store
        self._path = path
        self.custom_time: datetime | None = None
        self.cache_control: str | None = None

    def exists(self) -> bool:
        return self._path in self._store

    def download_as_bytes(self) -> bytes:
        return self._store[self._path]

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self._store[self._path] = data


class FakeBucket:
    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store

    def blob(self, path: str) -> FakeBlob:
        return FakeBlob(self._store, path)


class FakeStorageClient:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.store)


def _make_read_through_injector(fake_gcs):
    """S3-only in-memory read-through injector (GCP decommissioned).

    Replaces the retired ``google.cloud.storage`` double: drives the tool's
    ``read_through`` off an in-memory S3 store (``fake_gcs.store``, keyed by
    object KEY), minting ``s3://`` URIs and honoring cache hit/miss/write.
    """
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
        is_cacheable,
        ReadThroughResult,
    )

    store = fake_gcs.store

    def patched(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return patched


class _FakeHTTPResponse:
    """Minimal httpx.Response-like object for patching."""

    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    def json(self) -> dict[str, Any]:
        raise ValueError("CSV response — not JSON")


def _make_csv_body(rows: list[tuple[str, str, float, float, int]]) -> str:
    """Build a Movebank-style direct-read CSV with our requested attributes.

    Each row: (individual_local_identifier, timestamp, location_lat, location_long, sensor_type_id)
    """
    lines = [
        "individual_local_identifier,timestamp,location_lat,location_long,sensor_type_id"
    ]
    for ind, ts, lat, lon, sensor in rows:
        lines.append(f"{ind},{ts},{lat},{lon},{sensor}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_movebank_tracks appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_movebank_tracks" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_movebank_tracks"]
    assert entry.metadata.name == "fetch_movebank_tracks"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "movebank"
    assert entry.metadata.cacheable is True
    # Audit: supports_global_query=False (study-specific).
    assert entry.metadata.supports_global_query is False


# ---------------------------------------------------------------------------
# Validation / typed-error tests
# ---------------------------------------------------------------------------


def test_invalid_study_id_zero_raises_input_error():
    with pytest.raises(MovebankInputError, match="study_id must be > 0"):
        fetch_movebank_tracks(study_id=0, username="u", password="p")


def test_invalid_study_id_negative_raises_input_error():
    with pytest.raises(MovebankInputError):
        fetch_movebank_tracks(study_id=-5, username="u", password="p")


def test_degenerate_bbox_raises_input_error():
    with pytest.raises(MovebankInputError):
        _validate_bbox((-100.0, 47.0, -100.0, 47.0))


def test_lon_out_of_range_raises_input_error():
    with pytest.raises(MovebankInputError):
        _validate_bbox((-181.0, 46.0, -100.0, 47.0))


def test_lat_out_of_range_raises_input_error():
    with pytest.raises(MovebankInputError):
        _validate_bbox((-100.0, 46.0, -99.0, 91.0))


def test_bbox_none_is_valid():
    _validate_bbox(None)  # no raise


def test_bad_geometry_type_raises_input_error():
    with pytest.raises(MovebankInputError, match="geometry_type"):
        fetch_movebank_tracks(
            study_id=42,
            username="u",
            password="p",
            geometry_type="polygon",  # type: ignore[arg-type]
        )


def test_missing_credentials_raises_input_error(monkeypatch):
    """No explicit creds, no secret_ref, no env vars → MovebankInputError."""
    monkeypatch.delenv("TRID3NT_MOVEBANK_USER", raising=False)
    monkeypatch.delenv("TRID3NT_MOVEBANK_PASSWORD", raising=False)
    with pytest.raises(MovebankInputError, match="credentials missing"):
        fetch_movebank_tracks(study_id=42)


def test_input_error_is_not_retryable():
    """MovebankInputError carries retryable=False for FR-AS-11 mapping."""
    try:
        fetch_movebank_tracks(study_id=-1, username="u", password="p")
    except MovebankInputError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected MovebankInputError")


def test_max_records_zero_raises_input_error():
    with pytest.raises(MovebankInputError, match="max_records must be > 0"):
        fetch_movebank_tracks(
            study_id=42, username="u", password="p", max_records=0
        )


def test_max_records_over_cap_raises_input_error():
    with pytest.raises(MovebankInputError, match="exceeds hard cap"):
        fetch_movebank_tracks(
            study_id=42, username="u", password="p", max_records=10_000_000
        )


def test_inverted_time_range_raises_input_error():
    with pytest.raises(MovebankInputError, match="time_range start must be <= end"):
        fetch_movebank_tracks(
            study_id=42,
            username="u",
            password="p",
            time_range=(
                datetime(2024, 5, 1, tzinfo=timezone.utc),
                datetime(2024, 1, 1, tzinfo=timezone.utc),
            ),
        )


# ---------------------------------------------------------------------------
# Credential resolution tests
# ---------------------------------------------------------------------------


def test_resolve_credentials_explicit_kwargs():
    user, pw = _resolve_credentials("alice", "s3cret", None)
    assert user == "alice"
    assert pw == "s3cret"


def test_resolve_credentials_env_fallback(monkeypatch):
    monkeypatch.setenv("TRID3NT_MOVEBANK_USER", "env_user")
    monkeypatch.setenv("TRID3NT_MOVEBANK_PASSWORD", "env_pw")
    user, pw = _resolve_credentials(None, None, None)
    assert user == "env_user"
    assert pw == "env_pw"


def test_resolve_credentials_explicit_kwargs_override_env(monkeypatch):
    monkeypatch.setenv("TRID3NT_MOVEBANK_USER", "env_user")
    monkeypatch.setenv("TRID3NT_MOVEBANK_PASSWORD", "env_pw")
    user, pw = _resolve_credentials("alice", "s3cret", None)
    assert user == "alice"
    assert pw == "s3cret"


def test_resolve_credentials_missing_raises(monkeypatch):
    monkeypatch.delenv("TRID3NT_MOVEBANK_USER", raising=False)
    monkeypatch.delenv("TRID3NT_MOVEBANK_PASSWORD", raising=False)
    with pytest.raises(MovebankInputError, match="credentials missing"):
        _resolve_credentials(None, None, None)


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


def test_round_bbox_to_6dp():
    raw = (-101.123456789, 46.123456789, -99.987654321, 47.987654321)
    rounded = _round_bbox_to_6dp(raw)
    assert rounded == (-101.123457, 46.123457, -99.987654, 47.987654)


def test_round_bbox_to_6dp_none():
    assert _round_bbox_to_6dp(None) is None


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------


def test_parse_csv_basic_rows():
    body = _make_csv_body(
        [
            ("crane-A", "2024-05-12 10:00:00.000", 47.0, -100.0, 653),
            ("crane-A", "2024-05-12 11:00:00.000", 47.1, -100.05, 653),
        ]
    )
    records = _parse_movebank_csv(body)
    assert len(records) == 2
    assert records[0]["individual_id"] == "crane-A"
    assert records[0]["lon"] == -100.0
    assert records[0]["lat"] == 47.0
    assert records[0]["sensor_type_id"] == 653


def test_parse_csv_skips_rows_missing_coords():
    body = (
        "individual_local_identifier,timestamp,location_lat,location_long,sensor_type_id\n"
        "crane-A,2024-05-12 10:00:00.000,47.0,-100.0,653\n"
        "crane-B,2024-05-12 11:00:00.000,,,653\n"  # missing coords
        "crane-A,2024-05-12 12:00:00.000,47.1,-100.05,653\n"
    )
    records = _parse_movebank_csv(body)
    assert len(records) == 2


def test_parse_csv_empty_body():
    assert _parse_movebank_csv("") == []


def test_parse_csv_handles_hyphen_column_variants():
    """Some studies emit ``location-lat`` instead of ``location_lat``."""
    body = (
        "individual-local-identifier,timestamp,location-lat,location-long,sensor-type-id\n"
        "crane-A,2024-05-12 10:00:00.000,47.0,-100.0,653\n"
    )
    records = _parse_movebank_csv(body)
    assert len(records) == 1
    assert records[0]["individual_id"] == "crane-A"
    assert records[0]["lon"] == -100.0


# ---------------------------------------------------------------------------
# FlatGeobuf serialization
# ---------------------------------------------------------------------------


def test_records_to_linestring_groups_by_individual():
    """Two individuals each with 3 fixes → 2 LineString features."""
    records = [
        # crane-A — out of chronological order to verify sort
        {
            "individual_id": "crane-A",
            "timestamp_iso": "2024-05-12 12:00:00.000",
            "lon": -100.05,
            "lat": 47.1,
            "sensor_type_id": 653,
        },
        {
            "individual_id": "crane-A",
            "timestamp_iso": "2024-05-12 10:00:00.000",
            "lon": -100.0,
            "lat": 47.0,
            "sensor_type_id": 653,
        },
        {
            "individual_id": "crane-A",
            "timestamp_iso": "2024-05-12 11:00:00.000",
            "lon": -100.02,
            "lat": 47.05,
            "sensor_type_id": 653,
        },
        # crane-B — 3 fixes
        {
            "individual_id": "crane-B",
            "timestamp_iso": "2024-05-13 09:00:00.000",
            "lon": -100.5,
            "lat": 47.2,
            "sensor_type_id": 653,
        },
        {
            "individual_id": "crane-B",
            "timestamp_iso": "2024-05-13 10:00:00.000",
            "lon": -100.4,
            "lat": 47.25,
            "sensor_type_id": 653,
        },
        {
            "individual_id": "crane-B",
            "timestamp_iso": "2024-05-13 11:00:00.000",
            "lon": -100.3,
            "lat": 47.3,
            "sensor_type_id": 653,
        },
    ]
    fgb_bytes = _records_to_flatgeobuf_bytes(
        records, _ND_BBOX, "linestring", study_id=42
    )
    assert fgb_bytes.startswith(b"fgb")

    import tempfile

    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(fgb_bytes)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 2
        # All LineStrings.
        assert all(g.geom_type == "LineString" for g in gdf.geometry)
        # Each LineString carries 3 points.
        crane_a_row = gdf[gdf["individual_id"] == "crane-A"].iloc[0]
        assert crane_a_row["n_points"] == 3
        # Timestamps sorted: first < last.
        assert crane_a_row["first_timestamp"] < crane_a_row["last_timestamp"]
        # study_id property carried through.
        assert int(crane_a_row["study_id"]) == 42
        # Coords are in chronological order.
        coords = list(gdf[gdf["individual_id"] == "crane-A"].iloc[0].geometry.coords)
        assert coords[0] == (-100.0, 47.0)
        assert coords[-1] == (-100.05, 47.1)
    finally:
        os.unlink(tf_path)


def test_records_to_point_geometry():
    """geometry_type='point' → one Point per fix."""
    records = [
        {
            "individual_id": "crane-A",
            "timestamp_iso": "2024-05-12 10:00:00.000",
            "lon": -100.0,
            "lat": 47.0,
            "sensor_type_id": 653,
        },
        {
            "individual_id": "crane-A",
            "timestamp_iso": "2024-05-12 11:00:00.000",
            "lon": -100.05,
            "lat": 47.1,
            "sensor_type_id": 653,
        },
    ]
    fgb_bytes = _records_to_flatgeobuf_bytes(
        records, _ND_BBOX, "point", study_id=42
    )

    import tempfile

    import geopandas as gpd

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(fgb_bytes)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 2
        assert all(g.geom_type == "Point" for g in gdf.geometry)
        assert set(gdf.columns) >= {
            "individual_id",
            "timestamp",
            "sensor_type_id",
            "study_id",
            "geometry",
        }
    finally:
        os.unlink(tf_path)


def test_points_outside_bbox_are_filtered_geographic_correctness():
    """job-0086 codified lesson: points outside the bbox are dropped."""
    records = [
        # In-bbox.
        {
            "individual_id": "crane-A",
            "timestamp_iso": "2024-05-12 10:00:00.000",
            "lon": -100.0,
            "lat": 47.0,
            "sensor_type_id": 653,
        },
        # Way outside (California).
        {
            "individual_id": "crane-A",
            "timestamp_iso": "2024-05-12 11:00:00.000",
            "lon": -120.0,
            "lat": 40.0,
            "sensor_type_id": 653,
        },
    ]
    fgb_bytes = _records_to_flatgeobuf_bytes(
        records, _ND_BBOX, "point", study_id=42
    )

    import tempfile

    import geopandas as gpd

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(fgb_bytes)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 1
        # Surviving point is geographically inside the bbox.
        geom = gdf.geometry.iloc[0]
        assert _ND_BBOX[0] <= geom.x <= _ND_BBOX[2]
        assert _ND_BBOX[1] <= geom.y <= _ND_BBOX[3]
    finally:
        os.unlink(tf_path)


def test_linestring_with_out_of_bbox_vertex_is_dropped():
    """If ANY vertex of an individual's track is outside the bbox, the whole
    individual is dropped (conservative geographic-correctness gate)."""
    records = [
        # crane-A: 2 in-bbox + 1 out → drop entirely.
        {
            "individual_id": "crane-A",
            "timestamp_iso": "2024-05-12 10:00:00.000",
            "lon": -100.0,
            "lat": 47.0,
            "sensor_type_id": 653,
        },
        {
            "individual_id": "crane-A",
            "timestamp_iso": "2024-05-12 11:00:00.000",
            "lon": -120.0,
            "lat": 40.0,  # California — outside ND bbox
            "sensor_type_id": 653,
        },
        {
            "individual_id": "crane-A",
            "timestamp_iso": "2024-05-12 12:00:00.000",
            "lon": -100.05,
            "lat": 47.1,
            "sensor_type_id": 653,
        },
        # crane-B: all in-bbox → keep.
        {
            "individual_id": "crane-B",
            "timestamp_iso": "2024-05-13 09:00:00.000",
            "lon": -100.5,
            "lat": 47.2,
            "sensor_type_id": 653,
        },
        {
            "individual_id": "crane-B",
            "timestamp_iso": "2024-05-13 10:00:00.000",
            "lon": -100.4,
            "lat": 47.25,
            "sensor_type_id": 653,
        },
    ]
    fgb_bytes = _records_to_flatgeobuf_bytes(
        records, _ND_BBOX, "linestring", study_id=42
    )

    import tempfile

    import geopandas as gpd

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(fgb_bytes)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 1
        assert gdf.iloc[0]["individual_id"] == "crane-B"
    finally:
        os.unlink(tf_path)


def test_single_point_track_not_linestring():
    """A 1-fix individual cannot become a LineString — dropped."""
    records = [
        {
            "individual_id": "crane-A",
            "timestamp_iso": "2024-05-12 10:00:00.000",
            "lon": -100.0,
            "lat": 47.0,
            "sensor_type_id": 653,
        },
    ]
    fgb_bytes = _records_to_flatgeobuf_bytes(
        records, _ND_BBOX, "linestring", study_id=42
    )
    # Empty FlatGeobuf still has a header but no features.
    import tempfile

    import geopandas as gpd

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(fgb_bytes)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 0
    finally:
        os.unlink(tf_path)


# ---------------------------------------------------------------------------
# Mocked HTTP tests — happy path, auth, license, upstream errors
# ---------------------------------------------------------------------------


def test_mocked_happy_path_linestring():
    """A 2-individual CSV → 2 LineString FlatGeobuf features."""
    fake_gcs = FakeStorageClient()
    csv_body = _make_csv_body(
        [
            ("crane-A", "2024-05-12 10:00:00.000", 47.0, -100.0, 653),
            ("crane-A", "2024-05-12 11:00:00.000", 47.05, -100.02, 653),
            ("crane-A", "2024-05-12 12:00:00.000", 47.1, -100.05, 653),
            ("crane-B", "2024-05-13 09:00:00.000", 47.2, -100.5, 653),
            ("crane-B", "2024-05-13 10:00:00.000", 47.25, -100.4, 653),
        ]
    )

    with patch(
        "trid3nt_server.tools.fetch_movebank_tracks.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_movebank_tracks.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(200, csv_body)
        mock_client_cls.return_value = mock_client

        result = fetch_movebank_tracks(
            study_id=42,
            bbox=_ND_BBOX,
            username="alice",
            password="s3cret",
        )

    assert result.uri is not None
    assert result.uri.startswith("s3://")
    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units is None
    assert result.style_preset == "movebank_tracks"
    assert "42" in result.layer_id
    assert "linestring" in result.layer_id

    # Verify the saved FlatGeobuf has 2 LineString features.
    [(path, data)] = list(fake_gcs.store.items())
    assert path.startswith("cache/static-30d/movebank/")
    assert path.endswith(".fgb")

    import tempfile

    import geopandas as gpd

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 2
        assert all(g.geom_type == "LineString" for g in gdf.geometry)
    finally:
        os.unlink(tf_path)


def test_mocked_point_geometry_with_bbox_filter():
    """geometry_type='point' filters out-of-bbox fixes."""
    fake_gcs = FakeStorageClient()
    csv_body = _make_csv_body(
        [
            ("crane-A", "2024-05-12 10:00:00.000", 47.0, -100.0, 653),
            ("crane-A", "2024-05-12 11:00:00.000", 47.05, -100.02, 653),
            # Outside bbox — should be filtered.
            ("crane-B", "2024-05-13 09:00:00.000", 40.0, -120.0, 653),
        ]
    )

    with patch(
        "trid3nt_server.tools.fetch_movebank_tracks.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_movebank_tracks.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(200, csv_body)
        mock_client_cls.return_value = mock_client

        result = fetch_movebank_tracks(
            study_id=42,
            bbox=_ND_BBOX,
            username="alice",
            password="s3cret",
            geometry_type="point",
        )

    assert "point" in result.layer_id
    [(_, data)] = list(fake_gcs.store.items())

    import tempfile

    import geopandas as gpd

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        # Only 2 in-bbox fixes survive.
        assert len(gdf) == 2
        for geom in gdf.geometry:
            assert _ND_BBOX[0] <= geom.x <= _ND_BBOX[2]
            assert _ND_BBOX[1] <= geom.y <= _ND_BBOX[3]
    finally:
        os.unlink(tf_path)


def test_mocked_empty_study_returns_empty_flatgeobuf():
    """A header-only CSV → empty FlatGeobuf, no error."""
    fake_gcs = FakeStorageClient()
    csv_body = (
        "individual_local_identifier,timestamp,location_lat,location_long,sensor_type_id\n"
    )

    with patch(
        "trid3nt_server.tools.fetch_movebank_tracks.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_movebank_tracks.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(200, csv_body)
        mock_client_cls.return_value = mock_client

        result = fetch_movebank_tracks(
            study_id=42, username="alice", password="s3cret"
        )

    assert result.uri is not None
    [(_, data)] = list(fake_gcs.store.items())
    assert len(data) > 0  # empty FlatGeobuf still has a header

    import tempfile

    import geopandas as gpd

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 0
    finally:
        os.unlink(tf_path)


def test_mocked_401_raises_auth_error():
    """A 401 from Movebank raises a non-retryable MovebankAuthError."""
    fake_gcs = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetch_movebank_tracks.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_movebank_tracks.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(401, text="Unauthorized")
        mock_client_cls.return_value = mock_client

        with pytest.raises(MovebankAuthError) as exc_info:
            fetch_movebank_tracks(
                study_id=42, username="bad", password="creds"
            )
        assert exc_info.value.retryable is False
    # No artifact written.
    assert fake_gcs.store == {}


def test_mocked_403_raises_license_error():
    """A 403 from Movebank raises a non-retryable MovebankLicenseError."""
    fake_gcs = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetch_movebank_tracks.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_movebank_tracks.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(403, text="License not accepted")
        mock_client_cls.return_value = mock_client

        with pytest.raises(MovebankLicenseError) as exc_info:
            fetch_movebank_tracks(
                study_id=42, username="alice", password="s3cret"
            )
        assert exc_info.value.retryable is False


def test_mocked_html_license_page_raises_license_error():
    """An HTML licence-acceptance body is detected and raises MovebankLicenseError."""
    fake_gcs = FakeStorageClient()
    html_body = (
        "<html><body><h1>License Terms</h1>"
        "<p>Please accept the Data Use Statement.</p>"
        "</body></html>"
    )
    with patch(
        "trid3nt_server.tools.fetch_movebank_tracks.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_movebank_tracks.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(200, html_body)
        mock_client_cls.return_value = mock_client

        with pytest.raises(MovebankLicenseError):
            fetch_movebank_tracks(
                study_id=42, username="alice", password="s3cret"
            )


def test_mocked_5xx_raises_upstream_error_retryable():
    """A 503 from Movebank raises a retryable MovebankUpstreamError."""
    fake_gcs = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetch_movebank_tracks.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_movebank_tracks.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(503, text="Service Unavailable")
        mock_client_cls.return_value = mock_client

        with pytest.raises(MovebankUpstreamError) as exc_info:
            fetch_movebank_tracks(
                study_id=42, username="alice", password="s3cret"
            )
        assert exc_info.value.retryable is True
    assert fake_gcs.store == {}


# ---------------------------------------------------------------------------
# Cache-layer tests
# ---------------------------------------------------------------------------


def test_cache_hit_skips_fetch_fn():
    """Second call with identical params returns the cached URI without re-fetching."""
    fake_gcs = FakeStorageClient()
    csv_body = _make_csv_body(
        [
            ("crane-A", "2024-05-12 10:00:00.000", 47.0, -100.0, 653),
            ("crane-A", "2024-05-12 11:00:00.000", 47.05, -100.02, 653),
        ]
    )

    with patch(
        "trid3nt_server.tools.fetch_movebank_tracks.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_movebank_tracks.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(200, csv_body)
        mock_client_cls.return_value = mock_client

        r1 = fetch_movebank_tracks(
            study_id=42, bbox=_ND_BBOX, username="alice", password="s3cret"
        )
        r2 = fetch_movebank_tracks(
            study_id=42, bbox=_ND_BBOX, username="alice", password="s3cret"
        )

    # Only one HTTP call (second hit the cache).
    assert mock_client.get.call_count == 1
    assert r1.uri == r2.uri


def test_layer_uri_shape_fields():
    """The returned LayerURI carries the documented fields."""
    fake_gcs = FakeStorageClient()
    csv_body = _make_csv_body(
        [
            ("crane-A", "2024-05-12 10:00:00.000", 47.0, -100.0, 653),
            ("crane-A", "2024-05-12 11:00:00.000", 47.05, -100.02, 653),
        ]
    )
    with patch(
        "trid3nt_server.tools.fetch_movebank_tracks.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_movebank_tracks.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(200, csv_body)
        mock_client_cls.return_value = mock_client

        result = fetch_movebank_tracks(
            study_id=42, username="alice", password="s3cret"
        )

    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units is None
    assert result.style_preset == "movebank_tracks"
    assert "42" in result.layer_id
    assert "Movebank" in result.name


def test_request_includes_basic_auth():
    """Verify the HTTP request carries (username, password) via httpx auth."""
    fake_gcs = FakeStorageClient()
    csv_body = _make_csv_body(
        [
            ("crane-A", "2024-05-12 10:00:00.000", 47.0, -100.0, 653),
            ("crane-A", "2024-05-12 11:00:00.000", 47.05, -100.02, 653),
        ]
    )

    with patch(
        "trid3nt_server.tools.fetch_movebank_tracks.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_movebank_tracks.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(200, csv_body)
        mock_client_cls.return_value = mock_client

        fetch_movebank_tracks(
            study_id=42, username="alice", password="s3cret"
        )

    assert mock_client.get.call_count == 1
    call_kwargs = mock_client.get.call_args.kwargs
    assert call_kwargs["auth"] == ("alice", "s3cret")
    assert call_kwargs["params"]["entity_type"] == "event"
    assert call_kwargs["params"]["study_id"] == 42


# ---------------------------------------------------------------------------
# Live test — real Movebank API call (env-gated)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _HAS_LIVE_CREDS,
    reason="TRID3NT_TEST_LIVE_MOVEBANK=1 + TRID3NT_MOVEBANK_USER + TRID3NT_MOVEBANK_PASSWORD required",
)
def test_live_sandhill_crane_public_study(tmp_path):
    """LIVE: public study 1259686571 (Sandhill Crane Bismarck) → real tracks.

    Calls the real Movebank API. Captures evidence to evidence/movebank_live.txt.
    Asserts ≥1 feature returned.

    Note: this study requires the account to have accepted its Data Use Statement
    on movebank.org. If MovebankLicenseError fires, log into the Movebank web
    UI under the same account and click "Accept" on the study's licence page.
    """
    fake_gcs = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetch_movebank_tracks.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_movebank_tracks(
            study_id=_LIVE_STUDY_ID,
            username=_LIVE_USER,
            password=_LIVE_PASS,
            geometry_type="linestring",
            max_records=50_000,
        )

    assert result.uri is not None
    [(path, data)] = list(fake_gcs.store.items())
    assert path.startswith("cache/static-30d/movebank/")
    assert path.endswith(".fgb")

    import tempfile

    import geopandas as gpd

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
    finally:
        os.unlink(tf_path)

    assert len(gdf) >= 1, "Expected at least one sandhill crane track"

    # Capture evidence.
    evidence_lines = [
        f"# Movebank live test — sandhill crane study {_LIVE_STUDY_ID}",
        f"# result.uri: {result.uri}",
        f"# feature count: {len(gdf)}",
        "",
    ]
    for i, row in enumerate(gdf.head(5).itertuples(index=False)):
        evidence_lines.append(f"feature {i}: {row}")
    evidence_text = "\n".join(evidence_lines)
    print("\n" + evidence_text)
    evidence_dir = (
        os.path.dirname(os.path.abspath(__file__))
        + "/../../../reports/inflight/job-0130-engine-20260608/evidence"
    )
    os.makedirs(evidence_dir, exist_ok=True)
    with open(evidence_dir + "/movebank_live.txt", "w") as f:
        f.write(evidence_text)
