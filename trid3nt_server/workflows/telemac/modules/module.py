"""A TELEMAC module's keyword surface, as the engine publishes it.

Every assertion is data, fixed at import: a body reads no value any fill produced, and every
refusal (unknown keyword, wrong type, a body extending anything but its wrapper) is raised at import.
"""

from __future__ import annotations

import inspect

import difflib
import json
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import FunctionType, MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from trid3nt_server.workflows.runtime import DeclarativeError

__all__ = [
    "Composite",
    "Module",
    "Output",
    "Slot",
    "SlotRefused",
    "Unwritten",
    "identify_on",
    "load_module_input",
    "module_input_dir",
]


class SlotRefused(DeclarativeError):
    """A keyword the module does not have, or a value the dictionary does not take."""

    error_code = "TELEMAC_SLOT_REFUSED"


class _Unset:
    """The absence of an engine default - which is what makes a slot mandatory."""

    def __repr__(self) -> str:
        return "unset"


UNSET = _Unset()

# Class attributes a wrapper carries that are never keyword assertions.
_RESERVED = frozenset((
    "MODULE", "MODULE_INPUT", "COMPOSITES", "READS", "ASSERTED", "ARMS",
    "ARMS_ON_HOST", "MODULE_OUTPUT", "LISTING", "DERIVED", "PRINTOUTS",
    "CADENCE", "CLOCK", "TRACER", "APPENDS", "APPENDABLE", "ONLY_3D",
    "UNWRITTEN", "RESULT_FILE", "RESULT_FILES", "RESULT_KEYWORD", "FILLED_BY",
    "composites",
    "reads", "appends", "printouts", "seconds", "slot",
))


def module_input_dir() -> Path:
    """Where the committed module inputs live."""
    return Path(__file__).resolve().parent / "module_input"


# Plausibility bounds beside the dictionary row, by module and identifier: the unit the keyword is
# read in and the range a value is taken inside (refused by name outside). A sidecar because the
# dictionary is the image's own, compared byte for byte, and carries units only in help prose. One
# ``[lo, hi]`` bounds every value; a list is one per element in the keyword's order. Calibration reads this table.
_BOUNDS_FILE = Path(__file__).resolve().parent / "module_bounds.json"


@lru_cache(maxsize=None)
def _bounds(module: str) -> Mapping[str, tuple[str, tuple]]:
    rows = json.loads(_BOUNDS_FILE.read_text()).get(module) or {}
    return MappingProxyType({
        name: (str(row["unit"]),
               tuple(tuple(float(v) for v in pair)
                     for pair in (row["bounds"]
                                  if isinstance(row["bounds"][0], list)
                                  else [row["bounds"]])))
        for name, row in rows.items()})


