"""Regression tests for ``postprocess_capture_zone`` georegistration + tiers.

These guard the two blocking defects the Wave-4 adversarial review caught on a
real mf6 run (and that the full GWF->PRT->postprocess chain otherwise hid):

  1. The PRT GWF grid is built at LOCAL (0,0) origin (an mf6 6.7.0
     coordinate-check float-precision workaround), so the true UTM origin is NOT
     recoverable from any on-disk file.  It MUST be threaded explicitly via
     ``xoffset_m`` / ``yoffset_m`` -- otherwise the convex-hull polygon
     reprojects from local coords and lands ~thousands of km from the well
     (near the equator), yet still passes the translation-invariant area floor
     (a silent Invariant-1 honesty failure).
  2. The user/composer-requested travel-time isochrone tiers
     (``capture_zone`` [1, 5, 10] yr / ``wellhead_protection`` [2, 5, 10] yr)
     MUST be honored verbatim, not silently replaced by data-driven tiers.

The tests build a SYNTHETIC PRT track CSV in LOCAL coordinates (no mf6 needed --
the full two-sim mf6 chain is proven separately by the gated worker test
``test_real_run_capture_zone_produces_pathlines``), then assert the polygon
lands at the real well lon/lat and the tiers equal the request.  The FlatGeobuf
upload is monkeypatched to a ``file://`` URI so the test touches no storage.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from trid3nt_server.workflows import postprocess_modflow as pp
from trid3nt_server.workflows.postprocess_modflow import (
    PostprocessMODFLOWError,
    postprocess_capture_zone,
)

# A known well location + its UTM zone.  EPSG:32615 = UTM 15N (covers ~ -96..-90 lon).
_WELL_LON = -93.0
_WELL_LAT = 41.5
_UTM_EPSG = 32615
#: Local grid coordinate of the well (a 4100 m domain built at 0-origin, the
#: PRT adapter's 41x41 at 100 m -- the well sits at the centre cell).
_WELL_LOCAL_X = 2050.0
_WELL_LOCAL_Y = 2050.0


def _well_utm() -> tuple[float, float]:
    """True UTM easting/northing of the well (EPSG:4326 -> EPSG:32615)."""
    from pyproj import Transformer

    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{_UTM_EPSG}", always_xy=True)
    return to_utm.transform(_WELL_LON, _WELL_LAT)  # (easting, northing)


def _write_synthetic_track(csv_path: Path) -> None:
    """Write a synthetic PRT track CSV: 8 particles tracking backward (west).

    Coordinates are LOCAL (0-origin), as the real PRT track CSV is.  Each
    particle starts in a ring at the well and migrates up-gradient (decreasing
    x) over time; ``t`` is in DAYS.  Vertices straddle the 1 / 5 / 10-year tier
    cutoffs (365.25 / 1826.25 / 3652.5 d) so each cumulative tier has >= 3
    points.
    """
    t_days = [100.0, 300.0, 800.0, 2000.0, 3500.0]
    rows = ["kper,kstp,imdl,iprp,irpt,ilay,icell,izone,istatus,ireason,trelease,t,x,y,z,name"]
    for p in range(8):
        a = 2.0 * math.pi * p / 8.0
        for t in t_days:
            frac = t / 3500.0
            x = _WELL_LOCAL_X - frac * 1900.0 + 30.0 * math.cos(a)
            y = _WELL_LOCAL_Y + 30.0 * math.sin(a) + frac * 250.0 * math.sin(a)
            rows.append(
                f"1,1,0,1,{p},0,0,0,0,1,0.0,{t},{x:.3f},{y:.3f},25.0,p{p}"
            )
    csv_path.write_text("\n".join(rows) + "\n")


@pytest.fixture()
def _prt_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PRT working dir holding a synthetic ``prtmodel.trk.csv``; no storage."""
    csv = tmp_path / "prtmodel.trk.csv"
    _write_synthetic_track(csv)
    # Keep the FlatGeobuf local: return a file:// URI instead of uploading.
    monkeypatch.setattr(
        pp, "_upload_fgb", lambda local_fgb, run_id, runs_bucket, **kw: f"file://{local_fgb}"
    )
    return tmp_path


