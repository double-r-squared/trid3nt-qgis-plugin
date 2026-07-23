# 0020 - remote access is the same single user

Context: NATE wants the daemon on the PC and the QGIS plugin usable from the
laptop, identically to local use.
Decision: location transparency for the one-user daemon - NOT multi-user.
- One plugin setting (the server WS URL); the server advertises its data +
  HTTP endpoints in auth-ack, derived from the address the client dialed.
- The tailnet is the trust boundary (no TLS); an optional shared token
  (TRID3NT_ACCESS_TOKEN, off by default) is the only lock.
- Every connection is the SAME single user: cases, chat, layers live with
  the daemon; simultaneous sessions are supported but live turn updates go
  to the session that ran the turn.
Consequence: no service splitting, no auth system, no per-user isolation -
consistent with 0002 (monolith until >1 CONCURRENT USER, which remote
single-user access is not). Multi-user remains the explicit trigger for
revisiting both decisions together.
