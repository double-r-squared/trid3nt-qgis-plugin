"""chirps_precipitation hooks: the date the archive publishes an object under.

One irreducible per-source step, PURE. ``pre_resolve`` parses the asked date at
the asked period, holds it inside the published record, and names the object URL
the session's gdal provider opens - so the date reaches the provider datasource
as the archive's own path rather than as a template the executor would have to
understand."""

from __future__ import annotations

import re
from datetime import date as _date
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router import hooks as _hooks
from ..._router.errors import router_input_error

__all__ = ["pre_resolve"]


@_hooks.register_hook("chirps_precipitation.pre_resolve")
def pre_resolve(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Parse the date against the period's template and return the object URL,
    which enters the cache key with the date and period that built it. A
    template naming ``{day}`` requires a full YYYY-MM-DD; a monthly one accepts
    YYYY-MM. Record bounds refuse before anything is opened."""
    sc = spec.error_code_prefix
    isfx = spec.input_error_suffix
    block = (spec.ingest or {}).get("qgis_provider") or {}
    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    base = (endpoint.url or endpoint.url_template or "").rstrip("/")

    period = params.get(block.get("period_param", "period"))
    template = (block.get("url_templates") or {}).get(period)
    if template is None:
        raise router_input_error(sc, f"no URL template for period={period!r}", isfx)

    asked = params.get(block.get("date_param", "date"))
    if not isinstance(asked, str) or not asked.strip():
        raise router_input_error(
            sc, f"date must be a non-empty string; got {asked!r}", isfx)
    text = asked.strip()
    if "{day" in template:
        shape, pattern = "YYYY-MM-DD", r"(\d{4})-(\d{2})-(\d{2})"
    else:
        shape, pattern = "YYYY-MM or YYYY-MM-DD", r"(\d{4})-(\d{2})(?:-\d{2})?"
    matched = re.fullmatch(pattern, text)
    if not matched:
        raise router_input_error(
            sc, f"date={asked!r} is not a valid {period} date: expected {shape}", isfx)
    year, month = int(matched.group(1)), int(matched.group(2))
    day = int(matched.group(3)) if matched.lastindex == 3 else 1
    try:
        published = _date(year, month, day)
    except ValueError as exc:
        raise router_input_error(
            sc, f"date={asked!r} is not a valid {period} date: {exc}", isfx)
    min_year = int(block.get("min_year", 0))
    if published.year < min_year:
        raise router_input_error(
            sc, f"source record starts in {min_year}; date={asked!r} predates it", isfx)
    if published > datetime.now(timezone.utc).date():
        raise router_input_error(
            sc, f"date={asked!r} is in the future; only past data is published", isfx)
    return {
        "object_url": template.format(base=base, year=year, month=month, day=day),
    }
