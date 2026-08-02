# 0027 - session durability across WebSocket churn

Context: a mobile navigate-out/back (or any reconnect) opens a new WebSocket
while the old one is not always closed by the browser; a backgrounded socket
lingers until the slow transport ping-timeout reaps it, so a single browser
session could accumulate many live sockets. Separately, a reconnect must replay
the Case the user is actually in, not a stale server pointer.

Decision: track live connections per session and treat the client as the Case
authority on resume.
- Registry: `session_id -> set of live ServerConnection`. Single asyncio loop,
  one process, so a plain dict/set mutated from coroutine context needs no lock.
  A re-register is a no-op; an empty bucket is pruned so the dict cannot grow
  unbounded. Deregister on every handler exit path.
- Reap invariant: any per-session reap MUST exclude the keeper (the resuming
  connection), identified by OBJECT IDENTITY, before any close - mis-targeting
  kills the active tab. The reserved application close code 4408
  (`SESSION_SUPERSEDED_CLOSE_CODE`) marks a socket retired because a newer
  connection of the same session resumed.
- The eager per-session reap is currently DISABLED: it is incompatible with the
  dual-socket design (two sockets share one session_id) - it closed the
  legitimate sibling and killed its mid-stream turn with 4408. Re-enable only
  with a policy that preserves the dual-socket pair and never closes a socket
  whose session has an in-flight turn/solve. The registry itself stays (cheap,
  useful for observability).
- Case-authority resume: the client stamps its current `case_id` on
  `session-resume`; the server re-binds its active-Case pointer to it (only on a
  genuine change to a non-None Case) BEFORE replaying rendered layers, so a
  reconnect replays the Case the user is in even after an in-memory pointer went
  stale or was wiped by a restart. A bare resume with no stamp (older client)
  leaves the pointer untouched.
- Detach, do not cancel, on disconnect: the connection handler must NOT cancel a
  session's in-flight tasks on a socket drop. A long solve (e.g.
  `sfincs_flood -> wait_for_completion`, minutes) is detached and keeps running;
  cancelling it on a transient socket swap (StrictMode double-mount, reconnect)
  would docker-kill the solve. Genuine cancellation (stop button, same-stream
  supersede) still cancels.
- Keepalive vs fresh resume: the client sends an empty `session-resume` every
  ~25s as a proof-of-life ping, indistinguishable on the wire from a genuine
  fresh-socket resume. Layer-replay is gated on a per-connection fresh-resume
  latch (true only for the first resume of a fresh SessionState) so periodic
  keepalives never re-paint/un-hide layers or force a blocking persistence read.

Consequence: socket pileup is bounded by deregistration and the churn
root-causes (heartbeat + auth cold-reload), not by the eager reap; in-flight
solves survive reconnect; per-session layer/Case replay is correct and idempotent
across reconnect. The disabled-reap code path is retained verbatim for a future
policy-gated re-enable.
