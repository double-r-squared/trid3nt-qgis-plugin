"""A solved TELEMAC result's fields, read by the engine's own library.

``read_selafin(path)`` returns the mesh and the per-variable time series every
postprocessor works from. The read happens INSIDE
``trid3nt-local/telemac:latest``, where ``TelemacFile`` lives: the host mounts
the file's directory read-only and a scratch directory to write into, shells the
driver with no network, and loads the arrays it left.

A parser on this side would be a second implementation of a format nobody here
owns, and the one this replaces had drifted twice: it refused a truncated result
the engine reads without complaint, and it handed every consumer a variable name
with the record's unit still glued to it.

One container round trip per file. A read costs roughly a second of startup on
top of the parse, which is the shape of the call sites: a postprocess reads its
result once and works in memory from there, so nothing loops a container.
"""

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

logger = logging.getLogger("trid3nt_server.workflows.telemac.result_reader")

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
    """A result file -> its mesh and per-variable time series::

        {"varnames": [str], "npoin": int, "nelem": int,
         "x": ndarray(npoin), "y": ndarray(npoin), "ikle": ndarray(nelem, ndp),
         "x_origin": int, "y_origin": int,
         "times": ndarray(nframes),
         "data": {varname: ndarray(nframes, npoin)}}

    ``varnames`` are the engine's own names, with no unit glued on. ``ikle`` is
    0-based, so a mesh-faithful render triangulates the file's real elements
    rather than an unconstrained Delaunay of the node cloud, which bridges river
    bends into a spurious fan.

    The origins are the header's, REPORTED and not applied: ``x``/``y`` stay
    exactly as the file stores them, because every postprocessor adds the origin
    it recovers from the domain bbox and applying it here would double the offset
    on all of them.
    """
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
            "x": fields["x"],
            "y": fields["y"],
            "ikle": fields["ikle"],
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
