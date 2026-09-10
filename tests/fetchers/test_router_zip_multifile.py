"""Offline coverage for the zip and multi-file fold.

The fixed tile grid's math with its whole-object per-tile extract, window and
merge, and the request planner with the zip-member read the vector executor does.
The raster half stubs the shared archive step; the vector half points the driver
at real local zips, so the machinery runs deterministically."""

from __future__ import annotations

import io
import os
import zipfile

import numpy as np
import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterError
from trid3nt_server.tools.fetchers._router.executors import raster_cog, vector_ogr
from trid3nt_server.tools.fetchers.socioeconomic.fetch_administrative_boundaries import hooks as adm
from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers._router.transport import TransportNotFound


@pytest.fixture(scope="module")
def specs():
    return compose_specs_from_tree()


# --------------------------------------------------------------------------- #
# GHSL fixed_tile_grid
# --------------------------------------------------------------------------- #


def _grid_cfg(specs):
    return (specs["fetch_ghsl_population"].ingest or {})["fixed_tile_grid"]


def test_ghsl_grid_math_known_cities(specs):
    g = _grid_cfg(specs)
    # Lagos -> R9_C19 (single tile), Mexico City -> R7_C9, London -> two tiles.
    assert raster_cog._tile_grid_tiles((3.10, 6.35, 3.70, 6.75), g) == [(9, 19)]
    assert raster_cog._tile_grid_tiles((-99.3, 19.2, -98.9, 19.6), g) == [(7, 9)]
    assert raster_cog._tile_grid_tiles((-0.2, 51.3, 0.1, 51.6), g) == [(4, 18), (4, 19)]


def test_ghsl_grid_math_cross_tile(specs):
    g = _grid_cfg(specs)
    tiles = raster_cog._tile_grid_tiles((9.8, 5.0, 10.2, 5.5), g)
    assert len(tiles) >= 2  # straddles the C19/C20 seam


def _tile_bounds(r, c, g):
    left = -180.0 + (c - 1) * g["tile_deg"] + g["lon_offset"]
    top = 90.0 - (r - 1) * g["tile_deg"] + g["top_offset"]
    return left, top - g["tile_deg"], left + g["tile_deg"], top


def _make_tile_zip(r, c, g, value):
    """A synthetic 100x100 GeoTIFF for tile (r, c), zipped under its member name."""
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import from_bounds

    w, s, e, n = _tile_bounds(r, c, g)
    transform = from_bounds(w, s, e, n, 100, 100)
    arr = np.full((100, 100), value, dtype="float32")
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", height=100, width=100, count=1, dtype="float32",
                     crs="EPSG:4326", transform=transform) as ds:
            ds.write(arr, 1)
        tif_bytes = mf.read()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(g["member_template"].format(r=r, c=c), tif_bytes)
    buf.seek(0)
    return zipfile.ZipFile(buf)


def _patch_get_zip(monkeypatch, fn):
    monkeypatch.setattr(
        "trid3nt_server.tools.fetchers._router.transport.get_zip", fn
    )


def test_ghsl_fixed_tile_grid_happy(specs, monkeypatch):
    spec = specs["fetch_ghsl_population"]
    g = _grid_cfg(specs)
    bbox = (3.30, 6.45, 3.45, 6.60)  # Lagos, inside R9_C19
    (r, c) = raster_cog._tile_grid_tiles(bbox, g)[0]
    _patch_get_zip(monkeypatch, lambda client, url, headers=None: _make_tile_zip(r, c, g, 42.0))
    arr, transform, crs = raster_cog._fixed_tile_grid_to_array(spec, {"bbox": list(bbox)})
    assert arr.dtype == np.dtype("float32")
    assert np.isfinite(arr).all()
    assert float(np.nanmean(arr)) == pytest.approx(42.0)
    assert str(crs) == "EPSG:4326"


def test_ghsl_all_negative_tile_is_empty(specs, monkeypatch):
    spec = specs["fetch_ghsl_population"]
    g = _grid_cfg(specs)
    bbox = (3.30, 6.45, 3.45, 6.60)
    (r, c) = raster_cog._tile_grid_tiles(bbox, g)[0]
    # A negative source fill collapses to NaN (twin's `arr < 0 -> nan`) -> EMPTY.
    _patch_get_zip(monkeypatch, lambda client, url, headers=None: _make_tile_zip(r, c, g, -200.0))
    with pytest.raises(RouterError) as ei:
        raster_cog._fixed_tile_grid_to_array(spec, {"bbox": list(bbox)})
    assert ei.value.error_code == "GHSL_POPULATION_EMPTY"


def test_ghsl_missing_tile_is_empty(specs, monkeypatch):
    spec = specs["fetch_ghsl_population"]
    bbox = (3.30, 6.45, 3.45, 6.60)

    def _boom(client, url, headers=None):
        raise TransportNotFound("404")

    _patch_get_zip(monkeypatch, _boom)
    with pytest.raises(RouterError) as ei:
        raster_cog._fixed_tile_grid_to_array(spec, {"bbox": list(bbox)})
    assert ei.value.error_code == "GHSL_POPULATION_EMPTY"


def test_ghsl_input_errors(specs):
    spec = specs["fetch_ghsl_population"]
    with pytest.raises(RouterError) as ei:
        router.validate_params(spec, {"bbox": [3.3, 6.4, 3.3, 6.5]})  # degenerate
    assert ei.value.error_code == "GHSL_POPULATION_INPUT_INVALID"
    with pytest.raises(RouterError) as ei:
        router.validate_params(spec, {"bbox": [3.3, 6.4, 3.45, 6.6], "epoch": 2025})
    assert ei.value.error_code == "GHSL_POPULATION_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# admin_boundaries build_request planner + FIPS
