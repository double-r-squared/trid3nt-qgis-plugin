"""The borrowed-provider executor: the row's uri, the ask, and the two modes.

The session is a double here - a loop on its own thread plus an emitter that
answers the layer-request - because the executor body is sync and off-loaded, and
that is the only shape in which it may wait."""

from __future__ import annotations

import asyncio
import io
import json
import threading

import pyogrio
import pytest

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_contracts.ws import LayerResponsePayload
from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import (
    RouterInputError,
    RouterUpstreamError,
)
from trid3nt_server.tools.fetchers._router.executors import qgis_provider

_SERVICE = "https://example.test/arcgis/rest/services/Drought/FeatureServer"
_BBOX = (-114.0, 31.3, -109.0, 37.0)


def _open_spec() -> SourceSpec:
    return SourceSpec.model_validate({
        "name": "fetch_demo_overlay",
        "source_class": "demo_overlay",
        "shape": "record",
        "endpoints": {
            "current": {"url": f"{_SERVICE}/3"},
            "archive": {"url": f"{_SERVICE}/2"},
        },
        "params": {
            "bbox": {"type": "bbox", "required": True},
            "date": {"type": "date_compact", "required": False, "default": None},
        },
        "ingest": {
            "access": "qgis_provider",
            "qgis_provider": {
                "provider": "arcgisfeatureserver",
                "uri": "crs='EPSG:4326' url='{url}'",
                "mode": "open",
            },
            "endpoint_select": {
                "param": "date", "absent": "current", "present": "archive",
            },
            "where_clauses": [{"template": "period='{date}'", "require": ["date"]}],
        },
        "normalize": {"crs": "EPSG:4326", "units": "dm_class"},
        "output": {"layer_type": "record", "ext": "json"},
        "cache": {"ttl_class": "live-no-cache"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 0.01},
    })


def _materialise_spec() -> SourceSpec:
    return SourceSpec.model_validate({
        "name": "fetch_demo_rowed",
        "source_class": "demo_rowed",
        "shape": "vector-fgb",
        "endpoints": {"data": {"url": f"{_SERVICE}/0"}},
        "params": {"bbox": {"type": "bbox", "required": True}},
        "ingest": {
            "access": "qgis_provider",
            "qgis_provider": {
                "provider": "arcgisfeatureserver",
                "uri": "crs='EPSG:4326' url='{url}'",
                "mode": "materialise",
            },
            "column_map": {"dm": {"from": "dm", "kind": "int", "default": 0}},
        },
        "normalize": {"crs": "EPSG:4326", "units": "dm_class"},
        "output": {
            "layer_type": "vector", "ext": "fgb",
            "style": {"kind": "reference", "geometry": "polygon"},
        },
        "cache": {"ttl_class": "semi-static-7d"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 0.01},
    })


class _Session:
    """A bound session: the loop the executor drives its ask onto, and the
    answer the plugin would send back."""

    def __init__(self, loop, answer) -> None:
        self.session_id = "session-1"
        self._bound_loop = loop
        self._answer = answer
        self.asked: list = []

    async def send_envelope(self, envelope_type: str, payload) -> None:
        self.asked.append(payload)
        answer = self._answer(payload)
        if answer is not None:
            qgis_provider.resolve_pending_layer(self.session_id, answer)


@pytest.fixture()
def loop_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def _bind(monkeypatch, session) -> None:
    from trid3nt_server.render import pipeline_emitter

    monkeypatch.setattr(pipeline_emitter, "current_emitter", lambda: session)


def _gpkg_bytes() -> bytes:
    import geopandas as gpd
    from shapely.geometry import box

    frame = gpd.GeoDataFrame(
        {"dm": [4]}, geometry=[box(-113.0, 32.0, -112.0, 33.0)], crs="EPSG:4326"
    )
    buffer = io.BytesIO()
    frame.to_file(buffer, driver="GPKG", layer="exported")
    return buffer.getvalue()


def test_a_qgis_provider_row_routes_to_this_executor() -> None:
    assert router.select_executor(_open_spec()) is qgis_provider.execute
    assert router.select_executor(_materialise_spec()) is qgis_provider.execute


def test_the_uri_is_the_row_template_over_the_selected_endpoint() -> None:
    spec = _open_spec()
    assert qgis_provider.build_uri(spec, {"bbox": _BBOX, "date": None}) == (
        f"crs='EPSG:4326' url='{_SERVICE}/3'"
    )
    assert qgis_provider.build_uri(spec, {"bbox": _BBOX, "date": "20210803"}) == (
        f"crs='EPSG:4326' url='{_SERVICE}/2' sql=period='20210803'"
    )


def test_a_row_that_states_no_provider_block_refuses() -> None:
    spec = _open_spec()
    spec.ingest["qgis_provider"] = {"provider": "wms", "mode": "open"}
    with pytest.raises(RouterInputError, match="uri"):
        qgis_provider.execute(spec, {"bbox": _BBOX})


def test_mode_open_on_a_layer_row_refuses_rather_than_publish_a_packet_row() -> None:
    spec = _materialise_spec()
    spec.ingest["qgis_provider"]["mode"] = "open"
    with pytest.raises(RouterInputError, match="shape: record"):
        qgis_provider.execute(spec, {"bbox": _BBOX})


