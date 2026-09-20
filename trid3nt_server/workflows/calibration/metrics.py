"""The skill metrics over a run's pairs: NSE, KGE, PBIAS, RSR, RMSE, R2, peak.

Paired-series statistics no spatial algorithm computes, delegated to
``spotpy.objectivefunctions`` so nothing here reimplements metric math. A metric
that is undefined over this sample comes back null carrying its reason; the
acceptance verdict is not here - a band is a reader's judgement, not a measure.
"""

from __future__ import annotations

import math
from typing import Any

from . import CalibrationError
from .pairing import Pairs

__all__ = ["metrics", "nash_sutcliffe_efficiency", "pearson_r2"]


def _objective_functions() -> Any:
    """``spotpy.objectivefunctions`` behind a typed honest error."""
    try:
        import spotpy.objectivefunctions as sof
    except ImportError as exc:
        raise CalibrationError(
            "CALIBRATION_DEPENDENCY_MISSING",
            f"spotpy is not importable in this environment ({type(exc).__name__}"
            f": {exc}); the skill metrics stand on spotpy.objectivefunctions.",
            retryable=False) from exc
    return sof


def _finite(observed: Any, simulated: Any) -> tuple[Any, Any]:
    """The paired FINITE arrays, possibly empty so the caller decides how a short
    sample degrades; a length mismatch raises."""
    import numpy as np

    obs = np.asarray([float(v) for v in observed], dtype=np.float64)
    sim = np.asarray([float(v) for v in simulated], dtype=np.float64)
    if obs.shape[0] != sim.shape[0]:
        raise CalibrationError(
            "CALIBRATION_LENGTH_MISMATCH",
            f"observed (len={obs.shape[0]}) and simulated (len={sim.shape[0]}) "
            "must be the same length.")
    keep = np.isfinite(obs) & np.isfinite(sim)
    return obs[keep], sim[keep]


def _clean(value: Any) -> float | None:
    """NaN or inf to None - a metric is never fabricated - else rounded to 6."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 6) if math.isfinite(number) else None


def nash_sutcliffe_efficiency(observed: Any, simulated: Any) -> float | None:
    """NSE over a paired series, non-finite pairs dropped; None under two usable
    pairs or on a zero-variance observed series."""
    import numpy as np

    obs, sim = _finite(observed, simulated)
    if obs.shape[0] < 2 or float(np.var(obs)) == 0.0:
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        return _clean(_objective_functions().nashsutcliffe(obs, sim))


def pearson_r2(observed: Any, simulated: Any) -> float | None:
    """The squared Pearson correlation, non-finite pairs dropped; None under two
    usable pairs or when either side has zero variance."""
    import numpy as np

    obs, sim = _finite(observed, simulated)
    if obs.shape[0] < 2 or float(np.var(obs)) == 0.0 or float(np.var(sim)) == 0.0:
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        return _clean(_objective_functions().rsquared(obs, sim))


def metrics(pairs: Pairs, *, variable: str = "generic") -> dict[str, Any]:
    """Every skill metric this sample supports, with the caveat for each null.

    ``peak_timing_error`` is null unless the pairs carry a shared time axis: a
    static spatial pairing - one sample per surveyed mark off a max-flood field -
    has no simulated peak TIME, and an argmax-to-argmax comparison there would
    fabricate one."""
    import numpy as np

    obs, sim = _finite(pairs.observed, pairs.simulated)
    n = int(obs.shape[0])
    caveats: list[str] = []
    if n < 2:
        raise CalibrationError(
            "CALIBRATION_TOO_FEW_PAIRS",
            f"{n} usable pair(s): a skill metric over fewer than two is not a "
            "measure of anything.")

    sof = _objective_functions()
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = {"NSE": sof.nashsutcliffe(obs, sim), "KGE": sof.kge(obs, sim),
               "PBIAS": sof.pbias(obs, sim), "RSR": sof.rsr(obs, sim),
               "RMSE": sof.rmse(obs, sim), "R2": sof.rsquared(obs, sim)}
    out: dict[str, Any] = {key: _clean(value) for key, value in raw.items()}
    for key, why in (
            ("NSE", "the observed series has zero variance"),
            ("KGE", "the observed or the simulated series has zero variance"),
            ("RSR", "the observed series has zero variance"),
            ("PBIAS", "the sum of the observed values is zero"),
            ("R2", "the observed or the simulated series has zero variance"),
            ("RMSE", "spotpy returned a non-finite value")):
        if out[key] is None:
            caveats.append(f"{key} is null: {why}.")

    obs_peak, sim_peak = float(obs.max()), float(sim.max())
    if obs_peak == 0.0:
        out["peak_error"] = None
        caveats.append("peak_error is null: the observed peak is exactly zero.")
    else:
        out["peak_error"] = round(100.0 * (sim_peak - obs_peak) / abs(obs_peak), 6)

    out["peak_timing_error"] = _peak_timing(pairs, obs, sim, caveats)
    out["n"] = n
    out["n_dropped"] = len(pairs.dropped)
    out["units"] = pairs.units
    out["variable"] = variable
    out["caveats"] = caveats
    return out


def _peak_timing(pairs: Pairs, obs: Any, sim: Any,
                 caveats: list[str]) -> float | None:
    """Seconds between the simulated peak and the observed one, or None with its
    reason. A record whose instants repeat per location is not a time axis."""
    import numpy as np

    times = [t for t in pairs.times]
    if not times or any(t is None for t in times) or len(set(times)) < 2:
        caveats.append(
            "peak_timing_error is null: these pairs carry no shared time axis "
            "(a static spatial comparison has one sample per location), so "
            "there is no simulated peak time to compare against the observed.")
        return None
    from datetime import datetime

    try:
        moments = [datetime.fromisoformat(str(t).replace("Z", "+00:00"))
                   for t in times]
    except ValueError:
        caveats.append("peak_timing_error is null: the record's instants are "
                       "not ISO-8601.")
        return None
    at_obs = moments[int(np.argmax(obs))]
    at_sim = moments[int(np.argmax(sim))]
    return round((at_sim - at_obs).total_seconds(), 3)
