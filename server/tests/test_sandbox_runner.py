"""Tests for the Python sandbox host-side runner (job-0232, sprint-13 Stage 2).

Drives ``trid3nt_server.sandbox_runner`` in local-subprocess fallback mode
(``TRID3NT_SANDBOX_LOCAL=1``), which runs the SAME ``infra/python-sandbox/
executor.py`` harness baked into the Cloud Run Job image. These are the
environment-adjusted acceptance for the harness (the real VPC egress-deny is
BLOCKED-ENV — no docker/gcloud on this box — and verified later per the report
runbook).

The 4 kickoff scenarios + robustness extensions:
  (a) benign numpy script           -> result=float, status="ok"
  (b) matplotlib figure             -> chart payload (ChartEmissionPayload-shaped)
  (c) urllib/socket to example.com  -> blocked by the in-process net guard
  (d) infinite loop                 -> killed at the wallclock cap (status="timeout")
  (d2) SIGALRM defeated             -> outer subprocess hard-kill backstop
  + output-bound + DataFrame-conversion + layer-ref injection coverage.

No network. No Gemini. Pure local subprocess harness.
"""

from __future__ import annotations

import os
import time

import pytest

from trid3nt_server.sandbox_runner import (
    SandboxExecutionHandle,
    run_sandbox_local,
    submit_sandbox_job,
)


