"""Session-scoped layer-URI registry — layer-handle indirection (job-0263).

Kills the LLM-URI-mangling incident class observed live in Stage 3 / the
user's Tampa demo run. Gemini is structurally bad at echoing long opaque
URIs between turns — five distinct live incidents proved it:

1. **runs/ prefix mangle** (job-0253, agent_restart_0253.log:475): the real
   COG ``s3://trid3nt-runs/01KTS5W9GTE7A7WPC3BNBE10EQ/
   flood_depth_peak.tif`` came back as ``s3://trid3nt-runs/
   runs/01KTS5W9.../flood_depth_peak.tif`` (doubled path segment) → 404.
2. **layer_id-as-basename invention** (same call): ``assets_uri`` was
   ``gs://…/usace_nsi/usace-nsi--81.9126-26.5476--81.7511-26.6892.fgb`` —
   Gemini grafted the *layer_id* onto the cache directory instead of the
   real hash basename ``852a6cc379b18c865bf9d99ec1acaa35.fgb``.
3. **hash-tail hallucination ×3** (job-0257 report, /tmp/agent_demo_ready.log):
   ``090a4ff8d9a083f67c0b355caf40241a.tif`` echoed as
   ``090a4ff8d9a083b28499252309d12999.tif`` — first ~14 hex chars preserved,
   tail invented. Three out of three publishes.
4. **WMS-URL-as-hazard** (job-0255, agent_log_p5_turn.txt:170): the QGIS
   display URL ``https://…/ogc/wms?MAP=…&LAYERS=flood-depth-peak-01KTS8H8…``
   passed as ``hazard_raster_uri`` to Pelicun (which needs the gs:// COG).
5. **invented cache hash** (same call): ``assets_uri`` =
   ``gs://…/usace_nsi/20240516140505.fgb`` — a timestamp-shaped basename
   that never existed.

Prompt-engineering patches (job-0252 / job-0255 SYSTEM_PROMPT clauses) only
lowered the rate. THIS module removes the failure mode architecturally:

* Every tool result that carries URIs gets **registered** as
  ``handle → exact URI`` where the handle is the ``layer_id`` (or a minted
  stable key for bare URIs). Handles are surfaced to Gemini in the
  function_response, and the SYSTEM_PROMPT instructs it to pass handles —
  never raw ``gs://`` paths.
* Every URI-consuming tool param (``hazard_raster_uri``, ``assets_uri``,
  ``layer_uri``, …) **resolves** through the registry at dispatch:

  1. value is a known handle            → substitute the registered URI;
  2. value is an exactly-known URI      → pass through (verbatim echo is
     fine; a known *display* WMS URL is mapped back to the data URI);
  3. unknown but *close* to a registered URI (same basename, ≥12-char hash
     prefix, layer_id-as-basename, or unique same-directory candidate)
     → substitute + WARNING (the mangle classes above);
  4. unknown with no plausible match    → ADR 0014: object-store URIs
     (``gs://`` / ``s3://``) and display-only faces (WMS / tile-template
     URLs with no recoverable data URI) raise a typed retryable error
     (``URI_HANDLE_UNRESOLVED``) that TELLS the model which handles exist,
     so it self-corrects instead of inventing again. Non-object-store
     strings (external http(s) links, local paths, opaque tokens) still
     FAIL OPEN — user-supplied sources are never blocked.

ADR 0014 (layer handles, not URIs): alongside the ``layer_id`` handles above,
the registry mints SHORT per-case handles (``L1``, ``L2``, ...) the moment a
record gains a data URI. The emit seam (server.py) rewrites the LLM-facing
function_response so the model only ever sees ``L<n>`` where a registered URI
would appear; dispatch resolves ``L<n>`` (case-insensitive) back to the exact
URI. The ``{L<n>: uri}`` map persists WITH the Case (storage-only field) so a
reconnect/reopen resolves the same handles. Plugin-bound wire envelopes are
untouched — they keep the real uri the plugin renders from.

Scoping rules:

* The registry is **session-scoped** and lives in a module-level store keyed
  by ``session_id`` (the ``_SESSION_ACTIVE_CASE`` pattern from job-0259) so
  it survives WebSocket reconnects and is shared across the client's
  sibling connections.
* Unknown storage URIs pass through untouched (fail-open): user-supplied
  data must never be blocked, and a stale or invented path fails downstream
  with the consuming tool's own honest typed error. (The legacy-cloud-era
  managed-bucket strict-reject died with the cloud decommission -- nothing
  local mints the old bucket names anymore.)
* Composer-internal publishes (``run_model_flood_scenario`` →
  ``publish_layer``) are captured via a ``ContextVar`` observation hook:
  ``publish_layer`` calls :func:`observe_published_layer` with the
  (validated) gs:// COG + the WMS display URL, so the registry knows BOTH
  faces of a published layer even though the composer's envelope only
  carries the WMS URL.

Wired in ``server._invoke_tool_via_emitter`` (resolution before dispatch,
registration after) — see server.py. Unit coverage in
``tests/test_uri_registry.py`` replays all five incident shapes with the
real logged values.
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

logger = logging.getLogger("trid3nt_server.uri_registry")

__all__ = [
    "NESTED_REF_PARAMS",
    "RESOLVABLE_URI_PARAMS",
    "SHORT_HANDLE_RE",
    "SessionUriRegistry",
    "UriResolutionError",
    "activate_registry",
    "ambient_layer_handle_inventory",
    "deactivate_registry",
    "get_uri_registry",
    "lookup_handle_for_uri",
    "observe_published_layer",
    "reset_uri_registries_for_tests",
]


# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

#: Param names that consume layer/raster/vector URIs and therefore resolve
#: through the registry at dispatch. Names, not tools — the same param name
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
        "base_layer_uri",  # job-0319: compute_blended_composite base raster
        "overlay_layer_uri",  # job-0319: compute_blended_composite overlay raster
        "landcover_uri",
        "hazard_uri",
        "model_setup_uri",
        # compute_model_residuals: the MODEL raster + the OPTIONAL existing
        # observations vector layer -- both are handle/URI-resolved like the
        # other *_uri params above.
        "model_layer_uri",
        "observations_layer_uri",
    }
)

#: ADR 0014: params whose VALUES are handle/URI mappings (not a single string).
#: ``code_exec_request.layer_refs`` is ``{var_name: layer_uri}`` (values may
#: also be LISTS of URIs); every string value resolves through the same
#: four-branch machinery as the flat ``*_uri`` params above. ``layer_uris`` is
#: the documented alias the LLM sometimes uses.
NESTED_REF_PARAMS: frozenset[str] = frozenset({"layer_refs", "layer_uris"})

#: ADR 0014: the short per-case layer-handle shape the registry mints at the
#: emit seam (``L1``, ``L2``, ...). Case-insensitive on resolve (``l3`` works);
#: leading zeros normalize (``L07`` == ``L7``).
SHORT_HANDLE_RE = re.compile(r"^[Ll](\d+)$")

#: Guard against pathological mint growth (a session cannot realistically
#: produce this many distinct layer URIs; records themselves cap at 1024).
_SHORT_HANDLES_CAP = 4096

#: Minimum shared basename-stem prefix (chars) for the hash-prefix fuzzy
#: branch. The job-0257 evidence shows ~14 hex chars survive before the tail
#: hallucination starts; 12 keeps headroom while staying collision-safe for
#: 32-hex cache keys.
_HASH_PREFIX_MIN = 12

#: Caps — registries per process / records per registry / walk guards.
_REGISTRY_STORE_CAP = 4096
_RECORDS_PER_SESSION_CAP = 1024
_WALK_MAX_DEPTH = 8
_WALK_MAX_ITEMS = 64
_ANNOUNCE_CAP = 8
_ERROR_HANDLES_CAP = 10

#: F32: tools that consume a DEM as their primary input. When the branch-4
#: "no layers yet" fallback fires for one of these, suggest ``fetch_dem`` —
#: the generic ``run_model_flood_scenario`` example was actively misleading
#: for a terrain-derivative ask (live incident: a reconnect-empty registry +
#: the generic suggestion steered the model away from the actually-needed
#: ``fetch_dem`` call).
_DEM_CONSUMING_TOOLS: frozenset[str] = frozenset(
    {
        "compute_hillshade",
        "compute_slope",
        "compute_aspect",
        "compute_contours",
        "compute_terrain_profile",
    }
)


# --------------------------------------------------------------------------- #
# Typed error (branch 4) — adapter._classify_error harvests the class attrs
# --------------------------------------------------------------------------- #


class UriResolutionError(RuntimeError):
    """An LLM-supplied URI param matched nothing the session ever produced.

    ``error_code`` / ``retryable`` follow the FR-AS-11 typed-exception
    convention so ``summarize_tool_result`` renders the structured envelope
    and Gemini retries with a handle instead of re-inventing a path.
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


