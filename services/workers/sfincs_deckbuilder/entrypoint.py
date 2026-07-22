#!/usr/bin/env python3
"""COMBINED coastal quadtree worker — BUILD + SOLVE in ONE Batch job.

This worker fuses what used to be two separate one-shot Batch workers into a
single image + a single job-definition:

  1. the GPL deck-builder (``cht_sfincs`` authors a refined multi-level quadtree
     + SnapWave deck from a build-spec JSON), and
  2. the MIT solve shim (``/usr/local/bin/sfincs`` runs the deck in-place and
     writes ``sfincs_map.nc``).

Before the combine, the agent reached these over an S3 + Batch-submit seam with
TWO job submissions, TWO completion polls, and one S3 round-trip of the deck
(deckbuilder uploads the deck + manifest.json; the solve worker re-downloads
them). The combined worker eliminates the round-trip: after ``build_deck()``
populates a LOCAL deck dir, the same process invokes the SFINCS binary directly
on that dir (no download), uploads ``sfincs_map.nc`` + stdout/stderr, and writes
ONE ``completion.json``. The agent collapses to ONE submit + ONE poll against
ONE new job-def (``TRID3NT_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE``).

What the combined worker adds on top of the deck-build half:

  * AUTO-REFINEMENT — derives the cht refinement polygons (a GeoDataFrame with a
    descending ``refinement_level`` column) from the inputs rather than relying
    on a pre-baked URI: the topobathy 0 m NAVD88 contour buffered (finest), the
    nearshore ~-2..0 m band, a slope threshold, OSM river centerlines buffered,
    and OSM building footprints buffered. The agent may still hand a
    ``grid.refinement_polygons_uri`` (the explicit/legacy path) — it is unioned
    in. See :func:`derive_refinement_polygons`.
  * BUDGET CAP — estimates the resulting quadtree cell count and reduces the max
    refinement level (and, last resort, the refinement extent) until it fits the
    spec's ``grid.max_cells`` budget, logging exactly what it coarsened. This
    generalizes the regular-grid autoscale spirit to the quadtree.
    See :func:`apply_cell_budget`.
  * BUILDING OBSTACLES — burns OSM footprints into the deck so water routes
    AROUND buildings: ``thin_dams`` along footprint exterior edges (blocked
    uv-faces, the default), OR raised ``z`` at footprint cells, OR an exclude
    mask (dropped cells). See :func:`burn_building_obstacles`.

Two FIXED caveats vs the spike's proven (but flawed) deck are preserved:
    CAVEAT 1 — SnapWave forcing time column is tref-RELATIVE (0.0, 7200.0, ...),
               NOT the SnapWave-internal epoch seconds the spike emitted.
               Enforced two ways: (a) tref/tstart/tstop set as proper datetimes
               anchored so cht's ``(time - tref).total_seconds()`` already yields
               tref-relative values, and (b) a post-write normalizer that
               rewrites any bhs/btp/bwd/bds whose first time column is not
               0-anchored.
    CAVEAT 2 — snapwave_use_herbers = 1 (infragravity wave run-up), NOT 0.

GPL note: ``cht_sfincs`` is GPL-3.0 and stays IMAGE-ONLY (imported lazily inside
``build_deck`` / the refinement + obstacle helpers, NEVER by agent code). The
combined image bases on the ``deltares/sfincs-cpu`` solve image (for
``/usr/local/bin/sfincs``) AND carries the cht venv; the agent reaches this
worker arms-length over the object-store + Batch-submit seam exactly as before.

Contract:

    Input (CLI or env):
        --run-id RUN_ID                  ($TRID3NT_RUN_ID)
            Run identifier. completion.json + outputs land under
            {scheme}://${TRID3NT_RUNS_BUCKET}/${RUN_ID}/.
        --build-spec-uri s3://.../build_spec.json   ($TRID3NT_BUILD_SPEC_URI)
            JSON build spec (schema_version "v2"). See the module docstring of
            ``validate_build_spec`` / the build_spec_contract scout note for the
            full shape: aoi, topobathy COG, grid + refinement + max_cells,
            mask, buildings, rivers, snapwave, forcing, output.

    Output (all under {scheme}://${RUNS_BUCKET}/${RUN_ID}/):
        sfincs_map.nc                    the load-bearing flood output
        sfincs.stdout / sfincs.stderr    binary run logs
        manifest.json                    audit (the deck->solve manifest)
        deck/<file>                      optional deck audit upload
        completion.json                  UNION of the deck + solve schemas — the
                                         SAME object the agent's
                                         ``wait_for_completion`` polls identically.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

LOG = logging.getLogger("trid3nt.worker.sfincs_quadtree")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

SCRATCH = Path(os.environ.get("TRID3NT_DECK_SCRATCH", "/opt/grace2/work"))
GCP_PROJECT = os.environ.get("GCP_PROJECT", "legacy-cloud-project")
RUNS_BUCKET = os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")

# The SFINCS binary the combined image carries (from deltares/sfincs-cpu). The
# combined worker invokes it IN-PROCESS on the local deck dir after build.
SFINCS_BIN = os.environ.get("TRID3NT_SFINCS_BIN", "/usr/local/bin/sfincs")

# Deck files cht writes, in the order the solve half expects them. Globbed at
# runtime; this constant only documents the canonical set.
DECK_GLOB = "**/*"

# SnapWave time-series ascii files whose first column must be tref-relative.
SNAPWAVE_TS_FILES = (
    "snapwave.bhs",
    "snapwave.btp",
    "snapwave.bwd",
    "snapwave.bds",
)

SFINCS_TIME_FMT = "%Y%m%d %H%M%S"

# SFINCS outputs to upload after the solve (glob patterns, expanded under the
# deck dir). sfincs_map.nc is the load-bearing flood output; *.nc / *.tif sweep
# any extra outputs (his, point series, derived rasters). mesh.geojson is the
# best-effort quadtree-mesh layer emit_quadtree_mesh_geojson writes at the end of
# build_deck (EPSG:4326 active-cell polygons w/ per-cell level + size_m); the
# existing _expand_outputs sweep uploads it to <run_id>/mesh.geojson with no new
# upload code.
SOLVE_OUTPUT_PATTERNS = ("sfincs_map.nc", "*.nc", "*.tif", "mesh.geojson")

#: Default grid CRS when the build-spec leaves ``aoi.target_epsg`` unset — the
#: fetch_topobathy default (UTM 16N / Mexico Beach zone, the coastal North Star).
DEFAULT_TARGET_EPSG = 32616

#: Default cell budget when the spec omits ``grid.max_cells``. A quadtree this
#: size builds + solves comfortably inside a c7i-class Batch box; the budget cap
#: coarsens refinement levels until the estimate fits.
DEFAULT_MAX_CELLS = 2_000_000

#: Cap on the number of features written to the best-effort mesh.geojson layer.
#: The client renders the quadtree mesh as a vector overlay; a multi-million-cell
#: quadtree would produce a GeoJSON too large to ship/draw, so we deterministically
#: decimate (every Nth active cell) down to this many features and RECORD the cap +
#: decimation factor in the deck provenance (NO silent drop).
MESH_GEOJSON_MAX_FEATURES = 20_000


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Object-store abstraction — scheme-dispatched s3:// / gs:// (mirror of the
# solve worker's _download/_upload). Lazy SDK imports so a pure-S3 Batch image
# never pays for the GCP SDK.
# --------------------------------------------------------------------------- #


def _split_object_uri(uri: str) -> tuple[str, str, str]:
    """Split ``s3://bucket/key`` / ``gs://bucket/key`` → (scheme, bucket, key)."""
    for scheme in ("s3", "gs"):
        prefix = f"{scheme}://"
        if uri.startswith(prefix):
            bucket, _, key = uri[len(prefix):].partition("/")
            if not bucket or not key:
                raise ValueError(f"malformed {scheme}:// URI: {uri!r}")
            return scheme, bucket, key
    raise ValueError(
        f"unsupported object URI scheme: {uri!r} (expected s3:// or gs://)"
    )


def _output_scheme() -> str:
    """Runs-bucket output scheme — ``s3`` or ``gs`` (env TRID3NT_OBJECT_STORE)."""
    b = (os.environ.get("TRID3NT_OBJECT_STORE") or "gcs").strip().lower()
    return "s3" if b in {"s3", "aws"} else "gs"


def _runs_uri(run_id: str, rel: str) -> str:
    return f"{_output_scheme()}://{RUNS_BUCKET}/{run_id}/{rel}"


_GCS_CLIENT: Any = None
_S3_CLIENT: Any = None


def _gcs_client() -> Any:
    global _GCS_CLIENT
    if _GCS_CLIENT is None:
        from google.cloud import storage  # type: ignore

        _GCS_CLIENT = storage.Client(project=GCP_PROJECT)
    return _GCS_CLIENT


def _s3_client() -> Any:
    global _S3_CLIENT
    if _S3_CLIENT is None:
        import boto3  # type: ignore

        _S3_CLIENT = boto3.client(
            "s3", region_name=os.environ.get("AWS_REGION", "us-west-2")
        )
    return _S3_CLIENT


def _download(uri: str, dest: Path) -> None:
    scheme, bucket, key = _split_object_uri(uri)
    dest.parent.mkdir(parents=True, exist_ok=True)
    LOG.info("downloading %s -> %s", uri, dest)
    if scheme == "s3":
        resp = _s3_client().get_object(Bucket=bucket, Key=key)
        with dest.open("wb") as fh:
            shutil.copyfileobj(resp["Body"], fh)
        return
    _gcs_client().bucket(bucket).blob(key).download_to_filename(str(dest))


def _upload(src: Path, uri: str, content_type: str | None = None) -> str:
    scheme, bucket, key = _split_object_uri(uri)
    LOG.info("uploading %s -> %s", src, uri)
    if scheme == "s3":
        extra = {"ContentType": content_type} if content_type else {}
        with src.open("rb") as fh:
            _s3_client().put_object(Bucket=bucket, Key=key, Body=fh, **extra)
        return uri
    blob = _gcs_client().bucket(bucket).blob(key)
    if content_type:
        blob.upload_from_filename(str(src), content_type=content_type)
    else:
        blob.upload_from_filename(str(src))
    return uri


def _read_json(uri: str) -> dict:
    scheme, bucket, key = _split_object_uri(uri)
    LOG.info("reading json %s", uri)
    if scheme == "s3":
        resp = _s3_client().get_object(Bucket=bucket, Key=key)
        text = resp["Body"].read().decode("utf-8")
    else:
        text = _gcs_client().bucket(bucket).blob(key).download_as_text()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("build-spec must be a JSON object")
    return data


def _put_json(payload: dict, uri: str) -> str:
    scheme, bucket, key = _split_object_uri(uri)
    body = json.dumps(payload, indent=2)
    if scheme == "s3":
        _s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="application/json",
        )
    else:
        _gcs_client().bucket(bucket).blob(key).upload_from_string(
            body, content_type="application/json"
        )
    LOG.info("wrote json -> %s", uri)
    return uri


# --------------------------------------------------------------------------- #
# Pure-Python build-spec validation + helpers (NO cht_sfincs import — unit
# tested without the GPL library).
# --------------------------------------------------------------------------- #


class BuildSpecError(ValueError):
    """Raised when the build spec is malformed or missing required fields."""


def parse_sfincs_time(value: Any) -> _dt.datetime:
    """Parse a forcing time into a ``datetime`` (naive, tz-agnostic).

    Accepts the SFINCS ascii form ``"YYYYMMDD HHMMSS"`` (what sfincs.inp uses),
    ISO-8601 (``"2018-10-10T00:00:00Z"`` / ``"2018-10-10 00:00:00"``), or an
    already-parsed ``datetime``. Returning a real ``datetime`` is load-bearing:
    cht computes the SnapWave time column as ``(time - tref).total_seconds()``,
    so tref/tstart/tstop being PROPER datetimes (not strings, not epoch ints) is
    what makes the written time column tref-relative (CAVEAT 1).
    """
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo is None else value.replace(tzinfo=None)
    if not isinstance(value, str):
        raise BuildSpecError(f"unparseable time value: {value!r}")
    raw = value.strip()
    # SFINCS ascii form first (the canonical sfincs.inp representation).
    try:
        return _dt.datetime.strptime(raw, SFINCS_TIME_FMT)
    except ValueError:
        pass
    # ISO-8601 (tolerate trailing Z).
    iso = raw[:-1] if raw.endswith("Z") else raw
    try:
        return _dt.datetime.fromisoformat(iso).replace(tzinfo=None)
    except ValueError as exc:  # noqa: TRY003
        raise BuildSpecError(
            f"time {value!r} not in SFINCS ({SFINCS_TIME_FMT!r}) or ISO-8601 form"
        ) from exc


def _require(d: dict, key: str, ctx: str) -> Any:
    if key not in d or d[key] is None:
        raise BuildSpecError(f"build-spec missing required field {ctx}.{key}")
    return d[key]


