"""The TELEMAC door: fill, then run.

TWO ACTS ON ONE SHEET. ``fill`` sets slots, expands the composites the wrapper
registered, binds every producer the template named, and hands back what the
sheet now says; it is repeatable, it decides nothing, and it renders the sheet as
the card the run is HELD on. ``run`` is the other act and it is explicit: a
complete sheet is serialized into the engine's own steering files, the run
directory is staged, and the box receives it.

:class:`Door` is what a template hands over instead of a plan. It names the world
the sheet is filled FROM - the domain, the mesh recipe, the producers this
question needs - the STEERING body itself, and the wrapper OUTPUT that publishes
the solved file. The sequence it builds is the same for every question, which is
what a template no longer has to write down.

``TelemacWorkflow`` is what the skeleton records the engine and the solve step
under; every question this engine answers is a Door, so it declares those two
facts and realizes nothing else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.payload_warning import ParamSheet, ParamSheetRow

from trid3nt_server.workflows.runtime import (
    ParamRef,
    Ref,
    RunMode,
    Step,
    Workflow,
)
from trid3nt_server.workflows.mesh.step import MeshStep
from trid3nt_server.workflows.telemac.helpers.errors import TelemacDyeScenarioError
from trid3nt_server.workflows.telemac.modules.sheet import Sheet
from trid3nt_server.workflows.telemac.modules.sheet import fill as fill_slots
from trid3nt_server.workflows.telemac.modules.sheet import run as run_sheet_

logger = logging.getLogger("trid3nt_server.workflows.telemac.workflow")

__all__ = ["Door", "TelemacWorkflow", "fill_sheet", "run_sheet"]

_TELEMAC = "trid3nt_server.workflows.telemac"

#: What a reader may open on any run, past the files the engine is required to
#: write: the mesh it solved on, the deck it read, the listing and the metrics.
_ALWAYS_READABLE = ("full_listing.log", "telemac_metrics.json")


# --------------------------------------------------------------------------- #
# The door.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Door:
    """What a template hands over: the world, the sheet, and how it is read.

    Called by the skeleton at registration, it returns the step sequence - the
    domain, the mesh, this question's own producers, the two acts on the sheet,
    and the wrapper output that publishes the solved file. Nothing here decides
    anything: every value it carries is the template's own declaration.
    """

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
    #: The reader that publishes the solved file: ``(run) -> Step``.
    read: Callable[[Any], Step]
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

    def __call__(self, ops: Workflow) -> list[Any]:
        """The step sequence: the world, then fill, then run, then the reader."""
        read = self.read(Ref("solve"))
        # Every producer the body may READ, under the name it names it by. A
        # body states what it will hold; this is where the fill finds it.
        produced = {step.name: Ref(step.name)
                    for step in (*self.produce, *self.derive)
                    if step.name} | {"settled": Ref("settled")}
        if self.chart is not None:
            read = read.chart(self.chart[0], builder=self.chart[1])
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
                         "params": {prm.name: ParamRef(prm.name)
                                    for prm in ops.params},
                         "slots": dict(self.slots), "workflow": ops.name,
                         "title": self.review_title,
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


async def fill_sheet(*, steering: type, produced: Mapping[str, Any],
                     params: Mapping[str, Any], slots: Mapping[str, Any],
                     workflow: str, title: str,
                     input_mode: str | None) -> Sheet:
    """Set the body's slots against what the run measured -> the sheet, HELD.

    The producers have already run, so the canvas shows the mesh and the release
    before anything is filled. What comes back is every filled slot with its
    provenance and every mandatory slot still open - and in ``user_gated`` that
    IS the card: the sheet is shown, an edit is another fill, and the run waits.
    """
    # A slot the caller did not override is not a statement: the body's own
    # value stands. That is not the same as a body asserting None, which IS the
    # statement that this run says nothing about the keyword.
    stated = {name: value for name, value in slots.items() if value is not None}
    sheet = fill_slots(steering, produced=dict(produced), params=dict(params),
                       **stated)
    revised = await _review(sheet, workflow=workflow, title=title,
                            input_mode=input_mode)
    if revised:
        sheet = fill_slots(sheet, produced=dict(produced), params=dict(params),
                           **revised)
    logger.info("telemac sheet filled: %s states %d keywords, %d open",
                sheet.body.__name__, len(sheet.filled), len(sheet.open()))
    return sheet


async def run_sheet(*, sheet: Sheet, settled: Mapping[str, Any],
                    results: Sequence[str], steering: str, prefix: str,
                    dispatch: str, meta: Mapping[str, Any],
                    compute_class: Any) -> dict[str, Any]:
    """A complete sheet: serialize, stage, hand it to the box -> the run handle.

    What a reader may later OPEN is the sheet's own answer: the files the engine
    must write, the mesh it was handed, the deck it read, and every file a
    composite named beside it. Nothing is listed that the run does not carry.
    """
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
    return {**settled, **dict(meta), **handle}


#: How a slot's PROVENANCE reads on the card: which door served the value, and
#: what the badge says beside it. The card is a view of the sheet, so the row's
#: name is the keyword's own identifier and an edit of it is another fill.
_PROVENANCE_DOORS: Mapping[str, tuple[str, str]] = {
    "template": ("scenario", "derived"),
    "fill": ("user", "user"),
}


async def _review(sheet: Sheet, *, workflow: str, title: str,
                  input_mode: str | None) -> dict[str, Any]:
    """Show the filled sheet and HOLD -> the slot edits the user submitted.

    The card is the door's VIEW of the sheet rather than a step of its own: the
    set slots and the open mandatory ones are what a run is reviewed on, and
    submitting an edited sheet IS the approval, because the whole of it was on
    screen. In ``auto`` nothing is shown and nothing waits.
    """
    from trid3nt_server.gates.input_review import gate_input_review

    rows = [_slot_row(name, row) for name, row in sheet.filled.items()]
    rows += [ParamSheetRow(name=slot.identifier, value=None, desc=slot.desc[:512],
                           door="user", basis="derived", editable=True,
                           source_badge="open: the dictionary gives it no default")
             for slot in sheet.open()]
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
            if name in sheet.body.CATALOG or name in sheet.body.COMPOSITES}


def _slot_row(name: str, row: Any) -> ParamSheetRow:
    """One filled slot, as the card renders it."""
    door, basis = _PROVENANCE_DOORS.get(row.provenance, ("derived", "derived"))
    value = row.value if isinstance(row.value, (int, float, str, bool, list)) \
        else str(row.value)
    return ParamSheetRow(
        name=name, value=value, desc=row.slot.desc[:512], door=door, basis=basis,
        source_badge=row.provenance,
        # A slot the template or a part settled is inspectable rather than the
        # question, so it folds away; what a producer measured and what a fill
        # set are the rows a review is actually about.
        advanced=row.provenance not in ("fill",) and row.slot.level > 0)


class TelemacWorkflow(Workflow):
    """TELEMAC: the two facts the skeleton records a run of this engine under."""

    engine = "telemac2d"
    solve_step = "solve"
