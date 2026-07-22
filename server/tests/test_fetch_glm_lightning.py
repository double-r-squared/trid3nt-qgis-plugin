"""Unit tests for ``fetch_glm_lightning`` (GOES GLM group-energy-density fetcher).

Covers: registration + category + corpus coverage (per-tool convention), the GED
binning correctness on SYNTHETIC events (no network), the purple-ramp transparency,
the honesty floor (no granules / no in-AOI groups -> typed empty), input validation,
and the ``step <N>`` animation contract -- all with the S3 boundary monkeypatched.

ASCII only.
"""

from __future__ import annotations

import io
import pathlib
from datetime import datetime, timezone

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools import fetch_glm_lightning as glmmod
from trid3nt_server.tools.fetch_glm_lightning import (
    GED_FJ_CEILING,
    GLMBboxRequiredError,
    GLMEmptyError,
    GLMInputError,
    _bin_ged,
    _ged_to_purple_rgba,
    _fetch_glm_ged_cog_bytes,
    _glm_hour_prefixes,
    _glm_key_start_datetime,
    estimate_payload_mb,
    fetch_glm_lightning,
)
from trid3nt_server.tools.fetch_goes_archive_animation import _OUT_RES_DEG, _grid_for_bbox

# A small AOI for fast synthetic grids (2 deg x 2 deg @ 0.02 deg -> 100 x 100).
_UT_BBOX = (-1.0, -1.0, 1.0, 1.0)


class _FakeReadResult:
    __slots__ = ("uri", "data", "hit")

    def __init__(self, uri, data=b"", hit=False):
        self.uri = uri
        self.data = data
        self.hit = hit


def _synthetic_groups(points):
    """points: list of (lon, lat, energy_J) -> (lat, lon, eng) float64 arrays."""
    lon = np.array([p[0] for p in points], dtype=np.float64)
    lat = np.array([p[1] for p in points], dtype=np.float64)
    eng = np.array([p[2] for p in points], dtype=np.float64)
    return lat, lon, eng


# ---------------------------------------------------------------------------
# Registration + category + corpus coverage (the per-tool convention).
# ---------------------------------------------------------------------------
def test_tool_is_registered():
    assert "fetch_glm_lightning" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_glm_lightning"]
    assert entry.metadata.name == "fetch_glm_lightning"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "goes_glm"
    assert entry.metadata.cacheable is True
    assert entry.metadata.supports_global_query is False
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


def test_tool_categorized_under_weather_and_fire():
    from trid3nt_server.categories import PRIMARY_CATEGORY, SECONDARY_CATEGORIES

    assert PRIMARY_CATEGORY.get("fetch_glm_lightning") == "weather_atmosphere"
    assert SECONDARY_CATEGORIES.get("fetch_glm_lightning") == ("fire",)


def test_tool_in_query_corpus():
    import yaml

    corpus_path = (
        pathlib.Path(glmmod.__file__).resolve().parents[1]
        / "data"
        / "tool_query_corpus.yaml"
    )
    corpus = yaml.safe_load(corpus_path.read_text())
    assert "fetch_glm_lightning" in corpus
    assert len(corpus["fetch_glm_lightning"]) >= 3


# ---------------------------------------------------------------------------
# GED binning correctness (pure, synthetic -- no network).
# ---------------------------------------------------------------------------
def test_bin_ged_places_energy_in_correct_north_up_cell():
    _, width, height = _grid_for_bbox(_UT_BBOX)
    assert (height, width) == (100, 100)
    # A single group at lon=0.5, lat=0.5, energy=1e-14 J.
    lat, lon, eng = _synthetic_groups([(0.5, 0.5, 1e-14)])
    ged_j, n_in = _bin_ged(lat, lon, eng, _UT_BBOX, width, height)
    assert n_in == 1
    # col = (0.5 - (-1.0)) / 0.02 = 75 ; row = (1.0 - 0.5) / 0.02 = 25 (north-up).
    assert ged_j[25, 75] == pytest.approx(1e-14)
    assert ged_j.sum() == pytest.approx(1e-14)  # everything else is zero


def test_bin_ged_sums_coincident_groups():
    _, width, height = _grid_for_bbox(_UT_BBOX)
    lat, lon, eng = _synthetic_groups(
        [(0.5, 0.5, 1e-14), (0.505, 0.495, 2e-14)]  # same 0.02-deg cell
    )
    ged_j, n_in = _bin_ged(lat, lon, eng, _UT_BBOX, width, height)
    assert n_in == 2
    assert ged_j[25, 75] == pytest.approx(3e-14)  # numpy.add.at sums, not overwrites
    assert (ged_j > 0).sum() == 1


