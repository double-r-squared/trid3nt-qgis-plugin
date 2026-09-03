"""TELEMAC-3D stratified / 3D-hydrodynamics pipeline.

The productionized promotion of ``docs/proof/templates/telemac3d_sandbox.py``
(the canonical composer prototype whose physics is PROVEN through the baked
telemac3d binary in ``trid3nt-local/telemac:latest``). TELEMAC-3D solves the
three-dimensional (hydrostatic or non-hydrostatic) Navier-Stokes equations with
active-tracer (temperature / salinity) baroclinic coupling - the physics a 2D
depth-averaging structurally cannot resolve. Runs INSIDE the worker image (needs
the baked ``telemac3d`` binary + the opentelemac SELAFIN python API); imports NO
agent code.

Three question classes (the board's TELEMAC-3D rows), each a mode - each a
discriminating 3D-vs-2D or stratified-vs-mixed pair:

  * ``stratification``   - a lake initialised with a warm epilimnion over a cold
                           hypolimnion (DENSITY LAW 1, freshwater rho max near
                           4 C) either KEEPS its thermocline (calm) or has it
                           eroded by wind-shear turbulence (windy). The
                           discriminating metric is the persisting top-to-bottom
                           temperature difference (the lake-turnover question,
                           the stratified 3D column the AED2 STOP needs).
                           The run carries NO surface heat exchange (no THERMIC,
                           no met forcing): heat is CONSERVED and the only thing
                           that can change the column is REDISTRIBUTION. A falling
                           surface temperature is the warm layer MIXING DOWNWARD,
                           never the lake cooling - ``column_heat_*`` in the
                           metrics is the drift that proves it.
  * ``wind_circulation`` - a steady wind over a closed basin drives surface water
                           downwind; mass conservation forces a return flow at
                           depth. The vertical U profile at mid-basin (surface
                           downwind, bottom upwind, depth-integrated ~0) is
                           INVISIBLE to a 2D depth-averaged model. THE 3D-vs-2D
                           discriminant.
  * ``salt_wedge``       - a dense (saline) column released against a light column
                           produces a bottom gravity current whose front advances
                           at the Benjamin (1968) energy-conserving speed
                           0.5*sqrt(g'H); density-ON produces a current, OFF does
                           not (the salt-wedge / density-driven estuary physics).
                           Hydrostatic vs non-hydrostatic is the dam-break-3D
                           fidelity rung.

Two bathymetry paths (stratification / wind_circulation):
  * ``idealized`` (default) - the geography-free closed basin the sandbox proved
    (replicates the classic TELEMAC-3D validation set: wind-driven closed basin,
    thermal stratification; clears the citations law like the GWE analytic V&V).
  * ``noaa_greatlakes`` - a REAL US Great Lake AOI whose node bed is sampled from
    the NOAA NGDC ``DEM_all`` ImageServer (the ``greatlakes_lakedatum``
    bathymetry, verified for the TOMAWAC / ARTEMIS legs). A deep Great Lake
    (Superior / Michigan / Huron) stratifies; the vertical profile over real
    bathymetry IS the proof-norm-#9 discriminant. ``salt_wedge`` stays idealized
    (a real estuary needs a tidal liquid boundary - out of the closed-basin
    archetype), labeled as such.

ALL EIGHT steering gotchas  are baked here (see write_cas / the
CONDI3D fortran authors): mandatory scalar ``INITIAL VALUES OF TRACERS`` beside
the USER_CONDI3D_TRAC override, TEMPERATURE/SALINIT name-prefix indexing,
density-law selection, CONDI3D 3D-coord (bed-referenced Z) semantics, the 2D
``.cli`` extruded over the planes, explicit ``NON-HYDROSTATIC VERSION``, and
``VERTICAL TURBULENCE MODEL`` per mode.

ASCII only. No product/agent code touched.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

LOG = logging.getLogger("trid3nt.worker.telemac3d.build")

G = 9.81

# ---------------------------------------------------------------------------
# Config (strict-field manifest; the entrypoint rejects unknown keys)
# ---------------------------------------------------------------------------
@dataclass
class Telemac3dConfig:
    name: str = "telemac3d_stratified_flow"
    #: question class: stratification | wind_circulation | salt_wedge
    flow_mode: str = "stratification"
    #: bathymetry path: idealized | noaa_greatlakes (salt_wedge is idealized-only)
    bathy_source: str = "idealized"
    #: real-bathy AOI (min_lon, min_lat, max_lon, max_lat), EPSG:4326
    bbox: tuple = None                  # type: ignore[assignment]
    #: mesh/grid knob: target horizontal node spacing in metres.
    target_resolution_m: float = None   # type: ignore[assignment]
    #: number of horizontal levels (NPLAN, the sigma planes = the 3D DOF).
    nplan: int = 13
    #: wind forcing (stratification / wind_circulation). speed 0 -> calm (no wind).
    wind_speed_mps: float = 0.0
    #: meteorological direction the wind blows FROM (compass, 0=N/90=E/270=W).
    wind_dir_from_deg: float = 270.0    # from the west -> blows toward +X (east)
    #: non-hydrostatic knob (the dam-break-3D fidelity rung; salt_wedge only).
    non_hydrostatic: bool = False
    # --- thermal stratification knobs ---
    #: still-water depth for the idealized stratification basin (m, positive).
    strat_depth_m: float = 20.0
    #: warm epilimnion temperature (C) above the thermocline.
    warm_temp_c: float = 25.0
    #: cold hypolimnion temperature (C) below the thermocline.
    cold_temp_c: float = 15.0
    #: thermocline depth below the still-water surface (m).
    thermocline_depth_m: float = 8.0
    #: simulated duration (hours) for stratification / wind.
    duration_hours: float = 5.0
    #: real-bathy: nodes shallower than this (or land / NaN) are clamped to a wet
    #: bed at this depth so the closed basin is entirely wet (labeled).
    min_depth_m: float = 5.0
    # --- salt_wedge (lock-exchange) knobs ---
    #: lock-exchange channel salinity of the dense half (density law 2 units).
    lock_salinity: float = 26.7
    #: lock-exchange channel depth (m).
    lock_depth_m: float = 1.0
    workdir: str = dataclasses.field(
        default_factory=lambda: os.path.dirname(os.path.abspath(__file__)))


#: 2D node-count ceiling for a single local-docker TELEMAC-3D grid. The 3D node
#: count is NPOIN2 * NPLAN, so a tight 2D cap keeps the solve to minutes.
GRID_NODE_CAP: int = 4000
#: absolute floor on the horizontal grid spacing (below this the solve degrades).
GRID_H_FLOOR_M: float = 400.0


class Telemac3dInputError(RuntimeError):
    """A 3D-input problem gated before/at the solve (typed error_code)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# 0. Vertical discretisation planner (MESH TRANSFORMATION 1 vs 4).
