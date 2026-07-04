// GRACE-2 web - COLD-OPEN render reproduction (LIVE BUG NATE 2026-06-22).
//
// THE BUG: box ASLEEP -> open Case "100-year Flash Flood Ellicott City"
// (01KVQ53K4K9RRDAEXWA3EX0Y10, 26 layers in DynamoDB AND its case-view S3
// snapshot) -> the map shows the EMPTY-state "No layers loaded yet. Ask the
// assistant to add data." (App.tsx:1532) i.e. ZERO of the 26 layers reached the
// App layer state. The data, signer, and CDN are all proven correct, so the
// drop is in the WEB cold-open render pipeline (or the user is on a stale
// bundle).
//
// THIS TEST is the most faithful reproduction of the cold-open render pipeline
// that runs in happy-dom (the full <App/> mounts maplibre/WebGL which happy-dom
// cannot run - the established App.test.tsx / App.impactEnvelope.test.tsx /
// App.coldViewAuth.test.tsx pattern is a minimal shell that wires the REAL
// modules under test). The harness here wires the EXACT cold-open pipeline:
//
//   1. The REAL `useCases` hook (hooks/useCases.ts) - its real onCaseOpen sets
//      activeSession + activeCaseId, exactly as the live WS case-open AND the
//      App cold-load effect (App.tsx:1155 useCases_onCaseOpen(payload)) do.
//   2. The REAL shared LayerCache (lib/layer_cache.ts) - the cache.activeCaseId
//      lockstep + mergeSnapshot + the #158 empty-frame guard.
//   3. The REAL LayerPanelBus (createLayerPanelBus from ./LayerPanel).
//   4. App.tsx's THREE relevant effects, COPIED VERBATIM (line refs noted):
//        a. the layerCache.activeCaseId lockstep effect (App.tsx ~896-925),
//           keyed [activeCaseId, layerCache];
//        b. the Case rehydration replay effect (App.tsx ~986-1087) - the
//           `bus.pushSessionState({loaded_layers, replace_layers:true})` push,
//           keyed [activeSession, bus];
//        c. the bus subscriber (App.tsx ~1259-1271) that reads
//           layerCache.activeCaseId, mergeSnapshot()s, and setLayers(merged).
//   5. The REAL LayerPanel mounted on the same bus, plus the App empty-state
//      text gated on `layers.length > 0` (App.tsx:1532), reproduced verbatim.
//
// The fixture is the REAL saved snapshot. We feed it through
// useCases.onCaseOpen (the cold-load entry) with the socket NOT connected and
// assert how many of the 26 layers reach the App layer state / LayerPanel.
// EXPECT 26; the bug = fewer (likely 0) + the empty-state text present.

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { render, screen, act, cleanup, waitFor } from "@testing-library/react";
import { useEffect, useMemo, useRef, useState } from "react";

import { createLayerPanelBus, LayerPanel } from "./LayerPanel";
import { resolveBboxProgress } from "./lib/bbox_progress";
import type { ScreenRect } from "./lib/legend_snap";
import {
  LayerCache,
  setLayerCache,
  getLayerCache,
} from "./lib/layer_cache";
import { LS_ACTIVE_CASE, useCases } from "./hooks/useCases";
import type {
  CaseOpenEnvelopePayload,
  MapCommandPayload,
  ProjectLayerSummary,
} from "./contracts";

// The REAL saved cold snapshot: 1 vector (buildings, inline_geojson present)
// + 25 raster flood-depth-frame-NN (https CloudFront TiTiler tile templates).
import coldSnapshot from "./__fixtures__/ellicott_cold_snapshot.json";

const COLD_PAYLOAD = coldSnapshot as unknown as CaseOpenEnvelopePayload;
const EXPECTED_LAYERS = 26;
const EMPTY_STATE_TEXT = "No layers loaded yet. Ask the assistant to add data.";

// ── In-memory override backend so the real LayerCache never touches IndexedDB
// (happy-dom has none; the default backend already no-ops, but inject one so
// the cache is deterministic + isolated per test). ──────────────────────── //
function memBackend() {
  return {
    async load() {
      return {};
    },
    async save() {
      /* no-op */
    },
  };
}

