// GRACE-2 web — RegionPickerCard unit tests (region-disambiguation flow;
// state-bbox-fallback narrowing).
//
// Verifies the inline chat card mirrors the agent's region-choice-request:
//   1. Renders the honest prompt (message) + state name in the header.
//   2. Renders one list row per candidate county.
//   3. Clicking a row → onPickRegion(candidate) with the EXACT candidate
//      (so the consumer echoes selected_region_id + selected_bbox verbatim).
//   4. Hovering a row → onHoverRegion(region_id); leaving → onHoverRegion(null).
//   5. "Use whole state" button → onUseWholeState().
//   6. Bus-synced hoveredRegionId / selectedRegionId highlight the matching row
//      (a MAP hover/tap reflects here).
//   7. Empty candidates → only the whole-state default + an honest no-candidates
//      note (degrade path).
//   8. Resolved="region" folds to a compact "Narrowed to <County>" summary;
//      resolved="whole_state" folds to "Using whole state of <State>". The full
//      picker (list + whole-state button) is gone (cannot re-submit).

import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { RegionPickerCard } from "./RegionPickerCard";
import { RegionCandidate, RegionChoiceRequestPayload } from "../contracts";

const REQ_ID = "01HJREGIONCHOICE0000000001";

function candidate(
  region_id: string,
  name: string,
  bbox: [number, number, number, number],
): RegionCandidate {
  return { region_id, name, bbox, admin_level: "county" };
}

function makeRequest(
  partial: Partial<RegionChoiceRequestPayload> = {},
): RegionChoiceRequestPayload {
  return {
    envelope_type: "region-choice-request",
    request_id: partial.request_id ?? REQ_ID,
    state_name: partial.state_name ?? "Florida",
    state_code: partial.state_code ?? "FL",
    state_bbox: partial.state_bbox ?? [-87.6, 24.5, -80.0, 31.0],
    candidates:
      partial.candidates ?? [
        candidate("county-12071", "Lee County", [-82.3, 26.3, -81.6, 26.8]),
        candidate("county-12021", "Collier County", [-81.8, 25.8, -81.0, 26.4]),
        candidate("county-12099", "Palm Beach County", [-80.9, 26.3, -80.0, 27.0]),
      ],
    default_action: "use_whole_state",
    message:
      partial.message ??
      "'south Florida' isn't a precise place — pick an area in Florida, or use the whole state.",
  };
}

function noop(): void {}

const baseProps = {
  onHoverRegion: noop,
  onPickRegion: noop,
  onUseWholeState: noop,
};

