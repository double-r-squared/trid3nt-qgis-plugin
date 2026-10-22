"""One read a template asks for: the primitive, what the engine measured, and the format it is delivered in.

A primitive is named from the module's variable vocabulary (``field``, ``series``, ``max_over_time``, ``profile``,
``extent``, ``mesh``, ``mass_balance``); the read is the engine's answer to it and ``deliver`` is where it lands.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from typing import Any, Callable, Mapping, Sequence

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.render.formats import Chart, Deliverable, Mesh, Raster, RasterFrame, Vector, publish
from trid3nt_server.workflows.runtime import DeclarativeError

logger = logging.getLogger("trid3nt_server.workflows.runtime.primitives")

__all__ = [
    "Comparison",
    "Field",
    "Frames",
    "Line",
    "Profile",
    "Rasters",
    "Read",
    "Series",
    "Spectrum",
    "Track",
    "deliver",
    "OutputEmpty",
    "Primitive",
    "RasterUnanchored",
    "extent",
    "field",
    "mass_balance",
    "max_over_time",
    "mesh",
    "profile",
    "publish_reads",
    "series",
    "unanchored",
]

@dataclass(frozen=True, kw_only=True)
class Read:
    """What one primitive measured, by name. A bare read publishes nothing."""

    measures: Mapping[str, Any] = dataclass_field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class Field(Read):
    """One variable over the 2D nodes at one instant, or its envelope over time.

    ``t`` is the instant (``None`` the envelope); ``plane`` names the plane of a 3D result; a node below ``floor`` is nothing.
    """

    name: str
    units: str
    values: Any
    t: float | None = None
    plane: str | None = None
    floor: float | None = None
    # The values the measures and legend were read over - the nodes this run held water on; ``None`` reads the field whole.
    wet: Any = None


@dataclass(frozen=True, kw_only=True)
class Line:
    """One more line on a chart, over its x axis: a reference the caller computed, under its own label."""

    label: str
    x: Any
    values: Any


@dataclass(frozen=True, kw_only=True)
class Series(Read):
    """One variable over time: the domain maximum at each instant, or a point's."""

    name: str
    units: str
    times: Any
    values: Any
    # Where the series was read: ``"the domain maximum"`` or a point's name.
    at: str
    # The station the series was read at, in lon/lat; ``None`` for a maximum.
    lon: float | None = None
    lat: float | None = None
    lines: tuple[Line, ...] = ()


@dataclass(frozen=True, kw_only=True)
class Comparison(Read):
    """One variable solved against measured at each site of a set: a row per site the result holds.

    Each row is the site's own check - its ``site``, ``observed`` and ``simulated`` in ``units``, and when it was sampled.
    """

    name: str
    units: str
    sites: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, kw_only=True)
class Profile(Read):
    """One variable along a line at one instant: a value per station."""

    name: str
    units: str
    distance_m: Any
    values: Any
    # What the x axis is, e.g. ``"downstream distance"``.
    along: str
    lines: tuple[Line, ...] = ()


@dataclass(frozen=True, kw_only=True)
class Spectrum(Read):
    """The sea's energy at one point, over the polar frequency-direction grid.

    ``density`` is F(f, theta) at one instant (frequency rows, direction columns); ``values`` is its integral over direction. A direction is the grid's own angle in the deck's convention.
    """

    units: str
    frequency_hz: Any
    values: Any
    directions_deg: Any
    density: Any
    # Where the spectrum was recorded: the printout point's name.
    at: str
    lines: tuple[Line, ...] = ()


@dataclass(frozen=True, kw_only=True)
class Track(Read):
    """Positions at written instants, as a GeoJSON FeatureCollection in lon/lat."""

    features: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class Frames(Read):
    """One variable over time, as the result file an animation plays from."""

    name: str
    units: str
    # Every frame over the 2D nodes. A temporal layer's legend is measured over the whole record: ranged on the last frame alone, a peak-and-flush variable animates against an empty field.
    values: Any
    # The result file's basename under the run prefix.
    file: str
    # The dataset group the mesh reader binds the variable by.
    group: str
    epsg: int
    reference_time: str | None
    frames: int
    # Where the variable stops being drawn. A group in the result file cannot be rewritten with nothing below it, so the floor rides on the style row and the renderer masks there.
    floor: float | None = None


