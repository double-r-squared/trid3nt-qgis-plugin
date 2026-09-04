"""The preset family: four data KINDS, one .qml writer.

A preset is not a quantity. Four renderer shapes cover every product this
platform draws - a continuous raster ramp, a classed vector, a reference
outline, a mesh dataset group - and everything a quantity contributes (its
units, its legend title, the one scale it is read on, its ramp) is a
PARAMETER of one of those four, never a fifth preset.

Presentation is declared where the DATA is declared: a fetcher's
``source.yaml`` carries a ``style:`` row, a solved product carries its kind
and quantity. Workflow declarations carry none of it.

Three rules this module enforces, because a colour cannot state them itself:

* THE SCOPE OF ``policy: data`` IS THE RUN, NEVER THE FRAME. The range is
  computed once and every frame, plane and legend uses that one range - a
  per-frame range makes the same colour mean a different value in the next
  frame.
* A COMPARISON SET SHARES ONE RANGE, resolved together through
  :func:`shared_range`.
* THE LEGEND STATES THE POLICY AND THE RANGE, because a reader cannot tell a
  fixed domain scale from one read off this run's own data by looking.

The .qml written here is a SUBSET of QGIS's own style format - their format,
our writer, their validator. ``scripts/qml_preset_smoke.py`` loads every
document this module can produce into the installed QGIS and asserts the
POST-LOAD state (renderer type, stops read back, the range QGIS ends up
holding); a document that does not survive that read never ships.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Literal, Mapping
from xml.sax.saxutils import quoteattr

logger = logging.getLogger("trid3nt_server.emission.presets")

__all__ = [
    "DEFAULT_RAMP",
    "KINDS",
    "Preset",
    "Resolved",
    "Scale",
    "band_range_reader",
    "bare_default",
    "fixed_range_reader",
    "from_row",
    "qml",
    "ramp_stops",
    "resolve",
    "shared_range",
]

Kind = Literal["continuous", "classed", "reference", "mesh"]
Policy = Literal["data", "fixed"]
Transform = Literal["linear", "log", "sqrt", "percentile"]
Geometry = Literal["point", "line", "polygon"]

#: The whole family. A product that is none of these has no picture.
KINDS: tuple[Kind, ...] = ("continuous", "classed", "reference", "mesh")

DEFAULT_RAMP = "viridis"

#: QGIS reads a ramp as explicit stops, so the paint travels IN the document
#: and no reader needs a ramp library of its own. Five stops at t = 0, .25,
#: .5, .75, 1 of the matplotlib / ColorBrewer ramp of that name; ``hsv`` is
#: closed deliberately (a compass bearing wraps, so 0 and 360 are one colour).
_RAMP_STOPS: dict[str, tuple[str, ...]] = {
    "viridis": ("#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"),
    "magma": ("#000004", "#51127c", "#b73779", "#fc8961", "#fcfdbf"),
    "inferno": ("#000004", "#57106e", "#bc3754", "#f98e09", "#fcffa4"),
    "plasma": ("#0d0887", "#7e03a8", "#cc4778", "#f89540", "#f0f921"),
    "cividis": ("#00224e", "#434e6c", "#7d7c78", "#bcae6c", "#fee838"),
    "blues": ("#f7fbff", "#c6dbef", "#6aaed6", "#2070b4", "#08306b"),
    "greens": ("#f7fcf5", "#c7e9c0", "#73c476", "#228a44", "#00441b"),
    "oranges": ("#fff5eb", "#fdd0a2", "#fd8c3b", "#d84801", "#7f2704"),
    "reds": ("#fff5f0", "#fcbba1", "#fb694a", "#ca181d", "#67000d"),
    "rdbu": ("#67001f", "#e58368", "#f6f7f7", "#68abd0", "#053061"),
    "rdylbu": ("#a50026", "#f98e52", "#feffc0", "#8ec2dc", "#313695"),
    "rdylgn": ("#a50026", "#f98e52", "#feffbe", "#84ca66", "#006837"),
    "ylgn": ("#ffffe5", "#d9f0a3", "#77c679", "#228343", "#004529"),
    "ylgnbu": ("#ffffd9", "#c6e9b4", "#40b5c4", "#225da8", "#081d58"),
    "ylorrd": ("#ffffcc", "#fed976", "#fd8c3c", "#e2191c", "#800026"),
    "gnbu": ("#f7fcf0", "#ccebc5", "#7accc4", "#2a8bbe", "#084081"),
    "gray": ("#000000", "#404040", "#808080", "#c0c0c0", "#ffffff"),
    "hsv": ("#ff0000", "#b3ff00", "#00ffff", "#4d00ff", "#ff0000"),
}


def ramp_stops(name: str | None) -> tuple[str, ...]:
    """The hex stops of a ramp; a ``<base>_r`` name is the base reversed.

    An unknown name takes the default ramp rather than grey - a picture in the
    wrong ramp is recoverable by a restyle, a grey one reads as no data.
    """
    key = (name or "").strip().lower()
    stops = _RAMP_STOPS.get(key)
    if stops is None and key.endswith("_r"):
        base = _RAMP_STOPS.get(key[: -len("_r")])
        if base is not None:
            stops = tuple(reversed(base))
    if stops is None:
        if key:
            logger.warning("presets: unknown ramp %r -> %s", name, DEFAULT_RAMP)
        stops = _RAMP_STOPS[DEFAULT_RAMP]
    return stops


@dataclass(frozen=True, slots=True)
class Scale:
    """The one scale a quantity is read on.

    ``range`` under ``policy="data"`` is the FALLBACK the resolver uses when the
    raster's own statistics cannot be read - never a silent second opinion
    about the data.
    """

    policy: Policy = "data"
    range: tuple[float, float] | None = None
    transform: Transform = "percentile"
    clip: tuple[float, float] | None = (2.0, 98.0)

    def merged(self, other: "Scale | None") -> "Scale":
        """``other`` wins field by field - the override order, in one place."""
        if other is None:
            return self
        return Scale(
            policy=other.policy or self.policy,
            range=other.range if other.range is not None else self.range,
            transform=other.transform or self.transform,
            clip=other.clip if other.clip is not None else self.clip,
        )


@dataclass(frozen=True, slots=True)
class Preset:
    """One of four renderer shapes, parameterised by what the data is.

    ``classes`` are ``(lower, upper, "#rrggbb", label)`` breaks and are what
    makes a ``classed`` preset classed; ``geometry`` picks the symbol shape a
    vector kind needs; ``dataset_group`` names the MDAL group a mesh preset
    paints, which is how QGIS binds it (an index does not survive the load).
    """

    kind: Kind = "continuous"
    ramp: str = DEFAULT_RAMP
    units: str | None = None
    label: str | None = None
    scale: Scale = Scale()
    classes: tuple[tuple[float, float, str, str], ...] = ()
    geometry: Geometry = "point"
    color: str = "#3b7dd8"
    dataset_group: str | None = None

    def titled(self, label: str | None, units: str | None) -> "Preset":
        """The same preset speaking for a particular quantity."""
        return replace(self, label=label or self.label, units=units or self.units)


_BARE: dict[Kind, Preset] = {
    "continuous": Preset(kind="continuous"),
    "classed": Preset(kind="classed", geometry="polygon"),
    # A reference layer is an outline a reader locates themselves by - a gauge,
    # a flowline, an administrative edge - so it is drawn, not measured, and it
    # declares no scale.
    "reference": Preset(kind="reference", geometry="point",
                        scale=Scale(policy="fixed", range=None)),
    "mesh": Preset(kind="mesh"),
}


def bare_default(kind: str | None) -> Preset:
    """The kind's own preset, for a declaration that named no parameters."""
    return _BARE.get(str(kind or "continuous").strip().lower(), _BARE["continuous"])


