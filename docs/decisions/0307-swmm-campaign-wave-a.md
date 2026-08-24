
## Post-review corrections (verify lens, 2026-08-25)

- LOC staleness: rdii_rtk/steps.py is 443 (the final efaa46da fix added
  16 lines after the last count) -> RDII total 753, everything-touched
  3,324, family total 9,144, delta vs baseline +1,421.
- The SWMM template count is 14, not 15 (the group table itself sums to
  14): "3 of 14" migrated.
- Row 23b softened: the variable-step chart fix removes a LATENT
  assumption - these decks pin REPORT_STEP=dt so the engine clock was
  uniform and nothing previously plotted was wrong. The residual
  same-class pattern in snowmelt_metrics (cross-run clock indexing,
  currently harmless at 167/167 steps) is recorded for the family
  waves, as is the missing offline coverage for swmm_times_hr /
  flow_routing_error_pct (queued with wave B).
- Wave-B scope corrected: mesh/swmm_deck_runner (625) is imported by
  the C family too and cannot retire at wave B - the group-boundary
  argument strengthens; the wave-B sentence was wrong.
- Board nit: the snow_removal knobs line updated with the row.

- do_sag parity spot-check: re-attempted 2026-08-25 post disk-reclaim
  (96%, 18 GB free); the background solve was stopped externally before
  completion. Standing evidence: the wave diff touches no TELEMAC path
  (verified by the review lens) and test_telemac_do_sag is green. The
  pinned re-run remains one command when wanted.

## do_sag parity spot-check - CLOSED, reproduced

`scripts/run_do_sag_direct.py --discharge-m3s 2.0` (Eel River near Scotia,
California; BOD 20, 20 C, standard 5, k1 0.3, k2 0.9, 12 km, mesh auto), run
`01M0TB5Y4MW4B58DVA3889FKQ6`:

| | ADR 0303 pinned reference | this run |
|---|---|---|
| DO sag minimum | 8.5772 mg/L | **8.5772 mg/L** |
| sag location | 10631.7 m | **10631.7 m** |
| violates the 5 mg/L standard | false | **false** |
| points / first / last | 60 / 9.022 / 8.9623 | **60 / 9.022 / 8.9623** |

Bit-identical. `executed=['do_field', 'do_field.chart:do_sag_curve']
replayed=[] notes=[]`, layer
`s3://trid3nt-runs/01M0TB5Y4MW4B58DVA3889FKQ6/telemac_do_field.tif`.

The CARD drive is a different question and stays that way: a drawn outfall
moves the release off the derived mid-reach seed, so
`scripts/drive_do_sag_cards.py` answers with the USGS Scotia gage and lands
**8.6542 mg/L at 546.1 m** (ADR 0304 recorded 8.6537 at the same 546.1 m for
the same drawn point; the carrier discharge there resolves from NWM
analysis-assim at 2 m3/s). Parity is the pinned DERIVED-seed run, not the
drawn one.
