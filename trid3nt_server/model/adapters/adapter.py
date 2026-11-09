"""The turn builders and the provider-dispatch seam.

``trid3nt_contracts.message`` is the shared IR each provider adapter converts
at its own boundary; an unsupported ``MODEL_PROVIDER`` raises, never an empty
turn.
"""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import types as _builtin_types
from typing import Any, Literal, get_args, get_origin, get_type_hints, Union

from trid3nt_contracts.message import Message, Part, ToolCall, ToolDeclaration, ToolResponse

from trid3nt_server.settings import setting

logger = logging.getLogger("trid3nt_server.model.adapters.adapter")

#: The code a modeled envelope with no layers carries when its failure marker
#: names none of its own.
_NO_LAYERS_CODE = "MODEL_RUN_PRODUCED_NO_LAYERS"


@dataclass(frozen=True)
class TextDeltaEvent:
    """A streamed text fragment from the model."""
    delta: str


@dataclass(frozen=True)
class ThinkingDeltaEvent:
    """A streamed reasoning-channel fragment (OpenAI-compatible path only).
    Never emitted by the Anthropic or scripted paths, so the turn loop must
    tolerate its absence and may drop it when the user toggle is off."""
    delta: str


@dataclass(frozen=True)
class FunctionCallEvent:
    """The model decided to call a tool.
    ``name`` matches a registered tool name; ``call_id`` is the provider's
    per-call identifier, echoed back with the function response."""
    name: str
    call_id: str | None
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UsageMetadataEvent:
    """Per-turn usage metadata harvested from the provider's usage report. Emitted at most once
    per adapter call; every field may be ``None`` when the provider does not expose it."""

    cached_content_token_count: int | None = None
    total_token_count: int | None = None
    prompt_token_count: int | None = None
    candidates_token_count: int | None = None
    cache_hit: bool = False
    # Reasoning-channel tokens (OpenAI-compatible ``usage.completion_tokens_details.
    # reasoning_tokens``); ``None`` when the provider does not report them, NEVER fabricated.
    reasoning_token_count: int | None = None


@dataclass(frozen=True)
class CompactionStartEvent:
    """Client-side history management is about to run for this turn.
    Carries no fields -- the compacted-side count is not known yet, so it rides
    the matching complete event; a turn under budget emits neither."""


@dataclass(frozen=True)
class CompactionCompleteEvent:
    """The compaction a preceding ``CompactionStartEvent`` announced has finished.
    Always paired 1:1 with that prior event within the same adapter call."""

    before_tokens: int
    after_tokens: int


StreamEvent = (
    TextDeltaEvent
    | ThinkingDeltaEvent
    | FunctionCallEvent
    | UsageMetadataEvent
    | CompactionStartEvent
    | CompactionCompleteEvent
)


# Upstream-provider discipline: an upstream failure is NEVER internalized.
#
# The provider adapters classify TRANSIENT provider errors (HTTP 429, 5xx,
# timeouts, connection drops, provider-reported overload) as
# ``error_class="upstream_provider"``, log the provider's VERBATIM error, and
# retry with exponential backoff. On exhaustion they raise
# ``UpstreamProviderError`` so the turn ends with an HONEST
# provider-unavailable narration -- never a silent empty turn, never recorded
# as an internal error. Non-transient provider errors (auth, bad request) fail
# fast unchanged as ``error_class="provider_request"``.
#
# The retry policy the adapters share is PROVIDER_RETRIES retries after the first attempt, the wait
# before retry N (0-based) ``PROVIDER_BACKOFF_S * 2**N``; a provider Retry-After, when present,
# OVERRIDES the schedule for that attempt.


class UpstreamProviderError(RuntimeError):
    """A TRANSIENT upstream provider failure that survived retry exhaustion.
    ``detail`` carries the provider's VERBATIM last error string -- the honesty
    floor -- and ``attempts`` counts the original request plus its retries."""

    error_class = "upstream_provider"

    def __init__(self, provider: str, detail: str, attempts: int = 1) -> None:
        self.provider = provider
        self.detail = detail
        self.attempts = attempts
        super().__init__(
            f"upstream provider {provider} unavailable after {attempts} "
            f"attempt(s): {detail}"
        )


class UnsupportedModelProviderError(RuntimeError):
    """``MODEL_PROVIDER`` names a provider the dispatch does not support.
    The dispatch is EXPLICIT: any other value, the empty default included,
    raises here rather than falling through silently."""

    error_class = "internal"


#: Retries of one model call after its first attempt, on a transient provider fault.
PROVIDER_RETRIES = 3
#: The exponential-backoff base between those retries, in seconds.
PROVIDER_BACKOFF_S = 5.0


def provider_retries() -> int:
    """Max provider retries after the first attempt."""
    return PROVIDER_RETRIES


def provider_backoff_wait(attempt: int, *, cap: float = 60.0) -> float:
    """Seconds to wait before retry ``attempt`` (0-based): ``base * 2**attempt``
    capped at ``cap``. Pure so tests can pin the schedule."""
    wait = PROVIDER_BACKOFF_S * (2.0 ** max(int(attempt), 0))
    return max(0.0, min(wait, float(cap)))


def classify_provider_error_class(exc: BaseException) -> str:
    """Classify a turn-ending exception for the per-turn telemetry record.
    One of ``upstream_provider`` (transient, never recorded as internal),
    ``provider_request`` (a rejection of our request), or ``internal``."""
    if isinstance(exc, UpstreamProviderError) or getattr(exc, "error_class", None) == "upstream_provider":
        return "upstream_provider"

    # OpenAI-compatible path (optional dep).
    try:
        import openai  # noqa: WPS433

        if isinstance(
            exc,
            (
                openai.BadRequestError,  # 400
                openai.AuthenticationError,  # 401
                openai.PermissionDeniedError,  # 403
                openai.NotFoundError,  # 404
                openai.UnprocessableEntityError,  # 422
            ),
        ):
            return "provider_request"
        if isinstance(exc, (openai.RateLimitError, openai.APIConnectionError)):
            return "upstream_provider"  # 429 / timeout / connection drop
        if isinstance(exc, openai.APIStatusError):
            try:
                if int(getattr(exc, "status_code", 0) or 0) >= 500:
                    return "upstream_provider"
            except (TypeError, ValueError):
                pass
            return "provider_request"
        if isinstance(exc, openai.APIError):
            # Ambiguous APIError: transient-shaped messages are upstream.
            from .openai_adapter import _is_transient_upstream

            return (
                "upstream_provider" if _is_transient_upstream(exc) else "provider_request"
            )
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 -- classification must never raise
        pass

    # Anthropic Messages API path (optional dep).
    try:
        import anthropic  # noqa: WPS433

        if isinstance(exc, anthropic.APIError):
            from .anthropic_adapter import _is_transient_anthropic_error

            return (
                "upstream_provider"
                if _is_transient_anthropic_error(exc)
                else "provider_request"
            )
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 -- classification must never raise
        pass

    return "internal"


