"""The TELEMAC workflow: fill, run, then read.

``fill`` sets slots, expands composites and binds producers; it is repeatable and
decides nothing. ``run`` serializes the complete sheet, stages the run directory
and hands it to the box; it is explicit and consequential. ``publish_outputs``
reads the primitives a template listed off the solved run and publishes each
the way it asked."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from importlib import import_module
from typing import Any, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.execution import AnswerLayerURI
from trid3nt_contracts.payload_warning import ParamSheet, ParamSheetRow

from trid3nt_server.render.formats import publish
from trid3nt_server.workflows.runtime import (
    ParamRef,
    PlanValidationError,
    Continued,
    RawKeywords,
    Ref,
    RunMode,
    Step,
    Workflow,
)
from trid3nt_server.workflows.runtime.plan import declared_reads
from trid3nt_server.workflows.mesh.step import MeshStep
from trid3nt_server.workflows.telemac.errors import TelemacError
from trid3nt_server.workflows.telemac.modules import wrapper_for
from trid3nt_server.workflows.telemac.modules.module import SlotRefused, identify_on
from trid3nt_server.workflows.telemac.modules.outputs import (
    NOT_ASKED,
    NOT_READ,
    Line,
    Measure,
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
)
from trid3nt_server.workflows.telemac.modules.sheet import fill as fill_slots
from trid3nt_server.workflows.telemac.modules.sheet import fill_coupled, late_bound
from trid3nt_server.workflows.telemac.modules.sheet import run as run_sheet_

logger = logging.getLogger("trid3nt_server.workflows.telemac.workflow")

__all__ = ["Measured", "Placed", "TelemacWorkflow", "card_rows",
           "fill_sheet", "publish_outputs", "run_bodies", "run_sheet", "stated"]

_TELEMAC = "trid3nt_server.workflows.telemac"

#: What a reader may open on any run, past the files the engine is required to
#: write: the mesh it solved on, the deck it read, the listing and the metrics.
_ALWAYS_READABLE = ("full_listing.log", "telemac_metrics.json")

#: What a row calls the calendar day it asks a dated source over.
_READING_DAY = "reading_day"

#: What the workflow meshes a domain with, and the element it lays, where the
#: template declares no recipe of its own.
_MESHER, _MESH_KIND = "om2d", "unstructured_tri"

#: The dispatch a staged run goes to, by the path it is resolved at CALL time.
_DISPATCH = f"{_TELEMAC}.engine.solve_case"

#: The mesher's own clean passes, under its own names, that every domain gets
#: before anything is imposed on it. They change the TOPOLOGY, so they run ahead
#: of the bed and the roles - a renumbering after a primitive painted node values
#: is refused by the mesher itself. The smoothing pass is NOT among them: it folds
#: elements beside a rim locked at one spacing, and the boundary walk that numbers
#: a TELEMAC geometry meets the folded pair's unpaired edges as a second rim.
def _clean_ops() -> list[Any]:
    from trid3nt_server.workflows.mesh.tool import mesh_op

    return [mesh_op("delete_boundary_faces"),
            mesh_op("delete_faces_connected_to_one_face"),
            mesh_op("make_mesh_boundaries_traversable"),
            mesh_op("fix_mesh", delete_unused=True)]


@dataclass(frozen=True, slots=True)
class Placed(Ref):
    """A point the run SETTLES onto a node of the accepted mesh, read as a ref.

    A reference first: whoever reads the placement - a source composite, a
    weather record, a chart's anchor - writes this where it would write
    ``Ref(name)``, and reads ``name.at``/``name.lon`` off it afterwards. What
    rides beside the name is what settling it takes, so the stage that settles
    it is the workflow's to build and no template names a runner."""

    #: The point the user gave, or nothing - in which case the point sits
    #: ``fraction`` along the domain's own centerline.
    point: Any = None
    fraction: Any = 0.5
    #: What the marker published before the solve is called on the map.
    label: str = "Release point"
    #: Read the initial wet state off the run this one carries on from: a source
    #: has to enter water, and a continued run opens at another run's surface.
    continues: bool = False


#: The measurements the workflow takes of this question's world, by the KIND a
#: composite asks for: the runner that takes it, the stage it is taken at and
#: WHEN - before the world is meshed, before the run is settled, as the settle
#: itself, or against the settled run.
_MEASURES: Mapping[str, tuple[str, str, str]] = MappingProxyType({
    "footprint": ("trid3nt_server.inputs.structure.structure", "prep", "world"),
    "rating": (f"{_TELEMAC}.authoring.assembler.settle_outlet_rating",
               "author", "produce"),
    "harbour": (f"{_TELEMAC}.authoring.assembler.settle_harbour",
                "author", "settle"),
    "dredge": (f"{_TELEMAC}.authoring.assembler.settle_dredge",
               "author", "derive")})


