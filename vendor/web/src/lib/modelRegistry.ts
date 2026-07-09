/**
 * modelRegistry.ts — single source of truth for selectable Bedrock models.
 *
 * Mirrors the Python-side SELECTABLE_MODELS list in
 * services/agent/src/grace2_agent/bedrock_adapter.py.  Both must be kept in
 * sync: the server enforces cachePoint gating per-model; this file drives the
 * in-chat selector UI + localStorage persistence.
 *
 * Accent colors are muted to fit the dark theme.  Provider palette:
 *   Anthropic — warm terracotta / clay (#c2603c)
 *   Amazon    — muted amber (#b8860b)
 *   DeepSeek  — slate blue / indigo (#5c7fa3)
 *
 * LOCAL build (VITE_DEPLOYMENT=local; fingerprint audit A10 + F2 live-feedback
 * 2026-07-08): the local agent serves models through Ollama, and the selector
 * lists the REAL installed models so they hot-swap per turn exactly like the
 * cloud registry. The list is fetched at startup from the agent's
 * GET /api/local-models endpoint (`refreshLocalModels`), which proxies
 * Ollama's /api/tags. Until that fetch resolves (or when it fails) the
 * registry holds the single generic "Local model" fallback entry, whose id
 * ("local-default") the agent's resolve_selected_model maps to None ("use the
 * server default"). Cloud registry is byte-identical when the flag is
 * unset/cloud.
 */

import { isLocalDeployment } from "./deployment";
import { httpBase } from "./public_base";

export interface ModelEntry {
  /** Bedrock model id / cross-region inference profile id. */
  id: string;
  /** Short human label shown in the selector popover. */
  label: string;
  /** Provider family shown as a secondary line in the popover. */
  provider: string;
  /**
   * Muted accent color used to tint the chat input border while this model
   * is active.  Must be readable against the dark background (#1a1a20).
   */
  accentColor: string;
  /** Whether this model supports Bedrock cachePoint prompt caching. */
  supportsPromptCache: boolean;
}

// Only models PROVEN to work in account 226996537797/us-west-2 AND to support the
// Converse toolConfig (function calling) the agent loop needs are listed here.
// Probed live 2026-06-17:
//   us.anthropic.claude-sonnet-4-6   OK + tool use            -> default
//   us.amazon.nova-pro-v1:0          OK + tool use            -> cheap, capable
//   us.amazon.nova-lite-v1:0         OK + tool use            -> cheapest
//   us.anthropic.claude-haiku-4-5-*  valid id but ACCESS NOT ENABLED — enable in
//       Bedrock console (Model access -> Claude Haiku 4.5) then uncomment below;
//       it is the strongest cheap+agentic Anthropic option.
//   us.deepseek.r1-v1:0              REJECTS toolConfig — cannot drive the agent
//       loop on Bedrock; intentionally OMITTED (no broken option in the picker).
const CLOUD_SELECTABLE_MODELS: ModelEntry[] = [
  {
    id: "us.anthropic.claude-sonnet-4-6",
    label: "Claude Sonnet 4.6",
    provider: "Anthropic",
    accentColor: "#c2603c",
    supportsPromptCache: true,
  },
  {
    id: "us.amazon.nova-pro-v1:0",
    label: "Nova Pro",
    provider: "Amazon",
    accentColor: "#b8860b",
    // Nova REJECTS Bedrock cachePoint ("extraneous key [cachePoint] is not
    // permitted") — caching is Anthropic-only. The server gates this; the flag
    // is kept truthful for any UI that reads it.
    supportsPromptCache: false,
  },
  {
    id: "us.amazon.nova-lite-v1:0",
    label: "Nova Lite",
    provider: "Amazon",
    accentColor: "#b8860b",
    supportsPromptCache: false,
  },
  {
    // Access enabled + verified working 2026-06-17. Anthropic -> cachePoint OK.
    // The strongest cheap + agentic Anthropic option.
    id: "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    label: "Claude Haiku 4.5",
    provider: "Anthropic",
    accentColor: "#c2603c",
    supportsPromptCache: true,
  },
  {
    // User-pickable ONLY (no auto-routing). Verified invokable with toolConfig
    // 2026-06-24. Anthropic -> cachePoint OK. Default stays Sonnet; the user
    // must deliberately select this, so prod cost is never silently bumped.
    id: "us.anthropic.claude-opus-4-5-20251101-v1:0",
    label: "Claude Opus 4.5",
    provider: "Anthropic",
    accentColor: "#c2603c",
    supportsPromptCache: true,
  },
];

