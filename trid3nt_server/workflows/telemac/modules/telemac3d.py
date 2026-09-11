"""The TELEMAC-3D wrapper: its dictionary, its composites, and its outputs.

The wrapper asserts NO value of its own. The VERTICAL GRID and the COLUMN read
the SAME three numbers through the SAME planner, so the thermocline thickness the
Fortran writes and the layer thickness the grid achieves cannot disagree."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from trid3nt_server.workflows.runtime import DeclarativeError

from .module import Module
from .outputs import PRIMITIVES, read_column

__all__ = ["T3D", "Column", "USER_FORTRAN_DIR", "VARIABLES",
           "VerticalGridUnresolved", "VerticalGrid", "plan_vertical_grid"]

#: The module's variable vocabulary, by the mnemonic VARIABLES FOR 3D GRAPHIC
#: PRINTOUTS spells: the result-file name and the unit. A 3D variable is read on
#: one plane, bottom first; ``T<n>`` is the n-th NAMES OF TRACERS entry.
VARIABLES: Mapping[str, tuple[str, str]] = MappingProxyType({
    "Z": ("ELEVATION Z", "M"), "U": ("VELOCITY U", "M/S"),
    "V": ("VELOCITY V", "M/S"), "W": ("VELOCITY W", "M/S"),
})


class VerticalGridUnresolved(DeclarativeError):
    """No admissible sigma grid can hold the column this run declares."""

    error_code = "TELEMAC3D_VERTICAL_UNRESOLVED"


#: The directory the engine compiles when a run patches its own Fortran. The
#: keyword names a DIRECTORY, so the manifest channel and the steering statement
#: are the same word rather than two derivations of it.
USER_FORTRAN_DIR = "user_fortran"
_CONDI_SOURCE = f"{USER_FORTRAN_DIR}/user_condi3d_trac.f"

#: Bottom stretching coefficient. Held fixed: a thermocline is a SURFACE feature,
#: so the plan spends its freedom on the surface coefficient and keeps a mild
#: bed-boundary-layer clustering.
_STRETCH_BOTTOM = 1.0
#: Numerical ceiling on the surface coefficient - past this the tanh saturates and
#: the top layers collapse to millimetres.
_STRETCH_SURFACE_MAX = 8.0
#: Ceiling on the thickness ratio between adjacent layers. A terrain-following
#: vertical grid whose layers grow faster than this makes the vertical advection
#: and diffusion operators inconsistent, so a stretch that would need it is a
#: REFUSAL rather than a silently distorted grid.
_STRETCH_MAX_GROWTH = 2.0
#: The near-surface layer must be no thicker than this fraction of the thermocline
#: depth, else the epilimnion is a single node and the column is unrepresentable.
_DZ_FRACTION = 0.5

#: The dictionary's own sigma transformations, at INDEX 96 of the TELEMAC-3D
#: dictionary: 1 = uniform planes, 4 = sigma with a zoom factor. Only 4 reads
#: MESH STRETCHING COEFFICIENTS.
_UNIFORM_SIGMA, _ZOOMED_SIGMA = 1, 4


def _sigma_planes(nplan: int, dl: float, du: float) -> Any:
    """The plane distribution ``condim.f`` builds, bed(0) -> free surface(1).

    Both coefficients at zero is the uniform sigma the solver falls back to."""
    import numpy as np

    # ``ZSTAR(k) = (TANH((DL+DU)*s - DL) + TANH(DL)) / (TANH(DL) + TANH(DU))``
    # over ``s = (k-1)/(NPLAN-1)``.
    s = np.arange(int(nplan), dtype=float) / max(int(nplan) - 1, 1)
    if dl <= 0.0 and du <= 0.0:
        return s
    return (np.tanh((dl + du) * s - dl) + np.tanh(dl)) / (np.tanh(dl) + np.tanh(du))


def _stretch_stats(nplan: int, dl: float, du: float,
                   depth_m: float) -> tuple[float, float]:
    """``(near-surface layer thickness, worst adjacent growth ratio)`` in metres."""
    import numpy as np

    dz = np.diff(_sigma_planes(nplan, dl, du)) * float(depth_m)
    growth = float(np.max(dz[:-1] / dz[1:])) if dz.size > 1 else 1.0
    return float(dz[-1]), growth


def _min_surface_coef(nplan: int, depth_m: float, dz_target_m: float) -> float | None:
    """The SMALLEST surface coefficient reaching ``dz_target_m``, or ``None``.

    Near-surface thickness falls monotonically with it, so bisection is exact."""
    lo, hi = 0.0, _STRETCH_SURFACE_MAX
    if _stretch_stats(nplan, _STRETCH_BOTTOM, hi, depth_m)[0] > dz_target_m:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if _stretch_stats(nplan, _STRETCH_BOTTOM, mid, depth_m)[0] <= dz_target_m:
            hi = mid
        else:
            lo = mid
    return hi


def _min_planes_for(depth_m: float, thermocline_depth_m: float,
                    cap: int = 200) -> int:
    """The fewest levels an admissible stretch exists for - the refusal's advice."""
    target = _DZ_FRACTION * float(thermocline_depth_m)
    for n in range(5, cap + 1):
        if float(depth_m) / (n - 1) <= target:
            return n
        du = _min_surface_coef(n, depth_m, target)
        if du is not None and _stretch_stats(
                n, _STRETCH_BOTTOM, du, depth_m)[1] <= _STRETCH_MAX_GROWTH:
            return n
    return cap