# --------------------------------------------------------------------------- #


def test_admin_state_fips_for_bbox():
    # Lee County FL -> state 12 (Florida) only.
    assert adm._state_fips_for_bbox((-82.2, 26.3, -81.5, 26.8)) == ["12"]
    # Western Aleutians (positive lon) route to AK via the antimeridian tail.
    assert "02" in adm._state_fips_for_bbox((173.0, 52.0, 174.0, 52.5))


def test_admin_build_request_nationwide(specs):
    spec = specs["fetch_administrative_boundaries"]
    for level, frag in [("state", "us_state"), ("county", "us_county"), ("zcta", "us_zcta520")]:
        plans = adm.build_request(spec, {"level": level, "bbox": (-82.2, 26.3, -81.5, 26.8)})
        assert len(plans) == 1 and frag in plans[0].url


def test_admin_build_request_place_fanout(specs):
    spec = specs["fetch_administrative_boundaries"]
    plans = adm.build_request(spec, {"level": "place", "bbox": (-82.2, 26.3, -81.5, 26.8)})
    assert len(plans) == 1 and "tl_2024_12_place.zip" in plans[0].url


def test_admin_place_not_routable_raises_level_invalid(specs):
    spec = specs["fetch_administrative_boundaries"]
    with pytest.raises(RouterError) as ei:
        adm.build_request(spec, {"level": "place", "bbox": (-140.0, 20.0, -139.9, 20.1)})
    assert ei.value.error_code == "ADMIN_BOUNDARY_LEVEL_INVALID"


def test_admin_bad_level_enum(specs):
    spec = specs["fetch_administrative_boundaries"]
    with pytest.raises(RouterError) as ei:
        router.validate_params(spec, {"level": "galaxy", "bbox": [-82.2, 26.3, -81.5, 26.8]})
    assert ei.value.error_code == "ADMIN_BOUNDARY_LEVEL_INVALID"


# --------------------------------------------------------------------------- #
# The ZIP-member read (real local shapefile ZIPs, read through the driver)
# --------------------------------------------------------------------------- #


def _shapefile_zip(dir_path, polys):
    """A real shapefile ZIP on disk (GEOID/NAME + polygons); returns its path."""
    import geopandas as gpd
    from shapely.geometry import box as shp_box

    gdf = gpd.GeoDataFrame(
        {"GEOID": [p[0] for p in polys], "NAME": [p[1] for p in polys]},
        geometry=[shp_box(*p[2]) for p in polys],
        crs="EPSG:4326",
    )
    gdf.to_file(dir_path / "layer.shp", driver="ESRI Shapefile", engine="pyogrio")
    zip_path = dir_path / "layer.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in os.listdir(dir_path):
            if f.startswith("layer.") and not f.endswith(".zip"):
                zf.write(dir_path / f, f)
    return str(zip_path)


def _point_at_local_zips(monkeypatch, paths):
    """Drive the vsizip read against local archives instead of remote ones."""
    monkeypatch.setattr(vector_ogr, "zip_urls", lambda spec, params, endpoint: list(paths))
    monkeypatch.setattr(vector_ogr, "open_path", lambda spec, url: f"/vsizip/{url}")


def test_zip_member_reads_filters_and_serializes(specs, monkeypatch, tmp_path):
    import geopandas as gpd

    spec = specs["fetch_administrative_boundaries"]
    d = tmp_path / "s"
    d.mkdir()
    zp = _shapefile_zip(
        d,
        [("A", "Alpha", (-82.2, 26.3, -82.0, 26.5)),
         ("B", "Bravo", (-70.0, 40.0, -69.9, 40.1))],  # far outside the query bbox
    )
    _point_at_local_zips(monkeypatch, [zp])
    out = tmp_path / "out.fgb"
    out.write_bytes(vector_ogr.execute(
        spec, {"level": "county", "bbox": [-82.2, 26.3, -82.0, 26.5]}))
    gdf = gpd.read_file(out)
    assert len(gdf) == 1                      # only Alpha intersects the bbox
    assert set(gdf["GEOID"]) == {"A"}
    assert {"GEOID", "NAME"}.issubset(set(gdf.columns))


def test_zip_member_nationwide_empty_is_out_of_coverage(specs, monkeypatch, tmp_path):
    spec = specs["fetch_administrative_boundaries"]
    d = tmp_path / "s"
    d.mkdir()
    _point_at_local_zips(
        monkeypatch, [_shapefile_zip(d, [("B", "Bravo", (-70.0, 40.0, -69.9, 40.1))])])
    with pytest.raises(RouterError) as ei:
        vector_ogr.execute(spec, {"level": "county", "bbox": [-82.2, 26.3, -82.0, 26.5]})
    assert ei.value.error_code == "ADMIN_BOUNDARY_EMPTY"


def test_zip_member_place_merge_concatenates_the_states(specs, monkeypatch, tmp_path):
    import geopandas as gpd

    spec = specs["fetch_administrative_boundaries"]
    d1, d2 = tmp_path / "s1", tmp_path / "s2"
    d1.mkdir()
    d2.mkdir()
    hit = _shapefile_zip(d1, [("P1", "Place1", (-82.2, 26.3, -82.0, 26.5))])
    miss = _shapefile_zip(d2, [("P2", "Place2", (-70.0, 40.0, -69.9, 40.1))])
    _point_at_local_zips(monkeypatch, [hit, miss])
    out = tmp_path / "m.fgb"
    out.write_bytes(vector_ogr.execute(
        spec, {"level": "place", "bbox": [-82.2, 26.3, -82.0, 26.5]}))
    gdf = gpd.read_file(out)
    assert set(gdf["GEOID"]) == {"P1"}   # the state with nothing in the bbox adds nothing
