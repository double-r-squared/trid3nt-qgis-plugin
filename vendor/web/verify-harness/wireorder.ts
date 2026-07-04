// LIVE-VERIFY harness (panel job-0253b). Imports the REAL production GraceWs
// from web/src/ws.ts and drives its REAL open handler against a real gate-ON
// agent. No mock socket, no inject seam — the browser opens a real WebSocket
// and the production openSocket() runs verbatim. CDP captures the on-wire
// frame order; we ALSO record sends in-page as a cross-check.
import { GraceWs } from "../src/ws";

// Mode selected by query param: ?mode=token (fake JWT) | ?mode=anon (empty).
const params = new URLSearchParams(location.search);
const mode = params.get("mode") ?? "anon";
const wsUrl = params.get("url") ?? "ws://127.0.0.1:8905";

// A getter mirroring auth.ts getIdToken(): in token mode returns a structurally
// valid-looking (but unverifiable) JWT; in anon mode returns null exactly like
// the disabled-mode / signed-out path.
const fakeJwt =
  "eyJhbGciOiJSUzI1NiIsImtpZCI6ImZha2UifQ." +
  "eyJzdWIiOiJmYWtlLXVzZXIiLCJhdWQiOiJncmFjZS0yLWhhemFyZC1wcm9kIn0." +
  "ZmFrZS1zaWduYXR1cmUtbm90LXZlcmlmaWFibGU";
const idTokenGetter = async (): Promise<string | null> =>
  mode === "token" ? fakeJwt : null;

// Capture every frame the page SENDS (parsed type), as a cross-check to CDP.
(window as unknown as { __sentTypes: string[] }).__sentTypes = [];
const origSend = WebSocket.prototype.send;
WebSocket.prototype.send = function (this: WebSocket, data: unknown) {
  try {
    const t = JSON.parse(String(data)).type;
    (window as unknown as { __sentTypes: string[] }).__sentTypes.push(t);
  } catch {
    /* ignore non-JSON */
  }
  return origSend.call(this, data as string);
};

(window as unknown as { __events: string[] }).__events = [];
const log = (s: string) => {
  (window as unknown as { __events: string[] }).__events.push(s);
  const el = document.getElementById("frames");
  if (el) el.textContent += s + "\n";
};

const ws = new GraceWs(wsUrl, {
  onStatus: (s) => {
    log("status:" + s);
    const st = document.getElementById("status");
    if (st) st.textContent = s;
  },
  onAuthAck: (ack) =>
    log("auth-ack:" + JSON.stringify(ack)),
  onAuthExpired: () => log("auth-expired"),
  onError: (e) => log("error:" + JSON.stringify(e)),
  idTokenGetter,
  onAgentChunk: () => {},
  onPipelineState: () => {},
  onSessionState: () => {},
} as never);

(window as unknown as { __ws: GraceWs }).__ws = ws;
log("mode=" + mode + " url=" + wsUrl);
ws.connect();
