"""Round-trip + negative tests for the Mode 1 CatalogEntry, the new Appendix D
collections (D.11 ``catalog_entries`` + D.12 ``catalog_audit_log``), and the
four new Appendix A envelopes (sprint-08 — FR-FR-1 + §F.1.2 Mode 2).

Test count for this job (≥6 new tests required per the kickoff):

1. ``test_catalog_entry_mode1_roundtrip_idempotent`` — Tier-1 + Tier-2 entries
   round-trip through JSON serialize→deserialize→re-serialize, byte-identical.
2. ``test_catalog_entry_credential_tier_validator`` — cross-field rule rejects
   tier-1+secret-ref and tier-2/3-without-secret-ref combinations.
3. ``test_catalog_entry_document_inherits_catalog_entry`` — D.11 collection
   document is a CatalogEntry; round-trips through MongoDB dump kwargs.
4. ``test_catalog_audit_log_document_roundtrip`` — D.12 audit-log document
   round-trips with ULID ``_id`` aliasing + every event_type literal accepted.
5. ``test_recovery_choice_envelope_roundtrip`` — FR-FR-1 ``recovery-choice`` +
   ``recovery-choice-response`` shapes round-trip; options subset rule.
6. ``test_offer_catalog_addition_envelope_roundtrip`` — §F.1.2 Mode 2
   ``offer-catalog-addition`` + ``catalog-addition-response`` shapes round-trip
   with probe findings + suggested entry edits.
7. ``test_new_envelopes_in_payload_registries`` — all four new payloads are
   registered in the ``ALL_PAYLOADS`` / direction-specific registries.
8. ``test_json_schema_export_includes_new_contracts_and_is_idempotent`` —
   ``catalog_entry.json`` + four new ``ws_*.json`` + audit-log + entry-doc
   schemas are exported, and a second export is byte-identical.
9. ``test_catalog_entry_no_cost_field_invariant9`` — Invariant 9 negative
   control: ``cost_usd`` / ``estimated_cost`` extra fields are rejected.
10. ``test_recovery_choice_options_must_be_known_actions`` — options field
    is a closed Literal subset.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from grace2_contracts import ws
from grace2_contracts.catalog import CatalogEntry
from grace2_contracts.collections import (
    CATALOG_AUDIT_LOG_INDEXES,
    CATALOG_ENTRIES_INDEXES,
    MONGO_DUMP_KWARGS,
    CatalogAuditLogDocument,
    CatalogEntryDocument,
)
from grace2_contracts.common import new_ulid
from grace2_contracts.export_schemas import export
from grace2_contracts.ws import (
    CatalogAdditionResponsePayload,
    OfferCatalogAdditionPayload,
    ProbeFindings,
    RecoveryChoicePayload,
    RecoveryChoiceResponsePayload,
    SuggestedCatalogEntry,
)


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
# 5. FR-FR-1 recovery-choice + recovery-choice-response
# --------------------------------------------------------------------------- #


def test_recovery_choice_envelope_roundtrip() -> None:
    """FR-FR-1 envelopes round-trip; chat-text + cancelled cases work."""
    # recovery-choice (agent -> client)
    req = RecoveryChoicePayload(
        request_id=new_ulid(),
        failed_step_id=new_ulid(),
        error_code="UPSTREAM_API_ERROR",
        error_message="USGS 3DEP returned HTTP 503 — service unavailable",
        context="fetching DEM at Fort Myers bbox for flood scenario",
        options=["deny", "retry", "chat"],
        ttl_seconds=300,
    )
    a = req.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = RecoveryChoicePayload.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    assert req.MESSAGE_TYPE == "recovery-choice"

    # Empty options list rejected (gate must offer at least one action).
    bad = a.copy()
    bad["options"] = []
    with pytest.raises(ValidationError):
        RecoveryChoicePayload.model_validate(bad)

    # FR-FR-2 narrowing — single-option gate (e.g. GEOCODE_NO_MATCH -> chat only)
    narrowed = RecoveryChoicePayload(
        request_id=new_ulid(),
        failed_step_id=new_ulid(),
        error_code="GEOCODE_NO_MATCH",
        error_message="No geocoding match for the supplied place name",
        context="resolving 'Fort Myers'",
        options=["chat"],
        ttl_seconds=300,
    )
    assert narrowed.options == ["chat"]

    # recovery-choice-response (client -> agent)
    resp = RecoveryChoiceResponsePayload(
        request_id=req.request_id,
        choice="chat",
        chat_text="try the WCS endpoint instead of WMS",
    )
    assert resp.MESSAGE_TYPE == "recovery-choice-response"
    text_r = json.dumps(resp.model_dump(mode="json"), sort_keys=True)
    restored = RecoveryChoiceResponsePayload.model_validate(json.loads(text_r))
    assert restored.chat_text == "try the WCS endpoint instead of WMS"

    # Cancellation path: no choice, cancelled=True
    cancel = RecoveryChoiceResponsePayload(
        request_id=req.request_id, choice=None, chat_text=None, cancelled=True
    )
    assert cancel.cancelled is True
    assert cancel.choice is None


# --------------------------------------------------------------------------- #
# 6. §F.1.2 Mode 2 offer-catalog-addition + catalog-addition-response
# --------------------------------------------------------------------------- #


def test_offer_catalog_addition_envelope_roundtrip() -> None:
    """§F.1.2 Mode 2 envelopes round-trip; suggested entry permissive shape."""
    suggested = SuggestedCatalogEntry(
        id="femanflp-discharge-stations",
        name="FEMA NFHL discharge stations",
        description="Discharge stations from the FEMA NFHL WFS feed.",
        urls=["https://hazards.fema.gov/nfhlv2/services/public/NFHL/MapServer/WFSServer"],
        access_tier=2,
        credential_tier=1,
        ttl_class="semi-static-7d",
        source_class="flood_zone",
        license_claim="Public domain (US Federal)",
        how_to_use="OGC WFS GetFeature; bbox in EPSG:4326; layer NFHL:DischargeStations",
    )
    probe = ProbeFindings(
        tls_cert_org="U.S. Department of Homeland Security",
        access_tier_inferred=2,
        supports_range_requests=False,
        stac_root_found=False,
        ogc_capabilities_found=True,
        license_observed="Public domain (US Federal)",
        content_type="application/xml",
        last_modified_header="Wed, 01 Jun 2026 12:00:00 GMT",
    )
    offer = OfferCatalogAdditionPayload(
        request_id=new_ulid(),
        url="https://hazards.fema.gov/nfhlv2/services/public/NFHL/MapServer/WFSServer",
        discovered_via="user-query",
        probe_findings=probe,
        suggested_catalog_entry=suggested,
        ttl_seconds=600,
    )
    a = offer.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = OfferCatalogAdditionPayload.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    assert offer.MESSAGE_TYPE == "offer-catalog-addition"

    # Response: accept with edits
    edited = SuggestedCatalogEntry(
        **{**suggested.model_dump(mode="json"), "name": "FEMA NFHL Discharge Stations (curator-edited)"}
    )
    resp = CatalogAdditionResponsePayload(
        request_id=offer.request_id,
        decision="accept",
        edited_catalog_entry=edited,
    )
    assert resp.MESSAGE_TYPE == "catalog-addition-response"
    text_r = json.dumps(resp.model_dump(mode="json"), sort_keys=True)
    restored = CatalogAdditionResponsePayload.model_validate(json.loads(text_r))
    assert restored.decision == "accept"
    assert restored.edited_catalog_entry is not None
    assert restored.edited_catalog_entry.name.endswith("(curator-edited)")

    # Response: reject with reason
    reject = CatalogAdditionResponsePayload(
        request_id=offer.request_id,
        decision="reject",
        reject_reason="content-type was XML but body returned an HTML press release",
    )
    assert reject.decision == "reject"

    # discovered_via closed Literal: unknown value rejected
    bad_offer = a.copy()
    bad_offer["discovered_via"] = "arxiv-paper"
    with pytest.raises(ValidationError):
        OfferCatalogAdditionPayload.model_validate(bad_offer)


# --------------------------------------------------------------------------- #
# 7. Payload registries
# --------------------------------------------------------------------------- #


def test_new_envelopes_in_payload_registries() -> None:
    """All four new sprint-08 payloads land in ALL_PAYLOADS with correct direction."""
    assert "recovery-choice" in ws.AGENT_TO_CLIENT_PAYLOADS
    assert "offer-catalog-addition" in ws.AGENT_TO_CLIENT_PAYLOADS
    assert "recovery-choice-response" in ws.CLIENT_TO_AGENT_PAYLOADS
    assert "catalog-addition-response" in ws.CLIENT_TO_AGENT_PAYLOADS

    # And aggregated:
    for t in (
        "recovery-choice",
        "recovery-choice-response",
        "offer-catalog-addition",
        "catalog-addition-response",
    ):
        assert t in ws.ALL_PAYLOADS, f"{t} missing from ALL_PAYLOADS"


# --------------------------------------------------------------------------- #
# 8. JSON Schema export includes new contracts + is idempotent
# --------------------------------------------------------------------------- #


def test_json_schema_export_includes_new_contracts_and_is_idempotent(tmp_path: Path) -> None:
    """First export writes the new schemas; a second export is byte-identical."""
    export(tmp_path)
    expected = [
        "catalog_entry.json",
        "catalog_entry_document.json",
        "catalog_audit_log_document.json",
        "ws_recovery_choice.json",
        "ws_recovery_choice_response.json",
        "ws_offer_catalog_addition.json",
        "ws_catalog_addition_response.json",
    ]
    for stem in expected:
        assert (tmp_path / stem).exists(), f"missing exported schema: {stem}"

    snapshot_a = {p.name: p.read_bytes() for p in sorted(tmp_path.glob("*.json"))}
    export(tmp_path)
    snapshot_b = {p.name: p.read_bytes() for p in sorted(tmp_path.glob("*.json"))}
    assert snapshot_a == snapshot_b, "second export differs — not idempotent"


# --------------------------------------------------------------------------- #
# 9. Invariant 9 (no cost theater) negative control
# --------------------------------------------------------------------------- #


def test_catalog_entry_no_cost_field_invariant9() -> None:
    """Extra fields like cost_usd / estimated_cost are rejected by extra='forbid'."""
    base = _tier1_entry().model_dump(mode="json")
    for forbidden in ("cost_usd", "estimated_cost", "cost_per_call", "monthly_quota_cost"):
        bad = {**base, forbidden: 0.01}
        with pytest.raises(ValidationError):
            CatalogEntry.model_validate(bad)


# --------------------------------------------------------------------------- #
# 10. RecoveryChoiceOption closed Literal
# --------------------------------------------------------------------------- #


def test_recovery_choice_options_must_be_known_actions() -> None:
    """Unknown action strings in options are rejected by the Literal."""
    base = RecoveryChoicePayload(
        request_id=new_ulid(),
        failed_step_id=new_ulid(),
        error_code="UPSTREAM_API_ERROR",
        error_message="x",
        context="x",
        options=["deny", "retry", "chat"],
        ttl_seconds=300,
    ).model_dump(mode="json")
    bad = {**base, "options": ["deny", "frobnicate"]}
    with pytest.raises(ValidationError):
        RecoveryChoicePayload.model_validate(bad)

    # Response side: unknown choice value also rejected
    bad_resp = {"request_id": new_ulid(), "choice": "frobnicate"}
    with pytest.raises(ValidationError):
        RecoveryChoiceResponsePayload.model_validate(bad_resp)
