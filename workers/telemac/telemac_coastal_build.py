"""TELEMAC-2D COASTAL (open-water / tidal-surge inundation) worker payload.

Builds a coastal open-water TELEMAC-2D domain the same family way the TOMAWAC /
ARTEMIS wave modules build a lake / harbour grid (``tomawac_build.build_grid`` +
``_bed_cog``): a regular UTM grid over a coastal bbox, real NOAA topobathy at the
nodes, ONE seaward OPEN (liquid, free-surface-imposed) boundary edge and closed
(solid) land edges. The seaward boundary water level is driven in TIME by a
LIQUID BOUNDARIES FILE authored from a NOAA CO-OPS tide/surge series
(``fetch_noaa_coops_tides``) or a GTSM series -- so a storm-surge series floods
low land the calm astronomical tide leaves dry (SAINT-VENANT + TIDAL FLATS
wetting/drying).

Format authority (pinned against the in-image v9.0 sources, NEVER guessed):
  * LIQUID BOUNDARIES FILE keyword + T2DIMP file slot: telemac2d.dico
    (``NOM1 = 'LIQUID BOUNDARIES FILE'``, INDEX 38, SUBMIT ...;ASC;LIT;PARAL).
  * File grammar: sources/telemac2d/read_fic_frliq.f -- first non-``#`` line is
    the column NAMES, first name MUST be ``T``; the SECOND line (units/names) is
    skipped; then free-format ``T value...`` data rows with STRICTLY increasing
    time; ``#`` comment lines allowed anywhere; an out-of-range time aborts, so
    the series must bracket [0, DURATION].
  * Free-surface column name: sources/telemac2d/sl.f builds ``FCT='SL(<i>)'``
    (e.g. ``SL(1)``) per liquid-boundary index I and looks it up in the file;
    discharge would be ``Q(<i>)`` (sources/telemac2d/q.f). With a single ocean
    boundary the column is ``SL(1)``.

VERTICAL DATUM. The bed and the boundary series are on DIFFERENT references and
the composer reconciles them: DEM_all is a MIXED-datum mosaic whose served NCEI
1/9 arc-sec CUDEM tiles declare NAVD 88 over US coasts (its other components
declare MHW, EGM 2008 or Sea Level), while a CO-OPS series is reported on a tidal
datum. ``datum_offset_m`` carries that reconciliation; a zero one puts the whole
water column high by the difference and cold-starts land wet.

Parser version marker: ``coastal-tidal-3`` (a new coastal build path, distinct
from the telemac-reach parser).  ASCII only; imports NO agent code; runs only
inside the worker image.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

LOG = logging.getLogger("telemac_coastal")

#: worker-image / behavior provenance marker (mirrors _TOMAWAC_PARSER_VERSION).
#: THE ONE STAMP for this leg - the entrypoint imports it rather than declaring a
#: second, because two stamps for one parser gave a manual provenance check two
#: answers depending on which file it read.
#: -2 added the output_interval_min cadence lever (ADR 0283).
#: -4 fills the geometry SELAFIN's X-ORIGIN / Y-ORIGIN header so the published
#: result mesh lands on the domain rather than at the UTM false origin, and
#: echoes the origin in the metrics.
COASTAL_PARSER_VERSION = "coastal-tidal-4"

#: NOAA NGDC DEM_all topobathy mosaic ImageServer -- the SAME real-bathymetry
#: source the TOMAWAC lake path samples (negative below the DEM's own vertical
#: datum = bathymetry, positive = land topo; over US coasts that datum is
#: NAVD 88); covers US coastal waters + estuaries at node lon/lat.
_NOAA_DEM_ALL_URL = (
    "https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_all/"
    "ImageServer/exportImage"
)
_UA = "trid3nt-local-coastal (agent@trid3nt.dev)"

#: grid guardrails (mirror the TOMAWAC lake grid budget).
GRID_H_FLOOR_M = 20.0
GRID_NODE_CAP = 60_000

#: basename of the authored LIQUID BOUNDARIES FILE (T2DIMP; staged next to the
#: .cas, referenced by basename, uploaded as forcing evidence).
LIQBND_FILENAME = "coastal_liquid_bnd.txt"


class CoastalInputError(RuntimeError):
    """Typed manifest / geometry gate (error_code + message)."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