_PROMPT = """\
You are TRID3NT, a geospatial intelligence assistant. You fetch, analyze and map real geospatial data, and run real physics solvers over real domains to answer surface-water and groundwater questions.

Tools are offered per request. When the one you need is not offered, call search_tools; a capability no tool offers does not exist here.

Honesty:
- Never fabricate numbers. Every depth, area and count you state comes from a tool result.
- Never supply a physical model input (rate, magnitude, material property, forcing) yourself. On an INPUT-required error, relay the missing parameters with units and typical ranges and ask the user.
- State which values are the template's starting values and which are the user's, a source's or derived. Built-in runs are screening-grade unless calibrated; never present one as a calibrated study.
- A real duplicate layer is never a "display artifact": say two identical layers are on the map and offer to remove one.

Simulations:
- A simulation template gathers its own inputs. For a simulation question, call the template first with only what the user said; its review card or refusal shows what it found and what it still needs. Fetch data yourself only when the user asks for data.

Data and place:
- For data, call find_sources, then fetch("S1") with a row's handle. When the user names the source, find_sources(query=<its name>) directly; do not search_tools first. Never end a turn at a precursor (geocode, boundary lookup): continue to the data the user asked for.
- Every request stands alone for WHERE: geocode the place in the latest message and never carry an earlier bbox or layer over to a different place. Reuse earlier results only when the user refers to them ("that area", "the same map").
- "In [state, county, city, ZCTA, watershed]" means the polygon, not its bbox: geocode_location, fetch the administrative boundaries for that bbox, fetch the data, then clip with run_qgis_algorithm (gdal:cliprasterbymasklayer for a raster, native:clip for a vector) by canvas layer name. Use the bbox only when the user says "bounding box" or "rectangle".
- run_qgis_algorithm and run_pyqgis run over layers already on the map, by canvas name; fetch first, and never hand either a bbox or a URI.
- Layers are short handles (L1, L2, ...). Pass a handle exactly as a prior tool result gave it; never retype a URI.
- The canvas fits itself. To fit or zoom to a layer already on the map, read its bbox= off the [Case state] note and state the extent; call no tool, do not fetch again, and do not use run_pyqgis for extents.

Scope:
- Run a solver only for the user's current request; never resume an earlier one. Never re-run a finished solve, or re-fetch a layer the [Case state] note lists, unless the user asks or changes the area or parameters.

Narration:
- Be concise; do not recap or re-explain on retries. Before each tool round write one short sentence on what you are doing, and say when a solve will take minutes.
- After the last tool returns, narrate the outcome in 1-3 sentences from the result's values; never end silently. On a failed tool, cite its error_code and never claim success. On an argument or validation error, correct the argument and call again.
- On KEY_REQUIRED or KEY_REJECTED, say which source needs a key and that the plugin card takes it; never ask for a key in chat.
- No emojis.
"""


def system_prompt() -> str:
    """The prompt the daemon sends: the static core, no tool list. A tool reaches the model through
    the per-request declarations and ``search_tools``."""
    return _PROMPT


def _is_union_type(annotation: Any) -> bool:
    """True for any union form: ``X | Y`` is a ``types.UnionType`` while ``typing.Union`` is a
    ``_GenericAlias`` with origin ``Union``; both must be detected."""
    if isinstance(annotation, _builtin_types.UnionType):
        return True
    return get_origin(annotation) is Union


def _union_args(annotation: Any) -> tuple[Any, ...]:
    if isinstance(annotation, _builtin_types.UnionType):
        return annotation.__args__
    return get_args(annotation)


#: JSON Schema type for each primitive an annotation resolves to.
_JSON_TYPES: dict[Any, str] = {
    str: "string",
    bool: "boolean",
    int: "integer",
    float: "number",
    bytes: "string",
}


def _is_custom_class(annotation: Any) -> bool:
    """True for a class (pydantic model, dataclass, client object) that reaches a tool only as its
    serialized form, so the schema offers text."""
    return (
        isinstance(annotation, type)
        and get_origin(annotation) is None
        and annotation not in (str, int, float, bool, bytes, dict, list, set, tuple, type(None))
    )


def _json_schema_for_annotation(annotation: Any) -> dict[str, Any]:
    """Convert one annotation to a JSON Schema node. Every node carries an explicit ``type`` and
    neither ``anyOf`` nor ``$ref``: a provider rejects the whole tool catalog over either."""
    if annotation is inspect.Parameter.empty or annotation is Any or _is_custom_class(annotation):
        return {"type": "string"}
    if _is_union_type(annotation):
        members = [a for a in _union_args(annotation) if a is not type(None)]
        if not members:
            return {"type": "string"}
        # A typed list member covers a single value too, and text carries
        # anything else across the boundary, so a multi-type union collapses to
        # ONE member rather than losing its ``type``.
        if len(members) > 1:
            for member in members:
                if get_origin(member) is list:
                    return _json_schema_for_annotation(member)
            if any(member is str for member in members):
                return {"type": "string"}
        return _json_schema_for_annotation(members[0])
    origin = get_origin(annotation)
    if origin is Literal:
        values = list(get_args(annotation))
        base = _json_schema_for_annotation(type(values[0])) if values else {"type": "string"}
        return {**base, "enum": values}
    if origin in (list, set, frozenset, tuple) or annotation in (list, set, tuple):
        args = [a for a in get_args(annotation) if a is not Ellipsis]
        return {
            "type": "array",
            "items": _json_schema_for_annotation(args[0] if args else str),
        }
    if origin is dict or annotation is dict:
        return {"type": "object", "properties": {}}
    try:
        return {"type": _JSON_TYPES.get(annotation, "string")}
    except TypeError:  # an unhashable annotation object
        return {"type": "string"}


