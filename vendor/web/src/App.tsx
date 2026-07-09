// GRACE-2 web  -  top-level shell.
//
// job-0143 layout (sprint-12-mega Wave 4):
//
//   +-----------------------------------------------------------+
//   |  [- Layers] (TL hamburger, when left hidden)              |
//   |                                            [- Chat] (TR)  |
//   |                                                           |
//   |   Left rail (CasesPanel when no active Case,              |
//   |    CaseView with breadcrumb + LayerPanel when one is      |
//   |    selected)                                              |
//   |                                                           |
//   |   ...                                                     |
//   |                                                           |
//   |   [- Settings]   - bottom-row pill (Secrets now inside it)|
//   |                       Map (full bleed)                    |
//   |              [LayerLegend anchored to AOI bbox] (Map.tsx) |
//   +-----------------------------------------------------------+
//
// Restructure from job-0137 / Wave 3:
//   - When no Case is active, the left rail shows CasesPanel ONLY (no
//     LayerPanel  -  layers are a per-Case construct).
//   - When a Case is active, the left rail shows CaseView (breadcrumb +
//     LayerPanel embedded). Clicking the breadcrumb arrow deselects the
//     Case and returns to the Cases list; the map resets to CONUS.
//   - The [Settings] [Secrets] bottom-row pills replace the previous
//     bottom-left - key icon. Each opens a full-screen overlay popup.
//   - The top-right identity chip (auth/sign-out) is REMOVED; auth lives
//     in the Settings popup now.
//   - Anonymous "Sign in to save" copy is now triggered only at save
//     attempts via useSaveGate, not blanket-rendered.
//   - MapLibre navigation controls move to TOP-LEFT (under the
//     leftCollapsed hamburger)  -  Map.tsx owns the addControl call.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { MapView, type MapCommandSubscribeFunc, type MapTheme } from "./Map";
import { Chat, readChatWidth, clampSheetHeight } from "./Chat";
import { LayerPanel, createLayerPanelBus, readLayersWidth } from "./LayerPanel";
import { getLayerCache } from "./lib/layer_cache";
// JOB WEB-ANIM (#157.2-.3)  -  the floating sequence scrubber now lives at the App
// level (not inside LayerPanel) so it renders WHENEVER a sequence is animating on
// the shared AnimationController, regardless of whether the Layers panel is open.
import { SequenceScrubber } from "./components/SequenceScrubber";
// Item b (NATE 2026-06-20)  -  the MOBILE legend show/hide toggle lives inside the
// expanded Layers section (off the chat composer); legendHasContent gates it.
import { MobileLegendToggle, legendHasContent } from "./components/LayerLegend";
import { getAnimationController } from "./lib/animation_controller";
import { useAnimationState } from "./lib/use_animation_controller";
import type { ScreenRect } from "./lib/legend_snap";
// NATE map/loading-UX polish item 1 - the AOI-bbox loading animation overlay +
// its pure state machine / settings persistence. Driven from App's live
// loading / connection / sim signals; anchored to the projected AOI rect.
import { BboxProgressOverlay } from "./components/BboxProgressOverlay";
import {
  resolveBboxProgress,
  readBboxAnimationsEnabled,
  isPipelineRunning,
} from "./lib/bbox_progress";
// "3D terrain viz" first cut - the persisted 3D-terrain + contour enable flags.
import { readTerrain3dEnabled, readContoursEnabled } from "./lib/terrain_3d";
import {
  AuthGate,
  clearAnonymousAccepted,
  readAnonymousAccepted,
} from "./components/AuthGate";
import { AuthGuard } from "./components/AuthGuard";
import { CasesPanel } from "./components/CasesPanel";
import { CaseView } from "./components/CaseView";
import { SettingsPopup } from "./components/SettingsPopup";
import { ToolsCatalogPopup } from "./components/ToolsCatalogPopup";
import {
  RoutingQualityDashboard,
  type RoutingDashboardSummary,
} from "./components/RoutingQualityDashboard";
import {
  ImpactPanel,
  type ImpactEnvelope,
} from "./components/ImpactPanel";
import type { ChartPayload } from "./components/ChartStack";
import { BottomRowButtons } from "./components/BottomRowButtons";
import { SaveGateModal } from "./components/SaveGateModal";
import {
  SourceSuggestionAction,
  SourceSuggestionInline,
} from "./components/SourceSuggestionInline";
// FIX 2 (NATE 2026-06-17): the large-payload warning moved into Chat's per-Case
// interleaved stream (in-chat card), so App no longer imports / renders
// PayloadWarningInline. See Chat.tsx routePayloadWarning + InterleavedChatStream.
import {
  AuthUser,
  onAuthChanged,
  signOut as authSignOut,
  signIn as authSignIn,
  handleRedirectCallback,
  getIdToken,
} from "./auth";
import { ConnectionStatus, GraceWs } from "./ws";
import { SourceCandidatePayload } from "./lib/source_suggestion_suppression";
import { extractLastZoomTo, asBbox } from "./lib/case_zoom";
import { useCases } from "./hooks/useCases";
import { useIsMobile } from "./hooks/useIsMobile";
import { useSaveGate } from "./hooks/useSaveGate";
import {
  MobileDrawer,
  MobileDrawerButton,
} from "./components/MobileDrawer";
import { IconMenu, IconSettings } from "./components/icons";
import { AgentWaker, wakeConfigured, WakeState } from "./lib/wake";
import { isLocalDeployment } from "./lib/deployment";
import { fetchCaseView, caseViewConfigured } from "./lib/case_view";
import { fetchCaseList, caseListConfigured } from "./lib/case_list";
import {
  CaseListEnvelopePayload,
  CaseOpenEnvelopePayload,
  MapCommandPayload,
  // PayloadWarningEnvelopePayload retained for the dev-only window seam typing
  // (FIX 2: the warning is rendered by Chat now, not App).
  PayloadWarningEnvelopePayload,
  PipelineStatePayload,
  ProjectLayerSummary,
  ProviderID,
  SecretRecord,
  SecretsListPayload,
  SessionStatePayload,
} from "./contracts";

// sleep/wake STAGE 2 (NATE 2026-06-18)  -  number of CONSECUTIVE failed reconnect
// schedules before we run the REPORT-ONLY wakeState() GET probe (which classifies
// asleep and may surface the composer Wake UI). The first failed attempt is
// usually a transient WS blip while the box is still UP (CloudFront idle cull,
// brief network drop)  -  probing then would be noise. By the SECOND consecutive
// failure the box is plausibly stopped, so we GET-probe its state. NEVER
// AUTO-WAKE: the probe is read-only; only the user's explicit composer tap POSTs
// wake (StartInstances).
const WAKE_OVERLAY_THRESHOLD = 2;

// localStorage keys for panel collapse state (job-0065).
const LS_LEFT_COLLAPSED = "grace2.leftPanelCollapsed";
const LS_RIGHT_COLLAPSED = "grace2.rightPanelCollapsed";
// localStorage key for map theme (job-0076).
const LS_THEME = "grace2.theme";

// MOBILE SHEET-TOP DOCK (NATE 2026-06-24) - the COLLAPSED mobile chat sheet
// (drag handle + composer card) has an "auto" CSS height, so we estimate its
// on-screen height with one shared constant when computing where the sheet's
// TOP edge sits for the overlay dock. This replaces the two divergent
// fixed-clearance guesses the SequenceScrubber (116) and the LayerLegend pill
// (96) each carried; ~100px covers the collapsed handle + single-line composer
// card. The device safe-area inset is reserved by the overlays' own CSS calc
// fallback, so this is a pure layout-px estimate. The sheet's EXPANDED height
// is the user-dragged vh (clampSheetHeight), so no estimate is needed there.
const COLLAPSED_SHEET_PX = 100;
// Fallback expanded sheet height (vh) before Chat reports its real geometry -
// matches Chat's SHEET_HEIGHT_DEFAULT_VH so the first sheetTopPx is sane.
const SHEET_HEIGHT_FALLBACK_VH = 70;

