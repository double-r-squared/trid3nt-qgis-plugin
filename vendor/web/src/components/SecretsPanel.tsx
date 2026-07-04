// GRACE-2 web — SecretsPanel (job-0125, sprint-12-mega Wave 2).
//
// Per-Case Tier-2 API-key entry surface. Renders:
//   - A list of existing `SecretRecord`s (provider + optional label + revoke
//     button per row). NEVER renders the raw key value — only the opaque
//     vault_ref derived metadata.
//   - An "Add secret" form: provider <select>, optional label <input>, key
//     value <input type="password">, scope toggle (this-Case vs user-wide),
//     submit. After submission, the form clears the key field immediately.
//   - Friendly empty state when no secrets are present.
//
// Wire shape (per packages/contracts/src/grace2_contracts/secrets.py):
//   - secrets-list (server -> client) is consumed via the bus subscription.
//   - secret-add (client -> server) carries:
//       { provider, case_id, label, key_value }
//   - secret-revoke (client -> server) carries: { secret_id }
//
// Security (kickoff §5):
//   - key field is `<input type="password">` to suppress shoulder-surfing
//     and password-manager autofill of the wrong value.
//   - the key value is cleared from local state immediately after submit.
//   - we NEVER log the key value (no console.log, no error trace), NEVER
//     persist to localStorage, and the key is never echoed in DOM after
//     submit (the input is reset).
//   - the parent component is responsible for emitting the secret-add
//     envelope through GraceWs; SecretsPanel itself is decoupled from the
//     WebSocket — it calls `onSecretAdd` / `onSecretRevoke` callbacks the
//     parent wires.
//
// Invariant 9 (no cost theater): no cost / quota / usage field surfaced
// anywhere. `last_used_at` is the only usage signal we render.
//
// Decision F binding: the panel does NOT persist or echo `key_value` after
// submit — the only place it appears is the transient form-state during the
// keystroke window.

import { useState } from "react";
import { ProviderID, SecretRecord, SecretsListPayload } from "../contracts";

// --- Display vocabulary -------------------------------------------------- //

// Human-readable labels for each ProviderID. Closed Literal — adding a new
// member here without the corresponding SRS §F.3 + secrets.py amendment is
// a bug. The fallback `?? provider` is defensive against schema drift.
const PROVIDER_LABEL: Record<ProviderID, string> = {
  ebird: "eBird",
  iucn_red_list: "IUCN Red List",
  movebank: "Movebank",
  firms: "NASA FIRMS",
  ecmwf_cds: "Copernicus Climate Data Store",
  gtsm: "GTSM (Copernicus CDS)",
  nws: "NWS (weather)",
  openweathermap: "OpenWeatherMap",
  openai: "OpenAI",
  anthropic: "Anthropic",
  google_genai: "Google GenAI",
  mapbox: "Mapbox",
  maptiler: "MapTiler",
  // Generic name-only fallback scope (any keyed endpoint with no dedicated
  // provider). The credential card shows a tool-derived name; this is the
  // SecretsPanel list label for keys saved under that fallback scope.
  generic: "Other data source",
};

// Tier-2 conservation/weather providers (the kickoff §4 empty-state names
// these explicitly). Used only for the empty-state copy.
const TIER2_EMPTY_STATE_NAMES = "eBird, IUCN Red List, Movebank, NWS";

// --- Props --------------------------------------------------------------- //

export interface SecretsPanelProps {
  /** Current list of secrets to render. Empty array triggers the empty state. */
  secrets: SecretRecord[];
  /** Optional Case ID — when set, the "this Case" scope option is available
   *  and is the default for the scope toggle. When null, only user-wide is
   *  available (M6+ identity-required path; we still render it disabled so
   *  the user sees the affordance with an explanatory tooltip). */
  caseId: string | null;
  /** Parent emits the `secret-add` envelope. Receives a single payload
   *  argument; the panel handles form lifecycle (clear key on submit). */
  onSecretAdd: (payload: {
    provider: ProviderID;
    case_id: string | null;
    label: string | null;
    key_value: string;
  }) => void;
  /** Parent emits the `secret-revoke` envelope for the given secret ID. */
  onSecretRevoke: (secretId: string) => void;
}

// --- Styles -------------------------------------------------------------- //

