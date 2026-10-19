"""The engine-neutral module surface: a keyword dictionary of slots, composites and outputs, and the wrapper metaclass.

A wrapper is built from a name and the directory its dictionary lives in; the engine's subclass states that directory, its engine check, its partition keyword and the box it admits a value to.

Every assertion is data, fixed at import: a body reads no value any fill produced, and every refusal (unknown keyword, wrong type, a body extending anything but its wrapper) is raised at import.
"""

from __future__ import annotations

import difflib
import inspect
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import FunctionType, MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from trid3nt_server.errors import DeclarativeError
from trid3nt_server.inputs.validate import scalar

__all__ = [
    "Composite",
    "MOMENT",
    "Module",
    "Output",
    "Slot",
    "SlotRefused",
    "UNSET",
    "accept",
    "identify_on",
    "load_module_input",
]


class SlotRefused(DeclarativeError):
    """A keyword the module does not have, or a value the dictionary does not take."""

    error_code = "SLOT_REFUSED"


class _Unset:
    """The absence of an engine default - which is what makes a slot mandatory."""

    def __repr__(self) -> str:
        return "unset"


UNSET = _Unset()

# What a run holds once it states the moment it opens at: ``at`` the UTC datetime, ``lon`` and ``lat`` the domain's own point. A module names the keywords it spells them in under this in FILLED_BY; a run stating no moment holds none.
MOMENT = "moment"

# Class attributes a wrapper carries that are never keyword assertions.
_RESERVED = frozenset((
    "MODULE", "MODULE_INPUT", "COMPOSITES", "READS", "ASSERTED", "ARMS",
    "ARMS_ON_HOST", "MODULE_OUTPUT", "LISTING", "DERIVED", "PRINTOUTS",
    "CADENCE", "CLOCK", "TRACER", "APPENDS", "APPENDABLE", "ONLY_3D",
    "UNWRITTEN", "RESULT_FILE", "RESULT_FILES", "RESULT_KEYWORD", "FILLED_BY",
    "CODE_ON_HOST", "PLACED_AGAINST", "PARTITION_KEYWORD",
    "composites",
    "reads", "appends", "printouts", "seconds", "slot",
))


# Plausibility bounds beside the dictionary row, by module and identifier: the unit the keyword is
# read in and the range a value is taken inside (refused by name outside). A sidecar because the
# dictionary is the image's own, compared byte for byte, and carries units only in help prose. One
# ``[lo, hi]`` bounds every value; a list is one per element in the keyword's order. Calibration reads this table.
@lru_cache(maxsize=None)
def _bounds(module: str, *, directory: Path) -> Mapping[str, tuple[str, tuple]]:
    rows = json.loads(Path(directory).parent.joinpath("module_bounds.json").read_text()).get(module) or {}
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
    # The dictionary does not mark this keyword optional and gives it no default, so a run stating nothing refuses on it - MODFLOW's shape; a TELEMAC row leaves it false and is required only through its OBLIG file mark.
    mandatory: bool = False
    # The DFN's own shape for an ARRAY keyword (``(ncpl)`` per cell, ``(ncpl, nlay)`` per layer too), empty on the types it declares no shape for; the grid-data author places a value against it.
    shape: str = ""
    # The DFN marks this array layered: its value is one number for every layer, a row of cells per layer, or a name per layer.
    layered: bool = False

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
        """The engine will not start without this one: undefaulted and either an OBLIG file or a keyword the dictionary does not mark optional.

        Other demands come from the engine's own listing (LECDON names the keyword) or from the writer's validate; a set invented here would guess at the Fortran's conditions.
        """
        return self.is_open and (self.mandatory
                                 or (self.is_file and self.file_mandatory))

    @property
    def whole_bounds(self) -> tuple[float, float] | None:
        """The ONE pair that bounds the whole value - what a card clamps an editor to and a refusal offers as its remedy.
        A keyword with a pair per element has no single range, and the fill's refusal names the element."""
        return self.bounds[0] if len(self.bounds) == 1 else None

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
        if self.keyword in _UNREADABLE:
            raise SlotRefused(
                f"{self.keyword} cannot be stated: the engine's dictionary "
                "spells it with a doubled space, which its launcher's reader "
                "collapses and its Fortran reader does not, so no steering "
                "file states it and gets past both.")
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
        """``value`` if the bounds row takes it, or the refusal that names it.

        A keyword with no bounds row is bounded by the engine alone.
        """
        if not self.bounds or self.type in ("STRING", "ARRAY", "STRUCT"):
            return value
        many = len(self.bounds) > 1
        try:
            scalar(self.keyword, value,
                   self.bounds[position if many else 0], units=self.unit,
                   position=position if many else None,
                   beyond="A value outside it is either a unit this keyword "
                          "is not read in or a run nobody would believe.")
        except ValueError as refused:
            raise SlotRefused(str(refused)) from None
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


