"""LANE S -- the Mode 2 offer-to-add loop, server half (§F.1.2 Mode 2).

Wires the full loop OFFLINE (no network, no live model):

    mode2-candidate emits + registers a pending offer (keyed by candidate_id)
      -> the plugin card replies catalog-addition-response (request_id ==
         candidate_id)
      -> on ACCEPT the server drafts/completes the entry (probe stubbed),
         APPENDs it to the user-overlay catalog, resets the catalog cache
      -> search_data_catalog immediately finds it.
    REJECT / cancel just resolves (drops) the pending offer.
    The pending-offer registry is BOUNDED (TTL prune + cap) so an unanswered
    offer never leaks (proceeds, never hangs).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server.tools.discovery import catalog_common as cc
from trid3nt_server.tools.discovery import search_data_catalog as sdc_mod
from trid3nt_server.tools.discovery.catalog_common import load_catalog, reset_catalog_cache
from trid3nt_server.tools.cache import ReadThroughResult
from trid3nt_contracts.ws import (
    CatalogAdditionResponsePayload,
    ProbeFindings,
    SuggestedCatalogEntry,
)


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        try:
            self.sent.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            self.sent.append(raw)


GOV_PAGE = {
    "url": "https://data.example.gov/dataset/spill-monitoring",
    "title": "Spill Monitoring Dataset",
    "content": (
        '<script type="application/ld+json">{"@type":"Dataset"}</script>'
        '<a href="/data.csv">Download CSV</a>'
    ),
}


@pytest.fixture(autouse=True)
def _overlay_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRID3NT_USER_CATALOG_YAML", str(tmp_path / "user_catalog.yaml"))
    reset_catalog_cache()
    agent_server._PENDING_CATALOG_OFFERS.clear()
    # No live probe in tests -- degrade to empty findings.
    async def _no_probe(url):
        return ProbeFindings()

    monkeypatch.setattr(agent_server, "_probe_catalog_endpoint", _no_probe)
    # Bypass the search read-through cache so a fresh load is always consulted.
    monkeypatch.setattr(
        sdc_mod,
        "read_through",
        lambda metadata, params, ext, fetch_fn, **kw: ReadThroughResult(
            uri=None, data=fetch_fn(), hit=False
        ),
    )
    try:
        yield
    finally:
        reset_catalog_cache()
        agent_server._PENDING_CATALOG_OFFERS.clear()


def _state() -> agent_server.SessionState:
    return agent_server.SessionState(session_id="01SESSIONULID0000000000009")


def _emit_candidate(state) -> tuple[_FakeSocket, str]:
    """Run the real mode2 emission; return (socket, candidate_id)."""
    sock = _FakeSocket()
    asyncio.run(agent_server._maybe_emit_mode2_candidate(sock, state, GOV_PAGE))
    cards = [m for m in sock.sent if m.get("type") == "mode2-candidate"]
    assert cards, "expected a mode2-candidate envelope"
    return sock, cards[0]["payload"]["candidate"]["candidate_id"]


# ---------------------------------------------------------------------------
# emission registers a bounded pending offer.
# ---------------------------------------------------------------------------


def test_mode2_emit_registers_pending_offer():
    state = _state()
    _sock, cid = _emit_candidate(state)
    assert cid in agent_server._PENDING_CATALOG_OFFERS
    owner, candidate, _exp = agent_server._PENDING_CATALOG_OFFERS[cid]
    assert owner == state.session_id
    assert candidate["url"] == GOV_PAGE["url"]


# ---------------------------------------------------------------------------
# accept -> overlay append -> search finds it; offer resolved.
# ---------------------------------------------------------------------------


def test_accept_appends_entry_and_search_finds_it():
    state = _state()
    sock, cid = _emit_candidate(state)

    car = CatalogAdditionResponsePayload(
        request_id=cid,
        decision="accept",
        edited_catalog_entry=SuggestedCatalogEntry(
            id="gov-spill-monitoring-user",
            name="Spill Monitoring Feed",
            description="Government spill monitoring CSV feed for oil spills.",
            urls=[GOV_PAGE["url"]],
            source_class="spill",
            how_to_use="Fetch the CSV then parse spill events.",
        ),
    )
    asyncio.run(agent_server._handle_catalog_addition_response(sock, state, car))

    # offer resolved (dropped).
    assert cid not in agent_server._PENDING_CATALOG_OFFERS
    # entry landed in the user overlay + the catalog cache was reset.
    ids = {e.id for e in load_catalog()}
    assert "gov-spill-monitoring-user" in ids
    added = next(e for e in load_catalog() if e.id == "gov-spill-monitoring-user")
    assert added.status == "active"
    assert added.credential_tier == 1  # tier-1 draft (no secret ref)

    # search_data_catalog immediately surfaces it (fresh topic, no stale cache).
    results = sdc_mod.search_data_catalog(topic="spill monitoring")
    assert any(r["id"] == "gov-spill-monitoring-user" for r in results), [
        r["id"] for r in results
    ]


def test_accept_without_edit_drafts_from_candidate():
    """Accept with NO edited entry drafts from the stored mode2 candidate."""
    state = _state()
    sock, cid = _emit_candidate(state)
    car = CatalogAdditionResponsePayload(request_id=cid, decision="accept")
    asyncio.run(agent_server._handle_catalog_addition_response(sock, state, car))

    entries = [e for e in load_catalog() if GOV_PAGE["url"] in e.urls]
    assert entries, "an entry drafted from the candidate url should be appended"
    assert entries[0].status == "active"


# ---------------------------------------------------------------------------
# reject / cancel just resolves the offer -- no overlay write.
# ---------------------------------------------------------------------------


def test_reject_resolves_offer_without_appending():
    state = _state()
    sock, cid = _emit_candidate(state)
    base = {e.id for e in load_catalog()}

    car = CatalogAdditionResponsePayload(
        request_id=cid, decision="reject", reject_reason="not authoritative"
    )
    asyncio.run(agent_server._handle_catalog_addition_response(sock, state, car))

    assert cid not in agent_server._PENDING_CATALOG_OFFERS, "offer dropped"
    reset_catalog_cache()
    assert {e.id for e in load_catalog()} == base, "reject must not append"


def test_cancel_resolves_offer_without_appending():
    state = _state()
    sock, cid = _emit_candidate(state)
    base = {e.id for e in load_catalog()}
    car = CatalogAdditionResponsePayload(request_id=cid, cancelled=True)
    asyncio.run(agent_server._handle_catalog_addition_response(sock, state, car))
    assert cid not in agent_server._PENDING_CATALOG_OFFERS
    reset_catalog_cache()
    assert {e.id for e in load_catalog()} == base


def test_accept_with_no_offer_and_no_edit_is_ignored():
    """An accept for an unknown/expired offer with no edited entry cannot draft."""
    state = _state()
    sock = _FakeSocket()
    base = {e.id for e in load_catalog()}
    from trid3nt_contracts import new_ulid

    car = CatalogAdditionResponsePayload(request_id=new_ulid(), decision="accept")
    asyncio.run(agent_server._handle_catalog_addition_response(sock, state, car))
    reset_catalog_cache()
    assert {e.id for e in load_catalog()} == base, "nothing to draft -> no append"


# ---------------------------------------------------------------------------
# bounded registry: TTL prune + wrong-session refusal.
# ---------------------------------------------------------------------------


def test_offer_registry_ttl_prunes_stale(monkeypatch):
    monkeypatch.setenv("TRID3NT_CATALOG_OFFER_TTL_S", "0.01")
    agent_server._register_pending_catalog_offer("S", "REQ-TTL", {"url": "u"})
    assert "REQ-TTL" in agent_server._PENDING_CATALOG_OFFERS
    import time as _t

    _t.sleep(0.02)
    agent_server._prune_catalog_offers()
    assert "REQ-TTL" not in agent_server._PENDING_CATALOG_OFFERS, "stale offer pruned"


def test_pop_offer_refuses_wrong_session():
    agent_server._register_pending_catalog_offer("SESSION-A", "REQ-X", {"url": "u"})
    assert agent_server._pop_pending_catalog_offer("SESSION-B", "REQ-X") is None
    # still present (not consumed by the wrong session).
    assert "REQ-X" in agent_server._PENDING_CATALOG_OFFERS
    got = agent_server._pop_pending_catalog_offer("SESSION-A", "REQ-X")
    assert got == {"url": "u"}
    assert "REQ-X" not in agent_server._PENDING_CATALOG_OFFERS


def test_offer_registry_capped(monkeypatch):
    agent_server._PENDING_CATALOG_OFFERS.clear()
    monkeypatch.setattr(agent_server, "_CATALOG_OFFER_MAX", 3)
    for i in range(5):
        agent_server._register_pending_catalog_offer("S", f"REQ-{i}", {"url": str(i)})
    assert len(agent_server._PENDING_CATALOG_OFFERS) <= 3
    # the newest survive; the oldest were evicted.
    assert "REQ-4" in agent_server._PENDING_CATALOG_OFFERS
    assert "REQ-0" not in agent_server._PENDING_CATALOG_OFFERS
