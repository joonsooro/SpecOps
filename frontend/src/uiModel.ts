export type ConversationPhase = "WORKSHOP" | "HANDOFF_READY" | "COMPLETE";
export type ProposalIntent = "CONFIRM" | "EDIT" | "REJECT";

export function workshopStartEnabled(phase: string, runwayDepth: number): boolean {
  // READY is a durable server fact that could only have been reached with the
  // exact six-question initial runway. Current depth may legitimately fall as
  // the active Workshop consumes admitted questions.
  return phase === "READY" && runwayDepth >= 0;
}

export function formulationEnabled(phase: ConversationPhase, revisionLocked: boolean): boolean {
  return phase === "WORKSHOP" && !revisionLocked;
}

export function proposalControlPayload(
  intent: ProposalIntent,
  proposalRef: string,
  editInstruction: string,
) {
  return {
    intent,
    proposal_ref: proposalRef,
    edit_instruction: intent === "EDIT" ? editInstruction : null,
    acknowledgement: intent === "CONFIRM" ? "Confirmed" : null,
  } as const;
}

export function reduceLiveProjection(
  state: { callState: string; partial: string; failure: string | null },
  event: Record<string, unknown>,
) {
  if (event.type === "CALL_STATE") return { ...state, callState: String(event.state) };
  if (event.type === "TRANSCRIPT_PARTIAL") return { ...state, partial: String(event.text ?? "") };
  if (event.type === "TRANSCRIPT_FINAL") return { ...state, partial: "" };
  if (event.type === "ERROR") return { ...state, failure: `Voice control failed: ${String(event.code)}` };
  return state;
}
