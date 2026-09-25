"""One typed check over the staged run directory, before it leaves the daemon.

The launcher stages the authored files and the mesh pair into one directory and
the image solves whatever is in it, so a disagreement between the steering file
and the files beside it surfaces as a solve that stops inside Fortran. Every
clause below reads the STAGED artifacts rather than what the daemon believes it
wrote, and each refuses by name.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from trid3nt_server.workflows.runtime.levers import BOX_CORES

from ..errors import TelemacError

logger = logging.getLogger("trid3nt_server.workflows.telemac.authoring.staged_check")

__all__ = ["check_staged_run", "read_steering"]

#: The keyword a partitioned run states its core count under.
_PROCESSORS = "PARALLEL PROCESSORS"
_TIME_STEP = "TIME STEP"
_STEPS = "NUMBER OF TIME STEPS"
_DURATION = "DURATION"
_GEOMETRY = "GEOMETRY FILE"
_BOUNDARY = "BOUNDARY CONDITIONS FILE"
_LIQUID = "LIQUID BOUNDARIES FILE"
#: What a DAMOCLES line separates a keyword from its value with, and what opens
#: a comment line.
_ASSIGN = re.compile(r"^\s*([A-Z0-9][^=:]*?)\s*[=:]\s*(.*)$")
#: The prescribed lists, one entry per liquid boundary in the engine's own
#: numbering, however many faces that boundary spans.
_PRESCRIBED = ("PRESCRIBED FLOWRATES", "PRESCRIBED ELEVATIONS")
#: A solid wall states this code; every other LIHBOR opens the face.
_WALL = 2


def read_steering(path: Path) -> dict[str, list[str]]:
    """One steering file -> ``{keyword: [value, ...]}`` in its own spelling.

    The values stay strings: this reads what was WRITTEN, and a value's type is
    the dictionary's business, checked where the engine's own parser reads it."""
    values: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("/"):
            continue
        found = _ASSIGN.match(line)
        if found is None:
            continue
        values[found.group(1).strip()] = [
            item.strip().strip("'").strip() for item in found.group(2).split(";")]
    return values


def _refuse(code: str, message: str) -> None:
    raise TelemacError(message, error_code=f"TELEMAC_STAGED_{code}")


def _number(values: Mapping[str, list[str]], keyword: str) -> float | None:
    stated = values.get(keyword)
    try:
        return float(stated[0]) if stated else None
    except (TypeError, ValueError):
        return None


def _named_files(values: Mapping[str, list[str]]) -> list[tuple[str, str]]:
    """``(keyword, name)`` for every file a deck names, in the deck's spelling."""
    return [(keyword, stated[0])
            for keyword, stated in values.items()
            if keyword.split()[-1:] == ["FILE"] and stated and stated[0]]


def _staged_here(rundir: Path, inputs: Sequence[Mapping[str, str]]
                 ) -> dict[str, Path | str]:
    """Every name the box will find in the run directory -> where it is now.

    An authored file is on this disk; a mesh input is still the object the
    launcher will stage under that name."""
    # A DIRECTORY is a staged name too: the engine compiles the directory its
    # FORTRAN FILE statement names, so the deck states the directory and the
    # manifest carries the files inside it.
    here: dict[str, Path | str] = {
        str(p.relative_to(rundir)): p for p in rundir.rglob("*")}
    for row in inputs:
        dest = str(row.get("dest") or "")
        if dest and dest not in here:
            here[dest] = str(row.get("gs_uri") or "")
    return here


def _read(found: Path | str) -> Path:
    """A staged name resolved to a readable path, fetching the object if needed."""
    if isinstance(found, Path):
        return found
    from trid3nt_server.tools.cache import read_object_bytes_s3

    handle = tempfile.NamedTemporaryFile(suffix=Path(found).suffix, delete=False)
    handle.write(read_object_bytes_s3(found))
    handle.close()
    return Path(handle.name)


def _files_are_present(decks: Mapping[str, dict[str, list[str]]],
                       staged: Mapping[str, Path | str],
                       written_by_the_engine: Sequence[str]) -> None:
    for deck, values in sorted(decks.items()):
        for keyword, name in _named_files(values):
            if name in written_by_the_engine or name in staged:
                continue
            near = [have for have in staged if have.lower() == name.lower()]
            _refuse("FILE_MISSING",
                    f"{deck} states {keyword} = {name!r} and the staged run "
                    f"directory holds no such file"
                    + (f" - it holds {near[0]!r}, which is the same name in "
                       "another spelling" if near else
                       f" (it holds {sorted(staged)})")
                    + "; the solve would stop reading a file nobody staged.")


