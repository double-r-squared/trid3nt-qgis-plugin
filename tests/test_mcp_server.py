"""MCP stdio server: the tool registry as an MCP tool surface.

Offline: an in-process ``mcp.Client`` speaking to the real server object over
memory streams (the SDK's own harness), driving a stub registry. One test loads
the REAL registry to prove the advertised tool count matches it. No network, no
daemon, no stdio subprocess.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import mcp
import pytest

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import RegisteredTool
from trid3nt_server.mcp_server import build_server, dispatch_tool, load_registry


# --------------------------------------------------------------------------- #
# Stub registry
# --------------------------------------------------------------------------- #

#: Thread the sync stub tool observed itself running on -- proves the offload.
OBSERVED_THREADS: dict[str, int] = {}


def stub_fetch_dem(bbox: str, resolution_m: int | None = None) -> dict[str, Any]:
    """Fetch a DEM for an area of interest.

    Params:
        bbox: minx,miny,maxx,maxy in EPSG:4326.
        resolution_m: target ground resolution in metres.
    """
    OBSERVED_THREADS["stub_fetch_dem"] = threading.get_ident()
    return {
        "status": "ok",
        "layer_id": "dem-1",
        "uri": "s3://runs/dem-1.tif",
        "resolution_m": resolution_m or 10,
        "bbox": bbox,
    }


def stub_fetch_huge(bbox: str) -> dict[str, Any]:
    """Fetch a very large vector layer."""
    OBSERVED_THREADS["stub_fetch_huge"] = threading.get_ident()
    # A megabyte-class inline payload of the kind that must never reach the wire.
    return {
        "status": "ok",
        "layer_id": "huge-1",
        "uri": "s3://runs/huge-1.geojson",
        "feature_count": 40000,
        "features": [{"geometry": "x" * 200, "id": i} for i in range(4000)],
    }


def stub_fetch_gated(bbox: str) -> dict[str, Any]:
    """Fetch a layer whose payload is estimated before the call."""
    OBSERVED_THREADS["stub_fetch_gated"] = threading.get_ident()
    return {"status": "ok", "layer_id": "gated-1"}


def estimate_payload_mb(**kwargs: Any) -> float:
    """Declared payload estimator for the gated stub -- well over the hard cap."""
    return 900.0


def _entry(fn: Any, *, estimator: str | None = None) -> RegisteredTool:
    return RegisteredTool(
        metadata=AtomicToolMetadata(
            name=fn.__name__,
            ttl_class="dynamic-1h",
            source_class="stub",
            payload_mb_estimator_name=estimator,
        ),
        fn=fn,
        module=__name__,
    )


@pytest.fixture()
def stub_registry(monkeypatch):
    registry = {
        "stub_fetch_dem": _entry(stub_fetch_dem),
        "stub_fetch_huge": _entry(stub_fetch_huge),
        "stub_fetch_gated": _entry(stub_fetch_gated, estimator="estimate_payload_mb"),
    }
    # dispatch + the payload gate both read the live registry by name.
    monkeypatch.setattr("trid3nt_server.tools.TOOL_REGISTRY", registry)
    OBSERVED_THREADS.clear()
    return registry


def _payload(result: Any) -> dict[str, Any]:
    """The envelope carried by a CallToolResult's single text block."""
    assert result.content, "tool result carried no content"
    return json.loads(result.content[0].text)


# --------------------------------------------------------------------------- #
# Startup / listing
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_server_starts_and_lists_stub_tools(stub_registry):
    server = build_server(stub_registry)
    async with mcp.Client(server) as client:
        listed = await client.list_tools()
    names = sorted(t.name for t in listed.tools)
    assert names == ["stub_fetch_dem", "stub_fetch_gated", "stub_fetch_huge"]

    dem = next(t for t in listed.tools if t.name == "stub_fetch_dem")
    assert dem.input_schema["type"] == "object"
    assert set(dem.input_schema["properties"]) == {"bbox", "resolution_m"}
    assert dem.input_schema["required"] == ["bbox"]
    assert dem.input_schema["properties"]["resolution_m"]["type"] == "integer"
    assert dem.description.startswith("Fetch a DEM")


@pytest.mark.asyncio
async def test_tool_count_matches_the_real_registry():
    registry = load_registry()
    assert len(registry) > 100  # the real surface, not a stub
    server = build_server(registry)
    async with mcp.Client(server) as client:
        listed = await client.list_tools()
    assert len(listed.tools) == len(registry)
    assert sorted(t.name for t in listed.tools) == sorted(registry)


