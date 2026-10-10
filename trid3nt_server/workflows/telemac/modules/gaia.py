"""The GAIA wrapper: its module input, the two sediment composites and the dredge.

GAIA runs under a hydrodynamic module and states no value of its own (the quartz density default
is unwritten). Cohesive sediment is approximated as very fine non-cohesive. The dredge is NESTOR,
keywords on this deck; its material goes through GAIA's per-class mass evolution.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from .module import Module, Output, SlotRefused
from .outputs import PRIMITIVES

__all__ = ["GAIA", "GRAIN_UM_MAX", "GRAIN_UM_MIN", "MODULE_OUTPUT",
           "STEERING_FILENAME", "RESULT_FILENAME", "Backfill", "Bed", "Dig",
           "Dredging", "Dump", "SaveWaterLevel", "Suspension"]

# What the module writes, by VARIABLES FOR GRAPHIC PRINTOUTS mnemonic. The bed itself is the carrier's
# BOTTOM, rowed once there. Evolution is cumulative: the last frame is the event's signed bed change,
# deposition positive, diverging about zero. The surface diameter is written whatever the gradation,
# and the engine packs its unit letter into the 32-character NAME field.
MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({
    "E": Output("CUMUL BED EVOL", "m",
                style={"kind": "mesh", "ramp": "rdbu", "units": "m",
                       "center": 0.0}),
    "D50": Output("MEAN DIAMETER M", "",
                  style={"kind": "mesh", "ramp": "cividis", "units": "m"}),
    "TOB": Output("BED SHEAR STRESS", "N/m2",
                  style={"kind": "mesh", "ramp": "inferno", "units": "N/m2",
                         "floor": 0}),
})

# GAIA appends one tracer per suspended class to its carrier's tracers, which the carrier never counts; a class is put into the water, so it has an edge.
_SUSPENDED = Output("NCOH SEDIMENT", "g/L", has_edge=True, injected=True,
                    style={"kind": "mesh", "ramp": "oranges", "units": "g/L",
                           "floor": 0})

STEERING_FILENAME = "gaia_domain.cas"
# GAIA's own result SELAFIN, carrying CUMUL BED EVOL.
RESULT_FILENAME = "gaia_domain.slf"


# The grain-size window the transport formulae are authored for, in microns.
GRAIN_UM_MIN, GRAIN_UM_MAX = 5.0, 2000.0
# A mixture sorts; fewer classes than this is one class.
_MIXTURE_MIN_CLASSES = 2
_MIXTURE_MAX_CLASSES = 6
# mg/L -> kg/m3, the unit the source concentration keyword reads.
_MGL_TO_KGM3 = 1.0e-3


class _Gaia(Module("gaia")):  # type: ignore[misc]
    """The module input and the two sediment bodies."""

    @classmethod
    def bed(cls, *, geometry: Any, boundary: Any, gradation: Any = None,
            presets: Any = None, dredging: Any = None,
            **keywords: Any) -> Mapping[str, Any]:
        """A non-cohesive bed with a real stock, one class or a mixture that sorts.

        Diameters, stock, formula and morphological factor are dictionary keywords; this takes the
        gradation (preset name or fine-to-coarse pairs) and the dredge. Stated last, so a mixture's class table wins.
        """
        for name in keywords:
            cls.slot(name)
        return _body({"GEOMETRY_FILE": geometry, "BOUNDARY_CONDITIONS_FILE": boundary,
                      "RESULTS_FILE": RESULT_FILENAME,
                      "BED_LOAD_FOR_ALL_SANDS": True, **keywords,
                      "bed": Bed(gradation=gradation, presets=presets),
                      "dredging": dredging})

    @classmethod
    def suspended(cls, *, geometry: Any, boundary: Any, concentration_mgl: Any,
                  dredging: Any = None, **keywords: Any) -> Mapping[str, Any]:
        """One settling class over a bed with no stock: supply-limited.

        Zero thickness, so only the pulse deposits; a second carrier tracer. The source concentration arrives in mg/L and is ingested as kg/m3.
        """
        if dredging is not None:
            raise SlotRefused(
                "a dredge moves material out of a bed and into another, and this "
                "body is a suspension over a bed with NO stock - there is nothing "
                "to dig and nowhere the dumped material would be accounted. State "
                "the dredge on a bed().")
        for name in keywords:
            cls.slot(name)
        return _body({"GEOMETRY_FILE": geometry, "BOUNDARY_CONDITIONS_FILE": boundary,
                      "RESULTS_FILE": RESULT_FILENAME, **keywords,
                      "suspension": Suspension(concentration_mgl=concentration_mgl)})


def Bed(*, gradation: Any, presets: Any) -> Mapping[str, Any]:  # noqa: N802
    """The bed's gradation: a preset name in ``presets``, or fine-to-coarse ``[d50_um, fraction]`` pairs.
    Nothing leaves the deck's own class.
    """
    return {"gradation": gradation, "presets": presets}


def Suspension(*, concentration_mgl: Any) -> Mapping[str, Any]:  # noqa: N802
    """The settling class's source concentration, in mg/L."""
    return {"concentration_mgl": concentration_mgl}


