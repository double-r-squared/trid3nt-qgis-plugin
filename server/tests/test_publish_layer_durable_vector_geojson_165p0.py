"""publish_layer DATA-ISLAND #165 PHASE 0 — durable browser-readable vector GeoJSON.

CONTEXT: vectors are produced as FlatGeobuf (``.fgb``) which the browser cannot
read. Today the agent inlines them (reads the .fgb -> GeoJSON -> WS), so the
box-OFF cold path (signer -> S3) has no browser-readable vector and a cold case
paints rasters but not roads/rivers/footprints/mesh.

This phase FREEZES a durable contract: every IN-CASE vector publish materializes
a GeoJSON FeatureCollection at a stable per-Case key in the DURABLE runs bucket
(NOT the 30-day-TTL content-addressed cache bucket), and returns that asset URI.

Frozen contract (engine tracks rebase onto this):
  bucket : GRACE2_RUNS_BUCKET (solver._get_runs_bucket — the DURABLE runs bucket)
  key    : ``case-data/<case_id>/<layer_id>.geojson``
  asset  : ``s3://<runs_bucket>/case-data/<case_id>/<layer_id>.geojson`` (DISPLAY)
  faces  : observe_published_layer(layer_id, gcs_uri=<s3 .fgb DATA>,
           wms_url=<s3 .geojson DISPLAY>) — GeoJSON never displaces the data uri.

These exercise the s3 vector branch end-to-end with REAL FlatGeobuf bytes
(geopandas) + a mocked boto3 client (no S3 network I/O). The inline-render
no-case path (existing F32 benign no-op) stays green.
"""

from __future__ import annotations

import json

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point

from grace2_agent.tools.publish_layer import (
    DURABLE_CASE_DATA_PREFIX,
    _vector_uri_to_geojson_bytes,
    _write_durable_vector_geojson,
    durable_vector_geojson_key,
    publish_layer,
)

_RUNS_BUCKET = "trid3nt-runs"


# --------------------------------------------------------------------------- #
# Fixtures: real FlatGeobuf / GeoJSON bytes
# --------------------------------------------------------------------------- #


def _fgb_bytes(tmp_path) -> bytes:
    """A real two-feature LineString FlatGeobuf (EPSG:4326) as bytes."""
    gdf = gpd.GeoDataFrame(
        {"name": ["a", "b"]},
        geometry=[
            LineString([(0, 0), (1, 1)]),
            LineString([(2, 2), (3, 3)]),
        ],
        crs="EPSG:4326",
    )
    p = tmp_path / "roads.fgb"
    gdf.to_file(p, driver="FlatGeobuf")
    return p.read_bytes()


def _geojson_fc_bytes() -> bytes:
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
                    "properties": {"k": "v"},
                }
            ],
        }
    ).encode("utf-8")


@pytest.fixture()
def _s3_titiler_no_wms(monkeypatch: pytest.MonkeyPatch) -> None:
    """s3 backend + a tile base, but NO QGIS WMS base (the live stack today)."""
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_TILE_SERVER_BASE", "https://cf.example.net")
    monkeypatch.delenv("GRACE2_QGIS_WMS_BASE", raising=False)
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", _RUNS_BUCKET)


class _FakeS3:
    """Minimal boto3 S3 stub: records put_object + serves a planted get_object."""

    def __init__(self, objects: dict[tuple[str, str], bytes] | None = None) -> None:
        self.objects = dict(objects or {})
        self.puts: list[dict] = []

    def put_object(self, *, Bucket, Key, Body, ContentType=None):  # noqa: N803
        self.puts.append(
            {"Bucket": Bucket, "Key": Key, "Body": Body, "ContentType": ContentType}
        )
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket, Key):  # noqa: N803
        data = self.objects[(Bucket, Key)]

        class _Body:
            def read(self_inner) -> bytes:  # noqa: N805
                return data

        return {"Body": _Body()}


# --------------------------------------------------------------------------- #
# Frozen-key contract
# --------------------------------------------------------------------------- #


