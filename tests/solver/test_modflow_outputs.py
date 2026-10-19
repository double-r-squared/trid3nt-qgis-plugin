"""A solved MODFLOW run read: heads, saturation and discharge as cell-valued UGRID groups, a series at a Point, the listing's budget.

Offline over a synthetic verification fixture (tests/fixtures/modflow/lattice_solved): the lattice sheet solved once through
the image. Pinned: every read answers off the run's own head, budget and listing files, a dry or inactive cell is NaN, a
clock needs stated time units, and an output the sheet did not save refuses by the keyword that would have saved it.
"""

from __future__ import annotations

import io
import re
import shutil
from pathlib import Path

import netCDF4
import numpy as np
import pytest
from pyproj import Transformer

from trid3nt_server.inputs.observation import Observation
from trid3nt_server.render.formats import Mesh
from trid3nt_server.store import objects as storage
from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.modflow.modules.outputs import Solved
from trid3nt_server.workflows.runtime import primitives as P
from trid3nt_server.workflows.runtime.sheet import fill

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "modflow" / "lattice_solved"
_DAY = 86400.0
_TO_LONLAT = Transformer.from_crs(32617, 4326, always_xy=True).transform
_SAVED = {"OC_HEAD_FILERECORD": "gwf.hds", "OC_BUDGET_FILERECORD": "gwf.cbc"}


def _lattice(path: Path) -> Path:
    rows = ["MESH2D"]
    cell = 0
    for r in range(2):
        for c in range(2):
            a = r * 3 + c + 1
            for tri in ((a, a + 1, a + 4), (a, a + 4, a + 3)):
                cell += 1
                rows.append(f"E3T {cell} {tri[0]} {tri[1]} {tri[2]} 1")
    node = 0
    for y in (0.0, 10.0, 20.0):
        for x in (0.0, 10.0, 20.0):
            node += 1
            rows.append(f"ND {node} {x} {y} -2.0")
    path.write_text("\n".join(rows) + "\n")
    return path


def _sheet(**over):
    stated = {
        "DISV_NLAY": 2, "DISV_BOTM": [[-10.0] * 8, [-20.0] * 8],
        "DISV_LENGTH_UNITS": "meters", "NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0,
        "IC_STRT": 0.0, "NPF_SAVE_SATURATION": True,
        "NPF_SAVE_SPECIFIC_DISCHARGE": True, "TDIS_NPER": 2,
        "TDIS_TIME_UNITS": "days",
        "TDIS_PERIODDATA": [[5.0, 2, 1.0], [5.0, 1, 1.0]], **_SAVED}
    stated.update(over)
    return fill(GWF, **{k: v for k, v in stated.items() if v is not None})


@pytest.fixture
def solved(monkeypatch, tmp_path, fake_s3):
    def download(run_id, basename):
        dest = tmp_path / f"dl_{basename}"
        shutil.copy(_FIXTURE / basename, dest)
        return str(dest)

    monkeypatch.setattr("trid3nt_server.workflows.solver.solver.download_result", download)

    def build(**over) -> Solved:
        display = _lattice(tmp_path / "mesh.2dm")
        art = MeshArtifact(
            mesh_id="M01", name="lattice", mode="om2d", display_uri=str(display),
            crs_authid="EPSG:32617", has_bathymetry=True, node_count=9,
            element_count=8, bbox=(0.0, 0.0, 20.0, 20.0), utm_epsg=32617)
        run = {"run_id": "R1", "utm_epsg": 32617, "sheet": _sheet(**over),
               "reference_time": "2024-06-01T00:00:00Z", "vertical_frame": "NAVD88",
               "mesh": {"artifact": art, "display_uri": str(display)}}
        return Solved(run, GWF)

    return build


def _groups(fake_s3, read) -> netCDF4.Dataset:
    body = fake_s3.get_object(Bucket=storage.runs_bucket(), Key=f"R1/{read.file}")["Body"].read()
    return netCDF4.Dataset("inmemory.nc", memory=body)


def _heads():
    import flopy

    return flopy.utils.HeadFile(str(_FIXTURE / "gwf.hds"))


