"""``fetch_usgs_model_water_table`` and ``fetch_usgs_model_thickness``: the model's own head as an elevation, and over its layer bottoms.

Built from the real-archive excerpt zips offline: each value against the arrays read straight off the
excerpt, the head of the topmost active layer, the Des Moines head multiplier, the grid shift of the Cape Cod
head, the one mask both rows share, the whole-box answer, and the outside-grid and straddle behaviour."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import numpy as np
import pytest

import trid3nt_server.tools.cache as cache_mod
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._router import registration as reg
from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError, RouterInputError
from trid3nt_server.tools.fetchers.hydrology import _usgs_model_grid as grid
from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_model_thickness import hooks as MT
from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_model_water_table import hooks as MW

_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "usgs_model_k"
_CC_BBOX = [-70.62, 41.59, -70.50, 41.68]
_DM_BBOX = [-93.6729, 41.6625, -93.6369, 41.6895]
_EMPTY_BBOX = [-93.84, 41.60, -93.80, 41.63]
_OCEAN_BBOX = [-122.7, 45.4, -122.5, 45.6]
_SURVEY_FT = 1200.0 / 3937.0
_ROWS = {"fetch_usgs_model_water_table": "water_table_elevation",
         "fetch_usgs_model_thickness": "aquifer_saturated_thickness"}


class _R:
    def __init__(self, uri: str, data: bytes = b""):
        self.uri = uri
        self.data = data


def _wire(monkeypatch):
    """Both cache layers in memory and both archive reads on the excerpt zips; the datum grids are the Cape Cod excerpts."""
    zips = {"631405ded34e36012efa3476": (_FIX / "jbcc_ring_excerpt.zip").read_bytes(),
            "6601d849d34e64ff1548e2ad": (_FIX / "dm_ring_excerpt.zip").read_bytes()}
    seen = {"gets": 0, "uris": {}}

    def _get_bytes(client, url, *, headers=None, params=None):
        seen["gets"] += 1
        return next(body for key, body in zips.items() if key in url), "application/zip", url

    inner: dict[str, bytes] = {}

    def _inner_rt(metadata, params, ext, fetch_fn, **kw):
        key = str(params)
        inner.setdefault(key, fetch_fn() if key not in inner else inner[key])
        return _R(uri=f"s3://cache-in/{len(inner)}", data=inner[key])

    def _outer_rt(metadata, params, ext, fetch_fn, **kw):
        data = fetch_fn()
        uri = f"s3://cache/{metadata.name}-{params['bbox'][0]}.tif"
        seen["uris"][uri] = data
        return _R(uri=uri, data=data)

    monkeypatch.setattr(MW, "get_bytes", _get_bytes)
    monkeypatch.setattr(MT, "get_bytes", _get_bytes)
    monkeypatch.setattr(cache_mod, "read_through", _inner_rt)
    monkeypatch.setattr(router, "read_through", _outer_rt)
    return seen


def _read(cog: bytes):
    import rasterio

    with rasterio.open(io.BytesIO(cog)) as ds:
        assert ds.count == 1 and ds.crs.to_epsg() == 4326
        return ds.read(1, masked=True), ds.transform


def _call(monkeypatch, name, bbox):
    seen = _wire(monkeypatch)
    layer = TOOL_REGISTRY[name].fn(bbox=bbox)
    return layer, _read(seen["uris"][layer.uri]), seen


def _cell_of(arch, tf, arr, r, c):
    """The model (row, col) holding output cell (r, c)'s centre."""
    from rasterio.warp import transform

    lon, lat = tf.c + (c + 0.5) * tf.a, tf.f + (r + 0.5) * tf.e
    (x,), (y,) = transform("EPSG:4326", arch["crs"], [lon], [lat])
    west, _, _, north = arch["native"]
    nrow, ncol = arch["shape"]
    cell = (arch["native"][2] - west) / ncol
    return int((north - y) // cell), int((x - west) // cell)


def _arrays(key):
    """The excerpt's per-layer heads, masks and bottoms read by independent code."""
    arch = grid.ARCHIVES[key]
    name = "jbcc_ring_excerpt.zip" if key == "jbcc" else "dm_ring_excerpt.zip"
    with zipfile.ZipFile(_FIX / name) as zf:
        def text(member):
            return np.array(zf.read(member).decode().split(), dtype=np.float64).reshape(arch["shape"])

        masks = [text(arch["mask_member"].format(n=n)) == 1 for n in range(1, arch["layers"] + 1)]
        bottoms = [text(arch["bot_member"].format(n=n)) for n in range(1, arch["layers"] + 1)]
        if arch["head_format"] == "text":
            heads = [text(arch["head_member"])] * arch["layers"]
        else:
            raw = zf.read(arch["head_member"])
            record = 52 + 8 * 427 * 383
            heads = []
            for n in range(arch["layers"]):
                one = np.frombuffer(raw[n * record + 52:(n + 1) * record], "<f8").reshape(427, 383).copy()
                one[one > 1e20] = np.nan
                heads.append(one * 1.001)
    return arch, heads, masks, bottoms


def _differences(key):
    """The archive's own head (metres, model datum) of the topmost active layer, thickness and the layer 1 mask at every model cell."""
    arch, heads, masks, bottoms = _arrays(key)
    head = np.full(arch["shape"], np.nan)
    last = np.full(arch["shape"], np.nan)
    for n in range(arch["layers"] - 1, -1, -1):
        head[masks[n]] = heads[n][masks[n]]
        take = masks[n] & np.isnan(last)
        last[take] = bottoms[n][take]
    f = arch["factor"]
    return head * f, (head - last) * f, masks[0]


def _near(cube, row, col):
    """The value at a model cell and at its eight neighbours: the warp lands a cell centre on the cell or its neighbour."""
    return [cube[row + dr, col + dc] for dr in (-1, 0, 1) for dc in (-1, 0, 1)]


@pytest.fixture(autouse=True)
def _grids(proj_grids):
    proj_grids("capecod")


@pytest.mark.parametrize("name", list(_ROWS))
def test_the_rows_are_registered_and_state_their_quantity(name):
    spec = reg.get_spec(name)
    assert spec.shape == "raster-cog" and spec.normalize.units == "m"
    assert spec.normalize.quantity == _ROWS[name]
    assert spec.coverage[0].kind == "modelled"
    assert TOOL_REGISTRY[name].metadata.ttl_class == "static-30d"


def test_the_water_table_states_its_datum_and_the_thickness_a_difference():
    assert reg.get_spec("fetch_usgs_model_water_table").coverage[0].datum == "NAVD88"
    assert reg.get_spec("fetch_usgs_model_thickness").coverage[0].datum is None


@pytest.mark.parametrize("name", list(_ROWS))
def test_the_rings_are_the_k_rows_rings(name):
    k = reg.get_spec("fetch_usgs_model_k").coverage[0].extent.rings
    assert [list(map(list, r)) for r in reg.get_spec(name).coverage[0].extent.rings] == [
        list(map(list, r)) for r in k]


def test_the_rows_state_a_finer_cell_than_the_zell_sanford_rows():
    zell = reg.get_spec("fetch_water_table_depth").coverage[0].resolution_m
    assert reg.get_spec("fetch_usgs_model_water_table").coverage[0].resolution_m < zell
    zell = reg.get_spec("fetch_aquifer_thickness").coverage[0].resolution_m
    assert reg.get_spec("fetch_usgs_model_thickness").coverage[0].resolution_m < zell


def _check_cells(arr, tf, want, arch, slack=1e-3, shift=False):
    """Each unmasked output cell is the archive value at the cell holding its centre or that cell's neighbour (plus the
    NGVD29 -> NAVD88 grid offset at its centre when ``shift``); the share exact."""
    from trid3nt_server.tools.fetchers import _proj_grids

    exact = total = 0
    lons, lats = np.meshgrid(tf.c + (np.arange(arr.shape[1]) + 0.5) * tf.a,
                             tf.f + (np.arange(arr.shape[0]) + 0.5) * tf.e)
    adds = (_proj_grids.geodetic_offset("NGVD29", "NAVD88", lons.ravel(), lats.ravel()).reshape(lons.shape)
            if shift else np.zeros(lons.shape))
    for r in range(arr.shape[0]):
        for c in range(arr.shape[1]):
            if np.ma.is_masked(arr[r, c]):
                continue
            row, col = _cell_of(arch, tf, arr, r, c)
            add = adds[r, c]
            assert any(float(arr[r, c]) == pytest.approx(v + add, abs=slack) for v in _near(want, row, col))
            exact += float(arr[r, c]) == pytest.approx(want[row, col] + add, abs=slack)
            total += 1
    return exact, total


@pytest.mark.parametrize(("key", "bbox", "shifted"), [("jbcc", _CC_BBOX, True), ("des_moines", _DM_BBOX, False)])
def test_every_cell_is_the_archives_head_and_thickness(monkeypatch, key, bbox, shifted):
    """The water table is the head of the topmost active layer in metres on NAVD88 (Cape Cod through the datum grids, Des
    Moines as published); the thickness is the head less the deepest active bottom, in the model's datum."""
    layer, (head, tf), seen = _call(monkeypatch, "fetch_usgs_model_water_table", bbox)
    assert layer.units == "m" and layer.quantity == "water_table_elevation"
    layer, (thick, tft), _ = _call(monkeypatch, "fetch_usgs_model_thickness", bbox)
    assert layer.units == "m" and layer.quantity == "aquifer_saturated_thickness"
    arch = grid.ARCHIVES[key]
    want_head, want_thick, _ = _differences(key)
    exact, total = _check_cells(head, tf, want_head, arch, shift=shifted)
    exact_t, total_t = _check_cells(thick, tft, want_thick, arch)
    assert total > 100 and exact / total > 0.85 and exact_t / total_t > 0.85


def test_des_moines_head_carries_the_multiplier_its_initial_condition_states(monkeypatch):
    """dmww_tr6.ic reads the starting heads at FACTOR 1.001: the head is the record times it."""
    _, (head, tf), _ = _call(monkeypatch, "fetch_usgs_model_water_table", _DM_BBOX)
    arch, heads, masks, _ = _arrays("des_moines")
    assert arch["head_factor"] == 1.001
    r, c = next((r, c) for r in range(head.shape[0]) for c in range(head.shape[1])
                if not np.ma.is_masked(head[r, c]))
    row, col = _cell_of(arch, tf, head, r, c)
    assert float(head[r, c]) == pytest.approx(heads[0][row, col], abs=1e-3) or any(
        float(head[r, c]) == pytest.approx(v, abs=1e-3) for v in _near(_differences("des_moines")[0], row, col))
    assert abs(float(head[r, c]) - _differences("des_moines")[0][row, col] / 1.001) > 0.05


def test_jbcc_head_is_start_heads_in_feet_taken_to_metres_and_onto_navd88(monkeypatch):
    _, (head, tf), _ = _call(monkeypatch, "fetch_usgs_model_water_table", _CC_BBOX)
    arch, heads, masks, _ = _arrays("jbcc")
    r, c = next((r, c) for r in range(head.shape[0]) for c in range(head.shape[1])
                if not np.ma.is_masked(head[r, c]))
    row, col = _cell_of(arch, tf, head, r, c)
    # The full VERTCON grid reads -0.2605 m at the ring's south-west corner.
    assert any(float(head[r, c]) == pytest.approx(heads[0][a, b] * _SURVEY_FT - 0.26, abs=0.01)
               for a, b in [(row + dr, col + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)])


def test_a_cell_whose_layer_1_is_inactive_takes_the_head_of_the_topmost_active_layer(monkeypatch):
    """The water table where layer 1 is inactive lies in a deeper layer: that layer's record is the head, never a gap."""
    _, (head, tf), _ = _call(monkeypatch, "fetch_usgs_model_water_table", _DM_BBOX)
    arch, heads, masks, _ = _arrays("des_moines")
    want, _, first = _differences("des_moines")
    assert (~first & np.isfinite(want)).any(), "the excerpt holds cells whose layer 1 is inactive"
    deeper = 0
    for r in range(head.shape[0]):
        for c in range(head.shape[1]):
            row, col = _cell_of(arch, tf, head, r, c)
            if not first[row, col] and np.isfinite(want[row, col]):
                assert not np.ma.is_masked(head[r, c]) or not np.isfinite(want[row, col])
                deeper += not np.ma.is_masked(head[r, c])
    assert deeper > 0


def test_both_rows_measure_the_same_cells(monkeypatch):
    """One mask serves the water table and the thickness, so a run takes both from the model or neither."""
    _, (head, _), _ = _call(monkeypatch, "fetch_usgs_model_water_table", _DM_BBOX)
    _, (thick, _), _ = _call(monkeypatch, "fetch_usgs_model_thickness", _DM_BBOX)
    assert np.array_equal(np.ma.getmaskarray(head), np.ma.getmaskarray(thick))


def test_a_head_at_or_below_the_deepest_bottom_leaves_both_rows_empty(monkeypatch):
    """A column whose head sits under its last active bottom has no thickness, and so no water table either: nothing is clamped."""
    _wire(monkeypatch)
    real = grid.read_head

    def lowered(zf, a):
        return real(zf, a) - 1000.0

    monkeypatch.setattr(grid, "read_head", lowered)
    for name in _ROWS:
        with pytest.raises(RouterEmptyError):
            TOOL_REGISTRY[name].fn(bbox=_DM_BBOX)


@pytest.mark.parametrize("name", list(_ROWS))
def test_a_box_with_a_cell_the_model_leaves_unmeasured_is_empty_not_partial(monkeypatch, name):
    """The row answers only where it measures every cell of the box: a box that runs past the excerpt's measured window
    inside the grid is the row's EMPTY, naming the count, so the next source answers the whole run."""
    seen = _wire(monkeypatch)
    TOOL_REGISTRY[name].fn(bbox=_DM_BBOX)
    wider = [_DM_BBOX[0], _DM_BBOX[1], _DM_BBOX[2] + 0.04, _DM_BBOX[3]]
    with pytest.raises(RouterEmptyError) as ei:
        TOOL_REGISTRY[name].fn(bbox=wider)
    assert ei.value.error_code.endswith("_EMPTY")
    assert "measures" in str(ei.value) and "every cell" in str(ei.value)
    assert seen["gets"] == 1


@pytest.mark.parametrize("name", list(_ROWS))
def test_an_aoi_outside_both_grids_refuses_naming_them(monkeypatch, name):
    _wire(monkeypatch)
    with pytest.raises(RouterInputError) as ei:
        TOOL_REGISTRY[name].fn(bbox=_OCEAN_BBOX)
    assert ei.value.error_code.endswith("_INPUT_INVALID")
    assert "Cape Cod JBCC MODFLOW covers 0% of the AOI" in str(ei.value)


@pytest.mark.parametrize("name", list(_ROWS) + ["fetch_usgs_model_k", "fetch_usgs_model_layers"])
def test_an_aoi_a_grid_holds_only_in_part_is_the_rows_empty(monkeypatch, name):
    """A box that straddles the grid's edge is no wrong ask: the row is empty and the next source takes the run."""
    _wire(monkeypatch)
    with pytest.raises(RouterEmptyError) as ei:
        TOOL_REGISTRY[name].fn(bbox=[-70.62, 41.59, -70.0, 41.68])
    assert ei.value.error_code.endswith("_EMPTY")
    assert "Cape Cod JBCC MODFLOW covers" in str(ei.value)


@pytest.mark.parametrize("name", list(_ROWS))
def test_a_window_with_no_active_cell_is_the_rows_empty(monkeypatch, name):
    _wire(monkeypatch)
    with pytest.raises(RouterEmptyError) as ei:
        TOOL_REGISTRY[name].fn(bbox=_EMPTY_BBOX)
    assert ei.value.error_code.endswith("_EMPTY")


def test_one_archive_download_serves_both_rows(monkeypatch):
    seen = _wire(monkeypatch)
    TOOL_REGISTRY["fetch_usgs_model_water_table"].fn(bbox=_DM_BBOX)
    TOOL_REGISTRY["fetch_usgs_model_thickness"].fn(bbox=_DM_BBOX)
    assert seen["gets"] == 1


@pytest.mark.parametrize("name", list(_ROWS))
def test_found_through_its_class(class_routes_to_the_match, name):
    class_routes_to_the_match(
        "groundwater level" if name == "fetch_usgs_model_water_table" else "aquifer property", name)
