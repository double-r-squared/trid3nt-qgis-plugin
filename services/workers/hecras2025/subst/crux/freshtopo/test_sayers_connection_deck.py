"""Offline gates for the Bald Eagle Sayers Dam SA/2D-connection deck (ADR 0174).

Host-side, no solver: the assembled deck's plan HDF must be Results-typed, must
KEEP the ``Type="Connection"`` Sayers Dam structure (unlike the pure-2D fresh
decks, which strip Structures), must carry the 2D-BC Event-Conditions forcing on
g09's real BC lines, and must stage the matched x09 + an impounding b09. The
end-to-end SOLVE with NONZERO connection weir flow (peak ~300k cfs, vol err
0.0006%) is exercised by ``build_sayers_connection_deck.py`` +
``solve_sayers_connection.py`` inside ``trid3nt-local/hecras:latest`` (ADR 0174)."""
from __future__ import annotations

import pytest

import build_sayers_connection_deck as bd


def test_x09_weir_coef_patch_hits_both_fields_fixed_width():
    src = (bd._PURE2D / bd.XNN).read_text()
    out = bd.patch_x09_weir_coef(src, 4.0)
    lines = out.splitlines()
    h = next(i for i, l in enumerate(lines) if l.startswith("Conn"))
    assert "Sayers Dam" in lines[h]
    # the two weir-coef fields (T <coef> ... ; <coef> ...) become 4.0; the
    # fixed-field width is preserved (the fields keep their column positions)
    assert lines[h + 1].split()[1] == "4.0"
    assert lines[h + 2].split()[0] == "4.0"
    assert lines[h + 1] == src.splitlines()[h + 1].replace("3.1", "4.0")
    # the builder guards the occurrence count internally (raises if != 2)
    assert bd.patch_x09_weir_coef(src, 2.6).splitlines()[h + 1].split()[1] == "2.6"
    # a non-3-char coef is rejected (fixed-field discipline)
    with pytest.raises(ValueError):
        bd.patch_x09_weir_coef(src, 12.5)


@pytest.mark.skipif(not bd.G09.is_file(), reason="g09.hdf fixture not seeded")
def test_connection_deck_builds_results_typed_with_live_connection(tmp_path):
    import h5py

    info = bd.build(tmp_path, initial_stage=685.0, inflow_cfs=8000.0,
                    window_h=3.0, weir_coef=None)
    assert info["structure_types"] == ["Connection"]
    assert info["flow_areas"] == ["BaldEagleCr"]
    assert info["ec_faces"] == 9  # Upstream Inflow BC line perimeter faces

    plan = tmp_path / bd.PLAN
    assert plan.is_file() and (tmp_path / bd.XNN).is_file() and (tmp_path / bd.BNN).is_file()
    with h5py.File(plan, "r") as f:
        assert f.attrs["File Type"] == b"HEC-RAS Results"
        # the Sayers Dam connection survives the transplant (Structures NOT stripped)
        st = f["Geometry/Structures/Attributes"][()]
        assert st.shape[0] == 1
        row = st[0]
        assert row["Type"].decode().strip() == "Connection"
        assert row["Connection"].decode().strip() == "Sayers Dam"
        assert int(row["Use 2D for Overflow"]) == 1
        # the 2D-BC-line forcing that wets the mesh (read by read_un_q2d_bc_)
        fh = f["Event Conditions/Unsteady/Boundary Conditions/Flow Hydrographs"]
        assert "2D: BaldEagleCr BCLine: Upstream Inflow" in fh
        nd = f["Event Conditions/Unsteady/Boundary Conditions/Normal Depths"]
        assert "2D: BaldEagleCr BCLine: DSNormalDepth" in nd
        assert "2D: BaldEagleCr BCLine: DS2NormalD" in nd
        # no stale 1D White-River EC leaked from the Muncie template
        assert not any(k.startswith("River:") for k in fh)

    # b09 impounds the reservoir above the 683 ft crest, x09 keeps the connection
    b09 = (tmp_path / bd.BNN).read_text()
    assert "     685" in b09  # initial-stage seed (fixed-field)
    x09 = (tmp_path / bd.XNN).read_text()
    assert "Section - Storage Area Connection Data" in x09 and "Sayers Dam" in x09


@pytest.mark.skipif(not bd.G09.is_file(), reason="g09.hdf fixture not seeded")
def test_connection_deck_weir_coef_stamps_hdf_and_x09(tmp_path):
    import h5py

    bd.build(tmp_path, weir_coef=2.0, window_h=2.0)
    with h5py.File(tmp_path / bd.PLAN, "r") as f:
        assert float(f["Geometry/Structures/Attributes"][0]["Weir Coef"]) == pytest.approx(2.0)
    conn = (tmp_path / bd.XNN).read_text().split(
        "Section - Storage Area Connection Data")[1].split("Section - ")[0]
    assert conn.count("2.0") == 2 and "3.1" not in conn
