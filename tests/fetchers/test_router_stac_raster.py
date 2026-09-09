"""Router value coverage for the stac_raster executor (the STAC fold).

One access mode (``ingest.access: stac``) reads a catalog through
``odc.stac.load`` and serves three renders. These OFFLINE tests drive the whole
executor over LOCAL synthetic COGs wrapped in real STAC items -- the catalog
search is the only thing patched -- so the destination grid, the first-valid
fuse, the DN band math, the palette bake, the scene-select ladder and the typed
empty / refusal paths are exercised as the live path runs them.
"""

from __future__ import annotations

import contextlib

import numpy as np
import pystac
import pytest
import rasterio
from rasterio.io import MemoryFile

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.tools.fetchers._router.executors import stac_raster

_BBOX = (-91.30, 30.30, -91.00, 30.55)
#: the synthetic sources cover a superset of the request window.
_SRC_BOUNDS = (-91.35, 30.25, -90.95, 30.60)


# --------------------------------------------------------------------------- #
# Synthetic catalog: real pystac Items over local COGs.
# --------------------------------------------------------------------------- #


def _write_cog(tmp_path, array, *, dtype, nodata, colormap=None, name="src"):
    h, w = array.shape[-2:]
    path = str(tmp_path / f"{name}.tif")
    tf = rasterio.transform.from_bounds(*_SRC_BOUNDS, w, h)
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype=dtype, crs="EPSG:4326", transform=tf, nodata=nodata) as dst:
        dst.write(array.astype(dtype)[np.newaxis, :, :])
        if colormap is not None:
            dst.write_colormap(1, colormap)
    return path, tf, (h, w)


def _item(item_id, assets, transform, shape, *, dt="2024-06-01T00:00:00Z", props=None):
    item = pystac.Item(
        id=item_id,
        geometry={"type": "Polygon", "coordinates": [[
            [_SRC_BOUNDS[0], _SRC_BOUNDS[1]], [_SRC_BOUNDS[2], _SRC_BOUNDS[1]],
            [_SRC_BOUNDS[2], _SRC_BOUNDS[3]], [_SRC_BOUNDS[0], _SRC_BOUNDS[3]],
            [_SRC_BOUNDS[0], _SRC_BOUNDS[1]]]]},
        bbox=list(_SRC_BOUNDS),
        datetime=None,
        properties={"datetime": dt, "start_datetime": dt, "end_datetime": dt,
                    "proj:code": "EPSG:4326",
                    "proj:shape": [shape[0], shape[1]],
                    "proj:transform": list(transform)[:6],
                    **(props or {})},
        collection="synthetic",
        stac_extensions=["https://stac-extensions.github.io/projection/v2.0.0/schema.json"],
    )
    for key, path in assets.items():
        item.add_asset(key, pystac.Asset(href=path, media_type=pystac.MediaType.COG,
                                         roles=["data"]))
    return item


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


def _spec(ingest, **over) -> SourceSpec:
    base = {
        "schema_version": "v1",
        "name": "fetch_synthetic_stac",
        "source_class": "synthetic_stac",
        "error_prefix": "SYNTH",
        "empty_error_suffix": "NO_COVERAGE",
        "input_error_suffix": "INPUT_INVALID",
        "shape": "raster-cog",
        "endpoints": {"data": {"url": "https://catalog.test/stac"}},
        "auth": {"mode": "none"},
        "params": {"bbox": {"type": "bbox", "required": True}},
        "ingest": ingest,
        "normalize": {"crs": "EPSG:4326"},
        "output": {"layer_type": "raster", "ext": "tif", "role": "input"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 1.0, "floor_mb": 0.1},
        "docstring": "synthetic",
    }
    base.update(over)
    return SourceSpec.model_validate(base)


def _float_ingest(**stac_over):
    stac = {"root": "https://catalog.test/stac", "collection": "synthetic",
            "data_asset": "data", "select": {"mode": "mosaic"}}
    stac.update(stac_over)
    return {"access": "stac", "render": "float", "native_cell_m": 1000.0, "stac": stac}


