"""The ONE typed conversion a data-valued op kwarg passes through.

An op takes the data CLASS it is defined over; a recipe names that data as
whatever address the chain produced."""

from __future__ import annotations

import json
import math
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from trid3nt_server.inputs.geometry import METRES_PER_DEGREE
from trid3nt_server.tools.mesh.meshers import MeshToolError

__all__ = ["MeshTooLarge", "SolveEstimate", "finest_edge", "op_geometry", "op_input",
           "op_raster", "over_limit_accepted", "raster_cell_m", "raster_scope",
           "solve_estimated_by"]

#: The stated edge a person accepted over MESH_MAX_NODES or SOLVE_BUDGET_S, which is then built as stated.
_ACCEPTED_EDGE: ContextVar[float | None] = ContextVar("mesh_accepted_edge", default=None)


#: The engine's estimate of the wall seconds its solve takes on a mesh of ``nodes`` at ``edge_m``; the mesh knows no engine, so the caller hands it in.
SolveEstimate = Callable[[int, float], float]
_SOLVE: ContextVar[SolveEstimate | None] = ContextVar("mesh_solve_estimate", default=None)


@contextmanager
def solve_estimated_by(estimate: SolveEstimate | None) -> Iterator[None]:
    token = _SOLVE.set(estimate)
    try:
        yield
    finally:
        _SOLVE.reset(token)


@contextmanager
def over_limit_accepted(edge_m: float | None) -> Iterator[None]:
    token = _ACCEPTED_EDGE.set(edge_m)
    try:
        yield
    finally:
        _ACCEPTED_EDGE.reset(token)


class MeshTooLarge(MeshToolError):
    """The refusal a gate card quotes: the stated edge, its estimates, the limits and the finest edge that fits both.

    MESH_TOO_LARGE where the node estimate passes MESH_MAX_NODES; SOLVE_OVER_BUDGET where the engine's solve estimate passes SOLVE_BUDGET_S."""

    def __init__(self, message: str, *, edge_m: float, estimate: int, limit: int,
                 fit_edge_m: float, solve_s: float | None = None,
                 budget_s: float | None = None) -> None:
        super().__init__("MESH_TOO_LARGE" if solve_s is None else "SOLVE_OVER_BUDGET", message)
        self.edge_m, self.estimate = edge_m, estimate
        self.limit, self.fit_edge_m = limit, fit_edge_m
        self.solve_s, self.budget_s = solve_s, budget_s

    def __reduce__(self) -> tuple[Any, ...]:
        return (_rebuild_too_large, (str(self), self.edge_m, self.estimate,
                                     self.limit, self.fit_edge_m, self.solve_s,
                                     self.budget_s))


def _rebuild_too_large(message: str, edge_m: float, estimate: int, limit: int,
                       fit_edge_m: float, solve_s: float | None,
                       budget_s: float | None) -> MeshTooLarge:
    return MeshTooLarge(message, edge_m=edge_m, estimate=estimate, limit=limit,
                        fit_edge_m=fit_edge_m, solve_s=solve_s, budget_s=budget_s)


