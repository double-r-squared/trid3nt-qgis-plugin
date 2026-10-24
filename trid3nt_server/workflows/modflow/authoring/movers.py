"""The MVR package's rows from the features the placed composites name.

A mover statement ends in two features, each a name, a point in its cell or both, in any of the flow models; this module
resolves them to the numbers mf6 reads (reach, lake outlet or lake, well, UZF cell) and writes each period's whole mover list.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..errors import ModflowError
from trid3nt_server.inputs.point import PointNotViableError

from .stress import GIVERS, at_cell, pick

__all__ = ["grouped", "mover_rows", "resolved_moves"]

# The packages that take water from the mover (and give it); the boundary packages only give.
_MOVING = ("SFR", "LAK", "MAW", "UZF")


def mover_rows(statements: list[Mapping[str, Any]], features: list[Mapping[str, Any]], *, points: Any,
               cells: Any, utm_epsg: int | None) -> tuple[list[str], dict[int, list[list]]]:
    """Mover statements -> ``(packages used, {period index: rows})``, each row ``[provider package, id, receiver package, id, type, value]``.

    A provider package is a lake through one of its outlets (``from_outlet`` from 1, optional when it has one);
    a period a value is keyed by writes every mover with the value in force there, since an mf6 period block
    replaces the one before it. Packages are named by their prefix, in order of first use."""
    models = {"": {"features": features, "points": points, "cells": cells, "utm_epsg": utm_epsg}}
    moves = resolved_moves(statements, models, home="")
    used, periods = grouped(moves, lambda move: (move["from"][0], move["to"][0]))[("", "")]
    return used, periods


def resolved_moves(statements: list[Mapping[str, Any]], models: Mapping[str, Mapping[str, Any]], *,
                   home: str) -> list[dict[str, Any]]:
    """Each statement as ``{from: (model, package, id), to: (model, package, id), type, value}``.

    ``models`` is each flow model's placed ``features`` and its ``points``, ``cells`` and ``utm_epsg``; an end resolves
    to exactly one feature among all of them, and a point that misses ``home`` is read on the others first."""
    rows = []
    for rank, item in enumerate(statements, start=1):
        pools = {name: [f for f in held["features"] if f["package"] in (*_MOVING, *GIVERS)]
                 for name, held in models.items()}
        model, source = _end(item.get("from"), item.get("from_at"), pools, models, home=home,
                             role=f"WaterMover {rank} from")
        named = item.get("to")
        boundary = next((f["package"] for held in models.values() for f in held["features"]
                         if f["package"] in GIVERS and f["name"] == named), None)
        receivers = {name: [f for f in held["features"] if f["package"] in _MOVING] for name, held in models.items()}
        if boundary and not any(f["name"] == named for pool in receivers.values() for f in pool):
            raise ModflowError(
                f"WaterMover {rank} to names {named!r}, a {boundary} boundary: mf6 moves water to SFR, LAK, MAW "
                "and UZF features only.", error_code="MODFLOW_STRESS_UNSTATED")
        target_model, target = _end(named, item.get("to_at"), receivers, models, home=home,
                                    role=f"WaterMover {rank} to")
        outlet = item.get("from_outlet")
        if source["package"] == "LAK":
            outlets = source["outlets"]
            if outlet is None and len(outlets) != 1:
                raise ModflowError(
                    f"WaterMover {rank}: lake {source['number'] + 1} has {len(outlets)} outlet(s); a lake gives water to "
                    "the mover through an outlet, so state the lake's outlets and from_outlet "
                    "(from 1) when it has more than one.", error_code="MODFLOW_STRESS_UNSTATED")
            if outlet is not None and not 1 <= outlet <= len(outlets):
                raise ModflowError(
                    f"WaterMover {rank}: from_outlet {outlet} but lake {source['number'] + 1} has {len(outlets)} outlet(s).",
                    error_code="MODFLOW_STRESS_UNSTATED")
            provider = outlets[(outlet or 1) - 1]
        elif outlet is not None:
            raise ModflowError(f"WaterMover {rank}: from_outlet names a lake's outlet and the provider is "
                               f"{source['package']}.", error_code="MODFLOW_STRESS_UNSTATED")
        else:
            provider = source["number"]
        rows.append({"from": (model, source["package"], provider), "to": (target_model, target["package"], target["number"]),
                     "type": item["type"], "value": item["value"]})
    return rows


def _end(name: Any, at: Any, pools: Mapping[str, list[Mapping[str, Any]]], models: Mapping[str, Mapping[str, Any]], *,
         home: str, role: str) -> tuple[str, Mapping[str, Any]]:
    """The one feature of one model a statement's end names; with a single model, exactly ``pick``."""
    if len(pools) == 1:
        (model, pool), = pools.items()
        held = models[model]
        return model, pick(name, at, pool, role=role, points=held["points"], cells=held["cells"],
                           utm_epsg=held["utm_epsg"])
    if name is None and at is None:
        raise ModflowError(f"{role} states neither a name nor a point; state the feature it is.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    found: list[tuple[str, Mapping[str, Any]]] = []
    missed: list[Exception] = []
    for model, pool in pools.items():
        items = [item for item in pool if name is None or item["name"] == name]
        if at is not None and items:
            held = models[model]
            try:
                cell = at_cell(at, role=role, points=held["points"], cells=held["cells"], utm_epsg=held["utm_epsg"])
            except PointNotViableError as exc:
                missed.append(exc)
                continue
            items = [item for item in items if cell in item["cells"]]
        found += [(model, item) for item in items]
    if at is not None and not found and name is None and len(missed) == len(pools):
        raise missed[0]
    if len(found) != 1:
        named = sorted({str(item["name"]) for pool in pools.values() for item in pool if item["name"] is not None})
        raise ModflowError(
            f"{role} names {len(found)} features"
            + (f" ({', '.join(f'{model}: {item['package']} {item['number'] + 1}' for model, item in found)})" if found else "")
            + ("" if found or name is None else f", and no placed feature of any model carries the name {name!r}"
               + (f" (named: {', '.join(named)})" if named else ""))
            + "; state the one it is by a name, a point in its cell, or both.", error_code="MODFLOW_STRESS_UNSTATED")
    return found[0]


def grouped(moves: list[dict[str, Any]], key: Any) -> dict[Any, tuple[list[str], dict[int, list[list]]]]:
    """The moves by ``key(move)`` -> ``(packages used, {period: rows})`` each; a row is ``[model, package, id, model, package, id, type, value]`` when its key joins two models and ``[package, id, package, id, type, value]`` inside one."""
    out: dict[Any, tuple[list[str], dict[int, list[list]]]] = {}
    for group in dict.fromkeys(key(move) for move in moves):
        mine = [move for move in moves if key(move) == group]
        used: list[str] = []
        periods = {0}
        for move in mine:
            used += [package for package in (move["from"][1], move["to"][1]) if package not in used]
            if isinstance(move["value"], dict):
                periods |= set(move["value"])
        crossed = len({move["from"][0], move["to"][0]}) > 1
        rows = {period: [[*(move["from"][0:1] if crossed else ()), move["from"][1], move["from"][2],
                          *(move["to"][0:1] if crossed else ()), move["to"][1], move["to"][2],
                          move["type"], _in_force(move["value"], period)] for move in mine]
                for period in sorted(periods)}
        out[group] = (used, rows)
    return out


def _in_force(value: Any, period: int) -> Any:
    return value[max(key for key in value if key <= period)] if isinstance(value, dict) else value
