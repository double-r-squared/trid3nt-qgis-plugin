"""The TELEMAC door: fill, run, then read.

``fill`` sets slots, expands composites and binds producers; it is repeatable and
decides nothing. ``run`` serializes the complete sheet, stages the run directory
and hands it to the box; it is explicit and consequential. ``publish_outputs``
reads the primitives a template listed off the solved run and publishes each
the way it asked."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from importlib import import_module
from typing import Any, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.execution import AnswerLayerURI
from trid3nt_contracts.payload_warning import ParamSheet, ParamSheetRow

from trid3nt_server.workflows.publishing import Deliverable, Line, Profile
from trid3nt_server.workflows.publishing.publish import publish
from trid3nt_server.workflows.runtime import (
    ParamRef,
    PlanValidationError,
    RawKeywords,
    Ref,
    RunMode,
    Step,
    Workflow,
)
from trid3nt_server.workflows.mesh.step import MeshStep
from trid3nt_server.workflows.telemac.errors import TelemacError
from trid3nt_server.workflows.telemac.modules import wrapper_for
from trid3nt_server.workflows.telemac.modules.module import SlotRefused
from trid3nt_server.workflows.telemac.modules.outputs import (
    Measure,
    OutputEmpty,
    Primitive,
    Solved,
)
from trid3nt_server.workflows.telemac.modules.sheet import Origin, Sheet
from trid3nt_server.workflows.telemac.modules.sheet import fill as fill_slots
from trid3nt_server.workflows.telemac.modules.sheet import run as run_sheet_

logger = logging.getLogger("trid3nt_server.workflows.telemac.workflow")

__all__ = ["Door", "TelemacWorkflow", "card_rows", "fill_sheet", "publish_outputs",
           "run_sheet"]

_TELEMAC = "trid3nt_server.workflows.telemac"

#: What a reader may open on any run, past the files the engine is required to
#: write: the mesh it solved on, the deck it read, the listing and the metrics.
_ALWAYS_READABLE = ("full_listing.log", "telemac_metrics.json")


@dataclass(frozen=True, slots=True)
class Door:
    """What a template hands over: the world, the sheet, and how it is read.

    Decides nothing: every value it carries is the template's own declaration."""

    #: The STEERING body: the module wrapper plus the slots this question asserts.
    steering: type
    #: The step that MEASURES what the sheet is filled from, named ``settled``.
    settle: Step
    #: The steps that establish the modelled world, and what the mesh is built
    #: over - the name one of them was ``.named()`` by.
    domain: tuple[Step, ...]
    mesh: Any
    mesh_on: str
    #: The engine files this run has to write for it to have solved anything.
    results: tuple[str, ...]
    #: What the run directory calls the deck, and where the staged files live.
    steering_file: str
    prefix: str
    #: The dispatch the staged run goes to, by the path it is resolved at CALL
    #: time. WHICH box entry is the template's, not the sheet's.
    dispatch: str
    #: The primitives the template reads off the solved run, each with how it
    #: is published; ``captions`` names each variable's quantity in the
    #: template's words; ``answer`` names the measures the run answers with.
    outputs: Sequence[Primitive] = ()
    captions: Mapping[str, str] = field(default_factory=dict)
    answer: Mapping[str, Measure] = field(default_factory=dict)
    #: This question's own producers: what the WORLD gives, before the run is
    #: settled, and what the SETTLED run gives, after it.
    produce: tuple[Step, ...] = ()
    derive: tuple[Step, ...] = ()
    #: Slot values the fill sets that the body cannot state - a value the canvas
    #: or another producer answers, under the identifier it fills.
    slots: Mapping[str, Any] = field(default_factory=dict)
    compute_class: Any = None
    #: The title the card carries when the run is held for review.
    review_title: str = ""
    #: The DATA slot a caller may hand a built mesh in instead of the recipe.
    #: Filled, that mesh is adopted whole; unfilled, the recipe above is the mesh.
    supplied_mesh: Any = None

    def sheet_doc(self) -> str:
        """The ENGINE SURFACE line of this template's docstring.

        Read off the declaration itself, never claimed by prose."""
        body = self.steering
        stated = set(body.ASSERTED) | set(self.slots)
        touched = sorted({body.MODULE_INPUT[name].rubrique[0] for name in stated
                          if name in body.MODULE_INPUT and body.MODULE_INPUT[name].rubrique})
        open_required = sorted(slot.keyword for name, slot in body.MODULE_INPUT.items()
                               if slot.is_required and name not in stated)
        return (
            f"Sheet: {body.MODULE}, whose dictionary has {len(body.MODULE_INPUT)} "
            f"keywords. This template states {len(stated)} of them, under "
            f"{', '.join(touched)}. Open mandatory slots: "
            f"{', '.join(open_required) if open_required else 'none'}. Every "
            f"other keyword is the engine's own default and is set on the call - "
            f"keywords={{\"LAW OF BOTTOM FRICTION\": 4}} - after "
            f"describe_keywords(module=\"{body.MODULE}\", query=...) names it "
            f"with its help, its choices and that default.")

    def __call__(self, ops: Workflow) -> list[Any]:
        """The step sequence: the world, then fill, then run, then the outputs."""
        params = {prm.name: ParamRef(prm.name) for prm in ops.params}
        # Every DATA row and every producer the body may READ, under the name
        # it names it by. A body states what it will hold; this is where the
        # fill finds it. The accepted mesh is among them: a composite that
        # samples at its nodes reads the record the mesh step returned.
        produced = {row.name: Ref(row.name) for row in ops.data}
        produced |= {step.name: Ref(step.name)
                     for step in (*self.produce, *self.derive)
                     if step.name} | {"settled": Ref("settled"), "mesh": Ref("mesh")}
        return [
            *self.domain,
            MeshStep.build(mesh=self.mesh, name=Ref(self.mesh_on),
                           supplied=self.supplied_mesh,
                           tool=ops.name).named("mesh"),
            *self.produce,
            self.settle.named("settled"),
            *self.derive,
            Step(runner=f"{_TELEMAC}.workflow.fill_sheet", stage="author",
                 self_gating=True,
                 kwargs={"steering": self.steering, "produced": produced,
                         "params": params,
                         "slots": dict(self.slots), "workflow": ops.name,
                         "title": self.review_title,
                         "keywords": RawKeywords,
                         "input_mode": RunMode}).named("sheet"),
            Step(runner=f"{_TELEMAC}.workflow.run_sheet", stage="solve",
                 consequential=True,
                 kwargs={"sheet": Ref("sheet"), "settled": Ref("settled"),
                         "results": list(self.results),
                         "steering": self.steering_file, "prefix": self.prefix,
                         "dispatch": self.dispatch,
                         "compute_class": self.compute_class}).named("solve"),
            self._outputs_step(params),
        ]

    def _outputs_step(self, params: Mapping[str, Any]) -> Step:
        """The publish step, checked: every published variable has its caption."""
        if not self.outputs:
            raise PlanValidationError(
                "OUTPUTS lists nothing; a template reads its answer off the solved "
                "run through the primitives it lists.")
        for primitive in self.outputs:
            if primitive.publish is None:
                raise PlanValidationError(
                    f"OUTPUTS lists {primitive.kind}({primitive.variable!r}) with "
                    "no .layer(), .chart() or .animate(); a listed primitive is "
                    "published, and an answer is named under ANSWER.")
            named = primitive.variable or primitive.kind
            if named not in self.captions:
                raise PlanValidationError(
                    f"OUTPUTS publishes {named!r} and CAPTIONS names no caption "
                    "for it.")
        # A primitive's point, line and band, and the sheet value a measure is
        # held against, are reads the run resolves; they ride beside the list,
        # where the plan's binder walks, and rejoin it at publish.
        listed = [*self.outputs, *(m.primitive for m in self.answer.values())]
        anchors = [{"at": p.at, "along": p.along, "within": p.within}
                   for p in listed]
        return Step(runner=f"{_TELEMAC}.workflow.publish_outputs", stage="publish",
                    kwargs={"run": Ref("solve"),
                            "outputs": [_unanchored(p) for p in self.outputs],
                            "captions": dict(self.captions),
                            "answer": {name: replace(m, primitive=_unanchored(m.primitive),
                                                     against=None)
                                       for name, m in self.answer.items()},
                            "against": {name: m.against
                                        for name, m in self.answer.items()},
                            "anchors": anchors,
                            "params": dict(params)}).named("outputs")


