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
import logging
import os
import subprocess
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

LOG = logging.getLogger("trid3nt.worker.telemac.build")
from pathlib import Path


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
    dye_conc_mgl: float = 100.0         # dye concentration of the spill pulse
    # FINITE SPILL PULSE (realism default): a mid-reach point source injects dye
    # for a short window then stops, so the slug TRAVELS downstream and dilutes
    # rather than the old continuous upstream-inflow injection saturating the
    # whole reach. Clean flow (inflow->outflow) still drives it.
    spill_frac: float = 0.25            # along-channel position of the spill (0=up,1=down)
    # BK-6 release-point picker: explicit spill location (EPSG:4326). When BOTH
    # are set they OVERRIDE spill_frac - the source snaps to the nearest
    # interior mesh node to this point (validated within 2 channel widths).
    release_lon: float = None           # type: ignore[assignment]
    release_lat: float = None           # type: ignore[assignment]
    # BK-7 real-bank meshing: "auto" samples USGS NHDArea river polygons for
    # per-station left/right bank offsets (mesh follows the REAL river);
    # "constant" keeps the legacy fixed-width ribbon.
    bank_source: str = "auto"
    # OPEN-26 wrong-watercourse fix: when the prompt NAMES the river, re-seed
    # onto the NAMED GNIS mainstem before the NLDI position-snap. A raw
    # geocode-point snap near a confluence (Longview = Columbia x Cowlitz)
    # routinely lands on the tributary or a slough; the named-flowline query
    # (proven manually on the Columbia, comid 24520442) disambiguates.
    river_name: str = ""
    pulse_window_s: float = 300.0       # dye-on window; source turns OFF after
    source_q_m3s: float = 8.0           # carrier discharge of the point source (small vs inflow)
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


_NHDPLUS_HR = "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer"


def _named_flowline_seed(
    name: str, lon: float, lat: float, search_deg: float = 0.15
) -> tuple[float, float] | None:
    """Nearest vertex of the NAMED GNIS flowline to (lon, lat), or None.

    Queries NHDPlus_HR layer 3 (NetworkNHDFlowline) by gnis_name within a
    ~search_deg envelope around the raw seed. Fail-OPEN: any error / no match
    returns None and the caller keeps the raw position-snap (honest degrade).
    """
    safe = name.replace("'", "''").strip()
    if not safe:
        return None
    env = json.dumps({
        "xmin": lon - search_deg, "ymin": lat - search_deg,
        "xmax": lon + search_deg, "ymax": lat + search_deg,
        "spatialReference": {"wkid": 4326},
    })
    q = urllib.parse.urlencode({
        "f": "geojson",
        "where": f"UPPER(gnis_name)=UPPER('{safe}')",
        "geometry": env, "geometryType": "esriGeometryEnvelope",
        "inSR": 4326, "outSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "gnis_name", "returnGeometry": "true",
        "maxAllowableOffset": 0.0005, "resultRecordCount": 200,
    })
    try:
        fc = json.loads(_http_get(f"{_NHDPLUS_HR}/3/query?{q}", timeout=45.0))
    except Exception as exc:  # noqa: BLE001 -- network fail-open to raw seed
        LOG.warning("named-flowline seed query failed (%s) - raw seed kept", exc)
        return None
    best: tuple[float, float] | None = None
    best_d2 = float("inf")
    for feat in fc.get("features") or []:
        geom = feat.get("geometry") or {}
        lines = (
            [geom.get("coordinates")]
            if geom.get("type") == "LineString"
            else geom.get("coordinates") or []
        )
        for line in lines:
            for v in line or []:
                d2 = (v[0] - lon) ** 2 + (v[1] - lat) ** 2
                if d2 < best_d2:
                    best_d2, best = d2, (float(v[0]), float(v[1]))
    return best


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
    seed_lon, seed_lat, seed_kind = cfg.seed_lon, cfg.seed_lat, "position"
    if cfg.river_name:
        named = _named_flowline_seed(cfg.river_name, seed_lon, seed_lat)
        if named is not None:
            seed_lon, seed_lat, seed_kind = named[0], named[1], "named-flowline"
            LOG.info(
                "named-flowline re-seed %r: (%.5f,%.5f) -> (%.5f,%.5f)",
                cfg.river_name, cfg.seed_lon, cfg.seed_lat, seed_lon, seed_lat,
            )
        else:
            LOG.warning(
                "named-flowline re-seed %r found nothing - raw seed kept",
                cfg.river_name,
            )
    comid = _snap_comid(seed_lon, seed_lat)
    url = f"{_NLDI}/comid/{comid}/navigation/{cfg.nav_direction}/flowlines?distance={cfg.distance_km}"
    fc = json.loads(_http_get(url))
    feats = fc["features"]
    path = _stitch_flowlines(feats)
    ll = np.array(path, dtype=float)
    meta = dict(
        seed_comid=comid, n_flowlines=len(feats), n_raw_vertices=len(ll),
        seed_kind=seed_kind,
    )
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
# 2b. BK-7: real river banks from USGS NHDArea polygons
# ---------------------------------------------------------------------------
_NHDAREA_URL = (
    "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/"
    "MapServer/8/query"
)


