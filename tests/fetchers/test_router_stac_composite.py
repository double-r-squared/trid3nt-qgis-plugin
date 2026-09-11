"""The multi-asset RGB composite render over the catalog raster mode.

One composite path serves several bands under a joint stretch behind a quality
mask, a colormapped single band, or a raw passthrough. Offline these cover the
spec identity, the area and combination gates, and the render's value behaviour
over LOCAL synthetic COGs wrapped in real catalog items."""

from __future__ import annotations

import contextlib
import os

import numpy as np
import pystac
import pytest
import rasterio
from rasterio.io import MemoryFile

from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.executors import stac_raster
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

_BBOX = (-95.40, 29.70, -95.30, 29.80)
_SW = _SH = 160
_STX = rasterio.transform.from_bounds(-95.45, 29.65, -95.25, 29.85, _SW, _SH)
_GEOM = {"type": "Polygon", "coordinates": [[[-95.45, 29.65], [-95.25, 29.65],
         [-95.25, 29.85], [-95.45, 29.85], [-95.45, 29.65]]]}


@pytest.fixture(scope="module")
def specs():
    return compose_specs_from_tree()




@pytest.mark.parametrize("name,prefix,empty,cell", [
    ("fetch_landsat_imagery", "LANDSAT", "NO_IMAGERY", 30.0),
    ("fetch_sentinel2_truecolor", "S2_TRUECOLOR", "NO_IMAGERY", 10.0),
    ("fetch_naip", "NAIP", "NO_COVERAGE", 1.0),
])
def test_spec_identity(specs, name, prefix, empty, cell):
    s = specs[name]
    assert s.name == name
    assert s.shape == "raster-cog"
    assert s.error_code_prefix == prefix
    assert s.empty_error_suffix == empty
    assert s.supports_global_query is False        # twin metadata flag
    assert s.cache.ttl_class == "static-30d"        # -> cacheable True
    assert s.output.emit_bbox is False              # twins omit LayerURI.bbox
    assert s.ingest["access"] == "stac"
    assert s.ingest["render"] == "rgb"
    assert s.ingest["native_cell_m"] == cell


def test_landsat_role_and_units_split(specs):
    """thermal LST is role=primary + deg-C units; RGB combos are context + no units."""
    s = specs["fetch_landsat_imagery"]
    thermal = router.build_layer_uri(
        s, router.validate_params(s, {"bbox": list(_BBOX), "band_combo": "thermal"}), "s3://x/t.tif")
    assert thermal.role == "primary"
    assert thermal.units == "Land-surface temperature (deg C)"
    assert thermal.style["kind"] == "continuous"
    rgb = router.build_layer_uri(
        s, router.validate_params(s, {"bbox": list(_BBOX), "band_combo": "true_color"}), "s3://x/r.tif")
    assert rgb.role == "context"
    assert rgb.units is None
    assert rgb.style["kind"] == "continuous"




@pytest.mark.parametrize("name,bad_bbox,code", [
    ("fetch_landsat_imagery", [-100.0, 30.0, -98.0, 32.0], "LANDSAT_BBOX_INVALID"),
    ("fetch_sentinel2_truecolor", [-100.0, 30.0, -98.0, 32.0], "S2_TRUECOLOR_BBOX_INVALID"),
    ("fetch_naip", [-95.5, 29.5, -95.0, 30.0], "NAIP_BBOX_INVALID"),  # 0.25 deg^2 > 0.06
])
def test_bbox_area_gate(specs, name, bad_bbox, code):
    with pytest.raises(Exception) as ei:
        router.validate_params(specs[name], {"bbox": bad_bbox})
    assert getattr(ei.value, "error_code", "") == code


@pytest.mark.parametrize("raw,canon", [
    ("true_color", "true_color"), ("rgb", "true_color"), ("natural", "true_color"),
    ("cir", "false_color_nir"), ("false_color", "false_color_nir"),
    ("lst", "thermal"), ("THERMAL", "thermal"), ("surface_temperature", "thermal"),
])
def test_band_combo_alias_normalization(specs, raw, canon):
    p = router.validate_params(specs["fetch_landsat_imagery"], {"bbox": list(_BBOX), "band_combo": raw})
    assert p["band_combo"] == canon


