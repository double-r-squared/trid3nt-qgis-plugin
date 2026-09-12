"""The one publisher: a field becomes a layer, a series or a profile a chart, a
field over time an animation, a track a vector layer, a series at a station the
station layer that carries it.

Written once for every engine. What arrives is a read - coordinates, values, an
axis, a result file - with a caption in the reader's own words; what leaves is
the layer the map loads, the chart the dock renders, the animation the seam
plays. Nothing here names an engine, a module, a template or a question."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from trid3nt_contracts.execution import LayerURI

from . import cog, raster
from .animation import publish_results_mesh_via_seam
from .reads import Deliverable, Field, Profile, Series, Track

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
    """The quantity token a caption's own words spell: ``water depth`` ->
    ``water_depth``. The still, the frames and the chart of one quantity are
    held to one scale by it."""
    return "_".join(str(caption).strip().lower().split())


async def publish(*, run_id: str, engine: str, name: str, where: str,
                  reference_time: str | None,
                  items: Sequence[Deliverable]) -> Published:
    """Publish every deliverable -> what the run leads with, and the rest.

    Layers first, so an animation of the same quantity adopts its scale; the
    first layer is the run's primary and is the step's own return, and every
    layer after it is surfaced beside it."""
    from trid3nt_server.emission.layer_uri_emit import publish_input_layer
    from trid3nt_server.emission.pipeline_emitter import (
        current_emitter,
        emit_chart_payloads,
    )
    from trid3nt_server.workflows.runtime.run_products import persist_run_products

    layers: list[LayerURI] = []
    charts: dict[str, Any] = {}
    animations = 0
    # ONE QUANTITY, ONE SCALE: every field layer of a quantity is ranged over
    # all of them, so two planes of one variable read on one ramp.
    fields: dict[str, list[Field]] = {}
    for item in items:
        if item.mode == "layer" and isinstance(item.read, Field):
            fields.setdefault(quantity_of(item.caption), []).append(item.read)
    for item in items:
        if item.mode == "layer" and isinstance(item.read, Track):
            layers.append(await asyncio.to_thread(
                _vector_layer, item.read, run_id=run_id, engine=engine, name=name,
                caption=item.caption))
        elif item.mode == "layer":
            layers.append(await asyncio.to_thread(
                _layer, item.read, run_id=run_id, engine=engine, name=name,
                caption=item.caption, style=item.style,
                value_range=_value_range(fields[quantity_of(item.caption)],
                                         item.style)))
        elif item.mode == "station":
            layers.append(await asyncio.to_thread(
                _station_layer, item.read, run_id=run_id, engine=engine, name=name,
                caption=item.caption, reference_time=reference_time))
    emitter = current_emitter()
    for layer in layers[1:]:
        await publish_input_layer(emitter, layer, role="primary")
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


#: A style row's ``range`` names the percentile the legend's top is capped at.
_PERCENTILE_CAP = re.compile(r"p(\d+(?:\.\d+)?)")


def _value_range(reads: Sequence[Field], style: Mapping[str, Any] | None
                 ) -> tuple[float, float]:
    """The legend range the fields of one quantity share, measured in hand.

    A floored field is read from zero: the floor is where the field STOPS being
    drawn, so the ramp's bottom is nothing rather than the faintest cell. A row
    that declares a centre is a diverging ramp, ranged symmetrically about it so
    the centre colour means the centre value on every run; one that declares a
    ``floor`` pins the bottom there, and one that declares a ``range`` of
    ``p<q>`` caps the top at that percentile of the field."""
    import numpy as np

    values = np.concatenate([np.asarray(read.values, dtype="float64").ravel()
                             for read in reads])
    finite = values[np.isfinite(values)]
    hi = float(finite.max()) if finite.size else 0.0
    lo = float(finite.min()) if finite.size else 0.0
    row = style or {}
    center = row.get("center")
    floors = [read.floor for read in reads if read.floor is not None]
    if center is not None:
        reach = max(abs(hi - float(center)), abs(lo - float(center)))
        return (round(float(center) - reach, 6), round(float(center) + reach, 6))
    cap = _PERCENTILE_CAP.fullmatch(str(row.get("range") or ""))
    if cap is not None and finite.size:
        hi = float(np.percentile(finite, float(cap.group(1))))
    if row.get("floor") is not None:
        lo = float(row["floor"])
    elif floors:
        lo, hi = 0.0, max(hi, *floors)
    return (round(lo, 6), round(hi, 6))


def _layer(read: Field, *, run_id: str, engine: str, name: str, caption: str,
           style: Mapping[str, Any] | None,
           value_range: tuple[float, float] | None = None) -> LayerURI:
    """A field -> ONE COG on the map, styled by the row the caller declared.

    The legend range is the field's own unless the caller ranged it over the
    quantity's other fields; a plane of a 3D result is named in the layer."""
    import numpy as np
    from rasterio.transform import from_bounds

    from trid3nt_server.emission import presets
    from trid3nt_server.emission.publish import PublishLayerError, publish_layer

    quantity = quantity_of(caption)
    which = quantity + ("" if read.plane is None else f"_{quantity_of(read.plane)}")
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
        crs_roundtrip_guard=True, dst_suffix=f"_{which}.tif")
    try:
        uri = cog.upload_cog(path, run_id, None, dest_filename=f"{which}.tif",
                             log_label=f"{which} COG")
    finally:
        cog.safe_unlink(path)

    label = f"{caption[:1].upper()}{caption[1:]} ({read.units})"
    legend = presets.legend_key(
        style, value_range=value_range or _value_range((read,), style),
        units=read.units, label=label)
    instant = "" if read.t is None else f"-t{int(read.t)}"
    plane = "" if read.plane is None else f", {read.plane}"
    layer = LayerURI(
        layer_id=f"{engine}-{which}{instant}-{run_id}",
        name=(f"Peak {caption}{plane} ({name})" if read.t is None
              else f"{label} at t = {read.t:g} s{plane} ({name})"),
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


def _vector_layer(read: Track, *, run_id: str, engine: str, name: str,
                  caption: str) -> LayerURI:
    """A track -> ONE GeoJSON in the run's store, as a vector layer on the map."""
    import json

    from trid3nt_server import storage

    quantity = quantity_of(caption)
    bucket, key = storage.runs_bucket(), f"{run_id}/{quantity}.geojson"
    storage.client().put_object(Bucket=bucket, Key=key,
                                Body=json.dumps(read.features).encode("utf-8"),
                                ContentType="application/geo+json")
    points = [xy for feature in read.features.get("features", ())
              for xy in feature["geometry"]["coordinates"]]
    bbox = ((min(p[0] for p in points), min(p[1] for p in points),
             max(p[0] for p in points), max(p[1] for p in points))
            if points else None)
    return LayerURI(layer_id=f"{engine}-{quantity}-{run_id}",
                    name=f"{caption[:1].upper()}{caption[1:]} ({name})",
                    layer_type="vector", uri=f"s3://{bucket}/{key}",
                    quantity=quantity, role="primary", bbox=bbox)


#: A station's declared bbox half-width, in degrees: a zero-extent bbox reads as
#: no extent to the camera, so the point gets a small honest box.
_STATION_PAD_DEG = 0.002


def _station_layer(read: Series, *, run_id: str, engine: str, name: str,
                   caption: str, reference_time: str | None) -> LayerURI:
    """A series at a station -> ONE point feature carrying the series inline.

    ``time_series_csv`` rows are ``iso,value`` counted from ``reference_time``,
    the same instant the run's frames are counted from; with no instant to
    count from the rows carry the run's own seconds."""
    import json
    from datetime import datetime, timedelta

    from trid3nt_server import storage

    if read.lon is None or read.lat is None:
        raise ValueError(f"the series {read.name!r} was read {read.at}, which is "
                         "no station to publish it at.")
    quantity = quantity_of(caption)
    origin = (datetime.fromisoformat(reference_time.replace("Z", "+00:00"))
              if reference_time else None)
    rows = []
    for t, v in zip(read.times, read.values):
        stamp = ((origin + timedelta(seconds=float(t))).isoformat()
                 if origin is not None else f"{float(t):.3f}")
        rows.append(f"{stamp},{float(v):.6f}")
    label = f"{caption[:1].upper()}{caption[1:]}"
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point",
                     "coordinates": [round(float(read.lon), 6),
                                     round(float(read.lat), 6)]},
        "properties": {"name": f"{label} {read.at}", "quantity": quantity,
                       "units": read.units, "variable": read.name,
                       "reference_time": reference_time,
                       "n_timesteps": len(rows),
                       "time_series_csv": "\n".join(rows) + "\n"},
    }
    bucket, key = storage.runs_bucket(), f"{run_id}/{quantity}.geojson"
    storage.client().put_object(
        Bucket=bucket, Key=key,
        Body=json.dumps({"type": "FeatureCollection",
                         "features": [feature]}).encode("utf-8"),
        ContentType="application/geo+json")
    return LayerURI(layer_id=f"{engine}-{quantity}-{run_id}",
                    name=f"{label} ({name})", layer_type="vector",
                    uri=f"s3://{bucket}/{key}", quantity=quantity, role="primary",
                    units=read.units,
                    style={"kind": "reference", "geometry": "point"},
                    bbox=(float(read.lon) - _STATION_PAD_DEG,
                          float(read.lat) - _STATION_PAD_DEG,
                          float(read.lon) + _STATION_PAD_DEG,
                          float(read.lat) + _STATION_PAD_DEG))


