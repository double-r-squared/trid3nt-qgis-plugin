"""Tests for the v1 UPDATE button (NATE): version-stamp read, drift
detection, the fast-forward-only pull guard, install_plugin.sh execution,
and the daemon-reachability probe (``trid3nt/update.py`` -- pure Python,
stdlib only, no QGIS required for the tests below the Qt-wiring class).

The ff-only guard tests mock ``subprocess.run`` (per the feature spec: "the
ff-only guard logic (subprocess calls mocked)") so no real git process runs
for the abort-path assertions; ``read_repo_head`` instead uses a REAL tiny
local git repo (init + one commit, no network) since exercising the actual
``git rev-parse`` plumbing is cheap and more meaningful than mocking it.

The Qt dock-wiring harness (version-indicator drift render + Update-button
click -> streamed log -> re-enable) runs in a subprocess under the
``qgis.PyQt`` interpreter and skips honestly when absent (mirrors
``test_provider_config.py``'s ``TestDockProviderConfigWiring``).
"""

from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from trid3nt import update as upd  # noqa: E402


# --------------------------------------------------------------------------- #
# read_installed_version
# --------------------------------------------------------------------------- #


class TestReadInstalledVersion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trid3nt_update_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_missing_file_is_honest_unknown(self):
        result = upd.read_installed_version(self.tmp)
        self.assertFalse(result.ok)
        self.assertIn("installed_version.txt", result.error)
        self.assertEqual(result.sha, "")

    def test_empty_file_is_honest_unknown(self):
        path = os.path.join(self.tmp, upd.INSTALLED_VERSION_FILENAME)
        with open(path, "w") as fh:
            fh.write("")
        result = upd.read_installed_version(self.tmp)
        self.assertFalse(result.ok)

    def test_well_formed_stamp_parses_sha_and_branch(self):
        path = os.path.join(self.tmp, upd.INSTALLED_VERSION_FILENAME)
        with open(path, "w") as fh:
            fh.write("a1b2c3d\nrefactor/engine-doors\n")
        result = upd.read_installed_version(self.tmp)
        self.assertTrue(result.ok)
        self.assertEqual(result.sha, "a1b2c3d")
        self.assertEqual(result.branch, "refactor/engine-doors")

    def test_sha_only_no_branch_line_still_parses(self):
        path = os.path.join(self.tmp, upd.INSTALLED_VERSION_FILENAME)
        with open(path, "w") as fh:
            fh.write("a1b2c3d\n")
        result = upd.read_installed_version(self.tmp)
        self.assertTrue(result.ok)
        self.assertEqual(result.sha, "a1b2c3d")
        self.assertEqual(result.branch, "")


# --------------------------------------------------------------------------- #
# read_repo_head -- real tiny local git repo, no network
# --------------------------------------------------------------------------- #


def _init_repo(path: str) -> str:
    """git init + one commit; returns the short sha of HEAD."""
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-C", path, *args], check=True, capture_output=True, text=True
    )
    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "test")
    with open(os.path.join(path, "f.txt"), "w") as fh:
        fh.write("x")
    run("add", "f.txt")
    run("commit", "-q", "-m", "init")
    return run("rev-parse", "--short", "HEAD").stdout.strip()


class TestReadRepoHead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trid3nt_update_test_repo_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_real_repo_returns_matching_sha_and_branch(self):
        sha = _init_repo(self.tmp)
        result = upd.read_repo_head(self.tmp)
        self.assertTrue(result.ok)
        self.assertEqual(result.sha, sha)
        self.assertEqual(result.branch, "main")

    def test_nonexistent_path_is_honest_unknown(self):
        result = upd.read_repo_head(os.path.join(self.tmp, "does-not-exist"))
        self.assertFalse(result.ok)
        self.assertIn("not found", result.error)

    def test_non_git_directory_is_honest_unknown(self):
        result = upd.read_repo_head(self.tmp)  # dir exists, never git-init'd
        self.assertFalse(result.ok)

    def test_empty_path_is_honest_unknown(self):
        result = upd.read_repo_head("")
        self.assertFalse(result.ok)


# --------------------------------------------------------------------------- #
# compare_versions -- drift detection
# --------------------------------------------------------------------------- #


