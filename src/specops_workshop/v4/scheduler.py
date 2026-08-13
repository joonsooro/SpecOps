"""Database-leased, browser-independent V0 Analyzer worker."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import TypeAdapter
from specops_contracts import workshop_v1 as c

from .orchestrator import V4ProductionOrchestrator, _request


class DurableAnalyzerWorker:
    """Resume provider and Foundation stages without coupling work to Voice."""

    def __init__(
        self,
        orchestrator: V4ProductionOrchestrator,
        *,
        worker_id: str = "v0-analyzer-worker",
        poll_seconds: float = 0.1,
    ) -> None:
        self.orchestrator = orchestrator
        self.foundation = orchestrator.foundation
        self.worker_id = worker_id
        self.poll_seconds = poll_seconds
        self._stopping = asyncio.Event()

    async def run_once(self) -> bool:
        projection = self.foundation.preparation_projection(self.orchestrator.case_id)
        if projection["phase"] == "FAILED" and projection["cleanup_state"] in {
            "PENDING",
            "IN_PROGRESS",
        }:
            await self.orchestrator.cleanup_preparation()
            return True
        if projection["phase"] not in {"READY", "FAILED"}:
            try:
                projection = await self.orchestrator.prepare_workshop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                resources = self.foundation.preparation_resources(
                    self.orchestrator.case_id
                )
                cleanup_required = any(
                    resources[name]
                    for name in (
                        "pm_file_id",
                        "technical_file_id",
                        "provider_conversation_id",
                    )
                ) and self.foundation.active_analyzer_context(
                    self.orchestrator.case_id
                ) is None
                self.foundation.set_preparation_phase(
                    self.orchestrator.case_id,
                    "FAILED",
                    failure_code=f"PREPARATION_{type(exc).__name__.upper()}",
                    cleanup_state="PENDING" if cleanup_required else "NOT_REQUIRED",
                )
                return False
        if projection["phase"] != "READY":
            return False
        job = self.foundation.claim_analyzer_job(
            self.orchestrator.case_id, worker_id=self.worker_id
        )
        if job is None:
            return False
        try:
            await self._run_job(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if job["attempt_count"] >= 3:
                self.foundation.fail_analyzer_job(
                    job["job_id"],
                    worker_id=self.worker_id,
                    error_code=f"RETRY_EXHAUSTED_{type(exc).__name__}",
                )
            else:
                self.foundation.release_analyzer_job(
                    job["job_id"],
                    worker_id=self.worker_id,
                    error_code=type(exc).__name__,
                )
        return True

    async def drain(self, *, limit: int = 100) -> int:
        count = 0
        while count < limit and await self.run_once():
            count += 1
        return count

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            worked = await self.run_once()
            if not worked:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass

    def stop(self) -> None:
        self._stopping.set()

    async def _run_job(self, job: dict[str, Any]) -> None:
        context = await self.orchestrator.ensure_context()
        request = (
            TypeAdapter(c.AnalyzerProviderRequest).validate_json(job["request_json"])
            if job["request_json"]
            else self._build_request(job, context)
        )
        if not job["request_json"]:
            self.foundation.checkpoint_analyzer_job(
                job["job_id"],
                worker_id=self.worker_id,
                state="PROVIDER_REQUESTED",
                request_json=request.model_dump_json(),
            )
        candidate = (
            TypeAdapter(c.AnalyzerProviderCandidate).validate_json(job["candidate_json"])
            if job["candidate_json"]
            else await self.orchestrator.adapter.execute(request, context=context)
        )
        if not job["candidate_json"]:
            self.foundation.checkpoint_analyzer_job(
                job["job_id"],
                worker_id=self.worker_id,
                state="PROVIDER_COMPLETED",
                candidate_json=candidate.model_dump_json(),
            )
        receipt = (
            c.ProposalAdmissionReceipt.model_validate_json(job["admission_receipt_json"])
            if job["admission_receipt_json"]
            else self._admit(request, candidate)
        )
        if not job["admission_receipt_json"]:
            self.foundation.checkpoint_analyzer_job(
                job["job_id"],
                worker_id=self.worker_id,
                state="FOUNDATION_ADMITTED",
                admission_receipt_json=receipt.model_dump_json(),
            )
        self.foundation.checkpoint_analyzer_job(
            job["job_id"], worker_id=self.worker_id, state="COMPLETED"
        )

    def _build_request(self, job: dict[str, Any], context: c.AnalyzerContextBinding):
        snapshot = self.foundation.semantic_snapshot(self.orchestrator.case_id)
        common = self.orchestrator._provider_base(
            operation=c.AnalyzerOperation(job["operation"]),
            operation_key=job["job_id"],
            context=context,
            based_on_revision=snapshot.case_revision,
        )
        if job["operation"] == c.AnalyzerOperation.TURN_ANALYSIS.value:
            transcripts = self.foundation.final_transcripts(self.orchestrator.case_id)
            index = next(
                index for index, item in enumerate(transcripts) if str(item.event_id) == job["subject_id"]
            )
            event = transcripts[index]
            prior = transcripts[index - 1] if index else None
            return _request(
                c.AnalyzeFinalTurnRequest,
                dict(
                    common,
                    transcript=c.FinalizedTranscriptInput(
                        transcript_event_id=event.event_id,
                        transcript_hash=event.transcript_hash,
                        speaker_actor_id=event.speaker_actor_id,
                        actor=event.actor,
                        sequence_number=event.sequence_number,
                        text=event.text,
                    ),
                    prior_transcript=(
                        None
                        if prior is None
                        else c.PriorTranscriptBinding(
                            transcript_event_id=prior.event_id,
                            transcript_hash=prior.transcript_hash,
                            sequence_number=prior.sequence_number,
                        )
                    ),
                    foundation_snapshot=snapshot,
                    requested_output="TURN_ANALYSIS_CANDIDATE",
                ),
            )
        if job["operation"] == c.AnalyzerOperation.GUIDANCE.value:
            runway = self.foundation.runway_projection(self.orchestrator.case_id)
            return _request(
                c.ReplenishGuidanceRequest,
                dict(
                    common,
                    foundation_snapshot=snapshot,
                    runway_state=c.RunwayStateSnapshot(
                        active_question_refs=tuple(
                            c.FoundationEntityRef(
                                ref_kind="FOUNDATION_ID",
                                foundation_id=item["question_id"],
                                expected_version=item["question_version"],
                            )
                            for item in runway["questions"]
                        ),
                        asked_question_refs=tuple(
                            c.FoundationEntityRef(
                                ref_kind="FOUNDATION_ID",
                                foundation_id=identity,
                                expected_version=1,
                            )
                            for identity in runway["asked"]
                        ),
                        desired_safe_depth=5,
                    ),
                    requested_output="GUIDANCE_CANDIDATE",
                ),
            )
        raise RuntimeError("V0 worker accepts only TURN_ANALYSIS and GUIDANCE jobs")

    def _admit(self, request, candidate) -> c.ProposalAdmissionReceipt:
        if isinstance(request, c.AnalyzeFinalTurnRequest):
            command_type = "ADMIT_TURN_ANALYSIS"
            model = c.AdmitTurnAnalysisCommand
        elif isinstance(request, c.ReplenishGuidanceRequest):
            command_type = "ADMIT_GUIDANCE"
            model = c.AdmitGuidanceCommand
        else:
            raise RuntimeError("unsupported durable Analyzer operation")
        values = self.orchestrator._command_base(
            command_type,
            str(request.analyzer_run_id),
            expected_revision=request.based_on_case_revision,
        )
        values.update(
            command_type=command_type,
            analyzer_run_id=request.analyzer_run_id,
            context_id=request.context_id,
            provider_request_hash=request.request_hash,
            candidate=candidate,
        )
        receipt = self.foundation.execute(model(**values))
        assert isinstance(receipt, c.ProposalAdmissionReceipt)
        return receipt