describe("RegionPickerCard — active picker", () => {
  it("renders the honest prompt + state name + one row per candidate", () => {
    const req = makeRequest();
    render(<RegionPickerCard request={req} {...baseProps} />);
    expect(screen.getByTestId(`region-picker-title-${REQ_ID}`)).toHaveTextContent(
      "Florida",
    );
    expect(
      screen.getByTestId(`region-picker-message-${REQ_ID}`),
    ).toHaveTextContent("isn't a precise place");
    const list = screen.getByTestId(`region-picker-list-${REQ_ID}`);
    expect(list).toBeInTheDocument();
    expect(
      screen.getByTestId(`region-picker-row-${REQ_ID}-county-12071`),
    ).toHaveTextContent("Lee County");
    expect(
      screen.getByTestId(`region-picker-row-${REQ_ID}-county-12021`),
    ).toHaveTextContent("Collier County");
    expect(
      screen.getByTestId(`region-picker-row-${REQ_ID}-county-12099`),
    ).toHaveTextContent("Palm Beach County");
  });

  it("clicking a row calls onPickRegion with the EXACT candidate", () => {
    const req = makeRequest();
    const onPickRegion = vi.fn();
    render(
      <RegionPickerCard request={req} {...baseProps} onPickRegion={onPickRegion} />,
    );
    fireEvent.click(
      screen.getByTestId(`region-picker-row-${REQ_ID}-county-12021`),
    );
    expect(onPickRegion).toHaveBeenCalledTimes(1);
    const picked = onPickRegion.mock.calls[0]![0] as RegionCandidate;
    expect(picked.region_id).toBe("county-12021");
    expect(picked.bbox).toEqual([-81.8, 25.8, -81.0, 26.4]);
  });

  it("hovering a row reports the region_id; leaving reports null", () => {
    const req = makeRequest();
    const onHoverRegion = vi.fn();
    render(
      <RegionPickerCard request={req} {...baseProps} onHoverRegion={onHoverRegion} />,
    );
    const row = screen.getByTestId(`region-picker-row-${REQ_ID}-county-12071`);
    fireEvent.mouseEnter(row);
    expect(onHoverRegion).toHaveBeenLastCalledWith("county-12071");
    fireEvent.mouseLeave(row);
    expect(onHoverRegion).toHaveBeenLastCalledWith(null);
  });

  it("'Use whole state' button calls onUseWholeState", () => {
    const req = makeRequest();
    const onUseWholeState = vi.fn();
    render(
      <RegionPickerCard
        request={req}
        {...baseProps}
        onUseWholeState={onUseWholeState}
      />,
    );
    const btn = screen.getByTestId(`region-picker-whole-state-${REQ_ID}`);
    expect(btn).toHaveTextContent("Use whole state of Florida");
    fireEvent.click(btn);
    expect(onUseWholeState).toHaveBeenCalledTimes(1);
  });

  it("bus-synced hovered/selected ids highlight the matching rows (map → card sync)", () => {
    const req = makeRequest();
    render(
      <RegionPickerCard
        request={req}
        {...baseProps}
        hoveredRegionId="county-12071"
        selectedRegionId="county-12099"
      />,
    );
    expect(
      screen
        .getByTestId(`region-picker-row-${REQ_ID}-county-12071`)
        .getAttribute("data-active"),
    ).toBe("true");
    expect(
      screen
        .getByTestId(`region-picker-row-${REQ_ID}-county-12099`)
        .getAttribute("data-selected"),
    ).toBe("true");
    // An unrelated row carries neither state.
    const other = screen.getByTestId(`region-picker-row-${REQ_ID}-county-12021`);
    expect(other.getAttribute("data-active")).toBe("false");
    expect(other.getAttribute("data-selected")).toBe("false");
  });

  it("empty candidates → no list, honest no-candidates note + whole-state default", () => {
    const req = makeRequest({ candidates: [] });
    render(<RegionPickerCard request={req} {...baseProps} />);
    expect(
      screen.queryByTestId(`region-picker-list-${REQ_ID}`),
    ).not.toBeInTheDocument();
    expect(
      screen.getByTestId(`region-picker-no-candidates-${REQ_ID}`),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId(`region-picker-whole-state-${REQ_ID}`),
    ).toBeInTheDocument();
  });
});

describe("RegionPickerCard — resolved (folded) state", () => {
  it("resolved='region' folds to a compact 'Narrowed to <County>' summary", () => {
    const req = makeRequest();
    render(
      <RegionPickerCard
        request={req}
        resolved="region"
        resolvedRegionId="county-12071"
        {...baseProps}
      />,
    );
    const card = screen.getByTestId(`region-picker-card-${REQ_ID}`);
    expect(card.getAttribute("data-resolved")).toBe("region");
    expect(card.getAttribute("data-variant")).toBe("compact");
    expect(
      screen.getByTestId(`region-picker-resolved-${REQ_ID}`),
    ).toHaveTextContent("Narrowed to Lee County");
    // The full picker is gone — no list, no whole-state button (cannot re-submit).
    expect(
      screen.queryByTestId(`region-picker-list-${REQ_ID}`),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId(`region-picker-whole-state-${REQ_ID}`),
    ).not.toBeInTheDocument();
  });

  it("resolved='whole_state' folds to 'Using whole state of <State>'", () => {
    const req = makeRequest();
    render(
      <RegionPickerCard
        request={req}
        resolved="whole_state"
        {...baseProps}
      />,
    );
    expect(
      screen.getByTestId(`region-picker-resolved-${REQ_ID}`),
    ).toHaveTextContent("Using whole state of Florida");
  });

  it("the compact card re-expands the read-only detail via the chevron", () => {
    const req = makeRequest();
    render(
      <RegionPickerCard
        request={req}
        resolved="whole_state"
        {...baseProps}
      />,
    );
    expect(
      screen.queryByTestId(`region-picker-detail-${REQ_ID}`),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId(`region-picker-expand-${REQ_ID}`));
    expect(
      screen.getByTestId(`region-picker-detail-${REQ_ID}`),
    ).toHaveTextContent("isn't a precise place");
  });
});
