"""Router value coverage for the ISRIC SoilGrids fold (ADR 0086).

fetch_soilgrids folded to source.yaml + the raster_cog ``projected_vrt_window``
access mode: the Homolosine VRT is windowed in the SOURCE projection
(transform_bounds 4326->Homolosine, densified + the twin's floor/ceil + 2 px pad),
the intersecting members read through the coalescing transport, reprojected
native->4326 (bilinear, ~250 m), and the fixed-point Int16 scaled to physical units
per property. The twin was DELETED (byte-identical live ISRIC parity proven by the
live drive over a Louisiana AOI: clay/phh2o/soc/bdod all mask + value + nodata + crs
+ transform identical, maxdiff 0; the ocean honesty path agrees on SOILGRIDS_EMPTY).

These OFFLINE tests cover the spec identity + metadata flags (twin-identical), the
param gates (property/depth enum + alias tables, bbox area), the URL templating, the
per-property scale + serialize (synthetic 4326 source), and the coverage / all-nodata
honesty paths.
"""

from __future__ import annotations

import contextlib
import os

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile

from trid3nt_server.data.fetchers._router import router
from trid3nt_server.data.fetchers._router import transport as _tp
from trid3nt_server.data.fetchers._router.executors import raster_cog
from trid3nt_server.data.fetchers._router.executors.raster_cog import _VrtSource
from trid3nt_server.data.fetchers._router.router import synthesize_metadata
from trid3nt_server.data.fetchers._router.spec import compose_specs_from_tree

_BBOX = (-91.30, 30.30, -91.25, 30.35)


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_soilgrids"]


# --------------------------------------------------------------------------- #
# Spec identity + metadata flags (twin-identical; SPEC-IDENTITY rule).
# --------------------------------------------------------------------------- #


def test_spec_identity(spec):
    assert spec.name == "fetch_soilgrids"
    assert spec.shape == "raster-cog"
    assert spec.error_code_prefix == "SOILGRIDS"
    assert spec.input_error_suffix == "INPUT_INVALID"
    assert spec.empty_error_suffix == "EMPTY"
    assert spec.supports_global_query is False
    assert spec.cache.ttl_class == "static-30d"
    assert spec.output.auto_publish is True
    assert spec.ingest["access"] == "projected_vrt_window"
    pw = spec.ingest["projected_window"]
    assert pw["target_res_deg"] == 0.0025
    assert pw["scale_by_param"]["map"] == {
        "clay": 10.0, "sand": 10.0, "silt": 10.0, "soc": 10.0, "bdod": 100.0, "phh2o": 10.0}


def test_metadata_flags_twin_identical(spec):
    m = synthesize_metadata(spec)
    assert m.name == "fetch_soilgrids"
    assert m.ttl_class == "static-30d"
    assert m.source_class == "soilgrids"
    assert m.cacheable is True
    assert m.supports_global_query is False
    assert m.payload_mb_estimator_name == "estimate_payload_mb"


# --------------------------------------------------------------------------- #
# Param gates + enum alias tables.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw,canon", [
    ("clay", "clay"), ("CLAY", "clay"), ("ph", "phh2o"), ("soil_ph", "phh2o"),
    ("organic_carbon", "soc"), ("bulk_density", "bdod"), ("clay_content", "clay"),
])
def test_property_alias_normalization(spec, raw, canon):
    p = router.validate_params(spec, {"bbox": list(_BBOX), "soil_property": raw})
    assert p["soil_property"] == canon


@pytest.mark.parametrize("raw,canon", [
    ("0-5cm", "0-5cm"), ("0-5", "0-5cm"), ("5-15", "5-15cm"), ("100-200 cm", "100-200cm"),
])
def test_depth_alias_normalization(spec, raw, canon):
    p = router.validate_params(spec, {"bbox": list(_BBOX), "depth": raw})
    assert p["depth"] == canon


def test_defaults(spec):
    p = router.validate_params(spec, {"bbox": list(_BBOX)})
    assert p["soil_property"] == "clay" and p["depth"] == "0-5cm"


def test_bad_property_typed_error(spec):
    with pytest.raises(Exception) as ei:
        router.validate_params(spec, {"bbox": list(_BBOX), "soil_property": "gold"})
    assert getattr(ei.value, "error_code", "") == "SOILGRIDS_INPUT_INVALID"


def test_bad_depth_typed_error(spec):
    with pytest.raises(Exception) as ei:
        router.validate_params(spec, {"bbox": list(_BBOX), "depth": "7-9cm"})
    assert getattr(ei.value, "error_code", "") == "SOILGRIDS_INPUT_INVALID"


