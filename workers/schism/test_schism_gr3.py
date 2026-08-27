"""Worker tests for the coastal-TIN -> hgrid.gr3 bridge (flat-import from the
worker dir, mirroring the hecras/mesh worker test convention). Synthetic meshes
only -- deterministic, light, no fixture files. The live SCHISM-ipre acceptance
of a real Galveston TIN grid is proven in (not re-run here)."""

from __future__ import annotations

import numpy as np

import schism_gr3 as G


def _square_mesh(nx: int = 4, ny: int = 4):
    """A regular triangulated unit square: (nx*ny) nodes, 2*(nx-1)*(ny-1) tris.
    Half the triangles are wound CW on purpose so orientation gets exercised."""
    xs, ys = np.meshgrid(np.linspace(0, 1, nx), np.linspace(0, 1, ny))
    pts = np.column_stack([xs.ravel(), ys.ravel()])
    cells = []
    for j in range(ny - 1):
        for i in range(nx - 1):
            a = j * nx + i
            b = a + 1
            c = a + nx
            d = c + 1
            cells.append([a, b, d])   # CCW
            cells.append([a, d, c])   # CCW
    return pts, np.asarray(cells, dtype=np.int64)


def test_signed_area_sign():
    pts = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    ccw = np.array([[0, 1, 2]])
    cw = np.array([[0, 2, 1]])
    assert G.signed_area_ccw(pts, ccw)[0] > 0
    assert G.signed_area_ccw(pts, cw)[0] < 0


def test_orientation_normalized_ccw():
    pts, cells = _square_mesh()
    # flip every triangle to CW; the converter must restore CCW
    cells_cw = cells[:, [0, 2, 1]]
    txt = G.tin_to_hgrid(pts, cells_cw, depth=5.0, clean_boundary=False)
    ne, nnp = (int(v) for v in txt.splitlines()[1].split()[:2])
    tris = np.array(
        [[int(v) - 1 for v in txt.splitlines()[2 + nnp + e].split()[2:5]] for e in range(ne)]
    )
    node_xy = np.array(
        [[float(v) for v in txt.splitlines()[2 + i].split()[1:3]] for i in range(nnp)]
    )
    assert (G.signed_area_ccw(node_xy, tris) > 0).all()


def test_boundary_loops_cover_all_boundary_nodes():
    pts, cells = _square_mesh(5, 5)
    # boundary nodes of a 5x5 grid = the 16 perimeter nodes
    loops = G.extract_boundary_loops(cells)
    covered = {n for L in loops for n in L}
    # brute-force boundary nodes
    ec: dict[tuple[int, int], int] = {}
    for t in cells:
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            k = (int(a), int(b)) if a < b else (int(b), int(a))
            ec[k] = ec.get(k, 0) + 1
    bnd = {x for (a, b), n in ec.items() if n == 1 for x in (a, b)}
    assert covered == bnd
    assert len(loops) == 1  # a simple square has one boundary loop


def test_pinch_point_removed():
    # two triangles sharing ONLY node 2 = a bowtie (node 2 has boundary degree 4)
    pts = np.array(
        [[0, 0], [1, 0], [1, 1], [2, 1], [2, 2], [1.0, 1.0]], dtype=float
    )
    # tri A uses node 2; tri B uses node 5 which coincides with node 2 -> make B
    # actually share node 2 to force the pinch:
    pts = np.array([[0, 0], [2, 0], [1, 1], [0, 2], [2, 2]], dtype=float)
    cells = np.array([[0, 1, 2], [2, 3, 4]], dtype=np.int64)  # touch at node 2
    deg = G._boundary_degree(cells)
    assert deg[2] == 4  # pinch
    cleaned = G.remove_boundary_pinch_points(pts, cells)
    deg2 = G._boundary_degree(cleaned)
    assert max(deg2.values()) == 2  # bowtie opened


def test_hgrid_structural_validity():
    pts, cells = _square_mesh(6, 6)
    txt = G.tin_to_hgrid(pts, cells, depth=12.5, grid_name="unit_sq")
    lines = txt.splitlines()
    assert lines[0] == "unit_sq"
    ne, nnp = (int(v) for v in lines[1].split()[:2])
    assert nnp == 36 and ne == 50
    # node lines: 1-indexed, id x y depth
    first = lines[2].split()
    assert int(first[0]) == 1 and float(first[3]) == 12.5
    # element lines: id 3 n1 n2 n3, all node refs in [1, nnp]
    for e in range(ne):
        parts = lines[2 + nnp + e].split()
        assert parts[1] == "3"
        refs = [int(x) for x in parts[2:5]]
        assert all(1 <= r <= nnp for r in refs)
    # boundary blocks present and parseable
    tail = "\n".join(lines[2 + nnp + ne:])
    assert "Number of open boundaries" in tail
    assert "Number of land boundaries" in tail


def test_per_node_depth_array():
    pts, cells = _square_mesh(4, 4)
    d = np.linspace(1.0, 16.0, pts.shape[0])
    txt = G.tin_to_hgrid(pts, cells, depth=d, clean_boundary=False)
    nnp = int(txt.splitlines()[1].split()[1])
    got = float(txt.splitlines()[2 + nnp - 1].split()[3])
    assert abs(got - 16.0) < 1e-6


def test_open_sections_write_one_block_each_and_split_the_land_boundary():
    """A mesher that identified contiguous ocean stretches hands them straight
    through: NOPE counts the stretches, and the land segments are the runs
    BETWEEN them rather than one list that jumps across."""
    xy = np.array([[x, y] for y in (0.0, 1.0, 2.0) for x in (0.0, 1.0, 2.0)])
    cells = []
    for row in range(2):
        for col in range(2):
            a = row * 3 + col
            cells += [[a, a + 1, a + 4], [a, a + 4, a + 3]]
    cells = np.asarray(cells, dtype=np.int64)

    txt = G.tin_to_hgrid(xy, cells, depth=5.0, open_sections=[[0, 1], [7, 8]],
                         clean_boundary=False)
    assert "2 = Number of open boundaries" in txt
    assert "4 = Total number of open boundary nodes" in txt
    assert "2 = Number of land boundaries" in txt


def test_contiguous_runs_walks_a_loop_as_a_cycle():
    assert G._contiguous_runs([0, 1, 2, 3, 4, 5], {1, 2}) == [[3, 4, 5, 0]]
    assert G._contiguous_runs([0, 1, 2, 3, 4, 5], {1, 4}) == [[2, 3], [5, 0]]
    assert G._contiguous_runs([0, 1, 2], set()) == [[0, 1, 2]]