// ── Harness: App.tsx's cold-open render pipeline, the three effects verbatim.
// `activeSession === null` (the App socket is NOT connected, no live session
// has round-tripped) so the cold-load is what feeds onCaseOpen. ──────────── //
function ColdOpenHarness({
  onLayers,
}: {
  onLayers: (layers: ProjectLayerSummary[]) => void;
}): JSX.Element {
  const bus = useRef(createLayerPanelBus()).current;
  const layerCache = useRef(getLayerCache()).current;

  // The REAL useCases hook. sendCaseCommand is a no-op (the box is asleep - the
  // WS select only QUEUES; the cold-load drives onCaseOpen directly), exactly
  // like App when disconnected.
  const cases = useCases({
    sendCaseCommand: () => {
      /* no-op: box asleep, select would only queue */
    },
    isSignedIn: true,
  });
  const { activeSession, activeCaseId, onCaseOpen, selectCase, clearActive } =
    cases;

  // App's `layers` state (App.tsx:270) + the empty-state gate (App.tsx:1532).
  const [layers, setLayers] = useState<ProjectLayerSummary[]>([]);
  useEffect(() => {
    onLayers(layers);
  }, [layers, onLayers]);

  // BUG 2 harness - the AOI bbox overlay anchor (App.tsx:315 aoiScreenRect). The
  // test arms it directly via __test_setAoi to stand in for MapView's
  // onAoiScreenRectChange; the exit-to-root effect below must clear it.
  const [aoiScreenRect, setAoiScreenRect] = useState<ScreenRect | null>(null);

  // (a) layerCache.activeCaseId lockstep effect (App.tsx ~896-925), VERBATIM
  // (only the WS/scrubber side effects that need maplibre are dropped - they
  // are irrelevant to the layer-drop and don't run in this disconnected path).
  // BUG 2 - the exit-to-root clear (App.tsx ~1080) is reproduced verbatim.
  const activeCaseIdRef = useRef<string | null>(null);
  useEffect(() => {
    const prevCaseId = activeCaseIdRef.current;
    activeCaseIdRef.current = activeCaseId;
    if (prevCaseId !== null && prevCaseId !== activeCaseId) {
      layerCache.evictCase(prevCaseId);
    }
    // BUG 2 (NATE 2026-06-23) - exit-to-root is a CLEAR SLATE.
    if (activeCaseId === null) {
      setAoiScreenRect(null);
      setLayers([]);
    }
    layerCache.activeCaseId = activeCaseId;
  }, [activeCaseId, layerCache]);

  // (b) Case rehydration replay effect (App.tsx ~986-1087): the layer push.
  useEffect(() => {
    if (activeSession === null) {
      bus.pushSessionState({
        loaded_layers: [],
        chat_history: [],
        pipeline_history: [],
        current_pipeline: null,
        map_view: null,
        replace_layers: true,
      });
      return;
    }
    bus.pushSessionState({
      loaded_layers: activeSession.loaded_layers ?? [],
      chat_history: activeSession.chat_history ?? [],
      pipeline_history: activeSession.pipeline_history ?? [],
      current_pipeline: activeSession.current_pipeline ?? null,
      map_view: null,
      replace_layers: true,
    } as unknown as Parameters<typeof bus.pushSessionState>[0]);
  }, [activeSession, bus]);

  // (c) bus subscriber (App.tsx ~1259-1271), VERBATIM.
  useEffect(() => {
    const unsub = bus.subscribeSessionState((p) => {
      const incoming = p.loaded_layers ?? [];
      const authoritativeReplace =
        (p as { replace_layers?: boolean }).replace_layers !== false;
      const caseId = layerCache.activeCaseId;
      const merged = layerCache.mergeSnapshot(caseId, incoming, {
        authoritativeReplace,
      });
      setLayers(merged);
    });
    return unsub;
  }, [bus, layerCache]);

  // BUG 1 (NATE 2026-06-23) - the loading-scan settledness derivation, VERBATIM
  // from App.tsx (~1533). caseSelectedButUnsettled is FALSE once the ACTIVE
  // Case's layers are present (layers.length > 0), so the loading scan clears even
  // if the WS session-settle signal lags (the same-bbox switch / cold-view case).
  // activeSession.case.case_id matching is left out of this harness's settle
  // signal on purpose: useCases sets activeSession on cold-open, so to exercise
  // the "session lags but layers present" path we drive layers directly and keep
  // the session-match term TRUE-by-default (unsettled) via a forced flag.
  const sessionSettled =
    activeSession !== null && activeSession.case.case_id === activeCaseId;
  const caseSelectedButUnsettled =
    activeCaseId !== null && layers.length === 0 && !sessionSettled;
  const layersLoading = useMemo(
    () => activeCaseId !== null && caseSelectedButUnsettled,
    [activeCaseId, caseSelectedButUnsettled],
  );
  const bboxProgress = useMemo(
    () =>
      resolveBboxProgress({
        hasBbox: aoiScreenRect !== null,
        layerCount: layers.length,
        layersLoading,
        connecting: false,
        simRunning: false,
        animationsEnabled: true,
      }),
    [aoiScreenRect, layers.length, layersLoading],
  );

  // Expose the cold-load entry (App.tsx:1155 useCases_onCaseOpen(payload))
  // AND the user-tap entry (App.tsx:308 selectCase -> setActiveCaseId first).
  useEffect(() => {
    (window as unknown as Record<string, unknown>).__test_coldOpen = (
      p: CaseOpenEnvelopePayload,
    ) => onCaseOpen(p);
    (window as unknown as Record<string, unknown>).__test_selectCase = (
      id: string,
    ) => selectCase(id);
    (window as unknown as Record<string, unknown>).__test_setAoi = (
      r: ScreenRect | null,
    ) => setAoiScreenRect(r);
    (window as unknown as Record<string, unknown>).__test_clearActive = () =>
      clearActive();
    return () => {
      delete (window as unknown as Record<string, unknown>).__test_coldOpen;
      delete (window as unknown as Record<string, unknown>).__test_selectCase;
      delete (window as unknown as Record<string, unknown>).__test_setAoi;
      delete (window as unknown as Record<string, unknown>).__test_clearActive;
    };
  }, [onCaseOpen, selectCase, clearActive]);

  return (
    <div>
      <div data-testid="app-layer-count">{layers.length}</div>
      <div data-testid="app-bbox-mode">{bboxProgress.mode}</div>
      <div data-testid="app-has-aoi">{aoiScreenRect ? "yes" : "no"}</div>
      {/* ACTIVE-CASE RESTORE (NATE 2026-06-26) - App keys CasesPanel vs CaseView
          PURELY on activeCaseId===null (App.tsx). Reflect that branch so the
          reload-restore test can assert the open Case is RESTORED (case-view)
          on mount from a seeded localStorage key, not dropped to the list. */}
      <div data-testid="app-view">
        {activeCaseId === null ? "cases-panel" : "case-view"}
      </div>
      {/* The App empty-state, gated on layers.length (App.tsx:1532). */}
      {layers.length === 0 && <div>{EMPTY_STATE_TEXT}</div>}
      {/* The REAL LayerPanel on the same bus (App.tsx:1571 wiring). */}
      {layers.length > 0 && (
        <LayerPanel
          subscribeSessionState={bus.subscribeSessionState}
          onMapCommand={(c: MapCommandPayload) => bus.pushMapCommand(c)}
        />
      )}
    </div>
  );
}

let lastLayers: ProjectLayerSummary[] = [];

beforeEach(() => {
  // Fresh, isolated real LayerCache per test (in-memory backend, no IndexedDB).
  setLayerCache(new LayerCache({ maxCases: 4, backend: memBackend() }));
  lastLayers = [];
  // ACTIVE-CASE RESTORE (NATE 2026-06-26) - useCases now SEEDS activeCaseId from
  // localStorage (LS_ACTIVE_CASE). Clear it so the existing cold-open tests
  // start from the no-restore (null active Case) baseline; the restore test
  // seeds it explicitly.
  try {
    localStorage.clear();
  } catch {
    /* ignore */
  }
});

