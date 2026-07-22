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
  4. unknown with no plausible match    → storage URIs FAIL OPEN (pass
     through; the consuming tool's own typed fetch error surfaces the
     problem), while display-only faces (WMS / tile-template URLs with no
     recoverable data URI) raise a typed retryable error
     (``URI_HANDLE_UNRESOLVED``) that TELLS the model which handles exist,
     so it self-corrects instead of inventing again.

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

logger = logging.getLogger("grace2_agent.uri_registry")

__all__ = [
    "RESOLVABLE_URI_PARAMS",
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
            f"produced — do NOT construct gs:// paths. Pass a layer handle "
            f"(layer_id) from a prior tool result instead. {inventory}"
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
    """Bucket + path segments of a gs:// URI (for overlap scoring)."""
    body = uri[len("gs://") :] if _is_gs(uri) else uri
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
        if _is_gs(norm):
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
        """Drop every registered handle/URI/pending-announcement (F32)."""
        self._records.clear()
        self._uri_to_handle.clear()
        self._pending_announcements.clear()

    def replace_from_layers(self, layers: Any) -> None:
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
        """
        self.clear()
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

        # Non-gs, non-wms strings (plain https COG, local path, opaque token):
        # fail-open. The mangle classes are all gs:// or WMS shaped.
        if not _is_gs(v):
            return value

        # Branch 3 — unknown gs:// URI: fuzzy-match the mangle classes.
        substituted = self._fuzzy_match(v)
        if substituted is not None:
            return substituted

        # Branch 4 — unknown + no match: FAIL OPEN. The legacy-cloud-era
        # managed-bucket strict-reject died with the cloud decommission;
        # nothing local mints gs:// paths, so a stale or invented storage URI
        # now surfaces as the consuming tool's own typed fetch error instead
        # of a registry raise.
        logger.info(
            "uri_registry[%s]: passing through unknown uri %s.%s=%r",
            self.session_id,
            tool_name,
            param_name,
            value,
        )
        return value

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
        """Match an unknown gs:// URI against the registered inventory.

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
        gs_uris = [rec.uri for rec in self._records.values() if rec.uri and _is_gs(rec.uri)]
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
        lines = ", ".join(
            f"{r.handle} (from {r.tool_name or 'unknown'})"
            for r in layer_recs[:_ERROR_HANDLES_CAP]
        )
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
    "grace2_active_uri_registry", default=None
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