def _scale_from(doc: Any, fallback: Scale) -> Scale:
    if not isinstance(doc, Mapping):
        return fallback
    rng, clip = doc.get("range"), doc.get("clip")
    return Scale(
        policy=str(doc.get("policy") or fallback.policy),
        range=(float(rng[0]), float(rng[1])) if rng else fallback.range,
        transform=str(doc.get("transform") or fallback.transform),
        clip=(float(clip[0]), float(clip[1])) if clip else fallback.clip,
    )


def from_row(row: Any) -> Preset:
    """A declared ``style:`` row as a preset. An absent row is the bare default.

    The row is DATA about the data - which of the four shapes draws it, and the
    parameters that shape needs - so it lives beside the source or product that
    produces the layer, never in a table somewhere else.
    """
    if not isinstance(row, Mapping):
        return bare_default("continuous")
    base = bare_default(row.get("kind"))
    return Preset(
        kind=base.kind,
        ramp=str(row.get("ramp") or base.ramp),
        units=row.get("units") if row.get("units") is not None else base.units,
        label=row.get("label") if row.get("label") is not None else base.label,
        scale=_scale_from(row.get("scale"), base.scale),
        classes=tuple((float(c[0]), float(c[1]), str(c[2]), str(c[3]))
                      for c in (row.get("classes") or ())),
        geometry=str(row.get("geometry") or base.geometry),  # type: ignore[arg-type]
        color=str(row.get("color") or base.color),
        dataset_group=row.get("dataset_group") or base.dataset_group,
    )