@dataclass(frozen=True, slots=True)
class Measured(Ref):
    """A measurement the run takes against its own world, read as the ref it is.

    A reference first, like a placement: whoever asks for the measurement - a
    dredger, an outlet's rating curve - writes this where it would write
    ``Ref(name)``. ``kind`` says which measurement, and ``asked`` is what only
    this question can state; the mesh, the line, the domain and the settled run
    are the workflow's and are never restated here."""

    kind: str = ""
    asked: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Ref.__post_init__(self)
        if self.kind not in _MEASURES:
            raise PlanValidationError(
                f"{self.path}: there is no {self.kind!r} measurement; the "
                f"workflow takes {tuple(_MEASURES)}.")
        object.__setattr__(self, "asked", MappingProxyType(dict(self.asked)))


def _painted(row: Mapping[str, Any]) -> list[Primitive]:
    """One table row -> the ONE layer the run publishes of it: the temporal layer
    where the row varies in time, the final frame where it does not.

    A still beside a time series is a copy of a frame the temporal layer already
    carries; a picture of one instant is a render of that layer."""
    from trid3nt_server.workflows.telemac.modules.outputs import field

    token, module, style = row["token"], row["module"], row.get("style")
    if row.get("varies"):
        return [field(token, t="every", module=module).animate(style=style)]
    return [field(token, t=-1, module=module).layer(style=style)]


#: How long a stated value is printed before the doc names its shape instead: a
#: whole tracer array spelled out crowds the keywords around it off the page.
_VALUE_CHARS = 48


def _stated_value(value: Any) -> str:
    """One asserted value, as the docstring prints it."""
    from trid3nt_server.workflows.runtime.plan import _Placeholder

    if value is None:
        return "nothing (the engine's default stands)"
    if isinstance(value, (Ref, _Placeholder)):
        return "measured"
    if isinstance(value, (list, tuple)):
        text = ", ".join(_stated_value(item) for item in value)
        return f"[{text}]" if len(text) <= _VALUE_CHARS else f"{len(value)} values"
    return f"{value:g}" if isinstance(value, float) else str(value)


def _unanchored(primitive: Primitive) -> Primitive:
    return replace(primitive, at=None, along=None, within=None, over=None,
                   above=None)


def _anchored(primitive: Primitive, anchor: Mapping[str, Any]) -> Primitive:
    return replace(primitive, at=anchor["at"], along=anchor["along"],
                   within=anchor.get("within"), over=anchor.get("over"),
                   above=anchor.get("above"))


async def publish_outputs(*, run: Mapping[str, Any], outputs: Sequence[Primitive],
                          captions: Mapping[str, str],
                          answer: Mapping[str, Measure],
                          params: Mapping[str, Any],
                          anchors: Sequence[Mapping[str, Any]] = (),
                          against: Mapping[str, Any] | None = None
                          ) -> AnswerLayerURI:
    """Read what the run wrote off it, publish every variable, answer.

    The module's TABLE is the outputs list: every row of the host's and of each
    coupled module's, styled from the row and published as ONE layer - the
    temporal one where the row varies in time, the final frame where it does
    not; a row the result does not carry is skipped. The template's own list is the reads it PLACED beside them. Each
    module's result is read ONCE; a coupled module's own file goes through its
    own wrapper. A chart's reference is a callable computing lines beside the
    read, or another primitive read where the chart's own is anchored and drawn
    as a line. A placed read the result lacks refuses; an answer over one is
    ``None``."""
    table = [p for row in (run.get("module_output") or ())
             for p in _painted(row)]
    listed = [*outputs, *(m.primitive for m in answer.values())]
    if anchors:
        listed = [_anchored(p, a) for p, a in zip(listed, anchors)]
    outputs = listed[:len(outputs)]
    answer = {name: replace(m, primitive=p, against=(against or {}).get(name))
              for (name, m), p in zip(answer.items(), listed[len(outputs):])}
    solved: dict[str, Solved] = {}
    published_keys = {primitive.key for primitive in outputs}
    empty: dict[Primitive, str] = {}

    def _read(key: Primitive) -> Any:
        module = key.module or str(run["module"])
        if module not in solved:
            solved[module] = Solved(run, wrapper_for(module))
        try:
            return solved[module].body.READS[key.kind](key, solved[module])
        except OutputEmpty as exc:
            # A run that carried nothing a measure could read answers with the
            # REASON it carried nothing, which the delivery refuses; a published
            # output that is missing is a refusal here.
            if key in published_keys:
                raise
            empty[key] = str(exc)
            return None

    def _beside(primitive: Primitive) -> Primitive | None:
        reference = primitive.reference
        if not isinstance(reference, Primitive):
            return None
        return replace(reference, at=primitive.at, along=primitive.along).key

    wanted = {primitive.key for primitive in (*table, *outputs)}
    wanted |= {measure.primitive for measure in answer.values()}
    # A measure held against ANOTHER measure reads that one too; its primitive
    # carries no anchor, so it needs no place resolved for it.
    wanted |= {m.against.primitive for m in answer.values()
               if isinstance(m.against, Measure)}
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

    items = await asyncio.to_thread(
        lambda: [_delivered(primitive, reads[primitive.key].name.strip().lower())
                 for primitive in table if reads[primitive.key] is not None]
        + [_delivered(primitive, captions.get(primitive.variable or primitive.kind,
                                              primitive.kind))
           for primitive in outputs])
    published = await publish(run_id=str(run["run_id"]), engine="telemac",
                              name=name, items=items)

    def _answered(measure: Measure) -> Any:
        if measure.primitive in empty:
            if measure.unasked and measure.against is None:
                return f"{NOT_ASKED}{measure.unasked}"
            return f"{NOT_READ}{empty[measure.primitive]}"
        against = measure.against
        if isinstance(against, Measure):
            if against.primitive in empty:
                return f"{NOT_READ}{empty[against.primitive]}"
            other = reads[against.primitive]
            against = None if other is None else other.measures.get(against.stat)
        read = reads[measure.primitive]
        return measure.answer(
            None if read is None else read.measures.get(measure.stat), against)

    answered = {name: _answered(measure) for name, measure in answer.items()}
    if not published.layers:
        raise SlotRefused(
            f"the {run['module']} run wrote none of the variables its module "
            "rows, so it published nothing and there is nothing to paint.")
    logger.info("telemac outputs published run_id=%s layers=%d charts=%d answer=%s",
                run["run_id"], len(published.layers), len(published.charts),
                answered)
    host = str(run["module"])
    if host not in solved:
        solved[host] = Solved(run, wrapper_for(host))
    return await asyncio.to_thread(_record, solved[host], name=name,
                                   answer=answered)