@dataclass
class CoastalConfig:
    """Coastal tidal/surge inundation config (strict-unknown-field gated upstream)."""

    name: str = "coastal"
    #: (min_lon, min_lat, max_lon, max_lat) EPSG:4326 -- REQUIRED.
    bbox: tuple | None = None
    #: bed source: "noaa_demall" (real topobathy) | "synthetic" (analytic beach, tests).
    bathy_source: str = "noaa_demall"
    #: target node spacing (m); 0 -> auto (span / 120, node-capped).
    target_resolution_m: float = 0.0
    #: seaward boundary edge: "auto" (deepest-mean bbox edge) | N | S | E | W.
    ocean_edge: str = "auto"

    #: water-level forcing series as [[t_seconds, sl_meters], ...] at the ocean
    #: boundary. Authored from fetch_noaa_coops_tides / fetch_gtsm_tide_surge by
    #: the composer (time re-based to 0 at the sim start). REQUIRED.
    water_level_series: list | None = None
    #: series vertical datum label (provenance only).
    series_datum: str = "MLLW"
    #: added to every series value to reconcile the tide datum (e.g. MLLW) with
    #: the DEM datum (DEM_all over US coasts = NAVD 88); labeled knob, never invented.
    datum_offset_m: float = 0.0

    #: initial constant free-surface elevation (m, DEM datum). None -> series[0].
    init_wl_m: float | None = None
    #: simulation duration (s). None -> series time span.
    duration_s: float | None = None
    time_step_s: float = 5.0
    #: graphic printout period (timesteps). None -> ~40 frames across the run.
    graphic_period: int | None = None
    #: universal map-frame cadence lever (minutes between frames, ADR 0283). None
    #: keeps the computed ~40-frame default; set -> graphic_period is derived from
    #: it (interval_s / time_step_s) below. graphic_period, if given, still wins.
    output_interval_min: float | None = None

    #: bottom-friction law/coefficient (3 = Strickler; ~40 = mixed sand/marsh).
    friction_law: int = 3
    friction_coefficient: float = 40.0
    #: constant wind (optional surge set-up); 0 -> no WIND block.
    wind_speed_mps: float = 0.0
    wind_dir_from_deg: float = 0.0

    #: synthetic-bathy knobs (bathy_source="synthetic"): a plane beach sloping
    #: from +topo_max_m (landward) to -depth_max_m (seaward) across the ocean edge.
    syn_depth_max_m: float = 8.0
    syn_topo_max_m: float = 4.0

    #: dry-node fill for off-DEM / NaN nodes (m, above any tide -> permanent wall).
    dry_fill_m: float = 50.0


