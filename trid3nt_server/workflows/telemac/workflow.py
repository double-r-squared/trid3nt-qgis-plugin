"""The TELEMAC workflow: fill, run, then read.

``fill`` sets slots, expands composites and binds producers; it is repeatable and decides nothing.
``run`` serializes the sheet, stages the run directory and hands it to the box; it is explicit and consequential.
``publish_outputs`` reads the primitives a template listed off the solved run and publishes each as asked."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from importlib import import_module
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.payload_warning import ParamSheet, ParamSheetRow

from trid3nt_server.render.formats import publish
from trid3nt_server.workflows.runtime import (
    PlanValidationError,
    RunResult,
    Workflow,
)
from trid3nt_server.workflows.telemac.errors import TelemacError
from trid3nt_server.workflows.telemac.modules import wrapper_for
from trid3nt_server.workflows.telemac.modules.module import (SlotRefused, accept,
                                                              identify_on)
from trid3nt_server.workflows.telemac.modules.outputs import (
    Line,
    OutputEmpty,
    Primitive,
    Profile,
    Solved,
    deliver,
)
from trid3nt_server.workflows.telemac.modules.sheet import (
    PROCESSORS,
    Origin,
    Sheet,
    CONTINUATION,
)
from trid3nt_server.workflows.telemac.modules.sheet import fill as fill_slots
from trid3nt_server.workflows.telemac.modules.sheet import fill_coupled
from trid3nt_server.workflows.telemac.modules.sheet import run as run_sheet_

if TYPE_CHECKING:
    from trid3nt_server.inputs.fill import Fill

logger = logging.getLogger("trid3nt_server.workflows.telemac.workflow")

__all__ = ["Measured", "Placed", "TelemacWorkflow", "card_rows",
           "fill_sheet", "publish_outputs", "run_bodies", "run_sheet", "stated"]

_TELEMAC = "trid3nt_server.workflows.telemac"

#: What a reader may open on any run, past the files the engine is required to
#: write: the mesh it solved on, the deck it read, the listing and the metrics.
_ALWAYS_READABLE = ("full_listing.log", "telemac_metrics.json")

#: What the workflow meshes a domain with, and the element it lays, where the
#: template declares no recipe of its own.
_MESHER, _MESH_KIND = "om2d", "unstructured_tri"

#: The dispatch a staged run goes to, by the path it is resolved at CALL time.
_DISPATCH = f"{_TELEMAC}.engine.solve_case"

#: What the settle is called: what the deck and every later stage read it as.
_SETTLED = "settled"

#: The measurements the workflow takes of this question's world, by the KIND a composite asks for: the runner and the stage
#: it is taken at (before meshing, before the settle, as the settle, or against the settled run).
_MEASURES: Mapping[str, tuple[str, str, str]] = MappingProxyType({
    "footprint": ("trid3nt_server.inputs.structure.structure", "prep", "world"),
    "transect": ("trid3nt_server.inputs.structure.transect", "prep", "world"),
    "rating": (f"{_TELEMAC}.authoring.rating_curve.settle_outlet_rating",
               "author", "produce"),
    "harbour": (f"{_TELEMAC}.authoring.walked_boundary.settle_harbour",
                "author", "settle"),
    "dredge": (f"{_TELEMAC}.authoring.reference_surface.settle_dredge",
               "author", "derive")})

#: The mesher's own clean passes every domain gets first. They change the TOPOLOGY, so they run ahead of the bed and roles
#: (a renumbering after a primitive painted node values is refused). Smoothing is NOT among them: it folds elements beside
#: a rim locked at one spacing, and the boundary walk meets the folded pair's unpaired edges as a second rim.
def _clean_ops() -> list[Any]:
    from trid3nt_server.tools.mesh.tool import mesh_op

    return [mesh_op("delete_boundary_faces"),
            mesh_op("delete_faces_connected_to_one_face"),
            mesh_op("make_mesh_boundaries_traversable"),
            mesh_op("fix_mesh", delete_unused=True)]


@dataclass(frozen=True, slots=True)
class Placed:
    """A point the run SETTLES onto a node of the accepted mesh, under ``name``.

    ``point`` names the run input holding it; with none, it sits ``fraction`` (a number or an input name) along the domain's centerline."""

    name: str
    point: str | None = None
    fraction: Any = 0.5
    #: What the marker published before the solve is called on the map.
    label: str = "Release point"
    #: Read the initial wet state off the run this one carries on from: a source
    #: has to enter water, and a continued run opens at another run's surface.
    continues: bool = False


@dataclass(frozen=True, slots=True)
class Measured:
    """A measurement the run takes against its own world, under ``name``.

    ``kind`` picks the runner; ``reads`` maps its arguments to run inputs by name; ``asked`` states the literals only this question can state."""

    name: str
    kind: str = ""
    reads: Mapping[str, Any] = field(default_factory=dict)
    asked: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in _MEASURES:
            raise PlanValidationError(
                f"{self.name}: there is no {self.kind!r} measurement; the "
                f"workflow takes {tuple(_MEASURES)}.")
        object.__setattr__(self, "reads", MappingProxyType(dict(self.reads)))
        object.__setattr__(self, "asked", MappingProxyType(dict(self.asked)))


def _names(value: Any) -> list[str]:
    """Every input name a ``reads`` mapping names, nested mappings included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [name for item in value.values() for name in _names(item)]
    return []


async def _read_named(env: Any, value: Any) -> Any:
    """A ``reads`` mapping with every name replaced by what the run holds."""
    from trid3nt_server.inputs.fill import read

    if isinstance(value, str):
        return await read(env, value)
    if isinstance(value, Mapping):
        return {key: await _read_named(env, item) for key, item in value.items()}
    return value


def _painted(row: Mapping[str, Any]) -> list[Primitive]:
    """One table row -> the ONE layer the run publishes of it: the temporal layer where the row varies in time, the final frame where it does not.
    A still beside a time series would copy a frame the temporal layer already carries."""
    from trid3nt_server.workflows.telemac.modules.outputs import field

    token, module, style = row["token"], row["module"], row.get("style")
    if row.get("varies"):
        return [field(token, t="every", module=module).animate(style=style)]
    return [field(token, t=-1, module=module).layer(style=style)]


@dataclass(frozen=True, slots=True)
class _Written:
    """One variable a result file carries, and the layer it is published as.
    ``spelling`` is the file's own name (empty for a row the result lacks); ``first`` names the file whose layer carries a repeated spelling;
    ``rowed`` whether a module row names it; ``withheld`` the module's mark on a slot its engine never writes."""

    primitive: Primitive
    file: str
    spelling: str
    rowed: bool
    first: str | None = None
    withheld: str = ""