# --------------------------------------------------------------------------- #
# Record + registry
# --------------------------------------------------------------------------- #


@dataclass
class UriRecord:
    """One registered layer/artifact: handle → its exact URI face(s)."""

    handle: str
    uri: str | None = None  # canonical consumable URI (gs:// preferred)
    wms_url: str | None = None  # QGIS display URL when known
    tool_name: str | None = None  # producer (for the inventory message)
    seq: int = 0  # registration order (recency tie-breaks)


def _normalize_gs(value: str) -> str:
    """``/vsigs/bucket/key`` → ``gs://bucket/key``; everything else verbatim."""
    if value.startswith("/vsigs/"):
        return "gs://" + value[len("/vsigs/") :]
    return value


def _is_gs(value: str) -> bool:
    return value.startswith("gs://")


def _is_object_store(value: str) -> bool:
    """True for the object-store schemes this stack (or its legacy) mints.

    ADR 0014: these are exactly the shapes the LLM historically hallucinated
    (gs:// on the legacy cloud, s3:// on the local MinIO stack) — an UNKNOWN
    one in a layer-consuming param is a typed reject, never a pass-through.
    """
    return value.startswith(("gs://", "s3://"))


#: Any RFC-3986-ish scheme prefix (s3://, gs://, http://, https://, file://, ...).
_URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _is_uri_shaped(value: str) -> bool:
    """True when ``value`` carries a URI scheme or a GDAL /vsi prefix.

    uri-shaped values are NEVER placeholder-resolved (a hallucinated but
    well-formed gs:///s3:// path could be a real cross-case reference; the
    existing branch-3/branch-4 machinery owns those).
    """
    return bool(_URI_SCHEME_RE.match(value)) or value.startswith("/vsi")


