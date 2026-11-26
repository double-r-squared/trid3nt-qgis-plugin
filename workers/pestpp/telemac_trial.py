"""One PEST++ trial of a TELEMAC study: the keywords and fields PEST wrote, the outflow level at the trial's own friction or offset, the solve, the datums.

Runs inside the pestpp-telemac image in the trial's directory, importing nothing from trid3nt. Where the study wrote
study_telemac.json, each whole-list keyword, spatial field and friction layout is laid from the multiplier PEST wrote
(zone and pilot points included). Where it wrote outflow_levels.json, the outflow level is the trial's own: derived (the
mean friction over the outflow face, read off the table) or measured (the level plus the offset PEST wrote to
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

#: The layings PEST wrote: the base values of every keyword, field and friction layout, and where each one goes.
SPEC = "study_telemac.json"


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


def _multiplier(data_dir: str, name: str) -> float:
    with open(os.path.join(data_dir, f"{name}.dat")) as fh:
        return float(fh.read().split()[0])


def _pilot(data_dir: str, pilot: dict) -> "object":
    """The pilot points' kriged multiplier over every mesh node, 10 ** (the factors over the log10 values)."""
    import numpy as np

    held = np.load(os.path.join(data_dir, pilot["npz"]))
    stated = {}
    with open(os.path.join(data_dir, pilot["values"])) as fh:
        for line in fh:
            if line.strip():
                name, value = line.split()[:2]
                stated[name] = float(value)
    names = [str(name) for name in held["names"]]
    values = np.log10(np.array([stated[name] for name in names], dtype=float))
    return 10.0 ** (held["weight"] * values[held["index"]]).sum(axis=1)


def _apply_keywords(data_dir: str, spec: dict) -> None:
    grouped: dict[str, dict] = {}
    for entry in spec.get("keywords", []):
        multiplier = _multiplier(data_dir, entry["name"])
        grouped.setdefault(entry["steering"], {"module": entry["module"], "values": {}})
        grouped[entry["steering"]]["values"][entry["keyword"]] = [
            float(value) * multiplier for value in entry["base"]]
    if grouped:
        import cas

        cas.restate_steering({"data_dir": data_dir, "restate": grouped})


def _write_geometry(path: str, variable: str, values) -> None:
    """``path`` rewritten with ``variable`` carrying ``values`` (added, or replaced where it already exists)."""
    import numpy as np
    from serafin import SerafinReader, SerafinWriter

    reader = SerafinReader(path, "en")
    reader.__enter__()
    try:
        reader.read_header()
        reader.get_time()
        header = reader.header
        names = [name.decode().strip() for name in header.var_names]
        times = list(reader.time) or [0.0]
        frames = [np.asarray(reader.read_vars_in_frame(index), dtype=float) for index in range(len(times))]
    finally:
        reader.__exit__(None, None, None)
    written = header.copy()
    if variable.upper() in (name.upper() for name in names):
        at = next(index for index, name in enumerate(names) if name.upper() == variable.upper())
        frames = [np.concatenate([frame[:at], np.asarray(values, dtype=float).reshape(1, -1), frame[at + 1:]])
                  for frame in frames]
    else:
        written.add_variable_str(variable, variable, "")
        frames = [np.vstack([frame, np.asarray(values, dtype=float).reshape(1, -1)]) for frame in frames]
    temporary = path + ".tmp"
    with SerafinWriter(temporary, "en", overwrite=True) as writer:
        writer.write_header(written)
        for time, frame in zip(times, frames):
            writer.write_entire_frame(written, float(time), frame)
    os.replace(temporary, path)


def _apply_field(data_dir: str, info: dict) -> None:
    import numpy as np

    base = np.asarray(info["base"], dtype=float)
    multiplier = np.ones(base.shape[0], dtype=float)
    if info.get("whole"):
        multiplier[:] = _multiplier(data_dir, info["whole"])
    for piece in info.get("pieces", []):
        value = _multiplier(data_dir, piece["name"])
        if piece.get("nodes") is None:
            multiplier[:] = value
        else:
            multiplier[[int(node) for node in piece["nodes"]]] = value
    if info.get("pilot"):
        multiplier *= _pilot(data_dir, info["pilot"])
    laid = base * multiplier
    path = os.path.join(data_dir, info["file"])
    if info["kind"] == "scatter":
        written, at = [], 0
        with open(path) as fh:
            for line in fh.read().splitlines():
                if not line.strip():
                    continue
                if line.lstrip().startswith("#"):
                    written.append(line)
                    continue
                parts = line.split()
                parts[2] = f"{laid[at]:.10g}"
                written.append(" ".join(parts))
                at += 1
        with open(path, "w") as fh:
            fh.write("\n".join(written) + "\n")
    elif info["kind"] == "bottom":
        _write_geometry(path, info["variable"], laid)


