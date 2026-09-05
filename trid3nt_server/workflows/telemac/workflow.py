"""The TELEMAC door: fill, then run - and the facade the un-flipped fronts use.

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

``TelemacWorkflow`` still realizes the four operations for the two fronts that
have not been flipped - harbour agitation and stratified flow. Those are Stage
3's, and :data:`_PROCESSES` dies with them.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.payload_warning import ParamSheet, ParamSheetRow

from trid3nt_server.workflows.runtime import (
    DataRef,
    Forcing,
    ParamRef,
    Physics,
    PlanValidationError,
    Ref,
    RunMode,
    Step,
    Workflow,
)
from trid3nt_server.workflows.mesh.step import MeshStep
from trid3nt_server.workflows.shared.aoi import AcquireAoi
from trid3nt_server.workflows.telemac.authoring.agitation import (
    Agitation,
    write_agitation_case,
)
from trid3nt_server.workflows.telemac.authoring.open_water import SolveOpenWater
from trid3nt_server.workflows.telemac.authoring.stratified import (
    Stratified,
    write_stratified_case,
)
from trid3nt_server.workflows.telemac.modules import SlotRefused
from trid3nt_server.workflows.telemac.modules.sheet import Sheet
from trid3nt_server.workflows.telemac.modules.sheet import fill as fill_slots
from trid3nt_server.workflows.telemac.modules.sheet import run as run_sheet_

logger = logging.getLogger("trid3nt_server.workflows.telemac.workflow")

__all__ = ["Door", "TelemacWorkflow", "fill_sheet", "mesh_sheet_fields",
           "run_sheet"]

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
    #: The dispatch the staged run goes to. WHICH box entry is the template's,
    #: not the sheet's.
    dispatch: Callable[..., Any]
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

    def __call__(self, ops: Workflow) -> list[Any]:
        """The step sequence: the world, then fill, then run, then the reader."""
        read = self.read(Ref("solve"))
        if self.chart is not None:
            read = read.chart(self.chart[0], builder=self.chart[1])
        return [
            *self.domain,
            MeshStep.build(mesh=self.mesh, name=Ref(self.mesh_on)).named("mesh"),
            *self.produce,
            self.settle.named("settled"),
            *self.derive,
            Step(runner=f"{_TELEMAC}.workflow.fill_sheet", stage="author",
                 self_gating=True,
                 kwargs={"steering": self.steering, "settled": Ref("settled"),
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


async def fill_sheet(*, steering: type, settled: Mapping[str, Any],
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
    sheet = fill_slots(steering, produced={"settled": settled},
                       params=dict(params), **stated)
    revised = await _review(sheet, workflow=workflow, title=title,
                            input_mode=input_mode)
    if revised:
        sheet = fill_slots(sheet, produced={"settled": settled},
                           params=dict(params), **revised)
    logger.info("telemac sheet filled: %s states %d keywords, %d open",
                sheet.body.__name__, len(sheet.filled), len(sheet.open()))
    return sheet


async def run_sheet(*, sheet: Sheet, settled: Mapping[str, Any],
                    results: Sequence[str], steering: str, prefix: str,
                    dispatch: Callable[..., Any], meta: Mapping[str, Any],
                    compute_class: Any) -> dict[str, Any]:
    """A complete sheet: serialize, stage, hand it to the box -> the run handle.

    What a reader may later OPEN is the sheet's own answer: the files the engine
    must write, the mesh it was handed, the deck it read, and every file a
    composite named beside it. Nothing is listed that the run does not carry.
    """
    coupled = dict(sheet.resolved()).get("COUPLING WITH")
    outputs = [*results, *(row["dest"] for row in settled["mesh_inputs"]),
               steering, *_ALWAYS_READABLE, *sorted(sheet.files)]
    handle = await run_sheet_(
        sheet, dispatch=dispatch, mesh_inputs=settled["mesh_inputs"],
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
    entries = [SyntheticInput(param=row.name, value=row.value, basis=row.basis,
                              note=row.source_badge) for row in rows if row.value
               is not None and not row.advanced]
    outcome = await gate_input_review(
        tool_name=workflow, mode=input_mode, entries=entries, params={},
        param_sheet=ParamSheet(workflow=workflow,
                               title=title or f"Review the {workflow} sheet",
                               rows=rows))
    if not outcome.proceed:
        raise SlotRefused(outcome.cancel_reason
                          or f"{workflow} was cancelled at the sheet review.")
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


# --------------------------------------------------------------------------- #
# The facade the un-flipped fronts still run on. Stage 3 takes it.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Process:
    """One declared physics process, end to end.

    The facade routes on ``Physics.process``, and this is what it routes TO: the
    author step that serializes it, the writer whose real signature the slots are
    checked against in both directions, the solve that dispatches it, and the
    reader that turns the solved file into the question's deliverable.
    """

    domain_kw: str
    domain_ref: Any
    author: Callable[..., Step]
    writer: Callable[..., Any]
    solve: Callable[..., Step]
    read: Callable[..., Step]
    forcing_fields: Mapping[str, str]
    extra_fields: Mapping[str, Any] = None  # type: ignore[assignment]


def _open_water_solve(*, compute_class: Any) -> Step:
    return SolveOpenWater.telemac(run=Ref("run"), compute_class=compute_class)


def _read_agitation(*, solve: Any, physics: Physics, forcing: Forcing) -> Step:
    return Agitation.products(run=Ref("run"), solve=solve).named("agitation")


def _read_stratified(*, solve: Any, physics: Physics, forcing: Forcing) -> Step:
    return Stratified.products(run=Ref("run"), solve=solve).named("column")


#: The agitation author always reads the run's MESH slot: the domain a phase-
#: resolving solve runs on is the caller's to author, and an author that read the
#: slot only sometimes would answer the grid question on the runs that filled it.
_AGITATION_EXTRA: Mapping[str, Any] = {"supplied_mesh": DataRef("mesh")}

#: The TELEMAC physics processes still declared as plans. See :class:`_Process`.
_PROCESSES: dict[str, _Process] = {
    "harbor_agitation": _Process(
        domain_kw="aoi", domain_ref=Ref("aoi"),
        author=Agitation.case, writer=write_agitation_case,
        solve=_open_water_solve, read=_read_agitation, forcing_fields={},
        extra_fields=_AGITATION_EXTRA),
    "stratified_3d": _Process(
        domain_kw="aoi", domain_ref=Ref("aoi"),
        author=Stratified.case, writer=write_stratified_case,
        solve=_open_water_solve, read=_read_stratified, forcing_fields={}),
}

#: Recipe params a TELEMAC author reads, under the name that writer knows each
#: by. Only the AGNOSTIC params can appear here: an op is a call on a mesh library
#: and means nothing to a steering file.
_MESH_SHEET_PARAMS: Mapping[str, str] = {
    "resolution_m": "mesh_resolution_m",
}


class TelemacWorkflow(Workflow):
    """TELEMAC: the fill/run door, and the open-water pipeline behind the four."""

    engine = "telemac2d"
    solve_step = "solve"

    # -- 1. acquire -------------------------------------------------------- #

    def acquire_domain(self, *, location: Any, bbox: Any,
                       aoi_half_deg: float | tuple[float, float] = 0.06,
                       aoi_name: str = "aoi",
                       code_prefix: str = "TELEMAC") -> tuple[Step, ...]:
        """The AOI an OPEN-WATER domain is solved over, and nothing else.

        A lake fetch and a harbour basin are bounded by the ask itself; there is
        no flowline to find and no carrier to resolve. A reach and a catchment
        establish their own world in the template that asks for one.
        """
        return (AcquireAoi(location=location, bbox=bbox, half_deg=aoi_half_deg,
                           default_name=aoi_name,
                           code_prefix=code_prefix).named("aoi"),)

    # -- 2. author --------------------------------------------------------- #

    def author(self, *, mesh: Any, physics: Physics, forcing: Forcing) -> Step:
        """Serialize the mesh ask + physics + forcing into the process's run."""
        if not _is_mesh_recipe(mesh):
            raise PlanValidationError(
                "author needs the template's MESH recipe (tool.build_mesh(...)), "
                f"got {type(mesh).__name__}.")
        if not isinstance(physics, Physics):
            raise PlanValidationError(
                f"author needs a Physics slot, got {type(physics).__name__}.")
        if not isinstance(forcing, Forcing):
            raise PlanValidationError(
                f"author needs a Forcing slot, got {type(forcing).__name__}.")
        process = self._process(physics)
        fields: dict[str, Any] = {process.domain_kw: process.domain_ref}
        fields.update(dict(process.extra_fields or {}))
        fields.update(mesh_sheet_fields(mesh))
        fields.update(_translate(forcing.values, process.forcing_fields))
        fields.update(physics.values)
        _refuse_uncovered_fields(fields, process.writer, physics.process)
        return process.author(**fields).named("run")

    # -- 3. solve ---------------------------------------------------------- #

    def solve(self, *, compute_class: Any, physics: Physics) -> Step:
        """Dispatch the staged run to the worker the declared process runs on.

        ``physics`` is the process SELECTOR and is required: it must never
        default, because a template that forgot to name its physics would
        otherwise dispatch to the wrong solver silently.
        """
        if physics is None:
            raise PlanValidationError(
                "solve needs the physics process that selects the worker; "
                f"none was named (known: {sorted(_PROCESSES)}).")
        return self._process(physics).solve(compute_class=compute_class).named("solve")

    # -- 4. read ----------------------------------------------------------- #

    def read(self, run: Any, *, physics: Physics, forcing: Forcing) -> Step:
        """The published deliverable the declared physics process asks for."""
        return self._process(physics).read(solve=run, physics=physics, forcing=forcing)

    # -- routing ----------------------------------------------------------- #

    @staticmethod
    def _process(physics: Any) -> _Process:
        name = getattr(physics, "process", "")
        found = _PROCESSES.get(name)
        if found is None:
            raise PlanValidationError(
                f"TELEMAC models no physics process {name!r} "
                f"(known: {sorted(_PROCESSES)}).")
        return found


