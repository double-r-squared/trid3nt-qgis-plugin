"""Chart-emission envelope + Vega-Lite wire-format contract (sprint-13 Stage 1,
conversational data-analysis layer; see memory ``project_conversational_data
_analysis_layer`` + sprint-13 manifest job-0223).

The conversational analysis layer lets a user ask data-backed follow-up
questions about layers already on the map ("how many structures?", "show me a
damage distribution") and get an inline chart back. The agent's chart-generation
tools (job-0230: ``generate_histogram`` / ``generate_choropleth_legend`` /
``generate_time_series`` / ``generate_damage_distribution``) compute the chart
data, build a **Vega-Lite v5 JSON spec**, and emit a ``chart-emission`` envelope
(agent -> client, Appendix A.4 amendment). The client (job-0231) renders the
spec via ``vega-embed`` as an inline stacked preview and a full-viewport gallery.

Why Vega-Lite as the wire format
--------------------------------
Vega-Lite is a declarative JSON grammar — it is LLM-friendly (the model can be
shown the grammar and emit valid specs), self-describing (the spec carries its
own data + encodings), and renders client-side with no server round-trip. We put
the *entire* spec on the wire as an opaque ``dict`` rather than re-modeling
Vega-Lite's grammar in pydantic: the grammar is large, evolving, and owned
upstream by the Vega project; mirroring it here would be brittle and is not our
contract to police. We do a **cheap structural sanity check** (see
``_validate_vega_lite_spec``) — not full Vega validation — so an obviously-broken
or empty spec is rejected at the contract boundary instead of failing silently in
the browser.

Determinism boundary (Invariant 1 / Decision H / FR-AS-7)
---------------------------------------------------------
The chart's numbers live inside ``vega_lite_spec`` as structured data computed by
a deterministic tool, never narrated free text the LLM invents. The agent's
narration cites the same tool-computed values fed back as ``function_response``;
this envelope is the *visual* surface of those same numbers. No cost field
anywhere (Invariant 9).

Persistence — MongoDB ``sessions`` collection (manifest OQ-4, TENTATIVE)
-----------------------------------------------------------------------
Charts persist so they replay when a Case is rehydrated. Per sprint-13 manifest
OQ-4 (TENTATIVE), charts are stored as an **append-only field array on the
existing ``sessions`` collection document** — NOT a new ``charts`` collection.
Each emitted chart is wrapped in a :class:`SessionChartRecord` (the chart payload
plus ``emitted_at`` and ``session_id``) and appended to a ``charts: list`` field
on the session document. On Case rehydration the writer replays the array in
``emitted_at`` order; the client re-groups records into UI stacks by
``ChartEmissionPayload.created_turn_id`` (records sharing a turn id render as one
stack). The array is append-only — charts are never mutated in place, matching
the ``chat_history`` / ``pipeline_history`` append discipline already on the
session document (A.7 replace-not-reconcile applies at the document level, not the
array element level).

NOTE: this module owns the *record shape* (``SessionChartRecord``) the writer
appends; it does NOT add the ``charts`` array field to
``collections.SessionDocument`` itself. Adding that field is a sibling
schema follow-up (``collections.py`` is a separate ownership surface and the
field has downstream session-replay implications for ``web`` + ``agent``); it is
surfaced as an Open Question in the job-0223 report rather than landed here. The
writer can persist ``SessionChartRecord`` documents today via a ``$push`` to a
``charts`` array without the field being declared on the model, because the
session writer round-trips through Mongo, not through ``SessionDocument``
validation on write.

Registration (manifest job-0223 scope: "register chart-emission the same way")
------------------------------------------------------------------------------
``chart-emission`` is an agent -> client (Appendix A.4) message. Following the
``secrets`` / ``payload_warning`` precedent, this module exports a per-module
routing fragment :data:`CHART_AGENT_TO_CLIENT_PAYLOADS`; ``ws.py`` (Appendix A,
schema-owned) splats it into ``AGENT_TO_CLIENT_PAYLOADS`` / ``ALL_PAYLOADS`` so
the wire envelope is decode-routable like every other message.
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
# Vega-Lite structural sanity check (cheap — NOT full Vega validation)
# --------------------------------------------------------------------------- #


def is_structurally_valid_vega_lite_spec(spec: dict[str, Any]) -> bool:
    """Cheap structural sanity check that ``spec`` *looks like* a Vega-Lite spec.

    This is deliberately NOT a full Vega-Lite validation (we do not police the
    upstream grammar). A spec is accepted when EITHER:

    - it carries a ``"$schema"`` key (a real Vega-Lite/Vega spec declares its
      schema URL, e.g. ``https://vega.github.io/schema/vega-lite/v5.json``), OR
    - it carries BOTH a ``"mark"`` key AND an ``"encoding"`` key (the minimal
      single-view Vega-Lite shape: what to draw + how to map data to it).

    Anything else (an empty dict, a list, a JSON scalar, a dict of unrelated
    keys) is rejected. The point is to catch obviously-broken / empty payloads
    at the contract boundary so they never reach ``vega-embed`` in the browser.
    """
    if not isinstance(spec, dict):
        return False
    if "$schema" in spec:
        return True
    return "mark" in spec and "encoding" in spec


# --------------------------------------------------------------------------- #
# chart-emission envelope payload (agent -> client, Appendix A.4 amendment)
# --------------------------------------------------------------------------- #


class ChartEmissionPayload(GraceModel):
    """``chart-emission`` (Appendix A.4 amendment, job-0223, sprint-13).

    The agent emits this after a chart-generation tool computes chart data and
    builds a Vega-Lite v5 spec. The client renders the spec inline (stacked
    preview) and in a full-viewport gallery; the chart is also persisted to the
    ``sessions`` collection (wrapped in :class:`SessionChartRecord`) so it
    replays on Case rehydration.

    Fields:

    - ``envelope_type`` — discriminator, literal ``"chart-emission"``.
    - ``chart_id`` — unique id for this chart (ULID). The client keys the chart
      on it (de-dupe on replay, gallery navigation, save-as-PNG filename).
    - ``vega_lite_spec`` — the full Vega-Lite v5 JSON spec as an opaque dict.
      Validated structurally (``$schema`` OR ``mark``+``encoding``), not against
      the full grammar. Carries the chart's data + encodings; the client passes
      it straight to ``vega-embed``.
    - ``title`` — chart title shown in the preview card + gallery (non-empty).
    - ``caption`` — optional one-line caption / interpretation under the chart.
      Capped at 512 chars to keep it a caption, not a narrative.
    - ``source_layer_uri`` — optional ``gs://`` / layer URI the chart was
      computed from (e.g. the damage layer behind a damage-distribution
      histogram). Lets the client offer "show the source layer" and lets the
      writer cross-reference the chart to a layer on replay. Optional because
      some charts (a pure summary the agent assembled) have no single source
      layer.
    - ``created_turn_id`` — the **UI stack-grouping key**. Charts emitted within
      the same agent turn / tool-call sequence carry the same ``created_turn_id``
      and the client renders them as one stack (top chart visible, the rest
      offset behind with a ``+N`` badge). Optional: when None the client treats
      the chart as its own singleton stack. This is the only grouping signal —
      the client does NOT infer stacks from timing.

    Invariant 1 (Determinism boundary): every number rendered is structured data
    inside ``vega_lite_spec`` computed by a deterministic tool, never narrated
    prose. Invariant 9: no cost field anywhere.
    """

    MESSAGE_TYPE: ClassVar[str] = "chart-emission"

    envelope_type: Literal["chart-emission"] = "chart-emission"
    chart_id: ULIDStr
    vega_lite_spec: dict[str, Any]
    title: str = Field(min_length=1)
    caption: str | None = Field(default=None, max_length=512)
    source_layer_uri: str | None = None
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
# Persistence record — appended to the sessions-collection ``charts`` array
# --------------------------------------------------------------------------- #


class SessionChartRecord(GraceModel):
    """One persisted chart on a session document's append-only ``charts`` array.

    The session writer wraps each emitted :class:`ChartEmissionPayload` in this
    record and ``$push``-es it onto the ``charts`` field of the ``sessions``
    document (manifest OQ-4 TENTATIVE: same collection, no new collection). On
    Case rehydration the writer replays the array in ``emitted_at`` order and the
    client re-groups by ``payload.created_turn_id`` into UI stacks.

    Fields:

    - ``schema_version`` — contract version pin (additive growth only).
    - ``session_id`` — owning session (ULID; equals ``sessions._id``). Carried on
      the record so a chart is self-contained when read back outside its parent
      document (e.g. a projection that pulls only the ``charts`` array).
    - ``payload`` — the exact ``ChartEmissionPayload`` that was emitted to the
      client. Stored whole so replay reconstructs the identical envelope (same
      ``chart_id``, ``vega_lite_spec``, ``created_turn_id`` for stack grouping).
    - ``emitted_at`` — when the chart was emitted (UTC, ``Z`` on the wire). The
      replay sort key; also the within-array ordering authority (the array is
      append-only so insertion order already matches, but ``emitted_at`` is the
      explicit contract).

    Append-only: records are never mutated in place. A re-render of the same
    chart appends a new record (new ``chart_id``); the client de-dupes on
    ``chart_id`` if a tool re-emits.
    """

    schema_version: Literal["v1"] = "v1"

    session_id: ULIDStr
    payload: ChartEmissionPayload
    emitted_at: UTCDatetime = Field(...)


# --------------------------------------------------------------------------- #
# Routing registry fragment (sibling wires into ws.ALL_PAYLOADS — see ws.py)
# --------------------------------------------------------------------------- #
#
# ``chart-emission`` is agent -> client (Appendix A.4). Following the
# ``secrets`` / ``payload_warning`` precedent, this module exposes the typed
# routing fragment; ``ws.py`` (Appendix A, schema-owned) splats it into
# ``AGENT_TO_CLIENT_PAYLOADS`` so the decoder can route the wire envelope.

CHART_AGENT_TO_CLIENT_PAYLOADS: dict[str, type[GraceModel]] = {
    ChartEmissionPayload.MESSAGE_TYPE: ChartEmissionPayload,
}