def validate_build_spec(spec: dict) -> dict:
    """Validate the build spec shape; return a normalized copy.

    Pure structural validation — no I/O, no cht. Returns a dict with parsed
    datetimes under ``_parsed_times`` and resolved output URIs so ``build_deck``
    works against clean, typed values.

    Tolerant of the agent composer's actual shape (model_flood_scenario.py
    ``_compose_and_upload_deckbuild_spec``): ``aoi.target_epsg`` / ``mask.zmin`` /
    ``mask.zmax`` may be ``None`` (defaults applied), grid params are flat under
    ``grid`` (x0/y0/nmax/mmax/dx/dy required for cht's quadtree), and the surge /
    river forcing lives under ``forcing.surge_forcing`` (materialised
    timeseries+locations URIs).

    The combined worker additionally honours (all OPTIONAL, validated leniently):
        grid.refinement_levels   int  — max auto-refinement levels (default 2)
        grid.max_cells           int  — quadtree cell budget (default 2,000,000)
        buildings.footprints_uri str  — OSM building polygons FGB
        buildings.mode           str  — thin_dams | raise_subgrid | exclude
        rivers.lines_uri         str  — OSM waterway lines FGB
    """
    if not isinstance(spec, dict):
        raise BuildSpecError("build-spec must be a JSON object")

    aoi = _require(spec, "aoi", "")
    grid = _require(spec, "grid", "")
    topobathy = _require(spec, "topobathy", "")
    output = _require(spec, "output", "")
    forcing = _require(spec, "forcing", "")

    # target_epsg is OPTIONAL in the agent spec (may be None) — default it.
    raw_epsg = aoi.get("target_epsg")
    target_epsg = int(raw_epsg) if raw_epsg is not None else DEFAULT_TARGET_EPSG

    # cht's quadtree REQUIRES the base-grid geometry. The agent spreads these
    # flat under ``grid`` from build_sfincs_model's computed params.
    for k in ("x0", "y0", "nmax", "mmax", "dx", "dy"):
        if grid.get(k) is None:
            raise BuildSpecError(
                f"build-spec grid.{k} is required for the quadtree base grid "
                "(the agent must populate grid params from build_sfincs_model)"
            )

    _require(topobathy, "cog_uri", "topobathy")
    _require(output, "deck_dir_uri", "output")
    _require(output, "manifest_uri", "output")

    deck_dir_uri = str(output["deck_dir_uri"])
    if not deck_dir_uri.endswith("/"):
        deck_dir_uri += "/"
    manifest_uri = str(output["manifest_uri"])

    tref = parse_sfincs_time(_require(forcing, "tref", "forcing"))
    tstart = parse_sfincs_time(_require(forcing, "tstart", "forcing"))
    tstop = parse_sfincs_time(_require(forcing, "tstop", "forcing"))
    if tstop <= tstart:
        raise BuildSpecError(
            f"forcing.tstop ({tstop}) must be after tstart ({tstart})"
        )

    # Cell budget + refinement levels — lenient parse with safe defaults.
    raw_max = grid.get("max_cells")
    max_cells = int(raw_max) if raw_max is not None else DEFAULT_MAX_CELLS
    if max_cells <= 0:
        raise BuildSpecError(f"grid.max_cells must be positive, got {max_cells}")
    raw_levels = grid.get("refinement_levels")
    refinement_levels = int(raw_levels) if raw_levels is not None else 2
    if refinement_levels < 0:
        raise BuildSpecError(
            f"grid.refinement_levels must be >= 0, got {refinement_levels}"
        )

    # Buildings block — validate ``mode`` if present (default thin_dams).
    buildings = spec.get("buildings") or {}
    mode = str(buildings.get("mode", "thin_dams")).strip().lower()
    if buildings.get("footprints_uri") and mode not in {
        "thin_dams",
        "raise_subgrid",
        "exclude",
    }:
        raise BuildSpecError(
            f"buildings.mode {mode!r} invalid "
            "(expected thin_dams | raise_subgrid | exclude)"
        )

    normalized = dict(spec)
    normalized["aoi"] = {**aoi, "target_epsg": target_epsg}
    normalized["grid"] = {
        **grid,
        "max_cells": max_cells,
        "refinement_levels": refinement_levels,
    }
    normalized["output"] = {
        **output,
        "deck_dir_uri": deck_dir_uri,
        "manifest_uri": manifest_uri,
    }
    normalized["_parsed_times"] = {
        "tref": tref,
        "tstart": tstart,
        "tstop": tstop,
    }
    return normalized


def resolve_forcing_blocks(spec: dict) -> dict:
    """Resolve waterlevel / discharge / snapwave forcing from EITHER shape.

    The agent composer nests materialised forcing under
    ``forcing.surge_forcing.{waterlevel,discharge}`` (each
    ``{"timeseries_uri","locations_uri",...}``). A direct caller (and the tests)
    may instead place ``waterlevel`` / ``discharge`` / ``snapwave_boundary`` at
    the top of ``forcing``. This returns a single normalised dict:
        {"waterlevel": {...}|None, "discharge": {...}|None,
         "snapwave_boundary": {...}|None}
    """
    forcing = spec.get("forcing") or {}
    surge = forcing.get("surge_forcing") or {}

    def _pick(name: str):
        block = forcing.get(name)
        if isinstance(block, dict) and block:
            return block
        block = surge.get(name)
        return block if isinstance(block, dict) and block else None

    return {
        "waterlevel": _pick("waterlevel"),
        "discharge": _pick("discharge"),
        "snapwave_boundary": _pick("snapwave_boundary"),
    }


def snapwave_inp_overrides(spec: dict) -> dict:
    """Resolve the snapwave_* sfincs.inp knobs from the spec.

    CAVEAT 2 — ``snapwave_use_herbers`` is FORCED to **1** (infragravity-wave
    run-up). The agent composer (and the spike's proven deck) emit ``0``, the
    known-bad setting; the worker is the authority on the fix, so it ignores the
    spec's ``use_herbers`` value and forces 1. A DELIBERATE opt-out exists for
    callers that truly want the Herbers path OFF: ``snapwave.force_no_herbers =
    true`` (only that explicit flag turns it back to 0).

    Also threads the SnapWave coupling cadence ``dtwave`` (the bare SFINCS input
    variable, NOT a ``snapwave_*`` knob) when the agent pins it via
    ``snapwave.dtwave``. build_deck() owns the default (the output cadence) so the
    SnapWave field re-solves every output frame rather than hourly (DEFECT 2).
    """
    sw = spec.get("snapwave") or {}
    # CAVEAT 2 fix — force infragravity run-up ON unless the deliberate escape
    # hatch is set. The bare ``use_herbers`` field the agent emits is IGNORED.
    use_herbers = 0 if bool(sw.get("force_no_herbers", False)) else 1
    knobs: dict[str, Any] = {
        "snapwave_gamma": float(sw.get("gamma", 0.8)),
        "snapwave_gammaig": float(sw.get("gammaig", 1.0)),
        "snapwave_gammax": float(sw.get("gammax", 1.0)),
        "snapwave_dtheta": float(sw.get("dtheta", 15.0)),
        "snapwave_hmin": float(sw.get("hmin", 0.1)),
        "snapwave_fw0": float(sw.get("fw0", 0.01)),
        "snapwave_crit": float(sw.get("crit", 0.01)),
        "snapwave_igwaves": int(sw.get("igwaves", 1)),
        "snapwave_nrsweeps": int(sw.get("nrsweeps", 1)),
        "snapwave_use_herbers": use_herbers,
    }
    # DEFECT 2 FIX - ``dtwave`` (SnapWave coupling cadence). Only emitted when the
    # agent pins it; else build_deck() sets v.dtwave to the output cadence. Without
    # it SFINCS re-solves SnapWave hourly (dtwave default 3600 s) while map output
    # is every output_dt -> ~12 byte-identical hm0 frames per re-solve (a static
    # wave animation -- the live "literally nothing happening" symptom).
    if sw.get("dtwave") is not None:
        knobs["dtwave"] = float(sw["dtwave"])
    return knobs


def normalize_snapwave_time_columns(
    deck_dir: Path,
    tref: _dt.datetime,
    files: tuple[str, ...] = SNAPWAVE_TS_FILES,
) -> list[str]:
    """Force the SnapWave time-series time column to be tref-RELATIVE (CAVEAT 1).

    cht writes ``dt = (time - tref).total_seconds()``. When tref/tstart/tstop are
    proper datetimes this is already 0-anchored (0.0, 7200.0, ...). But the
    spike's proven deck emitted SnapWave-internal *epoch* seconds (e.g.
    242524800.0) because a non-datetime time index slipped through. This guard
    re-reads each bhs/btp/bwd/bds, and if the FIRST time value is not ~0 (i.e.
    not tref-anchored) it re-bases the entire column by subtracting the first
    value, so column[0] == 0.0 and spacing is preserved.

    Pure-Python (whitespace-delimited ascii) — no cht / pandas dependency, so it
    is unit-testable without the GPL library. Returns the list of files rewritten.
    """
    rewritten: list[str] = []
    for fname in files:
        fpath = deck_dir / fname
        if not fpath.exists():
            continue
        lines = fpath.read_text().splitlines()
        rows: list[tuple[float, list[str]]] = []
        for ln in lines:
            parts = ln.split()
            if not parts:
                continue
            try:
                t = float(parts[0])
            except ValueError:
                # Not a numeric time row (header?) — leave the file untouched.
                rows = []
                break
            rows.append((t, parts[1:]))
        if not rows:
            continue
        first_t = rows[0][0]
        # Already tref-relative if the first timestamp is ~0 (allow tiny fp).
        if abs(first_t) <= 1.0:
            continue
        LOG.warning(
            "normalizing %s: first time column %.3f is not tref-relative; "
            "re-basing to 0.0 (CAVEAT 1)",
            fname,
            first_t,
        )
        new_lines = []
        for t, rest in rows:
            rel_t = t - first_t
            new_lines.append(
                "  ".join([f"{rel_t:.3f}", *[f"{x}" for x in rest]])
            )
        fpath.write_text("\n".join(new_lines) + "\n")
        rewritten.append(fname)
    return rewritten


def compose_manifest(deck_dir: Path, deck_dir_uri: str) -> dict:
    """Compose the run_solver-compatible manifest.json (AUDIT artefact).

    In the combined worker the solve runs IN-PROCESS on the local deck dir, so
    this manifest is no longer fed to a second job — it is written for audit /
    debug parity with the old two-stage flow (and so a deck can still be replayed
    by the standalone solve worker if ever needed). IDENTICAL shape to
    sfincs_builder.py: one ``{"gs_uri","dest"}`` per deck file (legacy field name
    ``gs_uri``; the VALUE is scheme-resolved, s3:// on Batch), plus
    ``sfincs_args=[]`` and the standard outputs glob.
    """
    files = sorted(p for p in deck_dir.glob(DECK_GLOB) if p.is_file())
    inputs = []
    for f in files:
        rel = f.relative_to(deck_dir).as_posix()
        inputs.append({"gs_uri": deck_dir_uri + rel, "dest": rel})
    return {
        "inputs": inputs,
        "sfincs_args": [],
        "outputs": list(SOLVE_OUTPUT_PATTERNS),
    }


# --------------------------------------------------------------------------- #
# Pure-Python cell-budget estimation (NO cht — unit-tested). The cht quadtree
# halves dx/dy once per refinement level, so a polygon refined to level L has
# 4**L sub-cells per base cell it covers. We estimate the cell count from the
# base grid + the per-level refinement coverage (fraction of the base grid each
# level's polygons cover) and coarsen levels until the estimate fits the budget.
# --------------------------------------------------------------------------- #


def estimate_quadtree_cells(
    nmax: int,
    mmax: int,
    level_coverage: dict[int, float],
) -> int:
    """Estimate the refined quadtree cell count.

    ``level_coverage`` maps a refinement level (1..N) -> the FRACTION of the base
    grid that gets refined to AT LEAST that level (cumulative coverage; a cell at
    level 3 is also covered by levels 1 and 2). The base grid has ``nmax*mmax``
    level-0 cells; each base cell covered to level L is replaced by 4**L finest
    sub-cells along that nesting (cht refines x2 in each axis per level). We use
    the incremental coverage per level to avoid double counting:

        cells ≈ base*(1 - cov[1])
              + Σ_{L>=1}  base * (cov[L] - cov[L+1]) * 4**L

    where cov[L] is monotonically non-increasing in L (a deeper level covers a
    subset of a shallower one) and cov[L]=0 for L beyond the max level. This is
    an UPPER-ish estimate (treats covered base cells as fully nested), which is
    the safe side for a budget cap. Pure arithmetic — no cht, unit-testable.
    """
    base = int(nmax) * int(mmax)
    if base <= 0:
        return 0
    max_level = max(level_coverage) if level_coverage else 0
    # Normalise to cumulative, monotonically non-increasing coverage in [0,1].
    cov: dict[int, float] = {}
    for lvl in range(1, max_level + 1):
        c = float(level_coverage.get(lvl, 0.0))
        cov[lvl] = max(0.0, min(1.0, c))
    # Enforce monotonic non-increasing (deeper level ⊆ shallower).
    for lvl in range(2, max_level + 1):
        cov[lvl] = min(cov[lvl], cov[lvl - 1])

    cov1 = cov.get(1, 0.0)
    total = base * (1.0 - cov1)
    for lvl in range(1, max_level + 1):
        cov_here = cov.get(lvl, 0.0)
        cov_next = cov.get(lvl + 1, 0.0)
        incremental = max(0.0, cov_here - cov_next)
        total += base * incremental * (4 ** lvl)
    return int(round(total))


