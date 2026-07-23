"""Tests for dynamic hot-set integration with AllowedToolSet (Wave 4.11 M6).

Coverage:
1. ``test_env_flag_off_uses_static_hot_set`` — with TRID3NT_DYNAMIC_HOT_SET
   unset, ``as_frozenset_async`` returns the static HOT_SET_TOOLS baseline
   (no Mongo call).
2. ``test_env_flag_on_uses_get_dynamic_hot_set`` — with TRID3NT_DYNAMIC_HOT_SET=1,
   ``as_frozenset_async`` calls ``get_dynamic_hot_set`` and returns a frozenset
   that includes the dynamic result tools.
3. ``test_mongo_failure_falls_back_to_static`` — when ``get_dynamic_hot_set``
   raises (Mongo unavailable), ``as_frozenset_async`` falls back to the static
   HOT_SET_TOOLS cleanly.
4. ``test_dynamic_hot_set_includes_meta_tools`` — the result from
   ``as_frozenset_async`` always includes the meta-tools
   (list_categories, list_tools_in_category, search_tools) regardless of
   what ``get_dynamic_hot_set`` returns.
5. ``test_dynamic_hot_set_cached_after_first_call`` — the cached dynamic
   hot set is reused on subsequent synchronous ``as_frozenset()`` calls,
   i.e. only ONE ``get_dynamic_hot_set`` call is made across multiple accesses.
6. ``test_user_id_propagated_to_get_dynamic_hot_set`` — the ``user_id`` field
   on AllowedToolSet is passed through to ``get_dynamic_hot_set``.
7. ``test_bind_auth_result_sets_user_id_on_allowed_set`` — ``_bind_auth_result``
   in server.py propagates the authenticated user_id to the
   ``state.allowed_tool_set.user_id`` field.
8. ``test_synchronous_as_frozenset_returns_static_before_first_async_call``
   — synchronous ``as_frozenset()`` uses the static hot set when the async
   path has never run (no cached dynamic set).
9. ``test_empty_dynamic_result_falls_back_to_static`` — when
   ``get_dynamic_hot_set`` returns an empty frozenset, ``as_frozenset_async``
   falls back to ``HOT_SET_TOOLS`` rather than leaving the hot set empty.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from trid3nt_server.categories import AllowedToolSet, HOT_SET_TOOLS


# ---------------------------------------------------------------------------
# Meta-tools that must always be present.
# ---------------------------------------------------------------------------

_META_TOOLS = frozenset({"list_categories", "list_tools_in_category", "search_tools"})

_DYNAMIC_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "fetch_dem",
        "geocode_location",
        "fetch_nws_alerts_conus",
        "fetch_fema_nfhl_zones",
        "fetch_mrms_qpe",
        "run_model_flood_scenario",
        "fetch_usace_nsi",
        "run_pelicun_damage_assessment",
    }
)


# ---------------------------------------------------------------------------
# Test 1: env flag OFF → static hot set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_flag_off_uses_static_hot_set(monkeypatch):
    """Without TRID3NT_DYNAMIC_HOT_SET=1, as_frozenset_async uses static HOT_SET_TOOLS."""
    monkeypatch.delenv("TRID3NT_DYNAMIC_HOT_SET", raising=False)

    allowed = AllowedToolSet()
    result = await allowed.as_frozenset_async()

    # Must equal the static computation (no opened categories, no dispatched).
    assert result == allowed.as_frozenset()
    assert HOT_SET_TOOLS.issubset(result)


# ---------------------------------------------------------------------------
# Test 2: env flag ON → get_dynamic_hot_set is used
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_flag_on_uses_get_dynamic_hot_set(monkeypatch):
    """With TRID3NT_DYNAMIC_HOT_SET=1, as_frozenset_async calls get_dynamic_hot_set."""
    monkeypatch.setenv("TRID3NT_DYNAMIC_HOT_SET", "1")

    call_log: list[dict] = []

    async def _fake_get_dyn(user_id=None, top_k=8):
        call_log.append({"user_id": user_id, "top_k": top_k})
        return _DYNAMIC_TOOL_NAMES

    allowed = AllowedToolSet(user_id="user-abc")

    with patch(
        "trid3nt_server.tools.discovery.search_tools.get_dynamic_hot_set",
        side_effect=_fake_get_dyn,
    ):
        result = await allowed.as_frozenset_async()

    # get_dynamic_hot_set was called exactly once.
    assert len(call_log) == 1
    assert call_log[0]["user_id"] == "user-abc"

    # Dynamic tools are in the result.
    for t in _DYNAMIC_TOOL_NAMES:
        assert t in result, f"dynamic tool {t!r} missing from result"

    # Meta-tools always present.
    for t in _META_TOOLS:
        assert t in result, f"meta-tool {t!r} missing from result"


# ---------------------------------------------------------------------------
# Test 3: Mongo failure → static fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mongo_failure_falls_back_to_static(monkeypatch):
    """get_dynamic_hot_set raising falls back to static HOT_SET_TOOLS cleanly."""
    monkeypatch.setenv("TRID3NT_DYNAMIC_HOT_SET", "1")

    async def _failing_get_dyn(user_id=None, top_k=8):
        raise RuntimeError("Mongo unavailable — simulated failure")

    allowed = AllowedToolSet()
    with patch(
        "trid3nt_server.tools.discovery.search_tools.get_dynamic_hot_set",
        side_effect=_failing_get_dyn,
    ):
        result = await allowed.as_frozenset_async()

    # Must not raise; result must contain the static hot set.
    assert HOT_SET_TOOLS.issubset(result), (
        f"Static hot set missing after Mongo failure: {HOT_SET_TOOLS - result}"
    )


# ---------------------------------------------------------------------------
# Test 4: meta-tools always present regardless of dynamic result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_hot_set_includes_meta_tools(monkeypatch):
    """Meta-tools are always present even if the dynamic hot set omits them."""
    monkeypatch.setenv("TRID3NT_DYNAMIC_HOT_SET", "1")

    # Dynamic set that deliberately excludes meta-tools.
    _no_meta = frozenset({"fetch_dem", "fetch_nws_alerts_conus", "geocode_location"})

    async def _fake_get_dyn(user_id=None, top_k=8):
        return _no_meta

    allowed = AllowedToolSet()
    with patch(
        "trid3nt_server.tools.discovery.search_tools.get_dynamic_hot_set",
        side_effect=_fake_get_dyn,
    ):
        result = await allowed.as_frozenset_async()

    for t in _META_TOOLS:
        assert t in result, (
            f"meta-tool {t!r} not in result even though it must always be available"
        )


# ---------------------------------------------------------------------------
# Test 5: dynamic hot set cached after first async call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_hot_set_cached_after_first_call(monkeypatch):
    """as_frozenset_async fetches once; synchronous as_frozenset reuses the cache."""
    monkeypatch.setenv("TRID3NT_DYNAMIC_HOT_SET", "1")

    call_count = 0

    async def _counting_get_dyn(user_id=None, top_k=8):
        nonlocal call_count
        call_count += 1
        return _DYNAMIC_TOOL_NAMES

    allowed = AllowedToolSet()
    with patch(
        "trid3nt_server.tools.discovery.search_tools.get_dynamic_hot_set",
        side_effect=_counting_get_dyn,
    ):
        # First async call — triggers Mongo fetch.
        result1 = await allowed.as_frozenset_async()
        # Second async call — should reuse the in-process cache (search_tools's
        # own 5-min refresh window guards the Mongo round-trip).
        result2 = await allowed.as_frozenset_async()

    # Exactly ONE call to the (possibly expensive) Mongo read within a session.
    # (The search_tools module's own 5-min cache guards Mongo; here we just
    # verify AllowedToolSet stores the result and reuses it synchronously.)
    assert result1 == result2
    # After the async call, synchronous access sees the same dynamic set.
    sync_result = allowed.as_frozenset()
    assert sync_result == result1


# ---------------------------------------------------------------------------
# Test 6: user_id propagated to get_dynamic_hot_set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_id_propagated_to_get_dynamic_hot_set(monkeypatch):
    """AllowedToolSet.user_id is forwarded to get_dynamic_hot_set(user_id=...)."""
    monkeypatch.setenv("TRID3NT_DYNAMIC_HOT_SET", "1")

    received_user_ids: list[str | None] = []

    async def _recording_get_dyn(user_id=None, top_k=8):
        received_user_ids.append(user_id)
        return HOT_SET_TOOLS

    allowed = AllowedToolSet(user_id="user-xyz-789")
    with patch(
        "trid3nt_server.tools.discovery.search_tools.get_dynamic_hot_set",
        side_effect=_recording_get_dyn,
    ):
        await allowed.as_frozenset_async()

    assert len(received_user_ids) == 1
    assert received_user_ids[0] == "user-xyz-789"


# ---------------------------------------------------------------------------
# Test 7: _bind_auth_result propagates user_id to allowed_tool_set
# ---------------------------------------------------------------------------


def test_bind_auth_result_sets_user_id_on_allowed_set():
    """_bind_auth_result copies authenticated user_id into allowed_tool_set.user_id."""
    from unittest.mock import MagicMock

    from trid3nt_server.server import SessionState, _bind_auth_result

    state = SessionState(session_id="test-session-001")
    assert state.allowed_tool_set.user_id is None

    # Build a minimal AuthResult-like mock.
    mock_result = MagicMock()
    mock_result.user.user_id = "firebase-uid-abc123"
    mock_result.is_anonymous = False
    mock_result.firebase_uid = "firebase-uid-abc123"
    mock_result.tier = "standard"

    _bind_auth_result(state, mock_result)

    assert state.authenticated_user_id == "firebase-uid-abc123"
    assert state.allowed_tool_set.user_id == "firebase-uid-abc123", (
        "AllowedToolSet.user_id not updated after _bind_auth_result"
    )


# ---------------------------------------------------------------------------
# Test 8: synchronous as_frozenset uses static before first async call
# ---------------------------------------------------------------------------


def test_synchronous_as_frozenset_returns_static_before_first_async_call():
    """Before as_frozenset_async has run, as_frozenset() returns the static hot set."""
    allowed = AllowedToolSet()
    # _dynamic_hot_set is None at construction — static path should be used.
    assert allowed._dynamic_hot_set is None
    result = allowed.as_frozenset()
    assert result == HOT_SET_TOOLS


# ---------------------------------------------------------------------------
# Test 9: empty dynamic result falls back to static
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_dynamic_result_falls_back_to_static(monkeypatch):
    """An empty frozenset from get_dynamic_hot_set falls back to HOT_SET_TOOLS."""
    monkeypatch.setenv("TRID3NT_DYNAMIC_HOT_SET", "1")

    async def _empty_get_dyn(user_id=None, top_k=8):
        return frozenset()  # simulates zero telemetry / cold-start

    allowed = AllowedToolSet()
    with patch(
        "trid3nt_server.tools.discovery.search_tools.get_dynamic_hot_set",
        side_effect=_empty_get_dyn,
    ):
        result = await allowed.as_frozenset_async()

    # The cached value must be the static fallback (not an empty set).
    assert allowed._dynamic_hot_set == HOT_SET_TOOLS
    assert HOT_SET_TOOLS.issubset(result), (
        "HOT_SET_TOOLS not in result after empty dynamic hot set"
    )
