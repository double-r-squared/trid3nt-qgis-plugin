"""A solved TELEMAC result's fields, read by the engine's own library.

The read happens INSIDE the TELEMAC image, where ``TelemacFile`` lives: one
container round trip per file, with the file's directory mounted read-only, a
scratch directory to write into and no network. Nothing here loops a container."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir

logger = logging.getLogger("trid3nt_server.workflows.telemac.products.result_reader")

__all__ = ["SelafinReadError", "read_selafin"]

_TELEMAC_IMAGE_DEFAULT = "trid3nt-local/telemac:latest"
_INCONTAINER_SCRIPT = "telemac_result_driver.py"
_CONTAINER_TIMEOUT_S = 1800
_FIELDS_NAME = "telemac_result_fields.npz"
_META_NAME = "telemac_result_meta.json"


class SelafinReadError(RuntimeError):
    """The engine's reader could not open the result file."""

    error_code = "TELEMAC_RESULT_READ_FAILED"


def read_selafin(path: str | Path) -> dict[str, Any]:
    """A result file -> its mesh and per-variable time series.

    ``varnames`` carry no unit, ``ikle`` is 0-based, origins are not applied."""
    # {"varnames": [str], "npoin": int, "nelem": int,
    #  "x": ndarray(npoin2), "y": ndarray(npoin2), "ikle": ndarray(nelem, ndp),
    #  "nplan": int, "npoin2": int, "nelem2": int, "ikle2": ndarray(nelem2, 3),
    #  "x_origin": int, "y_origin": int, "times": ndarray(nframes),
    #  "data": {varname: ndarray(nframes, npoin)}}
    # ``x``/``y`` stay exactly as the file stores them: every postprocessor adds
    # the origin it recovers from the domain bbox, and applying it here would
    # double the offset on all of them.
    import numpy as np

    slf = Path(path).resolve()
    scratch = Path(tempfile.mkdtemp(prefix="telemac-read-"))
    try:
        meta = _run_driver(slf, scratch)
        fields = np.load(scratch / _FIELDS_NAME)
        varnames = [str(name) for name in meta["varnames"]]
        return {
            "varnames": varnames,
            "npoin": int(meta["npoin"]),
            "nelem": int(meta["nelem"]),
            # The vertical shape a 3D result carries. A 3D field is flat over
            # NPOIN3 and is NPLAN planes stacked over the 2D mesh, bottom first;
            # a 2D file reports one plane and the same mesh twice.
            "nplan": int(meta.get("nplan", 1)),
            "npoin2": int(meta.get("npoin2", meta["npoin"])),
            "nelem2": int(meta.get("nelem2", meta["nelem"])),
            "x": fields["x"],
            "y": fields["y"],
            "ikle": fields["ikle"],
            "ikle2": fields["ikle2"] if "ikle2" in fields else fields["ikle"],
            "x_origin": int(meta["x_origin"]),
            "y_origin": int(meta["y_origin"]),
            "times": fields["times"],
            "data": {name: fields[f"v{index}"]
                     for index, name in enumerate(varnames)},
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run_driver(slf: Path, scratch: Path) -> dict[str, Any]:
    """One driver run in the TELEMAC box -> the header it reported."""
    image = os.environ.get("TRID3NT_TELEMAC_IMAGE") or _TELEMAC_IMAGE_DEFAULT
    config = scratch / "telemac_result_config.json"
    config.write_text(json.dumps({"slf": f"/in/{slf.name}"}))
    argv = [
        "docker", "run", "--rm", "--network", "none",
        "-v", f"{drivers_dir()}:/drivers:ro", "-v", f"{slf.parent}:/in:ro",
        "-v", f"{scratch}:/data", image, "python",
        f"/drivers/{_INCONTAINER_SCRIPT}", f"/data/{config.name}", "/data"]
    logger.info("telemac result read: %s", " ".join(argv))
    cp = subprocess.run(argv, capture_output=True, text=True,
                        timeout=_CONTAINER_TIMEOUT_S)
    if cp.returncode != 0:
        raise SelafinReadError(
            f"the engine's own reader could not open {slf.name} "
            f"(rc={cp.returncode}):\n{cp.stdout[-2000:]}\n{cp.stderr[-2000:]}")
    return json.loads((scratch / _META_NAME).read_text())
