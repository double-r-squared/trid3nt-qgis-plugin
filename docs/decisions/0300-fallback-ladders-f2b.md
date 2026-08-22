# ADR 0300 -- fallback ladders, wave F2b: the honesty riders

Status: LANDED. Date: 2026-08-21. Acts on the adversarial review of ADR 0299
(wave F2), which found F2's code sound and its RECORD wrong in three places
(corrected in 0299's own Correction section) plus a ranked list of live honesty
defects. This ADR is the code half.

## The shape shared by every rider

F2 taught the coastal composers to refuse honestly. Each rider is a place where
the refusal is real but something ELSE downstream of it is not: a fault class
that walks around the refusal, a claim about the result that the result does not
support, or a label that dies before the physics reads it.

## 1. A transport fault bought a different bed

`geoclaw/inundation.py` and `schism/tidal_hydro.py` both branch a `LadderGap` /
`LadderRefused` on its CODE (the F1e precedent): a transport fault under a rung
propagates with its retryability, a genuine coverage gap becomes a typed refusal
naming why the LAND-ONLY `fetch_dem` is not a substitute for a coastal bed.

Below that branch, both had a bare `except Exception`. `TopobathyUpstreamError`
is a `FetchError`, not a `Ladder*` subclass, so it fell through it -- and the
land-only leg served. A transient S3 5xx or a wedged CUDEM tile read swapped a
tsunami/tidal bed for a land DEM whose ocean is flat 0 m, which GeoClaw runs as
dry ground and SCHISM samples onto every wet node. Proven by running the new
tests against the pre-fix tree: `schism bathymetry fetch_topobathy failed: CUDEM
tile read wedged (503)` followed by `schism bathymetry fetch_dem failed:` -- the
land leg was reached.

FIXED, same branching rule one level out: a fault carrying an `error_code` AND a
truthy `retryable` is transport, not geography, so it surfaces as the composer's
own typed error wearing the FAULT's code (`TOPOBATHY_UPSTREAM_ERROR`) with "RETRY
the same request" in the message -- neither envelope has a retryable field, so
the retryability is said out loud, as `flood.py`'s `ladder_detail` does. An
untyped or non-retryable failure still falls through: `fetch_dem` remains the
correct source for an inland dam-break, which is the leg's only honest use.

## 2. A sizing function that was requested and never bound

`generate_mesh._build_coastal` stamped the constant `sizing_source`
"distance-to-shore + wavelength-to-depth sizing", and the tool docstring made the
same claim to the model.

The wavelength term is `h_wl = T_M2 * sqrt(g*depth) / wl`. `T_M2` is 44714 s and
the depth is clamped at 0.5 m, so its FLOOR is 9903 m; it is then clipped to
`max_edge_length_m` and min'd with the distance-to-shore size. At any coastal max
edge it is clipped away and binds for zero nodes. The mesh was sized by
distance-to-shore alone, for the whole life of the claim.

FIXED at the source of truth: the in-container sizer now counts how often the
clipped wavelength size actually bound, and writes the answer into its own
`sizing_functions` report; the composer COPIES that report instead of composing a
claim. The docstring drops the term. Live, AOI `(-85.45, 29.90, -85.35, 30.00)`,
`min_edge 40 m / max_edge 150 m`, through `trid3nt-local/mesh:latest`:

```
sizing_source: OSM natural=coastline + NHDPlus areal water domain;
  feature_sizing(distance_to_shore);
  wavelength_sizing(shallow_water,wl=10) REQUESTED BUT NEVER BOUND
  (smallest h_wl 9903 m >= max_edge_length 150 m; the mesh size is
  distance-to-shore alone)
```

`mesh_stats.json` also gains `wavelength_binding_fraction` and
`wavelength_h_wl_min_m`, so a future AOI where the term DOES bind reports the
share instead of the refusal. The sandbox script is bind-mounted (`-v
{sandbox}:/sandbox`), not baked, so no image rebuild is involved.

## 3. Fabricated bed provenance on the no-rows branch

`_bed_provenance` fell back to the literal `topobathy: CUDEM 1/9" + 3DEP land`
when the fetch reported no activation rows -- naming two sources as though it had
measured them. It is reachable (a fetch that reports no rows and no note), and it
is the same fabrication class as the constant `dem_source` that ADR 0299 deleted
for claiming CoNED. Separately, a row set whose coverages were all 0.0 rendered a
dangling `topobathy: `.

FIXED: rows are filtered to positive coverage BEFORE the branch, so an all-zero
set takes the no-rows path, and that path says
`topobathy (source UNMEASURED: the fetch reported no activation rows)`.

## 4. The bed note died before the physics

`bed_fallback_note` -- the label that says a coastal bed fell to the global ETOPO
relief -- rode the build turn's `LayerURI.fallback_note` and stopped there. It was
not in `MeshArtifact.provenance`, so it did not survive to a later session. And
`tidal_hydro`'s supplied-mesh branch stamped
`SyntheticInput(param="bathymetry", basis="user")` from a static template,
reading nothing from the artifact: a 60%-ETOPO bed and a 100%-CUDEM bed reached
the solve's input review indistinguishable.

FIXED end to end. `provenance` carries `bed_fallback_note` alongside
`dem_source`; the SCHISM mesh precondition gate returns a fourth element, the
artifact's bed provenance; the supplied-mesh branch stamps the bathymetry entry
with `basis="fetched"`, `consequence="physics"`, `real_source_if_any` = what
painted, and appends `DEGRADED BED: <note>` to both the entry and the run's
`fallback_note`. Live sidecar from the build above,
`s3://trid3nt-cache/mesh/01M0KND3F8RASH4XZSC83JJCBR/mesh_artifact.json`:

```
"dem_source": "topobathy: cudem_nearshore 100%",
"bed_fallback_note": null
```

(null because that AOI is fully CUDEM -- the honest value for an undegraded bed.)
The degraded path is pinned by a test driving the real branch with a 60/40
CUDEM/ETOPO provenance.

`pahm_surge` shares the gate and drops the new element: its review entries carry
no bathymetry row yet for the label to land on. Registered here, not hidden.

## 5. The walker could still escape untyped

`walk_ladder` raised a bare `ValueError` when a call site named an alternative the
ladder does not declare. ADR 0291 section 7's guarantee is that every failure out
of the walker is TYPED, and the composers' never-raise contracts except on
`LadderRefused` / `LadderGap` -- an untyped escape slips past them into a
catch-all. It is a call-site bug either way, but a bug that reaches the user as
`GEOCLAW_INTERNAL_ERROR` instead of a named one.

FIXED: it raises `LadderRefused(error_code=LADDER_ERROR_CODE, retryable=False)`.
The `Activation` is now constructed before the plan loop so the exception can
carry it. Two existing tests updated from `ValueError` to the typed error.

## 6. Stale docs

- `docs/design/fallback-ladders.md`: "three degradation classes plus three
  structural" -> four structural, `enhancement` named (it was added to both
  `Consequence` and `FallbackConsequence` in F2).
