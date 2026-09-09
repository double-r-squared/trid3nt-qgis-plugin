"""Kwargs cleanup at the dispatch seam, before ``entry.fn(**params)``: aliases and
camelCase keys renamed onto names the signature accepts, a string-form design storm
parsed into its canonical fields, and anything the signature cannot absorb DROPPED
rather than raised. Pure and idempotent - a fresh dict out, the caller's params
untouched, never raises. Every rewrite and drop emits one log line."""

from __future__ import annotations

import difflib
import inspect
import logging
import os
import re
import typing
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("trid3nt_server.tools.tool_arg_normalizer")

__all__ = [
    "autofill_missing_bbox",
    "coerce_bbox_value",
    "coerce_latlon",
    "fuzzy_correct_enum_args",
    "normalize_args",
    "parse_forcing_string",
    "snake_case",
]


class LatLonCoercionError(ValueError):
    """Raised by :func:`coerce_latlon` when a value is genuinely not two numbers.
    A distinct subtype so a caller catches only this, not an unrelated
    ``ValueError``."""


# --------------------------------------------------------------------------- #
# Alias maps
# --------------------------------------------------------------------------- #

#: Bidirectional alias pairs. If a tool accepts the canonical (left) form and
#: the LLM provided the alias (right), we rename; and vice-versa. The pairs are
#: matched on **exact** name equality, not substring -- keeps the table tight.
#:
#: Add a new entry here whenever logs show a recurring kwarg-name miss.
_BIDIRECTIONAL_ALIASES: tuple[tuple[str, str], ...] = (
    ("return_period_years", "return_period_yr"),
    ("duration_hours", "duration_hr"),
    ("simulation_duration_hours", "simulation_duration_hr"),
    ("year_range", "years_range"),
    ("days_back", "days"),
    ("species_name", "scientific_name"),
)


def _build_alias_map() -> dict[str, str]:
    """Flatten the bidirectional pairs into a directed alias -> canonical map."""
    m: dict[str, str] = {}
    for canon, alias in _BIDIRECTIONAL_ALIASES:
        # Both directions land in the map keyed by the "wrong" name pointing at
        # the "right" name. At normalize time we look up params[alias] and
        # rename if the tool's signature accepts the canonical form.
        m[alias] = canon
        m[canon] = alias
    return m


