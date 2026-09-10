"""The ``chart-emission`` envelope and its Vega-Lite wire-format contract.

The ENTIRE spec crosses the wire as an opaque dict - the grammar is large,
evolving and owned upstream, so it is not re-modelled here. Only a CHEAP
structural check runs at the boundary, so an empty or broken spec is refused
before a renderer meets it.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field, field_validator

from .common import GraceModel, ULIDStr, UTCDatetime

__all__ = [
    "ChartEmissionPayload",
    "SessionChartRecord",
    "CHART_AGENT_TO_CLIENT_PAYLOADS",
    "is_structurally_valid_vega_lite_spec",
]


# --------------------------------------------------------------------------- #
# Vega-Lite structural sanity check - NOT full Vega validation
# --------------------------------------------------------------------------- #


def is_structurally_valid_vega_lite_spec(spec: dict[str, Any]) -> bool:
    """True when ``spec`` LOOKS like a Vega-Lite spec: a ``$schema`` key, or
    BOTH a ``mark`` and an ``encoding``. Not a grammar check - it catches an
    empty dict, a list, a scalar or an unrelated dict, and nothing finer.
    """
    if not isinstance(spec, dict):
        return False
    if "$schema" in spec:
        return True
    return "mark" in spec and "encoding" in spec


# --------------------------------------------------------------------------- #
# chart-emission envelope payload (agent -> client)
# --------------------------------------------------------------------------- #


class ChartEmissionPayload(GraceModel):
    """``chart-emission``, emitted once a tool has computed a chart's data.
    Every number rendered is structured data inside ``vega_lite_spec``, computed
    deterministically - never prose a model wrote. No cost field anywhere.
    """

    MESSAGE_TYPE: ClassVar[str] = "chart-emission"

    envelope_type: Literal["chart-emission"] = "chart-emission"
    #: The de-dupe key a consumer keys the chart on across a replay.
    chart_id: ULIDStr
    #: The full spec, opaque. Structurally checked, not grammar-validated.
    vega_lite_spec: dict[str, Any]
    title: str = Field(min_length=1)
    #: One-line interpretation under the chart. Capped to stay a caption.
    caption: str | None = Field(default=None, max_length=512)
    #: The layer the chart was computed from, when there is a single one. A
    #: chart assembled from several sources has none.
    source_layer_uri: str | None = None
    #: The ONLY stack-grouping signal: charts sharing a value render as one
    #: stack. ``None`` makes the chart its own stack. Timing is never inferred.
    created_turn_id: str | None = None

    @field_validator("vega_lite_spec")
    @classmethod
    def _validate_vega_lite_spec(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Cheap structural check: ``$schema`` present, OR both ``mark`` and
        ``encoding`` present. Rejects empty / junk specs at the boundary."""
        if not is_structurally_valid_vega_lite_spec(value):
            raise ValueError(
                "vega_lite_spec is not structurally a Vega-Lite spec: it must "
                "contain a '$schema' key, or BOTH 'mark' and 'encoding' keys. "
                f"got keys: {sorted(value.keys()) if isinstance(value, dict) else type(value).__name__}"
            )
        return value


# --------------------------------------------------------------------------- #
# Persistence record, appended to a session document's ``charts`` array
# --------------------------------------------------------------------------- #


class SessionChartRecord(GraceModel):
    """One persisted chart on a session document's append-only ``charts`` array.
    Append-only: a record is never mutated in place, and a re-render appends a
    new record rather than editing the old one.
    """

    schema_version: Literal["v1"] = "v1"

    #: Carried on the record so a chart read back outside its parent document
    #: is still self-contained.
    session_id: ULIDStr
    #: Stored WHOLE, so a replay reconstructs the identical envelope.
    payload: ChartEmissionPayload
    #: The replay sort key and the explicit within-array ordering authority.
    emitted_at: UTCDatetime = Field(...)


# --------------------------------------------------------------------------- #
# Routing registry fragment
# --------------------------------------------------------------------------- #

CHART_AGENT_TO_CLIENT_PAYLOADS: dict[str, type[GraceModel]] = {
    ChartEmissionPayload.MESSAGE_TYPE: ChartEmissionPayload,
}
