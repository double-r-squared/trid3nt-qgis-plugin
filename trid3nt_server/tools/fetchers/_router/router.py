"""The router engine.

One engine: resolve spec, validate params, apply gates, dispatch by shape, read
through the cache, emit a LayerURI. An executor is a pure ``(spec, params) -> bytes``
closure; the router owns everything around it and binds the four shared seams."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math as _math
from typing import Any, Callable

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_contracts.gate_spec import GateSpec, LeverSpec

#: The canonical confirm gate for a heavy raster FETCHER. All three gated fetchers
#: (fetch_dem/topobathy/landcover) share it -- one resolution_m lever, the shared
#: estimate/pin providers; kind='fetch' (no ``confirmed`` injection -- fetchers
#: ignore it). A source declares ``confirm_gate: fetch_resolution`` to opt in.
_PROVIDERS = "trid3nt_server.gates.cards.solver_confirm"
FETCH_RESOLUTION_GATE_SPEC = GateSpec(
    kind="fetch",
    estimate_provider=f"{_PROVIDERS}:estimate_fetch_resolution",
    pin_provider=f"{_PROVIDERS}:pin_fetch_resolution",
    levers=(LeverSpec(name="fetch resolution", param="resolution_m", unit="m"),),
    title="Fetch resolution",
    rationale=(
        "A heavy raster download/merge: let the user control the resolution "
        "(and see the px-grid estimate) before the big fetch."
    ),
)


def _gate_spec_for_source(spec: SourceSpec) -> GateSpec | None:
    """Map a source's declared ``confirm_gate`` marker to its GateSpec (or None)."""
    if spec.confirm_gate == "fetch_resolution":
        return FETCH_RESOLUTION_GATE_SPEC
    return None

from .._fetch_common import _validate_bbox, round_bbox_to_resolution
from ...cache import ProvenanceRecorder, cache_key_for, is_cacheable, read_through
from .errors import (
    bbox_error_suffix,
    router_input_error,
    router_not_available_error,
    router_upstream_error,
)
from .executors import qgis_provider, raster_cog, station_timeseries
from .params import validated
from .spec import record_shape
from .transforms import tiled_mosaic

logger = logging.getLogger("trid3nt_server.tools.fetchers._router.router")

__all__ = [
    "synthesize_metadata",
    "synthesize_payload_estimator",
    "validate_params",
    "select_executor",
    "prospective_cache_key",
    "try_dispatch",
    "route",
]

#: Sentinel: no ``spec.dispatch`` condition matched (distinct from a dispatch that
#: legitimately returns ``None``, though none does today). ``route()`` proceeds to
#: its own pipeline only on this sentinel.
_NO_DISPATCH = object()

#: CONUS domain for the conus_only gate (gridmet native bounds).
_CONUS_BBOX: tuple[float, float, float, float] = (-124.77, 25.05, -67.06, 49.40)




def synthesize_metadata(spec: SourceSpec) -> AtomicToolMetadata:
    """Synthesize ``AtomicToolMetadata`` from the spec."""
    return AtomicToolMetadata(
        name=spec.name,
        ttl_class=spec.cache.ttl_class,
        source_class=spec.source_class,          # cache prefix (NOT the error prefix)
        supports_global_query=spec.supports_global_query,
        payload_mb_estimator_name="estimate_payload_mb",
        open_world_hint=True,
        # data-native resolution declarations ride from the spec onto the
        # metadata so the gate card can quote them (two-layer truth: data facts here).
        resolution_specs=spec.resolution_declarations,
        # The declared confirm gate: a heavy fetcher's resolution gate rides onto the
        # metadata, so the server gate engine reads membership here.
        gate_spec=_gate_spec_for_source(spec),
    )