def fetch_bank_polygons(bbox4326, timeout=30.0):
    """NHDArea water polygons intersecting ``bbox4326`` (lonlat) as a list of
    (exterior_ring, [hole_rings]) lonlat arrays. None on ANY failure/empty -
    the caller falls back to the constant-width ribbon (honest degrade)."""
    import json as _json
    import urllib.parse
    import urllib.request

    params = urllib.parse.urlencode({
        "geometry": ",".join(f"{v:.6f}" for v in bbox4326),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326", "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "ftype", "f": "geojson",
        # big-river hardening (Columbia hang): server-side simplification
        # (~5 m at mid-latitudes) + a record cap - the reach bbox only needs
        # local bank detail, not the full mainstem polygon.
        "maxAllowableOffset": "0.00005",
        "resultRecordCount": "200",
    })
    try:
        with urllib.request.urlopen(f"{_NHDAREA_URL}?{params}", timeout=timeout) as r:
            data = _json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 -- degrade, never dead-end
        LOG.warning("NHDArea fetch failed (%s); constant-width fallback", exc)
        return None
    polys = []
    for f in data.get("features") or []:
        g = f.get("geometry") or {}
        if g.get("type") == "Polygon":
            rings = g.get("coordinates") or []
            if rings:
                polys.append((np.asarray(rings[0], dtype=float),
                              [np.asarray(rr, dtype=float) for rr in rings[1:]]))
        elif g.get("type") == "MultiPolygon":
            for rings in g.get("coordinates") or []:
                if rings:
                    polys.append((np.asarray(rings[0], dtype=float),
                                  [np.asarray(rr, dtype=float) for rr in rings[1:]]))
    return polys or None


