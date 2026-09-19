"""The sheet: a module's slots, what filled each one, and the two acts on it.

Resolution order, lowest to highest: the engine default (never written - the
dictionary supplies it), the template, the fill. OPEN
is informational; REQUIRED, the dictionary's OBLIG files, is what a run refuses on."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from trid3nt_server.workflows.runtime import ParamRef, Ref
from trid3nt_server.workflows.runtime.plan import declared_reads

from .module import Output, Slot, SlotRefused

__all__ = ["Filled", "Origin", "Provenance", "Sheet", "SheetIncomplete", "draw",
           "fill", "fill_coupled", "late_bound", "run"]


class Origin(str, Enum):
    """WHERE a filled slot's value came from. A closed set, read by people.

    Nothing downstream branches on it: the reader overrides with confidence, or
    does not."""

    TEMPLATE = "template"
    USER = "user"
    MODEL = "model"
    PRODUCER = "producer"
    DERIVED = "derived"
    CALIBRATED = "calibrated"


@dataclass(frozen=True, slots=True)
class Provenance:
    """One slot's origin, and the name that makes it checkable.

    ``detail`` is the template, the producer, the source slot or the calibration
    run the origin points at; empty where the origin names everything there is."""

    origin: Origin
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.origin.value}: {self.detail}" if self.detail \
            else self.origin.value


class SheetIncomplete(SlotRefused):
    """A run was asked for on a sheet whose REQUIRED slots are not all filled."""

    error_code = "TELEMAC_SHEET_INCOMPLETE"


@dataclass(frozen=True, slots=True)
class Filled:
    """One filled slot: the value, and where in the resolution order it came from."""

    slot: Slot
    value: Any
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class Sheet:
    """A module's slots as they stand: what is filled, and what is still open."""

    #: The body the sheet was filled from - a template, or the bare wrapper.
    body: type
    filled: Mapping[str, Filled]
    #: Files a composite named, by basename: content the serializer writes beside
    #: the steering file that names them.
    files: Mapping[str, Any] = MappingProxyType({})

    @property
    def module(self) -> str:
        return self.body.MODULE

    def open(self) -> tuple[Slot, ...]:
        """Every keyword the dictionary gives no default for and nothing has set.

        Informational and COMPLETE: what this run leaves to the engine, whole."""
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
        """Every tracer this run's result carries: the ones the deck declares,
        then the ones each coupled module appends behind them.

        A name is 32 characters - the name in the first 16, the unit after."""
        from . import wrapper_for

        row = self.body.MODULE_OUTPUT.get(self.body.TRACER)
        rows = []
        for declared in dict(self.resolved()).get("NAMES OF TRACERS") or ():
            padded = str(declared).ljust(32)
            # A TRACER THE DECK NAMES is a quantity the deck put into the
            # domain unless the module that put it there says otherwise, so a
            # carrier's own row states the edge and the injection for the ones
            # it declares and an appending module states its own below.
            rows.append(Output(name=padded[:16].strip(), unit=padded[16:].strip(),
                               style=row.style if row is not None else None,
                               has_edge=True if row is None or row.has_edge is None
                               else row.has_edge,
                               injected=True if row is None or row.injected is None
                               else row.injected))
        for body in self.coupled:
            appends = wrapper_for(body["module"]).APPENDS
            for appended in (list(appends(body)) if appends is not None else []):
                # ADOPTED, NOT APPENDED. The engine adds a module's tracer only
                # when no tracer already carries that name in its first sixteen
                # characters, so a carrier that declared one keeps it - with the
                # NAME and UNIT it declared, which are what the result file
                # carries - and the module attaches its process to it. The STYLE
                # is the appending module's either way: the carrier's is the
                # generic tracer row, which says nothing about what this one is.
                held = next((n for n, row in enumerate(rows)
                             if row.name == appended.name), None)
                if held is None:
                    rows.append(appended)
                else:
                    # WHAT the variable is, is the appending module's statement:
                    # it attached the process, so its style and its edge are the
                    # ones that describe the quantity the carrier now carries.
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

    def printouts(self) -> Mapping[str, str]:
        """THIS deck's variables keyword, generated from its module's table.

        The deck as it stands is handed to the generator, because a row that
        exists only under a keyword is written for a deck that states it."""
        return self.body.printouts(tracers=len(self.tracers),
                                   stated=self.stated())

    def published(self) -> tuple[tuple[str, str, Output], ...]:
        """Every variable this run's results carry, as ``(token, module, row)``.

        The host's table first, then its tracers by position, then each coupled
        module's own - the order the engine wrote them in."""
        from . import wrapper_for

        body = self.body
        stated = self.stated()
        rows = [(token, self.module, row)
                for token, row in body.table(stated).items()
                if token not in body.LISTING and token != body.TRACER]
        # A tracer is read by its POSITION among the carrier's own, which is the
        # token the primitives spell whatever the keyword calls it.
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
        """What this deck states, by IDENTIFIER - the name a row's condition and
        a keyword read are both written under."""
        return MappingProxyType({name: row.value
                                 for name, row in self.filled.items()})

    def resolved(self) -> tuple[tuple[str, Any], ...]:
        """``(keyword, value)`` for everything the deck states, in dictionary order.

        An engine default is never among them; the dictionary supplies it."""
        return tuple((row.slot.keyword, row.value)
                     for name, row in _in_dictionary_order(self.body, self.filled))

    def __getattr__(self, name: str) -> Any:
        """One FILLED keyword by identifier, so ``Ref("sheet.<KEYWORD>")`` reads
        the value this run states wherever the sheet is a step's result.

        Only what is filled: a keyword standing at the engine's own default is
        not on the sheet, and the reader's refusal names it."""
        row = object.__getattribute__(self, "filled").get(name)
        if row is None:
            raise AttributeError(name)
        return row.value

    def state(self) -> dict[str, Any]:
        """What fill hands back: the sheet, said plainly."""
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
    """The dictionary's own order - the order a sheet is read down."""
    return [(name, filled[name]) for name in body.MODULE_INPUT if name in filled]


