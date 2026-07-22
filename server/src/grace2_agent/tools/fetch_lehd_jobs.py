"""``fetch_lehd_jobs`` atomic tool -- Census LEHD LODES workplace-area
employment, aggregated to census tract, as a choropleth FlatGeobuf.

Returns **where the jobs are** -- the count of jobs located at each census
tract's workplaces, derived from the Census Bureau's LEHD LODES (Longitudinal
Employer-Household Dynamics, Origin-Destination Employment Statistics)
*Workplace Area Characteristics* (WAC) files. WAC tabulates jobs by the
employee's *work* census block. This tool aggregates those block-level counts
up to the enclosing census tract and joins them to authoritative tract geometry,
yielding a job-density choropleth clipped to a bbox.

This is the canonical **economic-exposure / recovery** surface: pair it with a
hazard footprint to ask "how many jobs sit inside the inundation / fire / plume
extent" or "rank exposed tracts by employment at risk". Where
``fetch_census_acs`` answers *who lives here* (residents), LODES WAC answers
*where people work* -- the distinction matters for daytime exposure and for
post-disaster economic-recovery planning.

**Two keyless authoritative sources, joined by 11-digit tract GEOID (FR-DC):**

1. **Tract geometry** -- the Census Bureau's TIGERweb Generalized ACS tract
   polygon layer (ArcGIS REST, keyless; the same layer ``fetch_census_acs``
   uses)::

       https://tigerweb.geo.census.gov/arcgis/rest/services/
           Generalized_ACS2023/Tracts_Blocks/MapServer/4/query

   Layer 4 ("Census Tracts 500K") carries the 11-digit ``GEOID``
   (state+county+tract) plus ``STATE``/``COUNTY``/``TRACT``/``NAME`` and the
   polygon geometry. Queried by ``esriGeometryEnvelope`` intersect, paged. The
   ``STATE`` FIPS values present in the bbox drive which LODES state files to
   pull (a bbox may straddle several states).

2. **LODES WAC flat files** -- the keyless LODES8 workplace-area CSVs, one per
   state::

       https://lehd.ces.census.gov/data/lodes/LODES8/<st>/wac/
           <st>_wac_S000_JT00_<year>.csv.gz

   ``S000`` = all workforce segments, ``JT00`` = all job types. The first column
   ``w_geocode`` is the 15-digit work census *block* code; its first 11 digits
   are the tract GEOID. Block rows are summed to tract. ``S000``/``JT00`` is the
   total-jobs file; the requested ``segment`` selects which column to keep.

**Segment registry** (the ``segment`` param selects one WAC column; all come
from the single ``S000_JT00`` file, so any segment is one download per state):

    - ``total``      -> C000  (total jobs at the workplace)
    - ``low_wage``   -> CE01  (jobs paying <= $1250/month)
    - ``mid_wage``   -> CE02  (jobs paying $1251-$3333/month)
    - ``high_wage``  -> CE03  (jobs paying > $3333/month)
    - ``goods``      -> CNS01+CNS02+CNS03+CNS04+CNS05 (agri/mining/util/constr/mfg)
    - ``trade_transport`` -> CNS06+CNS07+CNS08+CNS09 (wholesale/retail/transp/info)
    - ``services``   -> CNS10..CNS18 (finance/prof/edu/health/leisure/...)
    - ``public``     -> CNS20 (public administration)
    - ``retail``     -> CNS07 (retail trade -- a common single-sector ask)
    - ``manufacturing`` -> CNS05
    - ``health``     -> CNS15 (health care and social assistance)

**When to use:**
- "How many jobs are inside this flood / fire / plume footprint?" (economic
  exposure; feed the choropleth INTO ``compute_zonal_statistics`` over a hazard
  polygon).
- "Show where the jobs are" / "employment density by tract" / "daytime
  population proxy" for a metro.
- Recovery planning: which tracts concentrate employment at risk, by wage tier
  or sector (low-wage jobs are a key social-vulnerability dimension).

**When NOT to use:**
- For *residents* / who lives there -> ``fetch_census_acs`` (ACS population /
  demographics) or ``fetch_hrsl_population`` (gridded residential population).
- For a gridded population *raster* -> ``fetch_hrsl_population`` /
  ``fetch_worldpop`` (this tool returns tract polygons).
- For areas outside the United States -> LODES + tract geography is U.S.-only
  (states + DC + PR). An empty FGB is returned for non-U.S. bboxes.

**Parameters:**
    bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required --
        ``supports_global_query=False`` (a nationwide pull is millions of
        blocks). County-or-metro extent recommended. Example for downtown
        Houston / Harris County TX: ``(-95.45, 29.65, -95.25, 29.85)``.
    segment: a friendly segment name (see registry). Default ``"total"``.
        Unknown -> typed input error (never a silent wrong column).
    year: LODES8 data year (default ``2022``; LODES8 currently spans ~2002-2022,
        availability varies by state). A year with no file for a state degrades
        to an honest typed error.

**Returns:**
    ``LayerURI`` -> FlatGeobuf in the cache bucket. Each feature is a Polygon
    (census tract) in EPSG:4326. Properties: ``geoid`` (str, 11-digit),
    ``name`` (str), ``state`` (str FIPS), ``county`` (str FIPS), ``segment``
    (str friendly name), ``value`` (float|null -- aggregated job count; null
    where a tract has no LODES workplace record, e.g. unpopulated land),
    ``units`` (``"jobs"``), ``year`` (int). ``layer_type="vector"``,
    ``role="primary"``, ``style_preset="lehd_jobs_choropleth"``.

**Cross-tool dependencies (FR-TA-3):**
    - Feeds INTO ``compute_zonal_statistics`` / ``clip_vector_to_polygon`` for
      hazard-exposure intersections ("jobs inside the inundation polygon").
    - Combine WITH ``run_model_flood_scenario`` / ``fetch_noaa_slr_scenarios`` /
      ``fetch_firms_active_fire`` / MODFLOW plume footprints for employment at
      risk; ``fetch_administrative_boundaries`` for county scope.
    - Complements ``fetch_census_acs`` (residents) and ``fetch_cdc_svi``
      (social-vulnerability ranks) -- WAC is the workplace / daytime side.

**Cache:** ``static-30d`` (FR-DC-2). LODES vintages are annual; a 30-day stale
window is appropriate. Cache key factors bbox (6 dp) + segment + year.

**FR-AS-11 typed-error surface:** ``LehdJobsError`` (base, retryable=True),
``LehdJobsInputError`` (bad bbox / unknown segment / bad year, retryable=False),
``LehdJobsUpstreamError`` (TIGERweb or LODES network/HTTP/parse failure,
retryable=True), ``LehdJobsEmptyError`` (no tracts in bbox -- NOT raised by
default; an empty FGB is serialized so the layer appears with a zero-feature
notice -- e.g. open ocean or outside the U.S.).

``supports_global_query=False``. No API key required.

Endpoints verified live 2026-06-27.
"""

