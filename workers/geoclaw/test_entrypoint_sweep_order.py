"""Sweep-order regression test (fix for the rebuild smoke finding,
run 01KZWT7J3T0V95E8HF0E5S8XHF).

The bug: ``main()`` ran the ``_expand_outputs`` upload sweep BEFORE
``run_geoclaw_postprocess`` had written the peak/frame COGs into scratch, so
the in-image ``publish_manifest.json`` referenced ``cog_uris`` that were never
uploaded. ``workers/sfincs/entrypoint.py`` has the correct order
(postprocess writes COGs into ``run_dir`` BEFORE the sweep glob runs) -- this
test pins the fixed geoclaw order the same way: postprocess -> sweep -> reap,
with the reap strictly inside the ``status == "ok"`` branch and running AFTER
the sweep so freshly-swept COGs are never reaped.

Fully mocked / offline: no real GeoClaw binary, no S3/GCS, no rasterio.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from workers.geoclaw import entrypoint as ep


def _fake_deck_manifest():
    return SimpleNamespace(scenario="test_scenario", files_written=["setrun.py"], driver_descriptor="offshore")


def test_postprocess_runs_before_sweep_and_reap_runs_after(tmp_path, monkeypatch) -> None:
    """Pin the call sequence: run_geoclaw_postprocess -> _expand_outputs -> reap_run_scratch."""
    calls: list[str] = []

    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    monkeypatch.setattr(ep, "_read_manifest", lambda uri: {"inputs": [], "outputs": ep.DEFAULT_OUTPUT_GLOBS})
    monkeypatch.setattr(ep, "_prepare_scratch", lambda: scratch_dir)
    monkeypatch.setattr(ep, "_download", lambda uri, dest: None)
    monkeypatch.setattr(ep, "_normalize_topo_files", lambda scratch, build_spec: None)
    monkeypatch.setattr(ep, "_generate_fgmax_mask", lambda scratch, build_spec: None)
    monkeypatch.setattr(ep, "_author_deck", lambda build_spec, cwd: _fake_deck_manifest())
    monkeypatch.setattr(
        ep, "_run_geoclaw",
        lambda cwd, bouss=False: (0, tmp_path / "stdout.log", tmp_path / "stderr.log"),
    )
    (tmp_path / "stdout.log").write_text("ok")
    (tmp_path / "stderr.log").write_text("")
    monkeypatch.setattr(ep, "_upload", lambda src, uri: uri)
    monkeypatch.setattr(ep, "_write_completion", lambda **kw: "completion-uri")

    def _fake_expand_outputs(patterns, cwd):
        calls.append("sweep")
        # Prove the sweep runs AFTER postprocess by requiring the COG the
        # (mocked) postprocess step would have written to already be
        # "present" -- i.e. postprocess must have already run.
        assert "postprocess" in calls
        return [scratch_dir / "peak_depth.tif"]

    monkeypatch.setattr(ep, "_expand_outputs", _fake_expand_outputs)
    (scratch_dir / "peak_depth.tif").write_bytes(b"fake-cog")

    fake_manifest = {"cog_uris": {"peak_depth": "s3://bucket/runs/rid/peak_depth.tif"}}

    def _fake_run_geoclaw_postprocess(*, run_id, scratch, build_spec, runs_uri_for):
        calls.append("postprocess")
        return SimpleNamespace(
            status="ok", manifest=fake_manifest,
            error_code=None, error_message=None,
        )

    def _fake_reap_run_scratch(delete_fn, run_prefix, relative_keys, patterns, keep_patterns=()):
        calls.append("reap")
        # Reap must see the sweep's output_rels (i.e. run AFTER the sweep).
        assert "sweep" in calls
        # Belt-and-suspenders: the reap must never be asked to delete the
        # freshly-swept COG regardless of pattern content.
        assert "peak_depth.tif" not in relative_keys or not any(
            __import__("fnmatch").fnmatch("peak_depth.tif", p) for p in patterns
        )
        return {"deleted": [], "errors": []}

    postprocess_mod = mock.MagicMock()
    postprocess_mod.run_geoclaw_postprocess = _fake_run_geoclaw_postprocess
    postprocess_mod.GEOCLAW_SCRATCH_KEEP_PATTERNS = ("_output/gauge*.txt",)
    postprocess_mod.GEOCLAW_SCRATCH_PATTERNS = (
        "_output/fort.q*", "_output/fort.t*", "_output/fort.b*", "_output/fort.a*", "_output/*.data",
    )
    retention_mod = mock.MagicMock()
    retention_mod.reap_run_scratch = _fake_reap_run_scratch

    monkeypatch.setitem(
        __import__("sys").modules, "workers._geoclaw_postprocess", postprocess_mod,
    )
    monkeypatch.setitem(
        __import__("sys").modules, "workers._raster_postprocess.retention", retention_mod,
    )
    monkeypatch.setattr(ep, "_write_publish_manifest", lambda run_id, pp_manifest: "s3://bucket/runs/rid/publish_manifest.json")

    rc = ep.main(["--run-id", "rid", "--manifest-uri", "s3://bucket/manifest.json"])

    assert rc == 0
    assert calls == ["postprocess", "sweep", "reap"], (
        f"expected postprocess -> sweep -> reap, got {calls}"
    )


def test_reap_never_runs_when_postprocess_gate_fails(tmp_path, monkeypatch) -> None:
    """Postprocess honesty-gate failure (status='error') must skip the reap
    entirely -- the run keeps its raw scratch for debugging."""
    calls: list[str] = []
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    monkeypatch.setattr(ep, "_read_manifest", lambda uri: {"inputs": [], "outputs": ep.DEFAULT_OUTPUT_GLOBS})
    monkeypatch.setattr(ep, "_prepare_scratch", lambda: scratch_dir)
    monkeypatch.setattr(ep, "_download", lambda uri, dest: None)
    monkeypatch.setattr(ep, "_normalize_topo_files", lambda scratch, build_spec: None)
    monkeypatch.setattr(ep, "_generate_fgmax_mask", lambda scratch, build_spec: None)
    monkeypatch.setattr(ep, "_author_deck", lambda build_spec, cwd: _fake_deck_manifest())
    monkeypatch.setattr(
        ep, "_run_geoclaw",
        lambda cwd, bouss=False: (0, tmp_path / "stdout.log", tmp_path / "stderr.log"),
    )
    (tmp_path / "stdout.log").write_text("ok")
    (tmp_path / "stderr.log").write_text("")
    monkeypatch.setattr(ep, "_upload", lambda src, uri: uri)
    monkeypatch.setattr(ep, "_write_completion", lambda **kw: "completion-uri")
    monkeypatch.setattr(ep, "_expand_outputs", lambda patterns, cwd: (calls.append("sweep"), [])[1])

    def _fake_run_geoclaw_postprocess(*, run_id, scratch, build_spec, runs_uri_for):
        calls.append("postprocess")
        return SimpleNamespace(
            status="error", manifest=None,
            error_code="GEOCLAW_OUTPUT_EMPTY", error_message="no frames",
        )

    postprocess_mod = mock.MagicMock()
    postprocess_mod.run_geoclaw_postprocess = _fake_run_geoclaw_postprocess
    postprocess_mod.GEOCLAW_SCRATCH_KEEP_PATTERNS = ()
    postprocess_mod.GEOCLAW_SCRATCH_PATTERNS = ()
    retention_mod = mock.MagicMock()

    def _fail_if_called(*a, **kw):
        raise AssertionError("reap_run_scratch must not be called on a failed postprocess gate")

    retention_mod.reap_run_scratch = _fail_if_called

    monkeypatch.setitem(
        __import__("sys").modules, "workers._geoclaw_postprocess", postprocess_mod,
    )
    monkeypatch.setitem(
        __import__("sys").modules, "workers._raster_postprocess.retention", retention_mod,
    )

    rc = ep.main(["--run-id", "rid", "--manifest-uri", "s3://bucket/manifest.json"])

    assert rc == 0  # exit_code tracks the solver rc, not the postprocess gate
    assert calls == ["postprocess", "sweep"]
