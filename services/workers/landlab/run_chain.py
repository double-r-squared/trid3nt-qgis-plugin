"""Landlab worker CLI shim -- run a build_spec against a staged DEM (local build).

The out-of-process (local-exec) lane of the Landlab surface-process engine.
Mirrors ``services/workers/swmm/run_inp.py``: a thin solver shim that accepts
a manifest JSON on the CLI, builds the Landlab grid from the staged DEM, runs
the documented component chain, and writes the output field COG to CWD.

The SUPERVISOR (in the agent process) handles all S3/file I/O, completion.json
writing, and stdout/stderr upload -- this shim only runs the numerical core and
exits 0 on success, non-zero on failure. Artifacts produced here are picked up
by the supervisor's output-glob patterns.

Usage:
    python run_chain.py --manifest manifest.json

The manifest must be in the SAME shape the Landlab Batch worker entrypoint
uses (schema documented in services/workers/landlab/entrypoint.py), but with
all ``gs_uri`` values pointing to paths already staged in the CWD (the
supervisor downloaded them before launching this shim):

    {
      "inputs": [{"gs_uri": "/path/to/dem.tif", "dest": "dem.tif"}],
      "dem_dest": "dem.tif",
      "build_spec": { ... }
    }

Outputs (written to CWD):
    landlab_field.tif   -- primary output field COG (always)
    landlab_secondary_<token>.tif  -- per secondary field (if any)

Exit codes:
    0  -- chain ran to completion and wrote the field COG
    1  -- chain or I/O failure (see stderr)
    2  -- manifest / DEM not found or malformed args
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

LOG = logging.getLogger("grace2.worker.landlab.run_chain")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

#: Default field COG filename (mirrors entrypoint.FIELD_COG_NAME).
FIELD_COG_NAME = "landlab_field.tif"


def _secondary_cog_name(token: str) -> str:
    return f"landlab_secondary_{token}.tif"


def run_chain(manifest_path: str) -> int:
    """Read the manifest, build the grid from the staged DEM, run the chain.

    Returns 0 on success, non-zero on failure. All output COGs are written to
    the directory containing ``manifest_path`` (the rundir).
    """
    mpath = Path(manifest_path)
    if not mpath.exists():
        sys.stderr.write(f"run_chain.py: manifest not found: {manifest_path}\n")
        return 2

    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.stderr.write(f"run_chain.py: could not parse manifest: {exc}\n")
        return 2

    build_spec: dict = manifest.get("build_spec") or {}
    dem_dest: str = manifest.get("dem_dest") or "dem.tif"
    cwd = mpath.parent
    dem_path = cwd / dem_dest
    if not dem_path.exists():
        sys.stderr.write(f"run_chain.py: DEM not found: {dem_path}\n")
        return 2

    # Import the entrypoint's DEM read + chain run helpers (they live in the
    # same services.workers.landlab package -- the repo root must be on PYTHONPATH,
    # which the LocalSolverSpec's env_overrides handles).
    try:
        from services.workers.landlab.entrypoint import (  # type: ignore[import]
            _read_dem_for_grid,
            _write_field_cog,
        )
        from services.workers.landlab.component_chain import run_component_chain  # type: ignore[import]
    except ImportError as exc:
        sys.stderr.write(
            f"run_chain.py: could not import landlab worker modules -- "
            f"is PYTHONPATH set to the GRACE-2 repo root? ({exc})\n"
        )
        return 2

    target_res = float(build_spec.get("target_resolution_m", 30.0))
    try:
        dem, resolution_m, transform, crs = _read_dem_for_grid(dem_path, target_res)
    except Exception as exc:
        sys.stderr.write(f"run_chain.py: DEM read failed: {exc}\n")
        return 1

    try:
        chain = run_component_chain(
            dem, resolution_m=resolution_m, build_spec=build_spec
        )
    except Exception as exc:
        sys.stderr.write(f"run_chain.py: component chain failed: {exc}\n")
        return 1

    field_cog = cwd / FIELD_COG_NAME
    try:
        _write_field_cog(chain.field, field_cog, transform=transform, crs=crs)
        LOG.info("wrote primary field COG -> %s", field_cog)
    except Exception as exc:
        sys.stderr.write(f"run_chain.py: field COG write failed: {exc}\n")
        return 1

    # Write secondary fields (mirrors entrypoint logic).
    try:
        import numpy as _np

        for token, grid_field in (chain.secondary_fields or {}).items():
            arr = _np.asarray(grid_field, dtype="float64")
            if arr.size == 0 or not _np.any(_np.isfinite(arr)):
                continue
            sec_name = _secondary_cog_name(token)
            _write_field_cog(arr, cwd / sec_name, transform=transform, crs=crs)
            LOG.info("wrote secondary field COG -> %s", cwd / sec_name)
    except Exception as exc:
        # Non-fatal: the primary field succeeded.
        sys.stderr.write(f"run_chain.py: secondary field write failed (non-fatal): {exc}\n")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grace2-landlab-run-chain",
        description="GRACE-2 Landlab chain runner (local subprocess shim).",
    )
    parser.add_argument(
        "--manifest",
        default="manifest.json",
        help="Path to the manifest JSON (default: manifest.json in CWD).",
    )
    args = parser.parse_args(argv)
    return run_chain(args.manifest)


if __name__ == "__main__":
    raise SystemExit(main())