# --------------------------------------------------------------------------- #
# float render: the DN band math and the fill rule.
# --------------------------------------------------------------------------- #


def test_float_render_applies_scale_offset_and_fill(tmp_path):
    arr = np.full((40, 40), 15000, dtype="uint16")
    arr[18:22, :] = 0                                     # the declared fill DN
    path, tf, shape = _write_cog(tmp_path, arr, dtype="uint16", nodata=0)
    ingest = _float_ingest()
    ingest["transform"] = {"scale": 0.02, "offset": -273.15, "fill_dn": 0}
    with _catalog([_item("i1", {"data": path}, tf, shape)]):
        cog = stac_raster.execute(_spec(ingest), {"bbox": list(_BBOX)})
    with MemoryFile(cog) as m, m.open() as src:
        out = src.read(1)
    finite = out[np.isfinite(out)]
    assert finite.size and np.allclose(finite, 15000 * 0.02 - 273.15, atol=1e-3)
    assert bool(np.isnan(out).any())                      # the fill DN reads back NaN


def test_float_render_log10_db(tmp_path):
    arr = np.full((30, 30), 0.1, dtype="float32")
    path, tf, shape = _write_cog(tmp_path, arr, dtype="float32", nodata=None)
    ingest = _float_ingest()
    ingest["transform"] = {"log10_db": True}
    with _catalog([_item("i1", {"data": path}, tf, shape)]):
        cog = stac_raster.execute(_spec(ingest), {"bbox": list(_BBOX)})
    with MemoryFile(cog) as m, m.open() as src:
        out = src.read(1)
    assert np.allclose(out[np.isfinite(out)], -10.0, atol=1e-3)


def test_float_render_serialize_directive_stamps_sentinel(tmp_path):
    arr = np.full((30, 30), 12.5, dtype="float32")
    arr[13:17, :] = np.nan
    path, tf, shape = _write_cog(tmp_path, arr, dtype="float32", nodata=np.nan)
    ingest = _float_ingest()
    ingest["serialize"] = {"nodata": -9999.0, "dtype": "float32"}
    with _catalog([_item("i1", {"data": path}, tf, shape)]):
        cog = stac_raster.execute(_spec(ingest), {"bbox": list(_BBOX)})
    with MemoryFile(cog) as m, m.open() as src:
        assert src.nodata == -9999.0
        assert (src.read(1) == -9999.0).any()


def test_float_all_fill_window_is_typed_empty(tmp_path):
    arr = np.zeros((30, 30), dtype="uint16")
    path, tf, shape = _write_cog(tmp_path, arr, dtype="uint16", nodata=0)
    ingest = _float_ingest()
    ingest["transform"] = {"scale": 0.02, "offset": 0.0, "fill_dn": 0}
    with _catalog([_item("i1", {"data": path}, tf, shape)]):
        with pytest.raises(Exception) as ei:
            stac_raster.execute(_spec(ingest), {"bbox": list(_BBOX)})
    assert getattr(ei.value, "error_code", "") == "SYNTH_NO_COVERAGE"


def test_no_item_is_typed_empty():
    with _catalog([]):
        with pytest.raises(Exception) as ei:
            stac_raster.execute(_spec(_float_ingest()), {"bbox": list(_BBOX)})
    assert getattr(ei.value, "error_code", "") == "SYNTH_NO_COVERAGE"


# --------------------------------------------------------------------------- #
# The fuse: first-valid, in the supplied order.
# --------------------------------------------------------------------------- #