def test_bin_ged_excludes_groups_outside_bbox():
    _, width, height = _grid_for_bbox(_UT_BBOX)
    lat, lon, eng = _synthetic_groups(
        [(0.5, 0.5, 1e-14), (5.0, 5.0, 9e-14)]  # second is far outside
    )
    ged_j, n_in = _bin_ged(lat, lon, eng, _UT_BBOX, width, height)
    assert n_in == 1
    assert ged_j.sum() == pytest.approx(1e-14)


# ---------------------------------------------------------------------------
# Purple log-ramp transparency.
# ---------------------------------------------------------------------------
def test_ged_to_purple_rgba_zeros_transparent_lit_opaque():
    ged = np.zeros((4, 4), dtype=np.float64)
    ged[1, 1] = 1e-13  # 100 fJ -> mid ramp
    rgba = _ged_to_purple_rgba(ged)
    assert rgba.shape == (4, 4, 4)  # (bands, H, W)
    assert rgba.dtype == np.uint8
    alpha = rgba[3]
    assert alpha[1, 1] >= 120  # lit cell is at least ~50% opaque
    # every non-lit cell is fully transparent
    mask = np.ones((4, 4), dtype=bool)
    mask[1, 1] = False
    assert (alpha[mask] == 0).all()
    # purple: blue + red present, green muted at mid ramp
    assert rgba[2, 1, 1] > 0 and rgba[0, 1, 1] > 0


def test_ged_ramp_ceiling_saturates():
    ged = np.zeros((2, 2), dtype=np.float64)
    ged[0, 0] = (GED_FJ_CEILING * 100.0) * 1e-15  # well above ceiling
    rgba = _ged_to_purple_rgba(ged)
    # at/above ceiling the head goes white-pink (green channel engaged) + max alpha
    assert rgba[1, 0, 0] > 200  # green high near the white head
    assert rgba[3, 0, 0] == 255


# ---------------------------------------------------------------------------
# Honesty floor (typed empty -- never a blank overlay).
# ---------------------------------------------------------------------------
def test_no_granules_raises_typed_empty(monkeypatch):
    monkeypatch.setattr(glmmod, "_list_glm_keys_in_window", lambda *a, **k: [])
    with pytest.raises(GLMEmptyError):
        _fetch_glm_ged_cog_bytes(
            "goes-19",
            _UT_BBOX,
            datetime(2025, 9, 7, 18, 0, tzinfo=timezone.utc),
            datetime(2025, 9, 7, 18, 3, tzinfo=timezone.utc),
        )


def test_granules_but_no_in_aoi_groups_raises_typed_empty(monkeypatch):
    t = datetime(2025, 9, 7, 18, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        glmmod, "_list_glm_keys_in_window", lambda *a, **k: [(t, "GLM-L2-LCFA/k.nc")]
    )
    # all groups fall OUTSIDE _UT_BBOX
    monkeypatch.setattr(
        glmmod,
        "_fetch_glm_groups",
        lambda *a, **k: _synthetic_groups([(50.0, 50.0, 1e-13)]),
    )
    with pytest.raises(GLMEmptyError):
        _fetch_glm_ged_cog_bytes(
            "goes-19", _UT_BBOX, t, datetime(2025, 9, 7, 18, 3, tzinfo=timezone.utc)
        )


# ---------------------------------------------------------------------------
# Input validation (typed, BEFORE any network call).
# ---------------------------------------------------------------------------
def test_bbox_none_raises_bbox_required():
    with pytest.raises(GLMBboxRequiredError) as ei:
        fetch_glm_lightning(bbox=None)
    assert ei.value.error_code == "BBOX_REQUIRED"
    assert ei.value.retryable is False


def test_bad_bbox_shape_raises_input_error():
    with pytest.raises(GLMInputError):
        fetch_glm_lightning(bbox=(1.0, 2.0, 3.0))  # only 3 values


def test_unknown_satellite_raises_input_error():
    with pytest.raises(GLMInputError):
        fetch_glm_lightning(bbox=_UT_BBOX, satellite="goes-99")


def test_start_after_end_raises_input_error():
    with pytest.raises(GLMInputError):
        fetch_glm_lightning(
            bbox=_UT_BBOX,
            start_utc="2025-09-07T18:10:00Z",
            end_utc="2025-09-07T18:00:00Z",
        )


def test_single_frame_window_too_long_raises_input_error():
    with pytest.raises(GLMInputError):
        fetch_glm_lightning(
            bbox=_UT_BBOX,
            start_utc="2025-09-07T18:00:00Z",
            end_utc="2025-09-07T19:00:00Z",  # 60 min > 20 min single-frame cap
        )


