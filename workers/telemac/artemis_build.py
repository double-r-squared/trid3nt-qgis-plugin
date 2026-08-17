"""ARTEMIS harbour-agitation pipeline: idealized + real-harbour fields.

The productionized promotion of ``docs/proof/templates/artemis_sandbox.py`` (the
canonical composer prototype whose physics is PROVEN through the baked artemis
binary in ``trid3nt-local/telemac:latest``). ARTEMIS is TELEMAC's phase-RESOLVING
elliptic mild-slope (Berkhoff) wave solver: steady-state diffraction / refraction
/ partial reflection inside harbours and around structures - the refinement-grade
phase-resolving complement to TOMAWAC's phase-averaged spectral tier.
Runs INSIDE the worker image (needs the baked ``artemis`` binary + the
opentelemac SELAFIN python API); imports NO agent code.

Three question classes (the board's six ARTEMIS rows collapse to three), each a
mode:
  * ``diffraction``  - a breakwater / structure shelters a berthing area; the
                       diffracted wave in the lee is much smaller than the exposed
                       approach (Sommerfeld/Penny-Price). The proof-norm-#9
                       discriminating pair is the sheltered zone behind the
                       breakwater vs the exposed approach in front of it.
  * ``resonance``    - incoming swell amplifies inside a narrow-mouth harbour at
                       the seiche periods T_n=2Lh/(nc); the response spikes AT a
                       resonant period and is quiet OFF resonance.
  * ``shoal``        - a nearshore reef/shoal refracts + focuses waves (the exact
                       Berkhoff-Booij-Radder 1982 elliptic shoal); a focus peak
                       Kd~2.2 forms down-wave of the shoal.

Two bathymetry paths:
  * ``idealized`` (default) - the geography-free analytic domains the sandbox
    proved (replicates the physics of the classic ARTEMIS validation set; clears
    the citations law like the GWE analytic V&V).
  * ``noaa_greatlakes`` - a REAL US Great Lakes harbour AOI whose node bed is
    sampled from the NOAA NGDC ``DEM_all`` ImageServer (the ``greatlakes_lakedatum``
    bathymetry, verified in for the TOMAWAC leg: -159 m at a Lake
    Superior deep; USGS 3DEP is NoData over the lake and Copernicus gives the
    lake SURFACE, not the bottom). A schematic breakwater segment (labeled) is a
    thin solid barrier; the diffraction-sheltering pair over real bathymetry IS
    the proof-norm-#9 discriminating signature.

ALL EIGHT deck gotchas  are baked here (see write_cli / write_cas
build_mesh): the .cli column re-map (col1 LIHBOR / col4 HB / col5 TETAP tangent /
col7 RP), TETAP is the BOUNDARY TANGENT not the wave direction, the incident
direction lives only in DIRECTION OF WAVE PROPAGATION, the all-incident outer
ring for open-domain diffraction, bathymetry via BOTTOM=-depth + INITIAL WATER
LEVEL, marching-cell meshing for internal thin barriers, the shoreline-singularity
exclusion, and the constricted mouth for resonance.

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

LOG = logging.getLogger("trid3nt.worker.artemis.build")

G = 9.81

#: NOAA NGDC DEM mosaic ImageServer - the DEM_all mosaic includes the
#: greatlakes_lakedatum bathymetry (lake-bottom depth, negative below the Great
#: Lakes low-water datum). exportImage returns a GeoTIFF sampled at nodes. Same
#: proven source the TOMAWAC leg uses.
_NOAA_DEM_ALL_URL = (
    "https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_all/"
    "ImageServer/exportImage"
)
_UA = "trid3nt-local-artemis (agent@trid3nt.dev)"


# ---------------------------------------------------------------------------
# Config (strict-field manifest; the entrypoint rejects unknown keys)
# ---------------------------------------------------------------------------
@dataclass
class ArtemisConfig:
    name: str = "artemis_harbor_agitation"
    #: question class: diffraction | resonance | shoal
    wave_mode: str = "diffraction"
    #: bathymetry path: idealized | noaa_greatlakes
    bathy_source: str = "idealized"
    #: real-bathy AOI (min_lon, min_lat, max_lon, max_lat), EPSG:4326
    bbox: tuple = None                  # type: ignore[assignment]
    #: mesh/grid knob: target node spacing in metres.
    target_resolution_m: float = None   # type: ignore[assignment]
    #: incident wave forcing.
    wave_period_s: float = 8.0
    #: incident direction, DEGREES trig convention (0=+X east, 90=+Y north).
    wave_dir_deg: float = 90.0
    #: incident wave height H0 (m) on the KINC boundary.
    wave_height_m: float = 1.0
    #: structure reflection coefficient RP (1=fully reflecting quay, 0=absorbing).
    reflection_coef: float = 1.0
    #: real-bathy diffraction: schematic breakwater segment (lon0,lat0,lon1,lat1),
    #: EPSG:4326. None -> a labeled demo segment across the AOI (see build).
    breakwater: tuple = None            # type: ignore[assignment]
    #: real-bathy diffraction: the REAL surveyed structure as one or more polylines
    #: [[lon,lat], ...] (e.g. OSM man_made=breakwater/pier ways), EPSG:4326. Takes
    #: precedence over `breakwater` + the demo segment: the ACTUAL geometry is
    #: meshed as a thin solid reflecting barrier over the real bathymetry.
    breakwater_polylines: list = None   # type: ignore[assignment]
    #: proof-norm-#9 REMOVED control: keep the real bathy + the same lee/exposed
    #: split geometry but do NOT mesh the structure as solid (the "breakwater
    #: removed" half of the present-vs-removed pair). Only meaningful with
    #: breakwater_polylines set.
    remove_structure: bool = False
    #: real-bathy: nodes shallower than this (or land / NaN) are masked out of the
    #: wet domain (m, positive depth below the datum).
    min_depth_m: float = 1.0
    # --- idealized-domain geometry knobs (metres) ---
    #: resonance: harbour length / width / narrow mouth / constant depth.
    harbour_length_m: float = 500.0
    harbour_width_m: float = 100.0
    mouth_width_m: float = 25.0
    depth_m: float = 10.0
    #: diffraction: open domain size + breakwater tip position.
    domain_length_m: float = 600.0
    domain_width_m: float = 400.0
    breakwater_y_m: float = 120.0
    breakwater_tip_x_m: float = 300.0
    #: resonance period sweep (begin, end, step) seconds.
    scan_begin_s: float = 30.0
    scan_end_s: float = 244.0
    scan_step_s: float = 2.0
    workdir: str = dataclasses.field(
        default_factory=lambda: os.path.dirname(os.path.abspath(__file__)))


#: node-count ceiling for a single local-docker ARTEMIS grid (an elliptic solve
#: is heavier than TOMAWAC's spectral march, so a tighter cap keeps it to minutes).
GRID_NODE_CAP: int = 45000
#: absolute floor on the grid spacing (below this the solve cost degrades).
GRID_H_FLOOR_M: float = 20.0


class ArtemisInputError(RuntimeError):
    """A wave-input problem gated before/at the solve (typed error_code)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# 1. General structured-grid mesh with optional node mask + robust CCW
