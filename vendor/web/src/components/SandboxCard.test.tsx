// GRACE-2 web — SandboxCard unit tests (sprint-13, job-0234).
//
// Tests (per kickoff requirements):
//   1. REQUEST state renders code block + gate buttons (Proceed + Cancel).
//   2. Proceed emits the confirm reply decision with correct id.
//   3. Cancel emits cancel reply.
//   4. RUNNING state renders spinner when decided=proceed + no result.
//   5. RESULT states: ok (green chip), error (red chip), timeout (amber chip), blocked (red chip).
//   6. truncated=true marker shown.
//   7. stdout/stderr collapsible sections shown when present.
//   8. malformed payload (missing code_exec_id) is handled gracefully.
//   9. rationale line shown when present; absent when null.
//  10. layer_refs section shown when present.
//  11. Save button present in result state.
//  12. Buttons disabled after decision.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import {
  SandboxCard,
  type CodeExecRequestPayload,
  type CodeExecResultPayload,
  type SandboxCardDecision,
} from "./SandboxCard";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const BASE_REQUEST: CodeExecRequestPayload = {
  envelope_type: "code-exec-request",
  code_exec_id: "01J0000000000000000000001A",
  python_code: "import numpy as np\nresult = np.mean([1, 2, 3])",
  layer_refs: {},
  rationale: "Computing the mean of a test array",
};

const OK_RESULT: CodeExecResultPayload = {
  envelope_type: "code-exec-result",
  code_exec_id: "01J0000000000000000000001A",
  status: "ok",
  stdout_tail: "stdout line 1\nstdout line 2",
  stderr_tail: "",
  result: { kind: "json", value: 2.0 },
  truncated: false,
  duration_s: 0.42,
};

const ERROR_RESULT: CodeExecResultPayload = {
  envelope_type: "code-exec-result",
  code_exec_id: "01J0000000000000000000001A",
  status: "error",
  stdout_tail: "",
  stderr_tail: "Traceback (most recent call last):\n  File ...\nZeroDivisionError: division by zero",
  result: null,
  truncated: false,
  duration_s: 0.11,
};

const TIMEOUT_RESULT: CodeExecResultPayload = {
  envelope_type: "code-exec-result",
  code_exec_id: "01J0000000000000000000001A",
  status: "timeout",
  stdout_tail: "",
  stderr_tail: "SandboxTimeoutError: execution exceeded 60s",
  result: null,
  truncated: false,
  duration_s: 60.1,
};

const BLOCKED_RESULT: CodeExecResultPayload = {
  envelope_type: "code-exec-result",
  code_exec_id: "01J0000000000000000000001A",
  status: "blocked",
  stdout_tail: "",
  stderr_tail: "SandboxNetworkBlocked: egress to example.com:80 denied",
  result: null,
  truncated: false,
  duration_s: 0.05,
};

