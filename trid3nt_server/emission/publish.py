"""The raster PUBLISH mechanism - write, register, notify.

Emission is AUTOMATIC: there is no "display this" intent and no
``publish_layer`` tool for a model to call. Every renderable raster a tool
returns rides :func:`trid3nt_server.emission.layer_uri_emit.publish_for_emission`
through this module on its way to the map, intermediates included - the user
hides what they do not want to see in QGIS.

    ``publish_layer(layer_uri, layer_id, style, ...)``
      -> ``str`` (the raster's ``s3://`` COG URI, ready for the envelope)

One store, one scheme. A raster lives as a COG at ``s3://<bucket>/<key>`` and
the QGIS plugin - the ONLY client - reads THAT uri natively through GDAL
``/vsis3``, so a publish never mints a second face for the same layer. What it
does, in order:

1. **write** - enforce COG overviews, writing a tiled+overview sibling into the
   same bucket when the source has none (a no-overview COG renders spotty);
2. **register** - ``observe_published_layer`` binds the layer handle to that
   one uri, so downstream tools resolve the handle to readable bytes;
3. **notify** - resolve the DECLARED style row through ``emission/presets.py``
   (the already-painted guards first, then the preset) and stash the resulting
   ``LegendKey`` keyed by the uri the envelope carries.

Vectors are a benign no-op: they are already objects in the same store and the
plugin opens them natively too, so there is nothing to publish.

**Cross-cutting principles:**

- **Side effect, never cached.** A publish writes overview COGs and registers a
  layer; there is nothing to memoize.
- **Resilience:** failures surface as typed :class:`PublishLayerError`
  (not unhandled exceptions); style/legend/overview probes fail OPEN so a
  publish is never blocked by a best-effort enhancement. The caller at the
  emission seam also fails open: a raster whose publish fails still reaches
  the map as its ``s3://`` COG, unstyled, because the QGIS plugin can read
  that - a degrade, not a broken layer row.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from trid3nt_contracts import new_ulid

from . import presets
from .cog import translate_to_cog
from .presets import Scale
from .uri_registry import observe_published_layer

__all__ = [
    "publish_layer",
    "PublishLayerError",
    "derive_layer_id",
    "derive_readable_layer_name",
    "legend_for_published_layer",
    "pop_legend_for_uri",
    "resolve_layer_style",
]

logger = logging.getLogger("trid3nt_server.emission.publish")


# --------------------------------------------------------------------------- #
# Error class
# --------------------------------------------------------------------------- #


class PublishLayerError(RuntimeError):
    """Raised when ``publish_layer`` cannot complete the round-trip.

    The ``error_code`` attribute carries a SCREAMING_SNAKE_CASE code so the
    agent surface can render a useful failure narration and the pipeline strip
    shows ``UPSTREAM_API_ERROR``. ``retryable`` (contract; harvested
    by ``adapter._classify_error``) tells the model whether re-issuing the
    call with corrected args can succeed.

    Codes:
    - ``UNKNOWN_LAYER_HANDLE`` (retryable) - ``layer_uri`` is a
      bare placeholder token or fabricated scheme that no registry entry
      resolved; the message names the case's available handles so the model
      retries with one verbatim.
    - ``LAYER_URI_NOT_FOUND`` (retryable) - ``layer_uri`` is not an ``s3://``
      COG on this deployment; the model should re-issue with the producing
      tool's layer handle or its ``s3://`` URI verbatim.
    """

    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


# --------------------------------------------------------------------------- #
# The render chokepoint
#
# Two guards run FIRST because they are facts about the FILE rather than about
# the style, and each one is a way a ramp would CORRUPT an already-painted
# image: a COG carrying its own band-1 colour table (NLCD land cover) is
# coloured by that table, and an RGB(A) / multiband COG (a coloured relief, a
# landcover-plus-hillshade composite) is coloured already. Neither takes a
# preset - they are handed back as "already painted".
#
# Everything after that is the STYLE decision, and it is not made here: the
# producer DECLARED a style row and ``emission/presets.py`` resolves it,
# reading this raster's own range only when the declared policy asks for it.
# --------------------------------------------------------------------------- #

def _is_rgba_or_multiband(raster_bytes: bytes | None) -> bool:
    """True if the COG is RGB(A)/multiband - QGIS renders it DIRECTLY.

    Reads the in-hand COG bytes via a rasterio ``MemoryFile`` and reports True
    when band count >= 3 OR any band's color interpretation is one of
    Red/Green/Blue/Alpha. Such rasters (colored relief, blended landcover +
    hillshade composites) are already colorized: a single-band ramp would
    corrupt them, so the resolver hands them back unstyled. Best-effort:
    returns False on any read failure so a real single-band scalar still gets
    its range.
    """
    if not raster_bytes:
        return False
    try:
        import rasterio
        from rasterio.enums import ColorInterp
        from rasterio.io import MemoryFile
    except Exception as exc:  # noqa: BLE001 - deps unavailable: not RGBA
        logger.debug("rgba probe deps unavailable (%s: %s)", type(exc).__name__, exc)
        return False
    try:
        with MemoryFile(raster_bytes) as mem, mem.open() as src:
            if src.count >= 3:
                return True
            rgba = {
                ColorInterp.red,
                ColorInterp.green,
                ColorInterp.blue,
                ColorInterp.alpha,
            }
            return any(ci in rgba for ci in src.colorinterp)
    except Exception as exc:  # noqa: BLE001 - unreadable / not a raster
        logger.debug("rgba probe read failed (%s: %s)", type(exc).__name__, exc)
        return False


def _already_painted(raster_bytes: bytes | None) -> bool:
    """True when the COG carries its own colours and no preset may override them."""
    return _is_rgba_or_multiband(raster_bytes)


def resolve_layer_style(
    style: dict[str, Any] | None,
    layer_uri: str,
    *,
    override: "Scale | None" = None,
    shared: tuple[float, float] | None = None,
    raster_bytes: bytes | None = None,
    band_stats: tuple[float | None, float | None] | None = None,
) -> "presets.Resolved | None":
    """Resolve a DECLARED style row against this raster. The one resolution point.

    ``band_stats`` is the register-only fast path: a worker already computed the
    percentiles, so the range resolves without downloading the COG. ``None`` is
    returned for a raster that is already painted - an RGB(A) composite, or a
    COG carrying its own band-1 colour table.

    A VECTOR or MESH declaration resolves through this same call and the same
    ``presets.resolve``: it simply skips the raster probes, which are questions
    about a COG's bytes that a FlatGeobuf's features and a SELAFIN's dataset
    groups cannot answer. One seam, four kinds - never a second resolver per
    layer type.
    """
    preset = presets.from_row(style)
    if not presets.paints_a_raster(preset):
        resolved = presets.resolve(preset, override=override, shared=shared)
        logger.info("publish_layer (style) uri=%s -> %s", layer_uri,
                    resolved.legend_note())
        return resolved
    if raster_bytes is None and band_stats is None and presets.needs_run_range(
            preset, override):
        raster_bytes = _read_raster_bytes(layer_uri)
    if raster_bytes:
        try:
            from rasterio.io import MemoryFile

            with MemoryFile(raster_bytes) as mem, mem.open() as src:
                if _read_band1_colormap(src) is not None:
                    return None
        except Exception as exc:  # noqa: BLE001 - palette probe is best-effort
            logger.debug("palette probe skipped (%s: %s)", type(exc).__name__, exc)
    if _already_painted(raster_bytes):
        return None
    read_range = (presets.fixed_range_reader(*band_stats) if band_stats is not None
                  else presets.band_range_reader(raster_bytes))
    resolved = presets.resolve(preset, read_range=read_range, override=override,
                               shared=shared)
    logger.info("publish_layer (style) uri=%s -> %s", layer_uri,
                resolved.legend_note())
    return resolved


# --------------------------------------------------------------------------- #
# The resolved style, as the layer carries it
#
# The legend is built from the SAME resolution the .qml is written from, so the
# colourbar and the painted raster span identical numbers - there is no second
# range to drift. A raster that is already painted has no resolution to report:
# a paletted COG's legend comes from its own table, and an RGB(A) composite has
# no meaningful key at all.
#
# Fail-open: ANY failure here returns ``None`` so a publish is never blocked.
# --------------------------------------------------------------------------- #

#: Module-level side-table of the most-recent published-raster ``LegendKey``
#: keyed by the layer's ``s3://`` COG uri; the register-only manifest seam keys
#: by the same ``cog_uri``, so both producers share one key shape.
#: ``publish_layer`` returns a bare URI string, so the server wrap-site rebuilds
#: a ``LayerURI`` from it WITHOUT a legend; the pipeline emitter's
#: ``add_loaded_layer`` lifts the legend back out of this stash by ``layer.uri``.
#: Module scope is safe - the legend is a pure function of the
#: content-addressed COG plus the declared row. FIFO-bounded at the write site
#: so the always-on agent process never grows it without limit.
_MAX_LEGEND_ENTRIES: int = 256
_LAST_LEGEND_BY_URI: dict[str, Any] = {}


def _rgb_to_hex(entry: Any) -> str | None:
    """``(r, g, b[, a])`` 0-255 ints -> ``"#rrggbb"``; ``None`` on a bad entry."""
    try:
        r, g, b = int(entry[0]), int(entry[1]), int(entry[2])
    except (TypeError, ValueError, IndexError):
        return None
    if not all(0 <= c <= 255 for c in (r, g, b)):
        return None
    return f"#{r:02x}{g:02x}{b:02x}"


def _categorical_legend_from_colormap(
    cmap: dict, *, label: str | None = None
) -> "LegendKey | None":
    """Build a categorical ``LegendKey`` from a band-1 GDAL color table.

    ``cmap`` is ``{class_index: (r, g, b, a)}`` (the shape ``_read_band1_colormap``
    returns for NLCD + other paletted rasters). One ``LegendClass`` per MEANINGFUL
    entry, ordered by class index. GDAL always materializes the table to 256
    entries; indices the raster does not actually use come back as either fully
    transparent (``a == 0`` -- nodata / unused slots) OR the opaque-black filler
    default ``(0, 0, 0, 255)``. Both are dropped so the legend shows only the
    classes that meaningfully colorize pixels (a real NLCD table has ~16 distinct
    colors, not 256). Duplicate colors are collapsed to the first class index that
    carries them (paletted rasters never reuse a color for two real classes). The
    label is the class index rendered verbatim (this seam carries no code->name
    map). Returns ``None`` when nothing meaningful survives.
    """
    from trid3nt_contracts.execution import LegendClass, LegendKey

    classes: list[LegendClass] = []
    seen_colors: set[str] = set()
    for idx in sorted(cmap.keys()):
        entry = cmap[idx]
        # Drop fully-transparent slots (nodata / unused class codes).
        try:
            if len(entry) >= 4 and int(entry[3]) == 0:
                continue
        except (TypeError, ValueError):
            pass
        hex_color = _rgb_to_hex(entry)
        if hex_color is None:
            continue
        # Drop GDAL's opaque-black filler default for unset palette indices.
        if hex_color == "#000000":
            continue
        # Collapse duplicate colors (a paletted raster gives each real class a
        # distinct color; repeats are filler echoes).
        if hex_color in seen_colors:
            continue
        seen_colors.add(hex_color)
        classes.append(
            LegendClass(value=int(idx), color=hex_color, label=str(int(idx)))
        )
    if not classes:
        return None
    return LegendKey(kind="classed", classes=classes, label=label)


def legend_for_published_layer(
    style: dict[str, Any] | None,
    layer_uri: str,
    *,
    units: str | None = None,
    raster_bytes: bytes | None = None,
    override: "Scale | None" = None,
    shared: tuple[float, float] | None = None,
    band_stats: tuple[float | None, float | None] | None = None,
) -> "LegendKey | None":
    """The layer's resolved style, as the key the map renders from.

    The declared row is resolved ONCE here: the concrete range, the ramp and the
    .qml all come out of that one resolution. A raster that is already painted
    returns its own palette's classes (a paletted COG) or ``None`` (an RGB(A)
    composite, which has no meaningful key). A vector or mesh declaration takes
    the same route and never touches the object at all.

    Fail-open: ``None`` on any error, so a publish is never blocked.
    """
    from trid3nt_contracts.execution import LegendKey

    try:
        paints_raster = presets.paints_a_raster(presets.from_row(style))
        if paints_raster and raster_bytes is None and band_stats is None:
            raster_bytes = _read_raster_bytes(layer_uri)
        resolved = resolve_layer_style(
            style, layer_uri, override=override, shared=shared,
            raster_bytes=raster_bytes, band_stats=band_stats)
        if resolved is not None:
            preset = resolved.preset
            return LegendKey(
                kind=preset.kind,
                colormap=preset.ramp if preset.kind != "reference" else None,
                vmin=resolved.range[0] if resolved.range else None,
                vmax=resolved.range[1] if resolved.range else None,
                classes=_declared_legend_classes(preset) or None,
                units=preset.units or units,
                label=preset.label,
                qml=resolved.qml(),
            )
        # Already painted (a raster by construction - only the raster arm ever
        # declines to resolve): only a paletted one has a meaningful key.
        label = (style or {}).get("label")
        if raster_bytes is None:
            raster_bytes = _read_raster_bytes(layer_uri)
        if raster_bytes is None:
            return None
        try:
            from rasterio.io import MemoryFile

            with MemoryFile(raster_bytes) as mem, mem.open() as src:
                table = _read_band1_colormap(src)
        except Exception as exc:  # noqa: BLE001 - palette probe is best-effort
            logger.debug("legend palette probe skipped (%s: %s)",
                         type(exc).__name__, exc)
            return None
        if not table:
            return None
        return _categorical_legend_from_colormap(table, label=label)
    except Exception as exc:  # noqa: BLE001 - never block a publish on the legend
        logger.debug("legend_for_published_layer failed for %s (%s: %s)",
                     layer_uri, type(exc).__name__, exc)
        return None


def _declared_legend_classes(preset: "presets.Preset") -> list:
    """The preset's declared class breaks as legend swatches."""
    from trid3nt_contracts.execution import LegendClass

    return [LegendClass(value_min=lo, value_max=hi, color=color, label=label)
            for lo, hi, color, label in preset.classes]


