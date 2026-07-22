"""Unit tests for ``fetch_goes_animation`` (fire-animation demo S3).

Coverage:
- Registration + metadata.
- ``_band_to_slider_product`` maps geocolor / fire_temperature -> SLIDER slugs.
- ``_parse_utc`` parses ISO-8601 (Z / +00:00 / space / bare date).
- ``_select_frame_indices`` keeps endpoints + even-subsamples over the cap.
- ``_build_frame_list`` windows the SLIDER time index + orders ascending +
  caps -- the frame-list assembly with real UTC.
- bbox-required + unknown band/satellite raise typed errors.
- The SLIDER timestamp helpers round-trip a real UTC valid-time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools._satellite_slider import (
    FIRE_BLEND_RED_FLOOR,
    SliderEmptyError,
    blend_geocolor_fire_temperature,
    rgb_array_to_cog_bytes,
    rgb_cog_bytes_to_array,
    ts_int_to_datetime,
    ts_int_to_iso,
)
from trid3nt_server.tools.fetch_goes_satellite import GOESInputError
from trid3nt_server.tools.fetch_goes_animation import (
    GOESAnimBboxRequiredError,
    GOESAnimEmptyError,
    GOESAnimInputError,
    GOES_BLEND_PRODUCT,
    _band_to_slider_product,
    _build_frame_list,
    _parse_utc,
    _select_frame_indices,
    fetch_goes_animation,
    fetch_goes_blend_animation,
)

_UT_BBOX = (-113.346, 39.57, -111.765, 41.115)


# ---- registration ---------------------------------------------------------


def test_tool_is_registered():
    assert "fetch_goes_animation" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_goes_animation"]
    assert entry.metadata.name == "fetch_goes_animation"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "goes_animation"
    assert entry.metadata.cacheable is True


# ---- product slug mapping -------------------------------------------------


def test_band_to_slider_product_confirmed_slugs():
    # CONFIRMED slugs from define-products.js.
    assert _band_to_slider_product("geocolor") == "geocolor"
    assert _band_to_slider_product("fire_temperature") == "fire_temperature"


def test_band_to_slider_product_unknown_raises():
    with pytest.raises(GOESAnimInputError):
        _band_to_slider_product("ultraviolet")


# ---- _parse_utc -----------------------------------------------------------


def test_parse_utc_forms():
    assert _parse_utc("2026-06-22T13:30:00Z") == datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    assert _parse_utc("2026-06-22T13:30:00+00:00") == datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    assert _parse_utc("2026-06-22 13:30:00") == datetime(2026, 6, 22, 13, 30, tzinfo=timezone.utc)
    assert _parse_utc("2026-06-22") == datetime(2026, 6, 22, 0, 0, tzinfo=timezone.utc)


def test_parse_utc_rejects_garbage():
    with pytest.raises(GOESAnimInputError):
        _parse_utc("not-a-date")


# ---- frame-list assembly --------------------------------------------------


def test_select_frame_indices_keeps_all_under_cap():
    assert _select_frame_indices(5, cap=10) == [0, 1, 2, 3, 4]


def test_select_frame_indices_subsamples_keeping_endpoints():
    kept = _select_frame_indices(100, cap=10)
    assert kept[0] == 0
    assert kept[-1] == 99
    assert len(kept) <= 10
    # strictly increasing
    assert all(kept[i] < kept[i + 1] for i in range(len(kept) - 1))


def _ts(y, mo, d, h, mi):
    return int(f"{y:04d}{mo:02d}{d:02d}{h:02d}{mi:02d}00")


def test_build_frame_list_windows_and_orders():
    # 5-min GOES cadence across a date; reverse-chron input is sorted by the
    # SLIDER reader, but _build_frame_list also sorts the windowed slice.
    all_ts = [_ts(2026, 6, 22, 13, m) for m in (0, 5, 10, 15, 20, 25, 30)]
    start = datetime(2026, 6, 22, 13, 5, tzinfo=timezone.utc)
    end = datetime(2026, 6, 22, 13, 20, tzinfo=timezone.utc)
    frames = _build_frame_list(all_ts, start, end)
    # Only 13:05..13:20 inclusive (5,10,15,20).
    assert frames == [
        _ts(2026, 6, 22, 13, 5),
        _ts(2026, 6, 22, 13, 10),
        _ts(2026, 6, 22, 13, 15),
        _ts(2026, 6, 22, 13, 20),
    ]
    # Ascending.
    assert frames == sorted(frames)


def test_build_frame_list_caps_and_keeps_endpoints():
    # 300 valid 5-min timestamps spanning ~25 h from 2026-06-22T00:00Z.
    base = datetime(2026, 6, 22, 0, 0, tzinfo=timezone.utc)
    all_ts = []
    for i in range(300):
        dt = base + timedelta(minutes=5 * i)
        all_ts.append(int(dt.strftime("%Y%m%d%H%M%S")))
    start = ts_int_to_datetime(all_ts[0])
    end = ts_int_to_datetime(all_ts[-1])
    frames = _build_frame_list(all_ts, start, end, cap=20)
    assert len(frames) <= 20
    assert frames[0] == all_ts[0]
    assert frames[-1] == all_ts[-1]


def test_build_frame_list_empty_window():
    all_ts = [_ts(2026, 6, 22, 13, 0)]
    start = datetime(2026, 6, 23, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 23, 1, 0, tzinfo=timezone.utc)
    assert _build_frame_list(all_ts, start, end) == []


def test_frame_labels_carry_real_utc():
    ts = _ts(2026, 6, 22, 19, 26)
    assert ts_int_to_iso(ts) == "2026-06-22T19:26:00Z"
    assert ts_int_to_datetime(ts).tzinfo == timezone.utc


# ---- emitted name token: "GOES <ProductLabel> step <N> <ISO> (<SAT>)" -----
#
# The scrubber-group contract: each frame name carries (a) a "step <N>" MONOTONIC
# token (the web detectSequentialGroups grouping value -- a raw ISO is NOT a
# recognized token), (b) the product label so GeoColor / Fire Temperature form
# TWO distinct stems (two scrubber groups), and (c) the real UTC valid-time as
# the per-frame display label. ``<N>`` is the position in the shared windowed
# frame list, so the same step maps to the same SLIDER timestamp across both GOES
# products -> the two scrubbers stay time-synchronized.


class _FakeReadResult:
    def __init__(self, uri):
        self.uri = uri


class _FakeReadResultWithData:
    """ReadThroughResult stand-in that also carries the fetched bytes (.data),
    which _single_product_frame_bytes returns for the blend."""

    def __init__(self, uri, data):
        self.uri = uri
        self.data = data


def _patch_slider_for_three_frames(monkeypatch, product_seen):
    """Stub the SLIDER substrate so fetch_goes_animation emits 3 deterministic frames."""
    from trid3nt_server.tools import fetch_goes_animation as mod

    frame_ts = [
        _ts(2026, 6, 22, 18, 0),
        _ts(2026, 6, 22, 18, 5),
        _ts(2026, 6, 22, 18, 10),
    ]

    def _fake_timestamps(satellite, sector, product):
        product_seen.append(product)
        return list(frame_ts)

    def _fake_read_through(metadata, params, ext, fetch_fn):
        # Never actually fetch tiles; return a deterministic per-frame URI.
        return _FakeReadResult(uri=f"s3://fake/{params['product']}-{params['ts_int']}.tif")

    monkeypatch.setattr(mod, "fetch_slider_timestamps", _fake_timestamps)
    monkeypatch.setattr(mod, "read_through", _fake_read_through)
    monkeypatch.setattr(mod, "pick_zoom_for_aoi", lambda *a, **k: 5)
    return frame_ts


def test_emitted_name_carries_step_token_and_iso(monkeypatch):
    seen: list[str] = []
    frame_ts = _patch_slider_for_three_frames(monkeypatch, seen)
    layers = fetch_goes_animation(
        bbox=_UT_BBOX,
        band="fire_temperature",
        satellite="goes-18",
        start_utc="2026-06-22T17:30:00Z",
        end_utc="2026-06-22T18:30:00Z",
    )
    assert len(layers) == len(frame_ts) == 3
    # Each name: "GOES Fire Temperature step <N> <ISO> (GOES-18)".
    for n, (layer, ts) in enumerate(zip(layers, frame_ts), start=1):
        iso = ts_int_to_iso(ts)
        assert layer.name == f"GOES Fire Temperature step {n} {iso} (GOES-18)"
    # Monotonic, distinct step values 1..3 in order.
    import re

    steps = [int(re.search(r"step (\d+)", lyr.name).group(1)) for lyr in layers]
    assert steps == [1, 2, 3]
    # Shared style preset (scrubber-group contract).
    assert {lyr.style_preset for lyr in layers} == {"goes_rgb_animation"}


def test_two_goes_products_are_time_synchronized_by_step(monkeypatch):
    """GeoColor + Fire Temperature over the SAME window share the same step->ISO
    mapping, so step <N> picks the SAME valid-time in both -> synchronized."""
    seen: list[str] = []
    frame_ts = _patch_slider_for_three_frames(monkeypatch, seen)

    def _names_for(band):
        layers = fetch_goes_animation(
            bbox=_UT_BBOX,
            band=band,
            satellite="goes-18",
            start_utc="2026-06-22T17:30:00Z",
            end_utc="2026-06-22T18:30:00Z",
        )
        # step value -> ISO valid-time it points at, per product.
        out = {}
        import re

        for lyr in layers:
            step = int(re.search(r"step (\d+)", lyr.name).group(1))
            iso = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", lyr.name).group(1)
            out[step] = iso
        return out

    geocolor = _names_for("geocolor")
    fire = _names_for("fire_temperature")
    # Same step keys.
    assert set(geocolor) == set(fire) == {1, 2, 3}
    # CO-TEMPORAL: step N is the SAME valid-time in both products.
    for step in (1, 2, 3):
        assert geocolor[step] == fire[step]
    # And the two products produce DISTINCT product labels (-> two stems/groups).
    # (verified at the name level above; here assert the labels differ)
    assert geocolor[1] == fire[1]  # same time
    # The product token differs in the stem (GeoColor vs Fire Temperature):
    g_layers = fetch_goes_animation(
        bbox=_UT_BBOX, band="geocolor", satellite="goes-18",
        start_utc="2026-06-22T17:30:00Z", end_utc="2026-06-22T18:30:00Z",
    )
    f_layers = fetch_goes_animation(
        bbox=_UT_BBOX, band="fire_temperature", satellite="goes-18",
        start_utc="2026-06-22T17:30:00Z", end_utc="2026-06-22T18:30:00Z",
    )
    assert "GeoColor" in g_layers[0].name
    assert "Fire Temperature" in f_layers[0].name


# ---- shared satellite normalizer migration (goes18/GOES West accepted) -----
#
# The fetcher now routes ``satellite`` through the shared ``_normalize_satellite``
# seam BEFORE the allow-list, so the "goes18 vs goes-18" spelling zoo is accepted
# (GOES-18 / goes18 / "GOES West" all resolve to the canonical goes-18 bird) and
# the canonical token flows into every SLIDER path / cache key / LayerURI label.
# A truly-unknown bird (GOES-99) still fails LOUD; a valid GOES bird this tool
# does not serve (goes-16/goes-17) still raises the tool's OWN typed error.


@pytest.mark.parametrize("spelling", ["GOES-18", "goes18", "GOES West", "GOES_18", "G18", "18"])
def test_satellite_spellings_accepted_and_canonicalized(monkeypatch, spelling):
    """Forgiving GOES-West spellings all normalize to goes-18 and proceed; the
    emitted LayerURI label carries the canonical GOES-18 token (not the raw input)."""
    seen: list[str] = []
    frame_ts = _patch_slider_for_three_frames(monkeypatch, seen)
    layers = fetch_goes_animation(
        bbox=_UT_BBOX,
        band="geocolor",
        satellite=spelling,
        start_utc="2026-06-22T17:30:00Z",
        end_utc="2026-06-22T18:30:00Z",
    )
    assert len(layers) == len(frame_ts) == 3
    # Every frame label carries the canonical, upper-cased GOES-18 token -- the
    # raw spelling was normalized to goes-18 before the name was built.
    for layer in layers:
        assert layer.name.endswith("(GOES-18)")


@pytest.mark.parametrize("spelling", ["GOES-18", "goes18", "GOES West"])
def test_blend_fetcher_satellite_spellings_accepted(monkeypatch, spelling):
    """The blend fetcher also accepts the spelling zoo and canonicalizes to goes-18."""
    calls: list[int] = []
    frame_ts = _patch_blend_slider(monkeypatch, calls)
    layers = fetch_goes_blend_animation(
        bbox=_UT_BBOX,
        satellite=spelling,
        start_utc="2026-06-22T17:30:00Z",
        end_utc="2026-06-22T18:30:00Z",
    )
    assert len(layers) == len(frame_ts) == 3
    for layer in layers:
        assert layer.name.endswith("(GOES-18)")


def test_genuinely_unknown_bird_raises_loud():
    """A genuinely-unknown bird (GOES-99) fails LOUD via the shared normalizer's
    typed error -- never a silent bad SLIDER path."""
    with pytest.raises(GOESInputError):
        fetch_goes_animation(bbox=_UT_BBOX, satellite="GOES-99")
    with pytest.raises(GOESInputError):
        fetch_goes_blend_animation(bbox=_UT_BBOX, satellite="GOES-99")


def test_valid_but_unsupported_bird_raises_tool_own_error():
    """A REAL GOES bird this tool does not serve (goes-16 -- only goes-18/goes-19
    are in GOES_ANIM_SATELLITES) normalizes fine, then raises the tool's OWN typed
    error (not the base normalizer error leaking through)."""
    with pytest.raises(GOESAnimInputError):
        fetch_goes_animation(bbox=_UT_BBOX, satellite="goes-16")
    with pytest.raises(GOESAnimInputError):
        fetch_goes_blend_animation(bbox=_UT_BBOX, satellite="goes-16")


# ---- typed-error surface --------------------------------------------------


def test_bbox_none_raises_bbox_required():
    with pytest.raises(GOESAnimBboxRequiredError):
        fetch_goes_animation(bbox=None)  # type: ignore[arg-type]


def test_unknown_band_raises():
    with pytest.raises(GOESAnimInputError):
        fetch_goes_animation(bbox=_UT_BBOX, band="xyz")


def test_unknown_satellite_raises():
    # A non-GOES bird is truly unknown to the shared normalizer -> the loud base
    # typed error (GOESInputError) fires before the tool's own allow-list.
    with pytest.raises(GOESInputError):
        fetch_goes_animation(bbox=_UT_BBOX, satellite="himawari-9")


def test_degenerate_bbox_raises():
    with pytest.raises(GOESAnimInputError):
        fetch_goes_animation(bbox=(-112.0, 39.0, -112.0, 39.0))


# ---- GeoColor + Fire Temperature per-timestep BLEND (NATE 2026-06-22) -------
#
# NATE folds the two co-temporal GOES products into ONE composite frame: GeoColor
# is the true-color base and the Fire Temperature active-fire glow is overlaid
# ONLY where the SWIR signature is hot. The blend (a) is a valid 3-band RGB COG
# co-registered to the inputs, (b) changes pixels where Fire Temperature is hot,
# and (c) leaves non-fire pixels as the GeoColor base.


def _rgb_cog(arr, bbox):
    """Write a (3,H,W) uint8 array to RGB COG bytes over ``bbox`` (EPSG:4326)."""
    from rasterio.transform import from_bounds

    h, w = arr.shape[1], arr.shape[2]
    tr = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], w, h)
    return rgb_array_to_cog_bytes(arr, tr, w, h)


def test_blend_is_valid_3band_rgb_cog_coregistered_to_inputs():
    """The blended frame is a valid 3-band RGB COG on the SAME grid/transform as
    the GeoColor base (co-registration preserved)."""
    import numpy as np
    from rasterio.transform import from_bounds

    bbox = (-112.0, 39.0, -111.9, 39.08)
    h, w = 8, 10
    base_tr = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], w, h)

    geo = np.zeros((3, h, w), dtype=np.uint8)
    geo[0], geo[1], geo[2] = 60, 120, 80  # uniform true-color scene
    fire = np.zeros((3, h, w), dtype=np.uint8)
    fire[2] = 40  # cool blue-ish non-fire background

    out_bytes = blend_geocolor_fire_temperature(_rgb_cog(geo, bbox), _rgb_cog(fire, bbox))
    out, out_tr, out_w, out_h = rgb_cog_bytes_to_array(out_bytes)
    # 3-band uint8, same shape as the GeoColor input.
    assert out.shape == (3, h, w)
    assert out.dtype == np.uint8
    # Co-registered: identical affine transform + size as the base.
    assert (out_w, out_h) == (w, h)
    assert np.allclose(np.asarray(out_tr)[:6], np.asarray(base_tr)[:6], atol=1e-9)


def test_blend_overlay_changes_fire_pixels_keeps_base_elsewhere():
    """The fire-mask overlay actually changes pixels where Fire Temperature is hot
    (bright red SWIR) and leaves non-fire pixels as the GeoColor base."""
    import numpy as np

    bbox = (-112.0, 39.0, -111.9, 39.08)
    h, w = 8, 10
    geo = np.zeros((3, h, w), dtype=np.uint8)
    geo[0], geo[1], geo[2] = 60, 120, 80
    fire = np.zeros((3, h, w), dtype=np.uint8)
    fire[2] = 40
    # A hot fire core (high red, red >> blue) well above the mask floor.
    fire[0, 3:5, 4:6] = 240
    fire[1, 3:5, 4:6] = 90
    fire[2, 3:5, 4:6] = 30
    assert 240 >= FIRE_BLEND_RED_FLOOR  # the core is hot by the mask threshold

    out, _, _, _ = rgb_cog_bytes_to_array(
        blend_geocolor_fire_temperature(_rgb_cog(geo, bbox), _rgb_cog(fire, bbox))
    )
    # Fire-core pixel CHANGED from the GeoColor base (the glow was composited in).
    assert tuple(int(v) for v in out[:, 3, 4]) != (60, 120, 80)
    # Its red channel rose toward the fire color (active-fire glow).
    assert int(out[0, 3, 4]) > 60
    # A non-fire pixel (cool SWIR) is UNTOUCHED -- still the GeoColor base.
    assert tuple(int(v) for v in out[:, 0, 0]) == (60, 120, 80)


def test_blend_co_registers_when_input_grids_differ():
    """If the Fire Temperature frame is on a different grid, the blend reprojects
    it onto the GeoColor grid so the per-pixel composite stays valid."""
    import numpy as np
    from rasterio.transform import from_bounds

    bbox = (-112.0, 39.0, -111.9, 39.08)
    geo = np.full((3, 8, 10), 50, dtype=np.uint8)
    geo[1] = 100
    fire = np.zeros((3, 16, 20), dtype=np.uint8)  # finer grid, same bbox
    fire[2] = 40
    fire[0, 6:10, 8:12] = 240  # hot core in the finer grid

    geo_b = rgb_array_to_cog_bytes(geo, from_bounds(*bbox, 10, 8), 10, 8)
    fire_b = rgb_array_to_cog_bytes(fire, from_bounds(*bbox, 20, 16), 20, 16)
    out, _, out_w, out_h = rgb_cog_bytes_to_array(
        blend_geocolor_fire_temperature(geo_b, fire_b)
    )
    # Output is aligned to the GeoColor (base) grid, not the Fire Temperature one.
    assert (out_w, out_h) == (10, 8)
    assert out.shape == (3, 8, 10)
    assert out.any()


def test_blend_empty_inputs_raise_empty():
    """Both inputs all-zero -> the blended frame is empty (honesty floor)."""
    import numpy as np

    bbox = (-112.0, 39.0, -111.9, 39.08)
    zero = np.zeros((3, 8, 10), dtype=np.uint8)
    # rgb_array_to_cog_bytes does not gate emptiness (the fetcher does), so an
    # all-zero pair blends to all-zero and the blend raises SliderEmptyError.
    with pytest.raises(SliderEmptyError):
        blend_geocolor_fire_temperature(_rgb_cog(zero, bbox), _rgb_cog(zero, bbox))


# ---- fetch_goes_blend_animation: ONE blended scrubber group ----------------


def test_blend_fetcher_is_registered():
    assert "fetch_goes_blend_animation" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_goes_blend_animation"]
    assert entry.metadata.name == "fetch_goes_blend_animation"
    assert entry.metadata.source_class == "goes_animation"
    assert entry.metadata.cacheable is True


def _patch_blend_slider(monkeypatch, blend_calls, *, empty=False):
    """Stub the SLIDER substrate so fetch_goes_blend_animation emits 3 blended
    frames deterministically (no network, no real raster)."""
    from trid3nt_server.tools import fetch_goes_animation as mod

    frame_ts = [
        _ts(2026, 6, 22, 18, 0),
        _ts(2026, 6, 22, 18, 5),
        _ts(2026, 6, 22, 18, 10),
    ]

    def _fake_timestamps(satellite, sector, product):
        return list(frame_ts)

    def _fake_read_through(metadata, params, ext, fetch_fn):
        # The blend fetcher caches the COMPOSITE under the synthetic blend slug;
        # invoke fetch_fn so the underlying _blend_frame_cog_bytes path is exercised.
        assert params["product"] == GOES_BLEND_PRODUCT
        blend_calls.append(params["ts_int"])
        if empty:
            raise SliderEmptyError("empty crop")
        fetch_fn()  # exercise the blend assembly (its read/blend are stubbed below)
        return _FakeReadResult(uri=f"s3://fake/blend-{params['ts_int']}.tif")

    def _fake_blend_bytes(sat, sector, ts_int, zoom, bbox):
        return b"BLENDED_COG_BYTES"

    monkeypatch.setattr(mod, "fetch_slider_timestamps", _fake_timestamps)
    monkeypatch.setattr(mod, "read_through", _fake_read_through)
    monkeypatch.setattr(mod, "pick_zoom_for_aoi", lambda *a, **k: 5)
    monkeypatch.setattr(mod, "_blend_frame_cog_bytes", _fake_blend_bytes)
    return frame_ts


def test_blend_fetcher_emits_one_group_with_step_and_iso(monkeypatch):
    """fetch_goes_blend_animation emits ONE scrubber group: a single goes-fire-
    blend layer_id prefix + single product-label name + step token + ISO."""
    calls: list[int] = []
    frame_ts = _patch_blend_slider(monkeypatch, calls)
    layers = fetch_goes_blend_animation(
        bbox=_UT_BBOX,
        satellite="goes-18",
        start_utc="2026-06-22T17:30:00Z",
        end_utc="2026-06-22T18:30:00Z",
    )
    assert len(layers) == len(frame_ts) == 3
    # ONE group: single layer_id prefix + single product label across all frames.
    assert all(lyr.layer_id.startswith("goes-fire-blend-") for lyr in layers)
    for n, (layer, ts) in enumerate(zip(layers, frame_ts), start=1):
        iso = ts_int_to_iso(ts)
        assert layer.name == (
            f"GOES Fire (GeoColor + Fire Temperature) step {n} {iso} (GOES-18)"
        )
    # Monotonic step values 1..3.
    import re

    steps = [int(re.search(r"step (\d+)", lyr.name).group(1)) for lyr in layers]
    assert steps == [1, 2, 3]
    # Shared style preset (scrubber-group contract) + the blend assembly ran once
    # per frame.
    assert {lyr.style_preset for lyr in layers} == {"goes_rgb_animation"}
    assert calls == frame_ts


def test_blend_fetcher_honesty_floor_all_empty(monkeypatch):
    """Every blended frame empty -> the blend fetcher honesty-floors (typed empty)."""
    calls: list[int] = []
    _patch_blend_slider(monkeypatch, calls, empty=True)
    with pytest.raises(GOESAnimEmptyError):
        fetch_goes_blend_animation(
            bbox=_UT_BBOX,
            satellite="goes-18",
            start_utc="2026-06-22T17:30:00Z",
            end_utc="2026-06-22T18:30:00Z",
        )


def test_blend_frame_cog_bytes_composites_both_products(monkeypatch):
    """_blend_frame_cog_bytes fetches BOTH co-temporal products (cache-mediated)
    and returns a real blended RGB COG -- the true dual-product wiring, with only
    the SLIDER stitch + the S3 read_through stubbed."""
    import numpy as np
    from trid3nt_server.tools import fetch_goes_animation as mod

    bbox = (-112.0, 39.0, -111.9, 39.08)
    products_fetched: list[str] = []

    def _fake_stitch(sat, sector, product, ts_int, zoom, b, **k):
        products_fetched.append(product)
        # GeoColor = green scene; Fire Temperature = cool with a hot red core.
        if product == "geocolor":
            rgb = np.zeros((8, 10, 3), dtype=np.uint8)
            rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2] = 60, 120, 80
        else:  # fire_temperature
            rgb = np.zeros((8, 10, 3), dtype=np.uint8)
            rgb[:, :, 2] = 40
            rgb[3:5, 4:6, 0] = 240
        return rgb, bbox  # mosaic extent == aoi bbox here

    def _fake_read_through(metadata, params, ext, fetch_fn):
        # Single-product reads call fetch_fn() to materialize the COG bytes.
        data = fetch_fn()
        return _FakeReadResultWithData(uri=f"s3://fake/{params['product']}.tif", data=data)

    monkeypatch.setattr(mod, "stitch_slider_mosaic", _fake_stitch)
    monkeypatch.setattr(mod, "read_through", _fake_read_through)

    blended = mod._blend_frame_cog_bytes("goes-18", "conus", _ts(2026, 6, 22, 18, 0), 5, bbox)
    # BOTH products were fetched (base + fire), in order.
    assert products_fetched == ["geocolor", "fire_temperature"]
    # The result is a valid blended 3-band RGB COG with the fire glow composited.
    out, _, _, _ = rgb_cog_bytes_to_array(blended)
    assert out.shape == (3, 8, 10)
    assert tuple(int(v) for v in out[:, 3, 4]) != (60, 120, 80)  # fire pixel changed
    assert tuple(int(v) for v in out[:, 0, 0]) == (60, 120, 80)  # base elsewhere


def test_blend_fetcher_bbox_required():
    with pytest.raises(GOESAnimBboxRequiredError):
        fetch_goes_blend_animation(bbox=None)  # type: ignore[arg-type]


def test_blend_fetcher_unknown_satellite_raises():
    # A non-GOES bird is truly unknown to the shared normalizer -> the loud base
    # typed error (GOESInputError) fires before the tool's own allow-list.
    with pytest.raises(GOESInputError):
        fetch_goes_blend_animation(bbox=_UT_BBOX, satellite="himawari-9")


# ---------------------------------------------------------------------------
# Consolidation: fetch_goes_animation(band="blend") folds in the blend tool.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["blend", "blended", "combined", "GeoColor_Fire"])
def test_band_blend_delegates_to_blend_impl(monkeypatch, token):
    """A blend band token routes fetch_goes_animation to the blended impl."""
    from trid3nt_server.tools import fetch_goes_animation as mod

    sentinel = ["BLENDED"]
    seen = {}

    def _fake_impl(bbox, **kwargs):
        seen["bbox"] = bbox
        seen["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(mod, "_blend_animation_impl", _fake_impl)
    got = fetch_goes_animation(
        bbox=_UT_BBOX,
        band=token,
        satellite="goes-18",
        sector="conus",
        start_utc="2026-06-22T17:30:00Z",
        end_utc="2026-06-22T18:30:00Z",
    )
    assert got is sentinel
    assert seen["bbox"] == _UT_BBOX
    assert seen["kwargs"]["satellite"] == "goes-18"
    assert seen["kwargs"]["sector"] == "conus"


def test_deprecated_blend_alias_routes_through_impl(monkeypatch):
    """The deprecated fetch_goes_blend_animation still routes through the impl."""
    from trid3nt_server.tools import fetch_goes_animation as mod

    sentinel = ["BLENDED"]
    monkeypatch.setattr(mod, "_blend_animation_impl", lambda bbox, **k: sentinel)
    got = fetch_goes_blend_animation(
        bbox=_UT_BBOX,
        satellite="goes-18",
        start_utc="2026-06-22T17:30:00Z",
        end_utc="2026-06-22T18:30:00Z",
    )
    assert got is sentinel


def test_non_blend_band_does_not_delegate(monkeypatch):
    """geocolor / fire_temperature never hit the blend impl."""
    from trid3nt_server.tools import fetch_goes_animation as mod

    seen = _patch_slider_for_three_frames(monkeypatch, [])

    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("blend impl should not run for band=geocolor")

    monkeypatch.setattr(mod, "_blend_animation_impl", _boom)
    layers = fetch_goes_animation(
        bbox=_UT_BBOX,
        band="geocolor",
        satellite="goes-18",
        start_utc="2026-06-22T17:30:00Z",
        end_utc="2026-06-22T18:30:00Z",
    )
    assert len(layers) == 3