@dataclass(frozen=True, kw_only=True)
class Rasters(Read):
    """One variable on a lon/lat grid as north-up rasters: one per instant, or the one envelope over the run.

    ``values`` is (frames, rows, cols) with nothing as NaN; ``bounds`` the grid's cell edges (west, south, east, north);
    ``times`` each frame's seconds into the run (empty for an envelope) and ``until`` the second the run ends at, where
    the last frame's window stops (``None`` ends it at its own instant). The legend is measured over every frame.
    """

    name: str
    units: str
    values: Any
    bounds: tuple[float, float, float, float]
    times: tuple[float, ...] = ()
    until: float | None = None


class OutputEmpty(DeclarativeError):
    """The variable a primitive names is not in the result, or never rose above its floor."""

    error_code = "TELEMAC_OUTPUT_EMPTY"


class RasterUnanchored(DeclarativeError):
    """A raster read at instants on a run that states no reference time, so no frame has an absolute instant."""

    error_code = "RASTER_UNANCHORED"



@dataclass(frozen=True, eq=True)
class Primitive:
    """One read a template asks for, and how it is published.

    A value hashed by what it reads, so a resolved point or line of any shape keys it.
    """

    kind: str
    variable: str | None = None
    # ``"every"`` for the whole series, an int frame index, or a float instant in seconds; ``None`` with no time axis.
    t: Any = None
    # The Point a series is read at; ``None`` reads the domain maximum.
    at: Any = None
    # The line a profile is read along, as a geometry source, and how far off it a node still belongs; ``None`` reads the whole domain.
    along: Any = None
    within: Any = None
    # The plane of a 3D variable, bottom first; ``None`` on a 2D module.
    plane: int | None = None
    # The coupled module whose result this reads; ``None`` reads the run's own.
    module: str | None = None
    publish: str | None = None
    style: Any = None
    # ``(read, reads, params) -> lines`` drawn on the chart beside the read.
    reference: Any = None

    def __hash__(self) -> int:
        return hash((self.kind, self.variable, repr(self.t), repr(self.at),
                     repr(self.along), repr(self.within), self.plane,
                     self.module))

    def layer(self, *, style: Mapping[str, Any] | None = None) -> "Primitive":
        """Publish this field as a layer on the map, styled by ``style``."""
        return replace(self, publish="layer", style=style)

    def chart(self, *, reference: Any = None) -> "Primitive":
        """Publish this series or profile as a chart, ``reference`` lines beside it."""
        return replace(self, publish="chart", reference=reference)

    def animate(self, *, style: Mapping[str, Any] | None = None) -> "Primitive":
        """Publish this field over time as one temporal layer, styled by ``style``.

        The layer carries every frame; a picture of one instant is a render of it.
        """
        return replace(self, publish="animate", style=style)

    def station(self) -> "Primitive":
        """Publish this series at its Point as a station layer carrying it."""
        return replace(self, publish="station")

    def note(self) -> "Primitive":
        """Publish what this read measured as a note on the run's record."""
        return replace(self, publish="note")

    @property
    def key(self) -> "Primitive":
        """What one read IS: the primitive without its publishing."""
        return replace(self, publish=None, style=None, reference=None)


def field(name: str, t: Any = -1, *, plane: int | None = None,
          module: str | None = None) -> Primitive:
    """A variable over the domain at an instant, or over every instant."""
    return Primitive("field", variable=name, t=t, plane=plane, module=module)


