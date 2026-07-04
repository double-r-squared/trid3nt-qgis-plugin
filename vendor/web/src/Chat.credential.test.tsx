// GRACE-2 web — Chat credential-request routing tests (SRS §F.3 amendment).
//
// Chat itself cannot mount in happy-dom (it opens a WebSocket), so — following
// the established per-Case stream-routing test pattern — these tests exercise
// the exported pure route helpers directly:
//   - routeCredentialRequest lands a CredentialCard payload in the owning
//     stream and assigns a chronological arrival seq.
//   - duplicate request_id emits (the session-scoped fan-out can deliver the
//     same envelope twice) do NOT stack a second card.
//   - recordCredentialResolved marks the saved/declined resolution against the
//     stream the card lives in.
//   - credential cards are per-Case (route to the owning stream; a second
//     Case's stream is untouched).

import { describe, it, expect } from "vitest";
import {
  ROOT_STREAM_KEY,
  createChatStreams,
  getStream,
  routeUserMessage,
  routeCredentialRequest,
  recordCredentialResolved,
} from "./Chat";
import { CredentialRequestPayload } from "./contracts";

const CASE_A = "01CASEAAAAAAAAAAAAAAAAAAAA";
const CASE_B = "01CASEBBBBBBBBBBBBBBBBBBBB";

function req(
  requestId: string,
  overrides: Partial<CredentialRequestPayload> = {},
): CredentialRequestPayload {
  return {
    envelope_type: "credential-request",
    request_id: requestId,
    provider_id: "ebird",
    provider_label: "eBird",
    signup_url: "https://ebird.org/api/keygen",
    secret_key_name: "EBIRD_API_KEY",
    message: "eBird needs an API key.",
    tool_name: "fetch_ebird_observations",
    ...overrides,
  };
}

describe("routeCredentialRequest — credential card routing (§F.3)", () => {
  it("lands a credential request in the owning stream with an arrival seq", () => {
    const cs = createChatStreams();
    // A turn is in flight for CASE_A (targetKey owns following envelopes).
    routeUserMessage(cs, CASE_A, "show me bird observations");
    routeCredentialRequest(cs, req("R1"));
    const s = getStream(cs, CASE_A);
    expect(s.credentialRequests.map((r) => r.request_id)).toEqual(["R1"]);
    expect(s.credentialSeqs.get("R1")).toBeGreaterThan(0);
  });

  it("de-dupes a duplicate request_id (session-scoped fan-out can repeat)", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routeCredentialRequest(cs, req("R1"));
    routeCredentialRequest(cs, req("R1"));
    expect(getStream(cs, CASE_A).credentialRequests).toHaveLength(1);
  });

  it("routes to the OWNING stream; another Case is untouched", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "A prompt");
    routeCredentialRequest(cs, req("R1"));
    // The card lives only in CASE_A's stream.
    expect(getStream(cs, CASE_A).credentialRequests).toHaveLength(1);
    expect(getStream(cs, CASE_B).credentialRequests).toHaveLength(0);
    expect(getStream(cs, ROOT_STREAM_KEY).credentialRequests).toHaveLength(0);
  });

  it("explicit caseId targeting overrides the in-flight targetKey", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "A prompt");
    // A late credential-request for CASE_B (the user navigated away) buffers
    // into B's stream, not the currently-owning A.
    routeCredentialRequest(cs, req("R2"), CASE_B);
    expect(getStream(cs, CASE_A).credentialRequests).toHaveLength(0);
    expect(getStream(cs, CASE_B).credentialRequests.map((r) => r.request_id)).toEqual([
      "R2",
    ]);
  });

  // NATE no-URL fallback (2026-06-18): a credential-request with NO reliable
  // signup URL (null / undefined / "" — e.g. a USGS-water-gauge key, or a
  // credential-shaped failure from a tool that isn't even in the registry)
  // must still route + carry the card cleanly. The render guard lives in
  // CredentialCard (name-only, no fabricated link); here we pin that the
  // routing layer never drops or mangles a no-URL payload — the card the user
  // sees is built from exactly this stored request.
  it("routes a no-URL (signup_url: null) credential request unchanged", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "fetch era5 reanalysis");
    // Mirrors the live Mexico-Beach ERA5 failure: a keyed provider whose
    // request reaches the client with NO signup URL (NATE no-URL fallback).
    routeCredentialRequest(
      cs,
      req("R3", {
        provider_id: "ecmwf_cds",
        provider_label: "Copernicus CDS",
        signup_url: null,
        secret_key_name: "CDSAPI_KEY",
        message: "Copernicus CDS needs an API key.",
        tool_name: "fetch_era5_reanalysis",
      }),
    );
    const stored = getStream(cs, CASE_A).credentialRequests;
    expect(stored).toHaveLength(1);
    // The card consumes request.signup_url directly; a null survives intact so
    // CredentialCard's `request.signup_url && (...)` guard renders name-only.
    expect(stored[0]!.signup_url).toBeNull();
    expect(stored[0]!.provider_label).toBe("Copernicus CDS");
  });

  it("treats an empty-string signup_url as no-URL (carried through unchanged)", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "fetch some keyed source");
    routeCredentialRequest(cs, req("R4", { signup_url: "" }));
    const stored = getStream(cs, CASE_A).credentialRequests;
    expect(stored).toHaveLength(1);
    // "" is falsy, so CredentialCard's truthy guard hides the link exactly like
    // null does — no broken/empty anchor. The routing layer carries it verbatim.
    expect(stored[0]!.signup_url).toBe("");
  });

  it("a no-URL request still records a saved resolution (name-only card usable)", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routeCredentialRequest(cs, req("R5", { signup_url: null }));
    recordCredentialResolved(cs, CASE_A, "R5", "saved");
    expect(getStream(cs, CASE_A).credentialResolved.get("R5")).toBe("saved");
  });
});

describe("recordCredentialResolved — saved / declined", () => {
  it("marks a request saved against its stream", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routeCredentialRequest(cs, req("R1"));
    recordCredentialResolved(cs, CASE_A, "R1", "saved");
    expect(getStream(cs, CASE_A).credentialResolved.get("R1")).toBe("saved");
  });

  it("marks a request declined against its stream", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, CASE_A, "hi");
    routeCredentialRequest(cs, req("R1"));
    recordCredentialResolved(cs, CASE_A, "R1", "declined");
    expect(getStream(cs, CASE_A).credentialResolved.get("R1")).toBe("declined");
  });
});
