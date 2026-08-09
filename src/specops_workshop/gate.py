from __future__ import annotations

import asyncio
import json
import re
from datetime import timezone
from uuid import UUID, uuid5

from specops_workflow.enums import ApprovalScope, Domain
from specops_workflow.canonical import sha256 as canonical_sha256
from specops_workflow.models import (
    AcceptanceCheck,
    ApproveSpecPackageItemCommand,
    CreateSpecPackageV2Command,
    MarkSpecPackageItemReadyCommand,
    QueryOne,
    RecordItemAmbiguityFindingCommand,
    Requirement,
    ReviseSpecPackageV2Command,
    SpecPackageItem,
    SpecPackagePayloadV2,
    TechnicalDecision,
)
from specops_workflow.models import LineRange

from .analyzer import (
    AnalyzerRequest,
    AnalyzerTurnResult,
    ControlIntent,
    GroundingChecker,
    SpecAnalyzerProvider,
    proposal_id,
    validate_phase,
)
from .contracts import PackageProposalRecord, ProposalStatus
from .orchestration import WorkshopCoordinator
from .sessions import WorkshopStore
from .telemetry import SpanOutcome, TelemetryStage


GATE_NAMESPACE = UUID("aac80ba7-67e6-5538-a88b-3dac7ebde6ee")


class RegisteredEvidenceGrounding:
    """Fail-closed identity/range grounding; semantic fixtures may be injected."""
    def __init__(self, *, static_refs: dict[UUID, tuple[int, str, int | tuple[str, ...]]], store: WorkshopStore, session_id: UUID) -> None:
        self.static_refs = static_refs; self.store = store; self.session_id = session_id
        self.last_failure_code: str | None = None

    def supports(self, claim: str, evidence_refs: tuple) -> bool:
        self.last_failure_code = None
        if not claim.strip() or not evidence_refs:
            return self._reject("MISSING_CLAIM_OR_EVIDENCE")
        transcript = {
            snapshot.final_source_ref.model_dump_json(): snapshot.normalized_text
            for snapshot in self.store.latest_snapshots(self.session_id)
        }
        for ref in evidence_refs:
            transcript_text = transcript.get(ref.model_dump_json())
            if transcript_text is not None:
                if not self._semantic_overlap(claim, transcript_text):
                    return self._reject("TRANSCRIPT_SEMANTIC_MISMATCH")
                continue
            expected = self.static_refs.get(ref.artifact_id)
            if expected is None:
                return self._reject("UNKNOWN_SOURCE_ARTIFACT")
            line_source = None if expected is None else expected[2]
            line_count = line_source if isinstance(line_source, int) else len(line_source)
            if (
                expected is None
                or ref.version != expected[0]
                or ref.content_hash != expected[1]
                or not isinstance(ref.location, LineRange)
                or ref.location.end > line_count
            ):
                return self._reject("SOURCE_IDENTITY_OR_RANGE_MISMATCH")
            if isinstance(line_source, tuple):
                excerpt = "\n".join(line_source[ref.location.start - 1:ref.location.end])
                if not self._semantic_overlap(claim, excerpt):
                    return self._reject("SOURCE_SEMANTIC_MISMATCH")
        return True

    def _reject(self, code: str) -> bool:
        self.last_failure_code = code
        return False

    @staticmethod
    def _semantic_overlap(claim: str, evidence: str) -> bool:
        stop = {
            "a", "an", "and", "are", "as", "at", "be", "by", "does", "for", "from",
            "how", "in", "is", "it", "of", "on", "or", "should", "the", "this", "to",
            "what", "when", "which", "with",
        }
        def terms(value: str) -> set[str]:
            return {
                token for token in re.findall(r"[a-z0-9]+", value.casefold())
                if len(token) >= 3 and token not in stop
            }
        return bool(terms(claim).intersection(terms(evidence)))


