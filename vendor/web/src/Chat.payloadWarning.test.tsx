// GRACE-2 web — Chat large-payload-warning routing tests (FIX 2, NATE 2026-06-17).
//
// The large-payload warning ("Large response expected" / >25 MB) is no longer a
// separate App-level banner "hat" — it is an IN-CHAT card interleaved in the
// per-Case chat stream, exactly like the credential / tool / sandbox cards.
//
// Chat itself cannot mount in happy-dom (it opens a WebSocket), so — following
// the established per-Case stream-routing test pattern — these tests exercise
// the exported pure route helpers directly:
//   - routePayloadWarning lands a PayloadWarningEnvelopePayload in the owning
//     stream and assigns a chronological arrival seq.
//   - duplicate warning_id emits (the session-scoped fan-out can deliver the
//     same envelope twice) do NOT stack a second card.
//   - recordPayloadResolved marks the proceed/cancel/narrow_scope decision
//     against the stream the card lives in.
//   - warning cards are per-Case (route to the owning stream; another Case's
//     stream is untouched); explicit caseId targeting overrides the in-flight
//     targetKey.

import { describe, it, expect, vi, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import {
  ROOT_STREAM_KEY,
  createChatStreams,
  getStream,
  routeUserMessage,
  routePayloadWarning,
  recordPayloadResolved,
} from "./Chat";
import {
  GranularitySuggestion,
  PayloadWarningEnvelopePayload,
} from "./contracts";
import { PayloadWarningInline } from "./components/PayloadWarningInline";
import { ResolutionPickerCard } from "./components/ResolutionPickerCard";

afterEach(() => {
  cleanup();
});

const CASE_A = "01CASEAAAAAAAAAAAAAAAAAAAA";
const CASE_B = "01CASEBBBBBBBBBBBBBBBBBBBB";

function warning(
  warningId: string,
  overrides: Partial<PayloadWarningEnvelopePayload> = {},
): PayloadWarningEnvelopePayload {
  return {
    envelope_type: "tool-payload-warning",
    warning_id: warningId,
    tool_name: "fetch_buildings",
    tool_args: { bbox: [-82, 26, -81, 27] },
    estimated_mb: 42.5,
    threshold_mb: 25,
    recommendation: "Consider narrowing the bbox to reduce payload size.",
    options: ["proceed", "narrow_scope", "cancel"],
    ...overrides,
  };
}

function granularity(): GranularitySuggestion {
  return {
    engine: "swmm",
    resolution_param: "target_resolution_m",
    suggested_resolution_m: 20,
    resolution_choices: [10, 20, 40],
    estimated_active_cells: 40000,
    estimated_solve_seconds: 120,
    vcpus: 8,
    compute_class: "c7i.2xlarge",
    cell_cap: 250000,
    coarsened: false,
    reason: "Balanced resolution for the requested area.",
    spot_label: null,
  };
}

// Mirrors the production render conditional in Chat.tsx's InterleavedChatStream
// payload-warning branch (InterleavedChatStream is not exported; the conditional
// is a one-liner). A `granularity`-bearing warning routes to the
// ResolutionPickerCard; otherwise the generic PayloadWarningInline renders.
function PayloadWarningEntry({
  w,
}: {
  w: PayloadWarningEnvelopePayload;
}): JSX.Element {
  if (w.granularity) {
    return (
      <ResolutionPickerCard
        warning={w}
        granularity={w.granularity}
        resolved={null}
        onDecide={vi.fn()}
      />
    );
  }
  return (
    <PayloadWarningInline warning={w} resolved={null} onDecide={vi.fn()} />
  );
}

describe("routePayloadWarning — in-chat payload-warning card routing (FIX 2)", () => {
  it("lands a payload warning in the owning stream with an arrival seq", () => {
    const cs = createChatStreams();
    // A turn is in flight for CASE_A (targetKey owns following envelopes).
    routeUserMessage(cs, CASE_A, "fetch every building in the county");
    routePayloadWarning(cs, warning("W1"));
    const s = getStream(cs, CASE_A);
    expect(s.payloadWarnings.map((w) => w.warning_id)).toEqual(["W1"]);
    expect(s.payloadSeqs.get("W1")).toBeGreaterThan(0);
  });

  it("de-dupes a duplicate warning_id (session-scoped fan-out can repeat)", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routePayloadWarning(cs, warning("W1"));
    routePayloadWarning(cs, warning("W1"));
    expect(getStream(cs, CASE_A).payloadWarnings).toHaveLength(1);
  });

  it("routes to the OWNING stream; another Case is untouched", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "A prompt");
    routePayloadWarning(cs, warning("W1"));
    expect(getStream(cs, CASE_A).payloadWarnings).toHaveLength(1);
    expect(getStream(cs, CASE_B).payloadWarnings).toHaveLength(0);
    expect(getStream(cs, ROOT_STREAM_KEY).payloadWarnings).toHaveLength(0);
  });

  it("explicit caseId targeting overrides the in-flight targetKey", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "A prompt");
    // A late payload-warning for CASE_B (the user navigated away) buffers into
    // B's stream, not the currently-owning A.
    routePayloadWarning(cs, warning("W2"), CASE_B);
    expect(getStream(cs, CASE_A).payloadWarnings).toHaveLength(0);
    expect(getStream(cs, CASE_B).payloadWarnings.map((w) => w.warning_id)).toEqual([
      "W2",
    ]);
  });

  it("interleaves with the user message: warning seq comes AFTER the prompt", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "fetch everything");
    routePayloadWarning(cs, warning("W1"));
    const s = getStream(cs, CASE_A);
    const userSeq = s.messageOrder.get("user-0")!;
    const warnSeq = s.payloadSeqs.get("W1")!;
    expect(warnSeq).toBeGreaterThan(userSeq);
  });
});

