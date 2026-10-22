"""What a solved GeoClaw run publishes: fgmax rasters, fgout frames and gauge series.

Offline, on an excerpt of Clawpack's chile2010_fgmax-fgout example's own output files (tests/fixtures/geoclaw): the
fgmax points and fgout cells over one window of the Chilean coast, every fgout frame, and the DART 32412 gauge table.
Pinned: each primitive's values equal what Clawpack's reader gives, the overland mask is the frame-0 wet area, a
wet cell is the run's GEO_DRY_TOLERANCE, and a keyword the run did not state or a file it did not write refuses by name.
The one fgmax file written here (MAX_OVERLAND_DEPTH) is SYNTHETIC - a plumbing check that puts the fgmax nodes on the
fgout nodes; none of it is a deliverable.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
import rasterio

from trid3nt_server.inputs import Point
from trid3nt_server.inputs.point import PointNotViableError
from trid3nt_server.store import objects as storage
from trid3nt_server.workflows.geoclaw.errors import GeoclawError
from trid3nt_server.workflows.geoclaw.modules import GEO
from trid3nt_server.workflows.geoclaw.modules.outputs import _READ_BY, Solved
from trid3nt_server.workflows.runtime.primitives import Primitive, field, max_over_time, series
from trid3nt_server.workflows.runtime.sheet import fill
from trid3nt_server.workflows.solver.solver import RunOutputMissing

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "geoclaw" / "chile2010_fgmax_fgout"
_RUN = "run-geoclaw-outputs"

_SHEET = {
    "GEO_COORDINATE_SYSTEM": 2, "GEO_DRY_TOLERANCE": 0.001, "FGMAX_NUM_FGMAX_VAL": 2,
    "FGMAX_FGMAX_GRIDS": [[1, 10.0, 1.0e10, 60.0, 3, 1.0e-2, 0, 2,
                           {"x1": -77.583333334, "y1": -44.916666667, "x2": -72.416666667, "y2": -41.416666667,
                            "nx": 32, "ny": 22}]],
    "FGOUT_FGOUT_GRIDS": [[1, 1, 25, 0.0, 21600.0, 2, "binary32", -77.65, -72.25, -44.92, -41.32, 24, 20]],
    "GAUGE_GAUGES": [[32412, -86.392, -17.975, 0.0, 1.0e10]],
}


class _Store:
    """The run prefix as a dict: what the reads fetch and the COGs they write."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_object(self, *, Bucket: str, Key: str):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str | None = None):
        self.objects[Key] = Body


@pytest.fixture()
def store(monkeypatch):
    held = _Store()
    for path in _FIXTURE.iterdir():
        held.objects[f"{_RUN}/{path.name}"] = path.read_bytes()
    monkeypatch.setattr(storage, "_CLIENT", held)
    return held


def _solved(**over) -> Solved:
    sheet = fill(GEO, **{k: v for k, v in {**_SHEET, **over}.items() if v is not None})
    return Solved({"run_id": _RUN, "sheet": sheet}, GEO)


def _cog(store: _Store, uri: str):
    """``(values, bounds, crs)`` of a COG the read wrote; the nodata cells are NaN."""
    with rasterio.MemoryFile(store.objects[uri.split("/", 3)[3]]) as memory, memory.open() as src:
        return src.read(1), tuple(src.bounds), src.crs.to_string()


def _read(solved: Solved, primitive: Primitive):
    return solved.body.READS[primitive.kind](primitive, solved)


def _fgmax_expected():
    """Clawpack's own fgmax read of the fixture, as raster rows (north first) with NaN where it masks."""
    from clawpack.geoclaw.fgmax_tools import FGmaxGrid

    grid = FGmaxGrid()
    grid.read_fgmax_grids_data(1, data_file=str(_FIXTURE / "fgmax_grids.data"))
    grid.read_output(fgno=1, outdir=str(_FIXTURE), verbose=False)
    rows = lambda a: np.flipud(np.ma.filled(a.astype("float64"), np.nan).T)  # noqa: E731
    return grid, rows


def _same(got, want, *, floor: float = 0.0) -> float:
    """The largest difference between the COG and the reader's values, the nodata cells agreeing exactly."""
    assert np.array_equal(np.isnan(got), np.isnan(want))
    return float(np.nanmax(np.abs(got.astype("float64") - want))) if np.isfinite(want).any() else floor