# --------------------------------------------------------------------------- #
# resolution: a preset plus a layer's own values -> one concrete scale
# --------------------------------------------------------------------------- #

#: Where the concrete range came from. The legend says which, because the
#: colours cannot.
FIXED, FROM_DATA, SHARED, FALLBACK = "fixed", "data", "shared", "fallback"

_SAFE_RANGE = (0.0, 1.0)


@dataclass(frozen=True, slots=True)
class Resolved:
    """One preset resolved against a layer: the concrete range and its source."""

    preset: Preset
    range: tuple[float, float] | None
    source: str

    @property
    def kind(self) -> Kind:
        return self.preset.kind

    def legend_note(self) -> str:
        """What the legend says about its own scale - the policy AND the range."""
        if self.preset.classes:
            return f"{len(self.preset.classes)} declared classes"
        if self.range is None:
            return "no scale (drawn, not measured)"
        lo, hi = self.range
        units = f" {self.preset.units}" if self.preset.units else ""
        how = {
            FIXED: "fixed domain scale",
            FROM_DATA: f"scaled to this run ({_percentile_phrase(self.preset.scale)})",
            # A shared range was HANDED IN, so this preset's clip did not
            # produce it and naming those percentiles would describe a read
            # nobody made.
            SHARED: "one range shared across the compared set",
            FALLBACK: "declared fallback range (the run's own values were unreadable)",
        }.get(self.source, self.source)
        return f"{how}: {lo:g} to {hi:g}{units}"

    def qml(self) -> str:
        return qml(self)


def _percentile_phrase(scale: Scale) -> str:
    if scale.transform == "percentile" and scale.clip:
        return f"p{scale.clip[0]:g}-p{scale.clip[1]:g}"
    return scale.transform


def needs_run_range(preset: Preset, override: Scale | None = None) -> bool:
    """Does this preset's declared policy want the layer's own range read?"""
    if preset.kind == "reference" or preset.classes:
        return False
    scale = preset.scale.merged(override)
    return scale.policy != "fixed" or scale.range is None


def resolve(
    preset: Preset,
    *,
    read_range: Callable[[Scale], tuple[float, float] | None] | None = None,
    override: Scale | None = None,
    shared: tuple[float, float] | None = None,
) -> Resolved:
    """Resolve a preset to a concrete scale. The ONE place that decision is made.

    ``read_range`` is asked for the layer's own range ONLY when the resolved
    policy is ``data`` and no ``shared`` range was handed in, so a fixed-scale
    preset never pays for a band read and a comparison set never re-reads a
    range it was already given.

    Override order, later wins: the declared row -> ``override`` (a restyle
    ask) -> ``shared``.
    """
    scale = preset.scale.merged(override)
    spec = replace(preset, scale=scale)
    if spec.kind == "reference" or spec.classes:
        return Resolved(spec, None if spec.kind == "reference" else scale.range, FIXED)
    if shared is not None:
        return Resolved(spec, shared, SHARED)
    if scale.policy == "fixed" and scale.range is not None:
        return Resolved(spec, scale.range, FIXED)
    found = read_range(scale) if read_range is not None else None
    if found is not None:
        return Resolved(spec, _widen(found), FROM_DATA)
    return Resolved(spec, scale.range or _SAFE_RANGE, FALLBACK)


def _widen(rng: tuple[float, float]) -> tuple[float, float]:
    """A zero-width range is not a scale - renderers reject it, so pad it."""
    lo, hi = float(rng[0]), float(rng[1])
    if hi > lo:
        return (lo, hi)
    pad = max(abs(lo) * 0.01, 1e-6)
    return (lo - pad, hi + pad)


def shared_range(
    ranges: Iterable[tuple[float, float] | None],
) -> tuple[float, float] | None:
    """ONE range spanning a compared set - before/after, coarse-versus-refined.

    Layers a reader compares must be painted on one scale or the comparison is
    a picture of two colour maps. ``None`` when nothing in the set had a range.
    """
    found = [r for r in ranges if r is not None]
    if not found:
        return None
    return _widen((min(r[0] for r in found), max(r[1] for r in found)))


