"""LEHD LODES join VALUES-hook (trigger wave): the gzip-CSV values leg.

The census-tract choropleth JOIN (TIGERweb geometry LEFT-JOIN a per-tract value on
11-digit GEOID) is the ``transforms/join`` shape, but its built-in values leg speaks
the census Data-API (``get=`` codes, ``for=``/``in=`` scope). LODES workplace jobs
come from a per-STATE bulk gzip-CSV whole-object download aggregated block -> tract.
This module is the ``join.values.values_hook`` seam's two PURE functions -- the
storm_events gzip-CSV precedent -- while the join transform owns the I/O (it GETs the
plans over the shared transport) and the cache:

- ``values_plan`` (pure): scope (state FIPS) set -> the ordered ``(fips, RequestPlan)``
  LODES WAC GET set (one whole-object gzip per state).
- ``values_parse`` (pure): the fetched gzip bytes per state -> ``{tract11: {"value":
  summed_jobs}}``, block rows summed over the segment's WAC columns (``var_spec.cols``).

Both are pure: no socket, no cache, no stamps -- the tier-3 hook doctrine. The FIPS ->
2-letter-abbreviation table and the WAC URL template are the fixed census facts the
plan needs; the segment -> WAC-column map lives in the spec (``join.values.variables``).
"""

from __future__ import annotations

import collections
import csv
import gzip
import io
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_upstream_error
from ..hooks import RequestPlan, register_hook

#: LODES8 WAC flat-file URL template (keyless). ``S000`` = all segments, ``JT00`` =
#: all job types (the total-jobs file carrying every WAC column).
_LODES_WAC_URL_TMPL = (
    "https://lehd.ces.census.gov/data/lodes/LODES8/{abbr}/wac/"
    "{abbr}_wac_S000_JT00_{year}.csv.gz"
)

#: Census state FIPS -> 2-letter lowercase abbreviation (LODES path uses the
#: abbreviation; TIGERweb tracts carry the FIPS ``STATE`` field). 50 states + DC (11)
#: + Puerto Rico (72), the LODES universe. A FIPS absent here has no LODES coverage.
_FIPS_TO_ABBR: dict[str, str] = {
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


@register_hook("lehd_jobs.values_plan")
def values_plan(
    spec: SourceSpec, scope_keys: list[tuple[str, ...]], var_spec: dict[str, Any],
    params: dict[str, Any],
) -> list[tuple[str, RequestPlan]]:
    """Ordered ``(fips, RequestPlan)`` LODES WAC GETs for the in-scope states (pure).

    Each scope key is a ``(state_fips,)`` tuple (``join.values.scope_by: [STATE]``).
    A FIPS with no LODES coverage is skipped (no plan emitted). The requested ``year``
    templates the WAC URL; a state with no file for that year 404s at fetch (the join
    transport surfaces it as a typed upstream error).
    """
    year = int(params.get("year", 2022))
    ua = spec.auth.user_agent
    plans: list[tuple[str, RequestPlan]] = []
    for scope in scope_keys:
        if not scope:
            continue
        fips = str(scope[0])
        abbr = _FIPS_TO_ABBR.get(fips)
        if abbr is None:
            continue
        url = _LODES_WAC_URL_TMPL.format(abbr=abbr, year=year)
        plans.append((fips, RequestPlan(url=url, headers={"User-Agent": ua})))
    return plans


@register_hook("lehd_jobs.values_parse")
def values_parse(
    spec: SourceSpec, results_by_scope: dict[str, bytes], var_spec: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, dict[str, float | None]]:
    """Decode the per-state WAC gzip-CSV bodies to ``{tract11: {"value": sum}}`` (pure).

    For each state body: gunzip, CSV-decode, keep rows whose ``w_geocode`` begins with
    the state's FIPS, and sum ``var_spec["cols"]`` (the segment's WAC columns) across
    all work blocks in each 11-digit tract. Missing ``w_geocode`` / column -> a typed
    upstream error (schema drift, never a silent zero). The single record code
    ``"value"`` matches the spec's ``variables.<segment>.code`` so ``compute_value``
    reads the summed jobs straight through.
    """
    cols = [str(c) for c in (var_spec.get("cols") or [])]
    out: dict[str, dict[str, float | None]] = {}
    for fips, body in results_by_scope.items():
        try:
            text = gzip.decompress(body).decode("utf-8")
        except (OSError, EOFError, UnicodeDecodeError) as exc:
            raise router_upstream_error(
                spec.error_code_prefix,
                f"LODES WAC gunzip/decode failed state_fips={fips}: {exc}",
            )
        reader = csv.DictReader(io.StringIO(text))
        header = reader.fieldnames or []
        if "w_geocode" not in header:
            raise router_upstream_error(
                spec.error_code_prefix,
                f"LODES WAC CSV state_fips={fips} missing 'w_geocode'; header={header[:8]!r}",
            )
        missing = [c for c in cols if c not in header]
        if missing:
            raise router_upstream_error(
                spec.error_code_prefix,
                f"LODES WAC CSV state_fips={fips} missing column(s) {missing}",
            )
        state_tracts: dict[str, float] = collections.defaultdict(float)
        for row in reader:
            geo = row.get("w_geocode") or ""
            if len(geo) < 11 or not geo.startswith(str(fips)):
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
            state_tracts[tract] += total
        for tract, val in state_tracts.items():
            rec = out.setdefault(tract, {"value": 0.0})
            rec["value"] = (rec.get("value") or 0.0) + val
    return out