def _written(run: Mapping[str, Any],
             solved: Callable[[str], Solved]) -> list[_Written]:
    """EVERY variable this run wrote -> the one layer each is published under.
    Module rows come first in table order, then every variable a result file carries that no row resolved; a spelling two files carry is ONE layer.
    A table decides how a variable is drawn, never whether; a slot marked never-written is carried with that mark and published by no layer."""
    from trid3nt_server.workflows.telemac.modules.outputs import field

    written: list[_Written] = []
    claimed: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()

    def _claim(primitives: list[Primitive], file: str, spelling: str,
               rowed: bool) -> None:
        seen.add((file, spelling.upper()))
        first = claimed.get(spelling.upper())
        if first is not None:
            written.append(_Written(primitives[0], file, spelling, rowed, first))
            return
        claimed[spelling.upper()] = file
        written.extend(_Written(p, file, spelling, rowed) for p in primitives)

    rows = list(run.get("module_output") or ())
    for row in rows:
        read = solved(str(row["module"]))
        try:
            variable, _ = read.variable(str(row["token"]))
        except OutputEmpty:
            # A row the result does not carry claims no spelling and is skipped downstream.
            written.extend(_Written(p, read.result_file, "", True)
                           for p in _painted(row))
            continue
        _claim(_painted(row), read.result_file, variable.strip(), True)
    walked: set[str] = set()
    for module in dict.fromkeys([str(run["module"]),
                                 *(str(row["module"]) for row in rows)]):
        read = solved(module)
        if read.result_file in walked:
            continue
        walked.add(read.result_file)
        marked = {name.upper(): entry.cited
                  for entry in read.body.UNWRITTEN.values()
                  for name in (entry.spelling, entry.french) if name}
        for variable in read.result["varnames"]:
            spelling = str(variable).strip()
            if (read.result_file, spelling.upper()) in seen:
                continue
            if spelling.upper() in marked:
                seen.add((read.result_file, spelling.upper()))
                written.append(_Written(
                    field(spelling, t="every", module=module), read.result_file,
                    spelling, False, withheld=(
                        f"{read.body.MODULE} never writes it, by its own source "
                        f"- {marked[spelling.upper()]}")))
                continue
            _claim([field(spelling, t="every", module=module).animate()],
                   read.result_file, spelling, False)
    return written


def _account(written: Sequence[_Written], layers: Sequence[LayerURI],
             why: Mapping[Primitive, str]) -> None:
    """Journal what became of every variable a result file carries, read off the layers the publish SURFACED."""
    from trid3nt_server.render.formats import quantity_of
    from trid3nt_server.workflows.runtime.journal import journal_note

    landed = {layer.quantity for layer in layers}
    for entry in written:
        if not entry.spelling:
            continue
        where = f"{entry.file} wrote {entry.spelling!r}"
        if entry.withheld:
            journal_note(f"{where} and no layer carries it: {entry.withheld}")
        elif quantity_of(entry.spelling.lower()) not in landed:
            journal_note(f"{where} and no layer carries it: "
                         f"{why.get(entry.primitive.key, 'the publish surfaced none')}")
        elif entry.first is not None:
            journal_note(f"{where}, which {entry.first} wrote as well; the one "
                         f"layer of it is read off {entry.first}")
        elif not entry.rowed:
            journal_note(f"{where} and no module row names it; it is published "
                         "under the spelling the result file carries")


#: How long a stated value is printed before the doc names its shape: a whole tracer array crowds the keywords off the page.
_VALUE_CHARS = 48


def _stated_value(value: Any) -> str:
    """One asserted value, as the docstring prints it."""
    if value is None:
        return "nothing (the engine's default stands)"
    if isinstance(value, (list, tuple)):
        text = ", ".join(_stated_value(item) for item in value)
        return f"[{text}]" if len(text) <= _VALUE_CHARS else f"{len(value)} values"
    return f"{value:g}" if isinstance(value, float) else str(value)


def _unanchored(primitive: Primitive) -> Primitive:
    return replace(primitive, at=None, along=None, within=None)


def _anchored(primitive: Primitive, anchor: Mapping[str, Any]) -> Primitive:
    return replace(primitive, at=anchor["at"], along=anchor["along"],
                   within=anchor.get("within"))


async def publish_outputs(*, run: Mapping[str, Any], outputs: Sequence[Primitive],
                          captions: Mapping[str, str],
                          params: Mapping[str, Any],
                          anchors: Sequence[Mapping[str, Any]] = ()
                          ) -> LayerURI:
    """Read what the run wrote and publish every variable, ONE layer each (temporal where the row varies in time, else the final frame).
    Module rows style the layer; a variable no row names goes under the file's own spelling and one reaching no layer is journalled by name. Each module's result is read ONCE.
    A chart's reference is a callable or another primitive read; a placed read the result lacks refuses."""
    solved: dict[str, Solved] = {}

    def _solved(module: str) -> Solved:
        if module not in solved:
            solved[module] = Solved(run, wrapper_for(module))
        return solved[module]

    written = await asyncio.to_thread(_written, run, _solved)
    table = [entry.primitive for entry in written
             if entry.first is None and not entry.withheld]
    if anchors:
        outputs = [_anchored(p, a) for p, a in zip(outputs, anchors)]

    published_keys = {primitive.key for primitive in outputs}
    why: dict[Primitive, str] = {}

    def _read(key: Primitive) -> Any:
        read = _solved(key.module or str(run["module"]))
        try:
            return read.body.READS[key.kind](key, read)
        except OutputEmpty as exc:
            # A row of the module's OWN table the result lacks is skipped and journalled; a PLACED read is a refusal.
            if key in published_keys:
                raise
            why[key] = str(exc)
            return None

    def _beside(primitive: Primitive) -> Primitive | None:
        reference = primitive.reference
        if not isinstance(reference, Primitive):
            return None
        return replace(reference, at=primitive.at, along=primitive.along).key

    wanted = {primitive.key for primitive in (*table, *outputs)}
    wanted |= {_beside(p) for p in outputs if _beside(p) is not None}
    reads = await asyncio.to_thread(lambda: {key: _read(key) for key in wanted})
    for primitive in outputs:
        if primitive.reference is None:
            continue
        read = reads[primitive.key]
        beside = _beside(primitive)
        if beside is None:
            lines = tuple(primitive.reference(read, reads, params))
        else:
            other = reads[beside]
            caption = captions.get(primitive.variable or primitive.kind, primitive.kind)
            lines = (Line(label=f"{caption} at t = {other.measures['t']:g} s",
                          x=(other.distance_m if isinstance(other, Profile)
                             else other.times),
                          values=other.values),)
        reads[primitive.key] = replace(read, lines=lines)
    name, where = str(run["name"]), str(params.get("location") or run["name"])

    def _delivered(primitive: Primitive, caption: str) -> Any:
        return deliver(primitive, reads[primitive.key],
                       solved[primitive.module or str(run["module"])],
                       caption=caption, name=name, where=where)

    for primitive in outputs:
        if primitive.publish == "note":
            _note(captions.get(primitive.variable or primitive.kind,
                               primitive.kind), reads[primitive.key])
    items = await asyncio.to_thread(
        lambda: [_delivered(primitive, reads[primitive.key].name.strip().lower())
                 for primitive in table if reads[primitive.key] is not None]
        + [_delivered(primitive, captions.get(primitive.variable or primitive.kind,
                                              primitive.kind))
           for primitive in outputs if primitive.publish != "note"])
    published = await publish(run_id=str(run["run_id"]), engine="telemac",
                              name=name, items=items)

    _account(written, published.layers, why)
    if not published.layers:
        raise SlotRefused(
            f"the {run['module']} run's result files carry no variable at all, "
            "so it published nothing and there is nothing to paint.")
    logger.info("telemac outputs published run_id=%s layers=%d charts=%d",
                run["run_id"], len(published.layers), len(published.charts))
    return await asyncio.to_thread(_record, _solved(str(run["module"])),
                                   name=name)


