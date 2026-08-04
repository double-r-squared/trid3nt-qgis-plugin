"""Offline tests for the daemon-hosted QGIS plugin repository (plugin_repo.py)
and its ``/plugin-repo/*`` HTTP routes on the tool-catalog listener:

  - ``GET /plugin-repo/plugins.xml``    -- the QGIS plugin-repository index XML,
    with its download_url host filled from the request's own Host header.
  - ``GET /plugin-repo/<zip>``          -- the packaged installable zip.
  - ``GET /api/version``                -- daemon git sha + active model provider.

Covers: the deploy-time package (versioned zip name, top-level ``trid3nt/``
dir, LICENSE inside, caches/hidden/installed-marker excluded, metadata-driven
version, manifest + plugins.xml written with the HOST_SENTINEL), the
version-drift warning (tree changed but version not bumped), per-request host
substitution, the zip-serve path-traversal guard, and the HTTP dispatcher's
routing + error mapping (404/500/503).

Everything runs against a throwaway plugin tree + served dir under ``tmp_path``
-- no network, no real daemon checkout touched.
"""

from __future__ import annotations

import asyncio
import json
import logging
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


def _make_fake_repo(tmp_path: Path) -> Path:
    """Build ``<tmp_path>/repo`` with a minimal ``qgis-plugin/trid3nt`` tree
    (metadata.txt, __init__.py, a nested module, a __pycache__ dir, a hidden
    file, and an installed_version.txt marker to prove exclusion) plus a
    top-level LICENSE. No git needed -- the version is metadata-driven now.
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
    # Exclusion bait: __pycache__, a .pyc, a dotfile, the installed marker.
    pycache = plugin_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "plugin.cpython-312.pyc").write_bytes(b"\x00\x01")
    (plugin_dir / ".hidden").write_text("should be excluded\n", encoding="utf-8")
    (plugin_dir / "installed_version.txt").write_text("dev\n", encoding="utf-8")

    (repo_root / "qgis-plugin" / "LICENSE").write_text(
        "MIT-ish fixture\n", encoding="utf-8"
    )
    return repo_root


@pytest.fixture()
def fake_repo(tmp_path, monkeypatch):
    repo_root = _make_fake_repo(tmp_path)
    served = tmp_path / "served"
    monkeypatch.setenv("TRID3NT_REPO_ROOT", str(repo_root))
    monkeypatch.setenv("TRID3NT_PLUGIN_REPO_DIR", str(served))
    return repo_root


# ---------------------------------------------------------------------------
# package_plugin_repo -- deploy-time build
# ---------------------------------------------------------------------------


def test_package_builds_versioned_zip_and_index(fake_repo, tmp_path):
    info = plugin_repo.package_plugin_repo()
    served = Path(info["served_dir"])

    assert info["version"] == "1.2.3"  # metadata-driven, no suffix
    assert info["zip_filename"] == "trid3nt-1.2.3.zip"
    assert info["warned"] is False

    zip_path = served / "trid3nt-1.2.3.zip"
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert all(n.startswith("trid3nt/") for n in names), names
        assert "trid3nt/__init__.py" in names
        assert "trid3nt/net/__init__.py" in names
        assert "trid3nt/metadata.txt" in names
        assert "trid3nt/LICENSE" in names  # copied INSIDE the plugin folder
        assert not any("__pycache__" in n for n in names)
        assert not any(n.endswith(".pyc") for n in names)
        assert not any(n.endswith("/.hidden") for n in names)
        assert not any(n.endswith("installed_version.txt") for n in names)
        # The in-zip metadata.txt version matches plugins.xml (Plugin Manager
        # compares the two) and is NOT stamped/suffixed.
        meta_text = zf.read("trid3nt/metadata.txt").decode("utf-8")
    assert "version=1.2.3\n" in meta_text

    # manifest + plugins.xml written; xml carries the sentinel host.
    manifest = json.loads((served / "manifest.json").read_text())
    assert manifest["version"] == "1.2.3"
    assert manifest["zip_filename"] == "trid3nt-1.2.3.zip"
    xml = (served / "plugins.xml").read_text()
    assert plugin_repo.HOST_SENTINEL in xml
    assert "trid3nt-1.2.3.zip" in xml


def test_package_prunes_stale_version_zips(fake_repo):
    served = Path(plugin_repo.package_plugin_repo()["served_dir"])
    (served / "trid3nt-0.0.1.zip").write_bytes(b"old")
    info = plugin_repo.package_plugin_repo()
    zips = sorted(p.name for p in served.glob("trid3nt-*.zip"))
    assert zips == ["trid3nt-1.2.3.zip"], zips
    assert info["zip_filename"] == "trid3nt-1.2.3.zip"


def test_package_does_not_touch_real_metadata(fake_repo):
    real_metadata = fake_repo / "qgis-plugin" / "trid3nt" / "metadata.txt"
    before = real_metadata.read_text(encoding="utf-8")
    plugin_repo.package_plugin_repo()
    assert real_metadata.read_text(encoding="utf-8") == before


def test_package_missing_source_tree_raises(tmp_path, monkeypatch):
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    monkeypatch.setenv("TRID3NT_REPO_ROOT", str(empty_root))
    monkeypatch.setenv("TRID3NT_PLUGIN_REPO_DIR", str(tmp_path / "served"))
    with pytest.raises(plugin_repo.PluginRepoBuildError):
        plugin_repo.package_plugin_repo()


# ---------------------------------------------------------------------------
# version-drift warning (metadata-driven, never auto-bumped)
# ---------------------------------------------------------------------------


def test_drift_warns_when_tree_changes_but_version_does_not(fake_repo, caplog):
    plugin_repo.package_plugin_repo()  # first build, no previous manifest
    plugin_file = fake_repo / "qgis-plugin" / "trid3nt" / "plugin.py"
    plugin_file.write_text("# changed body, same version\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="trid3nt_server.plugin_repo"):
        info = plugin_repo.package_plugin_repo()
    assert info["warned"] is True
    assert any("was NOT bumped" in r.message for r in caplog.records)


def test_no_drift_warning_when_version_bumped(fake_repo, caplog):
    plugin_repo.package_plugin_repo()
    meta = fake_repo / "qgis-plugin" / "trid3nt" / "metadata.txt"
    meta.write_text(
        _METADATA_TXT.replace("version=1.2.3", "version=1.2.4"), encoding="utf-8"
    )
    (fake_repo / "qgis-plugin" / "trid3nt" / "plugin.py").write_text(
        "# changed\n", encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger="trid3nt_server.plugin_repo"):
        info = plugin_repo.package_plugin_repo()
    assert info["warned"] is False
    assert info["version"] == "1.2.4"
    assert not any("was NOT bumped" in r.message for r in caplog.records)


def test_no_drift_warning_on_identical_repackage(fake_repo):
    plugin_repo.package_plugin_repo()
    info = plugin_repo.package_plugin_repo()  # same tree, same version
    assert info["warned"] is False


# ---------------------------------------------------------------------------
# render_plugins_xml -- per-request host substitution
# ---------------------------------------------------------------------------


def test_render_substitutes_host(fake_repo):
    import xml.etree.ElementTree as ET

    plugin_repo.package_plugin_repo()
    body = plugin_repo.render_plugins_xml("myhost:8766")
    assert plugin_repo.HOST_SENTINEL.encode() not in body
    root = ET.fromstring(body)
    plugin_el = root.find("pyqgis_plugin")
    assert plugin_el.get("name") == "TRID3NT"
    assert plugin_el.get("version") == "1.2.3"
    assert plugin_el.find("version").text == "1.2.3"
    assert plugin_el.find("qgis_minimum_version").text == "3.28"
    assert plugin_el.find("file_name").text == "trid3nt-1.2.3.zip"
    assert (
        plugin_el.find("download_url").text
        == "http://myhost:8766/plugin-repo/trid3nt-1.2.3.zip"
    )


def test_render_before_package_raises(fake_repo):
    with pytest.raises(plugin_repo.PluginRepoBuildError):
        plugin_repo.render_plugins_xml("myhost:8766")


# ---------------------------------------------------------------------------
# served_zip_path -- static serve + traversal guard
# ---------------------------------------------------------------------------


def test_served_zip_path_returns_packaged_file(fake_repo):
    plugin_repo.package_plugin_repo()
    path = plugin_repo.served_zip_path("trid3nt-1.2.3.zip")
    assert path.is_file()
    assert path.name == "trid3nt-1.2.3.zip"


@pytest.mark.parametrize(
    "bad",
    ["../etc/passwd.zip", "nested/x.zip", "plugins.xml", ".hidden.zip", "trid3nt-9.9.zip"],
)
def test_served_zip_path_rejects(fake_repo, bad):
    plugin_repo.package_plugin_repo()
    with pytest.raises(FileNotFoundError):
        plugin_repo.served_zip_path(bad)


# ---------------------------------------------------------------------------
# build_version_payload
# ---------------------------------------------------------------------------


def test_build_version_payload_shape(fake_repo, monkeypatch):
    subprocess.run(["git", "-C", str(fake_repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(fake_repo), "config", "user.email", "f@e.invalid"], check=True
    )
    subprocess.run(
        ["git", "-C", str(fake_repo), "config", "user.name", "F"], check=True
    )
    subprocess.run(["git", "-C", str(fake_repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(fake_repo), "commit", "-q", "-m", "init"], check=True
    )
    monkeypatch.setenv("MODEL_PROVIDER", "bedrock")
    payload = plugin_repo.build_version_payload()
    assert set(payload.keys()) == {"git_sha", "provider"}
    assert len(payload["git_sha"]) == 7
    assert payload["provider"] == "bedrock"


def test_build_version_payload_degrades_without_git(fake_repo):
    payload = plugin_repo.build_version_payload()
    assert payload["git_sha"] == "unknown"


# ---------------------------------------------------------------------------
# HTTP dispatch (tool_catalog_http._handle_http)
# ---------------------------------------------------------------------------


class _FakeReader:
    def __init__(self, request: bytes):
        self._buf = [ln + b"\r\n" for ln in request.split(b"\r\n")]

    async def readline(self):
        return self._buf.pop(0) if self._buf else b""


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


def _status(out: bytes) -> int:
    return int(out.split(b" ", 2)[1])


def _headers(out: bytes) -> dict[str, str]:
    head, _, _ = out.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")[1:]
    result: dict[str, str] = {}
    for line in lines:
        name, _, value = line.partition(":")
        if name:
            result[name.strip().lower()] = value.strip()
    return result


def _body_bytes(out: bytes) -> bytes:
    _, _, body = out.partition(b"\r\n\r\n")
    return body


def _body_json(out: bytes) -> dict:
    return json.loads(_body_bytes(out).decode("utf-8"))


def _dispatch(path: str, *, host: str | None = "agent.local") -> _FakeWriter:
    reader = _FakeReader(_request(path, host=host))
    writer = _FakeWriter()
    asyncio.run(tool_catalog_http._handle_http(reader, writer))
    return writer


# --- /api/version -----------------------------------------------------------


def test_version_route_200(monkeypatch):
    monkeypatch.setattr(
        plugin_repo,
        "build_version_payload",
        lambda: {"git_sha": "abc1234", "provider": "bedrock"},
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


# --- /plugin-repo/plugins.xml ----------------------------------------------


def test_plugins_xml_route_serves_packaged_index_with_host(fake_repo):
    plugin_repo.package_plugin_repo()
    out = bytes(_dispatch("/plugin-repo/plugins.xml", host="agent.local:8766").buffer)
    assert _status(out) == 200
    assert _headers(out)["content-type"].startswith("text/xml")
    body = _body_bytes(out).decode()
    assert (
        "http://agent.local:8766/plugin-repo/trid3nt-1.2.3.zip" in body
    )
    assert plugin_repo.HOST_SENTINEL not in body


def test_plugins_xml_route_falls_back_when_host_absent(fake_repo, monkeypatch):
    monkeypatch.delenv("TRID3NT_AGENT_HTTP_PORT", raising=False)
    plugin_repo.package_plugin_repo()
    out = bytes(_dispatch("/plugin-repo/plugins.xml", host=None).buffer)
    assert _status(out) == 200
    assert "http://127.0.0.1:8766/plugin-repo/trid3nt-1.2.3.zip" in _body_bytes(out).decode()


def test_plugins_xml_route_before_package_is_503(fake_repo):
    out = bytes(_dispatch("/plugin-repo/plugins.xml").buffer)
    assert _status(out) == 503
    assert "not packaged" in _body_json(out)["error"]


def test_plugins_xml_route_unexpected_error_is_500(fake_repo, monkeypatch):
    def _boom(host, served_dir=None):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(plugin_repo, "render_plugins_xml", _boom)
    out = bytes(_dispatch("/plugin-repo/plugins.xml").buffer)
    assert _status(out) == 500


# --- /plugin-repo/<zip> -----------------------------------------------------


def test_zip_route_serves_packaged_bytes(fake_repo):
    info = plugin_repo.package_plugin_repo()
    out = bytes(_dispatch(f"/plugin-repo/{info['zip_filename']}").buffer)
    assert _status(out) == 200
    headers = _headers(out)
    assert headers["content-type"] == "application/zip"
    assert 'filename="trid3nt-1.2.3.zip"' in headers["content-disposition"]
    served_zip = Path(info["served_dir"]) / info["zip_filename"]
    assert _body_bytes(out) == served_zip.read_bytes()


def test_zip_route_unknown_file_is_404(fake_repo):
    plugin_repo.package_plugin_repo()
    out = bytes(_dispatch("/plugin-repo/trid3nt-9.9.9.zip").buffer)
    assert _status(out) == 404


def test_zip_route_traversal_is_404(fake_repo):
    plugin_repo.package_plugin_repo()
    out = bytes(_dispatch("/plugin-repo/nope.zip").buffer)
    assert _status(out) == 404


# --- unrelated path still 404 ----------------------------------------------


def test_unknown_plugin_repo_path_is_404(fake_repo):
    out = bytes(_dispatch("/plugin-repo/does-not-exist").buffer)
    assert _status(out) == 404