def _stash_legend_for_uri(layer_uri: str, legend: "LegendKey | None") -> None:
    """Record (or clear) the published layer's ``LegendKey`` keyed by its uri.

    FIFO-bounded so the always-on agent process cannot grow this side-table
    without limit. A ``None`` legend clears any stale entry for this uri (so a
    re-publish that now resolves to no key cannot leave an orphaned one behind).
    """
    if not layer_uri:
        return
    if layer_uri in _LAST_LEGEND_BY_URI:
        del _LAST_LEGEND_BY_URI[layer_uri]
    if legend is None:
        return
    _LAST_LEGEND_BY_URI[layer_uri] = legend
    while len(_LAST_LEGEND_BY_URI) > _MAX_LEGEND_ENTRIES:
        _LAST_LEGEND_BY_URI.pop(next(iter(_LAST_LEGEND_BY_URI)))


def pop_legend_for_uri(layer_uri: str) -> "LegendKey | None":
    """Look up the stashed ``LegendKey`` for a published layer's uri.

    Non-destructive READ (a re-emit / replay of the SAME layer must resolve the
    same key). The pipeline emitter's ``add_loaded_layer`` calls this to lift the
    legend onto the ``ProjectLayerSummary`` for the publish_layer wrap-site path
    (where the rebuilt ``LayerURI`` carries no legend of its own). Returns
    ``None`` when nothing was stashed (a categorical-RGBA layer).
    """
    return _LAST_LEGEND_BY_URI.get(layer_uri)


