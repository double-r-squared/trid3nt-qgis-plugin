"""A Model: one simulation over one template, edited by attribute, checked on set, filled to its deck and run in-process.

Every name resolves through the workflow's one lookup, and a value is held as the run states it, so the fill
takes it as it takes a call's. The model is held unattended: nothing asks a person, the person's AUTO setting
is never read, and no source is picked for an input the model leaves unstated. It fills and runs in its case
directory, and what it read, wrote and ran is written to the case's lock. ``strict`` and ``steps`` are the model's
own: whether an unresolved coverage shortfall refuses, and the coverage steps it states over its template's.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from trid3nt.errors import DeclarationError
from trid3nt.inputs.coverage import Coverage, Shortfall, Step, strictness
from trid3nt.inputs.review import unattended
from trid3nt.inputs.slots import BED
from trid3nt.render.emission import bind_turn_case
from trid3nt.workflows.runtime.data import refuse_wrong_shape
from trid3nt.workflows.runtime.errors import said
from trid3nt.workflows.runtime.fill import Fill, SourceRequest, Verdict
from trid3nt.workflows.runtime.sheet import Keyword
from trid3nt.workflows.runtime.workflow import Deck

from .template import Template, TemplateDrift

if TYPE_CHECKING:
    from .case import Case

__all__ = ["Failure", "Form", "Model", "Result", "Staged"]

#: The origin a keyword nobody states carries: the module dictionary's own value.
_ENGINE = "engine default"


@dataclass(frozen=True)
class Staged:
    """The deck a fill wrote: the run it is tagged with, the sha256 of every file the engine receives by its name in
    the run directory, one sha256 over them all (the lock's deck hash), and the content key of the mesh under it."""

    run_id: str
    files: Mapping[str, str]
    sha256: str
    mesh: str | None = None


@dataclass(frozen=True)
class Form:
    """What the fill answered: every input's verdict by name, whether the run is ready, the ``(code, sentence)``
    that keeps it from ready, the deck it wrote, and whether that staged deck passed the engine's own check
    (a deck is staged only once it has; where the check refused, the refusal's code is ``<ENGINE>_STAGED_*``).
    The form carries each input's coverage, unresolved shortfalls allowed under non-strict mode, and template drift."""

    verdicts: Mapping[str, Verdict]
    ready: bool
    refusal: tuple[str, str] | None = None
    notes: tuple[str, ...] = ()
    deck: Staged | None = None
    checked: bool = False
    coverage: Mapping[str, Coverage] = field(default_factory=dict)
    warnings: tuple[Shortfall | TemplateDrift, ...] = ()
    choices: Mapping[str, tuple[SourceRequest, ...]] = field(default_factory=dict)
    steps: Mapping[str, tuple[Step, ...]] = field(default_factory=dict)
    review_choices: tuple[Any, ...] = ()
    review_coverage: tuple[str, ...] = ()

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
        for name, covered in self.coverage.items():
            lines.append(f"  {name} coverage: " + ", ".join(f"{source} {share:.4g}" for source, share in covered.shares)
                         + f", unmeasured {covered.unmeasured:.4g}")
        for warning in self.warnings:
            if isinstance(warning, TemplateDrift):
                lines.append(f"  warning {warning.code} {warning.template}: "
                             f"{warning.pinned_sha256} -> {warning.current_sha256}")
            else:
                lines.append(f"  warning {warning.code} {warning.input}: {warning.cells} cells, share {warning.share:.4g}; "
                             f"remedies: {', '.join(map(repr, warning.remedies))}")
        if self.deck is not None:
            lines.append(f"  deck {self.deck.sha256[:12]} ({len(self.deck.files)} files, run {self.deck.run_id}"
                         + (f", mesh {self.deck.mesh}" if self.deck.mesh else "") + "), checked")
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

    def __init__(self, case: "Case", workflow: Any, values: Mapping[str, Any], *, template: Template) -> None:
        object.__setattr__(self, "_case", case)
        object.__setattr__(self, "_workflow", workflow)
        object.__setattr__(self, "_template", template)
        object.__setattr__(self, "_stated", {})
        object.__setattr__(self, "_keywords", {})
        #: Each input stated as outside data, by name: its lock entry, the copy in the case's data.
        object.__setattr__(self, "_taken", {})
        #: The deck the last fill wrote, held for the run until a set or another fill supersedes it.
        object.__setattr__(self, "_written", None)
        #: Whether an unresolved shortfall refuses, where the model states it (else the case's), and the coverage
        #: steps the model states by input over the ones its template's rows declare.
        object.__setattr__(self, "_strict", None)
        object.__setattr__(self, "_steps", {})
        for name, value in values.items():
            setattr(self, name, value)

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
        if name == "strict":
            if value is not None and not isinstance(value, bool):
                raise DeclarationError(f"strict is True or False, not {value!r}.")
            object.__setattr__(self, "_strict", value)
            return
        if name == "steps":
            object.__setattr__(self, "_steps", self._stepped(value))
            return
        self[name] = value

    @property
    def strict(self) -> bool:
        """Whether template drift or an unresolved shortfall refuses, inherited from the case unless set here."""
        return self._case.strict if self._strict is None else self._strict

    @property
    def steps(self) -> dict[str, tuple[Any, ...]]:
        """The coverage steps in force by input: the model's, else the ones its template's row declares."""
        return {row.name: self._steps.get(row.name, row.coverage_steps)
                for row in self._workflow.inputs if getattr(row.slot, "steps", None)}

    def _stepped(self, value: Any) -> dict[str, tuple[Any, ...]]:
        """The steps a model states, by input, each held to what its row's slot runs; ``None`` states none of its own."""
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise DeclarationError("steps is a mapping of input name to its steps, in the order they run.")
        held = {}
        for name, declared in value.items():
            row = next((r for r in self._workflow.inputs if r.name == name), None)
            if row is None or not getattr(row.slot, "steps", None):
                raise DeclarationError(f"{name!r} is no input of {self._workflow.name} that takes steps.")
            held[name] = row.steps(*(declared or ())).coverage_steps
        return held

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
        else:
            value = _checked_row(held, value)
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
        """The content hash of what this model states: its template, its inputs, its keywords, and its steps and
        strictness where it states them."""
        stated = {"template": self._template.key, "template_sha256": self._template.sha256,
                  "upstream_sha256": self._template.upstream_sha256, "inputs": self._stated, "keywords": self._keywords,
                  **({"steps": {k: [[s.name, list(s.args)] for s in v] for k, v in self._steps.items()}}
                     if self._steps else {}),
                  **({"strict": True} if self.strict else {})}
        return hashlib.sha256(json.dumps(stated, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _named(self) -> dict[str, str]:
        return {"name": self._template.key, "version": self.version, "template_path": self._template.path,
                "template_sha256": self._template.sha256,
                "template_upstream_sha256": self._template.upstream_sha256}

    def _template_warning(self) -> TemplateDrift | None:
        return self._template.warning(self._case._upstream_source(self._template.upstream))

    async def _filling(self, warning: TemplateDrift | None) -> tuple[Fill, Staged | None, tuple[str, str] | None]:
        """The fill, then the launch up to its written, checked deck where the fill is ready -> the fill, the deck,
        and the typed refusal the launch stopped on; the deck is held for the run, and the lock names what was read
        and written. Runs inside the case's binding."""
        self._drop()
        if warning is not None and self.strict:
            return Fill(self._workflow), None, (warning.code, "The upstream template differs from its pin.")
        state = await self._workflow.filled(self._wire(), model=self._named(), steps=self._steps)
        deck, refusal = None, None
        if state.ready:
            try:
                deck = await self._workflow.prepared(state)
            except Exception as failed:  # noqa: BLE001 - a typed refusal is the form's, anything else is a fault
                if getattr(failed, "error_code", None) is None:
                    raise
                refusal = (str(failed.error_code), said(failed))
        staged = _staged(self._case.root, deck)
        self._locked(state, staged)
        if deck is not None:
            object.__setattr__(self, "_written", _Written(self.version, self._row(), state, deck, staged))
        return state, staged, refusal

    async def _fill(self) -> Form:
        from .script_run import current, load_deck

        child = current()
        if child is not None:
            handoff = child.handoff
            deck = load_deck(handoff["deck"], self._workflow)
            deck.state.settings = handoff.get("settings", deck.state.settings)
            row = handoff["row"]
            staged = Staged(deck.run_id, row["deck"]["files"], row["deck"]["sha256"], row.get("mesh"))
            object.__setattr__(self, "_written", _Written(row["model"], row, deck.state, deck, staged))
            if await asyncio.to_thread(self._current) is None:
                self._refuse_reviewed()
            child.verify()
            return _form(deck.state, staged, strict=self.strict)
        bind_turn_case(self._case.name)
        warning = self._template_warning()
        with self._case.bound(), unattended(), strictness(self.strict):
            state, staged, refusal = await self._filling(warning)
        return _form(state, staged, refusal, strict=self.strict, warning=warning)

    @staticmethod
    def _refuse_reviewed() -> None:
        raise DeclarationError("The reviewed deck changed before launch.", error_code="SCRIPT_DECK_STALE")

    def fill(self) -> Form:
        """Every input answered and, where they all are, the deck written and checked -> the form; nothing is solved.
        Each fetch is read through the case's lock, and what the fill read and wrote is written to it."""
        return asyncio.run(self._fill())

    def _locked(self, state: Fill, staged: Staged | None) -> None:
        """The model's row of the case's lock after a fill: its template and version, what each input read, and
        the mesh and deck it wrote."""
        lock = self._case.lock
        row = lock.data["models"].get(self._template.key) or {}
        if row.get("model") != self.version:
            row = {}
        row.update(template={"name": self._workflow.name, "version": self._template.sha256,
                             "path": self._template.path, "upstream": self._template.upstream,
                             "upstream_version": self._template.upstream_sha256},
                   model=self.version, inputs=_read(lock, state, self._taken),
                   mesh=None if staged is None else staged.mesh,
                   deck=None if staged is None else {"sha256": staged.sha256, "files": dict(staged.files)})
        lock.data["models"][self._template.key] = row
        lock.save()

    def _row(self) -> dict[str, Any]:
        """The model's row of the case's lock as the file holds it, without the images a run adds."""
        from .lock import Lock

        row = dict(Lock(self._case.root).data["models"].get(self._template.key) or {})
        row.pop("images", None)
        return row

    def _current(self) -> "_Written | None":
        """The deck the last fill wrote, where it is still this model's: the version the model states and its row of
        the case's lock are the ones the fill left, and every staged file still holds the bytes the deck hash names."""
        written = self._written
        if written is None or written.version != self.version or written.row != self._row():
            return None
        from .script_run import current

        if written.staged is not None and _deck(
                self._case.root, written.deck.staged.get("inputs") or (), served=current() is not None) != {
                "sha256": written.staged.sha256, "files": dict(written.staged.files)}:
            return None
        return written

    def _drop(self) -> None:
        """The deck the last fill wrote, superseded: its staged copy removed, as a launch that does not reach the box
        leaves none. Runs inside the case's binding."""
        from trid3nt.workflows.solver.staging import discard_run

        written = self._written
        object.__setattr__(self, "_written", None)
        if written is not None and written.deck.run_id:
            discard_run(written.deck.run_id, written.deck.prefix, stem=self._workflow.engine)

    async def _run(self) -> Result | Failure:
        from .script_run import current, interruptible

        if (child := current()) is not None:
            bind_turn_case(child.handoff["case"])
            with interruptible(), unattended(), strictness(self.strict):
                written = await asyncio.to_thread(self._current)
                if written is None:
                    self._refuse_reviewed()
                object.__setattr__(self, "_written", None)
                published = await self._workflow.execute(written.state, written.deck)
            if isinstance(published, Mapping) and published.get("status") == "error":
                child.failure = (str(published["error_code"]), str(published["error_message"]))
                return Failure(*child.failure)
            return Result(written.deck.run_id, published, child.record or {})
        bind_turn_case(self._case.name)
        named = self._named()
        warning = self._template_warning()
        with self._case.bound(), unattended(), strictness(self.strict):
            written = None if warning is not None and self.strict else self._current()
            if written is None:
                state, staged, refusal = await self._filling(warning)
                if not state.ready or refusal is not None:
                    code, sentence = refusal or state.refusal()
                    return Failure(code, sentence, _form(state, staged, refusal, strict=self.strict, warning=warning))
                written = self._written
            # A deck is solved once: the next run fills again, under a new run id.
            object.__setattr__(self, "_written", None)
            try:
                published = await self._workflow.execute(written.state, written.deck)
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
        """What the run ran, onto the model's row of the lock - the images of its engine, and the mesh and deck of a
        launch that wrote its deck inside its solve - and the lock as it then stands kept under ``versions/`` by the
        model's version."""
        lock = self._case.lock
        row = lock.data["models"][self._template.key]
        run_id, engine = str(record.get("run_id") or ""), str(record.get("engine") or "")
        if row.get("deck") is None:
            row["mesh"] = (record.get("mesh") or {}).get("key")
            row["deck"] = _deck(self._case.root, _manifest_rows(self._case.root, engine, run_id))
        row["images"] = _images(self._case.root, engine, run_id)
        lock.save()
        (self._case.root / "versions" / f"{self.version}.lock").write_text(
            lock.path.read_text(encoding="utf-8"), encoding="utf-8")

    def run(self) -> Result | Failure:
        """Solve, publish and record the deck the last fill wrote, where it is still this model's; otherwise fill
        again first -> the result, or the failure it refused or failed on."""
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
        for name, declared in self.steps.items():
            lines.append(f"  {name} steps = {list(declared)}  [{'user' if name in self._steps else 'template'}]")
        lines.append(f"  strict = {self.strict}  [{'user' if self._strict is not None else 'case'}]")
        return "\n".join(lines)


@dataclass(frozen=True)
class _Written:
    """A deck a fill wrote, with what it is current against: the model's version and its row of the case's lock."""

    version: str
    row: Mapping[str, Any]
    state: Fill
    deck: Deck
    staged: Staged | None


def _form(state: Fill, staged: Staged | None = None, refusal: tuple[str, str] | None = None, *,
          strict: bool = False, warning: TemplateDrift | None = None) -> Form:
    ready = state.ready and refusal is None
    coverage = dict(state.coverage)
    warnings = () if strict else tuple(c.shortfall for c in coverage.values()
                                       if c.shortfall is not None and not c.shortfall.accepted)
    return Form(verdicts=dict(state.inputs), ready=ready,
                refusal=None if ready else (refusal or state.refusal()),
                notes=(*state.notes, *state.said[0], *state.moved),
                deck=staged, checked=staged is not None, coverage=coverage,
                warnings=warnings + ((warning,) if warning is not None else ()),
                choices={name: tuple(choices) for name, choices in
                         (state.env.choices or {}).items()} if state.env is not None else {},
                steps=dict(state.steps))


def _staged(root: Path, deck: Deck | None) -> Staged | None:
    """The deck a launch wrote, by what the engine receives; ``None`` where it staged none."""
    if deck is None or not deck.run_id or not deck.staged:
        return None
    hashed = _deck(root, deck.staged.get("inputs") or ())
    mesh = (deck.state.env.run.get("mesh") or {}) if deck.state.env is not None else {}
    return Staged(run_id=deck.run_id, files=hashed["files"], sha256=hashed["sha256"],
                  mesh=mesh.get("key") if isinstance(mesh, Mapping) else None)


def _record(case: str, named: Mapping[str, str]) -> dict[str, Any]:
    """The journal record this model's run appended: the last one of its case, template and version."""
    from trid3nt.store.journal import read_records

    return next((record for record in reversed(read_records())
                 if record.get("case") == case and record.get("model") == named["name"]
                 and record.get("version") == named["version"]), {})


def _checked_row(row: Any, value: Any) -> Any:
    if isinstance(value, Mapping) and "source" in value:
        from trid3nt.tools.fetchers._router.registration import get_spec
        from trid3nt.tools.fetchers._router.router import validate_options

        name = value["source"]
        spec = get_spec(name) if isinstance(name, str) else None
        if spec is None:
            raise DeclarationError("The named source is unregistered.", error_code="DATA_NEED_UNMATCHED")
        options = {key: item for key, item in value.items() if key not in {"source", "purpose", "visualize"}}
        if options:
            validate_options(spec, options)
        return deepcopy(dict(value))
    if row.slot is BED and isinstance(value, (list, tuple)):
        return [_checked_row(row, item) for item in value]
    refuse_wrong_shape(row, value)
    return value


def _outside(value: str) -> bool:
    """Whether a stated string names outside data the case copies: a file, an http(s) URL or an s3 URI."""
    if value.startswith(("http://", "https://", "s3://")):
        return True
    try:
        return Path(value).expanduser().is_file()
    except (OSError, ValueError):
        return False


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


def _manifest_rows(root: Path, engine: str, run_id: str) -> list[Mapping[str, Any]]:
    """Every file a run's staged manifest hands the engine; none where the run staged none."""
    manifest = root / "cache" / engine / run_id / "manifest.json"
    if not engine or not run_id or not manifest.is_file():
        return []
    return list(json.loads(manifest.read_text(encoding="utf-8")).get("inputs") or ())


def _deck(root: Path, rows: Any, *, served: bool = False) -> dict[str, Any] | None:
    """A staged deck by what the engine receives: the sha256 of every file by its name in the run directory, and one
    sha256 over them all; ``None`` where nothing is staged."""
    from trid3nt.store.objects import FileStore, sha256_of

    if not rows:
        return None
    from .lock import served_digest

    if served:
        files = {str(row["dest"]): served_digest(row["gs_uri"]) for row in rows}
    else:
        store = FileStore(root)
        files = {}
        for row in rows:
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
