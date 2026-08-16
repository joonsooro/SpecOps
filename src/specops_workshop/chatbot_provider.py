"""Selection-only Luna provider for Task 28 GUIDANCE.

Luna receives no source files, SourceRefs, Terra Conversation ID, or canonical
question text.  The server materializes its selected refs back to exact
Foundation records before the existing admission command runs.
"""

from __future__ import annotations

import json
from typing import Any, Protocol
from uuid import UUID

from openai import AsyncOpenAI
from sqlalchemy import select
from specops_contracts import workshop_v1 as c
from specops_workflow.persistence import (
    TASK28_RUNTIME_TABLES,
    V0_RUNTIME_TABLES,
    WORKSHOP_PROTOCOL_TABLES,
)
from specops_workflow.workshop_protocol import WorkshopFoundationService

from .chat_contracts import (
    ChatbotGuidanceRequest,
    ChatbotGuidanceSelection,
    ChatbotQuestionRef,
)


CHATBOT_MODEL = "gpt-5.6-luna"


class ChatbotProvider(Protocol):
    async def select_guidance(
        self, request: ChatbotGuidanceRequest
    ) -> ChatbotGuidanceSelection: ...


class OpenAIResponsesChatbotProvider:
    """Stateless OpenAI Responses adapter pinned to Luna/medium/store=false."""

    def __init__(self, *, api_key: str, client: Any | None = None) -> None:
        self._client = client or AsyncOpenAI(api_key=api_key, max_retries=0)

    async def select_guidance(
        self, request: ChatbotGuidanceRequest
    ) -> ChatbotGuidanceSelection:
        schema = ChatbotGuidanceSelection.model_json_schema()
        response = await self._client.responses.create(
            model=CHATBOT_MODEL,
            reasoning={"effort": "medium"},
            store=False,
            input=[
                {
                    "role": "developer",
                    "content": (
                        "Select and order only supplied ASKABLE question references. "
                        "Do not author question text, analyze evidence, create proposals, "
                        "perform governance, or finish the Workshop."
                    ),
                },
                {
                    "role": "user",
                    "content": request.model_dump_json(exclude_none=False),
                },
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "chatbot_guidance_selection",
                    "strict": True,
                    "schema": schema,
                }
            },
        )
        return ChatbotGuidanceSelection.model_validate_json(response.output_text)


