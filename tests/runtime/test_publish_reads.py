"""The runtime publish: a run that publishes nothing refuses in its reads' own words."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from trid3nt_server.render.formats import Published
from trid3nt_server.workflows.runtime import primitives as P
from trid3nt_server.workflows.runtime.module import SlotRefused


def test_a_run_publishing_nothing_names_why_each_read_held_nothing(monkeypatch):
    def empty(key, solved):
        raise P.OutputEmpty("not one value the run saved for HEAD is a number.")

    solved = SimpleNamespace(body=SimpleNamespace(READS={"field": empty}))

    async def publish(**kw):
        return Published(layers=(), charts=())

    monkeypatch.setattr(P, "publish", publish)
    with pytest.raises(SlotRefused) as caught:
        asyncio.run(P.publish_reads(
            engine="modflow", run={"run_id": "R1", "module": "gwf", "name": "x"},
            outputs=[], captions={}, params={}, solved=lambda module: solved,
            table=lambda s: ([P.field("HEAD", t="every", plane=1)], lambda *a: None),
            record=lambda s, n: None))
    assert "not one value the run saved for HEAD is a number" in str(caught.value)
