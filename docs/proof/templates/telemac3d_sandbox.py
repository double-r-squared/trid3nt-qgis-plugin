"""TELEMAC-3D local-first physics sandbox (ADR 0241).

Runs INSIDE trid3nt-local/telemac:latest (needs the baked telemac3d binary +
the opentelemac python SELAFIN API). Geography-free idealized verification of
the classic TELEMAC-3D validation set - the physics 2D depth-averaging cannot
resolve, so every case is a discriminating 3D-vs-2D or stratified-vs-mixed pair:

  * LOCK-EXCHANGE gravity current (Benjamin 1968 class): a dense (saline) column
    released against a light column produces a bottom gravity current whose front
    advances at the analytic energy-conserving speed U = 0.5*sqrt(g'*H),
    g' = g*drho/rho. Density-driven flow == the salinity-intrusion / estuary
    salt-wedge physics. Discriminating knob: density law ON vs OFF (barotropic ->
    no current); hydrostatic vs non-hydrostatic (the dam-break-3D fidelity rung).

  * WIND-DRIVEN CLOSED-BASIN circulation (THE reason 3D exists): a steady wind
    over a closed lake drives surface water downwind; mass conservation forces a
    return flow at depth. The VERTICAL VELOCITY PROFILE at mid-basin - surface
    downwind, bottom upwind, depth-integrated transport ~0 - is invisible to a 2D
    depth-averaged model (which shows ~zero velocity everywhere in a closed
    basin). Discriminating pair: 3D vertical structure vs its own depth-average.

  * THERMAL STRATIFICATION persistence vs wind mixing (the lake-turnover
    question): a lake initialised with a warm surface layer over a cold hypolimnion
    (DENSITY LAW = 1, freshwater max density near 4 C) either KEEPS its thermocline
    (calm) or has it eroded by wind-shear turbulence (windy). Discriminating pair:
    calm vs windy top-to-bottom temperature difference. This is the stratified 3D
    water column the AED2 lake-ecology coupling (ADR 0234 STOP) requires.

Sigma-layer mesh mechanics (NUMBER OF HORIZONTAL LEVELS = NPLAN, MESH
TRANSFORMATION = 1 sigma) are exercised by every case - the vertical discretisation
IS the 3D degree of freedom.
"""
import json
import os
import subprocess
import sys
import numpy as np

WORK = "/data"
G = 9.81


# --------------------------------------------------------------------------
# 1. Idealized regular-grid 2D triangular mesh (CCW ring, rank IPOBO).
#    TELEMAC-3D reads a 2D geometry + 2D boundary file and extrudes NPLAN
#    horizontal levels internally (the sigma transform), so the mesh scaffold
#    is identical to the 2D composers'.
# --------------------------------------------------------------------------
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


def write_slf(mesh, path):
    from data_manip.extraction.telemac_file import TelemacFile
    if os.path.exists(path):
        os.remove(path)
    tf = TelemacFile(path, access="w")
    tf.add_header("TELEMAC3D SANDBOX " + os.path.basename(path),
                  date=np.array([2026, 8, 13, 0, 0, 0]))
    tf.add_mesh(mesh["X"], mesh["Y"], mesh["ikle"], z=mesh["Z"])
    tf._ipob3 = mesh["ipob"].astype(np.int32)
    tf._ipob2 = tf._ipob3
    tf._nptfr = int(mesh["nptfr"])
    tf._nbor = mesh["ring"].astype(np.int32)
    tf._knolg = np.arange(1, mesh["npoin"] + 1, dtype=np.int32)
    tf.add_variable("BOTTOM          ", "M               ")
    tf.add_data_value("BOTTOM          ", 0, mesh["Z"])
    tf.write()
    tf.close()


def write_cli(mesh, path):
    """All-solid closed basin (T2D 13-column boundary format, which TELEMAC-3D
    reads as the horizontal boundary). LIHBOR=LIUBOR=LIVBOR=LITBOR=2 -> solid
    wall everywhere (no liquid boundary; every case is a closed initial-value
    problem: a gravity current, a wind-driven gyre, a stratified column)."""
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


# --------------------------------------------------------------------------
# 2. USER_CONDI3D_TRAC - non-uniform initial tracer field (the stratification /
#    lock-gate that CANNOT be expressed by the scalar INITIAL VALUES OF TRACERS).
#    X/Y/Z => MESH3D%X/Y/Z%R are the NPOIN3 3D node coords (Z is populated by
#    CALCOT before this hook is called); TA%ADR(itrac)%P%R(i3) is tracer i3.
# --------------------------------------------------------------------------
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


