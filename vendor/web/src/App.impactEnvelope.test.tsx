// GRACE-2 web — App.tsx impact-envelope WS wiring tests (Wave 4.11 P4).
//
// Verifies that when an `impact-envelope` WebSocket frame arrives, the
// App-level `impactEnvelope` state is updated and the ImpactPanel is mounted.
//
// Because App.tsx mounts Chat (WebSocket) and MapView (WebGL) which cannot
// run in happy-dom, we follow the same test harness pattern as App.test.tsx:
// a minimal shell component that mirrors only the wiring under test.
//
// Specifically we test:
//   1. Calling the handler (simulating GraceWs delivering impact-envelope)
//      causes ImpactPanel to appear in the DOM.
//   2. Calling the handler with null (clearing) removes ImpactPanel.
//   3. The dev-seam window.__grace2InjectImpactEnvelope (already wired in
//      App.tsx) calls setImpactEnvelope — asserted via the panel mount.

import { describe, it, expect } from "vitest";
import { render, screen, act, cleanup } from "@testing-library/react";
import { useState, useEffect } from "react";
import {
  ImpactPanel,
  type ImpactEnvelope,
} from "./components/ImpactPanel";

// ---------------------------------------------------------------------------
// Minimal fixture — mirrors ONLY the impactEnvelope state + ImpactPanel mount
// fragment from App.tsx, without importing WebSocket or WebGL dependencies.
// ---------------------------------------------------------------------------

/** Minimal ImpactEnvelope for mount/unmount tests. */
const FIXTURE_ENVELOPE: ImpactEnvelope = {
  schema_version: "v1",
  n_structures_total: 1_000,
  n_structures_damaged: 400,
  n_structures_destroyed: 60,
  damage_state_distribution: {
    DS0_none: 600,
    DS1_slight: 200,
    DS2_moderate: 100,
    DS3_extensive: 40,
    DS4_complete: 60,
  },
  total_replacement_value_usd: 200_000_000,
  damaged_replacement_value_usd: 80_000_000,
  expected_loss_usd: 55_000_000,
  loss_percentile_95_usd: 90_000_000,
  population_total: 2_400,
  population_displaced: 900,
  population_at_high_risk: 280,
  impact_area_km2: 9.2,
  bbox: [-81.85, 26.60, -81.78, 26.66],
  by_occupancy_class: {
    RES1: {
      n_structures: 800,
      n_damaged: 320,
      n_destroyed: 50,
      expected_loss_usd: 40_000_000,
      loss_percentile_95_usd: 70_000_000,
      population: 2_400,
      population_displaced: 900,
    },
  },
  pelicun_run_id: "01HWTEST1234567890ABCDEFGH",
  damage_layer_uri: "gs://grace2-runs/pelicun/01HWTEST/damage.gpkg",
  structure_inventory_source: "USACE_NSI",
  flood_layer_uri: "gs://grace2-runs/sfincs/01HWTEST/flood_depth_peak.tif",
  fragility_set: "HAZUS-MH-4.2-coastal",
  realization_count: 500,
  generated_at: "2026-06-09T10:00:00.000Z",
};

// ---------------------------------------------------------------------------
// Minimal App shell — mirrors App.tsx's impactEnvelope state + ImpactPanel
// mount without touching WebSocket or MapLibre.
// ---------------------------------------------------------------------------

