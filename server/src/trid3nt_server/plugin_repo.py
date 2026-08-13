"""QGIS custom plugin repository: deploy-time package + per-request serve.

The daemon hosts a real QGIS custom plugin repository so QGIS's OWN Plugin
Manager handles install + upgrade on every client (including a Mac reaching the
daemon over the tailnet). A user adds
``http://<daemon-host>:8766/plugin-repo/plugins.xml`` once under Plugin Manager
> Settings > Add repository; from then on QGIS diffs the served ``version``
against the installed one and offers Upgrade natively.

TWO PHASES, one source of truth:

- PACKAGE (deploy time, ``package_plugin_repo`` -- run by
  ``scripts/package_plugin.sh``, wired into ``make agent``). Builds the
  versioned zip ``trid3nt-<version>.zip`` from ``qgis-plugin/trid3nt`` into the
  served directory, regenerates ``plugins.xml`` from ``metadata.txt``, and
  writes a ``manifest.json``. The served directory is ``run/plugin-repo/``
  (server-owned, gitignored like the rest of ``run/``; override via
  ``TRID3NT_PLUGIN_REPO_DIR``).

- SERVE (per request, ``render_plugins_xml`` / ``served_zip_path`` -- mounted
  by ``tool_catalog_http._handle_http``). ``GET /plugin-repo/plugins.xml``
  returns the packaged index with its ``download_url`` host filled in from the
  REQUEST's own Host header; ``GET /plugin-repo/<versioned-zip>`` serves the
  packaged zip bytes as a fallback/manual-QA path.

- FRESH ZIP (per request, ``build_fresh_zip`` -- also mounted by
  ``tool_catalog_http._handle_http`` at the FIXED path
  :data:`FRESH_ZIP_URL_PATH`, ``/plugin-repo/trid3nt.zip``). This is the
  ``download_url`` every ``plugins.xml`` now advertises. It builds straight
  from ``qgis-plugin/trid3nt/`` on the daemon's OWN checkout -- no prior
  ``package_plugin_repo()`` deploy step required -- and mtime-caches the
  result (a cheap stat-only signature over the source tree; a real rebuild
  only happens when a file's size or mtime actually changed), so Install-
  from-ZIP / Plugin Manager's download is never stale behind a forgotten
  packaging step. The zip carries a build-time provenance stamp
  (``trid3nt/installed_version.txt``, git sha + branch) using the SAME
  two-line format ``scripts/install_plugin.sh`` writes into an rsync-
  installed profile -- today's PACKAGE zip excludes that file entirely (see
  ``_ZIP_IGNORE``), so a zip install previously had NO provenance a human
  could eyeball; the fresh-build path fixes that too.

HOST DERIVATION (the bug the stopgap static server had): a hardcoded IP in
``plugins.xml`` breaks the moment the client dials a different host. The
packaged ``plugins.xml`` therefore carries a :data:`HOST_SENTINEL` in place of
the host; ``render_plugins_xml`` substitutes the per-request Host at serve
time, so a tailnet client's "Add repository" URL always round-trips to a
reachable zip URL.

CACHE-BUSTING: ``download_url`` carries ``?v=<version>`` (the same
metadata.txt version driving everything else) so a client or intermediate
cache that keyed on the URL sees a new URL the moment the plugin version
changes. The server does not read or validate ``?v=`` -- it always serves
whatever ``build_fresh_zip`` currently builds; the query string is a pure
client/cache hint.

SAFARI CAVEAT: a browser (not QGIS itself) fetching ``download_url`` directly
-- e.g. a human clicking the link inside a rendered ``plugins.xml`` -- can
still auto-decompress the download depending on the browser's "open safe
files after downloading" setting (Safari on macOS defaults this on for
``application/zip``). ``Content-Type: application/zip`` +
``Content-Disposition: attachment`` reduce the chance but cannot eliminate
it -- that setting is entirely client-side. QGIS's OWN Plugin Manager
download path does not go through the browser and is unaffected.

VERSION (metadata.txt-driven, no auto-bump): ``<version>`` in ``plugins.xml``
and the zip's ``metadata.txt`` are the SAME ``version=`` line straight from
``qgis-plugin/trid3nt/metadata.txt`` -- that agreement is what Plugin Manager
compares to decide "installed" vs "available". A landing that changes plugin
code is expected to bump ``version=``; ``package_plugin_repo`` compares the
packaged tree's content hash against the previous manifest and WARNS (never
auto-bumps, never fails) when the tree changed but the version did not, so a
forgotten bump is caught at deploy time.

Every public function is SYNC (subprocess + filesystem + zipfile); the HTTP
callers wrap them in ``asyncio.to_thread`` -- no sync work belongs on the
agent's event loop (it would stall the WS keepalive).
"""

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