# Keywords neither spelling of which both of the engine's readers take; a deck stating one stops before the solve.
_UNREADABLE = frozenset({"LOCAL LONGITUDE, IN DEGREES"})

# What each dictionary type is in Python; LOGICAL is separated from INTEGER in ``_scalar`` because a bool is an int. ARRAY and STRUCT are MODFLOW's dictionary words: an array is a constant, one value per layer, the NAME of an input holding the grid, or a mapping stating cells; a structured block is a filename, a table, or the mapping the authoring writes.
_TYPES: Mapping[str, Any] = {
    "INTEGER": int, "REAL": (int, float), "LOGICAL": bool, "STRING": str,
    "ARRAY": (int, float, str, list, tuple, dict),
    "STRUCT": (str, list, tuple, dict),
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
        """Every input name ``value`` states in an argument that takes one, inside a mapping or a list of statements."""
        if isinstance(value, (list, tuple)):
            return [name for item in value for name in self.names(item)]
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
        if isinstance(value, (list, tuple)):
            value = [self._reading(item, run) for item in value]
        value = self._reading(value, run)
        if "run" in inspect.signature(self.expand).parameters:
            return self.expand(value, run=run)
        return self.expand(value)

    def _reading(self, value: Any, run: Mapping[str, Any]) -> Any:
        if not isinstance(value, Mapping):
            return value
        return {key: _read(run, item, self.name) if key in self.reads
                else item for key, item in value.items()}


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


@lru_cache(maxsize=None)
def load_module_input(module: str, *, directory: Path) -> Mapping[str, Slot]:
    """The module's whole keyword surface by identifier, in the dictionary's order (a sheet reads down it)."""
    path = Path(directory) / f"{module}.json"
    if not path.is_file():
        raise SlotRefused(
            f"no module input for {module!r}; the exposed modules are "
            f"{sorted(p.stem for p in Path(directory).glob('*.json'))}.")
    rows = json.loads(path.read_text())["keywords"]
    bounded = _bounds(module, directory=directory)
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
        mandatory=row.get("mandatory", False),
        shape=row.get("shape", ""),
        layered=row.get("layered", False),
        unit=bounded.get(row["identifier"], unbounded_row)[0],
        bounds=bounded.get(row["identifier"], unbounded_row)[1],
    ) for row in rows})