def plan_vertical_grid(nplan: int, max_depth_m: float,
                       thermocline_depth_m: float) -> dict[str, Any]:
    """The vertical discretisation that can HOLD the declared column, or refuse.

    A uniform sigma spreads the levels evenly, so a deep lake against a
    metres-thick thermocline gets a ONE-NODE epilimnion and the initial condition
    is unrepresentable; transformation 4 clusters the planes with the GOTM tanh
    stretch instead, and this reproduces that exact transform.

    What comes back is the keyword pair the grid IS, the achieved near-surface
    layer thickness, and the thermocline THICKNESS the initial condition must
    use, sized off that achieved layer. No admissible stretch REFUSES.
    """
    nplan = int(nplan)
    depth = float(max_depth_m)
    thermocline = float(thermocline_depth_m)
    uniform = depth / max(nplan - 1, 1)
    target = _DZ_FRACTION * thermocline

    if uniform <= target:
        dl = du = 0.0
        transformation = _UNIFORM_SIGMA
        dz_surface, growth = uniform, 1.0
    else:
        dl, du = _STRETCH_BOTTOM, _min_surface_coef(nplan, depth, target)
        dz_surface, growth = ((0.0, float("inf")) if du is None
                              else _stretch_stats(nplan, dl, du, depth))
        if du is None or growth > _STRETCH_MAX_GROWTH:
            raise VerticalGridUnresolved(
                f"{nplan} horizontal levels cannot resolve a {thermocline:g} m "
                f"thermocline over a {depth:.1f} m column: uniform sigma gives a "
                f"{uniform:.1f} m near-surface layer (target <= {target:g} m), and "
                "the surface zooming that would reach it needs a layer-growth "
                f"ratio of {growth:.2f} (limit {_STRETCH_MAX_GROWTH:g}). Raise "
                f"nplan to at least {_min_planes_for(depth, thermocline)}, or "
                "model a shallower domain or a thicker thermocline.")
        transformation = _ZOOMED_SIGMA
    return {
        "mesh_transformation": transformation,
        "mesh_stretching_coefficients": ([round(dl, 4), round(du, 4)]
                                         if transformation == _ZOOMED_SIGMA
                                         else None),
        "dz_surface_m": round(dz_surface, 3),
        "dz_uniform_m": round(uniform, 3),
        "layer_growth_ratio": round(growth, 3),
        "thermocline_delta_m": round(2.0 * dz_surface, 3),
        "label": (
            f"{nplan} sigma planes, near-surface layer {dz_surface:.2f} m over the "
            f"{depth:.1f} m deep column"
            + ("" if transformation == _UNIFORM_SIGMA else
               f" (surface-zoomed sigma, stretching {dl:g}/{du:.2f}; uniform sigma "
               f"would have been {uniform:.1f} m)")
            + f"; thermocline declared {2.0 * dz_surface:.2f} m thick"),
    }


def VerticalGrid(*, levels: Any, max_depth_m: Any,  # noqa: N802
                 thermocline_depth_m: Any) -> Mapping[str, Any]:
    """The sigma grid, as the three numbers that decide it."""
    return MappingProxyType({"levels": levels, "max_depth_m": max_depth_m,
                             "thermocline_depth_m": thermocline_depth_m})


