"""A study over a finished MODFLOW run's deck: its arrays multiplied through their own control records, its heads read at wells.

mf6 multiplies every value an array's control record reads by that record's FACTOR, and a CONSTANT record is
its one value, so a multiplier parameter is written into the record through a PEST template - the FACTOR
itself (PEST's scale the factor the run stated) or the CONSTANT (its scale the run's constant) - and the
array's values are never rewritten. A head is read at each well's cell through an OBS file the study adds.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..authoring.control import Observed, Parameter
from ..errors import PestppError

__all__ = ["Deck", "OBS_FILE", "OBS_OUTPUT", "copy_deck", "deck", "deck_sheet", "fetch_deck", "head_targets",
           "interface", "multiplier", "observe_heads", "substitute"]

#: The OBS file the study adds to the model, and the CSV mf6 writes the heads it reads into.
OBS_FILE = "study_heads.obs"
OBS_OUTPUT = "study_heads.csv"

#: Every field a template writes a multiplier into is this wide, delimiters included: room for PEST's
#: own exponent form at full precision.
_FIELD = 24

_CONTROL = re.compile(r"^(\s*)(CONSTANT|INTERNAL|OPEN/CLOSE)\b(.*)$", re.I)
_FACTOR = re.compile(r"(\bFACTOR\s+)(\S+)", re.I)


@dataclass(frozen=True)
class Deck:
    """What a staged deck states that a study reads: the flow model's name and package files, its grid's
    cells per layer and layers, the top of each cell, its length unit, and its time steps."""

    model: str
    name_file: str
    packages: Mapping[str, str]
    ncpl: int
    nlay: int
    top: tuple[float, ...]
    length_units: str
    steps: int


def fetch_deck(staged: Iterable[Mapping[str, str]], dest: Path) -> Path:
    """Every file the finished run staged, downloaded to the name it was staged under -> ``dest``."""
    from trid3nt_server.store import objects as storage

    dest.mkdir(parents=True, exist_ok=True)
    s3 = storage.client()
    for row in staged:
        bucket, _, key = str(row["gs_uri"])[len("s3://"):].partition("/")
        target = dest / str(row["dest"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    return dest


def _block(text: str, name: str) -> list[str]:
    found = re.search(rf"^\s*BEGIN\s+{name}\b[^\n]*\n(.*?)^\s*END\s+{name}\b", text, re.I | re.M | re.S)
    return [] if found is None else [line.split("#", 1)[0].strip() for line in found.group(1).splitlines()
                                     if line.split("#", 1)[0].strip()]


def deck(directory: Path) -> Deck:
    """The deck's flow model, read off the simulation's name file and the model's own; one flow model, or a refusal."""
    import flopy

    flows = [line.split() for line in _block((directory / "mfsim.nam").read_text(), "models")
             if line.split()[0].upper() == "GWF6"]
    if len(flows) != 1:
        raise PestppError(f"the run's simulation holds {len(flows)} flow models, and a study reads one",
                          error_code="PESTPP_DECK_UNREAD")
    name_file, model = flows[0][1], flows[0][2]
    packages: dict[str, str] = {}
    for line in _block((directory / name_file).read_text(), "packages"):
        kind, file = line.split()[:2]
        packages.setdefault(kind.upper().removesuffix("6"), file)
    sim = flopy.mf6.MFSimulation.load(sim_ws=str(directory), verbosity_level=0,
                                      load_only=["disv", "tdis"])
    flow = sim.get_model(model)
    dis = flow.get_package("disv")
    if dis is None:
        raise PestppError("the run's flow model is not on a DISV grid, which is the grid a study places wells on",
                          error_code="PESTPP_DECK_UNREAD")
    steps = sum(int(row[1]) for row in sim.tdis.perioddata.array)
    return Deck(model=model, name_file=name_file, packages=packages, ncpl=int(dis.ncpl.array),
                nlay=int(dis.nlay.array), top=tuple(float(v) for v in dis.top.array),
                length_units=str(dis.length_units.array or "unknown").lower(), steps=steps)


