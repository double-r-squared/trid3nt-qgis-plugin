"""The raster PUBLISH mechanism - overviews, styling, legend, registration.

Emission is AUTOMATIC: there is no "display this" intent and no
``publish_layer`` tool for a model to call. Every renderable raster a tool
returns rides :func:`trid3nt_server.emission.layer_uri_emit.publish_for_emission`
through this module on its way to the map, intermediates included - the user
hides what they do not want to see in QGIS.

    ``publish_layer(layer_uri, layer_id, style_preset, ...)``
      -> ``str`` (the raster's raw ``s3://`` COG URI, ready for the envelope)

**The path (s3 + QGIS-native rendering; the only publish path)**

Rasters live as COGs at ``s3://<bucket>/<key>`` on the object store (MinIO
locally). The QGIS plugin - the ONLY client - opens the COG DIRECTLY via
GDAL ``/vsicurl/`` (the same s3->http translation it already uses for
FlatGeobuf vectors) and applies its own renderer from the envelope's
legend/style fields, so the publish emits the raw ``s3://`` URI itself:

1. Guard against unresolved layer handles / placeholder URIs (typed,
   retryable errors that name the case's real handles).
2. Vectors: benign no-op (they already render inline via their producing
   fetch tool's GeoJSON), OR a durable per-Case GeoJSON asset,
   OR - when ``TRID3NT_QGIS_WMS_BASE`` is exported - a styled QGIS Server
   WMS GetMap face.
3. Rasters: enforce COG overviews (auto-translate when missing),
   resolve styling via ``_resolve_qgis_style_params`` (THE render
   chokepoint: categorical/RGBA/terrain passthroughs, then the contract-
   declared preset in ``contracts/trid3nt_contracts/styles.yaml`` resolved
   by ``emission/styles.py``, then band-stats percentile fallback, then a
   safe default), stash the data-driven legend keyed by the ``s3://`` uri
   the envelope will carry, and register the layer via
   ``observe_published_layer``.

QGIS-native rendering: nothing here mints
``{tile_base}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}`` XYZ templates or reads
``TRID3NT_TILE_SERVER_BASE``. Old persisted cases still carry legacy
tile-template URIs; a re-publish of one is UNWRAPPED to its embedded ``url=``
s3 COG and flows through the normal raster path, and the plugin unwraps legacy
templates it rehydrates on its own. No worker round-trip, no ``.qgs`` mutation
on the raster path.

**Cross-cutting principles:**

- **Side effect, never cached.** A publish writes overview COGs / durable
  vector assets and registers layer faces; there is nothing to memoize.
- **Resilience:** failures surface as typed :class:`PublishLayerError`
  (not unhandled exceptions); style/legend/overview probes fail OPEN so a
  publish is never blocked by a best-effort enhancement. The caller at the
  emission seam also fails open: a raster whose publish fails still reaches
  the map as its raw ``s3://`` COG, unstyled, because the QGIS plugin can
  read that - a degrade, not a broken layer row.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.styles import ScaleSpec

from . import styles
from .cog import translate_to_cog
from .uri_registry import observe_published_layer

__all__ = [
    "publish_layer",
    "style_preset_for_publish",
    "PublishLayerError",
    "derive_layer_id",
    "derive_readable_layer_name",
    "style_params_from_band_stats",
    "legend_for_published_layer",
    "pop_legend_for_uri",
    "set_default_qgs_uri",
    "DEFAULT_PROJECT_QGS_URI",
]

logger = logging.getLogger("trid3nt_server.emission.publish")


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Default canonical project .qgs URI, consumed by the ``TRID3NT_QGIS_WMS_BASE``
#: vector-WMS seam - override via ``set_default_qgs_uri``.
DEFAULT_PROJECT_QGS_URI: str = "s3://trid3nt-qgs/sample.qgs"


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
    - ``QGS_URI_PARSE_ERROR`` - malformed ``project_qgs_uri`` (vector-WMS seam).
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
# DI seams
# --------------------------------------------------------------------------- #

_DEFAULT_QGS_URI: str | None = None


def set_default_qgs_uri(uri: str | None) -> None:
    """Override the default canonical .qgs URI.

    Useful for smoke harnesses and integration tests that target a non-production
    project. ``None`` restores the constant default.
    """
    global _DEFAULT_QGS_URI
    _DEFAULT_QGS_URI = uri


def _get_effective_qgs_uri(project_qgs_uri: str | None) -> str:
    if project_qgs_uri is not None:
        return project_qgs_uri
    if _DEFAULT_QGS_URI is not None:
        return _DEFAULT_QGS_URI
    return DEFAULT_PROJECT_QGS_URI


def _parse_qgs_key(qgs_uri: str) -> str:
    """Extract the object key (no leading slash) from a gs:// or s3:// URI.

    Used to build the MAP= parameter in the WMS URL. Both schemes share the
    ``<scheme>://<bucket>/<key>`` shape, so the key extraction is identical.
    Accepting ``gs://`` keeps old persisted project URIs parseable; the
    QGIS-vector WMS branch (``TRID3NT_QGIS_WMS_BASE`` set) needs the
    ``s3://`` form or the branch fails.

    Examples:
        ``s3://trid3nt-qgs/sample.qgs`` -> ``sample.qgs``
        ``gs://legacy-cloud-qgs/sample.qgs`` -> ``sample.qgs``

    Raises:
        PublishLayerError: if the URI is not a gs:// or s3:// URI, or has no
        key component.
    """
    for scheme in ("gs://", "s3://"):
        if qgs_uri.startswith(scheme):
            rest = qgs_uri[len(scheme):]
            break
    else:
        raise PublishLayerError(
            "QGS_URI_PARSE_ERROR",
            f"project_qgs_uri must be a gs:// or s3:// URI; got {qgs_uri!r}",
        )
    # <scheme>://<bucket>/<key>
    slash_idx = rest.find("/")
    if slash_idx == -1 or slash_idx == len(rest) - 1:
        raise PublishLayerError(
            "QGS_URI_PARSE_ERROR",
            f"project_qgs_uri has no key component: {qgs_uri!r}",
        )
    key = rest[slash_idx + 1:]
    return key


#: Env var that, WHEN SET, activates the s3-branch QGIS-vector publish route.
#: It is the base URL of a QGIS Server WMS endpoint. Dormant seam: until
#: ``TRID3NT_QGIS_WMS_BASE`` is exported the s3 branch keeps the existing
#: ``_benign_vector_noop`` (vectors already render inline via their
#: producing fetch tool's GeoJSON), so behavior is unchanged. Exporting this
#: var flips publish_layer to compose a styled WMS GetMap face for the vector.
_QGIS_WMS_BASE_ENV: str = "TRID3NT_QGIS_WMS_BASE"


def _get_qgis_wms_base() -> str:
    """Return the configured QGIS Server WMS base (trailing slash stripped).

    Empty string when ``TRID3NT_QGIS_WMS_BASE`` is unset/blank - the caller
    treats that as "infra not yet stood up" and falls back to the benign no-op.
    """
    return os.environ.get(_QGIS_WMS_BASE_ENV, "").rstrip("/")


def _build_vector_wms_url(
    wms_base: str,
    layer_uri: str,
    layer_id: str,
    qgs_key: str,
) -> str:
    """Compose a styled WMS GetMap URL for a VECTOR on the QGIS-vector path.

    Points at ``TRID3NT_QGIS_WMS_BASE`` and carries the standard WMS GetMap
    envelope so ``uri_registry._looks_like_wms`` recognizes it as a
    renderable display face. The MAP= param uses the ``/mnt/qgs/<key>``
    mount convention the QGIS Server worker expects.

    Style seam: the family-aware ``_infer_style_preset`` (the same selector the
    raster paths use) is threaded into a ``STYLES=`` value so the QGIS Server
    can apply a named style when one is registered; the empty-string default
    (terrain-family / unknown) yields ``STYLES=`` which is a valid WMS "server
    default style" request.
    """
    from urllib.parse import quote

    style = _infer_style_preset(layer_uri, layer_id)
    map_param = f"/mnt/qgs/{qgs_key}"
    return (
        f"{wms_base}?MAP={quote(map_param, safe='/')}"
        "&SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
        f"&LAYERS={quote(layer_id, safe='')}"
        f"&STYLES={quote(style, safe='')}"
        "&FORMAT=image/png&TRANSPARENT=true"
    )


#: token vocabulary marking TERRAIN-family rasters. These are
#: RGBA (colored relief) or single-band grayscale/Float32 (hillshade, slope,
#: aspect, raw DEM) products - QGIS DEFAULT rendering visualizes them
#: correctly, while the flood-depth pseudocolor ramp clamps them to a
#: uniform/transparent tile.
#: Token-boundary matching (not substring) so e.g. a layer_id like
#: ``"demo-flood"`` does NOT match ``dem``.
_TERRAIN_STYLE_TOKENS = frozenset(
    # slope/aspect are NOT terrain tokens: they carry real colormaps
    # (slope_angle_deg ylorrd / aspect_compass_deg hsv) via the style
    # registry, routed by _infer_style_preset below. dem/relief/hillshade/
    # terrain/elevation stay grayscale -- bare DEM + shaded relief render
    # correctly unstyled.
    {"dem", "relief", "hillshade", "terrain", "elevation"}
)

#: URI/id token -> the slope/aspect colormap preset, applied BEFORE the
#: terrain passthrough so an auto-inferred slope/aspect layer is
#: colormapped (not left grayscale and not mis-defaulted to flood depth).
_SLOPE_ASPECT_PRESET_BY_TOKEN: dict[str, str] = {
    "slope": "slope_angle_deg",
    "aspect": "aspect_compass_deg",
}


def _infer_style_preset(layer_uri: str, layer_id: str) -> str:
    """The RENDERING-FAMILY default for a raster whose producer named no preset.

    This routes on how a raster must be PAINTED, never on what it measures: a
    filename is not a measurement, so nothing here may conclude a physical
    quantity from one. Slope and aspect carry their own colormaps; the remaining
    terrain rasters (dem/relief/hillshade/terrain/elevation) are RGBA or
    grayscale and render correctly unstyled, so they take ``""``.

    Everything else takes the NEUTRAL ramp - a single-hue colormap over the
    field's own range, labelled for an unknown quantity. A named physical ramp
    here would paint a quantity nobody declared in the colours and legend of one
    somebody guessed. A producer that knows its quantity declares it and gets
    the contract's ramp; the price of not declaring is a neutral picture, not a
    wrong one.

    Tokenizes BOTH the resolved URI and the layer_id on non-alphanumerics and
    matches whole tokens, so ``demo`` never trips ``dem``.
    """
    import re as _re

    tokens = set(
        _re.split(r"[^a-z0-9]+", f"{layer_uri} {layer_id}".lower())
    )
    for token, preset in _SLOPE_ASPECT_PRESET_BY_TOKEN.items():
        if token in tokens:
            return preset
    if tokens & _TERRAIN_STYLE_TOKENS:
        return ""
    return styles.NEUTRAL_FALLBACK_PRESET


# --------------------------------------------------------------------------- #
# QGIS style resolver (s3 branch)
#
# A single-band float32 raster renders as AUTOSCALED GRAYSCALE unless the
# resolved style-params carry an explicit ``&rescale=<lo>,<hi>`` and
# ``&colormap_name=<name>`` (the rio-tiler string the QGIS plugin parses into
# vmin/vmax/colormap).
#
# ``_resolve_qgis_style_params`` is the single resolution point. CRITICAL
# guards run FIRST so rasters that are ALREADY colorized are never corrupted
# by a single-band rescale/colormap (the HIGH-severity terrain/RGBA
# regression a rescale would otherwise introduce):
#   - categorical / paletted COG (NLCD land cover) -> "" (embedded GDAL
#     color table wins);
#   - RGB(A) / multiband COG (colored relief, blended landcover + hillshade
#     composite) -> "" (QGIS renders the baked colors directly);
#   - terrain-token preset/URI (continuous_dem / hillshade / slope / aspect /
#     relief / terrain / elevation) -> "" (grayscale terrain auto-scales,
#     RGBA terrain renders directly).
# Only AFTER those passthroughs does it delegate to the contract-declared
# preset in ``contracts/trid3nt_contracts/styles.yaml``, resolved by
# ``emission/styles.py``, for single-band weather SCALARS, falling back to a
# generic band-stats percentile rescale for any preset the contract does not
# cover, then a SAFE non-empty default. Colormap names are LOWERCASE
# rio-tiler names (viridis, blues, ylgnbu, reds, rdbu, rdylbu_r, ylgn,
# ylorrd, gray, gray_r, ...) - rio-tiler casing is lowercase (NOT
# matplotlib), do not change.
# --------------------------------------------------------------------------- #

def _is_rgba_or_multiband(raster_bytes: bytes | None) -> bool:
    """True if the COG is RGB(A)/multiband - QGIS renders it DIRECTLY.

    Reads the in-hand COG bytes via a rasterio ``MemoryFile`` and reports True
    when band count >= 3 OR any band's color interpretation is one of
    Red/Green/Blue/Alpha. Such rasters (colored relief, blended landcover +
    hillshade composites) are already colorized: a single-band ``&rescale`` +
    ``&colormap_name`` would corrupt them, so the resolver returns ``""``
    (empty style_params = QGIS passthrough) for them. Best-effort: returns
    False on any read failure so a real single-band scalar still gets its
    rescale.
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


