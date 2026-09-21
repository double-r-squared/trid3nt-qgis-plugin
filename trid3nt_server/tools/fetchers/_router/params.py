"""One pydantic model per source, built at spec load, over ``spec.params``.

The model is the ONE place a param type is stated: its Python annotation, which
the promoted tool's signature and its inputSchema are synthesized from, and its
coercion onto the quantized value the executor and the cache key share. A bad
input raises the source-stamped router error from inside the validator, which is
a RuntimeError and so reaches the caller untouched by pydantic's own machinery."""

from __future__ import annotations

import datetime as _dt
import math as _math
import re
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, create_model, model_validator
from trid3nt_contracts.source_spec import SourceSpec

from .._fetch_common import _validate_bbox, round_bbox_to_resolution
from .errors import router_input_error, router_not_available_error

__all__ = ["annotation_for", "params_model", "validated"]

#: id(spec) -> the model built for it, alongside the spec itself so the id stays
#: live. A spec object is immutable once loaded, so its model is built once with
#: it and a re-loaded spec gets its own.
_MODELS: dict[int, tuple[SourceSpec, type[BaseModel]]] = {}

#: param type -> the schema-compatible Python annotation. Every string-ish type
#: (iso_date / enum / str / date_compact) is a str.
_ANNOTATIONS: dict[str, Any] = {
    "bbox": list[float],
    "point": list[float],
    "int_range": list[int],
    "datetime_range": list[str],
    "float_list": list[float],
    "str_list": list[str],
    "bool": bool,
    "int": int,
    "float": float,
}


def annotation_for(pspec: Any) -> Any:
    """The annotation a param is promoted under. The adapter reads a None-default
    NON-Optional annotation as required-in-schema, so an OPTIONAL param declared
    ``T | None = None`` states ``schema_optional`` and is annotated ``X | None``
    to stay out of the schema's required set."""
    if pspec.type == "enum":
        # An enum is promoted under the type of the set it names: the SLR levels
        # are floats, and a caller handed a signature saying str would be right to
        # send one.
        values = pspec.values or []
        ann = type(values[0]) if values and not isinstance(values[0], str) else str
    else:
        ann = _ANNOTATIONS.get(pspec.type, str)
    if (pspec.default is None and not pspec.required
            and getattr(pspec, "schema_optional", False)):
        return ann | None
    return ann


def _quantize_bbox(bbox: tuple[float, ...], directive: str | None) -> tuple[float, ...]:
    if directive == "round_4dp":
        # climate_normals keys and filters on a 4dp bbox.
        return tuple(round(v, 4) for v in bbox)
    if directive and directive.startswith("res_"):
        try:
            res_m = int(directive.split("_", 1)[1])
        except (ValueError, IndexError):
            return tuple(round(v, 6) for v in bbox)
        return round_bbox_to_resolution(tuple(bbox), res_m)  # type: ignore[arg-type]
    return tuple(round(v, 6) for v in bbox)


def _range_checked(sc: str, pname: str, pspec: Any, value: float, sfx: str) -> None:
    """Inclusive int/float range gate (esri year [2017,2023] -> YEAR_INVALID)."""
    if pspec.min is not None and value < pspec.min:
        raise router_input_error(sc, f"{pname}={value} below min {pspec.min}", sfx)
    if pspec.max is not None and value > pspec.max:
        raise router_input_error(sc, f"{pname}={value} above max {pspec.max}", sfx)


