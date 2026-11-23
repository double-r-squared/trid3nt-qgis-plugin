"""Render a reviewed SDK fill as an independently checked case script."""

from __future__ import annotations

import json
import keyword
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping

from trid3nt import case
from trid3nt.errors import DeclarationError
from trid3nt.sdk import Form, Model
from trid3nt.workflows.runtime.fill import SourceRequest

__all__ = ["Script", "check_lines", "generate"]


@dataclass(frozen=True)
class Script:
    """A published case script or the typed refusal that prevented publication."""

    text: str = ""
    path: Path | None = None
    checked: bool = False
    refusal: tuple[str, str] | None = None
    check_code: str | None = None
    model_version: str | None = None


_VERIFY = """\
import json, runpy, sys
sys.modules["trid3nt_harness"] = None
sys.modules["trid3nt_server"] = None
scope = runpy.run_path(sys.argv[1])
form = scope["fill_case"]()
result = (form.deck.sha256 if form.ready and form.checked and form.deck else None, form.refusal)
print(json.dumps(result))
"""

_RUN = """
# %% Fill and run
def fill_case():
    return m.fill()


if __name__ == "__main__":
    form = fill_case()
    if not form.ready:
        raise SystemExit(form.refusal[0])
    result = m.run()
    if getattr(result, "code", None):
        raise SystemExit(result.code)
    print(result.run_id)
"""


def check_lines(template: str, values: Mapping[str, Any], form: Form, *,
                case_dir: str | os.PathLike[str], served: bool = False) -> Script:
    """Check the generated statements through a fresh SDK Model and return an unpublished draft."""
    c = case(case_dir, strict=True)
    if template not in c.templates():
        return _refused("CODEGEN_TEMPLATE_UNREGISTERED", "Select a registered template.")
    try:
        model = getattr(c, template)()
        text = _render(template, model, values, form, served=served)
    except Exception as error:
        if getattr(error, "error_code", None) is None:
            raise
        return _refused(error.error_code, "The SDK refused a script declaration.")
    if not form.ready or not form.checked or form.deck is None:
        code = form.refusal[0] if form.refusal else "CODEGEN_DECK_MISSING"
        return _refused(code, "Generation requires a ready, checked deck.")
    return Script(text=text, check_code="CODEGEN_LINE_CHECKED", model_version=model.version)


def generate(template: str, values: Mapping[str, Any], form: Form, *, case_dir: str | os.PathLike[str]) -> Script:
    """Publish a strict called-template case after the generated file reproduces the reviewed deck.

    The case directory supplies the locked inputs for both fills and is the working directory when running the script.
    """
    draft = check_lines(template, values, form, case_dir=case_dir)
    if draft.refusal is not None:
        return draft
    text = draft.text
    root = Path(case_dir).expanduser().resolve()
    target = root / "case.py"
    with TemporaryDirectory(dir=root, prefix=".codegen-") as tmp:
        candidate = Path(tmp) / target.name
        candidate.write_text(text, encoding="utf-8")
        digest, code = _check(candidate)
        if code is not None:
            return _refused(code, "The generated script's standalone fill refused.")
        if digest != form.deck.sha256:
            return _refused("CODEGEN_DECK_MISMATCH", "The generated deck differs from the reviewed deck.")
        try:
            os.link(candidate, target)
        except FileExistsError:
            if target.read_bytes() != text.encode("utf-8"):
                return _refused("CODEGEN_SCRIPT_EXISTS", "The case already holds a different script.")
    return Script(text, target, True)


def _refused(code: str, reason: str) -> Script:
    return Script(refusal=(code, reason))


def _check(path: Path) -> tuple[str | None, str | None]:
    done = subprocess.run([sys.executable, "-c", _VERIFY, str(path)],
                          cwd=path.parent.parent, capture_output=True, text=True, check=False)
    if done.returncode or not done.stdout.strip():
        return None, "CODEGEN_CHECK_FAILED"
    try:
        digest, refusal = json.loads(done.stdout.splitlines()[-1])
    except (ValueError, TypeError):
        return None, "CODEGEN_CHECK_FAILED"
    return digest, refusal[0] if refusal else None


