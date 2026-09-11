"""The one publisher: a field becomes a layer, a series a chart, a field over
time an animation.

Written once for every engine. What arrives is a read - coordinates, values, a
time axis, a result file - with a caption in the reader's own words; what leaves
is the layer the map loads, the chart the dock renders, the animation the seam
plays. Nothing here names an engine, a module, a template or a question."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from trid3nt_contracts.execution import LayerURI

from . import cog, raster
from .animation import publish_results_mesh_via_seam
from .reads import Deliverable, Field, Frames, Series

logger = logging.getLogger("trid3nt_server.workflows.publishing.publish")

__all__ = ["Published", "publish", "quantity_of"]

#: How far past the nodes a layer's bbox reaches, in degrees (~100 m), so a
#: ribbon painted to the bank is not clipped at it.
_PAD_DEG = 0.0009


@dataclass(frozen=True)
class Published:
    """What one outputs list left behind: the layer it leads with, and the rest."""

    primary: LayerURI | None
    layers: tuple[LayerURI, ...] = ()
    charts: Mapping[str, Any] = field(default_factory=dict)
    animations: int = 0


def quantity_of(caption: str) -> str:
    """The quantity token a caption's own words spell: ``dye concentration`` ->
    ``dye_concentration``. The still, the frames and the chart of one quantity
    are held to one scale by it."""
    return "_".join(str(caption).strip().lower().split())


async def publish(*, run_id: str, engine: str, name: str, where: str,
                  reference_time: str | None,
                  items: Sequence[Deliverable]) -> Published:
    """Publish every deliverable -> what the run leads with, and the rest.

    Layers first, so an animation of the same quantity adopts its scale; the
    first layer is the run's primary and is the step's own return."""
    from trid3nt_server.emission.pipeline_emitter import (
        current_emitter,
        emit_chart_payloads,
    )
    from trid3nt_server.workflows.shared.run_products import persist_run_products

    layers: list[LayerURI] = []
    charts: dict[str, Any] = {}
    animations = 0
    for item in items:
        if item.mode == "layer":
            layers.append(await asyncio.to_thread(
                _layer, item.read, run_id=run_id, engine=engine, name=name,
                caption=item.caption, style=item.style))
    emitter = current_emitter()
    for item in items:
        if item.mode == "chart":
            payload = _chart(item.read, caption=item.caption, where=where)
            charts[quantity_of(item.caption)] = payload
            await emit_chart_payloads(payload)
        elif item.mode == "animate":
            frames = item.read
            sibling = next((layer for layer in layers
                            if layer.quantity == quantity_of(item.caption)), None)
            animations += await publish_results_mesh_via_seam(
                emitter, run_id=run_id, engine=engine, peak_layer=sibling,
                peak_quantity=quantity_of(item.caption), mesh_group=frames.group,
                mesh_basename=frames.file, mesh_epsg=frames.epsg,
                reach_name=name, reference_time=reference_time)
    if charts:
        await persist_run_products(run_id, charts=charts, metrics=None)
    primary = layers[0] if layers else None
    if emitter is not None and primary is not None and primary.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(primary.bbox)})
        except Exception as exc:  # noqa: BLE001 - the camera never fails a publish
            logger.warning("zoom-to failed: %s", exc)
    return Published(primary=primary, layers=tuple(layers), charts=charts,
                     animations=animations)


def _layer(read: Field, *, run_id: str, engine: str, name: str, caption: str,
           style: Mapping[str, Any] | None) -> LayerURI:
    """A field -> ONE COG on the map, styled by the row the caller declared.

    The legend range is the field's own, measured while it is in hand."""
    import numpy as np
    from rasterio.transform import from_bounds

    from trid3nt_server.emission import presets
    from trid3nt_server.emission.publish import PublishLayerError, publish_layer

    quantity = quantity_of(caption)
    lon, lat = np.asarray(read.lon, dtype="float64"), np.asarray(read.lat, dtype="float64")
    bbox = (float(lon.min() - _PAD_DEG), float(lat.min() - _PAD_DEG),
            float(lon.max() + _PAD_DEG), float(lat.max() + _PAD_DEG))
    shape = raster.grid_shape(bbox, raster.TARGET_GROUND_RES_M)
    floor = -1e30 if read.floor is None else float(read.floor)
    grid = raster.rasterize_elements(lon, lat, read.ikle, read.values, bbox, shape,
                                     wet_floor=floor)
    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], shape[1], shape[0])
    path = cog.write_cog_4326_from_grid(
        grid, src_crs="EPSG:4326", src_transform=transform, reproject=False,
        crs_roundtrip_guard=True, dst_suffix=f"_{quantity}.tif")
    try:
        uri = cog.upload_cog(path, run_id, None, dest_filename=f"{quantity}.tif",
                             log_label=f"{quantity} COG")
    finally:
        cog.safe_unlink(path)

    values = np.asarray(read.values, dtype="float64")
    finite = values[np.isfinite(values)]
    hi = float(finite.max()) if finite.size else 0.0
    lo = float(finite.min()) if finite.size else 0.0
    # A floored field is read from zero: the floor is where the field STOPS
    # being drawn, so the ramp's bottom is nothing rather than the faintest cell.
    value_range = ((0.0, round(max(hi, read.floor), 6)) if read.floor is not None
                   else (round(lo, 6), round(hi, 6)))
    label = f"{caption[:1].upper()}{caption[1:]} ({read.units})"
    legend = presets.legend_key(style, value_range=value_range, units=read.units,
                                label=label)
    instant = "" if read.t is None else f"-t{int(read.t)}"
    layer = LayerURI(
        layer_id=f"{engine}-{quantity}{instant}-{run_id}",
        name=(f"Peak {caption} ({name})" if read.t is None
              else f"{label} at t = {read.t:g} s ({name})"),
        layer_type="raster", uri=uri, style=dict(style) if style else None,
        quantity=quantity, role="primary", units=read.units, bbox=bbox,
        legend=legend)
    try:
        published = publish_layer(layer_uri=uri, layer_id=layer.layer_id, style=style)
    except PublishLayerError as exc:
        # FAILURE NEVER RETRACTS: the COG is in the store and the case can still
        # find it, so the layer returns unstyled rather than the run losing it.
        logger.warning("publish_layer failed (%s) - the unpublished COG is returned",
                       exc)
        return layer
    return layer.model_copy(update={"uri": published})


def _chart(read: Series, *, caption: str, where: str) -> dict[str, Any]:
    """A series -> the chart spec the dock renders, titled by the caption."""
    from trid3nt_server.emission.charts import build_chart_payload

    times = [float(t) for t in read.times]
    values = [float(v) for v in read.values]
    peak = max(range(len(values)), key=values.__getitem__) if values else 0
    title = f"{caption[:1].upper()}{caption[1:]}"
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": True},
            "data": {"values": [{"t_s": t, "value": v} for t, v in zip(times, values)]},
            "encoding": {
                "x": {"field": "t_s", "type": "quantitative", "title": "Time (s)"},
                "y": {"field": "value", "type": "quantitative",
                      "title": f"{title} ({read.units})"},
            },
        },
        title=f"{title}, {read.at} - {where}",
        caption=(f"The {caption}, {read.at}, at each of {len(times)} output "
                 f"times" + (f"; peaks at {values[peak]:.3g} {read.units} at "
                             f"t = {times[peak]:.0f} s." if values else ".")),
    )
