"""A case: a directory that is the whole truth of its runs, with one factory per registered template.

``<case>/trid3nt.lock`` pins every byte a run read, held under ``data/<sha256>.<ext>``; ``runs/<run_id>/`` holds
what each run wrote and the journal its records; ``versions/`` a lock snapshot per model version that ran; and
``cache/`` the fetch cache, staged decks and meshes, which any run rebuilds. No run needs an object store: a
store is optional serving of the same tree, linked and packed by the paths relative to the case.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from trid3nt.errors import DeclarationError
from trid3nt.inputs.user_input import WireArgsError
from trid3nt.store import objects as storage
from trid3nt.store.objects import StorageError, sha256_of

from .lock import Lock
from .model import Model
from .template import Template, digest, load

logger = logging.getLogger("trid3nt.sdk.case")

__all__ = ["Case", "case"]

#: The directories every case holds; ``cache/`` is made by the first run that writes one.
_LAYOUT = ("data", "runs", "versions")

#: What a case links to a store: every file it holds but its cache, which any run rebuilds.
_UNSERVED = ("cache",)


def _workflow(template: str) -> Any:
    """The declared workflow a registered template runs; ``None`` for a name that is no template."""
    from trid3nt.tools import TOOL_REGISTRY

    registered = TOOL_REGISTRY.get(template)
    return getattr(getattr(registered, "fn", None), "workflow", None)


class Case:
    """One case directory: ``case.<template>(**values)`` makes a Model over that template, each value set as an
    attribute is; ``pack()`` and ``link(store)`` move it between packed and served."""

    def __init__(self, where: str | os.PathLike[str], *, strict: bool = False) -> None:
        if not isinstance(where, (str, os.PathLike)) or not str(where).strip():
            raise WireArgsError(f"a case is a directory, named by a non-empty path; got {type(where).__name__}.")
        if not isinstance(strict, bool):
            raise WireArgsError(f"strict is True or False, not {strict!r}.")
        #: Whether an unresolved coverage shortfall refuses in this case's models, where a model states none.
        self.strict = strict
        self.root = Path(where).expanduser().resolve()
        for name in _LAYOUT:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.name = self.root.name
        self.lock = Lock(self.root)

    def __getattr__(self, template: str) -> Callable[..., Model]:
        if template.startswith("_"):
            raise AttributeError(template)
        registered = _workflow(template)
        if registered is None and template not in Lock(self.root).data.get("called_templates", {}):
            raise AttributeError("no registered template is called so; the templates are "
                                 + ", ".join(self.templates()) + ".")

        def model(**values: Any) -> Model:
            lock = Lock(self.root)
            pin = self._called(lock, template)
            workflow = load(*_load_args(lock, pin["sha256"]))
            held = Template(template, "called", pin["sha256"], template, pin["sha256"])
            result = Model(self, workflow, values, template=held)
            lock.save()
            self.lock = lock
            return result

        model.__name__ = model.__qualname__ = template
        if registered is not None:
            model.__doc__ = _registered_fn(template).__doc__
        return model

    def copy_template(self, name: str, *, overwrite: bool = False) -> Path:
        """Write the registered module's source to the case. An edited destination requires overwrite."""
        registered = _workflow(name)
        source = self._upstream_source(name)
        if registered is None or source is None:
            raise DeclarationError("The registered template source is absent.", error_code="TEMPLATE_SOURCE_MISSING")
        lock = Lock(self.root)
        relative = f"templates/{registered.template.__name__.rsplit('.', 1)[-1]}.py"
        path = self._template_path(relative)
        copies = lock.data.setdefault("copied_templates", {})
        previous = copies.get(relative)
        if previous is not None and previous["name"] != name:
            raise DeclarationError("Another template owns the copy path.", error_code="TEMPLATE_COPY_CONFLICT")
        current = digest(source)
        if path.exists():
            baseline = previous["sha256"] if previous is not None else current
            if digest(path.read_bytes()) != baseline and not overwrite:
                raise DeclarationError("The template copy has edits.", error_code="TEMPLATE_COPY_EDITED")
        lock.hold_template(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as part:
            staged = Path(part.name)
        try:
            staged.write_bytes(source)
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)
        copies[relative] = {"name": name, "sha256": current, "source_sha256": current}
        lock.save()
        self.lock = lock
        return path

    def repin_template(self, name: str) -> None:
        """Take the current registered declaration for subsequent calls and extensions in this case."""
        lock = Lock(self.root)
        self._called(lock, name, take=True)
        lock.save()
        self.lock = lock

    def model(self, path: str | os.PathLike[str], *, upstream: str | None = None, **values: Any) -> Model:
        """Load a case-owned declaration. An extension names the registered template it imports."""
        path = self._template_path(path)
        relative = path.relative_to(self.root).as_posix()
        lock = Lock(self.root)
        copied = lock.data.get("copied_templates", {}).get(relative)
        if copied is not None:
            _workflow(copied["name"])  # Initialize the product registry before loading an isolated copy.
        source = path.read_bytes() if path.is_file() else None
        if source is None and copied is not None:
            _, source = lock.template_source(copied["source_sha256"])
        if source is None and upstream is not None:
            previous = lock.data["models"].get(relative, {}).get("template", {})
            if previous.get("upstream") == upstream:
                _, source = lock.template_source(previous["version"])
        if source is None:
            raise DeclarationError("The case template source is absent.", error_code="TEMPLATE_SOURCE_MISSING")
        if copied is not None and upstream is None:
            workflow = load(source, path)
            held = Template(relative, "copied", digest(source), copied["name"], copied["sha256"], relative)
        elif copied is None and upstream is not None:
            pin = self._called(lock, upstream)
            base = load(*_load_args(lock, pin["sha256"]))
            workflow = load(source, path, upstream=(pin["module"], base.template))
            held = Template(relative, "called", digest(source), upstream, pin["sha256"])
        else:
            raise DeclarationError("Select a copy or name the template extended.", error_code="TEMPLATE_INVALID")
        result = Model(self, workflow, values, template=held)
        lock.hold_template(source)
        if copied is not None:
            copied["source_sha256"] = held.sha256
        lock.save()
        self.lock = lock
        return result

    def _template_path(self, path: str | os.PathLike[str]) -> Path:
        target = (self.root / path).resolve()
        if self.root / "templates" not in target.parents or target.suffix != ".py":
            raise DeclarationError("The case template must be a Python file under templates.", error_code="TEMPLATE_INVALID")
        return target

    @staticmethod
    def _upstream_source(name: str) -> bytes | None:
        workflow = _workflow(name)
        if workflow is None:
            return None
        path = Path(workflow.template.__file__)
        return path.read_bytes() if path.is_file() else None

    def _called(self, lock: Lock, name: str, *, take: bool = False) -> dict[str, str]:
        registered = _workflow(name)
        pins = lock.data.setdefault("called_templates", {})
        if name in pins and not take:
            return pins[name]
        source = self._upstream_source(name)
        if registered is None or source is None:
            raise DeclarationError("The registered template source is absent.", error_code="TEMPLATE_SOURCE_MISSING")
        legacy = lock.data["models"].get(name, {}).get("template", {}).get("version")
        sha256 = digest(source)
        if not take and legacy is not None and legacy != sha256:
            lock.template_source(legacy)
            sha256 = legacy
        else:
            if take:
                load(source, Path(registered.template.__file__))
            lock.hold_template(source)
        pins[name] = {"sha256": sha256, "module": registered.template.__name__}
        return pins[name]

    @staticmethod
    def templates() -> list[str]:
        """Every registered template a model can be made over, by name."""
        from trid3nt.tools import TOOL_REGISTRY

        return sorted(name for name in TOOL_REGISTRY if _workflow(name) is not None)

    @contextmanager
    def bound(self) -> Iterator["Case"]:
        """Inside, the case directory is the object store, the journal and the work directory, and every fetch
        is read through the lock; outside, the deployment's own."""
        from trid3nt.tools.fetchers._router.router import bind_pin, reset_pin

        self.lock = Lock(self.root)
        store, pin = storage.bind_store(storage.FileStore(self.root)), bind_pin(self.lock.pin)
        try:
            yield self
        finally:
            reset_pin(pin)
            storage.reset_store(store)

    def path(self, uri: str) -> Path:
        """Where a case uri or a case-relative path stands in this directory."""
        relative = uri[len("s3://"):] if uri.startswith("s3://") else uri
        bucket, _, key = relative.partition("/")
        return storage.FileStore(self.root).path(bucket, key)

    def take(self, value: str) -> tuple[str, dict[str, Any]]:
        """Outside data - a local file, an http(s) URL or an s3 URI - copied into ``data/`` -> its case uri and
        its lock entry. A case uri or a path under this case's ``data/`` is taken as it is, checked."""
        parsed = urllib.parse.urlparse(value)
        held = self.root / value if not parsed.scheme else None
        if parsed.scheme == "s3" and parsed.netloc in storage.CASE_BUCKETS or (
                held is not None and held.is_file() and held.parent == self.root / "data"):
            target = self.path(value)
            if not target.is_file():
                raise StorageError(f"{value} names no file of this case.")
            digest = sha256_of(target)
            return f"s3://data/{target.name}", {"source": "case", "uri": f"data/{target.name}", "sha256": digest}
        part = self.root / "data" / ".taking.part"
        if parsed.scheme in ("http", "https"):
            with urllib.request.urlopen(value) as answer, part.open("wb") as handle:  # noqa: S310 - the person's URL
                shutil.copyfileobj(answer, handle)
            source, suffix = "url", Path(parsed.path).suffix
        elif parsed.scheme == "s3":
            storage.served_client().download_file(parsed.netloc, parsed.path.lstrip("/"), str(part))
            source, suffix = "s3", Path(parsed.path).suffix
        elif not parsed.scheme and Path(value).expanduser().is_file():
            if Path(value).suffix.lower() == ".shp":
                raise StorageError(
                    "a shapefile is several files beside each other and one of them alone reads nothing: hand "
                    "the layer as one file (GeoPackage, FlatGeobuf, GeoJSON).")
            shutil.copyfile(Path(value).expanduser(), part)
            source, suffix = "path", Path(value).suffix
        else:
            raise StorageError(f"{value} is neither a file, an http(s) URL nor an s3 URI, so nothing can be "
                               "copied into the case.")
        digest = sha256_of(part)
        name = f"{digest}{suffix.lower() if suffix[1:].isalnum() else ''}"
        part.replace(self.root / "data" / name)
        return f"s3://data/{name}", {"source": source, "request": value, "uri": f"data/{name}", "sha256": digest}

    def version(self, version: str) -> dict[str, Any]:
        """The lock snapshot of a model version that ran in this case."""
        import json

        path = self.root / "versions" / f"{version}.lock"
        if not path.is_file():
            raise StorageError(f"no model version {version} ran in this case.")
        return json.loads(path.read_text(encoding="utf-8"))

    def link(self, store: str) -> str:
        """Serve this case at ``store`` (``s3://bucket/prefix``): every file but its cache uploaded at its path
        relative to the case, and every pinned uri the served key -> ``store``. Each upload is read back checked."""
        bucket, prefix = _served(store)
        client = storage.served_client()
        lock = Lock(self.root)
        lock.data["served"] = store.rstrip("/")
        for entry in _pinned_rows(lock.data):
            entry["uri"] = f"{lock.data['served']}/{lock.relative(entry['uri'])}"
        lock.save()
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix()
            if path.is_file() and relative.split("/", 1)[0] not in _UNSERVED and not path.name.startswith("."):
                key = f"{prefix}/{relative}" if prefix else relative
                with path.open("rb") as handle:
                    client.put_object(Bucket=bucket, Key=key, Body=handle)
                read = hashlib.sha256(client.get_object(Bucket=bucket, Key=key)["Body"].read()).hexdigest()
                if read != sha256_of(path):
                    raise storage.DigestMismatchError(
                        f"{relative} reads back from {lock.data['served']} as other bytes than this case holds.")
        self.lock = lock
        return lock.data["served"]

    def pack(self) -> Path:
        """Every pinned object local under ``data/``, each read from the served store where it is not, checked
        against its sha256, and every uri the path relative to the case -> the case directory."""
        lock = Lock(self.root)
        for sha256 in _template_hashes(lock.data):
            lock.template_source(sha256)
        for entry in _pinned_rows(lock.data):
            relative = lock.relative(entry["uri"])
            for path, digest in {relative: entry["sha256"], **dict(entry.get("files") or {})}.items():
                target = self.root / path
                if not target.is_file():
                    if not lock.data.get("served"):
                        raise storage.ObjectMissingError(
                            f"{path} is pinned in this case's lock and held neither here nor in a served store.")
                    lock._served(path, digest)
                elif sha256_of(target) != digest:
                    raise storage.DigestMismatchError(
                        f"{path} in this case is not the file pinned there: its sha256 differs from {digest}.")
            entry["uri"] = relative
        lock.data.pop("served", None)
        lock.data.pop("remote_files", None)
        lock.save()
        self.lock = lock
        return self.root

    def __repr__(self) -> str:
        return f"Case({str(self.root)!r})"


