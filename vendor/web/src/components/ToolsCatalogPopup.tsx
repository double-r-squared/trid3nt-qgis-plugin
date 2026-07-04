// GRACE-2 web — ToolsCatalogPopup (Wave 4.10 Stage 3 — job C1).
//
// Full-screen overlay that browses the agent's atomic-tool catalog. Backed
// by ``GET /api/tool-catalog`` on the agent service's HTTP listener
// (default port 8766; override via VITE_GRACE2_HTTP_URL).
//
// Surface:
//
//   +------------------------------------------------------------+
//   | Tools catalog                                            ✕ |
//   |                                                            |
//   | [search box.............]                                  |
//   |                                                            |
//   | [Hazard modeling 1] [Weather 11] [Hydrology 5] ...         |
//   |                                                            |
//   | ─── tool list ───                                          |
//   | • fetch_dem                                                |
//   |   first 200 chars of docstring...                          |
//   |   [terrain_elevation] [open-world] [idempotent]            |
//   |   sample: "show me elevation data for the Grand Canyon"    |
//   | ...                                                        |
//   +------------------------------------------------------------+
//
// Match SettingsPopup/SecretsPopup chrome: 480-pixel-wide card, dark
// surface, X close, Esc / backdrop dismiss. Width is wider here (760px)
// because the tool list benefits from horizontal room.

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  catalogUrl as catalogUrlFromBase,
  coldCatalogUrl,
} from "../lib/public_base";
import { IconClose, IconGlobe } from "./icons";

// ---------------------------------------------------------------------------
// Wire types (mirror the agent's /api/tool-catalog response shape).
// ---------------------------------------------------------------------------

export interface ToolCatalogCategory {
  id: string;
  name: string;
  description: string;
  tool_count: number;
}

export interface ToolAnnotations {
  read_only_hint: boolean;
  open_world_hint: boolean;
  destructive_hint: boolean;
  idempotent_hint: boolean;
}

export interface ToolCatalogTool {
  name: string;
  description: string;
  description_full: string;
  category_id: string;
  secondary_category_ids: string[];
  supports_global_query: boolean;
  annotations: ToolAnnotations;
  estimate_payload_mb_default: number | null;
  ttl_class: string;
  source_class: string | null;
  cacheable: boolean;
  sample_queries: string[];
}

export interface ToolCatalogPayload {
  categories: ToolCatalogCategory[];
  tools: ToolCatalogTool[];
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ToolsCatalogPopupProps {
  /** Dismiss handler. */
  onClose: () => void;
  /**
   * Optional pre-fetched catalog (tests inject this to bypass network). When
   * unset, the component fetches /api/tool-catalog on mount.
   */
  initialCatalog?: ToolCatalogPayload | null;
  /** Optional fetch URL override. Tests pass a stubbed URL. */
  catalogUrl?: string;
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.55)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 9_500,
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
};

const cardStyle: React.CSSProperties = {
  background: "rgba(20,22,30,0.98)",
  // job-0283 — hairline border joins the modal family (was solid #444).
  border: "1px solid rgba(255,255,255,0.10)",
  borderRadius: 12,
  width: "min(820px, 96vw)",
  maxHeight: "90vh",
  display: "flex",
  flexDirection: "column",
  color: "#e8eaf0",
  boxShadow: "0 24px 64px rgba(0,0,0,0.55)",
  position: "relative",
  padding: "20px 22px 18px",
};

const headerStyle: React.CSSProperties = {
  fontSize: 20,
  fontWeight: 600,
  margin: "0 0 12px",
  color: "#e8eaf0",
  display: "flex",
  alignItems: "baseline",
  gap: 8,
};

const subtitleStyle: React.CSSProperties = {
  fontSize: 12,
  color: "#9aa0ad",
  fontWeight: 400,
};

const closeBtnStyle: React.CSSProperties = {
  position: "absolute",
  top: 12,
  right: 12,
  background: "transparent",
  border: "none",
  color: "#aaa",
  fontSize: 18,
  cursor: "pointer",
  width: 28,
  height: 28,
  borderRadius: 8,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
};

