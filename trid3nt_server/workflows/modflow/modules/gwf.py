"""The MODFLOW groundwater-flow wrapper: its dictionary and what a solved run writes."""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Any, Mapping

from ...runtime.module import MOMENT, Output, SlotRefused
from ..authoring.stress import GIVERS
from ..errors import ModflowError
from .module import (_LAKE_FLUXES, _LAKE_NUMBERS, _LAKES, _MAW, _MAW_NUMBERS, _MAW_SKIN, _STREAM_NUMBERS, _STREAMS,
                     _UZF, _UZF_ET, _INTERBED, _INTERBED_DELAY, ModflowModule, _diversions, _interbeds, _lakes, _movers,
                     _series, _start_date_time, _streams, _stress, _uzf, _wells, periods)
from .outputs import PRIMITIVES

__all__ = ["COUPLED_BODIES", "GWF", "MODULE_OUTPUT"]

# What the module writes, read off the head and budget files. A discharge component reads about zero, so its ramp diverges; saturation is a fraction.
_LEVEL = {"kind": "mesh", "ramp": "blues"}
_SATURATION = {"kind": "mesh", "ramp": "ylgnbu", "floor": 0}
_SIGNED = {"kind": "mesh", "ramp": "rdbu", "center": 0.0}
_COMPACTION = {"kind": "mesh", "ramp": "oranges"}

# Units stay empty: mf6 carries no unit system, and a run's lengths and rates are the units the modeller declares.
MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({
    "HEAD": Output("head", "", style=_LEVEL),
    "SATURATION": Output("saturation", "", style=_SATURATION),
    "SPECIFIC_DISCHARGE": Output("specific discharge", "", style=_SIGNED),
    "COMPACTION": Output("compaction", "", style=_COMPACTION),
    "SUBSIDENCE": Output("subsidence", "", style=_SIGNED),
})

# Each stress composite: its package and the arguments it takes with their dimensions, in the package's own column order after the cell id.
_STRESS = {
    "Drains": ("DRN", (("elevation", "L"), ("conductance", "L2/T"))),
    "GeneralHead": ("GHB", (("head", "L"), ("conductance", "L2/T"))),
    "Rivers": ("RIV", (("stage", "L"), ("conductance", "L2/T"), ("rbot", "L"))),
    "ConstantHead": ("CHD", (("head", "L"),)),
    "Wells": ("WEL", (("rate", "L3/T"),)),
    "Recharge": ("RCH", (("rate", "L/T"),)),
}


_AQUIFER = ("thickness", "saturated_thickness", "water_table", "water_table_depth", "conductivity",
            "transmissivity", "layers")


def _layered_source(value: Any) -> bool:
    """Whether a value states a layered source (a mapping of layers and surfaces) rather than one raster artifact."""
    return isinstance(value, Mapping) and bool({"layers", "surfaces"} & set(value))


# The layer a frame is of, read off the frame id each fetched row states: K frames ``-l{n}-``, surface frames ``-top-l1-`` (the top of layer 1, surface 0) and ``-bot-l{n}-``.
_K_FRAME = re.compile(r"-l(\d+)-")
_SURFACE_FRAME = re.compile(r"-(?:top-l(1)|bot-l(\d+))-")


def _frame_id(frame: Any) -> str:
    return str((frame.get("layer_id") if isinstance(frame, Mapping) else getattr(frame, "layer_id", None)) or "")


def _numbered(frames: Any, pattern: re.Pattern, *, name: str, top: bool = False) -> dict[int, Any]:
    """A frame list by the layer number each frame's id states (the top of layer 1 is 0 when ``top``), or a refusal naming the frame."""
    if not isinstance(frames, (list, tuple)):
        raise ModflowError(
            f"Aquifer conductivity by depth reads {name!r} as a list of frames; it holds {type(frames).__name__}.",
            error_code="MODFLOW_AQUIFER_UNSTATED")
    found: dict[int, Any] = {}
    for frame in frames:
        hit = pattern.search(_frame_id(frame))
        if hit is None:
            raise ModflowError(
                f"Aquifer conductivity by depth pairs {name!r} by layer, and the frame {_frame_id(frame)!r} "
                "states no layer in its id.", error_code="MODFLOW_AQUIFER_UNSTATED")
        number = 0 if top and hit.group(1) else int(hit.group(2) if top else hit.group(1))
        found[number] = frame
    return found


