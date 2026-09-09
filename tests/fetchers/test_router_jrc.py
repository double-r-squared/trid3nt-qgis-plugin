"""Router value coverage for the JRC Global Surface Water fold (ADR 0086).

fetch_jrc_global_surface_water folded to source.yaml + the stac_raster ``mosaic``
render (a continuous-value uint8 STAC mosaic: bilinear + per-band nodata 0/253,
signed by the planetary-computer SDK) with the PURE per-band colormap hook
(occurrence/recurrence/seasonality/change ramp) baked into the band-1 palette. The twin was DELETED (byte-identical live PC-STAC parity proven by the live
drive over Lake Okeechobee, all 4 bands array + palette + nodata + crs + transform
identical; the dry-AOI honesty path agrees on JRC_GSW_NO_COVERAGE).

These OFFLINE tests cover the spec identity + metadata flags (twin-identical), the
param gates (band enum + lowercase alias, bbox area), the palette bake + first-valid
mosaic value behaviour over local synthetic COGs wrapped in real STAC items, and the
all-nodata / no-item honesty paths.
"""

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
from trid3nt_server.tools.fetchers.hydrology.fetch_jrc_global_surface_water import hooks as jrc_hook
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
    assert spec.ingest["access"] == "stac"
    assert spec.ingest["render"] == "mosaic"
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
    nbp = spec.ingest["mosaic"]["nodata_by_param"]
    assert nbp["param"] == "band"
    assert nbp["map"] == {"occurrence": 0, "recurrence": 0,
                          "seasonality": 0, "change": 253}


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


#: the synthetic sources cover a superset of the request window.
_SRC_BOUNDS = (-91.35, 30.25, -90.95, 30.60)


def _write_cog(tmp_path, value_array, nodata):
    h, w = value_array.shape
    tf = rasterio.transform.from_bounds(*_SRC_BOUNDS, w, h)
    p = str(tmp_path / f"src_{os.urandom(4).hex()}.tif")
    with rasterio.open(p, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="uint8", crs="EPSG:4326", transform=tf, nodata=nodata) as d:
        d.write(value_array.astype("uint8"), 1)
    return p, tf, (h, w)


def _item(band, href, transform, shape):
    item = pystac.Item(
        id="jrc_fake",
        geometry={"type": "Polygon", "coordinates": [[
            [_SRC_BOUNDS[0], _SRC_BOUNDS[1]], [_SRC_BOUNDS[2], _SRC_BOUNDS[1]],
            [_SRC_BOUNDS[2], _SRC_BOUNDS[3]], [_SRC_BOUNDS[0], _SRC_BOUNDS[3]],
            [_SRC_BOUNDS[0], _SRC_BOUNDS[1]]]]},
        bbox=list(_SRC_BOUNDS), datetime=None,
        properties={"datetime": "2021-01-01T00:00:00Z",
                    "start_datetime": "2021-01-01T00:00:00Z",
                    "end_datetime": "2021-01-01T00:00:00Z",
                    "proj:code": "EPSG:4326",
                    "proj:shape": [shape[0], shape[1]],
                    "proj:transform": list(transform)[:6]},
        collection="jrc-gsw",
        stac_extensions=[
            "https://stac-extensions.github.io/projection/v2.0.0/schema.json"],
    )
    item.add_asset(band, pystac.Asset(href=href, media_type=pystac.MediaType.COG,
                                      roles=["data"]))
    return item


def test_occurrence_mosaic_bakes_blue_palette(spec, tmp_path):
    arr = np.full((80, 80), 100, dtype="uint8")   # all permanent water
    p, tf, shape = _write_cog(tmp_path, arr, nodata=0)
    with _catalog([_item("occurrence", p, tf, shape)]):
        cog = stac_raster.execute(spec, {"bbox": list(_BBOX), "band": "occurrence"})
    with MemoryFile(cog) as m, m.open() as s:
        out = s.read(1)
        assert s.nodata == 0
        assert s.colormap(1)[100] == (8, 48, 107, 255)   # baked deep-blue ramp
        assert int(out.max()) == 100 and int(out.min()) == 100


def test_change_mosaic_uses_253_nodata(spec, tmp_path):
    arr = np.full((60, 60), 150, dtype="uint8")   # gain
    p, tf, shape = _write_cog(tmp_path, arr, nodata=253)
    with _catalog([_item("change", p, tf, shape)]):
        cog = stac_raster.execute(spec, {"bbox": list(_BBOX), "band": "change"})
    with MemoryFile(cog) as m, m.open() as s:
        assert s.nodata == 253
        assert s.colormap(1)[253] == (0, 0, 0, 0)


def test_all_nodata_mosaic_is_no_coverage(spec, tmp_path):
    arr = np.zeros((40, 40), dtype="uint8")        # entirely nodata (dry)
    p, tf, shape = _write_cog(tmp_path, arr, nodata=0)
    with _catalog([_item("occurrence", p, tf, shape)]):
        with pytest.raises(Exception) as ei:
            stac_raster.execute(spec, {"bbox": list(_BBOX), "band": "occurrence"})
    assert getattr(ei.value, "error_code", "") == "JRC_GSW_NO_COVERAGE"


def test_no_items_is_no_coverage(spec):
    with _catalog([]):
        with pytest.raises(Exception) as ei:
            stac_raster.execute(spec, {"bbox": list(_BBOX), "band": "occurrence"})
    assert getattr(ei.value, "error_code", "") == "JRC_GSW_NO_COVERAGE"
