"""File-backed Persistence tests (job-0161, sprint-12-mega Wave 4.6).

The MongoDB Atlas MCP server is the production LLM-facing DB seam (FR-AS-4);
for LOCAL DEV without Atlas/MCP, ``FileMCPClient`` satisfies the same
``MCPClientProtocol`` against per-collection JSON files. These tests exercise
that substrate through the unmodified ``Persistence`` wrapper to prove the
file-backed shim is interchangeable with the live MCP path.

Coverage:
- ``test_file_mcp_round_trip_case`` — Case upsert + get round-trips and
  writes a JSON file in the expected location.
- ``test_file_mcp_list_cases`` — list_cases_for_user returns inserted Cases.
- ``test_file_mcp_archive_then_delete`` — soft-archive then soft-delete
  flips ``status`` and persists across a fresh ``FileMCPClient`` instance.
- ``test_file_mcp_chat_round_trip`` — append_chat_message + get_session_state
  rehydrates with ordered chat history.
- ``test_file_mcp_atomic_writes`` — interrupted write (a synthetic crash
  between tmp-write and rename) does not corrupt the on-disk store.
- ``test_is_dev_persistence_enabled_defaults`` — default-on semantics:
  no env vars set + no MCP wired → enabled; ``TRID3NT_DEV_PERSISTENCE=0``
  disables; ``TRID3NT_MONGO_MCP_STDIO=1`` defers to real MCP.
- ``test_init_persistence_from_env_engages_file_fallback`` — server-side
  wiring: ``init_persistence_from_env`` with no MCP env vars + a tmpdir
  override engages FilePersistence and binds the singleton.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trid3nt_server.persistence import (
    CASES_COLLECTION,
    CHAT_COLLECTION,
    DEFAULT_DATABASE,
    DEV_PERSISTENCE_DIR_ENV,
    DEV_PERSISTENCE_ENABLED_ENV,
    FileMCPClient,
    Persistence,
    is_dev_persistence_enabled,
    make_file_persistence,
)
from trid3nt_contracts.case import CaseChatMessage, CaseSummary
from trid3nt_contracts.common import new_ulid


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _fresh_case(title: str = "Hurricane Ian — Fort Myers flood scenario") -> CaseSummary:
    return CaseSummary(
        case_id=new_ulid(),
        title=title,
        created_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        status="active",
        bbox=(-82.0, 26.5, -81.8, 26.7),
        primary_hazard="flood",
        layer_summary=["nlcd-fort-myers", "flood-depth-01HX"],
    )


def _fresh_chat(case_id: str, role: str, content: str, *, when: datetime) -> CaseChatMessage:
    return CaseChatMessage(
        message_id=new_ulid(),
        case_id=case_id,
        role=role,
        content=content,
        created_at=when,
    )


# --------------------------------------------------------------------------- #
# Case CRUD round-trips
# --------------------------------------------------------------------------- #


def test_file_mcp_round_trip_case(tmp_path: Path) -> None:
    """Upsert a Case via FilePersistence; read it back; assert JSON file landed."""
    client = FileMCPClient(base_dir=tmp_path)
    p = Persistence(client)
    case = _fresh_case()

    saved = asyncio.run(p.upsert_case(case))
    assert saved.case_id == case.case_id

    fetched = asyncio.run(p.get_case(case.case_id))
    assert fetched is not None
    assert fetched.case_id == case.case_id
    assert fetched.title == case.title
    assert fetched.primary_hazard == "flood"
    assert fetched.bbox == (-82.0, 26.5, -81.8, 26.7)

    # JSON file landed at the expected path
    expected_path = tmp_path / DEFAULT_DATABASE / f"{CASES_COLLECTION}.json"
    assert expected_path.exists(), f"projects.json not written at {expected_path}"
    with expected_path.open("r", encoding="utf-8") as fh:
        store = json.load(fh)
    assert case.case_id in store
    assert store[case.case_id]["title"] == case.title


def test_file_mcp_list_cases(tmp_path: Path) -> None:
    """Multiple owned Cases are listed for their owner; others are excluded.

    job-0252 (OQ-0115-CASE-USER-LINK): the ``$exists:false`` leak clause is
    gone — Cases are owner-scoped on the file substrate too.
    """
    p = Persistence(FileMCPClient(base_dir=tmp_path))
    owner = new_ulid()
    case_a = _fresh_case("Case A")
    case_b = _fresh_case("Case B")
    asyncio.run(p.upsert_case(case_a, owner_user_id=owner))
    asyncio.run(p.upsert_case(case_b, owner_user_id=owner))

    cases = asyncio.run(p.list_cases_for_user(owner))
    titles = {c.title for c in cases}
    assert "Case A" in titles
    assert "Case B" in titles

    # A different user sees none of them (no leak).
    assert asyncio.run(p.list_cases_for_user(new_ulid())) == []


def test_file_mcp_rename_case(tmp_path: Path) -> None:
    """Rename a Case (re-upsert with new title) persists across substrate restart."""
    base = tmp_path
    p = Persistence(FileMCPClient(base_dir=base))
    case = _fresh_case("Original title")
    asyncio.run(p.upsert_case(case))

    renamed = case.model_copy(update={"title": "Renamed title"})
    asyncio.run(p.upsert_case(renamed))

    # Fresh client over the same dir to prove persistence across restart.
    p2 = Persistence(FileMCPClient(base_dir=base))
    fetched = asyncio.run(p2.get_case(case.case_id))
    assert fetched is not None
    assert fetched.title == "Renamed title"


def test_file_mcp_archive_then_delete(tmp_path: Path) -> None:
    """Archive flips status; delete flips again; both persist across restart."""
    base = tmp_path
    p = Persistence(FileMCPClient(base_dir=base))
    case = _fresh_case()
    asyncio.run(p.upsert_case(case))

    asyncio.run(p.archive_case(case.case_id))
    p2 = Persistence(FileMCPClient(base_dir=base))
    fetched = asyncio.run(p2.get_case(case.case_id))
    assert fetched is not None and fetched.status == "archived"

    asyncio.run(p2.delete_case(case.case_id))
    p3 = Persistence(FileMCPClient(base_dir=base))
    fetched = asyncio.run(p3.get_case(case.case_id))
    assert fetched is not None and fetched.status == "deleted"


# --------------------------------------------------------------------------- #
# Chat history + session state
# --------------------------------------------------------------------------- #


def test_file_mcp_chat_round_trip(tmp_path: Path) -> None:
    """Append chat messages; get_session_state hydrates in order."""
    p = Persistence(FileMCPClient(base_dir=tmp_path))
    case = _fresh_case()
    asyncio.run(p.upsert_case(case))

    m1 = _fresh_chat(case.case_id, "user", "first turn",
                     when=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc))
    m2 = _fresh_chat(case.case_id, "agent", "ack",
                     when=datetime(2026, 6, 8, 12, 0, 1, tzinfo=timezone.utc))
    m3 = _fresh_chat(case.case_id, "user", "second turn",
                     when=datetime(2026, 6, 8, 12, 0, 2, tzinfo=timezone.utc))
    asyncio.run(p.append_chat_message(m1))
    asyncio.run(p.append_chat_message(m2))
    asyncio.run(p.append_chat_message(m3))

    # chat_messages.json was written
    chat_path = tmp_path / DEFAULT_DATABASE / f"{CHAT_COLLECTION}.json"
    assert chat_path.exists()

    state = asyncio.run(p.get_session_state(case.case_id))
    assert state.case.case_id == case.case_id
    assert [m.content for m in state.chat_history] == [
        "first turn",
        "ack",
        "second turn",
    ]


# --------------------------------------------------------------------------- #
# Atomic-write semantics
# --------------------------------------------------------------------------- #


def test_file_mcp_atomic_writes_survive_partial_tmp(tmp_path: Path) -> None:
    """An orphaned .tmp file from a crashed write does NOT corrupt the store.

    Simulates: a previous run crashed AFTER writing the tmp file but BEFORE
    the os.replace landed. The next call must read the committed file, not
    the partial tmp.
    """
    client = FileMCPClient(base_dir=tmp_path)
    p = Persistence(client)
    case_a = _fresh_case("committed")
    asyncio.run(p.upsert_case(case_a))

    coll_path = tmp_path / DEFAULT_DATABASE / f"{CASES_COLLECTION}.json"
    assert coll_path.exists()

    # Simulate an orphaned tmp from a partial earlier write — corrupt content.
    orphan_tmp = coll_path.with_suffix(coll_path.suffix + ".tmp")
    orphan_tmp.write_text("this is not valid json {{{", encoding="utf-8")

    # A fresh client over the same dir should still read the committed file.
    p2 = Persistence(FileMCPClient(base_dir=tmp_path))
    fetched = asyncio.run(p2.get_case(case_a.case_id))
    assert fetched is not None
    assert fetched.title == "committed"

    # And a subsequent successful write replaces the committed file (the
    # orphan tmp is harmless — it isn't read, and gets overwritten on next
    # write of the same collection).
    case_b = _fresh_case("second commit")
    asyncio.run(p2.upsert_case(case_b))
    fetched_b = asyncio.run(p2.get_case(case_b.case_id))
    assert fetched_b is not None


# --------------------------------------------------------------------------- #
# is_dev_persistence_enabled() precedence
# --------------------------------------------------------------------------- #


def test_is_dev_persistence_enabled_default_on_when_mcp_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env vars + no MCP wired → enabled by default."""
    monkeypatch.delenv(DEV_PERSISTENCE_ENABLED_ENV, raising=False)
    monkeypatch.delenv("TRID3NT_MONGO_MCP_STDIO", raising=False)
    monkeypatch.delenv("TRID3NT_MONGO_MCP_URL", raising=False)
    assert is_dev_persistence_enabled() is True


