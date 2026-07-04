// GRACE-2 web — EntryRouter path-switch + session-passthrough tests
// (job-0285).
//
// Covers the load-bearing routing rule documented in EntryRouter.tsx:
//
//   "/"        → landing ONLY when no GRACE-2 session key exists;
//                app (passthrough) when ANY session key exists — this is
//                what keeps every Playwright live-verify tool (they all
//                seed `grace2_anonymous_accepted`) and returning users on
//                the app.
//   "/app"     → app, always.
//   "/privacy" → privacy policy, always.
//   "/landing" → landing, always (explicit preview).
//   unknown    → app (legacy deep-link behavior).
//
// App is mocked to a sentinel so these tests don't pull in
// WebSocket/MapLibre/Firebase real I/O (same pattern as App-adjacent
// suites). It is lazy-loaded in production code, hence the async findBy*
// assertions.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import {
  EntryRouter,
  GRACE2_SESSION_KEYS,
  hasExistingSession,
  resolveRoute,
} from "./EntryRouter";

vi.mock("./App", () => ({
  App: () => <div data-testid="app-sentinel">app</div>,
}));

function setPath(path: string): void {
  window.history.pushState({}, "", path);
}

beforeEach(() => {
  localStorage.clear();
  setPath("/");
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  setPath("/");
});

// --- resolveRoute (pure) ------------------------------------------------- //

describe("resolveRoute — path → route table", () => {
  it("routes '/' to landing when no session exists", () => {
    expect(resolveRoute("/", false)).toBe("landing");
  });

  it("routes '/' to app when a session exists (THE passthrough rule)", () => {
    expect(resolveRoute("/", true)).toBe("app");
  });

  it("routes '/app' to app regardless of session", () => {
    expect(resolveRoute("/app", false)).toBe("app");
    expect(resolveRoute("/app", true)).toBe("app");
  });

  it("routes '/app/<anything>' subpaths to app", () => {
    expect(resolveRoute("/app/cases/123", false)).toBe("app");
  });

  it("routes '/privacy' to privacy regardless of session", () => {
    expect(resolveRoute("/privacy", false)).toBe("privacy");
    expect(resolveRoute("/privacy", true)).toBe("privacy");
  });

  it("routes '/landing' to landing even with a session (explicit preview)", () => {
    expect(resolveRoute("/landing", true)).toBe("landing");
    expect(resolveRoute("/landing", false)).toBe("landing");
  });

  it("normalizes trailing slashes", () => {
    expect(resolveRoute("/privacy/", false)).toBe("privacy");
    expect(resolveRoute("/app/", false)).toBe("app");
    expect(resolveRoute("//", false)).toBe("landing");
  });

  it("routes unknown paths to app (legacy deep-link behavior)", () => {
    expect(resolveRoute("/cases/abc", false)).toBe("app");
    expect(resolveRoute("/anything/else", true)).toBe("app");
  });

  it("does NOT treat '/application' as an '/app' subpath", () => {
    // Falls into the unknown-path → app branch anyway, but must not match
    // the "/app" prefix test (guards against startsWith("/app") bugs).
    expect(resolveRoute("/application", false)).toBe("app");
  });
});

// --- hasExistingSession --------------------------------------------------- //

describe("hasExistingSession — session-key detection", () => {
  it("is false with empty localStorage", () => {
    expect(hasExistingSession()).toBe(false);
  });

  it.each(GRACE2_SESSION_KEYS.map((k) => [k]))(
    "is true when %s is present",
    (key) => {
      localStorage.setItem(key, "x");
      expect(hasExistingSession()).toBe(true);
    },
  );

  it("covers exactly the ws.ts + AuthGate key names", () => {
    // Contract pin: if ws.ts or AuthGate rename their keys, this must be
    // updated in lockstep or returning users will see the landing page.
    expect([...GRACE2_SESSION_KEYS]).toEqual([
      "grace2.session_id",
      "grace2.anonymous_user_id",
      "grace2_anonymous_accepted",
    ]);
  });

  it("treats a throwing storage as no-session (fresh visitor)", () => {
    const throwing = {
      getItem(): string | null {
        throw new Error("denied");
      },
    };
    expect(hasExistingSession(throwing)).toBe(false);
  });
});

// --- EntryRouter (rendered) ------------------------------------------------ //

describe("EntryRouter — rendered routes", () => {
  it("renders the landing page at '/' for a fresh visitor", () => {
    setPath("/");
    render(<EntryRouter />);
    expect(screen.getByTestId("grace2-landing")).toBeInTheDocument();
    expect(screen.queryByTestId("app-sentinel")).not.toBeInTheDocument();
  });

  it("passes '/' through to the app when grace2_anonymous_accepted is set (Playwright-tooling seam)", async () => {
    localStorage.setItem("grace2_anonymous_accepted", "true");
    setPath("/");
    render(<EntryRouter />);
    expect(await screen.findByTestId("app-sentinel")).toBeInTheDocument();
    expect(screen.queryByTestId("grace2-landing")).not.toBeInTheDocument();
  });

  it("passes '/' through to the app when grace2.session_id is set (returning user)", async () => {
    localStorage.setItem("grace2.session_id", "01JXAMPLEULID");
    setPath("/");
    render(<EntryRouter />);
    expect(await screen.findByTestId("app-sentinel")).toBeInTheDocument();
  });

  it("renders the app at '/app' even for a fresh visitor", async () => {
    setPath("/app");
    render(<EntryRouter />);
    expect(await screen.findByTestId("app-sentinel")).toBeInTheDocument();
    expect(screen.queryByTestId("grace2-landing")).not.toBeInTheDocument();
  });

  it("renders the privacy policy at '/privacy'", async () => {
    setPath("/privacy");
    render(<EntryRouter />);
    expect(await screen.findByTestId("grace2-privacy")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: /privacy policy/i }),
    ).toBeInTheDocument();
  });

  it("renders the landing at '/landing' even with a session (Resume variant)", () => {
    localStorage.setItem("grace2.session_id", "01JXAMPLEULID");
    setPath("/landing");
    render(<EntryRouter />);
    expect(screen.getByTestId("grace2-landing")).toBeInTheDocument();
    expect(screen.getByTestId("grace2-landing-cta")).toHaveTextContent(
      /resume session/i,
    );
  });
});
