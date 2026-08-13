"""Database-leased, browser-independent V0 Analyzer worker."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Callable

from pydantic import TypeAdapter
from specops_contracts import workshop_v1 as c

from .openai_adapter import ProviderAdapterError
from .orchestrator import V4ProductionOrchestrator, _request


class DurableAnalyzerWorker:
    """Resume provider and Foundation stages without coupling work to Voice."""

    def __init__(
        self,
        orchestrator: V4ProductionOrchestrator,
        *,
        worker_id: str = "v0-analyzer-worker",
        poll_seconds: float = 0.1,
        has_active_clients: Callable[[], bool] = lambda: False,
    ) -> None:
        self.orchestrator = orchestrator
        self.foundation = orchestrator.foundation
        self.worker_id = worker_id
        self.poll_seconds = poll_seconds
        self.has_active_clients = has_active_clients
        self._stopping = asyncio.Event()

    async def run_once(self) -> bool:
        projection = self.foundation.preparation_projection(self.orchestrator.case_id)
        if projection["workshop_complete_at"] is not None:
            await self.orchestrator.advance_workshop_completion()
            projection = self.foundation.preparation_projection(self.orchestrator.case_id)
        if projection["cleanup_state"] == "RESTART_GRACE":
            await self.orchestrator.expire_restart_grace()
            projection = self.foundation.preparation_projection(self.orchestrator.case_id)
        if projection["cleanup_state"] == "RETRY_WAIT":
            available_at = projection["cleanup_available_at"]
            if available_at is not None and self.foundation.now() < datetime.fromisoformat(
                available_at.replace("Z", "+00:00")
            ):
                return False
        if projection["cleanup_state"] in {
            "PENDING",
            "IN_PROGRESS",
            "RETRY_WAIT",
        }:
            completed = await self.orchestrator.cleanup_preparation()
            if (
                completed["cleanup_state"] == "COMPLETED"
                and completed["cleanup_reason"] == "RESTART_GRACE_EXPIRED"
                and self.has_active_clients()
            ):
                self.foundation.reset_preparation_after_provider_cleanup(
                    self.orchestrator.case_id
                )
            return True
        if projection["phase"] not in {"READY", "FAILED"}:
            try:
                projection = await self.orchestrator.prepare_workshop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                current = self.foundation.preparation_projection(
                    self.orchestrator.case_id
                )
                retention_state = (
                    current["cleanup_state"]
                    if current["cleanup_state"] in {"RESTART_GRACE", "RETAIN_UNCERTAIN"}
                    else "NOT_REQUIRED"
                )
                self.foundation.set_preparation_phase(
                    self.orchestrator.case_id,
                    "FAILED",
                    failure_code=f"PREPARATION_{type(exc).__name__.upper()}",
                    cleanup_state=retention_state,
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
            current = self._job(job["job_id"])
            if (
                current["state"] == "PROVIDER_REQUESTED"
                and current["candidate_json"] is None
            ):
                self.foundation.fail_analyzer_job(
                    job["job_id"],
                    worker_id=self.worker_id,
                    error_code="PROVIDER_OUTCOME_UNCERTAIN_CANCELLED",
                )
            raise
        except ProviderAdapterError as exc:
            uncertain = exc.receipt.code in {
                c.ProviderFailureCode.TIMEOUT,
                c.ProviderFailureCode.CONNECTION,
            }
            if uncertain or not exc.receipt.retryable or job["attempt_count"] >= 3:
                self.foundation.fail_analyzer_job(
                    job["job_id"],
                    worker_id=self.worker_id,
                    error_code=(
                        "PROVIDER_OUTCOME_UNCERTAIN_"
                        if uncertain
                        else "PROVIDER_REJECTED_"
                    )
                    + exc.receipt.code.value,
                )
            else:
                self.foundation.release_analyzer_job(
                    job["job_id"],
                    worker_id=self.worker_id,
                    error_code=f"PROVIDER_RETRYABLE_{exc.receipt.code.value}",
                )
        except Exception as exc:
            current = self._job(job["job_id"])
            uncertain = (
                current["state"] == "PROVIDER_REQUESTED"
                and current["candidate_json"] is None
            )
            if uncertain or job["attempt_count"] >= 3:
                self.foundation.fail_analyzer_job(
                    job["job_id"],
                    worker_id=self.worker_id,
                    error_code=(
                        "PROVIDER_OUTCOME_UNCERTAIN_"
                        if uncertain
                        else "RETRY_EXHAUSTED_"
                    )
                    + type(exc).__name__,
                )
            else:
                self.foundation.release_analyzer_job(
                    job["job_id"],
                    worker_id=self.worker_id,
                    error_code=type(exc).__name__,
                )
        return True

    def _job(self, job_id: str) -> dict[str, Any]:
        return next(
            item
            for item in self.foundation.analyzer_jobs(self.orchestrator.case_id)
            if item["job_id"] == job_id
        )

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