def condi_thermocline(dtherm, twarm, tcold):
    # warm epilimnion above depth DTHERM (below still-water surface at z=0),
    # cold hypolimnion below. Z is bed-referenced elevation; surface at 0 so
    # depth-below-surface = -Z.
    return (CONDI_HEAD +
            f"      DO I3=1,NPOIN3\n"
            f"        DPTH=-Z(I3)\n"
            f"        IF(DPTH.LT.{dtherm:.4f}D0) THEN\n"
            f"          TA%ADR(1)%P%R(I3)={twarm:.4f}D0\n"
            f"        ELSE\n"
            f"          TA%ADR(1)%P%R(I3)={tcold:.4f}D0\n"
            f"        ENDIF\n"
            f"      ENDDO\n" + CONDI_TAIL)


# --------------------------------------------------------------------------
# 3. TELEMAC-3D steering-file author
# --------------------------------------------------------------------------
def write_cas(path, geo, cli, res3d, res2d, fort, *, title, nplan, dt, nit,
              graprd, denlaw=0, tracer_name=None, nonhyd=False,
              wind=False, wind_u=0.0, wind_v=0.0, iturbv=2,
              rho0=None, friction_coef=0.01):
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
    A("MESH TRANSFORMATION : 1")             # 1 = sigma (uniform planes)
    A("NON-HYDROSTATIC VERSION : " + ("YES" if nonhyd else "NO"))
    # --- initial free surface at rest ---
    A("INITIAL CONDITIONS : 'CONSTANT ELEVATION'")
    A("INITIAL ELEVATION : 0.")
    # --- turbulence ---
    A("HORIZONTAL TURBULENCE MODEL : 1")     # constant viscosity
    A(f"VERTICAL TURBULENCE MODEL : {iturbv}")  # 1 const, 2 mixing length, 3 k-eps
    A("COEFFICIENT FOR HORIZONTAL DIFFUSION OF VELOCITIES : 1.E-4")
    A("COEFFICIENT FOR VERTICAL DIFFUSION OF VELOCITIES : 1.E-4")
    A("COEFFICIENT FOR HORIZONTAL DIFFUSION OF TRACERS : 1.E-4")
    A("COEFFICIENT FOR VERTICAL DIFFUSION OF TRACERS : 1.E-4")
    # --- bottom friction ---
    A("LAW OF BOTTOM FRICTION : 5")
    A(f"FRICTION COEFFICIENT FOR THE BOTTOM : {friction_coef}")
    # --- density / tracers ---
    if tracer_name is not None:
        A("NUMBER OF TRACERS : 1")
        A(f"NAMES OF TRACERS : '{tracer_name}'")
        # mandatory even when USER_CONDI3D_TRAC overrides it (solver PLANTEs on
        # "GIVE THE KEY-WORD INITIAL VALUES OF TRACERS" without it)
        A("INITIAL VALUES OF TRACERS : 0.")
    A(f"DENSITY LAW : {denlaw}")
    if rho0 is not None:
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


# --------------------------------------------------------------------------
# 4. Run + read the 3D SELAFIN
# --------------------------------------------------------------------------
def run_t3d(cas, tag):
    env = dict(os.environ)
    cmd = ["telemac3d.py", cas, "--ncsize=1"]
    p = subprocess.run(cmd, cwd=WORK, env=env, capture_output=True, text=True,
                       timeout=3600)
    log = os.path.join(WORK, f"t3d_{tag}.log")
    with open(log, "w") as f:
        f.write("CMD: " + " ".join(cmd) + "\n\nSTDOUT:\n" + p.stdout +
                "\n\nSTDERR:\n" + p.stderr)
    return p.returncode, p.stdout, p.stderr


def open_res(res_path):
    from data_manip.extraction.telemac_file import TelemacFile
    return TelemacFile(res_path)


def field_3d(tf, varname, rec):
    """Full NPOIN3 field at record `rec`, plus (npoin2, nplan) for reshaping.
    3D node ordering is (iplan)*npoin2 + j, iplan 0=bottom .. nplan-1=surface."""
    data = np.asarray(tf.get_data_value(varname, rec))
    nplan = int(tf.nplan)
    npoin3 = data.shape[0]
    npoin2 = npoin3 // nplan
    return data.reshape(nplan, npoin2), npoin2, nplan  # [iplan, j]


# --------------------------------------------------------------------------
# CASES
# --------------------------------------------------------------------------
def benjamin_front_speed(drho_over_rho, H):
    gp = G * drho_over_rho
    return 0.5 * np.sqrt(gp * H)             # energy-conserving Benjamin front