def _parameter_schema(annotation: Any, default: Any) -> tuple[dict[str, Any], bool]:
    """The JSON Schema node for one parameter, and whether the model must fill it. Required means
    the annotation admits no ``None`` and the signature offers no value; a ``None`` default is a
    sentinel, not a value the model may assume."""
    unfilled = default is inspect.Parameter.empty or default is None
    if annotation is inspect.Parameter.empty or annotation is Any or _is_custom_class(annotation):
        return {"type": "string"}, False
    if _is_union_type(annotation):
        members = [a for a in _union_args(annotation) if a is not type(None)]
        # A union spanning a coordinate tuple and another spelling is offered as
        # that other spelling: a place the user named, never coordinates the
        # model invents.
        if len(members) > 1 and any(get_origin(a) is tuple for a in members):
            return {"type": "string"}, unfilled
        nullable = len(members) != len(_union_args(annotation))
        return _json_schema_for_annotation(annotation), unfilled and not nullable
    return _json_schema_for_annotation(annotation), unfilled


_TRUNC_MARKER = "..."


def _truncate_word_boundary(text: str, cap: int) -> str:
    """Truncate ``text`` to at most ``cap`` chars, cutting at a word boundary and appending a
    trailing ``...`` marker. Text that already fits is returned unchanged (identity, no marker)."""
    if cap <= 0 or len(text) <= cap:
        return text
    room = max(cap - len(_TRUNC_MARKER), 1)
    cut = text[:room]
    space = cut.rfind(" ")
    # Back up to the last space so we never cut mid-word; but if the text has
    # no usable space in the back half (e.g. one giant token), hard-cut
    # rather than throwing away most of the budget.
    if space > room // 2:
        cut = cut[:space]
    return cut.rstrip() + _TRUNC_MARKER


def input_desc_cap() -> int:
    """Per-input schema description cap in chars; 0 sends each whole."""
    return int(setting("INPUT_DESC_CHARS"))


def _tool_schema(fn: Any) -> dict[str, Any]:
    """Build the JSON Schema of a tool callable's public parameters. Underscore-prefixed injection
    kwargs never reach the model; an unresolvable forward reference types as text."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}}
    # Tool modules use ``from __future__ import annotations``, so the signature
    # carries annotation STRINGS; the resolved hints are the real types.
    try:
        resolved: dict[str, Any] = get_type_hints(fn)
    except Exception:  # noqa: BLE001 -- name resolution can fail in unusual envs
        resolved = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name.startswith("_") or name in ("self", "cls"):
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        annotation = resolved.get(name, param.annotation)
        if isinstance(annotation, str):
            annotation = inspect.Parameter.empty
        properties[name], is_required = _parameter_schema(annotation, param.default)
        if is_required:
            required.append(name)
    cap = input_desc_cap()
    for name, text in (getattr(fn, "param_descriptions", None) or {}).items():
        if name in properties:
            properties[name]["description"] = _truncate_word_boundary(text, cap)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def build_tool_declarations(tool_registry: dict[str, Any]) -> list[ToolDeclaration]:
    """Build one ``ToolDeclaration`` per registered tool.
    A tool's registered docstring is the SOLE tool-selection signal the model
    reasons over, and it crosses in FULL; each adapter caps it to its own wire."""
    return [
        ToolDeclaration(
            name=name,
            description=inspect.getdoc(entry.fn) or f"Tool: {name}",
            schema=_tool_schema(entry.fn),
        )
        for name, entry in sorted(tool_registry.items())
    ]



# NEVER-REHYDRATE guard. The persisted agent chat row carries a ``thinking`` field (the
# reasoning-channel text for the same bubble as the answer -- see
# ``trid3nt_contracts.case.CaseChatMessage.thinking``). That text is DISPLAY REPLAY only and must
# NEVER re-enter LLM-bound contents: build_contents_from_history and _decode_parts_blob strip it
# BY RULE, so even a writer that leaks it into a parts_blob entry cannot re-inject it, and
# rehydrate_history_from_case reads only role/content/tool_card.

#: Persisted-row field names that must NEVER reach LLM-bound contents.
NEVER_REHYDRATE_FIELDS: frozenset[str] = frozenset({"thinking"})


def _strip_never_rehydrate(entry: dict) -> dict:
    """Return ``entry`` without any ``NEVER_REHYDRATE_FIELDS`` key; identity (no copy) when none is
    present, and never mutates the caller."""
    if not any(k in entry for k in NEVER_REHYDRATE_FIELDS):
        return entry
    return {k: v for k, v in entry.items() if k not in NEVER_REHYDRATE_FIELDS}


def _decode_parts_blob(blob: Any) -> list[Part] | None:
    """Decode a persisted ``parts_blob`` into a list of ``Part``; ``None`` when missing, empty or
    malformed, because one bad history row must never break the conversation."""
    import json as _json

    if blob is None:
        return None
    raw: Any
    if isinstance(blob, (bytes, bytearray)):
        try:
            raw = _json.loads(blob.decode("utf-8"))
        except Exception:  # noqa: BLE001 -- malformed → text fallback
            return None
    elif isinstance(blob, str):
        try:
            raw = _json.loads(blob)
        except Exception:  # noqa: BLE001
            return None
    elif isinstance(blob, (list, dict)):
        raw = blob
    else:
        return None
    if isinstance(raw, dict):
        # Single-part shorthand -- wrap.
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return None

    # Wire shape, one entry per part: {"text": "..."} text-only part {"function_call": {"name",
    # "id", "args"}} model turn {"function_response": {"name", "id", "response"}} The blob carries
    # enough fidelity to rebuild the exact Parts, so a replayed turn reaches the provider as the
    # turn it originally sent.
    parts: list[Part] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        # NEVER-REHYDRATE guard: strip ``thinking`` (and any future guarded field) from the blob
        # entry BY RULE before any key is read -- the full-fidelity parts_blob path must never
        # re-inject reasoning text into LLM-bound contents.
        entry = _strip_never_rehydrate(entry)
        kwargs: dict[str, Any] = {}
        if "text" in entry and entry["text"]:
            kwargs["text"] = entry["text"]
        if "function_call" in entry and isinstance(entry["function_call"], dict):
            fc = entry["function_call"]
            kwargs["call"] = ToolCall(
                name=fc.get("name") or "",
                args=fc.get("args") or {},
                id=fc.get("id"),
            )
        if "function_response" in entry and isinstance(entry["function_response"], dict):
            fr = entry["function_response"]
            kwargs["response"] = ToolResponse(
                name=fr.get("name") or "",
                result=fr.get("response") or {},
                id=fr.get("id"),
            )
        if not kwargs:
            continue
        try:
            parts.append(Part(**kwargs))
        except Exception:  # noqa: BLE001 -- drop the bad part, keep going
            continue
    return parts or None


def build_contents_from_history(
    user_text: str,
    chat_history: list[dict] | None = None,
) -> list[Message]:
    """Convert ``chat_history`` plus a new ``user_text`` into ``Message``s.
    A decodable ``parts_blob`` wins over the text shape; an empty-text row is
    dropped, and ``user_text`` is always the terminal ``user`` turn."""
    contents: list[Message] = []
    if chat_history:
        for entry in chat_history:
            # NEVER-REHYDRATE guard: strip the ``thinking`` field BY RULE
            # before ANY key is read. Persisted reasoning text is display
            # replay material only and must never reach the model.
            entry = _strip_never_rehydrate(entry)
            role = entry.get("role", "user")
            ir_role = "model" if role in ("agent", "assistant", "model") else "user"
            # parts_blob carries the call and response Parts, so the turn replays whole.
            blob = entry.get("parts_blob")
            decoded = _decode_parts_blob(blob) if blob is not None else None
            if decoded:
                contents.append(Message(role=ir_role, parts=decoded))
                continue
            text = entry.get("text", "")
            if not text:
                continue
            contents.append(Message(role=ir_role, parts=[Part(text=text)]))
    contents.append(Message(role="user", parts=[Part(text=user_text)]))
    return contents


# Default cap on the number of persisted chat rows rehydrated into the live model context on a Case
# reopen. A long-running Case can accumulate hundreds of user/agent/tool rows; replaying all of them
# every reopen turn would blow the context window (and the per-turn cost). The MOST RECENT rows
# are kept; the layers-present note anchors older work, so dropping the head loses nothing.
REHYDRATE_HISTORY_CAP = 40


def _summarize_tool_row_for_history(content: str, tool_card: Any) -> str:
    """Collapse a persisted ``role="tool"`` row into one model-side text line. The call/response
    Parts are not persisted, so ``[tool <name> completed|failed]`` is all that stands against a
    recompute."""
    name: str | None = None
    state: str | None = None
    # Prefer the typed record (duck-typed: ToolCardRecord or a dict).
    if tool_card is not None:
        name = getattr(tool_card, "tool_name", None)
        state = getattr(tool_card, "state", None)
        if name is None and isinstance(tool_card, dict):
            name = tool_card.get("tool_name")
            state = tool_card.get("state")
    if name is None and content:
        try:
            import json as _json

            parsed = _json.loads(content)
            if isinstance(parsed, dict):
                name = parsed.get("tool_name")
                state = parsed.get("state")
        except Exception:  # noqa: BLE001 -- content may not be JSON
            pass
    if not name:
        return ""
    outcome = "failed" if state == "failed" else "completed"
    return f"[tool {name} {outcome}]"


def _format_layer_bbox(bbox: Any) -> str | None:
    """Compact ``[lon_min, lat_min, lon_max, lat_max]`` for a layer line; ``None`` for anything but
    a valid 4-tuple. Rounding keeps the Case-state note short."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        b = [round(float(x), 4) for x in bbox]
    except (TypeError, ValueError):
        return None
    return f"[{b[0]}, {b[1]}, {b[2]}, {b[3]}]"


