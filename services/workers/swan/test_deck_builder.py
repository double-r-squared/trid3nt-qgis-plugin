"""Unit tests for the SWAN deck author (``deck_builder``) -- SWAN Phase 1.

The SWAN analogue of ``services/workers/geoclaw/test_setrun_builder.py``. These
pin the DETERMINISTIC, swan-free deck-authoring core (the heart of the engine):

  1. build_spec validation -- typed error on missing/invalid fields.
  2. .swn command-file generation -- the rendered command file carries the
     load-bearing SWAN keyword blocks (CGRID/CIRCLE, INPGRID+READINP BOTTOM,
     [WIND], GEN3, BOUND SHAPE + BOUNDSPEC, BLOCK output, COMPUTE) wired from the
     spec, per mode.
  3. bottom input array -- a rectangular depth grid of the right shape, demo flat
     bathymetry by default, overridable via depth_fn.
  4. full deck build into a tmp dir + the SwanDeckManifest provenance + the INPUT
     file (SWAN's literal command-file convention).

NO SWAN / gfortran is required -- the deck author never imports them; it is a pure
string render (mirrors the GeoClaw deck-author test).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from services.workers.swan.deck_builder import (
    INPUT_FILENAME,
    OUTPUT_MAT_FILENAME,
    SWAN_DEPMIN_M,
    SWN_CASENAME,
    SWN_FILENAME,
    SwanBuildSpec,
    SwanDeckError,
    build_swan_deck,
    parse_build_spec,
    render_bottom_input,
    render_swn_command_file,
)

_AOI = [-85.75, 29.55, -85.25, 30.20]  # Mexico Beach-ish demo box


def _spec(**over) -> dict:
    base = {
        "mode": "stationary",
        "bbox": list(_AOI),
        "bottom_file": "bottom.bot",
        "mx": 40,
        "my": 50,
        "n_dir": 36,
        "n_freq": 32,
        "freq_low_hz": 0.04,
        "freq_high_hz": 1.0,
        "boundary": {
            "hs_m": 3.0,
            "tp_s": 9.0,
            "dir_deg": 180.0,
            "spread_deg": 25.0,
            "side": "S",
        },
        "friction": True,
        "breaking": True,
        "triads": True,
        "output_quantities": ["HSIGN", "RTP", "DIR"],
    }
    base.update(over)
    return base


# ===========================================================================
# (1) build_spec validation.
# ===========================================================================
def test_parse_valid_spec_fills_defaults():
    spec = parse_build_spec({"bbox": _AOI, "bottom_file": "b.bot"})
    assert isinstance(spec, SwanBuildSpec)
    assert spec.mode == "stationary"  # default
    assert spec.n_dir == 36
    assert spec.n_freq == 32
    assert spec.bbox == tuple(_AOI)
    # HSIGN is always guaranteed present.
    assert "HSIGN" in spec.output_quantities


def test_parse_rejects_bad_mode():
    with pytest.raises(SwanDeckError) as ei:
        parse_build_spec(_spec(mode="nope"))
    assert ei.value.error_code == "SWAN_SPEC_INVALID"


def test_parse_rejects_bad_bbox():
    with pytest.raises(SwanDeckError):
        parse_build_spec({"bbox": [1, 2, 3], "bottom_file": "b.bot"})
    with pytest.raises(SwanDeckError):
        parse_build_spec({"bbox": [10, 10, 5, 5], "bottom_file": "b.bot"})


def test_parse_requires_bottom_file():
    with pytest.raises(SwanDeckError) as ei:
        parse_build_spec({"bbox": _AOI})
    assert ei.value.error_code == "SWAN_SPEC_INVALID"


def test_parse_rejects_bad_spectral_grid():
    with pytest.raises(SwanDeckError):
        parse_build_spec(_spec(n_dir=8))  # < 12
    with pytest.raises(SwanDeckError):
        parse_build_spec(_spec(n_freq=2))  # < 4
    with pytest.raises(SwanDeckError):
        parse_build_spec(_spec(freq_low_hz=1.0, freq_high_hz=0.5))  # low >= high


def test_parse_rejects_bad_boundary():
    with pytest.raises(SwanDeckError):
        parse_build_spec(_spec(boundary={"hs_m": -1.0}))
    with pytest.raises(SwanDeckError):
        parse_build_spec(_spec(boundary={"dir_deg": 999.0}))
    with pytest.raises(SwanDeckError):
        parse_build_spec(_spec(boundary={"side": "Z"}))


def test_parse_rejects_unknown_output_quantity():
    with pytest.raises(SwanDeckError):
        parse_build_spec(_spec(output_quantities=["NOPE"]))


def test_parse_nonstationary_requires_positive_timing():
    with pytest.raises(SwanDeckError):
        parse_build_spec(_spec(mode="nonstationary", sim_duration_s=0))
    with pytest.raises(SwanDeckError):
        parse_build_spec(_spec(mode="nonstationary", time_step_s=0))


# ===========================================================================
# (2) .swn command-file generation -- load-bearing SWAN keyword blocks.
# ===========================================================================
def test_render_swn_stationary_has_load_bearing_blocks():
    spec = parse_build_spec(_spec(mode="stationary"))
    text = render_swn_command_file(spec)
    # PROJECT + SET + run mode + coordinates.
    assert "PROJECT 'GRACE2' 'WAVE'" in text
    # SET uses positional/keyword SWAN syntax (NO ``KEY=value``) with NAUTICAL
    # last and a DEPMIN threshold; ``LEVEL=``/``NOR=`` were invalid SWAN keywords.
    # EXCEPTION is NOT a SET field (SWAN rejected it there); it moved to INPGRID.
    assert "SET LEVEL 0.0 NOR 90.0 DEPMIN 0.05 NAUTICAL" in text
    assert "LEVEL=" not in text and "NOR=" not in text
    assert "EXCEPTION" not in text.split("SET ", 1)[1].split("\n", 1)[0]
    assert "MODE STATIONARY TWODIMENSIONAL" in text
    assert "COORDINATES SPHERICAL" in text
    # CGRID with the spectral CIRCLE block (ndir flow fhigh nfreq).
    assert "CGRID REGULAR" in text
    assert "CIRCLE 36 0.0400 1.0000 32" in text
    # Domain origin wired from bbox (SW corner).
    assert "-85.750000 29.550000" in text
    # Bottom input grid + read of the staged bottom file.
    assert "INPGRID BOTTOM REGULAR" in text
    # idla=3 (SW/lower-left, rows south->north) matches render_bottom_input's
    # south-first write order; idla=1 (NW) mirrored the bed N<->S -> all-dry no-op.
    assert "READINP BOTTOM 1.0 'bottom.bot' 3 0 FREE" in text
    # EXCEPTION sentinel now on the INPGRID BOTTOM line (its correct home), not SET.
    assert "EXCEPTION -999.0" in text
    # Physics: GEN3 + friction + breaking + triads.
    assert "GEN3 WESTHUYSEN" in text
    # Zero-wind (no wind_file) deck disables quadruplets, else SWAN aborts at error
    # level 3 ("quadruplets in combination with zero wind") and never computes.
    assert "OFF QUAD" in text
    assert "FRICTION JONSWAP" in text
    assert "BREAKING CONSTANT" in text
    assert "TRIAD" in text
    # Parametric boundary (JONSWAP shape + SIDE S CONSTANT PAR Hs Tp dir dd).
    assert "BOUND SHAPE JONSWAP" in text
    assert "BOUNDSPEC SIDE S CONSTANT PAR 3.000 9.000 180.00 25.00" in text
    # Gridded output BLOCK to the .mat + the requested quantities.
    assert f"BLOCK 'COMPGRID' NOHEADER '{OUTPUT_MAT_FILENAME}' LAYOUT 3 HSIGN RTP DIR" in text
    # Stationary compute + stop. SWAN's manual is explicit: in MODE STATIONARY the
    # COMPUTE command takes NO option -- a bare ``COMPUTE`` (the ``STATIONARY``
    # token after COMPUTE is reserved for a stationary step inside a NONSTATIONARY
    # file, and writing it here made SWAN skip the iteration -> the 33 ms no-op).
    lines = text.splitlines()
    assert "COMPUTE" in lines  # the bare COMPUTE line, exactly
    assert "COMPUTE STATIONARY" not in text
    assert "STOP" in text


def test_render_swn_nonstationary_has_nonstat_compute():
    spec = parse_build_spec(
        _spec(mode="nonstationary", sim_duration_s=10800.0, time_step_s=600.0)
    )
    text = render_swn_command_file(spec)
    assert "MODE NONSTATIONARY TWODIMENSIONAL" in text
    assert "COMPUTE NONSTATIONARY" in text
    assert "600.0 SEC" in text
    assert "COMPUTE STATIONARY" not in text


def test_render_swn_stationary_deck_actually_computes_and_writes_output():
    """REGRESSION (live 2026-06-23 33 ms no-op): the authored STATIONARY deck must
    contain an EXECUTABLE compute over a NON-DEGENERATE grid plus a BLOCK that
    writes ``swan_out.mat`` with the significant-wave-height field the postprocess
    reads -- otherwise SWAN reaches "Normal end of run" without iterating, writes
    no .mat, and the workflow fails honestly with SWAN_BATCH_OUTPUT_MISSING.

    The two deck defects this pins:
      (1) STATIONARY mode must emit a BARE ``COMPUTE`` (no ``STATIONARY`` option);
          ``COMPUTE STATIONARY`` made SWAN skip the iteration (the no-op).
      (2) The BLOCK must target ``swan_out.mat`` with HSIGN -- the EXACT variable
          the postprocess (`_HS_PREFIXES`) matches. A .mat extension makes SWAN
          write a binary MATLAB file; LAYOUT 3 is SWAN's recommended .mat layout.
    """
    spec = parse_build_spec(_spec(mode="stationary"))
    text = render_swn_command_file(spec)
    lines = text.splitlines()

    # (1) An EXECUTABLE compute: a bare COMPUTE line, NOT ``COMPUTE STATIONARY``
    # (the no-op token), and it must be present exactly once.
    assert "COMPUTE" in lines, "stationary deck must contain a bare COMPUTE"
    assert "COMPUTE STATIONARY" not in text, (
        "COMPUTE STATIONARY in a MODE STATIONARY deck makes SWAN skip the solve "
        "(the live 33 ms no-op) -- emit a bare COMPUTE"
    )
    assert lines.count("COMPUTE") == 1

    # The compute must run over a NON-DEGENERATE computational grid: a real bbox
    # (-85.75..-85.25 lon, 29.55..30.20 lat) with mx=40 my=50 meshes -> the CGRID
    # spans must be strictly positive (zero-span = nothing to iterate over).
    cgrid = next(L for L in lines if L.startswith("CGRID REGULAR"))
    toks = cgrid.split()
    # CGRID REGULAR xpc ypc alpc xlenc ylenc mxc myc CIRCLE ...
    xlenc, ylenc = float(toks[5]), float(toks[6])
    mxc, myc = int(toks[7]), int(toks[8])
    assert xlenc > 0.0 and ylenc > 0.0, "degenerate CGRID extent -> SWAN no-op"
    assert mxc >= 1 and myc >= 1, "degenerate CGRID mesh count -> SWAN no-op"

    # (2) The BLOCK must write swan_out.mat with HSIGN (the postprocess primary
    # field), and BEFORE the COMPUTE (SWAN ignores output declared after COMPUTE).
    block = next(L for L in lines if L.startswith("BLOCK"))
    assert "swan_out.mat" in block
    assert "HSIGN" in block
    assert lines.index(block) < lines.index("COMPUTE"), (
        "BLOCK must precede COMPUTE -- SWAN ignores output requested after COMPUTE"
    )

    # Deck <-> postprocess contract: the BLOCK requests HSIGN, which SWAN writes to
    # the .mat as the variable ``Hsig``. ``postprocess_swan._HS_PREFIXES`` matches
    # ("Hsig", "Hsign", "HSIGN", "Hs"), so the BLOCK's HSIGN field is the one the
    # reader rasterizes. (We assert the deck side here -- the worker test path does
    # not import the agent package -- and pin the agreed token names explicitly so
    # a drift in either direction is visible.)
    assert "HSIGN" in block
    _POSTPROCESS_HS_PREFIXES = ("Hsig", "Hsign", "HSIGN", "Hs")
    assert "HSIGN" in _POSTPROCESS_HS_PREFIXES


def test_render_swn_wind_block_only_when_wind_file_present():
    no_wind = parse_build_spec(_spec())
    assert "READINP WIND" not in render_swn_command_file(no_wind)
    with_wind = parse_build_spec(_spec(wind_file="wind.dat"))
    text = render_swn_command_file(with_wind)
    assert "INPGRID WIND REGULAR" in text
    assert "READINP WIND 1.0 'wind.dat' 1 0 FREE" in text


def test_render_swn_physics_toggles_omit_blocks_when_disabled():
    spec = parse_build_spec(_spec(friction=False, breaking=False, triads=False))
    text = render_swn_command_file(spec)
    assert "GEN3" in text  # GEN3 always on (third-generation core)
    assert "FRICTION" not in text
    assert "BREAKING" not in text
    assert "TRIAD" not in text


def test_render_swn_boundary_side_respected():
    spec = parse_build_spec(_spec(boundary={"side": "E", "hs_m": 4.5, "tp_s": 11.0}))
    text = render_swn_command_file(spec)
    assert "BOUNDSPEC SIDE E CONSTANT PAR 4.500 11.000" in text


# ===========================================================================
# (3) bottom input array render.
# ===========================================================================
def test_render_bottom_input_shape_and_flat_default():
    spec = parse_build_spec(_spec(mx=4, my=3))
    text = render_bottom_input(spec)
    rows = [r for r in text.splitlines() if r.strip()]
    # (my+1) rows of (mx+1) values each (SWAN grid POINTS = mesh + 1).
    assert len(rows) == 4  # my+1 = 3+1
    for r in rows:
        vals = r.split()
        assert len(vals) == 5  # mx+1 = 4+1
        # flat demo bathymetry = 10.0 m everywhere.
        assert all(abs(float(v) - 10.0) < 1e-6 for v in vals)


def test_render_bottom_input_uses_depth_fn():
    spec = parse_build_spec(_spec(mx=4, my=4))
    # depth = 5 m + 1 m per degree of longitude east of the SW corner.
    def depth_fn(lon, lat):
        return 5.0 + (lon - _AOI[0])

    text = render_bottom_input(spec, depth_fn=depth_fn)
    rows = [r for r in text.splitlines() if r.strip()]
    first_row_vals = [float(v) for v in rows[0].split()]
    # the west-most value is ~5.0, the east-most is deeper (5 + bbox width).
    assert first_row_vals[0] == pytest.approx(5.0, abs=1e-6)
    assert first_row_vals[-1] > first_row_vals[0]


# ===========================================================================
# (4) full deck build into a tmp dir + SwanDeckManifest provenance.
# ===========================================================================
def test_swn_casename_and_filename_are_consistent():
    """REGRESSION (live 2026-06-23): ``swanrun -input <SWN_CASENAME>`` appends
    ``.swn`` and reads ``<SWN_CASENAME>.swn`` -- which MUST be the file the deck
    author writes (``SWN_FILENAME``). The case name must NOT be ``INPUT`` (that
    made swanrun hunt for the nonexistent ``INPUT.swn`` and abort before solving).
    """
    assert SWN_FILENAME == f"{SWN_CASENAME}.swn"
    assert SWN_CASENAME != INPUT_FILENAME
    assert not SWN_CASENAME.endswith(".swn")  # bare case name, swanrun adds .swn


def test_build_swan_deck_writes_swn_file_swanrun_reads(tmp_path: Path):
    """The deck author MUST write the ``<SWN_CASENAME>.swn`` file that
    ``swanrun -input <SWN_CASENAME>`` reads (the load-bearing convention the live
    Mexico Beach run violated). It ALSO writes a literal ``INPUT`` for fallback.
    """
    manifest = build_swan_deck(_spec(mode="stationary"), tmp_path)
    # The .swn file swanrun actually copies to INPUT MUST exist.
    assert (tmp_path / SWN_FILENAME).exists()
    assert SWN_FILENAME in manifest.files_written
    # The fallback literal INPUT command file is also present + identical bytes.
    assert (tmp_path / INPUT_FILENAME).exists()
    assert INPUT_FILENAME in manifest.files_written
    assert (tmp_path / SWN_FILENAME).read_text() == (
        tmp_path / INPUT_FILENAME
    ).read_text()


def test_build_swan_deck_writes_input_and_bottom(tmp_path: Path):
    manifest = build_swan_deck(_spec(mode="stationary"), tmp_path)
    # The command file MUST be written as the file literally named INPUT (the SWAN
    # convention) AND as the .swn swanrun reads -- both are load-bearing.
    assert (tmp_path / INPUT_FILENAME).exists()
    assert (tmp_path / SWN_FILENAME).exists()
    assert (tmp_path / "bottom.bot").exists()
    assert (tmp_path / "deck_manifest.json").exists()
    assert INPUT_FILENAME in manifest.files_written
    assert "bottom.bot" in manifest.files_written
    assert "stationary" in manifest.driver_descriptor
    assert manifest.wind_enabled is False
    # the on-disk INPUT carries the SWAN keyword sequence.
    input_text = (tmp_path / INPUT_FILENAME).read_text()
    assert "CGRID REGULAR" in input_text
    # Stationary mode -> a bare COMPUTE (no STATIONARY option); see the deck author.
    assert "COMPUTE" in input_text.splitlines()
    assert "COMPUTE STATIONARY" not in input_text
    # the persisted manifest round-trips.
    disk = json.loads((tmp_path / "deck_manifest.json").read_text())
    assert disk["mode"] == "stationary"
    assert disk["boundary_hs_m"] == 3.0
    assert disk["output_quantities"] == ["HSIGN", "RTP", "DIR"]


def test_build_swan_deck_wind_enabled_manifest(tmp_path: Path):
    manifest = build_swan_deck(_spec(wind_file="wind.dat"), tmp_path)
    assert manifest.wind_enabled is True
    assert "ERA5 wind" in manifest.driver_descriptor
    assert "READINP WIND" in (tmp_path / INPUT_FILENAME).read_text()


# ===========================================================================
# (5) REGRESSION: the entrypoint's swanrun invocation finds the authored .swn.
# ===========================================================================
# A fake ``swanrun`` that replicates the TU Delft launcher's load-bearing behavior:
# it APPENDS ``.swn`` to the ``-input`` argument and ABORTS if that file is
# missing (the exact "file <name>.swn does not exist" path that killed the live
# 2026-06-23 Mexico Beach run). It does NOT need SWAN -- it only proves the
# entrypoint hands swanrun a case name whose ``.swn`` the deck author wrote.
_FAKE_SWANRUN = """#!/usr/bin/env python3
import sys
inp = None
argv = sys.argv[1:]
for i, a in enumerate(argv):
    if a == "-input" and i + 1 < len(argv):
        inp = argv[i + 1]
