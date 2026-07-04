// GRACE-2 web - ErrorBoundary (white-screen-of-death guard).
//
// THE BUG (live, in-case): a user hit an uncaught render throw deep in the app.
// Nothing in the tree caught it, so React unmounted the WHOLE root and the page
// went WHITE - the worst possible failure (the user sees a blank screen with no
// way back). main.tsx renders <EntryRouter/> -> <App/> with no boundary above
// them, so any throw in a render/lifecycle blanked everything.
//
// This class component is the single render-throw catch. React only supports
// error boundaries as class components (getDerivedStateFromError +
// componentDidCatch), so this stays a class on purpose. On a caught throw it:
//   1. flips to an error state and renders a DARK fallback that matches the app
//      theme (centered "Something went wrong" + a Reload button on a dark bg) -
//      NEVER a white screen, the whole point of the fix; and
//   2. console.errors the error + component stack so the throw is still
//      diagnosable in the live console / Sentry-style log scrapers.
//
// Wrap <App/> (and ideally <EntryRouter/>) in main.tsx. Purely presentational on
// the happy path: when there is no error it renders `children` verbatim, so it
// adds zero behavior to a healthy tree.

import { Component, type ErrorInfo, type ReactNode } from "react";

export interface ErrorBoundaryProps {
  children: ReactNode;
  /**
   * Test seam: invoked with the caught error + info so a test can assert the
   * boundary fired without scraping console output. Optional; production does
   * not wire it (the console.error is the production signal).
   */
  onError?: (error: Error, info: ErrorInfo) => void;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  override state: ErrorBoundaryState = { hasError: false, error: null };

  // React calls this on a child render throw to derive the next state. Keep it
  // pure (no side effects) - the logging lives in componentDidCatch.
  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface the throw + component stack so it is diagnosable in the live
    // console even though the UI degraded to the dark fallback.
    try {
      // eslint-disable-next-line no-console
      console.error(
        "[ErrorBoundary] uncaught render error:",
        error,
        info?.componentStack,
      );
    } catch {
      /* console unavailable - swallow so the fallback still renders */
    }
    this.props.onError?.(error, info);
  }

  private handleReload = (): void => {
    try {
      window.location.reload();
    } catch {
      /* no window (test/SSR) - the button is a no-op there */
    }
  };

  override render(): ReactNode {
    if (!this.state.hasError) return this.props.children;

    // DARK fallback (NEVER white). Matches the app's dark chrome family
    // (rgba(11,16,24)/#0b1018 background used by the route fallback + panels).
    return (
      <div
        data-testid="grace2-error-boundary-fallback"
        role="alert"
        style={{
          position: "fixed",
          inset: 0,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 16,
          padding: 24,
          background: "#0b1018",
          color: "#e8e8ec",
          fontFamily:
            "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, Roboto, sans-serif",
          textAlign: "center",
          zIndex: 999999,
        }}
      >
        <div
          style={{
            fontSize: 18,
            fontWeight: 600,
            letterSpacing: "0.01em",
          }}
        >
          Something went wrong
        </div>
        <div
          style={{
            fontSize: 13,
            color: "#9aa1ab",
            maxWidth: 360,
            lineHeight: 1.5,
          }}
        >
          The app hit an unexpected error. Reloading usually fixes it - your
          saved cases are safe.
        </div>
        <button
          type="button"
          data-testid="grace2-error-boundary-reload"
          onClick={this.handleReload}
          style={{
            marginTop: 4,
            padding: "9px 18px",
            background: "rgba(74,163,255,0.16)",
            border: "1px solid rgba(74,163,255,0.45)",
            borderRadius: 10,
            color: "#cfe4ff",
            fontSize: 13,
            fontWeight: 600,
            cursor: "pointer",
          }}
        >
          Reload
        </button>
      </div>
    );
  }
}
