# Worker-side wiring test: the MODFLOW worker's --build-spec-uri postprocess
# (run_plume_postprocess) + completion.json build-mode fields
# (publish_manifest_uri / deck / error_code) + the _modflow_build job_spec gate.
#
# The plume postprocess reads the LOCAL gwt_model.ucn (final-timestep, max-over-
# layers concentration), reprojects it to an EPSG:4326 plume COG in the deck dir
# (so the entrypoint *.tif sweep ships it), builds the publish manifest, and fires
# the empty-plume honesty gate. flopy's UCN binary reader + the MF6 deck load are
# monkeypatched so no mf6 binary / real solve is needed (mirrors the SFINCS
# synthetic-NetCDF wiring test).

from __future__ import annotations

import sys
from pathlib import Path

import pytest

rasterio = pytest.importorskip("rasterio")
import numpy as np  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.workers._modflow_postprocess import postprocess as pp  # noqa: E402
from services.workers.modflow import entrypoint as ep  # noqa: E402


_GEO = {
    "xorigin": 500000.0,
    "yorigin": 4400000.0,
    "delr": 50.0,
    "delc": 50.0,
    "nrow": 40,
    "ncol": 40,
}


def _plume_grid(*, flooded: bool) -> "np.ndarray":
    arr = np.zeros((40, 40), dtype="float64")
    if flooded:
        arr[18:22, 18:26] = 12.5  # a patch above the 0.001 mg/L floor
    return arr


def _stage_deck(tmp_path: Path) -> Path:
    deck = tmp_path / "deck"
    deck.mkdir()
    (deck / pp.GWT_UCN_FILENAME).write_bytes(b"stub-ucn")  # located, read monkeypatched
    return deck