def _note(caption: str, read: Any) -> None:
    """A measured read, said on the run's record in the figures it measured."""
    from trid3nt_server.workflows.runtime.journal import journal_note

    figures = "; ".join(f"{name} = {value}" for name, value in read.measures.items())
    journal_note(f"{caption}: {figures or 'the run measured nothing for it'}")


def _record(solved: Solved, *, name: str) -> LayerURI:
    """The run's own record: the mesh every published group rides; it binds no group, so it is DRAWN rather than measured."""
    from trid3nt_server.store import objects as storage

    return LayerURI(
        layer_id=f"telemac-{solved.run_id}", name=name, layer_type="mesh",
        uri=f"s3://{storage.runs_bucket()}/{solved.run_id}/{solved.display_file}",
        style={"kind": "reference"}, bbox=solved.bbox,
        crs_authid=f"EPSG:{solved.utm_epsg}",
        reference_time=solved.run.get("started_at"))


def stated(*, steering: type, keywords: Mapping[str, Any]) -> dict[str, Any]:
    """The run's own keyword values by identifier, resolved ONCE before any stage.
    The template's assertions under the floor that overrides them; a composite has no number of its own and is absent."""
    out = {name: value for name, value in steering.ASSERTED.items()
           if value is not None and name in steering.MODULE_INPUT}
    return {**out, **_floor(steering, keywords)[0]}


