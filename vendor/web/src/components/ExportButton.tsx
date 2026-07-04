// GRACE-2 web - ExportButton (data export, NATE 2026-06-19).
//
// A per-case "Export" action that packages a case's data bundle (its rendered
// layers, archived server-side) into a single downloadable file. Lives in the
// CasesPanel per-row action cluster alongside rename / archive / delete.
//
// Flow (mirrors the SettingsPopup "Put agent to sleep" control - a signed-in,
// endpoint-gated, getIdToken-bearing serverless call with inline status):
//   1. Gate visibility on caseExportConfigured() AND being signed in
//      (the export endpoint requires a Cognito ID token). When either is false
//      the button renders nothing - dev/LAN + signed-out behave as before.
//   2. On click: set a SPINNER + "Preparing export" label and call
//      requestCaseExport(caseId, fetch, await getIdToken()).
//   3. On success: surface the human-readable archive size, then trigger a
//      browser download of the pre-signed S3 url (an anchor with `download`).
//   4. On null: surface a brief inline "Export failed, try again".
//
// The button owns ONLY local UI state (in-flight flag + the last result/error
// line). All AWS work happens behind requestCaseExport (lib/export.ts), which
// NEVER throws.

import { CSSProperties, useState } from "react";
import { getIdToken } from "../auth";
import { useAuth } from "../hooks/useAuth";
import {
  caseExportConfigured,
  requestCaseExport,
  type CaseExportResult,
} from "../lib/export";
import { IconDownload, IconRefresh } from "./icons";

export interface ExportButtonProps {
  /** The case to export. */
  caseId: string;
  /**
   * NATE 2026-06-19: render as an item INSIDE the per-row 3-dots (kebab)
   * popover menu instead of a standalone row icon. The popover supplies its
   * ``itemStyle`` so the row matches Rename/Archive/Delete. Clicking runs the
   * export in place (the label flips to "Preparing export" -> "Exported
   * <size>") and does NOT close the menu, so the status stays visible.
   */
  asMenuItem?: boolean;
  /** Menu-item style from the popover (CasesPanel ``menuItemStyle()``). */
  itemStyle?: CSSProperties;
}

/**
 * Human-readable byte size. Pure display formatting (e.g. 12_400_000 ->
 * "12.4 MB"). Binary-free decimal units (KB/MB/GB) so the number matches the
 * mental model a user has of a download.
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1000) return `${Math.round(bytes)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1000;
  let unitIdx = 0;
  while (value >= 1000 && unitIdx < units.length - 1) {
    value /= 1000;
    unitIdx += 1;
  }
  return `${value.toFixed(1)} ${units[unitIdx]}`;
}

/**
 * Trigger a browser download of a (pre-signed) url. Uses a transient anchor
 * with the `download` attribute; falls back to navigating the window when the
 * anchor approach is unavailable (SSR / no document).
 */
function triggerDownload(url: string): void {
  try {
    if (typeof document !== "undefined" && document.body) {
      const a = document.createElement("a");
      a.href = url;
      a.setAttribute("download", "");
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      return;
    }
  } catch {
    // fall through to the location-based path below.
  }
  if (typeof window !== "undefined") {
    window.location.assign(url);
  }
}

export function ExportButton({
  caseId,
  asMenuItem = false,
  itemStyle,
}: ExportButtonProps): JSX.Element | null {
  const { user } = useAuth();
  const isSignedIn = !!user && !user.isAnonymous;

  const [busy, setBusy] = useState(false);
  // null = idle; an object = last successful export (size shown); "error" = failed.
  const [outcome, setOutcome] = useState<CaseExportResult | "error" | null>(null);

  // Mirror SettingsPopup showSleepSection = isSignedIn && wakeConfigured().
  // The export endpoint needs a signed-in identity AND must be configured.
  const showExport = isSignedIn && caseExportConfigured();
  if (!showExport) return null;

  async function onExportClick(): Promise<void> {
    if (busy) return;
    setBusy(true);
    setOutcome(null);
    try {
      const token = await getIdToken();
      const result = await requestCaseExport(caseId, fetch, token);
      if (result) {
        setOutcome(result);
        triggerDownload(result.url);
      } else {
        setOutcome("error");
      }
    } finally {
      setBusy(false);
    }
  }

  // NATE 2026-06-19: in-menu variant — a single role=menuitem row that matches
  // Rename/Archive/Delete. Runs the export in place; the label reflects state.
  if (asMenuItem) {
    const label = busy
      ? "Preparing export"
      : outcome && outcome !== "error"
        ? `Exported ${formatBytes(outcome.size_bytes)}`
        : outcome === "error"
          ? "Export failed, try again"
          : "Export";
    return (
      <button
        data-testid="grace2-case-export-menuitem"
        role="menuitem"
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          void onExportClick();
        }}
        disabled={busy}
        aria-label={busy ? "Preparing export" : "Export case data"}
        style={itemStyle}
      >
        {busy ? <IconRefresh size={14} /> : <IconDownload size={14} />}
        <span>{label}</span>
      </button>
    );
  }

  return (
    <span
      data-testid="grace2-case-export"
      style={{ display: "inline-flex", flexDirection: "column", gap: 2 }}
      onClick={(e) => e.stopPropagation()}
    >
      <button
        data-testid="grace2-case-export-button"
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          void onExportClick();
        }}
        disabled={busy}
        aria-label={busy ? "Preparing export" : `Export case data`}
        title={busy ? "Preparing export" : "Export case data"}
        style={{
          background: "transparent",
          border: "none",
          color: "#aaa",
          cursor: busy ? "default" : "pointer",
          opacity: busy ? 0.6 : 1,
          fontSize: 12,
          padding: 2,
          height: 22,
          flexShrink: 0,
          borderRadius: 4,
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
          // Buttons need explicit fontFamily so they don't fall back to UA serif.
          fontFamily: "inherit",
        }}
      >
        {busy ? <IconRefresh size={14} /> : <IconDownload size={14} />}
        {busy && <span>Preparing export</span>}
      </button>
      {!busy && outcome != null && (
        <span
          data-testid="grace2-case-export-status"
          role="status"
          style={{
            fontSize: 10,
            lineHeight: 1.4,
            whiteSpace: "nowrap",
            color: outcome === "error" ? "#e08a8a" : "#7fd18a",
          }}
        >
          {outcome === "error"
            ? "Export failed, try again"
            : `Exported ${formatBytes(outcome.size_bytes)}`}
        </span>
      )}
    </span>
  );
}