def _vertical_grid(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                      Mapping[str, Any]]:
    """The planned grid -> the transformation keyword and its coefficients.

    Uniform sigma is the dictionary's own, so a run needing none states NOTHING."""
    plan = plan_vertical_grid(int(value["levels"]), float(value["max_depth_m"]),
                              float(value["thermocline_depth_m"]))
    if plan["mesh_transformation"] == _UNIFORM_SIGMA:
        return ({}, {})
    return ({"MESH_TRANSFORMATION": plan["mesh_transformation"],
             "MESH_STRETCHING_COEFFICIENTS": [
                 float(c) for c in plan["mesh_stretching_coefficients"]]}, {})


def Column(*, levels: Any, max_depth_m: Any,  # noqa: N802
           thermocline_depth_m: Any, warm_c: Any, cold_c: Any,
           surface_m: Any) -> Mapping[str, Any]:
    """The water column this run OPENS with: warm over cold, joined at a depth.

    ``surface_m`` is the free-surface elevation the thermocline depth is under."""
    return MappingProxyType({"levels": levels, "max_depth_m": max_depth_m,
                             "thermocline_depth_m": thermocline_depth_m,
                             "warm_c": warm_c, "cold_c": cold_c,
                             "surface_m": surface_m})


def _column(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                               Mapping[str, Any]]:
    """The declared column -> the initial-condition hook the engine compiles.

    No keyword carries a non-uniform initial tracer field; the hook does."""
    plan = plan_vertical_grid(int(value["levels"]), float(value["max_depth_m"]),
                              float(value["thermocline_depth_m"]))
    return ({"FORTRAN_FILE": USER_FORTRAN_DIR},
            {_CONDI_SOURCE: _condi_thermocline(
                float(value["thermocline_depth_m"]), float(value["warm_c"]),
                float(value["cold_c"]), float(plan["thermocline_delta_m"]),
                float(value["surface_m"]))})


_CONDI_HEAD = """!                   ****************************
                    SUBROUTINE USER_CONDI3D_TRAC
!                   ****************************
      USE BIEF
      USE INTERFACE_TELEMAC3D, EX_USER_CONDI3D_TRAC => USER_CONDI3D_TRAC
      USE DECLARATIONS_TELEMAC3D
      IMPLICIT NONE
      INTEGER I3
      DOUBLE PRECISION DPTH
"""
_CONDI_TAIL = """      RETURN
      END
"""


def _condi_thermocline(depth_m: float, warm_c: float, cold_c: float,
                       delta_m: float, surface_m: float) -> str:
    """A warm epilimnion over a cold hypolimnion, joined by a tanh thermocline.

    ``T = Tc + (Tw - Tc) * 0.5 * (1 - TANH((DPTH - DTHERM)/DELTA))``."""
    # The tanh resolves whenever DELTA is at least twice the near-surface layer
    # and the column anomaly then converges, which is why ``delta_m`` comes from
    # the grid plan and never from a literal. ``Z`` is on the same datum the free
    # surface was initialized on, so the depth below the water top is ``ZS - Z``.
    return (_CONDI_HEAD
            + "      DO I3=1,NPOIN3\n"
            + f"        DPTH={surface_m:.4f}D0-Z(I3)\n"
            + f"        TA%ADR(1)%P%R(I3)={cold_c:.4f}D0+"
              f"({warm_c - cold_c:.4f}D0)*0.5D0*\n"
            + f"     &    (1.D0-TANH((DPTH-{depth_m:.4f}D0)/{delta_m:.4f}D0))\n"
            + "      ENDDO\n" + _CONDI_TAIL)


def Wind(*, speed_mps: Any, from_deg: Any) -> Mapping[str, Any]:  # noqa: N802
    """A steady wind, stated the way weather states one: where it blows FROM."""
    return MappingProxyType({"speed_mps": speed_mps, "from_deg": from_deg})


def _wind(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """A from-direction -> the velocity components the engine reads.

    Meteorological FROM, engine TOWARD, in the mesh's own frame."""
    import math

    if not value["speed_mps"]:
        # A wind of no speed is not a wind. Stating one would put the whole wind
        # block in the deck for a run nobody asked a wind about.
        return ({}, {})
    speed = float(value["speed_mps"])
    theta = math.radians(float(value["from_deg"]))
    return ({"WIND": True,
             "WIND_VELOCITY_ALONG_X": -speed * math.sin(theta),
             "WIND_VELOCITY_ALONG_Y": -speed * math.cos(theta)}, {})


T3D = Module("telemac3d")
T3D.VARIABLES = VARIABLES
T3D.composites(vertical_grid=_vertical_grid, column=_column, wind=_wind)
T3D.outputs(**PRIMITIVES, column=read_column)
