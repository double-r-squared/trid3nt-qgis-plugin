"""Atomic tool ``compute_blended_composite`` — bake two rasters into ONE COG (job-0319).

This module registers one atomic tool that **server-side blends two raster
layers into a single composite Cloud-Optimized GeoTIFF**:

    ``compute_blended_composite(base_layer_uri, overlay_layer_uri, blend_mode,
                                overlay_opacity) → LayerURI``

WHY THIS EXISTS
---------------
MapLibre GL (the client's renderer) **cannot multiply-blend two raster
layers on the client**. There is no client-side "multiply blend mode" for
raster sources. So when a user wants a *shaded land cover* (land-cover RGB ×
hillshade grayscale) or a *shaded relief* (colored relief × hillshade), the
ONLY way to deliver it is to bake the two rasters into one composite raster
here, server-side, and publish that single layer.

**The agent MUST use this tool for that — and must NEVER tell the user to set
a client-side blend / multiply / opacity blend mode in the map. That is not a
capability the client has.**

The multiply math is the same Imhof "multiply" used by
``compute_hillshade``'s ``swiss_double`` preset (two hillshades multiplied):

    result = (A / 255) * (B / 255) * 255

generalized here to an RGB base × a (typically grayscale) overlay:

    overlay_factor = overlay_gray / 255            # in [0, 1]
    # honor opacity: at opacity=0 the overlay has no effect (factor → 1);
    # at opacity=1 it fully multiplies.
    effective      = (1 - opacity) + opacity * overlay_factor
    result_rgb     = base_rgb * effective

Supported ``blend_mode`` values:

- ``"multiply"`` (default) — darkens the base where the overlay is dark; the
  canonical hillshade-drape / shaded-relief / shaded-landcover blend.
- ``"screen"`` — inverse multiply; lightens (rarely used for shading).
- ``"overlay"`` — multiply in the dark half, screen in the light half
  (contrast-preserving).
- ``"normal"`` — alpha-composite the overlay over the base (no shading math;
  honors ``overlay_opacity`` as straight alpha).

IMPLEMENTATION FLOW (cache miss)
--------------------------------
1. Resolve + stage both layer URIs to local COGs (gs:// / s3:// / local path).
2. Read the BASE with rasterio → RGB(A) uint8. A single-band base that carries
   an embedded GDAL color table (e.g. the NLCD land-cover palette-index COG) is
   colorized through that table (index → palette RGBA) so the composite keeps
   the real land-cover hues; a single-band base with NO color table (a true
   grayscale base such as a hillshade) is broadcast to grayscale (R=G=B).
3. Read + **align the OVERLAY to the BASE grid** (reproject/resample to the
   base CRS + transform + shape, nodata-safe) → grayscale.
4. Apply the per-pixel blend (numpy), honoring ``overlay_opacity``.
5. Carry an alpha band: opaque where the base is valid, transparent where the
   base nodata mask says so (so the composite never paints over other layers).
6. Write a flat GTiff then run ``_translate_to_cog`` (imported from
   ``compute_hillshade``) → a **tiled COG WITH overviews**.
7. ``read_through`` caches the bytes (static-30d, source_class="blended").

The output is clipped to the **overlap extent** of the two inputs (the base
grid is the canvas; overlay pixels outside it are dropped by the warp; base
pixels with no overlay coverage keep the base unchanged via the nodata-safe
overlay fill of 255 → factor 1.0).

Cross-cutting invariants:
- Invariant 2 (Deterministic workflows): preserves — zero LLM calls.
- FR-DC-6 (cacheable): honors — ``cacheable=True``, ``ttl_class="static-30d"``,
  ``source_class="blended"``; the composite is fully determined by its inputs.
- NFR-R-1 (resilience): preserves — every failure surfaces as a typed
  ``BlendedCompositeError`` with a SCREAMING_SNAKE_CASE ``error_code``.
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
# job-0319: reuse the hillshade COG writer (tiled + overviews) verbatim so the
# composite renders fast over WMS/TiTiler exactly like every other derived COG.
from trid3nt_server.tools.processing.compute_hillshade import _translate_to_cog, _get_gdaldem_bin

__all__ = [
    "compute_blended_composite",
    "BlendedCompositeError",
]

logger = logging.getLogger("trid3nt_server.tools.processing.compute_blended_composite")


# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class BlendedCompositeError(RuntimeError):
    """Raised when raster blending fails or an input cannot be fetched.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the
    pipeline strip (NFR-R-1 typed-error requirement). ``retryable`` follows the
    FR-AS-11 convention so ``summarize_tool_result`` renders the envelope.

    Codes:
    - ``BASE_DOWNLOAD_FAILED`` — the base layer URI could not be staged.
    - ``OVERLAY_DOWNLOAD_FAILED`` — the overlay layer URI could not be staged.
    - ``BLEND_FAILED`` — the numpy/rasterio blend step failed.
    - ``INVALID_BLEND_MODE`` — an unsupported blend_mode was requested.
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
# URI staging (mirrors the sibling compute_* tools' download helpers)
# ---------------------------------------------------------------------------


