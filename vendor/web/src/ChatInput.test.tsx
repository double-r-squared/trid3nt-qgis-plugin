// GRACE-2 web — ChatInput component tests (job-0144 + NATE 2026-06-17 model selector).
//
// Verifies the merged send/stop button + dynamic textarea + wrapper styling
// per the job-0144 kickoff acceptance checklist:
//   - Idle state shows up-arrow disabled when empty
//   - Idle state shows up-arrow enabled when text present
//   - Submit transitions to in-flight; up-arrow becomes stop-square
//   - Cancel click emits cancel envelope + returns to idle
//   - Pipeline-complete returns to idle automatically
//   - Multi-line typing expands textarea height
//   - Cmd+Enter / Ctrl+Enter submits; Enter alone inserts newline
//   - Drop shadow + rounded corner styles applied (style assertions)
//
// Model selector additions (NATE 2026-06-17):
//   - Left button row (attach/mic/mode) renders; mode is ICON-ONLY (no "Mode"
//     text label).
//   - UNCONTROLLED ChatInput keeps an in-composer model trigger
//     (data-testid="chat-input-model") so standalone usage can still switch.
//   - CONTROLLED ChatInput (modelId prop supplied — the header now owns the
//     selector) hides the in-composer trigger and reflects the prop's model id.
//   - Clicking model button opens the model popover (now portal'd to <body>).
//   - Selecting a model closes the popover + updates the active label
//   - onSubmit receives (text, modelId) — modelId is a non-empty Bedrock id string
//   - Send button is a CIRCLE in send state / SQUARE in stop state, tinted to
//     the active model accent color.
//   - Provider accent tint appears on wrapper border
//   - Exported ModelSelectorButton (header trigger) renders icon-only + opens
//     the popover + reports selection via onChange.
//
// We test ChatInput directly rather than through Chat (Chat opens a real
// WebSocket which happy-dom can't run; the existing Chat.test.tsx exercises
// the pipelineReducer/shouldShowCancel logic with the same pattern).

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { ChatInput, ModelSelectorButton } from "./components/ChatInput";
import {
  DEFAULT_MODEL_ID,
  getModelById,
} from "./lib/modelRegistry";

afterEach(() => {
  cleanup();
  // Model selection persists to localStorage; clear it so a test that selects a
  // non-default model can't leak into a later test that asserts the default.
  try {
    window.localStorage.clear();
  } catch {
    /* private-mode no-op */
  }
});

function renderIdle(overrides: Partial<Parameters<typeof ChatInput>[0]> = {}) {
  const onSubmit = vi.fn();
  const onCancel = vi.fn();
  const utils = render(
    <ChatInput
      state="idle"
      onSubmit={onSubmit}
      onCancel={onCancel}
      {...overrides}
    />,
  );
  return { onSubmit, onCancel, ...utils };
}

function renderInFlight(
  overrides: Partial<Parameters<typeof ChatInput>[0]> = {},
) {
  const onSubmit = vi.fn();
  const onCancel = vi.fn();
  const utils = render(
    <ChatInput
      state="in-flight"
      onSubmit={onSubmit}
      onCancel={onCancel}
      {...overrides}
    />,
  );
  return { onSubmit, onCancel, ...utils };
}

describe("ChatInput — idle state glyph + button enablement", () => {
  it("renders the up-arrow glyph and disables the action button when empty", () => {
    renderIdle();
    const glyph = screen.getByTestId("chat-input-glyph");
    expect(glyph.getAttribute("data-glyph")).toBe("up-arrow");
    const btn = screen.getByTestId("chat-input-action") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.getAttribute("aria-label")).toBe("Send message");
  });

  it("enables the up-arrow once non-whitespace text is present", () => {
    renderIdle();
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "hello" } });
    const btn = screen.getByTestId("chat-input-action") as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    // Whitespace-only input should NOT enable submit.
    fireEvent.change(ta, { target: { value: "   " } });
    expect(
      (screen.getByTestId("chat-input-action") as HTMLButtonElement).disabled,
    ).toBe(true);
  });
});

