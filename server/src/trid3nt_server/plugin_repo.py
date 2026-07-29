"""QGIS custom plugin repository: on-demand zip build + ``plugins.xml`` index.

BACKGROUND: the plugin used to ship its own in-dock "Update" button (settings
Update section, commit 8fd5ca2) that ran ``install_plugin.sh`` for the user.
That UI was removed (commit 9011b48 -- "native Plugin Manager path is the
story") on the promise that the daemon would eventually serve a real QGIS
custom plugin repository so QGIS's OWN Plugin Manager could handle updates,
on every client (including a Mac reaching the daemon over tailnet). This
module is that promise landing.

WHAT THIS SERVES (mounted by ``tool_catalog_http._handle_http``):

- ``GET /plugins/plugins.xml`` -- the QGIS plugin-repository index XML
  (``build_plugins_repo_xml``). One ``<pyqgis_plugin>`` entry describing THIS
  daemon's own checkout of ``qgis-plugin/trid3nt``.
- ``GET /plugins/trid3nt.zip`` -- the installable zip (``ensure_plugin_zip``),
  built from that same checkout with the correct top-level ``trid3nt/`` layout
  (the same shape ``make plugin-zip`` at the repo root produces).

A user adds ``http://<daemon-host>:8766/plugins/plugins.xml`` once under QGIS
Plugin Manager > Settings > Add repository (with "Check for updates"
enabled); from then on QGIS diffs the served ``version`` against the
installed one and offers Upgrade natively -- no plugin-side UI needed.

VERSION SCHEME (one line): ``<metadata.txt version>+<git describe --tags
--always>`` e.g. ``0.3.2+7f709a9`` -- ``git describe`` falls back to the
short SHA on its own when no tag is reachable (true for this repo today, it
carries no version tags), so the suffix always changes on the next commit and
Plugin Manager sees a version bump. The repo's REAL
``qgis-plugin/trid3nt/metadata.txt`` is NEVER modified -- this composed
string is stamped only into the STAGED zip's copy of ``metadata.txt`` at
build time (``_stamp_metadata_version``), so ``plugins.xml``'s ``<version>``
and the zip's installed ``metadata.txt version=`` always agree (the
comparison Plugin Manager makes to decide there's an update).

BUILD CACHING: ``ensure_plugin_zip`` keys the cached zip on the daemon's own
git HEAD sha (one cheap ``git rev-parse HEAD``) -- a request when HEAD has
not moved reuses the cached zip; HEAD moving (a new commit landed, e.g. after
a ``git pull`` on the daemon box) triggers exactly one rebuild on the next
request. Cache lives under ``run/plugin-repo-cache/`` (server-owned scratch,
gitignored like the rest of ``run/``; override via
``TRID3NT_PLUGIN_REPO_CACHE_DIR``).

Every public function here is SYNC (subprocess + filesystem + zipfile work);
callers (``tool_catalog_http``) wrap them in ``asyncio.to_thread`` -- no sync
work belongs on the agent's event loop (it would stall the WS keepalive).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

__all__ = [
    "PLUGIN_NAME",
    "PluginRepoBuildError",
    "ensure_plugin_zip",
    "build_plugins_repo_xml",
    "build_version_payload",
]

PLUGIN_NAME = "trid3nt"
_DEFAULT_QGIS_MINIMUM_VERSION = "3.28"


class PluginRepoBuildError(Exception):
    """The plugin zip / repo index could not be built (no source tree on this
    checkout, or a filesystem fault while staging/zipping). NEVER raised for
    a git failure -- that degrades to an honest ``"unknown"`` sha/version
    instead (see ``_git_head_sha`` / ``_git_describe``), so a daemon running
    from a git-less checkout (e.g. an extracted tarball) still serves a zip,
    just without HEAD-keyed caching."""


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """The daemon's OWN checkout root (the directory containing
    ``qgis-plugin/``). Default: derived from this file's location
    (``server/src/trid3nt_server/plugin_repo.py`` -> repo root is three
    parents up). Override via ``TRID3NT_REPO_ROOT`` (tests; also covers an
    installed-package layout where the source-tree-relative walk would be
    wrong).
    """
    import os

    env = os.environ.get("TRID3NT_REPO_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


def _plugin_src_dir(repo_root: Path) -> Path:
    return repo_root / "qgis-plugin" / PLUGIN_NAME


def _cache_dir() -> Path:
    """Server-owned scratch/cache location for the built zip + its build
    metadata. Default ``<repo_root>/run/plugin-repo-cache`` -- ``run/`` is
    already the repo's convention for gitignored service-owned ephemeral
    state (PID files, solver-runs). Override via
    ``TRID3NT_PLUGIN_REPO_CACHE_DIR``.
    """
    import os

    env = os.environ.get("TRID3NT_PLUGIN_REPO_CACHE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return _repo_root() / "run" / "plugin-repo-cache"


# ---------------------------------------------------------------------------
# git (best-effort -- see PluginRepoBuildError docstring)
# ---------------------------------------------------------------------------


def _run_git(repo_root: Path, *args: str) -> str | None:
    """Run ``git -C <repo_root> <args>``; return stripped stdout, or ``None``
    on any failure (missing git binary, not a checkout, non-zero exit)."""
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


def _git_describe(repo_root: Path) -> str:
    """``git describe --tags --always`` -- git's own fallback to the short
    SHA when no tag is reachable. ``"unknown"`` when git is unavailable."""
    return _run_git(repo_root, "describe", "--tags", "--always") or "unknown"


# ---------------------------------------------------------------------------
# metadata.txt (read the real one; stamp only the staged COPY)
# ---------------------------------------------------------------------------


def _parse_metadata_txt(path: Path) -> dict[str, str]:
    """Parse ``key=value`` lines from a QGIS plugin ``metadata.txt``. Skips
    blank lines, ``#`` comments, and the ``[general]`` section header --
    every real field in this file is a flat ``key=value`` line."""
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


_VERSION_LINE_RE = re.compile(r"^version=.*$", re.MULTILINE)


def _stamp_metadata_version(path: Path, version: str) -> None:
    """Rewrite the ``version=`` line of a STAGED (zip-bound) ``metadata.txt``
    copy in place. Never touches the repo's real file -- the caller only ever
    passes a path under the staging tree built by ``_build_zip``."""
    text = path.read_text(encoding="utf-8")
    stamped, n = _VERSION_LINE_RE.subn(f"version={version}", text, count=1)
    if n == 0:
        raise PluginRepoBuildError(f"metadata.txt at {path} has no version= line")
    path.write_text(stamped, encoding="utf-8")


def _plugin_version(repo_root: Path, plugin_src: Path) -> str:
    """See the module docstring's VERSION SCHEME section."""
    fields = _parse_metadata_txt(plugin_src / "metadata.txt")
    base = fields.get("version") or "0.0.0"
    return f"{base}+{_git_describe(repo_root)}"


