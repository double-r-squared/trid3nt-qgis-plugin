"""Reuse of a layer already on the map, in place of a repeat fetch.

A fetch tool and a loaded layer each classify into one KIND token; a loaded
layer of the request's kind whose extent ENCLOSES the requested AOI answers the
request, and the dispatch swaps in the registry shim below so the same
emit-tool-call gate fires with that layer. Anything ambiguous re-fetches: a
false reuse hands back the wrong data, a false fetch only costs a fetch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trid3nt_contracts.execution import LayerURI

#: A ``fetch_*`` tool name to the KIND token its layer carries, aligned with each
#: fetcher's ``source_class`` and the ``layer_id`` prefix it mints. An absent tool
#: has no kind and never reaches the reuse check.
_FETCH_TOOL_KIND: dict[str, str] = {
    "fetch_administrative_boundaries": "admin",
    "fetch_roads_osm": "roads",
    "fetch_river_geometry": "rivers",
    "fetch_buildings": "buildings",
    "fetch_landcover": "landcover",
    "fetch_dem": "dem",
    "fetch_hrsl_population": "population",
    "fetch_population": "population",
}

#: ``layer_id`` / name markers that identify an already-loaded FETCHED layer of
#: each kind. Substring based against the lowercased ``"layer_id name"`` haystack,
#: so a per-place suffix does not defeat recognition; an unrecognized layer gets
#: no kind rather than a false one.
_FETCHED_KIND_MARKERS: dict[str, tuple[str, ...]] = {
    "admin": ("admin-", "administrative boundar", "boundaries"),
    "roads": ("osm-roads", "osm_roads", "-roads", " roads"),
    "rivers": ("river", "waterway", "stream", "nhd"),
    "buildings": ("building",),
    "landcover": ("landcover", "land cover", "nlcd"),
    "dem": ("-dem-", "dem-", "elevation", "srtm", "3dep"),
    "population": ("hrsl", "worldpop", "population"),
}

#: Markers of a simulation RESULT. A result is computed, not fetched, so a layer
#: carrying one is never offered to a repeat fetch however its reach or place
#: name reads - a reach called "Green River" must not answer a river fetch.
_RESULT_LAYER_MARKERS: tuple[str, ...] = (
    "flood-depth", "flood_depth", "flood-peak", "plume", "modflow",
    "contamination", "concentration", "swmm-depth", "swmm_depth",
)

#: Default bbox quantization (degrees). Two AOIs whose bbox corners agree to this
#: tolerance are the SAME extent; ~0.02 deg is coarse enough to absorb geocoder
#: jitter on one place name and fine enough that a different AOI never collides.
_BBOX_QUANT_DEG: float = 0.02


def fetched_kind_for_tool(tool_name: str) -> str | None:
    """Return the produced-layer KIND for a fetch_* tool, else None."""
    return _FETCH_TOOL_KIND.get(tool_name)


def _layer_haystack(layer_id: str | None, name: str | None) -> str:
    return " ".join(str(x).lower() for x in (layer_id or "", name or "") if x)


def _is_result_layer(layer_id: str | None, name: str | None = None) -> bool:
    """True when a loaded layer reads as a simulation RESULT rather than fetched
    data."""
    hay = _layer_haystack(layer_id, name)
    return bool(hay) and any(m in hay for m in _RESULT_LAYER_MARKERS)


def fetched_layer_kind(layer_id: str | None, name: str | None = None) -> str | None:
    """Classify a loaded FETCHED layer by id and name into a kind token, else
    ``None``; a layer that reads as a simulation RESULT is deliberately not a
    fetched kind, so the two taxonomies stay disjoint."""
    if _is_result_layer(layer_id, name):
        return None
    hay = _layer_haystack(layer_id, name)
    if not hay:
        return None
    for kind, markers in _FETCHED_KIND_MARKERS.items():
        for marker in markers:
            if marker in hay:
                return kind
    return None


def _coerce_bbox(value: Any) -> tuple[float, float, float, float] | None:
    """Coerce a 4-element bbox (list/tuple) of floats, else None."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        b = tuple(float(x) for x in value)
    except (TypeError, ValueError):
        return None
    return b  # type: ignore[return-value]


def _quantize_bbox(
    bbox: tuple[float, float, float, float] | None,
    quant: float = _BBOX_QUANT_DEG,
) -> tuple[int, int, int, int] | None:
    """Snap a bbox to a quantization grid so near-equal extents key identically."""
    if bbox is None or quant <= 0:
        return None
    return tuple(round(c / quant) for c in bbox)  # type: ignore[return-value]