def synthesize_payload_estimator(spec: SourceSpec) -> Callable[..., float]:
    """Synthesize ``estimate_payload_mb(**args) -> float`` from the spec, for the
    payload-warning seam that warns over 25 MB and blocks over 250 MB."""
    pe = spec.payload_estimate

    def _sq_deg(bbox: Any) -> float:
        if not bbox:
            cb = _CONUS_BBOX
            return (cb[2] - cb[0]) * (cb[3] - cb[1])
        try:
            w, s, e, n = bbox
            return max(0.0, e - w) * max(0.0, n - s)
        except (TypeError, ValueError):
            return 1.0

    def _n_days(start_date: Any, end_date: Any) -> int:
        try:
            d0 = _dt.date.fromisoformat(str(start_date))
            d1 = _dt.date.fromisoformat(str(end_date))
            return max(1, (d1 - d0).days + 1)
        except (TypeError, ValueError):
            return 1

    def _clip(v: float) -> float:
        # ceil_mb is the upper half of a [floor, ceil] clip; None = no cap.
        return v if pe.ceil_mb is None else min(v, pe.ceil_mb)

    def _bbox_area_coeff(kw: dict[str, Any]) -> float:
        # mb_per_sq_deg_by_param: a per-param coefficient table for the
        # bbox_area model (fetch_3dep_extra per-resolution 5/500/5000/1/200). The
        # resolved param value keys the map; absent -> default -> scalar -> 0.01.
        # No-op when unset (returns the scalar coefficient).
        table = pe.mb_per_sq_deg_by_param
        if not table:
            return pe.mb_per_sq_deg or 0.01
        pval = kw.get(table.get("param"))
        m = table.get("map") or {}
        if pval in m:
            return float(m[pval])
        if "default" in table:
            return float(table["default"])
        return pe.mb_per_sq_deg or 0.01

    def estimate_payload_mb(bbox: Any = None, **kw: Any) -> float:
        sq = _sq_deg(bbox)
        floor = pe.floor_mb
        if pe.model == "bbox_area":
            return _clip(max(floor, _bbox_area_coeff(kw) * sq))
        if pe.model == "per_feature":
            feats = (pe.features_per_sq_deg or 100.0) * sq
            return _clip(max(floor, feats * (pe.kb_per_feature or 1.0) / 1024.0))
        if pe.model == "per_station":
            stations = (pe.stations_per_sq_deg or 2.0) * sq
            n_days = _n_days(kw.get("start_date"), kw.get("end_date"))
            kb = stations * (pe.kb_per_station_per_day or 2.0) * n_days + (pe.overhead_kb or 0.0)
            return _clip(max(floor, kb / 1024.0))
        if pe.model == "tiled":
            tile_deg2 = pe.tile_deg2 or 0.5
            ntiles = max(1, int(sq / tile_deg2 + 0.999))
            return _clip(max(floor, ntiles * (pe.mb_per_tile or 0.05)))
        return _clip(max(floor, 0.01 * sq))

    return estimate_payload_mb




def validate_params(spec: SourceSpec, raw: dict[str, Any]) -> dict[str, Any]:
    """The request's params through the spec's own model, then the spec's gates,
    as the quantized dict BOTH the executor and the cache key are built from. Any
    bad input raises a source-stamped :class:`RouterInputError` before any network
    call."""
    out = validated(spec, raw)
    _apply_gates(spec, out)
    return out


def _apply_gates(spec: SourceSpec, params: dict[str, Any]) -> None:
    g = spec.gates
    bbox = None
    for pname, pspec in spec.params.items():
        if pspec.type == "bbox" and pname in params:
            bbox = params[pname]
            break
    if bbox is None:
        return
    # A bbox-class gate failure stamps the bbox param's own error suffix
    # (BBOX_INVALID, else INPUT_ERROR / INPUT_INVALID).
    bsfx = bbox_error_suffix(spec)
    if g.conus_only:
        envelope = g.conus_bbox or _CONUS_BBOX
        cw, cs, ce, cn = envelope
        w, s, e, n = bbox
        if e < cw or w > ce or n < cs or s > cn:
            raise router_input_error(
                spec.error_code_prefix, f"bbox {bbox} does not intersect CONUS {envelope}", bsfx
            )
    if g.max_bbox_deg2 is not None:
        area_deg2 = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        if area_deg2 > g.max_bbox_deg2:
            raise router_input_error(
                spec.error_code_prefix,
                f"bbox area {area_deg2:.2f} deg^2 exceeds max_bbox_deg2={g.max_bbox_deg2}",
                bsfx,
            )
    if g.max_bbox_km2 is not None:
        from .._fetch_common import _bbox_area_km2
        area_km2 = _bbox_area_km2(tuple(bbox))  # type: ignore[arg-type]
        if area_km2 > g.max_bbox_km2:
            raise router_input_error(
                spec.error_code_prefix,
                f"bbox area {area_km2:.1f} km^2 exceeds max_bbox_km2={g.max_bbox_km2}",
                bsfx,
            )