# ---------------------------------------------------------------------------
# Zip build (mirrors the repo-root ``make plugin-zip`` shape: a top-level
# ``trid3nt/`` dir, LICENSE copied inside it, caches/hidden files excluded).
# ---------------------------------------------------------------------------


def _build_zip(repo_root: Path, plugin_src: Path, version: str, dest_zip: Path) -> None:
    if not plugin_src.is_dir():
        raise PluginRepoBuildError(f"plugin source tree not found: {plugin_src}")

    staging_root = dest_zip.parent / "_staging"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_plugin = staging_root / PLUGIN_NAME
    try:
        shutil.copytree(
            plugin_src,
            staging_plugin,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".*"),
        )
        license_src = repo_root / "qgis-plugin" / "LICENSE"
        if license_src.is_file():
            shutil.copy2(license_src, staging_plugin / "LICENSE")
        _stamp_metadata_version(staging_plugin / "metadata.txt", version)

        tmp_zip = dest_zip.with_suffix(".zip.tmp")
        if tmp_zip.exists():
            tmp_zip.unlink()
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(staging_plugin.rglob("*")):
                if item.is_file():
                    zf.write(item, item.relative_to(staging_root))
        # Atomic swap -- a concurrent reader of dest_zip never sees a
        # partially-written file.
        tmp_zip.replace(dest_zip)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _read_build_meta(meta_path: Path) -> dict[str, Any] | None:
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def ensure_plugin_zip() -> dict[str, Any]:
    """SYNC (git subprocess + filesystem copy/zip) -- caller wraps in
    ``asyncio.to_thread``.

    Ensures ``<cache_dir>/trid3nt.zip`` reflects the daemon's OWN checkout at
    its CURRENT git HEAD, rebuilding only when HEAD has moved since the last
    build (the cheap staleness check: one ``git rev-parse HEAD`` compared
    against the cached build's recorded sha). ``head_sha == "unknown"``
    (no git) always rebuilds -- there is no honest cache key to compare
    against.

    Returns ``{"zip_path": Path, "version": str, "head_sha": str}``.
    """
    repo_root = _repo_root()
    plugin_src = _plugin_src_dir(repo_root)
    if not plugin_src.is_dir():
        raise PluginRepoBuildError(f"plugin source tree not found: {plugin_src}")

    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cache_dir / f"{PLUGIN_NAME}.zip"
    meta_path = cache_dir / "build.json"

    head_sha = _git_head_sha(repo_root)
    cached = _read_build_meta(meta_path)
    if (
        cached is not None
        and head_sha != "unknown"
        and cached.get("head_sha") == head_sha
        and zip_path.is_file()
    ):
        return {
            "zip_path": zip_path,
            "version": cached.get("version") or "unknown",
            "head_sha": head_sha,
        }

    version = _plugin_version(repo_root, plugin_src)
    _build_zip(repo_root, plugin_src, version, zip_path)
    meta_path.write_text(
        json.dumps({"head_sha": head_sha, "version": version}), encoding="utf-8"
    )
    return {"zip_path": zip_path, "version": version, "head_sha": head_sha}


