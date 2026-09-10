# Validation records

Every file here is a MEASUREMENT: a census, a conformance walk, a ledger or an
evaluation, dated and taken against the tree as it stood. A file in this folder
is not design space and not a proposal - when the thing it measured dies, the
file dies with it, and a later change that falsifies its wording does not
rewrite the finding.

The validation and calibration DESIGN space that used to live here as six
"STATUS: THINKING" notes is gone: the responsibility cut, the activation
boundary, the review-context rule and the metric background folded into
`docs/design/calibration-methodology.md` (appendix A), and the proposed build
order and the open-questions list were superseded by the campaign rulings in
`docs/IDEAS.md`.

The fetcher fold wave's measurements:
- fetcher-fold-census.md - the two-lens census by protocol family, 97 specs
- fetcher-fold-stage0.md - THE TRADE, per library, measured before any spec moved
- fetcher-fold-raster-half.md - the STAC stage
- fetcher-fold-hydro-stage.md - the HyRiver stage, its parity and the FEMA NFHL wall
- nlcd-manning-tables.md - our NLCD -> Manning table beside pygeohydro's, class by
  class, with each one's published source. A DESIGN-STOP for NATE: swapping tables
  changes run numbers, so nothing is switched
- section-vs-hyriver.md - pynhd.flowline_xsection and py3dep.elevation_profile
  measured against the section cut and the profile sampler. Neither folds
