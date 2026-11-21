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
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from ..authoring.control import Observed, Parameter, substitute, unheld_note
from ..errors import PestppError

__all__ = ["Deck", "OBSERVED", "OBS_FILE", "OBS_OUTPUT", "OWN", "PILOT_POINTS", "PLACE", "ROW", "SITE", "TRIAL", "calibrated",
           "command", "copy_deck", "deck", "deck_sheet", "fields", "head_targets", "interface", "lay", "multiplier",
           "observe_heads", "pilot_points", "published", "substitute", "supplied", "targets"]

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


def _block(text: str, name: str) -> list[str]:
    found = re.search(rf"^\s*BEGIN\s+{name}\b[^\n]*\n(.*?)^\s*END\s+{name}\b", text, re.I | re.M | re.S)
    return [] if found is None else [line.split("#", 1)[0].strip() for line in found.group(1).splitlines()
                                     if line.split("#", 1)[0].strip()]


def command(the_deck: Deck) -> str:
    """What a trial runs: each pilot-point array laid into its own file (none where the study has none), then mf6;
    every multiplier is already written into its own files. PEST++ starts the command without a shell."""
    return f"python {TRIAL}"


def deck(directory: Path, finished: Any = None) -> Deck:
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
                length_units=str(dis.length_units.array or "unknown").lower(), steps=steps,
                cell_size=_median_edge(dis))


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


def multiplier(directory: Path, identifier: str, *, lower: float, upper: float, the_deck: Deck,
               absolute: bool = False) -> tuple[Parameter, tuple[str, str]]:
    """One multiplier parameter over the array or list column ``identifier`` names -> the parameter and its
    (template, file).

    The parameter starts at 1, the run's own values; every control record of an array, or every row of a list
    column, takes the same multiplier, and records or rows stating different values refuse, one parameter writing
    one scale. ``absolute`` bounds are stated in the run's own unit and divided by that scale; they refuse over an
    array read INTERNAL, whose FACTOR is not its values, and where the run's own value lies outside them. A second
    array of the same file is written into the template the first one left."""
    prefix, _, variable = identifier.upper().partition("_")
    file = the_deck.packages.get(prefix)
    if file is None or not variable:
        raise PestppError(f"{identifier} names no package of the run's flow model; it states "
                          f"{', '.join(sorted(the_deck.packages))}", error_code="PESTPP_PARAMETER_UNPLACED")
    template = f"{file}.tpl"
    written = directory / template
    lines = (written.read_text().split("\n", 1)[1] if written.is_file()
             else (directory / file).read_text()).splitlines()
    name = identifier.lower()
    field = "~" + name.ljust(_FIELD - 2) + "~"
    if prefix in _LIST_COLUMNS:
        scale = _list_column(lines, identifier, _LIST_COLUMNS[prefix], variable.lower(), field)
    else:
        scale, internal = _array(lines, identifier, variable, field)
        if absolute and internal:
            raise PestppError(f"{identifier} is bounded in the run's own unit, and its array is read INTERNAL, "
                              "whose FACTOR is not its values: state its bounds as multipliers of the run's own "
                              "array", error_code="PESTPP_BOUNDS_UNSTATED")
    if absolute:
        lower, upper = lower / scale, upper / scale
        if not 0.0 < lower < 1.0 < upper:
            raise PestppError(f"{identifier} is {scale:g} in the run, outside its stated bounds "
                              f"{lower * scale:g} to {upper * scale:g}", error_code="PESTPP_BOUNDS_UNSTATED")
    written.write_text("ptf ~\n" + "\n".join(lines) + "\n")
    return (Parameter(name=name, value=1.0, lower=float(lower), upper=float(upper), group=name,
                      scale=scale), (template, file))


