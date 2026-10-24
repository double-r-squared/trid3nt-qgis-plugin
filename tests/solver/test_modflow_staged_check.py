"""The staged run directory, checked clause by clause before it leaves the daemon.

Each clause gets a directory that violates it and nothing else, so a clause that
stopped reading its artifact is a red test rather than a solve that dies in its
first second inside the image.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trid3nt_server.workflows.modflow.authoring import staged_check as C
from trid3nt_server.workflows.modflow.errors import ModflowError

#: The directory a solve would take: name files, timing, a two-cell DISV, one
#: package with a period list. Every clause breaks exactly one piece of it.
_STEERING = "mfsim.nam"
_TDIS = "trid3nt.tdis"

_MFSIM = """BEGIN timing
  TDIS6  {tdis}
END timing

BEGIN models
  GWF6  gwf.nam  gwf
END models

BEGIN solutiongroup  1
  IMS6  gwf.ims  gwf
END solutiongroup  1
"""

_GWF_NAM = """DISV6  mesh.disv  disv
CHD6  gwf.chd  chd
NPF6
"""

_TDIS_FILE = """BEGIN options
  TIME_UNITS  seconds
END options

BEGIN dimensions
  NPER  1
END dimensions

BEGIN perioddata
  600.0  1  600.0
END perioddata
"""

_DISV = """BEGIN dimensions
  NLAY  2
  NCPL  2
END dimensions

BEGIN griddata
  top
    constant -2.0
  botm  LAYERED
    constant -10.0
    constant -20.0
END griddata

BEGIN cell2d
  1  5.0  5.0  4  1 2 4 3
  2  15.0 5.0  4  2 5 6 4
END cell2d
"""

_CHD = """BEGIN options
END options

BEGIN period 1
  1  1  -1.0
  1  2  -1.0