#    boundary-ring extraction (marching cells handle the notched breakwater /
#    harbour-mouth domain; gotcha 6). Promoted verbatim from the sandbox.
# ---------------------------------------------------------------------------
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
            # marching cell (gotcha 6): fill any cell with exactly 3 kept corners
            # with its single CCW triangle so slot/mouth edges do not expose stray
            # boundary nodes (a both-diagonal drop otherwise punches a hole).
            if ka and kb and kc and kd:
                tris.append((newid[a], newid[b], newid[c]))
                tris.append((newid[a], newid[c], newid[d]))
            elif ka and kb and kc:
                tris.append((newid[a], newid[b], newid[c]))
            elif ka and kc and kd:
                tris.append((newid[a], newid[c], newid[d]))
            elif ka and kb and kd:
                tris.append((newid[a], newid[b], newid[d]))
            elif kb and kc and kd:
                tris.append((newid[b], newid[c], newid[d]))
    ikle = np.array(tris, dtype=np.int32)
    if ikle.size == 0:
        raise ArtemisInputError("ARTEMIS_MESH_EMPTY", "no triangles in the meshed domain")

    # boundary = directed edge whose reverse is absent (CCW around the domain)
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
        raise ArtemisInputError("ARTEMIS_MESH_NO_BOUNDARY", "no boundary edges")
    start = min(nxt)
    ring = [start]
    cur = nxt[start]
    guard = 0
    while cur != start:
        ring.append(cur)
        cur = nxt.get(cur)
        if cur is None:
            raise ArtemisInputError(
                "ARTEMIS_MESH_MULTIRING",
                "boundary ring did not close (multi-ring domain; the AOI has "
                "interior land islands - pick an open-water harbour AOI)")
        guard += 1
        if guard > len(nxt) + 5:
            raise ArtemisInputError(
                "ARTEMIS_MESH_MULTIRING", "boundary ring did not close")
    ring = np.array(ring, dtype=np.int32)
    nptfr = len(ring)

    ipob = np.zeros(npoin, dtype=np.int32)
    for rank, n in enumerate(ring, start=1):
        ipob[n] = rank

    Z = depth_fn(X, Y).astype(np.float64)
    return dict(X=X, Y=Y, ikle=ikle, ipob=ipob, ring=ring, nptfr=nptfr,
                npoin=npoin, xs=xs, ys=ys, nx=nx, ny=ny, Z=Z,
                Lx=Lx, Ly=Ly, dx=dx, dy=dy, x0=x0, y0=y0)


def write_slf(mesh, path, values=None, varname="BOTTOM          "):
    """Write the mesh as a single-frame SELAFIN. ``values`` defaults to the bed Z
    (BOTTOM); pass a field + varname to re-emit an agitation field for the COG."""
    from data_manip.extraction.telemac_file import TelemacFile
    if os.path.exists(path):
        os.remove(path)
    vals = mesh["Z"] if values is None else np.asarray(values, dtype=np.float64)
    tf = TelemacFile(path, access="w")
    tf.add_header("ARTEMIS " + os.path.basename(path),
                  date=np.array([2026, 8, 13, 0, 0, 0]))
    tf.add_mesh(mesh["X"], mesh["Y"], mesh["ikle"], z=mesh["Z"])
    tf._ipob3 = mesh["ipob"].astype(np.int32)
    tf._ipob2 = tf._ipob3
    tf._nptfr = int(mesh["nptfr"])
    tf._nbor = (mesh["ring"] + 1).astype(np.int32)
    tf._knolg = np.arange(1, mesh["npoin"] + 1, dtype=np.int32)
    tf.add_variable(varname, "M               ")
    tf.add_data_value(varname, 0, vals)
    tf.write()
    tf.close()


# ---------------------------------------------------------------------------
# 2. ARTEMIS boundary-conditions (.cli) author (gotcha 1: the .cli column re-map).
#      col1  LIHBOR  boundary type: 1=KINC incident, 2=KLOG solid, 4=KSORT free exit
#      col4  HB      incident wave height  (nonzero only on KINC boundaries)
#      col5  TETAP   BOUNDARY TANGENT angle, DEGREES (gotcha 2, NOT the wave dir)
#      col6  ALFAP   phase (deg), 0 here
#      col7  RP      reflection coefficient (1=full reflect, 0=absorbing)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 3. ARTEMIS steering-file author.
# ---------------------------------------------------------------------------
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


