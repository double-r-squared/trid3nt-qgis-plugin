"""Map-click point probe -- PURE PYTHON (no PyQGIS / PyQt imports).

One POST per click, deterministic and with no LLM in the loop. The SERVER
groups an animation-frame sequence into one ``series`` entry, so nothing here
groups: this builds the request, parses the reply, formats the note block."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List

__all__ = [
    "DEFAULT_PROBE_TIMEOUT",
    "ProbePointRequestError",
    "format_probe_result",
    "post_probe_point",
]

#: A probe click waits for one HTTP round trip; generous but bounded (the
#: server caps work at 40 raster layers per click, so this should never be
#: reached in practice).
DEFAULT_PROBE_TIMEOUT = 30.0


class ProbePointRequestError(Exception):
    """The probe API call failed -- carries the server's honest message, or
    an honest local description (agent unreachable, non-JSON reply)."""


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """The server's own ``{"error": ...}`` message, prefixed with the HTTP
    status."""
    detail = ""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
        if isinstance(payload, dict):
            detail = str(payload.get("error") or "")
    except Exception:  # noqa: BLE001 -- body may be anything
        pass
    return f"HTTP {exc.code}" + (f": {detail}" if detail else "")


def _build_probe_body(case_id: str, lon: float, lat: float) -> bytes:
    """``{"case_id", "lon", "lat"}`` as JSON bytes; the point is EPSG:4326."""
    return json.dumps(
        {"case_id": case_id, "lon": lon, "lat": lat}
    ).encode("utf-8")


def _parse_probe_response(raw: bytes) -> Dict[str, Any]:
    """Raw HTTP body bytes -> the result dict. A non-JSON or non-object body
    raises rather than degrading to an empty result."""
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbePointRequestError(f"probe API returned non-JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise ProbePointRequestError("probe API returned a non-object body")
    return result


def post_probe_point(
    base_url: str,
    case_id: str,
    lon: float,
    lat: float,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> Dict[str, Any]:
    """``POST {base_url}/api/probe-point`` -> the parsed result dict.
    BLOCKING: the caller runs this off the UI thread."""
    url = f"{base_url.rstrip('/')}/api/probe-point"
    request = urllib.request.Request(
        url,
        data=_build_probe_body(case_id, lon, lat),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ProbePointRequestError(_http_error_detail(exc)) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ProbePointRequestError(
            f"probe API unreachable at {url} ({exc}) -- is the local agent "
            "running with its HTTP listener?"
        ) from exc
    return _parse_probe_response(raw)


# --------------------------------------------------------------------------- #
# Response -> dock note-block formatting.
# --------------------------------------------------------------------------- #


def _format_number(value: float) -> str:
    return f"{value:.3g}"


def _format_single_line(entry: Dict[str, Any]) -> str:
    """One non-series result entry -> ``"<name>: <value> <units>"``, or the
    honest reason (``note`` / ``error``) when the point has no value."""
    name = str(entry.get("name") or entry.get("layer_id") or "layer")
    value = entry.get("value")
    if value is None:
        reason = entry.get("note") or entry.get("error") or "no data"
        return f"{name}: {reason}"
    units = entry.get("units")
    text = _format_number(float(value))
    if units:
        text = f"{text} {units}"
    return f"{name}: {text}"


def _format_series_line(entry: Dict[str, Any]) -> str:
    """One series result entry -> a compact chain line with a step count and
    a peak. A step with no value renders ``"--"`` rather than being dropped,
    so the step count always matches the frame count."""
    name = str(entry.get("name") or "series")
    series = entry.get("series") or []
    units = entry.get("units")
    numeric: List[float] = [
        float(pt["value"])
        for pt in series
        if isinstance(pt, dict) and isinstance(pt.get("value"), (int, float))
    ]
    unit_suffix = f" {units}" if units else ""
    if not numeric:
        return f"{name}: no data ({len(series)} steps)"
    chain = " -> ".join(
        _format_number(float(pt["value"]))
        if isinstance(pt, dict) and isinstance(pt.get("value"), (int, float))
        else "--"
        for pt in series
    )
    peak = max(numeric)
    return (
        f"{name}: {chain}{unit_suffix} "
        f"({len(series)} steps, peak {_format_number(peak)})"
    )


def format_probe_result(result: Dict[str, Any]) -> List[str]:
    """The dock's compact note-block lines for one probe click, one per
    ``results`` entry. An empty ``results`` returns a single honest line
    rather than an empty block, and a truncated reply appends another."""
    results = result.get("results") or []
    if not results:
        return ["No layers to probe at this point."]
    lines: List[str] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        if "series" in entry:
            lines.append(_format_series_line(entry))
        else:
            lines.append(_format_single_line(entry))
    if result.get("truncated"):
        lines.append(
            "(case has more raster layers than this probe samples -- some were skipped)"
        )
    return lines


def probe_location_label(lon: float, lat: float) -> str:
    """The dock's short point label for the note header, e.g.
    ``"(-85.42000, 29.95000)"`` -- 5 decimal places (~1 m precision)."""
    return f"({lon:.5f}, {lat:.5f})"
