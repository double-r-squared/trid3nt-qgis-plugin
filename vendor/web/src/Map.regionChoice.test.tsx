// GRACE-2 web — Map region-choice choropleth + bus-sync tests
// (state-bbox-fallback narrowing).
//
// The full MapView mount uses a heavy maplibre-gl mock (Map.test.tsx); the
// choropleth sync mechanics are exercised here through the EXPORTED pure
// helpers + the region-choice bus, following the same pure-helper pattern the
// rest of the suite uses:
//   - buildRegionChoiceGeoJson turns the candidate bboxes into one keyed
//     rectangle Polygon per region_id (the tappable county choropleth).
//   - applyRegionChoiceHighlight drives per-feature feature-state from the
//     bus-synced hovered/selected ids (a CARD hover/select highlights the
//     polygon).
//   - the region-choice bus is the SYNC SEAM: a map TAP funnels back through
//     subscribePick (Chat owns the WS reply), and hover/select state fans out
//     to both surfaces; resolving clears the active request (choropleth
//     teardown).

import { describe, it, expect, vi, beforeEach } from "vitest";
import type { Map as MapLibreMap } from "maplibre-gl";
import {
  buildRegionChoiceGeoJson,
  applyRegionChoiceHighlight,
  REGION_CHOICE_SOURCE_ID,
} from "./Map";
import { regionChoiceBus } from "./lib/region_choice_bus";
import { RegionCandidate, RegionChoiceRequestPayload } from "./contracts";

function candidate(
  region_id: string,
  name: string,
  bbox: [number, number, number, number],
): RegionCandidate {
  return { region_id, name, bbox, admin_level: "county" };
}

const CANDIDATES: RegionCandidate[] = [
  candidate("county-12071", "Lee County", [-82.3, 26.3, -81.6, 26.8]),
  candidate("county-12021", "Collier County", [-81.8, 25.8, -81.0, 26.4]),
];

function makeRequest(): RegionChoiceRequestPayload {
  return {
    envelope_type: "region-choice-request",
    request_id: "01HJREGION0000000000000001",
    state_name: "Florida",
    state_code: "FL",
    state_bbox: [-87.6, 24.5, -80.0, 31.0],
    candidates: CANDIDATES,
    default_action: "use_whole_state",
    message: "'south Florida' isn't a precise place — pick an area in Florida.",
  };
}

// Minimal fake map carrying just the surface applyRegionChoiceHighlight uses:
// getSource (presence) + setFeatureState (the highlight write).
interface FakeMap {
  setFeatureState: ReturnType<typeof vi.fn>;
  getSource: ReturnType<typeof vi.fn>;
}
function fakeMap(sourcePresent = true): FakeMap {
  return {
    setFeatureState: vi.fn(),
    getSource: vi.fn(() => (sourcePresent ? {} : undefined)),
  };
}

describe("buildRegionChoiceGeoJson — candidate choropleth geometry", () => {
  it("builds one keyed rectangle Polygon per candidate (verbatim from bbox)", () => {
    const fc = buildRegionChoiceGeoJson(CANDIDATES);
    expect(fc.type).toBe("FeatureCollection");
    expect(fc.features).toHaveLength(2);
    const f0 = fc.features[0]!;
    // feature id + properties.region_id both carry the stable region_id so
    // feature-state targeting AND tap hit-test read it back.
    expect(f0.id).toBe("county-12071");
    expect(f0.properties?.region_id).toBe("county-12071");
    expect(f0.properties?.name).toBe("Lee County");
    expect(f0.geometry.type).toBe("Polygon");
    // The ring is the bbox corners, closed (verbatim — Invariant 1).
    expect(f0.geometry.coordinates[0]).toEqual([
      [-82.3, 26.3],
      [-81.6, 26.3],
      [-81.6, 26.8],
      [-82.3, 26.8],
      [-82.3, 26.3],
    ]);
  });

  it("empty candidates → empty FeatureCollection", () => {
    expect(buildRegionChoiceGeoJson([]).features).toHaveLength(0);
  });
});