def test_every_fgmax_product_equals_the_values_clawpacks_reader_gives(store):
    grid, rows = _fgmax_expected()
    wet = np.isfinite(rows(grid.h)) & (rows(grid.h) > 0.001)
    wanted = {"MAX_DEPTH": np.where(wet, rows(grid.h), np.nan),
              "MAX_SPEED": np.where(wet, rows(grid.s), np.nan),
              "MAX_SURFACE": np.where(wet, rows(grid.h) + rows(grid.B), np.nan),
              "ARRIVAL_TIME": rows(grid.arrival_time)}
    solved = _solved()
    differences = {}
    for token, want in wanted.items():
        read = _read(solved, max_over_time(token))
        got, bounds, crs = _cog(store, read.uris[0])
        differences[token] = _same(got, want)
        assert crs == "EPSG:4326" and read.times == ()
        dx, dy = (grid.x[-1] - grid.x[0]) / 31, (grid.y[-1] - grid.y[0]) / 21
        assert np.allclose(bounds, (grid.x[0] - dx / 2, grid.y[0] - dy / 2, grid.x[-1] + dx / 2, grid.y[-1] + dy / 2))
        assert read.units == GEO.MODULE_OUTPUT[token].unit
    # float32 holds eight digits: the file's own text is eight digits, an arrival time up to 21600 s
    assert max(differences.values()) <= 2.0e-3
    assert np.isnan(wanted["ARRIVAL_TIME"]).any() and np.isfinite(wanted["ARRIVAL_TIME"]).any()


def test_a_cell_never_flagged_keeps_the_arrival_time_nodata(store):
    grid, rows = _fgmax_expected()
    got, _bounds, _crs = _cog(store, _read(_solved(), max_over_time("ARRIVAL_TIME")).uris[0])
    never = rows(grid.arrival_time)
    assert np.isnan(got[np.isnan(never)]).all() and np.isnan(never).sum() == 182


def _fgout_expected():
    from clawpack.geoclaw import fgout_tools

    grid = fgout_tools.FGoutGrid(1, str(_FIXTURE), "binary32")
    grid.read_fgout_grids_data()
    return grid, [grid.read_frame(n) for n in range(1, 26)]


def test_every_fgout_frame_equals_the_frame_clawpacks_reader_gives(store):
    grid, frames = _fgout_expected()
    for token, quantity in (("DEPTH", lambda f: f.h), ("SURFACE", lambda f: f.eta)):
        read = _read(_solved(), field(token, "every"))
        assert read.times == tuple(float(t) for t in grid.times) and len(read.uris) == 25
        worst = 0.0
        for uri, frame in zip(read.uris, frames):
            wet = frame.h > 0.001
            want = np.flipud(np.where(wet, quantity(frame).astype("float64"), np.nan).T)
            got, bounds, crs = _cog(store, uri)
            worst = max(worst, _same(got, want))
            assert crs == "EPSG:4326"
            assert np.allclose(bounds, (grid.extent_edges[0], grid.extent_edges[2], grid.extent_edges[1],
                                        grid.extent_edges[3]))
        assert worst == 0.0
        assert read.measures["frames"] == 25


def test_a_single_frame_is_read_by_index_or_by_the_nearest_instant(store):
    grid, frames = _fgout_expected()
    by_index = _read(_solved(), field("DEPTH", 3))
    by_instant = _read(_solved(), field("DEPTH", 3 * 900.0 + 120.0))
    last = _read(_solved(), field("DEPTH", -1))
    assert by_index.times == by_instant.times == (2700.0,) and last.times == (21600.0,)
    wet = frames[3].h > 0.001
    want = np.flipud(np.where(wet, frames[3].h.astype("float64"), np.nan).T)
    assert _same(_cog(store, by_index.uris[0])[0], want) == 0.0


def test_the_overland_depth_is_total_depth_where_frame_0_held_no_water(store):
    grid, frames = _fgout_expected()
    sea = frames[0].h > 0.001
    read = _read(_solved(), field("OVERLAND_DEPTH", "every"))
    flooded = 0
    for uri, frame in zip(read.uris, frames):
        want = np.flipud(np.where((frame.h > 0.001) & ~sea, frame.h.astype("float64"), np.nan).T)
        got, _bounds, _crs = _cog(store, uri)
        assert _same(got, want) == 0.0
        flooded += int(np.isfinite(want).sum())
    assert flooded > 0
    first, _bounds, _crs = _cog(store, read.uris[0])
    assert np.isnan(first).all()
    total, _bounds, _crs = _cog(store, _read(_solved(), field("DEPTH", -1)).uris[0])
    overland, _bounds, _crs = _cog(store, read.uris[-1])
    assert np.isfinite(total).sum() > np.isfinite(overland).sum() > 0