@dataclass(frozen=True, slots=True)
class Slot:
    """One keyword, as the dictionary describes it."""

    keyword: str
    identifier: str
    type: str
    size: int | None
    unbounded: bool
    desc: str
    rubrique: tuple[str, ...]
    level: int
    is_file: bool
    engine_default: Any = UNSET
    choices: Any = None
    mnemo: str = ""
    file_role: str = ""
    file_mandatory: bool = False
    # The unit this keyword's value is read in, per the bounds sidecar; empty if unbounded or dimensionless.
    unit: str = ""
    # The plausibility range per value: one pair for all, or one per element in the keyword's order.
    bounds: tuple[tuple[float, float], ...] = ()
    # This keyword's one value is a separator-joined selection from its choices, so the choices do not name whole values.
    multi_select: bool = False

    @property
    def is_list(self) -> bool:
        """TAILLE is the value's arity. Open-ended says the length is not fixed at it, not that an arity-one keyword carries several values (the engine reads the first and says nothing)."""
        return (self.size or 1) > 1

    @property
    def is_open(self) -> bool:
        """The dictionary gives this keyword no default, so nothing answers it.

        Lists included: an undefaulted list is empty until something states it.
        """
        return self.engine_default is UNSET

    @property
    def is_required(self) -> bool:
        """The engine will not start without this one: an OBLIG file, undefaulted.

        The dictionary's OBLIG mark is the only thing a run refuses on.
        """
        # Other demands come from the engine's own listing, where LECDON names the keyword; a set invented here would guess at the Fortran's conditions.
        return self.is_file and self.file_mandatory and self.is_open

    def check(self, value: Any) -> Any:
        value = self.fits(value, typed=self._typed)
        if self.choices and not self.multi_select and not self.is_list \
                and str(value) not in self.choices:
            raise SlotRefused(
                f"{self.keyword} does not take {value!r}. The dictionary's "
                f"choices are {self._named_choices()}.")
        return value

    def fits(self, value: Any, typed: Any = None) -> Any:
        """``value`` if its arity and plausibility range fit this slot - what the engine's steering-file class does not check."""
        typed = typed or (lambda item: item)
        if not self.is_list:
            return self._bounded(typed(value), 0)
        if not isinstance(value, (list, tuple)):
            raise SlotRefused(
                f"{self.keyword} takes a list of {self.type} values"
                + (f" ({self.size} of them)" if not self.unbounded else "")
                + f"; got {value!r}.")
        if not self.unbounded and len(value) != self.size:
            raise SlotRefused(
                f"{self.keyword} takes exactly {self.size} values; "
                f"got {len(value)}.")
        # A list's choices are the engine's to check: the dictionary spells a tracer choice as T*, kSi, T1*, and telapy's reader knows those spellings.
        return [self._bounded(typed(item), position)
                for position, item in enumerate(value)]

    def _bounded(self, value: Any, position: int) -> Any:
        """``value`` if the bounds row takes it, or the refusal that names both.

        A keyword with no bounds row is bounded by the engine alone.
        """
        if not self.bounds or self.type == "STRING":
            return value
        low, high = self.bounds[position if len(self.bounds) > 1 else 0]
        if not (low <= float(value) <= high):
            raise SlotRefused(
                f"{self.keyword} is taken between {low:g} and {high:g}"
                + (f" at position {position + 1}" if len(self.bounds) > 1 else "")
                + f"; got {value!r}. A value outside that is either a unit this "
                "keyword is not read in or a run nobody would believe.")
        return value

    def _typed(self, value: Any) -> Any:
        if not isinstance(value, _TYPES[self.type]) \
                or isinstance(value, bool) != (self.type == "LOGICAL"):
            raise SlotRefused(
                f"{self.keyword} is {self.type}; {value!r} is "
                f"{type(value).__name__}.")
        return value

    def _named_choices(self) -> str:
        if isinstance(self.choices, Mapping):
            return ", ".join(f"{k} ({v})" for k, v in self.choices.items())
        return ", ".join(str(c) for c in self.choices or ())


# What each dictionary type is in Python; LOGICAL is separated from INTEGER in ``_scalar`` because a bool is an int.
_TYPES: Mapping[str, Any] = {
    "INTEGER": int, "REAL": (int, float), "LOGICAL": bool, "STRING": str,
}


