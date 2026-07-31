"""Credential-request confirm-card builder (pure payload construction)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import ValidationError

from trid3nt_contracts.secrets import CredentialRequestEnvelopePayload

if TYPE_CHECKING:
    from ....credentials.credential_registry import CredentialProvider

logger = logging.getLogger("trid3nt_server.agent.gates.cards.credential")


def _build_credential_request_payload(
    *,
    request_id: str,
    provider: CredentialProvider,
    tool_name: str,
    message: str,
) -> "CredentialRequestEnvelopePayload | None":
    """Build a validated ``CredentialRequestEnvelopePayload``.

    Every registered provider's ``provider_id`` is a member of the closed
    ``ProviderID`` Literal, so the payload is scoped to the REAL provider --
    the same scope the resulting ``secret-add`` writes under and the same
    scope the resolver's session cache re-reads on retry, so the round-trip
    closes (no fallback scope ever mis-scopes the saved key).

    If a ``provider.provider_id`` is somehow NOT a valid Literal member (an
    unregistered provider slipped into the registry), we DO NOT fabricate a
    fallback scope -- emitting under the wrong provider would save the key where
    the retry can't re-resolve it. We log and return ``None`` so the caller
    abandons the prompt and lets the original typed error surface (the agent
    narrates honestly that it cannot request a key for an unknown provider).
    """
    try:
        return CredentialRequestEnvelopePayload(
            request_id=request_id,
            provider_id=provider.provider_id,  # type: ignore[arg-type]
            provider_label=provider.label,
            signup_url=provider.signup_url,
            secret_key_name=provider.secret_key_name,
            message=message,
            tool_name=tool_name,
        )
    except ValidationError:
        logger.error(
            "credential-request: provider_id=%r (%r) is not a member of the "
            "ProviderID Literal — cannot scope a secret-add that re-resolves "
            "on retry; abandoning prompt and surfacing the original error",
            provider.provider_id,
            provider.label,
        )
        return None