def _render(template: str, model: Model, values: Mapping[str, Any], form: Form, *, served: bool = False) -> str:
    lines = ["# %% Case", "import trid3nt", "", 'c = trid3nt.case(".", strict=True)']
    if served:
        lines.append("c.pack()")
    lines.append(f"m = c.{template}()")
    inputs = dict(values)
    keywords = inputs.pop("keywords", {}) or {}
    picks = inputs.pop("picks", {}) or {}
    if not isinstance(keywords, Mapping):
        raise DeclarationError("The keywords declaration takes a mapping.", error_code="KEYWORD_REFUSED")
    if not isinstance(picks, Mapping):
        raise DeclarationError("The picks declaration takes a mapping.")
    inputs.update({name: {"source": source} for name, source in picks.items()})
    inputs.update(keywords)

    requested_steps = inputs.pop("steps", None)
    if requested_steps is not None:
        model.steps = requested_steps
        if set(requested_steps) != form.steps.keys() or any(
                model.steps[name] != declared for name, declared in form.steps.items()):
            raise DeclarationError("Stated coverage steps differ from the reviewed fill.",
                                   error_code="CODEGEN_STEP_TRACE_MISMATCH")
    strict = inputs.pop("strict", None)
    if strict is not None and strict is not True:
        raise DeclarationError("Generation requires a strict fill.", error_code="CODEGEN_STRICT_REQUIRED")
    time = form.verdicts.get("event_time")
    if time is not None and isinstance(inputs.get("event_time"), str) and inputs["event_time"].lower() == "now":
        inputs["event_time"] = time.value
    for name, verdict in form.verdicts.items():
        if getattr(verdict.origin, "value", None) == "user" and verdict.value is not None:
            inputs.setdefault(name, verdict.value)

    lines.extend(("", "# %% Inputs"))
    for name in dict.fromkeys((*inputs, *form.choices)):
        choices = form.choices.get(name, ())
        if not choices:
            lines.append(_set(model, name, inputs[name]))
            continue
        lines.extend(_source_lines(model, name, inputs.get(name), choices))
    if form.steps:
        steps = {name: [(step.name, dict(step.args)) for step in declared] for name, declared in form.steps.items()}
        model.steps = steps
        lines.extend(("", "# %% Coverage", f"m.steps = {_literal(steps)}"))
    return "\n".join([*lines, _RUN])


def _source_lines(model: Model, name: str, value: Any, choices: tuple[SourceRequest, ...]) -> list[str]:
    if any("source" in choice.options for choice in choices):
        raise DeclarationError("The request uses the reserved source selector as an option.",
                               error_code="CODEGEN_SOURCE_OPTION_CONFLICT")
    asks = [{"source": choice.source, **choice.options} for choice in choices]
    reasons = [f"  # ranked {choice.rank} of {choice.candidate_count}" for choice in choices]
    if len(asks) == 1 and not isinstance(value, (tuple, list)):
        return [_set(model, name, asks[0]) + reasons[0]]
    rows = list(zip(asks, reasons, strict=True))
    if isinstance(value, (tuple, list)):
        named_count = sum(isinstance(item, Mapping) and "source" in item for item in value)
        if named_count != len(rows):
            raise DeclarationError("The recorded choices do not cover all named surfaces.",
                                   error_code="CODEGEN_SOURCE_TRACE_INCOMPLETE")
        traced = iter(rows)
        rows = [next(traced) if isinstance(item, Mapping) and "source" in item else (item, "") for item in value]
    model[name] = [item for item, _ in rows]
    return [f"{_target(name)} = [", *(f"    {_literal(item)},{reason}" for item, reason in rows), "]"]


def _target(name: str) -> str:
    if name.isidentifier() and not name.startswith("_") and not keyword.iskeyword(name):
        return f"m.{name}"
    return f"m[{name!r}]"


def _set(model: Model, name: str, value: Any) -> str:
    model[name] = value
    return f"{_target(name)} = {_literal(value)}"


def _literal(value: Any) -> str:
    if value is None or type(value) in (bool, int, str) or (type(value) is float and math.isfinite(value)):
        return repr(value)
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{_literal(key)}: {_literal(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(map(_literal, value)) + "]"
    if isinstance(value, tuple):
        return "(" + ", ".join(map(_literal, value)) + ("," if len(value) == 1 else "") + ")"
    raise DeclarationError("A value has no supported Python literal.", error_code="CODEGEN_LITERAL_UNSUPPORTED")
