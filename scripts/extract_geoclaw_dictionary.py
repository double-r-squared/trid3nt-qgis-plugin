#!/usr/bin/env python3
"""Extract the GeoClaw keyword dictionary out of Clawpack 5.14.0's own Data classes.

Clawpack publishes no DFN or dico: its Data classes ARE the keyword surface -
every ``add_attribute`` a rundata group registers, with the default the class
gives it. This runs with clawpack importable - the spike venv interpreter
(``scratchpad/geoclaw_spike/venv/bin/python``; F9: no linux clawpack wheel on
PyPI, and clawpack never enters ``venvs/agent``) - instantiating the one 2D
geoclaw rundata a run starts from, and reads the prose each ``write()`` states
beside a keyword with an AST pass over the same modules. The keyword rows this
writes are generated data, never hand edited.
"""

from __future__ import annotations

import ast
import json
import sys
from inspect import signature
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from clawpack.clawutil.data import ClawData, ClawRunData
except ImportError:  # pragma: no cover - the refusal path on a non-spike interpreter
    raise SystemExit(
        "clawpack is not importable; run this with the spike venv interpreter "
        "(scratchpad/geoclaw_spike/venv/bin/python). Clawpack has no linux wheel "
        "on PyPI and never enters venvs/agent - the committed geoclaw.json is "
        "what the product path reads.")

#: The Clawpack release the worker image carries (``workers/geoclaw/Dockerfile``).
CLAWPACK_VERSION = "5.14.0"

#: The module words this extractor answers under: the shallow-water body, and the Boussinesq body, which is the same
#: rundata plus Clawpack's BoussData group (the worker runs its own prebuilt executable).
MODULE = "geoclaw"
BOUSS_MODULE = "geoclaw_bouss"

#: The rundata groups of a 2D geoclaw run, in ``ClawRunData``'s own order, each
#: its keyword prefix. A release adding or dropping a group refuses below rather
#: than silently growing or shrinking the surface.
_GROUPS: tuple[tuple[str, str], ...] = (
    ("clawdata", "CLAW"), ("geo_data", "GEO"), ("gaugedata", "GAUGE"),
    ("topo_data", "TOPO"), ("dtopo_data", "DTOPO"), ("amrdata", "AMR"),
    ("adjointdata", "ADJOINT"), ("regiondata", "REGION"),
    ("flagregiondata", "FLAGREGION"), ("refinement_data", "REFINEMENT"),
    ("fgout_data", "FGOUT"), ("fgmax_data", "FGMAX"), ("qinit_data", "QINIT"),
    ("surge_data", "SURGE"), ("friction_data", "FRICTION"),
    ("multilayer_data", "MULTILAYER"))

#: The group the Boussinesq module adds, as setrun adds it: ``rundata.add_data(BoussData(), 'bouss_data')``.
_BOUSS_GROUP = ("bouss_data", "BOUSS")

#: A list-of-records keyword and its record's fields, as the writer spells them
#: (data.py's own parse of each record, or the fgmax/fgout tools' headers).
_RECORD_FIELDS: Mapping[str, str] = {
    "TOPO_TOPOFILES": "(topo_type, filename)",
    "DTOPO_DTOPOFILES": "(dtopo_type, filename)",
    "QINIT_QINITFILES": "(filename)",
    "QINIT_FORCE_DRY_LIST": "(filename, tend)",
    "GAUGE_GAUGES": "(gaugeno, x, y, t1, t2)",
    "REGION_REGIONS": "(minlevel, maxlevel, t1, t2, x1, x2, y1, y2)",
    "FLAGREGION_FLAGREGIONS": ("(name, minlevel, maxlevel, t1, t2, "
                               "spatial_region_type, spatial_region)"),
    "FGMAX_FGMAX_GRIDS": ("(fgno, tstart_max, tend_max, dt_check, "
                          "min_level_check, arrival_tol, interp_method, "
                          "point_style, points)"),
    "FGOUT_FGOUT_GRIDS": ("(fgno, output_style, nout, tstart, tend, "
                          "point_style, output_format, x1, x2, y1, y2, nx, ny)"),
    "FGMAX_FGMAX_FILES": "(filename)",
    "ADJOINT_ADJOINT_FILES": "(filename)",
    "FRICTION_FRICTION_REGIONS": "(lower, upper, depths, manning_coefficients)",
    "FRICTION_FRICTION_FILES": "(filename)",
}

#: An attribute whose writer accepts spellings outside the default's Python
#: type: a 0/1 keyword also spelled by word (``'unsplit'``), or a scalar a
#: reader takes per gauge (dict/list). ARRAY is the surface's word for a value
#: or a list of values.
_ALIASES: frozenset[str] = frozenset({
    "CLAW_OUTPUT_FORMAT", "CLAW_DIMENSIONAL_SPLIT", "CLAW_SOURCE_SPLIT",
    "CLAW_TRANSVERSE_WAVES", "GAUGE_FILE_FORMAT", "GAUGE_DISPLAY_FORMAT",
    "GAUGE_Q_OUT_FIELDS", "GAUGE_AUX_OUT_FIELDS", "GAUGE_MIN_TIME_INCREMENT",
    "GAUGE_GTYPE", "SURGE_ROTATION_OVERRIDE", "SURGE_STORM_SPECIFICATION_TYPE",
})

