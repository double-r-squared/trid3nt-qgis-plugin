"""Validation + round-trip tests for the chart-emission contract (sprint-13
Stage 1, conversational data-analysis layer, job-0223).

Covers:
- ``ChartEmissionPayload`` JSON round-trip (idempotent serialize/deserialize),
  the stack-grouping ``created_turn_id`` field, optional ``caption`` /
  ``source_layer_uri`` defaults, and ``extra='forbid'``.
- the structural Vega-Lite validator: accepts a real ``$schema``-bearing
  histogram spec AND a minimal ``mark``+``encoding`` spec; rejects junk
  (empty dict, missing mark/encoding, non-dict).
- ``SessionChartRecord`` round-trip + the append-only persistence shape.
- ``chart-emission`` is wired into the ws.py agent->client routing registry.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from grace2_contracts import ChartEmissionPayload, SessionChartRecord
from grace2_contracts.chart_contracts import (
    CHART_AGENT_TO_CLIENT_PAYLOADS,
    is_structurally_valid_vega_lite_spec,
)
from grace2_contracts.common import new_ulid
from grace2_contracts import ws


# --------------------------------------------------------------------------- #
# Fixtures: realistic Vega-Lite specs
# --------------------------------------------------------------------------- #


def _histogram_spec_with_schema() -> dict:
    """A realistic Vega-Lite v5 histogram spec (the damage-distribution shape).

    Declares ``$schema`` like a real Vega-Lite spec — this is the primary
    structural-validity signal.
    """
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": "Structure damage distribution",
        "data": {
            "values": [
                {"damage_pct": 5, "count": 120},
                {"damage_pct": 25, "count": 340},
                {"damage_pct": 50, "count": 210},
                {"damage_pct": 75, "count": 88},
                {"damage_pct": 100, "count": 41},
            ]
        },
        "mark": "bar",
        "encoding": {
            "x": {"field": "damage_pct", "bin": True, "type": "quantitative"},
            "y": {"field": "count", "aggregate": "sum", "type": "quantitative"},
        },
    }


def _minimal_mark_encoding_spec() -> dict:
    """A minimal single-view spec with NO ``$schema`` — valid via mark+encoding."""
    return {
        "mark": "point",
        "encoding": {
            "x": {"field": "a", "type": "quantitative"},
            "y": {"field": "b", "type": "quantitative"},
        },
        "data": {"values": [{"a": 1, "b": 2}]},
    }


def _payload(**overrides: object) -> ChartEmissionPayload:
    base = dict(
        chart_id=new_ulid(),
        vega_lite_spec=_histogram_spec_with_schema(),
        title="Structure damage distribution",
        caption="Most structures sustained 25-50% damage.",
        source_layer_uri="gs://trid3nt/runs/01HX/damage.fgb",
        created_turn_id="turn-01HX",
    )
    base.update(overrides)
    return ChartEmissionPayload(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Structural Vega-Lite validator
# --------------------------------------------------------------------------- #


def test_validator_accepts_schema_bearing_histogram() -> None:
    spec = _histogram_spec_with_schema()
    assert is_structurally_valid_vega_lite_spec(spec) is True
    # And it constructs a payload without error.
    p = _payload(vega_lite_spec=spec)
    assert p.vega_lite_spec["mark"] == "bar"


def test_validator_accepts_minimal_mark_encoding_without_schema() -> None:
    spec = _minimal_mark_encoding_spec()
    assert "$schema" not in spec
    assert is_structurally_valid_vega_lite_spec(spec) is True
    p = _payload(vega_lite_spec=spec)
    assert p.vega_lite_spec["mark"] == "point"


@pytest.mark.parametrize(
    "junk",
    [
        {},  # empty dict
        {"foo": "bar"},  # unrelated keys
        {"mark": "bar"},  # mark but no encoding
        {"encoding": {"x": {"field": "a"}}},  # encoding but no mark
        {"title": "just a title"},  # neither signal
    ],
)
def test_validator_rejects_junk_specs(junk: dict) -> None:
    assert is_structurally_valid_vega_lite_spec(junk) is False
    with pytest.raises(ValidationError):
        _payload(vega_lite_spec=junk)


@pytest.mark.parametrize("non_dict", [[], "spec", 42, None])
def test_validator_helper_rejects_non_dict(non_dict: object) -> None:
    # The free helper guards non-dict inputs (returns False, no raise).
    assert is_structurally_valid_vega_lite_spec(non_dict) is False  # type: ignore[arg-type]


def test_schema_alone_is_sufficient_even_without_mark_encoding() -> None:
    """A spec that declares ``$schema`` is accepted even if mark/encoding are
    expressed via layering / facet (no top-level ``mark``)."""
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "layer": [{"mark": "line"}, {"mark": "point"}],
    }
    assert is_structurally_valid_vega_lite_spec(spec) is True
    p = _payload(vega_lite_spec=spec)
    assert "layer" in p.vega_lite_spec


# --------------------------------------------------------------------------- #
# ChartEmissionPayload — fields, defaults, round-trip
# --------------------------------------------------------------------------- #


def test_envelope_type_discriminator_is_fixed() -> None:
    p = _payload()
    assert p.envelope_type == "chart-emission"
    assert ChartEmissionPayload.MESSAGE_TYPE == "chart-emission"


def test_stack_grouping_field_present_and_round_trips() -> None:
    """``created_turn_id`` is the only UI stack-grouping signal; it must survive
    the wire round-trip so the client can re-group charts into stacks."""
    p = _payload(created_turn_id="turn-ABC")
    dumped = p.model_dump(mode="json")
    assert "created_turn_id" in dumped
    assert dumped["created_turn_id"] == "turn-ABC"
    back = ChartEmissionPayload.model_validate(dumped)
    assert back.created_turn_id == "turn-ABC"


def test_created_turn_id_optional_defaults_none() -> None:
    p = _payload(created_turn_id=None)
    assert p.created_turn_id is None
    # A singleton chart (no turn id) is valid.
    assert p.model_dump(mode="json")["created_turn_id"] is None


def test_optional_fields_default_none() -> None:
    p = ChartEmissionPayload(
        chart_id=new_ulid(),
        vega_lite_spec=_minimal_mark_encoding_spec(),
        title="Untitled chart",
    )
    assert p.caption is None
    assert p.source_layer_uri is None
    assert p.created_turn_id is None


def test_title_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        _payload(title="")


def test_caption_capped_at_512_chars() -> None:
    with pytest.raises(ValidationError):
        _payload(caption="x" * 513)
    # 512 exactly is fine.
    p = _payload(caption="y" * 512)
    assert len(p.caption or "") == 512


def test_chart_id_must_be_a_ulid() -> None:
    with pytest.raises(ValidationError):
        _payload(chart_id="not-a-ulid")


def test_payload_forbids_extra_fields() -> None:
    """GraceModel extra='forbid' — an unknown field is a defect (no cost field
    sneaking in, no untyped extension)."""
    with pytest.raises(ValidationError):
        _payload(estimated_cost_usd=1.23)  # type: ignore[call-arg]


def test_payload_roundtrip_idempotent() -> None:
    p = _payload()
    a = p.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = ChartEmissionPayload.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    # The full Vega-Lite spec round-trips byte-for-byte (opaque dict preserved).
    assert a["vega_lite_spec"] == _histogram_spec_with_schema()


def test_payload_roundtrip_through_envelope_wrapper() -> None:
    """The payload wraps in the shared A.1 Envelope and round-trips on the wire."""
    sid = new_ulid()
    p = _payload()
    env = ws.Envelope[ChartEmissionPayload](
        type=ChartEmissionPayload.MESSAGE_TYPE,
        session_id=sid,
        payload=p,
    )
    dumped = env.model_dump(mode="json")
    assert dumped["type"] == "chart-emission"
    assert dumped["payload"]["envelope_type"] == "chart-emission"
    text = json.dumps(dumped, sort_keys=True)
    back = ws.Envelope[ChartEmissionPayload].model_validate(json.loads(text))
    assert back.payload.chart_id == p.chart_id
    assert back.payload.created_turn_id == p.created_turn_id


# --------------------------------------------------------------------------- #
# SessionChartRecord — persistence wrapper
# --------------------------------------------------------------------------- #


def test_session_chart_record_wraps_payload() -> None:
    sid = new_ulid()
    p = _payload()
    rec = SessionChartRecord(
        session_id=sid,
        payload=p,
        emitted_at="2026-06-09T12:00:00Z",
    )
    assert rec.schema_version == "v1"
    assert rec.session_id == sid
    assert rec.payload.chart_id == p.chart_id


def test_session_chart_record_roundtrip() -> None:
    sid = new_ulid()
    rec = SessionChartRecord(
        session_id=sid,
        payload=_payload(),
        emitted_at="2026-06-09T12:00:00Z",
    )
    a = rec.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = SessionChartRecord.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    # emitted_at serializes with a Z suffix (UTC wire convention).
    assert a["emitted_at"] == "2026-06-09T12:00:00Z"


def test_session_chart_record_preserves_stack_grouping_on_replay() -> None:
    """Replay reconstructs the same created_turn_id so the client re-groups
    charts into the same stacks they were emitted in."""
    sid = new_ulid()
    recs = [
        SessionChartRecord(
            session_id=sid,
            payload=_payload(created_turn_id="turn-1"),
            emitted_at="2026-06-09T12:00:00Z",
        ),
        SessionChartRecord(
            session_id=sid,
            payload=_payload(created_turn_id="turn-1"),
            emitted_at="2026-06-09T12:00:01Z",
        ),
        SessionChartRecord(
            session_id=sid,
            payload=_payload(created_turn_id="turn-2"),
            emitted_at="2026-06-09T12:05:00Z",
        ),
    ]
    # Simulate persist -> read-back (the append-only array on the session doc).
    array = [r.model_dump(mode="json") for r in recs]
    replayed = [SessionChartRecord.model_validate(d) for d in array]
    # Two distinct stacks group by created_turn_id.
    turn_ids = [r.payload.created_turn_id for r in replayed]
    assert turn_ids == ["turn-1", "turn-1", "turn-2"]
    # emitted_at ordering is the replay sort key and is preserved.
    assert [r.emitted_at for r in replayed] == sorted(r.emitted_at for r in replayed)


def test_session_chart_record_requires_session_id_and_emitted_at() -> None:
    with pytest.raises(ValidationError):
        SessionChartRecord(payload=_payload())  # type: ignore[call-arg]


def test_session_chart_record_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SessionChartRecord(
            session_id=new_ulid(),
            payload=_payload(),
            emitted_at="2026-06-09T12:00:00Z",
            chart_count=3,  # type: ignore[call-arg]
        )


# --------------------------------------------------------------------------- #
# Registration — chart-emission wired into ws.py routing
# --------------------------------------------------------------------------- #


def test_chart_emission_registered_agent_to_client() -> None:
    """chart-emission is an agent->client (A.4) message, decode-routable."""
    assert "chart-emission" in CHART_AGENT_TO_CLIENT_PAYLOADS
    assert "chart-emission" in ws.AGENT_TO_CLIENT_PAYLOADS
    assert ws.AGENT_TO_CLIENT_PAYLOADS["chart-emission"] is ChartEmissionPayload
    # And it is in the aggregate ALL_PAYLOADS the decoder consults.
    assert ws.ALL_PAYLOADS["chart-emission"] is ChartEmissionPayload
    # It is NOT a client->agent message.
    assert "chart-emission" not in ws.CLIENT_TO_AGENT_PAYLOADS