def estimate_bank_offsets(cl, polys_utm, max_half=800.0, step=4.0,
                          min_half=8.0, valid_frac_floor=0.3):
    """Per-station (left, right) bank distances from the water polygons.

    Casts a perpendicular transect at every centerline station, marks which
    samples are WATER (inside an exterior ring minus its holes), and takes the
    contiguous water run nearest the centerline. Stations with no water within
    ~30 m are interpolated from neighbours; if fewer than ``valid_frac_floor``
    of stations see water at all, returns None (constant-width fallback).
    Profiles are smoothed + gradient-limited so gmsh bank offsetting stays
    simple (no bowties from step changes)."""
    import shapely.geometry as sg
    try:
        import shapely
        _contains = lambda g, xs, ys: shapely.contains_xy(g, xs, ys)  # noqa: E731
    except Exception:  # noqa: BLE001 -- shapely<2 fallback
        from shapely.prepared import prep
        def _contains(g, xs, ys, _p={}):
            pg = _p.setdefault(id(g), prep(g))
            return np.array([pg.contains(sg.Point(x, y)) for x, y in zip(xs, ys)])

    water = [sg.Polygon(ext, holes=[h for h in holes if len(h) >= 4])
             for ext, holes in polys_utm if len(ext) >= 4]
    water = [w.buffer(0) for w in water if not w.is_empty]
    if not water:
        return None
    from shapely.ops import unary_union
    # CLIP to the transect envelope BEFORE the union (Columbia hang fix): the
    # fetched mainstem polygon can span far beyond the reach; unclipped it
    # makes every point-in-polygon test walk hundreds of thousands of
    # vertices. The clip box = centerline extent + max_half margin.
    clip = sg.box(cl[:, 0].min() - max_half, cl[:, 1].min() - max_half,
                  cl[:, 0].max() + max_half, cl[:, 1].max() + max_half)
    water = [w.intersection(clip) for w in water]
    water = [w for w in water if not w.is_empty]
    if not water:
        return None
    union = unary_union(water)
    try:
        nv = sum(len(g.exterior.coords) for g in getattr(union, "geoms", [union])
                 if g.geom_type == "Polygon")
        LOG.info("bank union: %d polys, ~%d verts after clip", len(water), nv)
    except Exception:  # noqa: BLE001
        pass

    x, y = cl[:, 0], cl[:, 1]
    dx = np.gradient(x); dy = np.gradient(y)
    seg = np.hypot(dx, dy); seg[seg == 0] = 1e-9
    nx = -dy / seg; ny = dx / seg
    ts = np.arange(-max_half, max_half + step, step)
    n = len(cl)
    left = np.full(n, np.nan); right = np.full(n, np.nan)
    for i in range(n):
        sx = x[i] + nx[i] * ts
        sy = y[i] + ny[i] * ts
        try:
            wet = np.asarray(_contains(union, sx, sy), dtype=bool)
        except Exception:  # noqa: BLE001
            return None
        if not wet.any():
            continue
        # contiguous wet runs; pick the one nearest t=0 (within 30 m)
        idx = np.flatnonzero(wet)
        splits = np.flatnonzero(np.diff(idx) > 1)
        runs = np.split(idx, splits + 1)
        zero = len(ts) // 2
        best, bestd = None, 1e18
        for run in runs:
            d = 0.0 if run[0] <= zero <= run[-1] else min(
                abs(ts[run[0]]), abs(ts[run[-1]]))
            if d < bestd:
                best, bestd = run, d
        if best is None or bestd > 30.0:
            continue
        # offsets relative to the centerline station (positive both sides)
        left[i] = max(min_half, ts[best[-1]])
        right[i] = max(min_half, -ts[best[0]])
    valid = np.isfinite(left) & np.isfinite(right)
    if valid.mean() < valid_frac_floor:
        return None
    # interpolate gaps from valid neighbours
    ii = np.arange(n)
    for arr in (left, right):
        good = np.isfinite(arr)
        arr[~good] = np.interp(ii[~good], ii[good], arr[good])
    # RECENTER + SMOOTH-FIRST (Columbia fold fix v2): the fold root causes were
    # (a) an off-center axis needing huge one-sided offsets and (b) building
    # the axis from RAW noisy per-station offsets (jagged axis -> oscillating
    # normals -> both banks fold). Correct order: smooth the shift/half-width
    # PROFILES first, then build the mid-water axis, then RESAMPLE it to
    # uniform spacing (kills kinks), then curvature-clamp.
    def _rsmooth(a, kk):
        pad = kk // 2
        ap = np.r_[a[pad:0:-1], a, a[-2:-pad - 2:-1]]
        return np.convolve(ap, np.ones(kk) / kk, mode="valid")

    shift = _rsmooth((left - right) / 2.0, 15)
    halfw = _rsmooth((left + right) / 2.0, 15)
    cl_mid = np.column_stack([x + nx * shift, y + ny * shift])
    cl_mid[:, 0] = _rsmooth(cl_mid[:, 0], 9)
    cl_mid[:, 1] = _rsmooth(cl_mid[:, 1], 9)

    # uniform arc-length resample of the axis (+ halfw onto the new stations)
    seg2 = np.hypot(*np.diff(cl_mid, axis=0).T)
    s = np.concatenate([[0.0], np.cumsum(seg2)])
    ds = float(np.median(seg))
    n_new = max(int(s[-1] / ds), 8)
    s_new = np.linspace(0.0, s[-1], n_new + 1)
    cl_mid = np.column_stack([np.interp(s_new, s, cl_mid[:, 0]),
                              np.interp(s_new, s, cl_mid[:, 1])])
    halfw = np.interp(s_new, s, halfw)

    # CURVATURE CLAMP: half-width <= 0.7 * local bend radius makes a fold
    # geometrically impossible (3-point circumradius, +-4 stations).
    n2 = len(cl_mid)
    radius = np.full(n2, 1e9)
    for i in range(4, n2 - 4):
        a, b, c = cl_mid[i - 4], cl_mid[i], cl_mid[i + 4]
        ab = np.hypot(*(b - a)); bc = np.hypot(*(c - b)); ca = np.hypot(*(a - c))
        area2 = abs((b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0]))
        if area2 > 1e-6:
            radius[i] = (ab * bc * ca) / (2.0 * area2)
    halfw = np.minimum(halfw, np.maximum(min_half, 0.7 * radius))

    # final half-width smoothing + clamps + gradient limit
    max_delta = 0.35 * ds
    halfw = _rsmooth(halfw, 9)
    np.clip(halfw, min_half, max_half, out=halfw)
    for _ in range(200):
        d = np.diff(halfw)
        over = np.abs(d) > max_delta
        if not over.any():
            break
        d = np.clip(d, -max_delta, max_delta)
        halfw[1:] = halfw[0] + np.cumsum(d)
        np.clip(halfw, min_half, max_half, out=halfw)
    return cl_mid, halfw, round(float(valid.mean()), 3)


