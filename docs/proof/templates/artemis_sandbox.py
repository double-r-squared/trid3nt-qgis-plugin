"""ARTEMIS local-first physics sandbox (ADR 0237).

Runs INSIDE trid3nt-local/telemac:latest (needs the baked artemis binary +
the opentelemac python SELAFIN API). ARTEMIS is TELEMAC's phase-resolving
elliptic mild-slope (Berkhoff) wave-agitation solver: steady-state
diffraction / refraction / partial reflection in harbours and around
structures. Geography-free analytic verification replicates the classic
ARTEMIS validation set so the proof clears the citations law without a US site:

  * RESONANCE (seiche): a rectangular basin forced at one end resonates when
    its length is a multiple of a half-wavelength; the response amplifies at
    the analytic mode periods T_n = 2 L / (n c), c = sqrt(g h). Discriminating:
    forced AT resonance -> large amplification; OFF resonance -> near unity.
  * DIFFRACTION (Sommerfeld semi-infinite breakwater): waves diffract around a
    breakwater tip into the geometric shadow. Penny-Price / Sommerfeld analytic
    diffraction coefficient K_d = H/H0 = 0.5 on the shadow-boundary ray through
    the tip, decaying deeper into the lee and recovering to ~1 in the lit zone.
    Discriminating: breakwater PRESENT (shadow) vs ABSENT (K_d ~ 1 uniform).
  * SHOAL FOCUSING (Berkhoff-Booij-Radder 1982 elliptic shoal): the canonical
    mild-slope benchmark; a monochromatic wave refracts+focuses over an
    elliptic shoal on a 1:50 beach, producing an amplification peak H/H0 ~ 2
    down-wave of the shoal. Discriminating: shoal PRESENT (focus) vs flat bed
    (H/H0 ~ 1).

Citations replicated: Berkhoff, Booij & Radder (1982), Coastal Eng. 6:255-279
(elliptic shoal); Penny & Price (1952) / Sommerfeld (1896) semi-infinite
screen diffraction; standard closed-basin seiche formula T_n = 2L/(n sqrt(gh)).
The Berkhoff bathymetry is the EXACT rotated-ellipse corfon from the official
opentelemac bosse_elliptique case (art_corfon.f), baked into BOTTOM here.
"""
import json
import os
import subprocess
import sys
import numpy as np

WORK = "/data"
G = 9.81


