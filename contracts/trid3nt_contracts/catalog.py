"""``CatalogEntry`` - one vetted public data source in the curated catalog.

The catalog is the source of truth for vetted endpoints. Every entry is
labelled at curation time with endpoints, tiering, provenance and a
``how_to_use`` payload carrying invocation constraints and known quirks - the
difference between a sterile URL list and an actionable catalog.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import GraceModel, UTCDatetime

__all__ = [
    "AccessTier",
    "CredentialTier",
    "TTLClass",
    "EntryStatus",
    "CatalogEntry",
]


#: Access-pattern tier, orthogonal to the credential tier.
#: 1 = STAC + COG byte-window; 2 = OGC service (WMS/WMTS/WCS/WFS);
#: 3 = direct HTTPS + Range; 4 = region download + local clip.
AccessTier = Literal[1, 2, 3, 4]

#: Credential tier. 1 = key-free public; 2 = key-required, free;
#: 3 = paid commercial. No dollar amount is ever carried for tier 3.
CredentialTier = Literal[1, 2, 3]

#: TTL class. Mirrors ``tool_registry.TTLClass`` VERBATIM so a catalog-driven
#: fetch shares one cache-class vocabulary with a coded atomic tool.
TTLClass = Literal["static-30d", "semi-static-7d", "dynamic-1h", "live-no-cache"]

#: Entry-status lifecycle.
#: - ``active``: curator-vetted; a search returns this entry.
#: - ``deprecated``: curator-removed. Retained for audit and historical run
#:   provenance, excluded from active search results.
#: - ``user_proposed_pending_curator_review``: a user-accepted addition.
#:   Returned by search but surfaced as provisional until a curator flips it.
EntryStatus = Literal[
    "active",
    "deprecated",
    "user_proposed_pending_curator_review",
]


class CatalogEntry(GraceModel):
    """One curated public data-source catalog entry.
    Provenance is structured and required - ``license``, ``citation`` and
    ``last_verified`` - so attribution is generated from data, never free text."""

    schema_version: Literal["v1"] = "v1"

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str

    # Endpoints: primary URL + zero-or-more alternative mirrors.
    urls: list[str] = Field(min_length=1)

    # Tier classification: the three orthogonal axes.
    access_tier: AccessTier
    credential_tier: CredentialTier
    ttl_class: TTLClass

    # Bucket-prefix discipline.
    source_class: str = Field(min_length=1)

    # Provenance. Structured and required, except a vintage some live feeds
    # do not have.
    license: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    vintage: str | None = None
    last_verified: UTCDatetime

    # Lifecycle.
    status: EntryStatus

    # The actionable payload: invocation examples, parameter constraints and
    # known quirks, in prose, for whoever calls the endpoint.
    how_to_use: str = Field(min_length=1)

    # Native ground resolution in metres, for raster access-tier-2 entries.
    # When set, a catalog-driven fetch targets this cell size instead of a
    # fixed pixel count. ``None`` for vector entries and for rasters with no
    # curated native resolution, which fall back to a bounded default.
    native_resolution_m: float | None = None

    # Required for credential tier >= 2, forbidden at tier 1.
    api_key_secret_ref: str | None = None

    @model_validator(mode="after")
    def _validate_credential_tier_consistency(self) -> CatalogEntry:
        """Tier 1 (key-free) MUST NOT carry a secret reference; tier 2 and 3
        MUST carry one, so a catalog-driven fetch can resolve the credential at
        call time rather than discovering its absence mid-request."""
        if self.credential_tier == 1:
            if self.api_key_secret_ref is not None:
                raise ValueError(
                    "credential_tier=1 (key-free public) must not declare "
                    "api_key_secret_ref; the field belongs to credential_tier >= 2 entries."
                )
        else:
            if not self.api_key_secret_ref:
                raise ValueError(
                    f"credential_tier={self.credential_tier} requires a non-empty "
                    "api_key_secret_ref (Secret Manager resource path) so the "
                    "catalog-driven fetcher can resolve the credential at call time."
                )
        return self
