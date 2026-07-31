"""Typed missing-credential error family (local-only).

The file-vault and its CRUD handlers were removed when QgsAuthManager became the
credential home: the plugin brokers key VALUES over the ``secret-add`` WS seam
into the in-memory session cache (``credentials.resolver``), and env vars are the
headless / dev floor. Nothing writes or reads a persisted secret file anymore.

What survives here is the typed-error family a keyed fetcher / the credential
pipeline raises when a key is missing or unusable, so callers can classify the
failure honestly (route to a credential-request card, never a crash, never a
silent empty value).
"""

from __future__ import annotations


class SecretError(RuntimeError):
    """Base for credential-resolution failures."""


class SecretRevokedError(SecretError):
    """Raised when a credential the caller holds has been revoked.

    Tier-2 fetchers catch this and surface a recoverable A.6 error code (the
    user can re-enter a fresh key), which routes to the credential-request card.
    """


class SecretNotFoundError(SecretError):
    """Raised when no credential value can be resolved for a keyed tool.

    The typed missing-secret path: Tier-2 fetchers treat it as "no key
    available", which routes to the credential-request card so the user enters
    the key -- honest re-prompt, never a crash, never a silent empty value.
    """


__all__ = [
    "SecretError",
    "SecretNotFoundError",
    "SecretRevokedError",
]
