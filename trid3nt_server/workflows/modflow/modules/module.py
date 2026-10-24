"""The MODFLOW wrapper over the engine-neutral module surface.

Where the dictionary lives and which bodies the engine exposes; the fill holds
this dictionary, and mf6's own writer judges what the authoring writes. The one
composite every body shares is ``Periods``: the simulation's own clock, spelled
in TDIS.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ..errors import ModflowError
from ...runtime.module import MOMENT, Module, SlotRefused
from trid3nt_server.inputs.temporal import Series

__all__ = ["ModflowModule", "module_input_dir", "periods"]


# A flow stress may carry the concentration of the water it moves: the package's AUXILIARY column the transport model's SSM reads.
_AUX = ("concentration", "CONCENTRATION", "M/L3")


def _stress(name: str, package: str, dimensions: Mapping[str, str], *, carries: bool = False, moves: bool = False):
    columns = tuple(dimensions)

    def expand(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        if isinstance(value, (list, tuple)) and value:
            expanded = [slots for item in value if (slots := expand(item)[0])]
            stated = [slots[f"{package}_STRESS_PERIOD_DATA"] for slots in expanded]
            carrying = [f"{package}_AUXILIARY" in slots for slots in expanded]
            if any(carrying) and not all(carrying):
                raise ModflowError(
                    f"{name} states {_AUX[0]} on some statements and not others; the "
                    f"column is on every cell of the package, so state it on each statement.",
                    error_code="MODFLOW_STRESS_UNSTATED")
            return ({f"{package}_STRESS_PERIOD_DATA": {"stress": package, "statements": stated},
                     **({f"{package}_AUXILIARY": [_AUX[1]]} if all(carrying) else {})}
                    if stated else {}), {}
        if not isinstance(value, Mapping):
            raise ModflowError(
                f"{name} takes a mapping of geometry, layer and {', '.join(columns)}, or a list of them; "
                f"got {value!r}.", error_code="MODFLOW_STRESS_UNSTATED")
        if value.get("geometry") is None:
            return {}, {}
        for argument in ("layer", *columns):
            if value.get(argument) is None:
                raise ModflowError(
                    f"{name} states no {argument}; the engine has no value for it, "
                    "so state it from a measurement or a named source.",
                    error_code="MODFLOW_STRESS_UNSTATED")
        layer = value["layer"]
        if layer != "all" and (isinstance(layer, bool) or not isinstance(layer, int) or layer < 1):
            raise ModflowError(
                f"{name} layer is {layer!r}; it is an integer from 1, or \"all\".",
                error_code="MODFLOW_STRESS_UNSTATED")
        aux = {_AUX[0]: _AUX[2]} if carries and value.get(_AUX[0]) is not None else {}
        taken = {**dimensions, **aux}
        return {f"{package}_STRESS_PERIOD_DATA": {
            "stress": package, "composite": name, "geometry": value["geometry"],
            "values": {column: value[column] for column in taken},
            "dimensions": dict(taken), "layer": layer, **({"name": _given(value, name)} if moves else {})},
            **({f"{package}_AUXILIARY": [_AUX[1]]} if aux else {})}, {}

    expand.__doc__ = (
        f"The {package} package's cells a geometry places: {name}(geometry, layer, "
        f"{', '.join(columns)}{f', [{_AUX[0]}]' if carries else ''}{', [name]' if moves else ''}), or a list of such statements; a geometry reading nothing states nothing."
        + (f"\n\n    name is what WaterMover calls each cell row this statement places; the {package} package gives water to the mover, never takes it." if moves else ""))
    return expand


_STATUS = ("ACTIVE", "INACTIVE", "CONSTANT")


def _by_period(held: Any) -> bool:
    """Whether a value is keyed by period (every key a period number) and not a raster's mapping."""
    return isinstance(held, Mapping) and bool(held) and all(
        isinstance(key, int) and not isinstance(key, bool) or isinstance(key, str) and key.strip().isdigit()
        for key in held)


def _timed(held: Any, *, composite: str, argument: str, words: tuple[str, ...] = (), number: bool = True) -> Any:
    """A period setting as stated -> its value (from period 1) or ``{period index: value}``; a mapping is keyed by period from 1."""
    if isinstance(held, Mapping):
        stated: dict[int, Any] = {}
        for period, item in held.items():
            index = period if isinstance(period, int) and not isinstance(period, bool) \
                else int(period) if isinstance(period, str) and period.strip().isdigit() else 0
            if index < 1 or index - 1 in stated:
                raise ModflowError(
                    f"{composite} {argument} is keyed by period, each an integer from 1 once; got {period!r}.",
                    error_code="MODFLOW_STRESS_UNSTATED")
            stated[index - 1] = _setting(item, composite=composite, argument=argument, words=words, number=number)
        if not stated:
            raise ModflowError(f"{composite} {argument} is an empty mapping; state a period and its value.",
                               error_code="MODFLOW_STRESS_UNSTATED")
        return stated
    return _setting(held, composite=composite, argument=argument, words=words, number=number)


def _setting(item: Any, *, composite: str, argument: str, words: tuple[str, ...], number: bool) -> Any:
    if isinstance(item, str) and item.strip().upper() in words:
        return item.strip().upper()
    if number and isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item):
        return item
    expected = " or ".join(part for part in ("a number" if number else "", ", ".join(words)) if part)
    raise ModflowError(
        f"{composite} {argument} is {expected}, from period 1 or keyed by period; got {item!r}.",
        error_code="MODFLOW_STRESS_UNSTATED")


def _settings(value: Mapping[str, Any], composite: str, *, numbers: tuple[str, ...],
              words: Mapping[str, tuple[tuple[str, ...], bool]]) -> dict[str, Any]:
    """The optional period settings a statement states, validated, by their mf6 names; the auxiliary values by name."""
    stated = {argument: _timed(value[argument], composite=composite, argument=argument)
              for argument in numbers if value.get(argument) is not None}
    stated.update({argument: _timed(value[argument], composite=composite, argument=argument,
                                    words=choices, number=allowed)
                   for argument, (choices, allowed) in words.items() if value.get(argument) is not None})
    aux = value.get("auxiliary")
    if aux is not None:
        if not isinstance(aux, Mapping) or not aux or not all(isinstance(name, str) for name in aux):
            raise ModflowError(
                f"{composite} auxiliary is a mapping of an auxiliary name to its value; got {aux!r}.",
                error_code="MODFLOW_STRESS_UNSTATED")
        stated["auxiliary"] = {name: _timed(held, composite=composite, argument=f"auxiliary {name}")
                               for name, held in aux.items()}
    return stated


