"""The storm a catchment is driven by: a real hourly hyetograph, or a constant
design rate over a declared duration. A BRANCH ON THE ASK, never a fallback."""

from __future__ import annotations

from typing import Any

from trid3nt_server.workflows.telemac.errors import TelemacError

__all__ = ["resolve_rain_event"]

#: Seconds in an hour, spelled once so no expression below spells it again.
_HOUR_S = 3600.0


def _rain_window_bbox() -> tuple[float, float, float, float]:
    """The extent the hyetograph is fetched over: the bound domain's own."""
    from trid3nt_server.workflows.runtime import current_domain

    domain = current_domain()
    if domain is None or domain.bbox is None:
        raise TelemacError(
            "the rain hyetograph cannot be fetched: no domain is bound. "
            "Resolve the AOI first.", error_code="TELEMAC_DOMAIN_UNBOUND")
    return tuple(float(v) for v in domain.bbox)  # type: ignore[return-value]


def resolve_rain_event(*, window: str | None, intensity_mm_per_hr: float,
                       storm_duration_hr: float,
                       sim_duration_hr: float | None) -> dict[str, Any]:
    """The storm, as either a real hourly hyetograph or a constant design rate.

    A BRANCH ON THE ASK, not a fallback ladder; the note labels a hypothetical."""
    # A dated ``window`` fetches the hourly AORC accumulation over the catchment,
    # so the run is driven by the real intensity structure, which is what resolves
    # the hydrograph SHAPE; with no window the storm is a constant design rate over
    # a declared duration. AORC rather than MRMS: MRMS covers ~2020-10 onward only,
    # and a replication window predating it would silently return nothing.
    from trid3nt_server.tools import TOOL_REGISTRY

    if not window:
        return {
            "kind": "design_storm", "blocks": None, "series": None,
            "time_varying": False,
            "intensity_mm_per_hr": float(intensity_mm_per_hr),
            "duration_s": float(sim_duration_hr if sim_duration_hr
                                else storm_duration_hr) * _HOUR_S,
            # How long it RAINS, as distinct from how long the run watches: a
            # window shorter than the run is what lets the recession limb appear.
            "rain_duration_s": float(storm_duration_hr) * _HOUR_S,
            "duration_basis": "user" if sim_duration_hr else "storm",
            "note": (f"a CONSTANT design storm of {float(intensity_mm_per_hr):g} mm/h "
                     f"over {float(storm_duration_hr):g} h - a hypothetical "
                     "event, not a record."),
        }
    bbox = _rain_window_bbox()
    sep = "/" if "/" in window else (".." if ".." in window else None)
    if not sep:
        raise TelemacError(
            f"the rain window must be 'start/end' dates; got {window!r}.",
            error_code="TELEMAC_RAIN_WINDOW_INVALID")
    start, end = [s.strip() for s in window.split(sep, 1)]
    payload = TOOL_REGISTRY["fetch_aorc_precip"].fn(
        bbox=[float(v) for v in bbox], start_date=start, end_date=end)
    payload = payload if isinstance(payload, dict) else getattr(payload, "__dict__", {})
    mm = [max(0.0, float(v)) for v in payload["precip_mm"]]
    if len(mm) < 2:
        raise TelemacError(
            f"AORC returned {len(mm)} hourly steps for {window!r}; a hyetograph "
            "needs at least two. Widen the window or run the design storm.",
            error_code="TELEMAC_HYETOGRAPH_EMPTY")
    blocks = [[float((i + 1) * _HOUR_S), round(mm[i], 5)] for i in range(len(mm))]
    asked_s = float(sim_duration_hr or 0.0) * _HOUR_S
    span_s = float(len(mm) * _HOUR_S)
    # A record whose wet hours all carry one rate is a constant storm with a
    # date on it, and the engine's own constant branch drives it; two distinct
    # rates are a shape only the block file can state.
    return {
        "kind": "hyetograph", "blocks": blocks, "series": mm,
        "time_varying": len({round(v, 6) for v in mm if v > 0.0}) >= 2,
        "intensity_mm_per_hr": float(intensity_mm_per_hr),
        "duration_s": max(asked_s, span_s),
        "duration_basis": "user" if asked_s > span_s else "hyetograph",
        "window": window, "total_mm": round(sum(mm), 3),
        "note": (f"the REAL hourly AORC hyetograph over {window} - {len(mm)} steps, "
                 f"{sum(mm):.3g} mm total."),
    }
