"""Atomic tool ``compute_wave_nomograph`` -- analytic fetch-limited wind-wave
growth (the "nomograph" wind+fetch -> Hs/Tp sanity estimate).

A pure-analytic, deterministic, ZERO-fetch coastal reality-check tool. It maps a
wind speed and a fetch (the over-water distance the wind blows) to a significant
wave height (Hs) and a peak wave period (Tp) using the standard parametric
deep-water wave-growth ("nomograph") formulas. It is the cheap pre-flight bound
the AWS / Australian-Water-School "Making Waves: Wave Modeling with SWAN" lecture
flags as a sanity check on a full spectral run (see
``reports/references/lecture_aws_swan_making_waves/notes.md`` -- NOMOGRAPHS
[~19:25-21:28, 37:32-37:48]: "graphical wave-height/period estimates from wind
speed + fetch ... a simple reality check / sanity bound on model results").

SOURCE OF THE FORMULAS
----------------------
The parametric relations are the deep-water fetch-limited wind-wave-growth laws
codified in the U.S. Army Corps of Engineers **Shore Protection Manual (SPM,
1984), eqns 3-33/3-34/3-35** and carried into the **Coastal Engineering Manual
(CEM, EM 1110-2-1100, Part II-2)**. They express the dimensionless significant
wave height and dimensionless peak period as power laws of the dimensionless
fetch, scaled by the wind speed ``U``. SPM scales by an "adjusted" wind speed
U_A; here U_A is taken equal to the supplied 10 m wind ``U`` (the SPM wind-stress
adjustment factor is taken as unity for this first-order sanity tool --
documented as a limitation below).

Dimensionless fetch-limited growth (SPM 1984 eqns 3-33/3-34; the same form in
CEM Part II-2):

    X_hat   = g * F / U^2                       (dimensionless fetch)
    H_hat   = 1.6e-3 * X_hat^(1/2)              (dimensionless Hmo = g*Hs/U^2)
    T_hat   = 2.857e-1 * X_hat^(1/3)            (dimensionless Tp   = g*Tp/U)

    Hs = H_hat * U^2 / g                        (significant wave height, m)
    Tp = T_hat * U   / g                        (peak wave period, s)

Fully-developed (Pierson-Moskowitz) CAPS, reached when the fetch is large enough
that further fetch no longer grows the sea (SPM 1984 eqn 3-35; CEM II-2):

    H_hat_fd = 2.433e-1   ->  Hs_fd = 0.2433 * U^2 / g
    T_hat_fd = 8.134      ->  Tp_fd = 8.134  * U   / g

Duration limit: when an optional ``duration_hr`` is supplied we convert the
storm duration to the EQUIVALENT fetch the wind could build in that time
(inverting the SPM duration relation g*t/U = 68.8 * X_hat^(2/3)) and use the
SMALLER of the geographic and duration-equivalent fetch. If the
duration-equivalent fetch governs, the regime is reported as
``duration-limited``.

REGIME (returned)
-----------------
- ``fully-developed``   -- the PM caps govern (Hs/Tp hit their fully-developed
  ceiling for this wind speed; more fetch / duration would not grow the sea).
- ``duration-limited``  -- a finite ``duration_hr`` was supplied and the storm
  has not blown long enough to develop the full geographic fetch; the
  duration-equivalent fetch is the binding constraint.
- ``fetch-limited``     -- the geographic fetch is the binding constraint (the
  classic nomograph case).

DETERMINISM (Invariant 1): the result is a pure closed-form function of the
inputs -- no LLM, no randomness, no network, no cache shim. Every number the
agent narrates from this tool is reproducible from the inputs alone.

LIMITATIONS (be honest -- this is a SANITY tool, not a spectral solver):
- Deep-water only. There is no depth-limited (finite-depth) breaking cap in
  this first version; ``depth_m`` is accepted and, when given, used only to
  flag (in ``notes``) that a shallow-water correction is NOT applied.
- The wind-stress factor U_A is approximated by the 10 m wind U (factor = 1).
- Single-segment straight-line fetch; no fetch-direction weighting.
For a defensible nearshore wave field use ``run_swan_waves`` (the real SWAN
spectral solver); this tool is the pre-flight bound on that run.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

__all__ = [
    "compute_wave_nomograph",
    "WaveNomographError",
]

logger = logging.getLogger("grace2_agent.tools.compute_wave_nomograph")

# Standard gravity (m/s^2).
_G = 9.81

# Deep-water fetch-limited growth coefficients (SPM 1984 eqns 3-33/3-34,
# U-scaled). H_hat = g*Hs/U^2, T_hat = g*Tp/U.
_C_H = 1.6e-3  # dimensionless significant-wave-height coefficient
_C_T = 2.857e-1  # dimensionless peak-period coefficient

# Pierson-Moskowitz fully-developed caps (dimensionless; SPM 1984 eqn 3-35).
_H_HAT_FD = 2.433e-1  # -> Hs_fd = 0.2433 * U^2 / g
_T_HAT_FD = 8.134  # -> Tp_fd = 8.134 * U / g

# Duration -> equivalent-fetch coefficient (SPM 1984 duration relation).
# t_hat = 68.8 * X_hat^(2/3) where t_hat = g*t/U; inverting gives
# X_hat_dur = (t_hat / 68.8)^(3/2).
_C_DUR = 68.8


class WaveNomographError(ValueError):
    """Raised when ``compute_wave_nomograph`` receives invalid inputs.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code (NFR-R-1 typed-error
    requirement) surfaced in the pipeline strip:

    - ``WIND_SPEED_INVALID`` -- ``wind_speed_ms`` is missing, non-numeric, or
      <= 0.
    - ``FETCH_INVALID``      -- ``fetch_km`` is missing, non-numeric, or <= 0.
    - ``DURATION_INVALID``   -- ``duration_hr`` was supplied but is
      non-numeric or <= 0.
    - ``DEPTH_INVALID``      -- ``depth_m`` was supplied but is non-numeric or
      <= 0.
    """

    error_code: str = "WAVE_NOMOGRAPH_INVALID"
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


_METADATA = AtomicToolMetadata(
    name="compute_wave_nomograph",
    # Pure deterministic math, no external read -> never touches the cache shim.
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


def _coerce_positive(value: Any, code: str, label: str) -> float:
    """Coerce ``value`` to a strictly-positive float or raise WaveNomographError."""
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise WaveNomographError(
            code, f"{label} must be a positive number; got {value!r}."
        ) from exc
    if not math.isfinite(f) or f <= 0.0:
        raise WaveNomographError(
            code, f"{label} must be a finite positive number; got {f!r}."
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
def compute_wave_nomograph(
    wind_speed_ms: float,
    fetch_km: float,
    duration_hr: float | None = None,
    depth_m: float | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Estimate significant wave height + peak period from wind speed and fetch.

    A cheap, deterministic coastal REALITY CHECK: given a 10 m wind speed and the
    over-water fetch the wind blows across, it returns the significant wave height
    (Hs) and peak wave period (Tp) from the standard fetch-limited deep-water
    wave-growth ("nomograph") formulas (USACE Shore Protection Manual 1984 /
    Coastal Engineering Manual Part II-2, JONSWAP form). It is the pre-flight
    sanity bound on a full spectral run (``run_swan_waves``) -- the lecture's
    "is this wave field even plausible?" check.

    Use this when:
        - You want a QUICK sanity estimate of wave height / period from wind and
          fetch BEFORE (or instead of) running a full SWAN simulation.
        - The user asks "what waves would a 20 m/s wind build over 50 km of open
          water?" or "is a 3 m offshore Hs reasonable for this wind and fetch?"
        - You need a defensible offshore boundary Hs/Tp guess to seed
          ``run_swan_waves`` when no measured spectrum is available.

    Do NOT use this for:
        - A defensible nearshore wave field over real bathymetry (use
          ``run_swan_waves`` -- the actual SWAN spectral solver).
        - Surge / inundation depth (use ``run_model_flood_scenario``).
        - Shallow-water depth-limited breaking -- this tool is DEEP-WATER only;
          if ``depth_m`` is given it is reported but no breaking cap is applied.

    Parameters:
        wind_speed_ms: sustained 10 m wind speed over the water, m/s (> 0).
        fetch_km: the over-water fetch distance the wind blows across, km (> 0).
        duration_hr: OPTIONAL storm duration, hours (> 0). When supplied, the
            duration-equivalent fetch is computed and the SMALLER of the
            geographic and duration-equivalent fetch governs (regime may become
            ``duration-limited``).
        depth_m: OPTIONAL representative water depth, m (> 0). Accepted for
            provenance only; this version applies NO finite-depth correction and
            notes the omission.

    Returns:
        dict with structure::

            {
              "hs_m": float,            # significant wave height, m
              "tp_s": float,            # peak wave period, s
              "regime": str,            # "fetch-limited"|"duration-limited"|"fully-developed"
              "wind_speed_ms": float,   # echoed input
              "fetch_km": float,        # echoed (geographic) input fetch
              "effective_fetch_km": float,  # fetch actually used (after duration cap)
              "duration_hr": float | None,
              "depth_m": float | None,
              "dimensionless_fetch": float, # g*F/U^2 used
              "method": str,            # citation string
              "notes": list[str],       # honesty notes (deep-water-only, etc.)
              "computed_at": str,       # ISO 8601 UTC timestamp
            }

    Raises:
        WaveNomographError: with a typed ``error_code`` when an input is missing,
            non-numeric, or non-positive.
    """
    u = _coerce_positive(wind_speed_ms, "WIND_SPEED_INVALID", "wind_speed_ms")
    fetch_km_f = _coerce_positive(fetch_km, "FETCH_INVALID", "fetch_km")

    duration_f: float | None = None
    if duration_hr is not None:
        duration_f = _coerce_positive(duration_hr, "DURATION_INVALID", "duration_hr")

    depth_f: float | None = None
    if depth_m is not None:
        depth_f = _coerce_positive(depth_m, "DEPTH_INVALID", "depth_m")

    notes: list[str] = [
        "Deep-water fetch-limited estimate (no finite-depth breaking cap applied).",
        "Wind-stress factor approximated by the 10 m wind (U_A = U).",
    ]

    # Geographic fetch in metres.
    fetch_m = fetch_km_f * 1000.0

    # Dimensionless geographic fetch.
    x_hat_geo = _G * fetch_m / (u * u)

    # Duration-equivalent fetch (CEM II-2-39): t_hat = C_DUR * X_hat^(2/3).
    # Invert to the fetch the wind can build in ``duration_hr``.
    duration_governs = False
    x_hat = x_hat_geo
    effective_fetch_m = fetch_m
    if duration_f is not None:
        t_seconds = duration_f * 3600.0
        t_hat = _G * t_seconds / u
        x_hat_dur = (t_hat / _C_DUR) ** 1.5
        if x_hat_dur < x_hat_geo:
            duration_governs = True
            x_hat = x_hat_dur
            effective_fetch_m = x_hat_dur * (u * u) / _G

    # Fetch-limited dimensionless energy + period.
    h_hat = _C_H * math.sqrt(x_hat)
    t_hat_peak = _C_T * (x_hat ** (1.0 / 3.0))

    # Apply the Pierson-Moskowitz fully-developed caps.
    fully_developed = False
    if h_hat >= _H_HAT_FD or t_hat_peak >= _T_HAT_FD:
        fully_developed = True
        h_hat = _H_HAT_FD
        t_hat_peak = _T_HAT_FD

    hs_m = h_hat * (u * u) / _G
    tp_s = t_hat_peak * u / _G

    if fully_developed:
        regime = "fully-developed"
        notes.append(
            "Sea is fully developed for this wind speed (Pierson-Moskowitz caps "
            "govern); additional fetch or duration would not grow the waves."
        )
    elif duration_governs:
        regime = "duration-limited"
        notes.append(
            "Storm duration (not the geographic fetch) limits wave growth; the "
            "duration-equivalent fetch governs."
        )
    else:
        regime = "fetch-limited"

    if depth_f is not None:
        notes.append(
            f"depth_m={depth_f:g} supplied but NO finite-depth correction is "
            "applied in this version (deep-water estimate only)."
        )

    result: dict[str, Any] = {
        "hs_m": hs_m,
        "tp_s": tp_s,
        "regime": regime,
        "wind_speed_ms": u,
        "fetch_km": fetch_km_f,
        "effective_fetch_km": effective_fetch_m / 1000.0,
        "duration_hr": duration_f,
        "depth_m": depth_f,
        "dimensionless_fetch": x_hat,
        "method": (
            "USACE Shore Protection Manual (1984) / Coastal Engineering Manual "
            "Part II-2 fetch-limited deep-water (JONSWAP) wave growth"
        ),
        "notes": notes,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        "compute_wave_nomograph U=%.3g F=%.3gkm dur=%s -> Hs=%.3fm Tp=%.3fs regime=%s",
        u,
        fetch_km_f,
        duration_f,
        hs_m,
        tp_s,
        regime,
    )

    return result
