import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { LiveAudioClient } from "./audio/liveClient";
import { INITIAL_RUNWAY_DEPTH, workshopStartEnabled } from "./uiModel";
import type { components as WorkshopProtocolComponents } from "./generated/workshopProtocol";

type DecisionBatchReviewView = WorkshopProtocolComponents["schemas"]["DecisionBatchReviewView"];
type ArtifactReviewProjection = WorkshopProtocolComponents["schemas"]["ArtifactReviewProjection"];

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
  event_id: string;
  turn_sequence: number;
  version: number;
  normalized_text: string;
  correction_of_version: number | null;
  speaker_actor_id: string | null;
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
  preparation: {
    phase: "VALIDATING_DOCUMENTS" | "PREPARING_ANALYZER" | "ANALYZER_REVIEWING_DOCUMENTS" | "FORMULATING_WORKSHOP_PLAN" | "VALIDATING_INITIAL_RUNWAY" | "READY" | "FAILED";
    message: string;
    started_at: string;
    updated_at: string;
    ready_at: string | null;
    failure_code: string | null;
    delayed: boolean;
    delayed_message: string | null;
  };
  runway: {
    guidance_id: string | null;
    depth: number;
    questions: { question_id: string; question_version: number; position: number; exact_text: string; reason: string }[];
    asked: string[];
  };
  analyzer_jobs: { job_id: string; operation: string; state: string; attempt_count: number }[];
  session: {
    workshop_state: string;
    conversation_phase: "WORKSHOP" | "HANDOFF_READY" | "COMPLETE";
    call_state: string;
    revision_locked: boolean;
    revision_lock_reason: string | null;
  };
  final_transcripts: Transcript[];
  pending_proposal: null;
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

type ArtifactType = "SPEC_PACKAGE" | "TECHNICAL_CONTRACT";
type DecisionAction = "CONFIRM" | "REVISE" | "REJECT" | "DEFER";

const formatDate = (value: string) => new Intl.DateTimeFormat("en-GB", {
  day: "2-digit", month: "short", year: "numeric", timeZone: "UTC",
}).format(new Date(value));

const shortId = (value: string) => `${value.slice(0, 8)}…${value.slice(-4)}`;

const reportBrowserSpan = (startedAt: number, outcome: "OK" | "ERROR") => {
  void fetch("/api/telemetry/spans", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      span_id: crypto.randomUUID(),
      stage: "BROWSER",
      duration_ms: Math.max(0, Math.round(performance.now() - startedAt)),
      outcome,
    }),
  });
};

function ItemDomain({ domain }: { domain: GovernanceItem["domain"] }) {
  return <span className={`domain domain-${domain.toLowerCase()}`}>{domain.replace("_", " ")}</span>;
}