afterEach(() => {
  cleanup();
  delete (window as unknown as Record<string, unknown>).__test_coldOpen;
});

describe("cold-open render pipeline (LIVE BUG 2026-06-22 repro)", () => {
  it("sanity: the fixture really carries 26 loaded_layers (1 vector + 25 raster)", () => {
    const ll = COLD_PAYLOAD.session_state?.loaded_layers ?? [];
    expect(ll.length).toBe(EXPECTED_LAYERS);
    const vectors = ll.filter((l) => l.layer_type === "vector");
    const rasters = ll.filter((l) => l.layer_type === "raster");
    expect(vectors.length).toBe(1);
    expect(rasters.length).toBe(25);
  });

  it("feeding the real cold snapshot through the cold-open pipeline reaches all 26 layers", async () => {
    render(
      <ColdOpenHarness
        onLayers={(l) => {
          lastLayers = l;
        }}
      />,
    );

    // Before the cold-open: no session -> empty-state present, zero layers.
    expect(screen.getByTestId("app-layer-count").textContent).toBe("0");
    expect(screen.queryByText(EMPTY_STATE_TEXT)).not.toBeNull();

    // Drive the EXACT cold-load entry: useCases.onCaseOpen(payload).
    await act(async () => {
      (
        window as unknown as {
          __test_coldOpen: (p: CaseOpenEnvelopePayload) => void;
        }
      ).__test_coldOpen(COLD_PAYLOAD);
    });

    // The bug: fewer than 26 (likely 0) reach the App layer state, leaving the
    // empty-state text. The fix: all 26 reach setLayers and the panel mounts.
    await waitFor(() => {
      expect(screen.getByTestId("app-layer-count").textContent).toBe(
        String(EXPECTED_LAYERS),
      );
    });

    expect(lastLayers.length).toBe(EXPECTED_LAYERS);
    // The empty-state must be GONE once layers arrive (App.tsx:1532 gate).
    expect(screen.queryByText(EMPTY_STATE_TEXT)).toBeNull();
    // Both kinds survived: the 1 vector + all 25 raster frames.
    expect(lastLayers.filter((l) => l.layer_type === "vector").length).toBe(1);
    expect(lastLayers.filter((l) => l.layer_type === "raster").length).toBe(25);
  });

  it("two-phase cold-open (tap selects case FIRST, cold-load arrives later) reaches all 26", async () => {
    // The REAL mobile sequence: user taps the case while the box is asleep, so
    // selectCase(id) sets activeCaseId LOCALLY (App.tsx:308) and the WS select
    // merely queues. A render later the cold-load's onCaseOpen arrives with the
    // session. This is the exact two-render ordering the prime-suspect race
    // (layerCache.activeCaseId stale when the rehydration push fires) would hit.
    const caseId = COLD_PAYLOAD.session_state!.case.case_id;

    render(
      <ColdOpenHarness
        onLayers={(l) => {
          lastLayers = l;
        }}
      />,
    );

    // Phase 1: tap -> selectCase sets activeCaseId; no session yet.
    await act(async () => {
      (
        window as unknown as { __test_selectCase: (id: string) => void }
      ).__test_selectCase(caseId);
    });
    // Still empty (no session has painted; the box is asleep).
    expect(screen.getByTestId("app-layer-count").textContent).toBe("0");

    // Phase 2: the cold-load resolves and feeds the snapshot through onCaseOpen.
    await act(async () => {
      (
        window as unknown as {
          __test_coldOpen: (p: CaseOpenEnvelopePayload) => void;
        }
      ).__test_coldOpen(COLD_PAYLOAD);
    });

    await waitFor(() => {
      expect(screen.getByTestId("app-layer-count").textContent).toBe(
        String(EXPECTED_LAYERS),
      );
    });
    expect(lastLayers.length).toBe(EXPECTED_LAYERS);
    expect(screen.queryByText(EMPTY_STATE_TEXT)).toBeNull();
  });
});

// ── Direct pipeline unit: the prime-suspect ordering race in isolation. ──── //
// Replays the bus subscriber's exact logic (App.tsx:1259-1271) against the real
// shared LayerCache to prove what happens when layerCache.activeCaseId is STALE
// (null) at the instant the 26-layer authoritative push fires - the failure
// mode the bug context names. mergeSnapshot(null, ...) passes the list through
// verbatim (root behavior), so even a stale-null caseId does NOT drop layers;
// and once the caseId is correctly set the merge tracks all 26.
describe("mergeSnapshot under a stale/correct activeCaseId (race isolation)", () => {
  const incoming = COLD_PAYLOAD.session_state!.loaded_layers ?? [];

  function subscriberMerge(
    cache: LayerCache,
    layers: ProjectLayerSummary[],
  ): ProjectLayerSummary[] {
    // App.tsx:1259-1271 verbatim, replace_layers:true (authoritative).
    const authoritativeReplace = true;
    const caseId = cache.activeCaseId;
    return cache.mergeSnapshot(caseId, layers, { authoritativeReplace });
  }

  it("STALE null activeCaseId: pass-through keeps all 26 (no drop)", () => {
    const cache = new LayerCache({ maxCases: 4, backend: memBackend() });
    cache.activeCaseId = null; // the race: lockstep effect hasn't run yet.
    const merged = subscriberMerge(cache, incoming);
    expect(merged.length).toBe(EXPECTED_LAYERS);
  });

  it("CORRECT activeCaseId set first: all 26 tracked under the case", () => {
    const cache = new LayerCache({ maxCases: 4, backend: memBackend() });
    cache.activeCaseId = COLD_PAYLOAD.session_state!.case.case_id;
    const merged = subscriberMerge(cache, incoming);
    expect(merged.length).toBe(EXPECTED_LAYERS);
    expect(cache.layersFor(cache.activeCaseId).length).toBe(EXPECTED_LAYERS);
  });
});

