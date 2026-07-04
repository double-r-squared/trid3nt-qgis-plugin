// Broker WS durability repro -- Chromium against the broker ALB DIRECTLY.
//
// Isolates the per-session broker's frame-forwarding from CloudFront entirely:
// a real browser engine opens ws://<ALB>/ws?sid=<ULID>&st=<demo-token>, performs
// the SAME in-band handshake web/src/ws.ts performs (auth-token then
// session-resume), records EVERY received frame with a timestamp for ~70s, then
// sends a user-message and waits for an agent reply.
//
// MEASURES:
//   - heartbeat DATA frame arrival intervals (expect ~12s on a healthy proxy)
//   - whether the socket holds open >60s or closes/reconnects ~25s
//   - whether a user-message gets an agent-message/turn-complete reply
//
// Run: node web/tools/broker_ws_durability_repro.mjs
// (Chromium from web/node_modules; demo token minted from the live demo-token
// Lambda. NO CloudFront, NO /ws cutover, NO EC2 box touched.)
//
// ASCII hyphens only.

import { chromium } from "playwright";

const ALB = "grace2-agent-broker-872872610.us-west-2.elb.amazonaws.com";
const DEMO_TOKEN_URL =
  "https://9ib093sis6.execute-api.us-west-2.amazonaws.com/demo-token";
const DEMO_CODE = "trident-demo-4db31803";
// Default window: ~40s cold Fargate provision + >60s warm observation.
const OBSERVE_MS = Number(process.env.OBSERVE_MS || 130000);
// FAITHFUL=1 mirrors web/src/ws.ts's keepalive watchdog EXACTLY: a
// `session-resume` ping every 25s that arms a 10s pong-deadline cleared only by
// an inbound DATA frame; a missed deadline force-reconnects. This is what makes
// the cold-provision dead-window (first DATA frame at ~48s > 35s deadline)
// surface as reconnect churn against the broker. Default OFF so the plain run is
// a clean one-socket measurement.
const FAITHFUL = process.env.FAITHFUL === "1";
const KEEPALIVE_INTERVAL_MS = 25000; // ws.ts KEEPALIVE_INTERVAL_MS
const KEEPALIVE_PONG_TIMEOUT_MS = 10000; // ws.ts KEEPALIVE_PONG_TIMEOUT_MS

// Proper TIME-BASED Crockford base32 ULID (26 chars): 10-char 48-bit ms
// timestamp + 16 random chars. MUST be time-based -- a purely-random first char
// can exceed '7' and overflow the agent's 48-bit ULID timestamp validator
// (pydantic rejects it and the handler crashes). Mirrors contracts.newUlid.
function newUlid() {
  const ENC = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  let ts = Date.now();
  const out = new Array(26);
  for (let i = 9; i >= 0; i--) {
    out[i] = ENC[ts % 32];
    ts = Math.floor(ts / 32);
  }
  for (let i = 10; i < 26; i++) out[i] = ENC[Math.floor(Math.random() * 32)];
  return out.join("");
}

async function mintToken() {
  const r = await fetch(DEMO_TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: DEMO_CODE }),
  });
  if (!r.ok) throw new Error(`demo-token HTTP ${r.status}`);
  const j = await r.json();
  if (!j.id_token) throw new Error("demo-token: no id_token in response");
  return j.id_token;
}

