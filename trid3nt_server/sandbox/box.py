"""The code-exec box: a staged workdir in, one constrained container run, results out.

Every byte a snippet reads is staged into the run directory HERE, before the
container starts, so a world-read stays on the substrate's own gate-visible
fetch path and the analysis runs on what it was handed. The run directory dies
with the run.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

__all__ = ["submit_sandbox_job"]

logger = logging.getLogger("trid3nt_server.sandbox.box")

#: The one image the playground runs in: the lightest box in the tree carrying
#: the whole analytical stack a snippet may import (numpy, pandas, scipy,
#: rasterio, geopandas, shapely, matplotlib).
_IMAGE_DEFAULT = "trid3nt-local/mesh:latest"
_DRIVER = Path(__file__).resolve().parent / "driver.py"
#: The container IS the wallclock bound - there is no in-process alarm to race
#: and nothing to defeat by installing a signal handler.
_CAP_SECONDS = 60
_MEMORY = "2g"
_PIDS_LIMIT = "256"
_CPUS = "2"


def submit_sandbox_job(python_code: str, layer_refs: dict[str, Any] | None = None,
                       *, timeout_seconds: int | None = None) -> dict[str, Any]:
    """Run ``python_code`` over the staged ``layer_refs`` in the box -> its envelope."""
    cap = int(timeout_seconds or _CAP_SECONDS)
    workdir = Path(tempfile.mkdtemp(prefix="trid3nt_sandbox_"))
    started = time.monotonic()
    try:
        (workdir / "staged").mkdir()
        refs, misses = _stage(layer_refs or {}, workdir / "staged")
        (workdir / "payload.json").write_text(
            json.dumps({"python_code": python_code, "layer_refs": refs}),
            encoding="utf-8")
        envelope = _run_container(workdir, cap)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    envelope["duration_s"] = round(time.monotonic() - started, 3)
    envelope["wallclock_cap_seconds"] = cap
    if misses:
        envelope["layer_errors"] = {**misses, **(envelope.get("layer_errors") or {})}
    return envelope


def _stage(layer_refs: dict[str, Any],
           staged: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """Every ref materialized under ``staged`` -> the box-side paths, and the misses.

    A ref that cannot be staged is handed through as its original string and
    named in the misses: the snippet gets the reason rather than a crash.
    """
    refs: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for var, ref in layer_refs.items():
        many = isinstance(ref, (list, tuple))
        paths: list[Any] = []
        for i, one in enumerate(list(ref) if many else [ref]):
            path, miss = _stage_one(one, staged, f"{var}_{i}" if many else var)
            paths.append(path)
            if miss:
                errors[f"{var}[{i}]" if many else var] = miss
        refs[var] = paths if many else paths[0]
    return refs, errors


def _stage_one(uri: Any, staged: Path, label: str) -> tuple[Any, str | None]:
    if not isinstance(uri, str):
        return uri, None
    tail = "".join(c if (c.isalnum() or c in "._-") else "_"
                   for c in uri.rstrip("/").rsplit("/", 1)[-1])
    dest = staged / f"{label}_{tail}"
    try:
        if uri.startswith("s3://"):
            from trid3nt_server.tools.cache import read_object_bytes_s3
            dest.write_bytes(read_object_bytes_s3(uri))
        elif os.path.isfile(uri):
            try:
                os.link(uri, dest)
            except OSError:
                shutil.copy2(uri, dest)
        else:
            return uri, None
    except Exception as exc:
        return uri, f"{type(exc).__name__}: {exc}"
    return f"/work/staged/{dest.name}", None


def _run_container(workdir: Path, cap: int) -> dict[str, Any]:
    """One box run -> the envelope its driver wrote, or an honest stand-in.

    The container is named so the timeout can kill IT: killing the client that
    launched it would leave the run alive and the cap unenforced.
    """
    name = f"trid3nt-sandbox-{uuid.uuid4().hex[:12]}"
    argv = ["docker", "run", "--rm", "--name", name,
            "--network", "none",
            "--memory", _MEMORY, "--memory-swap", _MEMORY,
            "--pids-limit", _PIDS_LIMIT, "--cpus", _CPUS,
            "-e", "HOME=/tmp", "-e", "MPLCONFIGDIR=/tmp", "-e", "MPLBACKEND=Agg",
            "-v", f"{_DRIVER}:/driver.py:ro", "-v", f"{workdir}:/work",
            "--entrypoint", "python",
            os.environ.get("TRID3NT_SANDBOX_IMAGE") or _IMAGE_DEFAULT,
            "/driver.py", "/work/payload.json", "/work/result.json"]
    logger.info("sandbox box run: %s", " ".join(argv))
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=cap)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True, text=True)
        return _stand_in("timeout", f"the snippet exceeded its {cap}s wallclock cap")
    result = workdir / "result.json"
    if result.is_file():
        return json.loads(result.read_text(encoding="utf-8"))
    return _stand_in("error", f"the box wrote no result (exit {done.returncode}): "
                              f"{(done.stderr or '')[-1000:]}")


def _stand_in(status: str, error: str) -> dict[str, Any]:
    return {"stdout": "", "stderr": error, "status": status, "error": error,
            "result": {"kind": "none", "value": None},
            "stdout_truncated": False, "stderr_truncated": False}
