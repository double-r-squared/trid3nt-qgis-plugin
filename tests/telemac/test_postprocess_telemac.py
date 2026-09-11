"""Unit tests for postprocess_telemac's pure pieces (no docker / TELEMAC / S3).

Covers the tracer-variable picker and the substance products the transported
field is still published under. The result read itself belongs to the engine's
own library inside the image (``tests/telemac/test_telemac_result_reader.py``),
and the rasterizers to ``tests/publishing/test_raster.py``.
"""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.workflows.telemac.products import postprocess_telemac as P


def test_pick_dye_var():
    assert P._pick_dye_var(["VELOCITY U", "DYE"]) == "DYE"
    # a T-prefixed tracer when no explicit DYE
    assert P._pick_dye_var(["WATER DEPTH", "T1"]) == "T1"
    assert P._pick_dye_var(["VELOCITY U", "WATER DEPTH"]) is None


def test_no_substance_product_publishes_a_dye_named_raster():
    """The product's NAME must not assert more than the field carries: an oil run
    advects the same passive tracer a dye run does, and a sediment run's tracer is
    a suspended grain load. Neither may reach the store as a dye raster."""
    from trid3nt_contracts.telemac_contracts import TELEMAC_SUBSTANCE_PRODUCTS as T

    assert sorted(T) == ["oil", "sediment"]
    for name in T:
        assert "dye" not in T[name].cog
        assert "dye" not in T[name].quantity
    # each class carries its OWN declared row, so two classes on one canvas are
    # told apart by what the reader is shown, not by a name in a table.
    from trid3nt_server.emission import presets

    for product in T.values():
        assert presets.from_row(product.style).kind == "continuous"
    assert len({(p.style["ramp"], p.style["label"]) for p in T.values()}) == 2


def test_the_peak_layer_handle_carries_the_product_that_wrote_it():
    """Two questions on one reach publish different rasters; a shared handle would
    register one over the other."""
    from trid3nt_contracts.telemac_contracts import TELEMAC_SUBSTANCE_PRODUCTS as T

    handles = {P.peak_layer_id("RID", product) for product in T.values()}
    assert len(handles) == 2
    assert P.peak_layer_id("RID", T["oil"]) != P.peak_layer_id("RID", T["sediment"])
    assert not any(" " in handle for handle in handles)


def _reach_result(telemac_result, varnames, fields):
    """A tiny solved reach: one cluster of wet nodes, two frames."""
    x = [0.0, 120.0, 0.0, 120.0, 60.0]
    y = [0.0, 0.0, 110.0, 110.0, 55.0]
    ikle = [[0, 1, 4], [1, 3, 4], [2, 3, 4], [0, 2, 4]]
    depth = np.full(5, 2.0)
    data = {"WATER DEPTH": [depth, depth],
            **{name: [np.asarray(f, dtype=float), np.asarray(f, dtype=float)]
               for name, f in fields.items()}}
    return telemac_result(varnames=["WATER DEPTH", *varnames], x=x, y=y,
                          ikle=ikle, times=[0.0, 60.0], data=data)


def _postprocessed(monkeypatch, tmp_path, telemac_result, *, product,
                   varnames, fields):
    from trid3nt_server.workflows.publishing import cog

    _reach_result(telemac_result, varnames, fields)
    monkeypatch.setattr(cog, "upload_cog",
                        lambda *a, **k: "s3://runs/RID/peak.tif")
    return P.postprocess_telemac(
        tmp_path / "r2d_river.slf", run_id="TESTPP0000000000000000AAA",
        utm_epsg=32610, reach_name="a reach", product=product)


def test_the_product_picks_the_tracer_and_the_units_it_is_carried_in(
        monkeypatch, tmp_path, telemac_result):
    """A coupled run writes the suspended load as a SECOND tracer, in g/l where the
    surface speaks mg/L. The product says which variable its field lands in, so the
    reader picks that one and scales it - a silent 1000x passes every other check."""
    from trid3nt_contracts.telemac_contracts import TELEMAC_SUBSTANCE_PRODUCTS as T

    fields = {"DYE": np.full(5, 4.0), "NCOH SEDIMENT1": np.full(5, 0.25)}
    dissolved, _ = _postprocessed(monkeypatch, tmp_path, telemac_result,
                                  product=T["oil"], varnames=list(fields),
                                  fields=fields)
    sediment, _ = _postprocessed(monkeypatch, tmp_path, telemac_result,
                                 product=T["sediment"], varnames=list(fields),
                                 fields=fields)
    assert dissolved[0].dye_cmax_mgl == pytest.approx(4.0)
    assert dissolved[0].quantity == "oil_tracer_concentration"
    assert sediment[0].dye_cmax_mgl == pytest.approx(250.0)   # 0.25 g/l -> mg/L
    assert sediment[0].quantity == "suspended_sediment_concentration"
    assert sediment[0].layer_id != dissolved[0].layer_id
