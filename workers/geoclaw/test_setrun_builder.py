"""Unit tests for the GeoClaw deck author (``setrun_builder``) — sprint-17.

The GeoClaw analogue of ``workers/modflow/test_gwt_adapter.py``. These
pin the DETERMINISTIC, clawpack-free deck-authoring core:

  1. build_spec validation — typed error on missing/invalid fields.
  2. setrun.py generation — the rendered module is valid Python with the
     load-bearing GeoClaw blocks (clawdata domain/grid/output, geo_data,
     topofiles, amrdata) wired from the spec, per scenario.
  3. scenario source files — dam_break writes qinit.xyz, tsunami (synthetic)
     writes maketopo.py, surge writes neither.
  4. full deck build into a tmp dir + the DeckManifest provenance.

NO clawpack / gfortran is required — the deck author never imports them (the
rendered maketopo.py does, but is only EXECUTED by the entrypoint, never here).
We py-compile the rendered setrun.py to prove it is syntactically valid Python.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from workers.geoclaw.setrun_builder import (
    FGMAX_MASK_FILENAME,
    GeoClawBuildSpec,
    GeoClawDeckError,
    build_geoclaw_deck,
    fgmax_grid_geom,
    parse_build_spec,
    render_maketopo_dtopo,
    render_makefile,
    render_qinit_data,
    render_setrun_py,
    render_storm_file,
    resolve_storm_track,
)

_AOI = [-85.75, 29.55, -85.25, 30.20]  # Mexico Beach-ish demo box


def _spec(**over) -> dict:
    base = {
        "scenario": "dam_break",
        "bbox": list(_AOI),
        "topo_file": "topo.asc",
        "sim_duration_s": 1800.0,
        "output_frames": 12,
        "amr_levels": 2,
        "manning_n": 0.03,
        "sea_level_m": 0.0,
        "base_num_cells": [30, 30],
        "dam_break_depth_m": 8.0,
    }
    base.update(over)
    return base


# ===========================================================================
# (1) build_spec validation.
# ===========================================================================
def test_parse_valid_spec_fills_defaults():
    spec = parse_build_spec({"bbox": _AOI, "topo_file": "t.asc"})
    assert isinstance(spec, GeoClawBuildSpec)
    assert spec.scenario == "dam_break"  # default
    assert spec.output_frames == 24
    assert spec.amr_levels == 2
    assert spec.bbox == tuple(_AOI)


def test_parse_rejects_unknown_top_level_field():
    """an unknown build_spec field errors loudly (the lesson
    was a stale image SILENTLY dropping unknown fields -- two knob templates
    ran as no-ops). Never a silent drop."""
    with pytest.raises(GeoClawDeckError) as ei:
        parse_build_spec(_spec(typo_field_name=1.0))
    assert ei.value.error_code == "GEOCLAW_SPEC_UNKNOWN_FIELDS"
    assert "typo_field_name" in str(ei.value)


def test_parse_rejects_bad_scenario():
    with pytest.raises(GeoClawDeckError) as ei:
        parse_build_spec(_spec(scenario="nope"))
    assert ei.value.error_code == "GEOCLAW_SPEC_INVALID"


def test_parse_rejects_bad_bbox():
    # wrong length
    with pytest.raises(GeoClawDeckError):
        parse_build_spec({"bbox": [1, 2, 3], "topo_file": "t.asc"})
    # min >= max
    with pytest.raises(GeoClawDeckError):
        parse_build_spec({"bbox": [10, 10, 5, 5], "topo_file": "t.asc"})


def test_parse_requires_topo_file():
    with pytest.raises(GeoClawDeckError) as ei:
        parse_build_spec({"bbox": _AOI})
    assert ei.value.error_code == "GEOCLAW_SPEC_INVALID"


def test_parse_rejects_nonpositive_duration_and_frames():
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_spec(sim_duration_s=0))
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_spec(output_frames=0))


def test_parse_reads_optional_domain_bbox():
    dom = [-86.5, 28.9, -85.0, 30.5]
    spec = parse_build_spec(_spec(scenario="tsunami", domain_bbox=dom))
    assert spec.domain_bbox == tuple(dom)
    # default absent -> None (domain falls back to bbox).
    spec2 = parse_build_spec(_spec(scenario="tsunami"))
    assert spec2.domain_bbox is None


def test_parse_rejects_bad_domain_bbox():
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_spec(domain_bbox=[1, 2, 3]))  # wrong length
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_spec(domain_bbox=[10, 10, 5, 5]))  # min >= max


def test_render_setrun_domain_bbox_drives_clawdata_aoi_drives_region_fgmax():
    # An offshore-extended domain: clawdata bounds span the DOMAIN; the region +
    # fgmax + gauge stay on the (smaller) AOI bbox -> the wave propagates from the
    # offshore source across the domain and runs up the refined AOI coast.
    dom = [-86.50, 28.90, -85.00, 30.50]
    src = [-86.30, 29.80]  # offshore (west), inside the domain, outside the AOI
    spec = parse_build_spec(
        _spec(scenario="tsunami", domain_bbox=dom, source_lonlat=src)
    )
    text = render_setrun_py(spec)
    ast.parse(text)
    # clawdata bounds = the DOMAIN (not the AOI).
    assert "clawdata.lower[0] = -86.5" in text
    assert "clawdata.upper[0] = -85.0" in text
    assert "clawdata.lower[1] = 28.9" in text
    assert "clawdata.upper[1] = 30.5" in text
    # The fine-AMR region pins the AOI extent (not the domain).
    assert "-85.75" in text and "-85.25" in text  # AOI lon edges in region/fgmax
    # The region line references the AOI bounds.
    assert ", -85.75, -85.25, 29.55, 30.2])" in text  # regiondata region over AOI
    # fgmax monitor x1/x2 anchored to the AOI lon edges (half-cell inset).
    assert "fg.x1 = -85.75 +" in text
    assert "fg.x2 = -85.25 -" in text


def test_render_setrun_domain_defaults_to_aoi_when_absent():
    # No domain_bbox -> clawdata bounds == AOI bbox (back-compat).
    spec = parse_build_spec(_spec(scenario="tsunami"))
    text = render_setrun_py(spec)
    assert "clawdata.lower[0] = -85.75" in text
    assert "clawdata.upper[0] = -85.25" in text


# ===========================================================================
# (2) setrun.py generation — valid Python + load-bearing blocks.
# ===========================================================================
def test_render_setrun_is_valid_python_dam_break():
    spec = parse_build_spec(_spec(scenario="dam_break"))
    text = render_setrun_py(spec)
    # Must parse as valid Python (proves no f-string / quoting break).
    ast.parse(text)
    # The clawpack import is INSIDE the generated module (executed only by the
    # entrypoint), not in the author module.
    assert "from clawpack.clawutil import data" in text
    assert "def setrun(" in text
    assert "def setgeo(" in text
    # Domain wired from bbox.
    assert "clawdata.lower[0] = -85.75" in text
    assert "clawdata.upper[0] = -85.25" in text
    assert "clawdata.lower[1] = 29.55" in text
    assert "clawdata.upper[1] = 30.2" in text
    # Base grid + output frames wired from spec.
    assert "clawdata.num_cells[0] = 30" in text
    assert "clawdata.num_output_times = 12" in text
    assert "clawdata.tfinal = 1800.0" in text
    # geo_data: lat/lon coordinate system + manning + sea level.
    assert "geo_data.coordinate_system = 2" in text
    assert "geo_data.manning_coefficient = 0.03" in text
    assert "geo_data.sea_level = 0.0" in text
    # topofile wired.
    assert "topo_data.topofiles.append([3, 'topo.asc'])" in text
    # AMR levels.
    assert "amrdata.amr_levels_max = 2" in text
    # dam_break -> qinit block present (topotype-1 file, single-element list:
    # GeoClaw read_qinit only parses bare x y z; QinitData.write requires len-1).
    assert "qinit_data.qinit_type = 4" in text
    assert "qinit_data.qinitfiles.append(['qinit.xyz'])" in text
    assert "qinit.tt3" not in text


def test_render_setrun_tsunami_has_dtopo_block_not_qinit():
    spec = parse_build_spec(_spec(scenario="tsunami", source_magnitude=8.2))
    text = render_setrun_py(spec)
    ast.parse(text)
    assert "dtopo_data.dtopofiles" in text
    assert "dtopo.tt3" in text
    assert "qinit_data.qinit_type" not in text


def test_render_setrun_surge_has_neither_qinit_nor_dtopo():
    spec = parse_build_spec(_spec(scenario="surge", sea_level_m=1.5))
    text = render_setrun_py(spec)
    ast.parse(text)
    assert "qinit_data.qinit_type" not in text
    assert "dtopo_data.dtopofiles" not in text
    assert "geo_data.sea_level = 1.5" in text


# ===========================================================================
# (surge) parametric-Holland wind + pressure forcing machinery.
# ===========================================================================
def _surge_spec(**over) -> dict:
    base = {
        "scenario": "surge",
        "bbox": list(_AOI),
        "topo_file": "topo.asc",
        "sim_duration_s": 54000.0,
        "t0_s": -43200.0,
        "output_frames": 10,
        "amr_levels": 2,
        "wind_drag_law": "garratt",
        "storm_track": [
            [-43200.0, -85.6, 27.0, 45.0, 46000.0, 96000.0, 500000.0],
            [0.0, -85.5, 29.8, 49.0, 46000.0, 95000.0, 500000.0],
            [10800.0, -85.4, 31.0, 40.0, 46000.0, 96000.0, 500000.0],
        ],
    }
    base.update(over)
    return base


def test_render_setrun_surge_wires_wind_and_pressure_forcing():
    text = render_setrun_py(parse_build_spec(_surge_spec()))
    ast.parse(text)
    assert "surge_data = rundata.surge_data" in text
    assert "surge_data.wind_forcing = True" in text
    assert "surge_data.pressure_forcing = True" in text
    assert "surge_data.storm_specification_type = 'holland80'" in text
    assert "storm.storm" in text
    # surge geo physics constants + the storm aux layout (3 shallow + 1 friction
    # + 3 storm = 7) matching surge_data's default wind/pressure indices.
    assert "geo_data.coriolis_forcing = True" in text
    assert "clawdata.num_aux = 7" in text
    assert 'amrdata.aux_type = ["center", "capacity", "yleft", "center", "center", "center", "center"]' in text
    # the run opens BEFORE landfall (t0 < 0) so the storm spins up.
    assert "clawdata.t0 = -43200.0" in text
    assert "clawdata.tfinal = 10800.0" in text


@pytest.mark.parametrize("law,code", [("none", 0), ("garratt", 1), ("powell", 2)])
def test_render_setrun_surge_drag_law_selects_distinct_code(law, code):
    text = render_setrun_py(parse_build_spec(_surge_spec(wind_drag_law=law)))
    assert f"surge_data.drag_law = {code}" in text


def test_parse_rejects_bad_drag_law():
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_surge_spec(wind_drag_law="quadratic"))


def test_parse_rejects_nonascending_storm_track():
    bad = _surge_spec(storm_track=[
        [0.0, -85.5, 29.8, 45.0, 46000.0, 96000.0, 500000.0],
        [-3600.0, -85.6, 27.0, 45.0, 46000.0, 96000.0, 500000.0],  # goes backwards
    ])
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(bad)


def test_render_storm_file_geoclaw_format():
    spec = parse_build_spec(_surge_spec())
    txt = render_storm_file(spec)
    lines = txt.splitlines()
    assert int(lines[0]) == 3               # num_casts
    assert lines[1].strip() == "0.0"        # time_offset (times are s from landfall)
    assert lines[2].strip() == ""           # blank header line
    rows = [r for r in lines[3:] if r.strip()]
    assert len(rows) == 3
    assert all(len(r.split()) == 7 for r in rows)  # 7 GeoClaw storm columns
    # first data row is the earliest track point (t = -43200 s).
    assert float(rows[0].split()[0]) == -43200.0


def test_resolve_storm_track_synthesizes_demo_when_absent():
    spec = parse_build_spec(_surge_spec(storm_track=[]))
    track, is_synth = resolve_storm_track(spec)
    assert is_synth is True
    assert len(track) >= 2
    # the demo track brackets the run window [t0, t0 + duration].
    assert track[0][0] <= spec.t0_s
    assert track[-1][0] >= spec.t0_s + spec.sim_duration_s
    # a user track is passed through verbatim (not synthesized).
    spec2 = parse_build_spec(_surge_spec())
    track2, is_synth2 = resolve_storm_track(spec2)
    assert is_synth2 is False
    assert len(track2) == 3


def test_surge_deck_stores_storm_file_and_drag_provenance(tmp_path: Path):
    manifest = build_geoclaw_deck(_surge_spec(wind_drag_law="powell"), tmp_path)
    assert "storm.storm" in manifest.files_written
    assert (tmp_path / "storm.storm").exists()
    assert "powell" in manifest.driver_descriptor


def test_non_surge_scenarios_keep_three_aux_byte_layout():
    # dam_break / tsunami keep the 3-aux shallow layout + t0 == 0 (byte-neutral).
    for scen in ("dam_break", "tsunami"):
        text = render_setrun_py(parse_build_spec(_spec(scenario=scen)))
        assert "clawdata.num_aux = 3" in text
        assert 'amrdata.aux_type = ["center", "capacity", "yleft"]' in text
        assert "clawdata.t0 = 0.0" in text
        assert "surge_data" not in text


def test_render_setrun_amr_ratios_scale_with_levels():
    spec = parse_build_spec(_spec(amr_levels=3))
    text = render_setrun_py(spec)
    ast.parse(text)
    # 3 levels -> 2 refinement ratios (between consecutive levels), INCREASING
    # toward the finest level (first transition 2x, then 4x) -- not a flat all-2s.
    assert "amrdata.refinement_ratios_x = [2, 4]" in text
    assert "amrdata.refinement_ratios_y = [2, 4]" in text
    assert "amrdata.refinement_ratios_t = [2, 4]" in text


def test_render_setrun_amr_ratios_increase_for_deeper_levels():
    spec = parse_build_spec(_spec(amr_levels=4))
    text = render_setrun_py(spec)
    ast.parse(text)
    # 4 levels -> 3 transitions; ratios increase (2, then 4 for every deeper
    # transition) so coarse levels stay cheap and the finest resolve the front.
    assert "amrdata.refinement_ratios_x = [2, 4, 4]" in text


# ===========================================================================
# (3) scenario source-file renders.
# ===========================================================================
def test_render_qinit_is_topotype1_xyz_with_raised_column():
    spec = parse_build_spec(_spec(scenario="dam_break", dam_break_depth_m=7.0))
    xyz = render_qinit_data(spec)
    lines = [r for r in xyz.splitlines() if r.strip()]
    # TOPOTYPE-1: bare `x y z` triples, NO header (the only form read_qinit takes).
    assert all(len(r.split()) == 3 for r in lines)
    assert len(lines) == 16 * 16  # 16x16 perturbation grid
    zs = [float(r.split()[2]) for r in lines]
    # the raised column reaches the dam_break depth at the centre, 0 outside.
    assert max(zs) == 7.0 and min(zs) == 0.0
    # north-first ordering: first row latitude is the maximum.
    ys = [float(r.split()[1]) for r in lines]
    assert ys[0] == max(ys) and ys[-1] == min(ys)


def test_render_maketopo_dtopo_is_valid_python_and_uses_dtopotools():
    spec = parse_build_spec(_spec(scenario="tsunami", source_magnitude=9.0))
    text = render_maketopo_dtopo(spec)
    ast.parse(text)
    assert "from clawpack.geoclaw import dtopotools" in text
    assert "mw = 9.0" in text
    assert 'fault.dtopo.write("dtopo.tt3"' in text
    # The Okada dtopo-authoring front: the helper ALSO writes the
    # final-time vertical deformation dZ as an ESRI-ASCII grid so postprocess can
    # rasterize the coseismic uplift/subsidence PRODUCT (the "what deformation"
    # answer). Assert the numpy import + the ESRI header + the north-first write.
    assert "import numpy as _np" in text
    assert 'open("deformation_dz.asc"' in text
    assert "ncols %d" in text and "cellsize %.12f" in text
    assert "fault.dtopo.dZ" in text


# ===========================================================================
# (3b) the per-application Makefile -- THIS supplies the `.output` target.
# ===========================================================================
def test_render_makefile_provides_output_target_via_includes():
    spec = parse_build_spec(_spec(scenario="dam_break"))
    mk = render_makefile(spec)
    # The load-bearing include: the `.output` rule lives in Makefile.common.
    # Its absence is exactly the live bug ("No rule to make target '.output'").
    # The canonical example reaches it via CLAWMAKE; assert both the binding and
    # the include of it (so $(CLAWMAKE) resolves to Makefile.common).
    assert "CLAWMAKE = $(CLAW)/clawutil/src/Makefile.common" in mk
    assert "include $(CLAWMAKE)" in mk
    # The GeoClaw 2d shallow module/source lists come from Makefile.geoclaw.
    assert "include $(CLAW)/geoclaw/src/2d/shallow/Makefile.geoclaw" in mk
    # REGRESSION (real-solve gate): the Riemann solvers MUST be listed in SOURCES
    # (Makefile.geoclaw does NOT add them) or xgeoclaw fails to link with
    # "undefined reference to rpn2_/rpt2_". Assert all three.
    assert "$(CLAW)/riemann/src/rpn2_geoclaw.f" in mk
    assert "$(CLAW)/riemann/src/rpt2_geoclaw.f" in mk
    assert "$(CLAW)/riemann/src/geoclaw_riemann_utils.f" in mk
    # The required GeoClaw build vars (mirror the canonical example Makefile).
    assert "CLAW_PKG = geoclaw" in mk
    assert "EXE = xgeoclaw" in mk
    assert "SETRUN_FILE = setrun.py" in mk
    assert "OUTDIR = _output" in mk
    # CLAW must be exported in the runtime env for the includes to resolve.
    assert "ifndef CLAW" in mk


def test_render_makefile_is_scenario_agnostic():
    # The build machinery is identical across scenarios -- only the deck data
    # (qinit/dtopo/sea_level) differs, not the Makefile.
    for scen in ("dam_break", "tsunami", "surge"):
        spec = parse_build_spec(_spec(scenario=scen))
        mk = render_makefile(spec)
        assert "CLAWMAKE = $(CLAW)/clawutil/src/Makefile.common" in mk
        assert "include $(CLAWMAKE)" in mk
        assert "CLAW_PKG = geoclaw" in mk


# ===========================================================================
# (4) full deck build into a tmp dir + DeckManifest provenance.
# ===========================================================================
def test_build_dam_break_deck_writes_setrun_and_qinit(tmp_path: Path):
    manifest = build_geoclaw_deck(_spec(scenario="dam_break"), tmp_path)
    assert manifest.scenario == "dam_break"
    assert (tmp_path / "setrun.py").exists()
    assert (tmp_path / "qinit.xyz").exists()
    assert not (tmp_path / "qinit.tt3").exists()
    assert (tmp_path / "deck_manifest.json").exists()
    assert "setrun.py" in manifest.files_written
    assert "qinit.xyz" in manifest.files_written
    assert "dam_break" in manifest.driver_descriptor
    # qinit.xyz is a TOPOTYPE-1 file (bare `x y z`, no header) -- the only form
    # GeoClaw's read_qinit accepts. Every non-blank line is exactly 3 floats.
    qlines = [r for r in (tmp_path / "qinit.xyz").read_text().splitlines() if r.strip()]
    assert all(len(r.split()) == 3 for r in qlines)
    assert float(qlines[0].split()[2]) >= 0.0  # z column parses as a float
    # north-first: the first row's latitude is the max (>= the last row's lat).
    assert float(qlines[0].split()[1]) >= float(qlines[-1].split()[1])
    # The Makefile MUST be written alongside setrun.py so `make .output` has a
    # rule for the `.output` target (the live "No rule to make target" bug).
    assert (tmp_path / "Makefile").exists()
    assert "Makefile" in manifest.files_written
    mk = (tmp_path / "Makefile").read_text()
    assert "CLAWMAKE = $(CLAW)/clawutil/src/Makefile.common" in mk
    assert "include $(CLAWMAKE)" in mk
    assert "CLAW_PKG = geoclaw" in mk
    # the on-disk setrun.py is valid Python.
    ast.parse((tmp_path / "setrun.py").read_text())
    # the persisted manifest round-trips.
    disk = json.loads((tmp_path / "deck_manifest.json").read_text())
    assert disk["scenario"] == "dam_break"
    assert disk["output_frames"] == 12


def test_build_tsunami_synthetic_writes_maketopo(tmp_path: Path):
    manifest = build_geoclaw_deck(_spec(scenario="tsunami"), tmp_path)
    assert (tmp_path / "maketopo.py").exists()
    assert "maketopo.py" in manifest.files_written
    assert "tsunami" in manifest.driver_descriptor
    assert not (tmp_path / "qinit.xyz").exists()


def test_build_tsunami_staged_dtopo_skips_maketopo(tmp_path: Path):
    manifest = build_geoclaw_deck(
        _spec(scenario="tsunami", dtopo_file="my_dtopo.tt3"), tmp_path
    )
    assert not (tmp_path / "maketopo.py").exists()
    assert "staged dtopo" in manifest.driver_descriptor
    # the setrun references the staged dtopo file.
    assert "my_dtopo.tt3" in (tmp_path / "setrun.py").read_text()


def test_build_surge_deck_writes_setrun_makefile_and_storm_file(tmp_path: Path):
    manifest = build_geoclaw_deck(_spec(scenario="surge", sea_level_m=2.0), tmp_path)
    assert (tmp_path / "setrun.py").exists()
    assert (tmp_path / "Makefile").exists()
    assert not (tmp_path / "qinit.xyz").exists()
    assert not (tmp_path / "maketopo.py").exists()
    # surge authors the parametric-Holland storm track file (no user track ->
    # a synthetic demo track), alongside the setrun.py + the Makefile.
    assert (tmp_path / "storm.storm").exists()
    assert manifest.files_written == ["setrun.py", "Makefile", "storm.storm"]
    # GeoClaw storm-file header: <num_casts>\n<time_offset>\n<blank>\n then rows.
    storm_lines = (tmp_path / "storm.storm").read_text().splitlines()
    n_casts = int(storm_lines[0])
    assert n_casts >= 2  # a demo track has multiple forecast rows
    assert storm_lines[1].strip() == "0.0"  # time_offset 0 -> times are s from landfall
    data_rows = [r for r in storm_lines[3:] if r.strip()]
    assert len(data_rows) == n_casts
    assert all(len(r.split()) == 7 for r in data_rows)  # 7 GeoClaw storm columns


def test_source_lonlat_overrides_centroid_in_qinit(tmp_path: Path):
    src = (-85.40, 29.80)
    build_geoclaw_deck(
        _spec(scenario="dam_break", source_lonlat=list(src)), tmp_path
    )
    # topotype-1 qinit: recover the x-range directly from the `x y z` columns.
    lines = [r for r in (tmp_path / "qinit.xyz").read_text().splitlines() if r.strip()]
    xs = [float(r.split()[0]) for r in lines]
    xmin, xmax = min(xs), max(xs)
    # the perturbation grid is centred on the explicit source -> its x-range
    # straddles src lon, distinct from the AOI centroid (-85.5).
    assert xmin < src[0] < xmax
    assert xmax < -85.30  # well left of the AOI centroid box if centred on src


# ===========================================================================
# (5) GAP1 fgmax - max depth/speed/arrival monitor over the AOI.
# ===========================================================================
def test_render_setrun_tsunami_emits_fgmax_block():
    spec = parse_build_spec(_spec(scenario="tsunami", amr_levels=3))
    text = render_setrun_py(spec)
    ast.parse(text)  # the fgmax block must keep the module valid Python.
    # The fgmax import lives in the GENERATED module (only when fgmax is emitted).
    assert "from clawpack.geoclaw import fgmax_tools" in text
    assert "rundata.fgmax_data.num_fgmax_val = 2" in text
    assert "fgmax_tools.FGmaxGrid()" in text
    assert "fg.point_style = 2" in text
    assert "fg.min_level_check = 3" in text  # finest level == amr_levels
    assert "fg.interp_method = 0" in text
    assert "fg.arrival_tol = 0.01" in text  # default fgmax_arrival_tol_m
    assert "fgmax_grids.append(fg)" in text


def test_render_setrun_surge_emits_fgmax_block():
    spec = parse_build_spec(_spec(scenario="surge", sea_level_m=1.5))
    text = render_setrun_py(spec)
    ast.parse(text)
    assert "from clawpack.geoclaw import fgmax_tools" in text
    assert "rundata.fgmax_data.num_fgmax_val = 2" in text
    assert "fgmax_grids.append(fg)" in text


def test_render_setrun_dam_break_has_no_fgmax_block():
    # dam_break has no coastal-arrival concept -> no fgmax import or block.
    spec = parse_build_spec(_spec(scenario="dam_break"))
    text = render_setrun_py(spec)
    ast.parse(text)
    assert "from clawpack.geoclaw import fgmax_tools" not in text
    assert "num_fgmax_val" not in text


def test_render_setrun_fgmax_arrival_tol_threads_from_spec():
    spec = parse_build_spec(_spec(scenario="tsunami", fgmax_arrival_tol_m=0.25))
    text = render_setrun_py(spec)
    ast.parse(text)
    assert "fg.arrival_tol = 0.25" in text


# ===========================================================================
# (6) GAP3 regions + GAP4 gauges - pin finest level + a coastal gauge.
# ===========================================================================
def test_render_setrun_appends_coastal_region_over_aoi():
    spec = parse_build_spec(_spec(scenario="tsunami", amr_levels=3))
    text = render_setrun_py(spec)
    ast.parse(text)
    # The region pins [minlevel, maxlevel, t1, t2, x1, x2, y1, y2] = finest
    # level over the AOI box for the whole run [0, tfinal].
    assert "rundata.regiondata.regions.append([3, 3, 0., 1800.0, " in text
    assert "-85.75, -85.25, 29.55, 30.2])" in text


def test_render_setrun_forces_intermediate_propagation_tier_offshore():
    # The multi-scale tsunami setup: a whole-DOMAIN region FORCES the offshore
    # propagation domain (source->coast corridor + shelf) to an INTERMEDIATE
    # mid-resolution level (so the shoaling wave is resolved as it travels, not
    # damped on the base grid) and caps it at one-below-finest; the costly finest
    # mesh is still created ONLY at the AOI (the second region).
    dom = [-125.65, 41.55, -124.06, 41.88]
    spec = parse_build_spec(
        _spec(scenario="tsunami", amr_levels=4, domain_bbox=dom)
    )
    text = render_setrun_py(spec)
    ast.parse(text)
    # propagation tier region: [propagation_level, amr_levels-1, 0., tfinal,
    # <domain extent>]. For amr_levels=4: propagation_level == 3 (2 above base,
    # capped at one-below-finest == 3) -> forced + capped at level 3.
    assert "rundata.regiondata.regions.append([3, 3, 0., 1800.0, " in text
    assert "-125.65, -124.06, 41.55, 41.88])" in text
    # finest pinned over the AOI: [amr_levels, amr_levels, ...].
    assert "rundata.regiondata.regions.append([4, 4, 0., 1800.0, " in text


def test_render_setrun_propagation_tier_offshore_only_for_deep_nest():
    # A deeper nest (amr_levels=5) keeps the propagation tier at level 3 (2 above
    # base) and caps the offshore domain at one-below-finest (level 4): the tier is
    # FORCED to 3 but the wave front may dynamically refine to 4 over the corridor.
    dom = [-125.65, 41.55, -124.06, 41.88]
    spec = parse_build_spec(
        _spec(scenario="tsunami", amr_levels=5, domain_bbox=dom)
    )
    text = render_setrun_py(spec)
    ast.parse(text)
    assert "rundata.regiondata.regions.append([3, 4, 0., 1800.0, " in text
    # finest pinned over the AOI: [5, 5, ...].
    assert "rundata.regiondata.regions.append([5, 5, 0., 1800.0, " in text


def test_render_setrun_no_propagation_tier_when_domain_equals_aoi():
    # dam_break / a tsunami with NO offshore extension (domain == AOI) keeps the
    # whole-domain region min level 1 (no propagation corridor to resolve) -- those
    # decks stay byte-identical to the pre-propagation-tier behavior.
    spec = parse_build_spec(_spec(scenario="dam_break", amr_levels=4))
    text = render_setrun_py(spec)
    ast.parse(text)
    # min level 1 (NOT forced to the propagation level) when domain == AOI.
    assert "rundata.regiondata.regions.append([1, 3, 0., 1800.0, " in text


def test_render_setrun_appends_gauge_fallback_seaward_edge():
    spec = parse_build_spec(_spec(scenario="tsunami"))
    text = render_setrun_py(spec)
    ast.parse(text)
    # gauge form: [gaugeno, x, y, t1, t2]; fallback x = AOI lon-mid (-85.5).
    assert "rundata.gaugedata.gauges.append([1, -85.5, " in text
    assert ", 0., 1.e10])" in text


def test_render_setrun_appends_explicit_coastal_gauge():
    spec = parse_build_spec(
        _spec(scenario="tsunami", coastal_gauge_lonlat=[-85.42, 29.61])
    )
    text = render_setrun_py(spec)
    ast.parse(text)
    assert "rundata.gaugedata.gauges.append([1, -85.42, 29.61, 0., 1.e10])" in text


# ===========================================================================
# (7) GAP7 nested DEM - primary topo + extra topos, ordered coarse->fine.
# ===========================================================================
def test_render_setrun_appends_extra_topo_files_coarse_to_fine():
    spec = parse_build_spec(
        _spec(
            scenario="tsunami",
            topo_file="coarse.asc",
            extra_topo_files=["mid.asc", "fine.asc"],
        )
    )
    text = render_setrun_py(spec)
    ast.parse(text)
    # primary first, then extras in order -> the ordered appends appear in
    # coarse->fine sequence in the generated setgeo block.
    i_primary = text.index("topo_data.topofiles.append([3, 'coarse.asc'])")
    i_mid = text.index("topo_data.topofiles.append([3, 'mid.asc'])")
    i_fine = text.index("topo_data.topofiles.append([3, 'fine.asc'])")
    assert i_primary < i_mid < i_fine


def test_parse_rejects_non_list_extra_topo_files():
    with pytest.raises(GeoClawDeckError) as ei:
        parse_build_spec(_spec(extra_topo_files="not-a-list.asc"))
    assert ei.value.error_code == "GEOCLAW_SPEC_INVALID"


# ===========================================================================
# (8) GAP6 Okada fault geometry - user-supplied vs synthetic + honesty banner.
# ===========================================================================
def test_render_maketopo_uses_create_dtopo_xy_and_centroid_spec():
    spec = parse_build_spec(_spec(scenario="tsunami", source_magnitude=9.0))
    text = render_maketopo_dtopo(spec)
    ast.parse(text)
    # the canonical GeoClaw helper, not a hand-rolled np.linspace box; the
    # single-subfault degrade rung pins dx=1/60 + a 2.0-deg buffer.
    assert "fault.create_dtopo_xy(dx=_DTOPO_DX, buffer_size=_DTOPO_BUFFER)" in text
    assert "_DTOPO_DX = 1/60." in text
    assert "_DTOPO_BUFFER = 2.0" in text
    assert "np.linspace" not in text
    # coordinate_specification stays 'centroid' (Okada requires it).
    assert 'subfault.coordinate_specification = "centroid"' in text


def test_render_maketopo_defaults_print_non_site_specific_banner():
    # No fault geometry supplied -> synthetic defaults + the honesty banner.
    spec = parse_build_spec(_spec(scenario="tsunami"))
    text = render_maketopo_dtopo(spec)
    ast.parse(text)
    assert "NON-SITE-SPECIFIC synthetic" in text
    # the defaulted geometry uses the synthetic values.
    assert "subfault.strike = 0.0" in text
    assert "subfault.dip = 15.0" in text
    assert "subfault.rake = 90.0" in text
    assert "subfault.depth = 10000.0" in text  # 10 km synthetic default in m


def test_render_maketopo_threads_user_fault_geometry_no_banner():
    spec = parse_build_spec(
        _spec(
            scenario="tsunami",
            fault_strike_deg=210.0,
            fault_dip_deg=20.0,
            fault_rake_deg=95.0,
            fault_depth_km=12.0,
        )
    )
    text = render_maketopo_dtopo(spec)
    ast.parse(text)
    # all four supplied -> user geometry threaded, depth_km -> m, NO banner.
    assert "subfault.strike = 210.0" in text
    assert "subfault.dip = 20.0" in text
    assert "subfault.rake = 95.0" in text
    assert "subfault.depth = 12000.0" in text  # 12 km -> 12000 m
    assert "NON-SITE-SPECIFIC synthetic" not in text


def test_render_maketopo_finite_fault_builds_multi_subfault():
    # finite-fault upgrade: a staged finite_fault_file switches the
    # maketopo to the NATIVE CSVFault multi-subfault reader (measured inversion),
    # NOT the single idealized rectangle. No synthetic banner on this rung.
    spec = parse_build_spec(
        _spec(scenario="tsunami", finite_fault_file="finite_fault.csv")
    )
    text = render_maketopo_dtopo(spec)
    ast.parse(text)
    assert "dtopotools.CSVFault()" in text
    assert 'fault.read(\'finite_fault.csv\', coordinate_specification="centroid")' in text
    assert "MEASURED finite-fault inversion source" in text
    # the finite-fault footprint IS the fault -> a modest 0.5-deg buffer.
    assert "_DTOPO_BUFFER = 0.5" in text
    # NO single-subfault synthesis / no synthetic banner on the measured rung.
    assert "NON-SITE-SPECIFIC synthetic" not in text
    assert "dtopotools.SubFault()" not in text
    # the shared deformation-product tail still fires (the "what deformation" answer).
    assert "deformation_dz.asc" in text


def test_render_maketopo_partial_fault_geometry_still_banners():
    # Only strike supplied -> the other three default -> banner names them.
    spec = parse_build_spec(_spec(scenario="tsunami", fault_strike_deg=180.0))
    text = render_maketopo_dtopo(spec)
    ast.parse(text)
    assert "subfault.strike = 180.0" in text  # user value
    assert "NON-SITE-SPECIFIC synthetic" in text
    assert "dip" in text and "rake" in text and "depth" in text


def test_build_spec_additive_defaults_preserve_behaviour():
    # The new optional fields default to safe no-ops so an old build_spec parses.
    spec = parse_build_spec({"bbox": _AOI, "topo_file": "t.asc"})
    assert spec.extra_topo_files == []
    assert spec.fgmax_arrival_tol_m == 0.01
    assert spec.coastal_gauge_lonlat is None
    assert spec.fault_strike_deg is None
    assert spec.fault_dip_deg is None
    assert spec.fault_rake_deg is None
    assert spec.fault_depth_km is None


# ===========================================================================
# GeoClaw CAND-S knobs: explicit AMR regions + spatially-varying Manning.
# ===========================================================================
def test_amr_regions_appended_after_default_tiers():
    """User AMR windows append to regiondata AFTER the engine default tiers and
    the rendered setrun.py stays valid Python."""
    win = {
        "min_level": 3, "max_level": 3, "t_start_s": 0.0, "t_end_s": 1800.0,
        "min_lon": -124.21, "max_lon": -124.18, "min_lat": 41.745, "max_lat": 41.77,
    }
    spec = parse_build_spec(
        _spec(scenario="tsunami", amr_levels=3, amr_regions=[win],
              domain_bbox=[-124.6, 41.6, -124.1, 41.85])
    )
    assert len(spec.amr_regions) == 1
    txt = render_setrun_py(spec)
    ast.parse(txt)
    # 2 default tiers (propagation + AOI) + 1 explicit user window == 3 appends.
    assert txt.count("regiondata.regions.append") == 3
    assert "3, 3, 0.0, 1800.0, -124.21, -124.18, 41.745, 41.77" in txt


def test_amr_regions_accepts_tuple_form_and_rejects_bad_level_order():
    tup = [2, 4, 0.0, 600.0, -124.2, -124.18, 41.74, 41.76]
    spec = parse_build_spec(_spec(amr_regions=[tup]))
    assert spec.amr_regions[0][:2] == (2.0, 4.0)
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_spec(amr_regions=[[4, 2, 0.0, 600.0, -124.2, -124.18, 41.74, 41.76]]))
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_spec(amr_regions=[[3, 3, 0.0, 600.0]]))  # wrong arity


def test_no_amr_regions_is_byte_identical_default():
    """An empty amr_regions list keeps the 2 default region tiers (back-compat)."""
    spec = parse_build_spec(_spec(scenario="tsunami", amr_levels=3,
                                  domain_bbox=[-124.6, 41.6, -124.1, 41.85]))
    txt = render_setrun_py(spec)
    assert txt.count("regiondata.regions.append") == 2


def test_banded_manning_emits_list_and_break():
    spec = parse_build_spec(_spec(manning_coefficients=[0.015, 0.06], manning_break=[0.0]))
    assert spec.manning_coefficients == [0.015, 0.06]
    assert spec.manning_break == [0.0]
    txt = render_setrun_py(spec)
    ast.parse(txt)
    assert "geo_data.manning_coefficient = [0.015, 0.06]" in txt
    assert "geo_data.manning_break = [0.0]" in txt


def test_scalar_manning_back_compat_unchanged():
    """No manning_coefficients -> the single scalar coefficient path (no break)."""
    spec = parse_build_spec(_spec(manning_n=0.033))
    txt = render_setrun_py(spec)
    assert "geo_data.manning_coefficient = 0.033" in txt
    assert "manning_break" not in txt


def test_banded_manning_rejects_bad_break_length_and_nonpositive():
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_spec(manning_coefficients=[0.02, 0.05], manning_break=[0.0, 1.0]))
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_spec(manning_coefficients=[0.02, -0.01], manning_break=[0.0]))
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_spec(manning_coefficients=[0.01, 0.02, 0.03], manning_break=[1.0, 0.0]))  # not ascending


def test_banded_manning_form_matches_fortran_consumer_contract():
    """The emitted geo_data.manning_coefficient list + manning_break must satisfy
    EXACTLY what clawpack 5.14.0 consumes, so GeoClawData.write authors
    ``geoclaw.data`` and the Fortran friction selector activate the banded form.

    Fortran side (geoclaw/src/2d/shallow/geoclaw_module.f90 + src2.f90):
      - GeoClawData.write raises unless len(manning_break) == len(coeffs) - 1, then
        writes ``num_manning = len(coeffs)`` + the two lists.
      - geoclaw_module allocates manning_break(num_manning), sets the TOP band to
        +inf, reads the num_manning-1 breaks below it.
      - src2 selects, per WET cell of topography B: ``do nman = num_manning,1,-1;
        if (B < manning_break(nman)) coeff = manning_coefficient(nman)`` -- so a
        single break at 0.0 gives coeff[0] offshore (B < 0) and coeff[1] onshore
        (B >= 0). N coefficients therefore REQUIRE exactly N-1 breaks.

    This test pins the RENDER form (clawpack-free) that makes that chain fire; the
    live smoke confirms the banded run differs from the scalar run.
    """
    coeffs = [0.015, 0.06]  # [offshore B<0, onshore B>=0]
    breaks = [0.0]          # split at the still-water datum
    spec = parse_build_spec(
        _spec(scenario="tsunami", manning_coefficients=coeffs, manning_break=breaks)
    )
    # parse keeps N coefficients + exactly N-1 breaks (the GeoClawData.write gate).
    assert spec.manning_coefficients == coeffs
    assert len(spec.manning_break) == len(spec.manning_coefficients) - 1
    txt = render_setrun_py(spec)
    ast.parse(txt)
    # the deck assigns the LIST (not a scalar) -> num_manning = 2 in geoclaw.data.
    assert "geo_data.manning_coefficient = [0.015, 0.06]" in txt
    assert "geo_data.manning_break = [0.0]" in txt
    # friction must be ON (else src2 never enters the selector) and unbounded in
    # depth (friction applied to every wet cell).
    assert "geo_data.friction_forcing = True" in txt
    assert "geo_data.friction_depth = 1.0e6" in txt


# ===========================================================================
# AMR window governs refinement: the AOI ambient drops one level below a window
# so an in-AOI window is demonstrably finer than its surroundings.
# ===========================================================================
def test_amr_window_lowers_aoi_ambient_one_below_the_window():
    """With an explicit window the AOI default region pins amr_levels-1 (ambient)
    and the window pins its own (finer) level, so the window is NOT subsumed."""
    win = {"min_level": 4, "max_level": 4, "t_start_s": 0.0, "t_end_s": 900.0,
           "min_lon": -85.6, "max_lon": -85.5, "min_lat": 29.7, "max_lat": 29.8}
    spec = parse_build_spec(
        _spec(scenario="tsunami", amr_levels=4, amr_regions=[win],
              domain_bbox=[-86.5, 28.9, -85.0, 30.5])
    )
    txt = render_setrun_py(spec)
    ast.parse(txt)
    # AOI default region pinned to the ambient level (amr_levels-1 == 3), NOT 4.
    assert "rundata.regiondata.regions.append([3, 3, 0., 1800.0, -85.75, -85.25, 29.55, 30.2])" in txt
    # the explicit window pins the finer level (4) over its box.
    assert "4, 4, 0.0, 900.0, -85.6, -85.5, 29.7, 29.8" in txt
    # amr_levels_max covers the finest window level.
    assert "amrdata.amr_levels_max = 4" in txt
    # fgmax monitors at the AOI ambient level (3) so the whole AOI is captured.
    assert "fg.min_level_check = 3" in txt


def test_amr_window_can_exceed_the_requested_finest_raising_max_levels():
    """A window asking for a level BEYOND amr_levels raises amr_levels_max to cover
    it (a bounded sub-box), while the AOI ambient stays amr_levels-1."""
    win = {"min_level": 5, "max_level": 5, "t_start_s": 0.0, "t_end_s": 900.0,
           "min_lon": -85.6, "max_lon": -85.5, "min_lat": 29.7, "max_lat": 29.8}
    spec = parse_build_spec(
        _spec(scenario="tsunami", amr_levels=3, amr_regions=[win],
              domain_bbox=[-86.5, 28.9, -85.0, 30.5])
    )
    txt = render_setrun_py(spec)
    ast.parse(txt)
    # amr_levels_max raised to the window level (5); AOI ambient = amr_levels-1 (2).
    assert "amrdata.amr_levels_max = 5" in txt
    assert "rundata.regiondata.regions.append([2, 2, 0., 1800.0, -85.75, -85.25, 29.55, 30.2])" in txt
    assert "5, 5, 0.0, 900.0, -85.6, -85.5, 29.7, 29.8" in txt


def test_no_amr_window_keeps_aoi_at_the_requested_finest():
    """No windows -> the AOI default region stays at amr_levels (byte-identical to
    the pre-deck) and amr_levels_max == amr_levels."""
    spec = parse_build_spec(_spec(scenario="tsunami", amr_levels=3,
                                  domain_bbox=[-86.5, 28.9, -85.0, 30.5]))
    txt = render_setrun_py(spec)
    ast.parse(txt)
    assert "rundata.regiondata.regions.append([3, 3, 0., 1800.0, -85.75, -85.25, 29.55, 30.2])" in txt
    assert "amrdata.amr_levels_max = 3" in txt
    assert "fg.min_level_check = 3" in txt


# ===========================================================================
# GeoClaw CAND-S tail: Lagrangian particle gauges + onshore fgmax.
# ===========================================================================
def test_lagrangian_particles_emit_gauges_and_per_gauge_gtype():
    """Seeded particles become gtype='lagrangian' gauges (ids 100+); the coastal
    gauge (id 1) is absent from the gtype dict -> stays stationary."""
    spec = parse_build_spec(
        _spec(scenario="tsunami", lagrangian_particles=[[-85.5, 29.8], [-85.45, 29.82]])
    )
    assert spec.lagrangian_particles == [(-85.5, 29.8), (-85.45, 29.82)]
    txt = render_setrun_py(spec)
    ast.parse(txt)
    # the stationary coastal gauge (id 1) is still emitted.
    assert "rundata.gaugedata.gauges.append([1, " in txt
    # two Lagrangian particle gauges, ids 100 and 101.
    assert "rundata.gaugedata.gauges.append([100, -85.5, 29.8, 0., 1.e10])" in txt
    assert "rundata.gaugedata.gauges.append([101, -85.45, 29.82, 0., 1.e10])" in txt
    # a per-gauge gtype dict marks ONLY the particle gauges lagrangian.
    assert "rundata.gaugedata.gtype = {100: 'lagrangian', 101: 'lagrangian'}" in txt


def test_no_lagrangian_particles_is_byte_identical_default():
    """No particles -> no gtype dict, no extra gauges (byte-identical deck)."""
    spec = parse_build_spec(_spec(scenario="tsunami"))
    txt = render_setrun_py(spec)
    assert "gaugedata.gtype" not in txt
    assert "gauges.append([100," not in txt


def test_fgmax_mask_onshore_emits_point_style_4_and_xy_fname():
    """fgmax_mask='onshore' -> point_style=4 referencing the topotype-3 mask file,
    NOT the point_style=2 uniform grid."""
    spec = parse_build_spec(_spec(scenario="tsunami", amr_levels=3, fgmax_mask="onshore"))
    assert spec.fgmax_mask == "onshore"
    txt = render_setrun_py(spec)
    ast.parse(txt)
    assert "fg.point_style = 4" in txt
    assert f"fg.xy_fname = {FGMAX_MASK_FILENAME!r}" in txt
    assert "fg.point_style = 2" not in txt
    assert "rundata.fgmax_data.num_fgmax_val = 2" in txt
    assert "fg.min_level_check = 3" in txt
    assert "fg.arrival_tol = 0.01" in txt


def test_fgmax_mask_full_is_byte_identical_point_style_2():
    """Default fgmax_mask='full' keeps the point_style=2 uniform grid (unchanged)."""
    spec_full = parse_build_spec(_spec(scenario="tsunami", amr_levels=3))
    spec_default = parse_build_spec(_spec(scenario="tsunami", amr_levels=3))
    assert spec_full.fgmax_mask == "full"
    txt = render_setrun_py(spec_default)
    assert "fg.point_style = 2" in txt
    assert "point_style = 4" not in txt
    assert FGMAX_MASK_FILENAME not in txt


def test_fgmax_grid_geom_dx_matches_emitted_point_style_2_grid():
    """The shared fgmax_grid_geom dx (used by the entrypoint onshore-mask builder)
    equals the dx_fine the point_style=2 setrun emits -- so the onshore mask lands
    on the SAME grid as the full grid (common cells match)."""
    spec = parse_build_spec(
        _spec(scenario="tsunami", amr_levels=3, domain_bbox=[-86.5, 28.9, -85.0, 30.5])
    )
    geom = fgmax_grid_geom(spec)
    txt = render_setrun_py(spec)
    # extract the emitted `dx_fine = <value>` from the point_style=2 block.
    line = next(l for l in txt.splitlines() if "dx_fine =" in l and "AOI ambient" in l)
    emitted_dx = float(line.split("dx_fine =")[1].split("#")[0].strip())
    assert abs(geom["dx"] - emitted_dx) < 1e-15
    assert geom["nx"] >= 2 and geom["ny"] >= 2


def test_parse_rejects_bad_fgmax_mask_and_bad_particles():
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_spec(fgmax_mask="sideways"))
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_spec(lagrangian_particles=[[-85.5]]))  # not a 2-tuple
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_spec(lagrangian_particles="nope"))


def test_cand_s_tail_additive_defaults_preserve_behaviour():
    spec = parse_build_spec({"bbox": _AOI, "topo_file": "t.asc"})
    assert spec.lagrangian_particles == []
    assert spec.fgmax_mask == "full"
    assert spec.fgout_frames == 0


# ===========================================================================
# (fgout) SMOOTH fixed-grid animation frames.
# ===========================================================================
def test_render_setrun_no_fgout_block_by_default():
    """A spec without fgout_frames emits NO fgout block -- byte-identical to a
    pre-fgout deck (the additive-off invariant)."""
    for scen in ("dam_break", "tsunami", "surge"):
        over = {"scenario": scen}
        if scen == "surge":
            over = _surge_spec()
        text = render_setrun_py(parse_build_spec(_spec(**over) if scen != "surge" else over))
        assert "fgout_tools" not in text
        assert "fgout_data.fgout_grids" not in text


def test_render_setrun_tsunami_emits_fgout_block_when_frames_set():
    spec = parse_build_spec(_spec(scenario="tsunami", fgout_frames=20))
    text = render_setrun_py(spec)
    ast.parse(text)
    assert "from clawpack.geoclaw import fgout_tools" in text
    assert "rundata.fgout_data.fgout_grids" in text
    assert "fgout.point_style = 2" in text
    assert "fgout.output_format = 'ascii'" in text
    assert "fgout.nout = 20" in text
    assert "fgout.fgno = 1" in text


def test_render_setrun_surge_emits_fgout_block_when_frames_set():
    text = render_setrun_py(parse_build_spec(_surge_spec(fgout_frames=8)))
    ast.parse(text)
    assert "fgout_tools" in text
    assert "fgout.nout = 8" in text
    # tend/tstart follow the surge window (t0 < 0 .. t0 + duration).
    assert "fgout.tstart = -43200.0" in text


def test_render_setrun_dam_break_never_emits_fgout_even_with_frames():
    """dam_break has no coastal-animation concept; fgout is tsunami/surge only."""
    text = render_setrun_py(parse_build_spec(_spec(scenario="dam_break", fgout_frames=12)))
    assert "fgout_tools" not in text


def test_fgout_grid_shares_fgmax_resolution():
    """The fgout uniform grid uses the AOI-ambient dx (same as fgmax) so the two
    monitors share a resolution."""
    spec = parse_build_spec(_spec(scenario="tsunami", amr_levels=3, fgout_frames=10))
    geom = fgmax_grid_geom(spec)
    text = render_setrun_py(spec)
    gnx_line = next(l for l in text.splitlines() if l.strip().startswith("fgout.nx ="))
    gnx = int(gnx_line.split("=")[1].strip())
    # nx from AOI span / dx_fgmax (fgmax grid geom dx), within one cell of fgmax nx.
    assert abs(gnx - geom["nx"]) <= 1


def test_parse_rejects_negative_fgout_frames():
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_spec(fgout_frames=-1))


# --------------------------------------------------------------------------- #
# Thacker paraboloid-basin V&V deck -- spec-5, Cartesian, frictionless.
# --------------------------------------------------------------------------- #
def _thacker_spec(**over):
    base = {
        "scenario": "thacker", "bbox": [-1.6, -1.6, 1.6, 1.6], "topo_file": "topo.asc",
        "sim_duration_s": 5.6, "output_frames": 20, "amr_levels": 3,
        "base_num_cells": [60, 60],
        "bowl_a_m": 1.0, "bowl_h0_m": 0.1, "bowl_eta_amp": 0.5,
    }
    base.update(over)
    return base


def test_thacker_parse_requires_bowl_params():
    from workers.geoclaw.setrun_builder import parse_build_spec, GeoClawDeckError
    import pytest
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_thacker_spec(bowl_a_m=None))
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_thacker_spec(bowl_eta_amp=1.5))  # out of (0,1)
    spec = parse_build_spec(_thacker_spec())
    assert spec.scenario == "thacker"
    assert spec.bowl_a_m == 1.0 and spec.bowl_h0_m == 0.1 and spec.bowl_eta_amp == 0.5


def test_bowl_params_rejected_for_geographic_scenario():
    from workers.geoclaw.setrun_builder import parse_build_spec, GeoClawDeckError
    import pytest
    raw = {"scenario": "tsunami", "bbox": [-124.3, 41.7, -124.1, 41.8],
           "topo_file": "topo.asc", "bowl_a_m": 1.0}
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(raw)


def test_thacker_setrun_is_cartesian_frictionless_walled():
    from workers.geoclaw.setrun_builder import parse_build_spec, render_setrun_py
    sr = render_setrun_py(parse_build_spec(_thacker_spec()))
    assert "geo_data.coordinate_system = 1" in sr
    assert "clawdata.capa_index = 0" in sr
    assert "clawdata.num_aux = 1" in sr
    assert "geo_data.friction_forcing = False" in sr
    assert sr.count('= "wall"') == 4  # all four boundaries closed
    assert "qinit_data.qinit_type = 4" in sr
    assert 'topo_data.topofiles.append([1, "topo.asc"])' in sr
    assert "gauges.append([1, 0.0, 0.0" in sr  # center gauge
    # NO offshore/fgmax/fgout machinery in a thacker deck.
    assert "fgmax" not in sr and "fgout" not in sr and "coordinate_system = 2" not in sr


def test_thacker_deck_writes_topo_and_qinit_matching_analytic(tmp_path):
    from workers.geoclaw.setrun_builder import build_geoclaw_deck
    from trid3nt_contracts.geoclaw_thacker import thacker_bed_elevation, thacker_eta
    m = build_geoclaw_deck(_thacker_spec(), tmp_path)
    assert "topo.asc" in m.files_written and "qinit.xyz" in m.files_written

    def _nearest(fn, tx, ty):
        best, bz, bd = None, None, 1e18
        for ln in (tmp_path / fn).read_text().splitlines():
            x, y, z = (float(v) for v in ln.split())
            d = (x - tx) ** 2 + (y - ty) ** 2
            if d < bd:
                bd, best, bz = d, (x, y), z
        return bz

    # topo == paraboloid bed; qinit == analytic t=0 surface (center + off-center).
    assert abs(_nearest("topo.asc", 0, 0) - thacker_bed_elevation(0, 0, 1.0, 0.1)) < 1e-6
    assert abs(_nearest("qinit.xyz", 0, 0) - thacker_eta(0, 0, 0, 1.0, 0.1, 0.5)) < 1e-6
    assert abs(_nearest("qinit.xyz", 0.5, 0) - thacker_eta(0.5, 0, 0, 1.0, 0.1, 0.5)) < 2e-3


# ===========================================================================
# Boussinesq (SGN) dispersive variant — num_eqn=5 + Makefile.bouss + PETSc.
# ===========================================================================
def test_bouss_sgn_deck_sets_num_eqn5_and_boussdata():
    from workers.geoclaw.setrun_builder import render_setrun_py, render_makefile

    spec = parse_build_spec(
        _spec(
            scenario="tsunami",
            bouss_equations=2,
            bouss_min_depth=20.0,
            bouss_min_level=1,
            bouss_max_level=3,
        )
    )
    sr = render_setrun_py(spec)
    ast.parse(sr)  # still valid Python
    assert "clawdata.num_eqn = 5" in sr
    assert "BoussData" in sr and "bouss_equations = 2" in sr
    assert "bouss_min_depth = 20.0" in sr
    assert "bouss_max_level = 3" in sr
    assert "bouss_solver = 3" in sr  # PETSc
    mk = render_makefile(spec)
    assert "Makefile.bouss" in mk and "Makefile.geoclaw" not in mk
    assert "HAVE_PETSC" in mk and "CLAW_MPIFC" in mk


def test_swe_default_stays_num_eqn3_and_no_bouss_block():
    from workers.geoclaw.setrun_builder import render_setrun_py, render_makefile

    spec = parse_build_spec(_spec(scenario="tsunami"))
    sr = render_setrun_py(spec)
    assert "clawdata.num_eqn = 3" in sr and "BoussData" not in sr
    mk = render_makefile(spec)
    assert "Makefile.geoclaw" in mk and "Makefile.bouss" not in mk


def test_bouss_full_deck_manifest_and_makefile(tmp_path):
    from workers.geoclaw.setrun_builder import build_geoclaw_deck

    m = build_geoclaw_deck(
        _spec(scenario="tsunami", bouss_equations=2, bouss_min_depth=15.0), tmp_path
    )
    assert m.bouss_equations == 2
    assert "Makefile.bouss" in (tmp_path / "Makefile").read_text()
    dm = json.loads((tmp_path / "deck_manifest.json").read_text())
    assert dm["bouss_equations"] == 2


@pytest.mark.parametrize(
    "over",
    [
        {"bouss_equations": 3},  # out of {0,1,2}
        {"scenario": "thacker", "bowl_a_m": 1e6, "bowl_h0_m": 100.0,
         "bowl_eta_amp": 0.1, "bouss_equations": 2},  # thacker incompatible
        {"bouss_equations": 2, "bouss_min_level": 5, "bouss_max_level": 2},  # min>max
        {"bouss_equations": 2, "bouss_min_depth": 0.0},  # depth must be > 0
    ],
)
def test_bouss_invalid_specs_rejected(over):
    with pytest.raises(GeoClawDeckError):
        parse_build_spec(_spec(**over))
