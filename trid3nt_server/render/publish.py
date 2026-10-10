"""The raster PUBLISH mechanism - write, register, notify.

One store, one scheme: a raster lives as a COG at ``s3://<bucket>/<key>`` that
the QGIS plugin reads through GDAL ``/vsis3``, so a publish never mints a second
face for it. Vectors are a benign no-op, and every probe here fails OPEN.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from trid3nt_contracts import new_ulid

from . import presets
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

logger = logging.getLogger("trid3nt_server.render.publish")


class PublishLayerError(RuntimeError):
    """Raised when ``publish_layer`` cannot complete the round-trip.
    ``error_code`` is a SCREAMING_SNAKE_CASE code; ``retryable`` says whether
    re-issuing the call with corrected arguments can succeed.
    """

    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


# Two guards run first because they are facts about the file, not the style, and a ramp would corrupt an already-painted
# image: a COG with its own band-1 colour table, or an RGB(A)/multiband COG. Neither takes a preset.

def _is_rgba_or_multiband(raster_bytes: bytes | None) -> bool:
    """A read failure returns False so a single-band scalar still gets its range."""
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


def resolve_layer_style(
    style: dict[str, Any] | None,
    layer_uri: str,
    *,
    override: "Scale | None" = None,
    shared: tuple[float, float] | None = None,
    raster_bytes: bytes | None = None,
) -> "presets.Resolved | None":
    """Resolve a DECLARED style row against this raster. The one resolution point.

    ``None`` for an already-painted raster.
    """
    preset = presets.from_row(style)
    # A vector or mesh declaration takes this call and resolve but skips the raster probes, which a FlatGeobuf or SELAFIN cannot answer. One seam, four kinds.
    if not presets.paints_a_raster(preset):
        resolved = presets.resolve(preset, override=override, shared=shared)
        logger.info("publish_layer (style) uri=%s -> %s", layer_uri,
                    resolved.legend_note())
        return resolved
    if raster_bytes is None and presets.needs_run_range(
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
    if _is_rgba_or_multiband(raster_bytes):
        return None
    resolved = presets.resolve(preset, read_range=presets.band_range_reader(raster_bytes),
                               override=override,
                               shared=shared)
    logger.info("publish_layer (style) uri=%s -> %s", layer_uri,
                resolved.legend_note())
    return resolved


# The legend is built from the same resolution the .qml is written from, so colourbar and raster span identical
# numbers. An already-painted raster has no resolution to report. Fail-open: any failure returns ``None``.

#: Published-raster ``LegendKey`` by ``s3://`` COG uri, shared with the register-only manifest seam. Module scope is
#: safe (the legend is a pure function of the content-addressed COG and declared row); FIFO-bounded at the write site.
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
) -> "LegendKey | None":
    """The layer's resolved style, as the key the map renders from.
    The declared row is resolved ONCE: the range, the ramp and the .qml all come
    out of that one resolution. ``None`` on an already-painted raster or any error.
    """
    from trid3nt_contracts.execution import LegendKey

    try:
        paints_raster = presets.paints_a_raster(presets.from_row(style))
        if paints_raster and raster_bytes is None:
            raster_bytes = _read_raster_bytes(layer_uri)
        resolved = resolve_layer_style(
            style, layer_uri, override=override, shared=shared,
            raster_bytes=raster_bytes)
        if resolved is None:
            return None
        preset = resolved.preset
        return LegendKey(
            kind=preset.kind,
            colormap=preset.ramp if preset.kind != "reference" else None,
            vmin=resolved.range[0] if resolved.range else None,
            vmax=resolved.range[1] if resolved.range else None,
            units=preset.units or units,
            floor=preset.floor,
            qml=resolved.qml(),
        )
    except Exception as exc:  # noqa: BLE001 - never block a publish on the legend
        logger.debug("legend_for_published_layer failed for %s (%s: %s)",
                     layer_uri, type(exc).__name__, exc)
        return None


def _stash_legend_for_uri(layer_uri: str, legend: "LegendKey | None") -> None:
    """A ``None`` legend clears the entry so a re-publish cannot leave an orphan."""
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


# The raw ``s3://`` COG uri IS what the client renders: do not reintroduce an XYZ tile-template mint here.


#: Vector artifact extensions, token-tail matched against the resolved URI basename. ``publish_layer`` is raster-only:
#: GDAL cannot open a FlatGeobuf as a COG, so a vector routed through it would fail rather than render.
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




def _raster_has_overviews(raster_bytes: bytes) -> bool | None:
    """``None`` means undeterminable (rasterio absent or the open failed); callers fail open."""
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
    """``None`` when band 1 carries no table, so no caller fabricates one."""
    try:
        return src.colormap(1)
    except ValueError:
        return None
    except Exception as exc:  # noqa: BLE001 - any other read failure: no-op
        logger.debug("colormap read skipped (%s: %s)", type(exc).__name__, exc)
        return None


def _apply_band1_colormap(dst, cmap: dict | None) -> None:
    """A ``None`` ``cmap`` is a no-op: a colour table is never fabricated."""
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
    """Encode raster bytes as a tiled COG with overviews, render's one COG writer; ``None`` when the encode fails or yields no overview."""
    try:
        from rasterio.io import MemoryFile

        with MemoryFile(raster_bytes) as src_mem, src_mem.open() as src:
            profile = {
                "driver": "COG", "width": src.width, "height": src.height,
                "count": src.count, "dtype": src.dtypes[0], "crs": src.crs,
                "transform": src.transform, "compress": "DEFLATE",
            }
            if src.nodata is not None:
                profile["nodata"] = src.nodata
            data = src.read()
            colorinterp = src.colorinterp
            cmap = _read_band1_colormap(src)
            count = len(_overview_factors(src.width, src.height))
        # Averaging class indices invents codes the palette maps to wrong colours, so a paletted raster downsamples by nearest.
        resampling = "NEAREST" if cmap else "AVERAGE"
        with MemoryFile() as dst_mem:
            with dst_mem.open(OVERVIEW_COUNT=count, OVERVIEW_RESAMPLING=resampling,
                              **profile) as dst:
                dst.write(data)
                try:
                    dst.colorinterp = colorinterp
                except Exception:  # noqa: BLE001 - colorinterp set is best-effort
                    pass
                _apply_band1_colormap(dst, cmap)
            out = dst_mem.read()
    except Exception as exc:  # noqa: BLE001 - fail-open upstream
        logger.warning(
            "publish_layer: COG encode failed (%s: %s) - publishing the original "
            "(no-overview) raster as-is", type(exc).__name__, exc)
        return None
    return out if _raster_has_overviews(out) else None


