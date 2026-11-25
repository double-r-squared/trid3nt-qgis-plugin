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
        coefficient = _kwargs['keywords'].get("FRICTION_COEFFICIENT", _kwargs["steering"].ASSERTED["FRICTION_COEFFICIENT"])
        await write(f"FRICTION {coefficient}")
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


_TEMPLATE = "telemac_dye_release"


def _template_source(monkeypatch, tmp_path):
    from pathlib import Path

    from trid3nt.sdk.case import _workflow

    module = _workflow(_TEMPLATE).template
    original = Path(module.__file__).read_bytes()
    source = tmp_path / "upstream.py"
    source.write_bytes(original)
    monkeypatch.setattr(module, "__file__", str(source))
    return source, original


def _changed(original):
    return original.replace(b"_FRICTION_COEFFICIENT = 33.0", b"_FRICTION_COEFFICIENT = 35.0")


def _friction(c, form):
    return (c.root / "cache" / "telemac" / form.deck.run_id / "deck.cas").read_bytes()


def test_template_copy_preserves_source_deck_and_catalog(monkeypatch, tmp_path):
    from trid3nt.tools import TOOL_REGISTRY
    from trid3nt_harness.tools.search.tool_retrieval import retrieve_visible_tools

    query = "a dye plume carried away from where it was released and diluting, animated"
    hits = tuple(retrieve_visible_tools(query, None, 8))
    assert _TEMPLATE in hits
    _stubbed(monkeypatch)
    _, original = _template_source(monkeypatch, tmp_path)
    registered = dict(TOOL_REGISTRY)
    assert len(registered) == 172
    c = case(tmp_path / "copy")
    path = c.copy_template(_TEMPLATE)
    assert path.read_bytes() == original
    called = c.telemac_dye_release(**_ZERO_FETCH).fill()
    copied = c.model(path, **_ZERO_FETCH).fill()
    assert called.checked and copied.checked and copied.deck.files == called.deck.files
    assert TOOL_REGISTRY == registered
    assert tuple(retrieve_visible_tools(query, None, 8)) == hits


