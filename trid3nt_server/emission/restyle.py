"""THE presentation surface. Re-paint, retitle, or un-emit an existing layer.

Emission is automatic - a produced layer appears - so nothing here puts a layer
on the map. What lives here is everything a reader may want to change about one
AFTERWARDS: its ramp, its title, its scale, which of the four preset shapes
draws it, and whether it is on the canvas at all. All of it is DISPLAY STATE:
changing any of it recomputes nothing and moves no number.

``hide=True`` is the un-emit and ``hide=False`` puts the layer back. Every
restyle is journaled with the sentence the legend ends up saying, because the
colours cannot state which policy produced them.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_server.workflows.runtime.journal import journal_note

from . import presets
from .presets import Resolved, Scale

logger = logging.getLogger("trid3nt_server.emission.restyle")

__all__ = ["RestyleError", "apply_style", "scale_override", "set_hidden"]


class RestyleError(RuntimeError):
    """The layer cannot be re-painted, and saying why beats painting nothing."""

    error_code = "RESTYLE_FAILED"

    def __init__(self, message: str, *, error_code: str = "RESTYLE_FAILED") -> None:
        super().__init__(message)
        self.error_code = error_code


def scale_override(*, policy: str | None = None,
                   value_range: tuple[float, float] | None = None,
                   transform: str | None = None,
                   clip: tuple[float, float] | None = None) -> Scale | None:
    """The caller's scale ASK in the preset vocabulary, or ``None``.

    ``None`` when nothing was overridden, which is what lets the resolver fall
    straight through to the declared row rather than merging an empty spec that
    would quietly re-assert defaults over it.
    """
    if policy is None and value_range is None and transform is None and clip is None:
        return None
    return Scale(
        policy=policy or "data",
        range=tuple(value_range) if value_range else None,   # type: ignore[arg-type]
        transform=transform or "linear",
        clip=tuple(clip) if clip else None,                  # type: ignore[arg-type]
    )


def restyled_row(declared: dict[str, Any] | None, *,
                 kind: str | None = None,
                 ramp: str | None = None,
                 label: str | None = None,
                 units: str | None = None) -> dict[str, Any]:
    """The declared row with the caller's presentation asks laid over it.

    A kind override re-shapes the layer (a classed field read as a ramp, say);
    everything else parameterises the shape it already has.
    """
    row = dict(declared or {})
    if kind is not None:
        if kind not in presets.KINDS:
            raise RestyleError(
                f"{kind!r} is not one of the four preset kinds "
                f"{list(presets.KINDS)}.", error_code="STYLE_KIND_UNKNOWN")
        row["kind"] = kind
    if ramp is not None:
        row["ramp"] = ramp
    if label is not None:
        row["label"] = label
    if units is not None:
        row["units"] = units
    return row


async def set_hidden(layer_id: str, hidden: bool) -> bool:
    """Take a layer off the canvas, or put it back. The un-emit.

    Returns False when no emitter is bound or the session never loaded that
    layer - a restyle of a layer nobody published is a refusal, not a no-op.
    """
    from .pipeline_emitter import current_emitter

    emitter = current_emitter()
    if emitter is None:
        return False
    return await emitter.set_layer_visible(layer_id, not hidden)


def apply_style(*, layer_uri: str, layer_id: str,
                declared: dict[str, Any] | None = None,
                kind: str | None = None,
                ramp: str | None = None,
                label: str | None = None,
                units: str | None = None,
                policy: str | None = None,
                value_range: tuple[float, float] | None = None,
                transform: str | None = None,
                clip: tuple[float, float] | None = None,
                shared: tuple[float, float] | None = None) -> Resolved:
    """Re-emit one published layer's display face under the caller's asks.

    Returns the RESOLVED preset, so the caller can say on the legend which
    policy ran and over what range - the resolver's answer, not the ask.
    """
    if not layer_uri or not layer_id:
        raise RestyleError("a restyle needs both the layer's uri and its layer id.")

    from .publish import publish_layer

    row = restyled_row(declared, kind=kind, ramp=ramp, label=label, units=units)
    override = scale_override(policy=policy, value_range=value_range,
                              transform=transform, clip=clip)
    publish_layer(layer_uri=layer_uri, layer_id=layer_id, style=row,
                  scale=override, shared_range=shared)
    resolved = presets.resolve(presets.from_row(row), override=override,
                               shared=shared, read_range=_range_reader(layer_uri))
    journal_note(f"restyle {layer_id}: {resolved.legend_note()}")
    return resolved


def _range_reader(layer_uri: str) -> Any:
    from .publish import _read_raster_bytes

    def _read(scale: Scale) -> tuple[float, float] | None:
        return presets.band_range_reader(_read_raster_bytes(layer_uri))(scale)

    return _read
