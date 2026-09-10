"""SCS curve-number infiltration for the TELEMAC-2D rain-on-grid template.

The rainfall-excess transform, the steep-slope correction, the AMC conversions
and a land-cover -> (curve number, Manning n) table keyed to NLCD classes. Every
function is pure: nothing here touches the mesh, the deck or the network."""

# Godara, Bruland and Alfredsen (2024, Front. Water 6:1384205) is the source of
# the surface: the SCS-CN rainfall-excess transform (eq 7-8), the steep-slope CN
# correction (eq 9) and the Table-1 land-cover analog. GCN250 (Jaafar 2019),
# fetched directly, bypasses the class-to-CN table and samples CN2 straight off
# the raster.
#
# TWO CONSUMERS, ONE MODULE.
#   * NATIVE runoff path - TELEMAC v9.0.0 carries the SCS-CN runoff model
#     natively (``runoff_scs_cn.f``, Ligier 2016; steering keyword
#     ``RAINFALL-RUNOFF MODEL = 1`` plus ``ANTECEDENT MOISTURE CONDITIONS`` and
#     ``OPTION FOR INITIAL ABSTRACTION RATIO``), reading a per-node CN2 field
#     from FORMATTED DATA FILE 2. The engine's steep-slope correction is compiled
#     OFF in the installed 9.0.0 build (``STEEPSLOPECOR = .FALSE.``, a hardcoded
#     flag rather than a keyword), so a requested correction is applied to the CN
#     field HERE, before the file is written.
#   * PREPROCESSING rainfall-excess path - the engine's native rain is a single
#     constant intensity over the rain duration, also hardcoded, so a
#     time-varying hyetograph cannot drive the native CN model. The SCS-CN
#     transform is applied up front instead, yielding a net series fed as
#     time-varying rain with ``RAINFALL-RUNOFF MODEL = 0``.

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = [
    "amc_condition_for",
    "scs_potential_retention_mm",
    "scs_runoff_mm",
    "huang_steep_slope_cn",
    "paper_exponential_steep_slope_cn",
    "amc_convert_cn",
    "landcover_cn_manning",
    "node_curve_numbers",
    "rainfall_excess_hyetograph",
    "select_runoff_path",
    "RunoffPathDecision",
    "NLCD_CN_MANNING",
    "CNInfiltrationError",
]


class CNInfiltrationError(ValueError):
    """Invalid CN-infiltration input (out-of-range CN, unknown AMC, etc.)."""


# ---------------------------------------------------------------------------
# Land-cover -> (CN2, Manning n) table -- paper Table 1 analog.
#
# Godara et al. Table 1 lists CN + Manning per land-cover class for the T2D
# model (bare rock, forest, open land, marsh, river, urban). Those classes are
# mapped here onto the NLCD 2019/2021 legend so ``fetch_landcover`` drives
# the field directly. CN values are the paper's T2D column for the mid hydrologic
# soil group (HSG B); the true CN depends on soil group and is a calibration
# lever rather than a fixed constant. Manning n is the paper T2D column verbatim.
# ---------------------------------------------------------------------------

