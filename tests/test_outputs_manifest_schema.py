"""Emit-on-solve ``outputs.json`` schema: writer + tolerant reader.

Pins the frozen schema (docs/design/outputs-manifest-schema.md):
1. Flat entry contract ``{kind, quantity, name, uri, t?, units?}`` -- t/units
   omitted (absent, not null) when unset; unknown kind rejected at write time.
2. Safe-append semantics: whole-array read-modify-write, immutable prior
   entries, order preserved, first-frame bootstrap.
3. Version marker: writer stamps schema_version; reader hard-rejects a missing
   / unknown version and a foreign kind (the completion-only fallback trigger).
4. Writer -> reader round trip.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_contracts import outputs_manifest as om


def test_build_entry_flat_and_omits_absent_optionals():
    e = om.build_entry(
        kind="raster", quantity="flood_depth", name="Flood depth step 7",
        uri="s3://runs/x/flood_depth_frame_07.tif", t=1800.0, units="meters",
    )
    assert e == {
        "kind": "raster",
        "quantity": "flood_depth",
        "name": "Flood depth step 7",
        "uri": "s3://runs/x/flood_depth_frame_07.tif",
        "t": 1800.0,
        "units": "meters",
    }
    bare = om.build_entry(
        kind="raster", quantity="flood_depth", name="Peak flood depth",
        uri="s3://runs/x/flood_depth_peak.tif",
    )
    assert "t" not in bare and "units" not in bare


def test_build_entry_rejects_unknown_kind_and_missing_fields():
    with pytest.raises(ValueError):
        om.build_entry(kind="heatmap", quantity="q", name="n", uri="s3://x")
    with pytest.raises(ValueError):
        om.build_entry(kind="raster", quantity="", name="n", uri="s3://x")
    with pytest.raises(ValueError):
        om.build_entry(kind="raster", quantity="q", name="n", uri="")


def test_append_bootstraps_then_grows_whole_array_in_order():
    e1 = om.build_entry(kind="raster", quantity="flood_depth", name="s1",
                        uri="s3://x/1.tif", t=0.0)
    e2 = om.build_entry(kind="raster", quantity="flood_depth", name="s2",
                        uri="s3://x/2.tif", t=600.0)
    first = om.append_entries(None, engine="sfincs", run_id="R", new=[e1])
    d1 = json.loads(first)
    assert d1["schema_version"] == om.OUTPUTS_MANIFEST_SCHEMA_VERSION
    assert d1["engine"] == "sfincs" and d1["run_id"] == "R"
    assert [x["uri"] for x in d1["entries"]] == ["s3://x/1.tif"]

    second = om.append_entries(first, engine="sfincs", run_id="R", new=[e2])
    d2 = json.loads(second)
    # Prior entry immutable + order preserved; array strictly grows.
    assert [x["uri"] for x in d2["entries"]] == ["s3://x/1.tif", "s3://x/2.tif"]
    assert d2["entries"][0] == d1["entries"][0]


def test_append_rejects_foreign_schema_version():
    foreign = json.dumps({"schema_version": 999, "engine": "sfincs",
                          "run_id": "R", "entries": []})
    with pytest.raises(ValueError):
        om.append_entries(foreign, engine="sfincs", run_id="R", new=[])


def test_reader_rejects_missing_and_unknown_version():
    with pytest.raises(ValueError):
        om.parse_outputs_manifest(json.dumps({"engine": "sfincs", "entries": []}))
    with pytest.raises(ValueError):
        om.parse_outputs_manifest(
            json.dumps({"schema_version": 2, "entries": []})
        )
    with pytest.raises(ValueError):
        om.parse_outputs_manifest("[]")  # non-dict body


def test_reader_rejects_foreign_kind():
    body = json.dumps({
        "schema_version": 1, "engine": "sfincs", "run_id": "R",
        "entries": [{"kind": "heatmap", "quantity": "q", "name": "n",
                     "uri": "s3://x"}],
    })
    with pytest.raises(ValueError):
        om.parse_outputs_manifest(body)


def test_reader_ignores_additive_keys():
    body = json.dumps({
        "schema_version": 1, "engine": "sfincs", "run_id": "R",
        "future_top_key": 1,
        "entries": [{"kind": "raster", "quantity": "flood_depth",
                     "name": "n", "uri": "s3://x", "t": 5.0,
                     "future_entry_key": "ok"}],
    })
    m = om.parse_outputs_manifest(body)
    assert m.entries[0].quantity == "flood_depth" and m.entries[0].t == 5.0


def test_writer_reader_round_trip():
    entries = [
        om.build_entry(kind="raster", quantity="flood_depth", name="Peak",
                       uri="s3://x/peak.tif"),
        om.build_entry(kind="raster", quantity="flood_depth", name="step 1",
                       uri="s3://x/1.tif", t=0.0, units="meters"),
        om.build_entry(kind="mesh", quantity="water_surface",
                       name="Mesh", uri="s3://x/r2d.slf"),
    ]
    text = om.append_entries(None, engine="sfincs", run_id="R", new=entries)
    m = om.parse_outputs_manifest(text)
    assert m.schema_version == 1 and len(m.entries) == 3
    assert m.entries[0].t is None and m.entries[1].units == "meters"
    assert m.entries[2].kind == "mesh"