def test_mosaic_fuse_is_first_valid_in_search_order(tmp_path):
    first = np.full((40, 40), 20, dtype="uint8")
    first[:, 20:] = 0                                     # a gap the second fills
    second = np.full((40, 40), 90, dtype="uint8")
    p1, tf, shape = _write_cog(tmp_path, first, dtype="uint8", nodata=0, name="a")
    p2, _tf2, _s2 = _write_cog(tmp_path, second, dtype="uint8", nodata=0, name="b")
    ingest = {"access": "stac", "render": "mosaic", "native_cell_m": 1000.0,
              "mosaic": {"nodata": 0},
              "stac": {"root": "https://catalog.test/stac", "collection": "synthetic",
                       "data_asset": "data", "resampling": "nearest",
                       "select": {"mode": "mosaic"}}}
    items = [_item("first", {"data": p1}, tf, shape),
             _item("second", {"data": p2}, tf, shape, dt="2020-01-01T00:00:00Z")]
    with _catalog(items):
        cog = stac_raster.execute(_spec(ingest), {"bbox": list(_BBOX)})
    with MemoryFile(cog) as m, m.open() as src:
        out = src.read(1)
        assert src.nodata == 0
    assert set(np.unique(out)) == {20, 90}
    assert int(out[:, 0].max()) == 20                     # the earlier item wins


def test_mosaic_bakes_the_spec_palette(tmp_path):
    arr = np.full((30, 30), 7, dtype="uint8")
    path, tf, shape = _write_cog(tmp_path, arr, dtype="uint8", nodata=0)
    ingest = {"access": "stac", "render": "mosaic", "native_cell_m": 1000.0,
              "mosaic": {"nodata": 0},
              "stac": {"root": "https://catalog.test/stac", "collection": "synthetic",
                       "data_asset": "data", "resampling": "nearest",
                       "select": {"mode": "mosaic"}}}
    spec = _spec(ingest, hooks={"colormap": "jrc_global_surface_water.colormap"})
    with _catalog([_item("i1", {"data": path}, tf, shape)]):
        arr_out, _tf, _crs, cmap = stac_raster.stac_to_mosaic(spec, {"bbox": list(_BBOX),
                                                                    "band": "occurrence"})
    assert int(arr_out.max()) == 7
    assert cmap is not None and cmap[0] == (0, 0, 0, 0)


def test_mosaic_passthrough_palette_comes_from_the_source(tmp_path):
    arr = np.full((30, 30), 3, dtype="uint8")
    palette = {i: (i, 0, 0, 255) for i in range(256)}
    path, tf, shape = _write_cog(tmp_path, arr, dtype="uint8", nodata=0, colormap=palette)
    ingest = {"access": "stac", "render": "mosaic", "native_cell_m": 1000.0,
              "palette": "passthrough", "mosaic": {"nodata": 0},
              "stac": {"root": "https://catalog.test/stac", "collection": "synthetic",
                       "data_asset": "data", "resampling": "nearest",
                       "select": {"mode": "mosaic"}}}
    with _catalog([_item("i1", {"data": path}, tf, shape)]):
        _arr, _tf, _crs, cmap = stac_raster.stac_to_mosaic(_spec(ingest), {"bbox": list(_BBOX)})
    assert cmap is not None and cmap[3] == (3, 0, 0, 255)


def test_mosaic_all_nodata_is_typed_empty(tmp_path):
    arr = np.zeros((30, 30), dtype="uint8")
    path, tf, shape = _write_cog(tmp_path, arr, dtype="uint8", nodata=0)
    ingest = {"access": "stac", "render": "mosaic", "native_cell_m": 1000.0,
              "mosaic": {"nodata": 0},
              "stac": {"root": "https://catalog.test/stac", "collection": "synthetic",
                       "data_asset": "data", "select": {"mode": "mosaic"}}}
    with _catalog([_item("i1", {"data": path}, tf, shape)]):
        with pytest.raises(Exception) as ei:
            stac_raster.execute(_spec(ingest), {"bbox": list(_BBOX)})
    assert getattr(ei.value, "error_code", "") == "SYNTH_NO_COVERAGE"


# --------------------------------------------------------------------------- #
# Scene select.
# --------------------------------------------------------------------------- #


