"""A TELEMAC module's keyword surface, as the engine publishes it.

Every assertion is DATA, fixed when the module is imported: a body reads no value
any fill produced, and every refusal - an unknown keyword, a wrong type, a body
extending anything but its wrapper - is raised at IMPORT time."""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import FunctionType, MappingProxyType
from typing import Any, Callable, Mapping

from trid3nt_server.workflows.runtime import DeclarativeError, ParamRef, Ref

__all__ = [
    "Composite",
    "Module",
    "Output",
    "Slot",
    "SlotRefused",
    "dictionary_dir",
    "load_dictionary",
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
    "MODULE", "DICTIONARY", "COMPOSITES", "OUTPUTS", "ASSERTED",
    "VARIABLES", "LISTING", "DERIVED", "RESULT_FILE", "composites", "outputs",
    "slot",
))


def dictionary_dir() -> Path:
    """Where the committed dictionaries live."""
    return Path(__file__).resolve().parents[1] / "dictionary"


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
            return [item if isinstance(item, (Ref, ParamRef)) else self._typed(item)
                    for item in value]
        value = self._typed(value)
        if self.choices and not self.multi_select and str(value) not in self.choices:
            raise SlotRefused(
                f"{self.keyword} does not take {value!r}. The dictionary's "
                f"choices are {self._named_choices()}.")
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
    """One of the module's outputs: a primitive, bound to the read of it."""

    name: str
    read: Callable[..., Any]


@lru_cache(maxsize=None)
def load_dictionary(module: str) -> Mapping[str, Slot]:
    """The module's whole keyword surface, keyed by the identifier it is written
    under. The dictionary's own order is kept: a sheet reads down it."""
    path = dictionary_dir() / f"{module}.json"
    if not path.is_file():
        raise SlotRefused(
            f"no dictionary for {module!r}; the exposed modules are "
            f"{sorted(p.stem for p in dictionary_dir().glob('*.json'))}.")
    rows = json.loads(path.read_text())["keywords"]
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
        dictionary = load_dictionary(name)
        return _Body(name.upper(), (Module,), {
            "__doc__": f"The {name} keyword surface: {len(dictionary)} slots.",
            "MODULE": name, "DICTIONARY": dictionary,
            "COMPOSITES": MappingProxyType({}), "OUTPUTS": MappingProxyType({}),
            "ASSERTED": MappingProxyType({}),
        })

    def __new__(mcls, name: str, bases: tuple, namespace: dict) -> type:
        cls = super().__new__(mcls, name, bases, dict(namespace))
        dictionary = namespace.get("DICTIONARY") or getattr(cls, "DICTIONARY", None)
        if dictionary is None or "DICTIONARY" in namespace:
            return cls
        _refuse_extended_body(cls, bases)
        cls.ASSERTED = MappingProxyType(_asserted(cls, namespace, dictionary))
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
    """A module's dictionary, its composites and its outputs. It asserts nothing.

    There is no hook for a default: the engine's default is the whole position."""

    #: The engine module this wraps, e.g. ``telemac2d``.
    MODULE: str = ""
    #: Every keyword the module has, by identifier.
    DICTIONARY: Mapping[str, Slot] = MappingProxyType({})
    COMPOSITES: Mapping[str, Composite] = MappingProxyType({})
    OUTPUTS: Mapping[str, Output] = MappingProxyType({})
    #: The module's variable vocabulary: mnemonic -> (result name, unit).
    VARIABLES: Mapping[str, tuple[str, str]] = MappingProxyType({})
    #: The tokens of that vocabulary the module PRINTS in its listing rather
    #: than writes to its result file; a series of one is read off the listing.
    LISTING: frozenset[str] = frozenset()
    #: The tokens of that vocabulary the module defines OVER the variables its
    #: result carries, each ``(solved) -> (name, units, values(nframes, npoin2))``.
    DERIVED: Mapping[str, Callable[..., Any]] = MappingProxyType({})
    #: The result file the primitives read; empty reads the run's own.
    RESULT_FILE: str = ""
    #: What THIS body asserts - empty on a wrapper, by law.
    ASSERTED: Mapping[str, Any] = MappingProxyType({})

    @classmethod
    def composites(cls, **expanders: Callable[[Any], Any]) -> None:
        """Register the module's composites: name -> its expander."""
        cls.COMPOSITES = MappingProxyType({
            **cls.COMPOSITES,
            **{name: Composite(name=name, expand=fn)
               for name, fn in _unshadowed(cls, expanders)}})

    @classmethod
    def outputs(cls, **readers: Callable[..., Any]) -> None:
        """Register the module's outputs: primitive -> the read of it."""
        cls.OUTPUTS = MappingProxyType({
            **cls.OUTPUTS,
            **{name: Output(name=name, read=fn)
               for name, fn in _unshadowed(cls, readers)}})

    @classmethod
    def slot(cls, identifier: str) -> Slot:
        """One slot by identifier, or the refusal that names the nearest."""
        found = cls.DICTIONARY.get(identifier)
        if found is None:
            raise SlotRefused(
                f"{cls.MODULE} has no keyword {identifier!r}."
                f"{_nearest(identifier, cls.DICTIONARY, cls.COMPOSITES)}")
        return found

    @classmethod
    def identify(cls, name: str) -> str:
        """The identifier a name is written under: raw keyword, identifier or
        composite. An unknown name refuses, naming the nearest keyword the
        dictionary itself spells."""
        wanted = str(name).strip()
        by_keyword = {slot.keyword: identifier
                      for identifier, slot in cls.DICTIONARY.items()}
        if wanted in by_keyword:
            return by_keyword[wanted]
        if wanted in cls.DICTIONARY or wanted in cls.COMPOSITES:
            return wanted
        close = difflib.get_close_matches(
            wanted.upper(), list(by_keyword) + list(cls.COMPOSITES), n=3)
        raise SlotRefused(
            f"{cls.MODULE} has no keyword {wanted!r}."
            + (f" Did you mean {', '.join(repr(c) for c in close)}?" if close else ""))


def _unshadowed(cls: type, registered: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """Registrations whose names are the wrapper's own to give."""
    for name in registered:
        if name in cls.DICTIONARY:
            raise SlotRefused(
                f"{cls.MODULE} already has the keyword {name!r}; a registration "
                "may not shadow a keyword.")
    return list(registered.items())
