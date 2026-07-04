// GRACE-2 web — vector_rendering.ts unit tests (job-0146).
//
// Covers:
//   - Curated VECTOR_PALETTE has exactly 12 distinct colours (Part 1)
//   - paletteColorFor is deterministic per layer_id (Part 1)
//   - paletteColorFor always returns a colour from VECTOR_PALETTE (Part 1)
//   - presetColorFor returns curated colours for all known presets (Part 1)
//   - presetColorFor returns PELICUN_DAMAGE_PRESET sentinel for pelicun_damage (Part 2)
//   - isPelicunDamageLayer returns true only for pelicun_damage (Part 2)
//   - buildDsMeanExpression returns the expected ramp at 3 sample values (Part 2)
//   - POLYGON_FILL_OPACITY constant is 0.4 (Part 3)
//   - POLYGON_STROKE_WIDTH constant is 1.5 (Part 3)
//   - CLUSTER_THRESHOLD constant is 500 (Part 4)
//   - resolveVectorColor neutral-grey for pelicun (not the sentinel) (Part 2)

import { describe, it, expect } from "vitest";
import {
  VECTOR_PALETTE,
  paletteColorFor,
  presetColorFor,
  resolveVectorColor,
  geomFamilyColor,
  isPelicunDamageLayer,
  buildDsMeanExpression,
  POLYGON_FILL_OPACITY,
  POLYGON_STROKE_WIDTH,
  CLUSTER_THRESHOLD,
  CLUSTER_RADIUS,
  PELICUN_DAMAGE_PRESET,
  vectorResultFromInlineGeoJson,
  resolveVectorLineWidth,
  VECTOR_LINE_WIDTH,
  MESH_LINE_WIDTH,
  isMeshGridLayer,
  MESH_FILL_OPACITY,
  legendHasValueField,
  buildLegendFillExpression,
  LEGEND_FILL_FALLBACK,
} from "./vector_rendering";
import type { LegendKey } from "../contracts";

// ---------------------------------------------------------------------------
// job-0175 — inline GeoJSON synchronous validator
// ---------------------------------------------------------------------------

describe("vectorResultFromInlineGeoJson (job-0175)", () => {
  it("classifies polygon FeatureCollection", () => {
    const fc = {
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          geometry: { type: "Polygon", coordinates: [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]] },
          properties: {},
        },
      ],
    };
    const res = vectorResultFromInlineGeoJson(fc);
    expect(res.geomKind).toBe("polygon");
    expect(res.featureCollection.features.length).toBe(1);
  });

  it("throws on non-FeatureCollection input", () => {
    expect(() => vectorResultFromInlineGeoJson({ type: "Feature" })).toThrow(
      /not a FeatureCollection/i,
    );
    expect(() => vectorResultFromInlineGeoJson(null)).toThrow();
    expect(() => vectorResultFromInlineGeoJson(undefined)).toThrow();
  });
});

// ---------------------------------------------------------------------------
// Part 1 — Curated palette
// ---------------------------------------------------------------------------

describe("VECTOR_PALETTE — curated 12-colour palette (job-0146 Part 1)", () => {
  it("has exactly 12 entries", () => {
    expect(VECTOR_PALETTE).toHaveLength(12);
  });

  it("all 12 entries are distinct (no duplicate colours)", () => {
    const unique = new Set(VECTOR_PALETTE);
    expect(unique.size).toBe(12);
  });

  it("all entries are valid #RRGGBB hex strings", () => {
    const hexRe = /^#[0-9A-Fa-f]{6}$/;
    for (const color of VECTOR_PALETTE) {
      expect(color).toMatch(hexRe);
    }
  });
});

describe("paletteColorFor — deterministic FNV-1a hash (job-0146 Part 1)", () => {
  it("returns the same colour on repeated calls for the same layer_id", () => {
    const ids = ["panther", "spoonbill", "alligator", "gbif-layer-123", "very-long-layer-id-XYZ-99"];
    for (const id of ids) {
      expect(paletteColorFor(id)).toBe(paletteColorFor(id));
    }
  });

  it("always returns a colour from VECTOR_PALETTE", () => {
    const ids = ["a", "b", "panther", "spoonbill", "alligator", "wdpa-big-cypress", "flood-depth-demo"];
    for (const id of ids) {
      expect(VECTOR_PALETTE).toContain(paletteColorFor(id));
    }
  });

  it("produces distinct colours for the 3 Case 1 species layers", () => {
    // These 3 IDs must hash to distinct palette slots so the species are
    // visually distinguishable on the map.
    const colors = [
      paletteColorFor("panther-occurrences"),
      paletteColorFor("spoonbill-occurrences"),
      paletteColorFor("alligator-occurrences"),
    ];
    expect(new Set(colors).size).toBe(3);
  });
});

