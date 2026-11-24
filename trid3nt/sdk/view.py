"""Draw the layers and chart payloads a run holds with matplotlib."""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping

from trid3nt.errors import DeclarationError
from trid3nt.render.presets import from_row, ramp_stops


def view(source: Any, *, root: str | Path | None = None, mesh: bool = False,
         frame: int = -1) -> list[Any]:
    """Return figures for a layer, a chart payload, or a run's recorded products."""
    from .model import Result

    if isinstance(source, Result):
        root = source.root if root is None else root
        source = source.record
    if hasattr(source, "model_dump"):
        source = source.model_dump()
    directory = Path(root or ".")
    if "outputs" not in source:
        return [_figure(source, directory, mesh, frame)]
    figures = [_figure(row, directory, mesh, frame) for row in source["outputs"]]
    if source.get("run_id"):
        from trid3nt.workflows.runtime.run_output import CHART_SPEC_KEY

        spec = directory / "runs" / source["run_id"] / CHART_SPEC_KEY
        if spec.is_file():
            figures.extend(_figure(chart, directory, mesh, frame)
                           for chart in json.loads(spec.read_text()).values())
    return figures


def _path(uri: str, root: Path, stack: ExitStack) -> Path:
    if not uri.startswith("s3://"):
        return root / uri
    bucket, _, key = uri[5:].partition("/")
    local = root / bucket / key
    if bucket in {"data", "runs", "cache"} and local.is_file():
        return local
    from trid3nt.store.objects import client

    target = Path(stack.enter_context(TemporaryDirectory())) / Path(key).name
    client().download_file(bucket, key, str(target))
    return target


def _figure(row: Mapping[str, Any], root: Path, grid: bool, frame: int):
    from matplotlib import pyplot as plt

    kind = row.get("layer_type") or ("chart" if "vega_lite_spec" in row else None)
    if kind not in ("raster", "vector", "mesh", "chart"):
        raise DeclarationError(f"Notebook view cannot draw layer kind {kind!r}.",
                               error_code="VIEW_LAYER_UNSUPPORTED")
    figure, axes = plt.subplots()
    try:
        with ExitStack() as files:
            if kind == "chart":
                _chart(axes, row)
            else:
                path = _path(row["uri"], root, files)
                if kind == "raster":
                    _raster(axes, row, path)
                elif kind == "vector":
                    _vector(axes, row, path)
                else:
                    datasets = [_path(uri, root, files) for uri in row.get("dataset_uris") or ()]
                    _mesh(axes, row, path, datasets, grid, frame)
        axes.set_title(row.get("title") or row.get("name") or "")
    except BaseException:
        plt.close(figure)
        raise
    return figure


def _axes(axes, crs):
    axes.set_aspect("equal")
    suffix = f" ({crs})" if crs else ""
    axes.set(xlabel=f"X{suffix}", ylabel=f"Y{suffix}")


def _colors(row):
    from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap, ListedColormap, Normalize

    style = from_row(row.get("style"))
    if style.classes:
        cmap = ListedColormap([item[2] for item in style.classes])
        norm = BoundaryNorm([item[0] for item in style.classes] + [style.classes[-1][1]], cmap.N)
        return cmap, norm
    legend = row.get("legend") or {}
    interval = (legend.get("vmin"), legend.get("vmax"))
    if None in interval and style.scale.range:
        interval = style.scale.range
    ramp = legend.get("colormap") or style.ramp
    stops = [(fraction, color) for fraction, color in ramp] if isinstance(ramp, (list, tuple)) else ramp_stops(ramp)
    return LinearSegmentedColormap.from_list("layer", stops), Normalize(*interval)


def _legend(axes, artist, row):
    key = row.get("legend") or {}
    axes.figure.colorbar(artist, ax=axes, label=key.get("units") or row.get("units") or "")


def _raster(axes, row, path):
    import rasterio
    from rasterio.plot import plotting_extent

    with rasterio.open(path) as raster:
        values = raster.read(1, masked=True)
        cmap, norm = _colors(row)
        artist = axes.imshow(values, extent=plotting_extent(raster), cmap=cmap, norm=norm)
        _axes(axes, raster.crs)
    _legend(axes, artist, row)


def _vector(axes, row, path):
    import geopandas as gpd

    data = gpd.read_file(path)
    style = from_row(row.get("style"))
    if style.classes and style.attribute:
        colors = data[style.attribute].map(lambda value: next(
            (color for lower, upper, color, _ in style.classes if lower <= value <= upper), style.color))
        data.plot(ax=axes, color=colors)
    elif style.kind == "reference" and not data.empty and data.geom_type.str.contains("Polygon").all():
        data.boundary.plot(ax=axes, color=style.color)
    else:
        data.plot(ax=axes, color=style.color)
    _axes(axes, data.crs or row.get("crs_authid"))


