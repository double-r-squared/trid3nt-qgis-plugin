"""HTTP-route wiring for ``/api/probe-point`` on the layer door.

Dispatch only - the sampling logic is covered where it lives. The route is served
UNCONDITIONALLY; a missing or invalid field is a typed 400 with the core never
invoked; a typed core error is an honest 404 or 400."""

from __future__ import annotations

import json

import pytest

from door_client import drive

from trid3nt_server.server.protocol.doors import layers
from trid3nt_server.tools.derive.probe_point.probe_point import (
    ProbePointCaseNotFoundError,
    ProbePointInputError,
)


def _post(path: str, body: bytes) -> tuple:
    return ("POST", path, body)


def _get(path: str) -> tuple:
    return ("GET", path, None)


def _drive(spec: tuple):
    return drive(*spec)


def _status(out) -> int:
    return out.status


def _body_json(out) -> dict:
    return out.json()


@pytest.fixture(autouse=True)
def _local_mode(monkeypatch):
    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", "local-docker")


# The route is unconditional: no env arms it and no posture withholds it.


def test_probe_point_route_served_without_env_arming(monkeypatch):
    """Served with no env arming at all.

    ``b"{}"`` reaching the handler's field validation proves dispatch serves the
    route - an absent route would have 404ed before any body parsing."""
    monkeypatch.delenv("TRID3NT_SOLVER_BACKEND", raising=False)
    out = _drive(_post("/api/probe-point", b"{}"))
    assert _status(out) == 400
    assert "case_id" in _body_json(out)["error"]




def test_probe_point_post_happy_path(monkeypatch):
    calls: list[dict] = []
    result = {
        "status": "ok",
        "point": {"lon": -85.42, "lat": 29.95},
        "case_id": "01CASE",
        "results": [
            {"layer_id": "l-1", "name": "Plume concentration", "value": 12.3, "units": "mg/L"},
            {
                "name": "flood depth",
                "series": [
                    {"label": "step 1", "value": 0.02},
                    {"label": "step 2", "value": 0.15},
                ],
                "units": "m",
                "layer_ids": ["f-1", "f-2"],
            },
        ],
        "truncated": False,
        "computed_at": "2026-07-11T00:00:00+00:00",
    }

    async def _fake_probe(**kwargs):
        calls.append(kwargs)
        return dict(result)

    monkeypatch.setattr(layers, "_probe_point_fn", lambda: _fake_probe)

    body = json.dumps(
        {"case_id": "01CASE", "lon": -85.42, "lat": 29.95}
    ).encode()
    out = _drive(_post("/api/probe-point", body))
    assert _status(out) == 200
    assert _body_json(out) == result
    assert calls == [{"point": (-85.42, 29.95), "case_id": "01CASE"}]


def test_probe_point_post_missing_case_id_400(monkeypatch):
    def _never():  # pragma: no cover
        raise AssertionError("probe fn must not be resolved on a bad request")

    monkeypatch.setattr(layers, "_probe_point_fn", _never)
    body = json.dumps({"lon": -85.42, "lat": 29.95}).encode()
    out = _drive(_post("/api/probe-point", body))
    assert _status(out) == 400
    assert "case_id" in _body_json(out)["error"]


def test_probe_point_post_missing_lon_400(monkeypatch):
    def _never():  # pragma: no cover
        raise AssertionError("probe fn must not be resolved on a bad request")

    monkeypatch.setattr(layers, "_probe_point_fn", _never)
    body = json.dumps({"case_id": "01CASE", "lat": 29.95}).encode()
    out = _drive(_post("/api/probe-point", body))
    assert _status(out) == 400
    assert "lon" in _body_json(out)["error"]


def test_probe_point_post_non_json_body_400():
    out = _drive(_post("/api/probe-point", b"not json"))
    assert _status(out) == 400
    assert "JSON" in _body_json(out)["error"]


def test_probe_point_post_case_not_found_404(monkeypatch):
    async def _fake_probe(**kwargs):
        raise ProbePointCaseNotFoundError("case '01GONE' not found.")

    monkeypatch.setattr(layers, "_probe_point_fn", lambda: _fake_probe)
    body = json.dumps({"case_id": "01GONE", "lon": -85.42, "lat": 29.95}).encode()
    out = _drive(_post("/api/probe-point", body))
    assert _status(out) == 404
    assert _body_json(out)["error"] == "case '01GONE' not found."


def test_probe_point_post_invalid_point_400(monkeypatch):
    async def _fake_probe(**kwargs):
        raise ProbePointInputError("lon/lat out of range")

    monkeypatch.setattr(layers, "_probe_point_fn", lambda: _fake_probe)
    body = json.dumps({"case_id": "01CASE", "lon": 999.0, "lat": 29.95}).encode()
    out = _drive(_post("/api/probe-point", body))
    assert _status(out) == 400
    assert "lon/lat" in _body_json(out)["error"]


def test_probe_point_post_unexpected_error_500(monkeypatch):
    async def _fake_probe(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(layers, "_probe_point_fn", lambda: _fake_probe)
    body = json.dumps({"case_id": "01CASE", "lon": -85.42, "lat": 29.95}).encode()
    out = _drive(_post("/api/probe-point", body))
    assert _status(out) == 500




def test_probe_point_route_does_not_perturb_catalog():
    out = _drive(_get("/api/tool-catalog"))
    assert _status(out) == 200
