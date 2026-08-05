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
    opts: PropertyTableOptions | None = None,
) -> dict:
    """Assemble the complete pure-2D deck for ``mesh``/``tables`` in ``rundir``.

    Mesh SOURCE-agnostic: ``mesh``/``tables`` may come from the Muncie carve or
    the C# AuthorMesh dump. The forcing is either an explicit ``times``/``flows``
    hydrograph OR a ``target_peak_cfs`` (a depth-based default ramp via
    ``default_hydrograph``); one of the two must be given.

    Returns a provenance dict (mesh counts, BC line length + face count, the EC
    peak/ordinates, the deck paths) for logging + the template's typed envelope.
    """
    import h5py

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
        projection = f.attrs["Projection"]
        proj_wkt = (projection.decode() if isinstance(projection, bytes)
                    else projection) or projection_wkt
        parent = f[AREA_GROUP]
        if area_name in parent:
            del parent[area_name]
        if "Attributes" in parent:
            del parent["Attributes"]
        write_2d_flow_area(
            f, area_name, mesh, tables, opts or PropertyTableOptions(),
            projection_wkt=proj_wkt,
        )

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

        # --- the 2D-BC-line Event-Conditions forcing (the wetting link, ADR 0138) ---
        n_stale = strip_1d_reach_bcs(f)
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
    bnn_path.write_text(patch_chippewa_bnn(
        100.0, hydrograph_node=1, initial_stage=initial_stage))

    return {
        "area_name": area_name,
        "cells_real": int(mesh.cell_count),
        "cells_total": int(mesh.cell_center_coord.shape[0]),
        "faces": int(mesh.faces_cell_indexes.shape[0]),
        "perimeter_pts": n_perim,
        "bc_faces": int(ec["faces"]),
        "bc_length_ft": round(float(bc_len), 1),
        "ec_peak_cfs": round(float(ec["peak_cfs"]), 1),
        "ec_ordinates": int(ec["n_ordinates"]),
        "stale_1d_ec_removed": int(n_stale),
        "paths": DeckPaths(rundir=rundir, plan=plan_path, xnn=xnn_path, bnn=bnn_path),
    }