- `stratified.render_cards_context` and `search_data_catalog`: the card contents
  are "gates/caveats/endpoint mirrors", not "fallback" -- the word the F2 rename
  retired.
- `docs/DELETION_LEDGER.md`: the blank line F2 left between the SWMM-lane row and
  its own two rows, which broke the table. Three older blank lines in the same
  table predate F2 and are left alone.

## The register (ADR 0299 correction C3)

`_PARKED_SILENT_SUBSTITUTIONS` goes from three rows to eleven sites across all
nine parked audit rows, keyed by audit row rather than by file. See 0299's
Correction section for the table and the honest limit of a marker.

## Gates

Live, this box, MinIO + local docker.

- `[a-e]` 1475 passed / 5 skipped
- `[f-o]` 6635 passed / 3 skipped / 1 xfailed, the 4 `fetch_resolution` failures
- `[p-r]` 2102 passed / 2 skipped, the 2 `river_dye` failures
- `[s-z]` 1405 passed / 6 skipped
- contracts 721 passed
- `ws_smoke.py` `all_passed=True`
- flood canary `status=ok`, depth COG
  `s3://trid3nt-runs/01M0KPXMY1ZKSQZXJG126XSDA2/overviews/01M0KPYX21PRYEAT1A2S65Q5J2.tif`

Composer + mesh + sandbox code only. NO `workers/` path was touched, and the
sandbox mesher is bind-mounted rather than baked, so no image rebuild is
involved.

Refutation check: with the two composer fixes stashed, the three new transport /
bed-label tests FAIL, and the captured log shows the pre-fix schism path reaching
the land-only leg after the 503.

## Consequences

- `_schism_mesh_precondition_gate` returns a 4-tuple. `pahm_surge._surge_mesh_gate`
  unpacks and drops the new element; `baroclinic_circulation` has its own copy of
  the gate and is untouched.
- A coastal `fetch_topobathy` transport fault is now FATAL to a geoclaw or schism
  coastal_tin run instead of degrading it. The typed code is the fault's own and
  the message says to retry.
- `MeshArtifact.provenance` gained `bed_fallback_note`. Sidecars written before
  this wave do not have the key; readers use `.get`.
- `sizing_source` is now variable text sourced from `mesh_stats.json`. Anything
  matching it exactly will need to match on substrings.
- `walk_ladder` no longer raises `ValueError` for an undeclared `allow=` name.