def test_is_dev_persistence_enabled_off_when_explicitly_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TRID3NT_DEV_PERSISTENCE=0`` disables even without MCP wired."""
    monkeypatch.setenv(DEV_PERSISTENCE_ENABLED_ENV, "0")
    monkeypatch.delenv("TRID3NT_MONGO_MCP_STDIO", raising=False)
    assert is_dev_persistence_enabled() is False


def test_is_dev_persistence_enabled_defers_to_real_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TRID3NT_MONGO_MCP_STDIO=1`` → defer to real MCP (default off)."""
    monkeypatch.delenv(DEV_PERSISTENCE_ENABLED_ENV, raising=False)
    monkeypatch.setenv("TRID3NT_MONGO_MCP_STDIO", "1")
    assert is_dev_persistence_enabled() is False


def test_is_dev_persistence_enabled_explicit_wins_over_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``TRID3NT_DEV_PERSISTENCE=1`` engages even if MCP is also set.

    The server-side init_persistence_from_env still prefers real MCP when
    TRID3NT_MONGO_MCP_STDIO=1 (because that branch returns first); this test
    just locks the env-var precedence inside the helper itself.
    """
    monkeypatch.setenv(DEV_PERSISTENCE_ENABLED_ENV, "1")
    monkeypatch.setenv("TRID3NT_MONGO_MCP_STDIO", "1")
    assert is_dev_persistence_enabled() is True


# --------------------------------------------------------------------------- #
# Server-side wiring
# --------------------------------------------------------------------------- #


def test_maybe_bind_dev_persistence_engages_file_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main._maybe_bind_dev_persistence`` binds a FilePersistence singleton.

    Mirrors the startup wiring in ``main.run``: with no MCP env vars set and
    no prior binding, the helper engages the file-backed substrate so the
    ``server.get_persistence()`` singleton is populated. The follow-on call
    to ``server.init_persistence_from_env`` preserves it (we exercise the
    preservation branch too).
    """
    from trid3nt_server.main import _maybe_bind_dev_persistence
    from trid3nt_server.server import (
        get_persistence,
        init_persistence_from_env,
        set_persistence,
    )

    # Clear any prior binding from other tests.
    set_persistence(None)
    monkeypatch.delenv("TRID3NT_MONGO_MCP_STDIO", raising=False)
    monkeypatch.delenv("TRID3NT_MONGO_MCP_URL", raising=False)
    monkeypatch.delenv(DEV_PERSISTENCE_ENABLED_ENV, raising=False)
    monkeypatch.setenv(DEV_PERSISTENCE_DIR_ENV, str(tmp_path))

    try:
        _maybe_bind_dev_persistence()
        p = get_persistence()
        assert p is not None

        # init_persistence_from_env must NOT clobber the pre-bound singleton.
        result = asyncio.run(init_persistence_from_env())
        assert result is p
        assert get_persistence() is p

        # Smoke check: a Case round-trip through the bound singleton lands a
        # JSON file in the override dir.
        case = _fresh_case()
        asyncio.run(p.upsert_case(case))
        fetched = asyncio.run(p.get_case(case.case_id))
        assert fetched is not None
        assert (tmp_path / DEFAULT_DATABASE / f"{CASES_COLLECTION}.json").exists()
    finally:
        set_persistence(None)