def select_executor(spec: SourceSpec) -> Callable[[SourceSpec, dict[str, Any]], bytes]:
    """Return the ``(spec, params) -> bytes`` closure for the spec's shape/transform."""
    # Borrowed-provider path: a row the session's QGIS opens through its own data
    # provider dispatches by ACCESS, ahead of the shape ladder, because the two
    # modes land on different shapes - an opened overlay is a record, a
    # materialised row is a layer - and one executor answers both.
    if (spec.ingest or {}).get("access") == qgis_provider.ACCESS:
        return qgis_provider.execute
    # A spec-declared library delegation wins over the shape dispatch. Two forms:
    #  - GENERIC library_delegate: a spec names ``hooks.delegate`` (a
    #    registered hook that calls a maintained library owning discovery+socket and
    #    returns arrays/frames). A raster spec routes through raster_cog (its
    #    fetch_source_array calls the delegate for the array); a vector spec routes
    #    through the generic library_delegate.execute (features -> FGB).
    #  - LEGACY dataretrieval: ``ingest.delegate.library == 'dataretrieval'``
    #    with a service dispatch, kept on its own module.
    #
    # Record-return path: a ``shape: record`` source produces a bare JSON dict, not a
    # LayerURI. It wins over every layer executor below: its build_request hook is the
    # PURE plan builder feeding the record dict-shaper, not the http_json vector path.
    if spec.shape == "record":
        from .executors import record as record_executor
        return record_executor.execute
    # Sidecar-write path: a source whose read ALSO yields ONE declared sidecar object
    # written next to the .fgb (fetch_buildings' tags.json). Its read is the delegate
    # seam below, so this wins over it.
    if (spec.ingest or {}).get("sidecar_write"):
        from .executors import overpass_sidecar
        return overpass_sidecar.execute
    if spec.hooks is not None and spec.hooks.delegate:
        if spec.shape == "raster-cog":
            return raster_cog.execute
        from .executors import library_delegate
        return library_delegate.execute
    if (spec.ingest or {}).get("delegate"):
        from .executors import dataretrieval_delegate
        return dataretrieval_delegate.execute
    # A vector row published through a GDAL driver (an ArcGIS query URL, an OGC
    # API - Features collection, a shapefile inside a remote ZIP) reads through
    # the vector_ogr executor: the library owns the socket, the paging and the
    # decode, so this wins over every hook-driven branch below.
    if (spec.ingest or {}).get("access") == "ogr":
        from .executors import vector_ogr
        return vector_ogr.execute
    # Chained-resolution path: a spec that declares an offset-paging
    # (next_page) or per-item detail-enrichment (enrich_plan) hook routes to the
    # chained_resolution executor (resolve-then-fetch + bounded enrichment over the
    # shared transport). The pure name->id resolve phase (resolve_build only) still
    # uses the http_json main-fetch body, so it is NOT a trigger here -- pre_resolve
    # runs it in route().
    if (spec.hooks is not None and (spec.hooks.next_page or spec.hooks.enrich_plan)) \
            or (spec.ingest or {}).get("enrich"):
        from .executors import chained_resolution
        return chained_resolution.execute
    # The HTTP fetch path: a spec that names a build_request hook, or one that
    # declares ``ingest.access: http_json`` and states its request and body as a
    # field map instead. The executor's two switches pick between them per call.
    if (spec.hooks is not None and spec.hooks.build_request) \
            or (spec.ingest or {}).get("access") == "http_json":
        from .executors import http_json
        return http_json.execute
    # No in-tree spec declares a join block, so a spec that does arrived with an
    # extension whose transform module is not present. Refuse rather than fall through
    # to the geometry-only vector read, which would silently drop the values leg.
    if spec.join is not None:
        raise router_not_available_error(
            spec.error_code_prefix,
            "this spec declares a join block but _router/transforms/join.py is not "
            "in the tree; restore it with the extension that supplies it",
        )
    if spec.shape == "raster-cog":
        ingest = spec.ingest or {}
        if "mosaic" in ingest or "tile_deg2" in ingest:
            return tiled_mosaic.execute
        # A STAC catalog source reads through the stac_raster executor (the
        # library owns search + grid + fuse); every other raster access mode
        # reads through the transport-bound raster_cog executor.
        if ingest.get("access") == "stac":
            from .executors import stac_raster
            return stac_raster.execute
        return raster_cog.execute
    if spec.shape == "vector-fgb":
        raise router_input_error(
            spec.error_code_prefix,
            "a vector-fgb row must declare HOW it is read - ingest.access: ogr for a "
            "driver-published layer, hooks.delegate for a library-owned one, or "
            "ingest.access: http_json with a field map (or a hooks.build_request "
            "pair) for a bespoke API",
            spec.input_error_suffix,
        )
    if spec.shape == "station-timeseries-fgb":
        return station_timeseries.execute
    raise router_input_error(
        spec.error_code_prefix, f"no executor for shape {spec.shape!r}", spec.input_error_suffix
    )