def Dredging(*, actions: Any, measured: Any,  # noqa: N802
             origin: Any) -> Mapping[str, Any]:
    """The dredge as one value: the actions, the measurement of its areas and reference surface, and the run's time origin.

    ``measured`` names what the workflow takes against the settled run; an action's string argument is a name read off it, else off the run.
    """
    return {"actions": list(actions), "measured": measured,
            "settled": "settled", "origin": origin}


def Dig(*, field: Any, start: Any, end: Any, volume: Any = None,  # noqa: N802
        rate: Any = None, depth: Any = None, crit_depth: Any = None,
        repeat: Any = None, min_volume: Any = None,
        min_volume_radius: Any = None, level: Any = None, dump: Any = None,
        dump_rate: Any = None) -> Mapping[str, Any]:
    """Material taken out of ``field``, optionally dumped into ``dump``.

    ``volume`` digs that much between the two times (Dig_by_time); ``rate`` digs to ``depth`` below the
    reference where the bed is shallower than ``crit_depth`` under it, every ``repeat`` (Dig_by_criterion).
    """
    if (volume is None) == (rate is None):
        raise SlotRefused(
            "a dig states a volume OR a rate: a volume is the material taken "
            "between the two times, a rate is the speed the bed is taken down to "
            "the grade at whenever the criterion is met. Stating both, or "
            "neither, names no action the engine has.")
    return {"type": "Dig_by_time" if volume is not None else "Dig_by_criterion",
            "field": field, "start": start, "end": end, "volume": volume,
            "rate": rate, "depth": depth, "crit_depth": crit_depth,
            "repeat": repeat, "min_volume": min_volume,
            "min_volume_radius": min_volume_radius, "level": level,
            "dump": dump, "dump_rate": dump_rate}


def Dump(*, field: Any, start: Any, end: Any, volume: Any,  # noqa: N802
         grain_class: Any) -> Mapping[str, Any]:
    """``volume`` of material put into ``field`` between the two times.

    ``grain_class`` is one fraction per sediment class, summing to one; the engine refuses a RATE on a timed dump.
    """
    return {"type": "Dump_by_time", "field": field, "start": start, "end": end,
            "volume": volume, "grain_class": list(grain_class)}


def Backfill(*, field: Any, start: Any, end: Any,  # noqa: N802
             crit_depth: Any, level: Any, grain_class: Any) -> Mapping[str, Any]:
    """``field`` filled back up to ``crit_depth`` under the reference level."""
    return {"type": "Backfill_to_level", "field": field, "start": start,
            "end": end, "crit_depth": crit_depth, "level": level,
            "grain_class": list(grain_class)}


def SaveWaterLevel(*, start: Any, level: Any) -> Mapping[str, Any]:  # noqa: N802
    """The free surface at ``start``, saved into ``level`` for a later action.

    ``level`` is WATERLVL1..WATERLVL3; an action reading one refuses unless a save wrote it earlier in the file.
    """
    return {"type": "Save_water_level", "start": start, "level": level}