def apply_cell_budget(
    nmax: int,
    mmax: int,
    level_coverage: dict[int, float],
    max_cells: int,
) -> tuple[int, list[str]]:
    """Reduce the max refinement level until the cell estimate fits the budget.

    Returns ``(allowed_max_level, notes)`` where ``allowed_max_level`` is the
    deepest refinement level kept (any polygon requesting a deeper level is
    clamped to it) and ``notes`` records what was coarsened (surfaced in the
    completion provenance + logs). Generalizes the regular-grid autoscale spirit:
    rather than shrinking dx, we drop the finest quadtree levels first (they cost
    4**L each), preserving coarse coverage of the whole AOI.

    Pure arithmetic — unit-testable without cht.
    """
    notes: list[str] = []
    max_level = max(level_coverage) if level_coverage else 0
    allowed = max_level
    while allowed > 0:
        capped = {
            lvl: cov for lvl, cov in level_coverage.items() if lvl <= allowed
        }
        est = estimate_quadtree_cells(nmax, mmax, capped)
        if est <= max_cells:
            if allowed < max_level:
                notes.append(
                    f"budget cap: reduced max refinement level "
                    f"{max_level} -> {allowed} "
                    f"(estimate {est:,} <= budget {max_cells:,})"
                )
            return allowed, notes
        notes.append(
            f"budget cap: level {allowed} estimate "
            f"{est:,} > budget {max_cells:,} — dropping to level {allowed - 1}"
        )
        allowed -= 1
    # Even the unrefined base grid is over budget — keep level 0 and warn loudly.
    base = int(nmax) * int(mmax)
    if base > max_cells:
        notes.append(
            f"budget cap: even the base grid ({base:,} cells) exceeds the "
            f"budget ({max_cells:,}) — refinement fully disabled; the agent "
            "should coarsen grid.dx/dy or shrink the AOI"
        )
    return 0, notes


# --------------------------------------------------------------------------- #
# The GPL section — cht_sfincs imported LAZILY here only. NEVER at module top
# level, NEVER in the agent. Adapts the proven spike (author_quadtree_cht.py).
# --------------------------------------------------------------------------- #


def _read_gdf(uri: str | None, scratch: Path, name: str):
    """Download + read an optional polygon/line vector into a GeoDataFrame."""
    if not uri:
        return None
    import geopandas as gpd  # type: ignore

    local = scratch / f"{name}{Path(_split_object_uri(uri)[2]).suffix or '.fgb'}"
    _download(uri, local)
    return gpd.read_file(local)


# Backwards-compatible alias retained for any external caller / test that
# imported the deck-builder-only name.
_read_polygon_gdf = _read_gdf


#: Absolute physical cap (metres) on a coastal topo-bathymetry elevation. Any
#: |z| at or above this is a nodata/fill sentinel leak, NOT a real elevation:
#: real coastal topobathy is a narrow band (roughly -50 .. +50 m for the North
#: Star AOIs), so 9000 m sits FAR above any genuine land/sea-floor value while
#: still catching the common fill sentinels (9999 / -9999 / 1e20 / 3.4e38).
_TOPOBATHY_SENTINEL_ABS = 9000.0


def _mask_topobathy_sentinels(samples, band_nodata):
    """Mask declared-nodata + common fill sentinels in a z array to NaN.

    Out-of-coverage / fill cells must become NaN (so the sfincs mask drops them
    to INACTIVE) rather than leak as a giant +9999 m "wall" into the solve
    (the live Mexico-Beach bug: ``z range -33.66 .. 9999.00 m``). We mask, in
    order:
      * the COG's DECLARED band nodata (``ds.nodata``), when set; and
      * any non-finite value (NaN / +-inf), and
      * defensively, any ``|z| >= _TOPOBATHY_SENTINEL_ABS`` — this catches the
        unflagged 9999 / -9999 / 1e20 / float32-max sentinels a source raster
        can carry even when ``ds.nodata`` is None or wrong.
    Pure-numpy so it is unit-testable without cht/rasterio.
    """
    import numpy as np  # type: ignore

    samples = np.asarray(samples, dtype="float32")
    if band_nodata is not None and np.isfinite(band_nodata):
        samples = np.where(
            samples == np.float32(band_nodata), np.float32("nan"), samples
        )
    # Defensive: any non-finite OR out-of-physical-band magnitude is a sentinel.
    sentinel = ~np.isfinite(samples) | (
        np.abs(samples) >= np.float32(_TOPOBATHY_SENTINEL_ABS)
    )
    samples = np.where(sentinel, np.float32("nan"), samples)
    return samples.astype("float32")


def _sample_topobathy(cog_local: Path, xc, yc, target_epsg: int):
    """Sample the topobathy COG at quadtree face centres -> z array (float32).

    Reprojects face centres (in target_epsg, the grid CRS) into the COG CRS if
    they differ (a no-op when the topobathy COG is already in the grid CRS, the
    North Star path), then point-samples (nearest). nodata / off-tile cells AND
    any unflagged fill sentinel (9999 / -9999 / 1e20 / |z|>=9000) are masked to
    NaN so the active-cell mask drops them to INACTIVE — they must NOT survive
    as a giant +9999 m wall in the domain (positive-up, NAVD88, matching
    fetch_topobathy's single-band float32 convention).
    """
    import numpy as np  # type: ignore
    import rasterio  # type: ignore
    from rasterio.warp import transform as warp_transform  # type: ignore

    with rasterio.open(cog_local) as ds:
        band_nodata = ds.nodata
        src_crs = ds.crs
        xs, ys = list(xc), list(yc)
        if src_crs is not None and src_crs.to_epsg() not in (target_epsg, None):
            xs, ys = warp_transform(
                f"EPSG:{target_epsg}", src_crs, xs, ys
            )
        samples = np.fromiter(
            (v[0] for v in ds.sample(zip(xs, ys))),
            dtype="float32",
            count=len(xs),
        )
    # Mask declared nodata + common fill sentinels -> NaN (inactive). NaN is
    # deliberately PRESERVED here (NOT re-filled with 9999): cht's mask.build
    # treats a NaN bed cell as out-of-domain and drops it to mask=0, exactly the
    # behaviour we want for out-of-coverage offshore cells past CUDEM.
    return _mask_topobathy_sentinels(samples, band_nodata)


# --------------------------------------------------------------------------- #
# AUTO-REFINEMENT — derive cht refinement polygons from the inputs.
# --------------------------------------------------------------------------- #


def _vectorize_mask_to_polygons(mask, transform, crs):
    """Vectorize a boolean raster mask into a dissolved (multi)polygon GDF row.

    Uses ``rasterio.features.shapes`` (no skimage dependency) to extract the
    polygons where ``mask`` is True, in the raster's CRS, and dissolves them into
    one geometry. Returns a shapely geometry (possibly a MultiPolygon) or None
    when the mask is empty.
    """
    import numpy as np  # type: ignore
    from rasterio import features  # type: ignore
    from shapely.geometry import shape  # type: ignore
    from shapely.ops import unary_union  # type: ignore

    mask = np.asarray(mask, dtype=bool)
    if not mask.any():
        return None
    geoms = [
        shape(geom)
        for geom, val in features.shapes(
            mask.astype("uint8"), mask=mask, transform=transform
        )
        if val == 1
    ]
    if not geoms:
        return None
    merged = unary_union(geoms)
    return merged if not merged.is_empty else None


def derive_refinement_polygons(
    spec: dict,
    scratch: Path,
    cog_local: Path,
    target_epsg: int,
):
    """Auto-derive the cht refinement-polygon GeoDataFrame from the inputs.

    Assembles, in DESCENDING ``refinement_level`` order, polygons from:
      * the topobathy 0 m NAVD88 contour buffered (finest level)   — coastline
      * the nearshore ~-2..0 m band                                — surf/run-up
      * a slope threshold band                                     — steep terrain
      * OSM river centerlines buffered                             — riverine flow
      * OSM building footprints buffered                           — urban detail
    plus any explicit ``grid.refinement_polygons_uri`` (legacy/manual path),
    unioned in at the finest level.

    Returns a GeoDataFrame with a ``refinement_level`` int column (the shape cht's
    ``grid.build`` consumes) and a dict ``level_coverage`` mapping each level ->
    the fraction of the AOI bbox it covers (for the budget estimate). Returns
    ``(None, {})`` when nothing could be derived (cht then builds the base grid).

    The deepest derived level is ``grid.refinement_levels`` (default 2); shallower
    features get shallower levels so the quadtree steps down gracefully from the
    coastline outward.
    """
    import geopandas as gpd  # type: ignore
    import numpy as np  # type: ignore
    import rasterio  # type: ignore
    from rasterio.warp import reproject, Resampling, calculate_default_transform  # type: ignore  # noqa: E501
    from shapely.geometry import box  # type: ignore

    grid = spec["grid"]
    max_level = int(grid.get("refinement_levels", 2) or 0)
    if max_level <= 0:
        LOG.info("auto-refinement disabled (grid.refinement_levels=0)")
        return None, {}

    sw = spec.get("snapwave") or {}
    mask_spec = spec.get("mask") or {}
    # Nearshore band: bathymetry between [near_lo, near_hi] (positive-up NAVD88).
    near_lo = float(sw.get("nearshore_zmin", -2.0))
    near_hi = float(sw.get("nearshore_zmax", 0.0))
    # Slope threshold (m of z change per cell) above which terrain is refined.
    slope_thresh = float(grid.get("slope_threshold", 0.05))
    # Buffer widths (m, projected) for line/point-derived features.
    river_buffer = float((spec.get("rivers") or {}).get("buffer_m", 150.0))
    building_buffer = float((spec.get("buildings") or {}).get("buffer_m", 20.0))

    # --- read the topobathy into the grid CRS (reproject if needed) ----------
    with rasterio.open(cog_local) as ds:
        src_crs = ds.crs
        dst_crs = rasterio.crs.CRS.from_epsg(target_epsg)
        if src_crs is not None and src_crs.to_epsg() == target_epsg:
            z = ds.read(1).astype("float32")
            transform = ds.transform
            nodata = ds.nodata
            pix = abs(transform.a)
        else:
            # Reproject the band into the grid CRS so contours/slope are metric.
            dt, dw, dh = calculate_default_transform(
                src_crs, dst_crs, ds.width, ds.height, *ds.bounds
            )
            z = np.full((dh, dw), np.nan, dtype="float32")
            reproject(
                source=rasterio.band(ds, 1),
                destination=z,
                src_transform=ds.transform,
                src_crs=src_crs,
                dst_transform=dt,
                dst_crs=dst_crs,
                resampling=Resampling.bilinear,
                dst_nodata=float("nan"),
            )
            transform = dt
            nodata = ds.nodata
            pix = abs(dt.a)

    if nodata is not None:
        z = np.where(z == np.float32(nodata), np.nan, z)
    valid = np.isfinite(z)
    if not valid.any():
        LOG.warning("topobathy has no valid pixels — auto-refinement skipped")
        return None, {}

    # --- band masks ----------------------------------------------------------
    # 0 m contour: cells straddling z==0 (a sign change between neighbours). A
    # cheap robust proxy: |z| within half a typical cell's expected relief. We
    # use the nearshore band's upper edge to capture the shoreline robustly.
    coast_band = valid & (z >= -0.5) & (z <= 0.5)
    nearshore_band = valid & (z >= near_lo) & (z <= near_hi)

    # slope (gradient magnitude in m per metre) via finite differences.
    gz = np.zeros_like(z)
    zf = np.where(valid, z, np.nan)
    gy, gx = np.gradient(np.nan_to_num(zf, nan=0.0))
    gz = np.sqrt(gx * gx + gy * gy) / max(pix, 1e-6)
    slope_band = valid & (gz >= slope_thresh)

    # --- vectorize each band -------------------------------------------------
    rows: list[dict] = []

    def _add(geom, level: int, source: str):
        if geom is None or geom.is_empty:
            return
        rows.append(
            {"refinement_level": int(level), "geometry": geom, "_source": source}
        )

    # finest level: coastline (0 m contour) + buildings.
    _add(
        _vectorize_mask_to_polygons(coast_band, transform, dst_crs),
        max_level,
        "coast_0m",
    )
    # one level shallower (clamped >=1): nearshore band + slope.
    mid_level = max(1, max_level - 1)
    _add(
        _vectorize_mask_to_polygons(nearshore_band, transform, dst_crs),
        mid_level,
        "nearshore_band",
    )
    _add(
        _vectorize_mask_to_polygons(slope_band, transform, dst_crs),
        mid_level,
        "slope_band",
    )

    # --- OSM rivers (lines) buffered -> mid level ----------------------------
    rivers = spec.get("rivers") or {}
    river_gdf = _read_gdf(rivers.get("lines_uri"), scratch, "rivers")
    if river_gdf is not None and len(river_gdf):
        try:
            river_gdf = river_gdf.to_crs(epsg=target_epsg)
            from shapely.ops import unary_union  # type: ignore

            buffered = unary_union(
                list(river_gdf.geometry.buffer(river_buffer).values)
            )
            _add(buffered, mid_level, "osm_rivers")
        except Exception as exc:  # noqa: BLE001 — refinement is best-effort
            LOG.warning("river refinement skipped: %s", exc)

    # --- OSM buildings (polygons) buffered -> finest level -------------------
    buildings = spec.get("buildings") or {}
    bld_gdf = _read_gdf(buildings.get("footprints_uri"), scratch, "buildings_refine")
    if bld_gdf is not None and len(bld_gdf):
        try:
            bld_gdf = bld_gdf.to_crs(epsg=target_epsg)
            from shapely.ops import unary_union  # type: ignore

            buffered = unary_union(
                list(bld_gdf.geometry.buffer(building_buffer).values)
            )
            _add(buffered, max_level, "osm_buildings")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("building refinement skipped: %s", exc)

    # --- explicit/legacy refinement polygons unioned in (finest) -------------
    explicit = _read_gdf(
        grid.get("refinement_polygons_uri"), scratch, "refine_explicit"
    )
    if explicit is not None and len(explicit):
        try:
            explicit = explicit.to_crs(epsg=target_epsg)
            for _, row in explicit.iterrows():
                lvl = row.get("refinement_level", max_level)
                _add(row.geometry, int(lvl) if lvl is not None else max_level,
                     "explicit_uri")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("explicit refinement polygons skipped: %s", exc)

    if not rows:
        LOG.info("auto-refinement derived no polygons — building base grid")
        return None, {}

    gdf = gpd.GeoDataFrame(
        [{"refinement_level": r["refinement_level"], "geometry": r["geometry"]}
         for r in rows],
        crs=f"EPSG:{target_epsg}",
    )

    # --- level coverage (fraction of the AOI bbox) for the budget estimate ---
    minx, miny, maxx, maxy = (
        float(grid["x0"]),
        float(grid["y0"]),
        float(grid["x0"]) + int(grid["mmax"]) * float(grid["dx"]),
        float(grid["y0"]) + int(grid["nmax"]) * float(grid["dy"]),
    )
    aoi_box = box(minx, miny, maxx, maxy)
    aoi_area = aoi_box.area or 1.0
    from shapely.ops import unary_union  # type: ignore

    level_coverage: dict[int, float] = {}
    levels_present = sorted({r["refinement_level"] for r in rows})
    # Cumulative coverage: a cell refined to level L is also covered by all
    # shallower levels (cht nests x2 each level), so accumulate from deepest up.
    for lvl in range(min(levels_present), max(levels_present) + 1):
        geoms = [r["geometry"] for r in rows if r["refinement_level"] >= lvl]
        if not geoms:
            level_coverage[lvl] = 0.0
            continue
        merged = unary_union(geoms).intersection(aoi_box)
        level_coverage[lvl] = float(merged.area / aoi_area) if not merged.is_empty else 0.0

    LOG.info(
        "auto-refinement: %d polygon group(s) across levels %s; coverage=%s",
        len(rows),
        levels_present,
        {k: round(v, 4) for k, v in level_coverage.items()},
    )
    return gdf, level_coverage