// LOCAL build: shared cosmetics for local-runtime entries.
const LOCAL_ACCENT = "#5c7fa3";
const LOCAL_PROVIDER = "Local runtime";

/** The pre-fetch/fallback local id. The agent's resolve_selected_model maps it
 *  to None ("use the server default" = the configured local model). */
export const LOCAL_DEFAULT_MODEL_ID = "local-default";

// LOCAL build fallback: one generic entry describing the locally hosted model.
// Shown only until refreshLocalModels() replaces it with the REAL installed
// list (or permanently when the agent/Ollama is unreachable). No prompt-cache
// claim (cachePoint is Bedrock-only).
const LOCAL_FALLBACK_MODELS: ModelEntry[] = [
  {
    id: LOCAL_DEFAULT_MODEL_ID,
    label: "Local model",
    provider: LOCAL_PROVIDER,
    accentColor: LOCAL_ACCENT,
    supportsPromptCache: false,
  },
];

// LOCAL mode gets a mutable copy (refreshLocalModels splices the real list in
// place); CLOUD keeps the exact same frozen-by-convention array as before.
export const SELECTABLE_MODELS: ModelEntry[] = isLocalDeployment()
  ? [...LOCAL_FALLBACK_MODELS]
  : CLOUD_SELECTABLE_MODELS;

/** Bedrock-shaped id heuristic (mirrors the agent's openai_adapter guard) --
 *  used ONLY in local mode to drop a stale cloud id instead of sending it to
 *  the local runtime or synthesizing a bogus entry for it. */
function looksLikeBedrockId(id: string): boolean {
  // NOTE: deliberately NOT the agent's ":0" heuristic -- real Ollama tags can
  // contain ":0" (e.g. "qwen2:0.5b"). Bedrock inference-profile ids are
  // "us.<vendor>.<model>" or bare "<vendor>." prefixed.
  return (
    id.startsWith("us.") ||
    id.includes("anthropic") ||
    id.includes("amazon.") ||
    id.includes("deepseek.")
  );
}

/**
 * F2 (live-feedback 2026-07-08) -- LOCAL mode only: fetch the REAL installed
 * model list from the agent's `GET /api/local-models` (which proxies Ollama's
 * /api/tags) and replace the registry contents in place, configured default
 * first. Wire shape: `{models: [{id, label}], default: string|null}`.
 *
 * Never throws; returns the new entries or null (cloud mode / fetch failed /
 * empty list -- the generic fallback entry then stays, which is honest: the
 * agent still serves its configured default). Kicked off once at module eval
 * in local builds; callers already mounted re-read SELECTABLE_MODELS on their
 * next render (the selector popover renders fresh on open).
 */
export async function refreshLocalModels(
  fetchImpl: typeof fetch = fetch,
): Promise<ModelEntry[] | null> {
  if (!isLocalDeployment()) return null;
  try {
    const resp = await fetchImpl(`${httpBase()}/api/local-models`);
    if (!resp.ok) return null;
    const data: unknown = await resp.json();
    const raw =
      data && typeof data === "object"
        ? (data as { models?: unknown }).models
        : null;
    if (!Array.isArray(raw)) return null;
    const entries: ModelEntry[] = [];
    for (const m of raw) {
      const id = (m as { id?: unknown } | null)?.id;
      if (typeof id !== "string" || id.trim() === "") continue;
      const label = (m as { label?: unknown }).label;
      entries.push({
        id: id.trim(),
        label:
          typeof label === "string" && label.trim() !== "" ? label.trim() : id.trim(),
        provider: LOCAL_PROVIDER,
        accentColor: LOCAL_ACCENT,
        supportsPromptCache: false,
      });
    }
    if (entries.length === 0) return null;
    SELECTABLE_MODELS.splice(0, SELECTABLE_MODELS.length, ...entries);
    return entries;
  } catch {
    return null;
  }
}

