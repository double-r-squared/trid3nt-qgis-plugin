// GRACE-2 web — useIsMobile hook tests (job-0278, mobile-friendly UI).
//
// The hook is the single guard for every mobile style branch in the app, so
// these tests pin its full contract:
//   - breakpoint constant (<768px ⇔ mobile);
//   - SSR-safety (no matchMedia → false, no crash);
//   - initial read from MediaQueryList.matches;
//   - live updates on the `change` event (rotation / resize);
//   - legacy addListener/removeListener fallback;
//   - listener cleanup on unmount.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, act, cleanup } from "@testing-library/react";
import {
  MOBILE_BREAKPOINT_PX,
  MOBILE_MEDIA_QUERY,
  readIsMobile,
  useIsMobile,
} from "./useIsMobile";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** Probe component exposing the hook's value as a data attribute. */
function Probe(): JSX.Element {
  const isMobile = useIsMobile();
  return <span data-testid="probe" data-mobile={String(isMobile)} />;
}

interface FakeMql {
  matches: boolean;
  media: string;
  addEventListener?: (type: string, cb: (e: { matches: boolean }) => void) => void;
  removeEventListener?: (type: string, cb: (e: { matches: boolean }) => void) => void;
  addListener?: (cb: (e: { matches: boolean }) => void) => void;
  removeListener?: (cb: (e: { matches: boolean }) => void) => void;
}

/** Controllable matchMedia stub. `legacy: true` exposes only the
 * addListener/removeListener API (Safari < 14 shape). */
function makeMatchMedia(initialMatches: boolean, opts?: { legacy?: boolean }) {
  let listeners: Array<(e: { matches: boolean }) => void> = [];
  const mql: FakeMql = { matches: initialMatches, media: "" };
  const add = (cb: (e: { matches: boolean }) => void): void => {
    listeners.push(cb);
  };
  const remove = (cb: (e: { matches: boolean }) => void): void => {
    listeners = listeners.filter((l) => l !== cb);
  };
  if (opts?.legacy) {
    mql.addListener = add;
    mql.removeListener = remove;
  } else {
    mql.addEventListener = (_type, cb) => add(cb);
    mql.removeEventListener = (_type, cb) => remove(cb);
  }
  const matchMedia = vi.fn((query: string) => {
    mql.media = query;
    return mql as unknown as MediaQueryList;
  });
  return {
    matchMedia,
    mql,
    fire(matches: boolean): void {
      mql.matches = matches;
      // Copy — a listener removing itself mid-fire must not skip others.
      [...listeners].forEach((l) => l({ matches }));
    },
    listenerCount: (): number => listeners.length,
  };
}

describe("useIsMobile constants", () => {
  it("breakpoint is 768px and the query matches strictly below it", () => {
    expect(MOBILE_BREAKPOINT_PX).toBe(768);
    expect(MOBILE_MEDIA_QUERY).toBe("(max-width: 767px)");
  });
});

describe("readIsMobile", () => {
  it("is false when matchMedia is unavailable (SSR-safe)", () => {
    vi.stubGlobal("matchMedia", undefined);
    expect(readIsMobile()).toBe(false);
  });

  it("is false when matchMedia throws", () => {
    vi.stubGlobal("matchMedia", () => {
      throw new Error("boom");
    });
    expect(readIsMobile()).toBe(false);
  });

  it("reflects MediaQueryList.matches", () => {
    const fake = makeMatchMedia(true);
    vi.stubGlobal("matchMedia", fake.matchMedia);
    expect(readIsMobile()).toBe(true);
    expect(fake.matchMedia).toHaveBeenCalledWith(MOBILE_MEDIA_QUERY);
  });
});

describe("useIsMobile", () => {
  it("returns false without matchMedia (SSR-safe, no crash)", () => {
    vi.stubGlobal("matchMedia", undefined);
    render(<Probe />);
    expect(screen.getByTestId("probe")).toHaveAttribute(
      "data-mobile",
      "false",
    );
  });

  it("returns the initial match state (mobile)", () => {
    const fake = makeMatchMedia(true);
    vi.stubGlobal("matchMedia", fake.matchMedia);
    render(<Probe />);
    expect(screen.getByTestId("probe")).toHaveAttribute("data-mobile", "true");
  });

  it("returns the initial match state (desktop)", () => {
    const fake = makeMatchMedia(false);
    vi.stubGlobal("matchMedia", fake.matchMedia);
    render(<Probe />);
    expect(screen.getByTestId("probe")).toHaveAttribute(
      "data-mobile",
      "false",
    );
  });

  it("updates when the viewport crosses the breakpoint (change event)", () => {
    const fake = makeMatchMedia(false);
    vi.stubGlobal("matchMedia", fake.matchMedia);
    render(<Probe />);
    expect(screen.getByTestId("probe")).toHaveAttribute(
      "data-mobile",
      "false",
    );
    act(() => fake.fire(true));
    expect(screen.getByTestId("probe")).toHaveAttribute("data-mobile", "true");
    act(() => fake.fire(false));
    expect(screen.getByTestId("probe")).toHaveAttribute(
      "data-mobile",
      "false",
    );
  });

  it("falls back to the legacy addListener API and still updates", () => {
    const fake = makeMatchMedia(false, { legacy: true });
    vi.stubGlobal("matchMedia", fake.matchMedia);
    render(<Probe />);
    expect(fake.listenerCount()).toBe(1);
    act(() => fake.fire(true));
    expect(screen.getByTestId("probe")).toHaveAttribute("data-mobile", "true");
  });

  it("removes its listener on unmount (modern + legacy)", () => {
    for (const legacy of [false, true]) {
      const fake = makeMatchMedia(false, { legacy });
      vi.stubGlobal("matchMedia", fake.matchMedia);
      const { unmount } = render(<Probe />);
      expect(fake.listenerCount()).toBe(1);
      unmount();
      expect(fake.listenerCount()).toBe(0);
      cleanup();
    }
  });
});
