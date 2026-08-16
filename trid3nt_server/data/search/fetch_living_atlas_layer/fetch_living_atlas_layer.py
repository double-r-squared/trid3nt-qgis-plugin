"""``fetch_living_atlas_layer``: fetch ONE discovered ESRI Living Atlas layer.

A generic bridge from a harvested catalog entry (or a raw ArcGIS service URL) to
published bytes. It builds a DYNAMIC ``SourceSpec`` per call from the entry's
service type and hands it to the router's ``route()`` -- which is registry-free and
spec-driven, so the ad-hoc spec rides EVERYTHING for free: param validation, the
payload gate, typed errors, ``read_through`` caching, and LayerURI emission.
No pre-registration; no bespoke transport.

Service-type -> router mode:
  - Image Service   -> raster-cog / imageserver_export (exportImage GeoTIFF)
  - Map Service     -> raster-cog / mapserver_export   (server-symbolized RGBA COG)
  - Feature Service -> vector-fgb  / esri_json          (FeatureServer /query -> FGB)

NATE's two-pool rule surfaces in the envelope: the returned ``LivingAtlasLayerURI``
labels the layer's curation class (authoritative | community). Premium /
subscription items -> an honest typed ``LIVING_ATLAS_SUBSCRIPTION_REQUIRED`` error
(missing-key parity: no ArcGIS token is registered, so premium content is never
silently half-fetched).

Cache disambiguation: ``source_class`` is per-item (``living_atlas_<item_id>``) so
the content-addressed cache key (``sha256(source_class || params || ttl)``) never
collides two different layers at the same bbox.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from trid3nt_contracts.execution import LivingAtlasLayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool
from trid3nt_server.data.search.living_atlas_common import (
    SERVICE_TYPES,
    LivingAtlasEntry,
    get_entry,
)

__all__ = ["fetch_living_atlas_layer", "estimate_payload_mb"]

logger = logging.getLogger(
    "trid3nt_server.data.search.fetch_living_atlas_layer.fetch_living_atlas_layer"
)

_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)


# --------------------------------------------------------------------------- #
# Typed errors (error_code + retryable, the fetcher honesty-floor surface).
# --------------------------------------------------------------------------- #


class LivingAtlasInputError(ValueError):
    """Unknown item / bad request (not retryable)."""

    error_code = "LIVING_ATLAS_INPUT_INVALID"
    retryable = False


class LivingAtlasSubscriptionError(RuntimeError):
    """The item is ESRI premium/subscription content and no token is registered.

    Missing-key parity: surfaced honestly and never silently, exactly like a
    keyed fetcher with no API key.
    """

    error_code = "LIVING_ATLAS_SUBSCRIPTION_REQUIRED"
    retryable = False


# --------------------------------------------------------------------------- #
# URL shaping + service probe.
# --------------------------------------------------------------------------- #


def _split_service(url: str, suffix: str) -> tuple[str, str]:
    """``(base, service_name)`` for an ImageServer/MapServer URL.

    ``{base}/{service_name}/{suffix}`` reconstructs the original service URL, which
    is exactly what ``imageserver_export`` / ``mapserver_export`` rebuild before
    appending ``/exportImage`` or ``/export``.
    """
    stripped = url.rstrip("/")
    low = stripped.lower()
    tail = "/" + suffix.lower()
    if low.endswith(tail):
        stripped = stripped[: -len(tail)]
    base, _, service = stripped.rpartition("/")
    return base, service


def _probe_service(url: str) -> dict[str, Any]:
    """GET ``<service_url>?f=json`` through the shared transport.

    Returns the parsed metadata dict. A token-required / forbidden error envelope
    raises :class:`LivingAtlasSubscriptionError`; any other failure returns ``{}``
    (the fetch proceeds and the router surfaces the real upstream error).
    """
    from trid3nt_server.data.fetchers._router.transport import (
        TransportError,
        get_bytes,
        get_client,
    )

    try:
        body, _ct, _u = get_bytes(
            get_client(), url.rstrip("/"),
            headers={"User-Agent": _USER_AGENT}, params={"f": "json"},
        )
    except TransportError:
        return {}
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return {}
    if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
        err = obj["error"]
        code = err.get("code")
        msg = str(err.get("message", ""))
        if code in (403, 498, 499) or "token" in msg.lower() or "subscription" in msg.lower():
            raise LivingAtlasSubscriptionError(
                f"ESRI Living Atlas service requires a subscription/token (no ArcGIS token "
                f"is registered): {msg or code} [{url}]"
            )
        return {}
    return obj if isinstance(obj, dict) else {}


def _feature_query_url(service_url: str) -> str:
    """Resolve the FeatureServer ``/query`` endpoint (layer index probed if absent)."""
    base = service_url.rstrip("/")
    if re.search(r"/FeatureServer/\d+$", base, re.IGNORECASE):
        return f"{base}/query"
    # A FeatureServer root -> pick the first sublayer id (default 0).
    meta = _probe_service(base)
    layers = meta.get("layers") if isinstance(meta, dict) else None
    layer_id = 0
    if isinstance(layers, list) and layers and isinstance(layers[0], dict):
        try:
            layer_id = int(layers[0].get("id", 0))
        except (TypeError, ValueError):
            layer_id = 0
    return f"{base}/{layer_id}/query"


# --------------------------------------------------------------------------- #
# Dynamic SourceSpec builders (one per service type).
# --------------------------------------------------------------------------- #


def _base_spec(entry_id: str, source_class: str) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "name": "fetch_living_atlas_layer",
        "source_class": source_class,
        "error_prefix": "LIVING_ATLAS",
        "supports_global_query": False,
        "auth": {"mode": "none", "user_agent": _USER_AGENT},
        "cache": {"ttl_class": "static-30d"},
    }


def _build_dynamic_spec(entry: LivingAtlasEntry):
    """Construct the per-call ``SourceSpec`` for the entry's service type."""
    from trid3nt_contracts.source_spec import SourceSpec

    source_class = f"living_atlas_{entry.id}"
    spec = _base_spec(entry.id, source_class)

    if entry.service_type == "Image Service":
        base, service = _split_service(entry.service_url, "ImageServer")
        spec.update({
            "shape": "raster-cog",
            "endpoints": {"data": {"url": base}},
            "params": {
                "bbox": {"type": "bbox", "required": True, "quantize": "round_6dp",
                         "error_suffix": "BBOX_INVALID"},
                "_svc": {"type": "enum", "default": "s", "values": ["s"]},
            },
            "ingest": {
                "access": "imageserver_export",
                "imageserver": {
                    "service_by_param": {"param": "_svc", "map": {"s": service}},
                    "native_cell_m": 30.0, "px_min": 16, "px_max": 4096,
                    "export_query": {"bboxSR": "4326", "imageSR": "4326",
                                     "format": "tiff", "f": "image"},
                },
            },
            "normalize": {"crs": "EPSG:4326", "orientation": "north_up",
                          "quantity": "living_atlas"},
            "output": {"layer_type": "raster", "ext": "tif", "role": "primary",
                       "style_preset": "", "emit_bbox": False},
            "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 1.0, "floor_mb": 0.05},
        })
    elif entry.service_type == "Map Service":
        base, service = _split_service(entry.service_url, "MapServer")
        spec.update({
            "shape": "raster-cog",
            "endpoints": {"data": {"url": base}},
            "params": {
                "bbox": {"type": "bbox", "required": True, "quantize": "round_6dp",
                         "error_suffix": "BBOX_INVALID"},
                "_svc": {"type": "enum", "default": "s", "values": ["s"]},
            },
            "ingest": {
                "access": "mapserver_export",
                "mapserver": {
                    "service_by_param": {"param": "_svc", "map": {"s": service}},
                    "res_deg": 0.0005, "px_min": 16, "px_max": 2048,
                    "export_query": {"bboxSR": "4326", "imageSR": "4326",
                                     "format": "png32", "transparent": "true", "f": "image"},
                },
            },
            "normalize": {"crs": "EPSG:4326"},
            "output": {"layer_type": "raster", "ext": "tif", "role": "primary",
                       "style_preset": "", "emit_bbox": False},
            "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 1.0, "floor_mb": 0.05},
        })
    elif entry.service_type == "Feature Service":
        query_url = _feature_query_url(entry.service_url)
        spec.update({
            "shape": "vector-fgb",
            "endpoints": {"data": {"url": query_url}},
            "params": {
                "bbox": {"type": "bbox", "required": True, "quantize": "round_6dp",
                         "error_suffix": "BBOX_INVALID"},
            },
            "gates": {"max_features": 30000},
            "ingest": {
                "esri_json": True,
                "geometry_envelope": "json",
                "query_template": {"out_fields": "*", "f": "json"},
                "pagination": {"mode": "result_offset", "page_size": 2000},
            },
            "normalize": {"crs": "EPSG:4326"},
            "output": {"layer_type": "vector", "ext": "fgb", "role": "primary",
                       "style_preset": "", "emit_bbox": True},
            "payload_estimate": {"model": "per_feature", "features_per_sq_deg": 500.0,
                                 "kb_per_feature": 2.0, "floor_mb": 0.02},
        })
    else:
        raise LivingAtlasInputError(
            f"unsupported Living Atlas service_type {entry.service_type!r} "
            f"(supported: {', '.join(SERVICE_TYPES)})"
        )
    return SourceSpec.model_validate(spec)


