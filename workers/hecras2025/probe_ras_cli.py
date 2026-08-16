"""Parsers for the HEC-RAS 2025 `ras` CLI probe outputs (flat-importable).

Two pure-text parsers used by the entrypoint probe and the worker tests. No
HEC-RAS install, no docker, no network: they operate on captured `ras` stdout so
the characterization is deterministic and offline-suite-safe.

- `parse_verb_surface(help_text)` -> {verb: description} from `ras --help`.
- `classify_linux_run(text)` -> a `RasLinuxProbe` verdict (managed-portable vs
  the native-payload gap) from a `ras healthcheck` / solve attempt trace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The verbs the headless real-AOI pipeline needs, in pipeline order. The spike
# proved every one of these prints on Linux (managed layer is portable); the
# native SOLVE is what is blocked (see classify_linux_run).
HEADLESS_PIPELINE_VERBS = (
    "createterrain",  # RAS terrain from input rasters (our fetch_dem output)
    "mesh",           # generate the computational mesh from a geometry file
    "prepare",        # ingest data + COMPUTE PROPERTY TABLES -> ready-to-run .r2r.h5
    "solve",          # run the RAS solver (CPU or GPU)
    "map",            # result -> map/raster (postprocess)
)

# The dlopen signature that marks the native-payload gap: the managed code takes
# a multiplatform path and looks for the LINUX shared library, absent from the
# win-x64-only public package.
_NATIVE_GAP_MARKERS = (
    "gdal_wrap",           # the Linux SWIG GDAL binding (gdal_wrap.so)
    "RasNativeParallel",   # the native parallel compute kernel
    "cannot open shared object file",
    "GDAL failed to load",
)

_VERB_LINE = re.compile(r"^\s{2}([a-z][a-z0-9-]*(?:,\s*[a-z][a-z0-9-]*)?)\s{2,}(\S.*)$")


def parse_verb_surface(help_text: str) -> dict[str, str]:
    """Parse `ras --help` into an ordered {verb: description} mapping.

    Continuation lines (wrapped descriptions) are folded into the preceding verb.
    Verb aliases (`ui, gui`) are split so each token keys the shared description.
    """
    verbs: dict[str, str] = {}
    last: str | None = None
    for line in help_text.splitlines():
        m = _VERB_LINE.match(line)
        if m:
            names, desc = m.group(1), m.group(2).strip()
            tokens = [t.strip() for t in names.split(",")]
            for tok in tokens:
                verbs[tok] = desc
            last = tokens[-1]
        elif last is not None and line.startswith(" " * 18) and line.strip():
            verbs[last] = (verbs[last] + " " + line.strip()).strip()
        else:
            last = None
    return verbs


@dataclass(frozen=True)
class RasLinuxProbe:
    """Verdict of running a `ras` compute verb under a Linux .NET runtime."""

    managed_cli_runs: bool  # did the managed .NET CLI load + execute on Linux?
    native_payload_present: bool  # did the Linux native compute libs resolve?
    detail: str

    @property
    def go(self) -> bool:
        """A headless SOLVE is possible only with the native payload present."""
        return self.managed_cli_runs and self.native_payload_present


def classify_linux_run(text: str, *, managed_cli_runs: bool = True) -> RasLinuxProbe:
    """Classify a `ras healthcheck`/solve trace as portable-managed vs native-gap.

    `managed_cli_runs` is True when `ras --version`/`--help` succeeded (the
    managed CLI is Linux-portable); this function decides whether the NATIVE
    compute/geospatial layer resolved. A `_NATIVE_GAP_MARKER` in the trace ->
    NO-GO-YET (win-x64-only payload); a clean run -> the native payload is present.
    """
    hit = next((m for m in _NATIVE_GAP_MARKERS if m in text), None)
    if hit is not None:
        return RasLinuxProbe(
            managed_cli_runs=managed_cli_runs,
            native_payload_present=False,
            detail=(
                "NO-GO-YET: managed .NET CLI is Linux-portable, but the native "
                f"payload is absent (marker: {hit!r}). The public HEC-RAS 2025 "
                "release is win-x64 only; no Linux SOLVE until HEC ships the "
                "linux-x64 natives / a container image."
            ),
        )
    return RasLinuxProbe(
        managed_cli_runs=managed_cli_runs,
        native_payload_present=True,
        detail="GO: managed CLI runs and the native compute/geospatial payload resolved on Linux.",
    )