def band_range_reader(
    raster_bytes: bytes | None,
) -> Callable[[Scale], tuple[float, float] | None]:
    """A ``read_range`` that reads band 1 of an in-hand COG at the scale's clip.

    ``None`` for missing bytes, missing deps, an unreadable raster or a band
    with no finite values - the resolver then falls back to the declared range
    and SAYS SO on the legend.
    """

    def _read(scale: Scale) -> tuple[float, float] | None:
        if not raster_bytes:
            return None
        try:
            import numpy as np
            from rasterio.io import MemoryFile
        except Exception as exc:  # noqa: BLE001 - deps absent: declared fallback
            logger.debug("band-stats deps unavailable (%s: %s)", type(exc).__name__, exc)
            return None
        lo_pct, hi_pct = scale.clip or (2.0, 98.0)
        try:
            with MemoryFile(raster_bytes) as mem, mem.open() as src:
                band = src.read(1, masked=True)
                arr = np.ma.filled(band.astype("float64"), np.nan)
                finite = arr[np.isfinite(arr)]
                if finite.size == 0:
                    return None
                lo = float(np.percentile(finite, lo_pct))
                hi = float(np.percentile(finite, hi_pct))
        except Exception as exc:  # noqa: BLE001 - unreadable / not a raster
            logger.debug("band-stats read failed (%s: %s)", type(exc).__name__, exc)
            return None
        if lo != lo or hi != hi:
            return None
        return (lo, hi)

    return _read


def fixed_range_reader(
    p_lo: float | None, p_hi: float | None
) -> Callable[[Scale], tuple[float, float] | None]:
    """A ``read_range`` fed by percentiles a WORKER already computed."""

    def _read(_scale: Scale) -> tuple[float, float] | None:
        if p_lo is None or p_hi is None:
            return None
        lo, hi = float(p_lo), float(p_hi)
        return None if (lo != lo or hi != hi) else (lo, hi)

    return _read


# --------------------------------------------------------------------------- #
# the writer: THEIR format, OUR subset
# --------------------------------------------------------------------------- #

_HEADER = ("<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
           '<qgis version="3.40.6" styleCategories="Symbology">\n')
_FOOTER = "</qgis>\n"

#: QGIS symbol class per geometry. A symbol whose class does not match the
#: layer's geometry loads and then draws nothing, so the writer picks it.
_SYMBOL: dict[Geometry, tuple[str, str, str]] = {
    "point": ("marker", "SimpleMarker", "color"),
    "line": ("line", "SimpleLine", "line_color"),
    "polygon": ("fill", "SimpleFill", "color"),
}


def _rgba(color: str, alpha: int = 255) -> str:
    value = color.lstrip("#")
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return f"{r},{g},{b},{alpha}"


def _tick_label(value: float, units: str | None) -> str:
    return f"{value:g} {units}" if units else f"{value:g}"


def _ramp_items(resolved: Resolved, indent: str) -> str:
    lo, hi = resolved.range or _SAFE_RANGE
    stops = ramp_stops(resolved.preset.ramp)
    span = hi - lo
    rows = []
    for i, color in enumerate(stops):
        value = lo + span * (i / (len(stops) - 1))
        rows.append(
            f'{indent}<item value="{value:.10g}" color="{color}" alpha="255" '
            f"label={quoteattr(_tick_label(value, resolved.preset.units))}/>")
    return "\n".join(rows)


def _colorrampshader(resolved: Resolved, indent: str) -> str:
    lo, hi = resolved.range or _SAFE_RANGE
    return (
        f'{indent}<colorrampshader colorRampType="INTERPOLATED" classificationMode="1"'
        f' clip="0" minimumValue="{lo:.10g}" maximumValue="{hi:.10g}"'
        f' labelPrecision="4">\n'
        f"{_ramp_items(resolved, indent + '  ')}\n"
        f"{indent}</colorrampshader>")


def _continuous_qml(resolved: Resolved) -> str:
    lo, hi = resolved.range or _SAFE_RANGE
    return (
        "  <pipe>\n"
        f'    <rasterrenderer type="singlebandpseudocolor" band="1" opacity="1"'
        f' alphaBand="-1" classificationMin="{lo:.10g}" classificationMax="{hi:.10g}"'
        f' nodataColor="">\n'
        "      <rastershader>\n"
        f"{_colorrampshader(resolved, '        ')}\n"
        "      </rastershader>\n"
        "    </rasterrenderer>\n"
        "  </pipe>\n")


