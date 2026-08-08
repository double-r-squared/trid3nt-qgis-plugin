"""SCS curve-number infiltration for the TELEMAC-2D rain-on-grid template.

Implements the Godara, Bruland and Alfredsen (2024, Front. Water 6:1384205)
rain-on-grid infiltration surface: the SCS-CN rainfall-excess transform
(eq 7-8), the steep-slope CN correction (eq 9), the SCS antecedent-moisture
conversions, and a land-cover -> (curve number, Manning n) table (paper Table 1
analog, keyed to NLCD classes so our ``fetch_landcover`` fetcher drives it
directly). A ready-made direct-CN alternative is ``fetch_gcn250_curve_numbers``
(GCN250, Jaafar 2019, with a dry/average/wet AMC selector) -- when that raster
is used the class-to-CN table is bypassed and CN2 is sampled straight from it.

Two consumers, one module:

  * NATIVE runoff path -- TELEMAC v9.0.0 carries the SCS-CN runoff model
    natively (``runoff_scs_cn.f``, Ligier 2016; deck keyword
    ``RAINFALL-RUNOFF MODEL = 1`` + ``ANTECEDENT MOISTURE CONDITIONS`` +
    ``OPTION FOR INITIAL ABSTRACTION RATIO``). The engine reads a per-node CN2
    field from FORMATTED DATA FILE 2. This module builds that CN field
    (:func:`node_curve_numbers`) from the land-cover classes sampled at the
    mesh nodes. IMPORTANT: the engine's steep-slope correction is compiled OFF
    (``STEEPSLOPECOR = .FALSE.`` -- a hardcoded flag, not a keyword, in the
    installed 9.0.0 build), so when steep-slope correction is requested we apply
    :func:`huang_steep_slope_cn` to the CN field HERE, before writing the file,
    reproducing the paper's eq-9 intent without recompiling the solver.

  * PREPROCESSING rainfall-excess path -- the engine's native rain is
    ``RAINDEF=1`` (a single constant intensity over the rain duration; also
    hardcoded in the installed build). A time-varying real hyetograph (e.g. an
    hourly MRMS QPE series) therefore cannot drive the NATIVE CN model without
    recompiling. For that case :func:`rainfall_excess_hyetograph` applies the
    SCS-CN transform (eq 7-8) to the hyetograph up front, yielding an
    excess-rainfall (net) series fed to TELEMAC as time-varying rain with
    ``RAINFALL-RUNOFF MODEL = 0`` (no double-counting infiltration).

Every function is pure and unit-testable offline; nothing here touches the
mesh, the deck, or the network.
"""

from __future__ import annotations

import math

