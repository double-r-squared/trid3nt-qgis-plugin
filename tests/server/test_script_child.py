"""A live launch runs the saved script with the reviewed deck in another interpreter."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
import runpy

import pytest

from trid3nt import case
from trid3nt.store import objects
from trid3nt.workflows.runtime import script
from trid3nt.workflows.runtime.fill import Fill, production
from trid3nt.workflows.runtime.workflow import Deck
from trid3nt.workflows.solver.stop import Stopped
from trid3nt_server.server.dispatch import script_child
from trid3nt_server.server.dispatch.case_script import archive

_RUN = "01M833H3N0B90A6M7GJ3HND42S"
_INPUTS = Path(__file__).resolve().parents[1] / "sdk" / "test_fill_deck.py"

_BOOT = r"""
import asyncio
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
sys.modules['trid3nt_harness'] = None
sys.modules['trid3nt_server'] = None
from trid3nt.errors import DeclarationError
from trid3nt.render.emission import current_emitter
from trid3nt.render.formats import record_run_outputs
from trid3nt.sdk.script_run import current, main
from trid3nt.store import objects, journal
from trid3nt.workflows.runtime.workflow import RunResult
from trid3nt.workflows.solver.stop import Stopped
from trid3nt.workflows.telemac.workflow import TelemacWorkflow
from trid3nt_contracts.execution import LayerURI

store = objects.FileStore(os.environ['SCRIPT_TEST_STORE'])
objects.client = objects.served_client = lambda: store
journal.journal_path = lambda: Path('journal.jsonl')

async def no_prepare(*args):
    raise AssertionError('the script rebuilt the reviewed deck')

async def solve(self, deck):
    assert current_emitter() is None
    assert deck.run_id == os.environ['SCRIPT_TEST_RUN']
    cores = current().handoff['cores']
    assert int(os.environ['OPENBLAS_NUM_THREADS']) == cores
    assert len(os.sched_getaffinity(0)) <= cores
    mode = os.environ['SCRIPT_TEST_MODE']

    async def engine():
        current().event('dispatch', 'telemac', 'telemac2d', SimpleNamespace(), cores)
        current().event('progress', {'run_id': deck.run_id, 'step': 'first'})
        current().event('progress', {'run_id': deck.run_id, 'step': 'second'})
        if mode == 'killed':
            os.kill(os.getpid(), signal.SIGKILL)
        if mode == 'refused':
            raise DeclarationError('The solver refused the deck.', error_code='TELEMAC_STUB_REFUSED')
        if mode in ('stopped', 'budget'):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                current().event('terminal', None)
                if mode == 'stopped':
                    await asyncio.sleep(0.3)
                raise Stopped(RunResult(value=None, results={self.solve_step: {'run_id': deck.run_id}}), None, 'stopped by the user')
        current().event('terminal', SimpleNamespace(status='complete'))

    layer = LayerURI(layer_id='layer', name='Depth', layer_type='vector',
                     uri=f's3://runs/{deck.run_id}/out.fgb')
    await self._stage({}, 'solve', engine)

    async def outputs():
        record_run_outputs([layer])

    await self._stage({}, 'outputs', outputs)
    return RunResult(value=layer, results={self.solve_step: {'run_id': deck.run_id}})

