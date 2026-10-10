"""The sheet: a module's slots, what filled each one, and the two acts on it.

Resolution order, lowest to highest: the engine default (never written), the template, the fill.
OPEN is informational; REQUIRED (the dictionary's OBLIG files) is what a run refuses on.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .module import Output, Slot, SlotRefused

__all__ = ["CONTINUATION", "Filled", "Origin", "Provenance", "Sheet", "SheetIncomplete",
           "fill", "fill_coupled", "filled_by", "run", "solve_cores",
           "tracer_text"]


class Origin(str, Enum):
    """Where a filled slot's value came from. A closed set; nothing downstream branches on it."""

    TEMPLATE = "template"
    USER = "user"
    MODEL = "model"
    PRODUCER = "producer"
    DERIVED = "derived"
    CALIBRATED = "calibrated"


@dataclass(frozen=True, slots=True)
class Provenance:
    """One slot's origin, and the name that makes it checkable.

    ``detail`` is the template, producer, source slot or calibration run the origin points at.
    """

    origin: Origin
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.origin.value}: {self.detail}" if self.detail \
            else self.origin.value


class SheetIncomplete(SlotRefused):
    """A run was asked for on a sheet whose REQUIRED slots are not all filled."""

    error_code = "TELEMAC_SHEET_INCOMPLETE"


def tracer_text(declared: Any) -> tuple[str, str]:
    """A tracer's name and unit off the 32-character text a deck writes: name in the first sixteen, unit after."""
    padded = str(declared).ljust(32)
    return (padded[:16].strip(), padded[16:].strip())


