"""``list_run_frames``: the ORDERED animation-frame COG URIs of a run's layer.

The emit-on-solve ``outputs.json`` is the one frame source - frames are the
raster entries carrying a physical ``t``. No match is an honest empty result
with a typed reason, never a fabricated list."""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.meta.list_run_frames.list_run_frames import (
    ListRunFramesError,
    list_run_frames,
)

_OUT_URI = "s3://runs-bucket/run-xyz/outputs.json"


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
    """Patch the solver S3 helpers so the manifest reader resolves from an
    in-memory body (no network). A ``None`` body = that object is absent."""

    def _install(*, outputs_text: str | None = None):
        from trid3nt_server import storage
        from trid3nt_server.workflows.solver import solver

        monkeypatch.setattr(storage, "runs_bucket", lambda: "runs-bucket")

        def _read(uri: str) -> bytes:
            if uri == _OUT_URI and outputs_text is not None:
                return outputs_text.encode()
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
    """No manifest -> honest empty result (frame_count 0 + a reason), NOT a
    crash and NOT a fabricated list."""
    _patch_run()
    out = list_run_frames("run-xyz", layer="flood_depth")
    assert out["frame_count"] == 0
    assert out["frame_uris"] == []
    assert "reason" in out and "no outputs.json" in out["reason"]


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
    """A peak-only run (no temporal entries) is an honest empty listing."""
    _patch_run(
        outputs_text=_outputs_json(
            [_entry("flood_depth", "Peak flood depth", "s3://b/peak.tif", None)]
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
