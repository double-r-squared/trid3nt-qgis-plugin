"""
THROWAWAY P0 GO/NO-GO SPIKE — PySWMM quasi-2D urban-flood engine.

NOT agent-wired. Run from the scratch venv (repo root):
    .venv-swmm-spike/bin/python services/workers/swmm/spike_quasi2d.py

De-risks three engine-killing questions BEFORE any agent integration:
  1. Does pyswmm run a quasi-2D overland grid headless without crashing, with
     acceptable mass balance?
  2. Can we represent a hard barrier ("red wall") by omitting an overland
     conduit, and does it actually pond water upstream?
  3. Can we represent a one-way structure ("green flap gate") with a native
     SWMM flap-gate element, and is it genuinely one-directional?

Plus an output-pipeline check: rasterize one timestep of node depths to a COG
that re-opens in rasterio with correct transform/CRS.

Design choices (pinned against installed swmm-api 0.4.73 / pyswmm 2.1.0):
  - ONE STORAGE node per active cell. FUNCTIONAL curve A1=0, A2=0, A0=cell_area
    so surface area is the constant cell footprint (a*d^b + c with a=0 -> c).
  - 4-connectivity overland CONDUITS, RECT_OPEN, width=cell_size, tall height,
    Manning n=0.03, length=cell_size.
  - ONE boundary OUTFALL (FREE) at the low corner.
  - Rainfall applied via one SubCatchment per cell (100% impervious, ~0
    depression storage) draining to that cell's storage node, fed by a single
    RAINGAGE + a synthetic ~100-yr SCS-Type-II-ish hyetograph TIMESERIES.
  - Red wall  = OMIT one overland conduit on the uphill side.
  - Green flap = ORIFICE with has_flap_gate=True (swmm-api kwarg pinned by
    source inspection; CONDUIT has NO flap-gate kwarg in this version, only
    Orifice/Outlet/Weir/Outfall do).
  - DYNWAVE routing, INERTIAL_DAMPING PARTIAL, VARIABLE_STEP, MIN_SURFAREA,
    small ROUTING_STEP.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np

# --- output / raster ---
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_origin

# --- swmm-api model authoring ---
from swmm_api import SwmmInput
from swmm_api.input_file.section_labels import (
    OPTIONS,
    REPORT,
    RAINGAGES,
    SUBCATCHMENTS,
    SUBAREAS,
    INFILTRATION,
    TIMESERIES,
    STORAGE,
    OUTFALLS,
    CONDUITS,
    ORIFICES,
    XSECTIONS,
)
from swmm_api.input_file.sections.node import Storage, Outfall
from swmm_api.input_file.sections.link import Conduit, Orifice
from swmm_api.input_file.sections.link_component import CrossSection
from swmm_api.input_file.sections.subcatch import (
    SubCatchment,
    SubArea,
    InfiltrationHorton,
)
from swmm_api.input_file.sections.others import RainGage, TimeseriesData

# --- pyswmm headless run ---
from pyswmm import Simulation, Nodes


# ---------------------------------------------------------------------------
# Configuration (deterministic, no data fetch)
# ---------------------------------------------------------------------------
WORK = Path(__file__).resolve().parent / "_spike_out"
WORK.mkdir(parents=True, exist_ok=True)

N = 20  # grid is N x N cells
CELL = 10.0  # cell size [m]
CELL_AREA = CELL * CELL  # [m^2]
CELL_AREA_HA = CELL_AREA / 10_000.0  # [ha] for subcatchment area
MANNING_OVERLAND = 0.03
COND_HEIGHT = 3.0  # tall RECT_OPEN wall so it never surcharges shut
DEPTH_MAX = 5.0  # storage max depth [m]

# A pin we return: which kwarg toggles the native flap gate.
FLAP_KWARG = "has_flap_gate"  # Orifice/Outlet/Weir/Outfall in swmm-api 0.4.73


def build_dem() -> np.ndarray:
    """Tilted plane (drains toward the (N-1,N-1) low corner) + central pit.

    Returns elevation grid [m], shape (N, N). Row 0 / col 0 is HIGH.
    """
    ii, jj = np.meshgrid(np.arange(N), np.arange(N), indexing="ij")
    # Plane: high at (0,0), low at (N-1,N-1). Slope ~ 2% across the grid.
    slope = 0.02
    plane = 30.0 - slope * CELL * (ii + jj)
    # Central Gaussian depression.
    ci = cj = (N - 1) / 2.0
    r2 = (ii - ci) ** 2 + (jj - cj) ** 2
    pit_depth = 2.0
    pit_sigma = 3.0
    pit = pit_depth * np.exp(-r2 / (2.0 * pit_sigma**2))
    dem = plane - pit
    return dem.astype(float)


def cell_node(i: int, j: int) -> str:
    return f"S_{i}_{j}"


def build_model(dem: np.ndarray):
    """Assemble the quasi-2D SWMM model. Returns (inp, meta)."""
    inp = SwmmInput()

    # ---- OPTIONS ----
    inp[OPTIONS] = {
        "FLOW_UNITS": "CMS",
        "INFILTRATION": "HORTON",
        "FLOW_ROUTING": "DYNWAVE",
        "LINK_OFFSETS": "DEPTH",
        "MIN_SLOPE": 0,
        "ALLOW_PONDING": "YES",
        "SKIP_STEADY_STATE": "NO",
        "START_DATE": "01/01/2024",
        "START_TIME": "00:00:00",
        "REPORT_START_DATE": "01/01/2024",
        "REPORT_START_TIME": "00:00:00",
        "END_DATE": "01/01/2024",
        "END_TIME": "06:00:00",
        "SWEEP_START": "01/01",
        "SWEEP_END": "12/31",
        "DRY_DAYS": 0,
        "REPORT_STEP": "00:05:00",
        "WET_STEP": "00:01:00",
        "DRY_STEP": "00:05:00",
        "ROUTING_STEP": 2,  # seconds
        "RULE_STEP": "00:00:00",
        "INERTIAL_DAMPING": "PARTIAL",
        "NORMAL_FLOW_LIMITED": "BOTH",
        "FORCE_MAIN_EQUATION": "H-W",
        "VARIABLE_STEP": 0.75,
        "LENGTHENING_STEP": 0,
        "MIN_SURFAREA": 1.0,
        "MAX_TRIALS": 8,
        "HEAD_TOLERANCE": 0.0015,
        "SYS_FLOW_TOL": 5,
        "LAT_FLOW_TOL": 5,
        "MINIMUM_STEP": 0.5,
        "THREADS": 1,
    }

    # ---- REPORT ----
    inp[REPORT] = {"INPUT": "NO", "CONTROLS": "NO", "SUBCATCHMENTS": "NONE", "NODES": "ALL", "LINKS": "ALL"}

    # ---- RAINGAGE + hyetograph TIMESERIES (~100-yr-ish, 2 h, SCS-II shaped) ----
    # Total depth ~120 mm over 2 h, peak in the middle. mm/hr intensities at 5-min steps.
    n_steps = 24  # 24 * 5 min = 2 h
    peak = 11  # center index
    base_mmhr = 180.0  # peak intensity
    ts_data = []
    for k in range(n_steps):
        # Triangular-ish around the peak.
        frac = max(0.0, 1.0 - abs(k - peak) / 9.0)
        intensity = round(base_mmhr * frac, 3)
        mins = k * 5
        ts_data.append((f"{mins // 60:02d}:{mins % 60:02d}", intensity))
    inp.add_obj(TimeseriesData(name="HYET", data=ts_data))

    inp.add_obj(
        RainGage(
            name="RG",
            form="INTENSITY",
            interval="0:05",
            SCF=1.0,
            source="TIMESERIES",
            timeseries="HYET",
        )
    )

    # ---- STORAGE nodes (one per cell — ALL cells are storage) ----
    low_corner = (N - 1, N - 1)  # lowest cell; drains to the dedicated outfall
    for i in range(N):
        for j in range(N):
            elev = float(dem[i, j])
            inp.add_obj(
                Storage(
                    name=cell_node(i, j),
                    elevation=elev,
                    depth_max=DEPTH_MAX,
                    depth_init=0.0,
                    kind=Storage.TYPES.FUNCTIONAL,
                    data=[0.0, 0.0, CELL_AREA],  # A1=0, A2=0, A0=cell_area
                )
            )

    # ---- ONE dedicated boundary OUTFALL (FREE) below the low corner ----
    # SWMM rule: an outfall may have exactly ONE inlet link. So we attach a
    # separate outfall node and a single conduit from the low-corner cell.
    oi, oj = low_corner
    out_elev = float(dem[oi, oj]) - 1.0  # 1 m drop to drive free discharge
    inp.add_obj(Outfall(name="OUT", elevation=out_elev, kind=Outfall.TYPES.FREE))
    inp.add_obj(
        Conduit(
            name="L_OUTLET",
            from_node=cell_node(oi, oj),
            to_node="OUT",
            length=CELL,
            roughness=MANNING_OVERLAND,
            offset_upstream=0,
            offset_downstream=0,
        )
    )
    inp.add_obj(
        CrossSection(link="L_OUTLET", shape="RECT_OPEN", height=COND_HEIGHT, parameter_2=CELL)
    )

    # ---- SUBCATCHMENTS: one per cell, drain rain onto that cell's node ----
    for i in range(N):
        for j in range(N):
            sc_out = cell_node(i, j)  # drains onto its own node (storage or outfall)
            scname = f"C_{i}_{j}"
            inp.add_obj(
                SubCatchment(
                    name=scname,
                    rain_gage="RG",
                    outlet=sc_out,
                    area=CELL_AREA_HA,
                    imperviousness=100.0,
                    width=CELL,
                    slope=0.5,
                )
            )
            inp.add_obj(
                SubArea(
                    subcatchment=scname,
                    n_imperv=0.012,
                    n_perv=0.1,
                    storage_imperv=0.0,  # zero depression storage -> ~all rain runs off
                    storage_perv=0.0,
                    pct_zero=100,
                    route_to="OUTLET",
                )
            )
            inp.add_obj(
                InfiltrationHorton(
                    subcatchment=scname,
                    rate_max=0.0,
                    rate_min=0.0,
                    decay=0.0,
                    time_dry=0.0,
                    volume_max=0.0,
                )
            )

    # ---- Overland CONDUITS (4-connectivity) ----
    # Red wall: omit the conduit between two specific neighbor cells, chosen on
    # the UPHILL side of the central pit so water genuinely backs up behind it.
    wall_a = (8, 9)
    wall_b = (9, 9)  # downhill of wall_a; omitting this edge dams the (<=8, 9..) column flow
    wall_edge = frozenset({wall_a, wall_b})

    def add_conduit(a, b):
        ai, aj = a
        bi, bj = b
        # from = higher node, to = lower node (by elevation)
        if dem[ai, aj] >= dem[bi, bj]:
            frm, to = a, b
        else:
            frm, to = b, a
        cname = f"L_{frm[0]}_{frm[1]}__{to[0]}_{to[1]}"
        inp.add_obj(
            Conduit(
                name=cname,
                from_node=cell_node(*frm),
                to_node=cell_node(*to),
                length=CELL,
                roughness=MANNING_OVERLAND,
                offset_upstream=0,
                offset_downstream=0,
            )
        )
        inp.add_obj(
            CrossSection(link=cname, shape="RECT_OPEN", height=COND_HEIGHT, parameter_2=CELL)
        )
        return cname

    n_conduits = 0
    for i in range(N):
        for j in range(N):
            # east neighbor
            if j + 1 < N:
                e = frozenset({(i, j), (i, j + 1)})
                if e != wall_edge:
                    add_conduit((i, j), (i, j + 1))
                    n_conduits += 1
            # south neighbor
            if i + 1 < N:
                e = frozenset({(i, j), (i + 1, j)})
                if e != wall_edge:
                    add_conduit((i, j), (i + 1, j))
                    n_conduits += 1

    # ---- GREEN flap gate: native SWMM flap gate via ORIFICE has_flap_gate=True ----
    # Place between two cells where the static gradient would push flow BACKWARD
    # through the gate, so a flap proves it blocks reverse flow.
    # Orient orifice from_node = the cell on the "protected" (intended-no-inflow)
    # side, to_node = the wet side. SWMM flap gate blocks reverse (to->from) flow.
    flap_from = (3, 3)
    flap_to = (4, 3)
    # invert offset 0; SIDE orifice; height = full.
    fname = "FLAP1"
    inp.add_obj(
        Orifice(
            name=fname,
            from_node=cell_node(*flap_from),
            to_node=cell_node(*flap_to),
            orientation="SIDE",
            offset=0.0,
            discharge_coefficient=0.65,
            has_flap_gate=True,
            hours_to_open=0,
        )
    )
    inp.add_obj(
        CrossSection(link=fname, shape="RECT_CLOSED", height=COND_HEIGHT, parameter_2=CELL)
    )

    meta = {
        "wall_edge": (wall_a, wall_b),
        "flap_edge": (flap_from, flap_to),
        "flap_link": fname,
        "low_corner": low_corner,
        "n_conduits": n_conduits + 1,  # + the dedicated outlet conduit
    }
    return inp, meta


def run_sim(inp_path: Path):
    """Run pyswmm headless. Track the PEAK-volume full depth grid + final grid.

    The peak frame (max total stored depth) is what shows the wall/flap physics;
    the final frame is mostly drained. Returns a dict of run artifacts.
    """
    final_grid = np.full((N, N), np.nan)
    peak_grid = np.full((N, N), np.nan)
    peak_sum = -1.0
    peak_time = None
    t0 = time.time()
    last_dt = None
    n_steps = 0
    sample_every = 30  # routing steps between full-grid snapshots
    with Simulation(str(inp_path)) as sim:
        node_objs = Nodes(sim)
        prev = None
        k = 0
        for step in sim:
            n_steps += 1
            k += 1
            now = sim.current_time
            if prev is not None:
                last_dt = (now - prev).total_seconds()
            prev = now
            if k % sample_every == 0:
                g = np.array(
                    [[node_objs[cell_node(i, j)].depth for j in range(N)] for i in range(N)]
                )
                s = float(g.sum())
                if s > peak_sum:
                    peak_sum = s
                    peak_grid = g.copy()
                    peak_time = now
        for i in range(N):
            for j in range(N):
                final_grid[i, j] = node_objs[cell_node(i, j)].depth
    wall = time.time() - t0
    return {
        "final_grid": final_grid,
        "peak_grid": peak_grid,
        "peak_sum": peak_sum,
        "peak_time": peak_time,
        "wall_seconds": wall,
        "n_steps": n_steps,
        "last_dt": last_dt,
    }


def read_continuity(rpt_path: Path) -> float | None:
    """Parse Flow Routing Continuity % error from the .rpt."""
    txt = rpt_path.read_text()
    # The block header is "Flow Routing Continuity"; the error line:
    #   Continuity Error (%) .......        x.xxx
    in_block = False
    for line in txt.splitlines():
        if "Flow Routing Continuity" in line:
            in_block = True
            continue
        if in_block and "Continuity Error" in line:
            # last token is the number
            tok = line.replace(".", " ").split()
            # safer: take the trailing float
            import re

            m = re.search(r"(-?\d+\.\d+)\s*$", line.strip())
            if m:
                return float(m.group(1))
    return None


def read_runoff_continuity(rpt_path: Path) -> float | None:
    """Parse Surface Runoff Continuity % error (rain-in vs runoff-out)."""
    import re

    txt = rpt_path.read_text()
    in_block = False
    for line in txt.splitlines():
        if "Runoff Quantity Continuity" in line:
            in_block = True
            continue
        if in_block and "Continuity Error" in line:
            m = re.search(r"(-?\d+\.\d+)\s*$", line.strip())
            if m:
                return float(m.group(1))
    return None


def read_volumes(rpt_path: Path) -> dict:
    """Parse the Flow Routing Continuity volume table (10^6 ltr) for mass balance."""
    import re

    txt = rpt_path.read_text()
    lines = txt.splitlines()
    out = {}

    def grab(label, key):
        for ln in lines:
            if label in ln:
                nums = re.findall(r"(-?\d+\.\d+)", ln)
                if nums:
                    out[key] = float(nums[0])
                    return

    # Within the Flow Routing Continuity block.
    start = next((i for i, l in enumerate(lines) if "Flow Routing Continuity" in l), None)
    if start is None:
        return out
    block = lines[start : start + 30]

    def grab_block(label, key):
        for ln in block:
            if label in ln:
                nums = re.findall(r"(-?\d+\.\d+)", ln)
                if nums:
                    out[key] = float(nums[0])
                    return

    grab_block("Total Inflow", "inflow_Mltr")
    grab_block("External Outflow", "outflow_Mltr")
    grab_block("Flooding Loss", "flooding_Mltr")
    grab_block("Final Stored Volume", "stored_Mltr")
    return out


def flap_experiment(work: Path) -> dict:
    """Dedicated one-way proof: a 2-tank model with the gate between them.

    Two SWMM runs, otherwise identical, differing ONLY in which tank starts
    full of water:
      FORWARD : upstream tank (orifice from_node) full, downstream empty.
                A working gate PASSES -> downstream tank fills.
      REVERSE : downstream tank (orifice to_node) full, upstream empty.
                A working flap gate BLOCKS reverse flow -> upstream stays ~dry.

    Control: an identical pair using a plain CONDUIT (no flap) must let the
    REVERSE case leak back, proving the difference is the flap, not geometry.
    """
    from swmm_api import SwmmInput as _SI

    def build(structure: str, primed: str) -> Path:
        """structure in {'flap','conduit'}; primed in {'up','down'}."""
        inp = _SI()
        inp[OPTIONS] = {
            "FLOW_UNITS": "CMS",
            "FLOW_ROUTING": "DYNWAVE",
            "LINK_OFFSETS": "DEPTH",
            "START_DATE": "01/01/2024",
            "START_TIME": "00:00:00",
            "REPORT_START_DATE": "01/01/2024",
            "REPORT_START_TIME": "00:00:00",
            "END_DATE": "01/01/2024",
            "END_TIME": "00:30:00",
            "REPORT_STEP": "00:01:00",
            "ROUTING_STEP": 1,
            "INERTIAL_DAMPING": "PARTIAL",
            "VARIABLE_STEP": 0.75,
            "MIN_SURFAREA": 1.0,
            "MAX_TRIALS": 8,
            "HEAD_TOLERANCE": 0.0015,
            "ALLOW_PONDING": "YES",
            "THREADS": 1,
        }
        inp[REPORT] = {"NODES": "ALL", "LINKS": "ALL"}
        up_init = 2.0 if primed == "up" else 0.0
        down_init = 2.0 if primed == "down" else 0.0
        # Both tanks at the SAME invert so only the initial water (head) drives flow.
        inp.add_obj(Storage("UP", elevation=10.0, depth_max=5.0, depth_init=up_init,
                            kind=Storage.TYPES.FUNCTIONAL, data=[0.0, 0.0, 100.0]))
        inp.add_obj(Storage("DOWN", elevation=10.0, depth_max=5.0, depth_init=down_init,
                            kind=Storage.TYPES.FUNCTIONAL, data=[0.0, 0.0, 100.0]))
        # Sealed system (no outfall) so all motion is between the two tanks.
        # SWMM needs an outlet node; add a FREE outfall far below, gated shut by a
        # high-invert conduit so it never actually drains (keeps mass in the pair).
        inp.add_obj(Outfall("SINK", elevation=0.0, kind=Outfall.TYPES.FREE))
        inp.add_obj(Conduit("L_SINK", from_node="DOWN", to_node="SINK", length=10.0,
                            roughness=0.03, offset_upstream=4.9, offset_downstream=0))
        inp.add_obj(CrossSection(link="L_SINK", shape="RECT_OPEN", height=0.05, parameter_2=1.0))
        # The structure under test: gate allows UP->DOWN only.
        if structure == "flap":
            inp.add_obj(Orifice("GATE", from_node="UP", to_node="DOWN", orientation="SIDE",
                                offset=0.0, discharge_coefficient=0.65, has_flap_gate=True))
            inp.add_obj(CrossSection(link="GATE", shape="RECT_CLOSED", height=3.0, parameter_2=2.0))
        else:
            inp.add_obj(Conduit("GATE", from_node="UP", to_node="DOWN", length=5.0,
                                roughness=0.03, offset_upstream=0, offset_downstream=0))
            inp.add_obj(CrossSection(link="GATE", shape="RECT_OPEN", height=3.0, parameter_2=2.0))
        p = work / f"flap_{structure}_{primed}.inp"
        inp.write_file(str(p))
        return p

    def final_depths(inp_path: Path):
        with Simulation(str(inp_path)) as sim:
            nodes = Nodes(sim)
            for _ in sim:
                pass
            return float(nodes["UP"].depth), float(nodes["DOWN"].depth)

    results = {}
    for structure in ("flap", "conduit"):
        for primed in ("up", "down"):
            up_d, down_d = final_depths(build(structure, primed))
            results[f"{structure}_{primed}"] = {"UP": round(up_d, 4), "DOWN": round(down_d, 4)}
    return results


def rasterize_to_cog(depth_grid: np.ndarray, out_cog: Path):
    """Write the (N,N) depth grid to a COG, re-open to confirm transform/CRS."""
    # Synthetic projected CRS (UTM-like meters). Origin top-left.
    crs = CRS.from_epsg(32616)  # UTM 16N (arbitrary but valid projected meters)
    ox, oy = 500000.0, 4000000.0
    transform = from_origin(ox, oy, CELL, CELL)
    data = np.where(np.isnan(depth_grid), -9999.0, depth_grid).astype("float32")

    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": N,
        "width": N,
        "crs": crs,
        "transform": transform,
        "nodata": -9999.0,
        "tiled": True,
        "blockxsize": 16,
        "blockysize": 16,
        "compress": "deflate",
    }
    with rasterio.open(out_cog, "w", **profile) as dst:
        dst.write(data, 1)
        dst.build_overviews([2], rasterio.enums.Resampling.nearest)
        dst.update_tags(ns="rio_overview", resampling="nearest")

    # Re-open and confirm.
    with rasterio.open(out_cog) as src:
        confirm = {
            "crs": src.crs.to_string(),
            "transform": list(src.transform)[:6],
            "shape": (src.height, src.width),
            "nodata": src.nodata,
            "bounds": list(src.bounds),
        }
    return confirm


def main():
    dem = build_dem()
    inp, meta = build_model(dem)
    inp_path = WORK / "spike.inp"
    inp.write_file(str(inp_path))

    wall_a, wall_b = meta["wall_edge"]
    flap_from, flap_to = meta["flap_edge"]
    pit = (N // 2, N // 2)

    run = run_sim(inp_path)
    peak = run["peak_grid"]
    final = run["final_grid"]

    rpt_path = WORK / "spike.rpt"
    cont_err = read_continuity(rpt_path)
    runoff_err = read_runoff_continuity(rpt_path)
    vols = read_volumes(rpt_path)

    # --- dedicated one-way flap experiment ---
    flap_exp = flap_experiment(WORK)

    # --- raster / COG (rasterize the PEAK frame — the meaningful wet state) ---
    cog_path = WORK / "spike_depth.tif"
    cog_confirm = rasterize_to_cog(peak, cog_path)

    # --- PNG ---
    png_path = WORK / "spike_depth.png"
    fig, axes = plt.subplots(1, 2, figsize=(11, 5))
    im0 = axes[0].imshow(dem, cmap="terrain")
    axes[0].set_title("DEM (m)")
    fig.colorbar(im0, ax=axes[0], fraction=0.046)
    pg = np.where(np.isnan(peak), 0.0, peak)
    im1 = axes[1].imshow(pg, cmap="Blues", vmin=0)
    axes[1].set_title(f"Peak node depth (m) @ {run['peak_time']}")
    for (ci, cj), col in [(wall_a, "red"), (wall_b, "darkred"),
                          (flap_from, "lime"), (flap_to, "green")]:
        axes[1].plot(cj, ci, "s", color=col, markersize=8)
    fig.colorbar(im1, ax=axes[1], fraction=0.046)
    fig.tight_layout()
    fig.savefig(png_path, dpi=110)
    plt.close(fig)

    # --- wall physics: compare the walled column vs an UNwalled neighbor column ---
    # Walled edge is between (8,9)->(9,9). Neighbor control column j=8 (no wall).
    wcol = wall_a[1]
    ctrl_col = wcol - 1
    wall_up = float(peak[wall_a])
    wall_dn = float(peak[wall_b])
    ctrl_up = float(peak[wall_a[0], ctrl_col])
    ctrl_dn = float(peak[wall_b[0], ctrl_col])

    # --- flap interpretation (from the dedicated experiment) ---
    fwd = flap_exp["flap_up"]  # UP primed -> should pass to DOWN
    rev = flap_exp["flap_down"]  # DOWN primed -> flap should BLOCK back to UP
    ctrl_rev = flap_exp["conduit_down"]  # conduit control: should leak back to UP
    flap_passes_forward = fwd["DOWN"] > 0.1
    flap_blocks_reverse = rev["UP"] < 0.05
    control_leaks_reverse = ctrl_rev["UP"] > 0.1  # proves the difference is the flap

    # mass balance from rpt volume table
    inflow = vols.get("inflow_Mltr")
    outflow = vols.get("outflow_Mltr")
    flooding = vols.get("flooding_Mltr")
    stored = vols.get("stored_Mltr")

    result = {
        "n_cells": int(N * N),
        "n_storage_nodes": int(N * N),
        "n_conduits": meta["n_conduits"],
        "n_steps": run["n_steps"],
        "last_dt_s": run["last_dt"],
        "wall_seconds": round(run["wall_seconds"], 3),
        "peak_time": str(run["peak_time"]),
        "flap_kwarg": FLAP_KWARG,
        "continuity_error_pct": cont_err,
        "runoff_continuity_error_pct": runoff_err,
        "volumes_Mltr": vols,
        # wall test
        "wall_peak_upstream": wall_up,
        "wall_peak_downstream": wall_dn,
        "wall_blocks": wall_up > wall_dn,
        "control_col_peak_upstream": ctrl_up,
        "control_col_peak_downstream": ctrl_dn,
        "control_col_normal_downhill": ctrl_dn >= ctrl_up,
        # pit test
        "depth_pit_peak": float(peak[pit]),
        # flap test (dedicated experiment)
        "flap_experiment": flap_exp,
        "flap_passes_forward": flap_passes_forward,
        "flap_blocks_reverse": flap_blocks_reverse,
        "control_conduit_leaks_reverse": control_leaks_reverse,
        # mass balance
        "mass_inflow_Mltr": inflow,
        "mass_outflow_plus_stored_Mltr": (
            round((outflow or 0) + (stored or 0) + (flooding or 0), 4)
            if inflow is not None else None
        ),
        # artifacts
        "cog_path": str(cog_path),
        "png_path": str(png_path),
        "inp_path": str(inp_path),
        "rpt_path": str(rpt_path),
        "cog_confirm": cog_confirm,
        "wall_edge": [list(wall_a), list(wall_b)],
        "flap_edge": [list(flap_from), list(flap_to)],
        "signatures": {
            "Storage": "Storage(name, elevation, depth_max, depth_init, kind, *args, data=...)",
            "Outfall": "Outfall(name, elevation, kind, *args, data, has_flap_gate=False, route_to)",
            "Conduit": "Conduit(name, from_node, to_node, length, roughness, offset_upstream=0, offset_downstream=0, ...)  [NO flap kwarg]",
            "Orifice": "Orifice(name, from_node, to_node, orientation, offset, discharge_coefficient, has_flap_gate=False, hours_to_open=0)",
            "CrossSection": "CrossSection(link, shape, height=0, parameter_2=0, ...)",
            "SubCatchment": "SubCatchment(name, rain_gage, outlet, area, imperviousness, width, slope, ...)",
            "RainGage": "RainGage(name, form, interval, SCF, source, *args, timeseries=...)",
            "TimeseriesData": "TimeseriesData(name, data)",
        },
    }

    print(json.dumps(result, indent=2, default=str))
    (WORK / "spike_result.json").write_text(json.dumps(result, indent=2, default=str))
    return result


if __name__ == "__main__":
    main()
