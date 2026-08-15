"""Pending user-interaction registries for the WebSocket server.

Three
independent request/response gates, all sharing the same shape: a module-level
dict keyed by an unguessable-ULID ``request_id`` tagged with the owning
``session_id``, so a reply arriving on a sibling WebSocket connection of the
same session still resolves the paused turn, and a cross-session reply is
refused. ``register`` / ``pop`` / ``resolve`` are pure registry operations;
the catalog-offer section adds a TTL prune + bounded cap. Moved verbatim
(behavior-preserving); ``_core`` re-imports these names by name so bare-global
references and monkeypatch targets on ``trid3nt_server.server.<name>`` resolve
exactly as the monolith's did. The ``logging.getLogger`` name matches ``_core``
so log records are indistinguishable across the split.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from trid3nt_contracts import new_ulid, now_utc

from .config import _catalog_offer_ttl_s

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

    from trid3nt_contracts.secrets import CredentialProvidedEnvelopePayload

    from ._core import SessionState

logger = logging.getLogger("trid3nt_server.server")


# --------------------------------------------------------------------------- #
# Pending tool-choice registry: keyed by the unguessable ULID
# request_id + owning session_id, so a reply arriving on a sibling WebSocket
# connection of the same session still resolves the paused turn.
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Pending tool-choice registry: module-level, keyed by the
# unguessable ULID request_id + owning session_id, so a reply arriving on a
# sibling WebSocket connection of the same session still resolves the paused
# turn.
# --------------------------------------------------------------------------- #

_PENDING_TOOL_CHOICES: dict[str, tuple[str, "asyncio.Future"]] = {}


def _register_pending_tool_choice(
    session_id: str, request_id: str, fut: "asyncio.Future"
) -> None:
    _PENDING_TOOL_CHOICES[request_id] = (session_id, fut)


def _pop_pending_tool_choice(request_id: str) -> None:
    _PENDING_TOOL_CHOICES.pop(request_id, None)


def _resolve_pending_tool_choice(session_id: str, payload: Any) -> bool:
    """Complete the pending tool-candidates gate for ``payload['request_id']``.

    The payload is a LOOSE dict on purpose -- the contracts lane declares the
    ``tool-choice`` model; until integration we parse defensively. Returns
    True when a live future was resolved.
    """
    if not isinstance(payload, dict):
        return False
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return False
    entry = _PENDING_TOOL_CHOICES.get(request_id)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "tool-choice request_id=%s owned by session=%s but resolved-by=%s; "
            "ignoring",
            request_id,
            owner_session,
            session_id,
        )
        return False
    if fut.done():
        return False
    fut.set_result(dict(payload))
    return True


# ---------------------------------------------------------------------------
# Mode 2 offer-to-add -- pending catalog-offer registry (FR-DS-* Mode 2).
#
# The ``mode2-candidate`` emission is FIRE-AND-FORGET: it never blocks the turn
# (unlike the tool-choice gate, so there is no future to await). Instead we
# stash the candidate keyed by its ``candidate_id`` -- which the plugin card
# echoes back as the ``catalog-addition-response.request_id`` -- so a later
# positive reply can draft the full catalog entry from the original candidate.
#
# BOUNDED, like every card: offers expire after a TTL and the registry is
# capped, so an offer the user never answers cannot leak (proceeds, never
# hangs). Session-scoped + unguessable-ULID keyed, mirroring the tool-choice
# registry so a reply on a sibling connection of the same session still
# resolves. Overlay write + probe run OFF the loop (asyncio.to_thread).
# ---------------------------------------------------------------------------

#: request_id -> (owner_session_id, candidate wire-dict, monotonic_expiry).
_PENDING_CATALOG_OFFERS: dict[str, tuple[str, dict, float]] = {}

#: Cap on outstanding offers; the oldest is dropped when the bound is hit.
_CATALOG_OFFER_MAX = 64


def _prune_catalog_offers(now: float | None = None) -> None:
    """Drop expired pending offers (called on every register / pop)."""
    now = time.monotonic() if now is None else now
    expired = [
        rid for rid, (_s, _c, exp) in _PENDING_CATALOG_OFFERS.items() if exp <= now
    ]
    for rid in expired:
        _PENDING_CATALOG_OFFERS.pop(rid, None)


def _register_pending_catalog_offer(
    session_id: str, request_id: str, candidate: dict
) -> None:
    """Stash a mode2 candidate so a later positive reply can draft its entry."""
    _prune_catalog_offers()
    # Insertion-ordered dict -> the first key is the oldest offer.
    while len(_PENDING_CATALOG_OFFERS) >= _CATALOG_OFFER_MAX:
        oldest = next(iter(_PENDING_CATALOG_OFFERS), None)
        if oldest is None:
            break
        _PENDING_CATALOG_OFFERS.pop(oldest, None)
    _PENDING_CATALOG_OFFERS[request_id] = (
        session_id,
        dict(candidate),
        time.monotonic() + _catalog_offer_ttl_s(),
    )


def _pop_pending_catalog_offer(session_id: str, request_id: str) -> dict | None:
    """Remove + return the candidate for ``request_id`` (owner-checked).

    Returns ``None`` when the offer is unknown / expired, or when a DIFFERENT
    session claims it (refused loudly -- mirrors ``_resolve_pending_tool_choice``).
    A None here is NOT fatal: a self-contained ``edited_catalog_entry`` on the
    reply can still be completed without the original candidate.
    """
    _prune_catalog_offers()
    entry = _PENDING_CATALOG_OFFERS.get(request_id)
    if entry is None:
        return None
    owner, candidate, _exp = entry
    if owner != session_id:
        logger.warning(
            "catalog-addition-response request_id=%s owned by session=%s but "
            "resolved-by=%s; ignoring",
            request_id,
            owner,
            session_id,
        )
        return None
    _PENDING_CATALOG_OFFERS.pop(request_id, None)
    return candidate


def _slug(text: str | None) -> str:
    """Lowercase kebab slug for a derived catalog id (``weather.gov`` -> ``weather-gov``)."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "source"