def resolve_style_row(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any] | None:
    """The declared ``style:`` row for THIS call, with ``by_param`` applied: a mapped
    entry overrides the base row key by key, so one source serving several variables
    gives each its own ramp, units and range."""
    row = spec.output.style
    if row is None:
        return None
    by_param = row.get("by_param")
    row = {k: v for k, v in row.items() if k != "by_param"}
    if by_param:
        mapped = (by_param.get("map") or {}).get(params.get(by_param.get("param")))
        if isinstance(mapped, dict):
            row = {**row, **mapped}
    return row


def prospective_cache_key(spec: SourceSpec, raw_params: dict[str, Any]) -> str | None:
    """The key a fetch of ``spec`` with ``raw_params`` would land its artifact under,
    else ``None``. It runs the same validation and pre-cache-key resolve ``route``
    does, so the two agree; a ``pre_resolve`` that reaches the network does that
    work again here, which is why the caller runs this off the event loop.

    ``None`` for an uncacheable source, for a frames list (one key per frame), for
    params that do not validate, and for a source whose key depends on a delegate
    round trip ``route`` owns - each simply does not reuse."""
    metadata = synthesize_metadata(spec)
    hooks = spec.hooks
    if not is_cacheable(metadata) or spec.shape == "animation_frames":
        return None
    if hooks is not None and (hooks.delegate_resolve or hooks.resolve_build):
        return None
    try:
        params = validate_params(spec, raw_params)
        if hooks is not None and hooks.pre_resolve:
            from .hooks import resolve_hook

            params = {**params, **resolve_hook(hooks.pre_resolve)(spec, params)}
    except Exception:  # noqa: BLE001 -- a request that cannot resolve has no key
        return None
    return cache_key_for(metadata, params, record_shape=record_shape(spec))


def build_layer_uri(spec: SourceSpec, params: dict[str, Any], uri: str) -> LayerURI:
    """Emit the ``LayerURI`` from ``spec.output`` (the shared emission seam)."""
    bbox = None
    for pname, pspec in spec.params.items():
        # A bbox param present-but-None (a pre_resolve that nulls bbox when an
        # alternate selector wins the cache key -- nwis state_code) yields no bbox
        # stamp; bbox_from_features or the requested bbox governs instead.
        if pspec.type == "bbox" and params.get(pname) is not None:
            b = params[pname]
            bbox = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
            break
    variable = params.get("variable") or params.get("product") or spec.source_class
    layer_id = f"{spec.source_class}-{variable}"
    # emit_bbox=false suppresses the LayerURI.bbox stamp entirely.
    if not spec.output.emit_bbox:
        bbox = None
    # Per-variable LayerURI.units (full fidelity): a JOIN spec carries the units
    # on the resolved variable (census: usd / years / percent / count), matching
    # A non-JOIN source keeps the single normalize.units stamp. Resolution can only
    # fail on an invalid variable, which the executor rejected before this point.
    units = spec.normalize.units
    # units_from_param (wqp): stamp LayerURI.units from a request param's resolved
    # value (the characteristic). No-op when unset. Overrides the static stamp.
    if spec.normalize.units_from_param:
        pv = params.get(spec.normalize.units_from_param)
        if pv is not None:
            units = str(pv)
    # units_by_param: MAP a param value to units (a per-layer source); a value absent
    # from the map -> units=None. No-op when unset.
    ubp = spec.normalize.units_by_param
    if ubp:
        units = (ubp.get("map") or {}).get(params.get(ubp.get("param")))
    # role_by_param: MAP a param value to the LayerURI role (landsat
    # thermal LST -> primary, RGB composites -> context); a value absent from the
    # map falls back to the static role. No-op when unset.
    role = spec.output.role
    rbp = spec.output.role_by_param
    if rbp:
        role = (rbp.get("map") or {}).get(params.get(rbp.get("param")), role)
    return LayerURI(
        layer_id=layer_id,
        name=spec.output.display_name or f"{spec.source_class} {variable}",
        layer_type=spec.output.layer_type,
        uri=uri,
        style=resolve_style_row(spec, params),
        role=role,
        units=units,
        bbox=bbox,
        # What the SOURCE ROW states its elevations are counted from, carried on
        # the layer it produced so a consumer reads the row it was handed. A
        # source whose data states its own zero per feature declares none here.
        vertical_datum=spec.vertical_datum or None,
    )