#: Per-tool override aliases that don't fit the generic bidirectional table.
#:
#: Shape: ``{tool_name: {wrong_kwarg: right_kwarg}}``. Tool-specific entries
#: win over the generic alias map.
_TOOL_SPECIFIC_ALIASES: dict[str, dict[str, str]] = {
    "sfincs_flood": {
        # The model reaches for "place" / "location_name" instead of
        # "location_query" because the docstring names places freely.
        "place": "location_query",
        "location_name": "location_query",
        "location": "location_query",
    },
    # -----------------------------------------------------------------------
    # NWS alert tools: the LLM names the state freely ("state",
    # "state_code", "location", "region") -- all land on the canonical "area"
    # param so the precise server-side ?area= filter engages instead of the
    # unscoped CONUS sweep.
    # -----------------------------------------------------------------------
    "fetch_nws_alerts_conus": {
        "state": "area",
        "state_code": "area",
        "state_name": "area",
        "location": "area",
        "region": "area",
    },
    "fetch_nws_event": {
        "state": "area",
        "state_code": "area",
        "state_name": "area",
        "location": "area",
        "region": "area",
        "fips": "area",
        "county_fips": "area",
    },
    # -----------------------------------------------------------------------
    # endpoint aliases.
    # For each new tool: param-name variants the model is likely to invent based
    # on (a) common GIS/API terminology, (b) naming patterns in adjacent tools,
    # (c) docstring prose that names related concepts.
    # -----------------------------------------------------------------------
    "fetch_fema_nfhl_zones": {
        # bbox aliases (common across all spatial tools)
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        # sfha_only aliases -- the model may expand the acronym or use noun form
        "sfha": "sfha_only",
        "special_flood_hazard": "sfha_only",
        "sfha_filter": "sfha_only",
        "flood_hazard_only": "sfha_only",
        # zone_filter aliases -- the model may use plural or shorter names
        "zones": "zone_filter",
        "flood_zones": "zone_filter",
        "zone_codes": "zone_filter",
        "flood_zone_filter": "zone_filter",
        "zone_types": "zone_filter",
    },
    "fetch_hrrr_forecast": {
        # bbox aliases
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        # variable aliases -- the model may use "vars", "fields", or shortened forms
        "vars": "variable",
        "fields": "variable",
        "variables": "variable",
        "field": "variable",
        # forecast_hour aliases -- common meteorological shorthand
        "fcst_hr": "forecast_hour",
        "fhr": "forecast_hour",
        "hour": "forecast_hour",
        "lead_hour": "forecast_hour",
        "lead_time": "forecast_hour",
        "forecast_lead": "forecast_hour",
        # cycle aliases -- the model may use ISO or descriptive names
        "cycle_iso": "cycle",
        "run_time": "cycle",
        "init_time": "cycle",
        "cycle_time": "cycle",
        "model_run": "cycle",
    },
    "fetch_noaa_nwm_streamflow": {
        # bbox aliases
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        # product / configuration aliases
        "configuration": "product",
        "model_run": "product",
        "cfg": "product",
        "run_type": "product",
        "model_config": "product",
        # valid_time aliases -- the model may use datetime / date / time
        "datetime": "valid_time",
        "date": "valid_time",
        "time": "valid_time",
        "timestamp": "valid_time",
        "valid_datetime": "valid_time",
        # forecast_hour aliases
        "fcst_hr": "forecast_hour",
        "fhr": "forecast_hour",
        "hour": "forecast_hour",
        "lead_hour": "forecast_hour",
    },
    "fetch_usace_levees": {
        # bbox aliases
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        # layer aliases -- the model may use "type", "layer_type", or specific layer names
        "layer_type": "layer",
        "geometry_type": "layer",
        "levee_type": "layer",
        "feature_type": "layer",
        "dataset": "layer",
    },
    "fetch_usace_dams": {
        # bbox aliases
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        "region": "bbox",
        "area": "bbox",
        # hazard_potential aliases
        "hazard": "hazard_potential",
        "hazard_class": "hazard_potential",
        "hazard_classification": "hazard_potential",
        # min_height_ft aliases - LLMs invent min_height / height variants
        "min_height": "min_height_ft",
        "minimum_height": "min_height_ft",
        "min_dam_height": "min_height_ft",
        "min_height_feet": "min_height_ft",
        "height_min": "min_height_ft",
    },
    "fetch_usace_nsi": {
        # bbox is the only param
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        "region": "bbox",
        "area": "bbox",
    },
    "fetch_asos_metar": {
        # bbox aliases
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        # start_time aliases -- the model often invents start_date / begin / from
        "start_date": "start_time",
        "begin": "start_time",
        "start": "start_time",
        "from_time": "start_time",
        "datetime_start": "start_time",
        "time_start": "start_time",
        # end_time aliases
        "end_date": "end_time",
        "end": "end_time",
        "stop": "end_time",
        "to_time": "end_time",
        "datetime_end": "end_time",
        "time_end": "end_time",
    },
    "fetch_gridmet": {
        # bbox aliases
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        # variable aliases
        "vars": "variable",
        "field": "variable",
        "variables": "variable",
        "param": "variable",
        "metric": "variable",
        # start_date aliases
        "start": "start_date",
        "begin": "start_date",
        "from_date": "start_date",
        "datetime_start": "start_date",
        "start_time": "start_date",
        "date_start": "start_date",
        # end_date aliases
        "end": "end_date",
        "stop": "end_date",
        "to_date": "end_date",
        "datetime_end": "end_date",
        "end_time": "end_date",
        "date_end": "end_date",
    },
    "fetch_noaa_coops_tides": {
        # bbox aliases
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        # start_date aliases
        "start": "start_date",
        "begin": "start_date",
        "from_date": "start_date",
        "start_time": "start_date",
        "datetime_start": "start_date",
        "date_start": "start_date",
        # end_date aliases
        "end": "end_date",
        "stop": "end_date",
        "to_date": "end_date",
        "end_time": "end_date",
        "datetime_end": "end_date",
        "date_end": "end_date",
        # product aliases -- the model may use "data_type", "observation_type"
        "data_type": "product",
        "observation_type": "product",
        "tide_product": "product",
        "measurement": "product",
    },
    "fetch_noaa_slr_scenarios": {
        # bbox aliases
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        # scenario_ft aliases -- the model may use scenario, sea_level_rise, slr
        "scenario": "scenario_ft",
        "scenarios": "scenario_ft",
        "sea_level_rise": "scenario_ft",
        "slr": "scenario_ft",
        "slr_ft": "scenario_ft",
        "rise_ft": "scenario_ft",
        "feet": "scenario_ft",
    },
    "fetch_gtsm_tide_surge": {
        # bbox aliases
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        # start_date aliases
        "start": "start_date",
        "begin": "start_date",
        "from_date": "start_date",
        "start_time": "start_date",
        "datetime_start": "start_date",
        # end_date aliases
        "end": "end_date",
        "stop": "end_date",
        "to_date": "end_date",
        "end_time": "end_date",
        "datetime_end": "end_date",
        # output aliases -- the model may use "variable", "product", "data_type"
        "variable": "output",
        "product": "output",
        "data_type": "output",
        "output_type": "output",
        "field": "output",
    },
    "fetch_raws_weather": {
        # bbox aliases
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        # start_time aliases
        "start_date": "start_time",
        "begin": "start_time",
        "start": "start_time",
        "from_time": "start_time",
        "datetime_start": "start_time",
        "time_start": "start_time",
        # end_time aliases
        "end_date": "end_time",
        "end": "end_time",
        "stop": "end_time",
        "to_time": "end_time",
        "datetime_end": "end_time",
        "time_end": "end_time",
    },
    "fetch_nhdplus_nldi_navigate": {
        # seed_point aliases -- the model may use "point", "location", "coordinate"
        "point": "seed_point",
        "location": "seed_point",
        "coordinate": "seed_point",
        "coordinates": "seed_point",
        "lat_lon": "seed_point",
        "latlon": "seed_point",
        # comid aliases -- the model may use "reach_id", "nhd_id", "feature_id"
        "reach_id": "comid",
        "nhd_id": "comid",
        "feature_id": "comid",
        "nhdplus_id": "comid",
        "nhd_comid": "comid",
        # direction aliases
        "nav_direction": "direction",
        "navigation": "direction",
        "navigate": "direction",
        "upstream_downstream": "direction",
        # distance_km aliases
        "distance": "distance_km",
        "km": "distance_km",
        "length_km": "distance_km",
        "search_distance": "distance_km",
        "max_distance_km": "distance_km",
    },
    "fetch_statsgo_soils": {
        # bbox aliases
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        # field aliases -- the model may use "attribute", "variable", "soil_property"
        "attribute": "field",
        "variable": "field",
        "soil_property": "field",
        "property": "field",
        "soil_attribute": "field",
        "soil_field": "field",
        # timeout_s aliases -- the model may omit the _s suffix or use different forms
        "timeout": "timeout_s",
        "timeout_seconds": "timeout_s",
        "http_timeout": "timeout_s",
        "request_timeout": "timeout_s",
    },
    "fetch_hrrr_smoke": {
        # bbox aliases
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        # variable aliases -- same as fetch_hrrr_forecast
        "vars": "variable",
        "field": "variable",
        "variables": "variable",
        "smoke_variable": "variable",
        # forecast_hour aliases
        "fcst_hr": "forecast_hour",
        "fhr": "forecast_hour",
        "hour": "forecast_hour",
        "lead_hour": "forecast_hour",
        "lead_time": "forecast_hour",
        # cycle aliases -- same as fetch_hrrr_forecast
        "cycle_iso": "cycle",
        "run_time": "cycle",
        "init_time": "cycle",
        "cycle_time": "cycle",
        "model_run": "cycle",
    },
    "fetch_3dep_extra": {
        # bbox aliases
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        # resolution aliases -- the model may use "res", "cell_size", "pixel_size"
        "res": "resolution",
        "cell_size": "resolution",
        "pixel_size": "resolution",
        "spatial_resolution": "resolution",
        "grid_resolution": "resolution",
        # max_tiles aliases
        "tile_limit": "max_tiles",
        "max_tile_count": "max_tiles",
        "tiles": "max_tiles",
        "num_tiles": "max_tiles",
        # timeout_s aliases
        "timeout": "timeout_s",
        "timeout_seconds": "timeout_s",
        "http_timeout": "timeout_s",
        "request_timeout": "timeout_s",
    },
    "fetch_usfs_canopy_fuels": {
        # bbox aliases
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        # layer aliases -- the model may use "variable", "fuel_layer", "product"
        "variable": "layer",
        "fuel_layer": "layer",
        "product": "layer",
        "dataset": "layer",
        "layer_name": "layer",
        "fuel_type": "layer",
    },
}


