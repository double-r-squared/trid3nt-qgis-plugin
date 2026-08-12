"""Third-party dependency installer for the TRID3NT QGIS plugin.

SINGLE SOURCE OF TRUTH for "what does this plugin need beyond QGIS's own
Python + PyQt/qgis/osgeo/processing": the ``DEPENDENCIES`` list below. QGIS
4.0.3 bundles numpy, pandas, shapely, pyproj, lxml and psycopg2 (per NATE's
crash report); none of those are imported anywhere in ``qgis-plugin/trid3nt/``
today, so nothing needs installing for them. matplotlib is the one gap:
QGIS 3 bundled it, QGIS 4 dropped it. ``DEPENDENCIES`` is cross-checked
against a live AST sweep of the plugin source by
``tests/test_install_dependencies.py`` -- a future third-party import that
is not added here fails that test (same pattern as the 0225 fetcher sweep).

Platform split (NATE ground truth, live-verified on his macOS QGIS 4.0.3,
supersedes every earlier console/PYTHONPATH attempt in this file's history):

* Linux -- QGIS runs under the SYSTEM python3, which has pip. ``python3
  install_dependencies.py`` (or the plugin's Missing-matplotlib panel's
  ``python3 -m pip install matplotlib`` one-liner) just works.
* Windows -- the OSGeo4W python.exe QGIS ships (``windows_python_executable``
  below) has pip too. ``<that python.exe> install_dependencies.py`` works.
* macOS -- QGIS 4's bundled Python has NO pip module at all
  ("No module named pip", live-verified) -- not a PATH problem, not an env
  problem, pip is simply absent from the interpreter. There is no in-
  interpreter fix. NATE's ruling: treat the SYSTEM python3 as a pure
  wheel DOWNLOADER (``pip download --only-binary``, never ``pip install``,
  so it never touches QGIS's interpreter at all) and unzip the resulting
  wheels straight into ``<profile>/python`` -- a directory already on
  QGIS's own ``sys.path``, no interpreter of QGIS's own involved. See
  ``mac_wheel_recipe`` below; this is the ONLY macOS path this file offers.

Pure stdlib (no PyQt/qgis import anywhere in this file) so it runs two ways:

  (a) directly, with the QGIS interpreter (Linux/Windows only -- see above)::

      linux:   python3 install_dependencies.py   (already QGIS's interpreter)
      windows: <QGIS install>\\apps\\Python3xx\\python.exe install_dependencies.py

      On macOS, running this script AT ALL means you launched it with your
      system python3 (never QGIS's own, which lacks pip); ``main()`` detects
      that and prints the wheel-download recipe instead of attempting pip.

  (b) referenced by ``trid3nt.ui.charts``'s ``linux_install_command`` /
      ``windows_install_command`` / ``mac_wheel_recipe`` re-exports, which
      the plugin's ``MissingMatplotlibPanel`` displays as the copy-able
      fix for the running platform -- one source of truth for what gets
      installed and how. The panel never runs anything itself: every path
      here is a command for the USER to run in a real terminal.

Behavior (direct run, Linux/Windows): check each dependency importable,
print a present/missing table, pip-install only what's missing into the
running interpreter (``-m pip install``, falling back to ``--user`` if the
bundle's site-packages is not writable), re-verify the imports, print an
honest per-dependency summary, and exit nonzero if anything is still
missing. ``--dry-run`` prints the table and the command that would run
without installing anything. On macOS, both modes just print the wheel
recipe and return nonzero -- there is nothing this script can install
itself into.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

#: (import_name, pip_package_name) pairs -- the true third-party import
#: surface of qgis-plugin/trid3nt/. See module docstring for the bundled-vs-
#: must-install reasoning.
DEPENDENCIES: List[Tuple[str, str]] = [
    ("matplotlib", "matplotlib"),
]

#: Modules that are part of the QGIS/Qt platform, never pip-installed by us
#: (QGIS provides them) and never counted as "third-party" by the source
#: sweep below.
_PLATFORM_MODULES = frozenset({
    "qgis", "PyQt5", "PyQt6", "PyQt", "sip", "osgeo", "processing", "console",
})


# --------------------------------------------------------------------------- #
# Dependency presence check
# --------------------------------------------------------------------------- #


@dataclass
class DependencyStatus:
    import_name: str
    pip_name: str
    present: bool
    error: Optional[str] = None


def check_dependency(import_name: str) -> Optional[str]:
    """Attempt the import; return None on success, the error string on
    failure. Never raises."""
    try:
        importlib.import_module(import_name)
    except Exception as exc:  # noqa: BLE001 -- absence is a supported state
        return f"{type(exc).__name__}: {exc}"
    return None


def check_all(
    deps: Sequence[Tuple[str, str]] = (),
) -> List[DependencyStatus]:
    deps = deps or DEPENDENCIES
    return [
        DependencyStatus(
            import_name, pip_name, err is None, err
        )
        for import_name, pip_name in deps
        for err in (check_dependency(import_name),)
    ]


def format_table(statuses: Sequence[DependencyStatus]) -> str:
    if not statuses:
        return "(no dependencies to check)"
    name_w = max(len("dependency"), max(len(s.import_name) for s in statuses))
    rows = [f"{'dependency'.ljust(name_w)}  status"]
    rows.append(f"{'-' * name_w}  ------")
    for s in statuses:
        label = "present" if s.present else "MISSING"
        line = f"{s.import_name.ljust(name_w)}  {label}"
        if not s.present and s.error:
            line += f"  ({s.error})"
        rows.append(line)
    return "\n".join(rows)


# --------------------------------------------------------------------------- #
# Windows: OSGeo4W python.exe resolution (Linux needs none -- system python3
# already has pip; macOS never resolves a QGIS-side interpreter at all, see
# mac_wheel_recipe below).
# --------------------------------------------------------------------------- #


def _first_real_executable(candidates: Sequence[str]) -> Optional[str]:
    """First candidate that exists AND is executable (``os.X_OK``) -- never
    trust a derived-but-unverified path."""
    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def windows_python_executable(
    exec_prefix: Optional[str] = None, executable: Optional[str] = None,
) -> str:
    """The OSGeo4W python.exe pip ships with, on Windows -- ``exec_prefix/
    python.exe`` first (the QGIS install's own ``apps/PythonNN`` dir),
    falling back to the launcher's own directory's ``python.exe``. Verified
    real on disk before being returned; an honest 'could not locate'
    sentence, never a fabricated path, when neither probes real."""
    exec_prefix = sys.exec_prefix if exec_prefix is None else exec_prefix
    executable = sys.executable if executable is None else executable
    exe_dir = os.path.dirname(executable) if executable else ""
    candidates = [os.path.join(exec_prefix, "python.exe")]
    if exe_dir:
        candidates.append(os.path.join(exe_dir, "python.exe"))
    found = _first_real_executable(candidates)
    if found:
        return found
    return (
        "could not locate the QGIS python.exe -- find it with: "
        f'dir /s /b "{exec_prefix}\\python.exe"'
    )


def pip_install_command_str(python_exe: str, pip_names: Sequence[str]) -> str:
    """The human-facing / --dry-run command line for installing ``pip_names``
    with ``python_exe``."""
    quoted_py = f'"{python_exe}"' if " " in python_exe else python_exe
    return " ".join([quoted_py, "-m", "pip", "install", *pip_names])


def _run_pip(
    python_exe: str, pip_names: Sequence[str], extra_args: Sequence[str] = ()
) -> bool:
    """Run ``python_exe -m pip install <pip_names> <extra_args>`` with
    inherited stdio so a caller sees the pip output live. Returns True on a
    zero exit code."""
    cmd = [python_exe, "-m", "pip", "install", *pip_names, *extra_args]
    print(f"+ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd)
    except OSError as exc:
        print(f"failed to launch pip: {exc}")
        return False
    return proc.returncode == 0


def install_missing(
    statuses: Sequence[DependencyStatus], python_exe: Optional[str] = None
) -> bool:
    """pip-install every MISSING status's pip package into ``python_exe``
    (default: the running interpreter's own executable -- Linux/Windows
    only, this is never called on macOS). Tries a plain install first; on
    failure, retries with ``--user`` (the bundle's site-packages is
    commonly not writable without elevated privileges). Returns True iff
    the install command reported success -- callers must still re-verify
    by import (packages can install without becoming importable, e.g. an
    ABI mismatch)."""
    missing = [s for s in statuses if not s.present]
    if not missing:
        return True
    python_exe = python_exe or sys.executable
    pip_names = [s.pip_name for s in missing]
    if _run_pip(python_exe, pip_names):
        return True
    print(
        "plain install failed -- retrying with --user "
        "(the bundle's site-packages may not be writable)"
    )
    return _run_pip(python_exe, pip_names, extra_args=["--user"])


# --------------------------------------------------------------------------- #
# macOS: pip-download-as-wheel-fetcher recipe (NATE's ruling -- QGIS 4's
# bundled Python has no pip at all, so there is no interpreter to install
# INTO; the fix downloads prebuilt wheels with the system python3 and
# unzips them into the QGIS profile's own python/ dir, which is already on
# QGIS's sys.path).
# --------------------------------------------------------------------------- #


def python_version_tag(version_info=None) -> str:
    """``MAJOR.MINOR`` this interpreter reports (``sys.version_info``) --
    the ``pip download --python-version`` value. Passed explicitly so
    callers are testable without patching ``sys``."""
    version_info = sys.version_info if version_info is None else version_info
    return f"{version_info.major}.{version_info.minor}"


def mac_platform_tag(machine: Optional[str] = None) -> str:
    """The ``pip download --platform`` tag for this Mac: Apple Silicon
    (``platform.machine() == 'arm64'``) -> ``macosx_11_0_arm64``, else
    ``macosx_11_0_x86_64``. Derived from ``platform.machine()`` at call
    time, never hardcoded to one architecture."""
    machine = platform.machine() if machine is None else machine
    arch = "arm64" if machine == "arm64" else "x86_64"
    return f"macosx_11_0_{arch}"


def profile_python_dir(file: Optional[str] = None) -> str:
    """``<profile>/python`` -- the QGIS profile directory that is already
    on QGIS's own ``sys.path``. The plugin ships at
    ``<profile>/python/plugins/trid3nt/``, so this is two dirnames up from
    the package directory: this module lives directly inside ``trid3nt/``,
    so its own ``__file__`` needs exactly three ``dirname()`` calls to
    reach ``<profile>/python``."""
    file = __file__ if file is None else file
    package_dir = os.path.dirname(os.path.abspath(file))  # .../trid3nt
    return os.path.dirname(os.path.dirname(package_dir))  # .../python


#: NATE-verified scratch download dir -- disposable, the wheels are unzipped
#: out of it into the profile and never read again after that.
_MAC_WHEEL_DOWNLOAD_DIR = "/tmp/qgis_mpl"


def mac_wheel_recipe(
    pip_names: Sequence[str] = ("matplotlib",),
    python_version: Optional[str] = None,
    platform_tag: Optional[str] = None,
    profile_python: Optional[str] = None,
    file: Optional[str] = None,
) -> str:
    """NATE's verified macOS recipe: download prebuilt wheels with the
    SYSTEM python3 as a pure downloader (``--only-binary``, never touches
    QGIS's own interpreter, which has no pip to touch), then unzip them
    (a wheel IS a zip) straight into ``<profile>/python``. Every value is
    runtime-derived -- ``python-version`` from ``sys.version_info``,
    ``platform`` from ``platform.machine()``, the profile path from this
    module's own location -- never hardcoded to one QGIS install."""
    python_version = (
        python_version_tag() if python_version is None else python_version
    )
    platform_tag = mac_platform_tag() if platform_tag is None else platform_tag
    profile_python = (
        profile_python_dir(file) if profile_python is None else profile_python
    )
    names = " ".join(pip_names)
    download = (
        f"python3 -m pip download {names} --only-binary=:all: "
        f"--python-version {python_version} --platform {platform_tag} "
        f"--implementation cp -d {_MAC_WHEEL_DOWNLOAD_DIR}"
    )
    # QGIS bundles numpy; the downloaded numpy wheel must NOT reach the
    # profile -- it shadows the bundled copy and breaks shapely's ABI,
    # taking all of PyQGIS down at startup (NATE-verified live).
    drop_numpy = f"rm -f {_MAC_WHEEL_DOWNLOAD_DIR}/numpy*.whl"
    install = (
        f'for w in {_MAC_WHEEL_DOWNLOAD_DIR}/*.whl; do unzip -o -q "$w" -d '
        f'"{profile_python}"; done'
    )
    return f"{download}\n{drop_numpy}\n{install}"


# --------------------------------------------------------------------------- #
# Self-enforcing source sweep -- what DEPENDENCIES must equal
# --------------------------------------------------------------------------- #


def _stdlib_module_names() -> frozenset:
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return frozenset(names)
    # Fallback for interpreters without sys.stdlib_module_names (< 3.10):
    # only used by the drift check, never by the install decision itself.
    return frozenset(sys.builtin_module_names)


def scan_third_party_imports(root: Path) -> frozenset:
    """AST-sweep every ``.py`` file under ``root``; return the set of
    top-level import names that are neither stdlib, a relative (internal)
    import, nor a known QGIS/Qt platform module. This is what
    ``DEPENDENCIES`` above must equal -- see
    ``tests/test_install_dependencies.py``."""
    stdlib = _stdlib_module_names()
    found = set()
    for path in sorted(Path(root).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # relative import -- internal to the plugin
                if node.module:
                    found.add(node.module.split(".")[0])
    return frozenset(n for n in found if n not in stdlib and n not in _PLATFORM_MODULES)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the TRID3NT QGIS plugin's third-party dependencies "
        "into the running (QGIS) Python interpreter. macOS has no interpreter "
        "to install into -- see the wheel-download recipe this prints instead."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the present/missing table and the install command; install nothing",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    statuses = check_all()
    print(format_table(statuses))
    missing = [s for s in statuses if not s.present]

    if not missing:
        print("\nAll dependencies present.")
        return 0

    if sys.platform.startswith("darwin"):
        print(
            "\nQGIS 4's bundled Python on macOS has no pip module at all "
            "('No module named pip', live-verified) -- there is no "
            "interpreter here to install into. Run this with your SYSTEM "
            "python3 in a terminal instead, then restart QGIS:\n"
        )
        print(mac_wheel_recipe([s.pip_name for s in missing]))
        return 1

    if args.dry_run:
        cmd = pip_install_command_str(sys.executable, [s.pip_name for s in missing])
        print(f"\nWould run: {cmd}")
        return 1

    print(
        f"\nInstalling {len(missing)} missing dependency(ies): "
        + ", ".join(s.pip_name for s in missing)
    )
    install_missing(statuses)

    print("\nRe-verifying...")
    final = check_all()
    print(format_table(final))
    still_missing = [s for s in final if not s.present]
    if still_missing:
        print(
            "\nFAILED -- still missing: "
            + ", ".join(s.import_name for s in still_missing)
        )
        return 1
    print("\nAll dependencies installed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
