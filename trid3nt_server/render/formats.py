"""The FORMAT SET, and the publish of one outputs list into it.

A raster arrives as a COG already in the store, a vector as the GeoJSON its
producer shaped, a mesh as the MDAL file its engine wrote plus the dataset files
written beside it, a series or a profile as a chart payload. Nothing here reads
an engine's result or paints a field: a product arrives in the set or it does
not arrive, and every product carries the style row its producer declared."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from trid3nt_contracts.execution import LayerURI

logger = logging.getLogger("trid3nt_server.render.formats")

__all__ = ["Chart", "Deliverable", "Mesh", "Published", "Raster", "Vector",
           "publish", "quantity_of"]


@dataclass(frozen=True, kw_only=True)
class Raster:
    """A COG in the store, in EPSG:4326."""

    uri: str
    bbox: tuple[float, float, float, float] | None = None
    units: str | None = None
    value_range: tuple[float, float] | None = None


@dataclass(frozen=True, kw_only=True)
class Vector:
    """A GeoJSON FeatureCollection, in lon/lat."""

    features: Mapping[str, Any]
    units: str | None = None
    #: Written under the run prefix as ``<stem>.geojson``.
    stem: str | None = None


@dataclass(frozen=True, kw_only=True)
class Mesh:
    """An MDAL mesh under the run prefix, and the ONE dataset group it paints.

    ``datasets`` are the dataset files written beside the mesh, loaded onto it
    before the group is bound; ``frames`` makes the layer temporal and ``t``
    names the instant a single-step group holds."""

    file: str
    group: str
    epsg: int
    datasets: tuple[str, ...] = ()
    #: The lon/lat box the MESH ITSELF spans - what the camera flies to. MDAL
    #: derives the layer's own extent from the file; this is the run stating it
    #: before the file is staged.
    bbox: tuple[float, float, float, float] | None = None
    reference_time: str | None = None
    frames: int | None = None
    t: float | None = None
    plane: str | None = None
    units: str | None = None
    value_range: tuple[float, float] | None = None


@dataclass(frozen=True, kw_only=True)
class Chart:
    """A chart payload the dock renders, built by whoever read the series."""

    payload: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class Deliverable:
    """One product and how it is drawn: the caption names the quantity in the
    producer's words, and ``style`` is the declared row."""

    product: Raster | Vector | Mesh | Chart
    caption: str
    style: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Published:
    """What one outputs list left behind: the layer it leads with, and the rest."""

    primary: LayerURI | None
    layers: tuple[LayerURI, ...] = ()
    charts: Mapping[str, Any] = field(default_factory=dict)


def quantity_of(caption: str) -> str:
    """The quantity token a caption's own words spell: ``water depth`` ->
    ``water_depth``. Every product of one quantity is held to one scale by it."""
    return "_".join(str(caption).strip().lower().split())


async def publish(*, run_id: str, engine: str, name: str,
                  items: Sequence[Deliverable]) -> Published:
    """Publish every deliverable -> what the run leads with, and the rest.

    Layers first, so the camera flies to the run's own product; the first layer
    is the run's primary and the step's own return, and every layer after it is
    surfaced beside it."""
    from trid3nt_server.render.layer_uri_emit import publish_input_layer
    from trid3nt_server.render.pipeline_emitter import (
        current_emitter,
        emit_chart_payloads,
    )
    from trid3nt_server.workflows.runtime.run_products import persist_run_products

    shared = _shared_ranges(items)
    layers: list[LayerURI] = []
    charts: dict[str, Any] = {}
    for item in items:
        quantity = quantity_of(item.caption)
        if isinstance(item.product, Mesh):
            layers.append(_mesh_layer(item, run_id=run_id, engine=engine,
                                      name=name, value_range=shared.get(quantity)))
        elif isinstance(item.product, Raster):
            layers.append(_raster_layer(item, run_id=run_id, engine=engine,
                                        name=name,
                                        value_range=shared.get(quantity)))
        elif isinstance(item.product, Vector):
            layers.append(await asyncio.to_thread(
                _vector_layer, item, run_id=run_id, engine=engine, name=name))
    emitter = current_emitter()
    for layer in layers[1:]:
        await publish_input_layer(emitter, layer, role="primary")
    for item in items:
        if isinstance(item.product, Chart):
            charts[quantity_of(item.caption)] = dict(item.product.payload)
            await emit_chart_payloads(dict(item.product.payload))
    if charts:
        await persist_run_products(run_id, charts=charts, metrics=None)
    record_run_outputs(layers)
    primary = layers[0] if layers else None
    if emitter is not None and primary is not None and primary.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(primary.bbox)})
        except Exception as exc:  # noqa: BLE001 - the camera never fails a publish
            logger.warning("zoom-to failed: %s", exc)
    return Published(primary=primary, layers=tuple(layers), charts=charts)


def record_run_outputs(layers: Sequence[LayerURI]) -> None:
    """Write every published layer onto the run in progress, for the record."""
    from trid3nt_server.workflows.runtime.journal import journal_outputs

    journal_outputs([layer.model_dump(mode="json",
                                      include={"layer_id", "name", "layer_type",
                                               "uri", "quantity", "units"})
                     for layer in layers])


def _shared_ranges(items: Sequence[Deliverable]
                   ) -> dict[str, tuple[float, float] | None]:
    """ONE range per quantity, over every product that measured one.

    Two planes of one variable and the still beside the animation read on one
    ramp, because the colour has to mean the same value on all of them."""
    from trid3nt_server.render import presets

    found: dict[str, list[tuple[float, float] | None]] = {}
    for item in items:
        measured = getattr(item.product, "value_range", None)
        if measured is not None:
            found.setdefault(quantity_of(item.caption), []).append(tuple(measured))
    return {quantity: presets.shared_range(ranges)
            for quantity, ranges in found.items()}


