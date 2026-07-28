# 0031 - LayerURI emission integrity

Context: LLM-authored LayerURIs were mangled in many ways observed live (a `runs/`
prefix rewrite, a layer_id substituted as basename, hallucinated hash tails, a
WMS URL passed as a hazard URI, an invented cache hash). Prompt-engineering
patches only lowered the rate. Separately, an early plan to sign every
client-bound LayerURI as a direct-fetch signed URL did not fit the delivery
paths (the browser does not fetch object storage directly; layers arrive via
WMS / inline GeoJSON / chart payloads).

Decision: LayerURI integrity is enforced in code at the emission seam, not by
prompting or by client-side signing.
- A canonicalization/validation registry is the single seam that resolves and
  validates a LayerURI before it is emitted; a URI it cannot resolve is a typed
  error, never a guessed rewrite. This eliminates the LLM-URI-mangling incident
  class structurally.
- No client-side signed URLs: the emitter delivers via WMS / inline GeoJSON /
  chart payloads. If a direct-fetch feature ever lands, it extends this one seam.
- `_session_safe_send`: a mid-turn send targets the socket captured at dispatch,
  which a reconnect leaves dead while the detached turn runs on (see 0027). The
  send tries the captured socket, then falls back to any live socket registered
  for the session, and never raises - a dead socket cannot abort the turn.

Consequence: a layer either emits with a valid, resolvable URI or fails with an
honest typed error; emission survives socket churn. Related: 0014, 0027.