const searchInputStyle: React.CSSProperties = {
  width: "100%",
  background: "rgba(15,15,20,0.85)",
  // job-0283 — hairline border + 8px radius (modal-family form controls).
  border: "1px solid rgba(255,255,255,0.12)",
  borderRadius: 8,
  color: "#e8eaf0",
  padding: "8px 12px",
  fontSize: 13,
  fontFamily: "inherit",
  boxSizing: "border-box",
  outline: "none",
  marginBottom: 12,
};

const categoryGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))",
  gap: 6,
  marginBottom: 14,
};

const categoryChipBase: React.CSSProperties = {
  background: "rgba(30,32,42,0.9)",
  borderWidth: 1,
  borderStyle: "solid",
  // job-0283 — hairline (was #3a3d49); the active #3b82f6 state is unchanged.
  borderColor: "rgba(255,255,255,0.10)",
  borderRadius: 8,
  padding: "8px 10px",
  cursor: "pointer",
  fontSize: 11,
  textAlign: "left",
  color: "#cfd3dc",
  fontFamily: "inherit",
  display: "flex",
  flexDirection: "column",
  gap: 2,
  transition: "background 80ms ease",
};

const categoryChipActive: React.CSSProperties = {
  ...categoryChipBase,
  background: "#1e3a5f",
  borderColor: "#3b82f6",
  color: "#dde6f5",
};

const listScrollStyle: React.CSSProperties = {
  overflowY: "auto",
  flex: 1,
  minHeight: 0,
  // job-0283 — hairline dividers (were #333 / #2a2d35), modal family.
  borderTop: "1px solid rgba(255,255,255,0.08)",
  paddingTop: 10,
  paddingRight: 4,
};

const toolRowStyle: React.CSSProperties = {
  padding: "10px 8px 12px",
  borderBottom: "1px solid rgba(255,255,255,0.06)",
};

const toolNameStyle: React.CSSProperties = {
  fontFamily:
    "'JetBrains Mono', 'Fira Code', 'Menlo', monospace",
  fontSize: 13,
  color: "#dfe5f0",
  fontWeight: 600,
  marginBottom: 4,
};

const toolDescStyle: React.CSSProperties = {
  fontSize: 12,
  color: "#aab0bd",
  lineHeight: 1.5,
  marginBottom: 6,
};

const badgesRowStyle: React.CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 4,
  marginTop: 4,
  marginBottom: 6,
};

const sampleQueryStyle: React.CSSProperties = {
  fontSize: 11,
  color: "#869aae",
  fontStyle: "italic",
  cursor: "pointer",
  padding: "2px 4px",
  borderRadius: 4,
  display: "inline-block",
  marginRight: 4,
  marginTop: 2,
};

// ---------------------------------------------------------------------------
// Badge logic.
// ---------------------------------------------------------------------------

interface BadgeSpec {
  label: string;
  background: string;
  color: string;
  border?: string;
  title?: string;
}

/**
 * Resolve the annotation badges we surface for a single tool. Default values
 * (read_only=true, idempotent=true, open_world=false, destructive=false) are
 * NOT surfaced — only the interesting outliers. This keeps the UI clean.
 */
export function deriveBadges(t: ToolCatalogTool): BadgeSpec[] {
  const badges: BadgeSpec[] = [];

  // Category chip — always shown.
  badges.push({
    label: t.category_id,
    background: "rgba(60,80,120,0.45)",
    color: "#cfd3dc",
    border: "1px solid #4a5266",
    title: `Primary category: ${t.category_id}`,
  });

  // Non-default annotation highlights.
  if (!t.annotations.read_only_hint) {
    badges.push({
      label: "writes",
      background: "rgba(120,60,30,0.55)",
      color: "#fae2c0",
      border: "1px solid #d97a3a",
      title: "Tool mutates external state (not read-only)",
    });
  }
  if (t.annotations.open_world_hint) {
    badges.push({
      label: "open-world",
      background: "rgba(150,120,30,0.45)",
      color: "#f3e7b2",
      border: "1px solid #c9a93a",
      title: "Tool calls external APIs / public data endpoints",
    });
  }
  if (t.annotations.destructive_hint) {
    badges.push({
      label: "destructive",
      background: "rgba(180,40,40,0.55)",
      color: "#ffe0e0",
      border: "1px solid #e74c4c",
      title: "Tool can permanently overwrite existing state",
    });
  }
  if (!t.annotations.idempotent_hint) {
    badges.push({
      label: "non-idempotent",
      background: "rgba(60,80,160,0.5)",
      color: "#d9e2f5",
      border: "1px solid #5478c9",
      title: "Re-invoking with same args may produce different results",
    });
  }
  return badges;
}