#
# A uniform sigma (MESH TRANSFORMATION 1) spreads NPLAN planes evenly, so the
# near-surface layer thickness over the deepest column is depth/(NPLAN-1). A deep
# lake against a metres-thick thermocline therefore gets a ONE-NODE epilimnion and
# the initial condition is unrepresentable no matter how the profile is written.
# MESH TRANSFORMATION 4 (verified in the baked telemac3d.dico, INDEX 96: "4: sigma
# with zoom factor") clusters planes with the GOTM tanh stretch steered by MESH
# STRETCHING COEFFICIENTS (dico 'COEFFICIENTS D ETIREMENT DU MAILLAGE', REAL x2,
# MNEMO TRANSF_COEF, "only used with MESH TRANSFORMATION = 4"; first = stretching
# near the BOTTOM, second = near the FREE SURFACE). condim.f implements it as
#
#     ZSTAR(k) = (TANH((DL+DU)*s - DL) + TANH(DL)) / (TANH(DL) + TANH(DU))
#     s = (k-1)/(NPLAN-1),  ZSTAR 0 = bed .. 1 = free surface
#
# so the planner below reproduces that exact transform to solve for the surface
# coefficient and to REPORT the achieved layer thickness as a fidelity fact.
# ---------------------------------------------------------------------------
#: Bottom stretching coefficient. Held fixed: the thermocline is a SURFACE
#: feature, so the solve spends its freedom on the surface coefficient and keeps a
#: mild bed-boundary-layer clustering.
VSTRETCH_BOTTOM_COEF: float = 1.0
#: Numerical ceiling on the surface coefficient (past this the tanh saturates and
#: the top layers collapse to millimetres).
VSTRETCH_SURFACE_MAX: float = 8.0
#: Ceiling on the thickness ratio between adjacent layers. A terrain-following
#: vertical grid whose layers grow faster than this makes the vertical advection /
#: diffusion operators inconsistent, so a stretch that would need it is a REFUSAL,
#: not a silently-distorted grid.
VSTRETCH_MAX_GROWTH: float = 2.0
#: The near-surface layer must be no thicker than this fraction of the thermocline
#: depth, else the epilimnion is a single node and the IC is unrepresentable.
VSTRETCH_DZ_FRACTION: float = 0.5


def sigma_planes(nplan: int, dl: float, du: float) -> "np.ndarray":
    """The condim.f MESH TRANSFORMATION 4 plane distribution, bed(0) -> surface(1).

    ``dl <= 0 and du <= 0`` is the uniform sigma the solver falls back to."""
    s = np.arange(int(nplan), dtype=np.float64) / max(int(nplan) - 1, 1)
    if dl <= 0.0 and du <= 0.0:
        return s
    return (np.tanh((dl + du) * s - dl) + np.tanh(dl)) / (np.tanh(dl) + np.tanh(du))


def _layer_thicknesses(nplan: int, dl: float, du: float, depth_m: float):
    """Layer thicknesses bed -> surface (m) over a column of ``depth_m``."""
    return np.diff(sigma_planes(nplan, dl, du)) * float(depth_m)


def _stretch_stats(nplan: int, dl: float, du: float, depth_m: float):
    dz = _layer_thicknesses(nplan, dl, du, depth_m)
    growth = float(np.max(dz[:-1] / dz[1:])) if dz.size > 1 else 1.0
    return float(dz[-1]), growth


def _min_surface_coef(nplan: int, depth_m: float, dz_target_m: float):
    """Smallest surface stretching coefficient reaching ``dz_target_m``, or None.

    Minimum-distortion by construction: the near-surface thickness falls
    monotonically with the coefficient, so the bisection returns the LEAST stretch
    that meets the target rather than starving the interior."""
    lo, hi = 0.0, VSTRETCH_SURFACE_MAX
    if _stretch_stats(nplan, VSTRETCH_BOTTOM_COEF, hi, depth_m)[0] > dz_target_m:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if _stretch_stats(nplan, VSTRETCH_BOTTOM_COEF, mid, depth_m)[0] <= dz_target_m:
            hi = mid
        else:
            lo = mid
    return hi


def plan_vertical_grid(nplan: int, max_depth_m: float, thermocline_depth_m: float):
    """Pick the vertical discretisation that can HOLD a thermocline at
    ``thermocline_depth_m`` over a column of ``max_depth_m``, or refuse.

    Returns a dict of declared fidelity facts (transformation keyword value, the
    stretching coefficients, the ACHIEVED near-surface layer thickness, the
    thermocline thickness DELTA the initial condition must use). Raises
    ``Telemac3dInputError('TELEMAC3D_VERTICAL_UNRESOLVED')`` when no admissible
    stretch reaches the target - the grid must never silently carry an initial
    condition it cannot represent."""
    nplan = int(nplan)
    depth = float(max_depth_m)
    dtherm = float(thermocline_depth_m)
    dz_uniform = depth / max(nplan - 1, 1)
    dz_target = VSTRETCH_DZ_FRACTION * dtherm

    if dz_uniform <= dz_target:
        dl = du = 0.0
        transf = 1
        dz_surface, growth = dz_uniform, 1.0
    else:
        du = _min_surface_coef(nplan, depth, dz_target)
        dl = VSTRETCH_BOTTOM_COEF
        if du is not None:
            dz_surface, growth = _stretch_stats(nplan, dl, du, depth)
        if du is None or growth > VSTRETCH_MAX_GROWTH:
            need = _min_planes_for(depth, dtherm)
            raise Telemac3dInputError(
                "TELEMAC3D_VERTICAL_UNRESOLVED",
                f"{nplan} horizontal levels cannot resolve a {dtherm:g} m "
                f"thermocline over a {depth:.1f} m column: uniform sigma gives a "
                f"{dz_uniform:.1f} m near-surface layer (target <= {dz_target:g} m), "
                "and the surface zooming that would reach it needs a layer-growth "
                f"ratio of {(growth if du is not None else float('inf')):.2f} "
                f"(limit {VSTRETCH_MAX_GROWTH:g}). Raise nplan to at least "
                f"{need} or model a shallower AOI / a thicker thermocline.")
        transf = 4
    delta = 2.0 * dz_surface
    z = sigma_planes(nplan, dl, du)
    depths = (1.0 - z) * depth
    return {
        "mesh_transformation": transf,
        "mesh_stretching_coefficients": ([round(dl, 4), round(du, 4)]
                                         if transf == 4 else None),
        "vertical_dz_surface_m": round(dz_surface, 3),
        "vertical_dz_uniform_m": round(dz_uniform, 3),
        "vertical_layer_growth_ratio": round(growth, 3),
        "vertical_planes_above_thermocline": int((depths < dtherm).sum()),
        "vertical_sigma_planes": np.round(z, 5).tolist(),
        "thermocline_delta_m": round(delta, 3),
        "vertical_resolution_label": (
            f"{nplan} sigma planes, near-surface layer {dz_surface:.2f} m over the "
            f"{depth:.1f} m deep column"
            + ("" if transf == 1 else
               f" (surface-zoomed sigma, stretching {dl:g}/{du:.2f}; uniform sigma "
               f"would have been {dz_uniform:.1f} m)")
            + f"; thermocline declared {delta:.2f} m thick"),
    }


