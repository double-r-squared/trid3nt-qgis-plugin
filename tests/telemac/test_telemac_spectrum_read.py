"""The spectrum at a point: the one read over a POLAR grid rather than a mesh.

Offline: the punctual file a TOMAWAC deck names is stated here - a JONSWAP-like
directional spectrum over a frequency-direction grid the engine's own geometry
rules build - and what is proved is the decode, the point the read answers for,
and the refusals when the deck named no file or the file is not polar.
"""

from __future__ import annotations

import math
import struct
from typing import Any

import numpy as np
import pytest

from trid3nt_server.workflows.telemac.modules import WAC, spectrum
from trid3nt_server.workflows.telemac.modules.outputs import (
    OutputEmpty,
    Solved,
    Spectrum,
)

#: The grid the deck below states: the engine lays NF frequencies from MINIMAL
#: FREQUENCY at the FREQUENTIAL RATIO, and NDIRE equal sectors of the circle.
_MINIMAL_HZ = 0.2
_RATIO = 1.1
_FREQUENCIES = 8
_DIRECTIONS = 12
#: Where the stated spectrum peaks: a frequency of the grid and a sector of it,
#: so the read's peak is a number a reader can check against the statement.
_PEAK_FREQUENCY = 3
_PEAK_DIRECTION = 4
#: The two printout points the deck names, as the 1-based 2D mesh nodes the
#: engine snapped them onto.
_NEAR_NODE = 2
_FAR_NODE = 5


def _polar() -> tuple[Any, Any]:
    """The punctual file's own mesh: one node per (frequency, direction)."""
    frequency = _MINIMAL_HZ * _RATIO ** np.arange(_FREQUENCIES)
    angle = np.radians(np.arange(_DIRECTIONS) * 360.0 / _DIRECTIONS)
    f, theta = np.meshgrid(frequency, angle, indexing="ij")
    return (f.ravel() * np.cos(theta.ravel()), f.ravel() * np.sin(theta.ravel()))


def _density(scale: float) -> np.ndarray:
    """A spectrum with ONE cell of energy in it, scaled - the read's peak is
    then the cell the statement put it in and its variance is arithmetic."""
    grid = np.zeros((_FREQUENCIES, _DIRECTIONS))
    grid[_PEAK_FREQUENCY, _PEAK_DIRECTION] = scale
    return grid.ravel()


def _spectra(*, varnames: list[str], values: list[np.ndarray],
             npoin: int | None = None) -> dict[str, Any]:
    x, y = _polar()
    return {"varnames": varnames, "varunits": ["UNITE SI"] * len(varnames),
            "npoin": npoin or x.size, "nelem": 0, "nplan": 1,
            "npoin2": npoin or x.size, "nelem2": 0,
            "x": x, "y": y, "ikle": np.zeros((0, 3), dtype="int64"),
            "ikle2": np.zeros((0, 3), dtype="int64"),
            "x_origin": 0, "y_origin": 0,
            "times": np.asarray([0.0, 600.0], dtype="float64"),
            "data": {name: np.vstack([np.zeros_like(frame), frame])
                     for name, frame in zip(varnames, values)}}


def _wave_field() -> dict[str, Any]:
    """The module's own 2D result: six nodes of a UTM mesh, one frame."""
    x = np.asarray([500000.0, 500100.0, 500200.0, 500300.0, 500400.0, 500500.0])
    y = np.asarray([4400000.0] * 6)
    return {"varnames": ["WAVE HEIGHT HM0"], "varunits": ["M"],
            "npoin": 6, "nelem": 0, "nplan": 1, "npoin2": 6, "nelem2": 0,
            "x": x, "y": y, "ikle": np.zeros((0, 3), dtype="int64"),
            "ikle2": np.zeros((0, 3), dtype="int64"),
            "x_origin": 0, "y_origin": 0,
            "times": np.asarray([0.0, 600.0], dtype="float64"),
            "data": {"WAVE HEIGHT HM0": np.zeros((2, 6))}}


