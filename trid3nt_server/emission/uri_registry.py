"""Session-scoped layer-URI registry - layer-handle indirection.

One store, one scheme: a layer has exactly ONE ``s3://`` uri, so a record is a
single handle-to-uri binding. The registry is SESSION-scoped and survives a
reconnect; a NON-store string is never blocked, whatever it names.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
from collections import OrderedDict
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger("trid3nt_server.emission.uri_registry")

__all__ = [
    "RESOLVABLE_URI_PARAMS",
    "SHORT_HANDLE_RE",
    "SessionUriRegistry",
    "UriResolutionError",
    "activate_registry",
    "deactivate_registry",
    "get_uri_registry",
    "lookup_handle_for_uri",
    "lookup_uri_for_handle",
    "observe_published_layer",
    "reset_uri_registries_for_tests",
]



#: Param names that consume layer/raster/vector URIs and therefore resolve
#: through the registry at dispatch. Names, not tools -- the same param name
#: means the same thing across the catalog. DESTINATION params (where the
#: tool *writes*) and server-owned params (``project_qgs_uri``) must NOT be
#: listed: branch 4 would reject a not-yet-existing output path.
RESOLVABLE_URI_PARAMS: frozenset[str] = frozenset(
    {
        "hazard_raster_uri",
        "assets_uri",
        "layer_uri",
        "value_layer_uri",
        "zone_layer_uri",
        "value_raster_uri",
        "zone_input_uri",
        "forcing_raster_uri",
        "damage_layer_uri",
        "flood_layer_uri",
        "source_layer_uri",
        "raster_uri",
        "vector_uri",
        "polygon_uri",
        "dem_uri",
        "landcover_uri",
        "hazard_uri",
        "model_setup_uri",
        # compute_model_residuals: the MODEL raster + the OPTIONAL existing
        # observations vector layer -- both are handle/URI-resolved like the
        # other *_uri params above.
        "model_layer_uri",
        "observations_layer_uri",
        # ``compute_skill_metrics`` (paired obs/sim table) and
        # ``compute_flood_extent_skill`` (modeled + benchmark wet/dry extent)
        # take handle/URI params like the ones above. ``run_handle``
        # (read_run_diagnostics) is excluded -- it self-resolves and must
        # not be mangled here.
        "paired_table_uri",
        "model_extent_uri",
        "benchmark_extent_uri",
    }
)


#: The short per-case layer-handle shape the registry mints at the
#: emit seam (``L1``, ``L2``, ...). Case-insensitive on resolve (``l3`` works);
#: leading zeros normalize (``L07`` == ``L7``).
SHORT_HANDLE_RE = re.compile(r"^[Ll](\d+)$")

#: Guard against pathological mint growth (a session cannot realistically
#: produce this many distinct layer URIs; records themselves cap at 1024).
_SHORT_HANDLES_CAP = 4096

#: Minimum shared basename-stem prefix (chars) for the hash-prefix fuzzy
#: branch. The evidence shows ~14 hex chars survive before the tail
#: hallucination starts; 12 keeps headroom while staying collision-safe for
#: 32-hex cache keys.
_HASH_PREFIX_MIN = 12

#: Caps -- registries per process / records per registry / walk guards.
_REGISTRY_STORE_CAP = 4096
_RECORDS_PER_SESSION_CAP = 1024
_WALK_MAX_DEPTH = 8
_WALK_MAX_ITEMS = 64
_ANNOUNCE_CAP = 8
_ERROR_HANDLES_CAP = 10

#: Tools that consume a DEM as their primary input. When the branch-4 "no
#: layers yet" fallback fires for one of these, the message names ``fetch_dem``:
#: a generic solver example steers a terrain-derivative ask away from the call
#: it actually needs.
_DEM_CONSUMING_TOOLS: frozenset[str] = frozenset({"compute_cross_section"})




class UriResolutionError(RuntimeError):
    """An LLM-supplied URI param matched nothing the session ever produced.

    ``error_code``/``retryable`` are the class-attribute typed-error contract.
    """

    error_code = "URI_HANDLE_UNRESOLVED"
    retryable = True

    def __init__(self, param_name: str, value: str, inventory: str) -> None:
        self.param_name = param_name
        self.value = value
        super().__init__(
            f"{param_name}={value!r} does not match any layer this session "
            f"produced — do NOT construct storage paths/URIs. Pass a layer "
            f"handle (the short L<n> handle or the layer_id) from a prior "
            f"tool result instead. {inventory}"
        )




@dataclass
class UriRecord:
    """One registered layer/artifact: handle → its one exact URI."""

    handle: str
    uri: str | None = None  # the object-store uri; a layer has exactly one
    tool_name: str | None = None  # producer (for the inventory message)
    seq: int = 0  # registration order (recency tie-breaks)


def _is_object_store(value: str) -> bool:
    """True for a store uri.

    An unknown one in a layer-consuming param is a typed reject, not a pass-through.
    """
    return value.startswith("s3://")


#: Any RFC-3986-ish scheme prefix (s3://, http://, https://, file://, ...).
_URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _is_uri_shaped(value: str) -> bool:
    """True when ``value`` carries a URI scheme or a GDAL ``/vsi`` prefix.

    A uri-shaped value is NEVER placeholder-resolved.
    """
    return bool(_URI_SCHEME_RE.match(value)) or value.startswith("/vsi")


def _basename(uri: str) -> str:
    return posixpath.basename(urlparse(uri).path if "://" in uri else uri)


def _stem(uri: str) -> str:
    base = _basename(uri)
    stem, _dot, _ext = base.rpartition(".")
    return stem if stem else base


def _parent_dir(uri: str) -> str:
    return uri.rsplit("/", 1)[0] if "/" in uri else uri


def _path_segments(uri: str) -> list[str]:
    """Bucket + path segments of a store URI (for overlap scoring)."""
    body = uri[len("s3://"):] if uri.startswith("s3://") else uri
    return [seg for seg in body.split("/") if seg]


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


@dataclass
class SessionUriRegistry:
    """Handle -> URI indirection table for ONE session.
    Additive: a registered data uri is never clobbered by ``None``. Every method
    is synchronous and in-memory - the registry sits on the dispatch path.
    """

    session_id: str
    _records: OrderedDict[str, UriRecord] = field(default_factory=OrderedDict)
    _uri_to_handle: dict[str, str] = field(default_factory=dict)
    _seq: int = 0
    _pending_announcements: OrderedDict[str, str] = field(
        default_factory=OrderedDict
    )
    # Short per-case layer handles (``L<n>``), minted monotonically the moment a
    # record gains a DATA uri and persisted with the Case, so a reconnect resolves
    # the SAME handles the model already saw. ``_short_to_uri`` keys are canonical
    # ``L<n>``: uppercase, no zero padding.
    _short_to_uri: OrderedDict[str, str] = field(default_factory=OrderedDict)
    _uri_to_short: dict[str, str] = field(default_factory=dict)
    _short_seq: int = 0
    _shorts_dirty: bool = False


    def record(
        self,
        handle: str,
        *,
        uri: str | None = None,
        tool_name: str | None = None,
        announce: bool = True,
    ) -> None:
        """Register/merge one ``handle → URI`` association."""
        if not handle:
            return
        rec = self._records.get(handle)
        if rec is None:
            self._seq += 1
            rec = UriRecord(handle=handle, seq=self._seq)
            self._records[handle] = rec
            self._evict_if_needed()
        if uri:
            if rec.uri and rec.uri != uri:
                logger.info(
                    "uri_registry[%s]: handle %r uri updated %s -> %s",
                    self.session_id,
                    handle,
                    rec.uri,
                    uri,
                )
            rec.uri = uri
            self._uri_to_handle[uri] = handle
            self._mint_short(uri)
        if tool_name:
            rec.tool_name = tool_name
        if announce and rec.uri:
            self._pending_announcements[handle] = rec.uri
            while len(self._pending_announcements) > _ANNOUNCE_CAP:
                self._pending_announcements.popitem(last=False)

    def _evict_if_needed(self) -> None:
        while len(self._records) > _RECORDS_PER_SESSION_CAP:
            evicted_handle, evicted = self._records.popitem(last=False)
            if evicted.uri and self._uri_to_handle.get(evicted.uri) == evicted_handle:
                self._uri_to_handle.pop(evicted.uri, None)
            # Short handles deliberately SURVIVE record eviction --
            # an already-announced L<n> must keep resolving for the life of
            # the Case (the map is tiny: two strings per layer).


    def _mint_short(self, uri: str) -> str | None:
        """Mint the next ``L<n>`` for a DATA uri (idempotent per uri)."""
        existing = self._uri_to_short.get(uri)
        if existing is not None:
            return existing
        if len(self._short_to_uri) >= _SHORT_HANDLES_CAP:
            return None
        self._short_seq += 1
        short = f"L{self._short_seq}"
        self._short_to_uri[short] = uri
        self._uri_to_short[uri] = short
        self._shorts_dirty = True
        return short

    def short_for_uri(self, uri: str | None) -> str | None:
        """``uri`` -> its ``L<n>`` handle, or None."""
        if not uri:
            return None
        return self._uri_to_short.get(uri.strip())

    def uri_for_short(self, short: str) -> str | None:
        """Case-insensitive ``L<n>`` -> registered uri (None when unknown)."""
        m = SHORT_HANDLE_RE.match(short.strip())
        if m is None:
            return None
        return self._short_to_uri.get(f"L{int(m.group(1))}")

    def export_short_handles(self) -> dict[str, str]:
        """The persistable ``{L<n>: uri}`` map (mint order preserved)."""
        return dict(self._short_to_uri)

    def import_short_handles(self, mapping: dict[str, str] | None) -> None:
        """Restore a persisted ``{L<n>: uri}`` map; malformed entries are skipped.
        Mint numbers are honoured verbatim and the counter resumes PAST the imported
        maximum, so a fresh layer never re-uses a number the model has already seen.
        """
        if not mapping:
            return
        for raw_short, raw_uri in mapping.items():
            if not isinstance(raw_short, str) or not isinstance(raw_uri, str):
                continue
            m = SHORT_HANDLE_RE.match(raw_short.strip())
            uri = raw_uri.strip()
            if m is None or not uri:
                continue
            n = int(m.group(1))
            short = f"L{n}"
            self._short_to_uri[short] = uri
            self._uri_to_short.setdefault(uri, short)
            self._short_seq = max(self._short_seq, n)

    @property
    def shorts_dirty(self) -> bool:
        """True when the short-handle map has mints not yet persisted."""
        return self._shorts_dirty

    def mark_shorts_persisted(self) -> None:
        self._shorts_dirty = False

    def rewrite_result_for_llm(self, node: Any) -> Any:
        """Emit seam: registered URIs -> short handles, LLM-only.
        A rewritten COPY - the input is never mutated. An unregistered string
        passes through untouched, and a failure falls back to the input.
        """
        try:
            mapping: dict[str, str] = dict(self._uri_to_short)
            if not mapping:
                return node
            faces = sorted(mapping, key=len, reverse=True)
            return self._rewrite_node(node, mapping, faces, depth=0)
        except Exception:  # noqa: BLE001 -- the rewrite must never break emit
            logger.exception(
                "uri_registry[%s]: rewrite_result_for_llm failed",
                self.session_id,
            )
            return node

    def _rewrite_node(
        self, node: Any, mapping: dict[str, str], faces: list[str], depth: int
    ) -> Any:
        if depth > _WALK_MAX_DEPTH or node is None:
            return node
        if isinstance(node, str):
            hit = mapping.get(node)
            if hit is not None:
                return hit
            if "://" in node or node.startswith("/vsi"):
                out = node
                for face in faces:
                    if face in out:
                        out = out.replace(face, mapping[face])
                return out
            return node
        if isinstance(node, dict):
            return {
                k: self._rewrite_node(v, mapping, faces, depth + 1)
                for k, v in node.items()
            }
        if isinstance(node, (list, tuple)):
            seq = [
                self._rewrite_node(v, mapping, faces, depth + 1) for v in node
            ]
            return type(node)(seq)
        return node

    def register_tool_result(self, tool_name: str, result: Any) -> dict[str, str]:
        """Walk a tool result and register every URI-bearing structure.
        Returns only the announceable ``{handle: uri}`` pairs from THIS result;
        a minted bare-uri handle serves fuzzy matching and is never announced.
        """
        before = dict(self._pending_announcements)
        try:
            self._walk(result, tool_name, depth=0, seen=set())
        except Exception:  # noqa: BLE001 -- registration must never break dispatch
            logger.exception(
                "uri_registry[%s]: register_tool_result failed tool=%s",
                self.session_id,
                tool_name,
            )
        return {
            h: u for h, u in self._pending_announcements.items() if before.get(h) != u
        }

    def _walk(self, node: Any, tool_name: str, depth: int, seen: set[int]) -> None:
        if depth > _WALK_MAX_DEPTH or node is None:
            return
        # Pydantic models (LayerURI, AssessmentEnvelope, …) → dict.
        if hasattr(node, "model_dump") and callable(node.model_dump):
            try:
                node = node.model_dump(mode="json")
            except Exception:  # noqa: BLE001 -- non-pydantic duck; skip
                return
        if isinstance(node, str):
            self._register_bare_string(node, tool_name)
            return
        if isinstance(node, dict):
            if id(node) in seen:
                return
            seen.add(id(node))
            layer_id = node.get("layer_id")
            uri = node.get("uri")
            if isinstance(layer_id, str) and layer_id and isinstance(uri, str) and uri:
                self.record(layer_id, uri=uri, tool_name=tool_name)
            for key, value in list(node.items())[:_WALK_MAX_ITEMS]:
                if key in {"inline_geojson", "features", "geometry", "chat_history"}:
                    continue  # huge / URI-free subtrees
                self._walk(value, tool_name, depth + 1, seen)
            return
        if isinstance(node, (list, tuple)):
            if id(node) in seen:
                return
            seen.add(id(node))
            for item in list(node)[:_WALK_MAX_ITEMS]:
                self._walk(item, tool_name, depth + 1, seen)

    def _register_bare_string(self, value: str, tool_name: str) -> None:
        """Register a bare store uri under a minted handle.
        So a verbatim echo resolves, a mangled one fuzzy-matches, and the emit
        rewrite has a short handle to hand over.
        """
        value = value.strip()
        if not value or not _is_object_store(value) or value in self._uri_to_handle:
            return
        # Mint a stable handle from the basename stem; if that stem is already
        # a layer handle, attach the URI there instead.
        stem = _stem(value)
        if stem in self._records:
            self.record(stem, uri=value, tool_name=tool_name, announce=False)
        else:
            self.record(
                f"uri:{_basename(value)}",
                uri=value,
                tool_name=tool_name,
                announce=False,
            )

    def clear(self) -> None:
        """Drop every registered handle, uri and pending announcement.

        The short-handle map and its counter clear too: shorts are PER-CASE state.
        """
        self._records.clear()
        self._uri_to_handle.clear()
        self._pending_announcements.clear()
        self._short_to_uri.clear()
        self._uri_to_short.clear()
        self._short_seq = 0
        self._shorts_dirty = False

    def replace_from_layers(
        self, layers: Any, short_handles: dict[str, str] | None = None
    ) -> None:
        """Reset this registry to EXACTLY ``layers`` (case-switch seed).

        REPLACES, never merges: the registry reflects only the now-active Case.
        """
        # A registry is keyed by session, not by Case, so a case switch reuses
        # this same object. A merge would leak across it - a handle from the old
        # Case satisfying a call in the new one, or a stale uri winning a fuzzy
        # match over the right one.
        self.clear()
        # The persisted map is imported BEFORE the layer seed, so already-
        # announced handles keep their numbers and fresh layers mint past the
        # persisted maximum.
        self.import_short_handles(short_handles)
        try:
            self._walk(layers, "case-rehydration", depth=0, seen=set())
        except Exception:  # noqa: BLE001 -- best-effort seam
            logger.exception("uri_registry[%s]: seed failed", self.session_id)


    def drain_announcements(self) -> dict[str, str]:
        """Pop the handles registered since the last drain ({handle: uri})."""
        out = dict(self._pending_announcements)
        self._pending_announcements.clear()
        return out


    def resolve_params(self, tool_name: str, params: dict) -> dict:
        """Resolve every ``RESOLVABLE_URI_PARAMS`` member of ``params``.

        A fresh dict; a param outside the allowlist passes through untouched.
        """
        if not params:
            return params
        out = dict(params)
        for name, value in params.items():
            if name not in RESOLVABLE_URI_PARAMS or not isinstance(value, str):
                continue
            resolved = self._resolve_one(tool_name, name, value)
            if resolved != value:
                logger.warning(
                    "uri_registry[%s]: %s.%s resolved %r -> %r",
                    self.session_id,
                    tool_name,
                    name,
                    value,
                    resolved,
                )
                out[name] = resolved
        return out

    def _resolve_one(self, tool_name: str, param_name: str, value: str) -> str:
        v = value.strip()

        # Branch 2a -- value IS a registered handle (the desired steady state).
        rec = self._records.get(v)
        if rec is not None and rec.uri:
            return rec.uri

        # Branch 1 -- exact URI known: pass through verbatim.
        if v in self._uri_to_handle:
            return v

        # Branch 2b -- a SHORT layer handle (L<n>, case-insensitive).
        # The desired steady state after the emit rewrite: the LLM passes the
        # short handle it was shown; an UNKNOWN short handle is a typed reject
        # carrying the real inventory so the retry self-corrects.
        m = SHORT_HANDLE_RE.match(v)
        if m is not None:
            short_uri = self._short_to_uri.get(f"L{int(m.group(1))}")
            if short_uri is not None:
                return short_uri
            raise UriResolutionError(param_name, value, self._inventory_text(tool_name))

        # PLACEHOLDER resolution, for a model that emits a producer and its
        # consumer in the same iteration and passes a stand-in like
        # 'LayerURI_from_fetch_dem' as the uri param. Tool calls dispatch
        # SEQUENTIALLY, so the producer's real uri is registered by the time the
        # consumer resolves. Conservative by construction: only a non-uri-shaped,
        # non-path string naming exactly ONE producing tool with exactly ONE
        # distinct registered uri resolves; everything else falls through.
        placeholder_hit = self._resolve_placeholder(v)
        if placeholder_hit is not None:
            resolved_uri, producer = placeholder_hit
            logger.info(
                "uri_registry[%s]: placeholder resolved tool=%s param=%s "
                "placeholder=%r -> %r (producer=%s)",
                self.session_id,
                tool_name,
                param_name,
                value,
                resolved_uri,
                producer,
            )
            return resolved_uri

        # Non-store strings (plain https COG, local path, opaque token):
        # fail-open -- external links / user-pasted sources must never be
        # blocked; the consuming tool's own typed error follows.
        if not _is_object_store(v):
            return value

        # Branch 3 -- unknown store URI: fuzzy-match the mangle classes.
        substituted = self._fuzzy_match(v)
        if substituted is not None:
            return substituted

        # Branch 4 -- unknown store URI with no plausible
        # match where a LAYER is expected: TYPED REJECT. The session never
        # produced this path, so passing it through can only 404 downstream
        # (or worse, read the wrong object) -- raising here with the handle
        # inventory makes URI hallucination structurally impossible and feeds
        # the retry loop a self-correcting message.
        logger.warning(
            "uri_registry[%s]: rejecting unregistered store uri "
            "%s.%s=%r",
            self.session_id,
            tool_name,
            param_name,
            value,
        )
        raise UriResolutionError(param_name, value, self._inventory_text(tool_name))

    def _resolve_placeholder(self, value: str) -> tuple[str, str] | None:
        """Resolve a placeholder string to a producer's layer URI.

        ``(uri, producer)`` only on an unambiguous match, else ``None``.
        """
        # Three conditions, all required. The value is NOT uri-shaped and not a
        # filesystem path, because a well-formed unknown uri must keep the
        # fuzzy-match and typed-reject treatment rather than be substituted
        # silently. It textually contains the name of exactly ONE tool that
        # registered a uri this session, the longest name winning when two nest.
        # And that tool registered exactly ONE distinct uri - two candidates are
        # ambiguous, and an ambiguity is never guessed at.
        if _is_uri_shaped(value) or value.startswith(("/", "\\")):
            return None
        lowered = value.lower()
        by_producer: dict[str, list[UriRecord]] = {}
        for rec in self._records.values():
            if rec.uri and rec.tool_name and rec.tool_name != "case-rehydration":
                by_producer.setdefault(rec.tool_name, []).append(rec)
        matched = [name for name in by_producer if name.lower() in lowered]
        if len(matched) > 1:
            # Nested tool names: drop any match that is a substring of a
            # longer matched name ('fetch_dem' loses to 'fetch_dem_hires').
            matched = [
                n
                for n in matched
                if not any(m != n and n.lower() in m.lower() for m in matched)
            ]
        if len(matched) != 1:
            return None
        producer = matched[0]
        uris = {rec.uri for rec in by_producer[producer] if rec.uri}
        if len(uris) != 1:
            return None  # ambiguous - never guess between two layers
        return next(iter(uris)), producer

    def _fuzzy_match(self, v: str) -> str | None:
        """Match an unknown store URI against the registered inventory.
        Every sub-branch below needs a UNIQUE winner; an ambiguity is never
        guessed at and falls through to the typed reject.
        """
        known = [
            rec.uri
            for rec in self._records.values()
            if rec.uri and _is_object_store(rec.uri)
        ]
        if not known:
            return None
        base = _basename(v)
        stem = _stem(v)
        ext = base.rpartition(".")[2] if "." in base else ""

        # (a) layer_id grafted on as the basename.
        rec = self._records.get(stem)
        if rec is not None and rec.uri:
            return rec.uri

        # (b) exact basename elsewhere -- path-segment mangle.
        same_base = [u for u in known if _basename(u) == base]
        if len(same_base) == 1:
            return same_base[0]
        if len(same_base) > 1:
            v_segs = set(_path_segments(v))
            scored = sorted(
                same_base,
                key=lambda u: len(v_segs & set(_path_segments(u))),
                reverse=True,
            )
            top = len(v_segs & set(_path_segments(scored[0])))
            second = len(v_segs & set(_path_segments(scored[1])))
            if top > second:
                return scored[0]
            return None  # ambiguous -- branch 4 lists the handles

        # (c) hash-prefix: tail hallucinated past >= _HASH_PREFIX_MIN chars.
        prefixed = [
            (u, _common_prefix_len(_stem(u), stem))
            for u in known
            if (_basename(u).rpartition(".")[2] if "." in _basename(u) else "") == ext
        ]
        prefixed = [(u, n) for (u, n) in prefixed if n >= _HASH_PREFIX_MIN]
        if prefixed:
            prefixed.sort(key=lambda t: t[1], reverse=True)
            if len(prefixed) == 1 or prefixed[0][1] > prefixed[1][1]:
                return prefixed[0][0]
            return None  # two equally-plausible hashes -- refuse to guess

        # (d) unique same-directory candidate.
        parent = _parent_dir(v)
        same_dir = [u for u in known if _parent_dir(u) == parent]
        if len(same_dir) == 1:
            return same_dir[0]
        return None


    def _inventory_text(self, tool_name: str | None = None) -> str:
        """Compact handle inventory for the branch-4 error message.
        Tool-aware only when the registry genuinely has no layers; otherwise the
        handle listing, capped at ``_ERROR_HANDLES_CAP``.
        """
        layer_recs = [
            r
            for r in self._records.values()
            if r.uri and not r.handle.startswith("uri:")
        ]
        layer_recs.sort(key=lambda r: r.seq, reverse=True)
        if not layer_recs:
            if tool_name in _DEM_CONSUMING_TOOLS:
                return (
                    "No layers have been produced this session yet — run "
                    "fetch_dem for this AOI first to get a dem_uri handle, "
                    f"then retry {tool_name}."
                )
            return (
                "No layers have been produced this session yet — run the "
                "producing tool first (e.g. sfincs_flood for a "
                "flood-depth raster, fetch_usace_nsi for building assets)."
            )
        def _one(r: UriRecord) -> str:
            # Lead with the short handle when one exists so the
            # retry passes ``L<n>`` (the cheapest, unmangleable form).
            short = self._uri_to_short.get(r.uri) if r.uri else None
            base = f"{r.handle} (from {r.tool_name or 'unknown'})"
            return f"{short} = {base}" if short else base

        lines = ", ".join(_one(r) for r in layer_recs[:_ERROR_HANDLES_CAP])
        return f"Known handles: {lines}."

    def known_handles(self) -> list[str]:
        """The registered handles, minted fuzzy-match records excluded."""
        return [h for h in self._records if not h.startswith("uri:")]


# Module-level session store: survives reconnects, and is shared across a
# session's sibling WebSocket connections

_SESSION_URI_REGISTRIES: OrderedDict[str, SessionUriRegistry] = OrderedDict()


def get_uri_registry(session_id: str) -> SessionUriRegistry:
    """Return (creating if needed) the registry for ``session_id``."""
    reg = _SESSION_URI_REGISTRIES.get(session_id)
    if reg is None:
        while len(_SESSION_URI_REGISTRIES) >= _REGISTRY_STORE_CAP:
            _SESSION_URI_REGISTRIES.popitem(last=False)
        reg = SessionUriRegistry(session_id=session_id)
        _SESSION_URI_REGISTRIES[session_id] = reg
    return reg


def reset_uri_registries_for_tests() -> None:
    """Test hook -- wipe the module-level store."""
    _SESSION_URI_REGISTRIES.clear()



_ACTIVE_REGISTRY: ContextVar[SessionUriRegistry | None] = ContextVar(
    "trid3nt_active_uri_registry", default=None
)


def activate_registry(reg: SessionUriRegistry) -> Token:
    """Bind ``reg`` as the ambient registry for the current dispatch."""
    return _ACTIVE_REGISTRY.set(reg)


def deactivate_registry(token: Token) -> None:
    _ACTIVE_REGISTRY.reset(token)


def lookup_uri_for_handle(handle: str) -> str | None:
    """The DATA uri a layer handle stands for - a short ``L<n>`` or a full layer id.

    ``None`` outside an active dispatch, or for a handle nothing published.
    """
    reg = _ACTIVE_REGISTRY.get()
    if reg is None or not handle:
        return None
    key = handle.strip()
    short = reg._short_to_uri.get(key.upper())
    if short:
        return short
    record = reg._records.get(key)
    return record.uri if record is not None else None


def lookup_handle_for_uri(
    uri: str, registry: SessionUriRegistry | None = None
) -> str | None:
    """Return the registered LAYER handle whose data uri is ``uri``.
    A minted ``uri:`` handle is never returned - it is fuzzy-match plumbing, not
    a layer id. ``None`` outside an active dispatch.
    """
    reg = registry if registry is not None else _ACTIVE_REGISTRY.get()
    if reg is None or not uri:
        return None
    handle = reg._uri_to_handle.get(uri.strip())
    if handle and not handle.startswith("uri:"):
        return handle
    return None


def observe_published_layer(layer_id: str, uri: str | None = None) -> None:
    """Record a published layer's handle and the one uri it carries.

    A no-op outside an active dispatch, and it never raises into its caller.
    """
    reg = _ACTIVE_REGISTRY.get()
    if reg is None:
        return
    try:
        reg.record(layer_id, uri=uri, tool_name="publish_layer")
    except Exception:  # noqa: BLE001 -- observation must never break the tool
        logger.exception("observe_published_layer failed layer_id=%s", layer_id)


# The 32-hex cache-stem convention, stated. Resolution does not use it: prefix
# matching is shape-agnostic.
HASH_STEM_RE = re.compile(r"^[0-9a-f]{32}$")
