"""The datums row's record: the tie ``pre_resolve`` resolved, shaped as this row's answer.

``station_tie.tie_at`` has already found the station, read its page and put the tie on ``params``
before the cache key; this hook is pure shaping, and an unresolved tie here is a typed refusal
rather than a record with missing heights."""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error
from ..._router.hooks import register_hook

__all__ = ["record"]


@register_hook("noaa_coops_datums.record")
def record(spec: SourceSpec, params: dict[str, Any],
           bodies: list[bytes]) -> dict[str, Any]:
    """The station tie as this row's record: the station, its distance and MHW above NAVD88.

    ``bodies`` is always empty - this row plans no request of its own, because the fetch its
    answer needs already ran in the resolve phase and rides ``params["tie"]``."""
    tie = params.get("tie")
    if not isinstance(tie, dict) or not tie.get("station_id"):
        raise router_empty_error(
            spec.error_code_prefix,
            "the station tie was not resolved before this record was shaped: "
            "no CO-OPS station carrying both MHW and NAVD88 reached the ask.",
            spec.empty_error_suffix)
    datums = dict(tie.get("datums") or {})
    try:
        above = round(float(datums["MHW"]) - float(datums["NAVD88"]), 4)
    except (KeyError, TypeError, ValueError):
        raise router_empty_error(
            spec.error_code_prefix,
            f"station {tie.get('station_id')} resolved without the pair this "
            f"record needs (MHW, NAVD88); its page publishes {sorted(datums)}.",
            spec.empty_error_suffix)
    station_id = str(tie["station_id"])
    return {
        "station_id": station_id,
        "station_name": str(tie.get("station_name") or ""),
        "lon": float(tie["lon"]),
        "lat": float(tie["lat"]),
        "distance_km": float(tie.get("distance_km") or 0.0),
        "datums": datums,
        "mhw_above_navd88_m": above,
        "epoch": str(tie.get("epoch") or ""),
        "units": str(tie.get("units") or "meters"),
        "source": f"NOAA CO-OPS station {station_id} datums",
    }
