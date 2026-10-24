"""One typed check over the staged run directory, before it leaves the daemon.

A disagreement between the name files and the files beside them otherwise surfaces as a
solve that stops inside mf6. Every clause reads the staged artifacts and refuses by name.
mf6's own validate ran first and caught the grammar; this reads the run's facts - what
the names point at, what the bytes are, which cells the lists reach, and the clock.
"""

from __future__ import annotations

import hashlib
import logging
import math
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import ModflowError

logger = logging.getLogger("trid3nt_server.workflows.modflow.authoring.staged_check")

__all__ = ["check_staged_run"]

# The units a perlen converts by. mf6's own default is "unknown" - a label with no
# second count - and its years run on its own calendar; an absent or unpresentable unit
# has nothing to measure a window against, and the clock clause stands down.
_UNIT_SECONDS = {"seconds": 1.0, "second": 1.0, "s": 1.0,
                 "minutes": 60.0, "minute": 60.0, "min": 60.0,
                 "hours": 3600.0, "hour": 3600.0, "hr": 3600.0, "h": 3600.0,
                 "days": 86400.0, "day": 86400.0, "d": 86400.0}


def _refuse(code: str, message: str) -> None:
    raise ModflowError(message, error_code=f"MODFLOW_STAGED_{code}")


def _records(path: Path) -> list[str]:
    """One name file's referenced filenames - the second token of every record line.

    Timing, models, solution groups and packages are all ``FTYPE FNAME [PNAME]``.
    """
    found: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        tokens = line.split()
        if not tokens or tokens[0] in ("BEGIN", "END") or tokens[0].startswith("#"):
            continue
        if len(tokens) >= 2:
            found.append(tokens[1])
    return found


def _blocks(path: Path) -> list[tuple[str, list[str]]]:
    """Every ``BEGIN <name> ... END`` body of one file -> ``(name, data lines)``."""
    blocks: list[tuple[str, list[str]]] = []
    current: str | None = None
    body: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        tokens = line.split()
        if tokens and tokens[0] == "BEGIN":
            if current is not None:
                blocks.append((current, body))
            current = " ".join(tokens[1:]).lower()
            body = []
        elif tokens and tokens[0] == "END":
            if current is not None:
                blocks.append((current, body))
            current, body = None, []
        elif current is not None and line.strip() and not line.lstrip().startswith("#"):
            body.append(line)
    if current is not None:
        blocks.append((current, body))
    return blocks


def _staged_here(rundir: Path, inputs: Sequence[Mapping[str, str]]
                 ) -> dict[str, Path | str]:
    """Every name the box will find in the run directory -> where it is now (authored: this disk; mesh input: the object the launcher stages)."""
    here: dict[str, Path | str] = {
        str(p.relative_to(rundir)): p for p in rundir.rglob("*")}
    for row in inputs:
        dest = str(row.get("dest") or "")
        if dest and dest not in here:
            here[dest] = str(row.get("gs_uri") or "")
    return here


def _read(found: Path | str) -> Path:
    if isinstance(found, Path):
        return found
    from trid3nt_server.tools.cache import read_object_bytes_s3

    handle = tempfile.NamedTemporaryFile(suffix=Path(found).suffix, delete=False)
    handle.write(read_object_bytes_s3(found))
    handle.close()
    return Path(handle.name)


def _name_files(rundir: Path, steering: str) -> list[Path]:
    """The steering name file and every model name file it references, read in place."""
    first = rundir / steering
    if not first.is_file():
        _refuse("FILE_MISSING",
                f"the run is dispatched against {steering!r} and the run directory "
                f"holds {sorted(p.name for p in rundir.iterdir())}; nothing would be read.")
    paths, seen, pending = [first], {steering}, [first]
    for path in pending:
        for ref in _records(path):
            if ref.endswith(".nam") and ref not in seen:
                seen.add(ref)
                candidate = rundir / ref
                if candidate.is_file():
                    paths.append(candidate)
                    pending.append(candidate)
    return paths