// panelStyle carries only typography/color — no card chrome (background,
// border, borderRadius, width, maxHeight). The enclosing surface (job-0321
// F29: the Settings popup's "API Keys" section; formerly the standalone
// SecretsPopup) is the sole card surface; the panel content lays out flat
// inside it.
const panelStyle: React.CSSProperties = {
  color: "#ccc",
  fontSize: 13,
  fontFamily: "inherit",
};

const sectionLabelStyle: React.CSSProperties = {
  fontSize: 11,
  textTransform: "uppercase",
  letterSpacing: "0.05em",
  color: "#888",
  marginTop: 12,
  marginBottom: 4,
};

// job-0283 — form controls join the modal family: hairline borders + 8px
// radius (was #555 / 4px). Visual only.
const inputStyle: React.CSSProperties = {
  background: "rgba(40,40,50,0.9)",
  border: "1px solid rgba(255,255,255,0.14)",
  borderRadius: 8,
  color: "#ddd",
  padding: "4px 6px",
  fontSize: 12,
  fontFamily: "inherit",
  width: "100%",
  boxSizing: "border-box",
  marginBottom: 6,
};

const buttonStyle: React.CSSProperties = {
  background: "rgba(40,40,50,0.9)",
  border: "1px solid rgba(255,255,255,0.14)",
  borderRadius: 8,
  color: "#ddd",
  padding: "4px 8px",
  cursor: "pointer",
  fontSize: 12,
  fontFamily: "inherit",
};

const submitButtonStyle: React.CSSProperties = {
  ...buttonStyle,
  background: "#2563eb",
  borderColor: "#2563eb",
  color: "#fff",
  marginTop: 4,
};

const secretRowStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "row",
  alignItems: "center",
  gap: 8,
  padding: "6px 4px",
  // job-0283 — hairline divider (was #333), matching the modal family.
  borderBottom: "1px solid rgba(255,255,255,0.08)",
};

const emptyStateStyle: React.CSSProperties = {
  fontSize: 12,
  color: "#aaa",
  padding: "10px 4px",
  lineHeight: 1.5,
};

const errorStyle: React.CSSProperties = {
  color: "#e88",
  fontSize: 11,
  marginTop: 4,
};

// --- Helpers ------------------------------------------------------------- //

function formatLastUsed(iso: string | null | undefined): string {
  if (!iso) return "never used";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "never used";
    // Display received timestamp verbatim — no arithmetic / "X days ago"
    // (invariant 1: no client-computed numbers). The agent / persistence
    // layer owns the time-arithmetic if it surfaces it.
    return `last used ${d.toISOString().slice(0, 10)}`;
  } catch {
    return "never used";
  }
}

// --- Component ----------------------------------------------------------- //

/**
 * SecretsPanel — list + add/revoke per-Case Tier-2 API keys.
 *
 * The component is purely a consumer/emitter: it reads the secrets list it
 * is handed (props.secrets) and calls onSecretAdd / onSecretRevoke when
 * the user submits. The parent App.tsx wires the bus subscription on the
 * read path and the GraceWs envelope emission on the write path.
 */