def test_maybe_bind_dev_persistence_respects_disable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``TRID3NT_DEV_PERSISTENCE=0`` keeps the singleton unbound."""
    from trid3nt_server.main import _maybe_bind_dev_persistence
    from trid3nt_server.server import get_persistence, set_persistence

    set_persistence(None)
    monkeypatch.delenv("TRID3NT_MONGO_MCP_STDIO", raising=False)
    monkeypatch.setenv(DEV_PERSISTENCE_ENABLED_ENV, "0")
    monkeypatch.setenv(DEV_PERSISTENCE_DIR_ENV, str(tmp_path))

    try:
        _maybe_bind_dev_persistence()
        assert get_persistence() is None
    finally:
        set_persistence(None)


def test_make_file_persistence_default_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """``make_file_persistence()`` with override env-var lands files there."""
    home = Path(os.environ.get("HOME", "/tmp"))
    # Avoid touching the real ~/.trid3nt — use a tmpdir via env override.
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv(DEV_PERSISTENCE_DIR_ENV, td)
        p = make_file_persistence()
        case = _fresh_case()
        asyncio.run(p.upsert_case(case))
        assert (Path(td) / DEFAULT_DATABASE / f"{CASES_COLLECTION}.json").exists()


# --------------------------------------------------------------------------- #
# Layer-B rename migration (grace2_dev -> trid3nt_dev, ~/.grace2 -> ~/.trid3nt)
# --------------------------------------------------------------------------- #


def test_layer_b_migration_renames_legacy_db_dir(tmp_path: Path) -> None:
    """A pre-rename store under <root>/grace2_dev is renamed once and stays readable."""
    legacy = tmp_path / "grace2_dev"
    legacy.mkdir()
    case = _fresh_case()
    doc = json.loads(case.model_dump_json())
    doc["_id"] = case.case_id
    (legacy / f"{CASES_COLLECTION}.json").write_text(
        json.dumps({case.case_id: doc}), encoding="utf-8"
    )

    p = Persistence(FileMCPClient(base_dir=tmp_path))

    assert not (tmp_path / "grace2_dev").exists()
    new_dir = tmp_path / DEFAULT_DATABASE
    assert new_dir.is_dir()
    assert (new_dir / f"{CASES_COLLECTION}.json").exists()
    fetched = asyncio.run(p.get_case(case.case_id))
    assert fetched is not None
    assert fetched.case_id == case.case_id
    assert fetched.title == case.title


def test_layer_b_migration_noop_when_new_dir_exists(tmp_path: Path) -> None:
    """If trid3nt_dev already exists the legacy dir is left alone (no clobber)."""
    legacy = tmp_path / "grace2_dev"
    legacy.mkdir()
    (legacy / "sentinel.json").write_text("{}", encoding="utf-8")
    new_dir = tmp_path / DEFAULT_DATABASE
    new_dir.mkdir()

    FileMCPClient(base_dir=tmp_path)

    assert (legacy / "sentinel.json").exists()
    assert new_dir.is_dir()


def test_layer_b_migration_renames_legacy_home_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no dir override, a legacy ~/.grace2 is renamed to ~/.trid3nt once."""
    from trid3nt_server.persistence import _default_dev_persistence_dir

    monkeypatch.delenv(DEV_PERSISTENCE_DIR_ENV, raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    legacy_home = tmp_path / ".grace2"
    (legacy_home / "dev_persistence" / "trid3nt_dev").mkdir(parents=True)
    marker = legacy_home / "dev_persistence" / "trid3nt_dev" / "cases.json"
    marker.write_text("{}", encoding="utf-8")

    resolved = _default_dev_persistence_dir()

    assert resolved == tmp_path / ".trid3nt" / "dev_persistence"
    assert not legacy_home.exists()
    assert (resolved / "trid3nt_dev" / "cases.json").exists()
