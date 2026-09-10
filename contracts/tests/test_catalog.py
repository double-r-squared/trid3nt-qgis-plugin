"""Round-trip and negative tests for ``CatalogEntry`` and its two collections.

Tier-1 and Tier-2 entries round-trip byte-identically; the cross-field rule
refuses tier-1-with-secret-ref and tier-2-or-3-without; the collection document
is a ``CatalogEntry`` and the audit-log document round-trips with ULID ``_id``
aliasing; a second schema export is byte-identical; a cost field is refused."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from trid3nt_contracts.catalog import CatalogEntry
from trid3nt_contracts.collections import (
    CATALOG_AUDIT_LOG_INDEXES,
    CATALOG_ENTRIES_INDEXES,
    MONGO_DUMP_KWARGS,
    CatalogAuditLogDocument,
    CatalogEntryDocument,
)
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.export_schemas import export


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _tier1_entry() -> CatalogEntry:
    """A Tier-1 (key-free public) catalog entry — USGS 3DEP DEM substrate."""
    return CatalogEntry(
        id="usgs-3dep-dem-1m",
        name="USGS 3DEP 1m DEM",
        description="USGS 3D Elevation Program 1-meter DEM, CONUS coverage.",
        urls=[
            "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/",
            "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/",
        ],
        access_tier=3,
        credential_tier=1,
        ttl_class="static-30d",
        source_class="dem",
        license="Public Domain (US Federal)",
        citation="U.S. Geological Survey, 2024, 3D Elevation Program 1-meter DEM",
        vintage="2024",
        last_verified=datetime(2026, 6, 1, tzinfo=timezone.utc),
        status="active",
        how_to_use=(
            "Access via /vsicurl/ windowed reads; bbox in EPSG:4326.\n"
            "Tile index at /vsicurl/<base>/USGS_one_meter_*.tif.\n"
            "Quirks: tile boundaries do not align to UTM zones."
        ),
        # api_key_secret_ref omitted — Tier 1 must not declare one.
    )


def _tier2_entry() -> CatalogEntry:
    """A Tier-2 (key-required free) catalog entry — Census ACS B01003."""
    return CatalogEntry(
        id="census-acs-b01003-2022",
        name="US Census ACS B01003 Population (2022 5-year)",
        description="American Community Survey total population at tract level.",
        urls=["https://api.census.gov/data/2022/acs/acs5"],
        access_tier=3,
        credential_tier=2,
        ttl_class="static-30d",
        source_class="population",
        license="Public Domain (US Federal)",
        citation="U.S. Census Bureau, 2022 American Community Survey 5-Year",
        vintage="2022",
        last_verified=datetime(2026, 6, 1, tzinfo=timezone.utc),
        status="active",
        how_to_use=(
            "GET ?get=B01003_001E&for=tract:*&in=state:<FIPS>&key=<KEY>\n"
            "Returns CSV; tract-level rows. Bbox not supported — use FIPS filters."
        ),
        api_key_secret_ref="projects/legacy-cloud-project/secrets/census_acs_api_key/versions/latest",
    )


# --------------------------------------------------------------------------- #
# 1. CatalogEntry round-trip
# --------------------------------------------------------------------------- #


def test_catalog_entry_mode1_roundtrip_idempotent() -> None:
    """JSON serialize -> deserialize -> re-serialize is byte-identical for both tiers."""
    for entry in (_tier1_entry(), _tier2_entry()):
        a = entry.model_dump(mode="json")
        text_a = json.dumps(a, sort_keys=True)
        b = CatalogEntry.model_validate(json.loads(text_a)).model_dump(mode="json")
        text_b = json.dumps(b, sort_keys=True)
        assert text_a == text_b, f"non-idempotent round-trip for {entry.id}"
        # Sanity: required fields land in the wire form.
        assert a["schema_version"] == "v1"
        assert a["status"] == "active"
        assert isinstance(a["urls"], list) and len(a["urls"]) >= 1
        assert a["last_verified"].endswith("Z"), "datetime must serialize with Z suffix"


# --------------------------------------------------------------------------- #
# 2. CatalogEntry credential-tier cross-field validator
# --------------------------------------------------------------------------- #


def test_catalog_entry_credential_tier_validator() -> None:
    """Tier 1 rejects api_key_secret_ref; Tier 2/3 require it."""
    # Tier 1 + secret-ref => ValidationError
    base = _tier1_entry().model_dump(mode="json")
    base["api_key_secret_ref"] = "projects/x/secrets/y/versions/latest"
    with pytest.raises(ValidationError) as exc:
        CatalogEntry.model_validate(base)
    assert "credential_tier=1" in str(exc.value)

    # Tier 2 + no secret-ref => ValidationError
    tier2 = _tier2_entry().model_dump(mode="json")
    tier2["api_key_secret_ref"] = None
    with pytest.raises(ValidationError) as exc:
        CatalogEntry.model_validate(tier2)
    assert "credential_tier=2" in str(exc.value)

    # Tier 2 + empty-string secret-ref => ValidationError (non-empty required)
    tier2["api_key_secret_ref"] = ""
    with pytest.raises(ValidationError):
        CatalogEntry.model_validate(tier2)

    # Tier 3 + missing secret-ref => ValidationError too
    tier3 = _tier2_entry().model_dump(mode="json")
    tier3["credential_tier"] = 3
    tier3["api_key_secret_ref"] = None
    with pytest.raises(ValidationError) as exc:
        CatalogEntry.model_validate(tier3)
    assert "credential_tier=3" in str(exc.value)


# --------------------------------------------------------------------------- #
# 3. D.11 CatalogEntryDocument
# --------------------------------------------------------------------------- #


def test_catalog_entry_document_inherits_catalog_entry() -> None:
    """CatalogEntryDocument *is* a CatalogEntry; it round-trips identically."""
    entry = _tier1_entry()
    doc = CatalogEntryDocument(**entry.model_dump(mode="json"))

    # Mongo dump form: by_alias has no effect since CatalogEntry's id is not aliased
    # (the entry id is the document id; no underscore field).
    mongo_form = doc.model_dump(**MONGO_DUMP_KWARGS)
    assert mongo_form["id"] == "usgs-3dep-dem-1m"
    assert mongo_form["status"] == "active"
    assert mongo_form["urls"][0].startswith("https://")

    # Round-trip through json identical to bare CatalogEntry.
    assert json.dumps(doc.model_dump(mode="json"), sort_keys=True) == json.dumps(
        entry.model_dump(mode="json"), sort_keys=True
    )

    # Indexes declared (smoke-check the contract surface infra consumes).
    index_names = {idx["name"] for idx in CATALOG_ENTRIES_INDEXES}
    assert "catalog_entries_source_class_1" in index_names
    assert "catalog_entries_status_1_source_class_1" in index_names


# --------------------------------------------------------------------------- #
# 4. D.12 CatalogAuditLogDocument
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "event_type",
    ["add", "update", "deprecate", "user_proposed", "curator_approved", "curator_rejected"],
)
def test_catalog_audit_log_document_roundtrip(event_type: str) -> None:
    """Every event_type literal round-trips; ULID _id aliasing works."""
    doc = CatalogAuditLogDocument(
        id=new_ulid(),
        entry_id="usgs-3dep-dem-1m",
        session_id=new_ulid(),
        user_id=None,  # v0.1: no user identity yet
        event_type=event_type,  # type: ignore[arg-type]
        event_payload={"note": "test"},
        timestamp=datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc),
    )
    mongo_form = doc.model_dump(**MONGO_DUMP_KWARGS)
    assert "_id" in mongo_form  # alias applied
    assert "id" not in mongo_form
    assert mongo_form["event_type"] == event_type
    assert mongo_form["timestamp"].endswith("Z")

    # Idempotent round-trip
    restored = CatalogAuditLogDocument.model_validate(
        {"_id": mongo_form["_id"], **{k: v for k, v in mongo_form.items() if k != "_id"}}
    )
    assert restored.event_type == event_type

    # Unknown event_type rejected (closed Literal)
    bad = mongo_form.copy()
    bad["event_type"] = "frobnicate"
    with pytest.raises(ValidationError):
        CatalogAuditLogDocument.model_validate(
            {**bad}
        )

    # Index declaration sanity
    assert any(
        idx["name"] == "catalog_audit_log_entry_id_1_timestamp_-1"
        for idx in CATALOG_AUDIT_LOG_INDEXES
    )


# --------------------------------------------------------------------------- #
# 5. JSON Schema export includes new contracts + is idempotent
# --------------------------------------------------------------------------- #


def test_json_schema_export_includes_new_contracts_and_is_idempotent(tmp_path: Path) -> None:
    """First export writes the new schemas; a second export is byte-identical."""
    export(tmp_path)
    expected = [
        "catalog_entry.json",
        "catalog_entry_document.json",
        "catalog_audit_log_document.json",
    ]
    for stem in expected:
        assert (tmp_path / stem).exists(), f"missing exported schema: {stem}"

    snapshot_a = {p.name: p.read_bytes() for p in sorted(tmp_path.glob("*.json"))}
    export(tmp_path)
    snapshot_b = {p.name: p.read_bytes() for p in sorted(tmp_path.glob("*.json"))}
    assert snapshot_a == snapshot_b, "second export differs — not idempotent"


# --------------------------------------------------------------------------- #
# 6. Invariant 9 (no cost theater) negative control
# --------------------------------------------------------------------------- #


def test_catalog_entry_no_cost_field_invariant9() -> None:
    """Extra fields like cost_usd / estimated_cost are rejected by extra='forbid'."""
    base = _tier1_entry().model_dump(mode="json")
    for forbidden in ("cost_usd", "estimated_cost", "cost_per_call", "monthly_quota_cost"):
        bad = {**base, forbidden: 0.01}
        with pytest.raises(ValidationError):
            CatalogEntry.model_validate(bad)