def test_a_first_frame_not_written_at_zero_holds_no_initial_sea(store):
    key = f"{_RUN}/fgout0001.t0001"
    store.objects[key] = store.objects[key].replace(b"0.00000000E+00", b"1.00000000E+02")
    with pytest.raises(GeoclawError) as caught:
        _read(_solved(), field("OVERLAND_DEPTH", -1))
    assert caught.value.error_code == "GEOCLAW_OVERLAND_UNANCHORED"


def test_the_wet_tolerance_is_the_runs_own_keyword(store):
    def cells(**over):
        read = _read(_solved(**over), field("DEPTH", -1))
        return int(np.isfinite(_cog(store, read.uris[0])[0]).sum())

    default = cells(GEO_DRY_TOLERANCE=None)
    assert default == cells(GEO_DRY_TOLERANCE=0.001)
    assert cells(GEO_DRY_TOLERANCE=1000.0) < default


def _synthetic_fgmax_on_the_fgout_nodes(store):
    """SYNTHETIC plumbing: an fgmax file whose points are the fgout cell centres, the depth the frames' maximum."""
    grid, frames = _fgout_expected()
    h = np.max([f.h for f in frames], axis=0)
    x, y = grid.x, grid.y
    rows = [[x[i], y[j], 3, frames[0].B[i, j], h[i, j], 0.0, 0.0, 0.0, 1.0] for j in range(grid.ny) for i in range(grid.nx)]
    buffer = io.StringIO()
    np.savetxt(buffer, np.array(rows), fmt="%.10e")
    store.objects[f"{_RUN}/fgmax0001.txt"] = buffer.getvalue().encode()
    text = (_FIXTURE / "fgmax_grids.data").read_text()
    text = text.replace("32  22 ", f"{grid.nx}  {grid.ny} ")
    text = text.replace("-7.7583333334e+01      -4.4916666667e+01", f"{x[0]:.10e}      {y[0]:.10e}")
    text = text.replace("-7.2416666667e+01      -4.1416666667e+01", f"{x[-1]:.10e}      {y[-1]:.10e}")
    store.objects[f"{_RUN}/fgmax_grids.data"] = text.encode()
    return grid, frames, h


def test_the_fgmax_overland_depth_drops_the_initial_sea_where_the_fgout_nodes_are_the_fgmax_nodes(store):
    grid, frames, h = _synthetic_fgmax_on_the_fgout_nodes(store)
    sheet = {"FGMAX_FGMAX_GRIDS": [[1, 10.0, 1.0e10, 60.0, 3, 1.0e-2, 0, 2,
                                    {"x1": grid.x[0], "y1": grid.y[0], "x2": grid.x[-1], "y2": grid.y[-1],
                                     "nx": grid.nx, "ny": grid.ny}]]}
    read = _read(_solved(**sheet), max_over_time("MAX_OVERLAND_DEPTH"))
    want = np.flipud(np.where((h > 0.001) & ~(frames[0].h > 0.001), h.astype("float64"), np.nan).T)
    got, _bounds, _crs = _cog(store, read.uris[0])
    assert 0 < np.isfinite(want).sum() < np.isfinite(np.where(h > 0.001, h, np.nan)).sum()
    assert _same(got, want) <= 1.0e-4


def test_the_fgmax_overland_depth_refuses_an_fgout_grid_off_its_nodes(store):
    with pytest.raises(GeoclawError) as caught:
        _read(_solved(), max_over_time("MAX_OVERLAND_DEPTH"))
    assert caught.value.error_code == "GEOCLAW_OVERLAND_GRIDS"


def test_a_gauge_series_equals_the_gauge_table_clawpack_reads(store):
    from clawpack.pyclaw.gauges import GaugeSolution

    want = GaugeSolution(32412, path=str(_FIXTURE))
    for at in (None, 32412, Point(-86.392, -17.975)):
        read = _read(_solved(), series("GAUGE_SURFACE", at))
        assert np.array_equal(read.values, want.q[3]) and np.array_equal(read.times, want.t)
        assert (read.lon, read.lat) == (-86.392, -17.975) and read.units == "m"
    assert read.measures["max"] == float(want.q[3].max()) == 0.1768705
    assert np.array_equal(_read(_solved(), series("GAUGE_DEPTH")).values, want.q[0])


def test_a_point_off_every_gauge_refuses_with_the_nearest_one(store):
    with pytest.raises(PointNotViableError) as caught:
        _read(_solved(), series("GAUGE_SURFACE", Point(-86.0, -18.0)))
    assert caught.value.hint["lon"] == -86.392


