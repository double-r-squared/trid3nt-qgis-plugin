"""job-0308: qgis_process RUN — param translation (stage-then-mount) unit tests."""
from unittest.mock import patch

from trid3nt_server.tools.meta.passthroughs import (
    QGIS_OFFLOADED_ERROR_CODE,
    _build_qgis_run_args,
    _qgis_onbox_docker_enabled,
    qgis_process,
)


def _fake_stager(value, rundir):
    if isinstance(value, str) and value.startswith(("s3://", "gs://")):
        return f"/data/{value.rsplit('/', 1)[-1]}"
    return None


def test_inputs_staged_outputs_collected_literals_passthrough():
    params = {"INPUT": "s3://b/dem.tif", "BAND": 1, "Z_FACTOR": 1.0, "OUTPUT": "slope.tif"}
    args, outputs = _build_qgis_run_args(params, "/tmp/x", _fake_stager)
    assert "--INPUT=/data/dem.tif" in args          # s3 input staged + rewritten
    assert "--BAND=1" in args and "--Z_FACTOR=1.0" in args  # literals pass through
    assert "--OUTPUT=/data/output.tif" in args      # output sink -> container path
    assert outputs == {"OUTPUT": "output.tif"}       # collected for upload


def test_output_without_ext_defaults_tif_and_gs_input_staged():
    params = {"INPUT": "gs://b/x.gpkg", "OUTPUT": ""}
    args, outputs = _build_qgis_run_args(params, "/tmp/x", _fake_stager)
    assert "--INPUT=/data/x.gpkg" in args
    assert outputs == {"OUTPUT": "output.tif"}
    assert "--OUTPUT=/data/output.tif" in args


def test_vector_output_ext_preserved():
    params = {"INPUT": "s3://b/pts.gpkg", "DISTANCE": 100, "OUTPUT": "buf.gpkg"}
    args, outputs = _build_qgis_run_args(params, "/tmp/x", _fake_stager)
    assert outputs == {"OUTPUT": "output.gpkg"}
    assert "--DISTANCE=100" in args


# ---------------------------------------------------------------------------
# On-box QGIS execution gate (reliability hardening 2026-06-29): the heavy
# docker/subprocess RUN path is DISABLED by default so it cannot compete for
# the shared agent box; an honest typed "did not run" result is returned.
# ---------------------------------------------------------------------------


def test_onbox_gate_default_off(monkeypatch):
    """The gate is OFF unless TRID3NT_QGIS_ONBOX_DOCKER is explicitly truthy."""
    monkeypatch.delenv("TRID3NT_QGIS_ONBOX_DOCKER", raising=False)
    assert _qgis_onbox_docker_enabled() is False
    for falsy in ("", "off", "0", "false", "no", "garbage"):
        monkeypatch.setenv("TRID3NT_QGIS_ONBOX_DOCKER", falsy)
        assert _qgis_onbox_docker_enabled() is False
    for truthy in ("1", "true", "TRUE", "yes", "on", "On"):
        monkeypatch.setenv("TRID3NT_QGIS_ONBOX_DOCKER", truthy)
        assert _qgis_onbox_docker_enabled() is True


def test_qgis_process_disabled_returns_honest_no_run(monkeypatch):
    """With the gate OFF, qgis_process returns a typed error and spawns NOTHING.

    No docker / subprocess is launched; the result reads as an error (NOT a
    fabricated success) so the model + UI + telemetry know it did not run."""
    monkeypatch.delenv("TRID3NT_QGIS_ONBOX_DOCKER", raising=False)
    # A docker image IS configured -- proving the gate short-circuits BEFORE
    # the docker path would otherwise engage.
    monkeypatch.setenv("TRID3NT_QGIS_DOCKER_IMAGE", "grace2-qgis:ltr")

    with patch(
        "trid3nt_server.tools.meta.passthroughs._run_qgis_process_docker"
    ) as run_docker, patch("subprocess.run") as subproc:
        result = qgis_process(
            algorithm="native:slope", params={"INPUT": "s3://b/dem.tif"}
        )

    run_docker.assert_not_called()
    subproc.assert_not_called()
    assert result["status"] == "error"
    assert result["error_code"] == QGIS_OFFLOADED_ERROR_CODE
    assert result["did_run"] is False
    assert result["algorithm"] == "native:slope"
    assert result["retryable"] is False
    assert "offloaded" in result["message"].lower()
    # Never reads as a success.
    assert result["status"] != "succeeded"


def test_qgis_process_enabled_runs_docker_path(monkeypatch):
    """With the gate ON, the existing docker RUN path engages (kept intact)."""
    monkeypatch.setenv("TRID3NT_QGIS_ONBOX_DOCKER", "on")
    monkeypatch.setenv("TRID3NT_QGIS_DOCKER_IMAGE", "grace2-qgis:ltr")

    sentinel = {"status": "succeeded", "tool": "qgis_process"}
    with patch(
        "trid3nt_server.tools.meta.passthroughs._run_qgis_process_docker",
        return_value=sentinel,
    ) as run_docker:
        result = qgis_process(algorithm="native:slope", params={"INPUT": "x"})

    run_docker.assert_called_once()
    assert result is sentinel
