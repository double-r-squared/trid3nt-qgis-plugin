"""Observation weights: each target's sigma off the accuracy its record cites, and the widening after the fit.

Sigma is the source's cited accuracy, a gauge zero's beside the reading's in quadrature; the weight is one over
it. A record citing none is weighed in its own group, from the widest its source cites elsewhere (a floor), or
from 1 in the run's unit where its source cites none. After the fit each group's sigma widens uniformly until
its phi is its count, the discrepancy rule; a cited sigma or a floor never narrows.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Callable, Mapping, Sequence

from .authoring.control import Observed
from .errors import PestppError

__all__ = ["CITED", "FLOOR", "NOT_CITED", "UNIT", "USER", "observed", "restated", "unit_started", "widened"]

#: How an observation's sigma was reached.
CITED, FLOOR, UNIT, USER = "cited", "floor", "unit start", "stated by the user"

#: The group every observation whose record cites no accuracy is weighed in.
NOT_CITED = "not_cited"


def _quadrature(parts: Sequence[tuple[str, float, str]]) -> float:
    return math.sqrt(sum(sigma ** 2 for _member, sigma, _cite in parts))


def observed(name: str, value: float, found: Any, *, to_run: Callable[[float], float], group: str) -> Observed:
    """One target weighed off its reading's accuracy and, where a gauge zero lifts it, that zero's; each in the
    reading's unit moved onto the run's by ``to_run``. A record citing either part not is weighed in NOT_CITED."""
    parts, floors, short = [], [], False
    for member, accuracy in (("accuracy", found.accuracy), ("above_accuracy", found.zero_accuracy)):
        if member == "above_accuracy" and accuracy is None:
            continue
        if accuracy is not None and accuracy.value is not None:
            parts.append((member, abs(to_run(accuracy.value)), accuracy.cite))
        elif accuracy is not None and accuracy.widest is not None:
            floors.append((member, abs(to_run(accuracy.widest)), accuracy.cite))
        else:
            short = True
    if not floors and not short:
        sigma, basis, group_of = _quadrature(parts), CITED, group
    elif parts or floors:
        sigma, basis, group_of = _quadrature(parts + floors), FLOOR, NOT_CITED
    else:
        sigma, basis, group_of = 1.0, UNIT, NOT_CITED
    if not sigma > 0.0:
        raise PestppError(f"{name}'s cited accuracy is 0, and a sigma of 0 weighs a reading without bound",
                          error_code="PESTPP_SIGMA_ZERO")
    return Observed(name=name, value=value, group=group_of, sigma=sigma, basis=basis, parts=tuple(parts + floors))


def restated(targets: Sequence[tuple[Observed, Any]], stated: Any) -> list[tuple[Observed, Any]]:
    """The targets with the sigma the user states per group, which wins over the cited one."""
    if not stated:
        return list(targets)
    groups = sorted({target.group for target, _where in targets})
    if not isinstance(stated, Mapping) or not set(stated) <= set(groups):
        raise PestppError(f"sigma is stated per observation group, and this study's groups are {', '.join(groups)}",
                          error_code="PESTPP_SIGMA_GROUP_UNKNOWN")
    for group, sigma in stated.items():
        if not isinstance(sigma, (int, float)) or isinstance(sigma, bool) or not sigma > 0:
            raise PestppError(f"the sigma stated for {group} is no number above 0 in the run's unit",
                              error_code="PESTPP_SIGMA_INVALID")
    return [(replace(target, sigma=float(stated[target.group]), basis=USER,
                     parts=(("stated", float(stated[target.group]), USER),))
             if target.group in stated else target, where) for target, where in targets]


def unit_started(observed: Sequence[Observed]) -> int:
    """The count of targets started at sigma 1 in the run's unit when any other target is weighed otherwise
    (cited, floor or stated), the case where that start sets the fit's relative weight; 0 otherwise."""
    started = sum(o.basis == UNIT for o in observed)
    return started if started < len(observed) else 0


def widened(observed: Sequence[Observed], residuals: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The discrepancy rule after the fit: each group's sigma scaled by one factor so its phi is its count of
    observations, the best parameters as they are -> the step as the study record states it. A group of cited
    sigmas or floors only widens; a group started at 1 in the run's unit takes its factor either way."""
    held = {o.name.lower(): o for o in observed}
    groups: dict[str, dict[str, Any]] = {}
    for row in residuals:
        if float(row["weight"]) <= 0.0:
            continue
        group = groups.setdefault(str(row["group"]).lower(), {"observations": 0, "phi": 0.0, "unit": True})
        group["observations"] += 1
        group["phi"] += (float(row["weight"]) * float(row["residual"])) ** 2
        group["unit"] &= held[str(row["name"]).lower()].basis == UNIT
    for group in groups.values():
        factor = math.sqrt(group["phi"] / group["observations"]) or 1.0
        group["factor"] = factor if group.pop("unit") else max(1.0, factor)
        group["widened_phi"] = group["phi"] / group["factor"] ** 2
    return {"rule": "discrepancy", "phi": sum(g["phi"] for g in groups.values()),
            "widened_phi": sum(g["widened_phi"] for g in groups.values()),
            "observations": sum(g["observations"] for g in groups.values()), "groups": groups,
            "sigma": {o.name: o.sigma * groups[o.group]["factor"] for o in observed if o.group in groups}}
