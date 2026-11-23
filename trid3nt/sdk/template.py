"""Load a case's pinned workflow declarations in isolation from the tool registry."""

from __future__ import annotations

import builtins
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from trid3nt.errors import DeclarationError
from trid3nt.workflows.runtime.workflow import Workflow, unregistered


@dataclass(frozen=True)
class TemplateFix:
    call: str
    args: tuple[str, ...]
    diff: str | None = None


@dataclass(frozen=True)
class TemplateDrift:
    template: str
    pinned_sha256: str
    current_sha256: str
    fix: TemplateFix
    code: str = "TEMPLATE_DRIFT"


@dataclass(frozen=True)
class Template:
    key: str
    path: str
    sha256: str
    upstream: str
    upstream_sha256: str
    copy: str | None = None

    def warning(self, source: bytes | None) -> TemplateDrift | None:
        if source is None:
            return None
        current = digest(source)
        if current == self.upstream_sha256:
            return None
        call = "copy_template" if self.copy is not None else "repin_template"
        return TemplateDrift(self.upstream, self.upstream_sha256, current, TemplateFix(call, (self.upstream,), self.copy))


def digest(source: bytes) -> str:
    return hashlib.sha256(source).hexdigest()


def load(source: bytes, path: Path, *, upstream: tuple[str, ModuleType] | None = None) -> Workflow:
    """Build exactly one workflow from the module's declaration inside the registration hold."""
    name = f"_trid3nt_case_template_{uuid4().hex}"
    module = ModuleType(name)
    module.__file__ = str(path)
    if upstream is not None:
        module.__dict__["__builtins__"] = {**vars(builtins), "__import__": _import_from(upstream)}
    sys.modules[name] = module
    try:
        with unregistered() as workflows:
            exec(compile(source, str(path), "exec"), vars(module))  # noqa: S102 - explicitly selected Python template
        if len(workflows) != 1 or workflows[0].template is not module:
            raise ValueError("one workflow declaration required")
        return workflows[0]
    except Exception:  # noqa: BLE001 - a case module that cannot load refuses without exposing source text
        raise DeclarationError("The template must load and declare one workflow.", error_code="TEMPLATE_INVALID") from None
    finally:
        del sys.modules[name]


def _import_from(upstream: tuple[str, ModuleType]):
    name, pinned = upstream
    parent, _, leaf = name.rpartition(".")

    def read(module, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002 - __import__ signature
        if level == 0 and module == name:
            if fromlist:
                return pinned
            raise ImportError("Use a from import for the pinned template.")
        if level == 0 and module == parent and leaf in fromlist:
            siblings = tuple(item for item in fromlist if item != leaf)
            real = builtins.__import__(module, globals, locals, siblings or ("__name__",), level)
            package = ModuleType(parent)
            vars(package).update(vars(real))
            setattr(package, leaf, pinned)
            return package
        return builtins.__import__(module, globals, locals, fromlist, level)

    return read
