"""TRID3NT UPDATE button (v1, local mode) -- pure Python, stdlib only.

Same hard rule as ``net/trid3nt_client.py``: NO PyQGIS / PyQt imports here, so
this module is importable and unit-testable with any plain CPython (the tests
run it under the trid3nt-local agent venv, outside QGIS entirely). The Qt
cross-thread wrapper lives in ``net/tasks.py`` (``_UpdateTask``); the UI lives
in ``ui/settings_dialog.py``.

Why this exists (NATE): the install-sync gap (scripts/install_plugin.sh's
docstring) means a fix landed in the repo is invisible to QGIS until someone
remembers to re-sync + reload. This module is the "am I current, and if not,
make me current" logic behind the Settings dialog's Update section:

  * ``read_installed_version`` -- reads the stamp ``install_plugin.sh`` writes
    into the INSTALLED profile copy at sync time (short sha + branch).
  * ``read_repo_head`` -- ``git rev-parse`` against the configured source
    checkout.
  * ``compare_versions`` -- "match" / "drift" / "unknown" (never claims a
    match it cannot prove).
  * ``probe_daemon`` -- best-effort agent-reachability probe; the daemon
    exposes no version today (see the docstring on ``probe_daemon``), so this
    is honest about that rather than fabricating a number.
  * ``git_fetch_and_ff_pull`` / ``run_install_script`` -- the two blocking
    steps of the Update button flow. Fast-forward-only, always: a dirty tree
    or a non-ff situation ABORTS with a typed, honest message -- never force,
    never stash.
"""
from __future__ import annotations

import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

#: Filename install_plugin.sh writes into the installed profile copy.
INSTALLED_VERSION_FILENAME = "installed_version.txt"

#: The installed profile copy is a disconnected COPY of the repo (QGIS loads
#: from ``~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/trid3nt``,
#: never the checkout -- see scripts/install_plugin.sh), so the source repo
#: path cannot be derived from ``__file__`` when running installed. This is
#: the known dev-machine path (README: "this plugin lives in the same repo as
#: that stack (trid3nt-local, the parent directory of this one)") -- a
#: sensible default; always editable in Settings (``PluginSettings.repo_path``).
DEFAULT_REPO_PATH = os.path.expanduser("~/Documents/trid3nt-local")

#: The plugin package/module name QGIS registers it under (the profile
#: plugins-dir folder name -- see scripts/install_plugin.sh's ``DST``, and
#: ``metadata.txt``'s ``[general] name`` is the display name, a different
#: thing). This is the argument ``qgis.utils.reloadPlugin`` expects.
PLUGIN_PACKAGE_NAME = "trid3nt"

GIT_TIMEOUT_S = 15.0
INSTALL_SCRIPT_TIMEOUT_S = 60.0
PROBE_TIMEOUT_S = 2.0


@dataclass
class VersionInfo:
    """One side's (installed / repo) short sha + branch. ``ok=False`` means
    the fact could not be established -- ``error`` says why, honestly; never
    a fabricated/blank sha standing in for "don't know"."""

    sha: str = ""
    branch: str = ""
    ok: bool = False
    error: str = ""

    @property
    def label(self) -> str:
        if not self.ok:
            return f"unknown ({self.error})" if self.error else "unknown"
        return f"{self.sha} ({self.branch or '?'})"


@dataclass
class DaemonProbeResult:
    """Best-effort agent-reachability probe result."""

    reachable: bool = False
    version: str = "unknown"
    error: str = ""

    @property
    def label(self) -> str:
        if self.reachable:
            return f"reachable, version {self.version}"
        return f"unreachable ({self.error})" if self.error else "unreachable"


@dataclass
class UpdateStepResult:
    """One step of the Update button flow (pull / install script)."""

    name: str
    ok: bool
    message: str


class GitAbort(Exception):
    """Typed abort for the ff-only guard and any other git step -- the guard
    NEVER forces or stashes; this is the only way a git step fails."""


# --------------------------------------------------------------------------- #
# Version-stamp + repo HEAD reads
# --------------------------------------------------------------------------- #


def installed_plugin_dir() -> str:
    """The directory this module's own ``.py`` file lives in -- identical
    whether that's the repo checkout (``qgis-plugin/trid3nt/``) or the
    installed profile copy (``.../plugins/trid3nt/``), since both are just
    "the trid3nt package root". This is where ``install_plugin.sh`` writes
    ``installed_version.txt``."""
    return os.path.dirname(os.path.abspath(__file__))


def read_installed_version(plugin_dir: str) -> VersionInfo:
    """Read the stamp ``install_plugin.sh`` writes at sync time. Format: two
    lines, ``<short sha>`` then ``<branch>``. Missing file (e.g. a dev
    checkout that has never been synced, or a pre-Update-button install) or a
    malformed/empty file both degrade honestly to ``ok=False`` -- never a
    guessed sha."""
    path = os.path.join(plugin_dir, INSTALLED_VERSION_FILENAME)
    if not os.path.isfile(path):
        return VersionInfo(ok=False, error="no installed_version.txt (re-run install_plugin.sh)")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = [line.strip() for line in fh.readlines()]
    except OSError as exc:
        return VersionInfo(ok=False, error=f"{type(exc).__name__}: {exc}")
    sha = lines[0] if len(lines) > 0 else ""
    branch = lines[1] if len(lines) > 1 else ""
    if not sha:
        return VersionInfo(ok=False, error="installed_version.txt is empty/malformed")
    return VersionInfo(sha=sha, branch=branch, ok=True)