def _unknown(value: Mapping[str, Any], composite: str, takes: tuple[str, ...]) -> None:
    unknown = sorted(set(value) - set(takes))
    if unknown:
        raise ModflowError(
            f"{composite} takes {', '.join(takes)}; {', '.join(unknown)} is none of them.",
            error_code="MODFLOW_STRESS_UNSTATED")


def _given(value: Mapping[str, Any], composite: str) -> str | None:
    """The optional name a statement gives its features, or None."""
    name = value.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ModflowError(f"{composite} name is a non-empty string; got {name!r}.", error_code="MODFLOW_STRESS_UNSTATED")
    return None if name is None else name.strip()


def _table(held: Any, *, composite: str, argument: str, widths: tuple[int, ...]) -> list[list[float]]:
    """A table stated as rows of numbers, every row one of ``widths`` columns, as stated or read off a run input."""
    if not isinstance(held, (list, tuple)) or not held or not all(
            isinstance(row, (list, tuple)) and len(row) == len(held[0]) and len(row) in widths
            and all(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) for item in row)
            for row in held):
        raise ModflowError(
            f"{composite} {argument} is a table of rows of {' or '.join(map(str, widths))} numbers, every row the same "
            f"length (a raster is no table); got {held!r}.", error_code="MODFLOW_STRESS_UNSTATED")
    return [[float(item) for item in row] for row in held]


_STREAMS = {"width": "L", "slope": "1", "bed_top": "L", "bed_thickness": "L",
            "bed_conductivity": "L/T", "manning": "1"}
_STREAM_NUMBERS = ("bedk", "stage", "inflow", "rainfall", "evaporation", "runoff", "upstream_fraction")
_STREAM_WORDS = {"status": (("ACTIVE", "INACTIVE", "SIMPLE"), False)}