def _paired(layers: Any, surfaces: Any, *, names: tuple[str, str]) -> Mapping[str, Any]:
    """The frames of a K row and a surfaces row as ``layers`` and ``surfaces``, paired by layer id.

    Layers run 1 to the deepest K frame; a layer or surface with no frame is NaN, unmeasured, and weighs zero."""
    k = _numbered(layers, _K_FRAME, name=names[0])
    s = _numbered(surfaces, _SURFACE_FRAME, name=names[1], top=True)
    if not k:
        raise ModflowError(
            f"Aquifer conductivity by depth reads {names[0]!r}, which holds no frame.",
            error_code="MODFLOW_AQUIFER_UNSTATED")
    deepest = max(k)
    unmeasured = float("nan")
    return {"layers": [k.get(n, unmeasured) for n in range(1, deepest + 1)],
            "surfaces": [s.get(n, unmeasured) for n in range(deepest + 1)]}


def _unstated(item: Any, run: Mapping[str, Any]) -> bool:
    return item is None or (isinstance(item, str) and run.get(item) is None)


def _by_depth(source: Mapping[str, Any], run: Mapping[str, Any]) -> Mapping[str, Any]:
    """A layered source's statement with its input names read: ``layers`` and ``surfaces``, m and m + 1 long, or a refusal naming them.

    An input name that holds a whole list of frames is paired by layer id."""
    named = [source.get(key) for key in ("layers", "surfaces")]
    if all(isinstance(item, str) and isinstance(run.get(item), (list, tuple)) for item in named):
        return _paired(run[named[0]], run[named[1]], names=(named[0], named[1]))
    missing = [key for key in ("layers", "surfaces") if source.get(key) is None]
    extra = sorted(set(source) - {"layers", "surfaces"})
    if missing or extra:
        raise ModflowError(
            "Aquifer conductivity by depth takes layers and surfaces; "
            + (f"{', '.join(missing)} is not stated" if missing else f"{', '.join(extra)} is neither")
            + ".", error_code="MODFLOW_AQUIFER_UNSTATED")
    layers, surfaces = source["layers"], source["surfaces"]
    if not isinstance(layers, (list, tuple)) or not isinstance(surfaces, (list, tuple)) or not layers \
            or len(surfaces) != len(layers) + 1:
        raise ModflowError(
            f"Aquifer conductivity by depth lists "
            f"{len(layers) if isinstance(layers, (list, tuple)) else 'no'} layer(s) and "
            f"{len(surfaces) if isinstance(surfaces, (list, tuple)) else 'no'} surface(s); m layers "
            "need m + 1 surfaces, the top of layer 1 then each layer's bottom.",
            error_code="MODFLOW_AQUIFER_UNSTATED")

    def read(item: Any) -> Any:
        if not isinstance(item, str):
            return item
        if run.get(item) is None:
            raise ModflowError(
                f"Aquifer conductivity by depth names {item!r}, which this run states no value for.",
                error_code="MODFLOW_AQUIFER_UNSTATED")
        return run[item]

    return {"layers": [read(item) for item in layers], "surfaces": [read(item) for item in surfaces]}


