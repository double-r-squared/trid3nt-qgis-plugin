"""``drop_minor_divergences`` - a flowline network with the minor paths of its divergences removed.

Reads a flowline layer carrying NHDPlus stream order and computed order; fetches nothing. A line whose computed
order (``streamcalc``) differs from its stream order (``streamorde``) lies on a minor divergence path, and every
other line is kept unchanged, so where a river splits only the main path leaves the split.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from typing import Any

from trid3nt_contracts.execution import LayerURI, layer_seed
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.derive import DeriveError

__all__ = ["dendritic", "drop_minor_divergences"]

_COLUMNS = ("nhdplusid", "hydroseq", "dnhydroseq", "streamorde", "streamcalc")
_STYLE = {"kind": "reference", "geometry": "line"}


def _frame(layer: Any) -> Any:
    """The flowlines as a GeoDataFrame, or the refusal naming the layer or the columns it lacks."""
    import geopandas as gpd

    from trid3nt_server.inputs.geometry import source_uri
    from trid3nt_server.render.uri_registry import lookup_layer_for_handle
    from trid3nt_server.tools.derive._hydrology_common import _stage_uri_local

    if isinstance(layer, str):
        found = lookup_layer_for_handle(layer)
        if found is None:
            raise DeriveError(
                "DROP_MINOR_DIVERGENCES_LAYER_UNKNOWN",
                f"the flowlines {layer!r} name no layer in this case; fetch them first and name the layer produced.")
        layer = found
    uri = str(source_uri(layer) or "").strip()
    if not uri:
        raise DeriveError("DROP_MINOR_DIVERGENCES_LAYER_UNKNOWN", "the flowlines name no file to read.")
    try:
        with tempfile.TemporaryDirectory(prefix="drop-minor-divergences-") as scratch:
            frame = gpd.read_file(_stage_uri_local(uri, scratch, "flowlines"))
    except Exception as exc:  # noqa: BLE001 - every reader fault, named by layer
        raise DeriveError("DROP_MINOR_DIVERGENCES_LAYER_UNREADABLE",
                          f"the flowlines {uri!r} could not be read ({exc}).") from exc
    missing = [column for column in _COLUMNS if column not in frame.columns]
    if missing:
        raise DeriveError(
            "DROP_MINOR_DIVERGENCES_COLUMNS_MISSING",
            f"the flowlines carry no {', '.join(missing)}; they have {', '.join(map(str, frame.columns))}. "
            "A minor path is read off NHDPlus HR's stream order and computed order, so fetch the NHDPlus flowline "
            "network.")
    return frame


def _kept(lines: Any) -> Any:
    """Which lines are on the dendritic network: stream order equal to computed order; a line stating neither refuses."""
    unstated = lines["streamorde"].isna() | lines["streamcalc"].isna()
    if unstated.any():
        first = lines.loc[unstated, "nhdplusid"].iloc[0]
        raise DeriveError(
            "DROP_MINOR_DIVERGENCES_ORDER_UNSTATED",
            f"{int(unstated.sum())} flowline(s) state no streamorde or streamcalc (nhdplusid {first:.0f} first); "
            "whether a line is on a minor path is not read off anything else.")
    return lines["streamorde"].astype(float) == lines["streamcalc"].astype(float)


def _written(lines: Any, seed: str, output_dir: str | None) -> str:
    """The layer as a FlatGeobuf -> its uri: a local path under ``output_dir``, else a key in the runs bucket."""
    filename = f"dendritic_flowlines_{seed}.fgb"
    with tempfile.TemporaryDirectory(prefix="drop-minor-divergences-") as scratch:
        local = os.path.join(scratch, filename)
        lines.to_file(local, driver="FlatGeobuf")
        if output_dir is not None:
            kept = os.path.join(output_dir, filename)
            shutil.move(local, kept)
            return kept
        from trid3nt_server.store import objects as storage

        bucket, key = storage.runs_bucket(), f"dendritic-flowlines-{seed}/{filename}"
        with open(local, "rb") as written:
            storage.client().put_object(Bucket=bucket, Key=key, Body=written.read())
    return f"s3://{bucket}/{key}"


def dendritic(flowlines: Any, *, _output_dir: str | None = None) -> LayerURI:
    """The filter itself, over a layer already resolved: what the tool runs."""
    from trid3nt_server.inputs.run_notes import journal_note

    lines = _frame(flowlines)
    keep = _kept(lines)
    if not keep.any():
        raise DeriveError("DROP_MINOR_DIVERGENCES_EMPTY",
                          f"all {len(lines)} flowlines lie on minor divergence paths; no network is left.")
    gone = set(lines.loc[~keep, "hydroseq"].dropna().astype(float))
    kept = lines[keep].copy()
    cut_off = int(kept["dnhydroseq"].dropna().astype(float).isin(gone).sum())
    seed = layer_seed()
    uri = _written(kept, seed, _output_dir)
    journal_note(f"{int((~keep).sum())} of {len(lines)} flowlines lie on minor divergence paths and are dropped"
                 + (f"; {cut_off} kept line(s) ran into a dropped one and now end there." if cut_off else "."))
    west, south, east, north = (float(v) for v in kept.total_bounds)
    return LayerURI.published(
        "dendritic-flowlines", seed=seed, name="Flowlines without minor divergences", layer_type="vector",
        uri=uri, style=_STYLE, role="primary", bbox=(west, south, east, north))


_METADATA = AtomicToolMetadata(name="drop_minor_divergences", ttl_class="live-no-cache", source_class=None)


@register_tool(_METADATA, read_only_hint=False, open_world_hint=True, destructive_hint=False, idempotent_hint=True)
async def drop_minor_divergences(flowlines: Any = None, **_extra_ignored: Any) -> LayerURI:
    """DROP the side channels of a braided or split river: keep only the main path where a stream divides.

    ROUTING: "route the river without its braided side channels", "make the stream network dendritic", "remove
    the minor divergences", "keep the main stem where the river splits". A new case layer; the input is not
    changed. Use it before routing streams through a model that needs each line to flow into one line.

    `flowlines` is a case layer id of NHDPlus HR flowlines carrying streamorde and streamcalc (the NHDPlus flowline
    network). Where a river splits, NHDPlus marks the minor branch and the lines below it until it rejoins with a
    computed order (streamcalc) that differs from the stream order (streamorde); those lines are dropped and every
    other line - tributaries included - is kept as it is.

    Returns the kept flowlines with their properties. A line stating no streamorde or streamcalc refuses, as does a
    layer every line of which lies on a minor path.
    """
    if flowlines is None:
        raise DeriveError("DROP_MINOR_DIVERGENCES_ARGS_INVALID",
                          "drop_minor_divergences needs the flowlines to filter.")
    return await asyncio.to_thread(dendritic, flowlines)
