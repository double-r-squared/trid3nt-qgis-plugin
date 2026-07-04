"""Contract tests for the thin per-case manifest dual-write (#165 data-island).

The data-island North Star wants a future cold path that lists cases + their
layers straight from S3 with the agent box asleep, WITHOUT downloading the fat
case-view snapshot per Case. This sprint adds a THIN ``CaseManifest`` written
ALONGSIDE the existing snapshot (DUAL-WRITE). The snapshot is NOT retired here
(cold serving + retirement are later phases) — the invariant under test is that
the manifest is additive and the snapshot path is unchanged.

Coverage:
- ``test_manifest_targets_runs_bucket_manifests_key`` — the S3 put goes to the
  durable runs bucket under ``case-manifests/{case_id}.json``.
- ``test_manifest_has_right_shape`` — the body validates as a ``CaseManifest``
  and projects the Case doc's ``loaded_layer_summaries`` into manifest layer
  rows (title / bbox / hazard / layer asset URLs).
- ``test_manifest_asset_url_is_display_face`` — a layer's ``asset_url`` is the
  DISPLAY face (``wms_url`` slot — tile-template / geojson asset / WMS URL),
  with ``wms_url`` set ONLY for a genuine WMS GetMap face.
- ``test_owner_travels_in_s3_metadata_not_body`` — the owner is carried in S3
  OBJECT METADATA exactly as the snapshot does, NEVER in the JSON body.
- ``test_owner_metadata_omitted_when_no_owner`` — no owner -> empty metadata.
- ``test_manifest_write_is_best_effort_on_s3_failure`` — an S3 error returns
  ``False`` and never raises.
- ``test_manifest_async_injected_put_is_awaited`` — an async injected put is
  awaited (the production path runs put_object off-thread).
- ``test_manifest_missing_case_returns_false_no_put`` — a missing Case never
  writes (returns False, no put).
- ``test_manifest_key_seam_matches`` — ``case_manifest_key`` is the single seam.
- ``test_manifest_failure_does_not_break_snapshot`` — DUAL-WRITE invariant: a
  manifest write that blows up does NOT prevent the snapshot write (the two are
  independent best-effort writers).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grace2_agent.persistence import (
    CASE_MANIFESTS_PREFIX,
    CASE_VIEWS_BUCKET,
    Persistence,
    case_manifest_key,
)
from grace2_contracts.case import CaseManifest
from grace2_contracts.common import new_ulid

# Reuse the snapshot test's seed helper + fake S3 capture so the manifest tests
# exercise the SAME Case fixture the snapshot tests do (file-disjoint lane).
from .test_case_view_snapshot import _FakeS3, _seed_case_with_vector_layer


# --------------------------------------------------------------------------- #
# S3 target + shape
# --------------------------------------------------------------------------- #


def test_manifest_targets_runs_bucket_manifests_key() -> None:
    p, case_id, _vector_layer_id, _inline = _seed_case_with_vector_layer()
    fake = _FakeS3()

    ok = asyncio.run(p.write_case_manifest(case_id, s3_put=fake.put))
    assert ok is True
    assert fake.bucket == CASE_VIEWS_BUCKET
    assert fake.key == f"{CASE_MANIFESTS_PREFIX}/{case_id}.json"
    assert fake.key == case_manifest_key(case_id)
    # Body is valid UTF-8 JSON.
    json.loads(fake.body.decode("utf-8"))


def test_manifest_key_seam_matches() -> None:
    cid = new_ulid()
    assert case_manifest_key(cid) == f"case-manifests/{cid}.json"


def test_manifest_has_right_shape() -> None:
    p, case_id, vector_layer_id, _inline = _seed_case_with_vector_layer()
    fake = _FakeS3()
    asyncio.run(p.write_case_manifest(case_id, s3_put=fake.put))

    body = json.loads(fake.body.decode("utf-8"))
    # Validates as a CaseManifest (round-trip contract).
    manifest = CaseManifest.model_validate(body)
    assert manifest.case_id == case_id
    assert manifest.title == "Hurricane Ian — Fort Myers flood scenario"
    assert manifest.primary_hazard == "flood"
    assert manifest.bbox == (-82.0, 26.5, -81.8, 26.7)
    assert manifest.updated_at is not None
    # The seeded Case carries a vector + raster layer -> two manifest rows.
    layer_ids = {layer.layer_id for layer in manifest.layers}
    assert vector_layer_id in layer_ids
    assert len(manifest.layers) == 2
    # Each row carries the projected fields the cold path needs.
    for layer in manifest.layers:
        assert layer.layer_id
        assert layer.name
        assert layer.layer_type in {"raster", "vector"}
        assert layer.style_preset
        assert layer.asset_url


def test_manifest_asset_url_is_display_face() -> None:
    """A WMS display face routes to ``asset_url`` AND ``wms_url``; a plain
    s3:// data uri (no display face) falls back to ``asset_url`` only."""
    from grace2_contracts.collections import ProjectLayerSummary

    mock_p, case_id, _vid, _inline = _seed_case_with_vector_layer()

    # Build a Case whose layers carry an explicit display (wms_url) face.
    from grace2_contracts.case import CaseSummary
    from datetime import datetime, timezone

    wms_layer = ProjectLayerSummary(
        layer_id="wms-layer",
        name="WMS layer",
        layer_type="raster",
        uri="s3://grace2-runs/abc/data.tif",
        style_preset="flood-depth",
        visible=True,
        role="primary",
        temporal=False,
        wms_url="https://qgis.example/ogc/wms?SERVICE=WMS&LAYERS=wms-layer",
    )
    nodisplay_layer = ProjectLayerSummary(
        layer_id="data-only",
        name="Data only",
        layer_type="vector",
        uri="s3://grace2-runs/case-data/CID/data-only.geojson",
        style_preset="buildings",
        visible=True,
        role="context",
        temporal=False,
        wms_url=None,
    )
    case_id2 = new_ulid()
    case = CaseSummary(
        case_id=case_id2,
        title="Display-face case",
        created_at=datetime(2026, 6, 8, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 8, tzinfo=timezone.utc),
        loaded_layer_summaries=[
            wms_layer.model_dump(mode="json"),
            nodisplay_layer.model_dump(mode="json"),
        ],
    )
    asyncio.run(mock_p.upsert_case(case))

    manifest = asyncio.run(mock_p.build_case_manifest(case_id2))
    assert manifest is not None
    rows = {layer.layer_id: layer for layer in manifest.layers}

    # WMS layer: asset_url IS the wms display face, wms_url carried separately.
    assert rows["wms-layer"].asset_url.startswith("https://qgis.example/ogc/wms")
    assert rows["wms-layer"].wms_url == rows["wms-layer"].asset_url

    # Data-only layer: no display face -> asset_url falls back to the s3 uri,
    # wms_url stays None (it is not a WMS GetMap face).
    assert rows["data-only"].asset_url.endswith("data-only.geojson")
    assert rows["data-only"].wms_url is None


