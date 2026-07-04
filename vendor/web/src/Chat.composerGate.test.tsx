// GRACE-2 web — composer-only gate machine tests (sleep/wake STAGE 2,
// NATE 2026-06-18).
//
// Chat itself cannot mount in happy-dom (it opens a WebSocket), so — per the
// established pure-helper pattern (pipelineReducer / mobileSheetContainerStyle)
// — these tests pin the EXPORTED phase deriver Chat composes for its composer
// slot. The contract:
//
//   - connected            -> "chat"        (live composer; status dot demoted).
//   - NOT connected + asleep + canWake -> "wake" (tap-to-wake in the slot).
//   - NOT connected + asleep + NOT canWake -> "connecting" (dev/LAN: no Wake UI
//     because there's no endpoint / tap handler to wake with).
//   - NOT connected + NOT asleep -> "connecting" (base, never auto-wakes).
//
// The scrollback + map are NOT gated by this — only the composer slot is.

import { describe, it, expect } from "vitest";
import { deriveComposerPhase } from "./Chat";

describe("deriveComposerPhase (composer-only gate)", () => {
  it("connected => chat (regardless of asleep/canWake)", () => {
    expect(deriveComposerPhase("connected", false, false)).toBe("chat");
    expect(deriveComposerPhase("connected", true, true)).toBe("chat");
    expect(deriveComposerPhase("connected", true, false)).toBe("chat");
  });

  it("not connected + asleep + canWake => wake", () => {
    expect(deriveComposerPhase("connecting", true, true)).toBe("wake");
    expect(deriveComposerPhase("reconnecting", true, true)).toBe("wake");
    expect(deriveComposerPhase("disconnected", true, true)).toBe("wake");
  });

  it("not connected + asleep but NOT canWake => connecting (dev/LAN; no Wake button)", () => {
    expect(deriveComposerPhase("connecting", true, false)).toBe("connecting");
    expect(deriveComposerPhase("reconnecting", true, false)).toBe("connecting");
  });

  it("not connected + NOT asleep => connecting (base; never auto-wakes)", () => {
    expect(deriveComposerPhase("connecting", false, true)).toBe("connecting");
    expect(deriveComposerPhase("reconnecting", false, true)).toBe("connecting");
    expect(deriveComposerPhase("disconnected", false, false)).toBe("connecting");
  });
});
