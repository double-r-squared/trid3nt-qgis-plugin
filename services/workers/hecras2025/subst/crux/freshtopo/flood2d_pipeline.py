#!/usr/bin/env python3
"""hecras_flood_2d authoring+solve pipeline -- fresh-AOI DEM -> solved 2D deck.

The durable backend the ``hecras_flood_2d`` template orchestrates (ADR 0139
promotion). Ties the proven chain into ONE callable:

    fetched DEM (4326/projected, m)                                  [seam-1]
      -> prepare_terrain: reproject to a local ftUS CRS, m->ftUS elevation,
         mesh seeds (perimeter_ccw_open.f64 + centers.f64)           [flood2d_terrain]
      -> AUTHORING container (trid3nt-local/hecras2025-authoring):
         ras createterrain + AuthorMesh (TryCreateMesh topology +
         MeshPropertyTables.ComputeFrom subgrid tables over the terrain)
      -> adapter (authormesh_to_mesh2d) + composer (compose_pure2d_deck):
         the complete pure-2D deck, stamped with the AOI CRS               [hecras_deck2d]
      -> SOLVE container (trid3nt-local/hecras:latest): production 6.6
         RasGeomPreprocess + RasUnsteady on the fresh tessellation      [solve_freshtopo]

The two stages that need the proprietary natives run in DOCKER (the substituted
GDAL authoring image + the 6.6 solver image); the reproject/compose glue is pure
numpy/h5py/rasterio on the host. Returns the solved plan HDF path + provenance so
the caller (template) postprocesses it into the depth COG + mesh + inflow chart.

CLI (proofs):  python flood2d_pipeline.py <dem.tif> <workdir> --peak-cfs F --resolution-m M
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_HECRAS2025 = _HERE.parents[2]
for _p in (str(_HERE), str(_HECRAS2025)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from flood2d_terrain import prepare_terrain, TerrainPrep  # noqa: E402

#: Worker images (env-overridable, mirroring the other engine image envs).
AUTHORING_IMAGE_DEFAULT = "trid3nt-local/hecras2025-authoring:latest"
SOLVER_IMAGE_DEFAULT = "trid3nt-local/hecras:latest"


class Flood2dPipelineError(RuntimeError):
    """A pipeline stage failed (terrain / authoring / compose / solve)."""


@dataclass
class Flood2dResult:
    plan_hdf: str
    deck_dir: str
    dump_dir: str
    crs_wkt: str
    cells_real: int
    cells_total: int
    faces: int
    peak_cfs: float
    resolution_m: float
    terrain_min_ft: float
    terrain_max_ft: float
    bbox4326: list


def _docker_author(workdir: Path, prep: TerrainPrep, image: str) -> Path:
    """Run the authoring container over the prepared terrain + seeds -> dump dir.

    The ESRI ``.prj`` (the AOI's custom ftUS CRS -- no EPSG) is written beside the
    terrain; the entrypoint feeds it to ``createterrain -j``."""
    from pyproj import CRS

    prj = workdir / "terrain.prj"
    prj.write_text(CRS.from_wkt(prep.crs_wkt).to_wkt("WKT1_ESRI"))
    dump = workdir / "dump"
    # clear stale createterrain outputs (it refuses to overwrite)
    for stale in ("terrain.hdf", "nvalue.hdf", "terrain.terrain.tif", "nvalue.terrain.tif"):
        (workdir / stale).unlink(missing_ok=True)
    argv = [
        "docker", "run", "--rm",
        "-e", "TRID3NT_HECRAS_IN=/work",
        "-e", "TRID3NT_HECRAS_OUT=/work/dump",
        "-e", "TRID3NT_HECRAS_PRJ=/work/terrain.prj",
        "-v", f"{workdir}:/work", image,
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0 or not (dump / "faces.i32").exists():
        raise Flood2dPipelineError(
            f"authoring container failed (exit {proc.returncode}):\n"
            f"{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}"
        )
    if not (dump / "cell_elev.f32").exists():
        raise Flood2dPipelineError(
            "authoring produced topology but NO subgrid tables "
            "(MeshPropertyTables.ComputeFrom did not run) -- terrain sampling failed"
        )
    return dump


def _compose(dump: Path, deck_dir: Path, prep: TerrainPrep, peak_cfs: float,
             inflow_edge: str | None, ds_edge: str,
             equation_set: str = "Diffusion Wave") -> dict:
    from authormesh_to_mesh2d import load_authormesh
    from hecras_deck2d import compose_pure2d_deck

    res = load_authormesh(dump)
    info = compose_pure2d_deck(
        deck_dir, res.mesh, res.tables,
        projection_wkt=prep.crs_wkt, target_peak_cfs=float(peak_cfs),
        inflow_edge=inflow_edge, ds_edge=ds_edge, equation_set=equation_set,
    )
    info.pop("paths", None)
    return info


def _docker_solve(deck_dir: Path, image: str) -> dict:
    ft = str(_HERE)
    argv = [
        "docker", "run", "--rm",
        "-v", f"{deck_dir}:/run", "-v", f"{ft}:/ft:ro",
        "--entrypoint", "bash", image,
        "-lc", "/opt/trid3nt/.venv/bin/python /ft/solve_freshtopo.py /run",
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
    result_json = deck_dir / "freshtopo_result.json"
    if proc.returncode != 0 or not result_json.exists():
        raise Flood2dPipelineError(
            f"solve container failed (exit {proc.returncode}):\n"
            f"{proc.stdout[-2000:]}\n{proc.stderr[-1500:]}"
        )
    return json.loads(result_json.read_text())


def author_and_compose(
    dem_tif: str | Path,
    workdir: str | Path,
    *,
    peak_cfs: float = 5000.0,
    resolution_m: float = 60.0,
    manning_n: float = 0.06,
    inflow_edge: str | None = None,
    ds_edge: str = "s",
    equation_set: str = "Diffusion Wave",
    authoring_image: str = AUTHORING_IMAGE_DEFAULT,
) -> tuple[Flood2dResult, dict]:
    """Fetch-DEM-prep -> author (docker) -> compose the deck (NO solve).

    Returns ``(result, compose_info)`` -- the deck files are in ``result.deck_dir``
    (``Fresh2D.p04.tmp.hdf`` + ``.x04`` + ``.b04``), ready to stage as run_solver
    inputs (the M3-gate no-archetype path in the hecras worker) OR to solve
    directly via ``_docker_solve``. This is the template's authoring stage; the
    solve dispatches through the generic run_solver seam. ``equation_set`` selects
    the 2D solver stamped on the plan HDF (Diffusion Wave default / SWE forms)."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    prep = prepare_terrain(dem_tif, workdir, resolution_m=resolution_m, manning_n=manning_n)
    dump = _docker_author(workdir, prep, authoring_image)
    deck_dir = workdir / "deck"
    info = _compose(dump, deck_dir, prep, peak_cfs, inflow_edge, ds_edge, equation_set)
    result = Flood2dResult(
        plan_hdf=str(deck_dir / "Fresh2D.p04.tmp.hdf"), deck_dir=str(deck_dir),
        dump_dir=str(dump), crs_wkt=prep.crs_wkt,
        cells_real=int(info["cells_real"]), cells_total=int(info["cells_total"]),
        faces=int(info["faces"]), peak_cfs=float(peak_cfs), resolution_m=float(resolution_m),
        terrain_min_ft=prep.terrain_min_ft, terrain_max_ft=prep.terrain_max_ft,
        bbox4326=prep.bbox4326,
    )
    return result, info


def run_flood2d(
    dem_tif: str | Path,
    workdir: str | Path,
    *,
    peak_cfs: float = 5000.0,
    resolution_m: float = 60.0,
    manning_n: float = 0.06,
    inflow_edge: str | None = None,
    ds_edge: str = "s",
    equation_set: str = "Diffusion Wave",
    authoring_image: str = AUTHORING_IMAGE_DEFAULT,
    solver_image: str = SOLVER_IMAGE_DEFAULT,
) -> tuple[Flood2dResult, dict]:
    """Author + solve a fresh-AOI 2D flood deck IN PROCESS (direct-call proof)."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    prep = prepare_terrain(dem_tif, workdir, resolution_m=resolution_m, manning_n=manning_n)
    dump = _docker_author(workdir, prep, authoring_image)
    deck_dir = workdir / "deck"
    info = _compose(dump, deck_dir, prep, peak_cfs, inflow_edge, ds_edge, equation_set)
    metrics = _docker_solve(deck_dir, solver_image)
    result = Flood2dResult(
        plan_hdf=str(deck_dir / "Fresh2D.p04.tmp.hdf"), deck_dir=str(deck_dir),
        dump_dir=str(dump), crs_wkt=prep.crs_wkt,
        cells_real=int(info["cells_real"]), cells_total=int(info["cells_total"]),
        faces=int(info["faces"]), peak_cfs=float(peak_cfs), resolution_m=float(resolution_m),
        terrain_min_ft=prep.terrain_min_ft, terrain_max_ft=prep.terrain_max_ft,
        bbox4326=prep.bbox4326,
    )
    return result, metrics


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dem_tif")
    ap.add_argument("workdir")
    ap.add_argument("--peak-cfs", type=float, default=5000.0)
    ap.add_argument("--resolution-m", type=float, default=60.0)
    ap.add_argument("--inflow-edge", default=None)
    ap.add_argument("--ds-edge", default="s")
    ap.add_argument("--equation-set", default="Diffusion Wave")
    args = ap.parse_args()
    result, metrics = run_flood2d(
        args.dem_tif, args.workdir, peak_cfs=args.peak_cfs,
        resolution_m=args.resolution_m, inflow_edge=args.inflow_edge, ds_edge=args.ds_edge,
        equation_set=args.equation_set,
    )
    print(json.dumps({"result": asdict(result), "solve": metrics}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
