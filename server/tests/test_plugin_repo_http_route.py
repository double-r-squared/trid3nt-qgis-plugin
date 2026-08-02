"""Offline tests for the QGIS custom plugin repository (plugin_repo.py) and
its three HTTP routes on the tool-catalog listener:

  - ``GET /plugins/plugins.xml``  -- the QGIS plugin-repository index XML.
  - ``GET /plugins/trid3nt.zip``  -- the installable zip.
  - ``GET /api/version``          -- daemon git sha + active model provider.

Covers: the zip's structure (top-level ``trid3nt/`` dir, LICENSE inside,
caches/hidden files excluded), the stamped ``metadata.txt`` version matching
``plugins.xml``'s ``<version>``, HEAD-keyed build caching (no rebuild until
HEAD moves), the git-less degrade path, and the HTTP dispatcher's routing +
error mapping (404/500/503) with the download_url derived from the request's
own Host header.

Everything here runs against a throwaway fixture git repo under ``tmp_path``
-- no network, no real daemon checkout touched.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from trid3nt_server import plugin_repo, tool_catalog_http

# ---------------------------------------------------------------------------
# Fixture repo helpers
# ---------------------------------------------------------------------------

_METADATA_TXT = """[general]
name=TRID3NT
qgisMinimumVersion=3.28
qgisMaximumVersion=4.99
description=TRID3NT agent chat dock -- fixture description.
about=Fixture about text.
version=1.2.3
author=Fixture Author
email=fixture@example.com
experimental=True
deprecated=False
icon=icon.svg
tags=ai,chat,agent
homepage=https://example.invalid/home
repository=https://example.invalid/repo
tracker=https://example.invalid/issues
"""


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _make_fake_repo(tmp_path: Path, *, init_git: bool = True) -> Path:
    """Build ``<tmp_path>/repo`` with a minimal ``qgis-plugin/trid3nt`` tree
    (metadata.txt, __init__.py, a nested module, a __pycache__ dir + a hidden
    file to prove exclusion) and a top-level LICENSE, optionally as a real
    git checkout with one commit.
    """
    repo_root = tmp_path / "repo"
    plugin_dir = repo_root / "qgis-plugin" / "trid3nt"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "metadata.txt").write_text(_METADATA_TXT, encoding="utf-8")
    (plugin_dir / "__init__.py").write_text("# init\n", encoding="utf-8")
    (plugin_dir / "plugin.py").write_text("# plugin code\n", encoding="utf-8")
    sub = plugin_dir / "net"
    sub.mkdir()
    (sub / "__init__.py").write_text("", encoding="utf-8")
    # Exclusion bait: __pycache__, a .pyc, and a dotfile.
    pycache = plugin_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "plugin.cpython-312.pyc").write_bytes(b"\x00\x01")
    (plugin_dir / ".hidden").write_text("should be excluded\n", encoding="utf-8")

    (repo_root / "qgis-plugin" / "LICENSE").write_text("MIT-ish fixture\n", encoding="utf-8")

    if init_git:
        _git(repo_root, "init", "-q")
        _git(repo_root, "config", "user.email", "fixture@example.com")
        _git(repo_root, "config", "user.name", "Fixture")
        _git(repo_root, "add", "-A")
        _git(repo_root, "commit", "-q", "-m", "initial fixture commit")

    return repo_root


@pytest.fixture()
def fake_repo(tmp_path, monkeypatch):
    repo_root = _make_fake_repo(tmp_path)
    monkeypatch.setenv("TRID3NT_REPO_ROOT", str(repo_root))
    monkeypatch.delenv("TRID3NT_PLUGIN_REPO_CACHE_DIR", raising=False)
    return repo_root


# ---------------------------------------------------------------------------
# plugin_repo.py -- zip build + structure
# ---------------------------------------------------------------------------


def test_ensure_plugin_zip_correct_structure(fake_repo):
    info = plugin_repo.ensure_plugin_zip()
    zip_path = info["zip_path"]
    assert zip_path.is_file()

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        # Top-level trid3nt/ dir -- correct QGIS plugin zip structure.
        assert all(n.startswith("trid3nt/") for n in names), names
        assert "trid3nt/__init__.py" in names
        assert "trid3nt/net/__init__.py" in names
        assert "trid3nt/metadata.txt" in names
        # LICENSE copied INSIDE the plugin folder.
        assert "trid3nt/LICENSE" in names
        # Caches / hidden files excluded.
        assert not any("__pycache__" in n for n in names)
        assert not any(n.endswith(".pyc") for n in names)
        assert not any("/.hidden" in n or n.endswith("/.hidden") for n in names)

        # Stamped version inside the zip's metadata.txt matches the returned
        # version exactly -- the agreement Plugin Manager relies on.
        meta_text = zf.read("trid3nt/metadata.txt").decode("utf-8")
    stamped_line = next(l for l in meta_text.splitlines() if l.startswith("version="))
    assert stamped_line == f"version={info['version']}"
    # Base prefix preserved (source metadata.txt's own version=1.2.3).
    assert info["version"].startswith("1.2.3+")


def test_ensure_plugin_zip_does_not_touch_real_metadata(fake_repo):
    real_metadata = fake_repo / "qgis-plugin" / "trid3nt" / "metadata.txt"
    before = real_metadata.read_text(encoding="utf-8")
    plugin_repo.ensure_plugin_zip()
    after = real_metadata.read_text(encoding="utf-8")
    assert before == after
    assert "version=1.2.3\n" in after  # unstamped, never touched


def test_ensure_plugin_zip_missing_source_tree_raises(tmp_path, monkeypatch):
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    monkeypatch.setenv("TRID3NT_REPO_ROOT", str(empty_root))
    monkeypatch.delenv("TRID3NT_PLUGIN_REPO_CACHE_DIR", raising=False)
    with pytest.raises(plugin_repo.PluginRepoBuildError):
        plugin_repo.ensure_plugin_zip()


# ---------------------------------------------------------------------------
# Staleness: cache keyed on git HEAD
# ---------------------------------------------------------------------------


def test_ensure_plugin_zip_caches_until_head_changes(fake_repo, monkeypatch):
    calls: list[int] = []
    real_build_zip = plugin_repo._build_zip

    def _spy_build_zip(*args, **kwargs):
        calls.append(1)
        return real_build_zip(*args, **kwargs)

    monkeypatch.setattr(plugin_repo, "_build_zip", _spy_build_zip)

    info1 = plugin_repo.ensure_plugin_zip()
    assert len(calls) == 1

    # Same HEAD -- second call is a cache hit, no rebuild.
    info2 = plugin_repo.ensure_plugin_zip()
    assert len(calls) == 1
    assert info2 == info1

    # Move HEAD: edit a source file + commit.
    (fake_repo / "qgis-plugin" / "trid3nt" / "plugin.py").write_text(
        "# changed\n", encoding="utf-8"
    )
    _git(fake_repo, "add", "-A")
    _git(fake_repo, "commit", "-q", "-m", "second commit")

    info3 = plugin_repo.ensure_plugin_zip()
    assert len(calls) == 2  # rebuilt
    assert info3["head_sha"] != info1["head_sha"]
    assert info3["version"] != info1["version"]


# ---------------------------------------------------------------------------
# git-less degrade path
# ---------------------------------------------------------------------------


def test_git_head_sha_and_describe_degrade_without_git(tmp_path, monkeypatch):
    repo_root = _make_fake_repo(tmp_path, init_git=False)
    assert plugin_repo._git_head_sha(repo_root) == "unknown"
    assert plugin_repo._git_describe(repo_root) == "unknown"


def test_ensure_plugin_zip_still_builds_without_git(tmp_path, monkeypatch):
    repo_root = _make_fake_repo(tmp_path, init_git=False)
    monkeypatch.setenv("TRID3NT_REPO_ROOT", str(repo_root))
    monkeypatch.delenv("TRID3NT_PLUGIN_REPO_CACHE_DIR", raising=False)

    info = plugin_repo.ensure_plugin_zip()
    assert info["head_sha"] == "unknown"
    assert info["version"] == "1.2.3+unknown"
    assert info["zip_path"].is_file()

    # No honest cache key -- every call rebuilds.
    calls: list[int] = []
    real_build_zip = plugin_repo._build_zip

    def _spy_build_zip(*args, **kwargs):
        calls.append(1)
        return real_build_zip(*args, **kwargs)

    monkeypatch.setattr(plugin_repo, "_build_zip", _spy_build_zip)
    plugin_repo.ensure_plugin_zip()
    plugin_repo.ensure_plugin_zip()
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# _stamp_metadata_version (pure)
# ---------------------------------------------------------------------------


def test_stamp_metadata_version_rewrites_only_version_line(tmp_path):
    meta = tmp_path / "metadata.txt"
    meta.write_text("[general]\nname=X\nversion=0.0.1\nauthor=Y\n", encoding="utf-8")
    plugin_repo._stamp_metadata_version(meta, "9.9.9+deadbee")
    out = meta.read_text(encoding="utf-8")
    assert "version=9.9.9+deadbee" in out
    assert "name=X" in out
    assert "author=Y" in out
    assert out.count("version=") == 1


def test_stamp_metadata_version_missing_line_raises(tmp_path):
    meta = tmp_path / "metadata.txt"
    meta.write_text("[general]\nname=X\n", encoding="utf-8")
    with pytest.raises(plugin_repo.PluginRepoBuildError):
        plugin_repo._stamp_metadata_version(meta, "1.0.0+abc")


# ---------------------------------------------------------------------------
# plugins.xml shape
# ---------------------------------------------------------------------------


def test_build_plugins_repo_xml_shape(fake_repo):
    import xml.etree.ElementTree as ET

    body = plugin_repo.build_plugins_repo_xml("http://myhost:8766/plugins/trid3nt.zip")
    root = ET.fromstring(body)
    assert root.tag == "plugins"
    plugin_el = root.find("pyqgis_plugin")
    assert plugin_el is not None
    assert plugin_el.get("name") == "TRID3NT"

    expected_version = plugin_repo.ensure_plugin_zip()["version"]
    assert plugin_el.get("version") == expected_version
    assert plugin_el.find("version").text == expected_version
    assert plugin_el.find("qgis_minimum_version").text == "3.28"
    assert plugin_el.find("qgis_maximum_version").text == "4.99"
    assert plugin_el.find("file_name").text == "trid3nt.zip"
    assert plugin_el.find("download_url").text == "http://myhost:8766/plugins/trid3nt.zip"
    assert "fixture description" in plugin_el.find("description").text
    assert plugin_el.find("experimental").text == "True"
    assert plugin_el.find("deprecated").text == "False"


def test_build_plugins_repo_xml_missing_tree_raises(tmp_path, monkeypatch):
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    monkeypatch.setenv("TRID3NT_REPO_ROOT", str(empty_root))
    monkeypatch.delenv("TRID3NT_PLUGIN_REPO_CACHE_DIR", raising=False)
    with pytest.raises(plugin_repo.PluginRepoBuildError):
        plugin_repo.build_plugins_repo_xml("http://host:8766/plugins/trid3nt.zip")


def test_version_scheme_no_tags_falls_back_to_short_sha(fake_repo):
    describe = plugin_repo._git_describe(fake_repo)
    short_sha = subprocess.run(
        ["git", "-C", str(fake_repo), "rev-parse", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert describe == short_sha  # no tags reachable -> git's own short-sha fallback


# ---------------------------------------------------------------------------
# build_version_payload
# ---------------------------------------------------------------------------


def test_build_version_payload_shape(fake_repo, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "bedrock")
    payload = plugin_repo.build_version_payload()
    assert set(payload.keys()) == {"git_sha", "provider"}
    assert len(payload["git_sha"]) == 7  # short sha
    assert payload["provider"] == "bedrock"


def test_build_version_payload_degrades_without_git(tmp_path, monkeypatch):
    repo_root = _make_fake_repo(tmp_path, init_git=False)
    monkeypatch.setenv("TRID3NT_REPO_ROOT", str(repo_root))
    payload = plugin_repo.build_version_payload()
    assert payload["git_sha"] == "unknown"


# ---------------------------------------------------------------------------
# HTTP dispatch (tool_catalog_http._handle_http), mirrors
# test_local_models_http_route.py's fake reader/writer pattern.
# ---------------------------------------------------------------------------


class _FakeReader:
    def __init__(self, request: bytes):
        self._lines = request.split(b"\r\n")
        self._buf = [ln + b"\r\n" for ln in self._lines]

    async def readline(self):
        if self._buf:
            return self._buf.pop(0)
        return b""


class _FakeWriter:
    def __init__(self):
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes):
        self.buffer.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True


def _request(path: str, *, host: str | None = "agent.local") -> bytes:
    if host is None:
        return f"GET {path} HTTP/1.1\r\n\r\n".encode()
    return f"GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode()


def _run(coro):
    return asyncio.run(coro)


def _status(out: bytes) -> int:
    return int(out.split(b" ", 2)[1])


def _headers(out: bytes) -> dict[str, str]:
    head, _, _ = out.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")[1:]
    out_headers: dict[str, str] = {}
    for line in lines:
        name, _, value = line.partition(":")
        if name:
            out_headers[name.strip().lower()] = value.strip()
    return out_headers


def _body_bytes(out: bytes) -> bytes:
    _, _, body = out.partition(b"\r\n\r\n")
    return body


def _body_json(out: bytes) -> dict:
    return json.loads(_body_bytes(out).decode("utf-8"))


def _dispatch(path: str, *, host: str | None = "agent.local") -> _FakeWriter:
    reader = _FakeReader(_request(path, host=host))
    writer = _FakeWriter()
    _run(tool_catalog_http._handle_http(reader, writer))
    return writer


# --- /api/version -----------------------------------------------------------


def test_version_route_200(monkeypatch):
    monkeypatch.setattr(
        plugin_repo, "build_version_payload", lambda: {"git_sha": "abc1234", "provider": "bedrock"}
    )
    out = bytes(_dispatch("/api/version").buffer)
    assert _status(out) == 200
    assert _body_json(out) == {"git_sha": "abc1234", "provider": "bedrock"}


def test_version_route_failure_is_500(monkeypatch):
    def _boom():
        raise RuntimeError("git subprocess exploded")

    monkeypatch.setattr(plugin_repo, "build_version_payload", _boom)
    out = bytes(_dispatch("/api/version").buffer)
    assert _status(out) == 500


# --- /plugins/plugins.xml ---------------------------------------------------


def test_plugins_xml_route_uses_host_header_for_download_url(monkeypatch):
    captured = {}

    def _fake_build(download_url):
        captured["download_url"] = download_url
        return b"<plugins/>"

    monkeypatch.setattr(plugin_repo, "build_plugins_repo_xml", _fake_build)
    out = bytes(_dispatch("/plugins/plugins.xml", host="agent.local:8766").buffer)
    assert _status(out) == 200
    assert _headers(out)["content-type"].startswith("text/xml")
    assert captured["download_url"] == "http://agent.local:8766/plugins/trid3nt.zip"
    assert _body_bytes(out) == b"<plugins/>"


def test_plugins_xml_route_falls_back_when_host_header_absent(monkeypatch):
    monkeypatch.delenv("TRID3NT_AGENT_HTTP_PORT", raising=False)
    captured = {}

    def _fake_build(download_url):
        captured["download_url"] = download_url
        return b"<plugins/>"

    monkeypatch.setattr(plugin_repo, "build_plugins_repo_xml", _fake_build)
    out = bytes(_dispatch("/plugins/plugins.xml", host=None).buffer)
    assert _status(out) == 200
    assert captured["download_url"] == "http://127.0.0.1:8766/plugins/trid3nt.zip"


def test_plugins_xml_route_build_error_is_503(monkeypatch):
    def _boom(download_url):
        raise plugin_repo.PluginRepoBuildError("no plugin source tree")

    monkeypatch.setattr(plugin_repo, "build_plugins_repo_xml", _boom)
    out = bytes(_dispatch("/plugins/plugins.xml").buffer)
    assert _status(out) == 503
    assert "no plugin source tree" in _body_json(out)["error"]


def test_plugins_xml_route_unexpected_error_is_500(monkeypatch):
    def _boom(download_url):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(plugin_repo, "build_plugins_repo_xml", _boom)
    out = bytes(_dispatch("/plugins/plugins.xml").buffer)
    assert _status(out) == 500


# --- /plugins/trid3nt.zip ---------------------------------------------------


def test_zip_route_serves_bytes_with_disposition(tmp_path, monkeypatch):
    zip_bytes = b"PK\x03\x04fake-zip-bytes"
    zip_path = tmp_path / "trid3nt.zip"
    zip_path.write_bytes(zip_bytes)
    monkeypatch.setattr(
        plugin_repo,
        "ensure_plugin_zip",
        lambda: {"zip_path": zip_path, "version": "1.2.3+abc", "head_sha": "abc"},
    )
    out = bytes(_dispatch("/plugins/trid3nt.zip").buffer)
    assert _status(out) == 200
    headers = _headers(out)
    assert headers["content-type"] == "application/zip"
    assert 'filename="trid3nt.zip"' in headers["content-disposition"]
    assert _body_bytes(out) == zip_bytes


def test_zip_route_build_error_is_503(monkeypatch):
    def _boom():
        raise plugin_repo.PluginRepoBuildError("no plugin source tree")

    monkeypatch.setattr(plugin_repo, "ensure_plugin_zip", _boom)
    out = bytes(_dispatch("/plugins/trid3nt.zip").buffer)
    assert _status(out) == 503


def test_zip_route_unexpected_error_is_500(monkeypatch):
    def _boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(plugin_repo, "ensure_plugin_zip", _boom)
    out = bytes(_dispatch("/plugins/trid3nt.zip").buffer)
    assert _status(out) == 500


# --- unrelated path still 404 (route additions did not widen the surface) --


def test_unknown_plugins_path_is_404():
    out = bytes(_dispatch("/plugins/does-not-exist").buffer)
    assert _status(out) == 404