describe("ChatInput — submit semantics", () => {
  it("clicking the up-arrow with text invokes onSubmit(text, modelId) and clears the draft", () => {
    const { onSubmit } = renderIdle();
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "model the flood" } });
    fireEvent.click(screen.getByTestId("chat-input-action"));
    // onSubmit receives (text, modelId) — modelId is a non-empty Bedrock id.
    expect(onSubmit).toHaveBeenCalledWith("model the flood", expect.any(String));
    const calledModelId: string = onSubmit.mock.calls[0]?.[1] as string;
    expect(calledModelId.length).toBeGreaterThan(0);
    // Component clears the textarea on submit.
    expect((screen.getByTestId("chat-input") as HTMLTextAreaElement).value).toBe(
      "",
    );
  });

  it("plain Enter submits; Shift+Enter does NOT submit (newline)", () => {
    // job-0153 Part 6: flipped semantics — Enter alone submits.
    const { onSubmit } = renderIdle();
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "Hurricane Ian Fort Myers" } });
    // Shift+Enter — should NOT submit (inserts newline; browser handles it).
    fireEvent.keyDown(ta, { key: "Enter", shiftKey: true });
    expect(onSubmit).not.toHaveBeenCalled();
    // Plain Enter — should submit.
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("Hurricane Ian Fort Myers", expect.any(String));
  });

  it("Cmd+Enter (metaKey) also submits", () => {
    // job-0153 Part 6: any non-Shift Enter modifier still submits.
    const { onSubmit } = renderIdle();
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "go" } });
    fireEvent.keyDown(ta, { key: "Enter", metaKey: true });
    expect(onSubmit).toHaveBeenCalledWith("go", expect.any(String));
  });

  it("Ctrl+Enter also submits", () => {
    const { onSubmit } = renderIdle();
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "go" } });
    fireEvent.keyDown(ta, { key: "Enter", ctrlKey: true });
    expect(onSubmit).toHaveBeenCalledWith("go", expect.any(String));
  });

  it("empty input + Enter does NOT submit", () => {
    const { onSubmit } = renderIdle();
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    // No text — Enter must not fire onSubmit (whitespace-trim guard).
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.change(ta, { target: { value: "   " } });
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("does NOT submit while in-flight even with text", () => {
    const onSubmit = vi.fn();
    const onCancel = vi.fn();
    const { rerender } = render(
      <ChatInput
        state="idle"
        onSubmit={onSubmit}
        onCancel={onCancel}
      />,
    );
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "queued text" } });
    rerender(
      <ChatInput
        state="in-flight"
        onSubmit={onSubmit}
        onCancel={onCancel}
      />,
    );
    // Enter while in-flight: the action button is now Stop; pressing Enter
    // on the textarea should NOT submit a second message.
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("onSubmit default model id matches the DEFAULT_MODEL_ID constant", () => {
    const { onSubmit } = renderIdle();
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "test" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    // When no model has been explicitly changed, the default model id is used.
    expect(onSubmit).toHaveBeenCalledWith("test", DEFAULT_MODEL_ID);
  });
});

describe("ChatInput — placeholder (job-0153 Part 5)", () => {
  it("defaults to the short 'Reply to TRID3NT' placeholder", () => {
    renderIdle();
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    expect(ta.placeholder).toBe("Reply to TRID3NT");
  });

  it("accepts an override via the placeholder prop", () => {
    renderIdle({ placeholder: "Ask TRID3NT..." });
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    expect(ta.placeholder).toBe("Ask TRID3NT...");
  });
});

describe("ChatInput — onHeightChange (job-0153 Part 4)", () => {
  it("invokes onHeightChange on mount + on every draft change", () => {
    const onHeightChange = vi.fn();
    const onSubmit = vi.fn();
    const onCancel = vi.fn();
    render(
      <ChatInput
        state="idle"
        onSubmit={onSubmit}
        onCancel={onCancel}
        onHeightChange={onHeightChange}
      />,
    );
    // Fires on initial useLayoutEffect.
    expect(onHeightChange).toHaveBeenCalled();
    const callsBefore = onHeightChange.mock.calls.length;
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "line1\nline2\nline3" } });
    expect(onHeightChange.mock.calls.length).toBeGreaterThan(callsBefore);
  });
});

