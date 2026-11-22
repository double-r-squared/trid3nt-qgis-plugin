"""The object store every run reaches through: one client, the bucket names, the work directory.

The client is a process-wide seam bound once by a deployment or test, else built lazily from the
store's own settings; a case directory bound for one task (``bind_store``) answers every call instead,
its buckets the case's own directories. A read of a past run falls back to a default runs bucket; an
UPLOAD refuses an unset environment rather than filling a bucket nobody provisioned.
"""

from __future__ import annotations

import contextvars
import hashlib
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

__all__ = ["CASE_BUCKETS", "DigestMismatchError", "FileStore", "ObjectMissingError", "StorageError",
           "bind_store", "bound_store", "cache_bucket", "case_relative", "client", "delete_prefix",
           "local_runs_bucket", "object_size", "reset_store", "runs_bucket", "served_client",
           "set_client", "set_runs_bucket", "sha256_of", "split_object_uri", "work_dir"]


class StorageError(RuntimeError):
    """The object store is unreachable, unconfigured, or handed a bad URI.

    The ``error_code`` attribute carries the typed code a downstream wrapper
    re-emits verbatim rather than re-deriving one."""

    error_code: str = "STORAGE_UNAVAILABLE"


class ObjectMissingError(StorageError):
    """No object stands at the URI."""

    error_code = "OBJECT_NOT_FOUND"


class DigestMismatchError(StorageError):
    """The bytes read are not the bytes pinned: their sha256 differs from the one recorded for them."""

    error_code = "DIGEST_MISMATCH"


#: A case directory's buckets, each the directory of that name under the case: ``data`` the pinned bytes a run
#: read, ``runs`` what each run wrote, ``cache`` the fetch cache, the staged decks and the meshes a run rebuilds.
CASE_BUCKETS = ("data", "runs", "cache")


class _Pages:
    def __init__(self, store: "FileStore") -> None:
        self._store = store

    def paginate(self, **listing: Any) -> Iterator[dict[str, Any]]:
        yield self._store.list_objects_v2(**listing)