def case_lock_exchange(nonhyd=False, tag="lock", S=26.7, H=1.0,
                       dx=0.25, nplan=13, friction=0.0005):
    """Lock-exchange gravity current. Channel L=16 m, H=1 m, dense saline half
    (X<8 m) released against fresh half. DENSITY LAW = 2 -> drho/rho = 750e-6*S.
    Front speed measured from the bottom-plane salinity front position vs time
    (sub-grid interpolated S/2 crossing), compared to Benjamin U = 0.5*sqrt(g'H).
    Low bottom friction approaches the free-slip inviscid front the theory
    assumes; the residual gap is the known hydrostatic under-prediction."""
    Lx, Ly = 16.0, 2.0
    xgate = Lx / 2.0
    mesh = build_grid(Lx, Ly, dx, lambda X, Y: np.full_like(X, -H))
    geo, cli = f"geo_{tag}.slf", f"bc_{tag}.cli"
    res3d, res2d, fort = f"r3_{tag}.slf", f"r2_{tag}.slf", f"ic_{tag}.f"
    write_slf(mesh, os.path.join(WORK, geo))
    write_cli(mesh, os.path.join(WORK, cli))
    with open(os.path.join(WORK, fort), "w") as f:
        f.write(condi_lock(xgate, S))
    dt, nit, graprd = 0.05, 800, 20          # 40 s, snapshot every 1 s
    write_cas(os.path.join(WORK, f"t3d_{tag}.cas"), geo, cli, res3d, res2d, fort,
              title=f"LOCK-EXCHANGE nonhyd={nonhyd}", nplan=nplan, dt=dt, nit=nit,
              graprd=graprd, denlaw=2, tracer_name="SALINITY        ",
              nonhyd=nonhyd, iturbv=1, rho0=1000.0, friction_coef=friction)
    rc, out, err = run_t3d(f"t3d_{tag}.cas", tag)
    if rc != 0:
        return dict(tag=tag, rc=rc, ok=False, err=err[-2500:])
    tf = open_res(os.path.join(WORK, res3d))
    nt = tf.ntimestep
    times = np.array(tf.times)
    xs = mesh["xs"]
    jmid_row = mesh["ny"] // 2
    front_x, front_t = [], []
    for rec in range(nt):
        sal, npoin2, nplan_r = field_3d(tf, "SALINITY", rec)
        bottom = sal[0]                       # iplan 0 = bed
        row = np.array([bottom[i * mesh["ny"] + jmid_row] for i in range(mesh["nx"])])
        # sub-grid nose: furthest downstream node still >S/2, then linearly
        # interpolate the S/2 crossing into the next (fresher) node.
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
    tf.close()
    # fit front speed over the propagation window (front past the gate, before
    # it nears the end wall): use records where gate < nose < 0.9*Lx
    ft = np.array(front_t)
    fx = np.array(front_x)
    win = (fx > xgate + 0.5) & (fx < 0.9 * Lx)
    if win.sum() >= 2:
        speed = float(np.polyfit(ft[win], fx[win], 1)[0])
    else:
        speed = float((fx[-1] - xgate) / max(ft[-1], 1e-9))
    drho = 750e-6 * S
    analytic = float(benjamin_front_speed(drho, H))
    return dict(tag=tag, rc=rc, ok=True, nonhyd=nonhyd, S=S, H=H,
                drho_over_rho=drho, front_t=front_t, front_x=front_x,
                measured_speed=speed, benjamin_speed=analytic,
                ratio=speed / analytic if analytic else None)


def case_wind_basin(tag="wind", U=10.0, H=10.0):
    """Wind-driven closed-basin circulation. L=5 km, W=1 km, H=10 m, steady wind
    +X. Reports the vertical U(z) profile at mid-basin: surface downwind (+),
    bottom upwind (-), depth-integrated ~0 (the 2D depth-average a shallow-water
    model would return everywhere). THE 3D-vs-2D discriminant."""
    Lx, Ly, dx = 5000.0, 1000.0, 250.0
    mesh = build_grid(Lx, Ly, dx, lambda X, Y: np.full_like(X, -H))
    geo, cli = f"geo_{tag}.slf", f"bc_{tag}.cli"
    res3d, res2d = f"r3_{tag}.slf", f"r2_{tag}.slf"
    write_slf(mesh, os.path.join(WORK, geo))
    write_cli(mesh, os.path.join(WORK, cli))
    dt, nit, graprd = 10.0, 1080, 108        # 3 h, seiche-damped steady state
    write_cas(os.path.join(WORK, f"t3d_{tag}.cas"), geo, cli, res3d, res2d, None,
              title="WIND-DRIVEN CLOSED BASIN", nplan=11, dt=dt, nit=nit,
              graprd=graprd, denlaw=0, tracer_name=None, nonhyd=False,
              wind=True, wind_u=U, wind_v=0.0, iturbv=2)
    rc, out, err = run_t3d(f"t3d_{tag}.cas", tag)
    if rc != 0:
        return dict(tag=tag, rc=rc, ok=False, err=err[-2500:])
    tf = open_res(os.path.join(WORK, res3d))
    nt = tf.ntimestep
    u3, npoin2, nplan = field_3d(tf, "VELOCITY U", nt - 1)
    # column at basin centre
    icx, jcy = mesh["nx"] // 2, mesh["ny"] // 2
    jc = icx * mesh["ny"] + jcy
    u_col = u3[:, jc]                          # iplan 0=bed .. nplan-1=surface
    # sigma-uniform planes -> depth-average is the plane mean (trapezoid)
    depth_avg = float(np.trapz(u_col, dx=1.0) / (nplan - 1))
    tf.close()
    sig = np.linspace(0.0, 1.0, nplan)        # 0 bed, 1 surface
    return dict(tag=tag, rc=rc, ok=True, U_wind=U, H=H, nplan=nplan,
                sigma=sig.tolist(), u_profile=u_col.tolist(),
                u_surface=float(u_col[-1]), u_bottom=float(u_col[0]),
                depth_avg_u=depth_avg)


