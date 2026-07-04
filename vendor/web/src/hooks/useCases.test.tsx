// job-0273: the auto-create case-open → case-list race.
//
// Observed live (Playwright WS capture, 2026-06-10): the server emits
// case-open 27ms BEFORE the refreshed case-list. With a non-empty rail, the
// tombstone guard saw activeCaseId pointing at a Case not yet in `cases`
// and bounced the user back to root — while Chat's adoption had already
// cleared the root stream, leaving a fully empty chat for the whole turn.
// onCaseOpen now optimistically upserts the envelope's CaseSummary.

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  CaseSessionState,
  CaseSummary,
  ProjectLayerSummary,
} from "../contracts";
import { LayerCache } from "../lib/layer_cache";
import { coldRenderableRasterSummaries, useCases } from "./useCases";

function summary(id: string, title: string): CaseSummary {
  return {
    case_id: id,
    title,
    created_at: "2026-06-11T00:00:00Z",
    updated_at: "2026-06-11T00:00:00Z",
    status: "active",
  } as CaseSummary;
}

function session(id: string, title: string): CaseSessionState {
  return {
    case: summary(id, title),
    chat_history: [],
    loaded_layers: [],
  } as unknown as CaseSessionState;
}

// A cold-renderable RASTER summary: its `uri` is the resolved TiTiler
// /cog/tiles/.../{z}/{x}/{y}.png?... template served by the always-on
// TiTiler+CloudFront box, so it paints with the agent box asleep.
function rasterLayer(
  id: string,
  uri = `https://d125yfbyjrpbre.cloudfront.net/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3://runs/${id}.tif`,
): ProjectLayerSummary {
  return {
    layer_id: id,
    name: `Raster ${id}`,
    layer_type: "raster",
    uri,
    visible: true,
    opacity: 1,
    z_index: 0,
  } as ProjectLayerSummary;
}

function vectorLayer(id: string, uri = `s3://runs/${id}.fgb`): ProjectLayerSummary {
  return {
    layer_id: id,
    name: `Vector ${id}`,
    layer_type: "vector",
    uri,
    visible: true,
    opacity: 1,
    z_index: 1,
  } as ProjectLayerSummary;
}

const noopSend = () => {};

describe("useCases auto-create race (job-0273)", () => {
  it("keeps activeCaseId when case-open precedes the refreshed case-list", () => {
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: noopSend as never, isSignedIn: false }),
    );

    // Rail already holds an older Case (the guard's arming condition).
    act(() => {
      result.current.onCaseList({ cases: [summary("01OLD", "Old Case")] });
    });

    // Auto-create: case-open arrives FIRST, before the refreshed list.
    act(() => {
      result.current.onCaseOpen({
        session_state: session("01NEW", "Fresh Auto Case"),
      });
    });

    // Pre-fix: the tombstone effect bounced this back to null.
    expect(result.current.activeCaseId).toBe("01NEW");
    expect(
      result.current.cases.some((c) => c.case_id === "01NEW"),
    ).toBe(true);

    // The authoritative case-list canonicalizes without disturbing active.
    act(() => {
      result.current.onCaseList({
        cases: [summary("01OLD", "Old Case"), summary("01NEW", "Fresh Auto Case")],
      });
    });
    expect(result.current.activeCaseId).toBe("01NEW");
  });

  it("still clears activeCaseId when the active Case is tombstoned", () => {
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: noopSend as never, isSignedIn: false }),
    );
    act(() => {
      result.current.onCaseList({ cases: [summary("01A", "A")] });
    });
    act(() => {
      result.current.onCaseOpen({ session_state: session("01A", "A") });
    });
    expect(result.current.activeCaseId).toBe("01A");

    // Delete flow: refreshed list no longer contains the active Case.
    act(() => {
      result.current.onCaseList({ cases: [summary("01B", "B")] });
    });
    expect(result.current.activeCaseId).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// PRIMARY - cold-open RENDER: a box-OFF case-open whose snapshot carries a