@dataclass(frozen=True, slots=True)
class Filled:
    """One filled slot: the value, and where in the resolution order it came from."""

    slot: Slot
    value: Any
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class Sheet:
    """A module's slots as they stand: what is filled, and what is still open."""

    # The body the sheet was filled from - a template, or the bare wrapper.
    body: type
    filled: Mapping[str, Filled]
    # Files a composite named, by basename: content the serializer writes beside the steering file naming them.
    files: Mapping[str, Any] = MappingProxyType({})
    # The run's mapping the sheet was filled against, read by name by a coupled body's composites.
    run: Mapping[str, Any] = MappingProxyType({})

    @property
    def module(self) -> str:
        return self.body.MODULE

    def open(self) -> tuple[Slot, ...]:
        """Every keyword the dictionary gives no default for and nothing has set.

        Informational and complete: what this run leaves to the engine.
        """
        return tuple(slot for name, slot in self.body.MODULE_INPUT.items()
                     if slot.is_open and name not in self.filled)

    def required(self) -> tuple[Slot, ...]:
        """The open slots a run cannot begin without: the dictionary's OBLIG files."""
        return tuple(slot for slot in self.open() if slot.is_required)

    @property
    def coupled(self) -> tuple[Mapping[str, Any], ...]:
        """The coupled bodies this deck names, in the order it names them."""
        return tuple(content for content in self.files.values()
                     if isinstance(content, Mapping) and "slots" in content)

    @property
    def tracers(self) -> tuple[Output, ...]:
        """Every tracer this run's result carries: the deck's declared ones, then each coupled module's appended ones.

        A name is 32 characters: the name in the first 16, the unit after.
        """
        from . import wrapper_for

        row = self.body.MODULE_OUTPUT.get(self.body.TRACER)
        rows = []
        for declared in dict(self.resolved()).get("NAMES OF TRACERS") or ():
            name, unit = tracer_text(declared)
            # A tracer the deck names is a quantity it put into the domain unless the putting module says otherwise; a carrier's row states edge and injection for its own.
            rows.append(Output(name=name, unit=unit,
                               style=row.style if row is not None else None,
                               has_edge=True if row is None or row.has_edge is None
                               else row.has_edge,
                               injected=True if row is None or row.injected is None
                               else row.injected))
        for body in self.coupled:
            appends = wrapper_for(body["module"]).APPENDS
            for appended in (list(appends(body)) if appends is not None else []):
                # Adopted, not appended: the engine adds a module's tracer only when none carries that name in its first sixteen characters, so the carrier's NAME and UNIT stay (the result file carries them) and the module attaches its process. The STYLE is the appending module's; the carrier's is generic.
                held = next((n for n, row in enumerate(rows)
                             if row.name == appended.name), None)
                if held is None:
                    rows.append(appended)
                else:
                    # The appending module attached the process, so its style and edge describe the quantity.
                    rows[held] = replace(
                        rows[held],
                        style=(appended.style if appended.style is not None
                               else rows[held].style),
                        has_edge=(appended.has_edge
                                  if appended.has_edge is not None
                                  else rows[held].has_edge),
                        injected=(appended.injected
                                  if appended.injected is not None
                                  else rows[held].injected))
        return tuple(rows)

    def kept(self) -> tuple[str, ...]:
        """The result files this run's decks name beside the one it is read from.

        A module may write several (TOMAWAC's polar spectra beside its 2D field); a named file the run threw away is unopenable.
        """
        from . import wrapper_for

        decks = [(self.body, dict(self.stated()))]
        decks += [(wrapper_for(body["module"]), dict(body.get("slots") or {}))
                  for body in self.coupled]
        return tuple(str(stated[name]) for body, stated in decks
                     for name in body.RESULT_FILES if stated.get(name))

    def user_code(self) -> tuple[str, ...]:
        """The directories this run's decks name their own user Fortran by.

        The engine compiles the directory each deck states, so a coupled module's patch is staged off its own deck.
        """
        named = [dict(self.resolved()).get("FORTRAN FILE")]
        named += [dict(body.get("slots") or {}).get("FORTRAN_FILE")
                  for body in self.coupled]
        return tuple(str(one) for one in named if one)

    def printouts(self) -> Mapping[str, str]:
        """This deck's variables keyword, generated from its module's table.

        The deck is handed to the generator because a row that exists only under a keyword is written for a deck that states it.
        """
        return self.body.printouts(tracers=len(self.tracers),
                                   stated=self.stated())

    def published(self) -> tuple[tuple[str, str, Output], ...]:
        """Every variable this run's results carry, as ``(token, module, row)``.

        The host's table first, then its tracers by position, then each coupled module's - the engine's write order.
        """
        from . import wrapper_for

        body = self.body
        stated = self.stated()
        rows = [(token, self.module, row)
                for token, row in body.table(stated).items()
                if token not in body.LISTING and token != body.TRACER]
        # A tracer is read by position among the carrier's own, the token primitives spell whatever the keyword calls it.
        rows += [(f"T{n}", self.module, row)
                 for n, row in enumerate(self.tracers, start=1)]
        for coupled in self.coupled:
            wrapper = wrapper_for(coupled["module"])
            under = dict(coupled.get("slots") or {})
            rows += [(token, coupled["module"], row)
                     for token, row in wrapper.table(under).items()
                     if token not in wrapper.LISTING and token != wrapper.TRACER]
        return tuple(rows)

    def stated(self) -> Mapping[str, Any]:
        """What this deck states, by identifier - the name a row's condition and a keyword are both written under."""
        return MappingProxyType({name: row.value
                                 for name, row in self.filled.items()})

    def resolved(self) -> tuple[tuple[str, Any], ...]:
        """``(keyword, value)`` for everything the deck states, in dictionary order.

        An engine default is never among them.
        """
        return tuple((row.slot.keyword, row.value)
                     for name, row in _in_dictionary_order(self.body, self.filled))

    def state(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "body": self.body.__name__,
            "filled": {name: {"keyword": row.slot.keyword, "value": row.value,
                              "provenance": str(row.provenance)}
                       for name, row in _in_dictionary_order(self.body, self.filled)},
            "files": sorted(self.files),
            "open": [{"keyword": slot.keyword, "identifier": slot.identifier,
                      "desc": slot.desc, "type": slot.type,
                      "choices": slot.choices, "required": slot.is_required}
                     for slot in self.open()],
        }


def _in_dictionary_order(body: type,
                      filled: Mapping[str, Filled]) -> list[tuple[str, Filled]]:
    return [(name, filled[name]) for name in body.MODULE_INPUT if name in filled]


