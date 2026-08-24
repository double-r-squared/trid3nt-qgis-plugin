"""The RTK-RDII step family: the closed form, the native deck, the comparison.

Three declared phases. The CLOSED FORM is the method under test (triangular unit
hydrographs convolved with the hyetograph); the NATIVE DECK is the authority it
is checked against (a real SWMM 5 ``[HYDROGRAPHS]``/``[RDII]`` deck through the
engine); the METRICS step is the comparison plus the direct-runoff reference the
question actually asks about.

Both halves are plan nodes rather than one composite, so the ledger can replay
the solve while the comparison re-executes, and a solver failure is a named step
failing rather than a best-effort branch quietly reporting nothing.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Sequence

from trid3nt_server.declarative import Step

from trid3nt_server.workflows.swmm.steps import (
    clock,
    line_chart_spec,
    peak,
    timeseries_block,
)

__all__ = [
    "ClosedForm",
    "Deck",
    "Metrics",
    "build_rdii_chart",
    "build_rtk_rdii_inp",
    "closed_form_rdii",
    "rdii_hydrograph",
    "rdii_metrics",
    "rdii_volume_cf",
    "rtk_expected_volume_cf",
    "rtk_unit_hydrograph",
    "write_rtk_rdii_deck",
]

logger = logging.getLogger("trid3nt_server.workflows.swmm.rdii_rtk.steps")

_STEPS = "trid3nt_server.workflows.swmm.rdii_rtk.steps"

#: 1 acre-inch per hour = 1.008389 cfs. A UNIT CONVERSION, not a scenario value:
#: it is the definition SWMM's own RDII implementation uses.
_ACRE_IN_PER_HR_TO_CFS = 1.008389

#: The node the ``[RDII]`` inflow is assigned to in the cross-check deck.
NODE = "N1"


class ClosedForm:
    """The RTK method itself, evaluated in closed form. Pure, no engine."""

    @staticmethod
    def rtk(**kwargs: Any) -> Step:
        """Convolve the declared hyetograph with the summed RTK unit hydrographs."""
        return Step(runner=f"{_STEPS}.closed_form_rdii", kwargs=kwargs)


class Deck:
    """SWMM deck writers. One constructor per question the family answers."""

    @staticmethod
    def rtk_rdii(**kwargs: Any) -> Step:
        """Author the native ``[HYDROGRAPHS]``/``[RDII]`` cross-check deck."""
        return Step(runner=f"{_STEPS}.write_rtk_rdii_deck", kwargs=kwargs)


class Metrics:
    """Answer extraction from the solved series - never a second physics model."""

    @staticmethod
    def rdii(**kwargs: Any) -> Step:
        """RDII vs direct runoff at the node, with both validation checks."""
        return Step(runner=f"{_STEPS}.rdii_metrics", kwargs=kwargs)


# --------------------------------------------------------------------------- #
# The RTK closed form (pure; the method under test)
# --------------------------------------------------------------------------- #
def rtk_unit_hydrograph(R: float, T: float, K: float, dt_hr: float,
                        area_ac: float) -> list[float]:
    """Triangular RTK unit-hydrograph ordinates (cfs per inch over the sewershed).

    Sampled at ``dt_hr`` from t=0 to the base ``T*(1+K)``. The peak is set so the
    triangle's area equals ``R * area_ac * 1 inch`` - the RTK volume identity,
    which is what makes R a VOLUME fraction rather than a shape knob.
    """
    tbase = T * (1.0 + K)
    qpeak = (2.0 * R * area_ac * 1.0 / tbase) * _ACRE_IN_PER_HR_TO_CFS
    n = int(round(tbase / dt_hr)) + 1
    ords: list[float] = []
    for i in range(n):
        t = i * dt_hr
        if t <= T:
            ords.append(qpeak * t / T if T > 0 else 0.0)
        elif t <= tbase:
            ords.append(qpeak * (tbase - t) / (K * T) if K * T > 0 else 0.0)
        else:
            ords.append(0.0)
    return ords


def rdii_hydrograph(uhs: Sequence[tuple[float, float, float]],
                    rain_in_per_step: Sequence[float], dt_hr: float,
                    area_ac: float) -> list[float]:
    """RDII inflow (cfs) = rainfall DEPTH per step convolved with the summed UHs."""
    import numpy as np

    rain = np.asarray(list(rain_in_per_step), dtype="float64")
    total = np.zeros(1)
    for (R, T, K) in uhs:
        q = np.asarray(rtk_unit_hydrograph(R, T, K, dt_hr, area_ac))
        conv = np.convolve(rain, q)
        length = max(len(total), len(conv))
        acc = np.zeros(length)
        acc[: len(total)] += total
        acc[: len(conv)] += conv
        total = acc
    return [float(x) for x in total]


def rdii_volume_cf(rdii_cfs: Sequence[float], dt_hr: float) -> float:
    return sum(rdii_cfs) * dt_hr * 3600.0


def rtk_expected_volume_cf(uhs: Sequence[tuple[float, float, float]],
                           rain_depth_in: float, area_ac: float) -> float:
    """The RTK volume identity: RDII volume = sum(R) * rain depth * area."""
    sum_r = sum(R for R, _, _ in uhs)
    return sum_r * (rain_depth_in / 12.0) * area_ac * 43560.0  # cubic feet


async def closed_form_rdii(
    *,
    R1: float, T1: float, K1: float,
    R2: float, T2: float, K2: float,
    R3: float, T3: float, K3: float,
    sewershed_area_ac: float,
    rainfall_depth_in: float,
    storm_duration_hr: float,
    dt_min: int,
    rainfall_series_in_per_hr: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the RTK closed form and hand back the forcing the deck must match.

    A unit hydrograph with any of R/T/K at zero is DROPPED rather than treated as
    a degenerate triangle - setting R to zero is how the declaration says "two
    unit hydrographs, not three".
    """
    from trid3nt_server.workflows.swmm.steps.errors import SwmmDeckError

    uhs = [(float(R1), float(T1), float(K1)),
           (float(R2), float(T2), float(K2)),
           (float(R3), float(T3), float(K3))]
    uhs = [(R, T, K) for (R, T, K) in uhs if R > 0.0 and T > 0.0 and K > 0.0]
    if not uhs:
        raise SwmmDeckError(
            "no RTK unit hydrograph is active: at least one of the three needs "
            "R, T and K all above zero.",
            error_code="SWMM_RDII_RTK_INVALID",
        )

    dt_min = int(dt_min)
    dt_hr = dt_min / 60.0
    steps_per_hr = max(int(round(60 / dt_min)), 1)
    explicit = _hourly_series(rainfall_series_in_per_hr)

    if explicit is not None:
        # An explicit HOURLY hyetograph: each hourly depth (inches) is also the
        # intensity in in/hr, held constant across that hour's substeps.
        rain_intensity_in_hr: list[float] = []
        rain_in_per_step: list[float] = []
        for hourly_in in explicit:
            rain_intensity_in_hr += [hourly_in] * steps_per_hr
            rain_in_per_step += [hourly_in * dt_hr] * steps_per_hr
        depth = sum(explicit)
    else:
        # A uniform design storm: constant intensity over the duration, then dry.
        duration = max(float(storm_duration_hr), dt_hr)
        depth = float(rainfall_depth_in)
        intensity = depth / duration
        n_storm = max(int(round(duration / dt_hr)), 1)
        rain_intensity_in_hr = [intensity] * n_storm
        rain_in_per_step = [intensity * dt_hr] * n_storm

    rdii = rdii_hydrograph(uhs, rain_in_per_step, dt_hr, sewershed_area_ac)
    times_hr = [i * dt_hr for i in range(len(rdii))]
    volume = rdii_volume_cf(rdii, dt_hr)
    expected = rtk_expected_volume_cf(uhs, depth, sewershed_area_ac)
    return {
        "uhs": uhs,
        "sum_R": sum(R for R, _, _ in uhs),
        "rainfall_depth_in": depth,
        "rain_intensity_in_hr": rain_intensity_in_hr,
        "times_hr": times_hr,
        "rdii_cfs": rdii,
        "rdii_volume_cf": volume,
        "rtk_volume_identity_ratio": (volume / expected) if expected > 0 else 0.0,
        "sim_hours": times_hr[-1] if times_hr else 24.0,
    }


