"""The sheet: a module's slots, what filled each one, and the two acts on it.

Resolution order, lowest to highest: the engine default (never written - the
dictionary supplies it), the listed parts in order, the template, the fill. OPEN
is informational; REQUIRED, the dictionary's OBLIG files, is what a run refuses on."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from trid3nt_server.workflows.runtime import ParamRef, Ref
from trid3nt_server.workflows.runtime.plan import declared_reads

from .module import Slot, SlotRefused

__all__ = ["Filled", "Sheet", "SheetIncomplete", "draw", "fill", "run"]


class SheetIncomplete(SlotRefused):
    """A run was asked for on a sheet whose REQUIRED slots are not all filled."""

    error_code = "TELEMAC_SHEET_INCOMPLETE"


@dataclass(frozen=True, slots=True)
class Filled:
    """One filled slot: the value, and where in the resolution order it came from."""

    slot: Slot
    value: Any
    provenance: str


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
        return tuple(slot for name, slot in self.body.CATALOG.items()
                     if slot.is_open and name not in self.filled)

    def required(self) -> tuple[Slot, ...]:
        """The open slots a run cannot begin without: the dictionary's OBLIG files."""
        return tuple(slot for slot in self.open() if slot.is_required)

    def resolved(self) -> tuple[tuple[str, Any], ...]:
        """``(keyword, value)`` for everything the deck states, in catalog order.

        An engine default is never among them; the dictionary supplies it."""
        return tuple((row.slot.keyword, row.value)
                     for name, row in _in_catalog_order(self.body, self.filled))

    def state(self) -> dict[str, Any]:
        """What fill hands back: the sheet, said plainly."""
        return {
            "module": self.module,
            "body": self.body.__name__,
            "filled": {name: {"keyword": row.slot.keyword, "value": row.value,
                              "provenance": row.provenance}
                       for name, row in _in_catalog_order(self.body, self.filled)},
            "files": sorted(self.files),
            "open": [{"keyword": slot.keyword, "identifier": slot.identifier,
                      "desc": slot.desc, "type": slot.type,
                      "choices": slot.choices, "required": slot.is_required}
                     for slot in self.open()],
        }


def _in_catalog_order(body: type,
                      filled: Mapping[str, Filled]) -> list[tuple[str, Filled]]:
    """The dictionary's own order - the order a sheet is read down."""
    return [(name, filled[name]) for name in body.CATALOG if name in filled]


def fill(source: type | Sheet, *, produced: Mapping[str, Any] | None = None,
         params: Mapping[str, Any] | None = None, **slots: Any) -> Sheet:
    """Set slots on a body or on a sheet already filled -> the sheet that results.

    Repeatable; an unknown keyword refuses BY NAME and None states nothing."""
    body, standing, pending = _standing(source)
    catalog = body.CATALOG
    composites = body.COMPOSITES
    for name, value in slots.items():
        if name not in composites:
            body.slot(name)      # refuses by name, and names the nearest keyword
        pending[name] = (value, "fill")

    filled = dict(standing)
    files: dict[str, Any] = dict(source.files) if isinstance(source, Sheet) else {}
    for name, (value, provenance) in _in_ref_order(pending):
        if _measured(value) and provenance != "fill":
            # A template states WHICH measurement this slot takes; the number
            # itself is the accepted artifact's - the boundary walk, the normal
            # depth, the time step the mesh's own CFL allows. Badging it
            # "template" would hide that nobody wrote it down.
            provenance = "derived"
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
                filled[key] = Filled(slot=slot, value=slot.check(item),
                                     provenance=f"producer {name}")
            files.update(named)
            continue
        slot = catalog[name]
        filled[name] = Filled(slot=slot, value=slot.check(value),
                              provenance=provenance)
    return Sheet(body=body, filled=MappingProxyType(filled),
                 files=MappingProxyType(files))


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


def _standing(source: type | Sheet) -> tuple[type, dict[str, Filled],
                                             dict[str, tuple[Any, str]]]:
    """What is on the sheet before this fill: the body's parts, or a sheet.

    A body's assertions beat its parts; a composite or a read is PENDING."""
    if isinstance(source, Sheet):
        return source.body, dict(source.filled), {}
    standing: dict[str, Filled] = {}
    pending: dict[str, tuple[Any, str]] = {}
    for body in (*source.PARTS, source):
        provenance = ("template" if body is source else f"part {body.__name__}")
        for name, value in body.ASSERTED.items():
            slot = source.CATALOG.get(name)
            if slot is None or value is None or _late(value):
                pending[name] = (value, provenance)
                standing.pop(name, None)
            else:
                standing[name] = Filled(slot=slot, value=value,
                                        provenance=provenance)
                pending.pop(name, None)
    return source, standing, pending


def _measured(value: Any) -> bool:
    """Is this assertion a read of something the run MEASURED?

    A ``Ref`` is; a ``ParamRef`` is the invocation's own answer and is not."""
    for _found in declared_reads(value, Ref):
        return True
    return False


def _late(value: Any) -> bool:
    """Does ``value`` still hold a read? Then it is not a value until fill binds it.

    Walked rather than tested: a placeholder refuses its own truth value."""
    for kind in (Ref, ParamRef):
        for _found in declared_reads(value, kind):
            return True
    return False


def _in_ref_order(pending: Mapping[str, tuple[Any, str]],
                  ) -> list[tuple[str, tuple[Any, str]]]:
    """The pending assignments, each after the ones it reads.

    A cycle refuses, naming the names in it."""
    waiting = dict(pending)
    ordered: list[tuple[str, tuple[Any, str]]] = []
    while waiting:
        ready = [name for name, (value, _) in waiting.items()
                 if not ({ref.root for ref in declared_reads(value, Ref)}
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
    if ref.root in produced:
        base = produced[ref.root]
    elif ref.root in filled:
        base = filled[ref.root].value
    else:
        raise SlotRefused(
            f"Ref({ref.path!r}) names neither a producer of this fill "
            f"({sorted(produced)}) nor a slot already on the sheet.")
    _missing = object()
    for part in ref.tail:
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