# NOTE: QGIS-native rendering emits the raw ``s3://`` COG uri directly (see
# module docstring) - do not reintroduce an XYZ tile-template mint here.


# --------------------------------------------------------------------------- #
# Benign vector handling
# --------------------------------------------------------------------------- #

#: Vector artifact extensions. ``publish_layer`` is RASTER-ONLY (see the module
#: docstring). A vector reaching here is ALREADY a store object the plugin
#: opens natively, so a publish is unnecessary - and GDAL cannot open a
#: FlatGeobuf as a raster COG, so routing one through the raster path would
#: fail to open, not render. Token-tail matched against the resolved URI
#: basename.
_VECTOR_EXTS = (
    ".fgb",
    ".geojson",
    ".json",
    ".geoparquet",
    ".parquet",
    ".gpkg",
    ".shp",
)


def _is_vector_uri(layer_uri: str) -> bool:
    """True when ``layer_uri`` names a vector artifact (by extension)."""
    return layer_uri.lower().rstrip("/").endswith(_VECTOR_EXTS)


def _benign_vector_noop(layer_uri: str, layer_id: str) -> str:
    """Return a calm, NON-ERROR signal for a vector handed to publish_layer.

    A vector needs no publish: it is already an object in the store and the
    plugin opens it natively. So this neither raises (the step completes green)
    nor registers anything. The returned string is what the caller gets back -
    an honest "no publish needed" rather than a failure it must explain.
    """
    logger.info(
        "publish_layer: benign vector no-op for layer_id=%s uri=%s",
        layer_id,
        layer_uri,
    )
    return (
        f"noop: layer_id={layer_id!r} is a VECTOR ({layer_uri!r}); it is already "
        "an object in the store and the map reads it directly. publish_layer is "
        "raster-only; no publish was needed and none was performed. Do NOT "
        "re-call publish_layer for this vector layer."
    )


