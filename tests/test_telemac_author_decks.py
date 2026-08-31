"""The authored TELEMAC decks, read back as text (offline; no solve, no network).

These are the assertions the worker's own deck tests made, repointed at the
server author. What they pin is the grammar the compiled parsers demand and the
per-class wiring an in-image solve exposed - a guessed format crashes DAMOCLES or
NESTOR's Fortran readers, and the failure names the wrong line.
"""

from __future__ import annotations

import re

import pytest

from trid3nt_server.workflows.telemac.steps import author as A

_BED = {"bed_top_m": 100.0, "bed_drop_m": 3.0}
_ORDER = ("outflow", "inflow")
_SOURCE = (500.0, 0.0)
#: A straight centerline in metres - enough for a channel box and a profile fence.
_CENTERLINE = [(x, 0.0) for x in range(0, 1100, 100)]
#: The reach's mapped water: a 60 m wide ribbon along that centerline. A dredge
#: field is cut out of THIS, never out of a width nobody surveyed.
_REACH_POLYGON = [(-50.0, -30.0), (1050.0, -30.0), (1050.0, 30.0), (-50.0, 30.0)]


def _author(tmp_path, **deck) -> str:
    base = {"name": "reach", "inflow_q_m3s": 50.0, "init_depth_m": 2.0,
            "duration_s": 3600.0, "time_step_s": 1.0}
    A.author_reach_deck(
        tmp_path, deck={**base, **deck}, geometry="mesh.slf",
        boundary="mesh.cli", results="r2d.slf", cas_name="t2d_river.cas",
        liquid_boundary_order=_ORDER, bed=_BED, source_utm=_SOURCE,
        centerline_utm=_CENTERLINE, reach_polygon_utm=_REACH_POLYGON)
    return (tmp_path / "t2d_river.cas").read_text()


# --------------------------------------------------------------------------- #
# The plain tracer deck: what every run writes, and what none of them add.
# --------------------------------------------------------------------------- #
def test_the_plain_deck_couples_nothing_and_writes_no_steering(tmp_path):
    cas = _author(tmp_path)
    assert "COUPLING WITH" not in cas
    assert "WATER QUALITY PROCESS" not in cas
    assert "OIL SPILL STEERING FILE" not in cas
    assert "VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B,T1'" in cas
    assert not (tmp_path / A.WAQTEL_FILENAME).exists()
    assert not (tmp_path / A.GAIA_STEERING_FILENAME).exists()


def test_the_prescribed_lists_follow_the_measured_order(tmp_path):
    """The walk numbers the outflow FIRST here, so the flowrate must be second.

    This is the whole reason the order is measured: written inflow-first, the
    deck would put the discharge on the downstream cap and drive the reach
    backwards, and nothing in the run would say so.
    """
    cas = _author(tmp_path)
    assert "PRESCRIBED FLOWRATES            = 0.0;50.0" in cas
    assert "PRESCRIBED ELEVATIONS           = 99.000;0.0" in cas


def test_every_deck_line_is_inside_the_parser_limit(tmp_path):
    cas = _author(tmp_path, name="longview_cowlitz_county_washington_98632_us")
    assert all(len(line) <= 72 for line in cas.splitlines())


def test_the_unset_physics_keeps_its_historical_literals(tmp_path):
    cas = _author(tmp_path)
    assert "LAW OF BOTTOM FRICTION          = 3" in cas
    assert "FRICTION COEFFICIENT            = 33." in cas
    assert "FRICTION COEFFICIENT            = 33.0" not in cas
    assert "VELOCITY DIFFUSIVITY            = 1.E-1" in cas
    assert "COEFFICIENT FOR DIFFUSION OF TRACERS     = 1.E-1" in cas


def test_a_set_physics_value_flows_through_as_a_real(tmp_path):
    cas = _author(tmp_path, friction_law=4, friction_coefficient=40,
                  velocity_diffusivity=0.5, tracer_diffusivity=0.25)
    assert "LAW OF BOTTOM FRICTION          = 4" in cas
    assert "FRICTION COEFFICIENT            = 40." in cas
    assert "VELOCITY DIFFUSIVITY            = 0.5" in cas
    assert "COEFFICIENT FOR DIFFUSION OF TRACERS     = 0.25" in cas


def test_the_source_file_carries_the_pulse_then_stops(tmp_path):
    _author(tmp_path, dye_conc_mgl=100.0, pulse_window_s=300.0)
    src = (tmp_path / A.SOURCES_FILENAME).read_text()
    assert "TR(1,1)" in src
    assert "100.000" in src
    assert src.rstrip().endswith("0.0 0.0")


