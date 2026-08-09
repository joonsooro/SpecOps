from __future__ import annotations

from uuid import UUID, uuid5

from specops_workflow.enums import Domain, ReviewRequestKind
from specops_workflow.models import (
    CreateReviewRequestCommand,
    DownstreamHandoff,
    QueryOne,
    UUIDListQuery,
)

from .analyzer import AnalyzerTurnResult, ProposalDisposition, proposal_id
from .contracts import ConversationPhase, ProposalStatus, WorkshopModel, WorkshopState
from .orchestration import WorkshopCoordinator
from .sessions import WorkshopStore


FINISH_NAMESPACE = UUID("7e60bfbd-3d88-5e85-b465-63c25e026c4a")


class FinishResult(WorkshopModel):
    handoff: DownstreamHandoff
    workshop_state: WorkshopState
    conversation_phase: ConversationPhase


class FinishCoordinator:
    def __init__(self, store: WorkshopStore, foundation, gate, *, clock) -> None:
        self.store = store
        self.foundation = foundation
        self.gate = gate
        self.clock = clock
        self.outbox = WorkshopCoordinator(
            store,
            foundation,
            clock=clock,
            telemetry=getattr(gate, "telemetry", None),
        )

    async def finish(self, session_id: UUID) -> FinishResult:
        session = self.store.get_session(session_id)
        if session.conversation_phase == ConversationPhase.HANDOFF_READY:
            return self._result(session_id)
        if session.conversation_phase != ConversationPhase.WORKSHOP:
            raise ValueError("only an active Workshop can finish")
        if session.revision_locked:
            raise ValueError("the Workshop is revision locked and cannot finish")
        if self.store.pending_proposal(session_id) is not None:
            raise ValueError("Confirm, edit, or reject the pending package proposal before finishing.")
        snapshots = self.store.latest_snapshots(session_id)
        if not snapshots:
            raise ValueError("At least one provider-final PM turn is required before finishing.")
        committed_record = self.store.latest_proposal(session_id, status=ProposalStatus.COMMITTED)
        if committed_record is None:
            raise ValueError("Commit a complete package proposal before finishing.")

        self.store.update_phase(
            session_id, workshop_state=WorkshopState.FINISHING, now=self.clock.now()
        )
        audit = await self.gate.analyze_final_turn(
            session_id,
            snapshots[-1].turn_sequence,
            effort="medium",
            purpose="FINISH_AUDIT",
        )
        if audit.complete_package_proposal is not None or self.store.pending_proposal(session_id) is not None:
            self.store.update_phase(
                session_id, workshop_state=WorkshopState.ACTIVE, now=self.clock.now()
            )
            raise ValueError("The final audit proposed package changes; resolve that proposal before finishing.")

        committed = AnalyzerTurnResult.model_validate_json(committed_record.analyzer_result_json)
        proposal = committed.complete_package_proposal
        if proposal is None:
            self.store.update_phase(
                session_id, workshop_state=WorkshopState.ACTIVE, now=self.clock.now()
            )
            raise ValueError("The committed proposal record is incomplete.")
        findings = {value.proposal_key: value for value in [*committed.finding_proposals, *audit.finding_proposals]}
        items_by_key = {value.proposal_key: value for value in proposal.items}
        governance = self.foundation.get_spec_package_governance(QueryOne(
            case_id=session.case_id, acting_actor_id=session.pm_actor_id
        ))
        item_views = {value.binding.item_id: value for value in governance.items}
        existing_reviews = self.foundation.list_review_requests(UUIDListQuery(
            case_id=session.case_id, acting_actor_id=session.pm_actor_id
        )).items

        for finding in findings.values():
            item_proposal = items_by_key.get(finding.item_proposal_key)
            if item_proposal is None:
                return self._refuse(session_id, "A finding does not identify a committed package item.")
            item_id = item_proposal.existing_item_id or proposal_id(
                session_id, "item", item_proposal.proposal_key
            )
            item = item_views.get(item_id)
            if item is None:
                return self._refuse(session_id, "A finding points to an unknown current ItemBinding.")
            expected_owners = (
                {session.pm_actor_id}
                if item.domain == Domain.BUSINESS
                else {self._dev_lead(existing_reviews, session, item.domain)}
                if item.domain == Domain.TECHNICAL
                else {session.pm_actor_id, self._dev_lead(existing_reviews, session, item.domain)}
            )
            if None in expected_owners or set(finding.owner_actor_ids) != expected_owners:
                return self._refuse(session_id, "A finding has an incomplete or incorrect decision owner.")
            if finding.disposition != ProposalDisposition.OPEN:
                continue
            if any(
                request.item_binding == item.binding
                and request.kind == ReviewRequestKind.DECISION_REQUIRED
                for request in existing_reviews
            ):
                continue
            review_id = uuid5(
                FINISH_NAMESPACE,
                f"{session_id}:{item.binding.item_id}:{item.binding.item_version}:{finding.proposal_key}",
            )
            current_session = self.store.get_session(session_id)
            command = CreateReviewRequestCommand(
                command_id=uuid5(FINISH_NAMESPACE, f"command:{review_id}"),
                case_id=session.case_id,
                acting_actor_id=session.pm_actor_id,
                expected_case_revision=current_session.expected_foundation_revision,
                expected_artifact_id=governance.package_binding.artifact_id,
                expected_artifact_version=governance.package_binding.version,
                expected_artifact_hash=governance.package_binding.semantic_hash,
                item_binding=item.binding,
                review_request_id=review_id,
                kind=ReviewRequestKind.DECISION_REQUIRED,
                question=finding.clarification_question,
                evidence_refs=finding.evidence_refs,
                attempted_resolution="Workshop evidence did not resolve this decision.",
                reviewer_actor_ids=finding.owner_actor_ids,
            )
            self.outbox.commit_foundation_command(
                session_id,
                logical_action_key=f"finish:decision-review:{review_id}",
                command_name="create_review_request",
                command=command,
            )

        self.store.update_phase(
            session_id,
            workshop_state=WorkshopState.COMPLETED,
            conversation_phase=ConversationPhase.HANDOFF_READY,
            now=self.clock.now(),
        )
        return self._result(session_id)

    def _dev_lead(self, _reviews, session, _domain):
        # The fixed case identity is read through the registered delegation premise.
        from .delegation import DEV_LEAD_ACTOR_ID
        return DEV_LEAD_ACTOR_ID

    def _refuse(self, session_id: UUID, message: str):
        self.store.update_phase(
            session_id, workshop_state=WorkshopState.ACTIVE, now=self.clock.now()
        )
        raise ValueError(message)

    def _result(self, session_id: UUID) -> FinishResult:
        session = self.store.get_session(session_id)
        handoff = self.foundation.get_downstream_handoff(QueryOne(
            case_id=session.case_id, acting_actor_id=session.pm_actor_id
        ))
        return FinishResult(
            handoff=handoff,
            workshop_state=session.workshop_state,
            conversation_phase=session.conversation_phase,
        )
