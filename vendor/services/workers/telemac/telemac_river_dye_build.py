"""TELEMAC-2D river-dye pipeline (PHASE 1): REAL river reach -> dye transport.

Standalone, parameterized builder that takes a real river reach (fetched from
USGS NLDI NHDPlus flowlines) + a Copernicus GLO-30 DEM bed, meshes the
channel-following polygon with Gmsh (tagged inflow/outflow/wall physical
groups), authors a TELEMAC-2D TRACER (.cas) steering file, solves locally, and
renders the dye advecting down the REAL river curves.

This is the artifact the P2 worker image will call. Factored into:
  fetch_river_centerline() -> real NHDPlus geometry
  process_centerline()     -> project/resample/smooth to UTM meters
  fetch_dem_bed()          -> Copernicus GLO-30 DEM sample
  build_channel_mesh()     -> Gmsh mesh (all P0 gotchas honored)
  assign_bed()             -> DEM onto nodes + gentle downstream slope
  write_slf() / write_cli()-> SELAFIN geometry + boundary conditions
  author_deck()            -> .cas with liquid-boundary mapping from the listing
  run_solver()             -> telemac2d.py (delete-empty-result gotcha)
  map_liquid_boundaries()  -> parse solver listing to map inflow/outflow BCs

HARD-WON P0 GOTCHAS honored (see build_gmsh_channel.py):
  (1) SELAFIN connectivity is 0-BASED
  (2) node array from triangle-referenced tags only (drop gmsh orphans)
  (3) IPOBO is rank-based (ring-walk order 1..nptfr)
  (4) liquid boundaries numbered by boundary-WALK order -> read the listing
  (5) tracer scheme = method-of-characteristics (scheme 1)
  (6) delete any empty pre-existing result .slf before running
  (7) meander bend radius > ~0.75*channel width (enforced via smoothing)

ASCII only. No product/agent code touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class ReachConfig:
    name: str = "snake_river_twin_falls"
    seed_lon: float = -114.307          # a point on the reach (NLDI snaps to COMID)
    seed_lat: float = 42.579
    nav_direction: str = "DM"           # downstream main
    distance_km: float = 6.0
    channel_width_m: float = 60.0       # Snake River near Twin Falls is broad
    mesh_size_m: float = 14.0
    resample_ds_m: float = 18.0         # centerline resample spacing
    smooth_window: int = 7              # centerline smoothing (odd)
    # hydraulics
    inflow_q_m3s: float = 250.0         # steady upstream discharge
    init_depth_m: float = 2.5           # initial constant water depth
    dye_conc_mgl: float = 100.0         # injected dye at the upstream inflow
    duration_s: float = 3600.0
    time_step_s: float = 1.0
    graphic_period: int = 200
    min_bed_slope: float = 3.0e-4       # enforced gentle downstream slope floor
    max_bed_slope: float = 6.0e-3
    workdir: str = field(default_factory=lambda: os.path.dirname(os.path.abspath(__file__)))


_NLDI = "https://api.water.usgs.gov/nldi/linked-data"
_UA = "trid3nt-local-spike (agent@trid3nt.dev)"


# ---------------------------------------------------------------------------
# 1. REAL river geometry via USGS NLDI NHDPlus
# ---------------------------------------------------------------------------
def _http_get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _snap_comid(lon: float, lat: float) -> int:
    url = f"{_NLDI}/comid/position?coords=POINT({lon}%20{lat})"
    fc = json.loads(_http_get(url))
    p = fc["features"][0]["properties"]
    return int(p.get("comid") or p.get("nhdplus_comid"))


def _stitch_flowlines(features) -> list[tuple[float, float]]:
    """Order flowline segments head-to-tail into one upstream->downstream path."""
    import shapely.geometry as sg

    segs = [list(sg.shape(f["geometry"]).coords) for f in features]
    segs = [[(round(x, 7), round(y, 7)) for x, y in s] for s in segs]
    # index endpoints
    starts = {i: s[0] for i, s in enumerate(segs)}
    ends = {i: s[-1] for i, s in enumerate(segs)}
    end_set = set(ends.values())
    # head = a segment whose start is nobody's end
    heads = [i for i in starts if starts[i] not in end_set]
    start_i = heads[0] if heads else 0
    # chain by matching end->start
    used = set()
    chain = [start_i]
    used.add(start_i)
    cur = start_i
    start_lookup = defaultdict(list)
    for i, s in starts.items():
        start_lookup[s].append(i)
    while True:
        nxts = [j for j in start_lookup.get(ends[cur], []) if j not in used]
        if not nxts:
            break
        cur = nxts[0]
        used.add(cur)
        chain.append(cur)
    # concatenate, dropping the duplicate shared vertex between segments
    path: list[tuple[float, float]] = []
    for k, i in enumerate(chain):
        s = segs[i]
        if k > 0 and path and s and path[-1] == s[0]:
            s = s[1:]
        path.extend(s)
    return path


def fetch_river_centerline(cfg: ReachConfig):
    """Return (lonlat centerline array, meta dict) from real NHDPlus flowlines."""
    comid = _snap_comid(cfg.seed_lon, cfg.seed_lat)
    url = f"{_NLDI}/comid/{comid}/navigation/{cfg.nav_direction}/flowlines?distance={cfg.distance_km}"
    fc = json.loads(_http_get(url))
    feats = fc["features"]
    path = _stitch_flowlines(feats)
    ll = np.array(path, dtype=float)
    meta = dict(seed_comid=comid, n_flowlines=len(feats), n_raw_vertices=len(ll))
    return ll, meta


# ---------------------------------------------------------------------------
# 2. Project / resample / smooth the real centerline
# ---------------------------------------------------------------------------
def _utm_epsg(lon: float, lat: float) -> int:
    zone = int((lon + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def process_centerline(ll: np.ndarray, cfg: ReachConfig):
    """lon/lat -> local UTM meters, resample uniform, light smoothing.

    Real centerlines are noisy (dense irregular vertices, small kinks). We
    (a) project to UTM, (b) resample to uniform arc-length spacing, (c) smooth
    with a moving average so offset banks do not self-intersect at bends.
    Flow direction = path order (NHDPlus flowlines are digitized downstream).
    """
    from pyproj import Transformer

    epsg = _utm_epsg(float(ll[:, 0].mean()), float(ll[:, 1].mean()))
    tr = Transformer.from_crs(4326, epsg, always_xy=True)
    xm, ym = tr.transform(ll[:, 0], ll[:, 1])
    xy = np.column_stack([xm, ym])

    # arc length
    d = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
    s = np.concatenate([[0.0], np.cumsum(d)])
    total = float(s[-1])

    # uniform resample
    ns = max(int(total / cfg.resample_ds_m) + 1, 10)
    su = np.linspace(0, total, ns)
    xu = np.interp(su, s, xy[:, 0])
    yu = np.interp(su, s, xy[:, 1])

    # moving-average smooth (keep endpoints)
    w = cfg.smooth_window
    if w >= 3 and ns > w:
        k = np.ones(w) / w
        xs = np.convolve(xu, k, mode="same")
        ys = np.convolve(yu, k, mode="same")
        # preserve endpoints (convolve edge bias)
        m = w // 2
        xs[:m] = xu[:m]; xs[-m:] = xu[-m:]
        ys[:m] = yu[:m]; ys[-m:] = yu[-m:]
        xu, yu = xs, ys

    cl = np.column_stack([xu, yu])
    meta = dict(utm_epsg=epsg, centerline_length_m=round(total, 1),
                n_centerline_pts=ns, lonlat_transformer=tr)
    return cl, meta


# ---------------------------------------------------------------------------
# 3. Channel banks + Gmsh mesh (adapts P0 build_gmsh_channel, honoring gotchas)
# ---------------------------------------------------------------------------
def _offset_banks(cl: np.ndarray, width: float):
    x, y = cl[:, 0], cl[:, 1]
    dx = np.gradient(x); dy = np.gradient(y)
    seg = np.hypot(dx, dy); seg[seg == 0] = 1e-9
    nx = -dy / seg; ny = dx / seg
    left = np.column_stack([x + nx * width / 2, y + ny * width / 2])
    right = np.column_stack([x - nx * width / 2, y - ny * width / 2])
    return left, right


def _banks_valid(left: np.ndarray, right: np.ndarray) -> bool:
    """Reject if either bank self-intersects (tight bend folded the inner bank)."""
    import shapely.geometry as sg

    return sg.LineString(left).is_simple and sg.LineString(right).is_simple


def build_channel_mesh(cl: np.ndarray, cfg: ReachConfig):
    """Gmsh mesh of the real channel-following polygon; tagged boundary groups.

    Returns a mesh dict with 0-based ikle, rank-based IPOBO ring, and the
    inflow/outflow node sets (P0 gotchas 1-3, 7).
    """
    import gmsh

    left, right = _offset_banks(cl, cfg.channel_width_m)
    # gotcha 7: if banks self-intersect at a bend, smooth harder until simple
    tries = 0
    while not _banks_valid(left, right) and tries < 6:
        k = np.ones(5) / 5
        cl = np.column_stack([np.convolve(cl[:, 0], k, mode="same"),
                              np.convolve(cl[:, 1], k, mode="same")])
        cl[0] = cl[0]; cl[-1] = cl[-1]
        left, right = _offset_banks(cl, cfg.channel_width_m)
        tries += 1
    banks_ok = _banks_valid(left, right)

    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.model.add(cfg.name)
    ms = cfg.mesh_size_m

    def add_pts(pts):
        return [gmsh.model.geo.addPoint(float(px), float(py), 0.0, ms)
                for px, py in pts]

    lpts = add_pts(left)     # left bank upstream->downstream
    rpts = add_pts(right)
    left_wall = gmsh.model.geo.addSpline(lpts)
    right_wall = gmsh.model.geo.addSpline(rpts)
    inflow = gmsh.model.geo.addLine(rpts[0], lpts[0])     # upstream cap
    outflow = gmsh.model.geo.addLine(lpts[-1], rpts[-1])  # downstream cap
    loop = gmsh.model.geo.addCurveLoop([left_wall, outflow, -right_wall, inflow])
    surf = gmsh.model.geo.addPlaneSurface([loop])
    gmsh.model.geo.synchronize()

    g_in = gmsh.model.addPhysicalGroup(1, [inflow])
    g_out = gmsh.model.addPhysicalGroup(1, [outflow])
    gmsh.model.addPhysicalGroup(1, [left_wall, right_wall])
    gmsh.model.addPhysicalGroup(2, [surf])

    gmsh.model.mesh.generate(2)
    gmsh.model.mesh.removeDuplicateNodes()

    all_tags, all_coords, _ = gmsh.model.mesh.getNodes()
    all_coords = all_coords.reshape(-1, 3)
    coord_of = {int(t): all_coords[i] for i, t in enumerate(all_tags)}

    # gotcha 2: node set from triangle-referenced tags ONLY
    etypes, _, enodes = gmsh.model.mesh.getElements(2)
    tri_tags = None
    for et, en in zip(etypes, enodes):
        if et == 2:
            tri_tags = en.reshape(-1, 3).astype(np.int64)
    used = np.unique(tri_tags)
    t2i = {int(t): i for i, t in enumerate(used)}
    npoin = len(used)
    X = np.array([coord_of[int(t)][0] for t in used])
    Y = np.array([coord_of[int(t)][1] for t in used])
    ikle = np.array([[t2i[int(a)] for a in row] for row in tri_tags], dtype=np.int64)

    def pg_nodes(tag):
        nt, _ = gmsh.model.mesh.getNodesForPhysicalGroup(1, tag)
        return set(t2i[int(t)] for t in nt if int(t) in t2i)

    in_nodes = pg_nodes(g_in)
    out_nodes = pg_nodes(g_out)
    gmsh.finalize()

    # coincident-node guard
    from scipy.spatial import cKDTree
    dd, _ = cKDTree(np.column_stack([X, Y])).query(np.column_stack([X, Y]), k=2)
    mind = float(dd[:, 1].min())
    assert mind > 1e-3, f"coincident nodes (min {mind:.2e} m)"

    # CCW orientation
    a, b, c = ikle[:, 0], ikle[:, 1], ikle[:, 2]
    area2 = (X[b] - X[a]) * (Y[c] - Y[a]) - (X[c] - X[a]) * (Y[b] - Y[a])
    ikle[area2 < 0] = ikle[area2 < 0][:, ::-1]

    # boundary ring (edges in exactly one triangle) -> single CCW cycle
    ec = defaultdict(int); ed = {}
    for t in ikle:
        for k in range(3):
            u, v = int(t[k]), int(t[(k + 1) % 3])
            key = (min(u, v), max(u, v))
            ec[key] += 1; ed[key] = (u, v)
    bnd = [ed[k] for k, n in ec.items() if n == 1]
    nxt = {u: v for u, v in bnd}
    assert len(nxt) == len(bnd), "non-manifold boundary"
    start = bnd[0][0]; ring = [start]; cur = nxt[start]
    while cur != start:
        ring.append(cur); cur = nxt[cur]
    assert len(ring) == len(bnd), "boundary not a single closed ring"
    ring = np.array(ring, dtype=np.int64)

    # gotcha 3: rank-based IPOBO
    nptfr = len(ring)
    ipob = np.zeros(npoin, dtype=np.int32)
    for rank, node in enumerate(ring, start=1):
        ipob[node] = rank

    # classify ring nodes -> BC codes
    lihbor = np.full(nptfr, 2); liubor = np.full(nptfr, 2)
    livbor = np.full(nptfr, 2); litbor = np.full(nptfr, 2)
    cls = np.array(["wall"] * nptfr, dtype=object)
    for i, node in enumerate(ring):
        n = int(node)
        if n in in_nodes:
            lihbor[i], liubor[i], livbor[i], litbor[i] = 4, 5, 5, 5
            cls[i] = "inflow"
        elif n in out_nodes:
            lihbor[i], liubor[i], livbor[i], litbor[i] = 5, 4, 4, 4
            cls[i] = "outflow"

    return dict(X=X, Y=Y, ikle=ikle, npoin=npoin, ring=ring, ipob=ipob,
                nptfr=nptfr, lihbor=lihbor, liubor=liubor, livbor=livbor,
                litbor=litbor, cls=cls, in_nodes=in_nodes, out_nodes=out_nodes,
                n_in=int((cls == "inflow").sum()),
                n_out=int((cls == "outflow").sum()),
                banks_ok=banks_ok, smooth_tries=tries, centerline=cl)


# ---------------------------------------------------------------------------
# 4. DEM bed onto mesh nodes + enforced gentle downstream slope
# ---------------------------------------------------------------------------
def fetch_dem_bed(mesh: dict, cfg: ReachConfig, tr):
    """Sample Copernicus GLO-30 DEM at mesh nodes; fit a gentle downstream bed.

    Real canyon DEM is the SURFACE (canyon rim + water), noisy along the thalweg.
    We (a) sample raw DEM at each node (lon/lat), (b) compute along-channel
    distance s per node, (c) fit bed = z0 - slope*s using a robust downstream
    trend clamped to [min_bed_slope, max_bed_slope] so flow always moves.
    Both the measured DEM drop and the enforced slope are reported.
    """
    import planetary_computer as pc
    import pystac_client
    import rasterio

    X, Y = mesh["X"], mesh["Y"]
    # node lon/lat (inverse transform)
    inv = tr  # Transformer 4326->utm; build inverse
    from pyproj import Transformer
    back = Transformer.from_crs(inv.target_crs, 4326, always_xy=True)
    lon, lat = back.transform(X, Y)
    pad = 0.01
    bbox = [float(lon.min() - pad), float(lat.min() - pad),
            float(lon.max() + pad), float(lat.max() + pad)]

    cat = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1")
    items = list(cat.search(collections=["cop-dem-glo-30"], bbox=bbox).items())
    if not items:
        raise RuntimeError(f"no Copernicus GLO-30 tiles for bbox {bbox}")
    z_raw = np.full(len(X), np.nan)
    with rasterio.Env(GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_TIMEOUT="30"):
        for it in items:
            href = pc.sign(it).assets["data"].href
            with rasterio.open("/vsicurl/" + href) as src:
                samp = np.array(list(src.sample(np.column_stack([lon, lat]))),
                                dtype=float).ravel()
                nod = src.nodata
                if nod is not None:
                    samp[samp == nod] = np.nan
                take = np.isnan(z_raw) & ~np.isnan(samp)
                z_raw[take] = samp[take]

    # along-channel distance s: project each node onto the centerline polyline
    cl = mesh["centerline"]
    s_node = _project_s(X, Y, cl)

    valid = ~np.isnan(z_raw)
    # robust linear fit z ~ z0 - slope * s
    A = np.column_stack([np.ones(valid.sum()), s_node[valid]])
    coef, *_ = np.linalg.lstsq(A, z_raw[valid], rcond=None)
    z0_fit, slope_fit = coef[0], -coef[1]     # slope positive = downhill
    measured_slope = slope_fit
    slope = float(np.clip(slope_fit, cfg.min_bed_slope, cfg.max_bed_slope))
    z_up = float(np.nanpercentile(z_raw[valid], 20))  # robust upstream bed level
    # bed = clean monotonic downstream plane anchored at the fitted top
    Z = z_up - slope * s_node
    # fill any nan raw with fitted
    dem_meta = dict(
        dem_min=float(np.nanmin(z_raw)), dem_max=float(np.nanmax(z_raw)),
        n_dem_nan=int((~valid).sum()),
        measured_slope=float(measured_slope),
        enforced_slope=slope, bed_top_m=z_up,
        bed_drop_m=float(slope * s_node.max()),
        reach_len_m=float(s_node.max()))
    return Z, dem_meta


def _project_s(X, Y, cl):
    """Along-channel distance of each (X,Y) node projected onto centerline cl."""
    seglen = np.hypot(np.diff(cl[:, 0]), np.diff(cl[:, 1]))
    cum = np.concatenate([[0.0], np.cumsum(seglen)])
    s = np.zeros(len(X))
    for i in range(len(X)):
        px, py = X[i], Y[i]
        best_d = 1e18; best_s = 0.0
        for j in range(len(cl) - 1):
            ax, ay = cl[j]; bx, by = cl[j + 1]
            vx, vy = bx - ax, by - ay
            L2 = vx * vx + vy * vy
            if L2 == 0:
                continue
            t = ((px - ax) * vx + (py - ay) * vy) / L2
            t = min(1.0, max(0.0, t))
            cx, cy = ax + t * vx, ay + t * vy
            dd = (px - cx) ** 2 + (py - cy) ** 2
            if dd < best_d:
                best_d = dd; best_s = cum[j] + t * np.sqrt(L2)
        s[i] = best_s
    return s


# ---------------------------------------------------------------------------
# 5. SELAFIN geometry + boundary conditions (from P0)
# ---------------------------------------------------------------------------
def write_slf(mesh, Z, path):
    from data_manip.extraction.telemac_file import TelemacFile
    if os.path.exists(path):
        os.remove(path)
    tf = TelemacFile(path, access="w")
    tf.add_header(f"P1 REAL RIVER {os.path.basename(path)}",
                  date=np.array([2026, 7, 14, 0, 0, 0]))
    tf.add_mesh(mesh["X"], mesh["Y"], mesh["ikle"], z=Z)
    tf._ipob3 = mesh["ipob"].astype(np.int32)
    tf._ipob2 = tf._ipob3
    tf._nptfr = int(mesh["nptfr"])
    tf._nbor = mesh["ring"].astype(np.int32)
    tf._knolg = np.arange(1, mesh["npoin"] + 1, dtype=np.int32)
    tf.add_variable("BOTTOM          ", "M               ")
    tf.add_data_value("BOTTOM          ", 0, Z)
    tf.write()
    tf.close()


def write_cli(mesh, path):
    ring = mesh["ring"]; nptfr = mesh["nptfr"]
    lines = []
    for k in range(nptfr):
        node1 = int(ring[k]) + 1
        rank = k + 1
        lih, liu = mesh["lihbor"][k], mesh["liubor"][k]
        liv, lit = mesh["livbor"][k], mesh["litbor"][k]
        lines.append(
            f"{lih} {liu} {liv}  0.000 0.000 0.000 0.000  {lit}  0.000 0.000 0.000 "
            f"{node1:>11d} {rank:>11d}   # {mesh['cls'][k]}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# 6. Deck author + liquid-boundary mapping (gotcha 4)
# ---------------------------------------------------------------------------
def author_deck(cfg, mesh, slf, cli, res, cas_path, lb_order, bed):
    """Write the .cas. lb_order maps TELEMAC liquid-boundary index (1-based, in
    boundary-walk order) -> 'inflow' or 'outflow'. PRESCRIBED lists are written
    in that liquid-boundary order (gotcha 4). bed = dem_meta dict."""
    bed_outflow = bed["bed_top_m"] - bed["bed_drop_m"]
    outflow_stage = bed_outflow + cfg.init_depth_m
    q = []; elev = []; tracer = []
    for role in lb_order:
        if role == "inflow":
            q.append(f"{cfg.inflow_q_m3s}")
            elev.append("0.0")
            tracer.append(f"{cfg.dye_conc_mgl}")
        else:  # outflow: prescribe a downstream stage = bed + target depth
            q.append("0.0")
            elev.append(f"{outflow_stage:.3f}")
            tracer.append("0.0")
    cas = f"""/-------------------------------------------------------------------/
