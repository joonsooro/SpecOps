import { describe, expect, it } from "vitest";
import { INITIAL_RUNWAY_DEPTH, formulationEnabled, proposalControlPayload, turnAnalysisOutcomeMessage, turnSubmissionEnabled, typedResponsePayload, workshopStartEnabled, zeroRunwayMessage } from "./uiModel";

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

  it("keeps drafting available while only a completed prior analysis unlocks Send", () => {
    expect(turnSubmissionEnabled("READY", null)).toBe(true);
    expect(turnSubmissionEnabled("ANALYSIS_PENDING", null)).toBe(false);
    expect(turnSubmissionEnabled("ANALYSIS_FAILED", null)).toBe(false);
    expect(turnSubmissionEnabled("READY", "send")).toBe(false);
  });

  it("shows wait copy only when zero runway has real Analyzer work pending", () => {
    expect(zeroRunwayMessage(1, "ANALYSIS_PENDING")).toBeNull();
    expect(zeroRunwayMessage(0, "ANALYSIS_PENDING")).toBe(
      "No questions are available right now. The analyzer is checking for new ambiguities. This may take a moment.",
    );
    expect(zeroRunwayMessage(0, "READY")).toBe(
      "No more questions are available. Review any proposals, then finish the workshop.",
    );
  });

  it("explains a completed turn that needs clarification instead of a proposal", () => {
    expect(turnAnalysisOutcomeMessage({
      committedTurnCount: 2,
      status: "READY",
      pendingProposalCount: 0,
      committedProposalCount: 1,
      hasNextQuestion: true,
    })).toBe(
      "Analysis complete. No decision proposal is waiting for review. Luna needs another clarification. Continue with the next question, or finish with 1 confirmed decision.",
    );
    expect(turnAnalysisOutcomeMessage({
      committedTurnCount: 2,
      status: "READY",
      pendingProposalCount: 1,
      committedProposalCount: 1,
      hasNextQuestion: true,
    })).toBeNull();
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
