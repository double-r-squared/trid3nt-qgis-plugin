"""A layer's notes reach the model's tool-response payload whole."""

from __future__ import annotations

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.adapters.adapter import summarize_tool_result


def _layer(notes: list[str]) -> LayerURI:
    return LayerURI(
        layer_id="dem-1",
        name="Elevation",
        layer_type="raster",
        uri="s3://bucket/dem.tif",
        notes=notes,
    )


def test_a_fallback_note_reaches_the_model_payload():
    note = "3DEP had no tile here; Copernicus GLO-30 painted the whole extent. " * 4
    payload = summarize_tool_result("fetch_dem", _layer([note, "second note"]))
    assert payload["status"] == "ok"
    assert payload["notes"] == [note, "second note"]


def test_a_layer_without_notes_adds_no_key():
    payload = summarize_tool_result("fetch_dem", _layer([]))
    assert "notes" not in payload