def _looks_like_wms(value: str) -> bool:
    if not value.startswith(("http://", "https://")):
        return False
    lowered = value.lower()
    return "service=wms" in lowered or "layers=" in lowered or "/wms" in lowered


def _is_tile_template(value: str) -> bool:
    """A TiTiler / XYZ tile-template URL — a DISPLAY face, not a data URI.

    LEGACY GUARD (TiTiler exit, 2026-07): ``publish_layer`` now emits the raw
    ``s3://`` COG URI and no longer mints tile templates, but OLD persisted
    cases (and the register-only manifest path) still carry template URIs
    that rehydrate through here — this guard MUST stay so those legacy
    display faces keep routing/unwrapping correctly.

    The AWS backend published rasters as TiTiler tile templates
    (``https://<cf>/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3%3A%2F%2F…``)
    rather than QGIS-Server WMS URLs. Like a WMS URL, the template is the
    renderable face — it carries ``{z}/{x}/{y}`` placeholders and cannot be
    opened by an analytical tool (Pelicun, zonal stats). It must route to the
    ``wms_url`` slot so it never displaces the registered ``s3://`` COG that
    downstream ``*_uri`` params resolve to (job-0304: live Pelicun read the
    template instead of the COG and failed). ``_looks_like_wms`` misses it
    (no ``service=wms`` / ``/wms`` / ``layers=``), hence this companion.
    """
    if not value.startswith(("http://", "https://")):
        return False
    lowered = value.lower()
    return "/cog/tiles/" in lowered or "{z}/{x}/{y}" in lowered


def _is_render_face(value: str) -> bool:
    """True when ``value`` is a renderable display URL (WMS or tile template)."""
    return _looks_like_wms(value) or _is_tile_template(value)


def _wms_layer_id(value: str) -> str | None:
    """Extract the ``LAYERS=`` value from a WMS-style URL (case-insensitive)."""
    try:
        q = parse_qs(urlparse(value).query)
    except Exception:  # noqa: BLE001 — malformed URL; treat as opaque
        return None
    for key, vals in q.items():
        if key.lower() == "layers" and vals and vals[0]:
            # Multiple layers possible; the publish convention is one.
            return vals[0].split(",")[0].strip() or None
    return None


def _titiler_cog_uri(value: str) -> str | None:
    """Unquote the ``url=<s3/gs COG>`` query param of a TiTiler tile template.

    Mirrors :func:`pipeline_emitter._layer_identity_key`: a TiTiler display URL
    (``https://<cf>/cog/tiles/.../{z}/{x}/{y}.png?url=s3%3A%2F%2F…``) embeds the
    real data COG as its (URL-encoded) ``url=`` param. Returns the unquoted COG,
    or ``None`` when there is no ``url=`` param (a foreign/malformed template).
    """
    try:
        q = parse_qs(urlparse(value).query)
    except Exception:  # noqa: BLE001 — malformed URL; treat as opaque
        return None
    cog = q.get("url")
    return unquote(cog[0]) if cog and cog[0] else None


def _basename(uri: str) -> str:
    return posixpath.basename(urlparse(uri).path if "://" in uri else uri)


def _stem(uri: str) -> str:
    base = _basename(uri)
    stem, _dot, _ext = base.rpartition(".")
    return stem if stem else base


def _parent_dir(uri: str) -> str:
    return uri.rsplit("/", 1)[0] if "/" in uri else uri


def _path_segments(uri: str) -> list[str]:
    """Bucket + path segments of an object-store URI (for overlap scoring)."""
    body = uri
    for scheme in ("gs://", "s3://"):
        if body.startswith(scheme):
            body = body[len(scheme) :]
            break
    return [seg for seg in body.split("/") if seg]


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


