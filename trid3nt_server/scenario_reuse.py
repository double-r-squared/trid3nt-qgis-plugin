"""Deterministic expensive-simulation reuse guard.

A signature match short-circuits an expensive re-run before the solver launches;
any ambiguity RUNS instead, since a false short-circuit returns a stale answer
while a false run only costs a re-solve. Pure, synchronous, never networked."""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("trid3nt_server.scenario_reuse")

__all__ = [
    "EXPENSIVE_SCENARIO_TOOLS",
    "ScenarioSignature",
    "ScenarioResult",
    "ScenarioResultIndex",
    "scenario_signature",
    "scenario_type_for_tool",
    "layer_id_scenario_type",
    "fetched_layer_kind",
    "fetched_kind_for_tool",
    "find_reusable_fetched_layer",
    "bbox_equivalent",
    "bbox_encloses",
    "get_scenario_index",
    "reset_scenario_indexes_for_tests",
]


# --------------------------------------------------------------------------- #
# Tool to produced-layer scenario family
# --------------------------------------------------------------------------- #
#
# ``scenario_type`` is the stable layer-family token that keys the reuse index
# and recognizes an existing RESULT layer by its ``layer_id`` prefix; keep the
# values aligned with the layer_id each workflow's postprocess step mints.
# NO KEY HERE IS A REGISTERED TOOL, so the guard is inert on every live turn.
# Keying a template in is a correctness decision: a false short-circuit hands
# the user a stale answer, so a key earns its place only once its postprocess
# layer_id and its answer-changing params are both pinned.
EXPENSIVE_SCENARIO_TOOLS: dict[str, str] = {
    "sfincs_flood": "flood-depth",
    "modflow_contaminant_plume": "plume",
    "swmm_urban_flood": "swmm-depth",
}

#: ``layer_id`` prefixes that identify an existing RESULT layer of each family.
#: A flood postprocess mints ``flood-depth-peak-<run_id>``. Matching is
#: prefix/substring based so run-id suffixes do not defeat recognition.
_SCENARIO_LAYER_ID_MARKERS: dict[str, tuple[str, ...]] = {
    "flood-depth": ("flood-depth", "flood_depth", "flood-peak"),
    "plume": ("plume", "modflow", "contamination", "concentration"),
    "swmm-depth": ("swmm-depth", "swmm_depth"),
}

#: Default bbox quantization (degrees). Two AOIs whose bbox corners agree to
#: this tolerance are treated as the SAME extent for reuse. ~0.01 deg ~ 1 km;
#: deliberately coarse enough to absorb geocoder jitter on the same place name
#: but fine enough that a genuinely different AOI never collides.
_BBOX_QUANT_DEG: float = 0.02


# --------------------------------------------------------------------------- #
# Fetched / context-layer kind
# --------------------------------------------------------------------------- #
#
# A fetched layer is not a simulation RESULT and so has no scenario_type; reuse
# needs a parallel notion of KIND, the data family a fetch produces. Two loaded
# layers of the same kind covering the same or an enclosing AOI are the same
# data. Recognition is prefix/substring based on layer_id and name so a
# per-place suffix does not defeat it, and an unrecognized layer yields ``None``
# rather than a false reuse.

#: A fetch_* tool name to the produced-layer KIND token, aligned with each
#: fetcher's ``source_class`` and the ``layer_id`` prefix it mints. An absent
#: tool simply gets no fetched-kind hint.
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
#: each kind. Substring based against the lowercased ``"layer_id name"`` haystack.
#: Order matters only for disjoint kinds; the markers are chosen to be unambiguous.
_FETCHED_KIND_MARKERS: dict[str, tuple[str, ...]] = {
    "admin": ("admin-", "administrative boundar", "boundaries"),
    "roads": ("osm-roads", "osm_roads", "-roads", " roads"),
    "rivers": ("river", "waterway", "stream", "nhd"),
    "buildings": ("building",),
    "landcover": ("landcover", "land cover", "nlcd"),
    "dem": ("-dem-", "dem-", "elevation", "srtm", "3dep"),
    "population": ("hrsl", "worldpop", "population"),
}


def scenario_type_for_tool(tool_name: str) -> str | None:
    """Return the produced-layer scenario family for an expensive tool, else None."""
    return EXPENSIVE_SCENARIO_TOOLS.get(tool_name)