def try_dispatch(spec: SourceSpec, raw_params: dict[str, Any]) -> Any:
    """Cross-sibling PRE-FLIGHT dispatch: on a matching ``spec.dispatch`` condition,
    return the named sibling tool's result verbatim -- its own cache prefix, layer_id
    and name -- else the ``_NO_DISPATCH`` sentinel."""

    # The seam is deliberately narrow: ONE spec-declared target per condition, no
    # chains (a target that itself dispatches is refused, so the result is always
    # exactly one sibling's verbatim output), and evaluation on RAW params before
    # validate / gate / cache / fetch. Only the ``pass_args``-mapped params are
    # forwarded, and nothing is re-cached under this spec.
    if not spec.dispatch:
        return _NO_DISPATCH
    from trid3nt_server.tools import TOOL_REGISTRY

    from .registration import get_spec

    for d in spec.dispatch:
        raw = raw_params.get(d.param)
        if d.normalize == "lower_strip" and isinstance(raw, str):
            val = raw.strip().lower()
        else:
            val = raw
        if val not in d.equals_any:
            continue
        # NO-CHAIN guard: the dispatched target must not itself dispatch.
        target_spec = get_spec(d.to)
        if target_spec is not None and target_spec.dispatch:
            raise router_upstream_error(
                spec.error_code_prefix,
                f"dispatch target {d.to!r} itself declares a dispatch block; "
                "cross-sibling dispatch chains are forbidden",
            )
        entry = TOOL_REGISTRY.get(d.to)
        if entry is None:
            raise router_upstream_error(
                spec.error_code_prefix,
                f"dispatch target {d.to!r} is not registered",
            )
        kwargs = {targ: raw_params.get(src) for targ, src in d.pass_args.items()}
        return entry.fn(**kwargs)
    return _NO_DISPATCH


def route(
    spec: SourceSpec, raw_params: dict[str, Any]
) -> LayerURI | dict[str, Any] | list[LayerURI]:
    """The engine: validate, gate, dispatch, cache, emit. Returns a LayerURI, a record
    dict for a ``shape: record`` source, or an ordered ``list[LayerURI]`` for a
    ``shape: animation_frames`` source."""

    # Fallback-ladder control kwargs ride the same router-level absorber as
    # ``purpose=``: ``fallback=(rung, ...)`` names the alternatives THIS call site
    # tolerates and ``fallback_gate="auto"|"user_gated"`` picks the loudness mode.
    # Absent ``fallback=``, a source that declares a ladder gets primary-or-typed-
    # error; a source with no ladder is untouched. On the ladder path emit-on-fetch
    # is DEFERRED until the activation is stamped, so the layer the map shows carries
    # the same rows as the layer the composer holds.
    from trid3nt_server.fallbacks import resolve_ladder, walk_ladder

    raw_params = dict(raw_params)
    allow = raw_params.pop("fallback", None) or ()
    gate_mode = raw_params.pop("fallback_gate", None)
    ladder = resolve_ladder(spec.name, raw_params)
    if ladder is None:
        return _route_once(spec, raw_params)
    if isinstance(allow, str):
        allow = (allow,)
    pending_emit: list[tuple[dict[str, Any], Any, str | None]] = []
    result, activation = walk_ladder(
        ladder,
        params=raw_params,
        attempt=lambda _rung, params: _route_once(
            spec, params, pending_emit=pending_emit
        ),
        allow=tuple(allow),
        gate_mode=gate_mode,
    )
    result = _stamp_activation(result, activation)
    from .emit_on_fetch import maybe_emit_input_on_fetch

    if pending_emit:
        params, visualize, purpose = pending_emit[-1]
    else:
        # A rung with its own ``source`` / ``call`` served WITHOUT going through
        # _route_once, so nothing recorded the emit arguments -- the request's own
        # are the truth for it. Without this a user_supplied bed never surfaces.
        params, visualize, purpose = (
            raw_params, raw_params.get("visualize"), raw_params.get("purpose"),
        )
    for layer in (result if isinstance(result, list) else [result]):
        if isinstance(layer, LayerURI):
            maybe_emit_input_on_fetch(
                spec, params, layer, visualize=visualize, purpose=purpose
            )
    return result


