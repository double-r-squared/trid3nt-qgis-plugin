"""TELEMAC-2D rain-on-grid (RoG) worker payload.

The RoG analogue of ``telemac_river_dye_build``: instead of building a channel
mesh from a river centerline and injecting a dye pulse, this consumes a
watershed SELAFIN staged by the agent-side mesh-acquisition step (ADR 0196
Decision 1 -- UTM metres, BOTTOM = bed, positive-up), authors a rain-on-grid
steering deck, solves locally, and extracts the outlet hydrograph + max fields
+ mass balance.

Physics (grounded in the installed TELEMAC v9.0.0 sources, not guessed):

  * RAIN forcing -- ``RAIN OR EVAPORATION = YES`` + ``RAIN OR EVAPORATION IN MM
    PER DAY`` (the native surface source term, ``prosou`` -> ``PLUIE``). The
    installed ``runoff_scs_cn.f`` hardcodes ``RAINDEF=1`` (constant intensity
    from the keyword), so the NATIVE path drives a constant design-storm
    intensity; a true time-varying hyetograph cannot drive the compiled build
    without recompiling ``user_rain.f`` (documented limitation; the agent-side
    ``select_runoff_path`` records which path a run took).
  * INFILTRATION (native path) -- ``RAINFALL-RUNOFF MODEL = 1`` (SCS curve
    number) + ``ANTECEDENT MOISTURE CONDITIONS = {1|2|3}`` + ``OPTION FOR
    INITIAL ABSTRACTION RATIO``. The per-node CN2 field is read via ``HYDROMAP``
    from FORMATTED DATA FILE 2 -- a scatter file of ``X Y CN2`` the engine
    interpolates back onto the mesh nodes. The engine's steep-slope branch is
    compiled OFF (``STEEPSLOPECOR=.FALSE.``), so the Huang correction is baked
    into the CN2 field by the agent before it is written (ADR 0195).
  * DISTRIBUTED MANNING -- ``FRICTION DATA = YES`` + a FRICTION DATA FILE
    (one ``<zone> MANNING <n> NULL`` line per distinct per-NLCD roughness, ended
    by ``END``) + a ZONES FILE (``<node> <zone>`` per node, read by
    ``friction_user.f`` into ``KFROPT``). Fully native per-node roughness.
  * OUTLET BC -- the ring nodes nearest the pour point are a FREE exit
    (``KSORT=4`` in the CLI: LIHBOR/LIUBOR/LIVBOR = 4, no imposed stage); every
    other boundary node is a solid wall (``KLOG=2``). Rain-fed interior,
    initially dry (``INITIAL CONDITIONS = 'ZERO ELEVATION'``), water leaves
    only through the pour-point segment -- the physical catchment outfall.

Deliverables (the honesty-floor typed numbers the agent narrates):

  * outlet discharge hydrograph Q(t) -- unit discharge integrated across the
    outlet boundary edges per output frame (the PRIMARY product);
  * max-depth + max-velocity node fields (for the depth/velocity COGs);
  * runoff volume + mass-balance continuity from the listing WATER VOLUME block.

ASCII only. No agent code imported; this runs only inside the worker image.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections import defaultdict
from typing import Any

import numpy as np

LOG = logging.getLogger("trid3nt.worker.telemac.rog")

#: staged input filenames (the agent-side composer writes these into the rundir
#: next to manifest.json; the worker reads them by basename).
WATERSHED_SLF = "watershed.slf"           # BOTTOM SELAFIN (UTM metres) from mesh_acquisition
NODE_CN2_FILE = "node_cn2.txt"            # one CN2 per line, mesh-node order
NODE_MANNING_FILE = "node_manning.txt"    # one Manning n per line, mesh-node order

#: authored output filenames (the supervisor uploads whatever the manifest
#: outputs globs match).
ROG_GEOMETRY_SLF = "rog_geometry.slf"     # rewritten geometry (consistent IPOBO)
ROG_RESULT_SLF = "r2d_rog.slf"            # the RESULT mesh (U,V,H,S,B per frame)
ROG_CLI = "rog.cli"                       # boundary conditions
ROG_CAS = "t2d_rog.cas"                   # steering deck
ROG_CN_MAP = "rog_cn_map.dat"             # FORMATTED DATA FILE 2 (X Y CN2 scatter)
ROG_FRICTION_COF = "rog_friction.tbl"     # FRICTION DATA FILE (zone laws)
ROG_ZONES_FILE = "rog_zones.dat"          # ZONES FILE (node -> zone id)
ROG_HYDROGRAPH = "rog_outlet_hydrograph.json"  # outlet Q(t) + summary


class RogInputError(RuntimeError):
    """A staged RoG input is missing or malformed (typed, never a silent guess)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# --------------------------------------------------------------------------- #
