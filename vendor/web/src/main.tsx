import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// job-0285 — entry-level path switch. ROUTING RULE (full table + rationale
// in EntryRouter.tsx):
//   "/"        → landing ONLY when no GRACE-2 session key exists in
//                localStorage; otherwise passes straight through to the app
//                (keeps Playwright live-verify tooling and returning users
//                on the app, exactly as before this page existed).
//   "/app"     → the app, always.
//   "/privacy" → privacy policy, always.
import { EntryRouter } from "./EntryRouter";
// White-screen-of-death guard: any uncaught render throw inside the app tree
// must degrade to a DARK fallback (ErrorBoundary), never blank the root to
// white. Wraps EntryRouter (-> App) so the whole tree is covered.
import { ErrorBoundary } from "./components/ErrorBoundary";
// job-0166 — global sans-serif font baseline for body + form controls so
// the Cases / CaseView / ConfirmationDialog surfaces (and any future
// browser-default text) don't fall back to UA serif. See styles/global.css
// header comment for the reasoning.
import "./styles/global.css";

const root = document.getElementById("root");
if (!root) throw new Error("missing #root element");

createRoot(root).render(
  <StrictMode>
    <ErrorBoundary>
      <EntryRouter />
    </ErrorBoundary>
  </StrictMode>,
);