# ---------------------------------------------------------------------------
# 3. Channel banks + Gmsh mesh (adapts P0 build_gmsh_channel, honoring gotchas)
# ---------------------------------------------------------------------------
def _offset_banks(cl: np.ndarray, width: float, offsets=None):
    x, y = cl[:, 0], cl[:, 1]
    dx = np.gradient(x); dy = np.gradient(y)
    seg = np.hypot(dx, dy); seg[seg == 0] = 1e-9
    nx = -dy / seg; ny = dx / seg
    if offsets is not None:
        lo, ro = offsets
        ds_med = float(np.median(seg))
        half_med = float(np.median((np.asarray(lo) + np.asarray(ro)) / 2.0))
        # HYBRID BY SCALE (Columbia fold fix v3): large offsets (wide rivers)
        # amplify tiny normal jitter into micro self-intersections, and that is
        # exactly the regime where width VARIATION is negligible (+-14% on the
        # Columbia). So: offsets > 3x station spacing -> GEOS offset_curve at
        # the CONSTANT median half-width (guaranteed simple by the geometry
        # kernel); small offsets (creeks, where variation is the whole point:
        # 16-46 m at Twin Falls) keep the per-station variable construction
        # (proven fold-free at that scale).
        if half_med > 3.0 * ds_med:
            try:
                import shapely.geometry as _sg
                axis = _sg.LineString(cl)
                def _side(dist):
                    try:
                        line = axis.offset_curve(dist)
                    except AttributeError:  # shapely<2
                        line = axis.parallel_offset(abs(dist),
                                                    "left" if dist > 0 else "right")
                    if line.geom_type == "MultiLineString":
                        line = max(line.geoms, key=lambda g: g.length)
                    pts = np.asarray(line.coords)
                    # GEOS may reverse the right-side curve; align to the axis
                    if np.hypot(*(pts[0] - cl[0])) > np.hypot(*(pts[-1] - cl[0])):
                        pts = pts[::-1]
                    return pts
                left = _side(+half_med)
                right = _side(-half_med)
                return left, right
            except Exception:  # noqa: BLE001 -- fall through to per-station
                pass
        left = np.column_stack([x + nx * lo, y + ny * lo])
        right = np.column_stack([x - nx * ro, y - ny * ro])
    else:
        left = np.column_stack([x + nx * width / 2, y + ny * width / 2])
        right = np.column_stack([x - nx * width / 2, y - ny * width / 2])
    return left, right


def _banks_valid(left: np.ndarray, right: np.ndarray) -> bool:
    """Reject if either bank self-intersects (tight bend folded the inner bank)."""
    import shapely.geometry as sg

    return sg.LineString(left).is_simple and sg.LineString(right).is_simple


