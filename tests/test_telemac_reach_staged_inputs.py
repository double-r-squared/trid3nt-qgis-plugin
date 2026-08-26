"""stage_manifest declares the in-worker bed COG for the river_dye/do_sag deck path.

Regression for the dead-COG 404: the worker ALWAYS attempts to write
bed_bathymetry.tif (best-effort) for a non-mesh_only solve and records it as
metrics.bed_cog, but stage_manifest's outputs list never named the file, so the
local-docker supervisor's glob-upload never uploaded it -- the context layer
published from that record 404s. Covers both templates that ride
write_reach_deck -> solve_reach -> stage_manifest: river_dye (plain reach) and
do_sag (substance_class="do_sag" reach).
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


def _stage(monkeypatch, reach: dict, *, mesh_only: bool = False) -> dict:
    import trid3nt_server.workflows.solver.solver as solver_mod

    fake = _FakeS3()
    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: fake)
    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "test-cache")
    stage_manifest(reach, "RUNTAG", mesh_only=mesh_only)
    assert fake.put is not None
    return json.loads(fake.put["Body"])


def test_river_dye_manifest_declares_bed_cog(monkeypatch):
    """A plain river_dye reach (no substance_class) still ships bed_bathymetry.tif
    -- the worker's DEM-bed step (and its best-effort COG write) runs regardless
    of substance."""
    manifest = _stage(monkeypatch, {"name": "test-reach"})
    assert "bed_bathymetry.tif" in manifest["outputs"]


def test_do_sag_manifest_declares_bed_cog(monkeypatch):
    """The do_sag deck sets substance_class="do_sag" (via _do_sag_block); the bed
    COG must still be declared -- it is not the sediment-only gaia_river.slf case."""
    manifest = _stage(monkeypatch, {"name": "test-reach", "substance_class": "do_sag"})
    assert "bed_bathymetry.tif" in manifest["outputs"]


def test_sediment_manifest_declares_bed_cog_alongside_gaia(monkeypatch):
    """A sediment run keeps BOTH the gaia deposition outputs and the bed COG."""
    manifest = _stage(monkeypatch, {"name": "test-reach", "substance_class": "sediment"})
    assert "bed_bathymetry.tif" in manifest["outputs"]
    assert "gaia_river.slf" in manifest["outputs"]
    assert "gaia_river.cas" in manifest["outputs"]


def test_mesh_only_manifest_omits_bed_cog(monkeypatch):
    """mesh_only returns before the worker ever fetches the DEM bed (step 4 in
    entrypoint.py) -- the file is never written, so it must not be declared
    (a glob-listed-but-never-written file is harmless, but this pins the
    intentional mesh_only output set)."""
    manifest = _stage(monkeypatch, {"name": "test-reach"}, mesh_only=True)
    assert "bed_bathymetry.tif" not in manifest["outputs"]


def test_stage_manifest_requires_cache_bucket(monkeypatch):
    import trid3nt_server.workflows.solver.solver as solver_mod

    from trid3nt_server.workflows.telemac.steps.errors import TelemacDyeScenarioError

    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: _FakeS3())
    monkeypatch.delenv("TRID3NT_CACHE_BUCKET", raising=False)
    with pytest.raises(TelemacDyeScenarioError):
        stage_manifest({"name": "test-reach"}, "RUNTAG")
