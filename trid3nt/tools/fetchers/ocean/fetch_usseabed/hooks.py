"""usseabed hooks: USGS usSEABED sediment texture samples.

The bbox packager's query is one JSON form field (``metadata``), not a query
string, and its answer is a ZIP whose ``ext`` table carries the samples --
neither is a step the declarative surface expresses, so both are here. The
parse hook reads the ZIP's own lat/lon columns for geometry and nulls the
service's own NODATA sentinels rather than reading -99 as a measurement; every other
column is kept under the service's name."""

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

#: The service's own no-value sentinels, read as no value rather than as -99.
_NODATA_NUM = {"-99", "-99.0", "-99.00"}
_NODATA_STR = {"-", ""}

#: The column the geometry is the WKB of; the position is the latitude and longitude columns.
_GEOMETRY_COLUMN = "geom"


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


def _cell(raw: str | None) -> str | None:
    """A cell as the service wrote it, its no-value sentinels and blanks as ``None``."""
    if raw is None:
        return None
    raw = raw.strip()
    return None if raw in _NODATA_NUM or raw in _NODATA_STR else raw


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


@_hooks.register_hook("usseabed.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Read the package ZIP's ``ext`` table into Point features, every column kept, sentinels nulled."""
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

    rows = [{k: _cell(v) for k, v in row.items() if k != _GEOMETRY_COLUMN} for row in csv.DictReader(io.StringIO(text))]
    # A column is a number where every cell it holds is one.
    for column in rows[0] if rows else ():
        held = [row[column] for row in rows if row[column] is not None]
        parsed = [_number(v) for v in held]
        if held and None not in parsed:
            whole = all(v.lstrip("+-").isdigit() for v in held)
            for row in rows:
                row[column] = None if row[column] is None else (int(row[column]) if whole else float(row[column]))
    out: list[dict[str, Any]] = []
    for row in rows:
        lat, lon = row.get("latitude"), row.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        out.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": row,
        })
    if not out:
        raise router_empty_error(sc, "usSEABED package carries no ext samples "
                                  "over this bbox", spec.empty_error_suffix)
    return out
