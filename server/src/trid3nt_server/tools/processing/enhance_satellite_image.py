"""Atomic tool ``enhance_satellite_image`` - polish an RGB satellite COG (CIRA-GeoColor-style).

This module registers ONE optional, composable polish/enhance atomic tool:

    ``enhance_satellite_image(source_layer_uri, rayleigh_correct=True,
        sharpen=True, white_balance=True, upscale_factor=1, ...) -> LayerURI``

WHY THIS EXISTS
---------------
A raw satellite true-color composite (e.g. a GOES/ABI true-color RGB, a Sentinel
or NAIP true-color COG) reads HAZY and BLUE-CAST compared with NOAA/CIRA's
GeoColor product. CIRA's land "pops" because the imagery has been de-hazed
(atmospheric Rayleigh / path-radiance removed), white-balanced, and lightly
sharpened. This tool applies that same family of defensible, documented,
INDIVIDUALLY-TOGGLEABLE post-processing passes to ANY RGB image COG so the agent
can offer "polish / enhance this image" on demand. It is NOT fire-specific and
NOT GOES-specific - it operates on the pixels of any 3(+)-band RGB raster.

It is the imagery sibling of ``compute_blended_composite``: read an RGB COG with
rasterio, run a pure-numpy/PIL transform, write a tiled RGB COG (with overviews
when ``gdal_translate`` is available), route through the ``read_through`` cache
shim, and return a ``LayerURI`` that ``publish_layer`` renders verbatim via the
existing multiband RGB passthrough (NO new style preset required).

THE FOUR PASSES (each toggleable, each a pure ``__all__`` helper)
-----------------------------------------------------------------
1. Rayleigh / haze correction (``rayleigh_correct``) - the single biggest gap
   vs CIRA's de-hazed look. We estimate a per-channel atmospheric path-radiance
   floor (the classic "dark-object subtraction": the dark-object haze value is
   the low-percentile radiance of each band, which over clear scenes is
   dominated by Rayleigh scattering - strongest in BLUE, weaker in green, weakest
   in red). We subtract that floor (scaled by ``haze_strength``, heavier on blue)
   and re-stretch so the darkest land returns to near-black. Result: the blue
   cast lifts, contrast returns, land saturates -> cleaner, more "GeoColor".

2. White-balance / green refinement (``white_balance``) - a mild per-channel gray-
   world gain so vegetation/terrain read natural (a gentle pull toward equal
   channel means, capped so we never over-correct a legitimately colored scene),
   plus an optional small green-trim so chlorophyll greens don't go neon.

3. Unsharp-mask sharpening (``sharpen``) - light local-contrast boost:
   ``out = img + amount * (img - gaussian_blur(img, radius))``. Radius + amount
   are tunable; the blur is a separable box-blur approximation (pure numpy, no
   scipy/skimage dependency) so edges/coastlines/roads read crisp like GeoColor.

4. Optional upscale (``upscale_factor``) - Lanczos resample (via PIL) by an
   integer factor for a higher-resolution PRESENTATION of a coarse satellite
   image. Pixel count grows by factor^2; the georeferencing transform is scaled
   so the COG stays correctly placed.

Operations are applied in the order 1 -> 2 -> 3 -> 4 (de-haze, then balance,
then sharpen the balanced image, then upscale the finished frame). Each pass is
a free function in ``__all__`` so it is unit-testable on synthetic arrays in
isolation, and any pass can be turned off independently.

HONEST TYPED ERROR
------------------
A non-RGB input (a single-band DEM, a 2-band raster, a paletted index COG with
no usable RGB) raises ``EnhanceSatelliteImageError(error_code="NOT_AN_RGB_IMAGE")``
rather than silently producing garbage - this tool polishes true-color RGB
imagery, not data rasters. (A 4-band RGBA image is accepted: bands 1-3 are RGB,
band 4 is carried through as alpha.)

Cross-cutting invariants:
- Invariant 2 (Deterministic workflows): preserves - zero LLM calls; the output
  is fully determined by the input pixels + the toggles/params.
- FR-DC-6 (cacheable): honors - ``cacheable=True``, ``ttl_class="static-30d"``,
  ``source_class="enhanced"``; the polished COG is a pure function of its inputs.
- NFR-R-1 (resilience): preserves - every failure surfaces as a typed
  ``EnhanceSatelliteImageError`` with a SCREAMING_SNAKE_CASE ``error_code``.
- No-sync-on-the-loop: the tool is dispatched through the agent's standard
  to_thread offload path like every other rasterio/numpy compute tool.
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

# Reuse the hillshade COG writer (tiled + overviews) verbatim so the enhanced
# image renders fast over WMS/TiTiler exactly like every other derived COG, and
# its env-var-overridable gdal binary resolver. Both fall back gracefully when
# gdal_translate is unavailable (flat GTiff bytes), so the tool never hard-fails
# on a box without gdal-bin.
from trid3nt_server.tools.processing.compute_hillshade import _translate_to_cog, _get_gdaldem_bin

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

logger = logging.getLogger("trid3nt_server.tools.processing.enhance_satellite_image")


# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class EnhanceSatelliteImageError(RuntimeError):
    """Raised when image enhancement fails or the input is not RGB imagery.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the pipeline
    strip (NFR-R-1 typed-error requirement). ``retryable`` follows the FR-AS-11
    convention so ``summarize_tool_result`` renders the envelope and the LLM can
    decide retry/clarify/fallback.

    Codes:
    - ``IMAGE_DOWNLOAD_FAILED`` - the source layer URI could not be staged.
    - ``NOT_AN_RGB_IMAGE`` - the input has fewer than 3 bands (a DEM / index /
      single-band data raster), so there is no true-color image to polish.
    - ``ENHANCE_FAILED`` - the numpy/PIL/rasterio enhancement step failed.
    - ``INVALID_PARAM`` - an out-of-range parameter (e.g. upscale_factor < 1).
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
# Pure enhancement passes (numpy + PIL only - no scipy/skimage dependency).
#
# Each works on a float32 RGB array of shape (3, H, W) in [0, 255] and returns a
# float32 (3, H, W) array in [0, 255], EXCEPT apply_upscale which changes H/W.
# They are independently testable and individually toggleable.
# ---------------------------------------------------------------------------


