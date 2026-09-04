"""Tests for ``list_run_frames`` (sandbox-staging).

``list_run_frames(run_id, layer)`` returns the ORDERED animation-frame COG URIs
for a completed run's layer -- the list the agent hands to ``code_exec_request``
as a multi-frame ``layer_refs`` entry. It reads the emit-on-solve
``outputs.json`` FIRST (frames = raster entries carrying a physical ``t``) and
falls back to a LEGACY run's ``publish_manifest.json`` frame layers (ordered by
``frame_no``) only when no outputs manifest is readable.

Coverage:
  - outputs.json frames ordered by t, the non-temporal peak excluded.
  - outputs.json wins over a legacy publish_manifest when both are present.
  - legacy publish_manifest frames still served, ordered by frame_no.
  - layer filtering matches on the web grouping name / quantity / layer_id_stem.
  - honest empty result when neither manifest exists -- never a fabricated list.
  - honest empty result when no matching frames (+ a typed reason).
  - missing run_id raises the typed error.

No network: the solver S3 helpers both readers share are monkeypatched.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.meta.list_run_frames.list_run_frames import (
    ListRunFramesError,
    list_run_frames,
)

_PM_URI = "s3://runs-bucket/run-xyz/publish_manifest.json"
_OUT_URI = "s3://runs-bucket/run-xyz/outputs.json"


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
        "cog_uri": cog_uri,
        "frame_no": frame_no,
    }


def _outputs_json(entries: list[dict]) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "engine": "sfincs",
            "run_id": "run-xyz",
            "entries": entries,
        }
    )


def _entry(quantity: str, name: str, uri: str, t: float | None) -> dict:
    e: dict = {"kind": "raster", "quantity": quantity, "name": name, "uri": uri}
    if t is not None:
        e["t"] = t
    return e


@pytest.fixture
def _patch_run(monkeypatch):
    """Patch the solver S3 helpers so both manifest readers resolve from
    in-memory bodies (no network). A ``None`` body = that object is absent."""

    def _install(*, outputs_text: str | None = None, publish_text: str | None = None):
        from trid3nt_server.workflows.solver import solver

        monkeypatch.setattr(solver, "_get_runs_bucket", lambda: "runs-bucket")
        monkeypatch.setattr(
            solver,
            "_try_get_completion_s3",
            lambda b, r: ({"publish_manifest_uri": _PM_URI} if publish_text else None),
        )

        def _read(uri: str) -> bytes:
            if uri == _OUT_URI and outputs_text is not None:
                return outputs_text.encode()
            if uri == _PM_URI and publish_text is not None:
                return publish_text.encode()
            raise FileNotFoundError(uri)

        monkeypatch.setattr(solver, "_read_object_bytes", _read)

    return _install


def test_outputs_frames_returned_ordered_by_t(_patch_run) -> None:
    """outputs.json frames come back ordered by the physical t; the non-temporal
    peak entry is excluded and frame_no is the 1-based ordinal."""
    entries = [
        _entry("flood_depth", "Peak flood depth", "s3://b/peak.tif", None),
        _entry("flood_depth", "Flood depth step 3", "s3://b/f3.tif", 1800.0),
        _entry("flood_depth", "Flood depth step 1", "s3://b/f1.tif", 600.0),
        _entry("flood_depth", "Flood depth step 2", "s3://b/f2.tif", 1200.0),
    ]
    _patch_run(outputs_text=_outputs_json(entries))

    out = list_run_frames("run-xyz", layer="flood depth")
    assert out["frame_count"] == 3
    assert out["frame_uris"] == ["s3://b/f1.tif", "s3://b/f2.tif", "s3://b/f3.tif"]
    assert [f["frame_no"] for f in out["frames"]] == [1, 2, 3]
    assert [f["t"] for f in out["frames"]] == [600.0, 1200.0, 1800.0]
    assert "reason" not in out


def test_outputs_matches_on_physical_quantity(_patch_run) -> None:
    """The layer filter matches the entry's physical ``quantity`` as well as its
    web grouping name."""
    entries = [
        _entry("flood_depth", "Depth step 1", "s3://b/f1.tif", 60.0),
        _entry("wave_height", "Wave step 1", "s3://b/w1.tif", 60.0),
    ]
    _patch_run(outputs_text=_outputs_json(entries))

    assert list_run_frames("run-xyz", layer="flood_depth")["frame_uris"] == [
        "s3://b/f1.tif"
    ]
    assert list_run_frames("run-xyz", layer="wave_height")["frame_uris"] == [
        "s3://b/w1.tif"
    ]


def test_outputs_wins_over_legacy_publish_manifest(_patch_run) -> None:
    """A run carrying BOTH manifests is served from outputs.json -- the seam's own
    contract is the single frame source of truth."""
    _patch_run(
        outputs_text=_outputs_json(
            [_entry("flood_depth", "Flood depth step 1", "s3://b/new1.tif", 60.0)]
        ),
        publish_text=_manifest_json(
            [_frame_layer("flood-step", "Flood depth step 1", 1, "s3://b/old1.tif")]
        ),
    )
    out = list_run_frames("run-xyz", layer="flood_depth")
    assert out["frame_uris"] == ["s3://b/new1.tif"]


def test_legacy_publish_manifest_frames_still_served(_patch_run) -> None:
    """A LEGACY run (publish_manifest frame layers, no outputs.json) is still
    served, ordered by frame_no, with the aggregate peak excluded."""
    layers = [
        _frame_layer("flood-peak", "Peak flood depth", None, "s3://b/peak.tif"),
        _frame_layer("flood-step", "Flood depth step 2", 2, "s3://b/f2.tif"),
        _frame_layer("flood-step", "Flood depth step 0", 0, "s3://b/f0.tif"),
        _frame_layer("flood-step", "Flood depth step 1", 1, "s3://b/f1.tif"),
    ]
    _patch_run(publish_text=_manifest_json(layers))

    out = list_run_frames("run-xyz", layer="flood depth")
    assert out["frame_count"] == 3
    assert out["frame_uris"] == ["s3://b/f0.tif", "s3://b/f1.tif", "s3://b/f2.tif"]
    assert [f["frame_no"] for f in out["frames"]] == [0, 1, 2]
    assert all(f["t"] is None for f in out["frames"])
    assert "reason" not in out


def test_legacy_layer_filter_excludes_non_matching(_patch_run) -> None:
    """Only layers matching the requested layer name are returned."""
    layers = [
        _frame_layer("flood-step", "Flood depth step 0", 0, "s3://b/flood0.tif"),
        _frame_layer("wave-step", "Wave height step 0", 0, "s3://b/wave0.tif"),
    ]
    _patch_run(publish_text=_manifest_json(layers))

    out = list_run_frames("run-xyz", layer="flood_depth")
    assert out["frame_uris"] == ["s3://b/flood0.tif"]


def test_blank_layer_lists_all_frames(_patch_run) -> None:
    """An empty ``layer`` lists ALL frames regardless of name."""
    entries = [
        _entry("flood_depth", "Flood depth step 1", "s3://b/flood1.tif", 60.0),
        _entry("wave_height", "Wave height step 1", "s3://b/wave1.tif", 120.0),
    ]
    _patch_run(outputs_text=_outputs_json(entries))

    out = list_run_frames("run-xyz", layer="")
    assert set(out["frame_uris"]) == {"s3://b/flood1.tif", "s3://b/wave1.tif"}
    assert out["frame_count"] == 2


def test_no_manifest_returns_honest_empty(_patch_run) -> None:
    """Neither manifest -> honest empty result (frame_count 0 + a reason), NOT a
    crash and NOT a fabricated list."""
    _patch_run()
    out = list_run_frames("run-xyz", layer="flood_depth")
    assert out["frame_count"] == 0
    assert out["frame_uris"] == []
    assert "reason" in out and "no outputs.json or publish_manifest.json" in out["reason"]


def test_no_matching_frames_returns_honest_empty(_patch_run) -> None:
    """A manifest with no matching frame -> honest empty result + reason."""
    _patch_run(
        outputs_text=_outputs_json(
            [_entry("wave_height", "Wave height step 1", "s3://b/w1.tif", 60.0)]
        )
    )
    out = list_run_frames("run-xyz", layer="flood_depth")
    assert out["frame_count"] == 0
    assert out["frame_uris"] == []
    assert "reason" in out and "outputs.json" in out["reason"]


def test_peak_only_outputs_manifest_returns_honest_empty(_patch_run) -> None:
    """A peak-only run (no temporal entries) is an honest empty listing -- the
    current publish_manifest carries no frame entries to fall back on."""
    _patch_run(
        outputs_text=_outputs_json(
            [_entry("flood_depth", "Peak flood depth", "s3://b/peak.tif", None)]
        ),
        publish_text=_manifest_json(
            [_frame_layer("flood-peak", "Peak flood depth", None, "s3://b/peak.tif")]
        ),
    )
    out = list_run_frames("run-xyz", layer="flood_depth")
    assert out["frame_count"] == 0
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


def test_frame_uris_feed_code_exec_multiframe_contract(_patch_run) -> None:
    """The frame_uris list is exactly a valid multi-frame layer_refs value for
    code_exec_request (the contract round-trips it)."""
    from trid3nt_contracts import new_ulid
    from trid3nt_contracts.sandbox_contracts import CodeExecRequestPayload

    _patch_run(
        outputs_text=_outputs_json(
            [
                _entry("flood_depth", "Flood depth step 1", "s3://b/f0.tif", 60.0),
                _entry("flood_depth", "Flood depth step 2", "s3://b/f1.tif", 120.0),
            ]
        )
    )
    out = list_run_frames("run-xyz", layer="flood_depth")

    payload = CodeExecRequestPayload(
        code_exec_id=new_ulid(),
        python_code="result = len(frames)",
        layer_refs={"frames": out["frame_uris"]},
    )
    assert payload.layer_refs["frames"] == ["s3://b/f0.tif", "s3://b/f1.tif"]
