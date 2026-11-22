"""A Model: one simulation over one template, edited by attribute, checked on set, filled and run in-process.

Every name resolves through the workflow's one lookup, and a value is held as the run states it, so the fill
takes it as it takes a call's. The model is held unattended: nothing asks a person, the person's AUTO setting
is never read, and no source is picked for an input the model leaves unstated. It fills and runs in its case
directory, and what it read and ran is written to the case's lock.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from trid3nt.inputs.review import unattended
from trid3nt.render.emission import bind_turn_case
from trid3nt.workflows.runtime.data import refuse_wrong_shape
from trid3nt.workflows.runtime.errors import said
from trid3nt.workflows.runtime.fill import Fill, Verdict
from trid3nt.workflows.runtime.sheet import Keyword

if TYPE_CHECKING:
    from .case import Case

__all__ = ["Failure", "Form", "Model", "Result"]

#: The origin a keyword nobody states carries: the module dictionary's own value.
_ENGINE = "engine default"


@dataclass(frozen=True)
class Form:
    """What the fill answered: every input's verdict by name, whether the run is ready, and the ``(code, sentence)``
    that keeps it from ready."""

    verdicts: Mapping[str, Verdict]
    ready: bool
    refusal: tuple[str, str] | None = None
    notes: tuple[str, ...] = ()

    def __getitem__(self, name: str) -> Verdict:
        return self.verdicts[name]

    def __repr__(self) -> str:
        lines = [f"Form ready={self.ready}" + ("" if self.refusal is None else f" refused {self.refusal[0]}")]
        for name, verdict in self.verdicts.items():
            origin = verdict.origin.value if verdict.origin is not None else ""
            lines.append(f"  {name}: {verdict.state} {_short(verdict.value)}"
                         + (f" [{origin}{', ' + verdict.source if verdict.source else ''}]" if origin else "")
                         + (f" {verdict.code}: {verdict.reason}" if verdict.code else "")
                         + (f" candidates: {', '.join(verdict.remedies)}" if verdict.remedies else ""))
        return "\n".join(lines)


@dataclass(frozen=True)
class Result:
    """A run that solved and published: its run id, the typed result it published, and its journal record."""

    run_id: str | None
    value: Any
    record: Mapping[str, Any] = field(default_factory=dict)

    @property
    def outputs(self) -> list[Mapping[str, Any]]:
        """The layers the run published, off its own record."""
        return list(self.record.get("outputs") or [])


@dataclass(frozen=True)
class Failure:
    """A run that did not solve: the code and sentence it refused or failed on, and the form where the fill refused."""

    code: str
    sentence: str
    form: Form | None = None


class _Body:
    """One coupled body of a model, by its module word: ``m.waqtel.X`` is ``m["waqtel: X"]``."""

    __slots__ = ("_model", "_word")

    def __init__(self, model: "Model", word: str) -> None:
        object.__setattr__(self, "_model", model)
        object.__setattr__(self, "_word", word)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._model[f"{self._word}: {name}"]

    def __setattr__(self, name: str, value: Any) -> None:
        self._model[f"{self._word}: {name}"] = value


class Model:
    """One simulation over one template: its inputs, the engine's keywords and each coupled body by attribute.
    A set is checked by the input's own accept and raises its typed refusal; ``None`` unstates a value.
    ``fill()`` returns the form and never raises for an input; ``run()`` returns a ``Result`` or a ``Failure``."""

    def __init__(self, case: "Case", workflow: Any, values: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_case", case)
        object.__setattr__(self, "_workflow", workflow)
        object.__setattr__(self, "_stated", {})
        object.__setattr__(self, "_keywords", {})
        #: Each input stated as outside data, by name: its lock entry, the copy in the case's data.
        object.__setattr__(self, "_taken", {})
        for name, value in values.items():
            self[name] = value

    def _held(self, name: str) -> tuple[str, Any]:
        """The key a name is stated under and what it names: an input by its own name, a keyword by its identifier,
        qualified by its body's module word where the body is not the carrier."""
        workflow = self._workflow
        try:
            held = workflow.input(name)
        except Exception as refused:  # noqa: BLE001 - the lowercase alias, else the lookup's own refusal
            if getattr(refused, "retryable", False) or name == name.upper():
                raise
            try:
                held = workflow.input(name.upper())
            except Exception:  # noqa: BLE001
                raise refused from None
        if isinstance(held, Keyword):
            return (held.name if held.carried else f"{held.body.MODULE}: {held.name}"), held
        return held.name, held

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self[name]
        except Exception as refused:  # noqa: BLE001 - an attribute nothing names is an AttributeError
            if name in self._workflow.bodies():
                return _Body(self, name)
            raise AttributeError(str(refused)) from refused

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def __getitem__(self, name: str) -> Any:
        key, held = self._held(name)
        return self._value(key, held)[0]

    def __setitem__(self, name: str, value: Any) -> None:
        key, held = self._held(name)
        held_in = self._keywords if isinstance(held, Keyword) else self._stated
        if value is None:
            held_in.pop(key, None)
            self._taken.pop(key, None)
            return
        taken = None
        if not isinstance(held, Keyword) and held.is_row and isinstance(value, str) and _outside(value):
            # Outside data is copied into the case and the slot reads the copy: a replay reads the same bytes.
            value, taken = self._case.take(value)
        if isinstance(held, Keyword) or not held.is_row:
            held.accept(value)
        elif not (isinstance(value, Mapping) and "source" in value):
            refuse_wrong_shape(held, value)
        held_in[key] = value
        if taken is not None:
            self._taken[key] = taken
        else:
            self._taken.pop(key, None)

    def _value(self, key: str, held: Any) -> tuple[Any, str]:
        """The value in force for one name and its origin: the model's, else the template's, else the engine's."""
        if isinstance(held, Keyword):
            if key in self._keywords:
                return self._keywords[key], "user"
            asserted = getattr(held.body, "ASSERTED", {})
            if held.name in asserted:
                return asserted[held.name], "template"
            slot = held.body.MODULE_INPUT.get(held.name)
            return (None, "") if slot is None else (slot.engine_default, _ENGINE)
        if key in self._stated:
            return self._stated[key], "user"
        default = getattr(held, "default", None)
        return (default, "template") if default is not None else (None, "")

    def _wire(self) -> dict[str, Any]:
        return {**self._stated, **({"keywords": dict(self._keywords)} if self._keywords else {})}

    @property
    def version(self) -> str:
        """The content hash of what this model states: its template, its inputs and its keywords."""
        stated = {"template": self._workflow.name, "inputs": self._stated, "keywords": self._keywords}
        return hashlib.sha256(json.dumps(stated, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _named(self) -> dict[str, str]:
        return {"name": self._workflow.name, "version": self.version}

    async def _fill(self) -> Fill:
        bind_turn_case(self._case.name)
        with self._case.bound(), unattended():
            state = await self._workflow.filled(self._wire(), model=self._named())
            self._locked(state)
        return state

    def fill(self) -> Form:
        """Every input answered, as the run would take it -> the form; nothing is solved. Each fetch is read
        through the case's lock, and what the fill read is written to it."""
        return _form(asyncio.run(self._fill()))

    def _locked(self, state: Fill) -> None:
        """The model's row of the case's lock after a fill: its template and version, and what each input read."""
        lock = self._case.lock
        row = lock.data["models"].get(self._workflow.name) or {}
        if row.get("model") != self.version:
            row = {}
        row.update(template={"name": self._workflow.name, "version": _template_version(self._workflow)},
                   model=self.version, inputs=_read(lock, state, self._taken))
        lock.data["models"][self._workflow.name] = row
        lock.save()

    async def _run(self) -> Result | Failure:
        bind_turn_case(self._case.name)
        named = self._named()
        with self._case.bound(), unattended():
            state = await self._workflow.filled(self._wire(), model=named)
            self._locked(state)
            if not state.ready:
                code, sentence = state.refusal()
                return Failure(code, sentence, _form(state))
            try:
                published = await self._workflow.execute(state)
            except Exception as failed:  # noqa: BLE001 - a typed failure is returned, anything else is a fault
                if getattr(failed, "error_code", None) is None:
                    raise
                return Failure(str(failed.error_code), said(failed))
            if isinstance(published, Mapping) and published.get("status") == "error":
                return Failure(str(published.get("error_code")), str(published.get("error_message")))
            record = await asyncio.to_thread(_record, self._case.name, named)
            await asyncio.to_thread(self._ran, record)
        return Result(run_id=record.get("run_id"), value=published, record=record)

    def _ran(self, record: Mapping[str, Any]) -> None:
        """What the run ran, onto the model's row of the lock - its mesh, its deck, the images of its engine - and
        the lock as it then stands kept under ``versions/`` by the model's version."""
        lock = self._case.lock
        row = lock.data["models"][self._workflow.name]
        run_id, engine = str(record.get("run_id") or ""), str(record.get("engine") or "")
        row["mesh"] = (record.get("mesh") or {}).get("key")
        row["deck"] = _deck(self._case.root, engine, run_id)
        row["images"] = _images(self._case.root, engine, run_id)
        lock.save()
        (self._case.root / "versions" / f"{self.version}.lock").write_text(
            lock.path.read_text(encoding="utf-8"), encoding="utf-8")

    def run(self) -> Result | Failure:
        """Fill, then solve, publish and record in this process -> the result, or the failure it refused or failed on."""
        return asyncio.run(self._run())

    def __repr__(self) -> str:
        workflow = self._workflow
        lines = [f"Model {workflow.name} in case {self._case.name} (version {self.version[:12]})"]
        for row in workflow.inputs:
            value, origin = self._value(row.name, row)
            lines.append(f"  {row.name} = {_short(value)}" + (f"  [{origin}]" if origin else "  [unstated]"))
        steering = getattr(workflow, "steering", None)
        asserted = dict(getattr(steering, "ASSERTED", {}) or {})
        for name in [*asserted, *(k for k in self._keywords if k not in asserted)]:
            stated = name in self._keywords
            value = self._keywords[name] if stated else asserted[name]
            lines.append(f"  {name} = {_short(value)}  [{'user' if stated else 'template'}]")
        lines.append(f"  every other keyword = the module dictionary's own value  [{_ENGINE}]")
        return "\n".join(lines)


def _form(state: Fill) -> Form:
    return Form(verdicts=dict(state.inputs), ready=state.ready,
                refusal=None if state.ready else state.refusal(),
                notes=(*state.notes, *state.said[0], *state.moved))


def _record(case: str, named: Mapping[str, str]) -> dict[str, Any]:
    """The journal record this model's run appended: the last one of its case, template and version."""
    from trid3nt.store.journal import read_records

    return next((record for record in reversed(read_records())
                 if record.get("case") == case and record.get("template") == named["name"]
                 and record.get("version") == named["version"]), {})


def _outside(value: str) -> bool:
    """Whether a stated string names outside data the case copies: a file, an http(s) URL or an s3 URI."""
    if value.startswith(("http://", "https://", "s3://")):
        return True
    try:
        return Path(value).expanduser().is_file()
    except (OSError, ValueError):
        return False


def _template_version(workflow: Any) -> str:
    """The content hash of the template module a workflow is declared by."""
    return hashlib.sha256(Path(workflow.template.__file__).read_bytes()).hexdigest()


def _read(lock: Any, state: Fill, taken: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Each input the fill read off a pinned object, by name: its source, the request, the file and its sha256."""
    fetched = [lock.data["fetched"][key] for key in dict.fromkeys(lock.read) if key in lock.data["fetched"]]
    read: dict[str, Any] = {name: dict(entry) for name, entry in taken.items()}
    for name, verdict in state.inputs.items():
        entry = next((entry for entry in fetched if verdict.source and entry["source"] == verdict.source), None)
        if name in read or entry is None:
            continue
        uri, digest = _primary(lock, entry)
        read[name] = {"source": entry["source"], "request": entry["request"], "uri": uri, "sha256": digest}
    return read


def _primary(lock: Any, entry: Mapping[str, Any]) -> tuple[str, str]:
    """The file a pinned fetch answered with: a layer's own file, else the pinned value itself."""
    value = json.loads(lock.local(entry["uri"]).read_bytes())
    uri = (value.get("fields") or {}).get("uri") if isinstance(value, dict) else None
    if isinstance(uri, str) and uri.startswith("s3://data/"):
        relative = uri[len("s3://"):]
        return relative, dict(entry.get("files") or {})[relative]
    return entry["uri"], entry["sha256"]


def _deck(root: Path, engine: str, run_id: str) -> dict[str, Any] | None:
    """The deck a run staged: the sha256 of every file the engine received, by its name in the run directory,
    and one sha256 over them all; ``None`` where the run staged none."""
    from trid3nt.store.objects import FileStore, sha256_of

    manifest = root / "cache" / engine / run_id / "manifest.json"
    if not engine or not run_id or not manifest.is_file():
        return None
    store = FileStore(root)
    files = {}
    for row in json.loads(manifest.read_text(encoding="utf-8")).get("inputs") or ():
        bucket, _, key = str(row["gs_uri"])[len("s3://"):].partition("/")
        files[str(row["dest"])] = sha256_of(store.path(bucket, key))
    files = dict(sorted(files.items()))
    return {"sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest(),
            "files": files}


def _images(root: Path, engine: str, run_id: str) -> dict[str, Any]:
    """The image each engine of the run ran in and the id docker held it under, off the run's completion."""
    completion = root / "runs" / run_id / "completion.json"
    if not run_id or not completion.is_file():
        return {}
    done = json.loads(completion.read_text(encoding="utf-8"))
    return {engine: {"image": done.get("image"), "id": done.get("image_id")}} if done.get("image") else {}


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 80 else f"{text[:77]}..."
