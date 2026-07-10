"""HTTP-route tests for GET /api/case-list (live-feedback 2026-07-09).

The QGIS local dock's Cases dialog previously could not show ANY cases
until the user pressed Connect, because the case-list envelope only ever
arrives over the WS session (``_emit_case_list`` in ``server.py``). This
route mirrors that envelope's data + user-scoping over plain HTTP so the
dock can populate the dialog before a WS connection exists.

Covered here:
  - route ABSENT (404) outside the local single-user seam
    (``GRACE2_SOLVER_BACKEND=local-docker``), matching the
    ``/api/local-models`` cloud-posture precedent;
  - happy path: a fake Persistence with 2 cases -> 200 + newest-first
    ordering + the wire shape (case_id/title/updated_at/bbox);
  - Persistence unbound -> honest 503 {"error": "persistence unavailable"};
  - the existing /api/health path stays unaffected.
"""

from __future__ import annotations

import asyncio
import json

from grace2_agent import server, tool_catalog_http
from grace2_contracts.case import CaseSummary
from grace2_contracts.common import new_ulid


class _FakeReader:
    """Feed a single HTTP/1.1 GET request, then EOF."""

    def __init__(self, request: bytes):
        self._lines = request.split(b"\r\n")
        self._buf = [ln + b"\r\n" for ln in self._lines]

    async def readline(self):
        if self._buf:
            return self._buf.pop(0)
        return b""


class _FakeWriter:
    def __init__(self):
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes):
        self.buffer.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True


def _request(path: str) -> bytes:
    return (f"GET {path} HTTP/1.1\r\nHost: agent.local\r\n\r\n").encode()


def _status(out: bytes) -> int:
    return int(out.split(b" ", 2)[1])


def _body(out: bytes) -> dict:
    _, _, body = out.partition(b"\r\n\r\n")
    return json.loads(body.decode("utf-8"))


def _dispatch(path: str = "/api/case-list") -> bytes:
    reader = _FakeReader(_request(path))
    writer = _FakeWriter()
    asyncio.run(tool_catalog_http._handle_http(reader, writer))
    assert writer.closed is True
    return bytes(writer.buffer)


def _case(case_id: str, title: str, updated_at: str, bbox=None) -> CaseSummary:
    return CaseSummary(
        case_id=case_id,
        title=title,
        created_at=updated_at,
        updated_at=updated_at,
        bbox=bbox,
    )


class _FakePersistence:
    """Only the one method ``build_case_list_payload`` calls."""

    def __init__(self, cases: list[CaseSummary]):
        self._cases = cases
        self.calls: list[str] = []

    async def list_cases_for_user(self, user_id: str) -> list[CaseSummary]:
        self.calls.append(user_id)
        return list(self._cases)


# ---------------------------------------------------------------------------
# Route gating (cloud posture: 404 like any unknown path)
# ---------------------------------------------------------------------------


def test_route_absent_when_not_local_single_user_mode(monkeypatch):
    monkeypatch.delenv("GRACE2_SOLVER_BACKEND", raising=False)
    out = _dispatch()
    assert _status(out) == 404


def test_route_absent_when_backend_aws_batch(monkeypatch):
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    out = _dispatch()
    assert _status(out) == 404


# ---------------------------------------------------------------------------
# Happy path (local single-user seam armed)
# ---------------------------------------------------------------------------


def test_case_list_happy_path_newest_first(monkeypatch):
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    older = _case(new_ulid(), "Older case", "2026-07-01T00:00:00Z")
    newer = _case(
        new_ulid(),
        "Newer case",
        "2026-07-08T00:00:00Z",
        bbox=(-1.0, -2.0, 3.0, 4.0),
    )
    fake = _FakePersistence([older, newer])
    monkeypatch.setattr(server, "get_persistence", lambda: fake)

    out = _dispatch()
    assert _status(out) == 200
    payload = _body(out)
    assert [c["case_id"] for c in payload["cases"]] == [
        newer.case_id,
        older.case_id,
    ]
    assert payload["cases"][0]["title"] == "Newer case"
    assert payload["cases"][0]["bbox"] == [-1.0, -2.0, 3.0, 4.0]
    assert payload["cases"][1]["bbox"] is None
    assert payload["cases"][0]["updated_at"].startswith("2026-07-08")
    # Scoped to the local single fixed user, not a per-client hint.
    from grace2_agent.auth_handshake import LOCAL_SINGLE_USER_ID

    assert fake.calls == [LOCAL_SINGLE_USER_ID]


def test_case_list_empty_is_ok(monkeypatch):
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    monkeypatch.setattr(server, "get_persistence", lambda: _FakePersistence([]))
    out = _dispatch()
    assert _status(out) == 200
    assert _body(out) == {"cases": []}


# ---------------------------------------------------------------------------
# Persistence unbound -> honest 503
# ---------------------------------------------------------------------------


def test_case_list_persistence_unbound_503(monkeypatch):
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    monkeypatch.setattr(server, "get_persistence", lambda: None)
    out = _dispatch()
    assert _status(out) == 503
    assert _body(out)["error"] == "persistence unavailable"


# ---------------------------------------------------------------------------
# Sibling routes unaffected
# ---------------------------------------------------------------------------


def test_case_list_route_does_not_perturb_health():
    out = _dispatch("/api/health")
    assert _status(out) == 200
    assert b'"ok":true' in out
