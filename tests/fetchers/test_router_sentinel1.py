"""``fetch_sentinel1_sar``: the catalog raster fold with two additions.

A coverage-fraction-then-recency scene select behind an asset-presence pre-filter,
and a decibel transform whose sentinel nodata is the existing serialize directive.
OFFLINE these cover the spec identity, the area, polarization and collection
gates, the collection-alias normalization and the nodata round trip."""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.executors import stac_raster
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_sentinel1_sar"]


def test_spec_identity(spec):
    assert spec.name == "fetch_sentinel1_sar"
    assert spec.shape == "raster-cog"
    assert spec.error_code_prefix == "SENTINEL1"
    assert spec.supports_global_query is False
    assert spec.cache.ttl_class == "static-30d"
    assert spec.source_class == "sentinel1_sar"
    assert spec.output.emit_bbox is False               # twin omits LayerURI.bbox
    assert spec.empty_error_suffix == "NO_IMAGERY"
    stac = (spec.ingest or {}).get("stac", {})
    assert stac.get("select", {}).get("mode") == "coverage"
    assert (spec.ingest or {}).get("transform", {}).get("log10_db") is True
    assert (spec.ingest or {}).get("serialize", {}).get("nodata") == -9999.0


def test_bbox_area_gate(spec):
    with pytest.raises(Exception) as ei:
        router.validate_params(spec, {"bbox": [-100.0, 30.0, -98.0, 32.0]})  # 2*2 = 4 deg^2 > 0.5
    assert getattr(ei.value, "error_code", "") == "SENTINEL1_BBOX_INVALID"


def test_bad_polarization_enum(spec):
    with pytest.raises(Exception) as ei:
        router.validate_params(
            spec, {"bbox": [-95.4, 29.7, -95.3, 29.8], "polarization": "hh"})
    assert getattr(ei.value, "error_code", "") == "SENTINEL1_POLARIZATION_INVALID"


def test_polarization_case_insensitive(spec):
    params = router.validate_params(
        spec, {"bbox": [-95.4, 29.7, -95.3, 29.8], "polarization": "VH"})
    assert params["polarization"] == "vh"


@pytest.mark.parametrize("raw,canon", [
    ("sentinel-1-rtc", "sentinel-1-rtc"),
    ("rtc", "sentinel-1-rtc"),
    ("s1-grd", "sentinel-1-grd"),
    ("GRD", "sentinel-1-grd"),
])
def test_collection_alias_normalization(spec, raw, canon):
    stac = (spec.ingest or {}).get("stac", {})
    norm = stac_raster._normalize_via_aliases(
        spec, raw, stac.get("product_aliases", {}),
        list((stac.get("collection_by_param") or {}).get("map", {}).keys()),
        stac.get("param_error_suffix", "PARAM_INVALID"),
    )
    assert stac["collection_by_param"]["map"][norm] == canon


def test_unknown_collection_raises_typed(spec):
    stac = (spec.ingest or {}).get("stac", {})
    with pytest.raises(Exception) as ei:
        stac_raster._normalize_via_aliases(
            spec, "landsat", stac.get("product_aliases", {}),
            list((stac.get("collection_by_param") or {}).get("map", {}).keys()),
            stac.get("param_error_suffix", "PARAM_INVALID"),
        )
    assert getattr(ei.value, "error_code", "") == "SENTINEL1_COLLECTION_INVALID"


def test_serialize_directive_fills_db_nodata(spec, monkeypatch):
    """The serialize directive fills NaN with the -9999 dB sentinel (single-band float32)."""
    import rasterio.transform as rt

    arr = np.array([[0.0, 10.0], [np.nan, -3.5]], dtype="float32")  # a post-log10_db dB tile
    transform = rt.from_bounds(-95.4, 29.7, -95.3, 29.8, 2, 2)

    def fake_fetch(_spec, _params):
        return arr, transform, "EPSG:4326"

    monkeypatch.setattr(stac_raster, "_render_float", fake_fetch)
    cog = stac_raster.execute(spec, {"bbox": [-95.4, 29.7, -95.3, 29.8]})
    assert cog[:4] in (b"II*\x00", b"MM\x00*")

    import os
    import tempfile

    import rasterio

    fd, p = tempfile.mkstemp(suffix=".tif")
    os.close(fd)
    try:
        with open(p, "wb") as f:
            f.write(cog)
        with rasterio.open(p) as src:
            assert src.nodata == -9999.0
            band = src.read(1)
            assert band[1, 0] == pytest.approx(-9999.0)   # NaN -> sentinel
            assert band[0, 1] == pytest.approx(10.0)       # a real dB value survives
    finally:
        os.unlink(p)


def test_units_by_param(spec):
    """LayerURI.units stamps per polarization (twin's f-string units)."""
    layer = router.build_layer_uri(
        spec, router.validate_params(spec, {"bbox": [-95.4, 29.7, -95.3, 29.8], "polarization": "vh"}),
        "s3://bucket/x.tif",
    )
    assert layer.units == "VH gamma0 backscatter (dB)"
    assert layer.style["kind"] == "continuous"
    assert layer.role == "primary"