def _mesh(axes, row, path, datasets, grid, frame):
    import numpy as np
    from matplotlib.tri import Triangulation

    style = from_row(row.get("style"))
    group = style.dataset_group
    if path.suffix.lower() in {".slf", ".ser", ".selafin"}:
        from trid3nt.workflows.telemac.authoring.selafin_io import read_selafin

        native = read_selafin(path)
        x, y, faces = native["x"], native["y"], native["ikle2"]
        values = native["data"].get(group)
        owners = None
    elif path.suffix.lower() in {".nc", ".netcdf"}:
        x, y, faces, owners, values = _ugrid(path, group)
    elif path.suffix.lower() == ".2dm":
        nodes, faces = {}, []
        for line in path.read_text().splitlines():
            parts = line.split()
            if parts and parts[0] == "ND":
                nodes[int(parts[1])] = (float(parts[2]), float(parts[3]))
            elif parts and parts[0] in {"E3T", "E4Q"}:
                faces.append([int(node) for node in parts[2:-1]])
        indices = {node: i for i, node in enumerate(nodes)}
        x, y = zip(*nodes.values())
        faces, owners = _triangles([[indices[node] for node in face] for face in faces])
        values = None
    else:
        raise DeclarationError(f"Notebook view cannot draw mesh format {path.suffix!r}.",
                               error_code="VIEW_MESH_UNSUPPORTED")
    for dataset in datasets:
        values = _sms(dataset, group, len(x))
        owners = None
        if values is not None:
            break
    tri = Triangulation(x, y, faces)
    if values is not None:
        data = np.ma.masked_invalid(values[frame])
        if style.floor is not None:
            data = np.ma.masked_less(data, style.floor)
        if owners is None:
            tri.set_mask(np.ma.getmaskarray(data)[tri.triangles].any(axis=1))
        cmap, norm = _colors(row)
        if tri.get_masked_triangles().size:
            if owners is None:
                artist = axes.tripcolor(tri, data.filled(0), cmap=cmap, norm=norm, shading="gouraud")
            else:
                artist = axes.tripcolor(tri, facecolors=data[owners], cmap=cmap, norm=norm)
            _legend(axes, artist, row)
    elif group:
        raise DeclarationError(f"Notebook view could not find mesh dataset {group!r}.",
                               error_code="VIEW_DATASET_MISSING")
    if grid or values is None:
        axes.triplot(Triangulation(x, y, faces), color=style.color, linewidth=.4)
    _axes(axes, row.get("crs_authid"))


def _triangles(rings):
    faces, owners = [], []
    for index, ring in enumerate(rings):
        for offset in range(1, len(ring) - 1):
            faces.append((ring[0], ring[offset], ring[offset + 1]))
            owners.append(index)
    return faces, owners


def _ugrid(path, group):
    import netCDF4
    import numpy as np

    with netCDF4.Dataset(path) as data:
        topology = next(var for var in data.variables.values() if getattr(var, "cf_role", None) == "mesh_topology")
        x, y = (data[name][:] for name in topology.node_coordinates.split())
        connectivity = data[topology.face_node_connectivity]
        start = getattr(connectivity, "start_index", 0)
        faces, owners = _triangles([list(np.ma.asarray(row).compressed() - start) for row in connectivity[:]])
        from trid3nt.render.mesh_display import _identifier

        variable = next((var for name, var in data.variables.items()
                         if group and _identifier(group) in
                         {_identifier(name), _identifier(getattr(var, "long_name", name))}), None)
        if variable is None:
            return x, y, faces, owners, None
        if getattr(variable, "location", "") == "node":
            owners = None
        return x, y, faces, owners, np.atleast_2d(variable[:])


def _sms(path, group, nodes):
    import numpy as np

    lines = path.read_text().splitlines()
    if not any(line == f'NAME "{group}"' for line in lines):
        return None
    return np.asarray([[float(value) for value in lines[i + 1:i + 1 + nodes]]
                       for i, line in enumerate(lines) if line.startswith("TS ")])


def _chart(axes, row):
    spec = row.get("vega_lite_spec", row)
    for layer in spec.get("layer", [spec]):
        enc = layer.get("encoding", spec.get("encoding", {}))
        data = layer.get("data", spec.get("data", {})).get("values", [])
        x, y = enc.get("x", {}).get("field"), enc.get("y", {}).get("field")
        if x and y:
            series = [point for point in data if point.get(x) is not None and point.get(y) is not None]
            axes.plot([point[x] for point in series], [point[y] for point in series])
            axes.set(xlabel=enc["x"].get("title", x), ylabel=enc["y"].get("title", y))