# --------------------------------------------------------------------------- #
# Wind and rain: emitted only when asked for.
# --------------------------------------------------------------------------- #
def test_no_wind_asked_for_writes_no_wind_block(tmp_path):
    cas = _author(tmp_path)
    assert "WIND" not in cas


def test_a_westerly_drives_the_water_eastward(tmp_path):
    cas = _author(tmp_path, wind_speed_mps=12.0, wind_dir_from_deg=270.0)
    assert "WIND                            = YES" in cas
    assert "OPTION FOR WIND                 = 1" in cas
    assert "THRESHOLD DEPTH FOR WIND        = 1." in cas
    assert "COEFFICIENT OF WIND INFLUENCE" not in cas
    along_x = re.search(r"WIND VELOCITY ALONG X\s+= (\S+)", cas)
    along_y = re.search(r"WIND VELOCITY ALONG Y\s+= (\S+)", cas)
    assert float(along_x.group(1)) > 11.0
    assert abs(float(along_y.group(1))) < 1e-6


def test_rain_is_absent_until_a_rate_is_stated(tmp_path):
    assert "RAIN OR EVAPORATION" not in _author(tmp_path)


def test_rain_states_a_clean_concentration_for_every_tracer(tmp_path):
    cas = _author(tmp_path, rain_or_evap_mm_per_day=120.0)
    assert "RAIN OR EVAPORATION             = YES" in cas
    assert "RAIN OR EVAPORATION IN MM PER DAY = 120." in cas
    assert "VALUES OF TRACERS IN THE RAIN   = 0." in cas
    sag = _author(tmp_path, substance_class="do_sag",
                  rain_or_evap_mm_per_day=-8.0)
    assert "RAIN OR EVAPORATION IN MM PER DAY = -8." in sag
    assert "VALUES OF TRACERS IN THE RAIN   = 0.;0.;0.;0." in sag


# --------------------------------------------------------------------------- #
# Continuation: the engine's own restart, authored into the deck.
# --------------------------------------------------------------------------- #
def test_a_run_that_starts_fresh_names_no_previous_computation(tmp_path):
    assert "PREVIOUS COMPUTATION FILE" not in _author(tmp_path)


def test_a_continued_run_names_the_previous_computation_file(tmp_path):
    """The file alone IS the continuation from release 9.0 - no arming boolean.

    The removed keyword is not merely redundant: DAMOCLES refuses a word its
    dictionary does not carry, so writing it would fail the deck it was meant to
    continue.
    """
    cas = _author(tmp_path, continue_from="previous.slf")
    assert "PREVIOUS COMPUTATION FILE       = previous.slf" in cas
    assert "COMPUTATION CONTINUED" not in cas


def test_the_deck_names_a_file_rather_than_wherever_it_came_from():
    """The engine opens a name in its own run directory, never a URI."""
    assert A.continuation_block("s3://runs/01J/r2d_river.slf") == (
        "PREVIOUS COMPUTATION FILE       = r2d_river.slf\n")
    assert A.continuation_block(None) == ""


# --------------------------------------------------------------------------- #
# WAQTEL: decay on the unchanged tracer, and the oxygen sag.
# --------------------------------------------------------------------------- #
def test_decay_rides_the_unchanged_tracer(tmp_path):
    cas = _author(tmp_path, substance_class="decay", decay_law=1, decay_coef=2.0)
    assert "COUPLING WITH" in cas and "'WAQTEL'" in cas
    assert "WATER QUALITY PROCESS" in cas and "= 17" in cas
    assert A.WAQTEL_FILENAME in cas
    assert "NUMBER OF TRACERS               = 1" in cas
    body = (tmp_path / A.WAQTEL_FILENAME).read_text()
    assert "LAW OF TRACERS DEGRADATION           = 1" in body
    assert "COEFFICIENT 1 FOR LAW OF TRACERS DEGRADATION = 2" in body
    assert all(len(line) <= 72 for line in body.splitlines())


