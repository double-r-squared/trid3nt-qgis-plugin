"""A case's lock: every byte its runs read, pinned by sha256 under data/, and what each model ran.

A fetch is read through the pin: a request the lock holds is answered from its pinned files, each checked
against its sha256, and only a file that is gone is fetched again by that request, refused where its bytes
differ. A new request is fetched once and pinned, so the first run reads exactly what every replay reads.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Mapping

from trid3nt.errors import DeclarationError
from trid3nt.store import objects as storage
from trid3nt.store.objects import CASE_BUCKETS, DigestMismatchError, sha256_of

logger = logging.getLogger("trid3nt.sdk.lock")

__all__ = ["LOCK_FILENAME", "Lock", "canonical", "request_key"]

LOCK_FILENAME = "trid3nt.lock"

#: A fetched value's own fields that name a case object; any other string is the value as the source said it.
_CASE_URI = tuple(f"s3://{bucket}/" for bucket in CASE_BUCKETS)


def canonical(value: Any) -> bytes:
    """The one byte form of a JSON value the lock hashes and writes: keys sorted, no free whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def request_key(source: str, request: Mapping[str, Any]) -> str:
    """The key a fetch is pinned under: the sha256 of its source and what it was asked."""
    return hashlib.sha256(canonical({"source": source, "request": request})).hexdigest()


class Lock:
    """``trid3nt.lock`` of one case directory: ``fetched`` (each pinned fetch by its request key), ``models``
    (each template's last fill and run) and ``served`` (the store the case is linked to, where it is)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / LOCK_FILENAME
        self.data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        self.data.setdefault("fetched", {})
        self.data.setdefault("models", {})
        #: The keys this task read or pinned, in order: which fetch answered which input.
        self.read: list[str] = []

    def save(self) -> None:
        """Write the lock where its bytes changed; a lock read back and written again is the same file."""
        text = json.dumps(self.data, sort_keys=True, indent=2) + "\n"
        if not self.path.is_file() or self.path.read_text(encoding="utf-8") != text:
            self.path.write_text(text, encoding="utf-8")

    def hold_template(self, source: bytes) -> str:
        """Keep the exact declaration bytes by hash and verify an existing object of that hash."""
        digest = hashlib.sha256(source).hexdigest()
        key = f"{digest}.py"
        store = storage.FileStore(self.root)
        path = store.path("data", key)
        if path.is_file():
            _check(path, digest)
        else:
            store.put_object(Bucket="data", Key=key, Body=source)
        return digest

    def template_source(self, digest: str) -> tuple[Path, bytes]:
        """Read the held Python source after checking its content hash."""
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DeclarationError("The template pin has an invalid hash.", error_code="TEMPLATE_INVALID")
        relative = f"data/{digest}.py"
        path = storage.FileStore(self.root).path("data", f"{digest}.py")
        if not path.is_file() and self.data.get("served"):
            self._served(relative, digest)
        if not path.is_file():
            raise DeclarationError("The case lacks the pinned template source.", error_code="TEMPLATE_SOURCE_MISSING")
        source = path.read_bytes()
        if hashlib.sha256(source).hexdigest() != digest:
            raise DigestMismatchError(f"The pinned template source differs from {digest}.")
        return path, source

    def local(self, uri: str) -> Path:
        """Where a pinned uri's bytes stand in this directory: a case-relative path, or a served one's same path."""
        return self.root / self.relative(uri)

    def relative(self, uri: str) -> str:
        served = self.data.get("served")
        if served and uri.startswith(f"{served}/"):
            return uri[len(served) + 1:]
        return uri

    def pin(self, source: str, request: dict[str, Any], fetch: Callable[[], Any]) -> Any:
        """A fetch read through the lock -> its value, every object it names under ``data/``."""
        key = request_key(source, request)
        self.read.append(key)
        entry = self.data["fetched"].get(key)
        if entry is not None:
            return self._replayed(entry, fetch)
        value = fetch()
        try:
            entry = self._pinned(source, request, value)
        except _Unpinnable as unpinnable:
            logger.warning("the %s fetch answered a %s the lock cannot hold, so a replay fetches it again",
                           source, unpinnable)
            return value
        self.data["fetched"][key] = entry
        self.save()
        return self._thawed(entry)

    def _pinned(self, source: str, request: Mapping[str, Any], value: Any) -> dict[str, Any]:
        return self._store_value(source, request, value, self._took)

    def hold_served(self, source: str, request: Mapping[str, Any], value: Any) -> None:
        """Keep a fetch's envelope with content-addressed references to its original objects."""
        self.data["fetched"][request_key(source, request)] = self._store_value(
            source, request, value, self._pointed, all_s3=True)

    def _store_value(self, source: str, request: Mapping[str, Any], value: Any,
                     took: Callable[[str, dict[str, str]], str], *, all_s3: bool = False) -> dict[str, Any]:
        files: dict[str, str] = {}
        frozen = _freeze(value, lambda uri: took(uri, files), all_s3=all_s3)
        body = canonical(frozen)
        digest = hashlib.sha256(body).hexdigest()
        target = self.root / "data" / f"{digest}.json"
        if not target.is_file():
            target.write_bytes(body)
        return {"source": source, "request": json.loads(canonical(request)),
                "uri": f"data/{digest}.json", "sha256": digest, "files": dict(sorted(files.items()))}

    def _took(self, uri: str, files: dict[str, str]) -> str:
        """One object a fetch answered with, copied into ``data/`` -> its case uri."""
        bucket, _, key = uri[len("s3://"):].partition("/")
        if bucket == "data":
            files[f"data/{key}"] = Path(key).stem
            return uri
        source = storage.bound_store().path(bucket, key)
        digest = sha256_of(source)
        name = f"{digest}{_suffix(key)}"
        target = self.root / "data" / name
        if not target.is_file():
            part = target.with_name(f".{name}.part")
            shutil.copyfile(source, part)
            part.replace(target)
        files[f"data/{name}"] = digest
        return f"s3://data/{name}"

    def reference(self, uri: str) -> tuple[str, str]:
        """Return the case key and SHA-256 of an existing served object."""
        for path, remote in self.data.get("remote_files", {}).items():
            if remote == uri:
                return path, Path(path).stem
        digest = served_digest(uri)
        path = f"data/{digest}{_suffix(uri)}"
        self.data.setdefault("remote_files", {})[path] = uri
        return path, digest

    def _pointed(self, uri: str, files: dict[str, str]) -> str:
        path, digest = self.reference(uri)
        files[path] = digest
        return f"s3://{path}"

    def _replayed(self, entry: Mapping[str, Any], fetch: Callable[[], Any]) -> Any:
        pinned = {self.relative(entry["uri"]): entry["sha256"], **dict(entry.get("files") or {})}
        gone = [path for path in pinned if not (self.root / path).is_file()]
        for path in gone:
            if self.data.get("served"):
                self._served(path, pinned[path])
        if any(not (self.root / path).is_file() for path in gone):
            again = self._pinned(entry["source"], entry["request"], fetch())
            if (again["sha256"], again["files"]) != (entry["sha256"], dict(entry.get("files") or {})):
                raise DigestMismatchError(
                    f"the {entry['source']} fetch pinned in this case's lock answers different bytes now: "
                    f"pinned {entry['sha256']}, read {again['sha256']}. Delete its entry from the lock to "
                    "take what the source answers today.")
        for path, digest in pinned.items():
            _check(self.root / path, digest)
        return self._thawed(entry)

    def _served(self, path: str, digest: str) -> None:
        """Bring one pinned file from the served store into this directory, checked against its sha256."""
        uri = self.data.get("remote_files", {}).get(path)
        if uri is None:
            prefix = self.data["served"].rstrip("/")
            uri = f"{prefix}/{path}"
        _, bucket, key = storage.split_object_uri(uri)
        section, _, name = path.partition("/")
        target = storage.FileStore(self.root).path(section, name)
        part = target.with_name(f".{target.name}.part")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            storage.served_client().download_file(bucket, key, str(part))
            _check(part, digest)
            part.replace(target)
        finally:
            part.unlink(missing_ok=True)

    def _thawed(self, entry: Mapping[str, Any]) -> Any:
        path = self.local(entry["uri"])
        _check(path, entry["sha256"])
        return _thaw(json.loads(path.read_bytes()))


