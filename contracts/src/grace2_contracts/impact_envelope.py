"""ImpactEnvelope — Pelicun post-processor output contract (SRS Appendix B.6c).

The ``ImpactEnvelope`` is the structured aggregate produced by
``postprocess_pelicun`` after aggregating a per-feature Pelicun damage-state
FlatGeobuf (emitted by ``run_pelicun_damage_assessment``) into portfolio-level
damage, loss, and population-impact statistics.

**Why a separate envelope type?**

``run_pelicun_damage_assessment`` returns a ``LayerURI`` pointing at a
per-feature FlatGeobuf — each asset carries ``ds_mean``, ``repair_cost_mean``,
``loss_ratio_mean``, etc.  The UI map layer is useful for spatial exploration,
but the agent's narrative and the UI summary panel need *aggregate* statistics:
total structures damaged, expected portfolio loss (USD), displaced population,
and per-occupancy-class breakdowns.  ``ImpactEnvelope`` is that aggregate
output — a typed, narratable shape that gives the agent **every number it might
cite without inventing any** (Invariant 1 / Decision N).

**Consumer guidance:**

- *Emitted by*: ``postprocess_pelicun`` (Wave 4.11 P2 atomic tool, sprint-12).
- *Consumed by*: agent narration (``impact.n_structures_damaged``,
  ``impact.expected_loss_usd`` etc. are cite-safe); the Case summary panel
  (``ImpactPanel`` — surfaced live via the ``impact-envelope`` WS frame).
  NOTE (sprint-14-aws / M5.5): the envelope is emitted live to the panel but
  is NOT currently persisted — there is no DynamoDB write of the raw envelope,
  so on a Case reload the panel is empty until the agent re-emits. Persisting
  the envelope alongside the parent run record is tracked as a follow-up;
  this contract makes no persistence guarantee today.
- *Provenance*: every numeric claim is traceable to ``pelicun_run_id`` (the
  ULID of the ``run_pelicun_damage_assessment`` call), ``damage_layer_uri``
  (the FlatGeobuf that was aggregated), and ``flood_layer_uri`` (the source
  hazard raster).

**Invariants this module enforces:**

- **1. Determinism boundary.** Every numeric field is a computed aggregate from
  the source FlatGeobuf; no LLM-generated numbers appear.
- **9. No cost theater.** No tool-invocation cost / quota field anywhere.
- ``extra="forbid"`` (via ``GraceModel``): unknown fields are a schema defect.

SRS reference: ``docs/srs/B-assessment-envelope-schema.md`` § B.6c.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import BBox, GraceModel, ULIDStr, UTCDatetime

__all__ = [
    "DamageStateKey",
    "StructureInventorySource",
    "OccupancyClassImpact",
    "ImpactEnvelope",
]


# --------------------------------------------------------------------------- #
# Typed vocabulary
# --------------------------------------------------------------------------- #

# HAZUS damage-state label set.  Closed Literal; any extension is an SRS
# amendment (the fragility-curve binning logic that maps loss ratios → DS
# labels is co-owned with ``run_pelicun_damage_assessment``).
DamageStateKey = Literal[
    "DS0_none",
    "DS1_slight",
    "DS2_moderate",
    "DS3_extensive",
    "DS4_complete",
]

# Structure inventory sources recognized by ``postprocess_pelicun``.  A new
# source requires both a new atomic fetcher tool *and* an SRS amendment here.
StructureInventorySource = Literal["USACE_NSI", "MS_BUILDINGS", "USER_SUPPLIED"]


# --------------------------------------------------------------------------- #
# Per-occupancy-class breakdown
# --------------------------------------------------------------------------- #


class OccupancyClassImpact(GraceModel):
    """Damage / loss summary for a single HAZUS occupancy class.

    Keys match the HAZUS vocabulary used throughout the tool chain:
    ``RES1``, ``RES3``, ``COM1``, ``IND1``, etc.  Populated for every
    occupancy class present in the source FlatGeobuf (after filtering by
    ``component_types`` in the upstream ``run_pelicun_damage_assessment`` call).

    Population fields are ``None`` when the structure inventory does not carry
    per-structure occupancy/population data (e.g. ``MS_BUILDINGS`` source,
    which defaults to ``RES1`` and carries no population attribute).
    ``USACE_NSI`` always populates these fields from the NSI ``pop2amu65`` /
    ``pop2amo65`` / ``pop2pmu65`` / ``pop2pmo65`` columns.
    """

    # Count statistics
    n_structures: int = Field(ge=0, description="Total structures of this occupancy class in the assessment area.")
    n_damaged: int = Field(ge=0, description="Structures with expected damage state DS1 or higher (ds_mean >= 1.0).")
    n_destroyed: int = Field(ge=0, description="Structures with expected damage state DS4 (ds_mean >= 3.5).")

    # Loss statistics (USD)
    expected_loss_usd: float = Field(ge=0.0, description="Sum of repair_cost_mean across all structures in this class.")
    loss_percentile_95_usd: float = Field(ge=0.0, description="Portfolio-level 95th-percentile loss (sum of repair_cost_p95).")

    # Population (None when inventory source lacks population attributes)
    population: int | None = Field(default=None, ge=0, description="Modeled daytime (AM) population from NSI pop2amu65+pop2amo65, or None.")
    population_displaced: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Estimated displaced population: structures with DS2+ "
            "(loss_ratio_mean ≥ 0.20) × mean occupant count from NSI, or None."
        ),
    )


# --------------------------------------------------------------------------- #
# Top-level ImpactEnvelope
# --------------------------------------------------------------------------- #


class ImpactEnvelope(GraceModel):
    """Pelicun post-processor output — portfolio-level damage/loss/population aggregates.

    Produced by ``postprocess_pelicun`` by aggregating the per-feature damage
    FlatGeobuf returned by ``run_pelicun_damage_assessment`` over all asset
    features.  Every numeric field is a deterministic aggregate computed from
    the source layer; no LLM-generated numbers appear (Invariant 1).

    **Damage-state thresholds (HAZUS convention):**

    - *Damaged* (``n_structures_damaged``): any structure with
      ``ds_mean >= 1.0``, i.e. expected DS1 or higher.
    - *Destroyed* (``n_structures_destroyed``): structures with
      ``ds_mean >= 3.5``, i.e. predominantly DS4 (complete loss) in
      Monte-Carlo realizations.

    **Loss statistics:**

    - ``expected_loss_usd``: sum of ``repair_cost_mean`` across all assets.
    - ``loss_percentile_95_usd``: sum of ``repair_cost_p95`` across all assets
      (a conservative portfolio-loss estimate; note this is NOT the true
      portfolio-level P95 — that requires a joint distribution computation not
      available from per-asset p95 sums, but this is the standard HAZUS-MH
      approximation and is labeled accordingly).

    **Population estimates:**

    Population fields come from the USACE NSI ``pop2amu65``/``pop2amo65``
    (AM residential population) columns.  When the inventory source is
    ``MS_BUILDINGS`` (no per-structure population), these fields are ``None``.

    **Spatial summary:**

    ``impact_area_km2``: area of the bounding polygon of *damaged* assets
    (DS1+), not the full assessment bbox.  Computed from the convex hull of
    damaged asset centroids.  ``bbox`` mirrors the source damage layer's bbox.

    **Provenance fields:**

    - ``pelicun_run_id``: ULID generated by ``postprocess_pelicun``; stable
      across re-runs with the same inputs (seeded from input hashes).
    - ``damage_layer_uri``: the gs:// URI of the FlatGeobuf aggregated; this
      is the ``LayerURI.uri`` returned by ``run_pelicun_damage_assessment``.
    - ``flood_layer_uri``: the gs:// URI of the source hazard raster (the
      ``hazard_raster_uri`` passed to ``run_pelicun_damage_assessment``).
    - ``structure_inventory_source``: typed Literal — ``"USACE_NSI"``,
      ``"MS_BUILDINGS"``, or ``"USER_SUPPLIED"``.
    """

    schema_version: Literal["v1"] = "v1"

    # ---------------------------------------------------------------------- #
    # Damage statistics
    # ---------------------------------------------------------------------- #

    n_structures_total: int = Field(
        ge=0,
        description="Total asset features in the damage layer (all damage states).",
    )
    n_structures_damaged: int = Field(
        ge=0,
        description=(
            "Structures with expected damage state DS1+ (ds_mean >= 1.0). "
            "Threshold is per the HAZUS fragility curve binning in "
            "run_pelicun_damage_assessment."
        ),
    )
    n_structures_destroyed: int = Field(
        ge=0,
        description=(
            "Structures with expected damage state predominantly DS4 "
            "(ds_mean >= 3.5), i.e. complete loss in most Monte-Carlo "
            "realizations."
        ),
    )
    damage_state_distribution: dict[DamageStateKey, int] = Field(
        description=(
            "Count of structures with modal damage state in each DS bucket. "
            "Keys: DS0_none, DS1_slight, DS2_moderate, DS3_extensive, DS4_complete. "
            "Values are structure counts; sum equals n_structures_total."
        ),
    )

    # ---------------------------------------------------------------------- #
    # Financial loss statistics (USD)
    # ---------------------------------------------------------------------- #

    total_replacement_value_usd: float = Field(
        ge=0.0,
        description=(
            "Sum of per-asset replacement_value across all assets in the "
            "damage layer (USD). Drawn from NSI val_struct where available; "
            "falls back to HAZUS-MH 4.2 class defaults scaled to 2024 USD."
        ),
    )
    damaged_replacement_value_usd: float = Field(
        ge=0.0,
        description="Sum of replacement_value for damaged assets only (DS1+).",
    )
    expected_loss_usd: float = Field(
        ge=0.0,
        description="Sum of repair_cost_mean across all assets (USD).",
    )
    loss_percentile_95_usd: float = Field(
        ge=0.0,
        description=(
            "Sum of repair_cost_p95 across all assets (USD). Standard HAZUS-MH "
            "portfolio P95 approximation; not a true joint-distribution P95."
        ),
    )

    # ---------------------------------------------------------------------- #
    # Population statistics
    # ---------------------------------------------------------------------- #

    population_total: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Total AM residential population in the assessment area, summed "
            "from NSI pop2amu65+pop2amo65 across all asset features. "
            "None when inventory source is MS_BUILDINGS (no population data)."
        ),
    )
    population_displaced: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Estimated displaced population: sum of NSI AM population for "
            "assets with loss_ratio_mean >= 0.20 (DS2+ — moderate damage, "
            "implying likely uninhabitable). None when source lacks population."
        ),
    )
    population_at_high_risk: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Population in assets with expected DS3 or DS4 (ds_mean >= 2.5). "
            "High-risk occupants face extensive-to-complete structural damage. "
            "None when source lacks population data."
        ),
    )

    # ---------------------------------------------------------------------- #
    # Spatial summary
    # ---------------------------------------------------------------------- #

    impact_area_km2: float = Field(
        ge=0.0,
        description=(
            "Area (km²) of the convex hull of damaged asset centroids (DS1+). "
            "Approximates the footprint of meaningful structural impact. "
            "Zero when no assets are damaged."
        ),
    )
    bbox: BBox = Field(
        description=(
            "Bounding box of the full damage layer in EPSG:4326: "
            "[minLon, minLat, maxLon, maxLat]. Copied from the source "
            "damage-assessment FlatGeobuf extent."
        ),
    )

    # ---------------------------------------------------------------------- #
    # Per-occupancy-class breakdown
    # ---------------------------------------------------------------------- #

    by_occupancy_class: dict[str, OccupancyClassImpact] = Field(
        description=(
            "Per-occupancy-class (HAZUS vocabulary) damage/loss/population "
            "breakdown.  Keys are HAZUS occupancy codes, e.g. "
            "'RES1', 'COM1', 'IND1'. "
            "Only classes present in the damage layer are populated."
        ),
    )

    # ---------------------------------------------------------------------- #
    # Provenance + metadata
    # ---------------------------------------------------------------------- #

    pelicun_run_id: ULIDStr = Field(
        description=(
            "ULID identifying this postprocess_pelicun run. Seeded from the "
            "input FlatGeobuf content hash + bbox so identical inputs produce "
            "the same run_id (cache-stable)."
        ),
    )
    damage_layer_uri: str = Field(
        min_length=1,
        description=(
            "gs:// URI of the FlatGeobuf aggregated by postprocess_pelicun. "
            "This is the LayerURI.uri returned by run_pelicun_damage_assessment."
        ),
    )
    structure_inventory_source: StructureInventorySource = Field(
        description=(
            "Typed source of the structure inventory used as assets in the "
            "upstream Pelicun run. Determines whether population fields are "
            "populated (USACE_NSI: yes; MS_BUILDINGS / USER_SUPPLIED: None)."
        ),
    )
    flood_layer_uri: str = Field(
        min_length=1,
        description=(
            "gs:// URI of the source hazard raster (hazard_raster_uri passed "
            "to run_pelicun_damage_assessment)."
        ),
    )
    fragility_set: str = Field(
        description=(
            "Fragility set used in the upstream Pelicun run, e.g. "
            "'hazus_flood_v6'. Carried forward for provenance and citation."
        ),
    )
    realization_count: int = Field(
        gt=0,
        description=(
            "Number of Monte-Carlo realizations used per asset in the upstream "
            "run_pelicun_damage_assessment call."
        ),
    )
    n_assets_default_replacement_value: int = Field(
        default=0,
        ge=0,
        description=(
            "Invariant 7 transparency: count of assessed structures whose loss "
            "figure rests on a HAZUS class-default replacement value (the source "
            "inventory lacked a usable per-asset replacement value, or the "
            "MS-buildings inventory which is default-by-design) rather than a "
            "measured value. Lets a consumer judge how much of expected_loss_usd "
            "is default-based. 0 means every loss used a measured replacement value "
            "(or the upstream layer predates this field)."
        ),
    )
    generated_at: UTCDatetime = Field(
        description="UTC timestamp when postprocess_pelicun produced this envelope.",
    )