// populated `session_state.loaded_layers` (the cold-renderable array: raster
// TiTiler tile templates + inline vectors) must reach the map paintable.
//
// The live contradiction (GROUND TRUTH 2026-06-22): NATE reports box-OFF
// case-open shows NO layers, but the real Ellicott snapshot's
// `session_state.loaded_layers` is fully cold-renderable. This pins the WEB
// cold-open path: onCaseOpen exposes the snapshot's loaded_layers verbatim on
// `activeSession.loaded_layers` (which App.tsx's rehydration effect pushes onto
// the map channel with replace_layers:true), and that frame, run through the
// SAME LayerCache.mergeSnapshot the bus subscriber uses, yields a PAINTABLE
// raster frame (tile template intact). So if the snapshot carries the layers,
// the web paints them cold - confirming the no-layers symptom is the
// server-side stale/lost-write race, NOT a web cold-render drop.
// ---------------------------------------------------------------------------
describe("useCases cold-open render (PRIMARY - box-off loaded_layers paint)", () => {
  it("exposes the cold snapshot's loaded_layers on activeSession verbatim", () => {
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: noopSend as never, isSignedIn: false }),
    );

    const raster = rasterLayer("01RAS");
    const vector = vectorLayer("01VEC");
    const coldSnapshot = {
      case: summary("01COLD", "Ellicott City"),
      chat_history: [],
      // The Ellicott snapshot's COLD-RENDERABLE array.
      loaded_layers: [raster, vector],
    } as unknown as CaseSessionState;

    act(() => {
      result.current.onCaseOpen({ session_state: coldSnapshot });
    });

    expect(result.current.activeCaseId).toBe("01COLD");
    // The layers App.tsx forwards to the map are read off activeSession - they
    // must be the snapshot's loaded_layers untouched (incl. the raster tile
    // template that paints with the agent box asleep).
    const layers = result.current.activeSession?.loaded_layers ?? [];
    expect(layers).toHaveLength(2);
    expect(layers.map((l) => l.layer_id)).toEqual(["01RAS", "01VEC"]);
  });

  it("a cold loaded_layers frame produces a paintable raster map frame (mergeSnapshot)", () => {
    // Mirror App.tsx's cold-open dispatch end-to-end: onCaseOpen sets
    // activeSession, the rehydration effect pushes activeSession.loaded_layers
    // with replace_layers:true, and the bus subscriber runs
    // LayerCache.mergeSnapshot. We assert the RASTER survives that merge into
    // the rendered list with its TiTiler tile template intact (cold-renderable).
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: noopSend as never, isSignedIn: false }),
    );
    const raster = rasterLayer("01RAS");
    const coldSnapshot = {
      case: summary("01COLD", "Ellicott City"),
      chat_history: [],
      loaded_layers: [raster],
    } as unknown as CaseSessionState;

    act(() => {
      result.current.onCaseOpen({ session_state: coldSnapshot });
    });

    const cache = new LayerCache({
      backend: { load: async () => ({}), save: async () => {} },
    });
    const caseId = result.current.activeCaseId!;
    cache.activeCaseId = caseId;
    // App.tsx pushes the OPEN-case frame with replace_layers:true => an
    // AUTHORITATIVE replace. On a fresh (cold) Case the cache has nothing
    // tracked, so the layer is ADDED (the #158 empty-frame guard only blocks an
    // EMPTY authoritative frame - a layer-bearing one always paints).
    const rendered = cache.mergeSnapshot(
      caseId,
      result.current.activeSession?.loaded_layers ?? [],
      { authoritativeReplace: true },
    );

    expect(rendered).toHaveLength(1);
    expect(rendered[0]!.layer_id).toBe("01RAS");
    // The cold-renderable proof: the raster URI is an http(s) TiTiler tile
    // template, so MapLibre can register a raster source with the agent asleep.
    expect(rendered[0]!.uri).toMatch(/^https:\/\/.*\/cog\/tiles\/.*\{z\}\/\{x\}\/\{y\}\.png/);
  });
});