def _min_planes_for(depth_m: float, thermocline_depth_m: float, cap: int = 200):
    """Fewest horizontal levels an admissible stretch exists for (refusal advice)."""
    dz_target = VSTRETCH_DZ_FRACTION * float(thermocline_depth_m)
    for n in range(5, cap + 1):
        if float(depth_m) / (n - 1) <= dz_target:
            return n
        du = _min_surface_coef(n, depth_m, dz_target)
        if du is not None and _stretch_stats(
                n, VSTRETCH_BOTTOM_COEF, du, depth_m)[1] <= VSTRETCH_MAX_GROWTH:
            return n
    return cap


# ---------------------------------------------------------------------------
# 1. Idealized regular-grid 2D triangular mesh (CCW ring, rank IPOBO) - promoted
#    from the sandbox. TELEMAC-3D reads a 2D geometry + 2D boundary file and
#    extrudes NPLAN horizontal levels internally (the sigma transform).
# ---------------------------------------------------------------------------
def build_grid(Lx, Ly, dx, depth_fn):
    nx = int(round(Lx / dx)) + 1
    ny = int(round(Ly / dx)) + 1
    xs = np.linspace(0.0, Lx, nx)
    ys = np.linspace(0.0, Ly, ny)
    X = np.repeat(xs, ny)                      # node n = i*ny + j
    Y = np.tile(ys, nx)
    npoin = nx * ny

    def nid(i, j):
        return i * ny + j

    tris = []
    for i in range(nx - 1):
        for j in range(ny - 1):
            a, b, c, d = nid(i, j), nid(i + 1, j), nid(i + 1, j + 1), nid(i, j + 1)
            tris.append((a, b, c))
            tris.append((a, c, d))
    ikle = np.array(tris, dtype=np.int32)

    ring = []
    for i in range(nx - 1):
        ring.append(nid(i, 0))
    for j in range(ny - 1):
        ring.append(nid(nx - 1, j))
    for i in range(nx - 1, 0, -1):
        ring.append(nid(i, ny - 1))
    for j in range(ny - 1, 0, -1):
        ring.append(nid(0, j))
    ring = np.array(ring, dtype=np.int32)
    nptfr = len(ring)

    ipob = np.zeros(npoin, dtype=np.int32)
    for rank, n in enumerate(ring, start=1):
        ipob[n] = rank

    Z = depth_fn(X, Y).astype(np.float64)      # BOTTOM elevation (neg = below datum)
    return dict(X=X.astype(np.float64), Y=Y.astype(np.float64), ikle=ikle,
                ipob=ipob, ring=ring, nptfr=nptfr, npoin=npoin, nx=nx, ny=ny,
                xs=xs, ys=ys, Z=Z, Lx=Lx, Ly=Ly, dx=dx)


def write_slf(mesh, path, values=None, varname="BOTTOM          "):
    """Write the 2D mesh as a single-frame SELAFIN. ``values`` defaults to the bed
    Z (BOTTOM); pass a field + varname to re-emit a surface/bottom layer for the
    COG (the artemis single-frame re-emit pattern)."""
    from data_manip.extraction.telemac_file import TelemacFile
    if os.path.exists(path):
        os.remove(path)
    vals = mesh["Z"] if values is None else np.asarray(values, dtype=np.float64)
    tf = TelemacFile(path, access="w")
    tf.add_header("TELEMAC3D " + os.path.basename(path),
                  date=np.array([2026, 8, 13, 0, 0, 0]))
    tf.add_mesh(mesh["X"], mesh["Y"], mesh["ikle"], z=mesh["Z"])
    tf._ipob3 = mesh["ipob"].astype(np.int32)
    tf._ipob2 = tf._ipob3
    tf._nptfr = int(mesh["nptfr"])
    tf._nbor = mesh["ring"].astype(np.int32)
    tf._knolg = np.arange(1, mesh["npoin"] + 1, dtype=np.int32)
    tf.add_variable(varname, "                ")
    tf.add_data_value(varname, 0, vals)
    tf.write()
    tf.close()