def _chart(read: Series | Profile, *, caption: str, where: str) -> dict[str, Any]:
    """A series or a profile -> the chart spec the dock renders, titled by the
    caption; every reference line rides as its own named series."""
    from trid3nt_server.emission.charts import build_chart_payload

    title = f"{caption[:1].upper()}{caption[1:]}"
    if isinstance(read, Profile):
        x, at = [float(d) for d in read.distance_m], read.along
        xfield, axis = "x_m", f"{at[:1].upper()}{at[1:]} (m)"
    else:
        x, at = [float(t) for t in read.times], read.at
        xfield, axis = "t_s", "Time (s)"
    values = [float(v) for v in read.values]
    rows = [{xfield: a, "value": v, "series": title} for a, v in zip(x, values)]
    for line in read.lines:
        rows += [{xfield: float(a), "value": float(v), "series": line.label}
                 for a, v in zip(line.x, line.values)]
    encoding = {"x": {"field": xfield, "type": "quantitative", "title": axis},
                "y": {"field": "value", "type": "quantitative",
                      "title": f"{title} ({read.units})"}}
    if read.lines:
        encoding["color"] = {"field": "series", "type": "nominal", "title": None}
    peak = max(range(len(values)), key=values.__getitem__) if values else 0
    low = min(range(len(values)), key=values.__getitem__) if values else 0
    what = (f"; lowest {values[low]:.3g} {read.units} at {x[low]:.0f} m"
            if isinstance(read, Profile) else
            f"; peaks at {values[peak]:.3g} {read.units} at t = {x[peak]:.0f} s")
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": not read.lines},
            "data": {"values": rows},
            "encoding": encoding,
        },
        title=f"{title}, {at} - {where}",
        caption=(f"The {caption}, {at}, at each of {len(x)} "
                 + ("stations" if isinstance(read, Profile) else "output times")
                 + (what if values else "") + "."
                 + "".join(f" {line.label} is drawn beside it." for line in read.lines)),
    )