function readTheme(): MapTheme {
  try {
    const v = localStorage.getItem(LS_THEME);
    return v === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

function readCollapsed(key: string): boolean {
  try {
    return localStorage.getItem(key) === "true";
  } catch {
    return false;
  }
}

// WebSocket endpoint  -  local agent (job-0015) on port 8765.
// Override at build time with VITE_GRACE2_WS_URL. job-0275: the default now
// derives the host from the page's own hostname (same pattern as the tool
// catalog HTTP endpoint), so the SAME dev build works from localhost, the
// LAN, or a tailnet  -  phones hitting http://<host>:5173 reach the agent at
// ws://<host>:8765 instead of dialing their own localhost.
const WS_URL: string =
  (import.meta.env.VITE_GRACE2_WS_URL as string | undefined) ??
  (typeof window !== "undefined" && window.location?.hostname
    ? `ws://${window.location.hostname}:8765`
    : "ws://localhost:8765");

declare global {
  interface Window {
    __grace2InjectSessionState?: (p: SessionStatePayload) => void;
    __grace2InjectMapCommand?: (p: MapCommandPayload) => void;
    /** Dev seam for pipeline-state; wired by Chat.tsx via its GraceWs handler. */
    __grace2InjectPipelineState?: (p: PipelineStatePayload) => void;
    /** Dev seam for error (job-0166); wired by Chat.tsx via its GraceWs handler. */
    __grace2InjectError?: (p: import("./contracts").ErrorPayload) => void;
    /** Dev seam for secrets-list (job-0125); wired by App.tsx GraceWs handler. */
    __grace2InjectSecretsList?: (p: SecretsListPayload) => void;
    /** Dev seam for source-suggestion (job-0126 -> renamed job-0145); wired by App.tsx GraceWs handler. */
    __grace2InjectSourceSuggestion?: (p: SourceCandidatePayload) => void;
    /** Dev seam for case-list (job-0137); wired by App.tsx GraceWs handler. */
    __grace2InjectCaseList?: (p: CaseListEnvelopePayload) => void;
    /** Dev seam for case-open (job-0137); wired by App.tsx GraceWs handler. */
    __grace2InjectCaseOpen?: (p: CaseOpenEnvelopePayload) => void;
    /** Dev seam for payload-warning (job-0140); wired by App.tsx GraceWs handler. */
    __grace2InjectPayloadWarning?: (p: PayloadWarningEnvelopePayload) => void;
    /**
     * Dev seam for ImpactEnvelope panel (Wave 4.11 P4). Tests + Playwright
     * UI-driver pass a full ImpactEnvelope to surface the side panel.
     */
    __grace2InjectImpactEnvelope?: (p: ImpactEnvelope | null) => void;
    /**
     * Dev seam for chart-emission (sprint-13 job-0231). Playwright / tests
     * inject a ChartPayload to surface the inline stacked preview + gallery
     * without driving a live agent. Mirrors __grace2InjectImpactEnvelope.
     */
    __grace2InjectChartEmission?: (p: ChartPayload) => void;
    /**
     * Dev seam to reset charts (sprint-13 job-0231). Lets Playwright clear
     * the accumulated chart list between test scenarios.
     */
    __grace2ClearCharts?: () => void;
  }
}

// Shared hamburger button style (job-0068). Same-side-as-panel per user direction.
// z-index 30 so it renders above panels (z=20) and legend (z=10).
// job-0283  -  desktop sleekness: hairline border + 10px radius + blur so the
// hamburgers sit in the same surface family as the rail panels/pills.
// Desktop-only (mobile uses MobileDrawerButton).
const hamburgerBtnStyle: React.CSSProperties = {
  position: "absolute",
  background: "rgba(18,19,24,0.92)",
  border: "1px solid rgba(255,255,255,0.08)",
  borderRadius: 10,
  boxShadow: "0 2px 12px rgba(0,0,0,0.35)",
  backdropFilter: "blur(6px)",
  WebkitBackdropFilter: "blur(6px)",
  color: "#cfd4db",
  width: 40,
  height: 40,
  padding: 0,
  cursor: "pointer",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 18,
  zIndex: 30,
  lineHeight: 1,
  top: 12,
};

// Session-durability Job E (NATE) - the layer-panel LOADING stub spins via the
// `grace2-spin` keyframes. PipelineCard.tsx defines the same keyframes, but it
// is NOT statically imported by App.tsx, so its module-load injection is not
// guaranteed to have run when the stub paints (a Case can open before any
// pipeline card mounts). Inject a self-contained, idempotent copy at module
// load (distinct style-element id; the rule is identical so a duplicate from
// PipelineCard is harmless). SSR/test guard: no-op when `document` is absent.
const GRACE2_APP_SPIN_KEYFRAMES_ID = "grace2-app-spin-keyframes";
function ensureAppSpinKeyframes(): void {
  if (typeof document === "undefined") return;
  if (document.getElementById(GRACE2_APP_SPIN_KEYFRAMES_ID)) return;
  const style = document.createElement("style");
  style.id = GRACE2_APP_SPIN_KEYFRAMES_ID;
  style.textContent = `
@keyframes grace2-spin {
  0%   { transform: rotate(0deg); }
  100% { transform: rotate(360deg); }
}
`;
  document.head.appendChild(style);
}
ensureAppSpinKeyframes();

/**
 * FLASH FIX (Lane 1a): structural equality of two layer lists by the fields the
 * renderer reads, ORDER-INSENSITIVE (keyed by layer_id) so an equal-set reorder
 * (the cache returns layers in Map-insertion order; a re-publish can shuffle
 * equal-z layers) can never defeat the skip-setState guard. Used by App to bail
 * out of a `setLayers` when an identical session-state heartbeat arrives.
 */
function layerListsEqual(
  a: ProjectLayerSummary[],
  b: ProjectLayerSummary[],
): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  const byId = new Map<string, ProjectLayerSummary>();
  for (const l of a) byId.set(l.layer_id, l);
  for (const y of b) {
    const x = byId.get(y.layer_id);
    if (!x) return false;
    if (
      x.visible !== y.visible ||
      x.opacity !== y.opacity ||
      x.z_index !== y.z_index ||
      x.name !== y.name ||
      x.uri !== y.uri ||
      (x.style_preset ?? null) !== (y.style_preset ?? null)
    ) {
      return false;
    }
  }
  return true;
}

export function App(): JSX.Element | null {
  const bus = useMemo(() => createLayerPanelBus(), []);

  // job-0179 (per-Case client cache + view-state durability  -  "the seatbelt").
  // The process-global LayerCache holds the per-Case layer SET (in-memory) and
  // the user's per-layer view-overrides (opacity / visibility / zIndex, mirrored
  // to IndexedDB). It is the single source of truth the bus-subscribing surfaces
  // (Map.tsx reconcile teardown gate, LayerPanel user edits) share. A WS blip /
  // stale snapshot routes through cache.mergeSnapshot (additive, never evicts);
  // only an explicit Case switch / delete evicts. See lib/layer_cache.ts.
  const layerCache = useMemo(() => getLayerCache(), []);
  useEffect(() => {
    // Best-effort one-time hydrate of persisted view-overrides from IndexedDB.
    void layerCache.hydrate();
  }, [layerCache]);

  // job-0278  -  mobile layout (<768px). EVERY mobile divergence below is
  // guarded by this flag so desktop renders pixel-identical to before.
  const isMobile = useIsMobile();
  // Mobile-only: slide-in drawer (replaces the desktop left rail). Hidden
  // by default; deliberately NOT persisted to localStorage  -  the drawer is
  // an overlay, and the desktop collapse keys keep their own semantics.
  const [mobileDrawerOpen, setMobileDrawerOpen] = useState<boolean>(false);

  // Collapse toggles  -  initialised from localStorage so reloads remember
  // the user's preference.
  const [leftCollapsed, setLeftCollapsed] = useState<boolean>(() =>
    readCollapsed(LS_LEFT_COLLAPSED),
  );
  const [rightCollapsed, setRightCollapsed] = useState<boolean>(() =>
    readCollapsed(LS_RIGHT_COLLAPSED),
  );
  // ux-batch-1 J1 (F10)  -  App mirrors the user-dragged chat width so dependent
  // chrome (and the F16 payload-warning banner) can track the chat column edge.
  // Chat owns persistence; App seeds from the same localStorage value and
  // updates via Chat's onWidthChange. Initial read matches Chat's own init.
  const [chatWidth, setChatWidth] = useState<number>(() => readChatWidth());
  // ux-batch-1 J1 (F11)  -  App mirrors the user-dragged Layers-panel width so
  // the desktop pointer-events wrapper can grow with the panel (else clicks on
  // a widened panel fall through to the map). LayerPanel owns persistence.
  const [layersWidth, setLayersWidth] = useState<number>(() => readLayersWidth());

  // Layers lifted here from session-state so we can gate the left panel
  // conditional mount on layers.length > 0 and feed the LayerPanel. (job-0321
  // F43: the legend itself no longer reads this list at App level  -  it renders
  // inside Map.tsx anchored to the AOI bounding box.)
  const [layers, setLayers] = useState<ProjectLayerSummary[]>([]);

  // Map theme (job-0076).
  const [theme, setTheme] = useState<MapTheme>(() => readTheme());

  // The TRUE projected AOI screen rectangle, lifted out of MapView so the
  // SequenceScrubber (rendered inside LayerPanel, which has no map handle) can
  // pin bottom-center of the AOI box and track pan/zoom like the legend keys.
  // MapView fires onAoiScreenRectChange when the rect changes (null when there
  // is no AOI / it leaves the viewport). Mirrors the layers/chatWidth lift
  // pattern: App holds the Map-derived screen state, LayerPanel consumes it.
  const [aoiScreenRect, setAoiScreenRect] = useState<ScreenRect | null>(null);

  // ZOOM-OUT HIDE (NATE 2026-06-27, mobile-only) - whether the AOI bbox is a tiny
  // DOT on screen (zoomed OUT far). MapView lifts it via onAoiTooSmallToShowChange;
  // App threads it into the SequenceScrubber so the scrubber HIDES on the
  // zoomed-out-speck case (the legend reads the same signal directly inside Map).
  // Default false (no hide). DESKTOP never reads it (the scrubber's hide is
  // mobile-gated), so this adds nothing to the desktop render.
  const [aoiTooSmallToShow, setAoiTooSmallToShow] = useState<boolean>(false);

  // MOBILE SHEET-TOP DOCK (NATE 2026-06-24) - the mobile chat bottom-sheet's
  // geometry (expanded? + dragged height in vh) is lifted out of Chat (via its
  // onSheetGeometryChange callback) so the App-root overlays (SequenceScrubber +
  // LayerLegend keys) can dock to the sheet's TOP edge - one clean band at the
  // chat-panel top - instead of floating over the map with a fixed-pixel
  // clearance guess. We derive a single `sheetTopPx` (the on-screen Y of the
  // sheet's top edge) and thread it to both overlays. Mobile-only; null on
  // desktop (the overlays keep their viewport-bottom placement there).
  const [sheetExpanded, setSheetExpanded] = useState<boolean>(false);
  const [sheetHeightVh, setSheetHeightVh] = useState<number>(SHEET_HEIGHT_FALLBACK_VH);
  // CHART-OVERLAY HIDE-LEGEND (NATE 2026-06-28, mobile) - is Chat's full-viewport
  // ChartGallery overlay open? Lifted out of Chat (onGalleryOpenChange) so we can
  // thread it to the MAP's LayerLegend, which renders nothing on mobile while a
  // chart is open (so the body-portaled legend never paints above/around the
  // chart). Parallel to the sheetTopPx lift. Default false; desktop ignores it.
  const [chartGalleryOpen, setChartGalleryOpen] = useState<boolean>(false);
  // MEASURED SHEET TOP (NATE 2026-06-27, mobile-only) - the REAL top-edge screen
  // Y (px) of the chat sheet/composer, measured in Chat via getBoundingClientRect
  // under a ResizeObserver and lifted here. null until Chat reports its first
  // measurement (and always on desktop). When present we PREFER it over the
  // arithmetic estimate below, so the scrubber + legend dock above the REAL
  // connecting/bare/collapsed composer instead of floating mid-screen.
  const [sheetTopMeasuredPx, setSheetTopMeasuredPx] = useState<number | null>(
    null,
  );
  // Track viewport height so sheetTopPx recomputes on resize / orientation flip
  // (the sheet height is a vh fraction, and the collapsed estimate is measured
  // from the bottom). Seeded from the live window; updated on resize.
  const [viewportH, setViewportH] = useState<number>(() =>
    typeof window !== "undefined" && Number.isFinite(window.innerHeight)
      ? window.innerHeight
      : 0,
  );
  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const onResize = (): void => setViewportH(window.innerHeight);
    window.addEventListener("resize", onResize);
    window.addEventListener("orientationchange", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      window.removeEventListener("orientationchange", onResize);
    };
  }, []);
  const handleSheetGeometryChange = useCallback(
    (g: {
      expanded: boolean;
      heightVh: number;
      topPx: number | null;
    }): void => {
      setSheetExpanded(g.expanded);
      setSheetHeightVh(g.heightVh);
      // MEASURED-TOP (NATE 2026-06-27) - Chat measures the sheet's real top-edge
      // screen Y under a ResizeObserver. Keep the last non-null measurement: once
      // we have a real top we never fall back to the arithmetic estimate (a
      // transient null mid-teardown should not pop the overlays back to center).
      if (g.topPx != null) setSheetTopMeasuredPx(g.topPx);
    },
    [],
  );
  // The on-screen Y of the mobile chat sheet's TOP edge. Both overlays
  // (SequenceScrubber + LayerLegend) dock their BOTTOM edge just above this Y.
  // Mobile-only; null on desktop (the overlays keep their viewport-bottom
  // placement there, byte-for-byte unchanged).
  //
  // MEASURED-TOP PREFERENCE (NATE 2026-06-27, mobile-only) - we PREFER the REAL
  // top-edge px Chat measured via getBoundingClientRect (`sheetTopMeasuredPx`).
  // It is correct even in the connecting / bare / collapsed composer state, where
  // the arithmetic estimate (COLLAPSED_SHEET_PX=100) was wrong and the overlays
  // floated mid-screen. We fall back to the arithmetic estimate ONLY before the
  // first real measurement lands (sheetTopMeasuredPx == null): EXPANDED ->
  // viewportH - clampSheetHeight(heightVh)vh; COLLAPSED -> viewportH -
  // COLLAPSED_SHEET_PX. Desktop short-circuits to null first, so it never reads
  // either path.
  const sheetTopPx = !isMobile
    ? null
    : sheetTopMeasuredPx != null
      ? sheetTopMeasuredPx
      : viewportH > 0
        ? viewportH -
          (sheetExpanded
            ? Math.round((clampSheetHeight(sheetHeightVh) / 100) * viewportH)
            : COLLAPSED_SHEET_PX)
        : null;

  // NATE map/loading-UX polish item 1 - the bbox loading-animation overlay.
  //   - `bboxAnimEnabled` is the user's persisted enable flag (DEFAULT ON; the
  //     SettingsPopup toggle writes it through + bumps `bboxAnimSettingsTick` so
  //     this re-reads after a change). The CONNECTING scan border ignores it.
  //   - `simRunning` mirrors session-state.current_pipeline being in progress;
  //     it drives the PURPLE scan border for a long-running solve. Updated by the
  //     session-state subscription below (isPipelineRunning).
  const [bboxAnimSettingsTick, setBboxAnimSettingsTick] = useState(0);
  const bboxAnimEnabled = useMemo(
    () => readBboxAnimationsEnabled(),
    [bboxAnimSettingsTick],
  );
  const [simRunning, setSimRunning] = useState<boolean>(false);

  // "3D terrain viz" first cut - the persisted 3D-terrain + contour flags. The
  // SettingsPopup toggles write them through to localStorage and bump this tick
  // (via onTerrain3dChange) so App re-reads + re-threads them into MapView, which
  // applies/removes MapLibre terrain. Default OFF (read-with-default helpers).
  const [terrain3dSettingsTick, setTerrain3dSettingsTick] = useState(0);
  const terrain3dEnabled = useMemo(
    () => readTerrain3dEnabled(),
    [terrain3dSettingsTick],
  );
  const contoursEnabled = useMemo(
    () => readContoursEnabled(),
    [terrain3dSettingsTick],
  );

  // #170 AOI-first manual case-creation: when the user taps "+ New Case" we open
  // an AOI-capture overlay (the AoiPickerCard, mounted by Map.tsx) instead of
  // creating immediately. On confirm we createCase(title?, bbox); on Skip we
  // createCase() (no bbox = current behavior). This boolean is the local
  // "aoi-capture active" signal threaded to MapView (NOT the spatialRequest bus).
  // Declared here, ABOVE the AuthGate early-return (~:1388), so the hook runs
  // unconditionally (React #310 rule).
  const [aoiCaptureOpen, setAoiCaptureOpen] = useState<boolean>(false);

  // Item b (NATE 2026-06-20)  -  MOBILE legend show/hide state, owned at App level
  // so the toggle can live INSIDE the expanded Layers section (out of the way of
  // the chat composer) while the legend itself renders from Map.tsx. On desktop
  // the legend keeps its own internal hide state (this stays false). Cleared on
  // Case exit (item c) along with the scrubber + legend.
  const [legendHiddenMobile, setLegendHiddenMobile] = useState<boolean>(false);
  // LANE D (NATE): the DESKTOP legend is now a CONTROLLED toggle owned by App
  // too (like mobile), so its Show/Hide control can live in BottomRowButtons
  // NEXT TO Settings instead of the floating bottom-center pill. Default shown.
  const [legendHiddenDesktop, setLegendHiddenDesktop] = useState<boolean>(false);

  // Auth state (job-0123, sprint-12-mega Wave 2).
  const [authUser, setAuthUser] = useState<AuthUser | null>(null);
  const [authResolved, setAuthResolved] = useState<boolean>(false);
  // job-0253 (sprint-13.5)  -  auth-expired latch from ws.ts (close 4401 /
  // AUTH_FAILED, after the one-shot forceRefresh retry failed). Drops a
  // signed-in user to the AuthGuard sign-in surface. Cleared whenever a fresh
  // signed-in user arrives (re-sign-in succeeded).
  const [authExpired, setAuthExpired] = useState<boolean>(false);
  // job-0253b  -  re-sign-in reconnect epoch. handleAuthFailure's give-up branch
  // (ws.ts:1032-1035) leaves BOTH GraceWs sockets terminally dead (no
  // reconnect is scheduled  -  correct; we must not hammer the gate). Nothing
  // reconnects them later on its own: the App ws effect's deps are otherwise
  // stable and Chat keys on [wsUrl, bump]. So after a successful re-sign-in the
  // guard would render children over dead sockets until a full page reload.
  // We bump `authEpoch` exactly when a fresh non-anonymous user lands WHILE we
  // were auth-expired; `authEpoch` is threaded into both ws effects' deps, so
  // each effect tears its dead socket down (cleanup -> ws.close()) and opens a
  // fresh one (new GraceWs + connect(), which resets the auth latches at
  // ws.ts:424-427)  -  exactly once per recovery, never in disabled/dev mode
  // (Firebase disabled -> onAuthChanged only ever fires null -> authExpired is
  // never set -> this branch is unreachable, so authEpoch stays 0 forever).
  const [authEpoch, setAuthEpoch] = useState<number>(0);
  const authExpiredRef = useRef<boolean>(false);
  authExpiredRef.current = authExpired;

  // GCP->AWS migration  -  Cognito Hosted UI OAuth /callback handler. On boot, if
  // the URL carries a `?code=` (the authorization-code returned by the Hosted
  // UI), exchange it for tokens via auth.ts, then strip the query so a reload
  // doesn't re-trigger the exchange. onAuthChanged (below) flips authUser once
  // the token set lands. No-op when there is no code / Cognito is disabled, so
  // the dev/tailnet pass-through path is untouched.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (!params.has("code")) return;
    void (async () => {
      try {
        await handleRedirectCallback();
      } catch {
        // Exchange failures drop to the sign-in surface on next render.
      } finally {
        // Strip ?code (+ ?state) from the URL regardless of outcome.
        const url = new URL(window.location.href);
        url.searchParams.delete("code");
        url.searchParams.delete("state");
        window.history.replaceState(
          {},
          document.title,
          url.pathname + url.search + url.hash,
        );
      }
    })();
  }, []);

  useEffect(() => {
    const unsub = onAuthChanged((u) => {
      setAuthUser(u);
      setAuthResolved(true);
      // A real (non-anonymous) sign-in clears any prior auth-expired state and,
      // if we WERE auth-expired (the dead-socket wedge), bumps authEpoch so
      // both ws effects reconnect. The ref read avoids re-subscribing on every
      // authExpired flip.
      if (u && !u.isAnonymous) {
        if (authExpiredRef.current) setAuthEpoch((n) => n + 1);
        setAuthExpired(false);
      }
    });
    return unsub;
  }, []);

  // job-0138: anonymous-accepted flag. TRID3NT LOCAL (F5, live-feedback
  // 2026-07-08): the local build is single-user with NO auth surfaces, so the
  // anonymous-accept gate is pre-satisfied -- the app opens straight into the
  // anonymous single-user session (the local agent already accepts anonymous).
  // Cloud (flag unset) reads localStorage exactly as before.
  const [anonymousAccepted, setAnonymousAccepted] = useState<boolean>(
    () => isLocalDeployment() || readAnonymousAccepted(),
  );
  const [upgradeToast, setUpgradeToast] = useState<string | null>(null);
  const prevSignedInRef = useRef<boolean>(false);
  useEffect(() => {
    const nowSignedIn = !!authUser && !authUser.isAnonymous;
    const wasSignedIn = prevSignedInRef.current;
    prevSignedInRef.current = nowSignedIn;
    if (nowSignedIn && !wasSignedIn && anonymousAccepted) {
      clearAnonymousAccepted();
      setAnonymousAccepted(false);
      setUpgradeToast("Welcome back  -  your Cases will now sync");
      const t = setTimeout(() => setUpgradeToast(null), 4500);
      return () => clearTimeout(t);
    }
    return undefined;
  }, [authUser, anonymousAccepted]);

  // Gate render rule: show the app only when authenticated OR anonymous-accepted.
  const appShouldRender: boolean =
    authResolved &&
    ((!!authUser && !authUser.isAnonymous) || anonymousAccepted);

  // AuthGate handlers.
  const handleAnonymousAccept = useCallback(() => {
    setAnonymousAccepted(true);
  }, []);
  const handleSignOut = useCallback(async () => {
    try {
      await authSignOut();
    } catch {
      // non-fatal
    }
    clearAnonymousAccepted();
    setAnonymousAccepted(false);
  }, []);

  // Sign-in handler routed through Settings + SaveGate. Redirects to the
  // Cognito Hosted UI (email/password); the browser navigates away and the
  // /callback effect below completes the round-trip on return.
  const handleSignInRequest = useCallback(() => {
    void (async () => {
      try {
        await authSignIn();
      } catch {
        // Sign-in errors surface on the gate surface; nothing to do here.
      }
    })();
  }, []);

  // Secrets state (job-0125).
  const [secrets, setSecrets] = useState<SecretRecord[]>([]);
  const wsRef = useRef<GraceWs | null>(null);
  // job-0357 (per-Case layer DURABILITY)  -  live WS connection status, held in
  // a ref so the GraceWs `onSessionState` handler (a stable closure created
  // once when the socket is constructed) can read the CURRENT status without
  // being re-created on every status flip. The map-side LayerPanel bus push
  // stamps `session-state.replace_layers` from this: server snapshots received
  // while NOT `connected` (the disconnect / reconnect window) are
  // non-authoritative top-ups that must NOT tear down the active Case's
  // already-rendered layers; snapshots received while `connected` are
  // authoritative (live layer add AND delete apply via replace-not-reconcile).
  const wsStatusRef = useRef<ConnectionStatus>("connecting");

  // CASE-SWITCH LAYER LEAK FIX (NATE 2026-06-19)  -  the currently-active Case id,
  // mirrored into a ref so the GraceWs `onSessionState` handler (a STABLE
  // closure created once when the socket is constructed, see the WS effect
  // below) can read the LIVE active Case without being re-created on every Case
  // switch (which would tear down + re-open the socket  -  the WS cycling we must
  // avoid). The leak NATE hit: switching Case A -> B paints B's layers, then a
  // TRAILING server `session-state` STILL TAGGED with Case A (a late solve-finish
  // snapshot, or the server's resume replay racing the case-open) arrives over
  // the live socket; under the old handler it was stamped authoritative
  // (`replace_layers:true`, because the socket is `connected`) and Map.tsx
  // REPLACED B's layers with A's. ws.ts already extracts the envelope-level
  // `case_id` and passes it as the 2nd arg of `onSessionState`; we now DROP any
  // snapshot whose tag != the active Case. Synced in the effect just below.
  const activeCaseIdRef = useRef<string | null>(null);

  // sleep/wake STAGE 2 (NATE 2026-06-18)  -  the always-on agent box can be
  // STOPPED by the idle-check Lambda; a stopped box answers nothing so the WS
  // can't connect. STAGE 2 gates ONLY the chat COMPOSER behind a Connecting ->
  // (Chat | Wake) state machine (Chat.tsx owns the slot); the scrollback + the
  // whole map stay LIVE with the box asleep. App.tsx is the SINGLE SOURCE OF
  // TRUTH for the asleep signal (the App socket + a report-only wakeState GET)
  // and threads it down to Chat:
  //   - `wsStatus` mirrors the App socket's live status as STATE (the ref above
  //     is a stable closure read; the asleep derivation needs a re-render).
  //   - the consecutive-failure count arrives directly as the `attempt` arg of
  //     ws.ts `onWakeNeeded`. We only RUN the report-only wakeState() probe past
  //     WAKE_OVERLAY_THRESHOLD so a single transient blip (one failed attempt)
  //     never trips the Wake UI.
  //   - `agentAsleep` is the classified result of that GET probe (true when the
  //     box reports stopped/stopping). It NEVER triggers a wake  -  only the
  //     user's explicit composer tap POSTs wake. Cleared on a healthy reconnect.
  //   - `wakerRef` is the SHARED AgentWaker so the composer's explicit-tap path
  //     (resetDebounce -> StartInstances POST) and the report-only GET probe
  //     coalesce against the same instance.
  const [wsStatus, setWsStatus] = useState<ConnectionStatus>("connecting");
  // sleep/wake STAGE 2  -  classified asleep signal from the report-only GET
  // probe (true = box reports stopped/stopping). Drives Chat's composer Wake UI.
  // NEVER set from a reconnect/case-open alone (never auto-wake); only the GET
  // probe sets it. A successful WS reconnect clears it (the box is up).
  const [agentAsleep, setAgentAsleep] = useState<boolean>(false);
  const wakerRef = useRef<AgentWaker | null>(null);
  if (wakerRef.current === null) wakerRef.current = new AgentWaker();
  // sleep/wake STAGE 2  -  guard so the report-only wakeState() probe runs at most
  // once per "unreachable" episode (not on every reconnect tick). Reset on a
  // healthy reconnect.
  const wakeProbeInFlightRef = useRef<boolean>(false);
  // SHARED-BOX SLEEP (NATE 2026-06-29)  -  an EXPLICIT per-session pause. The box
  // is shared, so the Settings "Put agent to sleep" no longer stops it (that
  // would yank the box out from under other connected users); instead THIS
  // session goes dormant: close our WS, clear our layers, and surface the asleep
  // composer. `sessionPaused` drives that asleep visual (alongside the probe's
  // `agentAsleep`) and SUPPRESSES the involuntary visibility-resume reconnect so
  // the session stays paused until the user explicitly taps Wake (or refreshes).
  // The ref mirrors it for the stable visibility-effect closure. Cleared on the
  // wake tap and on any healthy reconnect.
  const [sessionPaused, setSessionPaused] = useState<boolean>(false);
  const sessionPausedRef = useRef<boolean>(false);

  // Settings popup visibility (job-0143). job-0321 F29  -  the standalone
  // Secrets popup is retired; API-key management now lives INSIDE Settings
  // (SettingsPopup's embedded SecretsPanel), so there is no separate
  // `secretsOpen` state any more.
  const [settingsOpen, setSettingsOpen] = useState<boolean>(false);
  // Wave 4.10 C1: tools-catalog popup visibility.
  const [toolsCatalogOpen, setToolsCatalogOpen] = useState<boolean>(false);
  // Wave 4.11 M7: routing-quality dashboard visibility.
  const [routingDashOpen, setRoutingDashOpen] = useState<boolean>(false);
  // Wave 4.11 M7: optional inject seam for Playwright snapshots. When the
  // window-attached fixture is present we mount the dashboard with the
  // pre-fetched summary so the visual smoke test renders without driving a
  // live agent. Production code never touches this  -  guarded behind a
  // global flag set only in the dev-tools harness.
  const [routingDashInjected, setRoutingDashInjected] =
    useState<RoutingDashboardSummary | null>(null);
  useEffect(() => {
    interface InjectWindow {
      __grace2InjectTelemetryFixture?: RoutingDashboardSummary;
    }
    const w = window as unknown as InjectWindow;
    if (w.__grace2InjectTelemetryFixture) {
      setRoutingDashInjected(w.__grace2InjectTelemetryFixture);
      setRoutingDashOpen(true);
    }
  }, []);
  // Wave 4.11 P4: ImpactEnvelope side panel. Populated when a
  // ``compute_impact_envelope`` tool result arrives carrying
  // ``raw_envelope.n_structures_total`` (the ImpactEnvelope shape from B.6c).
  const [impactEnvelope, setImpactEnvelope] = useState<ImpactEnvelope | null>(
    null,
  );

  // sprint-13 job-0231: chart-emission accumulates per session in App.tsx's
  // GraceWs connection so the session-scoped hub fan-out reaches Chat.tsx.
  // Charts are actually rendered in Chat.tsx; App.tsx only holds the state
  // for reset-on-Case-switch (replace-not-reconcile) and the dev seam.
  const [charts, setCharts] = useState<ChartPayload[]>([]);

  const handleChartEmission = useCallback((p: ChartPayload) => {
    setCharts((prev) => {
      // De-duplicate on chart_id so re-emits from the same tool don't stack.
      if (prev.some((c) => c.chart_id === p.chart_id)) return prev;
      return [...prev, p];
    });
  }, []);

  // job-0137 Cases UX shell + job-0143 save-gate wiring.
  const sendCaseCommand = useCallback(
    (
      command: Parameters<GraceWs["sendCaseCommand"]>[0],
      caseId: string | null,
      args: Record<string, unknown>,
    ) => {
      wsRef.current?.sendCaseCommand(command, caseId, args);
    },
    [],
  );
  const isSignedIn = !!authUser && !authUser.isAnonymous;
  // COLD-LIST RASTER FALLBACK (coldview_layers_fix.md FIX C) - useCases calls
  // this from `onCaseList` with the OPEN Case's cold-renderable RASTER
  // loaded_layer_summaries (TiTiler tile templates via always-on CloudFront).
  // Push them onto the map channel as a NON-authoritative reconcile
  // (replace_layers:false) so when the per-case snapshot is missing / stale /
  // 404 (the box-stop lost-write race) the rasters STILL paint instead of "no
  // layers loaded". Non-authoritative => the seatbelt merges/adds these into
  // the active Case but never evicts, so this is idempotent with a later
  // snapshot / live frame and never wipes a warm Case nor strands vectors.
  const onListLayerSummaries = useCallback(
    (_caseId: string, rasterSummaries: ProjectLayerSummary[]) => {
      if (rasterSummaries.length === 0) return;
      bus.pushSessionState({
        loaded_layers: rasterSummaries,
        replace_layers: false,
      });
    },
    [bus],
  );
  const {
    cases,
    activeCaseId,
    restoredActiveCaseId,
    activeSession,
    casesSettled,
    onCaseList: useCases_onCaseList,
    onCaseOpen: useCases_onCaseOpen,
    createCase,
    selectCase,
    renameCase,
    archiveCase,
    deleteCase,
    clearActive,
  } = useCases({ sendCaseCommand, isSignedIn, onListLayerSummaries });

  // job-0143: gate save-triggering Case actions for anonymous users.
  const saveGate = useSaveGate({
    isSignedIn,
    onSignInRequest: handleSignInRequest,
  });

  // #170 AOI-first: "+ New Case" now OPENS the AOI-capture overlay (still
  // save-gated for anonymous users) instead of creating immediately. The actual
  // createCase fires from the overlay's confirm (with bbox) or skip (no bbox).
  const onCreateGated = useMemo(
    () =>
      saveGate.gateAction(() => setAoiCaptureOpen(true), "Create a new Case"),
    [saveGate],
  );
  // Confirm: create the Case WITH the chosen name + captured AOI bbox, then close
  // the overlay. The two-step onboarding (name -> AOI) yields both; an empty name
  // falls through to the server's "Untitled Case" default (createCase trims).
  const onAoiCaptureConfirm = useCallback(
    (bbox: [number, number, number, number], name: string) => {
      setAoiCaptureOpen(false);
      createCase(name || null, bbox);
    },
    [createCase],
  );
  // Skip: create the Case with the name + NO bbox - the no-bbox create path is
  // byte-identical to the prior behavior (just now carrying the chosen title).
  const onAoiCaptureSkip = useCallback(
    (name: string) => {
      setAoiCaptureOpen(false);
      createCase(name || null);
    },
    [createCase],
  );
  // Cancel: dismiss the overlay without creating a Case.
  const onAoiCaptureCancel = useCallback(() => {
    setAoiCaptureOpen(false);
  }, []);
  // ITEM 4 / feature #170 (NATE 2026-06-22) - AOI-first via the always-on
  // Draw-AOI control's green "+". The user draws a bbox on the live map and
  // confirms with "+"; that extent SEEDS a new case as its analysis area - the
  // SAME createCase(name, bbox) channel the AoiPickerCard onConfirm rides (here
  // with no name; the server defaults to "Untitled Case", renameable later).
  // The agent's first turn then reuses the seeded extent (no re-geocode). The
  // DrawAoiControl also fits the camera (draw-and-fit) before this fires.
  const onAoiStageConfirm = useCallback(
    (bbox: [number, number, number, number]) => {
      createCase(null, bbox);
    },
    [createCase],
  );
  const onRenameGated = useCallback(
    (caseId: string, newTitle: string) => {
      saveGate.gateAction(
        () => renameCase(caseId, newTitle),
        "Rename Case",
      )();
    },
    [saveGate, renameCase],
  );
  const onArchiveGated = useCallback(
    (caseId: string) => {
      saveGate.gateAction(
        () => archiveCase(caseId),
        "Archive Case",
      )();
    },
    [saveGate, archiveCase],
  );
  // job-0276: delete is NOT save-gated. It already has its own
  // ConfirmationDialog, and stacking the "Sign in to save" gate on top of
  // the delete confirm was live-reproduced as a click-eating modal trap
  // ("can't get back into the Case"). Deleting work is also not a
  // save-upsell moment.
  const onDeleteGated = useCallback(
    (caseId: string) => {
      deleteCase(caseId);
    },
    [deleteCase],
  );

  // NATE item 3 - SNAP-ON-SELECT. On case-select, fit the map to the case
  // SUMMARY bbox (CaseSummary.bbox, already on the cases list) IMMEDIATELY -
  // BEFORE the full case/layers round-trip - so the camera moves + the analysis
  // extent draws (and the bbox loading animation arms via aoiScreenRect) the
  // instant the user taps, instead of the dead air where nothing moves until the
  // whole case loads. The layers then stream in underneath. The later case-open
  // rehydration (the [activeSession, bus] effect) re-asserts the FLOORED bbox /
  // last zoom-to idempotently, so a refined extent supersedes this preview snap.
  // When the summary has no (valid) bbox we just select - no snap (older Cases /
  // no-AOI Cases are unchanged, and the case-open path still replays any history
  // zoom-to). asBbox is the SAME finite-4-number guard the rehydration uses.
  const onSelectCase = useCallback(
    (caseId: string) => {
      const summary = cases.find((c) => c.case_id === caseId);
      const previewBbox = summary ? asBbox(summary.bbox) : null;
      if (previewBbox) {
        bus.pushMapCommand({
          command: "zoom-to",
          args: { bbox: previewBbox },
        } as unknown as MapCommandPayload);
      }
      selectCase(caseId);
    },
    [cases, bus, selectCase],
  );

  // currentCaseId for the embedded SecretsPanel scope (inside Settings).
  const currentCaseId: string | null = activeCaseId;

  // FIX 2 (NATE 2026-06-17): the payload-warning gate moved OUT of App into
  // Chat's per-Case interleaved stream (an in-chat card, not a banner "hat").
  // App no longer accumulates / renders / answers the warning  -  Chat owns the
  // whole flow (route + render + sendPayloadConfirmation) because tool-payload-
  // warning is session-scoped and reaches Chat's GraceWs directly.

  // job-0126 (renamed job-0145): source-suggestion candidate fan-out. Server
  // wire envelope_type is still `mode2-candidate` (internal); UI translates.
  const sourceSuggestionSubscribersRef = useRef<
    Set<(p: SourceCandidatePayload) => void>
  >(new Set());
  const subscribeSourceSuggestion = useCallback(
    (cb: (p: SourceCandidatePayload) => void) => {
      sourceSuggestionSubscribersRef.current.add(cb);
      return () => {
        sourceSuggestionSubscribersRef.current.delete(cb);
      };
    },
    [],
  );
  const fanoutSourceSuggestion = useCallback(
    (p: SourceCandidatePayload) => {
      sourceSuggestionSubscribersRef.current.forEach((cb) => {
        try {
          cb(p);
        } catch {
          // eslint-disable-next-line no-console
          console.error("[source-suggestion] subscriber threw");
        }
      });
    },
    [],
  );

  const handleSourceSuggestionAction = useCallback(
    (action: SourceSuggestionAction) => {
      const ws = wsRef.current;
      const c = action.candidate;
      if (action.kind === "add") {
        ws?.sendMode2AddConfirmed({
          candidate_id: c.candidate_id,
          url: c.url,
          domain: c.domain,
          suggested_tool_kind: c.suggested_tool_kind,
        });
      }
      ws?.sendMode2AuditEvent({
        candidate_id: c.candidate_id,
        domain: c.domain,
        action: action.kind,
        confidence: c.confidence,
        surface: "inline",
      });
      // eslint-disable-next-line no-console
      console.debug(
        `[source-suggestion-audit] ${action.kind} surface=inline domain=${c.domain} candidate=${c.candidate_id}`,
      );
    },
    [],
  );

  function toggleTheme(): void {
    setTheme((prev) => {
      const next: MapTheme = prev === "light" ? "dark" : "light";
      try { localStorage.setItem(LS_THEME, next); } catch { /* non-fatal */ }
      return next;
    });
  }

  // FLASH FIX (Lane 1a): stable identity so React.memo(LayerPanel) is not
  // defeated by a fresh onClose closure every App render (the panel receives
  // collapseLeft as `onClose`). setLeftCollapsed is stable, so no deps.
  const collapseLeft = useCallback((): void => {
    setLeftCollapsed(true);
    try { localStorage.setItem(LS_LEFT_COLLAPSED, "true"); } catch { /* non-fatal */ }
  }, []);

  function expandLeft(): void {
    setLeftCollapsed(false);
    try { localStorage.setItem(LS_LEFT_COLLAPSED, "false"); } catch { /* non-fatal */ }
  }

  function collapseRight(): void {
    setRightCollapsed(true);
    try { localStorage.setItem(LS_RIGHT_COLLAPSED, "true"); } catch { /* non-fatal */ }
  }

  function expandRight(): void {
    setRightCollapsed(false);
    try { localStorage.setItem(LS_RIGHT_COLLAPSED, "false"); } catch { /* non-fatal */ }
  }

  // job-0143: clicking the breadcrumb arrow deselects the active Case.
  const handleCaseBack = useCallback(() => {
    clearActive();
  }, [clearActive]);

  // sleep/wake STAGE 2  -  whether the composer should surface the Wake UI. App
  // owns this single source of truth (the App socket + the report-only probe)
  // and threads it down to Chat, which renders the Wake UI INSIDE the composer
  // slot only (scrollback + map stay live). Gated on wakeConfigured() so dev/LAN
  // (no wake endpoint -> the box is never auto-stopped) never shows it, and on
  // the App socket NOT being connected (a healthy App socket implies the box is
  // up). The actual asleep classification is `agentAsleep`, set by the GET probe.
  const composerWakeReady =
    wakeConfigured() &&
    wsStatus !== "connected" &&
    (agentAsleep || sessionPaused);

  // Explicit user tap on the composer's "Wake up agent" rectangle: reset the
  // shared waker's debounce so a manual press always fires StartInstances (even
  // right after a prior attempt) and POST the wake endpoint. This is the ONLY
  // path that POSTs wake (never auto-wake). Fire-and-forget  -  never throws. The
  // App socket's onStatus "connected" clears agentAsleep when the box is back.
  const handleWakeTap = useCallback(() => {
    // SHARED-BOX SLEEP: if THIS session was explicitly paused (Settings), the box
    // is most likely still up (only OUR socket was torn down). Lift the pause and
    // revive our own socket so the open handler re-sends auth + session-resume
    // and the server replays our active Case's layers (per-Case durability). The
    // close() we did on pause set closedByUser, which stopped the reconnect loop,
    // so an explicit connect() is required to come back.
    if (sessionPausedRef.current) {
      sessionPausedRef.current = false;
      setSessionPaused(false);
      wsRef.current?.connect();
    }
    // Always ALSO POST wake (debounced, idempotent): the box may have genuinely
    // auto-stopped (server-side, once ALL sessions went idle) while we were
    // paused, so StartInstances covers that case; if the box is already up it is
    // a harmless no-op. This stays the ONLY path that POSTs wake.
    const waker = wakerRef.current;
    if (!waker) return;
    waker.resetDebounce();
    void waker.wake().catch(() => {
      /* best-effort; the reconnect loop owns recovery */
    });
  }, []);

  // SHARED-BOX SLEEP (NATE 2026-06-29) - the per-session "Put agent to sleep"
  // teardown wired into SettingsPopup. PURELY local: it NEVER POSTs a box stop
  // (the shared box auto-stops server-side only once ALL sessions are idle), so
  // it cannot disrupt other connected users. It (1) marks the session paused so
  // the composer shows the asleep/Wake card and the visibility-resume reconnect
  // is suppressed, (2) closes our WS cleanly (closedByUser -> no auto-reconnect),
  // and (3) clears our live layers / case-view state (App `layers` + an
  // authoritative empty replace into the LayerPanel/Map bus) so nothing of ours
  // lingers. activeCaseId + the chat scrollback are intentionally KEPT; tapping
  // Wake (or a refresh) reconnects and the server replays the Case's layers.
  const handleSleepSession = useCallback(() => {
    sessionPausedRef.current = true;
    setSessionPaused(true);
    wsRef.current?.close();
    setLayers([]);
    bus.pushSessionState({
      loaded_layers: [],
      chat_history: [],
      pipeline_history: [],
      current_pipeline: null,
      map_view: null,
      replace_layers: true,
    });
  }, [bus]);

  // Mount a GraceWs that routes session-state, map-command, AND secrets-list.
  useEffect(() => {
    const ws = new GraceWs(WS_URL, {
      // job-0357  -  record live status so onSessionState can classify a
      // server snapshot as authoritative (connected) vs a reconnect top-up.
      // Auto-stop/wake  -  ALSO mirror into state so the WakeOverlay re-renders
      // on a flip. On a successful (re)connect, clear the wake state: the box
      // is up, so the overlay fades out and the attempt counter resets.
      onStatus: (s) => {
        wsStatusRef.current = s;
        setWsStatus(s);
        if (s === "connected") {
          // Healthy (re)connect  -  the box is up. Clear the asleep state (the
          // composer flips Connecting/Wake -> Chat) and reset the probe guard so
          // a future unreachable episode probes again. SHARED-BOX SLEEP: a
          // healthy reconnect also lifts an explicit per-session pause (the user
          // is back online), so clear that too.
          setAgentAsleep(false);
          sessionPausedRef.current = false;
          setSessionPaused(false);
          wakeProbeInFlightRef.current = false;
        }
      },
      // sleep/wake STAGE 2  -  ws.ts schedules a reconnect that won't open (the box
      // may be stopped). NEVER AUTO-WAKE: ws.ts no longer POSTs wake here. Track
      // the consecutive-failure count and, once we cross the threshold, run a
      // REPORT-ONLY GET probe (wakeState  -  never starts the box) to classify
      // asleep. If the box reports stopped/stopping we flip `agentAsleep` so the
      // composer surfaces the tap-to-wake UI; otherwise we keep retrying the WS
      // (the composer stays "Connecting"). The wakeProbeInFlightRef guard runs
      // the probe at most once per unreachable episode (not on every tick).
      onWakeNeeded: (attempt) => {
        if (attempt < WAKE_OVERLAY_THRESHOLD) return;
        if (wakeProbeInFlightRef.current) return;
        const ws = wsRef.current;
        if (!ws) return;
        wakeProbeInFlightRef.current = true;
        void ws
          .reportWakeState()
          .then((state: WakeState) => {
            // Only the App socket's onStatus "connected" clears agentAsleep; a
            // probe that comes back "running"/"pending" leaves it as-is (keep
            // retrying WS). stopped/stopping -> asleep (show Wake UI).
            if (state === "stopped" || state === "stopping") {
              setAgentAsleep(true);
            }
          })
          .catch(() => {
            /* report-only probe is best-effort; stay in Connecting */
          })
          .finally(() => {
            // Allow a re-probe on the NEXT threshold crossing (e.g. a probe that
            // came back "pending" and the box later actually stopped). onStatus
            // "connected" also resets this.
            wakeProbeInFlightRef.current = false;
          });
      },
      onAgentChunk: () => { /* Chat owns rendering */ },
      onPipelineState: () => { /* Chat owns rendering */ },
      // job-0357 (per-Case layer DURABILITY) - stamp the client-only
      // `replace_layers` hint Map.tsx reads to decide replace-not-reconcile
      // (Appendix A.7) vs additive top-up. See the CLIENT FLICKER FIX note on
      // the stamp itself below for the exact authoritative-vs-no-op rule.
      onSessionState: (p, caseId, fannedOut) => {
        // CASE-SWITCH LAYER LEAK FIX (NATE 2026-06-19)  -  DROP a server snapshot
        // tagged with a Case that is NOT the active one. ws.ts surfaces the
        // envelope-level `case_id` here as `caseId`; a snapshot whose tag does
        // not match `activeCaseIdRef.current` is a TRAILING update for a Case we
        // already left (a late solve-finish frame, or the resume replay racing
        // the new Case's case-open). Applying it to the live map would replace
        // the now-active Case's layers with the stale Case's (NATE's bug:
        // "the layers are filled with the urban flood ones, and the original
        // disappeared"). We compare ONLY when BOTH are non-null: a snapshot with
        // no tag (`caseId == null`) is an untagged/root frame we still honor (it
        // never carries another Case's layers), and an untagged-active state
        // (`activeCaseIdRef.current == null`, the root view) likewise applies  - 
        // so per-Case layer DURABILITY across a WS reconnect is unaffected (a
        // reconnect resume for the SAME active Case is either tagged with that
        // Case, matching here, or untagged, applied). Only a genuine
        // cross-Case mismatch is dropped.
        const active = activeCaseIdRef.current;
        if (caseId != null && active != null && caseId !== active) return;
        bus.pushSessionState({
          ...p,
          // CLIENT FLICKER FIX (per-Case layer DURABILITY) - a SERVER-DELIVERED
          // snapshot is authoritative (full replace-not-reconcile: live adds AND
          // deletes apply) ONLY when the socket is healthy AND it actually
          // carries layers. The server re-ships a full session-state on every
          // resume INCLUDING the 25s keepalive heartbeat; a heartbeat (or a
          // reconnect mid-flight) can momentarily carry an EMPTY / stale
          // loaded_layers for the SAME Case, which under the old
          // `replace_layers = (connected)` stamp wiped the map then refilled on
          // the next good frame -> the flicker, and violated the durability HARD
          // REQ. An EMPTY server frame is now NON-authoritative (additive no-op):
          // Map.tsx never tears down the active Case's already-rendered overlays
          // on it. The EXPLICIT Case SWITCH / EXIT path (the activeSession effect
          // below) still stamps replace_layers:true on its empty clear, so only a
          // real Case change clears prior-Case layers.
          //
          // ITEM 1 (NATE 2026-06-22  -  roads-flash eviction fix): a HUB-FANNED-OUT
          // session-state (`fannedOut === true`) is built from a SIBLING socket's
          // emitter, which can be STALE relative to this socket's view. The
          // concrete bug: a live-added vector (roads) lands on the Chat socket and
          // fans out to App (paints), but ~25s later App's OWN keepalive resume
          // reply is built from App's stale emitter (has the flood raster, NOT
          // roads). Under the old stamp that App-own frame was authoritative
          // (connected + has layers), so mergeSnapshot evicted roads (the flash-
          // then-vanish). The fix makes any fanned-out frame ADDITIVE-ONLY (never
          // authoritative -> never evicts): it may ADD/refresh layers a sibling
          // saw but can never tear down a layer this socket already has. Only this
          // socket's OWN frame (fannedOut falsy)  -  or the explicit Case-switch path
          //  -  may authoritatively replace. The own-frame stamp is unchanged
          // (connected + non-empty), so per-Case durability, delete, and the
          // empty-heartbeat no-op all behave exactly as before.
          replace_layers:
            !fannedOut &&
            wsStatusRef.current === "connected" &&
            (p.loaded_layers?.length ?? 0) > 0,
        });
      },
      onMapCommand: (p) => bus.pushMapCommand(p),
      onSecretsList: (p) => setSecrets(p.secrets ?? []),
      onMode2Candidate: (p) => fanoutSourceSuggestion(p),
      // FIX 2  -  payload-warning is handled by Chat's GraceWs now (in-chat card),
      // not App. No onPayloadWarning handler here.
      onCaseList: (p: CaseListEnvelopePayload) => useCases_onCaseList(p),
      onCaseOpen: (p: CaseOpenEnvelopePayload) => {
        // CASE-SWITCH LAYER LEAK FIX (NATE 2026-06-19)  -  DROP a trailing
        // `case-open` for a Case we already LEFT. The same leak class as the
        // session-state guard above: after switching A -> B, a late `case-open`
        // still tagged with Case A (an in-flight `select` reply racing the new
        // one) would re-assert A's whole session  -  rail, layers AND chat  -  over
        // B. We drop ONLY when the active Case is non-null AND the incoming
        // Case is a DIFFERENT non-null Case. This deliberately still applies:
        //   - auto-create from root (active == null)  -  a brand-new Case opens;
        //   - the normal `select(B)` reply (incoming B == active B, set
        //     optimistically by selectCase)  -  re-affirms B idempotently;
        //   - a deselect-to-root reply (incoming null)  -  clears cleanly.
        const incoming = p.session_state?.case.case_id ?? null;
        const active = activeCaseIdRef.current;
        if (incoming != null && active != null && incoming !== active) return;
        useCases_onCaseOpen(p);
        // job-0179  -  mirror the cold-load: push case-open onto the bus so Chat
        // can build the stream's chat-history bubbles. Idempotent (routeCaseOpen
        // only rebuilds a stream the first time it sees the caseId).
        bus.pushCaseOpen(p);
      },
      onError: () => { /* Chat owns rendering */ },
      // job-0253 (sprint-13.5): the agent's prod auth gate rejected us
      // (4401 / AUTH_FAILED) and the one-shot token refresh also failed.
      // Drop to the AuthGuard sign-in surface. No-op when Firebase is
      // disabled (the gate never engages in dev/tailnet mode).
      onAuthExpired: () => setAuthExpired(true),
      // Wave 4.11 P4: surface ImpactPanel when agent emits impact-envelope.
      onImpactEnvelope: (p) => setImpactEnvelope(p),
      // sprint-13 job-0231: accumulate chart-emission payloads per session.
      onChartEmission: (p) => handleChartEmission(p),
    }, { waker: wakerRef.current ?? undefined });
    wsRef.current = ws;
    ws.connect();
    return () => {
      wsRef.current = null;
      ws.close();
    };
    // job-0253b  -  authEpoch is bumped on a recovered re-sign-in (see the
    // onAuthChanged effect above); re-running this effect closes the dead
    // post-4401 socket and opens a fresh one. In disabled/dev mode authEpoch
    // never changes, so this effect runs exactly once as before.
    //
    // BUG 4a (Wave 4.9) STABILITY CONTRACT  -  every dep here is a STABLE
    // reference so an UNRELATED re-render does NOT tear down + re-open the
    // GraceWs (which presented as the ~10-45s WS cycling). Specifically:
    //   - bus: useMemo([], ...)  -  created once.
    //   - fanoutSourceSuggestion / handleChartEmission: useCallback([], ...).
    //   - useCases_onCaseList / useCases_onCaseOpen: useCallback([], ...) inside
    //     useCases (verified stable in hooks/useCases.ts).
    //   - authEpoch: a number that ONLY changes on a re-sign-in recovery.
    // Do NOT add an unmemoized object/closure to this array  -  it would recreate
    // the socket every render. (Tested in App.test.tsx "GraceWs creation effect
    // stability".)
  }, [bus, fanoutSourceSuggestion, useCases_onCaseList, useCases_onCaseOpen, handleChartEmission, authEpoch]);

  // ACTIVE-CASE RESTORE (NATE 2026-06-26) - on reload (felt most on mobile) the
  // app dropped to the Cases LIST instead of staying in the open Case, because
  // nothing rehydrated the persisted active Case: useCases now seeds
  // activeCaseId from localStorage, but the server's reconnect path
  // (_handle_session_resume) re-emits session-state + case-list and NEVER a
  // case-open, so without a client `select` the Case shell stays empty. Dispatch
  // ONE selectCase(restored) AFTER the socket is wired (this effect runs after
  // the socket-construction effect above) so layers / chat / map rehydrate: the
  // WS `select` flushes to the server (or cold-loads via the disconnected path),
  // and selectCase is idempotent with any live case-open that re-affirms the
  // same id. A stale / deleted restored id self-heals via the archived/deleted
  // reconcile + tombstones on the next authoritative case-list. One-shot: gated
  // by a ref so it never re-fires on later renders.
  const didRestoreActiveCaseRef = useRef(false);
  useEffect(() => {
    if (didRestoreActiveCaseRef.current) return;
    didRestoreActiveCaseRef.current = true;
    if (restoredActiveCaseId) selectCase(restoredActiveCaseId);
  }, [restoredActiveCaseId, selectCase]);

  // LANE CASE-WEB  -  keep the GraceWs's notion of the CURRENT active Case in
  // sync with useCases.activeCaseId. ws.ts STAMPS this onto every outbound
  // user-message + session-resume so the server treats the client as the case
  // authority. This is a SEPARATE effect from the socket-construction effect on
  // purpose: that effect's deps are deliberately all-stable (adding activeCaseId
  // there would tear down + re-open the socket on every Case switch  -  the WS
  // cycling we must avoid). Here we only push the value into the EXISTING
  // socket. The null-guard covers the brief construct/teardown window; the open
  // handler reads the latest currentCaseId at connect time regardless.
  //
  // CROSS-CASE LAYER FLASH FIX (NATE 2026-06-29) - this is a useLayoutEffect, NOT
  // a useEffect, on purpose. The Lane 1b clear below (setLayers([]) + the empty
  // authoritative bus.pushSessionState) must run BEFORE the browser paints the
  // switched-into Case. As a passive useEffect it fired AFTER paint, so on a
  // case->case switch React committed + PAINTED one frame with activeCaseId =
  // the NEW Case but App's lifted `layers` (and the still-mounted LayerPanel
  // reducer, which only re-seeds from the bus, not from a changed `initialLayers`
  // prop) still holding the PREVIOUS Case's layers - the brief foreign-layer
  // flash NATE reported (Boulder layers blinking into the seismic Case). Running
  // the clear in a layout effect flushes the empty replace synchronously before
  // paint, so no prior-Case layer is ever painted in the new Case.
  useLayoutEffect(() => {
    wsRef.current?.setCurrentCaseId(activeCaseId);
    // CASE-SWITCH LAYER LEAK FIX  -  keep the ref the stable onSessionState
    // closure reads in lockstep with the active Case so a trailing snapshot
    // tagged with the PREVIOUS Case is dropped the instant we switch.
    const prevCaseId = activeCaseIdRef.current;
    activeCaseIdRef.current = activeCaseId;
    // job-0179  -  keep the shared LayerCache's notion of the active Case in
    // lockstep so Map.tsx / LayerPanel (bus subscribers with no caseId prop)
    // resolve allowsEvict / getOverride against the right Case. A genuine Case
    // SWITCH (prev != next, both meaningful) is the ONLY in-memory evict path:
    // drop the Case we just LEFT so its layer SET no longer protects against the
    // new Case's authoritative replace. (The persisted view-overrides survive
    // the evict  -  re-opening the old Case restores them.) Snapshot omission never
    // reaches here, so the seatbelt holds.
    if (prevCaseId !== null && prevCaseId !== activeCaseId) {
      layerCache.evictCase(prevCaseId);
      // CROSS-CASE STALE-LAYER FLASH FIX (Lane 1b): clear the panel SYNCHRONOUSLY
      // on a case->case SWITCH, not just on exit-to-root. Without this, App's
      // lifted `layers` and the LayerPanel reducer keep showing the PREVIOUS
      // case's layers until the new case's session-state arrives async (a WS
      // round-trip / cold-load later), so the old layers flash before the new
      // ones land. Mirror the exit-to-root clear: drop App `layers` now AND push
      // an authoritative empty replace_layers:true session-state so the panel
      // reducer drops the old layers instantly. The new case's own session-state
      // (activeSession rehydration effect) then replaces this empty frame. Net:
      // a brief empty/loading state on switch, never the prior case's layers.
      setLayers((prev) => (prev.length === 0 ? prev : []));
      bus.pushSessionState({
        loaded_layers: [],
        chat_history: [],
        pipeline_history: [],
        current_pipeline: null,
        map_view: null,
        replace_layers: true,
      });
      // Item c (NATE 2026-06-20)  -  clear the SCRUBBER + the (mobile) legend on
      // Case EXIT / SWITCH. The scrubber is driven by the module-level
      // AnimationController; on exit the LayerPanel unmounts (the rail shows the
      // Cases list, not CaseView) so it never pushes setGroups([]) to clear it  -
      // the left Case's groups would linger and the App-level scrubber would
      // keep showing. reset() drops all groups + stops playback so the scrubber
      // vanishes. The legend clears with the layers (Map.tsx), but the MOBILE
      // hide-state is App-owned, so reset it too (a fresh Case shows its legend).
      getAnimationController().reset();
      setLegendHiddenMobile(false);
    }
    // BUG 2 (NATE 2026-06-23) - EXIT-TO-ROOT is a CLEAR SLATE. Navigating OUT to
    // the Cases root (activeCaseId === null) must leave NOTHING on the map: the
    // AOI bbox overlay anchor (aoiScreenRect, which drives BboxProgressOverlay)
    // and the lifted `layers` list both have to be cleared HERE, unconditionally.
    // The switch path above handles a SWITCH (evict + scrubber reset); a fresh
    // Case re-arms its OWN bbox/layers via the activeSession effect + MapView
    // re-projection, so this exit-only clear never strands a switched-into Case.
    // Previously the AOI overlay lingered after exit because the only clear path
    // was the `clear-analysis-extent` map command -> MapView -> onAoiScreenRectChange,
    // which can lag / not re-fire (box busy/asleep, no map re-project), so the
    // dashed bbox + its progress scan stayed on the Cases root. Clearing the App
    // state directly is the durable "Cases is a clear slate" guarantee NATE keeps
    // reporting. These are setState calls inside an effect (NOT hooks), so they
    // do not affect the #310 hook-count rule.
    if (activeCaseId === null) {
      setAoiScreenRect(null);
      setLayers([]);
    }
    layerCache.activeCaseId = activeCaseId;
  }, [activeCaseId, layerCache, bus]);

  // job-0322 F31  -  resume-repaint (iOS zombie-socket fix). Mobile browsers
  // tear down (or silently wedge) the WebSocket when the tab is backgrounded;
  // on return the in-memory layers were never re-pulled, so the map looks empty
  // until a Case reopen.
  //
  // On `visibilitychange -> visible`:
  //   - MOBILE: iOS Safari leaves the socket nominally `OPEN` while the
  //     underlying connection is dead, so the lighter reconnect() path no-ops
  //     and requestSessionState() sends `session-resume` into a dead socket
  //     (the server never re-emits session-state). We call forceReconnect()
  //     which UNCONDITIONALLY tears the socket down and re-opens; the fresh
  //     open handler re-sends auth-token + session-resume, so the layers
  //     reconcile back through replace-not-reconcile (Appendix A.7). No
  //     separate requestSessionState()  -  the open handler resumes for us.
  //   - DESKTOP: the socket reliably fires `close` when it actually drops, so
  //     the cheaper reconnect() (revive only if dropped) + requestSessionState()
  //     (re-pull on the live socket) is enough and avoids needlessly dropping a
  //     healthy connection. Both are idempotent.
  //
  // The wsRef null-guard covers the brief window between unmount and re-mount.
  useEffect(() => {
    const onVisibility = (): void => {
      if (document.visibilityState !== "visible") return;
      // SHARED-BOX SLEEP: while THIS session is explicitly paused, do NOT revive
      // the socket on a tab refocus - the user paused on purpose and stays
      // dormant until they tap Wake (or refresh). Without this, the not-OPEN
      // branch below would forceReconnect()/reconnect() and silently un-pause.
      if (sessionPausedRef.current) return;
      const ws = wsRef.current;
      if (!ws) return;
      // BUG 4a (Wave 4.9)  -  do NOT force-reconnect an already-OPEN socket. A
      // healthy live connection only needs a state re-pull on resume; tearing
      // it down churns the socket (the cycling this fix targets). Only a
      // closed/closing/never-connected socket gets the teardown path:
      //   - OPEN: lighter requestSessionState()  -  re-pull authoritative
      //     session-state on the live socket (no teardown). The keepalive's
      //     missed-pong detector now owns the iOS zombie case (a dead socket
      //     that still reports OPEN) instead of an unconditional resume-time
      //     teardown, so this is safe on mobile too.
      //   - NOT OPEN (mobile background tear-down / desktop drop): revive it.
      //     forceReconnect() (mobile) / reconnect() (desktop) re-opens; the
      //     fresh open handler re-sends auth-token + session-resume, so the
      //     layers reconcile back via replace-not-reconcile (Appendix A.7).
      if (ws.isOpen) {
        ws.requestSessionState();
        return;
      }
      if (isMobile) {
        // Not OPEN: unconditionally re-open. The fresh open handler re-sends
        // session-resume itself, so no separate requestSessionState().
        ws.forceReconnect();
        return;
      }
      // Desktop, not OPEN: revive first (dead socket), then pull state.
      ws.reconnect();
      ws.requestSessionState();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [isMobile]);

  // job-0137: Case rehydration replay.
  useEffect(() => {
    // sprint-13 job-0231: Case switch resets charts (replace-not-reconcile
    // client-side rule). Charts for the new Case replay via
    // activeSession.charts below; on null (no active Case) we clear.
    setCharts([]);
    // M5.5: the ImpactPanel is per-Case ephemeral state. Without this reset
    // the slide-out from the previous Case bled into the next Case on switch
    // (same client-side replace-not-reconcile gap as charts). It re-populates
    // when the new Case's agent emits a fresh impact-envelope.
    setImpactEnvelope(null);

    if (activeSession === null) {
      // job-0357: Case EXIT is an AUTHORITATIVE clear  -  replace_layers:true so
      // Map.tsx tears down the prior Case's overlays (fresh slate). This is the
      // explicit Case-switch path the durability fix must KEEP clearing; only a
      // WS reconnect (server snapshot received while not `connected`) is exempt.
      bus.pushSessionState({
        loaded_layers: [],
        chat_history: [],
        pipeline_history: [],
        current_pipeline: null,
        map_view: null,
        replace_layers: true,
      });
      // ux-batch-1 (F14): exiting a Case must reset client map state, not just
      // the panels. The analysis-extent (AOI) rectangle is drawn directly on
      // the map by Map.tsx and is NOT part of loaded_layers, so clearing
      // session-state above does not remove it. Emit an explicit clear so the
      // prior Case's AOI outline does not linger on the root/new-case map.
      bus.pushMapCommand({
        command: "clear-analysis-extent",
      } as unknown as MapCommandPayload);
      // ux-batch-1 (F-CASES-CLEAR-ALL): also snap the camera back to the
      // default CONUS view so leaving a Case visibly resets the map (the empty
      // session-state above clears the data layers; this resets the camera).
      bus.pushMapCommand({
        command: "reset-view",
      } as unknown as MapCommandPayload);
      return;
    }
    // job-0357: opening / switching INTO a Case is an AUTHORITATIVE replace  - 
    // replace_layers:true so the new Case's loaded_layers replace whatever the
    // previously-viewed Case had on the map (a Case switch still clears, per
    // the durability requirement). The reconnect exemption only applies to
    // server-delivered snapshots received while the socket is not `connected`.
    bus.pushSessionState({
      loaded_layers: activeSession.loaded_layers ?? [],
      chat_history: activeSession.chat_history ?? [],
      pipeline_history: activeSession.pipeline_history ?? [],
      current_pipeline: activeSession.current_pipeline ?? null,
      map_view: null,
      replace_layers: true,
    });
    // JOB WEB-AOI-LEGEND (#159)  -  snap to the FINAL/floored AOI. Prefer
    // `case.bbox` (the agent-AOI job now persists the FLOORED bbox there), but
    // validate it with the SAME finite-4-number guard the zoom-to replay path
    // uses (asBbox)  -  a null / malformed / non-finite persisted bbox must NOT
    // produce a broken fitBounds; it falls through to the last zoom-to instead.
    const caseBbox = asBbox(activeSession.case.bbox);
    if (caseBbox) {
      bus.pushMapCommand({
        command: "zoom-to",
        args: { bbox: caseBbox },
      } as unknown as MapCommandPayload);
    } else {
      // job-0280  -  Case-open snap-to-location. `CaseSummary.bbox` is null in
      // practice today (and the agent-AOI floored bbox may not be persisted on
      // older Cases), so fall back to replaying the LAST `zoom-to` the Case's
      // persisted turns emitted (CaseChatMessage.map_command_emissions in the
      // rehydrated chat_history) through the SAME bus -> Map.tsx fitBounds path.
      // extractLastZoomTo walks newest-first, so this is the LATEST (floored)
      // zoom-to  -  never the first/small pre-floor one. No zoom-to anywhere in
      // history -> leave the camera alone (root/new Cases unchanged).
      const replay = extractLastZoomTo(activeSession.chat_history);
      if (replay) {
        bus.pushMapCommand(replay as unknown as MapCommandPayload);
      } else {
        // ux-batch-1 (F14): this Case has no AOI of its own (no bbox, no
        // zoom-to replay). ALWAYS clear any extent left over from the
        // previously viewed Case so switching into a no-AOI Case doesn't
        // inherit a stale rectangle (the Fort-Myers-bbox-shows-in-Chehalis
        // bleed). A Case WITH an AOI replaces the extent via the zoom-to above.
        // (The earlier F28 "skip clear when the Case has layers" was a wrong
        // band-aid: the bleed was actually the dead-basemap stall swallowing
        // the clear command, fixed by the CartoDB basemap swap  -  so the
        // unconditional clear is correct and bleed-free again.)
        bus.pushMapCommand({
          command: "clear-analysis-extent",
        } as unknown as MapCommandPayload);
      }
    }
    // Rehydrate charts from session. ``activeSession.charts`` is the
    // append-only array persisted via SessionChartRecord (sprint-13 schema).
    // When the field is absent (older sessions) or empty, charts stays [].
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const sessionCharts = (activeSession as any).charts as ChartPayload[] | undefined;
    if (Array.isArray(sessionCharts) && sessionCharts.length > 0) {
      setCharts(sessionCharts.filter(
        (c) => c && typeof c.chart_id === "string" && c.vega_lite_spec,
      ));
    }
  }, [activeSession, bus]);

  // sleep/wake STAGE 2 (NATE 2026-06-18)  -  COLD-LOAD a Case when the agent box
  // is asleep. "Pen = agent, paper = case": the case must PAINT even with the
  // agent (the pen) asleep. When the user opens a Case while the App socket is
  // NOT connected, the WS `select` only QUEUES (ws.ts sendOrQueue)  -  it never
  // reaches the agent, so NO `case-open` envelope comes back and the Case never
  // paints. This effect fills that gap: it fetches the agent's persisted S3
  // view-state snapshot via the signer (GET VITE_GRACE2_CASE_VIEW_URL) and feeds
  // the resulting CaseOpenEnvelopePayload through the SAME useCases_onCaseOpen
  // path the live WS uses, so rasters AND inline vectors paint with ZERO new
  // render code.
  //
  // Triggers when: a Case is active (activeCaseId) AND cold-load is configured
  //   AND we haven't already painted a live session for this Case AND the App
  //   socket is NOT connected (connecting / reconnecting / disconnected). The
  //   queued WS `select` stays in flight in parallel; if the box later wakes,
  //   the LIVE case-open re-runs onCaseOpen and supersedes the cold snapshot
  //   (replace_layers semantics + idempotent rail upsert handle the swap).
  //
  // 404 (no snapshot for this Case  -  never materialised) -> fetchCaseView
  //   returns null -> we leave the Case shell + Wake UI (NOT an error).
  //
  // The ref guards against a re-fetch storm while reconnecting: at most one
  //   cold-load per (caseId) while disconnected; a healthy reconnect or a Case
  //   switch resets it so a later disconnect can cold-load again.
  const coldLoadedCaseRef = useRef<string | null>(null);
  // The signed-in identity (uid, or "anon"). Shared by BOTH cold-load effects:
  // the cold-VIEW effect re-arms on a TOKEN-readiness identity flip (so load 1
  // paints the OWNER snapshot, no 2nd reload), and the cold-LIST effect keys its
  // guard to it. Defined here (above the cold-VIEW effect) so both can read it.
  const coldListIdentity = isSignedIn ? authUser?.uid ?? "signed-in" : "anon";
  // DOUBLE-REFRESH FIX (NATE 2026-06-26): once a cold-VIEW attempt for the
  // ACTIVE Case has RESOLVED (success OR a clean null/abort), record it so the
  // layers spinner can stop forcing itself purely on transient wsStatus
  // oscillation (box-off the WS never connects, so connecting<->reconnecting
  // flaps forever). Keyed to the caseId so a Case switch re-arms the spinner.
  const [coldViewAttemptedCaseId, setColdViewAttemptedCaseId] = useState<
    string | null
  >(null);
  // DOUBLE-REFRESH FIX (NATE 2026-06-26): reset the cold-load-view guard the
  // instant the App socket goes healthy - a live case-open is now authoritative
  // and a future disconnect should be allowed to cold-load again. Its OWN tiny
  // effect keyed on connected-ness so it does NOT re-run (and tear down the
  // in-flight fetch in the effect below) on every connecting<->reconnecting
  // flap while the box is asleep.
  useEffect(() => {
    if (wsStatus === "connected") {
      coldLoadedCaseRef.current = null;
    }
  }, [wsStatus]);
  // DOUBLE-REFRESH FIX (NATE 2026-06-26): depend on a COARSE notConnected
  // boolean, not raw wsStatus, so the box-off connecting<->reconnecting
  // oscillation (ws.ts CONNECT_ATTEMPT_TIMEOUT, ~10s) does NOT re-run this
  // effect and cancel the in-flight cold-fetch. Only a real transition
  // to/from "connected" flips this boolean.
  const notConnected = wsStatus !== "connected";
  useEffect(() => {
    if (!notConnected) return;
    if (!caseViewConfigured()) return;
    if (activeCaseId === null) return;
    // Already have the live session for this Case (it round-tripped over the WS
    // before the drop)  -  nothing to cold-load.
    if (activeSession && activeSession.case.case_id === activeCaseId) return;
    // Already cold-loaded this Case during the current disconnected episode.
    if (coldLoadedCaseRef.current === activeCaseId) return;

    // DOUBLE-REFRESH FIX (NATE 2026-06-26): do NOT advance the guard yet - it
    // now latches ONLY on a SUCCESSFUL fetch (success branch below). A cancel /
    // null / empty-token path always leaves it released so the next arm (a
    // token-ready identity flip, or a fresh notConnected episode) re-fetches.
    let cancelled = false;
    // #147 Feature B GAP B2 - forward the signed-in owner's Cognito bearer token
    // (the SAME token ws.ts sends in the `auth-token` handshake) to the signer
    // hop so the view_sign Lambda owner-gates it and mints the 12h OWNER-tier
    // pre-signed URL instead of the anon 15min TTL. Anonymous users (no token)
    // pass `undefined`, so the anon tier is unchanged. getIdToken() never throws
    // here (the .catch collapses any auth-subsystem failure to null -> undefined
    // 3rd arg), so a token hiccup degrades gracefully to the anon path.
    void (async () => {
      const rawToken = await getIdToken().catch(() => null);
      if (cancelled) return;
      // DOUBLE-REFRESH FIX (NATE 2026-06-26): signed in but the token is not
      // ready yet -> do NOT burn the attempt on a tokenless request (the signer
      // would answer with the anon tier / an empty owner snapshot). Release the
      // guard + return so the coldListIdentity-keyed re-run (added to the deps)
      // retries the instant the warm token arrives - so load 1 paints the
      // OWNER snapshot, no 2nd reload needed.
      if (
        isSignedIn &&
        (rawToken == null || rawToken.trim() === "")
      ) {
        coldLoadedCaseRef.current = null;
        return;
      }
      const authToken =
        rawToken != null && rawToken.trim() !== "" ? rawToken : undefined;
      const payload = await fetchCaseView(activeCaseId, undefined, authToken);
      if (cancelled) {
        // DOUBLE-REFRESH FIX (NATE 2026-06-26): a teardown ALWAYS releases the
        // guard (no `!cancelled` gate) so the next arm re-fetches - the old
        // code latched the guard on cancel, wedging the cold-load forever.
        coldLoadedCaseRef.current = null;
        return;
      }
      if (payload === null) {
        // COLD-VIEW GATE FIX (NATE 2026-06-28): a null result is TRANSIENT (no
        // snapshot yet / signer wedged / S3 hop aborted / parse error -
        // fetchCaseView collapses every failure to null), NOT a definitive
        // answer for this Case. Do NOT latch coldViewAttemptedCaseId here: a
        // Case that HAS persisted layers but cold-resolved null must stay
        // RETRYABLE so the next cold attempt / connection change self-heals it
        // WITHOUT a manual refresh (the refresh-then-paints bug). Release the
        // guard so a later attempt in the same disconnected episode re-fetches.
        // The 12s coldSettleTimedOut bound (below) still stops the spinner so it
        // never hangs forever; the empty/restore stub then renders the honest
        // "wake the agent to restore layers" copy for a Case the durable summary
        // says HAD layers.
        coldLoadedCaseRef.current = null;
        return;
      }
      // DEFINITIVE RESOLUTION: a non-null payload is the authoritative cold
      // answer for THIS Case (a genuinely-empty case still returns a valid
      // payload with empty loaded_layers). Mark the attempt RESOLVED so the
      // spinner falls through to the honest empty / restore stub box-off, and
      // latch the guard so we don't refetch this Case while still disconnected.
      // Then feed the cold snapshot through the SAME path the live WS case-open
      // uses; the rehydration effect ([activeSession, bus]) paints it. A later
      // live case-open supersedes idempotently.
      setColdViewAttemptedCaseId(activeCaseId);
      coldLoadedCaseRef.current = activeCaseId;
      useCases_onCaseOpen(payload);
      // job-0179  -  ALSO push the case-open onto the bus so Chat (which does
      // not subscribe to App's useCases state) can materialize the COLD
      // chat-history bubbles via routeCaseOpen. Idempotent vs the live WS
      // onCaseOpen below: routeCaseOpen only rebuilds a stream the first time
      // it sees the caseId, so whichever fires first wins and the other is a
      // no-op.
      bus.pushCaseOpen(payload);
    })();
    return () => {
      cancelled = true;
    };
    // coldListIdentity (the signed-in uid / "anon") re-arms this effect on TOKEN
    // readiness: an empty-token early return above releases the guard, and the
    // identity flip on sign-in / token-ready re-runs the fetch with the warm
    // token. notConnected (coarse) replaces raw wsStatus so connect-attempt
    // oscillation does NOT cancel the in-flight fetch.
  }, [
    activeCaseId,
    notConnected,
    coldListIdentity,
    isSignedIn,
    activeSession,
    useCases_onCaseOpen,
    bus,
  ]);

  // sleep/wake STAGE 2 (NATE 2026-06-19) - COLD-LOAD the Cases LIST when the
  // agent box is asleep. SIBLING of the case-VIEW cold-load above: that paints
  // ONE open Case; this paints the Cases ROOT (the left rail) so "paper" renders
  // even with the "pen" (the agent) asleep. When the App socket is NOT connected
  // the WS never delivers a `case-list` frame, so the rail would stay empty until
  // the box wakes. We GET the serverless /case-list snapshot (lib/case_list) and
  // feed it through the SAME useCases_onCaseList path the live WS uses - but with
  // isAuthoritative=true, so a genuinely-empty cold list correctly shows zero
  // cases (the LAST-CASE EDGE FIX in useCases.onCaseList).
  //
  // Fires ONCE per (signed-in identity) ref guard only while: the App socket is
  // NOT connected AND cold-load is configured AND NO authoritative list has been
  // applied yet (`!casesSettled`). TASK C (NATE 2026-06-26): the guard is
  // `casesSettled`, NOT `cases.length === 0` - on reload the cold case-VIEW
  // effect optimistically upserts the ONE restored case into cases[], so the old
  // length guard bailed and the full /case-list was never fetched (rail showed
  // only the restored case); `casesSettled` flips only on a real onCaseList
  // frame, so a lone cold-VIEW upsert no longer suppresses the cold list. A
  // later live `case-list` over the WS supersedes it (non-empty replaces; the
  // reconcile is idempotent). Gated to dev/LAN safety by caseListConfigured()
  // (null endpoint -> no fetch).
  //
  // COLD-LIST SIGNED-IN FIX (NATE 2026-06-19)  -  NATE is signed in (Cognito) but
  // saw an EMPTY rail with the box asleep. Root cause: the effect fired on mount
  // BEFORE auth resolved, so `getIdToken()` returned null; the Lambda's auth
  // contract answers a tokenless request with an AUTHORITATIVE 200 EMPTY list
  // (never 401), which is a non-null payload  -  so the old guard LATCHED `true`
  // and NEVER re-fetched once the token arrived (its deps `[wsStatus,
  // cases.length, ...]` don't change on sign-in). Two changes fix it:
  //   1. Depend on the signed-in identity (`coldListIdentity` = the uid, or
  //      "anon"); the guard is keyed to it (`coldLoadedListIdRef`) so when auth
  //      resolves / the user signs in, the identity flips and the effect RE-RUNS
  //      with the now-available token.
  //   2. NEVER cold-load WITHOUT the token when the user IS signed in: if
  //      `isSignedIn` but `getIdToken()` came back null (token not ready yet),
  //      skip the fetch and release the guard so the next identity-keyed run (or
  //      a token-ready re-render) retries  -  we must not burn the one attempt on
  //      a tokenless request that the Lambda would answer empty.
  const coldLoadedListIdRef = useRef<string | null>(null);
  useEffect(() => {
    // Reset the cold-load-list guard whenever the App socket goes healthy:
    // a live `case-list` is now authoritative and a future disconnect should
    // be allowed to cold-load the rail again. Mirrors coldLoadedCaseRef's
    // reset-on-reconnect above; without this the ref latched forever after the
    // first cold session, so a later cold session never re-fetched.
    if (wsStatus === "connected") {
      coldLoadedListIdRef.current = null;
      return;
    }
    // Already cold-loaded for THIS identity during the current disconnected
    // episode. A sign-in (identity flip) clears this by inequality below.
    if (coldLoadedListIdRef.current === coldListIdentity) return;
    if (!caseListConfigured()) return;
    // TASK C FIX (NATE 2026-06-26): gate on whether an AUTHORITATIVE list has
    // been applied, NOT on cases.length. On reload, 371caa3 seeds activeCaseId
    // non-null, so the cold case-VIEW effect runs first and OPTIMISTICALLY
    // UPSERTS the single restored case into cases[]; the old `cases.length > 0`
    // guard then bailed and the full /case-list was NEVER fetched -> the rail
    // showed ONLY the restored case. `casesSettled` flips only on a real
    // onCaseList frame (an onCaseOpen upsert does NOT set it), so a lone
    // optimistic cold-VIEW upsert no longer counts as "rail already loaded" and
    // the cold /case-list always runs once -> the FULL rail loads.
    if (casesSettled) return;

    coldLoadedListIdRef.current = coldListIdentity;
    let cancelled = false;
    void (async () => {
      const token = await getIdToken().catch(() => null);
      if (cancelled) return;
      // Signed in but the token is not ready yet  -  do NOT spend the attempt on a
      // tokenless request (the Lambda would answer an authoritative empty list).
      // Release the guard so an identity-keyed / token-ready re-run retries.
      if (isSignedIn && (token == null || token.trim() === "")) {
        coldLoadedListIdRef.current = null;
        return;
      }
      const payload = await fetchCaseList(undefined, token);
      // A failed / null cold-load releases the guard so a later attempt in the
      // same disconnected episode can re-fetch (mirrors coldLoadedCaseRef on
      // fetch failure). A successful non-null payload keeps the guard set for
      // this identity.
      if (cancelled || payload === null) {
        if (!cancelled) coldLoadedListIdRef.current = null;
        return;
      }
      // Cold FETCH is AUTHORITATIVE: an empty list genuinely means zero cases
      // (clears the rail); a non-empty list paints it.
      useCases_onCaseList(payload, true);
    })();
    return () => {
      cancelled = true;
    };
  }, [wsStatus, casesSettled, coldListIdentity, isSignedIn, useCases_onCaseList]);

  // Lift layers from session-state.
  //
  // job-0179 (per-Case client cache  -  "the seatbelt"): route the incoming layer
  // SET through cache.mergeSnapshot so a STALE / partial / reconnect frame that
  // OMITS a layer ADDS/REFRESHES but never EVICTS it; the rendered list comes
  // from cache.layersFor(active Case). `replace_layers` is App's authoritative
  // stamp (true on a Case switch/exit or a healthy non-empty server frame;
  // false on a transient reconnect frame)  -  it gates whether omitted layers may
  // be dropped. At the root (no active Case) there is no Case to cache against,
  // so mergeSnapshot passes the list through verbatim (byte-identical to before).
  useEffect(() => {
    const unsub = bus.subscribeSessionState((p) => {
      // CASES-ROOT NO-LAYERS GATE (NATE 2026-06-22)  -  at the cases-list / root
      // view (no Case entered) the LayerPanel-feeding `layers` list is empty, the
      // same gate Map.tsx applies to the map overlays + legend. A Case is entered
      // iff the shared cache's activeCaseId is non-null (App keeps it in lockstep
      // with activeCaseId). This keeps the rail / legend / map consistent: no
      // case layers anywhere until a Case is entered.
      // NATE item 1 - track the long-running-sim signal off the live pipeline
      // snapshot so the bbox overlay can paint the PURPLE scan border. Derived
      // here (vs a separate subscription) because this is the one place App sees
      // every session-state frame. Tolerant of the loosely-typed field.
      //
      // FLASH FIX (Lane 1b): GUARD the setState so an UNCHANGED pipeline/sim
      // state does not force an App re-render on every ~25s keepalive frame. The
      // functional updater returns the SAME boolean when it is unchanged, so
      // React bails (no re-render of App -> the panel/scrubber subtree).
      const nextSim = isPipelineRunning(
        (p as { current_pipeline?: unknown }).current_pipeline ?? null,
      );
      setSimRunning((prev) => (prev === nextSim ? prev : nextSim));

      const caseId = layerCache.activeCaseId;
      if (caseId === null) {
        setLayers((prev) => (prev.length === 0 ? prev : []));
        return;
      }
      const incoming = p.loaded_layers ?? [];
      const authoritativeReplace =
        (p as { replace_layers?: boolean }).replace_layers !== false;
      const merged = layerCache.mergeSnapshot(caseId, incoming, {
        authoritativeReplace,
      });
      // FLASH FIX (Lane 1a): mergeSnapshot now returns the SAME array instance
      // for an identical heartbeat, so this setState is already a no-op then.
      // The content-equality guard is belt-and-suspenders: skip the state change
      // when `merged` is ref-equal OR structurally equal to the prior layers, so
      // even a distinct-but-identical array can never re-render the subtree.
      setLayers((prev) =>
        prev === merged || layerListsEqual(prev, merged) ? prev : merged,
      );
    });
    return unsub;
  }, [bus, layerCache]);

  // Dev-only debug seam.
  useEffect(() => {
    if (!import.meta.env.DEV) return;
    window.__grace2InjectSessionState = (p) => bus.pushSessionState(p);
    window.__grace2InjectMapCommand = (p) => bus.pushMapCommand(p);
    window.__grace2InjectSecretsList = (p) => setSecrets(p.secrets ?? []);
    window.__grace2InjectSourceSuggestion = (p) => fanoutSourceSuggestion(p);
    window.__grace2InjectCaseList = (p) => useCases_onCaseList(p);
    window.__grace2InjectCaseOpen = (p) => useCases_onCaseOpen(p);
    window.__grace2InjectImpactEnvelope = (p) => setImpactEnvelope(p);
    // sprint-13 job-0231: chart injection seam for Playwright snapshots.
    // App.tsx owns the window seam; Chat.tsx receives the fan-out via
    // its own GraceWs onChartEmission handler (SESSION_SCOPED_TYPES hub).
    window.__grace2InjectChartEmission = (p) => handleChartEmission(p);
    window.__grace2ClearCharts = () => {
      // App.tsx chart state is the authoritative reset source. Charts in
      // Chat.tsx are reset separately via its own case-open handler.
      setCharts([]);
    };
    // Expose the current chart count for Playwright introspection.
    (window as unknown as Record<string, unknown>).__grace2ChartCount = () => charts.length;
    return () => {
      delete window.__grace2InjectSessionState;
      delete window.__grace2InjectMapCommand;
      delete window.__grace2InjectSecretsList;
      delete window.__grace2InjectSourceSuggestion;
      delete window.__grace2InjectCaseList;
      delete window.__grace2InjectCaseOpen;
      delete window.__grace2InjectImpactEnvelope;
      delete window.__grace2InjectChartEmission;
      delete window.__grace2ClearCharts;
    };
  }, [bus, fanoutSourceSuggestion, useCases_onCaseList, useCases_onCaseOpen, handleChartEmission]);

  // job-0179  -  per-layer delete is an EXPLICIT eviction: drop the layer (and its
  // persisted view-override) from the shared cache so the seatbelt's allowsEvict
  // permits Map.tsx to tear the overlay down, THEN tell the server (which echoes
  // a fresh session-state sans the layer). Without the cache delete, allowsEvict
  // would protect the just-deleted layer against the authoritative replace.
  const handleDeleteLayer = useCallback(
    (id: string): void => {
      layerCache.deleteLayer(layerCache.activeCaseId, id);
      wsRef.current?.sendDeleteLayer(id);
    },
    [layerCache],
  );

  // job-0125: bridge SecretsPanel callbacks to the active GraceWs.
  function handleSecretAdd(payload: {
    provider: ProviderID;
    case_id: string | null;
    label: string | null;
    key_value: string;
  }): void {
    if (!wsRef.current) return;
    wsRef.current.sendSecretAdd(payload);
  }

  function handleSecretRevoke(secretId: string): void {
    if (!wsRef.current) return;
    wsRef.current.sendSecretRevoke(secretId);
  }

  const showLayersHamburger = leftCollapsed;
  const showChatHamburger = rightCollapsed;

  // Session-durability Job E (NATE): layersLoading is a HOOK and MUST be computed
  // BEFORE the AuthGate early-return below so it runs unconditionally on every
  // render. A hook placed after the gate return only runs once auth resolves,
  // which trips React error #310 (more hooks than the previous render) on the
  // unauthed->authed transition and blanks the whole app. Derived from existing
  // signals; FALSE when the Case is open + settled + socket healthy, so a
  // genuinely-empty Case shows the empty stub (never spins forever).
  //
  // BUG 1 (NATE 2026-06-23) - the loading scan must clear once the ACTIVE Case's
  // layers are actually PRESENT, not only when the WS SESSION settles
  // (activeSession.case.case_id === activeCaseId). On a switch between two Cases
  // with the SAME bbox the new Case's layers render from cold-view/cache, but the
  // session-settle signal lags (or never fires while the box is busy/asleep), so
  // the scan kept running OVER already-loaded layers. Treat layers-present for the
  // active Case as settled: `layers` is keyed to the active Case (mergeSnapshot
  // by layerCache.activeCaseId; [] at the root), so layers.length > 0 means the
  // ACTIVE Case has painted. A genuinely-empty Case (layers.length 0) still waits
  // on the session-settle signal, so it shows the empty stub once settled rather
  // than spinning forever (the existing comment's intent is preserved).
  const caseSelectedButUnsettled =
    activeCaseId !== null &&
    layers.length === 0 &&
    (activeSession === null ||
      activeSession.case.case_id !== activeCaseId);

  // DOUBLE-REFRESH FIX (NATE 2026-06-26): BOUND the spinner box-off. The spinner
  // ties to wsStatus connecting/reconnecting, which box-off NEVER clears (the WS
  // can't connect, so it flaps connecting<->reconnecting forever) -> an endless
  // "Loading layers...". Arm a ~12s cold-settle timer (> the ~10s connect-attempt
  // timeout) whenever a Case is active, the socket is NOT connected, and we still
  // have zero layers; once it fires (OR the cold-VIEW attempt for this Case has
  // resolved) we STOP forcing the spinner purely on transient wsStatus and fall
  // through to the honest empty / Wake stub. Reset per (activeCaseId) so a Case
  // switch re-arms it; cleared the instant layers paint or the socket connects.
  const COLD_SETTLE_MS = 12_000;
  const [coldSettleTimedOut, setColdSettleTimedOut] = useState(false);
  useEffect(() => {
    setColdSettleTimedOut(false);
    if (activeCaseId === null) return;
    if (wsStatus === "connected") return;
    if (layers.length > 0) return;
    const t = setTimeout(() => setColdSettleTimedOut(true), COLD_SETTLE_MS);
    return () => clearTimeout(t);
  }, [activeCaseId, wsStatus, layers.length]);

  // The cold-VIEW attempt for the ACTIVE Case has RESOLVED (success/null/abort).
  const coldViewSettledForCase =
    activeCaseId !== null && coldViewAttemptedCaseId === activeCaseId;

  const layersLoading = useMemo(
    () => {
      if (activeCaseId === null) return false;
      // Genuine pre-paint loading: a Case is selected but its layers are not
      // present yet AND no cold-load attempt has settled / timed out. Once the
      // cold-VIEW attempt resolved OR the cold-settle timer fired, do NOT keep
      // the spinner alive on transient wsStatus oscillation - show the honest
      // empty / Wake stub instead.
      const coldDone = coldViewSettledForCase || coldSettleTimedOut;
      if (caseSelectedButUnsettled && !coldDone) return true;
      // Transport churn still forces the spinner WHILE a cold-load could still
      // resolve; once cold-done it no longer does (box-off would spin forever).
      if (!coldDone && (wsStatus === "connecting" || wsStatus === "reconnecting"))
        return true;
      return false;
    },
    [
      activeCaseId,
      caseSelectedButUnsettled,
      wsStatus,
      coldViewSettledForCase,
      coldSettleTimedOut,
    ],
  );

  // LANE B #4 (no-replay): suppress the loading shimmer REPLAY on a re-enter /
  // same-bbox switch when the layers are already present and the active Case +
  // bbox are unchanged since the last paint. Track the last painted
  // (caseId, bboxKey) in a ref; update it whenever the active Case has layers
  // painted and is NOT loading. `suppressLoadingReplay` is true when the current
  // context matches that last paint AND layers are present - i.e. nothing
  // genuinely new is being fetched, so any transient `layersLoading` (a
  // reconnect / re-select / resume re-pull) must not re-arm the shimmer. A
  // genuine NEW fetch (different case, different bbox, or no layers yet) is not
  // suppressed. This is a ref read, not a hook, so it does not affect the #310
  // hook-count rule.
  const lastPaintedRef = useRef<{ caseId: string | null; bboxKey: string } | null>(
    null,
  );
  const caseBboxKey = useMemo(() => {
    const b = asBbox(activeSession?.case.bbox ?? null);
    return b ? b.join(",") : "";
  }, [activeSession]);
  const suppressLoadingReplay =
    layers.length > 0 &&
    lastPaintedRef.current !== null &&
    lastPaintedRef.current.caseId === activeCaseId &&
    lastPaintedRef.current.bboxKey === caseBboxKey;
  useEffect(() => {
    // Record the last paint once the active Case's layers are present and the
    // load has settled, so a subsequent same-context re-enter is recognized as a
    // replay (and suppressed above).
    if (activeCaseId !== null && layers.length > 0 && !layersLoading) {
      lastPaintedRef.current = { caseId: activeCaseId, bboxKey: caseBboxKey };
    }
  }, [activeCaseId, layers.length, layersLoading, caseBboxKey]);

  // NATE item 1 - resolve the AOI-bbox loading-animation render descriptor from
  // the live signals (pure state machine, unit-tested in lib/bbox_progress). Must
  // be a HOOK computed BEFORE the AuthGate early-return (same #310 rule as
  // layersLoading above). `connecting` is exempt from the user toggle inside the
  // resolver, so a transport drop always shows the scan border. `hasBbox` is the
  // projected AOI rect being present (the overlay's anchor). LANE E threads
  // `terrain3d` so 3D suppresses the (misaligned) 2D overlay in favor of the
  // in-map line glow; LANE B #4 threads `suppressLoadingReplay`.
  const bboxProgress = useMemo(
    () =>
      resolveBboxProgress({
        hasBbox: aoiScreenRect !== null,
        layerCount: layers.length,
        layersLoading,
        connecting: wsStatus === "connecting" || wsStatus === "reconnecting",
        simRunning,
        animationsEnabled: bboxAnimEnabled,
        terrain3d: terrain3dEnabled,
        suppressLoadingReplay,
      }),
    [
      aoiScreenRect,
      layers.length,
      layersLoading,
      wsStatus,
      simRunning,
      bboxAnimEnabled,
      terrain3dEnabled,
      suppressLoadingReplay,
    ],
  );

  // ITEM 1 (NATE 2026-06-22) - does the CURRENT case context already have an AOI
  // set? The always-on Draw-AOI control group (draw + red-X + green "+") is for
  // STARTING a case: setting the AOI to begin. Once a case has a bounding box it
  // must NOT render. "Has an AOI" = any of: a valid persisted case.bbox, an AOI
  // rectangle currently projected on the map (aoiScreenRect, the agent set one),
  // or the case already carries loaded layers (established work). When no case is
  // entered (activeCaseId === null, the cases root), the control SHOWS so a fresh
  // bbox can be drawn + confirmed to seed a brand-new case (item 4). Must be a
  // HOOK before the AuthGate early-return (same #310 rule as above).
  const caseHasAoi = useMemo(() => {
    if (activeCaseId === null) return false; // cases root: allow drawing to seed.
    const persisted = asBbox(activeSession?.case.bbox ?? null) !== null;
    return persisted || aoiScreenRect !== null || layers.length > 0;
  }, [activeCaseId, activeSession, aoiScreenRect, layers.length]);

  // CASE-LIST LOADING SIGNAL (BUG 1: late spinner). True while the FIRST
  // case-list load is plausibly in flight AND has not yet settled, so the
  // CasesPanel shows its spinner IMMEDIATELY (on first paint, before the WS even
  // connects) instead of flashing the "no cases" empty stub. `casesSettled`
  // (from useCases) flips true on the first list frame of ANY source -> the
  // spinner turns off the moment the list arrives (rows or genuine empty).
  //
  // We GUARD against a forever-spinner: only spin while a load is actually
  // expected - the WS is connecting / reconnecting / connected (a live
  // `case-list` is inbound) OR a serverless cold-list fetch is configured (the
  // box-off path GETs /case-list). When NOTHING will load (dev with no cold-list
  // endpoint and a disconnected socket) we fall back to the settled empty stub
  // rather than spinning indefinitely. The signal is only consulted by
  // CasesPanel when the rail is empty, so a populated rail never spins.
  const casesListLoading = useMemo(
    () =>
      !casesSettled &&
      (wsStatus === "connected" ||
        wsStatus === "connecting" ||
        wsStatus === "reconnecting" ||
        caseListConfigured()),
    [casesSettled, wsStatus],
  );

  // job-0138: AuthGate full-screen gating (the anonymous-accept gate). job-0253
  // wraps it in AuthGuard: when Firebase is DISABLED (dev/tailnet  -  every
  // current session), AuthGuard is a transparent pass-through and this renders
  // exactly as before. When Firebase is ENABLED + signed-out (production),
  // AuthGuard renders its own Google-only sign-in surface and the anonymous
  // gate below is never reached (Decision 6  -  no anonymous in prod).
  if (!appShouldRender) {
    // TRID3NT LOCAL (F5): never mount a sign-in surface. The only way to be
    // here locally is the (near-instant) initAuth() anonymous resolve window
    // (anonymousAccepted is seeded true above), so render nothing rather than
    // flashing the gate. Cloud renders the exact prior gate stack.
    if (isLocalDeployment()) {
      return null;
    }
    return (
      <AuthGuard authExpired={authExpired}>
        <AuthGate onAnonymousAccept={handleAnonymousAccept} />
      </AuthGuard>
    );
  }

  // job-0143: derive the active Case object for the breadcrumb title.
  const activeCase = activeCaseId
    ? cases.find((c) => c.case_id === activeCaseId) ?? null
    : null;

  // (layersLoading + caseSelectedButUnsettled are computed ABOVE the AuthGate
  // gate return - see the Job E note there - so the hook runs unconditionally.)

  // COLD-VIEW GATE FIX (NATE 2026-06-28): the DURABLE signal that this Case HAD
  // layers, read from its CaseSummary (the cold /case-list already painted the
  // rail box-off, so activeCase is present even with the agent asleep). The
  // summary carries layer_summary (flat layer_ids) and loaded_layer_summaries
  // (persisted ProjectLayerSummary snapshots) - either being non-empty means
  // DynamoDB persists layers for this Case. When the cold-view then resolves
  // with ZERO live layers (a transient null that timed out, or an empty frame),
  // we render an HONEST "wake the agent to restore layers" stub instead of the
  // bare "add layers" empty (which falsely implies the Case is new/empty and is
  // why a manual refresh - a fresh cold attempt - then paints the real layers).
  // A genuinely-new/empty Case has NO durable layer count, so it correctly
  // keeps the "Ask the assistant to add data." empty.
  const caseHadLayers =
    (activeCase?.layer_summary?.length ?? 0) > 0 ||
    (activeCase?.loaded_layer_summaries?.length ?? 0) > 0;
  // Show the restore stub only once the spinner has stopped (not loading), the
  // live layer list is empty, and the durable summary says this Case HAD layers.
  const showRestoreLayersStub =
    !layersLoading && layers.length === 0 && caseHadLayers;

  // FIX 2  -  payload-warning gates render in Chat's per-Case stream now (no
  // App-level filtering / banner). See Chat.tsx routePayloadWarning.

  // job-0253  -  AuthGuard wraps the app shell. DISABLED (dev/tailnet) -
  // transparent pass-through, pixel-identical render. ENABLED + signed-in -
  // children render + a minimal "Sign out" affordance. ENABLED + expired -
  // back to the sign-in surface.
  return (
    <AuthGuard authExpired={authExpired}>
    <div
      data-testid="grace2-app-shell"
      style={{
        position: "fixed",
        inset: 0,
      }}
    >
      {/* Full-bleed map  -  first in DOM so panels render above it. */}
      <MapView
        subscribeSessionState={bus.subscribeSessionState}
        subscribeMapCommand={bus.subscribeMapCommand as MapCommandSubscribeFunc}
        theme={theme}
        /* Lift the projected AOI rect so the SequenceScrubber (inside
           LayerPanel) can pin bottom-center of the AOI box like the legend. */
        onAoiScreenRectChange={setAoiScreenRect}
        /* ZOOM-OUT HIDE (NATE 2026-06-27, mobile-only) - lift the "AOI bbox is a
           tiny dot on screen" signal so the SequenceScrubber can hide when the
           user zooms OUT far. The legend reads the same signal inside Map. */
        onAoiTooSmallToShowChange={setAoiTooSmallToShow}
        /* Item b (NATE 2026-06-20) + LANE D (NATE) - the legend show/hide is
           App-owned on BOTH platforms now. MOBILE: the toggle lives INSIDE the
           expanded Layers section. DESKTOP: it lives in BottomRowButtons next to
           Settings (LANE D). Either way the floating bottom-center pill is
           suppressed (suppressLegendShowPill always true). */
        /* NATE 2026-06-24: while the Settings panel is open, HIDE the docked
           desktop legend (a bottom overlay) so it does not overlap Settings.
           This is a transient VISUAL hide that does not touch the persisted
           legendHiddenDesktop toggle; closing Settings restores it. Mobile is
           unaffected (the legend lives inside the Layers section there). */
        legendHidden={
          isMobile ? legendHiddenMobile : legendHiddenDesktop || settingsOpen
        }
        onLegendHiddenChange={isMobile ? setLegendHiddenMobile : setLegendHiddenDesktop}
        suppressLegendShowPill={true}
        /* CASES-ROOT NO-LAYERS GATE (NATE 2026-06-22)  -  NATE: "no case layers
           should be loaded when we are in the cases section; they should only be
           rendered when we have entered a Case." When no Case is entered
           (activeCaseId === null, the cases-list / root view) MapView renders NO
           data overlays + an empty legend; entering a Case re-paints its layers.
           This extends the #122 reset-AOI-on-exit concept to every data layer. */
        caseActive={activeCaseId !== null}
        /* #170 AOI-first manual case-creation - the AoiPickerCard overlay mounts
           on the live map when this local signal is set (NOT the spatial bus). */
        aoiCaptureActive={aoiCaptureOpen}
        onAoiCaptureConfirm={onAoiCaptureConfirm}
        onAoiCaptureSkip={onAoiCaptureSkip}
        onAoiCaptureCancel={onAoiCaptureCancel}
        /* NATE FIX 2 - thread the chat panel geometry so the always-on Draw-AOI
           control rails to the LEFT of the chat panel (and tracks its dragged
           width), tucks under the chat-expand hamburger when collapsed, and
           keeps its plain top-right placement on mobile (bottom sheet). */
        chatWidthPx={chatWidth}
        chatCollapsed={rightCollapsed}
        mobile={isMobile}
        /* MOBILE SHEET-TOP DOCK (NATE 2026-06-24) - the on-screen Y of the chat
           sheet's top edge, threaded through to the mobile LayerLegend so its
           colorbar keys + collapsed pill dock to the chat-panel top (a clean
           band) instead of floating over the map. Null on desktop. */
        legendSheetTopPx={sheetTopPx}
        /* CHART-OVERLAY HIDE-LEGEND (NATE 2026-06-28, mobile) - when Chat's
           full-viewport ChartGallery is open, the LayerLegend renders nothing on
           mobile so it never paints above/around the chart. Default false;
           desktop ignores it (the legend sits below the z=10000 overlay). */
        legendChartOpen={chartGalleryOpen}
        /* LANE B #3 - the desktop left rail (CasesPanel / CaseView) is a fixed
           288px when open; 0 on mobile or when collapsed. Threaded so fitBounds
           pads the occluded left side and the AOI box centers in the visible
           gutter instead of snapping behind the rail. */
        leftPanelWidthPx={!isMobile && !leftCollapsed ? 288 : 0}
        /* NATE 2026-06-22 (item 4) - recolor the SINGLE on-map AOI rectangle to
           purple while a sim runs (revert to blue when done). No second box is
           drawn; the same blue analysis-extent box's stroke is mutated. */
        simRunning={simRunning}
        /* ITEM 1 - hide the Draw-AOI control group once the case has an AOI; it
           is only for STARTING a case (setting the AOI to begin). */
        caseHasAoi={caseHasAoi}
        /* NATE 2026-06-22 (item 6) + ITEM 4 / #170 - the green "+" confirm seeds a
           new case with the drawn AOI via createCase(null, bbox) (same channel as
           AoiPickerCard); the DrawAoiControl also fits the camera (draw-and-fit). */
        onAoiStageConfirm={onAoiStageConfirm}
        /* "3D terrain viz" first cut - the persisted 3D-terrain + contour flags.
           When terrain3dEnabled flips on, MapView enables MapLibre terrain
           (terrain-RGB DEM + hillshade + sky) and unlocks pitch/rotate; off
           restores the flat 2D camera. contoursEnabled is a stub seam for now. */
        terrain3dEnabled={terrain3dEnabled}
        contoursEnabled={contoursEnabled}
      />

      {/* NATE item 1 - AOI-bbox loading-animation overlay. Anchored to the
          projected AOI screen rect (aoiScreenRect, the same one the legend +
          scrubber pin against). The render mode/tone is decided by the pure
          resolveBboxProgress state machine off the live loading / connection /
          sim signals. The app shell is position:fixed;inset:0 (the rect coords
          are viewport-relative), so the overlay sits directly over the map. */}
      <BboxProgressOverlay
        rect={aoiScreenRect}
        mode={bboxProgress.mode}
        tone={bboxProgress.tone}
      />

      {/* MOBILE TOP FROST GRADIENT (NATE 2026-06-19)  - with the iOS status bar
          now translucent (apple-mobile-web-app-status-bar-style=black-translucent
          in index.html), the page runs UNDER the time/battery, so more map shows
          but those glyphs can wash out over a light basemap. This thin
          top-anchored dark->transparent gradient sits behind the status-bar area
          (height = the safe-area inset + a small amount) to keep them legible.
          pointer-events:none so the map underneath stays fully draggable; mobile
          only (desktop has no status bar to cover). z-index above the map but
          below the rail/hamburgers/overlays. */}
      {isMobile && (
        <div
          data-testid="grace2-mobile-top-frost"
          aria-hidden
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            height: "calc(env(safe-area-inset-top) + 14px)",
            pointerEvents: "none",
            zIndex: 15,
            background:
              "linear-gradient(180deg, rgba(10,11,15,0.55) 0%, rgba(10,11,15,0) 100%)",
          }}
        />
      )}

      {/* job-0321 F43  -  the layer legend/colorbar is no longer an App-level
          floating element. It now renders INSIDE Map.tsx, anchored to the
          bottom edge of the AOI bounding box (Group A owns that placement) so
          it reads as the key for that AOI. The App-level <LayerLegend> render
          (and its mobile-offset wrapper) is removed here. */}

      {/* Left rail (job-0143):
            - No active Case -> CasesPanel only (list view).
            - Active Case -> CaseView (breadcrumb + LayerPanel children).
          job-0278: desktop only  -  on mobile the SAME content rides in the
          slide-in MobileDrawer below. */}
      {!isMobile && !leftCollapsed && activeCaseId === null && (
        <div
          data-testid="grace2-left-rail"
          data-mode="cases-list"
          /* job-0283  -  scopes the desktop sleekness CSS (global.css) to the
             desktop rail only; the mobile drawer renders these components
             without this class and stays pixel-identical to job-0280. */
          className="grace2-desktop-rail"
          style={{
            position: "absolute",
            top: 12,
            left: 16,
            // cases-panel-layout (NATE 2026-06-20) - CONVERGE the desktop cases
            // rail onto the MOBILE cases-section presentation: content-sized but
            // CAPPED, with the inner list scrolling internally past the cap.
            //
            // The mobile mount (MobileDrawer hugger) is `flex:1` of a bounded
            // flex column whose footer (Settings pills + composer clearance)
            // reserves the bottom space, so the panel hugs its content, caps at
            // the available height, and its inner grace2-cases-list scrolls.
            //
            // The desktop wrapper previously stretched `top:12 -> bottom:12`
            // (a full-viewport-height bound) and CasesPanel height:100% filled
            // ALL of it, so the panel ran the entire left edge and OVERLAPPED
            // the bottom-left Settings pill (BottomRowButtons, position:absolute
            // left:12 bottom:12). To match mobile we drop the `bottom` anchor
            // and instead cap the column with a maxHeight that STOPS ABOVE the
            // Settings pill (the pill is ~44px tall at bottom:12, so reserve
            // ~72px = pill height + a 12px gap + the 12px top inset alignment).
            // With no fixed height the column is content-sized (a short list
            // hugs its rows, like mobile); when the content exceeds the cap the
            // column clips (overflow:hidden) and CasesPanel height:100% resolves
            // to the capped height so its inner list's overflowY:auto engages.
            maxHeight: "calc(100vh - 84px)",
            zIndex: 20,
            // flex column so CasesPanel's height:100% resolves against this
            // (now content-sized, max-capped) wrapper; minHeight:0 lets the
            // column squeeze so the inner list can scroll. overflow:hidden so
            // the cap clips to the inner list's own scroll region (matching the
            // mobile hugger's overflow:hidden - the LIST is the single scroll
            // container, never the wrapper).
            display: "flex",
            flexDirection: "column",
            minHeight: 0,
            overflow: "hidden",
          }}
        >
          <CasesPanel
            cases={cases}
            activeCaseId={activeCaseId}
            loading={casesListLoading}
            onCreate={onCreateGated}
            onSelect={onSelectCase}
            onRename={onRenameGated}
            onArchive={onArchiveGated}
            onDelete={onDeleteGated}
          />
        </div>
      )}
      {!isMobile && !leftCollapsed && activeCaseId !== null && (
        <>
          {/* Breadcrumb at the canonical top-left position. z-index 22 so
              it sits ABOVE the LayerPanel wrapper (z=20)  -  the panel is
              repositioned below the breadcrumb via top/left wrapper css. */}
          <div
            data-testid="grace2-left-rail"
            data-mode="case-view"
            /* job-0283  -  same desktop-only sleekness scope as cases-list mode. */
            className="grace2-desktop-rail"
            style={{
              position: "absolute",
              top: 12,
              left: 16,
              zIndex: 22,
              // Match CaseView's own 288px wrapStyle exactly. The prior 280px
              // here was 8px NARROWER than the CaseView it contained, so the
              // breadcrumb sized its title against 288 while the visible rail
              // was 280  -  the long-title right edge fell outside the wrapper
              // and hard-clipped mid-glyph (the recurring cutoff). Aligning the
              // widths lets CaseView's own ellipsis budget match the rail.
              width: 288,
            }}
          >
            <CaseView
              caseTitle={activeCase?.title ?? "Case"}
              onBack={handleCaseBack}
            />
            {/* Session-durability Job E (NATE) - three-way split of the
                layer-panel stub. SAME box (same marginTop/background/radius/
                padding/width/typography) so ONLY the outline style and the
                content change between states - NO layout shift:
                (1) LOADING (Case opening / layers inbound): SOLID outline +
                    spinner replacing the text.
                (2) SETTLED-EMPTY (Case open, settled, zero layers): UNCHANGED
                    dotted outline + the "No layers loaded yet" copy.
                (3) POPULATED (layers.length > 0): the LayerPanel below. */}
            {layers.length === 0 &&
              (layersLoading ? (
                <div
                  data-testid="grace2-case-view-loading-layers"
                  style={{
                    marginTop: 8,
                    background: "rgba(15,15,20,0.92)",
                    border: "1px solid #555",
                    borderRadius: 8,
                    padding: 12,
                    color: "#999",
                    fontSize: 12,
                    textAlign: "center",
                    lineHeight: 1.4,
                    width: 288,
                    boxSizing: "border-box",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: 8,
                  }}
                >
                  <span
                    aria-hidden="true"
                    style={{
                      width: 12,
                      height: 12,
                      borderRadius: "50%",
                      border: "2px solid rgba(255,255,255,0.2)",
                      borderTopColor: "#bbb",
                      animation: "grace2-spin 0.8s linear infinite",
                    }}
                  />
                  <span>Loading layers...</span>
                </div>
              ) : showRestoreLayersStub ? (
                <div
                  data-testid="grace2-case-view-restore-layers"
                  style={{
                    marginTop: 8,
                    background: "rgba(15,15,20,0.92)",
                    border: "1px dashed #6b5d2e",
                    borderRadius: 8,
                    padding: 12,
                    color: "#c9b873",
                    fontSize: 12,
                    textAlign: "center",
                    lineHeight: 1.4,
                    width: 288,
                    boxSizing: "border-box",
                  }}
                >
                  Wake the agent to restore this Case's layers.
                </div>
              ) : (
                <div
                  data-testid="grace2-case-view-empty-layers"
                  style={{
                    marginTop: 8,
                    background: "rgba(15,15,20,0.92)",
                    border: "1px dashed #444",
                    borderRadius: 8,
                    padding: 12,
                    color: "#999",
                    fontSize: 12,
                    textAlign: "center",
                    lineHeight: 1.4,
                    width: 288,
                    boxSizing: "border-box",
                  }}
                >
                  No layers loaded yet. Ask the assistant to add data.
                </div>
              ))}
          </div>
          {/* LayerPanel  -  its own absolute positioning at left:16, top:16.
              We mount it directly so MapLibre rendering picks up its
              effects; the visual placement below the breadcrumb is
              achieved by leaving room above (top:64 used by no wrapper  - 
              LayerPanel itself spans the column). The breadcrumb at
              z-index 22 sits above LayerPanel's chrome at z-index 20. */}
          {layers.length > 0 && (
            <div
              data-testid="grace2-case-view-layer-panel-wrap"
              style={{ position: "absolute", top: 64, left: 0, right: 0, bottom: 60, zIndex: 20, pointerEvents: "none" }}
            >
              {/* job-0173 Part 3  -  confine the pointer-events:auto region to
                  the LayerPanel column only. The prior implementation made the
                  inner div full-bleed (width:100%, height:100%) with
                  pointerEvents:"auto", which blocked map drag/pan everywhere
                  inside the (top:64 -> bottom:60, left:0 -> right:0) zone  - 
                  i.e. virtually the entire map. LayerPanel is absolutely
                  positioned at left:16 / top:16 / bottom:16 / width:280
                  relative to this wrapper, so a 280px-wide column from the
                  left edge is the exact click target. Outside that column,
                  map pan/drag passes through (parent pointerEvents:"none"). */}
              <div
                style={{
                  pointerEvents: "auto",
                  position: "absolute",
                  left: 0,
                  top: 0,
                  bottom: 0,
                  // ux-batch-1 J1 (F11): track the dragged panel width so the
                  // click target (incl. the right-edge resize handle) always
                  // covers the panel. left:16 offset + panel + 16 right pad.
                  width: layersWidth + 16 + 16,
                }}
              >
                <LayerPanel
                  subscribeSessionState={bus.subscribeSessionState}
                  subscribeMapCommand={bus.subscribeMapCommand}
                  initialLayers={layers}
                  onClose={collapseLeft}
                  width={layersWidth}
                  onWidthChange={setLayersWidth}
                  /* Projected AOI rect (lifted from MapView) so the
                     SequenceScrubber pins bottom-center of the AOI box. */
                  aoiRect={aoiScreenRect}
                  /* job-0258: user layer-control intents (opacity slider /
                     visibility checkbox / drag-reorder) flow through the bus
                     so MapView applies them to the live MapLibre instance.
                     Without this the panel controls were dead in the demo. */
                  onMapCommand={bus.pushMapCommand}
                  /* job-0322 F53  -  end-to-end delete. The LayerPanel delete
                     control (job-0325) optimistically removes the row, but the
                     layer resurrected on the next session-state because this
                     prop was never wired: the client never told the server.
                     sendDeleteLayer emits the `layer-delete` envelope; the
                     server persists the post-deletion list and echoes a fresh
                     session-state (sans the layer) which onSessionState ->
                     bus.pushSessionState reconciles into the Map via
                     replace-not-reconcile  -  so the layer stays gone. */
                  onDeleteLayer={handleDeleteLayer}
                />
              </div>
            </div>
          )}
        </>
      )}

      {/* job-0143: Bottom-row Settings pill. Hidden when the left rail is
          collapsed (it belongs to the rail). job-0278: on mobile it folds
          into the drawer footer instead  -  the floating pill would collide
          with the bottom-sheet composer.
          job-0321 F29  -  the standalone Secrets pill is retired (API keys now
          live inside Settings), so `onOpenSecrets` is no longer wired. */}
      {!isMobile && !leftCollapsed && (
        <BottomRowButtons
          onOpenSettings={() => setSettingsOpen(true)}
          /* LANE D (NATE) - the desktop "Show/Hide legend" toggle sits NEXT TO
             Settings (out of the way), replacing the floating bottom-center
             pill. Only renders when there is a legend to toggle. */
          legendHidden={legendHiddenDesktop}
          onToggleLegend={() => setLegendHiddenDesktop((h) => !h)}
          legendHasContent={legendHasContent(layers)}
        />
      )}

      {/* Right panel  -  Chat stays MOUNTED across collapse so its internal       */}
      {/* state (messages, pipeline history, lastError) is preserved. job-0162: */}
      {/* clicking the chevron-collapse button no longer destroys chat content. */}
      {/* Visually hidden via display:none + aria-hidden when collapsed.        */}
      <div
        data-testid="grace2-chat-mount"
        aria-hidden={rightCollapsed && !isMobile}
        style={{
          // job-0278  -  on mobile the chat is always present as the bottom
          // sheet (its own collapsed state IS the minimized form); the
          // desktop right-collapse toggle doesn't apply.
          display: rightCollapsed && !isMobile ? "none" : "contents",
        }}
      >
        {/* job-0266  -  activeCaseId selects Chat's visible per-Case stream:
            switching Cases swaps the entire stream; null (root) shows the
            clean empty composer. */}
        <Chat
          wsUrl={WS_URL}
          onClose={collapseRight}
          activeCaseId={activeCaseId}
          mobile={isMobile}
          authEpoch={authEpoch}
          width={chatWidth}
          onWidthChange={setChatWidth}
          /* sleep/wake STAGE 2  -  App owns the asleep classification (App socket
             + report-only probe) and threads it down so Chat's composer machine
             can branch Connecting -> (Chat | Wake). `agentAsleep` =
             composerWakeReady; `onWakeTap` is the ONLY POST-wake path (tap
             only). Chat gates ONLY the composer; its scrollback stays live. */
          agentAsleep={composerWakeReady}
          onWakeTap={handleWakeTap}
          /* job-0179  -  COLD chat-history render. App routes every case-open
             (live WS + cold serverless snapshot) onto the bus; Chat subscribes
             here to materialize the per-Case chat-history bubbles via
             routeCaseOpen. Chat does NOT see App's useCases state, so without
             this the cold view leaves the conversation blank. Idempotent. */
          subscribeCaseOpen={bus.subscribeCaseOpen}
          /* MOBILE SHEET-TOP DOCK (NATE 2026-06-24) - lift the sheet's
             expanded/height geometry so App can dock the SequenceScrubber +
             LayerLegend to the sheet's TOP edge (a clean band) instead of
             floating over the map. Mobile-only; Chat no-ops it on desktop. */
          onSheetGeometryChange={handleSheetGeometryChange}
          /* CHART-OVERLAY HIDE-LEGEND (NATE 2026-06-28, mobile) - lift the
             ChartGallery open state so the MAP's LayerLegend hides on mobile
             while a chart is open (the gallery is a full-viewport overlay; the
             body-portaled legend would otherwise paint above/around it). */
          onGalleryOpenChange={setChartGalleryOpen}
        />
      </div>

      {/* sleep/wake STAGE 2 (NATE 2026-06-18)  -  the OLD full-chat-panel
          WakeOverlay mount is REMOVED. It blanketed the entire chat panel
          (scrollback included) and was driven by the App socket. STAGE 2 keeps
          the chat scrollback + tool cards + insights AND the whole map LIVE with
          the box asleep, gating ONLY the text-entry composer. The Wake UI now
          lives INSIDE Chat's composer slot (Chat.tsx renders WakeOverlay scoped
          to that slot), driven by the asleep signal (`composerWakeReady`) +
          tap handler (`handleWakeTap`) threaded down via the Chat props below. */}

      {/* Layers hamburger  -  top-LEFT. (Desktop; mobile uses the drawer -.) */}
      {!isMobile && showLayersHamburger && (
        <button
          data-testid="grace2-layers-hamburger"
          aria-label="Show layers"
          aria-expanded={false}
          aria-controls="grace2-layer-panel"
          onClick={expandLeft}
          style={{ ...hamburgerBtnStyle, left: 16 }}
        >
          {/* job-0322 F52  -  icon-module glyph (no raw unicode -). */}
          <IconMenu size={18} />
        </button>
      )}

      {/* Chat hamburger  -  top-RIGHT. (Desktop only  -  the mobile sheet is
          always mounted with its own toggle handle.) */}
      {!isMobile && showChatHamburger && (
        <button
          data-testid="grace2-chat-hamburger"
          aria-label="Show chat"
          aria-expanded={false}
          onClick={expandRight}
          style={{ ...hamburgerBtnStyle, right: 12 }}
        >
          {/* job-0322 F52  -  icon-module glyph (no raw unicode -). */}
          <IconMenu size={18} />
        </button>
      )}

      {/* job-0278  -  mobile - opener (top-left, 44px touch target). Hidden
          while the drawer is open (the drawer overlays it anyway). */}
      {isMobile && !mobileDrawerOpen && (
        <MobileDrawerButton
          open={mobileDrawerOpen}
          onClick={() => setMobileDrawerOpen(true)}
        />
      )}

      {/* job-0321 F29  -  mobile-only top-right Settings entry. On mobile the
          only prior Settings reach was buried in the drawer footer; this puts
          a - button at the top-right so Settings (and, bundled inside it, the
          API-key entry) is reachable from anywhere. Desktop is unaffected
          (Settings stays on the bottom-row pill).
          z-index 36 sits above the upgrade toast (35) but below the
          payload-warning banner "hat" (60) and the Settings overlay itself
          (9500), and clears the toast which anchors at top:56. */}
      {isMobile && (
        <button
          data-testid="grace2-mobile-settings-button"
          aria-label="Open settings"
          onClick={() => setSettingsOpen(true)}
          style={{
            position: "absolute",
            top: 12,
            right: 12,
            width: 44,
            height: 44,
            padding: 0,
            background: "rgba(18,19,24,0.85)",
            border: "1px solid rgba(255,255,255,0.10)",
            borderRadius: 12,
            boxShadow: "0 2px 12px rgba(0,0,0,0.25)",
            color: "#cfd4db",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            lineHeight: 1,
            zIndex: 36,
            fontFamily:
              "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
          }}
        >
          {/* job-0322 F29  -  icon-module gear (no raw unicode -). */}
          <IconSettings size={20} />
        </button>
      )}

      {/* job-0278  -  mobile slide-in drawer. Hosts the SAME left-rail
          content as desktop (CasesPanel at root; CaseView + LayerPanel
          inside a Case) plus the Settings/Secrets pills in its footer.
          Tapping a Case row or the backdrop closes it. */}
      {isMobile && (
        <MobileDrawer
          open={mobileDrawerOpen}
          onClose={() => setMobileDrawerOpen(false)}
        >
          {activeCaseId === null ? (
            // job-0322 F52 (v2)  -  the layout wrapper is click-transparent so
            // empty/gutter taps fall through to the drawer backdrop (close);
            // the inner wrapper hugs the CasesPanel card and re-enables
            // hit-testing (`pointerEvents: "auto"`) so the card (and the
            // fixed ConfirmationDialog it mounts) still receive taps.
            <div
              style={{
                flex: 1,
                minHeight: 0,
                // CasesPanel scrolls its OWN list internally (pinned header +
                // mask fade); the hugger must NOT double-scroll. flex:1 +
                // minHeight:0 already give it a bounded height from the
                // MobileDrawer column (top:0/bottom:0); overflow:hidden (was
                // overflowY:auto) removes the competing scroll container so
                // CasesPanel height:100% fills this bound and the list  -  not
                // the whole panel including the header  -  is what scrolls.
                overflow: "hidden",
                pointerEvents: "none",
              }}
            >
              {/* job-0337  -  the hugger stays full-width so it never shrink-
                  wraps to a long Case title's intrinsic width (the job-0330
                  clip hazard). The CasesPanel inside is now a FIXED 288px
                  (max-width:100% guards sub-288 columns)  -  it neither grows
                  with content nor varies with viewport  -  so the row title's
                  flex:1 + min-width:0 ellipsis engages and the kebab
                  (flex-shrink:0) stays inside the column's overflow:hidden
                  clip. (The mobile fixed width is set in global.css
                  `.grace2-mobile-touch [data-testid="grace2-cases-panel"]`.)

                  MOBILE CASES-SCROLL FIX (NATE 2026-06-20): this inner wrapper
                  was `width:100%` ONLY  -  a content-sized (height:auto) block
                  BETWEEN the bounded hugger (flex:1 + minHeight:0) and
                  CasesPanel. CasesPanel's `height:100%` (the ed4cf91 desktop
                  fix) resolved against this auto height, so the panel sized to
                  content, never got squeezed, and its inner list never scrolled
                  on mobile. Making this wrapper a bounded flex column
                  (flex:1 + minHeight:0 + display:flex + flexDirection:column)
                  passes the hugger's real bounded height THROUGH to CasesPanel
                  (height:100% now resolves), so the grace2-cases-list  -  the
                  single scroll surface (pinned header + mask fade)  -  scrolls.
                  Mirrors the desktop convergence (rail wrapper is a bounded
                  flex column). width:100% + pointerEvents:auto are preserved. */}
              <div
                style={{
                  width: "100%",
                  flex: 1,
                  minHeight: 0,
                  display: "flex",
                  flexDirection: "column",
                  pointerEvents: "auto",
                }}
              >
                <CasesPanel
                  cases={cases}
                  activeCaseId={activeCaseId}
                  loading={casesListLoading}
                  onCreate={onCreateGated}
                  onSelect={(caseId) => {
                    onSelectCase(caseId);
                    setMobileDrawerOpen(false);
                  }}
                  onRename={onRenameGated}
                  onArchive={onArchiveGated}
                  onDelete={onDeleteGated}
                />
              </div>
            </div>
          ) : (
            <>
              {/* job-0284  -  mobile: the "Cases" breadcrumb link is the
                  SINGLE back affordance (no - arrow).
                  job-0322 F52 (v2)  -  wrap in a `pointerEvents: "auto"` hugger
                  so the breadcrumb card stays tappable even though the drawer
                  column above is click-transparent (gutter taps fall through to
                  the backdrop = close). */}
              {/* NATE 2026-06-19: fill the drawer width (was width:"fit-content",
                  which sized to CaseView's old fixed 288px wrap and overflowed
                  narrow phones -> breadcrumb cutoff). 100% + min-width:0 lets the
                  breadcrumb bound to the real drawer width and ellipsize. */}
              <div style={{ width: "100%", minWidth: 0, pointerEvents: "auto" }}>
                <CaseView
                  caseTitle={activeCase?.title ?? "Case"}
                  onBack={handleCaseBack}
                  mobile
                />
              </div>
              {/* Session-durability Job E (NATE) - three-way split (mobile).
                  SAME hairline card (same background/radius/padding/typography/
                  pointerEvents) so ONLY the outline (dotted->solid) and content
                  (text->spinner) change between LOADING and SETTLED-EMPTY - no
                  layout shift. POPULATED falls to the LayerPanel branch below. */}
              {layers.length === 0 ? (
                layersLoading ? (
                  <div
                    data-testid="grace2-case-view-loading-layers"
                    style={{
                      background: "rgba(18,19,24,0.72)",
                      border: "1px solid rgba(255,255,255,0.35)",
                      borderRadius: 10,
                      padding: 12,
                      color: "#a8b0bb",
                      fontSize: 12,
                      textAlign: "center",
                      lineHeight: 1.4,
                      boxSizing: "border-box",
                      pointerEvents: "auto",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      gap: 8,
                    }}
                  >
                    <span
                      aria-hidden="true"
                      style={{
                        width: 12,
                        height: 12,
                        borderRadius: "50%",
                        border: "2px solid rgba(255,255,255,0.2)",
                        borderTopColor: "#cfd6df",
                        animation: "grace2-spin 0.8s linear infinite",
                      }}
                    />
                    <span>Loading layers...</span>
                  </div>
                ) : showRestoreLayersStub ? (
                  <div
                    data-testid="grace2-case-view-restore-layers"
                    style={{
                      // COLD-VIEW GATE FIX (NATE 2026-06-28): same hairline card
                      // as the empty stub, only the outline/copy change so there
                      // is no layout shift; amber tint reads as "needs a wake".
                      background: "rgba(18,19,24,0.72)",
                      border: "1px dashed rgba(201,184,115,0.45)",
                      borderRadius: 10,
                      padding: 12,
                      color: "#c9b873",
                      fontSize: 12,
                      textAlign: "center",
                      lineHeight: 1.4,
                      boxSizing: "border-box",
                      pointerEvents: "auto",
                    }}
                  >
                    Wake the agent to restore this Case's layers.
                  </div>
                ) : (
                  <div
                    data-testid="grace2-case-view-empty-layers"
                    style={{
                      // job-0284 - floats as a translucent hairline card over
                      // the map (the drawer panel surface is gone).
                      background: "rgba(18,19,24,0.72)",
                      border: "1px dashed rgba(255,255,255,0.18)",
                      borderRadius: 10,
                      padding: 12,
                      color: "#a8b0bb",
                      fontSize: 12,
                      textAlign: "center",
                      lineHeight: 1.4,
                      boxSizing: "border-box",
                      // job-0322 F52 (v2) - this card is an actual component, so
                      // it re-enables hit-testing above the click-transparent
                      // drawer column. (It has no interactive controls today, but
                      // keeping it `auto` matches the spec and is forward-safe.)
                      pointerEvents: "auto",
                    }}
                  >
                    No layers loaded yet. Ask the assistant to add data.
                  </div>
                )
              ) : (
                <div
                  style={{
                    position: "relative",
                    flex: 1,
                    minHeight: 0,
                    // job-0322 F52 (v2)  -  the LayerPanel layout wrapper is
                    // click-transparent so gutter taps around the panel fall
                    // through to the backdrop (close). LayerPanel itself
                    // re-enables hit-testing via the `auto` wrapper below.
                    pointerEvents: "none",
                  }}
                >
                  {/* LayerPanel positions itself absolutely (left:16 /
                      top:16 / bottom:16 / width:288) relative to this
                      wrapper  -  it fills the drawer column.
                      job-0322 F52 (v2)  -  `pointerEvents: "auto"` wrapper
                      restores hit-testing for the absolutely-positioned panel
                      (pointer-events inherits down the DOM tree regardless of
                      layout position). */}
                  <div style={{ pointerEvents: "auto" }}>
                  <LayerPanel
                    subscribeSessionState={bus.subscribeSessionState}
                    subscribeMapCommand={bus.subscribeMapCommand}
                    initialLayers={layers}
                    onClose={() => setMobileDrawerOpen(false)}
                    onMapCommand={bus.pushMapCommand}
                    /* Projected AOI rect (lifted from MapView) so the mobile
                       SequenceScrubber pins bottom-center of the AOI box. */
                    aoiRect={aoiScreenRect}
                    /* job-0322 F53  -  end-to-end delete on the mobile drawer
                       mount too (swipe-right-to-delete in Group C drives this
                       same callback). See the desktop mount above for the
                       full data-flow rationale. */
                    onDeleteLayer={handleDeleteLayer}
                    /* Item b (NATE 2026-06-20)  -  the MOBILE legend show/hide
                       toggle lives IN the expanded Layers section (off the chat
                       composer). Only rendered when there's a legend to toggle. */
                    legendControl={
                      legendHasContent(layers) ? (
                        <MobileLegendToggle
                          hidden={legendHiddenMobile}
                          onToggle={setLegendHiddenMobile}
                        />
                      ) : null
                    }
                    mobile
                  />
                  </div>
                </div>
              )}
            </>
          )}
          {/* job-0322 F29  -  the drawer-footer Settings pill is REMOVED. The
              mobile-only top-right gear button (grace2-mobile-settings-button,
              above) is now the SOLE mobile Settings entry; API keys still live
              inside the SettingsPopup it opens. The desktop bottom-left
              BottomRowButtons pill is unchanged. */}
        </MobileDrawer>
      )}

      {/* Upgrade toast (job-0138 kickoff item 6). Renders below the chat
          hamburger so it doesn't collide with adjacent UI. */}
      {upgradeToast && (
        <div
          data-testid="grace2-upgrade-toast"
          role="status"
          style={{
            position: "absolute",
            top: 56,
            // job-0278  -  mobile: anchored near the right edge (the desktop
            // offsets assume the 380px side panel / hamburger, which don't
            // exist on phones and would push the toast off a 390px screen).
            right: isMobile ? 12 : rightCollapsed ? 60 : 380,
            background: "rgba(20,40,60,0.95)",
            border: "1px solid #3b82f6",
            borderRadius: 6,
            color: "#dde6f5",
            padding: "8px 12px",
            fontSize: 12,
            zIndex: 35,
            maxWidth: 280,
          }}
        >
          {upgradeToast}
        </div>
      )}

      {/* hidden marker so tests can assert App subscribes to auth changes */}
      <span
        data-testid="grace2-app-auth-state"
        data-auth-uid={authUser?.uid ?? ""}
        data-auth-anonymous={authUser?.isAnonymous ? "true" : "false"}
        style={{ display: "none" }}
      />
      {/* hidden marker so tests can assert App tracks active Case state */}
      <span
        data-testid="grace2-app-case-state"
        data-active-case-id={activeCaseId ?? ""}
        data-cases-count={String(cases.length)}
        style={{ display: "none" }}
      />

      {/* job-0145: Inline chat cards (payload-warnings + source suggestions)
          stack as a single column anchored over the chat panel  -  they
          visually sit IN the chat scroll while being mounted at App level
          (Chat owns its own GraceWs). Width matches chat message width
          (chat panel is 380px; cards use 340px with padding). Both surfaces
          use the InlineChatCard primitive for consistent visual language.
          When the chat panel is collapsed, cards still surface so a
          large-payload gate or new source suggestion isn't silently dropped. */}
      <div
        data-testid="inline-chat-card-stack"
        style={{
          position: "absolute",
          // job-0278  -  mobile: full-width column with 12px gutters (the
          // desktop 340px column anchored to the chat panel would clip on
          // a 390px screen).
          right: isMobile ? 12 : rightCollapsed ? 16 : 32,
          left: isMobile ? 12 : undefined,
          top: isMobile ? 64 : rightCollapsed ? 64 : 70,
          width: isMobile ? undefined : 340,
          display: "flex",
          flexDirection: "column",
          gap: 8,
          zIndex: 50,
          maxHeight: "calc(100vh - 96px)",
          overflowY: "auto",
          // Wrapper is click-through to the map when there's nothing inside;
          // inner column re-enables pointer events so cards are interactive.
          pointerEvents: "none",
        }}
      >
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 8,
            pointerEvents: "auto",
          }}
        >
          {/* FIX 2 (NATE 2026-06-17): payload-warning gates are no longer here
              NOR in any App-level banner  -  they render as in-chat cards in
              Chat's per-Case stream (Chat.tsx). Source suggestions stay here. */}
          {/* Source-suggestion inline card (job-0145, replaces Mode2OfferModal).
              Listens for candidate envelopes from the server; UI text never
              references the server-internal envelope name. Returns null when
              no candidate is active. */}
          <SourceSuggestionInline
            subscribeCandidate={subscribeSourceSuggestion}
            onAction={handleSourceSuggestionAction}
          />
        </div>
      </div>

      {/* FIX 2 (NATE 2026-06-17)  -  the large-payload warning BANNER "hat" is
          GONE. The warning is now an IN-CHAT card interleaved in the per-Case
          chat scroll (Chat.tsx kind:"payload-warning", PayloadWarningInline),
          matching the credential / tool / sandbox card family. tool-payload-
          warning is session-scoped (ws.ts SESSION_SCOPED_TYPES) so Chat's own
          GraceWs receives it via the fan-out hub; App no longer renders or
          tracks it. */}

      {/* job-0143: Settings popup (full-screen overlay). */}
      {settingsOpen && (
        <SettingsPopup
          userEmail={authUser?.email ?? null}
          isSignedIn={isSignedIn}
          theme={theme}
          onToggleTheme={toggleTheme}
          /* NATE item 1 - the bbox loading-animation enable toggle (DEFAULT ON).
             SettingsPopup persists the flag itself (localStorage); the callback
             bumps a tick so App re-reads `bboxAnimEnabled`. The CONNECTING scan
             border ignores this (it is a transport-health cue). */
          onBboxAnimationsChange={() => setBboxAnimSettingsTick((t) => t + 1)}
          /* "3D terrain viz" first cut - SettingsPopup persists the 3D-terrain +
             contour flags itself (localStorage); this callback bumps a tick so
             App re-reads them and re-threads terrain3dEnabled/contoursEnabled
             into MapView (which applies/removes MapLibre terrain). */
          onTerrain3dChange={() => setTerrain3dSettingsTick((t) => t + 1)}
          onSignOut={() => {
            void handleSignOut();
            setSettingsOpen(false);
          }}
          onSignInRequest={() => {
            handleSignInRequest();
            setSettingsOpen(false);
          }}
          onClose={() => setSettingsOpen(false)}
          onOpenToolsCatalog={() => {
            setSettingsOpen(false);
            setToolsCatalogOpen(true);
          }}
          onOpenRoutingDashboard={() => {
            setSettingsOpen(false);
            setRoutingDashOpen(true);
          }}
          /* job-0321 F29  -  bundle the per-Case API-key entry INSIDE Settings.
             These are the SAME wires that previously fed the standalone
             SecretsPopup; SettingsPopup renders SecretsPanel inline under its
             "API Keys" section. */
          secrets={secrets}
          caseId={currentCaseId}
          onSecretAdd={handleSecretAdd}
          onSecretRevoke={handleSecretRevoke}
          /* SHARED-BOX SLEEP (NATE 2026-06-29): "Put agent to sleep" is now a
             PER-SESSION pause - close THIS session's WS + clear our layers +
             surface the asleep composer, never a box-wide stop (the shared box
             keeps serving others and auto-stops server-side once ALL sessions
             are idle). The Settings popup stays open so the user reads the
             honest "workspace paused" line; the composer shows the Wake card. */
          onSleepSession={handleSleepSession}
        />
      )}

      {/* Wave 4.10 C1: Tools catalog popup (full-screen overlay). */}
      {toolsCatalogOpen && (
        <ToolsCatalogPopup onClose={() => setToolsCatalogOpen(false)} />
      )}

      {/* Wave 4.11 M7: Routing-quality dashboard (full-screen overlay). */}
      {routingDashOpen && (
        <RoutingQualityDashboard
          onClose={() => {
            setRoutingDashOpen(false);
            setRoutingDashInjected(null);
          }}
          initialSummary={routingDashInjected}
        />
      )}

      {/* Wave 4.11 P4: ImpactEnvelope side panel. Surfaces whenever a
          compute_impact_envelope tool result has populated impactEnvelope. */}
      {impactEnvelope && (
        <ImpactPanel
          envelope={impactEnvelope}
          caseName={activeCase?.title ?? null}
          onClose={() => setImpactEnvelope(null)}
        />
      )}

      {/* job-0321 F29  -  the standalone Secrets popup is retired. API-key
          management now lives inside the Settings popup above (SettingsPopup's
          embedded SecretsPanel), wired with the same secrets/case/add/revoke
          props. */}

      {/* job-0143: SaveGateModal  -  appears only when an anonymous user
          attempts a save-triggering action. */}
      {saveGate.isOpen && (
        <SaveGateModal
          pendingKind={saveGate.pendingKind}
          onSignIn={saveGate.requestSignIn}
          onContinueAnyway={saveGate.confirmContinue}
          onDismiss={saveGate.dismiss}
        />
      )}

      {/* JOB WEB-ANIM (#157.2-.3)  -  the floating sequence SCRUBBER. Rendered at
          the App root (always mounted) so it shows WHENEVER a sequential group is
          active on the shared AnimationController, regardless of whether the
          Layers panel is open/collapsed. It pins bottom-center of the AOI box via
          aoiScreenRect. Carries its own play/pause button (item 3) wired to the
          controller, so closing the panel never drops the scrubber or playback. */}
      <AppSequenceScrubber
        /* ITEM 2 (NATE 2026-06-23) - the scrubber is a MAP overlay; while the
           full-screen mobile Layers drawer is open it would float OVER the layer
           rows (reported: a scrubber pill mid-list). Hide it whenever the mobile
           drawer is open - it belongs to the map view, not over the list. Desktop
           is unaffected (the drawer is mobile-only).
           NATE 2026-06-24: also hide while the Settings panel is open so the
           bottom overlays do not overlap Settings. */
        hidden={(isMobile && mobileDrawerOpen) || settingsOpen}
        /* STATIC SCRUBBER (NATE 2026-06-26): the scrubber is now STATIC at the
           bottom of the screen (no AOI-bbox snap / dock). The only position input
           is the desktop side-panel geometry, so it centers in the open gutter
           and never hides under the side panels - a stable shift only on a panel
           toggle, never per animation frame. */
        leftPanelWidthPx={!isMobile && !leftCollapsed ? 288 : 0}
        chatWidthPx={chatWidth}
        chatCollapsed={rightCollapsed}
        /* TASK E (NATE 2026-06-26): thread the App's chat-sheet top-edge Y (the
           same value the mobile legend docks to) so the MOBILE scrubber docks
           to + tracks the chat panel top. Null on desktop -> bottom-pinned. */
        sheetTopPx={sheetTopPx}
        /* ZOOM-OUT HIDE (NATE 2026-06-27, mobile-only): hide the scrubber when the
           AOI bbox is a tiny dot on screen (zoomed OUT far). Gated to mobile here
           so desktop is byte-for-byte unchanged (the scrubber also mobile-gates
           the hide internally; this keeps the prop false on desktop). */
        aoiTooSmallToShow={isMobile && aoiTooSmallToShow}
      />
    </div>
    </AuthGuard>
  );
}

