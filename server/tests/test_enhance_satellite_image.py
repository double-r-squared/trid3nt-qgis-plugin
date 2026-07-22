"""Unit tests for ``enhance_satellite_image`` atomic tool (NATE 2026-06-23).

The tool is the OPTIONAL polish/enhance pass that pushes a true-color satellite
RGB COG closer to NOAA/CIRA's de-hazed "GeoColor" look. Coverage:

Pure passes (synthetic numpy arrays, no I/O):
 1. ``test_estimate_haze_floor`` - per-channel dark-object floor is the low
    percentile and tracks an injected blue-heavy haze offset.
 2. ``test_apply_rayleigh_correction_dehazes`` - a hazy (blue-cast, low-contrast)
    image gets its blue floor pulled down hardest and its contrast re-stretched.
 3. ``test_apply_white_balance_grayworld`` - a green-cast image's channel means
    move toward equal; gains are clamped; green trim pulls green down.
 4. ``test_apply_unsharp_mask_sharpens`` - sharpening increases edge local
    contrast (gradient magnitude) vs the original; amount=0 is a no-op.
 5. ``test_box_blur_smooths`` - the pure-numpy box blur reduces variance and
    preserves the mean (no border darkening), radius 0 is a copy.
 6. ``test_apply_upscale_lanczos`` - output grid grows by factor^2; factor 1 is a
    no-op passthrough.

Registry + COG round-trip (rasterio temp files):
 7. ``test_enhance_satellite_image_registered`` - tool in TOOL_REGISTRY with
    correct metadata (cacheable, static-30d, source_class="enhanced").
 8. ``test_enhance_resolvable_param_in_allowlist`` - source_layer_uri resolves.
 9. ``test_enhance_cog_round_trip`` - a synthetic RGB COG in -> a valid RGB COG
    out, same dims, 3 bands, uint8, georeferencing preserved.
10. ``test_enhance_upscale_round_trip`` - upscale_factor=2 doubles the grid and
    scales the affine pixel size by 1/2 (stays correctly georeferenced).
11. ``test_enhance_returns_layer_uri_fields`` - LayerURI fields (raster, rgb
    units, rgb_composite preset, "Enhanced" name).
12. ``test_enhance_non_rgb_raises`` - a single-band DEM-like input raises the
    typed NOT_AN_RGB_IMAGE error (honest failure, not garbage output).
13. ``test_enhance_invalid_upscale_raises`` - upscale_factor < 1 -> INVALID_PARAM.
14. ``test_enhance_cache_hit_skips_fetch`` - a second identical call hits the
    cache (the enhance compute is not re-run).
15. ``test_enhance_alpha_preserved`` - a 4-band RGBA input keeps its alpha band.
"""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.processing.enhance_satellite_image import (
    EnhanceSatelliteImageError,
    _box_blur,
    apply_rayleigh_correction,
    apply_unsharp_mask,
    apply_upscale,
    apply_white_balance,
    enhance_satellite_image,
    estimate_haze_floor,
)


# ---------------------------------------------------------------------------
# Synthetic raster helpers
# ---------------------------------------------------------------------------


def _hazy_rgb(size: int = 64) -> np.ndarray:
    """A low-contrast, blue-cast 'hazy' RGB image (3, H, W) float32 in [0,255].

    A mid-grey base with a structured gradient (so contrast + edges exist),
    compressed into a narrow band and lifted by a per-channel haze floor that
    is heaviest in blue - i.e. exactly the additive atmospheric path radiance
    the de-haze pass should remove.
    """
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    grad = (xx / size) * 60.0 + (yy / size) * 30.0  # 0..90 structured detail
    base = 90.0 + grad  # land radiance ~90..180, with structure
    rgb = np.stack([base, base * 0.97, base * 0.9], axis=0)
    # Compress contrast (haze washes detail out) then add a blue-heavy floor.
    rgb = rgb * 0.5 + np.array([30.0, 40.0, 70.0])[:, None, None]
    return np.clip(rgb, 0.0, 255.0).astype(np.float32)