@pytest.fixture(autouse=True)
def _force_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module runs the local-subprocess fallback."""
    monkeypatch.setenv("TRID3NT_SANDBOX_LOCAL", "1")
    monkeypatch.setenv("MPLBACKEND", "Agg")


# --------------------------------------------------------------------------- #
# (a) benign numpy -> result=float
# --------------------------------------------------------------------------- #


def test_scenario_a_benign_numpy_returns_float() -> None:
    code = (
        "import numpy as np\n"
        "arr = np.array([10.0, 20.0, 30.0, 40.0])\n"
        "print(f'sum={arr.sum()}')\n"
        "result = float(arr.mean())\n"
    )
    env = run_sandbox_local(code, {})
    assert env["status"] == "ok", env
    assert env["error"] is None
    assert env["result"]["kind"] == "json"
    assert env["result"]["value"] == 25.0
    assert "sum=100.0" in env["stdout"]
    assert env["wallclock_cap_seconds"] == 60


def test_scenario_a_numpy_scalar_descriptor() -> None:
    """A numpy scalar result is converted to a {'kind': 'scalar'} descriptor."""
    code = "import numpy as np\nresult = np.float64(3.14)\n"
    env = run_sandbox_local(code, {})
    assert env["status"] == "ok", env
    assert env["result"]["kind"] == "scalar"
    assert env["result"]["value"] == pytest.approx(3.14)


# --------------------------------------------------------------------------- #
# (b) matplotlib figure -> chart payload conversion
# --------------------------------------------------------------------------- #


def test_scenario_b_matplotlib_figure_to_chart_payload() -> None:
    code = (
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "import numpy as np\n"
        "fig, ax = plt.subplots()\n"
        "ax.hist(np.random.RandomState(42).normal(size=500), bins=20)\n"
        "ax.set_title('Damage distribution')\n"
        "result = fig\n"
    )
    env = run_sandbox_local(code, {})
    assert env["status"] == "ok", env
    r = env["result"]
    assert r["kind"] == "chart", r
    assert r["title"] == "Damage distribution"
    assert r["png_base64"], "figure PNG should be inlined"
    assert r["png_truncated"] is False
    # trid3nt_contracts is importable in this venv, so a ChartEmissionPayload-shaped
    # dict must be emitted.
    assert r.get("chart_contract_available") is True
    ce = r["chart_emission"]
    assert ce["title"] == "Damage distribution"
    assert ce["vega_lite_spec"]["$schema"].startswith("https://vega.github.io/schema/vega-lite")


def test_scenario_b_chart_payload_constructs_real_pydantic_model() -> None:
    """The emitted chart_emission dict must construct the real ChartEmissionPayload
    (drift-free against the job-0223 contract — the reconciliation target for
    job-0233)."""
    from trid3nt_contracts import new_ulid
    from trid3nt_contracts.chart_contracts import (
        ChartEmissionPayload,
        is_structurally_valid_vega_lite_spec,
    )

    code = (
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots()\n"
        "ax.plot([1, 2, 3], [4, 5, 6])\n"
        "ax.set_title('Line chart')\n"
        "result = fig\n"
    )
    env = run_sandbox_local(code, {})
    ce = env["result"]["chart_emission"]
    assert is_structurally_valid_vega_lite_spec(ce["vega_lite_spec"])
    payload = ChartEmissionPayload(
        chart_id=new_ulid(),
        vega_lite_spec=ce["vega_lite_spec"],
        title=ce["title"],
        caption=ce["caption"],
    )
    assert payload.envelope_type == "chart-emission"
    assert payload.title == "Line chart"


# --------------------------------------------------------------------------- #
# (c) malicious network -> blocked by the in-process guard
# --------------------------------------------------------------------------- #


def test_scenario_c_raw_socket_to_example_com_blocked() -> None:
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('example.com', 80), timeout=5)\n"
        "    result = 'REACHED_INTERNET'\n"
        "except Exception as e:\n"
        "    result = f'BLOCKED:{type(e).__name__}'\n"
    )
    env = run_sandbox_local(code, {})
    assert env["status"] == "ok", env
    assert env["result"]["value"].startswith("BLOCKED:"), env
    assert "REACHED_INTERNET" not in str(env)
    assert "SandboxNetworkBlocked" in env["result"]["value"]


def test_scenario_c_urllib_to_example_com_blocked() -> None:
    code = (
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('http://example.com', timeout=5)\n"
        "    result = 'REACHED_INTERNET'\n"
        "except Exception as e:\n"
        "    result = f'BLOCKED:{type(e).__name__}'\n"
    )
    env = run_sandbox_local(code, {})
    assert env["status"] == "ok", env
    assert "REACHED_INTERNET" not in str(env), "urllib reached the internet — guard failed"
    assert env["result"]["value"].startswith("BLOCKED:"), env


def test_scenario_c_loopback_allowed() -> None:
    """The guard must NOT block loopback (matplotlib Agg / multiprocessing rely on
    it). A connect to 127.0.0.1 to a closed port should fail with ConnectionError,
    NOT SandboxNetworkBlocked."""
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 1), timeout=2)\n"
        "    result = 'CONNECTED'\n"
        "except Exception as e:\n"
        "    result = type(e).__name__\n"
    )
    env = run_sandbox_local(code, {})
    assert env["status"] == "ok", env
    # Loopback is allowed by the guard, so we get a normal connection refusal, not
    # a SandboxNetworkBlocked.
    assert env["result"]["value"] != "SandboxNetworkBlocked", env


# --------------------------------------------------------------------------- #
# (d) infinite loop -> killed at the cap
# --------------------------------------------------------------------------- #


def test_scenario_d_infinite_loop_killed_at_cap() -> None:
    code = "x = 0\nwhile True:\n    x += 1\nresult = x\n"
    t0 = time.time()
    env = run_sandbox_local(code, {}, timeout_seconds=3)
    elapsed = time.time() - t0
    assert env["status"] == "timeout", env
    assert "wallclock cap" in env["error"]
    # In-process SIGALRM fires at ~3s; well under the outer kill window.
    assert elapsed < 12, f"cap not enforced promptly: {elapsed:.1f}s"


def test_scenario_d2_outer_kill_backstop_when_alarm_defeated() -> None:
    """Code that installs its own SIGALRM handler to defeat the in-process watchdog
    must still be killed by the outer subprocess timeout (belt-and-suspenders)."""
    code = (
        "import signal\n"
        "signal.signal(signal.SIGALRM, signal.SIG_IGN)\n"
        "x = 0\n"
        "while True:\n"
        "    x += 1\n"
        "result = x\n"
    )
    t0 = time.time()
    env = run_sandbox_local(code, {}, timeout_seconds=2)
    elapsed = time.time() - t0
    assert env["status"] == "timeout", env
    # Outer kill = cap(2) + grace(10) = 12s; must terminate within a small margin.
    assert elapsed < 20, f"outer kill too slow: {elapsed:.1f}s"


# --------------------------------------------------------------------------- #
# Output bounds + result conversions + error handling
# --------------------------------------------------------------------------- #


def test_stdout_truncation_bounds_runaway_print() -> None:
    """A flood of prints is truncated, not unbounded."""
    code = (
        "for i in range(2_000_000):\n"
        "    print('x' * 40)\n"
        "result = 'done'\n"
    )
    env = run_sandbox_local(code, {}, timeout_seconds=20)
    # Either the cap fires or it completes; either way stdout must be bounded.
    assert len(env["stdout"]) <= 70_000, len(env["stdout"])
    assert env["stdout_truncated"] is True


def test_dataframe_result_to_records() -> None:
    code = (
        "import pandas as pd\n"
        "result = pd.DataFrame({'a': [1, 2, 3], 'b': ['x', 'y', 'z']})\n"
    )
    env = run_sandbox_local(code, {})
    assert env["status"] == "ok", env
    r = env["result"]
    assert r["kind"] == "dataframe"
    assert r["columns"] == ["a", "b"]
    assert r["row_count"] == 3
    assert r["records"][0] == {"a": 1, "b": "x"}


def test_dataframe_row_cap_truncates() -> None:
    code = (
        "import pandas as pd\n"
        "result = pd.DataFrame({'a': list(range(10000))})\n"
    )
    env = run_sandbox_local(code, {})
    r = env["result"]
    assert r["kind"] == "dataframe"
    assert r["row_count"] == 10000
    assert r["truncated"] is True
    assert r["returned_rows"] <= 5000


def test_user_code_error_is_captured_not_crashed() -> None:
    """A traceback in user code yields status='error' with the message captured —
    the harness never propagates the exception."""
    code = "result = 1 / 0\n"
    env = run_sandbox_local(code, {})
    assert env["status"] == "error", env
    assert "ZeroDivisionError" in env["error"]
    assert "ZeroDivisionError" in env["stderr"]


def test_no_result_variable_yields_none_descriptor() -> None:
    code = "x = 5\nprint('no result set')\n"
    env = run_sandbox_local(code, {})
    assert env["status"] == "ok", env
    assert env["result"]["kind"] == "none"


def test_layer_refs_unknown_ext_exposed_as_uri() -> None:
    """A layer ref with an unknown extension is handed to user code as the raw URI
    string under a sanitized var name + a <name>_uri alias."""
    code = (
        "result = {'has_uri': isinstance(my_layer, str), "
        "'uri': my_layer, 'alias': my_layer_uri}\n"
    )
    env = run_sandbox_local(code, {"my_layer": "gs://bucket/path/data.unknownext"})
    assert env["status"] == "ok", env
    val = env["result"]["value"]
    assert val["has_uri"] is True
    assert val["uri"] == "gs://bucket/path/data.unknownext"
    assert val["alias"] == "gs://bucket/path/data.unknownext"


def test_layers_dict_and_layer_uris_injected() -> None:
    code = (
        "result = {'layer_uris': layer_uris, "
        "'in_layers': 'flood' in layers}\n"
    )
    env = run_sandbox_local(
        code, {"flood": "gs://b/flood.cog"}
    )
    assert env["status"] == "ok", env
    val = env["result"]["value"]
    assert val["layer_uris"] == {"flood": "gs://b/flood.cog"}
    assert val["in_layers"] is True


# --------------------------------------------------------------------------- #
# submit_sandbox_job routing (local mode returns a finished envelope)
# --------------------------------------------------------------------------- #


def test_submit_sandbox_job_local_mode_returns_envelope() -> None:
    """In local mode, submit_sandbox_job runs synchronously and returns the result
    envelope dict (not a pending handle)."""
    result = submit_sandbox_job("result = 6 * 7", {})
    assert isinstance(result, dict)
    assert result["status"] == "ok"
    assert result["result"]["value"] == 42


def test_submit_sandbox_job_always_local_after_gcp_decommission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GCP cloud sandbox path was removed; submit_sandbox_job always runs the
    local-subprocess path and returns a finished result envelope (never a pending
    handle), even with TRID3NT_SANDBOX_LOCAL unset."""
    monkeypatch.delenv("TRID3NT_SANDBOX_LOCAL", raising=False)
    result = submit_sandbox_job("result = 1 + 1", {})
    assert isinstance(result, dict)
    assert result["status"] == "ok"
    assert result["result"]["value"] == 2


