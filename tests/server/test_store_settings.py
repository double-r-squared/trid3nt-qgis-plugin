"""The object store is reached by its own settings and nothing else.

Offline: a real boto3 client is built and its request stubbed, so what is proved
is which endpoint and key pair a store write carries."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server import storage

_MINIO = ("AWS_ENDPOINT_URL=http://127.0.0.1:9000\n"
          "AWS_ACCESS_KEY_ID=minio-key\n"
          "AWS_SECRET_ACCESS_KEY=minio-secret\n"
          "AWS_REGION=us-east-1\n")


@pytest.fixture()
def wrong_ambient(monkeypatch, tmp_path):
    """Ambient AWS credentials deliberately present and WRONG, and no endpoint in
    the environment - the shell a canary's runner starts from."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAWRONGWRONGWRONG0")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wrong-secret")
    monkeypatch.setenv("AWS_PROFILE", "default")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("TRID3NT_RUNS_BUCKET", raising=False)
    settings = tmp_path / ".env.local"
    monkeypatch.setattr(storage, "_SETTINGS_FILE", settings)
    storage.set_client(None)
    yield settings
    storage.set_client(None)


def test_a_chart_spec_persists_on_the_store_s_settings_not_ambient_credentials(
        wrong_ambient, monkeypatch):
    from botocore.stub import ANY, Stubber

    from trid3nt_server.workflows.runtime import run_products

    wrong_ambient.write_text(_MINIO, encoding="utf-8")
    built = storage.client()
    credentials = built._request_signer._credentials
    assert (credentials.access_key, credentials.secret_key) == (
        "minio-key", "minio-secret")
    assert built.meta.endpoint_url == "http://127.0.0.1:9000"
    stub = Stubber(built)
    stub.add_response("put_object", {}, {
        "Bucket": "trid3nt-runs", "Key": "RID/chart_spec.json", "Body": ANY,
        "ContentType": "application/json"})
    monkeypatch.setattr(storage, "client", lambda: built)
    with stub:
        uris = asyncio.run(run_products.persist_run_products(
            "RID", charts={"ice_cover_fraction": {"title": "cover"}}))
    assert uris == ["s3://trid3nt-runs/RID/chart_spec.json"]
    stub.assert_no_pending_responses()


def test_a_store_with_no_endpoint_stated_refuses_rather_than_reach_a_cloud(
        wrong_ambient):
    wrong_ambient.write_text("AWS_ACCESS_KEY_ID=minio-key\n", encoding="utf-8")
    with pytest.raises(storage.StorageError, match="AWS_ENDPOINT_URL"):
        storage.client()
