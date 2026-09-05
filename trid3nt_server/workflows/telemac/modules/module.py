"""A TELEMAC module's keyword surface, as the engine publishes it.

Every keyword in a module's dictionary is a SLOT: its raw name, the help the
dictionary writes for it, its type, its allowed values, its engine default, its
level and whether it names a file. The catalog under ``telemac/catalog/`` is that
dictionary, extracted in-image; nothing here transcribes a keyword by hand.

A MODULE is the class the catalog makes. It asserts NO value of its own - it is
the analog of the engine's own defaults - and it carries the two other things a
wrapper holds: COMPOSITES, where one value stands for several slots and the file
they name, and OUTPUTS, where the module's results are bound to their readers.

A class body extending a module asserts slots under the identifiers the image
itself spells them by. A name the module has no keyword for refuses at IMPORT
time, naming the nearest keyword it does have; a value of the wrong type, the
wrong length or outside the dictionary's own choices refuses there too, naming
what the dictionary allows.

A body reuses another body by COMPOSITION: ``parts = [RIVER, TRACER]`` lists the
shared bodies this one is made of, merged in the listed order, and a keyword two
parts both set refuses by name unless this body settles it itself. A body never
extends another body - what a part asserts stays visible as the part's, so a
keyword that means something else in a new setting is seen rather than inherited
into silence. Every assertion is DATA, fixed when the module is imported: a body
reads no value any fill produced.
"""

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
    "catalog_dir",
    "load_catalog",
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
    "MODULE", "CATALOG", "COMPOSITES", "OUTPUTS", "ASSERTED", "PARTS", "parts",
    "composites", "outputs", "slot",
))


def catalog_dir() -> Path:
    """Where the committed catalogs live."""
    return Path(__file__).resolve().parents[1] / "catalog"


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
    def mandatory(self) -> bool:
        """A slot that has to be answered: the dictionary gives it no default.

        A LIST keyword is never one of them. The dictionary writes no DEFAUT for
        a list because its default is EMPTY - no sources, no tracers, no control
        sections - which is an answer, and calling it a question would put thirty
        keywords nobody asked about in front of a reader.
        """
        return self.engine_default is UNSET and not self.is_list

    def check(self, value: Any) -> Any:
        """``value`` as this slot takes it, or the refusal that says why not.

        A late-bound READ passes through: a body states what it will hold, and
        the fill that substitutes the value is what the value is checked at.
        """
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

    ``expand`` is ``(value) -> (slots, files)``: the keyword assertions the value
    means, under their identifiers, and the files those keywords name, by
    basename. It lives in the wrapper because the keyword group is the module's,
    not a template's private code.
    """

    name: str
    expand: Callable[[Any], tuple[Mapping[str, Any], Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class Output:
    """One of the module's outputs, bound to the reader that publishes it."""

    name: str
    read: Callable[..., Any]