async function main() {
  const token = await mintToken();
  const sid = newUlid();
  console.log(`[repro] minted demo token (len=${token.length}), sid=${sid}`);
  console.log(`[repro] target = ws://${ALB}/ws (broker ALB DIRECT)`);

  const browser = await chromium.launch();
  const page = await browser.newPage();
  page.on("console", (m) => console.log(`[page] ${m.text()}`));

  // http origin so the browser permits a ws:// (non-TLS) connect.
  await page.goto(`http://${ALB}/healthz`);

  const result = await page.evaluate(
    async ({ alb, sid, token, observeMs, faithful, kaInterval, kaPong }) => {
      const log = (...a) => console.log(a.join(" "));
      const t0 = Date.now();
      const rel = () => ((Date.now() - t0) / 1000).toFixed(2);
      const frames = [];
      const heartbeatTs = [];
      const reconnects = []; // {at, why} -- faithful-keepalive force-reconnects
      let userMsgSentAt = null;
      let agentReplyAt = null;
      let closeInfo = null;
      let firstInboundAt = null;
      let userMsgTimer = null;
      let ws = null;
      let keepaliveTimer = null;
      let pongDeadlineTimer = null;
      let userMsgArmed = false;

      const mkUlid = () => {
        const ENC = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
        let ts = Date.now();
        const out = new Array(26);
        for (let i = 9; i >= 0; i--) {
          out[i] = ENC[ts % 32];
          ts = Math.floor(ts / 32);
        }
        for (let i = 10; i < 26; i++)
          out[i] = ENC[Math.floor(Math.random() * 32)];
        return out.join("");
      };
      const env = (type, payload) => ({
        type,
        id: mkUlid(),
        ts: new Date().toISOString(),
        session_id: sid,
        payload,
      });

      const url = `ws://${alb}/ws?sid=${encodeURIComponent(
        sid,
      )}&st=${encodeURIComponent(token)}`;

      const sendUserMsg = () => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          userMsgSentAt = (Date.now() - t0) / 1000;
          log(`[ws] --> user-message "what is 2+2" @${rel()}s`);
          ws.send(
            JSON.stringify(
              env("user-message", {
                text: "what is 2+2",
                research_mode: "research",
                case_id: null,
              }),
            ),
          );
        } else {
          log(`[ws] cannot send user-message; socket not OPEN @${rel()}s`);
        }
      };

      // --- ws.ts-faithful keepalive (only when faithful=true) ---------------
      const clearPong = () => {
        if (pongDeadlineTimer !== null) {
          clearTimeout(pongDeadlineTimer);
          pongDeadlineTimer = null;
        }
      };
      const stopKeepalive = () => {
        if (keepaliveTimer !== null) {
          clearInterval(keepaliveTimer);
          keepaliveTimer = null;
        }
        clearPong();
      };
      const startKeepalive = () => {
        if (!faithful) return;
        stopKeepalive();
        keepaliveTimer = setInterval(() => {
          if (!ws || ws.readyState !== WebSocket.OPEN) return;
          ws.send(JSON.stringify(env("session-resume", { case_id: null })));
          if (pongDeadlineTimer === null) {
            pongDeadlineTimer = setTimeout(() => {
              pongDeadlineTimer = null;
              // No inbound DATA frame within the deadline -> ws.ts force-reconnects.
              const at = (Date.now() - t0) / 1000;
              reconnects.push({ at, why: "missed-pong" });
              log(
                `[ws] !! MISSED-PONG force-reconnect @${at.toFixed(2)}s ` +
                  `(no DATA frame within ${kaPong}ms of the 25s keepalive ping)`,
              );
              forceReconnect();
            }, kaPong);
          }
        }, kaInterval);
      };

      function connect() {
        // NO subprotocol -- the ?st query param carries the token (task spec).
        ws = new WebSocket(url);
        ws.addEventListener("open", () => {
          log(`[ws] OPEN @${rel()}s`);
          // SAME in-band handshake as web/src/ws.ts: auth-token FIRST, then
          // session-resume.
          ws.send(
            JSON.stringify(env("auth-token", { token: token, anonymous: false })),
          );
          ws.send(JSON.stringify(env("session-resume", { case_id: null })));
          startKeepalive();
        });
        ws.addEventListener("message", (ev) => {
          // ws.ts: EVERY inbound frame is proof-of-life -> clear the pong deadline.
          clearPong();
          let type = "?";
          try {
            type = JSON.parse(ev.data).type;
          } catch {
            /* non-JSON */
          }
          const at = (Date.now() - t0) / 1000;
          frames.push({ at, type });
          if (type === "heartbeat") heartbeatTs.push(at);
          if (firstInboundAt === null) {
            firstInboundAt = at;
            log(`[ws] first inbound frame @${at.toFixed(2)}s -> warm proxy live`);
          }
          if (!userMsgArmed) {
            userMsgArmed = true;
            userMsgTimer = setTimeout(sendUserMsg, 25000);
          }
          if (
            userMsgSentAt !== null &&
            agentReplyAt === null &&
            (type === "agent-message-chunk" ||
              type === "agent-message" ||
              type === "turn-complete" ||
              type === "pipeline-state")
          ) {
            agentReplyAt = at;
            log(`[ws] <-- AGENT REPLY type=${type} @${at.toFixed(2)}s`);
          }
          log(`[ws] <-- ${type} @${at.toFixed(2)}s`);
        });
        ws.addEventListener("close", (ev) => {
          closeInfo = { code: ev.code, reason: ev.reason, at: rel() };
          log(`[ws] CLOSE code=${ev.code} reason="${ev.reason}" @${rel()}s`);
        });
        ws.addEventListener("error", () => log(`[ws] ERROR @${rel()}s`));
      }

      function forceReconnect() {
        stopKeepalive();
        const stale = ws;
        ws = null;
        try {
          if (stale) stale.close();
        } catch {
          /* ignore */
        }
        connect();
      }

      return await new Promise((resolve) => {
        connect();
        setTimeout(() => {
          if (userMsgTimer) clearTimeout(userMsgTimer);
          stopKeepalive();
          const heldMs = Date.now() - t0;
          try {
            if (ws) ws.close();
          } catch {
            /* ignore */
          }
          const intervals = [];
          for (let i = 1; i < heartbeatTs.length; i++) {
            intervals.push(+(heartbeatTs[i] - heartbeatTs[i - 1]).toFixed(2));
          }
          resolve({
            frames,
            heartbeatTs,
            heartbeatIntervals: intervals,
            heartbeatCount: heartbeatTs.length,
            reconnects,
            firstInboundAt,
            userMsgSentAt,
            agentReplyAt,
            closeInfo,
            observedSeconds: heldMs / 1000,
            finalReadyState: ws ? ws.readyState : 3,
          });
        }, observeMs);
      });
    },
    {
      alb: ALB,
      sid,
      token,
      observeMs: OBSERVE_MS,
      faithful: FAITHFUL,
      kaInterval: KEEPALIVE_INTERVAL_MS,
      kaPong: KEEPALIVE_PONG_TIMEOUT_MS,
    },
  );

  await browser.close();

  console.log("\n========== REPRO RESULT ==========");
  console.log(`observed window:      ${result.observedSeconds.toFixed(1)}s`);
  console.log(`total frames:         ${result.frames.length}`);
  const byType = {};
  for (const f of result.frames) byType[f.type] = (byType[f.type] || 0) + 1;
  console.log(`frames by type:       ${JSON.stringify(byType)}`);
  console.log(
    `first inbound frame:  ${
      result.firstInboundAt === null
        ? "NONE (no frame ever arrived)"
        : result.firstInboundAt.toFixed(1) + "s (cold-provision window)"
    }`,
  );
  console.log(`heartbeat count:      ${result.heartbeatCount}`);
  console.log(`heartbeat intervals:  ${JSON.stringify(result.heartbeatIntervals)}`);
  console.log(
    `faithful keepalive:   ${FAITHFUL ? "ON (mirrors ws.ts 25s ping / 10s pong-deadline)" : "off"}`,
  );
  console.log(
    `force-reconnects:     ${result.reconnects.length}${
      result.reconnects.length
        ? " @ " + result.reconnects.map((r) => r.at.toFixed(1) + "s").join(", ")
        : ""
    }`,
  );
  console.log(
    `user-message sent at: ${
      result.userMsgSentAt === null ? "NOT SENT" : result.userMsgSentAt + "s"
    }`,
  );
  console.log(
    `agent reply at:       ${
      result.agentReplyAt === null ? "NO REPLY" : result.agentReplyAt + "s"
    }`,
  );
  console.log(
    `close:                ${
      result.closeInfo
        ? `code=${result.closeInfo.code} @${result.closeInfo.at}s`
        : "socket stayed OPEN (no close event)"
    }`,
  );
  const endAt = result.closeInfo
    ? Number(result.closeInfo.at)
    : result.observedSeconds;
  // Warm hold = time the live (post-first-frame) session survived. If no frame
  // ever arrived, warm hold is 0.
  const warmHold =
    result.firstInboundAt === null ? 0 : endAt - result.firstInboundAt;
  console.log(
    `VERDICT warm-hold>60s: ${
      warmHold > 60 ? "PASS" : "FAIL"
    } (warm session held ~${warmHold.toFixed(1)}s)`,
  );
  console.log("==================================\n");
}

main().catch((e) => {
  console.error("[repro] FATAL", e);
  process.exit(1);
});