def test_real_fetcher_schema_is_the_registry_schema():
    registry = load_registry()
    server = build_server(registry)
    tool = server._tool_manager.get_tool("fetch_dem")
    # The registry's own synthesis, not a naive signature read: bbox is a
    # list[float] on the tool and lands as a typed array in the schema.
    assert tool.parameters["properties"]["bbox"] == {
        "type": "array",
        "items": {"type": "number"},
    }
    assert tool.parameters["required"] == ["bbox"]
    # Description budget matches the adapters' cap.
    assert len(tool.description) <= 1000


# --------------------------------------------------------------------------- #
# Invocation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fetcher_round_trip_through_the_client(stub_registry):
    server = build_server(stub_registry)
    async with mcp.Client(server) as client:
        result = await client.call_tool(
            "stub_fetch_dem", {"bbox": "-83.0,42.2,-82.9,42.3", "resolution_m": 30}
        )
    assert result.is_error is False
    envelope = _payload(result)
    assert envelope["tool"] == "stub_fetch_dem"
    assert envelope["status"] == "ok"
    assert envelope["result"]["uri"] == "s3://runs/dem-1.tif"
    assert envelope["result"]["resolution_m"] == 30


@pytest.mark.asyncio
async def test_sync_tool_runs_off_the_event_loop(stub_registry):
    server = build_server(stub_registry)
    async with mcp.Client(server) as client:
        await client.call_tool("stub_fetch_dem", {"bbox": "0,0,1,1"})
    assert OBSERVED_THREADS["stub_fetch_dem"] != threading.get_ident()


@pytest.mark.asyncio
async def test_unknown_tool_is_reported_not_crashed(stub_registry):
    envelope = await dispatch_tool("no_such_tool", {})
    assert envelope["error_code"] == "TOOL_NOT_FOUND"
    assert envelope["retryable"] is False


@pytest.mark.asyncio
async def test_tool_exception_becomes_a_typed_envelope(stub_registry, monkeypatch):
    def _boom(bbox: str) -> dict[str, Any]:
        """Always fails."""
        raise ValueError("upstream said no")

    monkeypatch.setitem(stub_registry, "stub_fetch_dem", _entry(_boom))
    envelope = await dispatch_tool("stub_fetch_dem", {"bbox": "0,0,1,1"})
    assert envelope["status"] == "error"
    assert envelope["error_type"] == "ValueError"


# --------------------------------------------------------------------------- #
# Gate + payload discipline (AUTO mode)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gated_tool_refuses_honestly_in_auto_mode(stub_registry):
    server = build_server(stub_registry)
    async with mcp.Client(server) as client:
        result = await client.call_tool("stub_fetch_gated", {"bbox": "0,0,10,10"})
    envelope = _payload(result)
    assert envelope["error_code"] == "PAYLOAD_HARD_CAP"
    assert envelope["estimated_mb"] == 900.0
    assert envelope["hard_cap_mb"] == 250.0
    assert "narrow" in envelope["message"]
    # The refusal is a refusal: the tool body never ran.
    assert "stub_fetch_gated" not in OBSERVED_THREADS


@pytest.mark.asyncio
async def test_payload_between_thresholds_proceeds_with_a_label(
    stub_registry, monkeypatch
):
    monkeypatch.setenv("TRID3NT_PAYLOAD_WARNING_MB", "1")
    monkeypatch.setenv("TRID3NT_PAYLOAD_HARDCAP_MB", "5000")
    envelope = await dispatch_tool("stub_fetch_gated", {"bbox": "0,0,10,10"})
    assert envelope["status"] == "ok"
    assert envelope["payload_warning_mb"] == 900.0
    assert "stub_fetch_gated" in OBSERVED_THREADS  # it did run


@pytest.mark.asyncio
async def test_oversized_result_returns_uri_not_bytes(stub_registry):
    server = build_server(stub_registry)
    async with mcp.Client(server) as client:
        result = await client.call_tool("stub_fetch_huge", {"bbox": "0,0,1,1"})
    text = result.content[0].text
    raw_len = len(json.dumps(stub_fetch_huge("0,0,1,1")))
    assert raw_len > 800_000  # the tool really did return a huge payload
    assert len(text) < 20_000  # ... and the wire did not carry it
    envelope = _payload(result)
    assert envelope["result"]["uri"] == "s3://runs/huge-1.geojson"
    assert envelope["result"]["feature_count"] == 40000
    # The 4000 inline features collapsed to a shape marker, not geometry.
    features = envelope["result"]["features"]
    assert len(features) < 10
    assert all(isinstance(f, str) for f in features)