# ONE STUDY, BOTH COLUMNS. The curve number and the roughness are read off the
# same paper's same table for the same class, so a run's infiltration and its
# friction are parameterised together. Substituting another published roughness
# table - the domain library ships one that agrees with this on none of the 17
# shared codes, 5-8x rougher on open vegetated land and 40x smoother on water -
# would mix two calibrations in one field and MOVE every number this template
# has already produced. That is an author's declared choice, never a shim: swap
# the pair, or carry both as a declared lever, but never half of one.
#
# nlcd_code -> (curve_number_amc2, manning_n, paper_class_label)
NLCD_CN_MANNING: dict[int, tuple[float, float, str]] = {
    11: (100.0, 0.040, "river/open-water"),   # Open Water
    12: (100.0, 0.040, "river/open-water"),   # Perennial Ice/Snow
    21: (75.0, 0.050, "open-land"),           # Developed, Open Space
    22: (89.0, 0.100, "urban"),               # Developed, Low Intensity
    23: (89.0, 0.100, "urban"),               # Developed, Medium Intensity
    24: (89.0, 0.100, "urban"),               # Developed, High Intensity
    31: (85.0, 0.020, "bare-rock/scarce-veg"),  # Barren Land
    41: (80.0, 0.200, "forest"),              # Deciduous Forest
    42: (80.0, 0.200, "forest"),              # Evergreen Forest
    43: (80.0, 0.200, "forest"),              # Mixed Forest
    51: (75.0, 0.050, "open-land"),           # Dwarf Scrub
    52: (75.0, 0.050, "open-land"),           # Shrub/Scrub
    71: (75.0, 0.050, "open-land"),           # Grassland/Herbaceous
    72: (75.0, 0.050, "open-land"),           # Sedge/Herbaceous
    81: (80.0, 0.050, "open-land"),           # Pasture/Hay
    82: (80.0, 0.050, "open-land"),           # Cultivated Crops
    90: (90.0, 0.200, "marsh"),               # Woody Wetlands
    95: (90.0, 0.200, "marsh"),               # Emergent Herbaceous Wetlands
}

#: Fallback when an NLCD code is unmapped -- the "open land" row (moderate CN,
#: moderate roughness). Never silently a low CN (which would over-produce runoff)
#: nor 100 (which would zero infiltration).
_DEFAULT_CN_MANNING: tuple[float, float, str] = (75.0, 0.050, "open-land")


def landcover_cn_manning(nlcd_code: int) -> tuple[float, float, str]:
    """Return ``(CN2, Manning n, class_label)`` for an NLCD land-cover code.

    The CN is the AMC-II value and an unknown code falls back to open land."""
    return NLCD_CN_MANNING.get(int(nlcd_code), _DEFAULT_CN_MANNING)


# ---------------------------------------------------------------------------
# SCS-CN rainfall-excess (paper eq 7-8).
# ---------------------------------------------------------------------------


def scs_potential_retention_mm(cn: float) -> float:
    """Potential maximum retention S (mm): ``S = 25400 / CN - 254``.

    CN must be in (0, 100]."""
    cn = float(cn)
    if not (0.0 < cn <= 100.0):
        raise CNInfiltrationError(f"curve number must be in (0, 100]; got {cn}")
    return 25400.0 / cn - 254.0


def scs_runoff_mm(rainfall_mm: float, cn: float, ia_ratio: float = 0.2) -> float:
    """Direct runoff Q (mm) from CUMULATIVE event rainfall P via SCS-CN.

    ``Q = (P - Ia)^2 / (P - Ia + S)`` for ``P > Ia`` else 0, ``Ia = ia_ratio * S``."""
    p = float(rainfall_mm)
    if p < 0.0:
        raise CNInfiltrationError(f"rainfall must be >= 0; got {p}")
    s = scs_potential_retention_mm(cn)
    ia = float(ia_ratio) * s
    if p <= ia:
        return 0.0
    return (p - ia) ** 2 / (p - ia + s)


def rainfall_excess_hyetograph(
    rainfall_mm_series: list[float], cn: float, ia_ratio: float = 0.2
) -> list[float]:
    """Per-step excess (net) rainfall from a hyetograph via cumulative SCS-CN.

    Aligned with the input, non-negative, and summing to the SCS-CN runoff."""
    cum_p = 0.0
    cum_q = 0.0
    out: list[float] = []
    for incr in rainfall_mm_series:
        step = float(incr)
        if step < 0.0:
            raise CNInfiltrationError(f"hyetograph increments must be >= 0; got {step}")
        cum_p += step
        q_now = scs_runoff_mm(cum_p, cn, ia_ratio=ia_ratio)
        out.append(q_now - cum_q)
        cum_q = q_now
    return out


# ---------------------------------------------------------------------------
# Steep-slope CN correction (paper eq 9).
# ---------------------------------------------------------------------------


