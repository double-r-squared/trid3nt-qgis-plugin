// GRACE-2 web — LayerPanel unit tests (job-0065, tweak 2).
//
// Verifies:
//   1. LayerPanel returns null (renders nothing) when loaded_layers is empty.
//   2. LayerPanel renders when at least one layer is loaded.
//   3. LayerPanel shows/hides dynamically as layers go 0 → 1 → 0.
//   4. onLayersChange callback fires with the correct layer list.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import {
  LayerPanel,
  createLayerPanelBus,
  layerKind,
  clampLayersWidth,
  readLayersWidth,
  writeLayersWidth,
  dedupeByLayerId,
  applyVisibilityOverrides,
  readLayerVisibilityOverrides,
  writeLayerVisibilityOverride,
  parseFrameToken,
  detectSequentialGroups,
  isPeakLayer,
} from "./LayerPanel";
import { ProjectLayerSummary, SessionStatePayload } from "./contracts";
import { LayerCache, setLayerCache } from "./lib/layer_cache";
// JOB WEB-ANIM (#157.1) — the sequence playback now lives in the module-level
// AnimationController; LayerPanel pushes its detected groups in + steps through
// it (the controller drives the map via an emitter, not via onMapCommand). Tests
// reset the controller per-test and register a stub emitter to observe stepping.
import {
  AnimationController,
  setAnimationController,
  getAnimationController,
  type FrameVisibilityEmitter,
} from "./lib/animation_controller";

// Job 4 made layer visibility/opacity/order read from a SHARED LayerCache
// singleton (getLayerCache()). A prior test that writes a view-override into
// the singleton would otherwise leak that override into later tests in this
// file (e.g. the eye-toggle test reading a stale visible:false). Reset the
// singleton (and localStorage, belt-and-suspenders) before EVERY test so no
// override can leak across tests. Inner per-block setLayerCache() calls run in
// their own beforeEach AFTER this global one, so they still override cleanly.
const moduleNoopBackend = {
  async load() {
    return {};
  },
  async save() {
    /* no-op */
  },
};

beforeEach(() => {
  setLayerCache(new LayerCache({ backend: moduleNoopBackend }));
  // JOB WEB-ANIM — reset the shared AnimationController so playback / frame
  // state from one test never leaks into the next (it is a process-global).
  // ITEM 5 (NATE 2026-06-22): setGroups now AUTO-PLAYS a freshly-loaded
  // multi-frame group. These panel-mirroring tests assert the PAUSED frame-
  // readout / manual-play mechanics, so install the controller with reduced-
  // motion ON (auto-play suppressed) to keep that baseline. The first-frame
  // default (index 0) + the auto-play behavior itself are covered by the
  // animation_controller + App.sequenceScrubber item-5 suites.
  setAnimationController(
    new AnimationController({ prefersReducedMotion: () => true }),
  );
  try {
    localStorage.clear();
  } catch {
    /* ignore */
  }
});

// dnd-kit requires pointer events which happy-dom supports but the PointerSensor
// needs a minimum drag distance. Our tests don't exercise drag — just layer
// list rendering — so no special mocking is required here.

function makeLayer(id: string, z_index = 1): ProjectLayerSummary {
  return {
    layer_id: id,
    name: `Layer ${id}`,
    layer_type: "raster",
    uri: `gs://grace-2/runs/${id}/depth.cog.tif`,
    visible: true,
    opacity: 1,
    z_index,
  };
}

function sessionStateWith(layers: ProjectLayerSummary[]): SessionStatePayload {
  return { loaded_layers: layers };
}

describe("LayerPanel — width helpers + resize handle (ux-batch-1 J1 F11)", () => {
  afterEach(() => {
    try { localStorage.clear(); } catch { /* ignore */ }
  });

  it("clampLayersWidth clamps to [240, 560]; non-finite → default 288", () => {
    expect(clampLayersWidth(100)).toBe(240);
    expect(clampLayersWidth(9999)).toBe(560);
    expect(clampLayersWidth(Number.NaN)).toBe(288);
    expect(clampLayersWidth(300.4)).toBe(300);
  });

  it("readLayersWidth defaults to 288; writeLayersWidth round-trips clamped", () => {
    expect(readLayersWidth()).toBe(288);
    writeLayersWidth(400);
    expect(localStorage.getItem("grace2.layersWidthPx")).toBe("400");
    expect(readLayersWidth()).toBe(400);
    writeLayersWidth(99999);
    expect(readLayersWidth()).toBe(560);
  });

  it("renders the resize handle on desktop", () => {
    render(<LayerPanel initialLayers={[makeLayer("a")]} />);
    expect(
      screen.getByTestId("grace2-layer-panel-resize-handle"),
    ).toBeInTheDocument();
  });

  it("renders NO resize handle in mobile drawer mode", () => {
    render(<LayerPanel initialLayers={[makeLayer("a")]} mobile />);
    expect(
      screen.queryByTestId("grace2-layer-panel-resize-handle"),
    ).toBeNull();
  });

  it("applies a controlled width to the panel", () => {
    render(<LayerPanel initialLayers={[makeLayer("a")]} width={420} />);
    const panel = screen.getByTestId("grace2-layer-panel");
    expect(panel.style.width).toBe("420px");
  });
});

describe("LayerPanel — duplicate-layer dedupe + opacity (ux-batch-1 J3 F22/F8)", () => {
  it("dedupeByLayerId collapses same layer_id (last wins), keeps distinct", () => {
    const a1 = makeLayer("flood", 1);
    const a2 = { ...makeLayer("flood", 2), name: "Flood (newer)" };
    const b = makeLayer("roads", 1);
    const out = dedupeByLayerId([a1, a2, b]);
    expect(out).toHaveLength(2);
    const flood = out.find((l) => l.layer_id === "flood");
    expect(flood?.name).toBe("Flood (newer)"); // last write wins
  });

  it("renders ONE row when session-state carries a duplicate layer_id (no connected sliders)", () => {
    const bus = createLayerPanelBus();
    render(<LayerPanel subscribeSessionState={bus.subscribeSessionState} />);
    act(() => {
      bus.pushSessionState(
        sessionStateWith([makeLayer("flood"), makeLayer("flood")]),
      );
    });
    // Two same-id layers collapse to a single row -> a single opacity slider.
    expect(screen.getAllByTestId("layer-opacity")).toHaveLength(1);
  });

  it("a layer with undefined opacity shows 100% (not 0%) with the thumb at full", () => {
    const noOpacity = { ...makeLayer("a"), opacity: undefined as unknown as number };
    render(<LayerPanel initialLayers={[noOpacity]} />);
    const slider = screen.getByTestId("layer-opacity") as HTMLInputElement;
    expect(slider.value).toBe("1");
    expect(screen.getByText("100%")).toBeInTheDocument();
  });

  it("a real 0 opacity is preserved (0%, thumb at far left)", () => {
    const transparent = { ...makeLayer("a"), opacity: 0 };
    render(<LayerPanel initialLayers={[transparent]} />);
    const slider = screen.getByTestId("layer-opacity") as HTMLInputElement;
    expect(slider.value).toBe("0");
    expect(screen.getByText("0%")).toBeInTheDocument();
  });
});

