"""The router engine.

One engine: resolve row, validate params, apply gates, dispatch by shape, read
through the cache, emit a LayerURI. An executor is a pure ``(spec, params) -> bytes``
closure; the router owns everything around it and binds the four shared seams."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math as _math
from typing import Any, Callable

from trid3nt_contracts.coverage import PER_RECORD
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_contracts.gate_spec import GateSpec, LeverSpec

#: The confirm gate for a heavy raster fetcher (one resolution_m lever, no ``confirmed`` injection);
#: a source opts in with ``confirm_gate: fetch_resolution``.
_PROVIDERS = "trid3nt_server.inputs.gate.cards.fetch_resolution"
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

#: Sentinel: no ``spec.dispatch`` condition matched; ``route()`` proceeds only on this.
_NO_DISPATCH = object()

_CONUS_BBOX: tuple[float, float, float, float] = (-124.77, 25.05, -67.06, 49.40)




def synthesize_metadata(spec: SourceSpec) -> AtomicToolMetadata:
    """Synthesize ``AtomicToolMetadata`` from the row; an ``internal_only`` row is tier="internal" (off the pool and the index)."""
    return AtomicToolMetadata(
        name=spec.name,
        ttl_class=spec.cache.ttl_class,
        source_class=spec.source_class,          # cache prefix (NOT the error prefix)
        supports_global_query=spec.supports_global_query,
        payload_mb_estimator_name="estimate_payload_mb",
        open_world_hint=True,
        tier="internal" if spec.internal_only else "general",
        gate_spec=_gate_spec_for_source(spec),
    )


def synthesize_payload_estimator(spec: SourceSpec) -> Callable[..., float]:
    """Synthesize ``estimate_payload_mb(**args) -> float`` from the row for the 25 MB warn / 250 MB block seam."""
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
        return v if pe.ceil_mb is None else min(v, pe.ceil_mb)

    def _bbox_area_coeff(kw: dict[str, Any]) -> float:
        # mb_per_sq_deg_by_param: the resolved param value keys the map; absent -> default -> scalar -> 0.01.
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
    """The request's params through the row's model and gates, as the quantized dict BOTH the
    executor and the cache key are built from; bad input raises a source-stamped :class:`RouterInputError`."""
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
    bsfx = bbox_error_suffix(spec)
    if g.conus_only:
        envelope = g.conus_bbox or _CONUS_BBOX
        cw, cs, ce, cn = envelope
        w, s, e, n = bbox
        if e < cw or w > ce or n < cs or s > cn:
            raise router_input_error(
                spec.error_code_prefix, f"bbox {bbox} does not intersect the envelope {envelope} this "
                f"source's CONUS-only gate declares", bsfx
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
    # A QGIS-provider row dispatches by ACCESS ahead of the shape ladder: one executor answers both modes.
    if (spec.ingest or {}).get("access") == qgis_provider.ACCESS:
        return qgis_provider.execute
    # A hook-delegated row wins over the shape dispatch; a ``shape: record`` row wins over every
    # layer executor (its build_request is the plan builder for the record shaper).
    if spec.shape == "record":
        from .executors import record as record_executor
        return record_executor.execute
    if spec.hooks is not None and spec.hooks.delegate:
        if spec.shape == "raster-cog":
            return raster_cog.execute
        from .executors import library_delegate
        return library_delegate.execute
    # A GDAL-driver vector row wins over every hook-driven branch below.
    if (spec.ingest or {}).get("access") == "ogr":
        from .executors import vector_ogr
        return vector_ogr.execute
    # next_page / enrich_plan hooks route to chained_resolution; resolve_build alone does not
    # (pre_resolve runs it in route()).
    if (spec.hooks is not None and (spec.hooks.next_page or spec.hooks.enrich_plan)) \
            or (spec.ingest or {}).get("enrich"):
        from .executors import chained_resolution
        return chained_resolution.execute
    if (spec.hooks is not None and spec.hooks.build_request) \
            or (spec.ingest or {}).get("access") == "http_json":
        from .executors import http_json
        return http_json.execute
    # No in-tree row declares a join block: refuse rather than fall through to the geometry-only
    # read, which would silently drop the values leg.
    if spec.join is not None:
        raise router_not_available_error(
            spec.error_code_prefix,
            "this row declares a join block but _router/transforms/join.py is not "
            "in the tree; restore it with the extension that supplies it",
        )
    if spec.shape == "raster-cog":
        ingest = spec.ingest or {}
        if "mosaic" in ingest or "tile_deg2" in ingest:
            return tiled_mosaic.execute
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
    """The declared ``style:`` row for THIS call with ``by_param`` applied key by key over the base row."""
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
    """The key a fetch of ``spec`` with ``raw_params`` would land under, else ``None``. It runs
    the same validation and pre-resolve as ``route``, so a network ``pre_resolve`` repeats here: run off the loop.
    ``None`` for an uncacheable source, a frames list, invalid params, or a key needing a delegate round trip."""
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
    bbox = None
    for pname, pspec in spec.params.items():
        # A present-but-None bbox param yields no bbox stamp; the features' or requested bbox governs.
        if pspec.type == "bbox" and params.get(pname) is not None:
            b = params[pname]
            bbox = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
            break
    variable = params.get("variable") or params.get("product") or spec.source_class
    layer_id = f"{spec.source_class}-{variable}"
    if not spec.output.emit_bbox:
        bbox = None
    units = spec.normalize.units
    # units_from_param stamps units from a request param's resolved value and overrides the static stamp.
    if spec.normalize.units_from_param:
        pv = params.get(spec.normalize.units_from_param)
        if pv is not None:
            units = str(pv)
    # units_by_param maps a param value to units; an absent value yields units=None.
    ubp = spec.normalize.units_by_param
    if ubp:
        units = (ubp.get("map") or {}).get(params.get(ubp.get("param")))
    # role_by_param maps a param value to the LayerURI role; an absent value falls back to the static role.
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
        # The row's statement of what its elevations are counted from rides on the layer; a source
        # whose data states its own zero per feature is stamped off its records once written.
        vertical_datum=spec.vertical_datum or None,
        quantity=spec.normalize.quantity or None,
    )


def try_dispatch(spec: SourceSpec, raw_params: dict[str, Any]) -> Any:
    """Cross-sibling PRE-FLIGHT dispatch: on a matching ``spec.dispatch`` condition return the
    named sibling's result verbatim, else the ``_NO_DISPATCH`` sentinel."""

    # ONE row-declared target per condition, evaluated on RAW params before validate/gate/cache;
    # only ``pass_args``-mapped params are forwarded and nothing is re-cached under this row.
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
    """The engine: validate, gate, dispatch, cache, emit. Returns a LayerURI, a record dict for
    ``shape: record``, or an ordered ``list[LayerURI]`` for ``shape: animation_frames``."""
    # Router-level control kwargs (``visualize``, ``purpose``) are popped so they never reach
    # validation or the cache key.
    raw_params = dict(raw_params)
    _visualize = raw_params.pop("visualize", None)
    _purpose = raw_params.pop("purpose", None)
    dispatched = try_dispatch(spec, raw_params)
    if dispatched is not _NO_DISPATCH:
        return dispatched
    metadata = synthesize_metadata(spec)
    params = validate_params(spec, raw_params)
    # The ASK is logged for every fetch: a cache hit reaches no executor.
    logger.info("fetch %s ask=%s", metadata.name, str(params)[:400])
    # An animation source owns its per-frame read_through loop: there is no single LayerURI.
    if spec.shape == "animation_frames":
        from .executors import animation_frames
        return animation_frames.execute(spec, params, metadata)
    if spec.hooks is not None and spec.hooks.delegate:
        from .executors import library_delegate
        library_delegate.pre_validate(spec, params)
    # Socketed pre-cache-key delegate resolve: a cycle=None request would else key non-deterministically.
    if spec.hooks is not None and spec.hooks.delegate_resolve:
        from .executors import library_delegate
        params = {**params, **library_delegate.resolve(spec, params)}
    # Name -> id resolves BEFORE read_through so a name query and its id query share one cache entry.
    if spec.hooks is not None and spec.hooks.resolve_build:
        from .executors import chained_resolution
        params = chained_resolution.pre_resolve(spec, params)
    # pre_resolve runs BEFORE read_through so the resolved value enters the cache key (else a
    # date=None request forever serves the first-cached day).
    if spec.hooks is not None and spec.hooks.pre_resolve:
        from .hooks import resolve_hook
        params = {**params, **resolve_hook(spec.hooks.pre_resolve)(spec, params)}
    executor = select_executor(spec)

    # A ``shape: record`` source caches its JSON dict bytes and returns the parsed dict, no LayerURI.
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

    # A recorder rides through read_through so the delegate's provenance is persisted fresh and
    # replayed on a cache hit.
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
    # variant_by_emptiness: a feature-empty FGB returns the hook's record dict INSTEAD of the
    # LayerURI (a zero-fault AOI is never given a layer).
    vbe = spec.output.variant_by_emptiness
    if vbe is not None and result.data is not None and _fgb_feature_count(result.data) == 0:
        from .hooks import resolve_hook
        return resolve_hook(vbe)(spec, params)
    layer = build_layer_uri(spec, params, result.uri)
    if result.data and any(row.datum == PER_RECORD for row in spec.coverage):
        layer = layer.model_copy(update=_stated_by_records(result.data))
    # bbox_from_features reads the produced FGB so the stamp is consistent across cache paths.
    bff = spec.output.bbox_from_features
    if bff is not None and result.data:
        extent = _extent_from_fgb(result.data, float(bff.get("pad", 0.1)))
        if extent is not None:
            layer = layer.model_copy(update={"bbox": extent})
    # An envelope hook may ADD fields; uri/layer_type are dropped so it can never flip an error
    # to success or re-point the layer.
    if spec.hooks is not None and spec.hooks.envelope:
        layer = _apply_envelope(spec, params, layer, result.data, result.provenance)
    # A fetch nested inside a composer surfaces as a role=context input; best-effort, never fails
    # the fetch. A direct chat dispatch is skipped (the wrapper emits it).
    from .emit_on_fetch import maybe_emit_input_on_fetch
    maybe_emit_input_on_fetch(
        spec, params, layer, visualize=_visualize, purpose=_purpose
    )
    return layer


