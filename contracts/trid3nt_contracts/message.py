"""The message IR the adapters build a turn from.

A turn is a list of ``Message``; a ``Message`` carries ``Part``s, and a Part is
text, a tool call, or a tool response. Each provider adapter converts these to
its own wire shape at its own boundary, so no provider SDK type crosses the
seam. ``ToolDeclaration`` is the catalog entry the model reasons over.
"""

from __future__ import annotations

import warnings
from typing import Any, Literal

from pydantic import Field

from .common import GraceModel


class ToolCall(GraceModel):
    """The model's decision to call one tool.
    ``id`` is the provider's per-call identifier; it is echoed back on the
    matching response, and is absent where a provider mints none."""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None


class ToolResponse(GraceModel):
    """The result of one dispatched tool call, paired to it by ``id``."""

    name: str
    result: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None


class Part(GraceModel):
    """One element of a turn: text, a tool call, or a tool response.
    At most one of the three is set; a Part carrying none is dropped by the
    builders rather than sent to a provider."""

    text: str | None = None
    call: ToolCall | None = None
    response: ToolResponse | None = None


class Message(GraceModel):
    """One turn of the conversation.
    ``model`` is the assistant side; every other speaker, the dispatched tool
    results included, rides the ``user`` side, which is what both live provider
    wire formats accept."""

    role: Literal["user", "model"] = "user"
    parts: list[Part] = Field(default_factory=list)


# ``schema`` shadows a deprecated ``BaseModel`` classmethod: inert on a
# data-only model, and the field name the seam is specified in.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", UserWarning)

    class ToolDeclaration(GraceModel):
        """One tool as the model sees it.
        ``schema`` is JSON Schema for the call arguments - an object schema with
        a ``properties`` map - which is the shape both provider APIs take."""

        name: str
        description: str = ""
        schema: dict[str, Any] = Field(default_factory=dict)
