"""Offline tests for the ARTEMIS supplied-mesh path, both sides of the manifest.

AGENT SIDE: what the deck does when the template's ``mesh`` slot is filled - which
files it stages, what it stops asking the worker for, and what the layer then says
the solve read. The refusals matter as much as the happy path: a run told to solve
on a mesh and quietly handed a grid answers a different question than the one asked.

WORKER SIDE: the staged pair, read back into the dict the rest of the worker's
mesh code uses. It is imported BY PATH because the worker is a payload rather than
a package, and only its numpy-only half runs here - the SELAFIN reader lives in
the engine image.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.workflows.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.telemac.steps import agitation as AG
from trid3nt_server.workflows.telemac.steps.open_water import OpenWaterError

_REPO = Path(__file__).resolve().parents[1]

#: Point Judith Harbor of Refuge - the AOI the flagship authors its mesh over.
_AOI = {"lon": -71.5085, "lat": 41.353, "slug": "aoi", "name": "aoi",
        "bbox": (-71.525, 41.338, -71.492, 41.368)}


def _worker_module():
    """``workers/telemac/_supplied_mesh.py``, imported by path."""
    path = _REPO / "workers" / "telemac" / "_supplied_mesh.py"
    spec = importlib.util.spec_from_file_location("_supplied_mesh_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_supplied_mesh_under_test", module)
    spec.loader.exec_module(module)
    return module


def _artifact(**overrides):
    facts = {
        "mesh_id": "01TESTMESH", "name": "Point Judith Harbor of Refuge",
        "mode": "om2d",
        "display_uri": "s3://cache/mesh/01TESTMESH/mesh.2dm",
        "slf_uri": "s3://cache/mesh/01TESTMESH/mesh.slf",
        "cli_uri": "s3://cache/mesh/01TESTMESH/mesh.cli",
        "crs_authid": "EPSG:32619", "has_bathymetry": True,
        "node_count": 13110, "element_count": 25424,
        "bbox": (-71.525, 41.338, -71.492, 41.368),
        "engine_compat": ["telemac"],
        "probes": {"edge_length_m": {"min": 4.2, "max": 202.1, "mean": 28.1}},
        "provenance": {"mesher": "om2d",
                       "dem_source": "fetch_topobathy: cudem_nearshore 100%"},
    }
    facts.update(overrides)
    return MeshArtifact(**facts)


def _resolves_to(monkeypatch, artifact):
    monkeypatch.setattr(
        "trid3nt_server.workflows.mesh.tool.supplied_mesh_artifact",
        lambda explicit, *, engine, compatible=None: artifact)


def _deck(**kwargs):
    return asyncio.run(AG.write_agitation_deck(
        aoi=_AOI, wave_mode="diffraction", wave_period_s=12.0,
        wave_direction_deg=90.0, wave_height_m=2.0, reflection_coef=0.5,
        bathy_source="noaa_greatlakes", bed={"uri": "s3://cache/bed.tif"},
        **kwargs))


# --------------------------------------------------------------------------- #
# Resolving what was supplied.
# --------------------------------------------------------------------------- #
def test_an_unfilled_slot_is_not_a_mesh():
    assert AG.resolve_supplied_mesh(None, real=True) is None
    assert AG.resolve_supplied_mesh("  ", real=True) is None


def test_an_analytic_domain_refuses_a_supplied_mesh():
    with pytest.raises(OpenWaterError) as excinfo:
        AG.resolve_supplied_mesh("s3://cache/mesh/01TESTMESH/mesh.2dm", real=False)
    assert excinfo.value.error_code == "ARTEMIS_SUPPLIED_MESH_UNSUPPORTED_MODE"


def test_a_mesh_with_no_boundary_file_refuses_by_naming_what_is_missing(monkeypatch):
    _resolves_to(monkeypatch, _artifact(cli_uri=None))
    with pytest.raises(OpenWaterError) as excinfo:
        AG.resolve_supplied_mesh("s3://cache/mesh/01TESTMESH/mesh.2dm", real=True)
    assert excinfo.value.error_code == "ARTEMIS_SUPPLIED_MESH_CLOSED"
    assert "seaward boundary" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# The deck a supplied mesh writes.
# --------------------------------------------------------------------------- #
def test_the_deck_stages_the_pair_and_stops_asking_for_a_grid(monkeypatch):
    _resolves_to(monkeypatch, _artifact())
    deck = _deck(supplied_mesh="s3://cache/mesh/01TESTMESH/mesh.2dm")

    assert deck["config"]["supplied_mesh_slf"] == "supplied_mesh.slf"
    assert deck["config"]["supplied_mesh_cli"] == "supplied_mesh.cli"
    assert "target_resolution_m" not in deck["config"]
    assert deck["config"]["bathy_source"] == "supplied_mesh"
    assert deck["inputs"] == [
        {"gs_uri": "s3://cache/mesh/01TESTMESH/mesh.slf",
         "dest": "supplied_mesh.slf"},
        {"gs_uri": "s3://cache/mesh/01TESTMESH/mesh.cli",
         "dest": "supplied_mesh.cli"}]
    # The bed raster is not staged: the worker reads the bed off the mesh, and an
    # input nothing opens is not staged at all.
    assert all("bed" not in row["dest"] for row in deck["inputs"])
    assert deck["mesh_size_m"] == pytest.approx(28.1)


def test_the_bed_the_layer_names_is_the_one_the_mesh_carries(monkeypatch):
    _resolves_to(monkeypatch, _artifact())
    deck = _deck(supplied_mesh="s3://cache/mesh/01TESTMESH/mesh.2dm")
    assert "cudem_nearshore" in deck["bathy_label"]
    assert "Great Lakes" not in deck["bathy_label"]


def test_an_unfilled_slot_leaves_the_grid_deck_exactly_as_it_was(monkeypatch):
    deck = _deck(supplied_mesh=None, mesh_resolution_m=40.0)
    assert deck["config"]["target_resolution_m"] == 40.0
    assert "supplied_mesh_slf" not in deck["config"]
    assert deck["supplied_mesh"] is None
    assert deck["inputs"] == [{"gs_uri": "s3://cache/bed.tif",
                               "dest": "bed_source.tif"}]


def test_the_mesh_row_reports_the_solves_own_echo_not_the_ask(monkeypatch):
    _resolves_to(monkeypatch, _artifact())
    deck = _deck(supplied_mesh="s3://cache/mesh/01TESTMESH/mesh.2dm")
    rows = AG._provenance(deck, {
        "mesh_source": "supplied", "npoin": 13110, "nelem": 25424,
        "mesh_edge_min_m": 4.2, "mesh_edge_median_m": 29.5,
        "mesh_edge_max_m": 202.1, "mesh_boundary_nodes": 796,
        "mesh_open_boundary_nodes": 126, "mesh_structure_face_nodes": 547,
        "bed_clamped_nodes": 45, "bed_clamp_depth_m": 1.0,
        "structure_present": True, "bw_label": "REAL surveyed breakwater"})
    mesh_row = [r for r in rows if r.param == "mesh_domain"][0]
    assert "13110 nodes" in str(mesh_row.value)
    assert "126 of 796 boundary nodes designated liquid" in mesh_row.note
    assert "45 node(s)" in mesh_row.note


def test_a_solve_that_echoes_no_mesh_source_says_it_is_unmeasured(monkeypatch):
    _resolves_to(monkeypatch, _artifact())
    deck = _deck(supplied_mesh="s3://cache/mesh/01TESTMESH/mesh.2dm")
    rows = AG._provenance(deck, {"structure_present": True, "bw_label": "x"})
    mesh_row = [r for r in rows if r.param == "mesh_domain"][0]
    assert "UNMEASURED" in mesh_row.note


# --------------------------------------------------------------------------- #
# The worker's half: the staged pair, read back.
# --------------------------------------------------------------------------- #
def _cli(rows: list[tuple[int, int]]) -> str:
    """``(lihbor, node1)`` rows in rank order, in the .cli's own column layout."""
    return "\n".join(
        f"{lih} 2 2  0.0 0.0 0.0 0.0  2  0.0 0.0 0.0 {node:>11d} {rank:>11d}"
        for rank, (lih, node) in enumerate(rows, start=1)) + "\n"


