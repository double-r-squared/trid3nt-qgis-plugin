"""The study's TELEMAC piece: a finished TELEMAC-2D run's parameters tuned through its own dictionaries, its water level read at gauges.

A parameter is a multiplier in the place of a keyword's own value in a PEST template (PEST's scale the value the run
stated), a whole list scaled by the trial, or a field laid by zones or pilot points. Which keyword is tunable is the
TELEMAC module dictionary's own declaration across the deck's active bodies; nothing here lists a keyword. TELEMAC-2D
prints each variable asked for at its remarkable points into its FORMATTED RESULTS FILE once, at the last time step,
so a target is a gauge's reading held against the free surface at the run's end, at the node the run's own point read
lands the gauge on.
"""

from __future__ import annotations

import json
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..authoring.control import Observed, Parameter, unheld_note
from ..errors import PestppError

__all__ = ["Deck", "DATUMS", "DATUM_SHIFT", "HELD", "LEVELS", "OBSERVED", "OBS_OUTPUT", "OUTFLOW", "OWN", "PLACE", "ROW",
           "SITE", "SPEC", "TRIAL", "calibrated", "command", "datums", "deck", "fields", "interface", "multiplier",
           "observe_levels", "outflow_levels", "outflow_offset", "pilot_points", "published", "supplied", "targets",
           "zoned"]

#: The study's row the targets are read off: the gauges, each a water level on the run's own frame.
ROW = "gauges"

#: The study's instruction file reads whitespace-separated values: no delimiter of its own.
OWN = MappingProxyType({})

#: What a target is, where it stands, and what it is read at.
OBSERVED, SITE, PLACE = "water level", "gauge", "node"

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

#: What a trial runs in the image: it re-derives the outflow level at its own friction or offset, lays the fields and
#: list values PEST wrote, then the solve.
TRIAL = "/opt/trid3nt/workers/pestpp/telemac_trial.py"

#: The outflow levels, one per coefficient across the tuned range, beside the steering file.
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

#: What a trial and `realize()` read to lay the keywords, fields and friction PEST wrote: the base values and where
#: each one goes. Written beside the study's templates, so every agent's copy carries it.
SPEC = "study_telemac.json"

#: The spatial fields a caller may name: the friction coefficient, the node-scatter map `FORMATTED DATA FILE 2` names
#: (by its deck keyword or its column), and the geometry file's bed or bottom friction.
_SCATTER_NAMES = ("FORMATTED_DATA_FILE_2", "CN2", "KS")
_BOTTOM_NAMES = ("BOTTOM", "BOTTOM_FRICTION")
_FRICTION = "FRICTION_COEFFICIENT"

#: pyEMU 1.7.0 PstFrom's pilot-point spacing on a grid when none is stated: every 10th cell; its variogram's range is
#: that spacing (a = pp_space x the cell size), an exponential structure of contribution 1.
_EVERY = 10
_PYEMU = ("pyEMU 1.7.0 PstFrom default: a pilot point every 10th cell, an exponential variogram (contribution 1) whose "
          "range is that spacing; here a cell is the median edge of the run's mesh")


@dataclass(frozen=True)
class Deck:
    """What a staged TELEMAC deck states that a study reads: its steering file's name, every keyword it states, the
    module, the result file the finished run wrote, the frame its UTM coordinates are in, the coupled steering files
    it names, and the length its outflow level was derived over."""

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
    #: The coupled steering files the host names, by module, and each one's stated keywords.
    coupled: Mapping[str, str] = MappingProxyType({})
    coupled_stated: Mapping[str, Mapping[str, list[str]]] = MappingProxyType({})
    #: The median edge of the mesh's unique triangle edges, the cell a pilot point's default spacing is counted in.
    cell_size: float | None = None


def command(the_deck: Deck) -> str:
    """What a trial runs: the fields and keywords PEST wrote, the outflow level at the trial's own friction, then the
    deck through TELEMAC's own launcher, unpartitioned (the script execs it).

    The agents are the study's parallelism: a deck's own PARALLEL PROCESSORS would run that many ranks per trial.
    PEST++ starts the command without a shell, so a redirection would reach the launcher as arguments."""
    return f"python {TRIAL} {the_deck.module} {the_deck.steering}"


def deck(directory: Path, finished: Any) -> Deck:
    """The finished run's steering file as it was staged, with the coupled steering files it names; a run whose record
    says it ended UNSTEADY, a deck of another module, or one stating the keywords the study writes, refuses."""
    from trid3nt.workflows.telemac.authoring.staged_check import read_steering

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
    coupled, coupled_stated = _coupled_bodies(directory, stated)
    return Deck(steering=steering, stated=stated, module=module, result=str(results[0]),
                utm_epsg=int(case["server_facts"]["utm_epsg"]),
                reach_length_m=case["server_facts"].get("reach_length_m"), level=level, unheld=unheld,
                coupled=coupled, coupled_stated=coupled_stated, cell_size=_deck_cell_size(directory, stated))


def _coupled_bodies(directory: Path, stated: Mapping[str, list[str]]
                    ) -> tuple[dict[str, str], dict[str, Mapping[str, list[str]]]]:
    """The steering files the host's ``COUPLING WITH`` names -> ``{module: filename}`` and each file's keywords."""
    from trid3nt.workflows.telemac.authoring.staged_check import read_steering
    from trid3nt.workflows.telemac.modules import WRAPPERS

    coupled: dict[str, str] = {}
    bodies: dict[str, Mapping[str, list[str]]] = {}
    for word in stated.get("COUPLING WITH") or ():
        module = str(word).strip().lower()
        if not module:
            continue
        if module not in WRAPPERS:
            raise PestppError(f"the deck couples {module!r}, which is no TELEMAC module the study reads; it reads "
                              f"{', '.join(sorted(WRAPPERS))}", error_code="PESTPP_DECK_UNREAD")
        keyword = f"{module.upper()} STEERING FILE"
        name = (stated.get(keyword) or [None])[0]
        if not name:
            raise PestppError(f"the deck couples {module.upper()} and states no {keyword}, so the study cannot read "
                              "the body it couples", error_code="PESTPP_DECK_UNREAD")
        path = directory / str(name)
        if not path.is_file():
            raise PestppError(f"the deck couples {module.upper()} through {name!r}, and the staged deck holds no such "
                              "file", error_code="PESTPP_DECK_UNREAD")
        coupled[module] = str(name)
        bodies[module] = read_steering(path)
    return coupled, bodies


def _deck_cell_size(directory: Path, stated: Mapping[str, list[str]]) -> float | None:
    """The median edge of the mesh's unique triangle edges; None where the deck names no readable geometry."""
    name = (stated.get("GEOMETRY FILE") or [None])[0]
    if not name or not (directory / name).is_file():
        return None
    from trid3nt.workflows.telemac.authoring.selafin_io import read_selafin

    geometry = read_selafin(directory / name)
    return _median_edge(geometry["ikle2"], geometry["x"], geometry["y"])


def _median_edge(cells: Any, x: Any, y: Any) -> float:
    """The median length of the mesh's edges, each edge counted once."""
    import numpy as np

    xy = np.column_stack([np.asarray(x, dtype=float), np.asarray(y, dtype=float)])
    edges: set[tuple[int, int]] = set()
    for row in np.asarray(cells, dtype=int):
        ids = [int(i) for i in row]
        edges.update((min(a, b), max(a, b)) for a, b in zip(ids, ids[1:] + ids[:1]) if a != b)
    pairs = np.array(sorted(edges))
    return float(np.median(np.hypot(*(xy[pairs[:, 0]] - xy[pairs[:, 1]]).T)))


def _measured_level(directory: Path, stated: Mapping[str, list[str]], finished: Any
                    ) -> tuple[dict[str, Any] | None, str]:
    """The outflow level a gauge measured as the run's record and deck hold it -> (its entry, column, value, sd and
    cite; or None) and the code saying why it is none: a level the record cites no accuracy for (the reading's, and
    the gauge zero's where one lifts it), or one the deck does not hold at one boundary, is no parameter."""
    import math

    from trid3nt.inputs.slots import LEVEL

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


# The dictionary: which keyword a name is, and what its own module declares it to be.


