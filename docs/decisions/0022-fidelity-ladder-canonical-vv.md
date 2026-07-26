# 0022 - Fidelity ladder + canonical-case V&V (Malpasset / TELEMAC-2D)

Date: 2026-07-26. Status: accepted.

## Context

The Harvey L2 arc ran the verify-calibrate loop on SFINCS. NATE's correction:
SFINCS is a reduced-physics screening engine ("super fast" by collapsing
physics and geometry) - the wrong instrument for calibration-grade model
refinement. Fidelity (1D/2D/3D) is a per-question choice with known
limitations per rung.

## Decision

1. SFINCS results are screening-grade; calibration/refinement conclusions
   are never drawn on it.
2. The V&V loop's reference exercise moves to THE canonical documented flood
   calibration case run on its native full-physics solver: the Malpasset
   dam-break (1959) on TELEMAC-2D - the official TELEMAC validation case,
   whose mesh/steering files ship with the TELEMAC distribution and whose
   observed data (17 police max-level survey points, 3 EDF transformer
   outage times for wave arrival, 1/400 physical-model gauges) plus
   published calibration results (Strickler ~30-40 band) are in the primary
   literature. HEC-RAS acknowledged as the other canonical family - not in
   our stack; TELEMAC-2D is live, so Malpasset wins "most available
   documented example".
3. Group D gains set_telemac_parameters (friction law/coefficient, copy-on-
   write, bounds, child-deck-must-solve regression per ADR 0021 lessons).
4. OceanMesh2D integration (unstructured mesh generation) queued for its own
   scoping - not needed for Malpasset (mesh ships with the case).

## Consequence

The telemac diagnostics parser gets its first real HEALTHY fixture (the wave
build only had a failed-run listing). Harvey/SFINCS findings
(l2-harvey-findings.md) are re-labeled screening-scope. Supersedes nothing.