describe("presetColorFor — curated preset registry (job-0146 Part 1)", () => {
  it("maps gbif_occurrences → orange #FF7F0E", () => {
    expect(presetColorFor("gbif_occurrences")).toBe("#FF7F0E");
    expect(presetColorFor("gbif_something")).toBe("#FF7F0E");
  });

  it("maps inaturalist_observations → bright cyan #00BFFF", () => {
    expect(presetColorFor("inaturalist_observations")).toBe("#00BFFF");
    expect(presetColorFor("inat_birds")).toBe("#00BFFF");
  });

  it("maps wdpa variants → slate #708090", () => {
    expect(presetColorFor("wdpa_protected_areas")).toBe("#708090");
    expect(presetColorFor("wdpa_polygon")).toBe("#708090");
    expect(presetColorFor("wdpa")).toBe("#708090");
    expect(presetColorFor("protected_area")).toBe("#708090");
  });

  it("maps nws_alerts variants → fire red #FF4444", () => {
    expect(presetColorFor("nws_alerts")).toBe("#FF4444");
    expect(presetColorFor("nws_alert")).toBe("#FF4444");
    expect(presetColorFor("nws_warning")).toBe("#FF4444");
    expect(presetColorFor("flood_alert")).toBe("#FF4444");
  });

  it("maps mtbs_burn_severity and burn_perimeter → fire red #FF4444", () => {
    expect(presetColorFor("mtbs_burn_severity")).toBe("#FF4444");
    expect(presetColorFor("burn_perimeter")).toBe("#FF4444");
    expect(presetColorFor("mtbs")).toBe("#FF4444");
  });

  it("maps firms_active_fire variants → fire red #FF4444", () => {
    expect(presetColorFor("firms_active_fire")).toBe("#FF4444");
    expect(presetColorFor("firms")).toBe("#FF4444");
    expect(presetColorFor("active_fire")).toBe("#FF4444");
  });

  it("maps osm_roads variants → gold #FFD700", () => {
    expect(presetColorFor("osm_roads")).toBe("#FFD700");
    expect(presetColorFor("osm_road")).toBe("#FFD700");
    expect(presetColorFor("roads")).toBe("#FFD700");
  });

  it("maps water / hydrography presets → sky-blue #4477FF (job-3)", () => {
    // The agent emits osm_waterways for fetch_river_geometry; correctly-tagged
    // NHDPlus flowlines and compute_contours water layers also resolve here.
    expect(presetColorFor("osm_waterways")).toBe("#4477FF");
    expect(presetColorFor("rivers_streams")).toBe("#4477FF");
    expect(presetColorFor("nhdplus_flowlines")).toBe("#4477FF");
    expect(presetColorFor("flowline")).toBe("#4477FF");
    expect(presetColorFor("hydro_network")).toBe("#4477FF");
    expect(presetColorFor("stream_segments")).toBe("#4477FF");
  });

  it("returns PELICUN_DAMAGE_PRESET sentinel for pelicun_damage", () => {
    expect(presetColorFor("pelicun_damage")).toBe(PELICUN_DAMAGE_PRESET);
  });

  it("returns undefined for unknown presets", () => {
    expect(presetColorFor("totally_unknown")).toBeUndefined();
    expect(presetColorFor("species_roseate_spoonbill")).toBeUndefined();
    expect(presetColorFor(null)).toBeUndefined();
    expect(presetColorFor(undefined)).toBeUndefined();
    expect(presetColorFor("")).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Part 2 — Pelicun choropleth
// ---------------------------------------------------------------------------

describe("isPelicunDamageLayer (job-0146 Part 2)", () => {
  it("returns true only for exact 'pelicun_damage' preset", () => {
    expect(isPelicunDamageLayer("pelicun_damage")).toBe(true);
    expect(isPelicunDamageLayer("PELICUN_DAMAGE")).toBe(true); // case-insensitive
  });

  it("returns false for other presets and nullish inputs", () => {
    expect(isPelicunDamageLayer("wdpa_polygon")).toBe(false);
    expect(isPelicunDamageLayer("gbif_occurrences")).toBe(false);
    expect(isPelicunDamageLayer(null)).toBe(false);
    expect(isPelicunDamageLayer(undefined)).toBe(false);
    expect(isPelicunDamageLayer("")).toBe(false);
  });
});

describe("buildDsMeanExpression — green→yellow→red gradient (job-0146 Part 2)", () => {
  it("returns an array starting with 'case'", () => {
    const expr = buildDsMeanExpression();
    expect(Array.isArray(expr)).toBe(true);
    expect(expr[0]).toBe("case");
  });

  it("contains all 3 required gradient stops: green, yellow, red", () => {
    const expr = buildDsMeanExpression();
    const flat = JSON.stringify(expr);
    // Green stop (no damage)
    expect(flat).toContain("#2DC937");
    // Yellow stop (moderate)
    expect(flat).toContain("#E7B416");
    // Red stop (heavy damage)
    expect(flat).toContain("#CC3232");
  });

  it("contains the interpolate expression over ds_mean property", () => {
    const expr = buildDsMeanExpression();
    const flat = JSON.stringify(expr);
    expect(flat).toContain("interpolate");
    expect(flat).toContain("ds_mean");
  });

  it("provides a fallback colour for features without ds_mean", () => {
    const expr = buildDsMeanExpression();
    // The last element in a 'case' expr is the fallback.
    const fallback = expr[expr.length - 1];
    expect(typeof fallback).toBe("string");
    // Should be a valid hex
    expect(fallback as string).toMatch(/^#[0-9A-Fa-f]{6}$/);
  });

  it("maps stop 0.0 → green at sample position in interpolate array", () => {
    const expr = buildDsMeanExpression();
    const flat = JSON.stringify(expr);
    // 0.0 immediately precedes the green hex in the interpolate body
    expect(flat).toContain('0,"#2DC937"');
  });

  it("maps stop 0.5 → yellow at sample position in interpolate array", () => {
    const expr = buildDsMeanExpression();
    const flat = JSON.stringify(expr);
    expect(flat).toContain('0.5,"#E7B416"');
  });

  it("maps stop 1.0 → red at sample position in interpolate array", () => {
    const expr = buildDsMeanExpression();
    const flat = JSON.stringify(expr);
    expect(flat).toContain('1,"#CC3232"');
  });
});

// ---------------------------------------------------------------------------
// Part 3 — Polygon fill opacity + stroke width constants
// ---------------------------------------------------------------------------

describe("Polygon opacity + stroke constants (job-0146 Part 3)", () => {
  it("POLYGON_FILL_OPACITY is 0.4", () => {
    expect(POLYGON_FILL_OPACITY).toBe(0.4);
  });

  it("POLYGON_STROKE_WIDTH is 1.5", () => {
    expect(POLYGON_STROKE_WIDTH).toBe(1.5);
  });
});

// ---------------------------------------------------------------------------
// Part 4 — Cluster constants
// ---------------------------------------------------------------------------

describe("Cluster constants (job-0146 Part 4)", () => {
  it("CLUSTER_THRESHOLD is 500", () => {
    expect(CLUSTER_THRESHOLD).toBe(500);
  });

  it("CLUSTER_RADIUS is 50", () => {
    expect(CLUSTER_RADIUS).toBe(50);
  });
});

// ---------------------------------------------------------------------------
// resolveVectorColor — pelicun sentinel handling
// ---------------------------------------------------------------------------

describe("resolveVectorColor — pelicun fallback (job-0146)", () => {
  it("returns a neutral grey (not the PELICUN_DAMAGE_PRESET string) for pelicun_damage layers", () => {
    const color = resolveVectorColor("pelicun-damage-layer", "pelicun_damage");
    // Must NOT return the raw sentinel string (it's not a valid CSS color)
    expect(color).not.toBe(PELICUN_DAMAGE_PRESET);
    // Must be a valid hex color
    expect(color).toMatch(/^#[0-9A-Fa-f]{6}$/);
  });

  it("prefers preset over palette for non-pelicun presets", () => {
    expect(resolveVectorColor("any-id", "gbif_occurrences")).toBe("#FF7F0E");
    expect(resolveVectorColor("any-id", "osm_roads")).toBe("#FFD700");
    expect(resolveVectorColor("any-id", "wdpa_polygon")).toBe("#708090");
  });

  it("falls back to palette hash when preset is null/undefined (no geomKind)", () => {
    const id = "panther-occurrences";
    expect(resolveVectorColor(id, null)).toBe(paletteColorFor(id));
    expect(resolveVectorColor(id, undefined)).toBe(paletteColorFor(id));
  });
});

// ---------------------------------------------------------------------------
// job-3 — deterministic water colour + geometry-family fallback
// ---------------------------------------------------------------------------

describe("geomFamilyColor — geometry-family default colours (job-3)", () => {
  it("colours by geometry family deterministically", () => {
    expect(geomFamilyColor("line")).toBe("#FFD700"); // amber
    expect(geomFamilyColor("polygon")).toBe("#708090"); // slate
    expect(geomFamilyColor("point")).toBe("#FF7F0E"); // orange
    expect(geomFamilyColor("unknown")).toBe("#708090"); // neutral slate
  });
});

describe("resolveVectorColor — water preset + geometry-family (job-3)", () => {
  it("resolves a river/waterway preset to sky-blue regardless of layer_id", () => {
    expect(resolveVectorColor("rivers-1.23-4.56", "osm_waterways", "line")).toBe("#4477FF");
    expect(resolveVectorColor("nhd-flowlines-77", "nhdplus_flowlines", "line")).toBe("#4477FF");
  });

  it("two DIFFERENT river layer_ids resolve to the SAME blue (no per-id split)", () => {
    // The original bug: two rivers from different AOIs hashed to different
    // palette slots (yellow vs blue). Both must now read the same sky-blue.
    const a = resolveVectorColor("rivers-30.1234-87.5678", "osm_waterways", "line");
    const b = resolveVectorColor("rivers-44.9876-93.1111", "osm_waterways", "line");
    expect(a).toBe("#4477FF");
    expect(b).toBe("#4477FF");
    expect(a).toBe(b);
  });

  it("an unknown-preset LINE resolves to amber by geometry, NOT a per-id hash", () => {
    const a = resolveVectorColor("mystery-line-AAA", "totally_unknown", "line");
    const b = resolveVectorColor("mystery-line-ZZZ", "totally_unknown", "line");
    // Geometry-family deterministic: amber, and identical across layer_ids.
    expect(a).toBe("#FFD700");
    expect(b).toBe("#FFD700");
    expect(a).toBe(b);
    // And NOT the old per-layer-id palette hash (which would differ per id).
    expect(a).not.toBe(paletteColorFor("mystery-line-AAA"));
  });

  it("unknown-preset polygon=slate, point=orange (deterministic by geometry)", () => {
    expect(resolveVectorColor("poly-1", "totally_unknown", "polygon")).toBe("#708090");
    expect(resolveVectorColor("poly-2", "totally_unknown", "polygon")).toBe("#708090");
    expect(resolveVectorColor("pt-1", "totally_unknown", "point")).toBe("#FF7F0E");
    expect(resolveVectorColor("pt-2", "totally_unknown", "point")).toBe("#FF7F0E");
  });

  it("a known preset still wins over geometry family", () => {
    // osm_roads line stays amber via preset (same as geometry default here),
    // but gbif points stay orange-by-preset, wdpa polygons stay slate-by-preset.
    expect(resolveVectorColor("any", "gbif_occurrences", "point")).toBe("#FF7F0E");
    expect(resolveVectorColor("any", "wdpa_polygon", "polygon")).toBe("#708090");
    expect(resolveVectorColor("any", "osm_roads", "line")).toBe("#FFD700");
  });
});

// ---------------------------------------------------------------------------
// NATE #156 — computational-mesh wireframe colour + hairline width
// ---------------------------------------------------------------------------

describe("presetColorFor — mesh wireframe (NATE #156)", () => {
  it("maps mesh presets → cyan scaffold #5BC0DE", () => {
    expect(presetColorFor("mesh_grid")).toBe("#5BC0DE");
    expect(presetColorFor("computational_mesh")).toBe("#5BC0DE");
    expect(presetColorFor("sfincs_mesh")).toBe("#5BC0DE");
  });

  it("mesh wins over the water branch (no 'mesh' bleed into hydro blue)", () => {
    // A river/mesh collision must NOT resolve to the hydro sky-blue; the mesh
    // branch is ordered before the water branch.
    expect(presetColorFor("river_mesh")).toBe("#5BC0DE");
    expect(presetColorFor("river_mesh")).not.toBe("#4477FF");
  });

  it("does not disturb existing preset colours", () => {
    // Regression guard: the mesh branch must not change rivers / roads.
    expect(presetColorFor("osm_waterways")).toBe("#4477FF");
    expect(presetColorFor("osm_roads")).toBe("#FFD700");
    expect(presetColorFor("gbif_occurrences")).toBe("#FF7F0E");
  });
});

describe("resolveVectorColor — mesh line preset (NATE #156)", () => {
  it("a mesh_grid LINE layer resolves to the cyan mesh colour", () => {
    expect(resolveVectorColor("mesh-aoi-1", "mesh_grid", "line")).toBe("#5BC0DE");
  });

  it("the mesh colour is deterministic, NOT a per-layer-id hash", () => {
    const a = resolveVectorColor("mesh-30.1-87.5", "mesh_grid", "line");
    const b = resolveVectorColor("mesh-44.9-93.1", "mesh_grid", "line");
    expect(a).toBe("#5BC0DE");
    expect(b).toBe("#5BC0DE");
    expect(a).toBe(b);
    // And NOT the old per-layer-id palette hash.
    expect(a).not.toBe(paletteColorFor("mesh-30.1-87.5"));
  });

  it("the mesh cyan is DISTINCT from rivers-blue and roads-amber", () => {
    const mesh = resolveVectorColor("mesh-aoi", "mesh_grid", "line");
    const river = resolveVectorColor("river-aoi", "osm_waterways", "line");
    const road = resolveVectorColor("road-aoi", "osm_roads", "line");
    expect(mesh).toBe("#5BC0DE");
    expect(mesh).not.toBe(river); // not #4477FF
    expect(mesh).not.toBe(road); // not #FFD700
    // All three line colours are mutually distinct.
    expect(new Set([mesh, river, road]).size).toBe(3);
  });
});

describe("resolveVectorLineWidth — mesh hairline (NATE #156)", () => {
  it("mesh presets get the thinner MESH_LINE_WIDTH", () => {
    expect(resolveVectorLineWidth("mesh_grid")).toBe(MESH_LINE_WIDTH);
    expect(resolveVectorLineWidth("computational_mesh")).toBe(MESH_LINE_WIDTH);
    expect(MESH_LINE_WIDTH).toBeLessThan(VECTOR_LINE_WIDTH);
  });

  it("non-mesh / unknown / nullish presets keep the default VECTOR_LINE_WIDTH", () => {
    expect(resolveVectorLineWidth("osm_waterways")).toBe(VECTOR_LINE_WIDTH);
    expect(resolveVectorLineWidth("osm_roads")).toBe(VECTOR_LINE_WIDTH);
    expect(resolveVectorLineWidth("totally_unknown")).toBe(VECTOR_LINE_WIDTH);
    expect(resolveVectorLineWidth(null)).toBe(VECTOR_LINE_WIDTH);
    expect(resolveVectorLineWidth(undefined)).toBe(VECTOR_LINE_WIDTH);
    expect(VECTOR_LINE_WIDTH).toBe(2);
  });
});

describe("isMeshGridLayer + MESH_FILL_OPACITY — wireframe polygon gating (NATE #156)", () => {
  it("returns true for mesh presets", () => {
    expect(isMeshGridLayer("mesh_grid")).toBe(true);
    expect(isMeshGridLayer("computational_mesh")).toBe(true);
    expect(isMeshGridLayer("MESH_GRID")).toBe(true); // case-insensitive
  });

  it("returns false for non-mesh presets and nullish inputs", () => {
    expect(isMeshGridLayer("flood_depth")).toBe(false);
    expect(isMeshGridLayer("osm_waterways")).toBe(false);
    expect(isMeshGridLayer(null)).toBe(false);
    expect(isMeshGridLayer(undefined)).toBe(false);
    expect(isMeshGridLayer("")).toBe(false);
  });

  it("MESH_FILL_OPACITY is a small positive number < 0.2 (faint but clickable)", () => {
    // > 0 keeps the mesh polygon clickable for the feature popup; < 0.2 keeps
    // it effectively a wireframe (cells visible, no solid cyan blanket).
    expect(MESH_FILL_OPACITY).toBeGreaterThan(0);
    expect(MESH_FILL_OPACITY).toBeLessThan(0.2);
  });
});

// ---------------------------------------------------------------------------
// sprint-18 Wave-4 - MODFLOW PRT capture-zone / wellhead-protection violet
// ---------------------------------------------------------------------------

describe("presetColorFor - capture_zone / wellhead_protection (sprint-18 Wave-4)", () => {
  it("maps capture_zone -> violet #9B59B6", () => {
    expect(presetColorFor("capture_zone")).toBe("#9B59B6");
  });

  it("maps wellhead_protection -> violet #9B59B6", () => {
    expect(presetColorFor("wellhead_protection")).toBe("#9B59B6");
  });

  it("is case-insensitive (CAPTURE_ZONE / WELLHEAD_PROTECTION)", () => {
    expect(presetColorFor("CAPTURE_ZONE")).toBe("#9B59B6");
    expect(presetColorFor("WELLHEAD_PROTECTION")).toBe("#9B59B6");
  });

  it("resolveVectorColor picks the violet preset for capture_zone layers", () => {
    expect(resolveVectorColor("cz-layer-01", "capture_zone", "polygon")).toBe("#9B59B6");
    expect(resolveVectorColor("whp-layer-01", "wellhead_protection", "polygon")).toBe("#9B59B6");
  });

  it("is DISTINCT from rivers-blue, mesh-cyan, and roads-amber", () => {
    const cz = presetColorFor("capture_zone")!;
    const river = presetColorFor("osm_waterways")!;
    const mesh = presetColorFor("mesh_grid")!;
    const road = presetColorFor("osm_roads")!;
    expect(cz).not.toBe(river);
    expect(cz).not.toBe(mesh);
    expect(cz).not.toBe(road);
  });

  it("does not disturb existing preset colours (regression guard)", () => {
    expect(presetColorFor("osm_waterways")).toBe("#4477FF");
    expect(presetColorFor("mesh_grid")).toBe("#5BC0DE");
    expect(presetColorFor("osm_roads")).toBe("#FFD700");
    expect(presetColorFor("pelicun_damage")).toBe(PELICUN_DAMAGE_PRESET);
  });
});

// ---------------------------------------------------------------------------
// sprint-18 Wave-5 - MODFLOW BUY saltwater-intrusion transect + toe teal
// ---------------------------------------------------------------------------

describe("presetColorFor - saltwater_intrusion (sprint-18 Wave-5)", () => {
  it("maps saltwater_intrusion -> teal #1ABC9C", () => {
    expect(presetColorFor("saltwater_intrusion")).toBe("#1ABC9C");
  });

  it("is case-insensitive (SALTWATER_INTRUSION)", () => {
    expect(presetColorFor("SALTWATER_INTRUSION")).toBe("#1ABC9C");
  });

  it("resolveVectorColor picks the teal preset for saltwater_intrusion layers", () => {
    expect(resolveVectorColor("si-layer-01", "saltwater_intrusion", "line")).toBe("#1ABC9C");
  });

  it("is DISTINCT from capture-zone violet, rivers-blue, mesh-cyan, and roads-amber", () => {
    const si = presetColorFor("saltwater_intrusion")!;
    const cz = presetColorFor("capture_zone")!;
    const river = presetColorFor("osm_waterways")!;
    const mesh = presetColorFor("mesh_grid")!;
    const road = presetColorFor("osm_roads")!;
    expect(si).not.toBe(cz);
    expect(si).not.toBe(river);
    expect(si).not.toBe(mesh);
    expect(si).not.toBe(road);
  });

  it("does not disturb existing preset colours (regression guard)", () => {
    expect(presetColorFor("capture_zone")).toBe("#9B59B6");
    expect(presetColorFor("wellhead_protection")).toBe("#9B59B6");
    expect(presetColorFor("osm_waterways")).toBe("#4477FF");
  });
});

// ---------------------------------------------------------------------------
// DATA-DRIVEN LEGEND - generic vector fill from a LegendKey.value_field
// ---------------------------------------------------------------------------

describe("legendHasValueField (data-driven legend)", () => {
  it("is true only when the legend carries a non-empty value_field", () => {
    expect(legendHasValueField({ kind: "categorical", value_field: "ds_mean" })).toBe(true);
    expect(legendHasValueField({ kind: "continuous", value_field: "depth" })).toBe(true);
  });

  it("is false for a legend with no value_field, or nullish input", () => {
    expect(legendHasValueField({ kind: "continuous", colormap: "reds" })).toBe(false);
    expect(legendHasValueField({ kind: "categorical", value_field: "" })).toBe(false);
    expect(legendHasValueField(null)).toBe(false);
    expect(legendHasValueField(undefined)).toBe(false);
  });
});

describe("buildLegendFillExpression - categorical (match-by-bin)", () => {
  // The Pelicun-shaped categorical legend the producer now emits: ds_mean drives a
  // damage-state choropleth via half-open numeric bins.
  const pelicunLegend: LegendKey = {
    kind: "categorical",
    value_field: "ds_mean",
    vmin: 0,
    vmax: 4,
    classes: [
      { value_min: 0, value_max: 1, color: "#2DC937", label: "None" },
      { value_min: 1, value_max: 2, color: "#E7B416", label: "Slight" },
      { value_min: 2, value_max: 3, color: "#DB7B2B", label: "Moderate" },
      { value_min: 3, value_max: 4.0001, color: "#CC3232", label: "Complete" },
    ],
  };

  it("builds a `case` over the classes keyed on the value_field", () => {
    const expr = buildLegendFillExpression(pelicunLegend);
    expect(Array.isArray(expr)).toBe(true);
    expect(expr![0]).toBe("case");
    const flat = JSON.stringify(expr);
    // Each class color is present, the property is read via ["get","ds_mean"].
    expect(flat).toContain("#2DC937");
    expect(flat).toContain("#CC3232");
    expect(flat).toContain("ds_mean");
    // A half-open bin uses >= and < on the property.
    expect(flat).toContain('[">=",["get","ds_mean"]');
    expect(flat).toContain('["<",["get","ds_mean"]');
  });

  it("ends with the neutral fallback (honesty floor: no class match => no-data)", () => {
    const expr = buildLegendFillExpression(pelicunLegend)!;
    expect(expr[expr.length - 1]).toBe(LEGEND_FILL_FALLBACK);
  });

  it("supports discrete-value categorical classes (NLCD-style codes)", () => {
    const nlcd: LegendKey = {
      kind: "categorical",
      value_field: "class_code",
      classes: [
        { value: 11, color: "#476BA0", label: "Open water" },
        { value: 41, color: "#68AA63", label: "Deciduous forest" },
      ],
    };
    const expr = buildLegendFillExpression(nlcd)!;
    const flat = JSON.stringify(expr);
    // Discrete classes use an == match on the property.
    expect(flat).toContain('["==",["get","class_code"],11]');
    expect(flat).toContain("#476BA0");
  });
});

describe("buildLegendFillExpression - continuous (graduated ramp)", () => {
  it("builds an interpolate over the value_field scaled to [vmin,vmax]", () => {
    const legend: LegendKey = {
      kind: "continuous",
      value_field: "depth",
      colormap: "blues",
      vmin: 0,
      vmax: 4,
      units: "meters",
    };
    const expr = buildLegendFillExpression(legend)!;
    // Wrapped in a `case` guarded by `has` so a feature missing the prop -> fallback.
    expect(expr[0]).toBe("case");
    const flat = JSON.stringify(expr);
    expect(flat).toContain('["has","depth"]');
    expect(flat).toContain("interpolate");
    expect(flat).toContain('["get","depth"]');
    // The fallback is the neutral no-data color.
    expect(expr[expr.length - 1]).toBe(LEGEND_FILL_FALLBACK);
  });

  it("uses explicit [stop,hex] colormap stops scaled onto the value range", () => {
    const legend: LegendKey = {
      kind: "continuous",
      value_field: "conc",
      colormap: [
        [0, "#ffffff"],
        [1, "#ff0000"],
      ],
      vmin: 10,
      vmax: 20,
    };
    const expr = buildLegendFillExpression(legend)!;
    const flat = JSON.stringify(expr);
    // stop 0 -> vmin (10), stop 1 -> vmax (20).
    expect(flat).toContain("10,\"#ffffff\"");
    expect(flat).toContain("20,\"#ff0000\"");
  });

  it("returns null when the legend has no value_field (rasters / flat layers)", () => {
    expect(
      buildLegendFillExpression({ kind: "continuous", colormap: "reds", vmin: 0, vmax: 1 }),
    ).toBeNull();
    expect(buildLegendFillExpression(null)).toBeNull();
    expect(buildLegendFillExpression(undefined)).toBeNull();
  });

  it("returns null when a continuous legend has no resolvable colormap", () => {
    expect(
      buildLegendFillExpression({ kind: "continuous", value_field: "x", vmin: 0, vmax: 1 }),
    ).toBeNull();
  });
});