# 1. read the watershed SELAFIN staged by mesh_acquisition.
# --------------------------------------------------------------------------- #
def read_watershed_mesh(slf_path: str) -> dict[str, Any]:
    """Read a BOTTOM SELAFIN -> ``{X, Y, ikle, bed, npoin, nelem}`` (metres).

    ``ikle`` is 0-based (M,3). The BOTTOM variable is the bed (positive-up
    metres); its name is matched loosely ('BOTTOM' / starts with 'B')."""
    from data_manip.extraction.telemac_file import TelemacFile

    if not os.path.exists(slf_path) or os.path.getsize(slf_path) == 0:
        raise RogInputError(
            "TELEMAC_ROG_WATERSHED_SLF_MISSING",
            f"watershed SELAFIN not found or empty: {slf_path}",
        )
    tf = TelemacFile(slf_path)
    X = np.asarray(tf.meshx, dtype=float)
    Y = np.asarray(tf.meshy, dtype=float)
    ikle = np.asarray(tf.ikle2, dtype=np.int64)
    if ikle.min() == 1:  # some writers are 1-based; normalize to 0-based
        ikle = ikle - 1
    bedvars = [v for v in tf.varnames
               if "BOTTOM" in v.upper() or v.strip().upper().startswith("B")]
    if bedvars and tf.times is not None and len(tf.times) > 0:
        bed = np.asarray(tf.get_data_value(bedvars[0], 0), dtype=float)
    else:
        bed = np.zeros(X.shape[0], dtype=float)
    tf.close()
    npoin = int(X.shape[0])
    if ikle.shape[0] < 1 or npoin < 3:
        raise RogInputError(
            "TELEMAC_ROG_WATERSHED_SLF_DEGENERATE",
            f"watershed mesh too small: npoin={npoin} nelem={ikle.shape[0]}",
        )
    return {"X": X, "Y": Y, "ikle": ikle, "bed": bed,
            "npoin": npoin, "nelem": int(ikle.shape[0])}