def write_cli(mesh, path):
    """All-solid closed basin (T2D 13-column boundary format, which TELEMAC-3D
    reads as the horizontal boundary + extrudes over the planes). LIHBOR=LIUBOR=
    LIVBOR=LITBOR=2 -> solid wall everywhere (gotcha 5: every case is a closed
    initial-value problem - a gravity current, a wind gyre, a stratified column -
    so no liquid boundary is needed; the bed/surface are governed by keywords)."""
    ring = mesh["ring"]
    lines = []
    for k in range(mesh["nptfr"]):
        node1 = int(ring[k]) + 1
        rank = k + 1
        lih = liu = liv = lit = 2
        lines.append(
            f"{lih} {liu} {liv}  0.000 0.000 0.000 0.000  {lit}  0.000 0.000 0.000 "
            f"{node1:>11d} {rank:>11d}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# 2. USER_CONDI3D_TRAC - non-uniform initial tracer field (gotcha 4: X/Y/Z are the
#    NPOIN3 3D node coords, Z is a bed-referenced elevation populated by CALCOT
#    before this hook; TA%ADR(itrac)%P%R(i3) is tracer i3).
# ---------------------------------------------------------------------------
CONDI_HEAD = """!                   ****************************
                    SUBROUTINE USER_CONDI3D_TRAC
!                   ****************************
      USE BIEF
      USE INTERFACE_TELEMAC3D, EX_USER_CONDI3D_TRAC => USER_CONDI3D_TRAC
      USE DECLARATIONS_TELEMAC3D
      IMPLICIT NONE
      INTEGER I3
      DOUBLE PRECISION DPTH
"""
CONDI_TAIL = """      RETURN
      END
"""


def condi_lock(xgate, slock):
    # dense saline column for X < XGATE, fresh for X >= XGATE
    return (CONDI_HEAD +
            f"      DO I3=1,NPOIN3\n"
            f"        IF(X(I3).LT.{xgate:.4f}D0) THEN\n"
            f"          TA%ADR(1)%P%R(I3)={slock:.4f}D0\n"
            f"        ELSE\n"
            f"          TA%ADR(1)%P%R(I3)=0.D0\n"
            f"        ENDIF\n"
            f"      ENDDO\n" + CONDI_TAIL)


def condi_thermocline(dtherm, twarm, tcold, delta):
    """Warm epilimnion over a cold hypolimnion, joined by a tanh thermocline of
    thickness ``delta`` centred at depth ``dtherm``.

    T = Tc + (Tw - Tc) * 0.5 * (1 - TANH((DPTH - DTHERM)/DELTA))

    A STEP profile is not representable on a sigma grid: its column heat anomaly
    is O(dz) wrong and the discretisation error reads as a physical signal. The
    tanh is resolved whenever DELTA >= 2*dz, and the anomaly then converges
    O(dz^2) - so ``delta`` MUST come from ``plan_vertical_grid`` (which sizes it
    off the ACHIEVED near-surface layer), never from a literal.

    Z is bed-referenced elevation; depth-below-surface = -Z."""
    return (CONDI_HEAD +
            f"      DO I3=1,NPOIN3\n"
            f"        DPTH=-Z(I3)\n"
            f"        TA%ADR(1)%P%R(I3)={tcold:.4f}D0+({twarm - tcold:.4f}D0)*0.5D0*\n"
            f"     &    (1.D0-TANH((DPTH-{dtherm:.4f}D0)/{delta:.4f}D0))\n"
            f"      ENDDO\n" + CONDI_TAIL)


# ---------------------------------------------------------------------------
# 3. TELEMAC-3D steering-file author (all 8 gotchas baked) - from the sandbox.
# ---------------------------------------------------------------------------
def write_cas(path, geo, cli, res3d, res2d, fort, *, title, nplan, dt, nit,
              graprd, denlaw=0, tracer_name=None, nonhyd=False,
              wind=False, wind_u=0.0, wind_v=0.0, iturbv=2,
              rho0=None, friction_coef=0.01, transf=1, transf_coef=None):
    L = []
    A = L.append
    A(f"TITLE : '{title}'")
    A(f"GEOMETRY FILE : '{geo}'")
    A(f"BOUNDARY CONDITIONS FILE : '{cli}'")
    if fort:
        A(f"FORTRAN FILE : '{fort}'")
    A(f"3D RESULT FILE : '{res3d}'")
    A(f"2D RESULT FILE : '{res2d}'")
    A("3D RESULT FILE FORMAT : 'SERAFIN'")
    A("2D RESULT FILE FORMAT : 'SERAFIN'")
    A("VARIABLES FOR 3D GRAPHIC PRINTOUTS : 'Z,U,V,W,TA1'")
    A("VARIABLES FOR 2D GRAPHIC PRINTOUTS : 'U,V,H,B'")
    A(f"TIME STEP : {dt}")
    A(f"NUMBER OF TIME STEPS : {nit}")
    A(f"GRAPHIC PRINTOUT PERIOD : {graprd}")
    A(f"LISTING PRINTOUT PERIOD : {max(1, nit // 10)}")
    A("MASS-BALANCE : YES")
    # --- vertical discretisation (sigma) ---
    A(f"NUMBER OF HORIZONTAL LEVELS : {nplan}")
    # dico INDEX 96: 1 = sigma (uniform planes), 4 = sigma with zoom factor.
    A(f"MESH TRANSFORMATION : {int(transf)}")
    if int(transf) == 4:
        # dico: MESH STRETCHING COEFFICIENTS (TRANSF_COEF, 2 REALs, > 0) is read
        # ONLY under MESH TRANSFORMATION 4; first = bottom, second = free surface.
        if not transf_coef or len(transf_coef) != 2:
            raise Telemac3dInputError(
                "TELEMAC3D_PARAMS_INVALID",
                "MESH TRANSFORMATION 4 needs two MESH STRETCHING COEFFICIENTS; "
                f"got {transf_coef!r}.")
        A("MESH STRETCHING COEFFICIENTS : "
          f"{float(transf_coef[0]):.4f};{float(transf_coef[1]):.4f}")
    # gotcha 6: NON-HYDROSTATIC VERSION defaults YES in the dico; set explicitly.
    A("NON-HYDROSTATIC VERSION : " + ("YES" if nonhyd else "NO"))
    # --- initial free surface at rest ---
    A("INITIAL CONDITIONS : 'CONSTANT ELEVATION'")
    A("INITIAL ELEVATION : 0.")
    # --- turbulence (gotcha 8) ---
    A("HORIZONTAL TURBULENCE MODEL : 1")     # constant viscosity
    A(f"VERTICAL TURBULENCE MODEL : {iturbv}")  # 1 const, 2 mixing length, 3 k-eps
    A("COEFFICIENT FOR HORIZONTAL DIFFUSION OF VELOCITIES : 1.E-4")
    A("COEFFICIENT FOR VERTICAL DIFFUSION OF VELOCITIES : 1.E-4")
    A("COEFFICIENT FOR HORIZONTAL DIFFUSION OF TRACERS : 1.E-4")
    A("COEFFICIENT FOR VERTICAL DIFFUSION OF TRACERS : 1.E-4")
    # --- bottom friction ---
    A("LAW OF BOTTOM FRICTION : 5")
    A(f"FRICTION COEFFICIENT FOR THE BOTTOM : {friction_coef}")
    # --- density / tracers (gotchas 1, 2, 3, 7) ---
    if tracer_name is not None:
        A("NUMBER OF TRACERS : 1")
        A(f"NAMES OF TRACERS : '{tracer_name}'")
        # gotcha 1: mandatory even when USER_CONDI3D_TRAC overrides it (the solver
        # PLANTEs on "GIVE THE KEY-WORD INITIAL VALUES OF TRACERS" without it).
        A("INITIAL VALUES OF TRACERS : 0.")
    A(f"DENSITY LAW : {denlaw}")
    if rho0 is not None:
        # gotcha 7: AVERAGE WATER DENSITY is valid (rhoref); MEAN TEMPERATURE is NOT.
        A(f"AVERAGE WATER DENSITY : {rho0}")
    # --- wind ---
    if wind:
        A("WIND : YES")
        A(f"WIND VELOCITY ALONG X : {wind_u}")
        A(f"WIND VELOCITY ALONG Y : {wind_v}")
        A("COEFFICIENT OF WIND INFLUENCE : 1.55E-6")
        A("THRESHOLD DEPTH FOR WIND : 1.")
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")


# ---------------------------------------------------------------------------
# 4. Run + read the 3D SELAFIN.
# ---------------------------------------------------------------------------
def run_t3d(cas, workdir, tag, timeout=3600):
    env = dict(os.environ)
    cmd = ["telemac3d.py", os.path.basename(cas), "--ncsize=1"]
    p = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True,
                       timeout=timeout)
    with open(os.path.join(workdir, f"t3d_{tag}.log"), "w") as f:
        f.write("CMD: " + " ".join(cmd) + "\n\nSTDOUT:\n" + p.stdout +
                "\n\nSTDERR:\n" + p.stderr)
    out = p.stdout + "\n" + p.stderr
    ok = "CORRECT END OF RUN" in out or p.returncode == 0
    return ok, out


