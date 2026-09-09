"""Router value coverage for the vector_ogr executor (the vector fold).

One access mode (``ingest.access: ogr``) reads a published vector layer through a
GDAL driver. These OFFLINE tests drive the whole executor over LOCAL files the
same drivers open -- an esri-json document on disk for ``ESRIJSON``, a shapefile
inside a real zip for ``vsizip`` -- so the query build, the frame normalizer, the
declared schema, the max-features cap and the verbatim-upstream path are
exercised as the live path runs them.
"""

from __future__ import annotations

import json
import zipfile
from urllib.parse import parse_qs, urlsplit

import geopandas as gpd
import pyogrio
import pytest
from shapely.geometry import Point

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.tools.fetchers._router.errors import RouterError
from trid3nt_server.tools.fetchers._router.executors import vector_ogr

_BBOX = (-82.7, 27.7, -82.3, 28.1)


def _spec(ingest, **over) -> SourceSpec:
    base = {
        "schema_version": "v1",
        "name": "fetch_synthetic_ogr",
        "source_class": "synthetic_ogr",
        "error_prefix": "SYNTH",
        "input_error_suffix": "INPUT_INVALID",
        "shape": "vector-fgb",
        "endpoints": {"data": {"url": "https://service.test/FeatureServer/0/query"}},
        "auth": {"mode": "none", "user_agent": "trid3nt/test"},
        "params": {"bbox": {"type": "bbox", "required": True}},
        "ingest": ingest,
        "normalize": {"crs": "EPSG:4326"},
        "output": {"layer_type": "vector", "ext": "fgb", "role": "context"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 1.0, "floor_mb": 0.1},
        "docstring": "synthetic",
    }
    base.update(over)
    return SourceSpec.model_validate(base)


def _esrijson_file(tmp_path, rows):
    """An esri-json FeatureSet on disk -- what the ESRIJSON driver reads."""
    path = tmp_path / "layer.json"
    path.write_text(json.dumps({
        "objectIdFieldName": "OBJECTID",
        "geometryType": "esriGeometryPoint",
        "spatialReference": {"wkid": 4326},
        "fields": [
            {"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID"},
            {"name": "Layer.NAME", "type": "esriFieldTypeString", "alias": "NAME",
             "length": 64},
            {"name": "Layer.SIZE", "type": "esriFieldTypeDouble", "alias": "SIZE"},
        ],
        "features": [
            {"attributes": {"OBJECTID": i + 1, "Layer.NAME": n, "Layer.SIZE": s},
             "geometry": {"x": x, "y": y}}
            for i, (n, s, x, y) in enumerate(rows)
        ],
    }))
    return str(path)


# --------------------------------------------------------------------------- #
# The query the ESRIJSON driver opens.
# --------------------------------------------------------------------------- #


def test_query_pushes_the_bbox_envelope_and_the_where_clause():
    spec = _spec({
        "access": "ogr", "ogr": {"driver": "ESRIJSON"},
        "query_template": {"out_fields": "NAME,SIZE", "order_by": "OBJECTID ASC"},
        "where_clauses": [{"template": "SIZE >= {min_size}", "require": ["min_size"]}],
    })
    url = vector_ogr.build_query(
        spec, _BBOX, where=vector_ogr.build_where(spec, {"min_size": 3}))
    q = parse_qs(urlsplit(url).query)
    assert q["f"] == ["json"]
    assert q["geometry"] == ["-82.7,27.7,-82.3,28.1"]
    assert q["geometryType"] == ["esriGeometryEnvelope"]
    assert q["outFields"] == ["NAME,SIZE"]
    assert q["orderByFields"] == ["OBJECTID ASC"]
    assert q["where"] == ["SIZE >= 3"]
    assert "resultOffset" not in q and "resultRecordCount" not in q


def test_query_omits_the_envelope_for_a_global_sweep():
    spec = _spec({"access": "ogr", "ogr": {"driver": "ESRIJSON"}})
    q = parse_qs(urlsplit(vector_ogr.build_query(spec, None)).query)
    assert "geometry" not in q and "geometryType" not in q


