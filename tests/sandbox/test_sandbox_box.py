"""The code-exec box, driven through the seam the tool calls.

Every run here goes through a real container: the network posture, the staging
and the caps are properties of the box, and a stubbed box would verify none of
them.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_server.sandbox import box
from trid3nt_server.sandbox.box import submit_sandbox_job


def _tif(path: Path, value: float = 3.0) -> str:
    data = np.full((8, 8), value, dtype="float32")
    with rasterio.open(path, "w", driver="GTiff", height=8, width=8, count=1,
                       dtype="float32", crs="EPSG:4326",
                       transform=from_origin(-80.0, 27.0, 0.01, 0.01)) as dst:
        dst.write(data, 1)
    return str(path)


# --------------------------------------------------------------------------- #
# What a snippet computes comes back
# --------------------------------------------------------------------------- #


def test_a_snippet_runs_in_the_box_and_returns_what_it_computed() -> None:
    envelope = submit_sandbox_job(
        "import numpy as np\nprint('mean')\nresult = float(np.array([1., 2., 3.]).mean())")
    assert envelope["status"] == "ok", envelope
    assert envelope["result"] == {"kind": "json", "value": 2.0}
    assert envelope["stdout"] == "mean\n"
    assert envelope["duration_s"] > 0


def test_a_figure_comes_back_as_a_chart_descriptor() -> None:
    envelope = submit_sandbox_job(
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots()\n"
        "ax.plot([1, 2, 3], [4, 5, 6])\n"
        "ax.set_title('Depth over time')\n"
        "result = fig\n")
    assert envelope["status"] == "ok", envelope
    assert envelope["result"]["kind"] == "chart"
    assert envelope["result"]["title"] == "Depth over time"
    assert envelope["result"]["png_base64"]


def test_a_table_comes_back_as_a_dataframe_descriptor() -> None:
    envelope = submit_sandbox_job(
        "import pandas as pd\nresult = pd.DataFrame({'a': [1, 2], 'b': ['x', 'y']})")
    assert envelope["result"]["kind"] == "dataframe"
    assert envelope["result"]["columns"] == ["a", "b"]
    assert envelope["result"]["records"][0] == {"a": 1, "b": "x"}


def test_a_snippet_that_raises_is_captured_rather_than_crashing_the_run() -> None:
    envelope = submit_sandbox_job("result = 1 / 0")
    assert envelope["status"] == "error"
    assert "ZeroDivisionError" in envelope["error"]
    assert "ZeroDivisionError" in envelope["stderr"]


def test_a_snippet_that_assigns_no_result_says_so() -> None:
    envelope = submit_sandbox_job("x = 5\nprint('nothing to return')")
    assert envelope["status"] == "ok", envelope
    assert envelope["result"]["kind"] == "none"


def test_a_snippet_that_never_ends_is_killed_at_its_cap() -> None:
    started = time.monotonic()
    envelope = submit_sandbox_job("while True:\n    pass", timeout_seconds=5)
    assert envelope["status"] == "timeout", envelope
    assert time.monotonic() - started < 25


def test_an_oversized_result_is_truncated_and_says_so() -> None:
    """A result too big for the wire is marked, never quietly cut down."""
    envelope = submit_sandbox_job("result = 'x' * 9_000_000")
    assert envelope["result"]["truncated"] is True
    assert envelope["result"]["original_bytes"] > 2 * 1024 * 1024
    assert json.loads(json.dumps(envelope))["status"] == "ok"

    envelope = submit_sandbox_job("result = list(range(2_000_000))")
    assert envelope["result"]["kind"] == "too_large"


def test_a_flood_of_prints_is_bounded_and_says_so() -> None:
    envelope = submit_sandbox_job(
        "for _ in range(200_000):\n    print('x' * 40)\nresult = 'done'",
        timeout_seconds=60)
    assert envelope["stdout_truncated"] is True
    assert len(envelope["stdout"]) <= 70_000


# --------------------------------------------------------------------------- #
# SandboxIsNetworkNone
# --------------------------------------------------------------------------- #


def test_the_box_runs_with_the_network_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The launch line itself carries the denial: it is not a runtime opinion."""
    seen: list[list[str]] = []

    def capture(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "captured")

    monkeypatch.setattr(box.subprocess, "run", capture)
    submit_sandbox_job("result = 1")
    argv = seen[0]
    assert argv[:2] == ["docker", "run"]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--memory" in argv and "--pids-limit" in argv and "--cpus" in argv


