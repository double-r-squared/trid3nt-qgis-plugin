"""REAL-FIXTURE deck-capture LOCAL PROOF for the full SFINCS archetype set.

NATE 2026-06-26: the TIER-2 "in-process build" proof for the SFINCS
scenario-coverage work (task #194). The companion fast/YAML-emission proof lives
in ``test_sfincs_builder_surge_forcing.py`` (which asserts the
``_generate_hydromt_yaml_config`` block keys). THIS module proves the next tier
down: that ``build_sfincs_model`` actually RUNS HydroMT-SFINCS end-to-end against
the REAL Mexico Beach fixture (``tests/fixtures/sfincs_aoi/{dem.tif,landcover.tif}``
-- USGS 3DEP + MRLC NLCD 2021, NOT synthetic) and that each archetype's
``ForcingSpec`` lands the right keys in the written ``sfincs.inp`` + the right
forcing artifacts in the deck dir.

Per-archetype deck surface proven here (read from real captured ``sfincs.inp``):
  - PLUVIAL          -> ``precipfile = sfincs.precip``         (+ ``qinf = 0.0`` baseline)
  - FLUVIAL          -> ``disfile``/``srcfile`` (edge inflow)  (+ NO precipfile)
  - COMPOUND         -> ``bzsfile``+``bndfile``+``disfile``+``srcfile``+``precipfile`` in ONE deck
  - COASTAL          -> ``bzsfile``/``bndfile`` (surge bzs) + msk==2 seaward cells
  - TSUNAMI          -> ``bzsfile``/``bndfile`` (N-wave bzs)   + msk==2 seaward cells
  - INFILTRATION     -> ``scsfile = sfincs.scs`` (SCS-CN)  [constant variant -> ``qinf``]
  - WIND             -> ``wndfile`` + ``cdval`` flat curve + ``cdnrb = 3`` + ``advection``
  - LEVEE-BREACH     -> ``disfile``/``srcfile`` from a single INTERIOR src point

THE LOAD-BEARING HARNESS CONSTRAINT (per sfincs-coverage-design.md): for a LOCAL
(non-s3) manifest URI ``build_sfincs_model`` writes the deck into an INTERNAL
``tempfile.TemporaryDirectory`` and SKIPS the object-store upload, so the deck is
DESTROYED on return and the returned ``setup_uri`` points at a manifest that is
never written. We therefore CAPTURE the deck by monkeypatching the builder
module's ``tempfile.TemporaryDirectory`` with a persist-shim that ``mkdtemp``s into
a test-owned dir, then glob ``<capture>/**/deck/sfincs.inp`` (the proven method).
"""

from __future__ import annotations

import datetime as _dt
import glob
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# The build tests RUN HydroMT-SFINCS in-process; skip cleanly where the heavy
# venv is absent (CI without hydromt_sfincs). The local-proof gate runs in
# services/agent/.venv where both are present.
pytest.importorskip("hydromt_sfincs")
pytest.importorskip("rasterio")

import rasterio  # noqa: E402

import grace2_agent.workflows.sfincs_builder as _builder  # noqa: E402
from grace2_agent.workflows.sfincs_builder import (  # noqa: E402
    BuildOptions,
    DischargeForcing,
    ForcingSpec,
    InfiltrationForcing,
    WaterlevelForcing,
    WindForcing,
    build_sfincs_model,
)
from grace2_agent.workflows.sfincs_forcing_adapter import (  # noqa: E402
    SFINCS_TREF,
    StationHydrograph,
    reanchor_to_tref,
    synthesize_tsunami_bzs,
    write_dis_timeseries_csv,
    write_locations_fgb,
)

# The REAL fixture (Mexico Beach FL, the coastal North Star geography). The DEM
# is EPSG:5070 USGS 3DEP (-0.36..6.95 m -- a true seaward-to-land gradient so
# setup_mask_bounds finds msk==2 seaward cells); the landcover is EPSG:4326 MRLC
# NLCD 2021 carrying classes {11,21,22,23,24,31,42,52,71,90,95}.
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sfincs_aoi"
_DEM = str(_FIXTURE_DIR / "dem.tif")
_LANDCOVER = str(_FIXTURE_DIR / "landcover.tif")
# EPSG:4326 bbox of the fixture (matches fixtures/sfincs_aoi/manifest.json).
_BBOX = (-85.42, 29.93, -85.39, 29.96)