class _Unpinnable(TypeError):
    """A fetched value of a type the lock has no JSON form for."""


def _check(path: Path, digest: str) -> None:
    held = sha256_of(path)
    if held != digest:
        raise DigestMismatchError(
            f"{path.name} in this case's data is not the file pinned there: its sha256 is {held}, "
            f"pinned {digest}. Restore the file, or delete its entry from the lock to fetch it again.")


def served_digest(uri: str) -> str:
    """Hash the served bytes without making a second retained copy."""
    _, bucket, key = storage.split_object_uri(uri)
    digest = hashlib.sha256()
    with storage.client().get_object(Bucket=bucket, Key=key)["Body"] as body:
        for chunk in iter(lambda: body.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _suffix(key: str) -> str:
    suffix = Path(key).suffix.lower()
    return suffix if suffix[1:].isalnum() else ""


def _freeze(value: Any, took: Callable[[str], str], *, all_s3: bool = False) -> Any:
    if isinstance(value, str):
        return took(value) if value.startswith("s3://" if all_s3 else _CASE_URI) else value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        kind = type(value)
        return {"$model": f"{kind.__module__}:{kind.__qualname__}", "fields": _freeze(dump(mode="json"), took, all_s3=all_s3)}
    if isinstance(value, Mapping):
        return {str(key): _freeze(item, took, all_s3=all_s3) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_freeze(item, took, all_s3=all_s3) for item in value]
    raise _Unpinnable(type(value).__name__)


def _thaw(frozen: Any) -> Any:
    if isinstance(frozen, list):
        return [_thaw(item) for item in frozen]
    if not isinstance(frozen, dict):
        return frozen
    if set(frozen) == {"$model", "fields"}:
        module, _, name = str(frozen["$model"]).partition(":")
        kind = getattr(importlib.import_module(module), name)
        return kind.model_validate(_thaw(frozen["fields"]))
    return {key: _thaw(item) for key, item in frozen.items()}
