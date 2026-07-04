// GRACE-2 web — SecretsPanel + Tier2UnlockBadge + ws.ts secret-* tests
// (job-0125, sprint-12-mega Wave 2).
//
// Verifies:
//   1. SecretsPanel renders the friendly empty state when secrets=[].
//   2. SecretsPanel renders the list of existing secrets with provider +
//      label + revoke button (one row per active record).
//   3. SecretsPanel add-form submission calls onSecretAdd with the correct
//      payload shape (provider / case_id / label / key_value).
//   4. SecretsPanel revoke button calls onSecretRevoke with the secret id.
//   5. SecretsPanel clears the key field after successful submit (no echo).
//   6. Tier2UnlockBadge color flips GREEN/GRAY based on the `unlocked` prop.
//   7. ws.ts dispatches a `secrets-list` envelope to onSecretsList.
//   8. ws.ts sendSecretAdd emits the right envelope shape.
//   9. ws.ts sendSecretRevoke emits the right envelope shape.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { SecretsPanel } from "./components/SecretsPanel";
import { Tier2UnlockBadge } from "./components/Tier2UnlockBadge";
import { GraceWs, type WsHandlers } from "./ws";
import type { SecretRecord, SecretsListPayload } from "./contracts";

// --- Fake WebSocket harness (mirrors AuthPanel.test pattern) ------------ //

interface FakeSocket {
  readyState: number;
  sent: string[];
  listeners: Record<string, ((ev: unknown) => void)[]>;
  triggerOpen(): void;
  triggerMessage(data: unknown): void;
}

function installFakeWebSocket(): { factory: () => FakeSocket } {
  let lastSocket: FakeSocket | null = null;
  class FakeWS {
    static OPEN = 1;
    static CONNECTING = 0;
    static CLOSED = 3;
    readyState = FakeWS.CONNECTING;
    sent: string[] = [];
    listeners: Record<string, ((ev: unknown) => void)[]> = {};
    constructor(_url: string) {
      lastSocket = this as unknown as FakeSocket;
    }
    addEventListener(type: string, cb: (ev: unknown) => void): void {
      (this.listeners[type] ??= []).push(cb);
    }
    send(data: string): void {
      this.sent.push(data);
    }
    close(): void {
      this.readyState = FakeWS.CLOSED;
      (this.listeners["close"] ?? []).forEach((cb) => cb({}));
    }
    triggerOpen(): void {
      this.readyState = FakeWS.OPEN;
      (this.listeners["open"] ?? []).forEach((cb) => cb({}));
    }
    triggerMessage(data: unknown): void {
      (this.listeners["message"] ?? []).forEach((cb) =>
        cb({ data: typeof data === "string" ? data : JSON.stringify(data) }),
      );
    }
  }
  // @ts-expect-error replacing the global for the test only
  globalThis.WebSocket = FakeWS;
  return { factory: () => lastSocket as FakeSocket };
}

// --- Fixtures ----------------------------------------------------------- //

const SECRET_EBIRD: SecretRecord = {
  schema_version: "v1",
  secret_id: "01ABCDEFGHJKMNPQRSTVWX0001",
  provider: "ebird",
  case_id: "01ABCDEFGHJKMNPQRSTVWX0050",
  vault_ref: "gcp-sm://projects/grace2-dev/secrets/ebird-1/versions/latest",
  label: "personal-eBird-key",
  added_at: "2026-06-08T12:00:00.000Z",
  last_used_at: "2026-06-08T13:00:00.000Z",
  is_active: true,
};

const SECRET_IUCN: SecretRecord = {
  schema_version: "v1",
  secret_id: "01ABCDEFGHJKMNPQRSTVWX0002",
  provider: "iucn_red_list",
  case_id: "01ABCDEFGHJKMNPQRSTVWX0050",
  vault_ref: "gcp-sm://projects/grace2-dev/secrets/iucn-1/versions/latest",
  label: null,
  added_at: "2026-06-08T12:00:00.000Z",
  last_used_at: null,
  is_active: true,
};

// --- SecretsPanel rendering tests --------------------------------------- //