def _overview_factors(width: int, height: int) -> list[int]:
    """Never empty: a raster below the 256px floor still gets factor 2."""
    factors: list[int] = []
    factor = 2
    while max(width, height) // factor >= 256:
        factors.append(factor)
        factor *= 2
        if len(factors) >= 8:  # safety cap
            break
    # Always add factor=2: with no overview level QGIS computes minzoom == maxzoom for a tiny COG and renders nothing at a continental zoom.
    if not factors:
        factors = [2]
    return factors


def _read_raster_bytes(layer_uri: str) -> bytes | None:
    """Fail-open: any read error returns ``None``."""
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
    """A local path is a legal input, so this never raises."""
    from trid3nt_server.store import objects as storage

    try:
        _scheme, bucket, key = storage.split_object_uri(uri)
    except storage.StorageError:
        return None
    return (bucket, key) if bucket and key else None


def _write_overview_cog(layer_uri: str, cog_bytes: bytes) -> str | None:
    """A fresh ULID-suffixed sibling: the original COG is never mutated and a warm negative cache cannot poison the new object."""
    parsed_s3 = _split_s3_uri(layer_uri)
    try:
        if layer_uri.startswith("s3://") and parsed_s3 is not None:
            from trid3nt_server.store import objects as storage

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
    """Fail-open at every step: any failure returns ``layer_uri`` unchanged."""
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
    """Shape only: enough to tell an identifier from a human name."""
    import re as _re

    return bool(_re.match(r"^[0-9A-HJKMNP-TV-Z]{26}$", value, _re.IGNORECASE))


def _looks_like_hash_or_id(value: str) -> bool:
    """A URI segment not worth showing a reader."""
    import re as _re

    if _looks_like_ulid(value):
        return True
    return bool(_re.match(r"^[0-9a-f]{12,64}$", value, _re.IGNORECASE))


def _label_from_uri(layer_uri: str) -> str | None:
    """The parent segment wins over the file stem, which is usually a cache hash."""
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
    """So two derived names for one family do not collide in the layer list."""
    import re as _re

    tail = _re.sub(r"[^A-Za-z0-9]", "", layer_id or "")[-4:]
    if tail:
        return tail.upper()
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%m%d")


def derive_readable_layer_name(
    name: str | None,
    layer_id: str,
    layer_uri: str,
) -> str:
    """Derive a human-readable layer name for the layer list.

    An explicit non-ULID ``name`` returns verbatim; a derived one is disambiguated.
    """
    # Precedence: an explicit non-ULID name, a human segment of the source uri, then "Layer"; a bare ULID never reaches the summary.
    if name and name.strip() and not _looks_like_ulid(name.strip()):
        return name.strip()

    label = _label_from_uri(layer_uri) or "Layer"
    return f"{label} {_short_disambiguator(layer_id)}"


def publish_layer(
    layer_uri: str,
    layer_id: str,
    style: dict[str, Any] | None = None,
    #: A specialization of the contract's scale for this one layer (a param knob or `restyle_layer`); absent means the contract default.
    scale: "ScaleSpec | None" = None,
    #: One range shared across a compared set, so before/after and coarse-versus-refined are painted against each other.
    shared_range: tuple[float, float] | None = None,
) -> str:
    """Publish a COG raster: write, register, notify.
    Returns the ``s3://`` COG uri the client renders - the overview-enforced
    sibling when one had to be built. A vector needs no publish and comes back as is.
    """
    # A vector is already a store object the client opens natively; GDAL cannot open a FlatGeobuf as a raster COG.
    if _is_vector_uri(layer_uri):
        return layer_uri
    if not layer_uri.startswith("s3://"):
        raise PublishLayerError(
            "LAYER_URI_NOT_FOUND",
            f"layer_uri {layer_uri!r} is not an s3:// COG in this store. "
            "Pass the producing tool's layer handle or its s3:// URI verbatim.",
            retryable=True,
        )
    # A no-overview COG renders spotty (cold range requests time out, nothing downsamples it); enforced before registration so the registered uri is the one that renders.
    layer_uri = _ensure_raster_has_overviews(layer_uri)

    # The declared style row is resolved against this raster once and stashed under the returned s3:// uri, because the legend
    # travels by uri; a None legend clears the stash entry. Fail-open.
    try:
        _stash_legend_for_uri(layer_uri, legend_for_published_layer(
            style, layer_uri, override=scale, shared=shared_range))
    except Exception as exc:  # noqa: BLE001 - legend never blocks a publish
        logger.debug("publish_layer legend build skipped (%s: %s)",
                     type(exc).__name__, exc)
    logger.info("publish_layer layer_id=%s uri=%s", layer_id, layer_uri)
    # Register the layer so a downstream tool resolves the handle to readable bytes; the s3:// COG is both data uri and rendered uri.
    observe_published_layer(layer_id, uri=layer_uri)
    return layer_uri