class FileStore:
    """The object store over one directory: ``s3://<bucket>/<key>`` is ``<root>/<bucket>/<key>``, for the
    case's own buckets only. The calls are the subset of boto3's S3 client every run makes; a missing object
    raises botocore's ``ClientError`` with S3's own codes, so a caller handles it as it handles S3's."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def __reduce__(self) -> tuple[Any, tuple[str]]:
        return FileStore, (str(self.root),)

    def path(self, bucket: str, key: str) -> Path:
        if bucket not in CASE_BUCKETS:
            raise StorageError(f"a case directory holds the buckets {', '.join(CASE_BUCKETS)}; "
                               f"got {bucket!r}.")
        target = (self.root / bucket / key).resolve()
        if self.root / bucket not in target.parents:
            raise StorageError(f"the key {key!r} climbs out of the case's {bucket} directory.")
        return target

    def _missing(self, operation: str, bucket: str, key: str) -> Exception:
        from botocore.exceptions import ClientError

        return ClientError({"Error": {"Code": "NoSuchKey" if operation == "GetObject" else "404",
                                      "Message": f"no such object: s3://{bucket}/{key}"}}, operation)

    def put_object(self, *, Bucket: str, Key: str, Body: Any = b"", **_meta: Any) -> dict[str, Any]:
        target = self.path(Bucket, Key)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(f".{target.name}.part")
        with part.open("wb") as handle:
            if isinstance(Body, (bytes, bytearray, memoryview)):
                handle.write(Body)
            elif isinstance(Body, str):
                handle.write(Body.encode("utf-8"))
            else:
                shutil.copyfileobj(Body, handle)
        part.replace(target)
        return {"ETag": f'"{sha256_of(target)}"'}

    def _read(self, operation: str, bucket: str, key: str) -> Path:
        """The file an object is; one under ``data/`` is named by its sha256, which every read checks."""
        target = self.path(bucket, key)
        if not target.is_file():
            raise self._missing(operation, bucket, key)
        if bucket == "data" and _SHA256.match(target.stem) and sha256_of(target) != target.stem:
            raise DigestMismatchError(
                f"{target.name} in the case's data is not the file pinned under that name: its sha256 differs.")
        return target

    def get_object(self, *, Bucket: str, Key: str, **_range: Any) -> dict[str, Any]:
        target = self._read("GetObject", Bucket, Key)
        return {"Body": target.open("rb"), "ContentLength": target.stat().st_size}

    def head_object(self, *, Bucket: str, Key: str, **_meta: Any) -> dict[str, Any]:
        target = self.path(Bucket, Key)
        if not target.is_file():
            raise self._missing("HeadObject", Bucket, Key)
        stat = target.stat()
        return {"ContentLength": stat.st_size,
                "LastModified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)}

    def download_file(self, Bucket: str, Key: str, Filename: str, **_extra: Any) -> None:
        target = self._read("HeadObject", Bucket, Key)
        shutil.copyfile(target, Filename)

    def upload_file(self, Filename: str, Bucket: str, Key: str, **_extra: Any) -> None:
        with open(Filename, "rb") as handle:
            self.put_object(Bucket=Bucket, Key=Key, Body=handle)

    def list_objects_v2(self, *, Bucket: str, Prefix: str = "", **_paging: Any) -> dict[str, Any]:
        base = self.root / Bucket
        found = []
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                key = path.relative_to(base).as_posix()
                if path.is_file() and key.startswith(Prefix) and not path.name.startswith("."):
                    stat = path.stat()
                    found.append({"Key": key, "Size": stat.st_size,
                                  "LastModified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)})
        return {"Contents": found, "KeyCount": len(found), "IsTruncated": False}

    def get_paginator(self, operation: str) -> _Pages:
        if operation != "list_objects_v2":
            raise StorageError(f"a case directory pages list_objects_v2 only; got {operation!r}.")
        return _Pages(self)

    def delete_object(self, *, Bucket: str, Key: str, **_extra: Any) -> dict[str, Any]:
        self.path(Bucket, Key).unlink(missing_ok=True)
        return {}

    def delete_objects(self, *, Bucket: str, Delete: Mapping[str, Any], **_extra: Any) -> dict[str, Any]:
        for row in Delete.get("Objects") or ():
            self.delete_object(Bucket=Bucket, Key=row["Key"])
        return {}


#: The case directory a task's runs read and write, bound by the SDK around one fill or run; it crosses into a
#: mesh build's child with the other bound variables, and a thread the run starts carries it in its context.
_STORE: contextvars.ContextVar[FileStore | None] = contextvars.ContextVar("trid3nt_case_store", default=None)


def bind_store(store: FileStore | None) -> contextvars.Token:
    """Bind ``store`` as the object store of this task and every one it starts -> the token that unbinds it."""
    return _STORE.set(store)


def reset_store(token: contextvars.Token) -> None:
    _STORE.reset(token)


def bound_store() -> FileStore | None:
    """The case directory bound for this task, or ``None`` where the deployment's store answers."""
    return _STORE.get()


def sha256_of(path: str | Path) -> str:
    """The sha256 of a file's bytes, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_CASE_URI = re.compile(r"^s3://(" + "|".join(CASE_BUCKETS) + r")/")


def case_relative(value: Any) -> Any:
    """``value`` with every ``s3://`` URI of the bound case directory written as the path relative to it;
    unchanged where no case directory is bound."""
    if bound_store() is None:
        return value
    if isinstance(value, str):
        return _CASE_URI.sub(r"\1/", value) if "s3://" in value else value
    if isinstance(value, Mapping):
        return {key: case_relative(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [case_relative(item) for item in value]
    return value


_CLIENT: Any | None = None
_RUNS_BUCKET: str | None = None


_SETTINGS_FILE = Path(__file__).resolve().parents[2] / ".env.local"

#: Passed to boto3 explicitly: a client left to find its own credentials takes whatever the process inherited.
_SETTINGS = ("AWS_ENDPOINT_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
             "AWS_REGION")


def set_client(bound: Any) -> None:
    """Bind the boto3 S3 client used for ALL object-store I/O.

    ``None`` restores the lazy default, built from the store's own settings."""
    global _CLIENT
    _CLIENT = bound


def set_runs_bucket(name: str | None) -> None:
    """Override the runs-bucket name. ``None`` restores the env-based default."""
    global _RUNS_BUCKET
    _RUNS_BUCKET = name


def _settings() -> dict[str, str]:
    """The store's endpoint, key pair and region: the settings file's, then the
    process's for a setting the file does not state."""
    stated: dict[str, str] = {}
    if _SETTINGS_FILE.is_file():
        for line in _SETTINGS_FILE.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() in _SETTINGS:
                stated[name.strip()] = value.strip().strip("'\"")
    return {name: stated.get(name) or os.environ.get(name, "")
            for name in _SETTINGS}


