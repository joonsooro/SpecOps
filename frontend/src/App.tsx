import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { WorkshopPresenceClient } from "./presenceClient";
import { turnSubmissionEnabled, type TurnSubmissionStatus, zeroRunwayMessage } from "./uiModel";

type Bootstrap = {
  case_id: string;
  pm_actor_id: string;
  delegation_valid_from: string;
  delegation_valid_until: string;
  delegation_command_scope: string[];
  technical_source_lines: [number, string][];
};

type Dependency = {
  dependency_kind: string;
  entity_id: string | null;
  expected_version: number | null;
  source_set_hash: string | null;
};

type Question = {
  question_id: string;
  question_version: number;
  exact_text: string;
  reason: string;
  dependencies: Dependency[];
};

type SourceRef = {
  artifact_id: string;
  version: number;
  content_hash: string;
  location: { kind: "JSON_POINTER"; pointer: string };
};

type ResponseSnapshot = {
  response_id: string;
  session_id: string;
  question_id: string;
  question_version: number;
  turn_sequence: number;
  response_version: number;
  normalized_text: string;
  content_hash: string;
  final_source_ref: SourceRef;
  client_submission_id: string;
  correction_of_response_id: string | null;
  input_channel: "CHAT";
  channel_confirmation_receipt_id: null;
  created_at: string;
};

type ProposalBinding = {
  proposal_ref: string;
  proposal_version: number;
  base_case_revision: number;
  payload_hash: string;
};

type Proposal = {
  binding: ProposalBinding;
  status: "PENDING" | "EDIT_REQUESTED" | "SUPERSEDED" | "REJECTED" | "COMMITTED";
  view: {
    view_id: string;
    items: {
      handle: string;
      classification: string;
      exact_statement: string;
      rationale: string;
    }[];
  };
};

type WorkshopContext = {
  session_id: string;
  case_revision: number;
  workshop_state: "NOT_STARTED" | "ACTIVE" | "FINISHING" | "COMPLETED" | "BLOCKED";
  conversation_phase: "WORKSHOP" | "HANDOFF_READY" | "COMPLETE";
  session_card: { readiness: string; review_obligation: string };
  committed_turns: { question: Question; response: ResponseSnapshot }[];
  question_runway: { questions: Question[]; runway_depth: number };
  turn_submission_status: TurnSubmissionStatus;
  proposal_statuses: Proposal[];
  completion_status: "FINISHING_ANALYSIS" | "HANDOFF_READY" | "FINISH_FAILED" | null;
  generated_at: string;
};

type Preparation = {
  phase: "NOT_STARTED" | "UPLOADING_SOURCES" | "BOOTSTRAPPING" | "ADMITTING_GUIDANCE" | "READY" | "FAILED";
  message: string | null;
  delayed_message: string | null;
};

type PendingSubmission = {
  client_submission_id: string;
  question_id: string;
  expected_question_version: number;
  text: string;
  correction_of_response_id: string | null;
  edit_target: ProposalBinding | null;
};

const shortId = (value: string) => `${value.slice(0, 8)}…${value.slice(-4)}`;
const formatDate = (value: string) => new Intl.DateTimeFormat("en-GB", {
  day: "2-digit", month: "short", year: "numeric", timeZone: "UTC",
}).format(new Date(value));

const errorCode = async (response: Response) => {
  const payload = await response.json() as { detail?: { code?: string } | string };
  return typeof payload.detail === "string" ? payload.detail : payload.detail?.code;
};

