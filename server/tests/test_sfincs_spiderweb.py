"""Offline unit tests for the parametric hurricane spiderweb (SFINCS) module.

Covers (per the job's OFFLINE-TESTS mandate):

1. Holland field vs PUBLISHED Hurricane Michael landfall values (vmax / RMW /
   pc), tolerance-bounded.
2. spw header/format golden (Delft3D meteo_on_spiderweb_grid FileVersion 1.03,
   n_quantity 3) + the docker-proven sfincs.inp keyword shape.
3. tref-overlap assert (a spw window that does not overlap the deck window is a
   typed SpiderwebError, job-0248 class).
4. Emitter XOR — a spiderweb ForcingSpec refuses a co-present wind/pressure
   member (SFINCSSetupError SPIDERWEB_FORCING_CONFLICT).
5. Quadtree backend-gate — local-docker coastal is NOT forced to quadtree,
   aws-batch coastal STILL is (both directions).
6. fetch_storm_tracks radii parse — USA_RMW/POCI/ROCI/R34_* carried per fix,
   blank-tolerant.

All pure-offline (no docker, no network). The docker spw/utmzone uptake +
water-response is proven separately in the job's Phase-1b smoke.
"""

from __future__ import annotations

import datetime as dt

import pytest

from trid3nt_server.workflows import sfincs_spiderweb as S


# --------------------------------------------------------------------------- #
# Fixtures — a synthetic Michael-like best track (intensifying to landfall).
# --------------------------------------------------------------------------- #