def test_query_carries_the_json_envelope_and_the_static_endpoint_query():
    spec = _spec(
        {"access": "ogr", "ogr": {"driver": "ESRIJSON"}, "geometry_envelope": "json"},
        endpoints={"data": {"url": "https://service.test/MapServer/3/query",
                            "query": {"maxAllowableOffset": "0.0005"}}},
    )
    q = parse_qs(urlsplit(vector_ogr.build_query(spec, _BBOX)).query)
    assert json.loads(q["geometry"][0]) == {
        "xmin": -82.7, "ymin": 27.7, "xmax": -82.3, "ymax": 28.1,
        "spatialReference": {"wkid": 4326},
    }
    assert q["maxAllowableOffset"] == ["0.0005"]


def test_a_staged_uri_is_refused_by_name():
    spec = _spec({"access": "ogr", "ogr": {"driver": "ESRIJSON"}},
                 endpoints={"data": {"url": "s3://bucket/key.json"}})
    with pytest.raises(RouterError) as exc:
        vector_ogr.build_query(spec, _BBOX)
    assert exc.value.error_code == "SYNTH_UPSTREAM_ERROR"
    assert "staged s3:// uri" in str(exc.value)


def test_an_unknown_driver_is_a_typed_input_error():
    spec = _spec({"access": "ogr", "ogr": {"driver": "WFS3"}})
    with pytest.raises(RouterError) as exc:
        vector_ogr.open_path(spec, {}, spec.endpoints["data"], "https://x.test")
    assert exc.value.error_code == "SYNTH_INPUT_INVALID"


def test_the_vsizip_path_names_the_member():
    spec = _spec({"access": "ogr",
                  "ogr": {"driver": "vsizip", "member": "tl_2024_us_county.shp"}})
    path = vector_ogr.open_path(
        spec, {}, spec.endpoints["data"], "https://census.test/county.zip")
    assert path == "/vsizip/vsicurl/https://census.test/county.zip/tl_2024_us_county.shp"


# --------------------------------------------------------------------------- #
# The read: driver -> frame normalizer -> the declared schema.
# --------------------------------------------------------------------------- #


def _read_local(spec, path, params=None, monkeypatch=None):
    """Drive fetch_from_endpoint against a LOCAL file the driver opens."""
    monkeypatch.setattr(vector_ogr, "build_query", lambda *a, **k: path)
    monkeypatch.setattr(vector_ogr, "open_path", lambda *a, **k: f"ESRIJSON:{path}")
    return vector_ogr.fetch_from_endpoint(spec, spec.endpoints["data"], params or {})


def test_the_driver_read_projects_the_declared_column_map(tmp_path, monkeypatch):
    path = _esrijson_file(tmp_path, [("alpha", 4.0, -82.6, 27.8),
                                     ("bravo", 9.0, -82.5, 27.9)])
    spec = _spec({
        "access": "ogr", "ogr": {"driver": "ESRIJSON"},
        "column_map": {"name": {"from": "Layer.NAME"},
                       "size": {"from": "Layer.SIZE", "kind": "float"}},
    })
    feats = _read_local(spec, path, monkeypatch=monkeypatch)
    assert len(feats) == 2
    data = vector_ogr.features_to_fgb_bytes(feats, spec, {})
    out = tmp_path / "out.fgb"
    out.write_bytes(data)
    df = pyogrio.read_dataframe(str(out))
    assert list(df.columns) == ["name", "size", "geometry"]
    assert sorted(df["name"]) == ["alpha", "bravo"]
    assert sorted(df["size"]) == [4.0, 9.0]


def test_max_records_caps_the_read_in_server_order(tmp_path, monkeypatch):
    path = _esrijson_file(tmp_path, [(f"r{i}", float(i), -82.6 + i / 100, 27.8)
                                     for i in range(10)])
    spec = _spec({"access": "ogr", "ogr": {"driver": "ESRIJSON"}},
                 params={"bbox": {"type": "bbox", "required": True},
                         "max_records": {"type": "int", "default": 3}})
    feats = _read_local(spec, path, {"max_records": 3}, monkeypatch=monkeypatch)
    assert [f["properties"]["Layer.NAME"] for f in feats] == ["r0", "r1", "r2"]