def multiplier(directory: Path, identifier: str, *, lower: float, upper: float, the_deck: Deck
               ) -> tuple[Parameter, tuple[str, str]]:
    """One multiplier parameter over the array ``identifier`` names -> the parameter and its (template, file).

    The parameter starts at 1, the run's own array; every control record of the array takes the same
    multiplier, and records stating different factors or constants refuse, one parameter writing one scale.
    A second array of the same file is written into the template the first one left."""
    prefix, _, variable = identifier.upper().partition("_")
    file = the_deck.packages.get(prefix)
    if file is None or not variable:
        raise PestppError(f"{identifier} names no package of the run's flow model; it states "
                          f"{', '.join(sorted(the_deck.packages))}", error_code="PESTPP_PARAMETER_UNPLACED")
    template = f"{file}.tpl"
    written = directory / template
    lines = (written.read_text().split("\n", 1)[1] if written.is_file()
             else (directory / file).read_text()).splitlines()
    starts = [i for i, line in enumerate(lines)
              if re.match(rf"^\s*{re.escape(variable)}(\s+LAYERED)?\s*$", line, re.I)]
    if len(starts) != 1:
        raise PestppError(f"{identifier} is stated {len(starts)} times as an array in {file}; a multiplier is "
                          "written into one array's control records", error_code="PESTPP_PARAMETER_UNPLACED")
    name = identifier.lower()
    field = "~" + name.ljust(_FIELD - 2) + "~"
    scales, at = set(), starts[0] + 1
    while at < len(lines):
        record = _CONTROL.match(lines[at])
        if record is None:
            if re.match(r"^\s*[-+0-9.]", lines[at]):
                at += 1
                continue
            break
        kind = record.group(2).upper()
        if kind == "CONSTANT":
            scales.add(float(record.group(3).split()[0]))
            lines[at] = f"{record.group(1)}CONSTANT  {field}"
        elif kind == "INTERNAL":
            stated = _FACTOR.search(record.group(3))
            scales.add(float(stated.group(2)) if stated else 1.0)
            lines[at] = f"{record.group(1)}INTERNAL  FACTOR  {field}"
        else:
            raise PestppError(f"{identifier} is read from a file of its own (OPEN/CLOSE), which this study does "
                              "not write into", error_code="PESTPP_PARAMETER_UNPLACED")
        at += 1
    if len(scales) != 1:
        raise PestppError(f"{identifier} states {len(scales)} different factors or constants across its "
                          "records; one multiplier writes one scale", error_code="PESTPP_PARAMETER_UNPLACED")
    written.write_text("ptf ~\n" + "\n".join(lines) + "\n")
    return (Parameter(name=name, value=1.0, lower=float(lower), upper=float(upper), group=name,
                      scale=scales.pop()), (template, file))