def _clamp_refinement_levels(gdf, allowed_max_level: int):
    """Clamp the GDF's ``refinement_level`` column to ``allowed_max_level``.

    The budget cap may decide the deepest level the quadtree can afford; any
    polygon requesting a deeper level is clamped down (its geometry stays, only
    the level cap shrinks). Polygons whose level falls to 0 are dropped (no
    refinement). Returns the (possibly empty) clamped GDF or None.
    """
    if gdf is None or allowed_max_level <= 0:
        return None
    out = gdf.copy()
    out["refinement_level"] = out["refinement_level"].clip(upper=allowed_max_level)
    out = out[out["refinement_level"] >= 1]
    return out if len(out) else None


# --------------------------------------------------------------------------- #
# BUILDING OBSTACLES — burn OSM footprints so water routes AROUND buildings.
# --------------------------------------------------------------------------- #


def burn_building_obstacles(sf, spec: dict, scratch: Path, zb, target_epsg: int):
    """Burn OSM building footprints into the deck as flow obstacles.

    Three modes (``buildings.mode``):
      * ``thin_dams`` (default) — add a thin dam (blocked uv-face) along every
        footprint exterior ring, so flow cannot cross building walls without
        raising terrain. cht ``thin_dams.add_xy`` per ring, then snap + write via
        ``sf.write()``.
      * ``raise_subgrid`` — raise the sampled ``zb`` at face centres inside any
        footprint by ``buildings.raise_height_m`` (default 5 m), so buildings
        become high+dry blocks the flow goes around. Mutates + returns ``zb``;
        the caller re-assigns it to the grid BEFORE the mask is built.
      * ``exclude`` — passed through as an exclude polygon to the mask build
        (handled in ``build_deck``); this function is a no-op for that mode.

    Returns the (possibly modified) ``zb`` array. cht_sfincs / geopandas imported
    lazily. Best-effort: a footprint read failure logs + continues (the deck is
    still valid, just without obstacles).
    """
    import numpy as np  # type: ignore

    buildings = spec.get("buildings") or {}
    footprints_uri = buildings.get("footprints_uri")
    mode = str(buildings.get("mode", "thin_dams")).strip().lower()
    if not footprints_uri or mode == "exclude":
        return zb

    bld_gdf = _read_gdf(footprints_uri, scratch, "buildings")
    if bld_gdf is None or not len(bld_gdf):
        LOG.warning("buildings.footprints_uri empty — no obstacles burned")
        return zb
    try:
        bld_gdf = bld_gdf.to_crs(epsg=target_epsg)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("building reprojection failed (%s) — obstacles skipped", exc)
        return zb

    if mode == "thin_dams":
        added = 0
        for geom in bld_gdf.geometry:
            for ring in _exterior_rings(geom):
                xs = [float(c[0]) for c in ring]
                ys = [float(c[1]) for c in ring]
                if len(xs) >= 2:
                    sf.thin_dams.add_xy(xs, ys)
                    added += 1
        if added:
            try:
                sf.thin_dams.snap_to_grid()
            except Exception as exc:  # noqa: BLE001 — snap is best-effort
                LOG.warning("thin_dams snap_to_grid failed: %s", exc)
        LOG.info("burned %d building wall(s) as thin dams", added)
        return zb

    if mode == "raise_subgrid":
        raise_h = float(buildings.get("raise_height_m", 5.0))
        xc, yc = sf.grid.face_coordinates()
        inside = _faces_inside_polygons(xc, yc, bld_gdf, target_epsg)
        if inside is not None and inside.any():
            zb = np.asarray(zb, dtype="float32").copy()
            # Raise to building height ABOVE the local terrain (so multi-storey
            # footprints sit on a hill rather than at a flat absolute height).
            zb[inside] = np.maximum(zb[inside], 0.0) + np.float32(raise_h)
            LOG.info(
                "raised z by +%.1f m at %d building face(s) (raise_subgrid)",
                raise_h,
                int(inside.sum()),
            )
        else:
            LOG.warning("no quadtree faces inside building footprints")
        return zb

    return zb


def _exterior_rings(geom):
    """Yield exterior-ring coordinate lists for a (Multi)Polygon geometry."""
    gtype = getattr(geom, "geom_type", "")
    if gtype == "Polygon":
        yield list(geom.exterior.coords)
    elif gtype == "MultiPolygon":
        for part in geom.geoms:
            yield list(part.exterior.coords)
    # other geometry types (lines/points) have no walls to burn — skip.


def _faces_inside_polygons(xc, yc, gdf, target_epsg):
    """Boolean array: which face centres (xc,yc) fall inside any GDF polygon.

    Uses a unary-union + a vectorized shapely ``contains`` via STRtree when
    available, falling back to a per-point test. Returns a numpy bool array or
    None on failure.
    """
    try:
        import numpy as np  # type: ignore
        from shapely.geometry import Point  # type: ignore
        from shapely.ops import unary_union  # type: ignore
        from shapely.prepared import prep  # type: ignore

        merged = unary_union(list(gdf.geometry.values))
        pgeom = prep(merged)
        xs = list(xc)
        ys = list(yc)
        inside = np.fromiter(
            (pgeom.contains(Point(x, y)) for x, y in zip(xs, ys)),
            dtype=bool,
            count=len(xs),
        )
        return inside
    except Exception as exc:  # noqa: BLE001
        LOG.warning("face-in-polygon test failed: %s", exc)
        return None