def _boundary_matches_the_walk(mesh: Mapping[str, Any], geometry: str,
                               boundary: Path) -> tuple[list[int], int]:
    """The ``.cli`` rows -> ``(LIHBOR per row, liquid boundaries numbered)``.

    Or the refusal by name, when the file and the walk disagree."""
    import numpy as np

    from trid3nt_server.mesh.shared.formats.tin_topology import (
        boundary_numbering)

    from .selafin_io import _liquid_boundaries

    ipobo, loops = boundary_numbering(np.asarray(mesh["ikle2"], dtype="int64"),
                                      int(mesh["npoin2"]))
    walk = [int(n) + 1 for n in
            np.where(ipobo > 0)[0][np.argsort(ipobo[ipobo > 0])]]
    rows = [line.split() for line in
            boundary.read_text(encoding="utf-8").splitlines() if line.split()]
    if len(rows) != len(walk):
        _refuse("BOUNDARY_MISMATCH",
                f"{boundary.name} holds {len(rows)} row(s) and the walk over "
                f"{geometry}'s own connectivity numbers {len(walk)} boundary "
                "node(s); the two files describe different boundaries and the "
                "engine would classify the wrong nodes.")
    numbered = [int(row[-2]) for row in rows]
    if numbered != walk:
        first = next(k for k, (a, b) in enumerate(zip(numbered, walk)) if a != b)
        _refuse("BOUNDARY_MISMATCH",
                f"{boundary.name} row {first + 1} names node {numbered[first]} "
                f"and the walk over {geometry} numbers node {walk[first]} at "
                "that rank; the boundary file is ordered by a numbering the "
                "geometry does not agree with.")
    # The engine prescribes one value per LIQUID BOUNDARY - a run of open faces
    # between walls, split where the codes change - never one per face, so the
    # rows are numbered by the engine's own walk rather than counted.
    quads = [(int(row[0]), int(row[1]), int(row[2]), int(row[7])) for row in rows]
    try:
        runs = _liquid_boundaries(
            mesh["x"], mesh["y"], [n - 1 for n in walk], quads,
            [len(loop) for loop in loops])
    except ValueError as lone:
        _refuse("BOUNDARY_MISMATCH",
                f"{boundary.name} codes a boundary the engine cannot number: "
                f"{lone}.")
    return [quad[0] for quad in quads], len(runs)


def _bed_is_whole(mesh: Mapping[str, Any], geometry: str) -> None:
    import numpy as np

    bed = mesh["data"].get("BOTTOM")
    if bed is None:
        _refuse("BED_UNPAINTED",
                f"{geometry} publishes {mesh['varnames']} and no BOTTOM, so the "
                "mesh the run solves on carries no bed at all.")
    unpainted = int(np.count_nonzero(~np.isfinite(np.asarray(bed[0]))))
    if unpainted:
        _refuse("BED_UNPAINTED",
                f"{geometry} leaves {unpainted} of {mesh['npoin2']} node(s) with "
                "no bed value; a node nothing measured is refused here rather "
                "than solved through.")


def _boundaries_carry_a_value(values: Mapping[str, list[str]], codes: Sequence[int],
                              liquid: int, staged: Mapping[str, Path | str],
                              duration_s: float | None) -> None:
    if not liquid:
        return
    faces = sum(1 for code in codes if int(code) != _WALL)
    for keyword in _PRESCRIBED:
        stated = values.get(keyword)
        if stated is None:
            continue
        numbers = [item for item in stated if _finite(item)]
        if len(numbers) < liquid:
            _refuse("BOUNDARY_UNPRESCRIBED",
                    f"the boundary file numbers {liquid} liquid boundary(ies) "
                    f"over {faces} open face(s) and {keyword} states "
                    f"{len(numbers)} value(s) ({stated}); the liquid boundary "
                    "the list runs out at is prescribed nothing the engine can "
                    "read, and the solve stops at its first step.")
    named = values.get(_LIQUID)
    if named and named[0] and duration_s is not None:
        _series_covers_the_window(_read(staged[named[0]]), named[0], duration_s)


def _finite(item: str) -> bool:
    try:
        value = float(item)
    except (TypeError, ValueError):
        return False
    return value == value and abs(value) != float("inf")


