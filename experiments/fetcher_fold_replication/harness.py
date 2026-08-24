"""Replication-parity harness (router-pilot-contract sec 4.2).

For each pilot, run the hand-written TWIN and the spec-driven ROUTER with the
SAME fixed request and IDENTICAL synthetic upstream, then compare envelopes
field-by-field (values / layer-output / caveats) plus one forced upstream-failure
path. Deterministic + offline: the cache read_through is stubbed (no MinIO) and
every network seam is monkeypatched to a shared synthetic payload, so twin and
router consume byte-identical upstream and the ONLY thing that can differ is the
implementation body (twin Python vs spec+router) -- the fair-A/B the fold gate
needs. This offline gate is the sanctioned deterministic path (offline-first
rule); a live drive against the real endpoints is NOT the gate -- the router-
executor gaps VERDICT.md enumerates (esri STAC stub, coops YYYYMMDD date format,
hifld facility routing, census values protocol) would fail live regardless and
are REPORTED rather than exercised as flaky live drives.

Twin behavior is the contract. A divergence the spec cannot close is REPORTED
(never fudged): the per-source verdict records it with a precise root cause.
"""

from __future__ import annotations

import io
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable
from unittest import mock

# --------------------------------------------------------------------------- #
# Result records.
# --------------------------------------------------------------------------- #


@dataclass
class Check:
    name: str
    ok: bool
    twin: Any = None
    router: Any = None
    note: str = ""
    #: A recorded-but-non-gating divergence whose ROOT CAUSE is a twin defect the
    #: router must NOT copy (standing directive: do not silently copy a defect,
    #: flag for NATE). Distinct from a fudged PASS -- ``ok`` is honestly False,
    #: it is simply excluded from the gate because the twin, not the router, is
    #: wrong. Surfaced loudly in the verdict + report.
    reported: bool = False


@dataclass
class SourceResult:
    source: str
    checks: list[Check] = field(default_factory=list)
    error: str | None = None

    def add(self, name: str, ok: bool, twin: Any = None, router: Any = None,
            note: str = "", reported: bool = False) -> None:
        self.checks.append(Check(name, ok, twin, router, note, reported))

    @property
    def n_ok(self) -> int:
        return sum(1 for c in self.checks if c.ok)

    @property
    def n_total(self) -> int:
        return len(self.checks)

    @property
    def gate_checks(self) -> list[Check]:
        """Every contract-4.2 gating check (excludes non-gating info.* and the
        reported twin-defect divergences)."""
        return [
            c for c in self.checks
            if not c.reported and (
                c.name.startswith("values.")
                or c.name.startswith("schema.")
                or c.name.startswith("layer.")     # incl. layer.bbox_present (4.2 layer-output)
                or c.name.startswith("error.")     # empty + upstream + every invalid-param class
                or c.name.startswith("gate.")      # conus / max_bbox / cap parity
                or c.name == "caveats.reproduced"
            )
        ]

    @property
    def verdict(self) -> str:
        """PASS / PARTIAL / BLOCK per the contract sec 4.2 gate.

        GATE (all must match for PASS): values.* + schema.* (both VALUES fields --
        "property schema / column set" + timestamp spot-check) + layer.* (incl.
        bbox present/absent, a 4.2 layer-output field) + error.* (empty + upstream
        + EVERY invalid-param class: malformed bbox / out-of-range year+date /
        bad enum) + gate.* (conus / max_bbox / cap parity) + caveats.reproduced.
        Non-gating: info.* (cosmetic) and ``reported`` twin-defect divergences
        (recorded honestly, root-caused to a twin bug, flagged for NATE). A
        values/schema divergence BLOCKS; a non-value gate divergence (layer /
        error-prefix / gate) is PARTIAL.
        """
        if self.error:
            return "ERROR"
        gate = self.gate_checks
        values_checks = [
            c for c in self.checks
            if not c.reported and (c.name.startswith("values.") or c.name.startswith("schema."))
        ]
        if gate and all(c.ok for c in gate):
            return "PASS"
        if values_checks and all(c.ok for c in values_checks):
            return "PARTIAL"
        return "BLOCK"