def derive_seaward_open_boundary_polygon(sf, points, target_epsg: int, zb=None):
    """Derive a thin seaward-edge polygon hugging the domain's open edge.

    Used when SnapWave boundary POINTS are supplied (incident waves) but NO
    explicit wave / water-level open-boundary polygon URI is given. Without a
    polygon, ``snapwave.mask.build`` flags ZERO cells as the wave boundary
    (wavebnd=0), so the incident wave has no boundary to inject from and hm0
    stays flat at 0.

    The SFINCS hydrodynamic open boundary in this worker is supplied as a
    pre-built ``sfincs.bnd`` / ``sfincs.bzs`` pair staged by the forcing adapter
    (see ``_attach_waterlevel_forcing``), which bypasses cht's mask machinery,
    so there is genuinely no ``mask == 2`` open-boundary cell to reuse. We
    therefore construct the polygon from the SEAWARD domain edge.

    DEFECT 1 FIX - edge selection is DEPTH-AWARE. When the per-face bathymetry
    ``zb`` (positive-up; seabed < 0) is supplied, we pick the active-bbox edge
    whose outermost-ring active cells have the DEEPEST mean bed (most-negative zb,
    well seaward of the surf zone) instead of the edge merely nearest the incident
    points. The live failure (run 01KVSTC80F) had the worker keep the SHALLOWEST
    east/north edge (mean zb ~ -1 m, up to +2 m land) so SnapWave dissipated the
    wave at the boundary; the deep SW/S (Gulf) edge (mean zb ~ -8 to -15 m) is the
    correct one. Without ``zb`` we fall back to the prior nearest-incident-point
    heuristic.

    Returns a single-row geopandas GeoDataFrame (a thin rectangle in the grid's
    projected CRS) or ``None`` when it cannot be derived (no active cells / no
    usable points / geo stack unavailable), in which case the caller falls back
    to the prior behaviour.
    """
    try:
        import geopandas as gpd  # type: ignore
        import numpy as np  # type: ignore
        from pyproj import CRS  # type: ignore
        from shapely.geometry import Polygon  # type: ignore
    except Exception as exc:  # noqa: BLE001
        LOG.warning("snapwave seaward-boundary derive: geo stack missing: %s", exc)
        return None

    if not points:
        return None

    try:
        xc, yc = sf.grid.face_coordinates()
        xc = np.asarray(xc, dtype=float).reshape(-1)
        yc = np.asarray(yc, dtype=float).reshape(-1)
        # Restrict to ACTIVE cells (the SnapWave mask seeds active from zmin/zmax
        # exactly like sfincs.mask, so the seaward edge we want is the active
        # extent, not the full grid bounding box).
        sw_mask = None
        try:
            sw_mask = np.asarray(
                sf.grid.data["snapwave_mask"].values
            ).reshape(-1)
        except Exception:  # noqa: BLE001
            sw_mask = None
        if sw_mask is not None and sw_mask.shape == xc.shape:
            active = sw_mask > 0
        else:
            active = np.ones(xc.shape, dtype=bool)
        if not bool(active.any()):
            LOG.warning(
                "snapwave seaward-boundary derive: no active cells - skipping"
            )
            return None
        ax = xc[active]
        ay = yc[active]
        # Active-cell bathymetry (positive-up; seabed < 0), for depth-aware edge
        # selection. None when zb was not supplied or shape-mismatched.
        az = None
        if zb is not None:
            try:
                zb_arr = np.asarray(zb, dtype=float).reshape(-1)
                if zb_arr.shape == xc.shape:
                    az = zb_arr[active]
            except Exception:  # noqa: BLE001
                az = None

        # Mean incident-wave boundary point location (offshore anchor).
        px = float(np.mean([float(p["x"]) for p in points]))
        py = float(np.mean([float(p["y"]) for p in points]))

        xmin, xmax = float(ax.min()), float(ax.max())
        ymin, ymax = float(ay.min()), float(ay.max())

        # Cell pitch from grid attrs (fallback to active-extent heuristic). Used
        # to size the thin band so it captures the outermost ring of active cells
        # without grabbing the whole domain.
        dx = None
        for src in (
            getattr(sf.grid, "dx", None),
            sf.grid.data.attrs.get("dx")
            if hasattr(sf.grid.data, "attrs") else None,
        ):
            if src is not None:
                try:
                    dx = float(src)
                    break
                except (TypeError, ValueError):
                    continue
        if dx is None or not np.isfinite(dx) or dx <= 0:
            span = max(xmax - xmin, ymax - ymin, 1.0)
            dx = span / 50.0
        # Band is ~1.5 cells deep (captures the outermost active ring) plus a
        # generous outward pad so the polygon fully encloses those cell centres.
        band = 1.5 * dx
        pad = 2.0 * dx

        side = None
        # DEFECT 1 FIX - DEPTH-AWARE edge selection. Pick the active-bbox edge
        # whose outermost-ring active cells have the DEEPEST mean bed (smallest /
        # most-negative mean zb). A wide edge band (a few cell pitches) is used so
        # a refined quadtree edge still has enough cells to average.
        if az is not None:
            edge_band = max(band, 3.0 * dx)
            edge_masks = {
                "west": ax <= (xmin + edge_band),
                "east": ax >= (xmax - edge_band),
                "south": ay <= (ymin + edge_band),
                "north": ay >= (ymax - edge_band),
            }
            edge_mean_z: dict[str, float] = {}
            for name, m in edge_masks.items():
                if bool(np.any(m)):
                    zvals = az[m]
                    zvals = zvals[np.isfinite(zvals)]
                    if zvals.size:
                        edge_mean_z[name] = float(np.mean(zvals))
            if edge_mean_z:
                # Deepest = most-negative mean bed (positive-up convention).
                side = min(edge_mean_z, key=edge_mean_z.get)
                LOG.info(
                    "snapwave seaward-boundary: DEPTH-AWARE edge means zb=%s "
                    "-> deepest=%s",
                    {k: round(v, 2) for k, v in edge_mean_z.items()},
                    side,
                )

        if side is None:
            # Fallback (no zb / no finite bed): pick the seaward edge = the domain
            # side whose outward direction points toward the incident-wave point.
            offsets = {
                "west": xmin - px,   # point west of the western face
                "east": px - xmax,   # point east of the eastern face
                "south": ymin - py,  # point south of the southern face
                "north": py - ymax,  # point north of the northern face
            }
            side = max(offsets, key=offsets.get)
            # If the point sits INSIDE the active bbox on every axis (all offsets
            # negative), fall back to the nearest edge by absolute distance.
            if offsets[side] <= 0:
                side = min(offsets, key=lambda k: abs(offsets[k]))

        if side == "west":
            poly = Polygon([
                (xmin - pad, ymin - pad), (xmin + band, ymin - pad),
                (xmin + band, ymax + pad), (xmin - pad, ymax + pad),
            ])
        elif side == "east":
            poly = Polygon([
                (xmax - band, ymin - pad), (xmax + pad, ymin - pad),
                (xmax + pad, ymax + pad), (xmax - band, ymax + pad),
            ])
        elif side == "south":
            poly = Polygon([
                (xmin - pad, ymin - pad), (xmax + pad, ymin - pad),
                (xmax + pad, ymin + band), (xmin - pad, ymin + band),
            ])
        else:  # north
            poly = Polygon([
                (xmin - pad, ymax - band), (xmax + pad, ymax - band),
                (xmax + pad, ymax + pad), (xmin - pad, ymax + pad),
            ])

        LOG.info(
            "snapwave seaward-boundary derived on %s edge "
            "(active bbox x=[%.1f,%.1f] y=[%.1f,%.1f], band=%.1f m, "
            "point=(%.1f,%.1f))",
            side, xmin, xmax, ymin, ymax, band, px, py,
        )
        return gpd.GeoDataFrame(
            {"geometry": [poly]}, crs=CRS.from_epsg(int(target_epsg))
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("snapwave seaward-boundary derive failed: %s", exc)
        return None


def _apply_time_varying_snapwave_forcing(sf, points: list, tstart) -> None:
    """Replace each SnapWave boundary point's constant timeseries with a ramped one.

    DEFECT 2 realism: the agent emits a shared ``time_s`` (seconds from tstart) +
    per-point ``hs_series`` / ``tp_series`` ramped on the storm envelope. cht's
    ``add_point`` only seeds a 2-point (tstart, tstop) constant series; here we
    overwrite each point's ``timeseries`` DataFrame with the time-varying one so
    the written bhs/btp columns evolve and hm0 grows + recedes over the storm.

    Best-effort + NON-fatal: any point WITHOUT a ``time_s`` + series (or any
    failure) leaves the constant cht series intact (the prior behaviour). Returns
    None. The agent guarantees a single shared ``time_s`` across all points (cht's
    writer indexes every column by the FIRST point's time).
    """
    try:
        import datetime as _dtm  # local - lean top
        import pandas as pd  # type: ignore
    except Exception as exc:  # noqa: BLE001
        LOG.warning("time-varying snapwave forcing: pandas missing (%s)", exc)
        return
    try:
        gdf = sf.snapwave.boundary_conditions.gdf
    except Exception as exc:  # noqa: BLE001
        LOG.warning("time-varying snapwave forcing: no boundary gdf (%s)", exc)
        return
    if gdf is None or len(gdf.index) != len(points):
        return

    applied = 0
    for i, pt in enumerate(points):
        time_s = pt.get("time_s")
        hs_series = pt.get("hs_series")
        tp_series = pt.get("tp_series")
        if not (time_s and hs_series and tp_series):
            continue
        n = len(time_s)
        if not (len(hs_series) == n and len(tp_series) == n) or n < 2:
            continue
        try:
            times = [
                tstart + _dtm.timedelta(seconds=float(s)) for s in time_s
            ]
            # Constant wd/ds from the seeded series (cht stored them per add_point).
            prev = gdf.loc[i, "timeseries"]
            wd_val = float(prev["wd"].iloc[0]) if prev is not None and len(prev) else float(pt.get("wd", 0.0))
            ds_val = float(prev["ds"].iloc[0]) if prev is not None and len(prev) else float(pt.get("ds", 0.0))
            df = pd.DataFrame(
                {
                    "time": times,
                    "hs": [float(x) for x in hs_series],
                    "tp": [float(x) for x in tp_series],
                    "wd": [wd_val] * n,
                    "ds": [ds_val] * n,
                }
            ).set_index("time")
            gdf.at[i, "timeseries"] = df
            applied += 1
        except Exception as exc:  # noqa: BLE001 - keep the constant series
            LOG.warning(
                "time-varying snapwave forcing: point %d failed (%s) - keeping "
                "constant series",
                i, exc,
            )
    if applied:
        LOG.info(
            "time-varying snapwave forcing applied to %d/%d boundary point(s) "
            "(%d time steps)",
            applied, len(points), len(points[0].get("time_s") or []),
        )


def build_deck(spec: dict, scratch: Path) -> tuple[Path, dict]:
    """Author the quadtree + SnapWave deck via cht_sfincs (GPL-isolated).

    Adapts services/workers/sfincs_quadtree_spike/author_quadtree_cht.py, swapping
    its synthetic constants for the build-spec inputs + real topobathy sampling,
    and ADDING the combined worker's auto-refinement, budget cap, and
    building-obstacle steps. Returns ``(deck_dir, provenance)`` where provenance
    carries nr_cells / nr_levels / coverage / budget notes for the completion.
    """
    # --- GPL import, lazy + isolated to this function ---
    import numpy as np  # type: ignore
    import xarray as xr  # type: ignore
    import xugrid as xu  # type: ignore
    from cht_sfincs import SFINCS  # type: ignore  # GPL-3.0 — image-only

    deck_dir = scratch / "deck"
    if deck_dir.exists():
        shutil.rmtree(deck_dir)
    deck_dir.mkdir(parents=True, exist_ok=True)

    aoi = spec["aoi"]
    grid = spec["grid"]
    topobathy = spec["topobathy"]
    times = spec["_parsed_times"]
    target_epsg = int(aoi["target_epsg"])

    x0 = float(grid["x0"])
    y0 = float(grid["y0"])
    nmax = int(grid["nmax"])
    mmax = int(grid["mmax"])
    dx = float(grid["dx"])
    dy = float(grid["dy"])
    rotation = float(grid.get("rotation", 0.0))
    max_cells = int(grid.get("max_cells", DEFAULT_MAX_CELLS))

    provenance: dict[str, Any] = {"budget_notes": []}

    # ---- 0. download the topobathy COG (needed for refinement + sampling) ---
    cog_local = scratch / "topobathy.tif"
    _download(str(topobathy["cog_uri"]), cog_local)

    # ---- 1. AUTO-REFINEMENT + BUDGET CAP ------------------------------------
    refinement_polygons, level_coverage = derive_refinement_polygons(
        spec, scratch, cog_local, target_epsg
    )
    if refinement_polygons is not None and level_coverage:
        allowed_level, notes = apply_cell_budget(
            nmax, mmax, level_coverage, max_cells
        )
        provenance["budget_notes"].extend(notes)
        for n in notes:
            LOG.info("%s", n)
        refinement_polygons = _clamp_refinement_levels(
            refinement_polygons, allowed_level
        )

    # ---- 2. refined quadtree (the gate) -------------------------------------
    LOG.info(
        "building quadtree: x0=%s y0=%s nmax=%d mmax=%d dx=%s dy=%s epsg=%d "
        "refined=%s",
        x0, y0, nmax, mmax, dx, dy, target_epsg,
        refinement_polygons is not None,
    )
    sf = SFINCS(root=str(deck_dir), crs=target_epsg, mode="w")
    sf.grid.build(
        x0, y0, nmax, mmax, dx, dy, rotation,
        refinement_polygons=refinement_polygons,
    )
    nr_cells = int(sf.grid.data.sizes["mesh2d_nFaces"])
    nr_levels = int(sf.grid.data.attrs.get("nr_levels", 1))
    provenance["nr_cells"] = nr_cells
    provenance["nr_levels"] = nr_levels
    LOG.info("quadtree built: nr_cells=%d nr_levels=%d", nr_cells, nr_levels)
    if nr_cells > max_cells:
        # The estimate under-counted; the real grid still over-ran. Record it as
        # a hard provenance note (the solve still runs, but the agent + reviewer
        # should see the budget was breached).
        msg = (
            f"WARNING: built quadtree nr_cells={nr_cells:,} exceeds budget "
            f"{max_cells:,} (estimate under-counted the refinement)"
        )
        provenance["budget_notes"].append(msg)
        LOG.warning("%s", msg)

    # ---- 3. bathymetry from the topobathy COG -------------------------------
    xc, yc = sf.grid.face_coordinates()
    zb = _sample_topobathy(cog_local, xc, yc, target_epsg)

    # ---- 3b. BUILDING OBSTACLES (raise_subgrid path mutates zb BEFORE mask) -
    # thin_dams are added here too (they don't touch zb), so the deck carries
    # the walls; raise_subgrid raises zb so the mask drops/raises those cells.
    zb = burn_building_obstacles(sf, spec, scratch, zb, target_epsg)

    ugrid2d = sf.grid.data.grid
    sf.grid.data["z"] = xu.UgridDataArray(
        xr.DataArray(data=zb, dims=[ugrid2d.face_dimension]),
        ugrid2d,
    )
    LOG.info("bathymetry sampled: z range %.2f .. %.2f m", float(np.nanmin(zb)),
             float(np.nanmax(zb)))

    # ---- 4. SFINCS active + waterlevel-boundary mask ------------------------
    mask_spec = spec.get("mask") or {}

    def _mb(key: str, default: float) -> float:
        v = mask_spec.get(key)
        return float(v) if v is not None else float(default)

    mask_zmin = _mb("zmin", -1000.0)
    mask_zmax = _mb("zmax", 2.0)
    wl_bnd = _read_gdf(
        mask_spec.get("open_boundary_polygon_uri"), scratch, "wl_bnd"
    )
    # exclude buildings from the domain entirely if mode=exclude (mask=0).
    buildings = spec.get("buildings") or {}
    exclude_poly = None
    if str(buildings.get("mode", "")).strip().lower() == "exclude" and \
            buildings.get("footprints_uri"):
        exclude_poly = _read_gdf(
            buildings.get("footprints_uri"), scratch, "buildings_exclude"
        )
        if exclude_poly is not None:
            try:
                exclude_poly = exclude_poly.to_crs(epsg=target_epsg)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("exclude-polygon reprojection failed: %s", exc)
                exclude_poly = None
    # Allow an explicit exclude polygon URI on the mask block too.
    if exclude_poly is None and mask_spec.get("exclude_polygon_uri"):
        exclude_poly = _read_gdf(
            mask_spec.get("exclude_polygon_uri"), scratch, "mask_exclude"
        )

    mask_kwargs: dict[str, Any] = dict(
        zmin=mask_zmin,
        zmax=mask_zmax,
        open_boundary_polygon=wl_bnd,
        open_boundary_zmin=_mb("open_boundary_zmin", mask_zmin),
        open_boundary_zmax=_mb("open_boundary_zmax", mask_zmax),
    )
    if exclude_poly is not None:
        mask_kwargs["exclude_polygon"] = exclude_poly
    sf.mask.build(**mask_kwargs)
    mvals = sf.grid.data["mask"].values
    LOG.info(
        "sfincs mask: active=%d wlbnd=%d inactive=%d",
        int((mvals == 1).sum()), int((mvals == 2).sum()), int((mvals == 0).sum()),
    )

    # ---- 5. SnapWave mask ----------------------------------------------------
    sw_spec = spec.get("snapwave") or {}

    def _swb(key: str, default: float) -> float:
        v = sw_spec.get(key)
        return float(v) if v is not None else float(default)

    wave_bnd = _read_gdf(
        sw_spec.get("open_boundary_polygon_uri"), scratch, "wave_bnd"
    )
    # The explicit polygon path (URI given) is honoured first; fall back to the
    # water-level boundary polygon (also a URI). When BOTH are absent (the
    # synthetic / agent forcing path, which supplies snapwave_boundary POINTS but
    # no polygon) we DERIVE a thin seaward-edge polygon below so the wavebnd
    # mask is non-empty and the incident wave can inject (else hm0 stays flat 0).
    sw_open_poly = wave_bnd if wave_bnd is not None else wl_bnd

    def _build_snapwave_mask(open_poly):
        sf.snapwave.mask.build(
            zmin=_swb("mask_zmin", mask_zmin),
            zmax=_swb("mask_zmax", mask_zmax),
            open_boundary_polygon=open_poly,
            open_boundary_zmin=_swb("open_boundary_zmin", mask_zmin),
            open_boundary_zmax=_swb("open_boundary_zmax", mask_zmax),
        )
        v = sf.grid.data["snapwave_mask"].values
        return (int((v == 1).sum()), int((v > 1).sum()), int((v == 0).sum()))

    sw_active, sw_wavebnd, sw_inactive = _build_snapwave_mask(sw_open_poly)
    LOG.info(
        "snapwave mask: active=%d wavebnd=%d inactive=%d",
        sw_active, sw_wavebnd, sw_inactive,
    )
    provenance["snapwave_active_cells"] = sw_active
    provenance["snapwave_wavebnd_cells"] = sw_wavebnd

    # FIX: wave-boundary repair. If no cell was flagged as the wave boundary
    # (wavebnd=0) but incident-wave boundary POINTS are present, the wave field
    # has nowhere to inject from and hm0 stays flat at 0. This happens whenever
    # the spec carries snapwave_boundary points but no open-boundary polygon URI
    # (the synthetic / agent path), because the SFINCS open boundary is staged as
    # a pre-built sfincs.bnd/bzs that bypasses cht's mask machinery (so there is
    # no mask==2 cell to reuse). Derive a thin seaward-edge polygon from the SAME
    # domain edge the incident wave travels in from and rebuild the mask.
    if sw_wavebnd == 0:
        sw_points = (resolve_forcing_blocks(spec)["snapwave_boundary"]
                     or {}).get("points") or []
        if sw_points:
            derived_poly = derive_seaward_open_boundary_polygon(
                sf, sw_points, target_epsg, zb=zb
            )
            if derived_poly is not None:
                sw_active, sw_wavebnd, sw_inactive = _build_snapwave_mask(
                    derived_poly
                )
                LOG.info(
                    "snapwave mask REPAIRED via derived seaward boundary: "
                    "active=%d wavebnd=%d inactive=%d",
                    sw_active, sw_wavebnd, sw_inactive,
                )
                if sw_wavebnd == 0:
                    LOG.warning(
                        "snapwave wave-boundary STILL empty after seaward "
                        "derive - incident wave may not inject (hm0 flat)"
                    )
            else:
                LOG.warning(
                    "snapwave wavebnd=0 with %d boundary point(s) but seaward "
                    "boundary could not be derived - hm0 may stay flat",
                    len(sw_points),
                )

    # ---- 6. time keywords (MUST precede SnapWave forcing — CAVEAT 1) --------
    # Set tref/tstart/tstop as proper datetimes BEFORE building the SnapWave
    # boundary timeseries: set_timeseries_uniform / add_point read tstart/tstop
    # off input.variables and cht writes (time - tref).total_seconds(), so these
    # being real datetimes is what makes the time column tref-relative.
    v = sf.input.variables
    v.qtrfile = "sfincs.nc"
    v.x0, v.y0, v.dx, v.dy = x0, y0, dx, dy
    v.nmax, v.mmax, v.rotation = nmax, mmax, rotation
    v.epsg = target_epsg
    v.tref = times["tref"]
    v.tstart = times["tstart"]
    v.tstop = times["tstop"]
    out_dt = float((spec.get("output") or {}).get("output_dt",
                                                  spec.get("output_dt", 600.0)))
    v.dtout = out_dt
    v.dtmaxout = out_dt

    # ---- 7. SnapWave boundary forcing (incident waves) ----------------------
    forcing_blocks = resolve_forcing_blocks(spec)
    sw_bc = forcing_blocks["snapwave_boundary"] or {}
    points = sw_bc.get("points") or []
    if points:
        # One boundary point per offshore location. add_point(hs=..) seeds a
        # CONSTANT (tstart, tstop) timeseries - anchored to the datetimes set in
        # step 6, so the written time column is tref-relative (CAVEAT 1).
        for pt in points:
            sf.snapwave.boundary_conditions.add_point(
                float(pt["x"]), float(pt["y"]),
                hs=float(pt.get("hs", 0.0)),
                tp=float(pt.get("tp", 0.0)),
                wd=float(pt.get("wd", 0.0)),
                ds=float(pt.get("ds", 0.0)),
            )
        # DEFECT 2 realism - TIME-VARYING incident wave forcing. The agent emits a
        # shared ``time_s`` (seconds from tstart) plus per-point ``hs_series`` /
        # ``tp_series`` ramped on the storm envelope, so hm0 grows into the storm
        # peak and recedes (vs a single constant Hs/Tp per point). Replace each
        # point's constant cht timeseries with the time-varying one. cht's writer
        # indexes every column by the FIRST point's time, so all points share the
        # one ``time_s`` vector (the agent guarantees this).
        _apply_time_varying_snapwave_forcing(sf, points, times["tstart"])
    else:
        LOG.warning("no SnapWave boundary points in spec — deck has no wave forcing")

    # ---- 8. SnapWave coupling keywords + CAVEAT 2 ---------------------------
    v.snapwave = True
    v.snapwave_bndfile = "snapwave.bnd"
    v.snapwave_bhsfile = "snapwave.bhs"
    v.snapwave_btpfile = "snapwave.btp"
    v.snapwave_bwdfile = "snapwave.bwd"
    v.snapwave_bdsfile = "snapwave.bds"
    # DEFECT 2 FIX - SnapWave coupling cadence. Pin v.dtwave to the FINE output
    # cadence (capped at 600 s) so SnapWave RE-SOLVES every output frame. Without
    # this the deck never wrote dtwave and SFINCS fell back to dtwave=3600 s
    # (hourly), so ~12 consecutive map frames carried a BYTE-IDENTICAL hm0 field
    # and the wave animation was static. An agent-pinned snapwave.dtwave (returned
    # by snapwave_inp_overrides) OVERRIDES this default via the setattr loop below.
    v.dtwave = min(out_dt, 600.0)
    for key, val in snapwave_inp_overrides(spec).items():
        setattr(v, key, val)
    LOG.info(
        "snapwave keywords set (use_herbers=%s - CAVEAT 2 fix; dtwave=%s s, "
        "out_dt=%s s)",
        getattr(v, "snapwave_use_herbers"),
        getattr(v, "dtwave"),
        out_dt,
    )

    # ---- 9. optional water-level (surge) boundary forcing -------------------
    _attach_waterlevel_forcing(sf, forcing_blocks["waterlevel"])

    # ---- 10. optional discharge (river) forcing -----------------------------
    _attach_discharge_forcing(sf, forcing_blocks["discharge"])

    # ---- 11. write the whole deck -------------------------------------------
    sf.write()
    LOG.info("cht wrote deck to %s", deck_dir)

    # ---- 12. CAVEAT 1 guard: tref-relative SnapWave time columns ------------
    rewritten = normalize_snapwave_time_columns(deck_dir, times["tref"])
    if rewritten:
        LOG.info("re-based SnapWave time columns to tref-relative: %s", rewritten)

    # ---- 13. BEST-EFFORT quadtree mesh layer (mesh.geojson) -----------------
    # Extract the active quadtree faces as an EPSG:4326 vector overlay so the web
    # client can paint the computational mesh. This must NEVER fail the build or
    # solve: the helper wraps everything in try/except, logs a warning, returns.
    emit_quadtree_mesh_geojson(sf, deck_dir, target_epsg, provenance)

    return deck_dir, provenance


# --------------------------------------------------------------------------- #
# BEST-EFFORT quadtree mesh layer (mesh.geojson).
#
# Three separable pieces so the SERIALIZATION half is unit-testable with mock
# shapely polygons (no cht_sfincs / xugrid needed in the test):
#
#   1. extract_quadtree_faces(sf, target_epsg)  — GPL/xugrid-touching geometry
#      extraction: pull the per-face shapely polygons + the level / size_m / z /
#      mask / snapwave_mask arrays off the in-scope ``sf`` (the cht SFINCS model).
#   2. build_mesh_geodataframe(...)              — PURE shapely+geopandas: take
#      polygons + attribute arrays, filter to ACTIVE cells, deterministically
#      decimate to MESH_GEOJSON_MAX_FEATURES, build a GeoDataFrame in the source
#      CRS, reproject to EPSG:4326. No cht/xugrid touched -> unit-testable.
#   3. emit_quadtree_mesh_geojson(...)           — the best-effort orchestrator
#      called from build_deck: stitches 1 -> 2, writes deck_dir/mesh.geojson,
#      records n_features / decimated / cap in provenance. NEVER raises.
# --------------------------------------------------------------------------- #


def extract_quadtree_faces(sf, target_epsg: int) -> dict:
    """Extract quadtree faces + per-cell arrays from the cht SFINCS model.

    GPL/xugrid-touching half. ``sf`` is the in-scope cht ``SFINCS`` model after
    ``sf.write()``; ``sf.grid.data`` is a xugrid UgridDataset in EPSG:<target_epsg>
    (UTM) and ``sf.grid.data.grid`` is the underlying ``xugrid.Ugrid2d``. We
    convert the face dimension to shapely polygons and read the face-indexed
    ``level`` / ``z`` / ``mask`` / ``snapwave_mask`` UgridDataArrays.

    Per-cell metric size: cht stores a 0-based refinement ``level`` (0 = coarsest)
    and each level halves the base ``dx`` (``dxb[ilev] = dx / 2**ilev``), so the
    cell edge length is ``base_dx / 2**level``.

    Returns a dict with numpy/shapely arrays (all face-ordered, same length):
        {"polygons": ndarray[shapely.Polygon], "level": ndarray[int],
         "size_m": ndarray[float], "z": ndarray|None, "mask": ndarray|None,
         "snapwave_mask": ndarray|None, "source_epsg": int}
    """
    import numpy as np  # type: ignore

    ds = sf.grid.data
    ugrid2d = ds.grid
    face_dim = ugrid2d.face_dimension

    polygons = np.asarray(ugrid2d.to_shapely(face_dim))
    n_faces = int(polygons.shape[0])

    def _face_array(name: str):
        if name not in ds:
            return None
        try:
            return np.asarray(ds[name].values).reshape(-1)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("mesh: could not read face array %r: %s", name, exc)
            return None

    level = _face_array("level")
    if level is None:
        level = np.zeros(n_faces, dtype=int)
    level = level.astype(int)

    # Per-cell metric edge length. Prefer the model's base dx (square cells in the
    # coastal North Star); fall back to grid attrs.
    base_dx = None
    for src in (
        getattr(sf.grid, "dx", None),
        ds.attrs.get("dx") if hasattr(ds, "attrs") else None,
    ):
        if src is not None:
            try:
                base_dx = float(src)
                break
            except (TypeError, ValueError):
                continue
    if base_dx is None or not np.isfinite(base_dx) or base_dx <= 0:
        base_dx = float("nan")
    size_m = base_dx / np.power(2.0, level.astype(float))

    return {
        "polygons": polygons,
        "level": level,
        "size_m": size_m,
        "z": _face_array("z"),
        "mask": _face_array("mask"),
        "snapwave_mask": _face_array("snapwave_mask"),
        "source_epsg": int(target_epsg),
    }


def build_mesh_geodataframe(
    polygons,
    level,
    size_m,
    source_epsg: int,
    *,
    z=None,
    mask=None,
    snapwave_mask=None,
    max_features: int = MESH_GEOJSON_MAX_FEATURES,
):
    """Serialize quadtree faces -> active-only EPSG:4326 GeoDataFrame.

    PURE shapely + geopandas (no cht_sfincs / xugrid). Unit-testable with mock
    polygons + fake level / size arrays.

    Steps:
      * filter to ACTIVE cells (``mask == 1``) when a mask is supplied (keeps the
        layer useful — boundary/inactive cells are dropped); without a mask, all
        polygons are kept.
      * deterministically DECIMATE (every Nth active cell, evenly strided) down to
        ``max_features`` if over -> NO silent drop; the stride is reported back.
      * build a GeoDataFrame in EPSG:<source_epsg> with per-cell columns
        ``level`` + ``size_m`` (+ ``z`` + ``mask`` + ``snapwave_mask`` when given),
        then reproject ``.to_crs(4326)``.

    Returns ``(gdf, info)`` where ``gdf`` is a 4326 GeoDataFrame (possibly EMPTY)
    and ``info`` is ``{"n_total","n_active","n_features","decimated","stride",
    "max_features"}``.
    """
    import geopandas as gpd  # type: ignore
    import numpy as np  # type: ignore

    polygons = np.asarray(polygons, dtype=object)
    n_total = int(polygons.shape[0])
    level = np.asarray(level).reshape(-1)
    size_m = np.asarray(size_m, dtype=float).reshape(-1)

    def _opt(arr):
        return None if arr is None else np.asarray(arr).reshape(-1)

    z = _opt(z)
    mask = _opt(mask)
    snapwave_mask = _opt(snapwave_mask)

    # 1. active-cell filter (mask == 1). No mask -> keep everything.
    if mask is not None and mask.shape[0] == n_total:
        active = mask == 1
    else:
        active = np.ones(n_total, dtype=bool)
    idx = np.nonzero(active)[0]
    n_active = int(idx.shape[0])

    # 2. deterministic decimation (even stride) if over the cap.
    stride = 1
    decimated = False
    if max_features is not None and n_active > int(max_features):
        # ceil division so the strided count never exceeds max_features.
        stride = int(np.ceil(n_active / float(max_features)))
        idx = idx[::stride]
        decimated = True
    n_features = int(idx.shape[0])

    info = {
        "n_total": n_total,
        "n_active": n_active,
        "n_features": n_features,
        "decimated": decimated,
        "stride": int(stride),
        "max_features": int(max_features) if max_features is not None else None,
    }

    # 3. build the GeoDataFrame in the source CRS, then reproject to 4326.
    data: dict[str, Any] = {
        "level": [int(level[i]) for i in idx] if level.shape[0] == n_total else [],
        "size_m": [
            (float(size_m[i]) if np.isfinite(size_m[i]) else None) for i in idx
        ] if size_m.shape[0] == n_total else [],
    }
    if z is not None and z.shape[0] == n_total:
        data["z"] = [(float(z[i]) if np.isfinite(z[i]) else None) for i in idx]
    if mask is not None and mask.shape[0] == n_total:
        data["mask"] = [int(mask[i]) for i in idx]
    if snapwave_mask is not None and snapwave_mask.shape[0] == n_total:
        data["snapwave_mask"] = [int(snapwave_mask[i]) for i in idx]

    geoms = [polygons[i] for i in idx]
    gdf = gpd.GeoDataFrame(data, geometry=geoms, crs=f"EPSG:{int(source_epsg)}")
    if len(gdf) > 0:
        gdf = gdf.to_crs(epsg=4326)
    return gdf, info


def emit_quadtree_mesh_geojson(
    sf, deck_dir: Path, target_epsg: int, provenance: dict
) -> str | None:
    """BEST-EFFORT: write deck_dir/mesh.geojson (active quadtree faces, EPSG:4326).

    Called at the end of ``build_deck`` AFTER ``sf.write()`` while ``sf`` / the
    grid are in scope. Stitches the GPL geometry extraction to the pure
    serialization, writes the file, and records ``mesh`` provenance
    (``n_features`` / ``decimated`` / cap) so completion.json's deck block carries
    it. NEVER raises: any failure logs a warning and returns ``None`` so the deck
    build + solve are untouched.
    """
    try:
        faces = extract_quadtree_faces(sf, target_epsg)
        gdf, info = build_mesh_geodataframe(
            faces["polygons"],
            faces["level"],
            faces["size_m"],
            faces["source_epsg"],
            z=faces.get("z"),
            mask=faces.get("mask"),
            snapwave_mask=faces.get("snapwave_mask"),
            max_features=MESH_GEOJSON_MAX_FEATURES,
        )

        mesh_path = Path(deck_dir) / "mesh.geojson"
        mesh_prov: dict[str, Any] = {
            "path": "mesh.geojson",
            "crs": "EPSG:4326",
            "n_total_cells": info["n_total"],
            "n_active_cells": info["n_active"],
            "n_features": info["n_features"],
            "decimated": info["decimated"],
            "decimation_stride": info["stride"],
            "max_features": info["max_features"],
        }

        if info["n_features"] == 0:
            # Empty -> write a valid empty FeatureCollection so a downstream reader
            # never chokes, and record that the mesh was empty.
            mesh_path.write_text(
                '{"type": "FeatureCollection", "features": []}', encoding="utf-8"
            )
            mesh_prov["empty"] = True
            provenance["mesh"] = mesh_prov
            LOG.warning(
                "mesh: no active cells (total=%d) -> wrote empty FeatureCollection "
                "to %s", info["n_total"], mesh_path,
            )
            return str(mesh_path)

        gdf.to_file(mesh_path, driver="GeoJSON")
        provenance["mesh"] = mesh_prov
        LOG.info(
            "mesh: wrote %s (n_features=%d of %d active / %d total, decimated=%s "
            "stride=%d, cap=%s)",
            mesh_path, info["n_features"], info["n_active"], info["n_total"],
            info["decimated"], info["stride"], info["max_features"],
        )
        return str(mesh_path)
    except Exception as exc:  # noqa: BLE001 — best-effort: NEVER fail the build
        LOG.warning("mesh: best-effort quadtree mesh emit failed: %s", exc)
        try:
            provenance["mesh"] = {"error": f"{type(exc).__name__}: {exc}"}
        except Exception:  # noqa: BLE001
            pass
        return None


def _attach_waterlevel_forcing(sf, waterlevel: dict | None) -> None:
    """Attach optional surge water-level boundary (bnd + bzs) if present.

    The forcing adapter materialises a waterlevel timeseries (bzs CSV) +
    locations (bnd FlatGeobuf) as object URIs
    (``{"timeseries_uri","locations_uri"}``); we stage them into the deck dir
    under the canonical SFINCS names so the solve picks them up. These are
    already in SFINCS format from the adapter — cht's regular boundary machinery
    is intentionally bypassed.
    """
    wl = waterlevel or {}
    deck_dir = Path(sf.path)
    ts_uri = wl.get("timeseries_uri")
    loc_uri = wl.get("locations_uri")
    if ts_uri and loc_uri:
        _download(loc_uri, deck_dir / "sfincs.bnd")
        _download(ts_uri, deck_dir / "sfincs.bzs")
        sf.input.variables.bndfile = "sfincs.bnd"
        sf.input.variables.bzsfile = "sfincs.bzs"
        LOG.info("attached water-level boundary forcing (bnd + bzs)")


def _attach_discharge_forcing(sf, discharge: dict | None) -> None:
    """Attach optional river discharge (src + dis) if present (staged ascii)."""
    dis = discharge or {}
    deck_dir = Path(sf.path)
    src_uri = dis.get("locations_uri")
    dis_uri = dis.get("timeseries_uri")
    if src_uri and dis_uri:
        _download(src_uri, deck_dir / "sfincs.src")
        _download(dis_uri, deck_dir / "sfincs.dis")
        sf.input.variables.srcfile = "sfincs.src"
        sf.input.variables.disfile = "sfincs.dis"
        LOG.info("attached discharge forcing (src + dis)")


# --------------------------------------------------------------------------- #
# SOLVE — invoke /usr/local/bin/sfincs IN-PROCESS on the local deck dir. Reuses
# the MIT solve worker's invocation pattern (services/workers/sfincs/entrypoint).
# No download step: the deck is already local from build_deck.
# --------------------------------------------------------------------------- #


def _run_sfincs(args: list[str], cwd: Path) -> tuple[int, Path, Path]:
    """Run the SFINCS binary in ``cwd``; return (returncode, stdout, stderr).

    SFINCS reads its entire deck (sfincs.inp + sfincs.nc + snapwave.* + bnd/bzs/
    src/dis + thin-dam/subgrid files) from CWD and takes NO argv in practice
    (``args`` is ``[]`` for a quadtree deck). Mirrors
    services/workers/sfincs/entrypoint.py::_run_sfincs byte-for-byte.
    """
    stdout_path = cwd / "sfincs.stdout"
    stderr_path = cwd / "sfincs.stderr"
    cmd = [SFINCS_BIN, *args]
    LOG.info("exec: %s (cwd=%s)", " ".join(cmd), cwd)
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=out,
            stderr=err,
            check=False,
        )
    LOG.info(
        "sfincs exit=%d stdout_bytes=%d stderr_bytes=%d",
        proc.returncode,
        stdout_path.stat().st_size,
        stderr_path.stat().st_size,
    )
    return proc.returncode, stdout_path, stderr_path


