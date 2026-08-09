import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { LiveAudioClient } from "./audio/liveClient";
import { formulationEnabled, proposalControlPayload } from "./uiModel";

type LineRange = { kind: "LINE_RANGE"; start: number; end: number };
type SourceRef = { artifact_id: string; version: number; content_hash: string; location: LineRange };
type Bootstrap = {
  case_id: string;
  pm_actor_id: string;
  dev_lead_actor_id: string;
  delegation_id: string;
  delegation_valid_from: string;
  delegation_valid_until: string;
  delegation_command_scope: string[];
  technical_source_lines: [number, string][];
};
type Transcript = {
  turn_sequence: number;
  version: number;
  normalized_text: string;
  correction_of_version: number | null;
};
type ProposalUnit = {
  proposal_key: string;
  statement: string;
  domain: "BUSINESS" | "TECHNICAL" | "CROSS_DOMAIN";
  source_refs: SourceRef[];
};
type ProposalCheck = ProposalUnit & { related_unit_proposal_keys: string[] };
type ProposalItem = {
  proposal_key: string;
  title: string;
  requirement_proposal_keys: string[];
  technical_decision_proposal_keys: string[];
  acceptance_check_proposal_keys: string[];
};
type CompleteProposal = {
  requirements: ProposalUnit[];
  technical_decisions: ProposalUnit[];
  acceptance_checks: ProposalCheck[];
  items: ProposalItem[];
};
type PendingProposal = {
  record: { proposal_ref: string; status: string; version: number };
  result: {
    acknowledgement: string | null;
    next_question: string | null;
    complete_package_proposal: CompleteProposal;
  };
};
type GovernanceItem = {
  binding: { item_id: string; item_version: number; semantic_hash: string };
  title: string;
  domain: "BUSINESS" | "TECHNICAL" | "CROSS_DOMAIN";
  readiness: "FORMULATING" | "NEEDS_CLARIFICATION" | "BLOCKED" | "READY";
  review_obligation: "NONE" | "DECISION_REQUIRED" | "LATER_REVIEW";
  approval_scopes: ("BUSINESS" | "TECHNICAL")[];
};
type WorkshopProjection = {
  session: {
    workshop_state: string;
    conversation_phase: "WORKSHOP" | "HANDOFF_READY" | "COMPLETE";
    call_state: string;
    revision_locked: boolean;
    revision_lock_reason: string | null;
  };
  final_transcripts: Transcript[];
  pending_proposal: PendingProposal | null;
  governance: {
    package_readiness: string;
    items: GovernanceItem[];
  } | null;
  review_requests: { review_request_id: string; kind: string; question: string; status: string }[];
  handoff: null | {
    package_binding: { artifact_id: string; version: number; semantic_hash: string };
    ready_item_bindings: { item_id: string; item_version: number; item_hash: string }[];
    blocked_review_requests: { review_request_id: string; question: string; item_binding: { item_id: string } }[];
    later_review_requests: { review_request_id: string; question: string; item_binding: { item_id: string } }[];
    transcript_source_refs: SourceRef[];
  };
};

const formatDate = (value: string) => new Intl.DateTimeFormat("en-GB", {
  day: "2-digit", month: "short", year: "numeric", timeZone: "UTC",
}).format(new Date(value));

const shortId = (value: string) => `${value.slice(0, 8)}…${value.slice(-4)}`;

function EvidenceLink({ sourceRef, onFocus }: { sourceRef: SourceRef; onFocus: (line: number) => void }) {
  const line = sourceRef.location.start;
  return (
    <button className="evidence-link" type="button" onClick={() => onFocus(line)} aria-label={`Focus technical source line ${line}`}>
      <span aria-hidden="true">§</span> L{line}
    </button>
  );
}

function ItemDomain({ domain }: { domain: GovernanceItem["domain"] | ProposalUnit["domain"] }) {
  return <span className={`domain domain-${domain.toLowerCase()}`}>{domain.replace("_", " ")}</span>;
}