class TestCompareVersions(unittest.TestCase):
    def test_matching_sha_is_match(self):
        a = upd.VersionInfo(sha="abc1234", branch="main", ok=True)
        b = upd.VersionInfo(sha="abc1234", branch="main", ok=True)
        self.assertEqual(upd.compare_versions(a, b), "match")

    def test_differing_sha_is_drift(self):
        a = upd.VersionInfo(sha="abc1234", branch="main", ok=True)
        b = upd.VersionInfo(sha="def5678", branch="main", ok=True)
        self.assertEqual(upd.compare_versions(a, b), "drift")

    def test_installed_unreadable_is_unknown_never_match(self):
        a = upd.VersionInfo(ok=False, error="no stamp")
        b = upd.VersionInfo(sha="def5678", branch="main", ok=True)
        self.assertEqual(upd.compare_versions(a, b), "unknown")

    def test_repo_unreadable_is_unknown_never_match(self):
        a = upd.VersionInfo(sha="abc1234", branch="main", ok=True)
        b = upd.VersionInfo(ok=False, error="not a git repo")
        self.assertEqual(upd.compare_versions(a, b), "unknown")

    def test_both_unreadable_is_unknown(self):
        a = upd.VersionInfo(ok=False)
        b = upd.VersionInfo(ok=False)
        self.assertEqual(upd.compare_versions(a, b), "unknown")


# --------------------------------------------------------------------------- #
# git_fetch_and_ff_pull -- ff-only guard, subprocess mocked
# --------------------------------------------------------------------------- #


class TestGitFetchAndFfPull(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trid3nt_update_test_pull_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _mock_run(self, responses: dict):
        """``responses`` maps the git subcommand's first arg (e.g. "status",
        "fetch", "pull") to (returncode, stdout, stderr). Unlisted
        subcommands return a bland ok(0) so unrelated calls never crash the
        test."""

        def _side_effect(cmd, **kwargs):
            # cmd = ["git", "-C", repo, subcommand, ...]
            subcommand = cmd[3] if len(cmd) > 3 else ""
            rc, out, err = responses.get(subcommand, (0, "", ""))
            proc = mock.Mock()
            proc.returncode = rc
            proc.stdout = out
            proc.stderr = err
            return proc

        return _side_effect

    def test_dirty_tree_aborts_before_fetch_or_pull(self):
        calls = []

        def _side_effect(cmd, **kwargs):
            subcommand = cmd[3] if len(cmd) > 3 else ""
            calls.append(subcommand)
            if subcommand == "status":
                proc = mock.Mock()
                proc.returncode = 0
                proc.stdout = " M dirty_file.py\n"
                proc.stderr = ""
                return proc
            raise AssertionError(f"unexpected git call after dirty-tree abort: {cmd}")

        with mock.patch("trid3nt.update.subprocess.run", side_effect=_side_effect):
            result = upd.git_fetch_and_ff_pull(self.tmp)
        self.assertFalse(result.ok)
        self.assertIn("dirty", result.message)
        self.assertNotIn("fetch", calls)
        self.assertNotIn("pull", calls)

    def test_non_ff_pull_aborts_honestly_never_forces(self):
        side_effect = self._mock_run(
            {
                "status": (0, "", ""),
                "rev-parse": (0, "main", ""),
                "fetch": (0, "", ""),
                "pull": (1, "", "fatal: Not possible to fast-forward, aborting."),
            }
        )
        with mock.patch("trid3nt.update.subprocess.run", side_effect=side_effect):
            result = upd.git_fetch_and_ff_pull(self.tmp)
        self.assertFalse(result.ok)
        self.assertIn("fast-forward", result.message)

    def test_missing_repo_path_aborts(self):
        result = upd.git_fetch_and_ff_pull(os.path.join(self.tmp, "nope"))
        self.assertFalse(result.ok)
        self.assertIn("not found", result.message)

    def test_happy_path_reports_branch_and_output(self):
        side_effect = self._mock_run(
            {
                "status": (0, "", ""),
                "rev-parse": (0, "refactor/engine-doors", ""),
                "fetch": (0, "", ""),
                "pull": (0, "Updating a1b2c3d..d4e5f6a\nFast-forward", ""),
            }
        )
        with mock.patch("trid3nt.update.subprocess.run", side_effect=side_effect):
            result = upd.git_fetch_and_ff_pull(self.tmp)
        self.assertTrue(result.ok)
        self.assertIn("refactor/engine-doors", result.message)
        self.assertIn("Fast-forward", result.message)

    def test_git_not_found_is_honest_abort(self):
        with mock.patch(
            "trid3nt.update.subprocess.run", side_effect=FileNotFoundError("no git")
        ):
            result = upd.git_fetch_and_ff_pull(self.tmp)
        self.assertFalse(result.ok)
        self.assertIn("git not found", result.message)

    def test_git_timeout_is_honest_abort(self):
        with mock.patch(
            "trid3nt.update.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=15),
        ):
            result = upd.git_fetch_and_ff_pull(self.tmp)
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.message)