// ---------------------------------------------------------------------------
// SECONDARY - cold-LIST raster fallback (defense in depth, coldview FIX C).
// When the per-case snapshot is missing / stale / 404, the cold case-LIST
// envelope still carries each Case's `loaded_layer_summaries`. onCaseList
// surfaces the OPEN Case's cold-renderable RASTER summaries to the
// onListLayerSummaries sink so they still paint (non-authoritative top-up).
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// #170 AOI-first manual case-creation: createCase(title?, bbox?) forwards the
// bbox into the create-command args when present. The no-bbox path stays
// byte-identical (args carries only the title hint, or {}).
// ---------------------------------------------------------------------------
describe("useCases createCase bbox forwarding (#170)", () => {
  it("forwards the bbox into the create args when supplied", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    act(() => {
      result.current.createCase(null, [-85.31, 35.04, -85.3, 35.05]);
    });
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0]).toEqual([
      "create",
      null,
      { bbox: [-85.31, 35.04, -85.3, 35.05] },
    ]);
  });

  it("carries both title and bbox when both are supplied", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    act(() => {
      result.current.createCase("  Ellicott  ", [10, 20, 11, 21]);
    });
    expect(send.mock.calls[0]).toEqual([
      "create",
      null,
      { title: "Ellicott", bbox: [10, 20, 11, 21] },
    ]);
  });

  it("the no-bbox path is byte-identical to before (empty args)", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    act(() => {
      result.current.createCase();
    });
    expect(send.mock.calls[0]).toEqual(["create", null, {}]);
  });

  it("no-bbox with a title carries only the title (no bbox key)", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send, isSignedIn: true }),
    );
    act(() => {
      result.current.createCase("My Case");
    });
    expect(send.mock.calls[0]).toEqual(["create", null, { title: "My Case" }]);
  });
});

describe("coldRenderableRasterSummaries (cold-list filter)", () => {
  it("keeps raster layers with an http(s) tile-template uri", () => {
    const kept = coldRenderableRasterSummaries([
      rasterLayer("R1"),
      rasterLayer(
        "R2",
        "http://example.com/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=x",
      ),
    ]);
    expect(kept.map((l) => l.layer_id)).toEqual(["R1", "R2"]);
  });

  it("drops vectors, bare object-store rasters, and empty/garbage", () => {
    const kept = coldRenderableRasterSummaries([
      vectorLayer("V1"), // vector - needs inline geojson (J2), not the list
      rasterLayer("R_S3", "s3://runs/R_S3.tif"), // bare handle - not browser-readable
      rasterLayer("R_GS", "gs://runs/R_GS.tif"), // bare handle - not browser-readable
      rasterLayer("R_EMPTY", ""), // empty uri
    ]);
    expect(kept).toHaveLength(0);
  });

  it("tolerates undefined / null / non-array input", () => {
    expect(coldRenderableRasterSummaries(undefined)).toEqual([]);
    expect(coldRenderableRasterSummaries(null)).toEqual([]);
    expect(
      coldRenderableRasterSummaries(undefined as unknown as ProjectLayerSummary[]),
    ).toEqual([]);
  });
});