def test_bad_band_combo_typed_error(specs):
    with pytest.raises(Exception) as ei:
        router.validate_params(specs["fetch_landsat_imagery"], {"bbox": list(_BBOX), "band_combo": "sepia"})
    assert getattr(ei.value, "error_code", "") == "LANDSAT_BAND_COMBO_INVALID"




@contextlib.contextmanager
def _catalog(items):
    """Answer the executor's catalog search with ``items`` (no network)."""
    import pystac_client

    class _Search:
        def items(self):
            return list(items)

    class _Client:
        def search(self, **kw):
            return _Search()

    orig = pystac_client.Client.open
    pystac_client.Client.open = staticmethod(lambda *a, **k: _Client())
    try:
        yield
    finally:
        pystac_client.Client.open = orig


def _write_cog(tmp_path, bands, dtype, nodata=None):
    p = str(tmp_path / f"src_{os.urandom(4).hex()}.tif")
    prof = dict(driver="GTiff", height=_SH, width=_SW, count=len(bands), dtype=dtype,
                crs="EPSG:4326", transform=_STX)
    if nodata is not None:
        prof["nodata"] = nodata
    with rasterio.open(p, "w", **prof) as dst:
        for i, b in enumerate(bands):
            dst.write(b.astype(dtype), i + 1)
    return p


def _item(item_id, assets, props=None, band_counts=None):
    """A real STAC item over local COGs, carrying the projection metadata the
    loader reads the source grid from."""
    item = pystac.Item(
        id=item_id, geometry=_GEOM, bbox=[-95.45, 29.65, -95.25, 29.85],
        datetime=None,
        properties={"datetime": "2024-06-01T00:00:00Z",
                    "start_datetime": "2024-06-01T00:00:00Z",
                    "end_datetime": "2024-06-01T00:00:00Z",
                    "proj:code": "EPSG:4326",
                    "proj:shape": [_SH, _SW],
                    "proj:transform": list(_STX)[:6],
                    **(props or {})},
        collection="synthetic",
        stac_extensions=[
            "https://stac-extensions.github.io/projection/v2.0.0/schema.json"],
    )
    for key, path in assets.items():
        item.add_asset(key, pystac.Asset(href=path, media_type=pystac.MediaType.COG,
                                         roles=["data"]))
        n = (band_counts or {}).get(key)
        if n:
            item.assets[key].extra_fields["eo:bands"] = [{} for _ in range(n)]
    return item


def _decode(cog):
    assert cog[:4] in (b"II*\x00", b"MM\x00*")
    with MemoryFile(cog) as m, m.open() as s:
        return s.read(), str(s.dtypes[0]), s.colorinterp[0]


def test_s2_truecolor_render(specs, tmp_path):
    r = np.random.default_rng(1)
    red = r.integers(200, 3000, (_SH, _SW)).astype("uint16")
    grn = r.integers(200, 3000, (_SH, _SW)).astype("uint16")
    blu = r.integers(200, 3000, (_SH, _SW)).astype("uint16")
    scl = np.full((_SH, _SW), 4, dtype="uint16")
    scl[:, :80] = 9  # west-half cloud (source cols 0:80 -> the bbox west edge)
    p_r = _write_cog(tmp_path, [red], "uint16"); p_g = _write_cog(tmp_path, [grn], "uint16")
    p_b = _write_cog(tmp_path, [blu], "uint16"); p_s = _write_cog(tmp_path, [scl], "uint16")
    item = _item("s2", {"B04": p_r, "B03": p_g, "B02": p_b, "SCL": p_s},
                 props={"eo:cloud_cover": 5.0})
    with _catalog([item]):
        cog = stac_raster.execute(specs["fetch_sentinel2_truecolor"],
                                  {"bbox": list(_BBOX), "max_cloud_cover": 30.0})
    arr, dt, ci0 = _decode(cog)
    assert arr.shape[0] == 3 and dt == "uint8"
    assert ci0 == rasterio.enums.ColorInterp.red
    assert int(arr.max()) == 255 and int(arr.min()) == 0     # joint stretch spans full range
    assert int(arr[:, :, :5].max()) == 0                     # west cloud edge zeroed
    assert int(arr[:, :, -5:].max()) > 0                     # east clear edge has data


