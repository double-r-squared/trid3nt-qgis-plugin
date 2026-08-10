"""Targeted tests for the Landlab worker component chain (sprint-17 — NEW engine).

The Landlab analogue of ``services/workers/modflow/test_gwt_adapter.py``: pins
the worker-side numerical core (``component_chain.run_component_chain``) in
isolation.

1. **Dispatch + honest error** — an unknown ``analysis`` raises a typed
   ``ValueError`` (never a silent wrong field); the chain dispatches on
   ``build_spec['analysis']``. (no landlab needed — the dispatch guard fires
   BEFORE the lazy landlab import.)
2. **In-memory grid run (landlab-gated)** — a tiny synthetic DEM through the REAL
   LandslideProbability + OverlandFlow chains: a probability field in [0, 1] /
   a depth field, NaN where closed, the three narration scalars finite + in
   range. Skipped when landlab is not installed in the env (the worker image
   pip-installs it; the agent venv does not, so this is the build-time / CI gate).

Run from the repo root so ``services.workers.landlab`` imports resolve, e.g.
``PYTHONPATH=. pytest services/workers/landlab/test_component_chain.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

# Make ``services.workers.landlab.component_chain`` importable when this file is
# run directly from the worker dir (mirrors test_gwt_adapter's path bootstrap).
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.workers.landlab.component_chain import (  # noqa: E402
    OVERLAND_WET_DEPTH_M,
    UNSTABLE_PROBABILITY_THRESHOLD,
    run_component_chain,
)


# ===========================================================================
# (1) Dispatch + honest error — no landlab needed.
# ===========================================================================
def test_unknown_analysis_raises_typed_value_error():
    """An unknown analysis raises ValueError BEFORE any landlab import (the
    dispatch guard), so this runs even without landlab installed."""
    dem = np.full((4, 4), 100.0)
    with pytest.raises(ValueError) as exc:
        run_component_chain(dem, resolution_m=30.0, build_spec={"analysis": "warp_drive"})
    assert "unknown Landlab analysis" in str(exc.value)


# ===========================================================================
# (2) In-memory grid run — REAL landlab chain (gated on the dep).
# ===========================================================================
# NOTE: this worker package is itself named ``landlab``
# (``services/workers/landlab``), and pytest's default prepend import mode puts
# ``services/workers`` on sys.path — so a bare ``import landlab`` can resolve to
# THIS package, not the pip library. We therefore gate on the presence of the
# REAL library's ``landlab.components`` submodule (which this package does not
# define) rather than the bare top-level name, so the in-memory chain run is
# correctly SKIPPED in any env without the actual Landlab library installed (the
# agent venv). In the worker image ``/opt/trid3nt`` is the only PYTHONPATH entry
# (``services/workers`` is NOT on it), so ``import landlab`` resolves to the real
# library and ``import services.workers.landlab.component_chain`` resolves here —
# no shadow at runtime.
#
# This gate is a per-test ``skipif`` (NOT a module-level ``importorskip``) so the
# dispatch-guard test above ALWAYS runs (it needs no landlab).


def _real_landlab_available() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("landlab.components") is not None
    except Exception:  # noqa: BLE001 — a shadow package may raise on submodule probe
        return False


_REQUIRES_LANDLAB = pytest.mark.skipif(
    not _real_landlab_available(),
    reason="the real Landlab library is only in the worker image; the in-memory "
    "chain run is the build-time / CI gate, not an agent-venv test.",
)


def _tilted_dem(n: int = 16, cell: float = 30.0) -> np.ndarray:
    """A tilted plane draining to the low corner + a steep central scarp (so the
    LandslideProbability + OverlandFlow chains both have real slope to act on)."""
    ii, jj = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
    plane = 200.0 - 0.15 * cell * (ii + jj)
    ci = cj = (n - 1) / 2.0
    scarp = 40.0 * np.exp(-((ii - ci) ** 2 + (jj - cj) ** 2) / (2.0 * 2.0**2))
    return (plane + scarp).astype("float64")


@_REQUIRES_LANDLAB
def test_landslide_probability_chain_in_memory():
    """The REAL LandslideProbability chain on a tiny DEM: probability field in
    [0, 1], finite narration scalars, min FoS > 0."""
    dem = _tilted_dem()
    res = run_component_chain(
        dem,
        resolution_m=30.0,
        build_spec={
            "analysis": "landslide_probability",
            "n_monte_carlo": 25,  # small -> fast
            "soil_cohesion_pa": 8000.0,
            "soil_internal_friction_deg": 32.0,
        },
    )
    assert res.analysis == "landslide_probability"
    assert res.output_field_name == "landslide__probability_of_failure"
    field = np.asarray(res.field)
    assert field.shape == dem.shape
    finite = field[np.isfinite(field)]
    assert finite.size > 0
    # probability of failure is in [0, 1].
    assert float(finite.min()) >= 0.0
    assert float(finite.max()) <= 1.0
    # narration scalars in range.
    assert 0.0 <= res.unstable_area_fraction <= 1.0
    assert 0.0 <= res.mean_probability_of_failure <= 1.0
    assert res.min_factor_of_safety > 0.0
    # a deterministic FoS field came along for the min-FoS scalar.
    assert "factor_of_safety_field" in res.extra
    # the unstable fraction is consistent with the threshold on the field.
    n_active = int(finite.size)
    expected_unstable = float(
        np.count_nonzero(finite >= UNSTABLE_PROBABILITY_THRESHOLD) / n_active
    )
    assert res.unstable_area_fraction == pytest.approx(expected_unstable)


@_REQUIRES_LANDLAB
def test_overland_flow_chain_in_memory():
    """The REAL OverlandFlow chain on a tiny DEM: peak depth field >= 0, wet
    fraction in range, min_factor_of_safety carries the peak depth."""
    dem = _tilted_dem(n=12)
    res = run_component_chain(
        dem,
        resolution_m=30.0,
        build_spec={
            "analysis": "overland_flow",
            "rainfall_intensity_mm_hr": 80.0,
            "storm_duration_hr": 0.25,  # short -> fast
            "max_overland_steps": 200,
        },
    )
    assert res.analysis == "overland_flow"
    assert res.output_field_name == "surface_water__depth"
    field = np.asarray(res.field)
    assert field.shape == dem.shape
    finite = field[np.isfinite(field)]
    assert finite.size > 0
    assert float(finite.min()) >= 0.0
    assert 0.0 <= res.unstable_area_fraction <= 1.0
    # min_factor_of_safety carries the peak depth (>= 0).
    assert res.min_factor_of_safety >= 0.0
    assert res.mean_probability_of_failure == 0.0
    # wet fraction consistent with the threshold.
    n_active = int(finite.size)
    expected_wet = float(np.count_nonzero(finite >= OVERLAND_WET_DEPTH_M) / n_active)
    assert res.unstable_area_fraction == pytest.approx(expected_wet)


@_REQUIRES_LANDLAB
def test_nodata_cells_are_closed_and_nan():
    """No-data (NaN) DEM cells become closed boundaries and end up NaN in the
    output field (a hole in the active mesh)."""
    dem = _tilted_dem(n=12)
    dem[0, :] = np.nan  # a no-data row
    res = run_component_chain(
        dem,
        resolution_m=30.0,
        build_spec={"analysis": "landslide_probability", "n_monte_carlo": 15},
    )
    field = np.asarray(res.field)
    # the no-data row is NaN in the output.
    assert np.all(~np.isfinite(field[0, :]))


# ===========================================================================
# (3) Field-name contract — fake landlab injected (runs WITHOUT real landlab).
# ===========================================================================
# The single most important correctness property of the landslide chain is that
# it populates the EXACT grid fields the real Landlab ``LandslideProbability``
# API reads (``topographic__slope`` — NOT ``topographic__steepest_slope`` — plus
# ``topographic__specific_contributing_area`` and the documented soil__ fields,
# including the triangular cohesion triple {mode,minimum,maximum}_total_cohesion
# and soil__saturated_hydraulic_conductivity). A wrong field name fails silently
# in the real component (it raises FieldError at instantiation), so we pin the
# exact field set here with a FAKE landlab so the contract is checked in EVERY
# env, not only the worker image. The fake mirrors the parts of the Landlab API
# the chain touches: RasterModelGrid field bookkeeping, FlowAccumulator writing
# topographic__steepest_slope + drainage_area, and LandslideProbability writing
# landslide__probability_of_failure.


class _FakeGrid:
    """A minimal RasterModelGrid stand-in: node-field dict + status + BC const."""

    BC_NODE_IS_CLOSED = 4

    def __init__(self, shape, xy_spacing=1.0):  # noqa: ANN001
        self._shape = shape
        self.number_of_nodes = int(shape[0] * shape[1])
        self.at_node: dict[str, np.ndarray] = {}
        self.status_at_node = np.zeros(self.number_of_nodes, dtype=int)
        # core_nodes = every node (sufficient for the field-name assertion).
        self.core_nodes = np.arange(self.number_of_nodes)

    def add_field(self, name, values, at="node", clobber=False):  # noqa: ANN001
        arr = np.asarray(values, dtype="float64").ravel()
        if arr.size == 1:
            arr = np.full(self.number_of_nodes, float(arr[0]))
        self.at_node[name] = arr
        return self.at_node[name]

    def add_zeros(self, name, at="node", clobber=False):  # noqa: ANN001
        self.at_node[name] = np.zeros(self.number_of_nodes, dtype="float64")
        return self.at_node[name]


class _FakeFlowAccumulator:
    """Writes topographic__steepest_slope + drainage_area (what D8 produces)."""

    def __init__(self, grid, **kw):  # noqa: ANN001, ANN003
        self.grid = grid

    def run_one_step(self):
        n = self.grid.number_of_nodes
        # a non-trivial slope so the FoS math has something to bite on.
        self.grid.at_node["topographic__steepest_slope"] = np.full(n, 0.3)
        self.grid.at_node["drainage_area"] = np.full(n, 900.0)


class _FakeLandslideProbability:
    """Asserts the EXACT documented input fields exist, then writes the PoF."""

    REQUIRED_INPUTS = (
        "topographic__slope",
        "topographic__specific_contributing_area",
        "soil__transmissivity",
        "soil__saturated_hydraulic_conductivity",
        "soil__thickness",
        "soil__density",
        "soil__internal_friction_angle",
        "soil__mode_total_cohesion",
        "soil__minimum_total_cohesion",
        "soil__maximum_total_cohesion",
    )

    def __init__(self, grid, **kw):  # noqa: ANN001, ANN003
        self.grid = grid
        missing = [f for f in self.REQUIRED_INPUTS if f not in grid.at_node]
        if missing:
            # the real component raises landlab.FieldError on a missing input.
            raise KeyError(f"LandslideProbability missing input fields: {missing}")
        # the chain must NOT rely on topographic__steepest_slope as the model
        # input — that field is the FlowAccumulator product, not a model input.

    def calculate_landslide_probability(self):
        n = self.grid.number_of_nodes
        self.grid.at_node["landslide__probability_of_failure"] = np.linspace(
            0.0, 1.0, n
        )
        self.grid.at_node["soil__mean_relative_wetness"] = np.full(n, 0.5)
        self.grid.at_node["soil__probability_of_saturation"] = np.full(n, 0.5)


def _install_fake_landlab(monkeypatch):
    """Inject fake ``landlab`` + ``landlab.components`` into sys.modules."""
    import types

    fake_landlab = types.ModuleType("landlab")
    fake_landlab.RasterModelGrid = _FakeGrid  # type: ignore[attr-defined]
    fake_components = types.ModuleType("landlab.components")
    fake_components.FlowAccumulator = _FakeFlowAccumulator  # type: ignore[attr-defined]
    fake_components.LandslideProbability = _FakeLandslideProbability  # type: ignore[attr-defined]
    fake_components.OverlandFlow = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "landlab", fake_landlab)
    monkeypatch.setitem(sys.modules, "landlab.components", fake_components)


def test_landslide_chain_sets_documented_fields(monkeypatch):
    """The landslide chain populates the EXACT LandslideProbability input fields
    (topographic__slope, NOT topographic__steepest_slope, as the MODEL input) +
    the soil triple/sat-K, and reads landslide__probability_of_failure. Runs
    with a FAKE landlab so the field-name contract is pinned in every env."""
    _install_fake_landlab(monkeypatch)
    dem = _tilted_dem(n=8)
    res = run_component_chain(
        dem,
        resolution_m=30.0,
        build_spec={
            "analysis": "landslide_probability",
            "n_monte_carlo": 10,
            "soil_cohesion_pa": 10000.0,
        },
    )
    assert res.analysis == "landslide_probability"
    assert res.output_field_name == "landslide__probability_of_failure"
    field = np.asarray(res.field)
    assert field.shape == dem.shape
    finite = field[np.isfinite(field)]
    assert finite.size > 0
    assert 0.0 <= res.unstable_area_fraction <= 1.0
    assert 0.0 <= res.mean_probability_of_failure <= 1.0
    # the deterministic FoS field came along.
    assert "factor_of_safety_field" in res.extra


# ===========================================================================
# (3) flow_accumulation chain — REAL landlab chain (gated on the dep).
# ADR 0122 hazard-easy-four #1: drainage area + channel network + routing comp.
# ===========================================================================
@_REQUIRES_LANDLAB
def test_flow_accumulation_chain_in_memory():
    """The REAL FlowAccumulator chain: drainage-area field, channel-network
    secondary, and a 3-director routing comparison in ``extra``."""
    dem = _tilted_dem(n=24)
    res = run_component_chain(
        dem,
        resolution_m=30.0,
        build_spec={
            "analysis": "flow_accumulation",
            "flow_director": "D8",
            "depression_handler": "fill",
            "channel_threshold_cells": 30,
        },
    )
    assert res.analysis == "flow_accumulation"
    assert res.output_field_name == "drainage_area"
    field = np.asarray(res.field)
    assert field.shape == dem.shape
    da = field[np.isfinite(field)]
    assert da.size > 0
    # the largest accumulated area is >= a single cell (900 m^2).
    assert float(np.max(da)) >= 900.0
    # channel-network secondary is present + boolean-ish.
    assert "channel_network" in res.secondary_fields
    # routing comparison ran all 3 directors.
    rc = res.extra["routing_comparison"]
    assert {r["flow_director"] for r in rc} == {"D8", "Dinf", "MFD"}
    for r in rc:
        assert 0.0 <= r["channelized_area_fraction"] <= 1.0
        assert r["max_drainage_area_km2"] >= 0.0
    # narration scalars carried in extra.
    assert res.extra["max_drainage_area_km2"] >= 0.0
    assert 0.0 <= res.extra["channelized_area_fraction"] <= 1.0


@_REQUIRES_LANDLAB
def test_flow_accumulation_priority_flood_and_determinism():
    """priority_flood depression handling routes every director; two identical
    runs produce a byte-identical drainage-area field (determinism, Invariant 1)."""
    dem = _tilted_dem(n=20)
    spec = {
        "analysis": "flow_accumulation",
        "flow_director": "MFD",
        "depression_handler": "priority_flood",
        "channel_threshold_cells": 25,
    }
    r1 = run_component_chain(dem, resolution_m=30.0, build_spec=dict(spec))
    r2 = run_component_chain(dem, resolution_m=30.0, build_spec=dict(spec))
    f1 = np.nan_to_num(np.asarray(r1.field), nan=-1.0)
    f2 = np.nan_to_num(np.asarray(r2.field), nan=-1.0)
    assert np.array_equal(f1, f2)
    assert r1.extra["flow_director"] == "MFD"


# ===========================================================================
# (4) green_ampt_overland_flow chain -- REAL landlab chain (gated on the dep).
# ADR 0123 hazard-easy-four continuation #1: infiltration-vs-runoff partition.
# ===========================================================================
@_REQUIRES_LANDLAB
def test_green_ampt_partition_and_conservation():
    """The REAL Green-Ampt + OverlandFlow chain: infiltration-depth field +
    runoff-depth secondary + a partition that respects the storm total."""
    dem = _tilted_dem(n=24)
    res = run_component_chain(
        dem,
        resolution_m=30.0,
        build_spec={
            "analysis": "green_ampt_overland_flow",
            "rainfall_intensity_mm_hr": 90.0,
            "storm_duration_hr": 0.5,
            "soil_hydraulic_conductivity_m_s": 1e-5,
            "initial_soil_moisture_content": 0.15,
            "green_ampt_soil_type": "sandy loam",
        },
    )
    assert res.analysis == "green_ampt_overland_flow"
    assert res.output_field_name == "soil_water_infiltration__depth"
    infil = np.asarray(res.field)
    assert infil.shape == dem.shape
    assert "runoff_depth" in res.secondary_fields
    e = res.extra
    # storm total is the 90 mm/hr x 0.5 h = 45 mm design storm.
    assert abs(e["total_rainfall_mm"] - 45.0) < 1e-6
    # partition fractions are in [0, 1] and sum to <= 1 (some water may pond).
    assert 0.0 <= e["infiltrated_fraction"] <= 1.0
    assert 0.0 <= e["runoff_fraction"] <= 1.0
    assert e["infiltrated_fraction"] + e["runoff_fraction"] <= 1.0 + 1e-6
    # mean infiltration + runoff never exceed the storm total (conservation).
    assert e["mean_infiltration_mm"] <= 45.0 + 1e-6
    assert e["n_steps"] >= 1


@_REQUIRES_LANDLAB
def test_green_ampt_conductivity_monotonicity_and_determinism():
    """A HIGHER saturated conductivity infiltrates MORE (less runoff); two
    identical runs produce a byte-identical infiltration field (Invariant 1)."""
    dem = _tilted_dem(n=20)

    def _run(k):
        return run_component_chain(
            dem,
            resolution_m=30.0,
            build_spec={
                "analysis": "green_ampt_overland_flow",
                "rainfall_intensity_mm_hr": 90.0,
                "storm_duration_hr": 0.5,
                "soil_hydraulic_conductivity_m_s": k,
            },
        )

    lo = _run(1e-6)
    hi = _run(3e-5)
    assert hi.extra["infiltrated_fraction"] > lo.extra["infiltrated_fraction"]
    # determinism: re-run of the low-K case is byte-identical.
    lo2 = _run(1e-6)
    f1 = np.nan_to_num(np.asarray(lo.field), nan=-1.0)
    f2 = np.nan_to_num(np.asarray(lo2.field), nan=-1.0)
    assert np.array_equal(f1, f2)


# ===========================================================================
# (5) groundwater_steady / groundwater_storm chains -- REAL landlab chains
# (gated on the dep). ADR 0214: GroundwaterDupuitPercolator water table +
# seepage + baseflow (mass-conservation V&V) and storm-driven recession.
# ===========================================================================
@_REQUIRES_LANDLAB
def test_groundwater_steady_fields_and_mass_conservation():
    """The REAL steady GroundwaterDupuitPercolator chain: depth-to-water primary
    + water-table + seepage secondaries + the tutorial's mass-conservation V&V."""
    dem = _tilted_dem(n=24)
    res = run_component_chain(
        dem,
        resolution_m=30.0,
        build_spec={
            "analysis": "groundwater_steady",
            "gw_hydraulic_conductivity_m_s": 1e-4,
            "gw_porosity": 0.3,
            "gw_aquifer_thickness_m": 15.0,
            "gw_recharge_mm_yr": 300.0,
            "gw_steady_max_steps": 300,
        },
    )
    assert res.analysis == "groundwater_steady"
    assert res.output_field_name == "depth_to_water"
    dtw = np.asarray(res.field)
    assert dtw.shape == dem.shape
    # depth to water is non-negative where finite.
    assert np.nanmin(dtw) >= -1e-9
    assert "water_table_elevation" in res.secondary_fields
    e = res.extra
    # the mass-conservation gate: |rel error| < 1% (the V&V acceptance).
    assert abs(e["mass_balance_rel_error"]) < 0.01
    assert e["baseflow_discharge_m3s"] >= 0.0
    assert 0.0 <= e["seeping_area_fraction"] <= 1.0
    assert e["n_steps"] >= 1