# ---------------------------------------------------------------------------
# 1. Regular coastal grid (tomawac_build.build_grid family) + real topobathy bed
# ---------------------------------------------------------------------------
def _bbox_utm_epsg(bbox) -> int:
    lon = 0.5 * (bbox[0] + bbox[2])
    lat = 0.5 * (bbox[1] + bbox[3])
    zone = int((lon + 180.0) // 6.0) + 1
    return (32600 if lat >= 0 else 32700) + zone


def _build_grid(Lx: float, Ly: float, dx: float):
    """Structured triangular grid; node n = i*ny + j; CCW ring + IPOBO ranks.

    Byte-family-identical to ``tomawac_build.build_grid`` (shared front pipeline),
    minus the depth_fn (the bed is sampled at node lon/lat separately)."""
    nx = int(round(Lx / dx)) + 1
    ny = int(round(Ly / dx)) + 1
    xs = np.linspace(0.0, Lx, nx)
    ys = np.linspace(0.0, Ly, ny)
    X = np.repeat(xs, ny)
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
    for i in range(nx - 1):          # bottom edge  j=0     (+X)  -> "S"
        ring.append(nid(i, 0))
    for j in range(ny - 1):          # right  edge  i=nx-1  (+Y)  -> "E"
        ring.append(nid(nx - 1, j))
    for i in range(nx - 1, 0, -1):   # top    edge  j=ny-1  (-X)  -> "N"
        ring.append(nid(i, ny - 1))
    for j in range(ny - 1, 0, -1):   # left   edge  i=0     (-Y)  -> "W"
        ring.append(nid(0, j))
    ring = np.array(ring, dtype=np.int32)
    nptfr = len(ring)

    ipob = np.zeros(npoin, dtype=np.int32)
    for rank, n in enumerate(ring, start=1):
        ipob[n] = rank

    return dict(X=X.astype(np.float64), Y=Y.astype(np.float64), ikle=ikle,
                ipob=ipob, ring=ring, nptfr=nptfr, npoin=npoin, nx=nx, ny=ny,
                xs=xs, ys=ys, Lx=Lx, Ly=Ly, dx=dx)


def fetch_demall_bed(lon, lat, bbox, timeout: float = 180.0):
    """Sample NOAA DEM_all topobathy at node lon/lat (m, on the DEM's own datum - NAVD 88 for the NCEI CUDEM tiles that serve US coasts).

    exportImage returns a bbox F32 GeoTIFF; a node off-coverage / NoData is NaN.
    Same call family as ``tomawac_build.fetch_greatlakes_bathy``."""
    import requests
    from rasterio.io import MemoryFile

    ncols = int(np.clip(round((bbox[2] - bbox[0]) * 1800.0), 64, 3000))
    nrows = int(np.clip(round((bbox[3] - bbox[1]) * 1800.0), 64, 3000))
    resp = requests.get(_NOAA_DEM_ALL_URL, params={
        "bbox": ",".join(str(v) for v in bbox),
        "bboxSR": "4326", "imageSR": "4326",
        "size": f"{ncols},{nrows}",
        "format": "tiff", "pixelType": "F32", "f": "image",
    }, headers={"User-Agent": _UA}, timeout=timeout)
    resp.raise_for_status()
    body = resp.content
    if body[:4] not in (b"II*\x00", b"MM\x00*"):
        raise CoastalInputError(
            "COASTAL_BATHY_UNAVAILABLE",
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


def _synthetic_bed(mesh, cfg: CoastalConfig, ocean_edge: str):
    """Analytic plane beach: +topo_max landward -> -depth_max at the ocean edge.

    Deterministic bed for offline/CI proofs (no network). The bed slopes down
    toward ``ocean_edge`` so the seaward boundary is submerged and the landward
    third is dry above any tide -- the tidal series then floods the mid beach."""
    X, Y = mesh["X"], mesh["Y"]
    Lx, Ly = mesh["Lx"], mesh["Ly"]
    if ocean_edge == "S":
        s = Y / max(Ly, 1e-9)               # 0 at ocean edge (y=0) -> 1 landward
    elif ocean_edge == "N":
        s = 1.0 - Y / max(Ly, 1e-9)
    elif ocean_edge == "W":
        s = X / max(Lx, 1e-9)
    else:  # "E"
        s = 1.0 - X / max(Lx, 1e-9)
    depth = float(cfg.syn_depth_max_m)
    topo = float(cfg.syn_topo_max_m)
    return (-depth + (depth + topo) * s).astype(np.float64)


def _classify_ocean_edge(mesh, bed, requested: str) -> str:
    """Pick the seaward bbox edge: the requested one, or (auto) the deepest mean."""
    req = (requested or "auto").upper()
    if req in ("N", "S", "E", "W"):
        return req
    X, Y = mesh["X"], mesh["Y"]
    Lx, Ly, dx = mesh["Lx"], mesh["Ly"], mesh["dx"]
    edges = {
        "S": Y <= dx * 0.5,
        "N": Y >= Ly - dx * 0.5,
        "W": X <= dx * 0.5,
        "E": X >= Lx - dx * 0.5,
    }
    means = {}
    for k, m in edges.items():
        vals = bed[m]
        vals = vals[np.isfinite(vals)]
        means[k] = float(np.mean(vals)) if vals.size else np.inf
    # deepest = most negative mean bed
    return min(means, key=means.get)


def _edge_mask(mesh, ocean_edge: str) -> np.ndarray:
    """Boolean over ALL nodes: True where a node lies on the ocean bbox edge."""
    X, Y = mesh["X"], mesh["Y"]
    Lx, Ly, dx = mesh["Lx"], mesh["Ly"], mesh["dx"]
    if ocean_edge == "S":
        return Y <= dx * 0.5
    if ocean_edge == "N":
        return Y >= Ly - dx * 0.5
    if ocean_edge == "W":
        return X <= dx * 0.5
    return X >= Lx - dx * 0.5


def build_coastal_mesh(cfg: CoastalConfig):
    """Regular UTM grid over the coastal bbox with a real (or synthetic) bed and
    ONE seaward OPEN boundary edge; the rest solid. Returns (mesh, meta)."""
    from pyproj import Transformer

    bbox = cfg.bbox
    if not (bbox and len(bbox) == 4):
        raise CoastalInputError(
            "COASTAL_PARAMS_INVALID",
            "coastal domain needs a 4-value bbox (min_lon,min_lat,max_lon,max_lat); "
            f"got {bbox!r}.")
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

    mesh = _build_grid(Lx, Ly, dx)
    # The grid is laid LOCAL (node 0 at 0,0), so the domain's SW corner in UTM is
    # what makes it a place. It is kept on the mesh (not just used and dropped)
    # because the SELAFIN header carries it: X-ORIGIN / Y-ORIGIN are INTEGER
    # metres in the Fortran (read_mesh_info.f), so they round here.
    mesh["x_origin_m"] = int(round(min(x0, x1)))
    mesh["y_origin_m"] = int(round(min(y0, y1)))
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    xabs = mesh["X"] + min(x0, x1)
    yabs = mesh["Y"] + min(y0, y1)
    lon, lat = back.transform(xabs, yabs)
    mesh["bed_lon"] = np.asarray(lon, dtype=float)
    mesh["bed_lat"] = np.asarray(lat, dtype=float)

    src = str(cfg.bathy_source).lower()
    if src in ("noaa_demall", "demall", "noaa", "topobathy"):
        raw = fetch_demall_bed(np.asarray(lon), np.asarray(lat), bbox)
        wet = np.isfinite(raw) & (raw < 0.0)
        n_wet = int(wet.sum())
        if n_wet < 0.05 * raw.size:
            raise CoastalInputError(
                "COASTAL_BATHY_UNAVAILABLE",
                f"NOAA DEM_all covered only {n_wet}/{raw.size} nodes below the DEM datum over "
                f"{bbox} -- the AOI is essentially all land. Pick a bbox spanning "
                "the shoreline (open water on one side, low land on the other).")
        ocean_edge = _classify_ocean_edge(mesh, raw, cfg.ocean_edge)
        Z = np.where(np.isfinite(raw), raw, float(cfg.dry_fill_m)).astype(np.float64)
        mesh["bed_raw"] = np.where(wet, raw, np.nan).astype(float)
        depth_max = round(float(-np.nanmin(raw)), 2)
        topo_max = round(float(np.nanmax(raw)), 2)
    elif src == "synthetic":
        # synthetic bed has no real bathymetry to auto-pick from; default S.
        ocean_edge = cfg.ocean_edge.upper() if cfg.ocean_edge and \
            cfg.ocean_edge.upper() in ("N", "S", "E", "W") else "S"
        Z = _synthetic_bed(mesh, cfg, ocean_edge)
        wet = Z < 0.0
        mesh["bed_raw"] = np.where(wet, Z, np.nan).astype(float)
        depth_max = round(float(-Z.min()), 2)
        topo_max = round(float(Z.max()), 2)
    else:
        raise CoastalInputError(
            "COASTAL_PARAMS_INVALID",
            f"unknown bathy_source {cfg.bathy_source!r}; use 'noaa_demall' or 'synthetic'.")

    mesh["Z"] = Z

    # boundary codes: ocean edge -> KENT (free-surface imposed), rest -> solid wall.
    ocean_nodes = _edge_mask(mesh, ocean_edge)
    ring = mesh["ring"]
    lihbor = np.full(mesh["nptfr"], 2, dtype=np.int32)
    liubor = np.full(mesh["nptfr"], 2, dtype=np.int32)
    livbor = np.full(mesh["nptfr"], 2, dtype=np.int32)
    litbor = np.full(mesh["nptfr"], 2, dtype=np.int32)
    cls = []
    for k in range(mesh["nptfr"]):
        node0 = int(ring[k])
        if ocean_nodes[node0]:
            lihbor[k] = 5   # prescribed free surface (SL(i) from LIQUID BND FILE)
            liubor[k] = 4   # free velocity
            livbor[k] = 4
            litbor[k] = 4
            cls.append("ocean")
        else:
            cls.append("land")
    n_ocean = int(sum(1 for c in cls if c == "ocean"))
    if n_ocean < 2:
        raise CoastalInputError(
            "COASTAL_OCEAN_BOUNDARY_EMPTY",
            f"seaward edge {ocean_edge!r} carries {n_ocean} boundary nodes -- the "
            "open ocean segment is degenerate; widen the bbox toward open water.")
    mesh["lihbor"] = lihbor
    mesh["liubor"] = liubor
    mesh["livbor"] = livbor
    mesh["litbor"] = litbor
    mesh["cls"] = cls
    # ocean-node global indices (for the liquid-boundary count / listing sanity).
    mesh["ocean_ring_ranks"] = np.where([c == "ocean" for c in cls])[0] + 1

    # The bbox is ECHOED because the agent-side reader has to add this exact SW
    # corner back to the local mesh metres. Reconstructing it from the request
    # rather than from what was built is how a rounded corner offsets the field.
    meta = dict(utm_epsg=epsg, dx_m=round(dx, 1), coarsened=coarsened,
                bbox=[float(v) for v in bbox],
                x_origin_m=mesh["x_origin_m"], y_origin_m=mesh["y_origin_m"],
                ocean_edge=ocean_edge, n_ocean_nodes=n_ocean,
                n_wet_nodes=int(wet.sum()), depth_max_m=depth_max,
                topo_max_m=topo_max, bathy_source=src)
    return mesh, meta


# ---------------------------------------------------------------------------
# 2. SELAFIN geometry + boundary-conditions (.cli) writers (river_dye family)
# ---------------------------------------------------------------------------
def write_slf(mesh, path):
    """The coastal GEOMETRY SELAFIN - local metres, with the origin in the header.

    The mesh coordinates are local (node 0 at the domain's SW corner) and stay
    that way, because the solver's own arithmetic is happiest near zero. What was
    missing is the header telling a reader WHERE zero is: SELAFIN carries
    X-ORIGIN / Y-ORIGIN in IPARAM(3)/(4), TELEMAC copies them from the geometry
    into the results file (``read_mesh_info.f`` -> ``write_mesh.f``), and MDAL
    honours them - so the animated result mesh lands on the bay instead of at the
    UTM zone's false origin, ~1600 km away. Integer metres: the Fortran declares
    X_ORIG as an INTEGER.
    """
    from data_manip.extraction.telemac_file import TelemacFile
    if os.path.exists(path):
        os.remove(path)
    tf = TelemacFile(path, access="w")
    tf.add_header(f"COASTAL {os.path.basename(path)}",
                  date=np.array([2026, 8, 14, 0, 0, 0]))
    tf.add_mesh(mesh["X"], mesh["Y"], mesh["ikle"], z=mesh["Z"],
                orig=(int(mesh.get("x_origin_m") or 0),
                      int(mesh.get("y_origin_m") or 0)))
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
    """CONLIM writer (river_dye line grammar): per-node LIHBOR/LIUBOR/LIVBOR +
    LITBOR + node1 + rank + a class tag comment."""
    ring = mesh["ring"]
    lines = []
    for k in range(mesh["nptfr"]):
        node1 = int(ring[k]) + 1
        rank = k + 1
        lih, liu = int(mesh["lihbor"][k]), int(mesh["liubor"][k])
        liv, lit = int(mesh["livbor"][k]), int(mesh["litbor"][k])
        lines.append(
            f"{lih} {liu} {liv}  0.000 0.000 0.000 0.000  {lit}  0.000 0.000 0.000 "
            f"{node1:>11d} {rank:>11d}   # {mesh['cls'][k]}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# 3. LIQUID BOUNDARIES FILE author (format pinned to read_fic_frliq.f + sl.f)
# ---------------------------------------------------------------------------
def _normalize_series(raw, datum_offset_m: float) -> list[tuple[float, float]]:
    """Coerce water_level_series -> sorted, strictly-increasing [(t_s, sl_m)].

    Accepts [[t,sl],...] or [{'t':..,'sl':..},...]; drops NaNs; de-dups equal
    times (keeps last); applies the datum offset. Raises on <2 valid points."""
    pts: list[tuple[float, float]] = []
    for row in (raw or []):
        if isinstance(row, dict):
            t = row.get("t", row.get("time"))
            v = row.get("sl", row.get("value", row.get("v")))
        else:
            t, v = row[0], row[1]
        try:
            t = float(t)
            v = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(t) and math.isfinite(v):
            pts.append((t, v + float(datum_offset_m)))
    if len(pts) < 2:
        raise CoastalInputError(
            "COASTAL_SERIES_INVALID",
            "water_level_series needs >=2 finite [t_seconds, sl_meters] points "
            f"(got {len(pts)}); author it from fetch_noaa_coops_tides/fetch_gtsm_tide_surge.")
    pts.sort(key=lambda p: p[0])
    dedup: list[tuple[float, float]] = []
    for t, v in pts:
        if dedup and abs(t - dedup[-1][0]) < 1e-6:
            dedup[-1] = (t, v)          # equal time -> keep last (strictly-increasing law)
        else:
            dedup.append((t, v))
    if dedup[0][0] > 1e-6:
        dedup.insert(0, (0.0, dedup[0][1]))   # anchor at t=0 (bracket the run start)
    return dedup


def write_liquid_boundaries_file(path: str, series: list[tuple[float, float]],
                                 duration_s: float, boundary_index: int = 1) -> dict:
    """Author the LIQUID BOUNDARIES FILE (T2DIMP): time-varying free surface at
    the ocean boundary. Column names line first (``T SL(<i>)``), then a units
    line, then strictly-increasing ``T SL`` rows bracketing [0, DURATION], then a
    trailing blank line (some compilers require it -- read_fic_frliq.f).

    read_fic_frliq.f aborts if a solver time falls OUTSIDE the series, so the last
    row time is clamped to >= DURATION (its value held flat)."""
    col = f"SL({int(boundary_index)})"
    rows = list(series)
    if rows[-1][0] <= duration_s:
        rows.append((float(duration_s) + max(1.0, 0.01 * duration_s), rows[-1][1]))
        # hold flat strictly past DURATION so read_fic_frliq never runs off the end
    body = [
        "# TRID3NT coastal tidal/surge forcing -- ocean liquid boundary water level",
        f"# datum-adjusted series, {len(rows)} rows, span "
        f"{rows[0][0]:.0f}..{rows[-1][0]:.0f} s",
        f"T {col}",
        "s m",
    ]
    for t, sl in rows:
        body.append(f"{t:.3f} {sl:.4f}")
    body.append("")   # trailing blank line
    with open(path, "w") as f:
        f.write("\n".join(body) + "\n")
    return dict(liqbnd_file=os.path.basename(path), liqbnd_rows=len(rows),
                liqbnd_col=col, sl_min_m=round(min(r[1] for r in rows), 4),
                sl_max_m=round(max(r[1] for r in rows), 4),
                t_end_s=round(rows[-1][0], 1))


# ---------------------------------------------------------------------------
# 4. Steering-file (.cas) author
# ---------------------------------------------------------------------------
def _cas_real(v: float) -> str:
    s = f"{float(v):g}"
    return s if any(c in s for c in ".eE") else s + "."


def author_deck(cfg: CoastalConfig, mesh, slf, cli, res, liq, cas_path,
                init_wl: float, meta: dict) -> None:
    """Write the coastal .cas: CONSTANT-ELEVATION IC + a single elevation-imposed
    ocean boundary whose SL(1) is read from the LIQUID BOUNDARIES FILE, with
    TIDAL FLATS wetting/drying so the rising water floods the low land."""
    import math as _math

    _fric_law = 3 if cfg.friction_law is None else int(cfg.friction_law)
    _fric_coef = _cas_real(cfg.friction_coefficient)

    _wind = float(cfg.wind_speed_mps or 0.0)
    if _wind > 0.0:
        th = _math.radians(float(cfg.wind_dir_from_deg or 0.0))
        wx = -_wind * _math.sin(th)     # FROM-dir -> blows TOWARD
        wy = -_wind * _math.cos(th)
        wind_block = (
            "WIND                            = YES\n"
            "OPTION FOR WIND                 = 1\n"
            f"WIND VELOCITY ALONG X           = {_cas_real(wx)}\n"
            f"WIND VELOCITY ALONG Y           = {_cas_real(wy)}\n"
            "THRESHOLD DEPTH FOR WIND        = 1.\n"
        )
    else:
        wind_block = ""

    # ADR 0283 cadence: an explicit graphic_period wins; else output_interval_min
    # (minutes) -> a TIMESTEP COUNT; else the ~40-frame computed default.
    if cfg.graphic_period:
        gp = int(cfg.graphic_period)
    elif cfg.output_interval_min is not None:
        gp = max(1, round(float(cfg.output_interval_min) * 60.0 / float(cfg.time_step_s)))
    else:
        gp = max(1, int(round((cfg.duration_s or 3600.0) / cfg.time_step_s / 40.0)))

    cas = f"""/-------------------------------------------------------------------/
/  TELEMAC-2D  COASTAL TIDAL/SURGE INUNDATION  -  {cfg.name}
/  Regular UTM grid over a coastal bbox (NOAA DEM_all topobathy at nodes).
/  ONE seaward OPEN boundary (edge {meta.get('ocean_edge')}, {meta.get('n_ocean_nodes')}
/  nodes, LIHBOR=5) driven in time by the LIQUID BOUNDARIES FILE SL(1); land
/  edges solid. TIDAL FLATS wetting/drying floods the low coast as SL rises.
/-------------------------------------------------------------------/
GEOMETRY FILE                   = {os.path.basename(slf)}
BOUNDARY CONDITIONS FILE        = {os.path.basename(cli)}
LIQUID BOUNDARIES FILE          = {os.path.basename(liq)}
RESULTS FILE                    = {os.path.basename(res)}
/
TITLE : '{cfg.name} COASTAL TIDAL SURGE INUNDATION'
VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B,F'
GRAPHIC PRINTOUT PERIOD         = {gp}
LISTING PRINTOUT PERIOD         = 500
/
DURATION                        = {cfg.duration_s}
TIME STEP                       = {cfg.time_step_s}
/
INITIAL CONDITIONS              = 'CONSTANT ELEVATION'
INITIAL ELEVATION               = {init_wl:.4f}
/
OPTION FOR LIQUID BOUNDARIES    = 1
PRESCRIBED ELEVATIONS           = {init_wl:.4f}
PRESCRIBED FLOWRATES            = 0.
/
LAW OF BOTTOM FRICTION          = {_fric_law}
FRICTION COEFFICIENT            = {_fric_coef}
VELOCITY DIFFUSIVITY            = 1.E-1
{wind_block}/
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
FREE SURFACE GRADIENT COMPATIBILITY     = 0.9
H CLIPPING     : NO
"""
    with open(cas_path, "w") as f:
        f.write(cas)


# ---------------------------------------------------------------------------
# 5. Solver
# ---------------------------------------------------------------------------
def run_solver(cas_path, res_path, cwd, timeout=3600):
    if os.path.exists(res_path):
        os.remove(res_path)
    log = subprocess.run(
        ["telemac2d.py", os.path.basename(cas_path)],
        cwd=cwd, capture_output=True, text=True, timeout=timeout)
    out = log.stdout + "\n" + log.stderr
    ok = "CORRECT END OF RUN" in out
    return ok, out


def _read_final_surface(res_path):
    """Return (X, Y, S, B, ntimestep) -- final-frame free surface + bottom."""
    from data_manip.extraction.telemac_file import TelemacFile
    tf = TelemacFile(res_path)
    nt = tf.ntimestep
    X = np.array(tf.meshx)
    Y = np.array(tf.meshy)

    def _get(name, frame):
        try:
            return np.array(tf.get_data_value(name, frame))
        except Exception:  # noqa: BLE001
            return None
    S = _get("FREE SURFACE", nt - 1)
    B = _get("BOTTOM", nt - 1)
    H = _get("WATER DEPTH", nt - 1)
    # peak free surface across all frames (max inundation stage per node).
    peak = None
    for fr in range(nt):
        s = _get("FREE SURFACE", fr)
        if s is None:
            continue
        peak = s if peak is None else np.maximum(peak, s)
    tf.close()
    return X, Y, S, B, H, peak, nt


# ---------------------------------------------------------------------------
# 6. Orchestration
# ---------------------------------------------------------------------------
def solve(cfg: CoastalConfig, workdir: str, run_id: str | None = None) -> dict:
    """Author + solve one coastal tidal/surge inundation; return a metrics dict.

    Flooded-area metric: mesh cells whose 3 nodes are dry at t0 (bed > init_wl)
    but wet (depth>WET_TOL) at PEAK stage -- the newly-inundated land area that
    discriminates a surge series from a calm tide."""
    t0 = time.time()
    tag = "coastal"
    WET_TOL = 0.02   # m depth to count a node as wet

    mesh, meta = build_coastal_mesh(cfg)

    series = _normalize_series(cfg.water_level_series, cfg.datum_offset_m)
    duration = float(cfg.duration_s) if cfg.duration_s else float(series[-1][0])
    cfg.duration_s = duration
    init_wl = float(cfg.init_wl_m) if cfg.init_wl_m is not None else float(series[0][1])

    # in-worker bed COG (real bathy only) -> role=context input via _bed_input.
    bed_cog_meta: dict = {}
    if str(cfg.bathy_source).lower() != "synthetic" and mesh.get("bed_lon") is not None:
        try:
            import _bed_cog as _BC  # noqa: WPS433 -- worker payload sibling
            bed_cog_meta = _BC.write_bed_cog_lonlat(
                mesh["bed_lon"], mesh["bed_lat"], mesh["bed_raw"],
                os.path.join(workdir, _BC.BED_COG_FILENAME))
            bed_cog_meta["bed_cog_source"] = "noaa_demall"
        except Exception as exc:  # noqa: BLE001 -- never fatal
            LOG.warning("coastal bed COG write failed (non-fatal): %s", exc)

    geo = os.path.join(workdir, f"geo_{tag}.slf")
    cli = os.path.join(workdir, f"bc_{tag}.cli")
    res = os.path.join(workdir, f"res_{tag}.slf")
    liq = os.path.join(workdir, LIQBND_FILENAME)
    cas = os.path.join(workdir, f"t2d_{tag}.cas")

    write_slf(mesh, geo)
    write_cli(mesh, cli)
    liq_meta = write_liquid_boundaries_file(liq, series, duration, boundary_index=1)
    author_deck(cfg, mesh, geo, cli, res, liq, cas, init_wl, meta)

    ok, out = run_solver(cas, res, workdir,
                         timeout=int(os.environ.get(
                             "TRID3NT_COASTAL_SOLVE_TIMEOUT", "3600")))
    (open(os.path.join(workdir, "full_listing.log"), "w").write(out) if out else None)

    metrics = {
        "status": "ok" if ok else "error",
        "correct_end": bool(ok),
        "run_id": run_id,
        "parser_version": COASTAL_PARSER_VERSION,
        "mode": "coastal",
        "result_slf": os.path.basename(res),
        "geometry_slf": os.path.basename(geo),
        "cli": os.path.basename(cli),
        "cas": os.path.basename(cas),
        "npoin": int(mesh["npoin"]),
        "nelem": int(len(mesh["ikle"])),
        "nx": int(mesh["nx"]), "ny": int(mesh["ny"]),
        "init_wl_m": round(init_wl, 4),
        "duration_s": duration,
        "series_datum": cfg.series_datum,
        "datum_offset_m": float(cfg.datum_offset_m),
        **meta,
        **liq_meta,
        **bed_cog_meta,
        "wall_s": round(time.time() - t0, 1),
    }
    if not ok:
        metrics["error"] = "TELEMAC-2D coastal did not reach CORRECT END OF RUN"
        metrics["listing_tail"] = "\n".join((out or "").splitlines()[-40:])
        return metrics

    X, Y, S, B, H, peak, nt = _read_final_surface(res)
    if S is None or B is None:
        metrics["status"] = "error"
        metrics["correct_end"] = False
        metrics["error"] = "coastal result carried no FREE SURFACE / BOTTOM field"
        return metrics

    # cell (triangle) areas + flooded-land discrimination metric.
    ikle = mesh["ikle"]
    ax, ay = X[ikle[:, 0]], Y[ikle[:, 0]]
    bx, by = X[ikle[:, 1]], Y[ikle[:, 1]]
    cx, cy = X[ikle[:, 2]], Y[ikle[:, 2]]
    tri_area = 0.5 * np.abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay))

    dry0 = B > init_wl                     # land above the initial water line
    stage = peak if peak is not None else S
    wet_peak = (stage - B) > WET_TOL
    newly = dry0 & wet_peak                # newly-flooded land nodes
    cell_newly = newly[ikle].all(axis=1)
    cell_wet = wet_peak[ikle].all(axis=1)
    flooded_land_km2 = float((tri_area[cell_newly].sum()) / 1e6)
    wet_peak_km2 = float((tri_area[cell_wet].sum()) / 1e6)

    # peak water level reported over WET nodes only: on dry nodes TELEMAC sets
    # FREE SURFACE = BOTTOM, so a raw nanmax(stage) just echoes the highest dry
    # land topo, not a water level.
    wet_stage = stage[wet_peak]
    peak_wl_wet = float(np.nanmax(wet_stage)) if wet_stage.size else float("nan")
    metrics.update({
        "ntimestep": int(nt),
        "peak_wl_max_m": round(peak_wl_wet, 4),
        "final_wl_max_m": round(float(np.nanmax(np.where((S - B) > WET_TOL, S, np.nan))), 4),
        "flooded_land_km2": round(flooded_land_km2, 5),
        "wet_peak_km2": round(wet_peak_km2, 5),
        "n_newly_flooded_nodes": int(newly.sum()),
        **liq_meta,   # sl_min/sl_max echoed for the A/B summary
    })
    return metrics
