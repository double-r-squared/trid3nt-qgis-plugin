// GRACE-2 web - QgisExportButton (user-driven QGIS export, NATE 2026-07-06).
//
// A per-case "Export to QGIS" action that asks the agent's :8766 HTTP surface
// to run the export_case_to_qgis tool (one GeoPackage + local GeoTIFFs + a
// ready-to-open project.qgz), then downloads the .qgz through the
// path-guarded file endpoint. Lives in the CasesPanel kebab popover next to
// ExportButton and follows its exact popover-item contract (asMenuItem +
// itemStyle, spinner label -> success line -> inline error).
//
// Flow:
//   1. Render whenever a case row exists - LOCAL-FIRST, no config probe and
//      no serverless gate (unlike ExportButton's caseExportConfigured()). If
//      the agent is unreachable the POST fails and an inline honest error
//      shows; we never pre-hide the affordance.
//   2. On click: label flips to "Exporting to QGIS..." and we POST
//      {case_id} to `${httpBase()}/api/export-qgis`.
//   3. On success: label reads "QGIS project ready (N layers)", a browser
//      download of the .qgz is triggered via
//      GET /api/export-qgis/file?path=<qgz_path>, and a secondary line shows
//      output_dir for users who want the whole export folder.
//   4. On failure (typed 4xx or network error): inline honest error line.
//
// The button owns ONLY local UI state; the agent does all the work.

import { CSSProperties, useState } from "react";
import { httpBase } from "../lib/public_base";
import { IconDownload, IconRefresh } from "./icons";

export interface QgisExportButtonProps {
  /** The case to export. */
  caseId: string;
  /**
   * Render as an item INSIDE the per-row 3-dots (kebab) popover menu (the
   * only mount today). The popover supplies its ``itemStyle`` so the row
   * matches Rename/Export/Archive/Delete. Clicking runs the export in place
   * and does NOT close the menu, so the status stays visible.
   */
  asMenuItem?: boolean;
  /** Menu-item style from the popover (CasesPanel ``menuItemStyle()``). */
  itemStyle?: CSSProperties;
}

/** Successful export response from POST /api/export-qgis (the tool's result
 *  dict, trimmed to the fields the UI reads). */
interface QgisExportOutcome {
  qgzPath: string;
  outputDir: string;
  layerCount: number;
}

/** Failure outcome: an honest message from the endpoint or the transport. */
interface QgisExportError {
  error: string;
}

/**
 * Trigger a browser download of a url. Mirrors ExportButton's transient
 * anchor with the `download` attribute; falls back to navigating the window
 * when the anchor approach is unavailable (SSR / no document).
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

/** Parse the endpoint's success payload; null when unrecognisable. */
function parseOutcome(payload: unknown): QgisExportOutcome | null {
  if (payload === null || typeof payload !== "object") return null;
  const obj = payload as {
    qgz_path?: unknown;
    output_dir?: unknown;
    exported_vector_count?: unknown;
    exported_raster_count?: unknown;
  };
  if (typeof obj.qgz_path !== "string" || obj.qgz_path.trim() === "") return null;
  if (typeof obj.output_dir !== "string") return null;
  const vec =
    typeof obj.exported_vector_count === "number" ? obj.exported_vector_count : 0;
  const ras =
    typeof obj.exported_raster_count === "number" ? obj.exported_raster_count : 0;
  return {
    qgzPath: obj.qgz_path,
    outputDir: obj.output_dir,
    layerCount: vec + ras,
  };
}

export function QgisExportButton({
  caseId,
  asMenuItem = false,
  itemStyle,
}: QgisExportButtonProps): JSX.Element {
  const [busy, setBusy] = useState(false);
  // null = idle; success carries the result; failure carries the honest text.
  const [outcome, setOutcome] = useState<
    QgisExportOutcome | QgisExportError | null
  >(null);

  async function onExportClick(): Promise<void> {
    if (busy) return;
    setBusy(true);
    setOutcome(null);
    try {
      const base = httpBase();
      const resp = await fetch(`${base}/api/export-qgis`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ case_id: caseId }),
      });
      let payload: unknown = null;
      try {
        payload = await resp.json();
      } catch {
        payload = null;
      }
      if (!resp.ok) {
        const message =
          payload !== null &&
          typeof payload === "object" &&
          typeof (payload as { error?: unknown }).error === "string"
            ? ((payload as { error: string }).error)
            : `Export failed (HTTP ${resp.status})`;
        setOutcome({ error: message });
        return;
      }
      const result = parseOutcome(payload);
      if (!result) {
        setOutcome({ error: "Export returned an unrecognisable response" });
        return;
      }
      setOutcome(result);
      triggerDownload(
        `${base}/api/export-qgis/file?path=${encodeURIComponent(result.qgzPath)}`,
      );
    } catch {
      // Network error - the agent (:8766 / the edge) is unreachable.
      setOutcome({ error: "Agent unreachable - is the agent running?" });
    } finally {
      setBusy(false);
    }
  }

  const failed = outcome !== null && "error" in outcome;
  const label = busy
    ? "Exporting to QGIS..."
    : outcome && !failed
      ? `QGIS project ready (${(outcome as QgisExportOutcome).layerCount} layers)`
      : failed
        ? "QGIS export failed, try again"
        : "Export to QGIS";

  // Secondary line: output_dir on success (for users who want the folder,
  // not just the downloaded .qgz); the honest error text on failure.
  const statusLine =
    !busy && outcome !== null ? (
      <div
        data-testid="grace2-case-qgis-export-status"
        role="status"
        style={{
          fontSize: 10,
          lineHeight: 1.4,
          padding: asMenuItem ? "0 10px 6px" : undefined,
          maxWidth: 260,
          overflowWrap: "anywhere",
          color: failed ? "#e08a8a" : "#7fd18a",
        }}
      >
        {failed
          ? (outcome as QgisExportError).error
          : (outcome as QgisExportOutcome).outputDir}
      </div>
    ) : null;

  if (asMenuItem) {
    return (
      <>
        <button
          data-testid="grace2-case-qgis-export-menuitem"
          role="menuitem"
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            void onExportClick();
          }}
          disabled={busy}
          aria-label={busy ? "Exporting to QGIS" : "Export case to QGIS"}
          style={itemStyle}
        >
          {busy ? <IconRefresh size={14} /> : <IconDownload size={14} />}
          <span>{label}</span>
        </button>
        {statusLine}
      </>
    );
  }

  return (
    <span
      data-testid="grace2-case-qgis-export"
      style={{ display: "inline-flex", flexDirection: "column", gap: 2 }}
      onClick={(e) => e.stopPropagation()}
    >
      <button
        data-testid="grace2-case-qgis-export-button"
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          void onExportClick();
        }}
        disabled={busy}
        aria-label={busy ? "Exporting to QGIS" : "Export case to QGIS"}
        title={busy ? "Exporting to QGIS" : "Export case to QGIS"}
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
          fontFamily: "inherit",
        }}
      >
        {busy ? <IconRefresh size={14} /> : <IconDownload size={14} />}
        <span>{label}</span>
      </button>
      {statusLine}
    </span>
  );
}
