// GRACE-2 web — ImpactPanel unit tests (Wave 4.11 P4).
//
// Verifies the ImpactPanel renders the ImpactEnvelope (SRS Appendix B.6c)
// produced by the ``compute_impact_envelope`` workflow (Wave 4.11 P3):
//
//   1. Renders all 4 headline stat cards on a complete envelope.
//   2. Hides population stats when ``population_total === null``
//      (the MS_BUILDINGS inventory path — no per-structure population).
//   3. Renders damage state distribution bars for DS0..DS4.
//   4. Renders per-occupancy-class table rows.
//   5. Provenance footer surfaces truncated run_id + inventory source badge.
//   6. Slide-in animation respects ``prefers-reduced-motion``.
//   7. Close button dismisses the panel via the onClose callback.
//   8. Fixture validates against the schema fields the panel reads.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { ImpactPanel, type ImpactEnvelope } from "./ImpactPanel";

// ---------------------------------------------------------------------------
// Fixture: Fort Myers ImpactEnvelope (USACE_NSI path).
// Mirrors the ImpactEnvelope pydantic schema in
// packages/contracts/src/grace2_contracts/impact_envelope.py — every field
// the component reads is present and validated below in test (8).
// ---------------------------------------------------------------------------

function fortMyersFixture(): ImpactEnvelope {
  return {
    schema_version: "v1",
    n_structures_total: 12_843,
    n_structures_damaged: 3_217,
    n_structures_destroyed: 412,
    damage_state_distribution: {
      DS0_none: 9_626,
      DS1_slight: 1_802,
      DS2_moderate: 712,
      DS3_extensive: 291,
      DS4_complete: 412,
    },
    total_replacement_value_usd: 4_120_000_000,
    damaged_replacement_value_usd: 1_080_000_000,
    expected_loss_usd: 312_500_000,
    loss_percentile_95_usd: 487_200_000,
    population_total: 28_410,
    population_displaced: 6_840,
    population_at_high_risk: 1_220,
    impact_area_km2: 84.6,
    bbox: [-82.05, 26.45, -81.78, 26.72],
    by_occupancy_class: {
      RES1: {
        n_structures: 10_840,
        n_damaged: 2_910,
        n_destroyed: 312,
        expected_loss_usd: 228_000_000,
        loss_percentile_95_usd: 360_000_000,
        population: 24_900,
        population_displaced: 5_910,
      },
      RES3: {
        n_structures: 1_215,
        n_damaged: 184,
        n_destroyed: 56,
        expected_loss_usd: 48_700_000,
        loss_percentile_95_usd: 79_000_000,
        population: 2_840,
        population_displaced: 645,
      },
      COM1: {
        n_structures: 612,
        n_damaged: 99,
        n_destroyed: 32,
        expected_loss_usd: 28_400_000,
        loss_percentile_95_usd: 41_200_000,
        population: 410,
        population_displaced: 162,
      },
      IND1: {
        n_structures: 176,
        n_damaged: 24,
        n_destroyed: 12,
        expected_loss_usd: 7_400_000,
        loss_percentile_95_usd: 7_000_000,
        population: 260,
        population_displaced: 123,
      },
    },
    pelicun_run_id: "01HF2X3YM2N7QYZJ7E0H8WQ5XZ",
    damage_layer_uri:
      "gs://grace2-runs/sessions/fort-myers-2026/damage_assessment_01HF2X3YM2N7.fgb",
    structure_inventory_source: "USACE_NSI",
    flood_layer_uri:
      "gs://grace2-runs/sessions/fort-myers-2026/flood_depth_max_cog.tif",
    fragility_set: "hazus_flood_v6",
    realization_count: 200,
    generated_at: "2026-06-09T14:32:18Z",
  };
}

// Patch matchMedia for the reduced-motion test. Default = false (motion ok).
function mockMatchMedia(matches: boolean): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }));
}

