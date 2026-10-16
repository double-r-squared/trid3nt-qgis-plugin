"""The run's PROVENANCE rows: what each value input was answered with, and by whom.

Every answered value leaves a row on the layer.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping, Sequence

from trid3nt_contracts.common import Origin, SyntheticInput

from trid3nt_server.inputs.input import Input

__all__ = ["merge_provenance", "provenance_entries"]

#: How a value's origin reads beside its description.
_STATED = {Origin.USER: "supplied on this invocation",
           Origin.TEMPLATE: "declared default"}


def provenance_entries(answered: Mapping[str, Any],
                       declared: Sequence[Input]) -> list[SyntheticInput]:
    """The run's provenance rows off the fill's verdicts - what the layer carries."""
    out: list[SyntheticInput] = []
    for row in declared:
        verdict = answered.get(row.name)
        if row.is_row:
            continue
        if verdict is None or verdict.origin is None:
            continue
        out.append(SyntheticInput(
            param=row.name, value=_provenance_value(verdict.value),
            units=row.units, basis=verdict.origin,
            note=f"{row.desc} [{_STATED[verdict.origin]}]"))
    return out


def merge_provenance(existing: Sequence[SyntheticInput],
                     declared: Sequence[SyntheticInput]) -> list[SyntheticInput]:
    """Merge a composite step's own provenance rows with the declared rows.
    The composite's row WINS on a name collision - it stamped what actually
    resolved, while the declaration only knows what was asked for."""
    kept = list(existing)
    taken = {row.param for row in kept}
    kept.extend(row for row in declared if row.param not in taken)
    return kept


def _provenance_value(value: Any) -> Any:
    # A provenance row's value is a scalar or a string, so a coordinate pair travels as its text.
    rendered = wire_value(value)
    if isinstance(rendered, dict):
        return ", ".join(f"{k}={v}" for k, v in rendered.items() if v is not None)
    return str(rendered) if isinstance(rendered, (list, bool)) else rendered


def wire_value(value: Any) -> Any:
    """Render a resolved value for the wire - ONE rule for every surface.
    DISPLAY only: the run reads the sheet, and a row the user did not edit is
    never re-seated from what the card showed."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: wire_value(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, float):
        # Six SIGNIFICANT figures, not decimal places: 9.3e-07 to four decimals reads 0.0, and a physics value
        # reported as zero is worse than no row.
        return float(f"{value:.6g}")
    if isinstance(value, (list, tuple)):
        return [wire_value(v) for v in value]
    return str(value)
