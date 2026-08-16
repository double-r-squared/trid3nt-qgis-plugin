"""GLM animation-frames fold parity (ADR 0092): fetch_glm_lightning via the router.

Migrates the value-bearing coverage of the deleted fetch_glm_lightning twin
(test_fetch_glm_lightning.py) onto shape: animation_frames. The CONTRACT CHANGE
(approved, ADR 0092): the DEFAULT output is now a frames LIST -- the single
accumulation case is a ONE-frame list; ``accumulation_window_s`` fans the window into
scrubber-steppable ``step <N>`` frames. Covers:

- registration + spec-served + signature/return parity (list return),
- frames_plan bucket resolve (single -> 1 frame, accumulation -> N) + the twin's
  byte-identical per-frame cache_params + the ``step <N>`` scrubber name-token,
- frame_bytes GED binning to a REAL RGBA COG + FrameDegraded on an empty bucket,
- route() list-return + the plugin group_frame_layers scrubber proof (one group),
- the honesty floor (all buckets degrade / empty window -> GLM_EMPTY),
- typed input errors (unknown satellite / bad window / tiny accum / over-long single),
- the pure GED point-gridding math (bin + purple ramp) relocated into the hook module.

The S3 boundary is monkeypatched for a hermetic offline run; the real GLM archive path
is proven by the live proof recorded in ADR 0092. ASCII only.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pytest

from trid3nt_server.data import TOOL_REGISTRY
from trid3nt_server.data.fetchers._router import registration as reg
from trid3nt_server.data.fetchers._router import router
from trid3nt_server.data.fetchers._router.errors import RouterInputError
from trid3nt_server.data.fetchers._router.executors import animation_frames as EX
from trid3nt_server.data.fetchers._router.hooks import FrameDegraded
from trid3nt_server.data.fetchers._router.hooks import glm as GLM
from trid3nt_server.data.fetchers.imagery._goes_archive_core import _grid_for_bbox


def _load_group_frame_layers():
    spec = importlib.util.spec_from_file_location(
        "_plugin_temporal_gfl_glm", "plugin/render/temporal.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.group_frame_layers


group_frame_layers = _load_group_frame_layers()

_UT_BBOX = [-1.0, -1.0, 1.0, 1.0]  # 2 deg x 2 deg @ 0.02 -> 100 x 100
_SPEC = reg.get_spec("fetch_glm_lightning")


def _synthetic_groups(points):
    lon = np.array([p[0] for p in points], dtype=np.float64)
    lat = np.array([p[1] for p in points], dtype=np.float64)
    eng = np.array([p[2] for p in points], dtype=np.float64)
    return lat, lon, eng


class _R:
    def __init__(self, uri, data=b""):
        self.uri = uri
        self.data = data


def _wire_synthetic_glm(monkeypatch, n_keys=3):
    """Monkeypatch the S3 boundary + executor read_through so frame_bytes runs end-to-
    end on synthetic in-AOI groups and produces a REAL RGBA COG."""
    base = datetime(2025, 9, 7, 18, 0, tzinfo=timezone.utc)

    def _fake_list(satellite, start_dt, end_dt):
        out = []
        for i in range(n_keys):
            t = base + timedelta(seconds=20 * i)
            if start_dt <= t < end_dt:
                out.append((t, f"GLM-L2-LCFA/2025/250/18/k{i}.nc"))
        return out

    monkeypatch.setattr(GLM, "_list_glm_keys_in_window", _fake_list)
    monkeypatch.setattr(
        GLM, "_fetch_glm_groups",
        lambda *a, **k: _synthetic_groups([(0.0, 0.0, 5e-14), (0.2, -0.3, 2e-13), (-0.4, 0.4, 1e-13)]),
    )

    captured = {"calls": []}

    def _fake_rt(metadata, params, ext, fetch_fn):
        data = fetch_fn()  # exercises frame_bytes -> _fetch_glm_ged_cog_bytes
        captured["calls"].append({"params": params, "data": data})
        return _R(uri=f"s3://fake-cache/{params['start_utc']}.tif", data=data)

    monkeypatch.setattr(EX, "read_through", _fake_rt)
    return captured


def _assert_valid_rgba_cog(data: bytes):
    import rasterio

    with rasterio.open(io.BytesIO(data)) as ds:
        assert ds.count == 4
        assert ds.dtypes[0] == "uint8"
        assert ds.crs is not None and ds.crs.to_epsg() == 4326


# --------------------------------------------------------------------------- #
# Registration + surface parity.
# --------------------------------------------------------------------------- #


def test_glm_registered_and_served():
    assert "fetch_glm_lightning" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_glm_lightning"]
    assert entry.metadata.source_class == "goes_glm"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.supports_global_query is False
    assert _SPEC is not None and _SPEC.shape == "animation_frames"
    assert _SPEC.error_code_prefix == "GLM"
    # animation source returns a list (the new default contract).
    assert entry.fn.__annotations__["return"] is list


def test_glm_signature_matches_twin():
    import inspect

    p = inspect.signature(TOOL_REGISTRY["fetch_glm_lightning"].fn).parameters
    assert list(p) == ["bbox", "satellite", "start_utc", "end_utc", "accumulation_window_s", "_extra_ignored"]
    assert p["satellite"].default == "goes-19"


def test_glm_in_corpus():
    from trid3nt_server.data.search.search_tools.search_tools import _load_corpus

    corpus = _load_corpus()
    assert "fetch_glm_lightning" in corpus and len(corpus["fetch_glm_lightning"]) >= 3


# --------------------------------------------------------------------------- #
# frames_plan: single -> one-frame list; accumulation -> N; cache_params identity.
# --------------------------------------------------------------------------- #


def test_single_mode_is_a_one_frame_plan():
    plans = GLM.frames_plan(
        _SPEC, {"bbox": _UT_BBOX, "satellite": "goes-19",
                "start_utc": "2025-09-07T18:00:00Z", "end_utc": "2025-09-07T18:03:00Z"}
    )
    assert len(plans) == 1
    p = plans[0]
    assert p.cache_params == {
        "bbox": [-1.0, -1.0, 1.0, 1.0],
        "satellite": "goes-19",
        "product": "glm_ged",
        "start_utc": "2025-09-07T18:00:00Z",
        "end_utc": "2025-09-07T18:03:00Z",
        "ramp_fj": [GLM.GED_FJ_FLOOR, GLM.GED_FJ_CEILING],
        "res_deg": GLM._OUT_RES_DEG,
        "tool": "fetch_glm_lightning",
    }
    assert "step 1" in p.name and "(GOES-19)" in p.name
    assert p.layer_id.startswith("glm-ged-goes-19-")


def test_accumulation_mode_fans_into_step_frames():
    plans = GLM.frames_plan(
        _SPEC, {"bbox": _UT_BBOX, "start_utc": "2025-09-07T18:00:00Z",
                "end_utc": "2025-09-07T18:03:00Z", "accumulation_window_s": 60}
    )
    assert len(plans) == 3  # 3 x 60 s buckets over 3 min
    for n, p in enumerate(plans, start=1):
        assert f"step {n}" in p.name
    assert len({p.layer_id for p in plans}) == 3


def test_satellite_spelling_normalized_in_plan():
    p = GLM.frames_plan(_SPEC, {"bbox": _UT_BBOX, "satellite": "GOES West",
                                "start_utc": "2025-09-07T18:00:00Z", "end_utc": "2025-09-07T18:03:00Z"})[0]
    assert p.cache_params["satellite"] == "goes-18"
    assert "(GOES-18)" in p.name


# --------------------------------------------------------------------------- #
# route(): list-return + one scrubber group + per-frame RGBA COG.
# --------------------------------------------------------------------------- #


def test_route_single_returns_one_frame_list_and_one_group(monkeypatch):
    captured = _wire_synthetic_glm(monkeypatch)
    layers = TOOL_REGISTRY["fetch_glm_lightning"].fn(
        bbox=_UT_BBOX, satellite="goes-19",
        start_utc="2025-09-07T18:00:00Z", end_utc="2025-09-07T18:03:00Z",
    )
    assert isinstance(layers, list) and len(layers) == 1
    assert layers[0].layer_type == "raster" and layers[0].role == "context"
    assert layers[0].style_preset == "glm_lightning"
    assert "step 1" in layers[0].name
    # A ONE-frame list is a static overlay, not a scrubbable animation group.
    assert group_frame_layers([l.name for l in layers]) == []
    _assert_valid_rgba_cog(captured["calls"][0]["data"])


def test_route_accumulation_returns_ordered_scrubber_group(monkeypatch):
    _wire_synthetic_glm(monkeypatch, n_keys=9)
    layers = TOOL_REGISTRY["fetch_glm_lightning"].fn(
        bbox=_UT_BBOX, start_utc="2025-09-07T18:00:00Z",
        end_utc="2025-09-07T18:03:00Z", accumulation_window_s=60,
    )
    assert isinstance(layers, list) and len(layers) == 3
    for n, l in enumerate(layers, start=1):
        assert f"step {n}" in l.name
        assert l.style_preset == "glm_lightning" and tuple(l.bbox) == tuple(_UT_BBOX)
    groups = group_frame_layers([l.name for l in layers])
    assert len(groups) == 1  # one scrubber group, 3 ordered step frames


# --------------------------------------------------------------------------- #
# Honesty floor: FrameDegraded skip + all-degrade / empty-window -> GLM_EMPTY.
# --------------------------------------------------------------------------- #


def test_empty_bucket_skipped_not_emitted_blank(monkeypatch):
    _wire_synthetic_glm(monkeypatch, n_keys=9)
    real = GLM._fetch_glm_ged_cog_bytes
    calls = {"n": 0}

    def _maybe_empty(satellite, bbox, start_dt, end_dt):
        calls["n"] += 1
        if calls["n"] == 2:
            raise GLM._GLMEmpty("synthetic empty bucket")
        return real(satellite, bbox, start_dt, end_dt)

    monkeypatch.setattr(GLM, "_fetch_glm_ged_cog_bytes", _maybe_empty)
    layers = TOOL_REGISTRY["fetch_glm_lightning"].fn(
        bbox=_UT_BBOX, start_utc="2025-09-07T18:00:00Z",
        end_utc="2025-09-07T18:03:00Z", accumulation_window_s=60,
    )
    assert isinstance(layers, list) and len(layers) == 2  # middle empty bucket dropped


def test_frame_bytes_maps_empty_to_frame_degraded(monkeypatch):
    monkeypatch.setattr(GLM, "_list_glm_keys_in_window", lambda *a, **k: [])
    plan = GLM.frames_plan(_SPEC, {"bbox": _UT_BBOX,
                                   "start_utc": "2025-09-07T18:00:00Z", "end_utc": "2025-09-07T18:03:00Z"})[0]
    with pytest.raises(FrameDegraded):
        GLM.frame_bytes(_SPEC, {}, plan)


def test_single_empty_window_surfaces_typed_empty(monkeypatch):
    monkeypatch.setattr(GLM, "_list_glm_keys_in_window", lambda *a, **k: [])
    with pytest.raises(Exception) as ei:
        TOOL_REGISTRY["fetch_glm_lightning"].fn(
            bbox=_UT_BBOX, start_utc="2025-09-07T18:00:00Z", end_utc="2025-09-07T18:03:00Z"
        )
    assert getattr(ei.value, "error_code", None) == "GLM_EMPTY"


def test_no_in_aoi_groups_surfaces_typed_empty(monkeypatch):
    t = datetime(2025, 9, 7, 18, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(GLM, "_list_glm_keys_in_window", lambda *a, **k: [(t, "GLM-L2-LCFA/k.nc")])
    monkeypatch.setattr(GLM, "_fetch_glm_groups", lambda *a, **k: _synthetic_groups([(50.0, 50.0, 1e-13)]))
    with pytest.raises(Exception) as ei:
        TOOL_REGISTRY["fetch_glm_lightning"].fn(
            bbox=_UT_BBOX, start_utc="2025-09-07T18:00:00Z", end_utc="2025-09-07T18:03:00Z"
        )
    assert getattr(ei.value, "error_code", None) == "GLM_EMPTY"


# --------------------------------------------------------------------------- #
# Typed input errors (pre-network).
# --------------------------------------------------------------------------- #


def test_unknown_satellite_raises_glm_input_invalid():
    with pytest.raises(RouterInputError) as ei:
        TOOL_REGISTRY["fetch_glm_lightning"].fn(bbox=_UT_BBOX, satellite="goes-99")
    assert ei.value.error_code == "GLM_INPUT_INVALID"


def test_historical_bird_accepted():
    # goes-16 / goes-17 are historical birds the GLM archive set DOES serve.
    p = GLM.frames_plan(_SPEC, {"bbox": _UT_BBOX, "satellite": "goes-16",
                                "start_utc": "2025-09-07T18:00:00Z", "end_utc": "2025-09-07T18:03:00Z"})[0]
    assert p.cache_params["satellite"] == "goes-16"


def test_start_after_end_raises_input():
    with pytest.raises(RouterInputError):
        GLM.frames_plan(_SPEC, {"bbox": _UT_BBOX,
                                "start_utc": "2025-09-07T18:10:00Z", "end_utc": "2025-09-07T18:00:00Z"})


def test_over_long_single_window_raises_input():
    with pytest.raises(RouterInputError):
        GLM.frames_plan(_SPEC, {"bbox": _UT_BBOX,
                                "start_utc": "2025-09-07T18:00:00Z", "end_utc": "2025-09-07T19:00:00Z"})


def test_tiny_accumulation_window_raises_input():
    with pytest.raises(RouterInputError):
        GLM.frames_plan(_SPEC, {"bbox": _UT_BBOX, "start_utc": "2025-09-07T18:00:00Z",
                                "end_utc": "2025-09-07T18:03:00Z", "accumulation_window_s": 5})


# --------------------------------------------------------------------------- #
# Pure GED point-gridding math (relocated helpers).
# --------------------------------------------------------------------------- #


def test_bin_ged_places_energy_in_correct_north_up_cell():
    _, width, height = _grid_for_bbox(tuple(_UT_BBOX))
    assert (height, width) == (100, 100)
    lat, lon, eng = _synthetic_groups([(0.5, 0.5, 1e-14)])
    ged_j, n_in = GLM._bin_ged(lat, lon, eng, tuple(_UT_BBOX), width, height)
    assert n_in == 1
    assert ged_j[25, 75] == pytest.approx(1e-14)  # col 75, row 25 (north-up)
    assert ged_j.sum() == pytest.approx(1e-14)


def test_bin_ged_sums_coincident_and_excludes_outside():
    _, width, height = _grid_for_bbox(tuple(_UT_BBOX))
    lat, lon, eng = _synthetic_groups([(0.5, 0.5, 1e-14), (0.505, 0.495, 2e-14), (5.0, 5.0, 9e-14)])
    ged_j, n_in = GLM._bin_ged(lat, lon, eng, tuple(_UT_BBOX), width, height)
    assert n_in == 2  # the far-outside group is excluded
    assert ged_j[25, 75] == pytest.approx(3e-14)  # add.at sums coincident


def test_ged_to_purple_rgba_zeros_transparent_lit_opaque():
    ged = np.zeros((4, 4), dtype=np.float64)
    ged[1, 1] = 1e-13  # 100 fJ -> mid ramp
    rgba = GLM._ged_to_purple_rgba(ged)
    assert rgba.shape == (4, 4, 4) and rgba.dtype == np.uint8
    assert rgba[3, 1, 1] >= 120  # lit cell at least ~50% opaque
    mask = np.ones((4, 4), dtype=bool)
    mask[1, 1] = False
    assert (rgba[3][mask] == 0).all()  # every non-lit cell transparent


def test_glm_key_start_datetime_parses_doy():
    key = "GLM-L2-LCFA/2025/250/18/OR_GLM-L2-LCFA_G19_s20252501801000_e..._c....nc"
    assert GLM._glm_key_start_datetime(key) == datetime(2025, 9, 7, 18, 1, 0, tzinfo=timezone.utc)


def test_payload_estimator_is_small():
    estimate = router.synthesize_payload_estimator(_SPEC)
    mb = estimate(bbox=(-83.5, 25.5, -79.5, 31.5))  # 4 x 6 deg
    assert 0.0 < mb < 25.0