def test_sandbox_execution_handle_shape() -> None:
    """The vestigial cloud handle dataclass is still constructible (back-compat)."""
    from datetime import datetime, timezone

    h = SandboxExecutionHandle(
        handle_id="01HX",
        execution_name="projects/p/locations/us-central1/jobs/legacy-python-sandbox/executions/e1",
        payload_uri="gs://b/sandbox/x/payload.json",
        result_uri="gs://b/sandbox/x/result.json",
        submitted_at=datetime.now(timezone.utc),
    )
    assert h.mode == "cloud"
    assert "legacy-python-sandbox" in h.execution_name


# --------------------------------------------------------------------------- #
# sandbox-staging: multi-frame layer_refs + the S3 pre-fetch -> local-path rewrite
# --------------------------------------------------------------------------- #


def _write_tiny_tif(path: str, fill: float) -> None:
    """Write a 4x4 single-band float32 GeoTIFF filled with ``fill`` at ``path``."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    arr = np.full((4, 4), fill, dtype="float32")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=4,
        width=4,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(0, 4, 1, 1),
    ) as dst:
        dst.write(arr, 1)


def test_single_uri_string_path_unchanged(tmp_path, monkeypatch) -> None:
    """A SINGLE local .tif path opens to ONE rasterio handle bound to the var
    (the legacy single-string contract, byte-identical).

    Jail OFF: this exercises the executor's handle-OPENING logic over a local tif
    outside the jail's binds; the jailed staged-path flow is covered separately by
    ``test_staged_frames_open_under_jail_end_to_end``."""
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "0")
    tif = str(tmp_path / "depth.tif")
    _write_tiny_tif(tif, 3.0)
    code = (
        "import rasterio\n"
        "is_ds = hasattr(flood, 'read')\n"
        "val = float(flood.read(1).mean()) if is_ds else -1.0\n"
        "result = {'is_dataset': is_ds, 'mean': val, 'uri': flood_uri}\n"
    )
    env = run_sandbox_local(code, {"flood": tif})
    assert env["status"] == "ok", env
    val = env["result"]["value"]
    assert val["is_dataset"] is True
    assert val["mean"] == 3.0
    assert val["uri"] == tif


def test_list_valued_layer_refs_open_as_list_of_handles(tmp_path, monkeypatch) -> None:
    """A LIST of local frame .tifs opens to an ORDERED LIST of handles bound to
    the var (the multi-frame extension) — a snippet can iterate frames.

    Jail OFF (executor handle-opening logic; see the single-uri test note)."""
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "0")
    frames = []
    for i, fill in enumerate([1.0, 5.0, 9.0]):
        p = str(tmp_path / f"frame_{i}.tif")
        _write_tiny_tif(p, fill)
        frames.append(p)
    code = (
        "import rasterio\n"
        "means = [float(f.read(1).mean()) for f in frames]\n"
        "result = {'n': len(frames), 'means': means, "
        "'all_ds': all(hasattr(f, 'read') for f in frames), "
        "'uris': frames_uris}\n"
    )
    env = run_sandbox_local(code, {"frames": frames})
    assert env["status"] == "ok", env
    val = env["result"]["value"]
    assert val["n"] == 3
    assert val["means"] == [1.0, 5.0, 9.0]  # ORDER preserved
    assert val["all_ds"] is True
    assert val["uris"] == frames


def test_prefetch_rewrites_s3_uri_to_local_path(monkeypatch, tmp_path) -> None:
    """``stage_layer_refs_locally`` downloads every s3:// URI (single OR list) into
    the workdir and rewrites the refs to LOCAL paths; the executor only ever sees
    local files (the jail is network-denied)."""
    from trid3nt_server import sandbox_runner as sr

    fetched: list[str] = []

    def _fake_read(uri: str) -> bytes:
        fetched.append(uri)
        return b"COG-BYTES-FOR-" + uri.encode()

    # Patch the shared boto3 reader the staging path imports.
    monkeypatch.setattr("trid3nt_server.tools.cache.read_object_bytes_s3", _fake_read)

    workdir = str(tmp_path / "wd")
    os.makedirs(workdir, exist_ok=True)
    refs = {
        "flood": "s3://bucket/runs/peak.tif",
        "frames": ["s3://bucket/runs/f0.tif", "s3://bucket/runs/f1.tif"],
        "local": str(tmp_path / "already_local.tif"),  # untouched
    }
    rewritten, staged_dir = sr.stage_layer_refs_locally(refs, workdir)

    # Every s3:// URI was fetched once, in order.
    assert fetched == [
        "s3://bucket/runs/peak.tif",
        "s3://bucket/runs/f0.tif",
        "s3://bucket/runs/f1.tif",
    ]
    # The single s3 ref rewrote to a LOCAL path inside the staged dir.
    assert rewritten["flood"].startswith(workdir)
    assert os.path.isfile(rewritten["flood"])
    with open(rewritten["flood"], "rb") as fh:
        assert fh.read() == b"COG-BYTES-FOR-s3://bucket/runs/peak.tif"
    # The list ref rewrote to a LIST of local paths, order preserved.
    assert isinstance(rewritten["frames"], list)
    assert len(rewritten["frames"]) == 2
    assert all(p.startswith(workdir) and os.path.isfile(p) for p in rewritten["frames"])
    # The non-s3 (local) ref is passed through UNCHANGED.
    assert rewritten["local"] == refs["local"]
    # The staged dir is returned (something was staged) so the caller binds it.
    assert staged_dir is not None and staged_dir.startswith(workdir)


def test_staged_frames_open_under_jail_end_to_end(tmp_path, monkeypatch) -> None:
    """THE multi-frame proof: s3:// frame URIs are pre-fetched into the staged
    dir, bound READ-ONLY into the jail, and opened as an ORDERED LIST of rasterio
    handles BY THE NETWORK-DENIED EXECUTOR — end to end, jail ON (when bwrap is
    present). The fetch is monkeypatched to return REAL tif bytes (no network)."""
    import io as _io

    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    def _tif_bytes(fill: float) -> bytes:
        buf = _io.BytesIO()
        arr = np.full((4, 4), fill, dtype="float32")
        with rasterio.open(
            buf, "w", driver="GTiff", height=4, width=4, count=1,
            dtype="float32", crs="EPSG:4326", transform=from_origin(0, 4, 1, 1),
        ) as dst:
            dst.write(arr, 1)
        return buf.getvalue()

    fills = {"s3://b/f0.tif": 2.0, "s3://b/f1.tif": 4.0, "s3://b/f2.tif": 6.0}

    def _fake_read(uri: str) -> bytes:
        return _tif_bytes(fills[uri])

    monkeypatch.setattr("trid3nt_server.tools.cache.read_object_bytes_s3", _fake_read)

    code = (
        "import rasterio\n"
        "means = [float(f.read(1).mean()) for f in frames]\n"
        "result = {'means': means, 'n': len(frames)}\n"
    )
    env = run_sandbox_local(code, {"frames": list(fills.keys())})
    assert env["status"] == "ok", env
    val = env["result"]["value"]
    assert val["n"] == 3
    assert val["means"] == [2.0, 4.0, 6.0]  # ORDER preserved through staging+jail


def test_prefetch_no_staging_when_no_s3(tmp_path) -> None:
    """When NO ref is an s3:// URI, staging is a no-op: refs are returned unchanged
    and ``staged_dir`` is None (the caller skips the extra ro-bind)."""
    from trid3nt_server import sandbox_runner as sr

    workdir = str(tmp_path / "wd")
    os.makedirs(workdir, exist_ok=True)
    refs = {"a": "/local/path.tif", "b": ["gs://legacy/f0.tif"]}
    rewritten, staged_dir = sr.stage_layer_refs_locally(refs, workdir)
    assert rewritten == refs
    assert staged_dir is None


def test_prefetch_degrades_on_fetch_failure(monkeypatch, tmp_path) -> None:
    """A single failed s3 fetch degrades to the raw URI string (never crashes) so
    the executor's _open_layer falls back + records a _layer_errors entry."""
    from trid3nt_server import sandbox_runner as sr

    def _boom(uri: str) -> bytes:
        raise RuntimeError("simulated S3 outage")

    monkeypatch.setattr("trid3nt_server.tools.cache.read_object_bytes_s3", _boom)

    workdir = str(tmp_path / "wd")
    os.makedirs(workdir, exist_ok=True)
    rewritten, staged_dir = sr.stage_layer_refs_locally(
        {"flood": "s3://bucket/x.tif"}, workdir
    )
    # The failed fetch handed back the raw URI (degrade-don't-crash).
    assert rewritten["flood"] == "s3://bucket/x.tif"
    # Nothing was successfully staged.
    assert staged_dir is None


