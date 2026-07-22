"""``fetch_census_acs`` atomic tool -- US Census ACS 5-year demographics as a
census-tract choropleth FlatGeobuf.

Generalizes the population-only fetchers (``fetch_hrsl_population`` /
``fetch_worldpop``) to **arbitrary American Community Survey (ACS) 5-year
demographics** -- median income, age, home value, poverty rate, renter share,
no-vehicle share, ... -- joined to authoritative U.S. census-tract geometry and
returned as a FlatGeobuf choropleth clipped to a bbox. This is the canonical
vulnerability / environmental-justice (EJ) demographic surface: pair it with a
hazard footprint to ask "what is the median income of the tracts this flood
inundates" or "rank exposed tracts by poverty".

**Two keyless authoritative sources, joined by GEOID (FR-DC):**

1. **Tract geometry** -- the Census Bureau's TIGERweb Generalized ACS tract
   polygon layer (ArcGIS REST, keyless)::

       https://tigerweb.geo.census.gov/arcgis/rest/services/
           Generalized_ACS2023/Tracts_Blocks/MapServer/4/query

   Layer 4 ("Census Tracts 500K") carries the full 11-digit ``GEOID``
   (state+county+tract) plus ``STATE``/``COUNTY``/``TRACT``/``NAME`` and the
   polygon geometry. Queried by ``esriGeometryEnvelope`` intersect, paged.

2. **ACS estimates** -- the Census Bureau's ``data.census.gov`` backend table
   API (keyless; the same data served behind the data.census.gov website,
   carrying arbitrary ACS detailed-table B-codes)::

       https://data.census.gov/api/access/data/table
           ?g=0500000US<state><county>$1400000   (all tracts in a county)
           &tid=ACSDT5Y<year>.<table>            (e.g. ACSDT5Y2022.B19013)

   Returns ``GEO_ID`` (``1400000US48201100001`` -> strip to the 11-digit
   ``48201100001``), the requested table's estimate columns, and ``NAME``.

**Why not ``api.census.gov``?** The classic ``api.census.gov/data/<year>/
acs/acs5`` Data API now redirects EVERY request (all years, all geographies,
even ``us:1``) to ``/data/missing_key.html`` -- it requires an API key for all
data pulls as of 2026. This tool is KEYLESS by mandate, so it uses the
``data.census.gov`` backend (keyless) for estimates instead. If the operator
sets the optional ``CENSUS_API_KEY`` env var, the classic Data API is used as a
*primary* with the backend as fallback; without a key the backend is primary
(FR-DC-? data-source fallback norm: primary -> fallback -> honest typed error,
never a silent dead-end).

**Friendly variable registry** (the ``variable`` param maps a friendly name to
ACS detailed-table codes; an arbitrary raw B-code is also accepted):

    - ``median_income``     -> B19013_001E (median household income, USD)
    - ``median_age``        -> B01002_001E (median age, years)
    - ``median_home_value`` -> B25077_001E (median owner-occupied value, USD)
    - ``poverty_rate``      -> 100 * B17001_002E / B17001_001E (percent)
    - ``pct_renters``       -> 100 * B25003_003E / B25003_001E (percent)
    - ``pct_no_vehicle``    -> 100 * (B25044_003E+B25044_010E) / B25044_001E (%)

Plus passthrough of a raw ``B#####_###E`` code (returns that estimate as the
choropleth ``value``).

**When to use:**
- User asks for a specific demographic (income, age, home value, poverty,
  renters, car-free households) for an area, or "show median income by tract".
- Agent needs a demographic surface to intersect with a hazard footprint for
  exposure / equity analysis ("median income of the inundated tracts").
- A vulnerability overlay where the *underlying estimate* is wanted (vs the CDC
  SVI percentile rankings from ``fetch_cdc_svi``).

**When NOT to use:**
- For pre-composited social-vulnerability *percentile rankings* -> use
  ``fetch_cdc_svi`` (this tool returns raw estimates, not ranks).
- For a gridded population *raster* -> use ``fetch_hrsl_population`` /
  ``fetch_worldpop`` (this tool returns tract polygons, not a raster).
- For areas outside the United States -> ACS + tract geography is U.S.-only
  (50 states + DC + PR). An empty FGB is returned for non-U.S. bboxes.

**Parameters:**
    bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required --
        ``supports_global_query=False`` (a nationwide tract pull is ~85k
        polygons). County-or-smaller extent recommended. Example for urban
        Houston / Harris County TX: ``(-95.45, 29.65, -95.25, 29.85)``.
    variable: a friendly name (see registry) or a raw ACS estimate code
        (``B19013_001E``). Default ``"median_income"``. Unknown -> typed input
        error (never a silent wrong layer).
    year: ACS 5-year vintage end-year (default ``2022``).

**Returns:**
    ``LayerURI`` -> FlatGeobuf in the cache bucket. Each feature is a Polygon
    (census tract) in EPSG:4326. Properties: ``geoid`` (str, 11-digit),
    ``name`` (str), ``state`` (str FIPS), ``county`` (str FIPS), ``variable``
    (str friendly name), ``value`` (float|null -- the estimate or derived
    percent; null where the tract is suppressed / has no households / falls in
    water), ``units`` (str: ``usd``|``years``|``percent``|``count``).
    ``layer_type="vector"``, ``role="primary"``, ``style_preset="acs_choropleth"``.

**Cross-tool dependencies (FR-TA-3):**
    - Feeds INTO ``compute_zonal_statistics`` / ``clip_vector_to_polygon`` for
      hazard-exposure intersections and admin-bounded reports.
    - Combine WITH ``run_model_flood_scenario`` / ``fetch_noaa_slr_scenarios`` /
      ``fetch_firms_active_fire`` (hazard footprints) for demographic-weighted
      exposure; ``fetch_administrative_boundaries`` for county scope.
    - Complements ``fetch_cdc_svi`` (percentile ranks vs raw estimates).

**Cache:** ``static-30d`` (FR-DC-2). ACS 5-year vintages are annual; a 30-day
stale window is appropriate. Cache key factors bbox (6 dp) + variable + year.

**FR-AS-11 typed-error surface:** ``CensusACSError`` (base, retryable=True),
``CensusACSInputError`` (bad bbox / unknown variable, retryable=False),
``CensusACSUpstreamError`` (TIGERweb or data.census.gov network/HTTP/parse
failure, retryable=True), ``CensusACSEmptyError`` (no tracts in bbox -- NOT
raised by default; an empty FGB is serialized so the layer appears with a
zero-feature notice -- e.g. open ocean or outside the U.S.).

``supports_global_query=False``. No API key required.

Endpoints verified live 2026-06-27.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

import httpx

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_census_acs",
    "estimate_payload_mb",
    "CensusACSError",
    "CensusACSInputError",
    "CensusACSUpstreamError",
    "CensusACSEmptyError",
    "ACS_VARIABLES",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_resolve_variable",
    "_fetch_tiger_tracts",
    "_fetch_acs_values",
    "_compute_value",
    "_features_to_flatgeobuf",
    "_fetch_acs_bytes",
    "TIGER_TRACT_QUERY_URL",
    "DATA_CENSUS_TABLE_URL",
]

logger = logging.getLogger("trid3nt_server.tools.fetch_census_acs")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class CensusACSError(RuntimeError):
    """Base class for fetch_census_acs failures."""

    error_code: str = "CENSUS_ACS_ERROR"
    retryable: bool = True


class CensusACSInputError(CensusACSError):
    """Caller passed an invalid bbox or an unknown variable."""

    error_code = "CENSUS_ACS_INPUT_INVALID"
    retryable = False


class CensusACSUpstreamError(CensusACSError):
    """TIGERweb or data.census.gov request failed (network, HTTP, or parse)."""

    error_code = "CENSUS_ACS_UPSTREAM_ERROR"
    retryable = True


class CensusACSEmptyError(CensusACSError):
    """No tracts found in bbox.

    NOT raised by default (an empty FGB is serialized instead -- a bbox over
    open ocean or outside the U.S. legitimately has no tracts), but available
    for a future strict-mode opt-in.
    """

    error_code = "CENSUS_ACS_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants + variable registry.
# ---------------------------------------------------------------------------

#: TIGERweb Generalized-ACS tract polygon query endpoint (keyless ArcGIS REST).
#: Layer 4 = "Census Tracts 500K" -- carries the 11-digit GEOID + geometry.
TIGER_TRACT_QUERY_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "Generalized_ACS2023/Tracts_Blocks/MapServer/4/query"
)

#: data.census.gov backend table API (keyless) -- ACS detailed tables by GEO id.
DATA_CENSUS_TABLE_URL = "https://data.census.gov/api/access/data/table"

#: Classic Census Data API (key-gated as of 2026; used only when CENSUS_API_KEY
#: is set, with the data.census.gov backend as the keyless fallback).
_CENSUS_DATA_API_TMPL = "https://api.census.gov/data/{year}/acs/acs5"

#: Default ACS 5-year vintage end-year.
_DEFAULT_YEAR = 2022

#: Friendly-name -> ACS detailed-table spec. ``kind="value"`` returns the named
#: estimate code directly; ``kind="pct"`` returns 100 * sum(num) / denom.
ACS_VARIABLES: dict[str, dict[str, Any]] = {
    "median_income": {
        "table": "B19013", "code": "B19013_001E", "kind": "value", "units": "usd",
    },
    "median_age": {
        "table": "B01002", "code": "B01002_001E", "kind": "value", "units": "years",
    },
    "median_home_value": {
        "table": "B25077", "code": "B25077_001E", "kind": "value", "units": "usd",
    },
    "poverty_rate": {
        "table": "B17001", "num": ["B17001_002E"], "denom": "B17001_001E",
        "kind": "pct", "units": "percent",
    },
    "pct_renters": {
        "table": "B25003", "num": ["B25003_003E"], "denom": "B25003_001E",
        "kind": "pct", "units": "percent",
    },
    "pct_no_vehicle": {
        "table": "B25044", "num": ["B25044_003E", "B25044_010E"],
        "denom": "B25044_001E", "kind": "pct", "units": "percent",
    },
}

#: ACS "jam" / annotation sentinels are large negatives (e.g. -666666666). Any
#: estimate <= this floor is a sentinel, never a legitimate value.
_ACS_NULL_FLOOR = -666666000.0

#: ArcGIS REST page size (TIGERweb maxRecordCount is typically 1000).
_PAGE_SIZE = 1000

#: Defensive hard cap on total tracts fetched (a too-large bbox should be caught
#: by the payload warning upstream; we never page unboundedly).
_MAX_FEATURES = 20000

#: HTTP request timeout (seconds). data.census.gov per-county pulls can be slow.
_HTTP_TIMEOUT_S = 90.0

#: User-Agent.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: Payload estimation heuristic: MB per square degree of tract polygons.
_PAYLOAD_MB_PER_SQ_DEG = 3.0
_PAYLOAD_MIN_MB = 0.02
_PAYLOAD_MAX_MB = 80.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata -- registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common = dict(
        name="fetch_census_acs",
        ttl_class="static-30d",
        source_class="census_acs",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(
            **common,
            supports_global_query=False,
            payload_mb_estimator_name="estimate_payload_mb",
        )  # type: ignore[call-arg]
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not support all flags; registering "
            "fetch_census_acs without them"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    **_kw: Any,
) -> float:
    """Estimate the FlatGeobuf payload size for an ACS tract fetch.

    Heuristic: ~3 MB per square degree of tract polygons. An urban county
    (~0.04 sq deg) returns ~0.15 MB; a 1 sq deg metro returns ~3 MB.
    """
    if bbox is None:
        area_sq_deg = 1.0
    else:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
            area_sq_deg = max(0.0, max_lon - min_lon) * max(0.0, max_lat - min_lat)
        except (TypeError, ValueError):
            area_sq_deg = 1.0
    est = area_sq_deg * _PAYLOAD_MB_PER_SQ_DEG
    return max(_PAYLOAD_MIN_MB, min(_PAYLOAD_MAX_MB, est))


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``CensusACSInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise CensusACSInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise CensusACSInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise CensusACSInputError(f"bbox lon out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise CensusACSInputError(f"bbox lat out of [-90, 90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise CensusACSInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 dp (~0.1 m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _is_raw_acs_code(s: str) -> bool:
    """True if ``s`` looks like a raw ACS estimate code, e.g. ``B19013_001E``."""
    if not s or s[0].upper() not in ("B", "C"):
        return False
    if not s.upper().endswith("E"):
        return False
    return "_" in s


def _resolve_variable(variable: str) -> dict[str, Any]:
    """Resolve a friendly name or raw ACS code to a fetch spec.

    Returns a dict with keys ``table`` (str), ``kind`` ("value"/"pct"),
    ``units`` (str), and either ``code`` (value kind) or ``num``/``denom``
    (pct kind), plus ``friendly`` (the canonical name echoed into properties).

    Raises ``CensusACSInputError`` for an unknown / malformed variable.
    """
    if not isinstance(variable, str) or not variable.strip():
        raise CensusACSInputError(
            f"variable must be a non-empty string; got {variable!r}"
        )
    key = variable.strip()
    low = key.lower()
    if low in ACS_VARIABLES:
        spec = dict(ACS_VARIABLES[low])
        spec["friendly"] = low
        return spec
    # Raw ACS estimate code passthrough (e.g. B19013_001E).
    if _is_raw_acs_code(key):
        code = key.upper()
        table = code.split("_", 1)[0]
        return {
            "table": table, "code": code, "kind": "value",
            "units": "count", "friendly": code,
        }
    raise CensusACSInputError(
        f"unknown variable {variable!r}; known friendly names: "
        f"{sorted(ACS_VARIABLES)}; or pass a raw ACS estimate code like "
        f"'B19013_001E'"
    )


# ---------------------------------------------------------------------------
# Source 1: TIGERweb tract geometry (keyless ArcGIS REST).
# ---------------------------------------------------------------------------


def _fetch_tiger_tracts(
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Fetch census-tract polygons intersecting bbox from TIGERweb (paged).

    Returns a list of GeoJSON Feature dicts with ``GEOID``/``STATE``/``COUNTY``/
    ``TRACT``/``NAME`` properties. Possibly empty (ocean / outside U.S.).

    Raises ``CensusACSUpstreamError`` on network / HTTP / parse failure.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    all_feats: list[dict[str, Any]] = []
    offset = 0
    with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
        while True:
            params = {
                "where": "1=1",
                "outFields": "GEOID,NAME,STATE,COUNTY,TRACT",
                "geometry": f"{min_lon},{min_lat},{max_lon},{max_lat}",
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outSR": "4326",
                "returnGeometry": "true",
                "resultRecordCount": str(_PAGE_SIZE),
                "resultOffset": str(offset),
                "f": "geojson",
            }
            try:
                resp = client.get(
                    TIGER_TRACT_QUERY_URL,
                    params=params,
                    headers={"User-Agent": _USER_AGENT},
                )
            except httpx.HTTPError as exc:
                raise CensusACSUpstreamError(
                    f"TIGERweb request failed bbox={bbox} offset={offset}: {exc}"
                ) from exc
            if resp.status_code >= 400:
                raise CensusACSUpstreamError(
                    f"TIGERweb returned HTTP {resp.status_code} offset={offset}: "
                    f"{resp.text[:300]!r}"
                )
            try:
                body = resp.json()
            except ValueError as exc:
                raise CensusACSUpstreamError(
                    f"TIGERweb returned non-JSON offset={offset}: {exc}"
                ) from exc
            if isinstance(body, dict) and "error" in body:
                raise CensusACSUpstreamError(
                    f"TIGERweb query error envelope offset={offset}: {body['error']}"
                )
            if not isinstance(body, dict) or body.get("type") != "FeatureCollection":
                raise CensusACSUpstreamError(
                    f"TIGERweb response is not a GeoJSON FeatureCollection "
                    f"offset={offset}: type={body.get('type') if isinstance(body, dict) else type(body).__name__!r}"
                )
            page = body.get("features", []) or []
            all_feats.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            if len(all_feats) >= _MAX_FEATURES:
                logger.warning(
                    "fetch_census_acs: hit _MAX_FEATURES cap (%d); bbox too large",
                    _MAX_FEATURES,
                )
                break
            offset += _PAGE_SIZE
    logger.info("fetch_census_acs: TIGERweb returned %d tract(s)", len(all_feats))
    return all_feats


# ---------------------------------------------------------------------------
# Source 2: ACS estimates by county (keyless data.census.gov backend; optional
# key-gated classic Data API as primary when CENSUS_API_KEY is set).
# ---------------------------------------------------------------------------


def _parse_data_census_rows(
    rows: list[list[Any]], table: str
) -> dict[str, dict[str, float | None]]:
    """Parse a data.census.gov table response into {geoid11: {code: float|None}}.

    Selects estimate columns (suffix ``E``) belonging to ``table`` and maps ACS
    sentinel jam values (<= -666666000) to ``None``.
    """
    if not rows:
        return {}
    header = rows[0]
    idx = {h: i for i, h in enumerate(header)}
    if "GEO_ID" not in idx:
        return {}
    gi = idx["GEO_ID"]
    est_cols = [
        h for h in header
        if h.endswith("E") and not h.endswith("EA")
        and h.split("_", 1)[0] == table
    ]
    out: dict[str, dict[str, float | None]] = {}
    for row in rows[1:]:
        try:
            geoid = str(row[gi]).split("US")[-1]
        except (IndexError, AttributeError):
            continue
        rec: dict[str, float | None] = {}
        for col in est_cols:
            raw = row[idx[col]]
            try:
                v = float(raw)
                rec[col] = v if v > _ACS_NULL_FLOOR else None
            except (TypeError, ValueError):
                rec[col] = None
        out[geoid] = rec
    return out


def _fetch_via_data_census(
    table: str, counties: set[tuple[str, str]], year: int
) -> dict[str, dict[str, float | None]]:
    """Keyless: pull all tracts in each county from data.census.gov backend."""
    out: dict[str, dict[str, float | None]] = {}
    with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
        for (st, co) in sorted(counties):
            g = f"0500000US{st}{co}$1400000"  # all tracts in the county
            params = {"g": g, "tid": f"ACSDT5Y{year}.{table}"}
            try:
                resp = client.get(
                    DATA_CENSUS_TABLE_URL,
                    params=params,
                    headers={"User-Agent": _USER_AGENT},
                )
            except httpx.HTTPError as exc:
                raise CensusACSUpstreamError(
                    f"data.census.gov request failed table={table} "
                    f"county={st}{co}: {exc}"
                ) from exc
            ct = resp.headers.get("content-type", "")
            if resp.status_code >= 400:
                # A 400 with "no table" is an input problem (bad code).
                if "no table" in resp.text.lower():
                    raise CensusACSInputError(
                        f"ACS table {table!r} does not exist for year {year} "
                        f"(year {year} ACS 5-year): {resp.text[:200]!r}"
                    )
                raise CensusACSUpstreamError(
                    f"data.census.gov HTTP {resp.status_code} table={table} "
                    f"county={st}{co}: {resp.text[:300]!r}"
                )
            if "json" not in ct:
                raise CensusACSUpstreamError(
                    f"data.census.gov non-JSON table={table} county={st}{co} "
                    f"ct={ct!r}: {resp.text[:200]!r}"
                )
            try:
                body = resp.json()
            except ValueError as exc:
                raise CensusACSUpstreamError(
                    f"data.census.gov bad JSON table={table} county={st}{co}: {exc}"
                ) from exc
            rows = (body or {}).get("response", {}).get("data", [])
            out.update(_parse_data_census_rows(rows, table))
    return out


def _fetch_via_census_api(
    codes: list[str], counties: set[tuple[str, str]], year: int, api_key: str
) -> dict[str, dict[str, float | None]]:
    """Key-gated: pull tract estimates from the classic api.census.gov Data API.

    Used only when ``CENSUS_API_KEY`` is set. Raises ``CensusACSUpstreamError``
    if the key is rejected (caller falls back to the keyless backend).
    """
    url = _CENSUS_DATA_API_TMPL.format(year=year)
    out: dict[str, dict[str, float | None]] = {}
    with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
        for (st, co) in sorted(counties):
            params = {
                "get": "GEO_ID," + ",".join(codes),
                "for": "tract:*",
                "in": f"state:{st} county:{co}",
                "key": api_key,
            }
            try:
                resp = client.get(url, params=params)
            except httpx.HTTPError as exc:
                raise CensusACSUpstreamError(
                    f"api.census.gov request failed county={st}{co}: {exc}"
                ) from exc
            if "missing_key" in str(resp.url) or "invalid" in resp.text.lower()[:120]:
                raise CensusACSUpstreamError(
                    "api.census.gov rejected CENSUS_API_KEY (missing/invalid)"
                )
            if resp.status_code >= 400:
                raise CensusACSUpstreamError(
                    f"api.census.gov HTTP {resp.status_code} county={st}{co}: "
                    f"{resp.text[:300]!r}"
                )
            try:
                rows = resp.json()
            except ValueError as exc:
                raise CensusACSUpstreamError(
                    f"api.census.gov bad JSON county={st}{co}: {exc}"
                ) from exc
            if not rows:
                continue
            header = rows[0]
            idx = {h: i for i, h in enumerate(header)}
            gi = idx.get("GEO_ID")
            for row in rows[1:]:
                geoid = str(row[gi]).split("US")[-1] if gi is not None else None
                if not geoid:
                    continue
                rec: dict[str, float | None] = {}
                for cd in codes:
                    raw = row[idx[cd]] if cd in idx else None
                    try:
                        v = float(raw)
                        rec[cd] = v if v > _ACS_NULL_FLOOR else None
                    except (TypeError, ValueError):
                        rec[cd] = None
                out[geoid] = rec
    return out


def _fetch_acs_values(
    spec: dict[str, Any], counties: set[tuple[str, str]], year: int
) -> dict[str, dict[str, float | None]]:
    """Fetch the ACS estimates needed for ``spec`` across the given counties.

    Primary path is the keyless ``data.census.gov`` backend. If ``CENSUS_API_KEY``
    is set, the classic ``api.census.gov`` Data API is tried first and the
    backend is the fallback (FR-DC data-source fallback norm). Either path
    returns ``{geoid11: {code: float|None}}``.
    """
    if not counties:
        return {}
    table = spec["table"]
    if spec["kind"] == "value":
        codes = [spec["code"]]
    else:
        codes = list(spec["num"]) + [spec["denom"]]

    api_key = os.environ.get("CENSUS_API_KEY", "").strip()
    if api_key:
        try:
            return _fetch_via_census_api(codes, counties, year, api_key)
        except CensusACSUpstreamError as exc:
            logger.warning(
                "fetch_census_acs: api.census.gov path failed (%s); falling "
                "back to keyless data.census.gov backend",
                exc,
            )
    return _fetch_via_data_census(table, counties, year)


# ---------------------------------------------------------------------------
# Join + value derivation.
# ---------------------------------------------------------------------------


def _compute_value(
    spec: dict[str, Any], rec: dict[str, float | None] | None
) -> float | None:
    """Derive the choropleth value for one tract from its ACS estimate record."""
    if rec is None:
        return None
    if spec["kind"] == "value":
        return rec.get(spec["code"])
    # Percentage: 100 * sum(num) / denom.
    denom = rec.get(spec["denom"])
    if denom is None or denom <= 0:
        return None
    total = 0.0
    for k in spec["num"]:
        v = rec.get(k)
        if v is None:
            return None
        total += v
    return round(100.0 * total / denom, 2)


# ---------------------------------------------------------------------------
# Features → FlatGeobuf bytes.
# ---------------------------------------------------------------------------


def _features_to_flatgeobuf(
    tract_features: list[dict[str, Any]],
    values: dict[str, dict[str, float | None]],
    spec: dict[str, Any],
) -> bytes:
    """Join ACS values onto tract geometry by GEOID; serialize to FlatGeobuf.

    Always emits valid FlatGeobuf bytes -- an empty tract list yields an empty-
    schema FGB so the cache shim has something concrete to persist.

    Raises ``CensusACSUpstreamError`` if geopandas is unavailable or write fails.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CensusACSUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    friendly = spec["friendly"]
    units = spec["units"]
    cleaned: list[dict[str, Any]] = []
    for feat in tract_features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if geom is None:
            continue
        p = feat.get("properties") or {}
        geoid = p.get("GEOID")
        rec = values.get(geoid) if geoid is not None else None
        cleaned.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "geoid": geoid,
                "name": p.get("NAME"),
                "state": p.get("STATE"),
                "county": p.get("COUNTY"),
                "variable": friendly,
                "value": _compute_value(spec, rec),
                "units": units,
            },
        })

    _COLS = ["geoid", "name", "state", "county", "variable", "value", "units"]
    if not cleaned:
        import pandas as pd
        empty_df = pd.DataFrame(columns=_COLS)
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs="EPSG:4326")
    else:
        gdf = gpd.GeoDataFrame.from_features(cleaned, crs="EPSG:4326")
        gdf = gdf.dropna(subset=["geometry"]).copy()

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_census_acs_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise CensusACSUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} ACS tract(s): {exc}"
            ) from exc
        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()
        logger.info(
            "fetch_census_acs: FlatGeobuf = %d bytes (%d tract(s), variable=%s)",
            len(fgb_bytes), len(gdf), friendly,
        )
        return fgb_bytes
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# End-to-end fetcher (passed to read_through).
# ---------------------------------------------------------------------------