#: Kwargs silently dropped (model-convenience fields that never carry signal).
#: Logged at DEBUG level only.
_SILENT_DROP: frozenset[str] = frozenset(
    {
        "run_name",
        "scenario_id",
        "scenario_name",
        "description",
        "comment",
        "user_intent",
        "explanation",
        "reasoning",
        "purpose",
        "note",
    }
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def snake_case(name: str) -> str:
    """Convert ``durationHours`` -> ``duration_hours``; a no-op on snake_case."""
    if "_" in name or name.islower():
        # Already snake-ish (or single lowercase word) -- leave alone.
        return name.lower() if name.isupper() else name
    return _CAMEL_RE.sub("_", name).lower()


def coerce_bbox_value(value: Any) -> list[float] | None:
    """A bbox as ``[min_lon, min_lat, max_lon, max_lat]``, from a 4-element sequence
    or a string of 4 comma/space-separated numbers. ``None`` when unrecognizable, so
    the caller leaves the value alone and the tool's own validator speaks."""
    if isinstance(value, (list, tuple)):
        if len(value) != 4:
            return None
        try:
            return [float(v) for v in value]
        except (TypeError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    # A bbox double-encoded as a JSON string arrives with LITERAL quote chars, e.g.
    # ``'"-122.5,37.5,-121.5,38.5"'``. Peel up to two matching quote layers so
    # '"\'...\'"' also normalizes.
    for _ in range(2):
        if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
            s = s[1:-1].strip()
        else:
            break
    # Strip one layer of surrounding brackets/parens, then split on commas
    # and/or whitespace. ``re.split`` on ``[,\s]+`` handles "a,b,c,d",
    # "a, b, c, d", and "a b c d" uniformly.
    if s[:1] in "[(" and s[-1:] in "])":
        s = s[1:-1]
    parts = [p for p in re.split(r"[,\s]+", s.strip()) if p]
    if len(parts) != 4:
        return None
    try:
        return [float(p) for p in parts]
    except ValueError:
        return None


def coerce_latlon(value: Any) -> list[float]:
    """A lat/lon point as ``[lat, lon]``, from a 2-element sequence or a
    ``"lat,lon"`` string. Order is preserved verbatim and NOTHING is range-checked;
    a value that is not two numbers raises ``LatLonCoercionError``."""
    if value is None:
        raise LatLonCoercionError("lat/lon is required (got None)")
    # Real list/tuple path -- pass through with float coercion.
    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            raise LatLonCoercionError(
                f"lat/lon must have exactly 2 elements, got {len(value)}: {value!r}"
            )
        try:
            return [float(v) for v in value]
        except (TypeError, ValueError) as exc:
            raise LatLonCoercionError(
                f"lat/lon elements must be numbers: {value!r}"
            ) from exc
    # Reject other non-string scalars (int/float alone isn't a pair; dict; etc.)
    if not isinstance(value, str):
        raise LatLonCoercionError(
            f"lat/lon must be a 2-list or 'lat,lon' string, got {type(value).__name__}"
        )
    # A coordinate parameter arrives as a STRING often enough to matter: the naive
    # ``tuple(float(v) for v in value)`` would iterate the string's CHARACTERS and
    # die on the decimal point, turning a routable call into a hard param error.
    s = value.strip()
    if not s:
        raise LatLonCoercionError("lat/lon string is empty")
    # Strip one layer of surrounding brackets/parens.
    if s[:1] in "[(" and s[-1:] in "])":
        s = s[1:-1].strip()
    # Split on commas and/or whitespace uniformly (handles "a,b", "a, b",
    # "a b"). Drop empties so trailing separators don't create blank parts.
    parts = [p for p in re.split(r"[,\s]+", s) if p]
    if len(parts) != 2:
        raise LatLonCoercionError(
            f"lat/lon must parse to exactly 2 numbers, got {len(parts)}: {value!r}"
        )
    try:
        return [float(p) for p in parts]
    except ValueError as exc:
        raise LatLonCoercionError(
            f"lat/lon parts must be numbers: {value!r}"
        ) from exc


def parse_forcing_string(s: str) -> dict[str, int]:
    """Parse a free-text design storm into ``{return_period_years,
    duration_hours}``; either key may be absent. An unrecognizable string yields an
    empty dict, never an error, so the caller can fall back to its defaults."""
    if not s:
        return {}
    out: dict[str, int] = {}
    lower = s.lower()
    m_yr = re.search(r"(\d+)\s*[-_]?\s*(?:yr|year)s?", lower)
    if m_yr:
        try:
            out["return_period_years"] = int(m_yr.group(1))
        except ValueError:
            pass
    m_hr = re.search(r"(\d+)\s*[-_]?\s*(?:hr|hour)s?", lower)
    if m_hr:
        try:
            out["duration_hours"] = int(m_hr.group(1))
        except ValueError:
            pass
    return out


def _accepted_params(fn: Callable[..., Any]) -> tuple[set[str], bool]:
    """``(accepted_param_names, accepts_var_keyword)`` for ``fn``. A ``**kwargs``
    declaration sets the flag, and the normalizer then leaves unknown kwargs
    alone."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        # Builtins / C-extension callables we can't introspect -- be conservative
        # and pass everything through unchanged.
        return set(), True
    accepted: set[str] = set()
    accepts_var_keyword = False
    for name, p in sig.parameters.items():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            accepts_var_keyword = True
            continue
        if p.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        accepted.add(name)
    return accepted, accepts_var_keyword


# --------------------------------------------------------------------------- #
# Structured AOI slice -- dispatch-time bbox auto-fill
# --------------------------------------------------------------------------- #

#: Param names treated as "bbox-like" for the auto-fill. Only
#: REQUIRED (no-default) signature params auto-fill -- an optional bbox means
#: the tool has its own semantics for "absent" and the server must not guess.
_BBOX_AUTOFILL_PARAMS: frozenset[str] = frozenset({"bbox", "aoi_bbox"})


def _valid_aoi(candidate: Any) -> list[float] | None:
    """Coerce + sanity-check an AOI candidate to a finite, ordered bbox."""
    import math

    if candidate is None:
        return None
    coerced = coerce_bbox_value(candidate)
    if coerced is None:
        return None
    if not all(math.isfinite(v) for v in coerced):
        return None
    if not (coerced[0] < coerced[2] and coerced[1] < coerced[3]):
        return None
    return coerced


def autofill_missing_bbox(
    tool_name: str,
    params: dict[str, Any],
    fn: Callable[..., Any],
    *,
    active_aoi: Any = None,
    case_bbox: Any = None,
) -> dict[str, Any]:
    """Fill a REQUIRED bbox-like param the model OMITTED - first valid wins, in the
    order explicit arg > active canvas AOI > Case bbox. An explicit value is NEVER
    overridden and an optional param never fills; never raises."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return params
    out: dict[str, Any] | None = None
    for name, p in sig.parameters.items():
        if name not in _BBOX_AUTOFILL_PARAMS:
            continue
        if p.kind in (
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            continue
        if p.default is not inspect.Parameter.empty:
            continue  # optional -- the tool's own default owns absence
        if params.get(name) is not None:
            continue  # explicit arg wins; never override
        filled: list[float] | None = None
        source: str | None = None
        for candidate, label in (
            (active_aoi, "active-aoi"),
            (case_bbox, "case-bbox"),
        ):
            filled = _valid_aoi(candidate)
            if filled is not None:
                source = label
                break
        if filled is None:
            continue
        if out is None:
            out = dict(params)
        out[name] = filled
        logger.info(
            "aoi-autofill[%s]: %s <- %s %s (model omitted it)",
            tool_name,
            name,
            source,
            filled,
        )
    return out if out is not None else params


# --------------------------------------------------------------------------- #
# Fuzzy enum-arg correction
# --------------------------------------------------------------------------- #
# A string arg that fails a ``Literal[...]`` schema ("truecolour" for
# Literal["truecolor", "ndvi"], "Flood-Depth" for "flood_depth") would
# otherwise fall straight through to the tool's typed error and burn a full
# model round on a near-miss the harness can fix deterministically. At the
# normalize seam we difflib-match the bad value against the param's
# declared Literal choices (cutoff 0.8, case/sep-insensitive) and substitute
# the canonical choice with one log line; anything under the cutoff is left
# untouched so the tool's own typed error still owns genuine mismatches.
#
# Kill-switch: ``TRID3NT_ENUM_FUZZY=0`` (or ``off``/``false``) disables the
# correction entirely (default ON).

#: difflib similarity cutoff for an enum correction (assignment-fixed).
_ENUM_FUZZY_CUTOFF = 0.8


def _enum_fuzzy_enabled() -> bool:
    raw = (os.environ.get("TRID3NT_ENUM_FUZZY") or "").strip().lower()
    return raw not in ("0", "off", "false", "no")


def _literal_values(annotation: Any) -> tuple[str, ...]:
    """The STRING choices of a ``Literal`` annotation, through one level of union
    nesting. A non-string literal member disqualifies the param entirely - only
    strings are ever corrected."""
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        vals = typing.get_args(annotation)
        if vals and all(isinstance(v, str) for v in vals):
            return tuple(vals)
        return ()
    # Union / Optional: scan members for a single string-Literal.
    import types as _types

    if origin is typing.Union or origin is getattr(_types, "UnionType", None):
        for member in typing.get_args(annotation):
            vals = _literal_values(member)
            if vals:
                return vals
    return ()


def _literal_choices(fn: Callable[..., Any]) -> dict[str, tuple[str, ...]]:
    """``{param_name: (choice, ...)}`` for every string-Literal param of ``fn``.
    Best-effort: an un-introspectable signature or unresolvable annotations return
    ``{}``, never an error."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {}
    try:
        hints = typing.get_type_hints(fn)
    except Exception:  # noqa: BLE001 -- string annotations may not resolve
        hints = {}
    out: dict[str, tuple[str, ...]] = {}
    for name, p in sig.parameters.items():
        annotation = hints.get(name, p.annotation)
        if annotation is inspect.Parameter.empty:
            continue
        vals = _literal_values(annotation)
        if vals:
            out[name] = vals
    return out


def _enum_norm(value: str) -> str:
    """Case/separator-insensitive comparison form ('Flood-Depth' -> 'flood_depth')."""
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def fuzzy_correct_enum_args(
    tool_name: str,
    params: dict[str, Any],
    fn: Callable[..., Any],
) -> dict[str, Any]:
    """difflib-correct string args that miss their param's ``Literal`` choices. A
    case/separator-insensitive hit maps straight to the canonical choice; BELOW the
    cutoff the value is left alone, so the tool's typed error still owns it."""
    if not params or not _enum_fuzzy_enabled():
        return params
    choices_by_param = _literal_choices(fn)
    if not choices_by_param:
        return params
    out: dict[str, Any] | None = None
    for name, choices in choices_by_param.items():
        value = params.get(name)
        if not isinstance(value, str) or value in choices:
            continue
        norm_map = {_enum_norm(c): c for c in choices}
        norm_val = _enum_norm(value)
        corrected: str | None = norm_map.get(norm_val)
        if corrected is None:
            close = difflib.get_close_matches(
                norm_val, list(norm_map), n=1, cutoff=_ENUM_FUZZY_CUTOFF
            )
            corrected = norm_map[close[0]] if close else None
        if corrected is None or corrected == value:
            continue
        if out is None:
            out = dict(params)
        out[name] = corrected
        logger.info(
            "tool_arg_normalizer[%s]: fuzzy enum correction %s=%r -> %r "
            "(choices=%s)",
            tool_name,
            name,
            value,
            corrected,
            list(choices),
        )
    return out if out is not None else params


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def normalize_args(
    tool_name: str,
    raw_args: dict[str, Any],
    fn: Callable[..., Any],
) -> dict[str, Any]:
    """Normalize ``raw_args`` so ``fn(**normalized)`` will not raise on a model's
    kwarg quirks; ``fn`` is INSPECTED, never called. A function declaring
    ``**kwargs`` keeps every unknown kwarg, otherwise unknowns are dropped."""
    if not raw_args:
        return {}
    # A model may pack the release point into ONE 'spill_location_latlon' string
    # instead of release_lat/release_lon. This MUST normalize HERE: the gate preview
    # reads normalized args before the tool function runs, and the approve-click
    # injects release_* only afterwards, so a tool-level parse guarded on "coords
    # absent" would never fire and the preview would mesh the wrong river.
    if (tool_name == "telemac_river_dye"
            and isinstance(raw_args.get("spill_location_latlon"), str)
            and raw_args.get("release_lat") is None
            and raw_args.get("release_lon") is None):
        try:
            _lat_s, _lon_s = raw_args["spill_location_latlon"].split(",", 1)
            raw_args = dict(raw_args)
            raw_args["release_lat"] = float(_lat_s)
            raw_args["release_lon"] = float(_lon_s)
            raw_args.pop("spill_location_latlon")
            logger.info(
                "normalize_args(telemac_river_dye): spill_location_latlon -> "
                "release_lat=%s release_lon=%s",
                raw_args["release_lat"], raw_args["release_lon"],
            )
        except (ValueError, TypeError):
            logger.warning(
                "normalize_args(telemac_river_dye): unparseable "
                "spill_location_latlon %r - left for the tool to drop",
                raw_args.get("spill_location_latlon"),
            )
    accepted, accepts_var_keyword = _accepted_params(fn)
    tool_aliases = _TOOL_SPECIFIC_ALIASES.get(tool_name, {})
    out: dict[str, Any] = {}
    dropped_unknown: list[str] = []
    dropped_silent: list[str] = []

    generic_alias_map = _build_alias_map()
    for key, value in raw_args.items():
        target = key

        # Step 1: camelCase -> snake_case. Always normalize the case form so
        # subsequent alias chains can match. If the snake form is in accepted
        # OR in the alias map, the rename is useful; otherwise leave alone.
        if target not in accepted:
            snake = snake_case(target)
            if snake != target and (
                snake in accepted
                or snake in tool_aliases
                or snake in generic_alias_map
            ):
                logger.debug(
                    "tool_arg_normalizer[%s]: camelCase rename %r -> %r",
                    tool_name,
                    target,
                    snake,
                )
                target = snake

        # Step 2: tool-specific alias.
        if target not in accepted and target in tool_aliases:
            mapped = tool_aliases[target]
            if mapped in accepted:
                logger.info(
                    "tool_arg_normalizer[%s]: tool-specific alias %r -> %r",
                    tool_name,
                    key,
                    mapped,
                )
                target = mapped

        # Step 3: generic bidirectional alias.
        if target not in accepted:
            cand = generic_alias_map.get(target)
            if cand and cand in accepted and cand not in out and cand not in raw_args:
                logger.info(
                    "tool_arg_normalizer[%s]: generic alias %r -> %r",
                    tool_name,
                    key,
                    cand,
                )
                target = cand

        # Step 4 helper: string-form forcing parsing handled after the loop so
        # we have the final mapped set. Track originals for that step.

        # Final placement decision.
        if target in accepted:
            # Don't overwrite an already-mapped canonical value with an alias's
            # value (canonical wins on conflict).
            if target not in out:
                out[target] = value
        elif accepts_var_keyword:
            # Function explicitly absorbs unknowns -- pass through.
            out[key] = value
        elif key in _SILENT_DROP or target in _SILENT_DROP:
            dropped_silent.append(key)
        else:
            dropped_unknown.append(key)

    # Step 4: string-form forcing parsing. If the model sent ``forcing=...`` or
    # ``rainfall_event=...`` AND the tool accepts the canonical year/hour fields,
    # extract them. Don't overwrite explicit fields the LLM also supplied.
    forcing_str = raw_args.get("forcing") or raw_args.get("rainfall_event")
    if isinstance(forcing_str, str) and (
        "return_period_years" in accepted or "duration_hours" in accepted
    ):
        parsed = parse_forcing_string(forcing_str)
        for parsed_key, parsed_val in parsed.items():
            if parsed_key in accepted and parsed_key not in out:
                logger.info(
                    "tool_arg_normalizer[%s]: parsed forcing string %r -> %s=%s",
                    tool_name,
                    forcing_str,
                    parsed_key,
                    parsed_val,
                )
                out[parsed_key] = parsed_val

    # Step 4b: bbox value coercion. The LLM routinely emits ``bbox`` as a
    # STRING ("[-81.9, 26.5, -81.7, 26.6]" / "-81.9,26.5,...") even when the
    # tool wants a list of 4 floats; tools then reject it (``len(bbox) != 4``
    # on the char count, or a type error). Coerce in place when the tool
    # accepts ``bbox`` and the value is a recognizable 4-number bbox; leave it
    # untouched otherwise so the tool's own validator surfaces a clear error.
    if "bbox" in accepted and "bbox" in out and not (
        isinstance(out["bbox"], (list, tuple))
        and len(out["bbox"]) == 4
        and all(isinstance(v, (int, float)) for v in out["bbox"])
    ):
        coerced = coerce_bbox_value(out["bbox"])
        if coerced is not None:
            logger.info(
                "tool_arg_normalizer[%s]: coerced bbox %r -> %s",
                tool_name,
                out["bbox"],
                coerced,
            )
            out["bbox"] = coerced

    out = fuzzy_correct_enum_args(tool_name, out, fn)

    # Logging tail.
    if dropped_silent:
        logger.debug(
            "tool_arg_normalizer[%s]: silently dropped %s (convenience kwargs)",
            tool_name,
            dropped_silent,
        )
    if dropped_unknown:
        logger.info(
            "tool_arg_normalizer[%s]: dropped unknown kwargs %s (signature accepts %s)",
            tool_name,
            dropped_unknown,
            sorted(accepted),
        )

    return out