// ── BUG 1 (NATE 2026-06-23): the loading SCAN must clear once the active Case's
// layers are PRESENT, even if the WS session-settle signal lags (same-bbox
// switch / cold-view case). resolveBboxProgress maps layersLoading -> a scan
// over already-loaded layers; the App settledness fix makes layersLoading FALSE
// the instant layers.length > 0, so the scan stops. ──────────────────────── //
const setAoi = (r: ScreenRect | null): void =>
  (window as unknown as { __test_setAoi: (r: ScreenRect | null) => void })
    .__test_setAoi(r);
const clearActiveCase = (): void =>
  (window as unknown as { __test_clearActive: () => void }).__test_clearActive();
const coldOpen = (p: CaseOpenEnvelopePayload): void =>
  (window as unknown as { __test_coldOpen: (p: CaseOpenEnvelopePayload) => void })
    .__test_coldOpen(p);

describe("BUG 1 - loading scan clears when the active Case's layers are present", () => {
  it("with an AOI armed but ZERO layers, the bbox overlay shows a FILL scan (loading)", async () => {
    render(<ColdOpenHarness onLayers={(l) => (lastLayers = l)} />);
    // Arm the AOI + select a case (activeCaseId set, no session yet -> unsettled).
    await act(async () => {
      (
        window as unknown as { __test_selectCase: (id: string) => void }
      ).__test_selectCase(COLD_PAYLOAD.session_state!.case.case_id);
      setAoi({ left: 100, top: 100, right: 300, bottom: 200 });
    });
    // Unsettled + zero layers -> loading -> FILL shimmer (first fetch).
    expect(screen.getByTestId("app-bbox-mode").textContent).toBe("fill");
  });

  it("once the layers PAINT, the loading scan CLEARS (mode none) even before session-settle", async () => {
    render(<ColdOpenHarness onLayers={(l) => (lastLayers = l)} />);
    await act(async () => {
      setAoi({ left: 100, top: 100, right: 300, bottom: 200 });
      coldOpen(COLD_PAYLOAD);
    });
    await waitFor(() => {
      expect(screen.getByTestId("app-layer-count").textContent).toBe(
        String(EXPECTED_LAYERS),
      );
    });
    // Layers present -> caseSelectedButUnsettled FALSE -> layersLoading FALSE ->
    // resolveBboxProgress returns "none": NO scan running over loaded layers.
    expect(screen.getByTestId("app-bbox-mode").textContent).toBe("none");
  });
});

// ── BUG 2 (NATE 2026-06-23): exit-to-root is a CLEAR SLATE - the AOI bbox
// overlay anchor (aoiScreenRect) AND the layers must both clear when
// activeCaseId becomes null, so nothing lingers on the Cases root. ───────── //
describe("BUG 2 - exit-to-root clears the AOI overlay and the layers", () => {
  it("clearActive() drops aoiScreenRect + layers so the Cases root is blank", async () => {
    render(<ColdOpenHarness onLayers={(l) => (lastLayers = l)} />);
    // Open a case with layers + an armed AOI overlay.
    await act(async () => {
      setAoi({ left: 100, top: 100, right: 300, bottom: 200 });
      coldOpen(COLD_PAYLOAD);
    });
    await waitFor(() => {
      expect(screen.getByTestId("app-layer-count").textContent).toBe(
        String(EXPECTED_LAYERS),
      );
    });
    expect(screen.getByTestId("app-has-aoi").textContent).toBe("yes");

    // Exit to the Cases root.
    await act(async () => {
      clearActiveCase();
    });

    // CLEAR SLATE: no AOI overlay anchor, no layers, no bbox scan on the root.
    await waitFor(() => {
      expect(screen.getByTestId("app-has-aoi").textContent).toBe("no");
    });
    expect(screen.getByTestId("app-layer-count").textContent).toBe("0");
    expect(screen.getByTestId("app-bbox-mode").textContent).toBe("none");
  });
});

// ── ACTIVE-CASE RESTORE (NATE 2026-06-26): on RELOAD (felt most on mobile) the
// app must STAY in the open Case (CaseView), not drop to the Cases LIST. App
// keys CasesPanel vs CaseView PURELY on activeCaseId===null; useCases now SEEDS
// activeCaseId from a persisted localStorage key (LS_ACTIVE_CASE) so a fresh
// mount (a reload) restores the open Case before any WS round-trip. ───────── //
describe("ACTIVE-CASE RESTORE - reload stays in the open Case (not the list)", () => {
  it("a seeded localStorage active-Case id restores CaseView on mount (simulated reload)", () => {
    // Simulate the prior session having left a Case open: the persisted key is
    // present at the instant the app (re)mounts, exactly as after a reload.
    const restoredId = COLD_PAYLOAD.session_state!.case.case_id;
    localStorage.setItem(LS_ACTIVE_CASE, restoredId);

    render(<ColdOpenHarness onLayers={(l) => (lastLayers = l)} />);

    // The hook seeded activeCaseId from localStorage on mount -> App renders
    // CaseView, NOT the Cases list. This is the whole bug: without the seed the
    // view would be "cases-panel" (activeCaseId === null) after every reload.
    expect(screen.getByTestId("app-view").textContent).toBe("case-view");
  });

  it("with NO persisted id the app mounts to the Cases list (baseline unchanged)", () => {
    // No LS_ACTIVE_CASE key (cleared in beforeEach) -> activeCaseId null -> the
    // Cases list, proving the restore is opt-in on a persisted id only.
    render(<ColdOpenHarness onLayers={(l) => (lastLayers = l)} />);
    expect(screen.getByTestId("app-view").textContent).toBe("cases-panel");
  });
});