def test_the_oxygen_sag_outputs_and_sizes_its_four_tracers(tmp_path):
    cas = _author(tmp_path, substance_class="do_sag", do_sag_bod_mgl=25.0,
                  do_sag_upstream_do_mgl=8.0)
    assert "WATER QUALITY PROCESS           = 2" in cas
    assert "VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B,T1,T2,T3,T4'" in cas
    assert "INITIAL VALUES OF TRACERS       = 0.;8;0.;0." in cas
    prescribed = [ln for ln in cas.splitlines()
                  if ln.startswith("PRESCRIBED TRACERS VALUES")][0]
    assert prescribed.count(";") == 7  # four values on each of two boundaries
    assert "25" in prescribed and "8" in prescribed
    # do_sag rides in at the boundary, so there is no point-source pulse to
    # collide with the module's four tracers.
    assert "SOURCES FILE" not in cas
    assert not (tmp_path / A.SOURCES_FILENAME).exists()


def test_the_oxygen_steering_zeroes_everything_but_the_sag_pair(tmp_path):
    _author(tmp_path, substance_class="do_sag", do_k1_per_day=0.35,
            do_sat_mgl=9.1)
    body = (tmp_path / A.WAQTEL_FILENAME).read_text()
    assert "CONSTANT OF DEGRADATION OF ORGANIC LOAD K1    = 0.35" in body
    assert "O2 SATURATION DENSITY OF WATER (CS)           = 9.1" in body
    assert "FORMULA FOR COMPUTING K2                      = 0" in body
    assert "CONSTANT OF NITRIFICATION KINETIC K4          = 0." in body
    assert "BENTHIC DEMAND                                = 0." in body
    assert "PHOTOSYNTHESIS P                              = 0." in body
    assert "VEGETAL RESPIRATION R                         = 0." in body
    assert "WATER SALINITY                                = 0." in body


# --------------------------------------------------------------------------- #
# GAIA: three shapes, one selector.
# --------------------------------------------------------------------------- #
def test_a_supply_limited_suspension_appends_a_second_tracer(tmp_path):
    cas = _author(tmp_path, substance_class="sediment", dye_conc_mgl=2000.0)
    assert "COUPLING WITH" in cas and "'GAIA'" in cas
    assert A.GAIA_STEERING_FILENAME in cas
    assert "VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B,T1,T2'" in cas
    prescribed = [ln for ln in cas.splitlines()
                  if ln.startswith("PRESCRIBED TRACERS VALUES")][0]
    assert prescribed.count(";") == 3  # two tracers on each of two boundaries
    body = (tmp_path / A.GAIA_STEERING_FILENAME).read_text()
    assert "SUSPENSION FOR ALL SANDS        = YES" in body
    assert "BED LOAD FOR ALL SANDS          = NO" in body
    assert "CLASSES TYPE OF SEDIMENT        = NCO" in body
    assert "CLASSES SEDIMENT DIAMETERS      = 0.0002" in body
    assert "LAYERS INITIAL THICKNESS        = 0." in body
    assert "SUSPENDED SEDIMENTS CONCENTRATION VALUES AT THE SOURCES = 2" in body


def test_an_erodible_bed_turns_bedload_on_and_suspension_off(tmp_path):
    cas = _author(tmp_path, substance_class="sediment", erodible_bed=True,
                  bed_thickness_m=5.0)
    # no suspended class means the tracer count and the printouts stay as they were
    assert "VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B,T1'" in cas
    body = (tmp_path / A.GAIA_STEERING_FILENAME).read_text()
    assert "SUSPENSION FOR ALL SANDS        = NO" in body
    assert "BED LOAD FOR ALL SANDS          = YES" in body
    assert "BED-LOAD TRANSPORT FORMULA FOR ALL SANDS = 1" in body
    assert "LAYERS INITIAL THICKNESS        = 5" in body
    assert "SUSPENDED SEDIMENTS CONCENTRATION VALUES AT THE SOURCES" not in body


def test_a_mixture_sorts_and_reports_its_mean_diameter(tmp_path):
    _author(tmp_path, substance_class="sediment",
            sediment_gradation=[[100, 1.0], [400, 1.0], [1200, 1.0]])
    body = (tmp_path / A.GAIA_STEERING_FILENAME).read_text()
    assert "CLASSES TYPE OF SEDIMENT        = NCO;NCO;NCO" in body
    assert "CLASSES SEDIMENT DIAMETERS      = 0.0001;0.0004;0.0012" in body
    assert "HIDING FACTOR FORMULA           = 1" in body
    assert "D50" in body
    assert "SUSPENSION FOR ALL SANDS        = NO" in body


@pytest.mark.parametrize("raw", [[], [[200, 1.0]], None])
def test_fewer_than_two_classes_is_not_a_mixture(raw):
    assert A.normalize_gradation(raw) == []