def fill(source: type | Sheet, *, template: str = "",
         produced: Mapping[str, Any] | None = None,
         params: Mapping[str, Any] | None = None, **slots: Any) -> Sheet:
    """Set slots on a body or on a sheet already filled -> the sheet that results.

    Repeatable; an unknown keyword refuses BY NAME and None states nothing."""
    from ..authoring.atmosphere import write_atmosphere

    body, standing, pending = _standing(source, template)
    dictionary = body.MODULE_INPUT
    composites = body.COMPOSITES
    for name, value in slots.items():
        if name not in composites:
            body.slot(name)      # refuses by name, and names the nearest keyword
        pending[name] = (value, Provenance(Origin.USER))

    filled = dict(standing)
    files: dict[str, Any] = dict(source.files) if isinstance(source, Sheet) else {}
    for name, (value, provenance) in _in_ref_order(pending):
        if provenance.origin is not Origin.USER:
            read = _measured(value)
            if read is not None:
                # A template states WHICH measurement this slot takes; the number
                # itself is the accepted artifact's - the boundary walk, the
                # normal depth, the time step the mesh's own CFL allows. Badging
                # it "template" would hide that nobody wrote it down.
                provenance = Provenance(Origin.DERIVED, read)
        value = _bind(value, produced or {}, params or {}, filled)
        if value is None:
            # NOTHING is what None states. No keyword's value is None, so the
            # one thing it can mean is "this run does not state this" - a wind
            # nobody asked for, a coupling this class does not run - and the
            # dictionary's default is what the engine then reads.
            filled.pop(name, None)
            continue
        if name in composites:
            expanded, named = composites[name].expand(value)
            for key, item in expanded.items():
                slot = body.slot(key)
                filled[key] = Filled(
                    slot=slot, value=slot.check(item),
                    provenance=Provenance(Origin.PRODUCER, name))
            files.update(named)
            continue
        slot = dictionary[name]
        filled[name] = Filled(slot=slot, value=slot.check(value),
                              provenance=provenance)
    _arm(body, filled)
    write_atmosphere(body, {name: row.value for name, row in filled.items()},
                     files)
    return Sheet(body=body, filled=MappingProxyType(filled),
                 files=MappingProxyType(files))


def _arm(body: type, filled: dict[str, Filled]) -> None:
    """Turn on the term a stated value implies, where nothing has stated it.

    The engine reads the value only with its switch true, so a rate stated by
    its own name and left disarmed is a number nothing reads. A deck that
    states the switch itself keeps whatever it said, off included."""
    for name, switch in body.ARMS.items():
        if name not in filled or switch in filled:
            continue
        slot = body.slot(switch)
        filled[switch] = Filled(slot=slot, value=slot.check(True),
                                provenance=Provenance(Origin.PRODUCER, name))