def _format_aoi_bbox_line(case_bbox: Any) -> str | None:
    """Format the Case AOI bbox as one durable instruction line, or ``None``. The AOI ANCHOR that
    must survive history capping: a long Case drops the head turn naming the place, and a
    follow-up fetch would then re-geocode."""
    if not isinstance(case_bbox, (list, tuple)) or len(case_bbox) != 4:
        return None
    try:
        b = [float(x) for x in case_bbox]
    except (TypeError, ValueError):
        return None
    return (
        f"Case AOI bbox [lon_min, lat_min, lon_max, lat_max] = "
        f"[{b[0]}, {b[1]}, {b[2]}, {b[3]}]. Use this extent for follow-up fetches "
        "and clips in this Case; do not re-geocode the area."
    )


def build_layers_present_note(
    loaded_layers: list[dict] | None,
    case_bbox: Any = None,
) -> str | None:
    """Build the compact "Case state" model turn: layers plus the AOI bbox.
    ``None`` only when there is neither a layer nor a usable bbox."""
    # Each line carries enough IDENTITY for the model to recognize an existing RESULT and not re-run
    # the work that made it: - role: RESULT (a primary simulation / analysis output) vs INPUT (a
    # fetched / context layer used as an input), and for a fetched layer the DATASET it carries,
    # read off the address its bytes are cached under. Imported here: the tool package pulls the
    # server in at import time.
    from trid3nt_server.tools.cache import parse_cache_path

    lines: list[str] = []
    for layer in loaded_layers or []:
        if not isinstance(layer, dict):
            continue
        layer_id = layer.get("layer_id") or "?"
        name = layer.get("name") or layer_id
        layer_type = layer.get("layer_type") or "?"
        # The layer_id IS the reusable handle, surfaced as ``handle=`` beside the ``uri``.
        uri = layer.get("uri")
        role_raw = layer.get("role")
        # A ``role="primary"`` layer is a RESULT, which is what stops the re-run. A recognized
        # FETCHED layer is an INPUT tagged with its KIND so a follow-up reuses it, not re-fetches.
        if role_raw == "primary":
            role_label = "RESULT"
        else:
            fetched = parse_cache_path(uri if isinstance(uri, str) else None)
            role_label = f"INPUT[{fetched[0]}]" if fetched else "INPUT"
        parts = [f"id={layer_id}", role_label, layer_type, f"handle={layer_id}"]
        bbox = layer.get("bbox")
        bbox_str = _format_layer_bbox(bbox)
        if bbox_str:
            parts.append(f"bbox={bbox_str}")
        if isinstance(uri, str) and uri:
            parts.append(f"uri={uri}")
        lines.append(f"- {name} (" + ", ".join(parts) + ")")
    bbox_line = _format_aoi_bbox_line(case_bbox)
    if not lines and not bbox_line:
        return None
    segments: list[str] = []
    if lines:
        segments.append(
            "These layers are already on the map for this Case. RESULT is a finished "
            "simulation or analysis output; INPUT[<dataset>] is a layer fetched from that "
            "dataset. Reuse them by handle: do not re-run, re-fetch or recompute one unless "
            "the user asks or changes the area or parameters:\n"
            + "\n".join(lines)
        )
    if bbox_line:
        segments.append(bbox_line)
    return "[Case state] " + "\n".join(segments)


