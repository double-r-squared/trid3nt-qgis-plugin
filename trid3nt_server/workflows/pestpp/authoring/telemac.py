"""The study's TELEMAC piece: a finished TELEMAC-2D run's friction tuned through its steering file, its water level read at gauges.

A friction parameter is a multiplier in the place of the steering file's own FRICTION COEFFICIENT value in a PEST
template (PEST's scale the coefficient the run stated); every keyword the study states goes through the steering writer. TELEMAC-2D prints each variable asked for at its
remarkable points into its FORMATTED RESULTS FILE once, at the last time step, so a target is a gauge's reading
held against the free surface at the run's end, at the node the run's own point read lands the gauge on.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..authoring.control import Observed, Parameter, unheld_note
from ..errors import PestppError

__all__ = ["DATUMS", "DATUM_SHIFT", "Deck", "HELD", "LEVELS", "OBSERVED", "OBS_OUTPUT", "OUTFLOW", "OWN", "PLACE", "ROW",
           "SITE", "TRIAL", "TUNABLE", "calibrated", "command", "datums", "deck", "fields", "interface", "multiplier",
           "observe_levels", "outflow_levels", "outflow_offset", "published", "supplied", "targets"]

#: The study's row the targets are read off: the gauges, each a water level on the run's own frame.
ROW = "gauges"

#: The study's instruction file reads whitespace-separated values: no delimiter of its own.
OWN = MappingProxyType({})

#: What a target is, where it stands, and what it is read at.
OBSERVED, SITE, PLACE = "water level", "gauge", "node"

#: What a study tunes in a TELEMAC-2D run, by the dictionary's identifier.
TUNABLE = ("FRICTION_COEFFICIENT",)

#: The file the engine prints the remarkable points into, and the variable it prints there: the free surface.
OBS_OUTPUT = "study_points.txt"
_PRINTED = "S"
_HEADING = "FREE SURFACE"

#: Every field a template writes a multiplier into is this wide, delimiters included.
_FIELD = 24

#: Each gauge's datum as PEST writes it, and the free surface less it that the trial writes for the instruction.
DATUMS, HELD = "study_datums.dat", "study_levels.txt"
#: A datum's bounds, in its prior sd: PEST++'s own par_sigma_range (4) over 2, so the bounds imply the cited sd. PEST
#: holds it at DATUM_SHIFT bound-widths above zero, its offset taking that back, so a datum never crosses PEST's zero.
_DATUM_SDS, DATUM_SHIFT = 2.0, 10.0

#: Keywords the study writes into the trial's steering file, which the run's own must not already state.
_OWN_KEYWORDS = ("LIST OF POINTS", "NAMES OF POINTS", "VARIABLES TO BE PRINTED", "FORMATTED RESULTS FILE")

_ASSIGN = r"^(\s*{keyword}\s*[=:]\s*)(\S+)(.*)$"

#: What a trial runs in the image: it re-derives the outflow level at its own coefficient off LEVELS, then the solve.
TRIAL = "/opt/trid3nt/workers/pestpp/telemac_trial.py"

#: The trial's outflow levels, one per coefficient across the tuned range, beside its steering file.
LEVELS = "outflow_levels.json"
#: How many coefficients the range is read at: the stage is smooth and monotone in the coefficient, so a trial between
#: two reads is interpolated (millimetres on the Missouri section).
_LEVEL_POINTS = 81
#: A stated level that equals the derivation at the run's own coefficient within this is the derived one.
_SAME_LEVEL_M = 1.0e-3

#: The measured outflow level's offset as PEST writes it, where that level's accuracy is cited and so a parameter.
OUTFLOW = "study_outflow.dat"
_OUTFLOW = "outflow_level"
#: The suffix of the liquid boundaries file as the run wrote it, which each trial moves its own copy from.
_UNMOVED = ".unmoved"


@dataclass(frozen=True)
class Deck:
    """What a staged TELEMAC deck states that a study reads: its steering file's name, every keyword it states, the
    module, the result file the finished run wrote, the frame its UTM coordinates are in, and the length its outflow
    level was derived over."""

    steering: str
    stated: Mapping[str, list[str]]
    module: str
    result: str
    utm_epsg: int
    #: The length the run measured its friction slope over, along its walked line; None for a run that held none.
    reach_length_m: float | None = None
    #: The outflow level a gauge measured, where its record cites its accuracy: the PRESCRIBED ELEVATIONS entry it holds,
    #: the liquid boundaries file's column carrying its window (None for one reading), its sd and cite; None otherwise.
    level: Mapping[str, Any] | None = None
    #: Why the run's outflow level is no parameter, where it is not: a code the study's note names.
    unheld: str = ""


def command(the_deck: Deck) -> str:
    """What a trial runs: the outflow level re-derived at the trial's coefficient, then the deck through TELEMAC's own
    launcher, unpartitioned (the script execs it).

    The agents are the study's parallelism: a deck's own PARALLEL PROCESSORS would run that many ranks per trial.
    PEST++ starts the command without a shell, so a redirection would reach the launcher as arguments."""
    return f"python {TRIAL} {the_deck.module} {the_deck.steering}"


def deck(directory: Path, finished: Any) -> Deck:
    """The finished run's steering file as it was staged; a run whose record says it ended UNSTEADY, a deck of another
    module, or one stating the keywords the study writes, refuses."""
    from trid3nt_server.workflows.telemac.authoring.staged_check import read_steering

    case = finished.case
    module = str(case.get("module") or "")
    unsteady = [note for note in (dict(finished.record or {}).get("notes") or ())
                if str(note).startswith("the run ended UNSTEADY")]
    if unsteady:
        raise PestppError(f"the finished run is not at a steady state, so friction would be fitted to its transient: "
                          f"{unsteady[0]} Solve it longer, then calibrate.", error_code="PESTPP_RUN_UNSTEADY")
    if module != "telemac2d":
        raise PestppError(f"the run is a {module or 'moduleless'} run, and a study tunes a TELEMAC-2D deck",
                          error_code="PESTPP_DECK_UNREAD")
    steering = str(case["steering"])
    path = directory / steering
    stated = read_steering(path)
    if str((stated.get("LANGUAGE") or ["2"])[0]) != "2":
        raise PestppError(f"the run's steering file states LANGUAGE {stated['LANGUAGE'][0]}, and the study reads "
                          f"the engine's English heading {_HEADING!r} over its gauges", error_code="PESTPP_DECK_UNREAD")
    held = sorted(keyword for keyword in _OWN_KEYWORDS if keyword in stated)
    if held:
        raise PestppError(f"the run's steering file already states {', '.join(held)}, which the study writes to "
                          "print its gauges", error_code="PESTPP_OBSERVATION_UNPLACED")
    results = list(case.get("results") or finished.outputs)
    level, unheld = _measured_level(directory, stated, finished)
    return Deck(steering=steering, stated=stated, module=module, result=str(results[0]),
                utm_epsg=int(case["server_facts"]["utm_epsg"]),
                reach_length_m=case["server_facts"].get("reach_length_m"), level=level, unheld=unheld)


def _measured_level(directory: Path, stated: Mapping[str, list[str]], finished: Any
                    ) -> tuple[dict[str, Any] | None, str]:
    """The outflow level a gauge measured as the run's record and deck hold it -> (its entry, column, value, sd and
    cite; or None) and the code saying why it is none: a level the record cites no accuracy for (the reading's, and
    the gauge zero's where one lifts it), or one the deck does not hold at one boundary, is no parameter."""
    import math

    from trid3nt_server.inputs.slots import LEVEL

    row = ((getattr(finished, "record", None) or {}).get("inputs") or {}).get(LEVEL.name) or {}
    parts = [row.get(k) for k in ("accuracy", "zero_accuracy") if row.get(k) is not None]
    if not parts:
        return None, "unmeasured"
    if any(part.get("value") is None for part in parts):
        return None, "uncited"
    elevations = [float(v) for v in stated.get("PRESCRIBED ELEVATIONS") or ()]
    column, at = None, None
    file = (stated.get("LIQUID BOUNDARIES FILE") or [None])[0]
    if file and (directory / file).is_file():
        header = next((line.split() for line in (directory / file).read_text().splitlines()
                       if line.strip() and not line.startswith("#")), [])
        held = [name for name in header if name.startswith("SL(")]
        if len(held) == 1:
            column, at = held[0], int(held[0][3:-1]) - 1
    if at is None:
        flows = [float(v) for v in stated.get("PRESCRIBED FLOWRATES") or ()]
        # The deck writes 0.0 in a list at every boundary the other list prescribes.
        held_at = [k for k, z in enumerate(elevations) if z != 0.0 and (k >= len(flows) or flows[k] == 0.0)]
        at = held_at[0] if len(held_at) == 1 else None
    if at is None or at >= len(elevations):
        return None, "unplaced"
    initial = (stated.get("INITIAL ELEVATION") or [None])[0]
    return {"boundary": at, "column": column, "file": file if column else None, "value": elevations[at],
            "elevations": elevations,
            "initial": initial is not None and abs(float(initial) - elevations[at]) <= _SAME_LEVEL_M,
            "sd": math.sqrt(sum(float(part["value"]) ** 2 for part in parts)),
            "cite": "; ".join(str(part.get("cite") or "") for part in parts)}, ""


def supplied(the_deck: Deck) -> tuple[list[tuple[str, float, str]], list[str]]:
    """The run's supplied inputs a study holds as parameters -> ``[(name, sd, cite)]``, and the study's notes: which
    carry no accuracy on its record, and the measured outflow level as a parameter, or why it is none."""
    if the_deck.level is not None:
        return [(_OUTFLOW, the_deck.level["sd"], the_deck.level["cite"])], [
            unheld_note("its bed and stated values"),
            "the outflow level a gauge measured cites its accuracy, so it is an offset parameter of its own whose prior "
            "sd that accuracy is, each trial moving the level the outflow holds by it"]
    notes = [unheld_note("its bed, boundary levels and stated values")]
    if the_deck.unheld == "unplaced":
        notes.append("the outflow level a gauge measured cites its accuracy, and the deck holds it at no one boundary "
                     "the study can place, so it is no parameter")
    return [], notes


def fields(spatial: Mapping[str, Any], the_deck: Deck) -> dict[str, dict[str, Any]]:
    """No TELEMAC parameter is laid by pilot points: the run states one FRICTION COEFFICIENT, and a field of it would be
    a per-node friction in the geometry file the trial does not write. Any stated refuses naming it."""
    if spatial:
        raise PestppError(f"{', '.join(spatial)} is stated with pilot points, and a TELEMAC-2D study tunes the one "
                          "FRICTION COEFFICIENT its steering file states", error_code="PESTPP_PARAMETER_UNPLACED")
    return {}


def _templated(text: str, identifier: str, field: str) -> tuple[str, float]:
    """``text`` with the keyword ``identifier`` names written as ``field`` -> the text and the one value it stated."""
    keyword = identifier.replace("_", " ")
    pattern = re.compile(_ASSIGN.format(keyword=re.escape(keyword)), re.M | re.I)
    found = pattern.findall(text)
    if len(found) != 1:
        raise PestppError(f"the run's steering file states {keyword} {len(found)} times, and a multiplier is "
                          "written into the one value it states", error_code="PESTPP_PARAMETER_UNPLACED")
    return pattern.sub(lambda m: f"{m.group(1)}{field}{m.group(3)}", text), float(found[0][1])


def multiplier(directory: Path, identifier: str, *, lower: float, upper: float, the_deck: Deck,
               absolute: bool = False) -> tuple[Parameter, tuple[str, str]]:
    """One multiplier parameter over the steering file's keyword ``identifier`` -> the parameter and its
    (template, file). It starts at 1, the run's own value; ``absolute`` bounds are stated in the keyword's
    own unit and divided by that value. Zoned friction (FRICTION DATA) is not written and refuses."""
    if identifier.upper() not in TUNABLE:
        raise PestppError(f"{identifier} is not tuned in a TELEMAC-2D run; a study tunes {', '.join(TUNABLE)}",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    if str((the_deck.stated.get("FRICTION DATA") or [""])[0]).upper() in ("YES", "TRUE", "OUI", "VRAI"):
        raise PestppError("the run's friction is zoned (FRICTION DATA), and a study writes the one coefficient "
                          "the steering file states", error_code="PESTPP_PARAMETER_UNPLACED")
    template = f"{the_deck.steering}.tpl"
    written = directory / template
    text = (written.read_text().split("\n", 1)[1] if written.is_file()
            else (directory / the_deck.steering).read_text())
    name = identifier.lower()
    text, scale = _templated(text, identifier.upper(), "~" + name.ljust(_FIELD - 2) + "~")
    if absolute:
        lower, upper = lower / scale, upper / scale
        if not 0.0 < lower < 1.0 < upper:
            raise PestppError(f"{identifier} is {scale:g} in the run, outside its stated bounds "
                              f"{lower * scale:g} to {upper * scale:g}", error_code="PESTPP_BOUNDS_UNSTATED")
    written.write_text("ptf ~\n" + text)
    return (Parameter(name=name, value=1.0, lower=float(lower), upper=float(upper), group=name, scale=scale),
            (template, the_deck.steering))


def targets(gauges: Sequence[Any], *, the_deck: Deck, finished: Any, frame: str
            ) -> tuple[list[tuple[Observed, int]], list[str]]:
    """Each gauge's water level as a target at the node the run's own point read lands it on -> ``[(observation,
    node)]`` and what was left out.

    A reading in no length unit, on a frame other than the run's, or at a site the mesh does not hold (the point
    contract: never moved) is left out and said; a site is held once, at its first reading."""
    from trid3nt_server.inputs.temporal import TemporalUnitsError, convert_units
    from trid3nt_server.inputs.vertical_datum import names_frame
    from trid3nt_server.workflows.runtime.primitives import OutputEmpty, series
    from trid3nt_server.workflows.telemac.modules import wrapper_for
    from trid3nt_server.workflows.telemac.modules.outputs import Solved, _point, read_series

    from ..weights import observed

    solved = Solved({"run_id": finished.run_id, "utm_epsg": the_deck.utm_epsg,
                     "result_basename": the_deck.result}, wrapper_for(the_deck.module))
    out: list[tuple[Observed, int]] = []
    left: list[str] = []
    held: set[str] = set()
    for rank, gauge in enumerate(gauges, start=1):
        site = gauge.site_id or gauge.site_name or f"site {rank}"
        if site in held:
            left.append(f"{site}: it is held once, at its first reading, and this is another")
            continue
        try:
            value = convert_units(float(gauge.value), str(gauge.units or ""), "m")
        except TemporalUnitsError as exc:
            left.append(f"{site}: its reading is in {gauge.units!r}, no length ({exc})")
            continue
        if not (gauge.datum and names_frame(gauge.datum, frame)):
            left.append(f"{site}: its reading is counted from {gauge.datum or 'no datum'}, and the run's free "
                        f"surface from {frame or 'its own frame'}")
            continue
        try:
            read_series(series(_PRINTED, at=gauge), solved)
        except OutputEmpty as exc:
            left.append(f"{site}: {exc}")
            continue
        node = solved.node_at(_point(gauge))
        held.add(site)
        out.append((observed(f"level_{len(out) + 1}", value, gauge, group="levels", datum=True,
                             to_run=lambda sigma, gauge=gauge: convert_units(sigma, str(gauge.units), "m")), node + 1))
    return out, left


def observe_levels(directory: Path, targets: Sequence[tuple[Observed, int]], *, the_deck: Deck
                   ) -> tuple[str, str]:
    """The remarkable points stated in the steering file through its own writer - each target's node, named for it,
    the free surface printed into the formatted results file - and the instruction file that reads them
    -> ``(instruction, output)``."""
    from trid3nt_server.workflows.telemac.authoring.serializer import restate

    names = [f"OBS_{rank}" for rank, _ in enumerate(targets, start=1)]
    restate(directory, the_deck.steering, the_deck.module, {
        "LIST OF POINTS": [int(node) for _target, node in targets], "NAMES OF POINTS": names,
        "VARIABLES TO BE PRINTED": _PRINTED, "FORMATTED RESULTS FILE": OBS_OUTPUT})
    instruction = f"{OBS_OUTPUT}.ins"
    # The file holds one block per variable the run prints, each listing every point; the free surface's
    # block is found by the heading the engine writes over it, its points read in the order they were listed.
    reads = "\n".join(f"~{name} ~ ~:~ !{target.name}!" for name, (target, _node) in zip(names, targets))
    (directory / instruction).write_text(f"pif ~\n~{_HEADING}~\n{reads}\n")
    return instruction, OBS_OUTPUT


def interface(source: Path, dest: Path, chosen: Sequence[tuple[str, float, float, bool]], *, the_deck: Deck,
              targets: Sequence[tuple[Observed, int]], spatial: Mapping[str, Any] = {}
              ) -> tuple[list[Parameter], list[tuple[str, str]], list[tuple[str, str]]]:
    """A study directory: the deck at ``source`` copied to ``dest`` with its gauges printed and a multiplier
    template over each chosen ``(identifier, lower, upper, absolute)`` -> ``(parameters, templates, instructions)``.
    Pilot points refuse (``fields``)."""
    fields(spatial, the_deck)
    shutil.copytree(source, dest)
    instructions = [observe_levels(dest, targets, the_deck=the_deck)]
    parameters, templates = [], []
    for identifier, lower, upper, absolute in chosen:
        parameter, pair = multiplier(dest, identifier, lower=lower, upper=upper, the_deck=the_deck,
                                     absolute=absolute)
        parameters.append(parameter)
        if pair not in templates:
            templates.append(pair)
    if outflow_levels(dest, the_deck=the_deck, parameters=parameters) is None and the_deck.level is not None:
        parameters.append(outflow_offset(dest, the_deck=the_deck))
        templates.append((f"{OUTFLOW}.tpl", OUTFLOW))
    held = datums(dest, targets)
    if held:
        parameters += held
        templates.append((f"{DATUMS}.tpl", DATUMS))
        instructions = [(f"{HELD}.ins", HELD)]
    return parameters, templates, instructions


def datums(directory: Path, targets: Sequence[tuple[Observed, int]]) -> list[Parameter]:
    """Each gauge whose zero is a parameter of its own (its ``datum``) given one, prior sd the zero's cited accuracy,
    and the files the trial reads them through: every point's datum (0 where the zero is no parameter) and the
    instruction reading each point's free surface less it -> the datum parameters; none where no zero is one."""
    found = [(rank, target) for rank, (target, _node) in enumerate(targets, start=1) if target.datum is not None]
    if not found:
        return []
    parameters = []
    for rank, target in found:
        sd, cite = target.datum
        width = _DATUM_SDS * sd
        parameters.append(Parameter(name=f"datum_{rank}", value=DATUM_SHIFT * width,
                                    lower=(DATUM_SHIFT - 1.0) * width, upper=(DATUM_SHIFT + 1.0) * width,
                                    group=f"datum_{rank}", transform="none", offset=-DATUM_SHIFT * width, sd=sd,
                                    sd_source=f"the gauge zero's cited accuracy ({cite})"))
    named = {rank: f"datum_{rank}" for rank, _target in found}
    lines = [(f"OBS_{rank}", named.get(rank)) for rank in range(1, len(targets) + 1)]
    (directory / DATUMS).write_text("".join(f"{point} 0.0\n" for point, _name in lines))
    (directory / f"{DATUMS}.tpl").write_text("ptf ~\n" + "".join(
        f"{point} " + (f"~{name.ljust(_FIELD - 2)}~" if name else "0.0") + "\n" for point, name in lines))
    (directory / f"{HELD}.ins").write_text("pif ~\n" + "".join(
        f"l1 w !{target.name}!\n" for target, _node in targets))
    return parameters


def _nodes(directory: Path, the_deck: Deck) -> tuple[Any, Any]:
    """The staged geometry's node coordinates and bed, in the numbering its boundary file names."""
    import numpy as np

    from trid3nt_server.workflows.telemac.authoring.selafin_io import read_selafin

    geometry = read_selafin(directory / the_deck.stated["GEOMETRY FILE"][0])
    bed = next(values for name, values in geometry["data"].items() if name.upper() in ("BOTTOM", "FOND"))
    return np.column_stack([geometry["x"], geometry["y"]]), np.asarray(bed[0], dtype=float)


def _faces(directory: Path, the_deck: Deck) -> dict[str, list[int]]:
    """The prescribed-flowrate and prescribed-elevation faces of the boundary file, in its walk order, 0-based."""
    faces: dict[str, list[int]] = {"inflow": [], "outflow": []}
    for line in (directory / the_deck.stated["BOUNDARY CONDITIONS FILE"][0]).read_text().splitlines():
        row = line.split()
        if len(row) < 12:
            continue
        if row[0] == "5":
            faces["outflow"].append(int(row[11]) - 1)
        elif row[1] == "5":
            faces["inflow"].append(int(row[11]) - 1)
    return faces


def _derived(directory: Path, the_deck: Deck) -> tuple[Any, int, float, int, list[float]] | None:
    """The measured channel the run derived its outflow level over -> (channel, law, discharge, which of the
    PRESCRIBED ELEVATIONS it is, those elevations); ``None`` where the stated level is not the derivation (a gauge's)."""
    from trid3nt_server.workflows.telemac.authoring.opening import _measured_channel
    from trid3nt_server.workflows.telemac.helpers.uniform_flow import normal_depth_stage

    stated = the_deck.stated
    if not stated.get("PRESCRIBED ELEVATIONS") or not stated.get("PRESCRIBED FLOWRATES"):
        return None
    elevations = [float(v) for v in stated["PRESCRIBED ELEVATIONS"]]
    flows = [float(v) for v in stated["PRESCRIBED FLOWRATES"] if float(v) > 0.0]
    if len(flows) != 1:
        return None
    faces = _faces(directory, the_deck)
    if not (faces["inflow"] and faces["outflow"]):
        return None
    xy, bed = _nodes(directory, the_deck)
    channel = _measured_channel(faces, xy, bed, length_m=the_deck.reach_length_m)
    law, coefficient = int(stated["LAW OF BOTTOM FRICTION"][0]), float(stated["FRICTION COEFFICIENT"][0])
    stage = normal_depth_stage(channel, law=law, coefficient=coefficient, discharge_q=flows[0])["stage_m"]
    which = [k for k, value in enumerate(elevations) if abs(value - stage) <= _SAME_LEVEL_M]
    return (channel, law, flows[0], which[0], elevations) if len(which) == 1 else None


def outflow_levels(directory: Path, *, the_deck: Deck, parameters: Sequence[Parameter]) -> dict[str, Any] | None:
    """The outflow level a trial holds, derived at each coefficient across the tuned range, written as LEVELS beside the
    steering file -> what was written; ``None`` (nothing written) where the run's outflow level is not the derived one.

    A level derived at one friction never bounds a trial at another: the trial reads its own off this table."""
    import json

    from trid3nt_server.workflows.telemac.helpers.uniform_flow import normal_depth_stage

    friction = next((p for p in parameters if p.name == "friction_coefficient"), None)
    found = _derived(directory, the_deck) if friction is not None else None
    if found is None:
        return None
    channel, law, discharge, which, elevations = found
    low, high = friction.lower * friction.scale, friction.upper * friction.scale
    coefficients = [low + (high - low) * k / (_LEVEL_POINTS - 1) for k in range(_LEVEL_POINTS)]
    stages = [round(normal_depth_stage(channel, law=law, coefficient=c, discharge_q=discharge)["stage_m"], 4)
              for c in coefficients]
    table = {"module": the_deck.module, "steering": the_deck.steering, "boundary": which, "elevations": elevations,
             "coefficients": [round(c, 6) for c in coefficients], "stages": stages}
    (directory / LEVELS).write_text(json.dumps(table, indent=1))
    return table


def outflow_offset(directory: Path, *, the_deck: Deck) -> Parameter:
    """The measured outflow level's offset as a parameter, prior mean 0 and sd the level's cited accuracy, bounds 2 sd
    either side as a datum's; and the files the trial reads it through: the offset PEST writes and LEVELS, the level
    the trial adds it to (its steering entry, its window's column and a flat opening stated at it)."""
    import json

    held = the_deck.level
    width = _DATUM_SDS * held["sd"]
    if held["column"]:
        shutil.copy2(directory / held["file"], directory / f"{held['file']}{_UNMOVED}")
    (directory / OUTFLOW).write_text("OUTFLOW 0.0\n")
    (directory / f"{OUTFLOW}.tpl").write_text(f"ptf ~\nOUTFLOW ~{_OUTFLOW.ljust(_FIELD - 2)}~\n")
    (directory / LEVELS).write_text(json.dumps({
        "module": the_deck.module, "steering": the_deck.steering, "boundary": held["boundary"],
        "elevations": held["elevations"], "level": held["value"], "column": held["column"], "file": held["file"],
        "unmoved": held["file"] and f"{held['file']}{_UNMOVED}", "initial": held["initial"]}, indent=1))
    return Parameter(name=_OUTFLOW, value=DATUM_SHIFT * width, lower=(DATUM_SHIFT - 1.0) * width,
                     upper=(DATUM_SHIFT + 1.0) * width, group=_OUTFLOW, transform="none", offset=-DATUM_SHIFT * width,
                     sd=held["sd"], sd_source=f"the measured outflow level's cited accuracy ({held['cite']})")


def _offset_level(directory: Path, the_deck: Deck, offset: float) -> dict[str, Any]:
    """The measured outflow level moved by ``offset``: its window's column rewritten in place, and the steering values
    that hold it -> those values."""
    held = the_deck.level
    if held["column"]:
        path = directory / held["file"]
        lines = path.read_text().splitlines()
        rows = [k for k, line in enumerate(lines) if line.strip() and not line.startswith("#")]
        at = lines[rows[0]].split().index(held["column"])
        for k in rows[2:]:
            cells = lines[k].split()
            cells[at] = f"{float(cells[at]) + offset:.9g}"
            lines[k] = " ".join(cells)
        path.write_text("\n".join(lines) + "\n")
    elevations = list(held["elevations"])
    elevations[held["boundary"]] = round(held["value"] + offset, 4)
    return {"PRESCRIBED ELEVATIONS": elevations,
            **({"INITIAL ELEVATION": elevations[held["boundary"]]} if held["initial"] else {})}


async def calibrated(*, finished: Any, study_dir: Path, the_deck: Deck, parameters: list,
                     templates: list, best: Mapping[str, float], frame: str) -> dict[str, Any]:
    """The finished run's deck with the best parameters written into its own steering file, staged and solved as
    an ordinary TELEMAC run -> its handle, shaped as the TELEMAC publish reads one."""
    import asyncio

    from trid3nt_contracts import new_ulid

    from trid3nt_server.workflows.solver.staging import new_rundir, stage_run
    from trid3nt_server.workflows.telemac import engine
    from trid3nt_server.workflows.telemac.authoring.serializer import restate
    from trid3nt_server.workflows.telemac.authoring.staged_check import check_staged_run

    run_id = new_ulid()
    rundir = new_rundir(run_id, stem="telemac")
    deck_dir = study_dir.parent / "deck"
    await asyncio.to_thread(shutil.copytree, deck_dir, rundir, dirs_exist_ok=True)
    values = {parameter.name.upper().replace("_", " "): best[parameter.name] * parameter.scale + parameter.offset
              for parameter in parameters if parameter.name.upper() in TUNABLE}
    values.update(await asyncio.to_thread(_level_at, rundir, the_deck, values.get("FRICTION COEFFICIENT")))
    offset = next((best[p.name] * p.scale + p.offset for p in parameters if p.name == _OUTFLOW), None)
    if offset is not None:
        values.update(await asyncio.to_thread(_offset_level, rundir, the_deck, offset))
    await asyncio.to_thread(restate, rundir, the_deck.steering, the_deck.module, values)
    case = finished.case
    results = list(case.get("results") or [])
    facts = dict(case.get("server_facts") or {})
    staged = await stage_run(
        rundir, run_id, module=str(case["module"]), steering=the_deck.steering, results=results,
        outputs=list(finished.outputs), mesh_inputs=[], prefix="telemac", sheet={}, server_facts=facts,
        result_basename=the_deck.result, check=check_staged_run,
        user_fortran=list(case.get("user_fortran") or ()), coupling=case.get("coupling"),
        continue_from=case.get("continue_from"), cores=int(case.get("cores") or 1))
    handle = await engine.solve_case(run=staged, cores=int(case.get("cores") or 1))
    return {**handle, **facts, "result_basename": the_deck.result, "module": the_deck.module,
            "reference_time": (finished.record.get("outputs") or [{}])[0].get("reference_time")
            or _moment(finished), "display_basename": None,
            "module_output": _module_output(the_deck), "tracer_names": (the_deck.stated.get("NAMES OF TRACERS")
                                                                       or None)}


def _level_at(directory: Path, the_deck: Deck, coefficient: float | None) -> dict[str, Any]:
    """The outflow level and the flat opening at ``coefficient``, re-derived over the run's own channel; ``{}`` where
    the run's level is not the derived one or the coefficient is not tuned."""
    from trid3nt_server.workflows.telemac.helpers.uniform_flow import normal_depth_stage

    found = _derived(directory, the_deck) if coefficient is not None else None
    if found is None:
        return {}
    channel, law, discharge, which, elevations = found
    stage = round(normal_depth_stage(channel, law=law, coefficient=float(coefficient),
                                     discharge_q=discharge)["stage_m"], 4)
    elevations[which] = stage
    return {"PRESCRIBED ELEVATIONS": elevations, "INITIAL ELEVATION": stage}


def _moment(finished: Any) -> str | None:
    value = ((finished.record.get("inputs") or {}).get("event_time") or {}).get("value")
    return str(value) if value not in (None, "", "none", "now") else None


def _module_output(the_deck: Deck) -> list[dict[str, Any]]:
    """The module's own rows for what the deck writes, as a solved TELEMAC run's handle carries them."""
    from trid3nt_server.workflows.telemac.modules import wrapper_for

    body = wrapper_for(the_deck.module)
    return [{"token": token, "module": the_deck.module, "name": row.name, "unit": row.unit, "style": row.style,
             "varies": row.varies, "has_edge": bool(row.has_edge), "injected": bool(row.injected)}
            for token, row in body.table({}).items() if token not in body.LISTING and token != body.TRACER]


async def published(solved: Mapping[str, Any]) -> Any:
    """The calibrated run published as TELEMAC publishes a run: every variable its result carries."""
    from trid3nt_server.workflows.telemac.workflow import publish_outputs

    return await publish_outputs(run=solved, outputs=[], captions={}, params={})
