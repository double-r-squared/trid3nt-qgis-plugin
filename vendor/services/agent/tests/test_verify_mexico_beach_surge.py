"""Unit tests for the PURE acceptance functions in verify_mexico_beach_surge.py.

These prove the bathtub / connectivity / area-match / wet-front / runup logic on
SYNTHETIC inputs (a tilted-plane DEM + hand-built flooded masks) so the surge
acceptance is provable WITHOUT a live Batch run. The live S3/COG I/O in the
harness is NOT exercised here -- only the math the PASS/FAIL hinges on.

The headline test is the CONNECTIVITY one: a naive ``ground < surge`` bathtub
over-counts an isolated inland pocket; the connected flood-fill must exclude it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

# Import the verifier module by path (it lives at services/agent/, not in the
# grace2_agent package, mirroring verify_mexico_beach_waves.py).
_VERIFIER_PATH = Path(__file__).resolve().parents[1] / "verify_mexico_beach_surge.py"
_spec = importlib.util.spec_from_file_location("verify_mexico_beach_surge", _VERIFIER_PATH)
assert _spec and _spec.loader
vmbs = importlib.util.module_from_spec(_spec)
sys.modules["verify_mexico_beach_surge"] = vmbs
_spec.loader.exec_module(vmbs)


# -----------------------------------------------------------------------------
# Synthetic DEM helpers (row 0 = NORTH, last row = SOUTH = seaward, per the
# rasterio convention the pure functions assume).
# -----------------------------------------------------------------------------
def tilted_plane_dem(
    *, height: int = 40, width: int = 30, sea_floor_m: float = -5.0, crest_m: float = 8.0
) -> np.ndarray:
    """A simple beach profile: low/negative bathymetry on the SOUTH (last row),
    ramping up linearly to a high inland crest on the NORTH (row 0).

    row 0 (north) = crest_m  ...  row -1 (south, seaward) = sea_floor_m.
    Every column is identical so the geometry is a clean 1D ramp.
    """
    rows = np.linspace(crest_m, sea_floor_m, height)  # north high -> south low
    return np.repeat(rows[:, None], width, axis=1).astype("float64")


def test_tilted_plane_orientation():
    dem = tilted_plane_dem(height=10, width=4, sea_floor_m=-5.0, crest_m=5.0)
    # north row high, south row low.
    assert dem[0, 0] == pytest.approx(5.0)
    assert dem[-1, 0] == pytest.approx(-5.0)


# -----------------------------------------------------------------------------
# (core) connected_bathtub_mask
# -----------------------------------------------------------------------------
def test_connected_bathtub_on_tilted_plane_floods_low_south_band():
    # surge +3.5: every row whose elevation < 3.5 and connected south floods.
    dem = tilted_plane_dem(height=40, width=20, sea_floor_m=-5.0, crest_m=8.0)
    mask = vmbs.connected_bathtub_mask(dem, surge_wl_m=3.5, seaward_edge="south")
    # The south band (low rows) floods; the high north crest does not.
    assert mask[-1].all(), "seaward (south) row must flood"
    assert not mask[0].any(), "north crest (8 m) must stay dry"
    # Because it's a monotone ramp connected to the sea, the connected bathtub
    # equals the naive ground<surge mask here (no isolated pockets to exclude).
    naive = dem < 3.5
    assert np.array_equal(mask, naive)


def test_connectivity_excludes_isolated_inland_pocket():
    """THE load-bearing test: a low inland pocket walled off by high ground must
    NOT be counted, even though ground < surge there. A naive bathtub over-counts
    it; the connected flood-fill excludes it."""
    h, w = 30, 30
    dem = np.full((h, w), 6.0, dtype="float64")  # all high-and-dry by default
    # A seaward (south) low strip connected to the sea: rows 24..29 are low.
    dem[24:, :] = -2.0
    # An ISOLATED inland pocket up north: a 4x4 block of low ground at rows 5..8,
    # surrounded by 6 m walls -> below surge but NOT reachable from the sea.
    dem[5:9, 10:14] = 0.0
    surge = 3.5

    naive = dem < surge
    connected = vmbs.connected_bathtub_mask(dem, surge_wl_m=surge, seaward_edge="south")

    # The naive mask includes the pocket; the connected mask must not.
    assert naive[6, 11], "naive mask counts the isolated pocket"
    assert not connected[6, 11], "connected mask must EXCLUDE the isolated pocket"
    # The seaward strip is counted by both.
    assert connected[-1].all()
    # Connected area is strictly smaller than naive (pocket removed).
    assert connected.sum() < naive.sum()
    assert int(naive.sum() - connected.sum()) == 4 * 4  # exactly the 4x4 pocket


def test_connectivity_includes_inland_pocket_when_a_channel_links_it():
    """Same pocket, but now a below-surge channel connects it to the sea -> it
    SHOULD be counted (it is hydraulically reachable)."""
    h, w = 30, 30
    dem = np.full((h, w), 6.0, dtype="float64")
    dem[24:, :] = -2.0          # seaward low strip
    dem[5:9, 10:14] = 0.0        # inland pocket
    dem[9:24, 11:13] = 1.0       # a low channel linking the pocket down to the sea
    surge = 3.5
    connected = vmbs.connected_bathtub_mask(dem, surge_wl_m=surge, seaward_edge="south")
    assert connected[6, 11], "pocket linked by a below-surge channel must flood"


def test_connected_bathtub_no_sea_connection_is_empty():
    # A DEM whose entire seaward edge is ABOVE the surge -> nothing connects.
    dem = np.full((10, 10), 9.0, dtype="float64")
    dem[2:5, 2:5] = 0.0  # an inland pocket, but no seaward connection
    mask = vmbs.connected_bathtub_mask(dem, surge_wl_m=3.5, seaward_edge="south")
    assert not mask.any()


def test_connected_bathtub_handles_nan_nodata():
    dem = tilted_plane_dem(height=20, width=10)
    dem[10:12, :] = np.nan  # a nodata band across the middle
    mask = vmbs.connected_bathtub_mask(dem, surge_wl_m=3.5, seaward_edge="south")
    # NaN cells are never flooded.
    assert not mask[10].any() and not mask[11].any()
    # The NaN band walls off the north: only the south side connects to the sea.
    assert mask[-1].all()
    assert not mask[0].any()


def test_seaward_edge_directions():
    # A ramp low on the WEST, high on the EAST; seaward_edge="west" floods west.
    cols = np.linspace(-5.0, 8.0, 30)  # west low -> east high
    dem = np.repeat(cols[None, :], 20, axis=0).astype("float64")
    mask = vmbs.connected_bathtub_mask(dem, surge_wl_m=3.5, seaward_edge="west")
    assert mask[:, 0].all(), "west (seaward) column floods"
    assert not mask[:, -1].any(), "east (high) column stays dry"


def test_sea_floor_gate_blocks_a_high_seawall_seed():
    # The seaward row has a high seawall everywhere except one notch; with the
    # sea_floor gate only the notch seeds.
    dem = np.full((15, 15), 6.0, dtype="float64")
    dem[-1, :] = 5.0           # a 5 m seawall along the whole south edge
    dem[-1, 7] = -1.0           # a single low notch (an inlet)
    dem[10:, 6:9] = -1.0        # low ground behind the notch
    surge = 3.5
    # Without the gate, no south cell is below surge except the notch -> only the
    # notch column seeds anyway here; assert the notch path floods.
    mask = vmbs.connected_bathtub_mask(
        dem, surge_wl_m=surge, seaward_edge="south", sea_floor_m=0.0
    )
    assert mask[-1, 7], "the low notch seeds"
    assert mask[11, 7], "low ground behind the notch floods through it"
    assert not mask[-1, 0], "the seawall cell (5 m > surge) does not flood"


# -----------------------------------------------------------------------------
# area_m2
# -----------------------------------------------------------------------------
def test_area_m2_counts_cells_times_pixel_area():
    mask = np.zeros((10, 10), dtype=bool)
    mask[:2, :3] = True  # 6 cells
    assert vmbs.area_m2(mask, pixel_size_m=30.0) == pytest.approx(6 * 30.0 * 30.0)


# -----------------------------------------------------------------------------
# (1) bathtub_area_match
# -----------------------------------------------------------------------------
def _mask_with_n_true(shape, n):
    m = np.zeros(shape, dtype=bool)
    flat = m.ravel()
    flat[:n] = True
    return flat.reshape(shape)


def test_area_match_pass_within_tolerance():
    shape = (50, 50)
    bath = _mask_with_n_true(shape, 1000)
    model = _mask_with_n_true(shape, 1000 - 200)  # 20% under -> within 25% tol
    res = vmbs.bathtub_area_match(bath, model, pixel_size_m=30.0)
    assert res.passed, res.reason
    assert res.rel_err == pytest.approx(0.2, abs=1e-6)
    assert res.overflood_ratio <= vmbs.OVERFLOOD_FACTOR


def test_area_match_fail_too_little():
    shape = (50, 50)
    bath = _mask_with_n_true(shape, 1000)
    model = _mask_with_n_true(shape, 1000 - 400)  # 40% under -> exceeds 25% tol
    res = vmbs.bathtub_area_match(bath, model, pixel_size_m=30.0)
    assert not res.passed
    assert "area mismatch" in res.reason


def test_area_match_fail_overflood():
    shape = (50, 50)
    bath = _mask_with_n_true(shape, 1000)
    model = _mask_with_n_true(shape, 1080)  # +8% -> within 25% tol but OVER-floods
    res = vmbs.bathtub_area_match(bath, model, pixel_size_m=30.0)
    assert res.rel_err <= vmbs.AREA_TOL  # the relative error alone would pass
    assert not res.passed, "over-flood must fail even within the rel tolerance"
    assert "OVER-FLOOD" in res.reason


def test_area_match_zero_bathtub_fails():
    shape = (10, 10)
    bath = np.zeros(shape, dtype=bool)
    model = _mask_with_n_true(shape, 5)
    res = vmbs.bathtub_area_match(bath, model, pixel_size_m=30.0)
    assert not res.passed
    assert "bathtub area is zero" in res.reason


# -----------------------------------------------------------------------------
# (2) wet_front_advance
# -----------------------------------------------------------------------------
def _wet_band_from_south(shape, n_rows):
    """A wet mask flooding the southern ``n_rows`` rows (seaward inward)."""
    m = np.zeros(shape, dtype=bool)
    if n_rows > 0:
        m[-n_rows:, :] = True
    return m


def test_inland_penetration_rows_from_south():
    shape = (20, 5)
    assert vmbs.inland_penetration_rows(_wet_band_from_south(shape, 0)) == 0
    assert vmbs.inland_penetration_rows(_wet_band_from_south(shape, 1)) == 0  # only the edge row
    assert vmbs.inland_penetration_rows(_wet_band_from_south(shape, 5)) == 4  # 5 rows -> 4 inland


def test_wet_front_advance_pass():
    shape = (30, 10)
    # frames: dry, then a 2-row toe, then climbs to a 12-row inundation at peak.
    frames = [
        _wet_band_from_south(shape, 0),
        _wet_band_from_south(shape, 2),   # first wet: penetration 1
        _wet_band_from_south(shape, 6),
        _wet_band_from_south(shape, 12),  # peak: penetration 11
        _wet_band_from_south(shape, 8),   # drain-down
    ]
    res = vmbs.wet_front_advance(frames)
    assert res.passed, res.reason
    assert res.first_wet_frame_idx == 1
    assert res.peak_penetration > res.first_wet_penetration
    assert res.ratio >= vmbs.ADVANCE_FACTOR


def test_wet_front_advance_fail_appears_all_at_once():
    shape = (30, 10)
    # The front appears at full extent on the first wet frame and never advances.
    frames = [
        _wet_band_from_south(shape, 0),
        _wet_band_from_south(shape, 12),  # first wet AND peak -> ratio 1
        _wet_band_from_south(shape, 12),
    ]
    res = vmbs.wet_front_advance(frames)
    assert not res.passed
    assert res.ratio == pytest.approx(1.0)


def test_wet_front_advance_fail_never_floods():
    shape = (10, 10)
    frames = [np.zeros(shape, dtype=bool) for _ in range(3)]
    res = vmbs.wet_front_advance(frames)
    assert not res.passed
    assert "never flooded" in res.reason


# -----------------------------------------------------------------------------
# (3) runup_elevation
# -----------------------------------------------------------------------------
def test_runup_pass_in_window():
    dem = tilted_plane_dem(height=40, width=10, sea_floor_m=-5.0, crest_m=8.0)
    # Flood the connected bathtub at surge 3.5 -> the highest wet ground is ~3.5.
    mask = vmbs.connected_bathtub_mask(dem, surge_wl_m=3.5, seaward_edge="south")
    res = vmbs.runup_elevation(dem, mask)
    assert res.passed, res.reason
    assert vmbs.RUNUP_MIN_M <= res.max_wet_ground_m <= vmbs.RUNUP_MAX_M


def test_runup_fail_overshoot():
    dem = tilted_plane_dem(height=40, width=10, sea_floor_m=-5.0, crest_m=8.0)
    # An over-flood that wets a 6 m cell -> over-runup.
    mask = dem < 6.5
    res = vmbs.runup_elevation(dem, mask)
    assert not res.passed
    assert "OVER-RUNUP" in res.reason


def test_runup_fail_undershoot():
    dem = tilted_plane_dem(height=40, width=10, sea_floor_m=-5.0, crest_m=8.0)
    # Only the deep seaward cells get wet -> never climbed the berm.
    mask = dem < 0.0
    res = vmbs.runup_elevation(dem, mask)
    assert not res.passed
    assert "UNDER-RUNUP" in res.reason


# -----------------------------------------------------------------------------
# end-to-end on a synthetic "good run" and a synthetic "over-flood run"
# -----------------------------------------------------------------------------
def _synthetic_good_run(shape=(40, 20), surge=3.5, pixel=30.0):
    """A DEM + a peak-depth + frames that SHOULD pass all 3 parts.

    The DEM is a tilted beach. The 'modeled' peak depth equals (surge - ground)
    on the connected bathtub (a perfect bathtub solver), 0 elsewhere. The frames
    march the front in: each frame floods a growing southern band.
    """
    dem = tilted_plane_dem(height=shape[0], width=shape[1], sea_floor_m=-5.0, crest_m=8.0)
    bathtub = vmbs.connected_bathtub_mask(dem, surge_wl_m=surge, seaward_edge="south")
    depth = np.where(bathtub, surge - dem, 0.0).astype("float64")
    # The connected bathtub here is the southern band rows where ground<surge.
    wet_rows = int(bathtub[:, 0].sum())
    frames = []
    # toe -> peak -> drain; the last (peak) frame matches the full bathtub band.
    for n in [0, 2, max(2, wet_rows // 2), wet_rows, max(2, wet_rows - 3)]:
        fm = np.zeros(shape, dtype="float64")
        if n > 0:
            band = bathtub.copy()
            # restrict to the southernmost n rows of the bathtub
            keep = np.zeros(shape, dtype=bool)
            keep[-n:, :] = True
            band = band & keep
            fm = np.where(band, surge - dem, 0.0)
        frames.append(fm)
    return dem, depth, frames, pixel, surge


def test_evaluate_surge_acceptance_passes_on_good_run():
    dem, depth, frames, pixel, surge = _synthetic_good_run()
    acc = vmbs.evaluate_surge_acceptance(
        dem, depth, frames, pixel_size_m=pixel, surge_wl_m=surge
    )
    assert acc.area.passed, acc.area.reason
    assert acc.advance.passed, acc.advance.reason
    assert acc.runup.passed, acc.runup.reason
    assert acc.passed


def test_evaluate_surge_acceptance_fails_on_overflood_run():
    dem, depth, frames, pixel, surge = _synthetic_good_run()
    # Corrupt the peak depth to flood the ENTIRE grid (a runaway over-flood).
    over = np.full_like(depth, 2.0)
    acc = vmbs.evaluate_surge_acceptance(
        dem, over, frames, pixel_size_m=pixel, surge_wl_m=surge
    )
    assert not acc.area.passed, "over-flood must fail the area match"
    assert not acc.passed


def test_evaluate_surge_acceptance_fails_when_only_isolated_pocket_modeled():
    # The model floods ONLY an isolated inland pocket (disconnected from the sea):
    # connected bathtub is the seaward band, so the area match fails (modeled is
    # in the wrong place + wrong size).
    h, w = 30, 30
    dem = np.full((h, w), 6.0, dtype="float64")
    dem[24:, :] = -2.0           # seaward connected band
    dem[5:9, 10:14] = 0.0         # isolated pocket
    surge = 3.5
    # 'model' floods only the pocket (which the connected bathtub excludes).
    depth = np.zeros((h, w), dtype="float64")
    depth[5:9, 10:14] = 1.0
    frames = [depth.copy()]
    acc = vmbs.evaluate_surge_acceptance(
        dem, depth, frames, pixel_size_m=30.0, surge_wl_m=surge
    )
    assert not acc.area.passed
    assert not acc.passed


def test_thresholds_are_exposed_as_module_constants():
    # The acceptance thresholds are a single source of truth importable by tests.
    # SURGE_WL_M is the full peak WATER-SURFACE elevation (tidal base +0.3 m +
    # surge +3.5 m = +3.8 m), the level the model actually floods to -- NOT the
    # 3.5 m surge component alone. RUNUP_MAX_M brackets that surface plus a modest
    # dynamic run-up margin (live run reached 4.219 m). See the constants block.
    assert vmbs.SURGE_WL_M == 3.8
    assert vmbs.WET_DEPTH_M == 0.05
    assert vmbs.AREA_TOL == 0.25
    assert vmbs.OVERFLOOD_FACTOR == 1.05
    assert vmbs.ADVANCE_FACTOR == 2.0
    assert (vmbs.RUNUP_MIN_M, vmbs.RUNUP_MAX_M) == (2.5, 4.3)