def _run_git(repo_path: str, args: list, timeout: float = GIT_TIMEOUT_S) -> str:
    """Run ``git -C <repo_path> <args>``, returning stripped stdout. Raises
    ``GitAbort`` (typed, honest message) on a missing git binary, a timeout,
    or a non-zero exit -- never a bare traceback past the caller."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GitAbort(f"git not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitAbort(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise GitAbort((proc.stderr or proc.stdout or f"git {' '.join(args)} failed").strip())
    return proc.stdout.strip()


def read_repo_head(repo_path: str) -> VersionInfo:
    """``git rev-parse --short HEAD`` + ``--abbrev-ref HEAD`` against
    ``repo_path``. Honest ``ok=False`` when the path does not exist, is not a
    git repo, or git itself is unavailable/times out."""
    if not repo_path or not os.path.isdir(repo_path):
        return VersionInfo(ok=False, error=f"repo path not found: {repo_path!r}")
    try:
        sha = _run_git(repo_path, ["rev-parse", "--short", "HEAD"])
        branch = _run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    except GitAbort as exc:
        return VersionInfo(ok=False, error=str(exc))
    return VersionInfo(sha=sha, branch=branch, ok=True)


def compare_versions(installed: VersionInfo, repo: VersionInfo) -> str:
    """"match" / "drift" / "unknown" -- never claims a match it cannot prove:
    if either side could not be read, the comparison is "unknown", not a
    silent "match"."""
    if not installed.ok or not repo.ok:
        return "unknown"
    return "match" if installed.sha == repo.sha else "drift"


# --------------------------------------------------------------------------- #
# Daemon reachability probe
# --------------------------------------------------------------------------- #


def probe_daemon(http_base: str, timeout: float = PROBE_TIMEOUT_S) -> DaemonProbeResult:
    """Best-effort GET against the agent's existing ``/api/tool-catalog``
    route (cheap: it's already served for the catalog UI, no new server
    route needed). The hand-rolled HTTP server
    (``trid3nt_server/tool_catalog_http.py``) sends no ``Server``/version
    header today and the catalog payload carries no version field, so a
    reachable daemon honestly reports "version unknown" rather than
    fabricating one -- only reachability is provable here. If a future daemon
    build adds an ``X-Trid3nt-Version`` response header, it is picked up
    automatically."""
    if not http_base:
        return DaemonProbeResult(reachable=False, error="no daemon URL configured")
    url = http_base.rstrip("/") + "/api/tool-catalog"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            version = resp.headers.get("X-Trid3nt-Version") or "unknown"
            ok = 200 <= resp.status < 300
            return DaemonProbeResult(reachable=ok, version=version if ok else "unknown")
    except urllib.error.HTTPError as exc:
        # Reachable (a listener answered) but the route errored -- still
        # tells us the daemon is UP, just not the version.
        return DaemonProbeResult(reachable=True, version="unknown", error=f"HTTP {exc.code}")
    except urllib.error.URLError as exc:
        return DaemonProbeResult(reachable=False, error=str(exc.reason))
    except Exception as exc:  # noqa: BLE001 -- surfaced, never silent
        return DaemonProbeResult(reachable=False, error=f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# Update flow: (a) fetch + ff-only pull, (b) install_plugin.sh
# --------------------------------------------------------------------------- #


def git_is_dirty(repo_path: str) -> bool:
    status = _run_git(repo_path, ["status", "--porcelain"])
    return bool(status.strip())


def git_current_branch(repo_path: str) -> str:
    return _run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])


def git_fetch_and_ff_pull(repo_path: str) -> UpdateStepResult:
    """Step (a) of the Update flow: ``git fetch`` then a fast-forward-ONLY
    pull on the CURRENT branch.

    Aborts with no mutation (never force, never stash) when:
      * ``repo_path`` is not a directory / not a git repo;
      * the working tree is dirty (``git status --porcelain`` non-empty);
      * the pull cannot fast-forward (diverged history) -- ``git pull
        --ff-only`` itself refuses and exits non-zero, which we surface as a
        typed abort rather than retrying with a merge/rebase/force.
    """
    try:
        if not os.path.isdir(repo_path):
            raise GitAbort(f"repo path not found: {repo_path!r}")
        if git_is_dirty(repo_path):
            raise GitAbort(
                "working tree is dirty -- commit or discard changes before "
                "updating (never auto-stashed)"
            )
        branch = git_current_branch(repo_path)
        _run_git(repo_path, ["fetch"])
        output = _run_git(repo_path, ["pull", "--ff-only"])
    except GitAbort as exc:
        return UpdateStepResult("git fetch + pull --ff-only", False, str(exc))
    return UpdateStepResult(
        "git fetch + pull --ff-only",
        True,
        f"{branch}: {output or 'already up to date'}",
    )


def run_install_script(repo_path: str) -> UpdateStepResult:
    """Step (b): ``scripts/install_plugin.sh`` (sync source -> installed
    profile copy). Captures combined stdout+stderr as the honest message
    either way."""
    script = os.path.join(repo_path, "scripts", "install_plugin.sh")
    if not os.path.isfile(script):
        return UpdateStepResult("install_plugin.sh", False, f"script not found: {script}")
    try:
        proc = subprocess.run(
            [script],
            capture_output=True,
            text=True,
            timeout=INSTALL_SCRIPT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        return UpdateStepResult("install_plugin.sh", False, f"timed out: {exc}")
    except OSError as exc:
        return UpdateStepResult("install_plugin.sh", False, f"{type(exc).__name__}: {exc}")
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    ok = proc.returncode == 0
    return UpdateStepResult("install_plugin.sh", ok, output or f"exit {proc.returncode}")