def _expand_outputs(patterns: list[str], cwd: Path) -> list[Path]:
    """Glob each pattern under ``cwd`` -> sorted unique existing files."""
    seen: set[Path] = set()
    for pat in patterns:
        for hit in glob.glob(str(cwd / pat)):
            p = Path(hit)
            if p.is_file():
                seen.add(p.resolve())
    return sorted(seen)


# --------------------------------------------------------------------------- #
# POSTPROCESS — NetCDF -> COG on the LOCAL deck output (no S3 download). Moves
# the heavy raster postprocess OFF the always-on agent box (postprocess-offload
# spike, Phases 0+1). The shared, GPL-free substrate lives in
# services/workers/_raster_postprocess/ and is imported by BOTH SFINCS workers.
# --------------------------------------------------------------------------- #


def _spec_bbox_4326(spec: dict) -> tuple[float, float, float, float] | None:
    """Pull the EPSG:4326 AOI bbox from the build spec (bounds the quadtree grid)."""
    try:
        bb = (spec.get("aoi") or {}).get("bbox")
        if bb and len(bb) == 4:
            return (float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3]))
    except Exception:  # noqa: BLE001
        pass
    return None


#: Substring SnapWave prints when an offshore boundary point sits in < 5 m of
#: water (the incident wave dissipates AT the boundary -> a near-empty hm0 field).
_SNAPWAVE_SHALLOW_BND_MARKER = "dropped below 5 m"
#: Minimum wave-field coverage as a FRACTION of the depth-field coverage. The live
#: degenerate run had wave ~0.76% vs depth ~40.6% valid pixels (ratio ~1.9%); a
#: genuine nearshore wave field is a large fraction of the wetted area. Below this
#: the "modeled" wave layer is degenerate and must NOT read status=ok.
_WAVE_MIN_COVERAGE_FRAC_OF_DEPTH = float(
    os.environ.get("TRID3NT_WAVE_MIN_COVERAGE_FRAC_OF_DEPTH", "0.05")
)