# --------------------------------------------------------------------------- #
# 2. boundary ring + rank-based IPOBO (mirrors telemac_river_dye_build).
# --------------------------------------------------------------------------- #
def build_boundary(X: Any, Y: Any, ikle: Any) -> dict[str, Any]:
    """Boundary ring(s), rank-based IPOBO and NPTFR from a triangulation.

    Boundary edges are those in exactly one triangle; the directed triangle
    edges wind the domain on the left, so the walk yields outer-CCW / holes-CW
    rings (the TELEMAC convention). Ranks run consecutively, OUTER ring first."""
    ikle = np.asarray(ikle, dtype=np.int64)
    X = np.asarray(X, dtype=float)
    Y = np.asarray(Y, dtype=float)
    npoin = int(X.shape[0])

    # enforce CCW element orientation (positive signed area) so directed edges
    # wind consistently.
    a, b, c = ikle[:, 0], ikle[:, 1], ikle[:, 2]
    area2 = (X[b] - X[a]) * (Y[c] - Y[a]) - (X[c] - X[a]) * (Y[b] - Y[a])
    ikle = ikle.copy()
    ikle[area2 < 0] = ikle[area2 < 0][:, ::-1]

    ec: dict[tuple[int, int], int] = defaultdict(int)
    ed: dict[tuple[int, int], tuple[int, int]] = {}
    for t in ikle:
        for k in range(3):
            u, v = int(t[k]), int(t[(k + 1) % 3])
            key = (min(u, v), max(u, v))
            ec[key] += 1
            ed[key] = (u, v)
    bnd = [ed[k] for k, n in ec.items() if n == 1]
    nxt = {u: v for u, v in bnd}
    if len(nxt) != len(bnd):
        raise RogInputError(
            "TELEMAC_ROG_MESH_NONMANIFOLD",
            "watershed boundary is non-manifold (a node has two outgoing "
            "boundary edges); the staged mesh is not a clean single-body TIN.",
        )
    rings: list[list[int]] = []
    unvisited = set(nxt)
    while unvisited:
        start = next(iter(unvisited))
        walk = [start]
        cur = nxt[start]
        while cur != start:
            walk.append(cur)
            cur = nxt[cur]
        unvisited -= set(walk)
        rings.append(walk)
    rings.sort(key=len, reverse=True)
    ring = np.array([n for w in rings for n in w], dtype=np.int64)
    nptfr = int(ring.shape[0])
    ipob = np.zeros(npoin, dtype=np.int32)
    for rank, node in enumerate(ring, start=1):
        ipob[node] = rank
    return {"ikle": ikle, "ring": ring, "ipob": ipob, "nptfr": nptfr,
            "n_rings": len(rings)}


# --------------------------------------------------------------------------- #
# 3. classify the outlet segment (free exit) vs walls.
# --------------------------------------------------------------------------- #
def classify_outlet(
    X: Any, Y: Any, ring: Any, outlet_xy: tuple[float, float],
    *, n_outlet_nodes: int = 6,
) -> dict[str, Any]:
    """Mark the ``n_outlet_nodes`` ring nodes nearest the pour point as the free
    exit; every other ring node is a solid wall.

    Returns per-ring-node BC codes (LIHBOR/LIUBOR/LIVBOR/LITBOR) and the global
    node indices of the outlet segment. Free exit uses KSORT=4 (value computed,
    no imposed stage -- the rain-fed catchment drains out here); walls use
    KLOG=2. The outlet segment must span at least one boundary EDGE (>= 2 nodes)
    for the discharge integration; ``n_outlet_nodes`` is clamped up to that."""
    X = np.asarray(X, dtype=float)
    Y = np.asarray(Y, dtype=float)
    ring = np.asarray(ring, dtype=np.int64)
    nptfr = int(ring.shape[0])
    ox, oy = float(outlet_xy[0]), float(outlet_xy[1])
    rx = X[ring]
    ry = Y[ring]
    d2 = (rx - ox) ** 2 + (ry - oy) ** 2
    k = max(2, min(int(n_outlet_nodes), nptfr - 3))
    # A CONTIGUOUS arc of k ring nodes centred on the node nearest the pour point.
    # The ring is in boundary-walk order, so a contiguous index window is a
    # contiguous boundary segment; the k globally-nearest nodes are NOT guaranteed
    # contiguous and an isolated liquid node between two walls aborts the solver
    # (FRONT2: "LIQUID POINT BETWEEN TWO SOLID POINTS"). Centring on argmin keeps
    # the free-exit segment on the single stretch of boundary at the outlet.
    i0 = int(np.argmin(d2))
    half = k // 2
    outlet_ring_idx = set(int((i0 + off) % nptfr)
                          for off in range(-half, k - half))

    lihbor = np.full(nptfr, 2, dtype=int)
    liubor = np.full(nptfr, 2, dtype=int)
    livbor = np.full(nptfr, 2, dtype=int)
    litbor = np.full(nptfr, 2, dtype=int)
    cls = np.array(["wall"] * nptfr, dtype=object)
    for i in outlet_ring_idx:
        lihbor[i], liubor[i], livbor[i], litbor[i] = 4, 4, 4, 4
        cls[i] = "outlet"
    outlet_nodes = [int(ring[i]) for i in sorted(outlet_ring_idx)]
    return {"lihbor": lihbor, "liubor": liubor, "livbor": livbor,
            "litbor": litbor, "cls": cls, "outlet_nodes": outlet_nodes,
            "outlet_dist_min_m": round(float(math.sqrt(d2.min())), 2),
            "n_outlet_nodes": len(outlet_nodes)}