@_REQUIRES_LANDLAB
def test_groundwater_steady_recharge_monotonicity_and_determinism():
    """MORE recharge => a SHALLOWER mean water table (smaller depth-to-water);
    two identical runs produce a byte-identical field (Invariant 1)."""
    dem = _tilted_dem(n=20)

    def _run(recharge):
        return run_component_chain(
            dem,
            resolution_m=30.0,
            build_spec={
                "analysis": "groundwater_steady",
                "gw_recharge_mm_yr": recharge,
                "gw_aquifer_thickness_m": 15.0,
                "gw_steady_max_steps": 300,
            },
        )

    lo = _run(100.0)
    hi = _run(600.0)
    # higher recharge raises the water table -> smaller mean depth-to-water.
    assert hi.extra["mean_depth_to_water_m"] < lo.extra["mean_depth_to_water_m"]
    # determinism: re-run of the low-recharge case is byte-identical.
    lo2 = _run(100.0)
    f1 = np.nan_to_num(np.asarray(lo.field), nan=-1.0)
    f2 = np.nan_to_num(np.asarray(lo2.field), nan=-1.0)
    assert np.array_equal(f1, f2)


@_REQUIRES_LANDLAB
def test_groundwater_storm_hydrograph_and_conservation():
    """The REAL storm-driven chain: peak-seepage primary + a baseflow hydrograph
    + the transient mass-conservation V&V + a fitted recession timescale."""
    dem = _tilted_dem(n=20)
    res = run_component_chain(
        dem,
        resolution_m=30.0,
        build_spec={
            "analysis": "groundwater_storm",
            "gw_storm_aquifer_thickness_m": 6.0,
            "gw_storm_mean_depth_mm": 20.0,
            "gw_storm_total_days": 90.0,
        },
    )
    assert res.analysis == "groundwater_storm"
    assert res.output_field_name == "peak_seepage_specific_discharge"
    assert np.asarray(res.field).shape == dem.shape
    e = res.extra
    # transient mass-conservation gate: |rel error| < 1%.
    assert abs(e["mass_balance_rel_error"]) < 0.01
    # a hydrograph with >= 2 points and a non-negative peak discharge.
    assert isinstance(e["hydrograph"], list) and len(e["hydrograph"]) >= 2
    assert e["peak_baseflow_m3s"] >= 0.0
    assert e["recession_timescale_days"] >= 0.0
    assert e["n_storms"] >= 1
    # JSON-safety of the extra block (the entrypoint folds it verbatim).
    import json

    json.dumps(e)