def test_select_latest_takes_the_most_recent_item(tmp_path):
    old = np.full((20, 20), 5, dtype="float32")
    new = np.full((20, 20), 9, dtype="float32")
    p_old, tf, shape = _write_cog(tmp_path, old, dtype="float32", nodata=None, name="old")
    p_new, _t, _s = _write_cog(tmp_path, new, dtype="float32", nodata=None, name="new")
    ingest = _float_ingest(select={"mode": "latest"})
    items = [_item("old", {"data": p_old}, tf, shape, dt="2020-01-01T00:00:00Z"),
             _item("new", {"data": p_new}, tf, shape, dt="2024-06-01T00:00:00Z")]
    with _catalog(items):
        cog = stac_raster.execute(_spec(ingest), {"bbox": list(_BBOX)})
    with MemoryFile(cog) as m, m.open() as src:
        assert np.allclose(src.read(1), 9.0)


def test_select_rank_prefers_the_least_cloudy_scene(tmp_path):
    cloudy = np.full((20, 20), 1, dtype="float32")
    clear = np.full((20, 20), 4, dtype="float32")
    p_c, tf, shape = _write_cog(tmp_path, cloudy, dtype="float32", nodata=None, name="c")
    p_k, _t, _s = _write_cog(tmp_path, clear, dtype="float32", nodata=None, name="k")
    ingest = _float_ingest(select={"mode": "best", "rank": [{"by": "cloud_cover"}]})
    items = [_item("cloudy", {"data": p_c}, tf, shape, props={"eo:cloud_cover": 80.0}),
             _item("clear", {"data": p_k}, tf, shape, props={"eo:cloud_cover": 2.0})]
    with _catalog(items):
        cog = stac_raster.execute(_spec(ingest), {"bbox": list(_BBOX)})
    with MemoryFile(cog) as m, m.open() as src:
        assert np.allclose(src.read(1), 4.0)


def test_select_coverage_skips_scenes_without_the_asset(tmp_path):
    arr = np.full((20, 20), 3, dtype="float32")
    path, tf, shape = _write_cog(tmp_path, arr, dtype="float32", nodata=None)
    ingest = _float_ingest(select={"mode": "coverage"})
    with _catalog([_item("other", {"elsewhere": path}, tf, shape)]):
        with pytest.raises(Exception) as ei:
            stac_raster.execute(_spec(ingest), {"bbox": list(_BBOX)})
    assert getattr(ei.value, "error_code", "") == "SYNTH_NO_COVERAGE"


# --------------------------------------------------------------------------- #
# The destination grid.
# --------------------------------------------------------------------------- #


def test_native_lattice_snaps_to_the_source_origin(tmp_path):
    arr = np.zeros((40, 40), dtype="float32")
    path, tf, shape = _write_cog(tmp_path, arr, dtype="float32", nodata=None)
    # a source whose cell is not the asked density falls back to the metric grid
    ingest = _float_ingest()
    ingest["px_per_deg"] = 3600
    with _catalog([_item("i1", {"data": path}, tf, shape)]):
        gb, resampling = stac_raster._geobox(
            _spec(ingest), {"bbox": list(_BBOX)},
            [_item("i1", {"data": path}, tf, shape)])
    assert gb is not None
    assert resampling == "bilinear"          # the ask does not describe the data

    fine = np.zeros((900, 900), dtype="float32")
    p2, _tf2, _shape2 = _write_cog(tmp_path, fine, dtype="float32", nodata=None, name="fine")
    item = _item("fine", {"data": p2}, rasterio.transform.from_origin(
        -91.5, 30.75, 1 / 3600, 1 / 3600), (900, 900))
    with _catalog([item]):
        gb, resampling = stac_raster._geobox(_spec(ingest), {"bbox": list(_BBOX)}, [item])
    assert resampling == "nearest"
    assert abs(abs(gb.transform.a) - 1 / 3600) < 1e-12
    # every destination edge sits on the source lattice
    assert abs(((gb.transform.c + 91.5) * 3600) % 1.0) < 1e-6


def test_pixel_cap_falls_back_to_the_metric_grid(tmp_path):
    arr = np.zeros((20, 20), dtype="float32")
    path, tf, shape = _write_cog(tmp_path, arr, dtype="float32", nodata=None)
    ingest = _float_ingest()
    ingest["px_max"] = 64
    item = _item("i1", {"data": path}, rasterio.transform.from_origin(
        -91.5, 30.75, 1 / 3600, 1 / 3600), (900, 900))
    gb, resampling = stac_raster._geobox(_spec(ingest), {"bbox": list(_BBOX),
                                                        "px_per_deg": 3600}, [item])
    assert resampling == "bilinear" and max(gb.shape) <= 64