# Manning's n for EXACTLY the 11 NLCD classes present in the real fixture's
# landcover.tif -- a SUBSET of the version-pinned manning_mapping.csv, so the
# OQ-4 §4 NLCD-vintage validation gate passes (subset coverage) without dragging
# in the full prod table. Values mirror the prod CSV rows.
_FIXTURE_MANNING_ROWS = (
    (11, 0.025, "Open Water"),
    (21, 0.035, "Developed Open Space"),
    (22, 0.060, "Developed Low Intensity"),
    (23, 0.100, "Developed Medium Intensity"),
    (24, 0.150, "Developed High Intensity"),
    (31, 0.030, "Barren Land"),
    (42, 0.150, "Evergreen Forest"),
    (52, 0.080, "Shrub/Scrub"),
    (71, 0.040, "Grassland/Herbaceous"),
    (90, 0.120, "Woody Wetlands"),
    (95, 0.080, "Emergent Herbaceous Wetlands"),
)


# --------------------------------------------------------------------------- #
# Fixtures + capture helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def manning_csv(tmp_path: Path) -> str:
    """Write the fixture-class-subset Manning CSV and return its path."""
    path = tmp_path / "manning_fixture_subset.csv"
    lines = ["nlcd_class,manning_n,description"]
    lines += [f"{c},{n},{d}" for c, n, d in _FIXTURE_MANNING_ROWS]
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _build_and_capture(
    forcing: ForcingSpec,
    options: BuildOptions,
    capture_root: Path,
    manning_csv_path: str,
) -> tuple[str, str, list[str]]:
    """Run ``build_sfincs_model`` against the REAL fixture, CAPTURING the deck.

    The builder destroys its internal ``TemporaryDirectory`` on return and skips
    the upload for a local manifest, so we monkeypatch the builder-module's
    ``tempfile.TemporaryDirectory`` with a persist-shim (the proven capture
    method) and glob the surviving ``deck/sfincs.inp``.

    Returns ``(deck_dir, sfincs_inp_text, sorted(os.listdir(deck_dir)))``.
    """
    # An isolated capture dir per build so concurrent archetypes never collide.
    root = tempfile.mkdtemp(prefix="capture-", dir=str(capture_root))

    class _PersistTmp:
        """Drop-in for ``tempfile.TemporaryDirectory`` that does NOT auto-delete."""

        def __init__(self, prefix: str = "", **_kw: object) -> None:
            self.name = tempfile.mkdtemp(prefix=prefix, dir=root)

        def __enter__(self) -> str:
            return self.name

        def __exit__(self, *_a: object) -> bool:
            return False  # never delete -> the deck survives for inspection

    with mock.patch.object(_builder.tempfile, "TemporaryDirectory", _PersistTmp):
        build_sfincs_model(
            dem_uri=_DEM,
            landcover_uri=_LANDCOVER,
            river_geometry_uri=None,
            forcing=forcing,
            bbox=_BBOX,
            options=options,
            nlcd_vintage_year=2021,
            manning_mapping_csv=manning_csv_path,
        )

    inps = glob.glob(os.path.join(root, "**", "deck", "sfincs.inp"), recursive=True)
    assert inps, f"no captured deck/sfincs.inp under {root!r}"
    deck_dir = os.path.dirname(inps[0])
    inp_text = Path(inps[0]).read_text()
    return deck_dir, inp_text, sorted(os.listdir(deck_dir))


def _inp_value(inp_text: str, key: str) -> str | None:
    """Return the value of a ``key = value`` line from a captured sfincs.inp."""
    for line in inp_text.splitlines():
        parts = line.split("=", 1)
        if len(parts) == 2 and parts[0].strip() == key:
            return parts[1].strip()
    return None


def _options(capture_root: Path, **overrides: object) -> BuildOptions:
    """BuildOptions with autoscale OFF (verbatim resolution) + a local manifest."""
    overrides.setdefault("simulation_hours", 24.0)
    return BuildOptions(
        grid_resolution_m=100.0,
        autoscale_grid=False,
        output_setup_uri=os.path.join(str(capture_root), "out", "manifest.json"),
        **overrides,  # type: ignore[arg-type]
    )