def open_res(res_path):
    from data_manip.extraction.telemac_file import TelemacFile
    return TelemacFile(res_path)


def field_3d(tf, varname, rec):
    """Full NPOIN3 field at record ``rec`` reshaped (nplan, npoin2).
    3D node ordering is iplan*npoin2 + j, iplan 0=bottom .. nplan-1=surface."""
    data = np.asarray(tf.get_data_value(varname, rec))
    nplan = int(tf.nplan)
    npoin3 = data.shape[0]
    npoin2 = npoin3 // nplan
    return data.reshape(nplan, npoin2), npoin2, nplan


def benjamin_front_speed(drho_over_rho, H):
    gp = G * drho_over_rho
    return 0.5 * np.sqrt(gp * H)             # energy-conserving Benjamin front


# ---------------------------------------------------------------------------
# 5. Wind vector from a meteorological FROM-direction (compass -> UTM x/y).
# ---------------------------------------------------------------------------
def wind_components(speed, dir_from_deg):
    import math
    if speed <= 0:
        return 0.0, 0.0
    d_to = (dir_from_deg + 180.0) % 360.0
    trig = math.radians(90.0 - d_to)
    return round(speed * math.cos(trig), 4), round(speed * math.sin(trig), 4)


# ---------------------------------------------------------------------------
# 6. Real Great Lakes bathymetry -- READ from the staged raster.
# ---------------------------------------------------------------------------
def read_greatlakes_bathy(lon, lat, data_dir):
    """Bed elevation (m) at node lon/lat from the bed the run was staged with.

    A node outside the lake (land / NoData) is NaN. Raises Telemac3dInputError
    when no bed was staged at all, which is a staging fault rather than a
    coverage answer."""
    from _staged_bed import sample_staged_bed

    try:
        return sample_staged_bed(lon, lat, data_dir)
    except FileNotFoundError as exc:
        raise Telemac3dInputError("TELEMAC3D_BATHY_UNAVAILABLE", str(exc)) from exc


