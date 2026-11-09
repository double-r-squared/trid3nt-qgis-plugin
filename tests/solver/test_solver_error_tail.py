"""A failed solve's error carries the tail of the engine's own log, for every engine; a success carries none."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import trid3nt_server.workflows.solver.solver as solver_mod
from trid3nt_server.store import objects as storage
from trid3nt_server.workflows.geoclaw.engine import _spec as geoclaw_spec
from trid3nt_server.workflows.modflow.engine import _spec as modflow_spec
from trid3nt_server.workflows.telemac.engine import _spec as telemac_spec

_TELEMAC = (" ITERATION      500    TIME:  6 MN\n"
            " THE FOLLOWING KEYWORD IS MANDATORY: BOUNDARY CONDITIONS FILE\n"
            " PLANTE: PROGRAM STOPPED AFTER AN ERROR\n"
            " RETURNING EXIT CODE:            2\n")
_MODFLOW = ("  Solving:  Stress period:     1    Time step:     1\n"
            " ERROR REPORT:\n"
            "  1. Model did not converge in outer iteration 100\n"
            " Run time:   0.01 seconds\n")
_GEOCLAW = (" Reading data file: claw.data\n"
            "forrtl: severe (174): SIGSEGV, segmentation fault occurred\n"
            "Image              PC                Routine            Line        Source\n")

_ENGINES = [
    ("telemac", telemac_spec, "full_listing.log", _TELEMAC, "PLANTE: PROGRAM STOPPED AFTER AN ERROR"),
    ("modflow", modflow_spec, "mfsim.lst", _MODFLOW, "Model did not converge in outer iteration 100"),
    ("geoclaw", geoclaw_spec, "geoclaw.stdout", _GEOCLAW, "forrtl: severe (174)"),
]


class _Store:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, Bucket: str, Key: str, Body: Any, **_kw: Any) -> dict:  # noqa: N803
        self.objects[(Bucket, Key)] = Body.read() if hasattr(Body, "read") else Body
        return {}


@pytest.fixture()
def store():
    fake = _Store()
    storage.set_client(fake)
    try:
        yield fake
    finally:
        storage.set_client(None)


def _supervise(spec, rundir: Path, exit_code: int) -> dict[str, Any]:
    for name in (spec.stdout_name, spec.stderr_name):
        (rundir / name).touch()
    proc = subprocess.Popen([sys.executable, "-c", f"raise SystemExit({exit_code})"])
    run = solver_mod._LocalRun(
        run_id="R1", rundir=rundir, runs_bucket="runs", proc=proc, output_patterns=[],
        started_at="2026-11-09T00:00:00Z", stdout_path=rundir / spec.stdout_name,
        stderr_path=rundir / spec.stderr_name, spec=spec)
    solver_mod._supervise_local_run(run)
    return json.loads(storage.client().objects[("runs", "R1/completion.json")])


@pytest.mark.parametrize("engine,factory,log,text,cause", _ENGINES)
def test_failed_solve_carries_the_engine_log_tail(store, tmp_path, engine, factory, log, text, cause):
    (tmp_path / log).write_text(text, encoding="utf-8")
    completion = _supervise(factory(), tmp_path, 2)
    assert completion["status"] == "error"
    assert cause in completion["error"]
    assert completion["error"].startswith(f"{engine} exited with non-zero code 2")
    assert completion["listing_tail"] == text.strip()


@pytest.mark.parametrize("engine,factory,log,text,cause", _ENGINES)
def test_tail_is_bounded(store, tmp_path, engine, factory, log, text, cause):
    (tmp_path / log).write_text("x" * 20000 + text, encoding="utf-8")
    completion = _supervise(factory(), tmp_path, 2)
    assert completion["listing_tail"].endswith(text.strip())
    assert len(completion["listing_tail"]) == solver_mod.LISTING_TAIL_CHARS


@pytest.mark.parametrize("engine,factory,log,text,cause", _ENGINES)
def test_failed_solve_with_no_log_states_no_tail(store, tmp_path, engine, factory, log, text, cause):
    completion = _supervise(factory(), tmp_path, 2)
    assert completion["status"] == "error"
    assert "listing tail" not in completion["error"]
    assert "listing_tail" not in completion


@pytest.mark.parametrize("engine,factory,log,text,cause", _ENGINES)
def test_successful_solve_carries_no_tail(store, tmp_path, engine, factory, log, text, cause):
    (tmp_path / log).write_text(text, encoding="utf-8")
    (tmp_path / f"{engine}_metrics.json").write_text(json.dumps({"correct_end": True}), encoding="utf-8")
    completion = _supervise(factory(), tmp_path, 0)
    assert completion["status"] == "ok"
    assert completion["error"] is None
    assert "listing_tail" not in completion
