"""The tide series and the bed it drives must be on ONE vertical datum.

A CO-OPS series is reported on a TIDAL datum (MLLW). The coastal bed is NOAA
DEM_all, which over US coasts serves the NCEI 1/9 arc-sec CUDEM tiles whose
catalog declares NAVD 88 - a GEODETIC datum. Left unreconciled the whole water
column sits high by the difference, and the run cold-starts land wet: at
Apalachicola the offset is 0.232 m, which put 8220 nodes (12.0 km2) of marsh
under water at t=0, 15725 of them above the highest normal tide.

The offset comes from the gauge's OWN published datum table rather than a
regional constant, because it is a property of the individual station.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.workflows.shared.tide_series import (
    BED_DATUM,
    TideSeriesError,
    datum_offset_m,
)


class _Payload:
    """A stand-in for the CO-OPS datums endpoint response."""

    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Payload":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


#: The real published table for CO-OPS 8728690 Apalachicola (1983-2001 epoch,
#: metres on station datum), which is what makes -0.232 the right answer.
_APALACHICOLA = {"datums": [{"name": "MLLW", "value": 1.307},
                            {"name": "NAVD88", "value": 1.539},
                            {"name": "MHHW", "value": 1.799}]}




def test_the_apalachicola_offset_is_the_diagnosed_value(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Payload(_APALACHICOLA))
    assert datum_offset_m("8728690", "MLLW", "NAVD88") == -0.232


def test_the_datum_name_is_matched_loosely(monkeypatch):
    """'NAVD 88' and 'NAVD_88' name the same datum as 'NAVD88'."""
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Payload(
                            {"datums": [{"name": "MLLW", "value": 1.307},
                                        {"name": "NAVD 88", "value": 1.539}]}))
    assert datum_offset_m("8728690", "mllw", "NAVD88") == -0.232


def test_a_missing_datum_pair_refuses_instead_of_returning_zero(monkeypatch):
    """The silent zero IS the defect; an unreconcilable pair must be heard."""
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Payload(
                            {"datums": [{"name": "MLLW", "value": 1.307}]}))
    with pytest.raises(TideSeriesError) as excinfo:
        datum_offset_m("8728690", "MLLW", "NAVD88")
    assert "NAVD88" in str(excinfo.value)


def test_an_unreachable_datum_table_refuses_instead_of_returning_zero(monkeypatch):
    import urllib.request

    def _boom(*a: object, **k: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    with pytest.raises(TideSeriesError):
        datum_offset_m("8728690", "MLLW", "NAVD88")


def test_an_unnamed_station_cannot_be_reconciled():
    with pytest.raises(TideSeriesError):
        datum_offset_m("", "MLLW", "NAVD88")


def test_the_bed_datum_is_declared_not_assumed():
    """The bed datum is a named constant the steering file and the layer both
    carry."""
    assert BED_DATUM == "NAVD88"
