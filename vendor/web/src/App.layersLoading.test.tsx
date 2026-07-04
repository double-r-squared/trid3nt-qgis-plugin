// GRACE-2 web - layer-panel LOADING-state three-way split (session-durability
// Job E, NATE spec).
//
// THE FEATURE: the two empty-layer stubs (desktop + mobile) were a hard binary
// on `layers.length === 0` -> always showed "No layers loaded yet. Ask the
// assistant to add data." while a Case was still OPENING (its session/layers
// inbound). NATE's three-way split:
//   (1) LOADING (Case opening / layers inbound, not yet settled): a SOLID
//       outline + a spinner replacing the text (testid
//       grace2-case-view-loading-layers, label "Loading layers...").
//   (2) SETTLED-EMPTY (Case open, settled, zero layers): the UNCHANGED dotted
//       outline + "No layers loaded yet..." (testid
//       grace2-case-view-empty-layers).
//   (3) POPULATED (layers.length > 0): the LayerPanel.
//
// The full App mounts Chat (WebSocket) + MapView (WebGL / maplibre-gl) which
// happy-dom cannot run, so - per the established App.test.tsx /
// App.coldViewRender.test.tsx convention - this wires a MINIMAL shell that
// reproduces VERBATIM the two pieces under test:
//   - the `layersLoading` derivation added near App.tsx's activeCase block, and
//   - the two three-way stub render blocks (desktop + mobile).
// Driving activeCaseId / activeSession / wsStatus / layers as props exercises
// the EXACT branch logic. The derivation depends only on those plain values, so
// a faithful copy here is a true unit of the shipped logic.

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { useMemo } from "react";

type WsStatus = "connecting" | "connected" | "disconnected" | "reconnecting";

interface ShellSession {
  case: { case_id: string };
}

interface ShellProps {
  activeCaseId: string | null;
  activeSession: ShellSession | null;
  wsStatus: WsStatus;
  layers: ReadonlyArray<unknown>;
  variant: "desktop" | "mobile";
}

const EMPTY_TEXT = "No layers loaded yet. Ask the assistant to add data.";
const LOADING_TEXT = "Loading layers...";

// ── Minimal shell: the layersLoading derivation + the three-way stub render,
// reproduced VERBATIM from App.tsx (desktop ~:1515 / mobile ~:1839). ──────── //
function StubShell({
  activeCaseId,
  activeSession,
  wsStatus,
  layers,
  variant,
}: ShellProps): JSX.Element {
  // VERBATIM from App.tsx (session-durability Job E).
  const caseSelectedButUnsettled =
    activeCaseId !== null &&
    (activeSession === null || activeSession.case.case_id !== activeCaseId);
  const layersLoading = useMemo(
    () =>
      activeCaseId !== null &&
      (caseSelectedButUnsettled ||
        wsStatus === "connecting" ||
        wsStatus === "reconnecting"),
    [activeCaseId, caseSelectedButUnsettled, wsStatus],
  );

  if (variant === "desktop") {
    return (
      <div>
        {layers.length === 0 &&
          (layersLoading ? (
            <div data-testid="grace2-case-view-loading-layers">
              <span aria-hidden="true" />
              <span>{LOADING_TEXT}</span>
            </div>
          ) : (
            <div data-testid="grace2-case-view-empty-layers">{EMPTY_TEXT}</div>
          ))}
        {layers.length > 0 && (
          <div data-testid="grace2-case-view-layer-panel-wrap">LayerPanel</div>
        )}
      </div>
    );
  }

  // Mobile: empty-or-loading is the truthy arm; populated is the else arm.
  return (
    <div>
      {layers.length === 0 ? (
        layersLoading ? (
          <div data-testid="grace2-case-view-loading-layers">
            <span aria-hidden="true" />
            <span>{LOADING_TEXT}</span>
          </div>
        ) : (
          <div data-testid="grace2-case-view-empty-layers">{EMPTY_TEXT}</div>
        )
      ) : (
        <div data-testid="grace2-mobile-layer-panel">LayerPanel</div>
      )}
    </div>
  );
}

const CASE_ID = "01CASE";
const settledSession: ShellSession = { case: { case_id: CASE_ID } };
const otherSession: ShellSession = { case: { case_id: "01OTHER" } };

afterEach(cleanup);