def _water_polygon_domain(cl: np.ndarray, cfg: ReachConfig, ms: float):
    """The TRUE water-polygon mesh domain, or None to fall back to the ribbon.

    NATE 2026-07-18: the ribbon outline (smoothed sampled half-widths +
    curvature clamps + straight caps) visibly mismatches the river. Instead of
    approximating, mesh the NHDArea water polygon DIRECTLY: clip the water
    union to a corridor around the reach, take the component under the
    centerline, and use its exterior as the outer boundary (holes = islands).
    The corridor's end cuts leave straight cap segments ON the end transect
    lines - those become the inflow/outflow boundaries.

    Returns (exterior_ring[N,2], hole_rings, cap_in_line, cap_out_line) where
    the cap lines are ((x0,y0),(x1,y1)) segments the caps lie on.
    """
    import shapely.geometry as sg
    from shapely.ops import unary_union

    water_polys = getattr(cfg, "water_polys_utm", None)
    if not water_polys:
        return None
    offsets = getattr(cfg, "bank_offsets", None)
    if offsets is None:
        return None
    half_max = float(np.max((np.asarray(offsets[0]) + np.asarray(offsets[1])) / 2.0))
    # The corridor exists ONLY to cut the reach at its two ends - laterally it
    # must never cut water (NATE 2026-07-18: the back-channels behind Fisher
    # and Cottonwood islands were clipped off at ~1.3x the sampled half-width).
    W = 2.0 * max(4.0 * half_max, 2500.0)
    left, right = _offset_banks(cl, W, None)
    corridor = sg.Polygon(np.vstack([left, right[::-1]]))
    if not corridor.is_valid:
        corridor = corridor.buffer(0)
    water = unary_union([
        sg.Polygon(ext, holes=[h for h in holes if len(h) >= 4])
        for ext, holes in water_polys if len(ext) >= 4
    ]).buffer(0)
    clip = water.intersection(corridor)
    if clip.is_empty:
        return None
    mid = sg.Point(cl[len(cl) // 2])
    comps = list(getattr(clip, "geoms", [clip]))
    comps = [c for c in comps if isinstance(c, sg.Polygon) and not c.is_empty]
    if not comps:
        return None
    main = min(comps, key=lambda c: c.distance(mid))
    main = main.simplify(ms / 2.0)
    if main.is_empty or not main.is_valid or main.area < (10 * ms) ** 2:
        return None
    ext = np.asarray(main.exterior.coords)[:-1]
    holes = []
    for hole in main.interiors:
        hp = sg.Polygon(hole)
        if hp.area >= (2.5 * ms) ** 2 and len(hole.coords) >= 5:
            holes.append(np.asarray(hole.coords)[:-1])
    # end-cap lines = the corridor's end edges (transects at cl[0] / cl[-1])
    cap_in = (tuple(left[0]), tuple(right[0]))
    cap_out = (tuple(left[-1]), tuple(right[-1]))
    # COVERAGE GUARD (NATE 2026-07-18, after the amputated back-channels): the
    # meshed domain must account for ~all of the RIVER'S OWN water between the
    # end transects. Reference = the connected water component under the
    # centerline, clipped by a laterally-UNBOUNDED slab (20 km half-width) so
    # a too-narrow corridor cannot hide what it cut off; disconnected ponds
    # and sloughs never depress the number. Rides metrics + the gate card.
    try:
        slab_l, slab_r = _offset_banks(cl, 40000.0, None)
        slab = sg.Polygon(np.vstack([slab_l, slab_r[::-1]]))
        if not slab.is_valid:
            slab = slab.buffer(0)
        river_comp = None
        for c in getattr(water, "geoms", [water]):
            if c.contains(mid) or c.distance(mid) < 50.0:
                river_comp = c
                break
        ref_area = float(river_comp.intersection(slab).area) if river_comp is not None else 0.0
        coverage = float(main.area / ref_area) if ref_area > 0 else 1.0
        coverage = min(coverage, 1.0)
    except Exception as exc:  # noqa: BLE001 -- guard must never block meshing
        LOG.warning("water-coverage computation failed (%s)", exc)
        coverage = 1.0
    if coverage < 0.90:
        LOG.warning(
            "water-coverage LOW: mesh domain covers %.0f%% of the river's "
            "water in the reach (%.1fM of %.1fM m2) - water may be unmeshed",
            coverage * 100, main.area / 1e6, ref_area / 1e6)
    LOG.info("water-polygon domain: %d exterior pts, %d island holes, "
             "area %.0f m2, water coverage %.1f%%",
             len(ext), len(holes), main.area, coverage * 100)
    return ext, holes, cap_in, cap_out, coverage


def _dist_to_segment(pts: np.ndarray, a, b) -> np.ndarray:
    """Distances from pts[N,2] to segment a-b (vectorized)."""
    a = np.asarray(a, dtype=float); b = np.asarray(b, dtype=float)
    ab = b - a
    denom = float(ab @ ab) or 1e-12
    t = np.clip(((pts - a) @ ab) / denom, 0.0, 1.0)
    proj = a + t[:, None] * ab
    return np.hypot(*(pts - proj).T)


def build_channel_mesh(cl: np.ndarray, cfg: ReachConfig):
    """Gmsh mesh of the real channel-following polygon; tagged boundary groups.

    Returns a mesh dict with 0-based ikle, rank-based IPOBO ring, and the
    inflow/outflow node sets (P0 gotchas 1-3, 7).
    """
    import gmsh
    import signal

    offsets = getattr(cfg, "bank_offsets", None)
    left, right = _offset_banks(cl, cfg.channel_width_m, offsets)
    # gmsh-hang hardening (live: wide-river banks hung generate(2) for 18+ min
    # silently; a 50 km reach did the same earlier). Bound the WHOLE build with
    # SIGALRM (single-threaded worker) -> honest MESH_BUILD_TIMEOUT. Also dump
    # the exact geometry first so a failing case is reproducible offline.
    try:
        np.savez(str(Path(cfg.workdir) / "banks_debug.npz"),
                 cl=cl, left=left, right=right)
    except Exception:  # noqa: BLE001 -- debug dump is best-effort
        pass

    def _gmsh_timeout(_sig, _frm):
        raise TimeoutError("MESH_BUILD_TIMEOUT: gmsh exceeded 240 s")

    signal.signal(signal.SIGALRM, _gmsh_timeout)
    signal.alarm(240)
    # gotcha 7: if banks self-intersect at a bend, smooth harder until simple
    tries = 0
    while not _banks_valid(left, right) and tries < 6:
        k = np.ones(5) / 5
        cl = np.column_stack([np.convolve(cl[:, 0], k, mode="same"),
                              np.convolve(cl[:, 1], k, mode="same")])
        cl[0] = cl[0]; cl[-1] = cl[-1]
        if offsets is not None:
            offsets = (np.convolve(offsets[0], k, mode="same"),
                       np.convolve(offsets[1], k, mode="same"))
        left, right = _offset_banks(cl, cfg.channel_width_m, offsets)
        tries += 1
    if not _banks_valid(left, right):
        signal.alarm(0)
        raise RuntimeError(
            "MESH_BANKS_INVALID: bank offset curves still self-intersect "
            "after smoothing retries - refusing to mesh a folded channel"
        )
    banks_ok = _banks_valid(left, right)
    ms = cfg.mesh_size_m

    # TRUE water-polygon domain (NATE 2026-07-18: the ribbon outline mismatches
    # the river). When it resolves, the mesh boundary IS the NHDArea bank line
    # and holes are the real islands; the ribbon below stays as the fallback.
    domain = None
    try:
        domain = _water_polygon_domain(cl, cfg, ms)
    except Exception as exc:  # noqa: BLE001 -- polygon domain is best-effort
        LOG.warning("water-polygon domain failed (%s) - ribbon fallback", exc)
    ext_pts = on_in = on_out = None
    island_rings: list[np.ndarray] = []
    if domain is not None:
        ext_pts, island_rings, cap_in, cap_out, water_coverage = domain
        d_in = _dist_to_segment(ext_pts, *cap_in)
        d_out = _dist_to_segment(ext_pts, *cap_out)
        on_in = d_in < ms
        on_out = d_out < ms
        n_in_edges = int(np.sum(on_in & np.roll(on_in, -1)))
        n_out_edges = int(np.sum(on_out & np.roll(on_out, -1)))
        if n_in_edges == 0 or n_out_edges == 0:
            LOG.warning(
                "water-polygon domain has no cap edges (in=%d out=%d) - "
                "ribbon fallback", n_in_edges, n_out_edges)
            domain = None
            island_rings = []

    # Ribbon fallback island holes: any ribbon area NOT covered by water
    # (interior holes AND channel-splitting islands like Cottonwood) becomes a
    # walled hole. Kept clear of the outer boundary; slivers below (2.5*h)^2
    # dropped (unmeshable at edge length h).
    water_polys = getattr(cfg, "water_polys_utm", None)
    if domain is None and water_polys:
        try:
            import shapely.geometry as sg
            from shapely.ops import unary_union

            ribbon = sg.Polygon(np.vstack([left, right[::-1]]))
            if not ribbon.is_valid:
                ribbon = ribbon.buffer(0)
            water = unary_union([
                sg.Polygon(ext, holes=[h for h in holes if len(h) >= 4])
                for ext, holes in water_polys if len(ext) >= 4
            ]).buffer(0)
            land = ribbon.buffer(-1.5 * ms).difference(water)
            geoms = getattr(land, "geoms", [land])
            for g in geoms:
                if g.is_empty or g.area < (2.5 * ms) ** 2:
                    continue
                g = g.simplify(ms / 2.0)
                if g.is_empty or not g.is_valid:
                    continue
                ext = np.asarray(g.exterior.coords)
                if len(ext) >= 5:
                    island_rings.append(ext[:-1])  # drop closing duplicate
            if island_rings:
                LOG.info("island holes: %d (areas %s m2)", len(island_rings),
                         [int(sg.Polygon(r).area) for r in island_rings])
        except Exception as exc:  # noqa: BLE001 -- islands are an enhancement,
            # never a mesh blocker; fall back to the hole-less ribbon
            LOG.warning("island-hole derivation failed (%s) - meshing without", exc)
            island_rings = []

    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.model.add(cfg.name)

    def add_pts(pts):
        return [gmsh.model.geo.addPoint(float(px), float(py), 0.0, ms)
                for px, py in pts]

    if domain is not None:
        # exterior = the REAL bank line as a closed chain of straight edges;
        # edges whose BOTH endpoints lie on an end-transect line become the
        # inflow/outflow caps, the rest are walls
        ptags = add_pts(ext_pts)
        n_ext = len(ptags)
        in_lines, out_lines, wall_lines, ordered = [], [], [], []
        for i in range(n_ext):
            j = (i + 1) % n_ext
            ln = gmsh.model.geo.addLine(ptags[i], ptags[j])
            ordered.append(ln)
            if on_in[i] and on_in[j]:
                in_lines.append(ln)
            elif on_out[i] and on_out[j]:
                out_lines.append(ln)
            else:
                wall_lines.append(ln)
        loop = gmsh.model.geo.addCurveLoop(ordered)
        wall_group_curves = wall_lines
        inflow_curves, outflow_curves = in_lines, out_lines
    else:
        lpts = add_pts(left)     # left bank upstream->downstream
        rpts = add_pts(right)
        left_wall = gmsh.model.geo.addSpline(lpts)
        right_wall = gmsh.model.geo.addSpline(rpts)
        inflow = gmsh.model.geo.addLine(rpts[0], lpts[0])     # upstream cap
        outflow = gmsh.model.geo.addLine(lpts[-1], rpts[-1])  # downstream cap
        loop = gmsh.model.geo.addCurveLoop(
            [left_wall, outflow, -right_wall, inflow])
        wall_group_curves = [left_wall, right_wall]
        inflow_curves, outflow_curves = [inflow], [outflow]
    hole_loops = []
    for hr in island_rings:
        # straight LINE chain, not a spline: splines overshoot at polygon
        # corners and can poke outside the surface, which kills generate(2)
        # silently (live: 9 islands -> zero triangles)
        hpts = add_pts(hr)
        hlines = [gmsh.model.geo.addLine(hpts[i], hpts[(i + 1) % len(hpts)])
                  for i in range(len(hpts))]
        hole_loops.append(gmsh.model.geo.addCurveLoop(hlines))
    surf = gmsh.model.geo.addPlaneSurface([loop, *hole_loops])
    gmsh.model.geo.synchronize()

    g_in = gmsh.model.addPhysicalGroup(1, inflow_curves)
    g_out = gmsh.model.addPhysicalGroup(1, outflow_curves)
    gmsh.model.addPhysicalGroup(1, wall_group_curves)
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
    if tri_tags is None or len(tri_tags) == 0:
        gmsh.finalize()
        signal.alarm(0)
        raise RuntimeError(
            "MESH_BUILD_EMPTY: gmsh generated no triangles (bad hole/boundary "
            f"geometry? islands={len(island_rings)})"
        )
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
    signal.alarm(0)

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
    # M3: with island holes the boundary is SEVERAL closed cycles. Walk them
    # all; the triangle-oriented directed edges already wind outer-CCW /
    # holes-CW (domain on the left), which is the TELEMAC convention. IPOBO
    # ranks run consecutively ring by ring, OUTER (longest) first.
    rings: list[list[int]] = []
    unvisited = set(nxt)
    while unvisited:
        start = next(iter(unvisited))
        walk = [start]; cur = nxt[start]
        while cur != start:
            walk.append(cur); cur = nxt[cur]
        unvisited -= set(walk)
        rings.append(walk)
    assert sum(len(w) for w in rings) == len(bnd), "boundary walk lost edges"
    rings.sort(key=len, reverse=True)
    n_islands = len(rings) - 1
    boundary_rings = [np.array(w, dtype=np.int64) for w in rings]
    ring = np.array([n for w in rings for n in w], dtype=np.int64)

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
                n_islands=n_islands, boundary_rings=boundary_rings,
                domain_mode="water-polygon" if domain is not None else "ribbon",
                water_coverage_frac=(round(float(water_coverage), 4)
                                     if domain is not None else None),
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
#: Basename of the TELEMAC-2D SOURCES FILE (the time series for the finite
#: mid-reach dye pulse). Written by author_deck next to the .cas; referenced by
#: basename in the deck (the solver stages it into its temp workdir). The worker
#: entrypoint lists this in its outputs so the pulse forcing is uploaded as
#: evidence next to the result .slf.
SOURCES_FILENAME = "river_sources.txt"


def spill_point(mesh, cfg):
    """Mid-reach spill (X, Y, node index) at ``cfg.spill_frac`` of the channel.

    Walks the smoothed centerline to the ``spill_frac`` arc-length point, then
    snaps to the nearest INTERIOR mesh node (never a boundary-ring node, so the
    point source is a genuine in-channel release, not on the inflow/outflow cap
    or a wall). TELEMAC snaps ABSCISSAE/ORDINATES OF SOURCES to the nearest node
    anyway; we pre-snap so the reported coordinate is an actual wet node.
    """
    cl = mesh["centerline"]
    # BK-6: an explicit user-picked release point (set as UTM by run_pipeline
    # from cfg.release_lon/lat) overrides the spill_frac walk - but only when
    # it lands within 2 channel widths of the mesh (else fall back + note).
    rel = getattr(cfg, "release_utm", None)
    px = py = None
    if rel is not None:
        rx, ry = float(rel[0]), float(rel[1])
        d2r = (mesh["X"] - rx) ** 2 + (mesh["Y"] - ry) ** 2
        # accept radius: 2 stated widths OR 1.5x the widest REAL bank span
        # (wide rivers like the Columbia dwarf the stated default width)
        lim = 2.0 * float(cfg.channel_width_m)
        off = getattr(cfg, "bank_offsets", None)
        if off is not None:
            lim = max(lim, 1.5 * float((off[0] + off[1]).max()))
        if float(np.sqrt(d2r.min())) <= lim:
            px, py = rx, ry
            mesh["release_point_used"] = True
        else:
            mesh["release_point_rejected_dist_m"] = round(float(np.sqrt(d2r.min())), 1)
    if px is None:
        seglen = np.hypot(np.diff(cl[:, 0]), np.diff(cl[:, 1]))
        cum = np.concatenate([[0.0], np.cumsum(seglen)])
        total = float(cum[-1])
        target = float(np.clip(cfg.spill_frac, 0.0, 1.0)) * total
        j = int(np.clip(np.searchsorted(cum, target), 1, len(cl) - 1))
        seg = max(cum[j] - cum[j - 1], 1e-9)
        st = (target - cum[j - 1]) / seg
        px = cl[j - 1, 0] + st * (cl[j, 0] - cl[j - 1, 0])
        py = cl[j - 1, 1] + st * (cl[j, 1] - cl[j - 1, 1])
    ring = set(int(n) for n in mesh["ring"])
    d2 = (mesh["X"] - px) ** 2 + (mesh["Y"] - py) ** 2
    for idx in np.argsort(d2):
        if int(idx) not in ring:
            return float(mesh["X"][idx]), float(mesh["Y"][idx]), int(idx)
    return float(px), float(py), -1


def write_sources_pulse(path, cfg):
    """Write the SOURCES FILE: a FINITE dye pulse then the point source stops.

    Columns are the TELEMAC-2D sources-file names (same reader as the liquid-
    boundary file): ``T`` (s), ``Q(1)`` (m3/s carrier discharge), ``TR(1,1)``
    (dye mg/L). Q + dye are held over ``[0, pulse_window_s]`` then step to zero
    (a spill that stops), so the slug travels downstream and dilutes/passes. The
    final time exceeds DURATION so time interpolation never runs off the end.
    """
    w = float(cfg.pulse_window_s)
    q = float(cfg.source_q_m3s)
    dye = float(cfg.dye_conc_mgl)
    tend = max(float(cfg.duration_s) + 100.0, w + 100.0)
    lines = [
        "#",
        "T Q(1) TR(1,1)",
        "s m3/s mg/l",
        f"0.0 {q:.3f} {dye:.3f}",
        f"{w:.3f} {q:.3f} {dye:.3f}",
        f"{w + 0.1:.3f} 0.0 0.0",
        f"{tend:.3f} 0.0 0.0",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def author_deck(cfg, mesh, slf, cli, res, cas_path, lb_order, bed):
    """Write the .cas (+ the SOURCES FILE for the finite spill pulse).

    lb_order maps the TELEMAC liquid-boundary index (1-based, in boundary-walk
    order) -> 'inflow' or 'outflow'; PRESCRIBED lists are written in that order
    (gotcha 4). bed = dem_meta dict.

    DYE forcing = a FINITE PULSE at a mid-reach POINT SOURCE (not the old
    continuous upstream-inflow injection): clean flow (inflow Q, outflow stage)
    drives the reach with ZERO dye at the boundaries; the point source injects
    dye for ``pulse_window_s`` then turns off, so the plume advects downstream
    and dilutes/passes rather than saturating the whole reach.
    """
    bed_outflow = bed["bed_top_m"] - bed["bed_drop_m"]
    outflow_stage = bed_outflow + cfg.init_depth_m
    q = []; elev = []; tracer = []
    for role in lb_order:
        if role == "inflow":
            q.append(f"{cfg.inflow_q_m3s}")
            elev.append("0.0")
            tracer.append("0.0")     # clean flow -- dye enters via the point source
        else:  # outflow: prescribe a downstream stage = bed + target depth
            q.append("0.0")
            elev.append(f"{outflow_stage:.3f}")
            tracer.append("0.0")

    sx, sy, snode = spill_point(mesh, cfg)
    src_path = os.path.join(os.path.dirname(os.path.abspath(cas_path)), SOURCES_FILENAME)
    write_sources_pulse(src_path, cfg)

    cas = f"""/-------------------------------------------------------------------/
/  TELEMAC-2D  P1 REAL RIVER DYE  -  {cfg.name}
/  Mesh from NHDPlus flowlines (Gmsh, tagged physical groups) -> rank IPOBO.
/  Clean flow (inflow->outflow) drives the reach; a FINITE dye pulse is
/  released at a mid-reach point source (~{cfg.spill_frac:.0%} along, node
/  {snode}, x={sx:.1f} y={sy:.1f}) for {cfg.pulse_window_s:.0f}s then stops, so
/  the plume advects downstream following the REAL river curves and dilutes.
/  Liquid-boundary order (walk): {lb_order}
/-------------------------------------------------------------------/
GEOMETRY FILE                   = {os.path.basename(slf)}
BOUNDARY CONDITIONS FILE        = {os.path.basename(cli)}
RESULTS FILE                    = {os.path.basename(res)}
SOURCES FILE                    = {SOURCES_FILENAME}
/
TITLE : '{cfg.name} REAL RIVER DYE PULSE'
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
MAXIMUM NUMBER OF SOURCES        = 20
ABSCISSAE OF SOURCES             = {sx:.3f}
ORDINATES OF SOURCES             = {sy:.3f}
WATER DISCHARGE OF SOURCES       = 0.0
VALUES OF THE TRACERS AT THE SOURCES = 0.0
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
    # DAMOCLES hard 72-char line limit: one over-long line (e.g. a long
    # geocoded reach name in a comment or the TITLE) derails the parser into
    # "KEY-WORD ... IS UNKNOWN" on a LATER, valid line. Live-hit 2026-07-18:
    # 'longview_cowlitz_county_washington_98632_united_' made an 86-char
    # comment + ~80-char TITLE and DAMOCLES blamed 'GEOMETRY FILE' at line 10.
    # Comments are safely sliced; the quoted TITLE is shortened keeping quotes.
    lines = []
    for ln in cas.splitlines():
        if len(ln) <= 72:
            lines.append(ln)
        elif ln.startswith("/"):
            lines.append(ln[:72])
        elif ln.startswith("TITLE"):
            lines.append(f"TITLE : '{cfg.name[:40]} DYE PULSE'"[:72])
        else:
            lines.append(ln)  # data lines are worker-generated and short
    cas = "\n".join(lines) + "\n"
    over = [ln for ln in lines if len(ln) > 72]
    if over:
        LOG.warning("cas lines still >72 chars after clamp: %r", over[:3])
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
