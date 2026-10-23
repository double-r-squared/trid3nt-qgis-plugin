"""SIFT hooks: the table request, and the named rows read out of it.

NOAA publishes the unit sources as one comma-separated text file whose first two
lines are a comment and a header; every other line is one unit source.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_input_error, router_upstream_error
from ..._router.hooks import RequestPlan, register_hook

logger = logging.getLogger(__name__)

__all__ = ["build_request", "record"]

#: The columns after the name and the file link, in the order the table writes them.
_COLUMNS = ("longitude", "latitude", "slip_m", "strike", "dip", "depth_km", "length_km",
            "width_km", "rake")


@register_hook("sift.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """The one table request, after the names asked for are checked."""
    _names(spec, params)
    return [RequestPlan(url=str(spec.endpoints["table"].url), params={})]


@register_hook("sift.record")
def record(spec: SourceSpec, params: dict[str, Any],
           bodies: list[bytes]) -> dict[str, Any] | None:
    """The named unit sources, or a refusal naming each name the table does not hold."""
    wanted = _names(spec, params)
    sc = spec.error_code_prefix
    try:
        text = (bodies[0] if bodies else b"").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise router_upstream_error(sc, f"the SIFT table is not text: {exc}")
    table: dict[str, dict[str, float]] = {}
    for number, line in enumerate(text.splitlines()[2:], 3):
        tokens = [t.strip() for t in line.split(",")]
        if not line.strip():
            continue
        try:
            table[tokens[0]] = dict(zip(_COLUMNS, (float(t) for t in tokens[2:2 + len(_COLUMNS)])))
        except ValueError as exc:
            raise router_upstream_error(sc, f"line {number} of the SIFT table is no unit source: {exc}")
        if len(table[tokens[0]]) != len(_COLUMNS):
            raise router_upstream_error(sc, f"line {number} of the SIFT table carries too few columns.")
    if not table:
        raise router_upstream_error(sc, "the SIFT table holds no unit source.")
    missing = [name for name in wanted if name not in table]
    if missing:
        raise router_input_error(
            sc, f"the SIFT table holds no unit source named {', '.join(map(repr, missing))} "
            f"(it holds {len(table)}, such as {', '.join(sorted(table)[:3])}).",
            spec.input_error_suffix)
    logger.info("sift: %d of %d unit sources read", len(wanted), len(table))
    return {"unit_sources": {name: table[name] for name in wanted},
            "source": "NOAA PMEL SIFT unit-source table"}


def _names(spec: SourceSpec, params: dict[str, Any]) -> list[str]:
    names = [str(n).strip() for n in (params.get("names") or []) if str(n).strip()]
    if not names:
        raise router_input_error(
            spec.error_code_prefix, "names states no unit source: name the SIFT unit sources to read.",
            spec.input_error_suffix)
    return names