def _stage_uri_to_local(
    uri: str, label: str, storage_client: object | None = None, error_code: str = "RASTER_DOWNLOAD_FAILED"
) -> tuple[str, bool]:
    """Stage a layer URI to a local file path.

    Returns ``(local_path, is_temp)`` where ``is_temp`` marks paths the caller
    must clean up. Handles ``s3://`` and local-path inputs exactly like
    ``compute_zonal_statistics``/``compute_hillshade`` so handle-resolved COGs
    work. GCP is decommissioned, so ``storage_client`` is ignored.

    Raises ``BlendedCompositeError(error_code, …)`` on any download failure.
    """
    del storage_client  # GCP decommissioned — S3/local only.
    # sprint-14-aws: s3:// staging via the shared boto3 reader.
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

    # Local path (test / dev convenience) — read in place.
    if not os.path.isfile(uri):
        raise BlendedCompositeError(
            error_code, f"local raster path {uri!r} does not exist"
        )
    return uri, False


# ---------------------------------------------------------------------------
# Raster read + align helpers
# ---------------------------------------------------------------------------


def _colormap_to_lut(colormap: dict):
    """Build a (256, 4) uint8 index→RGBA lookup table from a GDAL color table.

    ``colormap`` is the ``{index: (r, g, b, a)}`` dict rasterio returns from
    ``dataset.colormap(band)``. Entries are clamped to the [0, 255] index range
    (GDAL palette index rasters are uint8); any index the table does not cover
    defaults to opaque black (rgb=0, a=255) so it still paints rather than
    silently vanishing. Returns the LUT array.
    """
    import numpy as np

    lut = np.zeros((256, 4), dtype=np.uint8)
    lut[:, 3] = 255  # default fully-opaque for uncovered indices
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
    """Read the base raster as an (3, H, W) uint8 RGB array + a valid-mask.

    A single-band base with an **embedded GDAL color table** (e.g. the NLCD
    land-cover palette-index COG) is colorized through that table — each index
    is mapped to its palette RGB(A) so the composite carries the real land-cover
    colors (job-0323 fix). A single-band base with NO color table is broadcast
    to grayscale (R=G=B) — the historical behavior for true-grayscale bases such
    as a hillshade used as the base. 3/4-band inputs use the first 3 bands as
    RGB; a 4th band (if present) is treated as alpha for the valid-mask. Returns
    ``(rgb, valid_mask, profile)`` where ``valid_mask`` is a bool (H, W) array —
    True == paint, False == transparent.
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
            # job-0323: a single-band base may be a palette-INDEX raster whose
            # colors live in an embedded GDAL color table (e.g. NLCD land
            # cover). Colorize through that table so the blend keeps the real
            # palette hues instead of a flat grayscale broadcast. Fall back to
            # the grayscale broadcast only when there is no color table (a true
            # grayscale base like a hillshade).
            colormap = None
            try:
                colormap = src.colormap(1)
            except (ValueError, KeyError):
                colormap = None  # no embedded color table → grayscale base
            except Exception:  # noqa: BLE001 — any read failure → grayscale
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
        # Valid-mask: prefer an explicit alpha band, then a palette alpha
        # (transparent palette entries), then the dataset mask, then the nodata
        # value, defaulting to all-valid.
        if count >= 4:
            alpha = src.read(4)
            valid = alpha > 0
        else:
            try:
                valid = src.read_masks(1) > 0
            except Exception:  # noqa: BLE001 — fall back to nodata compare
                valid = np.ones(rgb.shape[1:], dtype=bool)
            if nodata is not None:
                src_band = src.read(1)
                valid &= src_band != nodata
            if palette_alpha is not None:
                # Palette entries with alpha==0 (e.g. NLCD index 0 / no-data)
                # are transparent in the composite too.
                valid &= palette_alpha > 0
    rgb = np.clip(rgb, 0.0, 255.0)
    return rgb, valid, profile


def _read_overlay_aligned_gray(overlay_path: str, base_profile: dict):
    """Read the overlay and reproject/resample it onto the base grid → grayscale.

    Returns a float32 (H, W) array of grayscale values in [0, 255] on the
    BASE's CRS + transform + shape. Pixels with no overlay coverage (outside
    the overlay extent, or overlay-nodata) are filled with **255** so the
    multiply factor is 1.0 there — i.e. the base shows through unchanged
    (nodata-safe alignment).
    """
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    dst_crs = base_profile["crs"]
    dst_transform = base_profile["transform"]
    dst_h = base_profile["height"]
    dst_w = base_profile["width"]

    with rasterio.open(overlay_path) as src:
        # Collapse the overlay to a single grayscale band: a 1-band overlay is
        # used directly; a 3/4-band overlay is averaged across RGB.
        if src.count >= 3:
            bands = src.read([1, 2, 3]).astype(np.float32)
            src_gray = bands.mean(axis=0)
        else:
            src_gray = src.read(1).astype(np.float32)
        src_nodata = src.nodata
        src_crs = src.crs
        src_transform = src.transform

    # Fill value 255 → multiply factor 1.0 (base unchanged where uncovered).
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
    """Apply ``blend_mode`` per-pixel, honoring ``overlay_opacity``.

    Args:
        rgb: (3, H, W) float32 base in [0, 255].
        overlay_gray: (H, W) float32 overlay in [0, 255], aligned to the base.
        blend_mode: one of ``_VALID_BLEND_MODES``.
        overlay_opacity: 0.0 (overlay has no effect) .. 1.0 (full effect).

    Returns a (3, H, W) float32 array in [0, 255].
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
        # Photoshop "overlay": multiply where base<0.5, screen where base>=0.5.
        low = 2.0 * base * over3
        high = 1.0 - 2.0 * (1.0 - base) * (1.0 - over3)
        blended = np.where(base < 0.5, low, high)
    elif blend_mode == "normal":
        # Straight alpha-composite of the (grayscale) overlay over the base.
        blended = over3
    else:  # pragma: no cover — guarded by the caller
        raise BlendedCompositeError(
            "INVALID_BLEND_MODE",
            f"unsupported blend_mode={blend_mode!r}; allowed: {sorted(_VALID_BLEND_MODES)}",
        )

    # Lerp by opacity: opacity=0 → base unchanged; opacity=1 → fully blended.
    out = (1.0 - opacity) * base + opacity * blended
    return np.clip(out * 255.0, 0.0, 255.0)