_RECORD_STATEMENTS = ("vertical_datum", "datum_offset_m", "datum_offset_frame")


def _stated_by_records(data: bytes) -> dict[str, Any]:
    """The datum and published shift every record of a vector layer agrees on; a disputed statement is left unsaid."""
    import io

    import pyogrio

    frame = pyogrio.read_dataframe(io.BytesIO(data), read_geometry=False)
    out: dict[str, Any] = {}
    for key in _RECORD_STATEMENTS:
        if key in frame.columns:
            stated = set(frame[key].dropna().tolist())
            if len(stated) == 1:
                out[key] = stated.pop()
    return out


#: Fields an envelope hook must never re-write.
_ENVELOPE_PROTECTED_KEYS = ("uri", "layer_type")


def _apply_envelope(
    spec: SourceSpec,
    params: dict[str, Any],
    layer: LayerURI,
    data: bytes | None,
    provenance: dict[str, Any] | None = None,
) -> LayerURI:
    """Build the row's ``output.result_model`` subclass via the pure envelope hook; ``uri`` and ``layer_type`` are stripped."""

    # The provenance dict is passed only to a hook that DECLARES a ``provenance`` parameter.
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
        # No declared subclass: overlay the non-protected extra onto the base LayerURI.
        base_fields = set(type(layer).model_fields)
        safe = {k: v for k, v in extra.items() if k in base_fields}
        return layer.model_copy(update=safe) if safe else layer
    return cls(**{**layer.model_dump(), **extra})


def _fgb_feature_count(data: bytes) -> int:
    """The number of features in an FGB; an UNREADABLE FGB counts as non-empty."""
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
    """The (west, south, east, north) extent of an FGB's features, a collapsed axis padded by
    ``pad`` degrees; None when unreadable or empty."""
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