beforeEach(() => {
  mockMatchMedia(false);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// Test 1: All 4 headline stat cards render on a complete envelope.
// ---------------------------------------------------------------------------

describe("ImpactPanel — headline stat cards", () => {
  it("renders all 4 stat cards when given a complete ImpactEnvelope", () => {
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={() => {}} />,
    );
    expect(screen.getByTestId("grace2-impact-stat-structures"))
      .toBeInTheDocument();
    expect(screen.getByTestId("grace2-impact-stat-loss"))
      .toBeInTheDocument();
    expect(screen.getByTestId("grace2-impact-stat-population"))
      .toBeInTheDocument();
    expect(screen.getByTestId("grace2-impact-stat-area"))
      .toBeInTheDocument();
  });

  it("formats large counts with thousands separators", () => {
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={() => {}} />,
    );
    const structuresCard = screen.getByTestId("grace2-impact-stat-structures");
    expect(structuresCard.textContent).toMatch(/3,217/);
    expect(structuresCard.textContent).toMatch(/12,843/);
  });

  it("renders expected_loss_usd in compact USD format (M/B suffix)", () => {
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={() => {}} />,
    );
    const lossCard = screen.getByTestId("grace2-impact-stat-loss");
    // 312_500_000 → "$312.50M"
    expect(lossCard.textContent).toMatch(/\$312\.5\dM/);
  });
});

// ---------------------------------------------------------------------------
// Test 2: Hides population stats when population_total === null.
// ---------------------------------------------------------------------------

describe("ImpactPanel — population gating", () => {
  it("hides population stat card when population_total is null (MS_BUILDINGS path)", () => {
    const fx = fortMyersFixture();
    fx.population_total = null;
    fx.population_displaced = null;
    fx.population_at_high_risk = null;
    fx.structure_inventory_source = "MS_BUILDINGS";
    render(<ImpactPanel envelope={fx} onClose={() => {}} />);
    expect(screen.queryByTestId("grace2-impact-stat-population"))
      .toBeNull();
    // The 4-card grid degrades to 3 cards; structures + loss + area remain.
    expect(screen.getByTestId("grace2-impact-stat-structures"))
      .toBeInTheDocument();
    expect(screen.getByTestId("grace2-impact-stat-loss"))
      .toBeInTheDocument();
    expect(screen.getByTestId("grace2-impact-stat-area"))
      .toBeInTheDocument();
  });

  it("renders MS_BUILDINGS badge in provenance footer", () => {
    const fx = fortMyersFixture();
    fx.population_total = null;
    fx.structure_inventory_source = "MS_BUILDINGS";
    render(<ImpactPanel envelope={fx} onClose={() => {}} />);
    const badge = screen.getByTestId("grace2-impact-provenance-source-badge");
    expect(badge).toHaveTextContent("MS_BUILDINGS");
  });
});

// ---------------------------------------------------------------------------
// Test 3: Damage state distribution bars.
// ---------------------------------------------------------------------------

describe("ImpactPanel — damage-state distribution", () => {
  it("renders one bar per DS bucket (DS0..DS4)", () => {
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={() => {}} />,
    );
    expect(screen.getByTestId("grace2-impact-ds-row-DS0_none"))
      .toBeInTheDocument();
    expect(screen.getByTestId("grace2-impact-ds-row-DS1_slight"))
      .toBeInTheDocument();
    expect(screen.getByTestId("grace2-impact-ds-row-DS2_moderate"))
      .toBeInTheDocument();
    expect(screen.getByTestId("grace2-impact-ds-row-DS3_extensive"))
      .toBeInTheDocument();
    expect(screen.getByTestId("grace2-impact-ds-row-DS4_complete"))
      .toBeInTheDocument();
  });

  it("renders each bar fill with a non-empty width when counts are nonzero", () => {
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={() => {}} />,
    );
    const bar = screen.getByTestId("grace2-impact-ds-bar-DS0_none");
    expect((bar as HTMLElement).style.width).toMatch(/\d+(\.\d+)?%/);
    expect((bar as HTMLElement).style.width).not.toBe("0%");
  });

  it("zero counts render with zero-width bar fill (no crash)", () => {
    const fx = fortMyersFixture();
    fx.damage_state_distribution = {
      DS0_none: 100,
      DS1_slight: 0,
      DS2_moderate: 0,
      DS3_extensive: 0,
      DS4_complete: 0,
    };
    render(<ImpactPanel envelope={fx} onClose={() => {}} />);
    const ds1Bar = screen.getByTestId("grace2-impact-ds-bar-DS1_slight");
    expect((ds1Bar as HTMLElement).style.width).toBe("0%");
  });
});

// ---------------------------------------------------------------------------
// Test 4: Per-occupancy-class table.
// ---------------------------------------------------------------------------