class _Body(type):
    """The metaclass every wrapper and every body extending one is made by.

    A body's namespace is checked against the dictionary at import.
    """

    def __call__(cls, *args: str) -> type:
        if cls.MODULE:
            raise SlotRefused(
                f"{cls.__name__} is a declaration, not a value; fill() makes a "
                "sheet from it.")
        (name,) = args
        module_input = load_module_input(name, directory=cls.dictionary_dir())
        return _Body(name.upper(), (cls,), {
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
    # Keywords a run input fills itself, by the NAME of the row's type or of a placement: identifier -> value read off what the input holds. A body that states one wins.
    FILLED_BY: Mapping[str, Mapping[str, Callable[[Any], Any]]] = MappingProxyType({})
    # What the module writes, by printouts mnemonic: one row per variable; a variable the dictionary offers and this table does not row is not written.
    MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({})
    # The primitives over that output: kind -> the read off a solved run.
    READS: Mapping[str, Callable[..., Any]] = MappingProxyType({})
    # Rows the module prints in its listing rather than writing to its result file; a series of one is read off the listing.
    LISTING: frozenset[str] = frozenset()
    # Rows defined over the result's variables, each ``(solved) -> (name, units, values(nframes, npoin2))``.
    DERIVED: Mapping[str, Callable[..., Any]] = MappingProxyType({})
    # The keyword saying how often the table is written; empty on a module that does not march in time. Where named, a deck stating none refuses: the dictionary default is every step, an animation nobody sized.
    CADENCE: str = ""
    # What this module appends to its carrier's tracers, ``(body) -> rows``; ``None`` if none.
    APPENDS: Callable[[Any], Any] | None = None
    # The rows that hook can append and when, as ``(condition, rows)``, enumerable without a body.
    APPENDABLE: tuple[tuple[str, tuple[Output, ...]], ...] = ()
    # The result file the primitives read; empty reads the run's own.
    RESULT_FILE: str = ""
    # The keyword a deck NAMES that result in; modules spell it differently (TOMAWAC: 2D RESULTS FILE), so readers ask the module.
    RESULT_KEYWORD: str = "RESULTS_FILE"
    # How the module spells its run length: keywords whose product is the seconds covered (one for the window, two for a step and a count). Empty on a module that does not march in time.
    CLOCK: tuple[str, ...] = ()
    # Other result files a deck may name, no geographic-mesh primitive reading them (TOMAWAC's spectra). A deck that names one wrote it, so the run declares, checks and keeps it.
    RESULT_FILES: tuple[str, ...] = ()
    # What this body asserts - empty on a wrapper, by law.
    ASSERTED: Mapping[str, Any] = MappingProxyType({})
    # The keyword whose value arms a term, by the switch it turns on; a rate stated and left disarmed is a number nothing reads.
    ARMS: Mapping[str, str] = MappingProxyType({})
    # A derived keyword and the keywords its derivation reads at the dictionary's value; one of those stated while it is still derived refuses.
    PLACED_AGAINST: Mapping[str, tuple[str, ...]] = MappingProxyType({})
    # The keyword this engine partitions a run under; empty says this engine has none, and identify_on then reads no bare name as the carrier's.
    PARTITION_KEYWORD: str = ""

    @classmethod
    def dictionary_dir(cls) -> Path:
        """The directory this engine's dictionaries load from - the engine's module states it."""
        raise NotImplementedError("dictionary_dir() is the engine's module to state")

    @classmethod
    def engine_check(cls, module: str) -> Any:
        """The engine's ``(keyword, value) -> refusal`` over its own dictionary, or None where it is unreachable."""
        return None

    @classmethod
    def wrapper_for(cls, module: str) -> type:
        """The wrapper registered for ``module`` - the engine's registry answers it."""
        raise DeclarativeError(ENGINE_UNAVAILABLE)

    @classmethod
    def fits_box(cls, identifier: str, value: Any) -> None:
        """A value held to the box this run solves on - the engine's own boxes judge it."""
        return None

    @classmethod
    def sheet(cls, filled: dict[str, Any], files: dict[str, Any],
              run: Mapping[str, Any]) -> Any:
        """The filled sheet a deck makes from these slots - the engine's module builds it."""
        raise DeclarativeError(ENGINE_UNAVAILABLE)

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
                wrapper = cls.wrapper_for(coupled["module"])
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
    The engine's partition keyword is the exception: a run is partitioned once, across the carrier's, so the bare name is the carrier's.
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
    if spelled[:1] == [bodies[0]] and (
            bodies[0].identify(wanted) == bodies[0].PARTITION_KEYWORD):
        return bodies[0], bodies[0].PARTITION_KEYWORD
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
    engine = body.engine_check(body.MODULE)
    if engine is None:
        return identifier, slot.check(value), ENGINE_UNAVAILABLE
    refusal = engine(slot.keyword, list(value) if isinstance(value, tuple)
                     else value)
    if refusal:
        raise SlotRefused(f"{slot.keyword} refused by the engine: {refusal}")
    return identifier, slot.fits(value), ""
