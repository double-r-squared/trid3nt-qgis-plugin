"""Offline tests for the ESRI Living Atlas wave (ADR 0117).

Covers: the two-stratum loader, the harvest normalizer (stubbed sharing API), the
scoped search tool (two-pool composition), the fetch bridge (dynamic SourceSpec
riding a monkeypatched route()), the premium/subscription honesty gate, and the
corpus-first retrieval proof for both new tools. No network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from trid3nt_contracts.execution import LayerURI, LivingAtlasLayerURI


# --------------------------------------------------------------------------- #
# Fixtures: two tiny harvested catalogs pointed at by env overrides.
# --------------------------------------------------------------------------- #

_AUTH_ENTRIES = [
    {
        "id": "auth_wetlands_img",
        "title": "USA Wetlands (Authoritative)",
        "snippet": "National wetlands inventory raster mosaic, ESRI authoritative.",
        "service_url": "https://landscape.arcgis.com/arcgis/rest/services/USA_Wetlands/ImageServer",
        "service_type": "Image Service",
        "owner": "esri",
        "extent": [-125.0, 24.0, -66.0, 50.0],
        "authoritative": True,
        "curation": "authoritative",
        "premium": False,
        "tags": ["wetlands", "hydrology", "landcover"],
    },
    {
        "id": "auth_parcels_feat",
        "title": "USA Parcels Authoritative",
        "snippet": "Authoritative parcel boundaries feature service.",
        "service_url": "https://services.arcgis.com/abc/arcgis/rest/services/Parcels/FeatureServer/0",
        "service_type": "Feature Service",
        "owner": "esri",
        "extent": [-125.0, 24.0, -66.0, 50.0],
        "authoritative": True,
        "curation": "authoritative",
        "premium": False,
        "tags": ["parcels", "cadastral"],
    },
    {
        "id": "auth_terrain_premium",
        "title": "World Terrain Elevation (premium)",
        "snippet": "Premium elevation image service requiring subscription.",
        "service_url": "https://elevation.arcgis.com/arcgis/rest/services/WorldElevation/Terrain/ImageServer",
        "service_type": "Image Service",
        "owner": "esri",
        "extent": [-180.0, -90.0, 180.0, 90.0],
        "authoritative": True,
        "curation": "authoritative",
        "premium": True,
        "tags": ["elevation", "terrain"],
    },
]

_COMMUNITY_ENTRIES = [
    {
        "id": "comm_wetlands_feat",
        "title": "Community Wetlands Map",
        "snippet": "Community contributed wetlands feature layer.",
        "service_url": "https://services.arcgis.com/xyz/arcgis/rest/services/CommWetlands/FeatureServer/0",
        "service_type": "Feature Service",
        "owner": "somebody",
        "extent": [-100.0, 30.0, -95.0, 35.0],
        "authoritative": False,
        "curation": "community",
        "premium": False,
        "tags": ["wetlands", "community"],
    },
    {
        "id": "comm_random_map",
        "title": "Neighborhood Trails",
        "snippet": "A community map service of local trails.",
        "service_url": "https://maps.example.com/arcgis/rest/services/Trails/MapServer",
        "service_type": "Map Service",
        "owner": "hiker",
        "extent": [-100.0, 30.0, -95.0, 35.0],
        "authoritative": False,
        "curation": "community",
        "premium": False,
        "tags": ["trails", "recreation"],
    },
]


@pytest.fixture()
def la_catalogs(tmp_path, monkeypatch):
    """Write the two fixture catalogs and point the loader at them (cache reset)."""
    from trid3nt_server.data.search import living_atlas_common as lac
    from trid3nt_server.data.search import living_atlas_index as lai

    auth = tmp_path / "living_atlas_authoritative.yaml"
    comm = tmp_path / "living_atlas_community.yaml"
    auth.write_text(yaml.safe_dump({"entries": _AUTH_ENTRIES}))
    comm.write_text(yaml.safe_dump({"entries": _COMMUNITY_ENTRIES}))
    monkeypatch.setenv("TRID3NT_LIVING_ATLAS_AUTHORITATIVE_YAML", str(auth))
    monkeypatch.setenv("TRID3NT_LIVING_ATLAS_COMMUNITY_YAML", str(comm))
    lac.reset_living_atlas_cache()
    lai.reset_index()
    yield
    lac.reset_living_atlas_cache()
    lai.reset_index()


# --------------------------------------------------------------------------- #
# Loader.
# --------------------------------------------------------------------------- #


def test_loader_splits_and_validates(la_catalogs):
    from trid3nt_server.data.search.living_atlas_common import (
        get_entry,
        load_living_atlas,
    )

    auth = load_living_atlas("authoritative")
    comm = load_living_atlas("community")
    assert {e.id for e in auth} == {"auth_wetlands_img", "auth_parcels_feat", "auth_terrain_premium"}
    assert {e.id for e in comm} == {"comm_wetlands_feat", "comm_random_map"}

    resolved = get_entry("auth_wetlands_img")
    assert resolved is not None
    entry, curation = resolved
    assert curation == "authoritative"
    assert entry.service_type == "Image Service"
    # resolve by service_url too
    assert get_entry("https://maps.example.com/arcgis/rest/services/Trails/MapServer")[1] == "community"
    assert get_entry("nope") is None


# --------------------------------------------------------------------------- #
# Search: two-pool composition.
# --------------------------------------------------------------------------- #


def test_search_authoritative_only_by_default(la_catalogs):
    from trid3nt_server.data.search.search_living_atlas import search_living_atlas

    res = search_living_atlas("wetlands")
    assert res, "expected an authoritative wetlands hit"
    assert all(r["curation"] == "authoritative" for r in res), "community must be EXCLUDED by default"
    ids = {r["id"] for r in res}
    assert "auth_wetlands_img" in ids
    assert "comm_wetlands_feat" not in ids


def test_search_include_community_labels(la_catalogs):
    from trid3nt_server.data.search.search_living_atlas import search_living_atlas

    res = search_living_atlas("wetlands", include_community=True)
    curations = {r["curation"] for r in res}
    assert "authoritative" in curations and "community" in curations
    # community never ranked above authoritative
    first_comm = next(i for i, r in enumerate(res) if r["curation"] == "community")
    last_auth = max(i for i, r in enumerate(res) if r["curation"] == "authoritative")
    assert last_auth < first_comm


def test_search_last_resort_community(tmp_path, monkeypatch):
    """When the authoritative stratum is empty, community surfaces as LABELLED last resort."""
    from trid3nt_server.data.search import living_atlas_common as lac
    from trid3nt_server.data.search import living_atlas_index as lai
    from trid3nt_server.data.search.search_living_atlas import search_living_atlas

    auth = tmp_path / "auth.yaml"
    comm = tmp_path / "comm.yaml"
    auth.write_text(yaml.safe_dump({"entries": []}))  # empty authoritative stratum
    comm.write_text(yaml.safe_dump({"entries": _COMMUNITY_ENTRIES}))
    monkeypatch.setenv("TRID3NT_LIVING_ATLAS_AUTHORITATIVE_YAML", str(auth))
    monkeypatch.setenv("TRID3NT_LIVING_ATLAS_COMMUNITY_YAML", str(comm))
    lac.reset_living_atlas_cache()
    lai.reset_index()
    try:
        res = search_living_atlas("wetlands")
        assert res, "expected a community last-resort hit"
        assert all(r["curation"] == "community" and r["last_resort"] for r in res)
    finally:
        lac.reset_living_atlas_cache()
        lai.reset_index()


# --------------------------------------------------------------------------- #
# Fetch bridge (dynamic SourceSpec, monkeypatched route()).
# --------------------------------------------------------------------------- #


@pytest.fixture()
def no_probe(monkeypatch):
    import importlib

    fmod = importlib.import_module(
        "trid3nt_server.data.search.fetch_living_atlas_layer.fetch_living_atlas_layer"
    )
    monkeypatch.setattr(fmod, "_probe_service", lambda url: {})
    return fmod


def test_fetch_image_service_dynamic_spec(la_catalogs, no_probe, monkeypatch):
    from trid3nt_server.data.fetchers._router import router
    from trid3nt_server.data.search.fetch_living_atlas_layer import (
        fetch_living_atlas_layer,
    )

    captured = {}

    def fake_route(spec, params):
        captured["spec"] = spec
        captured["params"] = params
        return LayerURI(
            layer_id=spec.source_class, name="x", layer_type="raster",
            uri="s3://trid3nt-cache/cache/static-30d/%s/deadbeef.tif" % spec.source_class,
            style_preset="",
        )

    monkeypatch.setattr(router, "route", fake_route)
    out = fetch_living_atlas_layer(item_id="auth_wetlands_img", bbox=(-100.0, 30.0, -99.0, 31.0))
    assert isinstance(out, LivingAtlasLayerURI)
    assert out.curation == "authoritative"
    assert out.item_id == "auth_wetlands_img"
    assert out.service_type == "Image Service"
    assert out.provenance["source"].startswith("ESRI Living Atlas")
    # dynamic spec rode route() with a per-item cache source_class + imageserver mode
    spec = captured["spec"]
    assert spec.source_class == "living_atlas_auth_wetlands_img"
    assert (spec.ingest or {}).get("access") == "imageserver_export"
    # URL surgery: base + service reconstructs the ImageServer URL
    base = spec.endpoints["data"].url
    svc = spec.ingest["imageserver"]["service_by_param"]["map"]["s"]
    assert f"{base}/{svc}/ImageServer" == "https://landscape.arcgis.com/arcgis/rest/services/USA_Wetlands/ImageServer"


def test_fetch_feature_service_dynamic_spec(la_catalogs, no_probe, monkeypatch):
    from trid3nt_server.data.fetchers._router import router
    from trid3nt_server.data.search.fetch_living_atlas_layer import (
        fetch_living_atlas_layer,
    )

    captured = {}

    def fake_route(spec, params):
        captured["spec"] = spec
        return LayerURI(
            layer_id=spec.source_class, name="x", layer_type="vector",
            uri="s3://trid3nt-cache/cache/static-30d/%s/beef.fgb" % spec.source_class,
            style_preset="",
        )

    monkeypatch.setattr(router, "route", fake_route)
    out = fetch_living_atlas_layer(item_id="auth_parcels_feat", bbox=(-100.0, 30.0, -99.0, 31.0))
    assert out.layer_type == "vector"
    spec = captured["spec"]
    assert spec.shape == "vector-fgb"
    assert spec.ingest["esri_json"] is True
    assert spec.endpoints["data"].url.endswith("/FeatureServer/0/query")


def test_fetch_premium_is_honest_subscription_error(la_catalogs, no_probe):
    from trid3nt_server.data.search.fetch_living_atlas_layer import (
        LivingAtlasSubscriptionError,
        fetch_living_atlas_layer,
    )

    with pytest.raises(LivingAtlasSubscriptionError) as exc:
        fetch_living_atlas_layer(item_id="auth_terrain_premium", bbox=(-100.0, 30.0, -99.0, 31.0))
    assert exc.value.error_code == "LIVING_ATLAS_SUBSCRIPTION_REQUIRED"
    assert exc.value.retryable is False


def test_fetch_unknown_item_is_typed_input_error(la_catalogs, no_probe):
    from trid3nt_server.data.search.fetch_living_atlas_layer import (
        LivingAtlasInputError,
        fetch_living_atlas_layer,
    )

    with pytest.raises(LivingAtlasInputError) as exc:
        fetch_living_atlas_layer(item_id="does_not_exist", bbox=(-100.0, 30.0, -99.0, 31.0))
    assert exc.value.error_code == "LIVING_ATLAS_INPUT_INVALID"


def test_probe_raises_subscription_on_token_required(la_catalogs, monkeypatch):
    """A token-required error envelope on the probe -> honest subscription error."""
    from trid3nt_server.data.search.fetch_living_atlas_layer import (
        LivingAtlasSubscriptionError,
        fetch_living_atlas_layer,
    )

    def fake_get_bytes(client, url, headers=None, params=None):
        body = json.dumps({"error": {"code": 499, "message": "Token Required"}}).encode()
        return body, "application/json", url

    # Patch the transport import target inside _probe_service.
    import trid3nt_server.data.fetchers._router.transport as tp
    monkeypatch.setattr(tp, "get_bytes", fake_get_bytes)
    with pytest.raises(LivingAtlasSubscriptionError):
        # auth_wetlands_img is not premium-flagged, so the probe is what gates it.
        fetch_living_atlas_layer(item_id="auth_wetlands_img", bbox=(-100.0, 30.0, -99.0, 31.0))


# --------------------------------------------------------------------------- #
# Harvest normalizer (stubbed sharing API via --fixture).
# --------------------------------------------------------------------------- #


def test_harvest_normalizes_and_splits(tmp_path):
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "harvest_living_atlas.py"
    spec = importlib.util.spec_from_file_location("harvest_la", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    fixture = {
        "total": 4,
        "results": [
            {"id": "a1", "title": "Auth Img", "type": "Image Service", "owner": "esri",
             "url": "https://x/arcgis/rest/services/A/ImageServer",
             "contentStatus": "public_authoritative", "typeKeywords": [],
             "extent": [[-125, 24], [-66, 50]], "tags": ["dem"], "snippet": "<b>hi</b> there"},
            {"id": "c1", "title": "Comm Feat", "type": "Feature Service", "owner": "joe",
             "url": "https://x/arcgis/rest/services/C/FeatureServer/0",
             "contentStatus": "", "typeKeywords": [], "extent": [[-100, 30], [-95, 35]]},
            {"id": "p1", "title": "Premium Img", "type": "Image Service", "owner": "esri",
             "url": "https://x/arcgis/rest/services/P/ImageServer",
             "contentStatus": "public_authoritative", "typeKeywords": ["Requires Subscription"],
             "extent": []},
            {"id": "w1", "title": "A Web Map", "type": "Web Map", "owner": "esri",
             "url": None, "contentStatus": "public_authoritative"},
        ],
    }
    fpath = tmp_path / "fx.json"
    fpath.write_text(json.dumps(fixture))
    rc = mod.main(["--fixture", str(fpath), "--out-dir", str(tmp_path)])
    assert rc == 0

    auth = yaml.safe_load((tmp_path / "living_atlas_authoritative.yaml").read_text())
    comm = yaml.safe_load((tmp_path / "living_atlas_community.yaml").read_text())
    auth_ids = {e["id"] for e in auth["entries"]}
    comm_ids = {e["id"] for e in comm["entries"]}
    assert auth_ids == {"a1", "p1"}  # web map skipped (not consumable)
    assert comm_ids == {"c1"}
    # premium flagged; HTML stripped from snippet; extent normalized
    p1 = next(e for e in auth["entries"] if e["id"] == "p1")
    assert p1["premium"] is True
    a1 = next(e for e in auth["entries"] if e["id"] == "a1")
    assert "<b>" not in a1["snippet"] and a1["extent"] == [-125.0, 24.0, -66.0, 50.0]


# --------------------------------------------------------------------------- #
# Corpus-first retrieval proof (model-free, top-8) for BOTH new tools.
# --------------------------------------------------------------------------- #


def test_new_tools_surface_in_top8():
    import trid3nt_server.main as _main  # noqa: F401 -- full daemon registry
    from trid3nt_server.data.search.search_tools import search_tools as dd
    from trid3nt_server.data.search.tool_retrieval import retrieve_visible_tools

    _main._import_tools_registry()
    dd._get_index()
    corpus = dd._load_corpus()
    for tool in ("search_living_atlas", "fetch_living_atlas_layer"):
        queries = corpus.get(tool, [])
        assert queries, f"{tool} has NO corpus queries (retrieval-corpus-first rule)"
        surfaced = any(tool in retrieve_visible_tools(q, None, 8) for q in queries)
        assert surfaced, f"{tool} surfaces in NO top-8 for any corpus query"