def _bbox_utm_epsg(bbox):
    lon = 0.5 * (bbox[0] + bbox[2])
    lat = 0.5 * (bbox[1] + bbox[3])
    zone = int((lon + 180.0) // 6.0) + 1
    return (32600 if lat >= 0 else 32700) + zone


def build_real_lake_grid(cfg: Telemac3dConfig, data_dir: str):
    """Regular UTM grid over a real Great Lake AOI with NOAA lake-datum bed at
    nodes. Land / NaN / too-shallow nodes are CLAMPED to a wet bed at
    ``min_depth_m`` (labeled) so the closed basin is ENTIRELY wet - a
    TELEMAC-3D closed basin with dry nodes would need tidal-flats treatment; the
    stratification proof lives in the deep interior. Returns (mesh, meta)."""
    from pyproj import Transformer

    bbox = cfg.bbox
    if not (bbox and len(bbox) == 4):
        raise Telemac3dInputError(
            "TELEMAC3D_PARAMS_INVALID",
            "noaa_greatlakes bathy_source needs a 4-value bbox (min_lon,min_lat,"
            f"max_lon,max_lat); got {bbox!r}.")
    epsg = _bbox_utm_epsg(bbox)
    tr = Transformer.from_crs(4326, epsg, always_xy=True)
    x0, y0 = tr.transform(bbox[0], bbox[1])
    x1, y1 = tr.transform(bbox[2], bbox[3])
    Lx = abs(x1 - x0)
    Ly = abs(y1 - y0)
    dx_req = float(cfg.target_resolution_m or 0.0)
    dx = max(dx_req, GRID_H_FLOOR_M) if dx_req > 0 else max(Lx, Ly) / 40.0
    dx = max(dx, GRID_H_FLOOR_M)
    coarsened = False
    while (int(Lx / dx) + 1) * (int(Ly / dx) + 1) > GRID_NODE_CAP:
        dx *= 1.15
        coarsened = True

    mesh = build_grid(Lx, Ly, dx, lambda X, Y: np.zeros_like(X))
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    xabs = mesh["X"] + min(x0, x1)
    yabs = mesh["Y"] + min(y0, y1)
    lon, lat = back.transform(xabs, yabs)
    bed = read_greatlakes_bathy(np.asarray(lon), np.asarray(lat), data_dir)
    min_depth = max(float(cfg.min_depth_m), 1.0)
    wet = np.isfinite(bed) & (bed < -min_depth)
    n_wet = int(wet.sum())
    if n_wet < 0.20 * bed.size:
        raise Telemac3dInputError(
            "TELEMAC3D_BATHY_UNAVAILABLE",
            f"the staged lake bathymetry covered only {n_wet}/{bed.size} grid nodes "
            f">{min_depth} m deep over {bbox} -- the AOI is mostly land/shallow. "
            "Pick a DEEP OPEN-WATER AOI inside a Great Lake (Superior/Michigan/"
            "Huron) that can stratify.")
    # clamp every node to at least min_depth below the datum (all-wet closed basin)
    Z = np.where(wet, bed, -min_depth)
    Z = np.minimum(Z, -min_depth)
    mesh["Z"] = Z.astype(np.float64)
    # The clamp is a SOLVER-STABILITY device, not a statement about the world: a
    # clamped node is land or shallows the DEM says carries no lake water. The mask
    # travels with the mesh so the published layers read NoData there instead of
    # painting land as warm water (honesty floor).
    mesh["wet"] = wet
    meta = dict(utm_epsg=epsg, dx_m=round(dx, 1), coarsened=coarsened,
                n_wet_nodes=n_wet, n_clamped_nodes=int(bed.size - n_wet),
                depth_max_m=round(float(-np.nanmin(bed[wet])), 1),
                depth_mean_m=round(float(-np.nanmean(bed[wet])), 1))
    return mesh, meta


# ---------------------------------------------------------------------------
# 7. Single-frame surface / bottom re-emit for the agent-side COG rasterizer.
# ---------------------------------------------------------------------------
def _emit_layer_fields(mesh, surface_vals, bottom_vals, data_dir, varname):
    """Re-emit the surface + bottom layers as single-frame 2D SELAFINs (the
    artemis re-emit pattern) the agent postprocess rasterizes to the COGs.

    Nodes the bathymetry clamp forced wet (``mesh['wet']`` False - land, NoData or
    sub-threshold shallows) are written NaN. The solver needs a value there to keep
    the closed basin stable; the PRODUCT must not claim one, and every downstream
    scalar is computed off the masked arrays this returns.

    Returns ``(surface_basename, bottom_basename, surface_masked, bottom_masked)``."""
    wet = mesh.get("wet")
    surf = np.asarray(surface_vals, dtype=np.float64).copy()
    bot = np.asarray(bottom_vals, dtype=np.float64).copy()
    if wet is not None:
        dry = ~np.asarray(wet, dtype=bool)
        surf[dry] = np.nan
        bot[dry] = np.nan
    sp = os.path.join(data_dir, "t3d_surface.slf")
    bp = os.path.join(data_dir, "t3d_bottom.slf")
    write_slf(mesh, sp, values=surf, varname=varname)
    write_slf(mesh, bp, values=bot, varname=varname)
    return "t3d_surface.slf", "t3d_bottom.slf", surf, bot


def _vertical_profile(field3d, mesh, nplan):
    """Column of the primary variable at the basin centre, bed -> surface.

    A CLAMPED (land) centre node would make the whole profile chart a statement
    about 5 m of invented water, so the column snaps to the nearest wet node."""
    icx, jcy = mesh["nx"] // 2, mesh["ny"] // 2
    jc = icx * mesh["ny"] + jcy
    jc = min(jc, field3d.shape[1] - 1)
    wet = mesh.get("wet")
    if wet is not None:
        wet = np.asarray(wet, dtype=bool)[: field3d.shape[1]]
        if wet.any() and not wet[jc]:
            idx = np.flatnonzero(wet)
            jc = int(idx[np.argmin(np.abs(idx - jc))])
    col = field3d[:, jc]
    sigma = np.linspace(0.0, 1.0, nplan)
    return sigma, col


def _base_metrics(cfg, run_id, mesh, utm_epsg, bbox, res3d, surf_slf, bot_slf,
                  nplan):
    return {
        "status": "ok", "correct_end": True, "run_id": run_id,
        "flow_mode": cfg.flow_mode, "bathy_source": cfg.bathy_source,
        "result_slf": os.path.basename(res3d),
        "surface_field_slf": surf_slf, "bottom_field_slf": bot_slf,
        "npoin": int(mesh["npoin"]), "nelem": int(len(mesh["ikle"])),
        "nplan": int(nplan),
        "utm_epsg": utm_epsg,
        "bbox": list(bbox) if bbox else None,
    }


def _fail_metrics(cfg, run_id, out):
    return {
        "status": "error", "correct_end": False, "run_id": run_id,
        "flow_mode": cfg.flow_mode, "bathy_source": cfg.bathy_source,
        "error": "TELEMAC-3D did not reach CORRECT END OF RUN",
        "listing_tail": "\n".join((out or "").splitlines()[-40:]),
    }


# ---------------------------------------------------------------------------
# 8. The three question-class solves.
# ---------------------------------------------------------------------------
def _solve_stratification(cfg: Telemac3dConfig, data_dir, run_id, mesh, meta):
    """Thermal stratification persistence vs wind mixing (DENSITY LAW 1)."""
    tag = "strat"
    geo = os.path.join(data_dir, "geo_t3d.slf")
    cli = os.path.join(data_dir, "bc_t3d.cli")
    res3d = os.path.join(data_dir, "res3d_t3d.slf")
    res2d = os.path.join(data_dir, "res2d_t3d.slf")
    fort = os.path.join(data_dir, "ic_t3d.f")
    nplan = int(cfg.nplan)
    dtherm = float(cfg.thermocline_depth_m)
    twarm, tcold = float(cfg.warm_temp_c), float(cfg.cold_temp_c)
    # the vertical grid is planned BEFORE the steering file is authored: an
    # unresolvable thermocline refuses here rather than solving an IC the grid
    # cannot hold.
    vgrid = plan_vertical_grid(nplan, float(-np.nanmin(mesh["Z"])), dtherm)
    write_slf(mesh, geo)
    write_cli(mesh, cli)
    with open(fort, "w") as f:
        f.write(condi_thermocline(dtherm, twarm, tcold,
                                  vgrid["thermocline_delta_m"]))
    wind_on = cfg.wind_speed_mps > 0.0
    wu, wv = wind_components(float(cfg.wind_speed_mps), float(cfg.wind_dir_from_deg))
    dt = 20.0
    nit = int(float(cfg.duration_hours) * 3600.0 / dt)
    graprd = max(1, nit // 10)
    write_cas(os.path.join(data_dir, f"t3d_{tag}.cas"), os.path.basename(geo),
              os.path.basename(cli), os.path.basename(res3d),
              os.path.basename(res2d), os.path.basename(fort),
              title=f"THERMAL STRAT wind={wind_on} {cfg.name}", nplan=nplan,
              dt=dt, nit=nit, graprd=graprd, denlaw=1,
              tracer_name="TEMPERATURE     ", nonhyd=False,
              wind=wind_on, wind_u=wu, wind_v=wv, iturbv=2, rho0=1000.0,
              transf=vgrid["mesh_transformation"],
              transf_coef=vgrid["mesh_stretching_coefficients"])
    ok, out = run_t3d(os.path.join(data_dir, f"t3d_{tag}.cas"), data_dir, tag,
                      timeout=int(os.environ.get("TRID3NT_TELEMAC3D_SOLVE_TIMEOUT", "3600")))
    if not ok:
        return _fail_metrics(cfg, run_id, out)
    tf = open_res(res3d)
    nt = tf.ntimestep
    t_init, npoin2, nplan_r = field_3d(tf, "TEMPERATURE", 0)
    t_fin, _, _ = field_3d(tf, "TEMPERATURE", nt - 1)
    tf.close()
    sigma, col_fin = _vertical_profile(t_fin, mesh, nplan_r)
    _, col_init = _vertical_profile(t_init, mesh, nplan_r)
    dT_init = float(col_init[-1] - col_init[0])
    dT_final = float(col_fin[-1] - col_fin[0])
    z_planes = np.asarray(vgrid["vertical_sigma_planes"], dtype=np.float64)
    if z_planes.size != col_fin.size:
        z_planes = np.linspace(0.0, 1.0, col_fin.size)
    heat_init = float(np.trapz(col_init, z_planes))
    heat_final = float(np.trapz(col_fin, z_planes))
    # surface / bottom temperature, land-clamped nodes already NaN
    surf_slf, bot_slf, surf, bot = _emit_layer_fields(
        mesh, t_fin[-1], t_fin[0], data_dir, "TEMPERATURE     ")
    m = _base_metrics(cfg, run_id, mesh, meta.get("utm_epsg"), cfg.bbox,
                      res3d, surf_slf, bot_slf, nplan_r)
    m.update(vgrid)
    m.update({
        "variable_label": "Surface temperature", "variable_units": "degC",
        "stratification_metric": round(abs(dT_final), 4),
        "stratification_dt": round(dT_final, 4),
        "stratification_dt_init": round(dT_init, 4),
        # depth-weighted column mean: with no heat exchange in the run this is
        # CONSERVED, so the drift is the numerical error bar on "the warm layer
        # mixed down" - and the refutation of any "the lake cooled" reading.
        "column_heat_mean_init_c": round(heat_init, 4),
        "column_heat_mean_final_c": round(heat_final, 4),
        "column_heat_drift_frac": round(
            (heat_final - heat_init) / max(abs(heat_init), 1e-9), 6),
        "surface_value_mean": round(float(np.nanmean(surf)), 3),
        "bottom_value_mean": round(float(np.nanmean(bot)), 3),
        "wind_speed_mps": float(cfg.wind_speed_mps) if wind_on else 0.0,
        # the plane positions the run actually used - a zoomed sigma is NOT evenly
        # spaced, so plotting the column against linspace would misplace every point
        "chart_sigma": (np.round(vgrid["vertical_sigma_planes"], 4).tolist()
                        if len(vgrid["vertical_sigma_planes"]) == len(col_fin)
                        else np.round(sigma, 3).tolist()),
        "chart_profile": np.round(col_fin, 3).tolist(),
        "chart_profile_init": np.round(col_init, 3).tolist(),
        "chart_kind": "vertical_temperature_profile",
        **{k: meta[k] for k in ("dx_m", "coarsened", "n_wet_nodes",
                                "n_clamped_nodes", "depth_max_m",
                                "depth_mean_m") if k in meta},
    })
    LOG.info("telemac3d stratification ok: dT_final=%.3f C (wind=%s) surf=%.2f bot=%.2f "
             "dz_surface=%.2f m (transf %d)",
             dT_final, wind_on, float(np.nanmean(surf)), float(np.nanmean(bot)),
             vgrid["vertical_dz_surface_m"], vgrid["mesh_transformation"])
    return m


def _solve_wind_circulation(cfg: Telemac3dConfig, data_dir, run_id, mesh, meta):
    """Wind-driven closed-basin circulation: the vertical U structure (THE 3D-vs-2D
    discriminant). Barotropic (DENSITY LAW 0), steady wind."""
    tag = "wind"
    geo = os.path.join(data_dir, "geo_t3d.slf")
    cli = os.path.join(data_dir, "bc_t3d.cli")
    res3d = os.path.join(data_dir, "res3d_t3d.slf")
    res2d = os.path.join(data_dir, "res2d_t3d.slf")
    nplan = int(cfg.nplan)
    write_slf(mesh, geo)
    write_cli(mesh, cli)
    U = float(cfg.wind_speed_mps) if cfg.wind_speed_mps > 0.0 else 10.0
    wu, wv = wind_components(U, float(cfg.wind_dir_from_deg))
    dt = 10.0
    nit = int(float(cfg.duration_hours) * 3600.0 / dt)
    graprd = max(1, nit // 10)
    write_cas(os.path.join(data_dir, f"t3d_{tag}.cas"), os.path.basename(geo),
              os.path.basename(cli), os.path.basename(res3d),
              os.path.basename(res2d), None,
              title=f"WIND-DRIVEN CLOSED BASIN {cfg.name}", nplan=nplan,
              dt=dt, nit=nit, graprd=graprd, denlaw=0, tracer_name=None,
              nonhyd=False, wind=True, wind_u=wu, wind_v=wv, iturbv=2)
    ok, out = run_t3d(os.path.join(data_dir, f"t3d_{tag}.cas"), data_dir, tag,
                      timeout=int(os.environ.get("TRID3NT_TELEMAC3D_SOLVE_TIMEOUT", "3600")))
    if not ok:
        return _fail_metrics(cfg, run_id, out)
    tf = open_res(res3d)
    nt = tf.ntimestep
    u3, npoin2, nplan_r = field_3d(tf, "VELOCITY U", nt - 1)
    tf.close()
    sigma, u_col = _vertical_profile(u3, mesh, nplan_r)
    depth_avg = float(np.trapz(u_col, dx=1.0) / max(nplan_r - 1, 1))
    # surface / bottom U, land-clamped nodes already NaN
    surf_slf, bot_slf, surf, bot = _emit_layer_fields(
        mesh, u3[-1], u3[0], data_dir, "VELOCITY U      ")
    m = _base_metrics(cfg, run_id, mesh, meta.get("utm_epsg"), cfg.bbox,
                      res3d, surf_slf, bot_slf, nplan_r)
    m.update({
        "variable_label": "Surface velocity U", "variable_units": "m/s",
        "stratification_metric": round(abs(float(u_col[-1] - u_col[0])), 5),
        "u_surface": round(float(u_col[-1]), 5),
        "u_bottom": round(float(u_col[0]), 5),
        "depth_avg_u": round(depth_avg, 5),
        "surface_value_mean": round(float(np.nanmean(surf)), 5),
        "bottom_value_mean": round(float(np.nanmean(bot)), 5),
        "wind_speed_mps": U,
        "chart_sigma": np.round(sigma, 3).tolist(),
        "chart_profile": np.round(u_col, 5).tolist(),
        "chart_kind": "vertical_velocity_profile",
        **{k: meta[k] for k in ("dx_m", "coarsened", "n_wet_nodes",
                                "n_clamped_nodes", "depth_max_m",
                                "depth_mean_m") if k in meta},
    })
    LOG.info("telemac3d wind_circulation ok: u_surface=%.4f u_bottom=%.4f depth_avg=%.4f",
             u_col[-1], u_col[0], depth_avg)
    return m


def _solve_salt_wedge(cfg: Telemac3dConfig, data_dir, run_id):
    """Lock-exchange gravity current (DENSITY LAW 2 -> drho/rho=750e-6*S) - the
    density-driven salt-wedge / estuary physics. Idealized-only (a real estuary
    needs a tidal liquid boundary). Hydrostatic vs non-hydrostatic is the
    dam-break-3D fidelity rung (gotcha 6)."""
    tag = "lock"
    S = float(cfg.lock_salinity)
    H = float(cfg.lock_depth_m)
    Lx, Ly = 16.0, 2.0
    dx = 0.25
    nplan = int(cfg.nplan)
    xgate = Lx / 2.0
    mesh = build_grid(Lx, Ly, dx, lambda X, Y: np.full_like(X, -H))
    geo = os.path.join(data_dir, "geo_t3d.slf")
    cli = os.path.join(data_dir, "bc_t3d.cli")
    res3d = os.path.join(data_dir, "res3d_t3d.slf")
    res2d = os.path.join(data_dir, "res2d_t3d.slf")
    fort = os.path.join(data_dir, "ic_t3d.f")
    write_slf(mesh, geo)
    write_cli(mesh, cli)
    with open(fort, "w") as f:
        f.write(condi_lock(xgate, S))
    dt, nit, graprd = 0.05, 800, 20              # 40 s
    write_cas(os.path.join(data_dir, f"t3d_{tag}.cas"), os.path.basename(geo),
              os.path.basename(cli), os.path.basename(res3d),
              os.path.basename(res2d), os.path.basename(fort),
              title=f"LOCK-EXCHANGE nonhyd={cfg.non_hydrostatic} {cfg.name}",
              nplan=nplan, dt=dt, nit=nit, graprd=graprd, denlaw=2,
              tracer_name="SALINITY        ", nonhyd=bool(cfg.non_hydrostatic),
              iturbv=1, rho0=1000.0, friction_coef=0.0005)
    ok, out = run_t3d(os.path.join(data_dir, f"t3d_{tag}.cas"), data_dir, tag,
                      timeout=int(os.environ.get("TRID3NT_TELEMAC3D_SOLVE_TIMEOUT", "3600")))
    if not ok:
        return _fail_metrics(cfg, run_id, out)
    tf = open_res(res3d)
    nt = tf.ntimestep
    times = np.array(tf.times)
    xs = mesh["xs"]
    jmid = mesh["ny"] // 2
    front_x, front_t = [], []
    for rec in range(nt):
        sal, _, nplan_r = field_3d(tf, "SALINITY", rec)
        bottom = sal[0]
        row = np.array([bottom[i * mesh["ny"] + jmid] for i in range(mesh["nx"])])
        dense = np.where(row > S / 2.0)[0]
        if dense.size:
            k = int(dense.max())
            if k < len(xs) - 1 and row[k] != row[k + 1]:
                frac = (row[k] - S / 2.0) / (row[k] - row[k + 1])
                nose = xs[k] + frac * (xs[k + 1] - xs[k])
            else:
                nose = xs[k]
        else:
            nose = xgate
        front_x.append(float(nose))
        front_t.append(float(times[rec]))
    sal_fin, _, nplan_r = field_3d(tf, "SALINITY", nt - 1)
    tf.close()
    sigma, sal_col = _vertical_profile(sal_fin, mesh, nplan_r)
    ft, fx = np.array(front_t), np.array(front_x)
    win = (fx > xgate + 0.5) & (fx < 0.9 * Lx)
    if win.sum() >= 2:
        speed = float(np.polyfit(ft[win], fx[win], 1)[0])
    else:
        speed = float((fx[-1] - xgate) / max(ft[-1], 1e-9))
    drho = 750e-6 * S
    benjamin = float(benjamin_front_speed(drho, H))
    surf_slf, bot_slf, surf, bot = _emit_layer_fields(
        mesh, sal_fin[-1], sal_fin[0], data_dir, "SALINITY        ")
    m = _base_metrics(cfg, run_id, mesh, None, None, res3d, surf_slf, bot_slf,
                      nplan_r)
    m.update({
        "variable_label": "Surface salinity", "variable_units": "psu",
        "stratification_metric": round(abs(speed), 5),
        "front_speed_mps": round(speed, 5),
        "benjamin_speed_mps": round(benjamin, 5),
        "front_ratio": round(speed / benjamin, 4) if benjamin else None,
        "non_hydrostatic": bool(cfg.non_hydrostatic),
        "salinity_lock": round(S, 3), "drho_over_rho": round(drho, 6),
        "surface_value_mean": round(float(np.nanmean(surf)), 4),
        "bottom_value_mean": round(float(np.nanmean(bot)), 4),
        "chart_sigma": np.round(sigma, 3).tolist(),
        "chart_profile": np.round(sal_col, 4).tolist(),
        "chart_front_t_s": np.round(ft, 3).tolist(),
        "chart_front_x_m": np.round(fx, 3).tolist(),
        "chart_kind": "salt_wedge_gravity_current",
        "bathy_label": "idealized lock-exchange channel (analytic Benjamin "
                       "gravity-current V&V; no real estuary bathymetry fetched)",
    })
    LOG.info("telemac3d salt_wedge ok: front_speed=%.4f benjamin=%.4f ratio=%s nonhyd=%s",
             speed, benjamin, m["front_ratio"], cfg.non_hydrostatic)
    return m


def _idealized_strat_mesh(cfg: Telemac3dConfig):
    Lx, Ly = 4000.0, 1000.0
    dx = float(cfg.target_resolution_m or 250.0)
    dx = max(dx, 100.0)
    return build_grid(Lx, Ly, dx, lambda X, Y: np.full_like(X, -float(cfg.strat_depth_m)))


def _idealized_wind_mesh(cfg: Telemac3dConfig):
    Lx, Ly = 5000.0, 1000.0
    dx = float(cfg.target_resolution_m or 250.0)
    dx = max(dx, 100.0)
    depth = float(cfg.strat_depth_m if cfg.strat_depth_m else 10.0)
    return build_grid(Lx, Ly, dx, lambda X, Y: np.full_like(X, -depth))


def solve(cfg: Telemac3dConfig, workdir: str, run_id: str = None) -> dict[str, Any]:
    """Author + solve ONE TELEMAC-3D field; return a metrics dict.

    Dispatches by ``flow_mode``. Writes res3d_t3d.slf (the raw 3D result mesh
    sibling) + t3d_surface.slf / t3d_bottom.slf (the single-frame 2D layers the
    agent-side postprocess rasterizes to the surface/bottom COGs) into ``workdir``.
    """
    t0 = time.time()
    mode = str(cfg.flow_mode or "stratification").lower()
    real = str(cfg.bathy_source or "idealized").lower() in (
        "noaa_greatlakes", "greatlakes", "noaa")

    if mode == "salt_wedge":
        # idealized-only (a real estuary needs a tidal liquid boundary).
        metrics = _solve_salt_wedge(cfg, workdir, run_id)
    elif mode == "wind_circulation":
        if real:
            mesh, meta = build_real_lake_grid(cfg, workdir)
        else:
            mesh, meta = _idealized_wind_mesh(cfg), dict(
                utm_epsg=None, dx_m=float(cfg.target_resolution_m or 250.0),
                coarsened=False)
        metrics = _solve_wind_circulation(cfg, workdir, run_id, mesh, meta)
    else:  # stratification (default)
        if real:
            mesh, meta = build_real_lake_grid(cfg, workdir)
        else:
            mesh, meta = _idealized_strat_mesh(cfg), dict(
                utm_epsg=None, dx_m=float(cfg.target_resolution_m or 250.0),
                coarsened=False)
        metrics = _solve_stratification(cfg, workdir, run_id, mesh, meta)

    metrics["wall_s"] = round(time.time() - t0, 1)
    return metrics
