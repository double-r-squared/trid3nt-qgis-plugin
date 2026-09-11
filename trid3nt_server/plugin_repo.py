"""QGIS custom plugin repository: deploy-time package, per-request serve.

``package_plugin_repo`` writes the versioned zip, ``plugins.xml`` and a manifest;
serving substitutes the per-request Host for :data:`HOST_SENTINEL` and can build a
fresh zip from ``plugin/`` on demand. Every function here is SYNC."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("trid3nt_server.plugin_repo")

__all__ = [
    "PLUGIN_NAME",
    "HOST_SENTINEL",
    "FRESH_ZIP_URL_PATH",
    "PluginRepoBuildError",
    "package_plugin_repo",
    "render_plugins_xml",
    "served_zip_path",
    "build_fresh_zip",
    "read_manifest",
    "build_plugins_repo_xml",
    "build_version_payload",
]

PLUGIN_NAME = "trid3nt"
_DEFAULT_QGIS_MINIMUM_VERSION = "3.28"

#: Placeholder host stamped into the packaged ``plugins.xml`` download_url;
#: ``render_plugins_xml`` swaps it for the per-request Host so the served index
#: always points at a host the client can actually reach.
HOST_SENTINEL = "__TRID3NT_DAEMON_HOST__"

#: Fixed-name route the FRESH zip is served at. The literal in
#: ``catalog_http.py``'s route dispatch MUST match this string.
FRESH_ZIP_URL_PATH = "/plugin-repo/trid3nt.zip"

#: Basename patterns never carried into the zip (caches, hidden files, and the
#: dev-loop marker ``install_plugin.sh`` drops into an installed profile).
_ZIP_IGNORE_PATTERNS = ("__pycache__", "*.pyc", ".*", "installed_version.txt")
_TREE_HASH_EXCLUDE_NAMES = {"installed_version.txt"}

#: Repo-checkout siblings that live beside the plugin package at ``plugin/`` but
#: must never ship: the plugin's own test suite + build Makefile + docs + any
#: build output, and the license text (re-added INSIDE the zip separately).
#: Matched at the plugin-source ROOT only.
_NON_SHIPPING_TOPLEVEL = frozenset(
    {"tests", "Makefile", "README.md", "LICENSE", "docs", "dist"}
)


def _staging_ignore(plugin_src: Path):
    """``copytree`` ignore for :func:`_build_zip`: the base cache/hidden/marker
    patterns at every level, plus the non-shipping siblings at the source root."""
    plugin_src = plugin_src.resolve()
    base = shutil.ignore_patterns(*_ZIP_IGNORE_PATTERNS)

    def _ignore(directory, names):
        ignored = set(base(directory, names))
        if Path(directory).resolve() == plugin_src:
            ignored |= {n for n in names if n in _NON_SHIPPING_TOPLEVEL}
        return ignored

    return _ignore


class PluginRepoBuildError(Exception):
    """The plugin repository could not be packaged or served (no source tree
    on this checkout, a filesystem fault while staging/zipping, or a serve
    request before the repo was ever packaged)."""




def _repo_root() -> Path:
    """The daemon's OWN checkout root, the directory containing ``plugin/``:
    ``TRID3NT_REPO_ROOT`` when set, else two parents up from this file.
    """
    env = os.environ.get("TRID3NT_REPO_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def _plugin_src_dir(repo_root: Path) -> Path:
    return repo_root / "plugin"


def _served_dir(served_dir: Path | str | None = None) -> Path:
    """The directory holding the packaged zip, ``plugins.xml`` and
    ``manifest.json``: the ``served_dir`` argument, else
    ``TRID3NT_PLUGIN_REPO_DIR``, else ``<repo_root>/run/plugin-repo``."""
    if served_dir is not None:
        return Path(served_dir).expanduser().resolve()
    env = os.environ.get("TRID3NT_PLUGIN_REPO_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return _repo_root() / "run" / "plugin-repo"




def _parse_metadata_txt(path: Path) -> dict[str, str]:
    """Parse ``key=value`` lines from a QGIS plugin ``metadata.txt``. Skips
    blank lines, ``#`` comments, and the ``[general]`` section header -- every
    real field in this file is a flat ``key=value`` line."""
    if not path.is_file():
        raise PluginRepoBuildError(f"metadata.txt not found: {path}")
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        fields[key.strip()] = value.strip()
    return fields


def _plugin_version(plugin_src: Path) -> str:
    """The plugin version, verbatim from ``metadata.txt`` (no suffixing)."""
    fields = _parse_metadata_txt(plugin_src / "metadata.txt")
    version = fields.get("version")
    if not version:
        raise PluginRepoBuildError(
            f"metadata.txt at {plugin_src} has no version= line"
        )
    return version


def _iter_packaged_files(plugin_src: Path):
    """Every file that belongs in a packaged zip, in sorted-relpath order; the
    one exclude rule shared by the tree hash, the mtime signature and the
    fresh-build zip."""
    for item in sorted(plugin_src.rglob("*")):
        if not item.is_file():
            continue
        parts = item.relative_to(plugin_src).parts
        if parts and parts[0] in _NON_SHIPPING_TOPLEVEL:
            continue
        if any(p == "__pycache__" or p.startswith(".") for p in parts):
            continue
        if item.suffix == ".pyc" or item.name in _TREE_HASH_EXCLUDE_NAMES:
            continue
        yield item


def _tree_sha(plugin_src: Path) -> str:
    """A stable content hash of the packaged plugin tree: every packaged file as
    ``<relpath>\\0<bytes>`` in sorted order, so two byte-identical trees hash the
    same whatever their mtimes."""
    h = hashlib.sha256()
    for item in _iter_packaged_files(plugin_src):
        rel = item.relative_to(plugin_src).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(item.read_bytes())
    return h.hexdigest()


def _source_signature(plugin_src: Path) -> tuple[tuple[str, int, int], ...]:
    """Stat-only signature of the packaged tree - ``(relpath, size, mtime_ns)``
    per file, sorted - cheap enough to run on every request because it never
    reads file bytes."""
    entries = []
    for item in _iter_packaged_files(plugin_src):
        st = item.stat()
        entries.append((item.relative_to(plugin_src).as_posix(), st.st_size, st.st_mtime_ns))
    return tuple(entries)


# Zip build (mirrors the repo-root ``make plugin-zip`` shape: a top-level
# ``trid3nt/`` dir, LICENSE copied inside it, caches/hidden files excluded).


def _build_zip(plugin_src: Path, dest_zip: Path) -> None:
    if not plugin_src.is_dir():
        raise PluginRepoBuildError(f"plugin source tree not found: {plugin_src}")

    staging_root = dest_zip.parent / "_staging"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_plugin = staging_root / PLUGIN_NAME
    try:
        shutil.copytree(plugin_src, staging_plugin, ignore=_staging_ignore(plugin_src))
        license_src = plugin_src / "LICENSE"
        if license_src.is_file():
            shutil.copy2(license_src, staging_plugin / "LICENSE")

        tmp_zip = dest_zip.with_suffix(".zip.tmp")
        if tmp_zip.exists():
            tmp_zip.unlink()
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(staging_plugin.rglob("*")):
                if item.is_file():
                    zf.write(item, item.relative_to(staging_root))
        # Atomic swap -- a concurrent reader never sees a partial file.
        tmp_zip.replace(dest_zip)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _build_zip_bytes(repo_root: Path, plugin_src: Path) -> bytes:
    """In-memory build for :func:`build_fresh_zip`: the same layout and excludes
    as ``_build_zip`` plus a ``trid3nt/installed_version.txt`` provenance stamp
    that the deploy-time zip deliberately excludes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in _iter_packaged_files(plugin_src):
            arcname = f"{PLUGIN_NAME}/{item.relative_to(plugin_src).as_posix()}"
            zf.write(item, arcname)
        license_src = plugin_src / "LICENSE"
        if license_src.is_file():
            zf.write(license_src, f"{PLUGIN_NAME}/LICENSE")
        sha, branch = _git_provenance(repo_root)
        zf.writestr(f"{PLUGIN_NAME}/installed_version.txt", f"{sha}\n{branch}\n")
    return buf.getvalue()