# --------------------------------------------------------------------------- #
# read_through stub (no MinIO) + artifact parsers.
# --------------------------------------------------------------------------- #


def _make_stub_read_through(sink: dict[str, bytes]):
    """A read_through that runs fetch_fn() offline and returns a fake-uri result.

    Mirrors the real seam's contract (ReadThroughResult(uri, data, hit)) so both
    the twin and router build their LayerURI identically -- only the fetched
    bytes come from the synthetic upstream, no S3/MinIO round-trip.
    """
    from trid3nt_server.tools.cache import ReadThroughResult

    def _stub(metadata, params, ext, fetch_fn):  # noqa: ANN001
        data = fetch_fn()
        src = getattr(metadata, "source_class", "src")
        ttl = getattr(metadata, "ttl_class", "static-30d")
        uri = f"s3://trid3nt-cache/cache/{ttl}/{src}/fixture.{ext}"
        sink["bytes"] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return _stub


def raster_stats(b: bytes) -> dict[str, Any]:
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile

    with MemoryFile(b) as mf, mf.open() as ds:
        arr = ds.read(1).astype("float64")
        nod = ds.nodata
        finite = arr[np.isfinite(arr)]
        if nod is not None and nod == nod:  # non-nan nodata (e.g. 0)
            finite = finite[finite != nod]
        bounds = tuple(round(float(v), 4) for v in ds.bounds)
        return {
            "band_count": ds.count,
            "dtype": ds.dtypes[0],
            "crs": str(ds.crs),
            "nodata": (None if nod is None else ("nan" if nod != nod else float(nod))),
            "bounds": bounds,
            "min": (None if finite.size == 0 else round(float(finite.min()), 4)),
            "max": (None if finite.size == 0 else round(float(finite.max()), 4)),
            "mean": (None if finite.size == 0 else round(float(finite.mean()), 4)),
        }


def vector_info(b: bytes) -> dict[str, Any]:
    import geopandas as gpd

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(b)
    try:
        gdf = gpd.read_file(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    geom_types = sorted({g.geom_type for g in gdf.geometry if g is not None}) if len(gdf) else []
    non_geom = [c for c in gdf.columns if c != "geometry"]
    return {
        "n": int(len(gdf)),
        "geom_types": geom_types,
        "crs": str(gdf.crs),
        "columns": set(non_geom),
        "gdf": gdf,
    }


def err_frame(exc: BaseException) -> dict[str, Any]:
    return {
        "type": type(exc).__name__,
        "error_code": getattr(exc, "error_code", None),
        "retryable": getattr(exc, "retryable", None),
    }


@contextmanager
def _patched(*patchers):
    started = [p.start() for p in patchers]
    try:
        yield started
    finally:
        for p in patchers:
            p.stop()


# --------------------------------------------------------------------------- #
# Spec access.
# --------------------------------------------------------------------------- #


def load_specs():
    from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

    return compose_specs_from_tree()


def route_layer(spec, params, sink):
    """Run router.route with read_through stubbed; return the LayerURI."""
    from trid3nt_server.tools.fetchers._router import router as router_mod

    stub = _make_stub_read_through(sink)
    with _patched(mock.patch.object(router_mod, "read_through", stub)):
        return router_mod.route(spec, params)


# --------------------------------------------------------------------------- #
# Fake httpx response.
# --------------------------------------------------------------------------- #


class FakeResp:
    def __init__(self, json_body=None, status_code=200, text="", headers=None):
        self._json = json_body
        self.status_code = status_code
        self.text = text
        self.headers = headers or {"content-type": "application/json"}
        self.url = "http://fixture.local/"

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        import httpx

        if self.status_code >= 400:
            raise httpx.HTTPStatusError("fixture error", request=None, response=None)