def head_targets(wells: Sequence[Any], *, the_deck: Deck, mesh: Mapping[str, Any], utm_epsg: int,
                 frame: str) -> tuple[list[tuple[Observed, int]], list[str]]:
    """Each well's reading as a head target at its cell -> ``[(observation, cell)]`` and what was left out.

    A depth below the land surface is a head of the cell's top less the depth; an elevation is a head
    only on the run's own frame. A reading in no length unit, of another quantity, on another frame, or at
    a site no cell holds is left out and said, and so is a site's second reading: a site is held once, at
    the first of its readings that is a head. The deck's one layer is the layer each reading is of."""
    from trid3nt_server.inputs.temporal import TemporalUnitsError, convert_units
    from trid3nt_server.inputs.vertical_datum import names_frame
    from trid3nt_server.workflows.modflow.authoring.accepted_mesh import mesh_nodes
    from trid3nt_server.workflows.modflow.authoring.griddata import METRES
    from trid3nt_server.workflows.modflow.authoring.stress import at_cell

    if the_deck.length_units not in METRES:
        raise PestppError(f"the run's length unit is {the_deck.length_units!r}, so no reading can be read in it",
                          error_code="PESTPP_DECK_UNREAD")
    if the_deck.nlay != 1:
        raise PestppError(f"the run has {the_deck.nlay} layers, and which one each well screens is not stated",
                          error_code="PESTPP_OBSERVATION_UNPLACED")
    points, cells, _bed, _ = mesh_nodes(mesh)
    out: list[tuple[Observed, int]] = []
    left: list[str] = []
    held: set[str] = set()
    for rank, well in enumerate(wells, start=1):
        site = well.site_id or well.site_name or f"site {rank}"
        if site in held:
            left.append(f"{site}: it is held once, at its first reading that is a head, and this is another")
            continue
        try:
            metres = convert_units(float(well.value), str(well.units or ""), "m")
        except TemporalUnitsError as exc:
            left.append(f"{site}: its reading is in {well.units!r}, no length ({exc})")
            continue
        value = metres / METRES[the_deck.length_units]
        try:
            cell = at_cell({"type": "Point", "coordinates": [well.lon, well.lat]}, role=f"the well {site}",
                           points=points, cells=cells, utm_epsg=utm_epsg)
        except Exception as exc:  # noqa: BLE001 - a site the grid does not hold is said, never moved
            left.append(f"{site}: {exc}")
            continue
        if well.quantity == "water_table_depth":
            head = the_deck.top[cell] - value
        elif well.quantity == "groundwater_head" and well.datum and names_frame(well.datum, frame):
            head = value
        else:
            left.append(f"{site}: it reports {well.quantity or 'no quantity'} on {well.datum or 'no datum'}, and "
                        f"a head is read from a depth below the land surface or an elevation on {frame}")
            continue
        held.add(site)
        out.append((Observed(name=f"head_{len(out) + 1}", value=head, weight=1.0, group="heads"), cell))
    return out, left


def observe_heads(directory: Path, targets: Sequence[tuple[Observed, int]], *, the_deck: Deck) -> tuple[str, str]:
    """The OBS file reading each target's head, named in the model's name file, and the instruction file that
    reads its one saved row -> ``(instruction file, output)``. A deck already holding a model OBS file refuses."""
    if "OBS" in the_deck.packages:
        raise PestppError("the run's flow model already states its own OBS file, and a model holds one",
                          error_code="PESTPP_OBSERVATION_UNPLACED")
    if the_deck.steps != 1:
        raise PestppError(f"the run saves {the_deck.steps} time steps, and which one each reading is of is not "
                          "stated", error_code="PESTPP_OBSERVATION_UNPLACED")
    rows = "\n".join(f"  {target.name} HEAD 1 {cell + 1}" for target, cell in targets)
    (directory / OBS_FILE).write_text(
        f"BEGIN continuous FILEOUT {OBS_OUTPUT}\n{rows}\nEND continuous\n")
    name_file = directory / the_deck.name_file
    text = name_file.read_text()
    text = re.sub(r"(^\s*END\s+packages\b)", f"  OBS6  {OBS_FILE}  study_heads\n\\1", text, count=1,
                  flags=re.I | re.M)
    name_file.write_text(text)
    instruction = f"{OBS_OUTPUT}.ins"
    reads = " ".join(f"~,~ !{target.name}!" for target, _cell in targets)
    (directory / instruction).write_text(f"pif ~\nl2 {reads}\n")
    return instruction, OBS_OUTPUT


def substitute(directory: Path, templates: Sequence[tuple[str, str]],
               parameters: Sequence[Parameter], values: Mapping[str, float]) -> None:
    """Each template written to its model file with ``values`` through each parameter's scale and offset,
    as PEST writes it; a parameter without a value refuses."""
    held = {p.name: p for p in parameters}
    for template, model_file in templates:
        text = (directory / template).read_text().split("\n", 1)[1]

        def written(match: re.Match) -> str:
            name = match.group(1).strip().lower()
            if name not in values:
                raise PestppError(f"{name} has no value to write", error_code="PESTPP_PARAMETER_UNPLACED")
            return f"{values[name] * held[name].scale + held[name].offset:.12G}"

        (directory / model_file).write_text(re.sub(r"~([^~]+)~", written, text))


