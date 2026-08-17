"""``!run`` chat-invocation parser (client parse-first).

NATE's feature: a chat message may invoke a tool DIRECTLY -- the same way the
LLM or a workflow would -- by prefixing the composer text with ``!run`` and a
tool signature. The dock parses this BEFORE the normal chat path (see
``dock._send``): a match routes straight to the server as a ``dev-tool-invoke``
envelope, a non-match flows to chat byte-identically.

This module is PURE (no Qt, no network) so it unit-tests without QGIS. It only
turns composer text into a structured intent; the dock owns rendering + the
send.

Grammar (the remainder after the anchored ``!run`` token):

    !run                         -> help
    !run help                    -> help
    !run geocode_location(query="Boulder, Colorado")
    !run fetch_dem(bbox=[-85.4, 29.9, -85.3, 30.0], source="3dep")
    !run some_tool               -> bare call, no args
    !run some_tool()             -> bare call, no args
    !run some_tool {"bbox": [-85.4, 29.9, -85.3, 30.0]}   (JSON-object form)

Two argument styles are accepted:

* PYTHONIC KWARGS -- ``tool(k=v, ...)``. Parsed via ``ast`` in eval mode and
  each value resolved with ``ast.literal_eval`` (a SAFE literal parser -- never
  ``eval``). Positional args are rejected (the registry closures are
  keyword-only post-fold), as are non-literal values (names, calls, operators).
* JSON OBJECT -- ``tool {"k": v, ...}``. The brace object is ``json.loads``- d;
  it must be a JSON object (dict).

Prefix anchoring: the message must START with ``!run`` followed by whitespace
or end-of-string. ``!running``, ``!runx``, and any message that merely MENTIONS
``!run`` mid-sentence are NOT invocations -- they return ``None`` and flow to
chat.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Optional

#: The anchored prefix token.
RUN_PREFIX = "!run"

#: The one-line usage string ``!run help`` (and a bare ``!run``) render locally.
#: Points at the existing tool-search surface rather than adding a new listing.
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
    """Parsed outcome of a ``!run`` message.

    Exactly one of ``help`` / ``error`` / (``name`` + ``args``) is the payload:

    * ``help=True``      -- render ``USAGE`` locally; nothing sent.
    * ``error`` set      -- render the honest error bubble locally; nothing sent.
    * ``name`` set       -- a valid invocation; the dock sends ``dev-tool-invoke``.
    """

    help: bool = False
    error: Optional[str] = None
    name: Optional[str] = None
    args: dict = field(default_factory=dict)


def is_run_prefix(text: str) -> bool:
    """True when ``text`` is anchored as a ``!run`` invocation.

    This is the ONE shared predicate: the dock's parse-first routing
    (``parse_run_invocation`` returns non-``None`` iff this is True) AND the
    composer's blue-``!run`` highlight both read it, so the visual signal can
    never disagree with where the message routes. Prefix-anchored: the message
    must START with the exact ``!run`` token followed by whitespace or
    end-of-string. A mid-sentence mention (``use !run to ...``) is False.
    """
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
    """Parse composer ``text`` as a ``!run`` invocation.

    Returns ``None`` when ``text`` is NOT a ``!run`` message (route to chat
    unchanged). Returns a :class:`RunInvocation` otherwise -- help, a typed
    local error, or a valid ``(name, args)`` intent.

    ``text`` is expected pre-stripped by the caller; a defensive ``.strip()``
    here keeps the function correct in isolation (tests) without changing the
    dock's behaviour.
    """
    stripped = text.strip()
    # Prefix anchoring via the ONE shared predicate (see is_run_prefix): a
    # non-match routes to chat.
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
