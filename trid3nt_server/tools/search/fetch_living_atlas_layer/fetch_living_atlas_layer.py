"""``fetch_living_atlas_layer`` - one harvested catalog entry, or a raw ArcGIS
service URL, to published bytes. A DYNAMIC ``SourceSpec`` is built per call from
the entry's service type and routed like any other, so nothing here re-implements
validation, the payload gate, typed errors, caching or emission. ``source_class``
is per-item, so one key can never collide two layers at the same bbox."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from trid3nt_contracts.execution import LivingAtlasLayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.search.living_atlas_common import (
    SERVICE_TYPES,
    LivingAtlasEntry,
    get_entry,
)

__all__ = ["fetch_living_atlas_layer", "estimate_payload_mb"]

logger = logging.getLogger(
    "trid3nt_server.tools.search.fetch_living_atlas_layer.fetch_living_atlas_layer"
)

_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)




class LivingAtlasInputError(ValueError):
    """Unknown item / bad request (not retryable)."""

    error_code = "LIVING_ATLAS_INPUT_INVALID"
    retryable = False


class LivingAtlasSubscriptionError(RuntimeError):
    """The item is ESRI premium/subscription content and no token is registered.
    Surfaced honestly, never a silent half-fetch - the same contract a keyed
    fetcher has with no API key."""

    error_code = "LIVING_ATLAS_SUBSCRIPTION_REQUIRED"
    retryable = False


# URL shaping + service probe.
#
# Service type -> router mode:
#   Image Service   -> raster-cog / imageserver_export (exportImage GeoTIFF)
#   Map Service     -> raster-cog / mapserver_export   (server-symbolized RGBA COG)
#   Feature Service -> vector-fgb / esri_json          (FeatureServer /query -> FGB)


def _split_service(url: str, suffix: str) -> tuple[str, str]:
    """``(base, service_name)`` for an ImageServer/MapServer URL, split so that
    ``{base}/{service_name}/{suffix}`` reconstructs the original exactly - the
    export modes rebuild it that way before appending their own verb."""
    stripped = url.rstrip("/")
    low = stripped.lower()
    tail = "/" + suffix.lower()
    if low.endswith(tail):
        stripped = stripped[: -len(tail)]
    base, _, service = stripped.rpartition("/")
    return base, service


def _probe_service(url: str) -> dict[str, Any]:
    """The service's ``?f=json`` metadata dict. A token-required or forbidden
    envelope raises :class:`LivingAtlasSubscriptionError`; any OTHER failure returns
    ``{}`` so the fetch proceeds and the router surfaces the real error."""
    from trid3nt_server.tools.fetchers._router.transport import (
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
                       "emit_bbox": False},
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
                       "emit_bbox": False},
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
                       "emit_bbox": True},
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


# Payload estimator (the tool-payload-warning seam resolves this by name).


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
    """Fetch ONE ESRI Living Atlas layer and publish it, clipped to `bbox`.

    ROUTING: after `search_living_atlas` returns an entry worth pulling bytes for,
    or when an ArcGIS REST service URL is already in hand. NOT to rank or browse -
    that is `search_living_atlas`. NOT for a dataset with a dedicated fetcher, which
    gives native resolution and richer typing.

    Give either `item_id` (an id from the search) or `service_url` (a raw
    `.../ImageServer`, `.../MapServer` or `.../FeatureServer[/<n>]`). `bbox` is
    REQUIRED, EPSG:4326, and should respect the entry's own extent.

    Returns a `LivingAtlasLayerURI`: a layer plus `curation` (authoritative or
    community), `item_id`, `service_type` and `provenance`; the bytes are a COG for
    Image/Map Services and FlatGeobuf for Feature Services. Premium content raises
    LIVING_ATLAS_SUBSCRIPTION_REQUIRED rather than half-fetching; an unknown item,
    missing bbox or bad URL raises LIVING_ATLAS_INPUT_INVALID.
    """
    from trid3nt_server.tools.fetchers._router import router

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
