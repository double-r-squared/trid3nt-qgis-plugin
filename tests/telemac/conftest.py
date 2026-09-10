"""Fixtures for the TELEMAC tests.

The container boundary is stubbed in this directory and nowhere else: no test
outside it parses a steering file or reads a result file.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _offline_cas_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the in-container steering-file parse, which needs the image.

    What the suite proves instead is the WIRING - the author hands the parser every
    file it wrote, under the module whose dictionary reads it - and the REFUSAL shape."""
    def _parsed(rundir, steering):
        from pathlib import Path

        return {name: {"module": module, "ok": True, "keywords": 0}
                for name, module in steering.items()
                if (Path(rundir) / name).is_file()}

    # Only the SERIALIZER's binding: the gate's own module keeps its real
    # function so its tests exercise it with the container boundary stubbed one
    # level lower.
    monkeypatch.setattr(
        "trid3nt_server.workflows.telemac.authoring.serializer.validate_authored_steering",
        _parsed)


@pytest.fixture()
def telemac_result(monkeypatch: pytest.MonkeyPatch):
    """Hand a postprocess the fields a result file would have carried.

    The read is a docker round trip, so the fields are stated here and no test writes
    result bytes: a suite that spells out a file format re-implements it."""
    import numpy as np

    def install(*, varnames, x, y, ikle, times, data,
                x_origin: int = 0, y_origin: int = 0) -> dict[str, Any]:
        mesh = {
            "varnames": list(varnames),
            "npoin": len(x),
            "nelem": len(ikle),
            "x": np.asarray(x, dtype="float64"),
            "y": np.asarray(y, dtype="float64"),
            # 0-based, as the reader returns it.
            "ikle": np.asarray(ikle, dtype="int64"),
            "x_origin": int(x_origin),
            "y_origin": int(y_origin),
            "times": np.asarray(times, dtype="float64"),
            "data": {name: np.vstack([np.asarray(frame, dtype="float64")
                                      for frame in data[name]])
                     for name in varnames},
        }
        monkeypatch.setattr(
            "trid3nt_server.workflows.telemac.products.postprocess_telemac.read_selafin",
            lambda _path: mesh)
        return mesh

    return install
