"""Declared forcing DATA for a TELEMAC reach: net rain/evaporation, carrier discharge.

Both are ``Data`` producers rather than steps: they are artifacts fetched for the
current domain, and the plan Refs them into the deck.

Neither ever invents a number. The rain producer walks a DECLARED LADDER (a real
gridMET storm total supersedes a user rate) and a gridMET failure REFUSES typed -
degrading a requested real storm to zero rain would be a silent no-rain solve.
The discharge producer refuses typed when the National Water Model has no
coverage, so the value that governs dilution is never a baked constant.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any

from trid3nt_server.declarative import Step

from .errors import TelemacDyeScenarioError, TelemacDyeScenarioInputError

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.forcing")

__all__ = ["CarrierDischarge", "resolve_carrier_discharge", "resolve_rain_forcing"]

_STEPS = "trid3nt_server.workflows.telemac.steps"

#: Half-width (deg) of the NWM query box centred on the reach seed. NWM is a
#: ~2.7M-reach point layer; a small box keeps it to a handful of reaches so the
#: nearest-to-seed pick lands on the carrier reach, not a distant tributary.
_DISCHARGE_QUERY_HALF_DEG: float = 0.03

#: Physically-sane band for the signed net rain-or-evaporation rate (a violent
#: storm ~500 mm/day; extreme PET ~20 mm/day), so a bad knob cannot destabilize
#: the solve.
_NET_RAIN_MIN_MM_DAY, _NET_RAIN_MAX_MM_DAY = -50.0, 2000.0


def _domain_bbox() -> tuple[float, float, float, float]:
    from trid3nt_server.declarative import current_domain

    domain = current_domain()
    if domain is None or domain.bbox is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_SCENARIO_ERROR",
            "forcing cannot be resolved: no domain is bound.",
        )
    return tuple(domain.bbox)  # type: ignore[return-value]


def _parse_gridmet_window(window: str) -> tuple[str, str]:
    """``"YYYY-MM-DD:YYYY-MM-DD"`` -> (start, end). A bad window is a loud refusal."""
    parts = [p.strip() for p in str(window or "").split(":") if p.strip()]
    if len(parts) != 2:
        raise TelemacDyeScenarioInputError(
            f"rainfall_gridmet_window must be 'YYYY-MM-DD:YYYY-MM-DD' (got {window!r})."
        )
    import datetime as _dt
    try:
        _dt.date.fromisoformat(parts[0])
        _dt.date.fromisoformat(parts[1])
    except ValueError as exc:
        raise TelemacDyeScenarioInputError(
            f"rainfall_gridmet_window has a non-ISO date: {exc}"
        ) from exc
    return parts[0], parts[1]


def _gridmet_domain_mean_pr(bbox: tuple[float, float, float, float],
                            start_date: str, end_date: str) -> float:
    """Domain-mean daily precipitation (mm/day) from the wired gridMET fetcher.

    Any failure REFUSES typed: a requested real storm never silently degrades to
    zero rain.
    """
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile

    from trid3nt_server.data import TOOL_REGISTRY
    from trid3nt_server.data.simulation.solver.solver import _get_s3_client

    try:
        layer = TOOL_REGISTRY["fetch_gridmet"].fn(
            bbox=list(bbox), variable="pr", start_date=start_date, end_date=end_date)
    except Exception as exc:  # noqa: BLE001
        raise TelemacDyeScenarioError(
            "TELEMAC_RAIN_SOURCE_FAILED",
            f"gridMET precip fetch failed for {start_date}..{end_date}: {exc}",
        ) from exc
    uri = getattr(layer, "uri", None) or (
        layer.get("uri") if isinstance(layer, dict) else None)
    if not uri:
        raise TelemacDyeScenarioError(
            "TELEMAC_RAIN_SOURCE_FAILED", "gridMET fetch returned no COG uri.")
    try:
        if str(uri).startswith("s3://"):
            bucket, _, key = str(uri)[len("s3://"):].partition("/")
            data = _get_s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
            with MemoryFile(data) as mem, mem.open() as ds:
                arr = ds.read(1, masked=True).astype("float64")
        else:
            with rasterio.open(str(uri)) as ds:
                arr = ds.read(1, masked=True).astype("float64")
    except Exception as exc:  # noqa: BLE001
        raise TelemacDyeScenarioError(
            "TELEMAC_RAIN_SOURCE_FAILED", f"gridMET COG read failed: {exc}") from exc
    vals = np.asarray(arr.compressed() if hasattr(arr, "compressed") else arr,
                      dtype="float64")
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        raise TelemacDyeScenarioError(
            "TELEMAC_RAIN_SOURCE_FAILED",
            "gridMET precip COG had no finite pixels over the reach AOI.")
    return float(vals.mean())


async def resolve_rain_forcing(*, rainfall_mm_per_day: float | None,
                               evaporation_mm_per_day: float | None,
                               gridmet_window: str | None,
                               fallback: tuple[str, ...] = ()) -> dict[str, Any]:
    """The SIGNED net rain-or-evaporation rate (mm/day) the deck carries.

    ``fallback`` is the declared ladder, walked in order: a real gridMET storm
    total for the window supersedes an explicit user rate. Evaporation is then
    subtracted (TELEMAC's single signed RAIN OR EVAPORATION keyword). A ``None``
    rate means no forcing was asked for, and the deck stays byte-identical.
    """
    return await asyncio.to_thread(
        _rain_forcing, rainfall_mm_per_day, evaporation_mm_per_day,
        gridmet_window, tuple(fallback))


def _rain_forcing(rainfall_mm_per_day: float | None,
                  evaporation_mm_per_day: float | None,
                  gridmet_window: str | None,
                  fallback: tuple[str, ...]) -> dict[str, Any]:
    rung: str | None = None
    rain: float | None = None
    note_bits: list[str] = []
    if gridmet_window is not None and str(gridmet_window).strip():
        start_date, end_date = _parse_gridmet_window(gridmet_window)
        rain = _gridmet_domain_mean_pr(_domain_bbox(), start_date, end_date)
        rung = "gridmet_domain_mean"
        note_bits.append(
            f"gridMET pr domain-mean {rain:.1f} mm/day ({start_date}..{end_date})")
    elif rainfall_mm_per_day is not None:
        rain = float(rainfall_mm_per_day)
        rung = "user_rate"
        note_bits.append(f"rainfall {rain:.1f} mm/day (user)")

    evap: float | None = None
    if evaporation_mm_per_day is not None:
        evap = float(evaporation_mm_per_day)
        rung = rung or "user_rate"
        note_bits.append(f"evaporation {evap:.1f} mm/day")

    if rain is None and evap is None:
        return {"mm_per_day": None, "note": None, "rung": None,
                "ladder": list(fallback)}
    net = float(min(max((rain or 0.0) - (evap or 0.0),
                        _NET_RAIN_MIN_MM_DAY), _NET_RAIN_MAX_MM_DAY))
    note = "; ".join(note_bits) + f" -> net {net:+.1f} mm/day (distributed on-mesh)"
    logger.info("telemac rainfall forcing: %s", note)
    return {"mm_per_day": net, "note": note, "rung": rung, "ladder": list(fallback)}


async def resolve_carrier_discharge(*, seed: dict[str, Any],
                                    explicit: float | None) -> dict[str, Any]:
    """The reach CARRIER discharge (m3/s) - real NWM streamflow, or a typed gate.

    The carrier discharge governs dilution and transport. An explicit value
    short-circuits the fetch; otherwise the NHDPlus reach nearest the seed in the
    NOAA National Water Model is the carrier. A fetch/read miss REFUSES typed
    naming ``discharge_m3s`` - it is never reverted to a baked constant.
    """
    seed_lon, seed_lat = float(seed["lon"]), float(seed["lat"])
    if explicit is not None:
        return {"m3s": float(explicit), "basis": "user", "real_source": None,
                "note": f"carrier discharge {float(explicit):.0f} m3/s (user-supplied)"}

    best_q = await asyncio.to_thread(_nwm_nearest_streamflow, seed_lon, seed_lat)
    if best_q is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DISCHARGE_INPUT_REQUIRED",
            "The NOAA National Water Model streamflow lookup found no carrier "
            "discharge for this river reach, so the discharge that governs "
            "dilution is not fabricated. Retry with an explicit discharge_m3s "
            "(steady upstream carrier discharge, m3/s) for the reach - or name a "
            "reach with NWM (CONUS) coverage.",
        )
    return {
        "m3s": round(best_q, 1), "basis": "fetched",
        "real_source": "fetch_noaa_nwm_streamflow (NOAA National Water Model)",
        "note": (f"carrier discharge {best_q:.0f} m3/s (NOAA National Water Model, "
                 "nearest reach to the seed)"),
    }


def CarrierDischarge(*, seed: Any, explicit: Any) -> Step:  # noqa: N802
    """The reach's carrier discharge. A STEP, not Data: it reads the resolved seed,
    which is a step result rather than a declaration a producer could name."""
    return Step(runner=f"{_STEPS}.forcing.resolve_carrier_discharge",
                kwargs={"seed": seed, "explicit": explicit})


def _nwm_nearest_streamflow(seed_lon: float, seed_lat: float) -> float | None:
    """Streamflow (m3/s) of the NWM reach nearest the seed, or None on any miss."""
    from trid3nt_server.data import TOOL_REGISTRY

    box = (seed_lon - _DISCHARGE_QUERY_HALF_DEG, seed_lat - _DISCHARGE_QUERY_HALF_DEG,
           seed_lon + _DISCHARGE_QUERY_HALF_DEG, seed_lat + _DISCHARGE_QUERY_HALF_DEG)
    try:
        layer = TOOL_REGISTRY["fetch_noaa_nwm_streamflow"].fn(bbox=box)
    except Exception as exc:  # noqa: BLE001 - a fetch miss => the typed gate above
        logger.info("telemac: NWM streamflow fetch failed for seed %s (%s)",
                    (seed_lon, seed_lat), exc)
        return None
    uri = getattr(layer, "uri", None) or (
        layer.get("uri") if isinstance(layer, dict) else None)
    if not uri:
        return None

    local: str | None = None
    try:
        import geopandas as gpd  # lazy: never imported on the offline path

        from trid3nt_server.data.simulation.solver.solver import (
            _get_s3_client,
            _split_object_uri,
        )

        _scheme, bucket, key = _split_object_uri(str(uri))
        fd, local = tempfile.mkstemp(prefix="nwm-",
                                     suffix=os.path.splitext(key)[1] or ".fgb")
        os.close(fd)
        resp = _get_s3_client().get_object(Bucket=bucket, Key=key)
        with open(local, "wb") as fh:
            fh.write(resp["Body"].read())
        gdf = gpd.read_file(local, engine="pyogrio")
    except Exception as exc:  # noqa: BLE001 - a read miss => the typed gate above
        logger.info("telemac: could not read NWM streamflow layer %s (%s)", uri, exc)
        return None
    finally:
        if local and os.path.exists(local):
            try:
                os.unlink(local)
            except OSError:
                pass

    best_q: float | None = None
    best_d = float("inf")
    for _idx, row in gdf.iterrows():
        try:
            q = float(row["streamflow_cms"])
        except (KeyError, TypeError, ValueError):
            continue
        geom = row.get("geometry")
        try:
            d = (float(geom.x) - seed_lon) ** 2 + (float(geom.y) - seed_lat) ** 2
        except Exception:  # noqa: BLE001
            d = 0.0
        if d < best_d and q > 0.0:
            best_d, best_q = d, q
    return best_q
