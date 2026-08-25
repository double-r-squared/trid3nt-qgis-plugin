"""Router value coverage for the JRC Global Surface Water fold (ADR 0086).

fetch_jrc_global_surface_water folded to source.yaml + the raster_cog
``stac_continuous_mosaic`` access mode (a continuous-value uint8 STAC mosaic:
bilinear + per-band nodata 0/253 + two-tier PC REST /sign) with the PURE per-band
colormap hook (occurrence/recurrence/seasonality/change ramp) baked into the band-1
palette. The twin was DELETED (byte-identical live PC-STAC parity proven by the live
drive over Lake Okeechobee, all 4 bands array + palette + nodata + crs + transform
identical; the dry-AOI honesty path agrees on JRC_GSW_NO_COVERAGE).

These OFFLINE tests cover the spec identity + metadata flags (twin-identical), the
param gates (band enum + lowercase alias, bbox area), the palette bake + first-valid
mosaic value behaviour via synthetic source COGs read through a patched opener, and
the all-nodata / no-item honesty paths.
"""

from __future__ import annotations

import contextlib
import os

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from types import SimpleNamespace

from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router import transport as _tp
from trid3nt_server.tools.fetchers._router.executors import raster_cog
from trid3nt_server.tools.fetchers._router.hooks import jrc_global_surface_water as jrc_hook
from trid3nt_server.tools.fetchers._router.router import synthesize_metadata
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

# Small AOI well inside the 2.0 deg^2 guardrail (Mississippi floodplain, LA).
_BBOX = (-91.30, 30.30, -91.00, 30.55)


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_jrc_global_surface_water"]


# --------------------------------------------------------------------------- #
# Spec identity + metadata flags (twin-identical; SPEC-IDENTITY rule).
# --------------------------------------------------------------------------- #


def test_spec_identity(spec):
    assert spec.name == "fetch_jrc_global_surface_water"
    assert spec.shape == "raster-cog"
    assert spec.error_code_prefix == "JRC_GSW"
    assert spec.empty_error_suffix == "NO_COVERAGE"
    assert spec.supports_global_query is False
    assert spec.cache.ttl_class == "static-30d"          # -> cacheable True
    assert spec.ingest["access"] == "stac_continuous_mosaic"
    assert spec.ingest["native_cell_m"] == 30.0
    assert spec.hooks.colormap == "jrc_global_surface_water.colormap"


def test_metadata_flags_twin_identical(spec):
    m = synthesize_metadata(spec)
    assert m.name == "fetch_jrc_global_surface_water"
    assert m.ttl_class == "static-30d"
    assert m.source_class == "jrc_global_surface_water"
    assert m.cacheable is True
    assert m.supports_global_query is False
    assert m.payload_mb_estimator_name == "estimate_payload_mb"


def test_per_band_nodata_map(spec):
    assert raster_cog._stac_continuous_nodata(spec, {"band": "occurrence"}) == 0
    assert raster_cog._stac_continuous_nodata(spec, {"band": "recurrence"}) == 0
    assert raster_cog._stac_continuous_nodata(spec, {"band": "seasonality"}) == 0
    assert raster_cog._stac_continuous_nodata(spec, {"band": "change"}) == 253


# --------------------------------------------------------------------------- #
# Param gates + enum aliases.
# --------------------------------------------------------------------------- #


def test_bad_band_typed_error(spec):
    with pytest.raises(Exception) as ei:
        router.validate_params(spec, {"bbox": list(_BBOX), "band": "salinity"})
    assert getattr(ei.value, "error_code", "") == "JRC_GSW_BAND_INVALID"


def test_band_lowercase_alias(spec):
    p = router.validate_params(spec, {"bbox": list(_BBOX), "band": "OCCURRENCE"})
    assert p["band"] == "occurrence"


def test_band_defaults_to_occurrence(spec):
    p = router.validate_params(spec, {"bbox": list(_BBOX)})
    assert p["band"] == "occurrence"


def test_too_large_bbox_typed_error(spec):
    with pytest.raises(Exception) as ei:
        router.validate_params(spec, {"bbox": [-92.0, 29.0, -89.0, 31.0]})  # 6 deg^2
    assert getattr(ei.value, "error_code", "") == "JRC_GSW_BBOX_INVALID"


# --------------------------------------------------------------------------- #
# Colormap hook (pure, per-band ramp).
# --------------------------------------------------------------------------- #


