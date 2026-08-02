# 0033 - sandbox containment posture on a single host

Context: on the retired GCP stack the real containment boundary for user code
execution was the Cloud Run Job's VPC connector plus an egress-deny firewall and
a read-only runtime service account. The live stack runs the agent on a single
EC2 host (and the local build runs on the user's machine); there is NO Cloud Run
job, NO VPC sandbox boundary, and NO per-invocation isolated runtime.

Decision: the sandbox hardening layer owns containment in-process, and the
code-exec approval gate (0011) is the primary control.
- Containment is provided by the in-process hardening (resource limits, import /
  builtin restrictions, jail where available), documented as best-effort on a
  shared host - NOT an equivalent of a network-isolated ephemeral job.
- User code execution stays gated by explicit user approval (0011) with an honest
  bounded timeout; the gate, not the network boundary, is the trust control.

Consequence: the containment story is honest about the single-host stack; a
future re-introduction of a network-isolated execution boundary would be a new
note superseding this one. Related: 0006 (local-only, cloud code stripped), 0011.
