"""slider_timestamps record hooks: CIRA/RAMMB SLIDER availability index.

Folds the fetch_slider_timestamps twin onto the record-return output shape
 as a LIVE-NO-CACHE source: one GET of the SLIDER ``latest_times.json``
availability index, parsed + enriched into the availability + cadence dict the
frame-animation recipe stands on. The router owns the transport + the live-no-cache
short-circuit (no cache write: the index turns over every few minutes). These PURE
hooks reuse the shared ``_satellite_slider`` URL builder + timestamp helpers (UNCHANGED
-- still owned by the animation cluster, which imports the raw list[int] helper
directly) and shape the fetched JSON body into the enriched dict.
"""

from __future__ import annotations

import json
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ...imagery import _satellite_slider
from ..errors import router_upstream_error
from . import RequestPlan, register_hook

#: The twin fetched with the shared SLIDER User-Agent; reused verbatim.
_USER_AGENT = _satellite_slider._USER_AGENT


def _estimate_cadence_seconds(ts_int_ascending: list[int]) -> float | None:
    """Median gap (seconds) between consecutive frames, or None for < 2 frames."""
    if len(ts_int_ascending) < 2:
        return None
    dts = [
        _satellite_slider.ts_int_to_datetime(b)
        - _satellite_slider.ts_int_to_datetime(a)
        for a, b in zip(ts_int_ascending, ts_int_ascending[1:])
    ]
    secs = sorted(d.total_seconds() for d in dts)
    mid = len(secs) // 2
    if len(secs) % 2:
        return float(secs[mid])
    return float((secs[mid - 1] + secs[mid]) / 2.0)


@register_hook("slider_timestamps.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """One GET of the SLIDER latest_times.json for the (sat, sector, product)."""
    url = _satellite_slider.build_times_url(
        params["sat"], params["sector"], params["product"]
    )
    return [RequestPlan(url=url, headers={"User-Agent": _USER_AGENT})]


@register_hook("slider_timestamps.record")
def record(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> dict[str, Any] | None:
    """Parse the availability index into the enriched availability + cadence dict.

    A missing ``timestamps_int`` key or a non-JSON body raises the honest typed
    SLIDER_UPSTREAM_ERROR (the twin's ``SliderUpstreamError``); an empty index is a
    VALID zero-frame result (count 0), never a None-advance -- there is one plan.
    """
    sc = spec.error_code_prefix
    body = bodies[0] if bodies else b""
    try:
        parsed = json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"SLIDER time index returned non-JSON: {exc}")
    if not isinstance(parsed, dict) or "timestamps_int" not in parsed:
        got = list(parsed) if isinstance(parsed, dict) else type(parsed).__name__
        raise router_upstream_error(
            sc, f"SLIDER time index missing 'timestamps_int' key; got keys={got}"
        )
    ts_int: list[int] = []
    for v in parsed.get("timestamps_int") or []:
        try:
            ts_int.append(int(v))
        except (TypeError, ValueError):
            continue
    ts_int.sort()
    earliest_iso = _satellite_slider.ts_int_to_iso(ts_int[0]) if ts_int else None
    latest_iso = _satellite_slider.ts_int_to_iso(ts_int[-1]) if ts_int else None
    return {
        "sat": params["sat"],
        "sector": params["sector"],
        "product": params["product"],
        "count": len(ts_int),
        "timestamps_int": ts_int,
        "earliest_iso": earliest_iso,
        "latest_iso": latest_iso,
        "cadence_seconds": _estimate_cadence_seconds(ts_int),
    }
