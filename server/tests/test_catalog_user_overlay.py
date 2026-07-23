"""LANE S -- user-overlay catalog merge (§F.1.2 Mode 2 offer-to-add, load seam).

The Mode 2 offer-to-add loop appends user-accepted entries to a SEPARATE
overlay file (``user_catalog.yaml``); the vendored
``public_data_source_catalog.yaml`` is never mutated. ``load_catalog`` merges
the overlay on top of the vendored catalog at load, overlay winning on id
collision, with one summary log line. A malformed overlay (bad top-level or a
bad row) degrades honestly -- the vendored catalog still loads.

Covered here:
  * append -> reload finds the new entry (cache reset on append);
  * vendored file bytes are UNTOUCHED by an append + merge;
  * overlay wins on id collision (merged value is the overlay's);
  * a malformed overlay ROW is a typed skip (good rows still merge);
  * a malformed overlay TOP-LEVEL is skipped (vendored catalog still loads);
  * a missing overlay is a no-op.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from trid3nt_server.tools.discovery import catalog_common as cc
from trid3nt_server.tools.discovery.catalog_common import (
    append_user_catalog_entry,
    load_catalog,
    reset_catalog_cache,
    user_catalog_path,
)
from trid3nt_contracts.catalog import CatalogEntry


def _entry_dict(entry_id: str, name: str, **over) -> dict:
    """A minimal VALID CatalogEntry row (credential_tier 1 -> no secret ref)."""
    row = {
        "id": entry_id,
        "name": name,
        "description": f"{name} description",
        "urls": [f"https://example.test/{entry_id}"],
        "access_tier": 3,
        "credential_tier": 1,
        "ttl_class": "semi-static-7d",
        "source_class": "user_added",
        "license": "Public Domain",
        "citation": f"{name} citation",
        "last_verified": "2026-07-01",
        "status": "active",
        "how_to_use": f"Use {name} via web_fetch.",
    }
    row.update(over)
    return row


def _make_entry(entry_id: str, name: str, **over) -> CatalogEntry:
    row = _entry_dict(entry_id, name, **over)
    row["last_verified"] = f"{row['last_verified']}T00:00:00+00:00"
    return CatalogEntry.model_validate(row)


@pytest.fixture(autouse=True)
def _overlay_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the overlay at a temp file + reset the catalog cache around each test."""
    overlay = tmp_path / "user_catalog.yaml"
    monkeypatch.setenv("TRID3NT_USER_CATALOG_YAML", str(overlay))
    reset_catalog_cache()
    try:
        yield overlay
    finally:
        reset_catalog_cache()


def _write_overlay(path: Path, rows: list) -> None:
    path.write_text(yaml.safe_dump({"entries": rows}, sort_keys=False))


# ---------------------------------------------------------------------------
# append -> reload finds it; vendored untouched.
# ---------------------------------------------------------------------------


def test_append_then_reload_finds_new_entry(_overlay_env):
    reset_catalog_cache()
    base_ids = {e.id for e in load_catalog()}
    assert "trid3nt-user-added-xyz" not in base_ids

    append_user_catalog_entry(
        _make_entry("trid3nt-user-added-xyz", "Trid3nt User Added XYZ")
    )
    # append resets the cache -> next load rebuilds with the overlay merged in.
    merged_ids = {e.id for e in load_catalog()}
    assert "trid3nt-user-added-xyz" in merged_ids
    # every vendored entry survives.
    assert base_ids <= merged_ids


def test_append_does_not_mutate_vendored_catalog(_overlay_env):
    vendored = cc.CATALOG_YAML_PATH
    before = vendored.read_bytes()
    append_user_catalog_entry(_make_entry("trid3nt-vendored-guard", "Guard"))
    load_catalog()  # forces the merge path
    assert vendored.read_bytes() == before, "vendored catalog must never be mutated"
    # And the overlay file (NOT the vendored file) is where it landed.
    assert user_catalog_path().exists()
    assert user_catalog_path() != vendored


# ---------------------------------------------------------------------------
# overlay wins on id collision.
# ---------------------------------------------------------------------------


def test_overlay_wins_on_id_collision(_overlay_env):
    reset_catalog_cache()
    base = load_catalog()
    # pick a real vendored id to collide with.
    collide_id = "fema-nfhl-flood-zones"
    assert any(e.id == collide_id for e in base), "expected vendored id present"

    _write_overlay(
        _overlay_env,
        [_entry_dict(collide_id, "OVERRIDDEN BY USER OVERLAY")],
    )
    reset_catalog_cache()
    merged = load_catalog()
    hit = [e for e in merged if e.id == collide_id]
    assert len(hit) == 1, "collision must not duplicate the id"
    assert hit[0].name == "OVERRIDDEN BY USER OVERLAY", "overlay value must win"
    # count unchanged (override, not a new row).
    assert len(merged) == len(base)


# ---------------------------------------------------------------------------
# malformed overlay degrades honestly.
# ---------------------------------------------------------------------------


def test_malformed_overlay_row_is_typed_skip(_overlay_env):
    reset_catalog_cache()
    base_ids = {e.id for e in load_catalog()}
    _write_overlay(
        _overlay_env,
        [
            _entry_dict("good-overlay-entry", "Good Overlay Entry"),
            {"id": "bad-overlay-entry", "name": "missing required fields"},
        ],
    )
    reset_catalog_cache()
    merged_ids = {e.id for e in load_catalog()}
    assert "good-overlay-entry" in merged_ids, "the valid row still merges"
    assert "bad-overlay-entry" not in merged_ids, "the invalid row is skipped"
    assert base_ids <= merged_ids, "vendored catalog still loads"


def test_malformed_overlay_top_level_skipped(_overlay_env):
    reset_catalog_cache()
    base_ids = {e.id for e in load_catalog()}
    # A bare list at the top level (not a {entries: [...]} mapping).
    _overlay_env.write_text(yaml.safe_dump([1, 2, 3]))
    reset_catalog_cache()
    merged_ids = {e.id for e in load_catalog()}
    assert merged_ids == base_ids, "malformed overlay is a no-op; vendored intact"


def test_missing_overlay_is_noop(_overlay_env):
    assert not _overlay_env.exists()
    reset_catalog_cache()
    base_ids = {e.id for e in load_catalog()}
    reset_catalog_cache()
    again = {e.id for e in load_catalog()}
    assert again == base_ids