#: An undefaulted keyword's type, read off the reader that consumes it (a
#: default of ``None`` states no Python type to read one from).
_OPEN_TYPES: Mapping[str, str] = {
    "CLAW_NUM_OUTPUT_TIMES": "INTEGER",
    "CLAW_OUTPUT_STEP_INTERVAL": "INTEGER",
    "CLAW_TOTAL_STEPS": "INTEGER",
    "CLAW_TFINAL": "REAL",
    "ADJOINT_T1": "REAL",
    "ADJOINT_T2": "REAL",
    "ADJOINT_INNERPROD_INDEX": "INTEGER",
    # A scalar or one tolerance per level: the reader allocates by token count.
    "REFINEMENT_SPEED_TOLERANCE": "ARRAY",
    "REFINEMENT_DEEP_DEPTH": "REAL",
    "REFINEMENT_MAX_LEVEL_DEEP": "INTEGER",
    "SURGE_STORM_TYPE": "ARRAY",
    "SURGE_STORM_FILE": "STRING",
}

#: The undefaulted keywords the engine refuses a run without, because its own
#: reader reads them unconditionally and a written ``None`` is not a value it
#: can read: claw.data's style-1 branch reads both. The other undefaulted
#: keywords are tolerated unset (an empty speed_tolerance is the criterion
#: off, a None storm_file or adjoint t1 is the backcompat sentinel, the
#: output_style-3 pair is only read under that style).
_MANDATORY: frozenset[str] = frozenset({"CLAW_NUM_OUTPUT_TIMES", "CLAW_TFINAL"})