@dataclass(frozen=True, slots=True)
class Composite:
    """One value standing for several slots, and the file they name.

    ``expand`` is ``(value) -> (slots, files)``, keyed by identifier and basename; one also taking ``run``
    reads the run's mapping itself. ``reads`` are the arguments that take an input's name.
    """

    name: str
    expand: Callable[..., tuple[Mapping[str, Any], Mapping[str, Any]]]
    reads: frozenset[str] = frozenset()

    def names(self, value: Any) -> list[str]:
        """Every input name ``value`` states in an argument that takes one, including inside a mapping."""
        if not isinstance(value, Mapping):
            return []
        found: list[str] = []
        for key, item in value.items():
            if key in self.reads:
                for part in item if isinstance(item, (list, tuple)) else [item]:
                    found += [part] if isinstance(part, str) else self.names(part)
        return found

    def apply(self, value: Any, run: Mapping[str, Any]
              ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        if isinstance(value, Mapping):
            value = {key: _read(run, item, self.name) if key in self.reads
                     else item for key, item in value.items()}
        if "run" in inspect.signature(self.expand).parameters:
            return self.expand(value, run=run)
        return self.expand(value)


def _read(run: Mapping[str, Any], value: Any, composite: str) -> Any:
    if isinstance(value, (list, tuple)):
        return type(value)(_read(run, item, composite) if isinstance(item, str)
                           else item for item in value)
    if not isinstance(value, str):
        return value
    if value not in run:
        raise SlotRefused(f"{composite} reads {value!r}, which no input of this "
                          f"run is called.")
    return run[value]


@dataclass(frozen=True, slots=True)
class Output:
    """One variable the module writes: its result-file name, unit, style row, whether it varies in time, and whether it has a visible edge.

    A row that does not vary is painted, never animated. An edge row is masked below a fraction of its
    magnitude; a variable the water carries everywhere is drawn whole. ``None`` leaves it to the table: a tracer row
    has an edge, a written variable does not. The edge is about drawing, not whether the deck put the quantity there.
    """

    name: str
    unit: str = ""
    style: Mapping[str, Any] | None = None
    varies: bool = True
    has_edge: bool | None = None
    # Did the deck put this quantity into the domain (a declared tracer, a release, a stated initial value)? Only such a row is refused when nothing everywhere: a variable the engine grows (ice cover, bed evolution) is honestly zero. ``None`` leaves it to the table.
    injected: bool | None = None
    # The keyword this row exists under: the engine allocates the variable only with it true, and asking for an unallocated one stops the solve. Empty on a row always carried. A deck leaving it unstated decides it at the dictionary default.
    under: str = ""


@dataclass(frozen=True, slots=True)
class Unwritten:
    """A slot the engine's own source marks as never written: its result-file name in English (and French where it differs), and the source line making the mark.

    One entry governs both directions: never asked of the engine, never published off the file.
    """

    spelling: str
    cited: str
    french: str = ""


@lru_cache(maxsize=None)
def load_module_input(module: str) -> Mapping[str, Slot]:
    """The module's whole keyword surface by identifier, in the dictionary's order (a sheet reads down it)."""
    path = module_input_dir() / f"{module}.json"
    if not path.is_file():
        raise SlotRefused(
            f"no module input for {module!r}; the exposed modules are "
            f"{sorted(p.stem for p in module_input_dir().glob('*.json'))}.")
    rows = json.loads(path.read_text())["keywords"]
    bounded = _bounds(module)
    unbounded_row: tuple[str, tuple] = ("", ())
    return MappingProxyType({row["identifier"]: Slot(
        keyword=row["keyword"], identifier=row["identifier"], type=row["type"],
        size=row["size"], unbounded=row["unbounded"], desc=row["help"],
        rubrique=tuple(row["rubrique"]), level=row.get("level", 0),
        is_file=row["is_file"],
        engine_default=row["default"] if "default" in row else UNSET,
        choices=row.get("choices"), mnemo=row.get("mnemo", ""),
        multi_select=row.get("multi_select", False),
        file_role=row.get("file_role", ""),
        file_mandatory=row.get("file_mandatory", False),
        unit=bounded.get(row["identifier"], unbounded_row)[0],
        bounds=bounded.get(row["identifier"], unbounded_row)[1],
    ) for row in rows})


class _Body(type):
    """The metaclass every wrapper and every body extending one is made by.

    A body's namespace is checked against the dictionary at import.
    """

    def __call__(cls, *args: str) -> type:
        if cls is not Module:
            raise SlotRefused(
                f"{cls.__name__} is a declaration, not a value; fill() makes a "
                "sheet from it.")
        (name,) = args
        module_input = load_module_input(name)
        return _Body(name.upper(), (Module,), {
            "__doc__": f"The {name} keyword surface: {len(module_input)} slots.",
            "MODULE": name, "MODULE_INPUT": module_input,
            "COMPOSITES": MappingProxyType({}), "READS": MappingProxyType({}),
            "ASSERTED": MappingProxyType({}),
        })

    def __new__(mcls, name: str, bases: tuple, namespace: dict) -> type:
        cls = super().__new__(mcls, name, bases, dict(namespace))
        module_input = namespace.get("MODULE_INPUT") or getattr(cls, "MODULE_INPUT", None)
        if module_input is None or "MODULE_INPUT" in namespace:
            return cls
        _refuse_extended_body(cls, bases)
        cls.ASSERTED = MappingProxyType(_asserted(cls, namespace, module_input))
        return cls


def _refuse_extended_body(cls: type, bases: tuple) -> None:
    """A body extends the wrapper and nothing else: a value two bodies share is restated in each."""
    for base in bases:
        if getattr(base, "ASSERTED", None):
            raise SlotRefused(
                f"{cls.__name__} extends {base.__name__}, which is a body. A body "
                f"extends its module's wrapper; restate the values it needs.")


def _asserted(cls: type, namespace: Mapping[str, Any],
              dictionary: Mapping[str, Slot]) -> dict[str, Any]:
    composites = getattr(cls, "COMPOSITES", {})
    asserted: dict[str, Any] = {}
    for key, value in namespace.items():
        if key.startswith("_") or key in _RESERVED or _is_method(value):
            continue
        if key in composites:
            asserted[key] = value
            continue
        slot = dictionary.get(key)
        if slot is None:
            raise SlotRefused(
                f"{cls.__name__} asserts {key!r}, which {cls.MODULE} has no "
                f"keyword for.{_nearest(key, dictionary, composites)}")
        # None is no keyword's value, so it means this body states nothing here and the dictionary default stands.
        asserted[key] = None if value is None else slot.check(value)
    return asserted


def _is_method(value: Any) -> bool:
    """A keyword's value is data; no dictionary spells a keyword as a callable."""
    return isinstance(value, (classmethod, staticmethod, FunctionType))


def _nearest(key: str, dictionary: Mapping[str, Slot],
             composites: Mapping[str, Any]) -> str:
    close = difflib.get_close_matches(key, list(dictionary) + list(composites), n=3)
    return f" Did you mean {', '.join(close)}?" if close else ""


class Module(metaclass=_Body):
    """A module's dictionary, its composites, what it writes and how it is read.

    There is no hook for a default: the engine's default is the whole position.
    """

    # The engine module this wraps, e.g. ``telemac2d``.
    MODULE: str = ""
    # Every keyword the module has, by identifier.
    MODULE_INPUT: Mapping[str, Slot] = MappingProxyType({})
    COMPOSITES: Mapping[str, Composite] = MappingProxyType({})
    # Keywords a run input fills itself, by input name: identifier -> value read off what the input holds. A body that states one wins.
    FILLED_BY: Mapping[str, Mapping[str, Callable[[Any], Any]]] = MappingProxyType({})
    # What the module writes, by printouts mnemonic: one row per variable; a variable the dictionary offers and this table does not row is not written.
    MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({})
    # The primitives over that output: kind -> the read off a solved run.
    READS: Mapping[str, Callable[..., Any]] = MappingProxyType({})
    # Rows the module prints in its listing rather than writing to its result file; a series of one is read off the listing.
    LISTING: frozenset[str] = frozenset()
    # Rows defined over the result's variables, each ``(solved) -> (name, units, values(nframes, npoin2))``.
    DERIVED: Mapping[str, Callable[..., Any]] = MappingProxyType({})
    # The keyword the table is written into, by identifier; empty on a module with no result of its own.
    PRINTOUTS: str = ""
    # The keyword saying how often the table is written; empty on a module that does not march in time. Where named, a deck stating none refuses: the dictionary default is every step, an animation nobody sized.
    CADENCE: str = ""
    # The token a tracer takes in that keyword, also its style row; empty on a module with no tracer surface.
    TRACER: str = ""
    # What this module appends to its carrier's tracers, ``(body) -> rows``; ``None`` if none.
    APPENDS: Callable[[Any], Any] | None = None
    # The rows that hook can append and when, as ``(condition, rows)``, enumerable without a body.
    APPENDABLE: tuple[tuple[str, tuple[Output, ...]], ...] = ()
    # Choices the printouts keyword spells that the engine never writes, by mnemonic, each citing the source line: never a row or layer, though a wildcard may reach them.
    UNWRITTEN: Mapping[str, Unwritten] = MappingProxyType({})
    # The result file the primitives read; empty reads the run's own.
    RESULT_FILE: str = ""
    # The keyword a deck NAMES that result in; modules spell it differently (TOMAWAC: 2D RESULTS FILE), so readers ask the module.
    RESULT_KEYWORD: str = "RESULTS_FILE"
    # How the module spells its run length: keywords whose product is the seconds covered (one for the window, two for a step and a count). Empty on a module that does not march in time.
    CLOCK: tuple[str, ...] = ()
    # Other result files a deck may name, no geographic-mesh primitive reading them (TOMAWAC's spectra). A deck that names one wrote it, so the run declares, checks and keeps it.
    RESULT_FILES: tuple[str, ...] = ()
    # Keywords a coupled body has only under a 3D host; the host's coupling composite refuses them by name because a 2D host never builds the field and the engine ignores them.
    ONLY_3D: frozenset[str] = frozenset()
    # What this body asserts - empty on a wrapper, by law.
    ASSERTED: Mapping[str, Any] = MappingProxyType({})
    # The keyword whose value arms a term, by the switch it turns on; a rate stated and left disarmed is a number nothing reads.
    ARMS: Mapping[str, str] = MappingProxyType({})
    # Host switches a coupled body arms, by identifier; the engine reads what the module hands back only with the switch true. The host deck keeps whatever it states, off included.
    ARMS_ON_HOST: tuple[str, ...] = ()

    @classmethod
    def seconds(cls, stated: Mapping[str, Any]) -> float | None:
        """How long a run of this module covers, off the keywords it spells it in.

        ``None`` where the module states no clock or a keyword has no number yet.
        """
        if not cls.CLOCK:
            return None
        length = 1.0
        for name in cls.CLOCK:
            value = stated.get(name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return None
            length *= float(value)
        return length

    @classmethod
    def composites(cls, reads: Mapping[str, Sequence[str]] | None = None,
                   **expanders: Callable[[Any], Any]) -> None:
        """Register the module's composites: name -> expander; ``reads`` is name -> the arguments taking an input's name."""
        cls.COMPOSITES = MappingProxyType({
            **cls.COMPOSITES,
            **{name: Composite(name=name, expand=fn,
                               reads=frozenset((reads or {}).get(name, ())))
               for name, fn in _unshadowed(cls, expanders)}})

    @classmethod
    def named(cls) -> list[str]:
        """Every input name this body's composites read, a coupled body's among them."""
        from . import wrapper_for

        found: list[str] = []
        for slot, value in cls.ASSERTED.items():
            composite = cls.COMPOSITES.get(slot)
            if composite is None:
                continue
            found += composite.names(value)
            for coupled in value if isinstance(value, (list, tuple)) else ():
                if not (isinstance(coupled, Mapping) and "slots" in coupled):
                    continue
                found += [n for n in coupled.get("given", ()) if isinstance(n, str)]
                wrapper = wrapper_for(coupled["module"])
                found += [name for key, item in coupled["slots"].items()
                          if key in wrapper.COMPOSITES
                          for name in wrapper.COMPOSITES[key].names(item)]
        return found

    @classmethod
    def reads(cls, **readers: Callable[..., Any]) -> None:
        cls.READS = MappingProxyType({**cls.READS, **dict(_unshadowed(cls, readers))})

    @classmethod
    def appends(cls, expand: Callable[[Any], Any]) -> None:
        cls.APPENDS = staticmethod(expand)

    @classmethod
    def switched(cls, identifier: str, stated: Mapping[str, Any]) -> bool:
        """Is this keyword TRUE on the deck as it stands?

        An unstated switch is the dictionary's default, not a no.
        """
        return stated.get(identifier, cls.slot(identifier).engine_default) is True

    @classmethod
    def table(cls, stated: Mapping[str, Any] = MappingProxyType({}),
              ) -> Mapping[str, Output]:
        """The module's output table as this deck carries it, by token.

        A row existing only under a keyword is here where the deck switches it on; per-class numbered rows follow the deck's count.
        """
        return MappingProxyType({
            token: row for token, row in cls.MODULE_OUTPUT.items()
            if not row.under or cls.switched(row.under, stated)})

    @classmethod
    def written(cls, stated: Mapping[str, Any] = MappingProxyType({}),
                ) -> tuple[str, ...]:
        """The tokens the printouts keyword carries, past the run's tracers.

        A row the module prints or derives is published or read, never asked for.
        """
        return tuple(token for token in cls.table(stated)
                     if token not in cls.LISTING and token not in cls.DERIVED
                     and token != cls.TRACER)

    @classmethod
    def printouts(cls, *, tracers: int = 0,
                  stated: Mapping[str, Any] = MappingProxyType({}),
                  ) -> Mapping[str, str]:
        """The table as the keyword the engine reads it from, or nothing at all.

        Every token is checked against the dictionary's choices so an unspellable table refuses here; one longer than the engine's line is spelled in its wildcard.
        """
        if not cls.PRINTOUTS:
            return {}
        slot = cls.slot(cls.PRINTOUTS)
        tokens = list(cls.written(stated))
        if cls.TRACER:
            tokens += [f"{cls.TRACER}{n}" for n in range(1, int(tracers) + 1)]
        for token in tokens:
            if not _spelled(token, slot):
                raise SlotRefused(
                    f"{cls.MODULE} rows {token!r}, which {slot.keyword} does not "
                    f"spell; its choices are {sorted(slot.choices or ())}.")
        value = ",".join(tokens)
        if len(value) > _PRINTOUTS_COLUMNS:
            value = ",".join(_wildcarded(tokens, slot, cls.UNWRITTEN))
        if len(value) > _PRINTOUTS_COLUMNS:
            raise SlotRefused(
                f"{cls.MODULE} rows {len(tokens)} variables, which {slot.keyword} "
                f"cannot carry: the engine reads this value to column "
                f"{_PRINTOUTS_COLUMNS} and truncates the rest, and no wildcard "
                f"over this table's own mnemonics is shorter than {len(value)} "
                "characters.")
        return {slot.keyword: value}

    @classmethod
    def slot(cls, identifier: str) -> Slot:
        found = cls.MODULE_INPUT.get(identifier)
        if found is None:
            raise SlotRefused(
                f"{cls.MODULE} has no keyword {identifier!r}."
                f"{_nearest(identifier, cls.MODULE_INPUT, cls.COMPOSITES)}")
        return found

    @classmethod
    def identify(cls, name: str) -> str:
        """The identifier a name is written under (raw keyword, identifier or composite); an unknown name refuses naming the nearest keyword."""
        wanted = str(name).strip()
        by_keyword = {slot.keyword: identifier
                      for identifier, slot in cls.MODULE_INPUT.items()}
        if wanted in by_keyword:
            return by_keyword[wanted]
        if wanted in cls.MODULE_INPUT or wanted in cls.COMPOSITES:
            return wanted
        close = difflib.get_close_matches(
            wanted.upper(), list(by_keyword) + list(cls.COMPOSITES), n=3)
        raise SlotRefused(
            f"{cls.MODULE} has no keyword {wanted!r}."
            + (f" Did you mean {', '.join(repr(c) for c in close)}?" if close else ""))


# How a floor name names the body it resolves against: the module's name, then the keyword, split on the first separator (a keyword may carry one, a module name may not).
_QUALIFIER = ":"


def identify_on(bodies: Sequence[type], name: str) -> tuple[type, str]:
    """The ``(body, identifier)`` a name is written under, across a run's bodies.

    A qualified ``"waqtel: K2 REAERATION COEFFICIENT"`` resolves against that body alone. A bare name resolves where exactly one body spells it; two refuse, naming both.
    """
    wanted = str(name).strip()
    if _QUALIFIER in wanted:
        module, _, keyword = wanted.partition(_QUALIFIER)
        module = module.strip().lower()
        body = next((b for b in bodies if b.MODULE == module), None)
        if body is None:
            raise SlotRefused(
                f"{module!r} names no body of this run; it runs "
                f"{', '.join(b.MODULE for b in bodies)}.")
        return body, body.identify(keyword)
    spelled = [body for body in bodies if _spells(body, wanted)]
    if len(spelled) > 1:
        raise SlotRefused(
            f"{wanted!r} is a keyword of "
            + " and of ".join(body.MODULE for body in spelled)
            + ", and the two are read apart; name the body it belongs to - "
            + " or ".join(f"{body.MODULE}{_QUALIFIER} {wanted}"
                          for body in spelled) + ".")
    if not spelled:
        # Resolved against the host so the refusal names the nearest keyword, then what else the run could have meant.
        try:
            return bodies[0], bodies[0].identify(wanted)
        except SlotRefused as refused:
            raise SlotRefused(
                f"{refused} This run also couples "
                f"{', '.join(b.MODULE for b in bodies[1:])}."
                if len(bodies) > 1 else str(refused)) from refused
    return spelled[0], spelled[0].identify(wanted)


def _spells(body: type, name: str) -> bool:
    try:
        body.identify(name)
    except SlotRefused:
        return False
    return True


# The dictionary spells a numbered token with ``i``: ``T1`` is the choice ``Ti``, ``TA1`` is ``TAi``.
_INDEXED = re.compile(r"\d+$")


# The column a DAMOCLES line is read to, also the printouts value's declared width: a longer value is truncated and a table cut mid-mnemonic stops the run.
_PRINTOUTS_COLUMNS = 72

# The engine's wildcard: ``~`` stands for the rest of a mnemonic where the character under it is a letter, so ``COV_~`` asks for every ``COV_`` plus a letter.
_WILDCARD = "~"


def _wildcarded(tokens: Sequence[str], slot: Slot,
                unwritten: Mapping[str, Any] = MappingProxyType({})) -> list[str]:
    """The same table written in the engine's wildcard, where one is safe.

    A prefix is taken only where every mnemonic the keyword spells under it is a token of this table or never written.
    """
    choices = list(slot.choices or ())
    wanted = set(tokens) | set(unwritten)
    spelled: list[str] = []
    covered: set[str] = set()
    for token in tokens:
        if token in covered:
            continue
        reached = [(token[:n], _matched(token[:n], choices))
                   for n in range(1, len(token))]
        group = next(((prefix, under) for prefix, under in reached
                      if len(under) > 1 and under <= wanted), None)
        if group is None:
            spelled.append(token)
            continue
        spelled.append(group[0] + _WILDCARD)
        covered |= group[1]
    return spelled


def _matched(prefix: str, choices: Sequence[str]) -> set[str]:
    return {choice for choice in choices
            if choice.startswith(prefix) and len(choice) > len(prefix)
            and choice[len(prefix)].isalpha()}


def _spelled(token: str, slot: Slot) -> bool:
    choices = slot.choices or ()
    return token in choices or _INDEXED.sub("i", token) in choices


def _unshadowed(cls: type, registered: Mapping[str, Any]) -> list[tuple[str, Any]]:
    for name in registered:
        if name in cls.MODULE_INPUT:
            raise SlotRefused(
                f"{cls.MODULE} already has the keyword {name!r}; a registration "
                "may not shadow a keyword.")
    return list(registered.items())


# A fill states this where the engine's steering-file class cannot be imported: the keyword is held to this module's copy of the dictionary.
ENGINE_UNAVAILABLE = "the engine check is unavailable at fill; launch runs it"


def accept(body: type, name: str, value: Any) -> tuple[str, Any, str]:
    """One keyword at fill -> ``(identifier, value, note)``, or the refusal.

    Name, type and choices are the engine's class to judge; arity and range are ours. Without the engine tree, ours judges all and the note says so.
    """
    identifier = body.identify(name)
    if identifier not in body.MODULE_INPUT:
        return identifier, value, ""
    slot = body.MODULE_INPUT[identifier]
    if slot.is_file:
        return identifier, slot.check(value), ""
    engine = engine_check(body.MODULE)
    if engine is None:
        return identifier, slot.check(value), ENGINE_UNAVAILABLE
    refusal = engine(slot.keyword, list(value) if isinstance(value, tuple)
                     else value)
    if refusal:
        raise SlotRefused(f"{slot.keyword} refused by the engine: {refusal}")
    return identifier, slot.fits(value), ""


def engine_check(module: str) -> Any:
    """``(keyword, value) -> refusal or ""`` through the engine's ``TelemacCas`` from the tree ``HOMETEL`` names; ``None`` where that tree or dictionary is unreachable."""
    home = os.environ.get("HOMETEL", "")
    dico = Path(home, "sources", module, f"{module}.dico")
    if not home or not dico.is_file():
        return None
    scripts = str(Path(home, "scripts", "python3"))
    if scripts not in sys.path:
        sys.path.append(scripts)
    try:
        from execution.telemac_cas import TelemacCas
    except Exception:  # noqa: BLE001 - any import failure is the absent tree
        return None

    def check(keyword: str, value: Any) -> str:
        cas = TelemacCas(f"{module}.cas", str(dico), access="w")
        cas.lang = "en"
        try:
            cas.set(keyword, value)
            cas._check_choix()
        except Exception as exc:  # noqa: BLE001 - the engine's refusal is the answer
            return str(exc).strip()
        return ""

    return check