def test_a_gradation_is_sorted_clamped_and_renormalized():
    graded = A.normalize_gradation([[1000, 2.0], [100, 1.0], [400, 1.0]])
    assert [um for um, _ in graded] == [100.0, 400.0, 1000.0]
    assert abs(sum(fr for _, fr in graded) - 1.0) < 1e-9
    clamped = A.normalize_gradation([[1, 1.0], [9000, 1.0]])
    assert [um for um, _ in clamped] == [5.0, 2000.0]


# --------------------------------------------------------------------------- #
# NESTOR: the grammar its own Fortran readers demand.
# --------------------------------------------------------------------------- #
def test_dredging_names_its_three_files_and_stamps_a_time_origin(tmp_path):
    cas = _author(tmp_path, substance_class="sediment", erodible_bed=True,
                  dredging=True)
    assert "ORIGINAL DATE OF TIME           = 2024;1;1" in cas
    assert "ORIGINAL HOUR OF TIME           = 0;0;0" in cas
    body = (tmp_path / A.GAIA_STEERING_FILENAME).read_text()
    assert "NESTOR                          = YES" in body
    assert "NESTOR ACTION FILE              = nestor.act" in body
    assert "NESTOR POLYGON FILE             = nestor.pol" in body
    assert "NESTOR SURFACE REFERENCE FILE   = nestor.ref" in body


def test_the_scheduled_action_digs_a_volume_over_a_window(tmp_path):
    _author(tmp_path, substance_class="sediment", erodible_bed=True,
            dredging=True, dredge_mode="scheduled", dredge_volume_m3=4000.0)
    act = (tmp_path / "nestor.act").read_text()
    assert "RESTART = F" in act  # a Fortran logical, never the deck's YES/NO
    assert "ActionType      = Dig_by_time" in act
    assert "FieldDig        = 101_channel" in act
    assert "DigVolume       = 4000" in act
    assert act.rstrip().endswith("ENDFILE")
    dates = re.findall(r"Time(?:Start|End)\s+= (\S+)", act)
    assert dates and all(len(d) == 19 for d in dates)


def test_the_criterion_action_reads_its_grade_from_the_profiles(tmp_path):
    _author(tmp_path, substance_class="sediment", erodible_bed=True,
            dredging=True, dredge_mode="criterion", dredge_disposal=True)
    act = (tmp_path / "nestor.act").read_text()
    assert "ActionType      = Dig_by_criterion" in act
    assert "ReferenceLevel  = SECTIONS" in act
    assert "FieldDump       = 102_spoil" in act
    assert "DumpRate" in act


def test_the_polygon_file_names_each_field_and_terminates(tmp_path):
    _author(tmp_path, substance_class="sediment", erodible_bed=True,
            dredging=True, dredge_disposal=True)
    pol = (tmp_path / "nestor.pol").read_text()
    assert "NAME 101_channel" in pol
    assert "NAME 102_spoil" in pol
    assert pol.rstrip().endswith("ENDFILE")


def test_every_surface_reference_profile_carries_seven_reals(tmp_path):
    _author(tmp_path, substance_class="sediment", erodible_bed=True,
            dredging=True)
    lines = (tmp_path / "nestor.ref").read_text().splitlines()
    profiles = [ln for ln in lines if not ln.startswith(("#", "END"))]
    assert len(profiles) >= 2
    assert all(len(ln.split()) == 7 for ln in profiles)
    assert any(ln.startswith("END") for ln in lines)


def _dredge(tmp_path, **deck):
    return A.author_reach_deck(
        tmp_path, deck={"name": "reach", "duration_s": 3600.0,
                        "substance_class": "sediment", "erodible_bed": True,
                        "dredging": True, **deck},
        geometry="mesh.slf", boundary="mesh.cli", results="r2d.slf",
        cas_name="t2d_river.cas", liquid_boundary_order=_ORDER, bed=_BED,
        source_utm=_SOURCE, centerline_utm=_CENTERLINE,
        reach_polygon_utm=deck.pop("polygon", _REACH_POLYGON))


def test_the_dig_field_auto_fills_from_the_water_held_back_from_its_banks(tmp_path):
    """The setback is the one mechanism: the field is the water, minus the banks."""
    written = _dredge(tmp_path, dredge_bank_offset_m=5.0,
                      dredge_zone_len_m=200.0)["nestor"]
    assert written["dredge_zone_source"] == "auto"
    assert written["dredge_bank_offset_m"] == 5.0
    ys = [float(ln.split()[1]) for ln in
          (tmp_path / "nestor.pol").read_text().splitlines()
          if re.fullmatch(r"-?[\d.]+ -?[\d.]+", ln)]
    # 60 m of water, 5 m off each bank -> the cut spans the middle 50 m.
    assert max(ys) == pytest.approx(25.0, abs=0.01)
    assert min(ys) == pytest.approx(-25.0, abs=0.01)


