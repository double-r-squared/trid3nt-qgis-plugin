"""The layers a completed run put on the map: read off the run's own record.

No tool wraps this - the run journal's ``outputs`` field already carries every
frame a run published, and ``run_outputs`` is the one reader. This proves the
record alone answers "what did this run put on the map", by uri, name and
quantity, with no second registry."""

from __future__ import annotations

import pytest

from trid3nt_server.render.outputs_seam import run_outputs
from trid3nt_server.workflows.runtime import journal

RID = "run-xyz"


@pytest.fixture
def _journalled(tmp_path, monkeypatch):
    path = tmp_path / "run_journal.jsonl"
    monkeypatch.setattr(journal, "journal_path", lambda: path)

    def _install(outputs: list[dict]) -> None:
        journal.append_record(journal.build_record(
            run_id=RID, engine="telemac", module="telemac2d", sheet=(),
            provenance=(), result=None, wall_seconds=1.0, origin="headless",
            executed=(), replayed=(), notes=(), outputs=outputs))

    return _install


def _layer(quantity: str, name: str, uri: str, layer_type: str = "mesh") -> dict:
    return {"layer_id": f"telemac-{quantity}-{RID}", "name": name,
            "layer_type": layer_type, "uri": uri, "quantity": quantity,
            "units": "m"}


def test_every_published_frame_is_on_the_record(_journalled) -> None:
    _journalled([
        _layer("water_depth", "Peak water depth", "s3://b/rog.slf"),
        _layer("outlet_hydrograph", "Outlet hydrograph", "s3://b/outlet.geojson",
               layer_type="vector"),
    ])
    published = run_outputs(RID)
    assert [row["uri"] for row in published] == [
        "s3://b/rog.slf", "s3://b/outlet.geojson"]
    assert [row["quantity"] for row in published] == [
        "water_depth", "outlet_hydrograph"]


def test_a_run_with_no_record_is_an_empty_list(_journalled) -> None:
    assert run_outputs(RID) == []
