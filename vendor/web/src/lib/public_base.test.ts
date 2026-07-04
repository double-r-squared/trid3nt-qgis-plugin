// GRACE-2 web — public_base.ts URL-derivation tests (sprint-14-aws CloudFront).
//
// Verifies the env-gated single-origin seam:
//   - UNSET (today): defaultWsUrl() + httpBase() + catalogUrl() are
//     byte-identical to the pre-existing inline logic (ws://<host>:8765,
//     <proto>//<host>:8766, .../api/tool-catalog). happy-dom serves the page
//     from http://localhost so the derived values key on "localhost".
//   - VITE_GRACE2_PUBLIC_BASE set -> wss://<domain>/ws + https://<domain>.
//   - normalizePublicBase: bare domain assumes https, trailing slashes
//     stripped, empty/whitespace -> null.
//   - Per-surface overrides (VITE_GRACE2_WS_URL / VITE_GRACE2_HTTP_URL) still
//     win over the public base (precedence preserved).
//
// The env vars are read inside the helpers (not at module-eval), but we still
// use vi.resetModules + dynamic import for hygiene so each case re-evaluates
// against freshly-stubbed env.

import { describe, it, expect, afterEach, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("normalizePublicBase", () => {
  it("returns null for null / undefined / empty / whitespace", async () => {
    const { normalizePublicBase } = await import("./public_base");
    expect(normalizePublicBase(null)).toBeNull();
    expect(normalizePublicBase(undefined)).toBeNull();
    expect(normalizePublicBase("")).toBeNull();
    expect(normalizePublicBase("   ")).toBeNull();
  });

  it("assumes https for a bare domain and strips trailing slashes", async () => {
    const { normalizePublicBase } = await import("./public_base");
    expect(normalizePublicBase("d123.cloudfront.net")).toBe("https://d123.cloudfront.net");
    expect(normalizePublicBase("https://d123.cloudfront.net/")).toBe(
      "https://d123.cloudfront.net",
    );
    expect(normalizePublicBase("https://d123.cloudfront.net///")).toBe(
      "https://d123.cloudfront.net",
    );
  });

  it("preserves an explicit http scheme (does not force https)", async () => {
    const { normalizePublicBase } = await import("./public_base");
    expect(normalizePublicBase("http://10.0.0.5:9000/")).toBe("http://10.0.0.5:9000");
  });
});

describe("defaultWsUrl / httpBase / catalogUrl — UNSET (byte-identical to today)", () => {
  it("derives ws://<host>:8765 + <proto>//<host>:8766 from window.location", async () => {
    // No env stubbed. happy-dom default location is http://localhost/.
    const { defaultWsUrl, httpBase, catalogUrl } = await import("./public_base");
    expect(defaultWsUrl()).toBe("ws://localhost:8765");
    expect(httpBase()).toBe("http://localhost:8766");
    expect(catalogUrl()).toBe("http://localhost:8766/api/tool-catalog");
  });

  it("publicTileBase() is null when the seam is unset", async () => {
    const { publicTileBase } = await import("./public_base");
    expect(publicTileBase()).toBeNull();
  });
});

describe("defaultWsUrl / httpBase — VITE_GRACE2_PUBLIC_BASE set (HTTPS/WSS edge)", () => {
  it("derives wss://<domain>/ws for the agent socket", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_PUBLIC_BASE", "https://d123.cloudfront.net");
    const { defaultWsUrl } = await import("./public_base");
    expect(defaultWsUrl()).toBe("wss://d123.cloudfront.net/ws");
  });

  it("derives https://<domain> for the HTTP base + catalog path", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_PUBLIC_BASE", "https://d123.cloudfront.net");
    const { httpBase, catalogUrl, publicTileBase } = await import("./public_base");
    expect(httpBase()).toBe("https://d123.cloudfront.net");
    expect(catalogUrl()).toBe("https://d123.cloudfront.net/api/tool-catalog");
    expect(publicTileBase()).toBe("https://d123.cloudfront.net");
  });

  it("accepts a bare domain (assumes https -> wss)", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_PUBLIC_BASE", "d123.cloudfront.net");
    const { defaultWsUrl, httpBase } = await import("./public_base");
    expect(defaultWsUrl()).toBe("wss://d123.cloudfront.net/ws");
    expect(httpBase()).toBe("https://d123.cloudfront.net");
  });

  it("tolerates a trailing slash on the base", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_PUBLIC_BASE", "https://d123.cloudfront.net/");
    const { defaultWsUrl, catalogUrl } = await import("./public_base");
    expect(defaultWsUrl()).toBe("wss://d123.cloudfront.net/ws");
    expect(catalogUrl()).toBe("https://d123.cloudfront.net/api/tool-catalog");
  });
});

describe("precedence — explicit per-surface overrides win over public base", () => {
  it("VITE_GRACE2_WS_URL beats VITE_GRACE2_PUBLIC_BASE for the socket", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_PUBLIC_BASE", "https://d123.cloudfront.net");
    vi.stubEnv("VITE_GRACE2_WS_URL", "wss://explicit.example/socket");
    const { defaultWsUrl } = await import("./public_base");
    expect(defaultWsUrl()).toBe("wss://explicit.example/socket");
  });

  it("VITE_GRACE2_HTTP_URL beats VITE_GRACE2_PUBLIC_BASE for the http base", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_PUBLIC_BASE", "https://d123.cloudfront.net");
    vi.stubEnv("VITE_GRACE2_HTTP_URL", "https://explicit.example/");
    const { httpBase, catalogUrl } = await import("./public_base");
    expect(httpBase()).toBe("https://explicit.example");
    expect(catalogUrl()).toBe("https://explicit.example/api/tool-catalog");
  });

  it("blank-string overrides are ignored (fall through to public base)", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_GRACE2_PUBLIC_BASE", "https://d123.cloudfront.net");
    vi.stubEnv("VITE_GRACE2_WS_URL", "   ");
    vi.stubEnv("VITE_GRACE2_HTTP_URL", "");
    const { defaultWsUrl, httpBase } = await import("./public_base");
    expect(defaultWsUrl()).toBe("wss://d123.cloudfront.net/ws");
    expect(httpBase()).toBe("https://d123.cloudfront.net");
  });
});