class ChatbotGuidanceCoordinator:
    """Build Luna's bounded view and materialize only verified Foundation refs."""

    def __init__(
        self,
        foundation: WorkshopFoundationService,
        provider: ChatbotProvider,
        *,
        case_id: UUID,
        session_id: UUID,
    ) -> None:
        self.foundation = foundation
        self.provider = provider
        self.case_id = case_id
        self.session_id = session_id

    def _askable(self) -> tuple[dict[str, Any], ...]:
        records = WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"]
        runway = V0_RUNTIME_TABLES["workshop_runway_items"]
        with self.foundation.engine.connect() as connection:
            rows = connection.execute(
                select(records)
                .where(
                    records.c.case_id == str(self.case_id),
                    records.c.entity_kind == "QUESTION",
                    records.c.status == c.SemanticRecordStatus.OPEN.value,
                )
                .order_by(records.c.foundation_id, records.c.record_version)
            ).mappings().all()
            asked = set(
                connection.execute(
                    select(runway.c.question_id, runway.c.question_version).where(
                        runway.c.case_id == str(self.case_id),
                        runway.c.status == "ASKED",
                    )
                ).all()
            )
            record_by_ref = {
                (row["foundation_id"], row["record_version"]): row
                for row in connection.execute(
                    select(records).where(records.c.case_id == str(self.case_id))
                ).mappings()
            }
        guidance = self.foundation.current_admitted_guidance(self.case_id)
        blocked = (
            set()
            if guidance is None
            else {
                (str(ref.foundation_id), ref.expected_version)
                for ref in guidance.do_not_ask_questions
            }
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            identity = (row["foundation_id"], row["record_version"])
            if identity in asked or identity in blocked:
                continue
            payload = json.loads(row["payload_json"])
            if not payload.get("safe_without_current_turn_interpretation", False):
                continue
            safe = True
            for ref in payload.get("addresses_problem_refs", ()):
                problem = record_by_ref.get(
                    (str(ref["foundation_id"]), ref["expected_version"])
                )
                safe = safe and problem is not None and problem["status"] == "OPEN"
            for ref in payload.get("prerequisite_problem_refs", ()):
                problem = record_by_ref.get(
                    (str(ref["foundation_id"]), ref["expected_version"])
                )
                safe = safe and problem is not None and problem["status"] == "RESOLVED"
            if safe:
                result.append(dict(row, payload=payload))
        return tuple(result)

    async def execute(
        self, analyzer_request: c.ReplenishGuidanceRequest
    ) -> c.GuidanceCandidate:
        askable = self._askable()
        if not askable:
            raise ValueError("no ASKABLE Foundation question exists")
        runway_table = V0_RUNTIME_TABLES["workshop_runway_items"]
        typed = TASK28_RUNTIME_TABLES["workshop_typed_responses"]
        with self.foundation.engine.connect() as connection:
            latest = connection.execute(
                select(typed)
                .where(
                    typed.c.case_id == str(self.case_id),
                    typed.c.session_id == str(self.session_id),
                )
                .order_by(typed.c.turn_sequence.desc(), typed.c.response_version.desc())
                .limit(1)
            ).mappings().one_or_none()
            consumed = connection.execute(
                select(
                    runway_table.c.question_id,
                    runway_table.c.question_version,
                )
                .where(
                    runway_table.c.case_id == str(self.case_id),
                    runway_table.c.status == "ASKED",
                )
                .order_by(runway_table.c.consumed_at, runway_table.c.position)
            ).all()
        case = self.foundation.get_case(self.case_id)
        bounded = ChatbotGuidanceRequest(
            request_identity=f"chatbot-guidance:{analyzer_request.analyzer_run_id}",
            session_id=self.session_id,
            case_revision=analyzer_request.based_on_case_revision,
            readiness=case.readiness,
            review_obligation=case.review_obligation,
            askable_question_refs=tuple(
                ChatbotQuestionRef(
                    question_id=UUID(row["foundation_id"]),
                    question_version=row["record_version"],
                )
                for row in askable
            ),
            consumed_question_refs=tuple(
                ChatbotQuestionRef(
                    question_id=UUID(identity), question_version=version
                )
                for identity, version in consumed
            ),
            latest_response_id=None if latest is None else UUID(latest["response_id"]),
            latest_question_ref=(
                None
                if latest is None
                else ChatbotQuestionRef(
                    question_id=UUID(latest["question_id"]),
                    question_version=latest["question_version"],
                )
            ),
        )
        selection = await self.provider.select_guidance(bounded)
        supplied = {
            (UUID(row["foundation_id"]), row["record_version"]): row
            for row in askable
        }
        selected_refs = (
            selection.recommended_question_ref,
            *selection.safe_alternate_refs,
        )
        selected_keys = [
            (item.question_id, item.question_version) for item in selected_refs
        ]
        blocked_keys = [
            (item.question_id, item.question_version)
            for item in selection.do_not_ask_question_refs
        ]
        if (
            len(selected_keys) != len(set(selected_keys))
            or len(blocked_keys) != len(set(blocked_keys))
            or set(selected_keys).intersection(blocked_keys)
            or any(key not in supplied for key in (*selected_keys, *blocked_keys))
        ):
            raise ValueError("Luna selected a ref outside the supplied ASKABLE set")

        def question(ref: ChatbotQuestionRef) -> c.GuidanceQuestion:
            row = supplied[(ref.question_id, ref.question_version)]
            payload = row["payload"]
            return c.GuidanceQuestion(
                question_ref=c.FoundationEntityRef(
                    ref_kind="FOUNDATION_ID",
                    foundation_id=ref.question_id,
                    expected_version=ref.question_version,
                ),
                exact_text=payload["text"],
                reason=payload["rationale"],
            )

        dependencies = [
            c.GuidanceDependency(
                dependency_kind=c.GuidanceDependencyKind.SOURCE_SET,
                entity_ref=None,
            )
        ]
        dependencies.extend(
            c.GuidanceDependency(
                dependency_kind=c.GuidanceDependencyKind.QUESTION,
                entity_ref=c.FoundationEntityRef(
                    ref_kind="FOUNDATION_ID",
                    foundation_id=ref.question_id,
                    expected_version=ref.question_version,
                ),
            )
            for ref in selected_refs
        )
        return c.GuidanceCandidate(
            protocol_version=c.PROTOCOL_VERSION,
            output_type="GUIDANCE_CANDIDATE",
            analyzer_run_id=analyzer_request.analyzer_run_id,
            context_id=analyzer_request.context_id,
            request_hash=analyzer_request.request_hash,
            source_set_hash=analyzer_request.source_set_hash,
            based_on_case_revision=analyzer_request.based_on_case_revision,
            recommended_question=question(selection.recommended_question_ref),
            safe_alternates=tuple(question(ref) for ref in selection.safe_alternate_refs),
            do_not_ask_question_refs=tuple(
                c.FoundationEntityRef(
                    ref_kind="FOUNDATION_ID",
                    foundation_id=ref.question_id,
                    expected_version=ref.question_version,
                )
                for ref in selection.do_not_ask_question_refs
            ),
            dependencies=tuple(dependencies),
            acknowledgement_suggestion=selection.acknowledgement_suggestion,
        )