def test_every_saved_head_is_one_temporal_group_of_cell_values(solved, fake_s3):
    run = solved()
    read = GWF.READS["field"](P.field("HEAD", t="every", plane=1), run)

    heads = _heads()
    assert read.frames == len(heads.get_times()) == 3
    assert read.file == "modflow_head_layer_1.nc" and read.units == "meters"
    assert read.values.shape == (3, 8)
    ds = _groups(fake_s3, read)
    variable = ds.variables[read.group]
    assert variable.shape == (3, 8)
    # The time coordinate is the saved times in seconds from the run's reference time.
    assert ds.variables["time"].units == "seconds since 2024-06-01 00:00:00"
    assert ds.variables["time"][:].tolist() == [t * _DAY for t in heads.get_times()]
    expected = np.asarray(heads.get_data(totim=heads.get_times()[0]))[0, 0, :]
    assert variable[0, :7].tolist() == pytest.approx(expected[:7].tolist())


def test_an_inactive_cell_is_nan_never_a_number(solved, fake_s3):
    read = GWF.READS["field"](P.field("HEAD", t="every", plane=1), solved())
    assert np.isnan(read.values[:, 7]).all()
    assert np.isfinite(read.values[:, :7]).all()
    ds = _groups(fake_s3, read)
    assert np.isnan(np.ma.filled(ds.variables[read.group][:, 7], np.nan)).all()
    second = GWF.READS["field"](P.field("HEAD", t="every", plane=2), solved())
    assert np.isfinite(second.values).all()


def test_an_instant_and_the_maximum_are_each_their_own_one_step_file(solved, fake_s3):
    run = solved()
    every = GWF.READS["field"](P.field("HEAD", t="every", plane=1), run)
    last = GWF.READS["field"](P.field("HEAD", t=-1, plane=1), run)
    peak = GWF.READS["max_over_time"](P.max_over_time("HEAD", plane=1), run)

    assert last.frames is None and peak.frames is None
    assert last.group == "head_layer_1_at_t_864000_s"
    assert peak.group == "head_max_layer_1"
    held = np.ma.filled(_groups(fake_s3, last).variables[last.group][:, :], np.nan)
    assert held.shape == (1, 8) and np.isfinite(held[0, :7]).all()
    column = np.ma.filled(_groups(fake_s3, peak).variables[peak.group][0, :], np.nan)
    assert np.isnan(column[7])
    assert column[:7] == pytest.approx(np.nanmax(every.values[:, :7], axis=0))


def test_the_head_frames_deliver_as_a_mesh_naming_the_display_file_and_group(solved):
    run = solved()
    primitive = P.field("HEAD", t="every", plane=1).animate()
    read = GWF.READS["field"](primitive, run)
    delivered = P.deliver(primitive, read, run, caption="groundwater head", name="head", where="")
    assert isinstance(delivered.product, Mesh)
    assert delivered.product.file == "modflow_head_layer_1.nc"
    assert delivered.product.group == "head_layer_1"
    assert delivered.product.frames == 3 and delivered.product.epsg == 32617


def test_a_series_at_a_point_is_the_containing_cells_heads_in_seconds(solved):
    lon, lat = _TO_LONLAT(18.0, 12.0)
    read = GWF.READS["series"](P.series("HEAD", at=(lon, lat), plane=1), solved())
    heads = _heads()
    assert list(read.times) == [t * _DAY for t in heads.get_times()]
    expected = [np.asarray(heads.get_data(totim=t))[0, 0, 6] for t in heads.get_times()]
    assert list(read.values) == pytest.approx(expected)


def test_a_point_off_the_mesh_refuses_by_name(solved):
    lon, lat = _TO_LONLAT(100.0, 100.0)
    from trid3nt_server.inputs.point import PointNotViableError

    with pytest.raises(PointNotViableError) as caught:
        GWF.READS["series"](P.series("HEAD", at=(lon, lat), plane=1), solved())
    assert caught.value.error_code == "POINT_NOT_VIABLE"
    assert caught.value.hint["distance_m"] > 0


def test_the_budget_discrepancy_is_the_listings_own(solved):
    read = GWF.READS["mass_balance"](P.mass_balance(), solved())
    listing = (_FIXTURE / "gwf.lst").read_text()
    printed = [float(v) for v in re.findall(r"PERCENT DISCREPANCY =\s+(-?[\d.]+)", listing)]
    assert read.name == "budget"
    assert read.measures["percent_discrepancy"] == printed[-1] == pytest.approx(0.0, abs=1e-6)
    assert list(read.times) == [5.0 * _DAY, 10.0 * _DAY]
    assert {line.label for line in read.lines} >= {"WEL", "DRN", "GHB", "RCH", "CHD", "STO-SS"}
    assert read.measures["total_in"] == pytest.approx(read.measures["total_out"], rel=1e-3)


