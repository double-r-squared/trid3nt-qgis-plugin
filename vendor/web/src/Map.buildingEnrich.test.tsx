// GRACE-2 web - building footprint click-to-enrich tests (NATE 2026-06-27).
//
// Covers the PURE half of the slim-footprint enrich path:
//   1. buildFeaturePopupData on SLIM id-only props -> no osm_id/osm_type/fid rows.
//   2. mergeTagsIntoAttributes -> merges humanized tags, promotes a fallback
//      title to the tag `name`, is idempotent, and never duplicates a row.
//   3. FeaturePopup renders a "Loading details..." row when `enriching` is set
//      and hides it once tags merged (enriching:false).
//   4. NON-footprint popups (no enriching flag) are byte-for-byte unchanged
//      (no loading row, "No additional attributes" empty still renders).

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { buildFeaturePopupData, mergeTagsIntoAttributes } from "./Map";
import { FeaturePopup, type FeaturePopupData } from "./components/FeaturePopup";

const PT = { x: 10, y: 10 };
const CANVAS = { width: 400, height: 400 };

describe("buildFeaturePopupData on slim footprint props", () => {
  it("omits the id-only join keys (osm_id / osm_type / fid) from the rows", () => {
    const data = buildFeaturePopupData(
      { osm_id: 123456, osm_type: "way", fid: "w123456" },
      PT,
      { layerName: "Buildings (OSM)", geomKindLabel: "Polygon" },
    );
    const labels = data.attributes.map((a) => a.label.toLowerCase());
    expect(labels).not.toContain("osm id");
    expect(labels).not.toContain("osm type");
    expect(labels).not.toContain("fid");
    // With everything hidden, the slim popup has no attribute rows yet.
    expect(data.attributes.length).toBe(0);
  });
});

describe("mergeTagsIntoAttributes", () => {
  function slim(): FeaturePopupData {
    return buildFeaturePopupData(
      { osm_id: 123456, osm_type: "way", fid: "w123456" },
      PT,
      { layerName: "Buildings (OSM)", geomKindLabel: "Polygon" },
    );
  }

  it("merges humanized tag rows into the popup attributes", () => {
    const merged = mergeTagsIntoAttributes(slim(), {
      building: "house",
      height: "8",
      "addr:street": "Main St",
    });
    const byLabel = new Map(merged.attributes.map((a) => [a.label, a.value]));
    expect(byLabel.get("Building")).toBe("house");
    expect(byLabel.get("Height")).toBe("8");
    // The humanizer (unchanged) leaves a `:` intact -> "Addr:street".
    expect(byLabel.get("Addr:street")).toBe("Main St");
  });

  it("promotes a fallback-title popup to the tag name", () => {
    // A footprint with no name resolves a geometry-kind/layer fallback title.
    const base = buildFeaturePopupData(
      { osm_id: 1, osm_type: "way", fid: "w1" },
      PT,
      { geomKindLabel: "Polygon" },
    );
    expect(base.title).toBe("Polygon");
    const merged = mergeTagsIntoAttributes(base, { name: "City Hall", building: "civic" });
    expect(merged.title).toBe("City Hall");
    // The name is the title, NOT also a row.
    const labels = merged.attributes.map((a) => a.label.toLowerCase());
    expect(labels).not.toContain("name");
  });

  it("is idempotent and never duplicates an already-present row", () => {
    const once = mergeTagsIntoAttributes(slim(), { building: "house" });
    const twice = mergeTagsIntoAttributes(once, { building: "house" });
    const buildingRows = twice.attributes.filter((a) => a.label === "Building");
    expect(buildingRows.length).toBe(1);
  });

  it("does not mutate the input popup", () => {
    const base = slim();
    const before = base.attributes.length;
    mergeTagsIntoAttributes(base, { building: "house" });
    expect(base.attributes.length).toBe(before);
  });
});

describe("FeaturePopup enriching loading state", () => {
  it("shows a 'Loading details...' row while enriching", () => {
    const data: FeaturePopupData = {
      title: "Building",
      attributes: [],
      point: PT,
      enriching: true,
      enrichFid: "w1",
    };
    render(<FeaturePopup data={data} canvasSize={CANVAS} isMobile={false} onClose={() => {}} />);
    expect(screen.getByTestId("feature-popup-enriching")).toBeTruthy();
  });

  it("does NOT show the loading row once enriching is false", () => {
    const data: FeaturePopupData = {
      title: "Maison",
      attributes: [{ label: "Building", value: "house" }],
      point: PT,
      enriching: false,
      enrichFid: "w1",
    };
    render(<FeaturePopup data={data} canvasSize={CANVAS} isMobile={false} onClose={() => {}} />);
    expect(screen.queryByTestId("feature-popup-enriching")).toBeNull();
    expect(screen.getByText("house")).toBeTruthy();
  });

  it("leaves a NON-footprint popup byte-for-byte unchanged (no loading row)", () => {
    // A typical station/WDPA popup carries NO enriching flag.
    const data: FeaturePopupData = {
      title: "Some Park",
      subtitle: "National Park",
      attributes: [{ label: "Area", value: "12 km2" }],
      point: PT,
    };
    render(<FeaturePopup data={data} canvasSize={CANVAS} isMobile={false} onClose={() => {}} />);
    expect(screen.queryByTestId("feature-popup-enriching")).toBeNull();
    expect(screen.getByText("Some Park")).toBeTruthy();
    expect(screen.getByText("12 km2")).toBeTruthy();
  });
});