from __future__ import annotations

import collections
import csv
import gzip
import io
import logging
import math
import os
import tempfile
from typing import Any

import httpx

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_lehd_jobs",
    "estimate_payload_mb",
    "LehdJobsError",
    "LehdJobsInputError",
    "LehdJobsUpstreamError",
    "LehdJobsEmptyError",
    "LODES_SEGMENTS",
    "FIPS_TO_ABBR",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_resolve_segment",
    "_fetch_tiger_tracts",
    "_aggregate_wac_to_tract",
    "_parse_wac_csv",
    "_features_to_flatgeobuf",
    "_fetch_lehd_bytes",
    "TIGER_TRACT_QUERY_URL",
    "LODES_WAC_URL_TMPL",
]

logger = logging.getLogger("grace2_agent.tools.fetch_lehd_jobs")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class LehdJobsError(RuntimeError):
    """Base class for fetch_lehd_jobs failures."""

    error_code: str = "LEHD_JOBS_ERROR"
    retryable: bool = True


class LehdJobsInputError(LehdJobsError):
    """Caller passed an invalid bbox, unknown segment, or bad year."""

    error_code = "LEHD_JOBS_INPUT_INVALID"
    retryable = False


class LehdJobsUpstreamError(LehdJobsError):
    """TIGERweb or LODES request failed (network, HTTP, or parse)."""

    error_code = "LEHD_JOBS_UPSTREAM_ERROR"
    retryable = True


