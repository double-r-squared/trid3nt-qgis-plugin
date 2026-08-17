"""SCHISM shortlist proofs (docs/proof/templates/):
  Row 1 schism_coupled_waves (parametric JONSWAP): nearshore Hs over Esri imagery
    at the Duck NC FRF + a KNOB-demonstration cross-shore chart (storm Hs vs calm
    Hs, with wave setup) + the FRF mesh.
  Row 2 schism_baroclinic_circulation: surface + bottom salinity over Esri imagery
    (Galveston Bay footprint) + a stratification chart + the estuary mesh.

Usage: set -a; source .env.local; set +a
       python scripts/proof_schism_shortlist.py <storm_run_id> <calm_run_id> <baroclinic_run_id>
The wave Duck mesh is a local FRF projection; its lon/lat twin (hgrid.ll, same node
order as out2d) georeferences the field honestly for the Esri render.
"""
import io
import math
import os
import sys

import boto3
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.tri as mtri
import numpy as np
import requests
from matplotlib.colors import Normalize
from matplotlib.patches import Rectangle
from netCDF4 import Dataset
from PIL import Image
from pyproj import Transformer
from scipy.spatial import cKDTree, Delaunay

# Baroclinic showcase IC parameters (match the composer defaults) so the proof
# can subtract the initial linear salinity gradient and show CIRCULATION.
_EST_BBOX = (-94.95, 29.35, -94.70, 29.75)   # default Galveston Bay footprint
_OCEAN_SIDE = "south"
_OCEAN_SAL = 33.0
_RIVER_SAL = 0.0

TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
OUT = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
FIX = "/home/nate/Documents/trid3nt-local/workers/schism/fixtures/wwm_duck"
TMP = "/tmp/schism_proof"
os.makedirs(TMP, exist_ok=True)
TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
S3 = boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"])
RUNS = os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")