def test_landsat_thermal_ramp(specs, tmp_path):
    r = np.random.default_rng(3)
    thm = r.integers(30000, 50000, (_SH, _SW)).astype("uint16")
    qa = np.zeros((_SH, _SW), dtype="uint16"); qa[:, :80] = (1 << 3)  # west-half cloud bit
    p_t = _write_cog(tmp_path, [thm], "uint16"); p_q = _write_cog(tmp_path, [qa], "uint16")
    item = _item("ls", {"lwir11": p_t, "qa_pixel": p_q},
                 props={"eo:cloud_cover": 3.0, "platform": "landsat-9"})
    with _catalog([item]):
        cog = stac_raster.execute(specs["fetch_landsat_imagery"],
                                  {"bbox": list(_BBOX), "band_combo": "thermal",
                                   "max_cloud_cover": 30.0, "include_legacy_landsat": False})
    arr, dt, ci0 = _decode(cog)
    assert arr.shape[0] == 3 and dt == "uint8"
    assert int(arr[:, :, :5].max()) == 0                     # west cloud edge zeroed
    assert int(arr[:, :, -5:].max()) > 0                     # east clear edge ramped


def test_naip_passthrough(specs, tmp_path):
    r = np.random.default_rng(2)
    img = [r.integers(1, 256, (_SH, _SW)).astype("uint8") for _ in range(4)]  # RGBN
    p_i = _write_cog(tmp_path, img, "uint8")
    item = _item("naip", {"image": p_i}, band_counts={"image": 4})
    with _catalog([item]):
        cog = stac_raster.execute(specs["fetch_naip"], {"bbox": list(_BBOX)})
    arr, dt, ci0 = _decode(cog)
    assert arr.shape[0] == 3 and dt == "uint8"
    assert int(arr.max()) > 0                                # RGB passthrough, not all-black


def test_naip_all_black_is_no_coverage(specs, tmp_path):
    img = [np.zeros((_SH, _SW), dtype="uint8") for _ in range(4)]
    p_i = _write_cog(tmp_path, img, "uint8")
    item = _item("naip", {"image": p_i}, band_counts={"image": 4})
    with _catalog([item]):
        with pytest.raises(Exception) as ei:
            stac_raster.execute(specs["fetch_naip"], {"bbox": list(_BBOX)})
    assert getattr(ei.value, "error_code", "") == "NAIP_NO_COVERAGE"


def test_s2_all_cloud_is_no_imagery(specs, tmp_path):
    r = np.random.default_rng(9)
    band = lambda: r.integers(200, 3000, (_SH, _SW)).astype("uint16")
    scl = np.full((_SH, _SW), 9, dtype="uint16")  # every pixel high-prob cloud
    p_r = _write_cog(tmp_path, [band()], "uint16"); p_g = _write_cog(tmp_path, [band()], "uint16")
    p_b = _write_cog(tmp_path, [band()], "uint16"); p_s = _write_cog(tmp_path, [scl], "uint16")
    item = _item("s2", {"B04": p_r, "B03": p_g, "B02": p_b, "SCL": p_s},
                 props={"eo:cloud_cover": 5.0})
    with _catalog([item]):
        with pytest.raises(Exception) as ei:
            stac_raster.execute(specs["fetch_sentinel2_truecolor"], {"bbox": list(_BBOX)})
    assert getattr(ei.value, "error_code", "") == "S2_TRUECOLOR_NO_IMAGERY"