def _adhoc_entry(service_url: str) -> LivingAtlasEntry:
    """Build a minimal entry from a raw service URL not in the catalog (probe type)."""
    url = service_url.strip().rstrip("/")
    if re.search(r"/ImageServer$", url, re.IGNORECASE):
        service_type = "Image Service"
    elif re.search(r"/MapServer$", url, re.IGNORECASE):
        service_type = "Map Service"
    elif re.search(r"/FeatureServer(/\d+)?$", url, re.IGNORECASE):
        service_type = "Feature Service"
    else:
        raise LivingAtlasInputError(
            f"cannot infer service type from URL {service_url!r}; expected a URL ending in "
            "/ImageServer, /MapServer, or /FeatureServer[/<n>]"
        )
    # A raw URL carries no curation badge -> treat as community (never authoritative
    # by assumption; the honesty floor forbids inventing an authoritative label).
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", url)[-48:].strip("_") or "adhoc"
    return LivingAtlasEntry(
        id=f"url_{slug}", title=service_url, service_url=service_url,
        service_type=service_type, curation="community", authoritative=False,
    )


# --------------------------------------------------------------------------- #
# Payload estimator (the tool-payload-warning seam resolves this by name).
# --------------------------------------------------------------------------- #


def estimate_payload_mb(bbox: Any = None, **_kw: Any) -> float:
    """Coarse bbox-area payload estimate for the pre-flight warning gate."""
    if not bbox:
        return 1.0
    try:
        w, s, e, n = bbox
        area = max(0.0, float(e) - float(w)) * max(0.0, float(n) - float(s))
    except (TypeError, ValueError):
        return 1.0
    return max(0.05, 1.0 * area)


