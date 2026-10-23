"""The run directory's .data set: the sheet as one ClawRunData, written by Clawpack.

Each filled keyword lands on the Data object its prefix and suffix name - the mapping
read off ClawRunData itself and cross-checked against the dictionary's rubrique - then
``rundata.write()`` formats every file. A record whose shape declares a filename is
relabeled to the staged bare name after the write, because the writers abspath it and
the run directory is bind-mounted at /data.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Mapping

from ..errors import GeoclawError

logger = logging.getLogger("trid3nt_server.workflows.geoclaw.authoring.rundata")

__all__ = ["serialize"]

#: The STRUCT rows whose writers iterate tool objects instead of plain lists: each
#: record's fields are set on this class, by the names the dictionary's shape declares.
_OBJECT_ROWS: Mapping[str, str] = {
    "FGMAX_FGMAX_GRIDS": "FGmaxGrid",
    "FGOUT_FGOUT_GRIDS": "FGoutGrid",
    "FLAGREGION_FLAGREGIONS": "FlagRegion",
}


def serialize(sheet: Any, rundir: Path | str) -> dict[str, Any]:
    """Write the .data set the sheet states into the run directory -> what was written.

    Every value comes off the sheet, silent keywords at the dictionary's own default,
    and Clawpack's writer formats each line. A MANDATORY keyword nothing stated
    refuses here by name; the run directory is this author's alone.
    """
    work = Path(rundir)
    work.mkdir(parents=True, exist_ok=True)
    groups, rundata = _groups(sheet)
    _applied(sheet, groups)
    _unstated(sheet)
    from .dtopo import write_dtopofiles
    from .qinit import write_qinitfiles
    from .topo import write_topofiles

    write_topofiles(sheet, work)
    write_dtopofiles(sheet, work)
    write_qinitfiles(sheet, work)
    written = sorted(out_file(obj) for _name, obj in groups.values())
    try:
        rundata.write(str(work))
    except GeoclawError:
        raise
    except Exception as exc:
        _refuse("AUTHORING_WRITE",
                f"Clawpack refused the .data write - {type(exc).__name__}: "
                f"{exc}. What this sheet states has no form its writer takes.")
    _relabeled(sheet, work)
    logger.info("geoclaw .data written to %s: %s", work, written)
    return {"written": written}


def _refuse(code: str, message: str) -> None:
    raise GeoclawError(message, error_code=f"GEOCLAW_{code}")


def _prefix_of(group_attr: str) -> str:
    """A rundata group attribute's keyword prefix - the inverse leg 2's extractor used."""
    return group_attr.upper().removesuffix("_DATA").removesuffix("DATA")


def out_file(obj: Any) -> str:
    """The file a Data group's own write() names - the dictionary's rubrique."""
    import inspect

    return inspect.signature(obj.write).parameters["out_file"].default


def _groups(sheet: Any) -> tuple[dict[str, tuple[str, Any]], Any]:
    """Every prefix a filled keyword derives from -> the ClawRunData group it lands on.

    Read off a fresh ClawRunData, never a hand table; each keyword's rubrique must be
    the file its group's writer writes, so a release moving a keyword refuses by name
    instead of writing it into another group's file.
    """
    from clawpack.clawutil.data import ClawRunData

    rundata = ClawRunData("geoclaw", 2)
    groups = {_prefix_of(name): (name, obj)
              for name, obj in vars(rundata).items() if hasattr(obj, "write")}
    for identifier, row in sheet.filled.items():
        prefix, sep, _rest = identifier.partition("_")
        if not sep:
            _refuse("KEYWORD_SHAPE",
                    f"{identifier} is a bare name no rundata keyword can be "
                    "derived from; a keyword is PREFIX_NAME.")
        found = groups.get(prefix)
        if found is None:
            _refuse("GROUP_MISMATCH",
                    f"{identifier} names the prefix {prefix}, which no rundata "
                    "group answers; the groups are " + ", ".join(sorted(groups)) + ".")
        if out_file(found[1]) != row.slot.rubrique[0]:
            _refuse("GROUP_MISMATCH",
                    f"{identifier} belongs to {row.slot.rubrique[0]!r} per the "
                    f"dictionary, but {prefix} writes {out_file(found[1])!r}.")
    return groups, rundata


def _unstated(sheet: Any) -> None:
    """Refuse every MANDATORY keyword the sheet leaves unstated, naming each one.

    Clawpack's readers read these unconditionally and a written None is no value
    they can read, so only the question can state them - the engine has none.
    """
    missing = sorted(identifier
                     for identifier, slot in sheet.body.MODULE_INPUT.items()
                     if slot.is_required and identifier not in sheet.filled)
    if missing:
        _refuse("KEYWORD_UNSTATED",
                f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} "
                "mandatory and stated by nothing: state it from a measurement "
                "or a named source - the engine has no value for it.")


def _applied(sheet: Any, groups: dict[str, tuple[str, Any]]) -> None:
    """Set every stated keyword on its group: scalars first, record rows after.

    Scalars lead so a record object can read the group's own stated scalars
    (FlagRegion takes FLAGREGION_NUM_DIM at construction).
    """
    rows = []
    for identifier, row in sheet.filled.items():
        prefix = identifier.partition("_")[0]
        name = identifier[len(prefix) + 1:].lower()
        group = groups[prefix][1]
        if row.slot.type == "STRUCT":
            rows.append((identifier, name, row, group))
            continue
        try:
            setattr(group, name, row.value)
        except AttributeError:
            _refuse("KEYWORD_UNKNOWN",
                    f"{identifier} is no attribute of {groups[prefix][0]}: the "
                    "dictionary spells a keyword the rundata group does not hold.")
    for identifier, name, row, group in rows:
        setattr(group, name, _records(identifier, row, groups))


def _fields(row: Any) -> tuple[str, ...]:
    """A STRUCT row's record fields, parsed from the dictionary's own shape key."""
    fields = tuple(part.strip()
                   for part in row.slot.shape.strip().strip("()").split(","))
    if not fields or fields == ("",):
        _refuse("RECORD_SHAPE",
                f"{row.slot.identifier} is a record row carrying no shape: "
                "the dictionary's shape key declares its fields.")
    return fields


def _records(identifier: str, row: Any,
             groups: dict[str, tuple[str, Any]]) -> list[Any]:
    """One STRUCT row at the writer's own list form, or the tool object it iterates.

    A record must carry every field the dictionary's shape declares: the writers take
    the fields positionally and a short record would silently drop the tail.
    """
    fields = _fields(row)
    for record in row.value:
        if len(record) != len(fields):
            _refuse("RECORD_ARITY",
                    f"{identifier} carries a record of {len(record)} values "
                    f"where its shape {row.slot.shape} declares {len(fields)}: "
                    f"{record!r}")
    kind = _OBJECT_ROWS.get(identifier)
    if kind is None:
        return [list(record) for record in row.value]
    return [_object(kind, fields, record, identifier, groups)
            for record in row.value]


def _object(kind: str, fields: tuple[str, ...], record: Any,
            identifier: str, groups: dict[str, tuple[str, Any]]) -> Any:
    """One record as the tool object the writer iterates, fields set by shape name."""
    if kind == "FlagRegion":
        from clawpack.amrclaw.data import FlagRegion

        obj = FlagRegion(int(groups["FLAGREGION"][1].num_dim))
    elif kind == "FGmaxGrid":
        from clawpack.geoclaw.fgmax_tools import FGmaxGrid

        obj = FGmaxGrid()
    else:
        from clawpack.geoclaw.fgout_tools import FGoutGrid

        obj = FGoutGrid()
    for field, item in zip(fields, record):
        # An fgmax grid's geometry is stated under the tool's own attribute names (x1, y1, x2, y2, nx, ny for a uniform grid): the shape's one ``points`` field carries them.
        stated = (item.items() if kind == "FGmaxGrid" and field == "points"
                  and isinstance(item, Mapping) else [(field, item)])
        for name, value in stated:
            try:
                setattr(obj, name, value)
            except AttributeError:
                _refuse("RECORD_FIELD",
                        f"{identifier} states the field {name!r}, which "
                        f"{type(obj).__name__} does not hold.")
    return obj


def _relabeled(sheet: Any, work: Path) -> None:
    """The staged bare file name standing where the writer abspath'd it - path only.

    The writers resolve a record's relative filename against the .data file's own
    directory; the launcher bind-mounts the run directory at /data, so the staged
    name is what the engine opens and no host path may reach a file.
    """
    for identifier, row in sheet.filled.items():
        if row.slot.type != "STRUCT" or "filename" not in _fields(row):
            continue
        fields = _fields(row)
        index = fields.index("filename")
        path = work / row.slot.rubrique[0]
        text = path.read_text(encoding="utf-8")
        for record in row.value:
            name = str(record[index])
            absolute = os.path.abspath(
                os.path.join(os.path.dirname(str(path)), name))
            text = text.replace(absolute, name)
        path.write_text(text, encoding="utf-8")
