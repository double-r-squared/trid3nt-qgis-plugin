"""The infiltration surface, as the TELEMAC-2D wrapper produces it at the fill.

Every numeric assertion is a HAND-COMPUTED fixture. The steep-slope assertions
double as a parity check against the exact formula in the installed engine's
``runoff_scs_cn.f``, whose own branch is compiled off.
"""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.workflows.mesh.shared.nodes import reproject_nodes_to_utm
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.modules.telemac2d import Infiltration, _huang
from trid3nt_server.workflows.telemac.templates.rain_on_grid.declarations import (
    LANDCOVER_CN_MANNING,
    LANDCOVER_UNMAPPED,
)


def test_huang_steep_slope_matches_telemac_rational():
    # CN2=80, slope 0.5 m/m -> CN2 * (322.79 + 15.63*0.5) / (0.5 + 323.52)
    a = 0.5
    factor = (322.79 + 15.63 * a) / (a + 323.52)
    assert _huang(80.0, a) == pytest.approx(80.0 * factor, abs=1e-9)


def test_huang_no_correction_below_014():
    assert _huang(80.0, 0.10) == pytest.approx(80.0, abs=1e-12)


def test_huang_clamps_above_14_and_caps_at_100():
    a_hi = 3.0
    factor_14 = (322.79 + 15.63 * 1.4) / (1.4 + 323.52)
    assert _huang(80.0, a_hi) == pytest.approx(80.0 * factor_14, abs=1e-9)
    assert _huang(99.0, 1.0) <= 100.0


def test_landcover_table_forest_and_urban_and_the_unmapped_row():
    cn, n, label = LANDCOVER_CN_MANNING[42]  # Evergreen Forest
    assert (cn, n, label) == (80.0, 0.200, "forest")
    cn, n, label = LANDCOVER_CN_MANNING[24]  # Developed High Intensity
    assert (cn, n, label) == (89.0, 0.100, "urban")
    cn, n, label = LANDCOVER_CN_MANNING[11]  # Open Water
    assert (cn, n, label) == (100.0, 0.040, "river/open-water")
    assert LANDCOVER_UNMAPPED == (75.0, 0.050, "open-land")


def test_reproject_to_utm_coweeta():
    # Coweeta NC ~ (-83.4, 35.05) -> UTM 17N = EPSG 32617.
    pts = np.array([[-83.40, 35.05], [-83.41, 35.06], [-83.39, 35.04]])
    xy, epsg = reproject_nodes_to_utm(pts)
    assert epsg == 32617
    assert xy.shape == (3, 2)
    # eastings ~ a few hundred km, northings ~ 3.88 M m in zone 17N.
    assert 2e5 < xy[:, 0].mean() < 8e5
    assert 3.8e6 < xy[:, 1].mean() < 3.95e6


#: Three nodes on a bed that rises 5 m over 10 m eastward: a 0.5 m/m slope.
_POINTS = np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0]])
_CELLS = np.array([[0, 1, 2]])
_BED = np.array([0.0, 5.0, 0.0])
_LONLAT = np.array([[-83.40, 35.05], [-83.399, 35.05], [-83.40, 35.051]])


@pytest.fixture()
def surface(monkeypatch):
    """The composite's reads stood in for: the accepted mesh and the raster."""
    import trid3nt_server.workflows.mesh.shared.nodes as nodes_mod

    monkeypatch.setattr(nodes_mod, "read_accepted_mesh_nodes",
                        lambda _uri, utm_epsg=None: (_POINTS, _CELLS, _BED, _LONLAT))
    monkeypatch.setattr(nodes_mod, "sample_raster_at_nodes",
                        lambda _path, lonlat, interp="nearest":
                        np.array([41.0, 22.0, 7.0]))
    monkeypatch.setattr("trid3nt_server.tools.cache.read_object_bytes_s3",
                        lambda _uri: b"")
    from types import SimpleNamespace

    def _expand(**over):
        return T2D.COMPOSITES["infiltration"].expand(Infiltration(**{
            "mesh": {"artifact": SimpleNamespace(utm_epsg=32617),
                     "display_uri": "s3://m/mesh.2dm"},
            "landcover": {"uri": "s3://lc/nlcd.tif"},
            "table": LANDCOVER_CN_MANNING, "unmapped": LANDCOVER_UNMAPPED,
            "uniform_cn": None, "steep_slope_correction": False,
            "antecedent_moisture": "normal", "initial_abstraction": 1, **over}))

    return _expand


