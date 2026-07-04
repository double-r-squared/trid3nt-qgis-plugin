// GRACE-2 web — Claude-Code-style merged send/stop chat input (job-0144).
//
// Replaces the prior split textarea + Send + Cancel buttons with a single
// rounded input wrapper containing:
//   1. A dynamic textarea that auto-expands as the user types
//      (min ~1 line / ~48px, max ~40vh — then scrolls internally).
//   2. A LEFT button row of auxiliary controls (below the textarea):
//        - Paperclip (attach) — disabled stub; title "coming soon"
//        - Microphone (voice) — disabled stub; title "coming soon"
//        - Mode toggle       — disabled stub; ICON ONLY (Faders glyph), no
//                              literal "Mode" text label (NATE 2026-06-17)
//   3. A single action button anchored bottom-right of the controls row:
//        - idle  : up-arrow (↑) on a CIRCLE ground tinted to the active
//                  model's accent color, disabled when empty
//        - busy  : stop-square (■) on a SQUARE ground, click emits cancel
//        - returns to idle when the pipeline completes/cancels
//
// Chat-chrome rework (NATE 2026-06-17):
//   - The MODEL SELECTOR no longer lives in the composer bottom row. It moved
//     UP to the header status area (where the connection signal used to be) as
//     an ICON-ONLY trigger. That trigger is the exported `ModelSelectorButton`
//     below; the header (Chat.tsx) renders it and threads the chosen model id
//     back into ChatInput via the controlled `modelId` / `onModelChange`
//     props. When those props are omitted ChatInput stays UNCONTROLLED — it
//     owns its own selection + localStorage persistence exactly as before, so
//     existing standalone usages / tests keep working.
//   - The model popover now portals to <body> so it floats OVER the chat
//     window instead of being clipped by the chat panel's overflow.
//   - The selected model id is persisted to localStorage via modelRegistry.ts.
//   - The chat wrapper border is tinted to the active provider's accent color.
//   - The send button itself is tinted to the active provider's accent color.
//   - `onSubmit` receives the selected model id alongside the text so the
//     caller (Chat.tsx) can include `model_id` on the user-message envelope.
//
// Submission semantics (FR-WC-7, updated job-0153):
//   - Enter alone        → submit (clear text, send).
//   - Shift+Enter        → insert newline (multi-line input).
//   - Cmd+Enter/Ctrl+Enter → also submit (preserved as alternate hotkey for
//                            users coming from the prior job-0144 behavior).
// This flip matches Claude Code + user expectations; it resolves
// OQ-0144-CMD-ENTER-VS-PLAIN-ENTER-DEFAULT.
//
// Cancel semantics (FR-WC-9 / Invariant 8):
//   - Pressing the in-flight stop-square dispatches `cancel` via the
//     `onCancel` prop (which Chat.tsx wires to GraceWs.sendCancel).
//   - Cancellation leaves loaded layers in place — the input only emits
//     intent, never mutates map state.
//
// Wrapper presentation (kickoff Part 3 + Part 4):
//   - Subtle drop shadow + rounded corners + dark-theme aware background.
//   - The wrapper is positioned by its parent (Chat.tsx) as an overlay at
//     the bottom of the chat panel; the scrollable conversation area
//     applies the matching bottom-padding so messages aren't hidden
//     behind the input.
//
// Invariant 1 (determinism boundary): this component renders + emits intent
// only. It computes no user-facing number; the textarea text it submits is
// passed verbatim to `onSubmit`, which the parent feeds into
// GraceWs.sendUserMessage.