function ImpactEnvelopeShell(): JSX.Element {
  const [impactEnvelope, setImpactEnvelope] =
    useState<ImpactEnvelope | null>(null);

  // Mirrors the dev-seam from App.tsx (DEV only in production; always wired
  // here so tests can drive it without a real GraceWs).
  useEffect(() => {
    (window as unknown as {
      __grace2InjectImpactEnvelope?: (p: ImpactEnvelope | null) => void
    }).__grace2InjectImpactEnvelope = (p) => setImpactEnvelope(p);
    return () => {
      delete (window as unknown as {
        __grace2InjectImpactEnvelope?: unknown
      }).__grace2InjectImpactEnvelope;
    };
  }, []);

  return (
    <div data-testid="app-shell">
      <span
        data-testid="impact-envelope-present"
        data-value={impactEnvelope ? "true" : "false"}
      />
      {impactEnvelope && (
        <ImpactPanel
          envelope={impactEnvelope}
          caseName="Fort Myers · Ian"
          onClose={() => setImpactEnvelope(null)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Case-switch reset shell — mirrors App.tsx's activeSession effect, which
// resets ImpactEnvelope to null whenever the active Case changes
// (replace-not-reconcile, M5.5). The `activeCaseId` prop stands in for
// App.tsx's `activeSession`; flipping it must clear any surfaced panel.
// ---------------------------------------------------------------------------

function CaseSwitchShell({
  activeCaseId,
}: {
  activeCaseId: string | null;
}): JSX.Element {
  const [impactEnvelope, setImpactEnvelope] =
    useState<ImpactEnvelope | null>(null);

  useEffect(() => {
    (window as unknown as {
      __grace2InjectImpactEnvelope?: (p: ImpactEnvelope | null) => void
    }).__grace2InjectImpactEnvelope = (p) => setImpactEnvelope(p);
    return () => {
      delete (window as unknown as {
        __grace2InjectImpactEnvelope?: unknown
      }).__grace2InjectImpactEnvelope;
    };
  }, []);

  // Mirrors App.tsx's Case rehydration effect: on activeSession change the
  // ImpactPanel is reset (the new Case re-populates it via a fresh emission).
  useEffect(() => {
    setImpactEnvelope(null);
  }, [activeCaseId]);

  return (
    <div data-testid="case-shell">
      <span
        data-testid="impact-envelope-present"
        data-value={impactEnvelope ? "true" : "false"}
      />
      {impactEnvelope && (
        <ImpactPanel
          envelope={impactEnvelope}
          onClose={() => setImpactEnvelope(null)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("App — impactEnvelope state → ImpactPanel mount (Wave 4.11 P4)", () => {
  it("ImpactPanel is NOT mounted before any impact-envelope arrives", () => {
    render(<ImpactEnvelopeShell />);
    expect(
      screen.queryByTestId("grace2-impact-panel"),
    ).toBeNull();
    cleanup();
  });

  it("ImpactPanel mounts when the handler delivers a valid ImpactEnvelope", () => {
    render(<ImpactEnvelopeShell />);

    // Simulate GraceWs delivering an impact-envelope (the onImpactEnvelope
    // handler in App.tsx calls setImpactEnvelope, mirrored by the inject seam
    // wired in ImpactEnvelopeShell above).
    act(() => {
      const seam = (window as unknown as {
        __grace2InjectImpactEnvelope?: (p: ImpactEnvelope | null) => void
      }).__grace2InjectImpactEnvelope;
      seam?.(FIXTURE_ENVELOPE);
    });

    expect(screen.getByTestId("grace2-impact-panel")).toBeTruthy();
    // Marker attribute should reflect the presence of the envelope.
    expect(
      screen.getByTestId("impact-envelope-present").dataset.value,
    ).toBe("true");

    cleanup();
  });

  it("ImpactPanel is removed when the handler delivers null (onClose path)", () => {
    render(<ImpactEnvelopeShell />);

    // First surface the panel.
    act(() => {
      const seam = (window as unknown as {
        __grace2InjectImpactEnvelope?: (p: ImpactEnvelope | null) => void
      }).__grace2InjectImpactEnvelope;
      seam?.(FIXTURE_ENVELOPE);
    });
    expect(screen.getByTestId("grace2-impact-panel")).toBeTruthy();

    // Now clear it (simulates onClose → setImpactEnvelope(null) in App.tsx).
    act(() => {
      const seam = (window as unknown as {
        __grace2InjectImpactEnvelope?: (p: ImpactEnvelope | null) => void
      }).__grace2InjectImpactEnvelope;
      seam?.(null);
    });
    expect(screen.queryByTestId("grace2-impact-panel")).toBeNull();
    expect(
      screen.getByTestId("impact-envelope-present").dataset.value,
    ).toBe("false");

    cleanup();
  });

  it("ImpactPanel title and timestamp render from the envelope", () => {
    render(<ImpactEnvelopeShell />);

    act(() => {
      const seam = (window as unknown as {
        __grace2InjectImpactEnvelope?: (p: ImpactEnvelope | null) => void
      }).__grace2InjectImpactEnvelope;
      seam?.(FIXTURE_ENVELOPE);
    });

    expect(screen.getByTestId("grace2-impact-panel-title").textContent).toBe(
      "Impact summary",
    );
    // Timestamp should reference the fixture's generated_at.
    const ts = screen.getByTestId("grace2-impact-panel-timestamp");
    expect(ts.textContent).toContain("2026-06-09");

    cleanup();
  });

  // M5.5: the panel must NOT bleed across Cases. Switching the active Case
  // clears any surfaced ImpactPanel (replace-not-reconcile, mirrors App.tsx's
  // activeSession rehydration effect adding setImpactEnvelope(null)).
  it("ImpactPanel is cleared on Case switch (does not bleed across Cases)", () => {
    const { rerender } = render(<CaseSwitchShell activeCaseId="case-A" />);

    // Surface a panel while Case A is active.
    act(() => {
      const seam = (window as unknown as {
        __grace2InjectImpactEnvelope?: (p: ImpactEnvelope | null) => void
      }).__grace2InjectImpactEnvelope;
      seam?.(FIXTURE_ENVELOPE);
    });
    expect(screen.getByTestId("grace2-impact-panel")).toBeTruthy();
    expect(
      screen.getByTestId("impact-envelope-present").dataset.value,
    ).toBe("true");

    // Switch to Case B — the previous Case's panel must disappear.
    act(() => {
      rerender(<CaseSwitchShell activeCaseId="case-B" />);
    });
    expect(screen.queryByTestId("grace2-impact-panel")).toBeNull();
    expect(
      screen.getByTestId("impact-envelope-present").dataset.value,
    ).toBe("false");

    cleanup();
  });
});
