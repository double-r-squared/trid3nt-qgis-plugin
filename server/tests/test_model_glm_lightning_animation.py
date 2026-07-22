"""Unit tests for ``model_glm_lightning_animation`` (DIRECT GOES-19 GLM lightning composer).

Covers: registration + category + the DIRECT (no-news) contract -- crucially asserts
NO news/geocode tool is invoked -- the ordered ``step <N>`` baked-frame animation
contract, the standalone GED overlay, the honesty floor (no in-AOI lightning ->
typed empty), input validation, and the pure window/bucket helpers. The S3 boundary
(both the GLM granule fetch AND the ABI visible-base fetch) is monkeypatched so the
fetch->grid->bake->frame-assembly runs end-to-end on SYNTHETIC bytes with no network.

ASCII only.
"""

from __future__ import annotations

import asyncio
import io
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.workflows import model_glm_lightning_animation as anim
from grace2_agent.workflows.model_glm_lightning_animation import (
    DEFAULT_ACCUM_S,
    GLMAnimEmptyError,
    GLMAnimInputError,
    _frame_buckets,
    _parse_utc,
    _resolve_window,
    model_glm_lightning_animation,
)

# A small AOI for fast synthetic grids (2 deg x 2 deg @ 0.02 deg -> 100 x 100).
_UT_BBOX = (-1.0, -1.0, 1.0, 1.0)


