"""TELEMAC-3D vertical-discretisation + honesty-floor unit tests (offline).

Pins the three worker-side halves of the "cold edge" fix:

  * the vertical grid is PLANNED against the thermocline (MESH TRANSFORMATION 4
    with MESH STRETCHING COEFFICIENTS, keywords verified against the baked
    telemac3d.dico INDEX 96 / TRANSF_COEF), the achieved near-surface layer
    thickness is a declared fidelity fact, and an unresolvable column REFUSES;
  * the initial condition is a TANH of thickness DELTA >= 2*dz, not a step;
  * a bathymetry-clamped (land) node is NaN in the emitted layer product.

No telemac binary, no network: deck text, plane arithmetic, array masking.
"""
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import telemac3d_build as T  # noqa: E402


# --------------------------------------------------------------------------- #
# (A) vertical resolution: the planned grid, the declared dz, the refusal.
# --------------------------------------------------------------------------- #
def test_sigma_planes_reproduce_the_condim_gotm_stretch():
    """The planner must be the SAME transform condim.f applies, or every declared
    dz is fiction. Recomputed here straight from the dico/condim formula."""
    nplan, dl, du = 13, 1.0, 2.5
    s = np.arange(nplan) / (nplan - 1)
    expect = (np.tanh((dl + du) * s - dl) + np.tanh(dl)) / (np.tanh(dl) + np.tanh(du))
    got = T.sigma_planes(nplan, dl, du)
    assert np.allclose(got, expect)
    assert got[0] == pytest.approx(0.0) and got[-1] == pytest.approx(1.0)
    # both coefficients off -> the uniform sigma of MESH TRANSFORMATION 1
    assert np.allclose(T.sigma_planes(nplan, 0.0, 0.0), s)


def test_a_deep_lake_switches_to_zoomed_sigma_and_declares_the_achieved_dz():
    """402 m under 13 uniform planes is a 33.5 m near-surface layer against an 8 m
    thermocline - a ONE-NODE epilimnion. The zoom must bring it to <= 4 m."""
    v = T.plan_vertical_grid(nplan=13, max_depth_m=402.0, thermocline_depth_m=8.0)
    assert v["mesh_transformation"] == 4
    assert v["vertical_dz_uniform_m"] == pytest.approx(33.5, abs=0.05)
    assert v["vertical_dz_surface_m"] <= 4.0 + 1e-6
    assert v["vertical_layer_growth_ratio"] <= T.VSTRETCH_MAX_GROWTH
    dl, du = v["mesh_stretching_coefficients"]
    assert dl == pytest.approx(T.VSTRETCH_BOTTOM_COEF) and 0.0 < du < T.VSTRETCH_SURFACE_MAX
    # the declared number is the one the transform actually produces
    z = T.sigma_planes(13, dl, du)
    assert (z[-1] - z[-2]) * 402.0 == pytest.approx(v["vertical_dz_surface_m"], abs=1e-3)
    # more than the single node the uniform grid gave the epilimnion
    assert v["vertical_planes_above_thermocline"] >= 2
    assert "near-surface layer" in v["vertical_resolution_label"]


def test_a_shallow_basin_keeps_uniform_sigma():
    """Zooming a column the uniform grid already resolves only starves the interior."""
    v = T.plan_vertical_grid(nplan=13, max_depth_m=20.0, thermocline_depth_m=8.0)
    assert v["mesh_transformation"] == 1
    assert v["mesh_stretching_coefficients"] is None
    assert v["vertical_dz_surface_m"] == pytest.approx(20.0 / 12.0, abs=1e-3)


@pytest.mark.parametrize("nplan,depth,dtherm", [
    (7, 402.0, 8.0),      # too few planes: the stretch needs a 3.5x layer growth
    (13, 402.0, 1.0),     # a metre-thin thermocline under 402 m of water
])
def test_an_unresolvable_thermocline_refuses_rather_than_solving(nplan, depth, dtherm):
    with pytest.raises(T.Telemac3dInputError) as ei:
        T.plan_vertical_grid(nplan=nplan, max_depth_m=depth, thermocline_depth_m=dtherm)
    assert ei.value.error_code == "TELEMAC3D_VERTICAL_UNRESOLVED"
    msg = str(ei.value)
    assert "Raise nplan to at least" in msg and f"{dtherm:g} m thermocline" in msg