def test_the_boundary_file_names_the_liquid_nodes_and_nothing_else(tmp_path):
    module = _worker_module()
    path = tmp_path / "supplied_mesh.cli"
    path.write_text(_cli([(2, 1), (5, 2), (5, 3), (2, 4)]))
    ring, ipob, liquid = module._read_boundary(str(path), npoin=6)
    assert ring.tolist() == [0, 1, 2, 3]
    assert ipob.tolist() == [1, 2, 3, 4, 0, 0]
    # Nodes 4 and 5 are INTERIOR: absent from the file is not the same as liquid.
    assert liquid.tolist() == [False, True, True, False, False, False]


def test_a_boundary_file_whose_ranks_are_not_a_permutation_refuses(tmp_path):
    module = _worker_module()
    path = tmp_path / "supplied_mesh.cli"
    path.write_text(_cli([(2, 1), (2, 2)]).replace("          1\n", "          7\n"))
    with pytest.raises(module.SuppliedMeshUnusableError) as excinfo:
        module._read_boundary(str(path), npoin=4)
    assert "permutation" in str(excinfo.value)


def test_a_boundary_file_naming_one_node_twice_refuses(tmp_path):
    module = _worker_module()
    path = tmp_path / "supplied_mesh.cli"
    path.write_text(_cli([(2, 1), (2, 1)]))
    with pytest.raises(module.SuppliedMeshUnusableError) as excinfo:
        module._read_boundary(str(path), npoin=4)
    assert "twice" in str(excinfo.value)


def test_a_geometry_carrying_a_collapsed_element_refuses_before_the_solve(tmp_path):
    module = _worker_module()
    x = np.array([0.0, 100.0, 0.0, 200.0])
    y = np.array([0.0, 0.0, 100.0, 0.0])
    ikle = np.array([[0, 1, 2], [0, 1, 3]], dtype=np.int32)   # second is collinear
    with pytest.raises(module.SuppliedMeshUnusableError) as excinfo:
        module._refuse_degenerate(x, y, ikle, "supplied_mesh.slf")
    assert "no area to invert" in str(excinfo.value)
    assert "element 2" in str(excinfo.value)


def test_half_a_staged_pair_refuses_rather_than_meshing_something_else(tmp_path):
    module = _worker_module()
    (tmp_path / "supplied_mesh.slf").write_bytes(b"slf")
    with pytest.raises(module.SuppliedMeshUnusableError) as excinfo:
        module.staged_pair(str(tmp_path), "supplied_mesh.slf", "supplied_mesh.cli")
    assert "staged together or not at all" in str(excinfo.value)


def test_no_staging_is_not_an_error(tmp_path):
    module = _worker_module()
    assert module.staged_pair(str(tmp_path), None, None) is None