# --------------------------------------------------------------------------- #
# The refusal that names what is missing.
# --------------------------------------------------------------------------- #


def test_netrc_gated_catalog_refuses_by_name(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    ingest = _float_ingest(sign="netrc:urs.earthdata.nasa.gov")
    with pytest.raises(Exception) as ei:
        stac_raster.execute(_spec(ingest), {"bbox": list(_BBOX)})
    msg = str(ei.value)
    assert "urs.earthdata.nasa.gov" in msg and ".netrc" in msg
    assert getattr(ei.value, "error_code", "") == "SYNTH_UPSTREAM_ERROR"


def test_netrc_gated_catalog_proceeds_once_the_entry_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".netrc").write_text(
        "machine urs.earthdata.nasa.gov login u password p\n")
    (tmp_path / ".netrc").chmod(0o600)
    arr = np.full((20, 20), 2.0, dtype="float32")
    path, tf, shape = _write_cog(tmp_path, arr, dtype="float32", nodata=None)
    ingest = _float_ingest(sign="netrc:urs.earthdata.nasa.gov")
    with _catalog([_item("i1", {"data": path}, tf, shape)]):
        cog = stac_raster.execute(_spec(ingest), {"bbox": list(_BBOX)})
    with MemoryFile(cog) as m, m.open() as src:
        assert np.allclose(src.read(1), 2.0)


# --------------------------------------------------------------------------- #
# Param normalization: the collection / asset maps and their aliases.
# --------------------------------------------------------------------------- #


def _param_keyed_ingest():
    return {
        "access": "stac", "render": "float", "native_cell_m": 1000.0,
        "stac": {"root": "https://catalog.test/stac", "param_error_suffix": "PARAM_INVALID",
                 "sign": "none", "select": {"mode": "latest"},
                 "collection_by_param": {"param": "product",
                                         "map": {"11A2": "modis-11A2-061",
                                                 "21A2": "modis-21A2-061"}},
                 "asset_by_params": {"params": ["product", "daynight"],
                                     "map": {"11A2": {"day": "LST_Day_1km",
                                                      "night": "LST_Night_1km"},
                                             "21A2": {"day": "LST_Day_1KM",
                                                      "night": "LST_Night_1KM"}}},
                 "product_aliases": {"mod11a2": "11A2", "11a2": "11A2", "21a2": "21A2"},
                 "daynight_aliases": {"day": "day", "d": "day", "night": "night", "n": "night"}},
        "transform": {"scale": 0.02, "offset": -273.15, "fill_dn": 0},
    }


def _param_keyed_spec():
    return _spec(_param_keyed_ingest(), params={
        "bbox": {"type": "bbox", "required": True, "error_suffix": "BBOX_INVALID"},
        "product": {"type": "str", "default": "11A2"},
        "daynight": {"type": "str", "default": "day"},
    })


def test_alias_table_maps_and_validates():
    spec = _param_keyed_spec()
    al = {"mod11a2": "11A2", "11a2": "11A2"}
    assert stac_raster._normalize_via_aliases(
        spec, "MOD11A2", al, ["11A2", "21A2"], "PARAM_INVALID") == "11A2"
    # canonical passthrough via the upper() fallback
    assert stac_raster._normalize_via_aliases(
        spec, "21A2", al, ["11A2", "21A2"], "PARAM_INVALID") == "21A2"
    with pytest.raises(Exception) as ei:
        stac_raster._normalize_via_aliases(
            spec, "not_real", al, ["11A2", "21A2"], "PARAM_INVALID")
    assert getattr(ei.value, "error_code", "") == "SYNTH_PARAM_INVALID"