export function App() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [context, setContext] = useState<WorkshopContext | null>(null);
  const [preparation, setPreparation] = useState<Preparation | null>(null);
  const [text, setText] = useState("");
  const [correction, setCorrection] = useState<ResponseSnapshot | null>(null);
  const [editTarget, setEditTarget] = useState<ProposalBinding | null>(null);
  const [pendingSubmission, setPendingSubmission] = useState<PendingSubmission | null>(null);
  const [notice, setNotice] = useState("Loading the durable Workshop context…");
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [boundLine, setBoundLine] = useState<number | null>(null);
  const composer = useRef<HTMLTextAreaElement | null>(null);
  const finishIntent = useRef<{ client_action_id: string; expected_case_revision: number } | null>(null);

  const refresh = async () => {
    const [contextResponse, preparationResponse] = await Promise.all([
      fetch("/api/workshop"),
      fetch("/api/workshop/preparation"),
    ]);
    if (!contextResponse.ok || !preparationResponse.ok) {
      throw new Error("Workshop context is unavailable");
    }
    const [nextContext, nextPreparation] = await Promise.all([
      contextResponse.json() as Promise<WorkshopContext>,
      preparationResponse.json() as Promise<Preparation>,
    ]);
    setContext(nextContext);
    setPreparation(nextPreparation);
    if (nextPreparation.message) setNotice(nextPreparation.message);
    if (nextPreparation.delayed_message) setNotice(nextPreparation.delayed_message);
  };

  useEffect(() => {
    const presence = new WorkshopPresenceClient();
    presence.start();
    Promise.all([
      fetch("/api/bootstrap").then((response) => {
        if (!response.ok) throw new Error("Bootstrap unavailable");
        return response.json() as Promise<Bootstrap>;
      }),
      refresh(),
    ])
      .then(([value]) => setBootstrap(value))
      .catch(() => setFailure("Workshop setup failed. Durable state was not changed."));
    return () => presence.stop();
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => void refresh().catch(() => undefined), 1500);
    return () => window.clearInterval(timer);
  }, []);

  const nextQuestion = context?.question_runway.questions[0] ?? null;
  const runwayMessage = context
    ? zeroRunwayMessage(context.question_runway.runway_depth, context.turn_submission_status)
    : null;
  const latestByTurn = useMemo(() => {
    const result = new Map<number, string>();
    for (const turn of context?.committed_turns ?? []) {
      result.set(turn.response.turn_sequence, turn.response.response_id);
    }
    return result;
  }, [context]);
  const canDraft = preparation?.phase === "READY"
    && context?.workshop_state === "ACTIVE"
    && (!!nextQuestion || !!correction);
  const canSend = canDraft
    && turnSubmissionEnabled(context?.turn_submission_status ?? "ANALYSIS_PENDING", busy);
  const sendLabel = context?.turn_submission_status === "ANALYSIS_PENDING"
    ? "Analyzing previous response…"
    : context?.turn_submission_status === "ANALYSIS_FAILED"
      ? "Analysis needs attention"
      : busy === "send" ? "Saving…" : pendingSubmission ? "Retry" : correction ? "Save correction" : "Send";

  const submitResponse = async (event: FormEvent) => {
    event.preventDefault();
    const question = correction
      ? { question_id: correction.question_id, question_version: correction.question_version }
      : nextQuestion;
    if (!question || !text.trim() || !canSend) return;
    const body = pendingSubmission ?? {
      client_submission_id: crypto.randomUUID(),
      question_id: question.question_id,
      expected_question_version: question.question_version,
      text,
      correction_of_response_id: correction?.response_id ?? null,
      edit_target: editTarget,
    };
    setPendingSubmission(body);
    setBusy("send");
    setFailure(null);
    try {
      const response = await fetch("/api/workshop/responses", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(await errorCode(response) ?? "RESPONSE_NOT_COMMITTED");
      const receipt = await response.json() as { recovery_code: "ANALYSIS_QUEUED" | "ANALYSIS_FAILED" };
      setNotice(receipt.recovery_code === "ANALYSIS_QUEUED"
        ? "Your response is saved. Analysis will resume automatically."
        : "Your response is saved, but analysis did not complete. Retry analysis.");
      setPendingSubmission(null);
      setCorrection(null);
      setEditTarget(null);
      setText("");
      await refresh();
    } catch (error) {
      setFailure(error instanceof Error ? error.message : "RESPONSE_NOT_COMMITTED");
      setNotice("The response was not acknowledged. Retry sends the same submission identity.");
    } finally {
      setBusy(null);
    }
  };

  const beginCorrection = (response: ResponseSnapshot) => {
    if (pendingSubmission || context?.turn_submission_status !== "READY") return;
    setCorrection(response);
    setEditTarget(null);
    setText(response.normalized_text);
    setFailure(null);
    setNotice(`Correcting turn ${response.turn_sequence}, version ${response.response_version}. Earlier versions remain immutable.`);
    requestAnimationFrame(() => composer.current?.focus());
  };

  const playQuestion = async (question: Question) => {
    setBusy("playback");
    setFailure(null);
    try {
      const response = await fetch("/api/workshop/playback", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          question_id: question.question_id,
          question_version: question.question_version,
          exact_text: question.exact_text,
        }),
      });
      if (!response.ok) throw new Error(await errorCode(response) ?? "PLAYBACK_FAILED");
      const receipt = await response.json() as { exact_text: string };
      if (!("speechSynthesis" in window)) throw new Error("PLAYBACK_UNAVAILABLE");
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(new SpeechSynthesisUtterance(receipt.exact_text));
      setNotice("Playing the exact canonical question. Playback has no input or control authority.");
    } catch (error) {
      setFailure(error instanceof Error ? error.message : "PLAYBACK_FAILED");
      setNotice("Playback is unavailable. Chat remains fully operational.");
    } finally {
      setBusy(null);
    }
  };

  const proposalAction = async (proposal: Proposal, action: "confirm" | "edit" | "reject") => {
    setBusy(`${action}:${proposal.binding.proposal_ref}`);
    setFailure(null);
    const body = { client_action_id: crypto.randomUUID(), binding: proposal.binding };
    try {
      const response = await fetch(
        `/api/workshop/proposals/${encodeURIComponent(proposal.binding.proposal_ref)}/${action}`,
        { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) },
      );
      if (!response.ok) throw new Error(await errorCode(response) ?? "PROPOSAL_ACTION_FAILED");
      if (action === "edit") {
        setEditTarget(proposal.binding);
        setNotice("Edit requested. Your next typed response is bound to this proposal.");
        requestAnimationFrame(() => composer.current?.focus());
      } else {
        setNotice(action === "confirm" ? "Proposal committed through Foundation." : "Proposal rejected; Foundation content was not changed.");
      }
      await refresh();
    } catch (error) {
      setFailure(error instanceof Error ? error.message : "PROPOSAL_ACTION_FAILED");
    } finally {
      setBusy(null);
    }
  };

  const finishWorkshop = async () => {
    if (!context || context.workshop_state !== "ACTIVE") return;
    const body = finishIntent.current ?? {
      client_action_id: crypto.randomUUID(),
      expected_case_revision: context.case_revision,
    };
    finishIntent.current = body;
    setBusy("finish");
    setFailure(null);
    try {
      const response = await fetch("/api/workshop/finish", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(await errorCode(response) ?? "FINISH_FAILED");
      finishIntent.current = null;
      setNotice("Workshop finish committed. Existing analysis is draining before handoff.");
      await refresh();
    } catch (error) {
      setFailure(error instanceof Error ? error.message : "FINISH_FAILED");
    } finally {
      setBusy(null);
    }
  };

  const focusEvidence = (line: number) => {
    setBoundLine(line);
    requestAnimationFrame(() => document.getElementById(`line-${line}`)?.scrollIntoView({ behavior: "smooth", block: "center" }));
  };

  return (
    <main className="workshop-shell">
      <section className="panel voice-panel chat-panel" aria-labelledby="chat-title">
        <header className="panel-header chat-header">
          <div>
            <p className="eyebrow">Canonical Workshop chat</p>
            <h1 id="chat-title">CSV Export Workshop</h1>
          </div>
          <span className="phase-mark">{context?.workshop_state ?? "LOADING"}</span>
        </header>
        {bootstrap && <p className="fixed-identity">Fixed participant identity · {shortId(bootstrap.pm_actor_id)}</p>}

        <div className="context-strip" aria-label="Durable Workshop state">
          <div><span>Revision</span><strong>{context?.case_revision ?? "—"}</strong></div>
          <div><span>Runway</span><strong>{context?.question_runway.runway_depth ?? 0}</strong></div>
          <div><span>Review</span><strong>{context?.session_card.review_obligation ?? "—"}</strong></div>
        </div>

        <div className="live-status" aria-live="polite">
          <span>{notice}</span>
          {failure && <strong>{failure}</strong>}
        </div>

        <div className="chat-transcript" role="log" aria-label="Workshop conversation">
          <div className="transcript-head">
            <h2>Conversation</h2>
            <span>{context?.committed_turns.length ?? 0} committed</span>
          </div>
          <ol className="conversation-ledger">
            {(context?.committed_turns ?? []).map(({ question, response }) => (
              <li key={response.response_id}>
                <article className="question-message">
                  <div><span>Luna · canonical question</span><code>{shortId(question.question_id)} · v{question.question_version}</code></div>
                  <p>{question.exact_text}</p>
                </article>
                <article className="response-message">
                  <div><span>You · chat</span><code>turn {response.turn_sequence} · v{response.response_version}</code></div>
                  <p>{response.normalized_text}</p>
                  <small>Evidence {shortId(response.final_source_ref.artifact_id)} / {response.content_hash.slice(0, 12)}…</small>
                  {latestByTurn.get(response.turn_sequence) === response.response_id && context?.workshop_state === "ACTIVE" && (
                    <button type="button" onClick={() => beginCorrection(response)} disabled={!!pendingSubmission || context.turn_submission_status !== "READY"}>Correct response</button>
                  )}
                </article>
              </li>
            ))}
            {nextQuestion && !correction && context?.workshop_state === "ACTIVE" && (
              <li className="current-turn">
                <article className="question-message current-question">
                  <div><span>Luna · canonical question</span><code>next · v{nextQuestion.question_version}</code></div>
                  <p>{nextQuestion.exact_text}</p>
                  <small className="current-question-reason">{nextQuestion.reason}</small>
                  <button type="button" className="playback-button" onClick={() => void playQuestion(nextQuestion)} disabled={busy !== null}>
                    Play exact question
                  </button>
                </article>
              </li>
            )}
            {!context?.committed_turns.length && !(nextQuestion && context?.workshop_state === "ACTIVE") && (
              <li className="empty-row">No response evidence is committed yet.</li>
            )}
          </ol>
          {runwayMessage && preparation?.phase === "READY" && !correction && context?.workshop_state === "ACTIVE" && (
            <div className="recovery-card" role="status">{runwayMessage}</div>
          )}
          {context && context.workshop_state !== "ACTIVE" && (
            <div className="chat-closed-state" role="status">
              <span>{context.workshop_state}</span>
              <strong>{context.workshop_state === "COMPLETED" ? "Workshop complete" : "Workshop input unavailable"}</strong>
              <p>{context.workshop_state === "COMPLETED"
                ? "This conversation is read-only. Start a new active Workshop to answer another question."
                : "The composer returns when the Workshop is active."}</p>
            </div>
          )}
        </div>

        {context?.workshop_state === "ACTIVE" && (
          <form className="text-fallback composer" aria-label="Workshop response" onSubmit={submitResponse}>
            <label htmlFor="response-text">{correction ? "Correct committed response" : "Your typed response"}</label>
            {editTarget && <p className="composer-binding">Editing proposal · {shortId(editTarget.proposal_ref)}</p>}
            <div>
              <textarea
                ref={composer}
                id="response-text"
                rows={3}
                value={text}
                disabled={!canDraft || !!pendingSubmission || busy !== null}
                onChange={(event) => setText(event.target.value)}
                placeholder={preparation?.phase === "READY" ? "Answer Luna’s canonical question…" : "Composer unlocks when preparation is ready"}
              />
              <button type="submit" disabled={!canSend || !text.trim()}>{sendLabel}</button>
            </div>
            {correction && !pendingSubmission && <button className="cancel-mode" type="button" onClick={() => { setCorrection(null); setText(""); }}>Cancel correction</button>}
            <small aria-live="polite">{context.turn_submission_status === "ANALYSIS_PENDING"
              ? "Your previous response is saved. Analysis must finish before you send another."
              : context.turn_submission_status === "ANALYSIS_FAILED"
                ? "The previous analysis did not complete. Sending another response remains locked."
                : "Only a successful Send receipt finalizes evidence. Draft text has no authority."}</small>
          </form>
        )}
      </section>

      <section className="panel source-panel" aria-labelledby="source-title">
        <header className="panel-header source-heading">
          <div><p className="eyebrow">Bound source</p><h2 id="source-title">Technical Contract</h2></div>
          <span className="document-code">read-only</span>
        </header>
        {bootstrap && (
          <>
            <div className="delegation-banner">
              <div className="authority-seal">DEV<span>→</span>PM</div>
              <div>
                <p><strong>Technical authority delegated</strong><span>verified fixture</span></p>
                <p className="delegation-dates">{formatDate(bootstrap.delegation_valid_from)} — {formatDate(bootstrap.delegation_valid_until)}</p>
                <p className="delegation-note">Foundation remains authoritative; this document is evidence, not provider memory.</p>
              </div>
            </div>
            <div className="scope-strip"><span>Exact scope</span><p>{bootstrap.delegation_command_scope.join(" · ").replaceAll("_", " ")}</p></div>
            <div className="document-toolbar">
              <div><strong>filtered-orders-csv-export</strong><span>Line-addressable source</span></div>
              <button className="bound-flag" type="button" onClick={() => focusEvidence(18)}>Jump to evidence L18</button>
            </div>
            <div className="source-lines" aria-label="Technical source lines">
              {bootstrap.technical_source_lines.map(([line, value]) => (
                <p id={`line-${line}`} key={line} data-line={line} className={boundLine === line ? "is-bound" : undefined} tabIndex={boundLine === line ? 0 : -1}>
                  <span>{line}</span><code>{value || " "}</code>
                </p>
              ))}
            </div>
          </>
        )}
      </section>

      <section className="panel package-panel" aria-labelledby="govern-title">
        <header className="panel-header package-heading">
          <div><p className="eyebrow">Explicit governance</p><h2 id="govern-title">Proposals</h2></div>
          <span className={`readiness readiness-${(context?.session_card.readiness ?? "FORMULATING").toLowerCase()}`}>{context?.session_card.readiness ?? "FORMULATING"}</span>
        </header>

        <div className="rollup">
          <div><span>Pending</span><strong>{context?.proposal_statuses.filter((item) => item.status === "PENDING").length ?? 0}</strong></div>
          <div><span>Committed</span><strong>{context?.proposal_statuses.filter((item) => item.status === "COMMITTED").length ?? 0}</strong></div>
          <div><span>Turns</span><strong>{new Set(context?.committed_turns.map((item) => item.response.turn_sequence)).size}</strong></div>
        </div>

        {(context?.proposal_statuses ?? []).map((proposal) => (
          <article className="proposal-sheet" key={`${proposal.binding.proposal_ref}:${proposal.binding.proposal_version}`}>
            <header><p>Decision proposal</p><span>{proposal.status}</span><h3>Review exact Foundation statements</h3></header>
            {proposal.view.items.map((item) => (
              <div className="proposal-item" key={item.handle}>
                <div className="item-title"><h4>{item.handle}</h4><span className={`domain domain-${item.classification.toLowerCase()}`}>{item.classification.replace("_", " ")}</span></div>
                <div className="statement-group"><span>Statement</span><p>{item.exact_statement}</p></div>
                <div className="statement-group"><span>Rationale</span><p>{item.rationale}</p></div>
              </div>
            ))}
            {proposal.status === "PENDING" && (
              <div className="proposal-controls">
                <button className="confirm" type="button" disabled={busy !== null} onClick={() => void proposalAction(proposal, "confirm")}>Confirm</button>
                <button type="button" disabled={busy !== null} onClick={() => void proposalAction(proposal, "edit")}>Edit</button>
                <button type="button" disabled={busy !== null} onClick={() => void proposalAction(proposal, "reject")}>Reject</button>
              </div>
            )}
            {proposal.status === "EDIT_REQUESTED" && <button className="reject-after-edit" type="button" disabled={busy !== null} onClick={() => void proposalAction(proposal, "reject")}>Reject edited proposal</button>}
            <code className="proposal-hash">{proposal.binding.payload_hash.slice(0, 18)}…</code>
          </article>
        ))}
        {!context?.proposal_statuses.length && <div className="empty-package"><strong>No proposal awaits a decision.</strong><p>Typed responses are evidence only. Controls appear only for a durable Foundation review.</p></div>}

        <section className="finish-zone" aria-labelledby="finish-title">
          <p className="eyebrow">Separate terminal action</p>
          <h3 id="finish-title">Finish Workshop</h3>
          <p>Finishing is deliberate and independent of response text, model output, and playback.</p>
          <button className="finish-button" type="button" onClick={() => void finishWorkshop()} disabled={context?.workshop_state !== "ACTIVE" || busy !== null || !!context?.proposal_statuses.some((item) => ["PENDING", "EDIT_REQUESTED"].includes(item.status))}>
            <span>{busy === "finish" ? "Finishing…" : "Finish Workshop"}</span><span>→</span>
          </button>
          {context?.completion_status && <strong className="completion-state">{context.completion_status.replace("_", " ")}</strong>}
        </section>
      </section>
    </main>
  );
}