class LehdJobsEmptyError(LehdJobsError):
    """No tracts found in bbox.

    NOT raised by default (an empty FGB is serialized instead -- a bbox over
    open ocean or outside the U.S. legitimately has no tracts), but available
    for a future strict-mode opt-in.
    """

    error_code = "LEHD_JOBS_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants + registries.
# ---------------------------------------------------------------------------

#: TIGERweb Generalized-ACS tract polygon query endpoint (keyless ArcGIS REST).
#: Layer 4 = "Census Tracts 500K" -- carries the 11-digit GEOID + geometry.
TIGER_TRACT_QUERY_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "Generalized_ACS2023/Tracts_Blocks/MapServer/4/query"
)

#: LODES8 WAC flat-file URL template (keyless). ``{abbr}`` = 2-letter lowercase
#: state abbreviation, ``{year}`` = data year. ``S000`` = all segments,
#: ``JT00`` = all job types (the total-jobs file that carries every column).
LODES_WAC_URL_TMPL = (
    "https://lehd.ces.census.gov/data/lodes/LODES8/{abbr}/wac/"
    "{abbr}_wac_S000_JT00_{year}.csv.gz"
)

#: Default LODES8 data year.
_DEFAULT_YEAR = 2022

#: Census state FIPS code -> 2-letter lowercase abbreviation (LODES path uses the
#: abbreviation, while TIGERweb tracts carry the FIPS ``STATE`` field). Covers the
#: 50 states + DC (11) + Puerto Rico (72), the LODES universe.
FIPS_TO_ABBR: dict[str, str] = {
    "01": "al", "02": "ak", "04": "az", "05": "ar", "06": "ca", "08": "co",
    "09": "ct", "10": "de", "11": "dc", "12": "fl", "13": "ga", "15": "hi",
    "16": "id", "17": "il", "18": "in", "19": "ia", "20": "ks", "21": "ky",
    "22": "la", "23": "me", "24": "md", "25": "ma", "26": "mi", "27": "mn",
    "28": "ms", "29": "mo", "30": "mt", "31": "ne", "32": "nv", "33": "nh",
    "34": "nj", "35": "nm", "36": "ny", "37": "nc", "38": "nd", "39": "oh",
    "40": "ok", "41": "or", "42": "pa", "44": "ri", "45": "sc", "46": "sd",
    "47": "tn", "48": "tx", "49": "ut", "50": "vt", "51": "va", "53": "wa",
    "54": "wv", "55": "wi", "56": "wy", "72": "pr",
}

#: Friendly segment name -> {"cols": [WAC columns to sum], "label": str}. Every
#: segment comes from the single ``S000_JT00`` WAC file, so any segment costs one
#: download per state. ``cols`` are summed to produce the choropleth value.
LODES_SEGMENTS: dict[str, dict[str, Any]] = {
    "total": {"cols": ["C000"], "label": "total jobs"},
    "low_wage": {"cols": ["CE01"], "label": "jobs <= $1250/month"},
    "mid_wage": {"cols": ["CE02"], "label": "jobs $1251-$3333/month"},
    "high_wage": {"cols": ["CE03"], "label": "jobs > $3333/month"},
    "goods": {
        "cols": ["CNS01", "CNS02", "CNS03", "CNS04", "CNS05"],
        "label": "goods-producing jobs (agri/mining/util/constr/mfg)",
    },
    "trade_transport": {
        "cols": ["CNS06", "CNS07", "CNS08", "CNS09"],
        "label": "trade/transport/info jobs",
    },
    "services": {
        "cols": [
            "CNS10", "CNS11", "CNS12", "CNS13", "CNS14",
            "CNS15", "CNS16", "CNS17", "CNS18",
        ],
        "label": "service-sector jobs",
    },
    "public": {"cols": ["CNS20"], "label": "public-administration jobs"},
    "retail": {"cols": ["CNS07"], "label": "retail-trade jobs"},
    "manufacturing": {"cols": ["CNS05"], "label": "manufacturing jobs"},
    "health": {"cols": ["CNS15"], "label": "health-care/social-assistance jobs"},
}

