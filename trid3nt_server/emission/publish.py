"""The raster PUBLISH mechanism - write, register, notify.

One store, one scheme: a raster lives as a COG at ``s3://<bucket>/<key>`` that
the QGIS plugin reads through GDAL ``/vsis3``, so a publish never mints a second
face for it. Vectors are a benign no-op, and every probe here fails OPEN.
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
    "derive_readable_layer_name",
    "legend_for_published_layer",
    "pop_legend_for_uri",
    "resolve_layer_style",
]

logger = logging.getLogger("trid3nt_server.emission.publish")




class PublishLayerError(RuntimeError):
    """Raised when ``publish_layer`` cannot complete the round-trip.
    ``error_code`` is a SCREAMING_SNAKE_CASE code; ``retryable`` says whether
    re-issuing the call with corrected arguments can succeed.
    """

    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


# The render chokepoint
#
# Two guards run FIRST because they are facts about the FILE rather than about
# the style, and each one is a way a ramp would CORRUPT an already-painted
# image: a COG carrying its own band-1 colour table is coloured by that table,
# and an RGB(A) / multiband COG is coloured already. Neither takes a preset -
# they are handed back as "already painted".

def _is_rgba_or_multiband(raster_bytes: bytes | None) -> bool:
    """True if the COG is RGB(A)/multiband - QGIS renders it DIRECTLY.

    A read failure returns False, so a single-band scalar still gets its range.
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

    ``None`` for an already-painted raster; ``band_stats`` skips the COG read.
    """
    preset = presets.from_row(style)
    # A vector or mesh declaration takes this same call and the same resolve: it
    # skips the raster probes, which are questions about a COG's bytes that a
    # FlatGeobuf's features and a SELAFIN's dataset groups cannot answer. One
    # seam, four kinds - never a second resolver per layer type.
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


# The resolved style, as the layer carries it
#
# The legend is built from the SAME resolution the .qml is written from, so the
# colourbar and the painted raster span identical numbers - there is no second
# range to drift. A raster that is already painted has no resolution to report:
# a paletted COG's legend comes from its own table, and an RGB(A) composite has
# no meaningful key at all.
#
# Fail-open: ANY failure here returns ``None`` so a publish is never blocked.

#: The most-recent published-raster ``LegendKey`` keyed by the layer's ``s3://``
#: COG uri. The legend travels by URI rather than on the layer row, and the
#: register-only manifest seam keys by the same uri, so both producers share one
#: key shape. Module scope is safe - the legend is a pure function of the
#: content-addressed COG plus the declared row. FIFO-bounded at the write site so
#: the always-on agent process never grows it without limit.
_MAX_LEGEND_ENTRIES: int = 256
_LAST_LEGEND_BY_URI: dict[str, Any] = {}


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
    The declared row is resolved ONCE: the range, the ramp and the .qml all come
    out of that one resolution. ``None`` on an already-painted raster or any error.
    """
    from trid3nt_contracts.execution import LegendKey

    try:
        paints_raster = presets.paints_a_raster(presets.from_row(style))
        if paints_raster and raster_bytes is None and band_stats is None:
            raster_bytes = _read_raster_bytes(layer_uri)
        resolved = resolve_layer_style(
            style, layer_uri, override=override, shared=shared,
            raster_bytes=raster_bytes, band_stats=band_stats)
        if resolved is None:
            return None
        preset = resolved.preset
        return LegendKey(
            kind=preset.kind,
            colormap=preset.ramp if preset.kind != "reference" else None,
            vmin=resolved.range[0] if resolved.range else None,
            vmax=resolved.range[1] if resolved.range else None,
            units=preset.units or units,
            label=preset.label,
            qml=resolved.qml(),
        )
    except Exception as exc:  # noqa: BLE001 - never block a publish on the legend
        logger.debug("legend_for_published_layer failed for %s (%s: %s)",
                     layer_uri, type(exc).__name__, exc)
        return None


