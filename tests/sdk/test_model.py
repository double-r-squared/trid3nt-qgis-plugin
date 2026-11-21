"""A Model over telemac_dye_release from plain Python: set checks, the form, no pick, the stance, the record."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trid3nt import case
from trid3nt.errors import DeclarativeError
from trid3nt.store.journal import read_records
from trid3nt.workflows.runtime import fill as fill_mod
from trid3nt.workflows.runtime.workflow import RunResult
from trid3nt.workflows.telemac.workflow import TelemacWorkflow

REPO = Path(__file__).resolve().parents[2]

#: The zero-fetch dye release: a drawn ring, a stated depth, a stated flow and outflow level.
_ZERO_FETCH = dict(
    release=[-122.669784, 45.518485], spill_duration_s=300.0, event_time="now", mesh_resolution_m=40.0,
    DURATION=1800.0, discharge=56.6, level=0.0, bed=4.0,
    domain=[[-122.6769, 45.5153], [-122.6615, 45.5153], [-122.6615, 45.5198], [-122.6769, 45.5198]])


@pytest.mark.parametrize("name, value, code, named", [
    ("FRICTION_COEFFICIENT", "abc", "KEYWORD_REFUSED", "FRICTION COEFFICIENT"),
    ("LAW_OF_BOTTOM_FRICTION", 99, "KEYWORD_REFUSED", "LAW OF BOTTOM FRICTION"),
    ("spill_duration_s", -5, "GATE_INPUT_REQUIRED", "spill_duration_s"),
])
def test_a_bad_value_set_raises_the_input_s_own_refusal_naming_it_and_leaves_the_value(name, value, code, named):
    m = case("sdk-set").telemac_dye_release()
    before = getattr(m, name)
    with pytest.raises(DeclarativeError) as refused:
        setattr(m, name, value)
    assert refused.value.error_code == code and named in str(refused.value)
    assert getattr(m, name) == before


def test_a_locked_option_set_raises_its_refusal_naming_it():
    m = case("sdk-set").calibrate()
    with pytest.raises(DeclarativeError) as refused:
        m.save_binary = True
    assert refused.value.error_code == "KEYWORD_REFUSED" and "save_binary" in str(refused.value)


def test_every_spelling_of_a_name_is_the_one_lookup_and_repr_says_each_origin():
    m = case("sdk-names").telemac_dye_release(release=[-122.669784, 45.518485])
    assert m.FRICTION_COEFFICIENT == 33.0
    m.friction_coefficient = 35
    assert m["FRICTION COEFFICIENT"] == m.FRICTION_COEFFICIENT == 35
    m.waqtel.WATER_DENSITY = 999.0
    assert m["waqtel: WATER_DENSITY"] == 999.0
    shown = repr(m)
    assert "FRICTION_COEFFICIENT = 35  [user]" in shown
    assert "LAW_OF_BOTTOM_FRICTION = 3  [template]" in shown
    assert "waqtel: WATER_DENSITY = 999.0  [user]" in shown
    assert "release = [-122.669784, 45.518485]  [user]" in shown
    with pytest.raises(AttributeError):
        m.no_such_name


def test_with_no_walk_registered_an_unstated_sourced_input_refuses_naming_the_ranked_candidates(monkeypatch):
    called = []

    async def _runner(runner, kwargs, label):
        called.append(runner)
        raise RuntimeError("no source is called")

    monkeypatch.setattr(fill_mod, "_call_runner", _runner)
    monkeypatch.setattr(fill_mod, "_RANKED", {})
    form = case("sdk-nopick").telemac_dye_release(release=[-122.669784, 45.518485], event_time="now").fill()
    domain = form["domain"]
    assert not form.ready and form.refusal[0] == "DATA_SOURCE_UNSTATED" and called == []
    assert domain.state == "rejected" and domain.code == "DATA_SOURCE_UNSTATED" and "domain" in domain.reason
    assert domain.remedies and all(name.startswith("fetch_") for name in domain.remedies)
    assert form["release"].state == "accepted" and form["release"].origin.value == "user"
    assert form["spill_duration_s"].state == "defaulted" and form["spill_duration_s"].origin.value == "template"


def test_with_no_walk_registered_a_named_source_that_held_nothing_refuses_naming_the_rest(monkeypatch):
    called = []

    async def _runner(runner, kwargs, label):
        called.append(runner)
        raise RuntimeError("held nothing")

    monkeypatch.setattr(fill_mod, "_call_runner", _runner)
    monkeypatch.setattr(fill_mod, "_RANKED", {})
    stated = {key: value for key, value in _ZERO_FETCH.items() if key != "bed"}
    unstated = case("sdk-named").telemac_dye_release(**stated).fill()["bed"]
    top, *rest = unstated.remedies
    bed = case("sdk-named").telemac_dye_release(**stated, bed={"source": top}).fill()["bed"]
    assert called == [top]
    assert bed.code == "DATA_SOURCE_UNSTATED" and f"{top} held nothing" in bed.reason
    assert list(bed.remedies) == rest


def test_with_the_harness_walk_registered_the_same_fill_asks_the_ranked_candidates(monkeypatch):
    called = []

    async def _runner(runner, kwargs, label):
        called.append(runner)
        raise RuntimeError("held nothing")

    monkeypatch.setattr(fill_mod, "_call_runner", _runner)
    form = case("sdk-walk").telemac_dye_release(release=[-122.669784, 45.518485], event_time="now").fill()
    assert called and form["domain"].code != "DATA_SOURCE_UNSTATED"


class _Published:
    """The typed result a stubbed launch publishes: what the publish stage copies notes onto."""

    run_id = "RUNSDKTEST"

    def model_copy(self, update):
        return self


def _stub_launch(monkeypatch):
    async def launch(self, state):
        return RunResult(value=_Published(), results={})

    monkeypatch.setattr(TelemacWorkflow, "launch", launch)


def test_the_record_names_the_template_case_model_and_version(monkeypatch, tmp_path):
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path))
    _stub_launch(monkeypatch)
    m = case("sdk-record").telemac_dye_release(**_ZERO_FETCH)
    result = m.run()
    record = read_records()[-1]
    assert (record["template"], record["case"], record["model"], record["version"]) == (
        "telemac_dye_release", "sdk-record", "telemac_dye_release", m.version)
    assert result.run_id == "RUNSDKTEST" and result.record == record
    m.FRICTION_COEFFICIENT = 35
    assert m.version != record["version"]


_STANCE = r"""
import json, sys
sys.modules["trid3nt_harness"] = None
sys.modules["trid3nt_server"] = None
from trid3nt.inputs import review
read, asked = [], []
_setting = review.setting
review.setting = lambda name, *a, **k: read.append(name) or _setting(name, *a, **k)