def _hourly_series(series: Any) -> list[float] | None:
    """An explicit hourly hyetograph as bare depths; ``None`` when not supplied.

    Malformed input REFUSES rather than reverting to the declared design storm -
    silently modelling a different storm than the caller asked for is the swallow
    class this library exists to remove.
    """
    from trid3nt_server.workflows.swmm.steps.errors import SwmmDeckError

    if not series:
        return None
    try:
        return [float(v) for v in series]
    except (TypeError, ValueError) as exc:
        raise SwmmDeckError(
            f"rainfall_series_in_per_hr is not a list of hourly depths (in): {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# The native SWMM 5 cross-check deck
# --------------------------------------------------------------------------- #
async def write_rtk_rdii_deck(
    *,
    uhs: Sequence[Sequence[float]],
    rain_intensity_in_hr: Sequence[float],
    dt_min: int,
    sewershed_area_ac: float,
    sim_hours: float,
) -> dict[str, Any]:
    """Author the cross-check deck for the SAME R/T/K and the SAME rainfall."""
    inp = build_rtk_rdii_inp(
        [(float(R), float(T), float(K)) for R, T, K in uhs],
        list(rain_intensity_in_hr), int(dt_min), float(sewershed_area_ac),
        float(sim_hours),
    )
    return {"inp_text": inp, "node": NODE}


def build_rtk_rdii_inp(
    uhs: Sequence[tuple[float, float, float]],
    rain_intensity_in_hr: Sequence[float],
    dt_min: int,
    area_ac: float,
    sim_hours: float,
) -> str:
    """A minimal SWMM 5 deck: an RTK ``[HYDROGRAPHS]`` set + an ``[RDII]`` inflow.

    The drainage scaffolding that carries the answer OUT - two junctions, an
    outfall, two 400 ft circular pipes - is fixed: it is the schematic that lets
    the node inflow be read, not a property of any sewershed. An unused unit
    hydrograph is written with R=0 so SWMM parses three rows either way.
    """
    (R1, T1, K1) = uhs[0]
    (R2, T2, K2) = uhs[1] if len(uhs) > 1 else (0.0, 1.0, 1.0)
    (R3, T3, K3) = uhs[2] if len(uhs) > 2 else (0.0, 1.0, 1.0)
    ts = timeseries_block(
        "TS_RAIN",
        [(clock(i * dt_min), inten) for i, inten in enumerate(rain_intensity_in_hr)],
        precision=5,
    )
    end_h = int(math.ceil(sim_hours))
    return f"""[TITLE]
RTK RDII cross-check (row 4)

[OPTIONS]
FLOW_UNITS CFS
INFILTRATION HORTON
FLOW_ROUTING KINWAVE
START_DATE 01/01/2020
START_TIME 00:00:00
END_DATE 01/0{1 + max(1, end_h // 24 + 1)}/2020
END_TIME 00:00:00
REPORT_STEP 00:{dt_min:02d}:00
WET_STEP 00:{dt_min:02d}:00
DRY_STEP 00:{dt_min:02d}:00
ROUTING_STEP {dt_min * 60}

[RAINGAGES]
RG1 INTENSITY 0:{dt_min:02d} 1.0 TIMESERIES TS_RAIN

[HYDROGRAPHS]
UH1 RG1
UH1 ALL SHORT {R1} {T1} {K1} 0 0 0
UH1 ALL MEDIUM {R2} {T2} {K2} 0 0 0
UH1 ALL LONG {R3} {T3} {K3} 0 0 0

[RDII]
{NODE} UH1 {area_ac}

[JUNCTIONS]
{NODE} 10.0 0 0 0 0
N2 9.0 0 0 0 0

[OUTFALLS]
OUT 0.0 FREE NO

[CONDUITS]
C1 {NODE} N2 400 0.01 0 0 0 0
C2 N2 OUT 400 0.01 0 0 0 0

[XSECTIONS]
C1 CIRCULAR 3 0 0 0 1
C2 CIRCULAR 3 0 0 0 1

[TIMESERIES]
{ts}

[REPORT]
INPUT NO
NODES ALL
LINKS ALL
"""


# --------------------------------------------------------------------------- #
# The answer
# --------------------------------------------------------------------------- #
async def rdii_metrics(
    *,
    closed_form: dict[str, Any],
    solved: dict[str, Any],
    node: str,
    sewershed_area_ac: float,
    direct_runoff_coeff: float,
) -> dict[str, Any]:
    """RDII vs direct runoff at the node, plus the two validation checks.

    The direct-runoff reference is the RATIONAL METHOD (Q = C i A), which is
    sharp and in phase with the storm - the contrast that makes the RDII tail the
    point of the chart.
    """
    times_hr = list(closed_form["times_hr"])
    rdii = list(closed_form["rdii_cfs"])
    intensity = list(closed_form["rain_intensity_in_hr"])
    coeff = float(direct_runoff_coeff)

    runoff = [coeff * intensity[i] * sewershed_area_ac if i < len(intensity) else 0.0
              for i in range(len(rdii))]
    rdii_peak, _ = peak(rdii)
    runoff_peak, _ = peak(runoff)
    total_peak = rdii_peak + runoff_peak
    rdii_frac = (rdii_peak / total_peak) if total_peak > 0 else 0.0

    swmm_series = list(solved["nodes"][node]["total_inflow"])
    swmm_peak, _ = peak(swmm_series)
    swmm_ratio = (swmm_peak / rdii_peak) if rdii_peak > 0 else None
    identity = float(closed_form["rtk_volume_identity_ratio"])

    logger.info(
        "swmm RTK RDII: sum_R=%.3f area=%.0f ac peak=%.3f cfs vol_identity=%.5f "
        "swmm_peak=%.3f ratio=%s routing_continuity=%.3f%%",
        closed_form["sum_R"], sewershed_area_ac, rdii_peak, identity, swmm_peak,
        None if swmm_ratio is None else round(swmm_ratio, 4),
        solved["flow_routing_error_pct"],
    )
    return {
        "sum_R": round(float(closed_form["sum_R"]), 4),
        "sewershed_area_ac": float(sewershed_area_ac),
        "rainfall_depth_in": float(closed_form["rainfall_depth_in"]),
        "rdii_peak_cfs": round(rdii_peak, 4),
        "rdii_volume_cf": round(float(closed_form["rdii_volume_cf"]), 1),
        "rtk_volume_identity_ratio": round(identity, 5),
        "swmm_rdii_peak_cfs": round(swmm_peak, 4),
        "swmm_vs_closed_form_peak_ratio": (round(swmm_ratio, 4)
                                           if swmm_ratio is not None else None),
        "direct_runoff_peak_cfs": round(runoff_peak, 4),
        "rdii_fraction_of_total": round(rdii_frac, 4),
        "flow_routing_error_pct": round(float(solved["flow_routing_error_pct"]), 4),
        "curves": {
            "times_hr": [round(t, 3) for t in times_hr],
            "rdii_cfs": [round(x, 4) for x in rdii],
            "runoff_cfs": [round(x, 4) for x in runoff],
        },
    }


def build_rdii_chart(*, result: Any, params: Any) -> dict[str, Any] | None:
    """The RDII hydrograph against direct runoff at the node.

    Honest engine output only: the two series the metrics step already reported.
    ``None`` when there is nothing to draw.
    """
    curves = (result or {}).get("curves") or {}
    times = curves.get("times_hr") or []
    rdii = curves.get("rdii_cfs") or []
    runoff = curves.get("runoff_cfs") or []
    if len(times) < 2 or len(rdii) != len(times) or len(runoff) != len(times):
        return None

    from trid3nt_server.data.processing.charts_common import build_chart_payload

    title = "RDII (RTK unit hydrograph) vs direct runoff at the node"
    spec = line_chart_spec(
        title=title,
        series={"RDII (RTK)": list(zip(times, rdii)),
                "direct runoff": list(zip(times, runoff))},
        x_title="time (hr)", y_title="flow (cfs)",
        x_field="t_hr", y_field="flow_cfs", x_round=3, y_round=None,
    )
    if spec is None:
        return None

    ratio = result.get("swmm_vs_closed_form_peak_ratio")
    return build_chart_payload(
        vega_lite_spec=spec,
        title="RTK unit-hydrograph RDII vs direct runoff at the node",
        caption=(
            f"RTK RDII (sum R={result['sum_R']:.2f}) over "
            f"{result['sewershed_area_ac']:.0f} ac, "
            f"{result['rainfall_depth_in']:.2f} in storm: peak RDII "
            f"{result['rdii_peak_cfs']:.2f} cfs, "
            f"{result['rdii_fraction_of_total'] * 100:.0f}% of the node peak vs "
            f"direct runoff. Volume identity ratio "
            f"{result['rtk_volume_identity_ratio']:.4f}"
            + (f"; native SWMM peak ratio {ratio:.4f}." if ratio is not None else ".")
            + " EPA RTK method (Vallabhaneni et al. 2007)."
        ),
    )