describe("ImpactPanel — per-occupancy-class table", () => {
  it("renders one row per occupancy class present in by_occupancy_class", () => {
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={() => {}} />,
    );
    expect(screen.getByTestId("grace2-impact-occupancy-row-RES1"))
      .toBeInTheDocument();
    expect(screen.getByTestId("grace2-impact-occupancy-row-RES3"))
      .toBeInTheDocument();
    expect(screen.getByTestId("grace2-impact-occupancy-row-COM1"))
      .toBeInTheDocument();
    expect(screen.getByTestId("grace2-impact-occupancy-row-IND1"))
      .toBeInTheDocument();
  });

  it("sorts rows by n_damaged descending", () => {
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={() => {}} />,
    );
    const tableBody = screen.getByTestId("grace2-impact-occupancy-table");
    const rows = tableBody.querySelectorAll(
      "[data-testid^='grace2-impact-occupancy-row-']",
    );
    // RES1=2910 > RES3=184 > COM1=99 > IND1=24
    const classes = Array.from(rows).map(
      (r) =>
        (r as HTMLElement).getAttribute("data-testid")?.replace(
          "grace2-impact-occupancy-row-",
          "",
        ) ?? "",
    );
    expect(classes).toEqual(["RES1", "RES3", "COM1", "IND1"]);
  });

  it("renders '—' for population when an occupancy class lacks pop data", () => {
    const fx = fortMyersFixture();
    const res1 = fx.by_occupancy_class.RES1;
    if (!res1) throw new Error("fixture missing RES1");
    fx.by_occupancy_class.RES1 = {
      n_structures: res1.n_structures,
      n_damaged: res1.n_damaged,
      n_destroyed: res1.n_destroyed,
      expected_loss_usd: res1.expected_loss_usd,
      loss_percentile_95_usd: res1.loss_percentile_95_usd,
      population: null,
      population_displaced: null,
    };
    render(<ImpactPanel envelope={fx} onClose={() => {}} />);
    const row = screen.getByTestId("grace2-impact-occupancy-row-RES1");
    // Last <td> is population; should be "—".
    expect(row.textContent).toMatch(/—/);
  });
});

// ---------------------------------------------------------------------------
// Test 5: Provenance footer — truncated run_id + inventory source badge.
// ---------------------------------------------------------------------------

describe("ImpactPanel — provenance footer", () => {
  it("renders truncated pelicun_run_id (first 8 + last 4 chars)", () => {
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={() => {}} />,
    );
    const runid = screen.getByTestId("grace2-impact-provenance-runid");
    // Original = 01HF2X3YM2N7QYZJ7E0H8WQ5XZ (26 chars).
    // Truncated = 01HF2X3Y…Q5XZ.
    expect(runid.textContent).toContain("01HF2X3Y");
    expect(runid.textContent).toContain("Q5XZ");
    expect(runid.textContent).toContain("…");
    expect(runid.textContent?.length).toBeLessThan(20);
  });

  it("renders USACE_NSI badge for the NSI-path envelope", () => {
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={() => {}} />,
    );
    const badge = screen.getByTestId("grace2-impact-provenance-source-badge");
    expect(badge).toHaveTextContent("USACE_NSI");
  });

  it("exposes source LayerURIs in a collapsible details block", () => {
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={() => {}} />,
    );
    const uris = screen.getByTestId("grace2-impact-provenance-uris");
    expect(uris.textContent).toContain("gs://grace2-runs/sessions/fort-myers-2026/damage_assessment");
    expect(uris.textContent).toContain("gs://grace2-runs/sessions/fort-myers-2026/flood_depth_max_cog.tif");
  });
});

// ---------------------------------------------------------------------------
// Test 6: prefers-reduced-motion respect.
// ---------------------------------------------------------------------------

describe("ImpactPanel — reduced-motion respect", () => {
  it("does NOT apply slide-in animation when prefers-reduced-motion=reduce", () => {
    mockMatchMedia(true); // user prefers reduced motion
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={() => {}} />,
    );
    const panel = screen.getByTestId("grace2-impact-panel");
    expect(panel.getAttribute("data-reduced-motion")).toBe("true");
    const inlineStyle = (panel as HTMLElement).getAttribute("style") ?? "";
    expect(inlineStyle).not.toMatch(/grace2-impact-slide-in/);
  });

  it("applies slide-in animation when prefers-reduced-motion=no-preference", () => {
    mockMatchMedia(false);
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={() => {}} />,
    );
    const panel = screen.getByTestId("grace2-impact-panel");
    expect(panel.getAttribute("data-reduced-motion")).toBe("false");
    const inlineStyle = (panel as HTMLElement).getAttribute("style") ?? "";
    expect(inlineStyle).toMatch(/grace2-impact-slide-in/);
  });
});

// ---------------------------------------------------------------------------
// Test 7: Close button dismisses the panel.
// ---------------------------------------------------------------------------

