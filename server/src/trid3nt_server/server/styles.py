"""Raster style / publish-preset helpers for the WebSocket server.

Extracted from the monolith in server-refactor wave 3 (ADR 0263). Pure
predicates + a preset resolver for the ``publish_layer`` wrap-site: default a
re-published flood/depth COG that arrives with an EMPTY ``style_preset`` to
``continuous_flood_depth`` (an empty preset makes QGIS fall back to viridis and
paint a redundant styleless flood layer), and identify the raw object-store
raster LayerURIs ``emit_layer_uri`` drops (which must be converted to an
http(s) tile URL before they render). Moved verbatim (behavior-preserving);
``_core`` re-imports these names so bare-global references and monkeypatch
targets on ``trid3nt_server.server.<name>`` resolve exactly as the monolith's
did.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.execution import LayerURI


#: job duplicate-flood-layer (SAFETY NET): tokens that mark a FLOOD / DEPTH COG
#: (vs terrain / land-cover / plume / generic rasters). Used at the publish_layer
#: wrap-site so a re-publish of a flood-depth COG that arrives with an EMPTY
#: style_preset is defaulted to ``continuous_flood_depth`` (white->blue->green)
#: instead of "" -- an empty preset makes QGIS fall back to viridis and paints
#: a redundant styleless flood layer (the exact duplicate-flood-layer symptom).
#: Token-boundary matched (not substring) so e.g. ``demo`` never trips ``dem``.
_FLOOD_DEPTH_STYLE_TOKENS: frozenset[str] = frozenset(
    {"flood", "depth", "inundation", "floodepth"}
)
_DEFAULT_FLOOD_DEPTH_STYLE_PRESET: str = "continuous_flood_depth"


def _is_flood_depth_cog(layer_uri: str, layer_id: str) -> bool:
    """True when the resolved URI or layer_id tokenizes to a FLOOD/DEPTH raster.

    Token-boundary matching on non-alphanumerics so ``flood-depth-peak-<run_id>``
    and a ``.../flood_depth_peak.tif`` URI both match, while ``demo``/``dem`` do
    not. Conservative: an unrecognized raster returns False (keeps the existing
    empty-preset / QGIS-default behavior for non-flood rasters)."""
    import re as _re

    tokens = set(_re.split(r"[^a-z0-9]+", f"{layer_uri} {layer_id}".lower()))
    return bool(tokens & _FLOOD_DEPTH_STYLE_TOKENS)


def _resolve_publish_wrap_style_preset(
    *, style_preset: str | None, layer_uri: str, layer_id: str
) -> str:
    """Style preset for the publish_layer wrap-site LayerURI (job
    duplicate-flood-layer SAFETY NET).

    Honors an explicit non-empty ``style_preset`` (the LLM / tool asked for it).
    When it resolves EMPTY, default a flood/depth COG to
    ``continuous_flood_depth`` so a redundant re-publish is never styleless
    (which QGIS renders as viridis). Non-flood rasters keep ``""`` (QGIS
    default) exactly as before -- terrain auto-scales, paletted COGs use
    their embedded color table."""
    preset = (style_preset or "").strip()
    if preset:
        return preset
    if _is_flood_depth_cog(layer_uri, layer_id):
        return _DEFAULT_FLOOD_DEPTH_STYLE_PRESET
    return ""


def _is_droppable_object_store_raster(value: Any) -> bool:
    """True iff ``value`` is exactly the LayerURI class ``emit_layer_uri`` DROPS.

    The deterministic auto-publish targets precisely the LayerURIs that
    ``layer_uri_emit.emit_layer_uri`` refuses to deliver: a RENDERABLE
    RASTER carrying a raw object-store uri (``s3://`` / ``gs://``), which
    MapLibre cannot fetch. Those must be converted to an http(s) tile URL
    via publish_layer before they can render. A vector (inline-GeoJSON path),
    an http(s)-uri raster (already renderable), or any non-LayerURI return
    is NOT a candidate. ``PlumeLayerURI`` / ``SeepageLayerURI`` are LayerURI
    subclasses, so ``isinstance(..., LayerURI)`` covers them.
    """
    if not isinstance(value, LayerURI):
        return False
    if value.layer_type != "raster":
        return False
    uri = value.uri or ""
    return uri.startswith("s3://") or uri.startswith("gs://")
