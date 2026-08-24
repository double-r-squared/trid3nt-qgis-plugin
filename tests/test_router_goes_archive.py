"""Router-level coverage for the folded GOES raw-MCMIPC archive sources (ADR 0088).

fetch_goes_archive_animation + fetch_goes_active_fire fold onto shape:
animation_frames via the shared goes_archive.frames_plan / frame_bytes hooks over
the imagery._goes_archive_core substrate (netcdf_cf_object per-frame mode). This
covers:

- registration + spec-served + signature/return parity (twin-identical surface),
- per-frame cache_params BYTE-identity vs the twin (cache reuse), naming, layer_id,
  per-band style_preset, and the scrubber NAME-TOKEN grouping over REAL produced
  names (the plugin's pure-python group_frame_layers),
- the honesty floor (all-frames-degrade + empty-window -> GOES_ARCHIVE_EMPTY),
  satellite-spelling normalization, band aliasing, and typed input errors,
- the pure band-math core (Fire-Temp / true-color composite, split-window fire
  detection, hotspot ramp, bake, window subsample) now living in the core module.

ASCII only.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._router import registration as reg
from trid3nt_server.tools.fetchers._router.executors import animation_frames as EX
from trid3nt_server.tools.fetchers._router.hooks import FrameDegraded
from trid3nt_server.tools.fetchers._router.hooks import goes_archive as GA
from trid3nt_server.tools.fetchers.imagery import _goes_archive_core as core
from trid3nt_server.tools.fetchers.imagery._goes_common import (
    GOESInputError,
)


def _load_group_frame_layers():
    spec = importlib.util.spec_from_file_location(
        "_plugin_temporal_gfl_arch",
        "plugin/render/temporal.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.group_frame_layers


group_frame_layers = _load_group_frame_layers()

# Utah fire cluster AOI (the design-spike bbox).
_BBOX = [-114.05, 37.0, -109.04, 42.0]
_ARCH = reg.get_spec("fetch_goes_archive_animation")
_AF = reg.get_spec("fetch_goes_active_fire")


@pytest.fixture()
def _stub_three_keys(monkeypatch):
    """Stub the S3 listing with 3 in-window MCMIPC keys at :00 :05 :10 UTC."""

    def fake_list(sat, s, e, session=None):
        out = []
        for mi in (0, 5, 10):
            t = datetime(2026, 6, 22, 14, mi, 0, tzinfo=timezone.utc)
            out.append(
                (t, f"ABI-L2-MCMIPC/2026/173/14/OR_ABI-L2-MCMIPC-M6_G18_s2026173140{mi}000_e_c.nc")
            )
        return out

    monkeypatch.setattr(core, "_list_archive_keys_in_window", fake_list)
    monkeypatch.setattr(core, "_fetch_archive_frame_cog_bytes", lambda *a, **k: b"COG")

    class _R:
        def __init__(self, uri):
            self.uri = uri
            self.data = b"COG"

    def fake_rt(metadata, params, ext, fetch_fn):
        fetch_fn()  # exercise frame_bytes
        return _R(f"s3://trid3nt-cache/cache/dynamic-1h/{metadata.source_class}/{params['ts_start']}.tif")

    monkeypatch.setattr(EX, "read_through", fake_rt)


# --------------------------------------------------------------------------- #
# Registration + surface parity.
# --------------------------------------------------------------------------- #


def test_both_specs_registered_and_served():
    for n in ("fetch_goes_archive_animation", "fetch_goes_active_fire"):
        assert n in TOOL_REGISTRY
        s = reg.get_spec(n)
        assert s is not None and s.shape == "animation_frames"
        assert s.source_class == "goes_animation"
        assert s.error_code_prefix == "GOES_ARCHIVE"
        # animation source returns list.
        assert TOOL_REGISTRY[n].fn.__annotations__["return"] is list


def test_archive_signature_matches_twin():
    import inspect

    p = inspect.signature(TOOL_REGISTRY["fetch_goes_archive_animation"].fn).parameters
    assert list(p) == [
        "bbox", "satellite", "start_utc", "end_utc", "step_minutes",
        "band", "bt_c07_min_k", "bt_diff_min_k", "true_color_res_deg", "_extra_ignored",
    ]
    assert p["band"].default == "fire_temperature"
    assert p["satellite"].default == "goes-18"


def test_active_fire_signature_matches_twin():
    import inspect

    p = inspect.signature(TOOL_REGISTRY["fetch_goes_active_fire"].fn).parameters
    assert list(p) == [
        "bbox", "satellite", "start_utc", "end_utc",
        "bt_c07_min_k", "bt_diff_min_k", "_extra_ignored",
    ]


# --------------------------------------------------------------------------- #
# frames_plan: per-frame cache_params BYTE-identity + naming + presets.
# --------------------------------------------------------------------------- #


def test_archive_fire_temperature_cache_params_byte_identical(_stub_three_keys):
    plans = GA.frames_plan(_ARCH, {"bbox": _BBOX, "satellite": "GOES West", "band": "fire_temperature"})
    assert len(plans) == 3
    assert plans[0].cache_params == {
        "bbox": [-114.05, 37.0, -109.04, 42.0],
        "product": "fire_temperature",
        "satellite": "goes-18",
        "ts_start": "20260622140000",
        "gamma": 1,
        "res_deg": 0.02,
    }
    assert plans[0].name == "GOES Fire Temperature (Archive) step 1 2026-06-22T14:00:00Z (GOES-18)"
    assert plans[0].layer_id == "goes-arch-firetemp-20260622140000--114.050-37.000"
    assert plans[0].style_preset == "goes_rgb_animation"


def test_archive_hotspots_adds_thresholds_and_preset(_stub_three_keys):
    p = GA.frames_plan(_ARCH, {"bbox": _BBOX, "band": "fire_hotspots"})[0]
    assert p.cache_params["product"] == "fire_hotspots"
    assert p.cache_params["bt_c07_min_k"] == 320.0 and p.cache_params["bt_diff_min_k"] == 10.0
    assert p.cache_params["gamma"] == 1 and p.cache_params["res_deg"] == 0.02
    assert p.style_preset == "goes_fire_hotspots_rgba"


def test_archive_true_color_alias_and_finer_res(_stub_three_keys):
    p = GA.frames_plan(_ARCH, {"bbox": _BBOX, "band": "natural_color"})[0]
    assert p.cache_params["product"] == "true_color"
    assert p.cache_params["res_deg"] == 0.005
    assert p.layer_id.startswith("goes-arch-truecolor-")


def test_active_fire_cache_params_byte_identical(_stub_three_keys):
    p = GA.frames_plan(_AF, {"bbox": _BBOX, "satellite": "goes-18"})[0]
    assert p.cache_params == {
        "bbox": [-114.05, 37.0, -109.04, 42.0],
        "product": "fire_hotspots",
        "satellite": "goes-18",
        "ts_start": "20260622140000",
        "bt_c07_min_k": 320.0,
        "bt_diff_min_k": 10.0,
        "tool": "fetch_goes_active_fire",
    }
    assert p.name == "GOES Active Fire step 1 2026-06-22T14:00:00Z (GOES-18)"
    assert p.layer_id == "goes-activefire-20260622140000--114.050-37.000"


# --------------------------------------------------------------------------- #
# route(): list-return + scrubber grouping over REAL names + honesty floor.
# --------------------------------------------------------------------------- #


def test_route_returns_ordered_list_and_one_scrubber_group(_stub_three_keys):
    layers = TOOL_REGISTRY["fetch_goes_archive_animation"].fn(bbox=_BBOX, band="fire_temperature")
    assert isinstance(layers, list) and len(layers) == 3
    assert [l.layer_type for l in layers] == ["raster", "raster", "raster"]
    groups = group_frame_layers([l.name for l in layers])
    assert len(groups) == 1


def test_two_bands_form_two_synchronized_groups(_stub_three_keys):
    ft = TOOL_REGISTRY["fetch_goes_archive_animation"].fn(bbox=_BBOX, band="fire_temperature")
    tc = TOOL_REGISTRY["fetch_goes_archive_animation"].fn(bbox=_BBOX, band="true_color")
    groups = group_frame_layers([l.name for l in ft] + [l.name for l in tc])
    assert len(groups) == 2


def test_active_fire_route_returns_hotspot_frames(_stub_three_keys):
    layers = TOOL_REGISTRY["fetch_goes_active_fire"].fn(bbox=_BBOX)
    assert isinstance(layers, list) and len(layers) == 3
    assert layers[0].style_preset == "goes_fire_hotspots_rgba"


def test_honesty_floor_all_frames_degrade(_stub_three_keys, monkeypatch):
    def degrade(*a, **k):
        raise FrameDegraded("bbox off the disk")

    monkeypatch.setattr(core, "_fetch_archive_frame_cog_bytes", degrade)
    with pytest.raises(Exception) as ei:
        TOOL_REGISTRY["fetch_goes_archive_animation"].fn(bbox=_BBOX, band="fire_temperature")
    assert getattr(ei.value, "error_code", None) == "GOES_ARCHIVE_EMPTY"
    assert getattr(ei.value, "retryable", None) is False


def test_honesty_floor_empty_window(monkeypatch):
    monkeypatch.setattr(core, "_list_archive_keys_in_window", lambda *a, **k: [])
    with pytest.raises(Exception) as ei:
        TOOL_REGISTRY["fetch_goes_active_fire"].fn(bbox=_BBOX)
    assert getattr(ei.value, "error_code", None) == "GOES_ARCHIVE_EMPTY"


# --------------------------------------------------------------------------- #
# Input errors: satellite normalization + band gate + missing bbox.
# --------------------------------------------------------------------------- #


def test_unknown_satellite_raises_loud_normalizer_error():
    with pytest.raises(GOESInputError):
        TOOL_REGISTRY["fetch_goes_archive_animation"].fn(bbox=_BBOX, satellite="himawari-9")


def test_valid_but_unserved_bird_raises_source_input_error():
    # goes-19 IS served; goes-17 is a valid GOES bird NOT in the archive set.
    with pytest.raises(Exception) as ei:
        TOOL_REGISTRY["fetch_goes_archive_animation"].fn(bbox=_BBOX, satellite="goes-17")
    assert getattr(ei.value, "error_code", None) == "GOES_ARCHIVE_INPUT_INVALID"


def test_unknown_band_raises_source_input_error():
    with pytest.raises(Exception) as ei:
        TOOL_REGISTRY["fetch_goes_archive_animation"].fn(bbox=_BBOX, band="geocolor")
    assert getattr(ei.value, "error_code", None) == "GOES_ARCHIVE_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# Pure band-math core (relocated helpers).
# --------------------------------------------------------------------------- #


def test_fire_temperature_rgb_channel_stretches():
    c07 = np.array([[333.15]], dtype=np.float32)  # 60 C -> R=255
    c06 = np.array([[1.0]], dtype=np.float32)      # 100% -> G=255
    c05 = np.array([[0.75]], dtype=np.float32)     # 75%  -> B=255
    rgb = core._fire_temperature_rgb(c07, c06, c05)
    assert rgb.shape == (3, 1, 1)
    assert tuple(int(rgb[b, 0, 0]) for b in range(3)) == (255, 255, 255)


def test_true_color_synthetic_green_and_gamma():
    r = core._true_color_rgb(
        np.array([[1.0]], np.float32), np.array([[1.0]], np.float32), np.array([[1.0]], np.float32)
    )
    # all reflectances at 1.0 -> green coeffs sum to 1.0, gamma of 1.0 -> 255 each.
    assert tuple(int(r[b, 0, 0]) for b in range(3)) == (255, 255, 255)


def test_detect_active_fire_split_window_mask():
    c07 = np.array([[330.0, 305.0, 330.0]], np.float32)
    c13 = np.array([[300.0, 300.0, 325.0]], np.float32)  # diffs: 30, 5, 5
    mask = core._detect_active_fire_mask(c07, c13, 320.0, 10.0)
    assert list(mask[0]) == [True, False, False]


def test_fire_hotspots_rgba_alpha_isolates_fire():
    c07 = np.array([[330.0, 305.0]], np.float32)
    c13 = np.array([[300.0, 300.0]], np.float32)
    rgba = core._fire_hotspots_rgba(c07, c13, 320.0, 10.0)
    assert rgba.shape == (4, 1, 2)
    assert int(rgba[3, 0, 0]) == 255  # detected fire opaque
    assert int(rgba[3, 0, 1]) == 0    # non-fire transparent


def test_bake_fire_over_base_respects_alpha():
    base = np.zeros((3, 1, 2), np.uint8)
    fire = np.zeros((4, 1, 2), np.uint8)
    fire[:, 0, 0] = [255, 128, 0, 255]  # opaque fire at col 0
    baked = core._bake_fire_over_base(base, fire)
    assert list(baked[:, 0, 0]) == [255, 128, 0]  # fire replaces base
    assert list(baked[:, 0, 1]) == [0, 0, 0]      # base shows through


def test_select_window_keys_subsamples_keeping_endpoints():
    keys = [f"k{i}" for i in range(300)]
    kept = core._select_window_keys(keys, cap=144)
    assert len(kept) <= 144
    assert kept[0] == "k0" and kept[-1] == "k299"


def test_select_window_keys_under_cap_returns_all():
    keys = [f"k{i}" for i in range(10)]
    assert core._select_window_keys(keys, cap=144) == keys


def test_key_start_datetime_parses_abi_naming():
    t = core._key_start_datetime("OR_ABI-L2-MCMIPC-M6_G18_s20261731400000_e_c.nc")
    assert t == datetime(2026, 6, 22, 14, 0, 0, tzinfo=timezone.utc)