def series(name: str, at: Any = None, *, plane: int | None = None,
           module: str | None = None) -> Primitive:
    """A variable over time: at a Point, or the domain maximum at each instant."""
    return Primitive("series", variable=name, at=at, plane=plane, module=module)


def max_over_time(name: str, *, plane: int | None = None,
                  module: str | None = None) -> Primitive:
    """A variable's envelope: the maximum every node reached, and when."""
    return Primitive("max_over_time", variable=name, plane=plane, module=module)


def profile(name: str, along: Any, t: Any = -1, *, within_m: Any = None,
            plane: int | None = None, module: str | None = None) -> Primitive:
    """A variable along a line at an instant: the mean per station of the nodes
    within ``within_m`` of the line, or of the whole domain when none is stated."""
    return Primitive("profile", variable=name, along=along, within=within_m, t=t,
                     plane=plane, module=module)


def extent(*, module: str | None = None) -> Primitive:
    """The solved domain, and how much of it held water at the end."""
    return Primitive("extent", module=module)


def mesh(*, module: str | None = None) -> Primitive:
    """The mesh the run solved on: its counts and its measured edge."""
    return Primitive("mesh", module=module)


def mass_balance(*, module: str | None = None) -> Primitive:
    """The engine's own closure, off the listing it printed."""
    return Primitive("mass_balance", module=module)



def _drawn(values: Any, wet: Any = None) -> Any:
    """``values`` with every node the run held no water on read as nothing; legend and picture stand over the same nodes."""
    import numpy as np

    if wet is None:
        return values
    return np.where(wet, values, np.nan)



# A derived dataset group's file name under the run prefix; the stem keys the layer too, so one output's group never overwrites another's.
_DATASET_SUFFIX = ".dat"


def tracer_position(token: Any) -> int | None:
    """``T<n>`` -> ``n``, the tracer's place in the deck's order; else ``None``.

    The token is what the table publishes a tracer under; the position survives a rename.
    """
    upper = str(token or "").strip().upper()
    return (int(upper[1:]) if upper.startswith("T") and upper[1:].isdigit()
            else None)


def deliver(primitive: Primitive, read: Read, solved: Any, *, caption: str,
            name: str, where: str) -> Deliverable:
    """One read, in the format QGIS opens it in.

    A field is the solved mesh with one dataset group selected (the result's own group for the whole series, a derived one for an instant or envelope); a series or profile is a chart payload; a track and station are GeoJSON.
    """
    from trid3nt_server.render import presets
    from trid3nt_server.render.formats import quantity_of

    quantity = quantity_of(caption)
    tracer = tracer_position(primitive.variable)
    if isinstance(read, Frames):
        return Deliverable(
            product=Mesh(file=read.file, group=read.group, epsg=read.epsg,
                         reference_time=read.reference_time, frames=read.frames,
                         units=read.units, bbox=solved.bbox, floor=read.floor,
                         value_range=presets.measured_range(
                             read.values, primitive.style, floor=read.floor)),
            caption=caption, style=primitive.style, tracer=tracer)
    if isinstance(read, Field):
        return Deliverable(product=_derived_group(read, solved, caption=caption,
                                                  quantity=quantity,
                                                  style=primitive.style),
                           caption=caption, style=primitive.style, tracer=tracer)
    if isinstance(read, Rasters):
        return Deliverable(product=_rasters(read, solved, caption=caption, quantity=quantity,
                                            style=primitive.style),
                           caption=caption, style=primitive.style, tracer=tracer)
    if isinstance(read, Track):
        return Deliverable(product=Vector(features=read.features),
                           caption=caption, style=primitive.style)
    if isinstance(read, Series) and primitive.publish == "station":
        return Deliverable(
            product=_station(read, caption=caption,
                             reference_time=solved.run.get("reference_time")),
            caption=caption,
            # A station is a point a reader locates the series by, not a quantity painted over the domain.
            style={"kind": "reference", "geometry": "point"})
    return Deliverable(product=Chart(payload=_chart(read, caption=caption,
                                                    where=where)),
                       caption=caption, style=primitive.style)


