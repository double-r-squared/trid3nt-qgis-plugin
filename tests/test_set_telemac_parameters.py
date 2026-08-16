"""Unit + solve tests for ``set_telemac_parameters`` (LANE D, offline).

The TELEMAC-2D steering deck (``.cas``) is a flat ``KEYWORD = value`` text
file, so the text-edit tests build a tiny synthetic parent ``.cas`` inline (no
mesh needed to exercise the parse/rewrite/read-back path). The child-deck-must-
SOLVE regression (``test_child_deck_solves_through_telemac``) runs the real
Malpasset small mesh through the ``trid3nt-local/telemac:latest`` docker image
on a SHORTENED variant (3 time steps) -- guarding the ADR-0021 lesson that the
SFINCS setter once shipped an unsolvable child deck. It resolves the mesh from
the committed ``fixtures/telemac_malpasset/`` copy (fallback: the acquired
``data/cases/malpasset/`` case dir) and SKIPS when docker / the image / the
mesh are unavailable, so the committed suite stays offline + portable (local
docker is compute, not network).

Coverage:
 1.  test_registered -- TOOL_REGISTRY entry + metadata.
 2.  test_friction_coefficient_set -- Strickler Ks set; before/after read back
     from the WRITTEN child .cas; mixed ``:`` separator preserved.
 3.  test_friction_coefficient_scale.
 4.  test_friction_law_set -- switch Strickler -> Manning.
 5.  test_combined_law_and_coefficient_one_call -- Manning n=0.033 in one call.
 6.  test_law_switch_leaves_coeff_out_of_band_warns -- the classic trap: switch
     to Manning but leave Ks=30 -> in_range=false + honest note (proceeds).
 7a. test_out_of_plausible_range_warns -- Ks=120 (above 15-90) warns, proceeds.
 7b. test_meaningless_coefficient_hard_error -- coeff <= 0 raises BoundsViolation.
 7c. test_unknown_law_raises / test_scale_on_law_raises / test_scale_needs_parent_coeff.
 8.  test_unknown_parameter_raises / test_non_cas_parent_raises.
 9.  test_parent_cas_byte_identical -- copy-on-write proof.
 10. test_manifest_written.
 11. test_child_deck_solves_through_telemac -- the SOLVE regression (docker).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from trid3nt_server.data import TOOL_REGISTRY  # noqa: E402
from trid3nt_server.data.simulation._setter_envelope import (  # noqa: E402
    BoundsViolation,
    SetterInputError,
)
from trid3nt_server.data.simulation.telemac.set_telemac_parameters.set_telemac_parameters import (  # noqa: E402
    set_telemac_parameters,
)


# The friction block mirrors the real Malpasset decks EXACTLY: LAW uses ``=``,
# COEFFICIENT uses ``:`` (both are valid TELEMAC separators), and a ``/`` comment
# line carrying a colon must be ignored by the keyword parser.
_PARENT_CAS = """\
/  a comment line with a colon : should be ignored
GEOMETRY FILE            = geo_malpasset-small.slf
BOUNDARY CONDITIONS FILE = geo_malpasset-small.cli
RESULTS FILE             = r2d_out.slf
TITLE = 'Le barrage de MALPASSET'
NUMBER OF TIME STEPS = 1000
TIME STEP = 4.
LAW OF BOTTOM FRICTION = 3
FRICTION COEFFICIENT : 30.
TURBULENCE MODEL = 1
VELOCITY DIFFUSIVITY = 1.
&FIN
"""


def _write_parent(dir_path: Path, text: str = _PARENT_CAS) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    cas = dir_path / "t2d_malpasset.cas"
    cas.write_text(text, encoding="utf-8")
    return cas


def _read_friction(cas_path: Path) -> tuple[int | None, float | None]:
    """Independent read-back (regex, not the module's own parser) of the LAW /
    COEFFICIENT values actually written to a .cas -- avoids a tautology."""
    law: int | None = None
    coeff: float | None = None
    for line in cas_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("/") or s.startswith("&"):
            continue
        m = re.match(r"\s*LAW OF BOTTOM FRICTION\s*[:=]\s*([-\d.eE+]+)", line)
        if m:
            law = int(float(m.group(1)))
        m = re.match(r"\s*FRICTION COEFFICIENT\s*[:=]\s*([-\d.eE+]+)", line)
        if m:
            coeff = float(m.group(1))
    return law, coeff


def _child_model_dir(result: dict) -> Path:
    return Path(result["child_setup_uri"][len("file://"):]).parent / "model"


def _child_cas(result: dict) -> Path:
    return _child_model_dir(result) / "t2d_malpasset.cas"


# --------------------------------------------------------------------------- #
# Registration + metadata
# --------------------------------------------------------------------------- #
def test_registered() -> None:
    assert "set_telemac_parameters" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["set_telemac_parameters"]
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.read_only_hint is False
    assert entry.metadata.idempotent_hint is False


# --------------------------------------------------------------------------- #
# Happy-path edits (before/after read back from the WRITTEN child .cas)
# --------------------------------------------------------------------------- #
def test_friction_coefficient_set(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path / "parent")
    result = set_telemac_parameters(
        parent_model_uri=str(parent),
        changes=[{"parameter": "friction_coefficient", "op": "set", "value": 40.0}],
        _work_dir=str(tmp_path / "work"),
    )
    assert result["engine"] == "telemac"
    [entry] = result["changes_applied"]
    assert entry["param"] == "friction_coefficient"
    assert entry["scope"] == "global"
    assert entry["before"] == pytest.approx(30.0)
    assert entry["after"] == pytest.approx(40.0)
    [plaus] = result["plausibility"]
    assert plaus["in_range"] is True

    law, coeff = _read_friction(_child_cas(result))
    assert coeff == pytest.approx(40.0)
    assert law == 3  # law untouched by a coefficient-only change
    # the ``:`` separator on the FRICTION COEFFICIENT line is preserved.
    assert re.search(
        r"FRICTION COEFFICIENT\s*:\s*40\.", _child_cas(result).read_text()
    )


def test_friction_coefficient_scale(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path / "parent")
    result = set_telemac_parameters(
        parent_model_uri=str(parent),
        changes=[{"parameter": "friction_coefficient", "op": "scale", "factor": 1.5}],
        _work_dir=str(tmp_path / "work"),
    )
    [entry] = result["changes_applied"]
    assert entry["before"] == pytest.approx(30.0)
    assert entry["after"] == pytest.approx(45.0)  # 30 * 1.5
    _, coeff = _read_friction(_child_cas(result))
    assert coeff == pytest.approx(45.0)


def test_friction_law_set(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path / "parent")
    result = set_telemac_parameters(
        parent_model_uri=str(parent),
        changes=[{"parameter": "friction_law", "op": "set", "value": 4}],
        _work_dir=str(tmp_path / "work"),
    )
    [entry] = result["changes_applied"]
    assert entry["param"] == "friction_law"
    assert entry["before"] == 3
    assert entry["after"] == 4
    law, _ = _read_friction(_child_cas(result))
    assert law == 4


def test_combined_law_and_coefficient_one_call(tmp_path: Path) -> None:
    # Switch to Manning (law 4) AND set a physical Manning n=0.033 in one call:
    # the coefficient band is chosen from the NEW law, so 0.033 is in-range.
    parent = _write_parent(tmp_path / "parent")
    result = set_telemac_parameters(
        parent_model_uri=str(parent),
        changes=[
            {"parameter": "friction_law", "op": "set", "value": 4},
            {"parameter": "friction_coefficient", "op": "set", "value": 0.033},
        ],
        _work_dir=str(tmp_path / "work"),
    )
    by_param = {c["param"]: c for c in result["changes_applied"]}
    assert set(by_param) == {"friction_law", "friction_coefficient"}
    assert by_param["friction_law"]["after"] == 4
    assert by_param["friction_coefficient"]["after"] == pytest.approx(0.033)
    assert "Manning" in by_param["friction_coefficient"]["unit"]
    coeff_plaus = [p for p in result["plausibility"]][-1]
    assert coeff_plaus["in_range"] is True  # 0.033 inside Manning 0.011-0.1
    law, coeff = _read_friction(_child_cas(result))
    assert law == 4 and coeff == pytest.approx(0.033)


# --------------------------------------------------------------------------- #
# The classic law-interpretation trap
# --------------------------------------------------------------------------- #
def test_law_switch_leaves_coeff_out_of_band_warns(tmp_path: Path) -> None:
    # Switch Strickler -> Manning but DO NOT touch the coefficient (still 30).
    # A Manning n of 30 is absurd -> the coefficient re-check under the new law
    # warns (in_range=false) + an honest note; the setter proceeds (not a hard
    # reject -- the user may know what they are doing).
    parent = _write_parent(tmp_path / "parent")
    result = set_telemac_parameters(
        parent_model_uri=str(parent),
        changes=[{"parameter": "friction_law", "op": "set", "value": 4}],
        _work_dir=str(tmp_path / "work"),
    )
    # only friction_law is in changes_applied (coefficient not requested)
    assert [c["param"] for c in result["changes_applied"]] == ["friction_law"]
    # but a coefficient plausibility WARNING is surfaced under the new law
    coeff_plaus = [p for p in result["plausibility"] if p["param"] == "friction_coefficient"]
    assert coeff_plaus and coeff_plaus[0]["in_range"] is False
    assert any("meaning changed with the law" in n for n in result["notes"])
    # the coefficient itself is left untouched in the written deck (still 30).
    _, coeff = _read_friction(_child_cas(result))
    assert coeff == pytest.approx(30.0)


# --------------------------------------------------------------------------- #
# Bounds: soft warning vs hard error
# --------------------------------------------------------------------------- #
def test_out_of_plausible_range_warns(tmp_path: Path) -> None:
    # 120 is above the Strickler plausible band (15-90) but physically valid
    # (very smooth bed): warn-and-proceed with in_range=false, NOT a hard reject.
    parent = _write_parent(tmp_path / "parent")
    result = set_telemac_parameters(
        parent_model_uri=str(parent),
        changes=[{"parameter": "friction_coefficient", "op": "set", "value": 120.0}],
        _work_dir=str(tmp_path / "work"),
    )
    [entry] = result["changes_applied"]
    assert entry["after"] == pytest.approx(120.0)
    [plaus] = result["plausibility"]
    assert plaus["in_range"] is False
    assert plaus["range"] == [15.0, 90.0]
    assert "WARNING" in plaus["note"]


def test_meaningless_coefficient_hard_error(tmp_path: Path) -> None:
    # A non-positive friction coefficient is physically meaningless under every
    # law -> hard BoundsViolation (not a soft warning).
    parent = _write_parent(tmp_path / "parent")
    with pytest.raises(BoundsViolation) as excinfo:
        set_telemac_parameters(
            parent_model_uri=str(parent),
            changes=[{"parameter": "friction_coefficient", "op": "set", "value": 0.0}],
            _work_dir=str(tmp_path / "work"),
        )
    assert excinfo.value.error_code == "PARAM_BOUNDS_VIOLATION"
    assert excinfo.value.retryable is False


def test_unknown_law_raises(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path / "parent")
    with pytest.raises(SetterInputError):
        set_telemac_parameters(
            parent_model_uri=str(parent),
            changes=[{"parameter": "friction_law", "op": "set", "value": 7}],
            _work_dir=str(tmp_path / "work"),
        )


def test_scale_on_law_raises(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path / "parent")
    with pytest.raises(SetterInputError):
        set_telemac_parameters(
            parent_model_uri=str(parent),
            changes=[{"parameter": "friction_law", "op": "scale", "factor": 1.5}],
            _work_dir=str(tmp_path / "work"),
        )


def test_scale_needs_parent_coefficient(tmp_path: Path) -> None:
    # A deck with NO FRICTION COEFFICIENT line cannot be scaled (nothing to
    # multiply) -> typed input error.
    no_coeff = "\n".join(
        line for line in _PARENT_CAS.splitlines() if "FRICTION COEFFICIENT" not in line
    ) + "\n"
    parent = _write_parent(tmp_path / "parent", text=no_coeff)
    with pytest.raises(SetterInputError):
        set_telemac_parameters(
            parent_model_uri=str(parent),
            changes=[{"parameter": "friction_coefficient", "op": "scale", "factor": 2.0}],
            _work_dir=str(tmp_path / "work"),
        )


def test_unknown_parameter_raises(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path / "parent")
    with pytest.raises(SetterInputError):
        set_telemac_parameters(
            parent_model_uri=str(parent),
            changes=[{"parameter": "time_step", "op": "set", "value": 2.0}],
            _work_dir=str(tmp_path / "work"),
        )


def test_non_cas_parent_raises(tmp_path: Path) -> None:
    not_cas = tmp_path / "parent" / "geometry.slf"
    not_cas.parent.mkdir(parents=True)
    not_cas.write_text("not a cas")
    with pytest.raises(SetterInputError):
        set_telemac_parameters(
            parent_model_uri=str(not_cas),
            changes=[{"parameter": "friction_coefficient", "op": "set", "value": 40.0}],
            _work_dir=str(tmp_path / "work"),
        )


# --------------------------------------------------------------------------- #
# Copy-on-write invariants
# --------------------------------------------------------------------------- #
def test_parent_cas_byte_identical(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path / "parent")
    before = parent.read_bytes()
    set_telemac_parameters(
        parent_model_uri=str(parent),
        changes=[
            {"parameter": "friction_law", "op": "set", "value": 4},
            {"parameter": "friction_coefficient", "op": "set", "value": 0.05},
        ],
        _work_dir=str(tmp_path / "work"),
    )
    assert parent.read_bytes() == before  # parent untouched, byte-for-byte


def test_manifest_written(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path / "parent")
    result = set_telemac_parameters(
        parent_model_uri=str(parent),
        changes=[{"parameter": "friction_coefficient", "op": "set", "value": 35.0}],
        _work_dir=str(tmp_path / "work"),
    )
    manifest_path = Path(result["child_setup_uri"][len("file://"):])
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["engine"] == "telemac"
    assert manifest["parent_model"] == str(parent)
    assert manifest["cas_file"] == "t2d_malpasset.cas"


# --------------------------------------------------------------------------- #
# SOLVE regression (docker; skips when unavailable) -- ADR 0021 lesson.
# --------------------------------------------------------------------------- #
def _telemac_image() -> str | None:
    image = os.environ.get("TRID3NT_TELEMAC_IMAGE", "trid3nt-local/telemac:latest")
    if shutil.which("docker") is None:
        return None
    proc = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True
    )
    return image if proc.returncode == 0 else None


def _mesh_fixture_dir() -> Path | None:
    here = Path(__file__).parent / "fixtures" / "telemac_malpasset"
    if (here / "geo_malpasset-small.slf").is_file() and (here / "geo_malpasset-small.cli").is_file():
        return here
    case = Path(__file__).resolve().parents[1] / "data" / "cases" / "malpasset"
    if (case / "geo_malpasset-small.slf").is_file() and (case / "geo_malpasset-small.cli").is_file():
        return case
    return None


# A self-contained, v9-compatible SHORT Malpasset deck: TELEMAC v9 removed the
# COMPUTATION CONTINUED keyword and the shipped decks reference a user_fortran
# we do not ship, so both are omitted; a CONSTANT ELEVATION initial condition
# floods the reservoir and 3 time steps keep the solve to a few seconds.
_SHORT_CAS = """\
GEOMETRY FILE            = geo_malpasset-small.slf
BOUNDARY CONDITIONS FILE = geo_malpasset-small.cli
RESULTS FILE             = r2d_short.slf
TITLE = 'malpasset short solve'
VARIABLES FOR GRAPHIC PRINTOUTS = U,V,H,S,B
MASS-BALANCE = YES
NUMBER OF TIME STEPS = 3
TIME STEP = 4.
GRAPHIC PRINTOUT PERIOD = 1
LISTING PRINTOUT PERIOD = 1
INITIAL CONDITIONS = 'CONSTANT ELEVATION'
INITIAL ELEVATION = 100.
TYPE OF ADVECTION = 14;5
SUPG OPTION = 0;0
MATRIX STORAGE : 3
IMPLICITATION FOR DEPTH = 1.
IMPLICITATION FOR VELOCITY = 1.
MASS-LUMPING ON H = 1.
H CLIPPING = NO
LAW OF BOTTOM FRICTION = 3
FRICTION COEFFICIENT : 30.
TURBULENCE MODEL = 1
VELOCITY DIFFUSIVITY = 1.
TIDAL FLATS = YES
OPTION FOR THE TREATMENT OF TIDAL FLATS : 1
TREATMENT OF THE LINEAR SYSTEM : 2
SOLVER : 1
PRECONDITIONING : 2
MAXIMUM NUMBER OF ITERATIONS FOR SOLVER = 200
SOLVER ACCURACY = 0.0001
TREATMENT OF NEGATIVE DEPTHS : 2
CONTINUITY CORRECTION : YES
&FIN
"""


def _solves(case_dir: Path, cas_name: str, image: str) -> str:
    proc = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{case_dir}:/data", "-w", "/data",
         image, "telemac2d.py", cas_name],
        capture_output=True, text=True, timeout=600,
    )
    return proc.stdout + "\n" + proc.stderr


def test_child_deck_solves_through_telemac(tmp_path: Path) -> None:
    """Child-deck-must-SOLVE regression (ADR 0021): the child ``.cas`` a
    friction edit produces still runs to ``CORRECT END OF RUN`` under the real
    TELEMAC-2D docker image, exactly like its parent. The SFINCS setter once
    shipped an unsolvable child deck (a mangled CRS line); this proves
    set_telemac_parameters does not carry that defect class. Marked slow (a real
    ~10 s docker solve); skips offline when docker/image/mesh are unavailable."""
    image = _telemac_image()
    if image is None:
        pytest.skip("docker or trid3nt-local/telemac:latest image unavailable")
    mesh_dir = _mesh_fixture_dir()
    if mesh_dir is None:
        pytest.skip("Malpasset small mesh fixture unavailable")

    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()
    shutil.copy2(mesh_dir / "geo_malpasset-small.slf", parent_dir / "geo_malpasset-small.slf")
    shutil.copy2(mesh_dir / "geo_malpasset-small.cli", parent_dir / "geo_malpasset-small.cli")
    parent_cas = parent_dir / "t2d_short.cas"
    parent_cas.write_text(_SHORT_CAS, encoding="utf-8")

    # 1) copy-on-write the child FIRST (off the clean, unsolved parent).
    result = set_telemac_parameters(
        parent_model_uri=str(parent_cas),
        changes=[{"parameter": "friction_coefficient", "op": "set", "value": 40.0}],
        _work_dir=str(tmp_path / "work"),
    )
    child_dir = _child_model_dir(result)
    child_cas = child_dir / "t2d_short.cas"
    assert child_cas.is_file()
    _, child_coeff = _read_friction(child_cas)
    assert child_coeff == pytest.approx(40.0)  # the edit landed

    # 2) sanity: the parent deck itself solves.
    parent_out = _solves(parent_dir, "t2d_short.cas", image)
    assert "CORRECT END OF RUN" in parent_out, (
        f"parent short deck did not solve:\n{parent_out[-1500:]}"
    )

    # 3) THE assertion: the friction-edited child deck still solves.
    child_out = _solves(child_dir, "t2d_short.cas", image)
    assert "CORRECT END OF RUN" in child_out, (
        f"child (friction-edited) deck did not solve:\n{child_out[-1500:]}"
    )