def test_the_refusal_names_an_nplan_that_actually_works():
    with pytest.raises(T.Telemac3dInputError) as ei:
        T.plan_vertical_grid(nplan=7, max_depth_m=402.0, thermocline_depth_m=8.0)
    need = int(str(ei.value).split("Raise nplan to at least ")[1].split()[0])
    v = T.plan_vertical_grid(nplan=need, max_depth_m=402.0, thermocline_depth_m=8.0)
    assert v["vertical_dz_surface_m"] <= 4.0 + 1e-6


def test_the_deck_carries_the_dico_verified_stretching_keywords(tmp_path):
    v = T.plan_vertical_grid(nplan=13, max_depth_m=402.0, thermocline_depth_m=8.0)
    cas = tmp_path / "t3d.cas"
    T.write_cas(str(cas), "g.slf", "b.cli", "r3.slf", "r2.slf", "ic.f",
                title="X", nplan=13, dt=20.0, nit=10, graprd=1, denlaw=1,
                tracer_name="TEMPERATURE     ",
                transf=v["mesh_transformation"],
                transf_coef=v["mesh_stretching_coefficients"])
    text = cas.read_text()
    assert "MESH TRANSFORMATION : 4" in text
    dl, du = v["mesh_stretching_coefficients"]
    assert f"MESH STRETCHING COEFFICIENTS : {dl:.4f};{du:.4f}" in text


def test_a_uniform_deck_never_emits_the_stretching_keyword(tmp_path):
    cas = tmp_path / "t3d.cas"
    T.write_cas(str(cas), "g.slf", "b.cli", "r3.slf", "r2.slf", None,
                title="X", nplan=13, dt=20.0, nit=10, graprd=1, transf=1)
    text = cas.read_text()
    assert "MESH TRANSFORMATION : 1" in text
    assert "MESH STRETCHING COEFFICIENTS" not in text


def test_transformation_4_without_coefficients_is_a_typed_error(tmp_path):
    with pytest.raises(T.Telemac3dInputError) as ei:
        T.write_cas(str(tmp_path / "t3d.cas"), "g.slf", "b.cli", "r3.slf",
                    "r2.slf", None, title="X", nplan=13, dt=20.0, nit=10,
                    graprd=1, transf=4, transf_coef=None)
    assert ei.value.error_code == "TELEMAC3D_PARAMS_INVALID"


# --------------------------------------------------------------------------- #
# (B) initial condition: a tanh of declared thickness, not a step.
# --------------------------------------------------------------------------- #
def test_condi_thermocline_writes_a_tanh_not_a_step():
    src = T.condi_thermocline(8.0, 25.0, 15.0, 8.0)
    assert "TANH" in src
    assert "IF(DPTH" not in src                       # the step is gone
    assert "15.0000D0+(10.0000D0)*0.5D0*" in src
    assert "(1.D0-TANH((DPTH-8.0000D0)/8.0000D0))" in src
    # fixed-form Fortran: continuation in column 6, nothing past column 72
    for line in src.splitlines():
        assert len(line) <= 72, line


def test_condi_thermocline_parenthesises_an_inverted_column():
    """A cold surface over a warm bottom must not emit '25.0000D0+-10.0000D0'."""
    src = T.condi_thermocline(8.0, 15.0, 25.0, 8.0)
    assert "25.0000D0+(-10.0000D0)*0.5D0*" in src


