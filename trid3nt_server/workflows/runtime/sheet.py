"""The sheet: a module's slots, what filled each one, and the two acts on it.

Resolution order, lowest to highest: the engine default (never written), the template, the fill.
OPEN is informational; REQUIRED (the dictionary's OBLIG files) is what a run refuses on.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Collection, Mapping, Sequence

from trid3nt_contracts.common import Origin

from trid3nt_server.errors import DeclarativeError

from .module import Slot, SlotRefused, accept

__all__ = ["Filled", "Keyword", "Provenance", "Sheet", "SheetIncomplete",
           "complete", "fill", "fill_coupled", "filled_by", "run"]


@dataclass(frozen=True, slots=True)
class Provenance:
    """One slot's origin, and the name that makes it checkable.

    ``detail`` is the template, producer or source slot the origin points at.
    """

    origin: Origin
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.origin.value}: {self.detail}" if self.detail \
            else self.origin.value


class SheetIncomplete(SlotRefused):
    """A run was asked for on a sheet whose REQUIRED slots are not all filled."""

    error_code = "SHEET_INCOMPLETE"


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

    def engine_rows(self) -> tuple[list[Any], set[str]]:
        """The card's engine sections: what each coupled deck states and what each deck writes, and the printouts keyword each deck generates.

        A sheet carries none; the engine's sheet answers."""
        return [], set()


def _in_dictionary_order(body: type,
                      filled: Mapping[str, Filled]) -> list[tuple[str, Filled]]:
    return [(name, filled[name]) for name in body.MODULE_INPUT if name in filled]


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


def fill(source: type | Sheet, *, template: str = "",
         produced: Mapping[str, Any] | None = None,
         params: Mapping[str, Any] | None = None,
         settled: Mapping[str, Any] | None = None, inputs_fill: bool = True,
         **slots: Any) -> Sheet:
    """Set slots on a body or on a sheet already filled -> the sheet that results.

    ``produced`` and ``params`` are the run's one mapping (a param over the same name wins); ``settled`` is
    what the settle filled by keyword, and a template-stated keyword wins over it and over inputs. An unknown keyword refuses by name; None states nothing.
    """
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
    return body.sheet(filled, files, run_)


def filled_by(body: type, produced: Mapping[str, Any],
              only: Sequence[str] = ()) -> list[tuple[str, str, Any]]:
    """``(input, identifier, value)`` for every keyword an input of this run fills on ``body``; an absent input or None value fills nothing."""
    return [(source, name, value)
            for source, keywords in body.FILLED_BY.items()
            if (not only or source in only) and produced.get(source) is not None
            for name, read in keywords.items()
            if (value := read(produced[source])) is not None]


def _placed(wrapper: type, body: dict[str, Any]) -> dict[str, Any]:
    """``body``, refused where it states a keyword a still-derived one was placed against at the dictionary's value."""
    for derived, reads in wrapper.PLACED_AGAINST.items():
        for name in reads:
            if derived in body["derived"] and name in body["slots"]:
                raise SlotRefused(
                    f"{wrapper.slot(name).keyword} is stated and "
                    f"{wrapper.slot(derived).keyword} is not: the run derives "
                    "the second against the dictionary's value of the first. "
                    "State both to place it yourself.")
    return body


def fill_coupled(sheet: Sheet, stated: Mapping[str, Mapping[str, Any]]) -> Sheet:
    """Set slots on the run's coupled bodies -> the sheet that results.

    Keyed by module, then identifier; checked against that module's dictionary so a wrong value refuses
    here rather than in the Fortran. The body records which identifiers the run stated.
    """
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
        wrapper = sheet.body.wrapper_for(module)
        body = dict(files[basename])
        body["slots"] = {**body["slots"],
                         **{name: wrapper.slot(name).check(value)
                            for name, value in slots.items()}}
        body["stated"] = sorted(set(body.get("stated", ())) | set(slots))
        body["derived"] = sorted(set(body.get("derived", ())) - set(slots))
        files[basename] = _placed(wrapper, body)
    from dataclasses import replace

    return replace(sheet, files=MappingProxyType(files))


def complete(body: type, stated: Collection[str]) -> None:
    """Refuse a deck that leaves a REQUIRED file or its cadence out of ``stated``, by identifier.

    The fill judges what the template and the call state before any mesh; the stage
    judges the sheet a card edit left."""
    unanswered = [slot for name, slot in body.MODULE_INPUT.items()
                  if slot.is_required and name not in stated]
    # Past the OBLIG files the engine asks for by name in its own listing; a required set invented here would refuse runs it would take.
    if unanswered:
        raise SheetIncomplete(
            f"{body.MODULE} cannot run: "
            + "; ".join(f"{slot.keyword} ({slot.desc[:60]})"
                        for slot in unanswered))
    cadence = body.CADENCE
    if cadence and cadence not in stated:
        raise SheetIncomplete(
            f"{body.MODULE} states no {body.slot(cadence).keyword}, and "
            "the dictionary's own default writes a frame every step. State the "
            "period this question's frames are written at.")


@dataclass(slots=True)
class Keyword:
    """One keyword of one body, answered as an Input is: its name, the one range that
    bounds it and the module's own accept rule. ``carried`` says the body is the run's
    carrier; ``note`` is what the last accept has to say on a run that launches."""

    body: type
    name: str
    carried: bool = True
    note: str = ""
    is_row = False

    @property
    def bounds(self) -> tuple[float, float] | None:
        return getattr(self.body.MODULE_INPUT.get(self.name), "whole_bounds", None)

    def accept(self, value: Any) -> Any:
        """``value`` as the module takes it; a file keyword stated as a layer takes it
        as the file, and the processor count is held to this box."""
        if isinstance(value, Mapping) and "layer" in value:
            value = value["layer"]
        try:
            _identifier, taken, self.note = accept(self.body, self.name, value)
            self.body.fits_box(self.name, taken)
        except DeclarativeError as exc:
            exc.error_code = "KEYWORD_REFUSED"
            raise
        return taken


async def run(staged: Mapping[str, Any], *, dispatch: Callable[..., Any]) -> Any:
    """A staged run handed to the box -> the staged run and the box's answer, as one handle."""
    # The launcher partitions across the count the staged case states; a serial case states none.
    return {**staged, **await dispatch(run=staged, cores=int(staged["case"].get("cores", 1)))}
