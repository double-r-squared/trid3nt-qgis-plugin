"""One PEST++ trial of a TELEMAC study: the outflow level at the trial's own friction, then the solve.

Runs inside the pestpp-telemac image in the trial's directory, importing nothing from trid3nt. Where the study wrote
outflow_levels.json (a level derived at one friction never bounds a trial at another) the steering file's coefficient,
as PEST wrote it, is read through TelemacCas, its outflow level read off the table, and PRESCRIBED ELEVATIONS and the
flat opening's INITIAL ELEVATION restated through the steering driver; then TELEMAC's own launcher replaces this process.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LEVELS = "outflow_levels.json"


def level_at(table: dict, coefficient: float) -> float:
    """The outflow level the table holds at ``coefficient``, linear between its two nearest reads; outside it, none."""
    coefficients, stages = table["coefficients"], table["stages"]
    if not coefficients[0] <= coefficient <= coefficients[-1]:
        raise SystemExit(f"the trial's coefficient {coefficient} lies outside the derived range "
                         f"{coefficients[0]} to {coefficients[-1]}")
    for k in range(1, len(coefficients)):
        if coefficient <= coefficients[k]:
            c0, c1, s0, s1 = coefficients[k - 1], coefficients[k], stages[k - 1], stages[k]
            return s0 + (s1 - s0) * (coefficient - c0) / (c1 - c0)
    return stages[-1]


def restate_levels(data_dir: str) -> dict | None:
    """The steering file named by the table, restated with the outflow level at its own coefficient -> what was set."""
    path = os.path.join(data_dir, LEVELS)
    if not os.path.isfile(path):
        return None
    import cas

    table = json.load(open(path))
    steering = os.path.join(data_dir, table["steering"])
    coefficient = float(cas.TelemacCas(steering, cas.get_dico(table["module"]), access="r",
                                       check_files=False).values["FRICTION COEFFICIENT"])
    stage = round(level_at(table, coefficient), 4)
    elevations = list(table["elevations"])
    elevations[int(table["boundary"])] = stage
    values = {"PRESCRIBED ELEVATIONS": elevations, "INITIAL ELEVATION": stage}
    cas.restate_steering({"data_dir": data_dir,
                          "restate": {table["steering"]: {"module": table["module"], "values": values}}})
    return {"coefficient": coefficient, **values}


def main() -> None:
    module, steering = sys.argv[1], sys.argv[2]
    set_here = restate_levels(os.getcwd())
    if set_here is not None:
        print("TRIAL_OUTFLOW_LEVEL", json.dumps(set_here), flush=True)
    launcher = f"{module}.py"
    os.execvp(launcher, [launcher, "--ncsize", "0", steering])


if __name__ == "__main__":
    main()
