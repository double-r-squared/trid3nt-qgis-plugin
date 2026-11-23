"""Pilot-point numerics as pyEMU 1.7.0 computes them: ordinary kriging factors and a geostatistical prior covariance.

The default is pyEMU's ExpVario with contribution 1 and nugget 0. The factors use
OrdinaryKrige.calc_factors and the prior uses geostatistical_draws' normalization
to each point's variance.
"""

from __future__ import annotations

import numpy as np

__all__ = ["NEAREST", "covariance", "kriging_factors"]

#: pyEMU's maxpts_interp: how many of the nearest points a cell is interpolated from.
NEAREST = 20

#: pyEMU's EPSILON: a cell this close to a point takes that point's value alone.
_EPSILON = 1.0e-7


VARIOGRAMS = ("exponential", "spherical", "gaussian")


def _structure(h: np.ndarray, a: float, variogram: str) -> np.ndarray:
    # pyEMU 1.7.0 ExpVario, SphVario, and GauVario._h_function.
    if variogram == "exponential":
        return np.exp(-h / a)
    if variogram == "spherical":
        d = h / a
        return np.where(d > 1.0, 0.0, 1.0 - d * (1.5 - 0.5 * d * d))
    if variogram == "gaussian":
        return np.exp(-(h * h) / (a * a))
    raise ValueError(f"Unsupported variogram {variogram!r}.")


def _lag(dx: np.ndarray, dy: np.ndarray, *, anisotropy: float, bearing: float) -> np.ndarray:
    # pyEMU 1.7.0 Vario2d._apply_rotation, with bearing clockwise from north.
    if anisotropy == 1.0:
        return np.hypot(dx, dy)
    angle = np.deg2rad(90.0 - bearing)
    cosine, sine = np.cos(angle), np.sin(angle)
    return np.hypot(dx * cosine + dy * sine, (-dx * sine + dy * cosine) * anisotropy)


def covariance(points: np.ndarray, a: float, variance: np.ndarray, *, variogram: str = "exponential",
               contribution: float = 1.0, nugget: float = 0.0,
               anisotropy: float = 1.0, bearing: float = 0.0) -> np.ndarray:
    """The covariance between ``points`` (n x 2) under the selected structure of range ``a``, each point's diagonal
    its own ``variance`` and each off-diagonal scaled by sqrt(v_i v_j)."""
    points = np.asarray(points, dtype=float)
    lag = _lag(*(points[:, None, :] - points[None, :, :]).transpose(2, 0, 1),
               anisotropy=anisotropy, bearing=bearing)
    sd = np.sqrt(np.asarray(variance, dtype=float))
    # pyEMU 1.7.0 GeoStruct.covariance_matrix puts the nugget on the diagonal.
    held = _structure(lag, a, variogram) * (contribution / (contribution + nugget)) * np.outer(sd, sd)
    np.fill_diagonal(held, sd ** 2)
    return held


def kriging_factors(cells: np.ndarray, points: np.ndarray, a: float, *,
                    maxpts_interp: int = NEAREST, variogram: str = "exponential",
                    remove_negative_factors: bool = True, contribution: float = 1.0, nugget: float = 0.0,
                    anisotropy: float = 1.0, bearing: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """Each cell's ordinary kriging factors over ``points`` -> ``(index, factor)``, both cells x NEAREST (or fewer
    where there are fewer points), the unused places of a row index 0 at factor 0. A row's factors sum to 1."""
    cells, points = np.asarray(cells, dtype=float), np.asarray(points, dtype=float)
    n = min(maxpts_interp, len(points))
    squared = ((cells[:, None, :] - points[None, :, :]) ** 2).sum(axis=2)
    index = np.argsort(squared, axis=1, kind="quicksort")[:, :n]
    distance = np.sqrt(np.take_along_axis(squared, index, axis=1))
    near = points[index]
    system = np.ones((len(cells), n + 1, n + 1))
    pair_lag = _lag(*(near[:, :, None, :] - near[:, None, :, :]).transpose(3, 0, 1, 2),
                    anisotropy=anisotropy, bearing=bearing)
    system[:, :n, :n] = contribution * _structure(pair_lag, a, variogram)
    diagonal = np.arange(n)
    system[:, diagonal, diagonal] += nugget
    system[:, n, n] = 0.0
    rhs = np.ones((len(cells), n + 1))
    cell_lag = (distance if anisotropy == 1.0 else
                _lag(*(cells[:, None, :] - near).transpose(2, 0, 1), anisotropy=anisotropy, bearing=bearing))
    # pyEMU 1.7.0 OrdinaryKrige uses GeoStruct.covariance_points for the RHS.
    rhs[:, :n] = nugget + contribution * _structure(cell_lag, a, variogram)
    factor = np.linalg.solve(system, rhs[..., None])[:, :n, 0]
    if n > 1 and remove_negative_factors:
        factor = np.where(factor > 0.0, factor, 0.0)
        factor /= factor.sum(axis=1, keepdims=True)
    snapped = distance[:, 0] <= _EPSILON
    factor[snapped] = 0.0
    factor[snapped, 0] = 1.0
    return np.where(factor != 0.0, index, 0), factor
