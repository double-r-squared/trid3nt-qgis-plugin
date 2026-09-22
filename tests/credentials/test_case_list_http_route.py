"""HTTP-route tests for GET /api/case-list.

The route mirrors the WS case-list envelope's data and user scoping over plain
HTTP, so a dock can populate its dialog before a WS connection exists. It is
served UNCONDITIONALLY - ``TRID3NT_SOLVER_BACKEND`` does not gate it - newest
first, and an unbound Persistence is an honest 503 rather than an empty list."""

from __future__ import annotations

from door_client import drive

from trid3nt_server import server
from trid3nt_contracts.case import CaseSummary
from trid3nt_contracts.common import new_ulid


def _status(out) -> int:
    return out.status


def _body(out) -> dict:
    return out.json()


def _dispatch(path: str = "/api/case-list"):
    return drive("GET", path)


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


# The route is unconditional: no env arms it and no posture withholds it.


def test_route_served_when_backend_env_unset(monkeypatch):
    monkeypatch.delenv("TRID3NT_SOLVER_BACKEND", raising=False)
    case = _case(new_ulid(), "Env-unset case", "2026-07-09T00:00:00Z")
    fake = _FakePersistence([case])
    monkeypatch.setattr(server, "get_persistence", lambda: fake)

    out = _dispatch()
    assert _status(out) == 200
    payload = _body(out)
    assert [c["case_id"] for c in payload["cases"]] == [case.case_id]
    # Scoped to the fixed local single user without any env arming.
    from trid3nt_server.credentials.auth_handshake import LOCAL_SINGLE_USER_ID

    assert fake.calls == [LOCAL_SINGLE_USER_ID]




def test_case_list_happy_path_newest_first(monkeypatch):
    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", "local-docker")
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
    from trid3nt_server.credentials.auth_handshake import LOCAL_SINGLE_USER_ID

    assert fake.calls == [LOCAL_SINGLE_USER_ID]


def test_case_list_empty_is_ok(monkeypatch):
    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", "local-docker")
    monkeypatch.setattr(server, "get_persistence", lambda: _FakePersistence([]))
    out = _dispatch()
    assert _status(out) == 200
    assert _body(out) == {"cases": []}




def test_case_list_persistence_unbound_503(monkeypatch):
    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", "local-docker")
    monkeypatch.setattr(server, "get_persistence", lambda: None)
    out = _dispatch()
    assert _status(out) == 503
    assert _body(out)["error"] == "persistence unavailable"




def test_case_list_route_does_not_perturb_catalog():
    out = _dispatch("/api/tool-catalog")
    assert _status(out) == 200
