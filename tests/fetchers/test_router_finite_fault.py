"""The finite-fault row: the event record that names the product file and the FSP text read into subfaults.

Offline. The three FSP files are USGS's own complete_inversion.fsp for Kamchatka 2025 (three segments, Dx and Dz
per segment), Tohoku 2011 (three segments, Dx and Dz only on the Invs line) and Chignik 2021 (one segment, header
Nsbfs 21 over 294 rows); the event record is Chignik's finite-fault product list.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trid3nt_server.tools.fetchers._router.registration import get_spec
from trid3nt_server.tools.fetchers.hazard.fetch_finite_fault import hooks

_FIX = Path(__file__).parents[1] / "fixtures" / "geoclaw" / "finite_fault"


def _record(name: str) -> dict:
    spec = get_spec("fetch_finite_fault")
    return hooks.record(spec, {"event_id": name, "_fsp_url": "u", "_fsp_code": "c", "_fsp_updated_ms": 7},
                        [(_FIX / f"{name}.fsp").read_bytes()])


def test_a_multisegment_file_reads_each_segments_own_strike_dip_and_subfault_size():
    kam, toh = _record("kamchatka_2025"), _record("tohoku_2011")

    assert [(s["strike"], s["dip"]) for s in kam["segments"]] == [(217.0, 6.5), (217.0, 17.0), (217.0, 28.0)]
    assert len(kam["subfaults"]) == 465 and {(f["length_km"], f["width_km"]) for f in kam["subfaults"]} == {(20.0, 10.0)}
    assert [s["dip"] for s in toh["segments"]] == [15.0, 8.0, 21.0]
    assert len(toh["subfaults"]) == 325 and {(f["length_km"], f["width_km"]) for f in toh["subfaults"]} == {(25.0, 16.6)}
    assert kam["magnitude_mw"] == pytest.approx(8.8347, abs=1e-4) and toh["moment_nm"] == 4.7882953e22


def test_a_single_segment_file_takes_strike_dip_from_mech_and_counts_rows_not_nsbfs():
    chig = _record("chignik_2021")

    assert "Nsbfs = 21 subfaults" in (_FIX / "chignik_2021.fsp").read_text()
    assert len(chig["subfaults"]) == 294 and chig["segments"] == [{"segment": 1, "strike": 243.0, "dip": 14.0}]
    first = chig["subfaults"][0]
    assert (first["longitude"], first["latitude"], first["depth_km"], first["slip_m"]) == (-154.593, 55.3061, 8.4917, 0.4545)
    assert (first["rake"], first["rupture_time_s"], first["rise_time_s"]) == (112.9477, 86.883, 4.5)
    assert chig["source"] == {"product_code": "c", "updated_ms": 7, "url": "u"}


def test_the_event_record_resolves_to_the_newest_product_with_a_complete_inversion():
    spec = get_spec("fetch_finite_fault")
    body = (_FIX / "chignik_event_products.json").read_bytes()
    resolved = hooks.resolve_parse(spec, {"event_id": "ak0219neiszm"}, [body])

    assert resolved["_fsp_code"] == "ak0219neiszm_2" and resolved["_fsp_updated_ms"] == 1635193034829
    assert resolved["_fsp_url"].endswith("/ak0219neiszm_2/us/1635193034829/complete_inversion.fsp")
    plan = hooks.build_request(spec, {"event_id": "ak0219neiszm", **resolved})[0]
    assert plan.url == resolved["_fsp_url"]
    assert hooks.resolve_build(spec, {"event_id": "ak0219neiszm"})[0].params["eventid"] == "ak0219neiszm"


def test_an_event_with_no_finite_fault_file_refuses_naming_it():
    spec = get_spec("fetch_finite_fault")
    none = json.dumps({"properties": {"products": {"origin": [{}]}}}).encode()
    nofile = json.dumps({"properties": {"products": {"finite-fault": [{"contents": {"FFM.geojson": {"url": "x"}}}]}}}).encode()

    for body in (none, nofile):
        with pytest.raises(Exception, match="event 'us7000none' has no finite-fault product with a complete_inversion.fsp"):
            hooks.resolve_parse(spec, {"event_id": "us7000none"}, [body])
    with pytest.raises(Exception, match="states no event"):
        hooks.resolve_build(spec, {"event_id": " "})


def test_a_file_missing_a_value_the_parse_needs_refuses_by_name():
    text = (_FIX / "kamchatka_2025.fsp").read_text()
    spec = get_spec("fetch_finite_fault")
    cut_sizes = "\n".join(line for line in text.splitlines() if "Dx =" not in line)
    short_row = text.replace("  52.7932  162.0123  203.1413", "  52.7932  162.0123", 1)

    with pytest.raises(Exception, match="states no Dx and Dz for segment 1"):
        hooks.record(spec, {"event_id": "e"}, [cut_sizes.encode()])
    with pytest.raises(Exception, match="carries 9 values"):
        hooks.record(spec, {"event_id": "e"}, [short_row.encode()])
    with pytest.raises(Exception, match="states no Size line"):
        hooks.record(spec, {"event_id": "e"}, [b"% Mech : STRK = 1 DIP = 2\n"])
