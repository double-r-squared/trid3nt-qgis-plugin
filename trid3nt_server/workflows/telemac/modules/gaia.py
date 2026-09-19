"""The GAIA wrapper: its module input, the two sediment composites and the dredge.

GAIA runs UNDER a hydrodynamic module and states no value of its own: the bed
and the suspension expand what a template handed them, and the density the
dictionary defaults to (quartz) is unwritten. Cohesive sediment is approximated
as very fine non-cohesive; the Krone/Partheniades path is not exposed. The dredge
is NESTOR, which the engine offers as keywords on this deck rather than as a
module of its own: armed here, the material it moves goes through the per-class
mass evolution GAIA computes its bed evolution and its sediment balance from."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from .module import Module, Output, SlotRefused
from .outputs import PRIMITIVES

__all__ = ["GAIA", "GRAIN_UM_MAX", "GRAIN_UM_MIN", "MODULE_OUTPUT",
           "STEERING_FILENAME", "RESULT_FILENAME", "Backfill", "Bed", "Dig",
           "Dredging", "Dump", "SaveWaterLevel", "Suspension"]

#: What the module WRITES, by the mnemonic VARIABLES FOR GRAPHIC PRINTOUTS
#: spells: the result-file name, the unit and how it draws. The BED ITSELF is
#: not here: it is the carrier's own BOTTOM, rowed once on the carrier's table,
#: and rowing it again would put two layers of one field on one mesh. What is
#: here is what the sediment module ADDS. The evolution is cumulative, so its
#: last frame is the whole event's bed change - signed, deposition positive, on
#: a ramp diverging about zero. The surface diameter is written whatever the
#: gradation, and the engine packs its unit letter into the 32-character NAME
#: field rather than the unit field beside it.
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

#: The class GAIA appends to its carrier's tracers when the body carries a
#: suspension: one tracer per suspended class, which the carrier never counts.
#: A suspended class is PUT into the water, so what it reaches has a visible
#: edge and the water beyond it is not a faint wash of the same colour.
_SUSPENDED = Output("NCOH SEDIMENT", "g/L", has_edge=True, injected=True,
                    style={"kind": "mesh", "ramp": "oranges", "units": "g/L",
                           "floor": 0})

STEERING_FILENAME = "gaia_domain.cas"
#: GAIA's own result SELAFIN, carrying CUMUL BED EVOL.
RESULT_FILENAME = "gaia_domain.slf"


#: The grain-size window the transport formulae are authored for, in microns.
GRAIN_UM_MIN, GRAIN_UM_MAX = 5.0, 2000.0
#: A mixture sorts; fewer classes than this cannot, and the bed is one class.
_MIXTURE_MIN_CLASSES = 2
_MIXTURE_MAX_CLASSES = 6
#: mg/L -> kg/m3, the unit the source concentration keyword reads.
_MGL_TO_KGM3 = 1.0e-3


class _Gaia(Module("gaia")):  # type: ignore[misc]
    """The module input and the two sediment bodies."""

    @classmethod
    def bed(cls, *, geometry: Any, boundary: Any, gradation: Any = None,
            presets: Any = None, dredging: Any = None,
            **keywords: Any) -> Mapping[str, Any]:
        """A non-cohesive bed with a real stock, one class or a mixture that SORTS.

        The diameters, the stock, the transport formula and the morphological
        factor are keywords the dictionary carries and the deck states by name;
        what arrives here is the GRADATION the dictionary has no keyword for - a
        preset name or fine-to-coarse pairs - and the dredge moved out of it. The
        expansion is stated LAST, so a mixture's own class table wins over the
        single class a deck states beside it."""
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
        """ONE settling class over a bed with NO stock: supply-limited.

        Zero thickness, so only the pulse deposits; a SECOND carrier tracer. The
        class diameter, the transport formula and the advection scheme are the
        deck's own keywords; the source concentration arrives in the mg/L the
        question is asked in and is ingested as the keyword's kg/m3."""
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
    """The bed's GRADATION: a preset name in ``presets``, or fine-to-coarse
    ``[d50_um, fraction]`` pairs. Nothing at all leaves the deck's own class."""
    return {"gradation": gradation, "presets": presets}


def Suspension(*, concentration_mgl: Any) -> Mapping[str, Any]:  # noqa: N802
    """The settling class's source concentration, in the mg/L it is asked in."""
    return {"concentration_mgl": concentration_mgl}


def Dredging(*, actions: Any, reference: Any,  # noqa: N802
             origin: Any) -> Mapping[str, Any]:
    """The dredge as one value: what is done, the surface its levels are read
    from, and the run's own time origin every action is dated against."""
    return {"actions": list(actions), "reference": reference, "origin": origin}


