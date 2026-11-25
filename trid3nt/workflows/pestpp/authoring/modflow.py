"""The study's MODFLOW piece: a finished run's arrays multiplied through their own control records or by pilot points,
its heads read at wells.

mf6 multiplies every value an array's control record reads by that record's FACTOR, and a CONSTANT record is
its one value, so a multiplier parameter is written into the record through a PEST template - the FACTOR
itself (PEST's scale the factor the run stated) or the CONSTANT (its scale the run's constant). A pilot-point array
is read from a file of its own that the trial lays before mf6 runs: the run's values times the pilot points' kriged
multiplier. A head is read at each well's cell through an OBS file the study adds.
"""

from __future__ import annotations

import json
import math
import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from ..authoring.control import Observed, Parameter, substitute, unheld_note
from ..errors import PestppError
from . import eligibility

__all__ = ["Deck", "OBSERVED", "OBS_FILE", "OBS_OUTPUT", "OWN", "PILOT_POINTS", "PLACE", "ROW", "SITE", "TRIAL", "calibrated",
           "command", "copy_deck", "deck", "deck_sheet", "fields", "head_targets", "interface", "lay", "multiplier",
           "observe_heads", "pilot_points", "published", "substitute", "supplied", "targets",
           "zoned"]

#: The study's row the targets are read off: the wells.
ROW = "observe"

#: The option the study's instruction files are written for: the heads CSV's own separator.
OWN = MappingProxyType({"additional_ins_delimiters": ","})

#: What a target is, where it stands, and what it is read at: the words a study's notes and charts say it in.
OBSERVED, SITE, PLACE = "head", "well", "cell"

#: The OBS file the study adds to the model, and the CSV mf6 writes the heads it reads into.
OBS_FILE = "study_heads.obs"
OBS_OUTPUT = "study_heads.csv"

#: What a trial runs in the image: it lays each pilot-point array off PILOT_POINTS, then mf6.
TRIAL = "/opt/trid3nt/workers/pestpp/modflow_trial.py"

#: The arrays a trial lays, each from its pilot points' values and kriging factors, beside the deck.
PILOT_POINTS = "study_pilot_points.json"

#: pyEMU 1.7.0 PstFrom's pilot-point spacing on a grid when none is stated: every 10th cell; its variogram's range is
#: that spacing (a = pp_space x the cell size), an exponential structure of contribution 1.
_EVERY = 10
_PYEMU = ("pyEMU 1.7.0 PstFrom default: a pilot point every 10th cell, an exponential variogram (contribution 1) whose "
          "range is that spacing; here a cell is the median edge of the run's grid")

#: Every field a template writes a multiplier into is this wide, delimiters included: room for PEST's
#: own exponent form at full precision.
_FIELD = 24

#: mf6's own PACKAGEDATA columns after the reach number and its cell, in the order its input definition states
#: them: a list package whose every row states one value of a column is tuned by that value's multiplier.
_LIST_COLUMNS = {"SFR": ("rlen", "rwid", "rgrd", "rtp", "rbth", "rhk", "man")}

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
    #: The median edge of the grid's cells, the cell a pilot point's default spacing is counted in: pyEMU counts its
    #: ten cells in a structured cell's side, and a DISV cell's side is its edge.
    cell_size: float | None = None
    #: Every package instance the run's name files state, by the name the name file gives it -> (body, kind, file):
    #: a repeated package of one kind is one instance per name.
    instances: Mapping[str, tuple[str, str, str]] = MappingProxyType({})
    #: The words a CELLID takes in a list package's row: a DISV cell is its layer and its cell.
    cell_tokens: int = 2


def _block(text: str, name: str) -> list[str]:
    found = re.search(rf"^\s*BEGIN\s+{name}\b[^\n]*\n(.*?)^\s*END\s+{name}\b", text, re.I | re.M | re.S)
    return [] if found is None else [line.split("#", 1)[0].strip() for line in found.group(1).splitlines()
                                     if line.split("#", 1)[0].strip()]


def command(the_deck: Deck) -> str:
    """What a trial runs: each pilot-point array laid into its own file (none where the study has none), then mf6;
    every multiplier is already written into its own files. PEST++ starts the command without a shell."""
    return f"python {TRIAL}"


def _instances(directory: Path) -> dict[str, tuple[str, str, str]]:
    """Every package instance of every model the simulation's name file lists, by its own name -> (body, kind, file)."""
    found: dict[str, tuple[str, str, str]] = {}
    for line in _block((directory / "mfsim.nam").read_text(), "models"):
        words = line.split()
        body, name_file = words[0].upper().removesuffix("6").lower(), words[1]
        for package in _block((directory / name_file).read_text(), "packages"):
            parts = package.split()
            kind, file = parts[0].upper().removesuffix("6"), parts[1]
            instance = parts[2] if len(parts) > 2 else kind.lower()
            found[instance] = (body, kind, file)
    return found


def deck(directory: Path, finished: Any = None) -> Deck:
    """The deck's flow model, read off the simulation's name file and the model's own; one flow model, or a refusal."""
    import flopy

    flows = [line.split() for line in _block((directory / "mfsim.nam").read_text(), "models")
             if line.split()[0].upper() == "GWF6"]
    if len(flows) != 1:
        raise PestppError(f"the run's simulation holds {len(flows)} flow models, and a study reads one",
                          error_code="PESTPP_DECK_UNREAD")
    name_file, model = flows[0][1], flows[0][2]
    instances = _instances(directory)
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
                length_units=str(dis.length_units.array or "unknown").lower(), steps=steps,
                cell_size=_median_edge(dis), instances=MappingProxyType(instances))


def _median_edge(dis: Any) -> float:
    """The median length of the DISV grid's edges, each edge counted once."""
    import numpy as np

    xy = np.array([(float(row[1]), float(row[2])) for row in dis.vertices.array])
    edges = set()
    for row in dis.cell2d.array:
        ids = [int(i) for i in list(row)[4:4 + int(row[3])]]
        edges.update((min(a, b), max(a, b)) for a, b in zip(ids, ids[1:] + ids[:1]) if a != b)
    pairs = np.array(sorted(edges))
    return float(np.median(np.hypot(*(xy[pairs[:, 0]] - xy[pairs[:, 1]]).T)))