# --------------------------------------------------------------------------- #
# Overview enforcement (no-overview COGs render spotty / never paint)
# --------------------------------------------------------------------------- #


def _raster_has_overviews(raster_bytes: bytes) -> bool | None:
    """True/False if the in-memory raster has internal overviews; None if unknown.

    Reads the bytes through a rasterio ``MemoryFile`` and inspects
    ``overviews(1)``. A non-empty list = overviews present. ``None`` is
    returned when rasterio is unavailable or the open fails - callers treat
    ``None`` as "cannot determine" and fail-open (publish as-is, legacy
    behavior).
    """
    try:
        import rasterio
        from rasterio.io import MemoryFile
    except Exception as exc:  # noqa: BLE001 - rasterio not installed
        logger.warning(
            "publish_layer: rasterio unavailable (%s) - cannot verify COG "
            "overviews; publishing as-is",
            exc,
        )
        return None
    try:
        with MemoryFile(raster_bytes) as mem, mem.open() as src:
            return bool(src.overviews(1))
    except Exception as exc:  # noqa: BLE001 - unreadable / not a raster
        logger.warning(
            "publish_layer: could not inspect raster overviews (%s: %s) - "
            "publishing as-is",
            type(exc).__name__,
            exc,
        )
        return None


def _read_band1_colormap(src) -> dict | None:
    """Return the band-1 palette color table (``{idx: (r,g,b,a)}``) or ``None``.

    NLCD land cover (and other categorical rasters) ship a single-band
    palette-index COG with an EMBEDDED GDAL color table; QGIS colorizes from
    it. The overview-enforcement re-write must carry that table forward or
    the layer renders solid grey. rasterio raises ``ValueError`` when
    band 1 has no color table - the normal case for continuous rasters (DEM,
    hillshade, flood depth) - and we return ``None`` so callers do NOT fabricate
    one.
    """
    try:
        return src.colormap(1)
    except ValueError:
        return None
    except Exception as exc:  # noqa: BLE001 - any other read failure: no-op
        logger.debug("colormap read skipped (%s: %s)", type(exc).__name__, exc)
        return None


def _apply_band1_colormap(dst, cmap: dict | None) -> None:
    """Stamp a preserved band-1 color table + palette colorinterp onto ``dst``.

    No-op when ``cmap`` is ``None`` (non-paletted raster - never fabricate a
    color table). Otherwise writes the table on band 1 and marks band 1's color
    interpretation ``palette`` so QGIS treats the integer pixels as indices.
    """
    if cmap is None:
        return
    try:
        dst.write_colormap(1, cmap)
        try:
            from rasterio.enums import ColorInterp

            interp = list(dst.colorinterp)
            interp[0] = ColorInterp.palette
            dst.colorinterp = tuple(interp)
        except Exception:  # noqa: BLE001 - colorinterp set is best-effort
            pass
    except Exception as exc:  # noqa: BLE001 - colormap copy is best-effort
        logger.warning(
            "publish_layer: colormap preservation failed (%s: %s); land-cover "
            "output may render grey",
            type(exc).__name__,
            exc,
        )


def _build_cog_with_overviews(raster_bytes: bytes) -> bytes | None:
    """Translate flat raster bytes into a tiled COG WITH overviews.

    Two paths. The COG-driver encode (``emission/cog.translate_to_cog``) tiles and
    builds overviews in one pass; it degrades to the flat input bytes rather than
    raising, so the result is CHECKED for overviews before it is trusted. The
    rasterio fallback (``rio-cogeo`` if present, else a tiled-profile copy plus
    ``build_overviews``) covers whatever the first path could not encode.

    Returns the new COG bytes, or ``None`` when no path could produce a real
    overview-bearing COG (caller then fails-open and publishes the original).
    """
    in_tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as in_f:
            in_tmp = in_f.name
            in_f.write(raster_bytes)
        try:
            cog_bytes = translate_to_cog(in_tmp)
            if _raster_has_overviews(cog_bytes):
                return cog_bytes
            logger.info(
                "publish_layer: the COG encode produced no overviews - trying the "
                "rasterio fallback")
        except Exception as exc:  # noqa: BLE001 - encode unavailable / failed
            logger.info(
                "publish_layer: the COG encode path is unavailable (%s: %s) - "
                "trying the rasterio fallback", type(exc).__name__, exc)
    finally:
        if in_tmp is not None:
            try:
                os.unlink(in_tmp)
            except OSError:
                pass

    # 2. rasterio fallback (rio-cogeo preferred; manual overview build else).
    try:
        return _build_cog_with_overviews_rasterio(raster_bytes)
    except Exception as exc:  # noqa: BLE001 - fallback failed; fail-open upstream
        logger.warning(
            "publish_layer: rasterio COG/overview rebuild failed (%s: %s) - "
            "publishing original (no-overview) raster as-is",
            type(exc).__name__,
            exc,
        )
        return None