def _fetch_acs_bytes(
    bbox: tuple[float, float, float, float], spec: dict[str, Any], year: int
) -> bytes:
    """Fetch tract geometry + ACS values, join, serialize to FlatGeobuf bytes."""
    tract_features = _fetch_tiger_tracts(bbox)
    counties: set[tuple[str, str]] = set()
    for feat in tract_features:
        p = feat.get("properties") or {}
        st, co = p.get("STATE"), p.get("COUNTY")
        if st and co:
            counties.add((str(st), str(co)))
    values = _fetch_acs_values(spec, counties, year) if counties else {}
    return _features_to_flatgeobuf(tract_features, values, spec)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
    # readOnlyHint=True, openWorldHint=True (external public APIs),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_census_acs(
    bbox: tuple[float, float, float, float],
    variable: str = "median_income",
    year: int = _DEFAULT_YEAR,
    # Wave 4.10 convention: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """US Census ACS 5-year demographics as a census-tract choropleth FlatGeobuf.

    Joins authoritative U.S. census-tract geometry (Census TIGERweb, keyless)
    to ACS 5-year estimates (Census ``data.census.gov`` backend, keyless) by
    11-digit GEOID, returning a FlatGeobuf choropleth clipped to ``bbox``. The
    ``variable`` param maps friendly names (``median_income``, ``median_age``,
    ``median_home_value``, ``poverty_rate``, ``pct_renters``, ``pct_no_vehicle``)
    to ACS detailed-table codes; a raw ACS estimate code (``B19013_001E``) is
    also accepted. Generalizes the population-only fetchers to arbitrary
    demographics for vulnerability / environmental-justice analysis.

    **When to use:**
    - User asks for a specific demographic (income, age, home value, poverty,
      renters, car-free households) for an area, or "median income by tract".
    - Agent needs a demographic surface to intersect with a hazard footprint
      for exposure / equity analysis.

    **When NOT to use:**
    - For composited social-vulnerability *percentile ranks* -> ``fetch_cdc_svi``.
    - For a gridded population *raster* -> ``fetch_hrsl_population``.
    - For areas outside the U.S. -> ACS is U.S.-only; an empty FGB is returned.

    **Parameters:**
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required;
            ``supports_global_query=False``. County-or-smaller extent. Example
            for Houston / Harris County TX: ``(-95.45, 29.65, -95.25, 29.85)``.
        variable: friendly name (see registry) or raw ACS estimate code.
            Default ``"median_income"``. Unknown -> ``CensusACSInputError``.
        year: ACS 5-year vintage end-year. Default ``2022``.

    **Returns:**
        ``LayerURI`` -> FlatGeobuf. Each feature is a Polygon (census tract) in
        EPSG:4326. Properties: ``geoid`` (str, 11-digit), ``name`` (str),
        ``state`` (str FIPS), ``county`` (str FIPS), ``variable`` (str),
        ``value`` (float|null), ``units`` (str). ``layer_type="vector"``,
        ``role="primary"``, ``style_preset="acs_choropleth"``.

    **Error types (FR-AS-11):**
        - ``CensusACSInputError``: bad bbox / unknown variable / nonexistent
          ACS table (retryable=False).
        - ``CensusACSUpstreamError``: TIGERweb or data.census.gov network / HTTP
          / parse failure (retryable=True).
        - ``CensusACSEmptyError``: no tracts in bbox (not raised by default --
          an empty FGB is returned instead).

    Cache: ``static-30d``, ``source_class="census_acs"``. Cache key is bbox
    (6 dp) + variable + year. No API key required (optional ``CENSUS_API_KEY``
    env var, if set, uses the classic Data API as primary with the keyless
    backend as fallback).
    """
    # ---- Input validation ----
    if not isinstance(bbox, tuple):
        try:
            bbox = tuple(bbox)  # type: ignore[arg-type]
        except TypeError as exc:
            raise CensusACSInputError(
                f"bbox must be a 4-tuple; got {type(bbox).__name__}"
            ) from exc
    _validate_bbox(bbox)  # type: ignore[arg-type]

    spec = _resolve_variable(variable)

    try:
        year_int = int(year)
    except (TypeError, ValueError) as exc:
        raise CensusACSInputError(f"year must be an int; got {year!r}") from exc
    if not (2009 <= year_int <= 2030):
        raise CensusACSInputError(
            f"year out of supported ACS 5-year range [2009, 2030]: {year_int}"
        )

    q_bbox = _round_bbox_to_6dp(bbox)  # type: ignore[arg-type]

    # ---- Cache-key params ----
    params: dict[str, Any] = {
        "bbox": list(q_bbox),
        "variable": spec["friendly"],
        "year": year_int,
        "geography": "tract",
    }

    # ---- Read-through cache ----
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_acs_bytes(q_bbox, spec, year_int),
    )
    assert result.uri is not None, (
        "fetch_census_acs is cacheable; uri must be set by read_through"
    )

    # ---- Build LayerURI ----
    return LayerURI(
        layer_id=(
            f"census-acs-{spec['friendly']}-{year_int}-tract-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=(
            f"Census ACS {year_int} {spec['friendly']} (tract) -- bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="vector",
        uri=result.uri,
        style_preset="acs_choropleth",
        role="primary",
        units=spec["units"],
        bbox=q_bbox,
    )
