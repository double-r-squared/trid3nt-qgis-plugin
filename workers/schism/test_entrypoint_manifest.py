# SCHISM's manifest.json (variant/ncompute/nscribe/timeout_s/run_id)
# is the worker-side "build-spec parse entry point" for the mpirun invocation.
# An unknown key previously kept its default SILENTLY (the lesson
# a typo'd rank/variant knob would solve with the WRONG config, never erroring).

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Qualified import (never the bare "entrypoint" module name): several workers
# ship a same-named entrypoint.py, and a bare ``import entrypoint`` after a
# sys.path insert collides across worker test files sharing one pytest
# session (whichever worker's module wins the sys.modules["entrypoint"] slot
# poisons every other worker's import).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workers.schism.entrypoint import (  # noqa: E402
    SchismManifestUnknownFieldsError,
    _reject_unknown_manifest_fields,
)


def test_known_manifest_fields_accepted():
    _reject_unknown_manifest_fields(
        {"variant": "wwm", "ncompute": 3, "nscribe": 2, "timeout_s": 3600, "run_id": "r1"}
    )  # no raise


def test_seam_envelope_fields_accepted():
    # the generic run_solver seam writes inputs/outputs/schism_args into
    # rundir/manifest.json verbatim -- the entrypoint must accept-and-ignore them.
    _reject_unknown_manifest_fields(
        {"variant": "hydro", "ncompute": 3, "nscribe": 5, "run_id": "r2",
         "inputs": [{"gs_uri": "s3://c/x", "dest": "hgrid.gr3"}],
         "outputs": ["outputs/*.nc"], "schism_args": []}
    )  # no raise


def test_unknown_manifest_field_raises_typed_error():
    with pytest.raises(SchismManifestUnknownFieldsError, match="typo_field_name"):
        _reject_unknown_manifest_fields({"variant": "hydro", "typo_field_name": 1.0})
