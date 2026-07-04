// GRACE-2 web - csvFromFeatures unit tests (L3-web-station-csv).
//
// csvFromFeatures is the PURE, exported helper Map.tsx uses to flatten an array
// of station feature property bags into an RFC-4180 CSV string for the
// Download-CSV affordance on station popups. These tests pin the RFC-4180
// quoting rules + the header derivation (geometry / internal keys excluded).
//
// maplibre-gl is stubbed to a no-op default export so importing Map.tsx (which
// imports maplibre-gl at module top level) does not need WebGL under happy-dom.
// We only exercise the pure helper, not MapView.

import { describe, it, expect, vi } from "vitest";

vi.mock("maplibre-gl", () => {
  class MockMap {}
  class MockNavigationControl {}
  return {
    default: { Map: MockMap, NavigationControl: MockNavigationControl },
    Map: MockMap,
    NavigationControl: MockNavigationControl,
  };
});

import { csvFromFeatures } from "./Map";

describe("csvFromFeatures", () => {
  it("produces a header + one CRLF-terminated row per feature", () => {
    const csv = csvFromFeatures([
      { site_no: "02358000", name: "Apalachicola R" },
      { site_no: "02359170", name: "Brothers R" },
    ]);
    expect(csv).toBe(
      ["site_no,name", "02358000,Apalachicola R", "02359170,Brothers R"].join(
        "\r\n",
      ),
    );
  });

  it("RFC-4180 quotes fields containing comma, quote, CR or LF", () => {
    const csv = csvFromFeatures([
      {
        a: "has,comma",
        b: 'has "quote"',
        c: "line1\nline2",
        d: "plain",
      },
    ]);
    const lines = csv.split("\r\n");
    expect(lines[0]).toBe("a,b,c,d");
    // comma -> quoted; quote -> quoted + doubled; newline -> quoted; plain -> bare.
    expect(lines[1]).toBe('"has,comma","has ""quote""","line1\nline2",plain');
  });

  it("takes the UNION of keys across rows (first-seen order) and leaves missing cells empty", () => {
    const csv = csvFromFeatures([
      { site_no: "1", temp_c: 21 },
      { site_no: "2", gauge_height_ft: 4.3 },
    ]);
    const lines = csv.split("\r\n");
    expect(lines[0]).toBe("site_no,temp_c,gauge_height_ft");
    expect(lines[1]).toBe("1,21,");
    expect(lines[2]).toBe("2,,4.3");
  });

  it("excludes geometry + internal/noise keys from the derived header", () => {
    const csv = csvFromFeatures([
      { site_no: "1", geometry: { type: "Point" }, id: 7, name: "X" },
    ]);
    const header = csv.split("\r\n")[0];
    expect(header).toBe("site_no,name");
    expect(header).not.toContain("geometry");
    expect(header).not.toContain("id");
  });

  it("honors an explicit columns override (order + selection)", () => {
    const csv = csvFromFeatures(
      [{ a: 1, b: 2, c: 3 }],
      ["c", "a"],
    );
    expect(csv).toBe(["c,a", "3,1"].join("\r\n"));
  });

  it("serializes objects/arrays as JSON and renders an empty cell for null/undefined/non-finite", () => {
    const csv = csvFromFeatures([
      { obj: { k: "v" }, arr: [1, 2], nul: null, bad: NaN, ok: true },
    ]);
    const lines = csv.split("\r\n");
    expect(lines[0]).toBe("obj,arr,nul,bad,ok");
    // {"k":"v"} contains a comma -> quoted+doubled; [1,2] contains a comma -> quoted.
    expect(lines[1]).toBe('"{""k"":""v""}","[1,2]",,,true');
  });

  it("handles an empty input as a header-only (empty) string", () => {
    expect(csvFromFeatures([])).toBe("");
    expect(csvFromFeatures([{}])).toBe("\r\n");
  });
});