def _tile_xy(lon, lat, z):
    n = 2 ** z
    return ((lon + 180.0) / 360.0 * n,
            (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)


def _tile_bounds(x, y, z):
    n = 2 ** z
    def merc(tx, ty):
        lon = tx / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        return TO_3857.transform(lon, lat)
    x0, y0 = merc(x, y + 1); x1, y1 = merc(x + 1, y)
    return x0, y0, x1, y1


def fetch_basemap(w, s, e, n, zoom):
    x0f, y1f = _tile_xy(w, s, zoom); x1f, y0f = _tile_xy(e, n, zoom)
    xs = list(range(int(x0f), int(x1f) + 1)); ys = list(range(int(y0f), int(y1f) + 1))
    mosaic = Image.new("RGB", (256 * len(xs), 256 * len(ys)))
    sess = requests.Session()
    for j, ty in enumerate(ys):
        for i, tx in enumerate(xs):
            r = sess.get(TILE_URL.format(z=zoom, y=ty, x=tx), timeout=30); r.raise_for_status()
            mosaic.paste(Image.open(io.BytesIO(r.content)).convert("RGB"), (i * 256, j * 256))
    wm0, _, _, _ = _tile_bounds(min(xs), max(ys), zoom)
    _, sm0, _, _ = _tile_bounds(min(xs), max(ys), zoom)
    _, _, em1, nm1 = _tile_bounds(max(xs), min(ys), zoom)
    return np.asarray(mosaic), (wm0, em1, sm0, nm1)


def dl(run_id, rel, suffix):
    p = os.path.join(TMP, f"{run_id}_{suffix}")
    S3.download_file(RUNS, f"{run_id}/{rel}", p)
    return p


def read_gr3_nodes(path):
    lines = open(path).read().splitlines()
    ne, nn = (int(v) for v in lines[1].split()[:2])
    xy = np.array([[float(lines[2 + i].split()[1]), float(lines[2 + i].split()[2])]
                   for i in range(nn)], dtype=float)
    return xy


def rasterize(lon, lat, vals, w, h, bbox, mask_tri=None):
    minx, miny, maxx, maxy = bbox
    xs = minx + (np.arange(w) + 0.5) * (maxx - minx) / w
    ys = maxy - (np.arange(h) + 0.5) * (maxy - miny) / h
    gx, gy = np.meshgrid(xs, ys)
    q = np.column_stack([gx.ravel(), gy.ravel()])
    pts = np.column_stack([lon, lat])
    tree = cKDTree(pts)
    _, idx = tree.query(q, k=1)
    grid = vals[idx].reshape(h, w)
    # Mask to the modeled domain. Prefer the REAL element triangulation (a
    # shoreline-clipped mesh) so no cell paints land in a concavity; fall back to
    # the node convex hull when no connectivity is supplied.
    if mask_tri is not None:
        try:
            finder = mask_tri.get_trifinder()
            inside = finder(gx.ravel(), gy.ravel()) >= 0
            return np.where(inside.reshape(h, w), grid, np.nan)
        except Exception:
            pass
    try:
        hull = Delaunay(pts)
        inside = hull.find_simplex(q) >= 0
        grid = np.where(inside.reshape(h, w), grid, np.nan)
    except Exception:
        pass
    return grid


def render_map(lon, lat, vals, title, cbar, cmap, out_name, zoom=12, pad=0.25, vmin=0.0, vmax=None,
               mask_tri=None):
    lw, le = float(lon.min()), float(lon.max())
    ls, ln = float(lat.min()), float(lat.max())
    px, py = (le - lw) * pad, (ln - ls) * pad
    basemap, bm = fetch_basemap(lw - px, ls - py, le + px, ln + py, zoom)
    W = 700; H = int(W * (ln - ls) / max(le - lw, 1e-9))
    grid = rasterize(lon, lat, vals, W, H, (lw, ls, le, ln), mask_tri=mask_tri)
    if vmax is None:
        vmax = float(np.nanpercentile(vals, 99))
    # to 3857 for overlay
    gx0, gy0 = TO_3857.transform(lw, ls); gx1, gy1 = TO_3857.transform(le, ln)
    fig, ax = plt.subplots(figsize=(9, 8.5), dpi=115)
    ax.imshow(basemap, extent=bm, origin="upper")
    im = ax.imshow(grid, extent=(gx0, gx1, gy0, gy1), origin="upper", cmap=cmap,
                   norm=Normalize(vmin, vmax), alpha=0.82, zorder=3)
    ax.add_patch(Rectangle((gx0, gy0), gx1 - gx0, gy1 - gy0, fill=False,
                           edgecolor="white", linewidth=1.6, zorder=4))
    wx0, _ = TO_3857.transform(lw - px, ls); wx1, _ = TO_3857.transform(le + px, ls)
    _, wy0 = TO_3857.transform(lw, ls - py); _, wy1 = TO_3857.transform(lw, ln + py)
    ax.set_xlim(wx0, wx1); ax.set_ylim(wy0, wy1)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title(title, fontsize=11)
    cb = fig.colorbar(im, ax=ax, shrink=0.72, pad=0.02); cb.set_label(cbar)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, out_name), bbox_inches="tight")
    plt.close(fig)
    print("wrote", out_name, "vmax=%.3f" % vmax)


def render_mesh(lon, lat, title, out_name, zoom=11, pad=0.15, tris=None):
    # raw grid one color; white box = AOI only. Triangulate on the REAL element
    # connectivity when supplied (a shoreline-clipped mesh's concavities would
    # otherwise be bridged by an unconstrained Delaunay into spurious fans).
    lw, le = float(lon.min()), float(lon.max()); ls, ln = float(lat.min()), float(lat.max())
    px, py = (le - lw) * pad, (ln - ls) * pad
    tri = mtri.Triangulation(lon, lat, triangles=tris) if tris is not None \
        else mtri.Triangulation(lon, lat)
    fig, ax = plt.subplots(figsize=(8, 7.5), dpi=115)
    ax.triplot(tri, color="#2b8cbe", linewidth=0.4)
    ax.add_patch(Rectangle((lw, ls), le - lw, ln - ls, fill=False, edgecolor="white",
                           linewidth=1.4, zorder=5))
    ax.set_xlim(lw - px, le + px); ax.set_ylim(ls - py, ln + py)
    ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
    ax.set_title(title, fontsize=11)
    ax.set_facecolor("#0b1a2b")
    fig.tight_layout(); fig.savefig(os.path.join(OUT, out_name), bbox_inches="tight")
    plt.close(fig); print("wrote", out_name)


def read_out2d_var(path, cands):
    with Dataset(path) as ds:
        def first(cs):
            for c in cs:
                if c in ds.variables:
                    return c
            return None
        xk = first(("SCHISM_hgrid_node_x", "x", "longitude"))
        yk = first(("SCHISM_hgrid_node_y", "y", "latitude"))
        vk = first(cands)
        x = np.asarray(ds.variables[xk][:], float).ravel()
        y = np.asarray(ds.variables[yk][:], float).ravel()
        v = np.asarray(ds.variables[vk][:], float)
    return x, y, v


