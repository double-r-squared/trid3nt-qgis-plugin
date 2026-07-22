"""Contract tests for the materialized case-view snapshot (Lane A1).

The "view a Case with the agent box OFF" path (pen=agent / paper=case) serves a
PRE-SIGNED S3 GET of ``case-views/{case_id}.json``, a MATERIALIZED view-state
snapshot the agent writes on every Case mutation. For the web's existing
``useCases.onCaseOpen`` + ``App.tsx`` synthesize path to render it cold, the
snapshot MUST be byte-identical to the live ``case-open`` payload —
``CaseOpenEnvelopePayload(session_state=get_session_state(case_id))
.model_dump(mode="json")`` — with the ONE addition of the in-memory inline
vector GeoJSON merged onto the matching ``loaded_layers`` entries (persisted
vector layers carry no inline GeoJSON; the side-table is in-memory on the live
emitter).

Coverage:
- ``test_snapshot_is_byte_identical_to_case_open_payload_plus_inline_geojson`` —
  the S3-written JSON equals the live ``case-open`` payload byte-for-byte except
  the added inline vector GeoJSON; the vector layer's GeoJSON is present.
- ``test_snapshot_without_inline_is_exact_case_open_payload`` — with no inline
  side-table the snapshot is the unmodified ``case-open`` payload.
- ``test_snapshot_merges_density_tag_like_emit_session_state`` — a dense-vector
  ``vector_density`` tag is merged exactly as ``emit_session_state`` does.
- ``test_write_targets_runs_bucket_case_views_key`` — the S3 put goes to the
  durable runs bucket under ``case-views/{case_id}.json``,
  ``content-type: application/json``.
- ``test_write_is_best_effort_on_s3_failure`` — an S3 put error returns
  ``False`` and never raises (turn-safety discipline).
- ``test_async_injected_s3_put_is_awaited`` — an async injected ``s3_put`` is
  awaited (the production path runs put_object off-thread).
- ``test_cross_case_snapshot_inlines_vector_from_uri_without_open_case`` /
  ``test_build_cross_case_snapshot_inlines_vector_from_uri`` (FIX B, job-0372) -
  a snapshot built for a NON-open Case (no inline side-table) still carries
  non-empty ``inline_geojson`` for its vector layers, resolved from the
  persisted object-store URI at write time; rasters stay URI-only.
- ``test_explicit_inline_takes_precedence_over_uri_resolve`` - the open-case
  emitter's inline payload wins over the URI re-read (no override).
- ``test_cross_case_inline_skips_oversized_geojson`` - a vector GeoJSON over
  ``CASE_VIEW_INLINE_GEOJSON_MAX_BYTES`` is skipped (URI-only), not embedded.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone

import pytest

from grace2_agent.persistence import (
    CASE_VIEWS_BUCKET,
    CASE_VIEWS_PREFIX,
    Persistence,
    case_view_snapshot_key,
)
from grace2_contracts.case import (
    CaseOpenEnvelopePayload,
    CaseSummary,
)
from grace2_contracts.collections import ProjectLayerSummary
from grace2_contracts.common import new_ulid

# Reuse the in-memory MockMCPClient from the main persistence test suite (the
# file/dynamo test backend equivalent — it implements the same MCP tool surface
# Persistence calls into).
from .test_persistence import MockMCPClient


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _seed_case_with_vector_layer(
    *, owner_user_id: str | None = None,
) -> tuple[Persistence, str, str, dict]:
    """Seed a Case carrying a vector + a raster layer; return the pieces.

    Returns ``(persistence, case_id, vector_layer_id, inline_geojson)``.
    The persisted vector layer carries NO inline GeoJSON (URI-only) — that is
    the whole point: the snapshot writer must MERGE the in-memory GeoJSON in.

    ``owner_user_id`` (when given) stamps the raw ``projects`` doc's owner-link
    field via ``upsert_case`` — the snapshot writer resolves it off the raw doc
    and carries it in S3 object metadata (it is stripped from the snapshot BODY
    by ``_doc_to_case_summary``).
    """
    mock = MockMCPClient()
    p = Persistence(mock)
    case_id = new_ulid()
    vector_layer_id = new_ulid()
    raster_layer_id = new_ulid()

    vector_layer = ProjectLayerSummary(
        layer_id=vector_layer_id,
        name="Buildings (Fort Myers)",
        layer_type="vector",
        uri="s3://trid3nt-runs/abc/buildings.geojson",
        style_preset="buildings",
        visible=True,
        role="context",
        temporal=False,
    )
    raster_layer = ProjectLayerSummary(
        layer_id=raster_layer_id,
        name="Flood depth",
        layer_type="raster",
        uri="s3://trid3nt-runs/abc/flood.tif",
        style_preset="flood-depth",
        visible=True,
        role="primary",
        temporal=False,
    )
    case = CaseSummary(
        case_id=case_id,
        title="Hurricane Ian — Fort Myers flood scenario",
        created_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        status="active",
        bbox=(-82.0, 26.5, -81.8, 26.7),
        primary_hazard="flood",
        layer_summary=[vector_layer_id, raster_layer_id],
        loaded_layer_summaries=[
            vector_layer.model_dump(mode="json"),
            raster_layer.model_dump(mode="json"),
        ],
    )
    asyncio.run(p.upsert_case(case, owner_user_id=owner_user_id))

    inline_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[-81.9, 26.6], [-81.9, 26.61],
                                     [-81.89, 26.61], [-81.9, 26.6]]],
                },
                "properties": {"name": "bldg-1"},
            }
        ],
    }
    return p, case_id, vector_layer_id, inline_geojson


class _FakeS3:
    """Captures the (bucket, key, body, metadata) tuple written by a snapshot.

    The injected ``s3_put`` signature carries the owner-gate metadata dict
    (``{"owner-user-id": <owner>}`` or ``{}`` when the Case has no owner) so a
    contract test can assert the owner travels in S3 OBJECT METADATA — NEVER in
    the JSON body.
    """

    def __init__(self) -> None:
        self.bucket: str | None = None
        self.key: str | None = None
        self.body: bytes | None = None
        self.metadata: dict | None = None
        self.call_count = 0

    def put(self, bucket: str, key: str, body: bytes, metadata: dict) -> None:
        self.bucket = bucket
        self.key = key
        self.body = body
        self.metadata = metadata
        self.call_count += 1


# --------------------------------------------------------------------------- #
# Contract: byte-identical to case-open + inline vector GeoJSON
# --------------------------------------------------------------------------- #


def test_snapshot_is_byte_identical_to_case_open_payload_plus_inline_geojson() -> None:
    p, case_id, vector_layer_id, inline = _seed_case_with_vector_layer()
    fake = _FakeS3()

    ok = asyncio.run(
        p.write_case_view_snapshot(
            case_id,
            inline_geojson_by_layer_id={vector_layer_id: inline},
            s3_put=fake.put,
        )
    )
    assert ok is True
    assert fake.body is not None

    written = json.loads(fake.body.decode("utf-8"))

    # The live case-open payload the server ships on the wire.
    session_state = asyncio.run(p.get_session_state(case_id))
    live = CaseOpenEnvelopePayload(session_state=session_state).model_dump(
        mode="json"
    )

    # The snapshot must equal the live payload byte-for-byte EXCEPT the added
    # inline vector GeoJSON. Strip the additive field from the snapshot and the
    # two must be identical.
    written_layers = written["session_state"]["loaded_layers"]
    vector_entry = next(
        layer for layer in written_layers if layer["layer_id"] == vector_layer_id
    )
    # The inline GeoJSON IS present on the vector layer in the snapshot.
    assert vector_entry["inline_geojson"] == inline
    # ... and ONLY on the vector layer (the raster never gets one).
    raster_entries = [
        layer for layer in written_layers if layer["layer_id"] != vector_layer_id
    ]
    assert all("inline_geojson" not in layer for layer in raster_entries)

    # Remove the additive field -> byte-identical to the live case-open payload.
    stripped = json.loads(fake.body.decode("utf-8"))
    for layer in stripped["session_state"]["loaded_layers"]:
        layer.pop("inline_geojson", None)
    assert stripped == live


def test_snapshot_without_inline_is_exact_case_open_payload() -> None:
    p, case_id, _vector_layer_id, _inline = _seed_case_with_vector_layer()
    fake = _FakeS3()

    ok = asyncio.run(p.write_case_view_snapshot(case_id, s3_put=fake.put))
    assert ok is True

    written = json.loads(fake.body.decode("utf-8"))
    session_state = asyncio.run(p.get_session_state(case_id))
    live = CaseOpenEnvelopePayload(session_state=session_state).model_dump(
        mode="json"
    )
    # No inline side-table -> the snapshot is the unmodified case-open payload.
    assert written == live
    # No layer carries an inline_geojson field.
    for layer in written["session_state"]["loaded_layers"]:
        assert "inline_geojson" not in layer


def test_snapshot_merges_density_tag_like_emit_session_state() -> None:
    from grace2_agent.tools.vector_tiles import DensifyMeta

    p, case_id, vector_layer_id, inline = _seed_case_with_vector_layer()
    fake = _FakeS3()
    meta = DensifyMeta(
        strategy="simplified+capped",
        original_feature_count=50000,
        emitted_feature_count=8000,
        simplified=True,
        capped=True,
    )

    asyncio.run(
        p.write_case_view_snapshot(
            case_id,
            inline_geojson_by_layer_id={vector_layer_id: inline},
            density_meta_by_layer_id={vector_layer_id: meta},
            s3_put=fake.put,
        )
    )
    written = json.loads(fake.body.decode("utf-8"))
    vector_entry = next(
        layer
        for layer in written["session_state"]["loaded_layers"]
        if layer["layer_id"] == vector_layer_id
    )
    # The vector_density tag is merged EXACTLY as emit_session_state stamps it.
    assert vector_entry["vector_density"] == meta.as_wire_tag()["vector_density"]
    assert vector_entry["inline_geojson"] == inline


# --------------------------------------------------------------------------- #
# Owner-gate carrier: owner travels in S3 OBJECT METADATA, never in the body
# --------------------------------------------------------------------------- #


def test_owner_is_passed_as_s3_metadata_when_case_has_owner() -> None:
    """A Case with an owner stamps ``{"owner-user-id": <owner>}`` on the put.

    The signer reads this via ``head_object`` (cheap, no body download) to gate
    the SIGNED (12h) vs ANON (15min) TTL. S3 lowercases metadata keys, so the
    writer uses the lowercase ``owner-user-id`` key directly.
    """
    owner = new_ulid()
    p, case_id, vector_layer_id, inline = _seed_case_with_vector_layer(
        owner_user_id=owner
    )
    fake = _FakeS3()

    ok = asyncio.run(
        p.write_case_view_snapshot(
            case_id,
            inline_geojson_by_layer_id={vector_layer_id: inline},
            s3_put=fake.put,
        )
    )
    assert ok is True
    # Owner travels ONLY in object metadata.
    assert fake.metadata == {"owner-user-id": owner}


def test_owner_metadata_is_omitted_when_case_has_no_owner() -> None:
    """A Case with no owner link passes an EMPTY metadata dict (no key)."""
    p, case_id, vector_layer_id, inline = _seed_case_with_vector_layer()
    fake = _FakeS3()

    ok = asyncio.run(
        p.write_case_view_snapshot(
            case_id,
            inline_geojson_by_layer_id={vector_layer_id: inline},
            s3_put=fake.put,
        )
    )
    assert ok is True
    # No owner -> no metadata key (the production put then omits Metadata=...).
    assert fake.metadata == {}


def test_owner_lives_only_in_metadata_body_is_byte_unchanged() -> None:
    """The JSON BODY is byte-identical whether or not the Case has an owner.

    Snapshot the SAME Case twice — once with an owner stamped, once after the
    owner-link field is removed — and assert the body bytes are identical and
    the owner never appears in the body. The only difference is the metadata
    dict: present when owned, empty when not. This proves the owner lives ONLY
    in S3 object metadata.
    """
    owner = new_ulid()
    p, case_id, vector_layer_id, inline = _seed_case_with_vector_layer(
        owner_user_id=owner
    )

    owned_fake = _FakeS3()
    asyncio.run(
        p.write_case_view_snapshot(
            case_id,
            inline_geojson_by_layer_id={vector_layer_id: inline},
            s3_put=owned_fake.put,
        )
    )
    # The owner travels in metadata but NEVER in the body string.
    assert owned_fake.metadata == {"owner-user-id": owner}
    assert owner not in owned_fake.body.decode("utf-8")

    # Strip the owner link off the raw projects doc, then re-snapshot the SAME
    # Case: the body must be byte-identical, only the metadata changes to {}.
    raw_doc = p._mcp._store["projects"][case_id]  # type: ignore[attr-defined]
    raw_doc.pop("user_id", None)
    raw_doc.pop("owner_user_id", None)

    anon_fake = _FakeS3()
    asyncio.run(
        p.write_case_view_snapshot(
            case_id,
            inline_geojson_by_layer_id={vector_layer_id: inline},
            s3_put=anon_fake.put,
        )
    )
    assert anon_fake.metadata == {}
    # The owner stamp changed NOTHING in the body — byte-identical.
    assert owned_fake.body == anon_fake.body


def test_build_case_view_snapshot_is_pure_no_s3() -> None:
    """build_case_view_snapshot returns the dict without any S3 I/O."""
    p, case_id, vector_layer_id, inline = _seed_case_with_vector_layer()
    snapshot = asyncio.run(
        p.build_case_view_snapshot(
            case_id, inline_geojson_by_layer_id={vector_layer_id: inline}
        )
    )
    assert snapshot["session_state"]["case"]["case_id"] == case_id
    vector_entry = next(
        layer
        for layer in snapshot["session_state"]["loaded_layers"]
        if layer["layer_id"] == vector_layer_id
    )
    assert vector_entry["inline_geojson"] == inline


# --------------------------------------------------------------------------- #
# FIX B (job-0372): cross-case vector inline - a snapshot built for a NON-open
# Case carries inline_geojson for its vectors, resolved from the persisted
# object-store URI at write time (the browser cannot read an s3:// handle cold).
# --------------------------------------------------------------------------- #


def _seed_case_with_local_vector_artifact(
    tmp_dir: str, *, feature_count: int = 1
) -> tuple[Persistence, str, str, dict]:
    """Seed a Case whose vector layer's ``uri`` is a REAL local ``.geojson``.

    This mirrors the cross-case scenario WITHOUT S3: the persisted vector
    layer points at an object-store artifact (here a local file the snapshot
    writer's ``_read_vector_uri_as_geojson`` can read deterministically), and
    NO inline side-table is supplied - exactly what happens when the snapshot
    is built while a DIFFERENT Case is open (no live emitter holds the inline).

    Returns ``(persistence, case_id, vector_layer_id, geojson_on_disk)``.
    """
    mock = MockMCPClient()
    p = Persistence(mock)
    case_id = new_ulid()
    vector_layer_id = new_ulid()
    raster_layer_id = new_ulid()

    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [-81.9 + i * 0.001, 26.6 + i * 0.001],
                },
                "properties": {"name": f"bldg-{i}"},
            }
            for i in range(feature_count)
        ],
    }
    artifact_path = os.path.join(tmp_dir, f"{vector_layer_id}.geojson")
    with open(artifact_path, "w", encoding="utf-8") as fh:
        json.dump(geojson, fh)

    vector_layer = ProjectLayerSummary(
        layer_id=vector_layer_id,
        name="Buildings (cross-case)",
        layer_type="vector",
        uri=artifact_path,  # object-store handle the writer resolves cold
        style_preset="buildings",
        visible=True,
        role="context",
        temporal=False,
    )
    raster_layer = ProjectLayerSummary(
        layer_id=raster_layer_id,
        name="Flood depth",
        layer_type="raster",
        uri="https://d125yfbyjrpbre.cloudfront.net/cog/tiles/WebMercatorQuad/"
        "{z}/{x}/{y}.png?url=s3://trid3nt-runs/abc/flood.tif",
        style_preset="flood-depth",
        visible=True,
        role="primary",
        temporal=False,
    )
    case = CaseSummary(
        case_id=case_id,
        title="Cross-case vector inline",
        created_at=datetime(2026, 6, 22, 0, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 22, 0, 0, 0, tzinfo=timezone.utc),
        status="active",
        bbox=(-82.0, 26.5, -81.8, 26.7),
        primary_hazard="flood",
        layer_summary=[vector_layer_id, raster_layer_id],
        loaded_layer_summaries=[
            vector_layer.model_dump(mode="json"),
            raster_layer.model_dump(mode="json"),
        ],
    )
    asyncio.run(p.upsert_case(case))
    return p, case_id, vector_layer_id, geojson


def test_cross_case_snapshot_inlines_vector_from_uri_without_open_case() -> None:
    """A snapshot built with NO inline side-table still inlines its vectors.

    This is the FIX B acceptance: the snapshot is built for a Case that is NOT
    the open Case (so ``inline_geojson_by_layer_id`` is empty - the open-case
    emitter holds nothing for it). The writer must resolve the vector's inline
    GeoJSON from the persisted object-store URI at write time so the cold reopen
    paints the vector. The raster is left URI-only (its tile template renders
    cold already).
    """
    with tempfile.TemporaryDirectory() as td:
        p, case_id, vector_layer_id, geojson = _seed_case_with_local_vector_artifact(
            td
        )
        fake = _FakeS3()
        ok = asyncio.run(
            # No inline_geojson_by_layer_id at all -> the cross-case path.
            p.write_case_view_snapshot(case_id, s3_put=fake.put)
        )
        assert ok is True
        written = json.loads(fake.body.decode("utf-8"))
        layers = written["session_state"]["loaded_layers"]

        vector_entry = next(
            layer for layer in layers if layer["layer_id"] == vector_layer_id
        )
        # The vector carries NON-EMPTY inline GeoJSON resolved from its URI.
        inline = vector_entry.get("inline_geojson")
        assert isinstance(inline, dict)
        assert inline.get("type") == "FeatureCollection"
        assert len(inline.get("features", [])) == len(geojson["features"]) > 0

        # The raster stays URI-only (no inline - it renders cold via TiTiler).
        raster_entry = next(
            layer for layer in layers if layer["layer_id"] != vector_layer_id
        )
        assert "inline_geojson" not in raster_entry


def test_build_cross_case_snapshot_inlines_vector_from_uri() -> None:
    """build_case_view_snapshot (pure, no S3 put) inlines a cross-case vector."""
    with tempfile.TemporaryDirectory() as td:
        p, case_id, vector_layer_id, geojson = _seed_case_with_local_vector_artifact(
            td
        )
        snapshot = asyncio.run(p.build_case_view_snapshot(case_id))
        vector_entry = next(
            layer
            for layer in snapshot["session_state"]["loaded_layers"]
            if layer["layer_id"] == vector_layer_id
        )
        inline = vector_entry.get("inline_geojson")
        assert isinstance(inline, dict)
        assert len(inline.get("features", [])) == len(geojson["features"]) > 0


def test_explicit_inline_takes_precedence_over_uri_resolve() -> None:
    """The OPEN-case emitter's inline payload wins; the URI is not re-read.

    When ``inline_geojson_by_layer_id`` already carries the layer (the open-case
    fast path), that exact object is embedded and the cross-case URI-resolve
    pass leaves it untouched (no double-read, no override).
    """
    with tempfile.TemporaryDirectory() as td:
        p, case_id, vector_layer_id, _on_disk = _seed_case_with_local_vector_artifact(
            td, feature_count=3
        )
        # A DIFFERENT payload than what is on disk, to prove precedence.
        emitter_inline = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
                    "properties": {"name": "emitter-supplied"},
                }
            ],
        }
        snapshot = asyncio.run(
            p.build_case_view_snapshot(
                case_id,
                inline_geojson_by_layer_id={vector_layer_id: emitter_inline},
            )
        )
        vector_entry = next(
            layer
            for layer in snapshot["session_state"]["loaded_layers"]
            if layer["layer_id"] == vector_layer_id
        )
        # The emitter-supplied payload is embedded verbatim (not the 3 on disk).
        assert vector_entry["inline_geojson"] == emitter_inline


def test_cross_case_inline_skips_oversized_geojson() -> None:
    """An absurdly large vector GeoJSON is skipped + flagged, not embedded.

    Tightening the size cap to a tiny value forces the realistic artifact over
    the ceiling; the writer must leave the vector URI-only rather than balloon
    the cold snapshot past the payload norm.
    """
    import grace2_agent.persistence as persistence_mod

    with tempfile.TemporaryDirectory() as td:
        p, case_id, vector_layer_id, _g = _seed_case_with_local_vector_artifact(
            td, feature_count=5
        )
        original_cap = persistence_mod.CASE_VIEW_INLINE_GEOJSON_MAX_BYTES
        persistence_mod.CASE_VIEW_INLINE_GEOJSON_MAX_BYTES = 1  # 1 byte ceiling
        try:
            snapshot = asyncio.run(p.build_case_view_snapshot(case_id))
        finally:
            persistence_mod.CASE_VIEW_INLINE_GEOJSON_MAX_BYTES = original_cap
        vector_entry = next(
            layer
            for layer in snapshot["session_state"]["loaded_layers"]
            if layer["layer_id"] == vector_layer_id
        )
        # Over the cap -> skipped: the vector stays URI-only (no inline).
        assert "inline_geojson" not in vector_entry


# --------------------------------------------------------------------------- #
# S3 target + best-effort discipline
# --------------------------------------------------------------------------- #


def test_write_targets_runs_bucket_case_views_key() -> None:
    p, case_id, _vector_layer_id, _inline = _seed_case_with_vector_layer()
    fake = _FakeS3()
    asyncio.run(p.write_case_view_snapshot(case_id, s3_put=fake.put))

    assert fake.bucket == CASE_VIEWS_BUCKET
    assert fake.key == f"{CASE_VIEWS_PREFIX}/{case_id}.json"
    assert fake.key == case_view_snapshot_key(case_id)
    # Body is valid UTF-8 JSON (the content-type the production put stamps).
    json.loads(fake.body.decode("utf-8"))


def test_write_is_best_effort_on_s3_failure() -> None:
    p, case_id, _vector_layer_id, _inline = _seed_case_with_vector_layer()

    def _boom(bucket: str, key: str, body: bytes, metadata: dict) -> None:
        raise RuntimeError("S3 down")

    # Must NOT raise — returns False on any failure (turn-safety discipline).
    ok = asyncio.run(p.write_case_view_snapshot(case_id, s3_put=_boom))
    assert ok is False


def test_async_injected_s3_put_is_awaited() -> None:
    p, case_id, _vector_layer_id, _inline = _seed_case_with_vector_layer()
    captured: dict = {}

    async def _aput(bucket: str, key: str, body: bytes, metadata: dict) -> None:
        captured["bucket"] = bucket
        captured["key"] = key
        captured["body"] = body
        captured["metadata"] = metadata

    ok = asyncio.run(p.write_case_view_snapshot(case_id, s3_put=_aput))
    assert ok is True
    assert captured["bucket"] == CASE_VIEWS_BUCKET
    assert captured["key"] == case_view_snapshot_key(case_id)
    # No owner stamped on this Case -> empty metadata dict.
    assert captured["metadata"] == {}


def test_missing_case_writes_placeholder_snapshot() -> None:
    """A snapshot of a missing Case still writes a well-formed placeholder.

    get_session_state returns a minimal placeholder CaseSessionState for an
    unknown case_id (status="deleted"); the snapshot mirrors that so the cold
    view degrades to an empty state rather than a 404 in the writer.
    """
    mock = MockMCPClient()
    p = Persistence(mock)
    fake = _FakeS3()
    missing_id = new_ulid()
    ok = asyncio.run(p.write_case_view_snapshot(missing_id, s3_put=fake.put))
    assert ok is True
    written = json.loads(fake.body.decode("utf-8"))
    assert written["session_state"]["case"]["case_id"] == missing_id
    assert written["session_state"]["loaded_layers"] == []
