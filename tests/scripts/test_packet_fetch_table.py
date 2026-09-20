"""The fetch table is READ OFF THE DAEMON'S LOG, not recorded for the packet.

One window of log lines carries every fact the table states: the ask, the
executor, the URL and its status, the cache decision and the bytes. A cache hit
went nowhere upstream, so its row carries no endpoint; the journal's own pick
line supplies the slot, the class, the cell and the datum.
"""

from __future__ import annotations

import functools
import importlib.util
import sys
from pathlib import Path

import pytest

DEV = Path(__file__).resolve().parents[2] / "dev"
SCRIPT = DEV / "packet" / "assemble_proof_packet.py"

if not DEV.is_dir():
    pytest.skip("dev/ is absent: the dev tools are not on the remote",
                allow_module_level=True)

SESSION = "01TESTSESSION"
LOG = f"""\
2026-09-20 14:47:54,100 INFO trid3nt_server.server dev-tool-invoke dispatch session={SESSION} tool=t
2026-09-20 14:47:54,150 INFO trid3nt_server.tools.fetchers._router.router fetch fetch_dem ask={{'bbox': [-75.7, 36.1, -75.7, 36.2]}}
2026-09-20 14:47:54,250 INFO trid3nt_server.tools.cache read_through hit (s3) tool=fetch_dem key=abc123 bytes=159549
2026-09-20 14:47:55,000 INFO trid3nt_server.tools.fetchers._router.router fetch fetch_ndbc_buoys ask={{'bbox': [-75.7, 36.1, -75.7, 36.2]}}
2026-09-20 14:47:55,400 INFO httpx HTTP Request: GET https://www.ndbc.noaa.gov/data/realtime2/44100.txt "HTTP/1.1 200 OK"
2026-09-20 14:47:55,500 INFO trid3nt_server.tools.fetchers._router.executors.vector_fgb router.vector_fgb: FlatGeobuf = 5104 bytes (1 feature(s), source=ndbc_buoys)
2026-09-20 14:47:55,600 INFO trid3nt_server.tools.cache read_through miss-write (s3) tool=fetch_ndbc_buoys key=def456 bytes=5104
2026-09-20 14:47:56,000 INFO trid3nt_server.server ws-recv session={SESSION} type=case-command
"""

EVIDENCE = {
    "session_id": SESSION,
    "layers": [{"uri": "s3://trid3nt-cache/cache/static-30d/dem/abc123.tif",
                "legend": {"units": "m"}}],
}
JOURNAL = {"sources": {
    "bed terrain": {"picked": "fetch_dem", "reason": "bed terrain: fetch_dem "
                    "(measured, over this place, 10 m, measured to 2024-01-01, "
                    "NAVD88)."},
    "wave": {"picked": "fetch_ndbc_buoys", "reason": "wave: fetch_ndbc_buoys "
             "(measured, over this place, stations, 30-min samples, no datum "
             "stated)."}}}


@functools.lru_cache(maxsize=1)
def _packet_module():
    """The assembler, imported by path - ``dev/packet/`` is not a package."""
    spec = importlib.util.spec_from_file_location("assemble_proof_packet", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("assemble_proof_packet", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def rows(tmp_path, monkeypatch):
    module = _packet_module()
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "agent.log").write_text(LOG, encoding="utf-8")
    monkeypatch.setattr(module, "REPO", tmp_path)
    monkeypatch.setattr(module, "_latencies", lambda session: {})
    return {row["source"]: row for row in module._fetches(EVIDENCE, JOURNAL)}


def test_a_cache_hit_states_its_ask_bytes_and_pick(rows):
    hit = rows["fetch_dem"]
    assert hit["cache"] == "hit"
    assert hit["bytes"] == 159549
    assert "'bbox': [-75.7, 36.1, -75.7, 36.2]" in hit["ask"]
    assert (hit["kind"], hit["cell"], hit["datum"]) == ("bed terrain", "10 m", "NAVD88")
    assert hit["units"] == "m"
    assert hit["ms"] == 100


def test_a_hit_carries_no_endpoint_because_nothing_went_upstream(rows):
    assert rows["fetch_dem"]["endpoint"] == ""
    assert rows["fetch_dem"]["executor"] == ""


def test_a_miss_carries_its_url_status_executor_and_clock(rows):
    miss = rows["fetch_ndbc_buoys"]
    assert miss["cache"] == "miss"
    assert miss["endpoint"] == "www.ndbc.noaa.gov/data/realtime2/44100.txt"
    assert miss["status"] == "200"
    assert miss["executor"] == "vector_fgb"
    assert miss["ms"] == 600
    assert miss["kind"] == "wave"