def _probe_catalog_endpoint_sync(url: str) -> "ProbeFindings":
    """Cheap best-effort conformity probe for a Mode 2 candidate URL.

    Fills the ``ProbeFindings`` axes we can observe from a single bounded HEAD
    (with a ranged-GET fallback for servers that reject HEAD): content type,
    last-modified, range support. DEGRADES HONESTLY -- any failure returns an
    empty ``ProbeFindings`` (all None) rather than fabricating a finding. Runs
    OFF the event loop (called via ``asyncio.to_thread``).
    """
    from trid3nt_contracts.ws import ProbeFindings

    try:
        import requests

        resp = requests.head(url, timeout=5.0, allow_redirects=True)
        if resp.status_code >= 400 or not (resp.headers or {}):
            resp = requests.get(
                url, timeout=5.0, stream=True, headers={"Range": "bytes=0-0"}
            )
        headers = resp.headers or {}
        ct = headers.get("Content-Type") or headers.get("content-type")
        lm = headers.get("Last-Modified") or headers.get("last-modified")
        ar = headers.get("Accept-Ranges") or headers.get("accept-ranges")
        supports_range = (
            isinstance(ar, str) and "bytes" in ar.lower()
        ) or resp.status_code == 206
        return ProbeFindings(
            content_type=(
                ct.split(";")[0].strip() if isinstance(ct, str) and ct else None
            ),
            last_modified_header=lm if isinstance(lm, str) and lm else None,
            supports_range_requests=supports_range or None,
        )
    except Exception:  # noqa: BLE001 -- honest degrade, never fabricate
        logger.info(
            "catalog probe failed url=%s -- degrading to empty ProbeFindings",
            url,
            exc_info=True,
        )
        return ProbeFindings()


async def _probe_catalog_endpoint(url: str | None) -> "ProbeFindings | None":
    """Bounded async wrapper for ``_probe_catalog_endpoint_sync`` (never raises)."""
    from trid3nt_contracts.ws import ProbeFindings

    if not isinstance(url, str) or not url:
        return None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_probe_catalog_endpoint_sync, url), timeout=8.0
        )
    except Exception:  # noqa: BLE001 -- probe is best-effort
        return ProbeFindings()


