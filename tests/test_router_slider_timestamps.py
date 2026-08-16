"""SLIDER availability index record fold parity (ADR 0078): fetch_slider_timestamps.

The fetch_slider_timestamps twin (a thin dict-enriching wrapper over the shared
_satellite_slider latest_times.json reader) folds onto the record-return output shape
(ADR 0076) as the FIRST live-no-cache record source: the router registers it
uncacheable and read_through short-circuits it (the availability index turns over every
few minutes). This migrates the twin's value-bearing coverage -- the enriched
availability + cadence dict, the ascending sort, the honest typed upstream on a bad
body -- onto the spec-driven surface. The shared _satellite_slider helper is UNCHANGED
(still owned by the animation cluster, which imports the raw list[int] reader directly).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trid3nt_server.agent.tools.fetchers._router import router
from trid3nt_server.agent.tools.fetchers._router.errors import RouterUpstreamError
from trid3nt_server.agent.tools.fetchers._router.executors import http_json
from trid3nt_server.agent.tools.fetchers._router.hooks import slider_timestamps as sth
from trid3nt_server.agent.tools.fetchers._router.spec import load_spec_from_path

SLIDER_SPEC = load_spec_from_path(
    Path(__file__).resolve().parents[1]
    / "trid3nt_server/agent/tools/fetchers/imagery/fetch_slider_timestamps/source.yaml"
)


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _inject_read_through(monkeypatch):
    """Live-no-cache short-circuit: read_through returns fetch_fn() bytes, no bucket."""
    from trid3nt_server.agent.tools.cache import ReadThroughResult, is_cacheable

    def patched(metadata, params, ext, fetch_fn, **kw):
        assert not is_cacheable(metadata), "slider is live-no-cache; must short-circuit"
        return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)

    monkeypatch.setattr(router, "read_through", patched)


# --------------------------------------------------------------------------- #
# Registration + shape (the live-no-cache record surface).
# --------------------------------------------------------------------------- #


def test_slider_promoted_uncacheable_record():
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY["fetch_slider_timestamps"]
    assert entry.metadata.source_class == "slider_timestamps"
    assert entry.metadata.ttl_class == "live-no-cache"
    # The live-no-cache enabler: the promoted tool registers uncacheable (the
    # AtomicToolMetadata validator forbids cacheable=True + live-no-cache).
    assert entry.metadata.cacheable is False
    assert entry.fn.__module__.endswith("_promoted.fetch_slider_timestamps")


def test_slider_shape_is_record():
    assert SLIDER_SPEC.shape == "record"
    assert SLIDER_SPEC.output.layer_type == "record"
    assert SLIDER_SPEC.output.ext == "json"
    assert SLIDER_SPEC.cache.ttl_class == "live-no-cache"


def test_docstring_carried_verbatim():
    # Retrieval-index invariant: the promoted docstring is the twin's, so the
    # FunctionDeclaration description + BM25/dense document text do not shift.
    doc = SLIDER_SPEC.docstring or ""
    assert doc.startswith("List the AVAILABLE CIRA/RAMMB SLIDER imagery frames")
    assert "auto-snap primitive" in doc
    assert "cadence_seconds" in doc


# --------------------------------------------------------------------------- #
# Pure hooks: build_request URL + record enrichment.
# --------------------------------------------------------------------------- #


def test_build_request_url_matches_slider_template():
    plans = sth.build_request(
        SLIDER_SPEC, {"sat": "goes-18", "sector": "conus", "product": "geocolor"}
    )
    assert len(plans) == 1
    assert plans[0].url == (
        "https://rammb-slider.cira.colostate.edu/data/json/"
        "goes-18/conus/geocolor/latest_times.json"
    )
    assert plans[0].headers.get("User-Agent")


def test_record_enriches_and_sorts_ascending():
    # SLIDER ships reverse-chronological; the record sorts ascending.
    body = _body({"timestamps_int": [20260622120500, 20260622120000, 20260622121000]})
    rec = sth.record(
        SLIDER_SPEC, {"sat": "goes-18", "sector": "conus", "product": "geocolor"}, [body]
    )
    assert rec["sat"] == "goes-18"
    assert rec["sector"] == "conus"
    assert rec["product"] == "geocolor"
    assert rec["count"] == 3
    assert rec["timestamps_int"] == [20260622120000, 20260622120500, 20260622121000]
    assert rec["earliest_iso"] == "2026-06-22T12:00:00Z"
    assert rec["latest_iso"] == "2026-06-22T12:10:00Z"
    # median of [300s, 300s] gaps.
    assert rec["cadence_seconds"] == 300.0


def test_record_skips_non_int_entries():
    body = _body({"timestamps_int": [20260622120000, "bad", None, 20260622120500]})
    rec = sth.record(SLIDER_SPEC, {"sat": "jpss", "sector": "conus", "product": "x"}, [body])
    assert rec["count"] == 2
    assert rec["timestamps_int"] == [20260622120000, 20260622120500]


def test_record_empty_index_is_valid_zero_frames():
    # An empty index is a VALID zero-frame result (count 0), NEVER a typed error.
    rec = sth.record(
        SLIDER_SPEC,
        {"sat": "goes-19", "sector": "full_disk", "product": "geocolor"},
        [_body({"timestamps_int": []})],
    )
    assert rec["count"] == 0
    assert rec["timestamps_int"] == []
    assert rec["earliest_iso"] is None
    assert rec["latest_iso"] is None
    assert rec["cadence_seconds"] is None


def test_record_single_frame_cadence_none():
    rec = sth.record(
        SLIDER_SPEC,
        {"sat": "goes-18", "sector": "conus", "product": "geocolor"},
        [_body({"timestamps_int": [20260622120000]})],
    )
    assert rec["count"] == 1
    assert rec["cadence_seconds"] is None


def test_record_missing_key_raises_typed_upstream():
    with pytest.raises(RouterUpstreamError) as exc:
        sth.record(
            SLIDER_SPEC,
            {"sat": "goes-18", "sector": "conus", "product": "geocolor"},
            [_body({"not_timestamps": []})],
        )
    assert exc.value.error_code == "SLIDER_UPSTREAM_ERROR"


def test_record_non_json_raises_typed_upstream():
    with pytest.raises(RouterUpstreamError) as exc:
        sth.record(
            SLIDER_SPEC,
            {"sat": "goes-18", "sector": "conus", "product": "geocolor"},
            [b"<html>redirect</html>"],
        )
    assert exc.value.error_code == "SLIDER_UPSTREAM_ERROR"


# --------------------------------------------------------------------------- #
# End-to-end route() -> availability dict (live-no-cache, not a LayerURI).
# --------------------------------------------------------------------------- #


def test_route_returns_availability_dict(monkeypatch):
    _inject_read_through(monkeypatch)
    monkeypatch.setattr(
        http_json,
        "_get_raw",
        lambda plan: _body({"timestamps_int": [20260622121000, 20260622120000]}),
    )
    result = router.route(
        SLIDER_SPEC, {"sat": "goes-18", "sector": "conus", "product": "geocolor"}
    )
    assert isinstance(result, dict)
    assert result["count"] == 2
    assert result["timestamps_int"] == [20260622120000, 20260622121000]
    assert result["earliest_iso"] == "2026-06-22T12:00:00Z"
    assert result["cadence_seconds"] == 600.0