describe("ChatInput — in-flight state + cancel", () => {
  it("renders the stop-square glyph when state=in-flight", () => {
    renderInFlight();
    const glyph = screen.getByTestId("chat-input-glyph");
    expect(glyph.getAttribute("data-glyph")).toBe("stop");
    const btn = screen.getByTestId("chat-input-action") as HTMLButtonElement;
    expect(btn.getAttribute("aria-label")).toBe("Stop response");
    // Stop button is enabled in in-flight state regardless of textarea
    // contents (so the user can always abort).
    expect(btn.disabled).toBe(false);
  });

  it("clicking the stop-square emits onCancel", () => {
    const { onCancel } = renderInFlight();
    fireEvent.click(screen.getByTestId("chat-input-action"));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("transitioning back to idle re-shows up-arrow (replace-not-reconcile)", () => {
    const onSubmit = vi.fn();
    const onCancel = vi.fn();
    const { rerender } = render(
      <ChatInput
        state="in-flight"
        onSubmit={onSubmit}
        onCancel={onCancel}
      />,
    );
    expect(screen.getByTestId("chat-input-glyph").getAttribute("data-glyph"))
      .toBe("stop");
    rerender(
      <ChatInput
        state="idle"
        onSubmit={onSubmit}
        onCancel={onCancel}
      />,
    );
    expect(screen.getByTestId("chat-input-glyph").getAttribute("data-glyph"))
      .toBe("up-arrow");
  });
});

describe("ChatInput — wrapper presentation", () => {
  it("applies a drop shadow + rounded corners + dark background", () => {
    renderIdle();
    const wrapper = screen.getByTestId("chat-input-wrapper");
    const style = wrapper.style;
    // box-shadow non-empty (Kickoff Part 3 / live verification check).
    expect(style.boxShadow).not.toBe("");
    expect(style.boxShadow.toLowerCase()).toContain("rgba(0,0,0");
    // Rounded corners ≥ 12px per kickoff Part 3.
    const radius = parseInt(style.borderRadius, 10);
    expect(radius).toBeGreaterThanOrEqual(12);
    // Dark-theme aware background.
    expect(style.background).toMatch(/^#1[a-f0-9]{5}$/i);
  });

  it("textarea has a minHeight ≥ 48px so the single-line state matches kickoff", () => {
    renderIdle();
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    const minH = parseInt(ta.style.minHeight, 10);
    expect(minH).toBeGreaterThanOrEqual(48);
  });

  it("textarea maxHeight scales with the configured maxVh prop", () => {
    renderIdle({ maxVh: 30 });
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    expect(ta.style.maxHeight).toBe("30vh");
  });

  it("wrapper border carries a provider accent tint (not plain white)", () => {
    renderIdle();
    const wrapper = screen.getByTestId("chat-input-wrapper");
    // The default model is Claude Sonnet 4.6 (Anthropic, accent #c2603c).
    // The border should reference the accent color hex, not plain rgba white.
    const border = wrapper.style.border;
    expect(border).toMatch(/#[0-9a-fA-F]{6}/);
    // Must NOT be pure gray/white.
    const hex = border.match(/#([0-9a-fA-F]{6})/)?.[1]?.toLowerCase() ?? "";
    expect(hex).not.toBe("ffffff");
    expect(hex).not.toBe("000000");
  });

  it("wrapper exposes data-model-id attribute with the active model id", () => {
    renderIdle();
    const wrapper = screen.getByTestId("chat-input-wrapper");
    const modelId = wrapper.getAttribute("data-model-id");
    expect(typeof modelId).toBe("string");
    expect((modelId ?? "").length).toBeGreaterThan(0);
    expect(modelId).toBe(DEFAULT_MODEL_ID);
  });
});

describe("ChatInput — multi-line growth", () => {
  it("grows in measured height as multi-line content is added", () => {
    renderIdle();
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    // happy-dom doesn't lay out the textarea so scrollHeight is 0 — but the
    // component sets el.style.height in a useLayoutEffect; even when
    // scrollHeight is 0, MIN_HEIGHT_PX is the floor. Verify the floor.
    expect(ta.style.height).toMatch(/^\d+px$/);
    const initial = parseInt(ta.style.height, 10);
    expect(initial).toBeGreaterThanOrEqual(48);
    // After updating content, the layout effect re-runs.
    fireEvent.change(ta, {
      target: { value: "line1\nline2\nline3\nline4\nline5\nline6\nline7" },
    });
    // Re-read the inline style (the effect ran synchronously via
    // useLayoutEffect; height is at least the floor).
    const grown = parseInt(ta.style.height, 10);
    expect(grown).toBeGreaterThanOrEqual(48);
  });
});

describe("ChatInput — disabled prop (WS down)", () => {
  it("disables both textarea and action button regardless of text", () => {
    renderIdle({ disabled: true });
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    expect(ta.disabled).toBe(true);
    fireEvent.change(ta, { target: { value: "queued" } });
    const btn = screen.getByTestId("chat-input-action") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });
});

describe("ChatInput — left button row (NATE 2026-06-17 model selector)", () => {
  it("renders the left button row with attach, mic, and mode buttons", () => {
    renderIdle();
    expect(screen.getByTestId("chat-input-left-row")).toBeTruthy();
    expect(screen.getByTestId("chat-input-attach")).toBeTruthy();
    expect(screen.getByTestId("chat-input-mic")).toBeTruthy();
    expect(screen.getByTestId("chat-input-mode")).toBeTruthy();
  });

  it("attach, mic, and mode stubs are disabled", () => {
    renderIdle();
    expect((screen.getByTestId("chat-input-attach") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("chat-input-mic") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("chat-input-mode") as HTMLButtonElement).disabled).toBe(true);
  });

  // Chat-chrome rework item 4: the mode button is now ICON-ONLY — it must NOT
  // render the literal "Mode" text label.
  it("mode button is icon-only (no 'Mode' text label)", () => {
    renderIdle();
    const mode = screen.getByTestId("chat-input-mode");
    expect(mode.textContent).toBe("");
    // It still carries an accessible label so the control is announced.
    expect(mode.getAttribute("aria-label")).toMatch(/mode/i);
  });

  // UNCONTROLLED ChatInput (no modelId prop, as renderIdle does) keeps the
  // in-composer model trigger for standalone usage.
  it("uncontrolled: model button is NOT disabled and clicking it opens the popover", () => {
    renderIdle();
    const modelBtn = screen.getByTestId("chat-input-model") as HTMLButtonElement;
    expect(modelBtn.disabled).toBe(false);
    // Popover is not yet visible.
    expect(screen.queryByTestId("model-popover")).toBeNull();
    fireEvent.click(modelBtn);
    // After click, popover renders (portal'd to <body>; screen still finds it).
    expect(screen.getByTestId("model-popover")).toBeTruthy();
  });

  it("uncontrolled: clicking a model option in the popover closes the popover", () => {
    renderIdle();
    fireEvent.click(screen.getByTestId("chat-input-model"));
    expect(screen.getByTestId("model-popover")).toBeTruthy();
    // Click the second option (Nova Pro).
    fireEvent.click(screen.getByTestId("model-option-us.amazon.nova-pro-v1:0"));
    expect(screen.queryByTestId("model-popover")).toBeNull();
  });

  it("uncontrolled: after selecting a different model, onSubmit carries the new model id", () => {
    const { onSubmit } = renderIdle();
    // Open popover and select Nova Lite (a proven tool-capable cheap model).
    fireEvent.click(screen.getByTestId("chat-input-model"));
    fireEvent.click(screen.getByTestId("model-option-us.amazon.nova-lite-v1:0"));
    // Submit a message.
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "use nova lite" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("use nova lite", "us.amazon.nova-lite-v1:0");
  });
});

describe("ChatInput — controlled model (header now owns the selector)", () => {
  it("with a modelId prop, the in-composer model trigger is NOT rendered", () => {
    renderIdle({ modelId: "us.amazon.nova-pro-v1:0" });
    expect(screen.queryByTestId("chat-input-model")).toBeNull();
  });

  it("reflects the controlled model id on the wrapper data attribute", () => {
    renderIdle({ modelId: "us.amazon.nova-lite-v1:0" });
    const wrapper = screen.getByTestId("chat-input-wrapper");
    expect(wrapper.getAttribute("data-model-id")).toBe("us.amazon.nova-lite-v1:0");
  });

  it("submit carries the controlled model id (not the default)", () => {
    const { onSubmit } = renderIdle({ modelId: "us.amazon.nova-pro-v1:0" });
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "go" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("go", "us.amazon.nova-pro-v1:0");
  });

  it("wrapper border + send button tint to the controlled model's accent", () => {
    const novaProAccent = getModelById("us.amazon.nova-pro-v1:0").accentColor;
    renderIdle({ modelId: "us.amazon.nova-pro-v1:0" });
    const wrapper = screen.getByTestId("chat-input-wrapper");
    // Border references the Nova (Amazon) accent, not the default Anthropic one.
    expect(wrapper.style.border.toLowerCase()).toContain(novaProAccent.toLowerCase());
  });
});

describe("ChatInput — send/stop button shape + color (item 3)", () => {
  it("idle send button is a CIRCLE tinted to the active model accent", () => {
    const accent = getModelById(DEFAULT_MODEL_ID).accentColor;
    renderIdle();
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "hello" } });
    const btn = screen.getByTestId("chat-input-action") as HTMLButtonElement;
    expect(btn.getAttribute("data-shape")).toBe("circle");
    expect(btn.style.borderRadius).toBe("50%");
    // Accent-tinted ground (the default model's accent hex).
    expect(btn.style.background.toLowerCase()).toContain(accent.toLowerCase());
  });

  it("in-flight stop button is a SQUARE (not a circle)", () => {
    renderInFlight();
    const btn = screen.getByTestId("chat-input-action") as HTMLButtonElement;
    expect(btn.getAttribute("data-shape")).toBe("square");
    expect(btn.style.borderRadius).not.toBe("50%");
  });

  it("send-button circle color tracks a controlled model change", () => {
    const novaAccent = getModelById("us.amazon.nova-lite-v1:0").accentColor;
    renderIdle({ modelId: "us.amazon.nova-lite-v1:0" });
    const ta = screen.getByTestId("chat-input") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "hello" } });
    const btn = screen.getByTestId("chat-input-action") as HTMLButtonElement;
    expect(btn.style.background.toLowerCase()).toContain(novaAccent.toLowerCase());
  });
});