def fill_coupled(sheet: Sheet, stated: Mapping[str, Mapping[str, Any]]) -> Sheet:
    """Set slots on the run's COUPLED bodies -> the sheet that results.

    Keyed by module, then by identifier. The value is checked against that
    module's own dictionary, so a wrong one refuses here rather than in the
    Fortran, and the body records which identifiers the run stated so the card
    names the user rather than the deck."""
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


async def draw(source: type | Sheet, name: str, *, geometry: str = "point",
               prompt: str = "") -> Sheet:
    """Ask for ONE value on the canvas -> the sheet with it filled.

    Rides the SAME gate a typed value rides; a decline is a typed refusal."""
    from trid3nt_server.gates.draw_input import gate_draw_input

    body = source.body if isinstance(source, Sheet) else source
    outcome = await gate_draw_input(tool_name=body.MODULE, param=name,
                                    geometry=geometry, prompt=prompt)
    if not outcome.drawn:
        raise SlotRefused(
            f"{body.MODULE} needs {name!r} drawn on the canvas "
            f"({prompt or geometry}), and {outcome.reason}. It is not invented - "
            "supply the value explicitly or draw it.")
    return fill(source, **{name: outcome.value})


def _standing(source: type | Sheet, template: str = "",
              ) -> tuple[type, dict[str, Filled], dict[str, tuple[Any, str]]]:
    """What is on the sheet before this fill: the body's assertions, or a sheet.

    A composite or a read is PENDING until the fill binds it."""
    if isinstance(source, Sheet):
        return source.body, dict(source.filled), {}
    standing: dict[str, Filled] = {}
    pending: dict[str, tuple[Any, str]] = {}
    # The TEMPLATE the value started in, which is the only place a template
    # name survives a run. A fill with no template names the body instead,
    # because a bare wrapper has no template to name.
    provenance = Provenance(Origin.TEMPLATE, template or source.__name__)
    for name, value in source.ASSERTED.items():
        slot = source.MODULE_INPUT.get(name)
        if slot is None or value is None or late_bound(value):
            pending[name] = (value, provenance)
        else:
            standing[name] = Filled(slot=slot, value=value, provenance=provenance)
    return source, standing, pending


def _measured(value: Any) -> str | None:
    """The SLOT this assertion reads, or ``None`` when it reads nothing measured.

    A ``Ref`` is a read of what the run measured; a ``ParamRef`` is the
    invocation's own answer and is not."""
    for found in declared_reads(value, Ref):
        return str(found.path)
    return None


def late_bound(value: Any) -> bool:
    """Does ``value`` still hold a read? Then it is not a value until fill binds it.

    Walked rather than tested: a placeholder refuses its own truth value."""
    for kind in (Ref, ParamRef):
        for _found in declared_reads(value, kind):
            return True
    return False


#: The root a read of THIS SHEET's own filled slots is written under, so a value
#: placed on a keyword follows whatever the run stated that keyword at.
_SHEET = "sheet"


def _named(ref: Ref) -> str:
    """WHAT this read waits on: the slot a sheet read names, else the producer."""
    return ref.tail[0] if ref.root == _SHEET and ref.tail else ref.root


def _in_ref_order(pending: Mapping[str, tuple[Any, str]],
                  ) -> list[tuple[str, tuple[Any, str]]]:
    """The pending assignments, each after the ones it reads.

    A cycle refuses, naming the names in it."""
    waiting = dict(pending)
    ordered: list[tuple[str, tuple[Any, str]]] = []
    while waiting:
        ready = [name for name, (value, _) in waiting.items()
                 if not ({_named(ref) for ref in declared_reads(value, Ref)}
                         & (set(waiting) - {name}))]
        if not ready:
            raise SlotRefused(
                f"the fill of {sorted(waiting)} reads itself in a cycle; a "
                "producer cannot wait on its own result.")
        for name in ready:
            ordered.append((name, waiting.pop(name)))
    return ordered


def _bind(value: Any, produced: Mapping[str, Any], params: Mapping[str, Any],
          filled: Mapping[str, Filled]) -> Any:
    """Substitute every late-bound read in ``value`` with what it names.

    A PARAM read is the invocation's sheet; a ``Ref`` is this fill's own."""
    if isinstance(value, ParamRef):
        if value.name not in params:
            raise SlotRefused(
                f"ParamRef({value.name!r}) names no declared param of this fill "
                f"({sorted(params)}).")
        return params[value.name]
    if isinstance(value, Ref):
        return _read(value, produced, filled)
    if isinstance(value, Mapping):
        return {k: _bind(v, produced, params, filled) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_bind(v, produced, params, filled) for v in value)
    return value