def _build_cog_with_overviews_rasterio(raster_bytes: bytes) -> bytes | None:
    """rasterio-only COG+overview rebuild (no GDAL CLI required)."""
    import rasterio
    from rasterio.io import MemoryFile

    # Detect a band-1 palette color table up front. When present (NLCD land
    # cover), SKIP the rio-cogeo path - its colormap forwarding is
    # version-dependent - and fall through to the manual build below, which
    # explicitly re-stamps the table. Non-paletted rasters keep the
    # rio-cogeo fast path unchanged.
    with MemoryFile(raster_bytes) as probe_mem, probe_mem.open() as probe:
        has_colormap = _read_band1_colormap(probe) is not None

    # rio-cogeo is the cleanest path when installed (and the source is not a
    # palette raster whose color table we must guarantee).
    if not has_colormap:
        try:
            from rio_cogeo.cogeo import cog_translate
            from rio_cogeo.profiles import cog_profiles

            with MemoryFile(raster_bytes) as src_mem, src_mem.open() as src:
                dst_profile = cog_profiles.get("deflate")
                with MemoryFile() as dst_mem:
                    cog_translate(
                        src,
                        dst_mem.name,
                        dst_profile,
                        in_memory=True,
                        quiet=True,
                    )
                    out = dst_mem.read()
            if _raster_has_overviews(out):
                return out
        except Exception:  # noqa: BLE001 - rio-cogeo absent / failed; manual below
            logger.debug(
                "rio-cogeo path unavailable; manual overview build", exc_info=True
            )

    # Manual: copy into a tiled GTiff then build overviews in place.
    from rasterio.enums import Resampling

    with MemoryFile(raster_bytes) as src_mem, src_mem.open() as src:
        profile = src.profile.copy()
        profile.update(tiled=True, blockxsize=512, blockysize=512, compress="deflate")
        data = src.read()
        # Preserve a band-1 palette color table (e.g. NLCD land cover) across
        # the overview-enforcement re-write. None for non-paletted rasters
        # (DEM/hillshade/flood depth) - a pure no-op there.
        cmap = _read_band1_colormap(src)
        # Palette rasters must downsample by NEAREST, never average - averaging
        # class indices produces meaningless in-between codes that map to wrong
        # colors. Continuous rasters keep average.
        overview_resampling = Resampling.nearest if cmap else Resampling.average
        with MemoryFile() as dst_mem:
            with dst_mem.open(**profile) as dst:
                dst.write(data)
                _apply_band1_colormap(dst, cmap)
                factors = _overview_factors(src.width, src.height)
                if factors:
                    dst.build_overviews(factors, overview_resampling)
                    dst.update_tags(
                        ns="rio_overview", resampling=overview_resampling.name
                    )
            out = dst_mem.read()
    return out if _raster_has_overviews(out) else None


def _overview_factors(width: int, height: int) -> list[int]:
    """Power-of-two decimation factors down to a ~256px overview.

    For small rasters (max dimension < 512px) the 256px floor alone would
    produce an empty list, and QGIS then computes minzoom == maxzoom for a
    tiny overview-free COG and renders nothing at the default CONUS zoom, so
    this always includes at least factor=2 even when the 256px floor is
    never met. A single factor-2 overview (64-75px) is sufficient for QGIS
    to lower its minzoom and overzoom the tiles at any zoom level.
    """
    factors: list[int] = []
    factor = 2
    while max(width, height) // factor >= 256:
        factors.append(factor)
        factor *= 2
        if len(factors) >= 8:  # safety cap
            break
    # Always add factor=2 even when the image is already smaller than 512px so
    # QGIS gets at least one overview level for tiny rasters.
    if not factors:
        factors = [2]
    return factors