describe("applyRegionChoiceHighlight — feature-state sync", () => {
  it("sets hovered/selected feature-state on the touched regions", () => {
    const m = fakeMap();
    const next = applyRegionChoiceHighlight(
      m as unknown as MapLibreMap,
      "county-12071", // hovered
      "county-12021", // selected
      new Set(),
    );
    // Both touched regions get their state written.
    expect(m.setFeatureState).toHaveBeenCalledWith(
      { source: REGION_CHOICE_SOURCE_ID, id: "county-12071" },
      { hovered: true, selected: false },
    );
    expect(m.setFeatureState).toHaveBeenCalledWith(
      { source: REGION_CHOICE_SOURCE_ID, id: "county-12021" },
      { hovered: false, selected: true },
    );
    expect(next).toEqual(new Set(["county-12071", "county-12021"]));
  });

  it("clears stale highlights that are no longer hovered/selected", () => {
    const m = fakeMap();
    // Previously county-12071 was highlighted; now nothing is.
    const next = applyRegionChoiceHighlight(
      m as unknown as MapLibreMap,
      null,
      null,
      new Set(["county-12071"]),
    );
    expect(m.setFeatureState).toHaveBeenCalledWith(
      { source: REGION_CHOICE_SOURCE_ID, id: "county-12071" },
      { hovered: false, selected: false },
    );
    expect(next.size).toBe(0);
  });

  it("is a no-op (returns empty) when the source is absent (mid-teardown)", () => {
    const m = fakeMap(false);
    const next = applyRegionChoiceHighlight(
      m as unknown as MapLibreMap,
      "county-12071",
      null,
      new Set(["county-12021"]),
    );
    expect(m.setFeatureState).not.toHaveBeenCalled();
    expect(next.size).toBe(0);
  });
});

describe("regionChoiceBus — card ↔ map sync seam", () => {
  beforeEach(() => {
    regionChoiceBus.__reset();
  });

  it("subscribe fires immediately with current state (late Map mount paints)", () => {
    const req = makeRequest();
    regionChoiceBus.setRequest(req);
    const seen: (RegionChoiceRequestPayload | null)[] = [];
    const unsub = regionChoiceBus.subscribe((st) => seen.push(st.request));
    // A subscriber that mounts AFTER the request arrived gets it on subscribe.
    expect(seen).toHaveLength(1);
    expect(seen[0]?.request_id).toBe(req.request_id);
    unsub();
  });

  it("a MAP tap relays a pick through subscribePick (Chat owns the reply)", () => {
    const req = makeRequest();
    regionChoiceBus.setRequest(req);
    const picks: string[] = [];
    const unsub = regionChoiceBus.subscribePick((id) => picks.push(id));
    regionChoiceBus.pickRegion("county-12021");
    expect(picks).toEqual(["county-12021"]);
    // The tap also sets the pre-reply selection echo so both surfaces reflect it.
    expect(regionChoiceBus.getState().selectedRegionId).toBe("county-12021");
    unsub();
  });

  it("setHovered fans out the hover to subscribers (card → map and back)", () => {
    const req = makeRequest();
    regionChoiceBus.setRequest(req);
    let lastHover: string | null = "init";
    const unsub = regionChoiceBus.subscribe((st) => {
      lastHover = st.hoveredRegionId;
    });
    regionChoiceBus.setHovered("county-12071");
    expect(lastHover).toBe("county-12071");
    regionChoiceBus.setHovered(null);
    expect(lastHover).toBeNull();
    unsub();
  });

  it("clearRequest(requestId) tears down only the matching request", () => {
    const req = makeRequest();
    regionChoiceBus.setRequest(req);
    // A stale clear for a different id is ignored (a late reply can't wipe a
    // freshly-arrived second request).
    regionChoiceBus.clearRequest("some-other-id");
    expect(regionChoiceBus.getState().request?.request_id).toBe(req.request_id);
    // The matching clear tears it down (choropleth cleanup trigger).
    regionChoiceBus.clearRequest(req.request_id);
    expect(regionChoiceBus.getState().request).toBeNull();
    expect(regionChoiceBus.getState().hoveredRegionId).toBeNull();
    expect(regionChoiceBus.getState().selectedRegionId).toBeNull();
  });

  it("setRequest resets transient hover + selection from a prior request", () => {
    regionChoiceBus.setRequest(makeRequest());
    regionChoiceBus.setHovered("county-12071");
    regionChoiceBus.setSelected("county-12021");
    // A new request arrives — hover/selection reset.
    const req2 = { ...makeRequest(), request_id: "01HJREGION0000000000000002" };
    regionChoiceBus.setRequest(req2);
    expect(regionChoiceBus.getState().hoveredRegionId).toBeNull();
    expect(regionChoiceBus.getState().selectedRegionId).toBeNull();
  });
});