const TRUNCATED_RESULT: CodeExecResultPayload = {
  ...OK_RESULT,
  truncated: true,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderRequest(
  overrides?: Partial<CodeExecRequestPayload>,
  decided: SandboxCardDecision | null = null,
  onDecide = vi.fn(),
) {
  const req = { ...BASE_REQUEST, ...overrides };
  render(
    <SandboxCard request={req} decided={decided} onDecide={onDecide} />,
  );
  return { req, onDecide };
}

function renderWithResult(
  result: CodeExecResultPayload,
  decided: SandboxCardDecision | null = "proceed",
  onDecide = vi.fn(),
) {
  render(
    <SandboxCard
      request={BASE_REQUEST}
      result={result}
      decided={decided}
      onDecide={onDecide}
    />,
  );
  return { onDecide };
}

// NATE 2026-06-26: a resolved/cancelled card folds to a compact summary; the
// full result chrome lives behind the expander. Tests that assert on the detail
// chrome must expand the card first.
function expandCard() {
  fireEvent.click(screen.getByTestId("sandbox-card-expand"));
}

// ---------------------------------------------------------------------------
// REQUEST state
// ---------------------------------------------------------------------------

describe("SandboxCard — REQUEST state (no decision yet)", () => {
  afterEach(() => cleanup());

  it("renders the code block", () => {
    renderRequest();
    const code = screen.getByTestId("sandbox-card-code");
    expect(code.textContent).toContain("import numpy as np");
    expect(code.textContent).toContain("result = np.mean");
  });

  it("renders the Proceed button", () => {
    renderRequest();
    expect(screen.getByTestId("sandbox-card-proceed")).toBeTruthy();
  });

  it("renders the Cancel button", () => {
    renderRequest();
    expect(screen.getByTestId("sandbox-card-cancel")).toBeTruthy();
  });

  it("Cancel is rightmost (comes after Proceed in DOM order)", () => {
    renderRequest();
    const actions = screen.getByTestId("sandbox-card-actions");
    const buttons = actions.querySelectorAll("button");
    // Proceed first, Cancel last.
    expect(buttons[0]!.dataset.testid).toBe("sandbox-card-proceed");
    expect(buttons[buttons.length - 1]!.dataset.testid).toBe("sandbox-card-cancel");
  });

  it("renders rationale when present", () => {
    renderRequest();
    const rat = screen.getByTestId("sandbox-card-rationale");
    expect(rat.textContent).toBe("Computing the mean of a test array");
  });

  it("does NOT render rationale when null", () => {
    renderRequest({ rationale: null });
    expect(screen.queryByTestId("sandbox-card-rationale")).toBeNull();
  });

  it("renders layer refs section when layer_refs is non-empty", () => {
    renderRequest({ layer_refs: { flood_depth: "gs://bucket/runs/layer-123.tif" } });
    expect(screen.getByTestId("sandbox-card-layer-refs")).toBeTruthy();
  });

  it("does NOT render layer refs section when layer_refs is empty", () => {
    renderRequest({ layer_refs: {} });
    expect(screen.queryByTestId("sandbox-card-layer-refs")).toBeNull();
  });

  it("no status chip in REQUEST state", () => {
    renderRequest();
    expect(screen.queryByTestId("sandbox-card-status-chip")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Confirm wiring (Proceed/Cancel)
// ---------------------------------------------------------------------------

describe("SandboxCard — gate buttons emit correct decisions", () => {
  afterEach(() => cleanup());

  it("Proceed calls onDecide with 'proceed'", () => {
    const onDecide = vi.fn();
    renderRequest({}, null, onDecide);
    fireEvent.click(screen.getByTestId("sandbox-card-proceed"));
    expect(onDecide).toHaveBeenCalledOnce();
    expect(onDecide).toHaveBeenCalledWith("proceed");
  });

  it("Cancel calls onDecide with 'cancel'", () => {
    const onDecide = vi.fn();
    renderRequest({}, null, onDecide);
    fireEvent.click(screen.getByTestId("sandbox-card-cancel"));
    expect(onDecide).toHaveBeenCalledOnce();
    expect(onDecide).toHaveBeenCalledWith("cancel");
  });

  it("gate buttons hidden after decision is made", () => {
    renderRequest({}, "proceed");
    expect(screen.queryByTestId("sandbox-card-actions")).toBeNull();
  });

  // NATE 2026-06-26: the verbose "Decision sent: <x>" footer is removed. A
  // proceed decision with no result yet is the RUNNING state (running indicator,
  // no footer). The folded compact summary covers the decided/resolved states.
  it("no verbose decision footer after a proceed decision (running state)", () => {
    renderRequest({}, "proceed");
    expect(screen.queryByTestId("sandbox-card-decision-footer")).toBeNull();
    expect(screen.getByTestId("sandbox-card-running")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// RUNNING state
// ---------------------------------------------------------------------------

describe("SandboxCard — RUNNING state", () => {
  afterEach(() => cleanup());

  it("shows running indicator when decided=proceed and no result", () => {
    render(
      <SandboxCard
        request={BASE_REQUEST}
        decided="proceed"
        onDecide={vi.fn()}
      />,
    );
    expect(screen.getByTestId("sandbox-card-running")).toBeTruthy();
  });

  it("does NOT show running indicator when decided=cancel", () => {
    render(
      <SandboxCard
        request={BASE_REQUEST}
        decided="cancel"
        onDecide={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("sandbox-card-running")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// RESULT states — status chips
// ---------------------------------------------------------------------------

describe("SandboxCard — RESULT state status chips", () => {
  afterEach(() => cleanup());

  // NATE 2026-06-26: the status chip lives in the header chrome, now behind the
  // expander on a folded resolved card.
  it("ok status: chip with 'ok' text", () => {
    renderWithResult(OK_RESULT);
    expandCard();
    const chip = screen.getByTestId("sandbox-card-status-chip");
    expect(chip.textContent).toBe("ok");
    expect(chip.dataset.status).toBe("ok");
  });

  it("error status: chip with 'error' text", () => {
    renderWithResult(ERROR_RESULT);
    expandCard();
    const chip = screen.getByTestId("sandbox-card-status-chip");
    expect(chip.textContent).toBe("error");
    expect(chip.dataset.status).toBe("error");
  });

  it("timeout status: chip with 'timeout' text", () => {
    renderWithResult(TIMEOUT_RESULT);
    expandCard();
    const chip = screen.getByTestId("sandbox-card-status-chip");
    expect(chip.textContent).toBe("timeout");
    expect(chip.dataset.status).toBe("timeout");
  });

  it("blocked status: chip with 'blocked' text", () => {
    renderWithResult(BLOCKED_RESULT);
    expandCard();
    const chip = screen.getByTestId("sandbox-card-status-chip");
    expect(chip.textContent).toBe("blocked");
    expect(chip.dataset.status).toBe("blocked");
  });
});

// ---------------------------------------------------------------------------
// RESULT state — content sections
// ---------------------------------------------------------------------------

describe("SandboxCard — RESULT state content", () => {
  afterEach(() => cleanup());

  // NATE 2026-06-26: the result chrome now folds; expand the card to assert on
  // the detail sections.
  it("shows result section when result is non-null (after expand)", () => {
    renderWithResult(OK_RESULT);
    expandCard();
    expect(screen.getByTestId("sandbox-card-result-section")).toBeTruthy();
  });

  it("shows scalar result inline (after expand)", () => {
    renderWithResult(OK_RESULT);
    expandCard();
    const scalar = screen.getByTestId("sandbox-result-scalar");
    expect(scalar.textContent).toBe("2");
  });

  it("shows stdout toggle when stdout_tail present (after expand)", () => {
    renderWithResult(OK_RESULT);
    expandCard();
    expect(screen.getByTestId("sandbox-card-stdout")).toBeTruthy();
  });

  it("stdout content hidden by default (collapsed)", () => {
    renderWithResult(OK_RESULT);
    expandCard();
    expect(screen.queryByTestId("sandbox-card-stdout-content")).toBeNull();
  });

  it("stdout toggle opens the content", () => {
    renderWithResult(OK_RESULT);
    expandCard();
    fireEvent.click(screen.getByTestId("sandbox-card-stdout-toggle"));
    expect(screen.getByTestId("sandbox-card-stdout-content")).toBeTruthy();
    expect(screen.getByTestId("sandbox-card-stdout-content").textContent).toContain("stdout line 1");
  });

  it("shows stderr toggle when stderr_tail present (after expand)", () => {
    renderWithResult(ERROR_RESULT);
    expandCard();
    expect(screen.getByTestId("sandbox-card-stderr")).toBeTruthy();
  });

  it("does NOT show stdout toggle when stdout_tail is empty (after expand)", () => {
    renderWithResult(ERROR_RESULT);
    expandCard();
    expect(screen.queryByTestId("sandbox-card-stdout")).toBeNull();
  });

  it("Save button is present in RESULT state (after expand)", () => {
    renderWithResult(OK_RESULT);
    expandCard();
    expect(screen.getByTestId("sandbox-card-save-button")).toBeTruthy();
  });

  it("shows duration when non-zero (after expand)", () => {
    renderWithResult(OK_RESULT);
    expandCard();
    const dur = screen.getByTestId("sandbox-card-duration");
    expect(dur.textContent).toContain("0.42s");
  });
});

// ---------------------------------------------------------------------------
// Truncated marker
// ---------------------------------------------------------------------------

describe("SandboxCard — truncated marker", () => {
  afterEach(() => cleanup());

  it("shows truncated marker when truncated=true (after expand)", () => {
    renderWithResult(TRUNCATED_RESULT);
    expandCard();
    expect(screen.getByTestId("sandbox-card-truncated")).toBeTruthy();
  });

  it("does NOT show truncated marker when truncated=false (after expand)", () => {
    renderWithResult(OK_RESULT);
    expandCard();
    expect(screen.queryByTestId("sandbox-card-truncated")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Malformed payload dropped gracefully (consumer-side)
//
// The ws.ts layer already drops malformed payloads with console.warn before
// they reach the component; we verify the component itself handles edge cases
// (e.g. missing optional fields) without crashing.
// ---------------------------------------------------------------------------

describe("SandboxCard — edge cases / graceful handling", () => {
  afterEach(() => cleanup());

  it("renders without crash when python_code is a minimal string", () => {
    const req: CodeExecRequestPayload = {
      envelope_type: "code-exec-request",
      code_exec_id: "01J0000000000000000000002B",
      python_code: "x=1",
      layer_refs: {},
      rationale: null,
    };
    render(<SandboxCard request={req} decided={null} onDecide={vi.fn()} />);
    expect(screen.getByTestId("sandbox-card-code").textContent).toBe("x=1");
  });

  it("renders without crash when result is null (after expand)", () => {
    renderWithResult({ ...ERROR_RESULT, result: null });
    expandCard();
    // result-descriptor section should be absent when result is null
    expect(screen.queryByTestId("sandbox-card-result-descriptor")).toBeNull();
  });

  it("renders too_large result descriptor (after expand)", () => {
    const res: CodeExecResultPayload = {
      ...OK_RESULT,
      result: { kind: "too_large", original_bytes: 5_000_000 },
    };
    renderWithResult(res);
    expandCard();
    expect(screen.getByTestId("sandbox-result-too-large")).toBeTruthy();
    expect(screen.getByTestId("sandbox-result-too-large").textContent).toContain("4883");
  });

  it("renders chart result with note when no PNG present (after expand)", () => {
    const res: CodeExecResultPayload = {
      ...OK_RESULT,
      result: { kind: "chart", chart_id: "chart-xyz", title: "My chart" },
    };
    renderWithResult(res);
    expandCard();
    expect(screen.getByTestId("sandbox-result-chart-note")).toBeTruthy();
  });

  it("renders chart PNG inline when png_base64 present and not truncated (after expand)", () => {
    // 1x1 transparent PNG (valid base64, content irrelevant to the test).
    const png =
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";
    const res: CodeExecResultPayload = {
      ...OK_RESULT,
      result: {
        kind: "chart",
        title: "Lightning glow",
        png_base64: png,
        png_truncated: false,
      },
    };
    renderWithResult(res);
    expandCard();
    const wrap = screen.getByTestId("sandbox-result-chart-image");
    expect(wrap).toBeTruthy();
    const img = wrap.querySelector("img");
    expect(img).toBeTruthy();
    expect(img!.getAttribute("src")).toBe(`data:image/png;base64,${png}`);
    // The note path is NOT taken when the PNG renders.
    expect(screen.queryByTestId("sandbox-result-chart-note")).toBeNull();
  });

  it("falls back to the note when png_truncated is true (PNG dropped) (after expand)", () => {
    const res: CodeExecResultPayload = {
      ...OK_RESULT,
      result: {
        kind: "chart",
        title: "Huge figure",
        png_base64: null,
        png_truncated: true,
      },
    };
    renderWithResult(res);
    expandCard();
    expect(screen.queryByTestId("sandbox-result-chart-image")).toBeNull();
    const note = screen.getByTestId("sandbox-result-chart-note");
    expect(note.textContent).toContain("too large");
  });
});

// ---------------------------------------------------------------------------
// FOLD + state-machine behavior (NATE 2026-06-26)
//
// A decided/resolved card collapses to a compact one-line summary; the full
// chrome lives behind a chevron expander. The card accent (left border / icon /
// summary color) is a function of status: indigo pending/running, green ok, red
// error/blocked, amber timeout. A running card carries the glow animation.
// ---------------------------------------------------------------------------

describe("SandboxCard — fold + compact summary", () => {
  afterEach(() => cleanup());

  it("folds to a compact summary by default after a successful result", () => {
    renderWithResult(OK_RESULT);
    const card = screen.getByTestId("sandbox-card");
    expect(card.dataset.state).toBe("resolved-ok");
    // Compact summary present; detail chrome hidden until expanded.
    const summary = screen.getByTestId("sandbox-card-summary");
    expect(summary.textContent).toContain("Python sandbox - ok");
    expect(screen.queryByTestId("sandbox-card-detail")).toBeNull();
    expect(screen.queryByTestId("sandbox-card-result-section")).toBeNull();
  });

  it("success summary carries the cheap scalar hint", () => {
    renderWithResult(OK_RESULT);
    const summary = screen.getByTestId("sandbox-card-summary");
    // OK_RESULT.result = { kind: "json", value: 2.0 } -> "2" hint.
    expect(summary.textContent).toContain("Python sandbox - ok (2)");
  });

  it("expander toggles the detail chrome open and closed", () => {
    renderWithResult(OK_RESULT);
    expect(screen.queryByTestId("sandbox-card-detail")).toBeNull();
    // Open
    fireEvent.click(screen.getByTestId("sandbox-card-expand"));
    expect(screen.getByTestId("sandbox-card-detail")).toBeTruthy();
    expect(screen.getByTestId("sandbox-card-result-section")).toBeTruthy();
    // Close
    fireEvent.click(screen.getByTestId("sandbox-card-expand"));
    expect(screen.queryByTestId("sandbox-card-detail")).toBeNull();
  });

  it("error result: red accent on the card + failed summary text", () => {
    renderWithResult(ERROR_RESULT);
    const card = screen.getByTestId("sandbox-card");
    expect(card.dataset.state).toBe("resolved-failed");
    expect(card.dataset.accent).toBe("#ef4444");
    const summary = screen.getByTestId("sandbox-card-summary");
    expect(summary.textContent).toContain("Python sandbox - error");
  });

  it("blocked result: red accent on the card", () => {
    renderWithResult(BLOCKED_RESULT);
    const card = screen.getByTestId("sandbox-card");
    expect(card.dataset.state).toBe("resolved-failed");
    expect(card.dataset.accent).toBe("#ef4444");
    expect(screen.getByTestId("sandbox-card-summary").textContent).toContain(
      "Python sandbox - blocked",
    );
  });

  it("timeout result: amber accent on the card", () => {
    renderWithResult(TIMEOUT_RESULT);
    const card = screen.getByTestId("sandbox-card");
    expect(card.dataset.accent).toBe("#eab308");
    expect(screen.getByTestId("sandbox-card-summary").textContent).toContain(
      "Python sandbox - timeout",
    );
  });

  it("success result: green accent on the card", () => {
    renderWithResult(OK_RESULT);
    const card = screen.getByTestId("sandbox-card");
    expect(card.dataset.state).toBe("resolved-ok");
    expect(card.dataset.accent).toBe("#10b981");
  });

  it("cancelled (no result): compact summary, no detail chrome", () => {
    render(
      <SandboxCard request={BASE_REQUEST} decided="cancel" onDecide={vi.fn()} />,
    );
    const card = screen.getByTestId("sandbox-card");
    expect(card.dataset.state).toBe("cancelled");
    const summary = screen.getByTestId("sandbox-card-summary");
    expect(summary.textContent).toContain("Execution cancelled");
    expect(screen.queryByTestId("sandbox-card-detail")).toBeNull();
  });

  it("pending (REQUEST) state is NOT folded — full chrome, no summary", () => {
    renderRequest();
    const card = screen.getByTestId("sandbox-card");
    expect(card.dataset.state).toBe("pending");
    expect(card.dataset.accent).toBe("#6366f1");
    expect(screen.queryByTestId("sandbox-card-summary")).toBeNull();
    // Full chrome rendered inline (the gate buttons live in it).
    expect(screen.getByTestId("sandbox-card-actions")).toBeTruthy();
  });

  it("running state carries the glow animation + indigo accent", () => {
    render(
      <SandboxCard request={BASE_REQUEST} decided="proceed" onDecide={vi.fn()} />,
    );
    const card = screen.getByTestId("sandbox-card");
    expect(card.dataset.state).toBe("running");
    expect(card.dataset.accent).toBe("#6366f1");
    // The glow animation is applied to the running card root (inline style).
    expect(card.style.animation).toContain("sandbox-glow");
    // Running is NOT folded — no compact summary, running indicator shown.
    expect(screen.queryByTestId("sandbox-card-summary")).toBeNull();
    expect(screen.getByTestId("sandbox-card-running")).toBeTruthy();
  });

  it("non-running cards do NOT carry the glow animation", () => {
    renderWithResult(OK_RESULT);
    const card = screen.getByTestId("sandbox-card");
    expect(card.style.animation).not.toContain("sandbox-glow");
  });
});
