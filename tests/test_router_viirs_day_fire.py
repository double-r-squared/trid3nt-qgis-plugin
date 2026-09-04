"""Router tests for fetch_viirs_day_fire (ADR 0087).

The twin folded onto the frames-list output shape (shape: animation_frames):
- registration + metadata TWIN-IDENTICAL, spec-served.
- the frames-list shape returns ordered list[LayerURI] with the scrubber
  NAME-TOKEN ("VIIRS Day Fire step <N> <ISO> (<SAT>)"), proven by the plugin's
  pure-python group_frame_layers over REAL produced names.
- the day/night pass filter + the multi-satellite merge/sort pass-list.
- honesty floor + the typed-error surface.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import datetime, timezone

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._router import registration as reg
from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
)
from trid3nt_server.tools.fetchers._router.executors import animation_frames as EX
from trid3nt_server.tools.fetchers._router.hooks import viirs_day_fire as VF
from trid3nt_server.tools.fetchers.imagery._satellite_slider import (
    SliderEmptyError,
    ts_int_to_iso,
)

_CI_BBOX = (-120.50, 33.85, -119.50, 34.10)
_CI_CENTER_LON = (-120.50 + -119.50) / 2.0
_W = dict(start_utc="2026-05-15T20:47:00Z", end_utc="2026-05-19T22:01:00Z")


def _load_group_frame_layers():
    spec = importlib.util.spec_from_file_location(
        "_plugin_temporal_gfl_v", "plugin/render/temporal.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.group_frame_layers


group_frame_layers = _load_group_frame_layers()
_VF_SPEC = reg.get_spec("fetch_viirs_day_fire")


def _ts(y, mo, d, h, mi):
    return int(f"{y:04d}{mo:02d}{d:02d}{h:02d}{mi:02d}00")


# ---- registration ----------------------------------------------------------


def test_registered_and_spec_served():
    assert "fetch_viirs_day_fire" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_viirs_day_fire"]
    assert entry.metadata.name == "fetch_viirs_day_fire"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "viirs_satellite"
    assert entry.metadata.cacheable is True
    assert "fetch_viirs_day_fire" in reg.registered_spec_names()


def test_day_fire_slug_is_confirmed_value():
    assert VF.DAY_FIRE_PRODUCT_SLUG == "cira_natural_fire_color"


# ---- parse + day/night filter + pass-list ----------------------------------


def test_parse_utc_iso():
    assert VF._parse_utc(_VF_SPEC, "2026-05-15T20:47:00Z") == datetime(2026, 5, 15, 20, 47, tzinfo=timezone.utc)


def test_parse_utc_rejects_garbage():
    with pytest.raises(RouterInputError):
        VF._parse_utc(_VF_SPEC, "xyz")


def test_is_daytime_pass_keeps_local_afternoon():
    assert VF._is_daytime_pass(_ts(2026, 5, 15, 21, 0), _CI_CENTER_LON) is True


def test_is_daytime_pass_drops_local_night():
    assert VF._is_daytime_pass(_ts(2026, 5, 15, 9, 30), _CI_CENTER_LON) is False


def test_build_pass_list_merges_sorts_and_day_filters():
    all_ts = [
        _ts(2026, 5, 16, 21, 0),
        _ts(2026, 5, 15, 21, 30),
        _ts(2026, 5, 15, 9, 30),   # NIGHT -> dropped
        _ts(2026, 5, 16, 20, 0),
        _ts(2026, 5, 17, 23, 0),   # outside window -> dropped
    ]
    start = datetime(2026, 5, 15, 20, 47, tzinfo=timezone.utc)
    end = datetime(2026, 5, 16, 22, 1, tzinfo=timezone.utc)
    passes = VF._build_pass_list(all_ts, start, end, _CI_CENTER_LON, day_only=True)
    assert passes == [_ts(2026, 5, 15, 21, 30), _ts(2026, 5, 16, 20, 0), _ts(2026, 5, 16, 21, 0)]
    assert passes == sorted(passes)


def test_build_pass_list_day_only_false_keeps_night():
    all_ts = [_ts(2026, 5, 15, 9, 30), _ts(2026, 5, 15, 21, 0)]
    start = datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc)
    assert len(VF._build_pass_list(all_ts, start, end, _CI_CENTER_LON, day_only=False)) == 2


# ---- frames-list shape + the scrubber NAME-TOKEN contract -----------------


class _R:
    def __init__(self, uri):
        self.uri = uri


def _stub_three_passes(monkeypatch):
    passes = [_ts(2026, 5, 16, 20, 0), _ts(2026, 5, 16, 21, 0), _ts(2026, 5, 17, 20, 30)]
    monkeypatch.setattr(VF, "fetch_slider_timestamps", lambda *a, **k: list(passes))
    monkeypatch.setattr(VF, "pick_zoom_for_aoi", lambda *a, **k: 5)
    monkeypatch.setattr(
        EX, "read_through",
        lambda metadata, params, ext, fetch_fn: _R(uri=f"s3://fake/{params['ts_int']}.tif"),
    )
    return passes


def _run(**kw):
    return TOOL_REGISTRY["fetch_viirs_day_fire"].fn(bbox=_CI_BBOX, **_W, **kw)


def test_returns_ordered_list_with_step_and_iso(monkeypatch):
    passes = _stub_three_passes(monkeypatch)
    layers = _run(satellite="all")
    assert isinstance(layers, list) and len(layers) == 3
    for n, (layer, ts) in enumerate(zip(layers, passes), start=1):
        assert layer.name == f"VIIRS Day Fire step {n} {ts_int_to_iso(ts)} (JPSS)"
        assert layer.style["kind"] == "continuous"
        assert layer.layer_id.startswith("viirs-dayfire-")
    groups = group_frame_layers([lyr.name for lyr in layers])
    assert len(groups) == 1 and [m.value for m in groups[0].members] == [1, 2, 3]


def test_specific_satellite_label_recorded(monkeypatch):
    _stub_three_passes(monkeypatch)
    layers = _run(satellite="noaa-20")
    assert all(lyr.name.endswith("(NOAA-20)") for lyr in layers)


def test_honesty_floor_all_passes_empty(monkeypatch):
    _stub_three_passes(monkeypatch)
    monkeypatch.setattr(
        VF, "stitch_slider_mosaic",
        lambda *a, **k: (_ for _ in ()).throw(SliderEmptyError("off swath")),
    )
    def _rt(metadata, params, ext, fetch_fn):
        fetch_fn()
        return _R("s3://never")
    monkeypatch.setattr(EX, "read_through", _rt)
    with pytest.raises(RouterEmptyError) as ei:
        _run(satellite="all")
    assert ei.value.error_code == "VIIRS_DAY_FIRE_EMPTY"


def test_honesty_floor_no_daytime_passes(monkeypatch):
    monkeypatch.setattr(VF, "fetch_slider_timestamps", lambda *a, **k: [_ts(2026, 5, 16, 9, 30)])  # night only
    monkeypatch.setattr(VF, "pick_zoom_for_aoi", lambda *a, **k: 5)
    with pytest.raises(RouterEmptyError) as ei:
        _run(satellite="all")
    assert ei.value.error_code == "VIIRS_DAY_FIRE_EMPTY"


# ---- typed-error surface ---------------------------------------------------


def test_bbox_none_raises_input_error():
    # ADR 0087 divergence (non-gating): twin's bare BBOX_REQUIRED -> the source
    # INPUT code VIIRS_DAY_FIRE_INPUT_INVALID (same non-retryable actionability).
    with pytest.raises(RouterInputError):
        TOOL_REGISTRY["fetch_viirs_day_fire"].fn(bbox=None)


def test_unknown_satellite_raises():
    with pytest.raises(RouterInputError):
        _run(satellite="terra")


def test_unknown_product_raises():
    with pytest.raises(RouterInputError):
        _run(product="night_microphysics")