def estimate_haze_floor(rgb, low_percentile: float = 1.0):
    """Estimate the per-channel atmospheric path-radiance ("haze") floor.

    Dark-object subtraction (DOS): over a clear scene the darkest pixels of each
    band should be near-zero radiance; any residual is dominated by additive
    atmospheric path radiance (Rayleigh scattering, strongest in BLUE). The
    low-percentile value of each band is therefore a robust estimate of that
    haze floor. We use a percentile (default the 1st) rather than the strict
    minimum so a few black/nodata pixels do not zero out the estimate.

    Args:
        rgb: (3, H, W) float32 RGB in [0, 255].
        low_percentile: percentile (0-100) used as the dark-object floor.

    Returns:
        A length-3 float32 array ``[r_floor, g_floor, b_floor]`` in [0, 255].
    """
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
    """De-haze an RGB image via per-channel dark-object (path-radiance) subtraction.

    Subtracts the estimated per-channel haze floor (``estimate_haze_floor``),
    scaled by ``haze_strength``, with an EXTRA pull on the blue channel
    (``blue_extra``) because Rayleigh scattering - the dominant clear-sky haze
    term - falls off as ~1/lambda^4 and so is strongest in blue. After
    subtraction the result is re-stretched back to the full [0, 255] range so
    the de-hazed land regains contrast rather than just getting darker. This is
    the single biggest visual gap vs CIRA's de-hazed GeoColor look.

    Args:
        rgb: (3, H, W) float32 RGB in [0, 255].
        haze_strength: 0.0 (no de-haze) .. ~1.5; global multiplier on the floor.
        blue_extra: extra fraction of the blue floor to subtract on top of
            ``haze_strength`` (blue is hazed hardest). 0.4 = +40% on blue.
        low_percentile: dark-object percentile handed to ``estimate_haze_floor``.

    Returns:
        (3, H, W) float32 array in [0, 255].
    """
    import numpy as np

    floors = estimate_haze_floor(rgb, low_percentile=low_percentile)
    strength = float(max(haze_strength, 0.0))
    per_channel = np.array(
        [strength, strength, strength * (1.0 + float(max(blue_extra, 0.0)))],
        dtype=np.float32,
    )
    sub = (floors * per_channel)[:, None, None]
    dehazed = np.clip(rgb - sub, 0.0, 255.0)

    # Re-stretch: the brightest channel value usually drops after subtraction;
    # rescale by the global max so highlights return to ~255 and contrast is
    # restored (a single shared gain keeps the white-balance neutral here - the
    # per-channel balance is the white_balance pass's job).
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
    """Gray-world per-channel white balance + a gentle green trim.

    Gray-world assumption: averaged over a large natural scene the channel means
    should be roughly equal. We compute the gain that would equalize each channel
    to the overall mean, then apply it at ``strength`` (a partial pull so a
    legitimately colored scene is not flattened) and CLAMP each gain to
    ``[1/max_gain, max_gain]`` so a near-monochrome band cannot blow up. A small
    ``green_trim`` then pulls green down slightly so chlorophyll/vegetation greens
    read natural rather than neon (the classic over-green satellite look).

    Args:
        rgb: (3, H, W) float32 RGB in [0, 255].
        strength: 0.0 (no balance) .. 1.0 (full gray-world equalization).
        max_gain: clamp on any single channel gain (and 1/max_gain as the floor).
        green_trim: fraction to trim the green channel after balancing (0..~0.1).

    Returns:
        (3, H, W) float32 array in [0, 255].
    """
    import numpy as np

    means = rgb.reshape(rgb.shape[0], -1).mean(axis=1)
    target = float(means.mean())
    s = float(min(max(strength, 0.0), 1.0))
    gains = np.ones(3, dtype=np.float32)
    for c in range(3):
        if means[c] > 1e-6:
            raw_gain = target / float(means[c])
            # partial pull toward the gray-world gain
            gain = 1.0 + s * (raw_gain - 1.0)
            lo, hi = 1.0 / float(max(max_gain, 1.0)), float(max(max_gain, 1.0))
            gains[c] = float(min(max(gain, lo), hi))
    balanced = rgb * gains[:, None, None]
    trim = float(min(max(green_trim, 0.0), 0.5))
    if trim > 0.0:
        balanced[1] = balanced[1] * (1.0 - trim)
    return np.clip(balanced, 0.0, 255.0).astype(np.float32)