def _record(solved: Solved, *, name: str,
            answer: Mapping[str, Any]) -> AnswerLayerURI:
    """The run's own record: the mesh every published group rides, and the answer.

    It binds no group and ranks none of the rows the run published, so it is
    DRAWN rather than measured; its extent is what the camera frames."""
    from trid3nt_server import storage

    return AnswerLayerURI(
        layer_id=f"telemac-{solved.run_id}", name=name, layer_type="mesh",
        uri=f"s3://{storage.runs_bucket()}/{solved.run_id}/{solved.display_file}",
        style={"kind": "reference"}, bbox=solved.bbox,
        crs_authid=f"EPSG:{solved.utm_epsg}",
        reference_time=solved.run.get("started_at"), answer=dict(answer))


def stated(*, steering: type, keywords: Mapping[str, Any]) -> dict[str, Any]:
    """The run's own keyword values by identifier, resolved ONCE before any stage.

    The deck's assertions under the floor that overrides them, so a stage that
    runs before the sheet exists reads the value the deck will write. An
    assertion still holding a late-bound read has no number yet and is absent.
    This is the floor BEFORE the fill; ``Ref("sheet.<KEYWORD>")`` is the filled
    sheet after it, and only that one follows an edit made at the gate."""
    bodies = run_bodies(steering)
    out = {name: value for name, value in steering.ASSERTED.items()
           if value is not None and not late_bound(value)}
    for name, value in (keywords or {}).items():
        body, identifier = identify_on(bodies, name)
        if body is steering:
            out[identifier] = value
    return out


def run_bodies(steering: type) -> list[type]:
    """This run's bodies in DECK ORDER: the carrier, then each module its own
    coupling statement names.

    Read off the declaration, so the names a floor may qualify are known before
    any fill has run."""
    from trid3nt_server.workflows.telemac.modules import wrapper_for

    coupled = steering.ASSERTED.get("coupling") or ()
    return [steering] + [wrapper_for(body["module"]) for body in coupled
                         if isinstance(body, Mapping) and body.get("module")]


async def fill_sheet(*, steering: type, produced: Mapping[str, Any],
                     params: Mapping[str, Any],
                     workflow: str, title: str, keywords: Mapping[str, Any],
                     input_mode: str | None) -> Sheet:
    """Set the body's slots against what the run measured -> the sheet, HELD.

    The raw ``keywords`` floor is filled last and therefore beats a template value."""
    stated = {}
    if keywords and not isinstance(keywords, Mapping):
        # A floor that arrived as anything but a mapping is a caller error worth
        # naming: the alternative is an attribute error from inside the fill,
        # blaming a step rather than the argument.
        raise SlotRefused(
            f"keywords takes a mapping of the engine's own keyword names to "
            f"values, e.g. {{\"LAW OF BOTTOM FRICTION\": 4}}; got "
            f"{type(keywords).__name__}.")
    bodies = run_bodies(steering)
    coupled: dict[str, dict[str, Any]] = {}
    for name, value in (keywords or {}).items():
        body, identifier = identify_on(bodies, name)
        if body is steering:
            stated[identifier] = value
        else:
            coupled.setdefault(body.MODULE, {})[identifier] = value
    # A composite may read fetched data at the fill - a raster sampled at the
    # mesh's nodes - so the fill runs off the loop.
    sheet = await asyncio.to_thread(
        fill_slots, steering, template=workflow, produced=dict(produced),
        params=dict(params), **stated)
    if coupled:
        # After the fill, because the coupled decks are what the carrier's own
        # coupling composite wrote into the sheet's files.
        sheet = fill_coupled(sheet, coupled)
    revised = await _review(sheet, workflow=workflow, title=title,
                            input_mode=input_mode)
    if revised:
        sheet = await asyncio.to_thread(
            fill_slots, sheet, produced=dict(produced), params=dict(params),
            **revised)
    logger.info("telemac sheet filled: %s states %d keywords, %d open "
                "(%d required)", sheet.body.__name__, len(sheet.filled),
                len(sheet.open()), len(sheet.required()))
    return sheet