END period 1
"""


def _staged(tmp_path, **over) -> Path:
    (tmp_path / _STEERING).write_text(_MFSIM.format(tdis=_TDIS))
    (tmp_path / _TDIS).write_text(over.pop("tdis", _TDIS_FILE))
    (tmp_path / "gwf.nam").write_text(over.pop("gwf_nam", _GWF_NAM))
    (tmp_path / "gwf.ims").write_text("BEGIN options\nEND options\n")
    (tmp_path / "mesh.disv").write_text(over.pop("disv", _DISV))
    (tmp_path / "gwf.chd").write_text(over.pop("chd", _CHD))
    for name, text in over.items():
        (tmp_path / name).write_text(text)
    return tmp_path


def _check(rundir, *, duration_s=600.0, cores=1, inputs=(), written=()):
    return C.check_staged_run(
        rundir, steering=_STEERING, inputs=list(inputs),
        written_by_the_engine=list(written),
        duration_s=duration_s, cores=cores)


def _refusal(rundir, **kwargs):
    with pytest.raises(ModflowError) as caught:
        _check(rundir, **kwargs)
    return caught.value


def test_a_whole_staged_run_passes_every_clause(tmp_path):
    read = _check(_staged(tmp_path))

    assert read["cell_ids"] == 2 and read["tdis_total_s"] == 600.0
    assert read["name_files"] == ["gwf.nam", _STEERING]
    assert read["digested"] == []


def test_a_file_the_nam_references_and_nobody_staged_refuses_by_name(tmp_path):
    rundir = _staged(tmp_path)
    (rundir / "gwf.chd").unlink()

    refusal = _refusal(rundir)

    assert refusal.error_code == "MODFLOW_STAGED_FILE_MISSING"
    assert "gwf.chd" in str(refusal) and "references" in str(refusal)


def test_a_name_staged_in_another_spelling_is_named_as_that(tmp_path):
    rundir = _staged(tmp_path, gwf_nam=_GWF_NAM.replace("mesh.disv",
                                                        "MESH.DISV"))

    refusal = _refusal(rundir)

    assert refusal.error_code == "MODFLOW_STAGED_FILE_MISSING"
    assert "another spelling" in str(refusal) and "mesh.disv" in str(refusal)


def test_a_result_the_engine_writes_itself_is_not_asked_of_the_staging(tmp_path):
    rundir = _staged(tmp_path)
    (rundir / "gwf.ims").unlink()

    read = _check(rundir, written=["gwf.ims"])

    assert "gwf.ims" in read["referenced"]


def test_a_mesh_still_in_the_store_is_read_through_its_staged_name(
        tmp_path, monkeypatch):
    """A file the launcher stages later is checked as the object it is now."""
    rundir = _staged(tmp_path)
    store = tmp_path / "store"
    store.mkdir()
    (store / "mesh.disv").write_bytes((rundir / "mesh.disv").read_bytes())
    (rundir / "mesh.disv").unlink()
    monkeypatch.setattr(
        "trid3nt_server.tools.cache.read_object_bytes_s3",
        lambda uri: (store / uri.rsplit("/", 1)[-1]).read_bytes())

    read = _check(rundir, inputs=[{"gs_uri": "s3://bucket/mesh.disv",
                                   "dest": "mesh.disv"}])

    assert read["cell_ids"] == 2
    assert read["griddata_numbers"] >= 3


def test_bytes_that_are_not_what_the_run_was_handed_refuse(tmp_path):
    rundir = _staged(tmp_path)
    source = tmp_path / "source.disv"
    source.write_text(_DISV)
    (rundir / "mesh.disv").write_text(_DISV.replace("NCPL  2", "NCPL  3"))

    refusal = _refusal(
        rundir, inputs=[{"gs_uri": str(source), "dest": "mesh.disv"}])

    assert refusal.error_code == "MODFLOW_STAGED_DIGEST_MISMATCH"
    assert "mesh.disv" in str(refusal)


def test_a_dest_that_climbs_out_of_the_run_directory_refuses(tmp_path):
    rundir = _staged(tmp_path)
    outside = tmp_path / "outside.disv"
    outside.write_text(_DISV)

    refusal = _refusal(
        rundir, inputs=[{"gs_uri": str(outside), "dest": "../outside.disv"}])

    assert refusal.error_code == "MODFLOW_STAGED_DIGEST_MISMATCH"
    assert "climbs out" in str(refusal)


def test_a_cell_id_the_grid_does_not_number_refuses(tmp_path):
    rundir = _staged(tmp_path, chd=_CHD.replace("1  2  -1.0", "1  7  -1.0"))

    refusal = _refusal(rundir)

    assert refusal.error_code == "MODFLOW_STAGED_CELLID_OUTSIDE"
    assert "cell id 7" in str(refusal) and "cell2d 1 to 2" in str(refusal)


def test_a_layer_the_grid_does_not_have_refuses(tmp_path):
    rundir = _staged(tmp_path, chd=_CHD.replace("1  2  -1.0", "3  2  -1.0"))

    refusal = _refusal(rundir)

    assert refusal.error_code == "MODFLOW_STAGED_CELLID_OUTSIDE"
    assert "layer 3" in str(refusal) and "2 layer(s)" in str(refusal)


def test_a_cell_is_its_layer_and_its_cell2d_id_and_a_feature_row_is_not_a_cell(tmp_path):
    # Layer 2 of cell 1 on a grid of two cells; the SFR row leads with reach 9.
    sfr = "BEGIN period 1\n  9  INFLOW  1.0\n  12  2  STATUS  ACTIVE\nEND period 1\n"
    rundir = _staged(tmp_path, chd=_CHD.replace("1  2  -1.0", "2  1  -1.0"),
                     **{"gwf.sfr": sfr})

    assert _check(rundir)["cell_ids"] == 2


def test_a_disv_with_no_cell_table_refuses_rather_than_counting_nothing(tmp_path):
    rundir = _staged(tmp_path, disv=_DISV.split("BEGIN cell2d")[0])

    refusal = _refusal(rundir)

    assert refusal.error_code == "MODFLOW_STAGED_GRID_UNREADABLE"
    assert "cell2d" in str(refusal)


def test_a_non_finite_number_in_grid_data_refuses(tmp_path):
    rundir = _staged(tmp_path, disv=_DISV.replace("constant -2.0", "constant nan"))

    refusal = _refusal(rundir)

    assert refusal.error_code == "MODFLOW_STAGED_ARRAY_NOT_FINITE"
    assert "nan" in str(refusal)


def test_a_clock_that_ends_before_the_run_does_refuses(tmp_path):
    refusal = _refusal(_staged(tmp_path), duration_s=300.0)

    assert refusal.error_code == "MODFLOW_STAGED_CLOCK_INCONSISTENT"
    assert "600" in str(refusal) and "300" in str(refusal)


def test_a_time_unit_nothing_converts_with_leaves_the_clock_standing_down(tmp_path):
    rundir = _staged(
        tmp_path,
        tdis=_TDIS_FILE.replace("  TIME_UNITS  seconds\n", ""))

    read = _check(rundir, duration_s=12345.0)

    assert read["tdis_total_s"] is None


def test_a_fill_with_no_window_has_no_clock_to_disagree_with(tmp_path):
    read = _check(_staged(tmp_path), duration_s=None)

    assert read["tdis_total_s"] is None and read["cell_ids"] == 2


def test_a_nam_that_times_nothing_while_the_run_states_a_window_refuses(tmp_path):
    rundir = _staged(
        tmp_path, **{_STEERING: "BEGIN models\n  GWF6  gwf.nam  gwf\n"
                                "END models\n"})

    refusal = _refusal(rundir)

    assert refusal.error_code == "MODFLOW_STAGED_CLOCK_INCONSISTENT"
    assert "no TDIS" in str(refusal)


def test_the_dispatch_against_no_steering_file_refuses_by_name(tmp_path):
    (_staged(tmp_path) / _STEERING).unlink()

    refusal = _refusal(tmp_path)

    assert refusal.error_code == "MODFLOW_STAGED_FILE_MISSING"
    assert _STEERING in str(refusal)


def test_cores_are_accepted_without_a_clause(tmp_path):
    """mf6 is sequential: nothing in the files places a partition."""
    read = _check(_staged(tmp_path), cores=64)

    assert read["cores"] == 64


@pytest.mark.asyncio
async def test_a_run_staged_against_a_clause_refuses_before_the_manifest(
        tmp_path, monkeypatch):
    """The clauses read on the real path: the staging seam calls them itself."""
    from trid3nt_server.workflows.solver import staging
    from trid3nt_server.workflows.modflow.authoring.staged_check import (
        check_staged_run,
    )

    rundir = _staged(tmp_path, chd=_CHD.replace("1  2  -1.0", "1  7  -1.0"))
    written: list[str] = []
    monkeypatch.setattr(staging, "_clear_prefix", lambda *a: None)
    monkeypatch.setattr(staging, "_upload_authored", lambda *a, **k: [])
    monkeypatch.setattr(staging, "_write_manifest",
                        lambda *a, **k: written.append("manifest") or "s3://m")

    with pytest.raises(ModflowError) as caught:
        await staging.stage_run(
            rundir, "run-tag", module="gwf", steering=_STEERING,
            results=["heads.hds"], outputs=[], mesh_inputs=[], prefix="p",
            sheet={}, server_facts={"duration_s": 600.0},
            result_basename="heads", check=check_staged_run)

    assert caught.value.error_code == "MODFLOW_STAGED_CELLID_OUTSIDE"
    assert written == []