def _read_raster_bytes(layer_uri: str) -> bytes | None:
    """Read raster bytes for an ``s3://`` / local URI (None on failure).

    Used by the overview check. Fail-open: any read error returns ``None``
    so the publish proceeds with the original URI.
    """
    try:
        if layer_uri.startswith("s3://"):
            from trid3nt_server.tools.cache import read_object_bytes_s3

            return read_object_bytes_s3(layer_uri)
        # local path (dev/test convenience)
        with open(layer_uri, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning(
            "publish_layer: could not read raster bytes for overview check "
            "(%s: %s) - publishing as-is",
            type(exc).__name__,
            exc,
        )
        return None


def _split_s3_uri(uri: str) -> tuple[str, str] | None:
    """``(bucket, key)`` for an ``s3://`` URI, or ``None`` when it is not one.

    The solver's ``_split_object_uri`` RAISES on anything that is not an object
    URI; this is the fail-open caller's shape, because a local path here is a
    legal input rather than a fault.
    """
    from trid3nt_server.workflows.solver.solver import (
        SolverDispatchError,
        _split_object_uri,
    )

    try:
        _scheme, bucket, key = _split_object_uri(uri)
    except SolverDispatchError:
        return None
    return (bucket, key) if bucket and key else None


def _write_overview_cog(layer_uri: str, cog_bytes: bytes) -> str | None:
    """Write the auto-translated COG alongside the source; return its URI (None on fail).

    A fresh ULID-suffixed sibling object so the original (no-overview) COG is
    never mutated in place and warm negative-caches don't poison the new path.
    Fail-open: returns ``None`` on any write error (caller publishes original).
    """
    parsed_s3 = _split_s3_uri(layer_uri)
    try:
        if layer_uri.startswith("s3://") and parsed_s3 is not None:
            from trid3nt_server.workflows.solver.solver import _get_s3_client

            bucket, key = parsed_s3
            dir_prefix = key.rsplit("/", 1)[0] + "/" if "/" in key else ""
            new_key = f"{dir_prefix}overviews/{new_ulid()}.tif"
            s3 = _get_s3_client()
            s3.put_object(
                Bucket=bucket, Key=new_key, Body=cog_bytes, ContentType="image/tiff"
            )
            return f"s3://{bucket}/{new_key}"
        # local path: write a sibling file.
        base, _ext = os.path.splitext(layer_uri)
        new_path = f"{base}.ovr-{new_ulid()}.tif"
        with open(new_path, "wb") as f:
            f.write(cog_bytes)
        return new_path
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning(
            "publish_layer: could not write auto-translated overview COG "
            "(%s: %s) - publishing original raster as-is",
            type(exc).__name__,
            exc,
        )
        return None


def _ensure_raster_has_overviews(layer_uri: str) -> str:
    """Guarantee the published raster is a COG WITH overviews.

    A no-overview COG renders SPOTTY (per-strip range requests time out cold;
    QGIS can't downsample for low zooms), so before a raster is registered,
    validate the source COG has overviews. When missing, auto-translate to a
    tiled+overview COG (reusing ``emission.cog.translate_to_cog``, with a
    rasterio fallback), write it to a fresh sibling object, log the
    auto-translate, and publish THAT instead.

    Fail-open at every step: an unreadable raster, a missing rasterio, a failed
    translate, or a failed write all degrade to returning ``layer_uri``
    unchanged (never blocks a publish).
    """
    raster_bytes = _read_raster_bytes(layer_uri)
    if raster_bytes is None:
        return layer_uri

    has_ovr = _raster_has_overviews(raster_bytes)
    if has_ovr is not False:
        # True (overviews present) or None (cannot determine) → publish as-is.
        return layer_uri

    logger.warning(
        "publish_layer: raster %s has NO overviews - a no-overview COG renders "
        "spotty / times out cold; auto-translating to a tiled COG with "
        "overviews before publishing (F33)",
        layer_uri,
    )
    cog_bytes = _build_cog_with_overviews(raster_bytes)
    if cog_bytes is None:
        return layer_uri

    new_uri = _write_overview_cog(layer_uri, cog_bytes)
    if new_uri is None:
        return layer_uri

    logger.warning(
        "publish_layer: F33 auto-translate complete - publishing overview COG "
        "%s in place of no-overview source %s",
        new_uri,
        layer_uri,
    )
    return new_uri


#: URI schemes ``publish_layer`` can actually consume. Anything scheme-shaped
#: outside this set (e.g. a fabricated ``qgis://project1``) or a bare token
#: with no scheme/path shape at all (e.g. ``'LayerURI_from_previous_step'``)
#: is an UNRESOLVED HANDLE: a real handle would have been substituted with its
#: registered URI by ``uri_registry.resolve_params`` before dispatch.
_CONSUMABLE_URI_SCHEMES = ("s3://", "gs://", "http://", "https://", "file://")


def _looks_like_unresolved_handle(layer_uri: str) -> bool:
    """True when ``layer_uri`` cannot be a consumable URI or filesystem path.

    Small models sometimes call a publish in the SAME iteration as the
    producing tool with literal placeholders ('LayerURI_from_previous_step')
    or invented pseudo-URIs ('qgis://project1'). Those fail deep in the
    publish path with an unhelpful GDAL/storage error. This predicate gates
    them at the door so the caller gets a typed error that NAMES the
    actually available handles instead. The class is rarer now that no
    model calls a publish tool, but the guard still covers a composer
    handing on an unresolved handle.

    Conservative by construction -- everything a valid caller passes today is
    accepted: registered handles are already resolved to real URIs before this
    runs; composers pass ``s3://``/``gs://``/tile-template URLs; ``/vsi*`` GDAL
    paths and absolute filesystem paths pass through.
    """
    v = (layer_uri or "").strip()
    if not v:
        return True
    # Angle brackets and literal ellipses are never valid in a real URI -
    # they are template-placeholder shapes, e.g.
    # 'gs://<result-fetched_usgs_earthquakes-uri>' and a fabricated
    # 's3://.../earthquakes_layer.fgb' that slipped past a scheme allowlist
    # and hit the benign vector no-op, minting a success-shaped "Layer
    # published" for a URI that was never real. Tile-template braces
    # ({z}/{x}/{y}) remain VALID input for the legacy tile-template unwrap
    # branch (old persisted cases), so braces are NOT placeholder markers.
    if "<" in v or ">" in v or "..." in v:
        return True
    if v.startswith("/vsi") or v.startswith("/") or v.startswith("\\"):
        return False  # GDAL virtual path / absolute filesystem path
    if any(v.startswith(scheme) for scheme in _CONSUMABLE_URI_SCHEMES):
        return False
    return True  # bare token (placeholder/handle) or unknown scheme


def _unknown_handle_error(layer_uri: str) -> "PublishLayerError":
    """Typed, retryable unknown-handle error naming the available handles."""
    from trid3nt_server.emission.uri_registry import ambient_layer_handle_inventory

    handles = ambient_layer_handle_inventory(limit=8)
    if handles:
        inventory = (
            "available handles in this case: "
            + ", ".join(repr(h) for h in handles)
            + "; pass one verbatim"
        )
    else:
        inventory = (
            "no layers have been produced in this case yet - run a fetch or "
            "composer tool first"
        )
    return PublishLayerError(
        "UNKNOWN_LAYER_HANDLE",
        f"unknown layer handle {layer_uri!r}; {inventory}, or skip "
        f"publish_layer entirely - fetch and composer tools auto-publish "
        f"their own results.",
        retryable=True,
    )


def derive_layer_id(layer_uri: str, registry: Any | None = None) -> str:
    """Derive a stable ``layer_id`` when the caller omitted one.

    Local 8B models omit ``publish_layer``'s ``layer_id`` entirely.
    Derivation order:

    1. the registered layer handle whose URI equals the (already
       server-resolved) ``layer_uri`` - i.e. the producing tool's own
       ``layer_id`` (``uri_registry.lookup_handle_for_uri``; uses the ambient
       dispatch registry when ``registry`` is not passed);
    2. the URI basename stem, sanitized to ``[A-Za-z0-9_-]`` (QGIS layer name
       + WMS ``LAYERS=`` safe);
    3. a fresh ``layer-<ulid>`` when the stem is empty.
    """
    import re as _re
    from urllib.parse import urlparse as _urlparse

    from trid3nt_server.emission.uri_registry import lookup_handle_for_uri

    handle = lookup_handle_for_uri(layer_uri, registry)
    if handle:
        return handle
    path = _urlparse(layer_uri).path if "://" in layer_uri else layer_uri
    base = path.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    slug = _re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")
    if slug:
        return slug
    return f"layer-{new_ulid()}"


def _looks_like_ulid(value: str) -> bool:
    """True for a 26-char Crockford-base32 ULID shape (case-insensitive).

    Matches ``new_ulid()``'s output shape without importing the ``ulid``
    package here -- a cheap regex is enough to recognize "this is not a
    human name, it's an identifier" for the name-derivation guard.
    """
    import re as _re

    return bool(_re.match(r"^[0-9A-HJKMNP-TV-Z]{26}$", value, _re.IGNORECASE))


def _looks_like_hash_or_id(value: str) -> bool:
    """True for a bare ULID, or a long hex/opaque cache-key-shaped token.

    Used to skip non-human URI path segments (e.g. a cache-key filename
    stem like ``a1b2c3d4e5f6...tif``) when deriving a name from the URI.
    """
    import re as _re

    if _looks_like_ulid(value):
        return True
    return bool(_re.match(r"^[0-9a-f]{12,64}$", value, _re.IGNORECASE))


def _label_from_uri(layer_uri: str) -> str | None:
    """Human label from a source ``layer_uri`` path segment, or ``None``.

    Prefers the PARENT directory segment (e.g. ``.../hillshade/<hash>.tif``
    -> ``"hillshade"``) since the file stem is typically a cache hash or a
    bare ULID and not human-meaningful; falls back to the file stem itself
    when it IS human-shaped (no parent segment, or the parent is also
    opaque).
    """
    from urllib.parse import urlparse as _urlparse

    path = _urlparse(layer_uri).path if "://" in layer_uri else layer_uri
    segments = [s for s in path.split("/") if s]
    if not segments:
        return None
    stem = segments[-1].rsplit(".", 1)[0] if "." in segments[-1] else segments[-1]
    candidates = ([segments[-2]] if len(segments) >= 2 else []) + [stem]
    for cand in candidates:
        if not cand or _looks_like_hash_or_id(cand):
            continue
        cleaned = cand.replace("_", " ").replace("-", " ").strip()
        if cleaned:
            return cleaned.title()
    return None


def _short_disambiguator(layer_id: str) -> str:
    """Short suffix (last 4 alnum chars of ``layer_id``, else today's MMDD)
    so two derived names for the same family/preset don't collide in the
    UI's layer list."""
    import re as _re

    tail = _re.sub(r"[^A-Za-z0-9]", "", layer_id or "")[-4:]
    if tail:
        return tail.upper()
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%m%d")


def derive_readable_layer_name(
    name: str | None,
    layer_id: str,
    style: dict[str, Any] | None,
    layer_uri: str,
) -> str:
    """Derive a human-readable layer name for the UI's layer list.

    Local 8B models routinely omit ``publish_layer``'s ``name``, and when
    ``layer_id`` ALSO degrades to a bare ULID (``derive_layer_id``'s last
    resort), the published layer would show up in the UI as e.g.
    ``'01KX5TEZ20BK86EE6DG8PSVFJK'`` -- meaningless to the user. Precedence:

    1. an explicit, non-empty ``name`` that is not ITSELF a bare-ULID shape
       -- returned VERBATIM, no disambiguator appended (the caller already
       chose it deliberately; second-guessing it would be surprising).
    2. the declared style row's own ``label``.
    3. a human segment of the source ``layer_uri`` path (the parent
       directory / product-family segment -- the file stem is typically a
       cache hash or a ULID).
    4. a generic ``"Layer"`` fallback.

    Cases 2-4 append a short disambiguator (``_short_disambiguator``) so two
    derived names for the same family don't collide in the UI list.
    INVARIANT: a bare-ULID name must never reach the layer summary when any
    better signal (an explicit name, a declared label, or a URI segment) is
    available.
    """
    if name and name.strip() and not _looks_like_ulid(name.strip()):
        return name.strip()

    label = (style or {}).get("label") or _label_from_uri(layer_uri)
    if not label:
        label = "Layer"
    return f"{label} {_short_disambiguator(layer_id)}"


# --------------------------------------------------------------------------- #
# The mechanism
# --------------------------------------------------------------------------- #

def publish_layer(
    layer_uri: str,
    layer_id: str | None = None,
    style: dict[str, Any] | None = None,
    name: str | None = None,
    #: A declared SPECIALIZATION of the contract's scale for this one layer -
    #: a param knob, or `restyle_layer`. Absent means the contract default,
    #: which is what nearly every publish wants.
    scale: "ScaleSpec | None" = None,
    #: One range shared across a COMPARED set, so before/after and
    #: coarse-versus-refined are painted against each other rather than each
    #: against itself.
    shared_range: tuple[float, float] | None = None,
    # Absorb extra keywords: callers are ~30 composers plus the emission
    # seam, and a new keyword on one of them must not break the other
    # twenty-nine.
    **_extra_ignored: Any,
) -> str:
    """Publish a COG raster: write, register, notify.

    Enforces COG overviews, registers the layer handle against the ONE uri it
    carries, resolves the declared style row into a legend the envelope hands
    the map, and returns that ``s3://`` COG uri - the QGIS plugin opens it
    natively through GDAL ``/vsis3``. Vectors are a benign no-op: they are
    already store objects the plugin opens the same way.

    Called by the emission seam for every renderable raster a tool returns
    (``layer_uri_emit.publish_for_emission``), by the solver outputs seam, and
    by the composers that publish a product layer directly. It is NOT a
    registered tool and there is no model-facing intent that reaches it.

    Args:
        layer_uri: the ``s3://`` COG URI, or a registered layer handle the
            caller has already resolved. A bare unresolved handle raises.
        layer_id: stable, Case-unique id for the published layer. Derived from
            the registered producer id / the URI basename / a fresh
            ``layer-<ulid>`` when omitted.
        style: the DECLARED style row - which of the four preset shapes draws
            this layer and the parameters that shape needs. ``None`` takes the
            continuous kind's bare default.
        name: display name for the layer list. Derived from the preset label /
            a URI path segment when omitted, so a bare ULID never reaches the
            layer summary.

    Returns:
        The published raster's ``s3://`` COG URI (the overview-enforced sibling
        when one had to be built). Suitable as a ``LayerURI.uri``.

    Raises:
        PublishLayerError: unknown layer handle, or a non-``s3://`` raster URI.
            ``error_code`` carries a SCREAMING_SNAKE_CASE code.
    """
    # Unknown/placeholder handle guard. A registered
    # handle was already substituted with its real URI by the server's
    # ``uri_registry.resolve_params`` seam before this body runs, so a bare
    # token ('LayerURI_from_previous_step') or a fabricated scheme
    # ('qgis://project1') reaching this point can NEVER publish. Fail at the
    # door with a typed, retryable error that names the case's actually
    # available handles so a small model self-corrects instead of spiraling.
    if _looks_like_unresolved_handle(layer_uri):
        raise _unknown_handle_error(layer_uri)

    # Small-model resilience: layer_id is optional. Local 8B models call
    # publish_layer without it (otherwise a TypeError: missing 1 required
    # positional argument: 'layer_id'). The server dispatch seam injects the
    # same derived id into params so the wrap-site emission still fires; this
    # in-tool derivation covers direct/programmatic callers.
    if not layer_id:
        layer_id = derive_layer_id(layer_uri)
        logger.info(
            "publish_layer: layer_id omitted - derived %r from layer_uri=%s",
            layer_id,
            layer_uri,
        )

    # ``name`` is a transport-only carrier (see docstring) - the
    # actual LayerURI.name the client renders is computed by the server-side
    # wrap-site's ``derive_readable_layer_name`` call (it has the resolved
    # published URI this function's caller does not see yet).
    # Logged here purely for observability of what the model actually sent.
    if name:
        logger.info("publish_layer: name=%r layer_id=%r", name, layer_id)

    # A vector needs no publish: it is already a store object the plugin opens
    # natively, and GDAL cannot open a FlatGeobuf as a raster COG. Return a
    # BENIGN, non-error result so the step completes GREEN and the agent
    # narrates honestly instead of re-calling.
    if _is_vector_uri(layer_uri):
        return _benign_vector_noop(layer_uri, layer_id)
    if not layer_uri.startswith("s3://"):
        raise PublishLayerError(
            "LAYER_URI_NOT_FOUND",
            f"layer_uri {layer_uri!r} is not an s3:// COG in this store. "
            "Pass the producing tool's layer handle or its s3:// URI verbatim.",
            retryable=True,
        )
    # A no-overview COG renders SPOTTY (per-strip range requests time out
    # cold; QGIS can't downsample for low zooms), so validate the COG has
    # overviews and auto-translate to a tiled+overview COG before the
    # raster is registered. Fail-open (publishes as-is) on any error.
    layer_uri = _ensure_raster_has_overviews(layer_uri)

    # The DECLARED style row, resolved against this raster ONCE: the concrete
    # range, the ramp and the .qml the map loads all come out of that one
    # resolution, and the result is stashed keyed by the s3:// uri this call
    # returns. publish_layer returns a bare URI string, so the server wrap-site
    # rebuilds a LayerURI WITHOUT a legend; the pipeline emitter's
    # add_loaded_layer lifts it back out of the stash by layer.uri.
    # Fail-open: a None legend clears the stash entry.
    try:
        _stash_legend_for_uri(layer_uri, legend_for_published_layer(
            style, layer_uri, override=scale, shared=shared_range))
    except Exception as exc:  # noqa: BLE001 - legend never blocks a publish
        logger.debug("publish_layer legend build skipped (%s: %s)",
                     type(exc).__name__, exc)
    logger.info("publish_layer layer_id=%s uri=%s", layer_id, layer_uri)
    # Register the layer so the ``flood-depth-peak-<id>``-style handle resolves
    # for downstream tools (Pelicun, zonal stats). One scheme, one face: the
    # s3:// COG is both the data uri and the uri the plugin renders.
    observe_published_layer(layer_id, uri=layer_uri)
    return layer_uri
