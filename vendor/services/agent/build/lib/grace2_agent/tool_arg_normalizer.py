"""Tool-argument normalizer — kwargs cleanup at the call site (job-0164).

Gemini routinely invents kwargs the tool doesn't actually accept
(``run_name``, ``scenario_id``, ``description``, ``rainfall_event``,
``return_period_years`` when the function declared ``return_period_yr``, etc.).
Strict Python signatures fail loud on every one of them as
``TypeError: <fn>() got an unexpected keyword argument <name>``. We've patched
the most common offenders piecemeal — this module is the **centralized sweep**.

What this module does, at the agent's ``_invoke_tool_via_emitter`` boundary,
BEFORE ``entry.fn(**params)``:

1. **Alias mapping** — known abbreviation pairs (``_yr`` → ``_years``,
   ``_hr`` → ``_hours``, etc.) are rewritten if the tool's signature accepts
   the canonical name but the LLM provided the alias (or vice-versa).
2. **camelCase / snake_case bridging** — if the LLM sends ``durationHours``
   and the tool accepts ``duration_hours``, we rename.
3. **String-form forcing parsing** — when the LLM stuffs the design-storm
   spec into a string like ``"atlas14_100yr"`` or ``"100-yr / 24-hr design
   storm"``, we extract ``return_period_years=100`` / ``duration_hours=24``
   so downstream tools see the canonical fields.
4. **Unknown-kwarg absorption** — params not in the tool's signature and not
   absorbed by ``**kwargs`` get logged and dropped, never raised.

The function ``normalize_args(tool_name, raw_args, fn)`` is the public entry
point; ``fn`` is the registered callable (we inspect its signature directly
rather than maintain a parallel registry of accepted params).

Design notes:

- **No tool-body changes required.** This module's whole point is to keep the
  57 tool implementations free of ``**_extra_ignored`` boilerplate. The
  normalizer reads ``inspect.signature(fn)`` and decides what to forward.
- **Logs are the audit trail.** Every alias rewrite + drop emits a single INFO
  / DEBUG line so we can spot recurring LLM mistakes and bake them into the
  alias table.
- **Idempotent + side-effect free.** Returns a fresh dict; never mutates the
  caller's params. Safe to call inside hot loops.
- **Generic aliases first; tool-specific overrides win.** The ``_ALIAS_MAP``
  is global (``return_period_years`` ↔ ``return_period_yr`` works across
  every flood tool). Tool-specific quirks live in ``_TOOL_SPECIFIC_ALIASES``.
"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("grace2_agent.tool_arg_normalizer")

__all__ = [
    "coerce_bbox_value",
    "coerce_latlon",
    "normalize_args",
    "parse_forcing_string",
    "snake_case",
]


class LatLonCoercionError(ValueError):
    """Raised by :func:`coerce_latlon` when a value is genuinely not 2 numbers.

    Distinct ``ValueError`` subtype so callers can catch *only* the
    latlon-coercion failure and surface a clean typed error
    (e.g. ``MODFLOW_PARAMS_INVALID``) instead of swallowing an unrelated
    ``ValueError``.
    """


# --------------------------------------------------------------------------- #
# Alias maps
# --------------------------------------------------------------------------- #

#: Bidirectional alias pairs. If a tool accepts the canonical (left) form and
#: the LLM provided the alias (right), we rename; and vice-versa. The pairs are
#: matched on **exact** name equality, not substring — keeps the table tight.
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
    """Flatten the bidirectional pairs into a directed alias→canonical map."""
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
    "run_model_flood_scenario": {
        # Gemini sometimes uses "place" / "location_name" instead of
        # "location_query" because the docstring's "Examples:" block names
        # places freely.
        "place": "location_query",
        "location_name": "location_query",
        "location": "location_query",
    },
    "run_model_flood_habitat_scenario": {
        "place": "place_label",
        "location_name": "place_label",
    },
    # -----------------------------------------------------------------------
    # NWS alert tools (job-0261): the LLM names the state freely ("state",
    # "state_code", "location", "region") — all land on the canonical "area"
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
    # Wave 4.10 endpoint aliases (job B13).
    # For each new tool: param-name variants Gemini is likely to invent based
    # on (a) common GIS/API terminology, (b) naming patterns in adjacent tools,
    # (c) docstring prose that names related concepts.
    # -----------------------------------------------------------------------
    "fetch_fema_nfhl_zones": {
        # bbox aliases (common across all spatial tools)
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        # sfha_only aliases — Gemini may expand the acronym or use noun form
        "sfha": "sfha_only",
        "special_flood_hazard": "sfha_only",
        "sfha_filter": "sfha_only",
        "flood_hazard_only": "sfha_only",
        # zone_filter aliases — Gemini may use plural or shorter names
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
        # variable aliases — Gemini may use "vars", "fields", or shortened forms
        "vars": "variable",
        "fields": "variable",
        "variables": "variable",
        "field": "variable",
        # forecast_hour aliases — common meteorological shorthand
        "fcst_hr": "forecast_hour",
        "fhr": "forecast_hour",
        "hour": "forecast_hour",
        "lead_hour": "forecast_hour",
        "lead_time": "forecast_hour",
        "forecast_lead": "forecast_hour",
        # cycle aliases — Gemini may use ISO or descriptive names
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
        # valid_time aliases — Gemini may use datetime / date / time
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
        # layer aliases — Gemini may use "type", "layer_type", or specific layer names
        "layer_type": "layer",
        "geometry_type": "layer",
        "levee_type": "layer",
        "feature_type": "layer",
        "dataset": "layer",
    },
    "fetch_usace_dams": {
        # bbox is the only param; cover its common aliases
        "bounding_box": "bbox",
        "extent": "bbox",
        "bounds": "bbox",
        "region": "bbox",
        "area": "bbox",
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
        # start_time aliases — Gemini often invents start_date / begin / from
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
        # product aliases — Gemini may use "data_type", "observation_type"
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
        # scenario_ft aliases — Gemini may use scenario, sea_level_rise, slr
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
        # output aliases — Gemini may use "variable", "product", "data_type"
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
        # seed_point aliases — Gemini may use "point", "location", "coordinate"
        "point": "seed_point",
        "location": "seed_point",
        "coordinate": "seed_point",
        "coordinates": "seed_point",
        "lat_lon": "seed_point",
        "latlon": "seed_point",
        # comid aliases — Gemini may use "reach_id", "nhd_id", "feature_id"
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
        # field aliases — Gemini may use "attribute", "variable", "soil_property"
        "attribute": "field",
        "variable": "field",
        "soil_property": "field",
        "property": "field",
        "soil_attribute": "field",
        "soil_field": "field",
        # timeout_s aliases — Gemini may omit the _s suffix or use different forms
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
        # variable aliases — same as fetch_hrrr_forecast
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
        # cycle aliases — same as fetch_hrrr_forecast
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
        # resolution aliases — Gemini may use "res", "cell_size", "pixel_size"
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
        # layer aliases — Gemini may use "variable", "fuel_layer", "product"
        "variable": "layer",
        "fuel_layer": "layer",
        "product": "layer",
        "dataset": "layer",
        "layer_name": "layer",
        "fuel_type": "layer",
    },
}


#: Kwargs we silently drop without warning (Gemini convenience fields that
#: never carry signal). Logged at DEBUG level only.
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
    """Convert ``durationHours`` → ``duration_hours``.

    No-op on already-snake_case strings; pure (no global state).
    """
    if "_" in name or name.islower():
        # Already snake-ish (or single lowercase word) — leave alone.
        return name.lower() if name.isupper() else name
    return _CAMEL_RE.sub("_", name).lower()


def coerce_bbox_value(value: Any) -> list[float] | None:
    """Parse an LLM-emitted bbox into ``[min_lon, min_lat, max_lon, max_lat]``.

    Gemini frequently stuffs the bbox into a STRING — ``"[-81.9, 26.5, -81.7,
    26.6]"``, ``"-81.9,26.5,-81.7,26.6"``, ``"(-81.9 26.5 -81.7 26.6)"`` — even
    when the tool's Python signature wants a list of 4 floats. Several tools
    (``compute_impact_envelope``, ``compute_building_density``, …) then reject
    it with ``len(bbox) != 4`` (a string's char length) or a ``not a tuple/list``
    type error. Live 2026-06-16: ``compute_impact_envelope`` failed 3× on a
    string bbox, tripping the circuit breaker and blocking the ImpactPanel.

    Accepts: a 4-element list/tuple (coerced to floats), or a string holding 4
    comma/space-separated numbers with optional surrounding brackets/parens.
    Returns ``None`` when the value is not a recognizable 4-number bbox (caller
    leaves the original value untouched so the tool's own validator speaks).
    """
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
    # Strip surrounding quote characters first: a bbox double-encoded as a JSON
    # string arrives with LITERAL quote chars, e.g. ``'"-122.5,37.5,-121.5,38.5"'``
    # (observed live -- the first call failed with "bbox must be [min_lon,...]").
    # Peel up to two matching quote layers so '"\'...\'"' also normalizes.
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
    """Coerce an LLM-emitted lat/lon point into ``[lat, lon]`` (two floats).

    Bedrock Claude (and other providers) routinely pass a coordinate *point*
    parameter as a STRING rather than a JSON array — observed live (job-0317)
    on ``run_modflow_job``'s ``spill_location_latlon``::

        "40.8088861,-96.7077751"   "40.81, -96.71"
        "[40.81, -96.71]"          "(40.81, -96.71)"
        "40.81 -96.71"

    The naive ``tuple(float(v) for v in value)`` iterates the STRING'S
    CHARACTERS, so ``float('.')`` raises "could not convert string to float:
    '.'" and the whole run dies as ``MODFLOW_PARAMS_INVALID`` (non-retryable).
    Same coercion class as the job-0295 news-ingest fix.

    Accepts ALL of:
      * a real 2-element list/tuple of numbers (passed through as floats);
      * ``"lat,lon"`` / ``"lat, lon"`` (comma, optional whitespace);
      * ``"[lat, lon]"`` / ``"(lat, lon)"`` (one layer of brackets/parens);
      * whitespace-separated ``"lat lon"``.

    Returns ``[lat, lon]`` (floats). Order is preserved verbatim — this helper
    does NOT reorder or range-check (the downstream contract owns lat/lon range
    validation); it only guarantees two parsed floats.

    Raises:
        LatLonCoercionError: when ``value`` is genuinely not two numbers
            (wrong element count, non-numeric parts, ``None``, etc.).
    """
    if value is None:
        raise LatLonCoercionError("lat/lon is required (got None)")
    # Real list/tuple path — pass through with float coercion.
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
    """Parse a free-text design-storm string into ``{return_period_years, duration_hours}``.

    Handles the most common LLM-invented forms:

    - ``"atlas14_100yr"`` → ``{"return_period_years": 100}``
    - ``"atlas14_100yr_24hr"`` → ``{"return_period_years": 100, "duration_hours": 24}``
    - ``"100-yr / 24-hr design storm"`` → both fields
    - ``"500 year"`` → ``{"return_period_years": 500}``
    - ``"6 hour"`` → ``{"duration_hours": 6}``

    Returns an empty dict if nothing recognizable is found — the caller still
    gets a dict and can fall back to defaults.
    """
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
    """Return ``(accepted_param_names, accepts_var_keyword)`` for ``fn``.

    If the function declares ``**kwargs`` (any var-keyword param), the second
    element is True and the normalizer leaves unknown kwargs alone — the
    function will absorb them.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        # Builtins / C-extension callables we can't introspect — be conservative
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
# Public entry point
# --------------------------------------------------------------------------- #


def normalize_args(
    tool_name: str,
    raw_args: dict[str, Any],
    fn: Callable[..., Any],
) -> dict[str, Any]:
    """Normalize ``raw_args`` so ``fn(**normalized)`` won't raise on Gemini quirks.

    Pipeline (each step idempotent, fresh-dict output):

    1. **camelCase → snake_case** on every key the function does not already
       accept verbatim. Bypasses the rename if the snake form is also unknown.
    2. **Tool-specific alias** (``_TOOL_SPECIFIC_ALIASES[tool_name]``) rewrites.
    3. **Generic bidirectional alias** (``_BIDIRECTIONAL_ALIASES``) rewrites —
       only fires if the rename lands the kwarg on an accepted-param name and
       the canonical name isn't already in ``raw_args``.
    4. **String-form forcing parsing** — if ``raw_args`` carries ``forcing``
       or ``rainfall_event`` as a string AND the function accepts
       ``return_period_years`` / ``duration_hours``, extract those.
    5. **Silent-drop list** — known Gemini-convenience kwargs dropped at DEBUG.
    6. **Absorb-and-log** — remaining unknown kwargs dropped at INFO (with the
       tool name) so we can surface them in logs and add them to the alias
       map / silent-drop list over time.

    If the function declares ``**kwargs``, steps 5–6 are bypassed (the
    function explicitly opted into "give me everything").

    Args:
        tool_name: ``TOOL_REGISTRY`` key — used for per-tool override lookup
            and for log attribution.
        raw_args: the params dict as the LLM produced it (already
            ``parse_arguments_string``-decoded if string-form).
        fn: the registered callable. The signature is inspected to decide
            what's accepted; we never call it here.

    Returns:
        A fresh dict safe to splat into ``fn(**…)``. Never raises.
    """
    if not raw_args:
        return {}
    accepted, accepts_var_keyword = _accepted_params(fn)
    tool_aliases = _TOOL_SPECIFIC_ALIASES.get(tool_name, {})
    out: dict[str, Any] = {}
    dropped_unknown: list[str] = []
    dropped_silent: list[str] = []

    generic_alias_map = _build_alias_map()
    for key, value in raw_args.items():
        target = key

        # Step 1: camelCase → snake_case. Always normalize the case form so
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
            # Function explicitly absorbs unknowns — pass through.
            out[key] = value
        elif key in _SILENT_DROP or target in _SILENT_DROP:
            dropped_silent.append(key)
        else:
            dropped_unknown.append(key)

    # Step 4: string-form forcing parsing. If the LLM sent ``forcing=…`` or
    # ``rainfall_event=…`` AND the tool accepts the canonical year/hour fields,
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
