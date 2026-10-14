"""The TELEMAC sheet: the filled deck's engine side, and the stage that serializes it."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from trid3nt_contracts.common import Origin
from trid3nt_contracts.payload_warning import ParamSheetRow

from ...runtime.module import Output
from ...runtime.review import _group
from ...runtime.sheet import Filled, Provenance, Sheet, _placed, complete, filled_by
from .module import PROCESSORS, fits_the_box

__all__ = ["CONTINUATION", "TelemacSheet", "solve_cores", "stage", "tracer_text"]


def tracer_text(declared: Any) -> tuple[str, str]:
    """A tracer's name and unit off the 32-character text a deck writes: name in the first sixteen, unit after."""
    padded = str(declared).ljust(32)
    return (padded[:16].strip(), padded[16:].strip())


@dataclass(frozen=True, slots=True)
class TelemacSheet(Sheet):
    """A filled deck: the sheet, plus the tracers and listing only TELEMAC's modules carry."""

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

    def engine_rows(self) -> tuple[list[ParamSheetRow], set[str]]:
        """The card's engine sections: what each coupled deck states and what each deck writes, and the printouts keyword each deck generates."""
        return (_coupled_rows(self) + _written_rows(self),
                {body.PRINTOUTS for body, _, _ in _decks(self)})


def _decks(sheet: TelemacSheet) -> list[tuple[Any, list[str], Mapping[str, Any]]]:
    """Every body this run writes a deck for, its tracers, and what that deck states - the row a variable's condition is read against."""
    from . import wrapper_for

    return ([(sheet.body, [row.name for row in sheet.tracers], sheet.stated())]
            + [(wrapper_for(body["module"]), [], dict(body.get("slots") or {}))
               for body in sheet.coupled])


def _coupled_rows(sheet: TelemacSheet) -> list[ParamSheetRow]:
    """What each COUPLED deck of this run states, one group per deck in deck order.
    Read against that module's own dictionary, so the row carries its unit and bounds and the group names the body (two modules may spell one keyword).
    Not editable: the review's own filter answers for the carrier's sheet."""
    from . import wrapper_for

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
                origin=(Origin.USER if identifier in user
                        else Origin.DERIVED if identifier in body.get("derived", ())
                        else Origin.TEMPLATE),
                units=slot.unit or None, bounds=slot.whole_bounds,
                editable=False, group=f"{module}: {_group(slot)}",
                source_badge=(f"stated on this run as {module}: {slot.keyword}"
                              if identifier in user
                              else "read off this run's inputs"
                              if identifier in body.get("derived", ())
                              else f"the {module} deck this run couples")))
    return rows


def _written_rows(sheet: TelemacSheet) -> list[ParamSheetRow]:
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
            desc=slot.desc[:512], editable=False, group=_group(slot),
            source_badge=f"the {body.MODULE} module's own variable table"))
    return rows


def _coupled_filled(content: Mapping[str, Any],
                    produced: Mapping[str, Any]) -> Mapping[str, Any]:
    from . import wrapper_for

    wrapper = wrapper_for(content["module"])
    stated = dict(content["slots"])
    derived = {name: wrapper.slot(name).check(value)
               for _source, name, value in filled_by(wrapper, produced,
                                                     content["filled_by"])
               if name not in stated}
    return _placed(wrapper, {
        **content, "slots": {**stated, **derived},
        "derived": sorted({*content.get("derived", ()), *derived})})


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


# The matrix-solver keyword and the one value that is not partitioned: a DIRECT factorisation is solved whole on a single core whatever was asked; the parallel direct solver is another value and keeps the partition.
SOLVER = "SOLVER"
_DIRECT_SOLVER = 8


def solve_cores(body: type, filled: Mapping[str, Filled]) -> int:
    """How many cores this deck's solve runs on: the processors keyword as the deck resolves it.

    The dictionary spells a scalar computation as zero processors; it still runs on one core.
    """
    if PROCESSORS in filled:
        return max(1, int(filled[PROCESSORS].value))
    slot = body.MODULE_INPUT.get(PROCESSORS)
    return 1 if slot is None or slot.is_open else max(1, int(slot.engine_default))


def _solver(body: type, filled: Mapping[str, Filled]) -> Any:
    """The solver this deck resolves to (stated, else the dictionary default); ``None`` where the dictionary spells none."""
    slot = body.MODULE_INPUT.get(SOLVER)
    if slot is None:
        return None
    return filled[SOLVER].value if SOLVER in filled else slot.engine_default


def _partitioned(body: type, filled: dict[str, Filled]) -> None:
    """Hold the stated processor count to what this deck is solved on.

    The launcher is handed the same number, so a count the direct solver cannot use leaves the deck with the run's note.
    """
    from trid3nt_server.inputs.run_notes import journal_note

    if PROCESSORS not in filled:
        return
    asked = filled[PROCESSORS].value
    fits_the_box(asked)
    if int(asked) > 1 and _solver(body, filled) == _DIRECT_SOLVER:
        journal_note(
            f"{body.MODULE} solves this deck with the direct solver "
            f"({SOLVER} {_DIRECT_SOLVER}), which factorises the whole system "
            f"rather than partitioning it, so the run is serial and the "
            f"{int(asked)} processors it states are not used.")
        del filled[PROCESSORS]


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


async def stage(sheet: TelemacSheet, *, run_tag: str,
                mesh_inputs: Sequence[Mapping[str, str]],
                outputs: Sequence[str], results: Sequence[str], prefix: str,
                server_facts: Mapping[str, Any], steering: str | None = None,
                coupling: str | None = None,
                continue_from: str | None = None) -> dict[str, Any]:
    """A complete sheet: serialize it and stage the directory, checked against itself -> the staged run.

    ``run_tag`` names the run's one directory and prefix; a second staging replaces the first.
    Nothing is handed to the box here."""
    from ..authoring.serializer import serialize
    from ..authoring.staged_check import check_staged_run
    from ...solver.staging import new_rundir, stage_run
    complete(sheet.body, sheet.filled)
    # Files the decks name past the one read from are declared so the worker's success convention checks each landed.
    kept = [name for name in sheet.kept() if name not in results]
    rundir = new_rundir(run_tag, stem="telemac")
    partition = solve_cores(sheet.body, sheet.filled)
    # The serialization is a container round trip, so it runs off the loop.
    written = await asyncio.to_thread(serialize, sheet, rundir, steering=steering)
    return await stage_run(
        rundir, run_tag, module=sheet.module, steering=written["steering"],
        results=[*results, *kept], outputs=[*outputs, *kept],
        mesh_inputs=list(mesh_inputs), prefix=prefix, sheet=sheet.state(),
        result_basename=list(results)[0], server_facts=server_facts,
        check=check_staged_run,
        # The engine compiles the directory its FORTRAN FILE statement names; the manifest carries the same word the decks do.
        user_fortran=sheet.user_code(),
        coupling=coupling, continue_from=continue_from,
        # The same number the steering file states: the launcher partitions the mesh across it.
        cores=partition)