def _stamp_activation(result: Any, activation: Any) -> Any:
    """Carry the ladder activation onto the result envelope and the narration."""

    # A ``shape: animation_frames`` source stamps EVERY frame (they share one fetch,
    # so they share its provenance). A ``shape: record`` source has no envelope to
    # stamp: its rows are logged LOUDLY instead, because a silent drop is the class
    # of hole this machinery exists to close. An exempted request has NO rows and
    # still stamps -- its narration is the unverified note, the only visibility that
    # serve gets -- and CLEARS the envelope's ``rung_coverage``, because an envelope
    # whose note says the shares are UNMEASURED may not carry numbers beside it.
    rows = activation.to_contract()
    note = activation.narration()
    unverified = bool(getattr(activation, "coverage_unverified", False))
    if not rows and not note and not unverified:
        return result

    def _one(layer: Any) -> Any:
        update: dict[str, Any] = {}
        if rows:
            update["fallbacks"] = rows
        if unverified and "rung_coverage" in type(layer).model_fields:
            update["rung_coverage"] = None
        if note:
            update["fallback_note"] = (
                f"{layer.fallback_note} {note}" if layer.fallback_note else note
            )
        return layer.model_copy(update=update)

    if isinstance(result, LayerURI):
        return _one(result)
    if isinstance(result, list):
        return [_one(r) if isinstance(r, LayerURI) else r for r in result]
    if activation.degraded:
        logger.warning(
            "fallback ladder %s served a non-LayerURI result (%s): the activation "
            "rows have no envelope to ride and are recorded ONLY here -- %s",
            activation.capability, type(result).__name__, note,
        )
    return result


