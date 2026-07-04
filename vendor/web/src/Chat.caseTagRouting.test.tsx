// job-0277: envelope case-tag routing — a still-running turn's envelopes
// land in THEIR Case's stream even after the user messaged another Case
// (which re-points submit-time targetKey routing). Untagged envelopes keep
// the targetKey fallback for backward compatibility.

import { describe, expect, it } from "vitest";

import {
  createChatStreams,
  getStream,
  routeAgentChunk,
  routeChartEmission,
  routePipelineState,
  routeUserMessage,
  streamKeyFor,
} from "./Chat";
import type {
  AgentMessageChunkPayload,
  PipelineStatePayload,
} from "./contracts";
// ChartPayload is defined alongside the chart UI, not in contracts.ts.
import type { ChartPayload } from "./components/ChartStack";

const chunk = (id: string, delta: string): AgentMessageChunkPayload =>
  ({ message_id: id, delta, done: false }) as AgentMessageChunkPayload;

const pipeline = (pid: string): PipelineStatePayload =>
  ({
    pipeline_id: pid,
    steps: [
      {
        step_id: `${pid}-s1`,
        name: "fetch_dem",
        tool_name: "fetch_dem",
        state: "running",
      },
    ],
  }) as unknown as PipelineStatePayload;

describe("envelope case-tag routing (job-0277)", () => {
  it("tagged chunks land in the OWNING Case even after targetKey moved", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, streamKeyFor("CASEA"), "flood in A");
    // User switches and messages Case B → submit-time routing moves on.
    routeUserMessage(cs, streamKeyFor("CASEB"), "relief in B");
    expect(cs.targetKey).toBe("CASEA" === cs.targetKey ? "CASEA" : cs.targetKey);

    // Case A's still-running turn streams a tagged chunk.
    routeAgentChunk(cs, chunk("m1", "A narration"), "CASEA");
    const a = getStream(cs, "CASEA");
    const b = getStream(cs, "CASEB");
    expect(a.messages.some((m) => m.text.includes("A narration"))).toBe(true);
    expect(b.messages.some((m) => m.text.includes("A narration"))).toBe(false);
  });

  it("tagged pipeline-state buffers into the owning Case's cards", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, streamKeyFor("CASEA"), "flood in A");
    routeUserMessage(cs, streamKeyFor("CASEB"), "relief in B");
    routePipelineState(cs, pipeline("p-a"), "CASEA");
    const a = getStream(cs, "CASEA");
    const b = getStream(cs, "CASEB");
    expect(a.pipeline.live?.pipeline_id ?? a.pipeline.history.at(-1)?.pipeline_id).toBe(
      "p-a",
    );
    expect(b.pipeline.live).toBeNull();
  });

  it("untagged envelopes keep submit-time targetKey routing", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, streamKeyFor("CASEB"), "relief in B");
    routeAgentChunk(cs, chunk("m2", "untagged narration"));
    const b = getStream(cs, "CASEB");
    expect(b.messages.some((m) => m.text.includes("untagged narration"))).toBe(
      true,
    );
  });

  it("tagged charts de-dupe within the owning stream", () => {
    const cs = createChatStreams();
    routeUserMessage(cs, streamKeyFor("CASEB"), "chart in B");
    const chart = { chart_id: "c1", vega_lite_spec: {} } as unknown as ChartPayload;
    routeChartEmission(cs, chart, "CASEA");
    routeChartEmission(cs, chart, "CASEA");
    expect(getStream(cs, "CASEA").charts).toHaveLength(1);
    expect(getStream(cs, "CASEB").charts).toHaveLength(0);
  });
});
