"""``!run`` chat-invocation parser -- PURE (no Qt, no network).

An anchored ``!run`` prefix invokes a tool directly instead of flowing to chat;
a mid-sentence mention does not. Argument values go through
``ast.literal_eval``, never ``eval``, and positional args are rejected."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Optional

#: The anchored prefix token.
RUN_PREFIX = "!run"

#: The one-line usage string ``!run help`` (and a bare ``!run``) render
#: locally.
USAGE = (
    "!run <tool>(arg=value, ...)  or  !run <tool> {\"arg\": value}  -- invoke a "
    "tool directly (same call the model makes). "
    "Examples: !run geocode_location(query=\"Boulder, Colorado\")  |  "
    "!run fetch_dem(bbox=[-85.4, 29.9, -85.3, 30.0], source=\"3dep\"). "
    "To find tool names + args, ask the assistant to search the catalog "
    "(e.g. \"what tools can fetch elevation?\") or call search_tools."
)

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_JSON_FORM = re.compile(r"^(" + _IDENT + r")\s+(\{.*\})$", re.DOTALL)
_BARE_NAME = re.compile(r"^" + _IDENT + r"$")


@dataclass
class RunInvocation:
    """Parsed outcome of a ``!run`` message. Exactly one of ``help``, ``error``
    or ``name`` + ``args`` carries the outcome, and only the last one is ever
    sent."""

    help: bool = False
    error: Optional[str] = None
    name: Optional[str] = None
    args: dict = field(default_factory=dict)


def is_run_prefix(text: str) -> bool:
    """True when ``text`` is anchored as a ``!run`` invocation: the exact token
    at the START, followed by whitespace or end-of-string. A mid-sentence
    mention is False."""
    stripped = text.strip()
    if stripped == RUN_PREFIX:
        return True
    return (
        stripped.startswith(RUN_PREFIX + " ")
        or stripped.startswith(RUN_PREFIX + "\t")
        or stripped.startswith(RUN_PREFIX + "\n")
    )


def _syntax_error(detail: str) -> RunInvocation:
    return RunInvocation(error=f"{detail}\n\n{USAGE}")


def parse_run_invocation(text: str) -> Optional[RunInvocation]:
    """Parse composer ``text`` as a ``!run`` invocation. ``None`` means it is
    not one and routes to chat unchanged; otherwise help, a typed local error,
    or a valid ``(name, args)`` intent."""
    stripped = text.strip()
    if not is_run_prefix(stripped):
        return None

    remainder = stripped[len(RUN_PREFIX):].strip()

    # ``!run`` / ``!run help`` -> usage.
    if remainder == "" or remainder.lower() == "help":
        return RunInvocation(help=True)

    # JSON-object form: ``<name> {json}``.
    json_match = _JSON_FORM.match(remainder)
    if json_match is not None:
        name = json_match.group(1)
        try:
            parsed = json.loads(json_match.group(2))
        except (json.JSONDecodeError, ValueError) as exc:
            return _syntax_error(f"could not parse JSON args: {exc}")
        if not isinstance(parsed, dict):
            return _syntax_error("JSON args must be an object (e.g. {\"bbox\": [...]})")
        return RunInvocation(name=name, args=parsed)

    # Bare name: ``<name>`` with no args.
    if _BARE_NAME.match(remainder):
        return RunInvocation(name=remainder, args={})

    # Pythonic kwargs form: ``<name>(k=v, ...)``. Parse as a Python expression
    # (a Call) and literal-eval each keyword value. NEVER eval names/calls.
    try:
        tree = ast.parse(remainder, mode="eval")
    except SyntaxError as exc:
        return _syntax_error(f"could not parse tool call: {exc.msg}")

    node = tree.body
    if isinstance(node, ast.Name):
        # ``!run foo`` already handled above, but a name with odd whitespace
        # can land here -- treat as a bare call.
        return RunInvocation(name=node.id, args={})
    if not isinstance(node, ast.Call):
        return _syntax_error("expected a tool call like tool(arg=value, ...)")
    if not isinstance(node.func, ast.Name):
        return _syntax_error("the tool name must be a plain identifier")
    if node.args:
        return _syntax_error(
            "positional args are not supported -- use keyword args "
            "(tools are keyword-only), e.g. tool(bbox=[...], source=\"3dep\")"
        )
    args: dict = {}
    for kw in node.keywords:
        if kw.arg is None:
            return _syntax_error("**kwargs unpacking is not supported")
        try:
            args[kw.arg] = ast.literal_eval(kw.value)
        except (ValueError, SyntaxError, TypeError):
            return _syntax_error(
                f"argument {kw.arg!r} must be a literal value "
                "(string, number, list, dict, bool, or null)"
            )
    return RunInvocation(name=node.func.id, args=args)
