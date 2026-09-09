"""Atomic tool ``compute_blended_composite`` - bake two rasters into ONE COG.

The base grid is the canvas: the overlay is reprojected onto it, and a pixel with
no overlay coverage is filled with 255 so the base shows through unchanged.
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Any, Literal

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through
from trid3nt_server.emission.cog import translate_to_cog as _translate_to_cog

__all__ = [
    "compute_blended_composite",
    "BlendedCompositeError",
]

logger = logging.getLogger("trid3nt_server.tools.processing.compute_blended_composite.compute_blended_composite")


# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class BlendedCompositeError(RuntimeError):
    """An input could not be staged or the blend failed. ``error_code`` is one of
    BASE_DOWNLOAD_FAILED, OVERLAY_DOWNLOAD_FAILED, BLEND_FAILED, INVALID_BLEND_MODE.
    """

    retryable = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


_VALID_BLEND_MODES = frozenset({"multiply", "overlay", "screen", "normal"})


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_COMPUTE_BLENDED_COMPOSITE_METADATA = AtomicToolMetadata(
    name="compute_blended_composite",
    ttl_class="static-30d",   # fully determined by its two inputs; stable
    source_class="blended",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# URI staging
# ---------------------------------------------------------------------------


def _stage_uri_to_local(
    uri: str, label: str, storage_client: object | None = None, error_code: str = "RASTER_DOWNLOAD_FAILED"
) -> tuple[str, bool]:
    """Stage an ``s3://`` or local layer URI, returning ``(local_path, is_temp)``;
    ``is_temp`` marks a path the caller must unlink. ``storage_client`` is ignored.
    """
    del storage_client
    if uri.startswith("s3://"):
        try:
            from trid3nt_server.tools.cache import read_object_bytes_s3

            with tempfile.NamedTemporaryFile(
                suffix=".tif", delete=False, prefix=f"trid3nt_blend_{label}_"
            ) as f:
                f.write(read_object_bytes_s3(uri))
                return f.name, True
        except Exception as exc:  # noqa: BLE001
            raise BlendedCompositeError(
                error_code, f"S3 download failed for {uri!r}: {exc}"
            ) from exc

    if not os.path.isfile(uri):
        raise BlendedCompositeError(
            error_code, f"local raster path {uri!r} does not exist"
        )
    return uri, False


# ---------------------------------------------------------------------------
# Raster read + align helpers
# ---------------------------------------------------------------------------


def _colormap_to_lut(colormap: dict):
    """A (256, 4) uint8 index -> RGBA lookup table from rasterio's ``colormap``
    dict; an index the table does not cover defaults to opaque black, not vanish.
    """
    import numpy as np

    lut = np.zeros((256, 4), dtype=np.uint8)
    lut[:, 3] = 255
    for idx, entry in colormap.items():
        if not (0 <= idx <= 255):
            continue
        # GDAL color-table entries are (R, G, B) or (R, G, B, A); pad alpha.
        r = entry[0] if len(entry) > 0 else 0
        g = entry[1] if len(entry) > 1 else 0
        b = entry[2] if len(entry) > 2 else 0
        a = entry[3] if len(entry) > 3 else 255
        lut[idx] = (r, g, b, a)
    return lut


def _read_base_rgb(base_path: str):
    """``(rgb, valid_mask, profile)``: a (3, H, W) uint8 RGB array and a bool (H, W)
    mask where True means paint. Bands beyond the third are alpha, not colour.
    """
    import numpy as np
    import rasterio

    with rasterio.open(base_path) as src:
        profile = src.profile.copy()
        count = src.count
        nodata = src.nodata
        palette_alpha = None  # (H, W) palette-derived alpha, if colorized
        if count >= 3:
            rgb = src.read([1, 2, 3]).astype(np.float32)
        else:
            band = src.read(1)
            # A single-band base may be a palette-INDEX raster whose colours
            # live in an embedded GDAL color table (NLCD land cover). Colorizing
            # through that table keeps the real palette hues; the grayscale
            # broadcast is only right for a base that has no table at all.
            colormap = None
            try:
                colormap = src.colormap(1)
            except (ValueError, KeyError):
                colormap = None  # no embedded color table -> grayscale base
            except Exception:  # noqa: BLE001 -- any read failure -> grayscale
                colormap = None
            if colormap:
                lut = _colormap_to_lut(colormap)
                idx = band.astype(np.intp) & 0xFF  # clamp to LUT range [0,255]
                mapped = lut[idx]  # (H, W, 4) uint8 RGBA
                rgb = np.transpose(mapped[:, :, :3], (2, 0, 1)).astype(np.float32)
                palette_alpha = mapped[:, :, 3]
            else:
                fband = band.astype(np.float32)
                rgb = np.stack([fband, fband, fband], axis=0)
        # Valid-mask precedence: an explicit alpha band, then a palette alpha,
        # then the dataset mask, then the nodata value, else all-valid.
        if count >= 4:
            alpha = src.read(4)
            valid = alpha > 0
        else:
            try:
                valid = src.read_masks(1) > 0
            except Exception:  # noqa: BLE001 -- fall back to nodata compare
                valid = np.ones(rgb.shape[1:], dtype=bool)
            if nodata is not None:
                src_band = src.read(1)
                valid &= src_band != nodata
            if palette_alpha is not None:
                # A palette entry with alpha==0 (NLCD index 0) stays
                # transparent in the composite.
                valid &= palette_alpha > 0
    rgb = np.clip(rgb, 0.0, 255.0)
    return rgb, valid, profile


def _read_overlay_aligned_gray(overlay_path: str, base_profile: dict):
    """The overlay resampled onto the base grid as float32 (H, W) gray in [0, 255];
    uncovered pixels are 255, so the multiply factor is 1.0 and the base shows.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    dst_crs = base_profile["crs"]
    dst_transform = base_profile["transform"]
    dst_h = base_profile["height"]
    dst_w = base_profile["width"]

    with rasterio.open(overlay_path) as src:
        if src.count >= 3:
            bands = src.read([1, 2, 3]).astype(np.float32)
            src_gray = bands.mean(axis=0)
        else:
            src_gray = src.read(1).astype(np.float32)
        src_nodata = src.nodata
        src_crs = src.crs
        src_transform = src.transform

    dst_gray = np.full((dst_h, dst_w), 255.0, dtype=np.float32)
    reproject(
        source=src_gray,
        destination=dst_gray,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        src_nodata=src_nodata,
        dst_nodata=255.0,
        resampling=Resampling.bilinear,
    )
    return np.clip(dst_gray, 0.0, 255.0)


