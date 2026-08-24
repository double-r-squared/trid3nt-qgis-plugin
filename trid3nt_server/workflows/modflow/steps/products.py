"""Chart specs built from an archetype layer's own typed scalars.

The SPEC is the product: the plan declares the chart, the interpreter persists it
on ``RunResult.charts`` and the plugin dock renders it. Nothing here recomputes a
number the solver already reported.
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_budget_chart"]


def build_budget_chart(*, result: Any, params: Any) -> dict[str, Any] | None:  # noqa: ARG001
    """The regional budget as a signed inflow/outflow bar - real CBC terms only.

    ``None`` when the partition is empty: a bar chart of nothing is a claim the
    run did not earn (the honesty floor).
    """
    partition = dict(getattr(result, "budget_partition_m3_day", None) or {})
    if not partition:
        return None

    from trid3nt_server.tools.processing.charts_common import (
        build_budget_partition_chart,
    )

    return build_budget_partition_chart(
        budget_partition_m3_day=partition,
        source_layer_uri=getattr(result, "uri", None),
    )