def test_a_setback_wider_than_the_channel_excludes_the_stretch_itself(tmp_path):
    """Too narrow is not a rule: the shrunken polygon simply vanishes there."""
    with pytest.raises(A.DeckAuthorError) as excinfo:
        _dredge(tmp_path, dredge_bank_offset_m=40.0)
    assert excinfo.value.error_code == "TELEMAC_DREDGE_ZONE_TOO_NARROW"
    message = str(excinfo.value)
    assert "40 m bank setback" in message and "60.0 m across" in message


def test_a_supplied_polygon_wins_and_is_validated_inside_the_water(tmp_path):
    inside = [(400.0, -10.0), (600.0, -10.0), (600.0, 10.0), (400.0, 10.0)]
    written = _dredge(tmp_path, dredge_zone_utm=inside)["nestor"]
    assert written["dredge_zone_source"] == "supplied"


def test_a_supplied_polygon_on_dry_land_refuses(tmp_path):
    outside = [(400.0, 200.0), (600.0, 200.0), (600.0, 260.0), (400.0, 260.0)]
    with pytest.raises(A.DeckAuthorError) as excinfo:
        _dredge(tmp_path, dredge_zone_utm=outside)
    assert excinfo.value.error_code == "TELEMAC_DREDGE_ZONE_OUTSIDE_WATER"


def test_a_dredge_run_with_no_reach_polygon_refuses(tmp_path):
    """The field is cut out of mapped water; with none there is nothing to cut."""
    with pytest.raises(A.DeckAuthorError) as excinfo:
        _dredge(tmp_path, polygon=None)
    assert excinfo.value.error_code == "TELEMAC_DREDGE_ZONE_UNMAPPED"


# --------------------------------------------------------------------------- #
# Oil: the module rides on the tracer solve, with its release compiled in.
# --------------------------------------------------------------------------- #
def test_oil_names_its_steering_and_moves_the_release_into_the_fortran(tmp_path):
    cas = _author(tmp_path, substance_class="oil", oil_preset="diesel",
                  oil_release_step=600, n_drogues=100, drogues_period_s=60)
    assert "OIL SPILL STEERING FILE         = oil_spill.txt" in cas
    assert "FORTRAN FILE                    = user_fortran" in cas
    assert "MAXIMUM NUMBER OF DROGUES       = 100" in cas
    assert "ASCII DROGUES FILE              = drogues.txt" in cas
    steering = (tmp_path / "oil_spill.txt").read_text()
    assert steering.startswith("DIESEL - trid3nt oil preset")
    fortran = (tmp_path / "user_fortran" / "oil_flot.f").read_text()
    assert "IF(LT.EQ.600)" in fortran
    assert f"COORD_X={_SOURCE[0]:.0f}.D0" in fortran
    assert "COORD_X=497173.D0" not in fortran  # the module's own demo release


# --------------------------------------------------------------------------- #
# Rain on grid: three paths, and the deck says which.
# --------------------------------------------------------------------------- #
def _rog(tmp_path, **kwargs) -> str:
    A.author_rog_deck(
        tmp_path, deck={"name": "creek", "duration_s": 7200.0,
                        "time_step_s": 2.0, "graphic_period": 100},
        geometry="rog.slf", boundary="rog.cli", results="r2d_rog.slf",
        cas_name="t2d_rog.cas", cn_map="rog_cn_map.dat",
        friction_laws="rog_friction.tbl", zones_file="rog_zones.dat",
        rain_mm_per_day=48.0, **kwargs)
    return (tmp_path / "t2d_rog.cas").read_text()


def test_the_native_path_runs_the_engines_own_infiltration(tmp_path):
    cas = _rog(tmp_path, runoff_path="native")
    assert "RAINFALL-RUNOFF MODEL           = 1" in cas
    assert "ANTECEDENT MOISTURE CONDITIONS  = 2" in cas
    assert "FORMATTED DATA FILE 2           = rog_cn_map.dat" in cas
    assert "FORMATTED DATA FILE 1" not in cas
    assert "NUMBER OF TRACERS               = 0" in cas
    assert "LAW OF BOTTOM FRICTION          = 4" in cas
    assert "INITIAL CONDITIONS              = 'ZERO ELEVATION'" in cas


