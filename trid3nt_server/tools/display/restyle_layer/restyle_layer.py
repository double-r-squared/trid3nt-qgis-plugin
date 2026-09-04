"""``restyle_layer`` - THE presentation surface for layers already on the map.

Presentation is DISPLAY STATE. Changing a ramp, a title, a scale, or whether a
layer is on the canvas at all recomputes nothing and moves no number, so all of
it is available after the fact rather than only as a declaration up front. That
is what makes "rescale that to 0-30 so I can see the tail" a one-second answer
instead of a re-solve.

Two things this tool deliberately cannot do. It cannot CREATE a layer - emission
is automatic, and a uri nothing published is a typed refusal. And it cannot
invent a renderer: every layer is drawn by one of four preset shapes, and a
restyle parameterises one of those four.

The COMPARISON mode is the reason it takes a LIST. Two layers a reader is
comparing - before and after an override, a coarse run against its refined
rematch, two calibration iterations - must be painted on ONE range or the
picture is of two different colour maps rather than of a difference.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.emission import presets
from trid3nt_server.emission.restyle import RestyleError, apply_style, set_hidden
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
    hide: bool | None = None,
    kind: Literal["continuous", "classed", "reference", "mesh"] | None = None,
    ramp: str | None = None,
    title: str | None = None,
    units: str | None = None,
    policy: Literal["data", "fixed"] | None = None,
    min_value: float | None = None,
    max_value: float | None = None,
    transform: Literal["linear", "log", "sqrt", "percentile"] | None = None,
    clip_low: float | None = None,
    clip_high: float | None = None,
    shared_scale: bool = False,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """RE-PAINT, RETITLE or HIDE layers already on the map - zero recompute.

    ROUTING: use this for "rescale that layer", "the plume is all one colour, stretch
    it", "put these two on the same scale so I can compare them", "show that on a log
    scale", "change the colour ramp", "clip the outliers out of the legend", "call
    that layer X", "hide that layer", "bring it back". Presentation is DISPLAY STATE:
    nothing is re-solved, no number changes, and the data is untouched. NOT for
    creating a layer - a layer must already be published; use the tool that produces
    the quantity for that.

    Args:
        layer_ids: the layer id (or ids) to restyle. Several ids plus
            `shared_scale=True` paints them all on ONE range - the honest way to
            compare a before against an after.
        hide: `True` takes the layers off the canvas, `False` puts them back.
            When set, nothing else is applied.
        kind: re-shape how the layer is drawn - `continuous` (a ramp over a
            range), `classed` (declared breaks), `reference` (drawn, not
            measured), `mesh` (an MDAL dataset group). Omit to keep its own.
        ramp: a colour ramp name (viridis, blues, reds, rdbu, ...).
        title: the legend title the layer is read under.
        units: the units the legend annotates the numbers with.
        policy: `data` scales to the layer's own values; `fixed` uses min_value /
            max_value. Omit to keep the declared policy.
        min_value / max_value: the fixed range, when policy is `fixed`.
        transform: linear | log | sqrt | percentile.
        clip_low / clip_high: percentile bounds under `percentile` (e.g. 2 and 98).
        shared_scale: with several layer_ids, compute ONE range across all of them
            and paint every one on it.

    Returns:
        `status="ok"` plus, per layer, the resolved shape and the LEGEND SENTENCE
        stating which scale policy ran and over what range - narrate that sentence,
        because the colours cannot say it themselves. On failure `status="error"`
        with `error_code`.
    """
    ids = _ids(layer_ids)
    if not ids:
        raise RestyleArgsError(
            "restyle_layer needs at least one layer_id - the id of a layer that is "
            "already on the map.")
    if kind is not None and kind not in presets.KINDS:
        return _error("STYLE_KIND_UNKNOWN",
                      f"{kind!r} is not one of the four preset kinds "
                      f"{list(presets.KINDS)}.")
    if policy == "fixed" and (min_value is None or max_value is None):
        return _error("RESTYLE_ARGS_INVALID",
                      "policy='fixed' needs both min_value and max_value - a fixed "
                      "scale IS its range.")

    if hide is not None:
        missing = [lid for lid in ids if not await set_hidden(lid, hide)]
        if missing:
            return _error(
                "LAYER_NOT_PUBLISHED",
                f"nothing published {missing} - restyle_layer changes a layer that "
                "is already on the map; it cannot create one.")
        return {"status": "ok", "layers": [{"layer_id": lid, "hidden": hide}
                                           for lid in ids],
                "note": ("removed from the canvas" if hide
                         else "restored to the canvas")}

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
        shared = presets.shared_range(
            _band_range(uri) for uri in resolved_uris.values())
        logger.info("restyle_layer: %d layers share the range %s", len(ids), shared)

    out: list[dict[str, Any]] = []
    for layer_id, uri in resolved_uris.items():
        try:
            style = apply_style(layer_uri=uri, layer_id=layer_id,
                                declared=_declared_row(uri), kind=kind, ramp=ramp,
                                label=title, units=units, policy=policy,
                                value_range=value_range, transform=transform,
                                clip=clip, shared=shared)
        except RestyleError as exc:
            return _error(exc.error_code, str(exc))
        out.append({"layer_id": layer_id, "kind": style.preset.kind,
                    "ramp": style.preset.ramp,
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


def _declared_row(uri: str | None) -> dict[str, Any] | None:
    """The row this layer was published under, so a restyle overrides rather
    than replaces it."""
    from trid3nt_server.emission.publish import pop_legend_for_uri

    legend = pop_legend_for_uri(uri or "")
    if legend is None:
        return None
    ramp = legend.colormap if isinstance(legend.colormap, str) else None
    return {"kind": legend.kind, "ramp": ramp or presets.DEFAULT_RAMP,
            "units": legend.units, "label": legend.label}


def _band_range(uri: str | None) -> tuple[float, float] | None:
    from trid3nt_server.emission.publish import _read_raster_bytes

    if not uri:
        return None
    return presets.band_range_reader(_read_raster_bytes(uri))(presets.Scale())


def _error(code: str, message: str) -> dict[str, Any]:
    logger.warning("restyle_layer %s: %s", code, message)
    return {"status": "error", "error_code": code, "error_message": message}
