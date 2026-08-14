"""Cruz, Alexander & Wagner (2005) closed-form ACTIVE crown-fire rate of spread -
the ELMFIRE crown-fire V&V reference (the crown-fire exact-solution regression).

Cruz, M.G., Alexander, M.E., Wagner, R.H. (2005) "Development and testing of
models for predicting crown fire rate of spread in conifer forest stands",
Canadian Journal of Forest Research 35:1626-1639. The ACTIVE crown fire rate of
spread (their Eq. 24, CROSactive):

    R_active = 11.02 * U10^0.90 * CBD^0.19 * exp(-0.17 * EFFM)      [m/min]

  U10  = 10-m open wind speed (km/h)
  CBD  = canopy bulk density (kg/m3)
  EFFM = estimated fine dead-fuel moisture content (%)

ELMFIRE (third_party/elmfire build source elmfire_spread_rate.f90:177-179)
implements this VERBATIM, converting the 20-ft wind input to 10-m open wind and
the m/min result to ft/min:

    WS10KMPH = WS20MPH * (1.609 / 0.87)     ! mi/h @20ft -> km/h @10m
    CROSA    = CROWN_FIRE_ADJ * 11.02 * WS10KMPH**0.9 * CBD_EFF**0.19
               * EXP(-0.17 * 100.0 * M1) / 0.3048          ! ft/min

with CROWN_FIRE_ADJ defaulting to 1.0, CBD_EFF the canopy bulk density (kg/m3),
and M1 the fine dead-fuel-moisture FRACTION (so EFFM% = 100 * M1). Dropping the
0.3048 ft/min conversion leaves the m/min closed form above -- the exact target
the numerical level-set HEAD spread rate must reproduce on an UNCAPPED,
fully-active crown deck (CROWN_FIRE_SPREAD_RATE_LIMIT lifted so the MIN() cap
never bites, canopy cover >= CRITICAL_CANOPY_COVER, and CROSA/R0 > 1 so the fire
is active-crown rather than passive/torching).

Pure arithmetic -- no solver, no I/O. The verification composer overlays a live
in-image solve's measured head ROS against this reference and gates the relative
error to a stated tolerance.

In-image V&V (2026-08-14, trid3nt-local elmfire image; 20 mph @20ft, cbd 0.18
kg/m3, EFFM 3 %, 30 m cell, 0.4 h, uncapped active crown): the numerical
level-set head ROS was 123.75 m/min vs this closed form's 123.16 m/min -- a
relative error of 0.48 %.
"""

from __future__ import annotations

import math

__all__ = ["cruz_active_crown_ros_m_min", "MPH_20FT_TO_KMPH_10M"]

#: ELMFIRE elmfire_spread_rate.f90:140 -- 1.609 km/h per mi/h, divided by 0.87 to
#: convert a 20-ft wind speed to the 10-m open-wind height Cruz (2005) expects.
MPH_20FT_TO_KMPH_10M: float = 1.609 / 0.87


def cruz_active_crown_ros_m_min(
    wind_speed_mph_20ft: float,
    cbd_kg_m3: float,
    effm_pct: float,
    crown_fire_adj: float = 1.0,
) -> float:
    """Cruz (2005) active-crown rate of spread in m/min from a 20-ft wind.

    Args:
        wind_speed_mph_20ft: the 20-ft open wind speed (mph) -- ELMFIRE's WS20.
        cbd_kg_m3: canopy bulk density (kg/m3).
        effm_pct: estimated fine dead-fuel moisture content (%).
        crown_fire_adj: ELMFIRE CROWN_FIRE_ADJ multiplier (default 1.0).

    Returns:
        The active crown-fire rate of spread, metres/minute.
    """
    ws10_kmph = float(wind_speed_mph_20ft) * MPH_20FT_TO_KMPH_10M
    return (
        float(crown_fire_adj)
        * 11.02
        * ws10_kmph**0.9
        * float(cbd_kg_m3) ** 0.19
        * math.exp(-0.17 * float(effm_pct))
    )