def _is_terrain_token_preset(style_preset: str | None, layer_uri: str) -> bool:
    """True if the preset / URI tokenizes to a TERRAIN-family token.

    Reuses ``_TERRAIN_STYLE_TOKENS`` (dem, relief, hillshade, slope, aspect,
    terrain, elevation). Tokenizes the ``style_preset`` AND ``layer_uri`` on
    non-alphanumerics and matches whole tokens, so e.g. ``"continuous_dem"``
    tokenizes to ``{continuous, dem}`` -> matches ``dem``. Terrain rasters
    (grayscale hillshade/slope/aspect, RGBA colored relief) render correctly
    with NO rescale, so the resolver returns ``""`` for them before trying
    the contract preset / band-stats.
    """
    import re as _re

    tokens = set(
        _re.split(r"[^a-z0-9]+", f"{style_preset or ''} {layer_uri or ''}".lower())
    )
    return bool(tokens & _TERRAIN_STYLE_TOKENS)


def _resolve_qgis_style_params(
    style_preset: str | None, layer_uri: str, *,
    override: "ScaleSpec | None" = None,
    shared: tuple[float, float] | None = None,
) -> str:
    """The ``&rescale=..&colormap_name=..`` string the QGIS plugin parses.

    THE RENDER CHOKEPOINT. Three RASTER guards live here because they are facts
    about the file rather than about the style, and each one is a way a
    single-band rescale would CORRUPT an already-colorized image:

    1. an embedded band-1 GDAL colour table (NLCD land cover) - the palette wins;
    2. RGB(A) / >=3 bands (coloured relief, a landcover+hillshade composite) -
       the baked colours render directly;
    3. a terrain-family preset or URI (dem / hillshade / slope / aspect / relief /
       elevation) - grayscale terrain auto-scales and RGBA terrain renders as is.

    Everything after that is the STYLE decision, and it is not made here: the
    contract declares the preset and ``emission/styles.py`` resolves it, reading
    this raster's own range only when the declared policy asks for it. The COG
    bytes are read ONCE and shared by all three probes and the range read.
    """
    raster_bytes = _read_raster_bytes(layer_uri)

    if raster_bytes is not None:
        try:
            from rasterio.io import MemoryFile

            with MemoryFile(raster_bytes) as mem, mem.open() as src:
                if _read_band1_colormap(src) is not None:
                    logger.info(
                        "publish_layer (style) %s carries an embedded band-1 colour "
                        "table - leaving style_params empty so QGIS colorizes from "
                        "the palette", layer_uri)
                    return ""
        except Exception as exc:  # noqa: BLE001 - palette probe is best-effort
            logger.debug("palette probe skipped (%s: %s)", type(exc).__name__, exc)

    if _is_rgba_or_multiband(raster_bytes):
        logger.info(
            "publish_layer (style) %s is RGB(A)/multiband - leaving style_params "
            "empty so QGIS renders the baked colours directly", layer_uri)
        return ""

    if _is_terrain_token_preset(style_preset, layer_uri):
        logger.info(
            "publish_layer (style) preset=%r uri=%s is a TERRAIN-family raster - "
            "leaving style_params empty", style_preset, layer_uri)
        return ""

    resolved = styles.resolve_style(
        style_preset, read_range=styles.band_range_reader(raster_bytes),
        override=override, shared=shared)
    logger.info("publish_layer (style) preset=%r uri=%s -> %s",
                style_preset, layer_uri, resolved.legend_note())
    return resolved.style_params()


