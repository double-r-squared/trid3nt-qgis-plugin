"""Unit tests for the DOMAIN-ADAPTIVE SFINCS active-cell mask (job-0318).

CONFIRMED BUG (job-0318): ``sfincs_builder._generate_hydromt_yaml_config``
composed the hydromt-sfincs config with a HARDCODED active-cell mask::

    setup_mask_active:
      zmin: -10.0
      zmax: 10.0

``setup_mask_active``'s ``zmin``/``zmax`` is an ELEVATION WINDOW in metres —
only DEM cells whose elevation falls inside ``[-10, 10]`` become ACTIVE. For
inland / elevated terrain (Asheville sits at ~650 m) EVERY cell exceeds
``zmax=10`` -> ``hydromt_sfincs`` logs "No active cells found" -> empty domain
-> SFINCS produces zero inundation -> Pelicun fails
``PELICUN_NO_ASSETS_IN_HAZARD``. The flood model has only ever worked for
near-sea-level / coastal AOIs.

The fix reads the staged DEM's ACTUAL elevation min/max (rasterio, masking
nodata) and emits a window that brackets the full terrain with a small buffer,
so the whole AOI is active regardless of elevation. If the DEM range cannot be
read the bounds fall back to a VERY wide window (NEVER the broken -10/10), so a
flood deck is never silently emitted with an empty active mask (Invariant 7).

These tests PROVE:

1. A high-elevation DEM (~600-1000 m, Asheville-like) yields a ``zmin``/``zmax``
   window that INCLUDES that elevation range — i.e. active cells WOULD exist.
   With the OLD hardcoded -10/10 the whole AOI would be masked out.
2. A coastal DEM (~-5..+20 m, Fort-Myers-like) STILL yields a valid bracketing
   window covering both wet (below-zero topobathy) and dry cells — the existing
   coastal behaviour is NOT regressed.
3. An unreadable / missing DEM falls back to a wide window that never excludes
   land, and the fallback is NOT the broken -10/10.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds

from trid3nt_server.workflows.sfincs_builder import (
    _MASK_FALLBACK_ZMAX,
    _MASK_FALLBACK_ZMIN,
    BuildOptions,
    ForcingSpec,
    _compute_active_mask_bounds,
    _generate_hydromt_yaml_config,
)

# Asheville-ish inland AOI (high elevation) — the geography the bug breaks.
_ASHEVILLE_BBOX = (-82.60, 35.55, -82.50, 35.62)
# Fort-Myers-ish coastal AOI (near sea level) — the geography that worked.
_COASTAL_BBOX = (-82.00, 26.55, -81.85, 26.70)

# Design-storm forcing reused across the deck-emission assertions.
_FORCING = ForcingSpec(
    forcing_type="pluvial_synthetic",
    precip_inches=8.0,
    duration_hours=24.0,
    return_period_years=100,
)


def _write_dem(
    path: Path,
    values: np.ndarray,
    *,
    nodata: float | None = None,
    bbox: tuple[float, float, float, float] = _ASHEVILLE_BBOX,
) -> Path:
    """Write a single-band float32 GeoTIFF DEM with the given elevation values."""
    height, width = values.shape
    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], width, height)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": height,
        "width": width,
        "crs": "EPSG:4326",
        "transform": transform,
    }
    if nodata is not None:
        profile["nodata"] = nodata
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values.astype("float32"), 1)
    return path


def _parse_mask_bounds(yaml_text: str) -> tuple[float, float]:
    """Extract the emitted ``setup_mask_active`` zmin/zmax floats from the YAML."""
    zmin = zmax = None
    lines = yaml_text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "setup_mask_active:":
            # zmin/zmax are the next two indented keys; strip any trailing
            # ``# ...`` comment before float-parsing.
            for sub in lines[i + 1 : i + 3]:
                key, _, rest = sub.strip().partition(":")
                val = rest.split("#", 1)[0].strip()
                if key == "zmin":
                    zmin = float(val)
                elif key == "zmax":
                    zmax = float(val)
            break
    assert zmin is not None and zmax is not None, (
        f"could not parse setup_mask_active bounds from:\n{yaml_text}"
    )
    return zmin, zmax


# --------------------------------------------------------------------------- #
# Test 1 — high-elevation DEM yields a window that INCLUDES the terrain
# --------------------------------------------------------------------------- #


def test_high_elevation_dem_yields_inclusive_active_window() -> None:
    """An Asheville-like ~600-1000 m DEM yields zmin/zmax bracketing that range.

    This is the headline of job-0318: with the OLD hardcoded -10/10 window EVERY
    cell of this DEM (all >= ~600 m) would be excluded -> empty active mask ->
    zero inundation -> Pelicun PELICUN_NO_ASSETS_IN_HAZARD. The adaptive bounds
    must INCLUDE the full [dem_min, dem_max] so active cells exist.
    """
    # Realistic Asheville terrain: a 32x32 grid spanning ~620 m valley floor to
    # ~990 m ridge.
    rng = np.random.default_rng(42)
    elev = rng.uniform(620.0, 990.0, size=(32, 32)).astype("float32")
    dem_min, dem_max = float(elev.min()), float(elev.max())

    with tempfile.TemporaryDirectory() as td:
        dem_path = _write_dem(Path(td) / "dep.tif", elev)

        # (a) the helper directly
        zmin, zmax, adaptive = _compute_active_mask_bounds(str(dem_path))
        assert adaptive is True, "DEM range was readable -> must be adaptive"
        # The window must BRACKET the real terrain (active cells exist).
        assert zmin <= dem_min, f"zmin={zmin} must be <= dem_min={dem_min}"
        assert zmax >= dem_max, f"zmax={zmax} must be >= dem_max={dem_max}"
        # And it must reach UP into the high-elevation band — definitively NOT
        # the broken 10 m ceiling that masked everything out.
        assert zmax > 10.0
        assert zmax >= 990.0
        # Sanity: buffered a few metres beyond the terrain, not absurdly wide
        # (the wide fallback would be -1000/9000).
        assert zmax < 1100.0
        assert zmin > 500.0

        # (b) the full deck emission carries the same inclusive window
        yaml_text = _generate_hydromt_yaml_config(
            bbox=_ASHEVILLE_BBOX,
            options=BuildOptions(grid_resolution_m=30.0, simulation_hours=24.0),
            dem_local_path=str(dem_path),
            landcover_local_path="/tmp/lc.tif",
            river_local_path=None,
            forcing=_FORCING,
            mapping_csv_path="/tmp/manning.csv",
        )
        assert "setup_mask_active:" in yaml_text
        y_zmin, y_zmax = _parse_mask_bounds(yaml_text)
        assert y_zmin <= dem_min and y_zmax >= dem_max
        # The deck must NOT carry the old broken ceiling.
        assert "  zmax: 10.0" not in yaml_text
        assert "  zmin: -10.0" not in yaml_text


# --------------------------------------------------------------------------- #
# Test 2 — coastal DEM still yields a valid bracketing window (no regression)
# --------------------------------------------------------------------------- #


def test_coastal_dem_still_yields_valid_active_window() -> None:
    """A Fort-Myers-like -5..+20 m DEM still yields a valid bracketing window.

    The coastal case is the geography the flood model HAS worked for; this is
    the no-regression guard. The adaptive window must still cover both the wet
    (below-zero topobathy) and dry (above-zero) cells so the active mask is
    non-empty.
    """
    # Coastal topobathy: -5 m (shallow water / below-MSL) up to +20 m (dry land).
    rng = np.random.default_rng(7)
    elev = rng.uniform(-5.0, 20.0, size=(32, 32)).astype("float32")
    dem_min, dem_max = float(elev.min()), float(elev.max())

    with tempfile.TemporaryDirectory() as td:
        dem_path = _write_dem(Path(td) / "dep.tif", elev, bbox=_COASTAL_BBOX)

        zmin, zmax, adaptive = _compute_active_mask_bounds(str(dem_path))
        assert adaptive is True
        # Brackets the full topobathy range — active cells (wet AND dry) exist.
        assert zmin <= dem_min, f"zmin={zmin} must be <= dem_min={dem_min}"
        assert zmax >= dem_max, f"zmax={zmax} must be >= dem_max={dem_max}"
        # Below-zero topobathy stays inside the window (coastal correctness).
        assert zmin < 0.0
        # Window is a sensible coastal bracket, NOT the absurd wide fallback.
        assert zmin > -50.0
        assert zmax < 50.0
        # A non-empty, ordered window.
        assert zmax > zmin

        yaml_text = _generate_hydromt_yaml_config(
            bbox=_COASTAL_BBOX,
            options=BuildOptions(grid_resolution_m=30.0, simulation_hours=24.0),
            dem_local_path=str(dem_path),
            landcover_local_path="/tmp/lc.tif",
            river_local_path=None,
            forcing=_FORCING,
            mapping_csv_path="/tmp/manning.csv",
        )
        y_zmin, y_zmax = _parse_mask_bounds(yaml_text)
        assert y_zmin <= dem_min and y_zmax >= dem_max


# --------------------------------------------------------------------------- #
# Test 3 — nodata cells are ignored when computing the elevation range
# --------------------------------------------------------------------------- #


def test_nodata_cells_ignored_in_dem_range() -> None:
    """nodata / sentinel cells must not pull the active window off the real terrain.

    A -9999 nodata sentinel in a high-elevation DEM must be masked; otherwise
    ``zmin`` would crash to ~-10000 and the window, while still bracketing land,
    would misrepresent the domain depth.
    """
    elev = np.full((16, 16), 700.0, dtype="float32")
    elev[0, 0] = -9999.0  # nodata sentinel
    elev[5, 5] = 850.0  # a real high point

    with tempfile.TemporaryDirectory() as td:
        dem_path = _write_dem(Path(td) / "dep.tif", elev, nodata=-9999.0)
        zmin, zmax, adaptive = _compute_active_mask_bounds(str(dem_path))
        assert adaptive is True
        # The -9999 sentinel was ignored: zmin tracks the real 700 m floor, not
        # the sentinel.
        assert zmin > 500.0
        assert zmin <= 700.0
        assert zmax >= 850.0


# --------------------------------------------------------------------------- #
# Test 4 — unreadable DEM falls back to a wide window (NOT the broken -10/10)
# --------------------------------------------------------------------------- #


def test_unreadable_dem_falls_back_to_wide_window() -> None:
    """A missing / unreadable DEM falls back to a window wide enough to never
    exclude land — and explicitly NOT the broken -10/10 (Invariant 7)."""
    zmin, zmax, adaptive = _compute_active_mask_bounds("/tmp/does-not-exist-dep.tif")
    assert adaptive is False
    assert (zmin, zmax) == (_MASK_FALLBACK_ZMIN, _MASK_FALLBACK_ZMAX)
    # The fallback must NOT silently re-introduce the broken window.
    assert not (zmin == -10.0 and zmax == 10.0)
    # Wide enough to bracket any real terrain on Earth (below-sea-level basins
    # through the highest peaks).
    assert zmin <= -430.0  # Dead Sea shore ~-430 m
    assert zmax >= 8849.0  # Everest ~8849 m

    # The deck emission marks the fallback so it is auditable.
    yaml_text = _generate_hydromt_yaml_config(
        bbox=_ASHEVILLE_BBOX,
        options=BuildOptions(grid_resolution_m=30.0, simulation_hours=24.0),
        dem_local_path="/tmp/does-not-exist-dep.tif",
        landcover_local_path="/tmp/lc.tif",
        river_local_path=None,
        forcing=_FORCING,
        mapping_csv_path="/tmp/manning.csv",
    )
    y_zmin, y_zmax = _parse_mask_bounds(yaml_text)
    assert (y_zmin, y_zmax) == (_MASK_FALLBACK_ZMIN, _MASK_FALLBACK_ZMAX)
    assert "wide fallback" in yaml_text


# --------------------------------------------------------------------------- #
# Test 5 — all-nodata DEM falls back to the wide window
# --------------------------------------------------------------------------- #


def test_all_nodata_dem_falls_back_to_wide_window() -> None:
    """A DEM whose every cell is nodata has no valid range -> wide fallback."""
    elev = np.full((8, 8), -9999.0, dtype="float32")
    with tempfile.TemporaryDirectory() as td:
        dem_path = _write_dem(Path(td) / "dep.tif", elev, nodata=-9999.0)
        zmin, zmax, adaptive = _compute_active_mask_bounds(str(dem_path))
        assert adaptive is False
        assert (zmin, zmax) == (_MASK_FALLBACK_ZMIN, _MASK_FALLBACK_ZMAX)