// ── DOUBLE-REFRESH FIX (TASK A, NATE 2026-06-26): box-off a Case loads FOREVER
// and only a 2nd reload shows layers. ROOT: the cold case-VIEW effect set its
// one-shot guard SYNCHRONOUSLY before an async fetch and depended on RAW
// wsStatus; box-off the WS flaps connecting<->reconnecting every ~10s, each flip
// tearing the effect down -> cancelling the in-flight fetch -> re-running ->
// bailing on the latched guard (which the cancel branch did NOT release) so the
// fetch never completed and onCaseOpen never fired. The fix:
//   1. depend on a COARSE `notConnected` boolean so connecting<->reconnecting
//      flaps do NOT re-run/cancel the in-flight fetch;
//   2. release the guard on EVERY cancel + only latch it on SUCCESS;
//   3. a cold-settle / attempt-resolved signal so layersLoading stops spinning.
// This harness reproduces the FIXED cold-VIEW fetch effect VERBATIM (the App.tsx
// effect body) wired to the REAL useCases hook, driven by a DEFERRED cold-load
// so the test can flip wsStatus connecting->reconnecting MID-FETCH. ────────── //
type DeferredColdLoad = {
  promise: Promise<CaseOpenEnvelopePayload | null>;
  resolve: (p: CaseOpenEnvelopePayload | null) => void;
};
function makeDeferred(): DeferredColdLoad {
  let resolve!: (p: CaseOpenEnvelopePayload | null) => void;
  const promise = new Promise<CaseOpenEnvelopePayload | null>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

type WsStatusLike = "connecting" | "connected" | "disconnected" | "reconnecting";

function ColdViewFetchHarness({
  wsStatus,
  coldLoad,
  onLayers,
  // COLD-VIEW GATE FIX (NATE 2026-06-28): the durable "this Case HAD layers"
  // signal (the CaseSummary.layer_summary count box-off). When > 0 AND the cold
  // resolve is empty/null, App renders the honest restore stub instead of the
  // bare "add layers" empty. Defaults to 0 (a genuinely-new/empty Case).
  durableLayerCount = 0,
  // The 12s cold-settle bound (coldSettleTimedOut). The harness exposes a window
  // hook to fire it deterministically (the real App uses a 12s setTimeout).
}: {
  wsStatus: WsStatusLike;
  // The deferred cold-load stands in for fetchCaseView: the test resolves it
  // AFTER flipping wsStatus to prove a mid-fetch flap does not cancel it.
  coldLoad: () => Promise<CaseOpenEnvelopePayload | null>;
  onLayers: (layers: ProjectLayerSummary[]) => void;
  durableLayerCount?: number;
}): JSX.Element {
  const bus = useRef(createLayerPanelBus()).current;
  const layerCache = useRef(getLayerCache()).current;

  const cases = useCases({
    sendCaseCommand: () => {
      /* no-op: box asleep */
    },
    isSignedIn: true,
  });
  const { activeSession, activeCaseId, onCaseOpen, selectCase } = cases;

  const [layers, setLayers] = useState<ProjectLayerSummary[]>([]);
  useEffect(() => {
    onLayers(layers);
  }, [layers, onLayers]);

  // (a) layerCache.activeCaseId lockstep (App.tsx) - verbatim-minimal.
  const activeCaseIdRef = useRef<string | null>(null);
  useEffect(() => {
    const prevCaseId = activeCaseIdRef.current;
    activeCaseIdRef.current = activeCaseId;
    if (prevCaseId !== null && prevCaseId !== activeCaseId) {
      layerCache.evictCase(prevCaseId);
    }
    if (activeCaseId === null) setLayers([]);
    layerCache.activeCaseId = activeCaseId;
  }, [activeCaseId, layerCache]);

  // (b) Case rehydration replay effect (App.tsx) - the layer push.
  useEffect(() => {
    if (activeSession === null) {
      bus.pushSessionState({
        loaded_layers: [],
        chat_history: [],
        pipeline_history: [],
        current_pipeline: null,
        map_view: null,
        replace_layers: true,
      });
      return;
    }
    bus.pushSessionState({
      loaded_layers: activeSession.loaded_layers ?? [],
      chat_history: activeSession.chat_history ?? [],
      pipeline_history: activeSession.pipeline_history ?? [],
      current_pipeline: activeSession.current_pipeline ?? null,
      map_view: null,
      replace_layers: true,
    } as unknown as Parameters<typeof bus.pushSessionState>[0]);
  }, [activeSession, bus]);

  // (c) bus subscriber (App.tsx) - verbatim.
  useEffect(() => {
    const unsub = bus.subscribeSessionState((p) => {
      const incoming = p.loaded_layers ?? [];
      const authoritativeReplace =
        (p as { replace_layers?: boolean }).replace_layers !== false;
      const caseId = layerCache.activeCaseId;
      const merged = layerCache.mergeSnapshot(caseId, incoming, {
        authoritativeReplace,
      });
      setLayers(merged);
    });
    return unsub;
  }, [bus, layerCache]);

  // ── The FIXED cold-VIEW fetch effect (App.tsx ~1430), reproduced. ──────── //
  const coldLoadedCaseRef = useRef<string | null>(null);
  const [coldViewAttemptedCaseId, setColdViewAttemptedCaseId] = useState<
    string | null
  >(null);
  // (1) reset-on-connected in its OWN effect keyed on wsStatus===connected.
  useEffect(() => {
    if (wsStatus === "connected") coldLoadedCaseRef.current = null;
  }, [wsStatus]);
  // (1) depend on the COARSE notConnected boolean, NOT raw wsStatus.
  const notConnected = wsStatus !== "connected";
  useEffect(() => {
    if (!notConnected) return;
    if (activeCaseId === null) return;
    if (activeSession && activeSession.case.case_id === activeCaseId) return;
    if (coldLoadedCaseRef.current === activeCaseId) return;
    let cancelled = false;
    void (async () => {
      const payload = await coldLoad();
      if (cancelled) {
        // (2) ALWAYS release the guard on cancel so the next arm re-fetches.
        coldLoadedCaseRef.current = null;
        return;
      }
      // COLD-VIEW GATE FIX (NATE 2026-06-28): a null result is TRANSIENT - do
      // NOT latch coldViewAttemptedCaseId here; release the guard so the next
      // cold attempt re-fetches (the view self-heals without a manual refresh).
      if (payload === null) {
        coldLoadedCaseRef.current = null;
        return;
      }
      // DEFINITIVE RESOLUTION (non-null payload): mark resolved + latch + feed.
      setColdViewAttemptedCaseId(activeCaseId);
      coldLoadedCaseRef.current = activeCaseId;
      onCaseOpen(payload);
      bus.pushCaseOpen(payload);
    })();
    return () => {
      cancelled = true;
    };
  }, [activeCaseId, notConnected, activeSession, onCaseOpen, bus, coldLoad]);

  // ── The FIXED 12s cold-settle bound (App.tsx), reproduced as a window-fired
  // flag so a test can trip it deterministically (the real App uses a 12s
  // setTimeout). It STOPS the spinner box-off so it never hangs forever. ──── //
  const [coldSettleTimedOut, setColdSettleTimedOut] = useState(false);
  useEffect(() => {
    setColdSettleTimedOut(false);
  }, [activeCaseId]);

  // ── The FIXED layersLoading derivation (App.tsx), reproduced. ──────────── //
  const caseSelectedButUnsettled =
    activeCaseId !== null &&
    layers.length === 0 &&
    (activeSession === null || activeSession.case.case_id !== activeCaseId);
  const coldViewSettledForCase =
    activeCaseId !== null && coldViewAttemptedCaseId === activeCaseId;
  const layersLoading = useMemo(() => {
    if (activeCaseId === null) return false;
    const coldDone = coldViewSettledForCase || coldSettleTimedOut;
    if (caseSelectedButUnsettled && !coldDone) return true;
    if (!coldDone && (wsStatus === "connecting" || wsStatus === "reconnecting"))
      return true;
    return false;
  }, [
    activeCaseId,
    caseSelectedButUnsettled,
    wsStatus,
    coldViewSettledForCase,
    coldSettleTimedOut,
  ]);

  // ── The FIXED restore-stub gate (App.tsx): the Case HAD layers (durable
  // count > 0) but cold resolved empty + the spinner stopped -> honest stub. ─ //
  const caseHadLayers = durableLayerCount > 0;
  const showRestoreLayersStub =
    !layersLoading && layers.length === 0 && caseHadLayers;

  useEffect(() => {
    (window as unknown as Record<string, unknown>).__test_selectCase = (
      id: string,
    ) => selectCase(id);
    (window as unknown as Record<string, unknown>).__test_fireColdSettle = () =>
      setColdSettleTimedOut(true);
    return () => {
      delete (window as unknown as Record<string, unknown>).__test_selectCase;
      delete (window as unknown as Record<string, unknown>).__test_fireColdSettle;
    };
  }, [selectCase]);

  return (
    <div>
      <div data-testid="app-layer-count">{layers.length}</div>
      <div data-testid="app-layers-loading">{layersLoading ? "yes" : "no"}</div>
      {/* App's three-way empty/loading/restore split, reproduced. */}
      {layers.length === 0 &&
        (layersLoading ? null : showRestoreLayersStub ? (
          <div data-testid="grace2-case-view-restore-layers">
            Wake the agent to restore this Case's layers.
          </div>
        ) : (
          <div data-testid="grace2-case-view-empty-layers">{EMPTY_STATE_TEXT}</div>
        ))}
    </div>
  );
}

describe("TASK A - cold-VIEW survives a mid-fetch connecting->reconnecting flap", () => {
  it("a wsStatus flap MID-FETCH does NOT cancel the cold-load: onCaseOpen fires + all 26 layers reach setLayers + the spinner clears on the FIRST load", async () => {
    const deferred = makeDeferred();
    let lastLayers2: ProjectLayerSummary[] = [];

    const { rerender } = render(
      <ColdViewFetchHarness
        wsStatus="connecting"
        coldLoad={() => deferred.promise}
        onLayers={(l) => (lastLayers2 = l)}
      />,
    );

    // Select the Case while disconnected (box asleep) - the cold-VIEW effect
    // arms and AWAITS the deferred cold-load.
    await act(async () => {
      (
        window as unknown as { __test_selectCase: (id: string) => void }
      ).__test_selectCase(COLD_PAYLOAD.session_state!.case.case_id);
    });

    // Pre-resolve: zero layers, the spinner is up (genuine pre-paint loading).
    expect(screen.getByTestId("app-layer-count").textContent).toBe("0");
    expect(screen.getByTestId("app-layers-loading").textContent).toBe("yes");

    // THE FLAP: wsStatus connecting->reconnecting MID-FETCH (the deferred
    // cold-load has NOT resolved yet). With the OLD raw-wsStatus dep this tore
    // the effect down + cancelled + latched the guard forever. With the coarse
    // notConnected dep the effect does NOT re-run, so the in-flight fetch lives.
    await act(async () => {
      rerender(
        <ColdViewFetchHarness
          wsStatus="reconnecting"
          coldLoad={() => deferred.promise}
          onLayers={(l) => (lastLayers2 = l)}
        />,
      );
    });

    // Now resolve the cold-load (as the real fetch would, post-flap).
    await act(async () => {
      deferred.resolve(COLD_PAYLOAD);
      await deferred.promise;
    });

    // FIRST LOAD paints all 26 layers (no 2nd reload needed) ...
    await waitFor(() => {
      expect(screen.getByTestId("app-layer-count").textContent).toBe(
        String(EXPECTED_LAYERS),
      );
    });
    expect(lastLayers2.length).toBe(EXPECTED_LAYERS);
    expect(screen.queryByText(EMPTY_STATE_TEXT)).toBeNull();
    // ... and the spinner CLEARS even though wsStatus is still "reconnecting"
    // box-off (the attempt resolved -> stop forcing the spinner on the flap).
    await waitFor(() => {
      expect(screen.getByTestId("app-layers-loading").textContent).toBe("no");
    });
  });

  it("a no-snapshot cold-load (null) box-off is BOUNDED by the 12s cold-settle timer (not latched by the null itself)", async () => {
    const deferred = makeDeferred();
    render(
      <ColdViewFetchHarness
        wsStatus="reconnecting"
        coldLoad={() => deferred.promise}
        onLayers={() => {}}
      />,
    );
    await act(async () => {
      (
        window as unknown as { __test_selectCase: (id: string) => void }
      ).__test_selectCase(COLD_PAYLOAD.session_state!.case.case_id);
    });
    // Spinner up while the attempt is in flight.
    expect(screen.getByTestId("app-layers-loading").textContent).toBe("yes");
    // Resolve to NULL (no snapshot). COLD-VIEW GATE FIX: a null is TRANSIENT and
    // does NOT latch the attempt, so the spinner is still up (the view stays
    // retryable). It is the 12s coldSettleTimedOut bound that finally stops it.
    await act(async () => {
      deferred.resolve(null);
      await deferred.promise;
    });
    expect(screen.getByTestId("app-layers-loading").textContent).toBe("yes");
    // The 12s bound fires (box-off the WS never connects) -> spinner CLEARS so it
    // never hangs forever, falling through to the honest empty stub.
    await act(async () => {
      (
        window as unknown as { __test_fireColdSettle: () => void }
      ).__test_fireColdSettle();
    });
    await waitFor(() => {
      expect(screen.getByTestId("app-layers-loading").textContent).toBe("no");
    });
    expect(screen.getByTestId("app-layer-count").textContent).toBe("0");
    // No durable layer count -> a genuinely-empty Case still shows "add layers".
    expect(screen.queryByText(EMPTY_STATE_TEXT)).not.toBeNull();
    expect(screen.queryByTestId("grace2-case-view-restore-layers")).toBeNull();
  });
});

// ── COLD-VIEW GATE FIX (NATE 2026-06-28): a MODFLOW Case reopened box-asleep
// showed the bare "add layers" empty with no spinner; a manual refresh then
// painted it. ROOT: the cold-view path latched coldViewAttemptedCaseId on a
// TRANSIENT null BEFORE distinguishing it from a genuinely-empty case, so the
// spinner dropped and the bare empty rendered even for a Case that HAS persisted
// layers. The fix: (a) a null/abort/error stays RETRYABLE (no latch) so the view
// self-heals on the next cold attempt without a refresh; (b) for a Case the
// durable summary says HAD layers, render an honest "wake the agent" stub. ──── //
describe("COLD-VIEW GATE - transient null stays retryable + honest restore stub", () => {
  it("a Case with a DURABLE layer count whose cold resolves null shows the RESTORE stub (not the bare empty) once the spinner stops", async () => {
    const deferred = makeDeferred();
    render(
      <ColdViewFetchHarness
        wsStatus="reconnecting"
        coldLoad={() => deferred.promise}
        onLayers={() => {}}
        durableLayerCount={26}
      />,
    );
    await act(async () => {
      (
        window as unknown as { __test_selectCase: (id: string) => void }
      ).__test_selectCase(COLD_PAYLOAD.session_state!.case.case_id);
    });
    // Spinner up; the null resolves but does NOT latch (still retryable).
    await act(async () => {
      deferred.resolve(null);
      await deferred.promise;
    });
    expect(screen.getByTestId("app-layers-loading").textContent).toBe("yes");
    // The 12s bound stops the spinner. Because the Case HAD layers (durable
    // count 26) but cold resolved zero, the HONEST restore stub renders - NOT
    // the bare "add layers" empty (the bug).
    await act(async () => {
      (
        window as unknown as { __test_fireColdSettle: () => void }
      ).__test_fireColdSettle();
    });
    await waitFor(() => {
      expect(screen.queryByTestId("grace2-case-view-restore-layers")).not.toBeNull();
    });
    expect(screen.queryByText(EMPTY_STATE_TEXT)).toBeNull();
  });

  it("a transient null is RETRYABLE: a later cold attempt with the real snapshot self-heals to all 26 layers WITHOUT a manual refresh", async () => {
    // First arm: the cold-load resolves null (transient - box still waking).
    let attempt = 0;
    const firstDeferred = makeDeferred();
    const secondDeferred = makeDeferred();
    const coldLoad = (): Promise<CaseOpenEnvelopePayload | null> => {
      attempt += 1;
      return attempt === 1 ? firstDeferred.promise : secondDeferred.promise;
    };
    let lastLayers3: ProjectLayerSummary[] = [];

    const { rerender } = render(
      <ColdViewFetchHarness
        wsStatus="reconnecting"
        coldLoad={coldLoad}
        onLayers={(l) => (lastLayers3 = l)}
        durableLayerCount={26}
      />,
    );
    await act(async () => {
      (
        window as unknown as { __test_selectCase: (id: string) => void }
      ).__test_selectCase(COLD_PAYLOAD.session_state!.case.case_id);
    });
    // First attempt resolves NULL -> NOT latched (guard released, retryable).
    await act(async () => {
      firstDeferred.resolve(null);
      await firstDeferred.promise;
    });
    expect(screen.getByTestId("app-layer-count").textContent).toBe("0");

    // A connection change re-arms the cold-VIEW effect. The guard was already
    // RELEASED by the transient null (the whole point of the fix), so a fresh
    // disconnected episode (here: a brief "connected" that resets the guard,
    // then back to "reconnecting") fires a SECOND cold attempt - the self-heal
    // the OLD latch prevented (which forced a manual refresh). The first null
    // left the guard released, so this re-arm is allowed.
    await act(async () => {
      rerender(
        <ColdViewFetchHarness
          wsStatus="connected"
          coldLoad={coldLoad}
          onLayers={(l) => (lastLayers3 = l)}
          durableLayerCount={26}
        />,
      );
    });
    await act(async () => {
      rerender(
        <ColdViewFetchHarness
          wsStatus="reconnecting"
          coldLoad={coldLoad}
          onLayers={(l) => (lastLayers3 = l)}
          durableLayerCount={26}
        />,
      );
    });
    // Second attempt resolves the REAL snapshot -> all 26 layers paint.
    await act(async () => {
      secondDeferred.resolve(COLD_PAYLOAD);
      await secondDeferred.promise;
    });
    await waitFor(() => {
      expect(screen.getByTestId("app-layer-count").textContent).toBe(
        String(EXPECTED_LAYERS),
      );
    });
    expect(lastLayers3.length).toBe(EXPECTED_LAYERS);
    // It self-healed: more than one cold attempt was made (no refresh needed).
    expect(attempt).toBeGreaterThan(1);
    // No empty / restore stub once the layers are present.
    expect(screen.queryByText(EMPTY_STATE_TEXT)).toBeNull();
    expect(screen.queryByTestId("grace2-case-view-restore-layers")).toBeNull();
  });

  it("a GENUINELY-EMPTY Case (no durable layers) that cold-resolves an empty payload still shows the 'add layers' empty, NOT the restore stub", async () => {
    // The cold-load returns a VALID payload whose session_state has ZERO layers -
    // a real new/empty case. This is a DEFINITIVE resolution (non-null), so it
    // latches normally and the spinner stops; with no durable layer count the
    // honest empty (not the restore stub) renders.
    const emptyPayload = {
      ...COLD_PAYLOAD,
      session_state: {
        ...COLD_PAYLOAD.session_state!,
        loaded_layers: [],
      },
    } as unknown as CaseOpenEnvelopePayload;
    const deferred = makeDeferred();
    render(
      <ColdViewFetchHarness
        wsStatus="reconnecting"
        coldLoad={() => deferred.promise}
        onLayers={() => {}}
        durableLayerCount={0}
      />,
    );
    await act(async () => {
      (
        window as unknown as { __test_selectCase: (id: string) => void }
      ).__test_selectCase(COLD_PAYLOAD.session_state!.case.case_id);
    });
    await act(async () => {
      deferred.resolve(emptyPayload);
      await deferred.promise;
    });
    // Definitive resolution latches the attempt -> spinner stops.
    await waitFor(() => {
      expect(screen.getByTestId("app-layers-loading").textContent).toBe("no");
    });
    expect(screen.getByTestId("app-layer-count").textContent).toBe("0");
    // Genuinely empty -> the "add layers" empty, NOT the restore stub.
    expect(screen.queryByText(EMPTY_STATE_TEXT)).not.toBeNull();
    expect(screen.queryByTestId("grace2-case-view-restore-layers")).toBeNull();
  });
});

// ── TASK C (NATE 2026-06-26): EXITING/RELOADING a Case shows ONLY that ONE
// case; the rest vanish. ROOT: on reload activeCaseId is seeded non-null, the
// cold case-VIEW effect runs FIRST and OPTIMISTICALLY UPSERTS the single
// restored case into cases[]; the cold case-LIST effect then bailed on its old
// `if (cases.length > 0) return` guard -> the authoritative full /case-list was
// NEVER fetched. The fix gates on `casesSettled` (flips only on a real
// onCaseList frame; an onCaseOpen upsert does NOT set it), so the cold
// /case-list ALWAYS runs once even after a restored-case upsert. This harness
// reproduces the FIXED cold-LIST guard against the REAL useCases hook. ─────── //
function ColdListGuardHarness({
  onListFetched,
}: {
  onListFetched: () => void;
}): JSX.Element {
  const cases = useCases({
    sendCaseCommand: () => {
      /* no-op: box asleep */
    },
    isSignedIn: true,
  });
  const {
    cases: caseList,
    casesSettled,
    onCaseOpen,
    onCaseList,
    selectCase,
  } = cases;

  // The FIXED cold-LIST guard (App.tsx): gate on casesSettled, NOT cases.length.
  const coldLoadedListIdRef = useRef<string | null>(null);
  useEffect(() => {
    // Box asleep (never connected) so the reset-on-connected branch is moot.
    if (coldLoadedListIdRef.current === "signed-in") return;
    // TASK C: an optimistic cold-VIEW upsert leaves cases.length > 0 but
    // casesSettled FALSE, so the OLD guard bailed and this NEVER ran. The fixed
    // guard runs because casesSettled is still false.
    if (casesSettled) return;
    coldLoadedListIdRef.current = "signed-in";
    // Stand in for fetchCaseList -> useCases_onCaseList(payload, true): the FULL
    // authoritative list of THREE cases (incl. the restored one).
    onListFetched();
    onCaseList(
      {
        envelope_type: "case-list",
        cases: [
          { case_id: COLD_PAYLOAD.session_state!.case.case_id, title: "Restored" },
          { case_id: "CASE_B", title: "Case B" },
          { case_id: "CASE_C", title: "Case C" },
        ],
      } as never,
      true,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [casesSettled, onCaseList, onListFetched]);

  // Expose the restored-case optimistic upsert (the cold-VIEW path on reload).
  useEffect(() => {
    (window as unknown as Record<string, unknown>).__test_restoreUpsert = () =>
      onCaseOpen(COLD_PAYLOAD);
    (window as unknown as Record<string, unknown>).__test_selectCase = (
      id: string,
    ) => selectCase(id);
    return () => {
      delete (window as unknown as Record<string, unknown>).__test_restoreUpsert;
      delete (window as unknown as Record<string, unknown>).__test_selectCase;
    };
  }, [onCaseOpen, selectCase]);

  return <div data-testid="rail-count">{caseList.length}</div>;
}

describe("TASK C - cold /case-list runs even after a restored-case cold-VIEW upsert", () => {
  it("box-off reload: a seeded restored-case upsert does NOT suppress the cold /case-list -> the rail shows ALL cases, not just the restored one", async () => {
    let listFetched = 0;
    render(<ColdListGuardHarness onListFetched={() => (listFetched += 1)} />);

    // Reload sequence: the cold case-VIEW optimistically upserts the ONE
    // restored case into cases[] BEFORE any authoritative list arrives.
    await act(async () => {
      (
        window as unknown as { __test_restoreUpsert: () => void }
      ).__test_restoreUpsert();
    });

    // The cold /case-list STILL runs (the bug: it did not) ...
    await waitFor(() => expect(listFetched).toBeGreaterThan(0));
    // ... and the rail now shows ALL THREE cases, not just the restored one.
    await waitFor(() => {
      expect(screen.getByTestId("rail-count").textContent).toBe("3");
    });
  });
});
