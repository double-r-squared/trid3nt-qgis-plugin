import pytest

from trid3nt import case


def test_bare_telemac_has_no_assertions_and_does_not_pick_a_source(tmp_path):
    model = case(tmp_path / "bare").model("telemac2d")
    assert model._workflow.steering.ASSERTED == {}
    assert model["FRICTION COEFFICIENT"] == model._workflow.steering.slot("FRICTION_COEFFICIENT").engine_default
    form = model.fill()
    assert not form.ready
    assert "GEOMETRY FILE" in form.refusal[1]
    assert "BOUNDARY CONDITIONS FILE" in form.refusal[1]


def test_bare_telemac_exposes_the_selected_body(tmp_path):
    model = case(tmp_path / "bare").model(
        "telemac2d", coupling=[{"module": "waqtel", "process": 1}])
    model.waqtel.WATER_TEMPERATURE = 19.0
    assert model["waqtel: WATER TEMPERATURE"] == 19.0
    model.TITLE = "carrier"
    assert model["telemac2d: TITLE"] == "carrier"


@pytest.mark.parametrize(("name", "required"), [
    ("telemac2d", ("GEOMETRY FILE", "BOUNDARY CONDITIONS FILE", "RESULTS FILE")),
    ("telemac3d", ("GEOMETRY FILE", "BOUNDARY CONDITIONS FILE", "3D RESULT FILE")),
    ("artemis", ("GEOMETRY FILE", "BOUNDARY CONDITIONS FILE", "RESULTS FILE")),
    ("tomawac", ("PERIOD FOR GRAPHIC PRINTOUTS", "2D RESULTS FILE")),
    ("mf6_gwf", ("DIS, DISV or DISU", "IC_STRT", "NPF_K", "TDIS_PERIODDATA", "IMS_OUTER_MAXIMUM")),
    ("geoclaw", ("CLAW_LOWER", "CLAW_UPPER", "CLAW_TFINAL", "TOPO_TOPOFILES")),
    ("pestpp", ("tool", "trial", "files", "parameters", "observations", "templates", "instructions")),
])
def test_each_bare_entry_refuses_the_unstated_inputs(name, required, tmp_path):
    form = case(tmp_path / "bare").model(name).fill()
    assert not form.ready and form.deck is None
    assert all(key in form.refusal[1] for key in required)


def test_geoclaw_stages_the_explicit_native_grid_and_topography(tmp_path):
    pytest.importorskip("clawpack.geoclaw")
    topo = tmp_path / "topo.tt3"
    topo.write_text("2 ncols\n2 nrows\n-1 xllcenter\n-1 yllcenter\n2 cellsize\n"
                    "-9999 nodata_value\n-1 -1\n-1 -1\n")
    c = case(tmp_path / "bare")
    model = c.model("geoclaw", files={"topo.tt3": str(topo)}, CLAW_LOWER=[-1., -1.],
                    CLAW_UPPER=[1., 1.], CLAW_NUM_CELLS=[4, 4], CLAW_TFINAL=3.,
                    CLAW_NUM_OUTPUT_TIMES=1, TOPO_TOPOFILES=[[3, "topo.tt3"]])
    form = model.fill()
    assert form.ready and form.checked, form
    assert {"claw.data", "topo.data", "topo.tt3"} <= form.deck.files.keys()


def test_pestpp_stages_only_an_installed_trial_command(tmp_path):
    c = case(tmp_path / "bare")
    files = {}
    for filename, content in {"mfsim.nam": "test input", "k.tpl": "ptf ~\n~ k ~",
                              "head.ins": "pif @\nl1 !h!"}.items():
        path = tmp_path / filename
        path.write_text(content)
        files[filename] = str(path)
    model = c.model(
        "pestpp", tool="glm", trial="modflow", files=files, templates={"k.tpl": "k.dat"},
        instructions={"head.ins": "heads.csv"},
        parameters=[{"PARNME": "k", "PARVAL1": 2.0, "PARLBND": 1.0, "PARUBND": 3.0,
                     "PARGP": "p", "PARTRANS": "none", "PARCHGLIM": "relative", "SCALE": 1.0, "OFFSET": 0.0}],
        observations=[{"OBSNME": "h", "OBSVAL": 10.0, "WEIGHT": 1.0, "OBGNME": "heads"}], NOPTMAX=1)
    form = model.fill()
    assert form.ready and form.checked, form
    pst = c.root / "cache" / "pestpp" / form.deck.run_id / "pestpp.pst"
    assert "* model command line\npython /opt/trid3nt/workers/pestpp/modflow_trial.py\n" in pst.read_text()