def _selector(identifier: str) -> tuple[str, int | None]:
    """``NAME@N`` -> ``(NAME, N)``; a selector that is not an integer from 1 refuses."""
    base, _, selector = str(identifier).partition("@")
    if not selector:
        return base, None
    if not selector.isdigit() or int(selector) < 1:
        raise PestppError(f"{identifier} names list entry {selector!r}; a selector is an integer from 1",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    return base, int(selector)


def _bodies(the_deck: Deck) -> list[Any]:
    """The deck's active bodies: the host module and every module its coupling names."""
    from trid3nt.workflows.telemac.modules import wrapper_for

    return [wrapper_for(the_deck.module), *(wrapper_for(module) for module in the_deck.coupled)]


def _resolve(identifier: str, the_deck: Deck) -> tuple[Any, str, Any, int | None]:
    """``identifier`` against the deck's active bodies -> (body, identifier, slot, list selector or None).

    A qualified name is read against its own body; a bare name where exactly one body spells it; an unknown name or
    one of a module the deck does not couple refuses naming the deck's active bodies."""
    from trid3nt.workflows.runtime.module import SlotRefused, identify_on

    base, index = _selector(identifier)
    try:
        body, found = identify_on(_bodies(the_deck), base)
    except SlotRefused as refused:
        active = ", ".join(b.MODULE for b in _bodies(the_deck))
        raise PestppError(f"{identifier} names no keyword of the run's active TELEMAC bodies ({active}): {refused}",
                          error_code="PESTPP_PARAMETER_UNPLACED") from refused
    slot = body.MODULE_INPUT[found]
    return body, found, slot, index


def _target_file(the_deck: Deck, body: Any) -> str:
    """The steering file a body's keyword lives in: the host's, or the coupled steering file it names."""
    return the_deck.steering if body.MODULE == the_deck.module else str(the_deck.coupled[body.MODULE])


def _stated(the_deck: Deck, body: Any) -> Mapping[str, list[str]]:
    """The keywords a body's steering file states."""
    return the_deck.stated if body.MODULE == the_deck.module else the_deck.coupled_stated[body.MODULE]


def _values(the_deck: Deck, body: Any, slot: Any) -> list[str] | None:
    """The value(s) a keyword states in its steering file, in the file's own order; None where it states none."""
    held = _stated(the_deck, body).get(slot.keyword)
    return list(held) if held else None


def _numeric_default(slot: Any) -> list[float] | None:
    """The dictionary row's default where it is a finite, non-zero numeric (or numeric list), else None."""
    from trid3nt.workflows.runtime.module import UNSET

    default = slot.engine_default
    if default is UNSET or default is None:
        return None
    values = list(default) if isinstance(default, (list, tuple)) else [default]
    if not values:
        return None
    parsed: list[float] = []
    for value in values:
        if type(value) not in (int, float) or not math.isfinite(value):
            return None
        parsed.append(float(value))
    return parsed if any(value != 0.0 for value in parsed) else None


def _restate_default(directory: Path, the_deck: Deck, body: Any, slot: Any) -> list[float] | None:
    """A REAL keyword the deck leaves unstated, restated at its finite non-zero dictionary default -> its values.

    None where the dictionary states no such default, so the caller refuses by name."""
    default = _numeric_default(slot)
    if default is None:
        return None
    from trid3nt.workflows.telemac.authoring.serializer import restate

    restate(directory, _target_file(the_deck, body), body.MODULE,
            {slot.keyword: default if isinstance(slot.engine_default, (list, tuple)) else default[0]})
    return default


def _parse_values(identifier: str, values: Sequence[str]) -> list[float]:
    """A keyword's stated value(s) as numbers; a value the engine reads as something else refuses by name."""
    parsed: list[float] = []
    for token in values:
        try:
            number = float(str(token).strip())
        except (TypeError, ValueError) as refused:
            raise PestppError(f"{identifier} states {token!r}, which is not a number",
                              error_code="PESTPP_PARAMETER_UNPLACED") from refused
        parsed.append(number)
    return parsed


def _pest_name(module: str, identifier: str, the_deck: Deck) -> str:
    """The PEST name a keyword writes through: the host's bare, a coupled body's prefixed by its module."""
    return identifier.lower() if module == the_deck.module else f"{module}_{identifier}".lower()


def _field(name: str) -> str:
    """The template field a parameter's name writes through, this wide so PEST's own exponent form fits."""
    return "~" + name.ljust(_FIELD - 2) + "~"


def _read_source(directory: Path, file: str) -> str:
    """The file's text, or the template a prior parameter left, its ``ptf ~`` header dropped."""
    written = directory / f"{file}.tpl"
    return written.read_text().split("\n", 1)[1] if written.is_file() else (directory / file).read_text()


def _write_template(directory: Path, file: str, text: str) -> tuple[str, str]:
    (directory / f"{file}.tpl").write_text("ptf ~\n" + text)
    return f"{file}.tpl", file


def _assignment(text: str, keyword: str, *, whole: bool = False) -> tuple[re.Pattern[str], re.Match[str]]:
    """The one line stating ``keyword`` and the match over it; zero or several refuse naming the count.

    ``whole`` takes the line's value text entire (a list and its separators); otherwise the value is one non-space
    token and the rest of the line is left as it was."""
    body = rf"([^\r\n]*)$" if whole else r"(\S+)(.*)$"
    pattern = re.compile(rf"^(\s*{re.escape(keyword)}\s*[=:]\s*)" + body, re.M | re.I)
    found = list(pattern.finditer(text))
    if len(found) != 1:
        raise PestppError(f"the run's steering file states {keyword} {len(found)} times, and a multiplier is "
                          "written into the one value it states", error_code="PESTPP_PARAMETER_UNPLACED")
    return pattern, found[0]


def _templated(text: str, keyword: str, field: str) -> tuple[str, float]:
    """``text`` with the scalar keyword written as ``field`` -> the text and the one value it stated."""
    _pattern, match = _assignment(text, keyword)
    value = match.group(2).strip()
    return text[:match.start()] + f"{match.group(1)}{field}{match.group(3)}" + text[match.end():], float(value)


def _templated_entry(text: str, keyword: str, index: int, field: str) -> tuple[str, float]:
    """``text`` with entry ``index`` (1-based) of a list keyword written as ``field`` -> the text and that entry."""
    _pattern, match = _assignment(text, keyword, whole=True)
    parts = [part.strip() for part in match.group(2).split(";")]
    if index > len(parts):
        raise PestppError(f"{keyword} states {len(parts)} value(s), and entry {index} is named",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    base = float(parts[index - 1])
    parts[index - 1] = field
    return text[:match.start()] + f"{match.group(1)}{';'.join(parts)}" + text[match.end():], base


def _templated_list(text: str, keyword: str, count: int, field: str) -> tuple[str, float]:
    """``text`` with every entry of an all-equal list keyword written as the same ``field`` -> the text and the value.

    PEST++ substitutes one parameter's value into every field of that name, so one multiplier scales a list whose
    entries are equal by writing the field once per entry."""
    _pattern, match = _assignment(text, keyword, whole=True)
    parts = [part.strip() for part in match.group(2).split(";")]
    if len(parts) != count:
        raise PestppError(f"{keyword} states {len(parts)} value(s), and the study read {count}",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    return (text[:match.start()] + f"{match.group(1)}{';'.join([field] * count)}" + text[match.end():],
            float(parts[0]))


def _side_value(directory: Path, name: str) -> tuple[str, str]:
    """A side file carrying one multiplier as PEST writes it -> (template, model file)."""
    model = f"{name}.dat"
    (directory / model).write_text("1.0\n")
    (directory / f"{model}.tpl").write_text("ptf ~\n" + f"{_field(name)}\n")
    return f"{model}.tpl", model


def _load_spec(directory: Path) -> dict[str, Any]:
    path = directory / SPEC
    return json.loads(path.read_text()) if path.is_file() else {}


def _save_spec(directory: Path, spec: Mapping[str, Any]) -> None:
    (directory / SPEC).write_text(json.dumps(dict(spec), indent=1))


def _entry(spec: dict[str, Any], key: str) -> list[dict[str, Any]]:
    held = spec.setdefault(key, [])
    return held


# The spatial fields: friction, the node-scatter map, the geometry's bed and bottom friction.


def _field_kind(identifier: str, the_deck: Deck | None) -> dict[str, Any] | None:
    """What field ``identifier`` names, or None where it names no spatial field of this deck."""
    name = str(identifier).upper().replace(" ", "_")
    if the_deck is None:
        return None
    if name == _FRICTION:
        return {"kind": "friction"}
    if name in _SCATTER_NAMES:
        file = (the_deck.stated.get("FORMATTED DATA FILE 2") or [None])[0]
        if not file:
            raise PestppError(f"{identifier} names the node-scatter field, and the run states no FORMATTED DATA FILE 2",
                              error_code="PESTPP_PARAMETER_UNPLACED")
        return {"kind": "scatter", "file": str(file)}
    if name in _BOTTOM_NAMES:
        geometry = (the_deck.stated.get("GEOMETRY FILE") or [None])[0]
        if not geometry:
            raise PestppError(f"{identifier} names a geometry variable, and the run states no GEOMETRY FILE",
                              error_code="PESTPP_PARAMETER_UNPLACED")
        return {"kind": "bottom", "file": str(geometry), "variable": "BOTTOM" if name == "BOTTOM" else "BOTTOM FRICTION"}
    return None


def _friction_data(the_deck: Deck) -> bool:
    return str((the_deck.stated.get("FRICTION DATA") or [""])[0]).upper() in ("YES", "TRUE", "OUI", "VRAI")


def _mesh(directory: Path, the_deck: Deck) -> tuple[Any, Any, Any, dict[str, Any]]:
    """The deck's mesh nodes (npoin x 2), triangles (ikle2, 0-based), bed, and the whole geometry read."""
    import numpy as np

    from trid3nt.workflows.telemac.authoring.selafin_io import read_selafin

    geometry = read_selafin(directory / the_deck.stated["GEOMETRY FILE"][0])
    bed = next(values for name, values in geometry["data"].items() if name.upper() in ("BOTTOM", "FOND"))
    xy = np.column_stack([geometry["x"], geometry["y"]])
    return xy, geometry["ikle2"], np.asarray(bed[0], dtype=float), geometry


def _read_scatter(path: Path) -> tuple[list[str], list[list[float]], list[list[str]]]:
    """A node-scatter map's comment lines, its X/Y/value rows, and each row's trailing tokens."""
    comments: list[str] = []
    rows: list[list[float]] = []
    rest: list[list[str]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            comments.append(line)
            continue
        parts = line.split()
        if len(parts) < 3:
            raise PestppError(f"{path.name} states a row of {len(parts)} columns, and a scatter map is X Y value",
                              error_code="PESTPP_DECK_UNREAD")
        rows.append([float(parts[0]), float(parts[1]), float(parts[2])])
        rest.append(parts[3:])
    return comments, rows, rest


def _read_geometry_variable(directory: Path, the_deck: Deck, variable: str) -> Any:
    """One geometry variable's first-frame values; a variable the file does not carry refuses."""
    from trid3nt.workflows.telemac.authoring.selafin_io import geometry_variable

    return geometry_variable(directory / the_deck.stated["GEOMETRY FILE"][0], variable)


def _write_geometry_variable(path: Path, variable: str, values: Any) -> None:
    """``path`` rewritten with ``variable`` carrying ``values`` (added, or replaced where it already exists)."""
    from trid3nt.workflows.telemac.authoring.selafin_io import write_geometry_variable

    write_geometry_variable(path, variable, values)


def _friction_table(directory: Path, the_deck: Deck) -> list[dict[str, Any]]:
    """The FRICTION DATA FILE's zone rows -> each zone, law and coefficient."""
    name = (the_deck.stated.get("FRICTION DATA FILE") or [None])[0]
    if not name:
        raise PestppError("the run states FRICTION DATA = YES and no FRICTION DATA FILE",
                          error_code="PESTPP_DECK_UNREAD")
    rows: list[dict[str, Any]] = []
    for line in (directory / name).read_text().splitlines():
        text = line.strip()
        if not text or text.startswith(("*", "#", "/")):
            continue
        if text.upper() == "END":
            break
        parts = text.split()
        if len(parts) < 3:
            raise PestppError(f"{name} states the row {text!r}, and a zone row is an id, a law and a coefficient",
                              error_code="PESTPP_DECK_UNREAD")
        rows.append({"zone": parts[0], "law": parts[1], "coef": float(parts[2]), "rest": parts[3:]})
    if not rows:
        raise PestppError(f"{name} states no friction zone", error_code="PESTPP_DECK_UNREAD")
    return rows


def _friction_node_zone(directory: Path, the_deck: Deck) -> list[str]:
    """Each mesh node's friction zone, read off the ZONES FILE, in the geometry's node order."""
    name = (the_deck.stated.get("ZONES FILE") or [None])[0]
    if not name:
        raise PestppError("the run states FRICTION DATA = YES and no ZONES FILE", error_code="PESTPP_DECK_UNREAD")
    _xy, _cells, _bed, geometry = _mesh(directory, the_deck)
    node_zone = [""] * int(geometry["npoin"])
    for line in (directory / name).read_text().splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        node = int(parts[0]) - 1
        if 0 <= node < len(node_zone):
            node_zone[node] = parts[1]
    if any(not zone for zone in node_zone):
        raise PestppError(f"{name} assigns no friction zone to {sum(not z for z in node_zone)} mesh node(s)",
                          error_code="PESTPP_DECK_UNREAD")
    return node_zone


def _friction_base_nodes(directory: Path, the_deck: Deck) -> tuple[str, Any]:
    """The run's per-node friction before any multiplier -> (the kind, the array).

    With FRICTION DATA = YES each node takes its zone's coefficient; otherwise every node takes the stated scalar."""
    import numpy as np

    xy, _cells, _bed, _geometry = _mesh(directory, the_deck)
    if _friction_data(the_deck):
        table = {row["zone"]: row["coef"] for row in _friction_table(directory, the_deck)}
        node_zone = _friction_node_zone(directory, the_deck)
        try:
            values = np.array([table[zone] for zone in node_zone], dtype=float)
        except KeyError as missing:
            raise PestppError(f"the ZONES FILE assigns zone {missing.args[0]!r}, which the FRICTION DATA FILE does "
                              "not carry", error_code="PESTPP_DECK_UNREAD") from missing
        return "data", values
    stated = _stated_scalar(the_deck, "FRICTION COEFFICIENT")
    if stated is None or stated == 0.0:
        raise PestppError("the run states no non-zero FRICTION COEFFICIENT, so no per-node friction has a base to "
                          "scale", error_code="PESTPP_PARAMETER_UNPLACED")
    return "geometry", np.full(xy.shape[0], stated, dtype=float)


def _stated_scalar(the_deck: Deck, keyword: str) -> float | None:
    values = the_deck.stated.get(keyword)
    try:
        return float(values[0]) if values else None
    except (TypeError, ValueError):
        return None


def _friction_info(directory: Path, the_deck: Deck, spec: dict[str, Any]) -> dict[str, Any]:
    """The spec's one friction entry, created from the deck the first time a friction parameter needs it."""
    held = spec.get("friction")
    if held is not None:
        return held
    kind, base = _friction_base_nodes(directory, the_deck)
    info: dict[str, Any] = {
        "module": the_deck.module, "steering": the_deck.steering, "data": kind == "data",
        "geometry": str(the_deck.stated["GEOMETRY FILE"][0]),
        "data_file": (the_deck.stated.get("FRICTION DATA FILE") or [None])[0],
        "zones_file": (the_deck.stated.get("ZONES FILE") or [None])[0],
        "base_nodes": [float(v) for v in base],
        "pieces": [], "pilot": None, "whole": None,
    }
    if kind == "data":
        info["table"] = _friction_table(directory, the_deck)
        info["node_zone"] = _friction_node_zone(directory, the_deck)
    else:
        info["scalar"] = _stated_scalar(the_deck, "FRICTION COEFFICIENT")
    spec["friction"] = info
    return info


def _field_base(directory: Path, the_deck: Deck, info: Mapping[str, Any]) -> list[float]:
    """A non-friction field's base values, per mesh node."""
    if info["kind"] == "scatter":
        _comments, rows, _rest = _read_scatter(directory / info["file"])
        return [row[2] for row in rows]
    return [float(v) for v in _read_geometry_variable(directory, the_deck, info["variable"])]


def _field_info(directory: Path, the_deck: Deck, spec: dict[str, Any], identifier: str,
                info: Mapping[str, Any]) -> dict[str, Any]:
    """The spec's entry for one non-friction field, created with the field's base values."""
    for held in spec.get("fields", []):
        if held["kind"] == info["kind"] and held["file"] == info["file"] \
                and held.get("variable") == info.get("variable"):
            return held
    base = _field_base(directory, the_deck, info)
    held = {"kind": info["kind"], "file": info["file"], "variable": info.get("variable"),
            "base": base, "pieces": [], "pilot": None}
    spec.setdefault("fields", []).append(held)
    return held


def _piece_multipliers(directory: Path, info: Mapping[str, Any], values: Mapping[str, float],
                       source_dir: Path | None = None) -> Any:
    """Each mesh node's multiplier from the entry's whole value, zone pieces and pilot points."""
    import numpy as np

    size = len(info.get("base_nodes") or info.get("base") or ())
    multiplier = np.ones(size, dtype=float)
    if info.get("whole"):
        multiplier *= float(values[info["whole"]])
    for piece in info.get("pieces", []):
        value = float(values[piece["name"]])
        if "nodes" in piece and piece["nodes"] is not None:
            multiplier[[int(n) for n in piece["nodes"]]] = value
        elif "rows" in piece and info.get("node_zone"):
            zones = {str(info["table"][row]["zone"]) for row in piece["rows"]}
            held = [index for index, zone in enumerate(info["node_zone"]) if zone in zones]
            multiplier[held] = value
    pilot = info.get("pilot")
    if pilot:
        multiplier *= _kriged(source_dir or directory, pilot, values)
    return multiplier


def _kriged(directory: Path, pilot: Mapping[str, Any], values: Mapping[str, float]) -> Any:
    """The pilot points' kriged multiplier over every mesh node, 10 ** (the kriging factors over the log10 values)."""
    import numpy as np

    held = np.load(directory / pilot["npz"])
    names = [str(name) for name in held["names"]]
    stated = np.log10(np.array([float(values[name]) for name in names], dtype=float))
    return 10.0 ** (held["weight"] * stated[held["index"]]).sum(axis=1)


def _apply_friction(directory: Path, info: dict[str, Any], values: Mapping[str, float],
                    source_dir: Path | None = None) -> Any:
    """Lay the fitted friction into the deck's geometry or its data file -> the per-node coefficient laid."""
    import numpy as np

    multiplier = _piece_multipliers(directory, info, values, source_dir)
    base = np.asarray(info["base_nodes"], dtype=float)
    laid = base * multiplier
    if not info["data"]:
        _write_geometry_variable(directory / info["geometry"], "BOTTOM FRICTION", laid)
        return laid
    by_row = not info.get("pilot") and not any("nodes" in piece for piece in info["pieces"])
    if by_row:
        rows = [dict(row) for row in info["table"]]
        if info.get("whole"):
            value = float(values[info["whole"]])
            for row in rows:
                row["coef"] = float(row["coef"]) * value
        for piece in info["pieces"]:
            value = float(values[piece["name"]])
            for index in piece["rows"]:
                rows[int(index)]["coef"] = float(rows[int(index)]["coef"]) * value
        _write_friction_rows(directory, info, rows)
        _write_friction_zones(directory, info, [str(zone) for zone in info["node_zone"]])
        return laid
    laws = {str(row["zone"]): row["law"] for row in info["table"]}
    rows = []
    for node, (zone, coefficient) in enumerate(zip(info["node_zone"], laid), start=1):
        rows.append({"zone": str(node), "law": laws[zone], "coef": float(coefficient), "rest": ["NULL"]})
    _write_friction_rows(directory, info, rows)
    _write_friction_zones(directory, info, [str(node) for node in range(1, len(laid) + 1)])
    return laid


def _write_friction_rows(directory: Path, info: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> None:
    lines = ["* PEST++ calibrated friction zones", "* zone  law      coef    vegetation"]
    for row in rows:
        tail = " ".join(row.get("rest") or ["NULL"])
        lines.append(f"{row['zone']} {row['law']} {float(row['coef'])!r} {tail}".rstrip())
    lines.append("END")
    (directory / info["data_file"]).write_text("\n".join(lines) + "\n")


def _write_friction_zones(directory: Path, info: Mapping[str, Any], node_zone: Sequence[str]) -> None:
    (directory / info["zones_file"]).write_text(
        "".join(f"{node} {zone}\n" for node, zone in enumerate(node_zone, start=1)))


def _apply_field(directory: Path, info: dict[str, Any], values: Mapping[str, float],
                 source_dir: Path | None = None) -> None:
    """Lay one field's fitted values into its scatter map or geometry variable."""
    import numpy as np

    base = np.asarray(info["base"], dtype=float)
    multiplier = np.ones(base.shape[0], dtype=float)
    if info.get("whole"):
        multiplier[:] = float(values[info["whole"]])
    for piece in info.get("pieces", []):
        value = float(values[piece["name"]])
        if piece.get("nodes") is None:
            multiplier[:] = value
        else:
            multiplier[[int(node) for node in piece["nodes"]]] = value
    if info.get("pilot"):
        multiplier *= _kriged(source_dir or directory, info["pilot"], values)
    laid = base * multiplier
    if info["kind"] == "scatter":
        written, at = [], 0
        for line in (directory / info["file"]).read_text().splitlines():
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                written.append(line)
                continue
            parts = line.split()
            parts[2] = f"{laid[at]:.10g}"
            written.append(" ".join(parts))
            at += 1
        (directory / info["file"]).write_text("\n".join(written) + "\n")
    elif info["kind"] == "bottom":
        _write_geometry_variable(directory / info["file"], info["variable"], laid)


def _apply_keywords(directory: Path, spec: Mapping[str, Any], values: Mapping[str, float]) -> dict[str, Any]:
    """Restate every whole-list keyword at its fitted multiplier -> the values written, by keyword."""
    from trid3nt.workflows.telemac.authoring.serializer import restate

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    written: dict[str, Any] = {}
    for entry in spec.get("keywords", []):
        multiplier = float(values[entry["name"]])
        scaled = [float(value) * multiplier for value in entry["base"]]
        grouped.setdefault((entry["steering"], entry["module"]), {})[entry["keyword"]] = scaled
        written[entry["keyword"]] = scaled
    for (steering, module), held in grouped.items():
        restate(directory, steering, module, held)
    return written


def apply_layings(directory: Path, spec: Mapping[str, Any], values: Mapping[str, float],
                  source_dir: Path | None = None) -> dict[str, Any]:
    """Apply every laying the spec records -> what was written and the friction laid, per mesh node.

    ``source_dir`` holds the pilot points' kriging factors where they are not beside the deck being written (a
    calibrated deck is built in a fresh run directory from the study's own files)."""
    written = _apply_keywords(directory, spec, values)
    for info in spec.get("fields", []):
        _apply_field(directory, info, values, source_dir)
    friction = spec.get("friction")
    laid = _apply_friction(directory, friction, values, source_dir) if friction else None
    return {"keywords": written, "friction": laid}


# Authoring: one parameter per keyword, list entry, zone or pilot point.


def multiplier(directory: Path, identifier: str, *, lower: float, upper: float, the_deck: Deck,
               absolute: bool = False, source: str = "") -> tuple[Parameter, tuple[str, str]]:
    """One multiplier parameter over the REAL keyword ``identifier`` names -> the parameter and its (template, file).

    Eligibility is the keyword's own module dictionary: a REAL keyword is tuned, a scalar at its one value, a list by
    one multiplier over the whole list (laid by the trial) or ``@N`` at entry N alone; an INTEGER, LOGICAL, STRING or
    file keyword refuses naming its type. A keyword the deck leaves unstated is restated at its finite non-zero
    dictionary default, or refuses. ``absolute`` bounds are stated in the keyword's own unit and divided by that value."""
    info = _field_kind(identifier, the_deck)
    if info is not None and info["kind"] != "friction":
        return _field_multiplier(directory, identifier, info, lower=lower, upper=upper, absolute=absolute,
                                 the_deck=the_deck, source=source)
    body, found, slot, index = _resolve(identifier, the_deck)
    if slot.is_file or slot.type != "REAL":
        raise PestppError(f"{found} is a {slot.type} keyword of {body.MODULE}"
                          + (" naming a file" if slot.is_file else "") + ", and a calibration tunes a REAL keyword. "
                          "An integer flag, a logical, a string or a file is no parameter.",
                          error_code="PESTPP_PARAMETER_NOT_REAL")
    if found == _FRICTION and _friction_data(the_deck):
        return _data_multiplier(directory, identifier, found, body, slot, index, lower=lower, upper=upper,
                                the_deck=the_deck, source=source)
    file = _target_file(the_deck, body)
    stated = _values(the_deck, body, slot)
    if stated is None:
        default = _restate_default(directory, the_deck, body, slot)
        if default is None:
            raise PestppError(f"{found} is unstated in {file} and the {body.MODULE} dictionary states no finite "
                              "non-zero default to restate it at, so no multiplier has a value to scale",
                              error_code="PESTPP_PARAMETER_UNPLACED")
        parsed = default
    else:
        parsed = _parse_values(found, stated)
    if slot.is_list or slot.unbounded or len(parsed) > 1:
        return _list_multiplier(directory, identifier, found, body, slot, index, parsed, file,
                                lower=lower, upper=upper, absolute=absolute, the_deck=the_deck, source=source)
    base = parsed[0]
    if base == 0.0:
        raise PestppError(f"{found} is 0.0 in the run, and a multiplier cannot scale it; state a non-zero value",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    name = _pest_name(body.MODULE, found, the_deck)
    text = _read_source(directory, file)
    text, _base = _templated(text, slot.keyword, _field(name))
    if absolute:
        lower, upper = lower / base, upper / base
        if not 0.0 < lower < 1.0 < upper:
            raise PestppError(f"{found} is {base:g} in the run, outside its stated bounds {lower * base:g} to "
                              f"{upper * base:g}", error_code="PESTPP_BOUNDS_UNSTATED")
    return (Parameter(name=name, value=1.0, lower=float(lower), upper=float(upper), group=name, scale=base,
                      source=source, original_name=found, body=body.MODULE, file=file),
            _write_template(directory, file, text))


def _list_multiplier(directory: Path, identifier: str, found: str, body: Any, slot: Any, index: int | None,
                     parsed: Sequence[float], file: str, *, lower: float, upper: float, absolute: bool,
                     the_deck: Deck, source: str) -> tuple[Parameter, tuple[str, str]]:
    """A list keyword: ``@N`` written at its one entry, or the whole list laid by the trial at one multiplier."""
    name = _pest_name(body.MODULE, found, the_deck)
    if index is not None:
        if index > len(parsed):
            raise PestppError(f"{found} states {len(parsed)} value(s), and entry {index} is named",
                              error_code="PESTPP_PARAMETER_UNPLACED")
        base = parsed[index - 1]
        if base == 0.0:
            raise PestppError(f"{found} entry {index} is 0.0 in the run, and a multiplier cannot scale it",
                              error_code="PESTPP_PARAMETER_UNPLACED")
        entry = f"{name}_e{index}"
        text = _read_source(directory, file)
        text, _base = _templated_entry(text, slot.keyword, index, _field(entry))
        if absolute:
            lower, upper = lower / base, upper / base
            if not 0.0 < lower < 1.0 < upper:
                raise PestppError(f"{found} entry {index} is {base:g} in the run, outside its stated bounds "
                                  f"{lower * base:g} to {upper * base:g}", error_code="PESTPP_BOUNDS_UNSTATED")
        return (Parameter(name=entry, value=1.0, lower=float(lower), upper=float(upper), group=entry, scale=base,
                          source=source, original_name=found, body=body.MODULE, file=file),
                _write_template(directory, file, text))
    if absolute:
        raise PestppError(f"{found} is bounded in the run's own unit and states {len(parsed)} values, so one "
                          "multiplier cannot carry them: state its bounds as factors",
                          error_code="PESTPP_BOUNDS_UNSTATED")
    if any(value == 0.0 for value in parsed):
        raise PestppError(f"{found} states a 0.0 entry, and a multiplier cannot scale the list",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    if len(set(parsed)) == 1:
        base = parsed[0]
        text = _read_source(directory, file)
        text, _base = _templated_list(text, slot.keyword, len(parsed), _field(name))
        return (Parameter(name=name, value=1.0, lower=float(lower), upper=float(upper), group=name, scale=base,
                          source=source, original_name=found, body=body.MODULE, file=file, list_len=len(parsed)),
                _write_template(directory, file, text))
    spec = _load_spec(directory)
    entry = {"name": name, "kind": "list", "module": body.MODULE, "steering": file,
             "keyword": slot.keyword, "base": list(parsed), "index": None}
    held = [row for row in spec.get("keywords", []) if row["name"] == name]
    if held:
        held[0].update(entry)
    else:
        _entry(spec, "keywords").append(entry)
    _save_spec(directory, spec)
    return (Parameter(name=name, value=1.0, lower=float(lower), upper=float(upper), group=name, scale=1.0,
                      source=source, original_name=found, body=body.MODULE, file=file),
            _side_value(directory, name))


def _data_multiplier(directory: Path, identifier: str, found: str, body: Any, slot: Any, index: int | None, *,
                     lower: float, upper: float, the_deck: Deck, source: str) -> tuple[Parameter, tuple[str, str]]:
    """FRICTION COEFFICIENT over a deck whose friction is a data table: one multiplier over every zone coefficient."""
    if index is not None:
        raise PestppError(f"{found} is a scalar, and entry {index} is named", error_code="PESTPP_PARAMETER_UNPLACED")
    spec = _load_spec(directory)
    info = _friction_info(directory, the_deck, spec)
    name = _pest_name(body.MODULE, found, the_deck)
    info["whole"] = name
    _save_spec(directory, spec)
    return (Parameter(name=name, value=1.0, lower=float(lower), upper=float(upper), group=name, scale=1.0,
                      source=source, original_name=found, body=body.MODULE, file=_target_file(the_deck, body)),
            _side_value(directory, name))


def _field_multiplier(directory: Path, identifier: str, info: Mapping[str, Any], *, lower: float, upper: float,
                      absolute: bool, the_deck: Deck, source: str) -> tuple[Parameter, tuple[str, str]]:
    """A whole-field multiplier over a spatial field (friction, the scatter map, the bed)."""
    spec = _load_spec(directory)
    if info["kind"] == "friction":
        held = _friction_info(directory, the_deck, spec)
    else:
        held = _field_info(directory, the_deck, spec, identifier, info)
    base = held.get("base_nodes") if info["kind"] == "friction" else held.get("base")
    if absolute:
        unique = sorted({float(value) for value in (base or [])})
        if len(unique) != 1:
            raise PestppError(f"{identifier} is bounded in the run's own unit, and its field states different values, "
                              "so one multiplier cannot carry them: state its bounds as factors",
                              error_code="PESTPP_BOUNDS_UNSTATED")
        scale = unique[0]
        lower, upper = lower / scale, upper / scale
        if not 0.0 < lower < 1.0 < upper:
            raise PestppError(f"{identifier} is {scale:g} in the run, outside its stated bounds",
                              error_code="PESTPP_BOUNDS_UNSTATED")
    name = str(identifier).lower().replace(" ", "_").replace(":", "_")
    held["whole"] = name
    _save_spec(directory, spec)
    return (Parameter(name=name, value=1.0, lower=float(lower), upper=float(upper), group=name, scale=1.0,
                      source=source, original_name=identifier, body=the_deck.module, file=the_deck.steering),
            _side_value(directory, name))


def fields(spatial: Mapping[str, Any], the_deck: Deck | None) -> dict[str, dict[str, Any]]:
    """Each pilot-point field as stated, pyEMU's own default where not -> ``{identifier: field}``: the spacing between
    its points and its exponential variogram's range, in metres of the run's mesh, each with its source.

    ``spatial`` is ``{identifier: {"spacing": S, "source": "...", "variogram": {"a": A, "source": "..."}}}``, any part
    left out. Unstated, the spacing is 10 of the mesh's median edges and the range that spacing (pyEMU's rule); a range
    left out of a stated spacing is that spacing. A variogram of another type, or a member not read, refuses."""
    from math import isfinite

    from ..helpers.geostat import NEAREST, VARIOGRAMS

    structure_defaults = {"contribution": 1.0, "nugget": 0.0, "anisotropy": 1.0, "bearing": 0.0}
    out: dict[str, dict[str, Any]] = {}
    for identifier, stated in spatial.items():
        info = _field_kind(identifier, the_deck)
        if info is None:
            active = ", ".join(b.MODULE for b in _bodies(the_deck)) if the_deck is not None else "none"
            raise PestppError(f"{identifier} is no spatial field of a TELEMAC-2D deck: it lays "
                              "FRICTION_COEFFICIENT, FORMATTED_DATA_FILE_2 (SCS CN2 or Green-Ampt KS), or BOTTOM; the "
                              f"run's active bodies are {active}", error_code="PESTPP_PARAMETER_UNPLACED")
        stated = dict(stated or {})
        variogram = dict(stated.get("variogram") or {})
        unread = ((stated.keys() - {"spacing", "source", "variogram", "maxpts_interp", "remove_negative_factors"})
                  | (variogram.keys() - {"a", "source", "type", *structure_defaults}))
        if unread:
            raise PestppError(f"{identifier}'s pilot points state {len(unread)} members a study does not read; it "
                              "reads spacing, source, variogram, maxpts_interp and remove_negative_factors",
                              error_code="PESTPP_PILOT_POINTS_UNREAD")
        kind = str(variogram.get("type") or "exponential").lower()
        if kind not in VARIOGRAMS:
            raise PestppError(f"{identifier}'s variogram {kind!r} is unsupported. State one of {', '.join(VARIOGRAMS)}.",
                              error_code="PESTPP_VARIOGRAM_UNKNOWN")
        structure = {name: variogram.get(name, default) for name, default in structure_defaults.items()}
        if (any(type(v) not in (int, float) or not isfinite(v) for v in structure.values())
                or structure["contribution"] <= 0 or structure["nugget"] < 0 or structure["anisotropy"] <= 0):
            raise PestppError("Variogram contribution and anisotropy must be positive, nugget nonnegative and "
                              "bearing finite.", error_code="PESTPP_PILOT_POINTS_INVALID")
        nearest = stated.get("maxpts_interp", NEAREST)
        remove_negative = stated.get("remove_negative_factors", True)
        if type(nearest) is not int or nearest < 1 or type(remove_negative) is not bool:
            raise PestppError("maxpts_interp takes a positive integer and remove_negative_factors takes a boolean.",
                              error_code="PESTPP_PILOT_POINTS_INVALID")
        if stated.get("spacing") is not None:
            spacing, spacing_source = float(stated["spacing"]), str(stated.get("source") or "stated by the user")
        else:
            spacing, spacing_source = _EVERY * float(the_deck.cell_size or 0.0), _PYEMU
        if variogram.get("a") is not None:
            a, a_source = float(variogram["a"]), str(variogram.get("source") or "stated by the user")
        else:
            a, a_source = spacing, _PYEMU if spacing_source == _PYEMU else "pyEMU 1.7.0 PstFrom default: the range is the spacing"
        if not (isfinite(spacing) and isfinite(a) and spacing > 0.0 and a > 0.0):
            raise PestppError(f"{identifier}'s pilot points are spaced {spacing:g} with a range of {a:g}; both above 0. "
                              "The default spacing needs the run's mesh, which this deck does not carry.",
                              error_code="PESTPP_PILOT_POINTS_UNSTATED")
        out[identifier] = {"spacing": spacing, "spacing_source": spacing_source, "variogram": kind,
                           "a": a, "a_source": a_source, **structure,
                           "structure_stated": sorted(variogram.keys() & structure_defaults.keys()),
                           "maxpts_interp": nearest, "remove_negative_factors": remove_negative,
                           "kriging_stated": bool({"maxpts_interp", "remove_negative_factors"} & stated.keys()),
                           "stated": bool(stated), **info}
    return out


def _placed(xy: Any, cells: Any, spacing: float) -> Any:
    """pyEMU's vertex-grid placement (get_zoned_ppoints_for_vertexgrid): an even grid over the nodes' extent,
    ceil(extent / spacing) points a side end to end, kept where the triangles buffered by one spacing cover them."""
    import numpy as np
    from shapely import points
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    low, high = xy.min(axis=0), xy.max(axis=0)
    xs, ys = (np.linspace(low[k], high[k], int(np.ceil((high[k] - low[k]) / spacing))) for k in (0, 1))
    xv, yv = np.meshgrid(xs, ys)
    grid = points(list(zip(xv.ravel(), yv.ravel())))
    cells = unary_union([Polygon(xy[row]) for row in np.asarray(cells, dtype=int)])
    return np.array([(p.x, p.y) for p in grid[cells.buffer(spacing).covers(grid)]])


def pilot_points(directory: Path, identifier: str, field: Mapping[str, Any], *, lower: float, upper: float,
                 the_deck: Deck, source: str = "") -> tuple[list[Parameter], list[tuple[str, str]]]:
    """The spatial field ``identifier`` names laid by pilot points -> each point's parameter and the (template, file)
    pairs PEST writes their values through.

    The points are placed as pyEMU places them over the mesh at the field's spacing, and each node's ordinary kriging
    factors over them are written beside it for the trial, which lays the field's own values times 10 ** (the factors
    over the log10 of the points). Each point is a multiplier starting at 1 inside the parameter's bounds, carrying its
    place and the variogram's range for the prior."""
    import numpy as np

    from ..helpers.geostat import kriging_factors

    if field.get("kind") not in ("friction", "scatter", "bottom"):
        raise PestppError(f"{identifier} is no spatial field a TELEMAC-2D deck lays",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    spec = _load_spec(directory)
    if field["kind"] == "friction":
        info = _friction_info(directory, the_deck, spec)
        base = list(info["base_nodes"])
    else:
        info = _field_info(directory, the_deck, spec, identifier, field)
        base = list(info["base"])
    xy, cells, _bed, _geometry = _mesh(directory, the_deck)
    if len(base) != xy.shape[0]:
        raise PestppError(f"{identifier}'s field states {len(base)} value(s) over the mesh's {xy.shape[0]} nodes",
                          error_code="PESTPP_DECK_UNREAD")
    sites = _placed(xy, cells, float(field["spacing"]))
    name = str(identifier).lower().replace(" ", "_").replace(":", "_")
    names = [f"{name}_pp{k:04d}" for k in range(len(sites))]
    try:
        index, weight = kriging_factors(xy, sites, float(field["a"]), variogram=field["variogram"],
                                        maxpts_interp=field["maxpts_interp"],
                                        remove_negative_factors=field["remove_negative_factors"],
                                        contribution=field["contribution"], nugget=field["nugget"],
                                        anisotropy=field["anisotropy"], bearing=field["bearing"])
    except np.linalg.LinAlgError as exc:
        raise PestppError(f"{identifier}'s variogram cannot interpolate the pilot points.",
                          error_code="PESTPP_KRIGING_SINGULAR") from exc
    stem = f"{name}.pp"
    np.savez(directory / f"{stem}.npz", names=np.array(names), index=index, weight=weight, base=np.array(base))
    (directory / f"{stem}.dat").write_text("".join(f"{held} 1.0\n" for held in names))
    (directory / f"{stem}.dat.tpl").write_text("ptf ~\n" + "".join(f"{held} ~{held.ljust(_FIELD - 2)}~\n"
                                                                   for held in names))
    info["pilot"] = {"name": name, "npz": f"{stem}.npz", "values": f"{stem}.dat"}
    _save_spec(directory, spec)
    parameters = [Parameter(name=held, value=1.0, lower=float(lower), upper=float(upper), group=name,
                            site=(float(x), float(y)), correlation=float(field["a"]),
                            variogram=field["variogram"], contribution=field["contribution"], nugget=field["nugget"],
                            anisotropy=field["anisotropy"], bearing=field["bearing"], spatial_group=name,
                            source=source, original_name=identifier, body=the_deck.module)
                  for held, (x, y) in zip(names, sites)]
    return parameters, [(f"{stem}.dat.tpl", f"{stem}.dat")]


def _zone_nodes(zone: Mapping[str, Any], directory: Path, the_deck: Deck) -> list[int]:
    """The mesh nodes one zone selects: its 1-based node ids, or the nodes inside its polygon in the run's frame."""
    keys = set(zone) - {"source", "crs"}
    if len(keys) != 1:
        raise PestppError("a zone states one of nodes or polygon, its source and any CRS",
                          error_code="PESTPP_ZONE_INVALID")
    (kind, wanted), = ((kind, zone[kind]) for kind in keys)
    if kind == "nodes":
        if not isinstance(wanted, (list, tuple)) or not wanted:
            raise PestppError("a zone's nodes is a nonempty list of 1-based mesh node ids",
                              error_code="PESTPP_ZONE_INVALID")
        xy, _cells, _bed, _geometry = _mesh(directory, the_deck)
        nodes = []
        for value in wanted:
            node = int(value) - 1
            if node < 0 or node >= xy.shape[0]:
                raise PestppError(f"a zone names mesh node {value}, and the mesh holds 1 to {xy.shape[0]}",
                                  error_code="PESTPP_ZONE_INVALID")
            nodes.append(node)
        return nodes
    if kind == "polygon":
        return _polygon_nodes(directory, the_deck, wanted, crs=zone.get("crs"))
    raise PestppError(f"a {kind} zone is no selector of a spatial field: state nodes, polygon, or friction zones",
                      error_code="PESTPP_ZONE_INVALID")


def _polygon_nodes(directory: Path, the_deck: Deck, polygon: Sequence[Sequence[float]], *, crs: Any = None) -> list[int]:
    """The mesh nodes the polygon's ring covers, reprojected from its frame (longitude/latitude by default) into the
    run's own frame here, never handed to the caller."""
    from pyproj import Transformer
    from shapely.geometry import Point, Polygon

    ring = []
    for point in polygon:
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in point)):
            raise PestppError("a zone polygon is a list of finite [x, y] pairs", error_code="PESTPP_ZONE_INVALID")
        ring.append((float(point[0]), float(point[1])))
    source = 4326 if crs is None else _epsg(crs, "a zone polygon")
    if int(source) != int(the_deck.utm_epsg):
        ring = [Transformer.from_crs(int(source), int(the_deck.utm_epsg), always_xy=True).transform(x, y)
                for x, y in ring]
    zone = Polygon(ring)
    if not zone.is_valid or zone.area <= 0:
        raise PestppError("a zone polygon is a simple ring with area", error_code="PESTPP_ZONE_INVALID")
    xy, _cells, _bed, _geometry = _mesh(directory, the_deck)
    return [index for index, (x, y) in enumerate(xy) if zone.covers(Point(float(x), float(y)))]


def _epsg(value: Any, role: str) -> int:
    """A stated CRS as its EPSG code: an integer or ``EPSG:<code>``."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        matched = re.fullmatch(r"(?:EPSG:)?(\d{4,6})", value.strip(), re.I)
        if matched:
            return int(matched.group(1))
    raise PestppError(f"{role} states its CRS as an EPSG code or 'EPSG:<code>', not {value!r}.",
                      error_code="PESTPP_ZONE_CRS")


def zoned(directory: Path, identifier: str, zones: Sequence[Mapping[str, Any]], *, lower: float, upper: float,
          the_deck: Deck, absolute: bool = False, source: str = "") -> tuple[list[Parameter], list[tuple[str, str]]]:
    """One parameter per stated zone over the spatial field ``identifier`` names -> the parameters and the
    (template, file). Every node outside every stated zone is left as the run wrote it, and a node a second zone also
    holds refuses. A zone states ``nodes`` (1-based mesh node ids), a ``polygon`` in the run's frame, or - on a deck
    whose friction is a data table - the friction ``zones`` it selects."""
    info = _field_kind(identifier, the_deck)
    if info is None:
        raise PestppError(f"{identifier} is no spatial field a TELEMAC-2D deck zones",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    spec = _load_spec(directory)
    data = info["kind"] == "friction" and _friction_data(the_deck)
    held = _friction_info(directory, the_deck, spec) if info["kind"] == "friction" \
        else _field_info(directory, the_deck, spec, identifier, info)
    base = list(held["base_nodes"]) if info["kind"] == "friction" else list(held["base"])
    if absolute:
        unique = sorted({float(value) for value in base})
        if len(unique) != 1:
            raise PestppError(f"{identifier} is bounded in the run's own unit, and its field states different values, "
                              "so one multiplier cannot carry them: state its bounds as factors",
                              error_code="PESTPP_BOUNDS_UNSTATED")
        lower, upper = lower / unique[0], upper / unique[0]
        if not 0.0 < lower < 1.0 < upper:
            raise PestppError(f"{identifier} is {unique[0]:g} in the run, outside its stated bounds",
                              error_code="PESTPP_BOUNDS_UNSTATED")
    choices: list[tuple[Mapping[str, Any], list[int] | list[int]]] = []
    if data:
        table = {str(row["zone"]): index for index, row in enumerate(held["table"])}
        node_zone = held["node_zone"]
        for index, zone in enumerate(zones, start=1):
            keys = set(zone) - {"source", "crs"}
            if keys == {"zones"}:
                wanted = [str(value) for value in zone["zones"]]
                missing = [value for value in wanted if value not in table]
                if missing:
                    raise PestppError(f"{identifier}'s zone {index} names friction zone {missing[0]!r}, and the "
                                      "FRICTION DATA FILE states "
                                      f"{', '.join(sorted(table))}", error_code="PESTPP_ZONE_INVALID")
                selected = [table[value] for value in wanted]
                choices.append((zone, selected))
                continue
            selected = _zone_nodes(zone, directory, the_deck)
            choices.append((zone, selected))
    else:
        for zone in zones:
            keys = set(zone) - {"source", "crs"}
            if keys == {"zones"}:
                raise PestppError(f"{identifier}'s zone states friction zones, and friction zone ids are selectors "
                                  "only on a deck whose friction is a data table",
                                  error_code="PESTPP_ZONE_INVALID")
            choices.append((zone, _zone_nodes(zone, directory, the_deck)))
    # A row zone and a node zone must stay disjoint too: a node in a selected friction zone and in a node set would
    # take two multipliers.
    seen_nodes: dict[int, int] = {}
    seen_rows: dict[int, int] = {}
    for index, (zone, selected) in enumerate(choices, start=1):
        if not selected:
            raise PestppError(f"{identifier}'s zone {index} holds no node or row of the run",
                              error_code="PESTPP_ZONE_EMPTY")
        row_zone = data and (set(zone) - {"source", "crs"}) == {"zones"}
        if row_zone:
            for row in selected:
                if row in seen_rows:
                    raise PestppError(f"{identifier}'s zones {seen_rows[row]} and {index} both hold friction zone "
                                      f"{held['table'][row]['zone']!r}; state disjoint zones",
                                      error_code="PESTPP_ZONE_OVERLAP")
                seen_rows[row] = index
            wanted = {str(held["table"][row]["zone"]) for row in selected}
            affected = [node for node, held_zone in enumerate(held["node_zone"]) if held_zone in wanted]
        else:
            affected = selected
        for node in affected:
            if node in seen_nodes:
                raise PestppError(f"{identifier}'s zones {seen_nodes[node]} and {index} both hold mesh node "
                                  f"{node + 1}; state disjoint zones", error_code="PESTPP_ZONE_OVERLAP")
            seen_nodes[node] = index
    parameters, pairs = [], []
    base_name = str(identifier).lower().replace(" ", "_").replace(":", "_")
    for index, (zone, selected) in enumerate(choices, start=1):
        name = f"{base_name}_z{index}"
        if data and (set(zone) - {"source", "crs"}) == {"zones"}:
            held["pieces"].append({"name": name, "rows": list(selected)})
        else:
            held["pieces"].append({"name": name, "nodes": list(selected)})
        parameters.append(Parameter(name=name, value=1.0, lower=float(lower), upper=float(upper), group=name,
                                    scale=1.0, source=source, original_name=identifier, body=the_deck.module,
                                    zone=_kind_of(zone), zone_source=str(zone.get("source") or "")))
        pairs.append(_side_value(directory, name))
    _save_spec(directory, spec)
    return parameters, pairs


def _kind_of(zone: Mapping[str, Any]) -> str:
    """The selector a zone states, for its record."""
    keys = set(zone) - {"source", "crs"}
    return next(iter(keys)) if len(keys) == 1 else ""


def interface(source: Path, dest: Path, chosen: Sequence[tuple[str, float, float, bool]], *, the_deck: Deck,
              targets: Sequence[tuple[Observed, int]], spatial: Mapping[str, Any] = {},
              zones: Mapping[str, Sequence[Mapping[str, Any]]] = {}, sources: Mapping[str, str] = {}
              ) -> tuple[list[Parameter], list[tuple[str, str]], list[tuple[str, str]]]:
    """A study directory: the deck at ``source`` copied to ``dest`` with its gauges printed and a multiplier template or
    field laying over each chosen ``(identifier, lower, upper, absolute)`` -> ``(parameters, templates, instructions)``.
    A field stated with pilot points is laid first, so a keyword template over another file is written over the field."""
    fields(spatial, the_deck)
    shutil.copytree(source, dest)
    instructions = [observe_levels(dest, targets, the_deck=the_deck)]
    parameters, templates = [], []
    for identifier, lower, upper, absolute in chosen:
        if identifier in spatial and identifier in zones:
            raise PestppError(f"{identifier} is both laid by pilot points and divided into zones; state one",
                              error_code="PESTPP_OPTION_CONFLICT")
        source_of = sources.get(identifier.lower(), "")
        if identifier in spatial:
            if absolute:
                raise PestppError(f"{identifier} is laid by pilot points, each a multiplier on the run's own values, "
                                  "so its bounds are factors, not values in the run's own unit",
                                  error_code="PESTPP_BOUNDS_UNSTATED")
            laid, pairs = pilot_points(dest, identifier, spatial[identifier], lower=lower, upper=upper,
                                       the_deck=the_deck, source=source_of)
            parameters.extend(laid)
            templates.extend(pair for pair in pairs if pair not in templates)
            continue
        if identifier in zones:
            laid, pairs = zoned(dest, identifier, zones[identifier], lower=lower, upper=upper, the_deck=the_deck,
                                absolute=absolute, source=source_of)
            parameters.extend(laid)
            templates.extend(pair for pair in pairs if pair not in templates)
            continue
        info = _field_kind(identifier, the_deck)
        if info is not None and (info["kind"] != "friction" or _friction_data(the_deck)):
            parameter, pair = _field_multiplier(dest, identifier, info, lower=lower, upper=upper, absolute=absolute,
                                                the_deck=the_deck, source=source_of)
        else:
            parameter, pair = multiplier(dest, identifier, lower=lower, upper=upper, the_deck=the_deck,
                                         absolute=absolute, source=source_of)
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


def targets(gauges: Sequence[Any], *, the_deck: Deck, finished: Any, frame: str
            ) -> tuple[list[tuple[Observed, int]], list[str]]:
    """Each gauge's water level as a target at the node the run's own point read lands it on -> ``[(observation,
    node)]`` and what was left out.

    A reading in no length unit, on a frame other than the run's, or at a site the mesh does not hold (the point
    contract: never moved) is left out and said; a site is held once, at its first reading."""
    from trid3nt.inputs.temporal import TemporalUnitsError, convert_units
    from trid3nt.inputs.vertical_datum import names_frame
    from trid3nt.workflows.runtime.primitives import OutputEmpty, series
    from trid3nt.workflows.telemac.modules import wrapper_for
    from trid3nt.workflows.telemac.modules.outputs import Solved, _point, read_series

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
    from trid3nt.workflows.telemac.authoring.serializer import restate

    names = [f"OBS_{rank}" for rank, _ in enumerate(targets, start=1)]
    restate(directory, the_deck.steering, the_deck.module, {
        "LIST OF POINTS": [int(node) for _target, node in targets], "NAMES OF POINTS": names,
        "VARIABLES TO BE PRINTED": _PRINTED, "FORMATTED RESULTS FILE": OBS_OUTPUT})
    instruction = f"{OBS_OUTPUT}.ins"
    reads = "\n".join(f"~{name} ~ ~:~ !{target.name}!" for name, (target, _node) in zip(names, targets))
    (directory / instruction).write_text(f"pif ~\n~{_HEADING}~\n{reads}\n")
    return instruction, OBS_OUTPUT


def _nodes(directory: Path, the_deck: Deck) -> tuple[Any, Any]:
    """The staged geometry's node coordinates and bed, in the numbering its boundary file names."""
    xy, _cells, bed, _geometry = _mesh(directory, the_deck)
    return xy, bed


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


def _mean_friction(faces: Sequence[int], laid: Any) -> float:
    """The mean friction coefficient over the outflow boundary's nodes."""
    if not faces:
        raise PestppError("the run states derived friction and no outflow face the study can read",
                          error_code="PESTPP_DECK_UNREAD")
    return float(sum(float(laid[node]) for node in faces) / len(faces))


def _derived(directory: Path, the_deck: Deck) -> tuple[Any, int, float, int, list[float]] | None:
    """The measured channel the run derived its outflow level over -> (channel, law, discharge, which of the
    PRESCRIBED ELEVATIONS it is, those elevations); ``None`` where the stated level is not the derivation (a gauge's)."""
    from trid3nt.workflows.telemac.authoring.opening import _measured_channel
    from trid3nt.workflows.telemac.helpers.uniform_flow import normal_depth_stage

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
    if _friction_data(the_deck):
        coefficient = _mean_friction(faces["outflow"], _friction_base_nodes(directory, the_deck)[1])
    else:
        coefficient = _mean_friction(faces["outflow"], _scalar_like(faces["outflow"], the_deck))
    law = int(stated["LAW OF BOTTOM FRICTION"][0])
    stage = normal_depth_stage(channel, law=law, coefficient=coefficient, discharge_q=flows[0])["stage_m"]
    which = [k for k, value in enumerate(elevations) if abs(value - stage) <= _SAME_LEVEL_M]
    return (channel, law, flows[0], which[0], elevations) if len(which) == 1 else None


def _scalar_like(nodes: Sequence[int], the_deck: Deck) -> list[float]:
    """The stated scalar friction at every node, for a mean over a face (geometry friction is uniform until laid)."""
    coefficient = _stated_scalar(the_deck, "FRICTION COEFFICIENT")
    if coefficient is None or coefficient == 0.0:
        raise PestppError("the run states no non-zero FRICTION COEFFICIENT, so no per-node friction has a base to "
                          "scale", error_code="PESTPP_PARAMETER_UNPLACED")
    return [coefficient] * (max(nodes) + 1 if nodes else 1)


def outflow_levels(directory: Path, *, the_deck: Deck, parameters: Sequence[Parameter]) -> dict[str, Any] | None:
    """The outflow level a trial holds, derived at each coefficient across the tuned range, written as LEVELS beside the
    steering file -> what was written; ``None`` (nothing written) where the run's outflow level is not the derived one.

    A level derived at one friction never bounds a trial at another: the trial reads its own off this table."""
    import numpy as np

    from trid3nt.workflows.telemac.helpers.uniform_flow import normal_depth_stage

    friction = [p for p in parameters if (p.original_name or p.name).upper().replace(" ", "_") == _FRICTION]
    found = _derived(directory, the_deck) if friction else None
    if found is None:
        return None
    channel, law, discharge, which, elevations = found
    spatial = any(p.scale == 1.0 and p.original_name for p in friction)
    if spatial:
        if _friction_data(the_deck):
            base = _mean_friction(_faces(directory, the_deck)["outflow"],
                                  _friction_base_nodes(directory, the_deck)[1])
        else:
            base = _mean_friction(_faces(directory, the_deck)["outflow"],
                                  _scalar_like(_faces(directory, the_deck)["outflow"], the_deck))
        low, high = sorted((base * min(p.lower for p in friction),
                            base * max(p.upper for p in friction)))
    else:
        low, high = sorted((min(p.lower * p.scale + p.offset for p in friction),
                            max(p.upper * p.scale + p.offset for p in friction)))
    coefficients = [low + (high - low) * k / (_LEVEL_POINTS - 1) for k in range(_LEVEL_POINTS)]
    stages = [round(normal_depth_stage(channel, law=law, coefficient=c, discharge_q=discharge)["stage_m"], 4)
              for c in coefficients]
    faces = _faces(directory, the_deck)["outflow"]
    table = {"module": the_deck.module, "steering": the_deck.steering, "boundary": which, "elevations": elevations,
             "coefficients": [round(c, 6) for c in coefficients], "stages": stages}
    if spatial:
        table["faces"] = [int(node) for node in faces]
    (directory / LEVELS).write_text(json.dumps(table, indent=1))
    return table


def outflow_offset(directory: Path, *, the_deck: Deck) -> Parameter:
    """The measured outflow level's offset as a parameter, prior mean 0 and sd the level's cited accuracy, bounds 2 sd
    either side as a datum's; and the files the trial reads it through: the offset PEST writes and LEVELS, the level
    the trial adds it to (its steering entry, its window's column and a flat opening stated at it)."""
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

    from trid3nt.workflows.solver.staging import new_rundir, stage_run
    from trid3nt.workflows.telemac.authoring.staged_check import check_staged_run

    run_id = new_ulid()
    rundir = new_rundir(run_id, stem="telemac")
    _, values = await asyncio.to_thread(realize, rundir, study_dir, the_deck, parameters, templates, best)
    case = finished.case
    results = list(case.get("results") or [])
    facts = dict(case.get("server_facts") or {})
    staged = await stage_run(
        rundir, run_id, module=str(case["module"]), steering=the_deck.steering, results=results,
        outputs=list(finished.outputs), mesh_inputs=[], prefix="telemac", sheet={}, server_facts=facts,
        result_basename=the_deck.result, check=check_staged_run,
        user_fortran=list(case.get("user_fortran") or ()), coupling=case.get("coupling"),
        continue_from=case.get("continue_from"), cores=int(case.get("cores") or 1))
    solved = await solve_resolved(staged, finished, frame=frame, the_deck=the_deck)
    return {**solved, "study_values": values}


def realize(directory: Path, study_dir: Path, the_deck: Deck, parameters: list, templates: list,
            best: Mapping[str, float]) -> tuple[None, dict[str, Any]]:
    """Write the fitted steering, list values and fields in the model's physical units."""
    from trid3nt.workflows.telemac.authoring.serializer import restate
    from trid3nt.workflows.telemac.modules import wrapper_for

    shutil.copytree(study_dir.parent / "deck", directory, dirs_exist_ok=True)
    spec = _load_spec(study_dir)
    values: dict[str, Any] = {}
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    spec_names = {row["name"] for row in spec.get("keywords", [])}
    spec_names |= {row["name"] for entry in spec.get("fields", []) for row in entry.get("pieces", [])}
    if spec.get("friction"):
        spec_names |= {row["name"] for row in spec["friction"].get("pieces", [])}
        if spec["friction"].get("whole"):
            spec_names.add(spec["friction"]["whole"])
        if spec["friction"].get("pilot"):
            spec_names.add(spec["friction"]["pilot"]["name"] + "_pp0000")
    for parameter in parameters:
        if parameter.name in spec_names or parameter.site is not None:
            continue
        body = wrapper_for(parameter.body or the_deck.module)
        slot = body.MODULE_INPUT.get(parameter.original_name or "")
        if slot is None:
            continue
        value = best[parameter.name] * parameter.scale + parameter.offset
        if slot.is_list and parameter.list_len:
            value = [value] * int(parameter.list_len)
        grouped.setdefault((parameter.file or _target_file(the_deck, body), body.MODULE), {})[slot.keyword] = value
        values[slot.keyword] = value
    apply_layings(directory, spec, best, study_dir)
    for entry in spec.get("keywords", []):
        values[entry["keyword"]] = [float(value) * float(best[entry["name"]]) for value in entry["base"]]
    friction = spec.get("friction")
    coefficient = None
    if friction and _derived(directory, the_deck) is not None:
        coefficient = _mean_friction(_faces(directory, the_deck)["outflow"],
                                     _apply_friction_nodes(directory, friction, best, study_dir))
    elif parameters:
        direct = next((p for p in parameters if (p.original_name or "").upper().replace(" ", "_") == _FRICTION), None)
        if direct is not None:
            coefficient = best[direct.name] * direct.scale + direct.offset
    if coefficient is not None:
        levels = _level_at(directory, the_deck, float(coefficient))
        grouped.setdefault((the_deck.steering, the_deck.module), {}).update(levels)
        values.update(levels)
    offset = next((best[p.name] * p.scale + p.offset for p in parameters if (p.original_name or p.name) == _OUTFLOW), None)
    if offset is not None:
        moved = _offset_level(directory, the_deck, offset)
        grouped.setdefault((the_deck.steering, the_deck.module), {}).update(moved)
        values.update(moved)
    for (steering, module), held in grouped.items():
        restate(directory, steering, module, held)
    return None, {key.replace(" ", "_"): value for key, value in values.items()}


def _apply_friction_nodes(directory: Path, info: Mapping[str, Any], values: Mapping[str, float],
                          source_dir: Path | None = None) -> Any:
    """The per-node friction the friction entry lays at ``values``, without writing the files."""
    import numpy as np

    multiplier = _piece_multipliers(directory, info, values, source_dir)
    return np.asarray(info["base_nodes"], dtype=float) * multiplier


def _level_at(directory: Path, the_deck: Deck, coefficient: float | None) -> dict[str, Any]:
    """The outflow level and the flat opening at ``coefficient``, re-derived over the run's own channel; ``{}`` where
    the run's level is not the derived one or the coefficient is not tuned."""
    from trid3nt.workflows.telemac.helpers.uniform_flow import normal_depth_stage

    found = _derived(directory, the_deck) if coefficient is not None else None
    if found is None:
        return {}
    channel, law, discharge, which, elevations = found
    stage = round(normal_depth_stage(channel, law=law, coefficient=float(coefficient),
                                     discharge_q=discharge)["stage_m"], 4)
    elevations[which] = stage
    return {"PRESCRIBED ELEVATIONS": elevations, "INITIAL ELEVATION": stage}


async def solve_resolved(staged: Mapping[str, Any], finished: Any, *, frame: str, the_deck: Deck | None = None) -> dict:
    from trid3nt.workflows.solver.stop import SolveStopped
    from trid3nt.workflows.telemac import engine

    if the_deck is None:
        the_deck = deck(Path(staged["rundir"]), finished)
    case = staged["case"]
    facts = dict(case.get("server_facts") or {})
    def shaped(handle):
        return {**handle, **facts, "result_basename": the_deck.result, "module": the_deck.module,
                "reference_time": (finished.record.get("outputs") or [{}])[0].get("reference_time")
                or _moment(finished), "display_basename": None, "staged": staged,
                "module_output": _module_output(the_deck), "tracer_names": (the_deck.stated.get("NAMES OF TRACERS")
                                                                           or None)}

    try:
        return shaped(await engine.solve_case(run=staged, cores=int(case.get("cores") or 1)))
    except SolveStopped as stop:
        stop.solved = shaped(stop.solved)
        raise


def _moment(finished: Any) -> str | None:
    value = ((finished.record.get("inputs") or {}).get("event_time") or {}).get("value")
    return str(value) if value not in (None, "", "none", "now") else None


def _module_output(the_deck: Deck) -> list[dict[str, Any]]:
    """The module's own rows for what the deck writes, as a solved TELEMAC run's handle carries them."""
    from trid3nt.workflows.telemac.modules import wrapper_for

    body = wrapper_for(the_deck.module)
    return [{"token": token, "module": the_deck.module, "name": row.name, "unit": row.unit, "style": row.style,
             "varies": row.varies, "has_edge": bool(row.has_edge), "injected": bool(row.injected)}
            for token, row in body.table({}).items() if token not in body.LISTING and token != body.TRACER]


async def published(solved: Mapping[str, Any], *, partial: str | None = None) -> Any:
    """The calibrated run published as TELEMAC publishes a run: every variable its result carries."""
    from trid3nt.workflows.telemac.workflow import publish_outputs

    return await publish_outputs(run=solved, outputs=[], captions={}, params={}, partial=partial)