def test_the_preprocessed_path_takes_no_second_abstraction(tmp_path):
    cas = _rog(tmp_path, runoff_path="preprocessing")
    assert "RAINFALL-RUNOFF MODEL           = 0" in cas
    assert "ANTECEDENT MOISTURE CONDITIONS" not in cas
    assert "FORMATTED DATA FILE 2" not in cas


def test_the_time_varying_path_names_the_image_baked_raindef3_fortran(tmp_path):
    """RAINDEF is a compile-time PARAMETER; the baked patch is the only door."""
    cas = _rog(tmp_path, runoff_path="native", hyetograph_file="rog_hyeto.txt")
    assert "FORMATTED DATA FILE 1           = rog_hyeto.txt" in cas
    # QUOTED: an absolute path starting with '/' reads as a COMMENT to
    # damocles, which erases the keyword and swallows the line after it.
    assert f"FORTRAN FILE                    = '{A.RAINDEF3_USER_FORTRAN}'" in cas
    assert A.RAINDEF3_USER_FORTRAN.startswith("/")
    assert A.RAINDEF3_USER_FORTRAN.endswith("/raindef3")
    assert "RAINFALL-RUNOFF MODEL           = 1" in cas
    # the hyetograph carries its own dry tail, so no rain window is stated
    assert "DURATION OF RAIN OR EVAPORATION IN HOURS" not in cas


def test_a_constant_rain_run_stages_no_fortran_at_all(tmp_path):
    assert "FORTRAN FILE" not in _rog(tmp_path, runoff_path="native")


def test_a_rain_window_shorter_than_the_run_lets_the_catchment_drain(tmp_path):
    A.author_rog_deck(
        tmp_path, deck={"name": "creek", "duration_s": 7200.0,
                        "rain_duration_s": 1800.0},
        geometry="rog.slf", boundary="rog.cli", results="r2d_rog.slf",
        cas_name="t2d_rog.cas", cn_map="cn.dat", friction_laws="f.tbl",
        zones_file="z.dat", rain_mm_per_day=48.0, runoff_path="native")
    cas = (tmp_path / "t2d_rog.cas").read_text()
    assert "DURATION OF RAIN OR EVAPORATION IN HOURS = 0.5" in cas


def test_the_friction_pair_zones_the_distinct_manning_values(tmp_path):
    stats = A.write_friction_files(
        tmp_path, laws_basename="f.tbl", zones_basename="z.dat",
        manning_per_node=[0.035, 0.035, 0.1, 0.06])
    assert stats["n_zones"] == 3
    laws = (tmp_path / "f.tbl").read_text()
    assert "1 MANNING 0.035 NULL" in laws
    assert laws.rstrip().endswith("END")
    zones = (tmp_path / "z.dat").read_text().split()
    assert zones[:4] == ["1", "1", "2", "1"]  # node 1 -> zone 1


def test_the_curve_number_scatter_must_cover_every_node(tmp_path):
    with pytest.raises(A.DeckAuthorError) as excinfo:
        A.write_cn_map(tmp_path, "cn.dat", x=[0.0, 1.0], y=[0.0, 1.0],
                       cn2=[70.0])
    assert excinfo.value.error_code == "TELEMAC_ROG_CN_LENGTH_MISMATCH"


def test_the_hyetograph_appends_a_dry_tail_past_the_last_instant(tmp_path):
    stats = A.write_hyetograph_file(tmp_path, "hyeto.txt",
                                    blocks=[[1800.0, 12.0], [3600.0, 4.0]],
                                    duration_s=7200.0)
    assert stats["n_blocks"] == 3
    assert stats["hyetograph_total_mm"] == 16.0
    rows = (tmp_path / "hyeto.txt").read_text().splitlines()
    assert rows[-1].startswith("10800.000") and rows[-1].endswith("0.00000")


@pytest.mark.parametrize("blocks,code", [
    ([[1800.0, 5.0], [900.0, 5.0]], "TELEMAC_ROG_HYETO_NONMONOTONE"),
    ([[1800.0, -1.0]], "TELEMAC_ROG_HYETO_NEGATIVE"),
    ([], "TELEMAC_ROG_HYETO_EMPTY"),
])
def test_a_hyetograph_that_is_not_one_refuses(tmp_path, blocks, code):
    with pytest.raises(A.DeckAuthorError) as excinfo:
        A.write_hyetograph_file(tmp_path, "hyeto.txt", blocks=blocks,
                                duration_s=3600.0)
    assert excinfo.value.error_code == code