def _run_blend(
    base_uri: str,
    overlay_uri: str,
    blend_mode: str,
    overlay_opacity: float,
    storage_client: object | None,
) -> bytes:
    """Blend the two rasters and return tiled-COG-with-overviews bytes.

    Cache-miss path: stages both URIs, aligns the overlay to the base grid,
    applies the blend, writes an RGBA uint8 COG. Raises BlendedCompositeError
    on any failure.
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

        # Alpha band: opaque where the base is valid, transparent elsewhere so
        # the composite never paints over neighbouring layers.
        alpha = np.where(valid, 255, 0).astype(np.uint8)
        out_rgba = np.concatenate(
            [blended.astype(np.uint8), alpha[None, :, :]], axis=0
        )

        # Write a flat RGBA GTiff, then translate to a tiled COG with overviews.
        out_profile = base_profile.copy()
        out_profile.update(
            driver="GTiff",
            dtype="uint8",
            count=4,
            nodata=None,
            photometric="RGB",
            alpha="YES",
        )
        # Drop keys that may conflict with the RGBA rewrite.
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

        # job-0319: serve a real tiled COG with overviews (same writer the
        # hillshade / colored-relief COGs use). Falls back to flat bytes only
        # if gdal_translate is unavailable.
        try:
            gdal_bin = _get_gdaldem_bin()
            return _translate_to_cog(flat_tmp, gdal_bin)
        except Exception:  # noqa: BLE001 — COG step is best-effort
            logger.warning(
                "compute_blended_composite: COG translate unavailable; "
                "returning flat RGBA GTiff bytes",
                exc_info=True,
            )
            with open(flat_tmp, "rb") as fh:
                return fh.read()

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
    # computation is local rasterio/numpy/GDAL — no external API calls),
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
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Bake/blend/drape TWO raster layers into ONE composite COG, server-side.

    Reads two rasters, aligns the overlay to the base grid, multiply-blends
    them per-pixel, and returns a SINGLE new raster ``LayerURI`` (an RGBA COG
    with overviews). This is how you produce a *shaded land cover* (land-cover
    RGB x hillshade grayscale), a *shaded relief* (colored relief x hillshade),
    or any "drape layer A over layer B" composite.

    The BASE may be a PALETTED / CATEGORICAL single-band raster (CRITICAL):
        The base does NOT have to be a 3-band RGB image. A single-band raster
        that carries an EMBEDDED GDAL color table — e.g. the NLCD land-cover
        COG returned by fetch_landcover, whose pixels are class indices with a
        palette attached — is fully supported. This tool reads that embedded
        color table and applies it (index -> palette RGBA) BEFORE blending, so
        blending the land cover DIRECTLY yields the real NLCD CLASS colors
        (forest green, water blue, developed grey, cropland tan, etc.) draped
        with the hillshade.

        Therefore, to "bake"/"shade" NLCD land cover, pass the land-cover layer
        handle (from fetch_landcover) STRAIGHT IN as ``base_layer_uri`` and the
        hillshade as ``overlay_layer_uri``. Do NOT pre-colorize the land cover
        first, and do NOT substitute compute_colored_relief as the base in its
        place — colored_relief is ELEVATION colors (a DEM color ramp), NOT
        land-cover classes, so using it loses the land-cover information and
        produces a terrain map, not a shaded land-cover map. (A single-band
        base with NO color table — a true grayscale base such as a raw
        hillshade — is broadcast to R=G=B grayscale, the historical behavior.)

    CRITICAL — NEVER tell the user to set a client-side blend / multiply /
    opacity blend mode on the map. MapLibre GL (the client's renderer)
    CANNOT multiply-blend two raster layers in the browser. The ONLY way to
    deliver a multiply-blended / shaded / draped composite is to bake it into
    one raster with THIS tool and publish that single layer. If a user asks to
    "combine", "blend", "multiply", "drape", "overlay … as a shaded base",
    "shade the land cover with the hillshade", or "make a shaded relief", call
    compute_blended_composite — do not instruct the user to change a map blend
    setting.

    When to use:
        - Shaded / baked land cover: NLCD land cover (base) x hillshade
          grayscale (overlay), blend_mode="multiply". Pass the fetch_landcover
          handle DIRECTLY as base_layer_uri — it is palette-aware (see above),
          so the composite shows the NLCD class colors shaded by terrain. Do
          NOT colorize it first and do NOT use compute_colored_relief as the
          base (that is elevation colors, not land-cover classes).
        - Shaded relief: colored relief (base) x hillshade grayscale (overlay),
          blend_mode="multiply".
        - Any request to bake/combine/drape/blend two raster layers into one.
        - The user wants a cartographic shaded base they can put other overlays
          on top of.

    When NOT to use:
        - Combining two VECTOR layers (this is raster-only).
        - Stacking layers that should stay independently toggleable in the
          LayerPanel (keep them as separate published layers instead).
        - Producing the hillshade or colored relief itself (use
          compute_hillshade / compute_colored_relief first, then blend).

    Blend modes:
        "multiply" (default): result = base_rgb * (overlay_gray/255). Darkens
            the base where the overlay (hillshade) is dark — the canonical
            hillshade-drape / shaded-relief / shaded-landcover blend.
        "screen": inverse multiply; lightens the base (rarely used for shading).
        "overlay": multiply in the dark half, screen in the light half —
            contrast-preserving.
        "normal": alpha-composite the (grayscale) overlay straight over the
            base, honoring overlay_opacity as alpha; no shading math.

    Params:
        base_layer_uri: layer handle (layer_id) OR gs:///s3:// URI of the BASE
            raster — the colored OR PALETTED/CATEGORICAL layer you want to keep
            the hue of (e.g. the NLCD land cover from fetch_landcover, or the
            colored relief). A single-band paletted/categorical base (NLCD land
            cover) is colorized through its EMBEDDED color table automatically,
            so pass the fetch_landcover handle DIRECTLY here — do not pre-
            colorize it and do not swap in compute_colored_relief (elevation
            colors) when the user wanted land cover. Pass the handle returned by
            the producing tool (fetch_landcover / compute_colored_relief / …);
            the server resolves it to the real COG URI.
        overlay_layer_uri: layer handle OR URI of the OVERLAY raster — typically
            a grayscale hillshade (compute_hillshade). It is reprojected/
            resampled onto the base grid automatically; a single-band overlay is
            used as-is, a multi-band overlay is averaged to grayscale.
        blend_mode: one of the four modes above. Defaults to "multiply".
        overlay_opacity: 0.0–1.0. 1.0 (default) = the overlay fully multiplies;
            0.5 = a half-strength shade; 0.0 = the base unchanged.

    Returns:
        A ``LayerURI`` (layer_type="raster") pointing at an RGBA COG in the
        cache bucket:
        ``s3://trid3nt-cache/cache/static-30d/blended/<key>.tif``.
        The output shares the BASE's CRS + grid and is clipped to the overlap
        extent (the base is the canvas; base pixels uncovered by the overlay
        keep the base color). Pass the returned handle to ``publish_layer`` to
        put the single shaded layer on the map. layer_id/name are derived like
        "Shaded <base>".

    FR-CE-8: routed through ``read_through`` so identical
    ``(base_layer_uri, overlay_layer_uri, blend_mode, overlay_opacity)`` calls
    reuse the cached composite. 30-day TTL (the composite is fully determined
    by its two inputs).

    Cross-tool dependencies:
        Upstream (consumes):
        - ``fetch_landcover`` / ``compute_colored_relief`` — the BASE raster.
        - ``compute_hillshade`` — the OVERLAY grayscale hillshade.
        Downstream (feeds):
        - ``publish_layer`` — pass the returned handle as ``layer_uri`` to put
          the single shaded composite on the map.

    Raises:
        BlendedCompositeError: if either input cannot be fetched, the blend
            fails, or an unsupported blend_mode is requested. Carries
            ``error_code`` for the pipeline strip.
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
        style_preset="rgb_composite",  # RGBA COG — rendered as a true-color image
        role="context",
        units="rgb",
    )