def _streams(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if isinstance(value, (list, tuple)) and value:
        stated = [item for each in value if (item := _streams(each)[0])]
        return ({"SFR_PACKAGEDATA": {"streams": [row["SFR_PACKAGEDATA"]["streams"][0]
                                                 for row in stated]}} if stated else {}), {}
    if not isinstance(value, Mapping):
        raise ModflowError(
            "Streams takes a mapping of geometry, "
            f"{', '.join(_STREAMS)} and an optional layer, or a list of them; got {value!r}.",
            error_code="MODFLOW_STRESS_UNSTATED")
    _unknown(value, "Streams", ("geometry", "layer", "name", "cross_section", *_STREAMS, *_STREAM_NUMBERS,
                                *_STREAM_WORDS, "auxiliary"))
    if value.get("geometry") is None:
        return {}, {}
    for argument in _STREAMS:
        if value.get(argument) is None:
            raise ModflowError(
                f"Streams states no {argument}; the engine has no value for it, "
                "so state it from a measurement or a named source.",
                error_code="MODFLOW_STRESS_UNSTATED")
    layer = value.get("layer")
    if layer is not None and (isinstance(layer, bool) or not isinstance(layer, int) or layer < 1):
        raise ModflowError(f"Streams layer is {layer!r}; it is an integer from 1.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    manning = value["manning"]
    if isinstance(manning, bool) or not isinstance(manning, (int, float, Mapping)):
        raise ModflowError(
            "Streams manning is a number in the units the SFR conversions state, or keyed by period; "
            "a raster carries no unit this frame converts.", error_code="MODFLOW_STRESS_UNSTATED")
    settings = _settings(value, "Streams", numbers=_STREAM_NUMBERS, words=_STREAM_WORDS)
    values = {column: value[column] for column in _STREAMS}
    if isinstance(manning, Mapping):
        settings["manning"] = _timed(manning, composite="Streams", argument="manning")
        if 0 not in settings["manning"]:
            raise ModflowError(
                "Streams manning keyed by period states period 1, the reach's own roughness.",
                error_code="MODFLOW_STRESS_UNSTATED")
        values["manning"] = settings["manning"][0]
    return {"SFR_PACKAGEDATA": {"streams": [{
        "composite": "Streams", "geometry": value["geometry"], "layer": layer, "name": _given(value, "Streams"),
        "cross_section": None if value.get("cross_section") is None else _table(
            value["cross_section"], composite="Streams", argument="cross_section", widths=(2, 3)),
        "values": values, "dimensions": dict(_STREAMS), "settings": settings}]}}, {}


_streams.__doc__ = (
    "The SFR reaches a line geometry places: Streams(geometry, width, slope, bed_top, bed_thickness, "
    "bed_conductivity, manning[, layer, name, cross_section, status, bedk, stage, inflow, rainfall, evaporation, "
    "runoff, upstream_fraction, auxiliary]), or a list of such statements.\n\n"
    "    The line is cut by the cells it crosses in its digitized order, downstream; each\n"
    "    reach lies on the cell's top solved layer unless layer states one. A setting is mf6's own SFR\n"
    "    period setting: a number from period 1 or {period from 1: value}, written to every reach of the\n"
    "    line, inflow to the line's upstream reach; status is ACTIVE, INACTIVE or SIMPLE, manning keyed\n"
    "    by period names period 1 as the reach's own, auxiliary is {name: value} of the SFR_AUXILIARY names.\n"
    "    name is what WaterMover and Diversions call the line's reaches; cross_section is the rows (xfraction, height\n"
    "    [, manfraction]) of the reach cross-section table every reach of the line reads.")


_LAKES = ("stage", "bottom", "leakance")
_LAKE_FLUXES = ("rainfall", "evaporation")
_LAKE_NUMBERS = ("runoff", "inflow", "withdrawal")
_LAKE_WORDS = {"status": (_STATUS, False)}
_OUTLETS = ("to", "couttype", "rate", "invert", "width", "rough", "slope")
# The outlet values each outlet type reads; mf6 takes any value in a column its type does not read.
_COUTTYPE = {"SPECIFIED": ("rate",), "MANNING": ("invert", "width", "rough", "slope"), "WEIR": ("invert", "width")}


def _outlets(held: Any) -> list[dict[str, Any]]:
    if not isinstance(held, (list, tuple)) or not held or not all(isinstance(each, Mapping) for each in held):
        raise ModflowError(f"Lakes outlets is a list of mappings of {', '.join(_OUTLETS)}; got {held!r}.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    outlets = []
    for each in held:
        _unknown(each, "Lakes outlet", _OUTLETS)
        couttype = str(each.get("couttype") or "").strip().upper()
        if couttype not in _COUTTYPE:
            raise ModflowError(
                f"Lakes outlet couttype is {each.get('couttype')!r}; it is one of {', '.join(_COUTTYPE)}.",
                error_code="MODFLOW_STRESS_UNSTATED")
        to = each.get("to")
        if isinstance(to, str) and to.strip().lower() == "outside":
            to = 0
        if isinstance(to, bool) or not isinstance(to, int) or to < 0:
            raise ModflowError(
                f"Lakes outlet to is the number of the lake it discharges into (from 1, the order "
                f"the lakes are stated in) or \"outside\"; got {each.get('to')!r}.",
                error_code="MODFLOW_STRESS_UNSTATED")
        read = _COUTTYPE[couttype]
        outlet: dict[str, Any] = {"couttype": couttype, "to": to}
        for argument in ("rate", "invert", "width", "rough", "slope"):
            held = each.get(argument)
            if argument in read and held is None:
                raise ModflowError(
                    f"Lakes outlet {couttype} states no {argument}; the engine has no value for it, "
                    "so state it from a measurement or a named source.", error_code="MODFLOW_STRESS_UNSTATED")
            if argument not in read and held is not None:
                raise ModflowError(
                    f"Lakes outlet {couttype} reads no {argument}; remove it or state another couttype.",
                    error_code="MODFLOW_STRESS_UNSTATED")
            if held is not None:
                outlet[argument] = _timed(held, composite="Lakes", argument=f"outlet {argument}")
                if isinstance(outlet[argument], dict) and 0 not in outlet[argument]:
                    raise ModflowError(
                        f"Lakes outlet {argument} keyed by period states period 1, the outlet's first value.",
                        error_code="MODFLOW_STRESS_UNSTATED")
        outlets.append(outlet)
    return outlets


def _lakes(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if isinstance(value, (list, tuple)) and value:
        stated = [item for each in value if (item := _lakes(each)[0])]
        return ({"LAK_PACKAGEDATA": {"lakes": [row["LAK_PACKAGEDATA"]["lakes"][0]
                                               for row in stated]}} if stated else {}), {}
    if not isinstance(value, Mapping):
        raise ModflowError(
            "Lakes takes a mapping of geometry, "
            f"{', '.join(_LAKES)} and optionally {', '.join(_LAKE_FLUXES)}, or a list of them; got {value!r}.",
            error_code="MODFLOW_STRESS_UNSTATED")
    _unknown(value, "Lakes", ("geometry", "name", "table", *_LAKES, *_LAKE_FLUXES, *_LAKE_NUMBERS, *_LAKE_WORDS,
                              "auxiliary", "outlets"))
    if value.get("geometry") is None:
        return {}, {}
    for argument in _LAKES:
        if value.get(argument) is None:
            raise ModflowError(
                f"Lakes states no {argument}; the engine has no value for it, "
                "so state it from a measurement or a named source.",
                error_code="MODFLOW_STRESS_UNSTATED")
    leakance = value["leakance"]
    if isinstance(leakance, bool) or not isinstance(leakance, (int, float)):
        raise ModflowError(
            f"Lakes leakance is one number for the lake; got {leakance!r}.",
            error_code="MODFLOW_STRESS_UNSTATED")
    settings = _settings(value, "Lakes", numbers=(*_LAKE_FLUXES, *_LAKE_NUMBERS), words=_LAKE_WORDS)
    stage = _timed(value["stage"], composite="Lakes", argument="stage")
    if isinstance(stage, dict) and 0 not in stage:
        raise ModflowError("Lakes stage keyed by period states period 1, the lake's starting stage.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    return {"LAK_PACKAGEDATA": {"lakes": [{
        "composite": "Lakes", "geometry": value["geometry"], "name": _given(value, "Lakes"),
        "table": None if value.get("table") is None else _table(
            value["table"], composite="Lakes", argument="table", widths=(3,)),
        "outlets": _outlets(value["outlets"]) if value.get("outlets") is not None else [],
        "values": {"bottom": value["bottom"], "leakance": leakance, **settings, "stage": stage}}]}}, {}


_lakes.__doc__ = (
    "The LAK lakes a polygon geometry places: Lakes(geometry, stage, bottom, leakance[, name, table, rainfall, "
    "evaporation, runoff, inflow, withdrawal, status, auxiliary, outlets]), or a list of such statements.\n\n"
    "    Each polygon is one lake holding the cells whose centres it contains, in every solved layer whose top\n"
    "    lies above the lake's bottom (a number or a raster); those cells are not solved, and the lake connects\n"
    "    to the solved cells beside and below them over lakebed leakance. The stage is the lake's starting stage;\n"
    "    a setting not stated is not in the package. A setting is mf6's own LAK period setting: a number from\n"
    "    period 1 or {period from 1: value}; status is ACTIVE, INACTIVE or CONSTANT, a stage keyed by period names\n"
    "    period 1 as the starting stage and is the stage a CONSTANT lake holds, auxiliary is {name: value} of the\n"
    "    LAK_AUXILIARY names. outlets is a list of {to, couttype, rate | invert, width, rough, slope} leaving\n"
    "    the lake (one polygon): to is the number of the lake it discharges into or \"outside\"; couttype is SPECIFIED\n"
    "    (rate), MANNING (invert, width, rough, slope) or WEIR (invert, width), each value from period 1 or keyed by period.\n"
    "    name is what WaterMover calls the lake; table is the rows (stage, volume, sarea) of the lake's stage-volume-area\n"
    "    table, for a statement holding one polygon.")


_MOVER_TYPES = ("FACTOR", "EXCESS", "THRESHOLD", "UPTO")
_DIVERSION_TYPES = ("FRACTION", "EXCESS", "THRESHOLD", "UPTO")
_FRACTIONS = ("FACTOR", "FRACTION")
_ENDS = ("from", "from_at", "to", "to_at")


def _movement(value: Any, *, composite: str, slot: str, key: str, types: tuple[str, ...], takes: tuple[str, ...]
              ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if isinstance(value, (list, tuple)) and value:
        stated = [item for each in value if (item := _movement(
            each, composite=composite, slot=slot, key=key, types=types, takes=takes)[0])]
        return ({slot: {key: [row[slot][key][0] for row in stated]}} if stated else {}), {}
    if not isinstance(value, Mapping):
        raise ModflowError(
            f"{composite} takes a mapping of {', '.join(_ENDS)}, type and value, or a list of them; got {value!r}.",
            error_code="MODFLOW_STRESS_UNSTATED")
    _unknown(value, composite, (*_ENDS, "type", "value", *takes))
    if not value:
        return {}, {}
    for end in ("from", "to"):
        if value.get(end) is None and value.get(f"{end}_at") is None:
            raise ModflowError(f"{composite} states neither {end} nor {end}_at; state the feature it is.",
                               error_code="MODFLOW_STRESS_UNSTATED")
        if value.get(end) is not None and (not isinstance(value[end], str) or not value[end].strip()):
            raise ModflowError(f"{composite} {end} is the name of a feature; got {value[end]!r}.",
                               error_code="MODFLOW_STRESS_UNSTATED")
    kind = str(value.get("type") or "").strip().upper()
    if kind not in types:
        raise ModflowError(f"{composite} type is {value.get('type')!r}; it is one of {', '.join(types)}.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    if value.get("value") is None:
        raise ModflowError(
            f"{composite} states no value; the engine has no value for it, so state it from a measurement or a "
            "named source.", error_code="MODFLOW_STRESS_UNSTATED")
    held = _timed(value["value"], composite=composite, argument="value")
    stated = held.values() if isinstance(held, dict) else [held]
    if isinstance(held, dict) and 0 not in held:
        raise ModflowError(f"{composite} value keyed by period states period 1, the first value.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    if any(item < 0 or kind in _FRACTIONS and item > 1 for item in stated):
        raise ModflowError(
            f"{composite} value is {'a fraction from 0 to 1' if kind in _FRACTIONS else 'a rate from 0'} for {kind}; "
            f"got {value['value']!r}.", error_code="MODFLOW_STRESS_UNSTATED")
    outlet = value.get("from_outlet")
    if outlet is not None and (isinstance(outlet, bool) or not isinstance(outlet, int) or outlet < 1):
        raise ModflowError(f"{composite} from_outlet is an outlet number from 1; got {outlet!r}.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    return {slot: {key: [{"composite": composite, **{end: value.get(end) for end in _ENDS}, "type": kind,
                          "value": held, "from_outlet": outlet}]}}, {}


def _movers(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    return _movement(value, composite="WaterMover", slot="MVR_PERIODDATA", key="movers", types=_MOVER_TYPES,
                     takes=("from_outlet",))


_movers.__doc__ = (
    "The MVR movers that carry water from one placed feature to another: WaterMover(from, to, type, value[, from_at, "
    "to_at, from_outlet]), or a list of such statements.\n\n"
    "    from and to are the name a Streams, Lakes, MultiAquiferWells or UnsaturatedZone statement gave its features,\n"
    "    and from also the name a Wells, Drains, GeneralHead or Rivers statement gave its cell rows (they give water only);\n"
    "    from_at and to_at a point, read as the cell it lies in, to tell apart features sharing a name or to name a\n"
    "    feature by where it is; each end resolves to exactly one feature. A lake gives water through one of its\n"
    "    outlets (from_outlet from 1, optional when it has one); a stream through its reach, a well, an\n"
    "    UnsaturatedZone column through its land cell's rejected infiltration. type is FACTOR (a fraction of the\n"
    "    provider's water, 0 to 1), EXCESS, THRESHOLD or UPTO (a rate); value is a number from period 1 or\n"
    "    {period from 1: number}, a period it names restating every mover with the value in force.")


def _diversions(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    return _movement(value, composite="Diversions", slot="SFR_DIVERSIONS", key="diversions", types=_DIVERSION_TYPES,
                     takes=())


_diversions.__doc__ = (
    "The SFR diversions that take water from one placed reach to another: Diversions(from, to, type, value[, "
    "from_at, to_at]), or a list of such statements.\n\n"
    "    from and to name Streams reaches as WaterMover does, from_at and to_at a point in the reach's cell. The target\n"
    "    reach must have no upstream reach of its own; it becomes a downstream connection of the source and\n"
    "    receives the diverted water alone. type is FRACTION (of the source's outflow, 0 to 1), EXCESS, THRESHOLD or\n"
    "    UPTO (a rate); value is a number from period 1 or {period from 1: number}.")


_MAW = ("radius", "bottom", "start_head", "condeqn", "screen_top", "screen_bottom")
_MAW_NUMBERS = ("rate", "well_head", "fwelev", "fwcond", "fwrlen", "minrate", "maxrate", "pump_elevation",
                "scaling_length")
_MAW_WORDS = {"status": (_STATUS, False), "head_limit": (("OFF",), True)}
_MAW_SKIN = ("hk_skin", "radius_skin")
# The skin values each conductance equation reads; mf6 takes any value in a column its equation does not read.
_CONDEQN = {"THIEM": (), "SPECIFIED": ("hk_skin",), "SKIN": _MAW_SKIN, "CUMULATIVE": _MAW_SKIN,
            "MEAN": _MAW_SKIN}
_UNREAD = -999.0


def _wells(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if isinstance(value, (list, tuple)) and value:
        stated = [item for each in value if (item := _wells(each)[0])]
        return ({"MAW_PACKAGEDATA": {"wells": [row["MAW_PACKAGEDATA"]["wells"][0]
                                               for row in stated]}} if stated else {}), {}
    if not isinstance(value, Mapping):
        raise ModflowError(
            "MultiAquiferWells takes a mapping of geometry, "
            f"{', '.join(_MAW)}, rate and, by its condeqn, {', '.join(_MAW_SKIN)}, or a list of them; got {value!r}.",
            error_code="MODFLOW_STRESS_UNSTATED")
    _unknown(value, "MultiAquiferWells", ("geometry", "name", *_MAW, *_MAW_SKIN, *_MAW_NUMBERS, *_MAW_WORDS,
                                          "auxiliary"))
    if value.get("geometry") is None:
        return {}, {}
    for argument in _MAW:
        if value.get(argument) is None:
            raise ModflowError(
                f"MultiAquiferWells states no {argument}; the engine has no value for it, "
                "so state it from a measurement or a named source.",
                error_code="MODFLOW_STRESS_UNSTATED")
    for argument in _MAW:
        held = value[argument]
        if argument != "condeqn" and (isinstance(held, bool) or not isinstance(held, (int, float))):
            raise ModflowError(
                f"MultiAquiferWells {argument} is one number for the well; got {held!r}.",
                error_code="MODFLOW_STRESS_UNSTATED")
    condeqn = str(value["condeqn"]).strip().upper()
    if condeqn not in _CONDEQN:
        raise ModflowError(
            f"MultiAquiferWells condeqn is {value['condeqn']!r}; it is one of {', '.join(_CONDEQN)}.",
            error_code="MODFLOW_STRESS_UNSTATED")
    if value["screen_top"] <= value["screen_bottom"]:
        raise ModflowError(
            f"MultiAquiferWells screen runs from {value['screen_bottom']} up to {value['screen_top']}; "
            "its top is above its bottom.", error_code="MODFLOW_STRESS_UNSTATED")
    skin = {}
    for argument in _MAW_SKIN:
        held = value.get(argument)
        if argument not in _CONDEQN[condeqn]:
            if held is not None:
                raise ModflowError(
                    f"MultiAquiferWells {condeqn} reads no {argument}; remove it or state "
                    "another condeqn.", error_code="MODFLOW_STRESS_UNSTATED")
            skin[argument] = _UNREAD
        elif held is None or isinstance(held, bool) or not isinstance(held, (int, float)):
            raise ModflowError(
                f"MultiAquiferWells {condeqn} states {argument} as one number; got {held!r}.",
                error_code="MODFLOW_STRESS_UNSTATED")
        else:
            skin[argument] = held
    settings = _settings(value, "MultiAquiferWells", numbers=_MAW_NUMBERS, words=_MAW_WORDS)
    _rated(settings)
    return {"MAW_PACKAGEDATA": {"wells": [{
        "composite": "MultiAquiferWells", "geometry": value["geometry"], "name": _given(value, "MultiAquiferWells"),
        "values": {**{column: value[column] for column in _MAW if column != "condeqn"}, "condeqn": condeqn,
                   **skin, **settings}}]}}, {}


def _rated(settings: Mapping[str, Any]) -> None:
    """Refuse a well ACTIVE in a period where no rate has been stated yet: mf6 would take zero."""
    status = settings.get("status", {})
    rate = settings.get("rate")
    rate = {} if rate is None else rate if isinstance(rate, dict) else {0: rate}
    status = status if isinstance(status, dict) else {0: status}
    current, rated = "ACTIVE", False
    for period in sorted({0, *status, *rate}):
        current, rated = status.get(period, current), rated or period in rate
        if current == "ACTIVE" and not rated:
            raise ModflowError(
                f"MultiAquiferWells states no rate for period {period + 1}, where the well is ACTIVE; the engine "
                "has no value for it, so state it from a measurement or a named source, or state the well "
                "CONSTANT or INACTIVE.", error_code="MODFLOW_STRESS_UNSTATED")


_wells.__doc__ = (
    "The MAW wells a point geometry places: MultiAquiferWells(geometry, radius, bottom, start_head, condeqn, "
    "screen_top, screen_bottom, rate[, name, hk_skin, radius_skin, status, well_head, head_limit, fwelev, fwcond, "
    "fwrlen, minrate, maxrate, pump_elevation, scaling_length, auxiliary]), or a list of such statements.\n\n"
    "    Each point is one well in the cell holding it, connected to every solved layer its screen crosses;\n"
    "    condeqn names the conductance equation, and the skin values are those it reads (SKIN, CUMULATIVE and\n"
    "    MEAN both, SPECIFIED the conductance as hk_skin, THIEM none). A setting is mf6's own MAW period\n"
    "    setting: a number from period 1 or {period from 1: value}. rate is required wherever the well is ACTIVE\n"
    "    (the default), status is ACTIVE, INACTIVE or CONSTANT, head_limit a number or OFF. fwelev, fwcond and\n"
    "    fwrlen are the FLOWING_WELL record, minrate and maxrate SHUT_OFF, pump_elevation and scaling_length\n"
    "    RATE_SCALING - each record stated whole, in the same periods. auxiliary is {name: value} of the MAW_AUXILIARY names;\n"
    "    name is what WaterMover calls the well.")


_UZF = ("surfdep", "vks", "thtr", "thts", "thti", "eps", "finf")
_UZF_ET = ("pet", "extdp", "extwc", "ha", "hroot", "rootact")


def _uzf(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if isinstance(value, (list, tuple)) and value:
        stated = [item for each in value if (item := _uzf(each)[0])]
        return ({"UZF_PACKAGEDATA": {"zones": [row["UZF_PACKAGEDATA"]["zones"][0]
                                               for row in stated]}} if stated else {}), {}
    if not isinstance(value, Mapping):
        raise ModflowError(
            "UnsaturatedZone takes a mapping of geometry, "
            f"{', '.join(_UZF)} and optionally {', '.join(_UZF_ET)}, or a list of them; got {value!r}.",
            error_code="MODFLOW_STRESS_UNSTATED")
    _unknown(value, "UnsaturatedZone", ("geometry", "name", *_UZF, *_UZF_ET, "auxiliary"))
    if value.get("geometry") is None:
        return {}, {}
    for argument in _UZF:
        if value.get(argument) is None:
            raise ModflowError(
                f"UnsaturatedZone states no {argument}; the engine has no value for it, "
                "so state it from a measurement or a named source.",
                error_code="MODFLOW_STRESS_UNSTATED")
    values = {}
    for argument in (*_UZF, *_UZF_ET):
        held = value.get(argument)
        if argument in ("finf", *_UZF_ET) and _by_period(held):
            held = _timed(held, composite="UnsaturatedZone", argument=argument)
        elif held is not None and (isinstance(held, (bool, str))):
            raise ModflowError(
                f"UnsaturatedZone {argument} is a number or a raster; got {held!r}.",
                error_code="MODFLOW_STRESS_UNSTATED")
        values[argument] = held
    if value.get("auxiliary") is not None:
        values["auxiliary"] = _settings(value, "UnsaturatedZone", numbers=(), words={})["auxiliary"]
    return {"UZF_PACKAGEDATA": {"zones": [{
        "composite": "UnsaturatedZone", "geometry": value["geometry"], "name": _given(value, "UnsaturatedZone"),
        "values": values}]}}, {}


_uzf.__doc__ = (
    "The UZF cells a polygon geometry places: UnsaturatedZone(geometry, surfdep, vks, thtr, thts, thti, eps, finf"
    "[, name, pet, extdp, extwc, ha, hroot, rootact, auxiliary]), or a list of such statements.\n\n"
    "    The top solved cell of every column whose centre the polygon holds is a land-surface UZF cell, and each\n"
    "    solved layer below it down the column a UZF cell connected to the one above; a property is a number or a\n"
    "    raster read at the column's centre and holds for the whole column. finf and the ET values are the land\n"
    "    cell's period settings: a number or raster from period 1, or {period from 1: number}; the ET values are\n"
    "    those the stated UZF ET options read; stated beside none, they refuse. auxiliary is {name: value} of the\n"
    "    UZF_AUXILIARY names, from period 1 or keyed by period. name is what WaterMover calls the land cells.")


_INTERBED = ("pcs0", "thick_frac", "ssv_cc", "sse_cr", "theta")
_INTERBED_DELAY = ("rnb", "kv", "h0")
# The interbed values a raster can state: the ones whose dimension the options do not change.
_INTERBED_RASTER = {"thick_frac": "1", "theta": "1", "rnb": "1", "kv": "L/T", "h0": "L"}
_CDELAY = ("DELAY", "NODELAY")


def _interbeds(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if isinstance(value, (list, tuple)) and value:
        stated = [item for each in value if (item := _interbeds(each)[0])]
        return ({"CSUB_PACKAGEDATA": {"interbeds": [row["CSUB_PACKAGEDATA"]["interbeds"][0]
                                                    for row in stated]}} if stated else {}), {}
    if not isinstance(value, Mapping):
        raise ModflowError(
            "Interbeds takes a mapping of geometry, layer, cdelay, "
            f"{', '.join(_INTERBED)} and, for a DELAY interbed, {', '.join(_INTERBED_DELAY)}, or a list of them; "
            f"got {value!r}.", error_code="MODFLOW_STRESS_UNSTATED")
    _unknown(value, "Interbeds", ("geometry", "layer", "cdelay", "name", *_INTERBED, *_INTERBED_DELAY))
    if value.get("geometry") is None:
        return {}, {}
    cdelay = str(value.get("cdelay") or "").strip().upper()
    if cdelay not in _CDELAY:
        raise ModflowError(f"Interbeds cdelay is {value.get('cdelay')!r}; it is one of {', '.join(_CDELAY)}.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    layer = value.get("layer")
    if layer != "all" and (isinstance(layer, bool) or not isinstance(layer, int) or layer < 1):
        raise ModflowError(f"Interbeds layer is {layer!r}; it is an integer from 1, or \"all\".",
                           error_code="MODFLOW_STRESS_UNSTATED")
    read = _INTERBED if cdelay == "NODELAY" else (*_INTERBED, *_INTERBED_DELAY)
    for argument in (*_INTERBED, *_INTERBED_DELAY):
        held = value.get(argument)
        if argument not in read:
            if held is not None:
                raise ModflowError(
                    f"Interbeds NODELAY reads no {argument}; remove it or state cdelay DELAY.",
                    error_code="MODFLOW_STRESS_UNSTATED")
            continue
        if held is None:
            raise ModflowError(
                f"Interbeds states no {argument}; the engine has no value for it, so state it from a "
                "measurement or a named source.", error_code="MODFLOW_STRESS_UNSTATED")
        if isinstance(held, bool) or (argument not in _INTERBED_RASTER and not isinstance(held, (int, float))):
            raise ModflowError(
                f"Interbeds {argument} is one number: its dimension changes with the CSUB options, so a raster "
                f"carries no unit this frame converts; got {held!r}.", error_code="MODFLOW_STRESS_UNSTATED")
    return {"CSUB_PACKAGEDATA": {"interbeds": [{
        "composite": "Interbeds", "geometry": value["geometry"], "layer": layer, "cdelay": cdelay.lower(),
        "name": _given(value, "Interbeds"), "dimensions": dict(_INTERBED_RASTER),
        "values": {argument: value[argument] for argument in read}}]}}, {}


_interbeds.__doc__ = (
    "The CSUB interbeds a geometry places: Interbeds(geometry, layer, cdelay, pcs0, thick_frac, ssv_cc, sse_cr, theta"
    "[, rnb, kv, h0, name]), or a list of such statements.\n\n"
    "    Every cell the geometry places, on the layer (an integer from 1, or \"all\" for every solved layer), is\n"
    "    one interbed, numbered in statement order then cell order. cdelay is DELAY or NODELAY: rnb, kv and h0\n"
    "    are the delay interbed's own, required for DELAY and refused for NODELAY. pcs0, ssv_cc and sse_cr are\n"
    "    numbers in the units the CSUB options state (head or stress, compression indices or specific storage);\n"
    "    thick_frac, theta, rnb, kv and h0 are a number or a raster read at each cell centre. name is the\n"
    "    boundary name, required when CSUB_BOUNDNAMES is true.")


_INTERPOLATION = ("STEPWISE", "LINEAR", "LINEAREND")


def _series(value: Any, *, run: Mapping[str, Any] | None = None) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if isinstance(value, (list, tuple)) and value:
        slots: dict[str, Any] = {}
        for each in value:
            for key, held in _series(each, run=run)[0].items():
                if key in slots:
                    raise ModflowError(f"TimeSeries states {key[:-len('_TS_FILERECORD')]} twice; one file feeds a package.",
                                       error_code="MODFLOW_STRESS_UNSTATED")
                slots[key] = held
        return slots, {}
    if not isinstance(value, Mapping):
        raise ModflowError(
            f"TimeSeries takes a mapping of package, names and rows; got {value!r}.",
            error_code="MODFLOW_STRESS_UNSTATED")
    _unknown(value, "TimeSeries", ("package", "names", "rows", "units", "interpolation", "sfac"))
    if not value:
        return {}, {}
    package, names = value.get("package"), value.get("names")
    if not isinstance(package, str) or not package.strip():
        raise ModflowError("TimeSeries package is the prefix of the package the series feeds (CHD, CSUB ...).",
                           error_code="MODFLOW_STRESS_UNSTATED")
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, (list, tuple)) or not names or not all(isinstance(each, str) and each.strip() for each in names) \
            or len(set(names)) != len(names):
        raise ModflowError(f"TimeSeries names is a list of distinct series names; got {names!r}.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    held = value.get("rows")
    if isinstance(held, Series):
        held = _recorded(held, names, value.get("units"), run or {})
    elif value.get("units") is not None:
        raise ModflowError("TimeSeries units converts a run input's record; the rows stated here are in the package's "
                           "own units.", error_code="MODFLOW_STRESS_UNSTATED")
    rows = _table(held, composite="TimeSeries", argument="rows", widths=(len(names) + 1,))
    if any(later[0] <= earlier[0] for earlier, later in zip(rows, rows[1:])):
        raise ModflowError("TimeSeries rows run in increasing time.", error_code="MODFLOW_STRESS_UNSTATED")
    return {f"{package.strip().upper()}_TS_FILERECORD": {"series": {
        "package": package.strip().upper(), "names": [each.strip() for each in names], "rows": rows,
        "interpolation": _per_name(value.get("interpolation"), names, "interpolation", words=_INTERPOLATION),
        "sfac": _per_name(value.get("sfac"), names, "sfac")}}}, {}


def _recorded(record: Series, names: Any, units: Any, run: Mapping[str, Any]) -> list[list[float]]:
    """A run input's record as the rows of one series, its times counted from the run's start in TDIS_TIME_UNITS."""
    from ..authoring.griddata import SECONDS

    if len(names) != 1:
        raise ModflowError(
            f"TimeSeries reads a run input's record as one series; names lists {len(names)}.",
            error_code="MODFLOW_STRESS_UNSTATED")
    clock = str(run.get("TDIS_TIME_UNITS") or "unknown").strip().lower()
    if clock not in SECONDS:
        raise ModflowError(
            f"TDIS_TIME_UNITS is {clock!r}, so the record's times on the run's clock have no length in it; "
            "state TDIS_TIME_UNITS as seconds, minutes, hours, days or years.", error_code="MODFLOW_CLOCK_UNSTATED")
    if units is not None:
        try:
            record = record.in_units(str(units))
        except Exception as refused:
            raise ModflowError(f"TimeSeries cannot read the record, in {record.units}, as {units}: {refused}",
                               error_code="MODFLOW_STRESS_UNSTATED") from refused
    return [[t / SECONDS[clock], v] for t, v in zip(record.times_s, record.values)]


def _per_name(held: Any, names: Any, argument: str, *, words: tuple[str, ...] = ()) -> list | None:
    """An attribute stated once for every series or once per series, or None where unstated."""
    if held is None:
        return None
    each = list(held) if isinstance(held, (list, tuple)) else [held] * len(names)
    ok = lambda item: (isinstance(item, str) and item.strip().upper() in words) if words else (
        isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item))
    if len(each) != len(names) or not all(ok(item) for item in each):
        raise ModflowError(
            f"TimeSeries {argument} is {'one of ' + ', '.join(words) if words else 'a number'}, once or per series "
            f"({len(names)}); got {held!r}.", error_code="MODFLOW_STRESS_UNSTATED")
    return [item.strip().lower() if words else float(item) for item in each]


_series.__doc__ = (
    "A package's time-series file: TimeSeries(package, names, rows[, units, interpolation, sfac]), or a list of them.\n\n"
    "    package is the prefix of the package the file feeds; names the series, the columns after the time;\n"
    "    rows [time, value per name] in increasing time, or the name of a run input holding one record (one name;\n"
    "    its times are counted from the run's start in TDIS_TIME_UNITS, and units converts its values). interpolation\n"
    "    (STEPWISE, LINEAR or LINEAREND) and sfac are mf6's own series attributes, once or per series, absent\n"
    "    when unstated. A stress statement names a series as {\"series\": NAME}; one file feeds a package.")


_OBSERVATION = ("type", "name", "package", "at", "on", "layer", "connection", "to_at", "to_layer", "digits")
_OBSERVATION_TYPE = re.compile(r"[A-Za-z0-9-]+")


def _observations(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if isinstance(value, (list, tuple)) and value:
        slots: dict[str, Any] = {}
        for each in value:
            for key, held in _observations(each)[0].items():
                slots.setdefault(key, {"observations": []})["observations"] += held["observations"]
        return slots, {}
    if not isinstance(value, Mapping):
        raise ModflowError(f"Observations takes a mapping of type, name and where, or a list of them; got {value!r}.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    _unknown(value, "Observations", _OBSERVATION)
    if not value:
        return {}, {}
    kind, name = value.get("type"), value.get("name")
    if not isinstance(kind, str) or not _OBSERVATION_TYPE.fullmatch(kind.strip()):
        raise ModflowError(f"Observations type is mf6's own observation type (head, flow-ja-face, maw ...); got {kind!r}.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40 or len(name.split()) != 1:
        raise ModflowError(f"Observations name is 1 to 40 characters without a space; got {name!r}.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    package = value.get("package")
    if package is not None and (not isinstance(package, str) or not package.strip()):
        raise ModflowError(f"Observations package is the prefix of the package whose OBS file this line goes in; "
                           f"got {package!r}.", error_code="MODFLOW_STRESS_UNSTATED")
    for argument, floor in (("layer", 1), ("connection", 1), ("to_layer", 1), ("digits", 0)):
        held = value.get(argument)
        if held is not None and (isinstance(held, bool) or not isinstance(held, int) or held < floor):
            raise ModflowError(f"Observations {argument} is an integer from {floor}; got {held!r}.",
                               error_code="MODFLOW_STRESS_UNSTATED")
    if value.get("on") is not None:
        _given({"name": value["on"]}, "Observations")
    statement = {key: held for key, held in value.items() if held is not None}
    statement.update(type=kind.strip(), name=name.strip())
    package = package.strip().upper() if package else None
    statement["package"] = package
    return {f"{package}_OBS_FILERECORD" if package else "OBS_CONTINUOUS": {"observations": [statement]}}, {}


_observations.__doc__ = (
    "A line of an OBS file: Observations(type, name[, package, at, on, layer, connection, to_at, to_layer, digits]), or a list.\n\n"
    "    type is mf6's own observation type and name the observation's name; with no package the line is the\n"
    "    model's OBS file, with one (SFR, LAK, MAW, UZF, CSUB, WEL ...) that package's own. A cell is at (a map\n"
    "    point, never moved) on a layer from 1; a placed feature is on (the name its composite gave), at, or both;\n"
    "    connection (from 1) and to_at with to_layer are the second id. One CSV per file and type is written, and\n"
    "    published as a chart with a line per observation. digits is the file's significant digits.")


def module_input_dir() -> Path:
    """Where the committed module inputs live."""
    return Path(__file__).resolve().parent / "module_input"


def periods(value: Any, *, run: Mapping[str, Any] | None = None
            ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The TDIS clock a ``Periods`` value states: the count and one row per period.

    Two forms: a non-empty list of (length, steps, multiplier) rows in the time
    units the deck spells, or a mapping ``{"until": <instant>, "steps": <int>}``
    whose instant is a run input resolved by the fill - the clock then runs from
    the run's start (its moment, else its event_time) to that instant, in
    ``TDIS_TIME_UNITS``, over the stated number of equal steps."""
    if isinstance(value, Mapping):
        return _period_until(value, run or {})
    if not isinstance(value, (list, tuple)) or not value:
        raise SlotRefused(
            "Periods takes a non-empty list of (length, steps, multiplier) rows "
            f"the simulation marches, or a mapping of until and steps; got {value!r}.")
    rows: list[list[float | int]] = []
    for rank, row in enumerate(value, start=1):
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            raise SlotRefused(
                f"Periods row {rank} is (length, steps, multiplier); got {row!r}.")
        length, steps, multiplier = row
        if isinstance(length, bool) or isinstance(multiplier, bool) \
                or not isinstance(length, (int, float)) \
                or not isinstance(multiplier, (int, float)) \
                or isinstance(steps, bool) or not isinstance(steps, int):
            raise SlotRefused(
                f"Periods row {rank} takes numbers (length and multiplier "
                f"real, steps an integer); got {row!r}.")
        if length <= 0 or steps < 1 or multiplier <= 0:
            raise SlotRefused(
                f"Periods row {rank} is ({length!r}, {steps!r}, "
                f"{multiplier!r}); a period runs a positive length in a "
                "positive number of steps with a positive multiplier, or the "
                "simulation advances nothing.")
        rows.append([float(length), int(steps), float(multiplier)])
    return {"TDIS_NPER": len(rows), "TDIS_PERIODDATA": rows}, {}


def _period_until(value: Mapping[str, Any], run: Mapping[str, Any]
                  ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The one period from the run's start to a stated ``until`` instant, over ``steps`` equal steps."""
    import datetime as dt

    from trid3nt_server.inputs.instant import instant

    from ..authoring.griddata import SECONDS

    unknown = sorted(set(value) - {"until", "steps"})
    if unknown:
        raise SlotRefused(
            f"Periods takes until and steps; {', '.join(unknown)} is none of them.")
    until, steps = value.get("until"), value.get("steps")
    if until is None:
        raise SlotRefused("Periods states no until; the period runs to an instant.")
    if steps is None:
        raise SlotRefused("Periods states no steps; the period marches a number of steps.")
    if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
        raise SlotRefused(
            f"Periods steps is {steps!r}; it is an integer from 1.")
    moment = run.get(MOMENT)
    start: Any = None
    if isinstance(moment, Mapping) and moment.get("at") is not None:
        start = moment["at"]
    elif run.get("event_time") is not None:
        start = run["event_time"]
    if isinstance(start, str):
        start = dt.datetime.fromisoformat(start)
    if start is None:
        raise SlotRefused(
            "Periods states until, but the run holds no event_time and no "
            "moment for the clock to start at.")
    try:
        end = dt.datetime.fromisoformat(
            instant(until, label="Periods.until", code="PERIODS_UNTIL_INVALID")
            or "")
    except ValueError as refused:
        raise SlotRefused(f"Periods until is not a parseable instant: {refused}") from None
    if end <= start:
        raise SlotRefused(
            f"Periods until {end.isoformat()} is not after the run's start "
            f"{start.isoformat()}; a period that advances nothing is no period.")
    units = str(run.get("TDIS_TIME_UNITS") or "unknown").strip().lower()
    if units not in SECONDS:
        raise SlotRefused(
            f"TDIS_TIME_UNITS is {units!r}, so the period's length in it has "
            "no length; state TDIS_TIME_UNITS as seconds, minutes, hours, "
            "days or years.")
    length = (end - start).total_seconds() / SECONDS[units]
    return {"TDIS_NPER": 1, "TDIS_PERIODDATA": [[length, int(steps), 1.0]]}, {}


def _start_date_time(moment: Mapping[str, Any]) -> str:
    """The run's moment as mf6 reads a start: one ISO 8601 instant."""
    return moment["at"].isoformat()


class ModflowModule(Module):
    """The MODFLOW wrapper: its dictionary and the registry of exposed bodies."""

    @classmethod
    def dictionary_dir(cls) -> Path:
        return module_input_dir()

    @classmethod
    def wrapper_for(cls, module: str) -> type:
        from . import wrapper_for

        return wrapper_for(module)

    @classmethod
    def sheet(cls, filled: dict[str, Any], files: dict[str, Any],
              run: Mapping[str, Any]) -> Any:
        """The filled deck a body's slots make: the generic sheet, held immutable.

        Nothing is armed, coupled or written into files here - MODFLOW's one
        simulation directory is the authoring's to write."""
        from ...runtime.sheet import Sheet

        return Sheet(body=cls, filled=MappingProxyType(filled),
                     files=MappingProxyType(files),
                     run=MappingProxyType(run))
