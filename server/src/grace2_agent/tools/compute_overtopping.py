"""Atomic tool ``compute_overtopping`` -- empirical EurOtop mean wave-overtopping
discharge over a sloped coastal structure.

A pure-analytic, deterministic, ZERO-fetch coastal POST-PROCESSOR. Given a
nearshore sea state (significant wave height Hm0, peak period Tp -- which can come
straight from a SWAN / SnapWave run) and a coastal-structure geometry (crest
freeboard Rc, front slope, optional roughness), it returns the mean wave
overtopping discharge ``q`` (m^3/s per metre of structure length) from the
empirical EurOtop formulae.

This is the lecture's OVERTOPPING tool: the AWS / Australian-Water-School
"Making Waves: Wave Modeling with SWAN" reference flags EuroTop (EurOtop) as a
small deterministic post-processor that "compute[s] the water VOLUME passing OVER
a structure once the nearshore wave energy is known" (see
``reports/references/lecture_aws_swan_making_waves/notes.md`` -- OVERTOPPING
MODELS [~46:07-46:33]). It pairs with ``run_swan_waves`` / SFINCS+SnapWave: the
wave engine gives Hm0 + Tp at the toe; this tool turns that plus a crest level
into an overtopping rate for coastal-defence / levee / seawall scenarios.

SOURCE OF THE FORMULAS
----------------------
**EurOtop (2018), Manual on wave overtopping of sea defences and related
structures**, Chapter 5 (mean overtopping discharge for sloped/dike structures):

Mean-value (probabilistic design) formula -- EurOtop 2018 Eqn 5.10:

    q / sqrt(g * Hm0^3)
        = (0.023 / sqrt(tan_alpha)) * gamma_b * xi
          * exp[ -( 2.7 * Rc /
                    (xi * Hm0 * gamma_b * gamma_f * gamma_beta * gamma_v) )^1.3 ]

with the upper limit (steep slopes / large breaker parameter) -- EurOtop 2018
Eqn 5.11:

    q / sqrt(g * Hm0^3)
        = 0.09 * exp[ -( 1.5 * Rc /
                         (Hm0 * gamma_f * gamma_beta) )^1.3 ]

The governing dimensionless discharge is the SMALLER of the two (the upper-limit
formula caps the breaking formula at large surf-similarity). ``q`` is then
recovered as ``q* * sqrt(g * Hm0^3)``.

The breaker / surf-similarity parameter (Iribarren number) is

    xi = tan_alpha / sqrt( s_m1_0 ),   s_m1_0 = Hm0 / L_m1_0,
    L_m1_0 = g * T_m1_0^2 / (2*pi),    T_m1_0 = Tp / 1.1

(EurOtop uses the spectral period T_{m-1,0}; for a standard JONSWAP-shape sea
T_{m-1,0} ~= Tp / 1.1, the conversion this tool applies when only Tp is known).

REDUCTION FACTORS (gamma, all default 1.0 = smooth, perpendicular, no berm):
- ``gamma_f``    -- front-face roughness/permeability (1.0 smooth concrete; ~0.55
  for a 2-layer rock armour; ~0.45 for some armour units).
- ``gamma_b``    -- berm influence (1.0 = no berm).
- ``gamma_beta`` -- oblique-wave-attack influence (1.0 = perpendicular).
- ``gamma_v``    -- vertical-wall-on-slope / crest-wall influence (1.0 = none).

DETERMINISM (Invariant 1): the result is a pure closed-form function of the
inputs -- no LLM, no randomness, no network, no cache shim. Every overtopping
number the agent narrates is reproducible from the inputs alone.

LIMITATIONS (honesty -- empirical design tool, not a numerical solver):
- Sloped/dike geometry (EurOtop Chapter 5). Vertical / battered walls (EurOtop
  Chapter 7) are NOT covered by this version.
- Mean-value (probabilistic-design) coefficients; this is the central estimate,
  not a deterministic-design upper bound.
- The result is a MEAN discharge; it carries no individual-wave or volume-per-
  wave distribution.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

__all__ = [
    "compute_overtopping",
    "OvertoppingError",
]

logger = logging.getLogger("grace2_agent.tools.compute_overtopping")

# Standard gravity (m/s^2).
_G = 9.81

# EurOtop 2018 Chapter 5 mean-overtopping coefficients (sloped structures).
_C_BREAK = 0.023  # Eqn 5.10 leading coefficient
_C_BREAK_EXP = 2.7  # Eqn 5.10 in-exponent coefficient
_C_EXP_POWER = 1.3  # shared exponent power (both 5.10 and 5.11)
_C_MAX = 0.09  # Eqn 5.11 leading coefficient (upper limit)
_C_MAX_EXP = 1.5  # Eqn 5.11 in-exponent coefficient

# Spectral-to-peak period conversion (EurOtop uses T_{m-1,0}; for a standard
# JONSWAP-shape sea T_{m-1,0} ~= Tp / 1.1).
_TP_TO_TM10 = 1.0 / 1.1


class OvertoppingError(ValueError):
    """Raised when ``compute_overtopping`` receives invalid inputs.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code (NFR-R-1 typed-error
    requirement):

    - ``HS_INVALID``        -- ``hs_m`` is missing, non-numeric, or <= 0.
    - ``TP_INVALID``        -- ``tp_s`` is missing, non-numeric, or <= 0.
    - ``FREEBOARD_INVALID`` -- ``crest_freeboard_m`` is non-numeric or < 0.
    - ``SLOPE_INVALID``     -- ``slope`` is missing, non-numeric, or <= 0.
    - ``GAMMA_INVALID``     -- a reduction factor is non-numeric or outside
      (0, 1].
    """

    error_code: str = "OVERTOPPING_INVALID"
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


_METADATA = AtomicToolMetadata(
    name="compute_overtopping",
    # Pure deterministic math, no external read -> never touches the cache shim.
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


def _coerce_positive(value: Any, code: str, label: str) -> float:
    """Coerce ``value`` to a strictly-positive finite float or raise."""
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise OvertoppingError(
            code, f"{label} must be a positive number; got {value!r}."
        ) from exc
    if not math.isfinite(f) or f <= 0.0:
        raise OvertoppingError(
            code, f"{label} must be a finite positive number; got {f!r}."
        )
    return f


def _coerce_nonneg(value: Any, code: str, label: str) -> float:
    """Coerce ``value`` to a non-negative finite float or raise."""
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise OvertoppingError(
            code, f"{label} must be a non-negative number; got {value!r}."
        ) from exc
    if not math.isfinite(f) or f < 0.0:
        raise OvertoppingError(
            code, f"{label} must be a finite non-negative number; got {f!r}."
        )
    return f


def _coerce_gamma(value: Any, label: str) -> float:
    """Coerce a reduction factor into (0, 1] or raise OvertoppingError."""
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise OvertoppingError(
            "GAMMA_INVALID", f"{label} must be a number in (0, 1]; got {value!r}."
        ) from exc
    if not math.isfinite(f) or f <= 0.0 or f > 1.0:
        raise OvertoppingError(
            "GAMMA_INVALID",
            f"{label} must be a finite reduction factor in (0, 1]; got {f!r}.",
        )
    return f


@register_tool(
    _METADATA,
    # readOnlyHint=True (pure compute, no side effects), openWorldHint=False
    # (no external call), destructiveHint=False, idempotentHint=True
    # (deterministic: same inputs -> same output).
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def compute_overtopping(
    hs_m: float,
    tp_s: float,
    crest_freeboard_m: float,
    slope: float,
    gamma_f: float = 1.0,
    gamma_b: float = 1.0,
    gamma_beta: float = 1.0,
    gamma_v: float = 1.0,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Estimate the mean wave-overtopping discharge over a sloped coastal structure.

    A deterministic empirical POST-PROCESSOR (EurOtop 2018, Chapter 5): given a
    nearshore sea state (Hm0, Tp -- typically straight from a SWAN / SnapWave run)
    and a structure geometry (crest freeboard Rc above still-water level, front
    slope, optional roughness/reduction factors), it returns the mean overtopping
    discharge ``q`` in m^3/s per metre of structure crest length. Use it to turn
    a wave field into a coastal-defence overtopping rate.

    Use this when:
        - The user has a nearshore wave height + period and a seawall / levee /
          dike / revetment crest level and asks "how much water overtops?",
          "what is the overtopping rate?", or "is this crest high enough?".
        - You have just run ``run_swan_waves`` (or SFINCS+SnapWave) and want the
          overtopping discharge at a structure given its crest freeboard.

    Do NOT use this for:
        - Vertical / battered seawalls (EurOtop Chapter 7) -- this version covers
          SLOPED dike/revetment geometry only.
        - Computing the wave field itself (use ``run_swan_waves``).
        - Inland / pluvial flooding (use ``run_swmm_urban_flood``) or surge
          inundation depth (use ``run_model_flood_scenario``).

    Parameters:
        hs_m: nearshore significant wave height Hm0 at the structure toe, m (> 0).
        tp_s: peak wave period Tp at the structure toe, s (> 0). Internally
            converted to the spectral period T_{m-1,0} ~= Tp / 1.1.
        crest_freeboard_m: crest freeboard Rc -- the crest height ABOVE the still
            water level, m (>= 0). 0 means the crest is at the water line.
        slope: the front-face slope as the TANGENT (rise/run), e.g. 0.5 for a 1:2
            slope, 0.333 for 1:3 (> 0). A "cot" value > 1 is accepted and inverted
            (e.g. 2 -> 0.5) for convenience.
        gamma_f: front-face roughness/permeability reduction factor, (0, 1].
            1.0 = smooth (concrete/asphalt); ~0.55 = 2-layer rock armour. Default 1.0.
        gamma_b: berm influence factor, (0, 1]. 1.0 = no berm. Default 1.0.
        gamma_beta: oblique-wave-attack factor, (0, 1]. 1.0 = perpendicular
            attack. Default 1.0.
        gamma_v: crest-wall / vertical-element-on-slope factor, (0, 1]. 1.0 =
            none. Default 1.0.

    Returns:
        dict with structure::

            {
              "q_m3_s_per_m": float,    # mean overtopping discharge, m^3/s per m
              "q_l_s_per_m": float,     # same, litres/s per m (q*1000) -- convenience
              "governing_formula": str, # "breaking" (Eqn 5.10) | "max" (Eqn 5.11)
              "dimensionless_q": float, # q / sqrt(g * Hm0^3)
              "breaker_parameter": float,  # surf-similarity xi_{m-1,0}
              "hs_m": float, "tp_s": float, "crest_freeboard_m": float,
              "slope_tan": float,       # slope used (tan form)
              "gamma_f": float, "gamma_b": float,
              "gamma_beta": float, "gamma_v": float,
              "method": str,            # citation string
              "notes": list[str],       # honesty notes
              "computed_at": str,       # ISO 8601 UTC timestamp
            }

    Raises:
        OvertoppingError: with a typed ``error_code`` when an input is missing,
            non-numeric, or out of range.
    """
    hs = _coerce_positive(hs_m, "HS_INVALID", "hs_m")
    tp = _coerce_positive(tp_s, "TP_INVALID", "tp_s")
    rc = _coerce_nonneg(crest_freeboard_m, "FREEBOARD_INVALID", "crest_freeboard_m")
    slope_in = _coerce_positive(slope, "SLOPE_INVALID", "slope")

    gf = _coerce_gamma(gamma_f, "gamma_f")
    gb = _coerce_gamma(gamma_b, "gamma_b")
    gbeta = _coerce_gamma(gamma_beta, "gamma_beta")
    gv = _coerce_gamma(gamma_v, "gamma_v")

    notes: list[str] = [
        "EurOtop 2018 Chapter 5 mean-value (probabilistic-design) coefficients; "
        "sloped dike/revetment geometry only.",
        "Peak period Tp converted to spectral period T_{m-1,0} = Tp / 1.1.",
    ]

    # Accept a cotangent (run/rise) slope > 1 for convenience and invert it to
    # the tangent the formulae use.
    tan_alpha = slope_in
    if slope_in > 1.0:
        tan_alpha = 1.0 / slope_in
        notes.append(
            f"slope={slope_in:g} > 1 interpreted as a cotangent (1:{slope_in:g}); "
            f"used tan(alpha)={tan_alpha:g}."
        )

    # Spectral period and deep-water spectral wavelength.
    t_m10 = tp * _TP_TO_TM10
    l_m10 = _G * t_m10 * t_m10 / (2.0 * math.pi)
    s_m10 = hs / l_m10
    xi = tan_alpha / math.sqrt(s_m10)

    # --- EurOtop 2018 Eqn 5.10 (breaking / surf-similarity-scaled) ----------
    denom_break = xi * hs * gb * gf * gbeta * gv
    arg_break = (_C_BREAK_EXP * rc / denom_break) ** _C_EXP_POWER
    q_star_break = (
        (_C_BREAK / math.sqrt(tan_alpha)) * gb * xi * math.exp(-arg_break)
    )

    # --- EurOtop 2018 Eqn 5.11 (upper limit; non-breaking / steep) ----------
    denom_max = hs * gf * gbeta
    arg_max = (_C_MAX_EXP * rc / denom_max) ** _C_EXP_POWER
    q_star_max = _C_MAX * math.exp(-arg_max)

    # The governing dimensionless discharge is the SMALLER of the two.
    if q_star_break <= q_star_max:
        q_star = q_star_break
        governing = "breaking"
    else:
        q_star = q_star_max
        governing = "max"

    # Recover the dimensional discharge q = q* * sqrt(g * Hm0^3).
    q = q_star * math.sqrt(_G * hs ** 3)

    result: dict[str, Any] = {
        "q_m3_s_per_m": q,
        "q_l_s_per_m": q * 1000.0,
        "governing_formula": governing,
        "dimensionless_q": q_star,
        "breaker_parameter": xi,
        "hs_m": hs,
        "tp_s": tp,
        "crest_freeboard_m": rc,
        "slope_tan": tan_alpha,
        "gamma_f": gf,
        "gamma_b": gb,
        "gamma_beta": gbeta,
        "gamma_v": gv,
        "method": (
            "EurOtop (2018) Manual on wave overtopping, Chapter 5 mean "
            "overtopping discharge for sloped structures (Eqns 5.10 / 5.11)"
        ),
        "notes": notes,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        "compute_overtopping Hs=%.3g Tp=%.3g Rc=%.3g slope=%.3g xi=%.3f "
        "-> q=%.4g m3/s/m (%s)",
        hs,
        tp,
        rc,
        tan_alpha,
        xi,
        q,
        governing,
    )

    return result
