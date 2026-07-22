"""Unit tests for ``fetch_goes_active_fire`` -- the standalone GOES split-window
active-fire detector tool.

Coverage:
- Registration + metadata.
- Category membership (primary 'fire') + the corpus surface.
- The emitted hotspot LayerURI contract (label, transparent RGBA style preset,
  step token, ISO valid-time, bbox passthrough).
- The detector reuses the archive fire_hotspots composite path on the 2 km grid.
- Honesty floor: no archived frames OR no detected hot pixels -> typed empty.
- Tunable split-window thresholds flow through to the fetch path.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.imagery import fetch_goes_active_fire as afmod
from trid3nt_server.tools.fetchers.imagery import fetch_goes_archive_animation as archmod
from trid3nt_server.tools.fetchers.imagery.fetch_goes_active_fire import fetch_goes_active_fire
from trid3nt_server.tools.fetchers.imagery.fetch_goes_archive_animation import (
    FIRE_BT_C07_MIN_K,
    FIRE_BT_DIFF_MIN_K,
    GOESArchiveEmptyError,
    GOESArchiveInputError,
)
from trid3nt_server.tools.fetchers.imagery.fetch_goes_satellite import GOESInputError

_UT_BBOX = (-114.05, 37.0, -109.04, 42.0)


def _mk_key(dt: datetime) -> str:
    doy = dt.timetuple().tm_yday
    s = f"{dt.year:04d}{doy:03d}{dt.hour:02d}{dt.minute:02d}{dt.second:02d}0"
    return (
        f"ABI-L2-MCMIPC/{dt.year}/{doy:03d}/{dt.hour:02d}/"
        f"OR_ABI-L2-MCMIPC-M6_G18_s{s}_e..._c....nc"
    )


class _FakeReadResult:
    def __init__(self, uri):
        self.uri = uri


# ---- registration ---------------------------------------------------------


def test_tool_is_registered():
    assert "fetch_goes_active_fire" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_goes_active_fire"]
    assert entry.metadata.name == "fetch_goes_active_fire"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "goes_animation"
    assert entry.metadata.cacheable is True


def test_tool_categorized_under_fire():
    from trid3nt_server.categories import PRIMARY_CATEGORY

    assert PRIMARY_CATEGORY.get("fetch_goes_active_fire") == "fire"


def test_tool_in_query_corpus():
    import pathlib

    import yaml

    from trid3nt_server.tools.discovery.discover_dataset import _default_corpus_path

    corpus_path = pathlib.Path(_default_corpus_path())
    corpus = yaml.safe_load(corpus_path.read_text())
    assert "fetch_goes_active_fire" in corpus
    assert len(corpus["fetch_goes_active_fire"]) >= 3


# ---- input validation -----------------------------------------------------


def test_bbox_none_raises():
    from trid3nt_server.tools.fetchers.imagery.fetch_goes_archive_animation import (
        GOESArchiveBboxRequiredError,
    )

    with pytest.raises(GOESArchiveBboxRequiredError):
        fetch_goes_active_fire(bbox=None)  # type: ignore[arg-type]


def test_unknown_satellite_raises():
    """A genuinely-unknown bird fails LOUD via the shared satellite normalizer
    (typed GOESInputError listing accepted forms) -- never a silent 404 / empty."""
    with pytest.raises(GOESInputError):
        fetch_goes_active_fire(bbox=_UT_BBOX, satellite="GOES-99")
    with pytest.raises(GOESInputError):
        fetch_goes_active_fire(bbox=_UT_BBOX, satellite="himawari-9")


def test_valid_but_unsupported_satellite_raises_tool_error():
    """A REAL GOES bird this tool does not serve (goes-17, absent from
    GOES_ARCHIVE_SATELLITES) normalizes fine then raises THIS tool's own
    GOESArchiveInputError -- the base normalizer error type is not leaked here."""
    with pytest.raises(GOESArchiveInputError):
        fetch_goes_active_fire(bbox=_UT_BBOX, satellite="goes-17")


@pytest.mark.parametrize("spelling", ["GOES-18", "goes18", "GOES West", "G18", "18"])
def test_accepts_satellite_spelling_variants(monkeypatch, spelling):
    """Forgiving satellite spellings (GOES-18 / goes18 / GOES West / G18 / 18) all
    normalize to the canonical goes-18 bird and the tool proceeds, rather than
    being rejected by the allow-list membership check."""
    times = [datetime(2026, 6, 23, 19, 20, tzinfo=timezone.utc)]
    pairs = [(t, _mk_key(t)) for t in times]
    monkeypatch.setattr(afmod, "_list_archive_keys_in_window", lambda *a, **k: list(pairs))

    seen = {}

    def _fake_read_through(metadata, params, ext, fetch_fn):
        # The normalized canonical token must reach the cache-key params (and thus
        # every downstream bucket/key/path), not the raw spelling.
        seen["satellite"] = params["satellite"]
        return _FakeReadResult(uri=f"s3://fake/{params['ts_start']}.tif")

    monkeypatch.setattr(afmod, "read_through", _fake_read_through)

    layers = fetch_goes_active_fire(
        bbox=_UT_BBOX,
        satellite=spelling,
        start_utc="2026-06-23T19:15:00Z",
        end_utc="2026-06-23T19:30:00Z",
    )
    assert len(layers) == 1
    assert seen["satellite"] == "goes-18"
    # The canonical bird flows into the emitted layer label too.
    assert layers[0].name.endswith("(GOES-18)")


def test_start_after_end_raises():
    with pytest.raises(GOESArchiveInputError):
        fetch_goes_active_fire(
            bbox=_UT_BBOX,
            start_utc="2026-06-23T20:00:00Z",
            end_utc="2026-06-23T13:00:00Z",
        )


# ---- emitted hotspot LayerURI contract ------------------------------------


def test_emits_transparent_rgba_hotspot_layers(monkeypatch):
    """The tool emits ordered hotspot LayerURIs with the transparent-overlay style
    preset + 'GOES Active Fire' label + step token + ISO time, and routes through
    the archive fire_hotspots composite path."""
    times = [datetime(2026, 6, 23, 19, m, tzinfo=timezone.utc) for m in (20, 25)]
    pairs = [(t, _mk_key(t)) for t in times]
    monkeypatch.setattr(afmod, "_list_archive_keys_in_window", lambda *a, **k: list(pairs))

    captured = {}

    def _fake_read_through(metadata, params, ext, fetch_fn):
        captured["product"] = params["product"]
        captured["bt_c07_min_k"] = params["bt_c07_min_k"]
        captured["bt_diff_min_k"] = params["bt_diff_min_k"]
        return _FakeReadResult(uri=f"s3://fake/{params['ts_start']}.tif")

    monkeypatch.setattr(afmod, "read_through", _fake_read_through)

    layers = fetch_goes_active_fire(
        bbox=_UT_BBOX,
        satellite="goes-18",
        start_utc="2026-06-23T19:15:00Z",
        end_utc="2026-06-23T19:30:00Z",
    )
    assert len(layers) == 2
    # Reuses the archive fire_hotspots composite path; default thresholds are the
    # shared FIRE_* heritage values.
    assert captured["product"] == "fire_hotspots"
    assert captured["bt_c07_min_k"] == pytest.approx(FIRE_BT_C07_MIN_K)
    assert captured["bt_diff_min_k"] == pytest.approx(FIRE_BT_DIFF_MIN_K)
    for n, (layer, t) in enumerate(zip(layers, times), start=1):
        iso = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert layer.name == f"GOES Active Fire step {n} {iso} (GOES-18)"
        assert layer.layer_type == "raster"
        assert layer.role == "context"
        # Transparent RGBA hotspot preset (matches the archive hotspots band).
        assert layer.style_preset == "goes_fire_hotspots_rgba"
        assert layer.bbox == tuple(round(v, 6) for v in _UT_BBOX)
    steps = [int(re.search(r"step (\d+)", lyr.name).group(1)) for lyr in layers]
    assert steps == [1, 2]


def test_tunable_thresholds_flow_through(monkeypatch):
    times = [datetime(2026, 6, 23, 19, 20, tzinfo=timezone.utc)]
    pairs = [(t, _mk_key(t)) for t in times]
    monkeypatch.setattr(afmod, "_list_archive_keys_in_window", lambda *a, **k: list(pairs))
    seen = {}

    def _fake_read_through(metadata, params, ext, fetch_fn):
        seen["c07"] = params["bt_c07_min_k"]
        seen["diff"] = params["bt_diff_min_k"]
        return _FakeReadResult(uri="s3://fake/x.tif")

    monkeypatch.setattr(afmod, "read_through", _fake_read_through)
    fetch_goes_active_fire(
        bbox=_UT_BBOX,
        bt_c07_min_k=335.0,
        bt_diff_min_k=18.0,
        start_utc="2026-06-23T19:15:00Z",
        end_utc="2026-06-23T19:30:00Z",
    )
    assert seen == {"c07": 335.0, "diff": 18.0}


# ---- honesty floor --------------------------------------------------------


def test_no_frames_raises_typed_empty(monkeypatch):
    monkeypatch.setattr(afmod, "_list_archive_keys_in_window", lambda *a, **k: [])
    with pytest.raises(GOESArchiveEmptyError):
        fetch_goes_active_fire(
            bbox=_UT_BBOX,
            start_utc="2020-01-01T00:00:00Z",
            end_utc="2020-01-01T00:20:00Z",
        )


def test_no_hot_pixels_raises_typed_empty(monkeypatch):
    """Every frame's detector found no hot pixels -> honest typed empty (never a
    blank overlay)."""
    times = [datetime(2026, 6, 23, 19, m, tzinfo=timezone.utc) for m in (20, 25)]
    pairs = [(t, _mk_key(t)) for t in times]
    monkeypatch.setattr(afmod, "_list_archive_keys_in_window", lambda *a, **k: list(pairs))

    def _always_empty(metadata, params, ext, fetch_fn):
        raise GOESArchiveEmptyError("no hot pixels")

    monkeypatch.setattr(afmod, "read_through", _always_empty)
    with pytest.raises(GOESArchiveEmptyError):
        fetch_goes_active_fire(
            bbox=_UT_BBOX,
            start_utc="2026-06-23T19:15:00Z",
            end_utc="2026-06-23T19:30:00Z",
        )
