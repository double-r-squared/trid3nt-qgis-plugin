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
 */

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
export const SELECTABLE_MODELS: ModelEntry[] = [
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

// SELECTABLE_MODELS is always non-empty (5 entries defined above).
// The non-null assertions below are safe: the array is module-level and
// immutable after import.
// eslint-disable-next-line @typescript-eslint/no-non-null-assertion
export const DEFAULT_MODEL_ID = SELECTABLE_MODELS[0]!.id;

export const MODEL_STORAGE_KEY = "grace2.selected_model_id";

/** Look up a model entry by id; returns the default (Sonnet) when not found. */
export function getModelById(id: string | null | undefined): ModelEntry {
  // eslint-disable-next-line @typescript-eslint/no-non-null-assertion
  if (!id) return SELECTABLE_MODELS[0]!;
  // eslint-disable-next-line @typescript-eslint/no-non-null-assertion
  return SELECTABLE_MODELS.find((m) => m.id === id) ?? SELECTABLE_MODELS[0]!;
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