# --------------------------------------------------------------------------
# 1. General structured-grid mesh with optional node mask + robust CCW
#    boundary-ring extraction (handles the notched breakwater domain).
# --------------------------------------------------------------------------
def build_mesh(Lx, Ly, dx, depth_fn, dy=None, keep_fn=None, x0=0.0, y0=0.0):
    dy = dy or dx
    nx = int(round(Lx / dx)) + 1
    ny = int(round(Ly / dy)) + 1
    xs = x0 + np.linspace(0.0, Lx, nx)
    ys = y0 + np.linspace(0.0, Ly, ny)

    Xg = np.repeat(xs, ny)          # grid node (i,j) -> gid = i*ny + j
    Yg = np.tile(ys, nx)
    keep = np.ones(nx * ny, dtype=bool)
    if keep_fn is not None:
        keep = keep_fn(Xg, Yg).astype(bool)

    # compact re-index of kept nodes
    newid = -np.ones(nx * ny, dtype=np.int64)
    kept_gids = np.nonzero(keep)[0]
    newid[kept_gids] = np.arange(len(kept_gids))
    X = Xg[kept_gids].astype(np.float64)
    Y = Yg[kept_gids].astype(np.float64)
    npoin = len(kept_gids)

    def gid(i, j):
        return i * ny + j

    tris = []
    for i in range(nx - 1):
        for j in range(ny - 1):
            a, b, c, d = gid(i, j), gid(i + 1, j), gid(i + 1, j + 1), gid(i, j + 1)
            ka, kb, kc, kd = keep[a], keep[b], keep[c], keep[d]
            # marching cell: a partially-masked cell with exactly 3 kept corners
            # is filled with its single CCW triangle so slot/mouth edges do not
            # expose stray boundary nodes (both-diagonal drop leaves a hole).
            if ka and kb and kc and kd:
                tris.append((newid[a], newid[b], newid[c]))   # CCW
                tris.append((newid[a], newid[c], newid[d]))   # CCW
            elif ka and kb and kc:
                tris.append((newid[a], newid[b], newid[c]))
            elif ka and kc and kd:
                tris.append((newid[a], newid[c], newid[d]))
            elif ka and kb and kd:
                tris.append((newid[a], newid[b], newid[d]))
            elif kb and kc and kd:
                tris.append((newid[b], newid[c], newid[d]))
    ikle = np.array(tris, dtype=np.int32)

    # boundary = directed edge whose reverse is absent (CCW around domain)
    dedges = set()
    for (a, b, c) in ikle:
        dedges.add((a, b))
        dedges.add((b, c))
        dedges.add((c, a))
    nxt = {}
    for (u, v) in dedges:
        if (v, u) not in dedges:
            nxt[u] = v
    if len(nxt) == 0:
        raise RuntimeError("no boundary edges")
    start = min(nxt)                 # deterministic ring start
    ring = [start]
    cur = nxt[start]
    guard = 0
    while cur != start:
        ring.append(cur)
        cur = nxt[cur]
        guard += 1
        if guard > len(nxt) + 5:
            raise RuntimeError("boundary ring did not close (multi-ring domain)")
    ring = np.array(ring, dtype=np.int32)
    nptfr = len(ring)

    ipob = np.zeros(npoin, dtype=np.int32)
    for rank, n in enumerate(ring, start=1):
        ipob[n] = rank

    Z = depth_fn(X, Y).astype(np.float64)
    return dict(X=X, Y=Y, ikle=ikle, ipob=ipob, ring=ring, nptfr=nptfr,
                npoin=npoin, xs=xs, ys=ys, nx=nx, ny=ny, Z=Z,
                Lx=Lx, Ly=Ly, dx=dx, dy=dy, x0=x0, y0=y0)


def write_slf(mesh, path):
    from data_manip.extraction.telemac_file import TelemacFile
    if os.path.exists(path):
        os.remove(path)
    tf = TelemacFile(path, access="w")
    tf.add_header("ARTEMIS SANDBOX " + os.path.basename(path),
                  date=np.array([2026, 8, 13, 0, 0, 0]))
    tf.add_mesh(mesh["X"], mesh["Y"], mesh["ikle"], z=mesh["Z"])
    tf._ipob3 = mesh["ipob"].astype(np.int32)
    tf._ipob2 = tf._ipob3
    tf._nptfr = int(mesh["nptfr"])
    tf._nbor = (mesh["ring"] + 1).astype(np.int32)
    tf._knolg = np.arange(1, mesh["npoin"] + 1, dtype=np.int32)
    tf.add_variable("BOTTOM          ", "M               ")
    tf.add_data_value("BOTTOM          ", 0, mesh["Z"])
    tf.write()
    tf.close()


