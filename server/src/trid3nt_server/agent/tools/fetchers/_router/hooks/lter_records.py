"""fetch_lter_records hooks (ADR 0203): LTER / EDI environmental data records.

A generic reader for the long-term ecological / hydrologic station records the US
LTER network publishes on the Environmental Data Initiative (EDI) repository. Given
a PASTA package id (e.g. ``knb-lter-cwt.3037/19``) and an optional entity selector,
it returns a parsed time-series dict for one CSV/TSV data entity of the package.

ACCESS (ADR 0202): EDI's native PASTA REST API returns HTTP 403 anonymously from
this environment. The identical EML metadata + data objects are mirrored PUBLICLY
by DataONE (``cn.dataone.org/cn/v2/resolve/<encoded-PASTA-PID>``), which redirects
to the member-node copy; a credentialed EDI pull would hit the identical bytes. So
every fetch goes through the DataONE ``resolve`` endpoint (redirect-followed by the
router transport). Proven in ``scripts/sandbox/replication/edi_coweeta_coverage.py``
(Coweeta Ball Creek weir #9 hourly discharge, m3/s) -- generalized here.

Two phases over the shared router transport:
- resolve (PRE-cache-key): ``resolve_build`` -> the package's EML metadata request;
  ``resolve_parse`` -> pick the entity, extract its data-object URL + delimiter +
  header rows + column units, merged into params so the resolved entity is part of
  the cache key.
- record: ``build_request`` -> the entity's data-object request; ``build_record``
  -> parse the delimited table into the time-series dict (window-filtered, per-column
  peak/min/mean + units).

All hooks are PURE compute over already-fetched bodies (the router owns the sockets).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_empty_error, router_input_error, router_upstream_error
from . import RequestPlan, register_hook

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.hooks.lter_records"
)

_DATAONE_RESOLVE = "https://cn.dataone.org/cn/v2/resolve/"
_UA = "trid3nt/0.1 (Hazard Modeling Agent; agent@trid3nt.dev)"
#: EML entity blocks that carry a tabular data object.
_ENTITY_TAGS = ("dataTable", "otherEntity", "spatialRaster", "spatialVector")
#: Column-name fragments that are NOT observation values (flags, ids, coordinates).
_NON_VALUE_FRAGMENTS = ("flag", "site", "station", "code", "id", "lat", "lon", "batt")


def _dataone_resolve_url(pasta_pid: str) -> str:
    """The public DataONE resolve URL for a PASTA object PID (redirect-followed)."""
    return _DATAONE_RESOLVE + urllib.parse.quote(pasta_pid, safe="")


def _parse_package_id(sc: str, package_id: str) -> tuple[str, str, str]:
    """``scope.identifier.revision`` -> ``(scope, identifier, revision)``.

    Accepts both the ``scope.id.rev`` and the ``scope.id/rev`` spellings (the
    revision joined by ``.`` or ``/``); the scope may contain hyphens but not the
    final two dot-separated fields. Raises a typed INPUT error on a malformed id.
    """
    raw = (package_id or "").strip().replace("/", ".")
    parts = raw.rsplit(".", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        raise router_input_error(
            sc,
            f"package_id={package_id!r} must be 'scope.identifier.revision' "
            "(e.g. 'knb-lter-cwt.3037/19' or 'knb-lter-cwt.3037.19')",
            "INPUT_INVALID",
        )
    scope, identifier, revision = parts
    if not scope:
        raise router_input_error(
            sc, f"package_id={package_id!r} has an empty scope", "INPUT_INVALID"
        )
    return scope, identifier, revision


def _metadata_pid(scope: str, identifier: str, revision: str) -> str:
    """The canonical PASTA EML metadata PID for a package (the provenance anchor)."""
    return (
        f"https://pasta.lternet.edu/package/metadata/eml/"
        f"{scope}/{identifier}/{revision}"
    )


def _interpret_delimiter(raw: str | None) -> str:
    """EML ``fieldDelimiter`` -> the literal separator char (``\\t`` / ``,`` / ...)."""
    if not raw:
        return "\t"
    s = raw.strip()
    if s in ("\\t", "\t", "tab", "TAB"):
        return "\t"
    if s in ("\\n",):
        return "\n"
    # A literal comma / semicolon / pipe / space passes through.
    return s if len(s) == 1 else (s or "\t")


def _entity_blocks(xml: str) -> list[dict[str, Any]]:
    """Extract the package's data entities from EML: name, data URL, delimiter, units.

    Order-preserving over the EML entity elements. Each block carries ``kind``,
    ``name``, ``url`` (PASTA data PID), ``delimiter``, ``skip`` (numHeaderLines),
    ``columns`` (attributeName order), and ``units`` (attributeName -> unit string).
    """
    blocks: list[dict[str, Any]] = []
    for tag in _ENTITY_TAGS:
        for m in re.finditer(rf"<{tag}\b.*?</{tag}>", xml, re.S):
            blk = m.group(0)
            url_m = re.search(r"<url[^>]*>(.*?)</url>", blk, re.S)
            if not url_m:
                continue
            name_m = re.search(r"<entityName>(.*?)</entityName>", blk, re.S)
            delim_m = re.search(r"<fieldDelimiter>(.*?)</fieldDelimiter>", blk, re.S)
            skip_m = re.search(r"<numHeaderLines>(.*?)</numHeaderLines>", blk, re.S)
            columns: list[str] = []
            units: dict[str, str] = {}
            for attr in re.finditer(r"<attribute\b.*?</attribute>", blk, re.S):
                ab = attr.group(0)
                an = re.search(r"<attributeName>(.*?)</attributeName>", ab, re.S)
                if not an:
                    continue
                col = an.group(1).strip()
                columns.append(col)
                u = re.search(r"<(standardUnit|customUnit)>(.*?)</\1>", ab, re.S)
                if u:
                    units[col] = u.group(2).strip()
            try:
                skip = int(skip_m.group(1).strip()) if skip_m else 1
            except ValueError:
                skip = 1
            blocks.append(
                {
                    "kind": tag,
                    "name": (name_m.group(1).strip() if name_m else ""),
                    "url": url_m.group(1).strip(),
                    "delimiter": _interpret_delimiter(
                        delim_m.group(1) if delim_m else None
                    ),
                    "skip": skip,
                    "columns": columns,
                    "units": units,
                }
            )
    return blocks


def _select_entity(
    sc: str, blocks: list[dict[str, Any]], entity_sel: str | None
) -> dict[str, Any]:
    """Pick the requested entity: a 1-based index, an entityName substring, or the
    first tabular (``dataTable``) entity by default. Raises typed INPUT if none match."""
    tabular = [b for b in blocks if b["kind"] == "dataTable"] or blocks
    if not blocks:
        raise router_input_error(
            sc, "package EML declares no data entities", "INPUT_INVALID"
        )
    if entity_sel is None or not str(entity_sel).strip():
        return tabular[0]
    sel = str(entity_sel).strip()
    if sel.isdigit():
        idx = int(sel)
        if 1 <= idx <= len(blocks):
            return blocks[idx - 1]
        raise router_input_error(
            sc,
            f"entity index {idx} out of range (package has {len(blocks)} entities)",
            "INPUT_INVALID",
        )
    low = sel.lower()
    for b in blocks:
        if low in b["name"].lower():
            return b
    names = [b["name"] for b in blocks]
    raise router_input_error(
        sc, f"entity {sel!r} matched no entity name in {names}", "INPUT_INVALID"
    )


def _pick_date_col(columns: list[str], override: str | None) -> str | None:
    """The date/time column: an explicit override, else the first name mentioning
    'date'/'time' (preferring a bare 'Date'), else the first column."""
    if override:
        return override
    for col in columns:
        if col.lower() in ("date", "datetime", "timestamp", "time"):
            return col
    for col in columns:
        cl = col.lower()
        if "date" in cl or "time" in cl:
            return col
    return columns[0] if columns else None


def _pick_value_cols(
    columns: list[str],
    units: dict[str, str],
    date_col: str | None,
    override: list[str] | None,
) -> list[str]:
    """The observation value columns: an explicit override, else the unit-carrying
    non-flag/non-id columns (capped), else all non-date columns (capped)."""
    if override:
        return [c for c in override if c in columns] or list(override)
    skip = {date_col} if date_col else set()

    def _is_value(col: str) -> bool:
        if col in skip:
            return False
        cl = col.lower()
        return not any(frag in cl for frag in _NON_VALUE_FRAGMENTS)

    unit_cols = [c for c in columns if c in units and _is_value(c)]
    if unit_cols:
        return unit_cols[:8]
    return [c for c in columns if _is_value(c)][:8]


# --------------------------------------------------------------------------- #
# Resolve phase (PRE-cache-key): package_id -> data-entity URL + parse hints.
# --------------------------------------------------------------------------- #


@register_hook("lter_records.resolve_build")
def resolve_build(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """Build the EML metadata request (DataONE resolve of the package's PASTA PID)."""
    sc = spec.error_code_prefix
    package_id = str(params.get("package_id") or "").strip()
    if not package_id:
        raise router_input_error(sc, "package_id is required", "INPUT_INVALID")
    scope, identifier, revision = _parse_package_id(sc, package_id)
    meta_url = _dataone_resolve_url(_metadata_pid(scope, identifier, revision))
    return [RequestPlan(url=meta_url, headers={"User-Agent": _UA})]


@register_hook("lter_records.resolve_parse")
def resolve_parse(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> dict[str, Any]:
    """Parse EML, select the entity, and merge its data URL + parse hints into params."""
    sc = spec.error_code_prefix
    body = bodies[0] if bodies else b""
    xml = body.decode("utf-8", errors="replace")
    if "<eml" not in xml and "<dataTable" not in xml and "<otherEntity" not in xml:
        raise router_upstream_error(
            sc, "package metadata is not EML (unexpected DataONE body)"
        )
    blocks = _entity_blocks(xml)
    entity = _select_entity(sc, blocks, params.get("entity"))
    return {
        "_data_url": _dataone_resolve_url(entity["url"]),
        "_delimiter": entity["delimiter"],
        "_skip": entity["skip"],
        "_columns": json.dumps(entity["columns"]),
        "_units": json.dumps(entity["units"]),
        "_entity_name": entity["name"],
    }


# --------------------------------------------------------------------------- #
# Record phase: fetch the data entity, parse into the time-series dict.
# --------------------------------------------------------------------------- #


@register_hook("lter_records.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """The data-entity request (DataONE resolve URL merged in by the resolve phase)."""
    sc = spec.error_code_prefix
    data_url = params.get("_data_url")
    if not data_url:
        raise router_upstream_error(sc, "resolve phase did not yield a data URL")
    return [RequestPlan(url=str(data_url), headers={"User-Agent": _UA})]


@register_hook("lter_records.build_record")
def build_record(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> dict[str, Any] | None:
    """Parse the delimited data entity into the window-filtered time-series record."""
    sc = spec.error_code_prefix
    body = bodies[0] if bodies else b""
    text = body.decode("utf-8", errors="replace")
    delimiter = str(params.get("_delimiter") or "\t")
    skip = int(params.get("_skip") or 1)
    units: dict[str, str] = json.loads(params.get("_units") or "{}")

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) <= skip:
        raise router_upstream_error(sc, "data entity has no rows below the header")
    # The header is the last of the declared header lines (numHeaderLines).
    header = [h.strip() for h in lines[skip - 1].split(delimiter)]
    data_lines = lines[skip:]

    date_col = _pick_date_col(header, params.get("date_col"))
    value_cols = _pick_value_cols(
        header, units, date_col, params.get("value_cols")
    )
    if date_col not in header:
        raise router_input_error(
            sc, f"date_col {date_col!r} not in columns {header}", "INPUT_INVALID"
        )
    present_values = [c for c in value_cols if c in header]
    if not present_values:
        raise router_input_error(
            sc,
            f"no value column resolved from {value_cols} in columns {header}",
            "INPUT_INVALID",
        )
    di = header.index(date_col)
    vidx = {c: header.index(c) for c in present_values}

    start = params.get("start_date")
    end = params.get("end_date")

    times: list[str] = []
    cols: dict[str, list[float | None]] = {c: [] for c in present_values}
    for ln in data_lines:
        cells = ln.split(delimiter)
        if di >= len(cells):
            continue
        ts = cells[di].strip()
        if not ts:
            continue
        day = ts[:10]
        if start and day < str(start):
            continue
        if end and day > str(end):
            continue
        times.append(ts)
        for c, ci in vidx.items():
            raw = cells[ci].strip() if ci < len(cells) else ""
            try:
                cols[c].append(float(raw))
            except ValueError:
                cols[c].append(None)

    if not times:
        raise router_empty_error(
            sc,
            f"no rows in window {start}..{end} for entity "
            f"{params.get('_entity_name')!r}",
            spec.empty_error_suffix,
        )

    summary: dict[str, Any] = {}
    for c, vals in cols.items():
        nums = [v for v in vals if v is not None]
        summary[c] = {
            "units": units.get(c),
            "n": len(nums),
            "peak": round(max(nums), 6) if nums else None,
            "min": round(min(nums), 6) if nums else None,
            "mean": round(sum(nums) / len(nums), 6) if nums else None,
        }

    return {
        "source": "EDI / US-LTER (via DataONE mirror)",
        "package_id": str(params.get("package_id")),
        "entity": params.get("_entity_name"),
        "date_col": date_col,
        "value_columns": present_values,
        "units": {c: units.get(c) for c in present_values},
        "n_rows": len(times),
        "first_ts": times[0],
        "last_ts": times[-1],
        "window": [start, end] if (start or end) else None,
        "times": times,
        "series": {c: cols[c] for c in present_values},
        "summary": summary,
    }