def run_artemis(cas, workdir, tag, timeout=3600):
    cmd = ["artemis.py", os.path.basename(cas), "--ncsize=1"]
    p = subprocess.run(cmd, cwd=workdir, env=dict(os.environ),
                       capture_output=True, text=True, timeout=timeout)
    with open(os.path.join(workdir, f"artemis_{tag}.log"), "w") as f:
        f.write("CMD: " + " ".join(cmd) + "\n\nSTDOUT:\n" + p.stdout +
                "\n\nSTDERR:\n" + p.stderr)
    out = p.stdout + "\n" + p.stderr
    ok = "CORRECT END OF RUN" in out or p.returncode == 0
    return ok, out


def read_hs(res_path, all_frames=False):
    from data_manip.extraction.telemac_file import TelemacFile
    tf = TelemacFile(res_path)
    nt = tf.ntimestep
    X = np.array(tf.meshx)
    Y = np.array(tf.meshy)
    if all_frames:
        hs = np.array([tf.get_data_value("WAVE HEIGHT", it) for it in range(nt)])
        tf.close()
        return X, Y, hs, nt
    hs = tf.get_data_value("WAVE HEIGHT", nt - 1)
    tf.close()
    return X, Y, np.asarray(hs), nt


def dispersion_k(T, h):
    """Solve omega^2 = g k tanh(k h) for k (Newton)."""
    omega = 2 * np.pi / T
    k = omega * omega / G
    for _ in range(200):
        th = np.tanh(k * h)
        f = G * k * th - omega * omega
        df = G * th + G * k * h * (1 - th * th)
        dk = f / df
        k -= dk
        if abs(dk) < 1e-12:
            break
    return k


def harbour_periods(Lh, h, nmodes=4):
    """Open-mouth / closed-back quarter-wave seiche ladder T_n=4Lh/((2n-1)c)."""
    out = []
    for n in range(1, nmodes + 1):
        kn = (2 * n - 1) * np.pi / (2 * Lh)
        omega = np.sqrt(G * kn * np.tanh(kn * h))
        out.append(round(2 * np.pi / omega, 2))
    return out


def berkhoff_bottom(X, Y):
    """EXACT rotated-ellipse corfon bathymetry (bosse_elliptique art_corfon.f)."""
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


# ---------------------------------------------------------------------------
# 4. Real Great Lakes bathymetry (NOAA NGDC DEM_all -> greatlakes_lakedatum),
#    mirroring the proven TOMAWAC fetch.
# ---------------------------------------------------------------------------
def fetch_greatlakes_bathy(lon, lat, bbox):
    """Sample Great Lakes lake-datum bathymetry at node lon/lat via NOAA DEM_all.

    Returns per-node bed elevation (m, NEGATIVE below the datum); a node outside
    the lake (land / NoData) is NaN. Raises ArtemisInputError if the AOI carries
    no lake bathymetry."""
    import requests
    from rasterio.io import MemoryFile

    ncols = int(np.clip(round((bbox[2] - bbox[0]) * 3000.0), 64, 2500))
    nrows = int(np.clip(round((bbox[3] - bbox[1]) * 3000.0), 64, 2500))
    resp = requests.get(_NOAA_DEM_ALL_URL, params={
        "bbox": ",".join(str(v) for v in bbox),
        "bboxSR": "4326", "imageSR": "4326",
        "size": f"{ncols},{nrows}",
        "format": "tiff", "pixelType": "F32", "f": "image",
    }, headers={"User-Agent": _UA}, timeout=180)
    resp.raise_for_status()
    body = resp.content
    if body[:4] not in (b"II*\x00", b"MM\x00*"):
        raise ArtemisInputError(
            "ARTEMIS_BATHY_UNAVAILABLE",
            f"NOAA DEM_all exportImage returned non-tiff over {bbox}: {body[:160]!r}")
    with MemoryFile(body) as mf, mf.open() as src:
        samp = np.array(list(src.sample(np.column_stack([lon, lat]))),
                        dtype=float).ravel()
        nod = src.nodata
        if nod is not None:
            samp[samp == nod] = np.nan
    samp[~np.isfinite(samp)] = np.nan
    samp[samp < -1.0e4] = np.nan
    return samp