def _files_referenced_exist(paths: Sequence[Path], staged: Mapping[str, Any],
                            written_by_the_engine: Sequence[str]) -> set[str]:
    """Every name the name files reference -> those names; a gap otherwise stops mf6's first read."""
    referenced: set[str] = set()
    for path in paths:
        for name in _records(path):
            referenced.add(name)
            if name in written_by_the_engine or name in staged:
                continue
            near = [have for have in staged if have.lower() == name.lower()]
            _refuse("FILE_MISSING",
                    f"{path.name} references {name!r} and the staged run directory "
                    f"holds no such file"
                    + (f" - it holds {near[0]!r}, the same name in another spelling"
                       if near else f" (it holds {sorted(staged)})")
                    + "; the solve would stop reading a file nobody staged.")
    return referenced


def _read_bytes(uri: str) -> bytes:
    """One input row's source bytes, resolved by scheme the way the launcher stages them."""
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        return read_object_bytes_s3(uri)
    source = Path(uri[len("file://"):] if uri.startswith("file://") else uri)
    return source.read_bytes()


def _inputs_match_the_staged(rundir: Path,
                             inputs: Sequence[Mapping[str, str]]) -> dict[str, str]:
    """Each input row's dest -> its source; a staged file the source does not hold refuses.

    The rows are the run's provenance: the DISV here is the artifact's own bytes, a
    raster the fill sampled from is the raster, and a disagreement would solve against
    different data than the one the cards showed. A row the launcher stages later has
    no local copy yet - there is nothing to disagree with until the download.
    """
    checked: dict[str, str] = {}
    resolved = rundir.resolve()
    for row in inputs:
        dest = str(row.get("dest") or "")
        uri = str(row.get("gs_uri") or "")
        if not dest or not uri or dest in checked:
            continue
        checked[dest] = uri
        local = rundir / dest
        try:
            local.resolve().relative_to(resolved)
        except ValueError:
            _refuse("DIGEST_MISMATCH",
                    f"an input stages {dest!r} outside the run directory; a dest "
                    "climbs out of the directory the launcher writes.")
        if not local.is_file():
            continue
        source = _read_bytes(uri)
        if hashlib.sha256(local.read_bytes()).digest() != hashlib.sha256(source).digest():
            _refuse("DIGEST_MISMATCH",
                    f"{dest} on disk ({local.stat().st_size} bytes) is not the bytes "
                    f"the run was handed from {uri} ({len(source)} bytes); the solve "
                    "would read a file the cards never showed.")
    return checked


#: The packages whose period rows lead with a cell, by the suffix flopy names their file with.
_CELL_LED = frozenset({".chd", ".wel", ".drn", ".riv", ".ghb", ".rch", ".evt", ".csub"})

#: The grid packages a model name file lists, by the kind of cell id they give a period row.
_GRIDS = {"DISV6": "disv", "DISU6": "disu"}


def _model_grids(paths: Sequence[Path], staged: Mapping[str, Any]) -> list[tuple[str, str, Path | str, list[str]]]:
    """Each model name file -> ``(model name, grid kind, where its grid file is now, its package files)``; a model with no DISV or DISU is left to its parser."""
    found = []
    for path in paths:
        records = [(tokens[0].upper(), tokens[1])
                   for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                   if len(tokens := line.split()) >= 2 and tokens[0] not in ("BEGIN", "END")
                   and tokens[0].upper().endswith("6") and not tokens[0].startswith("#")]
        grid = next(((_GRIDS[ftype], name) for ftype, name in records if ftype in _GRIDS), None)
        if grid is None or staged.get(grid[1]) is None:
            continue
        found.append((path.stem, grid[0], staged[grid[1]],
                      [name for ftype, name in records if ftype not in _GRIDS]))
    return found


