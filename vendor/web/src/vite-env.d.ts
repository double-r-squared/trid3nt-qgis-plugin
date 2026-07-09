/// <reference types="vite/client" />

interface ImportMetaEnv {
  // Deployment-mode seam (local-cloud fingerprint fixes 2026-07-08): "local"
  // selects the TRID3NT LOCAL build wording/behavior (CPUs not vCPUs, no
  // sleep/wake copy, local privacy copy, generic local model selector, local
  // tool-catalog source). Unset / any other value = CLOUD (the Vercel build
  // sets nothing and renders byte-identical). See lib/deployment.ts - the
  // ONLY module that may read this.
  readonly VITE_DEPLOYMENT?: string;
  readonly VITE_GRACE2_WS_URL?: string;
  readonly VITE_GRACE2_HTTP_URL?: string;
  // sprint-14-aws CloudFront/HTTPS: a single public origin (e.g.
  // "https://d123.cloudfront.net"). When set, the web derives wss://<domain>/ws
  // for the agent socket and https://<domain> for the HTTP base (catalog).
  // When unset, every URL derivation is byte-identical to today.
  readonly VITE_GRACE2_PUBLIC_BASE?: string;
  // auto-stop/wake infra (NATE 2026-06-17): the API-Gateway HTTP endpoint that
  // fronts the StartInstances "wake" Lambda (infra/aws-autostop). When the
  // always-on agent box is STOPPED by the idle-check Lambda it answers neither
  // the WebSocket nor any HTTP endpoint; the web POSTs here to ask the wake
  // Lambda to start it. Precedence: VITE_GRACE2_WAKE_URL > VITE_GRACE2_PUBLIC_BASE(/wake)
  // > null (wake disabled — dev/LAN, where the box is never auto-stopped).
  // GET this same endpoint to REPORT the box state WITHOUT waking it (asleep
  // detection); POST WAKES (StartInstances). See lib/wake.ts wakeState().
  readonly VITE_GRACE2_WAKE_URL?: string;
  // sleep/wake STAGE 2 (NATE 2026-06-18): the API-Gateway HTTP endpoint that
  // fronts the "case-view-url" signer Lambda (infra/aws-autostop view_sign).
  // GET <url>?case_id=<id> -> 200 {url, expires_in, mode} where `url` is a
  // pre-signed S3 GET to the case-view JSON snapshot (a CaseOpenEnvelopePayload
  // byte-identical to the WS case-open). A MISSING snapshot -> 404 {error}. The
  // web fetches this when a Case is opened while the agent box is asleep, so the
  // Case paints COLD (rasters + inline vectors) with only the composer waiting.
  // Precedence: VITE_GRACE2_CASE_VIEW_URL > VITE_GRACE2_PUBLIC_BASE(/case-view-url)
  // > null (cold-load disabled — dev/LAN). See lib/case_view.ts.
  readonly VITE_GRACE2_CASE_VIEW_URL?: string;
  // sleep/wake STAGE 2 (NATE 2026-06-19): the API-Gateway HTTP endpoint that
  // serves the user's CASES LIST (the SINGLE-GET sibling of the case-view
  // signer). GET <url> -> 200 {envelope_type:"case-list", cases:[...]} where the
  // body is byte-identical to the WS case-list. The web fetches this when the
  // Cases ROOT is viewed while the agent box is asleep, so the rail renders COLD.
  // Precedence: VITE_GRACE2_CASE_LIST_URL > VITE_GRACE2_PUBLIC_BASE(/case-list)
  // > null (cold-load disabled - dev/LAN). See lib/case_list.ts.
  readonly VITE_GRACE2_CASE_LIST_URL?: string;
  // data export (NATE 2026-06-19): the API-Gateway HTTP endpoint that packages a
  // case's data bundle (its rendered layers) into a single downloadable archive.
  // GET <url>?case_id=<id> -> 200 {url, size_bytes, layer_count} where `url` is a
  // pre-signed S3 GET to the packaged archive; the web triggers a browser
  // download of it. Requires a signed-in user (Authorization: Bearer <id-token>).
  // Precedence: VITE_GRACE2_CASE_EXPORT_URL > VITE_GRACE2_PUBLIC_BASE(/case-export-url)
  // > null (export disabled - dev/LAN). See lib/export.ts.
  readonly VITE_GRACE2_CASE_EXPORT_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