/  TELEMAC-2D  P1 REAL RIVER DYE  -  {cfg.name}
/  Mesh from NHDPlus flowlines (Gmsh, tagged physical groups) -> rank IPOBO.
/  Dye prescribed at the upstream inflow liquid boundary, advects downstream
/  following the REAL river curves to the outflow.
/  Liquid-boundary order (walk): {lb_order}
/-------------------------------------------------------------------/
GEOMETRY FILE                   = {os.path.basename(slf)}
BOUNDARY CONDITIONS FILE        = {os.path.basename(cli)}
RESULTS FILE                    = {os.path.basename(res)}
/
TITLE : '{cfg.name} REAL RIVER DYE'
VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B,T1'
GRAPHIC PRINTOUT PERIOD         = {cfg.graphic_period}
LISTING PRINTOUT PERIOD         = 500
/
DURATION                        = {cfg.duration_s}
TIME STEP                       = {cfg.time_step_s}
/
INITIAL CONDITIONS              = 'CONSTANT DEPTH'
INITIAL DEPTH                   = {cfg.init_depth_m:.3f}
/
PRESCRIBED FLOWRATES            = {';'.join(q)}
PRESCRIBED ELEVATIONS           = {';'.join(elev)}
/
LAW OF BOTTOM FRICTION          = 3
FRICTION COEFFICIENT            = 33.
VELOCITY DIFFUSIVITY            = 1.E-1
/
EQUATIONS                       = 'SAINT-VENANT FE'
TREATMENT OF THE LINEAR SYSTEM  = 2
TYPE OF ADVECTION               = 1;5
SUPG OPTION                     = 0;0
MASS-LUMPING ON H : 1.
CONTINUITY CORRECTION : YES
SOLVER                          = 1
SOLVER ACCURACY                 = 1.E-6
MAXIMUM NUMBER OF ITERATIONS FOR SOLVER = 500
IMPLICITATION FOR DEPTH         = 0.6
IMPLICITATION FOR VELOCITY      = 0.6
TIDAL FLATS                             = YES
OPTION FOR THE TREATMENT OF TIDAL FLATS = 1
TREATMENT OF NEGATIVE DEPTHS            = 2
H CLIPPING     : NO
/
NUMBER OF TRACERS               = 1
NAMES OF TRACERS                = 'DYE             MG/L'
INITIAL VALUES OF TRACERS       = 0.
PRESCRIBED TRACERS VALUES       = {';'.join(tracer)}
SCHEME FOR ADVECTION OF TRACERS          = 1
COEFFICIENT FOR DIFFUSION OF TRACERS     = 1.E-1
"""
    with open(cas_path, "w") as f:
        f.write(cas)


def map_liquid_boundaries(listing_text, mesh, tr_back=None):
    """Parse the solver listing's LIQUID BOUNDARIES block and map each numbered
    liquid boundary -> 'inflow'/'outflow' by comparing its reported COORDINATES
    to our tagged inflow/outflow cap-node centroids (gotcha 4).

    TELEMAC v9 listing format:
        THERE IS     2 LIQUID BOUNDARIES:
         BOUNDARY    1 :
          BEGINS AT BOUNDARY POINT: ... GLOBAL NUMBER: ...
          AND COORDINATES:     720978.4           4717564.
          ENDS AT ...
    """
    import re

    def centroid(nodes):
        idx = np.array(sorted(nodes))
        return np.array([mesh["X"][idx].mean(), mesh["Y"][idx].mean()])
    c_in = centroid(mesh["in_nodes"])
    c_out = centroid(mesh["out_nodes"])

    # isolate the LIQUID BOUNDARIES section (up to SOLID BOUNDARIES)
    m0 = re.search(r"LIQUID BOUNDARIES", listing_text)
    if not m0:
        return None
    tail = listing_text[m0.end():]
    m1 = re.search(r"SOLID BOUNDARIES", tail)
    block = tail[:m1.start()] if m1 else tail

    order = {}
    # each "BOUNDARY  N :" followed later by first "COORDINATES:  x   y"
    for bm in re.finditer(r"BOUNDARY\s+(\d+)\s*:", block):
        lbnum = int(bm.group(1))
        sub = block[bm.end():]
        cm = re.search(r"COORDINATES:\s*([-\d.Ee+]+)\s+([-\d.Ee+]+)", sub)
        if not cm:
            continue
        p = np.array([float(cm.group(1)), float(cm.group(2))])
        role = "inflow" if np.hypot(*(p - c_in)) < np.hypot(*(p - c_out)) else "outflow"
        order[lbnum] = role
    if order:
        return [order[k] for k in sorted(order)]
    return None


# ---------------------------------------------------------------------------
# 7. Solver
# ---------------------------------------------------------------------------
def run_solver(cas_path, res_path, cwd, timeout=1200):
    if os.path.exists(res_path):
        os.remove(res_path)          # gotcha 6
    log = subprocess.run(
        ["telemac2d.py", os.path.basename(cas_path)],
        cwd=cwd, capture_output=True, text=True, timeout=timeout)
    out = log.stdout + "\n" + log.stderr
    ok = "CORRECT END OF RUN" in out
    return ok, out
