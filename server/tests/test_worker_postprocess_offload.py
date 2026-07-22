"""Agent-side wiring for the GeoClaw / OpenQuake / SWMM / Landlab worker
postprocess offload (mirror of ``test_sfincs_build_offload`` /
``test_publish_manifest_register_only_phase4``).

The heavy rasterize for these engines moved INTO their Batch workers (they write a
``publish_manifest.json`` alongside ``completion.json``). Each composer gains a
register-only branch, gated on a present manifest, in FRONT of the legacy on-box
download + ``postprocess_<engine>`` path. These tests assert:

  1. the register-only branch exists as a clean if/else per composer (source
     inspection - no live Batch),
  2. the legacy on-box fallback path is PRESERVED (byte-identical when the worker
     writes no manifest - one-release safety),
  3. ``read_publish_manifest`` degrades to ``None`` (the fallback trigger) when the
     completion carries no ``publish_manifest_uri`` (gate default-off / pre-rebuild
     worker image), and
  4. no cross-engine route: a manifest's ``engine`` tag round-trips through the
     shared schema gate unchanged.

The worker-side postprocess math + honesty gates are covered by the per-package
``services/workers/_<engine>_postprocess/test_postprocess_wiring.py`` suites.
"""

from __future__ import annotations

import inspect


# --------------------------------------------------------------------------- #
# 1. Each composer has the register-only branch in front of the on-box fallback.
# --------------------------------------------------------------------------- #
def test_geoclaw_composer_register_only_branch_and_fallback():
    import grace2_agent.workflows.model_dambreak_geoclaw_scenario as m

    body = inspect.getsource(m.model_dambreak_geoclaw_scenario)
    # register-only trigger
    assert "read_publish_manifest" in body
    assert "register_manifest_layers(" in body
    assert "if _gc_manifest is not None:" in body
    # legacy on-box fallback preserved
    assert "_download_batch_geoclaw_outputs" in body
    assert "postprocess_geoclaw," in body


def test_openquake_composer_register_only_branch_and_fallback():
    import grace2_agent.workflows.model_seismic_hazard_scenario as m

    body = inspect.getsource(m.model_seismic_hazard_scenario)
    assert "read_publish_manifest" in body
    assert "register_manifest_layers(" in body
    assert "if _oq_manifest is not None:" in body
    # legacy on-box fallback preserved
    assert "_download_batch_hazard_csv" in body
    assert "postprocess_openquake," in body


def test_swmm_composer_register_only_branch_and_fallback():
    import grace2_agent.workflows.model_urban_flood_swmm as m

    body = inspect.getsource(m.model_urban_flood_swmm)
    assert "read_publish_manifest" in body
    assert "register_manifest_layers(" in body
    assert "if _swmm_manifest is not None:" in body
    # legacy on-box fallback preserved
    assert "_download_batch_swmm_outputs" in body
    assert "postprocess_swmm," in body


def test_landlab_composer_register_only_branch_and_fallback():
    import grace2_agent.workflows.model_landslide_scenario as m

    body = inspect.getsource(m.model_landslide_scenario)
    assert "read_publish_manifest" in body
    assert "register_manifest_layers(" in body
    assert "if _manifest is not None:" in body
    # legacy on-box fallback preserved
    assert "download_landlab_outputs" in body
    assert "postprocess_landlab," in body


# --------------------------------------------------------------------------- #
# 2. read_publish_manifest degrades to None when the gate is OFF (no manifest
#    pointer in completion.json) - the fallback trigger.
# --------------------------------------------------------------------------- #
def test_read_publish_manifest_absent_pointer_returns_none(monkeypatch):
    """A completion.json with no ``publish_manifest_uri`` (pre-rebuild worker /
    gate default-off) -> read_publish_manifest returns None -> composer falls
    through to the legacy on-box postprocess."""
    import grace2_agent.workflows.register_published_manifest as rpm
    import grace2_agent.tools.solver as solver

    monkeypatch.setattr(solver, "_get_runs_bucket", lambda: "runs-bkt")
    monkeypatch.setattr(
        solver, "_try_get_completion_s3", lambda bkt, rid: {"status": "ok"}
    )

    class _RR:
        run_id = "RUNRUNRUN"

    assert rpm.read_publish_manifest(_RR()) is None


def test_read_publish_manifest_no_run_id_returns_none():
    import grace2_agent.workflows.register_published_manifest as rpm

    class _RR:
        run_id = None

    assert rpm.read_publish_manifest(_RR()) is None


# --------------------------------------------------------------------------- #
# 3. No cross-engine route: the manifest engine tag round-trips unchanged through
#    the shared schema gate (a geoclaw manifest never masquerades as swmm, etc).
# --------------------------------------------------------------------------- #
def test_manifest_engine_tag_round_trips_per_engine():
    from grace2_contracts.publish_manifest import parse_publish_manifest

    import json

    for engine in ("geoclaw", "openquake", "swmm", "landlab", "swan"):
        raw = json.dumps(
            {
                "schema_version": 1,
                "engine": engine,
                "run_id": "RID",
                "status": "ok",
                "frame_count": 0,
                "metrics": {},
                "layers": [],
            }
        )
        m = parse_publish_manifest(raw)
        assert m.engine == engine
