"""One-shot harvest: build the geoclaw_seafloor_deformation.tif COG from the
completed FIRST Cascadia M9 solve's raw deformation_dz.asc (the composer crashed
on a disk-full COG write AFTER the solve uploaded, so the product COG was never
built). Uploads it under the inundation run_id so the proof --setup-id finds it.
Disk-light (reads a 1.7 MB ASCII, writes a small COG)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SOLVER_RUN = "01KZW4N9RDHKECRF8C1JHP9T3C"   # has _output/deformation_dz.asc
SETUP_RUN = "01KZW4N9PDXEGPKJXA5WTTFJD9"    # inundation run_id (proof --setup-id)

from trid3nt_server.workflows.solver.solver import (
    _get_runs_bucket, _get_s3_client,
)
from trid3nt_server.workflows.geoclaw.postprocess_geoclaw import (
    build_geoclaw_deformation_layer,
)


def main() -> int:
    bucket = _get_runs_bucket()
    s3 = _get_s3_client()
    tmp = Path(tempfile.mkdtemp(prefix="cascadia_defo_"))
    out = tmp / "_output"
    out.mkdir(parents=True)
    key = f"{SOLVER_RUN}/deformation_dz.asc"
    dest = out / "deformation_dz.asc"
    resp = s3.get_object(Bucket=bucket, Key=key)
    dest.write_bytes(resp["Body"].read())
    print("downloaded", key, dest.stat().st_size, "bytes")

    layer, scalars = build_geoclaw_deformation_layer(str(tmp), run_id=SETUP_RUN)
    if layer is None:
        print("FAILED: deformation layer None", scalars)
        return 1
    print("uri:", layer.uri)
    print("max_uplift_m:", scalars.get("max_uplift_m"))
    print("max_subsidence_m:", scalars.get("max_subsidence_m"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
