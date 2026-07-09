// GRACE-2 web - deployment-mode seam tests (local-cloud fingerprint fixes,
// reports/reviews/local-cloud-fingerprints-2026-07-08.md).
//
// The HARD RULE under test: the CLOUD build is the default - only the exact
// value "local" (trimmed, case-insensitive) selects local mode. Unset, empty,
// and any other value (including typos) MUST read as cloud so the Vercel
// build, which sets nothing, is byte-identical in behavior.
//
// deployment.ts reads import.meta.env at CALL time, so vi.stubEnv works
// without vi.resetModules; resetModules is kept for hygiene anyway.

import { describe, it, expect, afterEach, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("deploymentMode / isLocalDeployment", () => {
  it("defaults to CLOUD when VITE_DEPLOYMENT is unset (the Vercel build)", async () => {
    const { deploymentMode, isLocalDeployment } = await import("./deployment");
    expect(deploymentMode()).toBe("cloud");
    expect(isLocalDeployment()).toBe(false);
  });

  it("VITE_DEPLOYMENT=cloud is explicitly cloud", async () => {
    vi.stubEnv("VITE_DEPLOYMENT", "cloud");
    const { deploymentMode, isLocalDeployment } = await import("./deployment");
    expect(deploymentMode()).toBe("cloud");
    expect(isLocalDeployment()).toBe(false);
  });

  it("VITE_DEPLOYMENT=local selects local", async () => {
    vi.stubEnv("VITE_DEPLOYMENT", "local");
    const { deploymentMode, isLocalDeployment } = await import("./deployment");
    expect(deploymentMode()).toBe("local");
    expect(isLocalDeployment()).toBe(true);
  });

  it("is tolerant of case + whitespace for the local value only", async () => {
    vi.stubEnv("VITE_DEPLOYMENT", "  LOCAL ");
    const { deploymentMode } = await import("./deployment");
    expect(deploymentMode()).toBe("local");
  });

  it("empty / whitespace / unknown values fall back to CLOUD (never local)", async () => {
    for (const bad of ["", "   ", "locall", "offline", "LOCAL_BUILD", "1", "true"]) {
      vi.resetModules();
      vi.stubEnv("VITE_DEPLOYMENT", bad);
      const { deploymentMode, isLocalDeployment } = await import("./deployment");
      expect(deploymentMode(), `value ${JSON.stringify(bad)}`).toBe("cloud");
      expect(isLocalDeployment(), `value ${JSON.stringify(bad)}`).toBe(false);
      vi.unstubAllEnvs();
    }
  });
});
