"""Worker tests for the HEC-RAS 2025 CLI probe parsers (flat-import).

Deterministic + offline: they run against the REAL captured `ras` outputs under
`fixtures/` (produced on `dotnet/runtime:9.0`, 2026-08-04), so no HEC-RAS install
/ docker / network is needed. Flat-importable from the worker dir (mirror of the
6.x hecras worker's test layout).
"""

from __future__ import annotations

import pathlib

from probe_ras_cli import (
    HEADLESS_PIPELINE_VERBS,
    classify_linux_run,
    parse_verb_surface,
)

_FIX = pathlib.Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (_FIX / name).read_text()


def test_verb_surface_has_full_headless_pipeline():
    verbs = parse_verb_surface(_read("ras_help.txt"))
    for verb in HEADLESS_PIPELINE_VERBS:
        assert verb in verbs, f"headless-pipeline verb {verb!r} missing from ras --help"
    # `prepare` is the piece that retires the 6.x M3 STOP: it computes the subgrid
    # property tables headless (what RASMapper's Windows DLLs did in the 6.x path).
    assert "property tables" in verbs["prepare"].lower()


def test_verb_aliases_split():
    verbs = parse_verb_surface(_read("ras_help.txt"))
    # `ui, gui` is a two-token alias -> both key the same description.
    assert "ui" in verbs and "gui" in verbs
    assert verbs["ui"] == verbs["gui"]


def test_solve_help_documents_cpu_and_r2r():
    txt = _read("ras_solve_help.txt")
    # CPU solve exists (GPU/CUDA is optional) and the consolidated single-.h5 /
    # ready-to-run model is the input contract.
    assert "CPU" in txt and "GPU" in txt
    assert ".r2r.h5" in txt or "ready-to-run" in txt.lower()


def test_linux_healthcheck_is_native_gap_not_managed_failure():
    probe = classify_linux_run(_read("ras_healthcheck_linux.txt"), managed_cli_runs=True)
    assert probe.managed_cli_runs is True  # the managed .NET CLI loaded on Linux
    assert probe.native_payload_present is False  # native payload absent
    assert probe.go is False  # -> NO-GO-YET for a headless solve
    assert "NO-GO-YET" in probe.detail


def test_classify_go_when_native_present():
    # The flip case: a clean healthcheck (Linux natives shipped) reads as GO.
    probe = classify_linux_run("All RAS dependencies initialized OK.", managed_cli_runs=True)
    assert probe.go is True
    assert probe.native_payload_present is True