class WorkshopGate:
    def __init__(
        self,
        store: WorkshopStore,
        foundation,
        analyzer: SpecAnalyzerProvider,
        grounding: GroundingChecker,
        *,
        clock,
        source_context=None,
        telemetry=None,
        default_effort: str = "medium",
    ) -> None:
        self.store = store; self.foundation = foundation; self.analyzer = analyzer; self.grounding = grounding; self.clock = clock
        self.source_context = source_context
        self.telemetry = telemetry
        self.default_effort = default_effort
        self.coordinator = WorkshopCoordinator(
            store, foundation, clock=clock, telemetry=telemetry
        )

    async def analyze_final_turn(
        self,
        session_id: UUID,
        turn_sequence: int,
        *,
        effort: str | None = None,
        purpose: str = "TURN",
        edit_instruction: str | None = None,
    ) -> AnalyzerTurnResult:
        resolved_effort = self.default_effort if effort is None else effort
        session = self.store.get_session(session_id)
        snapshots = {value.turn_sequence: value for value in self.store.latest_snapshots(session_id)}
        final = snapshots.get(turn_sequence)
        if final is None:
            raise ValueError("only persisted provider-final PM evidence may be analyzed")
        edit_identity = canonical_sha256(edit_instruction or "")
        request_id = uuid5(GATE_NAMESPACE, f"{session_id}:analyze:{turn_sequence}:{final.version}:{resolved_effort}:{purpose}:{edit_identity}")
        request = AnalyzerRequest(
            request_id=request_id, session_id=session_id, effort=resolved_effort, purpose=purpose,
            phase=session.conversation_phase,
            final_turn=final,
            source_context=self.source_context,
            final_transcript_snapshots=self.store.all_snapshots(session_id),
            committed_context_json=self._committed_context(session, edit_instruction),
        )
        span_id = None
        if self.telemetry is not None:
            span_id = self.telemetry.start(
                session_id=session_id,
                stage=TelemetryStage.ANALYZER,
                operation_id=str(request_id),
            )
        result = None
        last_error = None
        for attempt, delay in enumerate((0.0, 0.25, 0.75), start=1):
            if delay: await asyncio.sleep(delay)
            self.store.record_analyzer_attempt(
                request_id=request_id,
                session_id=session_id,
                turn_sequence=turn_sequence,
                effort=resolved_effort,
                status="IN_FLIGHT",
                attempt_count=attempt,
                now=self.clock.now(),
            )
            try:
                result = await self.analyzer.analyze(request)
                self.store.record_analyzer_attempt(
                    request_id=request_id,
                    session_id=session_id,
                    turn_sequence=turn_sequence,
                    effort=resolved_effort,
                    status="CONFIRMED",
                    attempt_count=attempt,
                    now=self.clock.now(),
                )
                break
            except Exception as exc:
                last_error = exc
                continue
        if result is None:
            self.store.record_analyzer_attempt(
                request_id=request_id,
                session_id=session_id,
                turn_sequence=turn_sequence,
                effort=resolved_effort,
                status="FAILED",
                attempt_count=3,
                now=self.clock.now(),
            )
            self.store.lock_session(session_id, "ANALYZER_UNAVAILABLE", self.clock.now())
            if span_id is not None:
                self.telemetry.finish(
                    span_id,
                    outcome=SpanOutcome.ERROR,
                    error_code="ANALYZER_UNAVAILABLE",
                )
            raise RuntimeError("analyzer failed after bounded retries") from last_error
        if span_id is not None:
            self.telemetry.finish(span_id, outcome=SpanOutcome.OK)
        if result.turn_source_ref != final.final_source_ref:
            raise ValueError("analyzer turn SourceRef mismatch")
        validate_phase(result, session.conversation_phase)
        self._validate_grounding(result)
        if result.complete_package_proposal is not None:
            self._validate_identities(session_id, result)
            latest = self.store.latest_proposal(session_id)
            version = 1 if latest is None else latest.version + 1
            proposal_ref = f"workshop-patch-{proposal_id(session_id, 'package', result.complete_package_proposal.proposal_key)}-v{version}"
            self.store.save_proposal(PackageProposalRecord(
                proposal_ref=proposal_ref, session_id=session_id, version=version,
                base_foundation_revision=session.expected_foundation_revision,
                analyzer_result_json=result.model_dump_json(), status=ProposalStatus.PENDING,
                created_at=self.clock.now(), updated_at=self.clock.now(),
            ))
        elif result.control_intent in {ControlIntent.CONFIRM, ControlIntent.EDIT, ControlIntent.REJECT}:
            self.apply_control(session_id, result, confirmation_context=True)
        return result

    def apply_control(
        self,
        session_id: UUID,
        control: AnalyzerTurnResult,
        *,
        confirmation_context: bool = False,
    ):
        session = self.store.get_session(session_id)
        validate_phase(control, session.conversation_phase)
        pending = self.store.pending_proposal(session_id)
        if control.control_intent == ControlIntent.REJECT:
            self._assert_target(control, pending)
            return self.store.set_proposal_status(pending.proposal_ref, ProposalStatus.REJECTED, self.clock.now())
        if control.control_intent == ControlIntent.EDIT:
            self._assert_target(control, pending)
            return self.store.set_proposal_status(pending.proposal_ref, ProposalStatus.SUPERSEDED, self.clock.now())
        if control.control_intent != ControlIntent.CONFIRM:
            return None
        if not confirmation_context:
            raise ValueError("confirmation requires an explicit pending-proposal prompt context")
        self._assert_target(control, pending)
        proposed = AnalyzerTurnResult.model_validate_json(pending.analyzer_result_json).complete_package_proposal
        if proposed is None:
            raise ValueError("pending proposal has no complete package")
        self._validate_identities(session_id, AnalyzerTurnResult.model_validate_json(pending.analyzer_result_json))
        payload, item_ids = self._materialize(session_id, proposed)
        package_id = proposal_id(session_id, "package", proposed.proposal_key)
        if proposed.existing_package_id is None:
            command = CreateSpecPackageV2Command(
                command_id=uuid5(GATE_NAMESPACE, f"{pending.proposal_ref}:create-package"),
                case_id=session.case_id, acting_actor_id=session.pm_actor_id,
                expected_case_revision=session.expected_foundation_revision,
                package_id=package_id, content_schema_version=2, hash_schema_version=3, payload=payload,
            )
            created = self._commit(session_id, pending.proposal_ref, "create_spec_package", command)
        else:
            current = self.foundation.get_workflow_view(
                QueryOne(case_id=session.case_id, acting_actor_id=session.pm_actor_id)
            ).current_package
            if current is None:
                raise ValueError("existing package identity is not registered")
            command = ReviseSpecPackageV2Command(
                command_id=uuid5(GATE_NAMESPACE, f"{pending.proposal_ref}:revise-package"),
                case_id=session.case_id, acting_actor_id=session.pm_actor_id,
                expected_case_revision=session.expected_foundation_revision,
                expected_artifact_id=current.artifact_id,
                expected_artifact_version=current.version,
                expected_artifact_hash=current.semantic_hash,
                content_schema_version=2,
                hash_schema_version=3,
                payload=payload,
            )
            created = self._commit(session_id, pending.proposal_ref, "revise_spec_package", command)
        revision = self._stored(created).receipt.revision
        self.store.set_foundation_revision(session_id, revision, self.clock.now())
        package_binding = self._stored(created).binding
        pending_result = AnalyzerTurnResult.model_validate_json(pending.analyzer_result_json)
        blocked_item_ids: set[UUID] = set()
        for finding in pending_result.finding_proposals:
            if finding.disposition.value != "OPEN":
                continue
            item_id = item_ids.get(finding.item_proposal_key)
            if item_id is None:
                raise ValueError("finding references an unknown package item proposal key")
            finding_id = proposal_id(session_id, "finding", finding.proposal_key)
            finding_command = RecordItemAmbiguityFindingCommand(
                command_id=uuid5(GATE_NAMESPACE, f"{pending.proposal_ref}:finding:{finding_id}"),
                case_id=session.case_id,
                acting_actor_id=session.pm_actor_id,
                expected_case_revision=revision,
                finding_id=finding_id,
                item_id=item_id,
                category=finding.category,
                domain=finding.domain,
                severity=finding.severity,
                evidence_refs=finding.evidence_refs,
                clarification_question=finding.clarification_question,
            )
            recorded = self._commit(
                session_id,
                f"{pending.proposal_ref}:finding:{finding_id}",
                "record_ambiguity_finding",
                finding_command,
            )
            revision = self._stored(recorded).receipt.revision
            blocked_item_ids.add(item_id)
            self.store.set_foundation_revision(session_id, revision, self.clock.now())
        governance = self.foundation.get_spec_package_governance(QueryOne(case_id=session.case_id, acting_actor_id=session.pm_actor_id))
        for item in governance.items:
            if item.binding.item_id in blocked_item_ids:
                continue
            mark_command = MarkSpecPackageItemReadyCommand(
                command_id=uuid5(GATE_NAMESPACE, f"{pending.proposal_ref}:mark:{item.binding.item_id}"),
                case_id=session.case_id, acting_actor_id=session.pm_actor_id, expected_case_revision=revision,
                expected_artifact_id=package_binding.artifact_id, expected_artifact_version=package_binding.version,
                expected_artifact_hash=package_binding.semantic_hash, item_binding=item.binding,
            )
            marked = self._commit(
                session_id,
                f"{pending.proposal_ref}:mark:{item.binding.item_id}",
                "mark_spec_package_item_ready",
                mark_command,
            )
            revision = self._stored(marked).receipt.revision
            scopes = [ApprovalScope.BUSINESS] if item.domain == Domain.BUSINESS else [ApprovalScope.TECHNICAL] if item.domain == Domain.TECHNICAL else [ApprovalScope.BUSINESS, ApprovalScope.TECHNICAL]
            for scope in scopes:
                approve_command = ApproveSpecPackageItemCommand(
                    command_id=uuid5(GATE_NAMESPACE, f"{pending.proposal_ref}:approve:{item.binding.item_id}:{scope.value}"),
                    case_id=session.case_id, acting_actor_id=session.pm_actor_id, expected_case_revision=revision,
                    expected_artifact_id=package_binding.artifact_id, expected_artifact_version=package_binding.version,
                    expected_artifact_hash=package_binding.semantic_hash, item_binding=item.binding, scope=scope,
                )
                approved = self._commit(
                    session_id,
                    f"{pending.proposal_ref}:approve:{item.binding.item_id}:{scope.value}",
                    "approve_spec_package_item",
                    approve_command,
                )
                revision = self._stored(approved).receipt.revision
            self.store.set_foundation_revision(session_id, revision, self.clock.now())
        return self.store.set_proposal_status(pending.proposal_ref, ProposalStatus.COMMITTED, self.clock.now())

    def _commit(self, session_id, proposal_ref, command_name, command):
        action = proposal_ref if command_name in {"create_spec_package", "revise_spec_package"} else proposal_ref
        return self.coordinator.commit_foundation_command(
            session_id,
            logical_action_key=f"gate:{command_name}:{action}",
            command_name=command_name,
            command=command,
        )

    def _validate_identities(self, session_id: UUID, result: AnalyzerTurnResult) -> None:
        proposal = result.complete_package_proposal
        for finding in result.finding_proposals:
            if finding.existing_finding_id is not None:
                raise ValueError("model supplied an unknown existing finding UUID")
        if proposal is None:
            return
        session = self.store.get_session(session_id)
        workflow = self.foundation.get_workflow_view(
            QueryOne(case_id=session.case_id, acting_actor_id=session.pm_actor_id)
        )
        committed_record = self.store.latest_proposal(session_id, status=ProposalStatus.COMMITTED)
        committed = None
        if committed_record is not None:
            committed = AnalyzerTurnResult.model_validate_json(
                committed_record.analyzer_result_json
            ).complete_package_proposal

        known: dict[tuple[str, str], UUID] = {}
        if committed is not None:
            known[("package", committed.proposal_key)] = (
                committed.existing_package_id
                or proposal_id(session_id, "package", committed.proposal_key)
            )
            for kind, values, field in (
                ("unit", [*committed.requirements, *committed.technical_decisions], "existing_unit_id"),
                ("check", committed.acceptance_checks, "existing_check_id"),
                ("item", committed.items, "existing_item_id"),
            ):
                for value in values:
                    known[(kind, value.proposal_key)] = getattr(value, field) or proposal_id(
                        session_id, kind, value.proposal_key
                    )

        def validate(kind: str, key: str, existing_id: UUID | None) -> None:
            recognized = known.get((kind, key))
            if existing_id is not None and existing_id != recognized:
                raise ValueError("model supplied a new or unknown UUID")
            if recognized is not None and existing_id != recognized:
                raise ValueError("committed proposal keys must retain their server UUID")

        validate("package", proposal.proposal_key, proposal.existing_package_id)
        if proposal.existing_package_id is not None and (
            workflow.current_package is None
            or workflow.current_package.artifact_id != proposal.existing_package_id
        ):
            raise ValueError("model supplied an unknown package UUID")
        for value in [*proposal.requirements, *proposal.technical_decisions]:
            validate("unit", value.proposal_key, value.existing_unit_id)
        for value in proposal.acceptance_checks:
            validate("check", value.proposal_key, value.existing_check_id)
        for value in proposal.items:
            validate("item", value.proposal_key, value.existing_item_id)

        if committed is not None:
            old_keys = {
                value.proposal_key
                for value in [
                    *committed.requirements,
                    *committed.technical_decisions,
                    *committed.acceptance_checks,
                    *committed.items,
                ]
            }
            new_keys = {
                value.proposal_key
                for value in [
                    *proposal.requirements,
                    *proposal.technical_decisions,
                    *proposal.acceptance_checks,
                    *proposal.items,
                ]
            }
            if not old_keys.issubset(new_keys):
                raise ValueError("package proposal is a partial collection")

    @staticmethod
    def _stored(result): return result.stored_result if not result.mutated else result

    @staticmethod
    def _assert_target(control, pending):
        if pending is None or control.target_proposal_ref != pending.proposal_ref:
            raise ValueError("control target is not the single visible pending proposal")

    def _validate_grounding(self, result: AnalyzerTurnResult) -> None:
        claims = []
        for finding in result.finding_proposals:
            claims.append((finding.clarification_question, tuple(finding.evidence_refs)))
        proposal = result.complete_package_proposal
        if proposal:
            for value in [*proposal.requirements, *proposal.technical_decisions, *proposal.acceptance_checks]:
                claims.append((value.statement, tuple(value.source_refs)))
        for claim, refs in claims:
            if not self.grounding.supports(claim, refs):
                code = getattr(self.grounding, "last_failure_code", None)
                suffix = f": {code}" if isinstance(code, str) else ""
                raise ValueError(
                    "analyzer proposal is not grounded in its cited evidence" + suffix
                )

    def _committed_context(self, session, edit_instruction: str | None = None) -> str:
        try:
            governance = self.foundation.get_spec_package_governance(
                QueryOne(case_id=session.case_id, acting_actor_id=session.pm_actor_id)
            ).model_dump(mode="json")
            content = self.foundation.get_spec_package_content(
                QueryOne(case_id=session.case_id, acting_actor_id=session.pm_actor_id)
            ).model_dump(mode="json")
        except Exception:
            governance = None
            content = None
        return json.dumps({
            "content": content,
            "governance": governance,
            "edit_instruction": edit_instruction,
        }, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _materialize(session_id, proposed):
        units = {value.proposal_key: (value.existing_unit_id or proposal_id(session_id, "unit", value.proposal_key)) for value in [*proposed.requirements, *proposed.technical_decisions]}
        checks = {value.proposal_key: (value.existing_check_id or proposal_id(session_id, "check", value.proposal_key)) for value in proposed.acceptance_checks}
        items = {value.proposal_key: (value.existing_item_id or proposal_id(session_id, "item", value.proposal_key)) for value in proposed.items}
        payload = SpecPackagePayloadV2(
            requirements=[Requirement(unit_id=units[v.proposal_key], statement=v.statement, domain=v.domain, delivery_required=v.delivery_required, source_refs=v.source_refs) for v in proposed.requirements],
            technical_decisions=[TechnicalDecision(unit_id=units[v.proposal_key], statement=v.statement, domain=v.domain, delivery_required=v.delivery_required, source_refs=v.source_refs, provisional=False) for v in proposed.technical_decisions],
            acceptance_checks=[AcceptanceCheck(check_id=checks[v.proposal_key], statement=v.statement, domain=v.domain, related_unit_ids=[units[k] for k in v.related_unit_proposal_keys], source_refs=v.source_refs) for v in proposed.acceptance_checks],
            items=[SpecPackageItem(item_id=items[v.proposal_key], title=v.title, requirement_ids=[units[k] for k in v.requirement_proposal_keys], technical_decision_ids=[units[k] for k in v.technical_decision_proposal_keys], acceptance_check_ids=[checks[k] for k in v.acceptance_check_proposal_keys], dependency_item_ids=[items[k] for k in v.dependency_item_proposal_keys]) for v in proposed.items],
        )
        return payload, items