def test_durable_key_layout_is_frozen() -> None:
    assert DURABLE_CASE_DATA_PREFIX == "case-data"
    assert (
        durable_vector_geojson_key("CASE123", "roads")
        == "case-data/CASE123/roads.geojson"
    )


# --------------------------------------------------------------------------- #
# _vector_uri_to_geojson_bytes — reuse of the existing read/parse helpers
# --------------------------------------------------------------------------- #


def test_vector_uri_to_geojson_bytes_reads_fgb_local(tmp_path) -> None:
    """A local .fgb is read + parsed to a GeoJSON FeatureCollection (geopandas)."""
    p = tmp_path / "roads.fgb"
    p.write_bytes(_fgb_bytes(tmp_path))
    out = _vector_uri_to_geojson_bytes(str(p))
    assert out is not None
    obj = json.loads(out)
    assert obj["type"] == "FeatureCollection"
    assert len(obj["features"]) == 2


def test_vector_uri_to_geojson_bytes_passes_through_geojson(tmp_path) -> None:
    p = tmp_path / "pts.geojson"
    p.write_bytes(_geojson_fc_bytes())
    out = _vector_uri_to_geojson_bytes(str(p))
    assert out is not None
    obj = json.loads(out)
    assert obj["type"] == "FeatureCollection"
    assert obj["features"][0]["geometry"]["type"] == "Point"


def test_vector_uri_to_geojson_bytes_reads_s3_fgb(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An s3:// .fgb is read via the shared boto3 cache helper, then parsed."""
    fgb = _fgb_bytes(tmp_path)
    fake = _FakeS3({(_RUNS_BUCKET, "runs/roads.fgb"): fgb})
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)

    out = _vector_uri_to_geojson_bytes(f"s3://{_RUNS_BUCKET}/runs/roads.fgb")
    assert out is not None
    assert json.loads(out)["type"] == "FeatureCollection"


def test_vector_uri_to_geojson_bytes_none_on_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read error returns None (fail-open) — never raises."""

    def _boom(*a, **k):
        raise RuntimeError("s3 down")

    monkeypatch.setattr("boto3.client", _boom)
    assert _vector_uri_to_geojson_bytes("s3://b/roads.fgb") is None


def test_vector_uri_to_geojson_bytes_none_on_bad_geojson(tmp_path) -> None:
    """A non-FeatureCollection .geojson returns None (fail-open)."""
    p = tmp_path / "notfc.geojson"
    p.write_bytes(json.dumps({"type": "Feature"}).encode("utf-8"))
    assert _vector_uri_to_geojson_bytes(str(p)) is None


def test_vector_uri_to_geojson_bytes_none_on_gs_uri() -> None:
    """A gs:// vector (GCP decommissioned) fails open to None."""
    assert _vector_uri_to_geojson_bytes("gs://b/roads.fgb") is None


# --------------------------------------------------------------------------- #
# _write_durable_vector_geojson — durable runs-bucket write
# --------------------------------------------------------------------------- #