def layer_id_scenario_type(layer_id: str | None, name: str | None = None) -> str | None:
    """Classify a loaded layer by id and name into a scenario RESULT family, or
    ``None`` for an input or context layer such as a fetched DEM.
    """
    hay = " ".join(str(x).lower() for x in (layer_id or "", name or "") if x)
    if not hay:
        return None
    for scenario_type, markers in _SCENARIO_LAYER_ID_MARKERS.items():
        for marker in markers:
            if marker in hay:
                return scenario_type
    return None


def fetched_kind_for_tool(tool_name: str) -> str | None:
    """Return the produced-layer KIND for a fetch_* tool, else None."""
    return _FETCH_TOOL_KIND.get(tool_name)


def fetched_layer_kind(layer_id: str | None, name: str | None = None) -> str | None:
    """Classify a loaded FETCHED layer by id and name into a kind token, else
    ``None``; a layer that classifies as a simulation RESULT is deliberately not
    a fetched kind."""
    # A simulation RESULT is not a fetched layer - keep the two taxonomies
    # disjoint so the note never double-labels.
    if layer_id_scenario_type(layer_id, name) is not None:
        return None
    hay = " ".join(str(x).lower() for x in (layer_id or "", name or "") if x)
    if not hay:
        return None
    for kind, markers in _FETCHED_KIND_MARKERS.items():
        for marker in markers:
            if marker in hay:
                return kind
    return None


# --------------------------------------------------------------------------- #
# Normalization helpers
# --------------------------------------------------------------------------- #


def _normalize_location_query(q: Any) -> str | None:
    """Lower/strip/collapse a free-text place name for stable comparison."""
    if not isinstance(q, str):
        return None
    norm = re.sub(r"\s+", " ", q.strip().lower())
    norm = norm.strip(" ,.")
    return norm or None


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