TelemacWorkflow.prepare = no_prepare
TelemacWorkflow.solve = solve
main()
"""


class Emitter:
    def __init__(self):
        self.events = []
        self.started = asyncio.Event()

    @asynccontextmanager
    async def substep(self, label):
        self.events.append(("begin", label))
        try:
            yield
        except asyncio.CancelledError:
            self.events.append(("cancelled", label))
            raise
        else:
            self.events.append(("end", label))

    async def emit_solve_progress(self, payload):
        self.events.append(("progress", payload["step"]))
        self.started.set()

    async def add_loaded_layer(self, layer):
        self.events.append(("layer", layer.name))


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    store = objects.FileStore(tmp_path / "store")
    monkeypatch.setattr(objects, "client", lambda: store)
    monkeypatch.setattr(objects, "runs_bucket", lambda: "runs")
    monkeypatch.setattr(script_child, "_BOOT", _BOOT)
    monkeypatch.setattr(script_child, "_docker_kill", lambda run_id: None)
    monkeypatch.setenv("SCRIPT_TEST_STORE", str(store.root))
    monkeypatch.setenv("SCRIPT_TEST_RUN", _RUN)
    monkeypatch.setenv("SCRIPT_TEST_MODE", "normal")
    monkeypatch.setattr(script_child, "setting", lambda name: {"LOCAL_CORES": 1, "SOLVE_BUDGET_S": 0}[name])
    store.put_object(Bucket="cache", Key="deck.cas", Body=b"reviewed deck")
    state = Fill(case(tmp_path / "declaration").telemac_dye_release()._workflow)
    production(state)
    deck = Deck(state, run_id=_RUN, staged={"inputs": [{"dest": "deck.cas", "gs_uri": "s3://cache/deck.cas"}]})
    capture = script.Capture(dict(runpy.run_path(str(_INPUTS))["_ZERO_FETCH"]), lambda *args: {})
    receipt = archive(tmp_path / "chat", capture, deck)
    return capture.path, deck, receipt, store


@pytest.mark.asyncio
async def test_child_solves_reviewed_deck_and_relays_ordered_output(prepared, monkeypatch):
    emitter = Emitter()
    monkeypatch.setattr(script_child, "current_emitter", lambda: emitter)
    path, deck, receipt, _ = prepared
    outcome = await asyncio.wait_for(script_child.run(path, deck, receipt), 10)
    assert outcome.value.uri == f"s3://runs/{_RUN}/out.fgb"
    assert emitter.events == [("begin", "solve"), ("progress", "first"), ("progress", "second"),
                              ("end", "solve"), ("begin", "outputs"), ("layer", "Depth"), ("end", "outputs")]
    record = json.loads(path.with_name("journal.jsonl").read_text())
    assert record["case_script"]["checked"] is True
    assert record["case_script"]["code"] == "CODEGEN_CHECKED"
    assert record["outputs"][0]["name"] == outcome.value.name


@pytest.mark.asyncio
async def test_changed_deck_is_refused_before_the_solve(prepared, monkeypatch):
    emitter = Emitter()
    monkeypatch.setattr(script_child, "current_emitter", lambda: emitter)
    path, deck, receipt, store = prepared
    store.put_object(Bucket="cache", Key="deck.cas", Body=b"changed deck")
    with pytest.raises(Exception, match="reviewed script") as result:
        await asyncio.wait_for(script_child.run(path, deck, receipt), 10)
    assert result.value.error_code == "SCRIPT_DECK_STALE"
    assert emitter.events == []


@pytest.mark.asyncio
async def test_stop_gets_the_childs_stopped_record(prepared, monkeypatch):
    emitter = Emitter()
    monkeypatch.setattr(script_child, "current_emitter", lambda: emitter)
    monkeypatch.setenv("SCRIPT_TEST_MODE", "stopped")
    monkeypatch.setattr(script_child, "DOCKER_KILL_TIMEOUT_S", 0.2)
    path, deck, receipt, _ = prepared
    task = asyncio.create_task(script_child.run(path, deck, receipt))
    await asyncio.wait_for(emitter.started.wait(), 10)
    task.cancel()
    with pytest.raises(Stopped) as stopped:
        await asyncio.wait_for(task, 10)
    assert stopped.value.recorded
    record = json.loads(path.with_name("journal.jsonl").read_text())
    assert record["stopped"] == "stopped by the user"


@pytest.mark.parametrize(("mode", "budget", "code"), [
    ("killed", 0, "SCRIPT_CHILD_DIED"),
    ("budget", 2, "SCRIPT_TIME_BUDGET"),
])
@pytest.mark.asyncio
async def test_child_death_or_time_cap_is_a_typed_refusal(prepared, monkeypatch, mode, budget, code):
    emitter = Emitter()
    monkeypatch.setattr(script_child, "current_emitter", lambda: emitter)
    monkeypatch.setattr(script_child, "setting", lambda name: 1 if name == "LOCAL_CORES" else budget)
    monkeypatch.setenv("SCRIPT_TEST_MODE", mode)
    path, deck, receipt, _ = prepared
    with pytest.raises(Exception, match="reviewed script") as result:
        await asyncio.wait_for(script_child.run(path, deck, receipt), 10)
    assert result.value.error_code == code


@pytest.mark.asyncio
async def test_solver_refusal_keeps_its_code_and_message(prepared, monkeypatch):
    emitter = Emitter()
    monkeypatch.setattr(script_child, "current_emitter", lambda: emitter)
    monkeypatch.setenv("SCRIPT_TEST_MODE", "refused")
    path, deck, receipt, _ = prepared
    with pytest.raises(Exception, match="The solver refused the deck.") as result:
        await asyncio.wait_for(script_child.run(path, deck, receipt), 10)
    assert result.value.error_code == "TELEMAC_STUB_REFUSED"