describe("recordPayloadResolved — proceed / cancel / narrow_scope", () => {
  it("marks a warning proceeded against its stream", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routePayloadWarning(cs, warning("W1"));
    recordPayloadResolved(cs, CASE_A, "W1", "proceed");
    expect(getStream(cs, CASE_A).payloadResolved.get("W1")).toBe("proceed");
  });

  it("marks a warning cancelled against its stream", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routePayloadWarning(cs, warning("W1"));
    recordPayloadResolved(cs, CASE_A, "W1", "cancel");
    expect(getStream(cs, CASE_A).payloadResolved.get("W1")).toBe("cancel");
  });

  it("marks a warning narrowed against its stream", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routePayloadWarning(cs, warning("W1"));
    recordPayloadResolved(cs, CASE_A, "W1", "narrow_scope");
    expect(getStream(cs, CASE_A).payloadResolved.get("W1")).toBe("narrow_scope");
  });
});

describe("payload-warning render routing - #154 granularity gate", () => {
  it("routes a warning WITH granularity to the ResolutionPickerCard", () => {
    render(<PayloadWarningEntry w={warning("W1", { granularity: granularity() })} />);
    expect(screen.getByTestId("resolution-picker-card")).toBeInTheDocument();
    expect(
      screen.queryByTestId("payload-warning-inline"),
    ).not.toBeInTheDocument();
  });

  it("routes a warning WITHOUT granularity to the existing PayloadWarningInline (back-compat)", () => {
    render(<PayloadWarningEntry w={warning("W1")} />);
    expect(screen.getByTestId("payload-warning-inline")).toBeInTheDocument();
    expect(
      screen.queryByTestId("resolution-picker-card"),
    ).not.toBeInTheDocument();
  });

  it("treats an explicit null granularity as absent (existing card)", () => {
    render(<PayloadWarningEntry w={warning("W1", { granularity: null })} />);
    expect(screen.getByTestId("payload-warning-inline")).toBeInTheDocument();
    expect(
      screen.queryByTestId("resolution-picker-card"),
    ).not.toBeInTheDocument();
  });
});