def _solved(monkeypatch, spectra: dict[str, Any] | None,
            *, named: str = "resWac.spe") -> Solved:
    """A solved standalone TOMAWAC run whose deck named its punctual file."""
    monkeypatch.setattr(
        "trid3nt_server.workflows.solver.solver.download_result",
        lambda run_id, basename, error_code=None: f"/tmp/{basename}")
    monkeypatch.setattr(
        "trid3nt_server.workflows.telemac.modules.outputs.read_selafin",
        lambda path, **_kw: spectra if str(path).endswith(".spe") else _wave_field())
    filled = ({"PUNCTUAL_RESULTS_FILE": {"keyword": "PUNCTUAL RESULTS FILE",
                                         "value": named, "provenance": "template"}}
              if named else {})
    return Solved({"run_id": "RID", "utm_epsg": 32610,
                   "result_basename": "tomawac_waves.slf", "name": "duck",
                   "module": "tomawac", "sheet": {"filled": filled}}, WAC)


def _read(solved: Solved, primitive) -> Spectrum:
    return WAC.READS["spectrum"](primitive.key, solved)


def test_the_polar_grid_decodes_into_frequencies_and_directions(monkeypatch):
    """The file's mesh IS the spectral grid: every node is one frequency at one
    direction, so the read rebuilds F(f, theta) from the coordinates alone."""
    solved = _solved(monkeypatch, _spectra(
        varnames=[f"F00001PT2D{_NEAR_NODE:06d}"], values=[_density(4.0)]))
    read = _read(solved, spectrum())
    assert read.frequency_hz.size == _FREQUENCIES
    assert read.directions_deg.size == _DIRECTIONS
    assert read.density.shape == (_FREQUENCIES, _DIRECTIONS)
    assert read.frequency_hz[0] == pytest.approx(_MINIMAL_HZ)
    assert read.frequency_hz[-1] == pytest.approx(
        _MINIMAL_HZ * _RATIO ** (_FREQUENCIES - 1))
    assert read.density[_PEAK_FREQUENCY, _PEAK_DIRECTION] == pytest.approx(4.0)
    assert read.units == "m2/Hz"


def test_the_measures_are_the_energy_the_stated_spectrum_carries(monkeypatch):
    """The energy against frequency is the directional spectrum summed over the
    grid's own equal sectors, and the wave height is four roots of its variance -
    both arithmetic a reader can redo on the stated cell."""
    solved = _solved(monkeypatch, _spectra(
        varnames=[f"F00001PT2D{_NEAR_NODE:06d}"], values=[_density(4.0)]))
    read = _read(solved, spectrum())
    sector = 2.0 * math.pi / _DIRECTIONS
    assert read.values[_PEAK_FREQUENCY] == pytest.approx(4.0 * sector)
    assert read.values[0] == pytest.approx(0.0)
    variance = float(np.trapezoid(read.values, read.frequency_hz))
    assert read.measures["variance_m2"] == pytest.approx(variance)
    assert read.measures["hm0_m"] == pytest.approx(4.0 * math.sqrt(variance))
    peak_hz = _MINIMAL_HZ * _RATIO ** _PEAK_FREQUENCY
    assert read.measures["peak_frequency_hz"] == pytest.approx(peak_hz)
    assert read.measures["peak_period_s"] == pytest.approx(1.0 / peak_hz)
    assert read.measures["peak_direction_deg"] == pytest.approx(
        _PEAK_DIRECTION * 360.0 / _DIRECTIONS)
    assert read.measures["t"] == 600.0


def test_the_point_asked_about_is_answered_by_the_nearest_printout_point(monkeypatch):
    """A punctual file carries one variable per printout point, each named for
    the 2D node the engine snapped it onto, so a placed read picks the point
    that stands nearest the place the question was asked at."""
    from trid3nt_server.inputs import Point

    solved = _solved(monkeypatch, _spectra(
        varnames=[f"F00001PT2D{_NEAR_NODE:06d}", f"F00002PT2D{_FAR_NODE:06d}"],
        values=[_density(4.0), _density(16.0)]))
    lon, lat = solved.lonlat
    near = _read(solved, spectrum(at=Point(float(lon[_NEAR_NODE - 1]),
                                           float(lat[_NEAR_NODE - 1]), "inshore")))
    far = _read(solved, spectrum(at=Point(float(lon[_FAR_NODE - 1]),
                                          float(lat[_FAR_NODE - 1]), "offshore")))
    assert near.at == "at inshore"
    assert far.at == "at offshore"
    assert far.measures["hm0_m"] == pytest.approx(2.0 * near.measures["hm0_m"])