def _unanchored(primitive: Primitive) -> Primitive:
    return replace(primitive, at=None, along=None, within=None)


def _anchored(primitive: Primitive, anchor: Mapping[str, Any]) -> Primitive:
    return replace(primitive, at=anchor["at"], along=anchor["along"],
                   within=anchor.get("within"))


async def publish_outputs(*, run: Mapping[str, Any], outputs: Sequence[Primitive],
                          captions: Mapping[str, str],
                          answer: Mapping[str, Measure],
                          params: Mapping[str, Any],
                          anchors: Sequence[Mapping[str, Any]] = (),
                          against: Mapping[str, Any] | None = None
                          ) -> AnswerLayerURI:
    """Read the listed primitives off the solved run, publish each, answer.

    Each module's result is read ONCE; every primitive and every answer reads
    from it, a coupled module's own file through its own wrapper. A chart's
    reference is a callable computing lines beside the read, or another
    primitive read where the chart's own is anchored and drawn as a line. A
    listed output the result lacks refuses; an answer over one is ``None``."""
    listed = [*outputs, *(m.primitive for m in answer.values())]
    if anchors:
        listed = [_anchored(p, a) for p, a in zip(listed, anchors)]
    outputs = listed[:len(outputs)]
    answer = {name: replace(m, primitive=p, against=(against or {}).get(name))
              for (name, m), p in zip(answer.items(), listed[len(outputs):])}
    solved: dict[str, Solved] = {}
    published_keys = {primitive.key for primitive in outputs}

    def _read(key: Primitive) -> Any:
        module = key.module or str(run["module"])
        if module not in solved:
            solved[module] = Solved(run, wrapper_for(module))
        try:
            return solved[module].body.OUTPUTS[key.kind].read(key, solved[module])
        except OutputEmpty:
            # A run that carried nothing a measure could read answers with
            # nothing; a published output that is missing is a refusal.
            if key in published_keys:
                raise
            return None

    def _beside(primitive: Primitive) -> Primitive | None:
        reference = primitive.reference
        if not isinstance(reference, Primitive):
            return None
        return replace(reference, at=primitive.at, along=primitive.along).key

    wanted = {primitive.key for primitive in outputs}
    wanted |= {measure.primitive for measure in answer.values()}
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
    published = await publish(
        run_id=str(run["run_id"]), engine="telemac", name=str(run["name"]),
        where=str(params.get("location") or run["name"]),
        reference_time=run.get("started_at"),
        items=[Deliverable(read=reads[primitive.key], mode=primitive.publish,
                           caption=captions.get(primitive.variable or primitive.kind,
                                                primitive.kind),
                           style=primitive.style)
               for primitive in outputs])
    answered = {name: measure.answer(
                    None if reads[measure.primitive] is None
                    else reads[measure.primitive].measures.get(measure.stat),
                    measure.against)
                for name, measure in answer.items()}
    if published.primary is None:
        raise SlotRefused(
            "the outputs list publishes no layer, so the run has nothing to lead "
            "with; list at least one .layer().")
    logger.info("telemac outputs published run_id=%s layers=%d charts=%d "
                "animations=%d answer=%s", run["run_id"], len(published.layers),
                len(published.charts), published.animations, answered)
    return AnswerLayerURI(**published.primary.model_dump(), answer=answered)


