import { describe, expect, it } from "vitest";
import { INITIAL_RUNWAY_DEPTH, formulationEnabled, proposalControlPayload, typedResponsePayload, workshopStartEnabled } from "./uiModel";

describe("Workshop server-projection UI rules", () => {
  it("fails closed until the server admits the exact four-question runway", () => {
    expect(workshopStartEnabled("VALIDATING_INITIAL_RUNWAY", INITIAL_RUNWAY_DEPTH)).toBe(false);
    expect(workshopStartEnabled("READY", INITIAL_RUNWAY_DEPTH - 1)).toBe(true);
    expect(workshopStartEnabled("READY", INITIAL_RUNWAY_DEPTH)).toBe(true);
  });

  it("freezes formulation outside an unlocked Workshop", () => {
    expect(formulationEnabled("WORKSHOP", false)).toBe(true);
    expect(formulationEnabled("WORKSHOP", true)).toBe(false);
    expect(formulationEnabled("HANDOFF_READY", false)).toBe(false);
    expect(formulationEnabled("COMPLETE", false)).toBe(false);
  });

  it("keeps proposal control shapes exact", () => {
    const binding = { proposal_ref: "patch-1", proposal_version: 2, base_case_revision: 7, payload_hash: "a".repeat(64) };
    expect(proposalControlPayload("action-1", binding)).toEqual({
      client_action_id: "action-1", binding,
    });
  });

  it("maps chat to the authoritative typed-response DTO without channel authority", () => {
    expect(typedResponsePayload(
      "submission-1",
      { question_id: "question-1", question_version: 3 },
      "Exact response",
    )).toEqual({
      client_submission_id: "submission-1",
      question_id: "question-1",
      expected_question_version: 3,
      text: "Exact response",
      correction_of_response_id: null,
      edit_target: null,
    });
  });
});
