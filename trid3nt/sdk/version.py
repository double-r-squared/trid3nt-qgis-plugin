"""Replay the finished run and declaration pinned by a case version."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from trid3nt.errors import DeclarationError
from trid3nt.inputs.run import FinishedRun

from .lock import Lock, _check, _freeze


class Version(dict):
    """The existing lock snapshot with access to the model it describes."""

    def __init__(self, case: Any, version: str, snapshot: dict):
        super().__init__(snapshot)
        self.case, self.version = case, version

    @property
    def model(self):
        from .model import Model
        from .template import Template, load

        rows = [(key, value) for key, value in self["models"].items() if value.get("model") == self.version]
        if len(rows) != 1 or "configuration" not in rows[0][1]:
            raise DeclarationError("The version has no pinned model declaration.", error_code="VERSION_MODEL_MISSING")
        key, row = rows[0]
        declaration, config = row["template"], row["configuration"]
        lock = Lock(self.case.root)
        source_hash = declaration["version"]
        upstream = declaration.get("upstream", declaration["name"])
        upstream_hash = declaration.get("upstream_version", source_hash)
        path, source = lock.template_source(source_hash)
        pending = None
        if declaration["path"] == "called" and source_hash != upstream_hash:
            base_path, base_source = lock.template_source(upstream_hash)
            base = load(base_source, base_path)
            pending = (self["called_templates"][upstream]["module"], base.template)
        template = Template(key, declaration["path"], source_hash, upstream, upstream_hash,
                            key if declaration["path"] == "copied" else None)
        model = Model(self.case, load(source, path, upstream=pending), config["inputs"], template=template)
        for name, value in config.get("keywords", {}).items():
            model[name] = value
        object.__setattr__(model, "_native_study", config.get("native_study") or {})
        model.steps = config.get("steps", {})
        model.strict = config.get("strict", False)
        object.__setattr__(model, "_base_run", row.get("finished"))
        if model.version != self.version:
            raise DeclarationError("The version declaration did not match its hash.", error_code="VERSION_MODEL_CHANGED")
        return model


def hold_finished(lock: Lock, run: FinishedRun, *, served: bool = False) -> dict[str, Any]:
    """Pin the input deck and mesh for a study independently of the solver cache."""
    files: dict[str, str] = {}
    document = asdict(run)
    pin = lock._pointed if served else lock._took
    frozen = _freeze({key: value for key, value in document.items() if key not in {"record", "layers"}},
                     lambda uri: pin(uri, files), all_s3=served)
    frozen.update(record=document["record"], layers=document["layers"])
    return {"run": frozen, "files": files}


def finished(lock: Lock, held: Mapping[str, Any]) -> FinishedRun:
    from trid3nt.tools.mesh.artifact import MeshArtifact

    for path, digest in held["files"].items():
        target = lock.local(path)
        if not target.is_file() and lock.data.get("served"):
            lock._served(path, digest)
        _check(target, digest)
    values = dict(held["run"])
    if values.get("mesh") and isinstance(values["mesh"].get("artifact"), dict):
        values["mesh"] = {**values["mesh"], "artifact": MeshArtifact.from_json(values["mesh"]["artifact"])}
    return FinishedRun(**values)
