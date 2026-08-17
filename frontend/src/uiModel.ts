export type ConversationPhase = "WORKSHOP" | "HANDOFF_READY" | "COMPLETE";
export type ProposalIntent = "CONFIRM" | "EDIT" | "REJECT";
export type TurnSubmissionStatus = "READY" | "ANALYSIS_PENDING" | "ANALYSIS_FAILED";
export const INITIAL_RUNWAY_DEPTH = 4;

export function workshopStartEnabled(phase: string, runwayDepth: number): boolean {
  // READY is a durable server fact that could only have been reached with the
  // exact four-question initial runway. Current depth may legitimately fall as
  // the active Workshop consumes admitted questions.
  return phase === "READY" && runwayDepth >= 0;
}

export function formulationEnabled(phase: ConversationPhase, revisionLocked: boolean): boolean {
  return phase === "WORKSHOP" && !revisionLocked;
}

export function turnSubmissionEnabled(status: TurnSubmissionStatus, busy: string | null): boolean {
  return status === "READY" && busy === null;
}

export function zeroRunwayMessage(runwayDepth: number, status: TurnSubmissionStatus): string | null {
  if (runwayDepth !== 0) return null;
  if (status === "ANALYSIS_PENDING") {
    return "No questions are available right now. The analyzer is checking for new ambiguities. This may take a moment.";
  }
  if (status === "ANALYSIS_FAILED") {
    return "No questions are available because analysis needs attention. You can still review proposals and finish the workshop.";
  }
  return "No more questions are available. Review any proposals, then finish the workshop.";
}

export function turnAnalysisOutcomeMessage({
  committedTurnCount,
  status,
  pendingProposalCount,
  committedProposalCount,
  hasNextQuestion,
}: {
  committedTurnCount: number;
  status: TurnSubmissionStatus;
  pendingProposalCount: number;
  committedProposalCount: number;
  hasNextQuestion: boolean;
}): string | null {
  if (
    committedTurnCount === 0
    || status !== "READY"
    || pendingProposalCount > 0
    || !hasNextQuestion
  ) return null;
  const finishSummary = committedProposalCount === 1
    ? "finish with 1 confirmed decision"
    : committedProposalCount > 1
      ? `finish with ${committedProposalCount} confirmed decisions`
      : "finish without another confirmed decision";
  return `Analysis complete. No decision proposal is waiting for review. Luna needs another clarification. Continue with the next question, or ${finishSummary}.`;
}

export function proposalControlPayload(
  clientActionId: string,
  binding: {
    proposal_ref: string;
    proposal_version: number;
    base_case_revision: number;
    payload_hash: string;
  },
) {
  return {
    client_action_id: clientActionId,
    binding,
  } as const;
}

export function typedResponsePayload(
  clientSubmissionId: string,
  question: { question_id: string; question_version: number },
  text: string,
  correctionOfResponseId: string | null = null,
) {
  return {
    client_submission_id: clientSubmissionId,
    question_id: question.question_id,
    expected_question_version: question.question_version,
    text,
    correction_of_response_id: correctionOfResponseId,
    edit_target: null,
  } as const;
}