__all__ = [
    "scs_potential_retention_mm",
    "scs_runoff_mm",
    "huang_steep_slope_cn",
    "paper_exponential_steep_slope_cn",
    "amc_convert_cn",
    "landcover_cn_manning",
    "node_curve_numbers",
    "rainfall_excess_hyetograph",
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
# lever (see the rog-replication-methodology recon -- CN per land cover per
# engine is a NATE sign-off input, not a fixed constant). Manning n is the paper
# T2D column verbatim.
# ---------------------------------------------------------------------------

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

    Unknown codes fall back to the open-land row (documented default). The CN is
    the AMC-II (normal) value; convert with :func:`amc_convert_cn` if a dry/wet
    antecedent condition is wanted, or pass it straight to TELEMAC (which does
    the AMC conversion itself via its keyword).
    """
    return NLCD_CN_MANNING.get(int(nlcd_code), _DEFAULT_CN_MANNING)


# ---------------------------------------------------------------------------
# SCS-CN rainfall-excess (paper eq 7-8).
# ---------------------------------------------------------------------------


def scs_potential_retention_mm(cn: float) -> float:
    """Potential maximum retention S (mm) from a curve number (paper eq 8).

    ``S = 25400 / CN - 254`` (mm form). CN must be in (0, 100].
    """
    cn = float(cn)
    if not (0.0 < cn <= 100.0):
        raise CNInfiltrationError(f"curve number must be in (0, 100]; got {cn}")
    return 25400.0 / cn - 254.0


def scs_runoff_mm(rainfall_mm: float, cn: float, ia_ratio: float = 0.2) -> float:
    """Direct runoff Q (mm) from cumulative rainfall P via SCS-CN (paper eq 7).

    ``Q = (P - Ia)^2 / (P - Ia + S)`` for ``P > Ia`` else 0, with initial
    abstraction ``Ia = ia_ratio * S`` (``ia_ratio`` 0.2 standard / 0.05 revised,
    matching TELEMAC's ``OPTION FOR INITIAL ABSTRACTION RATIO``). P is CUMULATIVE
    event rainfall; for a hyetograph use :func:`rainfall_excess_hyetograph`.
    """
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

    Applies eq 7-8 to the CUMULATIVE rainfall at each step and differences the
    cumulative runoff, giving a per-step excess-rainfall series (mm) aligned with
    the input. The sum equals the total SCS-CN runoff; the series is
    non-negative and monotone in cumulative terms (runoff never decreases). This
    is the preprocessing path fed to TELEMAC as time-varying net rain when the
    native constant-intensity runoff model cannot ingest the hyetograph.
    """
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
    """Steep-slope-corrected CN2 via the Huang et al. (2006) formula.

    ``CN2a = CN2 * (322.79 + 15.63*alpha) / (alpha + 323.52)`` for a terrain
    slope ``alpha`` (m/m) in [0.14, 1.4]; below 0.14 no correction (factor 1),
    above 1.4 the factor is clamped at its 1.4 value. This is the EXACT formula
    the TELEMAC ``runoff_scs_cn.f`` steep-slope branch uses (Huang, Gallichand,
    Wang, Goulet 2006, Hydrological Processes 20:579-589). Applied to the CN
    field here because the engine's branch is compiled off in the installed
    9.0.0 build. Result is capped at 100.
    """
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
    """The exponential steep-slope form printed in the paper (eq 9 as written).

    ``CN_corr = CN2 * exp(0.0065 * slope)``. The paper cites Huang 2006 but
    prints this simplified exponential; the ACTUAL Huang 2006 rational formula
    (:func:`huang_steep_slope_cn`) is what the engine implements. Provided for
    exact paper reproduction / comparison; the native-engine-consistent default
    is the Huang rational form. Result is capped at 100.
    """
    return min(100.0, float(cn2) * math.exp(0.0065 * float(slope_m_per_m)))


# ---------------------------------------------------------------------------
# Antecedent-moisture conversion (matches TELEMAC's runoff_scs_cn.f exactly).
# ---------------------------------------------------------------------------


def amc_convert_cn(cn2: float, amc: int) -> float:
    """Convert a normal-condition CN2 to the AMC dry (I) / normal (II) / wet (III).

    AMC I (dry):  ``CN1 = 4.2*CN2 / (10 - 0.058*CN2)``
    AMC II (norm): ``CN2`` unchanged
    AMC III (wet): ``CN3 = 23*CN2 / (10 + 0.13*CN2)``

    The three formulas are byte-for-byte those in TELEMAC's ``runoff_scs_cn.f``
    (the engine applies them from its ``ANTECEDENT MOISTURE CONDITIONS`` keyword,
    so on the NATIVE path you pass CN2 and let the engine convert; this helper is
    for the preprocessing path and for parity tests).
    """
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
    """Build the per-node CN2 field for a mesh.

    When ``uniform_cn`` is given, every node gets that CN2 (the ``curve_number``
    template knob). Otherwise CN2 is looked up per node from ``nlcd_codes`` via
    the Table-1 analog (the land-cover-distributed knob). When
    ``steep_slope_correction`` is set, :func:`huang_steep_slope_cn` is applied
    per node using ``slopes_m_per_m`` (required in that case) -- necessary
    because the engine's own steep-slope branch is compiled off. Returns CN2
    (normal-AMC) values; the engine applies the AMC conversion from its keyword.
    """
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