export function App() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [workshop, setWorkshop] = useState<WorkshopProjection | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [notice, setNotice] = useState("Validating governed sources…");
  const [callState, setCallState] = useState("READY");
  const [partial, setPartial] = useState("");
  const [text, setText] = useState("");
  const [started, setStarted] = useState(false);
  const [muted, setMuted] = useState(false);
  const [boundLine, setBoundLine] = useState<number | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editInstruction, setEditInstruction] = useState("");
  const live = useRef<LiveAudioClient | null>(null);

  const refresh = async () => {
    const response = await fetch("/api/workshop");
    if (!response.ok) throw new Error("Workshop projection unavailable");
    const value = await response.json() as WorkshopProjection;
    setWorkshop(value);
    setCallState(value.session.call_state);
  };

  useEffect(() => {
    Promise.all([
      fetch("/api/bootstrap").then((response) => {
        if (!response.ok) throw new Error("Bootstrap unavailable");
        return response.json() as Promise<Bootstrap>;
      }),
      fetch("/api/workshop").then((response) => {
        if (!response.ok) throw new Error("Workshop unavailable");
        return response.json() as Promise<WorkshopProjection>;
      }),
    ])
      .then(([nextBootstrap, nextWorkshop]) => {
        setBootstrap(nextBootstrap);
        setWorkshop(nextWorkshop);
        setCallState(nextWorkshop.session.call_state);
        setNotice("Foundation and delegation verified");
      })
      .catch(() => setFailure("Workshop setup failed. Check the local server configuration and source fixtures."));
    return () => live.current?.end();
  }, []);

  const onLiveEvent = (raw: unknown) => {
    const event = raw as Record<string, unknown>;
    if (event.type === "CALL_STATE") setCallState(String(event.state));
    if (event.type === "TRANSCRIPT_PARTIAL") setPartial(String(event.text ?? ""));
    if (event.type === "TRANSCRIPT_FINAL") { setPartial(""); void refresh(); }
    if (event.type === "FINAL_COMMITTED" || event.type === "PROPOSAL_PENDING" || event.type === "CONTROL_APPLIED" || event.type === "FINISH_COMPLETE") void refresh();
    if (event.type === "PROPOSAL_PENDING") setNotice("Proposal ready. Confirm, edit, or reject before the conversation advances.");
    if (event.type === "INTERRUPTED") setNotice("Agent playback stopped");
    if (event.type === "ERROR") setFailure(`Voice control failed: ${String(event.code)}`);
  };

  const startConversation = async () => {
    setFailure(null);
    setCallState("CONNECTING");
    try {
      const client = new LiveAudioClient();
      live.current = client;
      await client.start(onLiveEvent);
      setStarted(true);
      setNotice("Microphone connected. Final PM turns become immutable evidence.");
    } catch {
      setCallState("DISCONNECTED");
      setFailure("Microphone or voice connection is unavailable. Continue with the text field below.");
    }
  };

  const toggleMute = () => {
    const next = !muted;
    live.current?.setMuted(next);
    setMuted(next);
  };

  const endConversation = () => {
    live.current?.end();
    setCallState("ENDED");
    setStarted(false);
    setNotice("Conversation ended. Governed evidence remains available.");
  };

  const submitText = async (event: FormEvent) => {
    event.preventDefault();
    const value = text.trim();
    if (!value) return;
    setText("");
    const sequence = (workshop?.final_transcripts.at(-1)?.turn_sequence ?? 0) + 1;
    if (started) {
      live.current?.sendText(value, sequence, `browser-text-${sequence}-${Date.now()}`);
      setNotice("Registering final PM evidence…");
      return;
    }
    try {
      const response = await fetch("/api/session/final-turn", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          turn_sequence: sequence,
          text: value,
          provider_request_id: `browser-text-${sequence}-${Date.now()}`,
          correction_of_version: null,
        }),
      });
      if (!response.ok) throw new Error();
      await refresh();
      setNotice("Final PM evidence committed");
    } catch {
      setFailure("The final turn was not committed. Reconnect or retry the same text.");
    }
  };

  const focusEvidence = (line: number) => {
    setBoundLine(line);
    requestAnimationFrame(() => {
      const target = document.getElementById(`line-${line}`);
      target?.focus({ preventScroll: true });
      target?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  };

  const controlProposal = async (intent: "CONFIRM" | "EDIT" | "REJECT") => {
    const proposalRef = workshop?.pending_proposal?.record.proposal_ref;
    if (!proposalRef) return;
    setFailure(null);
    try {
      const response = await fetch("/api/proposals/control", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(proposalControlPayload(intent, proposalRef, editInstruction)),
      });
      if (!response.ok) throw new Error();
      setEditOpen(false);
      setEditInstruction("");
      await refresh();
      setNotice(intent === "CONFIRM" ? "Proposal committed to the governed package" : `Proposal ${intent.toLowerCase()}ed`);
    } catch {
      setFailure("The proposal control did not commit. Review the current proposal and try again.");
    }
  };

  const finishWorkshop = async () => {
    setFailure(null);
    setNotice("Running final governance audit…");
    try {
      const response = await fetch("/api/finish", { method: "POST" });
      if (!response.ok) {
        const body = await response.json() as { detail?: string };
        throw new Error(body.detail ?? "Finish refused");
      }
      await refresh();
      setNotice("Workshop formulation is frozen. The handoff summary remains live.");
    } catch (error) {
      setFailure(error instanceof Error ? error.message : "Finish was refused by the final audit.");
    }
  };

  const proposed = workshop?.pending_proposal?.result.complete_package_proposal;
  const blocked = workshop?.governance?.items.filter((item) => item.review_obligation === "DECISION_REQUIRED") ?? [];
  const later = workshop?.governance?.items.filter((item) => item.review_obligation === "LATER_REVIEW") ?? [];
  const phase = workshop?.session.conversation_phase ?? "WORKSHOP";
  const canFormulate = formulationEnabled(phase, workshop?.session.revision_locked ?? false);
  const lineMap = useMemo(() => new Map(bootstrap?.technical_source_lines ?? []), [bootstrap]);

  return (
    <main className="workshop-shell">
      <section className="panel voice-panel" aria-labelledby="voice-title">
        <header className="panel-header">
          <div>
            <p className="eyebrow">Live specification room</p>
            <h1 id="voice-title">CSV Export<br />Workshop</h1>
          </div>
          <span className="phase-mark">{phase.replace("_", " ")}</span>
        </header>

        <div className="call-stage" data-state={callState}>
          <div className="state-line"><span className="state-dot" />{callState.replace("_", " ")}</div>
          <div className="waveform" aria-hidden="true">
            {[14, 28, 18, 42, 66, 38, 72, 48, 24, 54, 32, 18].map((height, index) => (
              <i key={index} style={{ height: `${height}%` }} />
            ))}
          </div>
          <button className="mic-button" type="button" onClick={started ? () => live.current?.interrupt() : startConversation} aria-label={started ? "Stop agent playback" : "Start conversation"}>
            <span aria-hidden="true">{started ? "Ⅱ" : "●"}</span>
          </button>
          <p className="call-prompt">{started ? "Tap to stop playback" : "Start conversation"}</p>
          <div className="call-actions">
            <button type="button" onClick={toggleMute} disabled={!started}>{muted ? "Unmute" : "Mute"}</button>
            <button type="button" onClick={endConversation} disabled={!started}>End</button>
          </div>
        </div>

        <div className="live-status" role="status" aria-live="polite">
          <span>{notice}</span>
          {failure && <strong>{failure}</strong>}
        </div>

        <div className="transcript-head">
          <h2>Evidence transcript</h2>
          <span>{workshop?.final_transcripts.length ?? 0} final</span>
        </div>
        <ol className="transcript-list" aria-label="Immutable final transcript">
          {workshop?.final_transcripts.map((turn) => (
            <li key={`${turn.turn_sequence}-${turn.version}`}>
              <div><span>PM · TURN {String(turn.turn_sequence).padStart(2, "0")}</span><time>v{turn.version}</time></div>
              <p>{turn.normalized_text}</p>
              {turn.correction_of_version && <small>Correction of v{turn.correction_of_version}; the prior final remains immutable.</small>}
            </li>
          ))}
          {partial && <li className="partial"><div><span>DISPLAY-ONLY PARTIAL</span></div><p>{partial}</p></li>}
          {!workshop?.final_transcripts.length && !partial && <li className="empty-row">No final turns yet. Start with the export’s success criteria.</li>}
        </ol>

        <form className="text-fallback" onSubmit={submitText}>
          <label htmlFor="fallback-text">Text fallback</label>
          <div>
            <textarea id="fallback-text" value={text} onChange={(event) => setText(event.target.value)} placeholder="Add a final PM decision…" rows={2} />
            <button type="submit" disabled={!text.trim() || phase === "COMPLETE" || !!workshop?.session.revision_locked}>Send</button>
          </div>
          <small>Use after a voice or device disconnect. Final text follows the same evidence gate.</small>
        </form>
      </section>

      <section className="panel source-panel" aria-labelledby="source-title">
        <header className="panel-header source-heading">
          <div>
            <p className="eyebrow">Delegated technical evidence</p>
            <h2 id="source-title">Dev Lead pre-read</h2>
          </div>
          <span className="document-code">TECH / DRAFT 01</span>
        </header>

        <aside className="delegation-banner" aria-label="Active technical delegation">
          <div className="authority-seal" aria-hidden="true">DL<span>→</span>PM</div>
          <div>
            <p><strong>Technical authority delegated</strong><span>Active · inclusive dates</span></p>
            {bootstrap && <p className="delegation-dates">{formatDate(bootstrap.delegation_valid_from)} → {formatDate(bootstrap.delegation_valid_until)}</p>}
            <p className="delegation-note">PM may formulate, mark ready, and approve technical package items. Dev Lead review remains required later.</p>
          </div>
        </aside>

        <div className="scope-strip" aria-label="Delegated command scope">
          <span>Scope</span>
          <p>{bootstrap?.delegation_command_scope.map((command) => command.replaceAll("_", " ")).join(" · ")}</p>
        </div>

        <div className="document-toolbar">
          <div><strong>Filtered Orders CSV Export</strong><span>Draft technical specification · read only</span></div>
          {boundLine && <button className="bound-flag" type="button" onClick={() => focusEvidence(boundLine)}>BOUND EVIDENCE · L{boundLine}</button>}
        </div>
        <article className="source-lines" aria-label="Line-addressable technical specification">
          {bootstrap?.technical_source_lines.map(([line, value]) => (
            <p id={`line-${line}`} data-line={line} className={boundLine === line ? "is-bound" : ""} key={line} tabIndex={-1}>
              <span aria-hidden="true">{String(line).padStart(3, "0")}</span>
              <code>{value || " "}</code>
            </p>
          ))}
        </article>
        {boundLine && <p className="sr-only" role="status">Focused bound evidence line {boundLine}: {lineMap.get(boundLine)}</p>}
      </section>

      <section className="panel package-panel" aria-labelledby="package-title">
        <header className="panel-header package-heading">
          <div>
            <p className="eyebrow">Governed package</p>
            <h2 id="package-title">Spec Package</h2>
          </div>
          <span className={`readiness readiness-${(workshop?.governance?.package_readiness ?? "FORMULATING").toLowerCase()}`}>{workshop?.governance?.package_readiness ?? "FORMULATING"}</span>
        </header>

        {workshop?.session.revision_locked && <div className="blocked-banner" role="alert"><strong>Package changes blocked</strong><span>{workshop.session.revision_lock_reason ?? "Foundation state needs recovery."}</span></div>}

        <div className="rollup">
          <div><span>Items</span><strong>{workshop?.governance?.items.length ?? proposed?.items.length ?? 0}</strong></div>
          <div><span>Ready</span><strong>{workshop?.governance?.items.filter((item) => item.readiness === "READY").length ?? 0}</strong></div>
          <div><span>Decisions</span><strong>{blocked.length}</strong></div>
        </div>

        {workshop?.pending_proposal && proposed && (
          <section className="proposal-sheet" aria-labelledby="proposal-title">
            <div className="proposal-binding" aria-hidden="true"><i /><i /><i /></div>
            <header>
              <p>Proposed · awaiting PM confirmation</p>
              <span>PATCH {workshop.pending_proposal.record.version.toString().padStart(2, "0")}</span>
              <h3 id="proposal-title">{proposed.items.map((item) => item.title).join(" + ")}</h3>
            </header>
            {proposed.items.map((item) => {
              const requirements = proposed.requirements.filter((unit) => item.requirement_proposal_keys.includes(unit.proposal_key));
              const decisions = proposed.technical_decisions.filter((unit) => item.technical_decision_proposal_keys.includes(unit.proposal_key));
              const checks = proposed.acceptance_checks.filter((check) => item.acceptance_check_proposal_keys.includes(check.proposal_key));
              const domain = [...requirements, ...decisions, ...checks].some((unit) => unit.domain === "CROSS_DOMAIN") || (requirements.length && decisions.length) ? "CROSS_DOMAIN" : decisions.length ? "TECHNICAL" : "BUSINESS";
              return <div className="proposal-item" key={item.proposal_key}>
                <div className="item-title"><h4>{item.title}</h4><ItemDomain domain={domain} /></div>
                <div className="statement-group"><span>Requirement</span>{requirements.map((unit) => <p key={unit.proposal_key}>{unit.statement} {unit.source_refs.map((ref) => <EvidenceLink key={`${ref.artifact_id}-${ref.location.start}`} sourceRef={ref} onFocus={focusEvidence} />)}</p>)}</div>
                <div className="statement-group"><span>Decision</span>{decisions.map((unit) => <p key={unit.proposal_key}>{unit.statement} {unit.source_refs.map((ref) => <EvidenceLink key={`${ref.artifact_id}-${ref.location.start}`} sourceRef={ref} onFocus={focusEvidence} />)}</p>)}</div>
                <div className="statement-group"><span>Acceptance</span>{checks.map((check) => <p key={check.proposal_key}>{check.statement} {check.source_refs.map((ref) => <EvidenceLink key={`${ref.artifact_id}-${ref.location.start}`} sourceRef={ref} onFocus={focusEvidence} />)}</p>)}</div>
              </div>;
            })}
            {workshop.pending_proposal.result.next_question && <p className="confirmation-question">{workshop.pending_proposal.result.next_question}</p>}
            {editOpen && <label className="edit-field">Edit instruction<textarea rows={3} value={editInstruction} onChange={(event) => setEditInstruction(event.target.value)} autoFocus /></label>}
            <div className="proposal-controls">
              <button className="confirm" type="button" onClick={() => controlProposal("CONFIRM")} disabled={!canFormulate}>Confirm</button>
              {editOpen ? <button type="button" onClick={() => controlProposal("EDIT")} disabled={!editInstruction.trim()}>Apply edit</button> : <button type="button" onClick={() => setEditOpen(true)} disabled={!canFormulate}>Edit</button>}
              <button type="button" onClick={() => controlProposal("REJECT")} disabled={!canFormulate}>Reject</button>
            </div>
          </section>
        )}

        <div className="committed-ledger">
          <div className="section-rule"><h3>Committed items</h3><span>STATE MACHINE</span></div>
          {workshop?.governance?.items.map((item, index) => (
            <article className="committed-item" key={item.binding.item_id}>
              <div className="item-index">{String(index + 1).padStart(2, "0")}</div>
              <div>
                <div className="item-title"><h4>{item.title}</h4><ItemDomain domain={item.domain} /></div>
                <p className="binding-id">ITEM {shortId(item.binding.item_id)} · v{item.binding.item_version}</p>
                <div className="item-state"><strong>{item.readiness.replace("_", " ")}</strong><span>{item.approval_scopes.join(" + ") || "Awaiting approval"}</span></div>
                {item.review_obligation === "LATER_REVIEW" && <p className="review-note later-review">Ready under delegation — Dev Lead review later.</p>}
                {item.review_obligation === "DECISION_REQUIRED" && <p className="review-note decision-required">Blocked — technical decision required.</p>}
              </div>
            </article>
          ))}
          {!workshop?.governance && <div className="empty-package"><strong>No committed items yet</strong><p>Evidence-linked proposals will cover filter fidelity, CSV schema, large-export processing, authorization, and failure handling.</p></div>}
        </div>

        <div className="review-queues">
          <section><div className="section-rule"><h3>Blocked decisions</h3><span>{blocked.length}</span></div>{blocked.length ? blocked.map((item) => <p key={item.binding.item_id}>{item.title}</p>) : <p className="quiet">No blocked decisions.</p>}</section>
          <section><div className="section-rule"><h3>Later review</h3><span>{later.length}</span></div>{later.length ? later.map((item) => <p key={item.binding.item_id}>{item.title}</p>) : <p className="quiet">No delegated reviews yet.</p>}</section>
        </div>

        <div className="handoff-preview">
          <div className="section-rule"><h3>Downstream handoff</h3><span>READ ONLY</span></div>
          {workshop?.handoff ? <div className="handoff-manifest">
            <p><strong>Package</strong><code>{shortId(workshop.handoff.package_binding.artifact_id)} · v{workshop.handoff.package_binding.version}</code></p>
            <p><strong>Ready bindings</strong><span>{workshop.handoff.ready_item_bindings.length}</span></p>
            {workshop.handoff.ready_item_bindings.map((binding) => <code key={binding.item_id}>READY · {shortId(binding.item_id)} · v{binding.item_version}</code>)}
            <p><strong>Blocked decisions</strong><span>{workshop.handoff.blocked_review_requests.length}</span></p>
            {workshop.handoff.blocked_review_requests.map((request) => <code key={request.review_request_id}>BLOCKED · {shortId(request.item_binding.item_id)}</code>)}
            <p><strong>Later review</strong><span>{workshop.handoff.later_review_requests.length}</span></p>
            {workshop.handoff.later_review_requests.map((request) => <code key={request.review_request_id}>LATER · {shortId(request.item_binding.item_id)}</code>)}
            <p><strong>Transcript refs</strong><span>{workshop.handoff.transcript_source_refs.length}</span></p>
          </div> : <p>Finish the workshop to freeze formulation and expose the exact handoff manifest.</p>}
        </div>
        <button className="finish-button" type="button" onClick={finishWorkshop} disabled={!workshop?.governance || !!workshop?.pending_proposal || phase !== "WORKSHOP"}>Finish workshop <span aria-hidden="true">→</span></button>
      </section>
    </main>
  );
}