def _route_once(
    spec: SourceSpec,
    raw_params: dict[str, Any],
    *,
    pending_emit: list[tuple[dict[str, Any], Any, str | None]] | None = None,
) -> LayerURI | dict[str, Any] | list[LayerURI]:
    """ONE attempt at the pipeline: validate, gate, dispatch, cache, emit.
    ``pending_emit``, when given, RECORDS the emit-on-fetch arguments instead of
    surfacing the layer, so the caller can emit after stamping an activation."""
    # Emit-on-fetch control kwargs: router-level, so EVERY spec inherits
    # them via the promoted signature's ``**_extra_ignored`` absorber. Popped here
    # so they never reach validation / the cache key -- ``visualize=False`` (probe
    # fetch) suppresses the in-composer input surfacing; ``purpose`` contributes one
    # word to the surfaced layer's name.
    raw_params = dict(raw_params)
    _visualize = raw_params.pop("visualize", None)
    _purpose = raw_params.pop("purpose", None)
    # Cross-sibling PRE-FLIGHT dispatch: before ANY validation / gate /
    # cache / fetch, a declared ``source``-value condition may serve the request
    # from a named sibling tool and return its result verbatim (fetch_dem
    # source="copernicus" -> fetch_copernicus_dem's layer, verbatim).
    dispatched = try_dispatch(spec, raw_params)
    if dispatched is not _NO_DISPATCH:
        return dispatched
    metadata = synthesize_metadata(spec)
    params = validate_params(spec, raw_params)
    # The ASK, on the log, for every fetch: a cache hit reaches no executor, so
    # this is the only place that states what was requested of a source.
    logger.info("fetch %s ask=%s", metadata.name, str(params)[:400])
    # Frames-list output shape: an animation source returns an ORDERED
    # list[LayerURI] (one cache entry + one layer per timestamp), so the executor
    # owns the per-frame read_through loop -- there is no single top-level
    # read_through / LayerURI. It wins over every layer executor below.
    if spec.shape == "animation_frames":
        from .executors import animation_frames
        return animation_frames.execute(spec, params, metadata)
    # A delegated spec's source-specific INPUT validation (wqp bbox-required, nldi
    # seed/comid mutual-exclusion + CONUS + comid gate) runs BEFORE read_through, so
    # a bad request raises pre-cache and pre-network. No-op otherwise.
    if spec.hooks is not None and spec.hooks.delegate:
        # generic library delegate: run the source-specific pre-cache
        # input gate (hooks.delegate_validate) before read_through. No-op when unset.
        from .executors import library_delegate
        library_delegate.pre_validate(spec, params)
    elif (spec.ingest or {}).get("delegate"):
        from .executors import dataretrieval_delegate
        dataretrieval_delegate.pre_validate(spec, params)
    # Socketed pre-cache-key delegate resolve: the HRRR-Zarr s3fs cycle
    # walk resolves the published cycle BEFORE read_through so the resolved cycle
    # merges into params and enters the cache key (a cycle=None request would else
    # compute a non-deterministic key). No-op unless the spec declares it.
    if spec.hooks is not None and spec.hooks.delegate_resolve:
        from .executors import library_delegate
        params = {**params, **library_delegate.resolve(spec, params)}
    # Chained-resolution PHASE R: resolve a name -> id BEFORE read_through
    # so the resolved id enters the cache key (a name query and its id query collapse
    # to one entry). Does the round-1 I/O; no-op unless the spec declares resolve_build.
    if spec.hooks is not None and spec.hooks.resolve_build:
        from .executors import chained_resolution
        params = chained_resolution.pre_resolve(spec, params)
    # Generic pre-cache-key resolve: a source whose cache key depends on a
    # value resolved over the shared HTTP transport (the LANCE MCDWD year->doy dir-walk
    # for a date=None latest request) names a pure-ish pre_resolve hook. Runs BEFORE
    # read_through so the resolved value enters the cache key (a non-deterministic key
    # otherwise forever serves the first-cached day). No-op unless the spec declares it.
    if spec.hooks is not None and spec.hooks.pre_resolve:
        from .hooks import resolve_hook
        params = {**params, **resolve_hook(spec.hooks.pre_resolve)(spec, params)}
    executor = select_executor(spec)

    # Record-return path: a ``shape: record`` source caches its JSON dict
    # bytes and returns the parsed dict envelope -- no LayerURI. The honesty floor is
    # intact: the record executor raises the source's typed empty/upstream errors, so
    # read_through never writes a fabricated-success sentinel.
    if spec.output.layer_type == "record":
        result = read_through(
            metadata=metadata,
            params=params,
            ext=spec.output.ext,
            fetch_fn=lambda: executor(spec, params),
            record_shape=record_shape(spec),
        )
        assert result.data is not None, "record source is cacheable; data must be set"
        return json.loads(result.data.decode("utf-8"))

    # Fetch-time provenance channel: a spec that declares
    # output.provenance rides a recorder through read_through so the delegate's
    # record_provenance() is persisted as a sidecar (fresh) and replayed from it
    # (cache hit); the recorded dict reaches the envelope hook below. No-op
    # No-op when unset: recorder=None leaves read_through unchanged.
    recorder = ProvenanceRecorder() if spec.output.provenance else None
    result = read_through(
        metadata=metadata,
        params=params,
        ext=spec.output.ext,
        fetch_fn=lambda: executor(spec, params),
        provenance=recorder,
        record_shape=record_shape(spec),
    )
    assert result.uri is not None, "router source is cacheable; uri must be set"
    # variant_by_emptiness: a source whose non-empty path is a
    # renderable LayerURI but whose empty-AOI degrade is a bare record dict + typed
    # note (fetch_fault_sources' honesty gate: a zero-fault AOI is NEVER given a
    # layer). When the produced vector FGB is feature-empty the named hook returns
    # the record dict, which route() returns INSTEAD of the LayerURI. No-op unless
    # the spec declares it (a non-empty fetch always takes the LayerURI path below).
    vbe = spec.output.variant_by_emptiness
    if vbe is not None and result.data is not None and _fgb_feature_count(result.data) == 0:
        from .hooks import resolve_hook
        return resolve_hook(vbe)(spec, params)
    layer = build_layer_uri(spec, params, result.uri)
    # bbox_from_features: stamp the camera bbox from the emitted vector
    # features' extent (point-event fetchers auto-zoom to the events). Read from
    # the produced FGB (result.data is populated on cache hit + miss), so the
    # stamp is consistent across cache paths. No-op when unset.
    bff = spec.output.bbox_from_features
    if bff is not None and result.data:
        extent = _extent_from_fgb(result.data, float(bff.get("pad", 0.1)))
        if extent is not None:
            layer = layer.model_copy(update={"bbox": extent})
    # envelope hook: a source returning a LayerURI SUBCLASS with
    # business fields computed POST-serialize names a pure envelope hook +
    # output.result_model. The hook receives the assembled layer + produced bytes
    # and returns the extra fields; the router builds the named subclass. The
    # honesty floor still owns status/error semantics -- a hook may ADD fields but
    # the router drops uri/layer_type from its return so it can never flip an error
    # to success or re-point the layer. No-op when unset.
    if spec.hooks is not None and spec.hooks.envelope:
        layer = _apply_envelope(spec, params, layer, result.data, result.provenance)
    # Emit-on-fetch: when this fetch ran NESTED inside a composer (not
    # as its own direct dispatch), surface the fetched data as a role=context input
    # so the engine's terrain / rivers / land cover are visible. Best-effort; never
    # fails the fetch. A direct chat dispatch is skipped (the wrapper emits it).
    if pending_emit is not None:
        pending_emit.append((params, _visualize, _purpose))
        return layer
    from .emit_on_fetch import maybe_emit_input_on_fetch
    maybe_emit_input_on_fetch(
        spec, params, layer, visualize=_visualize, purpose=_purpose
    )
    return layer