def fill(source: type | Sheet, *, template: str = "",
         produced: Mapping[str, Any] | None = None,
         params: Mapping[str, Any] | None = None,
         settled: Mapping[str, Any] | None = None, inputs_fill: bool = True,
         **slots: Any) -> Sheet:
    """Set slots on a body or on a sheet already filled -> the sheet that results.

    ``produced`` and ``params`` are the run's one mapping (a param over the same name wins); ``settled`` is
    what the settle filled by keyword, and a template-stated keyword wins over it and over inputs. An unknown keyword refuses by name; None states nothing.
    """
    from ..authoring.atmosphere import write_atmosphere

    run_ = {**(produced or {}), **(params or {})}
    body, standing, pending = _standing(source, template)
    dictionary = body.MODULE_INPUT
    composites = body.COMPOSITES
    for name, value in slots.items():
        if name not in composites:
            body.slot(name)      # refuses by name, and names the nearest keyword
        pending[name] = (value, Provenance(Origin.USER))

    filled = {name: Filled(slot=dictionary[name], value=dictionary[name].check(value),
                           provenance=Provenance(Origin.DERIVED, "the settle"))
              for name, value in (settled or {}).items()
              if value is not None and name in dictionary}
    # A template-stated value wins over the settle and the inputs because standing merges last; a pending one wins in the loop below.
    filled |= {name: Filled(slot=dictionary[name], value=dictionary[name].check(value),
                            provenance=Provenance(Origin.DERIVED, source))
               for source, name, value in (filled_by(body, run_) if inputs_fill
                                           else ())} | standing
    files: dict[str, Any] = dict(source.files) if isinstance(source, Sheet) else {}
    # Keywords first, then composites in stated order: a composite reads the keywords so far by name, never another composite, and a keyword stated after it wins.
    order = list(pending)
    for name, (value, provenance) in sorted(
            pending.items(), key=lambda item: item[0] in composites):
        if value is None:
            # None means this run does not state it (no keyword's value is None), so the dictionary's default is what the engine reads.
            filled.pop(name, None)
            continue
        if name in composites:
            held = {**run_, **{key: row.value for key, row in filled.items()}}
            expanded, named = composites[name].apply(value, held)
            later = set(order[order.index(name) + 1:]) - set(composites)
            for key, item in expanded.items():
                if key in later:
                    continue
                slot = body.slot(key)
                filled[key] = Filled(
                    slot=slot, value=slot.check(item),
                    provenance=Provenance(Origin.PRODUCER, name))
            files.update(named)
            continue
        slot = dictionary[name]
        filled[name] = Filled(slot=slot, value=slot.check(value),
                              provenance=provenance)
    for basename, content in list(files.items()):
        if isinstance(content, Mapping) and content.get("filled_by"):
            files[basename] = _coupled_filled(content, run_)
    _arm(body, filled, files)
    _continued(body, filled, run_)
    _partitioned(body, filled, run_)
    write_atmosphere(body, {name: row.value for name, row in filled.items()},
                     files)
    return Sheet(body=body, filled=MappingProxyType(filled),
                 files=MappingProxyType(files), run=MappingProxyType(run_))


def filled_by(body: type, produced: Mapping[str, Any],
              only: Sequence[str] = ()) -> list[tuple[str, str, Any]]:
    """``(input, identifier, value)`` for every keyword an input of this run fills on ``body``; an absent input or None value fills nothing."""
    return [(source, name, value)
            for source, keywords in body.FILLED_BY.items()
            if (not only or source in only) and produced.get(source) is not None
            for name, read in keywords.items()
            if (value := read(produced[source])) is not None]


def _coupled_filled(content: Mapping[str, Any],
                    produced: Mapping[str, Any]) -> Mapping[str, Any]:
    from . import wrapper_for

    wrapper = wrapper_for(content["module"])
    slots = dict(content["slots"])
    for _source, name, value in filled_by(wrapper, produced,
                                          content["filled_by"]):
        slots.setdefault(name, wrapper.slot(name).check(value))
    return {**content, "slots": slots}


# What a continuing run states, by the keyword the engine reads its initial state from. Naming the file is the continuation: the engine opens at that file's LAST RECORD and ignores the deck's initial-condition statements. The format and record number are dictionary choices a deck states by name.
CONTINUATION = "PREVIOUS_COMPUTATION_FILE"


