"""A live launch runs the saved script with the reviewed deck in another interpreter."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
import runpy

import pytest

from trid3nt import case
from trid3nt.inputs.temporal import Series
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
from trid3nt.inputs.temporal import Series
from trid3nt.render.emission import current_emitter
from trid3nt.render.formats import record_run_outputs
from trid3nt.sdk.script_run import current, main, study_review
from trid3nt.store import objects, journal
from trid3nt.workflows.runtime.workflow import RunResult
from trid3nt.workflows.solver.stop import Stopped
from trid3nt.workflows.telemac.workflow import TelemacWorkflow
from trid3nt_contracts.execution import LayerURI

store = objects.FileStore(os.environ['SCRIPT_TEST_STORE'])
objects.set_client(store)
objects.served_client = lambda: store
journal.journal_path = lambda: Path('journal.jsonl')

async def no_prepare(*args):
    raise AssertionError('the script rebuilt the reviewed deck')

async def solve(self, deck):
    assert current_emitter() is None
    assert deck.run_id == os.environ['SCRIPT_TEST_RUN']
    assert deck.state.env.run["discharge"] == Series((0, 60), (1, 2), units="m3/s")
    cores = current().handoff['cores']
    assert int(os.environ['OPENBLAS_NUM_THREADS']) == cores
    assert len(os.sched_getaffinity(0)) <= cores
    mode = os.environ['SCRIPT_TEST_MODE']

    async def engine():
        current().event('dispatch', 'telemac', 'telemac2d', SimpleNamespace(), cores)
        current().event('progress', {'run_id': deck.run_id, 'step': 'first'})
        current().event('progress', {'run_id': deck.run_id, 'step': 'second'})
        if mode == 'reviewed':
            decision = await study_review(tool_name="calibrate", entries=[], params={"tuned": ["k"]})
            assert decision.proceed and decision.params == {"tuned": ["rhk"]}
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
    production(state).run["discharge"] = Series((0, 60), (1, 2), units="m3/s")
    deck = Deck(state, run_id=_RUN, staged={"inputs": [{"dest": "deck.cas", "gs_uri": "s3://cache/deck.cas"}]})
    capture = script.Capture(dict(runpy.run_path(str(_INPUTS))["_ZERO_FETCH"]), lambda *args: {})
    receipt = archive(tmp_path / "chat", capture, deck)
    return capture.path, deck, receipt, store


@pytest.mark.asyncio
async def test_child_solves_reviewed_deck_and_relays_ordered_output(prepared, monkeypatch):
    from trid3nt_server.server.dispatch import remote_run

    monkeypatch.delenv("TRID3NT_REMOTE_RUNNER_URL", raising=False)

    async def unexpected(*args):
        pytest.fail("An unset runner URL reached the remote client.")

    monkeypatch.setattr(remote_run, "submit", unexpected)
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


@pytest.fixture
def paired(tmp_path, monkeypatch):
    from trid3nt.render import emission

    monkeypatch.setenv("TRID3NT_HOME", str(tmp_path / "runner"))
    monkeypatch.setenv("TRID3NT_REMOTE_RUNNER_TOKEN", "fixture-runner")
    monkeypatch.setenv("TRID3NT_ACCESS_TOKEN", "fixture-plugin")
    monkeypatch.setattr(emission, "current_turn_case", lambda: _RUN)


@asynccontextmanager
async def _runner(enabled=True):
    from websockets.asyncio.server import serve

    from trid3nt_server.server.dispatch import remote_run

    async with serve(remote_run.serve, "127.0.0.1", 0,
                     process_request=lambda conn, request: remote_run.gate(conn, request, accepting=enabled)) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_remote_replays_the_reviewed_deck_and_relays_its_record(prepared, paired, monkeypatch):
    from trid3nt_server.server.dispatch import remote_run

    path, deck, receipt, store = prepared
    monkeypatch.setattr(objects, "served_client", lambda: store)
    monkeypatch.setattr(remote_run, "setting", lambda name: 0 if name == "SOLVE_BUDGET_S" else 1)
    local_records = []
    monkeypatch.setattr(remote_run, "append_record", local_records.append)
    emitter = Emitter()
    async with _runner() as url:
        result = await asyncio.wait_for(remote_run.submit(url, path, deck, receipt, script_child._Relay(emitter)), 15)
    assert result.value.uri == f"s3://runs/{_RUN}/out.fgb"
    assert emitter.events == [("begin", "solve"), ("progress", "first"), ("progress", "second"),
                              ("end", "solve"), ("begin", "outputs"), ("layer", "Depth"), ("end", "outputs")]
    assert local_records == [result.record]
    assert result.record["runner"]
    assert result.record["case_script"]["code"] == "CODEGEN_CHECKED"
    assert result.record["outputs"][0]["name"] == result.value.name
    runner = Path(remote_run.os.environ["TRID3NT_HOME"])
    records = list(runner.rglob("journal.jsonl"))
    assert len(records) == 1
    assert json.loads(records[0].read_text()) == result.record


@pytest.mark.parametrize(("token", "enabled", "code"), [
    (None, True, "REMOTE_CREDENTIAL_MISSING"),
    ("fixture-plugin", True, "REMOTE_CREDENTIAL_REJECTED"),
    ("fixture-runner", False, "REMOTE_RUNNER_DISABLED"),
])
@pytest.mark.asyncio
async def test_remote_refuses_before_reading_the_case(paired, monkeypatch, token, enabled, code):
    from trid3nt_server.server.dispatch import remote_run

    real_submit = remote_run.submit
    # In a test both ends share an interpreter. Resolve the distinct presented value only in the client task.
    task = asyncio.current_task()
    monkeypatch.setattr(remote_run, "remote_runner_credential",
                        lambda: token if asyncio.current_task() is task else "fixture-runner")
    async with _runner(enabled) as url:
        with pytest.raises(Exception) as error:
            await real_submit(url, None, None, {}, script_child._Relay(Emitter()))
    assert error.value.error_code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("asset", ["script", "staged-input"])
async def test_remote_rejects_a_tampered_served_copy(prepared, paired, monkeypatch, asset):
    from trid3nt_server.server.dispatch import remote_run

    path, deck, receipt, store = prepared
    monkeypatch.setattr(objects, "served_client", lambda: store)
    bundle = remote_run._bundle

    def tamper(*args):
        if asset == "staged-input":
            store.put_object(Bucket="cache", Key="deck.cas", Body=b"changed deck")
        result = bundle(*args)
        if asset == "script":
            _, bucket, key = objects.split_object_uri(result.served + "/case.py")
            store.put_object(Bucket=bucket, Key=key, Body=b"changed file")
        return result

    monkeypatch.setattr(remote_run, "_bundle", tamper)
    async with _runner() as url:
        with pytest.raises(Exception) as error:
            await remote_run.submit(url, path, deck, receipt, script_child._Relay(Emitter()))
    assert error.value.error_code == "DIGEST_MISMATCH"


@pytest.mark.asyncio
async def test_remote_propagates_refused_image_over_websocket(prepared, paired, monkeypatch):
    from trid3nt.workflows.solver.solver import EngineImageMissing
    from trid3nt_server.server.dispatch import remote_run

    path, deck, receipt, store = prepared
    monkeypatch.setattr(objects, "served_client", lambda: store)

    def missing(*args):
        raise EngineImageMissing("Image fixture:latest is missing. Build it with fixture-build.")

    monkeypatch.setattr(remote_run, "_prepare", missing)
    async with _runner() as url:
        with pytest.raises(Exception, match="fixture-build") as error:
            await remote_run.submit(url, path, deck, receipt, script_child._Relay(Emitter()))
    assert error.value.error_code == "ENGINE_IMAGE_MISSING"


@pytest.mark.asyncio
async def test_unreachable_remote_refuses_without_starting_the_child(paired):
    from trid3nt_server.server.dispatch import remote_run

    async with _runner() as url:
        pass
    with pytest.raises(Exception) as error:
        await remote_run.submit(url, None, None, {}, script_child._Relay(Emitter()))
    assert error.value.error_code == "REMOTE_UNREACHABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("repeated", [False, True])
async def test_remote_stop_writes_the_runner_record_once_locally(prepared, paired, monkeypatch, repeated):
    from trid3nt_server.server.dispatch import remote_run

    path, deck, receipt, store = prepared
    monkeypatch.setattr(objects, "served_client", lambda: store)
    monkeypatch.setattr(remote_run, "setting", lambda name: 0 if name == "SOLVE_BUDGET_S" else 1)
    monkeypatch.setenv("SCRIPT_TEST_MODE", "stopped")
    local_records = []
    monkeypatch.setattr(remote_run, "append_record", local_records.append)
    emitter = Emitter()
    async with _runner() as url:
        task = asyncio.create_task(remote_run.submit(url, path, deck, receipt, script_child._Relay(emitter)))
        await asyncio.wait_for(emitter.started.wait(), 15)
        task.cancel()
        if repeated:
            await asyncio.sleep(0.1)
            task.cancel()
        with pytest.raises(Stopped):
            await asyncio.wait_for(task, 15)
    [record] = local_records
    assert record["stopped"] and record["runner"]


@pytest.mark.asyncio
async def test_runner_review_is_answered_through_the_local_gate(prepared, paired, monkeypatch):
    from trid3nt.inputs import review
    from trid3nt_server.server.dispatch import remote_run

    path, deck, receipt, store = prepared
    monkeypatch.setattr(objects, "served_client", lambda: store)
    monkeypatch.setattr(remote_run, "setting", lambda name: 0 if name == "SOLVE_BUDGET_S" else 1)
    monkeypatch.setenv("SCRIPT_TEST_MODE", "reviewed")
    monkeypatch.setattr(remote_run, "append_record", lambda record: None)
    asked = []

    async def gate(**card):
        asked.append(card)
        return review.ReviewOutcome(proceed=True, entries=[], params={"tuned": ["rhk"]})

    monkeypatch.setattr(review, "gate_input_review", gate)
    async with _runner() as url:
        result = await asyncio.wait_for(remote_run.submit(url, path, deck, receipt, script_child._Relay(Emitter())), 15)
    assert result.value.name == "Depth"
    assert asked == [{"tool_name": "calibrate", "entries": [], "params": {"tuned": ["k"]}, "mode": None}]


def test_completed_calibration_is_pinned_back_into_the_callers_case(tmp_path, monkeypatch):
    from trid3nt.sdk.lock import Lock
    from trid3nt.sdk.run_messages import RunInstruction
    from trid3nt_server.server.dispatch import remote_run

    store = objects.FileStore(tmp_path / "store")
    monkeypatch.setattr(objects, "served_client", lambda: store)
    case_root = tmp_path / "runner" / _RUN
    runner = case_root / "runs" / _RUN
    caller = tmp_path / "caller" / _RUN
    archive = caller / "runs" / _RUN
    version = "a" * 64
    name = f"cal-{_RUN}"
    study_key = f"studies/{name}/study.json"
    deck_key = f"studies/{name}/resolve/deck.cas"
    for key, content in [(study_key, json.dumps({"base_version": version})), (deck_key, "reviewed steering")]:
        file = case_root / key
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
    held = {key: objects.sha256_of(case_root / key) for key in (study_key, deck_key)}
    remote_lock = Lock(case_root)
    remote_lock.data["studies"] = {name: held}
    remote_lock.save()
    archive_lock = Lock(archive)
    snapshot = archive / "versions" / f"{version}.lock"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("{}")
    archive_lock.data.update(versions={f"versions/{version}.lock": objects.sha256_of(snapshot)},
                             served=f"s3://runs/{_RUN}")
    archive_lock.save()

    instruction = RunInstruction(case=_RUN, run_id=_RUN, script="calibrate.py", served=f"s3://runs/{_RUN}/runner")
    transferred = remote_run._serve_study(runner, instruction)
    remote_run._keep_study(archive / "calibrate.py", instruction, transferred)
    assert Lock(caller).data["studies"][name] == held
    assert (caller / deck_key).read_text() == "reviewed steering"
    assert (caller / "versions" / snapshot.name).read_text() == "{}"