#: Honesty-floor-owned fields an envelope hook must never re-write (the layer
#: already points at real, successfully-produced bytes; a hook only adds fields).
_ENVELOPE_PROTECTED_KEYS = ("uri", "layer_type")


def _apply_envelope(
    spec: SourceSpec,
    params: dict[str, Any],
    layer: LayerURI,
    data: bytes | None,
    provenance: dict[str, Any] | None = None,
) -> LayerURI:
    """Build the spec's ``output.result_model`` subclass via the pure envelope hook,
    which computes extra business fields over the produced bytes with no I/O. The
    identity keys ``uri`` and ``layer_type`` are stripped, so a hook only enriches."""

    # The fetch-time provenance dict (fresh or replayed from a cache hit) is passed
    # only to a hook that DECLARES a ``provenance`` parameter, so a result model whose
    # fields ARE fetch-time provenance survives every cache path; a hook without that
    # parameter is called with the four-argument signature.
    import inspect

    from trid3nt_contracts.execution import LAYER_RESULT_MODELS

    from .hooks import resolve_hook

    hook = resolve_hook(spec.hooks.envelope)  # type: ignore[union-attr]
    if "provenance" in inspect.signature(hook).parameters:
        extra = hook(spec, params, layer, data, provenance=provenance)
    else:
        extra = hook(spec, params, layer, data)
    if not isinstance(extra, dict):
        extra = {}
    extra = {k: v for k, v in extra.items() if k not in _ENVELOPE_PROTECTED_KEYS}
    model_name = spec.output.result_model
    cls = LAYER_RESULT_MODELS.get(model_name) if model_name else None
    if cls is None:
        # No declared subclass: overlay the (non-protected) extra onto the base
        # LayerURI so a name/units override still lands (defensive; a spec that
        # declares envelope also declares result_model, validated at load).
        base_fields = set(type(layer).model_fields)
        safe = {k: v for k, v in extra.items() if k in base_fields}
        return layer.model_copy(update=safe) if safe else layer
    return cls(**{**layer.model_dump(), **extra})


def _fgb_feature_count(data: bytes) -> int:
    """The number of features in an FGB, 0 for a header-only honest-empty one. An
    UNREADABLE FGB counts as non-empty, so a read hiccup never silently swallows a
    real fetch into the empty-record degrade."""
    import os
    import tempfile

    import geopandas as gpd

    tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False, prefix="trid3nt_router_cnt_") as f:
            tmp = f.name
            f.write(data)
        return int(len(gpd.read_file(tmp)))
    except Exception:  # noqa: BLE001 -- an unreadable FGB is treated as non-empty
        return 1
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _extent_from_fgb(data: bytes, pad: float) -> tuple[float, float, float, float] | None:
    """The (west, south, east, north) extent of an FGB's features. A collapsed axis is
    padded by ``pad`` degrees so the camera cannot zoom to an infinite level; an
    unreadable or empty FGB returns None, leaving the caller its request bbox."""
    import os
    import tempfile

    import geopandas as gpd

    tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False, prefix="trid3nt_router_ext_") as f:
            tmp = f.name
            f.write(data)
        gdf = gpd.read_file(tmp)
        if gdf.empty or gdf.geometry.isna().all():
            return None
        west, south, east, north = (float(v) for v in gdf.total_bounds)
    except Exception:  # noqa: BLE001 -- never fail emission on an extent read
        return None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    if not all(_math.isfinite(v) for v in (west, south, east, north)):
        return None
    if west == east:
        west -= pad
        east += pad
    if south == north:
        south -= pad
        north += pad
    return (west, south, east, north)
