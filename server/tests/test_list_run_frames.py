"""Tests for ``list_run_frames`` (sandbox-staging).

``list_run_frames(run_id, layer)`` reads a completed run's
``publish_manifest.json`` (the SAME schema-gated reader the register-only fast
path uses) and returns the ORDERED animation-frame COG URIs for the requested
layer — the list the agent hands to ``code_exec_request`` as a multi-frame
``layer_refs`` entry.

Coverage:
  - frames are returned ordered by frame_no, aggregate (frame_no=None) excluded.
  - layer filtering matches on the web grouping name / layer_id_stem.
  - honest empty result when no manifest (None) — never a fabricated list.
  - honest empty result when no matching frame layers (+ a typed reason).
  - missing run_id raises the typed error.

No network: the manifest reader's S3 helpers are monkeypatched.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.meta.list_run_frames import (
    ListRunFramesError,
    list_run_frames,
)


def _manifest_json(layers: list[dict]) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "engine": "sfincs",
            "run_id": "run-xyz",
            "status": "ok",
            "frame_count": len([l for l in layers if l.get("frame_no") is not None]),
            "metrics": {},
            "layers": layers,
        }
    )


def _frame_layer(stem: str, name: str, frame_no, cog_uri: str) -> dict:
    return {
        "layer_id_stem": stem,
        "name": name,
        "style_preset": "flood_depth",
        "cog_uri": cog_uri,
        "frame_no": frame_no,
    }


@pytest.fixture
def _patch_manifest(monkeypatch):
    """Patch the solver S3 helpers so ``read_publish_manifest`` resolves a
    manifest from an in-memory body (no network)."""

    def _install(manifest_text: str | None):
        from trid3nt_server.tools.simulation import solver

        monkeypatch.setattr(solver, "_get_runs_bucket", lambda: "runs-bucket")
        if manifest_text is None:
            # No completion.json -> read_publish_manifest returns None.
            monkeypatch.setattr(
                solver, "_try_get_completion_s3", lambda b, r: None
            )
        else:
            monkeypatch.setattr(
                solver,
                "_try_get_completion_s3",
                lambda b, r: {"publish_manifest_uri": "s3://runs-bucket/run-xyz/publish_manifest.json"},
            )
            monkeypatch.setattr(
                solver, "_read_object_bytes", lambda uri: manifest_text.encode()
            )

    return _install


def test_frames_returned_ordered_by_frame_no(_patch_manifest) -> None:
    """Frame layers are returned ordered by frame_no; the aggregate peak layer
    (frame_no=None) is excluded."""
    layers = [
        _frame_layer("flood-peak", "Peak flood depth", None, "s3://b/peak.tif"),
        _frame_layer("flood-step", "Flood depth step 2", 2, "s3://b/f2.tif"),
        _frame_layer("flood-step", "Flood depth step 0", 0, "s3://b/f0.tif"),
        _frame_layer("flood-step", "Flood depth step 1", 1, "s3://b/f1.tif"),
    ]
    _patch_manifest(_manifest_json(layers))

    out = list_run_frames("run-xyz", layer="flood depth")
    assert out["frame_count"] == 3
    # Ordered by frame_no, peak excluded.
    assert out["frame_uris"] == ["s3://b/f0.tif", "s3://b/f1.tif", "s3://b/f2.tif"]
    assert [f["frame_no"] for f in out["frames"]] == [0, 1, 2]
    assert "reason" not in out


def test_layer_filter_excludes_non_matching(_patch_manifest) -> None:
    """Only layers matching the requested layer name are returned."""
    layers = [
        _frame_layer("flood-step", "Flood depth step 0", 0, "s3://b/flood0.tif"),
        _frame_layer("wave-step", "Wave height step 0", 0, "s3://b/wave0.tif"),
    ]
    _patch_manifest(_manifest_json(layers))

    out = list_run_frames("run-xyz", layer="flood_depth")
    assert out["frame_uris"] == ["s3://b/flood0.tif"]


def test_blank_layer_lists_all_frames(_patch_manifest) -> None:
    """An empty ``layer`` lists ALL frame layers regardless of name."""
    layers = [
        _frame_layer("flood-step", "Flood depth step 0", 0, "s3://b/flood0.tif"),
        _frame_layer("wave-step", "Wave height step 0", 1, "s3://b/wave1.tif"),
    ]
    _patch_manifest(_manifest_json(layers))

    out = list_run_frames("run-xyz", layer="")
    assert set(out["frame_uris"]) == {"s3://b/flood0.tif", "s3://b/wave1.tif"}
    assert out["frame_count"] == 2


def test_no_manifest_returns_honest_empty(_patch_manifest) -> None:
    """No manifest -> honest empty result (frame_count 0 + a reason), NOT a crash
    and NOT a fabricated list."""
    _patch_manifest(None)
    out = list_run_frames("run-xyz", layer="flood_depth")
    assert out["frame_count"] == 0
    assert out["frame_uris"] == []
    assert "reason" in out and "no publish_manifest" in out["reason"]


def test_no_matching_frames_returns_honest_empty(_patch_manifest) -> None:
    """A manifest with no matching frame layer -> honest empty result + reason."""
    layers = [_frame_layer("wave-step", "Wave height step 0", 0, "s3://b/wave0.tif")]
    _patch_manifest(_manifest_json(layers))
    out = list_run_frames("run-xyz", layer="flood_depth")
    assert out["frame_count"] == 0
    assert out["frame_uris"] == []
    assert "reason" in out


def test_missing_run_id_raises() -> None:
    """A blank run_id raises the typed error (FR-AS-11)."""
    with pytest.raises(ListRunFramesError) as exc:
        list_run_frames("")
    assert exc.value.error_code == "MISSING_RUN_ID"


def test_list_run_frames_is_registered() -> None:
    """The tool is wired into the registry (import-time @register_tool)."""
    import trid3nt_server.tools as tools

    assert "list_run_frames" in tools.TOOL_REGISTRY


def test_frame_uris_feed_code_exec_multiframe_contract(_patch_manifest) -> None:
    """The frame_uris list is exactly a valid multi-frame layer_refs value for
    code_exec_request (the contract round-trips it)."""
    from trid3nt_contracts import new_ulid
    from trid3nt_contracts.sandbox_contracts import CodeExecRequestPayload

    layers = [
        _frame_layer("flood-step", "Flood depth step 0", 0, "s3://b/f0.tif"),
        _frame_layer("flood-step", "Flood depth step 1", 1, "s3://b/f1.tif"),
    ]
    _patch_manifest(_manifest_json(layers))
    out = list_run_frames("run-xyz", layer="flood_depth")

    payload = CodeExecRequestPayload(
        code_exec_id=new_ulid(),
        python_code="result = len(frames)",
        layer_refs={"frames": out["frame_uris"]},
    )
    assert payload.layer_refs["frames"] == ["s3://b/f0.tif", "s3://b/f1.tif"]
