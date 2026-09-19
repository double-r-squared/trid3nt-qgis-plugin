"""A TELEMAC module's keyword surface, as the engine publishes it.

Every assertion is DATA, fixed when the module is imported: a body reads no value
any fill produced, and every refusal - an unknown keyword, a wrong type, a body
extending anything but its wrapper - is raised at IMPORT time."""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import FunctionType, MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from trid3nt_server.workflows.runtime import DeclarativeError, ParamRef, Ref

__all__ = [
    "Composite",
    "Module",
    "Output",
    "Slot",
    "SlotRefused",
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

#: Class attributes a wrapper carries that are never keyword assertions.
_RESERVED = frozenset((
    "MODULE", "MODULE_INPUT", "COMPOSITES", "READS", "ASSERTED", "ARMS",
    "MODULE_OUTPUT", "LISTING", "DERIVED", "PRINTOUTS", "CADENCE", "TRACER",
    "APPENDS", "APPENDABLE", "ONLY_3D",
    "RESULT_FILE", "composites", "reads", "appends", "printouts", "slot",
))


def module_input_dir() -> Path:
    """Where the committed module inputs live."""
    return Path(__file__).resolve().parent / "module_input"


#: PLAUSIBILITY BOUNDS beside the dictionary row, by module and identifier: the
#: UNIT the keyword is read in and the range a value of it is taken inside, and
#: refused by name outside. A sidecar rather than a column in the dictionary
#: itself, because the dictionary is the image's own and is compared to it byte
#: for byte - and the dictionary carries the unit only in the prose of its help,
#: which is not a fact a card row can render. One ``[lo, hi]`` bounds every
#: value the keyword holds; a list of them is one per element, in the keyword's
#: own order. This is the table calibration reads.
_BOUNDS_FILE = Path(__file__).resolve().parent / "module_bounds.json"


@lru_cache(maxsize=None)
def _bounds(module: str) -> Mapping[str, tuple[str, tuple]]:
    """The sidecar's ``(unit, bounds)`` rows for one module, by identifier."""
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
    #: The unit this keyword's value is read in, as the bounds sidecar states
    #: it; empty on a keyword nothing bounds and on a dimensionless one.
    unit: str = ""
    #: The plausibility range each of this keyword's values is taken inside: one
    #: pair for every value, or one pair per element in the keyword's own order.
    bounds: tuple[tuple[float, float], ...] = ()
    #: This keyword's ONE value is a separator-joined selection from its
    #: choices, so the choices do not name whole values.
    multi_select: bool = False

    @property
    def is_list(self) -> bool:
        """TAILLE is the value's ARITY. Being open-ended says the length is not
        fixed at it, not that a keyword of arity one carries several values -
        the engine reads only the first of those and says nothing."""
        return (self.size or 1) > 1

    @property
    def is_open(self) -> bool:
        """The dictionary gives this keyword no default, so nothing answers it.

        Lists included: an undefaulted list is empty until something states it."""
        return self.engine_default is UNSET

    @property
    def is_required(self) -> bool:
        """The engine will not start without this one: an OBLIG file, undefaulted.

        The dictionary's OBLIG mark is the only thing a run refuses on."""
        # What else the engine demands it demands from its own listing, where
        # LECDON names the keyword; a required set invented here would be this
        # code guessing at the Fortran's conditions.
        return self.is_file and self.file_mandatory and self.is_open

    def check(self, value: Any) -> Any:
        """``value`` as this slot takes it, or the refusal that says why not.

        A late-bound READ passes through and is checked at the fill instead."""
        if isinstance(value, (Ref, ParamRef)):
            return value
        if self.is_list:
            if not isinstance(value, (list, tuple)):
                raise SlotRefused(
                    f"{self.keyword} takes a list of {self.type} values"
                    + (f" ({self.size} of them)" if not self.unbounded else "")
                    + f"; got {value!r}.")
            if not self.unbounded and len(value) != self.size:
                raise SlotRefused(
                    f"{self.keyword} takes exactly {self.size} values; "
                    f"got {len(value)}.")
            # A LIST's choices are the ENGINE'S to check, not ours: the
            # dictionary spells a tracer choice as T*, kSi, T1*, and telapy's
            # own reader is what knows those spellings. It reads the written
            # file back, and that round trip is the gate.
            #
            # A late-bound read INSIDE the list passes through for the same
            # reason it does outside one: a body states what it will hold, and
            # the fill that substitutes the value is what the value is checked at.
            return [item if isinstance(item, (Ref, ParamRef))
                    else self._bounded(self._typed(item), position)
                    for position, item in enumerate(value)]
        value = self._bounded(self._typed(value), 0)
        if self.choices and not self.multi_select and str(value) not in self.choices:
            raise SlotRefused(
                f"{self.keyword} does not take {value!r}. The dictionary's "
                f"choices are {self._named_choices()}.")
        return value

    def _bounded(self, value: Any, position: int) -> Any:
        """``value`` if the bounds row takes it, or the refusal that names both.

        A keyword the sidecar rows no bounds for is bounded by the engine alone."""
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
        """``value`` if it is what the dictionary says this keyword holds."""
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


#: What each dictionary type is in Python. LOGICAL is separated from INTEGER in
#: ``_scalar`` because a bool IS an int and the two are not interchangeable here.
_TYPES: Mapping[str, Any] = {
    "INTEGER": int, "REAL": (int, float), "LOGICAL": bool, "STRING": str,
}


@dataclass(frozen=True, slots=True)
class Composite:
    """One value standing for several slots, and the file they name.

    ``expand`` is ``(value) -> (slots, files)``, keyed by identifier and basename."""

    name: str
    expand: Callable[[Any], tuple[Mapping[str, Any], Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class Output:
    """One variable the module writes: what the result file calls it, the unit it
    is read in, the style row it draws under, whether it varies in time, and
    whether it has a visible EDGE.

    A row that does not vary is painted and never animated. A row with an edge
    is a quantity INJECTED into the domain, masked below a fraction of its own
    magnitude so what is visible reads as the shape it has; a variable the water
    already carries is everywhere and is drawn whole. ``None`` leaves it to the
    table the row is read off: a tracer row has an edge, a written variable
    does not."""

    name: str
    unit: str = ""
    style: Mapping[str, Any] | None = None
    varies: bool = True
    has_edge: bool | None = None
    #: The keyword this row EXISTS UNDER, by identifier: the engine allocates
    #: the variable only with it true, and asking for a variable it did not
    #: allocate stops the solve rather than dropping the row. Empty on a row
    #: the module always carries. The DECK decides it, and a deck that leaves it
    #: unstated decides it at the dictionary's own default, because that is the
    #: value the engine then reads.
    under: str = ""


@lru_cache(maxsize=None)
def load_module_input(module: str) -> Mapping[str, Slot]:
    """The module's whole keyword surface, keyed by the identifier it is written
    under. The dictionary's own order is kept: a sheet reads down it."""
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

    A body's namespace is checked against the dictionary at import."""

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
    """A body extends the WRAPPER and nothing else: a value two bodies share is
    restated in each, under the template that states it."""
    for base in bases:
        if getattr(base, "ASSERTED", None):
            raise SlotRefused(
                f"{cls.__name__} extends {base.__name__}, which is a body. A body "
                f"extends its module's wrapper; restate the values it needs.")


def _asserted(cls: type, namespace: Mapping[str, Any],
              dictionary: Mapping[str, Slot]) -> dict[str, Any]:
    """This body's own assertions, each checked against the keyword it names."""
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
        # None is not a value any keyword takes, so the one thing it can mean is
        # that this body states nothing here and the dictionary's default stands.
        asserted[key] = None if value is None else slot.check(value)
    return asserted


def _is_method(value: Any) -> bool:
    """Is this namespace entry the body's own code rather than a slot's value?

    A keyword's value is data; no dictionary spells a keyword as a callable."""
    return isinstance(value, (classmethod, staticmethod, FunctionType))


def _nearest(key: str, dictionary: Mapping[str, Slot],
             composites: Mapping[str, Any]) -> str:
    """The keyword the misspelling was probably reaching for."""
    close = difflib.get_close_matches(key, list(dictionary) + list(composites), n=3)
    return f" Did you mean {', '.join(close)}?" if close else ""


class Module(metaclass=_Body):
    """A module's dictionary, its composites, what it writes and how it is read.

    There is no hook for a default: the engine's default is the whole position."""

    #: The engine module this wraps, e.g. ``telemac2d``.
    MODULE: str = ""
    #: Every keyword the module has, by identifier.
    MODULE_INPUT: Mapping[str, Slot] = MappingProxyType({})
    COMPOSITES: Mapping[str, Composite] = MappingProxyType({})
    #: What the module WRITES, by the mnemonic its printouts keyword spells: one
    #: row per variable, and the whole statement - a variable the dictionary
    #: offers and this table does not row is not written.
    MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({})
    #: The primitives over that output: kind -> the read of it off a solved run.
    READS: Mapping[str, Callable[..., Any]] = MappingProxyType({})
    #: The rows the module PRINTS in its listing rather than writes to its result
    #: file; a series of one is read off the listing.
    LISTING: frozenset[str] = frozenset()
    #: The rows the module defines OVER the variables its result carries, each
    #: ``(solved) -> (name, units, values(nframes, npoin2))``.
    DERIVED: Mapping[str, Callable[..., Any]] = MappingProxyType({})
    #: The keyword the table is written into, by identifier; empty on a module
    #: that writes no result of its own.
    PRINTOUTS: str = ""
    #: The keyword saying HOW OFTEN that table is written, by identifier; empty
    #: on a module that does not march in time, which writes one record. Where a
    #: module names one, a deck that states no value for it refuses: the
    #: dictionary's own default is every step, which is an animation nobody sized.
    CADENCE: str = ""
    #: The token a tracer takes in that keyword, which is also the row its style
    #: is under; empty on a module with no tracer surface.
    TRACER: str = ""
    #: What this module APPENDS to its carrier's tracers, ``(body) -> rows``;
    #: ``None`` where it appends none.
    APPENDS: Callable[[Any], Any] | None = None
    #: The rows that hook can append and WHEN, as ``(condition, rows)`` the hook
    #: itself reads - so what a module may put on its carrier is enumerable
    #: without a body to run it against.
    APPENDABLE: tuple[tuple[str, tuple[Output, ...]], ...] = ()
    #: The result file the primitives read; empty reads the run's own.
    RESULT_FILE: str = ""
    #: The keywords a COUPLED body of this module has only under a
    #: three-dimensional host, by identifier. The host's own coupling composite
    #: refuses them by name, because a two-dimensional host never builds the
    #: field they describe and the engine reads them off a deck it then ignores.
    ONLY_3D: frozenset[str] = frozenset()
    #: What THIS body asserts - empty on a wrapper, by law.
    ASSERTED: Mapping[str, Any] = MappingProxyType({})
    #: The keyword whose value ARMS a term, by the switch it turns on. The
    #: engine reads the value only with the switch true, so a rate stated by
    #: name and left disarmed is a number nothing reads.
    ARMS: Mapping[str, str] = MappingProxyType({})

    @classmethod
    def composites(cls, **expanders: Callable[[Any], Any]) -> None:
        """Register the module's composites: name -> its expander."""
        cls.COMPOSITES = MappingProxyType({
            **cls.COMPOSITES,
            **{name: Composite(name=name, expand=fn)
               for name, fn in _unshadowed(cls, expanders)}})

    @classmethod
    def reads(cls, **readers: Callable[..., Any]) -> None:
        """Register the primitives over the module's output: kind -> the read."""
        cls.READS = MappingProxyType({**cls.READS, **dict(_unshadowed(cls, readers))})

    @classmethod
    def appends(cls, expand: Callable[[Any], Any]) -> None:
        """Register what a coupled body of this module appends to its carrier."""
        cls.APPENDS = staticmethod(expand)

    @classmethod
    def switched(cls, identifier: str, stated: Mapping[str, Any]) -> bool:
        """Is this keyword TRUE on the deck as it stands?

        A deck that states nothing is a deck the engine reads the dictionary's
        own default for, so an unstated switch is that default and not a no."""
        return stated.get(identifier, cls.slot(identifier).engine_default) is True

    @classmethod
    def table(cls, stated: Mapping[str, Any] = MappingProxyType({}),
              ) -> Mapping[str, Output]:
        """The module's output table as THIS deck carries it, by token.

        A row that exists only UNDER a keyword is here where the deck switches
        that keyword on; a wrapper whose engine numbers rows per class writes
        those numbered rows here, off the count the deck itself states."""
        return MappingProxyType({
            token: row for token, row in cls.MODULE_OUTPUT.items()
            if not row.under or cls.switched(row.under, stated)})

    @classmethod
    def written(cls, stated: Mapping[str, Any] = MappingProxyType({}),
                ) -> tuple[str, ...]:
        """The tokens the printouts keyword carries, past the run's tracers.

        A row the module prints or derives is published or read, never asked for."""
        return tuple(token for token in cls.table(stated)
                     if token not in cls.LISTING and token not in cls.DERIVED
                     and token != cls.TRACER)

    @classmethod
    def printouts(cls, *, tracers: int = 0,
                  stated: Mapping[str, Any] = MappingProxyType({}),
                  ) -> Mapping[str, str]:
        """The table as the keyword the engine reads it from, or nothing at all.

        Every token is checked against the dictionary's own choices, so a table
        the engine would not spell refuses here rather than in the Fortran; a
        table longer than the engine's own line is spelled in its own wildcard."""
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
            value = ",".join(_wildcarded(tokens, slot))
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
        """One slot by identifier, or the refusal that names the nearest."""
        found = cls.MODULE_INPUT.get(identifier)
        if found is None:
            raise SlotRefused(
                f"{cls.MODULE} has no keyword {identifier!r}."
                f"{_nearest(identifier, cls.MODULE_INPUT, cls.COMPOSITES)}")
        return found

    @classmethod
    def identify(cls, name: str) -> str:
        """The identifier a name is written under: raw keyword, identifier or
        composite. An unknown name refuses, naming the nearest keyword the
        dictionary itself spells."""
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


#: How a floor name NAMES THE BODY it is resolved against: the module's own
#: name, then the keyword. Split on the FIRST separator, because a keyword may
#: carry one and a module name may not.
_QUALIFIER = ":"


def identify_on(bodies: Sequence[type], name: str) -> tuple[type, str]:
    """The ``(body, identifier)`` a name is written under, across a run's bodies.

    A qualified ``"waqtel: K2 REAERATION COEFFICIENT"`` resolves against that
    body alone. A bare name resolves where exactly ONE body spells it; two
    refuses, naming both qualified spellings, because the carrier and its
    coupled deck read their same-named keywords apart."""
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
        # Resolved against the HOST so the refusal names the nearest keyword it
        # spells, then says what else this run could have been reaching for.
        try:
            return bodies[0], bodies[0].identify(wanted)
        except SlotRefused as refused:
            raise SlotRefused(
                f"{refused} This run also couples "
                f"{', '.join(b.MODULE for b in bodies[1:])}."
                if len(bodies) > 1 else str(refused)) from refused
    return spelled[0], spelled[0].identify(wanted)


def _spells(body: type, name: str) -> bool:
    """Does this body have a keyword, an identifier or a composite by this name?"""
    try:
        body.identify(name)
    except SlotRefused:
        return False
    return True


#: How the dictionary spells a NUMBERED token: the index is written ``i``, so
#: ``T1`` is the choice ``Ti`` and ``TA1`` the choice ``TAi``.
_INDEXED = re.compile(r"\d+$")


#: The column a DAMOCLES line is read to, which is also the width the engine
#: declares the printouts value at: a longer value is TRUNCATED, and a table cut
#: mid-mnemonic stops the run on a word it cannot spell.
_PRINTOUTS_COLUMNS = 72

#: The engine's own wildcard in that keyword: ``~`` stands for the rest of a
#: mnemonic wherever the character under it is a LETTER, so ``COV_~`` asks for
#: every mnemonic spelled ``COV_`` and a letter after it.
_WILDCARD = "~"


def _wildcarded(tokens: Sequence[str], slot: Slot) -> list[str]:
    """The same table written in the engine's wildcard, where one is safe.

    A prefix is taken only where every mnemonic the keyword spells under it is
    already a token of this table, so the shorter spelling asks the engine for
    exactly what the table rows and never for a variable it did not row."""
    choices = list(slot.choices or ())
    wanted = set(tokens)
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
    """Every choice ``prefix~`` reaches, by the engine's own matching rule."""
    return {choice for choice in choices
            if choice.startswith(prefix) and len(choice) > len(prefix)
            and choice[len(prefix)].isalpha()}


def _spelled(token: str, slot: Slot) -> bool:
    """Is ``token`` one the keyword's own choices carry, numbered or plain?"""
    choices = slot.choices or ()
    return token in choices or _INDEXED.sub("i", token) in choices


def _unshadowed(cls: type, registered: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """Registrations whose names are the wrapper's own to give."""
    for name in registered:
        if name in cls.MODULE_INPUT:
            raise SlotRefused(
                f"{cls.MODULE} already has the keyword {name!r}; a registration "
                "may not shadow a keyword.")
    return list(registered.items())