def _derived_group(read: Field, solved: Any, *, caption: str, quantity: str,
                   style: Any) -> Mesh:
    """A read the result file carries no group for -> one written beside it.

    Written as the SMS ASCII dataset MDAL loads; a node below the floor is written as nothing. The floor rides on the product so the matching animation masks the same.
    """
    import numpy as np

    from trid3nt_server.store import objects as storage
    from trid3nt_server.render import presets
    from trid3nt_server.render.mesh_display import write_ascii_dataset

    values = np.asarray(read.values, dtype="float64").copy()
    # A non-finite node is not a measurement: written as nothing, so a node at infinity does not own the ramp.
    values[~np.isfinite(values)] = np.nan
    if read.floor is not None:
        values[values < float(read.floor)] = np.nan
    label = f"{caption[:1].upper()}{caption[1:]}"
    group = (label if read.t is None
             else f"{label} at t = {float(read.t):g} s")
    if read.plane is not None:
        group = f"{group}, {read.plane}"
    stem = quantity if read.plane is None else f"{quantity}_{_token(read.plane)}"
    instant = "" if read.t is None else f"-t{int(read.t)}"
    basename = f"{stem}{instant}{_DATASET_SUFFIX}"
    nodes, cells = solved.display_mesh
    storage.client().put_object(
        Bucket=storage.runs_bucket(), Key=f"{solved.run_id}/{basename}",
        Body=write_ascii_dataset(values, name=group, nodes=nodes, cells=cells,
                                 t=0.0 if read.t is None else float(read.t)
                                 ).encode("utf-8"),
        ContentType="text/plain")
    return Mesh(file=solved.display_file, group=group, epsg=solved.utm_epsg,
                datasets=(basename,), bbox=solved.bbox, t=read.t, plane=read.plane,
                units=read.units, floor=read.floor,
                # Ranged over the nodes the measures were read over, so the legend describes the water, not a drying-bar film.
                value_range=presets.measured_range(_drawn(values, read.wet),
                                                   style, floor=read.floor))


def _rasters(read: Rasters, solved: Any, *, caption: str, quantity: str, style: Any) -> Raster:
    """A raster read -> its COGs under the run prefix and the Raster that carries them.

    A frame holds the map from its instant to the next frame's, the last to ``until``; an instant is the run's reference
    time plus the frame's seconds, and a run stating none refuses.
    """
    from datetime import datetime, timedelta, timezone

    import numpy as np
    from rasterio.transform import from_bounds

    from trid3nt_server.render import presets
    from trid3nt_server.render.cog import array_to_cog_bytes
    from trid3nt_server.render.formats import instant_token
    from trid3nt_server.store import objects as storage

    values = np.asarray(read.values, dtype="float64")
    if values.shape[0] != (len(read.times) or 1):
        raise ValueError(f"{values.shape[0]} frames read at {len(read.times)} instants.")
    origin = None
    if read.times:
        stated = solved.run.get("reference_time")
        if not stated:
            raise RasterUnanchored(
                f"{caption} is read at {len(read.times)} instants and the run states no reference time, so no frame "
                "has an absolute instant; state the moment the run's seconds are counted from.")
        origin = datetime.fromisoformat(str(stated).replace("Z", "+00:00"))
        if origin.tzinfo is None:
            origin = origin.replace(tzinfo=timezone.utc)

    def _at(seconds: float) -> str:
        return (origin + timedelta(seconds=float(seconds))).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    west, south, east, north = read.bounds
    transform = from_bounds(west, south, east, north, values.shape[2], values.shape[1])
    bucket = storage.runs_bucket()
    uris: list[str] = []
    for index, frame in enumerate(values):
        basename = f"{quantity}-t{instant_token(read.times[index])}.tif" if read.times else f"{quantity}.tif"
        storage.client().put_object(
            Bucket=bucket, Key=f"{solved.run_id}/{basename}",
            Body=array_to_cog_bytes(frame, transform, "EPSG:4326"), ContentType="image/tiff")
        uris.append(f"s3://{bucket}/{solved.run_id}/{basename}")
    common = dict(bbox=read.bounds, units=read.units,
                  value_range=presets.measured_range(values, style))
    if not read.times:
        return Raster(uri=uris[0], **common)
    ends = [*read.times[1:], read.times[-1] if read.until is None else max(read.until, read.times[-1])]
    return Raster(frames=tuple(RasterFrame(uri=uri, t=float(t), valid_from=_at(t), valid_to=_at(end))
                               for uri, t, end in zip(uris, read.times, ends)), **common)


