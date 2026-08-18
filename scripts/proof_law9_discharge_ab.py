"""Live A/B proof for law 9 P5 -- SCHISM baroclinic river_discharge resolve-or-refuse.

(A) under-specified auto run, NWM cannot serve -> the typed input-review REFUSAL
    naming river_discharge_m3s (law 9: never solve on an invented demo inflow).
(B) the SAME prompt with NWM available -> the dominant-reach discharge is DERIVED
    (basis=derived / fetch_noaa_nwm_streamflow) and the baroclinic solve proceeds.

Run with the local env sourced (TRID3NT_SOLVER_BACKEND=local-docker, MinIO block,
schism image). The (B) 3D solve is heavy; a --probe-only run proves the live
derivation without the solve.
"""
from __future__ import annotations

import asyncio
import sys

from trid3nt_server.workflows.schism.baroclinic_circulation.baroclinic_circulation import (
    model_schism_baroclinic_circulation,
    _resolve_bbox,
)
from trid3nt_server.workflows.shared import discharge_resolve
from trid3nt_server.workflows.shared.discharge_resolve import resolve_dominant_discharge

PLACE = "Delaware Bay"
DAYS = 0.5  # minimum coarse spin-up smoke to keep the (B) solve as light as possible
DEAD_CONSTANT = 500.0  # the DELETED baroclinic demo default inflow


async def main() -> int:
    probe_only = "--probe-only" in sys.argv
    print("=" * 78)
    print(f"LAW-9 P5 LIVE A/B  -- {PLACE}  (SCHISM baroclinic river_discharge)")
    print("=" * 78)

    aoi_bbox, aoi_label, aoi_basis = _resolve_bbox(PLACE, None)
    print(f"\n[aoi] {PLACE} -> bbox={aoi_bbox} label={aoi_label} basis={aoi_basis}")

    # --- provenance probe: what does the shared resolver derive at the AOI? ------
    print("\n--- resolver provenance at the AOI (NWM dominant reach) ---")
    res = await asyncio.to_thread(resolve_dominant_discharge, aoi_bbox, None)
    print(f"resolved        : {res.resolved}")
    print(f"discharge_m3s   : {res.discharge_m3s}")
    print(f"source          : {res.source}")
    print(f"meta            : {res.meta}")
    e = res.entry
    print(f"  entry[{e.param}] basis={e.basis} consequence={e.consequence} "
          f"value={e.value} real_source={e.real_source_if_any}")
    print(f"    note: {e.note}")
    if res.discharge_m3s is not None:
        ratio = res.discharge_m3s / DEAD_CONSTANT
        print(f"\n[compare] derived Q={res.discharge_m3s:.1f} m3/s vs DEAD demo constant "
              f"{DEAD_CONSTANT:.0f} m3/s  (ratio {ratio:.2f}x)")

    # --- (A) under-specified auto run with NWM UNABLE to serve -> REFUSE ---------
    print("\n" + "-" * 78)
    print("(A) UNDER-SPECIFIED AUTO RUN -- NWM forced unavailable")
    print("-" * 78)
    orig = discharge_resolve.dominant_reach_discharge

    def _no_nwm(_bbox):
        return None, {"reason": "AOI off the CONUS NWM domain (A/B: forced unavailable)"}

    discharge_resolve.dominant_reach_discharge = _no_nwm  # type: ignore[assignment]
    a_outcome = ""
    try:
        r = await model_schism_baroclinic_circulation(
            location_query=PLACE, bbox=None, river_discharge_m3s=None,
            ocean_salinity_psu=33.0, sim_days=DAYS, ocean_side="south",
            input_mode="auto",
        )
        if isinstance(r, dict) and r.get("status") == "error":
            a_outcome = f"{r.get('error_code')}: {r.get('error_message')}"
            print(f"(A) refused as expected:\n{a_outcome}")
        else:
            a_outcome = "NO REFUSAL (law-9 FAILURE)"
            print("!! (A) did NOT refuse -- law 9 violated")
    finally:
        discharge_resolve.dominant_reach_discharge = orig  # type: ignore[assignment]

    a_ok = ("CANCELLED" in a_outcome or "INPUT_REQUIRED" in a_outcome) and (
        "river_discharge" in a_outcome
    )
    print(f"\n(A) verdict: {'PASS' if a_ok else 'FAIL'} "
          f"(typed refusal names river_discharge)")

    if probe_only:
        b_ok = res.resolved and res.entry.basis == "derived"
        print("\n(B) SKIPPED (--probe-only); derivation proven by the probe above: "
              f"basis={res.entry.basis}")
        print("\n" + "=" * 78)
        print(f"A/B RESULT (probe-only): A={'PASS' if a_ok else 'FAIL'}  "
              f"derive={'PASS' if b_ok else 'FAIL'}")
        print("=" * 78)
        return 0 if (a_ok and b_ok) else 1

    # --- (B) NWM on -> derived discharge + real solve ---------------------------
    # Salinity is supplied as a USER value (row 20 has no fetcher and refuses in
    # auto -- the WOA offer is the user_gated path), so the B leg ISOLATES the
    # discharge wiring: discharge DERIVED from NWM, salinity user, solve proceeds.
    print("\n" + "-" * 78)
    print("(B) NWM ON, salinity user-supplied -> derived discharge + real 3D solve")
    print("-" * 78)
    b_ok = False
    try:
        result = await model_schism_baroclinic_circulation(
            location_query=PLACE, bbox=None, river_discharge_m3s=None,
            ocean_salinity_psu=33.5, sim_days=DAYS, ocean_side="south",
            input_mode="auto",
        )
        if isinstance(result, dict):
            print(f"(B) returned error dict: {result.get('error_code')}: "
                  f"{result.get('error_message')}")
        else:
            print(f"(B) SOLVE OK -- layer_id={result.layer_id}")
            print(f"    river_discharge_m3s : {getattr(result, 'river_discharge_m3s', None)}")
            print(f"    surface_salinity    : [{result.surface_salinity_min_psu:.2f}, "
                  f"{result.surface_salinity_max_psu:.2f}]")
            print(f"    max_stratification  : {result.max_stratification_psu}")
            print(f"    uri                 : {result.uri}")
            got_q = getattr(result, "river_discharge_m3s", None)
            b_ok = got_q is not None and abs(float(got_q) - DEAD_CONSTANT) > 1e-6
    except Exception as exc:  # noqa: BLE001 -- report whatever happened
        print(f"(B) FAILED: {type(exc).__name__}: {exc}")

    print(f"\n(B) verdict: {'PASS' if b_ok else 'FAIL'} "
          f"(solved on the DERIVED inflow, not the dead 500 constant)")

    print("\n" + "=" * 78)
    print(f"A/B RESULT: A={'PASS' if a_ok else 'FAIL'}  B={'PASS' if b_ok else 'FAIL'}")
    print("=" * 78)
    return 0 if (a_ok and b_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