def case_thermal(tag="therm", wind=False, U=12.0):
    """Thermal stratification persistence vs wind mixing. Lake H=20 m, warm
    epilimnion (25 C) above an 8 m thermocline, cold hypolimnion (15 C).
    DENSITY LAW = 1 (freshwater rho max near 4 C). Calm -> thermocline persists;
    windy -> shear turbulence erodes it. Reports top-to-bottom dT."""
    Lx, Ly, dx = 4000.0, 1000.0, 250.0
    H, dtherm, twarm, tcold = 20.0, 8.0, 25.0, 15.0
    mesh = build_grid(Lx, Ly, dx, lambda X, Y: np.full_like(X, -H))
    geo, cli = f"geo_{tag}.slf", f"bc_{tag}.cli"
    res3d, res2d, fort = f"r3_{tag}.slf", f"r2_{tag}.slf", f"ic_{tag}.f"
    write_slf(mesh, os.path.join(WORK, geo))
    write_cli(mesh, os.path.join(WORK, cli))
    with open(os.path.join(WORK, fort), "w") as f:
        f.write(condi_thermocline(dtherm, twarm, tcold))
    dt, nit, graprd = 20.0, 900, 90          # 5 h
    write_cas(os.path.join(WORK, f"t3d_{tag}.cas"), geo, cli, res3d, res2d, fort,
              title=f"THERMAL STRAT wind={wind}", nplan=15, dt=dt, nit=nit,
              graprd=graprd, denlaw=1, tracer_name="TEMPERATURE     ",
              nonhyd=False, wind=wind, wind_u=U if wind else 0.0, wind_v=0.0,
              iturbv=2)
    rc, out, err = run_t3d(f"t3d_{tag}.cas", tag)
    if rc != 0:
        return dict(tag=tag, rc=rc, ok=False, err=err[-2500:])
    tf = open_res(os.path.join(WORK, res3d))
    nt = tf.ntimestep
    icx, jcy = mesh["nx"] // 2, mesh["ny"] // 2
    jc = icx * mesh["ny"] + jcy
    t_init, _, nplan = field_3d(tf, "TEMPERATURE", 0)
    t_fin, _, _ = field_3d(tf, "TEMPERATURE", nt - 1)
    col0 = t_init[:, jc]
    colf = t_fin[:, jc]
    tf.close()
    sig = np.linspace(0.0, 1.0, nplan)
    return dict(tag=tag, rc=rc, ok=True, wind=wind, U=U if wind else 0.0,
                sigma=sig.tolist(), t_init=col0.tolist(), t_final=colf.tolist(),
                dT_init=float(col0[-1] - col0[0]),
                dT_final=float(colf[-1] - colf[0]))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    out = {}
    if mode == "smoke":
        out["smoke"] = case_lock_exchange(nonhyd=False, tag="smoke")
    elif mode == "lock":
        out["hydro"] = case_lock_exchange(nonhyd=False, tag="lock_h")
        out["nonhyd"] = case_lock_exchange(nonhyd=True, tag="lock_nh")
    elif mode == "wind":
        out["wind"] = case_wind_basin(tag="wind")
    elif mode == "thermal":
        out["calm"] = case_thermal(tag="th_calm", wind=False)
        out["windy"] = case_thermal(tag="th_wind", wind=True)
    elif mode == "all":
        out["lock_hydro"] = case_lock_exchange(nonhyd=False, tag="lock_h")
        out["lock_nonhyd"] = case_lock_exchange(nonhyd=True, tag="lock_nh")
        out["wind"] = case_wind_basin(tag="wind")
        out["thermal_calm"] = case_thermal(tag="th_calm", wind=False)
        out["thermal_windy"] = case_thermal(tag="th_wind", wind=True)
        with open(os.path.join(WORK, "telemac3d_physics_direct_result.json"), "w") as f:
            json.dump(out, f, indent=1)
    print(json.dumps(out))
