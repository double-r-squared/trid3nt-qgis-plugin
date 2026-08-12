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

Pure stdlib (no PyQt/qgis import anywhere in this file) so it runs two ways:

  (a) directly, with the QGIS interpreter --
      mac:     <QGIS.app>/Contents/MacOS/bin/python3 install_dependencies.py
      linux:   python3 install_dependencies.py   (already QGIS's interpreter)
      windows: <QGIS install>\\apps\\Python3xx\\python.exe install_dependencies.py

  (b) imported by ``trid3nt.ui.charts.install_command_argv`` /
      ``trid3nt.ui.charts_window.MissingMatplotlibPanel``, whose "Attempt
      install" button runs THIS script (not raw pip) via ``QProcess`` -- one
      source of truth for what gets installed and how.

Behavior (direct run): check each dependency importable, print a
present/missing table, pip-install only what's missing into the running
interpreter (``-m pip install``, falling back to ``--user`` if the bundle's
site-packages is not writable), re-verify the imports, print an honest
per-dependency summary, and exit nonzero if anything is still missing.
``--dry-run`` prints the table and the command that would run without
installing anything.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import os
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
# Per-OS "which interpreter does pip need to target" resolution + install
# --------------------------------------------------------------------------- #


def install_python_executable(
    platform: Optional[str] = None,
    exec_prefix: Optional[str] = None,
    executable: Optional[str] = None,
) -> str:
    """The python binary pip must run under to land a dependency where this
    QGIS's interpreter will find it -- derived from ``sys.exec_prefix`` /
    ``sys.executable`` at call time, never a baked-in path."""
    platform = sys.platform if platform is None else platform
    exec_prefix = sys.exec_prefix if exec_prefix is None else exec_prefix
    executable = sys.executable if executable is None else executable
    if platform == "darwin":
        # QGIS.app's macOS bundle: sys.executable is the QGIS launcher, not a
        # runnable python; the real interpreter lives at exec_prefix/bin.
        return os.path.join(exec_prefix, "bin", "python3")
    if platform.startswith("win"):
        return os.path.join(exec_prefix, "python.exe")
    # Linux (and other POSIX): the running interpreter is already a real,
    # directly-invokable python -- prefer it; exec_prefix is the fallback
    # for the rare embedded build where sys.executable is empty.
    return executable or os.path.join(exec_prefix, "bin", "python3")


def pip_install_command_str(python_exe: str, pip_names: Sequence[str]) -> str:
    """The human-facing / --dry-run command line for installing ``pip_names``
    with ``python_exe``."""
    quoted_py = f'"{python_exe}"' if " " in python_exe else python_exe
    return " ".join([quoted_py, "-m", "pip", "install", *pip_names])


def _run_pip(
    python_exe: str, pip_names: Sequence[str], extra_args: Sequence[str] = ()
) -> bool:
    """Run ``python_exe -m pip install <pip_names> <extra_args>`` with
    inherited stdio so a QProcess parent sees the pip output live. Returns
    True on a zero exit code."""
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
    (default: the running interpreter's own executable). Tries a plain
    install first; on failure, retries with ``--user`` (the bundle's
    site-packages is commonly not writable without elevated privileges).
    Returns True iff the install command reported success -- callers must
    still re-verify by import (packages can install without becoming
    importable, e.g. an ABI mismatch)."""
    missing = [s for s in statuses if not s.present]
    if not missing:
        return True
    python_exe = python_exe or install_python_executable()
    pip_names = [s.pip_name for s in missing]
    if _run_pip(python_exe, pip_names):
        return True
    print(
        "plain install failed -- retrying with --user "
        "(the bundle's site-packages may not be writable)"
    )
    return _run_pip(python_exe, pip_names, extra_args=["--user"])


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
        "into the running (QGIS) Python interpreter."
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

    if args.dry_run:
        if missing:
            python_exe = install_python_executable()
            cmd = pip_install_command_str(
                python_exe, [s.pip_name for s in missing]
            )
            print(f"\nWould run: {cmd}")
            return 1
        print("\nAll dependencies present.")
        return 0

    if not missing:
        print("\nAll dependencies present. Nothing to install.")
        return 0

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