def _grid_cells(kind: str, grid: Path) -> tuple[set[int], int]:
    """The ids a period row may name in one grid file -> ``(the cell ids, the layer count)``; a DISU's ids are its node numbers and it has no layers."""
    nlay = 0
    nodes = 0
    cells: set[int] = set()
    for name, body in _blocks(grid):
        if name == "dimensions":
            for line in body:
                tokens = line.split()
                if len(tokens) >= 2 and tokens[0].upper() == "NLAY":
                    nlay = int(tokens[1])
                if len(tokens) >= 2 and tokens[0].upper() == "NODES":
                    nodes = int(tokens[1])
        if name != "cell2d" or kind != "disv":
            continue
        for line in body:
            first = line.split()[:1]
            if first:
                try:
                    cells.add(int(first[0]))
                except ValueError:
                    continue
    if kind == "disu":
        if not nodes:
            _refuse("GRID_UNREADABLE", f"the staged DISU {grid.name} states no NODES, so no node number in this "
                    "run has a grid to be inside of.")
        return set(range(1, nodes + 1)), 0
    if not cells:
        _refuse("GRID_UNREADABLE",
                "the staged DISV carries no cell2d table, so no cell id in this "
                "run has a grid to be inside of.")
    return cells, nlay


def _cell_ids_inside_the_grid(grids: Sequence[tuple[str, str, Path | str, list[str]]], local: Path) -> int:
    """Every cell a period list names, against its own model's grid.

    On a DISV a cell is (layer, cell2d id), both from 1; on a DISU it is the node number alone. Only the packages
    whose period rows lead with a cell are read (_CELL_LED, by flopy's file suffix): SFR, LAK, MAW, UZF
    and MVR rows lead with their own feature numbers. A row whose leading tokens are
    not integers is a record keyword and this clause leaves it to the parser.
    """
    checked = 0
    for model, kind, found, files in grids:
        cells, nlay = _grid_cells(kind, _read(found))
        for name in files:
            path = local / name
            if not path.is_file() or path.suffix.lower() not in _CELL_LED:
                continue
            for block, body in _blocks(path):
                if not block.startswith("period "):
                    continue
                for line in body:
                    try:
                        ids = [int(token) for token in line.split()[:1 if kind == "disu" else 2]]
                    except ValueError:
                        continue
                    if len(ids) != (1 if kind == "disu" else 2):
                        continue
                    layer, cellid = (0, ids[0]) if kind == "disu" else ids
                    checked += 1
                    if cellid not in cells:
                        _refuse(
                            "CELLID_OUTSIDE",
                            f"{path.name} names {'node' if kind == 'disu' else 'cell id'} {cellid} and the staged "
                            f"{'DISU' if kind == 'disu' else 'DISV'} of {model} numbers {len(cells)} cell(s) "
                            f"({'nodes' if kind == 'disu' else 'cell2d'} {min(cells)} to {max(cells)}); the solve "
                            "would write a value against no cell.")
                    if nlay and not 1 <= layer <= nlay:
                        _refuse(
                            "CELLID_OUTSIDE",
                            f"{path.name} names layer {layer} and the staged DISV of {model} "
                            f"has {nlay} layer(s); the solve would write a value "
                            "against no cell.")
    if checked:
        logger.info("modflow staged check read %d cell id(s) against %d model grid(s)", checked, len(grids))
    return checked


def _arrays_are_finite(local: Path, extra: Sequence[Path]) -> int:
    """Every number inside a GRIDDATA block, against the grid the solve would read it on.

    An unmeasured raster or a holed bed reaches here only if an earlier refusal missed;
    a NaN written into a file is a run that solves garbage silently, never an error.
    """
    checked = 0
    paths = [p for p in sorted(local.rglob("*"))
             if p.is_file() and p.suffix != ".lst"] + list(extra)
    for path in paths:
        for name, body in _blocks(path):
            if name != "griddata":
                continue
            for line in body:
                for token in line.split():
                    try:
                        value = float(token)
                    except ValueError:
                        continue
                    checked += 1
                    if not (abs(value) != float("inf") and value == value):
                        _refuse("ARRAY_NOT_FINITE",
                                f"{path.name} carries {token!r} in its grid data, and "
                                "a cell the solve reads a non-finite value into stops "
                                "or, worse, does not stop.")
    return checked