/** Compute the FetchCatalogUrl, honouring build-time overrides.
 *
 * Delegates to the canonical URL-derivation seam (lib/public_base.ts). That
 * preserves the prior precedence exactly — explicit VITE_GRACE2_HTTP_URL wins,
 * then the sprint-14-aws VITE_GRACE2_PUBLIC_BASE single-origin seam, then the
 * byte-identical window-derived <proto>//<host>:8766 default — while letting a
 * CloudFront/HTTPS deploy collapse the catalog onto https://<domain>. */
function defaultCatalogUrl(): string {
  return catalogUrlFromBase();
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

type LoadState = "loading" | "ready" | "error";

export function ToolsCatalogPopup({
  onClose,
  initialCatalog = null,
  catalogUrl,
}: ToolsCatalogPopupProps): JSX.Element {
  const [catalog, setCatalog] = useState<ToolCatalogPayload | null>(initialCatalog);
  const [state, setState] = useState<LoadState>(
    initialCatalog ? "ready" : "loading",
  );
  const [errorText, setErrorText] = useState<string | null>(null);
  const [activeCategory, setActiveCategory] = useState<string | null>(null);
  const [searchRaw, setSearchRaw] = useState<string>("");
  const [searchDebounced, setSearchDebounced] = useState<string>("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [copyFlash, setCopyFlash] = useState<string | null>(null);

  // Debounced search (≤300ms per kickoff).
  useEffect(() => {
    const t = setTimeout(() => setSearchDebounced(searchRaw), 250);
    return () => clearTimeout(t);
  }, [searchRaw]);

  // Esc to dismiss.
  useEffect(() => {
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Initial fetch -- COLD-FIRST (NATE 2026-06-27: "I shouldn't have to start an
  // agent to see tools"). The read-only catalog loads WITHOUT a running agent:
  // we try the durable STATIC snapshot in the public web bucket FIRST (always
  // up, and a plain S3 GET that does NOT wake the auto-stopped box), then fall
  // back to the live box-local /api/tool-catalog only if the snapshot is
  // missing/unreachable. Each attempt is bounded by its own 10s timeout; an
  // error surfaces only if EVERY source fails.
  //
  // An explicit `catalogUrl` prop is treated as a deliberate single-source
  // override (tests + callers that pin the endpoint) -- it skips the cold path.
  useEffect(() => {
    if (initialCatalog) return;
    let cancelled = false;
    let activeController: AbortController | null = null;
    const sources =
      catalogUrl != null && catalogUrl !== ""
        ? [catalogUrl]
        : [coldCatalogUrl(), defaultCatalogUrl()];
    const TIMEOUT_MS = 10_000;

    (async () => {
      let lastErr: unknown = null;
      let lastWasAbort = false;
      for (const url of sources) {
        const controller = new AbortController();
        activeController = controller;
        const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
        try {
          const resp = await fetch(url, {
            method: "GET",
            signal: controller.signal,
          });
          if (!resp.ok) {
            throw new Error(`HTTP ${resp.status}`);
          }
          const json = (await resp.json()) as ToolCatalogPayload;
          clearTimeout(timer);
          if (cancelled) return;
          setCatalog(json);
          setState("ready");
          return; // first successful source wins
        } catch (err) {
          clearTimeout(timer);
          if (cancelled) return;
          lastErr = err;
          lastWasAbort = err instanceof DOMException && err.name === "AbortError";
          // fall through to the next source
        }
      }
      if (cancelled) return;
      setErrorText(
        lastWasAbort
          ? `request timed out after ${TIMEOUT_MS / 1000}s (the agent may be asleep)`
          : lastErr instanceof Error
            ? lastErr.message
            : "unknown fetch error",
      );
      setState("error");
    })();

    return () => {
      cancelled = true;
      activeController?.abort();
    };
  }, [catalogUrl, initialCatalog]);

  const filteredTools = useMemo<ToolCatalogTool[]>(() => {
    if (!catalog) return [];
    const q = searchDebounced.trim().toLowerCase();
    return catalog.tools.filter((t) => {
      if (activeCategory) {
        const matchesPrimary = t.category_id === activeCategory;
        const matchesSecondary = t.secondary_category_ids.includes(activeCategory);
        if (!matchesPrimary && !matchesSecondary) return false;
      }
      if (!q) return true;
      return (
        t.name.toLowerCase().includes(q) ||
        t.description.toLowerCase().includes(q) ||
        t.description_full.toLowerCase().includes(q)
      );
    });
  }, [catalog, activeCategory, searchDebounced]);

  const handleCategoryClick = useCallback((id: string) => {
    setActiveCategory((prev) => (prev === id ? null : id));
  }, []);

  const handleToggleExpanded = useCallback((toolName: string) => {
    setExpanded((prev) => ({ ...prev, [toolName]: !prev[toolName] }));
  }, []);

  const handleCopyQuery = useCallback(async (text: string) => {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        // Fallback for happy-dom tests / older browsers.
        const ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        // eslint-disable-next-line deprecation/deprecation
        document.execCommand?.("copy");
        document.body.removeChild(ta);
      }
      setCopyFlash(text);
      setTimeout(() => setCopyFlash((prev) => (prev === text ? null : prev)), 1200);
    } catch {
      // swallow — copy is a UX nice-to-have, not a hard contract
    }
  }, []);

  return (
    <div
      data-testid="grace2-tools-catalog-popup"
      role="dialog"
      aria-modal="true"
      aria-label="Tools catalog"
      style={overlayStyle}
      onClick={onClose}
    >
      <div
        data-testid="grace2-tools-catalog-popup-card"
        style={cardStyle}
        onClick={(e) => e.stopPropagation()}
      >
        <button
          data-testid="grace2-tools-catalog-popup-close"
          aria-label="Close tools catalog"
          onClick={onClose}
          style={closeBtnStyle}
        >
          <IconClose size={18} />
        </button>

        <h2 style={headerStyle}>
          <span>Tools catalog</span>
          <span style={subtitleStyle}>
            {catalog
              ? `${catalog.tools.length} tools across ${catalog.categories.length} categories`
              : ""}
          </span>
        </h2>

        <input
          data-testid="grace2-tools-catalog-search"
          aria-label="Search tools"
          placeholder="Search tool names or descriptions..."
          value={searchRaw}
          onChange={(e) => setSearchRaw(e.target.value)}
          style={searchInputStyle}
        />

        {state === "loading" && (
          <div
            data-testid="grace2-tools-catalog-loading"
            style={{ padding: 20, color: "#9aa0ad", fontSize: 12 }}
          >
            Loading catalog...
          </div>
        )}

        {state === "error" && (
          <div
            data-testid="grace2-tools-catalog-error"
            style={{
              padding: 14,
              color: "#f9c1c1",
              background: "rgba(60,20,20,0.4)",
              borderRadius: 6,
              border: "1px solid #6b3030",
              fontSize: 12,
              lineHeight: 1.5,
            }}
          >
            Could not load the tool catalog: {errorText}.
            <br />
            Make sure the agent service is running and that{" "}
            <code style={{ fontFamily: "monospace" }}>
              /api/tool-catalog
            </code>{" "}
            is reachable.
          </div>
        )}

        {state === "ready" && catalog && (
          <>
            <div
              data-testid="grace2-tools-catalog-categories"
              style={categoryGridStyle}
            >
              {catalog.categories.map((c) => {
                const active = activeCategory === c.id;
                return (
                  <button
                    key={c.id}
                    data-testid={`grace2-tools-catalog-category-${c.id}`}
                    data-active={active ? "true" : "false"}
                    onClick={() => handleCategoryClick(c.id)}
                    style={active ? categoryChipActive : categoryChipBase}
                    title={c.description}
                    aria-pressed={active}
                  >
                    <span style={{ fontWeight: 600 }}>{c.name}</span>
                    <span style={{ fontSize: 10, color: "#9aa0ad" }}>
                      {c.tool_count} tool{c.tool_count === 1 ? "" : "s"}
                    </span>
                  </button>
                );
              })}
            </div>

            {activeCategory !== null && (
              <button
                data-testid="grace2-tools-catalog-clear-filter"
                onClick={() => setActiveCategory(null)}
                style={{
                  background: "transparent",
                  border: "none",
                  color: "#7aa7ff",
                  fontSize: 11,
                  cursor: "pointer",
                  textDecoration: "underline",
                  padding: 0,
                  marginBottom: 8,
                }}
              >
                Clear category filter
              </button>
            )}

            <div
              data-testid="grace2-tools-catalog-list"
              style={listScrollStyle}
            >
              {filteredTools.length === 0 && (
                <div
                  data-testid="grace2-tools-catalog-empty"
                  style={{
                    padding: 20,
                    color: "#9aa0ad",
                    fontSize: 12,
                    textAlign: "center",
                  }}
                >
                  No tools match. Try a category.
                </div>
              )}
              {filteredTools.map((t) => {
                const isExpanded = !!expanded[t.name];
                const showText = isExpanded
                  ? t.description_full
                  : t.description.slice(0, 200) + (t.description.length > 200 ? "…" : "");
                const badges = deriveBadges(t);
                return (
                  <div
                    key={t.name}
                    data-testid="grace2-tools-catalog-row"
                    data-tool-name={t.name}
                    style={toolRowStyle}
                  >
                    <div style={toolNameStyle}>{t.name}</div>
                    <div style={toolDescStyle}>{showText}</div>
                    {t.description_full.length > 200 && (
                      <button
                        data-testid={`grace2-tools-catalog-expand-${t.name}`}
                        onClick={() => handleToggleExpanded(t.name)}
                        style={{
                          background: "transparent",
                          border: "none",
                          color: "#7aa7ff",
                          fontSize: 10,
                          cursor: "pointer",
                          textDecoration: "underline",
                          padding: 0,
                          marginBottom: 4,
                        }}
                      >
                        {isExpanded ? "Show less" : "Show more"}
                      </button>
                    )}

                    <div style={badgesRowStyle}>
                      {badges.map((b, i) => (
                        <span
                          key={`${t.name}-badge-${i}`}
                          title={b.title}
                          style={{
                            background: b.background,
                            color: b.color,
                            border: b.border ?? "none",
                            borderRadius: 4,
                            padding: "2px 7px",
                            fontSize: 10,
                            letterSpacing: "0.02em",
                            fontFamily: "inherit",
                          }}
                        >
                          {b.label}
                        </span>
                      ))}
                      <span
                        data-testid={`grace2-tools-catalog-globalq-${t.name}`}
                        title={
                          t.supports_global_query
                            ? "Accepts a global / CONUS-wide query (no bbox required)"
                            : "Requires an explicit bbox or polygon scope"
                        }
                        style={{
                          fontSize: 10,
                          padding: "2px 5px",
                          borderRadius: 4,
                          color: t.supports_global_query ? "#9be0a0" : "#888",
                          background: t.supports_global_query
                            ? "rgba(40,100,40,0.3)"
                            : "rgba(40,40,40,0.5)",
                          border: t.supports_global_query
                            ? "1px solid #4f8a52"
                            : "1px solid #555",
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 3,
                        }}
                      >
                        <IconGlobe size={11} />
                        {t.supports_global_query ? "global ok" : "scoped"}
                      </span>
                    </div>

                    {t.sample_queries.length > 0 && (
                      <div data-testid={`grace2-tools-catalog-samples-${t.name}`}>
                        {t.sample_queries.slice(0, 3).map((q, i) => {
                          const flashing = copyFlash === q;
                          return (
                            <span
                              key={`${t.name}-q-${i}`}
                              role="button"
                              tabIndex={0}
                              onClick={() => handleCopyQuery(q)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter" || e.key === " ") {
                                  e.preventDefault();
                                  void handleCopyQuery(q);
                                }
                              }}
                              title="Click to copy this example query"
                              style={{
                                ...sampleQueryStyle,
                                background: flashing
                                  ? "rgba(40,90,40,0.45)"
                                  : "transparent",
                                color: flashing ? "#cfeac0" : "#869aae",
                              }}
                            >
                              {flashing ? "copied: " : "“"}
                              {q}
                              {flashing ? "" : "”"}
                            </span>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
