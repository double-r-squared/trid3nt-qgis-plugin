# Reanalyze ledger

Decisions taken deliberately that NATE wants to RE-ANALYZE later - not
parked work (that lives in IDEAS), not deletions (DELETION_LEDGER):
choices that stand today with a stated trigger for re-examination.
Convention: one entry per decision - what was decided, why, the
evidence at decision time, and the REVISIT TRIGGER.

## 2026-09-03 - rain-on-grid outlet: startup transient accepted
DECIDED: the derived Z(Q) rating-curve outlet pins WITH a measured
startup transient - 26/60 listing samples show flux briefly entering
the outlet before runoff begins (max 2.31 m3/s, ~20,285 m3 = 0.46% of
the storm), visible as a small negative trough in the delivered
hydrograph. The "all-exiting" acceptance phrase was orchestrator
wording stricter than the physics; a wetting boundary under an
imposed level can legitimately pass a bounded transient inflow, and
inventing an activation threshold would be sad-path machinery
(happy-path law). Evidence: run 01M1N1YP436AY5MQFER74BV7SN;
continuity -2.4e-15; water balance closed to the m3.
REVISIT TRIGGER: (a) the calibration era's gauged rating curves land
(the derived curve is replaced - re-measure the transient); (b) any
case where the trough measurably distorts a delivered hydrograph
answer; (c) the spin-up item lands (a settled initial state may
remove the dry-catchment startup class entirely).

## 2026-09-04 - uri_registry: the handle indirection survives the transport collapse
DECIDED: the spec's dies-row reads "uri_registry translation layers
(~1,200 -> thin id-to-URI record)". Everything that TRANSLATES was cut
(the wms/tile display face, the gs:// scheme, both display-face
resolution branches), landing 1,205 -> 1,035. What survives is the
layer-handle indirection: the L<n> mint the model is shown, the emit
rewrite that hands it those handles instead of uris, the fuzzy
mangle-match and the placeholder resolution. Those resolve an ID, not
a scheme, and cutting them would revive the URI-hallucination class
the module was built to prevent - so the row was met at the layer it
names rather than by LOC, and the residual is reported instead of
taken. Evidence: docs/validation/emission-fold-store-conformance.md,
deviation 1.
REVISIT TRIGGER: (a) the emit rewrite is proven to leave NO path by
which a model can echo a raw store uri into a *_uri param - then the
fuzzy match and the placeholder branch are dead weight, not defense;
(b) a wave that changes what the model is shown for a layer (a
different handle vocabulary) - the indirection is re-derived, not
patched; (c) NATE reads the residual and rules the thin record is what
he meant.

## 2026-09-04 - sheet form-card default view: set slots + open mandatory, rest under advanced
DECIDED: the module-surface sheet's card shows what the template or a
fill set (with provenance) plus any mandatory slot still open; every
other slot sits under advanced, grouped by the dico's rubrique, greyed
with its engine default. Chosen over "level-0 slots always visible"
(the dico's ~220 core keywords for telemac2d) for card length.
Evidence: 376 telemac2d keywords, 220 at NIVEAU 0 (measured in-image).
REVISIT TRIGGER: NATE's standing intent - a SIDE-BY-SIDE evaluation of
the two views once the sheet is live, judged on which performs better
for a human and for the model filling a sheet; the winner replaces the
default. Backburner until fill/run has real use.
