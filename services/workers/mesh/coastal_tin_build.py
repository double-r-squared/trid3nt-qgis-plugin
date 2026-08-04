"""oceanmesh coastal-TIN pipeline (oceanmesh wave leg 2 worker payload).

Shoreline (+ optional DEM) -> graded coastal triangular mesh (TIN) via the
OceanMesh2D sizing functions, exactly as the OceanMesh2D User Guide + the
`oceanmesh` README prescribe: a signed-distance domain from the shoreline, one
or more composed edge-length (sizing) functions -- distance / feature-size /
wavelength / slope (bathymetric-gradient) -- graded with `enforce_mesh_gradation`,
then `generate_mesh` (DistMesh) + the standard cleanup pass. Quality is read
back with `simp_qual` (equilateral quality q_E, User Guide Eq. 1) and the
q_E - 3*sigma control-limit floor (Eq. 2).

GPL-3: runs ONLY inside the isolated mesh worker image, never the server venv.
This module holds NO trid3nt/server imports -- it is pure oceanmesh + numpy.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def build_coastal_tin(
    *,
    shoreline_shp: str,
    bbox: tuple[float, float, float, float],
    min_edge_length_m: float,
    max_edge_length_m: float,
    dem_path: str | None = None,
    grade: float = 0.15,
    feature_size: bool = True,
    wavelength: bool = False,
    slope: bool = False,
    wavelength_period_s: float = 12.42 * 3600.0,
    wavelength_grid_per_wl: float = 100.0,
    slope_parameter: float = 20.0,
    crs: int = 4326,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Generate a graded coastal TIN. Returns ``(points, cells, stats)``.

    ``bbox`` is ``(min_lon, min_lat, max_lon, max_lat)`` (EPSG:4326); oceanmesh's
    ``Region`` takes ``extent=(west, east, south, north)`` so we transpose. Edge
    lengths are metres and converted to the shoreline's degree units with the
    SFINCS/SWMM autoscaler convention (111_320 m/deg lat). Sizing functions are
    composed with ``compute_minimum`` (Guide: h = min over the active functions),
    graded, then meshed + cleaned.
    """
    import oceanmesh as om

    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    mid_lat = 0.5 * (min_lat + max_lat)
    m_per_deg = 111_320.0  # lat metres/deg; lon scaled by cos(lat) below
    # oceanmesh sizing functions work in the shoreline CRS (degrees for 4326);
    # express the edge bounds in degrees of latitude (the smaller deg/m axis).
    min_edge = float(min_edge_length_m) / m_per_deg
    max_edge = float(max_edge_length_m) / m_per_deg

    region = om.Region(extent=(min_lon, max_lon, min_lat, max_lat), crs=crs)
    shore = om.Shoreline(shoreline_shp, region.bbox, min_edge)
    sdf = om.signed_distance_function(shore)

    edge_fns = []
    if feature_size:
        edge_fns.append(
            om.feature_sizing_function(shore, sdf, max_edge_length=max_edge)
        )
    else:
        edge_fns.append(
            om.distance_sizing_function(shore, max_edge_length=max_edge)
        )
    dem = None
    if dem_path:
        dem = om.DEM(dem_path, crs=crs)
        if wavelength:
            edge_fns.append(
                om.wavelength_sizing_function(
                    dem, wl=wavelength_grid_per_wl, period=wavelength_period_s
                )
            )
        if slope:
            edge_fns.append(
                om.bathymetric_gradient_sizing_function(
                    dem,
                    slope_parameter=slope_parameter,
                    filter_quotient=50,
                    min_edge_length=min_edge,
                    max_edge_length=max_edge,
                    crs=crs,
                )
            )

    edge = edge_fns[0] if len(edge_fns) == 1 else om.compute_minimum(edge_fns)
    edge = om.enforce_mesh_gradation(edge, gradation=float(grade))

    def _pc(result: Any) -> tuple[np.ndarray, np.ndarray]:
        # oceanmesh cleanup fns vary their return arity across versions
        # (fix_mesh returns (p, t, ix) in v1.0.0); take the first two.
        return np.asarray(result[0]), np.asarray(result[1])

    points, cells = _pc(om.generate_mesh(sdf, edge))
    # Standard cleanup (README order): fix -> traversable boundaries -> drop
    # 1-face + low-quality boundary faces -> Laplacian smooth.
    points, cells = _pc(om.fix_mesh(points, cells))
    points, cells = _pc(om.make_mesh_boundaries_traversable(points, cells))
    points, cells = _pc(om.delete_faces_connected_to_one_face(points, cells))
    points, cells = _pc(om.delete_boundary_faces(points, cells, min_qual=0.15))
    points, cells = _pc(om.laplacian2(points, cells))

    # QUALITY: the OceanMesh2D User Guide Eq. 1 EQUILATERAL quality
    # q_E = 4*sqrt(3)*A_E / sum(edge^2), = 1.0 for an equilateral triangle
    # (NOT oceanmesh's own simp_qual, which is a SCALE-DEPENDENT radius ratio --
    # 0.577 for a unit equilateral). Computed in LOCAL METRIC coordinates so the
    # lon/lat degree anisotropy does not distort the shape metric, making it
    # directly comparable to the paper's reported q_E.
    m_per_deg_lon = m_per_deg * max(0.05, float(np.cos(np.radians(mid_lat))))
    px = (points[:, 0] - float(points[:, 0].mean())) * m_per_deg_lon
    py = (points[:, 1] - float(points[:, 1].mean())) * m_per_deg
    pm = np.column_stack([px, py])  # metres
    tri = pm[cells]  # (n, 3, 2)
    e0 = tri[:, 1] - tri[:, 0]
    e1 = tri[:, 2] - tri[:, 1]
    e2 = tri[:, 0] - tri[:, 2]
    a2 = (e0 ** 2).sum(1) + (e1 ** 2).sum(1) + (e2 ** 2).sum(1)
    area = 0.5 * np.abs(e0[:, 0] * (-e2[:, 1]) - (-e2[:, 0]) * e0[:, 1])
    with np.errstate(divide="ignore", invalid="ignore"):
        qual_raw = 4.0 * np.sqrt(3.0) * area / a2
    finite = np.isfinite(qual_raw)
    n_nonfinite = int((~finite).sum())
    qual = qual_raw[finite]
    if qual.size == 0:
        qual = np.array([0.0])
    seg_m = np.sqrt(np.concatenate(
        [(e0 ** 2).sum(1), (e1 ** 2).sum(1), (e2 ** 2).sum(1)]
    ))

    q_mean = float(qual.mean())
    q_std = float(qual.std())
    stats: dict[str, Any] = {
        "n_vertices": int(points.shape[0]),
        "n_elements": int(cells.shape[0]),
        "min_quality": round(float(qual.min()), 4),
        "mean_quality": round(q_mean, 4),
        "std_quality": round(q_std, 4),
        "n_nonfinite_quality": n_nonfinite,
        # OceanMesh2D User Guide Eq. 2 termination criterion: q_bar - 3*sigma > 0.75.
        "quality_3sigma_lcl": round(q_mean - 3.0 * q_std, 4),
        "quality_3sigma_pass": bool((q_mean - 3.0 * q_std) > 0.75),
        "edge_min_m": round(float(seg_m.min()), 2),
        "edge_mean_m": round(float(seg_m.mean()), 2),
        "edge_max_m": round(float(seg_m.max()), 2),
        "min_edge_length_m": float(min_edge_length_m),
        "max_edge_length_m": float(max_edge_length_m),
        "gradation": float(grade),
        "sizing_functions": [
            n for n, on in (
                ("feature_size", feature_size),
                ("distance", not feature_size),
                ("wavelength", wavelength and dem is not None),
                ("slope", slope and dem is not None),
            ) if on
        ],
        "bbox4326": [min_lon, min_lat, max_lon, max_lat],
    }
    return points, cells, stats