def test_no_session_bound_refuses_by_name(monkeypatch) -> None:
    from trid3nt_server.server.processing import SessionUnavailableError
    from trid3nt_server.render import pipeline_emitter

    monkeypatch.setattr(pipeline_emitter, "current_emitter", lambda: None)
    with pytest.raises(SessionUnavailableError, match="live QGIS session"):
        qgis_provider.execute(_open_spec(), {"bbox": _BBOX, "date": None})


def test_mode_open_returns_the_record_and_never_the_store(monkeypatch, loop_thread):
    spec = _open_spec()
    session = _Session(
        loop_thread, lambda p: LayerResponsePayload(key=p.key)
    )
    _bind(monkeypatch, session)
    record = json.loads(qgis_provider.execute(spec, {"bbox": _BBOX, "date": None}))
    assert record["opened_in_session"] is True
    assert record["provider"] == "arcgisfeatureserver"
    assert session.asked[0].mode == "open"
    assert session.asked[0].name == "demo_overlay"


def test_a_public_row_names_no_credential(monkeypatch, loop_thread):
    session = _Session(loop_thread, lambda p: LayerResponsePayload(key=p.key))
    _bind(monkeypatch, session)
    qgis_provider.execute(_open_spec(), {"bbox": _BBOX, "date": None})
    assert session.asked[0].credential is None


def test_a_keyed_row_carries_its_credential_NAME_and_no_key(
    monkeypatch, loop_thread
):
    """The row states which key it needs; the session resolves it in its own
    auth store, so nothing the daemon sends could carry a key."""
    spec = _open_spec()
    spec.ingest["qgis_provider"]["credential"] = "example_token"
    session = _Session(loop_thread, lambda p: LayerResponsePayload(key=p.key))
    _bind(monkeypatch, session)
    qgis_provider.execute(spec, {"bbox": _BBOX, "date": None})
    assert session.asked[0].credential == "example_token"
    assert "authcfg" not in session.asked[0].uri


def test_the_provider_error_is_carried_verbatim(monkeypatch, loop_thread):
    session = _Session(
        loop_thread,
        lambda p: LayerResponsePayload(key=p.key, error="Layer is not valid"),
    )
    _bind(monkeypatch, session)
    with pytest.raises(RouterUpstreamError, match="Layer is not valid"):
        qgis_provider.execute(_open_spec(), {"bbox": _BBOX, "date": None})


def test_mode_materialise_lands_the_export_under_the_row_key(
    monkeypatch, loop_thread, fake_s3
):
    from trid3nt_server.tools.cache import read_through
    from trid3nt_server.tools.fetchers._router.router import synthesize_metadata
    from trid3nt_server.tools.fetchers._router.spec import record_shape

    spec = _materialise_spec()
    params = {"bbox": _BBOX}
    staged = "user-uploads/01J/demo.gpkg"
    fake_s3.store[staged] = _gpkg_bytes()
    session = _Session(
        loop_thread,
        lambda p: LayerResponsePayload(key=p.key, uri=f"s3://staging/{staged}"),
    )
    _bind(monkeypatch, session)

    def _fetch() -> bytes:
        return qgis_provider.execute(spec, params)

    first = read_through(
        metadata=synthesize_metadata(spec), params=params, ext="fgb",
        fetch_fn=_fetch, record_shape=record_shape(spec),
    )
    assert not first.hit
    assert first.uri is not None and first.uri.endswith(".fgb")
    frame = pyogrio.read_dataframe(io.BytesIO(first.data))
    assert list(frame["dm"]) == [4]

    # The cache HIT never asks QGIS again: the second read serves the stored
    # object and the session sees one request, not two.
    second = read_through(
        metadata=synthesize_metadata(spec), params=params, ext="fgb",
        fetch_fn=_fetch, record_shape=record_shape(spec),
    )
    assert second.hit
    assert len(session.asked) == 1
    assert session.asked[0].key in first.uri


def test_the_drought_monitor_is_a_provider_row_in_mode_open() -> None:
    from pathlib import Path

    from trid3nt_server.tools import fetchers
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

    specs = compose_specs_from_tree(Path(fetchers.__file__).resolve().parent)
    spec = specs["fetch_us_drought_monitor"]
    assert spec.ingest["access"] == qgis_provider.ACCESS
    assert spec.ingest["qgis_provider"]["mode"] == "open"
    assert spec.hooks is None or not spec.hooks.model_dump(exclude_none=True)
    assert spec.output.layer_type == "record"
    assert TOOL_REGISTRY["fetch_us_drought_monitor"].metadata.cacheable is False
    current = qgis_provider.build_uri(spec, {"bbox": _BBOX, "date": None})
    assert current.endswith("US_Drought_Intensity_v1/FeatureServer/3'")
    archive = qgis_provider.build_uri(spec, {"bbox": _BBOX, "date": "20210803"})
    assert archive.endswith("FeatureServer/2' sql=period='20210803'")


def test_the_drought_monitor_refuses_by_name_with_no_session(monkeypatch) -> None:
    from trid3nt_server.render import pipeline_emitter
    from trid3nt_server.server.processing import SessionUnavailableError
    from trid3nt_server.tools import TOOL_REGISTRY

    monkeypatch.setattr(pipeline_emitter, "current_emitter", lambda: None)
    with pytest.raises(SessionUnavailableError, match="us_drought_monitor"):
        TOOL_REGISTRY["fetch_us_drought_monitor"].fn(bbox=_BBOX)