def _continued(body: type, filled: dict[str, Filled],
               produced: Mapping[str, Any]) -> None:
    """State the file this run picks its initial state up from, where the run is a continuation and the body reads one.

    The file is the run's previous-computation fill, staged by the settle; no deck declares it.
    """
    staged = (produced.get("settled") or {}).get("continue_from") \
        if isinstance(produced.get("settled"), Mapping) else None
    if not staged or CONTINUATION in filled or \
            CONTINUATION not in body.MODULE_INPUT:
        return
    slot = body.slot(CONTINUATION)
    filled[CONTINUATION] = Filled(
        slot=slot, value=slot.check(str(staged)),
        provenance=Provenance(Origin.PRODUCER, "the run this one continues"))


# The keyword every TELEMAC module spells for the processor count its domain is partitioned across; its default is one machine with no parallel library, so a serial run states nothing.
PROCESSORS = "PARALLEL_PROCESSORS"


# The matrix-solver keyword and the one value that is not partitioned: a DIRECT factorisation is solved whole on a single core whatever was asked; the parallel direct solver is another value and keeps the partition.
SOLVER = "SOLVER"
_DIRECT_SOLVER = 8


def solve_cores(body: type, filled: Mapping[str, Filled], cores: Any) -> int:
    """How many cores this deck's solve runs on.

    A deck resolving to the direct solver, stated or by default, runs serial whatever the run asked for.
    """
    from trid3nt_server.workflows.runtime.levers import cores_asked

    asked = cores_asked(cores)
    if asked is None:
        asked = _engine_cores(body)
    return 1 if asked > 1 and _solver(body, filled) == _DIRECT_SOLVER else asked


def _engine_cores(body: type) -> int:
    """The partition the module's own dictionary states, as a core count.

    The dictionary spells a scalar computation as zero processors; it still runs on one core.
    """
    slot = body.MODULE_INPUT.get(PROCESSORS)
    stated = None if slot is None or slot.is_open else slot.engine_default
    try:
        return max(1, int(stated))
    except (TypeError, ValueError):
        return 1


def _solver(body: type, filled: Mapping[str, Filled]) -> Any:
    """The solver this deck resolves to (stated, else the dictionary default); ``None`` where the dictionary spells none."""
    slot = body.MODULE_INPUT.get(SOLVER)
    if slot is None:
        return None
    return filled[SOLVER].value if SOLVER in filled else slot.engine_default


def _partitioned(body: type, filled: dict[str, Filled],
                 params: Mapping[str, Any]) -> None:
    """State how many cores this run is solved on, where the body spells it.

    The launcher is handed the same number, so the partition told to the engine is the one it gets.
    """
    from trid3nt_server.workflows.runtime import journal_note
    from trid3nt_server.workflows.runtime.levers import cores_asked

    if PROCESSORS in filled or PROCESSORS not in body.MODULE_INPUT:
        return
    asked = cores_asked(params.get("cores"))
    if asked is None:
        return
    cores = solve_cores(body, filled, asked)
    if cores < asked:
        journal_note(
            f"{body.MODULE} solves this deck with the direct solver "
            f"({SOLVER} {_DIRECT_SOLVER}), which factorises the whole system "
            f"rather than partitioning it, so the run is serial and the "
            f"{asked} cores it was asked for are not used.")
    if cores < 2:
        return
    slot = body.slot(PROCESSORS)
    filled[PROCESSORS] = Filled(slot=slot, value=slot.check(cores),
                                 provenance=Provenance(Origin.PRODUCER,
                                                       "the run's cores lever"))


def _arm(body: type, filled: dict[str, Filled],
         files: Mapping[str, Any]) -> None:
    """Turn on the term a stated value implies, where nothing has stated it.

    The engine reads the value only with its switch true, so a disarmed rate is a number nothing reads.
    A deck that states the switch itself keeps it, off included.
    """
    from . import wrapper_for

    implied = [(name, switch) for name, switch in body.ARMS.items()
               if name in filled]
    implied += [(f"coupling with {content['module']}", switch)
                for content in files.values()
                if isinstance(content, Mapping) and "slots" in content
                for switch in wrapper_for(content["module"]).ARMS_ON_HOST]
    for name, switch in implied:
        if switch in filled:
            continue
        slot = body.slot(switch)
        filled[switch] = Filled(slot=slot, value=slot.check(True),
                                provenance=Provenance(Origin.PRODUCER, name))