describe("SecretsPanel — empty state (kickoff §4)", () => {
  afterEach(() => cleanup());

  it("renders friendly empty-state copy when no secrets exist", () => {
    render(
      <SecretsPanel
        secrets={[]}
        caseId="01ABCDEFGHJKMNPQRSTVWX0050"
        onSecretAdd={vi.fn()}
        onSecretRevoke={vi.fn()}
      />,
    );
    const empty = screen.getByTestId("grace2-secrets-empty-state");
    expect(empty).toBeInTheDocument();
    // Must mention unlock copy + at least one of the conservation providers.
    // (job-0151: "Tier-2" is an internal term — removed from user-facing copy.)
    expect(empty.textContent ?? "").toMatch(/unlock/i);
    expect(empty.textContent ?? "").toMatch(/eBird/);
  });

  it("still renders the add-form even when empty so the user can add a first key", () => {
    render(
      <SecretsPanel
        secrets={[]}
        caseId="01ABCDEFGHJKMNPQRSTVWX0050"
        onSecretAdd={vi.fn()}
        onSecretRevoke={vi.fn()}
      />,
    );
    expect(screen.getByTestId("grace2-secret-add-form")).toBeInTheDocument();
    expect(screen.getByTestId("grace2-secret-key")).toBeInTheDocument();
  });
});

describe("SecretsPanel — existing secrets list", () => {
  afterEach(() => cleanup());

  it("renders one row per active secret with provider + label + revoke button", () => {
    render(
      <SecretsPanel
        secrets={[SECRET_EBIRD, SECRET_IUCN]}
        caseId="01ABCDEFGHJKMNPQRSTVWX0050"
        onSecretAdd={vi.fn()}
        onSecretRevoke={vi.fn()}
      />,
    );
    const list = screen.getByTestId("grace2-secrets-list");
    expect(list).toBeInTheDocument();
    expect(
      screen.getByTestId(`grace2-secret-row-${SECRET_EBIRD.secret_id}`),
    ).toHaveAttribute("data-provider", "ebird");
    expect(
      screen.getByTestId(`grace2-secret-row-${SECRET_IUCN.secret_id}`),
    ).toHaveAttribute("data-provider", "iucn_red_list");
    // eBird label is rendered (when present)
    expect(
      screen.getByTestId(`grace2-secret-label-${SECRET_EBIRD.secret_id}`),
    ).toHaveTextContent("personal-eBird-key");
    // Revoke buttons render per row
    expect(
      screen.getByTestId(`grace2-secret-revoke-${SECRET_EBIRD.secret_id}`),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId(`grace2-secret-revoke-${SECRET_IUCN.secret_id}`),
    ).toBeInTheDocument();
  });

  it("does NOT render the key value anywhere in the DOM (Decision F binding)", () => {
    // We pass a SecretRecord; the panel must NEVER display the vault_ref
    // OR any value that resembles a key. Vault refs ARE in the record but
    // are also opaque references, not key values; spot-check the rendered
    // DOM does not echo the vault_ref string (defense-in-depth).
    const { container } = render(
      <SecretsPanel
        secrets={[SECRET_EBIRD]}
        caseId={null}
        onSecretAdd={vi.fn()}
        onSecretRevoke={vi.fn()}
      />,
    );
    expect(container.innerHTML).not.toContain(SECRET_EBIRD.vault_ref);
  });

  it("filters out is_active=false records (soft-revoked)", () => {
    const revoked: SecretRecord = { ...SECRET_EBIRD, is_active: false };
    render(
      <SecretsPanel
        secrets={[revoked, SECRET_IUCN]}
        caseId="01ABCDEFGHJKMNPQRSTVWX0050"
        onSecretAdd={vi.fn()}
        onSecretRevoke={vi.fn()}
      />,
    );
    expect(
      screen.queryByTestId(`grace2-secret-row-${revoked.secret_id}`),
    ).toBeNull();
    expect(
      screen.getByTestId(`grace2-secret-row-${SECRET_IUCN.secret_id}`),
    ).toBeInTheDocument();
  });
});