def _coerce_latlon(value: Any) -> tuple[float, float] | None:
    """Coerce a 2-element (lat, lon) point, else None. No string parsing here -
    the server's ``coerce_latlon`` runs earlier; this guard only sees the
    already-normalized shape (and tolerates the raw 2-list)."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        return (float(value[0]), float(value[1]))
    except (TypeError, ValueError):
        return None


def _round_num(value: Any, ndigits: int = 4) -> float | None:
    """Round a numeric param for signature comparison, else None."""
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Signature
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScenarioSignature:
    """Normalized identity of an expensive-scenario request: equal
    ``scenario_type`` and ``key_params`` over an equivalent AOI describe two runs
    that would produce the SAME layer."""

    scenario_type: str
    # Telemetry only: two tools that produce the same family are
    # interchangeable, so this is not part of signature equality.
    tool_name: str
    bbox: tuple[float, float, float, float] | None
    bbox_q: tuple[int, int, int, int] | None
    location_norm: str | None
    key_params: frozenset[tuple[str, Any]]

    def aoi_resolvable(self) -> bool:
        """True iff this request carries an AOI we can compare WITHOUT geocoding."""
        return self.bbox_q is not None or self.location_norm is not None


def _flood_signature(tool_name: str, params: dict) -> ScenarioSignature | None:
    bbox = _coerce_bbox(params.get("bbox"))
    location_norm = _normalize_location_query(params.get("location_query"))
    if bbox is None and location_norm is None:
        # No AOI we can key on without geocoding -> cannot match safely -> RUN.
        return None
    # Key physics params, accepting the _yr/_years and _hr/_hours aliases. A
    # forcing_raster_uri makes the run a DIFFERENT physics answer, so it is
    # part of the key.
    rp = _round_num(
        params.get("return_period_yr", params.get("return_period_years")), 0
    )
    dur = _round_num(params.get("duration_hr", params.get("duration_hours")), 0)
    forcing = params.get("forcing_raster_uri")
    key: set[tuple[str, Any]] = set()
    if rp is not None:
        key.add(("return_period_yr", rp))
    if dur is not None:
        key.add(("duration_hr", dur))
    if isinstance(forcing, str) and forcing:
        key.add(("forcing_raster_uri", forcing))
    return ScenarioSignature(
        scenario_type="flood-depth",
        tool_name=tool_name,
        bbox=bbox,
        bbox_q=_quantize_bbox(bbox),
        location_norm=location_norm,
        key_params=frozenset(key),
    )


def _plume_signature(tool_name: str, params: dict) -> ScenarioSignature | None:
    loc = _coerce_latlon(params.get("spill_location_latlon"))
    contaminant = params.get("contaminant")
    rate = _round_num(params.get("release_rate_kg_s"), 6)
    duration = _round_num(params.get("duration_days"), 4)
    # A plume run is identified by its spill point + contaminant + rate +
    # duration. Without a usable spill point we cannot match safely -> RUN.
    if loc is None:
        return None
    # Treat the spill POINT as the AOI anchor (quantize a degenerate bbox around
    # it so near-equal points collide). Plume point is (lat, lon) -> build a
    # lon-first degenerate bbox for the shared quantizer.
    lat, lon = loc
    point_bbox = (lon, lat, lon, lat)
    key: set[tuple[str, Any]] = set()
    if isinstance(contaminant, str) and contaminant.strip():
        key.add(("contaminant", contaminant.strip().lower()))
    if rate is not None:
        key.add(("release_rate_kg_s", rate))
    if duration is not None:
        key.add(("duration_days", duration))
    return ScenarioSignature(
        scenario_type="plume",
        tool_name=tool_name,
        bbox=point_bbox,
        bbox_q=_quantize_bbox(point_bbox),
        location_norm=None,
        key_params=frozenset(key),
    )


def scenario_signature(tool_name: str, params: dict) -> ScenarioSignature | None:
    """Build a normalized reuse signature for an expensive-scenario tool call, or
    ``None`` when the tool is unguarded or the request lacks identity we can match
    without geocoding - no signature means no short-circuit."""
    scenario_type = scenario_type_for_tool(tool_name)
    if scenario_type is None:
        return None
    if not isinstance(params, dict):
        return None
    if scenario_type == "flood-depth":
        return _flood_signature(tool_name, params)
    if scenario_type == "plume":
        return _plume_signature(tool_name, params)
    return None


# --------------------------------------------------------------------------- #
# Result identity + per-session index
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScenarioResult:
    """The reusable identity of an already-produced expensive-scenario layer."""

    scenario_type: str
    layer_id: str
    name: str
    layer_type: str
    uri: str
    bbox: tuple[float, float, float, float] | None
    signature: ScenarioSignature | None = None


@dataclass
class ScenarioResultIndex:
    """Per-session record of expensive-scenario results already produced, keyed
    for matching by ``scenario_type``, AOI and key params.
    """

    session_id: str
    _results: list[ScenarioResult] = field(default_factory=list)

    def record_result(
        self,
        signature: ScenarioSignature | None,
        *,
        layer_id: str,
        name: str,
        layer_type: str,
        uri: str,
        bbox: Any = None,
    ) -> None:
        """Record a freshly produced expensive-scenario result for future reuse."""
        if not layer_id or not uri:
            return
        scenario_type = (
            signature.scenario_type
            if signature is not None
            else layer_id_scenario_type(layer_id, name)
        )
        if scenario_type is None:
            return
        rec_bbox = _coerce_bbox(bbox)
        if rec_bbox is None and signature is not None:
            rec_bbox = signature.bbox
        result = ScenarioResult(
            scenario_type=scenario_type,
            layer_id=layer_id,
            name=name or layer_id,
            layer_type=layer_type or "raster",
            uri=uri,
            bbox=rec_bbox,
            signature=signature,
        )
        # Replace any existing entry for the SAME layer_id (re-run refresh), else
        # append. Newest-wins ordering: move/append to the end so ``find_reuse``
        # prefers the most recent result on a tie.
        self._results = [r for r in self._results if r.layer_id != layer_id]
        self._results.append(result)
        logger.info(
            "scenario_reuse[%s]: recorded result type=%s layer_id=%s",
            self.session_id, scenario_type, layer_id,
        )

    def seed_from_loaded_layers(self, loaded_layers: Any) -> None:
        """Seed the index from a Case's persisted ``loaded_layers`` on reopen.
        A persisted summary carries no signature, so such an entry matches only
        a request that itself carries no key params."""
        known = {r.layer_id for r in self._results}
        for layer in loaded_layers or []:
            d = _layer_to_dict(layer)
            if d is None:
                continue
            layer_id = d.get("layer_id")
            if not isinstance(layer_id, str) or not layer_id:
                continue
            # Never clobber an in-session record, which carries the full
            # signature, with a signature-LESS persisted seed: the seed is
            # strictly poorer and would defeat the next short-circuit.
            if layer_id in known:
                continue
            name = d.get("name") if isinstance(d.get("name"), str) else None
            scenario_type = layer_id_scenario_type(layer_id, name)
            if scenario_type is None:
                continue
            uri = d.get("uri")
            if not isinstance(uri, str) or not uri:
                continue
            self.record_result(
                None,
                layer_id=layer_id,
                name=name or layer_id,
                layer_type=d.get("layer_type") or "raster",
                uri=uri,
                bbox=d.get("bbox"),
            )

    def find_reuse(
        self,
        request: ScenarioSignature | None,
        *,
        case_bbox: Any = None,
    ) -> ScenarioResult | None:
        """Return an existing result that CLEARLY answers ``request``, newest
        first, else ``None``; anything ambiguous returns ``None`` and the caller
        runs the scenario."""
        if request is None or not request.aoi_resolvable():
            return None
        same_family = [
            r for r in self._results if r.scenario_type == request.scenario_type
        ]
        if not same_family:
            return None
        case_bbox_t = _coerce_bbox(case_bbox)
        # Newest-first.
        for result in reversed(same_family):
            if not self._key_params_match(request, result):
                continue
            if self._aoi_match(request, result, same_family, case_bbox_t):
                return result
        return None

    @staticmethod
    def _key_params_match(
        request: ScenarioSignature, result: ScenarioResult
    ) -> bool:
        """Key physics params must agree. A result with no recorded signature
        (seeded from persistence) has UNKNOWN params - only matchable when the
        request itself carries no key params (a bare "model the flood here")."""
        if result.signature is None:
            return len(request.key_params) == 0
        return request.key_params == result.signature.key_params

    def _aoi_match(
        self,
        request: ScenarioSignature,
        result: ScenarioResult,
        same_family: list[ScenarioResult],
        case_bbox: tuple[float, float, float, float] | None,
    ) -> bool:
        # bbox-keyed request.
        if request.bbox is not None:
            # Prefer the SIGNATURE bbox: for a plume that is the degenerate
            # spill-POINT bbox, never the plume FOOTPRINT in ``result.bbox``.
            # The footprint is the fallback for persistence-seeded results.
            result_bbox = (
                result.signature.bbox if result.signature else None
            ) or result.bbox
            if result_bbox is not None:
                return bbox_equivalent(request.bbox, result_bbox)
            # Result has no bbox (persistence-seeded). Only safe to reuse when
            # the request bbox matches the Case AOI AND there is exactly one
            # result of this family (no ambiguity about which it is).
            if case_bbox is not None and len(same_family) == 1:
                return bbox_equivalent(request.bbox, case_bbox)
            return False
        # location-keyed request (no bbox): need a recorded location_norm.
        if request.location_norm is not None and result.signature is not None:
            return request.location_norm == result.signature.location_norm
        return False


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


# --------------------------------------------------------------------------- #
# Fetched-layer reuse match
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Module-level per-session index store: survives a reconnect and is shared
# across a session's sibling WebSocket connections.
# --------------------------------------------------------------------------- #

_INDEX_STORE_CAP = 256
_SESSION_SCENARIO_INDEXES: "OrderedDict[str, ScenarioResultIndex]" = OrderedDict()


def get_scenario_index(session_id: str) -> ScenarioResultIndex:
    """Return (creating if needed) the scenario-result index for ``session_id``."""
    idx = _SESSION_SCENARIO_INDEXES.get(session_id)
    if idx is None:
        while len(_SESSION_SCENARIO_INDEXES) >= _INDEX_STORE_CAP:
            _SESSION_SCENARIO_INDEXES.popitem(last=False)
        idx = ScenarioResultIndex(session_id=session_id)
        _SESSION_SCENARIO_INDEXES[session_id] = idx
    return idx


def reset_scenario_indexes_for_tests() -> None:
    """Test hook - wipe the module-level store."""
    _SESSION_SCENARIO_INDEXES.clear()
