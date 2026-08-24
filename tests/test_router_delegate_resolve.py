"""hooks.delegate_resolve mechanism (ADR 0076): the socketed pre-cache-key resolve.

The delegate sibling of the chained-resolution resolve phase, for a source whose
cycle/key resolution walks a LIBRARY socket (HRRR-Zarr's s3fs backward cycle walk).
It runs in route() AFTER type/gate + delegate_validate and BEFORE read_through, under
the library_delegate constraints (declared timeout, telemetry, upstream backstop), and
MERGES its dict return into params so the resolved cycle enters the cache key.

The HRRR pair (its consuming fold) is a live-S3 zarr data path with no offline value
fixture, so it is STOP-RULED (twins intact) with this mechanism BUILT + proven here:
no-op for a spec that omits it, the pre-cache merge (distinct cache keys), the typed
upstream backstop on an unmapped library error, and the registration pairing gate.
Offline: a stub raster delegate + a stub resolve hook; read_through is monkeypatched to
capture the params it keys on.
"""

from __future__ import annotations

import numpy as np
import pytest
from trid3nt_contracts.source_spec import SourceSpec

from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import RouterUpstreamError
from trid3nt_server.tools.fetchers._router.executors import library_delegate
from trid3nt_server.tools.fetchers._router.hooks import register_hook, HOOK_REGISTRY


# --- stub hooks (registered once; register_hook raises on a dup of a DIFFERENT fn) --

def _ensure_hook(name, fn):
    if name not in HOOK_REGISTRY:
        register_hook(name)(fn)


def _stub_delegate(spec, params, *, timeout_s):
    # A tiny synthetic (array, transform, crs) so the raster COG writer succeeds.
    from affine import Affine
    arr = np.array([[1.0, 2.0], [3.0, 4.0]], dtype="float32")
    return arr, Affine(0.5, 0, -100.0, 0, -0.5, 40.0), "EPSG:4326"


def _stub_resolve(spec, params, *, timeout_s):
    # Reproduce the HRRR shape: a cycle=None request resolves a concrete cycle that
    # merges into params (so a non-deterministic key becomes deterministic).
    return {"cycle": "20260608_12z"}


def _boom_resolve(spec, params, *, timeout_s):
    raise RuntimeError("s3fs cycle walk exploded")


def _bad_type_resolve(spec, params, *, timeout_s):
    return ["not", "a", "dict"]


_ensure_hook("t_delres.delegate", _stub_delegate)
_ensure_hook("t_delres.resolve", _stub_resolve)
_ensure_hook("t_delres.boom", _boom_resolve)
_ensure_hook("t_delres.badtype", _bad_type_resolve)


def _spec(resolve_hook: str | None) -> SourceSpec:
    hooks = {"delegate": "t_delres.delegate"}
    if resolve_hook is not None:
        hooks["delegate_resolve"] = resolve_hook
    return SourceSpec.model_validate(
        {
            "name": "t_delres_source",
            "source_class": "t_delres",
            "shape": "raster-cog",
            "endpoints": {"main": {"url": "https://example.invalid/zarr"}},
            "params": {"cycle": {"type": "str", "schema_optional": True}},
            "ingest": {"access": "library_delegate", "delegate": {"library": "zarr-stub", "timeout_s": 5.0}},
            "hooks": hooks,
            "output": {"layer_type": "raster", "ext": "tif", "style_preset": "continuous_dem"},
            "cache": {"ttl_class": "static-30d"},
            "payload_estimate": {"model": "bbox_area", "floor_mb": 0.01},
        }
    )


# --------------------------------------------------------------------------- #
# library_delegate.resolve() unit.
# --------------------------------------------------------------------------- #


def test_resolve_noop_when_unset():
    assert library_delegate.resolve(_spec(None), {"cycle": None}) == {}


def test_resolve_returns_merge_dict():
    assert library_delegate.resolve(_spec("t_delres.resolve"), {"cycle": None}) == {"cycle": "20260608_12z"}


def test_resolve_backstops_unmapped_library_error():
    with pytest.raises(RouterUpstreamError) as exc:
        library_delegate.resolve(_spec("t_delres.boom"), {"cycle": None})
    assert exc.value.error_code == "T_DELRES_UPSTREAM_ERROR"
    assert "cycle walk exploded" in str(exc.value)


def test_resolve_rejects_non_dict_return():
    with pytest.raises(RouterUpstreamError):
        library_delegate.resolve(_spec("t_delres.badtype"), {"cycle": None})


# --------------------------------------------------------------------------- #
# route(): the resolved value merges into params BEFORE the cache key.
# --------------------------------------------------------------------------- #


def _capture_read_through(monkeypatch) -> dict:
    seen: dict = {}
    from trid3nt_server.tools.cache import ReadThroughResult

    def patched(metadata, params, ext, fetch_fn, **kw):
        seen["params"] = dict(params)
        return ReadThroughResult(uri="s3://fake/t_delres.tif", data=b"", hit=True)

    monkeypatch.setattr(router, "read_through", patched)
    return seen


def test_route_merges_resolved_cycle_into_cache_params(monkeypatch):
    seen = _capture_read_through(monkeypatch)
    layer = router.route(_spec("t_delres.resolve"), {"cycle": None})
    # The resolved cycle is what read_through (the cache key) sees, not the None input.
    assert seen["params"]["cycle"] == "20260608_12z"
    assert layer.layer_type == "raster"


def test_route_noop_without_resolve_leaves_params(monkeypatch):
    seen = _capture_read_through(monkeypatch)
    router.route(_spec(None), {"cycle": None})
    # No resolve hook -> params carry the validated input unchanged (cycle stays absent/None).
    assert seen["params"].get("cycle") in (None,)


# --------------------------------------------------------------------------- #
# Registration pairing gate: delegate_resolve requires delegate.
# --------------------------------------------------------------------------- #


def test_registration_rejects_resolve_without_delegate():
    from trid3nt_server.tools.fetchers._router.registration import _validate_hooks
    from trid3nt_server.tools.fetchers._router.hooks import HookResolutionError

    spec = SourceSpec.model_validate(
        {
            "name": "t_delres_bad",
            "source_class": "t_delres_bad",
            "shape": "raster-cog",
            "endpoints": {"main": {"url": "https://example.invalid/zarr"}},
            "ingest": {"access": "library_delegate"},
            "hooks": {"delegate_resolve": "t_delres.resolve"},
            "output": {"layer_type": "raster", "ext": "tif", "style_preset": "continuous_dem"},
            "cache": {"ttl_class": "static-30d"},
            "payload_estimate": {"model": "bbox_area", "floor_mb": 0.01},
        }
    )
    with pytest.raises(HookResolutionError):
        _validate_hooks(spec)