describe("useCases cold-list raster fallback (SECONDARY - coldview FIX C)", () => {
  function openCase(
    result: { current: ReturnType<typeof useCases> },
    id: string,
  ): void {
    // Open a Case so the OPEN-case selection in onCaseList has a target.
    act(() => {
      result.current.onCaseOpen({ session_state: session(id, id) });
    });
  }

  it("surfaces the OPEN Case's cold-renderable rasters to the sink", () => {
    const sink = vi.fn();
    const { result } = renderHook(() =>
      useCases({
        sendCaseCommand: noopSend as never,
        isSignedIn: false,
        onListLayerSummaries: sink,
      }),
    );
    openCase(result, "01OPEN");

    const openSummary = summary("01OPEN", "Open Case");
    // The case-list carries loaded_layer_summaries: a cold-renderable raster, a
    // bare-handle raster (dropped), and a vector (dropped - J2's job).
    openSummary.loaded_layer_summaries = [
      rasterLayer("RAS_OK"),
      rasterLayer("RAS_S3", "s3://runs/RAS_S3.tif"),
      vectorLayer("VEC"),
    ];

    act(() => {
      result.current.onCaseList(
        { cases: [openSummary, summary("01OTHER", "Other")] },
        true,
      );
    });

    expect(sink).toHaveBeenCalledTimes(1);
    const [caseIdArg, rastersArg] = sink.mock.calls[0]!;
    expect(caseIdArg).toBe("01OPEN");
    expect(rastersArg.map((l: ProjectLayerSummary) => l.layer_id)).toEqual([
      "RAS_OK",
    ]);
  });

  it("does NOT call the sink when no Case is open (Cases root)", () => {
    const sink = vi.fn();
    const { result } = renderHook(() =>
      useCases({
        sendCaseCommand: noopSend as never,
        isSignedIn: false,
        onListLayerSummaries: sink,
      }),
    );
    const s = summary("01A", "A");
    s.loaded_layer_summaries = [rasterLayer("RAS_OK")];
    act(() => {
      result.current.onCaseList({ cases: [s] }, true);
    });
    expect(sink).not.toHaveBeenCalled();
  });

  it("does NOT call the sink when the open Case has no cold-renderable rasters", () => {
    const sink = vi.fn();
    const { result } = renderHook(() =>
      useCases({
        sendCaseCommand: noopSend as never,
        isSignedIn: false,
        onListLayerSummaries: sink,
      }),
    );
    openCase(result, "01OPEN");
    const openSummary = summary("01OPEN", "Open Case");
    // Only a vector + a bare-handle raster - nothing cold-renderable.
    openSummary.loaded_layer_summaries = [
      vectorLayer("VEC"),
      rasterLayer("RAS_S3", "s3://runs/RAS_S3.tif"),
    ];
    act(() => {
      result.current.onCaseList({ cases: [openSummary] }, true);
    });
    expect(sink).not.toHaveBeenCalled();
  });

  it("does NOT regress the warm path - no sink wired => no extra behavior", () => {
    // The warm path passes no onListLayerSummaries; onCaseList must be a
    // byte-identical no-op beyond the rail reconcile (no throw, rail updates).
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: noopSend as never, isSignedIn: false }),
    );
    const s = summary("01OPEN", "Open Case");
    s.loaded_layer_summaries = [rasterLayer("RAS_OK")];
    act(() => {
      result.current.onCaseOpen({ session_state: session("01OPEN", "Open Case") });
    });
    act(() => {
      result.current.onCaseList({ cases: [s] }, true);
    });
    // Rail reconciled normally; no crash from the absent sink.
    expect(result.current.cases.map((c) => c.case_id)).toEqual(["01OPEN"]);
  });
});

// ---------------------------------------------------------------------------
// BUG 1 (late spinner): `casesSettled` must start FALSE (so CasesPanel shows
// its spinner immediately on first paint, before any frame) and flip TRUE on
// the FIRST case-list frame of any source (live or cold authoritative) -
// INCLUDING a genuinely-empty authoritative list (settled-to-zero shows the
// empty stub, not a forever-spinner).
// ---------------------------------------------------------------------------
describe("useCases casesSettled (BUG 1 - immediate spinner)", () => {
  it("starts FALSE so the spinner shows before the first list arrives", () => {
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: noopSend as never, isSignedIn: false }),
    );
    expect(result.current.casesSettled).toBe(false);
  });

  it("flips TRUE on the first NON-empty live case-list frame", () => {
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: noopSend as never, isSignedIn: false }),
    );
    act(() => {
      result.current.onCaseList({ cases: [summary("01A", "A")] });
    });
    expect(result.current.casesSettled).toBe(true);
  });

  it("flips TRUE on a genuinely-EMPTY authoritative list (settled-to-zero)", () => {
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: noopSend as never, isSignedIn: false }),
    );
    // An authoritative empty cold-fetch is the genuine "zero cases" answer: it
    // must SETTLE (empty stub), never leave the spinner running forever.
    act(() => {
      result.current.onCaseList({ cases: [] }, true);
    });
    expect(result.current.casesSettled).toBe(true);
    expect(result.current.cases).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// BUG 2 (delete + stale-reappear trap): deleteCase removes the Case AND