def supplied(the_deck: Deck) -> tuple[list[tuple[str, float, str]], list[str]]:
    """No supplied input of a MODFLOW run carries an accuracy on its record, so none is a parameter -> ``([], the
    study's note saying so)``."""
    return [], [unheld_note("its bed, boundary levels and stated values")]


def _param_name(identifier: str) -> str:
    """The PEST name an identifier writes through: a layer selector's ``@`` becomes ``_l``, so a PEST table names it."""
    return identifier.lower().replace("@", "_l")


def _field(name: str) -> str:
    """The template field a parameter's name writes through, this wide so PEST's own exponent form fits."""
    return "~" + name.ljust(_FIELD - 2) + "~"


def _template(directory: Path, file: str) -> list[str]:
    """The package file's lines, or the template a prior parameter left, its ``ptf ~`` header dropped."""
    written = directory / f"{file}.tpl"
    return ((written.read_text().split("\n", 1)[1] if written.is_file()
             else (directory / file).read_text()).splitlines())


def _write_template(directory: Path, file: str, lines: list[str]) -> tuple[str, str]:
    (directory / f"{file}.tpl").write_text("ptf ~\n" + "\n".join(lines) + "\n")
    return f"{file}.tpl", file


def _tokens(text: str) -> list[str]:
    """A line's tokens, a template field one token with the spaces inside its delimiters."""
    return re.findall(r"~[^~]*~|[^\s~]+", text.split("#", 1)[0])


def _factor(tokens: Sequence[str]) -> float:
    """The FACTOR a control record states, 1.0 where it states none."""
    words = [token.upper() for token in tokens]
    if "FACTOR" in words:
        return float(tokens[words.index("FACTOR") + 1])
    return 1.0


def _records(lines: list[str], start: int) -> list[dict[str, Any]]:
    """The array control records after a header line, each its kind, scale, file and where its data starts."""
    records: list[dict[str, Any]] = []
    at = start + 1
    while at < len(lines):
        record = _CONTROL.match(lines[at])
        if record is None:
            break
        kind, tokens = record.group(2).upper(), _tokens(record.group(3))
        entry = {"at": at, "kind": kind, "indent": record.group(1), "file": None,
                 "scale": float(tokens[0]) if kind == "CONSTANT" else _factor(tokens)}
        if kind == "OPEN/CLOSE":
            entry["file"] = tokens[0] if tokens else ""
        at += 1
        while (kind != "CONSTANT" and at < len(lines) and _CONTROL.match(lines[at]) is None
               and re.match(r"^\s*[-+0-9.]", lines[at])):
            at += 1
        entry["data_end"] = at
        records.append(entry)
    return records


def _set_record(lines: list[str], record: Mapping[str, Any], field: str) -> None:
    """Write ``field`` into one control record's own scale word."""
    if record["kind"] == "CONSTANT":
        lines[record["at"]] = f"{record['indent']}CONSTANT  {field}"
    elif record["kind"] == "INTERNAL":
        lines[record["at"]] = f"{record['indent']}INTERNAL  FACTOR  {field}"
    else:
        lines[record["at"]] = f"{record['indent']}OPEN/CLOSE  {record['file']}  FACTOR  {field}"


def _array_header(lines: list[str], identifier: str, variable: str) -> tuple[int, bool]:
    """The array's one header line and whether it is LAYERED (one control record per layer)."""
    starts = [i for i, line in enumerate(lines)
              if re.match(rf"^\s*{re.escape(variable)}(\s+LAYERED)?\s*$", line, re.I)]
    if len(starts) != 1:
        raise PestppError(f"{identifier} is stated {len(starts)} times as an array in its package; a multiplier is "
                          "written into one array's control records", error_code="PESTPP_PARAMETER_UNPLACED")
    return starts[0], bool(re.search(r"\bLAYERED\b", lines[starts[0]], re.I))


