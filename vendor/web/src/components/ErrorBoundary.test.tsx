// GRACE-2 web - ErrorBoundary tests (white-screen-of-death guard).
//
// The boundary must turn an uncaught child render throw into a DARK fallback
// (centered "Something went wrong" + Reload on a dark bg), NEVER a blank/white
// screen, and it must log the error. The happy path renders children verbatim.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { ErrorBoundary } from "./ErrorBoundary";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// A child that throws on render to trip the boundary.
function Boom(): JSX.Element {
  throw new Error("kaboom-from-child");
}

describe("ErrorBoundary", () => {
  it("renders children unchanged on the happy path (no error)", () => {
    render(
      <ErrorBoundary>
        <div data-testid="healthy-child">all good</div>
      </ErrorBoundary>,
    );
    expect(screen.getByTestId("healthy-child")).toHaveTextContent("all good");
    // No fallback when nothing threw.
    expect(
      screen.queryByTestId("grace2-error-boundary-fallback"),
    ).toBeNull();
  });

  it("catches a child render throw and shows the DARK fallback (not a blank/white screen)", () => {
    // Silence the expected React error log noise for this throw.
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );

    // The fallback IS rendered (the tree did NOT blank to nothing).
    const fallback = screen.getByTestId("grace2-error-boundary-fallback");
    expect(fallback).toBeInTheDocument();
    expect(fallback).toHaveTextContent("Something went wrong");

    // DARK background, never white - the whole point of the guard.
    const bg = fallback.style.background;
    expect(bg).toBe("#0b1018");
    expect(bg.toLowerCase()).not.toContain("#fff");
    expect(bg.toLowerCase()).not.toContain("white");

    // The throwing child is gone (its content is not in the document).
    expect(screen.queryByText("kaboom-from-child")).toBeNull();
  });

  it("console.errors the caught error + stack", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    // The boundary's own log call carries the prefix + the Error.
    const called = spy.mock.calls.some(
      (args) =>
        typeof args[0] === "string" &&
        args[0].includes("[ErrorBoundary]") &&
        args.some((a) => a instanceof Error && /kaboom/.test(a.message)),
    );
    expect(called).toBe(true);
  });

  it("invokes the onError test seam with the error", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const onError = vi.fn();
    render(
      <ErrorBoundary onError={onError}>
        <Boom />
      </ErrorBoundary>,
    );
    expect(onError).toHaveBeenCalledTimes(1);
    const firstArg = onError.mock.calls[0]?.[0];
    expect(firstArg).toBeInstanceOf(Error);
    expect((firstArg as Error).message).toBe("kaboom-from-child");
  });

  it("the Reload button triggers window.location.reload", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const reload = vi.fn();
    // jsdom's location.reload is non-configurable on some versions; redefine.
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, reload },
    });
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    fireEvent.click(screen.getByTestId("grace2-error-boundary-reload"));
    expect(reload).toHaveBeenCalledTimes(1);
  });
});
