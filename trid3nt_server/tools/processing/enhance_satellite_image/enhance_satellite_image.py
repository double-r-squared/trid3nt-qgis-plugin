"""Atomic tool ``enhance_satellite_image`` - polish an RGB satellite COG.

Improves PRESENTATION, not data meaning. Fewer than 3 bands is not a true-color
image and raises NOT_AN_RGB_IMAGE; a 4th band carries through as alpha.
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

from trid3nt_server.emission.cog import translate_to_cog as _translate_to_cog

__all__ = [
    "enhance_satellite_image",
    "EnhanceSatelliteImageError",
    # Pure, independently-testable passes:
    "estimate_haze_floor",
    "apply_rayleigh_correction",
    "apply_white_balance",
    "apply_unsharp_mask",
    "apply_upscale",
]

logger = logging.getLogger("trid3nt_server.tools.processing.enhance_satellite_image.enhance_satellite_image")


# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class EnhanceSatelliteImageError(RuntimeError):
    """Enhancement failed or the input is not RGB imagery. ``error_code`` is one
    of IMAGE_DOWNLOAD_FAILED, NOT_AN_RGB_IMAGE, ENHANCE_FAILED, INVALID_PARAM.
    """

    retryable = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_ENHANCE_SATELLITE_IMAGE_METADATA = AtomicToolMetadata(
    name="enhance_satellite_image",
    ttl_class="static-30d",   # fully determined by its input + params; stable
    source_class="enhanced",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Pure enhancement passes, numpy and PIL only.
#
# Each takes and returns a float32 RGB array of shape (3, H, W) in [0, 255],
# except apply_upscale, which changes H and W.
# ---------------------------------------------------------------------------


def estimate_haze_floor(rgb, low_percentile: float = 1.0):
    """The per-channel path-radiance floor as ``[r, g, b]`` in [0, 255]."""
    # Over a clear scene the darkest pixels of a band should be near-zero
    # radiance, so any residual is additive atmospheric path radiance. A low
    # percentile rather than the strict minimum keeps a few black or nodata
    # pixels from zeroing the estimate.
    import numpy as np

    p = float(min(max(low_percentile, 0.0), 100.0))
    floors = np.percentile(rgb.reshape(rgb.shape[0], -1), p, axis=1)
    return floors.astype(np.float32)


def apply_rayleigh_correction(
    rgb,
    haze_strength: float = 1.0,
    blue_extra: float = 0.4,
    low_percentile: float = 1.0,
):
    """De-haze by subtracting the per-channel haze floor, scaled by
    ``haze_strength`` with an extra pull on blue, then re-stretching.
    """
    import numpy as np

    # Rayleigh scattering, the dominant clear-sky haze term, falls off as
    # ~1/lambda^4, so blue is hazed hardest and takes the extra subtraction.
    floors = estimate_haze_floor(rgb, low_percentile=low_percentile)
    strength = float(max(haze_strength, 0.0))
    per_channel = np.array(
        [strength, strength, strength * (1.0 + float(max(blue_extra, 0.0)))],
        dtype=np.float32,
    )
    sub = (floors * per_channel)[:, None, None]
    dehazed = np.clip(rgb - sub, 0.0, 255.0)

    # Subtraction drops the brightest value, so a single shared gain returns
    # highlights to ~255; a per-channel gain here would pre-empt white balance.
    gmax = float(dehazed.max())
    if gmax > 1e-6:
        dehazed = dehazed * (255.0 / gmax)
    return np.clip(dehazed, 0.0, 255.0).astype(np.float32)


def apply_white_balance(
    rgb,
    strength: float = 0.6,
    max_gain: float = 1.6,
    green_trim: float = 0.04,
):
    """Gray-world per-channel white balance at ``strength``, each gain clamped to
    ``[1/max_gain, max_gain]``, then a ``green_trim`` pull on green.
    """
    import numpy as np

    # Averaged over a large natural scene the channel means should be roughly
    # equal. The pull is partial so a legitimately coloured scene is not
    # flattened, the clamp is what stops a near-monochrome band blowing up, and
    # the green trim keeps chlorophyll from reading neon.
    means = rgb.reshape(rgb.shape[0], -1).mean(axis=1)
    target = float(means.mean())
    s = float(min(max(strength, 0.0), 1.0))
    gains = np.ones(3, dtype=np.float32)
    for c in range(3):
        if means[c] > 1e-6:
            raw_gain = target / float(means[c])
            gain = 1.0 + s * (raw_gain - 1.0)
            lo, hi = 1.0 / float(max(max_gain, 1.0)), float(max(max_gain, 1.0))
            gains[c] = float(min(max(gain, lo), hi))
    balanced = rgb * gains[:, None, None]
    trim = float(min(max(green_trim, 0.0), 0.5))
    if trim > 0.0:
        balanced[1] = balanced[1] * (1.0 - trim)
    return np.clip(balanced, 0.0, 255.0).astype(np.float32)


def _box_blur(channel, radius: int):
    """Separable box-blur of a 2-D float32 array; ``radius`` 0 returns a copy."""
    # A box blur is a dependency-free gaussian stand-in at unsharp-mask radii.
    # Cumulative-sum sliding windows keep it O(H*W), and a shrinking window at
    # the edges avoids a dark halo at the border.
    import numpy as np

    r = int(radius)
    if r <= 0:
        return channel.astype(np.float32, copy=True)

    def _blur_axis(a, axis):
        a = np.moveaxis(a, axis, -1)
        n = a.shape[-1]
        # A leading zero makes each window sum a simple difference.
        csum = np.concatenate(
            [np.zeros(a.shape[:-1] + (1,), dtype=np.float64), np.cumsum(a, axis=-1)],
            axis=-1,
        )
        idx = np.arange(n)
        lo = np.maximum(idx - r, 0)
        hi = np.minimum(idx + r + 1, n)
        window_sum = csum[..., hi] - csum[..., lo]
        counts = (hi - lo).astype(np.float64)
        out = window_sum / counts
        return np.moveaxis(out, -1, axis)

    blurred = _blur_axis(channel.astype(np.float64), 0)
    blurred = _blur_axis(blurred, 1)
    return blurred.astype(np.float32)


def apply_unsharp_mask(rgb, radius: int = 2, amount: float = 0.6):
    """Unsharp mask: ``out = img + amount * (img - blur(img, radius))``, adding the
    high-pass detail back so edges, coastlines and cloud texture read crisp.
    """
    import numpy as np

    amt = float(max(amount, 0.0))
    if amt <= 0.0 or int(radius) <= 0:
        return rgb.astype(np.float32, copy=True)
    out = np.empty_like(rgb, dtype=np.float32)
    for c in range(rgb.shape[0]):
        blur = _box_blur(rgb[c], radius)
        out[c] = rgb[c] + amt * (rgb[c] - blur)
    return np.clip(out, 0.0, 255.0).astype(np.float32)


def apply_upscale(rgb, upscale_factor: int):
    """Lanczos-resample UP by an integer factor; 1 is a passthrough. The CALLER
    scales the georeferencing transform to match the new pixel size.
    """
    import numpy as np
    from PIL import Image

    f = int(upscale_factor)
    if f <= 1:
        return rgb.astype(np.float32, copy=True)
    hwc = np.transpose(np.clip(rgb, 0.0, 255.0).astype(np.uint8), (1, 2, 0))
    img = Image.fromarray(hwc, mode="RGB")
    new_size = (img.width * f, img.height * f)
    up = img.resize(new_size, resample=Image.Resampling.LANCZOS)
    out = np.transpose(np.asarray(up, dtype=np.float32), (2, 0, 1))
    return out


# ---------------------------------------------------------------------------
# URI staging
# ---------------------------------------------------------------------------


def _stage_uri_to_local(uri: str) -> tuple[str, bool]:
    """Stage an ``s3://`` or local layer URI, returning ``(local_path, is_temp)``;
    ``is_temp`` marks a path the caller must unlink.
    """
    if uri.startswith("s3://"):
        try:
            from trid3nt_server.tools.cache import read_object_bytes_s3

            with tempfile.NamedTemporaryFile(
                suffix=".tif", delete=False, prefix="trid3nt_enhance_"
            ) as f:
                f.write(read_object_bytes_s3(uri))
                return f.name, True
        except Exception as exc:  # noqa: BLE001
            raise EnhanceSatelliteImageError(
                "IMAGE_DOWNLOAD_FAILED", f"S3 download failed for {uri!r}: {exc}"
            ) from exc

    if not os.path.isfile(uri):
        raise EnhanceSatelliteImageError(
            "IMAGE_DOWNLOAD_FAILED", f"local raster path {uri!r} does not exist"
        )
    return uri, False


# ---------------------------------------------------------------------------
# Raster read (RGB(A)) + write
# ---------------------------------------------------------------------------


def _read_rgb(path: str):
    """``(rgb, alpha_or_None, profile)``: fewer than 3 bands raises
    NOT_AN_RGB_IMAGE, and a 4th band is carried through as alpha.
    """
    import numpy as np
    import rasterio

    with rasterio.open(path) as src:
        profile = src.profile.copy()
        count = src.count
        if count < 3:
            raise EnhanceSatelliteImageError(
                "NOT_AN_RGB_IMAGE",
                f"input has {count} band(s); enhance_satellite_image polishes "
                "3(+)-band true-color RGB imagery, not single-band data rasters "
                "(DEM / index / mask). Use a true-color COG as the input.",
            )
        rgb = src.read([1, 2, 3]).astype(np.float32)
        alpha = None
        if count >= 4:
            alpha = src.read(4).astype(np.uint8)
        else:
            # The dataset mask becomes alpha, so the output never paints over
            # neighbours where the source was transparent.
            try:
                mask = src.read_masks(1)
                if mask.min() < 255:
                    alpha = mask.astype(np.uint8)
            except Exception:  # noqa: BLE001 - no mask -> fully opaque
                alpha = None
    return np.clip(rgb, 0.0, 255.0), alpha, profile


def _run_enhance(
    source_uri: str,
    rayleigh_correct: bool,
    white_balance: bool,
    sharpen: bool,
    upscale_factor: int,
    haze_strength: float,
    blue_extra: float,
    sharpen_radius: int,
    sharpen_amount: float,
    wb_strength: float,
    green_trim: float,
) -> bytes:
    """Apply the enabled passes in order - de-haze, white-balance, sharpen,
    upscale - and return RGB(A) tiled-COG bytes with overviews.
    """
    import numpy as np
    import rasterio
    from rasterio.enums import ColorInterp

    src_path: str | None = None
    is_temp = False
    flat_tmp: str | None = None
    try:
        src_path, is_temp = _stage_uri_to_local(source_uri)
        rgb, alpha, profile = _read_rgb(src_path)

        if rayleigh_correct:
            rgb = apply_rayleigh_correction(
                rgb, haze_strength=haze_strength, blue_extra=blue_extra
            )
        if white_balance:
            rgb = apply_white_balance(
                rgb, strength=wb_strength, green_trim=green_trim
            )
        if sharpen:
            rgb = apply_unsharp_mask(rgb, radius=sharpen_radius, amount=sharpen_amount)

        f = int(upscale_factor)
        out_transform = profile["transform"]
        if f > 1:
            rgb = apply_upscale(rgb, f)
            if alpha is not None:
                from PIL import Image

                a_img = Image.fromarray(alpha, mode="L").resize(
                    (alpha.shape[1] * f, alpha.shape[0] * f),
                    resample=Image.Resampling.NEAREST,
                )
                alpha = np.asarray(a_img, dtype=np.uint8)
            # The new pixel covers 1/f the ground, so the affine scales with it
            # or the COG lands in the wrong place.
            t = out_transform
            out_transform = rasterio.Affine(
                t.a / f, t.b, t.c, t.d, t.e / f, t.f
            )

        out_rgb = np.clip(rgb, 0.0, 255.0).astype(np.uint8)
        h, w = out_rgb.shape[1], out_rgb.shape[2]
        band_count = 4 if alpha is not None else 3

        out_profile = profile.copy()
        out_profile.update(
            driver="GTiff",
            dtype="uint8",
            count=band_count,
            height=h,
            width=w,
            transform=out_transform,
            nodata=None,
            photometric="RGB",
        )
        out_profile.pop("colorinterp", None)
        if alpha is not None:
            out_profile["alpha"] = "YES"

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as fh:
            flat_tmp = fh.name
        with rasterio.open(flat_tmp, "w", **out_profile) as dst:
            dst.write(out_rgb[0], 1)
            dst.write(out_rgb[1], 2)
            dst.write(out_rgb[2], 3)
            interps = [ColorInterp.red, ColorInterp.green, ColorInterp.blue]
            if alpha is not None:
                dst.write(alpha, 4)
                interps.append(ColorInterp.alpha)
            dst.colorinterp = interps

        return _translate_to_cog(flat_tmp)

    except EnhanceSatelliteImageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise EnhanceSatelliteImageError(
            "ENHANCE_FAILED",
            f"image enhancement failed for source={source_uri!r}: {exc}",
        ) from exc
    finally:
        for path, temp in ((src_path, is_temp), (flat_tmp, True)):
            if path and temp:
                try:
                    os.unlink(path)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Registered atomic tool
# ---------------------------------------------------------------------------


@register_tool(
    _ENHANCE_SATELLITE_IMAGE_METADATA,
    # Annotations: readOnlyHint=True (reads one input raster; writes only a cache
    # artifact via the read-through shim), openWorldHint=False (all computation
    # is local rasterio/numpy/PIL - no external API calls), destructiveHint=False,
    # idempotentHint=True (deterministic transform; same input+params -> same
    # pixels).
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def enhance_satellite_image(
    source_layer_uri: str,
    rayleigh_correct: bool = True,
    white_balance: bool = True,
    sharpen: bool = True,
    upscale_factor: int = 1,
    haze_strength: float = 1.0,
    blue_extra: float = 0.4,
    sharpen_radius: int = 2,
    sharpen_amount: float = 0.6,
    wb_strength: float = 0.6,
    green_trim: float = 0.04,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """OPTIONAL cosmetic polish/enhance pass for a true-color satellite RGB image.

    Use ONLY when the user asks to polish, enhance, de-haze or sharpen an RGB
    satellite COG. Improves PRESENTATION, not data meaning: de-hazes,
    white-balances, sharpens, optionally Lanczos-upscales. Any 3+ band RGB
    raster works; a single-band data raster returns NOT_AN_RGB_IMAGE. Not for
    analysis needing unmodified radiometry, nor for blending two rasters
    (``compute_blended_composite``).

    Params:
        source_layer_uri: the RGB image.
        rayleigh_correct, white_balance, sharpen: per-pass toggles, all True.
        upscale_factor: integer Lanczos upscale; 1 is off.
        haze_strength, blue_extra: de-haze floor multiplier and blue extra.
        sharpen_radius, sharpen_amount: unsharp-mask radius and strength.
        wb_strength, green_trim: gray-world pull and green trim.

    Returns the polished RGB(A) COG.
    """
    if int(upscale_factor) < 1:
        raise EnhanceSatelliteImageError(
            "INVALID_PARAM",
            f"upscale_factor must be >= 1, got {upscale_factor!r}",
        )

    params = {
        "source_layer_uri": source_layer_uri,
        "rayleigh_correct": bool(rayleigh_correct),
        "white_balance": bool(white_balance),
        "sharpen": bool(sharpen),
        "upscale_factor": int(upscale_factor),
        "haze_strength": round(float(haze_strength), 4),
        "blue_extra": round(float(blue_extra), 4),
        "sharpen_radius": int(sharpen_radius),
        "sharpen_amount": round(float(sharpen_amount), 4),
        "wb_strength": round(float(wb_strength), 4),
        "green_trim": round(float(green_trim), 4),
    }

    result = read_through(
        metadata=_ENHANCE_SATELLITE_IMAGE_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _run_enhance(
            source_uri=source_layer_uri,
            rayleigh_correct=bool(rayleigh_correct),
            white_balance=bool(white_balance),
            sharpen=bool(sharpen),
            upscale_factor=int(upscale_factor),
            haze_strength=float(haze_strength),
            blue_extra=float(blue_extra),
            sharpen_radius=int(sharpen_radius),
            sharpen_amount=float(sharpen_amount),
            wb_strength=float(wb_strength),
            green_trim=float(green_trim),
        ),
        bucket=_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, (
        "enhance_satellite_image is cacheable; uri must be set by read_through"
    )

    src_key = source_layer_uri.rstrip("/").rsplit("/", 1)[-1].replace(".tif", "")
    suffix = abs(hash((source_layer_uri, tuple(sorted(params.items()))))) % 100_000
    layer_id = f"enhanced-{src_key}-{suffix:05d}"
    name = f"Enhanced {src_key}"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="raster",
        uri=result.uri,
        style={"kind": "continuous"},
        role="context",
        units="rgb",
    )
