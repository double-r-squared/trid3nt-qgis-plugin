"""Published regressions of rupture size on moment magnitude, and the slip a moment makes over a rectangle.

Each relation is one table of one paper: ``log10(size_km) = a + b * Mw`` with the magnitude range the
paper's regression was fitted over. A magnitude outside that range refuses by name; nothing is extrapolated.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from ..errors import GeoclawError

__all__ = ["RELATIONS", "rupture_dimensions", "slip_for_moment"]

_WC = ("Wells and Coppersmith (1994), Bull. Seismol. Soc. Am. 84(4):974-1002, Table 2A, p. 990: "
       "subsurface rupture length RLD and downdip rupture width RW, log10(km) = a + b * M, {kind} slip")

#: relation name -> (citation, (a, b, Mmin, Mmax) for length, (a, b, Mmin, Mmax) for width).
RELATIONS: Mapping[str, tuple[str, tuple[float, ...], tuple[float, ...]]] = MappingProxyType({
    "wells_coppersmith_1994_strike_slip": (_WC.format(kind="strike-slip"), (-2.57, 0.62, 4.8, 8.1), (-0.76, 0.27, 4.8, 8.1)),
    "wells_coppersmith_1994_reverse": (_WC.format(kind="reverse"), (-2.42, 0.58, 4.8, 7.6), (-1.61, 0.41, 4.8, 7.6)),
    "wells_coppersmith_1994_normal": (_WC.format(kind="normal"), (-1.88, 0.50, 5.2, 7.3), (-1.14, 0.35, 5.2, 7.3)),
    "wells_coppersmith_1994_all": (_WC.format(kind="all-slip-type"), (-2.44, 0.59, 4.8, 8.1), (-1.01, 0.32, 4.8, 8.1)),
})


def rupture_dimensions(relation: str, mw: float) -> tuple[float, float]:
    """The named relation at ``mw`` -> (length, width) in metres; an unknown name or a magnitude outside its fitted range refuses."""
    if relation not in RELATIONS:
        raise GeoclawError(f"scaling relation {relation!r} is none built: {', '.join(RELATIONS)}.",
                           error_code="GEOCLAW_SCALING_RELATION")
    citation, length, width = RELATIONS[relation]
    out = []
    for name, (a, b, low, high) in (("length", length), ("width", width)):
        if not low <= mw <= high:
            raise GeoclawError(f"Mw {mw:.2f} is outside the {relation} {name} regression's fitted range "
                               f"{low:g} to {high:g} ({citation}); it is not extrapolated.",
                               error_code="GEOCLAW_SCALING_RANGE")
        out.append(10.0 ** (a + b * mw) * 1000.0)
    return out[0], out[1]


def slip_for_moment(moment_nm: float, rigidity_pa: float, length_m: float, width_m: float) -> float:
    """Uniform slip in metres that a scalar moment makes over a rectangle: M0 / (mu L W)."""
    return moment_nm / (rigidity_pa * length_m * width_m)
