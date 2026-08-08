"""Thacker (1981) radially-symmetric paraboloid-basin analytic SWE solution.

A closed-form, frictionless shallow-water solution used ONLY as a V&V reference
for GeoClaw (the ``scenario="thacker"`` idealized bowl). Shared verbatim by the
worker deck-author (which builds the topo + t=0 qinit from it) and the agent-side
validation composer (which compares the numerical run against it) so the two agree
by construction. Pure arithmetic -- no numpy, no clawpack; unit-testable anywhere.

Geometry (planar Cartesian metres, ``coordinate_system=1``):

  Still-water depth        D(r) = h0 (1 - r^2/a^2)
  Bed elevation (up)       B(r) = -D(r) = h0 (r^2/a^2 - 1)   (deepest -h0 at r=0,
                                                              rises to 0 at r=a)

The radially-symmetric ("curved") Thacker oscillation, amplitude parameter
``A`` in (0, 1), angular frequency ``omega = sqrt(8 g h0)/a`` (period
``T = 2*pi*a/sqrt(8 g h0)``). At t=0 the flow is at rest (an extremum of the
oscillation), so the initial condition is a still surface -- the qinit sets only
eta, with u=v=0.

  P(t)         = 1 / (1 - A cos(omega t))
  eta(r,t)     = h0 [ sqrt(1-A^2) P - 1 - (r^2/a^2)((1-A^2) P^2 - 1) ]
  h(r,t)       = eta + D(r) = h0 sqrt(1-A^2) P [ 1 - (r^2/a^2) sqrt(1-A^2) P ]
                 (clipped at 0 -> dry outside the shoreline)
  shoreline    r_shore(t) = a (1 - A cos(omega t))^(1/2) / (1-A^2)^(1/4)

Derived reference scalars (``k = (1+A)/(1-A)``):

  period_s        = 2*pi / omega
  eta_center_max  = h0 (sqrt(k) - 1)        (central elevation at t=0)
  eta_center_min  = h0 (1/sqrt(k) - 1)      (central elevation at t=T/2)
  r_shore_min     = a k^(-1/4)              (waterline radius at t=0)
  r_shore_max     = a k^(1/4)               (waterline radius at t=T/2)
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "THACKER_GRAVITY",
    "thacker_bed_elevation",
    "thacker_eta",
    "thacker_depth",
    "thacker_shoreline_radius",
    "thacker_reference",
]

#: Gravitational acceleration (m/s^2) the Thacker deck + analytic reference share.
#: Pinned here so the worker's setgeo and the agent's V&V use the SAME g.
THACKER_GRAVITY: float = 9.81


def _omega(a_m: float, h0_m: float, g: float) -> float:
    return math.sqrt(8.0 * g * h0_m) / a_m


def thacker_bed_elevation(x: float, y: float, a_m: float, h0_m: float) -> float:
    """Paraboloid bed elevation B(x,y) (positive up), metres."""
    r2 = (x * x + y * y) / (a_m * a_m)
    return h0_m * (r2 - 1.0)


def thacker_eta(
    x: float, y: float, t: float, a_m: float, h0_m: float, amp_A: float,
    *, g: float = THACKER_GRAVITY,
) -> float:
    """Analytic free-surface elevation eta(x,y,t) relative to still water (m)."""
    om = _omega(a_m, h0_m, g)
    P = 1.0 / (1.0 - amp_A * math.cos(om * t))
    s = math.sqrt(1.0 - amp_A * amp_A)
    r2 = (x * x + y * y) / (a_m * a_m)
    return h0_m * (s * P - 1.0 - r2 * (s * s * P * P - 1.0))


def thacker_depth(
    x: float, y: float, t: float, a_m: float, h0_m: float, amp_A: float,
    *, g: float = THACKER_GRAVITY,
) -> float:
    """Analytic water depth h(x,y,t) (m), clipped at 0 outside the shoreline."""
    eta = thacker_eta(x, y, t, a_m, h0_m, amp_A, g=g)
    h = eta - thacker_bed_elevation(x, y, a_m, h0_m)
    return h if h > 0.0 else 0.0


def thacker_shoreline_radius(
    t: float, a_m: float, h0_m: float, amp_A: float, *, g: float = THACKER_GRAVITY
) -> float:
    """Analytic waterline radius r_shore(t) (m)."""
    om = _omega(a_m, h0_m, g)
    s = math.sqrt(1.0 - amp_A * amp_A)
    return a_m * math.sqrt(1.0 - amp_A * math.cos(om * t)) / (s ** 0.5)


def thacker_reference(
    a_m: float, h0_m: float, amp_A: float, *, g: float = THACKER_GRAVITY
) -> dict[str, Any]:
    """The closed-form reference scalars the V&V compares the numerical run to."""
    om = _omega(a_m, h0_m, g)
    k = (1.0 + amp_A) / (1.0 - amp_A)
    return {
        "gravity": float(g),
        "omega": float(om),
        "period_s": float(2.0 * math.pi / om),
        "eta_center_max_m": float(h0_m * (math.sqrt(k) - 1.0)),
        "eta_center_min_m": float(h0_m * (1.0 / math.sqrt(k) - 1.0)),
        "eta_center_amplitude_m": float(h0_m * (math.sqrt(k) - 1.0 / math.sqrt(k))),
        "r_shore_min_m": float(a_m * k ** (-0.25)),
        "r_shore_max_m": float(a_m * k ** (0.25)),
    }