def _array(lines: list[str], target: eligibility.Target, field: str, the_deck: Deck) -> tuple[float, bool]:
    """The target array's control records written with ``field`` -> its one scale, and whether its value is not a
    constant (a FACTOR is not the array's values). A layer selector writes that layer's own record, expanding a
    homogeneous array into the per-layer form; a whole array writes every record and takes their one scale."""
    header, layered = _array_header(lines, target.identifier, target.variable)
    records = _records(lines, header)
    if not records:
        raise PestppError(f"{target.identifier} states no control record under its array header",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    if target.layer is None:
        scales = {record["scale"] for record in records}
        if len(scales) != 1:
            raise PestppError(f"{target.identifier} states {len(scales)} different factors or constants across its "
                              "records; one multiplier writes one scale", error_code="PESTPP_PARAMETER_UNPLACED")
        for record in records:
            _set_record(lines, record, field)
        return scales.pop(), any(record["kind"] != "CONSTANT" for record in records)
    layer = int(target.layer)
    if layered:
        if layer > len(records):
            raise PestppError(f"{target.identifier} selects layer {layer}, and its array states {len(records)} layers",
                              error_code="PESTPP_PARAMETER_UNPLACED")
        record = records[layer - 1]
        _set_record(lines, record, field)
        return record["scale"], record["kind"] != "CONSTANT"
    record = records[0]
    if int(the_deck.nlay) <= 1:
        _set_record(lines, record, field)
        return record["scale"], record["kind"] != "CONSTANT"
    if record["kind"] != "CONSTANT":
        raise PestppError(f"{target.identifier} selects layer {layer}, and its one record holds the whole "
                          f"{int(the_deck.nlay)}-layer array; a layered deck writes one record per layer",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    lines[header] = re.sub(r"\s+$", "", lines[header]) + "  LAYERED"
    expanded = [f"    CONSTANT  {field if (k + 1) == layer else repr(record['scale'])}"
                for k in range(int(the_deck.nlay))]
    lines[record["at"]:record["at"] + 1] = expanded
    return record["scale"], False


def _columns_before(lines: list[str], target: eligibility.Target) -> list[dict[str, Any]]:
    """Every row of the target's list block, with the token the target column writes and the row's own ids."""
    order = [row["keyword"] for row in eligibility.columns(target.body, target.kind, recarray=target.recarray)]
    variable_at = order.index(target.column)
    cell_at = order.index("CELLID") if "CELLID" in order else None
    rows: list[dict[str, Any]] = []
    for start, end in _spans(lines, target.block):
        for at in range(start, end):
            tokens = _tokens(lines[at])
            if not tokens or len(tokens) <= variable_at:
                continue
            column, layer, cell, width = variable_at, None, None, 1
            if cell_at is not None:
                cell_start = cell_at
                width = 1 if tokens[cell_start].upper() == "NONE" else 2
                if variable_at > cell_at:
                    column = cell_start + width + (variable_at - cell_at - 1)
                if width == 2:
                    layer, cell = int(tokens[cell_start]), int(tokens[cell_start + 1])
            if column >= len(tokens):
                continue
            if tokens[column].startswith("~") or not re.match(r"^[-+0-9.]", tokens[column]):
                raise PestppError(f"{target.identifier} already holds a parameter at row {at + 1}; one column is "
                                  "tuned once", error_code="PESTPP_PARAMETER_UNPLACED")
            rows.append({"at": at, "tokens": tokens, "at_token": column,
                         "cell_start": cell_at, "away": (variable_at - cell_at - 1) if cell_at is not None else None,
                         "fixed": variable_at if cell_at is None else None,
                         "value": float(tokens[column]), "reach": int(tokens[0]) if order[0] == "IFNO" else None,
                         "layer": layer, "cell": cell})
    return rows


def _spans(lines: list[str], name: str) -> list[tuple[int, int]]:
    """The line ranges inside every BEGIN/END block ``name`` (a period block may repeat)."""
    spans, start = [], None
    for at, line in enumerate(lines):
        if re.match(rf"^\s*BEGIN\s+{re.escape(name)}\b", line, re.I):
            start = at + 1
        elif start is not None and re.match(rf"^\s*END\s+{re.escape(name)}\b", line, re.I):
            spans.append((start, at))
            start = None
    return spans


def _column(lines: list[str], target: eligibility.Target, field: str, the_deck: Deck, *,
            uniform: bool = True) -> float:
    """The target list column written as ``field`` in every row -> the one value they state; rows stating
    different values refuse unless ``uniform`` is false (a native table restated with one scalar)."""
    rows = _columns_before(lines, target)
    if not rows:
        raise PestppError(f"{target.identifier} names no row of the {target.kind} {target.block} block",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    scales = {row["value"] for row in rows}
    if uniform and len(scales) != 1:
        raise PestppError(f"{target.identifier} states {len(scales)} different values across the package's rows; "
                          "one multiplier writes one scale", error_code="PESTPP_PARAMETER_UNPLACED")
    for row in rows:
        row["tokens"][row["at_token"]] = field
        lines[row["at"]] = "  " + "  ".join(row["tokens"])
    return scales.pop() if len(scales) == 1 else next(iter(scales))


def _column_trial(directory: Path, identifier: str, target: eligibility.Target,
                  selections: Sequence[tuple[str, Sequence[Mapping[str, Any]], str, str]], *,
                  lower: float, upper: float, the_deck: Deck, source: str) -> tuple[list[Parameter], list[tuple[str, str]]]:
    """One multiplier per selection over a list column whose rows state different values -> the parameters and the
    values files PEST writes.

    A PEST template writes one scale, so rows holding different values are scaled by the trial: it multiplies each
    selected row's own value by the parameter PEST wrote. The package file is left as the run wrote it; the spec the
    trial and the calibrated deck read carries the lines and the token, and one values file writes each parameter."""
    spec = directory / PILOT_POINTS
    held = json.loads(spec.read_text()) if spec.is_file() else {"arrays": []}
    held.setdefault("columns", [])
    parameters, pairs = [], []
    for name, rows, zone, zone_source in selections:
        stem = f"{name}.zone"
        (directory / f"{stem}.dat").write_text(f"{name} 1.0\n")
        (directory / f"{stem}.dat.tpl").write_text(
            "ptf ~\n" + f"{name} ~{name.ljust(_FIELD - 2)}~\n")
        sample = rows[0]
        held["columns"].append({"file": target.file, "name": name, "lines": [row["at"] for row in rows],
                                "fixed": sample["fixed"], "cell_start": sample["cell_start"],
                                "away": sample["away"], "width": int(the_deck.cell_tokens),
                                "values": f"{stem}.dat"})
        parameters.append(Parameter(name=name, value=1.0, lower=float(lower), upper=float(upper), group=name,
                                    scale=1.0, source=source, original_name=identifier, zone=zone,
                                    zone_source=zone_source))
        pairs.append((f"{stem}.dat.tpl", f"{stem}.dat"))
    spec.write_text(json.dumps(held, indent=1))
    return parameters, pairs


def multiplier(directory: Path, identifier: str, *, lower: float, upper: float, the_deck: Deck,
               absolute: bool = False, source: str = "") -> tuple[Parameter, tuple[str, str]]:
    """One multiplier parameter over the array or list column ``identifier`` names -> the parameter and its
    (template, file).

    Eligibility is the MODFLOW 6 dictionary's own type: a REAL array or list column is tuned; an integer flag, a
    string or a keyword refuses. The parameter starts at 1, the run's own values; every control record of an array,
    or every row of a list column, takes the same multiplier, and records or rows stating different values refuse,
    one parameter writing one scale. ``absolute`` bounds are stated in the run's own unit and divided by that scale;
    they refuse over an array whose value is not a constant and where the run's own value lies outside them. A
    second array of the same file is written into the template the first one left."""
    target = eligibility.real(eligibility.resolve(identifier, the_deck))
    lines = _template(directory, target.file)
    name = _param_name(identifier)
    field = _field(name)
    if target.is_column:
        rows = _columns_before(lines, target)
        if not rows:
            raise PestppError(f"{target.identifier} names no row of the {target.kind} {target.block} block",
                              error_code="PESTPP_PARAMETER_UNPLACED")
        if len({row["value"] for row in rows}) != 1:
            if absolute:
                raise PestppError(f"{identifier} is bounded in the run's own unit and its rows state different "
                                  "values, so one multiplier cannot carry them: state its bounds as factors",
                                  error_code="PESTPP_BOUNDS_UNSTATED")
            parameters, pairs = _column_trial(directory, identifier, target, [(name, rows, "", "")],
                                             lower=lower, upper=upper, the_deck=the_deck, source=source)
            return parameters[0], pairs[0]
        scale, internal = _column(lines, target, field, the_deck), False
    else:
        scale, internal = _array(lines, target, field, the_deck)
    if absolute and internal:
        raise PestppError(f"{identifier} is bounded in the run's own unit, and its array is read as a factor over "
                          "its own values or from a file, whose FACTOR is not its values: state its bounds as "
                          "multipliers of the run's own array", error_code="PESTPP_BOUNDS_UNSTATED")
    if absolute:
        lower, upper = lower / scale, upper / scale
        if not 0.0 < lower < 1.0 < upper:
            raise PestppError(f"{identifier} is {scale:g} in the run, outside its stated bounds "
                              f"{lower * scale:g} to {upper * scale:g}", error_code="PESTPP_BOUNDS_UNSTATED")
    pair = _write_template(directory, target.file, lines)
    return (Parameter(name=name, value=1.0, lower=float(lower), upper=float(upper), group=name,
                      scale=scale, source=source, original_name=identifier), pair)


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


def _polygon_cells(directory: Path, the_deck: Deck, polygon: Sequence[Sequence[float]], *,
                   crs: Any = None, frame_epsg: int | None = None) -> set[int]:
    """The one-based DISV cells the polygon's geometry meets -> their ids.

    A caller-stated polygon is longitude/latitude (EPSG:4326) unless it names its CRS; either way it is
    reprojected into the run's grid frame here, never handed to the caller."""
    from pyproj import Transformer
    from shapely.geometry import Polygon

    if frame_epsg is None:
        raise PestppError("a zone polygon needs the run's grid frame, which this deck does not state",
                          error_code="PESTPP_ZONE_CRS")
    ring = []
    for point in polygon:
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in point)):
            raise PestppError("a zone polygon is a list of finite [x, y] pairs", error_code="PESTPP_ZONE_INVALID")
        ring.append((float(point[0]), float(point[1])))
    source = 4326 if crs is None else _epsg(crs, "a zone polygon")
    if source != int(frame_epsg):
        transform = Transformer.from_crs(source, int(frame_epsg), always_xy=True).transform
        ring = [transform(x, y) for x, y in ring]
    zone = Polygon(ring)
    if not zone.is_valid or zone.area <= 0:
        raise PestppError("a zone polygon is a simple ring with area", error_code="PESTPP_ZONE_INVALID")
    return {k + 1 for k, cell in enumerate(_cell_polygons(directory, the_deck))
            if cell.intersection(zone).area > 0.0}


def _cell_polygons(directory: Path, the_deck: Deck) -> list[Any]:
    import flopy
    from shapely.geometry import Polygon

    sim = flopy.mf6.MFSimulation.load(sim_ws=str(directory), verbosity_level=0, load_only=["disv", "tdis"])
    grid = sim.get_model(the_deck.model).modelgrid
    return [Polygon(grid.get_cell_vertices(cell)) for cell in range(grid.ncpl)]


def _zone_rows(zone: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], directory: Path,
               the_deck: Deck, *, frame_epsg: int | None = None) -> list[Mapping[str, Any]]:
    """The rows one zone selects: its cells, reaches, wells, layers or polygon; one selector a zone."""
    keys = set(zone) - {"source", "crs"}
    if len(keys) != 1:
        raise PestppError("a zone states one of cells, reaches, wells, layers or polygon, its source and any CRS",
                          error_code="PESTPP_ZONE_INVALID")
    (kind, wanted), = ((kind, zone[kind]) for kind in keys)
    if not isinstance(wanted, (list, tuple)) or not wanted:
        raise PestppError(f"a zone's {kind} is a nonempty list", error_code="PESTPP_ZONE_INVALID")
    if kind == "polygon":
        cells = _polygon_cells(directory, the_deck, wanted, crs=zone.get("crs"), frame_epsg=frame_epsg)
        return [row for row in rows if row["cell"] in cells]
    if kind == "cells":
        return [row for row in rows if row["cell"] in {int(value) for value in wanted}]
    if kind == "layers":
        return [row for row in rows if row["layer"] in {int(value) for value in wanted}]
    if kind == "reaches":
        return [row for row in rows if row["reach"] in {int(value) for value in wanted}]
    names = {value for value in wanted}
    return [row for row in rows if row["cell"] in names]


