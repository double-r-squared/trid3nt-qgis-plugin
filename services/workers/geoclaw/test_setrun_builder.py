"""Unit tests for the GeoClaw deck author (``setrun_builder``) — sprint-17.

The GeoClaw analogue of ``services/workers/modflow/test_gwt_adapter.py``. These
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

from services.workers.geoclaw.setrun_builder import (
    GeoClawBuildSpec,
    GeoClawDeckError,
    build_geoclaw_deck,
    parse_build_spec,
    render_maketopo_dtopo,
    render_makefile,
    render_qinit_data,
    render_setrun_py,
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
    # sea_level offset is the surge v0.1 fallback.
    assert "geo_data.sea_level = 1.5" in text


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


def test_build_surge_deck_writes_setrun_and_makefile_only(tmp_path: Path):
    manifest = build_geoclaw_deck(_spec(scenario="surge", sea_level_m=2.0), tmp_path)
    assert (tmp_path / "setrun.py").exists()
    assert (tmp_path / "Makefile").exists()
    assert not (tmp_path / "qinit.xyz").exists()
    assert not (tmp_path / "maketopo.py").exists()
    # surge writes no scenario source file -- only the setrun.py + the Makefile.
    assert manifest.files_written == ["setrun.py", "Makefile"]


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
    # the canonical GeoClaw helper, not a hand-rolled np.linspace box.
    assert "fault.create_dtopo_xy(dx=1/60., buffer_size=2.0)" in text
    assert "np.linspace" not in text
    # coordinate_specification stays 'centroid' (Okada requires it).
    assert 'subfault.coordinate_specification = "centroid"' in text


def test_render_maketopo_defaults_print_non_site_specific_banner():
    # No fault geometry supplied -> synthetic defaults + the honesty banner.
    spec = parse_build_spec(_spec(scenario="tsunami"))
    text = render_maketopo_dtopo(spec)
    ast.parse(text)
    assert "NON-SITE-SPECIFIC synthetic source" in text
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
    assert "NON-SITE-SPECIFIC synthetic source" not in text


def test_render_maketopo_partial_fault_geometry_still_banners():
    # Only strike supplied -> the other three default -> banner names them.
    spec = parse_build_spec(_spec(scenario="tsunami", fault_strike_deg=180.0))
    text = render_maketopo_dtopo(spec)
    ast.parse(text)
    assert "subfault.strike = 180.0" in text  # user value
    assert "NON-SITE-SPECIFIC synthetic source" in text
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