def _series_covers_the_window(table: Path, name: str, duration_s: float) -> None:
    rows = [line.split() for line in
            table.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]
    instants = [float(row[0]) for row in rows[2:] if row and _finite(row[0])]
    if not instants:
        _refuse("SERIES_SHORT",
                f"{name} carries no instant at all, so every column the engine "
                "scans for is empty and the run stops at its first read.")
    covered = instants[-1] - instants[0]
    if covered + 1e-6 < float(duration_s):
        _refuse("SERIES_SHORT",
                f"{name} runs {covered:.6g} s from {instants[0]:.6g} s and the "
                f"run is {float(duration_s):.6g} s long; the engine stops on an "
                "instant outside the table rather than holding the last value.")


def _clock_is_consistent(values: Mapping[str, list[str]],
                         duration_s: float | None) -> None:
    # A fill that states no window - a steady wave field is solved once, not
    # advanced - has no clock for a step count to disagree with.
    if duration_s is None:
        return
    step = _number(values, _TIME_STEP)
    steps = _number(values, _STEPS)
    stated = _number(values, _DURATION)
    if step is None or step <= 0:
        _refuse("CLOCK_INCONSISTENT",
                f"the deck states {_TIME_STEP} = {values.get(_TIME_STEP)}, and a "
                "run with no positive time step advances nothing.")
    window = float(duration_s)
    if steps is not None and abs(step * steps - window) > step:
        _refuse("CLOCK_INCONSISTENT",
                f"the deck runs {_STEPS} = {steps:.6g} of {step:.6g} s, which is "
                f"{step * steps:.6g} s, and the fill states a {window:.6g} s "
                "window; the run would end at an instant nobody asked for.")
    if steps is None and stated is not None and abs(stated - window) > step:
        _refuse("CLOCK_INCONSISTENT",
                f"the deck states {_DURATION} = {stated:.6g} s and the fill "
                f"states a {window:.6g} s window; the two clocks disagree.")


def _processors_fit_the_box(values: Mapping[str, list[str]], cores: int) -> None:
    asked = _number(values, _PROCESSORS)
    for count, where in ((asked, f"the deck's {_PROCESSORS}"),
                         (float(cores), "the manifest's core count")):
        if count is not None and int(count) > BOX_CORES:
            _refuse("CORES_OVER_BOX",
                    f"{where} states {int(count)} and this box has {BOX_CORES} "
                    "core(s); the launcher would partition the mesh across a "
                    "partition that cannot be seated.")


def check_staged_run(rundir: Path | str, *, steering: str,
                     inputs: Sequence[Mapping[str, str]],
                     written_by_the_engine: Sequence[str],
                     duration_s: float | None, cores: int) -> dict[str, Any]:
    """The staged run directory, checked clause by clause -> what each one read.

    A run that would die in the first second of the solve refuses here by name
    instead of reaching the image."""
    rundir = Path(rundir)
    decks = {p.name: read_steering(p) for p in sorted(rundir.rglob("*.cas"))}
    top = decks.get(steering)
    if top is None:
        _refuse("FILE_MISSING",
                f"the run is dispatched against {steering!r} and the run "
                f"directory holds {sorted(decks)}; nothing would be read.")
    staged = _staged_here(rundir, inputs)
    _files_are_present(decks, staged, written_by_the_engine)
    _clock_is_consistent(top, duration_s)
    _processors_fit_the_box(top, cores)
    geometry = (top.get(_GEOMETRY) or [""])[0]
    boundary = (top.get(_BOUNDARY) or [""])[0]
    # A deck naming one half of the pair and not the other states a numbering
    # nothing can be read against; a deck naming neither solves on no mesh.
    if not geometry or not boundary:
        _refuse("FILE_MISSING",
                f"{steering} states {_GEOMETRY} = {geometry!r} and {_BOUNDARY} "
                f"= {boundary!r}; the pair is one artifact and a run needs both.")
    from .selafin_io import read_selafin

    mesh = read_selafin(_read(staged[geometry]))
    codes, liquid = _boundary_matches_the_walk(mesh, geometry,
                                               _read(staged[boundary]))
    _bed_is_whole(mesh, geometry)
    _boundaries_carry_a_value(top, codes, liquid, staged, duration_s)
    read = {"decks": sorted(decks), "staged": len(staged),
            "boundary_rows": len(codes), "liquid_boundaries": liquid,
            "liquid_faces": sum(1 for code in codes if int(code) != _WALL)}
    logger.info("telemac staged check ok: %s", read)
    return read