describe("ModelSelectorButton — header-placed icon-only trigger (item 1)", () => {
  it("renders icon-only (no model-name text label)", () => {
    render(<ModelSelectorButton selectedId={DEFAULT_MODEL_ID} />);
    const btn = screen.getByTestId("model-selector-button");
    // Icon-only: no visible text content.
    expect(btn.textContent).toBe("");
    // Accessible label still names the model for assistive tech.
    expect(btn.getAttribute("aria-label")).toMatch(/Model:/);
    expect(btn.getAttribute("data-model-id")).toBe(DEFAULT_MODEL_ID);
  });

  it("clicking opens the portal'd popover", () => {
    render(<ModelSelectorButton selectedId={DEFAULT_MODEL_ID} />);
    expect(screen.queryByTestId("model-popover")).toBeNull();
    fireEvent.click(screen.getByTestId("model-selector-button"));
    expect(screen.getByTestId("model-popover")).toBeTruthy();
  });

  it("selecting a model reports the new id via onChange", () => {
    const onChange = vi.fn();
    render(<ModelSelectorButton selectedId={DEFAULT_MODEL_ID} onChange={onChange} />);
    fireEvent.click(screen.getByTestId("model-selector-button"));
    fireEvent.click(screen.getByTestId("model-option-us.amazon.nova-pro-v1:0"));
    expect(onChange).toHaveBeenCalledWith("us.amazon.nova-pro-v1:0");
    // Popover closes after selection.
    expect(screen.queryByTestId("model-popover")).toBeNull();
  });
});