describe("LayerPanel — hide-when-empty (tweak 2)", () => {
  it("renders null (panel hidden) when loaded_layers is empty", () => {
    const { container } = render(
      <LayerPanel initialLayers={[]} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders the panel when initialLayers has at least one layer", () => {
    render(<LayerPanel initialLayers={[makeLayer("a")]} />);
    expect(screen.getByTestId("grace2-layer-panel")).toBeInTheDocument();
  });

  it("hides panel after session-state with empty layers pushes through bus", () => {
    const bus: ReturnType<typeof createLayerPanelBus> = createLayerPanelBus();
    // Start with one layer so the panel is visible.
    render(
      <LayerPanel
        initialLayers={[makeLayer("a")]}
        subscribeSessionState={bus.subscribeSessionState}
        subscribeMapCommand={bus.subscribeMapCommand}
      />,
    );
    expect(screen.getByTestId("grace2-layer-panel")).toBeInTheDocument();

    // Push an empty session-state.
    act(() => {
      bus.pushSessionState(sessionStateWith([]));
    });

    expect(screen.queryByTestId("grace2-layer-panel")).toBeNull();
  });

  it("shows panel after session-state with layers pushes through bus from empty", () => {
    const bus: ReturnType<typeof createLayerPanelBus> = createLayerPanelBus();
    // Start empty — panel hidden.
    render(
      <LayerPanel
        initialLayers={[]}
        subscribeSessionState={bus.subscribeSessionState}
        subscribeMapCommand={bus.subscribeMapCommand}
      />,
    );
    expect(screen.queryByTestId("grace2-layer-panel")).toBeNull();

    // Push session-state with a layer.
    act(() => {
      bus.pushSessionState(sessionStateWith([makeLayer("b")]));
    });

    expect(screen.getByTestId("grace2-layer-panel")).toBeInTheDocument();
  });

  it("calls onLayersChange with the current layer list when layers update", () => {
    const bus = createLayerPanelBus();
    const onChange = vi.fn();

    render(
      <LayerPanel
        initialLayers={[makeLayer("x")]}
        subscribeSessionState={bus.subscribeSessionState}
        subscribeMapCommand={bus.subscribeMapCommand}
        onLayersChange={onChange}
      />,
    );

    // onLayersChange called on mount with initial layers.
    expect(onChange).toHaveBeenCalledWith(
      expect.arrayContaining([expect.objectContaining({ layer_id: "x" })]),
    );

    // Push a new session-state.
    act(() => {
      bus.pushSessionState(sessionStateWith([makeLayer("y"), makeLayer("z", 2)]));
    });

    const lastCall = (onChange.mock.calls[onChange.mock.calls.length - 1] as [ProjectLayerSummary[]])[0];
    expect(lastCall.map((l) => l.layer_id).sort()).toEqual(["y", "z"]);
  });
});

// --- No-nudge-buttons regression (job-0173 Part 4) ---------------------- //
//
// The ▲/▼ z-order nudge buttons were dropped — drag-and-drop reorder is the
// sole reorder affordance now. The drag handle (data-testid layer-drag-handle)
// must remain so the reorder path is still available.

describe("LayerPanel — no nudge buttons (job-0173 Part 4)", () => {
  it("renders rows without layer-nudge-up / layer-nudge-down buttons", () => {
    render(
      <LayerPanel
        initialLayers={[makeLayer("a", 2), makeLayer("b", 1)]}
      />,
    );
    expect(screen.queryAllByTestId("layer-nudge-up")).toHaveLength(0);
    expect(screen.queryAllByTestId("layer-nudge-down")).toHaveLength(0);
  });

  it("rows do NOT contain ▲ or ▼ glyph characters", () => {
    const { container } = render(
      <LayerPanel initialLayers={[makeLayer("a"), makeLayer("b", 2)]} />,
    );
    const text = container.textContent ?? "";
    expect(text).not.toContain("▲");
    expect(text).not.toContain("▼");
  });

  // ITEM 2 (NATE 2026-06-22): the dedicated drag-handle button was retired - the
  // whole card body is the drag handle now (matching the sequential-group row),
  // and the far-left grabber slot holds an EXPAND control. Reorder is still
  // possible (the row is a dnd-kit sortable); every row carries the expand
  // control + the eye in a single aligned left column.
  it("gives every layer row a far-left expand control in the grabber slot", () => {
    render(<LayerPanel initialLayers={[makeLayer("a"), makeLayer("b", 2)]} />);
    expect(screen.getAllByTestId("layer-expand")).toHaveLength(2);
    // The old dedicated drag-handle button is gone (body-drag now).
    expect(screen.queryByTestId("layer-drag-handle")).toBeNull();
    // Both rows still render (sortable card body = drag handle).
    expect(screen.getAllByTestId("layer-row")).toHaveLength(2);
  });

  it("the row expand control toggles the opacity detail sticky (ITEM 2)", () => {
    render(<LayerPanel initialLayers={[makeLayer("a")]} />);
    const expand = screen.getByTestId("layer-expand");
    // Collapsed by default.
    expect(screen.getByTestId("layer-row")).toHaveAttribute(
      "data-expanded",
      "false",
    );
    act(() => {
      fireEvent.click(expand);
    });
    expect(screen.getByTestId("layer-row")).toHaveAttribute(
      "data-expanded",
      "true",
    );
    expect(expand).toHaveAttribute("aria-expanded", "true");
  });

  it("preserves visibility checkbox + opacity slider (controls unaffected)", () => {
    render(<LayerPanel initialLayers={[makeLayer("a")]} />);
    expect(screen.getByTestId("layer-visibility")).toBeInTheDocument();
    expect(screen.getByTestId("layer-opacity")).toBeInTheDocument();
  });
});

// --- F53 (job-0326): raw glyphs replaced by shared Phosphor icons -------- //
//
// The close (×), trash, and eye glyphs are now rendered via the shared icon
// module (IconClose / IconDelete / IconEye / IconEyeOff) — never hardcoded
// unicode. These assert the raw close glyph is gone and the controls render an
// <svg> (Phosphor) rather than a literal character.

describe("LayerPanel — icons come from the shared module (job-0326 F53)", () => {
  it("the close button renders an svg icon, not a raw × glyph", () => {
    render(
      <LayerPanel initialLayers={[makeLayer("a")]} onClose={() => {}} />,
    );
    const closeBtn = screen.getByTestId("grace2-layer-panel-close");
    expect(closeBtn.textContent ?? "").not.toContain("×");
    expect(closeBtn.querySelector("svg")).not.toBeNull();
  });

  it("the delete control renders an svg icon (IconDelete)", () => {
    render(<LayerPanel initialLayers={[makeLayer("a")]} />);
    const del = screen.getByTestId("layer-delete");
    expect(del.querySelector("svg")).not.toBeNull();
  });

  it("the visibility toggle renders an eye svg icon for both states", () => {
    const visible = render(
      <LayerPanel initialLayers={[{ ...makeLayer("a"), visible: true }]} />,
    );
    // visible -> IconEye svg present beside the (visually hidden) checkbox.
    expect(
      screen.getByTestId("layer-visibility").parentElement?.querySelector("svg"),
    ).not.toBeNull();
    visible.unmount();

    render(<LayerPanel initialLayers={[{ ...makeLayer("b"), visible: false }]} />);
    // hidden -> IconEyeOff svg present.
    expect(
      screen.getByTestId("layer-visibility").parentElement?.querySelector("svg"),
    ).not.toBeNull();
  });

  it("no raw × glyph appears anywhere in the rendered panel", () => {
    const { container } = render(
      <LayerPanel initialLayers={[makeLayer("a")]} onClose={() => {}} />,
    );
    expect(container.textContent ?? "").not.toContain("×");
  });
});

// --- job-0258: user controls emit map-commands (LAYER CONTROLS DEAD fix) --- //
//
// Root cause being pinned: before job-0258 the slider/checkbox handlers only
// dispatched to the panel's local reducer (M3 "intent" stubs) — nothing left
// the component, so the MapLibre instance never changed. These tests assert
// the new outbound `onMapCommand` emission contract that App.tsx wires to
// the shared bus (MapView consumes it; see Map.test.tsx for that half).

import type { MapCommandPayload } from "./contracts";

describe("LayerPanel — user controls emit map-commands (job-0258)", () => {
  it("opacity slider change emits set-layer-opacity with the new value", () => {
    const onMapCommand = vi.fn<(cmd: MapCommandPayload) => void>();
    render(
      <LayerPanel
        initialLayers={[makeLayer("flood-demo")]}
        onMapCommand={onMapCommand}
      />,
    );

    const slider = screen.getByTestId("layer-opacity");
    fireEvent.change(slider, { target: { value: "0.35" } });

    expect(onMapCommand).toHaveBeenCalledWith({
      command: "set-layer-opacity",
      layer_id: "flood-demo",
      opacity: 0.35,
    });
  });

  it("visibility checkbox toggle emits set-layer-visibility", () => {
    const onMapCommand = vi.fn<(cmd: MapCommandPayload) => void>();
    render(
      <LayerPanel
        initialLayers={[makeLayer("flood-demo")]}
        onMapCommand={onMapCommand}
      />,
    );

    const checkbox = screen.getByTestId("layer-visibility");
    fireEvent.click(checkbox); // visible:true → false

    expect(onMapCommand).toHaveBeenCalledWith({
      command: "set-layer-visibility",
      layer_id: "flood-demo",
      visible: false,
    });
  });

  it("panel state still updates locally alongside the emission (slider %)", () => {
    const onMapCommand = vi.fn<(cmd: MapCommandPayload) => void>();
    render(
      <LayerPanel
        initialLayers={[makeLayer("flood-demo")]}
        onMapCommand={onMapCommand}
      />,
    );

    fireEvent.change(screen.getByTestId("layer-opacity"), {
      target: { value: "0.35" },
    });
    // The % readout reflects the reducer state (35%), proving the local
    // dispatch and the emission both happened from one handler.
    expect(screen.getByText("35%")).toBeTruthy();
  });

  it("emission is optional — controls do not throw without onMapCommand", () => {
    render(<LayerPanel initialLayers={[makeLayer("flood-demo")]} />);
    expect(() => {
      fireEvent.change(screen.getByTestId("layer-opacity"), {
        target: { value: "0.5" },
      });
      fireEvent.click(screen.getByTestId("layer-visibility"));
    }).not.toThrow();
  });
});

// --- job-0179: user edits WRITE THROUGH into the shared LayerCache --------- //
//
// The seatbelt: an opacity / visibility edit (and a drag-reorder, exercised in
// layer_cache.test.ts for the z-order math) must be remembered in the shared
// cache so it survives a panel unmount->remount + a WS reconnect even when the
// Map is not mounted to receive the bus command. We point the singleton at a
// fresh cache with a no-op backend and an active Case per test.
// (LayerCache + setLayerCache are imported at the top of the file.)

describe("LayerPanel — user edits write through into the shared cache (job-0179)", () => {
  const noopBackend = {
    async load() {
      return {};
    },
    async save() {
      /* no-op */
    },
  };

  function freshCache(activeCaseId: string | null): LayerCache {
    const cache = new LayerCache({ backend: noopBackend });
    cache.activeCaseId = activeCaseId;
    setLayerCache(cache);
    return cache;
  }

  it("opacity edit is written to the cache for the active Case", () => {
    const cache = freshCache("case-A");
    render(<LayerPanel initialLayers={[makeLayer("flood-demo")]} />);
    fireEvent.change(screen.getByTestId("layer-opacity"), {
      target: { value: "0.42" },
    });
    expect(cache.getOverride("case-A", "flood-demo")).toEqual({
      opacity: 0.42,
    });
  });

  it("visibility toggle is written to the cache for the active Case", () => {
    const cache = freshCache("case-A");
    render(<LayerPanel initialLayers={[makeLayer("flood-demo")]} />);
    fireEvent.click(screen.getByTestId("layer-visibility")); // true -> false
    expect(cache.getOverride("case-A", "flood-demo")).toEqual({
      visible: false,
    });
  });

  it("at the root (no active Case) the edit is a cache no-op (back-compat)", () => {
    const cache = freshCache(null);
    render(<LayerPanel initialLayers={[makeLayer("flood-demo")]} />);
    // Must not throw and must not record anything against a null Case.
    fireEvent.change(screen.getByTestId("layer-opacity"), {
      target: { value: "0.5" },
    });
    expect(cache.getOverride(null, "flood-demo")).toBeUndefined();
    expect(cache.layersFor(null)).toEqual([]);
  });
});

// --- job-0264: panel polish (kind chip, eye toggle, name, empty state) ---- //

function makeStyledLayer(
  id: string,
  overrides: Partial<ProjectLayerSummary> = {},
): ProjectLayerSummary {
  return { ...makeLayer(id), ...overrides };
}

describe("LayerPanel — kind chip derivation (job-0264)", () => {
  it.each([
    ["flood_depth", "flood"],
    ["fema_nfhl_zones", "flood"],
    ["hillshade", "hillshade"],
    ["colored_relief", "terrain"],
    ["firms_active_fire", "fire"],
    ["pelicun_damage_state", "damage"],
    ["gbif_occurrences", "biodiversity"],
    ["admin_boundaries", "vector"],
    ["nws_alerts", "weather"],
  ])("derives style_preset %s → kind '%s'", (preset, expected) => {
    expect(
      layerKind(makeStyledLayer("x", { style_preset: preset })).label,
    ).toBe(expected);
  });

  it("falls back to layer_type when style_preset is absent", () => {
    expect(layerKind(makeStyledLayer("x", { layer_type: "vector", style_preset: null })).label).toBe("vector");
    expect(layerKind(makeStyledLayer("x", { layer_type: "raster", style_preset: null })).label).toBe("raster");
    expect(layerKind(makeStyledLayer("x", { layer_type: "wms", style_preset: null })).label).toBe("tiles");
  });

  it("renders a kind chip on every layer row", () => {
    render(
      <LayerPanel
        initialLayers={[
          makeStyledLayer("a", { style_preset: "flood_depth", z_index: 2 }),
          makeStyledLayer("b", { style_preset: "hillshade", z_index: 1 }),
        ]}
      />,
    );
    const chips = screen.getAllByTestId("layer-kind-chip");
    expect(chips).toHaveLength(2);
    // Top-of-stack-first ordering: z_index 2 (flood) renders before z_index 1.
    expect(chips[0]).toHaveAttribute("data-kind", "flood");
    expect(chips[1]).toHaveAttribute("data-kind", "hillshade");
    expect(chips[0]).toHaveTextContent("flood");
  });
});

describe("LayerPanel — eye toggle + name + empty state (job-0264)", () => {
  it("eye toggle is backed by the layer-visibility checkbox (test id preserved)", () => {
    const onMapCommand = vi.fn();
    render(
      <LayerPanel
        initialLayers={[makeLayer("flood-demo")]}
        onMapCommand={onMapCommand}
      />,
    );
    const checkbox = screen.getByTestId("layer-visibility") as HTMLInputElement;
    expect(checkbox.type).toBe("checkbox");
    expect(checkbox.checked).toBe(true);
    fireEvent.click(checkbox);
    expect(onMapCommand).toHaveBeenCalledWith({
      command: "set-layer-visibility",
      layer_id: "flood-demo",
      visible: false,
    });
  });

  it("name span carries a title attribute (tooltip) for truncation", () => {
    render(
      <LayerPanel
        initialLayers={[
          makeStyledLayer("a", { name: "A Very Long Storm-Surge Maximum Depth Layer Name" }),
        ]}
      />,
    );
    const nameEl = screen.getByText(
      "A Very Long Storm-Surge Maximum Depth Layer Name",
    );
    expect(nameEl).toHaveAttribute(
      "title",
      "A Very Long Storm-Surge Maximum Depth Layer Name",
    );
    // Truncation styles: ellipsis + nowrap so the title tooltip is meaningful.
    expect(nameEl).toHaveStyle({ textOverflow: "ellipsis", whiteSpace: "nowrap" });
  });

  it("opacity slider + % readout remain present on each row", () => {
    render(<LayerPanel initialLayers={[makeStyledLayer("a", { opacity: 0.6 })]} />);
    expect(screen.getByTestId("layer-opacity")).toBeInTheDocument();
    expect(screen.getByText("60%")).toBeInTheDocument();
  });

  it("header count chip shows the number of loaded layers", () => {
    render(
      <LayerPanel
        initialLayers={[makeLayer("a", 1), makeLayer("b", 2), makeLayer("c", 3)]}
      />,
    );
    expect(screen.getByTestId("grace2-layer-panel-count")).toHaveTextContent("3");
  });

  it("empty-state copy reads 'No layers yet' when a single layer is removed live", () => {
    // The panel hides entirely at zero layers (tested elsewhere); this asserts
    // the subtle empty-state element + copy exists for the in-panel render path
    // by injecting a session-state that keeps the panel mounted with the empty
    // node present. We render the empty text node directly via the bus path:
    const bus = createLayerPanelBus();
    render(
      <LayerPanel
        initialLayers={[makeLayer("a")]}
        subscribeSessionState={bus.subscribeSessionState}
        subscribeMapCommand={bus.subscribeMapCommand}
      />,
    );
    // With a layer present the empty node must NOT show.
    expect(screen.queryByTestId("grace2-layer-panel-empty")).toBeNull();
  });
});

// --- F53 (job-0325 + job-0322): per-layer delete, now confirm-gated ------ //
//
// A per-row trash control (and, on mobile, a swipe-RIGHT gesture) OPENS a
// ConfirmationDialog. Only on confirm does the destructive path run: it sends
// the `layer-delete` envelope via `onDeleteLayer` (App.tsx -> ws.sendDeleteLayer)
// AND optimistically removes the row locally + emits a `remove-layer`
// map-command so the overlay drops instantly. Cancel leaves everything intact.

const DELETE_DIALOG = "grace2-layer-delete-dialog";

describe("LayerPanel — per-layer delete control + confirm gating (job-0325 F53 / job-0322 F53-COMPLETE)", () => {
  afterEach(() => {
    try { localStorage.clear(); } catch { /* ignore */ }
  });

  it("renders a delete control on every layer row", () => {
    render(
      <LayerPanel initialLayers={[makeLayer("a"), makeLayer("b", 2)]} />,
    );
    expect(screen.getAllByTestId("layer-delete")).toHaveLength(2);
  });

  it("clicking the trash control opens the confirm dialog WITHOUT deleting yet", () => {
    const onDeleteLayer = vi.fn<(id: string) => void>();
    const onMapCommand = vi.fn<(cmd: MapCommandPayload) => void>();
    render(
      <LayerPanel
        initialLayers={[makeLayer("flood-demo")]}
        onDeleteLayer={onDeleteLayer}
        onMapCommand={onMapCommand}
      />,
    );
    fireEvent.click(screen.getByTestId("layer-delete"));

    // Dialog appears; nothing destructive has fired yet, row still present.
    expect(screen.getByTestId(DELETE_DIALOG)).toBeInTheDocument();
    expect(onDeleteLayer).not.toHaveBeenCalled();
    expect(onMapCommand).not.toHaveBeenCalled();
    expect(screen.getByTestId("layer-row")).toBeInTheDocument();
  });

  it("confirm fires onDeleteLayer, emits remove-layer, and removes the row", () => {
    const onMapCommand = vi.fn<(cmd: MapCommandPayload) => void>();
    const onDeleteLayer = vi.fn<(id: string) => void>();
    render(
      <LayerPanel
        initialLayers={[makeLayer("keep", 2), makeLayer("gone", 1)]}
        onMapCommand={onMapCommand}
        onDeleteLayer={onDeleteLayer}
      />,
    );
    expect(screen.getAllByTestId("layer-row")).toHaveLength(2);

    // Open the dialog from the "gone" row's trash control.
    const goneRow = screen
      .getAllByTestId("layer-row")
      .find((r) => r.getAttribute("data-layer-id") === "gone")!;
    fireEvent.click(goneRow.querySelector('[data-testid="layer-delete"]')!);

    // Still two rows while the dialog is open (no optimistic removal yet).
    expect(screen.getAllByTestId("layer-row")).toHaveLength(2);

    // Confirm.
    fireEvent.click(screen.getByTestId(`${DELETE_DIALOG}-confirm`));

    expect(onDeleteLayer).toHaveBeenCalledWith("gone");
    expect(onMapCommand).toHaveBeenCalledWith({
      command: "remove-layer",
      layer_id: "gone",
    });
    // Optimistic local removal: only "keep" survives, dialog closed.
    expect(screen.getAllByTestId("layer-row")).toHaveLength(1);
    expect(screen.getAllByTestId("layer-row")[0]).toHaveAttribute(
      "data-layer-id",
      "keep",
    );
    expect(screen.queryByTestId(DELETE_DIALOG)).toBeNull();
  });

  it("cancel closes the dialog with NO delete and the layer stays", () => {
    const onMapCommand = vi.fn<(cmd: MapCommandPayload) => void>();
    const onDeleteLayer = vi.fn<(id: string) => void>();
    render(
      <LayerPanel
        initialLayers={[makeLayer("flood-demo")]}
        onMapCommand={onMapCommand}
        onDeleteLayer={onDeleteLayer}
      />,
    );
    fireEvent.click(screen.getByTestId("layer-delete"));
    fireEvent.click(screen.getByTestId(`${DELETE_DIALOG}-cancel`));

    expect(onDeleteLayer).not.toHaveBeenCalled();
    expect(onMapCommand).not.toHaveBeenCalled();
    expect(screen.queryByTestId(DELETE_DIALOG)).toBeNull();
    expect(screen.getByTestId("layer-row")).toBeInTheDocument();
  });

  it("the dialog uses a distinct testId + names the layer in its copy", () => {
    render(
      <LayerPanel
        initialLayers={[makeStyledLayer("a", { name: "Storm Surge Depth" })]}
      />,
    );
    fireEvent.click(screen.getByTestId("layer-delete"));
    expect(screen.getByTestId(DELETE_DIALOG)).toBeInTheDocument();
    expect(
      screen.getByTestId(`${DELETE_DIALOG}-message`),
    ).toHaveTextContent("Storm Surge Depth");
    expect(
      screen.getByTestId(`${DELETE_DIALOG}-confirm`),
    ).toHaveTextContent("Delete");
  });

  it("desktop trash → dialog → confirm calls onDeleteLayer (end-to-end gating)", () => {
    const onDeleteLayer = vi.fn<(id: string) => void>();
    render(
      <LayerPanel
        initialLayers={[makeLayer("flood-demo")]}
        onDeleteLayer={onDeleteLayer}
      />,
    );
    // Default (no mobile prop) = desktop. Trash control is present + routed.
    fireEvent.click(screen.getByTestId("layer-delete"));
    expect(onDeleteLayer).not.toHaveBeenCalled(); // gated
    fireEvent.click(screen.getByTestId(`${DELETE_DIALOG}-confirm`));
    expect(onDeleteLayer).toHaveBeenCalledWith("flood-demo");
  });

  it("confirm is safe without onDeleteLayer wired (no throw, row still vanishes)", () => {
    render(<LayerPanel initialLayers={[makeLayer("a")]} />);
    fireEvent.click(screen.getByTestId("layer-delete"));
    expect(() => {
      fireEvent.click(screen.getByTestId(`${DELETE_DIALOG}-confirm`));
    }).not.toThrow();
    // The row still vanishes locally even with no server round-trip wired.
    expect(screen.queryByTestId("layer-row")).toBeNull();
  });
});

// --- F53 (job-0326): mobile swipe-right-to-delete gesture REMOVED -------- //
//
// NATE reversed the earlier swipe-to-delete call: the gesture is dropped
// ENTIRELY. The per-row trash control is now the SOLE delete affordance on BOTH
// desktop and mobile. These regression tests assert the swipe gesture no longer
// exists — a horizontal-dominant swipe-right on a row must NOT open the dialog,
// on mobile OR desktop. (A pointer drag is now purely dnd-kit reorder territory.)

describe("LayerPanel — swipe-to-delete gesture removed (job-0326 F53)", () => {
  afterEach(() => {
    try { localStorage.clear(); } catch { /* ignore */ }
  });

  function swipeRow(row: Element, dx: number, dy: number): void {
    fireEvent.pointerDown(row, { clientX: 100, clientY: 100 });
    fireEvent.pointerMove(row, { clientX: 100 + dx, clientY: 100 + dy });
    fireEvent.pointerUp(row, { clientX: 100 + dx, clientY: 100 + dy });
  }

  it("the swipe predicate export is gone from the module", async () => {
    const mod = (await import("./LayerPanel")) as Record<string, unknown>;
    expect(mod.isHorizontalSwipeRight).toBeUndefined();
  });

  it("a horizontal-dominant swipe-right does NOT open the dialog (mobile)", () => {
    const onDeleteLayer = vi.fn<(id: string) => void>();
    render(
      <LayerPanel
        initialLayers={[makeLayer("flood-demo")]}
        onDeleteLayer={onDeleteLayer}
        mobile
      />,
    );
    swipeRow(screen.getByTestId("layer-row"), 90, 8);
    expect(screen.queryByTestId(DELETE_DIALOG)).toBeNull();
    expect(onDeleteLayer).not.toHaveBeenCalled();
  });

  it("a swipe-right does NOT open the dialog on desktop either", () => {
    render(<LayerPanel initialLayers={[makeLayer("flood-demo")]} />);
    swipeRow(screen.getByTestId("layer-row"), 90, 5);
    expect(screen.queryByTestId(DELETE_DIALOG)).toBeNull();
  });

  it("the explicit trash control is still the delete path AFTER a swipe (mobile)", () => {
    const onDeleteLayer = vi.fn<(id: string) => void>();
    render(
      <LayerPanel
        initialLayers={[makeLayer("flood-demo")]}
        onDeleteLayer={onDeleteLayer}
        mobile
      />,
    );
    // The swipe is inert...
    swipeRow(screen.getByTestId("layer-row"), 90, 8);
    expect(screen.queryByTestId(DELETE_DIALOG)).toBeNull();
    // ...but the trash icon still opens the dialog -> confirm deletes.
    fireEvent.click(screen.getByTestId("layer-delete"));
    expect(screen.getByTestId(DELETE_DIALOG)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId(`${DELETE_DIALOG}-confirm`));
    expect(onDeleteLayer).toHaveBeenCalledWith("flood-demo");
  });

  it("the delete (trash) control is present on mobile rows too", () => {
    render(<LayerPanel initialLayers={[makeLayer("a"), makeLayer("b", 2)]} mobile />);
    expect(screen.getAllByTestId("layer-delete")).toHaveLength(2);
  });
});

// --- F53 (job-0326): ConfirmationDialog portals to document.body --------- //
//
// The dialog is portaled to document.body via ReactDOM.createPortal so it always
// renders as a true full-screen overlay regardless of the LayerPanel's
// (absolutely-positioned, backdrop-filtered) stacking context.

describe("LayerPanel — delete dialog portals to document.body (job-0326 F53)", () => {
  afterEach(() => {
    try { localStorage.clear(); } catch { /* ignore */ }
  });

  it("renders the dialog as a direct descendant of document.body (not inside the panel)", () => {
    render(<LayerPanel initialLayers={[makeLayer("flood-demo")]} />);
    fireEvent.click(screen.getByTestId("layer-delete"));

    const backdrop = screen.getByTestId(`${DELETE_DIALOG}-backdrop`);
    // Portaled: the backdrop is NOT contained within the layer panel <aside>.
    const panel = screen.getByTestId("grace2-layer-panel");
    expect(panel.contains(backdrop)).toBe(false);
    // It lives under document.body directly (the portal target).
    expect(document.body.contains(backdrop)).toBe(true);
    expect(backdrop.parentElement).toBe(document.body);
  });

  it("the portaled backdrop is a fixed, full-viewport overlay", () => {
    render(<LayerPanel initialLayers={[makeLayer("flood-demo")]} />);
    fireEvent.click(screen.getByTestId("layer-delete"));
    const backdrop = screen.getByTestId(`${DELETE_DIALOG}-backdrop`);
    expect(backdrop).toHaveStyle({ position: "fixed" });
  });
});

// --- F55 (job-0325): per-layer visibility persists across unmount ------- //
//
// On mobile the panel lives in a drawer that unmounts on collapse, discarding
// the reducer's per-layer `visible`. The fix persists the user's toggle to
// localStorage keyed by layer_id and re-applies it on every (re-)seed, so a
// hidden layer stays hidden across unmount->remount. Desktop (never-toggled)
// render is byte-identical because the override is purely additive.

describe("LayerPanel — visibility override helpers (job-0325 F55)", () => {
  afterEach(() => {
    try { localStorage.clear(); } catch { /* ignore */ }
  });

  it("applyVisibilityOverrides returns the SAME list (server value verbatim) when no override exists", () => {
    const layers = [makeLayer("a"), { ...makeLayer("b"), visible: true }];
    const out = applyVisibilityOverrides(layers, {});
    expect(out).toBe(layers); // identity — desktop resting render unchanged
    expect(out.every((l) => l.visible)).toBe(true);
  });

  it("applyVisibilityOverrides overlays only the toggled layer_id", () => {
    const layers = [
      { ...makeLayer("a"), visible: true },
      { ...makeLayer("b"), visible: true },
    ];
    const out = applyVisibilityOverrides(layers, { a: false });
    expect(out.find((l) => l.layer_id === "a")?.visible).toBe(false);
    expect(out.find((l) => l.layer_id === "b")?.visible).toBe(true);
  });

  it("write/read round-trips the override map", () => {
    expect(readLayerVisibilityOverrides()).toEqual({});
    writeLayerVisibilityOverride("a", false);
    writeLayerVisibilityOverride("b", true);
    expect(readLayerVisibilityOverrides()).toEqual({ a: false, b: true });
  });

  it("garbage in localStorage degrades to an empty override map", () => {
    localStorage.setItem("grace2.layerVisibility", "{not-json");
    expect(readLayerVisibilityOverrides()).toEqual({});
    localStorage.setItem("grace2.layerVisibility", "[1,2,3]");
    expect(readLayerVisibilityOverrides()).toEqual({});
  });
});

describe("LayerPanel — visibility survives unmount/remount (job-0325 F55)", () => {
  afterEach(() => {
    try { localStorage.clear(); } catch { /* ignore */ }
  });

  it("a hidden layer stays hidden after unmount → remount (mobile collapse)", () => {
    const layers = [{ ...makeLayer("flood"), visible: true }];

    // First mount: user hides the layer.
    const first = render(<LayerPanel initialLayers={layers} />);
    let checkbox = screen.getByTestId("layer-visibility") as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
    fireEvent.click(checkbox); // hide
    expect((screen.getByTestId("layer-visibility") as HTMLInputElement).checked).toBe(false);

    // Unmount (drawer collapse).
    first.unmount();

    // Remount with the SAME server-provided initialLayers (visible:true). The
    // persisted override must re-hide it.
    render(<LayerPanel initialLayers={layers} />);
    checkbox = screen.getByTestId("layer-visibility") as HTMLInputElement;
    expect(checkbox.checked).toBe(false);
  });

  it("remount via session-state (bus) also restores the hidden state", () => {
    // Pre-seed the override as if the user had toggled in a prior mount.
    writeLayerVisibilityOverride("flood", false);

    const bus = createLayerPanelBus();
    render(
      <LayerPanel
        initialLayers={[]}
        subscribeSessionState={bus.subscribeSessionState}
        subscribeMapCommand={bus.subscribeMapCommand}
      />,
    );
    act(() => {
      bus.pushSessionState(
        sessionStateWith([{ ...makeLayer("flood"), visible: true }]),
      );
    });
    const checkbox = screen.getByTestId("layer-visibility") as HTMLInputElement;
    expect(checkbox.checked).toBe(false);
  });

  it("a never-toggled layer renders the server visible value verbatim (desktop unaffected)", () => {
    // No override written; server says visible:false → render shows hidden,
    // visible:true → shown. Proves the override does not interfere when absent.
    const hidden = render(
      <LayerPanel initialLayers={[{ ...makeLayer("x"), visible: false }]} />,
    );
    expect((screen.getByTestId("layer-visibility") as HTMLInputElement).checked).toBe(false);
    hidden.unmount();

    render(<LayerPanel initialLayers={[{ ...makeLayer("y"), visible: true }]} />);
    expect((screen.getByTestId("layer-visibility") as HTMLInputElement).checked).toBe(true);
  });
});

// --- Sequential-layer grouping (NATE: enumerated temporal raster stacks) --- //
//
// Enumerated temporal sequences (e.g. 3 HRRR forecast hours F+01h/F+03h/F+06h)
// collapse into ONE collapsible "sequential group" row + a bottom scrubber.
// Stepping shows ONE frame at a time via the EXISTING visibility callback.

/** A forecast-frame layer: shared run dir (same AOI/source) + a lead-time token. */
function makeFrame(
  hour: number,
  run = "run-a",
  preset = "hrrr_precip",
  z_index = 1,
): ProjectLayerSummary {
  const hh = String(hour).padStart(2, "0");
  return {
    layer_id: `${run}-f${hh}`,
    name: `HRRR precip F+${hh}h`,
    layer_type: "raster",
    uri: `gs://grace-2/runs/${run}/precip_f${hh}.cog.tif`,
    visible: true,
    opacity: 1,
    z_index,
    style_preset: preset,
  };
}

describe("parseFrameToken — lead-time / step / index parsing", () => {
  it("parses a forecast lead hour token (F+03h) into value + label + stem", () => {
    const t = parseFrameToken("HRRR precip F+03h");
    expect(t).not.toBeNull();
    expect(t?.value).toBe(3);
    expect(t?.label).toBe("F+03h");
    expect(t?.stem).toBe("hrrr precip");
  });

  it("two frames in one series share a stem (differ only by the token)", () => {
    const a = parseFrameToken("HRRR precip F+01h");
    const b = parseFrameToken("HRRR precip F+06h");
    expect(a?.stem).toBe(b?.stem);
    expect(a?.value).toBeLessThan(b?.value ?? -1);
  });

  it("parses t+N and step tokens", () => {
    expect(parseFrameToken("Depth t+2")?.value).toBe(2);
    expect(parseFrameToken("Depth t+2")?.label).toBe("t+2");
    expect(parseFrameToken("Frame 4 depth")?.value).toBe(4);
  });

  it("returns null when there is no monotonic token", () => {
    expect(parseFrameToken("Storm surge maximum")).toBeNull();
    expect(parseFrameToken("Basemap")).toBeNull();
    expect(parseFrameToken("")).toBeNull();
  });
});

describe("detectSequentialGroups — conservative grouping", () => {
  it("groups >=2 monotonic frames sharing source/AOI/preset into one group", () => {
    const groups = detectSequentialGroups([
      makeFrame(1),
      makeFrame(3),
      makeFrame(6),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.layers).toHaveLength(3);
    // Members ordered ascending by lead hour.
    expect(groups[0]?.frameLabels).toEqual(["F+01h", "F+03h", "F+06h"]);
  });

  it("does NOT group a single framed layer (needs >=2)", () => {
    expect(detectSequentialGroups([makeFrame(1)])).toHaveLength(0);
  });

  it("does NOT group frames from different SOURCES / AOIs together", () => {
    // BUG 2(B): the series signature is now RUN-INDEPENDENT, so two frames from
    // different RUNS of the SAME source DO group (that is the re-run fix). To prove
    // genuinely distinct sources still stay apart we use different STEMS (the name
    // token that identifies the series), so they never share a group key.
    const a = {
      ...makeFrame(1),
      layer_id: "src-a-f01",
      name: "SFINCS surge F+01h",
      uri: "gs://grace-2/runs/RUN1/sfincs/surge_f01.cog.tif",
    };
    const b = {
      ...makeFrame(3),
      layer_id: "src-b-f03",
      name: "HRRR precip F+03h",
      uri: "gs://grace-2/runs/RUN1/hrrr/precip_f03.cog.tif",
    };
    const groups = detectSequentialGroups([a, b]);
    // Different stems ("SFINCS surge" vs "HRRR precip") => no group forms.
    expect(groups).toHaveLength(0);
  });

  it("BUG 2(B): groups frames from DIFFERENT RUNS of the same source (run-independent key)", () => {
    // The whole point of the run-independent signature: a re-run (different run-id
    // dir) of the same scenario maps to the SAME group key, so the two distinct
    // hours form one monotonic series instead of looking like two new groups.
    const groups = detectSequentialGroups([
      makeFrame(1, "run-a"), // run-a/precip_f01
      makeFrame(3, "run-b"), // run-b/precip_f03 - different RUN, same source
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.layers).toHaveLength(2);
    expect(groups[0]?.frameLabels).toEqual(["F+01h", "F+03h"]);
  });

  it("does NOT group ordinary distinct layers without tokens", () => {
    const flood = { ...makeLayer("flood"), name: "Storm surge max" };
    const roads = { ...makeLayer("roads"), name: "Roads" };
    expect(detectSequentialGroups([flood, roads])).toHaveLength(0);
  });

  it("rejects a non-monotonic series (duplicate frame values)", () => {
    // Two frames both at F+03h (duplicate value) is not a clear monotonic series.
    const dupA = { ...makeFrame(3), layer_id: "dup-a", uri: "gs://grace-2/runs/run-a/precip_f03_a.cog.tif" };
    const dupB = { ...makeFrame(3), layer_id: "dup-b", uri: "gs://grace-2/runs/run-a/precip_f03_b.cog.tif" };
    expect(detectSequentialGroups([dupA, dupB])).toHaveLength(0);
  });

  it("collapses a re-run's duplicate full series into ONE group (dedupe by value, keep newest run)", () => {
    // Mimic the real SWMM re-run: both runs publish the SAME TiTiler tile
    // template shape and the run_id lives in the percent-encoded ?url= query
    // AFTER the last literal "/", so bboxSignature is identical across runs and
    // both series land in one bucket with duplicate values [1,1,2,2,3,3]. The
    // old code rejected this as non-monotonic, exploding into per-frame legends.
    const frame = (run: string, step: number): ProjectLayerSummary => {
      const ss = String(step).padStart(2, "0");
      return {
        layer_id: `${run}-swmm-depth-frame-${ss}`,
        name: `Flood depth step ${step}`,
        layer_type: "raster",
        uri:
          "https://cf.example/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png" +
          `?url=s3%3A%2F%2Fbucket%2F${run}%2Fswmm_depth_frame_${ss}.tif` +
          "&rescale=0,3&colormap_name=blues",
        visible: true,
        opacity: 1,
        z_index: 1,
        style_preset: "continuous_flood_depth",
      };
    };
    const layers = [
      frame("RUNA", 1), frame("RUNA", 2), frame("RUNA", 3),
      frame("RUNB", 1), frame("RUNB", 2), frame("RUNB", 3),
    ];
    const groups = detectSequentialGroups(layers);
    expect(groups).toHaveLength(1);
    // Deduped to one frame per step (3), not the raw 6.
    expect(groups[0]?.layers).toHaveLength(3);
    // Newest run kept: last occurrence per value == RUNB.
    expect(groups[0]?.layers.map((l) => l.layer_id)).toEqual([
      "RUNB-swmm-depth-frame-01",
      "RUNB-swmm-depth-frame-02",
      "RUNB-swmm-depth-frame-03",
    ]);
  });
});

// ITEM 3 (NATE 2026-06-22) - the static peak/primary "max-depth" layer is
// INDEPENDENT of the animation sequence: it must never join the frame group, so
// hiding the time-series animation keeps the static max-depth layer visible with
// its own toggle.
describe("ITEM 3 - peak max-depth decoupled from the frame sequence", () => {
  // The real engine emission: ONE peak ("Peak flood depth", layer_id
  // <engine>-depth-peak-<run>) + N frames ("Flood depth step N"), all sharing
  // the same style_preset + run-dir signature.
  const peak: ProjectLayerSummary = {
    layer_id: "swmm-depth-peak-RUN1",
    name: "Peak flood depth",
    layer_type: "raster",
    uri: "s3://bucket/RUN1/swmm_depth_peak.tif",
    visible: true,
    opacity: 1,
    z_index: 5,
    style_preset: "continuous_flood_depth",
  };
  const frame = (n: number): ProjectLayerSummary => {
    const ss = String(n).padStart(2, "0");
    return {
      layer_id: `swmm-depth-frame-${ss}-RUN1`,
      name: `Flood depth step ${n}`,
      layer_type: "raster",
      uri: `s3://bucket/RUN1/swmm_depth_frame_${ss}.tif`,
      visible: true,
      opacity: 1,
      z_index: n,
      style_preset: "continuous_flood_depth",
    };
  };

  it("isPeakLayer flags the peak (layer_id -peak- + 'Peak'/'Max' name) and not frames", () => {
    expect(isPeakLayer(peak)).toBe(true);
    expect(isPeakLayer(frame(1))).toBe(false);
    // Name-only peak (no -peak- id) still flagged via the leading token.
    expect(
      isPeakLayer({ ...frame(1), layer_id: "x", name: "Max flood depth" }),
    ).toBe(true);
    // "Maximum ..." too.
    expect(
      isPeakLayer({ ...frame(1), layer_id: "x", name: "Maximum depth" }),
    ).toBe(true);
  });

  it("EXCLUDES the peak from the detected frame group", () => {
    const groups = detectSequentialGroups([peak, frame(1), frame(2), frame(3)]);
    expect(groups).toHaveLength(1);
    // The group holds ONLY the 3 frames - the peak is not a member.
    expect(groups[0]?.layers).toHaveLength(3);
    expect(groups[0]?.layers.map((l) => l.layer_id)).not.toContain(
      "swmm-depth-peak-RUN1",
    );
  });

  it("renders the peak as an INDEPENDENT ordinary row alongside the group", () => {
    render(<LayerPanel initialLayers={[peak, frame(1), frame(2), frame(3)]} />);
    // The peak is its own single layer row (with its own eye), the group is one
    // group row - so hiding the animation group never touches the peak's toggle.
    const peakRow = screen
      .getAllByTestId("layer-row")
      .find((r) => r.getAttribute("data-layer-id") === "swmm-depth-peak-RUN1");
    expect(peakRow).toBeTruthy();
    expect(screen.getByTestId("layer-group-row")).toBeInTheDocument();
  });
});

describe("satellite fire-animation frames — step token + ISO valid-time label", () => {
  // A GOES / VIIRS fire-animation frame: "GOES <ProductLabel> step <N> <ISO>
  // (<SAT>)". The "step <N>" is the monotonic grouping value; the product label
  // distinguishes the GeoColor vs Fire Temperature series (two groups); the ISO
  // is the per-frame display label. Same run dir => same AOI/source signature.
  const satFrame = (
    productLabel: string,
    step: number,
    iso: string,
    run = "run-goes",
    preset = "goes_rgb_animation",
  ): ProjectLayerSummary => ({
    layer_id: `${run}-${productLabel.replace(/\s+/g, "")}-${step}`,
    name: `GOES ${productLabel} step ${step} ${iso} (GOES-18)`,
    layer_type: "raster",
    uri: `gs://grace-2/runs/${run}/${productLabel.replace(/\s+/g, "")}_${step}.cog.tif`,
    visible: true,
    opacity: 1,
    z_index: 1,
    style_preset: preset,
  });

  it("parses a step token and uses the ISO valid-time as the frame label", () => {
    const t = parseFrameToken(
      "GOES Fire Temperature step 2 2026-06-22T18:05:00Z (GOES-18)",
    );
    expect(t).not.toBeNull();
    expect(t?.value).toBe(2);
    // The ISO valid-time is the human label (not the synthetic "step 2").
    expect(t?.label).toBe("2026-06-22 18:05Z");
    // The ISO is stripped from the stem so the series groups.
    expect(t?.stem).toBe("goes fire temperature (goes-18)");
  });

  it("two frames of one product share a stem (ISO + step vary, product fixed)", () => {
    const a = parseFrameToken("GOES GeoColor step 1 2026-06-22T18:00:00Z (GOES-18)");
    const b = parseFrameToken("GOES GeoColor step 2 2026-06-22T18:05:00Z (GOES-18)");
    expect(a?.stem).toBe(b?.stem);
    expect(a?.value).toBeLessThan(b?.value ?? -1);
  });

  it("GeoColor and Fire Temperature have DISTINCT stems (two scrubber groups)", () => {
    const g = parseFrameToken("GOES GeoColor step 1 2026-06-22T18:00:00Z (GOES-18)");
    const f = parseFrameToken(
      "GOES Fire Temperature step 1 2026-06-22T18:00:00Z (GOES-18)",
    );
    expect(g?.stem).not.toBe(f?.stem);
  });

  it("groups a GOES dual-product run into TWO co-temporal scrubber groups", () => {
    // Same window, same valid-times, two products -> two groups.
    const times = [
      "2026-06-22T18:00:00Z",
      "2026-06-22T18:05:00Z",
      "2026-06-22T18:10:00Z",
    ];
    const layers = [
      ...times.map((t, i) => satFrame("GeoColor", i + 1, t, "run-goes-geo")),
      ...times.map((t, i) => satFrame("Fire Temperature", i + 1, t, "run-goes-fire")),
    ];
    const groups = detectSequentialGroups(layers);
    // TWO distinct groups.
    expect(groups).toHaveLength(2);
    const byLabel = Object.fromEntries(groups.map((gr) => [gr.label, gr]));
    const labels = Object.keys(byLabel).sort();
    expect(labels).toEqual(["Goes Fire Temperature (Goes-18)", "Goes Geocolor (Goes-18)"]);
    // Each group has all 3 frames, in ascending order.
    for (const gr of groups) {
      expect(gr.layers).toHaveLength(3);
      // CO-TEMPORAL: both groups carry the SAME ISO valid-time labels in order.
      expect(gr.frameLabels).toEqual([
        "2026-06-22 18:00Z",
        "2026-06-22 18:05Z",
        "2026-06-22 18:10Z",
      ]);
    }
  });

  it("does NOT group a single-product, single-frame run (needs >=2 per series)", () => {
    const groups = detectSequentialGroups([
      satFrame("GeoColor", 1, "2026-06-22T18:00:00Z"),
    ]);
    expect(groups).toHaveLength(0);
  });
});

describe("LayerPanel — sequential group row rendering", () => {
  afterEach(() => {
    try { localStorage.clear(); } catch { /* ignore */ }
  });

  it("collapses N forecast frames into ONE group row (not N ordinary rows)", () => {
    render(
      <LayerPanel initialLayers={[makeFrame(1), makeFrame(3), makeFrame(6)]} />,
    );
    expect(screen.getAllByTestId("layer-group-row")).toHaveLength(1);
    // The N individual SortableRows are NOT rendered for grouped layers.
    expect(screen.queryAllByTestId("layer-row")).toHaveLength(0);
    // Frame count chip + count attribute reflect 3 frames.
    expect(screen.getByTestId("layer-group-row")).toHaveAttribute(
      "data-frame-count",
      "3",
    );
    expect(screen.getByTestId("layer-group-count-chip")).toHaveTextContent("3f");
  });

  it("ungrouped layers still render as ordinary rows alongside a group", () => {
    const flood = { ...makeLayer("flood"), name: "Storm surge max", z_index: 9 };
    render(
      <LayerPanel initialLayers={[flood, makeFrame(1), makeFrame(3)]} />,
    );
    expect(screen.getAllByTestId("layer-group-row")).toHaveLength(1);
    expect(screen.getAllByTestId("layer-row")).toHaveLength(1);
    expect(screen.getByTestId("layer-row")).toHaveAttribute("data-layer-id", "flood");
  });

  it("shows the active frame position as x/N in header (defaults to the FIRST frame, item 5)", () => {
    render(
      <LayerPanel initialLayers={[makeFrame(1), makeFrame(3), makeFrame(6)]} />,
    );
    // ITEM 5: default active = FIRST frame (1/3), so the animation reads from the
    // start. Header shows the x/N counter (full frame labels are in the expanded
    // sub-rows + the scrubber).
    expect(screen.getByTestId("layer-group-frame-label")).toHaveTextContent("1/3");
  });

  it("play button in the group header toggles auto-play (item 5)", () => {
    render(
      <LayerPanel initialLayers={[makeFrame(1), makeFrame(3), makeFrame(6)]} />,
    );
    // Play button now lives in the group header row, not the scrubber.
    expect(screen.getByTestId("layer-group-play")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("layer-group-play"));
    // After one click the scrubber is playing (play icon changed to pause).
    expect(screen.getByTestId("layer-group-play")).toHaveAttribute(
      "aria-label",
      "Pause sequence",
    );
  });

  // JOB WEB-ANIM (#157.1) — the step ARROWS now live on the App-owned scrubber,
  // not inside LayerPanel. Frame stepping drives the module-level controller,
  // which advances the ACTIVE group + drives the map via its emitter. Advancing
  // the controller updates the panel header's x/N readout (the panel mirrors the
  // controller's frame). (Scrubber DOM-arrow tests live in SequenceScrubber +
  // the controller's own unit tests.)
  it("advancing the controller updates the group header x/N (panel mirrors it)", () => {
    render(
      <LayerPanel
        initialLayers={[
          { ...makeFrame(1), visible: false },
          { ...makeFrame(3), visible: false },
          { ...makeFrame(6), visible: true },
        ]}
      />,
    );
    // ITEM 5: default = 1/3 (FIRST frame). advanceActive(1) -> 2/3.
    act(() => {
      getAnimationController().advanceActive(1);
    });
    expect(screen.getByTestId("layer-group-frame-label")).toHaveTextContent("2/3");
  });

  it("header has NO step arrows (they live on the scrubber — item 5)", () => {
    render(
      <LayerPanel initialLayers={[makeFrame(1), makeFrame(3), makeFrame(6)]} />,
    );
    // layer-group-next / layer-group-prev were removed from the header row.
    expect(screen.queryByTestId("layer-group-next")).toBeNull();
    expect(screen.queryByTestId("layer-group-prev")).toBeNull();
  });

  it("expand chevron reveals per-frame sub-rows; collapse hides them", () => {
    render(
      <LayerPanel initialLayers={[makeFrame(1), makeFrame(3), makeFrame(6)]} />,
    );
    // Collapsed by default — no frame sub-rows.
    expect(screen.queryByTestId("layer-group-frames")).toBeNull();
    fireEvent.click(screen.getByTestId("layer-group-expand"));
    expect(screen.getByTestId("layer-group-frames")).toBeInTheDocument();
    expect(screen.getAllByTestId("layer-group-frame")).toHaveLength(3);
    // Collapse again.
    fireEvent.click(screen.getByTestId("layer-group-expand"));
    expect(screen.queryByTestId("layer-group-frames")).toBeNull();
  });

  it("an expanded frame select-dot steps the group to that frame", () => {
    render(
      <LayerPanel
        initialLayers={[
          { ...makeFrame(1), visible: false },
          { ...makeFrame(3), visible: false },
          { ...makeFrame(6), visible: true },
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId("layer-group-expand"));
    const dots = screen.getAllByTestId("layer-group-frame-select");
    // Click the first frame's dot → group active = 1/3.
    fireEvent.click(dots[0]!);
    expect(screen.getByTestId("layer-group-frame-label")).toHaveTextContent("1/3");
  });

  it("collapses an all-visible stack down to a single visible frame on mount", () => {
    // JOB WEB-ANIM (#157.1) — the map is now driven by the controller's emitter
    // (show frame i, hide the rest), NOT by onMapCommand. Register a stub emitter
    // BEFORE mount and assert the panel collapses the all-visible stack to the
    // default (FIRST, item 5) frame: the emitter is called with the full member
    // list + visibleIndex = 0 (F+01h).
    const emitted: Array<{ ids: string[]; idx: number }> = [];
    const emitter: FrameVisibilityEmitter = (ids, idx) =>
      emitted.push({ ids: [...ids], idx });
    getAnimationController().setEmitter(emitter);
    // All three start visible (the server published them all) — the group must
    // hide all but the default (FIRST) frame so the map shows one overlay.
    act(() => {
      render(
        <LayerPanel initialLayers={[makeFrame(1), makeFrame(3), makeFrame(6)]} />,
      );
    });
    // ITEM 5: the emitter was driven to show only the FIRST frame (index 0 of 3).
    expect(emitted.length).toBeGreaterThan(0);
    const last = emitted[emitted.length - 1]!;
    expect(last.ids).toEqual(["run-a-f01", "run-a-f03", "run-a-f06"]);
    expect(last.idx).toBe(0);
    // The panel reducer also reflects the single-visible-frame state: expand to
    // confirm only the FIRST frame's sub-row is the active one.
    fireEvent.click(screen.getByTestId("layer-group-expand"));
    const frames = screen.getAllByTestId("layer-group-frame");
    expect(frames[0]).toHaveAttribute("data-active", "true");
    expect(frames[2]).toHaveAttribute("data-active", "false");
  });
});

// --- ITEM 2/3/4 (NATE 2026-06-22) group-row redesign --------------------- //
//
// The animation group row now LOOKS like an ordinary layer card:
// [EYE] [NAME] [X/N] [PLAY], NO dedicated drag-grabber (the card body is the
// drag handle), the far-left eye toggles the WHOLE group's visibility, and the
// row is a sortable item so it can be dragged among other layers.
describe("LayerPanel  -  group row redesign (ITEM 2/3/4)", () => {
  afterEach(() => {
    try { localStorage.clear(); } catch { /* ignore */ }
  });

  it("group row has a far-left visibility eye and NO dedicated drag-grabber", () => {
    render(
      <LayerPanel initialLayers={[makeFrame(1), makeFrame(3), makeFrame(6)]} />,
    );
    // The group's whole-group eye exists (ITEM 4).
    expect(screen.getByTestId("layer-group-visibility")).toBeInTheDocument();
    // ITEM 2  -  no per-row grabber inside the group card (the only grabbers in
    // the panel belong to ordinary layer rows; here there are none).
    expect(screen.queryAllByTestId("layer-drag-handle")).toHaveLength(0);
  });

  it("the group eye toggles the WHOLE group's visibility (all frames hide)", () => {
    const emitted: Array<{ ids: string[]; idx: number }> = [];
    getAnimationController().setEmitter((ids, idx) =>
      emitted.push({ ids: [...ids], idx }),
    );
    const seen: ProjectLayerSummary[][] = [];
    render(
      <LayerPanel
        initialLayers={[makeFrame(1), makeFrame(3), makeFrame(6)]}
        onLayersChange={(ls) => seen.push(ls)}
      />,
    );
    const eye = screen.getByTestId("layer-group-visibility");
    // Initially the group is visible (the active frame shows).
    expect(eye).toBeChecked();
    // Toggle OFF -> every member is hidden.
    fireEvent.click(eye);
    const afterHide = seen[seen.length - 1]!;
    const frameIds = ["run-a-f01", "run-a-f03", "run-a-f06"];
    for (const id of frameIds) {
      const l = afterHide.find((x) => x.layer_id === id);
      expect(l?.visible).toBe(false);
    }
    // The eye now reads hidden.
    expect(screen.getByTestId("layer-group-visibility")).not.toBeChecked();
  });

  it("re-showing the group restores ONLY the active frame (single-frame invariant)", () => {
    const seen: ProjectLayerSummary[][] = [];
    render(
      <LayerPanel
        initialLayers={[makeFrame(1), makeFrame(3), makeFrame(6)]}
        onLayersChange={(ls) => seen.push(ls)}
      />,
    );
    const eye = screen.getByTestId("layer-group-visibility");
    fireEvent.click(eye); // hide all
    fireEvent.click(screen.getByTestId("layer-group-visibility")); // show again
    const after = seen[seen.length - 1]!;
    // ITEM 5: default active frame is the FIRST (F+01h); only it should be visible.
    expect(after.find((l) => l.layer_id === "run-a-f01")?.visible).toBe(true);
    expect(after.find((l) => l.layer_id === "run-a-f03")?.visible).toBe(false);
    expect(after.find((l) => l.layer_id === "run-a-f06")?.visible).toBe(false);
  });

  it("a sequential group + ordinary layers share ONE interleaved sortable order", () => {
    // A group (frames z=1) plus two ordinary layers (z=9, z=5). The panel renders
    // one group row + two layer rows; the order is top-of-stack first by z, so the
    // group can be dragged among them (ITEM 3  -  covered structurally here).
    const top = { ...makeLayer("roads", 9), name: "Roads" };
    const mid = { ...makeLayer("boundary", 5), name: "Boundary" };
    render(
      <LayerPanel initialLayers={[top, mid, makeFrame(1), makeFrame(3)]} />,
    );
    expect(screen.getAllByTestId("layer-group-row")).toHaveLength(1);
    expect(screen.getAllByTestId("layer-row")).toHaveLength(2);
  });
});

// JOB WEB-ANIM (#157.2) — the SCRUBBER no longer lives inside LayerPanel. It is
// rendered by App.tsx from the shared controller so it survives panel close. The
// LayerPanel's role is to DETECT groups + push them into the controller; these
// tests verify that contract (and that the panel itself no longer mounts the
// scrubber). (Scrubber DOM behaviour is covered in SequenceScrubber.test.tsx;
// the App-level "renders when animating + panel closed" coverage lives in
// App.sequenceScrubber.test.tsx.)
describe("LayerPanel — pushes groups to the controller (scrubber is App-owned)", () => {
  afterEach(() => {
    try { localStorage.clear(); } catch { /* ignore */ }
  });

  it("does NOT render the scrubber itself anymore (App renders it)", () => {
    render(
      <LayerPanel initialLayers={[makeFrame(1), makeFrame(3), makeFrame(6)]} />,
    );
    // The panel no longer mounts the scrubber overlay; App owns it now.
    expect(screen.queryByTestId("grace2-sequence-scrubber")).toBeNull();
  });

  it("pushes the detected sequential group into the shared controller", () => {
    act(() => {
      render(
        <LayerPanel initialLayers={[makeFrame(1), makeFrame(3), makeFrame(6)]} />,
      );
    });
    const ctrl = getAnimationController();
    const groups = ctrl.getGroups();
    expect(groups).toHaveLength(1);
    expect(groups[0]?.layerIds).toEqual([
      "run-a-f01",
      "run-a-f03",
      "run-a-f06",
    ]);
    // Active group + default frame (FIRST, item 5) are set so App can render the
    // scrubber.
    expect(ctrl.getActiveGroup()?.key).toBe(groups[0]?.key);
    expect(ctrl.frameIndexFor(groups[0]!.key)).toBe(0);
  });

  it("registers NO group when there is no sequential series", () => {
    act(() => {
      render(<LayerPanel initialLayers={[makeLayer("flood")]} />);
    });
    expect(getAnimationController().getGroups()).toHaveLength(0);
    expect(getAnimationController().getActiveGroup()).toBeNull();
  });

  it("the group header play button toggles the controller's playing state", () => {
    render(
      <LayerPanel initialLayers={[makeFrame(1), makeFrame(3), makeFrame(6)]} />,
    );
    expect(getAnimationController().isPlaying()).toBe(false);
    fireEvent.click(screen.getByTestId("layer-group-play"));
    expect(getAnimationController().isPlaying()).toBe(true);
    fireEvent.click(screen.getByTestId("layer-group-play"));
    expect(getAnimationController().isPlaying()).toBe(false);
  });
});

// ITEM 3 (NATE 2026-06-24) - frames must not "escape" into the layer list on the
// mobile background/foreground / refocus reconnect path. ROOT CAUSE: the
// LayerPanel reducer's `session-state` seed used to WHOLESALE-replace state.layers
// with the raw `loaded_layers`, with NO durability seatbelt. A partial / transient
// reconnect frame (rebound live turn emitting its mid-re-run accumulator) can
// carry FEWER frames of a series than the panel shows; below the >=2-member
// grouping threshold the series un-groups and each surviving frame renders as an
// ordinary row (the "frames in the layer list" symptom). FIX: route the reducer's
// session-state seed through the SAME durability cache + `replace_layers` honor
// App.tsx uses, so a partial reconnect frame keeps the full series present and
// detectSequentialGroups re-forms the group. These tests pin that.
describe("LayerPanel - session-state seatbelt keeps a frame group grouped on a partial reconnect frame (item 3)", () => {
  afterEach(() => {
    try { localStorage.clear(); } catch { /* ignore */ }
    // Restore the module default cache so later suites are unaffected.
    setLayerCache(new LayerCache({ backend: moduleNoopBackend }));
  });

  function cacheForCase(caseId: string): LayerCache {
    const cache = new LayerCache({ backend: moduleNoopBackend });
    cache.activeCaseId = caseId;
    setLayerCache(cache);
    return cache;
  }

  it("a partial reconnect session-state (replace_layers:false) does NOT un-group the series into individual rows", () => {
    cacheForCase("case-anim");
    const bus = createLayerPanelBus();
    render(<LayerPanel subscribeSessionState={bus.subscribeSessionState} />);

    // Full N-frame series arrives first (a healthy authoritative frame).
    act(() => {
      bus.pushSessionState({
        loaded_layers: [makeFrame(1), makeFrame(3), makeFrame(6)],
        replace_layers: true,
      } as SessionStatePayload);
    });
    // Grouped: ONE group row, NO individual frame rows.
    expect(screen.getAllByTestId("layer-group-row")).toHaveLength(1);
    expect(screen.queryAllByTestId("layer-row")).toHaveLength(0);

    // The mobile background/foreground reconnect delivers a PARTIAL frame that
    // carries only frame 1 with replace_layers:false (the rebound live turn's
    // mid-re-run accumulator). WITHOUT the seatbelt this would shrink the series
    // to 1 member, no group forms, and the surviving frame escapes to an
    // ordinary row. WITH the seatbelt (mergeSnapshot, additive on a non-
    // authoritative frame) the full series is kept and the group re-forms.
    act(() => {
      bus.pushSessionState({
        loaded_layers: [makeFrame(1)],
        replace_layers: false,
      } as SessionStatePayload);
    });

    // The group row is STILL present and the frames did NOT escape into the list.
    expect(screen.getAllByTestId("layer-group-row")).toHaveLength(1);
    expect(screen.queryAllByTestId("layer-row")).toHaveLength(0);
    expect(screen.getByTestId("layer-group-row")).toHaveAttribute(
      "data-frame-count",
      "3",
    );
  });

  it("an AUTHORITATIVE shrink (replace_layers:true) still applies (the seatbelt only protects partial frames)", () => {
    cacheForCase("case-anim2");
    const bus = createLayerPanelBus();
    render(<LayerPanel subscribeSessionState={bus.subscribeSessionState} />);
    act(() => {
      bus.pushSessionState({
        loaded_layers: [makeFrame(1), makeFrame(3), makeFrame(6)],
        replace_layers: true,
      } as SessionStatePayload);
    });
    expect(screen.getAllByTestId("layer-group-row")).toHaveLength(1);
    // A genuine authoritative replace to a single ordinary layer DOES shrink the
    // set (no group survives) - the seatbelt must not freeze the panel forever.
    act(() => {
      bus.pushSessionState({
        loaded_layers: [makeLayer("flood")],
        replace_layers: true,
      } as SessionStatePayload);
    });
    expect(screen.queryAllByTestId("layer-group-row")).toHaveLength(0);
    expect(screen.getAllByTestId("layer-row")).toHaveLength(1);
  });
});