def test_capture_zone_georegistration_lands_at_well(_prt_dir: Path) -> None:
    """With the true UTM offset threaded, the polygon lands at the well lon/lat.

    Without the offset (the blocking defect), the centroid would land near the
    equator (~lat 0), so this is the guard for defect #1.
    """
    east, north = _well_utm()
    layer = postprocess_capture_zone(
        str(_prt_dir),
        run_id="cz-georef",
        model_crs=f"EPSG:{_UTM_EPSG}",
        deck_dir=str(_prt_dir),
        xoffset_m=east - _WELL_LOCAL_X,
        yoffset_m=north - _WELL_LOCAL_Y,
        model_utm_epsg=_UTM_EPSG,
        tier_years=[1.0, 5.0, 10.0],
    )
    assert layer.layer_type == "vector"
    assert layer.capture_zone_area_km2 > 0.0
    assert layer.bbox is not None
    min_lon, min_lat, max_lon, max_lat = layer.bbox
    cx = 0.5 * (min_lon + max_lon)
    cy = 0.5 * (min_lat + max_lat)
    # The capture zone spreads a few km WEST of the well; the centroid must still
    # be within ~0.2 deg of the well -- and emphatically NOT at the equator.
    assert abs(cx - _WELL_LON) < 0.2, f"centroid lon {cx} far from well {_WELL_LON}"
    assert abs(cy - _WELL_LAT) < 0.2, f"centroid lat {cy} far from well {_WELL_LAT}"


def test_capture_zone_honours_requested_tiers(_prt_dir: Path) -> None:
    """The requested isochrone tiers are used verbatim (defect #2 guard)."""
    east, north = _well_utm()
    layer = postprocess_capture_zone(
        str(_prt_dir),
        run_id="cz-tiers",
        model_crs=f"EPSG:{_UTM_EPSG}",
        deck_dir=str(_prt_dir),
        xoffset_m=east - _WELL_LOCAL_X,
        yoffset_m=north - _WELL_LOCAL_Y,
        model_utm_epsg=_UTM_EPSG,
        tier_years=[1.0, 5.0, 10.0],
    )
    assert layer.travel_time_years == [1.0, 5.0, 10.0]
    assert set(layer.isochrone_areas_km2.keys()) == {"1", "5", "10"}
    # Nested zones of contribution: 1yr <= 5yr <= 10yr.
    a1 = layer.isochrone_areas_km2["1"]
    a5 = layer.isochrone_areas_km2["5"]
    a10 = layer.isochrone_areas_km2["10"]
    assert a1 <= a5 <= a10


def test_wellhead_protection_tiers_distinct(_prt_dir: Path) -> None:
    """A wellhead_protection request keeps the EPA [2, 5, 10] tiers (not [1,5,10])."""
    east, north = _well_utm()
    layer = postprocess_capture_zone(
        str(_prt_dir),
        run_id="whpa-tiers",
        model_crs=f"EPSG:{_UTM_EPSG}",
        deck_dir=str(_prt_dir),
        xoffset_m=east - _WELL_LOCAL_X,
        yoffset_m=north - _WELL_LOCAL_Y,
        model_utm_epsg=_UTM_EPSG,
        tier_years=[2.0, 5.0, 10.0],
    )
    assert layer.travel_time_years == [2.0, 5.0, 10.0]
    assert set(layer.isochrone_areas_km2.keys()) == {"2", "5", "10"}


def test_capture_zone_honesty_guard_zero_offset_real_utm(_prt_dir: Path) -> None:
    """A real UTM CRS with a (0,0) offset must RAISE, not emit an equator polygon."""
    with pytest.raises(PostprocessMODFLOWError) as exc:
        postprocess_capture_zone(
            str(_prt_dir),
            run_id="cz-guard",
            model_crs=f"EPSG:{_UTM_EPSG}",
            deck_dir=None,  # no deck reload -> offset stays (0,0)
            xoffset_m=0.0,
            yoffset_m=0.0,
            model_utm_epsg=_UTM_EPSG,
            tier_years=[1.0, 5.0, 10.0],
        )
    assert exc.value.error_code == "CAPTURE_ZONE_OUTPUT_READ_FAILED"


def test_capture_zone_data_driven_fallback_when_no_tiers(_prt_dir: Path) -> None:
    """With no tiers supplied, tiers are derived from the data (last-resort)."""
    east, north = _well_utm()
    layer = postprocess_capture_zone(
        str(_prt_dir),
        run_id="cz-fallback",
        model_crs=f"EPSG:{_UTM_EPSG}",
        deck_dir=str(_prt_dir),
        xoffset_m=east - _WELL_LOCAL_X,
        yoffset_m=north - _WELL_LOCAL_Y,
        model_utm_epsg=_UTM_EPSG,
        tier_years=None,
    )
    # The fallback derives tiers from the observed travel-time range (max ~9.6 yr
    # here), so they are NOT the canonical [1, 5, 10] request.
    assert layer.travel_time_years
    assert layer.capture_zone_area_km2 > 0.0