def test_saturation_and_discharge_read_off_the_budget_file(solved):
    saturation = GWF.READS["field"](P.field("SATURATION", t=-1, plane=1), solved())
    assert saturation.values[0, :7].tolist() == [1.0] * 7
    assert np.isnan(saturation.values[0, 7])
    discharge = GWF.READS["field"](P.field("SPECIFIC_DISCHARGE", t=-1, plane=2), solved())
    assert np.isfinite(discharge.values).all() and (discharge.values >= 0.0).all()


def test_a_layer_left_unstated_on_a_two_layer_run_refuses_naming_the_count(solved):
    with pytest.raises(ModflowError) as caught:
        GWF.READS["field"](P.field("HEAD", t="every"), solved())
    assert caught.value.error_code == "MODFLOW_LAYER_UNSTATED"
    assert "2 layers" in str(caught.value)


def test_unstated_time_units_refuse_a_clock_naming_the_keyword(solved):
    run = solved(TDIS_TIME_UNITS=None)
    with pytest.raises(ModflowError) as caught:
        GWF.READS["field"](P.field("HEAD", t="every", plane=1), run)
    assert caught.value.error_code == "MODFLOW_CLOCK_UNSTATED"
    assert "TDIS_TIME_UNITS" in str(caught.value)
    peak = GWF.READS["max_over_time"](P.max_over_time("HEAD", plane=1), run)
    assert peak.frames is None


def test_saturation_unsaved_refuses_naming_the_npf_keyword(solved):
    with pytest.raises(ModflowError) as caught:
        GWF.READS["field"](P.field("SATURATION", t=-1, plane=1),
                           solved(NPF_SAVE_SATURATION=None))
    assert caught.value.error_code == "MODFLOW_OUTPUT_UNSAVED"
    assert "NPF_SAVE_SATURATION" in str(caught.value)


def test_a_head_file_the_output_control_never_named_refuses(solved):
    with pytest.raises(ModflowError) as caught:
        GWF.READS["field"](P.field("HEAD", t=-1, plane=1), solved(OC_HEAD_FILERECORD=None))
    assert caught.value.error_code == "MODFLOW_OUTPUT_UNSAVED"
    assert "OC_HEAD_FILERECORD" in str(caught.value)


def test_head_units_are_the_sheets_length_units_or_empty(solved):
    read = GWF.READS["field"](P.field("HEAD", t=-1, plane=1), solved(DISV_LENGTH_UNITS=None))
    assert read.units == ""


def test_the_mesh_and_extent_reads_are_the_accepted_meshs(solved):
    run = solved()
    assert GWF.READS["mesh"](P.mesh(), run).measures == {
        "nodes": 9, "elements": 8, "layers": 2, "epsg": 32617}
    extent = GWF.READS["extent"](P.extent(), run).measures
    assert extent["area_km2"] == pytest.approx(400.0e-6, abs=1e-4)
    assert len(extent["bbox"]) == 4


def _well(value, *, quantity, units="ft", datum=None, sampled="2024-06-11T00:00:00Z",
          asked=None):
    lon, lat = _TO_LONLAT(18.0, 12.0)
    return Observation(value=value, units=units, site_id="USGS-1", lon=lon, lat=lat,
                       quantity=quantity, datum=datum, sampled=sampled, asked=asked)


def _head_at_the_well(step):
    heads = _heads()
    return float(np.asarray(heads.get_data(totim=heads.get_times()[step]))[0, 0, 6])


def test_a_depth_below_the_land_surface_is_checked_against_the_top_less_the_head(solved):
    read = GWF.READS["series"](P.series("HEAD", at=_well(10.0, quantity="water_table_depth"),
                                        plane=1), solved())
    # The lattice's land surface is its painted bed, -2 m; the reading was sampled ten days in, the last saved time.
    simulated = -2.0 - _head_at_the_well(-1)
    assert read.measures["observed"] == pytest.approx(3.048, abs=1e-3)
    assert read.measures["simulated"] == pytest.approx(simulated, abs=1e-3)
    assert read.measures["simulated_minus_observed"] == pytest.approx(simulated - 3.048, abs=2e-3)
    assert read.measures["quantity"] == "water_table_depth" and read.measures["units"] == "meters"


