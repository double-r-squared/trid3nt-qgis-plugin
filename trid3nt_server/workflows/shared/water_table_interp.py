"""Water-table interpolation seam: measured well heads -> a starting-head surface.

A SHARED, engine-agnostic seam that turns a set of georeferenced measured well
heads (local east/north metres + head elevation) into either an interpolated
water-table SURFACE (regression kriging: a least-squares trend plane plus an
ordinary-kriged residual field) or - when the well set is too thin/clustered to
support kriging - a plain trend PLANE, and reports which, with a stated rule.

Why a tiered rule (decide per evidence, state it):
    Ordinary kriging needs enough wells, with enough spatial spread and a
    non-degenerate empirical variogram, to estimate the spatial-correlation
    structure. A handful of clustered wells cannot; forcing kriging there invents
    structure. So the seam applies an explicit, evidence-based ladder:

      * n >= KRIGE_MIN_WELLS AND good 2-D spread AND a fittable variogram
            -> REGRESSION KRIGING: trend plane + ordinary-kriged residuals.
      * TREND_MIN_WELLS <= n < KRIGE_MIN_WELLS, OR the variogram is degenerate
            -> TREND PLANE only (a least-squares potentiometric plane).
      * n < TREND_MIN_WELLS, OR a collinear/clustered set with too little
        cross-gradient spread
            -> INSUFFICIENT: return ``None`` so the caller falls back LOUDLY to
               its next basis (DEM proxy / demo). Never a fabricated surface.

    The trend gradient (gx, gy, m/m east/north) is taken from the plane in every
    non-insufficient case - it is the regional flow direction the capture-zone
    CHD boundary is oriented to. Kriging refines the SURFACE (local curvature the
    plane cannot carry) for a starting-head initial condition; it does not change
    the regional gradient the plane already captures.

Citations:
    Ordinary kriging: Cressie, N. (1993). "Statistics for Spatial Data." Wiley.
    Regression kriging (trend + kriged residual): Hengl, T., Heuvelink, G.B.M.,
    Rossiter, D.G. (2007). "About regression-kriging: From equations to case
    studies." Computers & Geosciences 33(10):1301-1315.
    Exponential variogram model: Journel & Huijbregts (1978), "Mining
    Geostatistics."

Pure + offline: numpy only, no I/O. Deterministic. NEVER raises on a
physically-shaped input - a degenerate set returns ``None`` (insufficient), never
an exception, so the caller's basis ladder stays in control.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(
    "trid3nt_server.workflows.shared.water_table_interp"
)

__all__ = [
    "WaterTableSurface",
    "interpolate_water_table",
    "KRIGE_MIN_WELLS",
    "TREND_MIN_WELLS",
    "MIN_WELL_EXTENT_M",
    "MIN_WELL_MINOR_STD_M",
]

#: Well-count thresholds for each rung of the interpolation ladder.
#: Kriging a variogram from < ~8 wells over-fits noise; a plane needs >= 3 for a
#: determined 2-D gradient. These mirror the composer's gradient-fit guards so the
#: two paths agree on what "enough wells" means.
KRIGE_MIN_WELLS: int = 8
TREND_MIN_WELLS: int = 3

#: Spatial-spread guards (metres). The minor-axis std guards against a collinear /
#: clustered set that leaves the cross-gradient component (and any variogram
#: anisotropy) unconstrained.
MIN_WELL_EXTENT_M: float = 500.0
MIN_WELL_MINOR_STD_M: float = 150.0

#: Number of lag bins for the empirical semivariogram.
_VARIOGRAM_BINS: int = 12


@dataclass(frozen=True)
class WaterTableSurface:
    """An interpolated water-table surface + full provenance.

    ``sample(east, north)`` returns the head elevation (m, same datum as the input
    wells) at a local east/north metre coordinate - vectorised over array inputs.
    ``method`` is the ladder rung actually used; ``gradient_*`` is the regional
    trend the CHD boundary is oriented to; the variogram fields are populated only
    for the kriging rung.
    """

    method: str  # "regression_kriging" | "trend_plane"
    sample: Callable[[Any, Any], Any]
    n_wells: int
    gradient_x: float  # m/m, east
    gradient_y: float  # m/m, north
    gradient_magnitude: float
    gradient_azimuth_deg: float  # compass bearing groundwater FLOWS toward
    trend_residual_rms_m: float
    head_range_m: float
    variogram: dict[str, float] = field(default_factory=dict)
    reason: str = "ok"

    def provenance(self) -> dict[str, Any]:
        """JSON-friendly provenance dict for the narration summary."""
        out = {
            "method": self.method,
            "n_wells": self.n_wells,
            "gradient_magnitude_m_per_m": self.gradient_magnitude,
            "gradient_azimuth_deg": self.gradient_azimuth_deg,
            "trend_residual_rms_m": self.trend_residual_rms_m,
            "head_range_m": self.head_range_m,
            "reason": self.reason,
        }
        if self.variogram:
            out["variogram"] = self.variogram
        return out


def _fit_trend_plane(east, north, head):
    """Least-squares ``head = a*east + b*north + c``; return ``(a, b, c, rms)``."""
    import numpy as np

    A = np.column_stack([east, north, np.ones(len(east))])
    coeffs, *_ = np.linalg.lstsq(A, head, rcond=None)
    a, b, c = float(coeffs[0]), float(coeffs[1]), float(coeffs[2])
    resid = head - (a * east + b * north + c)
    rms = float(math.sqrt(float(np.mean(resid ** 2))))
    return a, b, c, rms


def _empirical_variogram(east, north, resid):
    """Binned method-of-moments semivariogram of the trend residuals.

    Returns ``(lags, gammas, counts)`` over up to ``_VARIOGRAM_BINS`` distance
    bins spanning 0..(half the max pair distance) - the standard active-lag cap
    that keeps the far, sparsely-populated bins out of the fit.
    """
    import numpy as np

    n = len(east)
    ii, jj = np.triu_indices(n, k=1)
    dx = east[ii] - east[jj]
    dy = north[ii] - north[jj]
    dist = np.hypot(dx, dy)
    semiv = 0.5 * (resid[ii] - resid[jj]) ** 2
    max_lag = float(dist.max()) * 0.5
    if not math.isfinite(max_lag) or max_lag <= 0.0:
        return np.array([]), np.array([]), np.array([])
    edges = np.linspace(0.0, max_lag, _VARIOGRAM_BINS + 1)
    lags, gammas, counts = [], [], []
    for b in range(_VARIOGRAM_BINS):
        m = (dist >= edges[b]) & (dist < edges[b + 1])
        cnt = int(m.sum())
        if cnt == 0:
            continue
        lags.append(float(0.5 * (edges[b] + edges[b + 1])))
        gammas.append(float(semiv[m].mean()))
        counts.append(cnt)
    return np.asarray(lags), np.asarray(gammas), np.asarray(counts)


def _fit_exponential_variogram(lags, gammas):
    """Fit gamma(h) = nugget + (sill-nugget)*(1 - exp(-3h/rng)); ``None`` if degenerate.

    A coarse bounded grid search over the range parameter with a closed-form
    (non-negative) least-squares split of nugget vs. partial sill at each range -
    robust, dependency-free, and good enough for a screening surface. Returns
    ``(nugget, sill, rng)`` or ``None`` when the residual field has no usable
    structure (flat/near-zero variance, too few bins).
    """
    import numpy as np

    if len(lags) < 3:
        return None
    var = float(np.mean(gammas))
    if not math.isfinite(var) or var <= 0.0:
        return None
    hmax = float(lags.max())
    best = None
    for rng in np.linspace(hmax * 0.2, hmax * 2.0, 24):
        basis = 1.0 - np.exp(-3.0 * lags / rng)
        # Non-negative closed form for [nugget, partial_sill] on columns [1, basis].
        X = np.column_stack([np.ones(len(lags)), basis])
        try:
            coef, *_ = np.linalg.lstsq(X, gammas, rcond=None)
        except np.linalg.LinAlgError:
            continue
        nugget = max(0.0, float(coef[0]))
        psill = max(0.0, float(coef[1]))
        pred = nugget + psill * basis
        sse = float(np.sum((gammas - pred) ** 2))
        if best is None or sse < best[0]:
            best = (sse, nugget, nugget + psill, float(rng))
    if best is None:
        return None
    _, nugget, sill, rng = best
    if sill <= 0.0 or not math.isfinite(sill):
        return None
    return nugget, sill, rng


def _ordinary_kriging_sampler(east, north, resid, nugget, sill, rng):
    """Build an OK sampler of the residual field (exponential covariance).

    Solves the ordinary-kriging system once (LU of the augmented covariance
    matrix with the unbiasedness Lagrange row) and returns a closure that applies
    the weights to any query point(s). Falls back to a nearest-well residual if
    the kriging matrix is singular for a given solve (never raises).
    """
    import numpy as np

    n = len(east)

    def cov(h):
        return (sill - nugget) * np.exp(-3.0 * h / rng)

    # Data-data covariance (sill on the diagonal), augmented with the Lagrange row.
    di = east[:, None] - east[None, :]
    dj = north[:, None] - north[None, :]
    dd = np.hypot(di, dj)
    K = cov(dd)
    np.fill_diagonal(K, sill)
    A = np.zeros((n + 1, n + 1))
    A[:n, :n] = K
    A[:n, n] = 1.0
    A[n, :n] = 1.0
    try:
        A_inv = np.linalg.inv(A)
    except np.linalg.LinAlgError:
        A_inv = None

    e_arr = np.asarray(east, float)
    n_arr = np.asarray(north, float)
    r_arr = np.asarray(resid, float)

    def sample_resid(qe, qn):
        qe = np.atleast_1d(np.asarray(qe, float))
        qn = np.atleast_1d(np.asarray(qn, float))
        out = np.empty(qe.shape, float)
        for idx in range(qe.size):
            hd = np.hypot(e_arr - qe.flat[idx], n_arr - qn.flat[idx])
            if A_inv is None:
                out.flat[idx] = r_arr[int(np.argmin(hd))]
                continue
            b = np.empty(n + 1)
            b[:n] = cov(hd)
            b[n] = 1.0
            w = A_inv @ b
            out.flat[idx] = float(np.dot(w[:n], r_arr))
        return out

    return sample_resid


def interpolate_water_table(
    wells: list[dict[str, Any]],
) -> WaterTableSurface | None:
    """Interpolate a water-table surface from measured well heads (tiered rule).

    Args:
        wells: usable wells, each a dict with local ``east`` / ``north`` metres and
            a ``head_m`` elevation (the composer's ``_usable_well_heads`` output).

    Returns:
        A ``WaterTableSurface`` (regression-kriging or trend-plane), or ``None``
        when the set is INSUFFICIENT (too few wells, or a degenerate spread) - the
        caller then falls back loudly to its next basis. NEVER raises.
    """
    try:
        import numpy as np

        n = len(wells)
        if n < TREND_MIN_WELLS:
            return None
        east = np.asarray([float(w["east"]) for w in wells], float)
        north = np.asarray([float(w["north"]) for w in wells], float)
        head = np.asarray([float(w["head_m"]) for w in wells], float)

        # Spread guard (collinear/clustered -> cross-gradient unconstrained).
        cov = np.cov(np.vstack([east, north]))
        evals = np.linalg.eigvalsh(cov)
        minor_std = math.sqrt(max(float(evals[0]), 0.0))
        extent = math.hypot(
            float(east.max() - east.min()), float(north.max() - north.min())
        )
        if extent < MIN_WELL_EXTENT_M or minor_std < MIN_WELL_MINOR_STD_M:
            return None

        a, b, c, rms = _fit_trend_plane(east, north, head)
        mag = math.hypot(a, b)
        if not math.isfinite(mag):
            return None
        az = math.degrees(math.atan2(-a, -b)) % 360.0
        head_range = float(head.max() - head.min())

        # --- Rung 1: regression kriging (trend + OK residual) ---------------- #
        method = "trend_plane"
        variogram: dict[str, float] = {}
        resid_sampler = None
        if n >= KRIGE_MIN_WELLS:
            resid = head - (a * east + b * north + c)
            lags, gammas, _counts = _empirical_variogram(east, north, resid)
            vg = _fit_exponential_variogram(lags, gammas)
            if vg is not None:
                nugget, sill, rng = vg
                resid_sampler = _ordinary_kriging_sampler(
                    east, north, resid, nugget, sill, rng
                )
                method = "regression_kriging"
                variogram = {
                    "model": 1.0,  # 1 == exponential (kept numeric for the dataclass)
                    "nugget": float(nugget),
                    "sill": float(sill),
                    "range_m": float(rng),
                }

        def _sample(qe, qn):
            trend = a * np.asarray(qe, float) + b * np.asarray(qn, float) + c
            if resid_sampler is None:
                return trend
            return trend + resid_sampler(qe, qn)

        reason = (
            "kriging: n>=%d, spread ok, variogram fit" % KRIGE_MIN_WELLS
            if method == "regression_kriging"
            else "trend plane: %d well(s) (%s)"
            % (
                n,
                "kriging variogram degenerate"
                if n >= KRIGE_MIN_WELLS
                else "below kriging min %d" % KRIGE_MIN_WELLS,
            )
        )
        return WaterTableSurface(
            method=method,
            sample=_sample,
            n_wells=n,
            gradient_x=a,
            gradient_y=b,
            gradient_magnitude=mag,
            gradient_azimuth_deg=az,
            trend_residual_rms_m=rms,
            head_range_m=head_range,
            variogram=variogram,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 - interpolation is best-effort
        logger.warning(
            "interpolate_water_table failed (non-fatal, caller falls back): %s", exc
        )
        return None