# ---------------------------------------------------------------------------
# plugins.xml
# ---------------------------------------------------------------------------


def _cdata(text: str) -> str:
    """Escape a ``]]>`` sequence that would otherwise terminate the CDATA
    section early (defensive -- none of our fields contain one today)."""
    return text.replace("]]>", "]]]]><![CDATA[>")


def _xml_escape(text: str) -> str:
    """Escape for XML ELEMENT TEXT (``&`` ``<`` ``>``)."""
    from xml.sax.saxutils import escape

    return escape(text)


def _xml_attr_escape(text: str) -> str:
    """Escape for a double-quoted XML ATTRIBUTE VALUE -- also escapes ``"``,
    which plain ``_xml_escape`` deliberately leaves alone (safe in element
    text, unsafe inside ``name="..."``)."""
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


def build_plugins_repo_xml(download_url: str) -> bytes:
    """SYNC (calls ``ensure_plugin_zip``, which shells to git) -- caller wraps
    in ``asyncio.to_thread``.

    Builds the QGIS plugin-repository index XML for THIS daemon's own
    checkout. ``download_url`` is the caller-supplied absolute URL of the
    zip route (``tool_catalog_http`` derives it from the request's own Host
    header, so a tailnet client's repository URL round-trips to a reachable
    zip URL without a hardcoded host).

    ``<version>`` here is byte-identical to the ``version=`` line stamped
    into the served zip's ``metadata.txt`` (both come from the SAME
    ``ensure_plugin_zip()`` call) -- that agreement is exactly what Plugin
    Manager compares to decide "installed" vs "available".
    """
    info = ensure_plugin_zip()
    repo_root = _repo_root()
    plugin_src = _plugin_src_dir(repo_root)
    fields = _parse_metadata_txt(plugin_src / "metadata.txt")
    version = info["version"]

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
        file_name=_xml_escape(f"{PLUGIN_NAME}.zip"),
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


# ---------------------------------------------------------------------------
# /api/version
# ---------------------------------------------------------------------------


def build_version_payload() -> dict[str, Any]:
    """SYNC (git subprocess) -- caller wraps in ``asyncio.to_thread``.

    The tiny version indicator the removed plugin-settings Update section
    wanted (see the module docstring): ``{"git_sha": <short sha>,
    "provider": <active MODEL_PROVIDER>}``. Both fields degrade to
    ``"unknown"`` rather than raising -- this is a cheap discovery endpoint,
    never worth a 500.
    """
    repo_root = _repo_root()
    head = _git_head_sha(repo_root)
    git_sha = head[:7] if head != "unknown" else "unknown"
    try:
        from .agent.adapters.bedrock_adapter import model_provider

        provider = model_provider()
    except Exception:  # noqa: BLE001 -- provider lookup is best-effort here
        provider = "unknown"
    return {"git_sha": git_sha, "provider": provider}