def rehydrate_history_from_case(
    chat_messages: list[Any] | None,
    loaded_layers: list[dict] | None = None,
    *,
    cap: int = REHYDRATE_HISTORY_CAP,
    case_bbox: Any = None,
) -> tuple[list[dict], int]:
    """Convert ONE Case's persisted chat into the ``chat_history`` dict shape.
    Returns ``(history, dropped)``: only the ``cap`` most recent rows are
    replayed, and the layers-present note is appended as the last model turn."""
    rows = list(chat_messages or [])
    dropped = 0
    if cap >= 0 and len(rows) > cap:
        dropped = len(rows) - cap
        rows = rows[-cap:]

    history: list[dict] = []
    for msg in rows:
        # NEVER-REHYDRATE rule: read ONLY role / content / tool_card off the
        # persisted row. The ``thinking`` field is reasoning-channel text for
        # display replay and is deliberately never read here.
        role = getattr(msg, "role", None)
        content = getattr(msg, "content", None)
        tool_card = getattr(msg, "tool_card", None)
        if role is None and isinstance(msg, dict):
            role = msg.get("role")
            content = msg.get("content")
            tool_card = msg.get("tool_card")
        content = content or ""
        if role == "tool":
            line = _summarize_tool_row_for_history(content, tool_card)
            if line:
                # Tool transcript reads as model-side narration of what ran.
                history.append({"role": "model", "text": line})
            continue
        if role == "user":
            if content.strip():
                history.append({"role": "user", "text": content})
            continue
        if role in ("agent", "assistant", "model", "system"):
            if content.strip():
                # ``agent`` collapses to ``model`` in the contents builder; ``system`` has no native
                # role there, so it folds to model-side context text -- safer for routing than
                # re-injecting it as a fresh ``user`` instruction.
                history.append({"role": "agent", "text": content})
            continue
        # Unknown role: skip rather than guess.

    note = build_layers_present_note(loaded_layers, case_bbox=case_bbox)
    if note:
        history.append({"role": "model", "text": note})

    return history, dropped


def encode_parts_blob(parts: list[Part]) -> bytes:
    """Encode a list of ``Part`` to the ``parts_blob`` wire shape. A JSON byte string, so it
    round-trips through JSON persistence."""
    import json as _json

    out: list[dict[str, Any]] = []
    for part in parts:
        entry: dict[str, Any] = {}
        if part.text:
            entry["text"] = part.text
        if part.call is not None and part.call.name:
            entry["function_call"] = {
                "name": part.call.name,
                "id": part.call.id,
                "args": dict(part.call.args or {}),
            }
        if part.response is not None and part.response.name:
            entry["function_response"] = {
                "name": part.response.name,
                "id": part.response.id,
                "response": dict(part.response.result or {}),
            }
        if entry:
            out.append(entry)
    return _json.dumps(out).encode("utf-8")


#: Result fields that are bytes the model cannot read, never sent.
_OPAQUE_FIELDS = frozenset({"raw_bytes", "raw_body", "binary", "geometry_wkb", "pixels"})
_CUT = "...[cut to the result budget]"


def _coerce_to_summary_value(value: Any, depth: int = 0) -> Any:
    """Coerce to summary form: non-JSON types become strings, long lists and strings truncate, and
    a dict past depth 2 collapses - signal, never fidelity."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        # Long strings (HTML bodies, base64 payloads) get clipped.
        if len(value) > 500:
            return value[:500] + "…[truncated]"
        return value
    if isinstance(value, (list, tuple)):
        if depth >= 2:
            return f"[list len={len(value)}]"
        # Keep up to 5 items; summarize the rest by count.
        items = [_coerce_to_summary_value(v, depth + 1) for v in list(value)[:5]]
        if len(value) > 5:
            items.append(f"…[+{len(value) - 5} more items]")
        return items
    if isinstance(value, dict):
        if depth >= 2:
            return f"{{dict keys={list(value.keys())[:8]}}}"
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                k = str(k)
            if k in _OPAQUE_FIELDS:
                out[k] = f"[{k} suppressed]"
                continue
            out[k] = _coerce_to_summary_value(v, depth + 1)
        return out
    # Pydantic models / dataclasses / arbitrary objects -- repr-coerce, clip.
    s = repr(value)
    if len(s) > 200:
        s = s[:200] + "…"
    return s


def _classify_error(error: BaseException) -> tuple[str, bool]:
    """Derive ``(error_code, retryable)`` for a tool-dispatch exception. A typed tool exception's
    own ``error_code`` / ``retryable`` win; NEVER raises, so the loop always gets a stable pair."""
    # 1. Honour typed-tool exception class attributes when present.
    code_attr = getattr(error, "error_code", None)
    retry_attr = getattr(error, "retryable", None)
    if isinstance(code_attr, str) and code_attr:
        code = code_attr
    else:
        code = type(error).__name__.upper()
    if isinstance(retry_attr, bool):
        return code, retry_attr

    # 2. Heuristic fallback for an untyped exception: a timeout or network-ish OSError is
    # retryable; an argument-shape error (ValueError/TypeError/KeyError/AttributeError) is not;
    # anything else is retryable, capped either way by the MAX_TURN_STEPS setting.
    import asyncio as _asyncio

    if isinstance(error, (_asyncio.TimeoutError, TimeoutError)):
        return code, True
    if isinstance(error, (ConnectionError, OSError)):
        return code, True
    if isinstance(error, (ValueError, TypeError, KeyError, AttributeError)):
        return code, False
    return code, True


def _summarize_chart_emission(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Compact summary for a chart-emission tool result. The full ``vega_lite_spec`` is DROPPED (it
    reached the client on its own envelope), so narration sources its numbers from the caption."""
    spec = result.get("vega_lite_spec")
    spec = spec if isinstance(spec, dict) else {}
    mark = spec.get("mark")
    if isinstance(mark, dict):
        chart_type = mark.get("type")
    elif isinstance(mark, str):
        chart_type = mark
    else:
        chart_type = None
    data = spec.get("data")
    n_rows = (
        len(data["values"])
        if isinstance(data, dict) and isinstance(data.get("values"), list)
        else None
    )
    return {
        "tool": tool_name,
        "status": "ok",
        "result": {
            "chart_emitted": True,
            "chart_id": result.get("chart_id"),
            "title": result.get("title"),
            "caption": result.get("caption"),
            "chart_type": chart_type,
            "n_data_rows": n_rows,
            "source_layer_uri": result.get("source_layer_uri"),
            "note": (
                "A chart has been rendered for the user. Narrate what it shows "
                "using the numbers in 'caption'; do NOT restate the raw data rows."
            ),
        },
    }