async def run_sheet(*, sheet: Sheet, settled: Mapping[str, Any],
                    results: Sequence[str], steering: str, prefix: str,
                    dispatch: str, compute_class: Any,
                    display: str = "") -> dict[str, Any]:
    """A complete sheet: serialize, stage, hand it to the box -> the run handle.

    Nothing is listed as readable that the run does not carry. The handle names
    every variable the deck asked for, because what the run wrote is decided
    here - where the coupled bodies and the declared tracers are both in hand -
    and not again where it is read."""
    module, _, attribute = str(dispatch).rpartition(".")
    to_the_box = getattr(import_module(module), attribute)
    coupled = dict(sheet.resolved()).get("COUPLING WITH")
    outputs = [*results, *(row["dest"] for row in settled["mesh_inputs"]),
               steering, *_ALWAYS_READABLE, *sorted(sheet.files)]
    handle = await run_sheet_(
        sheet, dispatch=to_the_box, mesh_inputs=settled["mesh_inputs"],
        outputs=outputs, results=list(results), prefix=prefix,
        server_facts=settled["server_facts"], steering=steering,
        compute_class=compute_class,
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


#: How a slot's ORIGIN reads on the card: which door served the value, and what
#: the basis beside it says. The card is a view of the sheet, so the row's name
#: is the keyword's own identifier and an edit of it is another fill.
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
    rows = _source_rows() + [_slot_row(name, row)
                             for name, row in sheet.filled.items()]
    rows += _coupled_rows(sheet)
    rows += _written_rows(sheet)
    rows += _serial_rows(sheet)
    rows += [_open_row(slot) for slot in sheet.required()]
    # The advanced fold reads down the dictionary's own RUBRIQUES, and inside one
    # down the dictionary's own order - the sections the engine's documentation
    # is written in, rather than a flat thousand-row list. A keyword the sheet
    # GENERATES is already a written row above; the fold is engine DEFAULTS, and
    # a generated value is not one.
    generated = {body.PRINTOUTS for body, _, _ in _decks(sheet)}
    rest = [slot for name, slot in sheet.body.MODULE_INPUT.items()
            if name not in sheet.filled and name not in generated
            and not slot.is_required]
    return rows + [_default_row(slot) for slot in
                   sorted(rest, key=lambda slot: _group(slot))]


def _source_rows() -> list[ParamSheetRow]:
    """One row per DATA slot the match filled: the ranked list, pick highlighted.

    The card renders the list the model was given, so the two cannot describe
    one run differently. Not editable here - a source is superseded by supplying
    the slot, which is a different door."""
    from trid3nt_server.workflows.runtime.journal import run_choices

    return [ParamSheetRow(
        name=f"{choice.slot} source", value=choice.picked or "nothing matched",
        desc=f"Which source filled the {choice.slot} slot, matched on "
             f"{choice.need}.",
        door="scenario", basis="derived", origin="producer", editable=False,
        source_badge=(f"{len(choice.rows)} sources weighed"
                      if choice.tie else "matched on the coverage rows"),
        note=choice.sentence, choices=choice, group="Sources")
        for choice in run_choices()]


def _decks(sheet: Sheet) -> list[tuple[Any, list[str], Mapping[str, Any]]]:
    """Every body this run writes a deck for, the tracers each carries, and what
    that deck states - the row a variable's own condition is read against."""
    from trid3nt_server.workflows.telemac.modules import wrapper_for

    return ([(sheet.body, [row.name for row in sheet.tracers], sheet.stated())]
            + [(wrapper_for(body["module"]), [], dict(body.get("slots") or {}))
               for body in sheet.coupled])


def _coupled_rows(sheet: Sheet) -> list[ParamSheetRow]:
    """What each COUPLED deck of this run states, one group per deck in deck order.

    The value is read against that module's own dictionary, so the row carries
    the unit and the bounds the coupled keyword is taken in, and the group names
    the body - two modules may spell one keyword and mean different numbers.
    Not editable: the review's own filter answers for the carrier's sheet."""
    from trid3nt_server.workflows.telemac.modules import wrapper_for

    rows = []
    for body in sheet.coupled:
        module = body["module"]
        wrapper = wrapper_for(module)
        user = set(body.get("stated", ()))
        for identifier, value in body["slots"].items():
            # KEYWORDS ONLY: a coupled body holds its composites unexpanded
            # until the serializer fills it, and a composite has neither the
            # unit nor the range a card row is rendered with.
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

    The keyword is generated from the module's table, so the card states it here
    rather than reading it off a slot nobody filled."""
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

    A body that spells no processor keyword runs on one core whatever class was
    asked for, and the card says so rather than leaving the lever looking like
    it did something."""
    return [ParamSheetRow(
        name=f"{body.MODULE}.cores", value="serial: this engine runs on one core",
        desc="How many cores this module's solve is partitioned across.",
        door="scenario", basis="derived", editable=False,
        source_badge="the module's own dictionary")
        for body, _tracers, _stated in _decks(sheet)
        if PROCESSORS not in body.MODULE_INPUT]


async def _review(sheet: Sheet, *, workflow: str, title: str,
                  input_mode: str | None) -> dict[str, Any]:
    """Show the filled sheet and HOLD -> the slot edits the user submitted.

    Submitting an edited sheet IS the approval; in ``auto`` nothing waits."""
    from trid3nt_server.gates.input_review import gate_input_review

    rows = card_rows(sheet)
    # A provenance row carries ONE value, so a keyword whose value is a list is
    # narrated as the list it is rather than dropped.
    entries = [SyntheticInput(
                   param=row.name, basis=row.basis, note=row.source_badge,
                   value=(row.value if isinstance(row.value, (int, float, str, bool))
                          else "; ".join(str(v) for v in row.value)))
               for row in rows if row.value is not None and not row.advanced]
    outcome = await gate_input_review(
        tool_name=workflow, mode=input_mode, entries=entries, params={},
        param_sheet=ParamSheet(workflow=workflow,
                               title=title or f"Review the {workflow} sheet",
                               rows=rows))
    if not outcome.proceed:
        # A DECLINED review is the user's answer, not a defect in the sheet, so
        # it carries the cancel code every gate in the tree refuses under.
        raise TelemacError(
            outcome.cancel_reason or f"{workflow} was cancelled at the review.",
            error_code="USER_INPUT_CANCELLED")
    return {name: value for name, value in outcome.params.items()
            if name in sheet.body.MODULE_INPUT or name in sheet.body.COMPOSITES}


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

    ONE pair bounds the whole value; a keyword the sidecar rows a pair per
    element for - a speed beside a bearing - has no single range an editor
    could clamp to, and the refusal at the fill names the element that missed."""
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
    # A slot the dictionary answers for carries a DEFAULT basis; one it answers
    # for nobody carries the same basis the open mandatory rows do, because there
    # is no default there to call one.
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

    A template DECLARES - STEERING and the coupling on it, DATA, PARAMS, OUTPUTS,
    CAPTIONS, ANSWER, DOC, and the run's own files beside them - and this builds
    every stage off those declarations: the world, the mesh, the placements, the
    settle, the fill, the solve and the publish. Nothing here restates a
    template, and a name a template does not state takes the default beside it."""

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

    def _states(self, name: str, default: Any) -> Any:
        """One declaration read off the template module, or the default beside it."""
        return getattr(self.template, name, default)

    def run_window_s(self, keywords: Mapping[str, Any]) -> float | None:
        """How long this run's solve covers: the deck's own DURATION, under the
        floor that may have moved it.

        A matched series source has to hold a record over it, so the number the
        deck will write is the number the match filters on."""
        window = stated(steering=self.steering, keywords=keywords).get("DURATION")
        return float(window) if isinstance(window, (int, float)) else None

    def steps(self) -> list[Any]:
        """The step sequence: the world, then fill, then run, then the outputs.

        Every stage is built off the slots this question declares - the domain it
        solves over, the bed under it, the runs its edge names, the flow an
        inflow carries - so a template states only what differs from that."""
        from trid3nt_server.workflows.mesh.tool import mesh_op, tool
        from trid3nt_server.workflows.runtime.data import (
            BED, DISCHARGE, DOMAIN, LEVEL, RUNS)
        from trid3nt_server.workflows.runtime.plan import DataRef

        slots: dict[str, list[str]] = {}
        for row in self.data:
            if row.role:
                slots.setdefault(row.role, []).append(row.name)
        domain = (slots.get(DOMAIN) or [""])[0]
        if not domain:
            raise PlanValidationError(
                f"{self.name} declares no domain: a run solves over a polygon, so "
                "the DATA body needs one row written Data.domain(...).")
        beds = slots.get(BED) or []
        if not beds:
            raise PlanValidationError(
                f"{self.name} declares no bed: every node carries an elevation, so "
                "the DATA body needs a row written Data.bed(...).")
        if len(beds) > 1:
            raise PlanValidationError(
                f"{self.name} declares {len(beds)} bed rows ({beds}); the bed is "
                "ONE source. A survey over a wider surface is composed by the "
                "merge derive into the one row this slot takes.")
        geometry = self._file("GEOMETRY_FILE", "geometry.slf")
        boundary = self._file("BOUNDARY_CONDITIONS_FILE", "boundary.cli")
        # The deck's own RESULTS statement, else the first file this template
        # says the run has to write: a 3D deck names a 3D and a 2D result rather
        # than one RESULTS FILE, and the run's own facts name what it wrote.
        listed = tuple(self._states("RESULTS", ()))
        result = self._file("RESULTS_FILE",
                            listed[0] if listed else "results.slf")
        declared = {prm.name for prm in self.params}
        level = (slots.get(LEVEL) or [""])[0]
        # THE OPEN-CHANNEL ADDITION, listed wherever this question declares a
        # discharge at all: whether a run CARRIES one is a run-time fact, so the
        # author decides it off the carrier it is handed. An absent carrier
        # authors no channel and the body opens on its level boundaries, which is
        # what the base settles on its own.
        inflow = (slots.get(DISCHARGE) or [""])[0]
        channel = (
            (Step(runner=f"{_TELEMAC}.authoring.assembler.open_channel",
                  stage="author",
                  kwargs={"mesh": Ref("mesh"),
                          "carrier": DataRef(inflow),
                          "stage": DataRef(level) if level else None,
                          "friction_law": self._asserted("LAW_OF_BOTTOM_FRICTION"),
                          "friction_coefficient":
                              self._asserted("FRICTION_COEFFICIENT")}
                  ).named("channel"),)
            if inflow else ())
        recipe = self._states("MESH", None)
        mesh = recipe if recipe is not None else tool.build_mesh(
            mesher=_MESHER, kind=_MESH_KIND, extent=DataRef(domain),
            resolution_m=ParamRef("mesh_resolution_m"),
            # THE RIM IS THE ASK'S TO SIZE, and every domain is cut from a
            # shoreline now: no sizing function the library has measures the
            # domain's own outline, so an undeclared rim comes back an order of
            # magnitude past the size word and the granularity lever is the
            # user's. No edge is stated, so the rim takes the recipe's own size
            # word.
            ops=[mesh_op("set_rim_size"), *_clean_ops(),
                 mesh_op("set_bed", source=DataRef(beds[0])),
                 # The runs come from wherever they were stated: the row the
                 # user fills, or the domain's own producer, which measured the
                 # edge it cut the polygon between.
                 mesh_op("set_boundary_roles",
                         runs=DataRef((slots.get(RUNS) or [domain])[0]))])
        settle = self._settle(recipe, domain) or Step(
            runner=f"{_TELEMAC}.authoring.assembler.open_water",
            stage="author",
            kwargs={"mesh": Ref("mesh"),
                    # WHAT THE WATER STANDS AT: the channel's own measurement
                    # where this question has one, else the level slot, else
                    # nothing - and a bed stated as a depth needs nothing.
                    "level": (Ref("channel") if channel
                              else DataRef(level) if level else None),
                    "geometry": geometry, "boundary": boundary,
                    "result": result,
                    "mesh_resolution_m": ParamRef("mesh_resolution_m"),
                    # THE CLOCK IS THE DECK'S: DURATION is a keyword the module
                    # carries, so the settle reads the seconds the deck was
                    # written for rather than a lever restating it.
                    "duration_s": self._asserted("DURATION"),
                    # WHICH RUN THIS ONE CARRIES ON FROM: a rerun ledger row,
                    # not a value the question asks about.
                    "continue_from": Continued,
                    **({"name": ParamRef("name")}
                       if "name" in declared else {})})
        produce = (self._reading_day() + channel + self._placements(domain)
                   + self._measurements(recipe, domain, when="produce"))
        derive = self._measurements(recipe, domain, when="derive")
        params = {prm.name: ParamRef(prm.name) for prm in self.params}
        # The rows and producers the body actually READS, under the names it
        # names them by. A DATA row nothing on the deck reads is NOT here, and
        # that is what makes a slot demand-pulled: a caller who supplies the bed
        # never pays for the survey and the terrain its producer would have
        # fetched. The accepted mesh is among them: a composite that samples at
        # its nodes reads the record the mesh step returned.
        read = self._reads()
        produced = {row.name: Ref(row.name) for row in self.data
                    if row.name in read}
        produced |= {step.name: Ref(step.name)
                     for step in (*produce, *derive)
                     if step.name} | {"settled": Ref("settled"), "mesh": Ref("mesh")}
        return [
            # THE FLOOR FIRST, before any stage: the six early readers settle a
            # clock, a rating curve and a weather record off keywords the run may
            # have overridden, and a stage built from the deck's own number would
            # describe a different run than the deck writes.
            Step(runner=f"{_TELEMAC}.workflow.stated", stage="prep",
                 kwargs={"steering": self.steering,
                         "keywords": RawKeywords}).named("stated"),
            *self._measurements(recipe, domain, when="world"),
            MeshStep.build(mesh=mesh,
                           name=Ref(self._states("MESH_ON", "") or domain),
                           supplied=self._states("SUPPLIED_MESH", None),
                           tool=self.name).named("mesh"),
            *produce,
            settle.named("settled"),
            *derive,
            Step(runner=f"{_TELEMAC}.workflow.fill_sheet", stage="author",
                 self_gating=True,
                 kwargs={"steering": self.steering, "produced": produced,
                         "params": params, "workflow": self.name,
                         "title": self._states("REVIEW_TITLE", ""),
                         "keywords": RawKeywords,
                         "input_mode": RunMode}).named("sheet"),
            Step(runner=f"{_TELEMAC}.workflow.run_sheet", stage="solve",
                 consequential=True,
                 kwargs={"sheet": Ref("sheet"), "settled": Ref("settled"),
                         "results": list(listed or (result,)),
                         "steering": (self._states("STEERING_FILE", "")
                                      or f"{self.steering.MODULE}_{self.name}.cas"),
                         "prefix": self._states("PREFIX", "telemac"),
                         "dispatch": _DISPATCH,
                         "display": self._states("DISPLAY_FILE", ""),
                         # HOW MANY CORES the solve is partitioned across: the
                         # param the question declares, never a second statement
                         # of it beside the params it already carries.
                         "compute_class": (ParamRef("compute_class")
                                           if "compute_class" in declared
                                           else None)}
                 ).named("solve"),
            self._outputs_step(params),
        ]

    def _asserted(self, keyword: str) -> Any:
        """One value the DECK itself states, read at RUN time off the floor.

        A stage settled at a number the deck was not written at describes a
        different run, and the floor may have moved that number before the deck
        writes it, so the stage reads it rather than being built from it. The
        deck still has to state it, and that refusal stands at import."""
        if self.steering.ASSERTED.get(keyword) is None:
            raise PlanValidationError(
                f"the workflow settles this question's run on the {keyword} its "
                f"deck states, and this deck states none.")
        return Ref(f"stated.{keyword}")

    def _file(self, keyword: str, fallback: str) -> str:
        """One file the deck itself names, read off the body that names it.

        The deck's own GEOMETRY / BOUNDARY CONDITIONS / RESULTS statements ARE
        the run directory's names; restating them would let the two drift."""
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

        Generated off ASSERTED so the doc cannot claim an opinion the deck does
        not hold; a value the run MEASURES is named as measured rather than
        printed, because it has no number until the run has one."""
        body = self.steering
        rows = []
        for name, slot in body.MODULE_INPUT.items():
            if name not in body.ASSERTED:
                continue
            rows.append(f"{slot.keyword} = {_stated_value(body.ASSERTED[name])}")
        return "; ".join(rows) if rows else "nothing"

    def _reads(self) -> set[str]:
        """Every row name the deck names, off the body's own assertions."""
        return {ref.root for ref in declared_reads(self.steering.ASSERTED, Ref)}

    def _placed(self) -> tuple[Placed, ...]:
        """Every point this question PLACES, in the order it is first named.

        Read off whatever names it - the deck's own composites, the reads the
        outputs and the answer are anchored on - because the thing that reads a
        placement is what knows there is one, and nothing else has to be told
        twice."""
        answer = self._states("ANSWER", {})
        anchors = [p.at for p in (*self._states("OUTPUTS", ()),
                                  *(m.primitive for m in answer.values()))]
        found: dict[str, Placed] = {}
        for surface in (self.steering.ASSERTED, anchors):
            for ref in declared_reads(surface, Ref):
                if isinstance(ref, Placed):
                    found.setdefault(ref.root, ref)
        return tuple(found.values())

    def _reading_day(self) -> tuple[Step, ...]:
        """The calendar DAY a dated source is asked over, where a row asks for one.

        It is the event_time lever read as a date, so it is the lever's own
        coercion rather than a stage a question writes: a row that names it gets
        it, and a run that reads no dated source never pays for it."""
        wanted = any(ref.root == _READING_DAY
                     for row in self.data if row.producer is not None
                     for rung in (row.producer, *row.producer.ladder_rungs)
                     for ref in declared_reads(dict(rung.kwargs), Ref))
        if not wanted:
            return ()
        return (Step(runner="trid3nt_server.inputs.instant.day", stage="prep",
                     kwargs={"value": ParamRef("event_time")}
                     ).named(_READING_DAY),)

    def _measurements(self, recipe: Any, domain: str, when: str) -> tuple[Step, ...]:
        """The stages that MEASURE this question's world, at the point they are taken.

        A composite that asks for a measurement is what says there is one; the
        world it is measured against is the workflow's, so the mesh, the line,
        the domain and the settled run are handed over here rather than by a
        template."""
        from trid3nt_server.workflows.runtime.plan import DataRef

        world = {"mesh": Ref("mesh"), "line": Ref("line"),
                 "domain": DataRef(domain), "settled": Ref("settled"),
                 "friction_law": None}
        steps = []
        for ask in self._asked(recipe):
            runner, stage, taken = _MEASURES[ask.kind]
            if taken != when:
                continue
            signature = _signature(runner)
            kwargs = {name: (self._asserted("LAW_OF_BOTTOM_FRICTION")
                             if name == "friction_law" else value)
                      for name, value in world.items() if name in signature}
            step = Step(runner=runner, stage=stage,
                        kwargs={**kwargs, **dict(ask.asked)})
            # The SETTLE is named by the plan that places it, which names every
            # settle the same thing; anything else is named for what it measured.
            steps.append(step if taken == "settle" else step.named(ask.root))
        return tuple(steps)

    def _settle(self, recipe: Any, domain: str) -> Step | None:
        """The stage this question is SETTLED by, where it is not open water.

        A harbour is settled by the wave that enters it rather than by a level
        the water stands at, so the composite that forces the domain is what
        says which settle this run takes."""
        taken = self._measurements(recipe, domain, when="settle")
        return taken[0] if taken else None

    def _asked(self, recipe: Any) -> tuple["Measured", ...]:
        """Every measurement this question asks for, in the order it is named.

        Read off the deck's own assertions and off the mesh recipe a template
        declared, because a measurement the MESHER consumes - the footprint a
        structure is cut out of - is asked for there and nowhere else."""
        found: dict[str, Measured] = {}
        ops = [dict(op.kwargs) for op in getattr(recipe, "ops", ())]
        for surface in (self.steering.ASSERTED, ops):
            for ref in declared_reads(surface, Ref):
                if isinstance(ref, Measured):
                    found.setdefault(ref.root, ref)
        return tuple(found.values())

    def _placements(self, domain: str) -> tuple[Step, ...]:
        """The stage that settles each placed point onto a node of the mesh.

        The domain rides along because an unplaced point sits its fraction along
        that domain's centerline companion, and a supplied one is held inside the
        water the same way."""
        from trid3nt_server.workflows.runtime.plan import DataRef

        return tuple(
            Step(runner=f"{_TELEMAC}.authoring.assembler.settle_release",
                 stage="author",
                 kwargs={"point": mark.point, "mesh": Ref("mesh"),
                         "domain": DataRef(domain), "fraction": mark.fraction,
                         "label": mark.label,
                         **({"continue_from": Continued} if mark.continues
                            else {})}).named(mark.root)
            for mark in self._placed())

    def _outputs_step(self, params: Mapping[str, Any]) -> Step:
        """The publish step, checked: every PLACED read has its caption.

        A template that reads nothing the user gives a place lists nothing: what
        the run writes is the module's table, published without being asked."""
        outputs = tuple(self._states("OUTPUTS", ()))
        captions = dict(self._states("CAPTIONS", {}))
        answer = dict(self._states("ANSWER", {}))
        for primitive in outputs:
            if primitive.publish is None:
                raise PlanValidationError(
                    f"OUTPUTS lists {primitive.kind}({primitive.variable!r}) with "
                    "no .layer(), .chart() or .animate(); a listed primitive is "
                    "published, and an answer is named under ANSWER.")
            named = primitive.variable or primitive.kind
            if named not in captions:
                raise PlanValidationError(
                    f"OUTPUTS publishes {named!r} and CAPTIONS names no caption "
                    "for it.")
        # A primitive's point, line and band, and the sheet value a measure is
        # held against, are reads the run resolves; they ride beside the list,
        # where the plan's binder walks, and rejoin it at publish.
        listed = [*outputs, *(m.primitive for m in answer.values())]
        anchors = [{"at": p.at, "along": p.along, "within": p.within,
                    "over": p.over, "above": p.above} for p in listed]
        return Step(runner=f"{_TELEMAC}.workflow.publish_outputs", stage="publish",
                    kwargs={"run": Ref("solve"),
                            "outputs": [_unanchored(p) for p in outputs],
                            "captions": captions,
                            "answer": {name: replace(m,
                                                     primitive=_unanchored(m.primitive),
                                                     against=None)
                                       for name, m in answer.items()},
                            "against": {name: m.against
                                        for name, m in answer.items()},
                            "anchors": anchors,
                            "params": dict(params)}).named("outputs")


def _signature(runner: str) -> frozenset[str]:
    """The keyword names one assembler runner takes."""
    from inspect import signature

    module, _, name = runner.rpartition(".")
    return frozenset(signature(getattr(import_module(module), name)).parameters)