def _apply_blend(rgb, overlay_gray, blend_mode: str, overlay_opacity: float):
    """Blend a (3, H, W) base with an aligned (H, W) overlay, both in [0, 255],
    into a (3, H, W) float32 result; ``overlay_opacity`` 0.0 leaves the base.
    """
    import numpy as np

    opacity = float(min(max(overlay_opacity, 0.0), 1.0))
    base = rgb / 255.0                       # [0, 1]
    over = overlay_gray / 255.0              # [0, 1], broadcast over channels
    over3 = over[None, :, :]

    if blend_mode == "multiply":
        blended = base * over3
    elif blend_mode == "screen":
        blended = 1.0 - (1.0 - base) * (1.0 - over3)
    elif blend_mode == "overlay":
        # multiply where base < 0.5, screen where base >= 0.5
        low = 2.0 * base * over3
        high = 1.0 - 2.0 * (1.0 - base) * (1.0 - over3)
        blended = np.where(base < 0.5, low, high)
    elif blend_mode == "normal":
        blended = over3
    else:  # pragma: no cover -- guarded by the caller
        raise BlendedCompositeError(
            "INVALID_BLEND_MODE",
            f"unsupported blend_mode={blend_mode!r}; allowed: {sorted(_VALID_BLEND_MODES)}",
        )

    out = (1.0 - opacity) * base + opacity * blended
    return np.clip(out * 255.0, 0.0, 255.0)