@dataclass
class SessionUriRegistry:
    """Handle → URI indirection table for ONE session (job-0263).

    Registration is additive (latest non-None face wins; a gs:// data URI is
    never clobbered by ``None``). Resolution implements the four branches
    documented in the module docstring. All methods are synchronous and
    in-memory — the registry sits on the hot dispatch path.
    """

    session_id: str
    _records: OrderedDict[str, UriRecord] = field(default_factory=OrderedDict)
    _uri_to_handle: dict[str, str] = field(default_factory=dict)
    _seq: int = 0
    _pending_announcements: OrderedDict[str, str] = field(
        default_factory=OrderedDict
    )
    # ADR 0014: short per-case layer handles (``L<n>``). Minted monotonically
    # the moment a record gains a DATA uri; persisted with the Case (see
    # server._persist_case_layer_handles) so a reconnect/reopen resolves the
    # SAME handles the LLM already saw. ``_short_to_uri`` keys are canonical
    # ``L<n>`` (uppercase, no zero padding).
    _short_to_uri: OrderedDict[str, str] = field(default_factory=OrderedDict)
    _uri_to_short: dict[str, str] = field(default_factory=dict)
    _short_seq: int = 0
    _shorts_dirty: bool = False

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def record(
        self,
        handle: str,
        *,
        uri: str | None = None,
        wms_url: str | None = None,
        tool_name: str | None = None,
        announce: bool = True,
    ) -> None:
        """Register/merge one ``handle → URI`` association."""
        if not handle:
            return
        uri = _normalize_gs(uri) if uri else None
        rec = self._records.get(handle)
        if rec is None:
            self._seq += 1
            rec = UriRecord(handle=handle, seq=self._seq)
            self._records[handle] = rec
            self._evict_if_needed()
        if uri:
            if _is_render_face(uri):
                # A renderable display URL (QGIS WMS *or* a TiTiler tile
                # template) landed in the ``uri`` slot (the flood composer
                # substitutes it per the layer-emission contract) — keep it on
                # the wms face; never displace a real data URI.
                rec.wms_url = rec.wms_url or uri
                self._uri_to_handle.setdefault(uri, handle)
            else:
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
        if wms_url:
            rec.wms_url = wms_url
            self._uri_to_handle.setdefault(wms_url, handle)
        if tool_name:
            rec.tool_name = tool_name
        if announce and rec.uri:
            self._pending_announcements[handle] = rec.uri
            while len(self._pending_announcements) > _ANNOUNCE_CAP:
                self._pending_announcements.popitem(last=False)

    def _evict_if_needed(self) -> None:
        while len(self._records) > _RECORDS_PER_SESSION_CAP:
            evicted_handle, evicted = self._records.popitem(last=False)
            for u in (evicted.uri, evicted.wms_url):
                if u and self._uri_to_handle.get(u) == evicted_handle:
                    self._uri_to_handle.pop(u, None)
            # ADR 0014: short handles deliberately SURVIVE record eviction —
            # an already-announced L<n> must keep resolving for the life of
            # the Case (the map is tiny: two strings per layer).

    # ------------------------------------------------------------------ #
    # ADR 0014 — short per-case layer handles (L<n>)
    # ------------------------------------------------------------------ #

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
        """``uri`` (data COG or display face) -> its ``L<n>`` handle, or None."""
        if not uri:
            return None
        norm = _normalize_gs(uri.strip())
        short = self._uri_to_short.get(norm)
        if short is not None:
            return short
        # A display face maps through its record to the data uri's handle.
        handle = self._uri_to_handle.get(norm)
        if handle is not None:
            rec = self._records.get(handle)
            if rec is not None and rec.uri:
                return self._uri_to_short.get(rec.uri)
        return None

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
        """Restore a persisted ``{L<n>: uri}`` map (Case reopen/reconnect).

        Existing mint numbers are honored verbatim; the monotonic counter
        resumes PAST the imported maximum so fresh layers never re-use a
        number the LLM has already seen. Malformed entries are skipped.
        Does NOT mark the map dirty (it just came FROM persistence).
        """
        if not mapping:
            return
        for raw_short, raw_uri in mapping.items():
            if not isinstance(raw_short, str) or not isinstance(raw_uri, str):
                continue
            m = SHORT_HANDLE_RE.match(raw_short.strip())
            uri = _normalize_gs(raw_uri.strip())
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
        """ADR 0014 emit seam: registered URIs -> short handles, LLM-only.

        Returns a REWRITTEN COPY of ``node`` (a function_response summary)
        in which every registered layer URI face (the data COG *and* its
        WMS/tile display URL) is replaced by the layer's short ``L<n>``
        handle — exact string matches are swapped outright; URIs embedded
        inside longer strings are substring-replaced (longest face first so
        a display URL that EMBEDS its COG is consumed whole). Unregistered
        strings pass through untouched, so external links the model must
        cite survive. The input is never mutated; the PLUGIN-bound wire
        envelopes are built from the LayerURI objects elsewhere and keep
        carrying the real uri. Never raises (falls back to the input).
        """
        try:
            mapping: dict[str, str] = dict(self._uri_to_short)
            for rec in self._records.values():
                if rec.wms_url and rec.uri:
                    short = self._uri_to_short.get(rec.uri)
                    if short:
                        mapping.setdefault(rec.wms_url, short)
            if not mapping:
                return node
            faces = sorted(mapping, key=len, reverse=True)
            return self._rewrite_node(node, mapping, faces, depth=0)
        except Exception:  # noqa: BLE001 — the rewrite must never break emit
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
            hit = mapping.get(node) or mapping.get(_normalize_gs(node))
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

        Returns the ``{handle: uri}`` pairs registered from THIS result
        (layer-handle registrations only — minted bare-URI handles support
        fuzzy matching but aren't announced to Gemini).
        """
        before = dict(self._pending_announcements)
        try:
            self._walk(result, tool_name, depth=0, seen=set())
        except Exception:  # noqa: BLE001 — registration must never break dispatch
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
            except Exception:  # noqa: BLE001 — non-pydantic duck; skip
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
        value = value.strip()
        if not value:
            return
        norm = _normalize_gs(value)
        # ADR 0014: s3:// joins gs:// — the local stack (MinIO) mints s3://
        # object keys (run frames, model_setup artifacts, published COGs);
        # they must register so verbatim echoes dual-accept, mangles fuzzy-
        # match, and the emit rewrite can hand the LLM a short handle.
        if _is_object_store(norm):
            if norm in self._uri_to_handle:
                return
            # Mint a stable handle from the basename stem; if that stem is
            # already a layer handle, attach the URI there instead.
            stem = _stem(norm)
            if stem in self._records:
                self.record(stem, uri=norm, tool_name=tool_name, announce=False)
            else:
                self.record(
                    f"uri:{_basename(norm)}",
                    uri=norm,
                    tool_name=tool_name,
                    announce=False,
                )
            return
        if _looks_like_wms(norm):
            layer_id = _wms_layer_id(norm)
            if layer_id:
                self.record(layer_id, wms_url=norm, tool_name=tool_name)

    def seed_from_layers(self, layers: Any) -> None:
        """Seed from persisted Case ``loaded_layers`` (rehydration path).

        ADDITIVE — merges into whatever this registry already holds. Callers
        switching the active Case on a connection (case-open / case-switch)
        must use :meth:`replace_from_layers` instead so a prior Case's
        handles don't leak into the new Case's inventory/resolution.
        """
        try:
            self._walk(layers, "case-rehydration", depth=0, seen=set())
        except Exception:  # noqa: BLE001 — best-effort seam
            logger.exception("uri_registry[%s]: seed failed", self.session_id)

    def clear(self) -> None:
        """Drop every registered handle/URI/pending-announcement (F32).

        ADR 0014: the short-handle map + its counter clear too — shorts are
        PER-CASE state; a case-switch reseeds them from the new Case's
        persisted map (``replace_from_layers(short_handles=...)``).
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
        """Reset this registry to EXACTLY ``layers`` (F32 case-switch seed).

        The registry is keyed by ``session_id``, not by Case — a session that
        switches Cases (or a fresh connection that opens an existing Case)
        reuses the SAME ``SessionUriRegistry``. ``seed_from_layers`` alone is
        additive, so a prior Case's handles/URIs would keep resolving after
        the switch (a cross-case leak: a handle from Case A could satisfy a
        Case B tool call, or a stale Case A URI could win a fuzzy match over
        the correct Case B one). Case-open / case-switch call sites clear
        first so the registry reflects ONLY the now-active Case's persisted
        layers, mirroring the emitter's ``reset_loaded_layers`` (replace, not
        reconcile — job-0245's rule applied here too).

        ADR 0014: ``short_handles`` is the Case's PERSISTED ``{L<n>: uri}``
        map — imported BEFORE the layer seed so already-announced handles
        keep their numbers and fresh layers mint PAST the persisted maximum.
        """
        self.clear()
        self.import_short_handles(short_handles)
        self.seed_from_layers(layers)

    # ------------------------------------------------------------------ #
    # Announcements (function_response surfacing)
    # ------------------------------------------------------------------ #

    def drain_announcements(self) -> dict[str, str]:
        """Pop the handles registered since the last drain ({handle: uri})."""
        out = dict(self._pending_announcements)
        self._pending_announcements.clear()
        return out

    # ------------------------------------------------------------------ #
    # Resolution
    # ------------------------------------------------------------------ #

    def resolve_params(self, tool_name: str, params: dict) -> dict:
        """Resolve every RESOLVABLE_URI_PARAMS member of ``params``.

        Returns a fresh dict; raises :class:`UriResolutionError` (typed,
        retryable) on branch 4. Non-string values and params outside the
        allowlist pass through untouched.
        """
        if not params:
            return params
        out = dict(params)
        for name, value in params.items():
            # ADR 0014: nested handle/URI mappings (code_exec layer_refs) —
            # every string VALUE resolves; keys (variable names) untouched.
            if name in NESTED_REF_PARAMS and isinstance(value, dict):
                resolved_refs = self._resolve_ref_mapping(tool_name, name, value)
                if resolved_refs != value:
                    out[name] = resolved_refs
                continue
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

    def _resolve_ref_mapping(
        self, tool_name: str, param_name: str, refs: dict
    ) -> dict:
        """Resolve every string value of a ``layer_refs``-style dict.

        List/tuple values resolve member-wise (the documented list-valued-ref
        shape); non-string members pass through. Raises the same typed
        :class:`UriResolutionError` as flat params on an unknown handle /
        unregistered object-store URI, with the offending key named.
        """
        out = dict(refs)
        changed = False
        for key, ref in refs.items():
            slot = f"{param_name}[{key}]"
            if isinstance(ref, str):
                resolved = self._resolve_one(tool_name, slot, ref)
                if resolved != ref:
                    logger.warning(
                        "uri_registry[%s]: %s.%s resolved %r -> %r",
                        self.session_id,
                        tool_name,
                        slot,
                        ref,
                        resolved,
                    )
                    out[key] = resolved
                    changed = True
            elif isinstance(ref, (list, tuple)):
                new_seq = [
                    self._resolve_one(tool_name, slot, item)
                    if isinstance(item, str)
                    else item
                    for item in ref
                ]
                if list(new_seq) != list(ref):
                    out[key] = type(ref)(new_seq)
                    changed = True
        return out if changed else refs

    def _resolve_one(self, tool_name: str, param_name: str, value: str) -> str:
        v = _normalize_gs(value.strip())

        # Branch 2a — value IS a registered handle (the desired steady state).
        rec = self._records.get(v)
        if rec is not None:
            if rec.uri:
                return rec.uri
            if rec.wms_url:
                # Only the display face is known — no data URI to hand over.
                raise UriResolutionError(param_name, value, self._inventory_text(tool_name))

        # Branch 1 — exact URI known: pass (data URI) or map back (display URL).
        handle = self._uri_to_handle.get(v)
        if handle is not None:
            rec = self._records[handle]
            if v == rec.uri:
                return v
            if v == rec.wms_url:
                if rec.uri:
                    return rec.uri  # display URL where a data URI belongs
                raise UriResolutionError(param_name, value, self._inventory_text(tool_name))
            return v

        # Branch 2b (ADR 0014) — a SHORT layer handle (L<n>, case-insensitive).
        # The desired steady state after the emit rewrite: the LLM passes the
        # short handle it was shown; an UNKNOWN short handle is a typed reject
        # carrying the real inventory so the retry self-corrects.
        m = SHORT_HANDLE_RE.match(v)
        if m is not None:
            short_uri = self._short_to_uri.get(f"L{int(m.group(1))}")
            if short_uri is not None:
                return short_uri
            raise UriResolutionError(param_name, value, self._inventory_text(tool_name))

        # Branch 3-titiler — a TiTiler tile-template DISPLAY URL: the underlying
        # data COG is the unquoted ``url=`` query param (job-0304 /
        # _is_tile_template). Recover it so a display URL handed to a *_uri param
        # resolves to the s3 COG instead of failing open as an unreadable
        # https:// string (the compute_layer_bounds UNKNOWN_LAYER_URI incident).
        # Mirrors pipeline_emitter._layer_identity_key.
        if _is_tile_template(v):
            cog = _titiler_cog_uri(v)
            if cog:
                cog_norm = _normalize_gs(cog)
                # Prefer the registered data face if the COG is known; else use
                # the embedded COG verbatim (it is the real object key).
                handle = self._uri_to_handle.get(cog_norm) or self._uri_to_handle.get(
                    cog
                )
                if handle is not None and self._records[handle].uri:
                    return self._records[handle].uri
                if _is_gs(cog_norm) or cog.startswith("s3://"):
                    return cog
            raise UriResolutionError(param_name, value, self._inventory_text(tool_name))

        # Branch 3-wms — unknown WMS-style URL: recover the layer_id handle.
        if _looks_like_wms(v):
            layer_id = _wms_layer_id(v)
            if layer_id:
                rec = self._records.get(layer_id)
                if rec is not None and rec.uri:
                    return rec.uri
            raise UriResolutionError(param_name, value, self._inventory_text(tool_name))

        # Small-model PLACEHOLDER resolution (2026-07-08): local 8B models emit
        # the producer (fetch_dem) and the consumer (publish_layer) in the SAME
        # iteration, passing stand-ins like 'LayerURI_from_fetch_dem' /
        # '<layer_uri_from_fetch_dem>' / 'fetch_dem_output' as the URI param.
        # Tool calls dispatch SEQUENTIALLY, so by the time the consumer
        # resolves, the producer's real URI is already registered. Conservative
        # by construction: only NON-uri-shaped, non-path strings that name
        # exactly ONE producing tool with exactly ONE distinct registered URI
        # resolve; everything else falls through to the existing honest paths.
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

        # Non-object-store, non-wms strings (plain https COG, local path,
        # opaque token): fail-open — external links / user-pasted sources must
        # never be blocked; the consuming tool's own typed error follows.
        if not _is_object_store(v):
            return value

        # Branch 3 — unknown object-store URI: fuzzy-match the mangle classes.
        substituted = self._fuzzy_match(v)
        if substituted is not None:
            return substituted

        # Branch 4 (ADR 0014) — unknown object-store URI with no plausible
        # match where a LAYER is expected: TYPED REJECT. The session never
        # produced this path, so passing it through can only 404 downstream
        # (or worse, read the wrong object) — raising here with the handle
        # inventory makes URI hallucination structurally impossible and feeds
        # the retry loop a self-correcting message. (This supersedes the
        # post-decommission fail-open: verbatim REGISTERED URIs still pass —
        # branch 1 — so old cases keep working via the dual-accept.)
        logger.warning(
            "uri_registry[%s]: rejecting unregistered object-store uri "
            "%s.%s=%r (ADR 0014)",
            self.session_id,
            tool_name,
            param_name,
            value,
        )
        raise UriResolutionError(param_name, value, self._inventory_text(tool_name))

    def _resolve_placeholder(self, value: str) -> tuple[str, str] | None:
        """Resolve a small-model placeholder string to a producer's layer URI.

        Returns ``(uri, producer_tool_name)`` when ALL of these hold, else
        ``None`` (caller falls through to the existing branches):

        - ``value`` is NOT uri-shaped (no ``<scheme>://`` prefix, no ``/vsi``)
          and NOT a filesystem-path shape (leading ``/`` or ``\\``) - a
          well-formed but unknown URI must keep the existing branch-3/branch-4
          treatment, never a silent substitution;
        - ``value`` textually contains (case-insensitive) the name of exactly
          ONE tool that registered a URI this session ('LayerURI_from_fetch_dem',
          '<layer_uri_from_fetch_dem>', 'fetch_dem_output', 'the layer from
          fetch_dem' all contain 'fetch_dem'); when two matched names nest
          (e.g. 'fetch_dem' inside 'fetch_dem_hires') the longest match wins;
        - that tool registered exactly ONE distinct URI in this session -
          multiple candidates are ambiguous, so we refuse to guess and the
          existing honest error fires downstream.
        """
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
        """Match an unknown object-store URI against the registered inventory.

        Sub-branches (each requires a UNIQUE winner; ambiguity falls through
        to branch 4 — never guess between two plausible layers):

        a. basename stem == a registered handle (layer_id-as-basename mangle);
        b. exact basename match (path mangles like the doubled ``runs/``
           segment), tie-broken by shared-path-segment overlap;
        c. basename-stem common prefix ≥ ``_HASH_PREFIX_MIN`` chars with the
           same extension (hash-tail hallucination), longest prefix wins;
        d. exactly one registered URI in the same parent directory
           (invented-basename mangle, e.g. the timestamp-shaped .fgb).
        """
        gs_uris = [
            rec.uri
            for rec in self._records.values()
            if rec.uri and _is_object_store(rec.uri)
        ]
        if not gs_uris:
            return None
        base = _basename(v)
        stem = _stem(v)
        ext = base.rpartition(".")[2] if "." in base else ""

        # (a) layer_id grafted on as the basename.
        rec = self._records.get(stem)
        if rec is not None and rec.uri:
            return rec.uri

        # (b) exact basename elsewhere — path-segment mangle.
        same_base = [u for u in gs_uris if _basename(u) == base]
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
            return None  # ambiguous — branch 4 lists the handles

        # (c) hash-prefix: tail hallucinated past >= _HASH_PREFIX_MIN chars.
        prefixed = [
            (u, _common_prefix_len(_stem(u), stem))
            for u in gs_uris
            if (_basename(u).rpartition(".")[2] if "." in _basename(u) else "") == ext
        ]
        prefixed = [(u, n) for (u, n) in prefixed if n >= _HASH_PREFIX_MIN]
        if prefixed:
            prefixed.sort(key=lambda t: t[1], reverse=True)
            if len(prefixed) == 1 or prefixed[0][1] > prefixed[1][1]:
                return prefixed[0][0]
            return None  # two equally-plausible hashes — refuse to guess

        # (d) unique same-directory candidate.
        parent = _parent_dir(v)
        same_dir = [u for u in gs_uris if _parent_dir(u) == parent]
        if len(same_dir) == 1:
            return same_dir[0]
        return None

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    def _inventory_text(self, tool_name: str | None = None) -> str:
        """Compact handle inventory for the branch-4 error message.

        F32: when the registry genuinely has no layers, the "run the
        producing tool first" example is tool-aware — a DEM-consuming tool
        (``_DEM_CONSUMING_TOOLS``) is told to ``fetch_dem`` for this AOI
        instead of the generic ``run_model_flood_scenario`` example, which
        was actively misleading (and factually irrelevant) for a terrain
        derivative ask. When the registry DOES have layers (the common F32
        reconnect-repair case — the registry was simply unseeded, not
        genuinely empty) this branch never fires; the handle listing below
        does, now capped at ``_ERROR_HANDLES_CAP`` (10, was 5).
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
                "producing tool first (e.g. run_model_flood_scenario for a "
                "flood-depth raster, fetch_usace_nsi for building assets)."
            )
        def _one(r: UriRecord) -> str:
            # ADR 0014: lead with the short handle when one exists so the
            # retry passes ``L<n>`` (the cheapest, unmangleable form).
            short = self._uri_to_short.get(r.uri) if r.uri else None
            base = f"{r.handle} (from {r.tool_name or 'unknown'})"
            return f"{short} = {base}" if short else base

        lines = ", ".join(_one(r) for r in layer_recs[:_ERROR_HANDLES_CAP])
        return f"Known handles: {lines}."

    def known_handles(self) -> list[str]:
        return [h for h in self._records if not h.startswith("uri:")]


# --------------------------------------------------------------------------- #
# Module-level session store (the _SESSION_ACTIVE_CASE pattern — survives
# reconnects; shared across a session's sibling WebSocket connections)
# --------------------------------------------------------------------------- #

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
    """Test hook — wipe the module-level store."""
    _SESSION_URI_REGISTRIES.clear()


# --------------------------------------------------------------------------- #
# ContextVar observation hook — composer-internal publishes
# --------------------------------------------------------------------------- #

_ACTIVE_REGISTRY: ContextVar[SessionUriRegistry | None] = ContextVar(
    "trid3nt_active_uri_registry", default=None
)


def activate_registry(reg: SessionUriRegistry) -> Token:
    """Bind ``reg`` as the ambient registry for the current dispatch."""
    return _ACTIVE_REGISTRY.set(reg)


def deactivate_registry(token: Token) -> None:
    _ACTIVE_REGISTRY.reset(token)


def lookup_handle_for_uri(
    uri: str, registry: SessionUriRegistry | None = None
) -> str | None:
    """Return the registered LAYER handle whose data/display URI is ``uri``.

    Used by ``publish_layer`` to derive a ``layer_id`` when the model omitted
    it: the (already server-resolved) ``layer_uri`` usually maps back to the
    producing tool's ``layer_id``. Minted ``uri:<basename>`` handles are NOT
    returned (they are fuzzy-match plumbing, not real layer ids). Falls back
    to the ambient ContextVar registry when ``registry`` is not passed; returns
    ``None`` outside an active dispatch (tests / direct programmatic calls).
    """
    reg = registry if registry is not None else _ACTIVE_REGISTRY.get()
    if reg is None or not uri:
        return None
    handle = reg._uri_to_handle.get(_normalize_gs(uri.strip()))
    if handle and not handle.startswith("uri:"):
        return handle
    return None


def ambient_layer_handle_inventory(limit: int = 8) -> list[str]:
    """Most-recent-first LAYER handles of the ambient (dispatch) registry.

    Used by ``publish_layer``'s unknown-handle guard (2026-07-13, OPEN-17
    class): when a small model passes a placeholder like
    ``'LayerURI_from_previous_step'``, the typed error NAMES the handles that
    actually exist in this case so the retry is self-correcting. Minted
    ``uri:<basename>`` records are fuzzy-match plumbing, not real layer ids,
    so they are excluded. Returns ``[]`` outside an active dispatch (tests /
    direct programmatic calls) or when nothing has been produced yet.
    """
    reg = _ACTIVE_REGISTRY.get()
    if reg is None:
        return []
    recs = [
        r
        for r in reg._records.values()
        if r.uri and not r.handle.startswith("uri:")
    ]
    recs.sort(key=lambda r: r.seq, reverse=True)
    return [r.handle for r in recs[: max(0, limit)]]


def observe_published_layer(
    layer_id: str,
    gcs_uri: str | None = None,
    wms_url: str | None = None,
) -> None:
    """Record a published layer's BOTH faces (gs:// COG + WMS display URL).

    Called from inside ``publish_layer`` (after URI validation/correction)
    so composer-internal publishes — whose envelopes only carry the WMS URL —
    still register the consumable data URI. No-op outside an active dispatch
    (e.g. direct programmatic tool calls in tests).
    """
    reg = _ACTIVE_REGISTRY.get()
    if reg is None:
        return
    try:
        reg.record(layer_id, uri=gcs_uri, wms_url=wms_url, tool_name="publish_layer")
    except Exception:  # noqa: BLE001 — observation must never break the tool
        logger.exception("observe_published_layer failed layer_id=%s", layer_id)


# Re-exported for completeness — some tests assert the regex contract of
# hash-shaped cache stems (32-hex). Not used in resolution (prefix matching
# is shape-agnostic) but documents the cache-key convention.
HASH_STEM_RE = re.compile(r"^[0-9a-f]{32}$")
