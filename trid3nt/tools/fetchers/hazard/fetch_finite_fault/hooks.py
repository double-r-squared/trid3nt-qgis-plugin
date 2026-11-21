"""Finite-fault hooks: the event record that names the product file, and the FSP text read into subfaults.

USGS writes each rupture model as an SRCMOD FSP file: ``%`` header lines, then one block of
subfault rows per segment. Strike, dip, subfault size and the row count are read from the file's
own headers; the X==EW column and the Nsbfs counts are not trusted.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import RequestPlan, register_hook

logger = logging.getLogger(__name__)

__all__ = ["build_request", "record", "resolve_build", "resolve_parse"]

_FILE = "complete_inversion.fsp"
_NUMBER = r"([-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)"
_SEGMENT = re.compile(rf"^%\s*SEGMENT\s*#\s*(\d+)\s*:\s*STRIKE\s*=\s*{_NUMBER}\s*(?:deg)?\s*,?\s*DIP\s*=\s*{_NUMBER}")
_MECH = re.compile(rf"^%\s*Mech\s*:\s*STRK\s*=\s*{_NUMBER}\s+DIP\s*=\s*{_NUMBER}")
_SIZE = re.compile(rf"^%\s*Size\s*:.*?Mw\s*=\s*{_NUMBER}\s+Mo\s*=\s*{_NUMBER}")
_SIZES = re.compile(rf"^%\s*(?:Invs\s*:\s*)?Dx\s*=\s*{_NUMBER}\s*km\s+Dz\s*=\s*{_NUMBER}\s*km")
_COLUMNS = ("latitude", "longitude", "x_ew", "y_ns", "depth_km", "slip_m", "rake", "rupture_time_s",
            "rise_time_s", "sf_moment")


@register_hook("finite_fault.resolve_build")
def resolve_build(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """The ComCat request for the event record."""
    return [RequestPlan(url=str(spec.endpoints["event"].url),
                        params={"eventid": _event(spec, params), "format": "geojson"})]


@register_hook("finite_fault.resolve_parse")
def resolve_parse(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> dict[str, Any]:
    """The newest finite-fault product that holds the complete inversion -> its file URL, code and update time merged into params."""
    event = _event(spec, params)
    sc = spec.error_code_prefix
    try:
        products = ((json.loads(bodies[0].decode("utf-8")).get("properties") or {})
                    .get("products") or {}).get("finite-fault") or []
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
        raise router_upstream_error(sc, f"the ComCat record of {event!r} is no GeoJSON event: {exc}")
    held = [p for p in products if ((p.get("contents") or {}).get(_FILE) or {}).get("url")]
    if not held:
        raise router_empty_error(
            sc, f"event {event!r} has no finite-fault product with a {_FILE} in ComCat "
            f"({len(products)} finite-fault product(s) listed).", spec.empty_error_suffix)
    newest = max(held, key=lambda p: int(p.get("updateTime") or 0))
    return {"_fsp_url": newest["contents"][_FILE]["url"], "_fsp_code": str(newest.get("code") or ""),
            "_fsp_updated_ms": int(newest.get("updateTime") or 0)}


@register_hook("finite_fault.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """The one request for the product's FSP file."""
    return [RequestPlan(url=str(params["_fsp_url"]), params={})]


@register_hook("finite_fault.record")
def record(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> dict[str, Any] | None:
    """The FSP text -> the event's segments and subfaults."""
    sc = spec.error_code_prefix
    try:
        text = (bodies[0] if bodies else b"").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise router_upstream_error(sc, f"the finite-fault file is not text: {exc}")
    segments, subfaults, mw, mo = parse_fsp(text, sc)
    logger.info("finite_fault: %s %d segment(s), %d subfault(s)", params.get("event_id"),
                len(segments), len(subfaults))
    return {"event_id": _event(spec, params), "magnitude_mw": mw, "moment_nm": mo,
            "segments": segments, "subfaults": subfaults,
            "source": {"product_code": params.get("_fsp_code"), "updated_ms": params.get("_fsp_updated_ms"),
                       "url": params.get("_fsp_url")}}


def parse_fsp(text: str, sc: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float, float]:
    """FSP text -> (segments, subfaults, Mw, Mo); a value the file does not state refuses naming it."""
    def refuse(message: str) -> Exception:
        return router_upstream_error(sc, f"the finite-fault file {message}")

    mw = mo = mech = global_sizes = None
    blocks: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("%"):
            if (m := _SEGMENT.match(stripped)):
                blocks.append({"segment": int(m[1]), "strike": float(m[2]), "dip": float(m[3]),
                               "sizes": None, "rows": []})
            elif (m := _SIZES.match(stripped)):
                sizes = (float(m[1]), float(m[2]))
                if blocks:
                    blocks[-1]["sizes"] = sizes
                else:
                    global_sizes = sizes
            elif (m := _MECH.match(stripped)):
                mech = (float(m[1]), float(m[2]))
            elif (m := _SIZE.match(stripped)):
                mw, mo = float(m[1]), float(m[2])
            continue
        tokens = stripped.split()
        try:
            values = [float(t) for t in tokens]
        except ValueError:
            raise refuse(f"line {number} is no subfault row: {stripped[:60]!r}")
        if len(values) != len(_COLUMNS) or not all(math.isfinite(v) for v in values):
            raise refuse(f"line {number} carries {len(values)} values; a subfault row carries {len(_COLUMNS)}.")
        if not blocks:
            if mech is None:
                raise refuse("states subfault rows before any segment and no Mech line (strike and dip).")
            blocks.append({"segment": 1, "strike": mech[0], "dip": mech[1], "sizes": None, "rows": []})
        blocks[-1]["rows"].append(dict(zip(_COLUMNS, values)))
    if mw is None or mo is None:
        raise refuse("states no Size line (Mw and Mo).")
    segments, subfaults = [], []
    for block in blocks:
        sizes = block["sizes"] or global_sizes
        if sizes is None:
            raise refuse(f"states no Dx and Dz for segment {block['segment']}.")
        if not block["rows"]:
            raise refuse(f"segment {block['segment']} holds no subfault row.")
        segments.append({"segment": block["segment"], "strike": block["strike"], "dip": block["dip"]})
        for row in block["rows"]:
            subfaults.append({
                "segment": block["segment"], "longitude": row["longitude"], "latitude": row["latitude"],
                "depth_km": row["depth_km"], "strike": block["strike"], "dip": block["dip"],
                "rake": row["rake"], "slip_m": row["slip_m"], "length_km": sizes[0], "width_km": sizes[1],
                "rupture_time_s": row["rupture_time_s"], "rise_time_s": row["rise_time_s"]})
    return segments, subfaults, mw, mo


def _event(spec: SourceSpec, params: dict[str, Any]) -> str:
    event = str(params.get("event_id") or "").strip()
    if not event:
        raise router_input_error(spec.error_code_prefix, "event_id states no event: name the ComCat event id.",
                                 spec.input_error_suffix)
    return event