def _run(coro):
    """Drive an async coroutine to completion (no asyncio_mode dependency)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Registration + category (the per-tool convention).
# ---------------------------------------------------------------------------
def test_run_wrapper_is_registered():
    assert "run_model_glm_lightning_animation" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["run_model_glm_lightning_animation"]
    assert entry.metadata.name == "run_model_glm_lightning_animation"
    assert entry.metadata.source_class == "workflow_dispatch"
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"


def test_categorized_under_hazard_and_weather():
    from grace2_agent.categories import PRIMARY_CATEGORY, SECONDARY_CATEGORIES

    assert (
        PRIMARY_CATEGORY.get("run_model_glm_lightning_animation") == "hazard_modeling"
    )
    assert SECONDARY_CATEGORIES.get("run_model_glm_lightning_animation") == (
        "weather_atmosphere",
    )


# ---------------------------------------------------------------------------
# Pure window / bucket helpers (no network).
# ---------------------------------------------------------------------------
def test_parse_utc_variants():
    assert _parse_utc(None) is None
    assert _parse_utc("") is None
    assert _parse_utc("2025-07-05T18:00:00Z") == datetime(
        2025, 7, 5, 18, 0, tzinfo=timezone.utc
    )
    # space separator + bare date
    assert _parse_utc("2025-07-05 18:00") == datetime(
        2025, 7, 5, 18, 0, tzinfo=timezone.utc
    )
    assert _parse_utc("2025-07-05") == datetime(2025, 7, 5, 0, 0, tzinfo=timezone.utc)


def test_parse_utc_bad_raises_input_error():
    with pytest.raises(GLMAnimInputError):
        _parse_utc("not-a-time")


def test_resolve_window_both_bounds_verbatim():
    s = datetime(2025, 7, 5, 18, 0, tzinfo=timezone.utc)
    e = datetime(2025, 7, 5, 18, 12, tzinfo=timezone.utc)
    assert _resolve_window(s, e) == (s, e)


def test_frame_buckets_one_minute_split():
    s = datetime(2025, 7, 5, 18, 0, tzinfo=timezone.utc)
    e = datetime(2025, 7, 5, 18, 3, tzinfo=timezone.utc)
    buckets = _frame_buckets(s, e, 60)
    assert len(buckets) == 3
    assert buckets[0] == (s, s + timedelta(minutes=1))
    assert buckets[-1][1] == e


def test_frame_buckets_caps_and_keeps_endpoints():
    s = datetime(2025, 7, 5, 18, 0, tzinfo=timezone.utc)
    e = s + timedelta(minutes=200)  # 200 one-min buckets
    buckets = _frame_buckets(s, e, 60, cap=10)
    assert len(buckets) <= 10
    assert buckets[0][0] == s  # first endpoint kept


# ---------------------------------------------------------------------------
# Input validation (typed, BEFORE any network call).
# ---------------------------------------------------------------------------
def test_bad_bbox_shape_raises():
    with pytest.raises(GLMAnimInputError):
        _run(model_glm_lightning_animation(bbox=(1.0, 2.0, 3.0)))


def test_degenerate_bbox_raises():
    with pytest.raises(GLMAnimInputError):
        _run(model_glm_lightning_animation(bbox=(1.0, 2.0, 1.0, 2.0)))  # zero-area


def test_unknown_satellite_raises():
    with pytest.raises(GLMAnimInputError):
        _run(model_glm_lightning_animation(bbox=_UT_BBOX, satellite="goes-99"))


def test_bad_base_band_raises():
    with pytest.raises(GLMAnimInputError):
        _run(model_glm_lightning_animation(bbox=_UT_BBOX, base_band="thermal"))


def test_start_after_end_raises():
    with pytest.raises(GLMAnimInputError):
        _run(model_glm_lightning_animation(
            bbox=_UT_BBOX,
            start_utc="2025-07-05T18:10:00Z",
            end_utc="2025-07-05T18:00:00Z",
        ))


# ---------------------------------------------------------------------------
# The DIRECT happy path (S3 boundary monkeypatched on SYNTHETIC groups + base).
# ---------------------------------------------------------------------------
def _synthetic_groups(points):
    lon = np.array([p[0] for p in points], dtype=np.float64)
    lat = np.array([p[1] for p in points], dtype=np.float64)
    eng = np.array([p[2] for p in points], dtype=np.float64)
    return lat, lon, eng


class _FakeReadResult:
    __slots__ = ("uri", "data", "hit")

    def __init__(self, uri, data=b""):
        self.uri, self.data, self.hit = uri, data, False


def _wire_synthetic(monkeypatch, *, base_band_seen=None, news_guard=None):
    """Monkeypatch BOTH S3 boundaries (GLM + ABI base) + read_through + publish so the
    composer runs fetch->grid->bake->assembly end-to-end on synthetic bytes, no network.

    ``news_guard`` (a set) records any news/geocode tool the composer would dispatch
    -- it must stay EMPTY (the DIRECT contract).
    """
    import grace2_agent.tools.cache as cachemod
    import grace2_agent.tools.fetch_glm_lightning as glmmod

    base_dt = datetime(2025, 7, 5, 18, 0, tzinfo=timezone.utc)

    # --- GLM granule list + read (3 granules/min, all groups inside _UT_BBOX). ---
    def _fake_glm_list(satellite, start_dt, end_dt):
        out = []
        t = start_dt
        i = 0
        while t < end_dt and i < 3:
            out.append((t, f"GLM-L2-LCFA/2025/186/18/k{i}.nc"))
            t = t + timedelta(seconds=20)
            i += 1
        return out

    monkeypatch.setattr(glmmod, "_list_glm_keys_in_window", _fake_glm_list)
    monkeypatch.setattr(
        glmmod,
        "_fetch_glm_groups",
        lambda *a, **k: _synthetic_groups(
            [(0.0, 0.0, 5e-14), (0.2, -0.3, 2e-13), (-0.4, 0.4, 1e-13)]
        ),
    )

    # --- ABI visible base: stub the grayscale-base reader to a synthetic gray RGB. -
    def _fake_base(satellite, bbox, when, base_band, transform, width, height):
        if base_band_seen is not None:
            base_band_seen.append(base_band)
        g = np.full((height, width), 120, dtype=np.uint8)
        return np.stack([g, g, g], axis=0)

    monkeypatch.setattr(anim, "_grayscale_visible_base", _fake_base)

    # --- read_through: run fetch_fn for REAL bytes, write nothing, return file uri. -
    captured = {"params": []}

    def _fake_read_through(metadata, params, ext, fetch_fn):
        data = fetch_fn()
        captured["params"].append(params)
        return _FakeReadResult(
            uri=f"file:///tmp/fake/{params['product']}_{params['start_utc']}.{ext}",
            data=data,
        )

    monkeypatch.setattr(cachemod, "read_through", _fake_read_through)
    monkeypatch.setattr(glmmod, "read_through", _fake_read_through)

    # --- publish_layer: no-op stub (avoid TiTiler). Records nothing dangerous. -----
    class _Entry:
        def __init__(self, fn):
            self.fn = fn

    monkeypatch.setitem(
        TOOL_REGISTRY,
        "publish_layer",
        _Entry(lambda uri, lid, preset=None: f"https://fake/{lid}"),
    )

    # --- NEWS GUARD: poison every news/geocode tool so a dispatch is a hard failure. -
    if news_guard is not None:
        for news_tool in (
            "run_model_news_event_ingest",
            "model_news_event_ingest",
            "geocode_location",
            "fetch_nifc_fire_perimeters",
        ):

            def _poison(*a, _name=news_tool, **k):
                news_guard.add(_name)
                raise AssertionError(f"DIRECT composer must NOT call {_name}")

            if news_tool in TOOL_REGISTRY:
                monkeypatch.setitem(TOOL_REGISTRY, news_tool, _Entry(_poison))

    return captured


def test_direct_run_returns_ordered_step_frames_no_news(monkeypatch):
    news_called: set[str] = set()
    base_bands: list[str] = []
    _wire_synthetic(monkeypatch, base_band_seen=base_bands, news_guard=news_called)

    res = _run(model_glm_lightning_animation(
        bbox=_UT_BBOX,
        start_utc="2025-07-05T18:00:00Z",
        end_utc="2025-07-05T18:03:00Z",  # 3 one-min frames
        satellite="goes-19",
        accumulation_window_s=60,
        storm_name="UT Gulf TC",
    ))
    assert res["status"] == "ok"
    # 3 baked frames + 3 standalone overlay frames.
    assert res["n_frames"] == 3
    assert res["n_overlay_frames"] == 3
    assert res["satellite"] == "goes-19"
    assert res["base_band"] == "visible"

    # THE DIRECT CONTRACT: no news/geocode tool was invoked.
    assert news_called == set(), f"composer called a news/geocode tool: {news_called}"

    # Ordered step <N> baked-frame contract (the web SequenceScrubber grouping).
    baked = [
        L for L in res["layers"] if L["style_preset"] == "glm_lightning_baked"
    ]
    assert len(baked) == 3
    for n, L in enumerate(baked, start=1):
        assert f"step {n}" in L["name"]
        assert "UT Gulf TC" in L["name"]
        assert L["layer_type"] == "raster"
    assert len({L["layer_id"] for L in baked}) == 3  # distinct frames

    # per-frame GED stats are real (binned synthetic groups).
    assert len(res["frame_stats"]) == 3
    assert all(s["n_groups_in_aoi"] == 3 for s in res["frame_stats"])
    assert all(s["n_lit_cells"] >= 1 for s in res["frame_stats"])
    assert res["peak_fj"] > 0


class _RecordingEmitter:
    """Minimal PipelineEmitter stand-in that records the per-frame emissions.

    NATE 2026-06-26: records add_loaded_layer so we can lock in that each
    published lightning frame is emitted into session-state loaded_layers (the
    step the composer omitted -> frames published to TiTiler but never rendered).
    """

    def __init__(self):
        self.map_commands: list[tuple[str, dict]] = []
        self.loaded_layers: list = []

    async def add_step(self, name, tool_name):
        return "step-1"

    async def mark_running(self, step_id):
        return None

    async def mark_complete(self, step_id):
        return None

    async def mark_failed(self, step_id, code, msg):
        return None

    async def emit_map_command(self, command, args):
        self.map_commands.append((command, args))

    async def add_loaded_layer(self, layer):
        self.loaded_layers.append(layer)


def test_confirm_emits_each_published_frame_as_a_loaded_layer(monkeypatch):
    """NATE 2026-06-26: the render-blocker fix, re-pinned on the NEW s3 publish
    shape (TiTiler exit / QGIS-native swap). Each baked + overlay frame whose
    publish returned a renderable uri -- now the raw s3:// COG the plugin reads
    via /vsicurl/ -- must be EMITTED into session-state loaded_layers via
    add_loaded_layer. The emitted LayerURI carries the PUBLISHED s3:// uri.
    """
    _wire_synthetic(monkeypatch)

    class _Entry:
        def __init__(self, fn):
            self.fn = fn

    # publish_layer returns the NEW raster success shape: the raw s3:// COG.
    monkeypatch.setitem(
        TOOL_REGISTRY,
        "publish_layer",
        _Entry(lambda uri, lid, preset=None: f"s3://fake-pub/{lid}.tif"),
    )
    emitter = _RecordingEmitter()

    res = _run(model_glm_lightning_animation(
        bbox=_UT_BBOX,
        start_utc="2025-07-05T18:00:00Z",
        end_utc="2025-07-05T18:03:00Z",  # 3 one-min frames
        satellite="goes-19",
        accumulation_window_s=60,
        storm_name="UT Gulf TC",
        pipeline_emitter=emitter,  # type: ignore[arg-type]
    ))
    assert res["status"] == "ok"
    # 3 baked frames + 3 standalone overlay frames = 6 published renderable layers.
    n_published = res["n_frames"] + res["n_overlay_frames"]
    assert n_published == 6
    # EVERY published frame is emitted into loaded_layers (the fix).
    assert len(emitter.loaded_layers) == n_published
    # Each emitted frame carries the PUBLISHED s3:// COG uri (the new shape).
    for lyr in emitter.loaded_layers:
        assert lyr.uri == f"s3://fake-pub/{lyr.layer_id}.tif"
    # Distinct frames (no dedup collapse): one emit per published layer_id.
    emitted_ids = [lyr.layer_id for lyr in emitter.loaded_layers]
    assert len(set(emitted_ids)) == len(emitted_ids)


def test_confirm_emits_http_published_frames_too(monkeypatch):
    """The http(s) publish face still emits -- widening the frame gate to s3://
    must not regress http tile URLs (the _wire_synthetic default stub)."""
    _wire_synthetic(monkeypatch)  # publish stub returns https://fake/<layer_id>
    emitter = _RecordingEmitter()

    res = _run(model_glm_lightning_animation(
        bbox=_UT_BBOX,
        start_utc="2025-07-05T18:00:00Z",
        end_utc="2025-07-05T18:02:00Z",  # 2 frames
        accumulation_window_s=60,
        overlay_standalone_ged=False,
        pipeline_emitter=emitter,  # type: ignore[arg-type]
    ))
    assert res["status"] == "ok"
    assert len(emitter.loaded_layers) == res["n_frames"] == 2
    for lyr in emitter.loaded_layers:
        assert lyr.uri == f"https://fake/{lyr.layer_id}"


def test_publish_failure_frame_is_skipped_not_emitted_raw(monkeypatch):
    """HONESTY FLOOR: a frame whose publish returns a NON-renderable value
    (neither http(s) nor s3:// -- publish failed) must NOT be emitted."""
    _wire_synthetic(monkeypatch)

    class _Entry:
        def __init__(self, fn):
            self.fn = fn

    # publish_layer returns a non-renderable sentinel -> every frame fails the
    # gate -> nothing is emitted (but the run still succeeds + returns frames).
    monkeypatch.setitem(
        TOOL_REGISTRY,
        "publish_layer",
        _Entry(lambda uri, lid, preset=None: "PUBLISH_FAILED_NOT_A_URL"),
    )
    emitter = _RecordingEmitter()

    res = _run(model_glm_lightning_animation(
        bbox=_UT_BBOX,
        start_utc="2025-07-05T18:00:00Z",
        end_utc="2025-07-05T18:02:00Z",  # 2 frames
        accumulation_window_s=60,
        overlay_standalone_ged=False,
        pipeline_emitter=emitter,  # type: ignore[arg-type]
    ))
    assert res["status"] == "ok"
    assert res["n_frames"] == 2
    # Non-renderable publish -> honest skip -> NO frames emitted.
    assert emitter.loaded_layers == []


def test_baked_frame_is_real_rgb_cog(monkeypatch):
    captured = _wire_synthetic(monkeypatch)
    _run(model_glm_lightning_animation(
        bbox=_UT_BBOX,
        start_utc="2025-07-05T18:00:00Z",
        end_utc="2025-07-05T18:01:00Z",
        accumulation_window_s=60,
        overlay_standalone_ged=False,
    ))
    import rasterio

    baked = [c for c in captured["params"] if c["product"] == "glm_baked"]
    assert len(baked) == 1
    # the bytes fetch_fn produced is a valid 3-band RGB EPSG:4326 COG.
    # (re-run the fetch through the recorded params is unnecessary; the data was
    #  validated by read_through running fetch_fn -- assert via a fresh bake.)
    from grace2_agent.workflows.model_glm_lightning_animation import (
        _bake_glm_frame_cog_bytes,
    )

    data, stats = _bake_glm_frame_cog_bytes(
        "goes-19",
        _UT_BBOX,
        datetime(2025, 7, 5, 18, 0, tzinfo=timezone.utc),
        datetime(2025, 7, 5, 18, 1, tzinfo=timezone.utc),
        "visible",
    )
    with rasterio.open(io.BytesIO(data)) as ds:
        assert ds.count == 3
        assert ds.dtypes[0] == "uint8"
        assert ds.crs.to_epsg() == 4326
    assert stats["n_groups_in_aoi"] == 3
    assert stats["peak_fj"] > 0
    assert len(stats["granule_keys"]) == 3


def test_ir_base_band_threads_through(monkeypatch):
    base_bands: list[str] = []
    _wire_synthetic(monkeypatch, base_band_seen=base_bands)
    res = _run(model_glm_lightning_animation(
        bbox=_UT_BBOX,
        start_utc="2025-07-05T18:00:00Z",
        end_utc="2025-07-05T18:01:00Z",
        accumulation_window_s=60,
        base_band="ir",
        overlay_standalone_ged=False,
    ))
    assert res["base_band"] == "ir"
    assert base_bands and all(b == "ir" for b in base_bands)


# ---------------------------------------------------------------------------
# Honesty floor: no in-AOI lightning -> typed empty (NEVER a blank animation).
# ---------------------------------------------------------------------------
def test_no_lightning_anywhere_raises_typed_empty(monkeypatch):
    import grace2_agent.tools.fetch_glm_lightning as glmmod

    base_dt = datetime(2025, 7, 5, 18, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        glmmod,
        "_list_glm_keys_in_window",
        lambda sat, s, e: [(base_dt, "GLM-L2-LCFA/2025/186/18/k.nc")],
    )
    # all groups OUTSIDE _UT_BBOX -> every bucket is empty.
    monkeypatch.setattr(
        glmmod,
        "_fetch_glm_groups",
        lambda *a, **k: _synthetic_groups([(50.0, 50.0, 1e-13)]),
    )

    class _Entry:
        def __init__(self, fn):
            self.fn = fn

    monkeypatch.setitem(
        TOOL_REGISTRY, "publish_layer", _Entry(lambda *a, **k: "x")
    )
    with pytest.raises(GLMAnimEmptyError) as ei:
        _run(model_glm_lightning_animation(
            bbox=_UT_BBOX,
            start_utc="2025-07-05T18:00:00Z",
            end_utc="2025-07-05T18:02:00Z",
            accumulation_window_s=60,
        ))
    assert ei.value.error_code == "GLM_ANIM_EMPTY"
    assert ei.value.retryable is False


def test_empty_buckets_skipped_not_emitted_blank(monkeypatch):
    """A bucket with no lightning is skipped; the run proceeds with the rest."""
    _wire_synthetic(monkeypatch)
    import grace2_agent.tools.fetch_glm_lightning as glmmod

    real_list = glmmod._list_glm_keys_in_window
    calls = {"n": 0}
    base_dt = datetime(2025, 7, 5, 18, 0, tzinfo=timezone.utc)

    def _maybe_empty_list(satellite, start_dt, end_dt):
        calls["n"] += 1
        if calls["n"] == 2:  # the 2nd bucket has NO granules
            return []
        return real_list(satellite, start_dt, end_dt)

    monkeypatch.setattr(glmmod, "_list_glm_keys_in_window", _maybe_empty_list)
    res = _run(model_glm_lightning_animation(
        bbox=_UT_BBOX,
        start_utc="2025-07-05T18:00:00Z",
        end_utc="2025-07-05T18:03:00Z",  # 3 buckets
        accumulation_window_s=60,
        overlay_standalone_ged=False,
    ))
    assert res["n_frames"] == 2  # the empty middle bucket was skipped
    assert res["n_empty_buckets"] == 1


# ---------------------------------------------------------------------------
# Defaults: GOES-19 East + 60 s accumulation are the demo defaults.
# ---------------------------------------------------------------------------
def test_demo_defaults():
    assert anim.DEFAULT_SATELLITE == "goes-19"
    assert DEFAULT_ACCUM_S == 60
