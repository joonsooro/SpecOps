export type ConversationPhase = "WORKSHOP" | "HANDOFF_READY" | "COMPLETE";
export type ProposalIntent = "CONFIRM" | "EDIT" | "REJECT";
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
