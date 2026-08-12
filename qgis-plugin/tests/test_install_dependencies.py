"""``install_dependencies.py`` -- the plugin's general third-party dependency
installer (matplotlib was the QGIS-4 gap that prompted it; NATE wants the
general answer, so this covers the check/install/re-verify/report logic and
the self-enforcing sweep that keeps ``DEPENDENCIES`` honest as the source
changes).

Three parts:
* ``TestDependencySweep`` -- the self-enforcing AST sweep: a third-party
  import in the plugin source that is not in ``DEPENDENCIES`` fails this,
  same pattern as the 0225 fetcher sweep.
* ``TestCheckAndReport`` / ``TestInstallMissing`` -- the check/report/install
  logic with mocked imports and mocked ``subprocess.run`` (no real pip
  network calls in the test suite).
* ``TestMain`` -- the CLI (``--dry-run`` and the full run), exit codes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from trid3nt import install_dependencies as inst  # noqa: E402

PLUGIN_ROOT = Path(os.path.dirname(__file__)).resolve().parent / "trid3nt"


class TestDependencySweep(unittest.TestCase):
    """Self-enforcing: DEPENDENCIES must equal what the source actually
    imports beyond stdlib/qgis/PyQt/osgeo/processing."""

    def test_dependencies_list_matches_source_sweep(self):
        found = inst.scan_third_party_imports(PLUGIN_ROOT)
        listed = {name for name, _pip in inst.DEPENDENCIES}
        self.assertEqual(
            found,
            listed,
            f"DEPENDENCIES drift -- source imports {sorted(found)}, "
            f"list has {sorted(listed)}. Add the new import to DEPENDENCIES "
            "in install_dependencies.py (or remove a stale entry).",
        )

    def test_sweep_ignores_stdlib_and_platform_modules(self):
        pkg = Path(__file__).resolve().parent / "_sweep_fixture"
        pkg.mkdir(exist_ok=True)
        try:
            (pkg / "a.py").write_text(
                "import os\nimport sys\nfrom qgis.PyQt import QtCore\n"
                "from osgeo import gdal\nimport processing\n"
            )
            found = inst.scan_third_party_imports(pkg)
            self.assertEqual(found, frozenset())
        finally:
            (pkg / "a.py").unlink(missing_ok=True)
            pkg.rmdir()

    def test_sweep_catches_a_new_undeclared_import(self):
        pkg = Path(__file__).resolve().parent / "_sweep_fixture2"
        pkg.mkdir(exist_ok=True)
        try:
            (pkg / "b.py").write_text("import requests\n")
            found = inst.scan_third_party_imports(pkg)
            self.assertIn("requests", found)
            self.assertNotIn("requests", {n for n, _ in inst.DEPENDENCIES})
        finally:
            (pkg / "b.py").unlink(missing_ok=True)
            pkg.rmdir()

    def test_sweep_skips_relative_imports(self):
        pkg = Path(__file__).resolve().parent / "_sweep_fixture3"
        pkg.mkdir(exist_ok=True)
        try:
            (pkg / "c.py").write_text("from . import sibling\nfrom .. import other\n")
            found = inst.scan_third_party_imports(pkg)
            self.assertEqual(found, frozenset())
        finally:
            (pkg / "c.py").unlink(missing_ok=True)
            pkg.rmdir()


class TestCheckAndReport(unittest.TestCase):
    def test_check_dependency_present(self):
        self.assertIsNone(inst.check_dependency("os"))

    def test_check_dependency_missing_reports_reason(self):
        err = inst.check_dependency("definitely_not_a_real_module_xyz")
        self.assertIsNotNone(err)
        self.assertIn("ModuleNotFoundError", err)

    def test_check_all_present_and_missing(self):
        deps = [("os", "n/a"), ("definitely_not_a_real_module_xyz", "fakepkg")]
        statuses = inst.check_all(deps)
        self.assertTrue(statuses[0].present)
        self.assertFalse(statuses[1].present)
        self.assertIsNotNone(statuses[1].error)

    def test_format_table_flags_missing(self):
        deps = [("os", "n/a"), ("definitely_not_a_real_module_xyz", "fakepkg")]
        table = inst.format_table(inst.check_all(deps))
        self.assertIn("os", table)
        self.assertIn("present", table)
        self.assertIn("MISSING", table)

    def test_format_table_empty(self):
        self.assertIn("no dependencies", inst.format_table([]))


class TestInstallMissing(unittest.TestCase):
    """Mocked subprocess.run -- no real pip / network calls."""

    def test_nothing_to_install_when_all_present(self):
        statuses = inst.check_all([("os", "n/a")])
        with mock.patch("subprocess.run") as run:
            self.assertTrue(inst.install_missing(statuses, python_exe="/usr/bin/python3"))
            run.assert_not_called()

    def test_plain_install_success(self):
        statuses = [inst.DependencyStatus("matplotlib", "matplotlib", False, "boom")]
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            ok = inst.install_missing(statuses, python_exe="/usr/bin/python3")
        self.assertTrue(ok)
        run.assert_called_once()
        cmd = run.call_args[0][0]
        self.assertEqual(
            cmd, ["/usr/bin/python3", "-m", "pip", "install", "matplotlib"]
        )

    def test_falls_back_to_user_flag_on_failure(self):
        statuses = [inst.DependencyStatus("matplotlib", "matplotlib", False, "boom")]
        results = [subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)]
        with mock.patch("subprocess.run", side_effect=results) as run:
            ok = inst.install_missing(statuses, python_exe="/usr/bin/python3")
        self.assertTrue(ok)
        self.assertEqual(run.call_count, 2)
        second_cmd = run.call_args_list[1][0][0]
        self.assertIn("--user", second_cmd)

    def test_reports_failure_when_both_attempts_fail(self):
        statuses = [inst.DependencyStatus("matplotlib", "matplotlib", False, "boom")]
        with mock.patch(
            "subprocess.run", return_value=subprocess.CompletedProcess([], 1)
        ) as run:
            ok = inst.install_missing(statuses, python_exe="/usr/bin/python3")
        self.assertFalse(ok)
        self.assertEqual(run.call_count, 2)

    def test_launch_failure_is_honest_not_raised(self):
        statuses = [inst.DependencyStatus("matplotlib", "matplotlib", False, "boom")]
        with mock.patch("subprocess.run", side_effect=OSError("no such file")):
            ok = inst.install_missing(statuses, python_exe="/nonexistent/python")
        self.assertFalse(ok)


class TestInstallPythonExecutable(unittest.TestCase):
    """Mirrors trid3nt.ui.charts' own coverage -- this is the shared
    implementation ``charts`` now delegates to."""

    def test_mac_derives_from_exec_prefix_bin_python3(self):
        py = inst.install_python_executable(
            "darwin",
            "/Applications/QGIS.app/Contents/MacOS",
            "/Applications/QGIS.app/Contents/MacOS/QGIS",
        )
        self.assertEqual(py, "/Applications/QGIS.app/Contents/MacOS/bin/python3")

    def test_linux_uses_running_interpreter(self):
        py = inst.install_python_executable("linux", "/usr", "/usr/bin/python3")
        self.assertEqual(py, "/usr/bin/python3")


class TestMain(unittest.TestCase):
    def test_dry_run_all_present_exits_zero(self):
        with mock.patch.object(
            inst, "check_all", return_value=[inst.DependencyStatus("os", "n/a", True)]
        ):
            code = inst.main(["--dry-run"])
        self.assertEqual(code, 0)

    def test_dry_run_missing_exits_nonzero_and_installs_nothing(self):
        with mock.patch.object(
            inst,
            "check_all",
            return_value=[
                inst.DependencyStatus("matplotlib", "matplotlib", False, "boom")
            ],
        ), mock.patch("subprocess.run") as run:
            code = inst.main(["--dry-run"])
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_full_run_installs_and_reverifies_success(self):
        missing = [inst.DependencyStatus("matplotlib", "matplotlib", False, "boom")]
        present = [inst.DependencyStatus("matplotlib", "matplotlib", True)]
        with mock.patch.object(
            inst, "check_all", side_effect=[missing, present]
        ), mock.patch.object(inst, "install_missing", return_value=True) as im:
            code = inst.main([])
        self.assertEqual(code, 0)
        im.assert_called_once()

    def test_full_run_still_missing_after_install_exits_nonzero(self):
        missing = [inst.DependencyStatus("matplotlib", "matplotlib", False, "boom")]
        with mock.patch.object(
            inst, "check_all", side_effect=[missing, missing]
        ), mock.patch.object(inst, "install_missing", return_value=False):
            code = inst.main([])
        self.assertEqual(code, 1)

    def test_full_run_nothing_missing_skips_install(self):
        present = [inst.DependencyStatus("os", "n/a", True)]
        with mock.patch.object(
            inst, "check_all", return_value=present
        ), mock.patch.object(inst, "install_missing") as im:
            code = inst.main([])
        self.assertEqual(code, 0)
        im.assert_not_called()


if __name__ == "__main__":
    unittest.main()