def test_run_plume_postprocess_ok(tmp_path: Path, monkeypatch):
    deck = _stage_deck(tmp_path)
    monkeypatch.setattr(pp, "_read_final_concentration", lambda _p: _plume_grid(flooded=True))
    monkeypatch.setattr(pp, "_grid_georegistration", lambda _d: dict(_GEO))

    result = pp.run_plume_postprocess(
        "RID", deck, "EPSG:32617", lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "ok"
    assert result.error_code is None
    manifest = result.manifest
    assert manifest is not None
    assert manifest["engine"] == "modflow"
    assert manifest["status"] == "ok"
    assert manifest["frame_count"] == 1
    assert len(manifest["layers"]) == 1
    layer = manifest["layers"][0]
    assert layer["cog_uri"] == "s3://runs-b/RID/plume_concentration_4326.tif"
    assert layer["style_preset"] == pp.PLUME_STYLE_PRESET
    assert layer["metrics"]["max_concentration_mgl"] == pytest.approx(12.5, rel=1e-3)
    assert layer["metrics"]["plume_area_km2"] > 0.0
    cog = deck / "plume_concentration_4326.tif"
    assert cog.exists()
    with rasterio.open(cog) as src:
        assert str(src.crs) == "EPSG:4326"


def test_run_plume_postprocess_empty_honesty_gate(tmp_path: Path, monkeypatch):
    deck = _stage_deck(tmp_path)
    monkeypatch.setattr(pp, "_read_final_concentration", lambda _p: _plume_grid(flooded=False))
    monkeypatch.setattr(pp, "_grid_georegistration", lambda _d: dict(_GEO))

    result = pp.run_plume_postprocess(
        "RID", deck, "EPSG:32617", lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "error"
    assert result.error_code == "MODFLOW_PLUME_EMPTY"
    assert not (deck / "plume_concentration_4326.tif").exists()
    # The gate still returns a manifest carrying the (zero) metrics + error_code.
    assert result.manifest is not None
    assert result.manifest["error_code"] == "MODFLOW_PLUME_EMPTY"


def test_run_plume_postprocess_missing_ucn(tmp_path: Path):
    deck = tmp_path / "empty_deck"
    deck.mkdir()
    result = pp.run_plume_postprocess(
        "RID", deck, "EPSG:32617", lambda rel: f"s3://runs-b/RID/{rel}"
    )
    assert result.status == "error"
    assert result.error_code == "MODFLOW_PLUME_OUTPUT_MISSING"
    assert result.manifest is None


def test_write_completion_carries_build_mode_fields(tmp_path: Path, monkeypatch):
    captured: dict = {}

    class _FakeS3:
        def put_object(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(ep, "_s3_client", lambda: _FakeS3())
    monkeypatch.setenv("TRID3NT_OBJECT_STORE", "s3")
    monkeypatch.setattr(ep, "RUNS_BUCKET", "runs-b")
    ep._write_completion(
        run_id="RID", status="ok", exit_code=0, converged=True,
        model_crs="EPSG:32617", output_uris=["s3://runs-b/RID/x.tif"],
        stdout_uri=None, stderr_uri=None, started_at="t", error=None,
        publish_manifest_uri="s3://runs-b/RID/publish_manifest.json",
        deck={"archetype": None, "model_crs": "EPSG:32617"},
        error_code=None,
    )
    import json

    body = json.loads(captured["Body"].decode("utf-8"))
    assert body["publish_manifest_uri"] == "s3://runs-b/RID/publish_manifest.json"
    assert body["deck"]["model_crs"] == "EPSG:32617"
    assert body["error_code"] is None
    # Legacy completion keys still present.
    for k in ("run_id", "status", "exit_code", "converged", "model_crs",
              "output_uris", "started_at", "finished_at", "error"):
        assert k in body


def test_validate_job_spec_gate():
    from services.workers._modflow_build import validate_job_spec

    ok = validate_job_spec({
        "schema_version": 1, "engine": "modflow",
        "run_args": {
            "spill_location_latlon": [40.0, -96.0], "contaminant": "x",
            "release_rate_kg_s": 1.0, "duration_days": 5.0,
        },
    })
    assert ok["run_args"]["spill_location_latlon"] == [40.0, -96.0]
    with pytest.raises(ValueError):
        validate_job_spec({"schema_version": 1, "run_args": {}})


def test_validate_job_spec_rejects_unknown_top_level_field():
    """ADR 0158: an unknown TOP-LEVEL job_spec key errors loudly instead of
    silently vanishing (the ADR 0148 lesson). run_args internals stay open
    (unpacked as **kwargs against build_modflow_deck's typed signature, which
    already raises TypeError on an unknown run_args key)."""
    from services.workers._modflow_build import validate_job_spec

    with pytest.raises(ValueError, match="typo_field_name"):
        validate_job_spec({
            "schema_version": 1, "engine": "modflow",
            "typo_field_name": 1.0,
            "run_args": {
                "spill_location_latlon": [40.0, -96.0], "contaminant": "x",
                "release_rate_kg_s": 1.0, "duration_days": 5.0,
            },
        })


def test_build_modflow_deck_rejects_unknown_run_args_key_via_typeerror():
    """The run_args passthrough is ALREADY strict by construction: an unknown
    optional-knob key survives validate_job_spec (run_args stays open) but
    build_deck_kwargs_from_spec unpacks it as **kwargs against
    build_modflow_deck's fully-typed signature, so a typo'd knob raises a loud
    TypeError rather than silently no-op'ing (never reaching mf6)."""
    from services.workers._modflow_build import (
        build_deck_kwargs_from_spec,
        validate_job_spec,
    )
    from services.workers.modflow import gwt_adapter

    spec = validate_job_spec({
        "schema_version": 1, "engine": "modflow",
        "run_args": {
            "spill_location_latlon": [40.0, -96.0], "contaminant": "x",
            "release_rate_kg_s": 1.0, "duration_days": 5.0,
            "aquifer_k_m_s": 1e-4,  # typo of aquifer_k_ms
        },
    })
    deck_kwargs = build_deck_kwargs_from_spec(spec)
    with pytest.raises(TypeError, match="aquifer_k_m_s"):
        gwt_adapter.build_modflow_deck(workdir="/tmp/does-not-run", write=False, **deck_kwargs)


def test_parser_version_is_bumped_for_adr_0215_fields():
    """The parser version names the ADR 0215 wellhead-reeval part-2 fields."""
    from services.workers._modflow_build.spec import _PARSER_VERSION

    assert _PARSER_VERSION == "modflow-jobspec-2"


@pytest.mark.parametrize(
    "field, value",
    [
        ("wells", [{"lon": -98.42, "lat": 40.905, "rate_m3_day": 1200.0, "name": "M-1"}]),
        ("capture_zone_transient", True),
        ("river_reaches", [[[-98.45, 40.92], [-98.43, 40.905]]]),
    ],
)
def test_job_spec_accepts_adr_0215_capture_zone_fields(field, value, tmp_path):
    """Each new ADR 0215 run_arg flows through the OPEN passthrough into the typed
    build_modflow_deck signature and is CONSUMED (builds a deck) -- not dropped."""
    from services.workers._modflow_build import (
        build_deck_kwargs_from_spec,
        validate_job_spec,
    )
    from services.workers.modflow import gwt_adapter

    spec = validate_job_spec({
        "schema_version": 1, "engine": "modflow",
        "run_args": {
            "spill_location_latlon": [40.905, -98.42], "contaminant": "n/a",
            "release_rate_kg_s": 1.0, "duration_days": 1.0,
            "aquifer_k_ms": 2.48e-6, "porosity": 0.198,
            "archetype": "capture_zone",
            field: value,
        },
    })
    deck_kwargs = build_deck_kwargs_from_spec(spec)
    deck = gwt_adapter.build_modflow_deck(workdir=str(tmp_path), write=False, **deck_kwargs)
    assert deck.archetype == "capture_zone"


@pytest.mark.parametrize(
    "typo",
    ["well", "capture_zone_transients", "river_reach", "starting_head_by_cells"],
)
def test_job_spec_rejects_typo_of_adr_0215_fields_via_typeerror(typo, tmp_path):
    """A MISNAMED variant of any new field raises a loud TypeError at the deck
    call (ADR 0148 guard), never silently no-op'ing the intended field."""
    from services.workers._modflow_build import (
        build_deck_kwargs_from_spec,
        validate_job_spec,
    )
    from services.workers.modflow import gwt_adapter

    spec = validate_job_spec({
        "schema_version": 1, "engine": "modflow",
        "run_args": {
            "spill_location_latlon": [40.905, -98.42], "contaminant": "n/a",
            "release_rate_kg_s": 1.0, "duration_days": 1.0,
            "archetype": "capture_zone",
            typo: True,
        },
    })
    deck_kwargs = build_deck_kwargs_from_spec(spec)
    with pytest.raises(TypeError, match=typo):
        gwt_adapter.build_modflow_deck(workdir=str(tmp_path), write=False, **deck_kwargs)