describe("SecretsPanel — add-secret form", () => {
  afterEach(() => cleanup());

  it("emits onSecretAdd with the correct payload shape on submit", () => {
    const onAdd = vi.fn();
    render(
      <SecretsPanel
        secrets={[]}
        caseId="01ABCDEFGHJKMNPQRSTVWX0050"
        onSecretAdd={onAdd}
        onSecretRevoke={vi.fn()}
      />,
    );

    // Provider defaults to "ebird"; change to "movebank".
    fireEvent.change(screen.getByTestId("grace2-secret-provider"), {
      target: { value: "movebank" },
    });
    fireEvent.change(screen.getByTestId("grace2-secret-label"), {
      target: { value: "movebank-academic" },
    });
    fireEvent.change(screen.getByTestId("grace2-secret-key"), {
      target: { value: "FAKE-KEY-VALUE-XYZ" },
    });

    fireEvent.submit(screen.getByTestId("grace2-secret-add-form"));

    expect(onAdd).toHaveBeenCalledTimes(1);
    expect(onAdd).toHaveBeenCalledWith({
      provider: "movebank",
      case_id: "01ABCDEFGHJKMNPQRSTVWX0050",
      label: "movebank-academic",
      key_value: "FAKE-KEY-VALUE-XYZ",
    });
  });

  it("clears the key field after submit (Decision F — no key persistence in DOM)", () => {
    const onAdd = vi.fn();
    render(
      <SecretsPanel
        secrets={[]}
        caseId="01ABCDEFGHJKMNPQRSTVWX0050"
        onSecretAdd={onAdd}
        onSecretRevoke={vi.fn()}
      />,
    );

    const keyInput = screen.getByTestId("grace2-secret-key") as HTMLInputElement;
    fireEvent.change(keyInput, { target: { value: "SECRET-VALUE-123" } });
    expect(keyInput.value).toBe("SECRET-VALUE-123");

    fireEvent.submit(screen.getByTestId("grace2-secret-add-form"));

    // After submit the key input is cleared. The form-state holding the key
    // is reset — no DOM echo, no localStorage, nothing.
    expect(keyInput.value).toBe("");
  });

  it("the key input is type=password (security: shoulder-surf suppression)", () => {
    render(
      <SecretsPanel
        secrets={[]}
        caseId="01ABCDEFGHJKMNPQRSTVWX0050"
        onSecretAdd={vi.fn()}
        onSecretRevoke={vi.fn()}
      />,
    );
    const keyInput = screen.getByTestId("grace2-secret-key") as HTMLInputElement;
    expect(keyInput.type).toBe("password");
  });

  it("blocks submit + surfaces an error when key field is empty", () => {
    const onAdd = vi.fn();
    render(
      <SecretsPanel
        secrets={[]}
        caseId="01ABCDEFGHJKMNPQRSTVWX0050"
        onSecretAdd={onAdd}
        onSecretRevoke={vi.fn()}
      />,
    );
    fireEvent.submit(screen.getByTestId("grace2-secret-add-form"));
    expect(onAdd).not.toHaveBeenCalled();
    expect(screen.getByTestId("grace2-secret-error")).toHaveTextContent(
      /required/i,
    );
  });
});

describe("SecretsPanel — revoke", () => {
  afterEach(() => cleanup());

  it("emits onSecretRevoke with the matching secret_id on click", () => {
    const onRevoke = vi.fn();
    render(
      <SecretsPanel
        secrets={[SECRET_EBIRD]}
        caseId="01ABCDEFGHJKMNPQRSTVWX0050"
        onSecretAdd={vi.fn()}
        onSecretRevoke={onRevoke}
      />,
    );
    fireEvent.click(
      screen.getByTestId(`grace2-secret-revoke-${SECRET_EBIRD.secret_id}`),
    );
    expect(onRevoke).toHaveBeenCalledWith(SECRET_EBIRD.secret_id);
  });
});

// --- Tier2UnlockBadge --------------------------------------------------- //

describe("Tier2UnlockBadge — color flips with unlocked prop", () => {
  afterEach(() => cleanup());

  it("renders GREEN with check icon when unlocked", () => {
    render(<Tier2UnlockBadge provider="ebird" unlocked={true} />);
    const badge = screen.getByTestId("grace2-tier2-badge-ebird");
    expect(badge).toHaveAttribute("data-unlocked", "true");
    expect(badge.textContent ?? "").toMatch(/eBird/);
    // The check is now rendered via the shared icon module (IconCheck), so it
    // is an inline <svg>, not a raw '✓' unicode glyph.
    expect(badge.querySelector("svg")).not.toBeNull();
    expect(badge.textContent ?? "").not.toMatch(/✓/);
  });

  it("renders GRAY with 'key required' text when locked", () => {
    render(<Tier2UnlockBadge provider="iucn_red_list" unlocked={false} />);
    const badge = screen.getByTestId("grace2-tier2-badge-iucn_red_list");
    expect(badge).toHaveAttribute("data-unlocked", "false");
    expect(badge.textContent ?? "").toMatch(/IUCN Red List/);
    expect(badge.textContent ?? "").toMatch(/key required/);
  });

  it("renders nothing for non-Tier-2 providers (e.g. anthropic)", () => {
    const { container } = render(
      <Tier2UnlockBadge provider="anthropic" unlocked={true} />,
    );
    // No data-testid attribute => no rendered span
    expect(container.querySelector("[data-testid^=grace2-tier2-badge]")).toBeNull();
  });
});