def _load_args(lock: Lock, sha256: str) -> tuple[bytes, Path]:
    path, source = lock.template_source(sha256)
    return source, path


def _served(store: str) -> tuple[str, str]:
    if not store.startswith("s3://") or not store[len("s3://"):].split("/", 1)[0]:
        raise StorageError(f"a case is served at an s3://bucket/prefix; got {store!r}.")
    bucket, _, prefix = store[len("s3://"):].partition("/")
    return bucket, prefix.strip("/")


def _template_hashes(lock: dict[str, Any]) -> set[str]:
    hashes = {pin["sha256"] for section in ("called_templates", "copied_templates")
              for pin in lock.get(section, {}).values()}
    hashes.update(pin["source_sha256"] for pin in lock.get("copied_templates", {}).values())
    for row in lock["models"].values():
        template = row.get("template", {})
        if template.get("path"):
            hashes.update((template["version"], template["upstream_version"]))
    return hashes


def _pinned_rows(lock: dict[str, Any]) -> list[dict[str, Any]]:
    """Every lock row that names a pinned object: each fetch, and each model's inputs that hold a file."""
    rows = list(lock.get("fetched", {}).values())
    for model in lock.get("models", {}).values():
        rows += [row for row in (model.get("inputs") or {}).values() if row.get("uri") and row.get("sha256")]
    return rows


def _registered_fn(template: str) -> Any:
    from trid3nt.tools import TOOL_REGISTRY

    return TOOL_REGISTRY[template].fn


def case(where: str | os.PathLike[str], *, strict: bool = False) -> Case:
    """The case directory at ``where``, opened or created; a bare name is that directory under the current one.
    ``strict`` True refuses an unresolved coverage shortfall in its models; False warns and runs where the engine can."""
    return Case(where, strict=strict)
