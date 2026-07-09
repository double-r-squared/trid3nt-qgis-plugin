// GRACE-2 web - deployment-mode seam (local-cloud fingerprint fixes,
// reports/reviews/local-cloud-fingerprints-2026-07-08.md A7/A8/A9/A10/A11/L1).
//
// ONE build-time flag decides which product this SPA is: the CLOUD stack
// (Vercel + CloudFront + Bedrock + AWS Batch) or the TRID3NT LOCAL build
// (vendored into trid3nt-local: Ollama + MinIO + local docker solvers).
//
// THE SEAM: `VITE_DEPLOYMENT`.
//   "local"           -> local deployment (trid3nt-local sets this)
//   anything else /
//   unset (default)   -> cloud. The Vercel build sets NOTHING and is
//                        byte-identical in behavior to the pre-seam app.
//
// HARD RULE: every local-vs-cloud divergence in the web tree gates on THIS
// module (isLocalDeployment / deploymentMode) - never on scattered
// import.meta.env reads - so the whole surface is auditable from one file.
//
// The env var is read at CALL time (not module eval), matching public_base.ts,
// so vitest can vi.stubEnv without vi.resetModules. In a real Vite build
// import.meta.env.VITE_DEPLOYMENT is statically inlined, so the check is free.

export type DeploymentMode = "cloud" | "local";

/** The build's deployment mode. Only the exact (trimmed, case-insensitive)
 *  value "local" selects local; every other value - including unset, empty,
 *  and typos - is CLOUD, so a misconfigured cloud build can never silently
 *  flip into local wording. */
export function deploymentMode(): DeploymentMode {
  const raw = (import.meta.env.VITE_DEPLOYMENT as string | undefined) ?? null;
  if (raw != null && raw.trim().toLowerCase() === "local") return "local";
  return "cloud";
}

/** True only for the TRID3NT LOCAL build (VITE_DEPLOYMENT=local). */
export function isLocalDeployment(): boolean {
  return deploymentMode() === "local";
}
