"""Offline round-trip gates for the CLEAN pure-2D fake-reach authors.

These validate the ``patch_chippewa_{xnn,bnn}`` authors against the vendored,
HEC-authored Chippewa reference WITHOUT the solver -- the same offline discipline
the writers land under. The end-to-end SOLVE (fresh carved mesh ->
these authors -> production 6.6 engines, vol err 0.0) is exercised by
``build_chippewa_fakereach_deck.py`` inside ``trid3nt-local/hecras:latest``.
"""
from __future__ import annotations

import re

from hecras_pure2d_deck import patch_chippewa_bnn, patch_chippewa_xnn


def test_chippewa_xnn_patches_name_and_perimeter_fixed_width():
    x = patch_chippewa_xnn("2D Interior Area", 171)
    lines = x.splitlines()
    sa = [l for l in lines if l.startswith("SA ")]
    assert sa and "2D Interior Area" in sa[0], "SA name not patched"
    assert "Perimeter 1" not in x, "old SA name leaked"
    # the perimeter-count line must be proper 8-char fixed-width (the W1->W2 fix:
    # a widened field desyncs RasGeomPreprocess's storage-area header read)
    perim = [l for l in lines if re.match(r"\s*0\s+0\s+0\s+171\s+T\s*$", l)]
    assert perim, "perimeter-count line not authored"
    assert perim[0] == f"{0:>8}{0:>8}{0:>8}{171:>8}{'T':>8}", "not 8-wide fixed"
    # the clean fake reach + empty SA-connection are carried verbatim
    assert "Fake River" in x and "Fake Reach" in x, "fake reach missing"
    assert "Section - Storage Area Connection Data" in x
    assert "Sayers Dam" not in x and "Conn " not in x, "dam entanglement leaked"


def test_chippewa_bnn_suffixed_header_and_valid_hydrograph_location():
    b = patch_chippewa_bnn(5000.0, hydrograph_node=1)
    # 6.6-correct 2D-BC-line header is the SUFFIXED fake-reach form, never bare
    assert "Upstream Flow Hydrograph - River: Fake River  Reach: Fake Reach  RS: 100" in b
    # the ordinate hold carries the requested peak in both flow columns
    assert re.search(r"\n\s*0\s+5000\s+8760\s+5000\n", b), "peak_cfs not authored"
    # HYDROGRAPH LOCATIONS must point at 1 valid node (the shipped 0 -> div-by-zero)
    m = re.search(r"HYDROGRAPH LOCATIONS\n 1 \n\s*1\n", b)
    assert m, "hydrograph location not repaired to a valid node"
    assert "Downstream Normal Depth" in b


def test_chippewa_bnn_flow_scale_moves_only_flow_column():
    lo = patch_chippewa_bnn(1000.0)
    hi = patch_chippewa_bnn(1500.0)
    assert "1000" in lo and "1500" in hi and "1000" not in hi
