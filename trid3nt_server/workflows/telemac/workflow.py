"""The TELEMAC door: fill, run, then read.

``fill`` sets slots, expands composites and binds producers; it is repeatable and
decides nothing. ``run`` serializes the complete sheet, stages the run directory
and hands it to the box; it is explicit and consequential. ``publish_outputs``
reads the primitives a template listed off the solved run and publishes each
the way it asked."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.execution import AnswerLayerURI
from trid3nt_contracts.payload_warning import ParamSheet, ParamSheetRow

from trid3nt_server.workflows.publishing import Deliverable
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
from trid3nt_server.workflows.telemac.helpers.errors import TelemacDyeScenarioError
from trid3nt_server.workflows.telemac.modules import wrapper_for
from trid3nt_server.workflows.telemac.modules.module import SlotRefused
from trid3nt_server.workflows.telemac.modules.outputs import Measure, Primitive, Solved
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

    #: The STEERING body: the module wrapper plus the parts and slots this
    #: question asserts.
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
    #: A per-question reader publishing the solved file, ``(run) -> Step``, for
    #: a template that lists no outputs.
    read: Callable[[Any], Step] | None = None
    #: This question's own producers: what the WORLD gives, before the run is
    #: settled, and what the SETTLED run gives, after it.
    produce: tuple[Step, ...] = ()
    derive: tuple[Step, ...] = ()
    #: Slot values the fill sets that the body cannot state - a value the canvas
    #: or another producer answers, under the identifier it fills.
    slots: Mapping[str, Any] = field(default_factory=dict)
    #: Constants the reader needs that no keyword carries: what was released, and
    #: which product family publishes it.
    meta: Mapping[str, Any] = field(default_factory=dict)
    chart: tuple[str, Callable[..., Any]] | None = None
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
        stated = {name for part in (*body.PARTS, body) for name in part.ASSERTED}
        stated |= set(self.slots)
        touched = sorted({body.DICTIONARY[name].rubrique[0] for name in stated
                          if name in body.DICTIONARY and body.DICTIONARY[name].rubrique})
        open_required = sorted(slot.keyword for name, slot in body.DICTIONARY.items()
                               if slot.is_required and name not in stated)
        return (
            f"Sheet: {body.MODULE}, whose dictionary has {len(body.DICTIONARY)} "
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
        if self.outputs:
            read = self._outputs_step(params)
        else:
            read = self.read(Ref("solve"))
            if self.chart is not None:
                read = read.chart(self.chart[0], builder=self.chart[1])
        # Every producer the body may READ, under the name it names it by. A
        # body states what it will hold; this is where the fill finds it.
        produced = {step.name: Ref(step.name)
                    for step in (*self.produce, *self.derive)
                    if step.name} | {"settled": Ref("settled")}
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
                         "dispatch": self.dispatch, "meta": dict(self.meta),
                         "compute_class": self.compute_class}).named("solve"),
            read,
        ]

    def _outputs_step(self, params: Mapping[str, Any]) -> Step:
        """The publish step, checked: every published variable has its caption."""
        for primitive in self.outputs:
            if primitive.publish is None:
                raise PlanValidationError(
                    f"OUTPUTS lists {primitive.kind}({primitive.variable!r}) with "
                    "no .layer(), .chart() or .animate(); a listed primitive is "
                    "published, and an answer is named under ANSWER.")
            if primitive.variable and primitive.variable not in self.captions:
                raise PlanValidationError(
                    f"OUTPUTS publishes {primitive.variable!r} and CAPTIONS names "
                    "no caption for it.")
        return Step(runner=f"{_TELEMAC}.workflow.publish_outputs", stage="publish",
                    kwargs={"run": Ref("solve"), "outputs": list(self.outputs),
                            "captions": dict(self.captions),
                            "answer": dict(self.answer),
                            "params": dict(params)}).named("outputs")


async def publish_outputs(*, run: Mapping[str, Any], outputs: Sequence[Primitive],
                          captions: Mapping[str, str],
                          answer: Mapping[str, Measure],
                          params: Mapping[str, Any]) -> AnswerLayerURI:
    """Read the listed primitives off the solved run, publish each, answer.

    The result is read ONCE; every primitive and every answer reads from it."""
    body = wrapper_for(str(run["module"]))
    solved = Solved(run, body)
    wanted = {primitive.key for primitive in outputs}
    wanted |= {measure.primitive for measure in answer.values()}
    reads = await asyncio.to_thread(
        lambda: {key: body.OUTPUTS[key.kind].read(key, solved) for key in wanted})
    published = await publish(
        run_id=str(run["run_id"]), engine="telemac", name=str(run["name"]),
        where=str(params.get("location") or run["name"]),
        reference_time=run.get("started_at"),
        items=[Deliverable(read=reads[primitive.key], mode=primitive.publish,
                           caption=captions.get(primitive.variable or "",
                                                primitive.kind),
                           style=primitive.style)
               for primitive in outputs])
    answered = {name: reads[measure.primitive].measures.get(measure.stat)
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
    sheet = fill_slots(steering, template=workflow, produced=dict(produced),
                       params=dict(params), **stated)
    revised = await _review(sheet, workflow=workflow, title=title,
                            input_mode=input_mode)
    if revised:
        sheet = fill_slots(sheet, produced=dict(produced), params=dict(params),
                           **revised)
    logger.info("telemac sheet filled: %s states %d keywords, %d open "
                "(%d required)", sheet.body.__name__, len(sheet.filled),
                len(sheet.open()), len(sheet.required()))
    return sheet


async def run_sheet(*, sheet: Sheet, settled: Mapping[str, Any],
                    results: Sequence[str], steering: str, prefix: str,
                    dispatch: str, meta: Mapping[str, Any],
                    compute_class: Any) -> dict[str, Any]:
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
    return {**settled, **dict(meta), **handle, "module": sheet.module,
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
    rest = [slot for name, slot in sheet.body.DICTIONARY.items()
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
        raise TelemacDyeScenarioError(
            "USER_INPUT_CANCELLED",
            outcome.cancel_reason or f"{workflow} was cancelled at the review.")
    return {name: value for name, value in outcome.params.items()
            if name in sheet.body.DICTIONARY or name in sheet.body.COMPOSITES}


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
