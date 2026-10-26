"""The staged .data set read back against the sheet: a mismatch refuses by name.

Each file Clawpack just wrote is parsed again - with the engine's own readers where
they ship (gauges, regions, flagregions, fgmax and fgout grids) and with this check's record
reader where Clawpack ships none (topo, dtopo) - so a keyword that did not survive
its file surfaces here, before the solve stops inside the engine. Nothing writes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import GeoclawError
from .rundata import _fields, _prefix_of, attribute_of, new_rundata

logger = logging.getLogger(
    "trid3nt_server.workflows.geoclaw.authoring.staged_check")

__all__ = ["check_staged_run"]

#: The record rows Clawpack ships no reader for: this check counts its own block.
_OWN_RECORDS: dict[str, str] = {
    "DTOPO_DTOPOFILES": "mdtopofiles",
    "TOPO_TOPOFILES": "ntopofiles",
}


def check_staged_run(rundir: Path | str, *, sheet: Any,
                     written: Sequence[str]) -> dict[str, Any]:
    """What the run directory's .data set carries, read back against the sheet -> the identifiers checked.

    A stated keyword that is absent from its file, a value that did not survive
    the write, a written file that is gone, or a named staged file nobody staged
    each refuse here by identifier - the engine never sees a half-true set.
    """
    work = Path(rundir)
    for name in written:
        if not (work / name).is_file():
            _refuse("FILE_MISSING",
                    f"the write recorded {name!r} and the run directory holds "
                    f"{_holding(work)}")
    rundata = new_rundata(sheet.body.MODULE)
    checked: list[str] = []
    for identifier, row in sorted(sheet.filled.items()):
        path = work / row.slot.rubrique[0]
        if not path.is_file():
            _refuse("FILE_MISSING",
                    f"{identifier} is written to {row.slot.rubrique[0]!r} and "
                    f"the run directory holds {_holding(work)}")
        if row.slot.type == "STRUCT" and row.value \
                and identifier not in _OWN_RECORDS and identifier not in _READERS:
            _refuse("UNREADABLE",
                    f"{identifier}: no reader parses records out of "
                    f"{path.name!r} yet, so what is staged there cannot be "
                    f"checked against {row.value!r}")
        try:
            found = _recovered(path, identifier, row, work, rundata)
        except GeoclawError:
            raise
        except Exception as exc:
            _refuse("UNREADABLE",
                    f"{identifier}: {path.name} could not be read back - "
                    f"{type(exc).__name__}: {exc}")
        if row.slot.type == "STRUCT" and found \
                and not isinstance(found[0], (list, tuple)):
            fields = _fields(row)
            found = [_record(obj, fields, stated)
                     for obj, stated in zip(found, row.value)]
        if not _same(row.value, found):
            _refuse("MISMATCH",
                    f"{identifier}: the sheet states {row.value!r} and "
                    f"{path.name} carries {found!r} - the engine's file "
                    "holds what was never asked for")
        if row.slot.type == "STRUCT":
            _staged_files(work, row)
        if identifier == "SURGE_STORM_FILE" and not (work / str(row.value)).is_file():
            _refuse("FILE_MISSING", f"SURGE_STORM_FILE names {row.value!r}, which the run directory does not hold - "
                    f"holding {_holding(work)}")
        checked.append(identifier)
    result = {"checked": checked, "files": sorted(written)}
    logger.info("geoclaw staged check passed: %i identifiers over %i files",
                len(checked), len(written))
    return result


def _refuse(code: str, message: str) -> None:
    raise GeoclawError(message, error_code=f"GEOCLAW_STAGED_{code}")


def _holding(work: Path) -> str:
    """What the run directory holds, for a refusal that must say where it looked."""
    if not work.is_dir():
        return "no run directory at all"
    return ", ".join(sorted(entry.name for entry in work.iterdir())) or "nothing"


def _recovered(path: Path, identifier: str, row: Any, work: Path,
               rundata: Any) -> Any:
    """The keyword's value as this file carries it: the shipped reader where one exists, the count block where Clawpack ships none, the base scalar read where the file is all keywords."""
    reader = _READERS.get(identifier)
    if reader is not None:
        return reader(path, work, rundata)
    if identifier in _OWN_RECORDS:
        return _quoted_records(path, _OWN_RECORDS[identifier])
    from clawpack.clawutil.data import ClawData

    prefix = identifier.partition("_")[0]
    group = next(obj for attr, obj in vars(rundata).items()
                 if hasattr(obj, "write") and _prefix_of(attr) == prefix)
    ClawData.read(group, str(path))
    return getattr(group, attribute_of(group, identifier[len(prefix) + 1:]))


def _count(text: str, name: str) -> int:
    """A file's own ``N =: count_keyword`` line - how many records it says it carries."""
    for line in text.splitlines():
        if "=:" in line and line.split("=:")[1].split()[:1] == [name]:
            return int(line.split("=:")[0].split()[0])
    raise ValueError(f"no {name!r} count line in the file")