def test_tiny_accumulation_window_raises_input_error():
    with pytest.raises(GLMInputError):
        fetch_glm_lightning(
            bbox=_UT_BBOX,
            start_utc="2025-09-07T18:00:00Z",
            end_utc="2025-09-07T18:03:00Z",
            accumulation_window_s=5,  # < one ~20 s granule
        )


# ---------------------------------------------------------------------------
# Key/prefix parsing (pure).
# ---------------------------------------------------------------------------
def test_glm_key_start_datetime_parses_doy():
    key = "GLM-L2-LCFA/2025/250/18/OR_GLM-L2-LCFA_G19_s20252501801000_e..._c....nc"
    dt = _glm_key_start_datetime(key)
    assert dt == datetime(2025, 9, 7, 18, 1, 0, tzinfo=timezone.utc)  # DOY 250 = Sep 7


def test_glm_hour_prefixes_span_window():
    start = datetime(2025, 9, 7, 18, 50, tzinfo=timezone.utc)
    end = datetime(2025, 9, 7, 20, 5, tzinfo=timezone.utc)
    prefixes = _glm_hour_prefixes(start, end)
    assert prefixes == [
        "GLM-L2-LCFA/2025/250/18/",
        "GLM-L2-LCFA/2025/250/19/",
        "GLM-L2-LCFA/2025/250/20/",
    ]


# ---------------------------------------------------------------------------
# Full-tool happy path: single frame + animation (S3 boundary monkeypatched).
# ---------------------------------------------------------------------------
def _wire_synthetic_glm(monkeypatch, n_keys=3):
    """Monkeypatch the S3 boundary + read_through so fetch_fn runs end-to-end on
    synthetic in-AOI groups and produces a REAL COG via _rgba_array_to_cog_bytes."""
    base = datetime(2025, 9, 7, 18, 0, tzinfo=timezone.utc)
    from datetime import timedelta

    def _fake_list(satellite, start_dt, end_dt):
        out = []
        for i in range(n_keys):
            t = base + timedelta(seconds=20 * i)
            if start_dt <= t < end_dt:
                out.append((t, f"GLM-L2-LCFA/2025/250/18/k{i}.nc"))
        return out

    monkeypatch.setattr(glmmod, "_list_glm_keys_in_window", _fake_list)
    monkeypatch.setattr(
        glmmod,
        "_fetch_glm_groups",
        lambda *a, **k: _synthetic_groups(
            [(0.0, 0.0, 5e-14), (0.2, -0.3, 2e-13), (-0.4, 0.4, 1e-13)]
        ),
    )

    captured = {"calls": []}

    def _fake_read_through(metadata, params, ext, fetch_fn):
        data = fetch_fn()  # runs _fetch_glm_ged_cog_bytes on the synthetic groups
        captured["calls"].append({"params": params, "len": len(data), "data": data})
        return _FakeReadResult(uri=f"s3://fake-cache/{params['start_utc']}.tif", data=data)

    monkeypatch.setattr(glmmod, "read_through", _fake_read_through)
    return captured


def _assert_valid_rgba_cog(data: bytes):
    import rasterio

    with rasterio.open(io.BytesIO(data)) as ds:
        assert ds.count == 4
        assert ds.dtypes[0] == "uint8"
        assert ds.crs is not None and ds.crs.to_epsg() == 4326


def test_single_frame_returns_rgba_layer(monkeypatch):
    captured = _wire_synthetic_glm(monkeypatch)
    layer = fetch_glm_lightning(
        bbox=_UT_BBOX,
        satellite="goes-19",
        start_utc="2025-09-07T18:00:00Z",
        end_utc="2025-09-07T18:03:00Z",
    )
    # default (no accumulation_window_s) -> a SINGLE LayerURI, not a list
    assert not isinstance(layer, list)
    assert layer.layer_type == "raster"
    assert layer.role == "context"
    assert layer.style_preset == "glm_lightning"
    assert tuple(layer.bbox) == _UT_BBOX
    assert layer.uri.startswith("s3://fake-cache/")
    assert "step" not in layer.name  # single frame is not an animation member
    # the COG that fetch_fn produced is a real 4-band RGBA EPSG:4326 raster
    assert len(captured["calls"]) == 1
    _assert_valid_rgba_cog(captured["calls"][0]["data"])