def _aquifer(value: Any, *, run: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """An aquifer's cells: Aquifer(thickness | saturated_thickness, water_table | water_table_depth, conductivity | transmissivity [, layers]).

    ``thickness`` runs down from the land surface, ``saturated_thickness`` down from the water table; the
    bottom lies under whichever, the starting head a water-table depth below the top or the ``water_table``
    elevation itself (on the run's vertical frame; a saturated thickness then lies under that elevation),
    and the conductivity is stated or is the transmissivity over the thickness. ``layers`` (an integer from 1, unstated one)
    splits the saturated thickness into that many equal layers: only layer 1 is convertible (unconfined),
    and the starting head is the water-table elevation in every layer. K is the same in each layer unless
    ``conductivity`` is a list of one number or raster per layer, top first, as long as ``layers``, or
    ``{"layers": [K_1..K_m], "surfaces": [S_0..S_m]}`` (each name an input holding a list of frames is paired by layer id): a layered source read by depth, each layer's K the
    thickness-weighted mean of the source layers it overlaps (m conductivities, m + 1 surfaces - the top of
    source layer 1 then each layer's bottom - on the run's vertical frame; a layer outside the source refuses)."""
    if not isinstance(value, Mapping):
        raise ModflowError(
            f"Aquifer takes a mapping of {', '.join(_AQUIFER)}; got {value!r}.",
            error_code="MODFLOW_AQUIFER_UNSTATED")
    unknown = sorted(set(value) - set(_AQUIFER))
    if unknown:
        raise ModflowError(
            f"Aquifer takes {', '.join(_AQUIFER)}; {', '.join(unknown)} is none of them.",
            error_code="MODFLOW_AQUIFER_UNSTATED")
    whole, saturated = value.get("thickness"), value.get("saturated_thickness")
    if (whole is None) == (saturated is None):
        raise ModflowError(
            "Aquifer states "
            + ("both thickness and saturated_thickness; state one" if whole is not None
               else "neither thickness nor saturated_thickness; state one")
            + " - from the land surface, or down from the water table.",
            error_code="MODFLOW_AQUIFER_UNSTATED")
    elevation, depth = value.get("water_table"), value.get("water_table_depth")
    if (elevation is None) == (depth is None):
        raise ModflowError(
            "Aquifer states "
            + ("both water_table and water_table_depth; state one" if elevation is not None
               else "neither water_table nor water_table_depth; the engine has no value for it, "
                    "so state one from a measurement or a named source")
            + " - an elevation, or a depth below the land surface.",
            error_code="MODFLOW_AQUIFER_UNSTATED")
    conductivity, transmissivity = value.get("conductivity"), value.get("transmissivity")
    if _layered_source(conductivity) and all(_unstated(conductivity.get(key), run) for key in ("layers", "surfaces")):
        # A by-depth source the run holds nothing for is no conductivity: the transmissivity states the aquifer.
        conductivity = None
    if (conductivity is None) == (transmissivity is None):
        raise ModflowError(
            "Aquifer states "
            + ("both conductivity and transmissivity; state one" if conductivity is not None
               else "neither conductivity nor transmissivity; state one")
            + ", the other follows from the thickness.",
            error_code="MODFLOW_AQUIFER_UNSTATED")
    layers = value.get("layers")
    if layers is not None and (isinstance(layers, bool) or not isinstance(layers, int)
                               or layers < 1):
        raise ModflowError(
            f"Aquifer layers is {layers!r}; it is an integer from 1.",
            error_code="MODFLOW_AQUIFER_UNSTATED")
    nlay = layers if layers is not None else 1
    if isinstance(transmissivity, (list, tuple)) or _layered_source(transmissivity):
        raise ModflowError(
            "Aquifer transmissivity is one value over the whole thickness; a list of "
            "values per layer is a conductivity, so state conductivity as that list.",
            error_code="MODFLOW_AQUIFER_UNSTATED")
    if isinstance(conductivity, (list, tuple)) and len(conductivity) != nlay:
        raise ModflowError(
            f"Aquifer conductivity lists {len(conductivity)} value(s) and layers is {nlay}; "
            "state one conductivity per layer, top first.",
            error_code="MODFLOW_AQUIFER_UNSTATED")
    by_depth = _layered_source(conductivity)
    if by_depth:
        conductivity = _by_depth(conductivity, run)
    stated = ({"cells": ("by_depth" if by_depth
                         else "layers" if isinstance(conductivity, (list, tuple)) else "raster"),
               "of": conductivity} if conductivity is not None
              else {"cells": "quotient", "of": [transmissivity, saturated if whole is None else whole]})
    if elevation is not None:
        bottom = ({"cells": "below_top", "of": whole} if saturated is None
                  else {"cells": "elevation", "of": [elevation, saturated]})
        head = {"cells": "elevation", "of": elevation}
    else:
        bottom = {"cells": "below_top", "of": whole if saturated is None else [depth, saturated]}
        head = {"cells": "below_top", "of": depth}
    slots: dict[str, Any] = {"DISV_NLAY": nlay, "NPF_K": stated, "DISV_BOTM": bottom, "IC_STRT": head}
    if nlay > 1:
        # Only the top layer is convertible (unconfined); the layers below it are confined.
        slots["NPF_ICELLTYPE"] = [1] + [0] * (nlay - 1)
    return slots, {}


# The stress statements a coupled body states, by its module word: each package word to the composite the wrapper holds for it.
_COUPLED_STRESS = {
    "gwt": {"CNC": "ConstantConcentration", "SRC": "MassSource"},
    "gwe": {"CTP": "ConstantTemperature", "ESL": "EnergySource"},
}

# The simulation's own clock and grid: one TDIS and one staged DISV describe the whole simulation, and the carrier states them.
_SIMULATION_OWNED = ("TDIS", "DISV")


def _coupled(module: str, name: str):
    stress = _COUPLED_STRESS[module]

    def expand(value: Any, *, run: Mapping[str, Any]
               ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        from . import wrapper_for

        if not isinstance(value, Mapping):
            raise SlotRefused(
                f"{name} takes a mapping with 'given' naming the input(s) that carry "
                f"the {module} body and 'slots' stating its {module.upper()} keywords; "
                f"got {type(value).__name__}.")
        given = value.get("given")
        if isinstance(given, str):
            given = (given,)
        given = tuple(given or ())
        if given and not any((run.get(item) if isinstance(item, str) else item)
                             is not None for item in given):
            return ({}, {})
        stated = value.get("slots")
        if stated is not None and not isinstance(stated, Mapping):
            raise SlotRefused(
                f"{name} slots takes a mapping of {module.upper()} identifiers to "
                f"values; got {type(stated).__name__}.")
        wrapper = wrapper_for(module)
        slots: dict[str, Any] = {}
        for key, item in (stated or {}).items():
            prefix = str(key).split("_", 1)[0]
            if prefix in _SIMULATION_OWNED:
                raise SlotRefused(
                    f"{key} states the simulation's own "
                    f"{'clock' if prefix == 'TDIS' else 'grid'}, which the carrier's deck "
                    "owns: one TDIS and the staged DISV describe the simulation, and the "
                    f"{module} body describes its own packages.")
            if item is None:
                continue
            slots[str(key)] = wrapper.slot(key).check(item)
        for key, item in list(slots.items()):
            if not key.endswith("_STRESS_PERIOD_DATA") or not isinstance(item, Mapping) \
                    or "stress" in item:
                continue
            composite = stress.get(key.split("_", 1)[0])
            if composite is not None:
                slots.update(wrapper.COMPOSITES[composite].apply(item, run)[0])
        return ({}, {module: {"module": module, "slots": slots, "given": list(given)}})

    expand.__doc__ = (
        f"The {module} body this run's flow couples: its module word, slots and given inputs.\n\n"
        "    ``given`` names the inputs that carry the body; a body whose given inputs all hold\n"
        f"    nothing states nothing. Each slot is a {module.upper()} dictionary identifier - an unknown one\n"
        "    refuses by name - and a stress statement is the wrapper's own composite's shape.")
    return expand


GWF = ModflowModule("gwf")
GWF.MODULE_OUTPUT = MODULE_OUTPUT
# The clock and grid family, which identify_on resolves as the carrier's wherever a bare name of it is spelled - the same families _coupled refuses on a body.
GWF.CARRIER_OWNED = tuple(f"{prefix}_" for prefix in _SIMULATION_OWNED)
# The simulation's own clock on every body: Periods spells the marching, the moment the instant it opens at.
# The until/steps form reads those two run inputs by name; the list form states TDIS rows directly.
GWF.composites(reads={"Periods": ("until", "steps")}, Periods=periods)
GWF.FILLED_BY = MappingProxyType({MOMENT: {"TDIS_START_DATE_TIME": _start_date_time}})
GWF.composites(
    reads={name: ("geometry", *(column for column, _ in columns), "concentration")
           for name, (_, columns) in _STRESS.items()},
    **{name: _stress(name, package, dict(columns), carries=True, moves=package in GIVERS)
       for name, (package, columns) in _STRESS.items()})
GWF.composites(reads={"Streams": ("geometry", "cross_section", *_STREAMS, *_STREAM_NUMBERS)}, Streams=_streams)
GWF.composites(reads={"Lakes": ("geometry", "table", *_LAKES, *_LAKE_FLUXES, *_LAKE_NUMBERS)}, Lakes=_lakes)
GWF.composites(reads={"WaterMover": ("from_at", "to_at")}, WaterMover=_movers)
GWF.composites(reads={"Diversions": ("from_at", "to_at")}, Diversions=_diversions)
GWF.composites(reads={"MultiAquiferWells": ("geometry", *(name for name in _MAW if name != "condeqn"), *_MAW_SKIN,
                                            *_MAW_NUMBERS)},
              MultiAquiferWells=_wells)
GWF.composites(reads={"UnsaturatedZone": ("geometry", *_UZF, *_UZF_ET)}, UnsaturatedZone=_uzf)
GWF.composites(reads={"Interbeds": ("geometry", *_INTERBED, *_INTERBED_DELAY)}, Interbeds=_interbeds)
GWF.composites(reads={"TimeSeries": ("rows",)}, TimeSeries=_series)
GWF.composites(reads={"Aquifer": (*_AQUIFER, "surfaces")}, Aquifer=_aquifer)
# Which coupled body each coupling composite states, by module word: the one statement the registration and the run's bodies both read.
COUPLED_BODIES = MappingProxyType({"Transport": "gwt", "Energy": "gwe"})
GWF.composites(**{name: _coupled(module, name)
                  for name, module in COUPLED_BODIES.items()})
GWF.reads(**PRIMITIVES)
