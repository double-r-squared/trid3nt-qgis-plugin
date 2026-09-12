"""``list_run_frames``: the uris of the layers a completed run put on the map.

The RUN RECORD is the one source - the publish stage writes every emitted layer
onto the journal line. No match is an honest empty result with a typed reason,
never a fabricated list."""

from __future__ import annotations

import pytest

from trid3nt_server.tools.meta.list_run_frames.list_run_frames import (
    ListRunFramesError,
    list_run_frames,
)
from trid3nt_server.workflows.runtime import journal

RID = "run-xyz"


@pytest.fixture
def _journalled(tmp_path, monkeypatch):
    """Write one run's record to a journal nothing else reads."""
    path = tmp_path / "run_journal.jsonl"
    monkeypatch.setattr(journal, "journal_path", lambda: path)

    def _install(outputs: list[dict] | None) -> None:
        if outputs is None:
            return
        journal.append_record(journal.build_record(
            run_id=RID, engine="telemac", module="telemac2d", sheet=(), answer={},
            provenance=(), result=None, wall_seconds=1.0, origin="headless",
            executed=(), replayed=(), notes=(), outputs=outputs))

    return _install


def _layer(quantity: str, name: str, uri: str, layer_type: str = "mesh") -> dict:
    return {"layer_id": f"telemac-{quantity}-{RID}", "name": name,
            "layer_type": layer_type, "uri": uri, "quantity": quantity,
            "units": "m"}


def test_every_recorded_output_is_listed(_journalled) -> None:
    _journalled([
        _layer("water_depth", "Peak water depth", "s3://b/rog.slf"),
        _layer("outlet_hydrograph", "Outlet hydrograph", "s3://b/outlet.geojson",
               layer_type="vector"),
    ])
    out = list_run_frames(RID)
    assert out["output_count"] == 2
    assert [row["uri"] for row in out["outputs"]] == [
        "s3://b/rog.slf", "s3://b/outlet.geojson"]
    assert "reason" not in out


def test_matches_on_the_physical_quantity(_journalled) -> None:
    _journalled([_layer("water_depth", "Peak water depth", "s3://b/rog.slf"),
                 _layer("bed_evolution", "Bed evolution", "s3://b/gaia.slf")])
    out = list_run_frames(RID, layer="water depth")
    assert [row["quantity"] for row in out["outputs"]] == ["water_depth"]


def test_matches_on_the_layer_name(_journalled) -> None:
    _journalled([_layer("bed_evolution", "Bed evolution", "s3://b/gaia.slf")])
    assert list_run_frames(RID, layer="Bed evolution")["output_count"] == 1


def test_no_record_is_an_honest_empty(_journalled) -> None:
    _journalled(None)
    out = list_run_frames(RID)
    assert out["output_count"] == 0 and out["outputs"] == []
    assert "no record" in out["reason"]


def test_no_match_names_what_the_run_did_publish(_journalled) -> None:
    _journalled([_layer("bed_evolution", "Bed evolution", "s3://b/gaia.slf")])
    out = list_run_frames(RID, layer="lightning")
    assert out["output_count"] == 0
    assert "1 layer(s)" in out["reason"] and "lightning" in out["reason"]


def test_a_missing_run_id_refuses_typed() -> None:
    with pytest.raises(ListRunFramesError) as ei:
        list_run_frames("")
    assert ei.value.error_code == "MISSING_RUN_ID"


def test_the_tool_reads_no_object_store(monkeypatch, _journalled) -> None:
    """The record is the one registry: a reader that reached the store for a
    second one would fail here."""
    from trid3nt_server import storage

    def _refuse(*_a, **_k):
        raise AssertionError("list_run_frames reached the object store")

    monkeypatch.setattr(storage, "client", _refuse)
    monkeypatch.setattr(storage, "runs_bucket", _refuse)
    _journalled([_layer("water_depth", "Peak water depth", "s3://b/rog.slf")])
    assert list_run_frames(RID)["output_count"] == 1