def _stash_legend_for_uri(layer_uri: str, legend: "LegendKey | None") -> None:
    """Record (or clear) the published layer's ``LegendKey`` keyed by its uri.
    A ``None`` legend CLEARS the entry, so a re-publish that now resolves to no
    key cannot leave an orphaned one behind.
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

    A non-destructive read: a re-emit of the SAME layer must resolve the same key.
    """
    return _LAST_LEGEND_BY_URI.get(layer_uri)


# The raw ``s3://`` COG uri IS what the client renders: do not reintroduce an
# XYZ tile-template mint here.



#: Vector artifact extensions. ``publish_layer`` is RASTER-ONLY: a vector
#: reaching it is already a store object the plugin opens natively, and GDAL
#: cannot open a FlatGeobuf as a raster COG, so routing one through the raster
#: path would fail to open rather than render. Token-tail matched against the
#: resolved URI basename.
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

    Neither raises nor registers anything: a vector needs no publish at all.
    """
    logger.info(
        "publish_layer: benign vector no-op for layer_id=%s uri=%s",
        layer_id,
        layer_uri,
    )
    return (
        f"noop: layer_id={layer_id!r} is a VECTOR ({layer_uri!r}); it is already "
        "an object in the store and the map reads it directly, so no publish "
        "was needed and none was performed."
    )


# Overview enforcement (no-overview COGs render spotty / never paint)


def _raster_has_overviews(raster_bytes: bytes) -> bool | None:
    """True/False if the in-memory raster has internal overviews; None if unknown.
    ``None`` means CANNOT DETERMINE - rasterio absent, or the open failed - and
    every caller fails open on it and publishes the raster as-is.
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
    ``None`` when band 1 carries no table - the normal case for a continuous
    raster - so that no caller fabricates one.
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

    A ``None`` ``cmap`` is a no-op: a colour table is never fabricated.
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
    ``None`` when no path produced a real overview-bearing COG; the COG encode
    degrades to flat bytes rather than raising, so its result is checked first.
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

    Never empty: a raster too small for the 256px floor still gets factor 2.
    """
    factors: list[int] = []
    factor = 2
    while max(width, height) // factor >= 256:
        factors.append(factor)
        factor *= 2
        if len(factors) >= 8:  # safety cap
            break
    # Always add factor=2 even when the image is already smaller than 512px: with
    # no overview level at all QGIS computes minzoom == maxzoom for a tiny COG and
    # renders nothing at a continental zoom. One factor-2 level is enough for it
    # to lower its minzoom and overzoom the tiles.
    if not factors:
        factors = [2]
    return factors


def _read_raster_bytes(layer_uri: str) -> bytes | None:
    """Read raster bytes for an ``s3://`` / local URI (None on failure).

    Fail-open: any read error returns ``None`` and the publish proceeds.
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

    A local path is a legal input here rather than a fault, so this never raises.
    """
    from trid3nt_server import storage

    try:
        _scheme, bucket, key = storage.split_object_uri(uri)
    except storage.StorageError:
        return None
    return (bucket, key) if bucket and key else None


def _write_overview_cog(layer_uri: str, cog_bytes: bytes) -> str | None:
    """Write the auto-translated COG alongside the source; ``None`` on failure.
    A fresh ULID-suffixed sibling: the original COG is never mutated in place
    and a warm negative cache cannot poison the new object.
    """
    parsed_s3 = _split_s3_uri(layer_uri)
    try:
        if layer_uri.startswith("s3://") and parsed_s3 is not None:
            from trid3nt_server import storage

            bucket, key = parsed_s3
            dir_prefix = key.rsplit("/", 1)[0] + "/" if "/" in key else ""
            new_key = f"{dir_prefix}overviews/{new_ulid()}.tif"
            s3 = storage.client()
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
    Fail-open at every step: an unreadable raster, a failed translate or a failed
    write all return ``layer_uri`` unchanged rather than blocking the publish.
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


def _looks_like_ulid(value: str) -> bool:
    """True for a 26-char Crockford-base32 ULID shape (case-insensitive).

    Shape only: enough to tell an identifier from a human name.
    """
    import re as _re

    return bool(_re.match(r"^[0-9A-HJKMNP-TV-Z]{26}$", value, _re.IGNORECASE))


def _looks_like_hash_or_id(value: str) -> bool:
    """True for a bare ULID, or a long hex/opaque cache-key-shaped token.

    The test for a URI segment that is not worth showing a reader.
    """
    import re as _re

    if _looks_like_ulid(value):
        return True
    return bool(_re.match(r"^[0-9a-f]{12,64}$", value, _re.IGNORECASE))


def _label_from_uri(layer_uri: str) -> str | None:
    """Human label from a source ``layer_uri`` path segment, or ``None``.

    The PARENT segment wins over the file stem, which is usually a cache hash.
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
    """Short suffix: the last 4 alnum chars of ``layer_id``, else today's MMDD.

    So two derived names for the same family do not collide in the layer list.
    """
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
    """Derive a human-readable layer name for the layer list.

    An explicit non-ULID ``name`` returns verbatim; a derived one is disambiguated.
    """
    # Precedence: an explicit non-ULID name, then the declared row's label, then a
    # human segment of the source uri, then "Layer". A bare ULID must never reach
    # the layer summary while any of the later signals is available.
    if name and name.strip() and not _looks_like_ulid(name.strip()):
        return name.strip()

    label = (style or {}).get("label") or _label_from_uri(layer_uri)
    if not label:
        label = "Layer"
    return f"{label} {_short_disambiguator(layer_id)}"



