"""The borrowed-provider pair round-trips, sits on the right side of the wire,
and is exported like every other message."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from trid3nt_contracts import ws
from trid3nt_contracts.export_schemas import render_schemas

_URI = (
    "crs='EPSG:4326' url='https://example.org/arcgis/rest/services/X/FeatureServer/0'"
)


def test_layer_request_round_trips_verbatim() -> None:
    p = ws.LayerRequestPayload(
        key="4f1c2a", provider="arcgisfeatureserver", uri=_URI, name="drought",
        bbox=(-114.0, 31.3, -109.0, 37.0), mode="open",
    )
    back = ws.LayerRequestPayload.model_validate(json.loads(p.model_dump_json()))
    assert back == p
    assert back.uri == _URI


def test_layer_request_refuses_an_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        ws.LayerRequestPayload(
            key="k", provider="wms", uri=_URI, name="n",
            bbox=(0.0, 0.0, 1.0, 1.0), mode="download",
        )


def test_a_global_ask_carries_no_bbox_and_still_round_trips() -> None:
    p = ws.LayerRequestPayload(
        key="k", provider="gdal", uri=_URI, name="chirps", mode="materialise",
    )
    assert p.bbox is None
    assert ws.LayerRequestPayload.model_validate(
        json.loads(p.model_dump_json())
    ) == p


def test_the_asked_grid_rides_the_request_and_is_a_positive_spacing() -> None:
    p = ws.LayerRequestPayload(
        key="k", provider="gdal", uri=_URI, name="landcover",
        bbox=(-114.0, 31.3, -109.0, 37.0), mode="materialise", resolution_m=300.0,
    )
    assert p.resolution_m == 300.0
    with pytest.raises(ValidationError):
        ws.LayerRequestPayload(
            key="k", provider="gdal", uri=_URI, name="landcover",
            bbox=(0.0, 0.0, 1.0, 1.0), mode="materialise", resolution_m=0.0,
        )


def test_layer_response_carries_the_store_uri_or_the_provider_text() -> None:
    ok = ws.LayerResponsePayload(key="k", uri="s3://bucket/user-uploads/1/k.gpkg")
    assert ws.LayerResponsePayload.model_validate(
        json.loads(ok.model_dump_json())
    ) == ok
    failed = ws.LayerResponsePayload(key="k", error="layer is not valid")
    assert failed.uri is None


def test_the_pair_sits_on_the_right_side_of_the_wire() -> None:
    assert ws.AGENT_TO_CLIENT_PAYLOADS["layer-request"] is ws.LayerRequestPayload
    assert ws.CLIENT_TO_AGENT_PAYLOADS["layer-response"] is ws.LayerResponsePayload
    assert "layer-request" not in ws.CLIENT_TO_AGENT_PAYLOADS
    assert "layer-response" not in ws.AGENT_TO_CLIENT_PAYLOADS


def test_the_pair_is_exported_with_no_edit_to_the_exporter() -> None:
    rendered = render_schemas()
    assert "ws_layer_request.json" in rendered
    assert "ws_layer_response.json" in rendered
