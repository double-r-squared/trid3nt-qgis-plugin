"""The DO-sag step family: derivations, the WAQTEL-O2 reach solve, the sag chart.

Engine knowledge stays in the engine - the plan names these by dotted path, the
way a ``GateSpec`` names its estimate/pin providers.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.telemac_contracts import TelemacDoLayerURI

from trid3nt_server.workflows.lib import Step

__all__ = [
    "OutfallCoordsInvalidError",
    "ReachSolve",
    "build_sag_chart",
    "coerce_outfall_point",
    "do_saturation_mgl",
    "upstream_do_mgl",
]

logger = logging.getLogger("trid3nt_server.workflows.telemac.do_sag.steps")


class OutfallCoordsInvalidError(ValueError):
    """``outfall_coords`` was supplied but is not a usable (lon, lat) point."""


def coerce_outfall_point(value: Any) -> tuple[float, float] | None:
    """``(lon, lat)`` from a wire value; ``None`` only when nothing was supplied.

    A MALFORMED outfall refuses rather than falling back to the derived reach
    point: silently modelling a different discharge location than the one asked
    for is the swallow class.
    """
    if value is None:
        return None
    try:
        lon, lat = (float(v) for v in tuple(value))  # type: ignore[misc]
    except (TypeError, ValueError):
        raise OutfallCoordsInvalidError(
            f"outfall_coords={value!r} is not a (lon, lat) pair. Supply the "
            "discharge point as two numbers in EPSG:4326, or omit it."
        ) from None
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise OutfallCoordsInvalidError(
            f"outfall_coords=({lon}, {lat}) is off the earth; it is (lon, lat) in "
            "EPSG:4326, longitude first."
        )
    return (lon, lat)


def do_saturation_mgl(params: Any) -> float:
    """Freshwater DO saturation Cs (mg/L) from water temperature (Elmore-Hayes, 1 atm).

    A narrated literature relation, not a site value; ~9.0 mg/L at 20 C.
    """
    t = max(0.0, min(40.0, float(params.water_temp_c)))
    return round(14.652 - 0.41022 * t + 0.0079910 * t * t - 0.000077774 * t ** 3, 3)


def upstream_do_mgl(params: Any) -> float:
    """Inflow DO when none is supplied: a stream at saturation upstream of the sag."""
    return float(params.do_saturation_mgl)


class ReachSolve:
    """TELEMAC-2D reach solves. One constructor per water-quality process."""

    @staticmethod
    def telemac_waqtel_o2(**kwargs: Any) -> Step:
        """The reach pipeline under WAQTEL O2: geocode -> banks -> mesh -> solve -> DO field.

        ``self_gating``: the composite runs its OWN input review over the values it
        resolves (the NWM carrier discharge, the bank source) - values no plan-level
        form can show, because they do not exist until the composite has fetched
        them. A plan may not put a second form gate in front of it.
        """
        return Step(
            runner="trid3nt_server.workflows.telemac.do_sag.steps.solve_waqtel_o2",
            kwargs=kwargs, consequential=True, self_gating=True,
        )


async def solve_waqtel_o2(
    *,
    location: str | None,
    bbox: tuple[float, float, float, float] | None,
    discharge_bod_mgl: float,
    upstream_do_mgl: float,
    do_saturation_mgl: float,
    water_temp_c: float,
    do_standard_mgl: float,
    k1_per_day: float,
    k2_per_day: float,
    reach_length_km: float,
    channel_width_m: float,
    sim_duration_s: float,
    discharge_m3s: float | None,
    mesh_resolution: str,
    mesh_resolution_m: float | None,
    bank_source: str,
    compute_class: str,
    outfall_coords: tuple[float, float] | list[float] | None = None,
    event_time: str | None = None,
    input_mode: str | None = None,
) -> TelemacDoLayerURI:
    """Solve the reach with WAQTEL O2 coupled and return the published DO-field layer.

    Composes the SHARED TELEMAC step family directly - geocode, flowline, seed,
    carrier discharge, deck, solve, products - rather than delegating to another
    template's plan. The review below is the composite's own: it presents the
    values this pipeline RESOLVED (the carrier discharge, the bank source), which
    no plan-level form could show because they do not exist until the fetch has
    run.
    """
    from trid3nt_server.workflows.lib import Domain
    from trid3nt_server.workflows.lib.domain import bind_domain, reset_domain
    from trid3nt_server.workflows.telemac.steps import (
        fetch_reach_flowline,
        geocode_reach,
        normalize_bank_source,
        publish_do_products,
        reach_seed,
        resolve_carrier_discharge,
        solve_reach,
        write_reach_deck,
    )

    # DO cannot ride in above its own saturation - a physics coupling between two
    # params, so it cannot be a declared static bound.
    up_do = min(max(float(upstream_do_mgl), 0.0), float(do_saturation_mgl))
    if up_do != float(upstream_do_mgl):
        logger.info("do_sag upstream_do_mgl %.3g pinned to saturation %.3g mg/L",
                    upstream_do_mgl, do_saturation_mgl)

    outfall = coerce_outfall_point(outfall_coords)
    do_sag_config = {
        "bod_mgl": float(discharge_bod_mgl),
        "upstream_do_mgl": up_do,
        "saturation_mgl": float(do_saturation_mgl),
        "water_temp_c": float(water_temp_c),
        "k1_per_day": float(k1_per_day),
        "k2_per_day": float(k2_per_day),
        "k2_formula": 0,      # constant k2 (the S-P idealization; the user sets k2)
        "standard_mgl": float(do_standard_mgl),
    }

    reach = await geocode_reach(location=location, bbox=bbox)
    token = bind_domain(Domain(bbox=reach["bbox"], label=reach["name"]))
    try:
        rivers = await fetch_reach_flowline(prefetched=None)
        seed = await reach_seed(reach=reach, rivers=rivers)
        discharge = await resolve_carrier_discharge(seed=seed, explicit=discharge_m3s,
                                                    event_time=event_time)
        logger.info("do_sag: %s (seed=%.5f,%.5f)", discharge["note"],
                    seed["lon"], seed["lat"])

        discharge = await _review_resolved_inputs(
            discharge, bank_source=bank_source, input_mode=input_mode)

        deck = await write_reach_deck(
            reach=reach, seed=seed, carrier_discharge=discharge, rain=None,
            reach_seed_coords=list(outfall) if outfall else None,
            reach_length_km=float(reach_length_km),
            channel_width_m=float(channel_width_m),
            sim_duration_s=float(sim_duration_s),
            mesh_resolution=str(mesh_resolution or "auto"),
            mesh_resolution_m=mesh_resolution_m,
            bank_source=normalize_bank_source(bank_source),
            do_sag_config=do_sag_config)
        solve = await solve_reach(deck=deck, compute_class=compute_class)
        return await publish_do_products(deck=deck, solve=solve,
                                         do_sag_config=do_sag_config,
                                         carrier_discharge=discharge)
    finally:
        reset_domain(token)


async def _review_resolved_inputs(discharge: dict[str, Any], *, bank_source: Any,
                                  input_mode: str | None) -> dict[str, Any]:
    """Present the RESOLVED carrier discharge + bank source before the solve.

    The carrier discharge governs dilution and is the physically dominant
    reviewable input, so ``user_gated`` pauses on it here - after the fetch that
    produced it and before the expensive solve. ``auto`` proceeds labeled.
    """
    from trid3nt_contracts.common import SyntheticInput as entry

    from trid3nt_server.gates.input_review import gate_input_review
    from trid3nt_server.workflows.telemac.steps import (
        TelemacDyeScenarioError,
        normalize_bank_source,
    )

    banks = normalize_bank_source(bank_source)
    outcome = await gate_input_review(
        tool_name="telemac_do_sag", mode=input_mode,
        entries=[
            entry(param="discharge_m3s", value=round(float(discharge["m3s"]), 2),
                  units="m^3/s", basis=discharge.get("basis") or "fetched",
                  real_source_if_any=(None if discharge.get("basis") == "user"
                                      else "NOAA National Water Model streamflow"),
                  note=discharge.get("note") or "carrier discharge governing dilution"),
            entry(param="bank_source", value=banks,
                  basis="fetched" if banks == "nhd_area" else "default_demo",
                  consequence="physics",
                  note=("real NHDArea banks" if banks == "nhd_area"
                        else "assumed constant-width ribbon")),
        ],
        params={"discharge_m3s": float(discharge["m3s"])})
    if outcome.cancelled:
        raise TelemacDyeScenarioError("USER_INPUT_CANCELLED",
                                      f"telemac_do_sag {outcome.cancel_reason}")
    revised = float(outcome.params.get("discharge_m3s", discharge["m3s"]))
    if revised != float(discharge["m3s"]):
        # A user-revised value is no longer the fetched cycle it started from -
        # the reference_time/product it carried would misdescribe this row.
        return {**discharge, "m3s": revised, "basis": "user", "real_source": None,
                "reference_time": None, "product": None,
                "note": f"carrier discharge {revised:.0f} m3/s (revised at review)"}
    return discharge


def build_sag_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The DO-sag chart SPEC: DO + CBOD vs downstream distance, standard as a rule.

    Honest postprocess scalars off the published layer (the binned centerline
    curve), never a fabricated line; ``None`` when the curve is absent.
    """
    xs = getattr(result, "sag_curve_distance_m", None)
    do = getattr(result, "sag_curve_do_mgl", None)
    bod = getattr(result, "sag_curve_bod_mgl", None)
    if not xs or not do or len(xs) != len(do):
        return None
    std = float(getattr(result, "do_standard_mgl", None) or 5.0)

    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    do_vals = [{"x_km": round(xs[i] / 1000.0, 4), "v": do[i], "series": "Dissolved O2"}
               for i in range(len(xs))]
    bod_vals = ([{"x_km": round(xs[i] / 1000.0, 4), "v": bod[i], "series": "CBOD"}
                 for i in range(len(xs))] if bod and len(bod) == len(xs) else [])
    vega_lite_spec = {
        "layer": [
            {"mark": {"type": "line", "point": False},
             "data": {"values": do_vals + bod_vals},
             "encoding": {
                 "x": {"field": "x_km", "type": "quantitative",
                       "title": "Downstream distance (km)"},
                 "y": {"field": "v", "type": "quantitative",
                       "title": "Concentration (mg/L)"},
                 "color": {"field": "series", "type": "nominal", "title": None}}},
            {"mark": {"type": "rule", "strokeDash": [6, 4], "color": "#c0392b"},
             "data": {"values": [{"y": std}]},
             "encoding": {"y": {"field": "y", "type": "quantitative"}}},
        ]
    }
    dmin = getattr(result, "do_min_mgl", None)
    dloc = getattr(result, "do_min_distance_m", None)
    verdict = "violates" if getattr(result, "do_violates_standard", False) else "meets"
    # With no location words the LAYER's own name is the title: it already reads
    # "Dissolved oxygen sag (<reach>)", so prefixing it would say it twice.
    where = params.get("location")
    title = (f"Dissolved-oxygen sag - {where}" if where
             else (getattr(result, "name", None) or "Dissolved-oxygen sag"))
    return build_chart_payload(
        vega_lite_spec=vega_lite_spec,
        title=title,
        caption=(
            f"Streeter-Phelps DO sag: minimum {dmin} mg/L at {dloc} m downstream "
            f"({verdict} the {std:g} mg/L standard, dashed). CBOD decay drives the "
            f"sag; reaeration recovers it. Screening/permit grade."
        ),
    )