def finest_edge(stated: Any, ops: Iterable[Any],
                bbox: tuple[float, float, float, float],
                area_m2: float | None = None) -> tuple[float, str | None]:
    """The mesh's finest edge -> ``(edge_m, the sentence the mesh states about it)``.

    A stated edge stands whatever the data under it, and the bed says so where it is coarser; unstated, it is the finest cell
    of the rasters the recipe names, because the mesh library requires one and nothing finer is measured.
    Unstated, the edge is the coarser of that cell and the finest edge both MESH_MAX_NODES and, where the engine hands in a solve
    estimate, SOLVE_BUDGET_S (0, the default, is off) let the domain hold, and the run notes say which limit set it; a stated edge whose mesh would pass
    either refuses here, before anything is built, unless that edge was accepted at the gate."""
    from trid3nt_server.settings import setting

    edge, note = _finest_edge(stated, ops, bbox)
    area = _domain_area_m2(bbox, area_m2)
    limit = int(setting("MESH_MAX_NODES"))
    solve = _SOLVE.get()
    budget = float(setting("SOLVE_BUDGET_S")) if solve is not None else 0.0
    if budget <= 0.0:
        solve = budget = None
    fit_nodes = _edge_for_nodes(limit, area)
    fit_solve = _edge_for_solve(solve, budget, area) if solve is not None else 0.0
    fit = max(fit_nodes, fit_solve)
    if stated in (None, ""):
        if fit > edge:
            from trid3nt_server.inputs.run_notes import journal_note

            by = (f"SOLVE_BUDGET_S = {budget:,.0f} s (an estimated solve of "
                  f"{solve(_estimated_nodes(fit, area), fit):,.0f} s)"
                  if fit_solve > fit_nodes else f"MESH_MAX_NODES = {limit:,}")
            derived = (f"no edge was stated, so the resolution is {fit:.4g} m, "
                       f"derived from {by} (the cell of the finest data is {edge:g} m)")
            journal_note(derived)
            return fit, derived
        return edge, note
    if _ACCEPTED_EDGE.get() == edge:
        return edge, note
    estimate = _estimated_nodes(edge, area)
    if estimate > limit:
        raise MeshTooLarge(
            f"a {edge:g} m edge over this domain ({area / 1e6:.4g} km2) is "
            f"an estimated {estimate:,} nodes, past the limit of {limit:,} "
            "(setting MESH_MAX_NODES). State a coarser mesh_resolution_m, or "
            "raise MESH_MAX_NODES in Settings.",
            edge_m=edge, estimate=estimate, limit=limit, fit_edge_m=fit)
    seconds = solve(estimate, edge) if solve is not None else 0.0
    if budget is not None and seconds > budget:
        raise MeshTooLarge(
            f"a {edge:g} m edge over this domain ({area / 1e6:.4g} km2) is an "
            f"estimated {estimate:,} nodes and a solve of about {seconds:,.0f} s, "
            f"past the budget of {budget:,.0f} s (setting SOLVE_BUDGET_S). State "
            "a coarser mesh_resolution_m, or raise SOLVE_BUDGET_S in Settings.",
            edge_m=edge, estimate=estimate, limit=limit, fit_edge_m=fit,
            solve_s=seconds, budget_s=budget)
    return edge, note


def _domain_area_m2(bbox: tuple[float, float, float, float],
                    area_m2: float | None) -> float:
    """The domain's area; the box's own where no polygon area is given."""
    if area_m2 is not None:
        return area_m2
    lon0, lat0, lon1, lat1 = bbox
    return ((lon1 - lon0) * METRES_PER_DEGREE
            * math.cos(math.radians(0.5 * (lat0 + lat1)))
            * (lat1 - lat0) * METRES_PER_DEGREE)


#: One equilateral-triangle node's share of area is this factor times edge**2; every element is taken at the finest edge, so grading can only lower the count.
_NODE_AREA = math.sqrt(3.0) / 2.0


def _estimated_nodes(edge_m: float, area_m2: float) -> int:
    return int(area_m2 / (_NODE_AREA * edge_m ** 2))


def _edge_for_nodes(nodes: int, area_m2: float) -> float:
    """The finest edge whose estimate fits ``nodes``: the estimate inverted."""
    return math.sqrt(area_m2 / (_NODE_AREA * nodes))


def _edge_for_solve(solve: SolveEstimate, budget_s: float, area_m2: float) -> float:
    """The finest edge whose solve estimate fits ``budget_s``, by bisection: the estimate falls as the edge grows.

    A domain no edge fits - one element across it is still over - takes that coarsest edge."""
    fine, coarse = 1e-3, math.sqrt(area_m2)
    if solve(_estimated_nodes(coarse, area_m2), coarse) > budget_s:
        return coarse
    for _ in range(80):
        mid = math.sqrt(fine * coarse)
        if solve(_estimated_nodes(mid, area_m2), mid) > budget_s:
            fine = mid
        else:
            coarse = mid
    return coarse


def _finest_edge(stated: Any, ops: Iterable[Any],
                 bbox: tuple[float, float, float, float]) -> tuple[float, str | None]:
    if stated not in (None, ""):
        return float(stated), None
    finest = min((cell for op in ops for cell in _cells(op, bbox)), default=None)
    if finest is None:
        raise MeshToolError(
            "MESH_EDGE_UNSTATED",
            "no resolution_m was stated and no op names a raster whose cell "
            "could set it, and the mesher needs a finest edge. State "
            "resolution_m, or name the bed with set_bed.")
    return finest[0], (f"no edge was stated, so the finest edge is "
                       f"{finest[0]:g} m, the cell of {finest[1]}")


def _cells(op: Any, bbox: tuple[float, float, float, float]
           ) -> Iterable[tuple[float, str]]:
    """The cell, in metres, of every raster one recipe op names."""
    from trid3nt_server.tools.mesh.shared.primitives import bed_raster_of
    from trid3nt_server.inputs.geometry import artifact_class

    for name, value in dict(op.kwargs).items():
        if op.fn == "set_bed" and name == "source":
            path = bed_raster_of(value, bbox)
        elif artifact_class(value) == "raster":
            path = op_raster(value)
        else:
            continue
        if path is not None:
            yield raster_cell_m(path), f"{op.fn} {name}"