def test_colormap_hook_per_band(spec):
    occ = jrc_hook.colormap(spec, {"band": "occurrence"})
    assert occ[0] == (0, 0, 0, 0)               # nodata transparent
    assert occ[100] == (8, 48, 107, 255)        # deep blue at 100%
    chg = jrc_hook.colormap(spec, {"band": "change"})
    assert chg[253] == (0, 0, 0, 0)             # change nodata transparent
    assert chg[100] == (247, 247, 247, 255)     # no-change white
    seas = jrc_hook.colormap(spec, {"band": "seasonality"})
    assert seas[0] == (0, 0, 0, 0)


# --------------------------------------------------------------------------- #
# Mosaic render value behaviour (synthetic source COGs, patched opener).
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _synthetic(items):
    """Route the mosaic read through local synthetic COGs (offline)."""
    real = rasterio.open

    @contextlib.contextmanager
    def fake_open(url):
        with real(url) as s:
            yield s

    class _FakeSearch:
        def items(self_inner):
            return list(items)

    class _FakeClient:
        def search(self_inner, **kw):
            return _FakeSearch()

    import pystac_client

    orig_owc = _tp.open_windowed_cog
    orig_sign = raster_cog._pc_sign_two_tier
    orig_open = pystac_client.Client.open
    _tp.open_windowed_cog = fake_open
    raster_cog._pc_sign_two_tier = lambda spec, href, coll: href
    pystac_client.Client.open = staticmethod(lambda *a, **k: _FakeClient())
    try:
        yield
    finally:
        _tp.open_windowed_cog = orig_owc
        raster_cog._pc_sign_two_tier = orig_sign
        pystac_client.Client.open = orig_open


def _write_cog(tmp_path, value_array, nodata):
    h, w = value_array.shape
    # source covers a superset of _BBOX so the request window is fully covered.
    tf = rasterio.transform.from_bounds(-91.35, 30.25, -90.95, 30.60, w, h)
    p = str(tmp_path / f"src_{os.urandom(4).hex()}.tif")
    with rasterio.open(p, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="uint8", crs="EPSG:4326", transform=tf, nodata=nodata) as d:
        d.write(value_array.astype("uint8"), 1)
    return p


def _item(band, href):
    return SimpleNamespace(id="jrc_fake", bbox=list(_BBOX),
                           assets={band: SimpleNamespace(href=href)})


def test_occurrence_mosaic_bakes_blue_palette(spec, tmp_path):
    arr = np.full((80, 80), 100, dtype="uint8")   # all permanent water
    p = _write_cog(tmp_path, arr, nodata=0)
    with _synthetic([_item("occurrence", p)]):
        cog = raster_cog.execute(spec, {"bbox": list(_BBOX), "band": "occurrence"})
    with MemoryFile(cog) as m, m.open() as s:
        out = s.read(1)
        assert s.nodata == 0
        assert s.colormap(1)[100] == (8, 48, 107, 255)   # baked deep-blue ramp
        assert int(out.max()) == 100 and int(out.min()) == 100


def test_change_mosaic_uses_253_nodata(spec, tmp_path):
    arr = np.full((60, 60), 150, dtype="uint8")   # gain
    p = _write_cog(tmp_path, arr, nodata=253)
    with _synthetic([_item("change", p)]):
        cog = raster_cog.execute(spec, {"bbox": list(_BBOX), "band": "change"})
    with MemoryFile(cog) as m, m.open() as s:
        assert s.nodata == 253
        assert s.colormap(1)[253] == (0, 0, 0, 0)


def test_all_nodata_mosaic_is_no_coverage(spec, tmp_path):
    arr = np.zeros((40, 40), dtype="uint8")        # entirely nodata (dry)
    p = _write_cog(tmp_path, arr, nodata=0)
    with _synthetic([_item("occurrence", p)]):
        with pytest.raises(Exception) as ei:
            raster_cog.execute(spec, {"bbox": list(_BBOX), "band": "occurrence"})
    assert getattr(ei.value, "error_code", "") == "JRC_GSW_NO_COVERAGE"


def test_no_items_is_no_coverage(spec):
    with _synthetic([]):
        with pytest.raises(Exception) as ei:
            raster_cog.execute(spec, {"bbox": list(_BBOX), "band": "occurrence"})
    assert getattr(ei.value, "error_code", "") == "JRC_GSW_NO_COVERAGE"