def _tdis_covers_the_run(tdis: Path | None, duration_s: float | None) -> float | None:
    """The TDIS clock summed against the run's window -> the seconds it runs, or None.

    mf6 ends a simulation exactly where the periods end: sum(perlen) times the stated
    unit, a step finer than that never shortens it. With no time unit stated there is
    nothing to convert with, and mf6 itself reads perlen as an unlabeled number.
    """
    if duration_s is None:
        return None
    if tdis is None:
        _refuse("CLOCK_INCONSISTENT",
                f"the run is {float(duration_s):.6g} s long and the staged files "
                "hold no TDIS file for it to advance with.")
    unit = None
    perlen: list[float] = []
    for name, body in _blocks(tdis):
        if name == "options":
            for line in body:
                tokens = line.split()
                if len(tokens) >= 2 and tokens[0].upper() == "TIME_UNITS":
                    unit = tokens[1].lower()
        elif name == "perioddata":
            for line in body:
                tokens = line.split()
                if tokens:
                    try:
                        perlen.append(float(tokens[0]))
                    except ValueError:
                        _refuse("CLOCK_INCONSISTENT",
                                f"{tdis.name} states a period row {tokens!r} this run "
                                "cannot add up.")
    factor = _UNIT_SECONDS.get(unit or "unknown")
    if factor is None:
        return None
    total = sum(perlen) * factor
    if perlen and not math.isclose(total, float(duration_s), rel_tol=1e-9,
                                    abs_tol=1e-6):
        _refuse("CLOCK_INCONSISTENT",
                f"{tdis.name} advances {total:.6g} s over {len(perlen)} period(s) "
                f"(time unit {unit!r}) and the fill states a {float(duration_s):.6g}"
                " s window; mf6 ends where its periods end and the run would end at "
                "an instant nobody asked for.")
    return total


def check_staged_run(rundir: Path | str, *, steering: str,
                     inputs: Sequence[Mapping[str, str]],
                     written_by_the_engine: Sequence[str],
                     duration_s: float | None, cores: int) -> dict[str, Any]:
    """The staged run directory, checked clause by clause -> what each one read.

    A run that would die in the solve's first second refuses here by name. mf6 is
    sequential, so ``cores`` is accepted without a clause: nothing in the files places a
    partition, and the launcher seats what it seats.
    """
    rundir = Path(rundir)
    staged = _staged_here(rundir, inputs)
    paths = _name_files(rundir, steering)
    referenced = _files_referenced_exist(paths, staged, written_by_the_engine)
    digested = _inputs_match_the_staged(rundir, inputs)
    grids = _model_grids([p for p in paths if p.name != steering], staged)
    cell_ids = _cell_ids_inside_the_grid(grids, rundir)
    numbers = _arrays_are_finite(
        rundir, [_read(found) for _model, _kind, found, _files in grids if not isinstance(found, Path)])
    tdis_name = next((name for name in referenced if name.endswith(".tdis")), None)
    found_tdis = staged.get(tdis_name or "")
    total = _tdis_covers_the_run(
        _read(found_tdis) if found_tdis is not None else None, duration_s)
    read = {"name_files": sorted(p.name for p in paths),
            "referenced": sorted(referenced), "staged": len(staged),
            "digested": sorted(digested), "cell_ids": cell_ids,
            "griddata_numbers": numbers, "tdis_total_s": total,
            "cores": int(cores)}
    logger.info("modflow staged check ok: %s", read)
    return read