async def fill_sheet(*, steering: type, produced: Mapping[str, Any],
                     params: Mapping[str, Any], slots: Mapping[str, Any],
                     workflow: str, title: str, keywords: Mapping[str, Any],
                     input_mode: str | None) -> Sheet:
    """Set the body's slots against what the run measured -> the sheet, HELD.

    The raw ``keywords`` floor is filled last and therefore beats a template value."""
    # A slot the caller did not override is not a statement: the body's own
    # value stands. That is not the same as a body asserting None, which IS the
    # statement that this run says nothing about the keyword.
    stated = {name: value for name, value in slots.items() if value is not None}
    if keywords and not isinstance(keywords, Mapping):
        # A floor that arrived as anything but a mapping is a caller error worth
        # naming: the alternative is an attribute error from inside the fill,
        # blaming a step rather than the argument.
        raise SlotRefused(
            f"keywords takes a mapping of the engine's own keyword names to "
            f"values, e.g. {{\"LAW OF BOTTOM FRICTION\": 4}}; got "
            f"{type(keywords).__name__}.")
    stated.update({steering.identify(name): value
                   for name, value in (keywords or {}).items()})
    # A composite may read fetched data at the fill - a raster sampled at the
    # mesh's nodes - so the fill runs off the loop.
    sheet = await asyncio.to_thread(
        fill_slots, steering, template=workflow, produced=dict(produced),
        params=dict(params), **stated)
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
                    dispatch: str, compute_class: Any) -> dict[str, Any]:
    """A complete sheet: serialize, stage, hand it to the box -> the run handle.

    Nothing is listed as readable that the run does not carry."""
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
    """The sheet as the card renders it: what is SET, what is OPEN, then the rest.

    The rest is the whole module, folded under advanced with its engine default."""
    rows = [_slot_row(name, row) for name, row in sheet.filled.items()]
    rows += [_open_row(slot) for slot in sheet.required()]
    # The advanced fold reads down the dictionary's own RUBRIQUES, and inside one
    # down the dictionary's own order - the sections the engine's documentation
    # is written in, rather than a flat thousand-row list.
    rest = [slot for name, slot in sheet.body.MODULE_INPUT.items()
            if name not in sheet.filled and not slot.is_required]
    return rows + [_default_row(slot) for slot in
                   sorted(rest, key=lambda slot: _group(slot))]


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
    """One SET slot, as the card renders it: the value and where it came from."""
    door, basis = _ORIGIN_DOORS[row.provenance.origin]
    value = row.value if isinstance(row.value, (int, float, str, bool, list)) \
        else str(row.value)
    return ParamSheetRow(
        name=name, value=value, desc=row.slot.desc[:512], door=door, basis=basis,
        origin=row.provenance.origin.value, source_badge=str(row.provenance),
        group=_group(row.slot))


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
    """TELEMAC: the two facts the skeleton records a run of this engine under."""

    engine = "telemac"
    solve_step = "solve"