def _make_discharge_files(
    stage_dir: Path,
    *,
    name: str,
    lon: float,
    lat: float,
    values: tuple[float, float],
) -> tuple[str, str]:
    """Write a 1-station dis CSV + a 1-point locations FGB for a discharge point.

    Reuses the SAME adapter writers the fetcher fan-out / breach synth use, so the
    deck consumes them unchanged. The single station spans the deck window via the
    reanchor-to-tref 2-point series.
    """
    station = StationHydrograph(
        point_id=1,
        lon=lon,
        lat=lat,
        times=[SFINCS_TREF, SFINCS_TREF + _dt.timedelta(hours=24)],
        values=list(values),
        source_id=name,
    )
    series = reanchor_to_tref(station.times, station.values, window_hours=24.0)
    dis = write_dis_timeseries_csv({1: series}, str(stage_dir / f"{name}_dis.csv"))
    src = write_locations_fgb([station], str(stage_dir / f"{name}_src.fgb"))
    return dis, src


# --------------------------------------------------------------------------- #
# PLUVIAL - precipfile + qinf baseline OFF
# --------------------------------------------------------------------------- #


def test_pluvial_deck_emits_precipfile(tmp_path, manning_csv) -> None:
    """A pluvial ForcingSpec lands ``precipfile = sfincs.precip`` (and the
    infiltration baseline ``qinf = 0.0`` -- losses OFF by default)."""
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic", precip_inches=8.0, duration_hours=24.0
    )
    deck, inp, listing = _build_and_capture(
        forcing, _options(tmp_path), tmp_path, manning_csv
    )
    assert _inp_value(inp, "precipfile") == "sfincs.precip"
    assert "sfincs.precip" in listing
    # Infiltration loss baseline is OFF (the regression anchor for the CN test).
    assert _inp_value(inp, "qinf") == "0.0"
    # Pure pluvial -> none of the surge/loss artifacts.
    for absent in ("sfincs.bzs", "sfincs.dis", "sfincs.src", "sfincs.scs"):
        assert absent not in listing


# --------------------------------------------------------------------------- #
# FLUVIAL - edge river inflow: disfile + srcfile, NO precip
# --------------------------------------------------------------------------- #


def test_fluvial_deck_emits_disfile_and_srcfile(tmp_path, manning_csv) -> None:
    """A river DischargeForcing lands ``disfile``/``srcfile`` + the ``sfincs.dis``
    /``sfincs.src`` artifacts (a pure-fluvial deck has NO precipfile)."""
    stage = tmp_path / "fluvial_forcing"
    stage.mkdir()
    dis, src = _make_discharge_files(
        stage, name="river", lon=-85.408, lat=29.945, values=(50.0, 80.0)
    )
    forcing = ForcingSpec(
        forcing_type="fluvial",
        discharge=DischargeForcing(timeseries_uri=dis, locations_uri=src),
    )
    deck, inp, listing = _build_and_capture(
        forcing, _options(tmp_path), tmp_path, manning_csv
    )
    assert _inp_value(inp, "disfile") == "sfincs.dis"
    assert _inp_value(inp, "srcfile") == "sfincs.src"
    assert {"sfincs.dis", "sfincs.src"} <= set(listing)
    # Pure fluvial -> no rainfall.
    assert _inp_value(inp, "precipfile") is None
    assert "sfincs.precip" not in listing


# --------------------------------------------------------------------------- #
# COMPOUND - bzs + dis + precip co-present in ONE deck
# --------------------------------------------------------------------------- #


def test_compound_deck_carries_bzs_dis_and_precip(tmp_path, manning_csv) -> None:
    """A compound run (surge waterlevel + river discharge + Atlas-14 precip) lands
    ALL of bzsfile/bndfile + disfile/srcfile + precipfile in a single deck."""
    stage = tmp_path / "compound_forcing"
    stage.mkdir()
    dis, src = _make_discharge_files(
        stage, name="river", lon=-85.408, lat=29.945, values=(50.0, 80.0)
    )
    # Reuse the tsunami synth purely as a bzs waterlevel-series writer here (a
    # real surge hydrograph has the same bzs/bnd file shape).
    wl = synthesize_tsunami_bzs(
        _BBOX,
        eta_max_m=2.0,
        period_s=900.0,
        wave_type="ldn",
        window_hours=24.0,
        stage_dir=str(stage),
    )
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=6.0,
        duration_hours=24.0,
        return_period_years=100,
        waterlevel=WaterlevelForcing(
            timeseries_uri=wl["timeseries_uri"], locations_uri=wl["locations_uri"]
        ),
        discharge=DischargeForcing(timeseries_uri=dis, locations_uri=src),
    )
    deck, inp, listing = _build_and_capture(
        forcing, _options(tmp_path), tmp_path, manning_csv
    )
    assert _inp_value(inp, "bzsfile") == "sfincs.bzs"
    assert _inp_value(inp, "bndfile") == "sfincs.bnd"
    assert _inp_value(inp, "disfile") == "sfincs.dis"
    assert _inp_value(inp, "srcfile") == "sfincs.src"
    assert _inp_value(inp, "precipfile") == "sfincs.precip"
    assert {"sfincs.bzs", "sfincs.bnd", "sfincs.dis", "sfincs.src", "sfincs.precip"} <= set(
        listing
    )


