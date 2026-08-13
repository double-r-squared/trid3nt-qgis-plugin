"""TOMAWAC local-first physics sandbox (ADR 0236).

Runs INSIDE trid3nt-local/telemac:latest (needs the baked tomawac binary +
the opentelemac python SELAFIN API). Geography-free idealized verification:
replicates the physics of the official TOMAWAC `fetch_limited` and `shoal`
example decks (rectangular basin, uniform wind / prescribed offshore swell over
a sloping beach) so the proof clears the citations law without a US site.

Physics proven (discriminating):
  * fetch-limited growth: Hs grows monotonically along the fetch and with wind
    speed, and approaches the fetch-limited asymptote (JONSWAP / CERC band).
  * shoaling: an offshore swell steepens (Hs rises) as depth shoals toward shore
    before depth-induced breaking caps it.
"""
import json
import os
import subprocess
import sys
import numpy as np

WORK = "/data"


# --------------------------------------------------------------------------
# 1. Idealized regular-grid triangular mesh (CCW outer ring, rank IPOBO)
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
            # CCW triangles (positive area)
            tris.append((a, b, c))
            tris.append((a, c, d))
    ikle = np.array(tris, dtype=np.int32)

    # outer boundary ring, CCW (domain on the left): bottom -> right -> top -> left
    ring = []
    for i in range(nx - 1):          # bottom edge j=0, +X
        ring.append(nid(i, 0))
    for j in range(ny - 1):          # right edge i=nx-1, +Y
        ring.append(nid(nx - 1, j))
    for i in range(nx - 1, 0, -1):   # top edge j=ny-1, -X
        ring.append(nid(i, ny - 1))
    for j in range(ny - 1, 0, -1):   # left edge i=0, -Y
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
    tf.add_header("TOMAWAC SANDBOX " + os.path.basename(path),
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


def write_cli(mesh, path, liquid_x0=False):
    """All-solid ring (LIHBOR=2). If liquid_x0, the upwind wall x=0 becomes a
    prescribed-spectrum liquid boundary (LIHBOR=1) for the shoaling swell case."""
    ring = mesh["ring"]
    X = mesh["X"]
    lines = []
    for k in range(mesh["nptfr"]):
        node0 = int(ring[k])
        node1 = node0 + 1
        rank = k + 1
        # KENT=5 marks an incident-wave boundary where TOMAWAC imposes the
        # prescribed boundary spectrum (limwac fills FBOR where LIFBOR==KENT);
        # KLOG=2 is a solid coastline. KINC=1 is NOT the spectrum-impose code.
        solid = not (liquid_x0 and X[node0] <= mesh["dx"] * 0.5)
        code = 2 if solid else 5
        lih = liu = liv = lit = code
        lines.append(
            f"{lih} {liu} {liv}  0.000 0.000 0.000 0.000  {lit}  0.000 0.000 0.000 "
            f"{node1:>11d} {rank:>11d}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# 2. TOMAWAC steering-file author
# --------------------------------------------------------------------------
def write_cas(path, geo, cli, res, *, title, ndir, nfreq, fmin, fratio,
              dt, nstep, wind_u=0.0, wind_v=0.0, stationary_wind=None,
              bc_spectrum=False, bc_hs=0.0, bc_fp=0.0, init_hs=0.01,
              breaking=False, bottom_friction=False, fbcoef=0.038,
              fortran_file=None, stationary_current=False):
    L = []
    A = L.append
    A(f"TITLE : '{title}'")
    A(f"GEOMETRY FILE : '{geo}'")
    if fortran_file:
        A(f"FORTRAN FILE : '{fortran_file}'")
    A(f"BOUNDARY CONDITIONS FILE : '{cli}'")
    A(f"2D RESULTS FILE : '{res}'")
    A("2D RESULTS FILE FORMAT : 'SERAFIN'")
    A("VARIABLES FOR 2D GRAPHIC PRINTOUTS : 'HM0;WD;TM01;DMOY;TPD'")
    A(f"NUMBER OF DIRECTIONS : {ndir}")
    A(f"NUMBER OF FREQUENCIES : {nfreq}")
    A(f"MINIMAL FREQUENCY : {fmin}")
    A(f"FREQUENTIAL RATIO : {fratio}")
    A(f"TIME STEP : {dt}")
    A(f"NUMBER OF TIME STEP : {nstep}")
    A("PERIOD FOR GRAPHIC PRINTOUTS : " + str(max(1, nstep)))
    A("PERIOD FOR LISTING PRINTOUTS : " + str(max(1, nstep // 4)))
    # --- source terms ---
    wind_on = 1 if stationary_wind else 0
    A("CONSIDERATION OF SOURCE TERMS : YES")
    A(f"LINEAR WAVE GROWTH : {wind_on}")   # Cavaleri-Malanotte-Rizzoli: seeds empty
                                           # downwind bins so WAM4 exp input bootstraps
    A(f"WIND GENERATION : {wind_on}")      # 1 = WAM cycle 4 growth (Janssen)
    A(f"WHITE CAPPING DISSIPATION : {wind_on}")  # 1 = WAM cycle 4
    A("NON-LINEAR TRANSFERS BETWEEN FREQUENCIES : 1")  # DIA
    A("BOTTOM FRICTION DISSIPATION : " + ("1" if bottom_friction else "0"))
    if bottom_friction:
        A(f"BOTTOM FRICTION COEFFICIENT : {fbcoef}")
    A("DEPTH-INDUCED BREAKING DISSIPATION : " + ("1" if breaking else "0"))
    if stationary_current:
        A("CONSIDERATION OF A STATIONARY CURRENT : YES")
    if breaking:
        A("DEPTH-INDUCED BREAKING 1 (BJ) COEFFICIENT GAMMA1 : 0.88")
        A("DEPTH-INDUCED BREAKING 1 (BJ) COEFFICIENT GAMMA2 : 0.8")
    # --- wind forcing ---
    if stationary_wind:
        A("CONSIDERATION OF A WIND : YES")
        A("STATIONARY WIND : YES")
        A(f"WIND VELOCITY ALONG X : {wind_u}")
        A(f"WIND VELOCITY ALONG Y : {wind_v}")
        A("WIND GENERATION COEFFICIENT : 1.0")
        A("AIR DENSITY : 1.225")
        A("WATER DENSITY : 1000.")
    # --- initial spectrum: INISPE=6 = parameterised JONSWAP keyed off
    #     INITIAL SIGNIFICANT WAVE HEIGHT + PEAK FREQUENCY (works with any
    #     wind; INISPE=1 instead builds from wind+fetch and needs a fetch). ---
    A("TYPE OF INITIAL DIRECTIONAL SPECTRUM : 6")
    A(f"INITIAL SIGNIFICANT WAVE HEIGHT : {init_hs}")
    A("INITIAL PEAK FREQUENCY : 0.3")
    A("INITIAL PEAK FACTOR : 3.3")
    A("INITIAL DIRECTIONAL SPREAD 1 : 20.")
    A("INITIAL MAIN DIRECTION 1 : 0.")
    # --- boundary spectrum (shoaling swell case) ---
    if bc_spectrum:
        # LIMSPE=6 = parameterised JONSWAP keyed off BOUNDARY Hs + PEAK FREQUENCY
        # (works with zero wind; LIMSPE=1 would need a wind+fetch instead).
        A("TYPE OF BOUNDARY DIRECTIONAL SPECTRUM : 6")
        A(f"BOUNDARY SIGNIFICANT WAVE HEIGHT : {bc_hs}")
        A(f"BOUNDARY PEAK FREQUENCY : {bc_fp}")
        A("BOUNDARY PEAK FACTOR : 3.3")
        A("BOUNDARY DIRECTIONAL SPREAD 1 : 20.")
        A("BOUNDARY MAIN DIRECTION 1 : 0.")
    A("SPHERICAL COORDINATES : NO")
    # trig convention: direction 0 deg = +X axis, aligned with +X wind forcing
    A("TRIGONOMETRICAL CONVENTION : YES")
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")


# --------------------------------------------------------------------------
# 3. Run + extract HM0
# --------------------------------------------------------------------------
def run_tomawac(cas, tag):
    env = dict(os.environ)
    cmd = ["tomawac.py", cas, "--ncsize=1"]
    p = subprocess.run(cmd, cwd=WORK, env=env, capture_output=True, text=True,
                       timeout=1800)
    log = os.path.join(WORK, f"tomawac_{tag}.log")
    with open(log, "w") as f:
        f.write("CMD: " + " ".join(cmd) + "\n\nSTDOUT:\n" + p.stdout +
                "\n\nSTDERR:\n" + p.stderr)
    return p.returncode, p.stdout, p.stderr


def read_hm0(res_path):
    from data_manip.extraction.telemac_file import TelemacFile
    tf = TelemacFile(res_path)
    nt = tf.ntimestep
    hm0 = tf.get_data_value("WAVE HEIGHT HM0", nt - 1)
    X = np.array(tf.meshx)
    Y = np.array(tf.meshy)
    tf.close()
    return X, Y, hm0


def centerline_profile(X, Y, hm0, mesh, along="x"):
    """Hs sampled along the mid-line (j = ny//2) versus X (fetch)."""
    ny = mesh["ny"]
    nx = mesh["nx"]
    jmid = ny // 2
    xs, hs = [], []
    for i in range(nx):
        n = i * ny + jmid
        xs.append(float(X[n]))
        hs.append(float(hm0[n]))
    return np.array(xs), np.array(hs)


# --------------------------------------------------------------------------
# CASES
# --------------------------------------------------------------------------
def cerc_hs(U, fetch_m):
    """CERC/SPM fetch-limited significant wave height (deep water)."""
    xstar = 9.81 * fetch_m / (U * U)
    return 0.0016 * np.sqrt(xstar) * U * U / 9.81


def case_fetch(Lx_km, U, depth=60.0, dx=1500.0, Ly=30000.0, hours=6.0,
               dt=120.0, tag="fetch", bottom_friction=False):
    """Fetch-limited wind-wave growth on a WIDE deep basin. The wide (30 km)
    domain keeps the centerline free of lateral side-wall spreading loss so the
    along-fetch profile reproduces the 1D CERC/SPM fetch law (replicates the
    physics of the official tomawac/fetch_limited example)."""
    Lx = Lx_km * 1000.0
    # BOTTOM is bed ELEVATION; submerged bed is NEGATIVE (depth = SWL 0 - bottom)
    mesh = build_grid(Lx, Ly, dx, lambda X, Y: np.full_like(X, -depth))
    geo, cli, res = f"geo_{tag}.slf", f"bc_{tag}.cli", f"res_{tag}.slf"
    write_slf(mesh, os.path.join(WORK, geo))
    write_cli(mesh, os.path.join(WORK, cli))
    nstep = int(hours * 3600.0 / dt)
    write_cas(os.path.join(WORK, f"tom_{tag}.cas"), geo, cli, res,
              title=f"FETCH {Lx_km}km U{U}",
              ndir=24, nfreq=32, fmin=0.04, fratio=1.1, dt=dt, nstep=nstep,
              wind_u=U, wind_v=0.0, stationary_wind=True, init_hs=0.02,
              bottom_friction=bottom_friction)
    rc, out, err = run_tomawac(f"tom_{tag}.cas", tag)
    if rc != 0:
        return dict(tag=tag, rc=rc, ok=False, err=err[-2000:])
    X, Y, hm0 = read_hm0(os.path.join(WORK, res))
    xs, hs = centerline_profile(X, Y, hm0, mesh)
    cerc = [float(cerc_hs(U, x)) for x in xs[1:]]
    return dict(tag=tag, rc=rc, ok=True, U=U, Lx_km=Lx_km, depth=depth,
                x_km=(xs / 1000.0).tolist(), hs=hs.tolist(),
                cerc_x_km=(xs[1:] / 1000.0).tolist(), cerc_hs=cerc,
                hs_max=float(np.nanmax(hs)), hs_end=float(hs[-1]),
                cerc_end=float(cerc_hs(U, xs[-1])))


def case_shoal(U=0.0, tag="shoal"):
    """Offshore swell (prescribed at x=0) shoaling up a linear beach.
    Depth 40 m offshore -> 3 m nearshore over 20 km. No wind."""
    Lx = 20000.0
    Ly = 6000.0
    dx = 500.0
    d_off, d_near = 40.0, 3.0

    def depth_fn(X, Y):
        # bed ELEVATION (negative = submerged): deep offshore, shoaling to shore
        return -(d_off - (d_off - d_near) * (X / Lx))

    mesh = build_grid(Lx, Ly, dx, depth_fn)
    geo, cli, res = f"geo_{tag}.slf", f"bc_{tag}.cli", f"res_{tag}.slf"
    write_slf(mesh, os.path.join(WORK, geo))
    write_cli(mesh, os.path.join(WORK, cli), liquid_x0=True)
    dt, nstep = 30.0, 240
    write_cas(os.path.join(WORK, f"tom_{tag}.cas"), geo, cli, res,
              title="SHOALING SWELL", ndir=24, nfreq=25, fmin=0.04,
              fratio=1.1, dt=dt, nstep=nstep, stationary_wind=None,
              bc_spectrum=True, bc_hs=1.5, bc_fp=0.1, init_hs=0.01,
              breaking=True)
    rc, out, err = run_tomawac(f"tom_{tag}.cas", tag)
    if rc != 0:
        return dict(tag=tag, rc=rc, ok=False, err=err[-2000:])
    X, Y, hm0 = read_hm0(os.path.join(WORK, res))
    xs, hs = centerline_profile(X, Y, hm0, mesh)
    depth_prof = d_off - (d_off - d_near) * (xs / Lx)
    return dict(tag=tag, rc=rc, ok=True,
                x_km=(xs / 1000.0).tolist(), depth=depth_prof.tolist(),
                hs=hs.tolist(), hs_offshore=float(hs[1]),
                hs_max=float(np.nanmax(hs)))


USER_ANACOS_TMPL = """!                   **********************
                    SUBROUTINE USER_ANACOS
!                   **********************
      USE DECLARATIONS_SPECIAL
      USE DECLARATIONS_TOMAWAC, ONLY : UC, VC, X, NPOIN2
      USE INTERFACE_TOMAWAC, EX_USER_ANACOS => USER_ANACOS
      IMPLICIT NONE
      INTEGER IP
      DOUBLE PRECISION UCMAX, LXDOM
      UCMAX=%(UC).4fD0
      LXDOM=%(LX).1fD0
!     current RAMPS linearly 0 -> UCMAX across the domain: waves entering at
!     x=0 (calm) propagate into a strengthening current (a gradient is required
!     -- a spatially UNIFORM current leaves steady-state Hs unchanged).
      DO IP=1,NPOIN2
        UC(IP)=UCMAX*X(IP)/LXDOM
        VC(IP)=0.D0
      ENDDO
      RETURN
      END
"""


def case_current(uc, tag="cur", depth=40.0, bc_hs=1.5, bc_fp=0.09):
    """Prescribed swell propagating +X into a current that RAMPS 0 -> uc across
    the domain. Opposing ramp (uc<0) amplifies Hs downstream (action bunching
    against an adverse current); following ramp (uc>0) lowers it. UC is imposed
    via a compiled USER_ANACOS (TOMAWAC's analytical-current hook; UCONST is 0
    in the shipped anacos.f)."""
    Lx, Ly, dx = 30000.0, 12000.0, 1000.0
    mesh = build_grid(Lx, Ly, dx, lambda X, Y: np.full_like(X, -depth))
    geo, cli, res = f"geo_{tag}.slf", f"bc_{tag}.cli", f"res_{tag}.slf"
    fort = f"user_{tag}.f"
    write_slf(mesh, os.path.join(WORK, geo))
    write_cli(mesh, os.path.join(WORK, cli), liquid_x0=True)
    with open(os.path.join(WORK, fort), "w") as f:
        f.write(USER_ANACOS_TMPL % {"UC": uc, "LX": Lx})
    write_cas(os.path.join(WORK, f"tom_{tag}.cas"), geo, cli, res,
              title=f"WAVE-CURRENT UC{uc}", ndir=24, nfreq=32, fmin=0.04,
              fratio=1.1, dt=60.0, nstep=180, stationary_wind=None,
              bc_spectrum=True, bc_hs=bc_hs, bc_fp=bc_fp, init_hs=0.01,
              fortran_file=fort, stationary_current=True)
    rc, out, err = run_tomawac(f"tom_{tag}.cas", tag)
    if rc != 0:
        return dict(tag=tag, rc=rc, ok=False, err=err[-2500:])
    X, Y, hm0 = read_hm0(os.path.join(WORK, res))
    xs, hs = centerline_profile(X, Y, hm0, mesh)
    return dict(tag=tag, rc=rc, ok=True, uc=uc,
                x_km=(xs / 1000.0).tolist(), hs=hs.tolist(),
                hs_offshore=float(hs[1]), hs_downstream=float(hs[-2]))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    out = {}
    if mode == "smoke":
        out["smoke"] = case_fetch(40.0, 18.0, hours=3.0, tag="smoke")
    elif mode == "fetch_pair":
        # discriminating: same wind, short vs long fetch; then wind sweep
        out["short"] = case_fetch(10.0, 20.0, tag="f_short")
        out["long"] = case_fetch(60.0, 20.0, tag="f_long")
        out["wind_lo"] = case_fetch(40.0, 10.0, tag="f_wlo")
        out["wind_hi"] = case_fetch(40.0, 25.0, tag="f_whi")
    elif mode == "shoal":
        out["shoal"] = case_shoal()
    elif mode == "friction":
        # discriminating: shallow shelf, friction OFF vs ON -> Hs lower with friction
        out["fric_off"] = case_fetch(40.0, 20.0, depth=8.0, tag="fr_off",
                                     bottom_friction=False)
        out["fric_on"] = case_fetch(40.0, 20.0, depth=8.0, tag="fr_on",
                                    bottom_friction=True)
    elif mode == "current":
        # discriminating: opposing vs no vs following current on the same swell
        out["oppose"] = case_current(-2.5, tag="c_opp")
        out["none"] = case_current(0.0, tag="c_non")
        out["follow"] = case_current(+2.5, tag="c_fol")
    print(json.dumps(out))