def huang_steep_slope_cn(cn2: float, slope_m_per_m: float) -> float:
    """Steep-slope-corrected CN2 via the Huang et al. (2006) formula, capped at 100.

    ``CN2a = CN2 * (322.79 + 15.63*a) / (a + 323.52)``, ``a`` (m/m) in [0.14, 1.4]."""
    # The exact formula the TELEMAC ``runoff_scs_cn.f`` steep-slope branch uses
    # (Huang, Gallichand, Wang, Goulet 2006, Hydrological Processes 20:579-589).
    cn2 = float(cn2)
    alpha = float(slope_m_per_m)
    cc_at_1_4 = (322.79 + 15.63 * 1.4) / (1.4 + 323.52)
    if alpha < 0.14:
        factor = 1.0
    elif alpha <= 1.4:
        factor = (322.79 + 15.63 * alpha) / (alpha + 323.52)
    else:
        factor = cc_at_1_4
    return min(100.0, cn2 * factor)


def paper_exponential_steep_slope_cn(cn2: float, slope_m_per_m: float) -> float:
    """The exponential steep-slope form as the paper prints it, capped at 100.

    ``CN_corr = CN2 * exp(0.0065 * slope)``, for paper reproduction only."""
    # The paper cites Huang 2006 but prints this simplified exponential; the
    # rational Huang form is what the engine implements and is the default.
    return min(100.0, float(cn2) * math.exp(0.0065 * float(slope_m_per_m)))


# ---------------------------------------------------------------------------
# Antecedent-moisture conversion (matches TELEMAC's runoff_scs_cn.f exactly).
# ---------------------------------------------------------------------------


#: The SCS antecedent-moisture words, and the condition each one names. Both the
#: wet/dry vocabulary a question is asked in and the I/II/III the literature uses
#: resolve to the integer the engine's ``ANTECEDENT MOISTURE CONDITIONS`` keyword
#: takes. ONE table, because a value that arrives typed and a value that arrives
#: through the form must mean the same condition.
_AMC_CONDITIONS: dict[str, int] = {
    "dry": 1, "i": 1, "1": 1,
    "normal": 2, "ii": 2, "2": 2,
    "wet": 3, "iii": 3, "3": 3,
}


def amc_condition_for(value: Any) -> int:
    """The SCS antecedent-moisture CONDITION (1/2/3) a declared word names.

    REFUSES an unknown word rather than seating AMC II under a wetter ask."""
    word = str(value).strip().lower()
    found = _AMC_CONDITIONS.get(word)
    if found is None:
        raise CNInfiltrationError(
            f"antecedent_moisture {value!r} is not an SCS condition; the three are "
            "'dry' (AMC I), 'normal' (AMC II) and 'wet' (AMC III).")
    return found


def amc_convert_cn(cn2: float, amc: int) -> float:
    """Convert a normal-condition CN2 to the AMC dry (I) / normal (II) / wet (III).

    For the PREPROCESSING path only; the native path lets the engine convert."""
    # Byte-for-byte the three formulas in TELEMAC's ``runoff_scs_cn.f``:
    #   AMC I (dry)   ``CN1 = 4.2*CN2 / (10 - 0.058*CN2)``
    #   AMC II (norm) ``CN2`` unchanged
    #   AMC III (wet) ``CN3 = 23*CN2 / (10 + 0.13*CN2)``
    cn2 = float(cn2)
    if amc == 1:
        return 4.2 * cn2 / (10.0 - 0.058 * cn2)
    if amc == 2:
        return cn2
    if amc == 3:
        return 23.0 * cn2 / (10.0 + 0.13 * cn2)
    raise CNInfiltrationError(f"AMC must be 1 (dry), 2 (normal) or 3 (wet); got {amc}")


# ---------------------------------------------------------------------------
# Per-node CN field builder (feeds TELEMAC FORMATTED DATA FILE 2 on the native
# path).
# ---------------------------------------------------------------------------


