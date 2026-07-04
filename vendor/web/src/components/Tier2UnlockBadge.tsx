// GRACE-2 web — Tier2UnlockBadge (job-0125, sprint-12-mega Wave 2).
//
// Small pill/badge rendered next to a Tier-2 tool reference in chat. Colors:
//   GREEN  — a key exists for the provider (tool will work)
//   GRAY   — no key (tool will fail at invocation; user prompted to add one)
//
// Driven by props the caller computes from the SecretsListPayload — the
// badge itself is purely presentational so it can be embedded wherever the
// chat stream surfaces a Tier-2 tool name without re-doing the lookup.
//
// Kickoff §3: "subtle pill/badge next to each Tier-2 tool reference in chat
// ('eBird (key required)') that's GREEN when a key exists, GRAY when not."

import { ProviderID } from "../contracts";
import { IconCheck } from "./icons";

// Tier-2 providers that surface a "key required" badge in chat. The set is
// closed at v0.1 — broadening it requires the corresponding §F.3 amendment.
// Each entry's label is the human-facing string the chat embeds.
const TIER2_BADGE_LABEL: Partial<Record<ProviderID, string>> = {
  ebird: "eBird",
  iucn_red_list: "IUCN Red List",
  movebank: "Movebank",
  nws: "NWS",
  openweathermap: "OpenWeatherMap",
};

export interface Tier2UnlockBadgeProps {
  /** The provider whose key-presence the badge reflects. */
  provider: ProviderID;
  /** True when a key for this provider exists in the secrets list (the
   *  parent computes this by scanning the active SecretRecords). */
  unlocked: boolean;
}

const badgeBaseStyle: React.CSSProperties = {
  display: "inline-block",
  fontSize: 10,
  padding: "1px 6px",
  borderRadius: 8,
  marginLeft: 4,
  fontWeight: 500,
  lineHeight: 1.4,
  fontFamily: "inherit",
  verticalAlign: "middle",
};

const unlockedStyle: React.CSSProperties = {
  ...badgeBaseStyle,
  background: "rgba(16,185,129,0.18)", // green-500 @ 18% — subtle on dark
  color: "#10b981",
  border: "1px solid rgba(16,185,129,0.4)",
};

const lockedStyle: React.CSSProperties = {
  ...badgeBaseStyle,
  background: "rgba(107,114,128,0.18)", // gray-500 @ 18%
  color: "#9ca3af",
  border: "1px solid rgba(107,114,128,0.4)",
};

/**
 * Tier2UnlockBadge — a pill rendering "{provider} (key required)" in gray
 * or "{provider}" with a check icon in green depending on whether a key exists.
 */
export function Tier2UnlockBadge({
  provider,
  unlocked,
}: Tier2UnlockBadgeProps): JSX.Element | null {
  const label = TIER2_BADGE_LABEL[provider];
  if (!label) return null; // not a Tier-2 surfaced provider; render nothing
  return (
    <span
      data-testid={`grace2-tier2-badge-${provider}`}
      data-unlocked={unlocked ? "true" : "false"}
      style={{
        ...(unlocked ? unlockedStyle : lockedStyle),
        ...(unlocked
          ? { display: "inline-flex", alignItems: "center", gap: 3 }
          : {}),
      }}
      title={
        unlocked
          ? `${label} key is registered`
          : `${label} needs an API key — add one in the secrets panel`
      }
    >
      {unlocked ? (
        <>
          {label}
          <IconCheck size={11} weight="bold" />
        </>
      ) : (
        `${label} (key required)`
      )}
    </span>
  );
}
