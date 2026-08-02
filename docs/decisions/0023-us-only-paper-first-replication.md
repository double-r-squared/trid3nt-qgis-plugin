# 0023 - US-only validation cases + paper-first replication standard

Date: 2026-07-26. Status: accepted.

## Context

Malpasset (ADR 0022) is a non-US case: its observations are hand-transcribed
from papers, not fetched through our tools, so it exercises less of the
stack - and its "canonical" status was asserted to NATE without sources in
hand, which is unverifiable.

## Decision

1. Malpasset completes (in flight) as the LAST non-US case.
2. All future validation/calibration cases are US events whose observations
   flow through our fetchers (USGS NWIS gauges - the from-the-start goal -
   STN HWMs, NOAA products).
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