def _floor(steering: type, keywords: Mapping[str, Any]
           ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """The raw keyword floor routed onto this run's bodies -> the carrier's values and each coupled module's, by identifier."""
    if keywords and not isinstance(keywords, Mapping):
        raise SlotRefused(
            f"keywords takes a mapping of the engine's own keyword names to "
            f"values, e.g. {{\"LAW OF BOTTOM FRICTION\": 4}}; got "
            f"{type(keywords).__name__}.")
    bodies = run_bodies(steering)
    own: dict[str, Any] = {}
    coupled: dict[str, dict[str, Any]] = {}
    for name, value in (keywords or {}).items():
        body, identifier = identify_on(bodies, name)
        if body is steering:
            own[identifier] = value
        else:
            coupled.setdefault(body.MODULE, {})[identifier] = value
    return own, coupled


def run_bodies(steering: type) -> list[type]:
    """This run's bodies in DECK ORDER: the carrier, then each module its coupling statement names.
    Read off the declaration, so the names a floor may qualify are known before any fill."""
    from trid3nt_server.workflows.telemac.modules import wrapper_for

    coupled = steering.ASSERTED.get("coupling") or ()
    return [steering] + [wrapper_for(body["module"]) for body in coupled
                         if isinstance(body, Mapping) and body.get("module")]


async def fill_sheet(*, steering: type, produced: Mapping[str, Any],
                     params: Mapping[str, Any],
                     settled: Mapping[str, Any] | None = None,
                     workflow: str, title: str, keywords: Mapping[str, Any],
                     input_mode: str | None, spent: Sequence[str] = (),
                     seat: Callable[[Mapping[str, Any]], None] | None = None
                     ) -> Sheet:
    """Set the body's slots against what the run measured -> the sheet, HELD.

    The raw ``keywords`` floor is filled last and therefore beats a template
    value. ``seat`` takes the param values the person proceeded on."""
    stated, coupled = _floor(steering, keywords)

    async def fill(values: Mapping[str, Any], edits: Mapping[str, Any]) -> Sheet:
        # A composite may read fetched data at the fill, so the fill runs off the loop.
        sheet = await asyncio.to_thread(
            fill_slots, steering, template=workflow, produced=dict(produced),
            params=dict(values), settled=settled, **{**stated, **edits})
        # After the fill: the coupled decks are what the carrier's coupling composite wrote into the sheet's files.
        return fill_coupled(sheet, coupled) if coupled else sheet

    sheet = await _review(fill, params, steering=steering, workflow=workflow,
                          title=title, input_mode=input_mode, spent=spent,
                          seat=seat)
    logger.info("telemac sheet filled: %s states %d keywords, %d open "
                "(%d required)", sheet.body.__name__, len(sheet.filled),
                len(sheet.open()), len(sheet.required()))
    return sheet


async def run_sheet(*, sheet: Sheet, settled: Mapping[str, Any],
                    results: Sequence[str], steering: str, prefix: str,
                    dispatch: str, cores: Any,
                    display: str = "") -> dict[str, Any]:
    """A complete sheet: serialize, stage, hand it to the box -> the run handle.
    The handle names every variable the deck asked for; what the run wrote is decided here, where the coupled bodies and declared tracers are in hand."""
    module, _, attribute = str(dispatch).rpartition(".")
    to_the_box = getattr(import_module(module), attribute)
    coupled = dict(sheet.resolved()).get("COUPLING WITH")
    outputs = [*results, *(row["dest"] for row in settled["mesh_inputs"]),
               steering, *_ALWAYS_READABLE, *sorted(sheet.files)]
    handle = await run_sheet_(
        sheet, dispatch=to_the_box, mesh_inputs=settled["mesh_inputs"],
        outputs=outputs, results=list(results), prefix=prefix,
        server_facts=settled["server_facts"], steering=steering,
        cores=cores,
        coupling=None if not coupled else str(coupled).split(";")[0].lower(),
        continue_from=settled.get("continue_from"))
    return {**settled, **handle, "module": sheet.module,
            "display_basename": display or None,
            "module_output": [{"token": token, "module": module,
                               "name": row.name, "unit": row.unit,
                               "style": row.style, "varies": row.varies,
                               "has_edge": bool(row.has_edge),
                               "injected": bool(row.injected)}
                              for token, module, row in sheet.published()],
            "tracer_names": dict(sheet.resolved()).get("NAMES OF TRACERS")}


#: How a slot's ORIGIN reads on the card: which door served the value and its basis. The row's name is the keyword's
#: identifier; an edit of it is another fill.
_ORIGIN_DOORS: Mapping[Origin, tuple[str, str]] = {
    Origin.TEMPLATE: ("scenario", "derived"),
    Origin.USER: ("user", "user"),
    Origin.MODEL: ("user", "user"),
    Origin.PRODUCER: ("derived", "derived"),
    Origin.DERIVED: ("derived", "derived"),
    Origin.CALIBRATED: ("derived", "derived"),
}


def card_rows(sheet: Sheet) -> list[ParamSheetRow]:
    """The sheet as the card renders it: what is SET, what the run WRITES, what
    is OPEN, then the rest.

    The rest is the whole module, folded under advanced with its engine default."""
    rows = _fetcher_rows() + _fetcher_rows() + [_slot_row(name, row)
                                               for name, row
                                               in sheet.filled.items()]
    rows += _coupled_rows(sheet)
    rows += _written_rows(sheet)
    rows += _serial_rows(sheet)
    rows += [_open_row(slot) for slot in sheet.required()]
    # The advanced fold reads down the dictionary's RUBRIQUES and, inside one, its own order. A keyword the sheet GENERATES
    # is already a written row above; the fold is engine DEFAULTS only.
    generated = {body.PRINTOUTS for body, _, _ in _decks(sheet)}
    rest = [slot for name, slot in sheet.body.MODULE_INPUT.items()
            if name not in sheet.filled and name not in generated
            and not slot.is_required]
    return rows + [_default_row(slot) for slot in
                   sorted(rest, key=lambda slot: _group(slot))]


def _fetcher_rows() -> list[ParamSheetRow]:
    """One row per DATA slot the match filled: the ranked list, pick highlighted.
    The card renders the list the model was given. Not editable: a source is superseded by supplying the slot."""
    from trid3nt_server.workflows.runtime.journal import run_choices

    return [ParamSheetRow(
        name=f"{choice.slot} source", value=choice.picked or "nothing matched",
        desc=f"Which source filled the {choice.slot} slot, matched on "
             f"{choice.need}.",
        door="scenario", basis="derived", origin="producer", editable=False,
        source_badge=(f"{len(choice.rows)} sources weighed"
                      if choice.tie else "matched on the rows"),
        note=choice.sentence, choices=choice, group="Sources")
        for choice in run_choices()]


def _fetcher_rows() -> list[ParamSheetRow]:
    """WHAT THE CUT COVERS, over the water and over the land, one row each.
    On the card so the unmeasured share is weighed BEFORE the solve. Not editable: an op or supplying the slot changes it."""
    from trid3nt_server.workflows.runtime.journal import run_coverage

    return [ParamSheetRow(
        name=f"coverage {index}", value=line[:200], desc=line[:512],
        door="scenario", basis="derived", origin="derived", editable=False,
        source_badge="measured on the grid the rows were merged at",
        group="Sources")
        for index, line in enumerate(run_coverage(), start=1)]


def _decks(sheet: Sheet) -> list[tuple[Any, list[str], Mapping[str, Any]]]:
    """Every body this run writes a deck for, its tracers, and what that deck states - the row a variable's condition is read against."""
    from trid3nt_server.workflows.telemac.modules import wrapper_for

    return ([(sheet.body, [row.name for row in sheet.tracers], sheet.stated())]
            + [(wrapper_for(body["module"]), [], dict(body.get("slots") or {}))
               for body in sheet.coupled])


def _coupled_rows(sheet: Sheet) -> list[ParamSheetRow]:
    """What each COUPLED deck of this run states, one group per deck in deck order.
    Read against that module's own dictionary, so the row carries its unit and bounds and the group names the body (two modules may spell one keyword).
    Not editable: the review's own filter answers for the carrier's sheet."""
    from trid3nt_server.workflows.telemac.modules import wrapper_for

    rows = []
    for body in sheet.coupled:
        module = body["module"]
        wrapper = wrapper_for(module)
        user = set(body.get("stated", ()))
        for identifier, value in body["slots"].items():
            # KEYWORDS ONLY: a coupled body holds composites unexpanded until the serializer fills it, and a composite has no unit or range.
            slot = wrapper.MODULE_INPUT.get(identifier)
            if slot is None or value is None:
                continue
            rows.append(ParamSheetRow(
                name=f"{module}.{identifier}",
                value=value if isinstance(value, (int, float, str, bool, list))
                else str(value),
                desc=slot.desc[:512],
                door="user" if identifier in user else "scenario",
                basis="user" if identifier in user else "derived",
                units=slot.unit or None, bounds=_editor_bounds(slot),
                editable=False, group=f"{module}: {_group(slot)}",
                source_badge=(f"stated on this run as {module}: {slot.keyword}"
                              if identifier in user
                              else f"the {module} deck this run couples")))
    return rows


def _written_rows(sheet: Sheet) -> list[ParamSheetRow]:
    """What each deck of this run WRITES, expanded, in the module's own order.
    Generated from the module's table, so the card states it rather than reading a slot nobody filled."""
    rows = []
    for body, tracers, stated in _decks(sheet):
        if not body.PRINTOUTS:
            continue
        slot = body.slot(body.PRINTOUTS)
        table = body.table(stated)
        rows.append(ParamSheetRow(
            name=f"{body.MODULE}.{slot.identifier}",
            value=[table[token].name for token in body.written(stated)] + tracers,
            desc=slot.desc[:512], door="scenario", basis="derived",
            editable=False, group=_group(slot),
            source_badge=f"the {body.MODULE} module's own variable table"))
    return rows


def _serial_rows(sheet: Sheet) -> list[ParamSheetRow]:
    """What a deck the engine cannot partition says about the run's sizing class.
    A body with no processor keyword runs on one core whatever class was asked, and the card says so."""
    return [ParamSheetRow(
        name=f"{body.MODULE}.cores", value="serial: this engine runs on one core",
        desc="How many cores this module's solve is partitioned across.",
        door="scenario", basis="derived", editable=False,
        source_badge="the module's own dictionary")
        for body, _tracers, _stated in _decks(sheet)
        if PROCESSORS not in body.MODULE_INPUT]


async def _review(fill: Callable[[Mapping[str, Any], Mapping[str, Any]], Any],
                  params: Mapping[str, Any], *, steering: type, workflow: str,
                  title: str, input_mode: str | None,
                  spent: Sequence[str],
                  seat: Callable[[Mapping[str, Any]], None] | None = None
                  ) -> Sheet:
    """Show the filled sheet and HOLD -> the sheet the person proceeded on.

    An edit re-fills its input and redraws the card, or refuses by name."""
    from trid3nt_server.inputs.gate.input_review import (
        PHYSICS_INPUT_REQUIRED,
        GateCard,
        gate_input_review,
        render_input_review_lines,
    )

    values, edits = dict(params), {}
    own = {name for name in _strings(steering.ASSERTED)
           if name in values} - set(spent)
    sheet = await fill(values, edits)

    def rows() -> list[ParamSheetRow]:
        return [_param_row(name, values[name]) for name in sorted(own)] \
            + card_rows(sheet)

    async def card() -> GateCard:
        drawn = rows()
        return GateCard(lines=render_input_review_lines(_entries(drawn)),
                        param_sheet=ParamSheet(
                            workflow=workflow, rows=drawn,
                            title=title or f"Review the {workflow} sheet"))

    async def revise(revised: Mapping[str, Any]) -> None:
        nonlocal sheet
        for name in revised:
            if name in params and name not in own:
                raise SlotRefused(
                    f"{name} is a param of {workflow} the card cannot re-fill: "
                    + ("another stage of this run reads it off the run itself"
                       if name in spent else "no keyword of this sheet reads it")
                    + f". State {name} on the run instead.")
            if name not in params:
                try:
                    accept(steering, name, revised[name])
                except SlotRefused as exc:
                    raise SlotRefused(
                        f"{name} is not an input of {workflow}: no param of it "
                        f"is named so, and {exc}") from exc
        for name, value in revised.items():
            (values if name in own else edits)[name] = value
        sheet = await fill(values, edits)

    outcome = await gate_input_review(
        tool_name=workflow, mode=input_mode, entries=_entries(rows()),
        params={}, present=card, apply_revision=revise)
    if not outcome.proceed:
        # A refusal keeps its reason's code; only a person's own cancel is the decline code every gate cancels under.
        raise TelemacError(
            outcome.cancel_reason or f"{workflow} was cancelled at the review.",
            error_code={"physics": PHYSICS_INPUT_REQUIRED, "no_session": "NO_SESSION"}
            .get(str(outcome.cancel_code), "USER_INPUT_CANCELLED"))
    if seat is not None:
        seat(values)
    return sheet


def _strings(value: Any) -> set[str]:
    """Every plain string a template's assertions carry, the params a keyword reads by name among them."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        return {s for item in value.values() for s in _strings(item)}
    if isinstance(value, (list, tuple)):
        return {s for item in value for s in _strings(item)}
    return set()


def _entries(rows: Sequence[ParamSheetRow]) -> list[SyntheticInput]:
    """The card's rows as provenance lines; a list value is narrated whole."""
    return [SyntheticInput(
                param=row.name, basis=row.basis, note=row.source_badge,
                value=(row.value if isinstance(row.value, (int, float, str, bool))
                       else "; ".join(str(v) for v in row.value)))
            for row in rows if row.value is not None and not row.advanced]


def _seat_on(env: Any) -> Callable[[Mapping[str, Any]], None]:
    """Seat the values a person proceeded on in the run's mapping, so every read
    of a param after the sheet - a chart's reference among them - reads the edit."""
    def seat(values: Mapping[str, Any]) -> None:
        edited = {name: value for name, value in values.items()
                  if name in env.params and value != env.params.value_of(name)}
        env.params = env.params.replacing({
            name: env.params.row(name).with_value(value, basis="user")
            for name, value in edited.items()})
        env.run.update(edited)
    return seat


def _param_row(name: str, value: Any) -> ParamSheetRow:
    """One template param a keyword of this sheet reads, editable by its name."""
    return ParamSheetRow(
        name=name, value=(value if isinstance(value, (int, float, str, bool, list))
                          or value is None else str(value)),
        desc=f"The template param {name}; an edit re-fills every keyword that "
             "reads it.",
        door="scenario", basis="derived", origin="template",
        source_badge="this run's template param", group="Template")


def _slot_row(name: str, row: Any) -> ParamSheetRow:
    """One SET slot, as the card renders it: the value, the unit it is read in,
    the range it is taken inside, and where it came from."""
    door, basis = _ORIGIN_DOORS[row.provenance.origin]
    value = row.value if isinstance(row.value, (int, float, str, bool, list)) \
        else str(row.value)
    return ParamSheetRow(
        name=name, value=value, desc=row.slot.desc[:512], door=door, basis=basis,
        units=row.slot.unit or None, bounds=_editor_bounds(row.slot),
        origin=row.provenance.origin.value, source_badge=str(row.provenance),
        group=_group(row.slot))


def _editor_bounds(slot: Any) -> tuple[float, float] | None:
    """The range a card clamps this keyword's editor to, or nothing at all.
    ONE pair bounds the whole value; a keyword with a pair per element has no single range, and the fill's refusal names the element."""
    return slot.bounds[0] if len(slot.bounds) == 1 else None


def _open_row(slot: Any) -> ParamSheetRow:
    """One OPEN MANDATORY slot: an empty the run cannot begin without."""
    return ParamSheetRow(
        name=slot.identifier, value=None, desc=slot.desc[:512], door="user",
        basis="derived", editable=True, group=_group(slot),
        source_badge="required: the dictionary marks this file OBLIG")


def _default_row(slot: Any) -> ParamSheetRow:
    """One slot this run leaves to the engine, under the advanced fold.

    Carries the value the engine will use rather than an empty."""
    # A slot the dictionary answers for carries a DEFAULT basis; one it answers for nobody carries the open-mandatory basis, as there is no default.
    return ParamSheetRow(
        name=slot.identifier,
        value=None if slot.is_open else slot.engine_default,
        desc=slot.desc[:512], door="scenario",
        basis="derived" if slot.is_open else "default_demo",
        editable=True, advanced=True, group=_group(slot),
        source_badge=("open: the dictionary gives it no default"
                      if slot.is_open else "engine default"))


def _group(slot: Any) -> str:
    """The dictionary's own top-level rubrique - what the advanced fold sorts by."""
    return slot.rubrique[0] if slot.rubrique else ""


class TelemacWorkflow(Workflow):
    """TELEMAC: the template module, read by its own names.
    A template DECLARES STEERING (and its coupling), DATA, PARAMS, OUTPUTS, CAPTIONS, DOC and its run files; this builds every stage off them.
    Nothing here restates a template, and a name a template does not state takes the default beside it."""

    engine = "telemac"
    solve_step = "solve"

    @classmethod
    def levers(cls) -> tuple[str, ...]:
        """Every runtime lever, because the stages that read them are this
        class's own and no template seats one on its own behalf."""
        from trid3nt_server.workflows.runtime.levers import LEVER_NAMES

        return LEVER_NAMES

    @property
    def steering(self) -> type:
        """The STEERING body: the module wrapper, the slots this question asserts
        and the coupling it carries."""
        return self.template.STEERING

    def accept_keyword(self, name: str, value: Any) -> tuple[str, Any, str]:
        """One keyword on whichever of this run's bodies it names, through that
        module's own accept rule."""
        body, identifier = identify_on(run_bodies(self.steering), name)
        return accept(body, identifier, value)

    def _states(self, name: str, default: Any) -> Any:
        """One declaration read off the template module, or the default beside it."""
        return getattr(self.template, name, default)

    def slot_units(self) -> Mapping[str, str]:
        """What the two series slots convert to: the unit of the PRESCRIBED list
        each one's value is written into, which the transform fixes."""
        from trid3nt_server.inputs.slots import DISCHARGE, LEVEL

        from .modules.telemac2d import PRESCRIBED_UNITS

        return {DISCHARGE: PRESCRIBED_UNITS["flowrate"],
                LEVEL: PRESCRIBED_UNITS["elevation"]}

    def published_units(self) -> Mapping[str, str]:
        """The unit each variable this run publishes is written in, by the name the result file carries.
        Three statements, all the deck's own: the module's variable table, the rows every coupled module appends,
        and the 32-character tracer text the deck writes its tracers as."""
        from .modules import wrapper_for
        from .modules.sheet import tracer_text

        published = {row.name: row.unit
                     for row in self.steering.MODULE_OUTPUT.values()}
        for body in getattr(self.steering, "coupling", ()) or ():
            appends = wrapper_for(body["module"]).APPENDS
            for row in (list(appends(body)) if appends is not None else []):
                published[row.name] = row.unit
        for declared in getattr(self.steering, "NAMES_OF_TRACERS", ()) or ():
            name, unit = tracer_text(declared)
            published[name] = unit
        return published

    def run_window_s(self, keywords: Mapping[str, Any]) -> float | None:
        """How long this run's solve covers, as THIS module spells it, under the floor that may have moved it.
        A matched series source filters on the number the deck will write."""
        return self.steering.seconds(
            stated(steering=self.steering, keywords=keywords))

    def check(self) -> None:
        """Refuse at import what this template cannot run on: a domain, one bed,
        the deck's own clock, a function for every measurement it asks for and
        a caption for every output it lists."""
        from trid3nt_server.inputs.slots import DISCHARGE

        slots = self._slots()
        self._clock()
        if DISCHARGE in slots:
            self._asserted(None, "LAW_OF_BOTTOM_FRICTION")
            self._asserted(None, "FRICTION_COEFFICIENT")
        for ask in self._declared(Measured):
            _signature(_MEASURES[ask.kind][0])
        self._outputs()

    def _slots(self) -> dict[str, str]:
        """The row this question declares for each role a stage reads."""
        from trid3nt_server.inputs.slots import BED, DOMAIN

        slots: dict[str, list[str]] = {}
        for row in self.data:
            if row.role:
                slots.setdefault(row.role, []).append(row.name)
        if not slots.get(DOMAIN):
            raise PlanValidationError(
                f"{self.name} declares no domain: a run solves over a polygon, so "
                "the DATA body needs a row named domain.")
        beds = slots.get(BED) or []
        if not beds:
            raise PlanValidationError(
                f"{self.name} declares no bed: every node carries an elevation, so "
                "the DATA body needs a row named bed.")
        if len(beds) > 1:
            raise PlanValidationError(
                f"{self.name} declares {len(beds)} bed rows ({beds}); the bed is "
                "ONE source. A survey over a wider surface is composed by the "
                "merge derive into the one row this slot takes.")
        return {role: names[0] for role, names in slots.items()}

    async def launch(self, state: Fill) -> RunResult:
        """The run, in order: the world, the mesh and its files, what the run places and measures, the settle, the sheet, the solve, the outputs.
        Every stage writes into the run's one mapping and reads off it by plain name."""
        from trid3nt_server.tools.mesh.step import build_declared_mesh, keep_mesh
        from trid3nt_server.render.pipeline_emitter import (begin_substeps,
                                                            current_emitter,
                                                            substep)
        from trid3nt_server.inputs.slots import DISCHARGE, DOMAIN, LEVEL
        from trid3nt_server.inputs.fill import (_load, _produce, call,
                                                           production, read)
        from .authoring.mesh_files import telemac_mesh_files
        from .authoring.opening import open_channel, open_water
        from .authoring.release_point import settle_release

        env = production(state)
        run = env.run
        slots = self._slots()
        domain = slots[DOMAIN]
        level, inflow = slots.get(LEVEL, ""), slots.get(DISCHARGE, "")
        keywords = dict(state.keywords)
        previous = _previous(self.steering, keywords)
        recipe = self._states("MESH", None)
        asks = self._declared(Measured)
        placed = self._declared(Placed)
        emitter = current_emitter()
        begin_substeps(emitter, 6 + len(asks) + len(placed) + bool(inflow))

        async def given(value: Any) -> Any:
            return await read(env, value) if isinstance(value, str) else value

        async def stage(label: str, fn: Any, /, **kwargs: Any) -> Any:
            async with substep(emitter, label):
                run[label] = await call(
                    fn if callable(fn) else _load(fn), kwargs, label)
            return run[label]

        async def measure(when: str) -> None:
            for ask in asks:
                runner, _stage, taken = _MEASURES[ask.kind]
                if taken == when:
                    world = await self._world(env, domain, _signature(runner))
                    await stage(_SETTLED if taken == "settle" else ask.name,
                                runner, **world,
                                **await _read_named(env, dict(ask.reads)),
                                **ask.asked)

        run.update(stated(steering=self.steering, keywords=keywords))
        await measure("world")
        mesh = recipe if recipe is not None else self._mesh(domain, slots)
        await stage("mesh", build_declared_mesh,
                    mesh=await self._recipe(env, mesh),
                    name=await read(env, self._states("MESH_ON", "") or domain),
                    supplied=await given(self._states("SUPPLIED_MESH", None)),
                    tool=self.name, input_mode=env.input_mode,
                    fresh=bool(state.carried.get("restart_clean")))
        # The files this engine asks the accepted mesh for are written from it before anything reads one: its boundary numbering says which face carries what.
        await stage("mesh_files", telemac_mesh_files, mesh=run["mesh"])
        await keep_mesh(run["mesh"])
        if inflow:
            # THE OPEN-CHANNEL ADDITION: whether a run CARRIES a discharge is a run-time fact, decided off the carrier handed in; an absent one authors no channel.
            await stage("channel", open_channel, mesh=run["mesh"],
                        files=run["mesh_files"], carrier=await read(env, inflow),
                        stage=await read(env, level) if level else None,
                        friction_law=self._asserted(run, "LAW_OF_BOTTOM_FRICTION"),
                        friction_coefficient=self._asserted(
                            run, "FRICTION_COEFFICIENT"))
        for mark in placed:
            await stage(mark.name, settle_release, point=await given(mark.point),
                        mesh=run["mesh"], domain=await read(env, domain),
                        fraction=await given(mark.fraction), label=mark.label,
                        **({"continue_from": previous} if mark.continues else {}))
        await measure("produce")
        settles = [ask for ask in asks if _MEASURES[ask.kind][2] == "settle"]
        if settles and previous:
            raise TelemacError(
                f"{self.name} is settled by the {settles[0].kind} it measures, "
                "which opens on no previous computation: this template does not "
                "carry a run on.", error_code="TELEMAC_CONTINUATION_REFUSED")
        if settles:
            await measure("settle")
        else:
            await stage(_SETTLED, open_water, **await self._opening(
                env, domain, level, inflow), continue_from=previous)
        await measure("derive")
        # A CONTEXT row's product is the SENTENCE, so one nothing read is asked once everything it could stand on is in hand;
        # a row a module fills a keyword from is read here, before the sheet.
        for row in self.data:
            if row.is_context and row.name not in run:
                run[row.name] = await _produce(env, row)
        for name in self._fills():
            await read(env, name)
        declared = {prm.name for prm in self.params}
        await stage("sheet", fill_sheet, steering=self.steering,
                    produced=run,
                    params={name: run[name] for name in declared},
                    settled=run[_SETTLED].get("keywords") or {},
                    workflow=self.name,
                    title=self._states("REVIEW_TITLE", ""), keywords=keywords,
                    input_mode=env.input_mode,
                    spent=sorted(({"cores"} & declared)
                                 | {mark.name for mark in placed}),
                    seat=_seat_on(env))
        listed = tuple(self._states("RESULTS", ()))
        solved = await stage(
            "solve", run_sheet, sheet=run["sheet"], settled=run[_SETTLED],
            results=list(listed or (self._result(),)),
            steering=(self._states("STEERING_FILE", "")
                      or f"{self.steering.MODULE}_{self.name}.cas"),
            prefix=self._states("PREFIX", "telemac"), dispatch=_DISPATCH,
            display=self._states("DISPLAY_FILE", ""),
            cores=run.get("cores") if "cores" in declared else None)
        await keep_mesh(run["mesh"], solved.get("run_id"))
        outputs, captions = self._outputs()
        # A primitive's point, line and band name run inputs; each is read here and rejoins its primitive at publish.
        anchors = [{"at": await given(p.at), "along": await given(p.along),
                    "within": await given(p.within)} for p in outputs]
        value = await stage(
            "outputs", publish_outputs, run=run["solve"],
            outputs=[_unanchored(p) for p in outputs], captions=captions,
            anchors=anchors, params={name: run[name] for name in declared})
        return RunResult(value=value, results=dict(run))

    def _declared(self, kind: type) -> tuple[Any, ...]:
        """Every placement or measurement the template module declares, in declared order."""
        return tuple(value for value in vars(self.template).values()
                     if isinstance(value, kind))

    def _fills(self) -> list[str]:
        """The declared rows a module of this run fills keywords from itself."""
        wanted = set(self.steering.FILLED_BY)
        for body in self.steering.ASSERTED.get("coupling") or ():
            if isinstance(body, Mapping):
                wanted |= set(body.get("filled_by", ()))
        return [row.name for row in self.data if row.name in wanted]

    def unnamed(self) -> tuple[str, ...]:
        """Every input the template names as a plain string - in a placement, a
        measurement, a row, an output, the mesh recipe or a composite - that no
        param, row, product or keyword of this run is called."""
        known = ({prm.name for prm in self.params} | {row.name for row in self.data}
                 | self._marks()
                 | {"mesh", "mesh_files", "channel", _SETTLED, "line"}
                 | {name for body in run_bodies(self.steering)
                    for name in body.MODULE_INPUT})
        return tuple(dict.fromkeys(name for name in self._named()
                                   if isinstance(name, str) and name not in known))

    def named_rows(self) -> tuple[str, ...]:
        """Every declared row the template names, but one asked near a point the
        run places: that point is a node of the mesh, so the row is produced
        once the point is placed, before the sheet."""
        rows, marks = {row.name: row for row in self.data}, tuple(self._marks())
        return tuple(dict.fromkeys(
            name for name in self._named() if isinstance(name, str)
            and name in rows and rows[name].coercion.get("near") not in marks))

    def _marks(self) -> set[str]:
        return ({mark.name for mark in self._declared(Placed)}
                | {ask.name for ask in self._declared(Measured)})

    def _named(self) -> list[Any]:
        from trid3nt_server.tools.mesh.recipe import input_names

        named = [mark.point for mark in self._declared(Placed)]
        named += [mark.fraction for mark in self._declared(Placed)]
        named += [name for ask in self._declared(Measured)
                  for name in _names(dict(ask.reads))]
        named += [row.coercion.get("near") for row in self.data]
        named += [value for p in self._states("OUTPUTS", ())
                  for value in (p.at, p.along, p.within)]
        recipe = self._states("MESH", None)
        if recipe is not None:
            named += input_names(recipe)
        named += self.steering.named()
        return named

    async def _recipe(self, env: Any, recipe: Any) -> dict[str, Any]:
        """The mesh ask with every input it names read off the run: the extent,
        a resolution stated as a name, and each op argument that takes one."""
        from trid3nt_server.tools.mesh.recipe import recipe_plan_value, takes_name
        from trid3nt_server.inputs.fill import read

        asked = recipe_plan_value(recipe)
        for key in ("extent", "resolution_m"):
            if isinstance(asked[key], str):
                asked[key] = await read(env, asked[key])
        for op, entry in zip(recipe.ops, asked["ops"]):
            entry["kwargs"] = {
                key: await read(env, value) if takes_name(recipe, op, key)
                else value for key, value in entry["kwargs"].items()}
        return asked

    def _mesh(self, domain: str, slots: Mapping[str, str]) -> Any:
        """The mesh this workflow asks for where the template declares none."""
        from trid3nt_server.tools.mesh.tool import mesh_op, tool
        from trid3nt_server.inputs.slots import BED

        return tool.build_mesh(
            mesher=_MESHER, kind=_MESH_KIND, extent=domain,
            resolution_m="mesh_resolution_m",
            # THE RIM IS THE ASK'S TO SIZE: no library sizing function measures the domain's outline, so an undeclared rim
            # comes back an order of magnitude past the size word; granularity is the user's.
            ops=[mesh_op("set_rim_size"), *_clean_ops(),
                 mesh_op("set_bed", source=slots[BED]),
                 # The runs ride on the DOMAIN: the producer measured them where it cut the polygon; a drawn outline carries the canvas's.
                 mesh_op("set_boundary_roles", runs=domain)])

    async def _opening(self, env: Any, domain: str, level: str,
                       inflow: str) -> dict[str, Any]:
        """What the open water is settled from, read off this run."""
        from trid3nt_server.inputs.fill import read

        run = env.run
        declared = {prm.name for prm in self.params}
        return {
            "mesh": run["mesh"], "files": run["mesh_files"],
            # WHAT THE WATER STANDS AT: the channel's own measurement, else the level slot, else nothing; a bed stated as a depth needs nothing.
            "level": (run["channel"] if inflow
                      else await read(env, level) if level else None),
            "geometry": self._file("GEOMETRY_FILE", "geometry.slf"),
            "boundary": self._file("BOUNDARY_CONDITIONS_FILE", "boundary.cli"),
            "result": self._result(),
            "mesh_resolution_m": run["mesh_resolution_m"],
            # THE CLOCK IS THE MODULE'S: the settle reads the seconds the deck was written for, not a lever restating them.
            "duration_s": self._clock(run),
            # A deck that STATES the depth an open edge is designated at prescribes the sea state across it, so a mesh where nothing reaches it is sealed.
            **({"deck": self.name,
                "open_depth_threshold_m": run["open_depth_threshold_m"]}
               if "open_depth_threshold_m" in declared else {}),
            **({"name": run["name"]} if "name" in declared else {})}

    def _result(self) -> str:
        """The deck's own RESULTS statement, else the first file this template
        says the run writes: a 3D deck names a 3D and a 2D result rather than
        one RESULTS FILE."""
        listed = tuple(self._states("RESULTS", ()))
        return self._file(self.steering.RESULT_KEYWORD,
                          listed[0] if listed else "results.slf")

    async def _world(self, env: Any, domain: str,
                     takes: frozenset[str]) -> dict[str, Any]:
        """What a measurement is taken against, read off this run, cut to what
        its function takes."""
        from trid3nt_server.inputs.fill import read

        run = env.run
        world: dict[str, Any] = {}
        for name in ("mesh", "files", "line", "domain", "settled"):
            if name not in takes:
                continue
            held = {"files": "mesh_files", "domain": domain}.get(name, name)
            world[name] = (run.get(held) if name in ("mesh", "files", "settled")
                           else await read(env, held))
        if "friction_law" in takes:
            world["friction_law"] = self._asserted(run, "LAW_OF_BOTTOM_FRICTION")
        return world

    def _clock(self, run: Mapping[str, Any] | None = None) -> Any:
        """How long the settle opens this run's water for, as the module spells it.
        One keyword where the module names the window, the step and count where it names those; the deck must state each (refused at import).
        A module that does not march in time spells none."""
        spelled = [self._asserted(run, keyword) for keyword in self.steering.CLOCK]
        return spelled[0] if len(spelled) == 1 else spelled or None

    def _asserted(self, run: Mapping[str, Any] | None, keyword: str) -> Any:
        """One value the DECK itself states, read at RUN time off the floor.
        The floor may move the number before the deck writes it, so the stage reads it rather than being built from it; the deck must state it (refused at import)."""
        if self.steering.ASSERTED.get(keyword) is None:
            raise PlanValidationError(
                f"the workflow settles this question's run on the {keyword} its "
                f"deck states, and this deck states none.")
        return None if run is None else run[keyword]

    def _file(self, keyword: str, fallback: str) -> str:
        """One file the deck itself names, read off the body that names it.
        The deck's GEOMETRY / BOUNDARY CONDITIONS / RESULTS statements ARE the run directory's names; restating them would let the two drift."""
        named = self.steering.ASSERTED.get(keyword)
        return str(named) if isinstance(named, str) and named else fallback

    def sheet_doc(self) -> str:
        """The ENGINE SURFACE line of this template's docstring.

        Read off the declaration itself, never claimed by prose."""
        body = self.steering
        asserts = set(body.ASSERTED)
        touched = sorted({body.MODULE_INPUT[name].rubrique[0] for name in asserts
                          if name in body.MODULE_INPUT
                          and body.MODULE_INPUT[name].rubrique})
        open_required = sorted(slot.keyword for name, slot in body.MODULE_INPUT.items()
                               if slot.is_required and name not in asserts)
        return (
            f"Sheet: {body.MODULE}, whose dictionary has {len(body.MODULE_INPUT)} "
            f"keywords. This template states {len(asserts)} of them, under "
            f"{', '.join(touched)}. Open mandatory slots: "
            f"{', '.join(open_required) if open_required else 'none'}. Every "
            f"other keyword is the engine's own default and is set on the call - "
            f"keywords={{\"LAW OF BOTTOM FRICTION\": 4}} - after "
            f"describe_keywords(module=\"{body.MODULE}\", query=...) names it "
            f"with its help, its choices and that default.\n"
            f"Stated by this deck (override any of them by name): "
            f"{self._stated_keywords()}.")

    def _stated_keywords(self) -> str:
        """The deck's own opinions, keyword by keyword, in dictionary order.
        Generated off ASSERTED so the doc cannot claim an opinion the deck does not hold; a value the run MEASURES is named as measured."""
        body = self.steering
        rows = []
        for name, slot in body.MODULE_INPUT.items():
            if name not in body.ASSERTED:
                continue
            rows.append(f"{slot.keyword} = {_stated_value(body.ASSERTED[name])}")
        return "; ".join(rows) if rows else "nothing"

    def _outputs(self) -> tuple[tuple[Primitive, ...], dict[str, str]]:
        """What this question PUBLISHES and what it calls each, checked: every
        listed read is published and has its caption."""
        outputs = tuple(self._states("OUTPUTS", ()))
        captions = dict(self._states("CAPTIONS", {}))
        for primitive in outputs:
            if primitive.publish is None:
                raise PlanValidationError(
                    f"OUTPUTS lists {primitive.kind}({primitive.variable!r}) with "
                    "no .layer(), .chart(), .animate() or .note(); a listed "
                    "primitive is published.")
            named = primitive.variable or primitive.kind
            if named not in captions:
                raise PlanValidationError(
                    f"OUTPUTS publishes {named!r} and CAPTIONS names no caption "
                    "for it.")
        return outputs, captions


def _previous(steering: type, keywords: dict[str, Any]) -> str | None:
    """The module's previous-computation file, taken off the keyword floor.
    The settle stages it and the sheet names the staged copy, so the floor never writes the caller's uri into the deck."""
    for name in list(keywords):
        body, identifier = identify_on(run_bodies(steering), name)
        if body is steering and identifier == CONTINUATION:
            return str(keywords.pop(name))
    return None


def _signature(runner: str) -> frozenset[str]:
    """The keyword names one measurement runner takes, by ONE attribute lookup.
    A name the module does not carry refuses here rather than at the call."""
    from inspect import signature

    module, _, name = runner.rpartition(".")
    op = getattr(import_module(module), name, None)
    if not callable(op):
        raise PlanValidationError(
            f"{module} carries no {name!r} that takes a measurement; a "
            "measurement names the runner that takes it.")
    return frozenset(signature(op).parameters)
