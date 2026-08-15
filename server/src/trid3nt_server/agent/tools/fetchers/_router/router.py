"""The router engine (contract sec 2).

One engine: ``resolve spec -> validate params -> apply gates -> dispatch to the
executor/transform by shape -> read_through cache -> emit LayerURI``. Each
executor is a pure ``(spec, validated_params) -> bytes`` closure passed as the
``fetch_fn`` to ``read_through``; the router owns everything around it and binds
the four shared seams (typed errors, cache, payload gate, LayerURI) so a
spec-driven source is INDISTINGUISHABLE from a hand-written twin.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math as _math
from typing import Any, Callable

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from .._fetch_common import _validate_bbox, round_bbox_to_resolution
from ...cache import ProvenanceRecorder, read_through
from .errors import (
    bbox_error_suffix,
    router_input_error,
    router_not_available_error,
    router_upstream_error,
)
from .executors import raster_cog, station_timeseries, vector_fgb
from .transforms import join as join_transform
from .transforms import tiled_mosaic

logger = logging.getLogger("trid3nt_server.agent.tools.fetchers._router.router")

__all__ = [
    "synthesize_metadata",
    "synthesize_payload_estimator",
    "validate_params",
    "select_executor",
    "try_dispatch",
    "route",
]

#: Sentinel: no ``spec.dispatch`` condition matched (distinct from a dispatch that
#: legitimately returns ``None``, though none does today). ``route()`` proceeds to
#: its own pipeline only on this sentinel.
_NO_DISPATCH = object()

#: CONUS domain for the conus_only gate (gridmet native bounds).
_CONUS_BBOX: tuple[float, float, float, float] = (-124.77, 25.05, -67.06, 49.40)


# --------------------------------------------------------------------------- #
# Metadata + payload-estimator synthesis (indistinguishability seams).
# --------------------------------------------------------------------------- #


def synthesize_metadata(spec: SourceSpec) -> AtomicToolMetadata:
    """Synthesize ``AtomicToolMetadata`` from the spec (the twin's registration)."""
    return AtomicToolMetadata(
        name=spec.name,
        ttl_class=spec.cache.ttl_class,
        source_class=spec.source_class,          # cache prefix (NOT the error prefix)
        # A live-no-cache spec (an availability index that turns over continuously,
        # fetch_slider_timestamps) is uncacheable by construction: read_through
        # short-circuits it and the AtomicToolMetadata cross-field validator forbids
        # cacheable=True with ttl_class=live-no-cache. No-op for every cacheable spec.
        cacheable=spec.cache.ttl_class != "live-no-cache",
        supports_global_query=spec.supports_global_query,
        payload_mb_estimator_name="estimate_payload_mb",
        open_world_hint=True,
        # data-native resolution declarations ride from the spec onto the
        # metadata so the gate card can quote them (two-layer truth: data facts here).
        resolution_specs=spec.resolution_declarations,
    )


def synthesize_payload_estimator(spec: SourceSpec) -> Callable[..., float]:
    """Synthesize ``estimate_payload_mb(**args) -> float`` from the spec.

    Server.py's ``tool-payload-warning`` seam reads the returned callable exactly
    as it reads a hand-written twin's estimator (>25MB warns, >250MB blocks).
    """
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
        # ceil_mb (wave-7) reproduces the usfs [floor, ceil] clip; None = no cap.
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


# --------------------------------------------------------------------------- #
# Param validation + gates (BEFORE any network call).
# --------------------------------------------------------------------------- #


def _quantize_bbox(bbox: tuple[float, ...], directive: str | None) -> tuple[float, ...]:
    if not directive:
        return tuple(round(v, 6) for v in bbox)
    if directive == "round_6dp":
        return tuple(round(v, 6) for v in bbox)
    if directive == "round_4dp":
        # climate_normals keys + filters on a 4dp bbox; byte-identical to the twin.
        return tuple(round(v, 4) for v in bbox)
    if directive.startswith("res_"):
        try:
            res_m = int(directive.split("_", 1)[1])
        except (ValueError, IndexError):
            return tuple(round(v, 6) for v in bbox)
        return round_bbox_to_resolution(tuple(bbox), res_m)  # type: ignore[arg-type]
    return tuple(round(v, 6) for v in bbox)


def validate_params(spec: SourceSpec, raw: dict[str, Any]) -> dict[str, Any]:
    """Validate + coerce request params against the spec (typed RouterInputError).

    Returns the validated + quantized params dict used for both the executor and
    the cache key. Raises :class:`RouterInputError` (source-stamped code) on any
    bad input, BEFORE any network call.
    """
    sc = spec.error_code_prefix
    out: dict[str, Any] = {}
    date_params: dict[str, _dt.date] = {}

    for pname, pspec in spec.params.items():
        # Per-param A.6 input suffix: the param's override else the spec default
        # (gridmet/coops INPUT_ERROR, hifld/census INPUT_INVALID, esri
        # bbox->BBOX_INVALID / year->YEAR_INVALID).
        sfx = pspec.error_suffix or spec.input_error_suffix
        present = pname in raw and raw[pname] is not None
        value = raw.get(pname)

        if not present:
            if pspec.default is not None:
                value = pspec.default
                present = True
            elif pspec.required:
                # bbox=None global-query policy: honest typed error.
                if pspec.type == "bbox" and not spec.supports_global_query:
                    raise router_input_error(sc, f"{pname} is required (bbox); global query not supported", sfx)
                raise router_input_error(sc, f"required param {pname!r} missing", sfx)
            else:
                continue

        if pspec.type == "bbox":
            try:
                bbox = tuple(float(v) for v in value)
            except (TypeError, ValueError):
                raise router_input_error(sc, f"{pname} must be a 4-tuple of floats; got {value!r}", sfx)
            if len(bbox) != 4:
                raise router_input_error(sc, f"{pname} must be (min_lon,min_lat,max_lon,max_lat); got {value!r}", sfx)
            try:
                _validate_bbox(bbox)
            except Exception as exc:  # noqa: BLE001 -- BboxInvalidError -> typed router error
                raise router_input_error(sc, str(exc), sfx)
            out[pname] = list(_quantize_bbox(bbox, pspec.quantize))

        elif pspec.type == "iso_date":
            try:
                d = _dt.date.fromisoformat(str(value))
            except ValueError:
                raise router_input_error(sc, f"{pname}={value!r} is not ISO YYYY-MM-DD", sfx)
            date_params[pname] = d
            out[pname] = d.isoformat()

        elif pspec.type == "enum":
            allowed = pspec.values or []
            # Case-insensitive enum (ejscreen indicator): normalize BEFORE the
            # allowed-set check, echoing the normalized key (no-op when unset).
            if getattr(pspec, "lowercase", False) and isinstance(value, str):
                value = value.strip().lower()
            # enum alias table: a known alias maps to the canonical value
            # BEFORE the allowed-set check, echoing the canonical key (landsat
            # band_combo accepts rgb/natural/cir/lst/... aliases). No-op when unset
            # (no prior enum param declares aliases).
            if getattr(pspec, "aliases", None) and isinstance(value, str):
                value = pspec.aliases.get(value.strip().lower(), value)
            if value not in allowed:
                raise router_input_error(sc, f"{pname}={value!r} not in {allowed}", sfx)
            out[pname] = value

        elif pspec.type == "int":
            try:
                iv = int(value)
            except (TypeError, ValueError):
                raise router_input_error(sc, f"{pname} must be an int; got {value!r}", sfx)
            _check_range(sc, pname, pspec, iv, sfx)
            out[pname] = iv

        elif pspec.type == "float":
            try:
                fv = float(value)
            except (TypeError, ValueError):
                raise router_input_error(sc, f"{pname} must be a float; got {value!r}", sfx)
            _check_range(sc, pname, pspec, fv, sfx)
            out[pname] = fv

        elif pspec.type == "int_range":
            # A 2-element [start, end] int list (mtbs year_range). Element bounds:
            # `min` floors start, `max` ceils end; start must be <= end. A bool is
            # rejected (True/False are ints in Python but never a valid year).
            if isinstance(value, bool) or not isinstance(value, (list, tuple)) or len(value) != 2:
                raise router_input_error(sc, f"{pname} must be a 2-element [start, end] list; got {value!r}", sfx)
            try:
                a, b = int(value[0]), int(value[1])
            except (TypeError, ValueError):
                raise router_input_error(sc, f"{pname} elements must be ints; got {value!r}", sfx)
            if isinstance(value[0], bool) or isinstance(value[1], bool):
                raise router_input_error(sc, f"{pname} elements must be ints; got {value!r}", sfx)
            if pspec.min is not None and a < pspec.min:
                raise router_input_error(sc, f"{pname} start {a} below min {int(pspec.min)}", sfx)
            if pspec.max is not None and b > pspec.max:
                raise router_input_error(sc, f"{pname} end {b} above max {int(pspec.max)}", sfx)
            if a > b:
                raise router_input_error(sc, f"{pname} start {a} must be <= end {b}", sfx)
            out[pname] = [a, b]

        elif pspec.type == "datetime_range":
            # A 2-element [start, end] ISO datetime-pair (movebank time_range).
            # Each entry parses as an ISO date OR datetime; start must be <= end.
            # Echoed as [start.isoformat(), end.isoformat()] for cache stability;
            # a build hook re-parses to the source's wire format.
            if isinstance(value, bool) or not isinstance(value, (list, tuple)) or len(value) != 2:
                raise router_input_error(sc, f"{pname} must be a 2-element [start, end] list; got {value!r}", sfx)
            def _parse_dt(v: Any) -> _dt.datetime:
                raw = str(v).strip()
                try:
                    d = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    try:
                        d = _dt.datetime.combine(_dt.date.fromisoformat(raw), _dt.time(0, 0, 0))
                    except ValueError:
                        raise router_input_error(sc, f"{pname} entry {v!r} is not an ISO date/datetime", sfx)
                return d
            a, b = _parse_dt(value[0]), _parse_dt(value[1])
            if a > b:
                raise router_input_error(sc, f"{pname} start {a.isoformat()} must be <= end {b.isoformat()}", sfx)
            out[pname] = [a.isoformat(), b.isoformat()]

        elif pspec.type == "point":
            # A 2-element [lon, lat] float list (nldi seed_point). Coerce +
            # finite/range check; the CONUS + seed/comid mutual-exclusion gate is
            # the delegating executor's (it stamps the twin's exact error code).
            if isinstance(value, bool) or not isinstance(value, (list, tuple)) or len(value) != 2:
                raise router_input_error(sc, f"{pname} must be a 2-element [lon, lat] list; got {value!r}", sfx)
            try:
                lon, lat = float(value[0]), float(value[1])
            except (TypeError, ValueError):
                raise router_input_error(sc, f"{pname} elements must be numbers; got {value!r}", sfx)
            if not (_math.isfinite(lon) and _math.isfinite(lat)):
                raise router_input_error(sc, f"{pname} has non-finite values: {value!r}", sfx)
            if not (-180.0 <= lon <= 180.0):
                raise router_input_error(sc, f"{pname} lon out of [-180,180]: {lon!r}", sfx)
            if not (-90.0 <= lat <= 90.0):
                raise router_input_error(sc, f"{pname} lat out of [-90,90]: {lat!r}", sfx)
            out[pname] = [lon, lat]

        elif pspec.type == "float_list":
            # A scalar float OR a list[float] (slr_scenarios scenario_ft). Each
            # entry must be in the allowed `values` set; the result is sorted +
            # deduped for cache-key stability (the twin's _validate_scenario_ft).
            if isinstance(value, bool):
                raise router_input_error(sc, f"{pname}={value!r} must be a float or list[float]", sfx)
            if isinstance(value, (int, float)):
                raw_levels = [float(value)]
            elif isinstance(value, (list, tuple)):
                if not value:
                    # An empty list falls back to the declared default (twin: [] -> DEFAULT).
                    dv = pspec.default
                    raw_levels = [float(v) for v in dv] if isinstance(dv, (list, tuple)) else []
                else:
                    raw_levels = []
                    for v in value:
                        if isinstance(v, bool) or not isinstance(v, (int, float)):
                            raise router_input_error(sc, f"{pname} entries must be numeric; got {type(v).__name__}: {v!r}", sfx)
                        raw_levels.append(float(v))
            else:
                raise router_input_error(sc, f"{pname} must be a float or list[float]; got {type(value).__name__}", sfx)
            allowed = pspec.values or []
            for lv in raw_levels:
                if allowed and lv not in allowed:
                    raise router_input_error(sc, f"{pname}={lv!r} not in {sorted(allowed)}", sfx)
            out[pname] = sorted(set(raw_levels))

        elif pspec.type == "str_list":
            # A scalar string OR a list[str] free-text filter (nws_event
            # event_types). Each entry stripped, empties dropped, then sorted +
            # deduped for cache-key stability -- the twin's
            # `sorted({e.strip() for e in event_types if e.strip()})`.
            if isinstance(value, str):
                raw_items: list[str] = [value]
            elif isinstance(value, (list, tuple)):
                raw_items = []
                for v in value:
                    if not isinstance(v, str):
                        raise router_input_error(sc, f"{pname} entries must be strings; got {type(v).__name__}: {v!r}", sfx)
                    raw_items.append(v)
            else:
                raise router_input_error(sc, f"{pname} must be a string or list[str]; got {type(value).__name__}", sfx)
            out[pname] = sorted({s.strip() for s in raw_items if s.strip()})

        elif pspec.type == "bool":
            # A truthy flag (nws_river_forecast include_thresholds / include_series).
            # Coerced with bool(value) -- the twin's `bool(flag)` contract; a JSON
            # false/true, 0/1, or a python bool all normalize the same way.
            out[pname] = bool(value)

        elif pspec.type == "date_compact":
            # Accept 'YYYY-MM-DD' or 'YYYYMMDD'; normalize to the 8-digit compact
            # form and validate it is a real calendar date (us_drought_monitor).
            if not isinstance(value, str):
                raise router_input_error(sc, f"{pname} must be a string date; got {type(value).__name__}", sfx)
            compact = value.strip().replace("-", "")
            import re as _re
            if not _re.fullmatch(r"\d{8}", compact):
                raise router_input_error(sc, f"{pname} must be 'YYYY-MM-DD' or 'YYYYMMDD' (8 digits); got {value!r}", sfx)
            try:
                _dt.datetime.strptime(compact, "%Y%m%d")
            except ValueError:
                raise router_input_error(sc, f"{pname}={value!r} is not a real calendar date", sfx)
            out[pname] = compact

        else:  # str
            s = str(value)
            # Alias-or-passthrough (wqp characteristic): lower/strip -> table, else
            # verbatim (stripped). No-op when the spec declares no aliases.
            if pspec.aliases:
                out[pname] = pspec.aliases.get(s.strip().lower(), s.strip())
            else:
                out[pname] = s

    # Date-range ceiling: any iso_date param carrying max_range_days pairs with
    # the first declared iso_date as the range start.
    _check_date_range(spec, date_params)
    _apply_gates(spec, out)
    return out


def _check_range(sc: str, pname: str, pspec: Any, value: float, sfx: str) -> None:
    """Inclusive int/float range gate (esri year [2017,2023] -> YEAR_INVALID)."""
    if pspec.min is not None and value < pspec.min:
        raise router_input_error(sc, f"{pname}={value} below min {pspec.min}", sfx)
    if pspec.max is not None and value > pspec.max:
        raise router_input_error(sc, f"{pname}={value} above max {pspec.max}", sfx)


def _check_date_range(spec: SourceSpec, date_params: dict[str, _dt.date]) -> None:
    """Order + coverage + range-days gates over the declared iso_date params.

    Order (start > end) and range-days over-cap are typed INPUT errors (both
    twins). Coverage bounds (``min_date`` floor / ``max_future_days`` ceiling) are
    typed NOT_AVAILABLE (gridmet GRIDMETNotAvailableError), distinct from input.
    "start" is the FIRST declared iso_date, "end" the LAST (declaration order,
    matching every twin's start_date/end_date signature).
    """
    sc = spec.error_code_prefix
    iso_names = [n for n, p in spec.params.items() if p.type == "iso_date" and n in date_params]
    if not iso_names:
        return
    d_start = date_params[iso_names[0]]

    # Check ORDER (INPUT) before COVERAGE (NOT_AVAILABLE) before RANGE (INPUT) --
    # the exact twin sequence (gridmet _validate_date_range), so a doubly-invalid
    # date (order-violated AND out-of-coverage) stamps the same code as the twin.
    if len(iso_names) >= 2:
        d_end = date_params[iso_names[-1]]
        if d_start > d_end:
            raise router_input_error(
                sc,
                f"start_date must be <= end_date; got start={d_start}, end={d_end}",
                spec.input_error_suffix,
            )

    today = _dt.date.today()
    for pname in iso_names:
        pspec = spec.params[pname]
        d = date_params[pname]
        if pspec.min_date is not None:
            try:
                floor = _dt.date.fromisoformat(str(pspec.min_date))
            except ValueError:
                floor = None
            if floor is not None and d < floor:
                raise router_not_available_error(
                    sc, f"{pname} {d.isoformat()} before coverage start {floor.isoformat()}"
                )
        if pspec.max_future_days is not None and d > today + _dt.timedelta(days=pspec.max_future_days):
            raise router_not_available_error(
                sc, f"{pname} {d.isoformat()} is beyond the source's real-time coverage"
            )

    if len(iso_names) >= 2:
        for pname in iso_names:
            pspec = spec.params[pname]
            if pspec.max_range_days is not None:
                n = (date_params[pname] - d_start).days + 1
                if n > pspec.max_range_days:
                    raise router_input_error(
                        sc,
                        f"date range {n} days exceeds max_range_days={pspec.max_range_days}",
                        spec.input_error_suffix,
                    )


def _apply_gates(spec: SourceSpec, params: dict[str, Any]) -> None:
    g = spec.gates
    bbox = None
    for pname, pspec in spec.params.items():
        if pspec.type == "bbox" and pname in params:
            bbox = params[pname]
            break
    if bbox is None:
        return
    # bbox-class gate failures stamp the bbox param's A.6 suffix (esri
    # BBOX_INVALID; gridmet/others INPUT_ERROR / INPUT_INVALID).
    bsfx = bbox_error_suffix(spec)
    if g.conus_only:
        cw, cs, ce, cn = _CONUS_BBOX
        w, s, e, n = bbox
        if e < cw or w > ce or n < cs or s > cn:
            raise router_input_error(
                spec.error_code_prefix, f"bbox {bbox} does not intersect CONUS {_CONUS_BBOX}", bsfx
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


# --------------------------------------------------------------------------- #
# Executor selection + LayerURI emission.
# --------------------------------------------------------------------------- #


def select_executor(spec: SourceSpec) -> Callable[[SourceSpec, dict[str, Any]], bytes]:
    """Return the ``(spec, params) -> bytes`` closure for the spec's shape/transform."""
    # A spec-declared library delegation wins over the shape dispatch. Two forms:
    #  - GENERIC library_delegate: a spec names ``hooks.delegate`` (a
    #    registered hook that calls a maintained library owning discovery+socket and
    #    returns arrays/frames). A raster spec routes through raster_cog (its
    #    fetch_source_array calls the delegate for the array); a vector spec routes
    #    through the generic library_delegate.execute (features -> FGB).
    #  - LEGACY dataretrieval: ``ingest.delegate.library == 'dataretrieval'``
    #    with a service dispatch, kept on its own module.
    # No-op for every prior spec (none declare hooks.delegate or ingest.delegate).
    # Record-return path: a ``shape: record`` source produces a bare
    # JSON dict, not a LayerURI. It wins over every layer executor below (its
    # build_request hook is the PURE plan builder feeding the record dict-shaper,
    # NOT the http_json vector path). No-op for every prior spec (none are record).
    if spec.shape == "record":
        from .executors import record as record_executor
        return record_executor.execute
    if spec.hooks is not None and spec.hooks.delegate:
        if spec.shape == "raster-cog":
            return raster_cog.execute
        from .executors import library_delegate
        return library_delegate.execute
    if (spec.ingest or {}).get("delegate"):
        from .executors import dataretrieval_delegate
        return dataretrieval_delegate.execute
    # zip_vector path: a source publishing a ZIP-wrapped multi-file vector
    # member (TIGER shapefile-ZIP) routes to the whole-object extract-read-filter
    # executor. Its build_request hook is the PURE URL planner, so this MUST win over
    # the http_json hooks.build_request branch below. No-op for every prior spec.
    if (spec.ingest or {}).get("zip_vector"):
        from .executors import zip_vector
        return zip_vector.execute
    # Chained-resolution path: a spec that declares an offset-paging
    # (next_page) or per-item detail-enrichment (enrich_plan) hook routes to the
    # chained_resolution executor (resolve-then-fetch + bounded enrichment over the
    # shared transport). The pure name->id resolve phase (resolve_build only) still
    # uses the http_json main-fetch body, so it is NOT a trigger here -- pre_resolve
    # runs it in route(). No-op for every prior spec (none declare these hooks).
    if spec.hooks is not None and (spec.hooks.next_page or spec.hooks.enrich_plan):
        from .executors import chained_resolution
        return chained_resolution.execute
    # Sidecar-write path (trigger wave): an overpass source that ALSO writes
    # ONE declared sidecar object next to the .fgb (fetch_buildings' tags.json). Routes
    # to the overpass_sidecar executor (build_request QL + a (features, tags) parse +
    # the constrained side write). MUST win over the http_json build_request branch
    # below (it also declares build_request). No-op for every prior spec.
    if (spec.ingest or {}).get("sidecar_write"):
        from .executors import overpass_sidecar
        return overpass_sidecar.execute
    # Tier-3 hook-driven path: a spec that names a build_request hook
    # routes to the http_json executor (source-specific request + parse via named
    # pure hooks). No-op for every prior spec (none declare hooks).
    if spec.hooks is not None and spec.hooks.build_request:
        from .executors import http_json
        return http_json.execute
    if spec.join is not None:
        return join_transform.execute
    # Declarative fan-out (multi-query-per-value + merge, slr_scenarios). No-op
    # for every prior spec (none declare ingest.fan_out).
    if (spec.ingest or {}).get("fan_out"):
        from .transforms import fan_out
        return fan_out.execute
    if spec.shape == "raster-cog":
        ingest = spec.ingest or {}
        if "mosaic" in ingest or "tile_deg2" in ingest:
            return tiled_mosaic.execute
        return raster_cog.execute
    if spec.shape == "vector-fgb":
        return vector_fgb.execute
    if spec.shape == "station-timeseries-fgb":
        return station_timeseries.execute
    raise router_input_error(
        spec.error_code_prefix, f"no executor for shape {spec.shape!r}", spec.input_error_suffix
    )


def _template(text: str, params: dict[str, Any]) -> str:
    """Best-effort ``str.format`` templating for style_preset (missing key -> raw)."""
    try:
        return text.format(**params)
    except (KeyError, IndexError):
        return text


def build_layer_uri(spec: SourceSpec, params: dict[str, Any], uri: str) -> LayerURI:
    """Emit the ``LayerURI`` from ``spec.output`` (the shared emission seam)."""
    bbox = None
    for pname, pspec in spec.params.items():
        # A bbox param present-but-None (a pre_resolve that nulls bbox when an
        # alternate selector wins the cache key -- nwis state_code) yields no bbox
        # stamp; bbox_from_features / the requested bbox governs. No-op for priors
        # (which never carry a None bbox value).
        if pspec.type == "bbox" and params.get(pname) is not None:
            b = params[pname]
            bbox = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
            break
    variable = params.get("variable") or params.get("product") or spec.source_class
    layer_id = f"{spec.source_class}-{variable}"
    # gridmet's twin omits LayerURI.bbox; emit_bbox=false suppresses it (parity).
    if not spec.output.emit_bbox:
        bbox = None
    # Per-variable LayerURI.units (full fidelity): a JOIN spec carries the units
    # on the resolved variable (census: usd / years / percent / count), matching
    # the twin's LayerURI.units=spec["units"]. Non-JOIN sources keep the single
    # normalize.units stamp. Resolution can only fail on an invalid variable,
    # which the executor already rejected before this point (guarded regardless).
    units = spec.normalize.units
    # units_from_param (wqp): stamp LayerURI.units from a request param's resolved
    # value (the characteristic). No-op when unset. Overrides the static stamp.
    if spec.normalize.units_from_param:
        pv = params.get(spec.normalize.units_from_param)
        if pv is not None:
            units = str(pv)
    # units_by_param (wave-7): MAP a param value to units (landfire/usfs per-layer);
    # a value absent from the map -> units=None. No-op when unset.
    ubp = spec.normalize.units_by_param
    if ubp:
        units = (ubp.get("map") or {}).get(params.get(ubp.get("param")))
    if spec.join is not None:
        try:
            _, var_spec = join_transform.select_variable(spec, params)
            resolved = var_spec.get("units")
            if resolved:
                units = resolved
        except Exception:  # noqa: BLE001 -- never fail emission on units resolution
            pass
    # style_preset_by_param (wave-7): MAP a param value to the preset (landfire/usfs
    # per-layer); a value absent from the map falls back to the static preset.
    style_preset = _template(spec.output.style_preset, params)
    sbp = spec.output.style_preset_by_param
    if sbp:
        style_preset = (sbp.get("map") or {}).get(
            params.get(sbp.get("param")), style_preset
        )
    # role_by_param: MAP a param value to the LayerURI role (landsat
    # thermal LST -> primary, RGB composites -> context); a value absent from the
    # map falls back to the static role. No-op when unset.
    role = spec.output.role
    rbp = spec.output.role_by_param
    if rbp:
        role = (rbp.get("map") or {}).get(params.get(rbp.get("param")), role)
    return LayerURI(
        layer_id=layer_id,
        name=f"{spec.source_class} {variable}",
        layer_type=spec.output.layer_type,
        uri=uri,
        style_preset=style_preset,
        role=role,
        units=units,
        bbox=bbox,
    )


def try_dispatch(spec: SourceSpec, raw_params: dict[str, Any]) -> Any:
    """Cross-sibling PRE-FLIGHT dispatch. Returns the sibling tool's
    result verbatim on a match, else the ``_NO_DISPATCH`` sentinel.

    For ONE declared ``spec.dispatch`` condition whose ``param`` value matches, the
    router SHORT-CIRCUITS before any validation / gate / cache / fetch and serves
    the request from the named sibling registered tool -- returning THAT tool's
    result byte-for-byte (its own ``source_class`` cache prefix, its own
    ``layer_id`` / ``name``). Byte-identical to the twin's
    ``TOOL_REGISTRY["fetch_copernicus_dem"].fn(bbox=bbox)`` leg: it forwards only
    the ``pass_args``-mapped RAW params and re-caches NOTHING under this spec.

    Constraints enforced (the seam is deliberately narrow):
      * ONE target per condition (``d.to`` is a single name);
      * SPEC-DECLARED (``d.to`` / ``d.equals_any`` are literals);
      * NO CHAINS -- a target that itself declares ``dispatch`` is refused here, so
        the returned result is always exactly one sibling's verbatim output;
      * PRE-FLIGHT -- evaluated on RAW params before validate/gate/cache/fetch.
    """
    if not spec.dispatch:
        return _NO_DISPATCH
    from trid3nt_server.agent.tools import TOOL_REGISTRY

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
    """The engine: validate -> gate -> dispatch -> cache -> emit LayerURI (or a
    record dict for a ``shape: record`` source, or an ordered
    ``list[LayerURI]`` for a ``shape: animation_frames`` source)."""
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
    # source="copernicus" -> fetch_copernicus_dem's layer, byte-identical).
    dispatched = try_dispatch(spec, raw_params)
    if dispatched is not _NO_DISPATCH:
        return dispatched
    metadata = synthesize_metadata(spec)
    params = validate_params(spec, raw_params)
    # Frames-list output shape: an animation source returns an ORDERED
    # list[LayerURI] (one cache entry + one layer per timestamp), so the executor
    # owns the per-frame read_through loop -- there is no single top-level
    # read_through / LayerURI. It wins over every layer executor below. No-op for
    # every prior spec (none are animation_frames).
    if spec.shape == "animation_frames":
        from .executors import animation_frames
        return animation_frames.execute(spec, params, metadata)
    # A delegated spec's source-specific INPUT validation (wqp bbox-required, nldi
    # seed/comid mutual-exclusion + CONUS + comid gate) runs BEFORE read_through so
    # a bad request raises pre-cache / pre-network -- indistinguishable from the
    # twin (which validates in its body before read_through). No-op otherwise.
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
    # intact: the record executor raises the source's typed empty/upstream errors (so
    # read_through never writes a fabricated-success sentinel), and the returned dict
    # is exactly what the twin returned. No-op for every prior (LayerURI) spec.
    if spec.output.layer_type == "record":
        result = read_through(
            metadata=metadata,
            params=params,
            ext=spec.output.ext,
            fetch_fn=lambda: executor(spec, params),
        )
        assert result.data is not None, "record source is cacheable; data must be set"
        return json.loads(result.data.decode("utf-8"))

    # Fetch-time provenance channel: a spec that declares
    # output.provenance rides a recorder through read_through so the delegate's
    # record_provenance() is persisted as a sidecar (fresh) and replayed from it
    # (cache hit); the recorded dict reaches the envelope hook below. No-op
    # (recorder=None -> byte-identical read_through) for every prior spec.
    recorder = ProvenanceRecorder() if spec.output.provenance else None
    result = read_through(
        metadata=metadata,
        params=params,
        ext=spec.output.ext,
        fetch_fn=lambda: executor(spec, params),
        provenance=recorder,
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
    """Build the spec's ``output.result_model`` subclass via the pure envelope hook.

    The hook computes the extra business fields over the already-produced bytes
    (no I/O). Protected identity keys (``uri`` / ``layer_type``) are stripped from
    the hook's return so it can only enrich, never flip the honesty floor.

    ``provenance``: the fetch-time provenance dict (fresh or cache-hit
    replay) is passed to a hook that DECLARES a ``provenance`` parameter, so a
    result model whose fields are fetch-time provenance survives every cache path.
    A hook without that parameter (every envelope hook) is called with the
    original 4-arg signature -- strictly additive, no existing hook changes.
    """
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
    """The number of features in an FGB (0 for a header-only honest-empty FGB).

    Used by the ``output.variant_by_emptiness`` switch to decide whether the
    fetch was empty (return the record dict) or non-empty (return the LayerURI).
    An unreadable FGB counts as non-empty (-> the LayerURI path) so a read hiccup
    never silently swallows a real fetch into the empty-record degrade.
    """
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
    """The (west, south, east, north) extent of an FGB's features, degenerate-padded.

    A single-point (or single-line) extent whose axis collapses is padded by
    ``pad`` degrees so the camera does not zoom to an infinite level -- the exact
    twin behavior for the point-event fetchers. Returns None on an unreadable /
    empty FGB (the caller keeps the request-bbox stamp).
    """
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
