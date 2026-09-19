"""``fetch_usseabed``: the bbox packager's one form field, and the ext table it
answers -- covered offline over a recorded package ZIP, with the sentinel
columns proven nulled rather than read as -99."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.ocean.fetch_usseabed import hooks as us

_HEADER = ("latitude,longitude,waterdepth,obsvntop,obsvnbot,locnname,datasetkey,"
           "locnkey,obsvnkey,device,datatypes,gravel,sand,mud,clay,grainsze,"
           "sorting,facies,facmshp,folkcde,rckmshp,vegmshp,carbonate,munslcolr,"
           "orgcarbn,lshearstr,porosity,pwavevel,roughness,lcritshstr,geolage,"
           "obsvndetai,key,obsvndate,datesrc,geom")

_ROW = ('40.390340,-73.782680,38,0.00,0.02,"OCNS9906:A:1005",247,162483,210171,'
        '"UnidDevice","TXR",1,81,18,6,3.60,1.8,"-",-99,"-",-99,-99,-99,"-",'
        '-99.0,-99.00,-99,-99,"-",-99.00,"-","-","ext_0097249",19991210,'
        '"collection date","0101000020E6100000EECEDA6D177252C00B293FA9F6314440"')


def _zip(rows: list[str] = None, member: str = "us9_ext.csv") -> bytes:
    rows = [_ROW] if rows is None else rows
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(member, "\n".join([_HEADER, *rows]))
    return buf.getvalue()


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_usseabed"]


def test_the_source_states_no_vertical_datum(spec):
    assert spec.coverage[0].datum is None
    assert spec.empty_error_suffix == "NO_SAMPLES"


def test_build_request_posts_the_bbox_as_one_metadata_form_field(spec):
    plans = us.build_request(spec, {"bbox": [-74.0, 40.3, -73.7, 40.5]})
    assert len(plans) == 1
    plan = plans[0]
    assert plan.method == "POST"
    assert plan.url == us.PACKAGE_URL
    meta = json.loads(plan.data["metadata"])
    assert meta["sources"] == ["ext"]
    assert (meta["westbc"], meta["southbc"], meta["eastbc"], meta["northbc"]) == (
        -74.0, 40.3, -73.7, 40.5)


def test_parse_response_reads_the_ext_table_into_a_point(spec):
    feats = us.parse_response(spec, {}, [_zip()])
    assert len(feats) == 1
    props = feats[0]["properties"]
    assert feats[0]["geometry"]["coordinates"] == [-73.782680, 40.390340]
    assert props["key"] == "ext_0097249"
    assert props["locnname"] == "OCNS9906:A:1005"
    assert props["waterdepth_m"] == 38.0
    assert props["gravel_pct"] == 1.0
    assert props["sand_pct"] == 81.0
    assert props["mud_pct"] == 18.0
    assert props["clay_pct"] == 6.0
    assert props["grainsize_phi"] == 3.60
    assert props["sorting_phi"] == 1.8


def test_the_service_s_sentinels_are_nulled_not_read_as_minus_99(spec):
    feats = us.parse_response(spec, {}, [_zip()])
    props = feats[0]["properties"]
    # facies/carbonate/etc are dropped from the emitted columns entirely; the
    # sentinel proof is on an emitted column instead: grainsze/sorting are real
    # here, so swap one for the sentinel string and prove it nulls.
    sentinel_row = _ROW.replace("3.60,1.8", "-99.00,-99")
    feats2 = us.parse_response(spec, {}, [_zip([sentinel_row])])
    assert feats2[0]["properties"]["grainsize_phi"] is None
    assert feats2[0]["properties"]["sorting_phi"] is None


def test_a_dash_sentinel_nulls_a_text_column(spec):
    dashed = _ROW.replace('"OCNS9906:A:1005"', '"-"')
    feats = us.parse_response(spec, {}, [_zip([dashed])])
    assert feats[0]["properties"]["locnname"] is None


def test_an_empty_ext_table_refuses_by_name(spec):
    with pytest.raises(RouterEmptyError) as excinfo:
        us.parse_response(spec, {}, [_zip([])])
    assert excinfo.value.error_code == "USSEABED_NO_SAMPLES"


def test_an_empty_body_refuses_by_name(spec):
    with pytest.raises(RouterEmptyError) as excinfo:
        us.parse_response(spec, {}, [b""])
    assert excinfo.value.error_code == "USSEABED_NO_SAMPLES"


def test_a_body_that_is_not_a_zip_is_an_upstream_error(spec):
    from trid3nt_server.tools.fetchers._router.errors import RouterUpstreamError

    with pytest.raises(RouterUpstreamError):
        us.parse_response(spec, {}, [b"<h1>error</h1>"])


def test_usseabed_surfaces_from_its_own_corpus_phrasings():
    from pathlib import Path

    import yaml

    from trid3nt_server.tools.search.search_tools import search_tools as dd
    from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

    dd._get_index()
    here = Path(us.__file__).resolve().parent
    queries = (yaml.safe_load((here / "corpus.yaml").read_text()) or {})["fetch_usseabed"]
    assert queries
    assert any("fetch_usseabed" in retrieve_visible_tools(q, None, 8) for q in queries), (
        "fetch_usseabed surfaces in NO top-8 for any of its corpus queries")