# --------------------------------------------------------------------------- #
# run_install_script -- real tiny shell scripts, no network
# --------------------------------------------------------------------------- #


class TestRunInstallScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trid3nt_update_test_install_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "scripts"))

    def _write_script(self, body: str) -> None:
        path = os.path.join(self.tmp, "scripts", "install_plugin.sh")
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def test_missing_script_is_honest_failure(self):
        result = upd.run_install_script(self.tmp)
        self.assertFalse(result.ok)
        self.assertIn("not found", result.message)

    def test_success_exit_zero_captures_stdout(self):
        self._write_script("#!/usr/bin/env bash\necho synced-ok\n")
        result = upd.run_install_script(self.tmp)
        self.assertTrue(result.ok)
        self.assertIn("synced-ok", result.message)

    def test_nonzero_exit_is_honest_failure(self):
        self._write_script("#!/usr/bin/env bash\necho boom >&2\nexit 3\n")
        result = upd.run_install_script(self.tmp)
        self.assertFalse(result.ok)
        self.assertIn("boom", result.message)


# --------------------------------------------------------------------------- #
# probe_daemon
# --------------------------------------------------------------------------- #


class _ProbeStub(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):  # noqa: N802
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


class TestProbeDaemon(unittest.TestCase):
    def test_reachable_200_reports_reachable(self):
        httpd = http.server.HTTPServer(("127.0.0.1", 0), _ProbeStub)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        result = upd.probe_daemon(base, timeout=5.0)
        self.assertTrue(result.reachable)
        self.assertEqual(result.version, "unknown")  # no version header today

    def test_unreachable_port_reports_unreachable(self):
        result = upd.probe_daemon("http://127.0.0.1:1", timeout=2.0)
        self.assertFalse(result.reachable)
        self.assertTrue(result.error)

    def test_empty_base_is_honest_unreachable(self):
        result = upd.probe_daemon("", timeout=2.0)
        self.assertFalse(result.reachable)


# --------------------------------------------------------------------------- #
# Qt dock wiring (subprocess under the qgis.PyQt interpreter)
# --------------------------------------------------------------------------- #


def _qt_python() -> str | None:
    candidates = []
    which = shutil.which("python3")
    if which:
        candidates.append(which)
    candidates.append("/usr/bin/python3")
    for py in dict.fromkeys(candidates):
        if not os.path.exists(py):
            continue
        try:
            probe = subprocess.run(
                [py, "-c", "from qgis.PyQt.QtCore import QCoreApplication"],
                capture_output=True,
                timeout=60,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return py
    return None


class TestDockUpdateWiring(unittest.TestCase):
    def test_version_drift_render_and_update_abort_flow(self):
        py = _qt_python()
        if py is None:
            self.skipTest("no interpreter with qgis.PyQt")
        harness = os.path.join(os.path.dirname(__file__), "qt_update_harness.py")
        proc = subprocess.run(
            [py, harness],
            capture_output=True,
            timeout=120,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, msg=f"harness failed:\n{out}\n{err}")
        self.assertIn("VERSION_DRIFT_OK", out, msg=out)
        self.assertIn("UPDATE_ABORT_OK", out, msg=out)
        self.assertIn("REPO_PATH_PERSIST_OK", out, msg=out)


if __name__ == "__main__":
    unittest.main()
