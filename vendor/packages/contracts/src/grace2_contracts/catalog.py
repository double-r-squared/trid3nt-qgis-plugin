"""CatalogEntry — the Mode 1 curated data-source catalog entry (FR-PHC-2 + §F.1.2).

One entry per **vetted public data source** in the curated catalog (sprint-08
substrate). The catalog (`public_data_source_catalog.yaml`, and its MongoDB
collection successor `catalog_entries` per Decision F) is the single source of
truth for vetted endpoints under §F.1.2 Mode 1 (catalog-mediated). Every entry
is research-driven and labeled at curator time with identification, endpoint
URLs, access/credential/TTL tiering, license/citation/vintage provenance, a
status lifecycle (`active` / `deprecated` / `user_proposed_pending_curator_review`),
and a multi-line "how_to_use" string carrying invocation examples + parameter
constraints + known quirks (e.g., "WorldPop returns HTTP 200 not 206 for Range
requests — use region-download tier; specify country in params.iso3").

This labeling is the difference between a sterile URL list and an actionable
catalog (§F.1.2 Mode 1 prose). The atomic tools `catalog_search` and
`catalog_fetch` consume this shape; `engine` curates the content; `schema`
owns the entry shape.

The fields rewrite the v0.1 FR-PHC-2 stub (which carried `id` / `title` /
`agency` / `topic` / `coverage` / `format` / `access` / `style_preset` /
`license` / `description` / `last_verified`) to land the §F.1.2 binding contract.
The earlier discovery-style fields (`agency` / `topic` / `coverage` /
`format` / `style_preset`) were FR-PHC-2 v0.1 stub fields; they are NOT
preserved (pre-MVP scope — no migration shims per AGENTS.md). The new shape
is the authoritative Mode 1 substrate.

Cross-field rule (`_validate_credential_tier_consistency`):
- ``credential_tier == 1`` (key-free) ⇒ no ``api_key_secret_ref`` (must be None).
- ``credential_tier >= 2`` (key-required / paid) ⇒ ``api_key_secret_ref`` is required
  (non-empty string — typically the Secret Manager resource path).

Invariants this module is responsible for:
- **Invariant 7 (Claims carry provenance).** ``license`` + ``citation`` +
  ``vintage`` + ``last_verified`` are required structured fields so downstream
  attribution is generated from data, not free text.
- **Invariant 9 (No cost theater).** No ``cost_usd`` / ``estimated_cost`` /
  similar fields. ``credential_tier == 3`` flags a paid source but does NOT
  surface dollar amounts; the user-consent flow is out of scope for v0.1.
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


#: Access-pattern tier per §F.1.1 (orthogonal to credential tier).
#: 1 = STAC + COG byte-window; 2 = OGC service (WMS/WMTS/WCS/WFS);
#: 3 = direct HTTPS + Range; 4 = region download + local clip.
AccessTier = Literal[1, 2, 3, 4]

#: Credential tier per §F.1.
#: 1 = key-free public; 2 = key-required, free; 3 = paid commercial.
CredentialTier = Literal[1, 2, 3]

#: TTL class per FR-DC-2. Mirrors ``tool_registry.TTLClass`` verbatim so a
#: catalog-driven fetch shares the same cache-class vocabulary as a hardcoded
#: atomic tool (FR-DC-1 bucket-layout discipline).
TTLClass = Literal["static-30d", "semi-static-7d", "dynamic-1h", "live-no-cache"]

#: Entry-status lifecycle per §F.1.2 Mode 1 + Mode 2.
#: - ``active``: curator-vetted; `catalog_search` returns this entry.
#: - ``deprecated``: curator-removed; retained for audit / historical run
#:   provenance lookups but excluded from active search results.
#: - ``user_proposed_pending_curator_review``: a Mode 2 user-accepted
#:   `offer-catalog-addition` entry; included in `catalog_search` results but
#:   surfaced as provisional until a curator flips it to ``active``.
EntryStatus = Literal[
    "active",
    "deprecated",
    "user_proposed_pending_curator_review",
]


class CatalogEntry(GraceModel):
    """A single curated public data-source catalog entry (FR-PHC-2 + §F.1.2).

    Fields (per §F.1.2 Mode 1):

    - ``id`` — stable identifier (e.g. ``"usgs-3dep-dem-1m"``,
      ``"worldpop-1km-aggregated"``).
    - ``name`` — human-readable label.
    - ``description`` — brief description of what the layer / dataset
      represents.
    - ``urls`` — endpoint URLs; the first is primary, subsequent entries are
      alternative mirrors. ``min_length=1``.
    - ``access_tier`` — §F.1.1 access-pattern tier (1/2/3/4).
    - ``credential_tier`` — §F.1 credential tier (1/2/3).
    - ``ttl_class`` — FR-DC-2 cache class.
    - ``source_class`` — FR-DC-1 bucket-prefix identifier (``"dem"``,
      ``"landcover"``, ``"flood_zone"``, …). Free-form string per FR-DC-1.
    - ``license`` — license text or URL (e.g. ``"Public Domain (US Federal)"``,
      ``"CC-BY-4.0"``). Required structured field per Invariant 7.
    - ``citation`` — formal citation string. Required.
    - ``vintage`` — data vintage descriptor (e.g. ``"2020"``, ``"2024-Q3"``,
      ``"R2020A"``). Optional — some live-feed sources do not have a stable
      vintage.
    - ``last_verified`` — UTC datetime when the entry was last confirmed
      working by a curator (or by the Mode 2 conformity probe).
    - ``status`` — entry lifecycle per ``EntryStatus``.
    - ``how_to_use`` — multi-line invocation examples + parameter constraints +
      known quirks. This is the actionable-catalog payload (§F.1.2 Mode 1).
    - ``api_key_secret_ref`` — Secret Manager resource path for the API key
      when ``credential_tier >= 2``. Must be ``None`` when ``credential_tier == 1``.
    """

    schema_version: Literal["v1"] = "v1"

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str

    # Endpoints: primary URL + zero-or-more alternative mirrors.
    urls: list[str] = Field(min_length=1)

    # Tier classification (the three orthogonal axes).
    access_tier: AccessTier
    credential_tier: CredentialTier
    ttl_class: TTLClass

    # Bucket-prefix discipline (FR-DC-1).
    source_class: str = Field(min_length=1)

    # Provenance (Invariant 7).
    license: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    vintage: str | None = None
    last_verified: UTCDatetime

    # Lifecycle (Mode 1 + Mode 2).
    status: EntryStatus

    # Actionable-catalog payload (§F.1.2 Mode 1).
    how_to_use: str = Field(min_length=1)

    # Native ground resolution in metres for raster Tier-2 entries (phase-2
    # resolution lever). Optional + additive: when set, the generic
    # ``catalog_fetch`` Tier-2 dispatch targets this cell size for the
    # extent-aware raster grid instead of a fixed pixel count; left ``None``
    # for vector entries and rasters without a curated native resolution
    # (those fall back to the adapter's bounded 30 m default).
    native_resolution_m: float | None = None

    # Conditional credential field (see cross-field rule).
    api_key_secret_ref: str | None = None

    @model_validator(mode="after")
    def _validate_credential_tier_consistency(self) -> CatalogEntry:
        """Enforce the §F.1 credential-tier consistency rule.

        Tier 1 (key-free) MUST NOT carry a Secret Manager reference; Tier 2/3
        (key-required / paid) MUST carry one so the catalog-driven fetcher can
        resolve the credential at call time.
        """
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
