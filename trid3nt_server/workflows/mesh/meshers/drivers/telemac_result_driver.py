"""In-container reader for a solved TELEMAC result file.

Runs INSIDE ``trid3nt-local/telemac:latest``, the only place the engine's own
``TelemacFile`` lives. The host mounts this file, the directory holding the
result, and an output directory, then shells it; nothing here imports trid3nt
code.

  python telemac_result_driver.py /data/config.json /data

Config key: ``slf`` - the result file's in-container path. Writes
``telemac_result_meta.json`` (what the engine reports about the header) beside
``telemac_result_fields.npz`` (node coordinates, the element table, the
instants, and one ``v<i>`` array per variable, shaped ``(frames, nodes)``).

The 3D quantities are the ones read: a 2D file reports the same numbers under
them, and a 3D file reports every plane, which is the shape its own postprocess
is handed. Variable names arrive as the engine states them, WITHOUT the unit the
record stores alongside - splitting the two is exactly the format knowledge this
driver exists to keep on the engine's side.
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, "/opt/conda/opentelemac/scripts/python3")

import numpy as np  # noqa: E402
from data_manip.extraction.telemac_file import TelemacFile  # noqa: E402

FIELDS_NAME = "telemac_result_fields.npz"
META_NAME = "telemac_result_meta.json"


def read_result(slf: str, out: str) -> dict:
    """One result file -> the fields on disk and the header as a dict."""
    res = TelemacFile(slf)
    try:
        varnames = [str(name) for name in res.varnames]
        arrays = {"x": np.asarray(res.meshx, dtype="float64"),
                  "y": np.asarray(res.meshy, dtype="float64"),
                  "ikle": np.asarray(res.ikle3, dtype="int64"),
                  "times": np.asarray(res.times, dtype="float64")}
        for index, name in enumerate(varnames):
            frames = [np.asarray(res.get_data_value(name, record),
                                 dtype="float64")
                      for record in range(res.ntimestep)]
            arrays[f"v{index}"] = (np.vstack(frames) if frames
                                   else np.empty((0, res.npoin3)))
        np.savez(out + "/" + FIELDS_NAME, **arrays)
        return {"varnames": varnames, "npoin": int(res.npoin3),
                "nelem": int(res.nelem3), "x_origin": int(res.x_orig),
                "y_origin": int(res.y_orig), "ntimestep": int(res.ntimestep),
                "fields": FIELDS_NAME}
    finally:
        res.close()


def main() -> int:
    cfg = json.load(open(sys.argv[1]))
    out = sys.argv[2].rstrip("/")
    meta = read_result(cfg["slf"], out)
    json.dump(meta, open(out + "/" + META_NAME, "w"), indent=2)
    print("TELEMAC_RESULT_OK", json.dumps(meta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