def Dig(*, field: Any, start: Any, end: Any, volume: Any = None,  # noqa: N802
        rate: Any = None, depth: Any = None, crit_depth: Any = None,
        repeat: Any = None, min_volume: Any = None,
        min_volume_radius: Any = None, level: Any = None, dump: Any = None,
        dump_rate: Any = None) -> Mapping[str, Any]:
    """Material taken out of ``field``, optionally dumped into ``dump``.

    A stated ``volume`` digs that much between the two times (Dig_by_time); a
    stated ``rate`` digs to ``depth`` below the reference wherever the bed sits
    shallower than ``crit_depth`` under it, every ``repeat`` (Dig_by_criterion)."""
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

    ``grain_class`` is one fraction per sediment class, summing to one; the
    engine refuses a RATE on a timed dump, which is the dig action's own keyword."""
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

    ``level`` is WATERLVL1..WATERLVL3, and an action reading one refuses unless
    a save wrote it earlier in the file."""
    return {"type": "Save_water_level", "start": start, "level": level}


def _dredging(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                 Mapping[str, Any]]:
    """The dredge -> NESTOR armed on this deck, and the three files it reads.

    The restart file is not among them: this composite authors none, so the
    action file states RESTART = FALSE and the deck names no file that is absent."""
    from ..authoring import nestor

    return ({"NESTOR": True,
             "NESTOR_ACTION_FILE": nestor.ACTION_FILENAME,
             "NESTOR_POLYGON_FILE": nestor.POLYGON_FILENAME,
             "NESTOR_SURFACE_REFERENCE_FILE": nestor.REFERENCE_FILENAME},
            nestor.files(value["actions"], reference=value["reference"],
                         origin=value["origin"]))


def _classes(gradation: Any, presets: Mapping[str, Any]
             ) -> list[tuple[float, float]] | None:
    """A gradation -> clean fine-to-coarse ``(d50_um, fraction)`` pairs, the
    fractions renormalized; ``None`` where fewer than two classes came."""
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
    """A diameter inside the window the formulae are authored for, or a refusal."""
    if not (GRAIN_UM_MIN <= micron <= GRAIN_UM_MAX):
        raise ValueError(
            f"a {micron:g} um class is outside the {GRAIN_UM_MIN:g}-{GRAIN_UM_MAX:g} "
            "um window GAIA's transport formulae are authored for.")
    return micron


def _bed(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The gradation -> the CLASSES lists a mixture IS, or no keyword at all.

    A gradation that resolves to fewer than two classes is not a mixture, and the
    single class the deck states stands."""
    classes = _classes(value["gradation"], value["presets"])
    if not classes:
        return ({"CLASSES_TYPE_OF_SEDIMENT": ["NCO"]}, {})
    return ({"CLASSES_TYPE_OF_SEDIMENT": ["NCO" for _ in classes],
             # The preset vocabulary is in the microns a grading is published in,
             # which is where the conversion to the keyword's metres belongs.
             "CLASSES_SEDIMENT_DIAMETERS": [_metres(um) for um, _ in classes],
             "CLASSES_INITIAL_FRACTION": [fraction for _, fraction in classes]},
            {})


def _suspension(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                   Mapping[str, Any]]:
    """One class, suspension armed, no stock to erode, the source loaded.

    The sorption terms downstream are defined over kg/m3, which is what the
    keyword reads; the question is asked in mg/L, so it converts here."""
    return ({"CLASSES_TYPE_OF_SEDIMENT": ["NCO"],
             "SUSPENSION_FOR_ALL_SANDS": True,
             "LAYERS_INITIAL_THICKNESS": [0.0],
             "SUSPENDED_SEDIMENTS_CONCENTRATION_VALUES_AT_THE_SOURCES": [
                 max(float(value["concentration_mgl"]) * _MGL_TO_KGM3, 0.0)]}, {})


def _metres(micron: Any) -> float:
    """A diameter stated in the micron a grading is published in, as GAIA's metres."""
    return float(micron) * 1.0e-6


def _body(slots: Mapping[str, Any]) -> Mapping[str, Any]:
    """One coupled body, as the carrier's ``coupling`` composite reads it."""
    return {"module": "gaia", "steering": STEERING_FILENAME,
            "slots": dict(slots)}


def _appended(body: Mapping[str, Any]) -> tuple[Output, ...]:
    """The tracers this coupled body puts on its carrier's result: one per
    suspended class, and none at all over a bed the carrier only erodes."""
    return (_SUSPENDED,) if "suspension" in dict(body.get("slots") or {}) else ()


#: The row whose ZERO is itself an answer, and the row the engine writes the
#: stress that would have moved it into. A cumulative bed evolution of exactly
#: nothing everywhere says the bed did not move; what it has to be read against
#: is the shear that was on it.
_BED_EVOLUTION = "E"
_BED_SHEAR = "TOB"


def _bed_field(primitive: Any, solved: Any) -> Any:
    """GAIA's field read, with the bed's ZERO stated where it did not move."""
    read = PRIMITIVES["field"](primitive, solved)
    if primitive.variable == _BED_EVOLUTION:
        _state_the_zero(read, solved)
    return read


def _state_the_zero(read: Any, solved: Any) -> None:
    """What the run SAYS when its bed did not move at all.

    An answer of exactly nothing is only readable beside what drove it, so the
    engine's OWN bed shear stress is quoted with it: the threshold of motion for
    the grain this deck states is a value of that quantity, and a reader holding
    both can see that nothing reached it."""
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
    """The engine's own driving stress, as far as this result carries one."""
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
#: The result the primitives read: GAIA writes its own file beside the carrier's.
GAIA.RESULT_FILE = RESULT_FILENAME
GAIA.composites(bed=_bed, suspension=_suspension, dredging=_dredging)
GAIA.appends(_appended)
GAIA.reads(**{**PRIMITIVES, "field": _bed_field})
