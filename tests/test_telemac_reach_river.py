"""The reach's river: the seed ladder, the staged geometry, and DETERMINISM.

Six network fetches used to run inside the solver container - an NLDI position
snap, two NHDPlus_HR flowline re-seeds, the NLDI navigate that IS the model
centerline, an NHDArea bank query and a private Copernicus/3DEP DEM ladder. They
are server tier now, and these pin what that bought.

The load-bearing one is REPEATABILITY. The old ladder was fail-OPEN: a slow
NHDPlus query kept the raw seed, meshed a different reach, and left nothing in
the record saying which had happened, so a canary pin could flip with no code
change between two runs. The ladder here distinguishes "the query answered, and
the answer was no improvement" (a decision, recorded) from "the query failed" (a
refusal), and it stamps the chosen seed, the navigated COMIDs and a digest of the
staged centerline onto the run.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from trid3nt_server.workflows.telemac.steps import reach as R
from trid3nt_server.workflows.telemac.steps.errors import TelemacDyeScenarioError

_REACH = {"lon": -124.10, "lat": 40.48, "name": "Scotia, California",
          "slug": "scotia", "river_name": "Eel River",
          "bbox": (-124.16, 40.42, -124.04, 40.54)}
_SEED = {"lon": -124.09, "lat": 40.49, "source": "mid-reach point"}


def _line(coords, **props):
    return {"type": "Feature", "properties": dict(props),
            "geometry": {"type": "LineString", "coordinates": coords}}


def _poly(ring, **props):
    return {"type": "Feature", "properties": dict(props),
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


class _Layer:
    def __init__(self, uri):
        self.uri = uri


class _World:
    """The four fetches, faked, each recording what it was ASKED for."""

    def __init__(self, *, named=None, mainstem=None, flowlines=None, banks=None,
                 bed_boom=False):
        self.named = named if named is not None else [
            _line([[-124.0904, 40.4971], [-124.0910, 40.4980]], gnis_name="Eel River")]
        self.mainstem = mainstem if mainstem is not None else []
        self.flowlines = flowlines if flowlines is not None else [
            _line([[-124.0905, 40.4969], [-124.1000, 40.5020]], nhdplus_comid=2704729),
            _line([[-124.1000, 40.5020], [-124.1189, 40.5099]], nhdplus_comid=2704730)]
        self.banks = banks if banks is not None else [
            _poly([[-124.11, 40.50], [-124.09, 40.50], [-124.09, 40.51],
                   [-124.11, 40.51], [-124.11, 40.50]], ftype=460)]
        self.bed_boom = bed_boom
        self.calls: list[tuple] = []
        self.staged: dict[str, list] = {}

    def registry_fn(self, name):
        def _flowlines(*, bbox, max_records, gnis_name=None, **_kw):
            self.calls.append(("flowlines", tuple(bbox), gnis_name, max_records))
            return _Layer("s3://c/named.fgb" if gnis_name else "s3://c/main.fgb")

        def _area(*, bbox, max_records, **_kw):
            self.calls.append(("banks", tuple(bbox), max_records))
            return _Layer("s3://c/banks.fgb")

        def _navigate(*, seed_point, direction, distance_km, **_kw):
            self.calls.append(("navigate", tuple(seed_point), direction, distance_km))
            return _Layer("s3://c/centerline.fgb")

        def _cop(*, bbox, px_per_deg, **_kw):
            self.calls.append(("bed_copernicus", tuple(bbox), px_per_deg))
            if self.bed_boom:
                raise RuntimeError("PC STAC 503")
            return _Layer("s3://c/bed.tif")

        def _dem(*, bbox, source, resolution_m, **_kw):
            self.calls.append(("bed_3dep", tuple(bbox), source, resolution_m))
            return _Layer("s3://c/3dep.tif")

        return {"fetch_nhdplus_hr_flowlines": _flowlines,
                "fetch_nhd_area_water": _area,
                "fetch_nhdplus_nldi_navigate": _navigate,
                "fetch_copernicus_dem": _cop,
                "fetch_dem": _dem}[name]

    def read_vector(self, uri):
        return {"s3://c/named.fgb": self.named,
                "s3://c/main.fgb": self.mainstem,
                "s3://c/centerline.fgb": self.flowlines,
                "s3://c/banks.fgb": self.banks}[str(uri)]

    def stage(self, features, *, run_tag, dest):
        import hashlib
        self.staged[dest] = features
        body = json.dumps({"type": "FeatureCollection", "features": features},
                          sort_keys=True).encode("utf-8")
        return f"s3://cache/telemac/{run_tag}/{dest}", hashlib.sha256(body).hexdigest()

    def run(self, **kw):
        args = {"reach": _REACH, "seed": _SEED, "run_tag": "TAG",
                "reach_length_km": 1.0}
        args.update(kw)
        with patch.object(R, "registry_fn", self.registry_fn), \
             patch.object(R, "_read_vector_features", self.read_vector), \
             patch.object(R, "_stage_geojson", self.stage):
            return asyncio.run(R.resolve_reach_river(**args))


# --------------------------------------------------------------------------- #
# The seed ladder
# --------------------------------------------------------------------------- #
def test_a_named_reach_reseeds_onto_the_named_watercourse():
    """The GNIS name is the disambiguator, so it is tried before anything else."""
    w = _World()
    out = w.run()
    assert out["provenance"]["seed_rung"] == "position-named-flowline"
    # the navigate started from the NAMED vertex, not the raw seed
    nav = next(c for c in w.calls if c[0] == "navigate")
    assert nav[1] == (-124.0904, 40.4971)


def test_a_name_that_matches_nothing_keeps_the_raw_seed_and_SAYS_so():
    """An empty answer is a decision about the reach, and it is recorded.

    This is the case the old fail-open ladder made indistinguishable from a
    failed query: both kept the raw seed and both logged a warning nobody read.
    """
    w = _World(named=[])
    out = w.run()
    assert out["provenance"]["seed_rung"] == "position-named-flowline-absent"
    nav = next(c for c in w.calls if c[0] == "navigate")
    assert nav[1] == (-124.09, 40.49)


def test_a_name_free_reach_prefers_the_dominant_mainstem():
    """At a confluence the nearest channel is often a low-order tributary stub."""
    w = _World(mainstem=[
        _line([[-124.0899, 40.4899]], gnis_name="", streamorde=3, totdasqkm=12.0),
        _line([[-124.0910, 40.4910]], gnis_name="", streamorde=7, totdasqkm=8000.0)])
    out = w.run(reach={**_REACH, "river_name": ""})
    assert out["provenance"]["seed_rung"] == "position-mainstem"
    nav = next(c for c in w.calls if c[0] == "navigate")
    assert nav[1] == (-124.091, 40.491)


def test_a_mainstem_that_does_not_outrank_the_nearest_leaves_the_seed_alone():
    w = _World(mainstem=[
        _line([[-124.0899, 40.4899]], gnis_name="", streamorde=5, totdasqkm=900.0),
        _line([[-124.0910, 40.4910]], gnis_name="", streamorde=5, totdasqkm=8000.0)])
    out = w.run(reach={**_REACH, "river_name": ""})
    assert out["provenance"]["seed_rung"] == "position-nearest-flowline"


def test_a_distant_mainstem_never_yanks_a_small_creek_study_onto_it():
    """The re-seed radius is what keeps a headwater question a headwater question."""
    w = _World(mainstem=[
        _line([[-124.0899, 40.4899]], gnis_name="", streamorde=2, totdasqkm=3.0),
        _line([[-124.5, 40.9]], gnis_name="", streamorde=8, totdasqkm=90000.0)])
    out = w.run(reach={**_REACH, "river_name": ""})
    assert out["provenance"]["seed_rung"] == "position-nearest-flowline"


def test_a_release_point_seeds_the_reach_when_one_was_supplied():
    w = _World()
    out = w.run(release=(-124.05, 40.44))
    assert out["provenance"]["seed_rung"].startswith("release-position")


def test_a_FAILED_seed_query_refuses_it_does_not_keep_the_raw_seed():
    """THE determinism repair. A fetch failure used to degrade to the raw seed,
    which meshed a DIFFERENT river and recorded nothing - so two identical
    invocations could disagree and the record could not say why."""
    w = _World()
    real = w.registry_fn

    def _boom(name):
        if name == "fetch_nhdplus_hr_flowlines":
            def _f(**_kw):
                raise RuntimeError("NHDPLUS_HR_FLOWLINES_UPSTREAM: 503")
            return _f
        return real(name)

    with patch.object(R, "registry_fn", _boom), \
         patch.object(R, "_read_vector_features", w.read_vector), \
         patch.object(R, "_stage_geojson", w.stage), \
         pytest.raises(RuntimeError, match="503"):
        asyncio.run(R.resolve_reach_river(
            reach=_REACH, seed=_SEED, run_tag="TAG", reach_length_km=1.0,
            ))


# --------------------------------------------------------------------------- #
# Determinism, as a checkable claim
# --------------------------------------------------------------------------- #
def test_two_identical_invocations_stage_the_identical_centerline():
    a = _World().run()
    b = _World().run()
    assert a["provenance"]["centerline_sha256"] == b["provenance"]["centerline_sha256"]
    assert a["provenance"]["seed_rung"] == b["provenance"]["seed_rung"]
    assert a["provenance"]["centerline_comids"] == b["provenance"]["centerline_comids"]


def test_a_different_centerline_produces_a_different_digest():
    """The digest has to be able to say NO, or it says nothing."""
    a = _World().run()
    b = _World(flowlines=[
        _line([[-120.0, 40.0], [-120.1, 40.1]], nhdplus_comid=999)]).run()
    assert a["provenance"]["centerline_sha256"] != b["provenance"]["centerline_sha256"]


def test_the_run_records_which_reaches_it_navigated():
    out = _World().run()
    assert out["provenance"]["centerline_comids"] == [2704729, 2704730]


# --------------------------------------------------------------------------- #
# What gets staged
# --------------------------------------------------------------------------- #
def test_the_three_inputs_land_under_the_names_the_worker_reads():
    out = _World().run()
    assert [row["dest"] for row in out["inputs"]] == [
        R.CENTERLINE_DEST, R.BANKS_DEST, R.BED_DEST]


def test_an_empty_bank_answer_is_STAGED_not_omitted():
    """"No NHDArea polygon covers this reach" is the answer the worker's
    banks-unavailable gate is built to raise. A missing FILE is a staging
    failure, which is a different thing and must not read as coverage."""
    w = _World(banks=[])
    out = w.run()
    assert R.BANKS_DEST in [row["dest"] for row in out["inputs"]]
    assert w.staged[R.BANKS_DEST] == []


def test_the_mesh_preview_stages_geometry_and_skips_the_bed():
    w = _World()
    out = w.run(with_bed=False)
    assert [row["dest"] for row in out["inputs"]] == [R.CENTERLINE_DEST, R.BANKS_DEST]
    assert not [c for c in w.calls if c[0].startswith("bed_")]


def test_an_empty_navigate_refuses_rather_than_meshing_nothing():
    w = _World(flowlines=[])
    with pytest.raises(TelemacDyeScenarioError, match="no reach to mesh"):
        w.run()


# --------------------------------------------------------------------------- #
# The bed
# --------------------------------------------------------------------------- #
def test_the_bed_is_asked_for_the_sources_own_lattice():
    """3600 px/deg IS GLO-30's grid. Asking for it is what makes the staged
    raster carry the source pixels rather than a resample of them, and therefore
    what keeps the fitted bed identical to the one the worker used to fetch."""
    w = _World()
    w.run()
    bed = next(c for c in w.calls if c[0] == "bed_copernicus")
    assert bed[2] == 3600.0


def test_the_bed_window_covers_the_whole_centerline_with_room_for_the_corridor():
    w = _World()
    w.run()
    bed = next(c for c in w.calls if c[0] == "bed_copernicus")
    min_lon, min_lat, max_lon, max_lat = bed[1]
    assert min_lon < -124.1189 and max_lon > -124.0905
    assert min_lat < 40.4969 and max_lat > 40.5099


def test_a_copernicus_outage_falls_to_3dep_and_NAMES_the_reason():
    """A cross-dataset substitution is loud by construction: the reason rides
    the record, where every consumer reads it."""
    w = _World(bed_boom=True)
    out = w.run()
    assert out["provenance"]["bed_source"] == "usgs-3dep"
    assert "503" in out["provenance"]["bed_fallback_reason"]
