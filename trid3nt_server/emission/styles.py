"""THE style resolver. One home, one answer.

The contract (``trid3nt_contracts/styles.yaml``) says what a preset IS and which
preset a quantity gets. This module is the only thing that turns that declaration
plus a raster into a concrete scale a renderer can paint - and into the sentence
the legend says about it.

Three rules the contract states and this module enforces:

* THE SCOPE OF ``policy: data`` IS THE RUN, NEVER THE FRAME. The range is computed
  once and every frame, every plane and every legend uses that one range. A
  per-frame range makes the same colour mean a different value in the next frame,
  which is a dishonest picture, not a nicer one.
* A COMPARISON SET SHARES ONE RANGE. Before/after, coarse-versus-refined and
  calibration iterations are read AGAINST each other, so they are resolved
  together through :func:`shared_range`.
* THE LEGEND STATES THE POLICY AND THE RANGE. A reader cannot tell a fixed
  domain scale from a range read off this run's own data by looking at the
  colours, so the caption says which it was.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence
from urllib.parse import quote

from trid3nt_contracts.styles import PresetSpec, ScaleSpec, default_scale, preset, presets

logger = logging.getLogger("trid3nt_server.emission.styles")

__all__ = [
    "NEUTRAL_FALLBACK_PRESET",
    "QUANTITY_FAMILY_SEP",
    "ResolvedStyle",
    "all_presets",
    "band_range_reader",
    "fixed_range_reader",
    "known_preset",
    "needs_run_range",
    "scale_for",
    "legend_classes",
    "preset_label",
    "preset_units",
    "quantity_axis",
    "resolve_style",
    "resolve_style_preset",
    "reset_unknown_quantity_fallback_count",
    "shared_range",
    "unknown_quantity_fallback_count",
]

#: The honest ramp for a quantity nobody registered: the field's OWN range under
#: a single-hue colormap. Deliberately NOT a physical band - an unknown quantity
#: has no known physical range.
NEUTRAL_FALLBACK_PRESET = "neutral_ramp"

#: Separator for a per-instance quantity (``plume_concentration__tce``). Siblings
#: differ only so their temporal groups stay distinct, never in their colormap, so
#: the family's registered preset styles all of them from ONE contract row.
QUANTITY_FAMILY_SEP = "__"

_UNKNOWN_FALLBACK_COUNT = 0


# --------------------------------------------------------------------------- #
# quantity -> preset
# --------------------------------------------------------------------------- #

def resolve_style_preset(quantity: str) -> tuple[str, bool]:
    """``(preset, is_fallback)`` for a published quantity, from the contract.

    A producer declares the QUANTITY it computed; this is where that becomes a
    style. An unregistered quantity gets the neutral ramp, a WARNING and a
    process counter - never a silent, physically wrong colormap.
    """
    global _UNKNOWN_FALLBACK_COUNT
    from trid3nt_contracts.styles import quantity_defaults

    table = quantity_defaults()
    key = (quantity or "").strip().lower()
    for candidate in _spellings(key):
        found = table.get(candidate)
        if found is not None:
            return found, False
    if QUANTITY_FAMILY_SEP in key:
        family = key.split(QUANTITY_FAMILY_SEP, 1)[0]
        for candidate in _spellings(family):
            found = table.get(candidate)
            if found is not None:
                return found, False
    _UNKNOWN_FALLBACK_COUNT += 1
    logger.warning(
        "styles: unknown quantity %r -> neutral ramp (%s); add a quantity_defaults "
        "row to the style contract to give it a physical colormap (fallback #%d)",
        quantity, NEUTRAL_FALLBACK_PRESET, _UNKNOWN_FALLBACK_COUNT)
    return NEUTRAL_FALLBACK_PRESET, True


def _spellings(key: str) -> tuple[str, ...]:
    """The one quantity, however the producer punctuated it.

    Two key spaces grew up side by side - the emit-on-solve seam writes
    ``flood_depth`` and the output registry writes ``flood-depth`` - and they name
    the same physical field. Accepting both is one rule; duplicating every row in
    the contract would be the mirror all over again.
    """
    return (key, key.replace("-", "_"), key.replace("_", "-"))


def unknown_quantity_fallback_count() -> int:
    return _UNKNOWN_FALLBACK_COUNT


def reset_unknown_quantity_fallback_count() -> None:
    """Reset the fallback counter (test hook only)."""
    global _UNKNOWN_FALLBACK_COUNT
    _UNKNOWN_FALLBACK_COUNT = 0


def needs_run_range(name: str | None, override: ScaleSpec | None = None) -> bool:
    """Does this preset's declared policy want the RUN's own range read?

    The seam asks before touching a COG: a fixed-scale preset never needs the
    read, and a data-policy one needs it exactly once per run.
    """
    spec = preset(name)
    scale = (spec.scale if spec is not None else default_scale()).merged(override)
    return scale.policy != "fixed" or scale.range is None


def scale_for(name: str | None, override: ScaleSpec | None = None) -> ScaleSpec:
    """The resolved scale DECLARATION (before any raster is read)."""
    spec = preset(name)
    return (spec.scale if spec is not None else default_scale()).merged(override)


def known_preset(name: str | None) -> bool:
    return preset(name) is not None


def legend_classes(name: str | None) -> tuple[tuple[float, float, str, str], ...]:
    """The declared class breaks of a categorical preset - the legend AND the paint."""
    spec = preset(name)
    return spec.classes if spec is not None else ()


# --------------------------------------------------------------------------- #
# the resolved style
# --------------------------------------------------------------------------- #

#: Where the concrete range came from. The legend says which, because the colours
#: cannot.
FIXED, FROM_DATA, SHARED, FALLBACK = "fixed", "data", "shared", "fallback"

_SAFE_RANGE = (0.0, 1.0)


@dataclass(frozen=True, slots=True)
class ResolvedStyle:
    """One preset, resolved against a raster: the concrete range and its provenance."""

    preset: str
    kind: str
    colormap: str
    scale: ScaleSpec
    range: tuple[float, float] | None
    source: str
    units: str | None = None
    label: str | None = None
    classes: tuple[tuple[float, float, str, str], ...] = ()

    def style_params(self) -> str:
        """``&rescale=..&colormap_name=..`` (or an interval ``&colormap=``).

        Never empty for a continuous raster: an empty string hands the renderer a
        stock per-tile autoscale, which is the per-frame range this whole module
        exists to prevent.
        """
        if self.classes:
            intervals = [[[lo, hi], _hex_to_rgba(color)]
                         for lo, hi, color, _label in self.classes]
            return "&colormap=" + quote(json.dumps(intervals), safe="")
        if self.kind == "mesh":
            return ""
        lo, hi = self.range or _SAFE_RANGE
        return f"&rescale={lo:g},{hi:g}&colormap_name={self.colormap}"

    def legend_note(self) -> str:
        """What the legend says about its own scale - the policy AND the range."""
        if self.classes:
            return f"{len(self.classes)} declared classes"
        lo, hi = self.range or _SAFE_RANGE
        units = f" {self.units}" if self.units else ""
        how = {
            FIXED: "fixed domain scale",
            FROM_DATA: f"scaled to this run ({_percentile_phrase(self.scale)})",
            SHARED: f"one range shared across the compared set "
                    f"({_percentile_phrase(self.scale)})",
            FALLBACK: "declared fallback range (the run's own values were unreadable)",
        }.get(self.source, self.source)
        return f"{how}: {lo:g} to {hi:g}{units}"


def _percentile_phrase(scale: ScaleSpec) -> str:
    if scale.transform == "percentile" and scale.clip:
        return f"p{scale.clip[0]:g}-p{scale.clip[1]:g}"
    return scale.transform


def resolve_style(
    preset_name: str | None,
    *,
    read_range: Callable[[ScaleSpec], tuple[float, float] | None] | None = None,
    override: ScaleSpec | None = None,
    shared: tuple[float, float] | None = None,
) -> ResolvedStyle:
    """Resolve a preset to a concrete scale. The ONE place that decision is made.

    ``read_range`` is asked for the run's own range ONLY when the resolved policy
    is ``data`` and no ``shared`` range was handed in - so a fixed-scale preset
    never pays for a band read, and a comparison set never re-reads a range it
    was already given. It receives the resolved scale so it knows which
    percentiles to clip at.

    Override order, later wins: contract default -> preset -> ``override`` (the
    ``.style()`` modifier, a param knob, or ``restyle_layer``'s arguments) ->
    ``shared``.
    """
    spec = preset(preset_name) or _default_spec(preset_name)
    scale = spec.scale.merged(override)
    if spec.classes:
        return _resolved(spec, scale, None, FIXED)
    if shared is not None:
        return _resolved(spec, scale, shared, SHARED)
    if scale.policy == "fixed" and scale.range is not None:
        return _resolved(spec, scale, scale.range, FIXED)
    found = read_range(scale) if read_range is not None else None
    if found is not None:
        return _resolved(spec, scale, _widen(found), FROM_DATA)
    return _resolved(spec, scale, scale.range or _SAFE_RANGE, FALLBACK)


def _default_spec(name: str | None) -> PresetSpec:
    return PresetSpec(name=(name or "").strip().lower() or NEUTRAL_FALLBACK_PRESET,
                      scale=default_scale())


def _resolved(spec: PresetSpec, scale: ScaleSpec,
              rng: tuple[float, float] | None, source: str) -> ResolvedStyle:
    return ResolvedStyle(preset=spec.name, kind=spec.kind, colormap=spec.colormap,
                         scale=scale, range=rng, source=source, units=spec.units,
                         label=spec.label, classes=spec.classes)


def _widen(rng: tuple[float, float]) -> tuple[float, float]:
    """A zero-width range is not a scale - renderers reject it, so pad it."""
    lo, hi = float(rng[0]), float(rng[1])
    if hi > lo:
        return (lo, hi)
    pad = max(abs(lo) * 0.01, 1e-6)
    return (lo - pad, hi + pad)


def shared_range(ranges: Iterable[tuple[float, float] | None]) -> tuple[float, float] | None:
    """ONE range spanning a compared set - before/after, coarse-versus-refined.

    Layers a reader compares must be painted on one scale or the comparison is a
    picture of two different colour maps. ``None`` when nothing in the set had a
    range to contribute.
    """
    found = [r for r in ranges if r is not None]
    if not found:
        return None
    return _widen((min(r[0] for r in found), max(r[1] for r in found)))


def band_range_reader(raster_bytes: bytes | None) -> Callable[[ScaleSpec],
                                                              tuple[float, float] | None]:
    """A ``read_range`` that reads band 1 of an in-hand COG at the scale's clip.

    Returns ``None`` for missing bytes, missing deps, an unreadable raster or a
    band with no finite values - the resolver then falls back to the preset's
    declared range and SAYS SO on the legend.
    """
    def _read(scale: ScaleSpec) -> tuple[float, float] | None:
        if not raster_bytes:
            return None
        try:
            import numpy as np
            from rasterio.io import MemoryFile
        except Exception as exc:  # noqa: BLE001 - deps unavailable: declared fallback
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
        if lo != lo or hi != hi:      # NaN guard
            return None
        return (lo, hi)

    return _read


def fixed_range_reader(p_lo: float | None,
                       p_hi: float | None) -> Callable[[ScaleSpec],
                                                       tuple[float, float] | None]:
    """A ``read_range`` fed by percentiles a WORKER already computed.

    The register-only fast path: the manifest carries the band stats, so the agent
    resolves the same scale without downloading the COG again.
    """
    def _read(_scale: ScaleSpec) -> tuple[float, float] | None:
        if p_lo is None or p_hi is None:
            return None
        lo, hi = float(p_lo), float(p_hi)
        return None if (lo != lo or hi != hi) else (lo, hi)

    return _read


def _hex_to_rgba(color: str) -> list[int]:
    value = color.lstrip("#")
    return [int(value[i:i + 2], 16) for i in (0, 2, 4)] + [255]


def preset_units(name: str | None) -> str | None:
    """The declared units of a preset - the ONE vocabulary charts and legends share."""
    spec = preset(name)
    return spec.units if spec is not None else None


def preset_label(name: str | None) -> str | None:
    """The declared legend label of a preset - charts title their axes from this."""
    spec = preset(name)
    return spec.label if spec is not None else None


def quantity_axis(quantity: str) -> tuple[str | None, str | None]:
    """``(label, units)`` for a published quantity - a chart's axis, from the contract.

    A chart of a quantity and the layer of that same quantity must not disagree
    about what it is called or what it is measured in, so both read this.
    ``(None, None)`` for a quantity the contract has no row for - the neutral
    ramp's own label is a placeholder, and handing it back as an axis title would
    label the chart "Value".
    """
    name, is_fallback = resolve_style_preset(quantity)
    if is_fallback:
        return (None, None)
    return preset_label(name), preset_units(name)


def all_presets() -> Sequence[str]:
    return sorted(presets())