def _failed_modeled_envelope_error_code(result: dict[str, Any]) -> str | None:
    """The threaded failure code of a failure-TAGGED "modeled" envelope, or ``None`` when the
    envelope carries no marker at all."""
    # A failed envelope threads its error code into TWO seams so it survives
    # ``_coerce_to_summary_value``'s depth>=2 dict collapse: the top-level ``workflow_name ==
    # "<name>:FAILED:<CODE>"``, and the buried ``<hazard>.metrics.solver_version ==
    # "failed:<CODE>"``. The scan of the second is generic over the payload key. A marker with no
    # code still means failed and names the generic code. A run can append its solver_run_id
    # BEFORE failing, so the marker, not the run id, decides.
    wf = result.get("workflow_name")
    if isinstance(wf, str) and ":FAILED:" in wf:
        return wf.split(":FAILED:", 1)[1].strip() or _NO_LAYERS_CODE
    for payload in result.values():
        if not isinstance(payload, dict):
            continue
        metrics = payload.get("metrics")
        if not isinstance(metrics, dict):
            continue
        sv = metrics.get("solver_version")
        if isinstance(sv, str) and sv.startswith("failed:"):
            return sv.split("failed:", 1)[1].strip() or _NO_LAYERS_CODE
    return None


def _metrics_phrase(result: dict[str, Any]) -> str:
    """Render whatever NUMERIC metrics an envelope carries into an honest narration fragment. A
    solve that never reached the map still produced real numbers; metric NAMES carry their own
    units, so nothing here knows one hazard's vocabulary. Returns "" when there are none."""
    metrics: dict[str, Any] | None = None
    for payload in result.values():
        if isinstance(payload, dict) and isinstance(payload.get("metrics"), dict):
            metrics = payload["metrics"]
            break
    if not metrics:
        return ""
    return ", ".join(
        f"{name} {value:g}"
        for name, value in metrics.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    )


def _extract_synthetic_inputs(result: Any) -> list[dict[str, Any]]:
    """Pull the structured ``synthetic_inputs`` provenance list off a tool result; plain dicts, or
    ``[]`` when none is declared. NEVER raises."""

    def _as_dicts(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, (list, tuple)) or not value:
            return []
        out: list[dict[str, Any]] = []
        for entry in value:
            if isinstance(entry, dict):
                out.append(entry)
            elif hasattr(entry, "model_dump"):
                try:
                    out.append(entry.model_dump(mode="json"))
                except Exception:  # noqa: BLE001
                    continue
        return out

    # 1. top-level attribute (LayerURI subclass / result model)
    direct = _as_dicts(getattr(result, "synthetic_inputs", None))
    if direct:
        return direct
    # 2. dict key
    if isinstance(result, dict):
        found = _as_dicts(result.get("synthetic_inputs"))
        if found:
            return found
        # nested summary / derived_params dicts
        for key in ("summary", "derived_params"):
            sub = result.get(key)
            if isinstance(sub, dict):
                found = _as_dicts(sub.get("synthetic_inputs"))
                if found:
                    return found
        # a layers list of LayerURIs
        for layer in result.get("layers") or []:
            found = _as_dicts(getattr(layer, "synthetic_inputs", None))
            if found:
                return found
        return []
    # 3. result models that wrap a single primary layer
    for attr in ("layers",):
        seq = getattr(result, attr, None)
        if isinstance(seq, (list, tuple)):
            for layer in seq:
                found = _as_dicts(getattr(layer, "synthetic_inputs", None))
                if found:
                    return found
    for attr in ("asr_layer", "primary", "layer", "peak"):
        layer = getattr(result, attr, None)
        if layer is not None:
            found = _as_dicts(getattr(layer, "synthetic_inputs", None))
            if found:
                return found
    return []


def _hoist_synthetic_inputs(payload: dict[str, Any], result: Any) -> None:
    """Hoist a one-line ``assumptions_summary`` and its structured list to the TOP of the
    function_response, so where each input came from is narrated as ONE line
    and never a table. A no-op when none."""
    from trid3nt_contracts.common import render_assumptions_line

    entries = _extract_synthetic_inputs(result)
    if not entries:
        return
    line = render_assumptions_line(entries)
    if line:
        payload["assumptions_summary"] = line
    # keep the structured list too (clipped) so a consumer can enumerate it.
    payload["synthetic_inputs"] = entries[:12]


#: What the model is told to DO with a decline. The note names the card; this
#: names the response, so no caller re-words it and no model treats a decision
#: as a fault it must apologise for.
_DECLINE_GUIDANCE = (
    "This is the user's own decision and not an error: say plainly that they "
    "declined, state what you were about to do, and ask what they would like "
    "instead. Do not apologise for a failure and do not re-issue the same call "
    "unless they ask for it."
)