# --------------------------------------------------------------------------- #
# 4. write the geometry SELAFIN (consistent IPOBO) + CLI.
# --------------------------------------------------------------------------- #
def write_rog_slf(path: str, X: Any, Y: Any, ikle: Any, bed: Any,
                  ipob: Any, ring: Any, nptfr: int) -> str:
    """Write a single-variable (BOTTOM) SELAFIN with the rank-based IPOBO so the
    CLI (ring order) and the geometry agree node-for-node."""
    from data_manip.extraction.telemac_file import TelemacFile

    if os.path.exists(path):
        os.remove(path)
    X = np.asarray(X, dtype=float)
    Y = np.asarray(Y, dtype=float)
    ikle = np.asarray(ikle, dtype=np.int64)
    bed = np.asarray(bed, dtype=float)
    npoin = int(X.shape[0])
    tf = TelemacFile(path, access="w")
    tf.add_header(f"TRID3NT RAIN-ON-GRID {os.path.basename(path)}",
                  date=np.array([2026, 8, 8, 0, 0, 0]))
    tf.add_mesh(X, Y, ikle, z=bed)
    tf._ipob3 = np.asarray(ipob, dtype=np.int32)
    tf._ipob2 = tf._ipob3
    tf._nptfr = int(nptfr)
    tf._nbor = (np.asarray(ring, dtype=np.int32) + 1)
    tf._knolg = np.arange(1, npoin + 1, dtype=np.int32)
    tf.add_variable("BOTTOM          ", "M               ")
    tf.add_data_value("BOTTOM          ", 0, bed)
    tf.write()
    tf.close()
    return path