def _coerce(spec: SourceSpec, pname: str, pspec: Any, value: Any) -> Any:
    """One declared param onto the value the executor and the cache key share."""
    sc = spec.error_code_prefix
    # Per-param input-error suffix: the param's override else the spec default
    # (INPUT_ERROR, INPUT_INVALID, bbox->BBOX_INVALID / year->YEAR_INVALID).
    sfx = pspec.error_suffix or spec.input_error_suffix
    ptype = pspec.type

    if ptype == "bbox":
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
        return list(_quantize_bbox(bbox, pspec.quantize))

    if ptype == "iso_date":
        try:
            return _dt.date.fromisoformat(str(value)).isoformat()
        except ValueError:
            raise router_input_error(sc, f"{pname}={value!r} is not ISO YYYY-MM-DD", sfx)

    if ptype == "enum":
        # Case-insensitive enum and the alias table both normalize BEFORE the
        # allowed-set check, echoing the canonical key (landsat band_combo accepts
        # rgb/natural/cir/lst aliases). Both are no-ops when unset.
        if getattr(pspec, "lowercase", False) and isinstance(value, str):
            value = value.strip().lower()
        if getattr(pspec, "aliases", None) and isinstance(value, str):
            value = pspec.aliases.get(value.strip().lower(), value)
        allowed = pspec.values or []
        if value not in allowed:
            raise router_input_error(sc, f"{pname}={value!r} not in {allowed}", sfx)
        return value

    if ptype in ("int", "float"):
        cast = int if ptype == "int" else float
        try:
            num = cast(value)
        except (TypeError, ValueError):
            raise router_input_error(sc, f"{pname} must be a{'n int' if ptype == 'int' else ' float'}; got {value!r}", sfx)
        _range_checked(sc, pname, pspec, num, sfx)
        return num

    if ptype == "int_range":
        # A 2-element [start, end] int list (mtbs year_range). Element bounds:
        # `min` floors start, `max` ceils end; start must be <= end. A bool is
        # rejected (True/False are ints in Python but never a valid year).
        if isinstance(value, bool) or not isinstance(value, (list, tuple)) or len(value) != 2:
            raise router_input_error(sc, f"{pname} must be a 2-element [start, end] list; got {value!r}", sfx)
        if isinstance(value[0], bool) or isinstance(value[1], bool):
            raise router_input_error(sc, f"{pname} elements must be ints; got {value!r}", sfx)
        try:
            a, b = int(value[0]), int(value[1])
        except (TypeError, ValueError):
            raise router_input_error(sc, f"{pname} elements must be ints; got {value!r}", sfx)
        if pspec.min is not None and a < pspec.min:
            raise router_input_error(sc, f"{pname} start {a} below min {int(pspec.min)}", sfx)
        if pspec.max is not None and b > pspec.max:
            raise router_input_error(sc, f"{pname} end {b} above max {int(pspec.max)}", sfx)
        if a > b:
            raise router_input_error(sc, f"{pname} start {a} must be <= end {b}", sfx)
        return [a, b]

    if ptype == "datetime_range":
        # A 2-element [start, end] ISO datetime-pair, each an ISO date OR
        # datetime, echoed as isoformat strings for cache stability; a build hook
        # re-parses to the source's wire format.
        if isinstance(value, bool) or not isinstance(value, (list, tuple)) or len(value) != 2:
            raise router_input_error(sc, f"{pname} must be a 2-element [start, end] list; got {value!r}", sfx)

        def _parse_dt(v: Any) -> _dt.datetime:
            text = str(v).strip()
            try:
                return _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                try:
                    return _dt.datetime.combine(_dt.date.fromisoformat(text), _dt.time(0, 0, 0))
                except ValueError:
                    raise router_input_error(sc, f"{pname} entry {v!r} is not an ISO date/datetime", sfx)

        a_dt, b_dt = _parse_dt(value[0]), _parse_dt(value[1])
        if a_dt > b_dt:
            raise router_input_error(sc, f"{pname} start {a_dt.isoformat()} must be <= end {b_dt.isoformat()}", sfx)
        return [a_dt.isoformat(), b_dt.isoformat()]

    if ptype == "point":
        # A 2-element [lon, lat] float list (nldi seed_point). The CONUS +
        # seed/comid mutual-exclusion gate is the delegating executor's (it stamps
        # the source's own error code).
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
        return [lon, lat]

    if ptype == "float_list":
        # A scalar float OR a list[float] (slr_scenarios scenario_ft), sorted and
        # deduped for cache-key stability.
        if isinstance(value, bool):
            raise router_input_error(sc, f"{pname}={value!r} must be a float or list[float]", sfx)
        if isinstance(value, (int, float)):
            levels = [float(value)]
        elif isinstance(value, (list, tuple)):
            if not value:
                # An empty list falls back to the declared default.
                dv = pspec.default
                levels = [float(v) for v in dv] if isinstance(dv, (list, tuple)) else []
            else:
                levels = []
                for v in value:
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        raise router_input_error(sc, f"{pname} entries must be numeric; got {type(v).__name__}: {v!r}", sfx)
                    levels.append(float(v))
        else:
            raise router_input_error(sc, f"{pname} must be a float or list[float]; got {type(value).__name__}", sfx)
        allowed = pspec.values or []
        for lv in levels:
            if allowed and lv not in allowed:
                raise router_input_error(sc, f"{pname}={lv!r} not in {sorted(allowed)}", sfx)
        return sorted(set(levels))

    if ptype == "str_list":
        # A scalar string OR a list[str] free-text filter (nws_event event_types),
        # stripped, empties dropped, sorted and deduped for cache-key stability.
        if isinstance(value, str):
            items: list[str] = [value]
        elif isinstance(value, (list, tuple)):
            items = []
            for v in value:
                if not isinstance(v, str):
                    raise router_input_error(sc, f"{pname} entries must be strings; got {type(v).__name__}: {v!r}", sfx)
                items.append(v)
        else:
            raise router_input_error(sc, f"{pname} must be a string or list[str]; got {type(value).__name__}", sfx)
        return sorted({s.strip() for s in items if s.strip()})

    if ptype == "bool":
        # A JSON false/true, a 0/1 and a python bool all normalize the same way.
        return bool(value)

    if ptype == "date_compact":
        # 'YYYY-MM-DD' or 'YYYYMMDD' onto the 8-digit compact form, checked as a
        # real calendar date (us_drought_monitor).
        if not isinstance(value, str):
            raise router_input_error(sc, f"{pname} must be a string date; got {type(value).__name__}", sfx)
        compact = value.strip().replace("-", "")
        if not re.fullmatch(r"\d{8}", compact):
            raise router_input_error(sc, f"{pname} must be 'YYYY-MM-DD' or 'YYYYMMDD' (8 digits); got {value!r}", sfx)
        try:
            _dt.datetime.strptime(compact, "%Y%m%d")
        except ValueError:
            raise router_input_error(sc, f"{pname}={value!r} is not a real calendar date", sfx)
        return compact

    # str: alias-or-passthrough (wqp characteristic). Lower/strip onto the table,
    # else verbatim; a no-op when the spec declares no aliases.
    text = str(value)
    if pspec.aliases:
        return pspec.aliases.get(text.strip().lower(), text.strip())
    return text


