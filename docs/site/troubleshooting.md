# TRID3NT Local -- Troubleshooting

The greatest hits from bringing the stack up and from the 3-pass tool sweep. Each entry:
symptom, root cause, fix.

---

## MinIO hijacks public NOAA buckets

**Symptom**: `fetch_glm_lightning`, `fetch_hrrr_forecast`, `fetch_hrrr_smoke` (and any other
tool reading public AWS open-data buckets anonymously) fail with empty listings, Access
Denied, or misleading "no data upstream" errors -- while the same tools work on cloud.

**Root cause**: `AWS_ENDPOINT_URL=http://127.0.0.1:9000` (the MinIO redirect) is honored
**globally** by boto3 (>= 1.28) and s3fs/aiobotocore. Anonymous reads of `noaa-goesNN` GLM
granules and the HRRR zarr mirror get silently redirected to MinIO, which has no such buckets.

**Fix**: shipped -- `server/src/grace2_agent/tools/_public_s3.py` pins
UNSIGNED public-bucket clients to the real `https://s3.<region>.amazonaws.com` endpoint
(cloud behavior unchanged, since the env var is unset there). If you add a new tool that
touches a public bucket, build its client/fs kwargs through `_public_s3` helpers -- never a
bare `boto3.client("s3")` or `fsspec.filesystem("s3", anon=True)`.

---

## Qwen3 thinking mode swallows all output

**Symptom**: the model "answers" but the chat renders no text; streaming content deltas are
empty; turns look dead even though Ollama logs show tokens being generated.

**Root cause**: Qwen3-family models default to thinking mode -- every token streams to the
reasoning channel, and the OpenAI-compatible `content` deltas arrive empty.

**Fix**: `GRACE2_OPENAI_EXTRA_SYSTEM=/no_think` in `.env.local` (shipped). The adapter appends
it to the system prompt, disabling thinking. Keep it set for any Qwen3 model; it is harmless
for models that ignore it.

---

## Cold discover index: bad routing right after startup

**Symptom**: the first prompts after an agent (re)start route badly -- generic `web_fetch`
instead of purpose-built fetchers, prose answers with no tool call -- then routing improves on
its own. The log shows
`tool_retrieval: discover index COLD; FAIL-OPEN to full registry`.

**Root cause**: tool retrieval never builds its index on the hot path (that would block on a
cold embedding-model load). Until the index is warm it fails open to the FULL 176-tool
registry, which measurably wrecks 8B-class selection (35.7% cold vs 57.1% warm on the
15-prompt bench).

**Fix**: shipped -- the server warms the index at startup in a background thread
(`tool_retrieval: discover index warmed at startup` in the log). Give a freshly started agent
a moment before benchmarking or demoing; if you see the COLD line mid-session, the warm failed
and a restart is the quickest recovery.

---

## Docker permission denied

**Symptom**: SFINCS/GeoClaw/SWAN dispatch fails with a docker-socket permission error, even
though `docker` works in another terminal.

**Root cause**: your user was added to the `docker` group after the current login session (or
the agent was started from a shell that predates the group membership). Group membership is
evaluated at login.

**Fix**: log out/in, or wrap docker-touching commands -- **including the agent start** -- in
`sg docker -c '...'`:

```sh
sg docker -c 'bash scripts/start_agent.sh'
```

---

## Heavy sync fetchers starve WebSocket handshakes

**Symptom**: while a long fetch chain runs (e.g. USACE NSI + river geometry), NEW browser
connections hang at the WS handshake, the UI shows connect flicker, or in-flight sessions drop
with no server error.

**Root cause**: synchronous tool bodies (multi-second rasterio merges, large downloads,
netCDF compute) running ON the asyncio event loop stall the server's heartbeat and accept
path. One abandoned sweep prompt's fetch chain was enough to block all new handshakes.

**Fix**: `GRACE2_SYNC_TOOL_OFFLOAD=global` in `.env.local` (shipped, armed 2026-07-06) --
every sync tool body is off-loaded to a thread. Global mode was proven safe on cloud before
being armed here. If you unset it, a hand-audited always-offload list still covers the known
pathological fetchers, but new heavy tools will not be covered.

---

## Playwright / fresh browser contexts see no cases

**Symptom**: an e2e script (or an incognito window) connects fine but the case list is empty,
even though cases exist in `data/persistence/`.

**Root cause**: local auth is anonymous -- the server mints a fresh ULID user per unknown
connection. A fresh Playwright context has no stored identity, so it IS a brand-new user, and
cases belong to the ULID that created them.

**Fix**: seed the identity before loading the app, as the e2e harnesses do:

```js
localStorage.setItem('grace2.anonymous_user_id', '<owner ULID>');
sessionStorage.setItem('grace2-save-gate-accepted', '1'); // bypass the save-gate prompt
```

The owner ULID for existing cases is visible in `logs/agent.log` (`auth-ack ... user_id=...`)
or in the persistence store.

---

## `:8766` stats / catalog datetime serialization warnings

**Symptom**: `logs/agent.log` fills with
`WARNING grace2_agent.telemetry shadow telemetry mongo write failed` +
`TypeError: Object of type datetime is not JSON serializable` (hundreds of occurrences), and
the shadow-selection (recall@K) telemetry that the `:8766` routing-quality/stats endpoints
read never accumulates.

**Root cause**: the shadow tool-retrieval telemetry document carries a raw Python `datetime`.
The cloud persistence backends accept that type; the local **FilePersistence** store is plain
`json.dump`, which cannot serialize it, so every shadow write fails.

**Fix / status**: benign but noisy -- telemetry is fail-open by design (never raises into the
turn), and per-tool-call telemetry still lands in `GRACE2_TELEMETRY_PATH`. The real fix
(isoformat-encode datetimes before the FilePersistence write) is an open item under the local
telemetry track (roadmap track 3). Until then, treat the WARNING as known noise and use the
JSONL telemetry file for stats.
