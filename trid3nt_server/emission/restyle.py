"""Re-paint an ALREADY-PUBLISHED layer. Display state, never solve state.

A scale is a property of the picture, not of the physics: changing it costs zero
recompute and changes no number. So the policy is available both up front (the
contract default, a template's ``.style()`` modifier, a declared param knob) and
AFTERWARDS, here.

What this is NOT: a way to put a layer on the map. It re-emits the DISPLAY FACE of
a layer that is already there, and refuses a URI nothing published - otherwise it
would be the deleted ``publish_layer`` tool wearing a new name, and the model
would be back to deciding what the user gets to see.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.styles import ScaleSpec

from . import styles
from .styles import ResolvedStyle

logger = logging.getLogger("trid3nt_server.emission.restyle")

__all__ = ["RestyleError", "apply_style", "scale_override"]


class RestyleError(RuntimeError):
    """The layer cannot be re-painted, and saying why beats painting nothing."""

    error_code = "RESTYLE_FAILED"

    def __init__(self, message: str, *, error_code: str = "RESTYLE_FAILED") -> None:
        super().__init__(message)
        self.error_code = error_code


def scale_override(*, policy: str | None = None,
                   value_range: tuple[float, float] | None = None,
                   transform: str | None = None,
                   clip: tuple[float, float] | None = None) -> ScaleSpec | None:
    """The caller's scale ASK as the contract's own vocabulary, or ``None``.

    ``None`` when nothing was overridden, which is what lets the resolver fall
    straight through to the contract default rather than merging an empty spec
    that would quietly re-assert defaults over the preset's own declaration.
    """
    if policy is None and value_range is None and transform is None and clip is None:
        return None
    return ScaleSpec(
        policy=policy or "data",
        range=tuple(value_range) if value_range else None,   # type: ignore[arg-type]
        transform=transform or "linear",
        clip=tuple(clip) if clip else None,                  # type: ignore[arg-type]
    )


def apply_style(*, layer_uri: str, layer_id: str,
                preset: str | None = None,
                colormap: str | None = None,
                policy: str | None = None,
                value_range: tuple[float, float] | None = None,
                transform: str | None = None,
                clip: tuple[float, float] | None = None,
                shared: tuple[float, float] | None = None,
                fallback_preset: str | None = None) -> ResolvedStyle:
    """Re-emit one published layer's display face under an overridden scale.

    Returns the RESOLVED style, so the caller can say on the legend which policy
    ran and over what range - the resolver's answer, not the caller's ask.
    """
    if not layer_uri or not layer_id:
        raise RestyleError("a restyle needs both the layer's uri and its layer id.")
    name = preset or fallback_preset
    if preset is not None and not styles.known_preset(preset):
        raise RestyleError(
            f"{preset!r} is not a declared style preset. Declared presets live in "
            "the style contract (trid3nt_contracts/styles.yaml).",
            error_code="STYLE_PRESET_UNKNOWN")
    if colormap:
        # A colormap with no other ask is still an ask, so it rides as an override
        # the resolver merges - but the CONTRACT owns which colormap a quantity
        # gets, and a per-layer swap is deliberately the ad hoc case.
        logger.info("restyle %s: colormap override %r", layer_id, colormap)

    from .publish import publish_layer

    override = scale_override(policy=policy, value_range=value_range,
                              transform=transform, clip=clip)
    publish_layer(layer_uri=layer_uri, layer_id=layer_id, style_preset=name,
                  scale=override, shared_range=shared)
    resolved = styles.resolve_style(
        name, override=override, shared=shared,
        read_range=_range_reader(layer_uri))
    if colormap:
        resolved = _with_colormap(resolved, colormap)
    logger.info("restyle %s -> %s", layer_id, resolved.legend_note())
    return resolved


def _range_reader(layer_uri: str) -> Any:
    from .publish import _read_raster_bytes

    def _read(scale: ScaleSpec) -> tuple[float, float] | None:
        return styles.band_range_reader(_read_raster_bytes(layer_uri))(scale)

    return _read


def _with_colormap(resolved: ResolvedStyle, colormap: str) -> ResolvedStyle:
    import dataclasses

    return dataclasses.replace(resolved, colormap=colormap)
