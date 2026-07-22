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
# agent venv). In the worker image ``/opt/grace2`` is the only PYTHONPATH entry
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