def zoned(directory: Path, identifier: str, zones: Sequence[Mapping[str, Any]], *, lower: float, upper: float,
          the_deck: Deck, absolute: bool = False, source: str = "",
          frame_epsg: int | None = None) -> tuple[list[Parameter], list[tuple[str, str]]]:
    """One parameter per stated zone over the array or list column ``identifier`` names -> the parameters and the
    (template, file). Every cell outside every stated zone is left as the run wrote it, and a row a second zone also
    holds refuses. An array's zones are its layers; a list column's are its cells, reaches, wells, layers or a
    polygon over the grid."""
    target = eligibility.real(eligibility.resolve(identifier, the_deck))
    lines = _template(directory, target.file)
    if target.is_column:
        rows = _columns_before(lines, target)
        held: dict[int, int] = {}
        chosen: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for index, zone in enumerate(zones, start=1):
            selected = _zone_rows(zone, rows, directory, the_deck, frame_epsg=frame_epsg)
            if not selected:
                raise PestppError(f"{identifier}'s zone {index} holds no row of the {target.kind} package",
                                  error_code="PESTPP_ZONE_EMPTY")
            for row in selected:
                if row["at"] in held:
                    raise PestppError(f"{identifier}'s zones {held[row['at']]} and {index} both hold row "
                                      f"{row['at'] + 1}; state disjoint zones", error_code="PESTPP_ZONE_OVERLAP")
                held[row["at"]] = index
            chosen.append((zone, selected))
        selections = [(f"{_param_name(identifier)}_z{index}", selected, kind_of(zone),
                       str(zone.get("source") or "")) for index, (zone, selected) in enumerate(chosen, start=1)]
        if any(len({row["value"] for row in selected}) != 1 for _name, selected, _kind, _zone in selections):
            if absolute:
                raise PestppError(f"{identifier}'s zones hold rows of different values, so one multiplier cannot "
                                  "carry them in the run's own unit: state its bounds as factors",
                                  error_code="PESTPP_BOUNDS_UNSTATED")
            parameters, pairs = _column_trial(directory, identifier, target, selections, lower=lower, upper=upper,
                                              the_deck=the_deck, source=source)
            return parameters, pairs
        parameters = []
        for name, selected, kind, zone_source in selections:
            scale = {row["value"] for row in selected}.pop()
            if absolute and not 0.0 < lower / scale < 1.0 < upper / scale:
                raise PestppError(f"{identifier}'s zone {name} is {scale:g} in the run, outside its stated bounds "
                                  f"{lower:g} to {upper:g}", error_code="PESTPP_BOUNDS_UNSTATED")
            field = _field(name)
            for row in selected:
                row["tokens"][row["at_token"]] = field
                lines[row["at"]] = "  " + "  ".join(row["tokens"])
            parameters.append(Parameter(name=name, value=1.0, lower=lower / scale if absolute else float(lower),
                                        upper=upper / scale if absolute else float(upper), group=name, scale=scale,
                                        source=source, original_name=identifier,
                                        zone=kind, zone_source=zone_source))
        pair = _write_template(directory, target.file, lines)
        return parameters, [pair]
    if any(set(zone) - {"source", "crs", "layers"} for zone in zones):
        raise PestppError(f"{identifier} is an array, and an array's zones are its layers: a cell or polygon zone "
                          "over an array is laid by the trial, which this study does not write",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    layers = [int(value) for zone in zones for value in zone["layers"]]
    if len(set(layers)) != len(layers):
        raise PestppError(f"{identifier}'s zones name a layer twice", error_code="PESTPP_ZONE_OVERLAP")
    parameters = []
    for zone in zones:
        for layer in (int(value) for value in zone["layers"]):
            name = f"{_param_name(identifier)}_l{layer}"
            field = _field(name)
            scale, internal = _array(lines, replace(target, layer=layer), field, the_deck)
            if absolute and internal:
                raise PestppError(f"{identifier} layer {layer} is read as a factor over its own values or from a "
                                  "file, whose FACTOR is not its values: state its bounds as multipliers",
                                  error_code="PESTPP_BOUNDS_UNSTATED")
            parameters.append(Parameter(name=name, value=1.0, lower=lower / scale if absolute else float(lower),
                                        upper=upper / scale if absolute else float(upper), group=name, scale=scale,
                                        source=source, original_name=identifier,
                                        zone=f"layer {layer}", zone_source=str(zone.get("source") or "")))
    pair = _write_template(directory, target.file, lines)
    return parameters, [pair]


def kind_of(zone: Mapping[str, Any]) -> str:
    """The selector a zone states, for its record."""
    keys = set(zone) - {"source", "crs"}
    return next(iter(keys)) if len(keys) == 1 else ""


def patch_columns(directory: Path, values: Mapping[str, float]) -> None:
    """Restate native list-column values on a newly built deck in model units."""
    parsed = deck(directory)
    for name, scalar in values.items():
        target = eligibility.resolve(name, parsed)
        if not target.is_column:
            raise PestppError(f"{name} is an array, and only a list column is restated in place",
                              error_code="PESTPP_PARAMETER_UNPLACED")
        path = directory / target.file
        lines = path.read_text(encoding="utf-8").splitlines()
        _column(lines, target, repr(float(scalar)), parsed, uniform=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fields(spatial: Mapping[str, Any], the_deck: Deck) -> dict[str, dict[str, Any]]:
    """Each pilot-point parameter's field as stated, pyEMU's own default where not -> ``{identifier: field}``: the
    spacing between its points and its exponential variogram's range, in metres of the run's grid, each with its source.

    ``spatial`` is ``{identifier: {"spacing": S, "source": "...", "variogram": {"a": A, "source": "..."}}}``, any part
    left out. Unstated, the spacing is 10 of the grid's median edges and the range that spacing (pyEMU's rule); a range left
    out of a stated spacing is that spacing. A variogram of another type, or a member not read, refuses."""
    from math import isfinite

    from ..helpers.geostat import NEAREST, VARIOGRAMS

    structure_defaults = {"contribution": 1.0, "nugget": 0.0, "anisotropy": 1.0, "bearing": 0.0}
    out: dict[str, dict[str, Any]] = {}
    for identifier, stated in spatial.items():
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
        # pyEMU 1.7.0 PstFrom, GeoStruct and Vario2d constructor defaults.
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
            raise PestppError(f"{identifier}'s pilot points are spaced {spacing:g} with a range of {a:g}; both above 0",
                              error_code="PESTPP_PILOT_POINTS_UNSTATED")
        out[identifier] = {"spacing": spacing, "spacing_source": spacing_source, "variogram": kind,
                           "a": a, "a_source": a_source, **structure,
                           "structure_stated": sorted(variogram.keys() & structure_defaults.keys()),
                           "maxpts_interp": nearest, "remove_negative_factors": remove_negative,
                           "kriging_stated": bool({"maxpts_interp", "remove_negative_factors"} & stated.keys()),
                           "stated": bool(stated)}
    return out


def _grid(directory: Path, the_deck: Deck) -> tuple[Any, Any]:
    """The deck's cell centres (ncpl x 2) and its cells' union, read off its DISV package."""
    import flopy
    import numpy as np
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    sim = flopy.mf6.MFSimulation.load(sim_ws=str(directory), verbosity_level=0, load_only=["disv", "tdis"])
    grid = sim.get_model(the_deck.model).modelgrid
    centres = np.column_stack([np.asarray(grid.xcellcenters, dtype=float), np.asarray(grid.ycellcenters, dtype=float)])
    return centres, unary_union([Polygon(grid.get_cell_vertices(cell)) for cell in range(grid.ncpl)])


def _placed(centres: Any, cells: Any, spacing: float) -> Any:
    """pyEMU's vertex-grid placement (get_zoned_ppoints_for_vertexgrid): an even grid over the cell centres' extent,
    ceil(extent / spacing) points a side end to end, kept where the cells buffered by one spacing cover them."""
    import numpy as np
    from shapely import points

    low, high = centres.min(axis=0), centres.max(axis=0)
    xs, ys = (np.linspace(low[k], high[k], int(np.ceil((high[k] - low[k]) / spacing))) for k in (0, 1))
    xv, yv = np.meshgrid(xs, ys)
    grid = points(list(zip(xv.ravel(), yv.ravel())))
    return np.array([(p.x, p.y) for p in grid[cells.buffer(spacing).covers(grid)]])


def _record_values(directory: Path, record: Mapping[str, Any], lines: list[str], count: int) -> list[float]:
    """One array control record's ``count`` values as mf6 reads them, its FACTOR applied; an external file is read."""
    if record["kind"] == "CONSTANT":
        return [float(record["scale"])] * count
    if record["kind"] == "OPEN/CLOSE":
        text = (directory / str(record["file"])).read_text()
        return [float(record["scale"]) * float(value) for value in _tokens(text)]
    values = [float(value) for at in range(record["at"] + 1, record["data_end"])
              for value in _tokens(lines[at])]
    return [float(record["scale"]) * value for value in values]


def _layer_base(directory: Path, target: eligibility.Target, lines: list[str], records: list[Mapping[str, Any]],
                layer: int, the_deck: Deck) -> list[float]:
    """The target array's values of one layer, as mf6 reads them."""
    if len(records) > 1:
        record = records[layer - 1]
    else:
        record = records[0]
        if record["kind"] != "CONSTANT" and int(the_deck.nlay) > 1:
            raise PestppError(f"{target.identifier} selects layer {layer}, and its one record holds the whole "
                              f"{int(the_deck.nlay)}-layer array; a layered deck writes one record per layer",
                              error_code="PESTPP_PARAMETER_UNPLACED")
    values = _record_values(directory, record, lines, int(the_deck.ncpl))
    if len(values) != int(the_deck.ncpl):
        raise PestppError(f"{target.identifier} states {len(values)} values over {int(the_deck.ncpl)} cells",
                          error_code="PESTPP_DECK_UNREAD")
    return values


def _lay_record(lines: list[str], header: int, records: list[Mapping[str, Any]], layer: int, nlay: int,
                array: str) -> None:
    """Point one layer's control record at ``array`` (OPEN/CLOSE, FACTOR 1); a homogeneous constant array expands
    into the per-layer form so only the selected layer is read from the file."""
    if len(records) > 1:
        record = records[layer - 1]
        lines[record["at"]:record["data_end"]] = [f"    OPEN/CLOSE  {array}  FACTOR  1.0"]
        return
    record = records[0]
    if nlay <= 1:
        lines[record["at"]:record["data_end"]] = [f"    OPEN/CLOSE  {array}  FACTOR  1.0"]
        return
    if record["kind"] != "CONSTANT":
        raise PestppError(f"the array at line {record['at'] + 1} holds {nlay} layers in one record, which pilot "
                          "points cannot lay", error_code="PESTPP_PARAMETER_UNPLACED")
    lines[header] = re.sub(r"\s+$", "", lines[header]) + "  LAYERED"
    expanded = [f"    OPEN/CLOSE  {array}  FACTOR  1.0" if (k + 1) == layer
                else f"    CONSTANT  {record['scale']!r}" for k in range(nlay)]
    lines[record["at"]:record["data_end"]] = expanded


def pilot_points(directory: Path, identifier: str, field: Mapping[str, Any], *, lower: float, upper: float,
                 the_deck: Deck, source: str = "") -> tuple[list[Parameter], list[tuple[str, str]]]:
    """The array ``identifier`` names laid by pilot points -> each point's parameter and the (template, file) pairs
    PEST writes their values through.

    The array (the whole of it, or the one layer a selector names) moves into a file of its own (``<package
    file>.<array>.dat``), the points are placed as pyEMU places them over the grid at the field's spacing, and each
    cell's ordinary kriging factors over them are written beside it for the trial, which lays the run's values times
    10 ** (the factors over the log10 of the points). Each point is a multiplier starting at 1 inside the parameter's
    bounds, carrying its place and the variogram's range for the prior. A run of several layers lays each one."""
    import numpy as np

    from ..helpers.geostat import kriging_factors

    target = eligibility.real(eligibility.resolve(identifier, the_deck))
    if target.is_column:
        raise PestppError(f"{identifier} is a list column, which pilot points do not lay",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    nlay = int(the_deck.nlay)
    layers = [int(target.layer)] if target.layer is not None else list(range(1, nlay + 1))
    centres, cells = _grid(directory, the_deck)
    sites = _placed(centres, cells, float(field["spacing"]))
    parameters, pairs = [], []
    for layer in layers:
        lines = (directory / target.file).read_text().splitlines()
        header, _layered = _array_header(lines, target.identifier, target.variable)
        records = _records(lines, header)
        if not records:
            raise PestppError(f"{identifier} states no control record under its array header",
                              error_code="PESTPP_PARAMETER_UNPLACED")
        base = _layer_base(directory, target, lines, records, layer, the_deck)
        one = nlay == 1
        name = _param_name(identifier) if one else f"{_param_name(identifier)}_l{layer}"
        array = (f"{target.file}.{target.variable}.dat" if one
                 else f"{target.file}.{target.variable}.l{layer}.dat")
        (directory / array).write_text("\n".join(repr(v) for v in base) + "\n")
        _lay_record(lines, header, records, layer, nlay, array)
        (directory / target.file).write_text("\n".join(lines) + "\n")
        names = [f"{name}_pp{k:04d}" for k in range(len(sites))]
        try:
            index, weight = kriging_factors(centres, sites, float(field["a"]), variogram=field["variogram"],
                                            maxpts_interp=field["maxpts_interp"],
                                            remove_negative_factors=field["remove_negative_factors"],
                                            contribution=field["contribution"], nugget=field["nugget"],
                                            anisotropy=field["anisotropy"], bearing=field["bearing"])
        except np.linalg.LinAlgError as exc:
            raise PestppError(f"{identifier}'s variogram cannot interpolate the pilot points.",
                              error_code="PESTPP_KRIGING_SINGULAR") from exc
        stem = f"{name}.pp"
        np.savez(directory / f"{stem}.npz", names=np.array(names), index=index, weight=weight, base=np.array(base))
        (directory / f"{stem}.dat").write_text("".join(f"{n} 1.0\n" for n in names))
        (directory / f"{stem}.dat.tpl").write_text(
            "ptf ~\n" + "".join(f"{n} ~{n.ljust(_FIELD - 2)}~\n" for n in names))
        spec = directory / PILOT_POINTS
        held = json.loads(spec.read_text()) if spec.is_file() else {"arrays": []}
        held["arrays"].append({"array": array, "values": f"{stem}.dat", "factors": f"{stem}.npz"})
        spec.write_text(json.dumps(held, indent=1))
        parameters.extend(Parameter(name=n, value=1.0, lower=float(lower), upper=float(upper), group=name,
                                    site=(float(x), float(y)), correlation=float(field["a"]),
                                    variogram=field["variogram"], contribution=field["contribution"],
                                    nugget=field["nugget"], anisotropy=field["anisotropy"],
                                    bearing=field["bearing"], spatial_group=name, source=source,
                                    original_name=identifier) for n, (x, y) in zip(names, sites))
        pairs.append((f"{stem}.dat.tpl", f"{stem}.dat"))
    return parameters, pairs


def lay(directory: Path) -> list[str]:
    """Each pilot-point array and zoned list column of the study at ``directory`` laid off its points' and zones'
    values as PEST wrote them -> the study's own files (none where it has neither); the trial lays them the same way."""
    import numpy as np

    spec = directory / PILOT_POINTS
    if not spec.is_file():
        return []
    held_spec = json.loads(spec.read_text())
    own = [PILOT_POINTS]
    for entry in held_spec.get("arrays", []):
        held = np.load(directory / entry["factors"])
        stated = dict(line.split()[:2] for line in (directory / entry["values"]).read_text().splitlines() if line.strip())
        values = np.log10(np.array([float(stated[str(n)]) for n in held["names"]]))
        laid = held["base"] * 10.0 ** (held["weight"] * values[held["index"]]).sum(axis=1)
        (directory / entry["array"]).write_text("\n".join(repr(float(v)) for v in laid) + "\n")
        own += [entry["values"], entry["factors"]]
    for entry in held_spec.get("columns", []):
        stated = dict(line.split()[:2] for line in (directory / entry["values"]).read_text().splitlines() if line.strip())
        value = float(stated[entry["name"]])
        path = directory / entry["file"]
        lines = path.read_text().splitlines()
        for at in entry["lines"]:
            tokens = _tokens(lines[at])
            width = 1 if entry["cell_start"] is not None and tokens[entry["cell_start"]].upper() == "NONE" \
                else (entry["width"] if entry["cell_start"] is not None else 1)
            column = entry["fixed"] if entry["cell_start"] is None else entry["cell_start"] + width + entry["away"]
            tokens[column] = repr(float(tokens[column]) * value)
            lines[at] = "  " + "  ".join(tokens)
        path.write_text("\n".join(lines) + "\n")
        own.append(entry["values"])
    return own


def head_targets(wells: Sequence[Any], *, the_deck: Deck, mesh: Mapping[str, Any], utm_epsg: int,
                 frame: str) -> tuple[list[tuple[Observed, int]], list[str]]:
    """Each well's reading as a head target at its cell -> ``[(observation, cell)]`` and what was left out.

    A depth below the land surface is a head of the cell's top less the depth; an elevation is a head
    only on the run's own frame. A reading in no length unit, of another quantity, on another frame, or at
    a site no cell holds is left out and said, and so is a site's second reading: a site is held once, at
    the first of its readings that is a head. The deck's one layer is the layer each reading is of."""
    from trid3nt.inputs.temporal import TemporalUnitsError, convert_units
    from trid3nt.inputs.vertical_datum import names_frame
    from trid3nt.workflows.modflow.authoring.accepted_mesh import mesh_nodes
    from trid3nt.workflows.modflow.authoring.griddata import METRES
    from trid3nt.workflows.modflow.authoring.stress import at_cell

    from ..weights import observed

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
        out.append((observed(f"head_{len(out) + 1}", head, well, group="heads", to_run=lambda sigma, well=well: (
            convert_units(sigma, str(well.units), "m") / METRES[the_deck.length_units])), cell))
    return out, left


def targets(wells: Sequence[Any], *, the_deck: Deck, finished: Any, frame: str
            ) -> tuple[list[tuple[Observed, int]], list[str]]:
    """Each well's reading as a head target at its cell of the finished run's mesh -> ``(targets, left out)``."""
    return head_targets(wells, the_deck=the_deck, mesh=finished.mesh,
                        utm_epsg=int(finished.case["server_facts"]["utm_epsg"]), frame=frame)


def observe_csv(directory: Path, output: str, columns: Sequence[str],
                readings: Mapping[str, tuple[str, int]]) -> tuple[str, str]:
    """Write one instruction file for native OBS columns and one-based data rows."""
    from ...modflow.errors import ModflowError

    indices = {name.casefold(): column for column, name in enumerate(columns, 1)}
    by_row: dict[int, list[tuple[int, str]]] = {}
    for name, (column, step) in readings.items():
        if (column.casefold() not in indices or type(step) is not int or step < 1
                or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,39}", name)):
            raise ModflowError(f"Cannot place observation {name!r} at {column!r}, data row {step}.",
                               error_code="MODFLOW_OBSERVATION_UNPLACED")
        by_row.setdefault(step, []).append((indices[column.casefold()], name))
    lines = ["pif ~"]
    previous = -1  # The first physical line is the OBS CSV header.
    for step, reads in sorted(by_row.items()):
        tokens = [f"l{step - previous}"]
        index = 0
        for column, name in sorted(reads):
            if column == index:
                raise ModflowError(f"Observation {name!r} repeats a CSV cell already read.",
                                   error_code="MODFLOW_OBSERVATION_UNPLACED")
            tokens.extend(["~,~"] * (column - index))
            tokens.append(f"!{name}!")
            index = column
        lines.append(" ".join(tokens))
        previous = step
    instruction = f"{output}.ins"
    (directory / instruction).write_text("\n".join(lines) + "\n")
    return instruction, output


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
    columns = [target.name for target, _cell in targets]
    return observe_csv(directory, OBS_OUTPUT, columns, {name: (name, 1) for name in columns})


def deck_sheet(directory: Path, the_deck: Deck) -> Any:
    """The flow model's sheet as the deck states it: the grid, clock and output control its outputs are
    read by, and the arrays mf6 requires, each read off the files the run solved."""
    import flopy

    from trid3nt.workflows.modflow.modules import GWF
    from trid3nt.workflows.runtime.sheet import fill

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


def interface(source: Path, dest: Path, chosen: Sequence[tuple[str, float, float, bool]], *, the_deck: Deck,
              targets: Sequence[tuple[Observed, int]], spatial: Mapping[str, Mapping[str, Any]] = {},
              zones: Mapping[str, Sequence[Mapping[str, Any]]] = {}, sources: Mapping[str, str] = {},
              frame_epsg: int | None = None
              ) -> tuple[list[Parameter], list[tuple[str, str]], list[tuple[str, str]]]:
    """A study directory: the deck at ``source`` copied to ``dest`` with a multiplier template over each chosen
    ``(identifier, lower, upper, absolute)`` - or its pilot points where ``spatial`` holds its field, or one parameter
    per stated zone where ``zones`` does - and the wells' heads observed -> ``(parameters, templates, instructions)``.
    Pilot-point arrays are laid first, so a multiplier template over another array of the same file is written over
    the file they leave."""
    shutil.copytree(source, dest)
    parameters, templates = [], []
    order = sorted(chosen, key=lambda row: (row[0] not in spatial, row[0] not in zones))
    for identifier, lower, upper, absolute in order:
        if identifier in spatial and identifier in zones:
            raise PestppError(f"{identifier} is both laid by pilot points and divided into zones; state one",
                              error_code="PESTPP_OPTION_CONFLICT")
        source_of = sources.get(identifier.lower(), "")
        if identifier in zones:
            laid, pairs = zoned(dest, identifier, zones[identifier], lower=lower, upper=upper, the_deck=the_deck,
                                absolute=absolute, source=source_of, frame_epsg=frame_epsg)
            parameters.extend(laid)
            templates.extend(pair for pair in pairs if pair not in templates)
            continue
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
        parameter, pair = multiplier(dest, identifier, lower=lower, upper=upper, the_deck=the_deck,
                                     absolute=absolute, source=source_of)
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


async def calibrated(*, finished: Any, study_dir: Path, the_deck: Deck, parameters: list,
                     templates: list, best: Mapping[str, float], frame: str) -> dict[str, Any]:
    """The deck with the best parameters written in, staged and solved as an ordinary MODFLOW run -> its handle."""
    import asyncio

    from trid3nt_contracts import new_ulid

    from trid3nt.workflows.modflow.authoring.staged_check import check_staged_run
    from trid3nt.workflows.solver.staging import new_rundir, stage_run

    run_id = new_ulid()
    rundir = new_rundir(run_id, stem="modflow")
    sheet, values = await asyncio.to_thread(realize, rundir, study_dir, the_deck, parameters, templates, best)
    case = finished.case
    results = list(case.get("results") or [])
    staged = await stage_run(
        rundir, run_id, module=str(case["module"]), steering=str(case["steering"]), results=results,
        outputs=list(dict.fromkeys([*finished.outputs, OBS_OUTPUT])), mesh_inputs=[],
        prefix="modflow", sheet={}, server_facts=dict(case.get("server_facts") or {}),
        result_basename=(results or list(finished.outputs))[0], check=check_staged_run)
    solved = await solve_resolved(staged, finished, frame=frame, sheet=sheet)
    return {**solved, "study_values": values}


def realize(directory: Path, study_dir: Path, the_deck: Deck, parameters: list, templates: list,
            best: Mapping[str, float]) -> tuple[Any, dict[str, Any]]:
    """Write the fitted deck and read its physical model values."""
    from .control import TABLES

    substitute(study_dir, templates, parameters, best)
    laid = lay(study_dir)
    own = [template for template, _file in templates] + [f"{OBS_OUTPUT}.ins", *TABLES.values(), *laid]
    copy_deck(study_dir, directory, own)
    sheet = deck_sheet(directory, the_deck)
    values = {}
    for p in parameters:
        name = (p.original_name or p.spatial_group or p.name).upper()
        if name not in values:
            values[name] = physical_value(directory, the_deck, sheet, name, best[p.name] * p.scale + p.offset)
    return sheet, values


def physical_value(directory: Path, the_deck: Deck, sheet: Any, name: str, scalar: float) -> Any:
    """The resolved array values in model units, or the native list-column value."""
    if name in sheet.filled:
        return sheet.filled[name].value
    try:
        target = eligibility.resolve(name.split("@", 1)[0], the_deck)
    except PestppError:
        target = None
    if target is not None and target.is_column:
        return scalar
    import flopy

    prefix = target.kind.lower() if target is not None else name.partition("_")[0].lower()
    variable = target.variable if target is not None else name.partition("_")[2].lower()
    sim = flopy.mf6.MFSimulation.load(sim_ws=str(directory), verbosity_level=0, load_only=[prefix])
    array = getattr(sim.get_model(the_deck.model).get_package(prefix), variable).array
    return array.reshape(the_deck.nlay, the_deck.ncpl).tolist()


async def solve_resolved(staged: Mapping[str, Any], finished: Any, *, frame: str, sheet: Any = None) -> dict:
    from trid3nt.workflows.modflow.workflow import run_sheet
    from trid3nt.workflows.solver.stop import SolveStopped

    if sheet is None:
        directory = Path(staged["rundir"])
        sheet = deck_sheet(directory, deck(directory, finished))
    try:
        handle = await run_sheet(sheet=sheet, staged=staged, mesh=finished.mesh,
                                 name=str((staged["case"].get("server_facts") or {}).get("name") or "domain"),
                                 moment=None, frame=frame or None)
    except SolveStopped as stop:
        stop.solved["deck_sheet"] = sheet
        raise
    return {**handle, "deck_sheet": sheet, "staged": staged}


async def published(solved: Mapping[str, Any], *, partial: str | None = None) -> Any:
    """The calibrated run published as MODFLOW publishes a run: every output its output control saved."""
    from trid3nt.workflows.modflow.workflow import publish_outputs

    return await publish_outputs(run=solved, sheet=solved["deck_sheet"], outputs=[], captions={}, params={},
                                 partial=partial)
