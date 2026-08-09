"""The pure-2D DECK COMPOSER -- assemble a complete, solvable HEC-RAS 6.6 deck
from any authored 2D mesh (carve OR the C# AuthorMesh path), source-agnostic.

This formalizes the deck assembly the ADR 0136/0137/0138 chain proved link by
link into ONE reusable composer. It takes a ``Mesh2D`` + ``SubgridTables`` (the
``hecras_geometry_writer`` inputs -- produced EITHER by ``carve_muncie`` from
Muncie's solver-proven arrays, OR by the C# ``AuthorMesh`` full-topology dump over
real terrain) plus the flow forcing, and writes the four deck files the production
Linux engines solve:

    <rundir>/<stem>.p04.tmp.hdf   plan HDF: Muncie plan skeleton with the 2D flow
                                  area REPLACED by the authored mesh, the Inflow +
                                  DS Boundary Condition Lines authored on the
                                  perimeter, the 1D<->2D coupling groups stripped,
                                  and the 2D-BC ``/Event Conditions`` forcing
                                  (Inflow = flow hydrograph, DS = normal-depth
                                  outlet -- the ADR 0138 partial-wetting physics).
    <rundir>/<stem>.x04           the Chippewa CLEAN pure-2D geometry preprocessor
                                  (dam-free fake reach; SA name + perimeter count
                                  patched to the authored mesh -- ADR 0137).
    <rundir>/<stem>.b04           the Chippewa boundary file (inert fake-reach hold;
                                  the REAL 2D forcing lives in Event Conditions).

Two deck files are still RE-USED, not authored from scratch, and this is a labeled
architectural fact (ADR 0137/0138): the plan HDF is COPIED from HEC's shipped
Muncie plan as a container (it carries Plan Data / run control / geompre metadata
the composer does not re-derive), and the ``.xNN`` is patched from the shipped
Chippewa clean pure-2D reference. Only the 2D-area subgroup, the BC lines, the
Event-Conditions forcing, and the flow ordinates are authored per-AOI.

The composer is mesh-SOURCE-agnostic: it never touches terrain, C#, or the carve.
The CARVE path (``build_chippewa_wetting_deck``) and the C# AuthorMesh path feed
the identical ``compose_pure2d_deck`` entry, so a deck that solves from one source
solves from the other (same writer, same engines). Acceptance (a) -- the Muncie
carve through this composer -- reproduces ADR 0138 (1906 wet / WSE 946.94 / the
monotone x1.5 delta) exactly.
"""
from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_HECRAS2025 = _HERE.parents[2]
for _p in (str(_HERE), str(_HECRAS2025)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from hecras_geometry_writer import (  # noqa: E402
    Mesh2D, SubgridTables, PropertyTableOptions, AREA_GROUP,
    write_2d_flow_area, write_boundary_condition_lines,
    BoundaryConditionLine, perimeter_face_run,
)
from hecras_pure2d_deck import patch_chippewa_xnn, patch_chippewa_bnn  # noqa: E402
from hecras_event_conditions import (  # noqa: E402
    write_flow_hydrograph_2d_bc, write_normal_depth_2d_bc,
    strip_1d_reach_bcs, finalize_event_conditions,
)
from hecras_infiltration import (  # noqa: E402
    build_infiltration_layer, write_infiltration_layer,
    write_percent_impervious, amc_to_int,
)
from hecras_meteorology import (  # noqa: E402
    write_uniform_precipitation, write_precipitation_interpolation,
    inject_precipitation_ascii, design_storm_units_and_rate,
)

#: The area name is reused from Muncie so the plan skeleton's Results paths + the
#: solve harness (``solve_freshtopo``) resolve unchanged.
AREA_NAME = "2D Interior Area"

#: Muncie plan skeleton reused as the plan-HDF container (ADR 0137 architecture).
MUNCIE_PLAN = (
    _HECRAS2025.parent
    / "hecras/fixtures/muncie_smoke/wrk_source/Muncie.p04.tmp.hdf"
)

#: Groups that make the copied Muncie plan a COMBINED 1D/2D deck; a pure-2D deck
#: strips them (the carved perimeter no longer lies on Muncie's weir/reference
#: lines -- left in they crash ``RasUnsteady`` in ``jobinit_lw_q2d``, ADR 0136).
_STRIP_2D_COUPLING = ["Structures", "Reference Lines", "2D Flow Area Break Lines"]

#: The composed plan's computational window mirrors the copied Muncie plan (25
#: hourly ordinates over one day, Interval=Days) so the EC hydrograph is
#: consistent with the plan's Plan Data (ADR 0138).
DEFAULT_START_DATE = "01Jan1900 2400"
DEFAULT_END_DATE = "02Jan1900 2400"
DEFAULT_N_ORD = 25

#: The plan-HDF path + attribute name the Linux engine reads the 2D solver from.
#: ``RasUnsteady`` prints "2D Unsteady <name> Equation Set" from this value
#: (ADR 0136), so the composed plan honours it with no ASCII .pXX and no image
#: rebuild -- it is a pure host-side h5py attribute on the copied plan skeleton.
_PLAN_PARAMS_GROUP = "Plan Data/Plan Parameters"
_EQUATION_SET_ATTR = "2D Equation Set"

#: The 2D equation sets the 6.x engine accepts. "Diffusion Wave" is the validated
#: default (every hecras_flood_2d acceptance solved with it, low volume error);
#: the SWE forms are the full-momentum shallow-water solvers (advanced, heavier,
#: less-tested on authored meshes).
EQUATION_SETS = ("Diffusion Wave", "SWE-ELM", "SWE-EM")
DEFAULT_EQUATION_SET = "Diffusion Wave"

#: The 2D computation (time-step) interval the RasUnsteady solver marches at, read
#: from the ``Computation Interval`` line of the ``.bNN`` boundary file (the shipped
#: Chippewa default is 2MIN). It is the primary numerical-stability knob: a
#: too-coarse step produces spurious water-surface oscillation spikes that shrink as
#: the step tightens (the stability-diagnostic convergence). Format is an integer +
#: a HEC-RAS unit token (SEC / MIN / HOUR), e.g. "30SEC", "1MIN", "5MIN".
_COMP_INTERVAL_RE = re.compile(r"^\d+(SEC|MIN|HOUR)$")
DEFAULT_COMPUTATION_INTERVAL = "2MIN"

#: Plan-HDF simulation window attrs (Plan Data/Plan Information). Rain-on-grid
#: patches the window to the storm duration so a CONSTANT-mode rate totals
#: ``rate * storm_duration_hr`` (the mass-balance-checkable design storm).
_PLAN_INFO_GROUP = "Plan Data/Plan Information"
_SIM_START = "02Jan1900 00:00:00"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _sim_end_from_duration(hours: float) -> str:
    """The ``DDMonYYYY HH:MM:SS`` end time ``hours`` after the fixed 02Jan1900 start."""
    import datetime as _dt
    start = _dt.datetime(1900, 1, 2, 0, 0, 0)
    end = start + _dt.timedelta(hours=float(hours))
    return f"{end.day:02d}{_MONTHS[end.month - 1]}{end.year} " \
           f"{end.hour:02d}:{end.minute:02d}:{end.second:02d}"


def _patch_sim_window(f, storm_duration_hr: float) -> str:
    """Set the plan's Simulation End Time / Time Window to a storm-duration window."""
    end = _sim_end_from_duration(storm_duration_hr)
    if _PLAN_INFO_GROUP in f:
        g = f[_PLAN_INFO_GROUP]
        g.attrs["Simulation Start Time"] = np.bytes_(_SIM_START)
        g.attrs["Simulation End Time"] = np.bytes_(end)
        g.attrs["Time Window"] = np.bytes_(f"{_SIM_START} to {end}")
    return end


def _patch_computation_interval(bnn_text: str, interval: str) -> str:
    """Rewrite the ``.bNN`` ``Computation Interval`` line to ``interval``.

    ``interval`` is validated against ``_COMP_INTERVAL_RE`` (integer + SEC/MIN/HOUR)
    -- the caller passing an unrecognized token is a hard error, not a silent
    fall-through (the loud-typed-fallback norm)."""
    interval = str(interval).strip().upper()
    if not _COMP_INTERVAL_RE.match(interval):
        raise ValueError(
            f"computation_interval {interval!r} must be an integer + SEC/MIN/HOUR "
            f"(e.g. '30SEC', '1MIN', '5MIN')")
    new, n = re.subn(r"(Computation Interval\s*=\s*)\S+", r"\g<1>" + interval,
                     bnn_text, count=1)
    if n != 1:
        raise ValueError("no 'Computation Interval' line found in the .bNN to patch")
    return new


@dataclass
class DeckPaths:
    """The four deck files a composed run consists of (the solve harness reads
    ``plan`` + the ``.x04``/``.b04`` siblings by stem)."""

    rundir: Path
    plan: Path
    xnn: Path
    bnn: Path


def default_hydrograph(peak_cfs: float, *, base_cfs: float = 200.0,
                       n_ord: int = DEFAULT_N_ORD):
    """A ``n_ord``-ordinate ramp: ``base_cfs`` -> ``peak_cfs`` over the first ~6 hrs,
    then hold. Time in DAYS over a one-day window (matches the plan skeleton).

    This is the composer's default forcing when the caller passes only a target
    peak; a caller with a real hydrograph (a USGS gauge / NWM peak / an Atlas-14
    derived event) passes explicit ``times``/``flows`` to ``compose_pure2d_deck``.
    """
    t = np.linspace(0.0, 1.0, int(n_ord))                    # days
    ramp = np.clip(t / (6.0 / 24.0), 0.0, 1.0)               # full by ~6 hrs
    q = base_cfs + (float(peak_cfs) - base_cfs) * ramp
    return t, q


def _bc_runs(mesh: Mesh2D, tables: SubgridTables, *, n_bc_faces: int,
             inflow_edge: str | None, ds_edge: str):
    """Select the Inflow + DS perimeter face runs.

    Inflow defaults to the LOWEST-elevation perimeter run (where water naturally
    enters); ``inflow_edge`` overrides to a compass side. DS is a second,
    non-overlapping run on ``ds_edge`` (default south) -- the normal-depth outlet
    so the domain DRAINS rather than fills (the ADR 0138 partial-wetting lesson:
    inflow + outlet = physical drainage). Raises if the two runs overlap.
    """
    if inflow_edge is not None:
        inflow_run = perimeter_face_run(mesh, edge=inflow_edge, n_faces=n_bc_faces)
    else:
        inflow_run = perimeter_face_run(
            mesh, min_elevation=tables.face_min_elevation, n_faces=n_bc_faces)
    ds_run = perimeter_face_run(mesh, edge=ds_edge, n_faces=n_bc_faces)
    if set(inflow_run) & set(ds_run):
        raise ValueError(
            "Inflow and DS perimeter runs overlap -- widen the mesh or pass a "
            "distinct inflow_edge/ds_edge (the outlet must not share the inlet faces)"
        )
    return inflow_run, ds_run


def compose_pure2d_deck(
    rundir: Path,
    mesh: Mesh2D,
    tables: SubgridTables,
    *,
    projection_wkt: str,
    area_name: str = AREA_NAME,
    stem: str = "Fresh2D",
    plan_template: Path = MUNCIE_PLAN,
    times=None,
    flows=None,
    target_peak_cfs: float | None = None,
    start_date: str = DEFAULT_START_DATE,
    end_date: str = DEFAULT_END_DATE,
    interval: str = "Days",
    ds_slope: float = 0.001,
    n_bc_faces: int = 14,
    inflow_edge: str | None = None,
    ds_edge: str = "s",
    equation_set: str = DEFAULT_EQUATION_SET,
    computation_interval: str | None = None,
    design_storm_mm_per_hr: float | None = None,
    storm_duration_hr: float = 6.0,
    curve_number: float | None = None,
    per_cell_cn2=None,
    amc_condition: str = "normal",
    ia_ratio: float = 0.2,
    min_infiltration_in_hr: float = 0.0,
    apply_infiltration: bool = False,
    opts: PropertyTableOptions | None = None,
) -> dict:
    """Assemble the complete pure-2D deck for ``mesh``/``tables`` in ``rundir``.

    Mesh SOURCE-agnostic: ``mesh``/``tables`` may come from the Muncie carve or
    the C# AuthorMesh dump. The forcing is either an explicit ``times``/``flows``
    hydrograph OR a ``target_peak_cfs`` (a depth-based default ramp via
    ``default_hydrograph``); one of the two must be given.

    ``equation_set`` selects the 2D solver stamped on the plan HDF: "Diffusion
    Wave" (default, validated -- low volume error on every acceptance) or a full
    shallow-water form ("SWE-ELM"/"SWE-EM"). The engine reads it from the plan
    HDF, so switching needs no ASCII plan and no image rebuild.

    ``computation_interval`` overrides the shipped Chippewa 2MIN time step marched
    by RasUnsteady (patched into the ``.bNN``), the primary numerical-stability
    knob: a coarse step over-shoots (spurious water-surface spikes) and tightening
    it converges the peak. ``None`` keeps the 2MIN default.

    Returns a provenance dict (mesh counts, BC line length + face count, the EC
    peak/ordinates, the equation set, the computation interval, the deck paths) for
    logging + the template's typed envelope.
    """
    import h5py

    if equation_set not in EQUATION_SETS:
        raise ValueError(
            f"equation_set {equation_set!r} not one of {EQUATION_SETS}")

    # Rain-on-grid: a uniform design storm over the whole 2D area REPLACES the
    # inflow hydrograph (no target_peak_cfs required); water is rain-fed and drains
    # through a single normal-depth outlet at the pour point.
    rain_on_grid = design_storm_mm_per_hr is not None
    if rain_on_grid:
        rain_rate, rain_units = design_storm_units_and_rate(design_storm_mm_per_hr)
        amc_i = amc_to_int(amc_condition)
        infil = build_infiltration_layer(
            int(mesh.cell_center_coord.shape[0]),
            curve_number=curve_number if per_cell_cn2 is None else None,
            per_cell_cn2=per_cell_cn2, amc=amc_i, ia_ratio=ia_ratio,
            min_infiltration_rate_in_hr=min_infiltration_in_hr,
        )
    else:
        if times is None or flows is None:
            if target_peak_cfs is None:
                raise ValueError(
                    "compose_pure2d_deck needs either an explicit times/flows "
                    "hydrograph or a target_peak_cfs default")
            times, flows = default_hydrograph(target_peak_cfs)
        times = np.asarray(times, np.float64).reshape(-1)
        flows = np.asarray(flows, np.float64).reshape(-1)

    rundir = Path(rundir)
    rundir.mkdir(parents=True, exist_ok=True)
    plan_path = rundir / f"{stem}.p04.tmp.hdf"
    xnn_path = rundir / f"{stem}.x04"
    bnn_path = rundir / f"{stem}.b04"
    shutil.copy2(plan_template, plan_path)

    with h5py.File(plan_path, "r+") as f:
        # Use the CALLER's CRS (the AOI's local ftUS WKT for a fresh AuthorMesh
        # AOI; Muncie's own WKT for the carve self-check -- so the carve path
        # stays byte-identical). STAMP it as the root Projection attr too: the
        # postprocess reads the model CRS from there to reproject the mesh to
        # 4326, so a fresh AOI must carry its OWN CRS or the depth COG mislocates
        # onto Muncie's footprint (the per-AOI geolocation fix).
        proj_wkt = projection_wkt
        try:
            existing = f.attrs["Projection"]
            existing = existing.decode() if isinstance(existing, bytes) else str(existing)
            if not proj_wkt:
                proj_wkt = existing
        except KeyError:
            pass
        f.attrs["Projection"] = np.bytes_(proj_wkt.encode("ascii", "replace"))
        # Stamp the 2D solver on the plan skeleton (the engine reads it here).
        if _PLAN_PARAMS_GROUP in f:
            f[_PLAN_PARAMS_GROUP].attrs[_EQUATION_SET_ATTR] = np.bytes_(equation_set)
        parent = f[AREA_GROUP]
        if area_name in parent:
            del parent[area_name]
        if "Attributes" in parent:
            del parent["Attributes"]
        write_2d_flow_area(
            f, area_name, mesh, tables, opts or PropertyTableOptions(),
            projection_wkt=proj_wkt,
        )

        if rain_on_grid:
            # ONE outlet BC line at the lowest-elevation perimeter run (the pour
            # point where the rain-fed interior drains); all other perimeter is a
            # closed wall (the watershed boundary). Mirrors the TELEMAC RoG design.
            outlet_run = perimeter_face_run(
                mesh, min_elevation=tables.face_min_elevation, n_faces=n_bc_faces)
            lines = [BoundaryConditionLine(
                name="Outlet", sa_2d=area_name, face_indices=outlet_run)]
        else:
            inflow_run, ds_run = _bc_runs(
                mesh, tables, n_bc_faces=n_bc_faces,
                inflow_edge=inflow_edge, ds_edge=ds_edge)
            lines = [
                BoundaryConditionLine(name="Inflow", sa_2d=area_name, face_indices=inflow_run),
                BoundaryConditionLine(name="DS", sa_2d=area_name, face_indices=ds_run),
            ]
        prov = write_boundary_condition_lines(f, lines, mesh)
        bc_len = prov["lines"][0]["length_ft"]
        for grp in _STRIP_2D_COUPLING:
            if grp in f["Geometry"]:
                del f["Geometry"][grp]

        # --- Event-Conditions forcing (the wetting link, ADR 0138) --- #
        n_stale = strip_1d_reach_bcs(f)
        if rain_on_grid:
            write_normal_depth_2d_bc(f, area_name, "Outlet", slope=ds_slope)
            xy = np.asarray(mesh.cell_center_coord, np.float64)
            met = write_uniform_precipitation(
                f, rate_mm_per_hr=rain_rate, duration_hr=storm_duration_hr,
                extents_xy=(float(xy[:, 0].min()), float(xy[:, 1].min()),
                            float(xy[:, 0].max()), float(xy[:, 1].max())),
                projection_wkt=proj_wkt)
            # The 2D-hydrology module (READ_UN_HYDROLOGY2D) is the FROZEN residual
            # (ADR 0205). The decoded precip interpolation folder (below) is READ
            # cleanly -- the engine passes the MetInterp segfault that blocked all 12
            # prior attempts -- but precip application (INIT_PRECIP2CELL ->
            # precip2fvcell) AND SCS-CN infiltration both route THROUGH the hydrology
            # module, which faults in its output-id/region setup (H5Gcreate2: invalid
            # location) -- a Windows-preprocessing coupling one layer below the precip
            # decode. So: apply_infiltration=True authors the byte-exact Infiltration
            # + Percent Impervious layers (offline-verified) and reaches the hydrology
            # fault; apply_infiltration=False omits them and the deck COMPLETES but
            # applies ZERO precipitation (hydrology skipped -> cells never linked).
            # Neither yet delivers a real rain-on-grid solve -- the residual is making
            # READ_UN_HYDROLOGY2D succeed (needs a NATE Windows reference plan HDF to
            # decode the hydrology output/region setup). Default OFF for a clean,
            # crash-free deck.
            if apply_infiltration:
                infil_prov = write_infiltration_layer(
                    f, area_name, infil, n_faces=int(mesh.faces_cell_indexes.shape[0]))
                write_percent_impervious(
                    f, area_name, n_faces=int(mesh.faces_cell_indexes.shape[0]), percent=0.0)
            else:
                infil_prov = None
            # Link 3: the per-area precip->cell interpolation folder MetInterp opens
            # (schema decoded byte-exact, ADR 0205). Cell arrays index 1:1 with the
            # geometry's Manning length (TOTAL cells incl. ghosts); faces = all faces.
            n_cells_total = int(f[f"{AREA_GROUP}/{area_name}/Cells Center Manning's n"].shape[0])
            interp_prov = write_precipitation_interpolation(
                f, area_name, n_cells=n_cells_total,
                n_faces=int(mesh.faces_cell_indexes.shape[0]))
            sim_end = _patch_sim_window(f, storm_duration_hr)
            finalize_event_conditions(f)
            ec = {"faces": int(len(outlet_run)), "peak_cfs": 0.0, "n_ordinates": 0}
        else:
            ec = write_flow_hydrograph_2d_bc(
                f, area_name, "Inflow", times, flows,
                start_date=start_date, end_date=end_date, interval=interval)
            write_normal_depth_2d_bc(f, area_name, "DS", slope=ds_slope)
            finalize_event_conditions(f)

    n_perim = int(mesh.perimeter.shape[0])
    xnn_path.write_text(patch_chippewa_xnn(area_name, n_perim))
    # The .bNN fake reach is an inert required-1D placeholder (the REAL 2D forcing
    # is in Event Conditions); a modest constant hold keeps the 1D block valid.
    # Seed the initial 2D water surface a few ft BELOW the AOI terrain minimum so
    # the area starts DRY (else the shipped Chippewa 679 ft profile stage floods any
    # AOI whose terrain sits below it -- the per-AOI initial-condition fix).
    terrain_min = float(np.nanmin(np.asarray(tables.cell_min_elevation, np.float64)))
    initial_stage = terrain_min - 10.0
    bnn_text = patch_chippewa_bnn(100.0, hydrograph_node=1, initial_stage=initial_stage)
    interval = computation_interval or DEFAULT_COMPUTATION_INTERVAL
    if computation_interval is not None:
        bnn_text = _patch_computation_interval(bnn_text, computation_interval)
    if rain_on_grid:
        # Persist the ASCII precipitation switch consistent with the plan-HDF
        # Meteorology group (the engine reads the HDF; the .bNN keeps the switch).
        bnn_text = inject_precipitation_ascii(bnn_text, rain_rate, rain_units)
    bnn_path.write_text(bnn_text)

    out = {
        "area_name": area_name,
        "equation_set": equation_set,
        "computation_interval": interval,
        "cells_real": int(mesh.cell_count),
        "cells_total": int(mesh.cell_center_coord.shape[0]),
        "faces": int(mesh.faces_cell_indexes.shape[0]),
        "perimeter_pts": n_perim,
        "bc_faces": int(ec["faces"]),
        "bc_length_ft": round(float(bc_len), 1),
        "ec_peak_cfs": round(float(ec["peak_cfs"]), 1),
        "ec_ordinates": int(ec["n_ordinates"]),
        "stale_1d_ec_removed": int(n_stale),
        "rain_on_grid": bool(rain_on_grid),
        "paths": DeckPaths(rundir=rundir, plan=plan_path, xnn=xnn_path, bnn=bnn_path),
    }
    if rain_on_grid:
        out.update({
            "design_storm_mm_per_hr": float(rain_rate),
            "storm_duration_hr": float(storm_duration_hr),
            "storm_total_mm": round(float(rain_rate) * float(storm_duration_hr), 2),
            "amc_condition": amc_to_int(amc_condition),
            "cn_min": infil_prov["cn_min"] if infil_prov else round(float(infil.curve_number.min()), 3),
            "cn_max": infil_prov["cn_max"] if infil_prov else round(float(infil.curve_number.max()), 3),
            "ia_ratio": infil_prov["ia_ratio"] if infil_prov else round(float(infil.abstraction_ratio[0]), 4),
            "infiltration_applied": bool(apply_infiltration),
            "sim_end": sim_end,
        })
    return out
