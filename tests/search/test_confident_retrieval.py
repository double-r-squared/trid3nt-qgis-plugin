"""Tools offered by confidence: the embedding backend, the step split, the similarity cut, find_sources rows cut by margin."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import trid3nt_server.main as main
import trid3nt_server.tools.search.search_tools.search_tools as dd
from trid3nt_server.tools.search import tool_retrieval as tr

main._import_tools_registry()

MULTI_STEP = [
    ("pull the USGS stream gauge for the Colorado River and then fetch the 10 m DEM for the same area", ["find_sources", "fetch"]),
    ("download building footprints for Tampa; then get the land cover", ["find_sources", "fetch"]),
    ("fetch the DEM for my area, then look up the 100-year 24-hour rainfall depth", ["find_sources", "lookup_precip_return_period"]),
    ("get the elevation raster and then probe the value at the pin I dropped", ["find_sources", "probe_point"]),
    ("fetch the lake and river flowlines and then mark which flowline each lake drains into", ["find_sources", "mark_lake_outlets"]),
    ("fetch hourly rainfall for the storm then simulate the runoff from the catchment", ["find_sources", "telemac_rain_on_grid"]),
    ("get the tide gauge record and then run a storm surge simulation for the hurricane", ["find_sources", "geoclaw_storm_surge"]),
    ("fetch the wind data and simulate the nearshore waves at the beach", ["find_sources", "tomawac_nearshore_waves"]),
    ("simulate a tsunami from the rupture and then chart the water depth over time", ["geoclaw_tsunami_inundation", "generate_chart"]),
    ("compute the water table under recharge; then restyle the layer with a blue ramp", ["modflow_water_table", "restyle_layer"]),
    ("run the dye release in the river and then read the run diagnostics", ["telemac_dye_release", "read_run_diagnostics"]),
    ("build a mesh for the harbor and then refine it near the breakwater", ["build_mesh", "mesh_op"]),
    ("fetch the DEM; then clip it to the county; then show it as a hillshade", ["find_sources", "run_qgis_algorithm"]),
    ("geocode the Port of Houston and then draw an area for me to confirm", ["geocode_location", "request_spatial_input"]),
    ("simulate a contaminant plume in the aquifer and then make a chart of the concentration at the well", ["modflow_plume", "generate_chart"]),
    ("fetch the wind and current data then run an oil spill simulation", ["find_sources", "telemac_oil_spill"]),
    ("run the maintenance dredging of the channel and then rename the result layer", ["telemac_channel_dredging", "restyle_layer"]),
    ("fetch the DEM for the area and then build a mesh of it", ["find_sources", "build_mesh"]),
    ("find the stream gauges near Des Moines, then plot their discharge", ["find_sources", "generate_chart"]),
    ("model the dissolved oxygen sag below the outfall and then export the results", ["telemac_do_sag"]),
]


@pytest.fixture(scope="module")
def warm_index():
    dd._get_index()
    yield


def _index(sims, names=None):
    """A dense-only index whose documents sit at the given cosines from a fixed query."""
    names = names or [f"tool_{i}" for i in range(len(sims))]
    rows = np.array([[s, (1.0 - s * s) ** 0.5] for s in sims], dtype="float32")
    return SimpleNamespace(
        tool_names=names, bm25=None, dense_matrix=rows, dense_owner=None,
        dense_encode_fn=lambda _t: np.array([[1.0, 0.0]], dtype="float32"),
        backend_name="hashed", descriptions=[""] * len(sims),
        synthetic_queries=[[] for _ in sims])


def test_the_embedding_model_loads_on_the_cpu(monkeypatch):
    import sentence_transformers

    seen = {}

    class _Model:
        def __init__(self, model_id, **kw):
            seen.update(kw)

        def encode(self, texts, **_kw):
            return np.zeros((len(texts), 2), dtype="float32")

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _Model)
    encode, _np, name = dd._try_sentence_transformers_backend()
    encode(["x"])
    assert name == "sentence_transformers" and seen == {"device": "cpu"}


def test_the_built_index_reports_the_sentence_transformers_backend(warm_index):
    index = dd._get_index()
    assert index.backend_name == "sentence_transformers" and index.dense_matrix is not None


@pytest.mark.parametrize("text,steps", [
    ("fetch the DEM", ["fetch the DEM"]),
    ("fetch the DEM then clip it", ["fetch the DEM", "clip it"]),
    ("fetch the DEM and then clip it", ["fetch the DEM", "clip it"]),
    ("fetch the DEM, after that clip it; chart it", ["fetch the DEM", "clip it", "chart it"]),
    ("fetch the DEM and run the model", ["fetch the DEM", "run the model"]),
    ("fetch rainfall and runoff for the basin", ["fetch rainfall and runoff for the basin"]),
])
def test_a_message_is_split_into_its_steps(text, steps):
    assert tr.split_steps(text) == steps


def test_the_best_tool_and_every_tool_within_the_margin_are_offered(monkeypatch, user_settings):
    monkeypatch.setattr(dd, "_INDEX", _index([0.80, 0.78, 0.50, 0.20]))
    user_settings(MATCH_MARGIN=0.1, MIN_MATCH_SIMILARITY=0.1)
    assert [n for n, _ in tr.retrieve_matched_tools("anything", 8)] == ["tool_0", "tool_1"]
    user_settings(MATCH_MARGIN=0)
    assert [n for n, _ in tr.retrieve_matched_tools("anything", 8)] == ["tool_0"]


def test_nothing_is_offered_below_the_minimum_similarity(monkeypatch, user_settings):
    monkeypatch.setattr(dd, "_INDEX", _index([0.25, 0.24]))
    user_settings(MATCH_MARGIN=0.5, MIN_MATCH_SIMILARITY=0.3)
    assert tr.retrieve_matched_tools("anything", 8) == []
    assert tr.retrieve_visible_tools("anything", None, 8) == set(tr.CORE_FLOOR)


def test_k_is_a_ceiling_not_a_fill(monkeypatch, user_settings):
    monkeypatch.setattr(dd, "_INDEX", _index([0.9, 0.5, 0.45, 0.4]))
    user_settings(MATCH_MARGIN=0.1, MIN_MATCH_SIMILARITY=0.1)
    assert len(tr.retrieve_matched_tools("anything", 8)) == 1
    user_settings(MATCH_MARGIN=1)
    assert len(tr.retrieve_matched_tools("anything", 3)) == 3


def test_a_late_step_is_not_crowded_out_by_an_early_steps_runners_up(warm_index, user_settings):
    user_settings(MATCH_MARGIN=1, MIN_MATCH_SIMILARITY=0.1)
    matched = [n for n, _ in tr.retrieve_matched_tools("build a mesh then restyle the layer colors", 2)]
    assert "restyle_layer" in matched and len(matched) == 2


def test_a_called_tool_stays_and_an_offered_unused_one_is_dropped(warm_index):
    first = tr.retrieve_visible_tools("refine the mesh", None, 8)
    assert "mesh_op" in first
    later = tr.retrieve_visible_tools("show the weather", {"probe_point"}, 8)
    assert "probe_point" in later and "mesh_op" not in later


@pytest.mark.parametrize("text,needed", MULTI_STEP)
def test_every_tool_a_multi_step_message_needs_is_offered_in_round_one(warm_index, text, needed):
    offered = tr.retrieve_visible_tools(text, None, 8)
    assert set(needed) <= offered, sorted(set(needed) - offered)


def test_the_split_is_what_offers_the_second_steps_tool(warm_index, user_settings):
    user_settings(MATCH_MARGIN=0.1)
    text = "build a mesh for the harbor and then restyle the layer"
    assert "restyle_layer" in tr.retrieve_visible_tools(text, None, 8)
    assert "restyle_layer" not in tr.retrieve_visible_tools("build a mesh for the harbor", None, 8)


def test_every_corpus_phrasing_offers_its_tool(warm_index):
    from trid3nt_contracts.coverage import DATA_CLASSES
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.fetchers._router.registration import registered_spec_names

    corpus = dd._load_corpus()
    sources = registered_spec_names()
    missed = []
    for key, queries in corpus.items():
        right = "find_sources" if key in DATA_CLASSES or key in sources else key
        if right not in TOOL_REGISTRY:
            continue
        missed += [q for q in queries if right not in tr.retrieve_visible_tools(q, None, 8)]
    assert not missed, missed[:10]


def _sources_index(monkeypatch, sims):
    from trid3nt_server.tools.fetchers._router.registration import registered_spec_names

    names = sorted(registered_spec_names())[: len(sims)]
    monkeypatch.setattr(dd, "_SOURCE_INDEX", _index(sims, names))
    return names


@pytest.mark.asyncio
async def test_find_sources_returns_the_rows_within_the_margin_and_a_tie_returns_both(monkeypatch, user_settings):
    from trid3nt_server.tools.search.find_sources.find_sources import find_sources

    user_settings(SOURCE_MARGIN=0.1)
    names = _sources_index(monkeypatch, [0.80, 0.80, 0.50, 0.30])
    rows = (await find_sources(query="anything"))["results"]
    assert [r["source"] for r in rows] == names[:2]
    names = _sources_index(monkeypatch, [0.80, 0.50, 0.30])
    assert [r["source"] for r in (await find_sources(query="anything"))["results"]] == names[:1]
