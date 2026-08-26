"""``restyle_layer`` - re-paint a layer that is ALREADY on the map.

A colour scale is DISPLAY STATE. Changing it recomputes nothing and changes no
number, so it is available after the fact and not only as a declaration up front.
That is what makes "rescale that to 0-30 so I can see the tail" a one-second
answer instead of a re-solve.

Two things this tool deliberately cannot do. It cannot make a layer VISIBLE - it
re-emits the display face of a layer some producer already published, and a URI
nothing published is a typed refusal. And it cannot invent a style: every preset
it accepts is declared in the style contract.

The COMPARISON mode is the reason it takes a LIST. Two layers a reader is
comparing - before and after an override, a coarse run against its refined
rematch, two calibration iterations - must be painted on ONE range or the picture
is of two different colour maps rather than of a difference.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.emission import styles
from trid3nt_server.emission.restyle import RestyleError, apply_style
from trid3nt_server.tools import register_tool

__all__ = ["restyle_layer"]

logger = logging.getLogger(
    "trid3nt_server.tools.display.restyle_layer.restyle_layer")


class RestyleArgsError(RuntimeError):
    """The restyle ask is not answerable as given."""

    error_code = "RESTYLE_ARGS_INVALID"
    retryable = False


_METADATA = AtomicToolMetadata(
    name="restyle_layer",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def restyle_layer(
    layer_ids: list[str] | str | None = None,
    preset: str | None = None,
    colormap: str | None = None,
    policy: Literal["data", "fixed"] | None = None,
    min_value: float | None = None,
    max_value: float | None = None,
    transform: Literal["linear", "log", "sqrt", "percentile"] | None = None,
    clip_low: float | None = None,
    clip_high: float | None = None,
    shared_scale: bool = False,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """RE-PAINT layers already on the map - a colour scale change, zero recompute.

    ROUTING: use this for "rescale that layer", "the plume is all one colour, stretch
    it", "put these two on the same scale so I can compare them", "show that on a log
    scale", "change the colour ramp", "clip the outliers out of the legend". Style is
    DISPLAY STATE: nothing is re-solved, no number changes, and the underlying data is
    untouched. NOT for creating a layer or making one visible - a layer must already be
    published; use the tool that produces the quantity for that.

    Args:
        layer_ids: the layer id (or ids) to re-paint. Several ids plus
            `shared_scale=True` paints them all on ONE range - the honest way to
            compare a before against an after.
        preset: a declared style preset to switch to. Omit to keep the layer's own.
        colormap: a colour ramp name (viridis, blues, reds, rdbu, ...).
        policy: `data` scales to the layer's own values; `fixed` uses min_value /
            max_value. Omit to keep the contract's declared policy.
        min_value / max_value: the fixed range, when policy is `fixed`.
        transform: linear | log | sqrt | percentile.
        clip_low / clip_high: percentile bounds under `percentile` (e.g. 2 and 98).
        shared_scale: with several layer_ids, compute ONE range across all of them
            and paint every one on it.

    Returns:
        `status="ok"` plus, per layer, the resolved preset and the LEGEND SENTENCE
        stating which scale policy ran and over what range - narrate that sentence,
        because the colours cannot say it themselves. On failure `status="error"`
        with `error_code`.
    """
    ids = _ids(layer_ids)
    if not ids:
        raise RestyleArgsError(
            "restyle_layer needs at least one layer_id - the id of a layer that is "
            "already on the map.")
    if preset is not None and not styles.known_preset(preset):
        return _error("STYLE_PRESET_UNKNOWN",
                      f"{preset!r} is not a declared style preset. Declared presets "
                      f"include: {', '.join(styles.all_presets()[:12])} ...")
    if policy == "fixed" and (min_value is None or max_value is None):
        return _error("RESTYLE_ARGS_INVALID",
                      "policy='fixed' needs both min_value and max_value - a fixed "
                      "scale IS its range.")

    resolved_uris = _resolve_uris(ids)
    missing = [lid for lid, uri in resolved_uris.items() if not uri]
    if missing:
        return _error(
            "LAYER_NOT_PUBLISHED",
            f"nothing published {missing} - restyle_layer re-paints a layer that is "
            "already on the map; it cannot create one.")

    value_range = ((float(min_value), float(max_value))
                   if min_value is not None and max_value is not None else None)
    clip = ((float(clip_low), float(clip_high))
            if clip_low is not None and clip_high is not None else None)

    shared = None
    if shared_scale and len(ids) > 1:
        shared = styles.shared_range(
            _band_range(uri, preset) for uri in resolved_uris.values())
        logger.info("restyle_layer: %d layers share the range %s", len(ids), shared)

    out: list[dict[str, Any]] = []
    for layer_id, uri in resolved_uris.items():
        try:
            style = apply_style(layer_uri=uri, layer_id=layer_id, preset=preset,
                                colormap=colormap, policy=policy,
                                value_range=value_range, transform=transform,
                                clip=clip, shared=shared)
        except RestyleError as exc:
            return _error(exc.error_code, str(exc))
        out.append({"layer_id": layer_id, "preset": style.preset,
                    "colormap": style.colormap,
                    "range": list(style.range) if style.range else None,
                    "legend": style.legend_note()})
    return {"status": "ok", "layers": out,
            "shared_scale": bool(shared is not None),
            "note": ("one range across every layer listed" if shared is not None
                     else "each layer on its own resolved range")}


def _ids(layer_ids: list[str] | str | None) -> list[str]:
    if layer_ids is None:
        return []
    if isinstance(layer_ids, str):
        return [p.strip() for p in layer_ids.split(",") if p.strip()]
    return [str(p).strip() for p in layer_ids if str(p).strip()]


def _resolve_uris(ids: list[str]) -> dict[str, str | None]:
    from trid3nt_server.emission.uri_registry import lookup_uri_for_handle

    return {layer_id: lookup_uri_for_handle(layer_id) for layer_id in ids}


def _band_range(uri: str | None, preset: str | None) -> tuple[float, float] | None:
    from trid3nt_server.emission.publish import _read_raster_bytes

    if not uri:
        return None
    return styles.band_range_reader(_read_raster_bytes(uri))(
        styles.scale_for(preset))


def _error(code: str, message: str) -> dict[str, Any]:
    logger.warning("restyle_layer %s: %s", code, message)
    return {"status": "error", "error_code": code, "error_message": message}