def style_params_from_band_stats(
    style_preset: str | None,
    *,
    is_categorical: bool = False,
    is_rgba: bool = False,
    p2: float | None = None,
    p98: float | None = None,
    layer_uri: str = "",
) -> str:
    """The same string, WITHOUT a COG download - the register-only fast path.

    The worker precomputed the band stats onto the manifest, so the agent asks the
    ONE resolver the same question with the percentiles already in hand. The three
    raster guards arrive as flags for the same reason.
    """
    if is_categorical or is_rgba:
        return ""
    if _is_terrain_token_preset(style_preset, layer_uri):
        return ""
    return styles.resolve_style(
        style_preset, read_range=styles.fixed_range_reader(p2, p98)).style_params()


# --------------------------------------------------------------------------- #
# Data-driven legend KEY: the color gradient/key must come FROM THE DATA - it
# must mean something.
#
# The legend is derived DIRECTLY from the resolved style_params string
# (the SAME ``&rescale=lo,hi&colormap_name=name`` the raster render uses), so
# the legend range and the painted raster range AGREE by construction -- there
# is no second, separately-computed range to drift. For contract-pinned
# presets that is the semantic fixed range (flood 0-3, seismic PGA 0-1,
# temperature 250-320 K); for the generic fallback it is the REAL p2/p98
# percentile range the resolver already read off the COG. Categorical
# (paletted/NLCD) rasters carry NO style_params (the embedded GDAL table
# colorizes them), so their legend comes from ``_read_band1_colormap``
# instead -- one ``LegendClass`` per table entry.
#
# Fail-open: ANY failure here returns ``None`` so the publish proceeds
# exactly as before (legend=None => the QGIS plugin falls back to rendering
# from style_preset).
# --------------------------------------------------------------------------- #