def _presence(spec: SourceSpec, raw: dict[str, Any]) -> dict[str, Any]:
    """The declared params that are actually being asked for: a default fills an
    absent one, an absent optional is left out, and an absent required refuses."""
    sc = spec.error_code_prefix
    asked: dict[str, Any] = {}
    for pname, pspec in spec.params.items():
        value = raw.get(pname)
        if pname in raw and value is not None:
            asked[pname] = value
        elif pspec.default is not None:
            asked[pname] = pspec.default
        elif pspec.required:
            sfx = pspec.error_suffix or spec.input_error_suffix
            # bbox=None global-query policy: honest typed error.
            if pspec.type == "bbox" and not spec.supports_global_query:
                raise router_input_error(sc, f"{pname} is required (bbox); global query not supported", sfx)
            raise router_input_error(sc, f"required param {pname!r} missing", sfx)
    return asked


def _date_gates(spec: SourceSpec, out: dict[str, Any]) -> None:
    """Order, coverage and range-days gates over the declared iso_date params.
    Order and over-cap are INPUT errors; coverage bounds are NOT_AVAILABLE, a
    distinct class. "start" is the FIRST declared iso_date and "end" the LAST."""
    sc = spec.error_code_prefix
    names = [n for n, p in spec.params.items() if p.type == "iso_date" and n in out]
    if not names:
        return
    dates = {n: _dt.date.fromisoformat(out[n]) for n in names}
    start = dates[names[0]]

    # ORDER (INPUT) before COVERAGE (NOT_AVAILABLE) before RANGE (INPUT), so a
    # doubly-invalid date -- order-violated AND out-of-coverage -- stamps ORDER.
    if len(names) >= 2 and start > dates[names[-1]]:
        raise router_input_error(
            sc, f"start_date must be <= end_date; got start={start}, end={dates[names[-1]]}",
            spec.input_error_suffix)

    today = _dt.date.today()
    for pname in names:
        pspec = spec.params[pname]
        d = dates[pname]
        if pspec.min_date is not None:
            try:
                floor = _dt.date.fromisoformat(str(pspec.min_date))
            except ValueError:
                floor = None
            if floor is not None and d < floor:
                raise router_not_available_error(
                    sc, f"{pname} {d.isoformat()} before coverage start {floor.isoformat()}")
        if pspec.max_future_days is not None and d > today + _dt.timedelta(days=pspec.max_future_days):
            raise router_not_available_error(
                sc, f"{pname} {d.isoformat()} is beyond the source's real-time coverage")

    if len(names) >= 2:
        for pname in names:
            max_days = spec.params[pname].max_range_days
            if max_days is not None:
                n = (dates[pname] - start).days + 1
                if n > max_days:
                    raise router_input_error(
                        sc, f"date range {n} days exceeds max_range_days={max_days}",
                        spec.input_error_suffix)


def params_model(spec: SourceSpec) -> type[BaseModel]:
    """The spec's own params model, built once and kept beside it."""
    held = _MODELS.get(id(spec))
    if held is not None:
        return held[1]
    fields: dict[str, Any] = {}
    for pname, pspec in spec.params.items():
        def _validator(value: Any, _spec: SourceSpec = spec, _n: str = pname,
                       _p: Any = pspec) -> Any:
            return _coerce(_spec, _n, _p, value)

        fields[pname] = (
            Annotated[annotation_for(pspec), BeforeValidator(_validator)], None)

    def _gates(self: BaseModel) -> BaseModel:
        _date_gates(spec, {k: v for k, v in self.__dict__.items() if v is not None})
        return self

    model = create_model(  # type: ignore[call-overload]
        f"{spec.name}_params", **fields,
        __validators__={"_date_gates": model_validator(mode="after")(_gates)})
    _MODELS[id(spec)] = (spec, model)
    return model


def validated(spec: SourceSpec, raw: dict[str, Any]) -> dict[str, Any]:
    """The request's params through the spec's model, as the quantized dict the
    executor and the cache key share."""
    asked = _presence(spec, raw)
    return params_model(spec)(**asked).model_dump(exclude_none=True)
