"""``fetch_usgs_model_layers`` through the animation-frames shape.

Each call fans into the top of layer 1 and the bottom of every layer of the ONE
model grid covering the AOI, built from the real-archive excerpt zips offline:
the n + 1 frame count, the stacking order, the same cells as the K row's frames,
one hand-checked cell per model on its vertical datum, the VDatum offset onto
NAVD88 and its refusal, and the outside-grid refusal."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

import trid3nt_server.tools.cache as cache_mod
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._router import registration as reg
from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
    RouterUpstreamError,
    router_empty_error,
    router_upstream_error,
)
from trid3nt_server.tools.fetchers._router.executors import animation_frames as EX
from trid3nt_server.tools.fetchers._router.transport import TransportError
from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_model_k import hooks as MK
from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_model_layers import (
    hooks as ML,
)

_SPEC = reg.get_spec("fetch_usgs_model_layers")
_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "usgs_model_k"
_CC_BBOX = [-70.62, 41.59, -70.50, 41.68]
_DM_BBOX = [-93.6729, 41.6625, -93.6369, 41.6895]
_EMPTY_BBOX = [-93.84, 41.60, -93.80, 41.63]
_OCEAN_BBOX = [-122.7, 45.4, -122.5, 45.6]
_CC_PROBE = (-70.56, 41.635)
_DM_PROBE = (-93.655, 41.676)
# The model's length unit is the US survey foot (EPSG:26786's axis unit).
_SURVEY_FT = 1200.0 / 3937.0


def _offset_field(lon: float, lat: float) -> float:
    """A stand-in NGVD29 -> NAVD88 offset surface that is LINEAR in lon/lat, so
    the four-corner bilinear reproduces it exactly at every cell centre."""
    return -0.30 + 0.50 * (lon + 70.56) + 0.20 * (lat - 41.635)


def _surface_no(layer_id: str) -> int:
    """0 for the top of layer 1, n for the bottom of layer n."""
    if "-top-l1-" in layer_id:
        return 0
    return int(re.search(r"-bot-l(\d+)-", layer_id).group(1))


def _k_layer_no(layer_id: str) -> int:
    return int(re.search(r"-l(\d+)-", layer_id).group(1))


class _R:
    def __init__(self, uri: str, data: bytes = b""):
        self.uri = uri
        self.data = data


def _wire(monkeypatch, offsets: bool = True) -> dict[str, Any]:
    """Both cache layers in memory, both archive reads on the excerpt zips (for
    the layers row AND the K row), and VDatum answering a known surface; the
    whole call runs offline."""
    jbcc = (_FIX / "jbcc_ring_excerpt.zip").read_bytes()
    dm = (_FIX / "dm_ring_excerpt.zip").read_bytes()
    seen: dict[str, Any] = {"gets": 0, "inner": [], "metas": [], "uris": {},
                            "asks": []}

    def _get_bytes(client, url, *, headers=None, params=None):
        seen["gets"] += 1
        body = jbcc if "631405ded34e36012efa3476" in url else dm
        return body, "application/zip", url

    inner: dict[str, bytes] = {}

    def _inner_rt(metadata, params, ext, fetch_fn, **kw):
        key = str(params)
        seen["metas"].append(metadata.source_class)
        if key not in inner:
            inner[key] = fetch_fn()
            seen["inner"].append(key)
        return _R(uri=f"s3://cache-in/{len(seen['inner'])}", data=inner[key])

    def _outer_rt(metadata, params, ext, fetch_fn, **kw):
        data = fetch_fn()
        tag = f"l{params['layer']}" if "layer" in params else f"s{params['surface']}"
        uri = f"s3://cache/{metadata.name}-{params['archive']}-{tag}.tif"
        seen["uris"][uri] = data
        return _R(uri=uri, data=data)

    def _vdatum(*, point, from_frame, to_frame):
        seen["asks"].append((tuple(point), from_frame, to_frame))
        return {"offset_m": _offset_field(*point), "region": "contiguous"}

    monkeypatch.setattr(MK, "get_bytes", _get_bytes)
    monkeypatch.setattr(ML, "get_bytes", _get_bytes)
    monkeypatch.setattr(cache_mod, "read_through", _inner_rt)
    monkeypatch.setattr(EX, "read_through", _outer_rt)
    if offsets:
        monkeypatch.setitem(
            TOOL_REGISTRY, "fetch_vertical_datum_offset", SimpleNamespace(fn=_vdatum))
    return seen


def _read(cog: bytes):
    import rasterio

    with rasterio.open(io.BytesIO(cog)) as ds:
        assert ds.count == 1 and ds.crs is not None and ds.crs.to_epsg() == 4326
        return ds.read(1, masked=True), ds.transform


def _centres(tf, arr):
    xs = tf.c + (np.arange(arr.shape[1]) + 0.5) * tf.a
    ys = tf.f + (np.arange(arr.shape[0]) + 0.5) * tf.e
    return xs, ys


def _nearest(arr, tf, probe):
    xs, ys = _centres(tf, arr)
    c = int(np.argmin(np.abs(xs - probe[0])))
    r = int(np.argmin(np.abs(ys - probe[1])))
    return r, c, float(xs[c]), float(ys[r])


def _frames(seen, layers):
    return {_surface_no(l.layer_id): _read(seen["uris"][l.uri]) for l in layers}


def _raw(zip_name: str, member: str, shape, row: int, col: int) -> float:
    with zipfile.ZipFile(_FIX / zip_name) as zf:
        vals = np.array(zf.read(member).decode().split(), dtype=np.float64)
    return float(vals.reshape(shape)[row, col])


def test_registered_and_served():
    entry = TOOL_REGISTRY["fetch_usgs_model_layers"]
    assert entry.metadata.source_class == "usgs_model_layers"
    assert entry.metadata.ttl_class == "static-30d"
    assert _SPEC is not None and _SPEC.shape == "animation_frames"
    assert _SPEC.error_code_prefix == "USGS_MODEL_LAYERS"
    assert entry.fn.__annotations__["return"] is list


def test_signature_is_bbox_only():
    import inspect

    p = inspect.signature(TOOL_REGISTRY["fetch_usgs_model_layers"].fn).parameters
    assert list(p) == ["bbox", "_extra_ignored"]


def test_spec_identity_states_metres_on_navd88():
    row = _SPEC.coverage[0]
    assert row.data_class == "aquifer property" and row.kind == "modelled"
    assert _SPEC.normalize.units == "m"
    assert _SPEC.normalize.quantity == "aquifer_layer_elevation"
    assert row.datum == "NAVD88" and _SPEC.vertical_datum == "NAVD88"
    assert row.units["elevation"] == "m" and row.value_column == "elevation"


def test_found_through_the_aquifer_property_class(class_routes_to_the_match):
    """A covered fetcher carries no corpus: its class is the door."""
    class_routes_to_the_match("aquifer property", "fetch_usgs_model_layers")


def test_rings_are_the_k_rows_rings():
    """The two rows state the same grids; a drift would admit an AOI one row
    refuses."""
    k = reg.get_spec("fetch_usgs_model_k").coverage[0].extent.rings
    assert [list(map(list, r)) for r in _SPEC.coverage[0].extent.rings] == [
        list(map(list, r)) for r in k]


def test_archive_constants_state_each_models_datum_and_unit():
    jbcc, dm = MK._ARCHIVES["jbcc"], MK._ARCHIVES["des_moines"]
    assert jbcc["datum"] == "NGVD29" and jbcc["factor"] == pytest.approx(_SURVEY_FT)
    assert dm["datum"] == "NAVD88" and dm["factor"] == 1.0


def test_cape_cod_plans_the_top_then_each_bottom():
    plans = ML.frames_plan(_SPEC, {"bbox": _CC_BBOX})
    assert len(plans) == 33
    assert [_surface_no(p.layer_id) for p in plans] == list(range(33))
    assert plans[0].name == "Cape Cod JBCC MODFLOW top of layer 1"
    assert plans[1].name == "Cape Cod JBCC MODFLOW bottom of layer 1"
    assert plans[32].name == "Cape Cod JBCC MODFLOW bottom of layer 32"
    assert plans[0].cache_params == {
        "bbox": _CC_BBOX, "archive": "jbcc", "surface": 0,
        "tool": "fetch_usgs_model_layers"}
    assert len({p.layer_id for p in plans}) == 33
    assert all(p.valid_from is None and p.valid_to is None for p in plans)


def test_des_moines_plans_seven_surfaces():
    plans = ML.frames_plan(_SPEC, {"bbox": _DM_BBOX})
    assert len(plans) == 7
    assert {p.fetch_context["archive"] for p in plans} == {"des_moines"}


def test_an_aoi_outside_both_grids_refuses_naming_them():
    with pytest.raises(RouterInputError) as ei:
        ML.frames_plan(_SPEC, {"bbox": _OCEAN_BBOX})
    assert ei.value.error_code == "USGS_MODEL_LAYERS_INPUT_INVALID"
    msg = str(ei.value)
    assert "outside the published USGS MODFLOW model grids" in msg
    assert "Cape Cod JBCC MODFLOW covers 0% of the AOI" in msg
    assert "Des Moines MF6 MODFLOW covers 0% of the AOI" in msg


def test_the_refusal_reaches_the_model_through_the_registry():
    with pytest.raises(RouterInputError) as ei:
        TOOL_REGISTRY["fetch_usgs_model_layers"].fn(bbox=_OCEAN_BBOX)
    assert ei.value.error_code == "USGS_MODEL_LAYERS_INPUT_INVALID"


def test_cape_cod_is_n_plus_one_frames_in_metres_on_navd88(monkeypatch):
    seen = _wire(monkeypatch)
    layers = TOOL_REGISTRY["fetch_usgs_model_layers"].fn(bbox=_CC_BBOX)
    # ib32 has no active cell over the ring, so the bottom of layer 32 degrades.
    assert [_surface_no(l.layer_id) for l in layers] == list(range(32))
    assert seen["gets"] == 1
    assert all(l.units == "m" and l.layer_type == "raster" for l in layers)
    frames = _frames(seen, layers)
    # Every ask went to VDatum as NGVD29 -> NAVD88 at the AOI's four corners.
    assert {a[1:] for a in seen["asks"]} == {("ngvd29", "navd88")}
    corners = {(_CC_BBOX[0], _CC_BBOX[1]), (_CC_BBOX[2], _CC_BBOX[1]),
               (_CC_BBOX[0], _CC_BBOX[3]), (_CC_BBOX[2], _CC_BBOX[3])}
    assert {a[0] for a in seen["asks"]} == corners
    top, _ = frames[0]
    # The active cells of the excerpt window read a median of 10.95 m on NGVD29;
    # less the stand-in offset (-0.30 m at the ring's centre) that is ~10.65 m,
    # and the frame's bbox crop moves the median by about a tenth.
    assert float(np.ma.median(top)) == pytest.approx(10.65, abs=0.15)


def test_cape_cod_hand_checked_cell(monkeypatch):
    """One cell: raw archive feet on NGVD29 -> metres by the survey foot -> plus
    the offset at that cell's centre -> NAVD88 metres."""
    seen = _wire(monkeypatch)
    layers = TOOL_REGISTRY["fetch_usgs_model_layers"].fn(bbox=_CC_BBOX)
    arr, tf = _frames(seen, layers)[0]
    r, c, lon, lat = _nearest(arr, tf, _CC_PROBE)
    from rasterio.warp import transform

    (x,), (y,) = transform("EPSG:4326", "EPSG:26786", [lon], [lat])
    row, col = int((294498.0 - y) // 400.0), int((x - 812245.0) // 400.0)
    raw_ft = _raw("jbcc_ring_excerpt.zip", "model/externalfiles/Bots/model_top_c2_3",
                  (290, 400), row, col)
    expected = raw_ft * _SURVEY_FT + _offset_field(lon, lat)
    assert float(arr[r, c]) == pytest.approx(expected, abs=2e-3)
    print(f"JBCC top L1 cell (row {row}, col {col}): raw {raw_ft} ft NGVD29 -> "
          f"{raw_ft * _SURVEY_FT:.4f} m, offset {_offset_field(lon, lat):+.4f} m "
          f"-> {float(arr[r, c]):.4f} m NAVD88")


def test_the_offset_is_applied_across_the_aoi_not_as_one_number(monkeypatch):
    """Two cells at different places differ by the offset field's difference
    over the model's own difference."""
    seen = _wire(monkeypatch)
    layers = TOOL_REGISTRY["fetch_usgs_model_layers"].fn(bbox=_CC_BBOX)
    arr, tf = _frames(seen, layers)[1]
    xs, ys = _centres(tf, arr)
    from rasterio.warp import transform

    pairs = [(r, c) for r in (0, arr.shape[0] - 1) for c in (0, arr.shape[1] - 1)
             if not arr.mask[r, c]]
    assert len(pairs) >= 2
    for r, c in pairs:
        (x,), (y,) = transform("EPSG:4326", "EPSG:26786", [xs[c]], [ys[r]])
        row, col = int((294498.0 - y) // 400.0), int((x - 812245.0) // 400.0)
        raw = _raw("jbcc_ring_excerpt.zip", "model/externalfiles/Bots/bot1",
                   (290, 400), row, col)
        assert float(arr[r, c]) == pytest.approx(
            raw * _SURVEY_FT + _offset_field(xs[c], ys[r]), abs=2e-3)


def test_each_surface_stacks_below_the_one_above(monkeypatch):
    seen = _wire(monkeypatch)
    layers = TOOL_REGISTRY["fetch_usgs_model_layers"].fn(bbox=_CC_BBOX)
    frames = _frames(seen, layers)
    for s in range(1, 32):
        upper, lower = frames[s - 1][0], frames[s][0]
        both = ~np.ma.getmaskarray(upper) & ~np.ma.getmaskarray(lower)
        assert both.any()
        assert (upper.data[both] > lower.data[both]).all(), f"surface {s}"


def test_the_archives_own_top_is_above_bottom_at_every_active_cell():
    """The data fact the frames rest on, read straight off the excerpt: at
    every ACTIVE cell of layer n the surface above it is higher than its
    bottom, whichever datum shift is later added (a shift is the same number
    on both surfaces)."""
    for name, shape, n_layers, top, bot, mask in (
        ("jbcc_ring_excerpt.zip", (290, 400), 32,
         "model/externalfiles/Bots/model_top_c2_3",
         "model/externalfiles/Bots/bot{n}", "model/externalfiles/Ibs/ib{n}"),
        ("dm_ring_excerpt.zip", (427, 383), 6,
         "external_files/surfs/l1_top.dat",
         "external_files/surfs/l{n}_bot.dat", "external_files/id/l{n}_id.dat"),
    ):
        with zipfile.ZipFile(_FIX / name) as zf:
            def grid(m):
                return np.array(zf.read(m).decode().split(),
                                dtype=np.float64).reshape(shape)
            above = grid(top)
            for n in range(1, n_layers + 1):
                active = grid(mask.format(n=n)) == 1
                below = grid(bot.format(n=n))
                assert active.any() or n in (3, 32)
                assert (above[active] > below[active]).all(), f"{name} layer {n}"
                above = below


def test_the_frames_are_the_k_rows_cells(monkeypatch):
    """Frame i + 1 here and frame i of the K row hold values at exactly the
    same cells, on the same grid."""
    seen = _wire(monkeypatch)
    surf = TOOL_REGISTRY["fetch_usgs_model_layers"].fn(bbox=_CC_BBOX)
    k = TOOL_REGISTRY["fetch_usgs_model_k"].fn(bbox=_CC_BBOX)
    assert seen["gets"] == 1, "the two rows share ONE archive download"
    assert set(seen["metas"]) == {"usgs_model_k"}, (
        "both rows cache the archive under the K row's identity")
    s_frames = _frames(seen, surf)
    k_frames = {_k_layer_no(l.layer_id): _read(seen["uris"][l.uri]) for l in k}
    assert sorted(k_frames) == list(range(1, 32))
    for layer, (karr, ktf) in k_frames.items():
        sarr, stf = s_frames[layer]
        assert stf == ktf
        assert (np.ma.getmaskarray(sarr) == np.ma.getmaskarray(karr)).all()
    # The top of layer 1 sits on layer 1's cells.
    t_arr, t_tf = s_frames[0]
    assert t_tf == k_frames[1][1]
    assert (np.ma.getmaskarray(t_arr) == np.ma.getmaskarray(k_frames[1][0])).all()


def test_every_frame_states_its_vertical_datum_and_the_k_frames_state_none(monkeypatch):
    _wire(monkeypatch)
    surfaces = TOOL_REGISTRY["fetch_usgs_model_layers"].fn(bbox=_DM_BBOX)
    assert surfaces and {l.vertical_datum for l in surfaces} == {"NAVD88"}
    k = TOOL_REGISTRY["fetch_usgs_model_k"].fn(bbox=_DM_BBOX)
    assert k and {l.vertical_datum for l in k} == {None}


def test_des_moines_is_seven_planned_six_emitted_and_read_as_published(monkeypatch):
    seen = _wire(monkeypatch)
    layers = TOOL_REGISTRY["fetch_usgs_model_layers"].fn(bbox=_DM_BBOX)
    # idomain has no active cell over the ring in layer 3, so its bottom drops.
    assert sorted(_surface_no(l.layer_id) for l in layers) == [0, 1, 2, 4, 5, 6]
    assert seen["gets"] == 1
    assert seen["asks"] == [], "a NAVD88 model is read as published, no VDatum ask"
    frames = _frames(seen, layers)
    arr, tf = frames[0]
    r, c, lon, lat = _nearest(arr, tf, _DM_PROBE)
    from rasterio.warp import transform

    (x,), (y,) = transform("EPSG:4326", "EPSG:26915", [lon], [lat])
    row, col = int((4629880.0 - y) // 60.0), int((x - 429070.0) // 60.0)
    raw = _raw("dm_ring_excerpt.zip", "external_files/surfs/l1_top.dat",
               (427, 383), row, col)
    assert float(arr[r, c]) == pytest.approx(raw, abs=1e-3)
    print(f"DM top L1 cell (row {row}, col {col}): raw {raw} m NAVD88 -> "
          f"{float(arr[r, c]):.4f} m NAVD88 (no conversion)")
    for s in (1, 2, 5, 6):
        upper, lower = frames[s - 1][0], frames[s][0]
        both = ~np.ma.getmaskarray(upper) & ~np.ma.getmaskarray(lower)
        if both.any():
            assert (upper.data[both] > lower.data[both]).all()


def test_a_vdatum_gap_refuses_by_name_and_returns_no_frames(monkeypatch):
    _wire(monkeypatch)

    def _gap(*, point, from_frame, to_frame):
        raise router_empty_error("VDATUM", "grids do not reach the point", "NO_COVERAGE")

    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_vertical_datum_offset", SimpleNamespace(fn=_gap))
    with pytest.raises(RouterInputError) as ei:
        TOOL_REGISTRY["fetch_usgs_model_layers"].fn(bbox=_CC_BBOX)
    assert ei.value.error_code == "USGS_MODEL_LAYERS_INPUT_INVALID"
    assert "cannot be put on NAVD88" in str(ei.value)
    assert "grids do not reach the point" in str(ei.value)


def test_a_vdatum_outage_is_retryable_upstream(monkeypatch):
    _wire(monkeypatch)

    def _down(*, point, from_frame, to_frame):
        raise router_upstream_error("VDATUM", "service down", True)

    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_vertical_datum_offset", SimpleNamespace(fn=_down))
    with pytest.raises(RouterUpstreamError) as ei:
        TOOL_REGISTRY["fetch_usgs_model_layers"].fn(bbox=_CC_BBOX)
    assert ei.value.error_code == "USGS_MODEL_LAYERS_UPSTREAM_ERROR"
    assert ei.value.retryable is True


def test_an_aoi_with_no_active_cell_raises_the_typed_empty(monkeypatch):
    seen = _wire(monkeypatch)
    with pytest.raises(RouterEmptyError) as ei:
        TOOL_REGISTRY["fetch_usgs_model_layers"].fn(bbox=_EMPTY_BBOX)
    assert ei.value.error_code == "USGS_MODEL_LAYERS_EMPTY"
    assert "every one of 7 frames" in str(ei.value)
    assert seen["asks"] == []


def test_a_failed_archive_download_is_a_typed_upstream_error(monkeypatch):
    _wire(monkeypatch)

    def _down(client, url, *, headers=None, params=None):
        raise TransportError("simulated outage", retryable=False)

    monkeypatch.setattr(ML, "get_bytes", _down)
    with pytest.raises(RouterUpstreamError) as ei:
        TOOL_REGISTRY["fetch_usgs_model_layers"].fn(bbox=_CC_BBOX)
    assert ei.value.error_code == "USGS_MODEL_LAYERS_UPSTREAM_ERROR"
    assert ei.value.retryable is False
    assert "simulated outage" in str(ei.value)


def test_payload_estimator_stays_under_the_warn_threshold():
    mb = router.synthesize_payload_estimator(_SPEC)(bbox=tuple(_CC_BBOX))
    assert 0.0 < mb < 25.0
