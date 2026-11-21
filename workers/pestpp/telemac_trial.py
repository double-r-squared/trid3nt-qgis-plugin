"""One PEST++ trial of a TELEMAC study: the outflow level at the trial's own friction or offset, the solve, the datums.

Runs inside the pestpp-telemac image in the trial's directory, importing nothing from trid3nt. Where the study wrote
outflow_levels.json, the outflow level is the trial's own: derived (the steering file's coefficient, as PEST wrote it,
read through TelemacCas and its level read off the table) or measured (the level plus the offset PEST wrote to
study_outflow.dat, its window's column moved by it too), restated with the flat opening's INITIAL ELEVATION through the
steering driver. Where it wrote study_datums.dat (a gauge zero that is a parameter) TELEMAC's own launcher runs, then
each point's free surface less its datum is written to study_levels.txt; otherwise the launcher replaces this process.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LEVELS = "outflow_levels.json"

#: Each remarkable point's datum as PEST wrote it, the formatted results the engine prints them into, and the free
#: surface less the datum, one line a point: what the study's instruction reads.
DATUMS, POINTS, HELD = "study_datums.dat", "study_points.txt", "study_levels.txt"
#: The measured outflow level's offset as PEST wrote it.
OUTFLOW = "study_outflow.dat"
_HEADING = "FREE SURFACE"


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
    if "level" in table:
        with open(os.path.join(data_dir, OUTFLOW)) as fh:
            offset = float(fh.read().split()[1])
        stage = round(float(table["level"]) + offset, 4)
        if table.get("column"):
            shift_column(os.path.join(data_dir, table["unmoved"]), os.path.join(data_dir, table["file"]),
                         table["column"], offset)
        held = {"offset": offset}
    else:
        steering = os.path.join(data_dir, table["steering"])
        coefficient = float(cas.TelemacCas(steering, cas.get_dico(table["module"]), access="r",
                                           check_files=False).values["FRICTION COEFFICIENT"])
        stage = round(level_at(table, coefficient), 4)
        held = {"coefficient": coefficient}
    elevations = list(table["elevations"])
    elevations[int(table["boundary"])] = stage
    values = {"PRESCRIBED ELEVATIONS": elevations,
              **({"INITIAL ELEVATION": stage} if table.get("initial", True) else {})}
    cas.restate_steering({"data_dir": data_dir,
                          "restate": {table["steering"]: {"module": table["module"], "values": values}}})
    return {**held, **values}


def shift_column(unmoved: str, path: str, column: str, offset: float) -> None:
    """The liquid boundaries file as the run wrote it (``unmoved``: an agent runs many trials in one directory), its
    ``column`` moved by ``offset`` at every instant, written to ``path``: past its comments, the header the engine
    splits its names on and the units line, one row an instant."""
    with open(unmoved) as fh:
        lines = fh.read().splitlines()
    rows = [k for k, line in enumerate(lines) if line.strip() and not line.startswith("#")]
    at = lines[rows[0]].split().index(column)
    for k in rows[2:]:
        cells = lines[k].split()
        cells[at] = f"{float(cells[at]) + offset:.9g}"
        lines[k] = " ".join(cells)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def free_surface(text: str, names: list) -> list:
    """Each point's free surface in the formatted results, read as the study's instruction reads it: past the heading,
    the first line naming the point, the value after its colon."""
    lines = text.splitlines()
    at = next(k for k, line in enumerate(lines) if _HEADING in line)
    values = []
    for name in names:
        at = next(k for k in range(at + 1, len(lines)) if f"{name} " in lines[k] + " ")
        values.append(float(lines[at].split(":", 1)[1].split()[0]))
    return values


def held_levels(data_dir: str) -> list:
    """Each point's free surface less its datum, written to HELD -> the lines written."""
    with open(os.path.join(data_dir, DATUMS)) as fh:
        datums = [line.split()[:2] for line in fh if line.strip()]
    with open(os.path.join(data_dir, POINTS)) as fh:
        surface = free_surface(fh.read(), [name for name, _datum in datums])
    lines = [f"{name} {level - float(datum)!r}" for (name, datum), level in zip(datums, surface)]
    with open(os.path.join(data_dir, HELD), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return lines


def main() -> None:
    import subprocess

    module, steering = sys.argv[1], sys.argv[2]
    set_here = restate_levels(os.getcwd())
    if set_here is not None:
        print("TRIAL_OUTFLOW_LEVEL", json.dumps(set_here), flush=True)
    launcher = f"{module}.py"
    if not os.path.isfile(DATUMS):
        os.execvp(launcher, [launcher, "--ncsize", "0", steering])
    solved = subprocess.run([launcher, "--ncsize", "0", steering])
    if solved.returncode:
        raise SystemExit(solved.returncode)
    print("TRIAL_DATUM_LEVELS", json.dumps(held_levels(os.getcwd())), flush=True)


if __name__ == "__main__":
    main()