def node_curve_numbers(
    nlcd_codes: list[int],
    *,
    uniform_cn: float | None = None,
    slopes_m_per_m: list[float] | None = None,
    steep_slope_correction: bool = False,
) -> list[float]:
    """Build the per-node CN2 field for a mesh, as normal-AMC values.

    ``uniform_cn`` overrides the lookup; a correction needs ``slopes_m_per_m``."""
    n = len(nlcd_codes)
    if uniform_cn is not None:
        base = [float(uniform_cn)] * n
    else:
        base = [landcover_cn_manning(c)[0] for c in nlcd_codes]
    if not steep_slope_correction:
        return base
    if slopes_m_per_m is None or len(slopes_m_per_m) != n:
        raise CNInfiltrationError(
            "steep_slope_correction requires slopes_m_per_m aligned with nlcd_codes"
        )
    return [huang_steep_slope_cn(cn, s) for cn, s in zip(base, slopes_m_per_m)]


# ---------------------------------------------------------------------------
# Automatic CN-path selection (native constant vs native time-varying hyetograph).
#
# TELEMAC v9.0.0's native SCS-CN runoff model ships with RAINDEF hardcoded to 1
# (a single CONSTANT rain intensity). The installed source, however, already
# implements a block-type time-varying hyetograph under RAINDEF=3 (reading
# FORMATTED DATA FILE 1) -- so a per-case FORTRAN FILE that flips that one
# parameter (staged by the worker) unlocks the native model on a REAL
# hyetograph without an image rebuild. The template picks per run:
#   * CONSTANT-intensity rain (a design storm: one rate over a duration) -> the
#     NATIVE model (RAINFALL-RUNOFF MODEL=1 + ANTECEDENT MOISTURE CONDITIONS +
#     the FORMATTED DATA FILE 2 per-node CN2 field). Infiltration is the
#     engine's own SCS-CN, spatially variable via the CN map.
#   * TIME-VARYING rain (an hourly MRMS/AORC hyetograph) -> the NATIVE
# TIME-VARYING path ("native_hyetograph"): the gross hourly
#     hyetograph is staged as FORMATTED DATA FILE 1 and the engine's own SCS-CN
#     applies the abstraction per-timestep on the real intensity structure
#     (RAINDEF=3). This resolves the hydrograph SHAPE the constant-rain path
#     could not; the residual peak-timing lag is bounded by the forcing product
#     and mesh routing, not the rain representation.
# The chosen path is recorded in the run envelope (runoff_path + reason).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunoffPathDecision:
    """Which CN/runoff path a rain-on-grid run uses, plus why.

    ``native`` for a constant design storm, ``native_hyetograph`` for a series."""

    path: str
    time_varying: bool
    reason: str


def select_runoff_path(
    *,
    hyetograph_mm: list[float] | None = None,
    constant_intensity_mm_per_hr: float | None = None,
) -> RunoffPathDecision:
    """Pick the runoff path automatically from the rain forcing shape.

    Two or more DISTINCT non-zero increments is time-varying; neither given errors."""
    if hyetograph_mm is None and constant_intensity_mm_per_hr is None:
        raise CNInfiltrationError(
            "select_runoff_path needs either a hyetograph_mm series or a "
            "constant_intensity_mm_per_hr; got neither (no rain forcing)."
        )
    if hyetograph_mm is not None:
        nonzero = [float(v) for v in hyetograph_mm if float(v) != 0.0]
        distinct = {round(v, 6) for v in nonzero}
        if len(distinct) >= 2:
            return RunoffPathDecision(
                path="native_hyetograph",
                time_varying=True,
                reason=(
                    f"time-varying hyetograph ({len(nonzero)} non-zero steps, "
                    f"{len(distinct)} distinct rates) drives the native SCS-CN "
                    "per-timestep on the real intensity structure via the "
                    "RAINDEF=3 FORTRAN FILE (gross rain as FORMATTED DATA FILE 1)."
                ),
            )
        return RunoffPathDecision(
            path="native",
            time_varying=False,
            reason=(
                "hyetograph is effectively a single constant rate -> native "
                "SCS-CN runoff model (RAINFALL-RUNOFF MODEL=1)."
            ),
        )
    return RunoffPathDecision(
        path="native",
        time_varying=False,
        reason=(
            f"constant design-storm intensity "
            f"{float(constant_intensity_mm_per_hr):g} mm/h -> native SCS-CN "
            "runoff model (RAINFALL-RUNOFF MODEL=1 + FORMATTED DATA FILE 2 CN2)."
        ),
    )