#: ArcGIS REST page size (TIGERweb maxRecordCount is typically 1000).
_PAGE_SIZE = 1000

#: Defensive hard cap on total tracts fetched (a too-large bbox should be caught
#: by the payload warning upstream; we never page unboundedly).
_MAX_FEATURES = 20000

#: HTTP request timeout (seconds). LODES state files can be tens of MB gzipped.
_HTTP_TIMEOUT_S = 180.0

#: Earliest / latest plausible LODES8 data year (availability varies by state).
_YEAR_MIN = 2002
_YEAR_MAX = 2030

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
        name="fetch_lehd_jobs",
        ttl_class="static-30d",
        source_class="lehd_lodes",
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
            "fetch_lehd_jobs without them"
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
    """Estimate the FlatGeobuf payload size for a LODES tract fetch.

    Heuristic: ~3 MB per square degree of tract polygons (the output is the
    tract geometry choropleth; the multi-MB LODES CSV is summarized away). An
    urban county (~0.04 sq deg) returns ~0.15 MB; a 1 sq deg metro ~3 MB.
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
    """Raise ``LehdJobsInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise LehdJobsInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise LehdJobsInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise LehdJobsInputError(f"bbox lon out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise LehdJobsInputError(f"bbox lat out of [-90, 90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise LehdJobsInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 dp (~0.1 m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _resolve_segment(segment: str) -> dict[str, Any]:
    """Resolve a friendly segment name to a fetch spec.

    Returns a dict with keys ``cols`` (list of WAC columns to sum), ``label``
    (human description), and ``friendly`` (canonical name echoed into
    properties). Raises ``LehdJobsInputError`` for an unknown segment.
    """
    if not isinstance(segment, str) or not segment.strip():
        raise LehdJobsInputError(
            f"segment must be a non-empty string; got {segment!r}"
        )
    key = segment.strip().lower()
    if key not in LODES_SEGMENTS:
        raise LehdJobsInputError(
            f"unknown segment {segment!r}; known segments: "
            f"{sorted(LODES_SEGMENTS)}"
        )
    spec = dict(LODES_SEGMENTS[key])
    spec["friendly"] = key
    return spec


# ---------------------------------------------------------------------------
# Source 1: TIGERweb tract geometry (keyless ArcGIS REST).
# ---------------------------------------------------------------------------


def _fetch_tiger_tracts(
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Fetch census-tract polygons intersecting bbox from TIGERweb (paged).

    Returns a list of GeoJSON Feature dicts with ``GEOID``/``STATE``/``COUNTY``/
    ``TRACT``/``NAME`` properties. Possibly empty (ocean / outside U.S.).

    Raises ``LehdJobsUpstreamError`` on network / HTTP / parse failure.
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
                raise LehdJobsUpstreamError(
                    f"TIGERweb request failed bbox={bbox} offset={offset}: {exc}"
                ) from exc
            if resp.status_code >= 400:
                raise LehdJobsUpstreamError(
                    f"TIGERweb returned HTTP {resp.status_code} offset={offset}: "
                    f"{resp.text[:300]!r}"
                )
            try:
                body = resp.json()
            except ValueError as exc:
                raise LehdJobsUpstreamError(
                    f"TIGERweb returned non-JSON offset={offset}: {exc}"
                ) from exc
            if isinstance(body, dict) and "error" in body:
                raise LehdJobsUpstreamError(
                    f"TIGERweb query error envelope offset={offset}: {body['error']}"
                )
            if not isinstance(body, dict) or body.get("type") != "FeatureCollection":
                raise LehdJobsUpstreamError(
                    f"TIGERweb response is not a GeoJSON FeatureCollection "
                    f"offset={offset}: "
                    f"type={body.get('type') if isinstance(body, dict) else type(body).__name__!r}"
                )
            page = body.get("features", []) or []
            all_feats.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            if len(all_feats) >= _MAX_FEATURES:
                logger.warning(
                    "fetch_lehd_jobs: hit _MAX_FEATURES cap (%d); bbox too large",
                    _MAX_FEATURES,
                )
                break
            offset += _PAGE_SIZE
    logger.info("fetch_lehd_jobs: TIGERweb returned %d tract(s)", len(all_feats))
    return all_feats


# ---------------------------------------------------------------------------
# Source 2: LODES WAC flat files -> block sums aggregated to tract.
# ---------------------------------------------------------------------------


def _parse_wac_csv(
    text: str, fips: str, cols: list[str]
) -> dict[str, float]:
    """Parse one state's WAC CSV text, aggregating block rows to tract sums.

    Returns ``{tract11: summed_value}`` where the value is the sum of ``cols``
    across all work blocks in the tract. Only rows whose ``w_geocode`` begins
    with ``fips`` are kept (a state file should be uniform, but guard anyway).

    Raises ``LehdJobsUpstreamError`` if the CSV header lacks the expected
    columns (a schema drift -- honest error, not a silent zero).
    """
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    if "w_geocode" not in header:
        raise LehdJobsUpstreamError(
            f"LODES WAC CSV for state {fips} missing 'w_geocode' column; "
            f"header={header[:8]!r}"
        )
    missing = [c for c in cols if c not in header]
    if missing:
        raise LehdJobsUpstreamError(
            f"LODES WAC CSV for state {fips} missing column(s) {missing}; "
            f"header has {len(header)} columns"
        )
    out: dict[str, float] = collections.defaultdict(float)
    for row in reader:
        geo = row.get("w_geocode") or ""
        if len(geo) < 11 or not geo.startswith(fips):
            continue
        tract = geo[:11]
        total = 0.0
        for c in cols:
            raw = row.get(c)
            if raw:
                try:
                    total += float(raw)
                except (TypeError, ValueError):
                    continue
        out[tract] += total
    return dict(out)


def _aggregate_wac_to_tract(
    states: set[str], cols: list[str], year: int
) -> dict[str, float]:
    """Download each state's LODES WAC file and aggregate block jobs to tract.

    Returns ``{tract11: summed_value}`` over all requested states. A state with
    no file for ``year`` raises ``LehdJobsUpstreamError`` (honest typed error;
    the caller surfaces it rather than silently dropping a state).
    """
    out: dict[str, float] = {}
    if not states:
        return out
    with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
        for fips in sorted(states):
            abbr = FIPS_TO_ABBR.get(fips)
            if abbr is None:
                # A FIPS with no LODES universe (e.g. a territory other than PR).
                logger.info(
                    "fetch_lehd_jobs: state FIPS %s has no LODES coverage; skipping",
                    fips,
                )
                continue
            url = LODES_WAC_URL_TMPL.format(abbr=abbr, year=year)
            try:
                resp = client.get(url, headers={"User-Agent": _USER_AGENT})
            except httpx.HTTPError as exc:
                raise LehdJobsUpstreamError(
                    f"LODES WAC request failed state={abbr} year={year}: {exc}"
                ) from exc
            if resp.status_code == 404:
                raise LehdJobsUpstreamError(
                    f"LODES WAC has no file for state={abbr} year={year} "
                    f"(HTTP 404 at {url}); try a different year (LODES8 spans "
                    f"~2002-2022, availability varies by state)"
                )
            if resp.status_code >= 400:
                raise LehdJobsUpstreamError(
                    f"LODES WAC HTTP {resp.status_code} state={abbr} year={year}: "
                    f"{resp.text[:200]!r}"
                )
            try:
                text = gzip.decompress(resp.content).decode("utf-8")
            except (OSError, EOFError, UnicodeDecodeError) as exc:
                raise LehdJobsUpstreamError(
                    f"LODES WAC gunzip/decode failed state={abbr} year={year}: {exc}"
                ) from exc
            state_tracts = _parse_wac_csv(text, fips, cols)
            for tract, val in state_tracts.items():
                out[tract] = out.get(tract, 0.0) + val
            logger.info(
                "fetch_lehd_jobs: LODES WAC state=%s year=%d -> %d tract(s)",
                abbr, year, len(state_tracts),
            )
    return out


# ---------------------------------------------------------------------------
# Features -> FlatGeobuf bytes.
# ---------------------------------------------------------------------------


def _features_to_flatgeobuf(
    tract_features: list[dict[str, Any]],
    values: dict[str, float],
    spec: dict[str, Any],
    year: int,
) -> bytes:
    """Join LODES tract sums onto tract geometry by GEOID; serialize FlatGeobuf.

    Always emits valid FlatGeobuf bytes -- an empty tract list yields an empty-
    schema FGB so the cache shim has something concrete to persist.

    Raises ``LehdJobsUpstreamError`` if geopandas is unavailable or write fails.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise LehdJobsUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    friendly = spec["friendly"]
    cleaned: list[dict[str, Any]] = []
    for feat in tract_features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if geom is None:
            continue
        p = feat.get("properties") or {}
        geoid = p.get("GEOID")
        val = values.get(geoid) if geoid is not None else None
        cleaned.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "geoid": geoid,
                "name": p.get("NAME"),
                "state": p.get("STATE"),
                "county": p.get("COUNTY"),
                "segment": friendly,
                "value": float(val) if val is not None else None,
                "units": "jobs",
                "year": int(year),
            },
        })

    _COLS = ["geoid", "name", "state", "county", "segment", "value", "units", "year"]
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
            suffix=".fgb", delete=False, prefix="grace2_lehd_jobs_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise LehdJobsUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} LODES tract(s): {exc}"
            ) from exc
        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()
        logger.info(
            "fetch_lehd_jobs: FlatGeobuf = %d bytes (%d tract(s), segment=%s)",
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


def _fetch_lehd_bytes(
    bbox: tuple[float, float, float, float], spec: dict[str, Any], year: int
) -> bytes:
    """Fetch tract geometry + LODES WAC sums, join, serialize to FlatGeobuf."""
    tract_features = _fetch_tiger_tracts(bbox)
    states: set[str] = set()
    for feat in tract_features:
        p = feat.get("properties") or {}
        st = p.get("STATE")
        if st:
            states.add(str(st))
    values = _aggregate_wac_to_tract(states, spec["cols"], year) if states else {}
    return _features_to_flatgeobuf(tract_features, values, spec, year)


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
def fetch_lehd_jobs(
    bbox: tuple[float, float, float, float],
    segment: str = "total",
    year: int = _DEFAULT_YEAR,
    # Wave 4.10 convention: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Census LEHD LODES workplace employment as a tract choropleth FlatGeobuf.

    Use this (not fetch_census_acs, and go past geocode_location) when you want LEHD LODES workplace JOBS/employment counts.

    Aggregates the Census Bureau's LODES Workplace Area Characteristics (WAC)
    block-level job counts up to census tract and joins them to authoritative
    tract geometry (TIGERweb, keyless), returning a job-density choropleth
    clipped to ``bbox``. WAC counts jobs at the *workplace* -- "where the jobs
    are" -- the canonical economic-exposure / recovery surface to intersect with
    a hazard footprint ("how many jobs sit inside the inundation extent"). The
    ``segment`` param selects total jobs, a wage tier, or a sector grouping.

    **When to use:**
    - "How many jobs are inside this flood / fire / plume footprint?" (feed the
      choropleth INTO ``compute_zonal_statistics`` over a hazard polygon).
    - "Show employment density by tract" / daytime-population proxy for a metro.
    - Recovery planning: tracts concentrating jobs at risk, by wage or sector.

    **When NOT to use:**
    - For *residents* / who lives there -> ``fetch_census_acs`` or
      ``fetch_hrsl_population``.
    - For a gridded population *raster* -> ``fetch_hrsl_population``.
    - For areas outside the U.S. -> LODES is U.S.-only; an empty FGB is returned.

    **Parameters:**
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required;
            ``supports_global_query=False``. County-or-metro extent. Example
            for downtown Houston TX: ``(-95.45, 29.65, -95.25, 29.85)``.
        segment: friendly segment name -- ``total``, ``low_wage``, ``mid_wage``,
            ``high_wage``, ``goods``, ``trade_transport``, ``services``,
            ``public``, ``retail``, ``manufacturing``, ``health``. Default
            ``"total"``. Unknown -> ``LehdJobsInputError``.
        year: LODES8 data year. Default ``2022``. A year with no file for a
            state in the bbox -> ``LehdJobsUpstreamError``.

    **Returns:**
        ``LayerURI`` -> FlatGeobuf. Each feature is a Polygon (census tract) in
        EPSG:4326. Properties: ``geoid`` (str, 11-digit), ``name`` (str),
        ``state`` (str FIPS), ``county`` (str FIPS), ``segment`` (str),
        ``value`` (float|null -- aggregated job count), ``units`` (``"jobs"``),
        ``year`` (int). ``layer_type="vector"``, ``role="primary"``,
        ``style_preset="lehd_jobs_choropleth"``.

    **Error types (FR-AS-11):**
        - ``LehdJobsInputError``: bad bbox / unknown segment / bad year
          (retryable=False).
        - ``LehdJobsUpstreamError``: TIGERweb or LODES network / HTTP / parse
          failure, or a year missing for a state (retryable=True).
        - ``LehdJobsEmptyError``: no tracts in bbox (not raised by default -- an
          empty FGB is returned instead).

    Cache: ``static-30d``, ``source_class="lehd_lodes"``. Cache key is bbox
    (6 dp) + segment + year. No API key required.
    """
    # ---- Input validation ----
    if not isinstance(bbox, tuple):
        try:
            bbox = tuple(bbox)  # type: ignore[arg-type]
        except TypeError as exc:
            raise LehdJobsInputError(
                f"bbox must be a 4-tuple; got {type(bbox).__name__}"
            ) from exc
    _validate_bbox(bbox)  # type: ignore[arg-type]

    spec = _resolve_segment(segment)

    try:
        year_int = int(year)
    except (TypeError, ValueError) as exc:
        raise LehdJobsInputError(f"year must be an int; got {year!r}") from exc
    if not (_YEAR_MIN <= year_int <= _YEAR_MAX):
        raise LehdJobsInputError(
            f"year out of supported LODES8 range "
            f"[{_YEAR_MIN}, {_YEAR_MAX}]: {year_int}"
        )

    q_bbox = _round_bbox_to_6dp(bbox)  # type: ignore[arg-type]

    # ---- Cache-key params ----
    params: dict[str, Any] = {
        "bbox": list(q_bbox),
        "segment": spec["friendly"],
        "year": year_int,
        "geography": "tract",
    }

    # ---- Read-through cache ----
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_lehd_bytes(q_bbox, spec, year_int),
    )
    assert result.uri is not None, (
        "fetch_lehd_jobs is cacheable; uri must be set by read_through"
    )

    # ---- Build LayerURI ----
    return LayerURI(
        layer_id=(
            f"lehd-jobs-{spec['friendly']}-{year_int}-tract-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=(
            f"LEHD LODES {year_int} {spec['label']} (tract) -- bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="vector",
        uri=result.uri,
        style_preset="lehd_jobs_choropleth",
        role="primary",
        units="jobs",
        bbox=q_bbox,
    )