def _snapwave_shallow_boundary_warning(deck_dir: Path) -> bool:
    """True if sfincs.stdout shows a SnapWave shallow-boundary warning.

    The marker (``depth at boundary input point ... dropped below 5 m ... Please
    specify input in deeper water``) means SnapWave dissipated the incident wave
    AT the boundary, so the hm0 field is born nearly empty. Best-effort; any read
    failure returns False (do not block on a missing log).
    """
    stdout_path = deck_dir / "sfincs.stdout"
    if not stdout_path.exists():
        return False
    try:
        data = stdout_path.read_bytes()
    except Exception:  # noqa: BLE001
        return False
    return _SNAPWAVE_SHALLOW_BND_MARKER.encode("utf-8", "ignore") in data


def run_raster_postprocess(
    run_id: str,
    deck_dir: Path,
    spec: dict,
) -> tuple[dict | None, str | None, str | None, list[str]]:
    """Run the shared depth + wave postprocess on the LOCAL ``sfincs_map.nc``.

    Writes the display-ready, overview-bearing COGs straight into ``deck_dir``
    (so the entrypoint's existing ``*.tif`` upload sweep ships them), builds the
    typed publish manifest (depth layers + optional SnapWave wave layers merged),
    and applies the empty-field honesty gate.

    Returns ``(manifest_dict | None, status_override | None, error_code | None,
    extra_output_rels)``:
      * ``manifest_dict`` — the publish_manifest.json body (None if the local
        sfincs_map.nc is missing, e.g. a failed solve).
      * ``status_override`` — "error" when the DEPTH honesty gate fires (empty
        flood field); else None (the entrypoint keeps the solve status).
      * ``error_code`` — the typed code for the gate (RUN_OUTPUT_EMPTY).
      * ``extra_output_rels`` — deck-relative COG filenames written (for logging;
        the sweep uploads them by glob).

    NEVER raises (best-effort): any postprocess failure logs + returns
    ``(None, None, None, [])`` so the raw sfincs_map.nc still uploads and the
    agent's legacy on-box path can still run (transition fallback).
    """
    local_nc = deck_dir / "sfincs_map.nc"
    if not local_nc.exists():
        LOG.warning(
            "raster postprocess: no local sfincs_map.nc in %s — skipping "
            "(solve produced no map output).", deck_dir,
        )
        return None, None, None, []

    try:
        from services.workers._raster_postprocess import postprocess as _pp
        from services.workers._raster_postprocess import manifest as _manifest_mod
    except Exception as exc:  # noqa: BLE001 — shared pkg missing -> legacy fallback
        LOG.warning("raster postprocess: shared package import failed (%s)", exc)
        return None, None, None, []

    bbox = _spec_bbox_4326(spec)
    runs_uri_for = lambda rel: _runs_uri(run_id, rel)  # noqa: E731

    try:
        depth = _pp.run_postprocess(
            local_nc, run_id=run_id, deck_dir=deck_dir,
            runs_uri_for=runs_uri_for, kind="depth", engine="sfincs_quadtree",
            bbox=bbox,
        )
    except Exception as exc:  # noqa: BLE001 — defensive; legacy fallback
        LOG.exception("raster postprocess: depth pass crashed (%s)", exc)
        return None, None, None, []

    # DEPTH honesty gate: an empty flood field sinks the run (Invariant 1).
    if depth.status == "error":
        return (
            depth.manifest, "error", depth.error_code,
            [],
        )

    layers = list(depth.manifest.get("layers", []))
    frame_count = int(depth.manifest.get("frame_count", 0))
    rels = [Path(lyr["cog_uri"]).name for lyr in layers]

    # WAVE pass (SnapWave) — best-effort; absence is the honest depth-only degrade.
    #
    # HONESTY GATE (DEFECT 1 floor): a "modeled" wave envelope that is degenerate
    # (the incident wave dissipated at a shallow boundary -> a near-empty hm0
    # field) must NOT publish as a status=ok wave layer. The postprocess already
    # drops a TRULY empty field (flooded_cell_count==0), but the live failure was
    # a NON-zero but tiny field (~0.76% vs depth's ~40.6% valid pixels). So we
    # ALSO drop the wave layer when EITHER (a) sfincs.stdout shows the SnapWave
    # shallow-boundary warning, OR (b) the wave coverage is < a few % of the depth
    # coverage. The DEPTH layer + animation are unaffected (depth is honest), so
    # we degrade to depth-only rather than sinking the whole run.
    try:
        waves = _pp.run_postprocess(
            local_nc, run_id=run_id, deck_dir=deck_dir,
            runs_uri_for=runs_uri_for, kind="waves", engine="sfincs_quadtree",
            bbox=bbox,
        )
        if waves.status == "ok" and waves.manifest.get("layers"):
            wave_flooded = int(
                (waves.metrics or {}).get("flooded_cell_count", 0) or 0
            )
            depth_flooded = int(
                (depth.metrics or {}).get("flooded_cell_count", 0) or 0
            )
            cov_frac = (
                (wave_flooded / depth_flooded) if depth_flooded > 0 else 0.0
            )
            shallow_warn = _snapwave_shallow_boundary_warning(deck_dir)
            degenerate = shallow_warn or (
                depth_flooded > 0 and cov_frac < _WAVE_MIN_COVERAGE_FRAC_OF_DEPTH
            )
            if degenerate:
                # Drop the degenerate wave COGs + DO NOT add the wave layer (no
                # status=ok-but-empty wave envelope). Depth-only honest degrade.
                for lyr in waves.manifest["layers"]:
                    try:
                        (deck_dir / Path(lyr["cog_uri"]).name).unlink(
                            missing_ok=True
                        )
                    except Exception:  # noqa: BLE001
                        pass
                LOG.warning(
                    "raster postprocess: WAVE honesty gate fired - dropping wave "
                    "layer (shallow_boundary_warning=%s, wave_cells=%d, "
                    "depth_cells=%d, coverage=%.2f%% < floor %.2f%%). The SnapWave "
                    "boundary likely landed in shallow water; depth-only degrade.",
                    shallow_warn, wave_flooded, depth_flooded,
                    100.0 * cov_frac,
                    100.0 * _WAVE_MIN_COVERAGE_FRAC_OF_DEPTH,
                )
            else:
                layers.extend(waves.manifest["layers"])
                frame_count += int(waves.manifest.get("frame_count", 0))
                rels.extend(
                    Path(lyr["cog_uri"]).name
                    for lyr in waves.manifest["layers"]
                )
    except Exception as exc:  # noqa: BLE001 — waves are optional
        LOG.warning("raster postprocess: wave pass failed (%s) — depth-only", exc)

    manifest = _manifest_mod.build_manifest(
        engine="sfincs_quadtree", run_id=run_id, status="ok",
        frame_count=frame_count, metrics=depth.metrics, layers=layers,
    )
    LOG.info(
        "raster postprocess: built manifest with %d layer(s) (%d frames)",
        len(layers), frame_count,
    )
    return manifest, None, None, rels