def _titles(item: Deliverable, *, name: str) -> tuple[str, str]:
    """``(the layer's name, the legend's label)`` for one product."""
    product = item.product
    caption = item.caption
    label = f"{caption[:1].upper()}{caption[1:]}"
    units = getattr(product, "units", None)
    legend = f"{label} ({units})" if units else label
    plane = getattr(product, "plane", None)
    where = f"{', ' + plane if plane else ''} ({name})"
    if getattr(product, "frames", None):
        return f"{label} over time{where}", legend
    t = getattr(product, "t", None)
    if t is None:
        return f"Peak {caption}{where}", legend
    return f"{legend} at t = {float(t):g} s{where}", legend


def _style_row(item: Deliverable, *, kind: str, label: str,
               value_range: tuple[float, float] | None,
               dataset_group: str | None = None) -> dict[str, Any]:
    """The declared row, shaped for the format the product arrived in.

    The producer declares the ramp, the units and where the legend is ranged
    from; the FORMAT decides which of the four shapes draws it."""
    row = {k: v for k, v in dict(item.style or {}).items()
           if k not in ("center", "floor", "range")}
    row["kind"] = kind
    row.setdefault("label", label)
    if dataset_group is not None:
        row["dataset_group"] = dataset_group
    if value_range is not None:
        row["scale"] = {"policy": "fixed",
                        "range": [float(value_range[0]), float(value_range[1])]}
    return row


def _mesh_layer(item: Deliverable, *, run_id: str, engine: str, name: str,
                value_range: tuple[float, float] | None) -> LayerURI:
    """An MDAL mesh + its dataset files -> ONE mesh layer painting one group."""
    from trid3nt_server import storage

    mesh: Mesh = item.product  # type: ignore[assignment]
    bucket = storage.runs_bucket()
    quantity = quantity_of(item.caption)
    title, label = _titles(item, name=name)
    stem = quantity if mesh.plane is None else f"{quantity}_{quantity_of(mesh.plane)}"
    instant = "" if mesh.t is None else f"-t{int(mesh.t)}"
    return LayerURI(
        layer_id=f"{engine}-{stem}{instant}-{run_id}",
        name=title,
        layer_type="mesh",
        uri=f"s3://{bucket}/{run_id}/{mesh.file}",
        dataset_uris=[f"s3://{bucket}/{run_id}/{basename}"
                      for basename in mesh.datasets],
        style=_style_row(item, kind="mesh", label=label, value_range=value_range,
                         dataset_group=mesh.group),
        quantity=quantity, role="primary", units=mesh.units, bbox=mesh.bbox,
        crs_authid=f"EPSG:{int(mesh.epsg)}",
        reference_time=mesh.reference_time)


def _raster_layer(item: Deliverable, *, run_id: str, engine: str, name: str,
                  value_range: tuple[float, float] | None) -> LayerURI:
    """A COG in the store -> ONE raster layer, styled by the declared row."""
    raster: Raster = item.product  # type: ignore[assignment]
    quantity = quantity_of(item.caption)
    title, label = _titles(item, name=name)
    return LayerURI(
        layer_id=f"{engine}-{quantity}-{run_id}",
        name=title, layer_type="raster", uri=raster.uri,
        style=_style_row(item, kind="continuous", label=label,
                         value_range=value_range or raster.value_range),
        quantity=quantity, role="primary", units=raster.units, bbox=raster.bbox)


def _vector_layer(item: Deliverable, *, run_id: str, engine: str,
                  name: str) -> LayerURI:
    """A GeoJSON FeatureCollection -> ONE vector layer in the run's store."""
    import json

    from trid3nt_server import storage

    vector: Vector = item.product  # type: ignore[assignment]
    quantity = quantity_of(item.caption)
    stem = vector.stem or quantity
    bucket, key = storage.runs_bucket(), f"{run_id}/{stem}.geojson"
    storage.client().put_object(
        Bucket=bucket, Key=key,
        Body=json.dumps(vector.features).encode("utf-8"),
        ContentType="application/geo+json")
    title, label = _titles(item, name=name)
    return LayerURI(
        layer_id=f"{engine}-{quantity}-{run_id}",
        name=title, layer_type="vector", uri=f"s3://{bucket}/{key}",
        style=_style_row(item, kind="reference", label=label, value_range=None),
        quantity=quantity, role="primary", units=vector.units,
        bbox=_features_bbox(vector.features))


#: How far a zero-extent geometry's declared bbox reaches, in degrees: a point
#: reads as no extent to the camera, so it gets a small honest box.
_POINT_PAD_DEG = 0.002


def _features_bbox(features: Mapping[str, Any]
                   ) -> tuple[float, float, float, float] | None:
    """The lon/lat box a FeatureCollection's own coordinates span."""
    points: list[tuple[float, float]] = []

    def _walk(coords: Any) -> None:
        if (isinstance(coords, (list, tuple)) and len(coords) >= 2
                and all(isinstance(v, (int, float)) for v in coords[:2])):
            points.append((float(coords[0]), float(coords[1])))
        elif isinstance(coords, (list, tuple)):
            for item in coords:
                _walk(item)

    for feature in features.get("features") or ():
        _walk((feature.get("geometry") or {}).get("coordinates"))
    if not points:
        return None
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    pad = _POINT_PAD_DEG if len(points) == 1 else 0.0
    return (min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad)