def test_animation_returns_ordered_step_frames(monkeypatch):
    _wire_synthetic_glm(monkeypatch, n_keys=9)
    layers = fetch_glm_lightning(
        bbox=_UT_BBOX,
        satellite="goes-19",
        start_utc="2025-09-07T18:00:00Z",
        end_utc="2025-09-07T18:03:00Z",
        accumulation_window_s=60,  # 3 buckets over 3 min
    )
    assert isinstance(layers, list)
    assert len(layers) == 3
    for n, layer in enumerate(layers, start=1):
        assert f"step {n}" in layer.name
        assert layer.style_preset == "glm_lightning"  # identical preset (grouping key)
        assert tuple(layer.bbox) == _UT_BBOX  # identical bbox (grouping key)
        assert layer.layer_type == "raster"
    # distinct frames
    assert len({lyr.layer_id for lyr in layers}) == 3


def test_animation_skips_empty_buckets(monkeypatch):
    """A bucket with no in-AOI lightning is skipped, not emitted blank."""
    captured = _wire_synthetic_glm(monkeypatch, n_keys=9)
    real_fetch = glmmod._fetch_glm_ged_cog_bytes
    calls = {"n": 0}

    def _maybe_empty(satellite, bbox, start_dt, end_dt):
        calls["n"] += 1
        if calls["n"] == 2:  # second bucket has no lightning
            raise GLMEmptyError("synthetic empty bucket")
        return real_fetch(satellite, bbox, start_dt, end_dt)

    monkeypatch.setattr(glmmod, "_fetch_glm_ged_cog_bytes", _maybe_empty)
    layers = fetch_glm_lightning(
        bbox=_UT_BBOX,
        start_utc="2025-09-07T18:00:00Z",
        end_utc="2025-09-07T18:03:00Z",
        accumulation_window_s=60,
    )
    assert isinstance(layers, list)
    assert len(layers) == 2  # the empty middle bucket was skipped, not emitted


# ---------------------------------------------------------------------------
# Satellite-spelling normalization (shared _normalize_satellite seam).
#
# GOES-18 / goes18 / "GOES West" used to be REJECTED by the bare-string
# membership check; after migrating to the shared normalizer they canonicalize
# to "goes-18" and the tool proceeds. A truly-unknown bird still raises loud.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "spelling, expected_token, expected_label",
    [
        ("GOES-18", "goes-18", "GOES-18"),   # hyphenated upper-case
        ("goes18", "goes-18", "GOES-18"),    # glued, no hyphen
        ("GOES West", "goes-18", "GOES-18"),  # directional alias -> current West
        ("G19", "goes-19", "GOES-19"),       # filename code
        ("east", "goes-19", "GOES-19"),      # bare directional -> current East
    ],
)
def test_satellite_spelling_accepted_and_canonicalized(
    monkeypatch, spelling, expected_token, expected_label
):
    """A forgiving spelling resolves to the canonical bird and proceeds (single frame)."""
    captured = _wire_synthetic_glm(monkeypatch)
    layer = fetch_glm_lightning(
        bbox=_UT_BBOX,
        satellite=spelling,
        start_utc="2025-09-07T18:00:00Z",
        end_utc="2025-09-07T18:03:00Z",
    )
    assert not isinstance(layer, list)
    assert layer.layer_type == "raster"
    # the canonical token flows into the layer_id + cache params + name label
    assert expected_token in layer.layer_id
    assert f"({expected_label})" in layer.name
    assert len(captured["calls"]) == 1
    assert captured["calls"][0]["params"]["satellite"] == expected_token
    _assert_valid_rgba_cog(captured["calls"][0]["data"])


def test_genuinely_unknown_satellite_raises_loud_glm_input_error():
    """A non-existent bird (GOES-99) fails LOUD as this tool's own typed error,
    not the shared normalizer's base GOESInputError (no leak across the seam)."""
    from trid3nt_server.tools.fetch_goes_satellite import GOESInputError

    with pytest.raises(GLMInputError) as ei:
        fetch_glm_lightning(bbox=_UT_BBOX, satellite="GOES-99")
    # the base GOES error type must NOT leak out of the GLM fetcher
    assert not isinstance(ei.value, GOESInputError)
    assert ei.value.error_code == "GLM_INPUT_INVALID"
    assert ei.value.retryable is False


def test_non_string_satellite_raises_loud_glm_input_error():
    """A non-string satellite is rejected loud as GLMInputError (re-wrapped seam error)."""
    with pytest.raises(GLMInputError):
        fetch_glm_lightning(bbox=_UT_BBOX, satellite=18)  # bare int, not "18"


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------
def test_estimate_payload_mb_scales_and_is_small():
    assert estimate_payload_mb(bbox=None) == 2.0
    mb = estimate_payload_mb(bbox=(-83.5, 25.5, -79.5, 31.5))  # 4 x 6 deg
    assert 0.0 < mb < 25.0  # never trips the chat-warn threshold
    assert estimate_payload_mb(bbox="garbage") == 2.0  # defensive default