// Kick off the local-model fetch once per page load. Guarded so cloud builds
// (the default) and the vitest module-eval path never issue a request.
if (
  isLocalDeployment() &&
  typeof window !== "undefined" &&
  typeof fetch === "function" &&
  import.meta.env.MODE !== "test"
) {
  void refreshLocalModels();
}

// SELECTABLE_MODELS is always non-empty (5 cloud entries / 1 local entry).
// The non-null assertions below are safe: the array is module-level and
// immutable after import.
// eslint-disable-next-line @typescript-eslint/no-non-null-assertion
export const DEFAULT_MODEL_ID = SELECTABLE_MODELS[0]!.id;

export const MODEL_STORAGE_KEY = "grace2.selected_model_id";

/** Look up a model entry by id; returns the default (Sonnet) when not found.
 *
 * LOCAL mode extra: the registry is dynamic (filled by refreshLocalModels
 * after module eval), so a locally-persisted Ollama id that is not (yet) in
 * the registry gets an honest synthesized entry (label = the id) rather than
 * being mislabeled as the default. Stale Bedrock-shaped ids and the legacy
 * "local-default" placeholder still fall back to the default entry. */
export function getModelById(id: string | null | undefined): ModelEntry {
  // eslint-disable-next-line @typescript-eslint/no-non-null-assertion
  if (!id) return SELECTABLE_MODELS[0]!;
  const found = SELECTABLE_MODELS.find((m) => m.id === id);
  if (found) return found;
  if (
    isLocalDeployment() &&
    id !== LOCAL_DEFAULT_MODEL_ID &&
    !looksLikeBedrockId(id)
  ) {
    return {
      id,
      label: id,
      provider: LOCAL_PROVIDER,
      accentColor: LOCAL_ACCENT,
      supportsPromptCache: false,
    };
  }
  // eslint-disable-next-line @typescript-eslint/no-non-null-assertion
  return SELECTABLE_MODELS[0]!;
}

/**
 * Load the persisted model id from localStorage; null when nothing stored OR the
 * stored id is no longer a selectable model. The validation matters: a previous
 * session may have persisted an id we have since removed (e.g. the malformed
 * `us.anthropic.claude-haiku-4-5` or DeepSeek-R1). Returning it verbatim would
 * send Bedrock an invalid/unsupported model id and throw a ConverseStream
 * ValidationException, so we drop unknown ids back to the default here.
 */
export function loadPersistedModelId(): string | null {
  try {
    const v = window.localStorage.getItem(MODEL_STORAGE_KEY);
    if (!v) return null;
    if (isLocalDeployment()) {
      // LOCAL: ids are dynamic (Ollama tags fetched after module eval), so a
      // registry-membership check here would drop a valid persisted pick on
      // every reload. Accept any non-Bedrock-shaped id verbatim (the agent
      // passes it to the local runtime, which raises honestly for a model it
      // does not have); the legacy "local-default" placeholder and stale
      // cloud ids fall back to the default.
      if (v === LOCAL_DEFAULT_MODEL_ID || looksLikeBedrockId(v)) return null;
      return v;
    }
    return SELECTABLE_MODELS.some((m) => m.id === v) ? v : null;
  } catch {
    return null;
  }
}

/** Persist the selected model id to localStorage. */
export function persistModelId(id: string): void {
  try {
    window.localStorage.setItem(MODEL_STORAGE_KEY, id);
  } catch {
    // ignore (private browsing mode)
  }
}
