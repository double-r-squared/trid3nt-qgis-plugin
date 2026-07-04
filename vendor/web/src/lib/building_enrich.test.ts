// GRACE-2 web - building_enrich.ts unit tests (click-to-enrich, NATE 2026-06-27).

import { describe, it, expect, vi } from "vitest";
import { buildingDetailUrl, fetchBuildingDetail } from "./building_enrich";

function fakeResponse(ok: boolean, body: unknown): Response {
  return {
    ok,
    json: async () => body,
  } as unknown as Response;
}

describe("buildingDetailUrl", () => {
  it("builds an /api/building-detail URL with encoded osm_type + osm_id", () => {
    const url = buildingDetailUrl("way", 123456);
    expect(url).toContain("/api/building-detail");
    expect(url).toContain("osm_type=way");
    expect(url).toContain("osm_id=123456");
  });

  it("URL-encodes the identifiers", () => {
    const url = buildingDetailUrl("re lation", "a b");
    expect(url).toContain("osm_type=re%20lation");
    expect(url).toContain("osm_id=a%20b");
  });
});

describe("fetchBuildingDetail", () => {
  it("returns the tags object on a 200 {fid, tags} response", async () => {
    const tags = { building: "house", name: "Maison", height: "8" };
    const fetchImpl = vi.fn(async () =>
      fakeResponse(true, { fid: "w123", tags }),
    );
    const out = await fetchBuildingDetail("way", 123, fetchImpl as unknown as typeof fetch);
    expect(out).toEqual(tags);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("returns null (never throws) when the fetch rejects", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new Error("network down");
    });
    const out = await fetchBuildingDetail("way", 123, fetchImpl as unknown as typeof fetch);
    expect(out).toBeNull();
  });

  it("returns null on a non-200 response", async () => {
    const fetchImpl = vi.fn(async () => fakeResponse(false, { error: "not found" }));
    const out = await fetchBuildingDetail("relation", 9, fetchImpl as unknown as typeof fetch);
    expect(out).toBeNull();
  });

  it("returns null when the body has no tags object", async () => {
    const fetchImpl = vi.fn(async () => fakeResponse(true, { fid: "w1" }));
    const out = await fetchBuildingDetail("way", 1, fetchImpl as unknown as typeof fetch);
    expect(out).toBeNull();
  });

  it("returns null when tags is an array (malformed)", async () => {
    const fetchImpl = vi.fn(async () => fakeResponse(true, { tags: ["x"] }));
    const out = await fetchBuildingDetail("way", 1, fetchImpl as unknown as typeof fetch);
    expect(out).toBeNull();
  });

  it("short-circuits to null without fetching when ids are missing", async () => {
    const fetchImpl = vi.fn(async () => fakeResponse(true, { tags: {} }));
    expect(await fetchBuildingDetail(null, 1, fetchImpl as unknown as typeof fetch)).toBeNull();
    expect(await fetchBuildingDetail("way", null, fetchImpl as unknown as typeof fetch)).toBeNull();
    expect(await fetchBuildingDetail("", "", fetchImpl as unknown as typeof fetch)).toBeNull();
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});
