"""The workflow SKELETON: what every declared workflow runs on.

A template file declares a workflow; :class:`Workflow` IS one, and owns the
fill-then-launch spine, post and publish, and the registration factory.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Collection, Iterator, Mapping, NoReturn, Sequence

from trid3nt_contracts.common import Origin, SyntheticInput
from trid3nt_contracts.coverage import SourceChoice

from trid3nt.inputs.bound_domain import bind_domain, reset_domain
from trid3nt.inputs.input import Input, declared, hydrate
from trid3nt.inputs.run_notes import (
    bind_coverage, bind_notes, drain_coverage, drain_notes)
from trid3nt.inputs.slots import BED, DOMAIN, Slot
from trid3nt.inputs.user_input import WireArgsError
from trid3nt.settings import bind_run, clear_session_run, reset_run
from trid3nt.workflows.solver.stop import SolveStopped, Stopped, finish

from . import journal
from .errors import DeclarationError, DeclarativeError, said
from .resolution import SensitivityDecl, sensitivity_notes
from .provenance import merge_provenance, provenance_entries

if TYPE_CHECKING:
    from pathlib import Path

    from .fill import Fill
    from .primitives import Primitive

__all__ = ["BANKS", "Deck", "EVENT_TIME", "REACH_LENGTH", "RunResult", "Said", "Workflow",
           "register_workflow", "unregistered"]

logger = logging.getLogger("trid3nt.workflows.runtime.workflow")

#: The moment a run of this engine opens at: what every dated source is asked for and every record is read at.
#: A workflow whose rows read a record (a record-slot need) lists it as required, so a call omitting it is
#: refused naming it; one reading no record leaves it stated-only.
EVENT_TIME = Input(
    name="event_time",
    desc="'now', an ISO date or datetime, or 'none' when the ask names no time and every observation is "
         "stated. Phrasing like 'during last Tuesday's storm' is a date, never 'now'. Each source keeps its own "
         "retention, and a request deeper than one refuses typed").stated()


#: How far down the line the domain reaches from the seed; a workflow whose domain need states a span lists it, with that span as its description's default.
REACH_LENGTH = Input(
    name="reach_length_km", type=float, units="km",
    desc="Leave unset unless the user names a reach length; the template's own span is used. How far downstream "
         "of the release or seed point the domain reaches, in km, greater than 0; the line's own end cuts a "
         "longer one short. A longer reach is a bigger mesh and a longer solve").stated()


#: The water surface a river domain is cut from, when the run hands one instead of the fetch it would make.
BANKS = Input(
    name="banks", geometry="polygon",
    desc="Leave unset unless the user hands the water surface of the reach: a polygon layer the reach is cut square "
         "from, used as it is in place of the mapped water surface the run would fetch").stated()


def _names_untaken(value: Any, untaken: set[str]) -> bool:
    return isinstance(value, str) and value in untaken


@dataclass
class RunResult:
    """What a launch produced: the published result, what each stage returned by
    name, and what the run collected on the way - its provenance rows, the notes
    it could not produce, the layers it published and the sources it matched."""

    value: Any
    results: dict[str, Any] = field(default_factory=dict)
    entries: list[SyntheticInput] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Every input's verdict by name, as the fill and the launch stamped them.
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    choices: list[SourceChoice] = field(default_factory=list)
    #: The settings the run read, by name, with the value in force and the level it resolved at.
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class Said:
    """What a run has said so far: its notes, the layers it published, the lists it filled from, what its cut
    covers and the settings it read."""

    notes: list[str] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    choices: list[Any] = field(default_factory=list)
    coverage: list[str] = field(default_factory=list)
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class Deck:
    """A launch stopped at its written, checked deck: the fill it was made from, the run it is tagged with, the
    staged run the box receives (its run directory, manifest and every file by its name in it), what the run said
    on the way and how long it took; ``held`` is what the engine's solve reads that the run's mapping does not hold.
    A launch that is not split writes its deck inside its solve, so its deck holds the fill alone."""

    state: Fill
    run_id: str | None = None
    prefix: str = ""
    staged: Mapping[str, Any] = field(default_factory=dict)
    held: dict[str, Any] = field(default_factory=dict)
    said: Said = field(default_factory=Said)
    seconds: float = 0.0


def _accuracies(value: Any) -> dict[str, Any]:
    """An ingested reading's published accuracy and its gauge zero's, as the observation carries them; none otherwise."""
    from trid3nt.inputs.observation import Observation

    if not isinstance(value, Observation):
        return {}
    return {name: asdict(getattr(value, name)) for name in ("accuracy", "zero_accuracy")
            if getattr(value, name) is not None}


class Workflow:
    """The universal skeleton. A template declares; this runs.
    A workflow declares its engine's facts - the solver family, its solve step's name
    and the input holding its moment; hooks have SILENT defaults and no subtype restates one."""

    engine: str = ""

    #: The SOLVE step's name; the skeleton reads the run prefix off it when the result carries none.
    solve_step: str = ""

    #: The name of the input holding the moment a run opens at; a runtime that dates nothing names none.
    moment: str = ""

    #: The engine stops its solve after the engine steps check_steps states; one that does not refuses the control.
    checkable: bool = False

    #: The engine lays its own ladder of bed surfaces and takes the finest at each point, so its bed slot
    #: takes one surface and neither merges nor fills.
    lays_own_bed: bool = False

    def input_schema(self) -> tuple[Path, str] | None:
        """Where this engine's template values read their schema off: the
        (directory, module) of the module input JSON, or None where a template
        declares alone."""
        return None

    def seated(self) -> tuple[Input, ...]:
        """The inputs a declaration of this workflow carries without restating
        them: what the type of each of its rows states beside itself. An engine
        adds its own; the skeleton declares none."""
        reach = next((row.span_km for row in self.inputs
                      if row.slot is DOMAIN and row.span_km is not None), None)
        # A domain found from a point with no kind of its own may stand in a river, which then walks a reach.
        river = any(row.slot is DOMAIN and row.data_class == "hydrography" and row.kind in ("", "flowline")
                    and row.coercion.get("near") for row in self.inputs)
        desc = (f"{REACH_LENGTH.desc}. This template reaches {reach:g} km" if reach is not None else
                "State it only when the user names a reach length: this template states none, so a run on a river "
                "asks for it on the review card and AUTO refuses without it. " + REACH_LENGTH.desc.split(". ", 1)[1]
                if river else "")
        return (tuple(stated for slot in dict.fromkeys(
            row.slot for row in self.inputs if row.slot is not None)
            for stated in slot.states)
            + (() if not desc else (replace(REACH_LENGTH, desc=desc), BANKS)))

    def __init__(self, *, metadata: Any, inputs: Any, template: Any,
                 sensitivity: Sequence[tuple[str, str]] | SensitivityDecl = (),
                 coerce: Sequence[Callable[[dict], Mapping[str, Any]]] = ()) -> None:
        self.metadata = metadata
        self.name = metadata.name
        #: The template MODULE: STEERING, OUTPUTS, CAPTIONS and its files are read off its own names.
        self.template = template
        #: THE ONE BODY: the template's inputs, then each one a row's type or the engine declares; a template's own wins.
        #: Values lead the wire and rows follow, each in body order. A value's
        #: schema and prose arrive from the module input JSON before it is declared.
        self.inputs = declared(hydrate(inputs, self.input_schema(), self.name))
        stated = {row.name for row in self.inputs}
        self.inputs = tuple(sorted(
            self.inputs + tuple(row for row in self.seated()
                                if row.name not in stated),
            key=lambda row: row.is_row))
        reads_moment = any(row.data_class and getattr(row.slot, "record", False) for row in self.inputs)
        # A need read off a record is asked for a window, and only a stated moment opens one.
        self.inputs = tuple(
            replace(row, stated_only=False) if reads_moment and row.name == self.moment and row.stated_only else row
            for row in self.inputs)
        #: What the template calls every published variable and every row it reads into; one dict.
        self.captions = dict(getattr(template, "CAPTIONS", {}) or {})
        #: Published reads in a resolution-sensitive class; see ``resolution.py``.
        self.sensitivity = (sensitivity if isinstance(sensitivity, SensitivityDecl)
                            else SensitivityDecl(sensitivity))
        self.coercions = tuple(coerce)
        self.error_prefix = str(getattr(metadata, "engine", "") or "workflow").upper()
        self.check()

    def input(self, name: str) -> Any:
        """THE ONE LOOKUP: the input a name states - the template's own, then one the
        engine or a row's type declares; an engine answers for its keywords after
        them, and a name nothing declares refuses."""
        found = next((row for row in self.inputs if row.name == name), None)
        if found is None:
            raise WireArgsError(f"{name!r} is not an input of {self.name}.")
        return found

    def bodies(self) -> tuple[str, ...]:
        """The module word each body of this run is qualified by in a keyword's name; the skeleton runs no body."""
        return ()

    def check(self) -> None:
        """Refuse at import what this template's declarations cannot run; the
        skeleton runs nothing of its own, so it refuses nothing."""

    async def prepare(self, state: Fill) -> Deck:
        """A READY fill launched up to its written, checked deck -> the deck; nothing is solved.
        The review card is drawn here, from the deck it holds. A launch that is not split writes nothing here."""
        return Deck(state)

    async def solve(self, deck: Deck) -> RunResult:
        """The deck ``prepare`` wrote, solved and read -> what the run produced.
        A launch that is not split runs whole here."""
        return await self.launch(deck.state)

    async def launch(self, state: Fill) -> RunResult:
        """Write, solve and read a READY fill in one piece -> what the run produced.

        The skeleton knows no engine, so it launches nothing."""
        raise WireArgsError(f"{self.name} declares no engine to launch.")

    async def _stage(self, run: dict[str, Any], label: str, fn: Any, /, **kwargs: Any) -> Any:
        """One stage of a launch: ``fn`` (or the function a dotted name loads) called under its own substep, its value
        held on the run's mapping by ``label``."""
        from trid3nt.render.emission import current_emitter, substep
        from .fill import _load, call

        async with substep(current_emitter(), label):
            run[label] = await call(fn if callable(fn) else _load(fn), kwargs, label)
        return run[label]

    def judged(self, state: Fill) -> dict[str, Any]:
        """What refuses the whole run before any stage of it, as a verdict under the name it is said by.
        An engine's run waits on a person, so one with no session to wait on refuses here; an engine adds its own."""
        from trid3nt.inputs.review import no_session
        from .fill import REJECTED, Verdict

        sentence = no_session(self.name) if self.engine else None
        refused = {} if sentence is None else {"AUTO": Verdict(REJECTED, None, reason=sentence, code="NO_SESSION")}
        steps = state.carried.get("check_steps")
        if steps is not None and (isinstance(steps, bool) or not isinstance(steps, int) or steps < 1):
            refused["check_steps"] = Verdict(
                REJECTED, steps, code="CHECK_STEPS_INVALID",
                reason=f"check_steps takes a whole number of engine steps, 1 or more; got {steps!r}.")
        elif steps is not None and not self.checkable:
            refused["check_steps"] = Verdict(
                REJECTED, steps, code="CHECK_STEPS_UNSUPPORTED",
                reason=f"{self.name} does not honour check_steps: leave it unset to run the full length.")
        return refused

    def unnamed(self) -> tuple[str, ...]:
        """Every input the template names as a plain string that nothing of
        this run is called; the fill refuses each by name."""
        return ()

    def named_rows(self, held: Collection[str] = ()) -> tuple[str, ...]:
        """Every declared row the fill produces, given the names the run already holds; the skeleton names none."""
        return ()

    def sheet_doc(self) -> str | None:
        """The ENGINE SURFACE line of this template's docstring, or nothing.

        Only the workflow knows which engine surface it fills, so a skeleton
        that fills none claims none."""
        return None

    def slot_units(self) -> Mapping[Slot, str]:
        """The UNIT each slot's value is converted to, by type.
        Fixed by the engine's transform for the keyword the type fills; a runtime writing no keywords states none."""
        return {}

    def published_units(self, stated: Mapping[str, Any] = MappingProxyType({})
                        ) -> Mapping[str, str]:
        """The UNIT each published variable is written in, by the name the result carries it under, off what the run ``stated``.
        A measurement and its variable compare in one unit only; a runtime publishing nothing named states none."""
        return {}

    def run_window_s(self, keywords: Mapping[str, Any]) -> float | None:
        """How long this run's solve covers, in seconds - the window a matched SERIES source must hold.
        The engine's deck states it; a runtime knowing no engine states none."""
        return None

    def checks(self, run: RunResult,
               inputs: Mapping[str, Mapping[str, Any]]) -> tuple[str, ...]:
        """Validation checks over the finished run, as NOTES the caller narrates.
        A template that declares no sensitivity classes produces no note, and a
        check reports - it never retracts a solved run."""
        return sensitivity_notes(self.sensitivity, self._published(run), inputs,
                                 mesh_size_m=self._mesh_size_m(run))

    def _published(self, run: RunResult) -> set[str]:
        """Every QUANTITY the run put on the map or a chart, by the name the publish stage wrote it under."""
        return {str(row.get("quantity") or "") for row in run.outputs} - {""}

    def _states(self, name: str, default: Any) -> Any:
        """One declaration read off the template module, or the default beside it."""
        return getattr(self.template, name, default)

    def _slots(self) -> dict[Any, str]:
        """The row this question declares for each type a stage reads: the first of each, and exactly one domain and one bed."""
        slots: dict[Any, list[str]] = {}
        for row in self.inputs:
            if row.slot is not None:
                slots.setdefault(row.slot, []).append(row.name)
        named = {row.name: row.slot for row in self.inputs}
        for slot in slots:
            if named.get(slot.name, slot) is not slot:
                # A module reads a row by the NAME of its type, so no other input takes that name.
                raise DeclarationError(
                    f"{self.name} declares a {slot.name} row and names another "
                    f"input {slot.name!r}; a module would read the wrong one.")
        for slot, why in ((DOMAIN, "a run solves over one polygon"),
                          (BED, "every node carries one elevation; a survey "
                                "over a wider surface is composed by the merge "
                                "derive into the one row")):
            rows = slots.get(slot) or []
            if len(rows) != 1:
                raise DeclarationError(
                    f"{self.name} declares {len(rows)} {slot.name} rows "
                    f"({rows}): {why}, so the INPUTS body needs exactly one "
                    f"Input({slot.name.upper()}) row.")
        return {slot: names[0] for slot, names in slots.items()}

    def _outputs(self) -> tuple[tuple[Primitive, ...], dict[str, str]]:
        """What this question PUBLISHES and what it calls each, checked: every
        listed read is published and has its caption."""
        outputs = tuple(self._states("OUTPUTS", ()))
        captions = dict(self._states("CAPTIONS", {}))
        for primitive in outputs:
            if primitive.publish is None:
                raise DeclarationError(
                    f"OUTPUTS lists {primitive.kind}({primitive.variable!r}) with "
                    "no .layer(), .chart(), .animate() or .note(); a listed "
                    "primitive is published.")
            named = primitive.variable or primitive.kind
            if named not in captions:
                raise DeclarationError(
                    f"OUTPUTS publishes {named!r} and CAPTIONS names no caption "
                    "for it.")
        return outputs, captions

    async def _recipe(self, env: Any, recipe: Any,
                      untaken: set[str]) -> dict[str, Any]:
        """The mesh ask with every input it names read off the run: the extent,
        a resolution stated as a name, and each op argument that takes one.
        An op set on a measurement the run did not take is not composed."""
        from trid3nt.tools.mesh.recipe import recipe_plan_value, takes_name
        from trid3nt.workflows.runtime.fill import read

        asked = recipe_plan_value(recipe)
        for key in ("extent", "resolution_m"):
            if isinstance(asked[key], str):
                asked[key] = await read(env, asked[key])
        composed = []
        for op, entry in zip(recipe.ops, asked["ops"]):
            if _names_untaken(op.kwargs.get("geometry"), untaken):
                continue
            entry["kwargs"] = {
                key: await read(env, value) if takes_name(recipe, op, key)
                else value for key, value in entry["kwargs"].items()}
            composed.append(entry)
        asked["ops"] = composed
        return asked


    async def filled(self, wire: Mapping[str, Any], *,
                     model: Mapping[str, str] | None = None,
                     steps: Mapping[str, Any] | None = None) -> Fill:
        """Every input ``wire`` states answered -> the fill; ``model`` names the SDK model it is a fill of, for the record,
        and ``steps`` the coverage steps it states for an input over the ones the input's row declares."""
        from .fill import Fill, fill

        # Each call starts from no domain: a domain bound by an earlier call of the same turn is not this run's place.
        token = bind_domain(None)
        try:
            return await fill(Fill(workflow=self, model=model, steps=dict(steps or {})), wire)
        finally:
            reset_domain(token)

    async def run(self, wire: Mapping[str, Any]) -> Any:
        """The absorbed tool body: fill every input, then launch when READY."""
        from .script import capture

        with capture(wire):
            return await self._run(wire)

    async def _run(self, wire: Mapping[str, Any]) -> Any:
        try:
            state = await self.filled(wire)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "retryable", False):
                raise
            logger.exception("%s fill failed", self.name)
            return self._error(f"{self.error_prefix}_INTERNAL_ERROR", exc)
        if not state.ready:
            code, sentence = state.refusal()
            logger.warning("%s %s: %s", self.name, code, sentence)
            return {"status": "error", "error_code": code,
                    "error_message": sentence}
        return await self.execute(state)

    async def execute(self, state: Fill, deck: Deck | None = None) -> Any:
        """Launch a READY fill - or solve the ``deck`` already written from it - then post and publish what it produced."""
        from .script import Executed

        started = time.monotonic() - (deck.seconds if deck is not None else 0.0)
        try:
            run = await (self._solved(deck) if deck is not None else self._launched(state))
        except Stopped as stop:
            if not stop.recorded:
                await asyncio.to_thread(self._journal_stop, stop, time.monotonic() - started,
                                        dict(state.env.supplied), state.model)
            raise
        except asyncio.CancelledError:
            raise
        except DeclarativeError as exc:
            logger.warning("%s %s: %s", self.name, exc.error_code, exc)
            return self._error(exc.error_code, exc)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "retryable", False):
                # A retryable typed error is a GATE: its .suggestions are harvested off the RAISED exception; an envelope destroys that channel.
                raise
            logger.exception("%s unexpected failure", self.name)
            return self._error(f"{self.error_prefix}_INTERNAL_ERROR", exc)
        if isinstance(run, Executed):
            return run.value
        run.notes[:0] = [*state.notes, *state.moved]
        run.settings = {**state.settings_read, **run.settings}
        return await self._publish(run, time.monotonic() - started,
                                   supplied=dict(state.env.supplied), model=state.model)

    async def _launched(self, state: Fill) -> Any:
        """The whole launch: the deck written, then solved."""
        from .script import archive, execute

        deck = await self.prepared(state)
        await archive(deck)
        solved = await execute(deck)
        # A launch whose script was refused solves its prepared deck here.
        return await self._solved(deck) if solved is None else solved

    async def prepared(self, state: Fill) -> Deck:
        """A READY fill launched up to its written, checked deck, under the run's own domain and collectors -> the deck,
        carrying what the run said on the way. A launch that refuses carries its notes on the error."""
        from .fill import production, restate

        production(state)
        started = time.monotonic()

        async def step() -> Deck:
            restate(state)
            return await self.prepare(state)

        deck, heard = await self._within(state, Said(), step)
        deck.said, deck.seconds = heard, time.monotonic() - started
        return deck

    async def _solved(self, deck: Deck) -> RunResult:
        """Solve a written deck under the run's own domain and collectors, opened on what its writing said; notes ride out on the run or on the failure.
        A launch the user stopped is completed the same way and raised again as the stop, carrying the run."""
        stopped: Stopped | None = None

        async def step() -> RunResult:
            nonlocal stopped
            try:
                if "applied_study" in deck.held:
                    from trid3nt.sdk.study import solve_applied

                    return await solve_applied(deck)
                return await self.solve(deck)
            except Stopped as stop:
                stopped = stop
                return stop.run

        run, heard = await self._within(deck.state, deck.said, step)
        env = deck.state.env
        # Read after the launch: a value a person edited on the review card and a row first read by a stage are stamped by then.
        run.entries = provenance_entries(env.inputs, self.inputs)
        run.inputs = dict(env.inputs)
        run.notes += heard.notes
        run.settings = {**heard.settings, **run.settings}
        run.outputs += heard.outputs
        run.choices += heard.choices
        if stopped is not None:
            raise stopped
        return run

    async def _within(self, state: Fill, held: Said, step: Callable[[], Awaitable[Any]]) -> tuple[Any, Said]:
        """``step`` under the run's own domain and collectors, each opened on what the run already said (``held``) ->
        its value and all the run has said by its end. An error leaves carrying every note the run said."""
        token = bind_domain(state.domain)
        notes, outputs = bind_notes(held.notes), journal.bind_outputs(held.outputs)
        choices, coverage = journal.bind_choices(held.choices), bind_coverage(held.coverage)
        reading, run_token = journal.bind_settings(held.settings), bind_run(state.settings)
        heard = held
        # The drain lives only in the finally so a second reset never replaces the run's own error.
        try:
            try:
                value = await step()
            finally:
                reset_domain(token)
                heard = Said(notes=drain_notes(notes), outputs=journal.drain_outputs(outputs),
                             choices=journal.drain_choices(choices), coverage=drain_coverage(coverage),
                             settings=journal.drain_settings(reading))
                reset_run(run_token)
        except Exception as exc:
            for note in heard.notes:
                exc.add_note(f"also missing from this run: {note}")
            raise
        return value, heard

    async def stopped(self, stop: SolveStopped, run: dict[str, Any],
                      products: Callable[[dict[str, Any], str], Awaitable[Any]]) -> NoReturn:
        """The user stopped the solve: publish what the run wrote, every layer and the record labelled partial, then end as the stop.
        ``products`` is the engine's own publish of a run handle; a run whose output cannot be read honestly publishes nothing and says why."""
        from trid3nt.inputs.run_notes import journal_note

        why = stop.sentence()
        run[self.solve_step] = stop.solved
        value = None
        try:
            value = await finish(products(stop.solved, why))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: nothing of the stopped run %s was published: %s", self.name, stop.run_id, said(exc))
            journal_note(f"{why}; nothing of what it wrote was published: {said(exc)}")
        raise Stopped(RunResult(value=value, results=dict(run)), value, why)

    def _journal_stop(self, stop: Stopped, wall_seconds: float, supplied: Mapping[str, Any],
                      model: Mapping[str, str] | None = None) -> None:
        """The record of a stopped run: what it published and what it was asked, labelled partial."""
        run = stop.run
        self._journal(self._run_id(stop.value, run), run, stop.value, wall_seconds, [*run.notes, stop.why],
                      self._inputs(run, supplied), stopped=stop.why, model=model)

    def _error(self, code: str, exc: BaseException) -> dict[str, Any]:
        """The failure, plus whatever auxiliary outputs the run also lost on the way."""
        notes = getattr(exc, "__notes__", ()) or ()
        # An exception that stringifies to nothing reports its type; the sentence is all a card prints.
        return {"status": "error", "error_code": code,
                "error_message": " ".join([said(exc), *notes])}


    async def _publish(self, run: RunResult, wall_seconds: float = 0.0, *,
                       supplied: Mapping[str, Any] | None = None,
                       model: Mapping[str, str] | None = None) -> Any:
        result = run.value
        inputs = self._inputs(run, supplied or {})
        notes = list(run.notes) + [n for n in self.checks(run, inputs) if n]
        # The tie view: sources ranked equal on every fact travel as rows so the model or user picks; a clear winner carries no table.
        notes += [_ranked_rows(choice) for choice in run.choices if choice.tie]
        update: dict[str, Any] = {
            "synthetic_inputs": merge_provenance(
                getattr(result, "synthetic_inputs", None) or [], run.entries),
        }
        if notes:
            update["notes"] = [*(getattr(result, "notes", None) or []), *notes]
        result = result.model_copy(update=update)

        run_id = self._run_id(result, run)
        # The journal takes the MERGED notes: a label only on the layer would vanish with it.
        await asyncio.to_thread(self._journal, run_id, run, result,
                                wall_seconds, notes, inputs, model=model)
        logger.info("%s complete layer_id=%s stages=%s notes=%s",
                    self.name, getattr(result, "layer_id", None),
                    list(run.results), notes)
        return result

    def _run_id(self, result: Any, run: RunResult) -> str | None:
        """The solve's run prefix from the layer or the declared ``solve_step``; none without one."""
        direct = getattr(result, "run_id", None)
        if direct or not self.solve_step:
            return direct
        return (run.results.get(self.solve_step) or {}).get("run_id")

    def _mesh_size_m(self, run: RunResult) -> Any:
        """The edge the run was meshed at, off the solve step's record; none without a solve step."""
        if not self.solve_step:
            return None
        return (run.results.get(self.solve_step) or {}).get("mesh_size_m")

    def _module(self, run: RunResult) -> str | None:
        """Which module of the engine ran, as the solve step states it."""
        if not self.solve_step:
            return None
        return (run.results.get(self.solve_step) or {}).get("module")

    def _inputs(self, run: RunResult,
                supplied: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """THE RUN'S INPUTS BY NAME - value, origin, source: every keyword of the solved deck off the
        solve step's sheet, then every input the fill answered that the deck does not already carry.
        A row's value is what the caller supplied for it; a matched one holds none and names its source."""
        sheet = (run.results.get(self.solve_step) or {}).get("sheet") or {} \
            if self.solve_step else {}
        inputs: dict[str, dict[str, Any]] = {}
        for name, row in (sheet.get("filled") or {}).items():
            origin, _, source = str(row.get("provenance") or "").partition(": ")
            inputs[name] = {"value": row.get("value"), "origin": origin,
                            "source": source or None}
        for name, verdict in run.inputs.items():
            if verdict.origin is None:
                continue
            held = self.input(name)
            # A keyword of the carrier's deck is that deck's own row; one stated on a coupled body keeps the name it was stated under.
            if verdict.origin not in (Origin.STUDY, Origin.FILE) and getattr(held, "carried", False) and held.name in inputs:
                continue
            inputs[name] = {
                "value": (supplied.get(name) if getattr(held, "is_row", False)
                          else verdict.value),
                "origin": verdict.origin.value, "source": verdict.source or None,
                **_accuracies(verdict.value)}
        return inputs

    def recorded_engine(self, run: RunResult) -> str | None:
        """The engine the run's record names: this workflow's own solver family."""
        return self.engine or None

    def _correct_end(self, run: RunResult) -> bool | None:
        """Did the solver reach its own correct end? None where the metrics say nothing."""
        if not self.solve_step:
            return None
        metrics = (run.results.get(self.solve_step) or {}).get("metrics") or {}
        flag = metrics.get("correct_end")
        return None if flag is None else bool(flag)

    def _journal(self, run_id: str | None, run: RunResult, result: Any,
                 wall_seconds: float, notes: Sequence[str],
                 inputs: Mapping[str, Mapping[str, Any]], *, stopped: str | None = None,
                 model: Mapping[str, str] | None = None) -> None:
        """Append this run to the run journal, from publish where the inputs and wall time are both in hand.
        ``model`` is the SDK model the run was filled from, its name and version; a call carries none."""
        from trid3nt.inputs.coverage import is_strict
        from trid3nt.render.emission import current_emitter, current_turn_case

        from .script import current

        from trid3nt.sdk.script_run import current as executing_script

        script = current()
        child = executing_script()
        record = journal.build_record(
            run_id=run_id, engine=self.recorded_engine(run), template=self.name,
            case_script=child.receipt() if child is not None else script.receipt if script is not None else None,
            case=current_turn_case(), model=(model or {}).get("name"), version=(model or {}).get("version"),
            strict=is_strict(),
            template_path=(model or {}).get("template_path"), template_sha256=(model or {}).get("template_sha256"),
            template_upstream_sha256=(model or {}).get("template_upstream_sha256"),
            module=self._module(run), inputs=inputs,
            correct_end=self._correct_end(run),
            result=result, wall_seconds=round(wall_seconds, 3),
            origin=journal.run_origin(live_session=child is not None or current_emitter() is not None),
            notes=list(notes), outputs=run.outputs,
            mesh=run.results.get("mesh"), settings=run.settings, stopped=stopped,
            check_steps=run.results.get("check_steps"), study=run.results.get("study"),
        )
        if child is not None and child.handoff.get("runner"):
            record["runner"] = child.handoff["runner"]
        journal.append_record(record)
        if child is not None:
            child.record = record
            child.value = result
        clear_session_run()


#: Controls every workflow carries: whether a kept mesh is rebuilt, the raw keyword floor,
#: and the source a caller names for a matched slot. None is a physical value, so none is an Input.
_CONTROLS: tuple[tuple[str, Any, Any], ...] = (
    ("restart_clean", bool, False),
    ("check_steps", int | None, None),
    ("keywords", dict | None, None),
    ("picks", dict | None, None),
)

#: What each control does, stated once here rather than restated per template. A control is
#: never a physical value, so neither sentence ever offers a range or a default.
_CONTROL_DOCS: tuple[tuple[str, str], ...] = (
    ("restart_clean",
     "True builds the mesh again even where one built from the same domain, "
     "bed, resolution and mesher is kept; unset, such a kept mesh is reused. "
     "Not a physical value."),
    ("check_steps",
     "A check run: the solve stops after this many engine steps, then the outputs "
     "it wrote are read, rendered and delivered marked as a check run; unset, the "
     "run is the full length. An engine that does not declare it refuses. "
     "Not a physical value."),
    ("keywords",
     "The engine's own keyword names this run states beyond the template, as "
     "a mapping of name to value; a run's statement beats the template's. A "
     "name is this engine's keyword, identifier or composite, or a qualified "
     '"module: keyword" for a body this run couples (gwt: '
     "IMS_PRINT_OPTION); find them with describe_keywords. A bare name two "
     "bodies spell refuses naming both: name the body it belongs to."),
)


#: The wire's one-line description of each control, unit or shape first; the framework's, stated once for every template.
_CONTROL_SCHEMA: dict[str, str] = {
    "restart_clean": "true/false; build the mesh again even where a kept one matches",
    "check_steps": "integer, 1 or more engine steps; stop the solve after that many and deliver a marked check run",
    "keywords": "object, keyword name to value; the engine keywords this run states beyond the template",
    "picks": "object, input name to source; the source this run names for a matched input",
}


def _routing_doc(template: Any) -> dict[str, str]:
    """A template's own prose, off its package's routing.yaml: summary, routing,
    not_for and returns. The file is the declaration - a template package
    without one refuses at import rather than registering a mute tool."""
    from pathlib import Path

    import yaml

    from trid3nt.inputs.input import DeclarationError

    package = Path(template.__file__).parent
    path = package / "routing.yaml"
    if not path.is_file():
        raise DeclarationError(
            f"{package.name} declares no routing.yaml beside its template.")
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    missing = {"summary", "routing", "returns"} - set(doc)
    if missing:
        raise DeclarationError(
            f"{package.name}/routing.yaml is missing {sorted(missing)}.")
    return doc


#: Bound while a template module is loaded for a case: each workflow its declaration builds, none registered.
_UNREGISTERED: contextvars.ContextVar[list[Workflow] | None] = contextvars.ContextVar(
    "unregistered", default=None)


@contextmanager
def unregistered() -> Iterator[list[Workflow]]:
    """Inside, ``register_workflow`` builds each declared workflow and registers no tool, reading no routing prose;
    the list holds every workflow built, in order."""
    built: list[Workflow] = []
    token = _UNREGISTERED.set(built)
    try:
        yield built
    finally:
        _UNREGISTERED.reset(token)


# A template answers one question on one engine from its own declarations; a question spanning templates or fetchers
# is composed by the model, never a wrapper tool (a wrapper fixes the chain and hides its judgment from the model).
def register_workflow(
    facade: type[Workflow],
    metadata: Any,
    template: Any,
    *,
    sensitivity: Sequence[tuple[str, str]] | SensitivityDecl = (),
    coerce: Sequence[Callable[[dict], Mapping[str, Any]]] = (),
    **register_kwargs: Any,
) -> Callable[..., Any]:
    """Generate and register the tool for a declared workflow.
    The template module is the declaration (INPUTS); its prose is the package's routing.yaml,
    and the signature is synthesized from the declared inputs, so the model-facing schema
    comes from the declaration the run resolves."""
    held = _UNREGISTERED.get()
    doc = _routing_doc(template) if held is None else {}
    workflow = facade(metadata=metadata, inputs=getattr(template, "INPUTS"),
                      template=template, sensitivity=sensitivity, coerce=coerce)

    async def _run(**wire: Any) -> Any:
        return await workflow.run(wire)

    _run.workflow = workflow  # type: ignore[attr-defined]
    if held is not None:
        held.append(workflow)
        return _run

    _run.__name__ = workflow.name
    _run.__qualname__ = workflow.name
    _run.__module__ = getattr(template, "__name__", __name__)
    sig, annotations = _wire_signature(workflow.inputs)
    _run.__signature__ = sig  # type: ignore[attr-defined]
    _run.__annotations__ = dict(annotations)
    _run.param_descriptions = {  # type: ignore[attr-defined]
        **{row.name: row.schema_description for row in workflow.inputs if row.fills_from_user},
        **_CONTROL_SCHEMA}
    from .docstring import render_docstring

    # The doc describes THIS wire, so it renders from the inputs the signature carries; the SHEET line is the
    # workflow's own, so a template cannot drift from it. The controls are the framework's,
    # stated once beside the signature they ride on.
    sheet = workflow.sheet_doc()
    doc = {**doc, "inputs": workflow.inputs, "controls": _CONTROL_DOCS,
           "gathers": not getattr(workflow, "bare", False),
           **({} if sheet is None else {"sheet": sheet})}
    _run.__doc__ = render_docstring(**doc)
    _run.routing_doc = render_docstring(**doc, view="routing")  # type: ignore[attr-defined]

    register_kwargs.setdefault("read_only_hint", False)
    register_kwargs.setdefault("open_world_hint", False)
    register_kwargs.setdefault("destructive_hint", False)
    register_kwargs.setdefault("idempotent_hint", False)
    from trid3nt.tools import register_tool

    return register_tool(metadata, **register_kwargs)(_run)


def _wire_signature(inputs: Sequence[Input]) -> tuple[inspect.Signature, dict]:
    """The generated tool's signature: every input on the wire, then the controls.
    The ``**`` absorber carries an unknown key to the fill, which refuses it by name; without it the dispatch drops the key unsaid."""
    # A required input's annotation admits no None, which is what lists it in the tool schema's ``required``;
    # a producer-less row is on the wire because a caller naming the layer is the only way it is filled.
    entries: list[tuple[str, Any, Any]] = [
        (row.name, row.wire_type if row.required else row.wire_type | None, None)
        for row in inputs if row.fills_from_user]
    entries += list(_CONTROLS)
    seen: set[str] = set()
    sig_params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for name, ann, default in entries:
        if name in seen:
            raise WireArgsError(f"the wire declares {name!r} twice.")
        seen.add(name)
        annotations[name] = ann
        sig_params.append(inspect.Parameter(
            name, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=default,
            annotation=ann))
    sig_params.append(inspect.Parameter("_extra_ignored",
                                        inspect.Parameter.VAR_KEYWORD, annotation=Any))
    annotations["_extra_ignored"] = Any
    annotations["return"] = Any
    return inspect.Signature(sig_params, return_annotation=Any), annotations


def _ranked_rows(choice: Any) -> str:
    """The ranked list as the tool result carries it: one line per source so a model can answer with a row."""
    rows = "; ".join(
        f"{index + 1}) {row.fetcher} - {row.resolution}, {row.recency}, "
        f"{row.datum}, {row.extent}"
        for index, row in enumerate(choice.rows))
    return (f"{choice.slot}: several sources rank equal for {choice.need}, and "
            f"{choice.picked} was taken. {rows}. Name another by its row to "
            "re-run on it.")
