"""A case is a directory: its lock pins what each run read, a replay reads only the pins, and the tree moves whole."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from trid3nt import case
from trid3nt.store import objects as storage
from trid3nt.store.objects import DigestMismatchError, FileStore, sha256_of
from trid3nt.tools.fetchers._router import router
from trid3nt.workflows.solver import solver as solver_mod

REPO = Path(__file__).resolve().parents[2]

#: The zero-fetch dye release at a stated moment, its bed left to a named source the test answers.
_DYE = dict(
    release=[-122.669784, 45.518485], spill_duration_s=300.0, event_time="2024-03-01T12:00:00Z",
    mesh_resolution_m=40.0, DURATION=1800.0, discharge=56.6, level=0.0,
    domain={"type": "Polygon",
            "coordinates": [[[-122.6769, 45.5153], [-122.6615, 45.5153],
                             [-122.6615, 45.5198], [-122.6769, 45.5198],
                             [-122.6769, 45.5153]]],
            "runs": [
                {"type": "inflow", "start": [-122.6769, 45.5153], "end": [-122.6769, 45.5198]},
                {"type": "outflow", "start": [-122.6615, 45.5153], "end": [-122.6615, 45.5198]}]},
    bed={"source": "fetch_etopo"})

#: One process of the case: the bed's fetch answered by a raster the script writes (or refused, to show none is
#: made), the solve stopped once the deck is staged; it prints what was fetched, each staged deck and the lock.
_PROCESS = r"""
import json, sys
sys.modules["trid3nt_harness"] = None
sys.modules["trid3nt_server"] = None
from pathlib import Path
from trid3nt.store import objects
objects._SETTINGS_FILE = Path("/nonexistent/.env.local")
from trid3nt_contracts.execution import LayerURI
from trid3nt.tools.fetchers._router import router
from trid3nt.workflows.telemac import engine
from trid3nt.workflows.telemac.errors import TelemacError
from trid3nt.sdk.model import _deck, _manifest_rows
from trid3nt import case

root, values, fetching = Path(sys.argv[1]), json.loads(sys.argv[2]), sys.argv[3] == "fetch"
fetched = []

def fetch(spec, raw):
    fetched.append(spec.name)
    if not fetching:
        raise AssertionError("a replay fetched " + spec.name)
    import numpy as np, rasterio
    from rasterio.transform import from_bounds
    path = root / "cache" / "upstream" / "bed.tif"
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", width=80, height=40, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_bounds(-122.68, 45.514, -122.66, 45.521, 80, 40)) as out:
        out.write(np.full((40, 80), -4.0, dtype="float32"), 1)
    return LayerURI(layer_id="etopo-bed", name="bed", layer_type="raster", uri="s3://cache/upstream/bed.tif", units="m")

async def at_the_deck(**_kwargs):
    raise TelemacError("stopped once the deck is staged", error_code="STOPPED_AT_THE_DECK")

router._keyed = fetch
engine.solve_case = at_the_deck
c = case(root)
m = c.telemac_dye_release(**values)
form = m.fill()
ran = m.run()
decks = {p.parent.name: _deck(root, _manifest_rows(root, "telemac", p.parent.name))
         for p in sorted((root / "cache" / "telemac").glob("*/manifest.json"))}
print(json.dumps({"ready": form.ready, "code": getattr(ran, "code", None), "fetched": fetched, "decks": decks,
                  "form": form.deck and {"sha256": form.deck.sha256, "files": dict(form.deck.files)},
                  "lock": (root / "trid3nt.lock").read_text()}))