def test_a_snippet_cannot_reach_the_network() -> None:
    envelope = submit_sandbox_job(
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('http://example.com', timeout=5)\n"
        "    result = 'REACHED'\n"
        "except Exception as exc:\n"
        "    result = f'DENIED:{type(exc).__name__}'\n")
    assert envelope["status"] == "ok", envelope
    assert envelope["result"]["value"].startswith("DENIED:"), envelope
    assert "REACHED" not in json.dumps(envelope)


def test_a_denied_egress_is_reported_as_blocked_rather_than_as_a_bug() -> None:
    envelope = submit_sandbox_job(
        "import socket\nsocket.create_connection(('example.com', 80), timeout=5)")
    assert envelope["status"] == "blocked", envelope


# --------------------------------------------------------------------------- #
# DataEntersStaged
# --------------------------------------------------------------------------- #


def test_a_local_raster_is_staged_and_opens_as_a_handle(tmp_path: Path) -> None:
    """The snippet gets an open dataset, and its path is inside the run dir."""
    envelope = submit_sandbox_job(
        "result = {'mean': float(flood.read(1).mean()), 'path': flood_uri}",
        {"flood": _tif(tmp_path / "flood.tif", 3.0)})
    assert envelope["status"] == "ok", envelope
    assert envelope["result"]["value"]["mean"] == pytest.approx(3.0)
    assert envelope["result"]["value"]["path"].startswith("/work/staged/")


def test_frames_stage_as_an_ordered_list(tmp_path: Path) -> None:
    frames = [_tif(tmp_path / f"f{i}.tif", float(i)) for i in range(3)]
    envelope = submit_sandbox_job(
        "result = [float(f.read(1).mean()) for f in frames]", {"frames": frames})
    assert envelope["status"] == "ok", envelope
    assert envelope["result"]["value"] == [0.0, 1.0, 2.0]


def test_a_remote_ref_is_fetched_by_the_host_before_the_box_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The box never fetches. The host reads the object and hands over the bytes."""
    payload = Path(_tif(tmp_path / "src.tif", 7.0)).read_bytes()
    asked: list[str] = []

    def read_object_bytes_s3(uri: str) -> bytes:
        asked.append(uri)
        return payload

    monkeypatch.setattr("trid3nt_server.tools.cache.read_object_bytes_s3",
                        read_object_bytes_s3)
    envelope = submit_sandbox_job("result = float(dem.read(1).mean())",
                                  {"dem": "s3://bucket/dem.tif"})
    assert asked == ["s3://bucket/dem.tif"]
    assert envelope["result"]["value"] == pytest.approx(7.0)


def test_a_ref_that_cannot_be_staged_is_named_rather_than_crashing_the_run() -> None:
    envelope = submit_sandbox_job(
        "result = isinstance(missing, str)", {"missing": "s3://bucket/gone.tif"})
    assert envelope["status"] == "ok", envelope
    assert envelope["result"]["value"] is True
    assert "missing" in envelope["layer_errors"]


def test_the_box_reaches_for_nothing_from_the_inside() -> None:
    """The driver has no fetch in it: a world-read cannot start inside the box."""
    source = (Path(box.__file__).parent / "driver.py").read_text(encoding="utf-8")
    for reacher in ("boto3", "requests", "urllib", "s3fs", "fsspec", "trid3nt_server"):
        assert f"import {reacher}" not in source


# --------------------------------------------------------------------------- #
# OffloadKeepsTheLoopUnblocked
# --------------------------------------------------------------------------- #


def test_the_tool_that_drives_the_box_is_always_offloaded() -> None:
    from trid3nt_server.server.dispatch import emitter

    assert "code_exec_request" in emitter._ALWAYS_OFFLOAD_SYNC_TOOLS
    assert emitter._should_offload_sync_tool("code_exec_request") is True


def test_the_offload_keeps_the_loop_unblocked() -> None:
    """A whole box run off the loop, with the loop ticking throughout it."""
    beats: list[float] = []

    async def drive() -> dict:
        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(0.05)
                beats.append(time.monotonic())

        beat = asyncio.create_task(heartbeat())
        try:
            return await asyncio.to_thread(submit_sandbox_job, "result = 6 * 7")
        finally:
            beat.cancel()

    envelope = asyncio.run(drive())
    assert envelope["result"]["value"] == 42
    assert len(beats) >= 5, "the loop stalled while the box ran"