def mesh_to_geojson(
    points: np.ndarray, cells: np.ndarray, *, edge_budget: int = 60000
) -> dict[str, Any]:
    """Triangle-edge wireframe (EPSG:4326) as ONE MultiLineString feature -- the
    mesh_grid preview paradigm the server's mesh_preview styles. ``points`` are
    lon/lat (oceanmesh keeps the shoreline CRS = 4326). Interior edges are
    subsampled past ``edge_budget`` so a large mesh stays a light GeoJSON."""
    e = np.concatenate(
        [cells[:, [0, 1]], cells[:, [1, 2]], cells[:, [2, 0]]]
    )
    e = np.unique(np.sort(e, axis=1), axis=0)
    if e.shape[0] > edge_budget:
        stride = int(np.ceil(e.shape[0] / edge_budget))
        e = e[::stride]
    coords = [
        [
            [round(float(points[a, 0]), 6), round(float(points[a, 1]), 6)],
            [round(float(points[b, 0]), 6), round(float(points[b, 1]), 6)],
        ]
        for a, b in e
    ]
    lon = points[:, 0]
    lat = points[:, 1]
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"kind": "coastal_tin_wireframe"},
                "geometry": {"type": "MultiLineString", "coordinates": coords},
            }
        ],
        "bbox": [
            float(lon.min()), float(lat.min()),
            float(lon.max()), float(lat.max()),
        ],
    }