def _token(text: str) -> str:
    return "_".join(str(text).strip().lower().split())


def _station(read: Series, *, caption: str, reference_time: str | None
             ) -> Vector:
    """A series at a station -> one point feature carrying the series inline.

    ``time_series_csv`` rows are ``iso,value`` counted from ``reference_time`` (the frames' origin); with none, the run's seconds.
    """
    from datetime import datetime, timedelta

    if read.lon is None or read.lat is None:
        raise OutputEmpty(f"the series {read.name!r} was read {read.at}, which is "
                          "no station to publish it at.")
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
        "properties": {"name": f"{label} {read.at}", "quantity": _token(caption),
                       "units": read.units, "variable": read.name,
                       "reference_time": reference_time,
                       "n_timesteps": len(rows),
                       "time_series_csv": "\n".join(rows) + "\n"},
    }
    return Vector(features={"type": "FeatureCollection", "features": [feature]},
                  units=read.units)


def _chart(read: Series | Profile | Spectrum | Comparison, *, caption: str,
           where: str) -> dict[str, Any]:
    """A series, profile, spectrum or comparison -> the dock's chart payload, titled by the caption; each reference line is its own named series."""
    from trid3nt_server.render.charts import build_chart_payload

    title = f"{caption[:1].upper()}{caption[1:]}"
    if isinstance(read, Comparison):
        return _comparison_chart(read, title=title, caption=caption, where=where)
    if isinstance(read, Profile):
        x, at = [float(d) for d in read.distance_m], read.along
        xfield, axis = "x_m", f"{at[:1].upper()}{at[1:]} (m)"
    elif isinstance(read, Spectrum):
        x, at = [float(f) for f in read.frequency_hz], read.at
        xfield, axis = "f_hz", "Frequency (Hz)"
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
    if isinstance(read, Profile):
        what = f"; lowest {values[low]:.3g} {read.units} at {x[low]:.0f} m"
    elif isinstance(read, Spectrum):
        what = (f"; peaks at {values[peak]:.3g} {read.units} at "
                f"{x[peak]:.3g} Hz")
    else:
        what = (f"; peaks at {values[peak]:.3g} {read.units} at "
                f"t = {x[peak]:.0f} s")
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": not read.lines},
            "data": {"values": rows},
            "encoding": encoding,
        },
        title=f"{title}, {at} - {where}",
        caption=(f"The {caption}, {at}, at each of {len(x)} "
                 + ("stations" if isinstance(read, Profile)
                    else "frequencies" if isinstance(read, Spectrum)
                    else "output times")
                 + (what if values else "") + "."
                 + "".join(f" {line.label} is drawn beside it." for line in read.lines)),
    )


