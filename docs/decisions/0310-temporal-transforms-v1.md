# 0310 - temporal transforms v1: `.resample()` / `.normalize()` on the Data declaration

## Context

`event_time` (ADR 0309) pinned WHICH moment a source was read at. It said
nothing about the next mismatch down: a source's CADENCE against the cadence
its consumer needs (6-min CO-OPS vs hourly NWM vs a solver's dt), or a source's
UNITS against the units a deck keyword reads. Both were handled, where they were
handled at all, by whichever consumer happened to notice - implicit, undeclared,
and invisible in the record. That is the wave-A clocks-align bug class: a value
silently realigned is indistinguishable from a value that was observed.

## Decision

Cadence and units become MODIFIERS ON THE `Data` DECLARATION, alongside
`.byo()` and `.ladder()`:

    Data("rain", Fetch.tool(...)
            .ladder("gridmet_domain_mean", "user_rate")
            .resample(to="1D", max_gap="native*3")
            .normalize(units="mm/day"))

`trid3nt_server/workflows/lib/temporal.py` is one shared implementation.
pandas does the arithmetic; the module is the doctrine around it:

1. THE QUANTITY CLASS PICKS THE METHOD, not the caller. RATE resamples
   conservatively - the interval mean going down, hold-the-interval going up,
   both mass-preserving; STATE interpolates linearly; CATEGORICAL moves by
   nearest. A declaration may override the first two. Asking to average class
   labels is a `MODIFIER_ILLEGAL` refusal: labels have no mean and no slope.
2. INTERPOLATION IS DECLARED. Every call returns a provenance stamp -
   `"resampled 6h->1h linear"`, `"converted in/day->mm/day"`, or
   `"native 1D matches the declared 1D rate, no resample"` - and a payload with
   no `.resample()` is never realigned. An identity transform still says so:
   "nothing moved" is a claim a reader has to be able to check.
3. A HOLE WIDER THAN `max_gap` REFUSES (`TEMPORAL_GAP_UNBRIDGED`), naming the
   hole, its end, the bound and the native cadence. Within-cadence
   interpolation is refinement; drawing a line across missing hours is
   invention. Default `"native*3"`; native is the record's LOWER-MEDIAN sample
   spacing (robust to a hole, and never a spacing the record does not contain -
   an interpolating median turns 6h and 12h into a 9h cadence nothing was
   sampled at).
4. UNITS convert through an EXPLICIT table (length / flow / depth-rate /
   temperature / concentration), not a units engine. An unlisted unit refuses
   and names the table; a cross-dimension request refuses as an invented
   relationship. Affine units (K, degF) carry an offset, so the table is
   factor + offset rather than a bare scale.

The declaration travels TO THE PRODUCER on the same channel `.ladder()` uses
(`kwargs.setdefault("temporal", ...)` in `_produce`), because the producer is
the only party that knows the payload's quantity class and native cadence. The
interpreter never reshapes a payload it cannot read - the no-double-middleware
law applied to time.

A SINGLE-VALUE payload accepts `.normalize()` and a `.resample()` at its own
cadence, and refuses a `.resample()` to any other (`TEMPORAL_NOT_RESAMPLEABLE`):
one number carries no time axis to redistribute, so honoring the request would
mean manufacturing the series it asked for.

## Adoption

`Data("rain")` in `telemac_river_dye` - the only declared FORCING `Data` in the
repo - now declares `.resample(to="1D", max_gap="native*3").normalize(
units="mm/day")`. Both ladder rungs deliver a daily rate (gridMET's aggregate
is time-reduced over the window by the router; a user rate is stated per day)
and TELEMAC's single RAIN OR EVAPORATION keyword reads mm/day, so the declared
transform resolves to a stamped identity:

    150.0 mm/day (user) -> net +150.0 mm/day (distributed on-mesh)
    [native 1D matches the declared 1D rate, no resample;
     units mm/day (declared, unchanged)]

The stamp rides a new `rain_or_evap_mm_per_day` `SyntheticInput` row on the
published layer (the `event_time` pinning style), which the run had no
provenance row for at all before this wave. The transform is not decorative:
a sub-daily target now REFUSES rather than manufacturing a storm shape gridMET
never reported, and a unit target the deck cannot carry refuses at the
producer. `telemac_do_sag` declares no `Data` and is untouched.

## Consequence

- An identity adoption is what an already-cadence-matched site should produce.
  The first transform that MOVES a value arrives with a series-shaped declared
  forcing - SWMM waves B/C, and the CO-OPS water-level series when
  `coastal_tidal_surge` migrates onto the library. The mechanism for that day is
  landed and tested here against synthetic series across all three quantity
  classes; what waits is a consumer, not a design.
- The FORM BADGE (the third blessed surface) is NOT wired. The form card is a
  param sheet; a `Data` row on it needs a `ParamSheet` contract change plus a
  plugin change, neither of which belongs in a library wave. The provenance
  stamp and the typed refusals are the two surfaces that ship.
- `resolve_rain_forcing` gained a `temporal` keyword defaulted to `None`, so
  every caller that does not declare a transform is byte-identical - including
  its note, which only grows the `[...]` stamp when a declaration exists.
- The unit table is deliberately small. It grows by entry, at the site that
  needs the entry, and a `speed` dimension was left out precisely because `m/s`
  already means a depth rate in the only block that reads it; a units engine
  would have resolved that collision silently.
