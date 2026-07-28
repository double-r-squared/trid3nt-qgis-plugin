"""Qt harness: SettingsDialog Update section wiring (v1 UPDATE button, NATE).

Runs under the ``qgis.PyQt`` interpreter (offscreen). Builds a real temp git
repo (one commit, no remote) so ``update.read_repo_head`` is exercised for
real, monkeypatches ``update.installed_plugin_dir`` to a temp dir carrying a
DELIBERATELY mismatched ``installed_version.txt`` (proves drift detection
renders), then:

  * waits for the version indicator to settle (installed + repo HEAD read
    synchronously; the daemon probe lands off-thread) -> asserts "DRIFT" is
    shown -> prints ``VERSION_DRIFT_OK``;
  * clicks Update. The temp repo has no git remote, so ``git fetch`` fails
    deterministically (no network needed) -- the ff-only guard aborts, the
    log shows the FAILED step, install_plugin.sh/reloadPlugin are never
    attempted, and the button re-enables -> prints ``UPDATE_ABORT_OK``;
  * asserts the repo path typed into the field was persisted to
    ``PluginSettings`` immediately (not gated on Save) -> prints
    ``REPO_PATH_PERSIST_OK``.

Exits non-zero on any mismatch.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

import trid3nt.update as upd  # noqa: E402
from trid3nt.ui.settings_dialog import SettingsDialog  # noqa: E402
from trid3nt.plugin_settings import PluginSettings  # noqa: E402


def _wait(app, predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _make_repo() -> str:
    repo_dir = tempfile.mkdtemp(prefix="trid3nt_update_harness_repo_")
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-C", repo_dir, *args], check=True, capture_output=True, text=True
    )
    run("init", "-q", "-b", "main")
    run("config", "user.email", "harness@example.com")
    run("config", "user.name", "harness")
    with open(os.path.join(repo_dir, "f.txt"), "w") as fh:
        fh.write("x")
    run("add", "f.txt")
    run("commit", "-q", "-m", "init")
    return repo_dir


def main() -> int:
    app = QApplication(sys.argv)

    from qgis.PyQt.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.IniFormat)
    QApplication.setOrganizationName("trid3nt-test")
    QApplication.setApplicationName("update-harness")

    repo_dir = _make_repo()
    plugin_dir = tempfile.mkdtemp(prefix="trid3nt_update_harness_plugin_")
    # Deliberately WRONG sha -- proves drift renders (never a false "match").
    with open(os.path.join(plugin_dir, upd.INSTALLED_VERSION_FILENAME), "w") as fh:
        fh.write("0000000\nmain\n")
    upd.installed_plugin_dir = lambda: plugin_dir  # module-level monkeypatch

    try:
        settings = PluginSettings()
        settings.repo_path = repo_dir
        settings.export_api = "http://127.0.0.1:1"  # unreachable, fast fail

        dlg = SettingsDialog(settings, None)

        if not _wait(app, lambda: "probing..." not in dlg.version_label.text()):
            print("VERSION_SETTLE_FAIL", dlg.version_label.text())
            return 1
        label_text = dlg.version_label.text()
        if "DRIFT" not in label_text or "0000000" not in label_text:
            print("VERSION_DRIFT_FAIL", label_text)
            return 1
        print("VERSION_DRIFT_OK")

        # Repo path field differs from the persisted settings value -- Update
        # must persist it immediately (action semantics, not gated on Save).
        other_repo_dir = _make_repo()
        dlg.repo_path_edit.setText(other_repo_dir)
        dlg.update_btn.click()

        if not _wait(app, lambda: dlg.update_btn.isEnabled()):
            print("UPDATE_FLOW_TIMEOUT", dlg.update_log.toPlainText())
            return 1
        log_text = dlg.update_log.toPlainText()
        if "FAILED" not in log_text or "git fetch" not in log_text:
            print("UPDATE_ABORT_FAIL", log_text)
            return 1
        if "reloadPlugin" in log_text:
            print("UPDATE_ABORT_FAIL unexpected reload attempt", log_text)
            return 1
        print("UPDATE_ABORT_OK")

        if settings.repo_path != other_repo_dir:
            print("REPO_PATH_PERSIST_FAIL", settings.repo_path)
            return 1
        print("REPO_PATH_PERSIST_OK")

        shutil.rmtree(other_repo_dir, ignore_errors=True)
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)
        shutil.rmtree(plugin_dir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
