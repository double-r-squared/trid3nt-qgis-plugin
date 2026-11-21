"""One PEST++ trial of a MODFLOW study: each pilot-point array laid off its points' values, then mf6.

Runs inside the pestpp image in the trial's directory, importing nothing from trid3nt. Where the study wrote
study_pilot_points.json, each array it lists is the run's own values times 10 ** (each cell's kriging factors over the
log10 of the pilot-point values PEST wrote), written into the array's own file; then mf6 replaces this process.
"""

from __future__ import annotations

import json
import os

PILOT_POINTS = "study_pilot_points.json"


def lay(directory: str) -> list[str]:
    """Each pilot-point array the spec lists, laid into its own file -> the files laid."""
    import numpy as np

    path = os.path.join(directory, PILOT_POINTS)
    if not os.path.isfile(path):
        return []
    laid = []
    for entry in json.load(open(path))["arrays"]:
        held = np.load(os.path.join(directory, entry["factors"]))
        with open(os.path.join(directory, entry["values"])) as fh:
            stated = dict(line.split()[:2] for line in fh if line.strip())
        values = np.log10(np.array([float(stated[str(n)]) for n in held["names"]]))
        field = held["base"] * 10.0 ** (held["weight"] * values[held["index"]]).sum(axis=1)
        with open(os.path.join(directory, entry["array"]), "w") as fh:
            fh.write("\n".join(repr(float(v)) for v in field) + "\n")
        laid.append(entry["array"])
    return laid


def main() -> None:
    laid = lay(os.getcwd())
    if laid:
        print("TRIAL_PILOT_POINTS", json.dumps(laid), flush=True)
    os.execvp("mf6", ["mf6"])


if __name__ == "__main__":
    main()
