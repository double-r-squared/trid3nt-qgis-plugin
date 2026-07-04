// GRACE-2 web — SourceSuggestionInline tests (job-0145, sprint-12-mega Wave 4).
//
// Coverage:
//   1. Renders the inline card when a candidate envelope arrives.
//   2. No "Mode 2" / "Mode 1" / "Tier 1/2" / "OQ-" text in output.
//   3. Detected patterns translate to user-friendly phrases (json-ld → "Has
//      machine-readable metadata"); unknown tokens are dropped.
//   4. Confidence renders as "{N}% match", never a raw decimal.
//   5. Snippet renders when present.
//   6. "Add data source" emits add action.
//   7. "Maybe later" emits dismiss action.
//   8. "Don't suggest this domain again" suppresses the domain (localStorage)
//      and emits the suppress action.
//   9. Suppressed-domain candidates do NOT surface.
//  10. Multiple distinct candidates render as a stack; dedupe by candidate_id.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import {
  SourceSuggestionAction,
  SourceSuggestionInline,
} from "./components/SourceSuggestionInline";
import {
  SOURCE_SUGGESTION_SUPPRESSION_STORAGE_KEY,
  SourceCandidate,
  SourceCandidatePayload,
  clearSuppressions,
  isSuppressed,
  listSuppressed,
  suppressDomain,
} from "./lib/source_suggestion_suppression";

// --- Helpers -------------------------------------------------------------- //

function makeCandidate(
  overrides: Partial<SourceCandidate> = {},
): SourceCandidate {
  return {
    candidate_id: "01HFAKE0000000000000000001",
    url: "https://water.weather.gov/ahps/",
    domain: "water.weather.gov",
    domain_tld: "gov",
    confidence: 0.7,
    detected_patterns: ["json-ld", "data-download-link"],
    title: "NWS AHPS Water",
    suggested_tool_kind: "endpoint",
    snippet: '<a href="/openapi.json">Download CSV</a>',
    ...overrides,
  };
}

function makeEnvelope(c: SourceCandidate): SourceCandidatePayload {
  return { envelope_type: "mode2-candidate", candidate: c };
}

interface SubscribeHarness {
  subscribe: (cb: (p: SourceCandidatePayload) => void) => () => void;
  emit: (p: SourceCandidatePayload) => void;
}

function createSubscribeHarness(): SubscribeHarness {
  const subscribers = new Set<(p: SourceCandidatePayload) => void>();
  return {
    subscribe: (cb) => {
      subscribers.add(cb);
      return () => {
        subscribers.delete(cb);
      };
    },
    emit: (p) => {
      subscribers.forEach((cb) => cb(p));
    },
  };
}

// --- Bookkeeping --------------------------------------------------------- //

beforeEach(() => {
  try {
    window.localStorage.removeItem(SOURCE_SUGGESTION_SUPPRESSION_STORAGE_KEY);
    window.localStorage.removeItem("grace2.mode2_suppressed_domains");
  } catch {
    // ignore
  }
});

afterEach(() => {
  cleanup();
  try {
    window.localStorage.removeItem(SOURCE_SUGGESTION_SUPPRESSION_STORAGE_KEY);
    window.localStorage.removeItem("grace2.mode2_suppressed_domains");
  } catch {
    // ignore
  }
});

// --- Tests --------------------------------------------------------------- //

describe("SourceSuggestionInline — render", () => {
  it("renders the inline card when a candidate envelope arrives", () => {
    const harness = createSubscribeHarness();
    render(
      <SourceSuggestionInline
        subscribeCandidate={harness.subscribe}
        onAction={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("source-suggestion-stack")).toBeNull();
    const c = makeCandidate();
    act(() => {
      harness.emit(makeEnvelope(c));
    });
    expect(screen.getByTestId("source-suggestion-stack")).toBeInTheDocument();
    expect(
      screen.getByTestId(`source-suggestion-inline-${c.candidate_id}`),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId(`source-suggestion-domain-${c.candidate_id}`),
    ).toHaveTextContent("water.weather.gov");
    expect(
      screen.getByTestId(`source-suggestion-title-${c.candidate_id}`),
    ).toHaveTextContent("NWS AHPS Water");
  });
});

describe("SourceSuggestionInline — user-facing language discipline", () => {
  it("contains NO 'Mode 2' / 'Mode 1' / 'Tier 1/2' / 'OQ-' references", () => {
    const harness = createSubscribeHarness();
    const { container } = render(
      <SourceSuggestionInline
        subscribeCandidate={harness.subscribe}
        onAction={vi.fn()}
      />,
    );
    act(() => {
      harness.emit(makeEnvelope(makeCandidate()));
    });
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/Mode\s*2/i);
    expect(text).not.toMatch(/Mode\s*1/i);
    expect(text).not.toMatch(/Tier\s*[12]/i);
    expect(text).not.toMatch(/OQ-/i);
    // Spot-check positive language.
    expect(text).toMatch(/Found a useful data source/i);
  });
});