#: In-memory fresh-zip cache: ``str(plugin_src) -> (signature, zip_bytes,
#: version, zip_filename)``. Keyed by path so a test that swaps
#: ``TRID3NT_REPO_ROOT`` never sees another test's bytes.
_fresh_zip_cache: dict[str, tuple[Any, bytes, str, str]] = {}
_fresh_zip_lock = threading.Lock()


def build_fresh_zip(repo_root: Path | None = None) -> tuple[bytes, str, str]:
    """SYNC - build, or reuse a cached, plugin zip straight from ``plugin/``,
    returning ``(zip_bytes, version, zip_filename)``; every call re-stats the
    tree and rebuilds only when a file's size or mtime changed."""
    root = repo_root if repo_root is not None else _repo_root()
    plugin_src = _plugin_src_dir(root)
    if not plugin_src.is_dir():
        raise PluginRepoBuildError(f"plugin source tree not found: {plugin_src}")

    cache_key = str(plugin_src)
    signature = _source_signature(plugin_src)
    with _fresh_zip_lock:
        cached = _fresh_zip_cache.get(cache_key)
        if cached is not None and cached[0] == signature:
            _sig, data, version, zip_filename = cached
            return data, version, zip_filename

        version = _plugin_version(plugin_src)
        zip_filename = f"{PLUGIN_NAME}-{version}.zip"
        data = _build_zip_bytes(root, plugin_src)
        _fresh_zip_cache[cache_key] = (signature, data, version, zip_filename)
        return data, version, zip_filename