def write_rog_cli(path: str, ring: Any, bc: dict[str, Any]) -> str:
    """Write the CLI in ring order (rank = k+1, node = ring[k]+1)."""
    ring = np.asarray(ring, dtype=np.int64)
    nptfr = int(ring.shape[0])
    lines = []
    for k in range(nptfr):
        node1 = int(ring[k]) + 1
        rank = k + 1
        lih, liu = int(bc["lihbor"][k]), int(bc["liubor"][k])
        liv, lit = int(bc["livbor"][k]), int(bc["litbor"][k])
        lines.append(
            f"{lih} {liu} {liv}  0.000 0.000 0.000 0.000  {lit}  0.000 0.000 "
            f"0.000 {node1:>11d} {rank:>11d}   # {bc['cls'][k]}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------- #
# 5. CN2 scatter map (FORMATTED DATA FILE 2, read by HYDROMAP).
# --------------------------------------------------------------------------- #
def write_cn_map(path: str, X: Any, Y: Any, cn2: Any) -> str:
    """Write the per-node CN2 scatter file HYDROMAP reads: ``X Y CN2`` per line.

    HYDROMAP (bief) skips ``#`` comment lines and interpolates the scatter onto
    the mesh; since the scatter points ARE the mesh nodes the interpolation is
    an identity. CN2 is clamped to (0, 100] (the engine aborts otherwise)."""
    X = np.asarray(X, dtype=float)
    Y = np.asarray(Y, dtype=float)
    cn = np.clip(np.asarray(cn2, dtype=float), 1.0, 100.0)
    if cn.shape[0] != X.shape[0]:
        raise RogInputError(
            "TELEMAC_ROG_CN_LENGTH_MISMATCH",
            f"CN2 field has {cn.shape[0]} values but mesh has {X.shape[0]} nodes.",
        )
    with open(path, "w") as f:
        f.write("# X Y CN2 (curve number, AMC-II) -- FORMATTED DATA FILE 2\n")
        for x, y, c in zip(X, Y, cn):
            f.write(f"{x:.3f} {y:.3f} {c:.3f}\n")
    return path


# --------------------------------------------------------------------------- #
# 6. distributed Manning: FRICTION DATA FILE (zone laws) + ZONES FILE (node->zone).
# --------------------------------------------------------------------------- #
def write_friction_files(
    path_cof: str, path_zones: str, manning_per_node: Any,
) -> dict[str, Any]:
    """Write the native distributed-friction pair from a per-node Manning field.

    Distinct Manning values (rounded to 1e-3) become friction zones; the
    FRICTION DATA FILE lists ``<zone> MANNING <n> NULL`` per zone (ended by
    ``END``, required -- ``friction_scan`` aborts on a bare EOF), and the ZONES
    FILE lists ``<node> <zone>`` per node (1-based node id, read by
    ``friction_user`` into ``KFROPT``). LAW OF BOTTOM FRICTION = 4 (Manning)."""
    n = np.asarray(manning_per_node, dtype=float)
    n = np.clip(n, 0.005, 1.0)
    keys = np.round(n, 3)
    uniq = sorted(set(float(v) for v in keys))
    zone_of_value = {v: i + 1 for i, v in enumerate(uniq)}  # 1-based zone ids
    node_zone = [zone_of_value[float(v)] for v in keys]

    with open(path_cof, "w") as f:
        f.write("* TRID3NT rain-on-grid distributed Manning (per-NLCD)\n")
        f.write("* zone  law      coef    vegetation\n")
        for v in uniq:
            f.write(f"{zone_of_value[v]} MANNING {v:.3f} NULL\n")
        f.write("END\n")

    with open(path_zones, "w") as f:
        for i, z in enumerate(node_zone, start=1):  # 1-based node numbering
            f.write(f"{i} {z}\n")
    return {"n_zones": len(uniq), "manning_values": uniq}


# --------------------------------------------------------------------------- #
# 7. author the RoG steering deck.
# --------------------------------------------------------------------------- #
def _cas_real(v: float) -> str:
    s = f"{float(v):g}"
    return s if any(ch in s for ch in ".eE") else s + "."


def author_rog_deck(
    cfg: Any, *, slf: str, cli: str, res: str, cas_path: str,
    cn_map: str, friction_cof: str, zones_file: str,
    rain_mm_per_day: float, runoff_path: str,
) -> str:
    """Write the rain-on-grid ``.cas``.

    NATIVE path (``runoff_path == 'native'``): constant rain + RAINFALL-RUNOFF
    MODEL = 1 (SCS-CN) + AMC + FORMATTED DATA FILE 2 (CN2 scatter). PREPROCESSING
    path: RAINFALL-RUNOFF MODEL = 0 (the agent already removed infiltration up
    front, so the fed rain is the net excess; no double counting). Both paths
    share the free-exit outlet, distributed Manning and the zero-elevation
    (dry) start. NO tracers -- pure hydraulics; the outlet hydrograph is the
    product."""
    amc = int(getattr(cfg, "amc_condition", 2) or 2)
    ia_opt = int(getattr(cfg, "initial_abstraction_option", 1) or 1)
    duration_s = float(getattr(cfg, "duration_s", 3600.0))
    dt = float(getattr(cfg, "time_step_s", 2.0))
    gp = int(getattr(cfg, "graphic_period", 100))

    # Rain-on window: rain falls for rain_duration_s (native keyword
    # DURATION OF RAIN OR EVAPORATION IN HOURS / RAIN_HDUR), then stops so the
    # catchment drains -- the recession limb the constant-full-duration source
    # cannot produce. Emitted only when a finite rain window shorter than the
    # total DURATION is set; else rain falls the whole run (legacy behaviour).
    rain_dur_s = getattr(cfg, "rain_duration_s", None)
    rain_hdur_line = ""
    if rain_dur_s is not None and 0.0 < float(rain_dur_s) < duration_s:
        rain_hdur_line = (
            "DURATION OF RAIN OR EVAPORATION IN HOURS = "
            f"{_cas_real(float(rain_dur_s) / 3600.0)}\n"
        )

    if str(runoff_path).lower() == "native":
        runoff_block = (
            "RAIN OR EVAPORATION             = YES\n"
            f"RAIN OR EVAPORATION IN MM PER DAY = {_cas_real(rain_mm_per_day)}\n"
            f"{rain_hdur_line}"
            "RAINFALL-RUNOFF MODEL           = 1\n"
            f"ANTECEDENT MOISTURE CONDITIONS  = {amc}\n"
            f"OPTION FOR INITIAL ABSTRACTION RATIO = {ia_opt}\n"
            f"FORMATTED DATA FILE 2           = {os.path.basename(cn_map)}\n"
        )
    else:
        # net excess rain already computed up front (agent side); no infiltration
        # in the solver so it is not double counted.
        runoff_block = (
            "RAIN OR EVAPORATION             = YES\n"
            f"RAIN OR EVAPORATION IN MM PER DAY = {_cas_real(rain_mm_per_day)}\n"
            f"{rain_hdur_line}"
            "RAINFALL-RUNOFF MODEL           = 0\n"
        )

    cas = f"""/-------------------------------------------------------------------/
/  TELEMAC-2D  RAIN-ON-GRID  -  {getattr(cfg, 'name', 'watershed')}
/  Rain-fed catchment on a delineated watershed TIN (UTM metres).
/  Runoff path: {runoff_path}. Rain = {rain_mm_per_day:g} mm/day.
/  Distributed Manning (per-NLCD zones); free-exit outlet at the pour point.
/-------------------------------------------------------------------/
GEOMETRY FILE                   = {os.path.basename(slf)}
BOUNDARY CONDITIONS FILE        = {os.path.basename(cli)}
RESULTS FILE                    = {os.path.basename(res)}
FRICTION DATA FILE              = {os.path.basename(friction_cof)}
ZONES FILE                      = {os.path.basename(zones_file)}
/
TITLE : '{getattr(cfg, 'name', 'watershed')} RAIN-ON-GRID'
VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B'
GRAPHIC PRINTOUT PERIOD         = {gp}
LISTING PRINTOUT PERIOD         = {gp}
/
DURATION                        = {duration_s}
TIME STEP                       = {dt}
/
INITIAL CONDITIONS              = 'ZERO ELEVATION'
/
LAW OF BOTTOM FRICTION          = 4
FRICTION DATA                   = YES
{runoff_block}/
EQUATIONS                       = 'SAINT-VENANT FE'
TREATMENT OF THE LINEAR SYSTEM  = 2
TYPE OF ADVECTION               = 1;5
SUPG OPTION                     = 0;0
MASS-LUMPING ON H : 1.
CONTINUITY CORRECTION : YES
SOLVER                          = 1
SOLVER ACCURACY                 = 1.E-6
MAXIMUM NUMBER OF ITERATIONS FOR SOLVER = 200
IMPLICITATION FOR DEPTH         = 0.6
IMPLICITATION FOR VELOCITY      = 0.6
FREE SURFACE GRADIENT COMPATIBILITY = 0.9
TIDAL FLATS                             = YES
OPTION FOR THE TREATMENT OF TIDAL FLATS = 1
TREATMENT OF NEGATIVE DEPTHS            = 2
H CLIPPING     : NO
MASS-BALANCE                    = YES
INFORMATION ABOUT SOLVER        = YES
/
NUMBER OF TRACERS               = 0
"""
    with open(cas_path, "w") as f:
        f.write(cas)
    return cas_path


# --------------------------------------------------------------------------- #
# 8. run the solver (identical envelope to the river-dye path).
# --------------------------------------------------------------------------- #
def run_solver(cas_path: str, res_path: str, cwd: str, timeout: float = 1200.0):
    import subprocess

    if os.path.exists(res_path):
        os.remove(res_path)
    log = subprocess.run(
        ["telemac2d.py", os.path.basename(cas_path)],
        cwd=cwd, capture_output=True, text=True, timeout=timeout)
    out = log.stdout + "\n" + log.stderr
    ok = "CORRECT END OF RUN" in out
    return ok, out


# --------------------------------------------------------------------------- #
# 9. extract the outlet hydrograph + max fields + mass balance.
# --------------------------------------------------------------------------- #
def _outlet_edges(ring: Any, outlet_nodes: list[int]) -> list[tuple[int, int]]:
    """Consecutive ring-node pairs that are both outlet nodes = the outlet edges."""
    ring = [int(n) for n in np.asarray(ring, dtype=np.int64)]
    oset = set(int(n) for n in outlet_nodes)
    edges = []
    for k in range(len(ring)):
        a = ring[k]
        b = ring[(k + 1) % len(ring)]
        if a in oset and b in oset:
            edges.append((a, b))
    return edges


def extract_rog_outputs(
    res_slf: str, listing_text: str, *,
    X: Any, Y: Any, ring: Any, outlet_nodes: list[int],
) -> dict[str, Any]:
    """Outlet Q(t) + max-depth/velocity fields + runoff volume / continuity.

    Outlet discharge per frame = sum over outlet boundary edges of the mean unit
    discharge (H*Vn) projected on the OUTWARD edge normal times the edge length
    (trapezoidal across the two edge nodes). Positive = leaving the domain.
    Max-depth / max-velocity are the per-node maxima over all frames. Runoff
    volume + continuity are parsed from the listing WATER VOLUME / MASS balance
    block (the engine's own closure, never invented)."""
    from data_manip.extraction.telemac_file import TelemacFile

    X = np.asarray(X, dtype=float)
    Y = np.asarray(Y, dtype=float)
    edges = _outlet_edges(ring, outlet_nodes)

    tf = TelemacFile(res_slf)
    names = {v.strip().upper(): v for v in tf.varnames}

    def _find(*cands: str) -> str | None:
        for c in cands:
            for key, orig in names.items():
                if key == c or key.startswith(c):
                    return orig
        return None

    vU = _find("VELOCITY U", "U")
    vV = _find("VELOCITY V", "V")
    vH = _find("WATER DEPTH", "H")
    times = np.asarray(tf.times, dtype=float)
    npoin = int(X.shape[0])

    # outward normals per outlet edge: rotate the edge tangent -90 deg and flip
    # so it points away from the domain centroid.
    cx, cy = float(X.mean()), float(Y.mean())
    edge_geom = []
    for a, b in edges:
        ex, ey = X[b] - X[a], Y[b] - Y[a]
        L = math.hypot(ex, ey)
        if L <= 0:
            continue
        nx, ny = ey / L, -ex / L
        mx, my = 0.5 * (X[a] + X[b]), 0.5 * (Y[a] + Y[b])
        if (mx - cx) * nx + (my - cy) * ny < 0:  # ensure outward
            nx, ny = -nx, -ny
        edge_geom.append((a, b, nx, ny, L))

    q_series = []
    max_depth = np.zeros(npoin, dtype=float)
    max_vel = np.zeros(npoin, dtype=float)
    for i in range(len(times)):
        H = np.asarray(tf.get_data_value(vH, i), dtype=float) if vH else np.zeros(npoin)
        U = np.asarray(tf.get_data_value(vU, i), dtype=float) if vU else np.zeros(npoin)
        V = np.asarray(tf.get_data_value(vV, i), dtype=float) if vV else np.zeros(npoin)
        Hc = np.clip(H, 0.0, None)
        speed = np.hypot(U, V)
        max_depth = np.maximum(max_depth, Hc)
        max_vel = np.maximum(max_vel, speed)
        q = 0.0
        for a, b, nx, ny, L in edge_geom:
            qa = Hc[a] * (U[a] * nx + V[a] * ny)
            qb = Hc[b] * (U[b] * nx + V[b] * ny)
            q += 0.5 * (qa + qb) * L
        q_series.append(float(q))
    tf.close()

    q_arr = np.asarray(q_series, dtype=float)
    peak_q = float(q_arr.max()) if q_arr.size else 0.0
    peak_idx = int(q_arr.argmax()) if q_arr.size else 0
    # outflow volume via trapezoidal integration of Q(t).
    outflow_vol = float(np.trapz(np.clip(q_arr, 0.0, None), times)) if q_arr.size > 1 else 0.0

    mass = _parse_mass_balance(listing_text)

    return {
        "outlet_hydrograph": {
            "t_s": [round(float(t), 1) for t in times],
            "q_m3s": [round(v, 4) for v in q_series],
        },
        "peak_discharge_m3s": round(peak_q, 4),
        "peak_time_s": round(float(times[peak_idx]), 1) if times.size else 0.0,
        "outflow_volume_m3": round(outflow_vol, 2),
        "n_outlet_edges": len(edge_geom),
        "max_depth_m": max_depth,      # np arrays for the COGs (not JSON)
        "max_velocity_ms": max_vel,
        "max_depth_peak_m": round(float(max_depth.max()), 4) if max_depth.size else 0.0,
        "max_velocity_peak_ms": round(float(max_vel.max()), 4) if max_vel.size else 0.0,
        "n_frames": int(times.size),
        **mass,
    }


def _parse_mass_balance(listing_text: str) -> dict[str, Any]:
    """Parse the WATER VOLUME balance block the engine prints (its own closure).

    From the installed v9.0.0 listing shape::

        RUNOFF_SCS_CN : ACCUMULATED RAINFALL :  0.1000000  M
        BALANCE OF WATER VOLUME
        VOLUME IN THE DOMAIN :   8166.593  M3
        FLUX BOUNDARY 1: -1.279987  M3/S ( >0 : ENTERING <0 : EXITING )
        ADDITIONAL VOLUME DUE TO SOURCE TERMS:  23.28232  M3
        RELATIVE ERROR IN VOLUME AT T = 1200. S : -0.25E-15

    We surface the FINAL domain volume, the FINAL relative volume error
    (continuity), the accumulated rainfall depth, and the peak |boundary flux|
    (an independent cross-check on the integrated outlet hydrograph). Every field
    is best-effort (omitted if absent)."""
    out: dict[str, Any] = {}
    txt = listing_text or ""

    # continuity: the number AFTER the final "... S :" on the relative-error line.
    rel = re.findall(
        r"RELATIVE ERROR IN VOLUME[^:]*:\s*([-\d.Ee+]+)", txt)
    if rel:
        try:
            out["continuity_rel_error"] = float(rel[-1])
        except (TypeError, ValueError):
            pass
    dom = re.findall(r"VOLUME IN THE DOMAIN\s*:\s*([-\d.Ee+]+)", txt)
    if dom:
        try:
            out["final_domain_volume_m3"] = round(float(dom[-1]), 2)
        except (TypeError, ValueError):
            pass
    rain = re.findall(r"ACCUMULATED RAINFALL\s*:\s*([-\d.Ee+]+)", txt)
    if rain:
        try:
            out["accumulated_rainfall_m"] = round(float(rain[-1]), 5)
        except (TypeError, ValueError):
            pass
    src = re.findall(
        r"ADDITIONAL VOLUME DUE TO SOURCE TERMS\s*:\s*([-\d.Ee+]+)", txt)
    if src:
        try:
            out["source_volume_m3"] = round(float(src[-1]), 2)
        except (TypeError, ValueError):
            pass
    flux = re.findall(r"FLUX BOUNDARY\s+\d+\s*:\s*([-\d.Ee+]+)", txt)
    if flux:
        try:
            vals = [abs(float(v)) for v in flux]
            out["listing_peak_boundary_flux_m3s"] = round(max(vals), 4)
        except (TypeError, ValueError):
            pass
    return out
