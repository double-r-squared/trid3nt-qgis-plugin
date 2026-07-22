"""Unit tests for ``_species_reference`` (job-0117).

Validates the structural shape of ``FLORIDA_DEMO_SPECIES``: every entry has
the three required keys (``gbif_taxon_key`` int, ``scientific_name`` str,
``common`` str) and the four canonical species are present.

The taxonKeys themselves were live-verified against the GBIF species endpoint
on 2026-06-08 (see module docstring); the live cross-check is exercised in
``test_fetch_gbif_occurrences::test_live_florida_panther_over_big_cypress``.
"""

from __future__ import annotations

from grace2_agent.tools._species_reference import (
    FLORIDA_DEMO_SPECIES,
    DemoSpeciesEntry,
)


# ---------------------------------------------------------------------------
# Structural validation.
# ---------------------------------------------------------------------------


def test_florida_demo_species_is_nonempty():
    assert isinstance(FLORIDA_DEMO_SPECIES, dict)
    assert len(FLORIDA_DEMO_SPECIES) >= 4


def test_florida_demo_species_canonical_keys_present():
    """The four canonical Florida demo species are all defined."""
    expected = {"florida_panther", "american_alligator", "roseate_spoonbill", "manatee"}
    assert expected.issubset(FLORIDA_DEMO_SPECIES.keys())


def test_every_entry_has_required_fields():
    """Each entry has gbif_taxon_key (int), scientific_name (str), common (str)."""
    for key, entry in FLORIDA_DEMO_SPECIES.items():
        assert "gbif_taxon_key" in entry, f"{key!r} missing gbif_taxon_key"
        assert "scientific_name" in entry, f"{key!r} missing scientific_name"
        assert "common" in entry, f"{key!r} missing common"
        assert isinstance(entry["gbif_taxon_key"], int), (
            f"{key!r}.gbif_taxon_key must be int; got {type(entry['gbif_taxon_key']).__name__}"
        )
        assert entry["gbif_taxon_key"] > 0, (
            f"{key!r}.gbif_taxon_key must be positive; got {entry['gbif_taxon_key']}"
        )
        assert isinstance(entry["scientific_name"], str), (
            f"{key!r}.scientific_name must be str"
        )
        assert entry["scientific_name"].strip(), (
            f"{key!r}.scientific_name must be non-empty"
        )
        assert isinstance(entry["common"], str), f"{key!r}.common must be str"
        assert entry["common"].strip(), f"{key!r}.common must be non-empty"


def test_florida_panther_uses_species_level_taxon_key():
    """OQ-0087-PANTHER-TAXON-KEY: must NOT use the subspecies key 7193927.

    Florida-panther occurrences in GBIF are catalogued under the parent species
    (Puma concolor = 2435099) not the subspecies (Puma concolor concolor =
    7193927). Using 7193927 returns no records in Florida — the demo would
    silently appear broken.
    """
    panther = FLORIDA_DEMO_SPECIES["florida_panther"]
    assert panther["gbif_taxon_key"] == 2435099, (
        f"Florida panther must use species-level GBIF key 2435099, "
        f"not subspecies key (got {panther['gbif_taxon_key']}); "
        f"see OQ-0087-PANTHER-TAXON-KEY."
    )
    assert panther["scientific_name"] == "Puma concolor"


def test_taxon_keys_are_unique():
    """No two demo species share a taxonKey (would indicate a copy-paste error)."""
    keys = [entry["gbif_taxon_key"] for entry in FLORIDA_DEMO_SPECIES.values()]
    assert len(keys) == len(set(keys)), (
        f"Duplicate taxonKeys in FLORIDA_DEMO_SPECIES: {keys}"
    )


def test_demo_species_entry_typed_dict_signature():
    """DemoSpeciesEntry TypedDict is importable + has the three required keys."""
    # TypedDict introspection: __required_keys__ surfaces the schema fields.
    required = getattr(DemoSpeciesEntry, "__required_keys__", None)
    assert required is not None, "DemoSpeciesEntry should be a TypedDict"
    assert {"gbif_taxon_key", "scientific_name", "common"}.issubset(required)