// --- App-level sequence scrubber (JOB WEB-ANIM #157.2-.3) --------------- //
//
// The scrubber used to render from inside LayerPanel, so closing the panel
// dropped it (and, since the play interval also lived there, killed playback).
// It now lives here, driven entirely by the module-level AnimationController, so
// it appears whenever ANY sequence is active  -  panel open or not. Stepping +
// play/pause go straight to the controller (which drives the map + the interval);
// the LayerPanel, when open, mirrors the controller's frame into its own rows.
function AppSequenceScrubber({
  hidden = false,
  leftPanelWidthPx = 0,
  chatWidthPx = 0,
  chatCollapsed = false,
  sheetTopPx = null,
  aoiTooSmallToShow = false,
}: {
  /**
   * ITEM 2 - suppress the scrubber entirely (mobile Layers drawer open, or the
   * Settings panel open). The scrubber is a map overlay; when the full-screen
   * drawer / Settings covers the map it must not float over the rows / settings.
   * Hooks still run above this guard.
   */
  hidden?: boolean;
  /** Desktop left rail width (288 when open, else 0) - gutter centering. */
  leftPanelWidthPx?: number;
  /** Right chat panel width (px) - gutter centering. */
  chatWidthPx?: number;
  /** Whether the chat panel is collapsed (its width counts as 0). */
  chatCollapsed?: boolean;
  /**
   * TASK E (NATE 2026-06-26): the on-screen Y of the chat sheet's TOP edge
   * (mobile only; null on desktop). Threaded to SequenceScrubber so the MOBILE
   * scrubber docks its bottom to + tracks the chat panel top instead of floating
   * over the map. Desktop ignores it (stays bottom-pinned).
   */
  sheetTopPx?: number | null;
  /**
   * ZOOM-OUT HIDE (NATE 2026-06-27, mobile-only) - when true the scrubber HIDES
   * (the AOI bbox is a tiny dot on screen). App already mobile-gates this; the
   * scrubber also mobile-gates the hide internally. Default false (no hide).
   */
  aoiTooSmallToShow?: boolean;
}): JSX.Element | null {
  const controller = useMemo(() => getAnimationController(), []);
  const anim = useAnimationState(controller);
  const activeGroup =
    anim.activeGroupKey != null
      ? anim.groups.find((g) => g.key === anim.activeGroupKey) ?? null
      : null;
  if (!activeGroup) return null;
  // ITEM 2 - the mobile Layers drawer is open over the map; do not paint the
  // scrubber over the layer list. (Placed AFTER the hooks above so hook order
  // is stable across renders.)
  if (hidden) return null;
  // activeIndex is read LIVE from the controller every render; useAnimationState
  // re-renders on every controller notify (incl. the auto-advance tick), so the
  // scrubber slider handle tracks autoplay (NATE 2026-06-26 autoplay-handle fix).
  const activeIndex = controller.frameIndexFor(activeGroup.key);
  return (
    <SequenceScrubber
      label={activeGroup.label}
      frameLabels={activeGroup.frameLabels}
      activeIndex={activeIndex}
      onStep={(idx) => controller.stepGroupTo(activeGroup.key, idx)}
      playing={anim.playing}
      onPlayToggle={() => {
        controller.setActiveGroup(activeGroup.key);
        controller.togglePlaying();
      }}
      leftPanelWidthPx={leftPanelWidthPx}
      chatWidthPx={chatWidthPx}
      chatCollapsed={chatCollapsed}
      sheetTopPx={sheetTopPx}
      aoiTooSmallToShow={aoiTooSmallToShow}
    />
  );
}