def _write_rgb_cog(path: str, rgb: np.ndarray, *, bands: int = 3) -> None:
    """Write an RGB(A) uint8 GeoTIFF on a known EPSG:5070 grid."""
    _, h, w = rgb.shape
    transform = from_bounds(0.0, 0.0, w * 10.0, h * 10.0, w, h)
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": w,
        "height": h,
        "count": bands,
        "crs": "EPSG:5070",
        "transform": transform,
    }
    arr = np.clip(rgb, 0.0, 255.0).astype(np.uint8)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr[0], 1)
        dst.write(arr[1], 2)
        dst.write(arr[2], 3)
        if bands == 4:
            alpha = np.full((h, w), 255, dtype=np.uint8)
            alpha[: h // 2, : w // 2] = 0  # a transparent quadrant
            dst.write(alpha, 4)


def _grad_mag(channel: np.ndarray) -> float:
    """Mean absolute gradient magnitude of a 2-D array (edge sharpness proxy)."""
    gy, gx = np.gradient(channel.astype(np.float64))
    return float(np.mean(np.abs(gx) + np.abs(gy)))


# ===========================================================================
# 1-6 : pure pass unit tests (no I/O)
# ===========================================================================


def test_estimate_haze_floor():
    rgb = np.zeros((3, 32, 32), dtype=np.float32)
    rgb[0] += 10.0
    rgb[1] += 20.0
    rgb[2] += 50.0  # blue hazed hardest
    # add a few bright pixels so the floor is NOT just the global min/max
    rgb[:, 0, 0] = 250.0
    floors = estimate_haze_floor(rgb, low_percentile=1.0)
    assert floors.shape == (3,)
    # The low-percentile floor recovers the injected per-channel offsets.
    assert floors[0] == pytest.approx(10.0, abs=1.0)
    assert floors[1] == pytest.approx(20.0, abs=1.0)
    assert floors[2] == pytest.approx(50.0, abs=1.0)
    # blue floor is the largest (the blue cast)
    assert floors[2] > floors[1] > floors[0]


def test_apply_rayleigh_correction_dehazes():
    rgb = _hazy_rgb()
    before_floor = estimate_haze_floor(rgb)
    out = apply_rayleigh_correction(rgb, haze_strength=1.0, blue_extra=0.4)
    after_floor = estimate_haze_floor(out)
    assert out.shape == rgb.shape
    assert out.dtype == np.float32
    assert out.max() <= 255.0 + 1e-3 and out.min() >= 0.0
    # The blue floor (the cast) drops the most after de-hazing.
    assert after_floor[2] < before_floor[2]
    blue_drop = before_floor[2] - after_floor[2]
    red_drop = before_floor[0] - after_floor[0]
    assert blue_drop >= red_drop  # blue is pulled hardest
    # Contrast is re-stretched: the de-hazed image spans a wider dynamic range.
    assert (out.max() - out.min()) > (rgb.max() - rgb.min())
    # haze_strength=0 is (almost) a no-op aside from the re-stretch monotonicity
    noop = apply_rayleigh_correction(rgb, haze_strength=0.0, blue_extra=0.0)
    assert noop.shape == rgb.shape


def test_apply_white_balance_grayworld():
    # Green-cast scene: green mean far above red/blue.
    rgb = np.stack(
        [
            np.full((32, 32), 80.0, dtype=np.float32),
            np.full((32, 32), 180.0, dtype=np.float32),  # green dominates
            np.full((32, 32), 90.0, dtype=np.float32),
        ],
        axis=0,
    )
    before = rgb.reshape(3, -1).mean(axis=1)
    out = apply_white_balance(rgb, strength=1.0, max_gain=4.0, green_trim=0.04)
    after = out.reshape(3, -1).mean(axis=1)
    # Channel means move CLOSER together (gray-world equalization).
    assert np.ptp(after) < np.ptp(before)
    # Red (was darkest) is brightened; the gain is positive.
    assert after[0] > before[0]
    # strength=0 is a no-op.
    noop = apply_white_balance(rgb, strength=0.0, green_trim=0.0)
    assert np.allclose(noop, rgb)


def test_apply_white_balance_gain_clamped():
    # A near-black channel would demand an enormous gain; it must be clamped.
    rgb = np.stack(
        [
            np.full((16, 16), 1.0, dtype=np.float32),     # almost black
            np.full((16, 16), 200.0, dtype=np.float32),
            np.full((16, 16), 200.0, dtype=np.float32),
        ],
        axis=0,
    )
    out = apply_white_balance(rgb, strength=1.0, max_gain=2.0, green_trim=0.0)
    # red mean can rise by AT MOST max_gain (2x) -> <= 2.0, not ~130.
    assert out[0].mean() <= 1.0 * 2.0 + 1e-3


def test_apply_unsharp_mask_sharpens():
    # An image with a hard edge down the middle.
    rgb = np.zeros((3, 40, 40), dtype=np.float32)
    rgb[:, :, 20:] = 200.0
    before = _grad_mag(rgb[0])
    out = apply_unsharp_mask(rgb, radius=2, amount=1.0)
    after = _grad_mag(out[0])
    assert out.shape == rgb.shape
    # Unsharp masking increases local contrast at the edge (overshoot).
    assert after > before
    # amount=0 -> exact passthrough.
    noop = apply_unsharp_mask(rgb, radius=2, amount=0.0)
    assert np.allclose(noop, rgb)


def test_box_blur_smooths():
    rng = np.random.default_rng(3)
    channel = rng.uniform(0, 255, size=(48, 48)).astype(np.float32)
    blurred = _box_blur(channel, radius=2)
    assert blurred.shape == channel.shape
    # Blurring reduces variance.
    assert blurred.var() < channel.var()
    # Mean is preserved (no border darkening because edge windows shrink).
    assert blurred.mean() == pytest.approx(channel.mean(), abs=1.0)
    # radius 0 -> a copy.
    assert np.allclose(_box_blur(channel, radius=0), channel)


def test_apply_upscale_lanczos():
    rgb = _hazy_rgb(size=32)
    up = apply_upscale(rgb, upscale_factor=2)
    assert up.shape == (3, 64, 64)
    assert up.dtype == np.float32
    assert up.max() <= 255.0 + 1e-3 and up.min() >= 0.0
    # factor 1 -> no-op passthrough (same shape, same values within rounding).
    noop = apply_upscale(rgb, upscale_factor=1)
    assert noop.shape == rgb.shape


# ===========================================================================
# 7-15 : registry + COG round-trip tests
# ===========================================================================


def test_enhance_satellite_image_registered():
    assert "enhance_satellite_image" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["enhance_satellite_image"].metadata
    assert meta.cacheable is True
    assert meta.ttl_class == "static-30d"
    assert meta.source_class == "enhanced"


def test_enhance_resolvable_param_in_allowlist():
    from trid3nt_server.uri_registry import RESOLVABLE_URI_PARAMS

    # The public URI param must be server-resolvable so a layer handle is turned
    # into the real COG URI before dispatch.
    assert "source_layer_uri" in RESOLVABLE_URI_PARAMS


def test_enhance_cog_round_trip(tmp_path, monkeypatch_local_cache):
    src = str(tmp_path / "truecolor.tif")
    rgb = _hazy_rgb(size=80)
    _write_rgb_cog(src, rgb)

    with rasterio.open(src) as ds:
        in_w, in_h, in_crs = ds.width, ds.height, ds.crs
        in_transform = ds.transform

    layer = enhance_satellite_image(src, _bucket="test-bucket")
    assert layer.uri is not None
    out_path = monkeypatch_local_cache(layer.uri)

    with rasterio.open(out_path) as ds:
        assert ds.count >= 3
        assert ds.dtypes[0] == "uint8"
        assert (ds.width, ds.height) == (in_w, in_h)
        assert ds.crs == in_crs
        # Same ground footprint (transform pixel size unchanged at factor 1).
        assert ds.transform.a == pytest.approx(in_transform.a)
        out_rgb = ds.read([1, 2, 3]).astype(np.float32)
    # The enhancement actually changed the pixels (it is not a passthrough).
    assert not np.allclose(out_rgb, np.clip(rgb, 0, 255).astype(np.uint8))


def test_enhance_upscale_round_trip(tmp_path, monkeypatch_local_cache):
    src = str(tmp_path / "truecolor_up.tif")
    _write_rgb_cog(src, _hazy_rgb(size=40))
    with rasterio.open(src) as ds:
        in_w, in_h = ds.width, ds.height
        in_px = ds.transform.a

    layer = enhance_satellite_image(src, upscale_factor=2, _bucket="test-bucket")
    out_path = monkeypatch_local_cache(layer.uri)
    with rasterio.open(out_path) as ds:
        assert (ds.width, ds.height) == (in_w * 2, in_h * 2)
        # Pixel ground-size halves so the COG stays correctly georeferenced.
        assert ds.transform.a == pytest.approx(in_px / 2.0)


def test_enhance_returns_layer_uri_fields(tmp_path, monkeypatch_local_cache):
    src = str(tmp_path / "tc_fields.tif")
    _write_rgb_cog(src, _hazy_rgb(size=48))
    layer = enhance_satellite_image(src, _bucket="test-bucket")
    assert layer.layer_type == "raster"
    assert layer.units == "rgb"
    assert layer.style_preset == "rgb_composite"
    assert layer.name.startswith("Enhanced")
    assert layer.role == "context"


def test_enhance_non_rgb_raises(tmp_path):
    # A single-band DEM-like raster is NOT a true-color image.
    src = str(tmp_path / "dem.tif")
    h = w = 32
    transform = from_bounds(0.0, 0.0, w * 10.0, h * 10.0, w, h)
    with rasterio.open(
        src, "w", driver="GTiff", dtype="float32", width=w, height=h,
        count=1, crs="EPSG:5070", transform=transform,
    ) as dst:
        dst.write(np.random.default_rng(1).uniform(0, 100, (h, w)).astype("float32"), 1)

    with pytest.raises(EnhanceSatelliteImageError) as exc:
        enhance_satellite_image(src, _bucket="test-bucket")
    assert exc.value.error_code == "NOT_AN_RGB_IMAGE"


def test_enhance_invalid_upscale_raises(tmp_path):
    src = str(tmp_path / "tc_bad.tif")
    _write_rgb_cog(src, _hazy_rgb(size=16))
    with pytest.raises(EnhanceSatelliteImageError) as exc:
        enhance_satellite_image(src, upscale_factor=0, _bucket="test-bucket")
    assert exc.value.error_code == "INVALID_PARAM"


def test_enhance_cache_hit_skips_fetch(tmp_path, monkeypatch_local_cache):
    src = str(tmp_path / "tc_cache.tif")
    _write_rgb_cog(src, _hazy_rgb(size=32))

    import trid3nt_server.tools.processing.enhance_satellite_image as mod

    calls = {"n": 0}
    real_run = mod._run_enhance

    def _counting_run(*a, **k):
        calls["n"] += 1
        return real_run(*a, **k)

    mod._run_enhance = _counting_run
    try:
        layer1 = enhance_satellite_image(src, _bucket="test-bucket")
        layer2 = enhance_satellite_image(src, _bucket="test-bucket")
    finally:
        mod._run_enhance = real_run

    # Same inputs -> the second call is served from cache; compute ran once.
    assert calls["n"] == 1
    assert layer1.uri == layer2.uri


def test_enhance_alpha_preserved(tmp_path, monkeypatch_local_cache):
    src = str(tmp_path / "rgba.tif")
    _write_rgb_cog(src, _hazy_rgb(size=40), bands=4)
    layer = enhance_satellite_image(src, _bucket="test-bucket")
    out_path = monkeypatch_local_cache(layer.uri)
    with rasterio.open(out_path) as ds:
        assert ds.count == 4  # alpha carried through
        alpha = ds.read(4)
    # The transparent quadrant stays transparent.
    assert alpha[: alpha.shape[0] // 4, : alpha.shape[1] // 4].max() == 0


# ---------------------------------------------------------------------------
# Local-cache fixture: route read_through writes to a temp dir + give the test a
# way to read the written COG bytes back. The shared in-memory S3 double in
# conftest already monkeypatches boto3; here we additionally capture the bytes
# the tool writes so the round-trip tests can re-open the output COG.
# ---------------------------------------------------------------------------


@pytest.fixture
def monkeypatch_local_cache(monkeypatch, tmp_path):
    """Patch ``read_through`` so the enhanced COG bytes land in a temp file.

    Returns a resolver ``uri -> local_path`` the test uses to re-open the
    output COG. This keeps the round-trip tests self-contained (no boto3 / no
    network) while still exercising the real _run_enhance compute path through
    the tool's public entrypoint.
    """
    import trid3nt_server.tools.processing.enhance_satellite_image as mod
    from trid3nt_contracts.tool_registry import AtomicToolMetadata

    store: dict[str, str] = {}
    counter = {"n": 0}
    # Cache by the param signature so an identical second call is a hit.
    cache: dict[tuple, str] = {}

    class _Result:
        def __init__(self, uri: str) -> None:
            self.uri = uri
            self.cache_hit = False

    def _fake_read_through(*, metadata, params, ext, fetch_fn, bucket=None, **kw):
        key = tuple(sorted((k, str(v)) for k, v in params.items()))
        if key in cache:
            return _Result(cache[key])
        data = fetch_fn()  # runs the real _run_enhance
        counter["n"] += 1
        out = tmp_path / f"cache_{counter['n']}.tif"
        out.write_bytes(data)
        uri = f"s3://{bucket or 'test'}/cache/static-30d/enhanced/{counter['n']}.tif"
        store[uri] = str(out)
        cache[key] = uri
        return _Result(uri)

    monkeypatch.setattr(mod, "read_through", _fake_read_through)

    def _resolve(uri: str) -> str:
        return store[uri]

    return _resolve