#: The spellings a writer or reader validates, as its own case/select states
#: them. Only scalar keywords carry this: a list's spellings are the engine's
#: own to check element by element.
_CHOICES: Mapping[str, tuple[str, ...]] = {
    "CLAW_OUTPUT_STYLE": ("1", "2", "3"),
    "CLAW_OUTPUT_FORMAT": ("1", "2", "3", "ascii", "binary32", "binary64",
                           "binary"),
    "CLAW_DIMENSIONAL_SPLIT": ("0", "1", "2", "unsplit", "godunov", "strang"),
    "CLAW_SOURCE_SPLIT": ("0", "1", "2", "none", "godunov", "strang"),
    "CLAW_TRANSVERSE_WAVES": ("0", "1", "2", "none", "increment", "all"),
    "CLAW_CHECKPT_STYLE": ("-4", "-3", "-2", "-1", "0", "1", "2", "3", "4"),
    # storm_module.f90: case(0..2) else stop.
    "SURGE_DRAG_LAW": ("0", "1", "2"),
    "SURGE_ROTATION_OVERRIDE": ("0", "1", "2", "normal", "n", "s"),
    # SurgeData.storm_spec_dict_mapping, the writer's own name-to-code table.
    "SURGE_STORM_SPECIFICATION_TYPE": (
        "data", "holland80", "holland08", "holland10", "cle", "slosh",
        "rankine", "modified-rankine", "demaria", "willoughby",
        "-1", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"),
    # FGmaxData.write: num_fgmax_val not in [1, 2, 5] raises.
    "FGMAX_NUM_FGMAX_VAL": ("1", "2", "5"),
    # BoussData.write's own descriptions: 0=SWE, 1=MS, 2=SGN and 1=GMRES, 2=Pardiso, 3=PETSc.
    "BOUSS_BOUSS_EQUATIONS": ("0", "1", "2"),
    "BOUSS_BOUSS_SOLVER": ("1", "2", "3"),
}


def module_input_dir() -> Path:
    """Where the committed module inputs live."""
    return (Path(__file__).resolve().parents[1] / "trid3nt_server" / "workflows"
            / "geoclaw" / "modules" / "module_input")


def _type_word(identifier: str, value: Any) -> str:
    """The dictionary type word this attribute's default and writer agree on."""
    if identifier in _OPEN_TYPES:
        return _OPEN_TYPES[identifier]
    if identifier in _RECORD_FIELDS:
        return "STRUCT"
    if identifier in _ALIASES or isinstance(value, (list, tuple)):
        return "ARRAY"
    if isinstance(value, bool):
        return "LOGICAL"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "REAL"
    if isinstance(value, str):
        return "STRING"
    raise ValueError(f"{identifier}: no type word for {value!r}")


def _text(node: ast.AST) -> str:
    """A description expression as its prose: a string, a join, or a tuple of parts."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Tuple):
        return "".join(_text(item) for item in node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _text(node.left) + _text(node.right)
    raise ValueError(f"cannot read the description {ast.dump(node)}")


def _descriptions() -> dict[tuple[str, str], str]:
    """The prose each ``write()`` states beside a keyword: (class, attribute) -> text.

    Read statically so a branch the default run does not take (a test
    topography's fields, say) still carries its own writer's words.
    """
    found: dict[tuple[str, str], str] = {}
    for module in ("clawpack.clawutil.data", "clawpack.amrclaw.data",
                   "clawpack.geoclaw.data"):
        tree = ast.parse(Path(sys.modules[module].__file__).read_text())
        for cls in (node for node in tree.body
                    if isinstance(node, ast.ClassDef)):
            write = next((node for node in cls.body
                          if isinstance(node, ast.FunctionDef)
                          and node.name == "write"), None)
            if write is None:
                continue
            for call in (node for node in ast.walk(write)
                         if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Attribute)
                         and node.func.attr == "data_write"):
                positional = [arg for arg in call.args]
                name = next((_text(arg) for arg in positional
                             if isinstance(arg, ast.Constant)
                             and isinstance(arg.value, str)), None)
                name = name or next(
                    (_text(kw.value) for kw in call.keywords
                     if kw.arg == "name" and isinstance(kw.value, ast.Constant)),
                    None)
                described = next((kw.value for kw in call.keywords
                                  if kw.arg == "description"), None)
                if name is None or described is None:
                    continue
                found.setdefault((cls.name, name), _text(described))
    return found


def _group_rows(name: str, group: str, obj: ClawData,
                helps: Mapping[tuple[str, str], str]) -> list[dict[str, Any]]:
    """Every keyword of one rundata group, in the attribute order it registers."""
    out_file = signature(type(obj).write).parameters["out_file"].default
    if not isinstance(out_file, str):
        raise ValueError(f"{name}: write() spells no out_file default")
    rows: list[dict[str, Any]] = []
    for attribute in obj.attributes():
        value = getattr(obj, attribute)
        identifier = f"{group}_{attribute.upper()}"
        row: dict[str, Any] = {
            "keyword": identifier,
            "identifier": identifier,
            "type": _type_word(identifier, value),
            "shape": _RECORD_FIELDS.get(identifier, ""),
            "size": 1,
            "unbounded": False,
            "help": helps.get((type(obj).__name__, attribute), ""),
            "rubrique": [out_file],
            "is_file": False,
            "mandatory": identifier in _MANDATORY,
        }
        if identifier in _MANDATORY and value is not None:
            raise ValueError(f"{identifier}: mandatory but the class defaults it")
        if value is not None:
            row["default"] = value
        elif identifier not in _OPEN_TYPES:
            raise ValueError(
                f"{identifier}: the class defaults it to None and no reader "
                "types it; adjudicate the keyword's type in _OPEN_TYPES")
        if identifier in _CHOICES:
            row["choices"] = list(_CHOICES[identifier])
        rows.append(row)
    return rows


def rows(module: str = MODULE) -> list[dict[str, Any]]:
    """Every dictionary row of one geoclaw module, in the rundata's order."""
    run = ClawRunData("geoclaw", 2)
    all_groups = _GROUPS
    if module == BOUSS_MODULE:
        from clawpack.geoclaw.data import BoussData

        run.add_data(BoussData(), _BOUSS_GROUP[0])
        all_groups = (*_GROUPS, _BOUSS_GROUP)
    elif module != MODULE:
        raise ValueError(f"no rundata for the module {module!r}; the modules are {MODULE} and {BOUSS_MODULE}.")
    groups = dict(all_groups)
    carried = {attribute for attribute in run.attributes()
               if isinstance(getattr(run, attribute), ClawData)}
    if carried != set(groups):
        raise ValueError(
            f"ClawRunData {CLAWPACK_VERSION} carries {sorted(carried)}, not the "
            f"adjudicated groups {sorted(groups)}; re-read write() and update "
            "_GROUPS, _RECORD_FIELDS and _OPEN_TYPES before extracting.")
    helps = _descriptions()
    found = [row for name, group in all_groups
             for row in _group_rows(name, group, getattr(run, name), helps)]
    identifiers = [row["identifier"] for row in found]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("two groups spell a keyword the same")
    dead = set(_RECORD_FIELDS) - set(identifiers)
    if dead:
        raise ValueError(
            f"_RECORD_FIELDS names {sorted(dead)}, which no group spells; "
            "the writer moved - re-read write() before extracting.")
    return found


def extract(dest: Path, module: str = MODULE) -> dict[str, int]:
    """Write ``<module>.json`` into ``dest`` -> keyword counts.

    The fresh rows take the ``module`` and ``keywords`` keys of the committed
    file they replace; every other key it carries stays, in its own order, so
    an authored section survives regeneration."""
    found = rows(module)
    dest.mkdir(parents=True, exist_ok=True)
    committed = dest / f"{module}.json"
    doc = json.loads(committed.read_text()) if committed.is_file() else {}
    doc.update({"module": module, "keywords": found})
    (dest / f"{module}.json").write_text(json.dumps(doc, indent=2) + "\n")
    return {module: len(found)}


def main() -> int:
    counts = {**extract(module_input_dir()), **extract(module_input_dir(), BOUSS_MODULE)}
    print(json.dumps(counts, indent=2))
    print("total keywords:", sum(counts.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