export function SecretsPanel({
  secrets,
  caseId,
  onSecretAdd,
  onSecretRevoke,
}: SecretsPanelProps): JSX.Element {
  const [provider, setProvider] = useState<ProviderID>("ebird");
  const [label, setLabel] = useState<string>("");
  const [keyValue, setKeyValue] = useState<string>("");
  // Scope: "case" => this Case only; "user" => cross-Case (M6+ — disabled
  // when no Firebase identity is configured, but the affordance is shown).
  const [scope, setScope] = useState<"case" | "user">(caseId ? "case" : "user");
  const [error, setError] = useState<string | null>(null);

  function handleSubmit(e: React.FormEvent<HTMLFormElement>): void {
    e.preventDefault();
    setError(null);
    if (!keyValue.trim()) {
      setError("Key value is required.");
      return;
    }
    if (scope === "case" && !caseId) {
      setError("Open a Case before scoping a secret to it.");
      return;
    }
    // Emit. The parent wraps it in an envelope and pushes it on the WS.
    onSecretAdd({
      provider,
      case_id: scope === "case" ? caseId : null,
      label: label.trim() || null,
      key_value: keyValue,
    });
    // Security: clear key from local state IMMEDIATELY after submit.
    // (kickoff §5 / Decision F: key value never lingers in the DOM /
    // React state past the submit transaction.)
    setKeyValue("");
    setLabel("");
  }

  const activeSecrets = secrets.filter((s) => s.is_active);

  return (
    <div data-testid="grace2-secrets-panel" style={panelStyle}>
      {/* Empty state — friendly copy when nothing exists yet. */}
      {activeSecrets.length === 0 && (
        <div
          data-testid="grace2-secrets-empty-state"
          style={emptyStateStyle}
        >
          Add a key to unlock additional data sources ({TIER2_EMPTY_STATE_NAMES}).
        </div>
      )}

      {/* Existing secrets list */}
      {activeSecrets.length > 0 && (
        <div data-testid="grace2-secrets-list">
          {activeSecrets.map((s) => (
            <div
              key={s.secret_id}
              data-testid={`grace2-secret-row-${s.secret_id}`}
              data-provider={s.provider}
              style={secretRowStyle}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ color: "#ddd", fontSize: 12 }}>
                  {PROVIDER_LABEL[s.provider] ?? s.provider}
                </div>
                {s.label && (
                  <div
                    data-testid={`grace2-secret-label-${s.secret_id}`}
                    style={{
                      fontSize: 11,
                      color: "#aaa",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={s.label}
                  >
                    {s.label}
                  </div>
                )}
                <div style={{ fontSize: 10, color: "#777" }}>
                  {formatLastUsed(s.last_used_at)}
                </div>
              </div>
              <button
                data-testid={`grace2-secret-revoke-${s.secret_id}`}
                onClick={() => onSecretRevoke(s.secret_id)}
                style={buttonStyle}
                aria-label={`Revoke ${PROVIDER_LABEL[s.provider] ?? s.provider} key`}
              >
                Revoke
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Add-secret form */}
      <div style={sectionLabelStyle}>Add a key</div>
      <form
        data-testid="grace2-secret-add-form"
        onSubmit={handleSubmit}
        autoComplete="off"
      >
        <label htmlFor="grace2-secret-provider" style={{ fontSize: 11, color: "#aaa" }}>
          Provider
        </label>
        <select
          id="grace2-secret-provider"
          data-testid="grace2-secret-provider"
          value={provider}
          onChange={(e) => setProvider(e.target.value as ProviderID)}
          style={inputStyle}
        >
          {(Object.keys(PROVIDER_LABEL) as ProviderID[]).map((p) => (
            <option key={p} value={p}>
              {PROVIDER_LABEL[p]}
            </option>
          ))}
        </select>

        <label htmlFor="grace2-secret-label" style={{ fontSize: 11, color: "#aaa" }}>
          Label (optional)
        </label>
        <input
          id="grace2-secret-label"
          data-testid="grace2-secret-label"
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="e.g. personal-eBird-key"
          maxLength={200}
          style={inputStyle}
        />

        <label htmlFor="grace2-secret-key" style={{ fontSize: 11, color: "#aaa" }}>
          API key
        </label>
        <input
          id="grace2-secret-key"
          data-testid="grace2-secret-key"
          // Security: type=password suppresses casual shoulder-surfing.
          // autocomplete=new-password keeps password managers from filling
          // in the wrong saved credential.
          type="password"
          autoComplete="new-password"
          value={keyValue}
          onChange={(e) => setKeyValue(e.target.value)}
          maxLength={2048}
          style={inputStyle}
        />

        <div style={{ marginBottom: 6 }}>
          <label style={{ fontSize: 11, color: "#aaa", marginRight: 8 }}>
            <input
              type="radio"
              data-testid="grace2-secret-scope-case"
              name="grace2-secret-scope"
              value="case"
              checked={scope === "case"}
              disabled={!caseId}
              onChange={() => setScope("case")}
              style={{ marginRight: 4 }}
            />
            This Case
          </label>
          <label
            style={{ fontSize: 11, color: "#aaa" }}
            title="User-wide secrets require a signed-in account."
          >
            <input
              type="radio"
              data-testid="grace2-secret-scope-user"
              name="grace2-secret-scope"
              value="user"
              checked={scope === "user"}
              onChange={() => setScope("user")}
              style={{ marginRight: 4 }}
            />
            User-wide
          </label>
        </div>

        <button
          type="submit"
          data-testid="grace2-secret-submit"
          style={submitButtonStyle}
          aria-label="Add secret"
        >
          Add key
        </button>
        {error && (
          <div data-testid="grace2-secret-error" style={errorStyle} role="alert">
            {error}
          </div>
        )}
      </form>
    </div>
  );
}

// Re-export for parent-side wiring — Chat.tsx / App.tsx use this signature
// to bridge between bus subscription and panel props.
export type { SecretsListPayload };