# --------------------------------------------------------------------------- #
# COASTAL / TSUNAMI - bzs water-level boundary + msk==2 seaward cells
# --------------------------------------------------------------------------- #


def _count_msk2(deck_dir: str) -> int:
    """Count the seaward water-level boundary cells (msk==2) in the deck's mask.

    The mask raster lands in the deck's ``gis/`` dir as ``msk.tif``; msk==2 marks
    the water-level boundary cells. If those are 0 the bzs forcing is INERT (the
    surge-inundation root cause), so the coastal/tsunami proof asserts >0.
    """
    msk_path = os.path.join(deck_dir, "gis", "msk.tif")
    assert os.path.isfile(msk_path), f"no gis/msk.tif in {deck_dir!r}"
    with rasterio.open(msk_path) as ds:
        return int((ds.read(1) == 2).sum())


def test_coastal_surge_deck_emits_bzs_with_seaward_boundary(
    tmp_path, manning_csv
) -> None:
    """A surge WaterlevelForcing lands ``bzsfile``/``bndfile`` AND creates the
    seaward msk==2 boundary cells the bzs needs to drive (not inert)."""
    stage = tmp_path / "coastal_forcing"
    stage.mkdir()
    wl = synthesize_tsunami_bzs(
        _BBOX,
        eta_max_m=2.5,
        period_s=1800.0,
        wave_type="solitary",
        window_hours=24.0,
        stage_dir=str(stage),
    )
    forcing = ForcingSpec(
        forcing_type="storm_surge",
        waterlevel=WaterlevelForcing(
            timeseries_uri=wl["timeseries_uri"], locations_uri=wl["locations_uri"]
        ),
    )
    deck, inp, listing = _build_and_capture(
        forcing, _options(tmp_path), tmp_path, manning_csv
    )
    assert _inp_value(inp, "bzsfile") == "sfincs.bzs"
    assert _inp_value(inp, "bndfile") == "sfincs.bnd"
    assert {"sfincs.bzs", "sfincs.bnd"} <= set(listing)
    assert _count_msk2(deck) > 0, "no seaward msk==2 cells -> the bzs surge is inert"


def test_tsunami_deck_emits_bzs_nwave_with_seaward_boundary(
    tmp_path, manning_csv
) -> None:
    """A TSUNAMI leading-depression N-wave (synthesize_tsunami_bzs ldn) reuses the
    bzs water-level seam: ``bzsfile``/``bndfile`` + msk==2 seaward cells."""
    stage = tmp_path / "tsunami_forcing"
    stage.mkdir()
    wl = synthesize_tsunami_bzs(
        _BBOX,
        eta_max_m=3.0,
        period_s=900.0,
        wave_type="ldn",
        lead_depression=True,
        window_hours=6.0,
        stage_dir=str(stage),
    )
    forcing = ForcingSpec(
        forcing_type="storm_surge",
        waterlevel=WaterlevelForcing(
            timeseries_uri=wl["timeseries_uri"], locations_uri=wl["locations_uri"]
        ),
    )
    deck, inp, listing = _build_and_capture(
        forcing, _options(tmp_path, simulation_hours=6.0), tmp_path, manning_csv
    )
    assert _inp_value(inp, "bzsfile") == "sfincs.bzs"
    assert _inp_value(inp, "bndfile") == "sfincs.bnd"
    assert {"sfincs.bzs", "sfincs.bnd"} <= set(listing)
    assert _count_msk2(deck) > 0, "no seaward msk==2 cells -> the tsunami bzs is inert"


# --------------------------------------------------------------------------- #
# INFILTRATION - SCS-CN scsfile (single-band GCN250, antecedent_moisture=null)
# --------------------------------------------------------------------------- #