def test_unknown_product_is_typed_param_error():
    spec = _param_keyed_spec()
    with pytest.raises(Exception) as ei:
        stac_raster.execute(spec, {"bbox": list(_BBOX), "product": "not_real",
                                   "daynight": "day"})
    assert getattr(ei.value, "error_code", "") == "SYNTH_PARAM_INVALID"


def test_unknown_daynight_is_typed_param_error(tmp_path):
    arr = np.full((20, 20), 1000, dtype="uint16")
    path, tf, shape = _write_cog(tmp_path, arr, dtype="uint16", nodata=0)
    spec = _param_keyed_spec()
    with _catalog([_item("i1", {"LST_Day_1km": path}, tf, shape)]):
        with pytest.raises(Exception) as ei:
            stac_raster.execute(spec, {"bbox": list(_BBOX), "product": "11A2",
                                       "daynight": "dusk"})
    assert getattr(ei.value, "error_code", "") == "SYNTH_PARAM_INVALID"


def test_param_keyed_collection_and_asset_resolve(tmp_path):
    arr = np.full((20, 20), 15000, dtype="uint16")
    path, tf, shape = _write_cog(tmp_path, arr, dtype="uint16", nodata=0)
    spec = _param_keyed_spec()
    with _catalog([_item("i1", {"LST_Night_1KM": path}, tf, shape)]):
        cog = stac_raster.execute(spec, {"bbox": list(_BBOX), "product": "21a2",
                                         "daynight": "n"})
    with MemoryFile(cog) as m, m.open() as src:
        assert np.allclose(src.read(1)[np.isfinite(src.read(1))],
                           15000 * 0.02 - 273.15, atol=1e-3)


def test_asset_suffix_matches_a_numbered_key(tmp_path):
    arr = np.full((20, 20), 1, dtype="uint8")
    path, tf, shape = _write_cog(tmp_path, arr, dtype="uint8", nodata=255)
    ingest = {"access": "stac", "render": "mosaic", "native_cell_m": 30.0,
              "mosaic": {"nodata": 255},
              "stac": {"root": "https://catalog.test/stac", "collection": "synthetic",
                       "asset_suffix": "_B01_WTR", "resampling": "nearest",
                       "select": {"mode": "latest"}}}
    with _catalog([_item("i1", {"0_B01_WTR": path}, tf, shape)]):
        arr_out, _tf, _crs, _cm = stac_raster.stac_to_mosaic(_spec(ingest),
                                                             {"bbox": list(_BBOX)})
    assert int(arr_out.max()) == 1


# --------------------------------------------------------------------------- #
# The honesty floor: a read failure is the spec's typed error, carrying what
# the library said.
# --------------------------------------------------------------------------- #


def test_a_failed_asset_read_is_the_specs_typed_upstream_error(tmp_path):
    arr = np.zeros((20, 20), dtype="float32")
    path, tf, shape = _write_cog(tmp_path, arr, dtype="float32", nodata=None)
    item = _item("i1", {"data": path}, tf, shape)
    import os

    os.unlink(path)                                       # the asset is gone
    with _catalog([item]):
        with pytest.raises(Exception) as ei:
            stac_raster.execute(_spec(_float_ingest()), {"bbox": list(_BBOX)})
    assert getattr(ei.value, "error_code", "") == "SYNTH_UPSTREAM_ERROR"
    assert getattr(ei.value, "retryable", None) is True
    assert "asset read failed" in str(ei.value)
    assert path in str(ei.value)                          # what the library said


def test_a_missing_asset_names_the_keys_the_item_does_carry(tmp_path):
    arr = np.zeros((20, 20), dtype="float32")
    path, tf, shape = _write_cog(tmp_path, arr, dtype="float32", nodata=None)
    with _catalog([_item("i1", {"elsewhere": path}, tf, shape)]):
        with pytest.raises(Exception) as ei:
            stac_raster.execute(_spec(_float_ingest()), {"bbox": list(_BBOX)})
    assert getattr(ei.value, "error_code", "") == "SYNTH_UPSTREAM_ERROR"
    assert "elsewhere" in str(ei.value) and "data" in str(ei.value)