def test_write_durable_vector_geojson_writes_to_runs_bucket(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writes case-data/<case>/<layer>.geojson to the DURABLE runs bucket and
    returns the s3:// asset URI."""
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", _RUNS_BUCKET)
    fgb = _fgb_bytes(tmp_path)
    fake = _FakeS3({(_RUNS_BUCKET, "runs/roads.fgb"): fgb})
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    # solver caches an override binding; ensure the env default is used.
    monkeypatch.setattr("grace2_agent.tools.solver._RUNS_BUCKET", None, raising=False)

    asset = _write_durable_vector_geojson(
        f"s3://{_RUNS_BUCKET}/runs/roads.fgb", "roads", "CASE9"
    )
    assert asset == f"s3://{_RUNS_BUCKET}/case-data/CASE9/roads.geojson"
    # Exactly one put to the durable case-data key.
    assert len(fake.puts) == 1
    put = fake.puts[0]
    assert put["Bucket"] == _RUNS_BUCKET
    assert put["Key"] == "case-data/CASE9/roads.geojson"
    assert put["ContentType"] == "application/geo+json"
    # The written body is a valid GeoJSON FeatureCollection.
    assert json.loads(put["Body"])["type"] == "FeatureCollection"


def test_write_durable_vector_geojson_not_cache_bucket(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The durable write targets the RUNS bucket — never the 30-day-TTL cache
    bucket (a published layer must outlive cache eviction)."""
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", _RUNS_BUCKET)
    monkeypatch.setattr("grace2_agent.tools.solver._RUNS_BUCKET", None, raising=False)
    p = tmp_path / "pts.geojson"
    p.write_bytes(_geojson_fc_bytes())
    # Read from a LOCAL path; write through the fake.
    fake = _FakeS3()
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)

    asset = _write_durable_vector_geojson(str(p), "pts", "CASE1")
    assert asset is not None
    assert fake.puts[0]["Bucket"] == _RUNS_BUCKET
    assert "case-data/CASE1/pts.geojson" == fake.puts[0]["Key"]


def test_write_durable_vector_geojson_fail_open_on_write_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A put_object failure returns None (fail-open) — never raises."""
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", _RUNS_BUCKET)
    monkeypatch.setattr("grace2_agent.tools.solver._RUNS_BUCKET", None, raising=False)
    p = tmp_path / "pts.geojson"
    p.write_bytes(_geojson_fc_bytes())

    class _BoomS3(_FakeS3):
        def put_object(self, **k):  # noqa: N803
            raise RuntimeError("AccessDenied")

    monkeypatch.setattr("boto3.client", lambda *a, **k: _BoomS3())
    assert _write_durable_vector_geojson(str(p), "pts", "CASE1") is None


def test_write_durable_vector_geojson_fail_open_on_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable source returns None before any write is attempted."""

    def _boom(*a, **k):
        raise RuntimeError("no such key")

    monkeypatch.setattr("boto3.client", _boom)
    assert _write_durable_vector_geojson("s3://b/missing.fgb", "x", "C") is None


# --------------------------------------------------------------------------- #
# publish_layer s3 vector branch end-to-end (the FROZEN contract)
# --------------------------------------------------------------------------- #


def test_publish_vector_in_case_writes_durable_and_registers_both_faces(
    _s3_titiler_no_wms: None, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An IN-CASE vector publish: durable GeoJSON written + asset URI returned +
    BOTH faces registered (.fgb DATA preserved; GeoJSON asset = DISPLAY)."""
    monkeypatch.setattr("grace2_agent.tools.solver._RUNS_BUCKET", None, raising=False)
    fgb = _fgb_bytes(tmp_path)
    data_uri = f"s3://{_RUNS_BUCKET}/runs/roads.fgb"
    fake = _FakeS3({(_RUNS_BUCKET, "runs/roads.fgb"): fgb})
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)

    calls: list[tuple] = []
    monkeypatch.setattr(
        "grace2_agent.tools.publish_layer.observe_published_layer",
        lambda *a, **k: calls.append((a, k)),
    )

    result = publish_layer(layer_uri=data_uri, layer_id="roads", case_id="CASE9")

    # 1. Returns the durable s3:// GeoJSON asset URI (no bare noop).
    assert result == f"s3://{_RUNS_BUCKET}/case-data/CASE9/roads.geojson"
    assert not result.startswith("noop")
    # 2. The durable GeoJSON was written to the runs bucket at the frozen key.
    assert fake.puts[0]["Key"] == "case-data/CASE9/roads.geojson"
    # 3. BOTH faces registered: .fgb is the DATA uri; the GeoJSON asset is DISPLAY.
    assert len(calls) == 1
    (args, kwargs) = calls[0]
    assert args[0] == "roads"
    assert kwargs["gcs_uri"] == data_uri  # data face preserved
    assert kwargs["wms_url"] == result  # display face = durable GeoJSON


def test_publish_vector_no_case_stays_benign_no_op(
    _s3_titiler_no_wms: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Case context -> existing benign no-op (inline-render path unchanged);
    no durable write, no registration."""
    puts: list = []
    monkeypatch.setattr(
        "boto3.client",
        lambda *a, **k: pytest.fail("must not touch S3 without a Case"),
    )
    calls: list = []
    monkeypatch.setattr(
        "grace2_agent.tools.publish_layer.observe_published_layer",
        lambda *a, **k: calls.append((a, k)),
    )

    result = publish_layer(layer_uri="s3://bucket/roads.fgb", layer_id="roads")

    assert result.startswith("noop")
    assert calls == []


def test_publish_vector_in_case_fail_open_to_noop_on_write_error(
    _s3_titiler_no_wms: None, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable-write failure in a Case falls OPEN to the benign no-op (no
    raise) and registers NOTHING (data-source-fallback norm)."""
    monkeypatch.setattr("grace2_agent.tools.solver._RUNS_BUCKET", None, raising=False)
    fgb = _fgb_bytes(tmp_path)

    class _BoomOnPut(_FakeS3):
        def put_object(self, **k):  # noqa: N803
            raise RuntimeError("AccessDenied")

    boom = _BoomOnPut({(_RUNS_BUCKET, "runs/roads.fgb"): fgb})
    monkeypatch.setattr("boto3.client", lambda *a, **k: boom)
    calls: list = []
    monkeypatch.setattr(
        "grace2_agent.tools.publish_layer.observe_published_layer",
        lambda *a, **k: calls.append((a, k)),
    )

    result = publish_layer(
        layer_uri=f"s3://{_RUNS_BUCKET}/runs/roads.fgb",
        layer_id="roads",
        case_id="CASE9",
    )

    # Fell open to the benign no-op; nothing registered.
    assert result.startswith("noop")
    assert calls == []


def test_publish_vector_in_case_fail_open_to_noop_on_read_error(
    _s3_titiler_no_wms: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable source in a Case falls open to the benign no-op."""
    monkeypatch.setattr("grace2_agent.tools.solver._RUNS_BUCKET", None, raising=False)

    def _boom(*a, **k):
        raise RuntimeError("NoSuchKey")

    monkeypatch.setattr("boto3.client", _boom)
    result = publish_layer(
        layer_uri="s3://bucket/missing.fgb", layer_id="roads", case_id="CASE9"
    )
    assert result.startswith("noop")


def test_publish_vector_in_case_geojson_source(
    _s3_titiler_no_wms: None, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .geojson source (e.g. SFINCS/SWMM mesh) is also materialized durably."""
    monkeypatch.setattr("grace2_agent.tools.solver._RUNS_BUCKET", None, raising=False)
    data_uri = f"s3://{_RUNS_BUCKET}/runs/mesh.geojson"
    fake = _FakeS3({(_RUNS_BUCKET, "runs/mesh.geojson"): _geojson_fc_bytes()})
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    monkeypatch.setattr(
        "grace2_agent.tools.publish_layer.observe_published_layer",
        lambda *a, **k: None,
    )

    result = publish_layer(
        layer_uri=data_uri, layer_id="swmm-mesh", case_id="CASE2"
    )
    assert result == f"s3://{_RUNS_BUCKET}/case-data/CASE2/swmm-mesh.geojson"
    assert fake.puts[0]["Key"] == "case-data/CASE2/swmm-mesh.geojson"


def test_publish_vector_wms_branch_still_wins_when_env_set(
    _s3_titiler_no_wms: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """job-0308 compose-not-replace: when GRACE2_QGIS_WMS_BASE is set the WMS
    branch still wins (no durable GeoJSON write attempted)."""
    monkeypatch.setenv("GRACE2_QGIS_WMS_BASE", "https://cf.example.net/ogc/wms")
    monkeypatch.setattr(
        "boto3.client",
        lambda *a, **k: pytest.fail("WMS branch must not write durable GeoJSON"),
    )
    calls: list = []
    monkeypatch.setattr(
        "grace2_agent.tools.publish_layer.observe_published_layer",
        lambda *a, **k: calls.append((a, k)),
    )

    result = publish_layer(
        layer_uri="s3://bucket/roads.fgb", layer_id="roads", case_id="CASE9"
    )
    # The WMS GetMap URL (not a durable GeoJSON asset).
    assert result.startswith("https://cf.example.net/ogc/wms?")
    assert "SERVICE=WMS" in result
    assert calls[0][1]["wms_url"] == result