def _box_blur(channel, radius: int):
    """Separable box-blur of a 2-D float32 array (pure-numpy gaussian stand-in).

    A small box blur applied is a cheap, dependency-free approximation of a
    gaussian for unsharp masking (one pass is enough at unsharp-mask radii).
    Uses cumulative-sum sliding windows along each axis so it is O(H*W) and
    needs neither scipy nor skimage. Edges use a shrinking window (partial
    averaging) so there is no dark halo at the border.

    Args:
        channel: (H, W) float32.
        radius: box half-width in pixels (>=1). radius 0 returns a copy.

    Returns:
        (H, W) float32 blurred array.
    """
    import numpy as np

    r = int(radius)
    if r <= 0:
        return channel.astype(np.float32, copy=True)

    def _blur_axis(a, axis):
        a = np.moveaxis(a, axis, -1)
        n = a.shape[-1]
        # Cumulative sum with a leading zero so window sums are a simple diff.
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
    """Unsharp-mask sharpening: boost local contrast around edges.

    ``out = img + amount * (img - blur(img, radius))``. The high-pass detail
    (image minus its blur) is added back scaled by ``amount`` so edges,
    coastlines, roads, and cloud texture read crisp - the light sharpening CIRA
    applies. Uses the pure-numpy ``_box_blur`` so there is no scipy/skimage dep.

    Args:
        rgb: (3, H, W) float32 RGB in [0, 255].
        radius: blur radius in pixels (>=1); larger = coarser sharpening.
        amount: 0.0 (no sharpening) .. ~1.5; strength of the detail add-back.

    Returns:
        (3, H, W) float32 array in [0, 255].
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
    """Lanczos-resample an RGB array UP by an integer factor (PIL).

    Grows the pixel grid by ``upscale_factor`` in each dimension using Lanczos
    resampling (high-quality, sharp) for a higher-resolution PRESENTATION of a
    coarse satellite image. ``upscale_factor=1`` is a no-op passthrough. The
    CALLER is responsible for scaling the georeferencing transform to match the
    new pixel size (see ``_run_enhance``).

    Args:
        rgb: (3, H, W) float32 RGB in [0, 255].
        upscale_factor: integer >= 1.

    Returns:
        (3, H*f, W*f) float32 array in [0, 255].
    """
    import numpy as np
    from PIL import Image

    f = int(upscale_factor)
    if f <= 1:
        return rgb.astype(np.float32, copy=True)
    # (3, H, W) float -> (H, W, 3) uint8 for PIL, resize, back to (3, H, W).
    hwc = np.transpose(np.clip(rgb, 0.0, 255.0).astype(np.uint8), (1, 2, 0))
    img = Image.fromarray(hwc, mode="RGB")
    new_size = (img.width * f, img.height * f)
    up = img.resize(new_size, resample=Image.Resampling.LANCZOS)
    out = np.transpose(np.asarray(up, dtype=np.float32), (2, 0, 1))
    return out


# ---------------------------------------------------------------------------
# URI staging (mirrors compute_blended_composite._stage_uri_to_local)
# ---------------------------------------------------------------------------


def _stage_uri_to_local(uri: str) -> tuple[str, bool]:
    """Stage a layer URI to a local file path.

    Returns ``(local_path, is_temp)`` where ``is_temp`` marks paths the caller
    must clean up. Handles ``s3://`` (via the shared boto3 reader) and local
    paths exactly like the sibling compute_* tools so handle-resolved COGs work.
    GCP is decommissioned, so only S3/local are supported.

    Raises ``EnhanceSatelliteImageError(IMAGE_DOWNLOAD_FAILED, ...)`` on failure.
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
    """Read the source as (3, H, W) float32 RGB + optional alpha + profile.

    Enforces the RGB contract: a raster with fewer than 3 bands is NOT a true-
    color image and raises ``EnhanceSatelliteImageError(NOT_AN_RGB_IMAGE)``. A
    4-band input is treated as RGBA - bands 1-3 are RGB, band 4 is carried
    through as alpha so transparent regions stay transparent in the output.

    Returns ``(rgb, alpha_or_None, profile)``.
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
            # Carry the dataset/nodata mask through as alpha so we never paint
            # over neighbours where the source was transparent.
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
    """Run the enhancement passes and return tiled-COG-with-overviews bytes.

    Cache-miss path: stages the URI, reads RGB(A), applies the enabled passes in
    order (de-haze -> white-balance -> sharpen -> upscale), writes an RGB(A)
    uint8 COG. Raises EnhanceSatelliteImageError on any failure.
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
                # Nearest-resize the alpha to the new grid (mask edges are fine).
                from PIL import Image

                a_img = Image.fromarray(alpha, mode="L").resize(
                    (alpha.shape[1] * f, alpha.shape[0] * f),
                    resample=Image.Resampling.NEAREST,
                )
                alpha = np.asarray(a_img, dtype=np.uint8)
            # Scale the affine so the COG stays correctly georeferenced: the new
            # pixel is 1/f the ground size of the original.
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

        # Serve a real tiled COG with overviews (same writer the hillshade /
        # blended-composite COGs use). Falls back to flat bytes when
        # gdal_translate is unavailable (e.g. no gdal-bin on the box).
        try:
            gdal_bin = _get_gdaldem_bin()
            return _translate_to_cog(flat_tmp, gdal_bin)
        except Exception:  # noqa: BLE001 - COG translate is best-effort
            logger.warning(
                "enhance_satellite_image: COG translate unavailable; "
                "returning flat RGB GTiff bytes",
                exc_info=True,
            )
            with open(flat_tmp, "rb") as fh:
                return fh.read()

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
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """OPTIONAL polish/enhance pass for a true-color satellite RGB image.

    Use this (not publish_layer) when you want an OPTIONAL cosmetic enhancement pass on an RGB satellite COG before publishing.

    Reads an RGB image COG and returns a NEW, polished RGB COG that reads closer
    to NOAA/CIRA's de-hazed "GeoColor" look: de-hazed (atmospheric haze removed),
    white-balanced, lightly sharpened, and optionally upscaled. This is a
    composable, OPTIONAL cosmetic step - call it ONLY when the user asks to
    "polish", "enhance", "clean up", "de-haze", "sharpen", or "make this satellite
    image look better / clearer / more like GeoColor". It does NOT change the
    data meaning; it improves the PRESENTATION of an existing true-color image.

    It works on ANY 3(+)-band RGB raster - a GOES/ABI true-color composite, a
    Sentinel/NAIP true-color COG, or any RGB image LayerURI. It is NOT fire- or
    GOES-specific. A single-band data raster (DEM, NDVI, an index/mask, a
    paletted COG with no RGB) is NOT a true-color image and returns a typed
    NOT_AN_RGB_IMAGE error.

    What each pass does (all individually toggleable):
        rayleigh_correct (default True): de-haze. Subtracts a per-channel
            atmospheric path-radiance / haze floor (dark-object subtraction),
            heavier on BLUE (Rayleigh scattering peaks in blue), then re-stretches
            contrast. This is the single biggest gap vs CIRA's clean look - it
            lifts the blue cast and saturates the land.
        white_balance (default True): a mild gray-world per-channel gain so
            vegetation/terrain read natural, with a small green trim so greens are
            not neon.
        sharpen (default True): a light unsharp mask (radius/amount tunable) so
            edges, coastlines, and roads read crisp.
        upscale_factor (default 1 = off): Lanczos upscale by an integer factor
            for a higher-resolution presentation of a coarse image. The output
            georeferencing is scaled to stay correctly placed.

    When to use:
        - The user wants a raw satellite true-color image to look cleaner / less
          hazy / more like CIRA GeoColor.
        - As a final cosmetic step after composing a true-color RGB (e.g. a GOES
          true-color frame) before publishing it for presentation.

    When NOT to use:
        - On a data raster (DEM, NDVI, slope, a hazard depth grid, a paletted
          land-cover index) - those are not true-color photos; returns
          NOT_AN_RGB_IMAGE.
        - When the user needs the UNMODIFIED radiometry for analysis (this is a
          cosmetic transform; keep the original for any quantitative use).
        - To blend/drape two rasters into one - use compute_blended_composite.

    Params:
        source_layer_uri: layer handle (layer_id) OR s3:// URI OR local path of
            the RGB image to polish. The server resolves a handle to the real COG
            URI (it is in RESOLVABLE_URI_PARAMS).
        rayleigh_correct, white_balance, sharpen: per-pass on/off toggles.
        upscale_factor: integer >= 1 (1 = no upscale).
        haze_strength: 0..~1.5, global multiplier on the de-haze floor.
        blue_extra: extra blue de-haze fraction on top of haze_strength.
        sharpen_radius / sharpen_amount: unsharp-mask radius (px) + strength.
        wb_strength: 0..1 gray-world white-balance pull.
        green_trim: small green-channel trim after balancing (0..~0.1).

    Returns:
        A ``LayerURI`` (layer_type="raster") pointing at the polished RGB(A) COG
        in the cache bucket
        (``.../cache/static-30d/enhanced/<key>.tif``). Rendered verbatim by
        publish_layer's multiband RGB passthrough - no new style preset needed.
        Pass the returned handle to ``publish_layer`` to put the polished image
        on the map. layer_id/name derive as "Enhanced <source>".

    FR-CE-8: routed through ``read_through`` so identical calls (same source +
    same toggles/params) reuse the cached polished image. 30-day TTL (the output
    is fully determined by its input + params).

    Cross-tool dependencies:
        Upstream (consumes): any RGB-image-producing tool - a GOES/ABI true-color
            composite, fetch_naip (NAIP true-color), a Sentinel true-color COG.
        Downstream (feeds): publish_layer - pass the returned handle as layer_uri
            to put the polished image on the map.

    Raises:
        EnhanceSatelliteImageError: typed, with ``error_code`` for the pipeline
            strip - IMAGE_DOWNLOAD_FAILED, NOT_AN_RGB_IMAGE, INVALID_PARAM, or
            ENHANCE_FAILED.
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
        style_preset="rgb_composite",  # RGB(A) COG - rendered as a true-color image
        role="context",
        units="rgb",
    )