async def _ask(**card):
    asked.append(card.get("tool_name"))
    raise AssertionError("a gate was asked")

review.register_gate(review=_ask, draw=_ask)
from trid3nt.workflows.runtime.workflow import RunResult
from trid3nt.workflows.telemac.workflow import TelemacWorkflow

class Published:
    run_id = "RUNSTANCE"
    def model_copy(self, update):
        return self

async def launch(self, state):
    outcome = await review.gate_input_review(tool_name=self.name, entries=[], params={})
    assert outcome.proceed
    return RunResult(value=Published(), results={})

TelemacWorkflow.launch = launch
from trid3nt import case
m = case("sdk-stance").telemac_dye_release(**json.loads(sys.argv[1]))
form = m.fill()
result = m.run()
print(json.dumps({"ready": form.ready, "ran": type(result).__name__, "read": read, "asked": asked,
                  "loaded": sorted(n for n in sys.modules if n.split(".")[0] in ("trid3nt_harness", "trid3nt_server")
                                   and sys.modules[n] is not None)}))
"""


def test_fill_and_run_with_harness_and_server_blocked_never_ask_and_never_read_auto(tmp_path):
    env = {**os.environ, "TRID3NT_DEV_PERSISTENCE_DIR": str(tmp_path)}
    done = subprocess.run([sys.executable, "-c", _STANCE, json.dumps(_ZERO_FETCH)], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert (seen["ready"], seen["ran"], seen["asked"], seen["loaded"]) == (True, "Result", [], [])
    assert "AUTO" not in seen["read"]
