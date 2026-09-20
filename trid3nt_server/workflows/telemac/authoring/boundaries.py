"""The LIQUID BOUNDARIES FILE: the value at an open edge, instant by instant.

ONE writer for both hosts: ``read_fic_frliq`` sits in TELEMAC-2D and
TELEMAC-3D's own boundary routines call it, so the table is the same table. The
engine looks for ONE COLUMN PER BOUNDARY by the name it builds from that
boundary's number, and reads the steering file's constant list for every
boundary whose column is absent - so a run states a measured series where it
has one and a number where it does not, in the same deck.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from trid3nt_server.workflows.runtime.temporal import Series

from ..errors import TelemacError

__all__ = ["LIQUID_BOUNDARIES_FILENAME", "TRACER", "column_name",
           "liquid_boundaries_file"]

#: The file a host's LIQUID BOUNDARIES FILE statement names.
LIQUID_BOUNDARIES_FILENAME = "river_boundaries.txt"

#: The reader's own name for the time column, which it refuses to start without.
_TIME = "T"

#: What the engine calls the column it looks for, by what the boundary
#: prescribes: ``q.f`` builds ``Q(I)`` and ``sl.f`` builds ``SL(I)`` from the
#: LIQUID BOUNDARY NUMBER, which is the walk order the mesh topology numbers,
#: and ``tr.f`` builds ``TR(I,ITRAC)`` from that number AND the tracer's
#: position. The scan compares nine characters exactly, so no other spelling is
#: found. A tracer's unit is the record's own and rides on the series, so this
#: table states none for it.
_MNEMONIC: Mapping[str, tuple[str, str]] = {
    "flowrate": ("Q", "m3/s"),
    "elevation": ("SL", "m"),
    "tracer": ("TR", ""),
}

#: What a TRACER column is addressed by, beside its boundary.
TRACER = "tracer"

#: The longest column name the reader holds (``CHARACTER(LEN=9)``).
_NAME_CHARS = 9


def column_name(prescribes: str, number: int, tracer: int | None = None) -> str:
    """The column THIS boundary's value is read out of, or a refusal by name.

    A tracer is read per boundary AND per tracer, so its column names both and
    the engine falls back to the steering list's constant wherever it is
    absent - which is how one deck states a measured inflow and a number
    everywhere else."""
    if str(prescribes) not in _MNEMONIC:
        raise TelemacError(
            f"liquid boundary {number} prescribes {prescribes!r}, and the "
            f"engine builds a column name only for {sorted(_MNEMONIC)}; a "
            "series at a boundary the engine reads nothing at would be a table "
            "nobody opens.",
            error_code="TELEMAC_BOUNDARY_SERIES_UNREAD")
    inside = (str(int(number)) if tracer is None
              else f"{int(number)},{int(tracer)}")
    name = f"{_MNEMONIC[str(prescribes)][0]}({inside})"
    if len(name) > _NAME_CHARS:
        raise TelemacError(
            f"the engine holds a column name in {_NAME_CHARS} characters and "
            f"{name!r} is longer, so the scan would never match it.",
            error_code="TELEMAC_BOUNDARY_SERIES_UNREAD")
    return name


def liquid_boundaries_file(columns: Sequence[tuple[str, str, Series]], *,
                           start_s: float, until_s: float,
                           tail_s: float, note: str = "") -> str:
    """The columns a run measured -> the table the engine scans.

    A comment, the header the scan splits on, a units line the reader skips
    unconditionally, then one row per instant. Every column shares ONE clock,
    which is the run's: the reader takes one time column and reads every value
    of a row at it."""
    if not columns:
        raise TelemacError(
            "a liquid boundaries file with no column states nothing the engine "
            "reads; omit it and let the steering file's constant lists stand.",
            error_code="TELEMAC_BOUNDARY_SERIES_EMPTY")
    instants = _instants(columns, start_s, until_s, tail_s)
    # TABS ARE NOT WRITTEN. The header scan splits the line itself, and its own
    # error path names a tab as what it cannot split.
    rows = [f"#{note}" if note else "#the values this run was driven by",
            " ".join([_TIME] + [name for name, _unit, _series in columns]),
            " ".join(["s"] + [unit for _name, unit, _series in columns])]
    for instant in instants:
        rows.append(" ".join(
            [f"{instant:.3f}"]
            + [f"{found.at(instant):.9g}" for _n, _u, found in columns]))
    return "\n".join(rows) + "\n"


def _instants(columns: Sequence[tuple[str, str, Series]], start_s: float,
              until_s: float, tail_s: float) -> list[float]:
    """The one clock every column is read on: every instant any of them
    measured inside the run, and a row past the end.

    The engine STOPS on a time outside the table at either end, so the table
    opens at or before the run does and closes past where it ends."""
    end = float(until_s) + float(tail_s)
    inside = sorted({float(t) for _n, _u, found in columns
                     for t in found.times_s
                     if float(start_s) < t < end})
    return [float(start_s), *inside, end]