# --------------------------------------------------------------------------- #
# Owner-gate carrier: owner travels in S3 OBJECT METADATA, never in the body
# --------------------------------------------------------------------------- #


def test_owner_travels_in_s3_metadata_not_body() -> None:
    owner = new_ulid()
    p, case_id, _vid, _inline = _seed_case_with_vector_layer(owner_user_id=owner)
    fake = _FakeS3()

    ok = asyncio.run(p.write_case_manifest(case_id, s3_put=fake.put))
    assert ok is True
    # Owner ONLY in object metadata.
    assert fake.metadata == {"owner-user-id": owner}
    # ... and NEVER in the body.
    assert owner not in fake.body.decode("utf-8")


def test_owner_metadata_omitted_when_no_owner() -> None:
    p, case_id, _vid, _inline = _seed_case_with_vector_layer()
    fake = _FakeS3()
    ok = asyncio.run(p.write_case_manifest(case_id, s3_put=fake.put))
    assert ok is True
    assert fake.metadata == {}


# --------------------------------------------------------------------------- #
# Best-effort discipline
# --------------------------------------------------------------------------- #


def test_manifest_write_is_best_effort_on_s3_failure() -> None:
    p, case_id, _vid, _inline = _seed_case_with_vector_layer()

    def _boom(bucket, key, body, metadata):
        raise RuntimeError("S3 down")

    # Must NOT raise — returns False on any failure.
    ok = asyncio.run(p.write_case_manifest(case_id, s3_put=_boom))
    assert ok is False


def test_manifest_async_injected_put_is_awaited() -> None:
    p, case_id, _vid, _inline = _seed_case_with_vector_layer()
    captured: dict = {}

    async def _aput(bucket, key, body, metadata):
        captured["bucket"] = bucket
        captured["key"] = key
        captured["body"] = body
        captured["metadata"] = metadata

    ok = asyncio.run(p.write_case_manifest(case_id, s3_put=_aput))
    assert ok is True
    assert captured["bucket"] == CASE_VIEWS_BUCKET
    assert captured["key"] == case_manifest_key(case_id)
    assert captured["metadata"] == {}  # no owner stamped


def test_manifest_missing_case_returns_false_no_put() -> None:
    p, _case_id, _vid, _inline = _seed_case_with_vector_layer()
    fake = _FakeS3()
    missing = new_ulid()
    ok = asyncio.run(p.write_case_manifest(missing, s3_put=fake.put))
    # A missing Case is not materialized: no put, returns False.
    assert ok is False
    assert fake.call_count == 0


# --------------------------------------------------------------------------- #
# DUAL-WRITE invariant: a manifest failure does NOT break the snapshot path
# --------------------------------------------------------------------------- #


def test_manifest_failure_does_not_break_snapshot() -> None:
    """The two writers are independent best-effort writers.

    Even when the manifest write blows up, the snapshot write still lands. This
    proves the dual-write is additive: a manifest hiccup never regresses the
    existing snapshot (view-without-agent) path.
    """
    p, case_id, _vid, _inline = _seed_case_with_vector_layer()
    snap_fake = _FakeS3()

    def _manifest_boom(bucket, key, body, metadata):
        raise RuntimeError("manifest S3 down")

    # Snapshot still succeeds.
    snap_ok = asyncio.run(
        p.write_case_view_snapshot(case_id, s3_put=snap_fake.put)
    )
    assert snap_ok is True
    assert snap_fake.call_count == 1
    assert snap_fake.key.startswith("case-views/")

    # Manifest fails but never raises -> returns False; snapshot was untouched.
    manifest_ok = asyncio.run(
        p.write_case_manifest(case_id, s3_put=_manifest_boom)
    )
    assert manifest_ok is False
    # The snapshot capture is unchanged (manifest never touched it).
    assert snap_fake.call_count == 1
    assert snap_fake.key.startswith("case-views/")