def _cdata(text: str) -> str:
    """Escape a ``]]>`` sequence that would otherwise terminate the CDATA
    section early (defensive -- none of our fields contain one today)."""
    return text.replace("]]>", "]]]]><![CDATA[>")


def _xml_escape(text: str) -> str:
    from xml.sax.saxutils import escape

    return escape(text)


def _xml_attr_escape(text: str) -> str:
    from xml.sax.saxutils import escape

    return escape(text, {'"': "&quot;"})


_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<plugins>
  <pyqgis_plugin name="{name_attr}" version="{version_attr}">
    <description><![CDATA[{description}]]></description>
    <about><![CDATA[{about}]]></about>
    <version>{version}</version>
    <qgis_minimum_version>{qgis_min}</qgis_minimum_version>
    <qgis_maximum_version>{qgis_max}</qgis_maximum_version>
    <homepage><![CDATA[{homepage}]]></homepage>
    <file_name>{file_name}</file_name>
    <icon>{icon}</icon>
    <author_name><![CDATA[{author}]]></author_name>
    <download_url>{download_url}</download_url>
    <uploaded_by><![CDATA[{author}]]></uploaded_by>
    <experimental>{experimental}</experimental>
    <deprecated>{deprecated}</deprecated>
    <tracker><![CDATA[{tracker}]]></tracker>
    <repository><![CDATA[{repository}]]></repository>
    <tags><![CDATA[{tags}]]></tags>
  </pyqgis_plugin>
