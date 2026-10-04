"""The dispatch path's own refusal: a tool name nothing registered."""

from __future__ import annotations


class ToolNotFoundError(RuntimeError):
    """Raised when a dispatched tool name is not registered; ``retryable=False``,
    because no retry reaches a registration that does not exist. ``valid_tools``
    carries the first 20 registered names as a correction hint."""

    error_code: str = "TOOL_NOT_FOUND"
    retryable: bool = False

    def __init__(self, tool_name: str, valid_tools: list[str]) -> None:
        # Limit to first 20 names to stay within _FUNCTION_RESPONSE_CHAR_BUDGET.
        hint = valid_tools[:20]
        super().__init__(
            f"tool {tool_name!r} not in TOOL_REGISTRY; "
            f"valid tools (first 20): {hint}"
        )
        self.tool_name = tool_name
        self.valid_tools = hint
