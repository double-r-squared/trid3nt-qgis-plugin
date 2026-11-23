"""Keep a live case's reviewed script and served input pins before the solve."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, Mapping

from trid3nt_contracts import ULIDStr
from pydantic import TypeAdapter, ValidationError

from trid3nt.inputs.point import Point
from trid3nt.render.emission import current_emitter, current_turn_case
from trid3nt.sdk.lock import Lock, served_digest
from trid3nt.sdk.model import Staged, _form
from trid3nt.store import objects
from trid3nt.workflows.runtime.script import Capture
from trid3nt.workflows.runtime.workflow import Deck
from trid3nt_harness.codegen import check_lines

_ID = TypeAdapter(ULIDStr)


def host():
    """Offer a writer only for a live turn with a validated case id."""
    case_id = current_turn_case()
    if case_id is None or current_emitter() is None:
        return None
    try:
        _ID.validate_python(case_id)
    except ValidationError:
        return None
    home = Path(os.environ.get("TRID3NT_HOME") or Path.home() / ".trid3nt").expanduser()
    return partial(archive, home / "cases" / case_id)


def archive(root: Path, capture: Capture, deck: Deck) -> dict[str, Any]:
    """Publish the run's own script and lock. The editable case script is retained."""
    if not deck.run_id or not deck.staged.get("inputs"):
        return {"code": "CODEGEN_DECK_MISSING"}
    run_id = _ID.validate_python(deck.run_id)
    files = {row["dest"]: served_digest(row["gs_uri"]) for row in deck.staged["inputs"]}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest()
    form = _form(deck.state, Staged(run_id, files, digest), strict=True)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root, prefix=".script-") as temp:
        draft = Path(temp)
        lock = Lock(draft)
        values, choices, inputs = _declarations(lock, capture, deck, form.choices)
        form = replace(form, choices=choices)
        script = check_lines(deck.state.workflow.name, values, form, case_dir=draft, served=True)
        if script.refusal:
            return {"code": script.refusal[0]}
        pins = lock.data
        lock = Lock(draft)
        lock.data.update(pins)
        for source, request, value in capture.fetched:
            lock.hold_served(source, request, value)
        bucket = objects.runs_bucket()
        lock.data["served"] = f"s3://{bucket}/{run_id}"
        lock.data["models"][deck.state.workflow.name] = {
            "template": {
                "name": deck.state.workflow.name,
                "version": lock.data["called_templates"][deck.state.workflow.name]["sha256"],
                "path": "called",
                "upstream_version": lock.data["called_templates"][deck.state.workflow.name]["sha256"],
            },
            "model": script.model_version,
            "deck": {"sha256": digest, "files": files},
            "inputs": inputs,
        }
        lock.save()
        (draft / "case.py").write_text(script.text, encoding="utf-8")
        destination = root / "runs" / run_id
        _publish(draft, destination, bucket, run_id)
        _keep_first(root, draft, script.model_version)
    return {"code": script.check_code, "checked": script.checked, "deck_sha256": digest,
            "uri": f"s3://{bucket}/{run_id}/case.py", "lock_uri": f"s3://{bucket}/{run_id}/trid3nt.lock"}


def _declarations(lock: Lock, capture: Capture, deck: Deck, traced: Mapping[str, Any]):
    state = deck.state
    values = {key: value for key, value in capture.wire.items() if key != "restart_clean"}
    values.update(capture.params)
    if capture.keywords:
        values["keywords"] = {**(values.get("keywords") or {}), **capture.keywords}
    env = state.env
    for name, supplied in env.supplied.items():
        if name in values and not isinstance(values[name], dict) and isinstance(supplied, str):
            values[name] = supplied
        elif name in values and isinstance(values[name], dict) and "layer" in values[name]:
            values[name] = supplied
    choices = dict(traced)
    if capture.seeds:
        for name, kind in capture.seeds.items():
            values[name] = _point(env.run[name], {"flowline": "river", "waterbody": "water"}[kind])
        for name in ("domain_water", "domain_waterbody"):
            choices.pop(name, None)
    if capture.banks is not None:
        values["banks"] = capture.banks
        choices.pop("domain_banks", None)
    for name, verdict in state.inputs.items():
        if isinstance(verdict.value, Point) and name not in values:
            values[name] = _point(verdict.value)
    inputs = {}
    for name, value in values.items():
        uri = getattr(value, "uri", value)
        if isinstance(uri, Mapping) and isinstance(uri.get("uri"), str):
            uri = uri["uri"]
        if isinstance(uri, str) and uri.startswith("s3://"):
            path, digest = lock.reference(uri)
            inputs[name] = {"source": "served", "uri": path, "sha256": digest}
            values[name] = {"layer": f"s3://{path}"}
    return values, choices, inputs


def _point(value: Point, kind: str | None = None) -> dict[str, Any]:
    return {"lon": value.lon, "lat": value.lat, "name": value.name, "place_type": kind or value.kind}


def _publish(draft: Path, destination: Path, bucket: str, run_id: str) -> None:
    client = objects.client()
    paths = sorted((draft / "data").iterdir()) + [draft / "trid3nt.lock", draft / "case.py"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    local = draft / "run"
    local.mkdir()
    for path in paths:
        relative = path.relative_to(draft)
        if path.parent == draft:
            shutil.copyfile(path, local / path.name)
        with path.open("rb") as body:
            client.put_object(Bucket=bucket, Key=f"{run_id}/{relative.as_posix()}", Body=body)
    local.rename(destination)


def _keep_first(root: Path, draft: Path, version: str | None) -> None:
    for name in ("data", "versions"):
        (root / name).mkdir(exist_ok=True)
    if version is not None:
        snapshot = draft / "version.lock"
        shutil.copyfile(draft / "trid3nt.lock", snapshot)
        snapshot.replace(root / "versions" / f"{version}.lock")
    if (root / "case.py").exists() or (root / "trid3nt.lock").exists():
        return
    try:
        os.link(draft / "case.py", root / "case.py")
    except FileExistsError:
        return
    try:
        os.link(draft / "trid3nt.lock", root / "trid3nt.lock")
    except OSError:
        (root / "case.py").unlink()
        raise
