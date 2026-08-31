"""Offline tests for the TELEMAC worker entrypoint.

TELEMAC-free by construction: the dispatch, the strict gate and the metrics
envelope are exercised without telapy, without the solver binaries and without
the network. The one test that spawns a real child does so to prove the seam the
crash isolation rests on - the child dies, the parent still writes the metrics.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workers.telemac import entrypoint as E


def _metrics(tmp_path: Path) -> dict:
    return json.loads((tmp_path / E.METRICS_FILENAME).read_text())


def _write_manifest(tmp_path: Path, body) -> list[str]:
    (tmp_path / "manifest.json").write_text(
        body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return ["--data-dir", str(tmp_path)]


def _case(tmp_path: Path, **over) -> dict:
    (tmp_path / "t2d.cas").write_text("/ deck\n", encoding="utf-8")
    case = {"module": "telemac2d", "steering": "t2d.cas",
            "results": ["r2d.slf"], "family": "river_dye",
            "echo": {"utm_epsg": 32612, "npoin": 4211, "nelem": 8080}}
    case.update(over)
    return {"case": case, "run_id": "RUN123"}


# --------------------------------------------------------------------------- #
# The contract stamps
# --------------------------------------------------------------------------- #


def test_the_parser_stamp_is_the_unified_one():
    assert E._PARSER_VERSION == "telemac-unified-1"


def test_the_four_engines_a_case_may_name_come_from_telapy():
    assert set(E._MODULES) == {"telemac2d", "telemac3d", "tomawac", "artemis"}
    assert all(path.startswith("telapy.api.")
               for path, _cls in E._MODULES.values())


def test_the_dispatch_is_one_table_of_three_sections():
    assert set(E._DISPATCH) == {"case", "agitation", "stratified"}


# --------------------------------------------------------------------------- #
# Which runner a case gets - the telapy arm, or the module's own launcher
# --------------------------------------------------------------------------- #


def test_an_uncoupled_case_runs_on_the_telapy_arm():
    argv = E._solve_argv("telemac2d", "t2d.cas", None, "")
    assert argv[:1] == [__import__("sys").executable]
    assert argv[-4:] == ["--solve", "telemac2d", "--steering", "t2d.cas"]


def test_only_the_two_measured_couplings_leave_the_telapy_arm():
    """The deviation is SCOPED: a coupling nobody measured stays on the API arm."""
    assert E._LAUNCHER_COUPLINGS == frozenset({"waqtel", "gaia"})
    assert E._solve_argv("telemac2d", "t2d.cas", None, "nestor")[0] != "telemac2d.py"


@pytest.mark.parametrize("coupling", ["waqtel", "gaia"])
def test_a_coupled_case_runs_the_modules_own_launcher(coupling):
    assert E._solve_argv("telemac2d", "t2d.cas", None, coupling) == [
        "telemac2d.py", "t2d.cas"]


def test_the_launcher_reads_the_user_fortran_off_the_deck_not_the_argv():
    """The steering file names it; a second channel could name a second thing."""
    assert E._solve_argv("telemac2d", "t2d.cas", "user_fortran", "waqtel") == [
        "telemac2d.py", "t2d.cas"]


def test_a_waqtel_case_is_dispatched_through_the_launcher(tmp_path, monkeypatch):
    seen = {}

    def _child(data_dir, argv):
        seen["argv"] = argv
        (data_dir / "r2d.slf").write_bytes(b"SELAFIN")
        return 0

    monkeypatch.setattr(E, "_run_child", _child)
    rc = E.main(_write_manifest(tmp_path, _case(tmp_path, coupling="waqtel")))
    assert rc == 0
    assert seen["argv"] == ["telemac2d.py", "t2d.cas"]


# --------------------------------------------------------------------------- #
# The one strict gate
# --------------------------------------------------------------------------- #


def test_the_gate_keeps_known_keys_and_drops_the_pinned_ones():
    clean = E._strict_section("agitation", {"wave_period_s": 8.0,
                                            "workdir": "/etc/nope",
                                            "mode": "diffraction"},
                              {"wave_period_s"}, drop=("workdir", "mode"))
    assert clean == {"wave_period_s": 8.0}


def test_the_gate_refuses_an_unknown_key_and_names_the_parser():
    """A dropped key no-ops the knob the caller meant to set, silently."""
    with pytest.raises(E.UnknownManifestFieldError) as err:
        E._strict_section("case", {"module": "telemac2d", "bogus": 1},
                          E._CASE_FIELDS)
    assert "bogus" in str(err.value)
    assert E._PARSER_VERSION in str(err.value)


# --------------------------------------------------------------------------- #
# Manifest refusals
# --------------------------------------------------------------------------- #


def test_a_manifest_that_is_not_an_object_is_a_typed_error(tmp_path):
    rc = E.main(_write_manifest(tmp_path, "[1, 2, 3]"))
    assert rc == 2
    assert _metrics(tmp_path)["error_code"] == "TELEMAC_MANIFEST_INVALID"


def test_a_missing_manifest_is_a_typed_error_not_a_default_run(tmp_path):
    rc = E.main(["--data-dir", str(tmp_path)])
    assert rc == 2
    assert _metrics(tmp_path)["correct_end"] is False


def test_a_manifest_naming_no_runnable_section_refuses(tmp_path):
    rc = E.main(_write_manifest(tmp_path, {"reach": {"distance_km": 4.0}}))
    assert rc == 2
    assert "no runnable section" in _metrics(tmp_path)["error"]


def test_a_case_with_an_unknown_field_refuses(tmp_path):
    rc = E.main(_write_manifest(tmp_path, _case(tmp_path, channel_width_m=60.0)))
    assert rc == 5
    assert _metrics(tmp_path)["error_code"] == "TELEMAC_MANIFEST_UNKNOWN_FIELD"


def test_a_case_naming_an_engine_the_image_has_no_class_for_refuses(tmp_path):
    rc = E.main(_write_manifest(tmp_path, _case(tmp_path, module="sisyphe")))
    assert rc == 5
    assert _metrics(tmp_path)["error_code"] == "TELEMAC_CASE_MODULE_UNKNOWN"


def test_a_case_whose_deck_was_never_staged_refuses(tmp_path):
    rc = E.main(_write_manifest(tmp_path, _case(tmp_path, steering="absent.cas")))
    assert rc == 5
    assert _metrics(tmp_path)["error_code"] == "TELEMAC_CASE_STEERING_MISSING"


def test_a_case_declaring_no_results_refuses(tmp_path):
    """On an empty list the success convention collapses back to the exit code."""
    rc = E.main(_write_manifest(tmp_path, _case(tmp_path, results=[])))
    assert rc == 5
    assert _metrics(tmp_path)["error_code"] == "TELEMAC_CASE_NO_RESULTS"


# --------------------------------------------------------------------------- #
# The solve time bound
# --------------------------------------------------------------------------- #


def test_the_solve_bound_defaults_to_a_day_and_the_knob_states_it(monkeypatch):
    monkeypatch.delenv(E._SOLVE_TIMEOUT_ENV, raising=False)
    assert E._solve_timeout_s() == E._SOLVE_TIMEOUT_DEFAULT_S == 86400.0
    monkeypatch.setenv(E._SOLVE_TIMEOUT_ENV, "12.5")
    assert E._solve_timeout_s() == 12.5
    monkeypatch.setenv(E._SOLVE_TIMEOUT_ENV, "soon")
    assert E._solve_timeout_s() == E._SOLVE_TIMEOUT_DEFAULT_S


def test_a_child_that_outruns_the_bound_is_killed_and_still_reports(tmp_path,
                                                                    monkeypatch):
    """The metrics-always clause: an expiry is a typed report, not a wedged box."""
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time\nprint('TIME LOOP', flush=True)\n"
                       "time.sleep(60)\n", encoding="utf-8")
    monkeypatch.setenv(E._SOLVE_TIMEOUT_ENV, "0.5")
    monkeypatch.setattr(E, "__file__", str(sleeper))
    rc = E.main(_write_manifest(tmp_path, _case(tmp_path)))
    assert rc == 5
    metrics = _metrics(tmp_path)
    assert metrics["error_code"] == "TELEMAC_SOLVE_TIMEOUT"
    assert E._SOLVE_TIMEOUT_ENV in metrics["error"]
    assert metrics["correct_end"] is False


# --------------------------------------------------------------------------- #
# The metrics envelope
# --------------------------------------------------------------------------- #


def test_a_clean_child_that_wrote_its_results_is_the_run_succeeding(tmp_path,
                                                                    monkeypatch):
    def _child(data_dir, argv):
        (data_dir / "r2d.slf").write_bytes(b"SELAFIN")
        return 0

    monkeypatch.setattr(E, "_run_child", _child)
    rc = E.main(_write_manifest(tmp_path, _case(tmp_path)))
    assert rc == 0
    metrics = _metrics(tmp_path)
    assert metrics["status"] == "ok" and metrics["correct_end"] is True
    assert metrics["module"] == "telemac2d" and metrics["family"] == "river_dye"
    assert metrics["run_id"] == "RUN123" and isinstance(metrics["wall_s"], float)
    # the echo is the SERVER's measurement, copied rather than re-derived
    assert metrics["utm_epsg"] == 32612 and metrics["npoin"] == 4211
    assert "listing_tail" not in metrics


def test_a_clean_exit_that_wrote_no_result_is_not_a_solve(tmp_path, monkeypatch):
    """Both old conventions retire: the exit code alone never decides this."""
    monkeypatch.setattr(E, "_run_child", lambda data_dir, argv: 0)
    (tmp_path / E.LISTING_FILENAME).write_text("PLANTE\n", encoding="utf-8")
    rc = E.main(_write_manifest(tmp_path, _case(tmp_path)))
    assert rc == 1
    metrics = _metrics(tmp_path)
    assert metrics["error_code"] == "TELEMAC_RESULTS_MISSING"
    assert "r2d.slf" in metrics["error"]
    assert "PLANTE" in metrics["listing_tail"]


def test_a_user_fortran_case_hands_the_child_its_fortran(tmp_path, monkeypatch):
    seen = {}

    def _child(data_dir, argv):
        seen["argv"] = argv
        (data_dir / "r2d.slf").write_bytes(b"SELAFIN")
        return 0

    monkeypatch.setattr(E, "_run_child", _child)
    E.main(_write_manifest(tmp_path,
                           _case(tmp_path, user_fortran="user_fortran")))
    assert seen["argv"][-2:] == ["--user-fortran", "user_fortran"]


# --------------------------------------------------------------------------- #
# Crash isolation, through a real child
# --------------------------------------------------------------------------- #


def test_a_child_that_dies_still_leaves_the_metrics_written(tmp_path):
    """A Fortran STOP kills the process it runs in; the metrics write survives.

    The child here dies on telapy being absent rather than on a solver abort, but
    the seam under test is the same one: whatever the child does, the parent
    reads its exit code, keeps its output as the listing, and writes the run's
    only report.
    """
    rc = E.main(_write_manifest(tmp_path, _case(tmp_path)))
    assert rc == 1
    metrics = _metrics(tmp_path)
    assert metrics["status"] == "error" and metrics["correct_end"] is False
    assert metrics["error_code"] == "TELEMAC_SOLVE_FAILED"
    assert "telapy" in (tmp_path / E.LISTING_FILENAME).read_text()
    assert "telapy" in metrics["listing_tail"]
