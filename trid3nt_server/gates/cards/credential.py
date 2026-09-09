"""Credential-request confirm-card builder (pure payload construction)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import ValidationError

from trid3nt_contracts.secrets import CredentialRequestEnvelopePayload

if TYPE_CHECKING:
    from trid3nt_server.credentials.credential_registry import CredentialProvider

logger = logging.getLogger("trid3nt_server.gates.cards.credential")


def _build_credential_request_payload(
    *,
    request_id: str,
    provider: CredentialProvider,
    tool_name: str,
    message: str,
) -> "CredentialRequestEnvelopePayload | None":
    """Build a validated ``CredentialRequestEnvelopePayload``.

    ``None`` for an unregistered ``provider_id`` rather than a fabricated
    fallback scope, so the caller surfaces the original typed error."""
    try:
        return CredentialRequestEnvelopePayload(
            request_id=request_id,
            # The card must be scoped to the REAL provider: this is the scope
            # the resulting ``secret-add`` writes under and the scope the
            # resolver re-reads on retry, so a fallback scope would save the key
            # where the retry cannot find it.
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