def _comparison_chart(read: Comparison, *, title: str, caption: str,
                      where: str) -> dict[str, Any]:
    """Measured across, solved up, a point per site, and the line where the two agree."""
    from trid3nt_server.render.charts import build_chart_payload

    rows = [{"observed": float(row["observed"]), "simulated": float(row["simulated"]),
             "site": str(row["site"]), "sampled": str(row["sampled"])}
            for row in read.sites]
    ends = [min(min(r["observed"], r["simulated"]) for r in rows),
            max(max(r["observed"], r["simulated"]) for r in rows)]
    encoding = {"x": {"field": "observed", "type": "quantitative",
                      "title": f"Measured ({read.units})"},
                "y": {"field": "simulated", "type": "quantitative",
                      "title": f"Modelled ({read.units})"}}
    return build_chart_payload(
        vega_lite_spec={"layer": [
            {"mark": {"type": "point"}, "data": {"values": rows}, "encoding": encoding},
            {"mark": {"type": "line"},
             "data": {"values": [{"observed": e, "simulated": e} for e in ends]},
             "encoding": encoding}]},
        title=f"{title}, measured and modelled at {len(rows)} sites - {where}",
        caption=(f"The {caption} the run solved against what {len(rows)} sites "
                 "measured, each at the saved time its own sample speaks for; the "
                 "line is where the two agree"
                 + (f", and {read.measures['left_out']} sites the mesh does not "
                    "hold are left out" if read.measures.get("left_out") else "")
                 + "."))


async def publish_reads(*, engine: str, run: Mapping[str, Any],
                        outputs: Sequence[Primitive], captions: Mapping[str, str],
                        params: Mapping[str, Any],
                        solved: Callable[[str], Any],
                        table: Callable[[Callable[[str], Any]], tuple[
                            Sequence[Primitive],
                            Callable[[Sequence[LayerURI], Mapping[Primitive, str]], None]]],
                        record: Callable[[Any, str], LayerURI],
                        anchors: Sequence[Mapping[str, Any]] = ()) -> LayerURI:
    """Read what the run wrote and publish every row of the engine's module table plus the template's own reads -> the run's record.
    ``solved`` makes a module's solved run, read ONCE per module; ``table`` turns it into the module table's reads and the account of what they landed as; ``record`` is the engine's own record of the run.
    A chart's reference is a callable or another primitive read; a placed read the result lacks refuses."""
    from .module import SlotRefused

    held: dict[str, Any] = {}

    def _solved(module: str) -> Any:
        if module not in held:
            held[module] = solved(module)
        return held[module]

    rows, account = await asyncio.to_thread(table, _solved)
    if anchors:
        outputs = [_anchored(p, a) for p, a in zip(outputs, anchors)]

    published_keys = {primitive.key for primitive in outputs}
    why: dict[Primitive, str] = {}

    def _read(key: Primitive) -> Any:
        from trid3nt_server.inputs.observation import Observation, sited

        read = _solved(key.module or str(run["module"]))
        try:
            if sited(key.at) and not isinstance(key.at, Observation):
                return _compared(key, read)
            return read.body.READS[key.kind](key, read)
        except OutputEmpty as exc:
            from trid3nt_server.inputs.run_notes import journal_note
            if sited(key.at):
                # A read at a data input's site the result holds nothing for composes
                # nothing: the miss is said on the journal in the engine's own words,
                # wherever the row was heading, and the run carries on without it.
                why[key] = str(exc)
                journal_note(str(exc))
                return None
            # A row of the module's OWN table the result lacks is skipped and journalled; a PLACED read is a refusal.
            if key in published_keys:
                raise
            why[key] = str(exc)
            return None

    def _beside(primitive: Primitive) -> Primitive | None:
        reference = primitive.reference
        if not isinstance(reference, Primitive):
            return None
        return replace(reference, at=primitive.at, along=primitive.along).key

    wanted = {primitive.key for primitive in (*rows, *outputs)}
    wanted |= {_beside(p) for p in outputs if _beside(p) is not None}
    reads = await asyncio.to_thread(lambda: {key: _read(key) for key in wanted})
    for primitive in outputs:
        if primitive.reference is None:
            continue
        read = reads[primitive.key]
        if read is None:
            continue
        beside = _beside(primitive)
        if beside is None:
            lines = tuple(primitive.reference(read, reads, params))
        else:
            other = reads[beside]
            if other is None:
                continue
            caption = captions.get(primitive.variable or primitive.kind, primitive.kind)
            lines = (Line(label=f"{caption} at t = {other.measures['t']:g} s",
                          x=(other.distance_m if isinstance(other, Profile)
                             else other.times),
                          values=other.values),)
        reads[primitive.key] = replace(read, lines=lines)
    name, where = str(run["name"]), str(params.get("location") or run["name"])

    def _delivered(primitive: Primitive, caption: str) -> Any:
        return deliver(primitive, reads[primitive.key],
                       held[primitive.module or str(run["module"])],
                       caption=caption, name=name, where=where)

    for primitive in outputs:
        if primitive.publish == "note" and reads[primitive.key] is not None:
            _note(captions.get(primitive.variable or primitive.kind,
                               primitive.kind), reads[primitive.key])
    items = await asyncio.to_thread(
        lambda: [_delivered(primitive, reads[primitive.key].name.strip().lower())
                 for primitive in rows if reads[primitive.key] is not None]
        + [_delivered(primitive, captions.get(primitive.variable or primitive.kind,
                                              primitive.kind))
           for primitive in outputs
           if primitive.publish != "note" and reads[primitive.key] is not None])
    published = await publish(run_id=str(run["run_id"]), engine=engine,
                              name=name, items=items)

    account(published.layers, why)
    if not published.layers:
        # A read the result held nothing for says why in its own words; the refusal carries them, not a guess at the cause.
        reasons = "; ".join(sorted({str(reason) for reason in why.values()}))
        raise SlotRefused(
            f"the {run['module']} run published nothing, so there is nothing to "
            "paint: " + (reasons or "its result files carry no variable at all."))
    logger.info("%s outputs published run_id=%s layers=%d charts=%d", engine,
                run["run_id"], len(published.layers), len(published.charts))
    return await asyncio.to_thread(record, _solved(str(run["module"])), name)


