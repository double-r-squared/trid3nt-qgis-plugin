"""Florida demo species reference — common name → GBIF taxonKey + scientific name.

Small curated lookup of charismatic Florida species used in Case 1 (flood +
habitat overlay) demos and other ecological-overlay scenarios. Each entry pins
a verified GBIF ``taxonKey`` (``usageKey`` from ``species/match``) at the
species level — NOT at the subspecies level — so occurrence searches return
records actually catalogued in GBIF (most observers tag at species level).

Why this matters (the OQ-0087-PANTHER-TAXON-KEY lesson):
    Florida panther is *Puma concolor coryi* (a subspecies). The
    subspecies-level GBIF key (``7193927`` = ``Puma concolor concolor``) has
    ~310 records globally, NONE in Florida. The species-level key
    (``2435099`` = ``Puma concolor``) has ~250 records in Big Cypress alone.
    The lesson: prefer species-level keys for demo defaults unless the
    subspecies is what's really intended.

Every key here was verified live against ``https://api.gbif.org/v1/species/<key>``
on 2026-06-08:

    2435099  → Puma concolor                  (SPECIES)
    2441370  → Alligator mississippiensis     (SPECIES)
    2480803  → Platalea ajaja                 (SPECIES)
    2435296  → Trichechus manatus             (SPECIES)

(The audit-md kickoff originally suggested 2436873 / 2481008 / 2440777 for the
three non-panther species; those resolve to unrelated taxa or no record. See
OQ-0117-DEMO-SPECIES-KEYS for the verification trail.)

Used by demo prompts and Case 1 scenarios; downstream tools (e.g.
``fetch_gbif_occurrences``) consume the ``gbif_taxon_key`` directly.
"""

from __future__ import annotations

from typing import TypedDict

__all__ = ["FLORIDA_DEMO_SPECIES", "DemoSpeciesEntry"]


class DemoSpeciesEntry(TypedDict):
    """Schema of each ``FLORIDA_DEMO_SPECIES`` entry."""

    gbif_taxon_key: int
    scientific_name: str
    common: str


# Verified taxonKeys (live GBIF lookup, 2026-06-08). Species-level — see module
# docstring for the OQ-0087-PANTHER-TAXON-KEY rationale.
FLORIDA_DEMO_SPECIES: dict[str, DemoSpeciesEntry] = {
    "florida_panther": {
        "gbif_taxon_key": 2435099,
        "scientific_name": "Puma concolor",
        "common": "Florida panther",
    },
    "american_alligator": {
        "gbif_taxon_key": 2441370,
        "scientific_name": "Alligator mississippiensis",
        "common": "American alligator",
    },
    "roseate_spoonbill": {
        "gbif_taxon_key": 2480803,
        "scientific_name": "Platalea ajaja",
        "common": "Roseate spoonbill",
    },
    "manatee": {
        "gbif_taxon_key": 2435296,
        "scientific_name": "Trichechus manatus",
        "common": "West Indian manatee",
    },
}