describe.each(["desktop", "mobile"] as const)(
  "layer-panel three-way loading split (%s)",
  (variant) => {
    it("LOADING: case selected but no session yet -> spinner stub, NOT empty stub", () => {
      render(
        <StubShell
          variant={variant}
          activeCaseId={CASE_ID}
          activeSession={null}
          wsStatus="connecting"
          layers={[]}
        />,
      );
      expect(
        screen.queryByTestId("grace2-case-view-loading-layers"),
      ).not.toBeNull();
      expect(screen.getByText(LOADING_TEXT)).toBeTruthy();
      expect(
        screen.queryByTestId("grace2-case-view-empty-layers"),
      ).toBeNull();
    });

    it("LOADING: session carries the PREVIOUS case (unsettled) -> spinner stub", () => {
      render(
        <StubShell
          variant={variant}
          activeCaseId={CASE_ID}
          activeSession={otherSession}
          wsStatus="connected"
          layers={[]}
        />,
      );
      expect(
        screen.queryByTestId("grace2-case-view-loading-layers"),
      ).not.toBeNull();
      expect(
        screen.queryByTestId("grace2-case-view-empty-layers"),
      ).toBeNull();
    });

    it("LOADING: settled session but socket reconnecting -> spinner stub", () => {
      render(
        <StubShell
          variant={variant}
          activeCaseId={CASE_ID}
          activeSession={settledSession}
          wsStatus="reconnecting"
          layers={[]}
        />,
      );
      expect(
        screen.queryByTestId("grace2-case-view-loading-layers"),
      ).not.toBeNull();
      expect(
        screen.queryByTestId("grace2-case-view-empty-layers"),
      ).toBeNull();
    });

    it("SETTLED-EMPTY: case open + settled + socket healthy + zero layers -> empty stub (never spins forever)", () => {
      render(
        <StubShell
          variant={variant}
          activeCaseId={CASE_ID}
          activeSession={settledSession}
          wsStatus="connected"
          layers={[]}
        />,
      );
      expect(
        screen.queryByTestId("grace2-case-view-empty-layers"),
      ).not.toBeNull();
      expect(screen.getByText(EMPTY_TEXT)).toBeTruthy();
      expect(
        screen.queryByTestId("grace2-case-view-loading-layers"),
      ).toBeNull();
    });

    it("SETTLED-EMPTY: disconnected (not connecting/reconnecting) settled case -> empty stub", () => {
      // A genuinely-empty case whose socket later dropped must NOT spin: only
      // connecting/reconnecting count as inbound, not 'disconnected'.
      render(
        <StubShell
          variant={variant}
          activeCaseId={CASE_ID}
          activeSession={settledSession}
          wsStatus="disconnected"
          layers={[]}
        />,
      );
      expect(
        screen.queryByTestId("grace2-case-view-empty-layers"),
      ).not.toBeNull();
      expect(
        screen.queryByTestId("grace2-case-view-loading-layers"),
      ).toBeNull();
    });

    it("POPULATED: layers present -> LayerPanel, neither stub", () => {
      render(
        <StubShell
          variant={variant}
          activeCaseId={CASE_ID}
          activeSession={settledSession}
          wsStatus="connected"
          layers={[{ layer_id: "l1" }]}
        />,
      );
      expect(
        screen.queryByTestId("grace2-case-view-empty-layers"),
      ).toBeNull();
      expect(
        screen.queryByTestId("grace2-case-view-loading-layers"),
      ).toBeNull();
      // Populated branch renders the panel (testid differs per variant).
      const panel =
        variant === "desktop"
          ? screen.queryByTestId("grace2-case-view-layer-panel-wrap")
          : screen.queryByTestId("grace2-mobile-layer-panel");
      expect(panel).not.toBeNull();
    });

    it("ROOT (no active case): layersLoading is false even while connecting -> empty stub, no spinner", () => {
      // At the Cases root there is no active case, so the connecting socket must
      // not spin a phantom loader.
      render(
        <StubShell
          variant={variant}
          activeCaseId={null}
          activeSession={null}
          wsStatus="connecting"
          layers={[]}
        />,
      );
      expect(
        screen.queryByTestId("grace2-case-view-loading-layers"),
      ).toBeNull();
      expect(
        screen.queryByTestId("grace2-case-view-empty-layers"),
      ).not.toBeNull();
    });
  },
);