# --------------------------------------------------------------------------- #
# executor-path resolution (deploy executor-shipping gap)
# --------------------------------------------------------------------------- #


def test_executor_path_honors_env_override_first(monkeypatch, tmp_path) -> None:
    """TRID3NT_SANDBOX_EXECUTOR is honored FIRST, unconditionally, before any
    repo-root walk-up.

    This is the seam the agent deploy relies on: on the /opt/grace2 site-packages
    install the repo-root walk-up + parents[4] fallback both miss (executor.py is
    not on the import path), so deploy_agent_onbox.sh installs the bundled
    executor.py to a stable path and points this env var at it. Resolving via the
    override -- even to a path that does not exist yet -- proves the on-box fix
    closes the FileNotFoundError-fails-closed gap deterministically.
    """
    from trid3nt_server import sandbox_runner as sr

    override = tmp_path / "python-sandbox" / "executor.py"
    monkeypatch.setenv("TRID3NT_SANDBOX_EXECUTOR", str(override))
    assert sr._executor_path() == override

    # Whitespace-only / empty override is ignored -> falls through to the walk-up.
    monkeypatch.setenv("TRID3NT_SANDBOX_EXECUTOR", "   ")
    resolved = sr._executor_path()
    assert resolved.name == "executor.py"
    assert resolved.parent.name == "python-sandbox"


def test_executor_path_default_walkup_resolves_in_repo() -> None:
    """With NO override (dev/repo layout) the walk-up finds the real
    infra/python-sandbox/executor.py and it exists on disk."""
    import os as _os

    from trid3nt_server import sandbox_runner as sr

    prior = _os.environ.pop("TRID3NT_SANDBOX_EXECUTOR", None)
    try:
        p = sr._executor_path()
        assert p.exists(), f"executor not found at {p}"
        assert p.parts[-2:] == ("python-sandbox", "executor.py")
    finally:
        if prior is not None:
            _os.environ["TRID3NT_SANDBOX_EXECUTOR"] = prior