def summarize_tool_result(
    tool_name: str,
    result: Any,
    error: BaseException | None = None,
) -> dict[str, Any]:
    """Compact a tool result into the ``function_response.response`` payload.
    A SUMMARY, never the raw result: metadata, key metrics, error codes and
    counts. The dict always carries ``"tool"`` and ``"status"``."""
    import json as _json

    # An error becomes {status: "error", error_code, message, retryable, error_type}; code and
    # retryability come from the tool's own typed exception, so the model can retry with corrected
    # args or narrate honestly. The MAX_TURN_STEPS setting caps a runaway retry either way.
    if error is not None:
        from trid3nt_server.model.guards.actionability import classify_actionability

        # A gate card the user declined is NOT a tool failure: its own status names the card and
        # what it asked, so the model acknowledges the decision instead of apologising.
        if getattr(error, "declined", False):
            return {
                "tool": tool_name,
                "status": "declined",
                "error_code": getattr(error, "error_code", "USER_INPUT_CANCELLED"),
                "message": f"{error.decline_note} {_DECLINE_GUIDANCE}",
                "retryable": False,
            }

        code, retryable = _classify_error(error)
        message = str(error)
        actionability = classify_actionability(tool_name, error)
        if actionability == "operator":
            # Contract violation or internal exception: the model gets a terse, honest
            # acknowledgment ONLY -- nothing here for it to act on. The full exception was logged
            # at the dispatch site.
            message = "internal error, logged"
        envelope = {
            "tool": tool_name,
            "status": "error",
            "error_code": code,
            "message": message,
            "retryable": retryable,
            "error_type": type(error).__name__,
            "actionability": actionability,
        }
        # Typed no-data / recovery contract: a tool exception may carry a ``suggestions`` sequence
        # of short recovery options. It is surfaced as a STRUCTURED list so a small model relays the
        # options rather than inventing a next step.
        raw_suggestions = getattr(error, "suggestions", None)
        if isinstance(raw_suggestions, (list, tuple)):
            suggestions = [str(s) for s in raw_suggestions if str(s).strip()]
            if suggestions:
                envelope["suggestions"] = suggestions[:8]
        return _within_budget(envelope, "message")

    if result is None:
        return {"tool": tool_name, "status": "no_result"}

    # A chart-emission result carries a Vega-Lite spec with up to ~2000 INLINE rows: strip it and
    # surface a compact summary, so the model narrates from numbers and the budget holds.
    if (
        isinstance(result, dict)
        and result.get("envelope_type") == "chart-emission"
        and isinstance(result.get("vega_lite_spec"), dict)
    ):
        return _summarize_chart_emission(tool_name, result)

    # NET GUARANTEE: envelope_type=="modeled" AND an EMPTY ``layers`` list is NEVER stamped
    # status="ok". Two sub-cases: (a) FAILURE-TAGGED -- the depth-0 ``workflow_name`` carries
    # ":FAILED:", or a payload's ``metrics.solver_version`` starts with "failed:"; (b) the solve
    # COMPLETED but the layer was dropped at publish/render -> NO_RENDERABLE_LAYER with the
    # metrics. Keys off result STRUCTURE, not a raised exception (depth-2 collapse would hide it).
    # Do NOT broaden it to observed/fetched tools: they legitimately return non-layer data.
    if (
        isinstance(result, dict)
        and result.get("envelope_type") == "modeled"
        and not result.get("layers")
    ):
        code = _failed_modeled_envelope_error_code(result)
        if code is not None:
            # Sub-case (a): failure-tagged, whether or not a solver_run_id was appended.
            message = (
                f"{tool_name} produced no layers and the model did NOT run "
                f"successfully ({code})."
            )
            return {
                "tool": tool_name,
                "status": "error",
                "error_code": code,
                "message": message,
                "retryable": False,
                "error": message,
                "error_type": "FailedModelEnvelope",
            }
        # Sub-case (b): the numbers are surfaced so the narration is honest.
        metrics_phrase = _metrics_phrase(result)
        if metrics_phrase:
            message = (
                f"The simulation completed ({metrics_phrase}) but the result "
                f"layer could not be published/rendered - it is not on the map."
            )
        else:
            message = (
                f"{tool_name} completed but produced no renderable layer - "
                f"the result is not on the map."
            )
        return {
            "tool": tool_name,
            "status": "error",
            "error_code": "NO_RENDERABLE_LAYER",
            "message": message,
            "retryable": False,
            "error": message,
            "error_type": "NoRenderableLayer",
        }

    if isinstance(result, dict) and result.get("status") == "error":
        # A refusal the tool RETURNED (a template's fill naming every held input) is an error
        # the model corrects its call from, so it reaches the model as one, whole.
        return _within_budget({"tool": tool_name, **_whole(result)}, "error_message")
    if isinstance(result, dict):
        payload: dict[str, Any] = {
            "tool": tool_name,
            "status": "ok",
            "result": _whole(result),
        }
    else:
        payload = {
            "tool": tool_name,
            "status": "ok",
            "result": _whole(result) if isinstance(result, (list, tuple))
            else _coerce_to_summary_value(result),
        }
        # HONESTY FLOOR: a bare LayerURI result repr-coerces clipped to 200 chars, which would drop
        # the layer's ``notes``. They are hoisted to a top-level key so the model ALWAYS reads them
        # - a fallback source is named there, never mistaken for the primary.
        _notes = getattr(result, "notes", None)
        if isinstance(_notes, list) and _notes:
            payload["notes"] = [str(n) for n in _notes]
        # Same clipping hazard, second channel: ``fallback_warning`` is the LABELED degrade a
        # fetcher writes about its own composite (which source painted what share).
        _fb_warning = getattr(result, "fallback_warning", None)
        if isinstance(_fb_warning, str) and _fb_warning:
            payload["fallback_warning"] = _fb_warning

    # Whichever branch built the payload, hoist the structured input provenance: it is buried in
    # the coerced/repr'd result otherwise.
    _hoist_synthetic_inputs(payload, result)

    # A result that fits the budget reaches the model whole; one past it is summarized, then clipped.
    budget = setting("TOOL_RESULT_CHARS")
    try:
        encoded = _json.dumps(payload, default=str)
        if len(encoded) > budget and "result" in payload:
            payload["result"] = _coerce_to_summary_value(result)
            encoded = _json.dumps(payload, default=str)
    except Exception:  # noqa: BLE001 -- pathological non-serializable
        return {
            "tool": tool_name,
            "status": "ok",
            "result_repr": repr(result)[:1000],
            "note": "result not JSON-serializable; coerced via repr",
        }
    if len(encoded) > budget:
        return {
            "tool": tool_name,
            "status": "ok",
            "result_summary": encoded[:budget] + "…[clipped]",
            "note": "full result exceeded char budget; clipped for LLM context",
        }
    return payload