def _complete_catalog_entry(
    base: Any, candidate: dict | None, findings: "ProbeFindings | None"
) -> Any:
    """Draft + complete a full ``CatalogEntry`` for the user-overlay catalog.

    Sources, in precedence order: the user's ``edited_catalog_entry`` (a
    ``SuggestedCatalogEntry``), then the originating mode2 ``candidate`` dict,
    then the conformity ``findings``, then honest defaults. Returns a validated
    ``CatalogEntry`` (status ``"active"`` -- in the single-user local build the
    user's acceptance IS the curator approval), or ``None`` when no endpoint
    URL is derivable (the one field we refuse to fabricate).
    """
    from trid3nt_contracts.catalog import CatalogEntry
    from urllib.parse import urlparse

    cand = candidate or {}

    def _b(attr: str) -> Any:
        return getattr(base, attr, None) if base is not None else None

    urls: list[str] = []
    if base is not None and _b("urls"):
        urls = [u for u in _b("urls") if isinstance(u, str) and u]
    if not urls and isinstance(cand.get("url"), str) and cand["url"]:
        urls = [cand["url"]]
    if not urls:
        return None  # no endpoint -> cannot honestly draft an entry.
    url = urls[0]
    host = (urlparse(url).hostname or "").lower()

    entry_id = _b("id") or f"user-{_slug(host or 'source')}-{new_ulid()[-8:].lower()}"
    name = (
        _b("name")
        or (cand.get("title") if isinstance(cand.get("title"), str) else None)
        or host
        or entry_id
    )
    description = (
        _b("description")
        or f"User-added source via Mode 2 catalog addition ({host or url})."
    )
    access_tier = (
        _b("access_tier")
        or (findings.access_tier_inferred if findings is not None else None)
        or 3
    )
    # credential_tier: draft ONLY as tier 1 (key-free). Tier >= 2 requires an
    # api_key_secret_ref we do not have at draft time; downgrading keeps the
    # entry valid against the CatalogEntry cross-field rule (honest, not a shim).
    ttl_class = _b("ttl_class") or "semi-static-7d"
    source_class = (
        _b("source_class")
        or (
            cand.get("suggested_tool_kind")
            if isinstance(cand.get("suggested_tool_kind"), str)
            else None
        )
        or "user_added"
    )
    license_txt = (
        _b("license_claim")
        or (findings.license_observed if findings is not None else None)
        or "Unknown (user-proposed, unverified)"
    )
    how_to_use = (
        _b("how_to_use")
        or (
            f"User-proposed endpoint {url}. Fetch via web_fetch or the generic "
            "OGC/HTTP adapters; verify the response shape before relying on it."
        )
    )
    citation = (
        f"{name} -- {host or url} (user-proposed via Mode 2, added "
        f"{now_utc().date().isoformat()})"
    )
    try:
        return CatalogEntry(
            id=entry_id,
            name=name,
            description=description,
            urls=urls,
            access_tier=access_tier,
            credential_tier=1,
            ttl_class=ttl_class,
            source_class=source_class,
            license=license_txt,
            citation=citation,
            vintage=None,
            last_verified=now_utc().isoformat(),
            status="active",
            how_to_use=how_to_use,
            api_key_secret_ref=None,
        )
    except Exception:  # noqa: BLE001 -- surface the honest failure, no shim
        logger.warning(
            "catalog completion: could not build a valid CatalogEntry id=%r",
            entry_id,
            exc_info=True,
        )
        return None