def test_a_gauge_the_run_does_not_hold_refuses_naming_the_ones_it_does(store):
    with pytest.raises(GeoclawError) as caught:
        _read(_solved(), series("GAUGE_SURFACE", 7))
    assert "32412" in str(caught.value)
    two = {"GAUGE_GAUGES": _SHEET["GAUGE_GAUGES"] + [[7, -80.0, -20.0, 0.0, 1.0e10]]}
    with pytest.raises(GeoclawError) as caught:
        _read(_solved(**two), series("GAUGE_SURFACE"))
    assert "32412, 7" in str(caught.value)


@pytest.mark.parametrize("token, kind, over, keyword", [
    ("MAX_DEPTH", max_over_time, {"FGMAX_FGMAX_GRIDS": None}, "FGMAX_FGMAX_GRIDS"),
    ("MAX_SPEED", max_over_time, {"FGMAX_NUM_FGMAX_VAL": None}, "FGMAX_NUM_FGMAX_VAL"),
    ("MAX_DEPTH", max_over_time, {"GEO_COORDINATE_SYSTEM": None}, "GEO_COORDINATE_SYSTEM"),
    ("MAX_OVERLAND_DEPTH", max_over_time, {"FGOUT_FGOUT_GRIDS": None}, "FGOUT_FGOUT_GRIDS"),
    ("DEPTH", field, {"FGOUT_FGOUT_GRIDS": None}, "FGOUT_FGOUT_GRIDS"),
    ("GAUGE_SURFACE", series, {"GAUGE_GAUGES": None}, "GAUGE_GAUGES"),
    ("GAUGE_SURFACE", series, {"GAUGE_FILE_FORMAT": "binary"}, "GAUGE_FILE_FORMAT"),
])
def test_an_output_whose_keyword_the_run_did_not_state_refuses_naming_it(store, token, kind, over, keyword):
    solved = _solved(**over)
    with pytest.raises(GeoclawError) as caught:
        _read(solved, kind(token, "every") if kind is field else kind(token))
    assert caught.value.error_code == "GEOCLAW_OUTPUT_UNSAVED" and keyword in str(caught.value)
    assert keyword in str(solved.unsaved(token))


@pytest.mark.parametrize("name, primitive", [
    ("fgmax0001.txt", max_over_time("MAX_DEPTH")),
    ("fgmax_grids.data", max_over_time("MAX_DEPTH")),
    ("fgout0001.b0007", field("DEPTH", "every")),
    ("fgout_grids.data", field("DEPTH", -1)),
    ("gauge32412.txt", series("GAUGE_SURFACE")),
])
def test_a_file_the_run_did_not_write_refuses_naming_it(store, name, primitive):
    del store.objects[f"{_RUN}/{name}"]
    with pytest.raises(RunOutputMissing) as caught:
        _read(_solved(), primitive)
    assert name in str(caught.value)


def test_a_name_the_module_does_not_write_or_reads_otherwise_refuses(store):
    for token, primitive, code in (("TEMPERATURE", max_over_time("TEMPERATURE"), "GEOCLAW_OUTPUT_UNKNOWN"),
                                   ("DEPTH", max_over_time("DEPTH"), "GEOCLAW_OUTPUT_READ"),
                                   ("DEPTH", field("DEPTH", None), "GEOCLAW_FIELD_TIME_UNSTATED"),
                                   ("DEPTH", field("DEPTH", 99), "GEOCLAW_FIELD_TIME_UNSTATED"),
                                   ("DEPTH", field("DEPTH", -1, plane=2), "GEOCLAW_PLANE_UNSTATED")):
        with pytest.raises(GeoclawError) as caught:
            _read(_solved(), primitive)
        assert caught.value.error_code == code


def test_a_run_stating_two_grids_has_no_one_grid_to_read(store):
    twice = {"FGOUT_FGOUT_GRIDS": _SHEET["FGOUT_FGOUT_GRIDS"] * 2}
    with pytest.raises(GeoclawError) as caught:
        _read(_solved(**twice), field("DEPTH", -1))
    assert caught.value.error_code == "GEOCLAW_OUTPUT_AMBIGUOUS"


def test_the_table_names_exactly_the_outputs_the_reads_know(store):
    assert set(GEO.MODULE_OUTPUT) == set(_READ_BY)
    assert {row.unit for row in GEO.MODULE_OUTPUT.values()} == {"m", "m/s", "s"}
    assert set(GEO.READS) == {"field", "max_over_time", "series"}

