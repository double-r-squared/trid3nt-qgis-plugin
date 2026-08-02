"""Offline tests for the LayerURI-envelope seam (ADR 0073), no live calls.

Covers the post-emit envelope hook contract (registration validation of the
``envelope`` hook + ``output.result_model`` pairing/resolution; the honesty-floor
protected-key strip; strict no-op for the priors) and the fetch_high_water_marks
migration (event resolve, states-overlap build_request + US-outside gate, the
bbox-clip / NO_MARKS parse, and the quality/type/datum envelope read back from the
produced FGB -> HighWaterMarksLayerURI). Migrates the value-bearing coverage from
the deleted test_fetch_high_water_marks.py.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_contracts.execution import (
    LAYER_RESULT_MODELS,
    HighWaterMarksLayerURI,
    LayerURI,
)
from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.agent.tools.fetchers._router import registration as reg
from trid3nt_server.agent.tools.fetchers._router import router as _router_mod
from trid3nt_server.agent.tools.fetchers._router.errors import (
    RouterInputError,
    RouterUpstreamError,
)
from trid3nt_server.agent.tools.fetchers._router.executors import vector_fgb
from trid3nt_server.agent.tools.fetchers._router.hooks import (
    HookResolutionError,
    RequestPlan,
    has_hook,
    resolve_hook,
)
from trid3nt_server.agent.tools.fetchers._router.hooks import usgs_stn_hwm as hwm
from trid3nt_server.agent.tools.fetchers._router.spec import compose_specs_from_tree

_SPECS = compose_specs_from_tree()
_HWM = _SPECS["fetch_high_water_marks"]

# One event body + a FilteredHWMs body: two marks inside a small FL bbox, one
# outside, one missing coords (skipped).
_BBOX = [-85.0, 30.0, -84.0, 31.0]


def _events_body() -> bytes:
    return json.dumps(
        [
            {"event_id": 7, "event_name": "2018 Michael"},
            {"event_id": 9, "event_name": "2017 Harvey"},
            {"event_id": 11, "event_name": "2018 Florence"},
        ]
    ).encode()


def _hwms_body() -> bytes:
    return json.dumps(
        [
            {"hwm_id": 1, "latitude": 30.5, "longitude": -84.5, "elev_ft": 12.3,
             "hwmQualityName": "Excellent", "hwmTypeName": "Seed line",
             "verticalDatumName": "NAVD88", "eventName": "2018 Michael",
             "stateName": "Florida"},
            {"hwm_id": 2, "latitude": 30.7, "longitude": -84.2, "elev_ft": 9.1,
             "hwmQualityName": "Unknown/Historical", "hwmTypeName": "Mud line",
             "verticalDatumName": "NGVD29", "eventName": "2018 Michael"},
            {"hwm_id": 3, "latitude": 45.0, "longitude": -120.0, "elev_ft": 5.0,
             "hwmQualityName": "Good", "verticalDatumName": "NAVD88"},
            {"hwm_id": 4, "latitude": None, "longitude": -84.5, "elev_ft": 1.0},
        ]
    ).encode()


# --------------------------------------------------------------------------- #
# Seam mechanics.
# --------------------------------------------------------------------------- #


def test_envelope_hook_registered():
    assert has_hook("usgs_stn_hwm.envelope") and callable(resolve_hook("usgs_stn_hwm.envelope"))


def test_result_model_in_contract_registry():
    assert LAYER_RESULT_MODELS["HighWaterMarksLayerURI"] is HighWaterMarksLayerURI


def test_hwm_spec_declares_envelope_and_result_model():
    assert _HWM.hooks.envelope == "usgs_stn_hwm.envelope"
    assert _HWM.output.result_model == "HighWaterMarksLayerURI"


def test_envelope_is_strict_no_op_for_priors():
    """Exactly one spec declares an envelope hook (the HWM fold); every other
    spec leaves envelope + result_model unset (strict no-op)."""
    with_env = [s.name for s in _SPECS.values() if s.hooks is not None and s.hooks.envelope]
    with_rm = [s.name for s in _SPECS.values() if s.output.result_model]
    assert with_env == ["fetch_high_water_marks"]
    assert with_rm == ["fetch_high_water_marks"]


def test_validate_rejects_unknown_result_model():
    bad = _HWM.model_copy(
        update={"output": _HWM.output.model_copy(update={"result_model": "GhostResult"})}
    )
    with pytest.raises(HookResolutionError):
        reg._validate_hooks(bad)


def test_validate_rejects_envelope_without_result_model():
    bad = _HWM.model_copy(
        update={"output": _HWM.output.model_copy(update={"result_model": None})}
    )
    with pytest.raises(HookResolutionError):
        reg._validate_hooks(bad)


def test_validate_rejects_unknown_envelope_hook():
    bad = _HWM.model_copy(
        update={"hooks": _HWM.hooks.model_copy(update={"envelope": "ghost.envelope"})}
    )
    with pytest.raises(HookResolutionError):
        reg._validate_hooks(bad)


def test_apply_envelope_strips_protected_keys():
    """A hook that tries to re-point uri / flip layer_type cannot: the router
    drops those keys before constructing the result model (honesty floor)."""
    from trid3nt_server.agent.tools.fetchers._router.hooks import register_hook

    @register_hook("test_envelope.evil")
    def _evil(spec, params, layer, data):  # noqa: ANN001
        return {"uri": "s3://evil/hijack.fgb", "layer_type": "raster", "n_marks": 3}

    spec = _HWM.model_copy(
        update={"hooks": _HWM.hooks.model_copy(update={"envelope": "test_envelope.evil"})}
    )
    base = LayerURI(
        layer_id="x", name="n", layer_type="vector", uri="s3://real/ok.fgb",
        style_preset="usgs_high_water_marks",
    )
    out = _router_mod._apply_envelope(spec, {"bbox": _BBOX}, base, b"")
    assert isinstance(out, HighWaterMarksLayerURI)
    assert out.uri == "s3://real/ok.fgb"   # protected: NOT hijacked
    assert out.layer_type == "vector"       # protected: NOT flipped
    assert out.n_marks == 3                 # additive field DID land


# --------------------------------------------------------------------------- #
# HWM hooks (migrated twin coverage).
# --------------------------------------------------------------------------- #


def test_resolve_build_skips_without_event():
    assert hwm.resolve_build(_HWM, {"bbox": _BBOX}) == []
    plans = hwm.resolve_build(_HWM, {"bbox": _BBOX, "event": "Michael"})
    assert len(plans) == 1 and "Events.json" in plans[0].url


def test_resolve_parse_substring_match():
    upd = hwm.resolve_parse(_HWM, {"event": "Michael"}, [_events_body()])
    assert upd == {"event_id": 7, "event_name": "2018 Michael"}


def test_resolve_parse_not_found_and_ambiguous():
    with pytest.raises(RouterInputError) as ei:
        hwm.resolve_parse(_HWM, {"event": "Nonexistent Flood"}, [_events_body()])
    assert ei.value.error_code == "HWM_EVENT_NOT_FOUND"
    with pytest.raises(RouterInputError) as ea:
        hwm.resolve_parse(_HWM, {"event": "2018"}, [_events_body()])
    assert ea.value.error_code == "HWM_EVENT_NOT_FOUND"


def test_build_request_states_scope_and_event_scope():
    # No event -> States filter over the FL-overlapping states.
    plans = hwm.build_request(_HWM, {"bbox": _BBOX})
    assert len(plans) == 1 and "States=" in plans[0].url and "FilteredHWMs" in plans[0].url
    # Event resolved -> Event filter, NO States.
    ev = hwm.build_request(_HWM, {"bbox": _BBOX, "event_id": 7})
    assert "Event=7" in ev[0].url and "States=" not in ev[0].url


def test_build_request_us_outside_gate():
    with pytest.raises(RouterInputError) as ei:
        hwm.build_request(_HWM, {"bbox": [10.0, 40.0, 11.0, 41.0]})  # Europe, no event
    assert ei.value.error_code == "HWM_INPUT_ERROR"


def test_parse_response_clips_and_stamps_quantity():
    feats = hwm.parse_response(_HWM, {"bbox": _BBOX, "event_id": 7}, [_hwms_body()])
    assert len(feats) == 2  # in-bbox marks 1 + 2; mark 3 (OR) + mark 4 (no coords) dropped
    ids = {f["properties"]["hwm_id"] for f in feats}
    assert ids == {1, 2}
    for f in feats:
        assert f["properties"]["quantity"] == "water_surface_elevation"
        assert f["geometry"]["type"] == "Point"


def test_parse_response_no_marks_raises():
    empty = json.dumps([{"hwm_id": 9, "latitude": 0.0, "longitude": 0.0}]).encode()
    with pytest.raises(Exception) as ei:  # RouterEmptyError
        hwm.parse_response(_HWM, {"bbox": _BBOX, "event_id": 7}, [empty])
    assert ei.value.error_code == "HWM_NO_MARKS"
    assert ei.value.retryable is False


def test_parse_response_bad_body_upstream():
    with pytest.raises(RouterUpstreamError) as ei:
        hwm.parse_response(_HWM, {"bbox": _BBOX}, [b"not json"])
    assert ei.value.error_code == "HWM_UPSTREAM_ERROR"


def test_envelope_end_to_end_from_fgb():
    feats = hwm.parse_response(_HWM, {"bbox": _BBOX, "event_id": 7}, [_hwms_body()])
    data = vector_fgb.features_to_fgb_bytes(feats, _HWM, {"bbox": _BBOX})
    base = _router_mod.build_layer_uri(_HWM, {"bbox": _BBOX, "event_name": "2018 Michael"}, "s3://cache/hwm.fgb")
    out = _router_mod._apply_envelope(
        _HWM, {"bbox": _BBOX, "event_id": 7, "event_name": "2018 Michael"}, base, data
    )
    assert isinstance(out, HighWaterMarksLayerURI)
    assert out.n_marks == 2
    assert out.event == "2018 Michael"
    assert out.observed_quantity == "water_surface_elevation"
    assert out.name == "USGS high-water marks (2)"
    assert out.units.startswith("ft")
    assert out.quality_breakdown == {"Excellent": 1, "Unknown/Historical": 1}
    assert out.type_breakdown == {"Seed line": 1, "Mud line": 1}
    assert out.datum_summary == {"NAVD88": 1, "NGVD29": 1}
    # multi-datum + unknown-historical caveats both appended.
    assert any("Unknown/Historical" in c for c in out.caveats)
    assert any("multiple vertical datums" in c for c in out.caveats)
    assert out.uri == "s3://cache/hwm.fgb"


def test_datetime_range_paramtype_validates():
    """The rider ParamType: a 2-element ISO datetime pair coerces + orders."""
    spec = _HWM.model_copy(
        update={
            "params": {
                **_HWM.params,
                "time_range": _HWM.params["event"].model_copy(
                    update={"type": "datetime_range", "required": False}
                ),
            }
        }
    )
    out = _router_mod.validate_params(
        spec, {"bbox": _BBOX, "time_range": ["2018-10-10", "2018-10-12T06:00:00"]}
    )
    assert out["time_range"] == ["2018-10-10T00:00:00", "2018-10-12T06:00:00"]
    with pytest.raises(RouterInputError):
        _router_mod.validate_params(
            spec, {"bbox": _BBOX, "time_range": ["2018-10-12", "2018-10-10"]}
        )