"""


def _process(root: Path, fetching: bool, persistence: Path) -> dict:
    """One fresh interpreter over the case with no object store in its environment."""
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("AWS_") and name not in ("TRID3NT_RUNS_BUCKET", "TRID3NT_CACHE_BUCKET",
                                                           "TRID3NT_RUNS_DIR")}
    env["TRID3NT_DEV_PERSISTENCE_DIR"] = str(persistence)
    done = subprocess.run([sys.executable, "-c", _PROCESS, str(root), json.dumps(_DYE),
                           "fetch" if fetching else "replay"],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=280)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def first_run(tmp_path_factory):
    """A case whose bed was fetched once, its deck staged: the root, what the run printed and its persistence."""
    base = tmp_path_factory.mktemp("case_dir")
    root, persistence = base / "dye", base / "persistence"
    return root, _process(root, True, persistence), persistence


def test_a_bare_name_is_that_directory_under_the_current_one_with_the_layout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = case("dye-l2")
    assert c.root == tmp_path / "dye-l2" and c.name == "dye-l2"
    assert sorted(p.name for p in c.root.iterdir()) == ["data", "runs", "versions"]
    assert case(tmp_path / "dye-l2").root == c.root


def test_a_replay_of_the_moved_case_in_a_fresh_process_fetches_nothing_and_stages_the_same_deck(first_run, tmp_path):
    """The replay is moved without its cache, so it builds the mesh again: the deck is the same bytes, geometry included."""
    root, first, persistence = first_run
    assert first["fetched"] == ["fetch_etopo"] and first["ready"] and first["code"] == "STOPPED_AT_THE_DECK"
    lock = json.loads(first["lock"])
    (pin,) = lock["fetched"].values()
    assert pin["source"] == "fetch_etopo" and all((root / path).is_file() for path in pin["files"])
    bed = lock["models"]["telemac_dye_release"]["inputs"]["bed"]
    assert bed["source"] == "fetch_etopo" and bed["uri"].startswith("data/") and bed["sha256"] == sha256_of(root / bed["uri"])
    moved = tmp_path / "elsewhere" / "dye"
    shutil.copytree(root, moved, ignore=shutil.ignore_patterns("cache"))
    # The case stands only where it was moved to while the replay runs: nothing of it is read off the old place.
    away = root.rename(root.with_name("dye-away"))
    try:
        replay = _process(moved, False, persistence)
    finally:
        away.rename(root)
    assert replay["fetched"] == [] and replay["ready"] and replay["code"] == "STOPPED_AT_THE_DECK"
    (deck,), (again,) = first["decks"].values(), replay["decks"].values()
    assert deck and first["form"] == deck and "domain.slf" in deck["files"]
    assert again == deck and replay["form"] == deck
    assert replay["lock"] == first["lock"]


def test_a_pinned_file_edited_refuses_on_its_sha256(first_run, tmp_path, monkeypatch):
    root, _first, _persistence = first_run
    tampered = tmp_path / "tampered"
    shutil.copytree(root, tampered)
    bed = json.loads((tampered / "trid3nt.lock").read_text())["models"]["telemac_dye_release"]["inputs"]["bed"]
    with (tampered / bed["uri"]).open("r+b") as handle:
        handle.seek(-1, 2)
        handle.write(b"\x01")
    monkeypatch.setattr(router, "_keyed", lambda spec, raw: pytest.fail(f"{spec.name} was fetched"))
    verdict = case(tampered).telemac_dye_release(**_DYE).fill()["bed"]
    assert verdict.code == "DIGEST_MISMATCH"
    with pytest.raises(DigestMismatchError):
        FileStore(tampered).get_object(Bucket="data", Key=bed["uri"].split("/", 1)[1])


def test_a_pinned_file_gone_is_fetched_again_and_refused_where_its_bytes_differ(tmp_path, monkeypatch):
    c = case(tmp_path / "gone")
    answers = iter([b"first", b"other"])

    def fetch(spec, raw):
        FileStore(c.root).put_object(Bucket="cache", Key="up/record.json", Body=next(answers))
        return {"uri": "s3://cache/up/record.json"}

    monkeypatch.setattr(router, "_keyed", fetch)
    from trid3nt.tools.fetchers._router.registration import get_spec

    spec = get_spec("fetch_etopo")
    with c.bound():
        first = router.route(spec, {"bbox": [0, 0, 1, 1], "purpose": "a control"})
    (pin,) = c.lock.data["fetched"].values()
    assert pin["request"] == {"bbox": [0, 0, 1, 1]} and first["uri"].startswith("s3://data/")
    (c.root / first["uri"][len("s3://"):]).unlink()
    with c.bound(), pytest.raises(DigestMismatchError):
        router.route(spec, {"bbox": [0, 0, 1, 1]})


def test_outside_data_by_path_and_url_is_copied_into_the_case_and_pinned(tmp_path):
    held = tmp_path / "outside" / "bed.tif"
    held.parent.mkdir()
    held.write_bytes(b"a bed raster's bytes")
    digest = sha256_of(held)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), lambda *a, **k: Quiet(*a, directory=str(held.parent), **k))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c = case(tmp_path / "outside-data")
        by_path = c.telemac_dye_release(bed=str(held))
        by_url = c.telemac_dye_release(bed=f"http://127.0.0.1:{server.server_port}/bed.tif")
    finally:
        server.shutdown()
    for model, source in ((by_path, "path"), (by_url, "url")):
        assert model.bed == f"s3://data/{digest}.tif"
        assert model._taken["bed"] == {"source": source, "request": model._taken["bed"]["request"],
                                       "uri": f"data/{digest}.tif", "sha256": digest}
    assert sha256_of(c.root / "data" / f"{digest}.tif") == digest


class _Served:
    """A served store standing in for one reached over S3: buckets of keys, held in memory."""

    def __init__(self):
        self.held: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket, Key, Body):
        self.held[(Bucket, Key)] = Body.read() if hasattr(Body, "read") else bytes(Body)

    def get_object(self, *, Bucket, Key):
        import io

        return {"Body": io.BytesIO(self.held[(Bucket, Key)])}

    def download_file(self, Bucket, Key, Filename):
        Path(Filename).write_bytes(self.held[(Bucket, Key)])


def test_link_serves_the_tree_at_its_relative_paths_and_pack_brings_every_pin_back_checked(tmp_path, monkeypatch):
    c = case(tmp_path / "linked")
    monkeypatch.setattr(router, "_keyed", lambda spec, raw: (
        FileStore(c.root).put_object(Bucket="cache", Key="up/a.tif", Body=b"raster"),
        {"uri": "s3://cache/up/a.tif"})[1])
    from trid3nt.tools.fetchers._router.registration import get_spec

    with c.bound():
        router.route(get_spec("fetch_etopo"), {"bbox": [0, 0, 1, 1]})
    served = _Served()
    monkeypatch.setattr(storage, "served_client", lambda: served)
    assert c.link("s3://bucket/cases/linked") == "s3://bucket/cases/linked"
    (pin,) = c.lock.data["fetched"].values()
    assert pin["uri"].startswith("s3://bucket/cases/linked/data/")
    assert {key for _bucket, key in served.held} >= {f"cases/linked/{path}" for path in pin["files"]} | {
        "cases/linked/trid3nt.lock"}
    copy = tmp_path / "fresh" / "linked"
    copy.mkdir(parents=True)
    (copy / "trid3nt.lock").write_bytes((c.root / "trid3nt.lock").read_bytes())
    packed = case(copy)
    (path,) = pin["files"]
    served.held[("bucket", f"cases/linked/{path}")] = b"not the raster"
    with pytest.raises(DigestMismatchError):
        packed.pack()
    served.held[("bucket", f"cases/linked/{path}")] = b"raster"
    packed.pack()
    (pin,) = packed.lock.data["fetched"].values()
    assert pin["uri"].startswith("data/") and "served" not in packed.lock.data
    assert all(sha256_of(copy / name) == digest for name, digest in pin["files"].items())


def test_a_run_whose_engine_image_is_absent_refuses_naming_the_image_and_its_build(tmp_path, monkeypatch):
    spec = solver_mod.LocalSolverSpec(
        solver="fake", workflow_name=solver_mod.LOCAL_DOCKER_WORKFLOW_NAME, args_key="fake_args",
        build_argv=lambda run_id, rundir, args: ["docker", "run", "trid3nt-local/no-such-engine:latest"],
        stdout_name="fake.stdout", stderr_name="fake.stderr", stdout_uri_field="o", stderr_uri_field="e",
        image=lambda rundir: ("trid3nt-local/no-such-engine:latest",
                              "docker build -t trid3nt-local/no-such-engine:latest workers/no-such-engine/"))
    asked, run = [], subprocess.run

    def docker(argv, **kwargs):
        if argv[0] != "docker":
            return run(argv, **kwargs)
        asked.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "Error: No such image: trid3nt-local/no-such-engine:latest")

    monkeypatch.setattr(solver_mod.subprocess, "run", docker)
    launched = []
    monkeypatch.setattr(solver_mod, "_with_cpu_limit", lambda cmd, cores: launched.append(cmd) or ["true"])
    c = case(tmp_path / "no-image")
    store = FileStore(c.root)
    store.put_object(Bucket="cache", Key="fake/R/manifest.json", Body=json.dumps({"inputs": []}).encode())
    token = storage.bind_store(store)
    try:
        with pytest.raises(solver_mod.EngineImageMissing) as refused:
            solver_mod.launch_local_solver(spec, "s3://cache/fake/R/manifest.json", run_id="R")
    finally:
        storage.reset_store(token)
    assert refused.value.error_code == "ENGINE_IMAGE_MISSING"
    assert "trid3nt-local/no-such-engine:latest" in str(refused.value)
    assert "docker build -t trid3nt-local/no-such-engine:latest workers/no-such-engine/" in str(refused.value)
    assert [argv[:3] for argv in asked] == [["docker", "image", "inspect"]] and launched == []


def test_unattended_an_unstated_run_input_with_several_finished_runs_refuses_naming_them(monkeypatch):
    import asyncio

    from trid3nt.inputs import run as run_mod
    from trid3nt.inputs.review import unattended

    found = [{"run_id": "01NEWER", "engine": "modflow"}, {"run_id": "01OLDER", "engine": "modflow"}]
    monkeypatch.setattr(run_mod, "_case_runs", lambda engines: found)

    async def taken():
        with unattended():
            return await run_mod.case_run(("modflow",), label="run", tool="calibrate")

    with pytest.raises(run_mod.FinishedRunError) as refused:
        asyncio.run(taken())
    assert refused.value.error_code == "RUN_UNSTATED" and refused.value.remedies == ("01NEWER", "01OLDER")
    monkeypatch.setattr(run_mod, "_case_runs", lambda engines: found[:1])
    with unattended():
        assert asyncio.run(run_mod.case_run(("modflow",), label="run")) == "01NEWER"
