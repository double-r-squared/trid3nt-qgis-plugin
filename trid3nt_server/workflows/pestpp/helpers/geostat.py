"""Pilot-point numerics as pyEMU 1.7.0 computes them: ordinary kriging factors and a geostatistical prior covariance.

The variogram is pyEMU's ExpVario at contribution 1 and nugget 0 (a sill of 1, the structure only): its covariance at
lag h is exp(-h / a). The factors are OrdinaryKrige.calc_factors' (the 20 nearest points, a point within 1e-7 taken
alone, negative factors dropped and the rest renormalised, Deutsch 1996); the covariance is geostatistical_draws',
the structure scaled so each point's variance is its own.
"""

from __future__ import annotations

import numpy as np

__all__ = ["NEAREST", "covariance", "kriging_factors"]

#: pyEMU's maxpts_interp: how many of the nearest points a cell is interpolated from.
NEAREST = 20

#: pyEMU's EPSILON: a cell this close to a point takes that point's value alone.
_EPSILON = 1.0e-7


def _structure(h: np.ndarray, a: float) -> np.ndarray:
    return np.exp(-h / a)


def covariance(points: np.ndarray, a: float, variance: np.ndarray) -> np.ndarray:
    """The covariance between ``points`` (n x 2) under the exponential structure of range ``a``, each point's diagonal
    its own ``variance`` and each off-diagonal scaled by sqrt(v_i v_j)."""
    points = np.asarray(points, dtype=float)
    lag = np.hypot(*(points[:, None, :] - points[None, :, :]).transpose(2, 0, 1))
    sd = np.sqrt(np.asarray(variance, dtype=float))
    held = _structure(lag, a) * np.outer(sd, sd)
    np.fill_diagonal(held, sd ** 2)
    return held


def kriging_factors(cells: np.ndarray, points: np.ndarray, a: float) -> tuple[np.ndarray, np.ndarray]:
    """Each cell's ordinary kriging factors over ``points`` -> ``(index, factor)``, both cells x NEAREST (or fewer
    where there are fewer points), the unused places of a row index 0 at factor 0. A row's factors sum to 1."""
    cells, points = np.asarray(cells, dtype=float), np.asarray(points, dtype=float)
    n = min(NEAREST, len(points))
    squared = ((cells[:, None, :] - points[None, :, :]) ** 2).sum(axis=2)
    index = np.argsort(squared, axis=1, kind="quicksort")[:, :n]
    lag = np.sqrt(np.take_along_axis(squared, index, axis=1))
    near = points[index]
    system = np.ones((len(cells), n + 1, n + 1))
    system[:, :n, :n] = _structure(np.hypot(*(near[:, :, None, :] - near[:, None, :, :]).transpose(3, 0, 1, 2)), a)
    system[:, n, n] = 0.0
    rhs = np.ones((len(cells), n + 1))
    rhs[:, :n] = _structure(lag, a)
    factor = np.linalg.solve(system, rhs[..., None])[:, :n, 0]
    if n > 1:
        factor = np.where(factor > 0.0, factor, 0.0)
        factor /= factor.sum(axis=1, keepdims=True)
    snapped = lag[:, 0] <= _EPSILON
    factor[snapped] = 0.0
    factor[snapped, 0] = 1.0
    return np.where(factor > 0.0, index, 0), factor