def _write_cn_raster(path: str, *, cn: float = 75.0) -> str:
    """Write a tiny single-band float32 GCN250-style CN raster over the bbox."""
    from rasterio.transform import from_bounds

    transform = from_bounds(*_BBOX, 20, 20)
    import numpy as np

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=20,
        width=20,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as ds:
        ds.write(np.full((20, 20), cn, dtype="float32"), 1)
    return path


def test_infiltration_cn_deck_emits_scsfile(tmp_path, manning_csv) -> None:
    """A ``cn_uri`` infiltration member runs ``setup_cn_infiltration`` (single-band
    -> antecedent_moisture null) and lands ``scsfile = sfincs.scs`` in the deck."""
    cn = _write_cn_raster(str(tmp_path / "gcn250.tif"))
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=8.0,
        duration_hours=24.0,
        infiltration=InfiltrationForcing(cn_uri=cn, antecedent_moisture=None),
    )
    deck, inp, listing = _build_and_capture(
        forcing, _options(tmp_path), tmp_path, manning_csv
    )
    assert _inp_value(inp, "scsfile") == "sfincs.scs"
    assert "sfincs.scs" in listing
    # Precip still drives the flood; CN is the loss term layered on top.
    assert _inp_value(inp, "precipfile") == "sfincs.precip"


# --------------------------------------------------------------------------- #
# WIND - wndfile + advanced-physics cdval flat curve + advection
# --------------------------------------------------------------------------- #


def test_wind_deck_emits_wndfile_and_physics_switches(tmp_path, manning_csv) -> None:
    """A uniform WindForcing + advanced_physics lands a ``wndfile`` artifact AND
    the physics switches in sfincs.inp: a flat ``cdval`` drag curve (cdnrb=3),
    ``advection``, and ``latitude`` (coriolis) -- the only true proof the switch
    reached the engine (the physics_registry FIX: wind_drag->cdval, not cdwnd;
    coriolis_latitude->latitude, not a fictional coriolis key)."""
    forcing = ForcingSpec(
        forcing_type="storm_surge",
        wind=WindForcing(magnitude=45.0, direction=170.0),
    )
    options = _options(
        tmp_path,
        advanced_physics={
            "advection": 1,
            "coriolis_latitude": 29.9,
            "wind_drag": 0.0025,
        },
    )
    deck, inp, listing = _build_and_capture(forcing, options, tmp_path, manning_csv)
    # The uniform-wind forcing artifact landed.
    assert _inp_value(inp, "wndfile") == "sfincs.wnd"
    assert "sfincs.wnd" in listing
    # The physics switches reached sfincs.inp via setup_config passthrough.
    assert _inp_value(inp, "advection") == "1"
    assert _inp_value(inp, "cdnrb") == "3"
    assert _inp_value(inp, "cdval") == "0.0025 0.0025 0.0025"
    assert _inp_value(inp, "latitude") == "29.9"


# --------------------------------------------------------------------------- #
# LEVEE-BREACH - interior dis point source (disfile/srcfile, single src)
# --------------------------------------------------------------------------- #


def test_levee_breach_deck_emits_interior_discharge_point(
    tmp_path, manning_csv
) -> None:
    """A ``breach`` DischargeForcing (interior Point + hydrograph, NO rivers) lands
    ``disfile``/``srcfile`` from a single INTERIOR src point -- the levee-breach
    seam (setup_discharge_forcing with explicit locations, no setup_river_inflow)."""
    stage = tmp_path / "breach_forcing"
    stage.mkdir()
    # A point well inside the AOI (the breach cell), NOT on a domain edge.
    bdis, bsrc = _make_discharge_files(
        stage, name="breach", lon=-85.405, lat=29.945, values=(0.0, 120.0)
    )
    forcing = ForcingSpec(
        forcing_type="storm_surge",
        breach=DischargeForcing(timeseries_uri=bdis, locations_uri=bsrc),
    )
    deck, inp, listing = _build_and_capture(
        forcing, _options(tmp_path), tmp_path, manning_csv
    )
    assert _inp_value(inp, "disfile") == "sfincs.dis"
    assert _inp_value(inp, "srcfile") == "sfincs.src"
    assert {"sfincs.dis", "sfincs.src"} <= set(listing)
    # The breach is a single INTERIOR src point (not an edge river-inflow fan).
    src_lines = [
        ln for ln in Path(os.path.join(deck, "sfincs.src")).read_text().splitlines()
        if ln.strip()
    ]
    assert len(src_lines) == 1, f"breach must be ONE interior src point, got {src_lines}"
    # A pure breach carries no rainfall.
    assert "sfincs.precip" not in listing