# --------------------------------------------------------------------------- #
# Completion + main
# --------------------------------------------------------------------------- #


def _write_completion(
    run_id: str,
    status: str,
    exit_code: int,
    output_uris: list[str],
    stdout_uri: str | None,
    stderr_uri: str | None,
    deck_provenance: dict | None,
    started_at: str,
    error: str | None,
    publish_manifest_uri: str | None = None,
) -> str:
    """Write the combined completion.json — a UNION of the deck + solve schemas.

    The agent's ``wait_for_completion`` polls this object identically to the
    standalone solve worker's completion: the keys it reads (status, exit_code,
    output_uris, sfincs_stdout_uri/sfincs_stderr_uri, started/finished_at, error)
    are all present; ``deck`` is the extra build-provenance block.

    ``publish_manifest_uri`` (postprocess-offload spike) is an EXPLICIT pointer to
    the worker-written publish_manifest.json so the agent never globs for it. It
    is present only when the raster postprocess produced a manifest.
    """
    payload = {
        "run_id": run_id,
        "status": status,
        "exit_code": exit_code,
        "sfincs_stdout_uri": stdout_uri,
        "sfincs_stderr_uri": stderr_uri,
        "output_uris": output_uris,
        "deck": deck_provenance,
        "publish_manifest_uri": publish_manifest_uri,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "error": error,
    }
    return _put_json(payload, _runs_uri(run_id, "completion.json"))


def _build_argv_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trid3nt-sfincs-quadtree",
        description=(
            "Combined SFINCS quadtree+SnapWave BUILD+SOLVE worker "
            "(AWS Batch, one job)."
        ),
    )
    p.add_argument(
        "--run-id",
        default=os.environ.get("TRID3NT_RUN_ID", "").strip(),
        help="Run identifier (also $TRID3NT_RUN_ID).",
    )
    p.add_argument(
        "--build-spec-uri",
        default=os.environ.get("TRID3NT_BUILD_SPEC_URI", "").strip(),
        help="s3:// / gs:// URI of the build spec JSON "
        "(also $TRID3NT_BUILD_SPEC_URI).",
    )
    return p


def _prepare_scratch() -> Path:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    return SCRATCH


def main(argv: list[str] | None = None) -> int:
    args = _build_argv_parser().parse_args(argv)
    run_id = args.run_id
    build_spec_uri = args.build_spec_uri
    if not run_id:
        LOG.error("run_id is required (--run-id or $TRID3NT_RUN_ID)")
        return 2
    if not build_spec_uri:
        LOG.error(
            "build_spec_uri is required (--build-spec-uri or $TRID3NT_BUILD_SPEC_URI)"
        )
        return 2

    LOG.info(
        "trid3nt-sfincs-quadtree (BUILD+SOLVE) starting — run_id=%s spec=%s "
        "object_store=%s sfincs_bin=%s",
        run_id, build_spec_uri, _output_scheme(), SFINCS_BIN,
    )
    started_at = _utc_now()
    output_uris: list[str] = []
    stdout_uri: str | None = None
    stderr_uri: str | None = None
    deck_provenance: dict | None = None
    publish_manifest_uri: str | None = None
    error_msg: str | None = None
    exit_code = 1
    status = "error"

    try:
        raw_spec = _read_json(build_spec_uri)
        spec = validate_build_spec(raw_spec)
        scratch = _prepare_scratch()

        # ---- BUILD (GPL: cht_sfincs) ----------------------------------------
        deck_dir, deck_provenance = build_deck(spec, scratch)

        # Compose + upload the audit manifest (NOT fed to a second job anymore).
        deck_dir_uri = spec["output"]["deck_dir_uri"]
        manifest = compose_manifest(deck_dir, deck_dir_uri)
        manifest_uri = spec["output"]["manifest_uri"]
        _put_json(manifest, manifest_uri)
        deck_provenance["manifest_uri"] = manifest_uri

        # ---- SOLVE (MIT: /usr/local/bin/sfincs on the LOCAL deck) -----------
        # No download — the deck is already populated in deck_dir.
        rc, stdout_path, stderr_path = _run_sfincs([], deck_dir)

        # Always upload stdout/stderr so even a failed solve produces evidence.
        stdout_uri = _upload(stdout_path, _runs_uri(run_id, "sfincs.stdout"))
        stderr_uri = _upload(stderr_path, _runs_uri(run_id, "sfincs.stderr"))

        # ---- RASTER POSTPROCESS (NetCDF -> COG on the LOCAL deck) -----------
        # Runs ON THE WORKER (postprocess-offload spike): write display-ready
        # overview-bearing COGs into the deck dir BEFORE the output sweep below
        # (so the *.tif glob ships them), build the publish manifest, and apply
        # the empty-field honesty gate. Only attempted on a clean solve (rc==0
        # AND a local sfincs_map.nc); a failed solve falls straight through to
        # the raw-NetCDF upload + the agent's legacy on-box path. Best-effort:
        # never sinks the run by itself.
        pp_status_override: str | None = None
        pp_error_code: str | None = None
        if rc == 0:
            manifest, pp_status_override, pp_error_code, pp_rels = (
                run_raster_postprocess(run_id, deck_dir, spec)
            )
            if manifest is not None:
                # Write the manifest BEFORE completion.json (Spot-reclaim
                # atomicity): status=ok in completion.json implies the manifest
                # + every listed COG already exist.
                from services.workers._raster_postprocess import (
                    manifest as _manifest_mod,
                )

                publish_manifest_uri = _put_json(
                    manifest, _runs_uri(run_id, _manifest_mod.MANIFEST_FILENAME)
                )
                LOG.info(
                    "raster postprocess: wrote %s (%d COG(s))",
                    publish_manifest_uri, len(pp_rels),
                )

        # Upload the SFINCS outputs (sfincs_map.nc is the load-bearing one; the
        # postprocess COGs written above are caught by the *.tif glob).
        for path in _expand_outputs(list(SOLVE_OUTPUT_PATTERNS), deck_dir):
            rel = path.relative_to(deck_dir).as_posix()
            # Skip stdout/stderr re-upload (handled above); keep map + tifs.
            if rel in {"sfincs.stdout", "sfincs.stderr"}:
                continue
            output_uris.append(_upload(path, _runs_uri(run_id, rel)))

        # Optional deck audit upload (off by default — the deck is large). The
        # manifest URI is the load-bearing audit pointer; list it for parity.
        if str(spec.get("output", {}).get("upload_deck", "")).strip().lower() in {
            "1", "true", "yes",
        }:
            for f in sorted(p for p in deck_dir.glob(DECK_GLOB) if p.is_file()):
                rel = f.relative_to(deck_dir).as_posix()
                if rel in {"sfincs.stdout", "sfincs.stderr"} or \
                        rel in {Path(u).name for u in output_uris}:
                    continue
                output_uris.append(
                    _upload(f, _runs_uri(run_id, f"deck/{rel}"))
                )
        output_uris.append(manifest_uri)
        if publish_manifest_uri:
            output_uris.append(publish_manifest_uri)

        # status is OK only if BOTH the build succeeded (we got here) AND the
        # solve exited 0 AND the raster honesty gate did not fire (the depth
        # field was non-empty). An empty flood field on an otherwise-clean solve
        # is a status=error (RUN_OUTPUT_EMPTY) so the agent never registers a
        # status=ok-but-empty layer (Invariant 1 / FR-AS-7).
        exit_code = rc
        if rc != 0:
            status = "error"
            error_msg = f"sfincs exited with non-zero code {rc}"
        elif pp_status_override == "error":
            status = "error"
            error_msg = (
                f"raster postprocess honesty gate: {pp_error_code} "
                "(solve clean but the flood field is empty)"
            )
        else:
            status = "ok"
        LOG.info(
            "combined run finished: build OK (nr_cells=%s nr_levels=%s), "
            "solve exit=%d, %d output(s)",
            (deck_provenance or {}).get("nr_cells"),
            (deck_provenance or {}).get("nr_levels"),
            rc,
            len(output_uris),
        )
    except Exception as exc:  # noqa: BLE001 — defensive, logged + emitted
        LOG.exception("combined quadtree worker failed")
        error_msg = f"{type(exc).__name__}: {exc}"
        exit_code = 1
        status = "error"

    _write_completion(
        run_id=run_id,
        status=status,
        exit_code=exit_code,
        output_uris=output_uris,
        stdout_uri=stdout_uri,
        stderr_uri=stderr_uri,
        deck_provenance=deck_provenance,
        started_at=started_at,
        error=error_msg,
        publish_manifest_uri=publish_manifest_uri,
    )
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