def test_an_empty_read_is_an_honest_header_only_fgb(tmp_path, monkeypatch):
    path = _esrijson_file(tmp_path, [])
    spec = _spec({"access": "ogr", "ogr": {"driver": "ESRIJSON"},
                  "properties": ["name", "size"]})
    feats = _read_local(spec, path, monkeypatch=monkeypatch)
    assert feats == []
    out = tmp_path / "empty.fgb"
    out.write_bytes(vector_ogr.features_to_fgb_bytes(feats, spec, {}))
    df = pyogrio.read_dataframe(str(out))
    assert len(df) == 0
    assert list(df.columns) == ["name", "size", "geometry"]


def test_a_zipped_shapefile_reads_through_vsizip(tmp_path):
    src = gpd.GeoDataFrame(
        {"NAME": ["one", "two"]},
        geometry=[Point(-82.6, 27.8), Point(-82.4, 28.0)], crs="EPSG:4326")
    shp_dir = tmp_path / "shp"
    shp_dir.mkdir()
    src.to_file(shp_dir / "places.shp", engine="pyogrio")
    zip_path = tmp_path / "places.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for member in shp_dir.iterdir():
            zf.write(member, member.name)
    df = pyogrio.read_dataframe(f"/vsizip/{zip_path}/places.shp", bbox=(-82.7, 27.7, -82.5, 27.9))
    assert list(df["NAME"]) == ["one"]


# --------------------------------------------------------------------------- #
# The honesty floor: an ArcGIS 200-with-an-error-body.
# --------------------------------------------------------------------------- #


def test_an_error_envelope_reaches_the_caller_verbatim(monkeypatch):
    """The driver can only call it a missing 'features' member; the service's own
    message is recovered by one plain read of the same URL."""
    body = json.dumps({"error": {"code": 400, "message": "Unable to complete operation.",
                                 "details": ["Invalid field: BOGUS"]}}).encode()
    monkeypatch.setattr(vector_ogr, "get_client", lambda: object())
    monkeypatch.setattr(vector_ogr, "get_bytes",
                        lambda client, url, **kw: (body, "application/json", url))
    spec = _spec({"access": "ogr", "ogr": {"driver": "ESRIJSON"}})
    exc = vector_ogr._verbatim_upstream(
        spec, "https://service.test/q", RuntimeError("Missing 'features' member."))
    assert exc.error_code == "SYNTH_UPSTREAM_ERROR"
    assert "Unable to complete operation." in str(exc)


def test_a_status_failure_keeps_the_drivers_own_text(monkeypatch):
    monkeypatch.setattr(vector_ogr, "get_client", lambda: object())
    monkeypatch.setattr(vector_ogr, "get_bytes",
                        lambda client, url, **kw: (b"<html>gone</html>", "text/html", url))
    spec = _spec({"access": "ogr", "ogr": {"driver": "ESRIJSON"}})
    exc = vector_ogr._verbatim_upstream(
        spec, "https://service.test/q", RuntimeError("HTTP response code: 404"))
    assert "HTTP response code: 404" in str(exc)


# --------------------------------------------------------------------------- #
# The mirror chain.
# --------------------------------------------------------------------------- #


def test_a_same_dataset_mirror_is_tried_on_the_primarys_failure(monkeypatch):
    spec = _spec({"access": "ogr", "ogr": {"driver": "ESRIJSON"}},
                 endpoints={"data": {"url": "https://primary.test/q"},
                            "medium": {"url": "https://mirror.test/q"}},
                 endpoint_fallback=["medium"])
    seen = []

    def _fetch(spec_, endpoint, params):
        seen.append(endpoint.url)
        if endpoint.url == "https://primary.test/q":
            raise RuntimeError("HTTP response code: 503")
        return [{"type": "Feature", "geometry": None, "properties": {}}]

    monkeypatch.setattr(vector_ogr, "fetch_from_endpoint", _fetch)
    assert len(vector_ogr.fetch_features(spec, {})) == 1
    assert seen == ["https://primary.test/q", "https://mirror.test/q"]
