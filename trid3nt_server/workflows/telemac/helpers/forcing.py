"""Declared forcing DATA: the rain that falls, and the flow that carries.

Three producers rather than steps: they are artifacts fetched for the current
domain, and the plan Refs them into the sheet.

Neither ever invents a number. The rain producer walks a DECLARED LADDER (a real
gridMET storm total supersedes a user rate) and a gridMET failure REFUSES typed -
degrading a requested real storm to zero rain would be a silent no-rain solve.
The discharge producer refuses typed when the National Water Model has no
coverage (at ``event_time`` when one was asked for, else the latest cycle), so
the value that governs dilution is never a baked constant, and the cycle it
actually read is pinned onto the note/provenance rather than left as an
unpinned "latest".
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import tempfile
from typing import Any

from trid3nt_server.workflows.lib import RATE, Step, TemporalSpec, transform_value

from .errors import (
    RainOnGridError,
    TelemacDyeScenarioError,
    TelemacDyeScenarioInputError,
)

logger = logging.getLogger("trid3nt_server.workflows.telemac.helpers.forcing")

__all__ = ["CarrierDischarge", "ReviewResolvedInputs", "coerce_event_time",
           "event_time",
           "resolve_carrier_discharge", "resolve_rain_event",
           "resolve_rain_forcing", "review_resolved_inputs"]

#: Seconds in an hour, spelled once so no expression below spells it again.
_HOUR_S = 3600.0

_HELPERS = "trid3nt_server.workflows.telemac.helpers"

#: Half-width (deg) of the NWM query box centred on the reach seed. NWM is a
#: ~2.7M-reach point layer; a small box keeps it to a handful of reaches so the
#: nearest-to-seed pick lands on the carrier reach, not a distant tributary.
_DISCHARGE_QUERY_HALF_DEG: float = 0.03

#: Physically-sane band for the signed net rain-or-evaporation rate (a violent
#: storm ~500 mm/day; extreme PET ~20 mm/day), so a bad knob cannot destabilize
#: the solve.
_NET_RAIN_MIN_MM_DAY, _NET_RAIN_MAX_MM_DAY = -50.0, 2000.0

#: What the rain producer DELIVERS, which is what a declared ``.resample()`` /
#: ``.normalize()`` on the ``Data("rain")`` declaration is checked against. Both
#: rungs are daily-cadence rates: gridMET's aggregate is a daily field the router
#: time-reduces over the window, and a user rate is stated per day. TELEMAC's
#: single RAIN OR EVAPORATION keyword reads mm/day, so a declaration that asked
#: for anything else would be asking the run for a number it cannot carry.
_RAIN_NATIVE_INTERVAL, _RAIN_UNITS = "1D", "mm/day"


def _domain_bbox() -> tuple[float, float, float, float]:
    from trid3nt_server.workflows.lib import current_domain

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

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.solver.solver import _get_s3_client

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
                               temporal: TemporalSpec | None = None) -> dict[str, Any]:
    """The SIGNED net rain-or-evaporation rate (mm/day) the sheet carries.

    A dated gridMET window is the storm total for that window; without one, an
    explicit user rate. Evaporation is then subtracted (TELEMAC's single signed
    RAIN OR EVAPORATION keyword). A ``None`` rate means no forcing was asked for,
    and the steering file stays byte-identical.

    ``temporal`` is the declaration's own ``.resample()`` / ``.normalize()``,
    checked against the cadence and units this producer actually delivers; the
    transform it performs (or declines as unnecessary) is stamped onto the note.
    """
    return await asyncio.to_thread(
        _rain_forcing, rainfall_mm_per_day, evaporation_mm_per_day,
        gridmet_window, temporal)


def _rain_forcing(rainfall_mm_per_day: float | None,
                  evaporation_mm_per_day: float | None,
                  gridmet_window: str | None,
                  temporal: TemporalSpec | None = None) -> dict[str, Any]:
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
                "temporal_note": None}
    net = float(min(max((rain or 0.0) - (evap or 0.0),
                        _NET_RAIN_MIN_MM_DAY), _NET_RAIN_MAX_MM_DAY))
    if temporal is not None and temporal.units is not None \
            and temporal.units.units != _RAIN_UNITS:
        raise TelemacDyeScenarioInputError(
            f"Data('rain').normalize(units={temporal.units.units!r}) cannot be "
            f"honored: TELEMAC's RAIN OR EVAPORATION keyword reads {_RAIN_UNITS}."
        )
    # The clamp band above is stated in mm/day, so the declared transform runs
    # after it; the units assertion just above is what keeps the two agreeing.
    moved = transform_value(net, temporal, quantity=RATE, units=_RAIN_UNITS,
                            native=_RAIN_NATIVE_INTERVAL)
    net = float(moved.values)
    note = "; ".join(note_bits) + f" -> net {net:+.1f} mm/day (distributed on-mesh)"
    if temporal is not None:
        note = f"{note} [{moved.note}]"
    logger.info("telemac rainfall forcing: %s", note)
    return {"mm_per_day": net, "note": note, "rung": rung,
            "temporal_note": moved.note}


def coerce_event_time(value: Any) -> str | None:
    """A UTC ISO-8601 timestamp from a date/datetime wire value; ``None`` reads
    the MOST RECENT published NWM cycle.

    Accepts a bare date (midnight UTC) or a full ISO datetime, with or without a
    ``Z``/offset. A MALFORMED value REFUSES rather than silently falling back to
    the latest cycle - which discharge cycle governs dilution is a physically
    consequential choice, and silently reading a different one than the one
    asked for is the swallow class (the outfall-coordinate precedent). The NWM
    PDS bucket retains only the last ~30 days; a request outside that window
    still parses here and refuses later, typed, at the fetch itself.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = _dt.datetime.fromisoformat(iso)
    except ValueError:
        raise TelemacDyeScenarioError(
            "TELEMAC_PARAMS_INVALID",
            f"event_time={value!r} is not a parseable ISO-8601 date or datetime "
            "(e.g. '2026-08-20' or '2026-08-20T06:00:00Z'). Omit it to read the "
            "most recent published NWM cycle.",
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc).isoformat()