def _michael_like_fixes():
    """A 72-h synthetic track peaking at pc 919 mb / vmax 140 kt near landfall.

    Blank RMW/POCI so the Knaff-Zehr + standard-atmosphere fallbacks fire (the
    common IBTrACS reality). 6-hourly cadence, tracking north across the shelf.
    """
    base = dt.datetime(2018, 10, 8, 0, 0, 0, tzinfo=dt.timezone.utc)
    fixes = []
    for h in range(0, 72, 6):
        # triangular intensity ramp, peak at h=54 (landfall proxy)
        frac = max(0.0, 1.0 - abs((h - 54) / 54.0))
        fixes.append(
            {
                "lon": -85.5,
                "lat": 22.0 + 0.13 * h,
                "iso_time": (base + dt.timedelta(hours=h)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "wind_kt": 60.0 + 80.0 * frac,   # peaks ~140 kt
                "pres_mb": 1000.0 - 81.0 * frac,  # bottoms ~919 mb
                "name": "MICHAEL",
            }
        )
    return fixes


# --------------------------------------------------------------------------- #
# 1. Holland core numbers vs published Michael.
# --------------------------------------------------------------------------- #


def test_holland_core_numbers_vs_published_michael(tmp_path):
    res = S.build_spiderweb_from_fixes(
        _michael_like_fixes(),
        (-85.9, 29.3, -84.9, 30.2),
        out_dir=str(tmp_path),
        deck_sim_hours=48.0,
        storm_name="Michael",
    )
    prov = res.provenance
    # vmax: 140 kt 1-min -> 10-min *0.93 *0.514 ~= 67 m/s (published cat-5 band).
    assert 62.0 <= prov["peak_wind_10min_ms"] <= 72.0, prov
    # central pressure floor near the published 919 mb.
    assert 915.0 <= prov["min_central_pressure_mb"] <= 925.0, prov
    # the 0.93 averaging factor is applied + surfaced.
    assert prov["averaging_factor_1min_to_10min"] == pytest.approx(0.93)
    # RMW came from the Knaff-Zehr fallback (blank USA_RMW) — surfaced, not faked.
    assert "Knaff-Zehr" in prov["rmw_source"]
    assert "standard-atmosphere" in prov["pn_source"]
    # Mexico Beach AOI -> UTM 16N.
    assert res.utmzone == "16n"
    assert res.utm_epsg == 32616


def test_holland_profile_peaks_at_rmw_and_decays_outward():
    # A single Holland slice: wind maxes at ~RMW, decays to near-zero far out.
    vmax = 140.0 * S.KT_TO_MS * S.ONE_MIN_TO_TEN_MIN
    pc = 91900.0
    pn = 101300.0
    dp = pn - pc
    rmw_m = S.knaff_zehr_rmw_km(vmax, 29.68) * 1000.0
    b = S.holland_b(vmax, pn, pc)
    v_at_rmw, _ = S.holland_profile(rmw_m, rmw_m, b, dp, pc, 29.68)
    v_far, _ = S.holland_profile(400_000.0, rmw_m, b, dp, pc, 29.68)
    v_near, _ = S.holland_profile(rmw_m * 0.3, rmw_m, b, dp, pc, 29.68)
    assert v_at_rmw > v_far          # decays outward
    assert v_at_rmw >= v_near        # peak at/after RMW (not in the calm eye)
    assert v_far < 0.3 * v_at_rmw    # far-field is small
    # pressure DEFICIT (pn - p) is MAX near the eye and ~0 far out.
    _, p_near = S.holland_profile(rmw_m * 0.1, rmw_m, b, dp, pc, 29.68)
    _, p_far = S.holland_profile(490_000.0, rmw_m, b, dp, pc, 29.68)
    assert (pn - p_near) > 0.7 * dp   # deep deficit in the core
    assert (pn - p_far) < 0.2 * dp    # near-ambient far out


def test_northern_hemisphere_rotation_and_right_of_track_asymmetry():
    # Regression for the CW-vs-CCW sign trap: a NH cyclone rotates COUNTER-
    # clockwise, so at the point EAST of the eye the wind must blow toward the
    # NORTH (wind_from_direction ~= 180, i.e. from the south). A clockwise field
    # (Southern-Hemi) would silently invert the right-of-track asymmetry and put
    # the surge on the WRONG side - passing every magnitude test. n_spokes=36 ->
    # spoke 9 = east (bearing 90), spoke 27 = west (bearing 270).
    rmw_m = 25_900.0
    speed_t, fromdir_t, _p, dr = S._build_polar_field(
        60.0, rmw_m, 1.49, 9400.0, 91900.0, 30.0, (0.0, 0.0),
        n_spokes=36, n_rings=100, spw_radius_m=500_000.0,
        inflow_deg=0.0, asym_weight=0.0,
    )
    ring = int(round(rmw_m / dr))
    # pure tangential at east -> wind FROM ~180 deg (blows north): CCW/NH.
    assert abs(((fromdir_t[ring][9] - 180.0 + 180.0) % 360.0) - 180.0) < 5.0
    # ...and at west -> FROM ~0 deg (blows south).
    assert abs(((fromdir_t[ring][27] - 0.0 + 180.0) % 360.0) - 180.0) < 5.0
    # With a NORTHWARD-moving storm the RIGHT-of-track (east) side is stronger.
    speed_a, _fd, _p2, dr2 = S._build_polar_field(
        60.0, rmw_m, 1.49, 9400.0, 91900.0, 30.0, (0.0, 7.0),
        n_spokes=36, n_rings=100, spw_radius_m=500_000.0,
        inflow_deg=20.0, asym_weight=0.6,
    )
    ring2 = int(round(rmw_m / dr2))
    assert speed_a[ring2][9] > speed_a[ring2][27]  # east (right) > west (left)


def test_knaff_zehr_rmw_reasonable():
    # A strong storm's Knaff-Zehr RMW is a small (tens-of-km) core, floored >=10.
    vmax = 140.0 * S.KT_TO_MS * S.ONE_MIN_TO_TEN_MIN
    rmw_km = S.knaff_zehr_rmw_km(vmax, 29.68)
    assert 10.0 <= rmw_km <= 80.0


# --------------------------------------------------------------------------- #
# 2. spw header / format golden.
# --------------------------------------------------------------------------- #


def test_spw_header_and_format_golden(tmp_path):
    res = S.build_spiderweb_from_fixes(
        _michael_like_fixes(),
        (-85.9, 29.3, -84.9, 30.2),
        out_dir=str(tmp_path),
        deck_sim_hours=48.0,
        n_spokes=36,
        n_rings=100,
        storm_name="Michael",
    )
    lines = open(res.spw_path, encoding="ascii").read().splitlines()
    head = {ln.split("=", 1)[0].strip(): ln.split("=", 1)[1].strip()
            for ln in lines[:16] if "=" in ln and not ln.startswith("TIME")}
    assert head["FileVersion"] == "1.03"
    assert head["Filetype"] == "meteo_on_spiderweb_grid"
    assert head["n_cols"] == "36"
    assert head["n_rows"] == "100"
    assert head["n_quantity"] == "3"
    assert head["quantity1"] == "wind_speed"
    assert head["quantity2"] == "wind_from_direction"
    assert head["quantity3"] == "p_drop"
    assert head["unit1"] == "m s-1"
    assert head["unit3"] == "Pa"
    # TIME lines are "minutes since <tref>"; first block anchors at minute 0.
    time_lines = [ln for ln in lines if ln.startswith("TIME")]
    assert len(time_lines) >= 2
    assert "minutes since 2026-01-01 00:00:00" in time_lines[0]
    assert time_lines[0].split("=", 1)[1].strip().startswith("0.000000")
    # each TIME block carries eye lon/lat + n_rings quantity rows x 3 quantities.
    first_time_idx = lines.index(time_lines[0])
    assert lines[first_time_idx + 1].startswith("x_spw_eye")
    assert lines[first_time_idx + 2].startswith("y_spw_eye")


# --------------------------------------------------------------------------- #
# 3. tref / deck-window overlap assert.
# --------------------------------------------------------------------------- #


def test_deck_window_overlap_assert_raises_on_no_overlap(tmp_path):
    # A deck window of ~0 h cannot overlap any spw span -> typed SpiderwebError.
    with pytest.raises(S.SpiderwebError):
        S.build_spiderweb_from_fixes(
            _michael_like_fixes(),
            (-85.9, 29.3, -84.9, 30.2),
            out_dir=str(tmp_path),
            deck_sim_hours=0.001,  # degenerate deck window
        )


def test_default_window_tracks_deck_keeps_landfall_inside(tmp_path):
    # The canonical no-window prompt: caller passes only deck_sim_hours (24 h) and
    # NOT window_hr. Regression for the #1 hazard - the old fixed 48 h window put
    # landfall (~0.6*48=28.8 h=1728 min) PAST the 24 h deck end (1440 min), so peak
    # surge was never simulated while the pre-landfall ramp still overlapped (the
    # overlap assert passed = false confidence). With window_hr tracking the deck,
    # landfall must re-anchor INSIDE [0, deck_min].
    res = S.build_spiderweb_from_fixes(
        _michael_like_fixes(),
        (-85.9, 29.3, -84.9, 30.2),
        out_dir=str(tmp_path),
        deck_sim_hours=24.0,  # no window_hr -> defaults to the deck length
        storm_name="Michael",
    )
    prov = res.provenance
    deck_min = prov["deck_window_min"]
    assert prov["window_hr"] == pytest.approx(24.0)          # tracked the deck
    assert 0.0 <= prov["landfall_min"] <= deck_min, prov     # landfall simulated
    # landfall sits well inside the deck (not clipped at the very end).
    assert prov["landfall_min"] < deck_min


def test_explicit_oversized_window_raises_landfall_outside_deck(tmp_path):
    # A caller that FORCES a 48 h window over a 24 h deck pushes landfall past the
    # deck end -> the landfall-inside-deck assert must hard-fail (not silently
    # underestimate surge). This is the exact old-default failure, now caught.
    with pytest.raises(S.SpiderwebError, match="landfall"):
        S.build_spiderweb_from_fixes(
            _michael_like_fixes(),
            (-85.9, 29.3, -84.9, 30.2),
            out_dir=str(tmp_path),
            deck_sim_hours=12.0,   # short deck
            window_hr=48.0,        # landfall re-anchors past the 12 h deck end
        )


def test_too_few_fixes_raises(tmp_path):
    with pytest.raises(S.SpiderwebError):
        S.build_spiderweb_from_fixes(
            [{"lon": -85.5, "lat": 29.6,
              "iso_time": "2018-10-10 12:00:00", "wind_kt": 120, "pres_mb": 940}],
            (-85.9, 29.3, -84.9, 30.2),
            out_dir=str(tmp_path),
            deck_sim_hours=48.0,
        )


def test_utm_zone_from_lon_lat():
    assert S._utm_from_lon_lat(-85.5, 29.68) == ("16n", 32616)
    assert S._utm_from_lon_lat(-122.4, 37.8) == ("10n", 32610)
    # southern hemisphere -> 327xx
    zone, epsg = S._utm_from_lon_lat(151.2, -33.8)
    assert zone.endswith("s") and 32700 < epsg < 32761


# --------------------------------------------------------------------------- #
# 4. Emitter XOR (spiderweb refuses co-present wind/pressure).
# --------------------------------------------------------------------------- #


def test_emitter_emits_spwfile_utmzone_baro():
    from trid3nt_server.workflows.sfincs_builder import (
        BuildOptions,
        ForcingSpec,
        SpiderwebForcing,
        _generate_hydromt_yaml_config,
    )

    fs = ForcingSpec(
        forcing_type="pluvial_synthetic", precip_inches=5.0, duration_hours=48,
        wind_spiderweb=SpiderwebForcing(
            spw_path="/tmp/sfincs.spw", utmzone="16n"),
    )
    assert fs.has_surge_forcing() is True  # spiderweb IS the surge driver
    yaml_text = _generate_hydromt_yaml_config(
        bbox=(-85.9, 29.3, -84.9, 30.2),
        options=BuildOptions(crs="EPSG:32616"),
        dem_local_path="/tmp/dem.tif",
        landcover_local_path="/tmp/lc.tif",
        river_local_path=None,
        forcing=fs,
        mapping_csv_path="/tmp/m.csv",
    )
    assert "spwfile: sfincs.spw" in yaml_text
    assert "utmzone: 16n" in yaml_text
    assert "baro: 1" in yaml_text
    assert "crs: EPSG:32616" in yaml_text


def test_emitter_xor_refuses_wind_with_spiderweb():
    from trid3nt_server.workflows.sfincs_builder import (
        BuildOptions,
        ForcingSpec,
        SFINCSSetupError,
        SpiderwebForcing,
        WindForcing,
        _generate_hydromt_yaml_config,
    )

    fs = ForcingSpec(
        forcing_type="pluvial_synthetic", precip_inches=5.0, duration_hours=48,
        wind_spiderweb=SpiderwebForcing(spw_path="/tmp/x.spw", utmzone="16n"),
        wind=WindForcing(magnitude=30.0, direction=90.0),
    )
    with pytest.raises(SFINCSSetupError) as exc:
        _generate_hydromt_yaml_config(
            bbox=(-85.9, 29.3, -84.9, 30.2), options=BuildOptions(),
            dem_local_path="/tmp/dem.tif", landcover_local_path="/tmp/lc.tif",
            river_local_path=None, forcing=fs, mapping_csv_path="/tmp/m.csv",
        )
    assert exc.value.error_code == "SPIDERWEB_FORCING_CONFLICT"


def test_pure_pluvial_deck_emits_no_spiderweb_keys():
    # Regression guard: a non-storm deck stays byte-identical (no spw keys).
    from trid3nt_server.workflows.sfincs_builder import (
        BuildOptions,
        ForcingSpec,
        _generate_hydromt_yaml_config,
    )

    fs = ForcingSpec(
        forcing_type="pluvial_synthetic", precip_inches=5.0, duration_hours=24)
    yaml_text = _generate_hydromt_yaml_config(
        bbox=(-85.9, 29.3, -84.9, 30.2), options=BuildOptions(),
        dem_local_path="/tmp/dem.tif", landcover_local_path="/tmp/lc.tif",
        river_local_path=None, forcing=fs, mapping_csv_path="/tmp/m.csv",
    )
    assert "spwfile" not in yaml_text
    assert "utmzone" not in yaml_text


# --------------------------------------------------------------------------- #
# 5. (removed) Quadtree backend-gate test — the AWS Batch arm was removed
#    (local-only slim); the coastal quadtree combined-Batch dispatch no longer
#    exists, so the `quadtree = quadtree or (is_coastal and backend==aws-batch)`
#    inline gate it exercised is gone. No local-path behavior to cover here.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 6. fetch_storm_tracks radii parse (blank-tolerant IBTrACS CSV slice).
# --------------------------------------------------------------------------- #


def test_fetch_storm_tracks_radii_parse_blank_tolerant():
    from trid3nt_server.tools.fetchers.weather.fetch_storm_tracks import _parse_ibtracs_csv

    header = (
        "SID,SEASON,BASIN,NAME,ISO_TIME,LAT,LON,NATURE,USA_WIND,USA_PRES,"
        "USA_SSHS,USA_STATUS,USA_RMW,USA_POCI,USA_ROCI,"
        "USA_R34_NE,USA_R34_SE,USA_R34_SW,USA_R34_NW,TRACK_TYPE"
    )
    units = ",".join([" "] + ["Year"] + [" "] * 18)  # skipped units row
    # one fully-populated fix, one with blank radii (the common older-fix case).
    row_full = (
        "2018282N26283,2018,NA,MICHAEL,2018-10-10 17:00:00,29.6,-85.5,TS,"
        "140,919,5,HU,15,1005,180,60,50,40,45,main"
    )
    row_blank = (
        "2018282N26283,2018,NA,MICHAEL,2018-10-10 18:00:00,29.9,-85.5,TS,"
        "135,922,5,HU,,,,,,,,main"
    )
    raw = "\n".join([header, units, row_full, row_blank]).encode("utf-8")
    storms = _parse_ibtracs_csv(raw, y0=2018, y1=2018, storm_name="MICHAEL")
    fixes = storms["2018282N26283"]
    assert len(fixes) == 2
    f0, f1 = fixes
    # populated fix carries the radii verbatim.
    assert f0["rmw_nmi"] == 15.0
    assert f0["poci_mb"] == 1005.0
    assert f0["roci_nmi"] == 180.0
    assert f0["r34_ne_nmi"] == 60.0
    assert f0["r34_nw_nmi"] == 45.0
    # blank fix -> None (never fabricated).
    assert f1["rmw_nmi"] is None
    assert f1["poci_mb"] is None
    assert f1["r34_ne_nmi"] is None
    # the base wind/pressure still parse on the blank-radii fix.
    assert f1["wind_kt"] == 135.0
    assert f1["pres_mb"] == 922.0