def row1(storm_id, calm_id):
    llxy = read_gr3_nodes(os.path.join(FIX, "hgrid.ll"))    # lon/lat, node order == out2d
    frfxy = read_gr3_nodes(os.path.join(FIX, "hgrid.gr3"))  # local FRF (xFRF metres)
    # storm Hs field
    so = dl(storm_id, "outputs/out2d_1.nc", "out2d.nc")
    _, _, hs_s = read_out2d_var(so, ("sigWaveHeight", "WWM_1"))
    hs_s = np.where(np.isfinite(hs_s) & (hs_s >= 0) & (hs_s < 1e4), hs_s, np.nan)
    hs_s_max = np.nanmax(hs_s, axis=0)
    n = min(len(llxy), len(hs_s_max))
    lon, lat = llxy[:n, 0], llxy[:n, 1]
    fin = np.isfinite(hs_s_max[:n])
    render_map(lon[fin], lat[fin], hs_s_max[:n][fin],
               "schism_coupled_waves -- nearshore Hs, PARAMETRIC JONSWAP Hs=4.0 m (Duck NC FRF)",
               "Max significant wave height Hs (m)", "cool",
               "schism_coupled_waves_parametric_hs.png", zoom=14, vmax=None)
    render_mesh(lon, lat, "schism_coupled_waves -- Duck NC FRF unstructured mesh (lon/lat)",
                "schism_coupled_waves_parametric_mesh.png", zoom=14)
    # calm Hs field for the knob chart
    co = dl(calm_id, "outputs/out2d_1.nc", "out2d.nc")
    _, _, hs_c = read_out2d_var(co, ("sigWaveHeight", "WWM_1"))
    hs_c = np.where(np.isfinite(hs_c) & (hs_c >= 0) & (hs_c < 1e4), hs_c, np.nan)
    hs_c_max = np.nanmax(hs_c, axis=0)
    # cross-shore profiles: bin Hs-max by xFRF (cross-shore metre coordinate)
    xfrf = frfxy[:n, 0]
    order = np.argsort(xfrf)
    def prof(hmax):
        h = hmax[:n][order]; x = xfrf[order]
        bins = np.linspace(x.min(), x.max(), 40)
        idx = np.clip(np.digitize(x, bins), 1, len(bins) - 1)
        xm = 0.5 * (bins[:-1] + bins[1:])
        ym = np.array([np.nanmean(h[idx == b]) for b in range(1, len(bins))])
        return xm, ym
    xs, ys_s = prof(hs_s_max); _, ys_c = prof(hs_c_max)
    fig, ax = plt.subplots(figsize=(6.0, 2.2), dpi=100)
    ax.plot(xs, ys_s, "-", color="#08519c", lw=1.4, label="Hs=4.0 m (storm)")
    ax.plot(xs, ys_c, "-", color="#6baed6", lw=1.4, label="Hs=1.5 m (calm)")
    ax.set_xlabel("cross-shore position xFRF (m)", fontsize=7)
    ax.set_ylabel("Hs (m)", fontsize=7)
    ax.tick_params(labelsize=6); ax.legend(fontsize=6, loc="upper left")
    ax.grid(True, alpha=0.25)
    fig.text(0.005, 0.005,
             "schism_coupled_waves parametric JONSWAP boundary knob: nearshore cross-shore Hs "
             "for two prescribed offshore sea states on the Duck FRF geometry. The nearshore "
             "field scales with the offshore forcing (the knob is honored); both shoal toward the beach.",
             fontsize=4.6, color="0.35")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.savefig(os.path.join(OUT, "schism_coupled_waves_parametric_knob_chart.png"), dpi=200)
    plt.close(fig)
    print("wrote schism_coupled_waves_parametric_knob_chart.png",
          "storm Hs_max=%.2f calm Hs_max=%.2f" % (np.nanmax(hs_s_max), np.nanmax(hs_c_max)))


