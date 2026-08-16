"""Tests for AllowedToolSet's static hot-set path + auth user_id binding.

The per-user dynamic hot-set feature (Wave 4.11 M6: Mongo-backed, gated
behind ``TRID3NT_DYNAMIC_HOT_SET=1``) was cut as feature-creep - the flag
was never set in any live config, so ``as_frozenset_async`` was already
always the static path in practice. Its dedicated tests (env-flag-on/off,
``get_dynamic_hot_set`` mocking, Mongo-failure fallback, caching,
``user_id`` propagation into the Mongo query) were removed along with the
function.

Remaining coverage (still exercises real, live behavior):
1. ``test_bind_auth_result_sets_user_id_on_allowed_set`` — ``_bind_auth_result``
   in server.py propagates the authenticated user_id to the
   ``state.allowed_tool_set.user_id`` field. (That field itself is now
   vestigial - it has no readers - but the propagation is still exercised
   here as a regression guard on ``_bind_auth_result``.)
2. ``test_synchronous_as_frozenset_returns_static_before_first_async_call``
   — synchronous ``as_frozenset()`` always returns the static hot set
   (the ``_dynamic_hot_set`` cache slot is never populated any more).
"""

from __future__ import annotations

from trid3nt_server.agent.categories import AllowedToolSet, HOT_SET_TOOLS


# ---------------------------------------------------------------------------
# Test: _bind_auth_result propagates user_id to allowed_tool_set
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
    mock_result.tier = "standard"

    _bind_auth_result(state, mock_result)

    assert state.authenticated_user_id == "firebase-uid-abc123"
    assert state.allowed_tool_set.user_id == "firebase-uid-abc123", (
        "AllowedToolSet.user_id not updated after _bind_auth_result"
    )


# ---------------------------------------------------------------------------
# Test: synchronous as_frozenset always returns the static hot set
# ---------------------------------------------------------------------------


def test_synchronous_as_frozenset_returns_static_before_first_async_call():
    """as_frozenset() returns the static hot set (_dynamic_hot_set is never set)."""
    allowed = AllowedToolSet()
    # _dynamic_hot_set is None at construction and never assigned any more —
    # the static path is the only path.
    assert allowed._dynamic_hot_set is None
    result = allowed.as_frozenset()
    assert result == HOT_SET_TOOLS