def raster_cell_m(path: Path) -> float:
    """A raster's finer cell side, in metres."""
    import rasterio

    with rasterio.open(str(path)) as src:
        dx, dy = (abs(float(v)) for v in src.res)
        if src.crs is not None and src.crs.is_geographic:
            lat = math.radians(0.5 * (src.bounds.bottom + src.bounds.top))
            dx, dy = dx * METRES_PER_DEGREE * math.cos(lat), dy * METRES_PER_DEGREE
    return round(min(dx, dy), 3)


def op_input(value: Any) -> Any:
    """One op kwarg as the concrete thing the op is defined over."""
    from trid3nt_server.inputs.geometry import artifact_class

    if isinstance(value, Mapping) and "type" in value:
        return dict(value)
    # Two conversions, chosen by the artifact's CLASS and never guessed: raster -> readable raster, vector -> geometry document.
    # A value whose class is not knowable passes through and the op refuses it in its own words.
    kind = artifact_class(value)
    if kind == "raster":
        return op_raster(value)
    if kind == "vector":
        return op_geometry(value)
    return value


def op_raster(source: Any) -> Path:
    """A raster source -> a LOCAL readable path, whatever it arrived as."""
    from trid3nt_server.tools.cache import read_object_bytes_s3
    from trid3nt_server.inputs.geometry import source_uri

    uri = str(source_uri(source) or "").strip()
    if not uri:
        raise MeshToolError(
            "MESH_OP_INPUT_UNREADABLE",
            f"the raster {source!r} names no readable address.")
    if uri.startswith("s3://"):
        from trid3nt_contracts import new_ulid

        local = _rasters_dir() / f"raster-{new_ulid()}{Path(uri).suffix or '.tif'}"
        local.write_bytes(read_object_bytes_s3(uri))
        return local
    path = Path(uri)
    if not path.exists():
        raise MeshToolError(
            "MESH_OP_INPUT_UNREADABLE",
            f"the raster {uri!r} is neither an object-store uri nor a file on "
            "disk.")
    return path


def op_geometry(source: Any) -> dict[str, Any]:
    """A geometry source -> GeoJSON, whatever vector format it arrived in.

    A source is a typed SLOT value, inline GeoJSON, an object-store uri, a path,
    or a layer handle."""
    from trid3nt_server.tools.cache import read_object_bytes_s3
    from trid3nt_server.inputs.geometry import source_uri

    # A slot's typed value carries its own collection: reading it back off an
    # address would re-open what is already in hand, and a drawn domain has no
    # address at all.
    collection = getattr(source, "as_feature_collection", None)
    if callable(collection):
        return collection()
    resolved = source_uri(source)
    if isinstance(resolved, Mapping):
        return dict(resolved)
    text = str(resolved).strip()
    if text.startswith("{"):
        return json.loads(text)
    if text.startswith("s3://"):
        from trid3nt_contracts import new_ulid

        raw = read_object_bytes_s3(text)
        suffix = Path(text).suffix.lower()
        if suffix in (".geojson", ".json"):
            return json.loads(raw.decode("utf-8"))
        with tempfile.TemporaryDirectory(prefix="mesh-geometry-") as scratch:
            local = Path(scratch) / f"geom-{new_ulid()}{suffix}"
            local.write_bytes(raw)
            return _read_vector(local)
    path = Path(text)
    if not path.exists():
        raise MeshToolError(
            "MESH_OP_INPUT_UNREADABLE",
            f"the geometry {source!r} could not be read: it is neither inline "
            "GeoJSON, an object-store uri, nor a file on disk.")
    return _read_vector(path)


def _read_vector(path: Path) -> dict[str, Any]:
    if path.suffix.lower() in (".geojson", ".json"):
        return json.loads(path.read_text())
    import geopandas as gpd

    return json.loads(gpd.read_file(path).to_crs(4326).to_json())


_RASTER_DIR: ContextVar[Path | None] = ContextVar("mesh_raster_dir", default=None)


@contextmanager
def raster_scope() -> Iterator[Path]:
    """The directory one mesh build downloads its op rasters into; it is removed when the build returns."""
    with tempfile.TemporaryDirectory(prefix="mesh-rasters-") as scratch:
        token = _RASTER_DIR.set(Path(scratch))
        try:
            yield Path(scratch)
        finally:
            _RASTER_DIR.reset(token)


def _rasters_dir() -> Path:
    scope = _RASTER_DIR.get()
    if scope is None:
        raise MeshToolError(
            "MESH_OP_INPUT_UNREADABLE",
            "an object-store raster was read outside a mesh build, which owns the directory it lands in.")
    return scope
