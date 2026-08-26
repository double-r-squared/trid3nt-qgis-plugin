"""TOMAWAC spectral-wave pipeline: idealized + real-lake wave fields.

The productionized promotion of ``docs/proof/templates/tomawac_sandbox.py`` (the
canonical composer prototype whose physics is PROVEN through the baked tomawac
binary in ``trid3nt-local/telemac:latest``). Runs INSIDE the worker image (needs
the baked ``tomawac`` binary + the opentelemac SELAFIN python API); imports NO
agent code.

Four question classes (the board's four distinct TOMAWAC rows), each a mode:
  * ``fetch_growth``     - fetch-limited wind-wave growth (Hs grows downwind).
  * ``shoaling``         - offshore swell shoals + depth-breaks up a beach.
  * ``bottom_friction``  - shallow-shelf friction dissipation (Hs lower with fric).
  * ``wave_current``     - opposing/following current amplifies/damps Hs.

Two bathymetry paths:
  * ``idealized`` (default) - the geography-free rectangular basin the sandbox
    proved (replicates the physics of the official tomawac fetch_limited / shoal /
    opposing_current / bottom_friction verification cases; clears the citations
    law like the GWE analytic V&V).
  * ``noaa_greatlakes`` - a REAL US lake AOI meshed on a regular grid whose node
    bed is sampled from the NOAA NGDC ``DEM_all`` ImageServer, which serves the
    ``greatlakes_lakedatum`` Great Lakes bathymetry (verified 2026-08-13: -159.3 m
    at a Lake Superior deep point; USGS 3DEP returns NoData over the lake, and
    Copernicus GLO-30 gives the lake SURFACE, not the bottom). The lake-datum
    depth is already NEGATIVE below the datum, matching TOMAWAC's bed-sign
    convention (gotcha 1). The fetch-growth mode over a real lake IS the proof
    norm #9 discriminating pair (upwind vs downwind shore, same storm).

ALL SIX deck gotchas  are baked here (see write_cas / the mesh
writers): negative bed, initial/boundary spectrum type 6, linear wave growth
bootstrap, KENT=5 incident boundary, wide domain for the 1D fetch law, and a
current GRADIENT for the wave-current class (compiled USER_ANACOS).

ASCII only. No product/agent code touched.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import subprocess
import time
from dataclasses import dataclass

import numpy as np

LOG = logging.getLogger("trid3nt.worker.tomawac.build")

# ---------------------------------------------------------------------------
# Config (strict-field manifest; the entrypoint rejects unknown keys)
# ---------------------------------------------------------------------------
@dataclass
class TomawacConfig:
    name: str = "tomawac_wave_field"
    #: question class: fetch_growth | shoaling | bottom_friction | wave_current
    wave_mode: str = "fetch_growth"
    #: bathymetry path: idealized | noaa_greatlakes
    bathy_source: str = "idealized"
    #: real-bathy AOI (min_lon, min_lat, max_lon, max_lat), EPSG:4326
    bbox: tuple = None                  # type: ignore[assignment]
    #: mesh/grid knob: target node spacing in metres. The grid
    #: node count is capped; a coarser spacing is used (self-labeled) if the AOI
    #: would exceed the cap.
    target_resolution_m: float = None   # type: ignore[assignment]
    #: idealized-basin geometry (metres). A WIDE basin keeps the centerline free
    #: of side-wall spreading loss so the 1D fetch law holds (gotcha 5).
    domain_length_m: float = 40000.0
    domain_width_m: float = 30000.0
    #: idealized constant still-water depth (m, positive; bed elevation = -depth).
    depth_m: float = 60.0
    #: shoaling beach depths (m, offshore -> nearshore).
    beach_depth_offshore_m: float = 40.0
    beach_depth_nearshore_m: float = 3.0
    #: wind forcing. speed 0 -> no wind block (swell-only run).
    wind_speed_mps: float = 20.0
    #: meteorological direction the wind blows FROM (compass degrees, 0=N/90=E).
    wind_dir_from_deg: float = 270.0    # from the west -> blows toward +X (east)
    #: boundary swell (shoaling / wave_current): incident Hs + peak freq.
    boundary_hs_m: float = 0.0
    boundary_fp_hz: float = 0.1
    #: wave-current: current magnitude ramped 0 -> uc across the domain (m/s).
    #: NEGATIVE opposes the incident swell (amplifies Hs), POSITIVE follows it.
    current_uc_mps: float = -2.5
    #: dissipation toggles.
    bottom_friction: bool = False
    friction_coef: float = 0.038        # FRICTION FACTOR (Hasselmann/JONSWAP)
    breaking: bool = False
    #: spectral discretization.
    ndir: int = 24
    nfreq: int = 32
    fmin: float = 0.04
    fratio: float = 1.1
    #: time integration.
    duration_hours: float = 6.0
    time_step_s: float = 120.0
    #: initial spectrum seed (tiny; gotcha 2 keys off this on the idealized path).
    init_hs_m: float = 0.02
    workdir: str = dataclasses.field(
        default_factory=lambda: os.path.dirname(os.path.abspath(__file__)))


#: node-count ceiling for a single local-docker TOMAWAC grid (keeps the solve to
#: minutes). The real-bathy grid coarsens target_resolution_m to stay under this.
GRID_NODE_CAP: int = 60000
#: absolute floor on the grid spacing (below this the solve cost degrades).
GRID_H_FLOOR_M: float = 150.0


class TomawacInputError(RuntimeError):
    """A wave-input problem gated before/at the solve (typed error_code)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# 1. Regular-grid triangular mesh (CCW outer ring, rank IPOBO) - from sandbox
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
    tf.add_header("TOMAWAC " + os.path.basename(path),
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
    """All-solid ring (KLOG=2). If liquid_x0, the upwind wall x=0 becomes a
    prescribed-spectrum incident boundary coded KENT=5 (gotcha 4: TOMAWAC imposes
    the boundary spectrum only where LIFBOR==KENT; KINC=1 does NOT inject it)."""
    ring = mesh["ring"]
    X = mesh["X"]
    lines = []
    for k in range(mesh["nptfr"]):
        node0 = int(ring[k])
        node1 = node0 + 1
        rank = k + 1
        solid = not (liquid_x0 and X[node0] <= mesh["dx"] * 0.5)
        code = 2 if solid else 5
        lih = liu = liv = lit = code
        lines.append(
            f"{lih} {liu} {liv}  0.000 0.000 0.000 0.000  {lit}  0.000 0.000 0.000 "
            f"{node1:>11d} {rank:>11d}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# 2. TOMAWAC steering-file author (all 6 gotchas baked) - from sandbox
# ---------------------------------------------------------------------------
def write_cas(path, geo, cli, res, *, title, ndir, nfreq, fmin, fratio,
              dt, nstep, wind_u=0.0, wind_v=0.0, stationary_wind=False,
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
    # gotcha 3: linear wave growth (Cavaleri-Malanotte-Rizzoli) seeds empty
    # downwind bins so WAM4 exponential wind input can bootstrap.
    A(f"LINEAR WAVE GROWTH : {wind_on}")
    A(f"WIND GENERATION : {wind_on}")                # 1 = WAM cycle 4 (Janssen)
    A(f"WHITE CAPPING DISSIPATION : {wind_on}")      # 1 = WAM cycle 4
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
    # gotcha 2: INISPE=6 = parameterised JONSWAP keyed off INITIAL SIGNIFICANT
    # WAVE HEIGHT + PEAK FREQUENCY (works at any wind; INISPE=1 needs a fetch).
    A("TYPE OF INITIAL DIRECTIONAL SPECTRUM : 6")
    A(f"INITIAL SIGNIFICANT WAVE HEIGHT : {init_hs}")
    A("INITIAL PEAK FREQUENCY : 0.3")
    A("INITIAL PEAK FACTOR : 3.3")
    A("INITIAL DIRECTIONAL SPREAD 1 : 20.")
    A("INITIAL MAIN DIRECTION 1 : 0.")
    # --- boundary spectrum (shoaling / wave-current swell) ---
    if bc_spectrum:
        # gotcha 2 (boundary): LIMSPE=6 keyed off BOUNDARY Hs + PEAK FREQUENCY.
        A("TYPE OF BOUNDARY DIRECTIONAL SPECTRUM : 6")
        A(f"BOUNDARY SIGNIFICANT WAVE HEIGHT : {bc_hs}")
        A(f"BOUNDARY PEAK FREQUENCY : {bc_fp}")
        A("BOUNDARY PEAK FACTOR : 3.3")
        A("BOUNDARY DIRECTIONAL SPREAD 1 : 20.")
        A("BOUNDARY MAIN DIRECTION 1 : 0.")
    A("SPHERICAL COORDINATES : NO")
    # trig convention: direction 0 deg = +X axis, aligned with +X wind forcing.
    A("TRIGONOMETRICAL CONVENTION : YES")
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")


# gotcha 6: a spatially UNIFORM current leaves steady-state Hs unchanged (no
# gradient = no action-flux change). USER_ANACOS ramps UC linearly 0 -> UCMAX
# across the domain so the incident swell propagates into a strengthening
# current (UCONST is 0 in the shipped anacos.f).
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
      DO IP=1,NPOIN2
        UC(IP)=UCMAX*X(IP)/LXDOM
        VC(IP)=0.D0
      ENDDO
      RETURN
      END
"""


# ---------------------------------------------------------------------------
# 3. Run + extract fields
# ---------------------------------------------------------------------------
def run_tomawac(cas, workdir, tag, timeout=1800):
    env = dict(os.environ)
    cmd = ["tomawac.py", os.path.basename(cas), "--ncsize=1"]
    p = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True,
                       timeout=timeout)
    out = p.stdout + "\n" + p.stderr
    with open(os.path.join(workdir, f"tomawac_{tag}.log"), "w") as f:
        f.write("CMD: " + " ".join(cmd) + "\n\nSTDOUT:\n" + p.stdout +
                "\n\nSTDERR:\n" + p.stderr)
    ok = "CORRECT END OF RUN" in out or p.returncode == 0
    return ok, out


def _read_field(res_path, name):
    from data_manip.extraction.telemac_file import TelemacFile
    tf = TelemacFile(res_path)
    nt = tf.ntimestep
    try:
        val = tf.get_data_value(name, nt - 1)
    except Exception:  # noqa: BLE001 -- name not present
        val = None
    X = np.array(tf.meshx)
    Y = np.array(tf.meshy)
    tf.close()
    return X, Y, (np.array(val) if val is not None else None)


def read_hm0(res_path):
    """Final-frame Hs (SERAFIN var 'WAVE HEIGHT HM0'). Returns (X, Y, hm0)."""
    return _read_field(res_path, "WAVE HEIGHT HM0")


def centerline_profile(X, Y, hm0, mesh, field=None):
    """Value sampled along the mid-line (j = ny//2) versus X (fetch/cross-shore)."""
    ny = mesh["ny"]
    nx = mesh["nx"]
    jmid = ny // 2
    src = hm0 if field is None else field
    xs, hs = [], []
    for i in range(nx):
        n = i * ny + jmid
        xs.append(float(X[n]))
        hs.append(float(src[n]))
    return np.array(xs), np.array(hs)


def cerc_hs(U, fetch_m):
    """CERC/SPM fetch-limited significant wave height (deep water)."""
    xstar = 9.81 * fetch_m / (U * U)
    return 0.0016 * math.sqrt(xstar) * U * U / 9.81


# ---------------------------------------------------------------------------
# 4. Real Great Lakes bathymetry -- READ from the staged raster.
# ---------------------------------------------------------------------------
def read_greatlakes_bathy(lon, lat, data_dir):
    """Bed elevation (m) at node lon/lat from the bed the run was staged with.

    The lake-datum bathymetry the wave field is solved on arrives as a file in the
    run directory; a node outside the lake (land / NoData) is NaN. Raises
    TomawacInputError when no bed was staged at all, which is a staging fault
    rather than a coverage answer."""
    from _staged_bed import sample_staged_bed

    try:
        return sample_staged_bed(lon, lat, data_dir)
    except FileNotFoundError as exc:
        raise TomawacInputError("TOMAWAC_BATHY_UNAVAILABLE", str(exc)) from exc


def _bbox_utm_epsg(bbox):
    lon = 0.5 * (bbox[0] + bbox[2])
    lat = 0.5 * (bbox[1] + bbox[3])
    zone = int((lon + 180.0) // 6.0) + 1
    return (32600 if lat >= 0 else 32700) + zone


def build_real_lake_grid(cfg: TomawacConfig, data_dir: str):
    """Regular UTM grid over a real lake AOI with NOAA lake-datum bed at nodes.

    Deep-water NaN nodes (land / outside the lake) are lifted to a shallow +2 m
    bed (dry land above the datum) so the solid ring stays closed and the wave
    field is confined to the wet lake interior. Returns (mesh, meta)."""
    from pyproj import Transformer

    bbox = cfg.bbox
    if not (bbox and len(bbox) == 4):
        raise TomawacInputError(
            "TOMAWAC_PARAMS_INVALID",
            "noaa_greatlakes bathy_source needs a 4-value bbox (min_lon,min_lat,"
            f"max_lon,max_lat); got {bbox!r}.")
    epsg = _bbox_utm_epsg(bbox)
    tr = Transformer.from_crs(4326, epsg, always_xy=True)
    x0, y0 = tr.transform(bbox[0], bbox[1])
    x1, y1 = tr.transform(bbox[2], bbox[3])
    Lx = abs(x1 - x0)
    Ly = abs(y1 - y0)
    # grid spacing: honor target_resolution_m, floor + node-cap coarsen (labeled).
    dx_req = float(cfg.target_resolution_m or 0.0)
    dx = max(dx_req, GRID_H_FLOOR_M) if dx_req > 0 else max(Lx, Ly) / 120.0
    dx = max(dx, GRID_H_FLOOR_M)
    coarsened = False
    while (int(Lx / dx) + 1) * (int(Ly / dx) + 1) > GRID_NODE_CAP:
        dx *= 1.15
        coarsened = True

    # build a plain grid, then sample real bathy at node lon/lat.
    mesh = build_grid(Lx, Ly, dx, lambda X, Y: np.zeros_like(X))
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    # node UTM -> absolute UTM (offset by the AOI SW corner) -> lonlat
    xabs = mesh["X"] + min(x0, x1)
    yabs = mesh["Y"] + min(y0, y1)
    lon, lat = back.transform(xabs, yabs)
    bed = read_greatlakes_bathy(np.asarray(lon), np.asarray(lat), data_dir)
    wet = np.isfinite(bed) & (bed < 0.0)
    n_wet = int(wet.sum())
    if n_wet < 0.05 * bed.size:
        raise TomawacInputError(
            "TOMAWAC_BATHY_UNAVAILABLE",
            f"the staged lake bathymetry covered only {n_wet}/{bed.size} grid nodes over "
            f"{bbox} -- the AOI is mostly land/dry. Pick a bbox inside a Great "
            "Lake (Superior/Michigan/Huron/Erie/Ontario) open water.")
    Z = np.where(wet, bed, 2.0)                # NaN/land -> +2 m dry bed
    mesh["Z"] = Z.astype(np.float64)
    # node lon/lat + the RAW sampled lake-datum bed (NaN off the wet lake) for the
    # in-worker bed-COG input surface: show the true bathymetry, not the +2 m fill.
    mesh["bed_lon"] = np.asarray(lon, dtype=float)
    mesh["bed_lat"] = np.asarray(lat, dtype=float)
    mesh["bed_raw"] = np.where(wet, bed, np.nan).astype(float)
    # The bbox is ECHOED because the agent-side reader has to add this exact SW
    # corner back to the local mesh metres. Reconstructing it from the request
    # rather than from what was built is how a rounded corner offsets the field.
    meta = dict(utm_epsg=epsg, dx_m=round(dx, 1), coarsened=coarsened,
                bbox=[float(v) for v in bbox],
                n_wet_nodes=n_wet, depth_max_m=round(float(-np.nanmin(bed)), 1),
                depth_mean_m=round(float(-np.nanmean(bed[wet])), 1),
                aoi_epsg=epsg)
    return mesh, meta


# ---------------------------------------------------------------------------
# 5. Wind vector from a meteorological FROM-direction
# ---------------------------------------------------------------------------
def wind_components(speed, dir_from_deg):
    """(u, v) in local UTM (x=east, y=north) from speed + met FROM-direction.

    Compass FROM d -> blows TO d+180; convert compass-to to trig (CCW from east):
    trig = 90 - (d + 180). u = speed*cos(trig), v = speed*sin(trig)."""
    if speed <= 0:
        return 0.0, 0.0
    d_to = (dir_from_deg + 180.0) % 360.0
    trig = math.radians(90.0 - d_to)
    return round(speed * math.cos(trig), 4), round(speed * math.sin(trig), 4)


# ---------------------------------------------------------------------------
# 6. The four question-class solves
# ---------------------------------------------------------------------------
def _idealized_mesh(cfg: TomawacConfig):
    Lx, Ly = float(cfg.domain_length_m), float(cfg.domain_width_m)
    dx = float(cfg.target_resolution_m or 1500.0)
    dx = max(dx, GRID_H_FLOOR_M)
    if cfg.wave_mode == "shoaling":
        d_off, d_near = cfg.beach_depth_offshore_m, cfg.beach_depth_nearshore_m
        return build_grid(
            Lx, Ly, dx,
            lambda X, Y: -(d_off - (d_off - d_near) * (X / Lx)))
    return build_grid(Lx, Ly, dx, lambda X, Y: np.full_like(X, -float(cfg.depth_m)))


def solve(cfg: TomawacConfig, workdir: str, run_id: str = None):
    """Author + solve one TOMAWAC wave field; return a metrics dict.

    Writes geo/cli/cas + result SELAFIN into ``workdir`` and reads the final
    Hs field (+ TM01 period, DMOY direction) for the run summary + the
    question-class chart. The result SELAFIN is the artifact the agent-side
    postprocess rasterizes to the Hs COG.
    """
    t0 = time.time()
    tag = "wave"
    liquid = cfg.wave_mode in ("shoaling", "wave_current")
    fort = None

    # --- mesh ---
    if str(cfg.bathy_source).lower() in ("noaa_greatlakes", "greatlakes", "noaa"):
        mesh, bmeta = build_real_lake_grid(cfg, workdir)
        # a real lake fetch run is all-solid (fetch develops from the upwind
        # shore); shoaling/current over a real lake would need an open boundary
        # picker, out of this pass -- force the wind-driven all-solid path.
        liquid = False
        cfg.wave_mode = "fetch_growth" if cfg.wave_mode not in (
            "fetch_growth", "bottom_friction") else cfg.wave_mode
    else:
        mesh = _idealized_mesh(cfg)
        bmeta = dict(utm_epsg=32615, dx_m=float(cfg.target_resolution_m or 1500.0),
                     coarsened=False)

    geo = os.path.join(workdir, f"geo_{tag}.slf")
    cli = os.path.join(workdir, f"bc_{tag}.cli")
    res = os.path.join(workdir, f"res_{tag}.slf")
    cas = os.path.join(workdir, f"tom_{tag}.cas")
    write_slf(mesh, geo)
    write_cli(mesh, cli, liquid_x0=liquid)

    # --- wind ---
    wind_on = cfg.wave_mode in ("fetch_growth", "bottom_friction") \
        and cfg.wind_speed_mps > 0.0
    wu, wv = wind_components(float(cfg.wind_speed_mps), float(cfg.wind_dir_from_deg))

    # --- wave-current: compile the ramped USER_ANACOS ---
    stationary_current = False
    if cfg.wave_mode == "wave_current":
        fort = os.path.join(workdir, f"user_{tag}.f")
        with open(fort, "w") as f:
            f.write(USER_ANACOS_TMPL % {"UC": float(cfg.current_uc_mps),
                                        "LX": float(mesh["Lx"])})
        stationary_current = True

    nstep = int(float(cfg.duration_hours) * 3600.0 / float(cfg.time_step_s))
    bc = cfg.wave_mode in ("shoaling", "wave_current")
    write_cas(
        cas, os.path.basename(geo), os.path.basename(cli), os.path.basename(res),
        title=f"TOMAWAC {cfg.wave_mode} {cfg.name}",
        ndir=int(cfg.ndir), nfreq=int(cfg.nfreq), fmin=float(cfg.fmin),
        fratio=float(cfg.fratio), dt=float(cfg.time_step_s), nstep=nstep,
        wind_u=wu, wind_v=wv, stationary_wind=wind_on,
        bc_spectrum=bc, bc_hs=float(cfg.boundary_hs_m or 1.5),
        bc_fp=float(cfg.boundary_fp_hz), init_hs=float(cfg.init_hs_m),
        breaking=bool(cfg.breaking or cfg.wave_mode == "shoaling"),
        bottom_friction=bool(cfg.bottom_friction),
        fbcoef=float(cfg.friction_coef),
        fortran_file=(os.path.basename(fort) if fort else None),
        stationary_current=stationary_current)

    ok, out = run_tomawac(cas, workdir, tag,
                          timeout=int(os.environ.get(
                              "TRID3NT_TOMAWAC_SOLVE_TIMEOUT", "3600")))
    (open(os.path.join(workdir, "full_listing.log"), "w")
     .write(out) if out else None)

    metrics = {
        "status": "ok" if ok else "error",
        "correct_end": bool(ok),
        "run_id": run_id,
        "wave_mode": cfg.wave_mode,
        "bathy_source": cfg.bathy_source,
        "result_slf": os.path.basename(res),
        "geometry_slf": os.path.basename(geo),
        "cli": os.path.basename(cli),
        "cas": os.path.basename(cas),
        "npoin": int(mesh["npoin"]),
        "nelem": int(len(mesh["ikle"])),
        "nx": int(mesh["nx"]), "ny": int(mesh["ny"]),
        "wind_speed_mps": float(cfg.wind_speed_mps) if wind_on else 0.0,
        "wind_dir_from_deg": float(cfg.wind_dir_from_deg) if wind_on else None,
        **bmeta,
        "wall_s": round(time.time() - t0, 1),
    }
    if not ok:
        metrics["error"] = "TOMAWAC did not reach CORRECT END OF RUN"
        metrics["listing_tail"] = "\n".join(out.splitlines()[-40:])
        return metrics

    # --- read Hs (+ period, direction) + build the question-class chart ---
    X, Y, hm0 = read_hm0(res)
    if hm0 is None:
        metrics["status"] = "error"
        metrics["correct_end"] = False
        metrics["error"] = "TOMAWAC result carried no WAVE HEIGHT HM0 field"
        return metrics
    _, _, tm01 = _read_field(res, "MEAN PERIOD TM01")
    _, _, dmoy = _read_field(res, "MEAN DIRECTION")
    finite = np.isfinite(hm0)
    metrics["hs_max_m"] = round(float(np.nanmax(hm0[finite])), 4)
    metrics["hs_mean_m"] = round(float(np.nanmean(hm0[finite])), 4)
    if tm01 is not None and np.isfinite(tm01).any():
        metrics["peak_period_max_s"] = round(float(np.nanmax(tm01[np.isfinite(tm01)])), 3)

    xs, hs = centerline_profile(X, Y, hm0, mesh)
    metrics["chart_x_km"] = (xs / 1000.0).round(3).tolist()
    metrics["chart_hs_m"] = np.round(hs, 4).tolist()
    metrics["hs_upwind_m"] = round(float(hs[1]), 4)
    metrics["hs_downwind_m"] = round(float(hs[-2]), 4)
    if cfg.wave_mode == "fetch_growth" and wind_on and cfg.bathy_source == "idealized":
        U = float(cfg.wind_speed_mps)
        metrics["chart_cerc_hs_m"] = [round(cerc_hs(U, x), 4) for x in xs[1:]]
    if cfg.wave_mode == "shoaling":
        d_off, d_near = cfg.beach_depth_offshore_m, cfg.beach_depth_nearshore_m
        depth_prof = d_off - (d_off - d_near) * (xs / mesh["Lx"])
        metrics["chart_depth_m"] = np.round(depth_prof, 2).tolist()

    LOG.info("tomawac %s solve ok: Hs_max=%.3f m upwind=%.3f downwind=%.3f wall=%.1fs",
             cfg.wave_mode, metrics["hs_max_m"], metrics["hs_upwind_m"],
             metrics["hs_downwind_m"], metrics["wall_s"])
    return metrics