def _bbox_utm_epsg(bbox):
    lon = 0.5 * (bbox[0] + bbox[2])
    lat = 0.5 * (bbox[1] + bbox[3])
    zone = int((lon + 180.0) // 6.0) + 1
    return (32600 if lat >= 0 else 32700) + zone


def _polylines_to_segments(polylines, tr, x0m, y0m):
    """Project REAL lon/lat structure polylines to LOCAL-frame UTM segments.

    ``polylines`` is a list of ``[[lon,lat], ...]`` vertex lists (e.g. OSM
    man_made=breakwater / pier ways). Returns an ``(M, 4)`` array of consecutive
    ``(x0,y0,x1,y1)`` segments in the mesh's local frame (AOI SW-corner origin
    subtracted, matching the node coordinates). A degenerate polyline (< 2 pts)
    contributes nothing; if NOTHING is drawable it is a typed input error."""
    segs = []
    for pl in polylines or ():
        pts = [tr.transform(float(lon), float(lat)) for lon, lat in pl if pl]
        for (ax, ay), (bx, by) in zip(pts[:-1], pts[1:]):
            segs.append((ax - x0m, ay - y0m, bx - x0m, by - y0m))
    if not segs:
        raise ArtemisInputError(
            "ARTEMIS_PARAMS_INVALID",
            "breakwater_polylines carried no drawable segment (need >= 2 vertices "
            "in at least one polyline).")
    return np.asarray(segs, dtype=float)


def _dist_to_segments(px, py, segs):
    """Min distance from each point to a SET of segments (loops the few segments,
    vectorized over the many points). ``segs`` is the ``(M,4)`` local-frame array."""
    px = np.asarray(px, dtype=float)
    py = np.asarray(py, dtype=float)
    best = np.full(px.shape, np.inf)
    for ax, ay, bx, by in segs:
        vx, vy = bx - ax, by - ay
        l2 = (vx * vx + vy * vy) or 1.0
        t = np.clip(((px - ax) * vx + (py - ay) * vy) / l2, 0.0, 1.0)
        cx = ax + t * vx
        cy = ay + t * vy
        best = np.minimum(best, np.hypot(px - cx, py - cy))
    return best


# ---------------------------------------------------------------------------
# 5. The three question-class solves.
#    Each writes: res_agitation.slf (the raw ARTEMIS result, mesh sibling for
#    animation) + agit_field.slf (a single-frame WAVE HEIGHT field the agent-side
#    postprocess rasterizes to the Kd COG) + returns the metrics dict.
# ---------------------------------------------------------------------------
def _emit_primary_field(mesh, hs_field, data_dir):
    """Re-emit the representative Hs field as a single-frame SELAFIN for the COG."""
    path = os.path.join(data_dir, "agit_field.slf")
    write_slf(mesh, path, values=hs_field, varname="WAVE HEIGHT     ")
    return "agit_field.slf"


def _solve_diffraction_idealized(cfg: ArtemisConfig, data_dir: str, run_id):
    Lx, Ly = float(cfg.domain_length_m), float(cfg.domain_width_m)
    dx = float(cfg.target_resolution_m or 8.0)
    dx = max(dx, GRID_H_FLOOR_M * 0.25)   # idealized breakwater needs a fine grid
    h = float(cfg.depth_m)
    y_bw = float(cfg.breakwater_y_m)
    x_tip = float(cfg.breakwater_tip_x_m)
    dy = dx

    def keep_fn(X, Y):
        return ~((np.abs(Y - y_bw) <= dy * 0.5) & (X <= x_tip))

    mesh = build_mesh(Lx, Ly, dx, lambda X, Y: np.full_like(X, -h), dy=dy,
                      keep_fn=keep_fn)
    wdir = float(cfg.wave_dir_deg)
    H0 = float(cfg.wave_height_m)
    rp = float(cfg.reflection_coef)

    def classify(x, y):
        # gotcha 4: entire outer ring = incident (imposes the plane wave AND
        # radiates the scattered field); only the breakwater faces are solid.
        on_bw = (abs(y - y_bw) <= dy * 1.5) and (x <= x_tip + dx * 0.5)
        if on_bw:
            return (2, 0.0, 0.0, 0.0, rp)
        return (1, H0, 0.0, 0.0, 0.0)

    return _run_diffraction(cfg, mesh, data_dir, run_id, classify,
                            H0=H0, T=float(cfg.wave_period_s), h=h, wdir=wdir,
                            x_tip=x_tip, y_bw=y_bw, dx=dx,
                            bathy_label="idealized flat bed (analytic Sommerfeld "
                            "semi-infinite breakwater)", utm_epsg=None, bbox=None)


def _solve_diffraction_real(cfg: ArtemisConfig, data_dir: str, run_id):
    """Real Great Lakes harbour AOI: real lake-datum bathy at grid nodes, a
    schematic breakwater as a thin solid barrier, all-incident outer ring."""
    from pyproj import Transformer

    bbox = cfg.bbox
    if not (bbox and len(bbox) == 4):
        raise ArtemisInputError(
            "ARTEMIS_PARAMS_INVALID",
            "noaa_greatlakes bathy_source needs a 4-value bbox "
            f"(min_lon,min_lat,max_lon,max_lat); got {bbox!r}.")
    epsg = _bbox_utm_epsg(bbox)
    tr = Transformer.from_crs(4326, epsg, always_xy=True)
    x0, y0 = tr.transform(bbox[0], bbox[1])
    x1, y1 = tr.transform(bbox[2], bbox[3])
    Lx, Ly = abs(x1 - x0), abs(y1 - y0)
    dx_req = float(cfg.target_resolution_m or 0.0)
    dx = max(dx_req, GRID_H_FLOOR_M) if dx_req > 0 else max(Lx, Ly) / 120.0
    dx = max(dx, GRID_H_FLOOR_M)
    coarsened = False
    while (int(Lx / dx) + 1) * (int(Ly / dx) + 1) > GRID_NODE_CAP:
        dx *= 1.15
        coarsened = True

    # first pass on a full grid to sample bathy + decide the wet mask
    x0m, y0m = min(x0, x1), min(y0, y1)
    nx = int(round(Lx / dx)) + 1
    ny = int(round(Ly / dx)) + 1
    xs = np.linspace(0.0, Lx, nx)
    ys = np.linspace(0.0, Ly, ny)
    Xg = np.repeat(xs, ny)
    Yg = np.tile(ys, nx)
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    lon, lat = back.transform(Xg + x0m, Yg + y0m)
    bed = fetch_greatlakes_bathy(np.asarray(lon), np.asarray(lat), bbox)
    min_depth = max(float(cfg.min_depth_m), 0.1)
    wet_grid = np.isfinite(bed) & (bed < -min_depth)
    n_wet = int(wet_grid.sum())
    if n_wet < 0.25 * bed.size:
        raise ArtemisInputError(
            "ARTEMIS_BATHY_UNAVAILABLE",
            f"NOAA lake bathymetry covered only {n_wet}/{bed.size} grid nodes "
            f">{min_depth} m deep over {bbox} -- the AOI is mostly land/shallow. "
            "Pick an OPEN-WATER harbour approach AOI (few interior land nodes) "
            "inside a Great Lake.")

    # Structure geometry as LOCAL-frame UTM segments. Three sources, by priority:
    #   1. breakwater_polylines -- the REAL surveyed structure (e.g. OSM man_made=
    #      breakwater / pier ways): mesh the ACTUAL geometry as a thin solid barrier
    #      (many segments; marching cells route the mesh around the 1-cell slit).
    #   2. breakwater -- a single user-supplied (lon0,lat0,lon1,lat1) segment.
    #   3. else -- a labeled schematic demo segment attached to the west AOI edge
    #      (a floating internal barrier isolates stray boundary nodes and aborts
    #      FRONT2, so the demo attaches to the edge; for the demo the incident wave
    #      is forced perpendicular (+Y) so the geometric shadow sits due-north).
    real_struct = bool(cfg.breakwater_polylines)
    demo_bw = (not real_struct
               and not (cfg.breakwater and len(cfg.breakwater) == 4))
    if real_struct:
        segs = _polylines_to_segments(cfg.breakwater_polylines, tr, x0m, y0m)
        wdir = float(cfg.wave_dir_deg)
        bw_label = (f"REAL surveyed breakwater (as mapped in OpenStreetMap "
                    f"man_made=breakwater/pier, {len(segs)} segments) meshed as a "
                    f"thin solid reflecting barrier over real NOAA lake bathymetry")
    elif not demo_bw:
        bx0, by0 = tr.transform(cfg.breakwater[0], cfg.breakwater[1])
        bx1, by1 = tr.transform(cfg.breakwater[2], cfg.breakwater[3])
        segs = np.asarray([(bx0 - x0m, by0 - y0m, bx1 - x0m, by1 - y0m)],
                          dtype=float)
        bw_label = "user-supplied breakwater segment"
        wdir = float(cfg.wave_dir_deg)
    else:
        y_bw = Ly * 0.55
        tip_x = Lx * 0.5
        segs = np.asarray([(0.0, y_bw, tip_x, y_bw)], dtype=float)
        wdir = 90.0                      # +Y, perpendicular to the barrier
        bw_label = ("schematic demo breakwater (labeled): a thin solid semi-"
                    "infinite barrier from the west AOI edge to an interior tip, "
                    "the incident wave normal to it -- not a surveyed structure")

    def _dist_fn(px, py):
        # min distance to ANY structure segment (single-seg + demo are 1-element).
        return _dist_to_segments(px, py, segs)

    # proof-norm-#9 REMOVED control: same bathy + the same split geometry, but the
    # structure is NOT a solid barrier (no masked line, no reflecting faces).
    structure_solid = not (real_struct and bool(cfg.remove_structure))

    # mask: wet AND not on the structure line (thin barrier ~1 cell wide)
    on_bw_grid = (_dist_fn(Xg, Yg) <= dx * 0.6) if structure_solid \
        else np.zeros(Xg.shape, dtype=bool)
    keep_grid = wet_grid & (~on_bw_grid)
    # bed restricted to the kept nodes (build_mesh keeps nodes in np.nonzero(keep)
    # order, so the masked bed lines up with the compacted node coords).
    bed_filled = np.where(np.isfinite(bed), bed, -min_depth)
    bed_kept = bed_filled[keep_grid]

    def keep_fn(X, Y):
        # keep_fn is called on the FULL grid (Xg,Yg order); return the grid mask.
        return keep_grid

    def depth_fn(X, Y):
        # depth_fn is called on the COMPACTED kept nodes -> the kept-bed subset.
        return bed_kept

    mesh = build_mesh(Lx, Ly, dx, depth_fn, dy=dx, keep_fn=keep_fn)
    h_mean = float(-np.nanmean(bed[wet_grid]))
    H0 = float(cfg.wave_height_m)
    rp = float(cfg.reflection_coef)

    # boundary classification (gotcha 4 + the Jukbyeon complex-coastline caveat):
    #   * breakwater faces -> solid reflecting (rp)
    #   * AOI rectangle edge (open water) -> incident (imposes + radiates)
    #   * interior coastline (a masked-land shore) -> solid ABSORBING shore
    #     (rp=0), NOT incident: imposing a plane wave at the shore injects spurious
    #     energy and blows up Kd. Absorbing avoids fake standing waves off a
    #     complex coast (the documented ARTEMIS weakness).
    edge_tol = dx * 0.5

    def classify(x, y):
        if structure_solid and _dist_fn(np.array([x]), np.array([y]))[0] <= dx * 1.6:
            return (2, 0.0, 0.0, 0.0, rp)          # structure face: solid reflecting
        on_edge = (x <= edge_tol or x >= Lx - edge_tol
                   or y <= edge_tol or y >= Ly - edge_tol)
        if on_edge:
            return (1, H0, 0.0, 0.0, 0.0)          # open-water AOI edge: incident
        return (2, 0.0, 0.0, 0.0, 0.0)             # interior coastline: absorbing

    meta = dict(utm_epsg=epsg, dx_m=round(dx, 1), coarsened=coarsened,
                n_wet_nodes=int(mesh["npoin"]), depth_mean_m=round(h_mean, 1),
                depth_max_m=round(float(-np.nanmin(bed[wet_grid])), 1),
                bathy_label="real NOAA Great Lakes lake-datum bathymetry",
                structure_present=bool(structure_solid),
                bw_label=(bw_label if structure_solid
                          else bw_label + " -- REMOVED (proof-norm-#9 control)"))

    # in-worker bed-COG input surface: write the sampled lake-datum bed the solve
    # ran on (the RAW bathymetry at the full-grid nodes, NaN off the wet lake) as a
    # 4326 COG so the composer surfaces it as a role=context input. Best-effort: a
    # bed-COG hiccup NEVER voids a CORRECT END solve.
    try:
        import _bed_cog as _BC  # noqa: WPS433 -- worker payload sibling

        bed_raw = np.where(wet_grid, bed, np.nan)
        bed_cog_meta = _BC.write_bed_cog_lonlat(
            lon, lat, bed_raw, os.path.join(data_dir, _BC.BED_COG_FILENAME))
        bed_cog_meta["bed_cog_source"] = "noaa_greatlakes"
        meta.update(bed_cog_meta)
        LOG.info("artemis bed COG written: %s", bed_cog_meta)
    except Exception as exc:  # noqa: BLE001 -- input surfacing is never fatal
        LOG.warning("artemis bed COG write failed (non-fatal): %s", exc)
    if demo_bw:
        # semi-infinite west-attached barrier + normal incidence: the geometric
        # shadow is the idealized split (downwave of + laterally behind the tip).
        return _run_diffraction(cfg, mesh, data_dir, run_id, classify,
                                H0=H0, T=float(cfg.wave_period_s), h=max(h_mean, 1.0),
                                wdir=wdir, x_tip=tip_x, y_bw=y_bw, dx=dx,
                                bathy_label=meta["bathy_label"], utm_epsg=epsg,
                                bbox=bbox, x0m=x0m, y0m=y0m, back=back,
                                bw_mid=None, wave_uv=None, extra=meta)
    # real surveyed structure OR user segment: the projection split about the
    # structure centroid (all segment endpoints, local frame). Sheltered = downwave
    # of the barrier along the incident direction; exposed = the lit approach.
    wrad = np.radians(wdir)
    ux, uy = np.cos(wrad), np.sin(wrad)
    allx = np.concatenate([segs[:, 0], segs[:, 2]])
    ally = np.concatenate([segs[:, 1], segs[:, 3]])
    bw_mid = (float(allx.mean()), float(ally.mean()))
    meta["n_structure_segments"] = int(len(segs))
    return _run_diffraction(cfg, mesh, data_dir, run_id, classify,
                            H0=H0, T=float(cfg.wave_period_s), h=max(h_mean, 1.0),
                            wdir=wdir, x_tip=None, y_bw=None, dx=dx,
                            bathy_label=meta["bathy_label"], utm_epsg=epsg,
                            bbox=bbox, x0m=x0m, y0m=y0m, back=back,
                            bw_mid=bw_mid, wave_uv=(ux, uy), extra=meta)


def _run_diffraction(cfg, mesh, data_dir, run_id, classify, *, H0, T, h, wdir,
                     x_tip, y_bw, dx, bathy_label, utm_epsg, bbox,
                     x0m=0.0, y0m=0.0, back=None, bw_mid=None, wave_uv=None,
                     extra=None):
    tag = "agit"
    geo = os.path.join(data_dir, "geo_agit.slf")
    cli = os.path.join(data_dir, "bc_agit.cli")
    res = os.path.join(data_dir, "res_agitation.slf")
    cas = os.path.join(data_dir, "art_agit.cas")
    write_slf(mesh, geo)
    write_cli(mesh, cli, classify)
    write_cas(cas, os.path.basename(geo), os.path.basename(cli),
              os.path.basename(res), title=f"ARTEMIS DIFFRACTION {cfg.name}",
              wave_period=T, wave_dir=wdir, swl=0.0)
    ok, out = run_artemis(cas, data_dir, tag,
                          timeout=int(os.environ.get("TRID3NT_ARTEMIS_SOLVE_TIMEOUT", "3600")))
    (open(os.path.join(data_dir, "full_listing.log"), "w").write(out) if out else None)
    if not ok:
        return _fail_metrics(cfg, run_id, out)
    X, Y, hs, nt = read_hs(res)
    hs = np.asarray(hs)
    kd = hs / max(H0, 1e-9)

    # incident reference: mean Hs where Kd ~ 1 (the lit / exposed field far from lee)
    lam = 2 * np.pi / dispersion_k(T, h)
    # sheltered vs exposed split (proof norm #9)
    if bw_mid is not None and wave_uv is not None:
        # real path: project nodes onto the incident direction relative to the
        # barrier midpoint. Behind (downwave, +u) = sheltered; in front = exposed.
        ux, uy = wave_uv
        proj = (X - bw_mid[0]) * ux + (Y - bw_mid[1]) * uy
        # lateral distance to the barrier line to focus the split near the lee
        lat_ok = np.ones_like(proj, dtype=bool)
        sheltered = proj > 0.5 * lam
        exposed = proj < -0.5 * lam
    else:
        # idealized path: shadow is x<x_tip on the downwave (y>y_bw) side.
        sheltered = (Y > y_bw + 0.5 * lam) & (X < x_tip - 0.5 * lam)
        exposed = (Y < y_bw - 0.5 * lam)
    kd_sheltered = float(np.nanmean(kd[sheltered])) if sheltered.any() else None
    kd_exposed = float(np.nanmean(kd[exposed])) if exposed.any() else None
    kd_max = float(np.nanmax(kd))
    hs_max = float(np.nanmax(hs))

    field_slf = _emit_primary_field(mesh, hs, data_dir)

    # chart: Kd transect across the incident direction through the lee
    chart = _diffraction_transect(X, Y, kd, bw_mid, wave_uv, x_tip, y_bw, lam)

    metrics = _base_metrics(cfg, run_id, mesh, utm_epsg, bbox, field_slf, res)
    metrics.update({
        "wave_mode": "diffraction",
        "wave_period_s": round(T, 2), "wave_dir_deg": round(wdir, 1),
        "wave_height_m": round(H0, 3), "wavelength_m": round(float(lam), 1),
        "kd_max": round(kd_max, 3), "hs_max_m": round(hs_max, 3),
        "kd_sheltered": round(kd_sheltered, 3) if kd_sheltered is not None else None,
        "kd_exposed": round(kd_exposed, 3) if kd_exposed is not None else None,
        "sheltering_ratio": (round(kd_sheltered / kd_exposed, 3)
                             if (kd_sheltered and kd_exposed) else None),
        "bathy_label": bathy_label,
        **chart,
        **(extra or {}),
    })
    LOG.info("artemis diffraction ok: kd_max=%.2f sheltered=%s exposed=%s wall=%s",
             kd_max, metrics.get("kd_sheltered"), metrics.get("kd_exposed"),
             metrics.get("wall_s"))
    return metrics


def _diffraction_transect(X, Y, kd, bw_mid, wave_uv, x_tip, y_bw, lam):
    """A 1-D Kd profile along the incident direction through the shelter zone."""
    if bw_mid is not None and wave_uv is not None:
        ux, uy = wave_uv
        s = (X - bw_mid[0]) * ux + (Y - bw_mid[1]) * uy       # along-wave coord
        band = np.abs((X - bw_mid[0]) * (-uy) + (Y - bw_mid[1]) * ux) <= max(lam, 1.0)
    else:
        s = Y - y_bw
        band = np.abs(X - (x_tip - lam)) <= lam
    if band.sum() < 3:
        band = np.ones_like(s, dtype=bool)
    order = np.argsort(s[band])
    sv = s[band][order]
    kv = kd[band][order]
    # decimate to ~60 points for the chart
    if sv.size > 60:
        idx = np.linspace(0, sv.size - 1, 60).astype(int)
        sv, kv = sv[idx], kv[idx]
    return {"chart_s_m": np.round(sv, 1).tolist(),
            "chart_kd": np.round(kv, 3).tolist(),
            "chart_kind": "diffraction_transect"}


def _solve_resonance(cfg: ArtemisConfig, data_dir: str, run_id):
    """Narrow-mouth harbour resonance sweep (idealized; gotcha 8 constricted mouth)."""
    Wx = float(cfg.harbour_width_m)
    Lh = float(cfg.harbour_length_m)
    h = float(cfg.depth_m)
    H0 = float(cfg.wave_height_m)
    mouth = float(cfg.mouth_width_m)
    y_sea = 150.0
    dx = float(cfg.target_resolution_m or 12.5)
    dx = max(dx, 8.0)
    dy = dx
    Ly = y_sea + Lh
    y_wall = y_sea

    def keep_fn(X, Y):
        in_wall_row = np.abs(Y - y_wall) <= dy * 0.5
        in_mouth = np.abs(X - Wx * 0.5) <= mouth * 0.5
        return ~(in_wall_row & (~in_mouth))

    mesh = build_mesh(Wx, Ly, dx, lambda X, Y: np.full_like(X, -h), dy=dy,
                      keep_fn=keep_fn)
    rp = float(cfg.reflection_coef)

    def classify(x, y):
        vertical = (x <= dx * 0.5) or (x >= Wx - dx * 0.5)
        tang = 90.0 if vertical else 0.0
        if y <= dy * 0.5:
            return (1, H0, 0.0, 0.0, 0.0)          # incident sea end
        on_wall = (abs(y - y_wall) <= dy * 1.5) and (abs(x - Wx * 0.5) > mouth * 0.5)
        if on_wall:
            return (2, 0.0, 0.0, 0.0, 1.0)         # dividing wall (horizontal)
        if y < y_wall:
            return (4, 0.0, tang, 0.0, 0.0)        # sea-side walls radiate
        return (2, 0.0, tang, 0.0, rp)             # harbour walls reflect

    geo = os.path.join(data_dir, "geo_agit.slf")
    cli = os.path.join(data_dir, "bc_agit.cli")
    res = os.path.join(data_dir, "res_agitation.slf")
    cas = os.path.join(data_dir, "art_agit.cas")
    scan = (float(cfg.scan_begin_s), float(cfg.scan_end_s), float(cfg.scan_step_s))
    write_slf(mesh, geo)
    write_cli(mesh, cli, classify)
    write_cas(cas, os.path.basename(geo), os.path.basename(cli),
              os.path.basename(res), title=f"ARTEMIS RESONANCE {cfg.name}",
              wave_period=scan[0], wave_dir=90.0, swl=0.0, period_scan=scan,
              phase_ref=(Wx * 0.5, y_wall + Lh * 0.5))
    ok, out = run_artemis(cas, data_dir, "agit",
                          timeout=int(os.environ.get("TRID3NT_ARTEMIS_SOLVE_TIMEOUT", "3600")))
    (open(os.path.join(data_dir, "full_listing.log"), "w").write(out) if out else None)
    if not ok:
        return _fail_metrics(cfg, run_id, out)
    X, Y, hs, nt = read_hs(res, all_frames=True)
    periods = [scan[0] + i * scan[2] for i in range(nt)]
    inh = (Y > y_wall + 2 * dy)
    backwall = (Y > y_wall + Lh - 3 * dy)
    resp = np.array([float(np.mean(hs[i][inh]) / H0) for i in range(nt)])
    back = np.array([float(np.max(hs[i][backwall]) / H0) for i in range(nt)])
    i_res = int(np.argmax(resp))
    off = np.array(resp)
    off[max(0, i_res - 3):i_res + 4] = 1e9
    i_off = int(np.argmin(off))

    field_slf = _emit_primary_field(mesh, hs[i_res], data_dir)
    metrics = _base_metrics(cfg, run_id, mesh, None, None, field_slf, res)
    metrics.update({
        "wave_mode": "resonance",
        "wave_height_m": round(H0, 3),
        "harbour_length_m": Lh, "harbour_depth_m": h, "mouth_width_m": mouth,
        "resonant_period_s": round(periods[i_res], 1),
        "response_at_resonance": round(float(resp[i_res]), 3),
        "response_off_resonance": round(float(resp[i_off]), 3),
        "off_resonance_period_s": round(periods[i_off], 1),
        "backwall_amplification": round(float(back[i_res]), 3),
        "kd_max": round(float(resp.max()), 3),
        "hs_max_m": round(float(np.nanmax(hs[i_res])), 3),
        "seiche_ladder_s": harbour_periods(Lh, h),
        "chart_period_s": [round(p, 1) for p in periods],
        "chart_response": np.round(resp, 3).tolist(),
        "chart_kind": "resonance_sweep",
        "bathy_label": "idealized constant-depth narrow-mouth harbour basin "
                       "(analytic seiche ladder)",
    })
    LOG.info("artemis resonance ok: T_res=%.1fs resp=%.2f (off=%.2f) wall=%s",
             metrics["resonant_period_s"], metrics["response_at_resonance"],
             metrics["response_off_resonance"], metrics.get("wall_s"))
    return metrics


def _solve_shoal(cfg: ArtemisConfig, data_dir: str, run_id):
    """Berkhoff-Booij-Radder (1982) elliptic shoal focusing (idealized; gotcha 7)."""
    dx = float(cfg.target_resolution_m or 0.15)
    dx = max(min(dx, 0.25), 0.1)
    H0 = float(cfg.wave_height_m if cfg.wave_height_m and cfg.wave_height_m < 0.2
               else 0.0464)
    T = float(cfg.wave_period_s if 0.5 <= cfg.wave_period_s <= 2.0 else 1.0)
    Lx, Ly = 30.0, 35.0
    mesh = build_mesh(Lx, Ly, dx, berkhoff_bottom, dy=dx)
    wdir = -90.0

    def classify(x, y):
        if y >= Ly - dx * 0.5:
            return (1, H0, 0.0, 0.0, 0.0)          # incident (top)
        if y <= dx * 0.5:
            return (4, 0.0, 0.0, 0.0, 0.0)         # down-wave free exit
        return (2, 0.0, 90.0, 0.0, 0.0)            # lateral absorbing

    geo = os.path.join(data_dir, "geo_agit.slf")
    cli = os.path.join(data_dir, "bc_agit.cli")
    res = os.path.join(data_dir, "res_agitation.slf")
    cas = os.path.join(data_dir, "art_agit.cas")
    write_slf(mesh, geo)
    write_cli(mesh, cli, classify)
    write_cas(cas, os.path.basename(geo), os.path.basename(cli),
              os.path.basename(res), title=f"ARTEMIS SHOAL {cfg.name}",
              wave_period=T, wave_dir=wdir, swl=0.0, rapid_topo=3)
    ok, out = run_artemis(cas, data_dir, "agit",
                          timeout=int(os.environ.get("TRID3NT_ARTEMIS_SOLVE_TIMEOUT", "3600")))
    (open(os.path.join(data_dir, "full_listing.log"), "w").write(out) if out else None)
    if not ok:
        return _fail_metrics(cfg, run_id, out)
    X, Y, hs, nt = read_hs(res)
    hs = np.asarray(hs)
    kd = hs / H0
    depth = -mesh["Z"]
    good = (depth > 0.12) & (X > 1) & (X < 29) & (Y > 2) & (Y < 34)
    kg = kd[good]
    # wave-axis transect (x ~ shoal center, along -Y) for the chart
    axis = np.abs(X - 15.75) <= 0.75
    order = np.argsort(-Y[axis])
    yv = Y[axis][order]
    kv = kd[axis][order]
    if yv.size > 60:
        idx = np.linspace(0, yv.size - 1, 60).astype(int)
        yv, kv = yv[idx], kv[idx]

    field_slf = _emit_primary_field(mesh, hs, data_dir)
    metrics = _base_metrics(cfg, run_id, mesh, None, None, field_slf, res)
    metrics.update({
        "wave_mode": "shoal", "wave_period_s": round(T, 2),
        "wave_height_m": round(H0, 4),
        "kd_focus_peak": round(float(np.percentile(kg, 99)), 3),
        "kd_focus_max": round(float(np.max(kg)), 3),
        "kd_max": round(float(np.max(kg)), 3),
        "hs_max_m": round(float(np.max(hs[good])), 4),
        "chart_axis_y_m": np.round(yv, 2).tolist(),
        "chart_kd": np.round(kv, 3).tolist(),
        "chart_kind": "shoal_axis_transect",
        "bathy_label": "EXACT Berkhoff-Booij-Radder (1982) elliptic-shoal "
                       "bathymetry (analytic refraction-focusing V&V)",
    })
    LOG.info("artemis shoal ok: kd_focus_peak=%.2f wall=%s",
             metrics["kd_focus_peak"], metrics.get("wall_s"))
    return metrics


def _base_metrics(cfg, run_id, mesh, utm_epsg, bbox, field_slf, res_slf):
    return {
        "status": "ok", "correct_end": True, "run_id": run_id,
        "result_slf": os.path.basename(res_slf),
        "agitation_field_slf": field_slf,
        "npoin": int(mesh["npoin"]), "nelem": int(len(mesh["ikle"])),
        "utm_epsg": utm_epsg, "bathy_source": cfg.bathy_source,
        "bbox": list(bbox) if bbox else None,
        "reflection_coef": float(cfg.reflection_coef),
    }


def _fail_metrics(cfg, run_id, out):
    return {
        "status": "error", "correct_end": False, "run_id": run_id,
        "wave_mode": cfg.wave_mode, "bathy_source": cfg.bathy_source,
        "error": "ARTEMIS did not reach CORRECT END OF RUN",
        "listing_tail": "\n".join((out or "").splitlines()[-40:]),
    }


def solve(cfg: ArtemisConfig, workdir: str, run_id: str = None) -> dict[str, Any]:
    """Author + solve ONE ARTEMIS agitation field; return a metrics dict.

    Dispatches by (wave_mode, bathy_source). Writes res_agitation.slf (the raw
    ARTEMIS result mesh sibling) + agit_field.slf (the single-frame WAVE HEIGHT
    field the agent-side postprocess rasterizes to the Kd COG) into ``workdir``.
    """
    t0 = time.time()
    mode = str(cfg.wave_mode or "diffraction").lower()
    real = str(cfg.bathy_source or "idealized").lower() in (
        "noaa_greatlakes", "greatlakes", "noaa")
    if mode == "resonance":
        metrics = _solve_resonance(cfg, workdir, run_id)
    elif mode == "shoal":
        metrics = _solve_shoal(cfg, workdir, run_id)
    else:  # diffraction (default)
        metrics = (_solve_diffraction_real(cfg, workdir, run_id) if real
                   else _solve_diffraction_idealized(cfg, workdir, run_id))
    metrics["wall_s"] = round(time.time() - t0, 1)
    return metrics
