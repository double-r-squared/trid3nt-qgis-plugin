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
# The stepped loop, and the one point between steps
# --------------------------------------------------------------------------- #


class _WholeRunStudy:
    """A telapy study the image gave no per-step call: the lifecycle, recorded."""

    def __init__(self, steering, user_fortran=None, model=None):
        self.steering = steering
        self.user_fortran = user_fortran
        self._model = dict(model or {"MODEL.NTIMESTEPS": 3})
        self.calls: list[str] = []

    def get(self, name):
        if name not in self._model:
            raise KeyError(name)
        return self._model[name]

    def set_case(self):
        self.calls.append("set_case")

    def init_state_default(self):
        self.calls.append("init")

    def run_all_time_steps(self):
        self.calls.append("all")

    def finalize(self):
        self.calls.append("finalize")


class _Study(_WholeRunStudy):
    """The four classes in the image: telapy's own per-step call is inherited."""

    def run_one_time_step(self):
        self.calls.append("step")


def _telapy(monkeypatch, factory):
    """Point ``_MODULES``' import at ``factory`` -> the study it built."""
    built = {}

    def _make(steering, user_fortran=None):
        built["study"] = factory(steering, user_fortran=user_fortran)
        return built["study"]

    monkeypatch.setattr(E.importlib, "import_module",
                        lambda path: type("M", (), {"Telemac2d": _make}))
    return built


def test_the_loop_drives_the_engines_own_per_step_call(tmp_path, monkeypatch):
    built = _telapy(monkeypatch, lambda s, user_fortran=None: _Study(s))
    assert E._solve_in_process("telemac2d", "t2d.cas", None) == 0
    assert built["study"].calls == ["set_case", "init", "step", "step", "step",
                                    "finalize"]


def test_the_step_count_is_the_models_own(monkeypatch):
    assert E._step_count(_Study("c", model={"MODEL.NTIMESTEPS": 7})) == 7


def test_a_finite_volume_run_advances_its_whole_loop_in_one_call():
    """The engine collapses it to one; driving 7 would run seven whole loops."""
    assert E._step_count(_Study("c", model={"MODEL.NTIMESTEPS": 7,
                                            "MODEL.EQUATION": "SAINT-VENANT VF"})) == 1
    assert E._step_count(_Study("c", model={"MODEL.NTIMESTEPS": 7,
                                            "MODEL.EQUATION": "SAINT-VENANT FE"})) == 7


def test_a_class_with_no_per_step_call_keeps_the_whole_run_wrapper(tmp_path,
                                                                   monkeypatch):
    built = _telapy(monkeypatch,
                    lambda s, user_fortran=None: _WholeRunStudy(s))
    E._solve_in_process("telemac2d", "t2d.cas", None)
    assert built["study"].calls == ["set_case", "init", "all", "finalize"]


def test_there_is_one_hook_point_and_it_fires_once_per_step(monkeypatch):
    """The seam frames, progress and steering attach to - a no-op until then."""
    seen: list[tuple] = []
    monkeypatch.setattr(E, "_on_step",
                        lambda study, step, steps: seen.append((step, steps)))
    _telapy(monkeypatch, lambda s, user_fortran=None: _Study(s))
    E._solve_in_process("telemac2d", "t2d.cas", None)
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_the_hook_ships_doing_nothing(monkeypatch):
    assert E._on_step(_Study("c"), 1, 1) is None


# --------------------------------------------------------------------------- #
# Continuation: the deck restarts, the worker only stages
# --------------------------------------------------------------------------- #


def test_a_continued_case_runs_once_its_previous_file_is_staged(tmp_path,
                                                                monkeypatch):
    def _child(data_dir, argv):
        (data_dir / "r2d.slf").write_bytes(b"SELAFIN")
        return 0

    monkeypatch.setattr(E, "_run_child", _child)
    (tmp_path / "previous.slf").write_bytes(b"SELAFIN")
    rc = E.main(_write_manifest(
        tmp_path, _case(tmp_path, continue_from="previous.slf")))
    assert rc == 0
    assert _metrics(tmp_path)["correct_end"] is True


def test_a_continuation_whose_previous_run_was_never_staged_refuses(tmp_path):
    rc = E.main(_write_manifest(
        tmp_path, _case(tmp_path, continue_from="previous.slf")))
    assert rc == 5
    metrics = _metrics(tmp_path)
    assert metrics["error_code"] == "TELEMAC_CASE_PREVIOUS_MISSING"
    assert "previous.slf" in metrics["error"]


@pytest.mark.parametrize("coupling", ["waqtel", "gaia"])
def test_a_launcher_deviation_case_refuses_to_be_continued(tmp_path, coupling):
    """It runs whole-process behind the deviation; continuing it is unrun."""
    (tmp_path / "previous.slf").write_bytes(b"SELAFIN")
    rc = E.main(_write_manifest(tmp_path, _case(
        tmp_path, coupling=coupling, continue_from="previous.slf")))
    assert rc == 5
    assert _metrics(tmp_path)["error_code"] == "TELEMAC_CASE_NOT_CONTINUABLE"


@pytest.mark.parametrize("section", ["agitation", "stratified"])
def test_the_legacy_builders_have_no_continuation_to_ask_for(section):
    """Only the case section learned the word; a builder config never carries it.

    The builders author their own domain in-container and are never extended, so
    asking one to continue is refused by the same gate that refuses a typo.
    """
    assert "continue_from" in E._CASE_FIELDS
    with pytest.raises(E.UnknownManifestFieldError) as exc:
        E._strict_section(section, {"continue_from": "previous.slf"},
                          {"bbox", "wave_period_s"}, drop=("workdir", "mode"))
    assert "continue_from" in str(exc.value)


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