// FOOTPRINT ENRICH TERMINAL STATE (NATE 2026-06-28): a null detail fetch (the
// agent box is asleep / the 10s timeout fired) used to silently clear the
// "Loading details..." row with no message -> a bare card that read as "loaded
// then stopped". The fix sets enrichFailed:true so FeaturePopup shows an honest
// terminal message. These cover BOTH the slim-attrs region AND the no-attrs
// region, the unchanged success/loading paths, and a non-footprint popup.
describe("FeaturePopup enrich TERMINAL FAILURE state", () => {
  const FAILED_TEXT =
    "Details unavailable -- the agent must be awake to load building details.";

  it("shows the honest failure line when enrichFailed is set WITH slim attrs", () => {
    const data: FeaturePopupData = {
      title: "Building",
      attributes: [{ label: "Building", value: "yes" }],
      point: PT,
      enriching: false,
      enrichFailed: true,
      enrichFid: "w1",
    };
    render(<FeaturePopup data={data} canvasSize={CANVAS} isMobile={false} onClose={() => {}} />);
    expect(screen.getByTestId("feature-popup-enrich-failed")).toBeTruthy();
    expect(screen.getByText(FAILED_TEXT)).toBeTruthy();
    // The loading row is gone (the spinner stopped) ...
    expect(screen.queryByTestId("feature-popup-enriching")).toBeNull();
  });

  it("shows the honest failure line when enrichFailed is set with NO attrs (slim-empty footprint)", () => {
    const data: FeaturePopupData = {
      title: "Building",
      attributes: [],
      point: PT,
      enriching: false,
      enrichFailed: true,
      enrichFid: "w1",
    };
    render(<FeaturePopup data={data} canvasSize={CANVAS} isMobile={false} onClose={() => {}} />);
    expect(screen.getByTestId("feature-popup-enrich-failed")).toBeTruthy();
    expect(screen.getByText(FAILED_TEXT)).toBeTruthy();
    // It REPLACES the bare "No additional attributes." empty (the bug card).
    expect(screen.queryByTestId("feature-popup-empty")).toBeNull();
  });

  it("does NOT show the failure line while still enriching (loading wins)", () => {
    const data: FeaturePopupData = {
      title: "Building",
      attributes: [],
      point: PT,
      enriching: true,
      enrichFid: "w1",
    };
    render(<FeaturePopup data={data} canvasSize={CANVAS} isMobile={false} onClose={() => {}} />);
    expect(screen.getByTestId("feature-popup-enriching")).toBeTruthy();
    expect(screen.queryByTestId("feature-popup-enrich-failed")).toBeNull();
  });

  it("a SUCCESSFUL merge (enrichFailed unset) shows tags + no failure line", () => {
    // The real Map.tsx success path merges tags + sets enriching:false WITHOUT
    // enrichFailed. mergeTagsIntoAttributes is the merge; assert no failure row.
    const merged = mergeTagsIntoAttributes(
      buildFeaturePopupData(
        { osm_id: 1, osm_type: "way", fid: "w1" },
        PT,
        { layerName: "Buildings (OSM)", geomKindLabel: "Polygon" },
      ),
      { building: "house" },
    );
    const data: FeaturePopupData = { ...merged, enriching: false };
    expect(data.enrichFailed).toBeUndefined();
    render(<FeaturePopup data={data} canvasSize={CANVAS} isMobile={false} onClose={() => {}} />);
    expect(screen.getByText("house")).toBeTruthy();
    expect(screen.queryByTestId("feature-popup-enrich-failed")).toBeNull();
  });

  it("a NON-footprint popup never shows the failure line (no enrichFailed flag)", () => {
    const data: FeaturePopupData = {
      title: "Some Park",
      subtitle: "National Park",
      attributes: [{ label: "Area", value: "12 km2" }],
      point: PT,
    };
    render(<FeaturePopup data={data} canvasSize={CANVAS} isMobile={false} onClose={() => {}} />);
    expect(screen.queryByTestId("feature-popup-enrich-failed")).toBeNull();
    // The plain no-extra-attrs popup still falls through normally.
    expect(screen.getByText("12 km2")).toBeTruthy();
  });
});
