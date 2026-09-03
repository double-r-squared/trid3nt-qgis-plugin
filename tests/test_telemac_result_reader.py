"""The result read runs in the engine's box, and nothing on this side parses.

Offline: the container boundary is stubbed one level down, at ``subprocess.run``,
so the launch line, the mount set and the assembly of the arrays the driver
leaves are exercised without the image. The parse itself is the engine's own
``TelemacFile`` and is proved against real result files in the image it runs in.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.workflows.telemac import result_reader as R

#: Every module on this side that reads a solved result. None may parse the
#: format: a second implementation of it is a second thing to be wrong about it.
_READERS = (
    "trid3nt_server/workflows/telemac/result_reader.py",
    "trid3nt_server/workflows/telemac/products/postprocess_telemac.py",
    "trid3nt_server/workflows/telemac/products/run_reads.py",
    "trid3nt_server/workflows/telemac/authoring/assembler.py",
)

_REPO = Path(__file__).resolve().parents[1]


def _scratch_of(argv: list[str]) -> Path:
    """The host directory the launch line mounts as the driver's ``/data``."""
    return Path(next(a.split(":")[0] for a in argv if a.endswith(":/data")))


def _driver_leaves(fields: dict, meta: dict):
    """A fake ``subprocess.run`` that writes what the driver would have left."""
    def run(argv, **_kw):
        out = _scratch_of(argv)
        np.savez(out / "telemac_result_fields.npz", **fields)
        (out / "telemac_result_meta.json").write_text(json.dumps(meta))
        return subprocess.CompletedProcess(argv, 0, "TELEMAC_RESULT_OK", "")
    return run


def test_the_read_runs_in_the_telemac_box_with_no_network(tmp_path, monkeypatch):
    slf = tmp_path / "r2d_river.slf"
    slf.write_bytes(b"result")
    seen: dict = {}

    leave = _driver_leaves(
        {"x": np.zeros(3), "y": np.zeros(3), "ikle": np.array([[0, 1, 2]]),
         "times": np.array([0.0]), "v0": np.zeros((1, 3))},
        {"varnames": ["DYE"], "npoin": 3, "nelem": 1, "x_origin": 0,
         "y_origin": 0, "ntimestep": 1, "fields": "telemac_result_fields.npz"})

    def run(argv, **kw):
        seen["argv"] = argv
        return leave(argv, **kw)

    monkeypatch.setattr(R.subprocess, "run", run)
    R.read_selafin(slf)

    argv = seen["argv"]
    # the box is sealed: the read needs the file and nothing else.
    assert argv[:5] == ["docker", "run", "--rm", "--network", "none"]
    # the result's directory goes in READ-ONLY; only the scratch dir is writable.
    assert f"{slf.resolve().parent}:/in:ro" in argv
    assert f"{R.drivers_dir()}:/drivers:ro" in argv
    assert "/drivers/telemac_result_driver.py" in argv


def test_the_fields_the_driver_left_become_the_reader_s_answer(tmp_path,
                                                               monkeypatch):
    slf = tmp_path / "res_coastal.slf"
    slf.write_bytes(b"result")
    monkeypatch.setattr(R.subprocess, "run", _driver_leaves(
        {"x": np.array([0.0, 1.0, 0.0]), "y": np.array([0.0, 0.0, 1.0]),
         "ikle": np.array([[0, 1, 2]]), "times": np.array([0.0, 60.0]),
         "v0": np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]),
         "v1": np.zeros((2, 3))},
        {"varnames": ["WATER DEPTH", "FREE SURFACE"], "npoin": 3, "nelem": 1,
         "x_origin": 425000, "y_origin": 5150000, "ntimestep": 2,
         "fields": "telemac_result_fields.npz"}))

    mesh = R.read_selafin(slf)

    # the variable NAME is the engine's own, with no unit glued to it.
    assert mesh["varnames"] == ["WATER DEPTH", "FREE SURFACE"]
    assert mesh["data"]["WATER DEPTH"].shape == (2, 3)
    assert mesh["data"]["WATER DEPTH"][1].max() == pytest.approx(6.0)
    assert mesh["npoin"] == 3 and mesh["nelem"] == 1
    # the origin is REPORTED, never applied: the coordinates stay as the file
    # stores them, because every postprocess adds the origin it recovered itself.
    assert (mesh["x_origin"], mesh["y_origin"]) == (425000, 5150000)
    assert mesh["x"].max() == pytest.approx(1.0)


def test_the_scratch_directory_does_not_outlive_the_read(tmp_path, monkeypatch):
    """A result is up to a hundred megabytes; a leaked copy per read fills a disk."""
    slf = tmp_path / "r2d_river.slf"
    slf.write_bytes(b"result")
    scratch: list[Path] = []

    leave = _driver_leaves(
        {"x": np.zeros(3), "y": np.zeros(3), "ikle": np.array([[0, 1, 2]]),
         "times": np.array([0.0]), "v0": np.zeros((1, 3))},
        {"varnames": ["DYE"], "npoin": 3, "nelem": 1, "x_origin": 0,
         "y_origin": 0, "ntimestep": 1, "fields": "telemac_result_fields.npz"})

    def run(argv, **kw):
        scratch.append(_scratch_of(argv))
        return leave(argv, **kw)

    monkeypatch.setattr(R.subprocess, "run", run)
    R.read_selafin(slf)
    assert scratch and not scratch[0].exists()


def test_a_refusal_names_the_file_and_what_the_engine_said(tmp_path, monkeypatch):
    slf = tmp_path / "truncated.slf"
    slf.write_bytes(b"")
    monkeypatch.setattr(R.subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 1, "", "not a result"))
    with pytest.raises(R.SelafinReadError) as ei:
        R.read_selafin(slf)
    assert "truncated.slf" in str(ei.value)
    assert "not a result" in str(ei.value)


@pytest.mark.parametrize("module", _READERS)
def test_no_reader_on_this_side_parses_the_format(module):
    """The byte layout is the engine's to know.

    The reader this replaced refused a truncated result the engine reads without
    complaint, and handed every consumer a variable name with the record's unit
    still glued on - two ways of being wrong about a format nobody here owns.
    """
    tree = ast.parse((_REPO / module).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert "struct" not in imported
