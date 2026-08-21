# ADR 0293 -- fallback ladders, wave F1e: the tool entrypoint learns the ladder's own errors

Status: LANDED. Date: 2026-08-20. Surgical follow-up to ADR 0292 (F1d), from the
F1d final verifier's E6/E7 (tool-entrypoint) and B3d (walker-ordering) probes.

## What the verifier found

**E6/E7 (blocker).** 0292 fixed the WALKER to raise a retryable
`FALLBACK_LADDER_ERROR` instead of a coverage code when a fault (not a gap) hit a
rung, and fixed the two COMPOSER helpers (`geoclaw._fetch_topo_for_geoclaw`,
`schism._fetch_bathymetry_cog`) to re-raise that fault verbatim rather than wrap
it into `GEOCLAW_NO_BATHYMETRY` / `SCHISM_BATHYMETRY_UNAVAILABLE`. Nobody checked
what the REGISTERED TOOL entrypoints -- `geoclaw_inundation` and
`schism_tidal_hydro`, what the model actually calls -- did with that re-raised
exception. Their typed-exception tuples never learned `LadderRefused`/
`LadderGap`, so the fault fell to the catch-all `except Exception`, which fires
`logger.exception` for what is an EXPECTED condition and returns
`GEOCLAW_INTERNAL_ERROR` / `SCHISM_INTERNAL_ERROR` -- the exact code and
retryability loss 0292 had just fixed one layer down, reopened at the surface the
model actually sees. Proven live (E6, before the fix): a retryable MinIO fault
under the ETOPO rung surfaced as `{"error_code": "GEOCLAW_INTERNAL_ERROR", ...}`
with no retry signal.

**B3d (latent).** The walker's terminal-refusal branches checked
`declined_over_gap` BEFORE checking whether the last thing attempted was an
unrelated fault. `primary gaps -> alt1 declined (gap outstanding) -> alt2
permitted but transport-faults` matched `declined_over_gap` (alt1's decline
stood in front of the primary's gap) and returned first, so the walker reported
"declined at the fallback gate" with the capability's non-retryable coverage
code -- erasing both the fact that alt2 ever ran and its retryability. The
decline did not cause the refusal; alt2's fault did.

## What changed

### 1. The registered tools' typed tuples learn the ladder's exceptions

`geoclaw_inundation` (`trid3nt_server/workflows/geoclaw/inundation/inundation.py`)
and `schism_tidal_hydro` (`trid3nt_server/workflows/schism/tidal_hydro/tidal_hydro.py`)
now except `LadderRefused`/`LadderGap` explicitly, ahead of the catch-all. The
handler mirrors `sfincs/flood`'s pattern (0292 section 2): thread the
exception's own `error_code` (never the generic `*_INTERNAL_ERROR`), and --
since this envelope has no dedicated `retryable` field -- say the retryability
in the message when it's a retryable `FALLBACK_LADDER_ERROR` ("This is a
TRANSIENT fault under a fallback rung, not a bathymetry coverage verdict: RETRY
the same request."). A genuine coverage gap is UNCHANGED: it is already wrapped
into `GeoClawComposerError`/`SchismScenarioError` upstream (in
`_fetch_topo_for_geoclaw` / `_fetch_bathymetry_cog`) before it ever reaches this
handler, so `GEOCLAW_NO_BATHYMETRY` / `SCHISM_BATHYMETRY_UNAVAILABLE` still land
exactly as before this fix -- only the non-gap ladder-error path changes.

Both composer helpers already had their own local `from trid3nt_server.fallbacks
import LADDER_ERROR_CODE, LadderGap, LadderRefused`; hoisted to the module-level
import (the tool entrypoint needs the same names) and the now-redundant local
import removed.

### 2. The walker checks "did anything fault after the decline" before "was
something declined"

`walk_ladder`'s terminal-refusal block now runs the
`gap_note is not None and not isinstance(last_exc, LadderGap)` check (the
unrelated-fault branch, `FALLBACK_LADDER_ERROR` + the failing rung's own
`retryable`) BEFORE the `declined_over_gap` check. `last_exc` only carries a
non-gap exception when something was actually ATTEMPTED and faulted (a decline
never sets it), so the reordering is exact: whenever the last thing the walk
tried failed for its own reason, that fault is the truth about the refusal --
regardless of whether a decline also happened earlier in the same walk. The
`declined_over_gap` branch still fires, unchanged, whenever the last recorded
failure IS the gap itself (nothing after the decline was ever attempted, or
everything attempted also gapped) -- B3a/B3c/B3e are unaffected.

## Evidence

Verifier probes E6/E7/B3d, re-run after the fix (pytest, this repo):

- **E6** (retryable ladder fault through `TOOL_REGISTRY["geoclaw_inundation"]`):
  `{"status": "error", "error_code": "FALLBACK_LADDER_ERROR", "error_message":
  "FALLBACK_LADDER_ERROR: the fetch_topobathy primary rung failed with an
  untyped _Transient: MinIO unreachable. This is NOT a TOPOBATHY_COVERAGE_GAP
  ..."}` -- was `GEOCLAW_INTERNAL_ERROR` before.
- **E7** (genuine coverage gap through the same tool): unchanged --
  `{"error_code": "GEOCLAW_NO_BATHYMETRY", ...}`.
- **B3d** (walker, decline-then-later-fault): `error_code=FALLBACK_LADDER_ERROR
  retryable=True`, message names the transient cause and the rungs tried
  (`primary (primary 50%), alt1 (declined at the fallback gate), alt2
  (_Transient: ...)`) -- was `TEST_REFUSED retryable=False` before.

Pinned as repo tests in `tests/test_fallback_ladder.py`:
`test_a_transport_fault_after_a_decline_still_beats_the_decline_verdict` (walker
reorder), `test_geoclaw_tool_entrypoint_threads_a_retryable_ladder_fault` +
`test_geoclaw_tool_entrypoint_keeps_the_terminal_coverage_code_unchanged`, and
their `schism_tool_entrypoint_*` counterparts.

Suite: four slices at baseline (4 `fetch_resolution` in `[f-o]` + 2 `river_dye`
in `[p-r]`; `[a-e]` and `[s-z]` fully green), contracts 721, `ws_smoke`
`all_passed=True`, flood canary `status=ok`. No `workers/` path touched, so no
image rebuild.

## Also in this wave

`docs/specs/fetcher-fold-audit.md`'s `fetch_topobathy` bespoke citation still
named `_build_merged_topobathy`/`_merge_sources_rasterio`, both deleted in
ADR 0292 (section "Deleted"). Corrected to `_composite_sources_to_array`
(`trid3nt_server/data/fetchers/_router/hooks/topobathy.py:1002-1105`), the
function that carries the merge today.

## Consequences

- `geoclaw_inundation` / `schism_tidal_hydro` error envelopes may now carry
  `FALLBACK_LADDER_ERROR` alongside the composer-specific codes; a caller
  branching on error_code to detect "no bathymetry" must already be checking
  the specific code (`GEOCLAW_NO_BATHYMETRY` / `SCHISM_BATHYMETRY_UNAVAILABLE`),
  not treating every non-2xx envelope as terminal.
- No schema change, no cache-provenance bump, no worker-image rebuild.
