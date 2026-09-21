"""``fetch_chirps_precipitation``: the borrowed gdal provider over a gzip object.

The pre-resolve names the object the archive publishes for the asked date and
period, and refuses a malformed, pre-record or future date before anything is
opened. The export step applies the row's two stated facts: the ocean sentinel
collapses to the serialize nodata, and a window that is entirely that sentinel
is typed EMPTY. A row that declares a global query asks with no bbox at all."""

from __future__ import annotations

import io

import numpy as np
import pytest
import rasterio
from rasterio import transform as rtransform
from rasterio.io import MemoryFile

from trid3nt_contracts.ws import LayerResponsePayload
from trid3nt_server.tools.fetchers._router import router as _router
from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
)
from trid3nt_server.tools.fetchers._router.executors import qgis_provider as qp
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.climate.fetch_chirps_precipitation import hooks as ch

_WESTERN_GHATS = [72.0, 15.0, 78.0, 21.0]


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_chirps_precipitation"]


def _exported(arr: np.ndarray) -> bytes:
    """What the session exports: the grid as it stands, sentinel pixels and all."""
    height, width = arr.shape
    profile = dict(
        driver="GTiff", height=height, width=width, count=1, dtype="float32",
        crs="EPSG:4326",
        transform=rtransform.from_bounds(72.0, 15.0, 78.0, 21.0, width, height),
    )
    buf = io.BytesIO()
    with rasterio.open(buf, "w", **profile) as dst:
        dst.write(arr.astype("float32"), 1)
    return buf.getvalue()


def _patch_session(monkeypatch, exported: bytes) -> list:
    asked: list = []

    def fake_ask(payload):
        asked.append(payload)
        return LayerResponsePayload(key=payload.key, uri="s3://cache/staged/x.tif")

    monkeypatch.setattr(qp, "ask_session_for_layer", fake_ask)
    monkeypatch.setattr(qp, "_store_bytes", lambda uri: exported)
    return asked


def _patch_router_cache(monkeypatch) -> dict:
    from trid3nt_server.tools.cache import ReadThroughResult

    store: dict[str, bytes] = {}

    def patched(metadata, params, ext, fetch_fn, **kw):
        data = fetch_fn()
        store["cog"] = data
        return ReadThroughResult(uri="s3://fake/chirps.tif", data=data, hit=False)

    monkeypatch.setattr(_router, "read_through", patched)
    return store


def test_spec_identity(spec):
    assert spec.shape == "raster-cog"
    assert spec.supports_global_query is True
    assert (spec.ingest or {}).get("access") == "qgis_provider"
    block = spec.ingest["qgis_provider"]
    assert block["provider"] == "gdal" and block["mode"] == "materialise"
    assert block["nodata_sentinel"] == -9000.0
    assert spec.ingest["serialize"] == {"nodata": -9999.0, "dtype": "float32"}


def test_pre_resolve_names_the_published_object(spec):
    monthly = ch.pre_resolve(spec, {"date": "2023-07", "period": "monthly"})
    assert monthly["object_url"].endswith(
        "/global_monthly/tifs/chirps-v2.0.2023.07.tif.gz")
    daily = ch.pre_resolve(spec, {"date": "2022-08-25", "period": "daily"})
    assert daily["object_url"].endswith(
        "/global_daily/tifs/p05/2022/chirps-v2.0.2022.08.25.tif.gz")


def test_bad_pre_record_and_future_dates_refuse(spec):
    for bad in ({"date": "nope", "period": "monthly"},
                {"date": "1970-01", "period": "monthly"},
                {"date": "2999-01", "period": "monthly"},
                {"date": "2022-08", "period": "daily"}):
        with pytest.raises(RouterInputError) as ei:
            ch.pre_resolve(spec, bad)
        assert ei.value.error_code == "CHIRPS_INPUT_ERROR"


def test_the_uri_carries_the_object_the_date_resolved_to(spec):
    params = {"date": "2023-07", "period": "monthly"}
    params.update(ch.pre_resolve(spec, params))
    assert qp.build_uri(spec, params) == (
        "/vsigzip//vsicurl/https://data.chc.ucsb.edu/products/CHIRPS-2.0"
        "/global_monthly/tifs/chirps-v2.0.2023.07.tif.gz")


def test_a_windowed_ask_collapses_the_ocean_sentinel(spec, monkeypatch):
    arr = np.array([[5.0, 10.0], [-9999.0, 20.0]], dtype="float32")
    asked = _patch_session(monkeypatch, _exported(arr))
    store = _patch_router_cache(monkeypatch)

    out = _router.route(spec, {"bbox": list(_WESTERN_GHATS), "date": "2023-07"})
    assert out.uri.startswith("s3://")
    assert asked[-1].bbox == tuple(_WESTERN_GHATS)
    assert asked[-1].resolution_m is None

    with MemoryFile(store["cog"]) as mem, mem.open() as ds:
        assert ds.nodata == -9999.0 and str(ds.dtypes[0]) == "float32"
        got = ds.read(1)
    assert got[0, 0] == 5.0 and got[1, 1] == 20.0
    assert got[1, 0] == -9999.0


def test_a_global_ask_carries_no_bbox(spec, monkeypatch):
    asked = _patch_session(monkeypatch, _exported(np.full((4, 4), 3.0, "float32")))
    _patch_router_cache(monkeypatch)

    out = _router.route(spec, {"date": "2023-07"})
    assert out.uri.startswith("s3://")
    assert asked[-1].bbox is None


def test_an_all_sentinel_export_is_empty(spec, monkeypatch):
    _patch_session(monkeypatch, _exported(np.full((2, 2), -9999.0, "float32")))
    _patch_router_cache(monkeypatch)

    with pytest.raises(RouterEmptyError) as ei:
        _router.route(spec, {"bbox": list(_WESTERN_GHATS), "date": "2023-07"})
    assert ei.value.error_code == "CHIRPS_EMPTY"
