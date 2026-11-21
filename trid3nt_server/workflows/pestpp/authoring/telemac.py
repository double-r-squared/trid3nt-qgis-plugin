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

from ..authoring.control import Observed, Parameter
from ..errors import PestppError

__all__ = ["Deck", "LEVELS", "OBSERVED", "OBS_OUTPUT", "OWN", "PLACE", "ROW", "SITE", "TRIAL", "TUNABLE",
           "calibrated", "command", "deck", "interface", "multiplier", "observe_levels", "outflow_levels",
           "published", "targets"]

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
    return Deck(steering=steering, stated=stated, module=module, result=str(results[0]),
                utm_epsg=int(case["server_facts"]["utm_epsg"]),
                reach_length_m=case["server_facts"].get("reach_length_m"))


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
        out.append((observed(f"level_{len(out) + 1}", value, gauge, group="levels", to_run=lambda sigma, gauge=gauge: (
            convert_units(sigma, str(gauge.units), "m"))), node + 1))
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
              targets: Sequence[tuple[Observed, int]]
              ) -> tuple[list[Parameter], list[tuple[str, str]], list[tuple[str, str]]]:
    """A study directory: the deck at ``source`` copied to ``dest`` with its gauges printed and a multiplier
    template over each chosen ``(identifier, lower, upper, absolute)`` -> ``(parameters, templates, instructions)``."""
    shutil.copytree(source, dest)
    instructions = [observe_levels(dest, targets, the_deck=the_deck)]
    parameters, templates = [], []
    for identifier, lower, upper, absolute in chosen:
        parameter, pair = multiplier(dest, identifier, lower=lower, upper=upper, the_deck=the_deck,
                                     absolute=absolute)
        parameters.append(parameter)
        if pair not in templates:
            templates.append(pair)
    outflow_levels(dest, the_deck=the_deck, parameters=parameters)
    return parameters, templates, instructions


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
              for parameter in parameters}
    values.update(await asyncio.to_thread(_level_at, rundir, the_deck, values.get("FRICTION COEFFICIENT")))
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
