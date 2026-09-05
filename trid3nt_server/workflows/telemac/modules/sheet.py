"""The sheet: a module's slots, what filled each one, and the two acts on it.

FILL is repeatable and decides nothing: it sets slots, expands the composites the
wrapper registered, binds every late-bound read in dependency order, and hands
back what the sheet now says - every filled slot with its PROVENANCE, and every
mandatory slot still open, carrying the meaning and the choices that would
answer it. Nothing runs.

RUN is the other act, and it is explicit. Execution is HELD until a complete
sheet is handed to it; then the sheet is serialized into the engine's own
steering file, the run directory is staged, and the box receives it.

Resolution order, lowest to highest: the engine default (never written - the
dictionary supplies it), the listed parts in order, the template, the fill. At
most two opinion layers exist and both are templates.
"""

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
    """A run was asked for on a sheet whose mandatory slots are not all filled."""

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
        """The mandatory slots nothing has answered yet.

        Mandatory IS "the dictionary gives it no default", so an open slot
        carries its meaning and its choices and no engine default to grey out -
        there is none, which is exactly why it has to be answered.
        """
        return tuple(slot for name, slot in self.body.CATALOG.items()
                     if slot.mandatory and name not in self.filled)

    def resolved(self) -> tuple[tuple[str, Any], ...]:
        """``(keyword, value)`` for everything the deck states, in catalog order.

        An engine default is not among them: the dictionary already supplies it,
        and writing it back would make the deck claim a choice nobody made.
        """
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
                      "choices": slot.choices}
                     for slot in self.open()],
        }


def _in_catalog_order(body: type,
                      filled: Mapping[str, Filled]) -> list[tuple[str, Filled]]:
    """The dictionary's own order - the order a sheet is read down."""
    return [(name, filled[name]) for name in body.CATALOG if name in filled]


def fill(source: type | Sheet, *, produced: Mapping[str, Any] | None = None,
         params: Mapping[str, Any] | None = None, **slots: Any) -> Sheet:
    """Set slots on a body or on a sheet already filled -> the sheet that results.

    Repeatable: an edit is another fill. A keyword the module does not have, and
    a value the dictionary does not take, refuse BY NAME rather than reaching the
    engine as a line nobody can account for. A value of None states nothing at
    all, so the dictionary's default stands and the deck stays silent about a
    choice nobody made.
    """
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

    A fill, reached through the canvas rather than through an argument. It rides
    the SAME gate a typed value rides, so the drawn vocabulary and the typed
    vocabulary cannot drift: what comes back has passed the one set of
    normalizers, and what comes back as nothing is a typed refusal naming the
    slot that stayed empty. Nothing is invented on a decline.
    """
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

    The body's own assertions are the TEMPLATE layer and beat every part; each
    part's assertions are the PART's, named on the row - the same keyword can
    mean something else in a new setting, and composed context never hides. A
    composite assertion, and one carrying a late-bound read, are PENDING rather
    than filled: neither is a value until the fill binds it.
    """
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


def _late(value: Any) -> bool:
    """Does ``value`` still hold a read? Then it is not a value until fill binds it.

    Walked rather than tested with ``any``: a placeholder refuses its own truth
    value, which is what keeps a description from being read as data anywhere
    else, and this is a place that counts them rather than reading one.
    """
    for kind in (Ref, ParamRef):
        for _found in declared_reads(value, kind):
            return True
    return False


def _in_ref_order(pending: Mapping[str, tuple[Any, str]],
                  ) -> list[tuple[str, tuple[Any, str]]]:
    """The pending assignments, each after the ones it reads.

    A value that reads another pending name waits for it; a cycle refuses naming
    the names in it, because a producer that waits on its own result never runs.
    """
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

    Two namespaces, because a body states both: a PARAM read is the sheet the
    invocation resolved, and a Ref is a producer of this fill or a slot already
    on it. Neither is a value while the body is being read - that is what makes
    the body static - and both become one here.
    """
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
    for part in ref.tail:
        found = (base.get(part) if isinstance(base, Mapping)
                 else getattr(base, part, None))
        if found is None:
            raise SlotRefused(
                f"Ref({ref.path!r}) reads {part!r} off {ref.root}, which carries "
                "no value for it.")
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

    The sheet is checked FIRST. A mandatory slot the engine has no default for is
    a question nobody answered, and a run started on one fails inside Fortran
    several minutes later, naming a keyword rather than the gap.

    WHICH box entry the staged run goes to is the caller's, not this function's:
    the sequence is what is held here, and a module surface that picked a
    dispatch would be holding an opinion.

    The serialization is a container round trip and runs off the loop.
    """
    from ..authoring.assembler import new_rundir, stage_run
    from ..authoring.serializer import serialize

    still_open = sheet.open()
    if still_open:
        raise SheetIncomplete(
            f"{sheet.module} cannot run: "
            + "; ".join(f"{slot.keyword} ({slot.desc[:60]})"
                        for slot in still_open))
    run_tag, rundir = new_rundir()
    written = await asyncio.to_thread(serialize, sheet, rundir, steering=steering)
    staged = await stage_run(
        rundir, run_tag, module=sheet.module, steering=written["steering"],
        results=list(results), outputs=list(outputs),
        mesh_inputs=list(mesh_inputs), prefix=prefix, sheet=sheet.state(),
        result_basename=list(results)[0], server_facts=server_facts,
        coupling=coupling, continue_from=continue_from)
    return await dispatch(run=staged, compute_class=compute_class)
