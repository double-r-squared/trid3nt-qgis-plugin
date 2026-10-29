"""``mark_lake_outlets`` - the flowlines that leave a lake, each tagged with the waterbody it receives from.

Reads a flowline layer carrying NHDPlus topology and a waterbody layer carrying NHD identity; fetches nothing.
The tag is the property ``receives_from_waterbody`` on the line the lake's in-lake artificial path runs into.
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

__all__ = ["RECEIVES", "marked", "mark_lake_outlets"]

RECEIVES = "receives_from_waterbody"

_FLOWLINE_COLUMNS = ("hydroseq", "dnhydroseq", "wbarea_permanent_identifier")
_STYLE = {"kind": "reference", "geometry": "line"}


def _frame(layer: Any, role: str, needs: tuple[str, ...]) -> Any:
    """The features of one case layer as a GeoDataFrame, or the refusal naming the layer and the columns it lacks."""
    import geopandas as gpd

    from trid3nt_server.inputs.geometry import source_uri
    from trid3nt_server.render.uri_registry import lookup_layer_for_handle
    from trid3nt_server.tools.derive._hydrology_common import _stage_uri_local

    if isinstance(layer, str):
        found = lookup_layer_for_handle(layer)
        if found is None:
            raise DeriveError(
                "MARK_LAKE_OUTLETS_LAYER_UNKNOWN",
                f"the {role} {layer!r} names no layer in this case; fetch it first and name the layer it produced.")
        layer = found
    uri = str(source_uri(layer) or "").strip()
    if not uri:
        raise DeriveError("MARK_LAKE_OUTLETS_LAYER_UNKNOWN", f"the {role} names no file to read.")
    try:
        with tempfile.TemporaryDirectory(prefix="mark-lake-outlets-") as scratch:
            frame = gpd.read_file(_stage_uri_local(uri, scratch, role))
    except Exception as exc:  # noqa: BLE001 - every reader fault, named by layer
        raise DeriveError("MARK_LAKE_OUTLETS_LAYER_UNREADABLE", f"the {role} {uri!r} could not be read ({exc}).") from exc
    missing = [column for column in needs if column not in frame.columns]
    if missing:
        raise DeriveError(
            "MARK_LAKE_OUTLETS_COLUMNS_MISSING",
            f"the {role} carries no {', '.join(missing)}; it has {', '.join(map(str, frame.columns))}. "
            "The two-hop join reads NHDPlus HR topology, so fetch the NHDPlus flowline network and the NHD waterbodies.")
    return frame


def _outlets(flowlines: Any, waterbodies: Any) -> dict[int, str]:
    """``{row position of a flowline: the id of the waterbody it receives from}`` by the two-hop join.

    An in-lake line names its lake in ``wbarea_permanent_identifier`` and the line below it in ``dnhydroseq``; the line
    below, when it lies in no waterbody of the layer, is the lake's outlet."""
    lakes = set(waterbodies["permanent_identifier"].dropna().astype(str))
    at = {float(seq): row for row, seq in enumerate(flowlines["hydroseq"]) if seq == seq}
    named = flowlines["wbarea_permanent_identifier"].astype("object").where(
        flowlines["wbarea_permanent_identifier"].notna(), None)
    inside = {row: str(lake) for row, lake in enumerate(named) if lake is not None and str(lake) in lakes}
    receives: dict[int, str] = {}
    for row, lake in inside.items():
        below = flowlines["dnhydroseq"].iloc[row]
        out = at.get(float(below)) if below == below else None
        if out is None or out in inside:
            continue
        if receives.get(out, lake) != lake:
            raise DeriveError(
                "MARK_LAKE_OUTLETS_AMBIGUOUS",
                f"the flowline with hydroseq {flowlines['hydroseq'].iloc[out]:.0f} is the outlet of two waterbodies, "
                f"{receives[out]} and {lake}; a flowline receives from one waterbody.")
        receives[out] = lake
    return receives


def _written(lines: Any, seed: str, output_dir: str | None) -> str:
    """The layer as a FlatGeobuf -> its uri: a local path under ``output_dir``, else a key in the runs bucket."""
    filename = f"marked_lake_outlets_{seed}.fgb"
    with tempfile.TemporaryDirectory(prefix="mark-lake-outlets-") as scratch:
        local = os.path.join(scratch, filename)
        lines.to_file(local, driver="FlatGeobuf")
        if output_dir is not None:
            kept = os.path.join(output_dir, filename)
            shutil.move(local, kept)
            return kept
        from trid3nt_server.store import objects as storage

        bucket, key = storage.runs_bucket(), f"marked-lake-outlets-{seed}/{filename}"
        with open(local, "rb") as written:
            storage.client().put_object(Bucket=bucket, Key=key, Body=written.read())
    return f"s3://{bucket}/{key}"


def marked(flowlines: Any, waterbodies: Any, *, _output_dir: str | None = None) -> LayerURI:
    """The mark itself, over layers already resolved: what the tool runs."""
    from trid3nt_server.inputs.run_notes import journal_note

    lines = _frame(flowlines, "flowlines", _FLOWLINE_COLUMNS)
    lakes = _frame(waterbodies, "waterbodies", ("permanent_identifier",))
    receives = _outlets(lines, lakes)
    lines[RECEIVES] = [receives.get(row) for row in range(len(lines))]
    seed = layer_seed()
    uri = _written(lines, seed, _output_dir)
    note = (f"{len(receives)} of {len(lines)} flowlines receive from a waterbody "
            f"({len(set(receives.values()))} of {len(lakes)} waterbodies have an outlet line here).")
    journal_note(note)
    west, south, east, north = (float(v) for v in lines.total_bounds)
    return LayerURI.published(
        "marked-lake-outlets", seed=seed, name="Flowlines with their lake outlets", layer_type="vector", uri=uri,
        style=_STYLE, role="primary", bbox=(west, south, east, north))


_METADATA = AtomicToolMetadata(name="mark_lake_outlets", ttl_class="live-no-cache", source_class=None)


@register_tool(_METADATA, read_only_hint=False, open_world_hint=True, destructive_hint=False, idempotent_hint=True)
async def mark_lake_outlets(flowlines: Any = None, waterbodies: Any = None, **_extra_ignored: Any) -> LayerURI:
    """MARK which flowline each lake drains into: the line a lake's outlet runs into carries the lake's id.

    ROUTING: "which stream does this lake drain into", "link the lakes to their outlet streams", "tag the
    flowlines that start at a lake", "which waterbody does this flowline receive water from". A new case layer
    of the same flowlines; the inputs are not changed.

    `flowlines` is a case layer id of NHDPlus HR flowlines carrying hydroseq, dnhydroseq and
    wbarea_permanent_identifier (the NHDPlus flowline network). `waterbodies` is a case layer id of NHD waterbodies
    carrying permanent_identifier. Inside a lake NHDPlus runs artificial paths that name the lake; the line below
    the last of them, outside every waterbody, receives from that lake.

    Returns the flowlines with a `receives_from_waterbody` property: the lake's permanent_identifier on each outlet
    line, null elsewhere. A line that two lakes' outlets run into refuses.
    """
    if flowlines is None or waterbodies is None:
        raise DeriveError("MARK_LAKE_OUTLETS_ARGS_INVALID",
                          "mark_lake_outlets needs the flowlines and the waterbodies they run through.")
    return await asyncio.to_thread(marked, flowlines, waterbodies)
