"""Check the input objects named by a remote run and track the objects it writes."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

from trid3nt.errors import DeclarationError
from trid3nt.store import objects


def _digest_error() -> DeclarationError:
    return DeclarationError("A served input differs from its pin.", error_code="DIGEST_MISMATCH")


class VerifiedStore:
    def __init__(self, client: Any, pins: dict[str, str]):
        self.client = client
        self.pins = pins
        self.written: set[str] = set()

    def _expected(self, bucket, key):
        uri = f"s3://{bucket}/{key}"
        if uri in self.written:
            return None
        try:
            return self.pins[uri]
        except KeyError:
            raise DeclarationError("The run requested an unpinned object.", error_code="REMOTE_INPUT_UNPINNED") from None

    def get_object(self, *, Bucket, Key, **kwargs):
        expected = self._expected(Bucket, Key)
        result = self.client.get_object(Bucket=Bucket, Key=Key, **kwargs)
        if expected is not None:
            body = result["Body"]
            data = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
            digest = hashlib.sha256()
            try:
                with body:
                    for chunk in iter(lambda: body.read(1024 * 1024), b""):
                        digest.update(chunk)
                        data.write(chunk)
                if digest.hexdigest() != expected:
                    raise _digest_error()
                data.seek(0)
                result["Body"] = data
            except BaseException:
                data.close()
                raise
        return result

    def download_file(self, Bucket, Key, Filename, **kwargs):
        expected = self._expected(Bucket, Key)
        self.client.download_file(Bucket, Key, Filename, **kwargs)
        if expected is not None and objects.sha256_of(Path(Filename)) != expected:
            Path(Filename).unlink(missing_ok=True)
            raise _digest_error()

    def head_object(self, *, Bucket, Key, **kwargs):
        self._expected(Bucket, Key)
        return self.client.head_object(Bucket=Bucket, Key=Key, **kwargs)

    def put_object(self, *, Bucket, Key, **kwargs):
        result = self.client.put_object(Bucket=Bucket, Key=Key, **kwargs)
        self.written.add(f"s3://{Bucket}/{Key}")
        return result

    def upload_file(self, Filename, Bucket, Key, **kwargs):
        result = self.client.upload_file(Filename, Bucket, Key, **kwargs)
        self.written.add(f"s3://{Bucket}/{Key}")
        return result

    def __getattr__(self, name):
        return getattr(self.client, name)


def bind_verified_store(pins: dict[str, str]) -> None:
    """Bind this child process to its pins and block replay fetches."""
    from trid3nt.tools.fetchers._router.router import bind_pin

    store = VerifiedStore(objects.served_client(), pins)
    objects.set_client(store)
    objects.served_client = lambda: store

    def reject(*args):
        raise DeclarationError("The run requested an unpinned source.", error_code="REMOTE_INPUT_UNPINNED")

    bind_pin(reject)