def _cn_column(files) -> list[float]:
    return [float(row.split()[2]) for row in files["rog_cn_map.dat"].splitlines()[1:]]


def _manning_per_node(files) -> list[float]:
    zones = dict(line.split() for line in files["rog_zones.dat"].splitlines())
    coef = {line.split()[0]: float(line.split()[2])
            for line in files["rog_friction.tbl"].splitlines() if line[:1].isdigit()}
    return [coef[zones[str(i)]] for i in range(1, len(zones) + 1)]


def test_the_surface_is_distributed_from_the_land_cover_and_the_unmapped_row(surface):
    """41 = deciduous forest -> CN 80 / n 0.20; 22 = developed low -> CN 89 /
    n 0.10; 7 is no NLCD class and takes the open-land row."""
    slots, files = surface()
    assert _cn_column(files) == [80.0, 89.0, 75.0]
    assert _manning_per_node(files) == [0.20, 0.10, 0.05]
    assert slots["LAW_OF_BOTTOM_FRICTION"] == 4
    assert slots["ANTECEDENT_MOISTURE_CONDITIONS"] == 2
    assert slots["OPTION_FOR_INITIAL_ABSTRACTION_RATIO"] == 1
    assert slots["FORMATTED_DATA_FILE_2"] == "rog_cn_map.dat"
    assert set(files) == {"rog_cn_map.dat", "rog_friction.tbl", "rog_zones.dat"}
    # the scatter IS the mesh, so the interpolation the engine performs is identity
    assert files["rog_cn_map.dat"].splitlines()[1] == "0.000 0.000 80.000"


def test_a_uniform_curve_number_overrides_the_field_and_keeps_the_roughness(surface):
    _slots, files = surface(uniform_cn=75.0)
    assert _cn_column(files) == [75.0, 75.0, 75.0]
    assert _manning_per_node(files) == [0.20, 0.10, 0.05]


def test_the_steep_slope_correction_reads_the_meshs_own_gradient(surface):
    """The slope is the bed's over the one element every node belongs to, so
    all three take the same factor."""
    _slots, files = surface(steep_slope_correction=True)
    assert _cn_column(files) == pytest.approx(
        [round(_huang(cn, 0.5), 3) for cn in (80.0, 89.0, 75.0)])


def test_the_antecedent_moisture_words_map_to_the_engines_three_conditions(surface):
    assert surface(antecedent_moisture="dry")[0]["ANTECEDENT_MOISTURE_CONDITIONS"] == 1
    assert surface(antecedent_moisture="II")[0]["ANTECEDENT_MOISTURE_CONDITIONS"] == 2
    assert surface(antecedent_moisture="wet")[0]["ANTECEDENT_MOISTURE_CONDITIONS"] == 3
    assert surface(antecedent_moisture=3)[0]["ANTECEDENT_MOISTURE_CONDITIONS"] == 3


def test_a_fourth_moisture_word_refuses_rather_than_seating_amc_ii(surface):
    with pytest.raises(ValueError, match="not an SCS condition"):
        surface(antecedent_moisture="saturated")


def test_the_surface_names_no_runoff_model_and_the_template_does(surface):
    """Four rainfall-runoff models read the same field: which one runs is the
    template's assertion, not the composite's."""
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        STEERING,
    )

    slots, _files = surface()
    assert "RAINFALL_RUNOFF_MODEL" not in slots
    assert STEERING.ASSERTED["RAINFALL_RUNOFF_MODEL"] == 1