def _run_blend(
    base_uri: str,
    overlay_uri: str,
    blend_mode: str,
    overlay_opacity: float,
    storage_client: object | None,
) -> bytes:
    """Stage both URIs, align, blend, and return RGBA tiled-COG bytes with
    overviews; any failure raises ``BlendedCompositeError``.
    """
    import numpy as np
    import rasterio

    base_path: str | None = None
    overlay_path: str | None = None
    base_is_temp = overlay_is_temp = False
    flat_tmp: str | None = None

    try:
        base_path, base_is_temp = _stage_uri_to_local(
            base_uri, "base", storage_client, "BASE_DOWNLOAD_FAILED"
        )
        overlay_path, overlay_is_temp = _stage_uri_to_local(
            overlay_uri, "overlay", storage_client, "OVERLAY_DOWNLOAD_FAILED"
        )

        rgb, valid, base_profile = _read_base_rgb(base_path)
        overlay_gray = _read_overlay_aligned_gray(overlay_path, base_profile)
        blended = _apply_blend(rgb, overlay_gray, blend_mode, overlay_opacity)

        # Transparent where the base is invalid, so the composite never paints
        # over the layers beneath it.
        alpha = np.where(valid, 255, 0).astype(np.uint8)
        out_rgba = np.concatenate(
            [blended.astype(np.uint8), alpha[None, :, :]], axis=0
        )

        out_profile = base_profile.copy()
        out_profile.update(
            driver="GTiff",
            dtype="uint8",
            count=4,
            nodata=None,
            photometric="RGB",
            alpha="YES",
        )
        out_profile.pop("colorinterp", None)

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
            flat_tmp = f.name
        with rasterio.open(flat_tmp, "w", **out_profile) as dst:
            dst.write(out_rgba)
            dst.colorinterp = [
                rasterio.enums.ColorInterp.red,
                rasterio.enums.ColorInterp.green,
                rasterio.enums.ColorInterp.blue,
                rasterio.enums.ColorInterp.alpha,
            ]

        return _translate_to_cog(flat_tmp)

    except BlendedCompositeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BlendedCompositeError(
            "BLEND_FAILED",
            f"raster blend failed for base={base_uri!r} overlay={overlay_uri!r} "
            f"mode={blend_mode!r}: {exc}",
        ) from exc
    finally:
        for path, is_temp in (
            (base_path, base_is_temp),
            (overlay_path, overlay_is_temp),
            (flat_tmp, True),
        ):
            if path and is_temp:
                try:
                    os.unlink(path)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Registered atomic tool
# ---------------------------------------------------------------------------


@register_tool(
    _COMPUTE_BLENDED_COMPOSITE_METADATA,
    # Annotations: readOnlyHint=True (reads two input rasters; writes a cache
    # artifact only via the read-through shim), openWorldHint=False (all
    # computation is local rasterio/numpy/GDAL -- no external API calls),
    # destructiveHint=False, idempotentHint=True (deterministic transform;
    # same inputs always produce the same output pixels).
)
def compute_blended_composite(
    base_layer_uri: str,
    overlay_layer_uri: str,
    blend_mode: Literal["multiply", "overlay", "screen", "normal"] = "multiply",
    overlay_opacity: float = 1.0,
    *,
    _storage_client: object | None = None,
    _bucket: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Bake/blend/drape TWO raster layers into ONE composite COG, server-side.

    Blends per-pixel: shaded land cover (land-cover RGB x hillshade), shaded
    relief (colored relief x hillshade), any drape of A over B. NEVER tell the
    user to set a blend mode in QGIS instead - baking here IS the delivery. Not
    for vector layers, for layers meant to stay toggleable, or to produce the
    hillshade or colored relief itself.

    Params:
        base_layer_uri: BASE raster, keeps its hue. A PALETTED base (NLCD from
            fetch_landcover) is auto-colorized through its embedded table - pass
            that handle directly, do not pre-colorize it.
        overlay_layer_uri: OVERLAY raster, typically a grayscale hillshade;
            reprojected onto the base grid, multi-band averaged to gray.
        blend_mode: "multiply" (default, the hillshade drape), "screen"
            (lightens), "overlay" (contrast-preserving), "normal" (alpha).
        overlay_opacity: 0.0 (base unchanged) to 1.0 (full effect).

    Output keeps the base CRS and grid, clipped to the overlap.
    """
    if blend_mode not in _VALID_BLEND_MODES:
        raise BlendedCompositeError(
            "INVALID_BLEND_MODE",
            f"unsupported blend_mode={blend_mode!r}; allowed: {sorted(_VALID_BLEND_MODES)}",
        )

    params = {
        "base_layer_uri": base_layer_uri,
        "overlay_layer_uri": overlay_layer_uri,
        "blend_mode": blend_mode,
        "overlay_opacity": round(float(overlay_opacity), 4),
    }

    result = read_through(
        metadata=_COMPUTE_BLENDED_COMPOSITE_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _run_blend(
            base_uri=base_layer_uri,
            overlay_uri=overlay_layer_uri,
            blend_mode=blend_mode,
            overlay_opacity=float(overlay_opacity),
            storage_client=_storage_client,
        ),
        bucket=_bucket,
        storage_client=_storage_client,
    )
    assert result.uri is not None, (
        "compute_blended_composite is cacheable; uri must be set by read_through"
    )

    base_key = base_layer_uri.rstrip("/").rsplit("/", 1)[-1].replace(".tif", "")
    layer_id = f"blended-{base_key}-{blend_mode}-{abs(hash((base_layer_uri, overlay_layer_uri, blend_mode))) % 100_000:05d}"
    name = f"Shaded {base_key}" if blend_mode == "multiply" else f"Blended {base_key} ({blend_mode})"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="raster",
        uri=result.uri,
        style={"kind": "continuous"},
        role="context",
        units="rgb",
    )
