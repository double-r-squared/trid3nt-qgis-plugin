"""The TELEMAC wrapper over the engine-neutral module surface.

Where the dictionary lives, the engine's check over it, the printouts table, and the six attributes only these bodies carry.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from ..authoring.staged_check import BOX_CORES
from trid3nt_server.workflows.runtime.module import Module, Slot, SlotRefused, _spelled

__all__ = [
    "ORIGIN_OF_TIME",
    "PROCESSORS",
    "TelemacModule",
    "USER_FORTRAN_DIR",
    "Unwritten",
    "module_input_dir",
]


def module_input_dir() -> Path:
    """Where the committed module inputs live."""
    return Path(__file__).resolve().parent / "module_input"


@dataclass(frozen=True, slots=True)
class Unwritten:
    """A slot the engine's own source marks as never written: its result-file name in English (and French where it differs), and the source line making the mark.

    One entry governs both directions: never asked of the engine, never published off the file.
    """

    spelling: str
    cited: str
    french: str = ""


# The directory the engine compiles for patched Fortran; the keyword names a directory, so manifest and steering statement share the word.
USER_FORTRAN_DIR = "user_fortran"
# The hydrodynamic modules' spelling: the date and the UTC hour, each a triple.
ORIGIN_OF_TIME: Mapping[str, Callable[[Any], Any]] = MappingProxyType({
    "ORIGINAL_DATE_OF_TIME": lambda moment: [
        moment["at"].year, moment["at"].month, moment["at"].day],
    "ORIGINAL_HOUR_OF_TIME": lambda moment: [
        moment["at"].hour, moment["at"].minute, moment["at"].second]})

# The keyword every TELEMAC module spells for the processor count its domain is partitioned across; its default is one machine with no parallel library, so a serial run states nothing.
PROCESSORS = "PARALLEL_PROCESSORS"


class TelemacModule(Module):
    """The TELEMAC wrapper: its dictionary, engine check, printouts table and the six attrs only these bodies carry."""

    PRINTOUTS: str = ""
    TRACER: str = ""
    UNWRITTEN: Mapping[str, Unwritten] = MappingProxyType({})
    ONLY_3D: frozenset[str] = frozenset()
    CODE_ON_HOST: Mapping[Any, Mapping[str, Callable[[], str]]] = MappingProxyType({})
    ARMS_ON_HOST: tuple[str, ...] = ()
    PARTITION_KEYWORD: str = PROCESSORS

    @classmethod
    def dictionary_dir(cls) -> Path:
        return module_input_dir()

    @classmethod
    def engine_check(cls, module: str) -> Any:
        return engine_check(module)

    @classmethod
    def wrapper_for(cls, module: str) -> type:
        from . import wrapper_for

        return wrapper_for(module)

    @classmethod
    def fits_box(cls, identifier: str, value: Any) -> None:
        if identifier == cls.PARTITION_KEYWORD:
            fits_the_box(value)

    @classmethod
    def sheet(cls, filled: dict[str, Any], files: dict[str, Any],
              run: Mapping[str, Any]) -> Any:
        from ..authoring.atmosphere import write_atmosphere
        from .sheet import (TelemacSheet, _arm, _continued, _coupled_filled,
                            _partitioned)

        for basename, content in list(files.items()):
            if isinstance(content, Mapping) and content.get("filled_by"):
                files[basename] = _coupled_filled(content, run)
        _arm(cls, filled, files)
        _continued(cls, filled, run)
        _partitioned(cls, filled)
        write_atmosphere(cls, {name: row.value for name, row in filled.items()},
                         files)
        return TelemacSheet(body=cls, filled=MappingProxyType(filled),
                            files=MappingProxyType(files), run=MappingProxyType(run))

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


def fits_the_box(count: Any) -> None:
    """Refuse a processor count past this box's cores; a count cut down to fit is a partition the caller was never told about."""
    if int(count) > BOX_CORES:
        raise SlotRefused(
            f"PARALLEL PROCESSORS {int(count)} is past the {BOX_CORES} cores this "
            f"box has. State {BOX_CORES} or fewer - a count this box cannot seat "
            "is refused rather than cut down to fit.")


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