export function App() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [workshop, setWorkshop] = useState<WorkshopProjection | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [notice, setNotice] = useState("Validating governed sources…");
  const [callState, setCallState] = useState("PREPARING");
  const [partial, setPartial] = useState("");
  const [text, setText] = useState("");
  const [started, setStarted] = useState(false);
  const [muted, setMuted] = useState(false);
  const [boundLine, setBoundLine] = useState<number | null>(null);
  const [decisionReview, setDecisionReview] = useState<DecisionBatchReviewView | null>(null);
  const [artifactReview, setArtifactReview] = useState<ArtifactReviewProjection | null>(null);
  const [artifactType, setArtifactType] = useState<ArtifactType>("SPEC_PACKAGE");
  const [decisionActions, setDecisionActions] = useState<Record<string, DecisionAction | "">>({});
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const live = useRef<LiveAudioClient | null>(null);

  const loadDecisionReview = async (caseId: string) => {
    try {
      const response = await fetch(`/api/v4/cases/${caseId}/decision-review`);
      setDecisionReview(response.ok ? await response.json() as DecisionBatchReviewView | null : null);
    } catch {
      setDecisionReview(null);
    }
  };

  const loadArtifactReview = async (caseId: string) => {
    try {
      const response = await fetch(`/api/v4/cases/${caseId}/artifact-review`);
      setArtifactReview(response.ok ? await response.json() as ArtifactReviewProjection | null : null);
    } catch {
      setArtifactReview(null);
    }
  };

  const refresh = async () => {
    const response = await fetch("/api/workshop");
    if (!response.ok) throw new Error("Workshop projection unavailable");
    const value = await response.json() as WorkshopProjection;
    setWorkshop(value);
    if (!started) setCallState(value.session.call_state);
    if (bootstrap?.case_id) await Promise.all([
      loadDecisionReview(bootstrap.case_id),
      loadArtifactReview(bootstrap.case_id),
    ]);
  };

  useEffect(() => {
    const startedAt = performance.now();
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
        setNotice(nextWorkshop.preparation.message);
        void loadDecisionReview(nextBootstrap.case_id);
        void loadArtifactReview(nextBootstrap.case_id);
        reportBrowserSpan(startedAt, "OK");
      })
      .catch(() => {
        reportBrowserSpan(startedAt, "ERROR");
        setFailure("Workshop setup failed. Check the local server configuration and source fixtures.");
      });
    return () => live.current?.end();
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => { void refresh().catch(() => undefined); }, 1000);
    return () => window.clearInterval(timer);
  });

  useEffect(() => {
    if (!started && workshop?.preparation) setNotice(workshop.preparation.message);
  }, [started, workshop?.preparation.phase, workshop?.preparation.message]);

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
    if (workshop?.preparation.phase !== "READY") return;
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

  const runArtifactAction = async (action: "synthesize" | "review" | "confirm") => {
    const latestTranscript = workshop?.final_transcripts.at(-1);
    if (action === "confirm" && (!latestTranscript || !bootstrap)) {
      setFailure("Add a final spoken or text confirmation before confirming the exact artifact review.");
      return;
    }
    setFailure(null);
    setBusyAction(action);
    try {
      const operationKey = `browser-${action}-${artifactType.toLowerCase()}-${crypto.randomUUID()}`;
      const endpoint = action === "synthesize"
        ? `/api/v4/artifacts/${artifactType}/synthesize`
        : action === "review"
          ? `/api/v4/artifacts/${artifactType}/review`
          : "/api/v4/artifacts/current/confirm";
      const body = action === "confirm" && latestTranscript && bootstrap
        ? {
            actor_authentication: {
              authentication_method: "VERBAL_SELF_ASSERTION",
              assurance_level: "SELF_ASSERTED",
              actor_id: bootstrap.pm_actor_id,
              asserted_display_name: "Product Manager",
              claimed_role: "Product Manager",
              assertion_transcript_event_id: latestTranscript.event_id,
            },
            confirmation_transcript_event_id: latestTranscript.event_id,
          }
        : { operation_key: operationKey };
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        const payload = await response.json() as { detail?: { code?: string } | string };
        const detail = typeof payload.detail === "string" ? payload.detail : payload.detail?.code;
        throw new Error(detail ?? "Foundation refused the artifact action");
      }
      await refresh();
      setNotice({
        synthesize: `${artifactType === "SPEC_PACKAGE" ? "Spec Package" : "Technical Contract"} synthesized and quality-audited`,
        review: "Exact Foundation review projection opened",
        confirm: "Exact artifact version confirmed",
      }[action]);
    } catch (error) {
      setFailure(error instanceof Error ? error.message : "Foundation refused the artifact action.");
    } finally {
      setBusyAction(null);
    }
  };

  const applyDecisionSelection = async () => {
    const latestTranscript = workshop?.final_transcripts.at(-1);
    const selected = decisionReview?.items.filter((item) => decisionActions[item.handle]);
    if (!bootstrap || !latestTranscript || !selected?.length) {
      setFailure("Add a final decision response and select at least one displayed decision.");
      return;
    }
    setFailure(null);
    setBusyAction("decisions");
    try {
      const response = await fetch("/api/v4/decisions/current/respond", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          operation_key: `browser-decisions-${crypto.randomUUID()}`,
          response_transcript_event_id: latestTranscript.event_id,
          actor_authentication: {
            authentication_method: "VERBAL_SELF_ASSERTION",
            assurance_level: "SELF_ASSERTED",
            actor_id: bootstrap.pm_actor_id,
            asserted_display_name: "Product Manager",
            claimed_role: "Product Manager",
            assertion_transcript_event_id: latestTranscript.event_id,
          },
          selections: selected.map((item) => {
            const action = decisionActions[item.handle] as DecisionAction;
            return {
              handle: item.handle,
              action,
              revision_span: action === "REVISE" ? {
                transcript_event_id: latestTranscript.event_id,
                start_character: 0,
                end_character_exclusive: latestTranscript.normalized_text.length,
              } : null,
            };
          }),
        }),
      });
      if (!response.ok) {
        const payload = await response.json() as { detail?: { code?: string } | string };
        const detail = typeof payload.detail === "string" ? payload.detail : payload.detail?.code;
        throw new Error(detail ?? "Foundation refused the decision response");
      }
      setDecisionActions({});
      await refresh();
      setNotice("Selected decisions committed atomically; unselected decisions remain pending");
    } catch (error) {
      setFailure(error instanceof Error ? error.message : "Foundation refused the decision response.");
    } finally {
      setBusyAction(null);
    }
  };

  const blocked = workshop?.governance?.items.filter((item) => item.review_obligation === "DECISION_REQUIRED") ?? [];
  const later = workshop?.governance?.items.filter((item) => item.review_obligation === "LATER_REVIEW") ?? [];
  const phase = workshop?.session.conversation_phase ?? "WORKSHOP";
  const preparationReady = workshopStartEnabled(
    workshop?.preparation.phase ?? "VALIDATING_DOCUMENTS",
    workshop?.runway.depth ?? 0,
  );
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
        {bootstrap && <p className="fixed-identity">Fixed PM identity · {shortId(bootstrap.pm_actor_id)}</p>}

        <div className="call-stage" data-state={callState}>
          <div className="state-line"><span className="state-dot" />{callState.replace("_", " ")}</div>
          <div className="waveform" aria-hidden="true">
            {[14, 28, 18, 42, 66, 38, 72, 48, 24, 54, 32, 18].map((height, index) => (
              <i key={index} style={{ height: `${height}%` }} />
            ))}
          </div>
          <ol className="runway-rail" aria-label={`${workshop?.runway.depth ?? 0} of ${INITIAL_RUNWAY_DEPTH} admitted questions ready`}>
            {Array.from({ length: INITIAL_RUNWAY_DEPTH }, (_, index) => (
              <li key={index} data-admitted={index < (workshop?.runway.depth ?? 0)}>
                <span className="sr-only">Question {index + 1} {index < (workshop?.runway.depth ?? 0) ? "admitted" : "not ready"}</span>
              </li>
            ))}
          </ol>
          <button className="mic-button" type="button" disabled={!started && !preparationReady} onClick={started ? () => live.current?.interrupt() : startConversation} aria-label={started ? "Stop agent playback" : "Start Spec Workshop"}>
            <span aria-hidden="true">{started ? "Ⅱ" : "●"}</span>
          </button>
          <p className="call-prompt">{started ? "Tap to stop playback" : "Start Spec Workshop"}</p>
          <div className="call-actions">
            <button type="button" onClick={toggleMute} disabled={!started}>{muted ? "Unmute" : "Mute"}</button>
            <button type="button" onClick={endConversation} disabled={!started}>End</button>
          </div>
        </div>

        <div className="live-status" role="status" aria-live="polite">
          <span>{notice}</span>
          {workshop?.preparation.delayed && <em>{workshop.preparation.delayed_message}</em>}
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
            <textarea id="fallback-text" value={text} onChange={(event) => setText(event.target.value)} placeholder={preparationReady ? "Add a final PM decision…" : "Available when Workshop preparation is ready"} rows={2} disabled={!preparationReady} />
            <button type="submit" disabled={!preparationReady || !text.trim() || phase === "COMPLETE" || !!workshop?.session.revision_locked}>Send</button>
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
          <div><span>Items</span><strong>{workshop?.governance?.items.length ?? 0}</strong></div>
          <div><span>Ready</span><strong>{workshop?.governance?.items.filter((item) => item.readiness === "READY").length ?? 0}</strong></div>
          <div><span>Decisions</span><strong>{blocked.length}</strong></div>
        </div>

        {decisionReview && (
          <section className="decision-review-view" aria-labelledby="decision-review-title">
            <header>
              <div>
                <p>Foundation review · exact displayed content</p>
                <h3 id="decision-review-title">Decision batch</h3>
              </div>
              <span>VOICE BOUND</span>
            </header>
            <ol>
              {decisionReview.items.map((item) => (
                <li key={item.review_item_id}>
                  <strong>{item.handle}</strong>
                  <div>
                    <p>{item.exact_statement}</p>
                    <small>{item.rationale}</small>
                    {item.problem_origins.map((origin) => (
                      <em key={origin.problem_id}>{origin.problem_statement}</em>
                    ))}
                    <label className="decision-action">
                      <span>Action for {item.handle}</span>
                      <select
                        aria-label={`Action for ${item.handle}`}
                        value={decisionActions[item.handle] ?? ""}
                        onChange={(event) => setDecisionActions((current) => ({
                          ...current,
                          [item.handle]: event.target.value as DecisionAction | "",
                        }))}
                      >
                        <option value="">Leave pending</option>
                        <option value="CONFIRM">Confirm exact decision</option>
                        <option value="REVISE">Request revision from final response</option>
                        <option value="REJECT">Reject</option>
                        <option value="DEFER">Defer</option>
                      </select>
                    </label>
                  </div>
                </li>
              ))}
            </ol>
            <footer>
              <code>VIEW {shortId(decisionReview.view_id)}</code>
              <code>HASH {decisionReview.view_hash.slice(7, 19)}…</code>
            </footer>
            <button
              className="review-commit-button"
              type="button"
              onClick={applyDecisionSelection}
              disabled={busyAction !== null || !Object.values(decisionActions).some(Boolean)}
            >
              {busyAction === "decisions" ? "Applying selection…" : "Apply selected decisions"}
            </button>
            <small className="partial-policy">Unselected handles remain pending. Valid items commit independently in one Foundation transaction.</small>
          </section>
        )}

        <section className="production-controls" aria-labelledby="artifact-pipeline-title">
          <header>
            <div>
              <p>V4 production seam</p>
              <h3 id="artifact-pipeline-title">Artifact pipeline</h3>
            </div>
            <span>FOUNDATION OWNED</span>
          </header>
          <label>
            <span>Artifact</span>
            <select
              aria-label="Artifact type"
              value={artifactType}
              onChange={(event) => setArtifactType(event.target.value as ArtifactType)}
              disabled={busyAction !== null}
            >
              <option value="SPEC_PACKAGE">Spec Package</option>
              <option value="TECHNICAL_CONTRACT">Technical Contract</option>
            </select>
          </label>
          <ol>
            <li>
              <span>01</span>
              <button type="button" onClick={() => runArtifactAction("synthesize")} disabled={busyAction !== null}>
                {busyAction === "synthesize" ? "Synthesizing + auditing…" : "Synthesize + audit"}
              </button>
              <small>A fresh Terra audit Conversation evaluates the exact immutable draft.</small>
            </li>
            <li>
              <span>02</span>
              <button type="button" onClick={() => runArtifactAction("review")} disabled={busyAction !== null}>
                {busyAction === "review" ? "Opening review…" : "Open exact review"}
              </button>
              <small>Foundation projects the full payload and binds the displayed hash.</small>
            </li>
            <li>
              <span>03</span>
              <button type="button" onClick={() => runArtifactAction("confirm")} disabled={busyAction !== null || !artifactReview}>
                {busyAction === "confirm" ? "Confirming…" : "Confirm exact artifact"}
              </button>
              <small>First add a final confirmation statement; the transcript, view, and payload bind together.</small>
            </li>
          </ol>
        </section>

        {artifactReview && (() => {
          const view = artifactReview.view as {
            view_type?: string;
            mode?: string;
            header?: { package_name?: string; contract_name?: string; overall_readiness?: string; readiness?: string };
            items?: Array<{ id: string; title: string; summary: string }>;
            contract_content?: Record<string, unknown>;
            projection_integrity?: { all_material_items_included?: boolean };
          };
          return (
            <section className="artifact-review-view" aria-labelledby="artifact-review-title">
              <header>
                <div>
                  <p>Foundation artifact projection · immutable review instance</p>
                  <h3 id="artifact-review-title">{view.header?.package_name ?? view.header?.contract_name ?? "Artifact review"}</h3>
                </div>
                <span>{(view.mode ?? "review").toUpperCase()}</span>
              </header>
              <div className="artifact-review-summary">
                <strong>{view.header?.overall_readiness ?? view.header?.readiness ?? "FORMULATING"}</strong>
                <span>{view.items?.length ?? Object.keys(view.contract_content ?? {}).length} material sections</span>
                <span>{view.projection_integrity?.all_material_items_included ? "Complete projection" : "Projection blocked"}</span>
              </div>
              {view.items?.map((item) => <article key={item.id}><h4>{item.title}</h4><p>{item.summary}</p></article>)}
              <footer>
                <code>VIEW {shortId(artifactReview.view_id)}</code>
                <code>HASH {artifactReview.view_hash.slice(7, 19)}…</code>
                <span>{artifactReview.confirmed ? "CONFIRMED" : "AWAITING HUMAN CONFIRMATION"}</span>
              </footer>
            </section>
          );
        })()}

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
          </div> : <p>Confirm the governed artifacts to expose the exact read-only handoff manifest.</p>}
        </div>
      </section>
    </main>
  );
}