#: Module-level side-table of the most-recent published-raster ``LegendKey``
#: keyed by the layer's ENVELOPE uri - the raw ``s3://`` COG the atomic
#: ``publish_layer`` returns (QGIS-native); the register-only manifest seam
#: keys by the same raw ``cog_uri``, so both producers share one key shape.
#: ``publish_layer`` returns a bare URI string, so the server wrap-site
#: rebuilds a ``LayerURI`` from it WITHOUT a legend; the pipeline emitter's
#: ``add_loaded_layer`` lifts the legend back out of this stash by
#: ``layer.uri``. Mirrors ``_LAST_DENSITY_META_BY_URI`` exactly (module scope
#: is safe -- the legend is a pure function of the content-addressed COG +
#: preset, so two sessions publishing the same layer compute the identical
#: key). FIFO-bounded at the write site so the always-on agent process never
#: grows it without limit.
_MAX_LEGEND_ENTRIES: int = 256
_LAST_LEGEND_BY_URI: dict[str, Any] = {}


def _parse_style_params(style_params: str) -> tuple[float | None, float | None, str | None]:
    """Pull ``(vmin, vmax, colormap_name)`` out of a ``&rescale=lo,hi&colormap_name=name``
    style-params string. Any field absent / unparseable -> ``None`` for that slot.

    This is the inverse of the strings the resolver builds, so the legend and the
    raster render are GUARANTEED to use the same numbers (no second range read).
    """
    from urllib.parse import parse_qsl

    vmin: float | None = None
    vmax: float | None = None
    cmap: str | None = None
    if not style_params:
        return (None, None, None)
    for k, v in parse_qsl(style_params.lstrip("&"), keep_blank_values=False):
        if k == "rescale" and "," in v:
            lo_s, hi_s = v.split(",", 1)
            try:
                vmin, vmax = float(lo_s), float(hi_s)
            except ValueError:
                vmin = vmax = None
        elif k == "colormap_name":
            cmap = v or None
    return (vmin, vmax, cmap)


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
    return LegendKey(kind="categorical", classes=classes, label=label)


def legend_for_published_layer(
    style_preset: str | None,
    layer_uri: str,
    style_params: str,
    *,
    units: str | None = None,
    raster_bytes: bytes | None = None,
) -> "LegendKey | None":
    """Build the data-driven ``LegendKey`` for a just-published RASTER layer.

    Derived from the ALREADY-resolved ``style_params`` so the legend range equals
    the rendered range by construction:

    - ``style_params`` carries ``&rescale=lo,hi&colormap_name=name`` -> a
      ``kind="continuous"`` key with ``colormap=name``, ``vmin=lo``, ``vmax=hi``
      (the real p2/p98 range for unpinned presets; the pinned semantic range for
      contract presets -- whichever the raster actually renders with).
    - empty ``style_params`` (categorical / RGBA / terrain passthrough) -> probe
      the COG for an embedded GDAL color table and emit a ``kind="categorical"``
      key of one swatch per class. RGBA composites + grayscale terrain carry no
      table, so they get ``None`` (there is no meaningful key).

    Fail-open: returns ``None`` on ANY error so the publish is never blocked
    (``legend=None`` => the QGIS plugin renders the layer from style_preset
    exactly as before).
    """
    from trid3nt_contracts.execution import LegendKey

    try:
        vmin, vmax, cmap_name = _parse_style_params(style_params)
        label = _legend_label_for(style_preset)
        if cmap_name is not None and vmin is not None and vmax is not None:
            # Continuous raster: the resolved rescale IS the legend range, so the
            # colorbar and the painted tiles span the identical numbers.
            return LegendKey(
                kind="continuous",
                colormap=cmap_name,
                vmin=vmin,
                vmax=vmax,
                units=units,
                label=label,
            )
        # No rescale/colormap in the URL -> categorical/paletted, RGBA, or
        # terrain passthrough. Only a paletted raster has a meaningful key.
        if raster_bytes is None:
            raster_bytes = _read_raster_bytes(layer_uri)
        if raster_bytes is None:
            return None
        try:
            import rasterio
            from rasterio.io import MemoryFile

            with MemoryFile(raster_bytes) as mem, mem.open() as src:
                table = _read_band1_colormap(src)
        except Exception as exc:  # noqa: BLE001 - palette probe is best-effort
            logger.debug(
                "legend palette probe skipped (%s: %s)", type(exc).__name__, exc
            )
            return None
        if not table:
            return None
        return _categorical_legend_from_colormap(table, label=label)
    except Exception as exc:  # noqa: BLE001 - never block a publish on the legend
        logger.debug(
            "legend_for_published_layer failed for %s (%s: %s)",
            layer_uri,
            type(exc).__name__,
            exc,
        )
        return None