@lru_cache(maxsize=None)
def load_catalog(module: str) -> Mapping[str, Slot]:
    """The module's whole keyword surface, keyed by the identifier it is written
    under. The dictionary's own order is kept: a sheet reads down it."""
    path = catalog_dir() / f"{module}.json"
    if not path.is_file():
        raise SlotRefused(
            f"no catalog for {module!r}; the exposed modules are "
            f"{sorted(p.stem for p in catalog_dir().glob('*.json'))}.")
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

    Calling :class:`Module` builds the wrapper class for a module; every other
    class this creates is a BODY, and its namespace and its parts are checked
    against that module's catalog while the module is still being imported.
    """

    def __call__(cls, *args: str) -> type:
        if cls is not Module:
            raise SlotRefused(
                f"{cls.__name__} is a declaration, not a value; fill() makes a "
                "sheet from it.")
        (name,) = args
        catalog = load_catalog(name)
        return _Body(name.upper(), (Module,), {
            "__doc__": f"The {name} keyword surface: {len(catalog)} slots.",
            "MODULE": name, "CATALOG": catalog,
            "COMPOSITES": MappingProxyType({}), "OUTPUTS": MappingProxyType({}),
            "ASSERTED": MappingProxyType({}),
        })

    def __new__(mcls, name: str, bases: tuple, namespace: dict) -> type:
        cls = super().__new__(mcls, name, bases, dict(namespace))
        catalog = namespace.get("CATALOG") or getattr(cls, "CATALOG", None)
        if catalog is None or "CATALOG" in namespace:
            return cls
        _refuse_extended_body(cls, bases)
        cls.PARTS = _parts(cls, namespace.get("parts", ()))
        cls.ASSERTED = MappingProxyType(_asserted(cls, namespace, catalog))
        _refuse_unsettled(cls)
        return cls


def _refuse_extended_body(cls: type, bases: tuple) -> None:
    """A body extends the WRAPPER. Reuse between bodies is composition.

    Subclassing a body would put its assertions on this one's chain under this
    one's name, and per-slot provenance could then only say "inherited". A part
    keeps its own name on every row it fills.
    """
    for base in bases:
        if getattr(base, "ASSERTED", None):
            raise SlotRefused(
                f"{cls.__name__} extends {base.__name__}, which is a body. A body "
                f"extends its module's wrapper; to reuse {base.__name__}, list it: "
                f"parts = [{base.__name__}].")


def _parts(cls: type, declared: Any) -> tuple[type, ...]:
    """The shared bodies this one is made of, flattened in the listed order."""
    flat: list[type] = []
    for part in declared:
        if not (isinstance(part, type) and getattr(part, "MODULE", "") == cls.MODULE):
            raise SlotRefused(
                f"{cls.__name__} lists {part!r} as a part; a part is a body of "
                f"the same module ({cls.MODULE}).")
        for member in (*part.PARTS, part):
            if member not in flat:
                flat.append(member)
    return tuple(flat)


def _refuse_unsettled(cls: type) -> None:
    """A keyword two parts both set is settled by this body, or it refuses.

    Merging in the listed order would let the second part win silently, and the
    reader of the later template would have no way to see that the first part
    said something else about the same keyword.
    """
    seen: dict[str, str] = {}
    for part in cls.PARTS:
        for key in part.ASSERTED:
            if key in seen and key not in cls.ASSERTED:
                raise SlotRefused(
                    f"{cls.__name__} lists {seen[key]} and {part.__name__}, which "
                    f"both set {key}; settle it on {cls.__name__} or drop one part.")
            seen[key] = part.__name__


def _asserted(cls: type, namespace: Mapping[str, Any],
              catalog: Mapping[str, Slot]) -> dict[str, Any]:
    """This body's own assertions, each checked against the keyword it names."""
    composites = getattr(cls, "COMPOSITES", {})
    asserted: dict[str, Any] = {}
    for key, value in namespace.items():
        if key.startswith("_") or key in _RESERVED or _is_method(value):
            continue
        if key in composites:
            asserted[key] = value
            continue
        slot = catalog.get(key)
        if slot is None:
            raise SlotRefused(
                f"{cls.__name__} asserts {key!r}, which {cls.MODULE} has no "
                f"keyword for.{_nearest(key, catalog, composites)}")
        # None is not a value any keyword takes, so the one thing it can mean is
        # that this body states nothing here and the dictionary's default stands.
        asserted[key] = None if value is None else slot.check(value)
    return asserted


def _is_method(value: Any) -> bool:
    """Is this namespace entry the body's own code rather than a slot's value?

    A keyword's value is data. A body that carries a coupled-body constructor or
    a shared helper is carrying code, and no dictionary spells a keyword as one.
    """
    return isinstance(value, (classmethod, staticmethod, FunctionType))


def _nearest(key: str, catalog: Mapping[str, Slot],
             composites: Mapping[str, Any]) -> str:
    """The keyword the misspelling was probably reaching for."""
    close = difflib.get_close_matches(key, list(catalog) + list(composites), n=3)
    return f" Did you mean {', '.join(close)}?" if close else ""


class Module(metaclass=_Body):
    """A module's catalog, its composites and its outputs. It asserts nothing.

    ``Module("telemac2d")`` is the wrapper class; a template body extends it.
    There is deliberately no hook here for a default: the engine's default is the
    wrapper's whole position, and every opinion above it lives in a template.
    """

    #: The engine module this wraps, e.g. ``telemac2d``.
    MODULE: str = ""
    #: Every keyword the module has, by identifier.
    CATALOG: Mapping[str, Slot] = MappingProxyType({})
    COMPOSITES: Mapping[str, Composite] = MappingProxyType({})
    OUTPUTS: Mapping[str, Output] = MappingProxyType({})
    #: The shared bodies this one is made of, in the order they merge.
    PARTS: tuple[type, ...] = ()
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
        """Register the module's outputs: name -> the reader that publishes it."""
        cls.OUTPUTS = MappingProxyType({
            **cls.OUTPUTS,
            **{name: Output(name=name, read=fn)
               for name, fn in _unshadowed(cls, readers)}})

    @classmethod
    def slot(cls, identifier: str) -> Slot:
        """One slot by identifier, or the refusal that names the nearest."""
        found = cls.CATALOG.get(identifier)
        if found is None:
            raise SlotRefused(
                f"{cls.MODULE} has no keyword {identifier!r}."
                f"{_nearest(identifier, cls.CATALOG, cls.COMPOSITES)}")
        return found


def _unshadowed(cls: type, registered: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """Registrations whose names are the wrapper's own to give."""
    for name in registered:
        if name in cls.CATALOG:
            raise SlotRefused(
                f"{cls.MODULE} already has the keyword {name!r}; a registration "
                "may not shadow a keyword.")
    return list(registered.items())