def _quoted_records(path: Path, count_name: str) -> list[list[Any]]:
    """The block the topo and dtopo writers quote: the count line, then a name line and a type line per record."""
    text = path.read_text(encoding="utf-8")
    count = _count(text, count_name)
    records: list[list[Any]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if line.startswith("'"):
            following = index + 1
            while following < len(lines) and not lines[following].strip():
                following += 1
            kind = int(lines[following].split()[0])
            records.append([kind, line.strip("'")])
            index = following
        index += 1
    if len(records) != count:
        raise ValueError(f"the file declares {count} records and carries "
                         f"{len(records)}")
    return records


def _gauges(path: Path, work: Path, _rundata: Any) -> Any:
    from clawpack.amrclaw.data import GaugeData

    group = GaugeData()
    group.read(data_path=str(work), file_name=path.name)
    return group.gauges


def _flagregions(path: Path, work: Path, rundata: Any) -> Any:
    rundata.flagregiondata.read(str(path))
    for region in rundata.flagregiondata.flagregions:
        if region.spatial_region_type == 2:
            region.read_spatial_region(str(work / region.spatial_region_file))
    return rundata.flagregiondata.flagregions


def _fgout(path: Path, work: Path, _rundata: Any) -> Any:
    from clawpack.geoclaw.fgout_tools import FGoutGrid

    text = path.read_text(encoding="utf-8")
    count = _count(text, "num_fgout_grids")
    fgnos = [int(line.partition("#")[0])
             for line in text.splitlines()
             if line.partition("#")[2].strip().startswith("fgno")]
    if len(fgnos) != count:
        raise ValueError(f"the file declares {count} grids and carries "
                         f"{len(fgnos)}")
    grids = []
    for fgno in fgnos:
        grid = FGoutGrid(fgno=fgno, outdir=str(work))
        grid.read_fgout_grids_data(fgno, data_file=path.name)
        grids.append(grid)
    return grids


def _fgmax(path: Path, work: Path, _rundata: Any) -> Any:
    from clawpack.geoclaw.fgmax_tools import FGmaxGrid

    text = path.read_text(encoding="utf-8")
    count = _count(text, "num_fgmax_grids")
    fgnos = [int(line.partition("#")[0])
             for line in text.splitlines()
             if line.partition("#")[2].strip().startswith("fgno")]
    if len(fgnos) != count:
        raise ValueError(f"the file declares {count} grids and carries "
                         f"{len(fgnos)}")
    grids = []
    for fgno in fgnos:
        grid = FGmaxGrid()
        grid.read_fgmax_grids_data(fgno, data_file=str(path))
        grids.append(grid)
    return grids


def _qinitfiles(path: Path, _work: Path, _rundata: Any) -> Any:
    """The quoted file line qinit.data carries, one record per file."""
    return [[line.strip().strip("'")] for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("'")]


def _record(obj: Any, fields: Sequence[str], stated: Any) -> list[Any]:
    """A grid object as the record the sheet states: ``points`` is the geometry the record named, read off the attributes it named."""
    return [{name: getattr(obj, name) for name in item}
            if field == "points" and isinstance(item, Mapping)
            else _ruled(obj) if field == "spatial_region" and isinstance(item, Mapping)
            else getattr(obj, field)
            for field, item in zip(fields, stated)]


def _ruled(region: Any) -> dict[str, Any]:
    """A flagregion's ruled rectangle as the record states it, read back off the file the region names."""
    rule = region.spatial_region
    return {"ixy": rule.ixy, "method": rule.method, "s": rule.s.tolist(), "lower": rule.lower.tolist(),
            "upper": rule.upper.tolist(), "filename": region.spatial_region_file}


_READERS: dict[str, Any] = {
    "FGMAX_FGMAX_GRIDS": _fgmax,
    "FGOUT_FGOUT_GRIDS": _fgout,
    "FLAGREGION_FLAGREGIONS": _flagregions,
    "GAUGE_GAUGES": _gauges,
    "QINIT_QINITFILES": _qinitfiles,
}


def _staged_files(work: Path, row: Any) -> None:
    """Every file name a stated record carries must sit in the run directory - the engine opens it from there."""
    fields = _fields(row)
    if "filename" not in fields:
        return
    index = fields.index("filename")
    for record in row.value:
        name = str(record[index])
        if not (work / name).is_file():
            _refuse("FILE_MISSING",
                    f"{row.slot.identifier} names {name!r}, which the run "
                    f"directory does not hold - holding {_holding(work)}")


def _same(stated: Any, found: Any) -> bool:
    """Equal at what the file format carries: quote wrapping stripped, floats at the writers' precision, a length-one list read back as the scalar it wrote."""
    if isinstance(found, str) and len(found) > 1 \
            and found.startswith("'") and found.endswith("'"):
        found = found[1:-1]
    if stated is None:
        return found is None or found == "None"
    if isinstance(stated, bool) or isinstance(found, bool):
        return stated is found or stated == found
    if isinstance(stated, Mapping) and isinstance(found, Mapping):
        return stated.keys() == found.keys() and all(
            _same(want, found[name]) for name, want in stated.items())
    if isinstance(stated, (list, tuple)) and isinstance(found, (list, tuple)):
        return len(stated) == len(found) and all(
            _same(want, got) for want, got in zip(stated, found))
    if isinstance(stated, (list, tuple)) and len(stated) == 1:
        return _same(stated[0], found)
    if isinstance(stated, (int, float)) and isinstance(found, (int, float)):
        return stated == found or abs(stated - found) <= 1e-9 * max(
            1.0, abs(float(stated)))
    return stated == found
