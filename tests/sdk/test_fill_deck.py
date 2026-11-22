"""A model's fill ends at its written, checked deck and solves nothing; its run solves that deck while it is current,
and fills again once a set or the deck's own bytes say it is not."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from trid3nt import case
from trid3nt.store import objects
from trid3nt.tools.mesh import step as mesh_step
from trid3nt.workflows.telemac import workflow as tw
from trid3nt.workflows.telemac.authoring import mesh_files, opening, release_point

#: The zero-fetch dye release: a drawn ring, a stated depth, a stated flow and outflow level.
_ZERO_FETCH = dict(
    release=[-122.669784, 45.518485], spill_duration_s=300.0, event_time="2024-03-01T12:00:00Z",
    mesh_resolution_m=40.0, DURATION=1800.0, discharge=56.6, level=0.0, bed=4.0,
    domain=[[-122.6769, 45.5153], [-122.6615, 45.5153], [-122.6615, 45.5198], [-122.6769, 45.5198]])


class _Published:
    run_id = "RUNFILLDECK"

    def model_copy(self, update):
        return self


def _stubbed(monkeypatch) -> list:
    """The launch's stages answered by stubs that say what they were asked; the deck is one file in the case's cache."""
    calls: list = []

    def said(name, value=None):
        async def run(*_args, **kwargs):
            calls.append(name)
            return value(kwargs) if callable(value) else {"name": name, **(value or {})}
        return run

    async def sheet(*, write, **_kwargs):
        await write(f"FRICTION {_kwargs['keywords'].get('FRICTION_COEFFICIENT')}")
        return {"name": "sheet"}

    async def stage_sheet(*, sheet, run_id, prefix, **_kwargs):
        calls.append("write")
        key = f"{prefix}/{run_id}/deck.cas"
        objects.client().put_object(Bucket=objects.cache_bucket(), Key=key, Body=sheet.encode("utf-8"))
        return {"run_id": run_id, "inputs": [{"gs_uri": f"s3://cache/{key}", "dest": "deck.cas"}]}

    art = SimpleNamespace(mesh_id="M1", name="domain", engine_files={})
    monkeypatch.setattr(mesh_step, "build_declared_mesh", said("mesh", {"artifact": art, "key": "K1"}))
    monkeypatch.setattr(mesh_step, "keep_mesh", said("keep"))
    monkeypatch.setattr(mesh_files, "telemac_mesh_files", said("mesh_files"))
    monkeypatch.setattr(opening, "open_channel", said("channel"))
    monkeypatch.setattr(opening, "open_water", said("settled", {"mesh_inputs": [], "server_facts": {}}))
    monkeypatch.setattr(release_point, "settle_release", said("source"))
    monkeypatch.setattr(tw, "fill_sheet", sheet)
    monkeypatch.setattr(tw, "stage_sheet", stage_sheet)
    monkeypatch.setattr(tw, "run_sheet", said("solve", lambda kwargs: {"run_id": kwargs["staged"]["run_id"]}))
    monkeypatch.setattr(tw, "publish_outputs", said("outputs", lambda _kwargs: _Published()))
    return calls


def test_fill_writes_the_deck_and_solves_nothing_and_the_form_carries_it(monkeypatch, tmp_path):
    calls = _stubbed(monkeypatch)
    c = case(tmp_path / "deck")
    form = c.telemac_dye_release(**_ZERO_FETCH).fill()
    assert form.ready and form.checked and calls.count("write") == 1 and "solve" not in calls
    body = (c.root / "cache" / "telemac" / form.deck.run_id / "deck.cas").read_bytes()
    files = {"deck.cas": hashlib.sha256(body).hexdigest()}
    assert dict(form.deck.files) == files and form.deck.mesh == "K1"
    assert form.deck.sha256 == hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest()
    row = json.loads((c.root / "trid3nt.lock").read_text())["models"]["telemac_dye_release"]
    assert row["deck"] == {"sha256": form.deck.sha256, "files": files} and row["mesh"] == "K1"


def test_run_after_an_unchanged_fill_solves_that_deck_and_a_set_fills_again(monkeypatch, tmp_path):
    calls = _stubbed(monkeypatch)
    c = case(tmp_path / "deck")
    m = c.telemac_dye_release(**_ZERO_FETCH)
    form = m.fill()
    calls.clear()
    assert m.run().run_id == "RUNFILLDECK"
    assert calls.count("solve") == 1 and not {"mesh", "write"} & set(calls)
    # A deck is solved once: the next run fills again.
    calls.clear()
    m.run()
    assert calls.count("mesh") == calls.count("write") == calls.count("solve") == 1
    superseded = m.fill()
    m.FRICTION_COEFFICIENT = 35
    calls.clear()
    m.run()
    assert calls.count("mesh") == calls.count("write") == calls.count("solve") == 1
    # The superseded deck leaves no staged copy behind.
    assert not list((c.root / "cache" / "telemac" / superseded.deck.run_id).glob("*"))
    assert (c.root / "cache" / "telemac" / form.deck.run_id / "deck.cas").is_file()


def test_a_staged_file_whose_bytes_changed_is_written_again(monkeypatch, tmp_path):
    calls = _stubbed(monkeypatch)
    c = case(tmp_path / "deck")
    m = c.telemac_dye_release(**_ZERO_FETCH)
    form = m.fill()
    (c.root / "cache" / "telemac" / form.deck.run_id / "deck.cas").write_bytes(b"edited")
    calls.clear()
    m.run()
    assert calls.count("write") == calls.count("solve") == 1
