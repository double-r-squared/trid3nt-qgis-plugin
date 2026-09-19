"""usseabed hooks: USGS usSEABED sediment texture samples.

The bbox packager's query is one JSON form field (``metadata``), not a query
string, and its answer is a ZIP whose ``ext`` table carries the samples --
neither is a step the declarative surface expresses, so both are here. The
parse hook reads the ZIP's own lat/lon columns for geometry and nulls the
service's own NODATA sentinels rather than reading -99 as a measurement."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router import hooks as _hooks
from ..._router.errors import router_empty_error, router_upstream_error

__all__ = ["build_request", "parse_response"]

PACKAGE_URL = "https://cmgds.marine.usgs.gov/usseabed/package.php"

#: The lab-analysed table only (the ``ext`` source), one CSV per package ZIP,
#: named ``us9_<source>.csv``.
_MEMBER_SUFFIX = "us9_ext.csv"

#: Raw-column -> emitted-column, the record's own values, renamed with the
#: unit the coverage row states.
_COLUMN_MAP = {
    "waterdepth": "waterdepth_m",
    "gravel": "gravel_pct",
    "sand": "sand_pct",
    "mud": "mud_pct",
    "clay": "clay_pct",
    "grainsze": "grainsize_phi",
    "sorting": "sorting_phi",
}
_PASSTHROUGH_COLUMNS = ("key", "locnname", "obsvndate")

#: The service's own no-value sentinels, read as no value rather than as -99.
_NODATA_NUM = {"-99", "-99.0", "-99.00"}
_NODATA_STR = {"-", ""}


def _metadata(bbox: list[float]) -> dict[str, Any]:
    min_lon, min_lat, max_lon, max_lat = bbox
    return {
        "westbc": min_lon, "eastbc": max_lon,
        "northbc": max_lat, "southbc": min_lat,
        "sources": ["ext"],
        "procdesc": "trid3nt bbox query",
    }


@_hooks.register_hook("usseabed.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """One POST: the bbox as the packager's ``metadata`` form field."""
    bbox = list(params["bbox"])
    return [
        _hooks.RequestPlan(
            url=PACKAGE_URL,
            headers={"User-Agent": spec.auth.user_agent},
            method="POST",
            data={"metadata": json.dumps(_metadata(bbox))},
        )
    ]


def _num(raw: str | None) -> float | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw or raw in _NODATA_NUM:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return None if value == -99.0 else value


def _text(raw: str | None) -> str | None:
    if raw is None:
        return None
    raw = raw.strip()
    return None if raw in _NODATA_STR else raw


@_hooks.register_hook("usseabed.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Read the package ZIP's ``ext`` table into Point features, sentinels nulled."""
    sc = spec.error_code_prefix
    raw = bodies[0] if bodies else b""
    if not raw:
        raise router_empty_error(sc, "usSEABED packager returned an empty body",
                                  spec.empty_error_suffix)
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise router_upstream_error(sc, f"usSEABED package is not a ZIP: {exc}")
    member = next((n for n in archive.namelist()
                   if n.lower().endswith(_MEMBER_SUFFIX)), None)
    if member is None:
        raise router_upstream_error(
            sc, f"usSEABED package carries no {_MEMBER_SUFFIX}; "
            f"members={archive.namelist()!r}")
    try:
        text = archive.read(member).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise router_upstream_error(sc, f"usSEABED {_MEMBER_SUFFIX} is not UTF-8: {exc}")

    out: list[dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(text)):
        lat, lon = _num(row.get("latitude")), _num(row.get("longitude"))
        if lat is None or lon is None:
            continue
        props: dict[str, Any] = {name: _text(row.get(name)) for name in _PASSTHROUGH_COLUMNS}
        for raw_col, emitted_col in _COLUMN_MAP.items():
            props[emitted_col] = _num(row.get(raw_col))
        out.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props,
        })
    if not out:
        raise router_empty_error(sc, "usSEABED package carries no ext samples "
                                  "over this bbox", spec.empty_error_suffix)
    return out
