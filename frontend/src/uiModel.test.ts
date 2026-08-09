import { describe, expect, it } from "vitest";
import { formulationEnabled, proposalControlPayload, reduceLiveProjection } from "./uiModel";

describe("Workshop server-projection UI rules", () => {
  it("freezes formulation outside an unlocked Workshop", () => {
    expect(formulationEnabled("WORKSHOP", false)).toBe(true);
    expect(formulationEnabled("WORKSHOP", true)).toBe(false);
    expect(formulationEnabled("HANDOFF_READY", false)).toBe(false);
    expect(formulationEnabled("COMPLETE", false)).toBe(false);
  });

  it("keeps proposal control shapes exact", () => {
    expect(proposalControlPayload("CONFIRM", "patch-1", "ignored")).toEqual({
      intent: "CONFIRM", proposal_ref: "patch-1", edit_instruction: null, acknowledgement: "Confirmed",
    });
    expect(proposalControlPayload("EDIT", "patch-1", "Keep UTF-8 explicit")).toEqual({
      intent: "EDIT", proposal_ref: "patch-1", edit_instruction: "Keep UTF-8 explicit", acknowledgement: null,
    });
    expect(proposalControlPayload("REJECT", "patch-1", "ignored")).toEqual({
      intent: "REJECT", proposal_ref: "patch-1", edit_instruction: null, acknowledgement: null,
    });
  });

  it("treats partial transcript as display-only projection state", () => {
    const initial = { callState: "LISTENING", partial: "", failure: null };
    const partial = reduceLiveProjection(initial, { type: "TRANSCRIPT_PARTIAL", text: "not final" });
    expect(partial.partial).toBe("not final");
    expect(reduceLiveProjection(partial, { type: "TRANSCRIPT_FINAL" }).partial).toBe("");
    expect(reduceLiveProjection(initial, { type: "ERROR", code: "DEVICE_LOST" }).failure).toContain("DEVICE_LOST");
  });
});
