"""Test that Bedrock messages are normalized to start with role=='user'.

Bedrock ConverseStream requires the first message to have role 'user'.
This test ensures that our normalizer strips leading non-user messages
and prepends a synthetic user message if needed.
"""

from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server.bedrock_adapter import (
    _ensure_messages_start_with_user,
    _build_converse_kwargs,
)


class TestEnsureMessagesStartWithUser:
    """Tests for the message normalization function."""

    def test_empty_messages_list(self):
        """Empty messages list gets a synthetic user message."""
        result = _ensure_messages_start_with_user([])
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["text"] == "(context)"

    def test_user_first_unchanged(self):
        """Messages already starting with 'user' are unchanged."""
        messages = [
            {"role": "user", "content": [{"text": "Hello"}]},
            {"role": "assistant", "content": [{"text": "Hi"}]},
        ]
        result = _ensure_messages_start_with_user(messages)
        assert result == messages

    def test_assistant_first_stripped(self):
        """Leading assistant messages are stripped, user message kept."""
        messages = [
            {"role": "assistant", "content": [{"text": "Context"}]},
            {"role": "user", "content": [{"text": "Hello"}]},
        ]
        result = _ensure_messages_start_with_user(messages)
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["text"] == "Hello"

    def test_multiple_leading_non_user_stripped(self):
        """All leading non-user messages are stripped."""
        messages = [
            {"role": "assistant", "content": [{"text": "First"}]},
            {"role": "tool", "content": [{"text": "Second"}]},
            {"role": "user", "content": [{"text": "Question"}]},
        ]
        result = _ensure_messages_start_with_user(messages)
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["text"] == "Question"

    def test_all_non_user_messages_prepend_synthetic(self):
        """All non-user messages get a synthetic user message prepended."""
        messages = [
            {"role": "assistant", "content": [{"text": "First"}]},
            {"role": "tool", "content": [{"text": "Second"}]},
        ]
        result = _ensure_messages_start_with_user(messages)
        assert len(result) == 3
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["text"] == "(context)"
        # Original messages are preserved after the synthetic one.
        assert result[1]["role"] == "assistant"
        assert result[2]["role"] == "tool"

    def test_tool_first_stripped(self):
        """Leading tool messages are stripped."""
        messages = [
            {"role": "tool", "content": [{"text": "Result"}]},
            {"role": "user", "content": [{"text": "Query"}]},
        ]
        result = _ensure_messages_start_with_user(messages)
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["text"] == "Query"


class TestBuildConverseKwargsNormalization:
    """Tests that _build_converse_kwargs properly normalizes messages."""

    def test_messages_normalized_in_kwargs(self):
        """Messages in returned kwargs are normalized to start with 'user'."""
        # Mock contents that would produce non-user-first messages
        mock_contents = []  # Empty contents -> no messages initially

        kwargs = _build_converse_kwargs(
            contents=mock_contents,
            tool_declarations=None,
            system_prompt="Test system",
            model="us.anthropic.claude-sonnet-4-6",
        )

        messages = kwargs["messages"]
        assert len(messages) > 0
        assert messages[0]["role"] == "user"

    def test_log_preview_when_enabled(self, caplog):
        """LLM input preview is logged when TRID3NT_LOG_LLM_INPUT is set."""
        import logging
        caplog.set_level(logging.INFO)

        with patch.dict(
            "os.environ",
            {"TRID3NT_LOG_LLM_INPUT": "1"}
        ):
            kwargs = _build_converse_kwargs(
                contents=[],
                tool_declarations=None,
                system_prompt="Test system",
                model="us.anthropic.claude-sonnet-4-6",
            )

        # Check that the preview log was emitted.
        assert any(
            "LLM input preview" in record.message
            for record in caplog.records
        )

    def test_log_preview_not_emitted_by_default(self, caplog):
        """LLM input preview is not logged when env var is not set."""
        import logging
        caplog.set_level(logging.INFO)

        with patch.dict("os.environ", {}, clear=False):
            # Ensure TRID3NT_LOG_LLM_INPUT is not set.
            if "TRID3NT_LOG_LLM_INPUT" in __import__("os").environ:
                del __import__("os").environ["TRID3NT_LOG_LLM_INPUT"]

            kwargs = _build_converse_kwargs(
                contents=[],
                tool_declarations=None,
                system_prompt="Test system",
                model="us.anthropic.claude-sonnet-4-6",
            )

        # Check that the preview log was NOT emitted.
        assert not any(
            "LLM input preview" in record.message
            for record in caplog.records
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
