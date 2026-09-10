"""Offline tests for the shared call shim, with no live calls.

The three behaviours the library does not have and the shim supplies: a JSON error
document RETURNED as data becomes a typed upstream error carrying the upstream
text verbatim; a retryable status backs off and recovers; and the provider's own
``Retry-After`` is obeyed. Plus the cache placement the provenance rules require."""

from __future__ import annotations

import os
import time

import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterUpstreamError
from trid3nt_server.tools.fetchers._router.hooks import hyriver
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

_SPEC = compose_specs_from_tree()["fetch_high_water_marks"]


def test_data_passes_through():
    assert hyriver.hyriver_call(_SPEC, "x", lambda: [{"hwm_id": 1}]) == [{"hwm_id": 1}]


def test_rfc7807_error_document_is_raised_verbatim():
    doc = {"type": "about:blank", "title": "Not Found", "status": 404, "detail": "Not Found"}
    with pytest.raises(RouterUpstreamError) as ei:
        hyriver.hyriver_call(_SPEC, "nldi lookups", lambda: [doc])
    assert ei.value.error_code == "HWM_UPSTREAM_ERROR"
    assert "about:blank" in str(ei.value) and "Not Found" in str(ei.value)


def test_esri_error_envelope_is_raised_verbatim():
    env = {"error": {"code": 400, "message": "Unable to complete operation."}}
    with pytest.raises(RouterUpstreamError) as ei:
        hyriver.hyriver_call(_SPEC, "esri query", lambda: env)
    assert "Unable to complete operation." in str(ei.value)


def test_retryable_status_recovers(monkeypatch):
    monkeypatch.setattr(hyriver, "_BACKOFF_BASE_S", 0.0)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            return {"title": "Too Many Requests", "status": 429}
        return "DATA"

    assert hyriver.hyriver_call(_SPEC, "flaky", flaky) == "DATA"
    assert calls["n"] == 3


def test_retry_after_in_the_body_is_obeyed():
    t0 = time.time()
    with pytest.raises(RouterUpstreamError):
        hyriver.hyriver_call(
            _SPEC, "throttled", lambda: {"title": "Slow down", "status": 503, "Retry-After": 1}
        )
    # Four waits of the server's own 1 s, not the exponential default.
    assert 3.5 <= time.time() - t0 < 7.0


def test_non_retryable_status_fails_on_the_first_try(monkeypatch):
    monkeypatch.setattr(hyriver, "_BACKOFF_BASE_S", 0.0)
    calls = {"n": 0}

    def gone():
        calls["n"] += 1
        raise RuntimeError("ServiceError: status: 404 not found")

    with pytest.raises(RouterUpstreamError):
        hyriver.hyriver_call(_SPEC, "gone", gone)
    assert calls["n"] == 1


def test_raised_library_error_keeps_its_text():
    body = 'URL: https://example/x\nERROR: {"status": 400, "title": "Bad Request"}'

    def raiser():
        raise RuntimeError(body)

    with pytest.raises(RouterUpstreamError) as ei:
        hyriver.hyriver_call(_SPEC, "raiser", raiser)
    assert "https://example/x" in str(ei.value) and "Bad Request" in str(ei.value)


def test_cache_sits_under_the_runs_dir_with_an_explicit_expiry(monkeypatch, tmp_path):
    for var in ("HYRIVER_CACHE_NAME", "HYRIVER_CACHE_NAME_HTTP", "HYRIVER_CACHE_EXPIRE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    hyriver.configure_cache()
    assert os.environ["HYRIVER_CACHE_NAME"].startswith(str(tmp_path / "hyriver-cache"))
    assert os.environ["HYRIVER_CACHE_NAME_HTTP"].startswith(str(tmp_path / "hyriver-cache"))
    assert os.environ["HYRIVER_CACHE_EXPIRE"] == str(hyriver._CACHE_EXPIRE_S)


def test_a_connection_that_never_answered_is_retried(monkeypatch):
    """A reset connection names no status; its exception class is the whole signal."""
    import aiohttp

    monkeypatch.setattr(hyriver, "_BACKOFF_BASE_S", 0.0)
    calls = {"n": 0}

    def reset():
        calls["n"] += 1
        if calls["n"] < 3:
            raise aiohttp.ServerDisconnectedError("Connection reset by peer")
        return "DATA"

    assert hyriver.hyriver_call(_SPEC, "reset", reset) == "DATA"
    assert calls["n"] == 3


def test_an_arcgis_error_page_names_its_status_through_the_markup(monkeypatch):
    """ArcGIS prints the status inside HTML; the classifier reads through it."""
    monkeypatch.setattr(hyriver, "_BACKOFF_BASE_S", 0.0)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("<b>Error: </b>Error performing query operation<br/>"
                               "<b>Code: </b>500<br/>")
        return "DATA"

    assert hyriver.hyriver_call(_SPEC, "arcgis", flaky) == "DATA"
    assert calls["n"] == 3