def _symbol(name: str, geometry: Geometry, color: str, indent: str) -> str:
    stype, sclass, color_key = _SYMBOL[geometry]
    extra = ('        <Option name="outline_color" type="QString" '
             f'value="{_rgba("#232323")}"/>\n'
             '        <Option name="outline_width" type="QString" value="0.26"/>\n'
             if geometry != "line" else "")
    size = ('        <Option name="size" type="QString" value="2.4"/>\n'
            '        <Option name="size_unit" type="QString" value="MM"/>\n'
            '        <Option name="name" type="QString" value="circle"/>\n'
            if geometry == "point" else "")
    width = ('        <Option name="line_width" type="QString" value="0.66"/>\n'
             '        <Option name="line_width_unit" type="QString" value="MM"/>\n'
             if geometry == "line" else "")
    return (
        f'{indent}<symbol name="{name}" type="{stype}" alpha="1" force_rhr="0"'
        ' clip_to_extent="1" frame_rate="10" is_animated="0">\n'
        f'{indent}  <layer class="{sclass}" enabled="1" locked="0" pass="0">\n'
        f'{indent}    <Option type="Map">\n'
        f'{indent}      <Option name="{color_key}" type="QString" value="{_rgba(color)}"/>\n'
        f"{extra}{size}{width}"
        f'{indent}      <Option name="style" type="QString" value="solid"/>\n'
        f"{indent}    </Option>\n"
        f"{indent}  </layer>\n"
        f"{indent}</symbol>")


def _classed_qml(resolved: Resolved, attribute: str) -> str:
    classes = resolved.preset.classes
    ranges = "\n".join(
        f'      <range lower="{lo:.10f}" upper="{hi:.10f}" symbol="{i}"'
        f" label={quoteattr(label)} render=\"true\"/>"
        for i, (lo, hi, _color, label) in enumerate(classes))
    symbols = "\n".join(
        _symbol(str(i), resolved.preset.geometry, color, "      ")
        for i, (_lo, _hi, color, _label) in enumerate(classes))
    return (
        f'  <renderer-v2 type="graduatedSymbol" attr={quoteattr(attribute)}'
        ' graduatedMethod="GraduatedColor" symbollevels="0" forceraster="0"'
        ' enableorderby="0" referencescale="-1">\n'
        "    <ranges>\n"
        f"{ranges}\n"
        "    </ranges>\n"
        "    <symbols>\n"
        f"{symbols}\n"
        "    </symbols>\n"
        "  </renderer-v2>\n")


def _reference_qml(resolved: Resolved) -> str:
    return (
        '  <renderer-v2 type="singleSymbol" symbollevels="0" forceraster="0"'
        ' enableorderby="0" referencescale="-1">\n'
        "    <symbols>\n"
        f"{_symbol('0', resolved.preset.geometry, resolved.preset.color, '      ')}\n"
        "    </symbols>\n"
        "  </renderer-v2>\n")


def _mesh_qml(resolved: Resolved) -> str:
    lo, hi = resolved.range or _SAFE_RANGE
    group = resolved.preset.dataset_group or ""
    # QGIS remaps the settings' group index through this name on load; without
    # the row the whole mesh-renderer-settings block is silently dropped.
    binding = (f'  <name-to-global-index global-index="0" name={quoteattr(group)}/>\n'
               if group else "")
    return (
        "  <mesh-renderer-settings>\n"
        '    <active-dataset-group scalar="0" vector="-1"/>\n'
        f'    <scalar-settings group="0" min-val="{lo:.10g}" max-val="{hi:.10g}"'
        ' opacity="1" interpolation-method="no-resampling">\n'
        f"{_colorrampshader(resolved, '      ')}\n"
        "    </scalar-settings>\n"
        "  </mesh-renderer-settings>\n"
        f"{binding}")


def qml(resolved: Resolved, *, attribute: str = "value") -> str:
    """The resolved preset as a QGIS ``.qml`` document.

    ``attribute`` is the vector field a ``classed`` preset classifies on; the
    other three kinds ignore it.
    """
    body = {
        "continuous": lambda: _continuous_qml(resolved),
        "classed": lambda: _classed_qml(resolved, attribute),
        "reference": lambda: _reference_qml(resolved),
        "mesh": lambda: _mesh_qml(resolved),
    }[resolved.preset.kind]()
    return _HEADER + body + _FOOTER