def _legend_label_for(style_preset: str | None) -> str | None:
    """A short human-readable legend title from the preset, or ``None``.

    The caption is DECLARED on the preset in the style contract, because a
    caption derived from the preset name alone cannot state what the name does
    not carry: a modelled surface reads as a measured one. The derivation below
    is the fallback for a preset the contract does not declare.

    Best-effort cosmetic: the QGIS plugin renders the result verbatim as the
    legend caption and ``None`` is fine (it falls back to the layer name). Never
    affects the range.
    """
    if not style_preset or style_preset == "auto":
        return None
    declared = styles.preset_label(style_preset)
    if declared:
        return declared
    cleaned = style_preset
    for prefix in ("continuous_", "categorical_", "diverging_"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    cleaned = cleaned.replace("_", " ").strip()
    if not cleaned:
        return None
    return cleaned[:1].upper() + cleaned[1:]


def _stash_legend_for_uri(display_uri: str, legend: "LegendKey | None") -> None:
    """Record (or clear) the published layer's ``LegendKey`` keyed by envelope uri
    (the raw ``s3://`` COG - both the atomic publish and the register-only
    manifest seam key by it).

    FIFO-bounded (mirrors ``_LAST_DENSITY_META_BY_URI``) so the always-on agent
    process cannot grow this side-table without limit. A ``None`` legend clears
    any stale entry for this uri (so a re-publish that now resolves to no key
    cannot leave an orphaned one behind).
    """
    if not display_uri:
        return
    if display_uri in _LAST_LEGEND_BY_URI:
        del _LAST_LEGEND_BY_URI[display_uri]
    if legend is None:
        return
    _LAST_LEGEND_BY_URI[display_uri] = legend
    while len(_LAST_LEGEND_BY_URI) > _MAX_LEGEND_ENTRIES:
        _LAST_LEGEND_BY_URI.pop(next(iter(_LAST_LEGEND_BY_URI)))


def pop_legend_for_uri(display_uri: str) -> "LegendKey | None":
    """Look up the stashed ``LegendKey`` for a published layer's envelope uri
    (the raw ``s3://`` COG - atomic publish and register-only path alike).

    Non-destructive READ (a re-emit / replay of the SAME layer must resolve the
    same key). The pipeline emitter's ``add_loaded_layer`` calls this to lift the
    legend onto the ``ProjectLayerSummary`` for the publish_layer wrap-site path
    (where the rebuilt ``LayerURI`` carries no legend of its own). Returns
    ``None`` when nothing was stashed (legacy / categorical-RGBA layers).
    """
    return _LAST_LEGEND_BY_URI.get(display_uri)


# NOTE: QGIS-native rendering emits the raw ``s3://`` COG uri directly (see
# module docstring) - do not reintroduce an XYZ tile-template mint here.


# --------------------------------------------------------------------------- #
# Benign vector handling
# --------------------------------------------------------------------------- #

#: Vector artifact extensions. ``publish_layer`` is RASTER-ONLY (see the module
#: docstring + the inline-GeoJSON path). A vector reaching here is ALREADY on
#: the map via its producing fetch tool (``add_loaded_layer`` inline GeoJSON),
#: so a publish is unnecessary - and GDAL cannot open a FlatGeobuf as a
#: raster COG, so routing one through the raster path would fail to open,
#: not render. Token-tail matched against the resolved URI basename.
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

    The agent keeps calling ``publish_layer`` on vector layers (roads/rivers)
    that ALREADY rendered inline via their producing fetch tool's GeoJSON
    (``add_loaded_layer`` path); this is a benign no-op for that call: NO
    raise (so ``emit_tool_call`` ``mark_complete``s the step - green, not
    red), NO tile template, NO ``observe_published_layer`` registration (so
    no hanging-tile face is minted). The returned string is what the caller
    gets back - a clear, honest "already rendered inline; no publish needed"
    rather than a failure it would have to explain.
    """
    logger.info(
        "publish_layer: benign vector no-op for layer_id=%s uri=%s - vector "
        "already rendered inline (Wave 4.9 GeoJSON); no raster publish needed",
        layer_id,
        layer_uri,
    )
    return (
        f"noop: layer_id={layer_id!r} is a VECTOR ({layer_uri!r}) and is already "
        "rendered on the map inline by its producing fetch tool (GeoJSON). "
        "publish_layer is raster-only; no publish was needed and none was "
        "performed. Do NOT re-call publish_layer for this vector layer."
    )


# --------------------------------------------------------------------------- #
# Durable browser-readable GeoJSON for every vector.
#
# Vectors are produced as FlatGeobuf (``.fgb``) which the browser CANNOT read,
# and today the agent delivers them INLINE (it reads the .fgb back, parses to
# GeoJSON, and ships the FeatureCollection on the WS). That works ONLY while the
# agent box is awake - the box-off cold path (signer -> S3) has no browser-
# readable copy of a vector layer, so a cold-opened case paints rasters but not
# roads/rivers/footprints/mesh.
#
# Durable contract: every vector publish materializes a GeoJSON
# FeatureCollection at a STABLE, per-Case key in the DURABLE runs bucket
# (the same bucket that holds the case-view snapshot + solver decks), so a
# case manifest / cold-view materializer can serve it with ZERO agent
# involvement. The .fgb stays the DATA face (analytical tools open it); the
# GeoJSON asset is the DISPLAY face (the browser fetches it).
#
# Contract:
#   bucket : TRID3NT_RUNS_BUCKET (solver._get_runs_bucket - the DURABLE runs
#            bucket, NOT the 30-day-TTL content-addressed cache bucket; a
#            published layer must outlive cache eviction).
#   key    : ``case-data/<case_id>/<layer_id>.geojson``
#   asset  : the returned ``s3://<runs_bucket>/case-data/<case_id>/<layer_id>.geojson``
#            URI - the DISPLAY face the QGIS plugin reads for the vector layer.
#   faces  : observe_published_layer(layer_id, gcs_uri=<s3 .fgb DATA>,
#            wms_url=<s3 .geojson DISPLAY>) - the GeoJSON never displaces the
#            data uri (mirrors the vector-WMS branch above).
# --------------------------------------------------------------------------- #

#: Object-key prefix for durable per-Case vector GeoJSON assets in the runs
#: bucket. Single seam so the writer and any reader name the object identically.
DURABLE_CASE_DATA_PREFIX: str = "case-data"


def durable_vector_geojson_key(case_id: str, layer_id: str) -> str:
    """Return the runs-bucket object key for a Case's durable vector GeoJSON.

    Frozen contract: ``case-data/<case_id>/<layer_id>.geojson``.
    One seam so the writer (here) and any later reader name it identically.
    """
    return f"{DURABLE_CASE_DATA_PREFIX}/{case_id}/{layer_id}.geojson"


def _vector_uri_to_geojson_bytes(layer_uri: str) -> bytes | None:
    """Read a vector artifact URI and return UTF-8 GeoJSON FeatureCollection bytes.

    REUSES the existing read + parse helpers - does NOT reimplement them:
      - ``.fgb`` bytes -> ``pipeline_emitter._fgb_bytes_to_geojson`` (pyogrio +
        geopandas; the same converter the inline path uses).
      - ``.geojson`` / ``.json`` -> validated FeatureCollection passed through.

    Source bytes are read with the SAME boto3 client every other s3
    download in this module uses (``cache.read_object_bytes_s3``); a
    local path is read directly (dev / test convenience). Returns ``None`` on
    ANY read / parse / unsupported-extension error (caller fails open).
    """
    import json as _json

    try:
        if layer_uri.startswith("s3://"):
            from trid3nt_server.tools.cache import read_object_bytes_s3

            raw = read_object_bytes_s3(layer_uri)
        elif layer_uri.startswith(("gs://", "/vsigs/")):
            # gs:// is not a live store here (MinIO/s3:// is); a gs:// vector
            # is unexpected. Fail open (caller -> benign no-op).
            return None
        else:
            with open(layer_uri, "rb") as f:
                raw = f.read()
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning(
            "publish_layer: durable-geojson source read failed uri=%s (%s: %s)",
            layer_uri,
            type(exc).__name__,
            exc,
        )
        return None

    ext = layer_uri.lower().rstrip("/").rsplit(".", 1)[-1] if "." in layer_uri else ""
    try:
        if ext == "fgb":
            from trid3nt_server.emission.pipeline_emitter import _fgb_bytes_to_geojson

            obj = _fgb_bytes_to_geojson(raw)
            if obj is None:
                return None
        elif ext in {"geojson", "json"}:
            obj = _json.loads(raw)
            if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
                logger.warning(
                    "publish_layer: durable-geojson source is not a "
                    "FeatureCollection uri=%s",
                    layer_uri,
                )
                return None
        else:
            logger.warning(
                "publish_layer: durable-geojson unsupported extension %r uri=%s",
                ext,
                layer_uri,
            )
            return None
        return _json.dumps(obj).encode("utf-8")
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning(
            "publish_layer: durable-geojson parse/dump failed uri=%s (%s: %s)",
            layer_uri,
            type(exc).__name__,
            exc,
        )
        return None


def _write_durable_vector_geojson(
    layer_uri: str, layer_id: str, case_id: str
) -> str | None:
    """Materialize a vector layer's GeoJSON to the DURABLE runs bucket.

    Reads ``layer_uri`` (FlatGeobuf / GeoJSON) to a GeoJSON FeatureCollection,
    writes it to ``s3://<runs_bucket>/case-data/<case_id>/<layer_id>.geojson``
    through the ONE object-store seam (``solver._get_s3_client`` +
    ``solver._get_runs_bucket``), and returns the durable ``s3://`` asset URI.

    FAIL-OPEN: returns ``None`` on ANY read / parse / write error (the
    caller degrades to the existing benign no-op). NEVER raises.
    """
    geojson_bytes = _vector_uri_to_geojson_bytes(layer_uri)
    if geojson_bytes is None:
        return None
    try:
        from trid3nt_server.workflows.solver.solver import (
            _get_runs_bucket,
            _get_s3_client,
        )

        bucket = _get_runs_bucket()
        key = durable_vector_geojson_key(case_id, layer_id)
        s3 = _get_s3_client()
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=geojson_bytes,
            ContentType="application/geo+json",
        )
        asset_uri = f"s3://{bucket}/{key}"
        logger.info(
            "publish_layer: durable vector GeoJSON written layer_id=%s case=%s "
            "asset=%s bytes=%d",
            layer_id,
            case_id,
            asset_uri,
            len(geojson_bytes),
        )
        return asset_uri
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning(
            "publish_layer: durable vector GeoJSON write failed layer_id=%s "
            "case=%s (%s: %s) - falling back to benign no-op",
            layer_id,
            case_id,
            type(exc).__name__,
            exc,
        )
        return None


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


def _label_from_style_preset(style_preset: str | None) -> str | None:
    """Human label for a ``style_preset``, or ``None`` if uninformative.

    A preset's label is part of what the preset IS, so it is read from the STYLE
    CONTRACT and nowhere else. The token cleanup below is the FALLBACK for a
    preset the contract does not declare - never a second table of labels.
    """
    if not style_preset:
        return None
    label = styles.preset_label(style_preset)
    if label:
        return label
    if style_preset == "auto":
        return None
    import re as _re

    # Strip a family prefix (e.g. "continuous_"/"standard_"/"categorical_")
    # and title-case what remains, so an undeclared-but-descriptive preset
    # (e.g. "continuous_ndvi") still yields a readable label ("Ndvi").
    cleaned = _re.sub(r"^(standard_|continuous_|categorical_)", "", style_preset)
    cleaned = cleaned.replace("_", " ").replace("-", " ").strip()
    return cleaned.title() if cleaned else None


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
    style_preset: str | None,
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
    2. ``style_preset`` mapped to a human label (e.g. ``"standard_hillshade"``
       -> ``"Hillshade"``).
    3. a human segment of the source ``layer_uri`` path (the parent
       directory / product-family segment -- the file stem is typically a
       cache hash or a ULID).
    4. a generic ``"Layer"`` fallback.

    Cases 2-4 append a short disambiguator (``_short_disambiguator``) so two
    derived names for the same family don't collide in the UI list.
    INVARIANT: a bare-ULID name must never reach the layer summary when any
    better signal (an explicit name, a style_preset, or a URI segment) is
    available.
    """
    if name and name.strip() and not _looks_like_ulid(name.strip()):
        return name.strip()

    label = _label_from_style_preset(style_preset) or _label_from_uri(layer_uri)
    if not label:
        label = "Layer"
    return f"{label} {_short_disambiguator(layer_id)}"


# --------------------------------------------------------------------------- #
# Style-preset resolution at the publish boundary
# --------------------------------------------------------------------------- #

def style_preset_for_publish(
    *, style_preset: str | None, quantity: str | None = None
) -> str:
    """The preset a layer publishes under, from what its producer DECLARED.

    Deliberately NOT called ``resolve_style_preset``: ``emission/styles.py``
    already owns that name for the contract lookup this delegates to. This one
    answers the boundary question - what a layer arriving at publish is styled
    as - and the whole rule is three steps:

    1. an explicit non-empty ``style_preset``: the producer named its own ramp;
    2. the declared ``quantity``, resolved through the style contract's
       ``quantity_defaults`` table;
    3. the NEUTRAL ramp, for a layer whose physical meaning nobody declared.

    A raster's QUANTITY is never inferred from its filename or its layer id. A
    name is not a measurement, so a ramp guessed from one paints a physical
    band over values that may not be in it; the neutral ramp over the field's
    own range is the honest picture of an undeclared quantity.
    """
    named = (style_preset or "").strip()
    if named:
        return named
    declared = (quantity or "").strip()
    if not declared:
        return styles.NEUTRAL_FALLBACK_PRESET
    return styles.resolve_style_preset(declared)[0]


# --------------------------------------------------------------------------- #
# The mechanism
# --------------------------------------------------------------------------- #

def publish_layer(
    layer_uri: str,
    layer_id: str | None = None,
    style_preset: str | None = None,
    project_qgs_uri: str | None = None,
    case_id: str | None = None,
    name: str | None = None,
    #: A declared SPECIALIZATION of the contract's scale for this one layer -
    #: the `.style()` modifier, a param knob, or `restyle_layer`. Absent means
    #: the contract default, which is what nearly every publish wants.
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
    """Publish a COG raster: overviews, styling, legend, registration.

    Resolves the raster's styling (rescale + colormap -> the data-driven
    legend), enforces COG overviews, registers the layer, and returns the
    raster's ``s3://`` COG URI - the QGIS plugin loads the COG directly via
    GDAL ``/vsicurl/`` and renders it from the envelope's legend/style fields.
    Vectors are a benign no-op (they already render inline via their producing
    fetch tool's GeoJSON) unless a durable per-Case GeoJSON asset or the
    dormant WMS face is asked for.

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
        style_preset: the preset naming this quantity's ramp, or ``None`` for
            AUTO selection through the resolver ladder.
        project_qgs_uri: legacy ``.qgs`` project URI; consumed only by the
            dormant ``TRID3NT_QGIS_WMS_BASE`` vector-WMS seam.
        case_id: Case identifier for case-scoped ``.qgs`` routing on the
            vector-WMS seam. Transport-only; no persistence I/O happens here.
        name: display name for the layer list. Derived from the preset label /
            a URI path segment when omitted, so a bare ULID never reaches the
            layer summary.

    Returns:
        The published raster's raw ``s3://`` COG URI (the overview-enforced
        sibling when one had to be built). Suitable as a ``LayerURI.uri``.

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
    # published URI + style_preset this function's caller does not see yet).
    # Logged here purely for observability of what the model actually sent.
    if name:
        logger.info("publish_layer: name=%r layer_id=%r", name, layer_id)

    # QGIS-native rendering: rasters publish as their raw s3:// COG URI -
    # the QGIS plugin (the only client) opens the COG directly via GDAL
    # /vsicurl/ and styles it from the envelope legend. No tile server, no
    # TRID3NT_TILE_SERVER_BASE, no XYZ template mint. The COG itself is the
    # published artifact; no .qgs mutation, no worker round-trip.
    #
    # Legacy republish:
    # old persisted cases (and pre-swap composer registrations) carry legacy
    # tile-TEMPLATE display URLs. A re-publish of one is NOT an error - UNWRAP
    # the embedded ``url=`` s3 COG (the same trick
    # ``_uri_util._unwrap_tile_template`` uses) and flow it through
    # the normal raster path below, so the envelope comes out in the NEW raw
    # ``s3://`` shape with a fresh legend stash. A template with no
    # recoverable COG is returned verbatim (degraded legacy behavior; the
    # plugin unwraps templates it rehydrates on its own).
    if layer_uri.startswith(("http://", "https://")) and "/cog/tiles/" in layer_uri:
        from trid3nt_server.tools._uri_util import _unwrap_tile_template

        unwrapped = _unwrap_tile_template(layer_uri)
        if unwrapped != layer_uri and unwrapped.startswith("s3://"):
            logger.info(
                "publish_layer: legacy tile-template input unwrapped to its "
                "s3 COG layer_id=%s cog=%s",
                layer_id,
                unwrapped,
            )
            layer_uri = unwrapped
        else:
            logger.info(
                "publish_layer: legacy tile-template input with no "
                "recoverable s3 COG - returning verbatim layer_id=%s",
                layer_id,
            )
            return layer_uri
    # publish_layer is RASTER-ONLY (see module docstring) but is repeatedly
    # handed VECTOR artifacts (roads/rivers .fgb/.geojson) that ALREADY
    # rendered inline via their producing fetch tool's GeoJSON
    # (``add_loaded_layer`` path), and GDAL cannot open a FlatGeobuf as a
    # raster. Return a BENIGN, non-error result instead - no raise (the step
    # completes GREEN), no tile template, no ``observe_published_layer``
    # registration (no hanging-tile face), and a calm function_response so
    # the agent narrates honestly and never re-calls publish_layer for the
    # vector.
    if _is_vector_uri(layer_uri):
        # Dormant seam: WHEN a QGIS Server is stood up and
        # TRID3NT_QGIS_WMS_BASE is exported, route the vector through a
        # styled WMS GetMap face (MAP=<.qgs key>&LAYERS=<id>&...). This
        # NO-OPs on the live stack TODAY: the var is unset until the infra
        # exists, so the existing benign no-op is returned and behavior is
        # byte-for-byte unchanged (vectors render inline via their
        # producing fetch tool's GeoJSON).
        wms_base = _get_qgis_wms_base()
        if wms_base:
            effective_qgs_uri = _get_effective_qgs_uri(project_qgs_uri)
            qgs_key = _parse_qgs_key(effective_qgs_uri)
            wms_url = _build_vector_wms_url(
                wms_base, layer_uri, layer_id, qgs_key
            )
            logger.info(
                "publish_layer (qgis-vector) layer_id=%s uri=%s wms=%s",
                layer_id,
                layer_uri,
                wms_url,
            )
            # Register BOTH faces: the s3:// vector (consumable DATA uri)
            # + the WMS GetMap URL (display face). ``_looks_like_wms``
            # routes the WMS URL to the wms/display slot so it never
            # displaces the s3:// data uri.
            observe_published_layer(
                layer_id, gcs_uri=layer_uri, wms_url=wms_url
            )
            return wms_url
        # When no QGIS WMS base is configured (the live stack TODAY) write a
        # DURABLE, browser-readable GeoJSON for this vector so the box-off
        # cold path can paint it. The .fgb is the browser-unreadable DATA
        # face; the GeoJSON asset is the DISPLAY face. ``case_id`` is
        # threaded by the server wrapper (``_invoke_tool_via_emitter``:
        # ``params.setdefault("case_id", ...)`` for EVERY publish_layer
        # call, raster OR vector) so an in-Case vector publish reaches here
        # with the Case bound. FAIL-OPEN: any geopandas/read/write error
        # returns the existing benign no-op (never raise).
        if case_id:
            asset_uri = _write_durable_vector_geojson(
                layer_uri, layer_id, case_id
            )
            if asset_uri is not None:
                # Register BOTH faces: the s3:// .fgb stays the DATA uri,
                # the durable s3:// GeoJSON asset is the DISPLAY face. It is
                # routed via ``wms_url`` so it NEVER displaces the data uri
                # (mirrors the WMS branch above).
                observe_published_layer(
                    layer_id, gcs_uri=layer_uri, wms_url=asset_uri
                )
                logger.info(
                    "publish_layer (durable-vector) layer_id=%s data=%s "
                    "display=%s",
                    layer_id,
                    layer_uri,
                    asset_uri,
                )
                return asset_uri
        # No Case context, or the durable write failed: fall back to the
        # existing benign no-op (vectors still render inline via their
        # producing fetch tool's GeoJSON while the agent box is awake).
        return _benign_vector_noop(layer_uri, layer_id)
    if not layer_uri.startswith("s3://"):
        raise PublishLayerError(
            "LAYER_URI_NOT_FOUND",
            f"layer_uri {layer_uri!r} is not an s3:// COG on this AWS "
            "deployment. Pass the producing tool's layer handle or its "
            "s3:// URI verbatim.",
            retryable=True,
        )
    # A no-overview COG renders SPOTTY (per-strip range requests time out
    # cold; QGIS can't downsample for low zooms), so validate the COG has
    # overviews and auto-translate to a tiled+overview COG before the
    # raster is registered. Fail-open (publishes as-is) on any error.
    layer_uri = _ensure_raster_has_overviews(layer_uri)

    # Style -> render params. The ``&rescale=..&colormap_name=..`` string
    # does not ride a tile-URL query (QGIS-native); it feeds the stashed
    # LEGEND the plugin renders from. _resolve_qgis_style_params is the
    # single resolution point:
    #   - flood depths keep the blue ramp over 0-3 m; plume concentrations
    # keep the red ramp over 0-10 mg/L (byte-for-byte);
    #   - precip / temperature / wind / drought / fuel-moisture / satellite
    #     resolve to physically-correct contract bands;
    #   - anything unknown gets a band-1 2nd/98th-percentile auto-rescale
    #     (viridis) read from the COG bytes already in hand, with a SAFE
    #     non-empty default if the stats read fails;
    #   - CATEGORICAL guard: a COG with an embedded GDAL color table (NLCD
    # land cover) gets NO rescale so the palette colorizes it
    #     - never washed out; the legend carries the palette classes.
    # _infer_style_preset is applied here for the auto/None case so the
    # raster path keeps the same default selection as before.
    effective_preset = style_preset
    if effective_preset is None or effective_preset == "auto":
        effective_preset = _infer_style_preset(layer_uri, layer_id)
    style_params = _resolve_qgis_style_params(
        effective_preset, layer_uri, override=scale, shared=shared_range)
    # DATA-DRIVEN LEGEND: derive the render KEY from the SAME resolved
    # style_params (so the legend range equals the painted range by
    # construction) and stash it keyed by the ENVELOPE uri - the raw s3://
    # COG this call returns. publish_layer returns a bare URI string, so the
    # server wrap-site rebuilds a LayerURI WITHOUT a legend; the pipeline
    # emitter's add_loaded_layer lifts the legend back out of the stash by
    # layer.uri (which now equals this s3 uri). The legend carries the
    # colormap NAME + vmin/vmax (continuous) or palette classes
    # (categorical) - everything the plugin renderer needs alongside the
    # envelope's style_preset field. Fail-open: a None legend just clears
    # the stash entry and the plugin falls back to its style_preset/default
    # rendering exactly as before.
    try:
        _legend = legend_for_published_layer(
            effective_preset, layer_uri, style_params
        )
        _stash_legend_for_uri(layer_uri, _legend)
    except Exception as exc:  # noqa: BLE001 - legend never blocks a publish
        logger.debug(
            "publish_layer legend build skipped (%s: %s)",
            type(exc).__name__,
            exc,
        )
    logger.info(
        "publish_layer (raw-cog) layer_id=%s uri=%s style_params=%s",
        layer_id,
        layer_uri,
        style_params,
    )
    # register the published layer in the session URI registry so
    # the ``flood-depth-peak-<id>``-style handle resolves to a consumable
    # DATA uri for downstream tools (Pelicun, zonal stats). Under
    # QGIS-native rendering there is no separate display face: the raw
    # s3:// COG IS both the data uri and the envelope uri the plugin renders.
    observe_published_layer(layer_id, gcs_uri=layer_uri)
    return layer_uri
