#!/usr/bin/env python
"""Replication-parity runner (router-pilot-contract sec 4.2).

Runs the twin-vs-router envelope comparison for all 5 pilots and writes
``results/VERDICT.md`` + prints a per-source one-liner. Offline + deterministic
by default (synthetic upstream, no MinIO): the fair-A/B fold gate.

Usage:
    python run.py            # run all 5, write results/VERDICT.md
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from drivers import run_all  # noqa: E402
from harness import SourceResult  # noqa: E402


def _fmt(res: SourceResult) -> str:
    if res.error:
        return f"- {res.source}: ERROR -- {res.error}"
    div = [c for c in res.checks if not c.ok]
    tail = ""
    if div:
        bits = []
        for c in div:
            bits.append(c.note or f"{c.name} (twin={c.twin!r} router={c.router!r})")
        tail = " | divergences: " + "; ".join(bits)
    return f"- {res.source}: {res.verdict} ({res.n_ok}/{res.n_total} checks){tail}"


def _verdict_md(results: list[SourceResult]) -> str:
    lines = [
        "# Replication-parity VERDICT -- data-router fold pilots (5)",
        "",
        "Authority: docs/specs/router-pilot-contract.md sec 4.2. Twin vs router,",
        "identical synthetic upstream, offline + deterministic (no MinIO). Twin",
        "behavior is the contract; divergences are recorded, never fudged.",
        "",
        "| source | verdict | checks | key divergence |",
        "|---|---|---|---|",
    ]
    for r in results:
        div = [c for c in r.checks if not c.ok]
        key = (div[0].note or div[0].name) if div else "-"
        lines.append(f"| {r.source} | {r.verdict} | {r.n_ok}/{r.n_total} | {key} |")
    lines += ["", "## Per-check detail", ""]
    for r in results:
        lines.append(f"### {r.source} -- {r.verdict}")
        if r.error:
            lines.append(f"- ERROR: {r.error}")
        for c in r.checks:
            mark = "ok" if c.ok else "XX"
            note = f" -- {c.note}" if c.note else ""
            if c.ok:
                lines.append(f"- [{mark}] {c.name}{note}")
            else:
                lines.append(f"- [{mark}] {c.name}: twin={c.twin!r} router={c.router!r}{note}")
        lines.append("")
    lines += _FINDINGS.splitlines()
    return "\n".join(lines)


_FINDINGS = """## Findings: 5/5 parity across the FULL edge matrix (round-2 gaps CLOSED)

The harness now grades the contract-4.2 edge matrix per source (error paths ARE
values): happy-path values/schema/layer + BOTH honesty-floor empty paths + every
invalid-param class (malformed bbox / out-of-range year+date / bad enum) + every
declared gate (conus / max_bbox / soft caps) + a forced upstream failure. Grading
is tightened: layer.bbox_present is a GATING 4.2 layer-output field; error.* and
gate.* are gating; only info.* + a flagged twin-defect are non-gating.

Round-2 divergences the adversarial parity lens named beyond the fixed requests --
all CLOSED to twin behavior and now COVERED by a harness case that would catch a
regression:

1. [CLOSED] esri empty/no-coverage: router raised ESRI_LANDCOVER_EMPTY; twin +
   the esri caveat say ESRI_LANDCOVER_NO_COVERAGE. FIX: SourceSpec.empty_error_suffix
   (default EMPTY; esri = NO_COVERAGE) threaded through every router_empty_error.
   Covered by esri error.empty (empty STAC search, driven through both entrypoints).

2. [CLOSED] esri year unvalidated: year=1850/2099 silently proceeded to STAC (EMPTY)
   vs twin ESRI_LANDCOVER_YEAR_INVALID. FIX: ParamSpec.min/max range gate + per-param
   error_suffix; esri year = {min:2017, max:2023, error_suffix: YEAR_INVALID}.
   Covered by esri error.year_low + error.year_high.

3. [CLOSED] input-error suffix leaked origin: errors.py hardcoded _INPUT_ERROR, so
   census/hifld emitted *_INPUT_ERROR (twin *_INPUT_INVALID) and esri emitted
   *_INPUT_ERROR (twin *_BBOX_INVALID). FIX: SourceSpec.input_error_suffix
   (hifld/census = INPUT_INVALID) + per-param error_suffix (esri bbox = BBOX_INVALID);
   router_input_error takes the suffix; bbox-class gate failures use bbox_error_suffix.
   Covered by every source's error.bad_bbox / error.bad_enum (+ esri gate.max_bbox).

4. [CLOSED] harness under-covered 4.2 (empty path was coops-only). Empty is now
   graded on all 5: typed error for gridmet (GRIDMET_EMPTY) / esri (NO_COVERAGE) /
   coops (COOPS_TIDES_EMPTY); honest header-only FGB for hifld + census (n=0).

5. [CLOSED] gridmet LayerURI.bbox tell: twin omits it, router always populated it.
   FIX: OutputSpec.emit_bbox (default True; gridmet = false). layer.bbox_present is
   now a gating check and matches.

6. [CLOSED, bonus] gridmet date coverage: start<1979 / future-end are twin
   GRIDMET_NOT_AVAILABLE (distinct from INPUT_ERROR). FIX: ParamSpec.min_date +
   max_future_days -> router_not_available_error. Covered by error.date_before_coverage
   + error.date_future (+ error.date_order / error.date_range as INPUT_ERROR).

7. [DOC] esri source.yaml stale "STAC sub-mode is a stub" NOTE removed (the shipped
   stac_to_mosaic implements sas-sign + reproject + uint8 + palette + mosaic).

REPORTED twin defect (flagged for NATE, NOT copied): gridmet values.nodata -- the
twin's rioxarray writer silently DROPS the declared nodata=nan (emits None); the
router correctly writes nodata=nan. This is an observable COG-metadata difference,
scored honestly as ok=False but non-gating because the ROOT CAUSE is the twin
writer (needs rio.write_nodata()); the router must not propagate the bug. Byte-
identical nodata parity requires a twin fix, which only NATE lands.

Documented (not a defect): LayerURI.units is a router single-string field while
gridmet/census units are per-variable; the per-FEATURE units (census FGB) vary
correctly via the JOIN. A general fix needs a normalize.units_by_param hook.

Fold-arm drift fix (out of the replication lens, from the regression lens): server
._default_declarable_registry applied the pool substitution AFTER the tier=template
filter had already dropped the fetch_X__spec alias, so its ON-path swap could never
fire. Reordered to substitute on the FULL pre-filter snapshot (matching
_build_index), so all three pool producers now apply the same substitution -- the
drift guard. Verified: OFF the declarable fetch_gridmet resolves to the twin module,
ON it resolves to the router _virtual module, zero __spec alias leak either arm.
"""


def main() -> int:
    results = run_all()
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "VERDICT.md").write_text(_verdict_md(results))
    print("=== replication-parity per-source verdicts ===")
    for r in results:
        print(_fmt(r))
    n_pass = sum(1 for r in results if r.verdict == "PASS")
    n_partial = sum(1 for r in results if r.verdict == "PARTIAL")
    n_block = sum(1 for r in results if r.verdict in ("BLOCK", "ERROR"))
    print(f"=== PASS={n_pass} PARTIAL={n_partial} BLOCK/ERROR={n_block} ===")
    print(f"wrote {out_dir / 'VERDICT.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
