# 0023 - fetchable-observation validation cases + paper-first replication standard

Date: 2026-07-26. Status: accepted.

## Context

Malpasset (ADR 0022) is a non-US case: its observations are hand-transcribed
from papers, not fetched through our tools, so it exercises less of the
stack - and its "canonical" status was asserted to NATE without sources in
hand, which is unverifiable.

## Decision

1. Malpasset completes (in flight) as the LAST non-US case.
2. All future validation/calibration cases are events whose observations FLOW
   THROUGH OUR FETCHERS (USGS NWIS gauges - the from-the-start goal - STN HWMs,
   NOAA products). AMENDED 2026-09-09: the clause was written US-only and is
   refined to what it was always for - a case wherever the substrate can FETCH
   gauges. US events dominate that set by infrastructure, not by rule, and a
   hand-transcribed observation is what the clause refuses. Paper-first is
   unchanged.
3. Paper-first replication standard: a validation arc starts from a
   published, verified V&V study - full citations + data/model availability
   delivered to NATE for verification BEFORE any build; we then replicate
   that study's computed-vs-observed work with our tools. Citations are
   adversarially verified (links fetched, claims confirmed) before
   presentation.

## Consequence

Gauge time-series pairing (mode B) becomes the next live-validated path.
docs/validation/replication-candidates.md holds the vetted candidate list.
Supersedes the case-selection method of ADR 0022 (not its fidelity ladder).
