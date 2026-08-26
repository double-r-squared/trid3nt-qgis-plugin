"""stage_manifest carries the reach's STAGED INPUTS, and declares no bed COG.

The worker used to fetch its own bed and write a node-sampled COG beside the
result so the composer had something to publish; the manifest had to name that
file or the supervisor's glob-upload never uploaded it. Both halves of that are
gone. The bed arrives as a staged input the launcher walks into the run
directory, the emit-on-fetch seam surfaces the CONTINUOUS source raster, and a
declared output nothing writes would be a name with no file behind it.

What replaces the old pins is the staging contract itself: an ``inputs`` row per
staged artifact, for the solve path and the mesh preview alike.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.workflows.telemac.steps.deck import stage_manifest


class _FakeS3:
    def __init__(self) -> None:
        self.put: dict | None = None

    def put_object(self, **kw):  # noqa: ANN001
        self.put = kw


def _stage(monkeypatch, reach: dict, *, mesh_only: bool = False,
           inputs: list[dict[str, str]] | None = None) -> dict:
    import trid3nt_server.workflows.solver.solver as solver_mod

    fake = _FakeS3()
    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: fake)
    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "test-cache")
    stage_manifest(reach, "RUNTAG", mesh_only=mesh_only, inputs=inputs)
    assert fake.put is not None
    return json.loads(fake.put["Body"])


_SOLVE_INPUTS = [
    {"gs_uri": "s3://cache/telemac/RUNTAG/river_centerline.geojson",
     "dest": "river_centerline.geojson"},
    {"gs_uri": "s3://cache/telemac/RUNTAG/river_banks.geojson",
     "dest": "river_banks.geojson"},
    {"gs_uri": "s3://cache/copernicus/bed.tif", "dest": "bed_source.tif"},
]


@pytest.mark.parametrize("reach", [
    {"name": "test-reach"},
    {"name": "test-reach", "substance_class": "do_sag"},
    {"name": "test-reach", "substance_class": "sediment"},
])
def test_every_reach_class_stages_the_same_three_inputs(monkeypatch, reach):
    """The centerline, the banks and the bed ride whatever the substance is.

    They are the GEOMETRY the reach is meshed on, so a dye run, a DO-sag run and
    a GAIA sediment run all need the same three files - which is why they are
    staged by the deck writer rather than by a per-class branch.
    """
    manifest = _stage(monkeypatch, reach, inputs=_SOLVE_INPUTS)
    assert [row["dest"] for row in manifest["inputs"]] == [
        "river_centerline.geojson", "river_banks.geojson", "bed_source.tif"]


def test_no_reach_run_declares_a_bed_cog_output(monkeypatch):
    """The node-lattice bed COG is dead; nothing may name it as an output."""
    for reach in ({"name": "r"}, {"name": "r", "substance_class": "do_sag"},
                  {"name": "r", "substance_class": "sediment"}):
        manifest = _stage(monkeypatch, reach, inputs=_SOLVE_INPUTS)
        assert "bed_bathymetry.tif" not in manifest["outputs"]


def test_sediment_keeps_its_gaia_outputs(monkeypatch):
    manifest = _stage(monkeypatch, {"name": "r", "substance_class": "sediment"},
                      inputs=_SOLVE_INPUTS)
    assert "gaia_river.slf" in manifest["outputs"]
    assert "gaia_river.cas" in manifest["outputs"]


def test_mesh_only_stages_geometry_but_no_bed(monkeypatch):
    """A preview meshes and stops, so it is staged with the two geometry files
    and no bed - a raster it never samples is a fetch nobody asked for."""
    manifest = _stage(monkeypatch, {"name": "r"}, mesh_only=True,
                      inputs=_SOLVE_INPUTS[:2])
    assert [row["dest"] for row in manifest["inputs"]] == [
        "river_centerline.geojson", "river_banks.geojson"]
    assert manifest["mesh_only"] is True


def test_an_unstaged_manifest_carries_an_empty_inputs_list(monkeypatch):
    """The key is always present: the worker's contract reads it unconditionally."""
    manifest = _stage(monkeypatch, {"name": "r"})
    assert manifest["inputs"] == []


def test_stage_manifest_requires_cache_bucket(monkeypatch):
    import trid3nt_server.workflows.solver.solver as solver_mod

    from trid3nt_server.workflows.telemac.steps.errors import TelemacDyeScenarioError

    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: _FakeS3())
    monkeypatch.delenv("TRID3NT_CACHE_BUCKET", raising=False)
    with pytest.raises(TelemacDyeScenarioError):
        stage_manifest({"name": "test-reach"}, "RUNTAG")