describe("ImpactPanel — dismissal", () => {
  it("close button calls onClose", () => {
    const onClose = vi.fn();
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={onClose} />,
    );
    const btn = screen.getByTestId("grace2-impact-panel-close");
    fireEvent.click(btn);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("backdrop click calls onClose", () => {
    const onClose = vi.fn();
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={onClose} />,
    );
    const backdrop = screen.getByTestId("grace2-impact-panel-backdrop");
    fireEvent.click(backdrop);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("Escape key calls onClose", () => {
    const onClose = vi.fn();
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={onClose} />,
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("clicking inside the panel does NOT dismiss", () => {
    const onClose = vi.fn();
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={onClose} />,
    );
    const panel = screen.getByTestId("grace2-impact-panel");
    fireEvent.click(panel);
    expect(onClose).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Test 8: Fixture validates against the schema fields the panel reads.
//
// We can't import the Pydantic class from TS, but we can check structural
// invariants the schema enforces (the panel will misrender without them):
//   - n_structures_damaged <= n_structures_total
//   - Sum of damage_state_distribution.values() === n_structures_total
//   - bbox length === 4 and values are real
//   - by_occupancy_class is a non-empty object (for the fixture)
//   - structure_inventory_source ∈ {USACE_NSI,MS_BUILDINGS,USER_SUPPLIED}
//   - All required scalar fields present with correct types
// ---------------------------------------------------------------------------

describe("ImpactPanel — fixture schema-shape validation", () => {
  it("Fort Myers fixture obeys ImpactEnvelope schema invariants", () => {
    const fx = fortMyersFixture();
    // Damage counts ordering.
    expect(fx.n_structures_damaged).toBeLessThanOrEqual(fx.n_structures_total);
    expect(fx.n_structures_destroyed).toBeLessThanOrEqual(
      fx.n_structures_damaged,
    );

    // Damage state distribution sums to total.
    const dsSum = Object.values(fx.damage_state_distribution).reduce(
      (a, b) => (a ?? 0) + (b ?? 0),
      0,
    );
    expect(dsSum).toBe(fx.n_structures_total);

    // bbox is a 4-tuple.
    expect(fx.bbox.length).toBe(4);
    for (const v of fx.bbox) {
      expect(Number.isFinite(v)).toBe(true);
    }

    // Loss invariants.
    expect(fx.expected_loss_usd).toBeGreaterThanOrEqual(0);
    expect(fx.loss_percentile_95_usd).toBeGreaterThanOrEqual(
      fx.expected_loss_usd,
    );
    expect(fx.damaged_replacement_value_usd).toBeLessThanOrEqual(
      fx.total_replacement_value_usd,
    );

    // Per-occupancy: every class's n_damaged ≤ n_structures.
    for (const [, cls] of Object.entries(fx.by_occupancy_class)) {
      expect(cls.n_damaged).toBeLessThanOrEqual(cls.n_structures);
      expect(cls.n_destroyed).toBeLessThanOrEqual(cls.n_damaged);
    }

    // Provenance closed-set check.
    expect(
      ["USACE_NSI", "MS_BUILDINGS", "USER_SUPPLIED"].includes(
        fx.structure_inventory_source,
      ),
    ).toBe(true);

    expect(fx.pelicun_run_id.length).toBe(26); // ULID
    expect(fx.realization_count).toBeGreaterThan(0);
    expect(fx.damage_layer_uri.startsWith("gs://")).toBe(true);
    expect(fx.flood_layer_uri.startsWith("gs://")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Test 9: Header carries Case name + timestamp.
// ---------------------------------------------------------------------------

describe("ImpactPanel — header surface", () => {
  it("renders caseName when provided", () => {
    render(
      <ImpactPanel
        envelope={fortMyersFixture()}
        caseName="Fort Myers · Ian"
        onClose={() => {}}
      />,
    );
    const title = screen.getByTestId("grace2-impact-panel-title");
    expect(title).toBeInTheDocument();
    // caseName is in the subtitle, not the title itself.
    expect(screen.getByText(/Fort Myers · Ian/)).toBeInTheDocument();
  });

  it("renders generated_at in the subtitle", () => {
    render(
      <ImpactPanel envelope={fortMyersFixture()} onClose={() => {}} />,
    );
    const ts = screen.getByTestId("grace2-impact-panel-timestamp");
    // 2026-06-09T14:32:18Z → "2026-06-09 14:32:18 UTC"
    expect(ts.textContent).toContain("2026-06-09");
    expect(ts.textContent).toContain("14:32:18");
  });
});