def bbox_equivalent(
    a: Any,
    b: Any,
    quant: float = _BBOX_QUANT_DEG,
) -> bool:
    """True iff two bboxes are the SAME extent within the quantization tolerance."""
    qa = _quantize_bbox(_coerce_bbox(a), quant)
    qb = _quantize_bbox(_coerce_bbox(b), quant)
    return qa is not None and qa == qb


def bbox_encloses(
    outer: Any,
    inner: Any,
    quant: float = _BBOX_QUANT_DEG,
) -> bool:
    """True iff ``outer`` covers ``inner`` within the quantization tolerance; a
    request contained by an already-loaded extent is answered by that layer,
    while one that pokes outside it is genuinely new data."""
    o = _coerce_bbox(outer)
    i = _coerce_bbox(inner)
    if o is None or i is None:
        return False
    tol = quant if quant > 0 else 0.0
    # outer = (min_lon, min_lat, max_lon, max_lat); inner must sit inside it.
    return (
        o[0] - tol <= i[0]
        and o[1] - tol <= i[1]
        and o[2] + tol >= i[2]
        and o[3] + tol >= i[3]
    )


def _layer_to_dict(layer: Any) -> dict | None:
    """Coerce a loaded-layer entry (dict or pydantic summary) to a plain dict."""
    if isinstance(layer, dict):
        return layer
    if hasattr(layer, "model_dump") and callable(layer.model_dump):
        try:
            return layer.model_dump(mode="json")
        except Exception:  # noqa: BLE001 - non-pydantic duck
            return None
    return None


@dataclass(frozen=True)
class FetchedLayerMatch:
    """An already-loaded FETCHED layer that answers a repeat fetch request;
    ``layer_id`` IS the handle, so a fit or resize follow-up passes it straight
    to a bounds call instead of re-fetching."""

    kind: str
    layer_id: str
    name: str
    layer_type: str
    uri: str
    bbox: tuple[float, float, float, float] | None


def find_reusable_fetched_layer(
    tool_name: str,
    params: Any,
    loaded_layers: Any,
    *,
    case_bbox: Any = None,
) -> FetchedLayerMatch | None:
    """Return an already-loaded fetched layer that ANSWERS a fetch request, else
    ``None``: the tool must be a recognized fetcher and a loaded layer of the same
    kind must ENCLOSE the requested AOI, or the caller re-fetches."""
    kind = fetched_kind_for_tool(tool_name)
    if kind is None:
        return None
    if not isinstance(params, dict):
        params = {}
    req_bbox = _coerce_bbox(params.get("bbox"))
    if req_bbox is None:
        req_bbox = _coerce_bbox(case_bbox)
    if req_bbox is None:
        # No AOI we can compare without geocoding -> conservative: re-fetch.
        return None
    # Newest-first so a refreshed layer wins on a tie.
    for layer in reversed(list(loaded_layers or [])):
        d = _layer_to_dict(layer)
        if d is None:
            continue
        layer_id = d.get("layer_id")
        if not isinstance(layer_id, str) or not layer_id:
            continue
        name = d.get("name") if isinstance(d.get("name"), str) else None
        if fetched_layer_kind(layer_id, name) != kind:
            continue
        layer_bbox = _coerce_bbox(d.get("bbox"))
        # A loaded layer with an extent must ENCLOSE the request (same box or a
        # tighter fit). With no recorded bbox, a same-kind layer answers only a
        # request at the Case AOI, the extent it was fetched at.
        if layer_bbox is not None:
            if not bbox_encloses(layer_bbox, req_bbox):
                continue
        else:
            cbb = _coerce_bbox(case_bbox)
            if cbb is None or not bbox_equivalent(cbb, req_bbox):
                continue
        uri = d.get("uri")
        return FetchedLayerMatch(
            kind=kind,
            layer_id=layer_id,
            name=name or layer_id,
            layer_type=d.get("layer_type") or "vector",
            uri=uri if isinstance(uri, str) else "",
            bbox=layer_bbox,
        )
    return None


@dataclass
class _ReuseEntry:
    """A drop-in ``RegisteredTool``-shaped shim for the reuse short-circuit: the
    real tool's ``metadata``, so the card and telemetry label are unchanged, with
    an ``fn`` that returns the EXISTING layer instead of re-fetching it."""

    metadata: Any
    layer: "LayerURI"

    @property
    def fn(self) -> Any:
        layer = self.layer

        def _return_existing(**_ignored: Any) -> "LayerURI":
            return layer

        return _return_existing