// tombstones its id so a subsequent STALE case-list frame (a 25s keepalive
// resume / a fresh-resume case-list that races the server soft-delete write
// and still carries the just-deleted Case) cannot RESURRECT it in the rail.
// An AUTHORITATIVE server list that re-affirms the Case clears the tombstone
// (the undo / legitimate-reappearance path).
// ---------------------------------------------------------------------------
describe("useCases deleteCase tombstone (BUG 2 - no stale resurrection)", () => {
  it("deleteCase emits delete + optimistically drops the Case from the rail", () => {
    const send = vi.fn();
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: send as never, isSignedIn: true }),
    );
    act(() => {
      result.current.onCaseList(
        { cases: [summary("01A", "A"), summary("01B", "B")] },
        true,
      );
    });
    act(() => {
      result.current.deleteCase("01A");
    });
    // Sent the delete command + removed the Case from the local rail at once.
    expect(send).toHaveBeenCalledWith("delete", "01A", {});
    expect(result.current.cases.map((c) => c.case_id)).toEqual(["01B"]);
  });

  it("a STALE non-authoritative case-list still carrying the deleted Case does NOT resurrect it", () => {
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: noopSend as never, isSignedIn: true }),
    );
    act(() => {
      result.current.onCaseList(
        { cases: [summary("01A", "A"), summary("01B", "B")] },
        true,
      );
    });
    act(() => {
      result.current.deleteCase("01A");
    });
    expect(result.current.cases.map((c) => c.case_id)).toEqual(["01B"]);

    // A keepalive / fresh-resume case-list that RACED the server delete write
    // still carries 01A. Non-authoritative => the tombstone filters 01A out so
    // it can NOT reappear in the rail.
    act(() => {
      result.current.onCaseList({
        cases: [summary("01A", "A"), summary("01B", "B")],
      });
    });
    expect(result.current.cases.map((c) => c.case_id)).toEqual(["01B"]);
  });

  it("a STALE last-case keepalive does NOT resurrect the deleted final Case", () => {
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: noopSend as never, isSignedIn: true }),
    );
    act(() => {
      result.current.onCaseList({ cases: [summary("01ONLY", "Only")] }, true);
    });
    act(() => {
      result.current.deleteCase("01ONLY");
    });
    expect(result.current.cases).toEqual([]);
    // A racing non-authoritative frame still carrying the just-deleted last Case.
    act(() => {
      result.current.onCaseList({ cases: [summary("01ONLY", "Only")] });
    });
    expect(result.current.cases).toEqual([]);
  });

  it("a late onCaseOpen for the deleted Case does NOT re-add it to the rail", () => {
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: noopSend as never, isSignedIn: true }),
    );
    act(() => {
      result.current.onCaseList({ cases: [summary("01A", "A")] }, true);
    });
    act(() => {
      result.current.deleteCase("01A");
    });
    expect(result.current.cases).toEqual([]);
    // A queued `select` that round-trips AFTER the delete emits case-open for
    // 01A; the optimistic upsert must be suppressed by the tombstone.
    act(() => {
      result.current.onCaseOpen({ session_state: session("01A", "A") });
    });
    expect(result.current.cases.some((c) => c.case_id === "01A")).toBe(false);
  });

  it("an AUTHORITATIVE list still carrying the Case KEEPS it suppressed (box-off stale cold-list, B-CLIENT NATE 2026-06-26)", () => {
    // CONTRACT CHANGE (B-CLIENT, NATE 2026-06-26): the earlier behavior cleared
    // the tombstone whenever an authoritative list re-carried the id (treating
    // authoritative == undo proof). That was the box-OFF resurrection bug: the
    // `delete` command queues + never reaches the asleep server, while the cold
    // /case-list fetch is dispatched isAuthoritative=true even though it is STALE
    // (pre-delete) -> clearing on it re-added the just-deleted Case. We now KEEP
    // the tombstone (never clear it from a plain authoritative list that still
    // carries the id); it is cleared only on POSITIVE proof of un-delete or a
    // bounded TTL (neither wired yet).
    const { result } = renderHook(() =>
      useCases({ sendCaseCommand: noopSend as never, isSignedIn: true }),
    );
    act(() => {
      result.current.onCaseList({ cases: [summary("01A", "A")] }, true);
    });
    act(() => {
      result.current.deleteCase("01A");
    });
    expect(result.current.cases).toEqual([]);
    // The STALE box-off AUTHORITATIVE cold list still carries 01A -> stays gone.
    act(() => {
      result.current.onCaseList({ cases: [summary("01A", "A")] }, true);
    });
    expect(result.current.cases).toEqual([]);
  });
});
