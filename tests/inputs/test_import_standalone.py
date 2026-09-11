"""The input layer imports on its own, with nothing else imported first.

A fresh interpreter is the only place this can be seen: once the tool registry
is loaded the cycle is already resolved and every order succeeds.
"""

from __future__ import annotations

import subprocess
import sys


def _import_first(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, f"{module} first: {result.stderr}"


def test_inputs_imports_with_nothing_else_imported_first() -> None:
    _import_first("trid3nt_server.inputs")


def test_runtime_imports_with_nothing_else_imported_first() -> None:
    _import_first("trid3nt_server.workflows.runtime")