def fill_coupled(sheet: Sheet, stated: Mapping[str, Mapping[str, Any]]) -> Sheet:
    """Set slots on the run's coupled bodies -> the sheet that results.

    Keyed by module, then identifier; checked against that module's dictionary so a wrong value refuses
    here rather than in the Fortran. The body records which identifiers the run stated.
    """
    from . import wrapper_for

    files = dict(sheet.files)
    for module, slots in stated.items():
        basename = next(
            (name for name, content in files.items()
             if isinstance(content, Mapping) and "slots" in content
             and content.get("module") == module), None)
        if basename is None:
            raise SlotRefused(
                f"this run couples no {module} body, so there is no deck for "
                f"{', '.join(sorted(slots))} to be written into.")
        wrapper = wrapper_for(module)
        body = dict(files[basename])
        body["slots"] = {**body["slots"],
                         **{name: wrapper.slot(name).check(value)
                            for name, value in slots.items()}}
        body["stated"] = sorted(set(body.get("stated", ())) | set(slots))
        files[basename] = body
    return replace(sheet, files=MappingProxyType(files))


def _standing(source: type | Sheet, template: str = "",
              ) -> tuple[type, dict[str, Filled], dict[str, tuple[Any, str]]]:
    """What is on the sheet before this fill: the body's assertions, or a sheet. A composite is pending until the fill expands it."""
    if isinstance(source, Sheet):
        return source.body, dict(source.filled), {}
    standing: dict[str, Filled] = {}
    pending: dict[str, tuple[Any, str]] = {}
    # The template the value started in is the only place a template name survives a run; with none, the body is named.
    provenance = Provenance(Origin.TEMPLATE, template or source.__name__)
    for name, value in source.ASSERTED.items():
        slot = source.MODULE_INPUT.get(name)
        if slot is None or value is None:
            pending[name] = (value, provenance)
        else:
            standing[name] = Filled(slot=slot, value=value, provenance=provenance)
    return source, standing, pending


async def run(sheet: Sheet, *, dispatch: Callable[..., Any],
              mesh_inputs: Sequence[Mapping[str, str]],
              outputs: Sequence[str], results: Sequence[str], prefix: str,
              server_facts: Mapping[str, Any], steering: str | None = None,
              cores: int | None = None,
              coupling: str | None = None,
              continue_from: str | None = None) -> Any:
    """A complete sheet: serialize, stage, hand it to the box.

    Checked against the REQUIRED slots only; the box entry is the caller's.
    """
    # Past the OBLIG files the engine asks for by name in its own listing; a required set invented here would refuse runs it would take.
    from ..authoring.staging import new_rundir, stage_run
    from ..authoring.serializer import serialize

    unanswered = sheet.required()
    if unanswered:
        raise SheetIncomplete(
            f"{sheet.module} cannot run: "
            + "; ".join(f"{slot.keyword} ({slot.desc[:60]})"
                        for slot in unanswered))
    cadence = sheet.body.CADENCE
    if cadence and cadence not in sheet.filled:
        raise SheetIncomplete(
            f"{sheet.module} states no {sheet.body.slot(cadence).keyword}, and "
            "the dictionary's own default writes a frame every step. State the "
            "period this question's frames are written at.")
    # Files the decks name past the one read from are declared so the worker's success convention checks each landed.
    kept = [name for name in sheet.kept() if name not in results]
    run_tag, rundir = new_rundir()
    partition = solve_cores(sheet.body, sheet.filled, cores)
    # The serialization is a container round trip, so it runs off the loop.
    written = await asyncio.to_thread(serialize, sheet, rundir, steering=steering)
    staged = await stage_run(
        rundir, run_tag, module=sheet.module, steering=written["steering"],
        results=[*results, *kept], outputs=[*outputs, *kept],
        mesh_inputs=list(mesh_inputs), prefix=prefix, sheet=sheet.state(),
        result_basename=list(results)[0], server_facts=server_facts,
        # The engine compiles the directory its FORTRAN FILE statement names; the manifest carries the same word the decks do.
        user_fortran=sheet.user_code(),
        coupling=coupling, continue_from=continue_from,
        # The same number the steering file states: the launcher partitions the mesh across it.
        cores=partition)
    # The staged run and the box's answer are one handle so the caller need not join two results.
    return {**staged, **await dispatch(run=staged, cores=partition)}