def publish_layer(
    layer_uri: str,
    layer_id: str,
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
    # Absorb extra keywords: a new keyword on one caller must not break the rest.
    **_extra_ignored: Any,
) -> str:
    """Publish a COG raster: write, register, notify.
    Returns the ``s3://`` COG uri the client renders - the overview-enforced
    sibling when one had to be built. A vector is a benign no-op, not an error.
    """
    # ``name`` is transport-only: the name the client renders is derived later,
    # where the published uri this call has not returned yet is in hand. Logged
    # here only to show what the caller actually sent.
    if name:
        logger.info("publish_layer: name=%r layer_id=%r", name, layer_id)

    # A vector needs no publish: it is already a store object the client opens
    # natively, and GDAL cannot open a FlatGeobuf as a raster COG. The result is
    # BENIGN and non-error, so the step completes green rather than re-calling.
    if _is_vector_uri(layer_uri):
        return _benign_vector_noop(layer_uri, layer_id)
    if not layer_uri.startswith("s3://"):
        raise PublishLayerError(
            "LAYER_URI_NOT_FOUND",
            f"layer_uri {layer_uri!r} is not an s3:// COG in this store. "
            "Pass the producing tool's layer handle or its s3:// URI verbatim.",
            retryable=True,
        )
    # A no-overview COG renders spotty: per-strip range requests time out cold
    # and nothing can downsample it for a low zoom. Enforced BEFORE registration
    # so the uri that is registered is the one that renders.
    layer_uri = _ensure_raster_has_overviews(layer_uri)

    # The DECLARED style row, resolved against this raster ONCE: the range, the
    # ramp and the .qml all come out of that one resolution, stashed under the
    # s3:// uri this call returns because the legend travels by uri, not on the
    # returned value. Fail-open: a None legend clears the stash entry.
    try:
        _stash_legend_for_uri(layer_uri, legend_for_published_layer(
            style, layer_uri, override=scale, shared=shared_range))
    except Exception as exc:  # noqa: BLE001 - legend never blocks a publish
        logger.debug("publish_layer legend build skipped (%s: %s)",
                     type(exc).__name__, exc)
    logger.info("publish_layer layer_id=%s uri=%s", layer_id, layer_uri)
    # Register the layer so a downstream tool resolves the handle to readable
    # bytes. One scheme, one face: the s3:// COG is both the data uri and the uri
    # the client renders.
    observe_published_layer(layer_id, uri=layer_uri)
    return layer_uri
