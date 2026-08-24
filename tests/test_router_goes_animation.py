"""Router tests for fetch_goes_animation + fetch_goes_blend_animation (ADR 0087).

The twins folded onto the frames-list output shape (shape: animation_frames):
- registration + metadata TWIN-IDENTICAL, spec-served.
- the frames-list shape returns an ordered list[LayerURI] with the scrubber
  NAME-TOKEN ("GOES <ProductLabel> step <N> <ISO> (<SAT>)"), proven by the
  plugin's pure-python group_frame_layers over REAL produced names.
- band routing: geocolor / fire_temperature (two synchronized groups) vs blend
  (ONE composite group); the deprecated fetch_goes_blend_animation delegate.
- honesty floor (all frames degrade / empty window -> typed EMPTY).
- the typed-error surface + the GOES spelling-zoo normalization.
- the pure frame-window helpers + the _satellite_slider blend (UNCHANGED).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._router import registration as reg
from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
)
from trid3nt_server.tools.fetchers._router.executors import animation_frames as EX
from trid3nt_server.tools.fetchers._router.hooks import goes_animation as GA
from trid3nt_server.tools.fetchers.imagery._satellite_slider import (
    FIRE_BLEND_RED_FLOOR,
    SliderEmptyError,
    blend_geocolor_fire_temperature,
    rgb_array_to_cog_bytes,
    rgb_cog_bytes_to_array,
    ts_int_to_datetime,
    ts_int_to_iso,
)
from trid3nt_server.tools.fetchers.imagery._goes_common import (
    GOESInputError,
)

_UT_BBOX = (-113.346, 39.57, -111.765, 41.115)
_W = dict(start_utc="2026-06-22T17:30:00Z", end_utc="2026-06-22T18:30:00Z")


# The plugin scrubber grouper is pure python (no PyQGIS); load it by path so the
# name-token contract is proven in the offline suite.
def _load_group_frame_layers():
    spec = importlib.util.spec_from_file_location(
        "_plugin_temporal_gfl",
        "plugin/render/temporal.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.group_frame_layers


group_frame_layers = _load_group_frame_layers()


def _ts(y, mo, d, h, mi):
    return int(f"{y:04d}{mo:02d}{d:02d}{h:02d}{mi:02d}00")


_GOES_SPEC = reg.get_spec("fetch_goes_animation")


# ---- registration + metadata (twin-identical) -----------------------------


def test_goes_animation_registered_and_spec_served():
    assert "fetch_goes_animation" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_goes_animation"]
    assert entry.metadata.name == "fetch_goes_animation"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "goes_animation"
    assert entry.metadata.cacheable is True
    assert entry.metadata.supports_global_query is False
    assert "fetch_goes_animation" in reg.registered_spec_names()


def test_goes_blend_registered_and_spec_served():
    assert "fetch_goes_blend_animation" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_goes_blend_animation"]
    assert entry.metadata.name == "fetch_goes_blend_animation"
    assert entry.metadata.source_class == "goes_animation"
    assert entry.metadata.cacheable is True
    assert "fetch_goes_blend_animation" in reg.registered_spec_names()


# ---- pure frame-window helpers --------------------------------------------


def test_band_to_slider_product_confirmed_slugs():
    assert GA._band_to_slider_product(_GOES_SPEC, "geocolor") == "geocolor"
    assert GA._band_to_slider_product(_GOES_SPEC, "fire_temperature") == "fire_temperature"


def test_band_to_slider_product_unknown_raises():
    with pytest.raises(RouterInputError):
        GA._band_to_slider_product(_GOES_SPEC, "ultraviolet")


def test_parse_utc_forms():
    p = GA._parse_utc
    assert p(_GOES_SPEC, "2026-06-22T13:30:00Z") == datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    assert p(_GOES_SPEC, "2026-06-22T13:30:00+00:00") == datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    assert p(_GOES_SPEC, "2026-06-22 13:30:00") == datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    assert p(_GOES_SPEC, "2026-06-22") == datetime(2026, 6, 22, 0, 0, tzinfo=timezone.utc)


def test_parse_utc_rejects_garbage():
    with pytest.raises(RouterInputError):
        GA._parse_utc(_GOES_SPEC, "not-a-date")


def test_select_frame_indices_keeps_all_under_cap():
    assert GA._select_frame_indices(5, cap=10) == [0, 1, 2, 3, 4]


def test_select_frame_indices_subsamples_keeping_endpoints():
    kept = GA._select_frame_indices(100, cap=10)
    assert kept[0] == 0 and kept[-1] == 99 and len(kept) <= 10
    assert all(kept[i] < kept[i + 1] for i in range(len(kept) - 1))


def test_build_frame_list_windows_and_orders():
    all_ts = [_ts(2026, 6, 22, 13, m) for m in (0, 5, 10, 15, 20, 25, 30)]
    start = datetime(2026, 6, 22, 13, 5, tzinfo=timezone.utc)
    end = datetime(2026, 6, 22, 13, 20, tzinfo=timezone.utc)
    frames = GA._build_frame_list(all_ts, start, end)
    assert frames == [_ts(2026, 6, 22, 13, m) for m in (5, 10, 15, 20)]
    assert frames == sorted(frames)


def test_build_frame_list_caps_and_keeps_endpoints():
    base = datetime(2026, 6, 22, 0, 0, tzinfo=timezone.utc)
    all_ts = [int((base + timedelta(minutes=5 * i)).strftime("%Y%m%d%H%M%S")) for i in range(300)]
    frames = GA._build_frame_list(all_ts, ts_int_to_datetime(all_ts[0]), ts_int_to_datetime(all_ts[-1]), cap=20)
    assert len(frames) <= 20 and frames[0] == all_ts[0] and frames[-1] == all_ts[-1]


def test_build_frame_list_empty_window():
    all_ts = [_ts(2026, 6, 22, 13, 0)]
    start = datetime(2026, 6, 23, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 23, 1, 0, tzinfo=timezone.utc)
    assert GA._build_frame_list(all_ts, start, end) == []


# ---- frames-list shape + the scrubber NAME-TOKEN contract -----------------


class _R:
    def __init__(self, uri, data=b"COG"):
        self.uri = uri
        self.data = data


def _stub_three_frames(monkeypatch):
    """Emit 3 deterministic frames with no network (stub the SLIDER substrate)."""
    frame_ts = [_ts(2026, 6, 22, 18, 0), _ts(2026, 6, 22, 18, 5), _ts(2026, 6, 22, 18, 10)]
    monkeypatch.setattr(GA, "fetch_slider_timestamps", lambda *a, **k: list(frame_ts))
    monkeypatch.setattr(GA, "pick_zoom_for_aoi", lambda *a, **k: 5)
    monkeypatch.setattr(
        EX, "read_through",
        lambda metadata, params, ext, fetch_fn: _R(uri=f"s3://fake/{params['product']}-{params['ts_int']}.tif"),
    )
    return frame_ts


def _run(name, **kw):
    return TOOL_REGISTRY[name].fn(bbox=_UT_BBOX, **_W, **kw)


def test_returns_ordered_list_with_step_token_and_iso(monkeypatch):
    frame_ts = _stub_three_frames(monkeypatch)
    layers = _run("fetch_goes_animation", band="fire_temperature", satellite="goes-18")
    assert isinstance(layers, list) and len(layers) == len(frame_ts) == 3
    for n, (layer, ts) in enumerate(zip(layers, frame_ts), start=1):
        assert layer.name == f"GOES Fire Temperature step {n} {ts_int_to_iso(ts)} (GOES-18)"
        assert layer.layer_type == "raster" and layer.role == "context"
    steps = [int(re.search(r"step (\d+)", lyr.name).group(1)) for lyr in layers]
    assert steps == [1, 2, 3]
    assert {lyr.style_preset for lyr in layers} == {"goes_rgb_animation"}


def test_scrubber_group_forms_over_real_names(monkeypatch):
    _stub_three_frames(monkeypatch)
    layers = _run("fetch_goes_animation", band="fire_temperature", satellite="goes-18")
    groups = group_frame_layers([lyr.name for lyr in layers])
    assert len(groups) == 1
    assert [m.value for m in groups[0].members] == [1, 2, 3]


def test_two_products_form_two_synchronized_groups(monkeypatch):
    frame_ts = _stub_three_frames(monkeypatch)
    geo = _run("fetch_goes_animation", band="geocolor", satellite="goes-18")
    fire = _run("fetch_goes_animation", band="fire_temperature", satellite="goes-18")
    # Two DISTINCT scrubber groups (distinct product stems).
    groups = group_frame_layers([lyr.name for lyr in geo] + [lyr.name for lyr in fire])
    assert len(groups) == 2
    # step N -> the SAME valid-time in both products (time-synchronized).
    def _step_iso(layers):
        out = {}
        for lyr in layers:
            step = int(re.search(r"step (\d+)", lyr.name).group(1))
            out[step] = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", lyr.name).group(1)
        return out
    assert _step_iso(geo) == _step_iso(fire) == {1: ts_int_to_iso(frame_ts[0]), 2: ts_int_to_iso(frame_ts[1]), 3: ts_int_to_iso(frame_ts[2])}


@pytest.mark.parametrize("token", ["blend", "blended", "combined", "geocolor_fire_temperature"])
def test_blend_band_forms_one_group(monkeypatch, token):
    _stub_three_frames(monkeypatch)
    layers = _run("fetch_goes_animation", band=token, satellite="goes-19")
    assert len(layers) == 3
    assert all(lyr.layer_id.startswith("goes-fire-blend-") for lyr in layers)
    for n, lyr in enumerate(layers, start=1):
        assert lyr.name.startswith(f"GOES Fire (GeoColor + Fire Temperature) step {n} ")
        assert lyr.name.endswith("(GOES-19)")
    assert len(group_frame_layers([lyr.name for lyr in layers])) == 1


def test_blend_delegate_returns_one_group(monkeypatch):
    frame_ts = _stub_three_frames(monkeypatch)
    layers = _run("fetch_goes_blend_animation", satellite="goes-18")
    assert len(layers) == len(frame_ts) == 3
    assert all(lyr.layer_id.startswith("goes-fire-blend-") for lyr in layers)
    assert len(group_frame_layers([lyr.name for lyr in layers])) == 1


# ---- honesty floor ---------------------------------------------------------


def test_honesty_floor_all_frames_degrade(monkeypatch):
    _stub_three_frames(monkeypatch)
    # read_through invokes fetch_fn (the frame builder), which degrades every frame.
    monkeypatch.setattr(
        GA, "stitch_slider_mosaic",
        lambda *a, **k: (_ for _ in ()).throw(SliderEmptyError("empty crop")),
    )
    def _rt(metadata, params, ext, fetch_fn):
        fetch_fn()
        return _R("s3://never")
    monkeypatch.setattr(EX, "read_through", _rt)
    with pytest.raises(RouterEmptyError) as ei:
        _run("fetch_goes_animation", band="geocolor", satellite="goes-18")
    assert ei.value.error_code == "GOES_ANIM_EMPTY"


def test_honesty_floor_empty_window(monkeypatch):
    monkeypatch.setattr(GA, "fetch_slider_timestamps", lambda *a, **k: [_ts(2020, 1, 1, 0, 0)])
    monkeypatch.setattr(GA, "pick_zoom_for_aoi", lambda *a, **k: 5)
    with pytest.raises(RouterEmptyError) as ei:
        _run("fetch_goes_animation", band="geocolor", satellite="goes-18")
    assert ei.value.error_code == "GOES_ANIM_EMPTY"


# ---- typed-error surface + spelling zoo ------------------------------------


def test_bbox_none_raises_input_error():
    # NOTE (ADR 0087 divergence, non-gating): the twin stamped a bare BBOX_REQUIRED;
    # the router stamps the source INPUT code (GOES_ANIM_INPUT_INVALID) -- both are
    # non-retryable input errors with the same server actionability.
    with pytest.raises(RouterInputError):
        TOOL_REGISTRY["fetch_goes_animation"].fn(bbox=None, band="geocolor")


def test_unknown_band_raises():
    with pytest.raises(RouterInputError):
        _run("fetch_goes_animation", band="xyz")


def test_unsupported_goes_bird_raises_source_error():
    with pytest.raises(RouterInputError):
        _run("fetch_goes_animation", satellite="goes-16", band="geocolor")


def test_unknown_bird_raises_loud_normalizer_error():
    with pytest.raises(GOESInputError):
        _run("fetch_goes_animation", satellite="himawari-9", band="geocolor")


def test_degenerate_bbox_raises():
    with pytest.raises(RouterInputError):
        TOOL_REGISTRY["fetch_goes_animation"].fn(bbox=(-112.0, 39.0, -112.0, 39.0), band="geocolor", **_W)


@pytest.mark.parametrize("spelling", ["GOES-18", "goes18", "GOES West", "G18", "18"])
def test_satellite_spellings_canonicalize(monkeypatch, spelling):
    _stub_three_frames(monkeypatch)
    layers = _run("fetch_goes_animation", band="geocolor", satellite=spelling)
    assert len(layers) == 3
    assert all(lyr.name.endswith("(GOES-18)") for lyr in layers)


# ---- _satellite_slider blend (UNCHANGED helper; parity coverage) -----------


def _rgb_cog(arr, bbox):
    from rasterio.transform import from_bounds

    h, w = arr.shape[1], arr.shape[2]
    return rgb_array_to_cog_bytes(arr, from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], w, h), w, h)


def test_blend_is_valid_3band_rgb_cog_coregistered():
    import numpy as np
    from rasterio.transform import from_bounds

    bbox = (-112.0, 39.0, -111.9, 39.08)
    h, w = 8, 10
    base_tr = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], w, h)
    geo = np.zeros((3, h, w), dtype=np.uint8)
    geo[0], geo[1], geo[2] = 60, 120, 80
    fire = np.zeros((3, h, w), dtype=np.uint8)
    fire[2] = 40
    out, out_tr, out_w, out_h = rgb_cog_bytes_to_array(
        blend_geocolor_fire_temperature(_rgb_cog(geo, bbox), _rgb_cog(fire, bbox))
    )
    assert out.shape == (3, h, w) and out.dtype == np.uint8
    assert (out_w, out_h) == (w, h)
    assert np.allclose(np.asarray(out_tr)[:6], np.asarray(base_tr)[:6], atol=1e-9)


def test_blend_overlay_changes_fire_pixels_keeps_base_elsewhere():
    import numpy as np

    bbox = (-112.0, 39.0, -111.9, 39.08)
    h, w = 8, 10
    geo = np.zeros((3, h, w), dtype=np.uint8)
    geo[0], geo[1], geo[2] = 60, 120, 80
    fire = np.zeros((3, h, w), dtype=np.uint8)
    fire[2] = 40
    fire[0, 3:5, 4:6] = 240
    fire[1, 3:5, 4:6] = 90
    fire[2, 3:5, 4:6] = 30
    assert 240 >= FIRE_BLEND_RED_FLOOR
    out, _, _, _ = rgb_cog_bytes_to_array(
        blend_geocolor_fire_temperature(_rgb_cog(geo, bbox), _rgb_cog(fire, bbox))
    )
    assert tuple(int(v) for v in out[:, 3, 4]) != (60, 120, 80)
    assert int(out[0, 3, 4]) > 60
    assert tuple(int(v) for v in out[:, 0, 0]) == (60, 120, 80)


def test_blend_empty_inputs_raise_empty():
    import numpy as np

    bbox = (-112.0, 39.0, -111.9, 39.08)
    zero = np.zeros((3, 8, 10), dtype=np.uint8)
    with pytest.raises(SliderEmptyError):
        blend_geocolor_fire_temperature(_rgb_cog(zero, bbox), _rgb_cog(zero, bbox))


def test_blend_frame_bytes_composites_both_products(monkeypatch):
    """frame_bytes for the blend product fetches BOTH co-temporal single products
    (cache-mediated) and returns a real blended RGB COG."""
    import numpy as np
    from trid3nt_server.tools.fetchers._router.hooks import FramePlan

    bbox = (-112.0, 39.0, -111.9, 39.08)
    products_fetched: list[str] = []

    def _fake_stitch(sat, sector, product, ts_int, zoom, b, **k):
        products_fetched.append(product)
        rgb = np.zeros((8, 10, 3), dtype=np.uint8)
        if product == "geocolor":
            rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2] = 60, 120, 80
        else:
            rgb[:, :, 2] = 40
            rgb[3:5, 4:6, 0] = 240
        return rgb, bbox

    monkeypatch.setattr(GA, "stitch_slider_mosaic", _fake_stitch)
    # _single_product_frame_bytes lazily imports read_through from cache; stub it.
    import trid3nt_server.tools.cache as cache

    def _fake_rt(metadata, params, ext, fetch_fn):
        return _R(uri="s3://x", data=fetch_fn())
    monkeypatch.setattr(cache, "read_through", _fake_rt)

    frame = FramePlan(
        cache_params={
            "bbox": list(bbox), "product": GA.GOES_BLEND_PRODUCT, "satellite": "goes-18",
            "sector": "conus", "ts_int": _ts(2026, 6, 22, 18, 0), "zoom": 5,
        },
        name="x", layer_id="goes-fire-blend-x", bbox=bbox,
    )
    blended = GA.frame_bytes(_GOES_SPEC, {}, frame)
    assert products_fetched == ["geocolor", "fire_temperature"]
    out, _, _, _ = rgb_cog_bytes_to_array(blended)
    assert out.shape == (3, 8, 10)
    assert tuple(int(v) for v in out[:, 3, 4]) != (60, 120, 80)
    assert tuple(int(v) for v in out[:, 0, 0]) == (60, 120, 80)