def client() -> Any:
    """The case directory bound for this task, else the bound S3 client, else one built from the store's settings.

    boto3, never s3fs, which falls back to anonymous credentials. A store with
    no endpoint stated refuses rather than reaching for a cloud nobody named."""
    store = bound_store()
    if store is not None:
        return store
    if _CLIENT is not None:
        return _CLIENT
    return served_client()


def served_client() -> Any:
    """The S3 client built from the store's own settings, whatever case directory is bound: the store a case
    is linked to and packed from."""
    try:
        import boto3  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise StorageError(
            f"boto3 not importable: {exc}; the object store requires boto3 for "
            "staging and upload."
        ) from exc
    stated = _settings()
    missing = [name for name in _SETTINGS[:3] if not stated[name]]
    if missing:
        raise StorageError(
            f"the object store's {', '.join(missing)} is stated neither in "
            f"{_SETTINGS_FILE} nor in the environment, so there is no store to "
            "reach.")
    return boto3.client("s3", endpoint_url=stated["AWS_ENDPOINT_URL"],
                        aws_access_key_id=stated["AWS_ACCESS_KEY_ID"],
                        aws_secret_access_key=stated["AWS_SECRET_ACCESS_KEY"],
                        region_name=stated["AWS_REGION"] or "us-east-1")


def runs_bucket() -> str:
    """The bound case's ``runs``, the overridden runs bucket, or ``TRID3NT_RUNS_BUCKET``, or the default name."""
    if bound_store() is not None:
        return "runs"
    if _RUNS_BUCKET is not None:
        return _RUNS_BUCKET
    return os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")


def local_runs_bucket() -> str:
    """The runs bucket a solve uploads into, with NO default at all.

    An unset ``TRID3NT_RUNS_BUCKET`` fails loudly: a default would let every run
    upload into a bucket nobody provisioned and call it a success."""
    if bound_store() is not None:
        return "runs"
    if _RUNS_BUCKET is not None:
        return _RUNS_BUCKET
    bucket = (os.environ.get("TRID3NT_RUNS_BUCKET") or "").strip()
    if not bucket:
        raise StorageError(
            "TRID3NT_RUNS_BUCKET must be set when TRID3NT_SOLVER_BACKEND="
            "local-docker; there is no default runs bucket.")
    return bucket


def cache_bucket() -> str:
    """The bound case's ``cache``, else ``TRID3NT_CACHE_BUCKET``, else ``""``: each caller states what an unset one means."""
    if bound_store() is not None:
        return "cache"
    return (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()


def work_dir(default: str) -> Path:
    """Where a run works on this machine: the bound case's ``cache/work``, else ``TRID3NT_RUNS_DIR``, else ``default``."""
    store = bound_store()
    if store is not None:
        return store.root / "cache" / "work"
    return Path(os.environ.get("TRID3NT_RUNS_DIR") or default)


def split_object_uri(uri: str) -> tuple[str, str, str]:
    """Split ``s3://bucket/key`` into ``(scheme, bucket, key)``.

    Only ``s3://`` is supported; anything else raises ``StorageError``."""
    prefix = "s3://"
    if uri.startswith(prefix):
        bucket, _, key = uri[len(prefix):].partition("/")
        if not bucket or not key:
            raise StorageError(f"malformed s3:// URI: {uri!r}")
        return "s3", bucket, key
    raise StorageError(f"unsupported object URI scheme: {uri!r} (expected s3://)")


def object_size(uri: str) -> int:
    """The byte size of the ``s3://`` object at ``uri``, without reading it.

    A missing object refuses as :class:`ObjectMissingError`; any other failure
    to inspect it is a plain :class:`StorageError`."""
    from botocore.exceptions import ClientError

    _scheme, bucket, key = split_object_uri(uri)
    try:
        head = client().head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code", "") in (
                "404", "NoSuchKey", "NotFound"):
            raise ObjectMissingError(f"no such object: {uri}") from exc
        raise StorageError(f"could not inspect {uri}: {exc}") from exc
    return int(head.get("ContentLength") or 0)


def delete_prefix(bucket: str, prefix: str) -> None:
    """Delete every object under ``prefix`` in ``bucket``; an empty prefix is a no-op."""
    s3 = client()
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents") or ()]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