#: Fixed-name route the FRESH zip is served at (see module docstring). The
#: literal string in ``tool_catalog_http.py``'s route dispatch MUST match
#: this -- it is duplicated there rather than imported to follow that
#: module's existing per-branch literal-path convention.
FRESH_ZIP_URL_PATH = "/plugin-repo/trid3nt.zip"

#: Files/dirs never carried into the zip (caches, hidden files, and the
#: dev-loop marker ``install_plugin.sh`` drops into an installed profile).
_ZIP_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".*", "installed_version.txt"
)
_TREE_HASH_EXCLUDE_NAMES = {"installed_version.txt"}


class PluginRepoBuildError(Exception):
    """The plugin repository could not be packaged or served (no source tree
    on this checkout, a filesystem fault while staging/zipping, or a serve
    request before the repo was ever packaged)."""


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """The daemon's OWN checkout root (the directory containing
    ``qgis-plugin/``). Default: derived from this file's location
    (``server/src/trid3nt_server/plugin_repo.py`` -> three parents up).
    Override via ``TRID3NT_REPO_ROOT`` (tests; also covers an installed-package
    layout where the source-tree-relative walk would be wrong).
    """
    env = os.environ.get("TRID3NT_REPO_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


def _plugin_src_dir(repo_root: Path) -> Path:
    return repo_root / "qgis-plugin" / PLUGIN_NAME


def _served_dir(served_dir: Path | str | None = None) -> Path:
    """The directory the packaged zip + ``plugins.xml`` + ``manifest.json``
    live in. Default ``<repo_root>/run/plugin-repo`` -- ``run/`` is already the
    repo's convention for gitignored service-owned state. Override via the
    ``served_dir`` argument (tests) or ``TRID3NT_PLUGIN_REPO_DIR``.
    """
    if served_dir is not None:
        return Path(served_dir).expanduser().resolve()
    env = os.environ.get("TRID3NT_PLUGIN_REPO_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return _repo_root() / "run" / "plugin-repo"


# ---------------------------------------------------------------------------
# metadata.txt + tree hash
# ---------------------------------------------------------------------------


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
    """Every file that belongs in a packaged trid3nt zip, in sorted-relpath
    order -- the one exclude rule (``__pycache__``, ``.pyc``, hidden files,
    the installed-version marker) shared by ``_tree_sha``, the fresh-zip
    mtime signature, and the in-memory fresh-build zip itself. (``_build_zip``
    -- the deploy-time PACKAGE path -- expresses the same rule as a
    ``shutil.ignore_patterns`` for ``copytree`` instead; kept separate since
    it walks a different way.)
    """
    for item in sorted(plugin_src.rglob("*")):
        if not item.is_file():
            continue
        parts = item.relative_to(plugin_src).parts
        if any(p == "__pycache__" or p.startswith(".") for p in parts):
            continue
        if item.suffix == ".pyc" or item.name in _TREE_HASH_EXCLUDE_NAMES:
            continue
        yield item


def _tree_sha(plugin_src: Path) -> str:
    """A stable content hash of the packaged plugin tree.

    Hashes every packaged file (see ``_iter_packaged_files``) as
    ``<relpath>\\0<bytes>``, in sorted-relpath order. Used only for the
    deploy-time version-drift warning -- two byte-identical trees hash the
    same regardless of filesystem mtimes.
    """
    h = hashlib.sha256()
    for item in _iter_packaged_files(plugin_src):
        rel = item.relative_to(plugin_src).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(item.read_bytes())
    return h.hexdigest()


def _source_signature(plugin_src: Path) -> tuple[tuple[str, int, int], ...]:
    """Cheap (stat-only, no file reads) signature of the packaged tree, used
    to invalidate the fresh-zip cache: ``(relpath, size, mtime_ns)`` per
    packaged file (see ``_iter_packaged_files``), sorted-relpath order. Unlike
    ``_tree_sha`` this never reads file bytes -- it is meant to run on every
    request."""
    entries = []
    for item in _iter_packaged_files(plugin_src):
        st = item.stat()
        entries.append((item.relative_to(plugin_src).as_posix(), st.st_size, st.st_mtime_ns))
    return tuple(entries)


# ---------------------------------------------------------------------------
# Zip build (mirrors the repo-root ``make plugin-zip`` shape: a top-level
# ``trid3nt/`` dir, LICENSE copied inside it, caches/hidden files excluded).
# ---------------------------------------------------------------------------


def _build_zip(repo_root: Path, plugin_src: Path, dest_zip: Path) -> None:
    if not plugin_src.is_dir():
        raise PluginRepoBuildError(f"plugin source tree not found: {plugin_src}")

    staging_root = dest_zip.parent / "_staging"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_plugin = staging_root / PLUGIN_NAME
    try:
        shutil.copytree(plugin_src, staging_plugin, ignore=_ZIP_IGNORE)
        license_src = repo_root / "qgis-plugin" / "LICENSE"
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
    """In-memory build for :func:`build_fresh_zip` -- same top-level
    ``trid3nt/`` layout + LICENSE + excludes as ``_build_zip``, plus a
    build-time provenance stamp (``trid3nt/installed_version.txt``, git sha +
    branch in the same two-line format ``scripts/install_plugin.sh`` writes)
    that the deploy-time PACKAGE zip deliberately excludes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in _iter_packaged_files(plugin_src):
            arcname = f"{PLUGIN_NAME}/{item.relative_to(plugin_src).as_posix()}"
            zf.write(item, arcname)
        license_src = repo_root / "qgis-plugin" / "LICENSE"
        if license_src.is_file():
            zf.write(license_src, f"{PLUGIN_NAME}/LICENSE")
        sha, branch = _git_provenance(repo_root)
        zf.writestr(f"{PLUGIN_NAME}/installed_version.txt", f"{sha}\n{branch}\n")
    return buf.getvalue()


#: In-memory fresh-zip cache: ``str(plugin_src) -> (signature, zip_bytes,
#: version, zip_filename)``. One entry in practice (one plugin source tree
#: per daemon process); keyed by path anyway so tests that swap
#: ``TRID3NT_REPO_ROOT`` never see another test's bytes.
_fresh_zip_cache: dict[str, tuple[Any, bytes, str, str]] = {}
_fresh_zip_lock = threading.Lock()


def build_fresh_zip(repo_root: Path | None = None) -> tuple[bytes, str, str]:
    """SYNC -- build (or reuse a cached) plugin zip straight from the source
    tree ``qgis-plugin/trid3nt/``. See the module docstring's FRESH ZIP
    section. No prior ``package_plugin_repo()`` call is required; every call
    re-stats the source tree (cheap -- ``_source_signature`` never reads file
    bytes) and only re-zips when a file's size or mtime actually changed, so
    a hot source edit is served on the very next request.

    Returns ``(zip_bytes, version, zip_filename)`` -- ``zip_filename`` is the
    versioned display name (``trid3nt-<version>.zip``) for
    Content-Disposition; the served URL itself is the fixed
    :data:`FRESH_ZIP_URL_PATH`.

    Raises :class:`PluginRepoBuildError` when there is no source tree on this
    checkout (mirrors ``package_plugin_repo``).
    """
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


# ---------------------------------------------------------------------------
# plugins.xml
# ---------------------------------------------------------------------------


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
    """Render the QGIS plugin-repository index XML for ``plugin_src``.

    Pure/deterministic: ``version`` (metadata-driven, byte-identical to the
    zip's ``metadata.txt version=``), ``file_name`` (the versioned zip name),
    and ``download_url`` are all supplied by the caller so the same shape is
    reused at package time (with the :data:`HOST_SENTINEL` host) and readable
    back at serve time.
    """
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


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# PACKAGE (deploy time)
# ---------------------------------------------------------------------------


def package_plugin_repo(served_dir: Path | str | None = None) -> dict[str, Any]:
    """SYNC (filesystem copy/zip) -- deploy-time entrypoint.

    Rebuilds the served plugin repository from the daemon's own checkout: the
    versioned zip ``trid3nt-<version>.zip``, a ``plugins.xml`` carrying the
    :data:`HOST_SENTINEL` host, and a ``manifest.json``. Old
    ``trid3nt-*.zip`` are removed so exactly one artifact is served.

    Version-drift warning: when the previous manifest's version equals this
    one but the packaged tree's content hash differs, logs a WARNING (a code
    change shipped without a ``version=`` bump, so Plugin Manager would not
    offer the update). Never auto-bumps, never fails on drift.

    Returns ``{"version", "zip_filename", "tree_sha", "warned", "served_dir"}``.
    """
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
            "qgis-plugin/trid3nt/metadata.txt.",
            version,
        )

    zip_filename = f"{PLUGIN_NAME}-{version}.zip"
    for stale in dest.glob(f"{PLUGIN_NAME}-*.zip"):
        if stale.name != zip_filename:
            stale.unlink()
    _build_zip(repo_root, plugin_src, dest / zip_filename)

    # download_url points at the FIXED fresh-build endpoint (not this
    # versioned zip_filename -- that artifact is still written to `dest` as a
    # manual-QA/fallback path, see served_zip_path) so Plugin Manager never
    # depends on this deploy-time packaging step having run; ?v= is a pure
    # cache-busting hint, see module docstring.
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


# ---------------------------------------------------------------------------
# SERVE (per request)
# ---------------------------------------------------------------------------


def render_plugins_xml(host: str, served_dir: Path | str | None = None) -> bytes:
    """SYNC -- read the packaged ``plugins.xml`` and fill its download_url host.

    Substitutes the per-request ``host`` (``host:port`` from the request's own
    Host header) for the :data:`HOST_SENTINEL`, so a client reaches the zip on
    the same host it dialed the index on. Raises :class:`PluginRepoBuildError`
    when the repo was never packaged (``scripts/package_plugin.sh`` /
    ``make agent`` not yet run).
    """
    xml_path = _served_dir(served_dir) / "plugins.xml"
    if not xml_path.is_file():
        raise PluginRepoBuildError(
            "plugin repo not packaged yet -- run scripts/package_plugin.sh "
            "(or make agent)"
        )
    body = xml_path.read_text(encoding="utf-8")
    return body.replace(HOST_SENTINEL, host).encode("utf-8")


def served_zip_path(zip_filename: str, served_dir: Path | str | None = None) -> Path:
    """SYNC -- resolve a ``GET /plugin-repo/<zip>`` filename to a real file.

    Path-traversal safe: the filename may carry no directory separators and
    must end in ``.zip``. Raises :class:`FileNotFoundError` when the named zip
    is not in the served directory (the route maps that to 404).
    """
    name = zip_filename.strip()
    if not name.endswith(".zip") or "/" in name or "\\" in name or name.startswith("."):
        raise FileNotFoundError(zip_filename)
    path = _served_dir(served_dir) / name
    if not path.is_file():
        raise FileNotFoundError(zip_filename)
    return path


# ---------------------------------------------------------------------------
# /api/version (unrelated to the repo -- the daemon git-sha + provider probe)
# ---------------------------------------------------------------------------


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
    """Short git sha + branch for ``repo_root``, the same two values
    ``scripts/install_plugin.sh`` stamps into an rsync-installed profile's
    ``installed_version.txt`` -- ``"unknown"`` for either when this checkout
    is not a git repo (matches that script's own fallback). Used by
    :func:`_build_zip_bytes` to stamp the same file into the fresh-build zip.
    """
    head = _git_head_sha(repo_root)
    sha = head[:7] if head != "unknown" else "unknown"
    branch = _run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    return sha, branch


def build_version_payload() -> dict[str, Any]:
    """SYNC (git subprocess) -- caller wraps in ``asyncio.to_thread``.

    The tiny version indicator ``/api/version`` serves: ``{"git_sha": <short
    sha>, "provider": <active MODEL_PROVIDER>}``. Both degrade to ``"unknown"``
    rather than raising -- a cheap discovery endpoint, never worth a 500.
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
