"""A reviewed daemon deck and its replay pair use the same bytes."""

import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

from trid3nt import case
from trid3nt.store import objects
from trid3nt.workflows.runtime import script
from trid3nt.workflows.runtime.fill import Fill, production
from trid3nt.workflows.runtime.workflow import Deck
from trid3nt_server.server.dispatch.case_script import archive

_ROOT = Path(__file__).resolve().parents[2]
_SUPPORT = _ROOT / "tests/sdk/test_fill_deck.py"
_RUN = "01M833H3N0B90A6M7GJ3HND42S"


def test_reviewed_script_and_served_lock_replay_in_plain_python(tmp_path, monkeypatch):
    values = dict(runpy.run_path(str(_SUPPORT))["_ZERO_FETCH"])
    storage = objects.FileStore(tmp_path / "store")
    monkeypatch.setattr(objects, "client", lambda: storage)
    monkeypatch.setattr(objects, "runs_bucket", lambda: "runs")
    storage.put_object(Bucket="cache", Key="deck.cas", Body=b"FRICTION 35")
    c = case(tmp_path / "scratch")
    workflow = c.telemac_dye_release()._workflow
    state = Fill(workflow)
    production(state)
    deck = Deck(state, run_id=_RUN, staged={"inputs": [{"dest": "deck.cas", "gs_uri": "s3://cache/deck.cas"}]})
    capture = script.Capture(values, lambda *args: {})
    capture.keywords["FRICTION_COEFFICIENT"] = 35
    root = tmp_path / "chat"
    receipt = archive(root, capture, deck)
    assert receipt["code"] == "CODEGEN_LINE_CHECKED"
    assert "m.FRICTION_COEFFICIENT = 35" in (root / "case.py").read_text()
    assert (root / "trid3nt.lock").is_file()
    version, = (root / "versions").glob("*.lock")
    assert len(version.stem) == 64
    first_script = root / "runs" / _RUN / "case.py"
    assert first_script.is_file()
    (root / "case.py").write_text("# user kept this version\n")
    next_id = "01M833H3N0B90A6M7GJ3HND42T"
    next_deck = Deck(state, run_id=next_id, staged=deck.staged)
    assert archive(root, capture, next_deck)["code"] == "CODEGEN_LINE_CHECKED"
    assert (root / "case.py").read_text() == "# user kept this version\n"
    assert first_script.read_text() == (root / "runs" / next_id / "case.py").read_text()

    target = tmp_path / "standalone"
    target.mkdir()
    for name in ("case.py", "trid3nt.lock"):
        storage.download_file("runs", f"{_RUN}/{name}", str(target / name))
    bootstrap = tmp_path / "python"
    bootstrap.mkdir()
    (bootstrap / "sitecustomize.py").write_text(
        "import runpy, sys, pytest\n"
        "sys.modules['trid3nt_harness'] = None\n"
        "sys.modules['trid3nt_server'] = None\n"
        "from trid3nt.store import objects\n"
        f"objects.served_client = lambda: objects.FileStore({str(storage.root)!r})\n"
        f"runpy.run_path({str(_SUPPORT)!r})['_stubbed'](pytest.MonkeyPatch())\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(bootstrap), str(_ROOT)))}
    done = subprocess.run([sys.executable, "case.py"], cwd=target, env=env, text=True, capture_output=True)
    assert done.returncode == 0, done.stderr
    replay = json.loads((target / "trid3nt.lock").read_text())["models"]["telemac_dye_release"]["deck"]
    assert replay["files"]["deck.cas"] == hashlib.sha256(b"FRICTION 35").hexdigest()
    assert replay["sha256"] == receipt["deck_sha256"]


def test_archive_refuses_a_deck_that_has_not_been_prepared(tmp_path):
    c = case(tmp_path / "scratch")
    workflow = c.telemac_dye_release()._workflow
    capture = script.Capture({}, lambda *args: {})
    assert archive(tmp_path / "chat", capture, Deck(Fill(workflow))) == {"code": "CODEGEN_DECK_MISSING"}
    assert not (tmp_path / "chat").exists()


def test_archived_layer_inputs_keep_the_producers_units(tmp_path, monkeypatch):
    from trid3nt.sdk.lock import Lock
    from trid3nt_server.server.dispatch import case_script

    c = case(tmp_path / "case")
    state = Fill(c.modflow_water_table()._workflow)
    env = production(state)
    uri = "s3://cache/thickness.tif"
    env.supplied["saturated_thickness"] = uri
    capture = script.Capture({"saturated_thickness": "L4"}, lambda *args: {})
    store = objects.FileStore(tmp_path / "store")
    store.put_object(Bucket="cache", Key="thickness.tif", Body=b"measured thickness")
    monkeypatch.setattr(objects, "served_client", lambda: store)
    monkeypatch.setattr(case_script, "lookup_layer_for_handle",
                        lambda value: {"uri": value, "units": "m", "quantity": "aquifer_saturated_thickness"})
    values, _, pins = case_script._declarations(Lock(c.root), capture, Deck(state), {})
    assert values["saturated_thickness"] == {"layer": {
        "uri": "s3://" + pins["saturated_thickness"]["uri"], "units": "m", "quantity": "aquifer_saturated_thickness",
    }}


def test_point_probe_replays_as_the_existing_point_kind_and_served_banks(tmp_path, monkeypatch):
    import asyncio

    from trid3nt.inputs.domain import domain, kind_under, place_kind
    from trid3nt.inputs.point import Point, point
    from trid3nt.sdk.lock import Lock
    from trid3nt.workflows.runtime.fill import SourceRequest
    from trid3nt_server.server.dispatch.case_script import _declarations

    store = objects.FileStore(tmp_path / "store")
    monkeypatch.setattr(objects, "client", lambda: store)
    monkeypatch.setattr(objects, "served_client", lambda: store)
    fixtures = _ROOT / "tests" / "fixtures"
    banks = fixtures / "nhd_water_surface" / "missouri_release_7_5km.fgb"
    centerline = str(fixtures / "nhdplus_nldi" / "missouri_release_dm_7km.fgb")
    store.put_object(Bucket="cache", Key="banks.fgb", Body=banks.read_bytes())
    model = case(tmp_path / "case").telemac_dye_release()
    state = Fill(model._workflow)
    env = production(state)
    coordinates = (-95.921233757, 41.257659140)
    env.run["release"] = Point(*coordinates)
    active = script.Capture({"release": list(coordinates)}, lambda *args: {})
    active.seeds["release"] = kind_under(str(banks), env.run["release"])
    active.banks = "s3://cache/banks.fgb"
    lock = Lock(tmp_path / "case")
    choices = {name: (SourceRequest("fetch_nhd_hr_river_areas", 1, 1, {}),)
               for name in ("domain_water", "domain_banks")}
    values, replay_choices, inputs = _declarations(lock, active, Deck(state), choices)
    seed = asyncio.run(point(values["release"]))
    assert place_kind(seed) == "flowline"
    assert (seed.lon, seed.lat) == (env.run["release"].lon, env.run["release"].lat)
    assert not replay_choices
    assert values["banks"]["layer"] == "s3://" + inputs["banks"]["uri"]
    lock._served(inputs["banks"]["uri"], inputs["banks"]["sha256"])
    pinned = lock.local(inputs["banks"]["uri"])
    assert pinned.read_bytes() == banks.read_bytes()
    original = domain(centerline, banks=str(banks), near=coordinates, span_km=7.0, above_m=60.0)
    replayed = domain(centerline, banks=str(pinned), near=(seed.lon, seed.lat), span_km=7.0, above_m=60.0)
    assert replayed.geometry == original.geometry
    assert replayed.runs == original.runs