def row2(baro_id):
    salt = dl(baro_id, "outputs/salinity_1.nc", "salinity.nc")
    o2 = dl(baro_id, "outputs/out2d_1.nc", "out2d.nc")
    with Dataset(o2) as ds:
        lon = np.asarray(ds.variables["SCHISM_hgrid_node_x"][:], float).ravel()
        lat = np.asarray(ds.variables["SCHISM_hgrid_node_y"][:], float).ravel()
        # REAL element connectivity (shoreline-clipped mesh): SCHISM face_nodes are
        # 1-based, quads padded with -1; all our cells are triangles (cols 0:3).
        fnodes = np.ma.filled(np.asarray(ds.variables["SCHISM_hgrid_face_nodes"][:]), -1)
        tris0 = (fnodes[:, :3] - 1).astype(int)
    mask_tri = mtri.Triangulation(lon, lat, triangles=tris0)
    with Dataset(salt) as ds:
        s = np.asarray(ds.variables["salinity"][:], float)
    s = np.where(np.isfinite(s) & (s >= 0) & (s < 100), s, np.nan)
    node_layer = np.nanmean(s[s.shape[0] // 2:], axis=0)  # (node, layer) spun-up mean
    nn = node_layer.shape[0]
    surf = np.full(nn, np.nan); bot = np.full(nn, np.nan)
    for i in range(nn):
        v = np.where(np.isfinite(node_layer[i]))[0]
        if v.size:
            bot[i] = node_layer[i][v[0]]; surf[i] = node_layer[i][v[-1]]
    n = min(len(lon), nn)
    lon, lat, surf, bot = lon[:n], lat[:n], surf[:n], bot[:n]
    fin = np.isfinite(surf) & np.isfinite(bot)
    smax = float(np.nanmax([np.nanmax(surf[fin]), np.nanmax(bot[fin])]))
    render_map(lon[fin], lat[fin], surf[fin],
               "schism_baroclinic_circulation -- SURFACE salinity (Galveston Bay footprint, 3D baroclinic)",
               "Surface salinity (psu)", "viridis",
               "schism_baroclinic_circulation_surface_salinity.png", zoom=9, vmin=0, vmax=smax,
               mask_tri=mask_tri)
    render_map(lon[fin], lat[fin], bot[fin],
               "schism_baroclinic_circulation -- BOTTOM salinity (salt wedge intrusion)",
               "Bottom salinity (psu)", "viridis",
               "schism_baroclinic_circulation_bottom_salinity.png", zoom=9, vmin=0, vmax=smax,
               mask_tri=mask_tri)
    render_mesh(lon, lat, "schism_baroclinic_circulation -- shoreline-clipped estuary mesh (Galveston Bay)",
                "schism_baroclinic_circulation_mesh.png", zoom=9, tris=tris0)
    # stratification chart: surface vs bottom salinity along the estuary axis (latitude)
    order = np.argsort(lat[fin])
    la = lat[fin][order]; su = surf[fin][order]; bo = bot[fin][order]
    bins = np.linspace(la.min(), la.max(), 30)
    idx = np.clip(np.digitize(la, bins), 1, len(bins) - 1)
    lm = 0.5 * (bins[:-1] + bins[1:])
    sm = np.array([np.nanmean(su[idx == b]) for b in range(1, len(bins))])
    bm = np.array([np.nanmean(bo[idx == b]) for b in range(1, len(bins))])
    fig, ax = plt.subplots(figsize=(6.0, 2.2), dpi=100)
    ax.plot(lm, sm, "-", color="#1f9e89", lw=1.4, label="surface salinity")
    ax.plot(lm, bm, "-", color="#440154", lw=1.4, label="bottom salinity")
    ax.fill_between(lm, sm, bm, color="#35b779", alpha=0.18, label="stratification")
    ax.set_xlabel("latitude (river N -> ocean S is left<-right by axis)", fontsize=7)
    ax.set_ylabel("salinity (psu)", fontsize=7)
    ax.tick_params(labelsize=6); ax.legend(fontsize=6, loc="upper left")
    ax.grid(True, alpha=0.25)
    fig.text(0.005, 0.005,
             "schism_baroclinic_circulation: surface vs bottom salinity along the estuary axis. "
             "The bottom stays saltier than the surface (a stratified salt wedge) -- the 3D "
             "baroclinic solve produces gravitational estuarine circulation. Coarse demo geometry.",
             fontsize=4.6, color="0.35")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.savefig(os.path.join(OUT, "schism_baroclinic_circulation_stratification_chart.png"), dpi=200)
    plt.close(fig)
    print("wrote schism_baroclinic_circulation_stratification_chart.png",
          "strat_max=%.2f" % float(np.nanmax((bot - surf)[fin])))

    # ---- change-from-IC map: CIRCULATION, not the initial condition ---------
    # The run starts from a LINEAR salinity gradient (fresh landward -> salty
    # seaward). After multi-day river + tidal forcing, the density-driven
    # (gravitational) circulation restructures it: the surface freshens where the
    # river plume + estuarine outflow ride over the wedge, the seaward end mixes.
    # surface(spun-up) MINUS the initial gradient makes that dynamics visible
    # (fresher-than-IC = blue, saltier-than-IC = red) -- if this were the IC
    # echoed back it would be flat zero everywhere.
    from matplotlib.colors import TwoSlopeNorm
    lon0, lat0, lon1, lat1 = _EST_BBOX
    if _OCEAN_SIDE in ("south", "north"):
        fr = (lat - lat0) / (lat1 - lat0)
        if _OCEAN_SIDE == "south":
            fr = 1.0 - fr
    else:
        fr = (lon - lon0) / (lon1 - lon0)
        if _OCEAN_SIDE == "west":
            fr = 1.0 - fr
    ic_surf = _RIVER_SAL + (_OCEAN_SAL - _RIVER_SAL) * np.clip(fr, 0.0, 1.0)
    delta = surf - ic_surf
    dfin = fin & np.isfinite(delta)
    dv = float(np.nanpercentile(np.abs(delta[dfin]), 98)) or 1.0
    lw, le = float(lon[dfin].min()), float(lon[dfin].max())
    ls, ln = float(lat[dfin].min()), float(lat[dfin].max())
    px, py = (le - lw) * 0.25, (ln - ls) * 0.25
    basemap, bm = fetch_basemap(lw - px, ls - py, le + px, ln + py, 9)
    W = 700; H = int(W * (ln - ls) / max(le - lw, 1e-9))
    grid = rasterize(lon[dfin], lat[dfin], delta[dfin], W, H, (lw, ls, le, ln), mask_tri=mask_tri)
    gx0, gy0 = TO_3857.transform(lw, ls); gx1, gy1 = TO_3857.transform(le, ln)
    fig, ax = plt.subplots(figsize=(9, 8.5), dpi=115)
    ax.imshow(basemap, extent=bm, origin="upper")
    im = ax.imshow(grid, extent=(gx0, gx1, gy0, gy1), origin="upper", cmap="RdBu_r",
                   norm=TwoSlopeNorm(vcenter=0.0, vmin=-dv, vmax=dv), alpha=0.85, zorder=3)
    ax.add_patch(Rectangle((gx0, gy0), gx1 - gx0, gy1 - gy0, fill=False,
                           edgecolor="white", linewidth=1.6, zorder=4))
    wx0, _ = TO_3857.transform(lw - px, ls); wx1, _ = TO_3857.transform(le + px, ls)
    _, wy0 = TO_3857.transform(lw, ls - py); _, wy1 = TO_3857.transform(lw, ln + py)
    ax.set_xlim(wx0, wx1); ax.set_ylim(wy0, wy1); ax.set_xticks([]); ax.set_yticks([])
    ax.set_title("schism_baroclinic_circulation -- surface salinity CHANGE from the "
                 "initial gradient\n(density-driven circulation restructures the field; "
                 "blue=fresher, red=saltier)", fontsize=10)
    cb = fig.colorbar(im, ax=ax, shrink=0.72, pad=0.02)
    cb.set_label("surface salinity minus initial condition (psu)")
    fig.text(0.01, 0.01,
             f"Galveston Bay footprint, shoreline-clipped mesh. Spun-up surface salinity "
             f"minus the linear IC (fresh river -> {_OCEAN_SAL:.0f} psu ocean). A non-zero "
             f"field = the 3D baroclinic solve moved salt (gravitational estuarine "
             f"circulation + tidal exchange), NOT the IC echoed back. Pinned scale "
             f"+-{dv:.2f} psu. Coarse demo geometry (ADR 0191).",
             fontsize=7, color="0.35", wrap=True)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "schism_baroclinic_circulation_salinity_change.png"),
                bbox_inches="tight")
    plt.close(fig)
    print("wrote schism_baroclinic_circulation_salinity_change.png",
          "abs_delta_p98=%.2f max_fresh=%.2f max_salt=%.2f"
          % (dv, float(np.nanmin(delta[dfin])), float(np.nanmax(delta[dfin]))))


if __name__ == "__main__":
    storm, calm, baro = sys.argv[1], sys.argv[2], sys.argv[3]
    if storm != "-":
        row1(storm, calm)
    if baro != "-":
        row2(baro)