describe("SourceSuggestionInline — pattern translation", () => {
  it("translates known detected_patterns to user-friendly phrases", () => {
    const harness = createSubscribeHarness();
    render(
      <SourceSuggestionInline
        subscribeCandidate={harness.subscribe}
        onAction={vi.fn()}
      />,
    );
    const c = makeCandidate({
      candidate_id: "01HFAKEPATTERNS0000000001",
      detected_patterns: ["json-ld", "data-download-link", "made-up-token"],
    });
    act(() => {
      harness.emit(makeEnvelope(c));
    });
    const caps = screen.getByTestId(
      `source-suggestion-capabilities-${c.candidate_id}`,
    );
    expect(caps).toHaveTextContent("Has machine-readable metadata");
    expect(caps).toHaveTextContent("Offers data downloads");
    // Unknown token dropped — must NOT surface raw.
    expect(caps).not.toHaveTextContent("made-up-token");
  });

  it("caps capability list to 3", () => {
    const harness = createSubscribeHarness();
    render(
      <SourceSuggestionInline
        subscribeCandidate={harness.subscribe}
        onAction={vi.fn()}
      />,
    );
    const c = makeCandidate({
      candidate_id: "01HFAKECAP0000000000000001",
      detected_patterns: [
        "json-ld",
        "data-download-link",
        "openapi-spec-link",
        "geojson-link",
        "wms-endpoint",
      ],
    });
    act(() => {
      harness.emit(makeEnvelope(c));
    });
    const caps = within(
      screen.getByTestId(`source-suggestion-capabilities-${c.candidate_id}`),
    );
    const chips = caps.queryAllByText(/.+/);
    // Each chip is a <span>; chips.length is the number of phrases rendered.
    expect(chips.length).toBeLessThanOrEqual(3);
  });
});

describe("SourceSuggestionInline — confidence formatting", () => {
  it("renders confidence as 'N% match', not a raw decimal", () => {
    const harness = createSubscribeHarness();
    render(
      <SourceSuggestionInline
        subscribeCandidate={harness.subscribe}
        onAction={vi.fn()}
      />,
    );
    const c = makeCandidate({
      candidate_id: "01HFAKECONFIDENCE000000001",
      confidence: 0.83,
    });
    act(() => {
      harness.emit(makeEnvelope(c));
    });
    const conf = screen.getByTestId(
      `source-suggestion-confidence-${c.candidate_id}`,
    );
    expect(conf).toHaveTextContent("83% match");
    expect(conf.textContent ?? "").not.toMatch(/0\.83/);
  });
});

describe("SourceSuggestionInline — snippet", () => {
  it("renders the page snippet when present", () => {
    const harness = createSubscribeHarness();
    render(
      <SourceSuggestionInline
        subscribeCandidate={harness.subscribe}
        onAction={vi.fn()}
      />,
    );
    const c = makeCandidate({
      candidate_id: "01HFAKESNIPPET00000000001",
      snippet: "Hello snippet world",
    });
    act(() => {
      harness.emit(makeEnvelope(c));
    });
    expect(
      screen.getByTestId(`source-suggestion-snippet-${c.candidate_id}`),
    ).toHaveTextContent("Hello snippet world");
  });
});

describe("SourceSuggestionInline — actions", () => {
  it("Add data source emits add action and removes the card", () => {
    const harness = createSubscribeHarness();
    const onAction = vi.fn();
    render(
      <SourceSuggestionInline
        subscribeCandidate={harness.subscribe}
        onAction={onAction}
      />,
    );
    const c = makeCandidate({ candidate_id: "01HFAKEADD0000000000000001" });
    act(() => {
      harness.emit(makeEnvelope(c));
    });
    fireEvent.click(screen.getByTestId(`source-suggestion-add-${c.candidate_id}`));
    expect(onAction).toHaveBeenCalledWith({
      kind: "add",
      candidate: c,
    } satisfies SourceSuggestionAction);
    expect(
      screen.queryByTestId(`source-suggestion-inline-${c.candidate_id}`),
    ).toBeNull();
  });

  it("Maybe later emits dismiss action", () => {
    const harness = createSubscribeHarness();
    const onAction = vi.fn();
    render(
      <SourceSuggestionInline
        subscribeCandidate={harness.subscribe}
        onAction={onAction}
      />,
    );
    const c = makeCandidate({ candidate_id: "01HFAKEDISMISS00000000001" });
    act(() => {
      harness.emit(makeEnvelope(c));
    });
    fireEvent.click(
      screen.getByTestId(`source-suggestion-dismiss-${c.candidate_id}`),
    );
    expect(onAction).toHaveBeenCalledWith({
      kind: "dismiss",
      candidate: c,
    } satisfies SourceSuggestionAction);
    expect(isSuppressed(c.domain)).toBe(false);
  });
});

