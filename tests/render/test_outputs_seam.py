"""A run's outputs come off the run RECORD, and from nowhere else.

The publish stage writes every layer it emitted onto the journal line; a reader
that names a run id reads that field. No second registry, no object-store read.
"""

from __future__ import annotations

import pytest

from trid3nt_server.render.outputs_seam import quantity_label, run_outputs
from trid3nt_server.workflows.runtime import journal

RID = "01JRUNRUNRUNRUNRUNRUNRUNRU"


@pytest.fixture
def _journal(tmp_path, monkeypatch):
    path = tmp_path / "run_journal.jsonl"
    monkeypatch.setattr(journal, "journal_path", lambda: path)
    return path


def _record(**overrides):
    base = dict(
        run_id=RID, engine="telemac", module="telemac2d", sheet=(), answer={},
        provenance=(), result=None, wall_seconds=1.0, origin="headless",
        executed=(), replayed=(), notes=(),
        outputs=[{"layer_id": "telemac-dye-1", "name": "Peak dye concentration",
                  "layer_type": "mesh", "uri": "s3://runs/RID/river.slf",
                  "quantity": "dye_concentration", "units": "mg/L"}])
    base.update(overrides)
    return journal.build_record(**base)


def test_outputs_read_off_the_record(_journal) -> None:
    journal.append_record(_record())
    published = run_outputs(RID)
    assert [row["quantity"] for row in published] == ["dye_concentration"]
    assert published[0]["uri"] == "s3://runs/RID/river.slf"


def test_a_run_nobody_journalled_is_empty(_journal) -> None:
    assert run_outputs(RID) == []
    assert run_outputs("") == []


def test_the_last_line_for_a_run_stands(_journal) -> None:
    journal.append_record(_record())
    journal.append_record(_record(outputs=[{"layer_id": "x", "name": "Bed evolution",
                                            "layer_type": "mesh", "uri": "s3://r/g.slf",
                                            "quantity": "bed_evolution"}]))
    assert [row["quantity"] for row in run_outputs(RID)] == ["bed_evolution"]


def test_the_seam_reads_no_object_store(monkeypatch, _journal) -> None:
    """The record is the ONE source: a seam that reached the store for a second
    registry would fail here rather than quietly reading one."""
    from trid3nt_server import storage

    def _refuse(*_a, **_k):
        raise AssertionError("the outputs seam reached the object store")

    monkeypatch.setattr(storage, "client", _refuse)
    monkeypatch.setattr(storage, "runs_bucket", _refuse)
    journal.append_record(_record())
    assert run_outputs(RID)[0]["layer_type"] == "mesh"


def test_the_publish_stage_writes_the_field() -> None:
    """What render publishes is what the record carries: the channel the publish
    stage writes through is the one the record is built from."""
    from trid3nt_contracts.execution import LayerURI
    from trid3nt_server.render.formats import record_run_outputs

    token = journal.bind_outputs()
    record_run_outputs([LayerURI(layer_id="a", name="Peak depth", layer_type="mesh",
                                 uri="s3://runs/R/m.slf", quantity="water_depth",
                                 units="m")])
    written = journal.drain_outputs(token)
    assert written == [{"layer_id": "a", "name": "Peak depth", "layer_type": "mesh",
                        "uri": "s3://runs/R/m.slf", "quantity": "water_depth",
                        "units": "m"}]


def test_quantity_label_says_the_quantity_out_loud() -> None:
    assert quantity_label("flood_depth") == "Flood depth"
    assert quantity_label("") == "Value"
