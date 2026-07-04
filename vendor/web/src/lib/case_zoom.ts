// GRACE-2 web — Case-open snap-to-location (job-0280).
//
// Reopening a Case must fly the map camera to the Case's geography. The
// primary field for this — `CaseSummary.bbox` — is null in practice today,
// but the persisted per-turn `CaseChatMessage.map_command_emissions` carry
// the typed map-command dicts each turn emitted (contract:
// packages/contracts/src/grace2_contracts/case.py — entries are
// `{"command": "...", "args": {...}}` validated against ws.MAP_COMMAND_ARGS
// at write time). The original turn's `zoom-to` is therefore replayable
// verbatim: App.tsx extracts the LAST zoom-to from the rehydrated
// `session_state.chat_history` and pushes it through the SAME LayerPanelBus →
// Map.tsx fitBounds path the live envelope took (job-0068/0072 machinery).
//
// If no zoom-to exists anywhere in the history, the camera is left alone —
// root/new Cases behave exactly as before (no surprise CONUS reset, no jump).
//
// Pure module: no React, no side effects — unit-testable in isolation per the
// established pure-helper pattern (pipelineReducer / buildInterleavedStream).

import type { CaseChatMessage } from "../contracts";

/** The wire shape Map.tsx's zoom-to handler consumes (fitBounds on bbox).
 * Mirrors the local `ZoomToCommand` in Map.tsx — zoom-to is deliberately NOT
 * in the frozen contracts.ts MapCommandPayload union (deferred per job-0025),
 * so the type lives with its consumers. */
export interface ZoomToMapCommand {
  command: "zoom-to";
  args: { bbox: [number, number, number, number] };
}

/** Narrow an unknown to a valid [minLon, minLat, maxLon, maxLat] bbox:
 * exactly 4 finite numbers. Matches Map.tsx's own length-4 guard while
 * additionally rejecting NaN / Infinity / non-number entries so a malformed
 * persisted row can never produce a broken fitBounds call. */
export function asBbox(
  x: unknown,
): [number, number, number, number] | null {
  if (!Array.isArray(x) || x.length !== 4) return null;
  if (!x.every((v) => typeof v === "number" && Number.isFinite(v))) {
    return null;
  }
  return [x[0], x[1], x[2], x[3]] as [number, number, number, number];
}

/** Narrow one persisted map-command emission entry to a zoom-to command.
 *
 * Canonical persisted shape (case.py docstring):
 *   `{ "command": "zoom-to", "args": { "bbox": [...] } }`
 * Defensive secondary shape (a bare ZoomToArgs dump next to the command key):
 *   `{ "command": "zoom-to", "bbox": [...] }`
 *
 * Anything else — other commands, missing/malformed bbox — returns null.
 */
export function asZoomToCommand(entry: unknown): ZoomToMapCommand | null {
  if (entry === null || typeof entry !== "object") return null;
  const o = entry as Record<string, unknown>;
  if (o.command !== "zoom-to") return null;
  const args =
    o.args !== null && typeof o.args === "object"
      ? (o.args as Record<string, unknown>)
      : null;
  const bbox = asBbox(args?.bbox) ?? asBbox(o.bbox);
  if (bbox === null) return null;
  return { command: "zoom-to", args: { bbox } };
}

/**
 * Extract the LAST valid `zoom-to` across a Case's rehydrated chat history.
 *
 * Walks messages newest-first, and each message's `map_command_emissions`
 * last-entry-first, returning the first valid hit — i.e. the most recent
 * zoom-to the Case ever emitted, which is where the user was last looking.
 * Returns null when the history carries no replayable zoom-to (the caller
 * then leaves the camera alone).
 */
export function extractLastZoomTo(
  chat: CaseChatMessage[] | null | undefined,
): ZoomToMapCommand | null {
  if (!Array.isArray(chat)) return null;
  for (let i = chat.length - 1; i >= 0; i--) {
    const emissions: unknown = chat[i]?.map_command_emissions;
    if (!Array.isArray(emissions)) continue;
    for (let j = emissions.length - 1; j >= 0; j--) {
      const cmd = asZoomToCommand(emissions[j]);
      if (cmd !== null) return cmd;
    }
  }
  return null;
}