# --------------------------------------------------------------------------
# 2. ARTEMIS boundary-conditions (.cli) author.
#    Ground-truth column semantics decoded from the official example decks
#    (bosse_elliptique / reso_canal / ile_para) + confirmed by borh.f:
#      col1  LIHBOR  boundary type: 1=KINC incident, 2=KLOG solid, 4=KSORT free exit
#      col4  HB      incident wave height  (nonzero only on KINC boundaries)
#      col5  TETAP   wall / exit reference angle, DEGREES
#      col6  ALFAP   phase (deg), 0 here
#      col7  RP      reflection coefficient (1=full reflect, 0=absorbing)
# --------------------------------------------------------------------------
def write_cli(mesh, path, classify):
    """classify(x, y) -> (lihbor, HB, TETAP, ALFAP, RP)."""
    ring = mesh["ring"]
    X, Y = mesh["X"], mesh["Y"]
    lines = []
    for k in range(mesh["nptfr"]):
        n0 = int(ring[k])
        lih, hb, tetap, alfap, rp = classify(float(X[n0]), float(Y[n0]))
        liu = liv = 5 if lih in (1, 2) else 2
        lit = 2
        node1 = n0 + 1
        rank = k + 1
        lines.append(
            f"{lih} {liu} {liv}  {hb:.4f} {tetap:.3f} {alfap:.3f} {rp:.3f}  "
            f"{lit}  0.000 0.000 0.000  {node1:>11d} {rank:>11d}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# 3. ARTEMIS steering-file author.
# --------------------------------------------------------------------------
def write_cas(path, geo, cli, res, *, title, wave_period, wave_dir,
              swl=0.0, breaking=False, rapid_topo=None, period_scan=None,
              phase_ref=None):
    L = []
    A = L.append
    A(f"TITLE : '{title}'")
    A(f"GEOMETRY FILE : '{geo}'")
    A(f"BOUNDARY CONDITIONS FILE : '{cli}'")
    A(f"RESULTS FILE : '{res}'")
    A("RESULTS FILE FORMAT : 'SERAFIN'")
    A("VARIABLES FOR GRAPHIC PRINTOUTS : 'HS,PHAS,ZS,ZF'")
    A("INITIAL CONDITIONS : 'CONSTANT ELEVATION'")
    A(f"INITIAL WATER LEVEL : {swl}")
    A("MATRIX STORAGE : 3")
    A("SOLVER : 8")
    A("MAXIMUM NUMBER OF ITERATIONS FOR SOLVER : 4000")
    A(f"WAVE PERIOD : {wave_period}")
    A(f"DIRECTION OF WAVE PROPAGATION : {wave_dir}")
    A("BREAKING : " + ("YES" if breaking else "NO"))
    A("WAVE HEIGHTS SMOOTHING : NO")
    if rapid_topo is not None:
        A(f"RAPIDLY VARYING TOPOGRAPHY : {rapid_topo}")
    if period_scan is not None:
        p0, p1, ps = period_scan
        A("PERIOD SCANNING : YES")
        A(f"BEGINNING PERIOD FOR PERIOD SCANNING : {p0}")
        A(f"ENDING PERIOD FOR PERIOD SCANNING : {p1}")
        A(f"STEP FOR PERIOD SCANNING : {ps}")
    if phase_ref is not None:
        A(f"PHASE REFERENCE COORDINATES : {phase_ref[0]}; {phase_ref[1]}")
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")


def run_artemis(cas, tag):
    cmd = ["artemis.py", cas, "--ncsize=1"]
    p = subprocess.run(cmd, cwd=WORK, env=dict(os.environ),
                       capture_output=True, text=True, timeout=3600)
    with open(os.path.join(WORK, f"artemis_{tag}.log"), "w") as f:
        f.write("CMD: " + " ".join(cmd) + "\n\nSTDOUT:\n" + p.stdout +
                "\n\nSTDERR:\n" + p.stderr)
    return p.returncode, p.stdout, p.stderr


def read_hs(res_path, frame=-1, all_frames=False):
    from data_manip.extraction.telemac_file import TelemacFile
    tf = TelemacFile(res_path)
    nt = tf.ntimestep
    X = np.array(tf.meshx)
    Y = np.array(tf.meshy)
    if all_frames:
        hs = np.array([tf.get_data_value("WAVE HEIGHT", it) for it in range(nt)])
        tf.close()
        return X, Y, hs, nt
    it = nt - 1 if frame < 0 else frame
    hs = tf.get_data_value("WAVE HEIGHT", it)
    tf.close()
    return X, Y, np.asarray(hs), nt


# --------------------------------------------------------------------------
# analytic references
# --------------------------------------------------------------------------
def dispersion_k(T, h):
    """Solve omega^2 = g k tanh(k h) for k (Newton)."""
    omega = 2 * np.pi / T
    k = omega * omega / G          # deep-water seed
    for _ in range(200):
        th = np.tanh(k * h)
        f = G * k * th - omega * omega
        df = G * th + G * k * h * (1 - th * th)
        dk = f / df
        k -= dk
        if abs(dk) < 1e-12:
            break
    return k


def seiche_periods(Lbasin, h, nmodes=5):
    """Closed-basin longitudinal resonance: k_n L = n pi -> lambda_n = 2L/n."""
    out = []
    for n in range(1, nmodes + 1):
        kn = n * np.pi / Lbasin
        omega = np.sqrt(G * kn * np.tanh(kn * h))
        out.append(2 * np.pi / omega)
    return out


# --------------------------------------------------------------------------
# CASE A: harbour agitation / resonance -- a rectangular harbour connected to
# the open sea through a narrow mouth. A perfectly-radiating open end cannot
# trap energy (it just gives the trivial 2x standing wave at all periods); a
# CONSTRICTED mouth makes the basin a frequency-selective resonator, so the
# in-harbour amplification spikes at the quarter-wave modes and is small off
# resonance. Open-closed quarter-wave ladder: T_n = 4 L_h / ((2n-1) c).
# --------------------------------------------------------------------------
def harbour_periods(Lh, h, nmodes=4):
    out = []
    for n in range(1, nmodes + 1):
        # quarter-wave (open mouth / closed back): kL = (2n-1) pi/2
        kn = (2 * n - 1) * np.pi / (2 * Lh)
        omega = np.sqrt(G * kn * np.tanh(kn * h))
        out.append(2 * np.pi / omega)
    return out


def case_resonance(Wx=100.0, y_sea=150.0, Lh=500.0, h=10.0, H0=1.0,
                   dx=12.5, mouth=25.0, tag="reso", scan=(30.0, 244.0, 1.0)):
    Ly = y_sea + Lh
    y_wall = y_sea
    dy = dx

    def keep_fn(X, Y):
        # solid dividing wall at y=y_wall spanning the width, minus a central
        # mouth gap; realised by removing the nearest grid row outside the gap
        in_wall_row = np.abs(Y - y_wall) <= dy * 0.5
        in_mouth = np.abs(X - Wx * 0.5) <= mouth * 0.5
        return ~(in_wall_row & (~in_mouth))

    mesh = build_mesh(Wx, Ly, dx, lambda X, Y: np.full_like(X, -h),
                      dy=dy, keep_fn=keep_fn)

    def classify(x, y):
        # TETAP (col5) is the BOUNDARY TANGENT angle: vertical walls -> 90,
        # horizontal boundaries -> 0 (decoded from the example decks; a wrong
        # angle mis-orients the reflection/radiation condition).
        vertical = (x <= dx * 0.5) or (x >= Wx - dx * 0.5)
        tang = 90.0 if vertical else 0.0
        if y <= dy * 0.5:                               # incident sea end
            return (1, H0, 0.0, 0.0, 0.0)
        on_wall = (abs(y - y_wall) <= dy * 1.5) and (abs(x - Wx * 0.5) > mouth * 0.5)
        if on_wall:                                     # dividing wall (horizontal)
            return (2, 0.0, 0.0, 0.0, 1.0)
        if y < y_wall:                                  # sea side walls: radiate
            return (4, 0.0, tang, 0.0, 0.0)
        return (2, 0.0, tang, 0.0, 1.0)                 # harbour walls: reflect

    geo, cli, res = f"geo_{tag}.slf", f"bc_{tag}.cli", f"res_{tag}.slf"
    write_slf(mesh, os.path.join(WORK, geo))
    write_cli(mesh, os.path.join(WORK, cli), classify)
    write_cas(os.path.join(WORK, f"art_{tag}.cas"), geo, cli, res,
              title="HARBOUR RESONANCE", wave_period=scan[0],
              wave_dir=90.0, swl=0.0, period_scan=scan,
              phase_ref=(Wx * 0.5, y_wall + Lh * 0.5))
    rc, out, err = run_artemis(f"art_{tag}.cas", tag)
    if rc != 0:
        return dict(tag=tag, ok=False, rc=rc, err=err[-3000:])
    X, Y, hs, nt = read_hs(os.path.join(WORK, res), all_frames=True)
    periods = [scan[0] + i * scan[2] for i in range(nt)]
    inh = (Y > y_wall + 2 * dy)                         # harbour interior
    backwall = (Y > y_wall + Lh - 3 * dy)               # near closed back wall
    resp = np.array([float(np.mean(hs[i][inh]) / H0) for i in range(nt)])
    back = np.array([float(np.max(hs[i][backwall]) / H0) for i in range(nt)])
    peaks = []
    for i in range(1, len(resp) - 1):
        if resp[i] > resp[i - 1] and resp[i] >= resp[i + 1] and resp[i] > 1.3:
            peaks.append((float(periods[i]), float(resp[i]), float(back[i])))
    # dump mode-shape field at the strongest resonance + an off-resonance period
    i_res = int(np.argmax(resp))
    off = np.array(resp);  off[max(0, i_res - 3):i_res + 4] = 1e9
    i_off = int(np.argmin(off))
    np.savez(os.path.join(WORK, f"field_{tag}.npz"), X=X, Y=Y,
             hs_res=hs[i_res], hs_off=hs[i_off], Z=mesh["Z"],
             T_res=periods[i_res], T_off=periods[i_off])
    return dict(tag=tag, ok=True, rc=rc, Lh=Lh, h=h, H0=H0, mouth=mouth,
                periods=periods, response=resp.tolist(), backwall=back.tolist(),
                peaks=peaks, analytic_Tn=harbour_periods(Lh, h),
                resp_max=float(resp.max()), resp_min=float(resp.min()),
                back_max=float(back.max()), back_min=float(back.min()))


# --------------------------------------------------------------------------
# CASE B: Sommerfeld semi-infinite breakwater diffraction
# --------------------------------------------------------------------------
def case_breakwater(present=True, Lx=600.0, Ly=400.0, h=10.0, T=8.0, H0=1.0,
                    dx=8.0, y_bw=120.0, x_tip=300.0, tag=None):
    tag = tag or ("bw_on" if present else "bw_off")
    dy = dx

    def keep_fn(X, Y):
        if not present:
            return np.ones_like(X, dtype=bool)
        # remove the single grid row nearest y_bw for x <= x_tip -> a thin
        # (2 dy) breakwater attached to the left wall, tip at (x_tip, y_bw)
        return ~((np.abs(Y - y_bw) <= dy * 0.5) & (X <= x_tip))

    mesh = build_mesh(Lx, Ly, dx, lambda X, Y: np.full_like(X, -h),
                      dy=dy, keep_fn=keep_fn)
    wdir = 90.0                                          # +Y (trig: 0=+X)

    def classify(x, y):
        # entire outer ring = incident (KINC imposes the plane wave AND radiates
        # the scattered field, per the ile_para island-diffraction convention);
        # only the breakwater faces are solid. Free-exit on the vertical side
        # walls is degenerate for a wall-parallel incident wave and reflects.
        on_bw = present and (abs(y - y_bw) <= dy * 1.5) and (x <= x_tip + dx * 0.5)
        if on_bw:                                       # breakwater faces + tip
            return (2, 0.0, 0.0, 0.0, 1.0)
        return (1, H0, 0.0, 0.0, 0.0)                   # incident (all outer)

    geo, cli, res = f"geo_{tag}.slf", f"bc_{tag}.cli", f"res_{tag}.slf"
    write_slf(mesh, os.path.join(WORK, geo))
    write_cli(mesh, os.path.join(WORK, cli), classify)
    write_cas(os.path.join(WORK, f"art_{tag}.cas"), geo, cli, res,
              title=f"BREAKWATER present={present}", wave_period=T,
              wave_dir=wdir, swl=0.0)
    rc, out, err = run_artemis(f"art_{tag}.cas", tag)
    if rc != 0:
        return dict(tag=tag, ok=False, rc=rc, err=err[-3000:])
    X, Y, hs, nt = read_hs(os.path.join(WORK, res))
    np.savez(os.path.join(WORK, f"field_{tag}.npz"), X=X, Y=Y, hs=hs, Z=mesh["Z"])
    # incident reference H0m: mean HS on a strip below the breakwater
    below = (Y < y_bw - dx) & (Y > dx)
    h0m = float(np.mean(hs[below]))
    # K_d transect at y = y_bw + 130 across x
    yt = y_bw + 130.0
    band = np.abs(Y - yt) <= dy
    xs = X[band]
    kd = hs[band] / h0m
    order = np.argsort(xs)
    xs, kd = xs[order], kd[order]
    lam = 2 * np.pi / dispersion_k(T, h)
    # K_d at the shadow-boundary line (x = x_tip)
    kd_shadow = float(np.interp(x_tip, xs, kd))
    kd_deep = float(np.mean(kd[xs < x_tip - 2 * lam])) if np.any(xs < x_tip - 2 * lam) else None
    kd_lit = float(np.mean(kd[xs > x_tip + 2 * lam])) if np.any(xs > x_tip + 2 * lam) else None
    return dict(tag=tag, ok=True, rc=rc, present=present, T=T, h=h,
                wavelength=float(lam), h0m=h0m, x_tip=x_tip, y_transect=yt,
                xs=xs.tolist(), kd=kd.tolist(), kd_shadow=kd_shadow,
                kd_deep_shadow=kd_deep, kd_lit=kd_lit,
                hs_max=float(np.max(hs)), hs_min=float(np.min(hs)))


# --------------------------------------------------------------------------
# CASE C: Berkhoff-Booij-Radder (1982) elliptic shoal focusing
#   EXACT bathymetry from the official bosse_elliptique art_corfon.f
# --------------------------------------------------------------------------
def berkhoff_bottom(X, Y):
    cosa, sina = 0.939692621, 0.342020143            # 20 deg rotation
    t1 = (X - 15.75) * cosa - (Y - 18.5) * sina
    t2 = (X - 15.75) * sina + (Y - 18.5) * cosa
    zf = np.empty_like(X)
    flat = t2 > 5.2
    ell = (~flat) & (((t1 / 4.0) ** 2 + (t2 / 3.0) ** 2) <= 1.0)
    slope = (~flat) & (~ell)
    zf[flat] = -0.45
    zf[slope] = -0.45 - 0.02 * (-5.2 + t2[slope])
    zf[ell] = (-0.45 - 0.02 * (-5.2 + t2[ell]) + 0.5 *
               np.sqrt(np.clip(1.0 - (t1[ell] / 5.0) ** 2
                               - (t2[ell] / 3.75) ** 2, 0.0, None)) - 0.3)
    return zf


def case_shoal(present=True, dx=0.15, H0=0.0464, T=1.0, tag=None):
    tag = tag or ("shoal_on" if present else "shoal_flat")
    Lx, Ly = 30.0, 35.0
    if present:
        depth_fn = berkhoff_bottom
    else:
        # flat control at the shoal-region mean depth (~0.45 m plane offshore)
        depth_fn = lambda X, Y: np.full_like(X, -0.45)
    mesh = build_mesh(Lx, Ly, dx, depth_fn, dy=dx)
    wdir = -90.0                                         # -Y (down the beach)

    def classify(x, y):
        if y >= Ly - dx * 0.5:                          # incident (top); dir set
            return (1, H0, 0.0, 0.0, 0.0)               # globally by keyword
        if y <= dx * 0.5:                               # down-wave free exit
            return (4, 0.0, 0.0, 0.0, 0.0)              # TETAP = boundary tangent
        return (2, 0.0, 90.0, 0.0, 0.0)                 # lateral: absorbing (RP=0)

    geo, cli, res = f"geo_{tag}.slf", f"bc_{tag}.cli", f"res_{tag}.slf"
    write_slf(mesh, os.path.join(WORK, geo))
    write_cli(mesh, os.path.join(WORK, cli), classify)
    write_cas(os.path.join(WORK, f"art_{tag}.cas"), geo, cli, res,
              title=f"BERKHOFF SHOAL present={present}", wave_period=T,
              wave_dir=wdir, swl=0.0, rapid_topo=3)
    rc, out, err = run_artemis(f"art_{tag}.cas", tag)
    if rc != 0:
        return dict(tag=tag, ok=False, rc=rc, err=err[-3000:])
    X, Y, hs, nt = read_hs(os.path.join(WORK, res))
    np.savez(os.path.join(WORK, f"field_{tag}.npz"), X=X, Y=Y, hs=hs, Z=mesh["Z"])
    kd = hs / H0
    depth = -mesh["Z"]
    # exclude the near-dry shoreline singularity (the 1:50 slope reaches the
    # waterline down-wave; with BREAKING off, shoaling H ~ h^-1/4 diverges as
    # h -> 0). The physical shoal focus lives at moderate depth.
    good = (depth > 0.12) & (X > 1) & (X < 29) & (Y > 2) & (Y < 34)
    kg = kd[good]
    return dict(tag=tag, ok=True, rc=rc, present=present, T=T, H0=H0,
                kd_focus_p99=float(np.percentile(kg, 99)),
                kd_focus_p95=float(np.percentile(kg, 95)),
                kd_focus_max=float(np.max(kg)),
                kd_mean=float(np.mean(kg)),
                kd_raw_max=float(np.nanmax(kd)))


# --------------------------------------------------------------------------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    out = {}
    if mode == "smoke":
        # tiny constant-depth basin, single period, prove binary + HS read
        m = build_mesh(400.0, 100.0, 20.0, lambda X, Y: np.full_like(X, -10.0), dy=25.0)
        write_slf(m, os.path.join(WORK, "geo_smoke.slf"))
        write_cli(m, os.path.join(WORK, "bc_smoke.cli"),
                  lambda x, y: (1, 1.0, 0.0, 0.0, 0.0) if x <= 10.0
                  else (2, 0.0, 0.0, 0.0, 1.0))
        write_cas(os.path.join(WORK, "art_smoke.cas"), "geo_smoke.slf",
                  "bc_smoke.cli", "res_smoke.slf", title="SMOKE",
                  wave_period=60.0, wave_dir=0.0)
        rc, o, e = run_artemis("art_smoke.cas", "smoke")
        if rc == 0:
            X, Y, hs, nt = read_hs(os.path.join(WORK, "res_smoke.slf"))
            out["smoke"] = dict(ok=True, rc=rc, nt=nt, npoin=int(m["npoin"]),
                                hs_min=float(hs.min()), hs_max=float(hs.max()),
                                hs_mean=float(hs.mean()))
        else:
            out["smoke"] = dict(ok=False, rc=rc, err=e[-3000:])
    elif mode == "resonance":
        out["resonance"] = case_resonance()
    elif mode == "breakwater":
        out["present"] = case_breakwater(present=True)
        out["absent"] = case_breakwater(present=False)
    elif mode == "shoal":
        out["shoal"] = case_shoal(present=True)
        out["flat"] = case_shoal(present=False)
    print(json.dumps(out))
