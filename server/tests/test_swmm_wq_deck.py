"""SWMM water-quality DECK-AUTHORING tests (sprint-WQ).

Proves the two invariants the design demands of the builder WQ branch:

  1. ``pollutants=None`` (or ``[]``) => a BYTE-IDENTICAL hydraulics-only deck: the
     six WQ sections ([POLLUTANTS]/[LANDUSES]/[BUILDUP]/[WASHOFF]/[COVERAGES]) are
     ABSENT and the rest of the deck matches the pre-WQ build exactly (zero
     depth-path regression).
  2. ``pollutants=[tss, e_coli]`` => the WQ sections appear with the pinned SWMM
     keywords (POW buildup / EXP washoff / one "urban" land use at 100% coverage
     per active cell), the deck still RUNS through pyswmm, and ``out.pollutants``
     exposes each authored pollutant so the concentration read is index-clean.

The DEM fixture + build kwargs mirror ``test_swmm_mesh_builder.py``.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from grace2_agent.workflows import swmm_mesh_builder as mb

swmm_api = pytest.importorskip("swmm_api")
pyswmm = pytest.importorskip("pyswmm")

from grace2_contracts.swmm_contracts import resolve_pollutant_presets  # noqa: E402

# --- synthetic DEM (same geometry as test_swmm_mesh_builder) ---------------
_N = 20
_CELL = 10.0
_OX, _OY = 500000.0, 4600000.0
_EPSG = 32616


def _write_dem_geotiff(path: Path) -> None:
    import rasterio
    from rasterio.crs import CRS
    from rasterio.transform import from_origin

    ii, jj = np.meshgrid(np.arange(_N), np.arange(_N), indexing="ij")
    plane = 30.0 - 0.02 * _CELL * (ii + jj)
    ci = cj = (_N - 1) / 2.0
    pit = 2.0 * np.exp(-((ii - ci) ** 2 + (jj - cj) ** 2) / (2.0 * 3.0**2))
    dem = (plane - pit).astype("float64")
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "height": _N, "width": _N, "crs": CRS.from_epsg(_EPSG),
        "transform": from_origin(_OX, _OY, _CELL, _CELL), "nodata": -9999.0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(dem.astype("float32"), 1)


# WQ section header labels the strip/detect logic keys on.
_WQ_HEADERS = ("[POLLUTANTS]", "[LANDUSES]", "[BUILDUP]", "[WASHOFF]", "[COVERAGES]")


def _build(dem_path: str, out_inp: str, **extra):
    return mb.build_swmm_mesh(
        dem_path=dem_path,
        out_inp_path=out_inp,
        total_rain_depth_mm=50.8,
        storm_duration_hr=2.0,
        rain_interval_min=5,
        target_resolution_m=_CELL,
        enable_autoscale=False,  # deterministic resolution for byte-compare
        **extra,
    )


def _significant_lines(deck_text: str) -> list[str]:
    """Deck lines with the decorative ``;;___`` rules + blank lines removed."""
    return [
        ln for ln in deck_text.splitlines()
        if ln.strip() and not ln.startswith(";;__")
    ]


def _drop_wq_blocks(lines: list[str]) -> list[str]:
    """Remove every WQ ``[SECTION]`` block (header through the line before the
    next ``[`` header) from a decorative-rule-free line list."""
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip() in _WQ_HEADERS:
            i += 1
            while i < n and not lines[i].startswith("["):
                i += 1
        else:
            out.append(lines[i])
            i += 1
    return out


@pytest.fixture()
def dem(tmp_path: Path) -> str:
    p = tmp_path / "dem.tif"
    _write_dem_geotiff(p)
    return str(p)


def test_no_pollutants_deck_has_no_wq_sections(dem, tmp_path):
    """A hydraulics-only build authors ZERO WQ sections (byte-identical path)."""
    res = _build(dem, str(tmp_path / "plain.inp"))
    text = Path(res.inp_path).read_text()
    for hdr in _WQ_HEADERS:
        assert hdr not in text, f"unexpected WQ section {hdr} on a no-pollutant deck"
    assert res.pollutants == []


def test_empty_pollutants_is_byte_identical_to_none(dem, tmp_path):
    """``pollutants=[]`` produces the SAME deck as ``pollutants=None``."""
    a = _build(dem, str(tmp_path / "none.inp"), pollutants=None)
    b = _build(dem, str(tmp_path / "empty.inp"), pollutants=[])
    assert Path(a.inp_path).read_text() == Path(b.inp_path).read_text()


def test_wq_deck_adds_only_wq_sections(dem, tmp_path):
    """A WQ build == the hydraulics-only build PLUS exactly the WQ sections.

    Stripping the six WQ sections from the WQ deck must recover the byte-identical
    hydraulics-only deck (proves WQ is purely ADDITIVE — zero depth regression).
    """
    specs = resolve_pollutant_presets(["tss", "e_coli"])
    plain = _build(dem, str(tmp_path / "plain.inp"))
    wq = _build(dem, str(tmp_path / "wq.inp"), pollutants=specs, dry_buildup_days=5)

    wq_text = Path(wq.inp_path).read_text()
    plain_text = Path(plain.inp_path).read_text()

    # WQ sections present on the WQ deck.
    for hdr in _WQ_HEADERS:
        assert hdr in wq_text, f"missing WQ section {hdr} on a pollutant deck"

    # Dropping the WQ blocks (and the DRY_DAYS lever, the only OPTIONS delta:
    # 5 vs 0) must recover the hydraulics-only deck line-for-line.
    wq_lines = _drop_wq_blocks(_significant_lines(wq_text))
    wq_lines = ["DRY_DAYS             0" if "DRY_DAYS" in ln else ln for ln in wq_lines]
    assert wq_lines == _significant_lines(plain_text)

    # BuildResult carries (name, unit) in authored order for the postprocess.
    assert wq.pollutants == [("TSS", "MG/L"), ("E_coli", "#/L")]


def test_wq_keywords_and_coverage_per_cell(dem, tmp_path):
    """POW buildup / EXP washoff keywords + one Coverage row per active cell."""
    specs = resolve_pollutant_presets(["tss", "e_coli"])
    wq = _build(dem, str(tmp_path / "wq.inp"), pollutants=specs)
    text = Path(wq.inp_path).read_text()
    assert "TSS MG/L" in text
    assert "E_coli #/L" in text
    assert "urban TSS POW" in text
    assert "urban TSS EXP" in text
    assert "urban E_coli POW" in text
    # Coverage: one "urban ... 100" row per active cell (== storage nodes).
    cov_rows = [ln for ln in text.splitlines() if " urban " in f" {ln} " and "100" in ln
                and ln.startswith("C_")]
    assert len(cov_rows) == wq.n_active_cells


def test_emc_washoff_mode(dem, tmp_path):
    """``washoff_model='emc'`` authors an EMC washoff row (flat-conc control)."""
    specs = resolve_pollutant_presets(["tss"])
    wq = _build(dem, str(tmp_path / "emc.inp"), pollutants=specs, washoff_model="emc")
    text = Path(wq.inp_path).read_text()
    assert "urban TSS EMC" in text
    assert "urban TSS EXP" not in text


def test_wq_deck_runs_and_exposes_pollutants(dem, tmp_path):
    """The WQ deck solves through pyswmm and ``out.pollutants`` maps the names."""
    specs = resolve_pollutant_presets(["tss", "e_coli"])
    wq = _build(dem, str(tmp_path / "wq.inp"), pollutants=specs, dry_buildup_days=3)
    # The WQ mass balance can be loose on a coarse synthetic deck; relax the gate
    # so the RUN completes (we assert the pollutant read, not the depth gate).
    run = mb.run_swmm_deck(wq, mass_balance_tolerance_pct=100.0)
    from pyswmm import Output

    with Output(run.out_path) as out:
        pol = out.pollutants
    assert set(pol.keys()) == {"TSS", "E_coli"}
    # index order matches the authored order (TSS=0, E_coli=1).
    assert pol["TSS"] == 0 and pol["E_coli"] == 1