def _list_column(lines: list[str], identifier: str, columns: Sequence[str], variable: str, field: str) -> float:
    """The PACKAGEDATA column ``variable`` written as ``field`` in every row of ``lines`` -> the one value they
    state; a column the package has not, or rows stating different values, refuse."""
    if variable not in columns:
        raise PestppError(f"{identifier} names no PACKAGEDATA column; it states {', '.join(columns)}",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    rows = [i for i, line in enumerate(lines)
            if re.match(r"^\s*BEGIN\s+packagedata\b", line, re.I)]
    if len(rows) != 1:
        raise PestppError(f"{identifier}: the package states {len(rows)} PACKAGEDATA blocks",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    scales, at = set(), rows[0] + 1
    while at < len(lines) and not re.match(r"^\s*END\s+packagedata\b", lines[at], re.I):
        # a field a template already writes is one token, the spaces inside its delimiters included
        tokens = re.findall(r"~[^~]*~|[^\s~]+", lines[at].split("#", 1)[0])
        if tokens:
            # a reach on no cell states NONE in place of its DISV layer and cell
            column = 1 + (1 if tokens[1].upper() == "NONE" else 2) + columns.index(variable)
            scales.add(float(tokens[column]))
            tokens[column] = field
            lines[at] = "  " + "  ".join(tokens)
        at += 1
    if len(scales) != 1:
        raise PestppError(f"{identifier} states {len(scales)} different values across the package's rows; one "
                          "multiplier writes one scale", error_code="PESTPP_PARAMETER_UNPLACED")
    return scales.pop()


def _array(lines: list[str], identifier: str, variable: str, field: str) -> tuple[float, bool]:
    """The array ``variable``'s control records written with ``field`` -> its one scale, and whether it is read INTERNAL."""
    starts = [i for i, line in enumerate(lines)
              if re.match(rf"^\s*{re.escape(variable)}(\s+LAYERED)?\s*$", line, re.I)]
    if len(starts) != 1:
        raise PestppError(f"{identifier} is stated {len(starts)} times as an array in its package; a multiplier is "
                          "written into one array's control records", error_code="PESTPP_PARAMETER_UNPLACED")
    scales, kinds, at = set(), set(), starts[0] + 1
    while at < len(lines):
        record = _CONTROL.match(lines[at])
        if record is None:
            if re.match(r"^\s*[-+0-9.]", lines[at]):
                at += 1
                continue
            break
        kind = record.group(2).upper()
        kinds.add(kind)
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
    return scales.pop(), "INTERNAL" in kinds


def fields(spatial: Mapping[str, Any], the_deck: Deck) -> dict[str, dict[str, Any]]:
    """Each pilot-point parameter's field as stated, pyEMU's own default where not -> ``{identifier: field}``: the
    spacing between its points and its exponential variogram's range, in metres of the run's grid, each with its source.

    ``spatial`` is ``{identifier: {"spacing": S, "source": "...", "variogram": {"a": A, "source": "..."}}}``, any part
    left out. Unstated, the spacing is 10 of the grid's median edges and the range that spacing (pyEMU's rule); a range left
    out of a stated spacing is that spacing. A variogram of another type, or a member not read, refuses."""
    out: dict[str, dict[str, Any]] = {}
    for identifier, stated in spatial.items():
        stated = dict(stated or {})
        variogram = dict(stated.get("variogram") or {})
        unread = sorted(set(stated) - {"spacing", "source", "variogram"}) + sorted(set(variogram) - {"a", "source", "type"})
        if unread:
            raise PestppError(f"{identifier}'s pilot points state {len(unread)} members a study does not read; it "
                              "reads a spacing and its source, and a variogram's type, range a and source",
                              error_code="PESTPP_PILOT_POINTS_UNREAD")
        if str(variogram.get("type") or "exponential").lower() != "exponential":
            raise PestppError(f"{identifier}'s variogram is stated of another type, and a study lays an exponential "
                              "one", error_code="PESTPP_VARIOGRAM_UNKNOWN")
        if stated.get("spacing") is not None:
            spacing, spacing_source = float(stated["spacing"]), str(stated.get("source") or "stated by the user")
        else:
            spacing, spacing_source = _EVERY * float(the_deck.cell_size or 0.0), _PYEMU
        if variogram.get("a") is not None:
            a, a_source = float(variogram["a"]), str(variogram.get("source") or "stated by the user")
        else:
            a, a_source = spacing, _PYEMU if spacing_source == _PYEMU else "pyEMU 1.7.0 PstFrom default: the range is the spacing"
        if not (spacing > 0.0 and a > 0.0):
            raise PestppError(f"{identifier}'s pilot points are spaced {spacing:g} with a range of {a:g}; both above 0",
                              error_code="PESTPP_PILOT_POINTS_UNSTATED")
        out[identifier] = {"spacing": spacing, "spacing_source": spacing_source, "variogram": "exponential",
                           "a": a, "a_source": a_source, "contribution": 1.0, "nugget": 0.0,
                           "stated": stated.get("spacing") is not None or variogram.get("a") is not None}
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


def _values(lines: list[str], identifier: str, variable: str, count: int) -> tuple[int, int, list[float]]:
    """The array ``variable``'s record in ``lines`` -> (its first line, the line after it, its ``count`` values as mf6
    reads them, FACTOR applied); an array read from its own file refuses."""
    starts = [i for i, line in enumerate(lines) if re.match(rf"^\s*{re.escape(variable)}\s*$", line, re.I)]
    if len(starts) != 1:
        raise PestppError(f"{identifier} is stated {len(starts)} times as one array in its package; pilot points are "
                          "laid over one array of one layer", error_code="PESTPP_PARAMETER_UNPLACED")
    record = _CONTROL.match(lines[starts[0] + 1]) if starts[0] + 1 < len(lines) else None
    kind = record.group(2).upper() if record else ""
    if kind not in ("CONSTANT", "INTERNAL"):
        raise PestppError(f"{identifier} is read from a file of its own (OPEN/CLOSE), which this study does not lay",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    at, values = starts[0] + 2, []
    if kind == "CONSTANT":
        values = [float(record.group(3).split()[0])] * count
    else:
        stated = _FACTOR.search(record.group(3))
        factor = float(stated.group(2)) if stated else 1.0
        while at < len(lines) and re.match(r"^\s*[-+0-9.]", lines[at]):
            values.extend(factor * float(v) for v in lines[at].split())
            at += 1
    if len(values) != count:
        raise PestppError(f"{identifier} states {len(values)} values over {count} cells", error_code="PESTPP_DECK_UNREAD")
    return starts[0], at, values


def pilot_points(directory: Path, identifier: str, field: Mapping[str, Any], *, lower: float, upper: float,
                 the_deck: Deck) -> tuple[list[Parameter], tuple[str, str]]:
    """The array ``identifier`` names laid by pilot points -> each point's parameter and the (template, file) PEST
    writes their values through.

    The array moves into a file of its own (``<package file>.<array>.dat``, the run's values to start), the points are
    placed as pyEMU places them over the grid at the field's spacing, and each cell's ordinary kriging factors over them
    are written beside it for the trial, which lays the run's values times 10 ** (the factors over the log10 of the
    points). Each point is a multiplier starting at 1 inside the parameter's bounds, carrying its place and the
    variogram's range for the prior. A run of more than one layer refuses."""
    import numpy as np

    from ..helpers.geostat import kriging_factors

    prefix, _, variable = identifier.upper().partition("_")
    file = the_deck.packages.get(prefix)
    if file is None or not variable or prefix in _LIST_COLUMNS:
        raise PestppError(f"{identifier} names no array of the run's flow model a pilot point lays; it states "
                          f"{', '.join(sorted(the_deck.packages))}", error_code="PESTPP_PARAMETER_UNPLACED")
    if the_deck.nlay != 1:
        raise PestppError(f"the run has {the_deck.nlay} layers, and pilot points are laid over one",
                          error_code="PESTPP_PARAMETER_UNPLACED")
    name = identifier.lower()
    lines = (directory / file).read_text().splitlines()
    first, after, base = _values(lines, identifier, variable.lower(), the_deck.ncpl)
    array = f"{file}.{variable.lower()}.dat"
    lines[first + 1:after] = [f"    OPEN/CLOSE  {array}  FACTOR  1.0"]
    (directory / file).write_text("\n".join(lines) + "\n")
    (directory / array).write_text("\n".join(repr(v) for v in base) + "\n")
    centres, cells = _grid(directory, the_deck)
    sites = _placed(centres, cells, float(field["spacing"]))
    names = [f"{name}_pp{k:04d}" for k in range(len(sites))]
    index, weight = kriging_factors(centres, sites, float(field["a"]))
    stem = f"{name}.pp"
    np.savez(directory / f"{stem}.npz", names=np.array(names), index=index, weight=weight, base=np.array(base))
    (directory / f"{stem}.dat").write_text("".join(f"{n} 1.0\n" for n in names))
    (directory / f"{stem}.dat.tpl").write_text(
        "ptf ~\n" + "".join(f"{n} ~{n.ljust(_FIELD - 2)}~\n" for n in names))
    spec = directory / PILOT_POINTS
    held = json.loads(spec.read_text()) if spec.is_file() else {"arrays": []}
    held["arrays"].append({"array": array, "values": f"{stem}.dat", "factors": f"{stem}.npz"})
    spec.write_text(json.dumps(held, indent=1))
    return ([Parameter(name=n, value=1.0, lower=float(lower), upper=float(upper), group=name,
                       site=(float(x), float(y)), correlation=float(field["a"])) for n, (x, y) in zip(names, sites)],
            (f"{stem}.dat.tpl", f"{stem}.dat"))


def lay(directory: Path) -> list[str]:
    """Each pilot-point array of the study at ``directory`` laid off its points' values as PEST wrote them -> the study's
    own files the arrays are laid from (none where it has no pilot points); the trial lays them the same way."""
    import numpy as np

    spec = directory / PILOT_POINTS
    if not spec.is_file():
        return []
    own = [PILOT_POINTS]
    for entry in json.loads(spec.read_text())["arrays"]:
        held = np.load(directory / entry["factors"])
        stated = dict(line.split()[:2] for line in (directory / entry["values"]).read_text().splitlines() if line.strip())
        values = np.log10(np.array([float(stated[str(n)]) for n in held["names"]]))
        laid = held["base"] * 10.0 ** (held["weight"] * values[held["index"]]).sum(axis=1)
        (directory / entry["array"]).write_text("\n".join(repr(float(v)) for v in laid) + "\n")
        own += [entry["values"], entry["factors"]]
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
              targets: Sequence[tuple[Observed, int]], spatial: Mapping[str, Mapping[str, Any]] = {}
              ) -> tuple[list[Parameter], list[tuple[str, str]], list[tuple[str, str]]]:
    """A study directory: the deck at ``source`` copied to ``dest`` with a multiplier template over each chosen
    ``(identifier, lower, upper, absolute)`` - or its pilot points, where ``spatial`` holds its field - and the wells'
    heads observed -> ``(parameters, templates, instructions)``. Pilot-point arrays are laid first, so a multiplier
    template over another array of the same file is written over the file they leave."""
    shutil.copytree(source, dest)
    parameters, templates = [], []
    for identifier, lower, upper, absolute in sorted(chosen, key=lambda row: row[0] not in spatial):
        if identifier in spatial:
            if absolute:
                raise PestppError(f"{identifier} is laid by pilot points, each a multiplier on the run's own values, "
                                  "so its bounds are factors, not values in the run's own unit",
                                  error_code="PESTPP_BOUNDS_UNSTATED")
            laid, pair = pilot_points(dest, identifier, spatial[identifier], lower=lower, upper=upper,
                                      the_deck=the_deck)
            parameters.extend(laid)
            templates.append(pair)
            continue
        parameter, pair = multiplier(dest, identifier, lower=lower, upper=upper, the_deck=the_deck,
                                     absolute=absolute)
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
    from trid3nt.workflows.modflow.workflow import run_sheet
    from trid3nt.workflows.solver.staging import new_rundir, stage_run

    from .control import TABLES

    run_id = new_ulid()
    rundir = new_rundir(run_id, stem="modflow")
    await asyncio.to_thread(substitute, study_dir, templates, parameters, best)
    laid = await asyncio.to_thread(lay, study_dir)
    own = [template for template, _file in templates] + [f"{OBS_OUTPUT}.ins", *TABLES.values(), *laid]
    await asyncio.to_thread(copy_deck, study_dir, rundir, own)
    sheet = await asyncio.to_thread(deck_sheet, rundir, the_deck)
    case = finished.case
    results = list(case.get("results") or [])
    staged = await stage_run(
        rundir, run_id, module=str(case["module"]), steering=str(case["steering"]), results=results,
        outputs=list(dict.fromkeys([*finished.outputs, OBS_OUTPUT])), mesh_inputs=[],
        prefix="modflow", sheet={}, server_facts=dict(case.get("server_facts") or {}),
        result_basename=(results or list(finished.outputs))[0], check=check_staged_run)
    handle = await run_sheet(sheet=sheet, staged=staged, mesh=finished.mesh,
                             name=str((case.get("server_facts") or {}).get("name") or "domain"),
                             moment=None, frame=frame or None)
    return {**handle, "deck_sheet": sheet}


async def published(solved: Mapping[str, Any]) -> Any:
    """The calibrated run published as MODFLOW publishes a run: every output its output control saved."""
    from trid3nt.workflows.modflow.workflow import publish_outputs

    return await publish_outputs(run=solved, sheet=solved["deck_sheet"], outputs=[], captions={}, params={})
