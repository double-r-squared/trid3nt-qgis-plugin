"""The CASE door: the cold case list, over plain HTTP.

The same rows the WebSocket session emits, so a client can populate its dialog
BEFORE a connection exists. The WS path scopes rows to the handshake's user; a
cold caller has no handshake, and this build collapses every connection onto one
fixed local user id, so it resolves the identical id anyway."""

from __future__ import annotations

from typing import Any

from aiohttp import web

from trid3nt_server.server.protocol.doors.transport import (
    HttpError, failure, json_reply,
)


class _PersistenceUnavailable(Exception):
    """Persistence is unbound; the case list cannot be sourced (-> 503)."""


#: The case-list row the client reads: the envelope serializes every Case field,
#: and these four are what a left-rail row renders. A case with no bbox carries
#: an honest None.
_ROW_FIELDS = ("case_id", "title", "updated_at", "bbox")


async def build_case_list_payload() -> dict[str, Any]:
    """Assemble the case-list payload newest-first, through the same
    persistence call the WS path makes; unbound persistence raises, so the route
    answers an honest 503 rather than a fabricated empty list."""
    from trid3nt_server.credentials.auth_handshake import LOCAL_SINGLE_USER_ID
    from trid3nt_server.server import get_persistence

    persistence = get_persistence()
    if persistence is None:
        raise _PersistenceUnavailable("persistence unavailable")

    from trid3nt_contracts.case import CaseListEnvelopePayload

    cases = await persistence.list_cases_for_user(LOCAL_SINGLE_USER_ID)
    serialized = CaseListEnvelopePayload(cases=cases).model_dump(mode="json")
    rows = [{k: row.get(k) for k in _ROW_FIELDS} for row in serialized["cases"]]
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return {"cases": rows}


@failure("case list failed")
async def _case_list(_request: web.Request) -> web.Response:
    """``GET /api/case-list``: the cold case list, for a client with no
    WebSocket session yet."""
    try:
        return json_reply(await build_case_list_payload())
    except _PersistenceUnavailable as exc:
        raise HttpError(503, str(exc)) from exc


def add_routes(app: web.Application) -> None:
    """Register the case door's route on the door's one app."""
    app.router.add_get("/api/case-list", _case_list, allow_head=False)