def event_time() -> Any:
    """A coercion that reads the wire's ``event_time`` into a pinned ISO cycle."""

    def _coerce(args: Any) -> dict[str, Any]:
        return {"event_time": coerce_event_time(args.get("event_time"))}

    _coerce.__name__ = "event_time"
    return _coerce


def _fmt_cycle(reference_time: str | None) -> str:
    """A short display form of a resolved cycle ISO string, for names/notes."""
    if not reference_time:
        return "unresolved cycle"
    try:
        dt = _dt.datetime.fromisoformat(str(reference_time).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%dT%H:%MZ")
    except ValueError:
        return str(reference_time)


async def resolve_carrier_discharge(*, seed: dict[str, Any],
                                    explicit: float | None,
                                    event_time: str | None = None) -> dict[str, Any]:
    """The reach CARRIER discharge (m3/s) - real NWM streamflow, or a typed gate.

    The carrier discharge governs dilution and transport. An explicit value
    short-circuits the fetch; otherwise the NHDPlus reach nearest the seed in
    the NOAA National Water Model is the carrier, read at ``event_time`` when
    set (else the most recent published cycle). A fetch/read miss REFUSES typed
    naming ``discharge_m3s`` - it is never reverted to a baked constant.

    The returned ``note`` (and ``reference_time``) PIN the cycle the fetch
    actually served, never the bare request word: a "latest" request resolves
    to a real timestamp before it ever reaches provenance or the run's metrics
    (replayability-by-declaration).
    """
    seed_lon, seed_lat = float(seed["lon"]), float(seed["lat"])
    if explicit is not None:
        return {"m3s": float(explicit), "basis": "user", "real_source": None,
                "reference_time": None, "product": None,
                "note": f"carrier discharge {float(explicit):.0f} m3/s (user-supplied)"}

    found = await asyncio.to_thread(_nwm_nearest_streamflow, seed_lon, seed_lat, event_time)
    if found is None:
        retention_txt = (
            f" for event_time={event_time!r} (the NWM PDS bucket retains only "
            "the last ~30 days; a request outside that window is not "
            "available - deeper history is a documented gap, not a source we "
            "carry)" if event_time else " for this reach"
        )
        raise TelemacDyeScenarioError(
            "TELEMAC_DISCHARGE_INPUT_REQUIRED",
            "The NOAA National Water Model streamflow lookup found no carrier "
            f"discharge{retention_txt}, so the discharge that governs dilution "
            "is not fabricated. Retry with an explicit discharge_m3s (steady "
            "upstream carrier discharge, m3/s) for the reach, a different "
            "event_time within the retention window, or omit event_time for "
            "the latest cycle.",
        )
    await _surface_discharge_station_layer(found.get("layer"))
    reference_time = found.get("reference_time")
    product = found.get("product") or "analysis_assim"
    cycle_txt = f"{product} @ {_fmt_cycle(reference_time)}"
    return {
        "m3s": round(found["m3s"], 1), "basis": "fetched",
        "real_source": "fetch_noaa_nwm_streamflow (NOAA National Water Model)",
        "reference_time": reference_time, "product": product,
        "note": (f"carrier discharge {found['m3s']:.0f} m3/s (NOAA National "
                 f"Water Model, nearest reach to the seed, {cycle_txt})"),
    }


def CarrierDischarge(*, seed: Any, explicit: Any, event_time: Any = None) -> Step:  # noqa: N802
    """The reach's carrier discharge. A STEP, not Data: it reads the resolved seed,
    which is a step result rather than a declaration a producer could name."""
    return Step(runner=f"{_HELPERS}.forcing.resolve_carrier_discharge", stage="acquire",
                kwargs={"seed": seed, "explicit": explicit, "event_time": event_time})


def ReviewResolvedInputs(*, carrier_discharge: Any,  # noqa: N802
                         workflow: str, input_mode: Any) -> Step:
    """Review the values the pipeline RESOLVED, before the expensive solve.

    ``self_gating``: the review is over values no plan-level form can show,
    because they do not exist until the fetch that produced them has run. A plan
    that declares this step may not also declare a FormGate - the validator
    refuses it, because a second card's edits would land on a sheet this review
    never reads.
    """
    return Step(runner=f"{_HELPERS}.forcing.review_resolved_inputs", stage="gates",
                self_gating=True,
                kwargs={"discharge": carrier_discharge,
                        "workflow": workflow, "input_mode": input_mode})


async def review_resolved_inputs(*, discharge: dict[str, Any],
                                 workflow: str,
                                 input_mode: str | None) -> dict[str, Any]:
    """Present the RESOLVED carrier discharge before the expensive solve.

    The carrier discharge governs dilution and is the physically dominant
    reviewable input, so ``user_gated`` pauses on it here - after the fetch that
    produced it and before the expensive solve. ``auto`` proceeds labeled.
    """
    from trid3nt_contracts.common import SyntheticInput as entry

    from trid3nt_server.gates.input_review import gate_input_review

    outcome = await gate_input_review(
        tool_name=workflow, mode=input_mode,
        entries=[
            entry(param="discharge_m3s", value=round(float(discharge["m3s"]), 2),
                  units="m^3/s", basis=discharge.get("basis") or "fetched",
                  real_source_if_any=(None if discharge.get("basis") == "user"
                                      else "NOAA National Water Model streamflow"),
                  note=discharge.get("note") or "carrier discharge governing dilution"),
        ],
        params={"discharge_m3s": float(discharge["m3s"])})
    if outcome.cancelled:
        raise TelemacDyeScenarioError("USER_INPUT_CANCELLED",
                                      f"{workflow} {outcome.cancel_reason}")
    revised = float(outcome.params.get("discharge_m3s", discharge["m3s"]))
    if revised != float(discharge["m3s"]):
        # A user-revised value is no longer the fetched cycle it started from -
        # the reference_time/product it carried would misdescribe this row.
        return {**discharge, "m3s": revised, "basis": "user", "real_source": None,
                "reference_time": None, "product": None,
                "note": f"carrier discharge {revised:.0f} m3/s (revised at review)"}
    return discharge


async def _surface_discharge_station_layer(layer: Any) -> None:
    """Publish the NWM point layer as a context input, its name PINNED to the
    cycle actually served - never the bare request word.

    ``_nwm_nearest_streamflow`` fetches with ``visualize=False`` (suppressing
    the generic auto-emission, which would only know the REQUESTED time, not
    the resolved one), so this is the only station layer that reaches the
    canvas: exactly one, honestly captioned. BEST-EFFORT (mirrors
    the input-surfacing floor): never raises, and a missing
    station layer never voids the discharge resolution.
    """
    if layer is None:
        return
    try:
        from trid3nt_server.emission.layer_uri_emit import publish_input_layer
        from trid3nt_server.emission.pipeline_emitter import current_emitter

        emitter = current_emitter()
        if emitter is None:
            return
        reference_time = getattr(layer, "reference_time", None)
        product = getattr(layer, "product", None) or "analysis_assim"
        station = layer.model_copy(update={
            "layer_id": f"input-nwm-streamflow-station-{layer.layer_id}",
            "name": f"Input: NWM discharge station ({product} @ "
                    f"{_fmt_cycle(reference_time)})",
            "role": "context", "bbox": None,
        })
        await publish_input_layer(emitter, station, role="context")
    except Exception as exc:  # noqa: BLE001 - input surfacing is NEVER fatal
        logger.warning("telemac: NWM station layer surfacing failed (non-fatal, "
                       "the discharge resolution is unaffected): %s", exc)


def _nwm_nearest_streamflow(seed_lon: float, seed_lat: float,
                            valid_time: str | None = None) -> dict[str, Any] | None:
    """The NWM reach nearest the seed: its discharge + the RESOLVED cycle served.

    ``None`` on any miss - offline, no NHDPlus coverage at the seed, or
    ``valid_time`` falling outside the ~30-day NWM PDS retention window. The
    typed gate in ``resolve_carrier_discharge`` narrates the difference.
    """
    from trid3nt_server.tools import TOOL_REGISTRY

    box = (seed_lon - _DISCHARGE_QUERY_HALF_DEG, seed_lat - _DISCHARGE_QUERY_HALF_DEG,
           seed_lon + _DISCHARGE_QUERY_HALF_DEG, seed_lat + _DISCHARGE_QUERY_HALF_DEG)
    try:
        # visualize=False: this is a probe fetch for ONE scalar, not the
        # engine's own input - resolve_carrier_discharge surfaces its own
        # cycle-pinned station layer once the resolved reference_time is known.
        layer = TOOL_REGISTRY["fetch_noaa_nwm_streamflow"].fn(
            bbox=box, valid_time=valid_time, visualize=False)
    except Exception as exc:  # noqa: BLE001 - a fetch miss => the typed gate above
        logger.info("telemac: NWM streamflow fetch failed for seed %s valid_time=%s "
                    "(%s)", (seed_lon, seed_lat), valid_time, exc)
        return None
    uri = getattr(layer, "uri", None) or (
        layer.get("uri") if isinstance(layer, dict) else None)
    if not uri:
        return None

    local: str | None = None
    try:
        import geopandas as gpd  # lazy: never imported on the offline path

        from trid3nt_server.workflows.solver.solver import (
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
    if best_q is None:
        return None
    return {
        "m3s": best_q,
        "reference_time": getattr(layer, "reference_time", None),
        "product": getattr(layer, "product", None) or "analysis_assim",
        "layer": layer,
    }


def _rain_window_bbox() -> tuple[float, float, float, float]:
    """The extent the hyetograph is fetched over: the bound domain's own."""
    from trid3nt_server.workflows.lib import current_domain

    domain = current_domain()
    if domain is None or domain.bbox is None:
        raise RainOnGridError(
            "the rain hyetograph cannot be fetched: no domain is bound. "
            "Resolve the AOI first.",
            error_code="TELEMAC_ROG_DOMAIN_UNBOUND")
    return tuple(float(v) for v in domain.bbox)  # type: ignore[return-value]


def resolve_rain_event(*, window: str | None, intensity_mm_per_hr: float,
                       storm_duration_hr: float,
                       sim_duration_hr: float | None) -> dict[str, Any]:
    """The storm, as either a real hourly hyetograph or a constant design rate.

    A BRANCH ON THE ASK, not a fallback ladder: a dated ``window`` fetches the
    hourly AORC accumulation over the catchment and the run is driven by the REAL
    intensity structure, which is what resolves the hydrograph SHAPE. With no
    window the storm is a constant design rate over a declared duration - a
    hypothetical, and the returned ``note`` labels it as one.

    AORC rather than MRMS despite the argument's history: MRMS only covers
    ~2020-10 onward, and a replication window that predates it would silently
    return nothing.
    """
    from trid3nt_server.tools import TOOL_REGISTRY

    if not window:
        return {
            "kind": "design_storm", "blocks": None, "series": None,
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
        raise RainOnGridError(
            f"the rain window must be 'start/end' dates; got {window!r}.",
            error_code="TELEMAC_ROG_BAD_WINDOW")
    start, end = [s.strip() for s in window.split(sep, 1)]
    payload = TOOL_REGISTRY["fetch_aorc_precip"].fn(
        bbox=[float(v) for v in bbox], start_date=start, end_date=end)
    payload = payload if isinstance(payload, dict) else getattr(payload, "__dict__", {})
    mm = [max(0.0, float(v)) for v in payload["precip_mm"]]
    if len(mm) < 2:
        raise RainOnGridError(
            f"AORC returned {len(mm)} hourly steps for {window!r}; a hyetograph "
            "needs at least two. Widen the window or run the design storm.",
            error_code="TELEMAC_ROG_EMPTY_HYETO")
    blocks = [[float((i + 1) * _HOUR_S), round(mm[i], 5)] for i in range(len(mm))]
    asked_s = float(sim_duration_hr or 0.0) * _HOUR_S
    span_s = float(len(mm) * _HOUR_S)
    return {
        "kind": "hyetograph", "blocks": blocks, "series": mm,
        "intensity_mm_per_hr": float(intensity_mm_per_hr),
        "duration_s": max(asked_s, span_s),
        "duration_basis": "user" if asked_s > span_s else "hyetograph",
        "window": window, "total_mm": round(sum(mm), 3),
        "note": (f"the REAL hourly AORC hyetograph over {window} - {len(mm)} steps, "
                 f"{sum(mm):.3g} mm total."),
    }