def test_the_tanh_profile_recovers_both_reservoirs_and_the_declared_thickness():
    """The emitted arithmetic, evaluated: warm at the surface, cold at depth, the
    thermocline centred at DTHERM, and DELTA the declared transition thickness."""
    dtherm, twarm, tcold, delta = 8.0, 25.0, 15.0, 8.0

    def temp(d):
        return tcold + (twarm - tcold) * 0.5 * (1.0 - np.tanh((d - dtherm) / delta))

    assert temp(dtherm) == pytest.approx(0.5 * (twarm + tcold))       # centred
    assert temp(dtherm - 3 * delta) == pytest.approx(twarm, abs=0.03)  # epilimnion
    assert temp(dtherm + 3 * delta) == pytest.approx(tcold, abs=0.03)  # hypolimnion
    # monotone down the column (tanh saturates to exactly 1 in the deep water)
    assert np.all(np.diff(temp(np.linspace(0.0, 400.0, 400))) <= 0.0)
    assert np.all(np.diff(temp(np.linspace(0.0, 3 * dtherm, 60))) < 0.0)


def test_delta_is_twice_the_achieved_layer_so_the_grid_can_hold_it():
    for depth, nplan in ((402.0, 13), (200.0, 13), (20.0, 13), (402.0, 21)):
        v = T.plan_vertical_grid(nplan, depth, 8.0)
        assert v["thermocline_delta_m"] == pytest.approx(
            2.0 * v["vertical_dz_surface_m"], abs=1e-3)
        # ... and still thin enough to BE a thermocline, not a linear column
        assert v["thermocline_delta_m"] <= 8.0 + 1e-6


# --------------------------------------------------------------------------- #
# (C) honesty floor: a clamped land node is NoData in the product.
# --------------------------------------------------------------------------- #
def _stub_write_slf(monkeypatch):
    written = {}

    def fake(mesh, path, values=None, varname="BOTTOM          "):
        written[os.path.basename(path)] = np.asarray(values, dtype=np.float64).copy()

    monkeypatch.setattr(T, "write_slf", fake)
    return written


def test_clamped_land_nodes_are_nan_in_the_emitted_layers(monkeypatch, tmp_path):
    written = _stub_write_slf(monkeypatch)
    mesh = {"wet": np.array([True, True, False, True, False])}
    surf = np.array([21.0, 20.5, 25.0, 19.8, 25.0])
    bot = np.array([15.0, 15.1, 25.0, 15.2, 25.0])

    sname, bname, surf_m, bot_m = T._emit_layer_fields(
        mesh, surf, bot, str(tmp_path), "TEMPERATURE     ")

    assert (sname, bname) == ("t3d_surface.slf", "t3d_bottom.slf")
    dry = np.array([False, False, True, False, True])
    for arr in (surf_m, bot_m, written["t3d_surface.slf"], written["t3d_bottom.slf"]):
        assert np.all(np.isnan(arr[dry]))
        assert np.all(np.isfinite(arr[~dry]))
    # the returned arrays are what the scalars are computed from, so the clamped
    # 25 C land can never inflate the reported surface mean
    assert float(np.nanmean(surf_m)) == pytest.approx(np.mean([21.0, 20.5, 19.8]))
    # the SOLVER still saw a value there - only the PRODUCT is masked
    assert surf[2] == 25.0


def test_an_idealized_mesh_without_a_mask_emits_every_node(monkeypatch, tmp_path):
    written = _stub_write_slf(monkeypatch)
    mesh = {}
    vals = np.array([1.0, 2.0, 3.0])
    _, _, surf_m, _ = T._emit_layer_fields(mesh, vals, vals, str(tmp_path), "U")
    assert np.all(np.isfinite(surf_m))
    assert np.all(np.isfinite(written["t3d_surface.slf"]))


def test_the_profile_column_snaps_off_a_clamped_centre_node():
    """A land centre node would make the whole profile chart a statement about
    5 m of invented water."""
    nx = ny = 5
    mesh = {"nx": nx, "ny": ny,
            "wet": np.ones(nx * ny, dtype=bool)}
    centre = (nx // 2) * ny + (ny // 2)
    mesh["wet"][centre] = False
    field = np.tile(np.arange(nx * ny, dtype=float), (3, 1))    # nplan=3
    _, col = T._vertical_profile(field, mesh, 3)
    assert col[0] != float(centre)
    assert mesh["wet"][int(col[0])]