def test_an_elevation_on_the_run_s_frame_is_checked_against_the_head_itself(solved):
    well = _well(-3.0, quantity="groundwater_head", units="m", datum="NAVD88",
                 sampled="2024-06-03T12:00:00Z")
    read = GWF.READS["series"](P.series("HEAD", at=well, plane=1), solved())
    # Two and a half days in is the first saved time.
    assert read.measures["simulated"] == pytest.approx(_head_at_the_well(0), abs=1e-3)
    assert read.measures["observed"] == -3.0


def test_an_elevation_on_another_frame_refuses_naming_both(solved):
    well = _well(-3.0, quantity="groundwater_head", units="m", datum="NGVD29")
    with pytest.raises(ModflowError) as caught:
        GWF.READS["series"](P.series("HEAD", at=well, plane=1), solved())
    assert caught.value.error_code == "MODFLOW_OBSERVATION_UNCOMPARABLE"
    assert "NGVD29" in str(caught.value) and "NAVD88" in str(caught.value)


def test_a_reading_of_another_quantity_refuses_naming_it(solved):
    well = _well(4.0, quantity="depth_below_measuring_point")
    with pytest.raises(ModflowError) as caught:
        GWF.READS["series"](P.series("HEAD", at=well, plane=1), solved())
    assert caught.value.error_code == "MODFLOW_OBSERVATION_UNCOMPARABLE"
    assert "depth_below_measuring_point" in str(caught.value)


def test_a_reading_with_no_instant_on_a_run_saving_several_times_refuses(solved):
    with pytest.raises(ModflowError) as caught:
        GWF.READS["series"](P.series("HEAD", at=_well(10.0, quantity="water_table_depth",
                                                      sampled=None), plane=1), solved())
    assert caught.value.error_code == "MODFLOW_OBSERVATION_UNCOMPARABLE"


def test_a_stated_number_stands_at_no_cell_to_check_against(solved):
    with pytest.raises(P.OutputEmpty) as caught:
        GWF.READS["series"](P.series("HEAD", at=Observation(value=3.0, units="m"),
                                     plane=1), solved())
    assert "no site" in str(caught.value)


def test_a_site_off_the_grid_reads_nothing_and_says_how_far(solved):
    lon, lat = _TO_LONLAT(100.0, 100.0)
    site = Observation(value=3.0, units="m", site_id="FAR-OUT", lon=lon, lat=lat)
    with pytest.raises(P.OutputEmpty) as caught:
        GWF.READS["series"](P.series("HEAD", at=site, plane=1), solved())
    assert "FAR-OUT" in str(caught.value)
    assert "outside the mesh" in str(caught.value)


def test_a_reading_asked_at_an_instant_is_compared_at_the_saved_time_its_window_holds(solved):
    well = _well(10.0, quantity="water_table_depth", sampled="2024-06-05T00:00:00Z",
                 asked="2024-06-06T00:00:00+00:00")
    read = GWF.READS["series"](P.series("HEAD", at=well, plane=1), solved())
    # The window closing a day past the asked instant holds days 2.5 and 5; day 5 is nearest the sample.
    assert read.measures["compared_at"] == "2024-06-06T00:00:00+00:00"
    assert read.measures["simulated"] == pytest.approx(-2.0 - _head_at_the_well(1), abs=1e-3)


def test_an_instant_the_run_saved_no_time_near_reads_the_nearest_and_says_so(solved, monkeypatch):
    from trid3nt_server.inputs import run_notes

    said: list[str] = []
    monkeypatch.setattr(run_notes, "journal_note", said.append)
    well = _well(10.0, quantity="water_table_depth", sampled="2024-07-20T00:00:00Z",
                 asked="2024-08-01T00:00:00+00:00")
    read = GWF.READS["series"](P.series("HEAD", at=well, plane=1), solved())
    assert read.measures["compared_at"].startswith("2024-06-11")
    assert any("2024-08-01" in line and "2024-06-11" in line for line in said)