# --------------------------------------------------------------------------- #
# The registered tool.
# --------------------------------------------------------------------------- #


# The OUTER tool does not cache itself -- route() caches under the per-item dynamic
# source_class -- so it is live-no-cache (cacheable=False's required pairing).
_FETCH_LIVING_ATLAS_METADATA = AtomicToolMetadata(
    name="fetch_living_atlas_layer",
    ttl_class="live-no-cache",
    source_class="living_atlas_fetch",
    cacheable=False,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


@register_tool(_FETCH_LIVING_ATLAS_METADATA, open_world_hint=True)
def fetch_living_atlas_layer(
    item_id: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    service_url: str | None = None,
    **_extra_ignored: Any,
) -> LivingAtlasLayerURI:
    """Fetch one ESRI Living Atlas layer (discovered via ``search_living_atlas``).

    **What it does:** Takes a Living Atlas item id (or a raw ArcGIS service URL),
    builds the right ArcGIS request for its service type, downloads the layer
    clipped to ``bbox``, publishes it (GeoTIFF COG for Image/Map Services,
    FlatGeobuf for Feature Services), and returns a curation-labelled layer.

    **When to use:**
    - After ``search_living_atlas`` returns an entry you want to pull bytes for.
    - You have an ESRI Living Atlas / ArcGIS REST service URL to fetch directly.

    **When NOT to use:**
    - To RANK/BROWSE the Living Atlas -> ``search_living_atlas`` (this fetches one).
    - For a US dataset with a dedicated fetcher (DEM, land cover, flood zones) ->
      call that fetcher (native resolution, richer typing).

    **Two-pool label + premium honesty:** the returned envelope's ``curation`` field
    reports authoritative vs community. Premium/subscription items raise
    ``LIVING_ATLAS_SUBSCRIPTION_REQUIRED`` (no ArcGIS token is registered) -- never a
    silent half-fetch.

    **Parameters:**
        item_id: the Living Atlas item id from ``search_living_atlas`` (its ``id``).
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326. Required
            (``supports_global_query=False``; respect the entry's extent).
        service_url: alternative to ``item_id`` -- a raw ArcGIS REST service URL
            (``.../ImageServer`` | ``.../MapServer`` | ``.../FeatureServer[/<n>]``).

    **Returns:** a ``LivingAtlasLayerURI`` (a ``LayerURI`` plus ``curation``,
    ``item_id``, ``service_type``, ``provenance``). ``uri`` -> the published COG/FGB.

    **Error types:**
        - ``LIVING_ATLAS_INPUT_INVALID``: unknown item / missing bbox / bad URL.
        - ``LIVING_ATLAS_SUBSCRIPTION_REQUIRED``: premium content, no token.
        - ``LIVING_ATLAS_*``: router-stamped upstream/empty errors from the fetch.
    """
    from trid3nt_server.data.fetchers._router import router

    # Resolve the entry: catalog first, then a raw-URL ad-hoc entry.
    curation: str
    if item_id:
        resolved = get_entry(str(item_id))
        if resolved is None and service_url:
            entry = _adhoc_entry(service_url)
            curation = "community"
        elif resolved is None:
            raise LivingAtlasInputError(
                f"unknown Living Atlas item_id {item_id!r} (not in the harvested catalog); "
                "pass service_url= to fetch a raw ArcGIS service URL"
            )
        else:
            entry, curation = resolved
    elif service_url:
        resolved = get_entry(str(service_url))
        if resolved is not None:
            entry, curation = resolved
        else:
            entry = _adhoc_entry(service_url)
            curation = "community"
    else:
        raise LivingAtlasInputError("fetch_living_atlas_layer requires item_id= or service_url=")

    # Premium/subscription honesty gate (harvest signal); the probe below catches
    # any premium item the harvest did not flag.
    if entry.premium:
        raise LivingAtlasSubscriptionError(
            f"Living Atlas item {entry.id!r} ({entry.title!r}) is ESRI premium/subscription "
            "content and no ArcGIS token is registered; cannot fetch."
        )
    # Probe the service (also raises the typed subscription error on token-required).
    _probe_service(entry.service_url)

    spec = _build_dynamic_spec(entry)
    layer = router.route(spec, {"bbox": bbox})
    # route() returns a LayerURI (or, defensively, a list for animation shapes --
    # never for these declarative modes). Wrap it with the curation envelope.
    if isinstance(layer, list):
        layer = layer[0]

    provenance = {
        "item_id": entry.id,
        "service_type": entry.service_type,
        "service_url": entry.service_url,
        "curation": curation,
        "authoritative": entry.authoritative,
        "owner": entry.owner,
        "source": "ESRI Living Atlas of the World",
    }
    result = LivingAtlasLayerURI(
        **layer.model_dump(),
        curation=curation,
        item_id=entry.id,
        service_type=entry.service_type,
        provenance=provenance,
    )
    logger.info(
        "fetch_living_atlas_layer item=%s type=%s curation=%s uri=%s",
        entry.id, entry.service_type, curation, result.uri,
    )
    return result
