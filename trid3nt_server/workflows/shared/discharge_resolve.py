"""Shared NWM-derived river discharge resolution (law 9).

An estuary / river simulation needs a freshwater inflow (m3/s) to force it. A
baked demo constant (SCHISM baroclinic's 500 m3/s freshwater source) is an
invented world value: the discharge governs the salt-intrusion length and the
whole estuarine gradient, so a wrong inflow silently reshapes the result. Law 9
forbids defaulting it.

This is the single resolution seam engines share for a bulk AOI inflow. The real
source is the NOAA National Water Model (``fetch_noaa_nwm_streamflow``), a point
FlatGeobuf of NHDPlus reaches each carrying ``streamflow_cms`` (m3/s). The DOMINANT
reach - the one carrying the largest streamflow within the AOI - is a SCREENING
proxy for the freshwater inflow (the river-dye path resolves the reach NEAREST a
seed point instead; the estuary case wants a bulk inflow, not a seeded reach). It
is a real value, loudly labeled: for a WIDE tidal-bay AOI the main-stem river often
enters upstream of the bay footprint, so this under-samples the true inflow - a
tighter river-mouth AOI or an explicit discharge is the calibrated path (an NLDI
upstream-navigation refinement to find the true inflow reach is QUEUED).

- ``resolve_dominant_discharge`` -> the user -> NWM-dominant-reach -> REFUSE ladder,
  with a ``SyntheticInput`` provenance entry the caller narrates under its own name.

A caller-supplied value is used (``user``); when NWM can serve, the dominant-reach
discharge is DERIVED (``derived``, source named, screening caveat stated); when NWM
cannot serve (fetch fails, AOI off the CONUS NWM domain, or no positive reach) the
value is UNRESOLVED and its ``SyntheticInput`` carries ``basis="default_demo",
consequence="physics"`` so the input-review gate REFUSES in auto mode. There is no
invented value to fall back to.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

from trid3nt_contracts.common import SyntheticInput

from trid3nt_server.data import TOOL_REGISTRY

logger = logging.getLogger("trid3nt_server.workflows.shared.discharge_resolve")

__all__ = [
    "DischargeResolution",
    "dominant_reach_discharge",
    "resolve_dominant_discharge",
]


def dominant_reach_discharge(bbox: Any) -> tuple[float | None, dict[str, Any]]:
    """Largest NWM ``streamflow_cms`` over ``bbox`` (the main-stem carrier).

    Fetches ``fetch_noaa_nwm_streamflow`` via ``TOOL_REGISTRY`` (never a module
    internal), reads the returned reach FlatGeobuf, and returns
    ``(max_positive_streamflow_m3s_or_None, meta)``. None when the fetch/read fails
    or no reach carries a positive streamflow (the caller REFUSES - no demo
    default). NEVER raises. Blocking (network + geopandas read); the caller wraps it
    in a thread.
    """
    meta: dict[str, Any] = {}
    try:
        layer = TOOL_REGISTRY["fetch_noaa_nwm_streamflow"].fn(bbox=list(bbox))
    except Exception as exc:  # noqa: BLE001 -- fetch miss => REFUSE upstream
        meta["reason"] = f"NWM streamflow fetch error: {exc}"
        logger.warning(
            "discharge: fetch_noaa_nwm_streamflow failed (non-fatal, will REFUSE - "
            "no demo default): %s",
            exc,
        )
        return None, meta
    uri = getattr(layer, "uri", None) or (
        layer.get("uri") if isinstance(layer, dict) else None
    )
    if not uri:
        meta["reason"] = "NWM returned no reach layer (AOI off the CONUS NWM domain)"
        return None, meta

    local: str | None = None
    try:
        import geopandas as gpd  # lazy: never imported on the offline path

        from trid3nt_server.data.simulation.solver.solver import (
            _get_s3_client,
            _split_object_uri,
        )

        _scheme, bucket, key = _split_object_uri(str(uri))
        fd, local = tempfile.mkstemp(prefix="nwm-", suffix=os.path.splitext(key)[1] or ".fgb")
        os.close(fd)
        s3 = _get_s3_client()
        resp = s3.get_object(Bucket=bucket, Key=key)
        with open(local, "wb") as fh:
            fh.write(resp["Body"].read())
        gdf = gpd.read_file(local, engine="pyogrio")
    except Exception as exc:  # noqa: BLE001 -- read miss => REFUSE upstream
        meta["reason"] = f"NWM reach-layer read error: {exc}"
        logger.warning("discharge: could not read NWM layer %s (%s)", uri, exc)
        return None, meta
    finally:
        if local and os.path.exists(local):
            try:
                os.unlink(local)
            except OSError:
                pass

    best_q: float | None = None
    n_reaches = 0
    for _idx, row in gdf.iterrows():
        try:
            q = float(row["streamflow_cms"])
        except (KeyError, TypeError, ValueError):
            continue
        if q > 0.0:
            n_reaches += 1
            if best_q is None or q > best_q:
                best_q = q
    if best_q is None:
        meta["reason"] = "no NWM reach over the AOI carries a positive streamflow"
        return None, meta
    meta.update({"n_reaches": n_reaches, "dominant_streamflow_m3s": round(best_q, 1)})
    return round(best_q, 1), meta


@dataclass
class DischargeResolution:
    """The resolved freshwater discharge (m3/s) + its machine-readable provenance.

    ``discharge_m3s`` is None only when UNRESOLVED (NWM could not serve and the
    caller supplied nothing) - then ``entry`` carries ``basis="default_demo",
    consequence="physics"`` so the input-review gate refuses in auto mode. When the
    resolution proceeds (user or derived), the value is real (never an invention).
    """

    discharge_m3s: float | None
    source: str  # "user_supplied" | "nwm_dominant_reach" | "unresolved"
    entry: SyntheticInput
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        """True when the discharge is real (never an invented default)."""
        return self.discharge_m3s is not None


def resolve_dominant_discharge(
    bbox: Any,
    user_value: float | None,
    *,
    param_name: str = "river_discharge_m3s",
    note_role: str = "freshwater inflow to the estuary",
) -> DischargeResolution:
    """Resolve a bulk AOI freshwater discharge: user -> NWM-dominant-reach -> REFUSE.

    Synchronous (the NWM fetch + geopandas read block); the caller offloads it to a
    thread. ``note_role`` lets each engine narrate the physical role of the inflow.
    """
    if user_value is not None:
        q = float(user_value)
        return DischargeResolution(
            discharge_m3s=round(q, 1),
            source="user_supplied",
            entry=SyntheticInput(
                param=param_name, value=round(q, 1), units="m3/s",
                basis="user", consequence="physics", real_source_if_any=None,
                note=f"caller-supplied {note_role}.",
            ),
            meta={"source": "user_supplied"},
        )

    q_bar, meta = dominant_reach_discharge(bbox)
    if q_bar is not None and math.isfinite(q_bar) and q_bar > 0:
        return DischargeResolution(
            discharge_m3s=float(q_bar),
            source="nwm_dominant_reach",
            entry=SyntheticInput(
                param=param_name, value=round(float(q_bar), 1), units="m3/s",
                basis="derived", consequence="physics",
                real_source_if_any="fetch_noaa_nwm_streamflow (NWM analysis, dominant reach)",
                note=(
                    f"DERIVED as the {note_role}: the LARGEST NWM analysis streamflow "
                    f"reach WITHIN the AOI ({meta.get('n_reaches', '?')} reaches). "
                    "SCREENING estimate - a single steady bulk inflow, NOT a gauged "
                    "hydrograph. For a WIDE tidal estuary the main-stem river inflow "
                    "often enters UPSTREAM of the bay footprint and is under-sampled "
                    "here; supply an explicit discharge, or a tighter river-mouth AOI, "
                    "for a representative inflow."
                ),
            ),
            meta=meta,
        )

    return DischargeResolution(
        discharge_m3s=None,
        source="unresolved",
        entry=SyntheticInput(
            param=param_name, value=None, units="m3/s",
            basis="default_demo", consequence="physics", real_source_if_any=None,
            note=(
                f"the {note_role} is required and could not be resolved from NWM "
                f"streamflow at this AOI ({meta.get('reason', 'unavailable')}). No "
                f"invented default (law 9): supply {param_name} or run over an AOI with "
                "NWM (CONUS) coverage."
            ),
        ),
        meta=meta,
    )