def _friction_multiplier(data_dir: str, info: dict) -> "object":
    import numpy as np

    multiplier = np.ones(len(info["base_nodes"]), dtype=float)
    if info.get("whole"):
        multiplier *= _multiplier(data_dir, info["whole"])
    for piece in info.get("pieces", []):
        value = _multiplier(data_dir, piece["name"])
        if "nodes" in piece and piece["nodes"] is not None:
            multiplier[[int(node) for node in piece["nodes"]]] = value
        elif "rows" in piece and info.get("node_zone"):
            zones = {str(info["table"][row]["zone"]) for row in piece["rows"]}
            held = [index for index, zone in enumerate(info["node_zone"]) if zone in zones]
            multiplier[held] = value
    if info.get("pilot"):
        multiplier *= _pilot(data_dir, info["pilot"])
    return multiplier


def _write_friction_rows(data_dir: str, info: dict, rows) -> None:
    lines = ["* PEST++ calibrated friction zones", "* zone  law      coef    vegetation"]
    for row in rows:
        tail = " ".join(row.get("rest") or ["NULL"])
        lines.append(f"{row['zone']} {row['law']} {float(row['coef'])!r} {tail}".rstrip())
    lines.append("END")
    with open(os.path.join(data_dir, info["data_file"]), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def _apply_friction(data_dir: str, info: dict) -> "object":
    import numpy as np

    multiplier = _friction_multiplier(data_dir, info)
    base = np.asarray(info["base_nodes"], dtype=float)
    laid = base * multiplier
    if not info["data"]:
        _write_geometry(os.path.join(data_dir, info["geometry"]), "BOTTOM FRICTION", laid)
        return laid
    by_row = not info.get("pilot") and not any("nodes" in piece for piece in info["pieces"])
    if by_row:
        rows = [dict(row) for row in info["table"]]
        if info.get("whole"):
            value = _multiplier(data_dir, info["whole"])
            for row in rows:
                row["coef"] = float(row["coef"]) * value
        for piece in info["pieces"]:
            value = _multiplier(data_dir, piece["name"])
            for index in piece["rows"]:
                rows[int(index)]["coef"] = float(rows[int(index)]["coef"]) * value
        _write_friction_rows(data_dir, info, rows)
        with open(os.path.join(data_dir, info["zones_file"]), "w") as fh:
            fh.write("".join(f"{node} {zone}\n" for node, zone in enumerate(info["node_zone"], start=1)))
        return laid
    laws = {str(row["zone"]): row["law"] for row in info["table"]}
    rows = [{"zone": str(node), "law": laws[zone], "coef": float(coefficient), "rest": ["NULL"]}
            for node, (zone, coefficient) in enumerate(zip(info["node_zone"], laid), start=1)]
    _write_friction_rows(data_dir, info, rows)
    with open(os.path.join(data_dir, info["zones_file"]), "w") as fh:
        fh.write("".join(f"{node} {node}\n" for node in range(1, len(laid) + 1)))
    return laid


def apply_spec(data_dir: str) -> dict | None:
    path = os.path.join(data_dir, SPEC)
    if not os.path.isfile(path):
        return None
    with open(path) as fh:
        spec = json.load(fh)
    _apply_keywords(data_dir, spec)
    for info in spec.get("fields", []):
        _apply_field(data_dir, info)
    friction = spec.get("friction")
    laid = _apply_friction(data_dir, friction) if friction else None
    return {"friction": laid}


def restate_levels(data_dir: str, applied: dict | None = None) -> dict | None:
    """The steering file named by the table, restated with the outflow level at its own coefficient -> what was set."""
    path = os.path.join(data_dir, LEVELS)
    if not os.path.isfile(path):
        return None
    import cas

    with open(path) as fh:
        table = json.load(fh)
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
        if applied and applied.get("friction") is not None:
            faces = table.get("faces") or []
            coefficient = float(sum(float(applied["friction"][node]) for node in faces) / len(faces))
            held = {"coefficient": coefficient}
        else:
            coefficient = float(cas.TelemacCas(steering, cas.get_dico(table["module"]), access="r",
                                               check_files=False).values["FRICTION COEFFICIENT"])
            held = {"coefficient": coefficient}
        stage = round(level_at(table, coefficient), 4)
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
    applied = apply_spec(os.getcwd())
    if applied is not None:
        print("TRIAL_LAYINGS", json.dumps({"friction": applied["friction"] is not None}), flush=True)
    set_here = restate_levels(os.getcwd(), applied)
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
