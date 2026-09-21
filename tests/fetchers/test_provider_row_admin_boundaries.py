"""The administrative-boundary row: a qgis_provider row in mode materialise.

One endpoint per admin level TIGERweb serves, the asked name applied as the
provider's subset, and the session's export landing under the row-shaped cache
key - the polygon reaches the store, so it can stand as a run's extent. The
recorded attributes are a live TIGERweb answer for Lee County FL."""

from __future__ import annotations

import io
from pathlib import Path

import pyogrio
import pytest

from trid3nt_contracts.ws import LayerResponsePayload
from trid3nt_server.server.processing import SessionUnavailableError
from trid3nt_server.tools import TOOL_REGISTRY, fetchers
from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import RouterError
from trid3nt_server.tools.fetchers._router.executors import qgis_provider
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

from .test_router_qgis_provider import _Session, _bind, loop_thread  # noqa: F401

_NAME = "fetch_administrative_boundaries"
_BBOX = (-82.2, 26.3, -81.5, 26.8)          # Lee County FL
_SERVICE = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb"
_RECORDED = {"GEOID": "12071", "NAME": "Lee County"}

_SPEC = compose_specs_from_tree(Path(fetchers.__file__).resolve().parent)[_NAME]


def _export_bytes() -> bytes:
    """The GeoPackage the session uploads after windowing the provider layer."""
    import geopandas as gpd
    from shapely.geometry import box

    frame = gpd.GeoDataFrame(
        {k: [v] for k, v in _RECORDED.items()},
        geometry=[box(*_BBOX)],
        crs="EPSG:4326",
    )
    buffer = io.BytesIO()
    frame.to_file(buffer, driver="GPKG", layer="exported")
    return buffer.getvalue()


def test_the_row_is_a_provider_row_in_mode_materialise() -> None:
    assert _SPEC.ingest["access"] == qgis_provider.ACCESS
    assert _SPEC.ingest["qgis_provider"]["mode"] == "materialise"
    assert _SPEC.hooks is None or not _SPEC.hooks.model_dump(exclude_none=True)
    assert _SPEC.output.layer_type == "vector" and _SPEC.output.ext == "fgb"
    assert TOOL_REGISTRY[_NAME].metadata.cacheable is True


@pytest.mark.parametrize(
    ("level", "tail"),
    [
        ("state", "State_County/MapServer/0"),
        ("county", "State_County/MapServer/1"),
        ("place", "Places_CouSub_ConCity_SubMCD/MapServer/4"),
        ("cdp", "Places_CouSub_ConCity_SubMCD/MapServer/5"),
        ("zcta", "PUMA_TAD_TAZ_UGA_ZCTA/MapServer/1"),
    ],
)
def test_the_level_picks_the_tigerweb_sub_layer(level: str, tail: str) -> None:
    uri = qgis_provider.build_uri(_SPEC, {"level": level, "bbox": _BBOX, "name": None})
    assert uri == f"crs='EPSG:4326' url='{_SERVICE}/{tail}'"


def test_a_named_region_is_the_providers_subset() -> None:
    uri = qgis_provider.build_uri(
        _SPEC, {"level": "county", "bbox": _BBOX, "name": _RECORDED["NAME"]}
    )
    assert uri.endswith("State_County/MapServer/1' sql=NAME='Lee County'")


def test_a_level_the_row_does_not_serve_is_a_typed_error() -> None:
    with pytest.raises(RouterError) as ei:
        router.validate_params(_SPEC, {"level": "galaxy", "bbox": list(_BBOX)})
    assert ei.value.error_code == "ADMIN_BOUNDARY_LEVEL_INVALID"


def test_it_refuses_by_name_with_no_session(monkeypatch) -> None:
    from trid3nt_server.render import pipeline_emitter

    monkeypatch.setattr(pipeline_emitter, "current_emitter", lambda: None)
    with pytest.raises(SessionUnavailableError, match="administrative_boundaries"):
        TOOL_REGISTRY[_NAME].fn(level="county", bbox=_BBOX)


def test_the_export_lands_under_the_row_shaped_key(monkeypatch, loop_thread, fake_s3):
    from trid3nt_server.tools.cache import read_through
    from trid3nt_server.tools.fetchers._router.router import synthesize_metadata
    from trid3nt_server.tools.fetchers._router.spec import record_shape

    params = router.validate_params(
        _SPEC, {"level": "county", "bbox": list(_BBOX), "name": _RECORDED["NAME"]}
    )
    staged = "user-uploads/01J/admin.gpkg"
    fake_s3.store[staged] = _export_bytes()
    session = _Session(
        loop_thread,
        lambda p: LayerResponsePayload(key=p.key, uri=f"s3://staging/{staged}"),
    )
    _bind(monkeypatch, session)

    landed = read_through(
        metadata=synthesize_metadata(_SPEC), params=params, ext="fgb",
        fetch_fn=lambda: qgis_provider.execute(_SPEC, params),
        record_shape=record_shape(_SPEC),
    )
    assert not landed.hit
    assert landed.uri is not None and session.asked[0].key in landed.uri
    assert session.asked[0].mode == "materialise"
    assert session.asked[0].uri.endswith("sql=NAME='Lee County'")
    frame = pyogrio.read_dataframe(io.BytesIO(landed.data))
    assert list(frame["GEOID"]) == [_RECORDED["GEOID"]]
    assert list(frame["NAME"]) == [_RECORDED["NAME"]]