print("swan.exe is /opt/swan/swan.exe")
if inp is None:
    print("no -input argument"); sys.exit(1)
import os
swn = inp + ".swn"
if not os.path.isfile(swn):
    # The exact swanrun failure mode from the live log.
    print("file %s does not exist" % swn); sys.exit(1)
# swanrun copies <name>.swn -> INPUT, then runs swan.exe (which reads INPUT).
with open(swn) as fh:
    data = fh.read()
with open("INPUT", "w") as fh:
    fh.write(data)
# Stand in for swan.exe: write a trivial swan_out.mat so the run looks complete.
open("swan_out.mat", "wb").write(b"FAKE-SWAN-MAT")
sys.exit(0)
"""


def test_entrypoint_swanrun_invocation_finds_authored_swn(tmp_path: Path):
    """REGRESSION (live 2026-06-23): the entrypoint's default swanrun command must
    reference the case name whose ``.swn`` the deck author actually wrote. The old
    ``swanrun -input INPUT`` made the launcher hunt for ``INPUT.swn`` (never
    written) and abort with exit 1 BEFORE SWAN solved. This authors a real deck,
    then runs the entrypoint's ``_run_swan`` against a fake ``swanrun`` that
    replicates the launcher's ``.swn``-append-or-die behavior -- proving the file
    is found and the run reaches a 0 exit.
    """
    # Author a real deck into the scratch dir (writes swan_run.swn + INPUT + bottom).
    build_swan_deck(_spec(mode="nonstationary", sim_duration_s=3600.0), tmp_path)
    assert (tmp_path / SWN_FILENAME).exists()

    # Drop a fake swanrun on PATH that fails exactly like the real launcher would
    # if the <case>.swn file is missing.
    fake_swanrun = tmp_path / "swanrun"
    fake_swanrun.write_text(_FAKE_SWANRUN.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
    fake_swanrun.chmod(fake_swanrun.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    from services.workers.swan.entrypoint import _run_swan

    old_path = os.environ.get("PATH", "")
    old_swan_run = os.environ.pop("GRACE2_SWAN_RUN", None)  # exercise the DEFAULT cmd
    try:
        os.environ["PATH"] = f"{tmp_path}{os.pathsep}{old_path}"
        rc, stdout_path, _stderr_path = _run_swan(tmp_path)
    finally:
        os.environ["PATH"] = old_path
        if old_swan_run is not None:
            os.environ["GRACE2_SWAN_RUN"] = old_swan_run

    stdout = stdout_path.read_text()
    # The launcher must NOT have aborted on a missing .swn (the live failure).
    assert "does not exist" not in stdout, (
        f"swanrun could not find the authored .swn -- the live bug recurred: {stdout!r}"
    )
    assert rc == 0
    # swanrun copied the authored deck to INPUT + produced the (fake) wave output.
    assert (tmp_path / "swan_out.mat").exists()
    assert "CGRID REGULAR" in (tmp_path / INPUT_FILENAME).read_text()


# ===========================================================================
# (6) REGRESSION: bottom-grid SIGN convention + the all-dry no-op guard.
# ===========================================================================
# These pin the THIRD SWAN bug (live 2026-06-23 Mexico Beach 33 ms no-op): SWAN
# "prepares computation", runs ZERO sweeps, writes no swan_out.mat when the bottom
# grid is ALL DRY (every cell below DEPMIN). The two things that must hold:
#   (a) The worker's DEM sampler negates positive-UP NAVD88 elevation -> SWAN's
#       positive-DOWN depth, so an offshore (below-datum) point renders WET.
#   (b) The worker's all-dry guard FAILS FAST (SWAN_ALL_DRY_GRID) on a bottom grid
#       with no wet cell, naming depth min/max -- instead of an opaque SWAN no-op.
def test_bottom_sign_convention_offshore_point_is_wet():
    """The DEM sampler convention (depth = -elevation) must render a known offshore
    point as WET (depth >= DEPMIN). Mexico Beach offshore (Gulf) sits tens of
    metres below NAVD88: elevation ~ -20 m -> depth +20 m -> deep wet sea. Land
    (elevation > 0 -> negative depth) must render DRY. This is the exact sign that,
    if inverted (or fed a land-only DEM), produces the all-dry no-op.
    """
    spec = parse_build_spec(_spec(mx=4, my=4))

    # The entrypoint's _build_depth_fn does ``return -elev``; replicate that here so
    # the convention is pinned at the deck-author boundary the worker samples onto.
    # Offshore (south, lower-lat half of the bbox) is below datum; nearshore/land
    # (north, upper-lat half) is above datum.
    def depth_fn(lon, lat):
        # NAVD88 positive-up elevation: -25 m offshore (south), +3 m on land (north).
        mid_lat = 0.5 * (_AOI[1] + _AOI[3])
        elevation = -25.0 if lat < mid_lat else 3.0
        return -elevation  # the worker's positive-up -> positive-down negation.

    text = render_bottom_input(spec, depth_fn=depth_fn)
    vals = [float(v) for row in text.splitlines() if row.strip() for v in row.split()]
    wet = [d for d in vals if d >= SWAN_DEPMIN_M]
    dry = [d for d in vals if d < SWAN_DEPMIN_M]
    # The offshore (below-datum) cells MUST be wet with a positive, deep depth.
    assert wet, "no wet cells -- the all-dry no-op signature; sign convention is wrong"
    assert max(wet) == pytest.approx(25.0, abs=1e-6), (
        "a -25 m NAVD88 (offshore) elevation must render as +25 m positive-down "
        "DEPTH (wet); a wrong sign would render it dry"
    )
    # Land (positive elevation) cells render negative depth -> dry.
    assert dry, "expected the land (above-datum) cells to render as dry"
    assert min(dry) == pytest.approx(-3.0, abs=1e-6)


def test_all_dry_guard_fires_and_passes():
    """The worker's all-dry guard must RAISE SwanAllDryGridError (code
    SWAN_ALL_DRY_GRID) on a bottom grid with no wet cell -- and PASS a grid with at
    least one wet cell, returning depth min/max/wet/total. This converts the opaque
    SWAN no-op into an actionable, typed failure.
    """
    from services.workers.swan.entrypoint import (
        SwanAllDryGridError,
        _assert_bottom_has_wet_cells,
    )

    # An ALL-DRY bottom grid (every cell a land-only positive elevation -> negative
    # depth, the fetch_dem-land-only fallback signature) must trip the guard.
    dry_dir = Path(tempfile.mkdtemp())
    (dry_dir / "bottom.bot").write_text("-3.0 -2.5 -4.0\n-1.0 -0.5 -2.0\n")
    with pytest.raises(SwanAllDryGridError) as ei:
        _assert_bottom_has_wet_cells(dry_dir)
    assert ei.value.error_code == "SWAN_ALL_DRY_GRID"
    msg = str(ei.value)
    assert "ALL DRY" in msg
    # The error names the depth range so the operator can diagnose.
    assert "-4.000" in msg and "-0.500" in msg

    # A grid with at least one wet (>= DEPMIN) cell must PASS and report the stats.
    wet_dir = Path(tempfile.mkdtemp())
    (wet_dir / "bottom.bot").write_text("-3.0 12.0 -4.0\n0.10 25.0 -2.0\n")
    depth_min, depth_max, wet, total = _assert_bottom_has_wet_cells(wet_dir)
    assert total == 6
    assert wet == 3  # 12.0, 0.10, 25.0 are >= DEPMIN (0.05)
    assert depth_max == pytest.approx(25.0, abs=1e-6)
    assert depth_min == pytest.approx(-4.0, abs=1e-6)


def test_all_dry_guard_excludes_exception_sentinel():
    """The all-dry guard must DROP SWAN's exception/no-data sentinel (-999.0) from
    the depth stats so a grid of all-sentinel + a single real wet cell still passes
    correctly (the sentinel is not a real -999 m-deep ``wet`` cell, nor a dry one).
    """
    from services.workers.swan.deck_builder import SWAN_EXCEPTION_VALUE
    from services.workers.swan.entrypoint import _assert_bottom_has_wet_cells

    d = Path(tempfile.mkdtemp())
    (d / "bottom.bot").write_text(
        f"{SWAN_EXCEPTION_VALUE:.1f} 8.0 {SWAN_EXCEPTION_VALUE:.1f}\n"
    )
    depth_min, depth_max, wet, total = _assert_bottom_has_wet_cells(d)
    # Only the single real (8.0 m) cell is counted; the two -999 sentinels dropped.
    assert total == 1
    assert wet == 1
    assert depth_min == pytest.approx(8.0, abs=1e-6)
    assert depth_max == pytest.approx(8.0, abs=1e-6)