describe("SourceSuggestionInline — suppression", () => {
  it("Don't-suggest-again suppresses the domain and emits suppress action", () => {
    const harness = createSubscribeHarness();
    const onAction = vi.fn();
    render(
      <SourceSuggestionInline
        subscribeCandidate={harness.subscribe}
        onAction={onAction}
      />,
    );
    const c = makeCandidate({
      candidate_id: "01HFAKESUPPRESS000000000001",
      domain: "data.usgs.gov",
    });
    act(() => {
      harness.emit(makeEnvelope(c));
    });
    expect(isSuppressed("data.usgs.gov")).toBe(false);
    fireEvent.click(
      screen.getByTestId(`source-suggestion-suppress-${c.candidate_id}`),
    );
    expect(isSuppressed("data.usgs.gov")).toBe(true);
    expect(listSuppressed()).toContain("data.usgs.gov");
    expect(onAction).toHaveBeenCalledWith({
      kind: "suppress",
      candidate: c,
    } satisfies SourceSuggestionAction);

    // A future candidate from the same domain must NOT surface.
    act(() => {
      harness.emit(
        makeEnvelope(
          makeCandidate({
            candidate_id: "01HFAKESUPPRESSED000000002",
            domain: "data.usgs.gov",
          }),
        ),
      );
    });
    expect(
      screen.queryByTestId("source-suggestion-inline-01HFAKESUPPRESSED000000002"),
    ).toBeNull();
  });

  it("pre-suppressed domain does not surface", () => {
    suppressDomain("water.weather.gov");
    const harness = createSubscribeHarness();
    render(
      <SourceSuggestionInline
        subscribeCandidate={harness.subscribe}
        onAction={vi.fn()}
      />,
    );
    act(() => {
      harness.emit(makeEnvelope(makeCandidate()));
    });
    expect(screen.queryByTestId("source-suggestion-stack")).toBeNull();
  });
});

describe("SourceSuggestionInline — multiple candidates", () => {
  it("stacks two distinct candidates", () => {
    const harness = createSubscribeHarness();
    render(
      <SourceSuggestionInline
        subscribeCandidate={harness.subscribe}
        onAction={vi.fn()}
      />,
    );
    const c1 = makeCandidate({
      candidate_id: "01HFAKESTACK01000000000001",
      domain: "a.gov",
    });
    const c2 = makeCandidate({
      candidate_id: "01HFAKESTACK02000000000002",
      domain: "b.edu",
    });
    act(() => {
      harness.emit(makeEnvelope(c1));
      harness.emit(makeEnvelope(c2));
    });
    expect(
      screen.getByTestId(`source-suggestion-inline-${c1.candidate_id}`),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId(`source-suggestion-inline-${c2.candidate_id}`),
    ).toBeInTheDocument();
  });

  it("dedupes by candidate_id", () => {
    const harness = createSubscribeHarness();
    render(
      <SourceSuggestionInline
        subscribeCandidate={harness.subscribe}
        onAction={vi.fn()}
      />,
    );
    const c = makeCandidate({ candidate_id: "01HFAKEDUPE00000000000001" });
    act(() => {
      harness.emit(makeEnvelope(c));
      harness.emit(makeEnvelope(c));
    });
    const all = screen.getAllByTestId(
      `source-suggestion-inline-${c.candidate_id}`,
    );
    expect(all).toHaveLength(1);
  });
});

// --- Suppression helper unit tests --------------------------------------- //

describe("source_suggestion_suppression helpers", () => {
  it("suppressDomain is case-insensitive and idempotent", () => {
    suppressDomain("Water.Weather.GOV");
    expect(isSuppressed("water.weather.gov")).toBe(true);
    expect(isSuppressed("WATER.WEATHER.GOV")).toBe(true);
    suppressDomain("water.weather.gov");
    expect(listSuppressed()).toHaveLength(1);
  });

  it("clearSuppressions empties the list", () => {
    suppressDomain("a.gov");
    suppressDomain("b.edu");
    expect(listSuppressed()).toHaveLength(2);
    clearSuppressions();
    expect(listSuppressed()).toHaveLength(0);
  });

  it("merges legacy mode2 storage key on read (one-cycle migration)", () => {
    try {
      window.localStorage.setItem(
        "grace2.mode2_suppressed_domains",
        JSON.stringify(["legacy.example.gov"]),
      );
    } catch {
      // localStorage unavailable — skip
      return;
    }
    expect(isSuppressed("legacy.example.gov")).toBe(true);
  });
});