// --- ws.ts secrets envelope wiring -------------------------------------- //

function makeHandlers(overrides: Partial<WsHandlers> = {}): WsHandlers {
  return {
    onStatus: vi.fn(),
    onAgentChunk: vi.fn(),
    onPipelineState: vi.fn(),
    onSessionState: vi.fn(),
    onError: vi.fn(),
    ...overrides,
  };
}

describe("ws.ts — secrets-list dispatch + secret-* outbound (job-0125)", () => {
  let originalWS: typeof WebSocket;

  beforeEach(() => {
    originalWS = globalThis.WebSocket;
  });

  afterEach(() => {
    globalThis.WebSocket = originalWS;
  });

  it("dispatches `secrets-list` envelope to the onSecretsList handler", () => {
    const fake = installFakeWebSocket();
    const onSecretsList: (p: SecretsListPayload) => void = vi.fn();
    const ws = new GraceWs("ws://test", makeHandlers({ onSecretsList }));
    ws.connect();
    const sock = fake.factory();
    sock.triggerOpen();

    // Simulate the agent sending a secrets-list frame.
    const env = {
      type: "secrets-list",
      id: "01ABCDEFGHJKMNPQRSTVWX0100",
      ts: "2026-06-08T14:00:00.000Z",
      session_id: "01ABCDEFGHJKMNPQRSTVWX0200",
      payload: {
        envelope_type: "secrets-list",
        secrets: [SECRET_EBIRD],
      },
    };
    sock.triggerMessage(JSON.stringify(env));

    const mockFn = onSecretsList as unknown as ReturnType<typeof vi.fn>;
    expect(mockFn).toHaveBeenCalledOnce();
    const received = mockFn.mock.calls[0]![0] as SecretsListPayload;
    expect(received.secrets).toHaveLength(1);
    expect(received.secrets[0]!.provider).toBe("ebird");

    ws.close();
  });

  it("sendSecretAdd emits a properly-shaped `secret-add` envelope", () => {
    const fake = installFakeWebSocket();
    const ws = new GraceWs("ws://test", makeHandlers());
    ws.connect();
    const sock = fake.factory();
    sock.triggerOpen();

    ws.sendSecretAdd({
      provider: "ebird",
      case_id: "01ABCDEFGHJKMNPQRSTVWX0050",
      label: "personal-key",
      key_value: "FAKE-KEY",
    });

    const matched = sock.sent
      .map((s) => JSON.parse(s) as { type: string; payload: unknown })
      .find((e) => e.type === "secret-add");
    expect(matched).toBeTruthy();
    const payload = matched!.payload as Record<string, unknown>;
    expect(payload.provider).toBe("ebird");
    expect(payload.case_id).toBe("01ABCDEFGHJKMNPQRSTVWX0050");
    expect(payload.label).toBe("personal-key");
    // Key value is on the wire (transient — server clears after vault write).
    expect(payload.key_value).toBe("FAKE-KEY");

    ws.close();
  });

  it("sendSecretRevoke emits a properly-shaped `secret-revoke` envelope", () => {
    const fake = installFakeWebSocket();
    const ws = new GraceWs("ws://test", makeHandlers());
    ws.connect();
    const sock = fake.factory();
    sock.triggerOpen();

    ws.sendSecretRevoke(SECRET_EBIRD.secret_id);

    const matched = sock.sent
      .map((s) => JSON.parse(s) as { type: string; payload: unknown })
      .find((e) => e.type === "secret-revoke");
    expect(matched).toBeTruthy();
    const payload = matched!.payload as Record<string, unknown>;
    expect(payload.secret_id).toBe(SECRET_EBIRD.secret_id);

    ws.close();
  });
});