async def _handle_catalog_addition_response(
    websocket: ServerConnection,
    state: SessionState,
    car: "CatalogAdditionResponsePayload",
) -> None:
    """Mode 2 offer-to-add completion (the server half of the loop).

    On ACCEPT: draft + complete the entry (edited entry / candidate / probe),
    APPEND it to the user-overlay catalog, and reset the catalog cache so
    ``search_data_catalog`` finds it on the very next load. On REJECT / cancel:
    just resolve (drop) the pending offer. Best-effort -- never raises into the
    message loop; a probe / append fault degrades honestly and logs.
    """
    candidate = _pop_pending_catalog_offer(state.session_id, car.request_id)

    if car.cancelled or car.decision != "accept":
        logger.info(
            "catalog-addition-response resolved session=%s request_id=%s "
            "decision=%s cancelled=%s (offer dropped)",
            state.session_id,
            car.request_id,
            car.decision,
            car.cancelled,
        )
        return

    base = car.edited_catalog_entry
    if base is None and candidate is None:
        logger.warning(
            "catalog-addition-response accept with NO pending offer and NO "
            "edited entry session=%s request_id=%s -- cannot draft; ignored",
            state.session_id,
            car.request_id,
        )
        return

    url_for_probe: str | None = None
    if base is not None and base.urls:
        url_for_probe = base.urls[0]
    elif candidate and isinstance(candidate.get("url"), str):
        url_for_probe = candidate["url"]
    findings = await _probe_catalog_endpoint(url_for_probe)

    entry = _complete_catalog_entry(base, candidate, findings)
    if entry is None:
        logger.warning(
            "catalog-addition-response accept could not complete a valid entry "
            "session=%s request_id=%s",
            state.session_id,
            car.request_id,
        )
        return

    try:
        from ..agent.tools.search.catalog_common import append_user_catalog_entry

        await asyncio.to_thread(append_user_catalog_entry, entry)
    except Exception:  # noqa: BLE001 -- append fault must not break the loop
        logger.exception(
            "catalog-addition-response overlay append failed session=%s id=%s",
            state.session_id,
            entry.id,
        )
        return

    logger.info(
        "catalog-addition-response ACCEPTED session=%s request_id=%s appended "
        "catalog id=%s url=%s",
        state.session_id,
        car.request_id,
        entry.id,
        entry.urls[0],
    )


# --------------------------------------------------------------------------- #
# Session-scoped pending-CREDENTIAL registry
# --------------------------------------------------------------------------- #
#
# Mirrors ``_PENDING_CONFIRMATIONS`` (the payload-warning / code-exec / solver
# gate registry) but for the credential-request flow: when a keyed tool
# dispatch hits a missing/invalid credential the dispatch coroutine pauses on
# a future keyed by the credential ``request_id``, having emitted a
# ``credential-request`` envelope. The inbound ``credential-provided``
# handler (which may arrive on a different WebSocket connection of the same
# session) resolves the future, and the paused dispatch retries the tool
# (which now reads the freshly-pushed session-cache key). Tagged with the
# owning session_id so a cross-session credential-provided is refused.
_PENDING_CREDENTIALS: dict[str, tuple[str, asyncio.Future]] = {}


def _register_pending_credential(
    session_id: str, request_id: str, fut: "asyncio.Future"
) -> None:
    _PENDING_CREDENTIALS[request_id] = (session_id, fut)


def _pop_pending_credential(request_id: str) -> None:
    _PENDING_CREDENTIALS.pop(request_id, None)


def _resolve_pending_credential(
    session_id: str, provided: "CredentialProvidedEnvelopePayload"
) -> bool:
    """Complete the pending credential future for ``provided.request_id``.

    Returns True when a live future was resolved. False when the request_id is
    unknown/already-resolved, or when the answering session is not the owner
    (refused loudly -- the request_id is an unguessable ULID, but the string
    compare is cheap defense-in-depth, matching ``_resolve_pending_confirmation``).
    """
    entry = _PENDING_CREDENTIALS.get(provided.request_id)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "credential-provided REFUSED: session=%s is not the owner "
            "(owner=%s) for request_id=%s",
            session_id,
            owner_session,
            provided.request_id,
        )
        return False
    if fut.done():
        _PENDING_CREDENTIALS.pop(provided.request_id, None)
        return False
    fut.set_result(provided)
    _PENDING_CREDENTIALS.pop(provided.request_id, None)
    return True