def test_too_large_bbox_typed_error(spec):
    with pytest.raises(Exception) as ei:
        router.validate_params(spec, {"bbox": [-95.0, 40.0, -93.0, 42.0]})  # 4 deg^2
    assert getattr(ei.value, "error_code", "") == "SOILGRIDS_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# URL templating (property/depth -> the ISRIC VRT object).
# --------------------------------------------------------------------------- #


def test_url_template_fills_property_depth(spec):
    captured = {}

    def fake_get_bytes(client, url, **kw):
        captured["url"] = url
        raise RuntimeError("stop after url capture")

    import trid3nt_server.data.fetchers._router.transport as tp
    orig = tp.get_bytes
    tp.get_bytes = fake_get_bytes
    try:
        with pytest.raises(Exception):
            raster_cog._resolve_multi_url_members(
                spec, {"bbox": list(_BBOX), "soil_property": "phh2o", "depth": "5-15cm"})
    finally:
        tp.get_bytes = orig
    assert captured["url"] == (
        "https://files.isric.org/soilgrids/latest/data/phh2o/phh2o_5-15cm_mean.vrt")


# --------------------------------------------------------------------------- #
# Coverage fast-reject (honesty, no network).
# --------------------------------------------------------------------------- #


def test_antarctica_is_empty(spec):
    with pytest.raises(Exception) as ei:
        raster_cog.execute(spec, {"bbox": [10.0, -80.0, 10.05, -79.95],
                                  "soil_property": "clay", "depth": "0-5cm"})
    assert getattr(ei.value, "error_code", "") == "SOILGRIDS_EMPTY"


# --------------------------------------------------------------------------- #
# Per-property scale + serialize + all-nodata honesty (synthetic 4326 source).
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _synthetic(tmp_path, native_vals, nodata=-32768):
    """Route the projected read through a synthetic 4326 'native' COG (offline).

    Using a 4326 source makes transform_bounds an identity so the reproject is a
    near-identity resample -- this exercises the scale/serialize/honesty surface;
    the real Homolosine projected path is proven by the live drive.
    """
    real = rasterio.open
    h, w = native_vals.shape
    tf = rasterio.transform.from_bounds(-91.35, 30.25, -90.95, 30.60, w, h)
    p = str(tmp_path / f"nat_{os.urandom(4).hex()}.tif")
    with rasterio.open(p, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="int16", crs="EPSG:4326", transform=tf, nodata=nodata) as d:
        d.write(native_vals.astype("int16"), 1)

    src = _VrtSource(p, (0, 0, w, h), (0, 0, w, h))

    def fake_resolve(spec, params):
        return tf, w, h, "EPSG:4326", float(nodata), [src]

    @contextlib.contextmanager
    def fake_open(url):
        with real(url) as s:
            yield s

    orig_res = raster_cog._resolve_multi_url_members
    orig_owc = _tp.open_windowed_cog
    raster_cog._resolve_multi_url_members = fake_resolve
    _tp.open_windowed_cog = fake_open
    try:
        yield
    finally:
        raster_cog._resolve_multi_url_members = orig_res
        _tp.open_windowed_cog = orig_owc


def test_scale_divisor_applied(spec, tmp_path):
    # clay stored g/kg x10 -> /10 -> percent. A stored 325 -> 32.5 %.
    vals = np.full((60, 60), 325, dtype="int16")
    with _synthetic(tmp_path, vals):
        cog = raster_cog.execute(spec, {"bbox": list(_BBOX), "soil_property": "clay", "depth": "0-5cm"})
    with MemoryFile(cog) as m, m.open() as s:
        out = s.read(1)
        assert s.nodata == -9999.0
        valid = out != -9999.0
        assert valid.any()
        assert abs(float(out[valid].mean()) - 32.5) < 1e-3


def test_bdod_uses_100_divisor(spec, tmp_path):
    # bdod stored cg/cm3 x100 -> /100 -> kg/dm3. A stored 134 -> 1.34.
    vals = np.full((50, 50), 134, dtype="int16")
    with _synthetic(tmp_path, vals):
        cog = raster_cog.execute(spec, {"bbox": list(_BBOX), "soil_property": "bdod", "depth": "0-5cm"})
    with MemoryFile(cog) as m, m.open() as s:
        out = s.read(1)
        valid = out != -9999.0
        assert abs(float(out[valid].mean()) - 1.34) < 1e-3


def test_all_nodata_is_empty(spec, tmp_path):
    vals = np.full((40, 40), -32768, dtype="int16")
    with _synthetic(tmp_path, vals):
        with pytest.raises(Exception) as ei:
            raster_cog.execute(spec, {"bbox": list(_BBOX), "soil_property": "clay", "depth": "0-5cm"})
    assert getattr(ei.value, "error_code", "") == "SOILGRIDS_EMPTY"
