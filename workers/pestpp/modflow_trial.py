"""One PEST++ trial of a MODFLOW study: each pilot-point array and zoned list column laid off its values, then mf6.

Runs inside the pestpp image in the trial's directory, importing nothing from trid3nt. Where the study wrote
study_pilot_points.json, each array it lists is the run's own values times 10 ** (each cell's kriging factors over the
log10 of the pilot-point values PEST wrote), written into the array's own file, and each list column it lists has the
selected rows' own values multiplied by the zone's parameter; then mf6 replaces this process.
"""

from __future__ import annotations

import json
import os
import re

PILOT_POINTS = "study_pilot_points.json"


def _tokens(text: str) -> list[str]:
    """A line's tokens, a template field one token with the spaces inside its delimiters."""
    return re.findall(r"~[^~]*~|[^\s~]+", text.split("#", 1)[0])


def lay(directory: str) -> list[str]:
    """Each pilot-point array and zoned list column the spec lists, laid -> the files laid."""
    import numpy as np

    path = os.path.join(directory, PILOT_POINTS)
    if not os.path.isfile(path):
        return []
    spec = json.load(open(path))
    laid = []
    for entry in spec.get("arrays", []):
        held = np.load(os.path.join(directory, entry["factors"]))
        with open(os.path.join(directory, entry["values"])) as fh:
            stated = dict(line.split()[:2] for line in fh if line.strip())
        values = np.log10(np.array([float(stated[str(n)]) for n in held["names"]]))
        field = held["base"] * 10.0 ** (held["weight"] * values[held["index"]]).sum(axis=1)
        with open(os.path.join(directory, entry["array"]), "w") as fh:
            fh.write("\n".join(repr(float(v)) for v in field) + "\n")
        laid.append(entry["array"])
    for entry in spec.get("columns", []):
        with open(os.path.join(directory, entry["values"])) as fh:
            stated = dict(line.split()[:2] for line in fh if line.strip())
        value = float(stated[entry["name"]])
        target = os.path.join(directory, entry["file"])
        with open(target) as fh:
            lines = fh.read().splitlines()
        for at in entry["lines"]:
            tokens = _tokens(lines[at])
            if entry["cell_start"] is None:
                column, width = entry["fixed"], 1
            else:
                width = 1 if tokens[entry["cell_start"]].upper() == "NONE" else entry["width"]
                column = entry["cell_start"] + width + entry["away"]
            tokens[column] = repr(float(tokens[column]) * value)
            lines[at] = "  " + "  ".join(tokens)
        with open(target, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        laid.append(entry["file"])
    return laid


def main() -> None:
    laid = lay(os.getcwd())
    if laid:
        print("TRIAL_PILOT_POINTS", json.dumps(laid), flush=True)
    os.execvp("mf6", ["mf6"])


if __name__ == "__main__":
    main()
