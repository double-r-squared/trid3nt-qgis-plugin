"""The installer moves an existing profile to the SDK-safe package name."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
QGIS_PYTHON = "/usr/bin/python3"
SETTINGS = """
import sys
from qgis.PyQt.QtCore import QSettings
settings = QSettings(sys.argv[1], QSettings.IniFormat)
"""


@pytest.mark.parametrize("enabled", [True, False])
def test_profile_install_migrates_legacy_settings_once(tmp_path, enabled):
    if not shutil.which("rsync") or subprocess.run(
        [QGIS_PYTHON, "-c", "from qgis.PyQt.QtCore import QSettings"],
        capture_output=True, check=False,
    ).returncode:
        pytest.skip("the Linux installer needs rsync and the system QGIS Python")
    profile = tmp_path / "profile"
    ini = profile / "QGIS" / "QGIS3.ini"
    legacy = profile / "python" / "plugins" / "trid3nt"
    legacy.mkdir(parents=True)
    (legacy / "old.py").touch()

    def qsettings(code):
        subprocess.run([QGIS_PYTHON, "-c", SETTINGS + code, str(ini)], check=True)

    qsettings(f"""
settings.setValue("trid3nt/token", "saved-test-value")
settings.setValue("trid3nt/nested/value", ["one", "two"])
settings.setValue("PythonPlugins/trid3nt", {enabled!r})
settings.sync()
""")
    env = {**os.environ, "TRID3NT_QGIS_PROFILE": str(profile)}
    install = [str(REPO / "scripts" / "install_plugin.sh")]
    subprocess.run([*install, "--check"], env=env, check=True, capture_output=True)
    assert legacy.is_dir()
    subprocess.run(install, env=env, check=True, capture_output=True)
    assert not legacy.exists()
    installed = legacy.with_name("trid3nt_qgis")
    assert "name=TRID3NT" in (installed / "metadata.txt").read_text()
    qsettings(f"""
assert settings.value("trid3nt_qgis/token") == "saved-test-value"
assert settings.value("trid3nt_qgis/nested/value") == ["one", "two"]
assert settings.value("PythonPlugins/trid3nt_qgis", type=bool) is {enabled!r}
assert not settings.contains("PythonPlugins/trid3nt")
settings.setValue("trid3nt_qgis/token", "changed-after-install")
settings.sync()
""")
    subprocess.run(install, env=env, check=True, capture_output=True)
    qsettings('assert settings.value("trid3nt_qgis/token") == "changed-after-install"')