def test_a_template_copy_uses_its_own_edited_constant(monkeypatch, tmp_path):
    _stubbed(monkeypatch)
    upstream, original = _template_source(monkeypatch, tmp_path)
    c = case(tmp_path / "edited")
    path = c.copy_template(_TEMPLATE)
    path.write_bytes(_changed(original))
    m = c.model(path, **_ZERO_FETCH)
    assert _friction(c, m.fill()) == b"FRICTION 35.0"
    assert upstream.read_bytes() == original
    record = m.run().record
    assert record["template_path"] == "copied"
    assert record["template_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_template_drift_keeps_the_pin_on_both_paths(monkeypatch, tmp_path):
    _stubbed(monkeypatch)
    upstream, original = _template_source(monkeypatch, tmp_path)
    pinned = hashlib.sha256(original).hexdigest()
    current = hashlib.sha256(_changed(original)).hexdigest()
    called, copied = case(tmp_path / "called"), case(tmp_path / "copied")
    relative = copied.copy_template(_TEMPLATE).relative_to(copied.root)
    called.telemac_dye_release(**_ZERO_FETCH).fill()
    copied.model(relative, **_ZERO_FETCH).fill()
    assert (called.root / "data" / f"{pinned}.py").read_bytes() == original
    upstream.unlink()
    replayed = case(called.root).telemac_dye_release(**_ZERO_FETCH).fill()
    assert _friction(called, replayed) == b"FRICTION 33.0"
    upstream.write_bytes(_changed(original))

    for c, kind, factory in (
        (case(called.root), "called", lambda c: c.telemac_dye_release(**_ZERO_FETCH)),
        (case(copied.root), "copied", lambda c: c.model(relative, **_ZERO_FETCH)),
    ):
        m = factory(c)
        form = m.fill()
        assert form.ready and _friction(c, form) == b"FRICTION 33.0"
        warning, = form.warnings
        assert (warning.code, warning.template, warning.pinned_sha256, warning.current_sha256) == (
            "TEMPLATE_DRIFT", _TEMPLATE, pinned, current)
        expected = "repin_template" if kind == "called" else "copy_template"
        assert warning.fix.call == expected and warning.fix.args == (_TEMPLATE,)
        assert warning.fix.diff == (None if kind == "called" else relative.as_posix())
        m.strict = True
        strict = m.fill()
        assert strict.refusal[0] == warning.code and strict.deck is None
        getattr(c, expected)(*warning.fix.args)
        refreshed = factory(c)
        form = refreshed.fill()
        assert not form.warnings and _friction(c, form) == b"FRICTION 35.0"
        held = json.loads((c.root / "trid3nt.lock").read_text())["models"]
        assert any(row["template"]["path"] == kind and row["template"]["version"] == current
                   for row in held.values())


def test_template_copy_refuses_bad_shape_and_guards_edits(monkeypatch, tmp_path):
    import pytest

    from trid3nt.errors import DeclarationError

    _, original = _template_source(monkeypatch, tmp_path)
    c = case(tmp_path / "invalid")
    path = c.copy_template(_TEMPLATE)
    path.write_bytes(original.replace(b"class INPUTS:", b"class BAD_INPUTS:"))
    with pytest.raises(DeclarationError) as broken:
        c.model(path, **_ZERO_FETCH)
    assert broken.value.error_code == "TEMPLATE_INVALID"
    edited = path.read_bytes()
    with pytest.raises(DeclarationError) as conflict:
        c.copy_template(_TEMPLATE)
    assert conflict.value.error_code == "TEMPLATE_COPY_EDITED" and path.read_bytes() == edited
    c.copy_template(_TEMPLATE, overwrite=True)
    assert path.read_bytes() == original


def test_case_template_overrides_captions_over_the_pinned_upstream(monkeypatch, tmp_path):
    _stubbed(monkeypatch)
    upstream, original = _template_source(monkeypatch, tmp_path)
    c = case(tmp_path / "extended")
    path = c.root / "templates" / "extension.py"
    path.parent.mkdir()
    path.write_text(
        "import sys\n"
        "from trid3nt.workflows.telemac.templates.dye_release.dye_release import "
        "INPUTS, STEERING, OUTPUTS, CAPTIONS as BaseCaptions, _METADATA, _RELEASE, "
        "TelemacWorkflow, register_workflow, telemac_dye_release\n"
        "CAPTIONS = {**BaseCaptions, \"T1\": \"tracer concentration\"}\n"
        "tool = register_workflow(TelemacWorkflow, _METADATA, sys.modules[__name__], "
        "coerce=telemac_dye_release.workflow.coercions)\n"
    )
    m = c.model(path, upstream=_TEMPLATE, **_ZERO_FETCH)
    assert m._workflow.captions["T1"] == "tracer concentration"
    assert _friction(c, m.fill()) == b"FRICTION 33.0"
    upstream.write_bytes(_changed(original))
    path.write_text(
        "import sys\n"
        "from trid3nt.workflows.telemac.templates.dye_release import dye_release as base\n"
        "INPUTS, STEERING, OUTPUTS, _RELEASE = base.INPUTS, base.STEERING, base.OUTPUTS, base._RELEASE\n"
        "CAPTIONS = {**base.CAPTIONS, \"T1\": \"tracer concentration\"}\n"
        "tool = base.register_workflow(base.TelemacWorkflow, base._METADATA, sys.modules[__name__], "
        "coerce=base.telemac_dye_release.workflow.coercions)\n"
    )
    m = c.model(path, upstream=_TEMPLATE, **_ZERO_FETCH)
    form = m.fill()
    assert form.warnings[0].code == "TEMPLATE_DRIFT" and _friction(c, form) == b"FRICTION 33.0"