def _compared(key: Primitive, solved: Any) -> Comparison:
    """A read at every site of a set, each through the engine's own read at one site: a site the result holds
    nothing for is left out and said, never moved. A read that checks nothing at a site has no comparison."""
    from trid3nt_server.inputs.run_notes import journal_note

    rows: list[Mapping[str, Any]] = []
    one = None
    for site in key.at:
        try:
            one = solved.body.READS[key.kind](replace(key, at=site), solved)
        except OutputEmpty as exc:
            journal_note(str(exc))
            continue
        if "simulated" not in one.measures:
            raise DeclarativeError(
                f"{key.kind}({key.variable}) at a site checks nothing against the "
                "reading there, so a set of sites has no comparison to publish.",
                error_code="OBSERVATION_UNCOMPARED")
        rows.append(dict(one.measures))
    if not rows:
        raise OutputEmpty(f"not one of the {len(key.at)} sites is inside the mesh, "
                          "so there is nothing to compare.")
    gaps = [row["simulated_minus_observed"] for row in rows]
    return Comparison(name=one.name, units=str(rows[0]["units"]), sites=tuple(rows),
                      measures={"sites": len(rows), "left_out": len(key.at) - len(rows),
                                "mean_simulated_minus_observed": round(sum(gaps) / len(gaps), 3)})


def unanchored(primitive: Primitive) -> Primitive:
    """``primitive`` with its point, line and band taken off: each names a run input, read before the publish rejoins it."""
    return replace(primitive, at=None, along=None, within=None)


def _anchored(primitive: Primitive, anchor: Mapping[str, Any]) -> Primitive:
    return replace(primitive, at=anchor["at"], along=anchor["along"],
                   within=anchor.get("within"))


def _note(caption: str, read: Any) -> None:
    """A measured read, said on the run's record in the figures it measured."""
    from trid3nt_server.inputs.run_notes import journal_note

    figures = "; ".join(f"{name} = {value}" for name, value in read.measures.items())
    journal_note(f"{caption}: {figures or 'the run measured nothing for it'}")