import {
  CSSProperties,
  KeyboardEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import {
  IconPaperclip,
  IconMic,
  IconMode,
  IconModel,
} from "./icons";
import {
  SELECTABLE_MODELS,
  getModelById,
  loadPersistedModelId,
  persistModelId,
  type ModelEntry,
} from "../lib/modelRegistry";

export type ChatInputState = "idle" | "in-flight";

export interface ChatInputProps {
  /** Current pipeline / input state. Drives idle vs in-flight rendering. */
  state: ChatInputState;
  /**
   * Called when the user submits text in idle state (Cmd/Ctrl+Enter or
   * up-arrow click). The component clears its own draft on submit.
   * `modelId` carries the currently-selected Bedrock model id so the caller
   * can include it on the `user-message` envelope.
   */
  onSubmit: (text: string, modelId: string) => void;
  /**
   * Called when the user clicks the stop-square in in-flight state.
   * Wired to GraceWs.sendCancel by Chat.tsx.
   */
  onCancel: () => void;
  /**
   * Optional: hard-disable the input wrapper (e.g. WS disconnected). When
   * true, both the textarea and the action button are disabled regardless
   * of state. The button still shows the idle/in-flight icon so users see
   * the current pipeline phase.
   */
  disabled?: boolean;
  /** Optional placeholder; defaults to the multi-line hint. */
  placeholder?: string;
  /** Maximum height the textarea grows to before it scrolls internally (vh). */
  maxVh?: number;
  /**
   * Called whenever the wrapper's measured pixel height changes (job-0153
   * Part 4). The parent uses this to grow the chat scroll's bottom-padding
   * so the floating input never clips messages.
   */
  onHeightChange?: (heightPx: number) => void;
  /**
   * job-0278 — textarea font size in px (default 14, the historical desktop
   * value). The mobile bottom sheet passes 16: iOS Safari auto-zooms the
   * page when focusing an input whose font-size is < 16px, which would
   * wreck the fixed-shell layout on phones. Desktop callers omit it.
   */
  fontSizePx?: number;
  /**
   * Chat-chrome rework (NATE 2026-06-17) — the active model id. OPTIONAL
   * controlled prop: when supplied (the model selector now lives in the
   * header), ChatInput uses it for the send-button tint, the wrapper accent,
   * and the `model_id` carried on submit, and DOES NOT render its own model
   * trigger. When omitted, ChatInput stays uncontrolled and owns its own
   * selection + persistence (legacy / standalone behavior).
   */
  modelId?: string;
  /**
   * Fired when the (uncontrolled) in-composer model trigger changes the
   * model — only relevant in uncontrolled mode. Controlled callers drive
   * selection from the header instead.
   */
  onModelChange?: (modelId: string) => void;
}

const MIN_HEIGHT_PX = 48;
const DEFAULT_MAX_VH = 40;

/** Action glyph stack — up-arrow (↑) idle or stop-square (■) in-flight. */
function ActionGlyph({ state }: { state: ChatInputState }): JSX.Element {
  if (state === "in-flight") {
    // Stop-square — small solid square centered in the button.
    return (
      <span
        data-testid="chat-input-glyph"
        data-glyph="stop"
        aria-hidden="true"
        style={{
          display: "inline-block",
          width: 12,
          height: 12,
          background: "#fff",
          borderRadius: 2,
        }}
      />
    );
  }
  // Up-arrow — SVG so it renders crisply across platforms and matches the
  // Claude Code look (a centered chevron-up).
  return (
    <svg
      data-testid="chat-input-glyph"
      data-glyph="up-arrow"
      aria-hidden="true"
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      style={{ display: "block" }}
    >
      <path
        d="M8 3.5L8 12.5"
        stroke="#fff"
        strokeWidth="1.75"
        strokeLinecap="round"
      />
      <path
        d="M3.75 7.75L8 3.5L12.25 7.75"
        stroke="#fff"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Model selector popover
// ---------------------------------------------------------------------------
//
// Chat-chrome rework (NATE 2026-06-17): the popover now renders through a
// portal to <body> and positions itself with fixed coordinates derived from
// the anchor button's bounding rect. This lifts it OUT of the chat panel's
// stacking/overflow context so it floats over the chat window (item 5 of the
// rework) regardless of where the trigger lives (composer or header).

interface ModelPopoverProps {
  anchorRef: React.RefObject<HTMLButtonElement>;
  selectedId: string;
  onSelect: (model: ModelEntry) => void;
  onClose: () => void;
}

function ModelPopover({
  anchorRef,
  selectedId,
  onSelect,
  onClose,
}: ModelPopoverProps): JSX.Element | null {
  const popoverRef = useRef<HTMLDivElement | null>(null);
  // Fixed-position coordinates computed from the anchor's bounding rect so the
  // portal'd popover floats directly over the chat window beside its trigger.
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(
    null,
  );

  const reposition = useCallback(() => {
    const anchor = anchorRef.current;
    if (!anchor) return;
    const r = anchor.getBoundingClientRect();
    const MENU_WIDTH = 240;
    const GAP = 8;
    // Prefer dropping BELOW the trigger (header placement); if that would run
    // off the bottom of the viewport, flip ABOVE it (composer placement).
    const estHeight = 220;
    const below = r.bottom + GAP;
    const dropAbove = below + estHeight > window.innerHeight;
    const top = dropAbove ? Math.max(8, r.top - GAP - estHeight) : below;
    // Right-align the menu to the trigger but keep it on-screen.
    let left = r.right - MENU_WIDTH;
    if (left < 8) left = 8;
    if (left + MENU_WIDTH > window.innerWidth - 8) {
      left = window.innerWidth - 8 - MENU_WIDTH;
    }
    setCoords({ top, left });
  }, [anchorRef]);

  useLayoutEffect(() => {
    reposition();
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [reposition]);

  // Close on click-outside.
  useEffect(() => {
    function handlePointerDown(e: PointerEvent): void {
      const target = e.target as Node | null;
      if (!target) return;
      if (popoverRef.current?.contains(target)) return;
      if (anchorRef.current?.contains(target)) return;
      onClose();
    }
    document.addEventListener("pointerdown", handlePointerDown, { capture: true });
    return () => document.removeEventListener("pointerdown", handlePointerDown, { capture: true });
  }, [anchorRef, onClose]);

  // Close on Escape.
  useEffect(() => {
    function handleKey(e: globalThis.KeyboardEvent): void {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [onClose]);

  // Group models by provider for display.
  const grouped = SELECTABLE_MODELS.reduce<Record<string, ModelEntry[]>>((acc, m) => {
    const list = acc[m.provider] ?? [];
    list.push(m);
    acc[m.provider] = list;
    return acc;
  }, {});

  // High z-index + fixed positioning so the menu overlays the chat window
  // (the whole point of the portal). 9600 clears the chat panel chrome and
  // the inline-card stack (z=50) but stays under full-screen modals (≥9500
  // overlays use their own portals; this is intentionally just above them for
  // a transient menu that closes on any outside interaction).
  const popoverStyle: CSSProperties = {
    position: "fixed",
    top: coords?.top ?? -9999,
    left: coords?.left ?? -9999,
    visibility: coords ? "visible" : "hidden",
    zIndex: 9600,
    background: "#1e1e26",
    border: "1px solid rgba(255,255,255,0.1)",
    borderRadius: 10,
    padding: "6px 0",
    width: 240,
    boxShadow: "0 8px 24px rgba(0,0,0,0.5)",
  };

  return createPortal(
    <div
      ref={popoverRef}
      data-testid="model-popover"
      role="listbox"
      aria-label="Select model"
      style={popoverStyle}
    >
      {Object.entries(grouped).map(([provider, models]) => (
        <div key={provider}>
          <div
            style={{
              padding: "4px 14px 2px",
              fontSize: 10,
              fontWeight: 600,
              letterSpacing: "0.08em",
              textTransform: "uppercase",
              color: "rgba(255,255,255,0.35)",
              userSelect: "none",
            }}
          >
            {provider}
          </div>
          {models.map((m) => {
            const isSelected = m.id === selectedId;
            return (
              <button
                key={m.id}
                role="option"
                aria-selected={isSelected}
                data-testid={`model-option-${m.id}`}
                onClick={() => { onSelect(m); onClose(); }}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 8,
                  width: "100%",
                  padding: "6px 14px",
                  background: isSelected ? "rgba(255,255,255,0.05)" : "transparent",
                  border: "none",
                  cursor: "pointer",
                  color: isSelected ? "#fff" : "rgba(255,255,255,0.7)",
                  fontSize: 13,
                  textAlign: "left",
                  transition: "background 100ms ease",
                }}
                onMouseEnter={(e) => {
                  (e.currentTarget as HTMLButtonElement).style.background = "rgba(255,255,255,0.07)";
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLButtonElement).style.background =
                    isSelected ? "rgba(255,255,255,0.05)" : "transparent";
                }}
              >
                <span>{m.label}</span>
                {isSelected && (
                  <span
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: "50%",
                      background: m.accentColor,
                      flexShrink: 0,
                    }}
                  />
                )}
              </button>
            );
          })}
        </div>
      ))}
    </div>,
    document.body,
  );
}

// ---------------------------------------------------------------------------
// ModelSelectorButton — icon-only model trigger for the header status area.
// ---------------------------------------------------------------------------
//
// Chat-chrome rework (NATE 2026-06-17, item 1): the model button moved OUT of
// the composer and into the header (where the connection signal used to be),
// rendered ICON-ONLY (Brain glyph, NO model-name text). The header (Chat.tsx)
// renders this; it owns the selection + localStorage persistence and reports
// changes through `onChange` so the composer can mirror the active model id.
//
// `selectedId` is OPTIONAL controlled: when omitted the button seeds from
// localStorage and self-manages, exactly like the old in-composer trigger.

export interface ModelSelectorButtonProps {
  /** Controlled active model id. Omit to self-manage from localStorage. */
  selectedId?: string;
  /** Fired with the new model id whenever the user picks a different model. */
  onChange?: (modelId: string) => void;
  /** Optional pixel size for the Brain icon (default 16). */
  iconSize?: number;
}

export function ModelSelectorButton({
  selectedId,
  onChange,
  iconSize = 16,
}: ModelSelectorButtonProps): JSX.Element {
  const [internalId, setInternalId] = useState<string>(
    () => getModelById(loadPersistedModelId()).id,
  );
  const activeId = selectedId ?? internalId;
  const activeModel = getModelById(activeId);
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement | null>(null);

  function handleSelect(model: ModelEntry): void {
    if (selectedId === undefined) setInternalId(model.id);
    persistModelId(model.id);
    onChange?.(model.id);
  }

  return (
    <>
      <button
        ref={buttonRef}
        onClick={() => setOpen((o) => !o)}
        title={`Model: ${activeModel.label}`}
        aria-label={`Model: ${activeModel.label}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        data-testid="model-selector-button"
        data-model-id={activeModel.id}
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          width: 28,
          height: 28,
          borderRadius: 6,
          border: "none",
          padding: 0,
          background: open ? "rgba(255,255,255,0.08)" : "transparent",
          // Icon tints to the active provider accent so "which model" reads at
          // a glance even without a text label.
          color: activeModel.accentColor,
          cursor: "pointer",
          transition: "background 120ms ease, color 120ms ease",
          flexShrink: 0,
        }}
      >
        <IconModel size={iconSize} />
      </button>
      {open && (
        <ModelPopover
          anchorRef={buttonRef as React.RefObject<HTMLButtonElement>}
          selectedId={activeModel.id}
          onSelect={handleSelect}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Small icon button used for left-row stubs.
// ---------------------------------------------------------------------------
interface IconButtonProps {
  onClick?: () => void;
  disabled?: boolean;
  title: string;
  "data-testid"?: string;
  children: React.ReactNode;
  active?: boolean;
  accentColor?: string;
}

function LeftIconButton({
  onClick,
  disabled,
  title,
  "data-testid": testId,
  children,
  active = false,
  accentColor,
}: IconButtonProps): JSX.Element {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      data-testid={testId}
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        width: 28,
        height: 28,
        borderRadius: 6,
        border: "none",
        padding: 0,
        background: active ? "rgba(255,255,255,0.08)" : "transparent",
        color: active && accentColor
          ? accentColor
          : disabled
          ? "rgba(255,255,255,0.2)"
          : "rgba(255,255,255,0.5)",
        cursor: disabled ? "default" : "pointer",
        transition: "background 120ms ease, color 120ms ease",
        flexShrink: 0,
      }}
    >
      {children}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function ChatInput({
  state,
  onSubmit,
  onCancel,
  disabled = false,
  placeholder = "Reply to TRID3NT",
  maxVh = DEFAULT_MAX_VH,
  onHeightChange,
  fontSizePx = 14,
  modelId,
  onModelChange,
}: ChatInputProps): JSX.Element {
  const [draft, setDraft] = useState("");

  // Model selection. Controlled when `modelId` is supplied (the selector lives
  // in the header now); otherwise ChatInput self-manages from localStorage and
  // renders its own in-composer trigger (legacy / standalone usage).
  const controlled = modelId !== undefined;
  const [internalModelId, setInternalModelId] = useState<string>(() =>
    getModelById(loadPersistedModelId()).id,
  );
  const selectedModel = getModelById(controlled ? modelId : internalModelId);
  const [popoverOpen, setPopoverOpen] = useState(false);
  const modelButtonRef = useRef<HTMLButtonElement | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);

  // Auto-grow: measure scrollHeight against the configured maxHeight every
  // time the draft changes. Falls back to MIN_HEIGHT when empty.
  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    // Reset height so we measure the natural scrollHeight (not the previous
    // expanded height).
    el.style.height = "auto";
    const maxPx = Math.round(window.innerHeight * (maxVh / 100));
    const target = Math.min(Math.max(el.scrollHeight, MIN_HEIGHT_PX), maxPx);
    el.style.height = `${target}px`;
    el.style.overflowY = el.scrollHeight > maxPx ? "auto" : "hidden";
    // job-0153 Part 4: report total wrapper height so the parent can grow
    // its scroll-area bottom-padding when the textarea expands. We measure
    // the wrapper (not the textarea) so wrapper padding + border are
    // included in the reported pixel value.
    if (onHeightChange && wrapperRef.current) {
      const h = wrapperRef.current.getBoundingClientRect().height;
      onHeightChange(h);
    }
  }, [draft, maxVh, onHeightChange]);

  // When transitioning back from in-flight to idle, focus the textarea so
  // the user can immediately type their next message — matches the Claude
  // Code interaction model.
  useEffect(() => {
    if (state === "idle") {
      // Microtask delay so React commits the new disabled state first.
      const t = window.setTimeout(() => textareaRef.current?.focus(), 0);
      return () => window.clearTimeout(t);
    }
    return undefined;
  }, [state]);

  function handleModelSelect(model: ModelEntry): void {
    if (!controlled) setInternalModelId(model.id);
    persistModelId(model.id);
    onModelChange?.(model.id);
  }

  function handleSubmit(): void {
    const text = draft.trim();
    if (!text) return;
    if (state !== "idle" || disabled) return;
    onSubmit(text, selectedModel.id);
    setDraft("");
    // Reset textarea height immediately on clear so the wrapper doesn't
    // briefly retain the prior expanded height.
    const el = textareaRef.current;
    if (el) {
      el.style.height = `${MIN_HEIGHT_PX}px`;
    }
  }

  function handleCancel(): void {
    if (state !== "in-flight" || disabled) return;
    onCancel();
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>): void {
    // job-0153 Part 6 — Enter alone submits; Shift+Enter inserts a newline.
    // Cmd+Enter / Ctrl+Enter also submit (kept as alternate hotkey for users
    // who learned the prior job-0144 behavior).
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  }

  function onButtonClick(): void {
    if (state === "in-flight") {
      handleCancel();
    } else {
      handleSubmit();
    }
  }

  const hasText = draft.trim().length > 0;
  const accentColor = selectedModel.accentColor;

  // Button state derivation:
  //   - idle + empty             → disabled, accent (faded)
  //   - idle + has text          → enabled, accent
  //   - in-flight                → enabled, grey
  //   - disabled (prop)          → forced-disabled (e.g. WS down)
  const buttonDisabled =
    disabled ||
    (state === "idle" && !hasText);
  // Chat-chrome rework (NATE 2026-06-17, item 3):
  //   - SEND  state → CIRCLE (borderRadius 50%), tinted to the model accent.
  //   - STOP  state → SQUARE (borderRadius 8), neutral grey ground.
  const isStop = state === "in-flight";
  const buttonStyle: CSSProperties = isStop
    ? {
        borderRadius: 8,
        background: buttonDisabled ? "#4a4a52" : "#6b6b76",
        cursor: buttonDisabled ? "default" : "pointer",
        opacity: buttonDisabled ? 0.6 : 1,
      }
    : {
        borderRadius: "50%",
        background: accentColor,
        cursor: buttonDisabled ? "default" : "pointer",
        opacity: buttonDisabled ? 0.45 : 1,
      };

  // The wrapper border is tinted to the active model's provider accent color.
  // Use a low-opacity tint at rest and a higher-opacity tint on focus (via
  // CSS variable approach with inline style). The provider tint also provides
  // ambient "which model is active" signal at a glance.
  const wrapperStyle: CSSProperties = {
    display: "flex",
    flexDirection: "column",
    gap: 0,
    background: "#1a1a20",
    border: `1.5px solid ${accentColor}55`,
    borderRadius: 14,
    boxShadow: "0 2px 12px rgba(0,0,0,0.35)",
    padding: "10px 10px 8px 14px",
    transition: "box-shadow 160ms ease, border-color 160ms ease",
    position: "relative",
  };

  return (
    <div
      ref={wrapperRef}
      data-testid="chat-input-wrapper"
      data-state={state}
      data-model-id={selectedModel.id}
      style={wrapperStyle}
    >
      {/* Textarea (full width) — all controls live on the bottom row below */}
      <textarea
        ref={textareaRef}
        data-testid="chat-input"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
        disabled={disabled}
        rows={1}
        style={{
          width: "100%",
          boxSizing: "border-box",
          minHeight: MIN_HEIGHT_PX,
          maxHeight: `${maxVh}vh`,
          resize: "none",
          background: "transparent",
          color: "#eee",
          border: "none",
          outline: "none",
          fontFamily: "inherit",
          fontSize: fontSizePx,
          lineHeight: 1.4,
          padding: "6px 2px",
        }}
      />

      {/* Left button row: attach + mic + mode stubs (model selector moved to
          the header per the chat-chrome rework). */}
      <div
        data-testid="chat-input-left-row"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 2,
          marginTop: 4,
        }}
      >
        {/* Attach — disabled stub */}
        <LeftIconButton
          disabled
          title="Attach file (coming soon)"
          data-testid="chat-input-attach"
        >
          <IconPaperclip size={15} />
        </LeftIconButton>

        {/* Voice — disabled stub */}
        <LeftIconButton
          disabled
          title="Voice input (coming soon)"
          data-testid="chat-input-mic"
        >
          <IconMic size={15} />
        </LeftIconButton>

        {/* Mode toggle — disabled stub. ICON ONLY now (Faders glyph), no
            literal "Mode" text label (NATE 2026-06-17, item 4). */}
        <LeftIconButton
          disabled
          title="Research mode toggle (coming soon)"
          data-testid="chat-input-mode"
        >
          <IconMode size={15} />
        </LeftIconButton>

        {/* Legacy / standalone mode ONLY: when ChatInput is UNCONTROLLED (no
            `modelId` prop), the model selector still lives in the composer so
            existing standalone usages keep a way to switch models. When the
            header drives selection (controlled), this is hidden — the header's
            ModelSelectorButton is the single trigger (item 1). */}
        {!controlled && (
          <>
            {/* Subtle divider */}
            <span
              aria-hidden="true"
              style={{
                width: 1,
                height: 14,
                background: "rgba(255,255,255,0.1)",
                margin: "0 4px",
                flexShrink: 0,
              }}
            />
            <button
              ref={modelButtonRef}
              onClick={() => setPopoverOpen((o) => !o)}
              title={`Model: ${selectedModel.label}`}
              aria-label={`Model: ${selectedModel.label}`}
              data-testid="chat-input-model"
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                width: 28,
                height: 28,
                borderRadius: 6,
                border: "none",
                padding: 0,
                background: popoverOpen ? "rgba(255,255,255,0.08)" : "transparent",
                color: accentColor,
                cursor: "pointer",
                transition: "background 120ms ease, color 120ms ease",
                flexShrink: 0,
              }}
            >
              <IconModel size={15} />
            </button>
            {popoverOpen && (
              <ModelPopover
                anchorRef={modelButtonRef as React.RefObject<HTMLButtonElement>}
                selectedId={selectedModel.id}
                onSelect={handleModelSelect}
                onClose={() => setPopoverOpen(false)}
              />
            )}
          </>
        )}

        {/* Spacer pushes the send button to the right edge so the left
            controls sit INLINE with send (Claude-Code composer), NATE 2026-06-17. */}
        <div style={{ flex: 1 }} />

        {/* Send / stop — inline on the controls row, right-aligned. Circle in
            send state (accent-tinted), square in stop state (item 3). */}
        <button
          data-testid="chat-input-action"
          data-action-state={state}
          data-shape={isStop ? "square" : "circle"}
          aria-label={isStop ? "Stop response" : "Send message"}
          onClick={onButtonClick}
          disabled={buttonDisabled}
          style={{
            flex: "0 0 auto",
            width: 32,
            height: 32,
            border: "none",
            color: "#fff",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 0,
            transition:
              "background 180ms ease, opacity 180ms ease, transform 120ms ease",
            ...buttonStyle,
          }}
        >
          <ActionGlyph state={state} />
        </button>
      </div>
    </div>
  );
}