def test_a_deck_that_named_no_punctual_file_wrote_no_spectrum(monkeypatch):
    """The file is the DECK's to name: a run whose deck named none wrote none,
    and the read says so by the keyword's own name rather than guessing one."""
    solved = _solved(monkeypatch, None, named="")
    with pytest.raises(OutputEmpty) as refused:
        _read(solved, spectrum())
    assert "PUNCTUAL RESULTS FILE" in str(refused.value)


def test_a_file_whose_mesh_is_not_polar_is_refused_rather_than_reshaped(monkeypatch):
    """A variable written over a node count the frequency and direction rings do
    not multiply to is not a spectrum, and reading it as one would report an
    energy nobody computed."""
    spectra = _spectra(varnames=[f"F00001PT2D{_NEAR_NODE:06d}"],
                       values=[_density(4.0)])
    spectra["data"][f"F00001PT2D{_NEAR_NODE:06d}"] = np.zeros((2, 7))
    solved = _solved(monkeypatch, spectra)
    with pytest.raises(OutputEmpty) as refused:
        _read(solved, spectrum())
    assert "polar spectral grid" in str(refused.value)


def test_no_module_whose_result_is_a_mesh_reads_a_spectrum():
    """The read is registered on the wave module alone: a spectrum lives over a
    frequency-direction grid, and nothing a hydrodynamic result carries is one."""
    from trid3nt_server.workflows.telemac.modules import T2D, T3D

    assert "spectrum" in WAC.READS
    assert "spectrum" not in T2D.READS
    assert "spectrum" not in T3D.READS


def _record(payload: bytes) -> bytes:
    size = struct.pack(">i", len(payload))
    return size + payload + size


def _punctual_file(path) -> None:
    """SYNTHETIC - a punctual file written here, not one an engine wrote: the
    polar grid above as the QUADRANGLES the engine joins it into, one printout
    point, the stated cell of energy in its second frame."""
    x, y = _polar()
    cells = np.asarray([[ring * _DIRECTIONS + sector,
                         ring * _DIRECTIONS + (sector + 1) % _DIRECTIONS,
                         (ring + 1) * _DIRECTIONS + (sector + 1) % _DIRECTIONS,
                         (ring + 1) * _DIRECTIONS + sector]
                        for ring in range(_FREQUENCIES - 1)
                        for sector in range(_DIRECTIONS)])
    name = f"F01PT2D{_NEAR_NODE:06d}".encode().ljust(16) + b"UNITE SI".ljust(16)
    frames = b"".join(
        _record(struct.pack(">f", at)) + _record(frame.astype(">f4").tobytes())
        for at, frame in ((0.0, _density(0.0)), (600.0, _density(4.0))))
    path.write_bytes(
        _record(b"SYNTHETIC SPECTRUM".ljust(72) + b"SERAFIN ")
        + _record(struct.pack(">2i", 1, 0)) + _record(name)
        + _record(struct.pack(">10i", 1, *[0] * 9))
        + _record(struct.pack(">4i", len(cells), x.size, 4, 1))
        + _record((cells + 1).astype(">i4").tobytes())
        + _record(np.arange(1, x.size + 1, dtype=">i4").tobytes())
        + _record(x.astype(">f4").tobytes()) + _record(y.astype(">f4").tobytes())
        + frames)


def test_the_punctual_file_s_quadrangles_are_read_and_a_result_s_are_refused(
        monkeypatch, tmp_path):
    """The engine joins the polar grid into quadrangles, which no result mesh is
    made of: the spectrum read opens them, and the result read still refuses a
    file whose elements are not the triangles or prisms a mesh has."""
    from trid3nt_server.workflows.telemac.authoring.selafin_io import (
        SelafinReadError, read_selafin)

    written = tmp_path / "resWac.spe"
    _punctual_file(written)
    with pytest.raises(SelafinReadError):
        read_selafin(written)
    solved = _solved(monkeypatch, None)
    monkeypatch.setattr(
        "trid3nt_server.workflows.solver.solver.download_result",
        lambda run_id, basename, error_code=None: str(tmp_path / basename))
    monkeypatch.setattr(
        "trid3nt_server.workflows.telemac.modules.outputs.read_selafin",
        lambda path, **stated: (read_selafin(path, **stated)
                                if str(path).endswith(".spe") else _wave_field()))
    read = _read(solved, spectrum())
    assert read.density.shape == (_FREQUENCIES, _DIRECTIONS)
    assert read.density[_PEAK_FREQUENCY, _PEAK_DIRECTION] == pytest.approx(4.0)
    assert read.measures["t"] == 600.0