def _dredging(value: Mapping[str, Any], *, run: Mapping[str, Any]
              ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The dredge -> NESTOR armed on this deck, and the three files it reads.

    No restart file is authored, so the action file states RESTART = FALSE.
    """
    from ..authoring import nestor

    return ({"NESTOR": True,
             "NESTOR_ACTION_FILE": nestor.ACTION_FILENAME,
             "NESTOR_POLYGON_FILE": nestor.POLYGON_FILENAME,
             "NESTOR_SURFACE_REFERENCE_FILE": nestor.REFERENCE_FILENAME},
            nestor.files([_named_action(action, value, run)
                          for action in value["actions"]],
                         reference=value["measured"]["profiles"],
                         origin=value["origin"]))


# Arguments of an action that take an input's name; its type and level are the engine's words.
_ACTION_READS = ("field", "start", "end", "volume", "rate", "depth", "crit_depth",
                 "repeat", "min_volume", "min_volume_radius", "dump",
                 "dump_rate", "grain_class")


def _named_action(action: Mapping[str, Any], value: Mapping[str, Any],
                  run: Mapping[str, Any]) -> dict[str, Any]:
    """One action with each name read off the measurement, else the run; a minimum volume is gathered over the mesh's edge where no radius is stated."""
    held = {**run, **value["measured"]}
    named = {key: held[item] if key in _ACTION_READS and isinstance(item, str)
             else item for key, item in action.items()}
    if named.get("min_volume") is not None and named.get("min_volume_radius") is None:
        named["min_volume_radius"] = value["settled"]["mesh_size_m"]
    return named


def _classes(gradation: Any, presets: Mapping[str, Any]
             ) -> list[tuple[float, float]] | None:
    """A gradation -> clean fine-to-coarse ``(d50_um, fraction)`` pairs, renormalized; ``None`` under two classes."""
    if gradation is None:
        return None
    if isinstance(gradation, str):
        gradation = dict(presets).get(gradation.strip().lower().replace(" ", "_"))
        if gradation is None:
            return None
    pairs = []
    for item in list(gradation):
        um, fraction = ((float(item["d50_um"]), float(item.get("fraction", 0.0)))
                        if isinstance(item, Mapping)
                        else (float(item[0]), float(item[1])))
        if um > 0.0 and fraction >= 0.0:
            pairs.append((_windowed(um), fraction))
    if len(pairs) < _MIXTURE_MIN_CLASSES:
        return None
    pairs = sorted(pairs)[:_MIXTURE_MAX_CLASSES]
    total = sum(fraction for _, fraction in pairs)
    return [(um, fraction / total if total > 0.0 else 1.0 / len(pairs))
            for um, fraction in pairs]


def _windowed(micron: float) -> float:
    if not (GRAIN_UM_MIN <= micron <= GRAIN_UM_MAX):
        raise ValueError(
            f"a {micron:g} um class is outside the {GRAIN_UM_MIN:g}-{GRAIN_UM_MAX:g} "
            "um window GAIA's transport formulae are authored for.")
    return micron


def _bed(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The gradation -> the CLASSES lists a mixture is, or no keyword at all.

    Under two classes it is not a mixture and the deck's single class stands.
    """
    classes = _classes(value["gradation"], value["presets"])
    if not classes:
        return ({"CLASSES_TYPE_OF_SEDIMENT": ["NCO"]}, {})
    return ({"CLASSES_TYPE_OF_SEDIMENT": ["NCO" for _ in classes],
             # Presets are in microns; the conversion to the keyword's metres belongs here.
             "CLASSES_SEDIMENT_DIAMETERS": [_metres(um) for um, _ in classes],
             "CLASSES_INITIAL_FRACTION": [fraction for _, fraction in classes]},
            {})


def _suspension(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                   Mapping[str, Any]]:
    """One class, suspension armed, no stock to erode; sorption terms are over kg/m3, so mg/L converts here."""
    return ({"CLASSES_TYPE_OF_SEDIMENT": ["NCO"],
             "SUSPENSION_FOR_ALL_SANDS": True,
             "LAYERS_INITIAL_THICKNESS": [0.0],
             "SUSPENDED_SEDIMENTS_CONCENTRATION_VALUES_AT_THE_SOURCES": [
                 max(float(value["concentration_mgl"]) * _MGL_TO_KGM3, 0.0)]}, {})


def _metres(micron: Any) -> float:
    return float(micron) * 1.0e-6


def _body(slots: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"module": "gaia", "steering": STEERING_FILENAME,
            "slots": dict(slots)}


def _appended(body: Mapping[str, Any]) -> tuple[Output, ...]:
    """One tracer per suspended class, none over a bed the carrier only erodes."""
    return (_SUSPENDED,) if "suspension" in dict(body.get("slots") or {}) else ()


# The row whose zero is itself an answer, and the stress row that would have moved it: an exactly-zero cumulative evolution is read against the shear on the bed.
_BED_EVOLUTION = "E"
_BED_SHEAR = "TOB"


def _bed_field(primitive: Any, solved: Any) -> Any:
    read = PRIMITIVES["field"](primitive, solved)
    if primitive.variable == _BED_EVOLUTION:
        _state_the_zero(read, solved)
    return read


def _state_the_zero(read: Any, solved: Any) -> None:
    """What the run says when its bed did not move at all.

    Exactly nothing is readable only beside the engine's bed shear stress; the threshold of motion is a value of it.
    """
    import numpy as np

    from trid3nt_server.workflows.runtime import journal_note

    values = np.asarray(getattr(read, "values", ()), dtype=float)
    if not values.size or bool(np.any(values != 0.0)):
        return
    journal_note(
        f"the bed did not move: {read.name.strip()} reads exactly 0 m at every "
        f"node this run solved on, {_against(solved)} - nothing this flow put on "
        "the bed reached the threshold of motion for the grain the deck states.")


def _against(solved: Any) -> str:
    import numpy as np

    try:
        name, units, stress = solved.frames(_BED_SHEAR, None)
    except Exception:  # noqa: BLE001 - a deck that rows no stress states so
        return f"and the deck wrote no {_BED_SHEAR} to read that against"
    peak = float(np.nanmax(np.asarray(stress, dtype=float)))
    return f"and the engine's own {name.strip()} peaked at {peak:.4g} {units}"


GAIA = _Gaia
GAIA.APPENDABLE = (("each suspended class", (_SUSPENDED,)),)
GAIA.MODULE_OUTPUT = MODULE_OUTPUT
GAIA.PRINTOUTS = "VARIABLES_FOR_GRAPHIC_PRINTOUTS"
GAIA.RESULT_FILE = RESULT_FILENAME
GAIA.composites(reads={"bed": ("gradation",),
                       "suspension": ("concentration_mgl",),
                       "dredging": ("measured", "settled", "actions",
                                    *_ACTION_READS)},
                bed=_bed, suspension=_suspension, dredging=_dredging)
GAIA.appends(_appended)
GAIA.reads(**{**PRIMITIVES, "field": _bed_field})