def _whole(value: Any) -> Any:
    """``value`` as JSON-able data with nothing cut; only the opaque byte fields are suppressed."""
    if isinstance(value, dict):
        return {str(k): f"[{k} suppressed]" if k in _OPAQUE_FIELDS else _whole(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_whole(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)


def _within_budget(envelope: dict[str, Any], field_name: str) -> dict[str, Any]:
    """``envelope`` with its one text field cut so the whole fits the result budget: a refusal is
    read whole, and only an unbounded one (an upstream body quoted in an exception) is cut."""
    import json as _json

    over = len(_json.dumps(envelope, default=str)) - setting("TOOL_RESULT_CHARS")
    text = envelope.get(field_name)
    if over > 0 and isinstance(text, str):
        envelope[field_name] = text[:max(len(text) - over - len(_CUT), 0)] + _CUT
    return envelope


# ``success`` (no raise, no failure-tagged envelope) is not ``result_usable``: a layer-producing
# tool can return status="ok" with an EMPTY layers list (a run that produced no renderable layer,
# or a publish/render drop). Usable keys off the same classifier as summarize_tool_result, so the
# two stay in lockstep. True = renderable layer or non-empty data; False = a layer-producer with
# no layer even when success; None = not applicable (meta tools, errored calls). Anything not
# positively classified returns None rather than guessing True.

#: Result keys whose presence marks a layer-producing return. A non-empty value
#: under any of these is a renderable artifact (LayerURI dict, gs://"/s3:// COG,
#: or a layers list). Mirrors the ``*_uri`` vocabulary the adapter already
#: tracks for handle-passing.
_LAYER_RESULT_KEYS = frozenset(
    {
        "layers",
        "layer_uri",
        "layer",
        "published_layers",
        "result_layers",
    }
)


def _result_has_renderable_layer(result: Any) -> bool | None:
    """Whether ``result`` carries a renderable layer artifact: ``True`` a layer survived, ``False``
    a layer-producer whose slot is empty, ``None`` not layer-shaped at all."""
    # LayerURI / pydantic-or-dataclass return with the two defining attributes.
    if not isinstance(result, (dict, str, bytes)) and result is not None:
        if hasattr(result, "layer_id") and hasattr(result, "uri"):
            return bool(getattr(result, "uri", None))
    if not isinstance(result, dict):
        return None
    # A modeled assessment envelope: the layers list is the renderable slot.
    if result.get("envelope_type") is not None:
        return bool(result.get("layers"))
    # Generic layer keys.
    saw_layer_key = False
    for key in _LAYER_RESULT_KEYS:
        if key in result:
            saw_layer_key = True
            val = result.get(key)
            if isinstance(val, (list, tuple, str)):
                if val:
                    return True
            elif val:
                return True
    if saw_layer_key:
        return False
    return None


def classify_result_usable(
    tool_name: str,
    result: Any,
    summary: dict[str, Any] | None,
) -> bool | None:
    """Classify whether a completed tool call produced a USABLE result.
    Keyed off the honesty-floor signal ``summarize_tool_result`` already
    stamped, so the two never diverge; NEVER raises, degrading to ``None``."""
    try:
        # 1. The honesty floor already decided this is an empty-layer modeled
        #    run (status=error + NO_RENDERABLE_LAYER). That is the canonical
        #    "succeeded but unusable" case.
        if isinstance(summary, dict):
            if summary.get("error_code") == "NO_RENDERABLE_LAYER":
                return False
        # 2. A failure-tagged modeled envelope also has no usable layer; keyed off the result
        #    STRUCTURE directly so it survives a change to the summary shape.
        if isinstance(result, dict) and result.get("envelope_type") == "modeled":
            if not result.get("layers"):
                return False
            return True
        # 3. Layer-shaped results: True iff a renderable layer survived.
        layer_state = _result_has_renderable_layer(result)
        if layer_state is not None:
            return layer_state
        # 4. None / no_result: the call produced nothing usable, but the notion
        #    of "renderable layer" doesn't apply -- treat as N/A (meta path).
        if result is None:
            return None
        # 5. A non-empty data payload from a non-layer tool (point query, table, scalar, count) IS a
        # usable result. An empty dict / empty string / empty collection is not. Anything else (a
        # populated dict, a number, a non-empty string) counts.
        if isinstance(result, dict):
            # Drop bookkeeping-only keys before judging emptiness so a dict that
            # carries ONLY a status/tool marker is not mistaken for data.
            data_keys = [
                k
                for k in result
                if k not in {"status", "tool", "envelope_type"}
            ]
            # Real data -> usable (True).
            return bool(data_keys) or None
        if isinstance(result, (list, tuple, set, str, bytes)):
            return bool(result)
        # A bare scalar (int/float/bool) return is a usable data result.
        return True
    except Exception:  # noqa: BLE001 -- classification must never break dispatch
        return None


async def stream_events(
    client: Any,
    model: str,
    user_text: str,
    tool_declarations: list[ToolDeclaration] | None = None,
    system_prompt: str | None = None,
    chat_history: list[dict] | None = None,
    model_cache_ref: str | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream the model's reply to ``user_text`` as typed ``StreamEvent``s.
    ``client`` is accepted for signature parity and IGNORED -- each provider
    adapter opens its own; an empty ``tool_declarations`` means text-only."""
    contents = build_contents_from_history(user_text, chat_history)
    async for event in stream_events_with_contents(
        contents,
        tool_declarations=tool_declarations,
        system_prompt=system_prompt,
    ):
        yield event


async def stream_events_with_contents(
    contents: list[Message],
    tool_declarations: list[ToolDeclaration] | None = None,
    system_prompt: str | None = None,
    model_id: str | None = None,
    show_thinking: bool = False,
) -> AsyncIterator[StreamEvent]:
    """Stream ONE model round from a fully-built ``contents`` list.
    ``show_thinking`` reaches the OpenAI adapter only; the dispatch is an
    EXPLICIT provider switch, and any other provider raises."""
    # Every adapter converts IR contents and tool declarations at its own boundary and yields the
    # SAME StreamEvent union.
    from .model_selection import model_provider
    from .scripted_adapter import model_provider_is_scripted, stream_scripted

    # scripted (aliases replay/fake) is intercepted FIRST so it never reaches a provider client.
    if model_provider_is_scripted():
        async for _ev in stream_scripted(
            contents=contents,
            tool_declarations=tool_declarations,
            system_prompt=system_prompt,
            model=model_id,
        ):
            yield _ev
        return

    if model_provider() == "anthropic":
        from .anthropic_adapter import stream_anthropic

        async for _ev in stream_anthropic(
            contents=contents,
            tool_declarations=tool_declarations,
            system_prompt=system_prompt,
            model=model_id,
        ):
            yield _ev
        return

    # The OpenAI-compatible adapter covers any endpoint that speaks chat.completions streaming
    # (Ollama, vLLM, llama.cpp, LM Studio, Groq, DeepSeek, OpenRouter).
    if model_provider() == "openai":
        from .openai_adapter import stream_openai
        async for _ev in stream_openai(
            contents=contents,
            tool_declarations=tool_declarations,
            system_prompt=system_prompt,
            model=model_id,
            show_thinking=show_thinking,
        ):
            yield _ev
        return

    # Dispatch is EXPLICIT: anything else is a TYPED error, never a silent empty turn.
    raise UnsupportedModelProviderError(
        f"MODEL_PROVIDER={model_provider()!r} is not supported. Valid providers: "
        "scripted/replay/fake, anthropic, openai."
    )


__all__ = [
    "StreamEvent",
    "TextDeltaEvent",
    "ThinkingDeltaEvent",
    "FunctionCallEvent",
    "UsageMetadataEvent",
    "CompactionStartEvent",
    "CompactionCompleteEvent",
    "UpstreamProviderError",
    "classify_provider_error_class",
    "provider_retries",
    "provider_backoff_wait",
    "NEVER_REHYDRATE_FIELDS",
    "system_prompt",
    "build_contents_from_history",
    "build_layers_present_note",
    "build_tool_declarations",
    "encode_parts_blob",
    "stream_events",
    "stream_events_with_contents",
    "summarize_tool_result",
    "classify_result_usable",
]