</plugins>
"""


def build_plugins_repo_xml(
    plugin_src: Path, download_url: str, file_name: str, version: str
) -> bytes:
    """Render the QGIS plugin-repository index XML for ``plugin_src``; pure and
    deterministic, since ``version``, ``file_name`` and ``download_url`` are all
    supplied by the caller."""
    fields = _parse_metadata_txt(plugin_src / "metadata.txt")
    xml = _XML_TEMPLATE.format(
        name_attr=_xml_attr_escape(fields.get("name") or PLUGIN_NAME),
        version_attr=_xml_attr_escape(version),
        version=_xml_escape(version),
        description=_cdata(fields.get("description") or ""),
        about=_cdata(fields.get("about") or ""),
        qgis_min=_xml_escape(
            fields.get("qgisMinimumVersion") or _DEFAULT_QGIS_MINIMUM_VERSION
        ),
        qgis_max=_xml_escape(fields.get("qgisMaximumVersion") or ""),
        homepage=_cdata(fields.get("homepage") or ""),
        file_name=_xml_escape(file_name),
        icon=_xml_escape(fields.get("icon") or ""),
        author=_cdata(fields.get("author") or ""),
        download_url=_xml_escape(download_url),
        experimental=_xml_escape(fields.get("experimental") or "True"),
        deprecated=_xml_escape(fields.get("deprecated") or "False"),
        tracker=_cdata(fields.get("tracker") or ""),
        repository=_cdata(fields.get("repository") or ""),
        tags=_cdata(fields.get("tags") or ""),
    )
    return xml.encode("utf-8")




def read_manifest(served_dir: Path | str | None = None) -> dict[str, Any] | None:
    """The last packaged build's ``manifest.json`` (``version`` /
    ``tree_sha`` / ``zip_filename``), or ``None`` if never packaged / unreadable."""
    meta_path = _served_dir(served_dir) / "manifest.json"
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None




def package_plugin_repo(served_dir: Path | str | None = None) -> dict[str, Any]:
    """SYNC - rebuild the served repository (versioned zip, ``plugins.xml`` with
    the sentinel host, ``manifest.json``), returning ``{"version",
    "zip_filename", "tree_sha", "warned", "served_dir"}``."""
    repo_root = _repo_root()
    plugin_src = _plugin_src_dir(repo_root)
    if not plugin_src.is_dir():
        raise PluginRepoBuildError(f"plugin source tree not found: {plugin_src}")

    version = _plugin_version(plugin_src)
    tree_sha = _tree_sha(plugin_src)
    dest = _served_dir(served_dir)
    dest.mkdir(parents=True, exist_ok=True)

    warned = False
    previous = read_manifest(dest)
    if (
        previous is not None
        and previous.get("version") == version
        and previous.get("tree_sha") != tree_sha
    ):
        warned = True
        logger.warning(
            "plugin-repo: packaged tree changed but version=%s was NOT bumped "
            "-- QGIS Plugin Manager will not offer the update. Bump version= in "
            "plugin/metadata.txt.",
            version,
        )

    zip_filename = f"{PLUGIN_NAME}-{version}.zip"
    for stale in dest.glob(f"{PLUGIN_NAME}-*.zip"):
        if stale.name != zip_filename:
            stale.unlink()
    _build_zip(plugin_src, dest / zip_filename)

    # download_url points at the FIXED fresh-build endpoint rather than this
    # versioned artifact, so a client never depends on this packaging step
    # having run; ``?v=`` is a cache-busting hint the server never reads.
    download_url = f"http://{HOST_SENTINEL}{FRESH_ZIP_URL_PATH}?v={version}"
    xml = build_plugins_repo_xml(plugin_src, download_url, zip_filename, version)
    (dest / "plugins.xml").write_bytes(xml)

    (dest / "manifest.json").write_text(
        json.dumps(
            {"version": version, "tree_sha": tree_sha, "zip_filename": zip_filename}
        ),
        encoding="utf-8",
    )
    return {
        "version": version,
        "zip_filename": zip_filename,
        "tree_sha": tree_sha,
        "warned": warned,
        "served_dir": str(dest),
    }




def render_plugins_xml(host: str, served_dir: Path | str | None = None) -> bytes:
    """SYNC - read the packaged ``plugins.xml`` and substitute ``host`` for the
    :data:`HOST_SENTINEL`, so a client reaches the zip on the host it dialed.
    Refuses with :class:`PluginRepoBuildError` when nothing was packaged yet."""
    xml_path = _served_dir(served_dir) / "plugins.xml"
    if not xml_path.is_file():
        raise PluginRepoBuildError(
            "plugin repo not packaged yet -- run scripts/package_plugin.sh "
            "(or make agent)"
        )
    body = xml_path.read_text(encoding="utf-8")
    return body.replace(HOST_SENTINEL, host).encode("utf-8")


def served_zip_path(zip_filename: str, served_dir: Path | str | None = None) -> Path:
    """SYNC - resolve a ``GET /plugin-repo/<zip>`` filename to a real file.
    Path-traversal safe: no directory separators, ``.zip`` suffix required, and
    a name that is not in the served directory raises ``FileNotFoundError``."""
    name = zip_filename.strip()
    if not name.endswith(".zip") or "/" in name or "\\" in name or name.startswith("."):
        raise FileNotFoundError(zip_filename)
    path = _served_dir(served_dir) / name
    if not path.is_file():
        raise FileNotFoundError(zip_filename)
    return path




def _run_git(repo_root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    stdout = out.stdout.strip()
    return stdout or None


def _git_head_sha(repo_root: Path) -> str:
    """Full HEAD sha, or ``"unknown"`` (not a git checkout / git missing)."""
    return _run_git(repo_root, "rev-parse", "HEAD") or "unknown"


def _git_provenance(repo_root: Path) -> tuple[str, str]:
    """Short git sha and branch for ``repo_root``, the two values stamped into
    ``installed_version.txt``; ``"unknown"`` for either outside a git checkout.
    """
    head = _git_head_sha(repo_root)
    sha = head[:7] if head != "unknown" else "unknown"
    branch = _run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    return sha, branch


def build_version_payload() -> dict[str, Any]:
    """SYNC (git subprocess): the ``/api/version`` payload,
    ``{"git_sha", "provider"}``; either degrades to ``"unknown"`` rather than
    raising, because a discovery endpoint is never worth a 500."""
    repo_root = _repo_root()
    head = _git_head_sha(repo_root)
    git_sha = head[:7] if head != "unknown" else "unknown"
    try:
        from .adapters.model_selection import model_provider

        provider = model_provider()
    except Exception:  # noqa: BLE001 -- provider lookup is best-effort here
        provider = "unknown"
    return {"git_sha": git_sha, "provider": provider}