def _read(ref: Ref, produced: Mapping[str, Any],
          filled: Mapping[str, Filled]) -> Any:
    """One late-bound read: a producer's result, or a slot already on the sheet."""
    if ref.root == _SHEET:
        return _off_sheet(ref, filled)
    if ref.root in produced:
        base = produced[ref.root]
    elif ref.root in filled:
        base = filled[ref.root].value
    else:
        raise SlotRefused(
            f"Ref({ref.path!r}) names neither a producer of this fill "
            f"({sorted(produced)}) nor a slot already on the sheet.")
    if base is None:
        # A row that is WHOLLY ABSENT states nothing, exactly as a field that is
        # present and empty does below.
        return None
    _missing = object()
    for part in ref.tail:
        if isinstance(base, (list, tuple)) and part.isdigit():
            # A PAIR is one value with an order, not two fields: a settled point
            # is [x, y] in the mesh's own metres, and a keyword that takes the
            # abscissae apart from the ordinates reads it by position.
            found = base[int(part)] if int(part) < len(base) else _missing
        else:
            found = (base.get(part, _missing) if isinstance(base, Mapping)
                     else getattr(base, part, _missing))
        if found is _missing:
            raise SlotRefused(
                f"Ref({ref.path!r}) reads {part!r} off {ref.root}, which names "
                "no such field.")
        # A field the row HOLDS as nothing states nothing: a wind nobody asked
        # for, a previous run this one does not continue. That is an answer, and
        # the composite reading it expands to no keyword at all.
        if found is None:
            return None
        base = found
    return base


def _off_sheet(ref: Ref, filled: Mapping[str, Filled]) -> Any:
    """A read placed on a KEYWORD, resolved through the sheet as it now stands.

    The value the deck will write, whatever set it - so a placement follows an
    override instead of the number the body was authored at."""
    if not ref.tail:
        raise SlotRefused(
            f"Ref({ref.path!r}) reads the sheet and names no keyword on it.")
    row = filled.get(ref.tail[0])
    if row is None:
        raise SlotRefused(
            f"Ref({ref.path!r}) names {ref.tail[0]!r}, which this sheet does not "
            "state; a read placed on a keyword follows a keyword the deck sets.")
    base = row.value
    for part in ref.tail[1:]:
        base = base[int(part)] if isinstance(base, (list, tuple)) \
            else base[part] if isinstance(base, Mapping) else getattr(base, part)
    return base


async def run(sheet: Sheet, *, dispatch: Callable[..., Any],
              mesh_inputs: Sequence[Mapping[str, str]],
              outputs: Sequence[str], results: Sequence[str], prefix: str,
              server_facts: Mapping[str, Any], steering: str | None = None,
              compute_class: str = "medium",
              coupling: str | None = None,
              continue_from: str | None = None) -> Any:
    """A complete sheet: serialize, stage, hand it to the box.

    Checked against the REQUIRED slots only; the box entry is the caller's."""
    # Everything past the OBLIG files the engine asks for by name in its own
    # listing, so a required set invented here would refuse runs it would take.
    from ..authoring.assembler import new_rundir, stage_run
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
    run_tag, rundir = new_rundir()
    # The serialization is a container round trip, so it runs off the loop.
    written = await asyncio.to_thread(serialize, sheet, rundir, steering=steering)
    staged = await stage_run(
        rundir, run_tag, module=sheet.module, steering=written["steering"],
        results=list(results), outputs=list(outputs),
        mesh_inputs=list(mesh_inputs), prefix=prefix, sheet=sheet.state(),
        result_basename=list(results)[0], server_facts=server_facts,
        # The engine compiles the DIRECTORY its FORTRAN FILE statement names, so
        # the manifest channel carries the same word the deck does rather than a
        # second derivation of it.
        user_fortran=dict(sheet.resolved()).get("FORTRAN FILE"),
        coupling=coupling, continue_from=continue_from)
    # The staged run and the box's answer are ONE handle: the reader needs both
    # what was staged and what came back, and two results would make the caller
    # carry the join.
    return {**staged, **await dispatch(run=staged, compute_class=compute_class)}
