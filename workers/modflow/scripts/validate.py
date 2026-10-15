"""In-image: the engine's own mf6 validate pass over the mounted run directory.

Inside the image where mf6 lives, importing nothing from trid3nt. mf6 reads ``mfsim.nam``
from its working directory - the mount point the launcher binds at ``/data`` - and
``--mode validate`` is the solve's own parse, one flag longer. stdout is mf6's own words
and the exit code is its verdict.
"""

from __future__ import annotations

import os
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    data_dir = (os.environ.get("TRID3NT_MODFLOW_DATA_DIR") or "").strip() or "/data"
    proc = subprocess.run(["mf6", "--mode", "validate"], cwd=data_dir)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