def deck_sheet(directory: Path, the_deck: Deck) -> Any:
    """The flow model's sheet as the deck states it: the grid, clock and output control its outputs are
    read by, and the arrays mf6 requires, each read off the files the run solved."""
    import flopy

    from trid3nt_server.workflows.modflow.modules import GWF
    from trid3nt_server.workflows.runtime.sheet import fill

    sim = flopy.mf6.MFSimulation.load(sim_ws=str(directory), verbosity_level=0,
                                      load_only=["disv", "tdis", "npf", "ic", "oc"])
    flow = sim.get_model(the_deck.model)
    dis, npf, ic, oc = (flow.get_package(kind) for kind in ("disv", "npf", "ic", "oc"))

    def layers(array: Any) -> list[list[float]]:
        return [[float(v) for v in row] for row in array.array.reshape(the_deck.nlay, the_deck.ncpl)]

    stated: dict[str, Any] = {
        "DISV_NLAY": the_deck.nlay, "DISV_BOTM": layers(dis.botm), "DISV_LENGTH_UNITS": the_deck.length_units,
        "TDIS_NPER": int(sim.tdis.nper.array), "TDIS_TIME_UNITS": str(sim.tdis.time_units.array).lower(),
        "TDIS_PERIODDATA": [[float(r[0]), int(r[1]), float(r[2])] for r in sim.tdis.perioddata.array],
        "NPF_K": layers(npf.k), "NPF_ICELLTYPE": [[int(v) for v in row] for row in layers(npf.icelltype)],
        "IC_STRT": layers(ic.strt)}
    if npf.save_flows.array:
        stated["NPF_SAVE_FLOWS"] = True
    if oc is not None:
        for record, identifier in ((oc.head_filerecord, "OC_HEAD_FILERECORD"),
                                   (oc.budget_filerecord, "OC_BUDGET_FILERECORD")):
            if record.array is not None and len(record.array):
                stated[identifier] = str(record.array[0][0])
        saved = oc.saverecord.get_data(0)
        if saved is not None and len(saved):
            stated["OC_SAVERECORD"] = [[str(row[0]).upper(), str(row[1]).upper()] for row in saved]
    return fill(GWF, **stated)


def interface(source: Path, dest: Path, chosen: Sequence[tuple[str, float, float]], *, the_deck: Deck,
              targets: Sequence[tuple[Observed, int]]) -> tuple[list[Parameter], list[tuple[str, str]], list[tuple[str, str]]]:
    """A study directory: the deck at ``source`` copied to ``dest`` with a multiplier template over each chosen
    ``(identifier, lower, upper)`` and the wells' heads observed -> ``(parameters, templates, instructions)``."""
    shutil.copytree(source, dest)
    parameters, templates = [], []
    for identifier, lower, upper in chosen:
        parameter, pair = multiplier(dest, identifier, lower=lower, upper=upper, the_deck=the_deck)
        parameters.append(parameter)
        if pair not in templates:
            templates.append(pair)
    return parameters, templates, [observe_heads(dest, targets, the_deck=the_deck)]


def model_files(directory: Path, study_files: Iterable[str]) -> list[Path]:
    """Every file of the directory a MODFLOW solve of the deck reads or writes beside: all but the study's own."""
    own = set(study_files)
    return [path for path in directory.iterdir() if path.is_file() and path.name not in own
            and not path.name.startswith("pestpp")]


def copy_deck(source: Path, dest: Path, study_files: Iterable[str]) -> None:
    """The deck as a MODFLOW run stages it: every model file of ``source`` copied into ``dest``."""
    dest.mkdir(parents=True, exist_ok=True)
    for path in model_files(source, study_files):
        shutil.copy2(path, dest / path.name)
