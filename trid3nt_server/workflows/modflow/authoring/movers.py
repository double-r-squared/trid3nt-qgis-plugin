"""The MVR package's rows from the features the placed composites name.

A mover statement ends in two features, each a name, a point in its cell or both; this module resolves them to
the numbers mf6 reads (reach, lake outlet or lake, well, UZF cell) and writes each period's whole mover list.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..errors import ModflowError
from .stress import GIVERS, pick

__all__ = ["mover_rows"]

# The packages that take water from the mover (and give it); the boundary packages only give.
_MOVING = ("SFR", "LAK", "MAW", "UZF")


def mover_rows(statements: list[Mapping[str, Any]], features: list[Mapping[str, Any]], *, points: Any,
               cells: Any, utm_epsg: int | None) -> tuple[list[str], dict[int, list[list]]]:
    """Mover statements -> ``(packages used, {period index: rows})``, each row ``[provider package, id, receiver package, id, type, value]``.

    A provider package is a lake through one of its outlets (``from_outlet`` from 1, optional when it has one);
    a period a value is keyed by writes every mover with the value in force there, since an mf6 period block
    replaces the one before it. Packages are named by their prefix, in order of first use."""
    mesh = {"points": points, "cells": cells, "utm_epsg": utm_epsg}
    receivers = [f for f in features if f["package"] in _MOVING]
    givers = [f for f in features if f["package"] in (*_MOVING, *GIVERS)]
    rows, used, periods = [], [], {0}
    for rank, item in enumerate(statements, start=1):
        source = pick(item.get("from"), item.get("from_at"), givers,
                      role=f"WaterMover {rank} from", **mesh)
        named = item.get("to")
        boundary = next((f["package"] for f in features if f["package"] in GIVERS and f["name"] == named), None)
        if boundary and not any(f["name"] == named for f in receivers):
            raise ModflowError(
                f"WaterMover {rank} to names {named!r}, a {boundary} boundary: mf6 moves water to SFR, LAK, MAW "
                "and UZF features only.", error_code="MODFLOW_STRESS_UNSTATED")
        target = pick(item.get("to"), item.get("to_at"), receivers,
                      role=f"WaterMover {rank} to", **mesh)
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
        rows.append((source["package"], provider, target["package"], target["number"], item["type"], item["value"]))
        used += [package for package in (source["package"], target["package"]) if package not in used]
        if isinstance(item["value"], dict):
            periods |= set(item["value"])
    return used, {period: [[a, i, b, j, kind, _in_force(value, period)] for a, i, b, j, kind, value in rows]
                  for period in sorted(periods)}


def _in_force(value: Any, period: int) -> Any:
    return value[max(key for key in value if key <= period)] if isinstance(value, dict) else value
