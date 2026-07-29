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
import logging
from typing import Any, Callable

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from .._fetch_common import _validate_bbox, round_bbox_to_resolution
from ...cache import read_through
from .errors import bbox_error_suffix, router_input_error, router_not_available_error
from .executors import raster_cog, station_timeseries, vector_fgb
from .transforms import join as join_transform
from .transforms import tiled_mosaic

logger = logging.getLogger("trid3nt_server.agent.tools.fetchers._router.router")

__all__ = [
    "synthesize_metadata",
    "synthesize_payload_estimator",
    "validate_params",
    "select_executor",
    "route",
]

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
        cacheable=True,
        supports_global_query=spec.supports_global_query,
        payload_mb_estimator_name="estimate_payload_mb",
        open_world_hint=True,
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

    def estimate_payload_mb(bbox: Any = None, **kw: Any) -> float:
        sq = _sq_deg(bbox)
        floor = pe.floor_mb
        if pe.model == "bbox_area":
            return max(floor, (pe.mb_per_sq_deg or 0.01) * sq)
        if pe.model == "per_feature":
            feats = (pe.features_per_sq_deg or 100.0) * sq
            return max(floor, feats * (pe.kb_per_feature or 1.0) / 1024.0)
        if pe.model == "per_station":
            stations = (pe.stations_per_sq_deg or 2.0) * sq
            n_days = _n_days(kw.get("start_date"), kw.get("end_date"))
            kb = stations * (pe.kb_per_station_per_day or 2.0) * n_days + (pe.overhead_kb or 0.0)
            return max(floor, kb / 1024.0)
        if pe.model == "tiled":
            tile_deg2 = pe.tile_deg2 or 0.5
            ntiles = max(1, int(sq / tile_deg2 + 0.999))
            return max(floor, ntiles * (pe.mb_per_tile or 0.05))
        return max(floor, 0.01 * sq)

    return estimate_payload_mb


# --------------------------------------------------------------------------- #
# Param validation + gates (BEFORE any network call).
# --------------------------------------------------------------------------- #


def _quantize_bbox(bbox: tuple[float, ...], directive: str | None) -> tuple[float, ...]:
    if not directive:
        return tuple(round(v, 6) for v in bbox)
    if directive == "round_6dp":
        return tuple(round(v, 6) for v in bbox)
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

        else:  # str
            out[pname] = str(value)

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


# --------------------------------------------------------------------------- #
# Executor selection + LayerURI emission.
# --------------------------------------------------------------------------- #


def select_executor(spec: SourceSpec) -> Callable[[SourceSpec, dict[str, Any]], bytes]:
    """Return the ``(spec, params) -> bytes`` closure for the spec's shape/transform."""
    if spec.join is not None:
        return join_transform.execute
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
        if pspec.type == "bbox" and pname in params:
            b = params[pname]
            bbox = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
            break
    variable = params.get("variable") or params.get("product") or spec.source_class
    layer_id = f"{spec.source_class}-{variable}"
    # gridmet's twin omits LayerURI.bbox; emit_bbox=false suppresses it (parity).
    if not spec.output.emit_bbox:
        bbox = None
    return LayerURI(
        layer_id=layer_id,
        name=f"{spec.source_class} {variable}",
        layer_type=spec.output.layer_type,
        uri=uri,
        style_preset=_template(spec.output.style_preset, params),
        role=spec.output.role,
        units=spec.normalize.units,
        bbox=bbox,
    )


def route(spec: SourceSpec, raw_params: dict[str, Any]) -> LayerURI:
    """The engine: validate -> gate -> dispatch -> cache -> emit LayerURI."""
    metadata = synthesize_metadata(spec)
    params = validate_params(spec, raw_params)
    executor = select_executor(spec)

    result = read_through(
        metadata=metadata,
        params=params,
        ext=spec.output.ext,
        fetch_fn=lambda: executor(spec, params),
    )
    assert result.uri is not None, "router source is cacheable; uri must be set"
    return build_layer_uri(spec, params, result.uri)