def _is_mesh_recipe(value: Any) -> bool:
    """Is ``value`` a frozen ``tool.build_mesh`` recipe?

    Imported where it is asked rather than at module scope: the mesh tool
    registers itself into the tool registry, which imports the templates that
    import this facade, and a module-level import would close that circle.
    """
    from trid3nt_server.workflows.mesh.tool import MeshRecipe

    return isinstance(value, MeshRecipe)


def mesh_sheet_fields(mesh: Any) -> dict[str, Any]:
    """The declared mesh recipe, as the keywords a TELEMAC author reads.

    A param the template did not declare is ABSENT rather than ``None``: passing
    None would override the author's own default with nothing, and "the template
    did not ask" is what absence means.
    """
    return {field_name: getattr(mesh, name)
            for name, field_name in _MESH_SHEET_PARAMS.items()
            if getattr(mesh, name, None) is not None}


def _translate(values: Mapping[str, Any], table: Mapping[str, str]) -> dict[str, Any]:
    """Rename slot members onto the author's own keyword names."""
    return {table.get(k, k): v for k, v in values.items()}


def _refuse_uncovered_fields(fields: Mapping[str, Any], writer: Callable[..., Any],
                             process: str) -> None:
    """The slots and the author's signature must AGREE, in both directions.

    An UNKNOWN key vanishes - the run is authored without it and answers a
    different question than the template declared. A MISSING required key is the
    mirror image and costs more: the plan builds, the acquire stage geocodes and
    fetches, and only then does the writer die on a TypeError.
    """
    signature = inspect.signature(writer).parameters
    accepted = set(signature)
    unknown = sorted(set(fields) - accepted)
    if unknown:
        raise PlanValidationError(
            f"the {process!r} declaration names {unknown}, which "
            f"{writer.__name__} does not accept (accepted: {sorted(accepted)})."
        )
    required = sorted(
        name for name, prm in signature.items()
        if prm.kind is inspect.Parameter.KEYWORD_ONLY
        and prm.default is inspect.Parameter.empty
    )
    missing = [name for name in required if name not in fields]
    if missing:
        raise PlanValidationError(
            f"the {process!r} declaration covers no {missing}, which "
            f"{writer.__name__} requires (required: {required})."
        )
